#!/usr/bin/env python3
"""Query the arXiv search API and emit accumulated RSS 2.0 feeds.

For each query in QUERIES, fetches the latest matching papers via arXiv's
Atom search API, converts to RSS 2.0 items, merges with the existing on-disk
feed (preserves history), sorts by submitted date desc, writes feeds/<name>.xml.

arXiv allows ~1 req / 3s; we run a couple of queries every 15 min — well under.
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx

ROOT = Path(__file__).parent
FEEDS_DIR = ROOT / "feeds"
MAX_ITEMS = 5000

ARXIV_ICON = "https://static.arxiv.org/static/browse/0.3.4/images/icons/apple-touch-icon.png"

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

# name -> (search_query, channel_title, channel_description)
#
# Design note on `quant-llm`:
#   The intersection target is "LLMs applied to quant/finance/economics".
#   Naïvely AND-ing finance keywords with LLM keywords across all categories
#   floods with false positives — `transformer`/`GPT`/`LLM` appear in
#   essentially any modern ML paper, and `market`/`trading` show up casually
#   in unrelated abstracts. The reliable signal is the SUBMITTER'S category
#   choice: papers actually about finance/economics get tagged q-fin.* or
#   econ.*. So we restrict to those categories AND require an LLM keyword.
#   "agentic" is intentionally excluded — it false-positives on classical
#   agent-based market-microstructure simulation papers.
QUERIES: dict[str, tuple[str, str, str]] = {
    "quant-llm": (
        '(cat:q-fin.CP OR cat:q-fin.TR OR cat:q-fin.PM OR cat:q-fin.ST OR cat:q-fin.MF OR cat:q-fin.RM OR cat:q-fin.GN OR cat:q-fin.EC OR cat:econ.GN OR cat:econ.EM OR cat:econ.TH)'
        ' AND '
        '(abs:LLM OR abs:"large language model" OR abs:"language model" OR abs:transformer OR abs:GPT OR abs:"foundation model")',
        "arXiv — LLMs in Quant / Finance / Economics",
        "Papers categorized under q-fin.* or econ.* whose abstract mentions LLMs, language models, transformers, GPT, or foundation models.",
    ),
}

log = logging.getLogger("arxiv_search")

_NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc":      "http://purl.org/dc/elements/1.1/",
    "atom":    "http://www.w3.org/2005/Atom",
}
for _p, _u in _NS.items():
    ET.register_namespace(_p, _u)


def fetch_arxiv(query: str, max_results: int = 100) -> list[ET.Element]:
    url = "https://export.arxiv.org/api/query"
    params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": str(max_results),
    }
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        r = c.get(url, params=params, headers={"User-Agent": UA})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    return list(root.findall(ATOM + "entry"))


def atom_to_rss_item(entry: ET.Element) -> ET.Element:
    item = ET.Element("item")

    title = re.sub(r"\s+", " ", (entry.findtext(ATOM + "title") or "").strip())
    ET.SubElement(item, "title").text = title

    link = ""
    for ln in entry.findall(ATOM + "link"):
        if ln.get("rel") == "alternate" and ln.get("type") == "text/html":
            link = ln.get("href", "")
            break
    if not link:
        link = (entry.findtext(ATOM + "id") or "").strip()
    ET.SubElement(item, "link").text = link

    guid = ET.SubElement(item, "guid")
    guid.text = link
    guid.set("isPermaLink", "true" if link.startswith("http") else "false")

    pub_raw = entry.findtext(ATOM + "published") or entry.findtext(ATOM + "updated") or ""
    try:
        dt = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
        ET.SubElement(item, "pubDate").text = format_datetime(dt)
    except Exception:
        pass

    summary = re.sub(r"\s+", " ", (entry.findtext(ATOM + "summary") or "").strip())

    authors: list[str] = []
    for a in entry.findall(ATOM + "author"):
        n = a.findtext(ATOM + "name")
        if n:
            authors.append(n.strip())
    if authors:
        ET.SubElement(item, f"{{{_NS['dc']}}}creator").text = ", ".join(authors)

    cats: list[str] = []
    for c in entry.findall(ATOM + "category"):
        term = c.get("term")
        if term:
            cats.append(term)
            ET.SubElement(item, "category").text = term

    pdf = ""
    for ln in entry.findall(ATOM + "link"):
        if ln.get("title") == "pdf":
            pdf = ln.get("href", "")
            break

    html_parts = [
        f'<p><strong>Authors:</strong> {", ".join(authors) or "(unknown)"}</p>',
        f'<p><strong>Categories:</strong> {", ".join(cats) or "—"}</p>',
        f'<p>{summary}</p>',
    ]
    if link:
        html_parts.append(f'<p><a href="{link}">abstract</a>')
        if pdf:
            html_parts[-1] += f' · <a href="{pdf}">PDF</a>'
        html_parts[-1] += '</p>'
    ET.SubElement(item, f"{{{_NS['content']}}}encoded").text = "".join(html_parts)
    ET.SubElement(item, "description").text = summary[:500]
    return item


def item_guid(it: ET.Element) -> str:
    return (it.findtext("guid") or it.findtext("link") or "").strip()


def item_date(it: ET.Element) -> datetime:
    try:
        return parsedate_to_datetime(it.findtext("pubDate") or "")
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def build(name: str, new_items: list[ET.Element], title: str, desc: str) -> bytes:
    seen = {item_guid(it) for it in new_items}
    seen.discard("")
    items = list(new_items)

    existing = FEEDS_DIR / f"{name}.xml"
    if existing.exists():
        try:
            prev = ET.parse(existing).getroot()
            prev_ch = prev.find("channel")
            if prev_ch is not None:
                for old in prev_ch.findall("item"):
                    g = item_guid(old)
                    if g and g not in seen:
                        items.append(old)
                        seen.add(g)
        except Exception as e:
            log.warning(f"existing {name}.xml unreadable: {e}")

    items.sort(key=item_date, reverse=True)
    items = items[:MAX_ITEMS]

    out = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(out, "channel")
    link_text = f"https://melroseee-e.github.io/twitter-rss/{name}.xml"
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = link_text
    ET.SubElement(channel, "description").text = desc
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(channel, "ttl").text = "60"
    img = ET.SubElement(channel, "image")
    ET.SubElement(img, "url").text = ARXIV_ICON
    ET.SubElement(img, "title").text = title
    ET.SubElement(img, "link").text = link_text
    for it in items:
        channel.append(it)

    ET.indent(out, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(out, encoding="utf-8")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    for name, (query, title, desc) in QUERIES.items():
        log.info(f"--- {name}  query: {query[:90]}{'…' if len(query) > 90 else ''}")
        try:
            entries = fetch_arxiv(query)
            log.info(f"{name}: arXiv returned {len(entries)} entries")
            new_items = [atom_to_rss_item(e) for e in entries]
            data = build(name, new_items, title, desc)
            (FEEDS_DIR / f"{name}.xml").write_bytes(data)
            count = len(ET.fromstring(data).find("channel").findall("item"))
            log.info(f"{name}: {count} items total, {len(data) // 1024} KB")
            ok += 1
        except Exception as e:
            log.error(f"{name}: {e}")
    log.info(f"done: {ok}/{len(QUERIES)} queries ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
