#!/usr/bin/env python3
"""Merge multiple already-accumulated feeds into one combined feed.

Reads `merge_feeds.txt`. Each non-comment line:

    <output_name>  <src1> <src2> ...

For each line, loads `feeds/<src>.xml`, collects every <item>, dedupes by
GUID/link, sorts by pubDate desc, and writes `feeds/<output_name>.xml`. Each
item gains a `<source url="...">Source Name</source>` element pointing back to
its origin feed (RSS 2.0 standard; harmless to readers that ignore it).

Run AFTER accumulate.py so source feeds are fresh on disk.
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).parent
FEEDS_DIR = ROOT / "feeds"
CONFIG = ROOT / "merge_feeds.txt"

MAX_ITEMS = 5000

# Per-output channel icon. RSS readers (Reeder, Folo) pick this up as the
# feed's avatar. Folo caches metadata by URL — if you change this AFTER
# subscribing, append `?v=N` to the feed URL to force re-registration.
ARXIV_ICON = "https://static.arxiv.org/static/browse/0.3.4/images/icons/apple-touch-icon.png"
ICONS: dict[str, str] = {
    "quant-papers": ARXIV_ICON,
}

log = logging.getLogger("merge_feeds")

# Mirror accumulate.py's namespace registrations so prefixes (content:encoded,
# dc:creator, etc.) survive serialization.
_NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc":      "http://purl.org/dc/elements/1.1/",
    "atom":    "http://www.w3.org/2005/Atom",
    "itunes":  "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "media":   "http://search.yahoo.com/mrss/",
    "sy":      "http://purl.org/rss/1.0/modules/syndication/",
    "webfeeds":"http://webfeeds.org/rss/1.0",
    "wfw":     "http://wellformedweb.org/CommentAPI/",
    "slash":   "http://purl.org/rss/1.0/modules/slash/",
    "podcast": "https://podcastindex.org/namespace/1.0",
}
for _p, _u in _NS.items():
    ET.register_namespace(_p, _u)


def parse_config(path: Path) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for raw in path.read_text().splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = re.split(r"\s+", ln)
        if len(parts) < 2:
            log.warning(f"skip malformed line: {ln!r}")
            continue
        out.append((parts[0], parts[1:]))
    return out


def item_guid(it: ET.Element) -> str:
    g = it.findtext("guid") or it.findtext("link") or it.findtext("title") or ""
    return g.strip()


def item_date(it: ET.Element) -> datetime:
    txt = it.findtext("pubDate") or ""
    try:
        return parsedate_to_datetime(txt)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def load_feed(src: str) -> tuple[str, str, list[ET.Element]]:
    """Return (channel_title, channel_link, items) for feeds/<src>.xml."""
    p = FEEDS_DIR / f"{src}.xml"
    if not p.exists():
        log.warning(f"missing feeds/{src}.xml — skip")
        return "", "", []
    try:
        root = ET.parse(p).getroot()
    except Exception as e:
        log.warning(f"unparseable feeds/{src}.xml: {e}")
        return "", "", []
    ch = root.find("channel")
    if ch is None:
        return "", "", []
    title = (ch.findtext("title") or src).strip()
    link = (ch.findtext("link") or "").strip()
    return title, link, list(ch.findall("item"))


def tag_source(it: ET.Element, title: str, link: str) -> None:
    """Add/replace <source url="...">Title</source> on the item."""
    for old in it.findall("source"):
        it.remove(old)
    src_el = ET.SubElement(it, "source")
    if link:
        src_el.set("url", link)
    src_el.text = title


def build_merged(name: str, sources: list[str]) -> bytes:
    items: list[ET.Element] = []
    seen: set[str] = set()
    feed_titles: list[str] = []
    for src in sources:
        title, link, src_items = load_feed(src)
        if not src_items:
            continue
        feed_titles.append(title)
        for it in src_items:
            g = item_guid(it)
            if not g or g in seen:
                continue
            seen.add(g)
            tag_source(it, title, link)
            items.append(it)
    items.sort(key=item_date, reverse=True)
    items = items[:MAX_ITEMS]

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    title_text = f"Merged: {name}"
    link_text = f"https://melroseee-e.github.io/twitter-rss/{name}.xml"
    ET.SubElement(channel, "title").text = title_text
    ET.SubElement(channel, "link").text = link_text
    ET.SubElement(channel, "description").text = "Merged feed of: " + ", ".join(feed_titles)
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(channel, "ttl").text = "15"
    if name in ICONS:
        img = ET.SubElement(channel, "image")
        ET.SubElement(img, "url").text = ICONS[name]
        ET.SubElement(img, "title").text = title_text
        ET.SubElement(img, "link").text = link_text
    for it in items:
        channel.append(it)

    ET.indent(rss, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="utf-8")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not CONFIG.exists():
        log.info("no merge_feeds.txt — nothing to do")
        return 0
    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    groups = parse_config(CONFIG)
    log.info(f"processing {len(groups)} merge groups")
    ok = 0
    for name, sources in groups:
        log.info(f"--- {name}  ←  {' + '.join(sources)} ---")
        try:
            data = build_merged(name, sources)
            (FEEDS_DIR / f"{name}.xml").write_bytes(data)
            n = len(ET.fromstring(data).find("channel").findall("item"))
            log.info(f"{name}: {n} items, {len(data) // 1024} KB")
            ok += 1
        except Exception as e:
            log.error(f"{name}: {e}")
    log.info(f"done: {ok}/{len(groups)} merged feeds ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
