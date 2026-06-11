#!/usr/bin/env python3
"""Mirror + accumulate external RSS feeds.

For each entry in `accumulate.txt`, fetch the source RSS, merge its <item>s
with the locally-stored feed file (hydrated beforehand from Pages), dedupe by
GUID, sort by pubDate desc, keep top N. Write back to feeds/<name>.xml.

Handles RSS 2.0. Atom is not handled here (none of our sources are Atom).
"""
from __future__ import annotations

import html
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx

ROOT = Path(__file__).parent
FEEDS_DIR = ROOT / "feeds"
CONFIG = ROOT / "accumulate.txt"

# Podcasts + YouTube feeds are small; Substack/OpenAI can be large. 5000 cap
# is generous and comfortably fits any real-world feed size.
MAX_ITEMS = 5000

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/rss+xml,application/xml,text/xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}
log = logging.getLogger("accumulate")


def parse_config(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    rsshub_key = os.environ.get("RSSHUB_KEY", "")
    for raw in path.read_text().splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = re.split(r"\s+", ln, maxsplit=1)
        if len(parts) != 2:
            log.warning(f"skip malformed line: {ln!r}")
            continue
        name, url = parts
        if "${RSSHUB_KEY}" in url:
            if not rsshub_key:
                log.warning(f"skip {name}: RSSHUB_KEY env var not set")
                continue
            url = url.replace("${RSSHUB_KEY}", rsshub_key)
        out.append((name, url))
    return out


def fetch(url: str, client: httpx.Client) -> bytes | None:
    try:
        r = client.get(url, headers=HEADERS, timeout=60)
        if r.status_code == 200 and len(r.content) > 100 and b"<rss" in r.content[:2000]:
            return r.content
        log.warning(f"fetch {url}: HTTP {r.status_code} size={len(r.content)}")
    except Exception as e:
        log.warning(f"fetch {url}: {e}")
    return None


def item_guid(it: ET.Element) -> str:
    g = it.findtext("guid") or it.findtext("link") or it.findtext("title") or ""
    return g.strip()


def item_date(it: ET.Element) -> datetime:
    txt = it.findtext("pubDate") or ""
    try:
        return parsedate_to_datetime(txt)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


# Register common RSS namespace prefixes so ElementTree serializes them with
# their real names (e.g. <content:encoded>) instead of auto-generated ns0/ns1
# etc. Many readers (Reeder among them) match `<content:encoded>` by prefix
# rather than by namespace URI — lose the prefix and they fall back to the
# plaintext <description>, killing rich HTML rendering.
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
    "cc":      "http://web.resource.org/cc/",
    "georss":  "http://www.georss.org/georss",
    "geo":     "http://www.w3.org/2003/01/geo/wgs84_pos#",
    "podcast": "https://podcastindex.org/namespace/1.0",
}
for _p, _u in _NS.items():
    ET.register_namespace(_p, _u)


# --- 橘鸦 AI 早报 排版器 -------------------------------------------------------
# 橘鸦的官方 GitHub RSS(已随账号被封而死)是排好版的富文本;现在只能抓他 YouTube,
# 而 YouTube 简介是一坨"标题 时间戳 裸链接…"的章节流水账。这里把它还原成
# 「每条新闻 = 小标题 + 来源链接列表」的干净 HTML(<content:encoded>)。幂等。
_CONTENT_ENC = "{http://purl.org/rss/1.0/modules/content/}encoded"
_TS_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")   # YouTube 章节时间戳 00:09 / 1:23:45
_URL_RE = re.compile(r"^https?://")


def _domain(u: str) -> str:
    m = re.match(r"https?://([^/]+)", u)
    return (m.group(1) if m else u).replace("www.", "")


def format_juya_description(text: str) -> tuple[str | None, list[str]]:
    """YouTube 章节式简介 → (HTML, [各条标题])。不像章节列表则返回 (None, [])."""
    raw = text or ""
    m_img = re.search(r'<img[^>]+src="([^"]+)"', raw)   # 抽 youtube 缩略图当封面
    cover = m_img.group(1) if m_img else None
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)            # <br/> → 换行
    raw = re.sub(r"<[^>]+>", " ", raw)                   # 去掉 <img> 等其它标签
    toks = html.unescape(raw).replace("　", " ").split()
    items: list[tuple[str, list[str]]] = []
    title: list[str] = []
    urls: list[str] = []
    state = "title"

    def flush() -> None:
        t = " ".join(title).strip()
        if t and t.lower() != "intro" and urls:   # 跳过 Intro 与无链接的尾巴
            items.append((t, urls.copy()))

    for tok in toks:
        if _TS_RE.match(tok):
            state = "urls"
            continue
        if _URL_RE.match(tok):
            urls.append(tok)
            continue
        if state == "urls":           # url 之后又冒出文字 = 下一条新闻开始
            flush()
            title.clear()
            urls.clear()
            title.append(tok)
            state = "title"
        else:
            title.append(tok)
    flush()

    if not items:
        return None, []
    parts: list[str] = []
    if cover:
        parts.append(f'<p><img src="{html.escape(cover)}"/></p>')
    for t, us in items:
        lis = "".join(
            f'<li><a href="{html.escape(u)}">{html.escape(_domain(u))}</a></li>'
            for u in us
        )
        parts.append(f"<h3>{html.escape(t)}</h3>\n<ul>{lis}</ul>")
    return "\n".join(parts), [t for t, _ in items]


def juya_format_item(it: ET.Element) -> None:
    """给一条橘鸦 YouTube item 幂等地补上干净 <content:encoded> + 清爽 description."""
    if it.find(_CONTENT_ENC) is not None:
        return  # 已格式化(或本就是橘鸦旧 GitHub 富文本条目)→ 不动
    body, titles = format_juya_description(it.findtext("description") or "")
    if not body:
        return  # 不是章节式简介 → 原样保留
    ET.SubElement(it, _CONTENT_ENC).text = body
    desc = it.find("description")
    summary = "｜".join(titles)
    if desc is not None:
        desc.text = summary
    else:
        ET.SubElement(it, "description").text = summary


def merge(source_bytes: bytes, existing_path: Path, limit: int = MAX_ITEMS,
          item_transform=None) -> bytes:
    """Parse source RSS 2.0; merge its items with existing file (if any)."""
    source_root = ET.fromstring(source_bytes)
    channel = source_root.find("channel")
    if channel is None:
        raise ValueError("no <channel> in source — not RSS 2.0")

    new_items = list(channel.findall("item"))
    seen_guids = {item_guid(it) for it in new_items if item_guid(it)}

    if existing_path.exists():
        try:
            prev_root = ET.parse(existing_path).getroot()
            prev_channel = prev_root.find("channel")
            if prev_channel is not None:
                for old_it in prev_channel.findall("item"):
                    g = item_guid(old_it)
                    if g and g not in seen_guids:
                        new_items.append(old_it)
                        seen_guids.add(g)
        except Exception as e:
            log.warning(f"existing {existing_path.name} unreadable: {e}")

    new_items.sort(key=item_date, reverse=True)
    new_items = new_items[:limit]

    if item_transform:
        for it in new_items:
            try:
                item_transform(it)
            except Exception as e:
                log.warning(f"item_transform failed on an item: {e}")

    # Replace items in source with merged set
    for it in list(channel.findall("item")):
        channel.remove(it)
    for it in new_items:
        channel.append(it)

    # Refresh lastBuildDate
    lbd = channel.find("lastBuildDate")
    now_rfc = format_datetime(datetime.now(timezone.utc))
    if lbd is not None:
        lbd.text = now_rfc
    else:
        ET.SubElement(channel, "lastBuildDate").text = now_rfc

    ET.indent(source_root, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(source_root, encoding="utf-8")


def process_one(name: str, url: str, client: httpx.Client) -> bool:
    source = fetch(url, client)
    if source is None:
        return False
    transform = juya_format_item if name == "juya-ai-daily" else None
    try:
        merged = merge(source, FEEDS_DIR / f"{name}.xml", item_transform=transform)
    except Exception as e:
        log.error(f"{name}: merge failed: {e}")
        return False
    out_path = FEEDS_DIR / f"{name}.xml"
    out_path.write_bytes(merged)
    # Quick summary
    try:
        count = len(ET.fromstring(merged).find("channel").findall("item"))
        size_kb = len(merged) // 1024
        log.info(f"{name}: {count} items, {size_kb} KB")
    except Exception:
        log.info(f"{name}: wrote {len(merged)} bytes")
    return True


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    entries = parse_config(CONFIG)
    log.info(f"processing {len(entries)} accumulated feeds")

    ok = 0
    with httpx.Client(http2=True, follow_redirects=True) as client:
        for name, url in entries:
            # Truncate url for logging (hide key)
            shown = re.sub(r"key=[^&]+", "key=***", url)
            log.info(f"--- {name}  ←  {shown} ---")
            if process_one(name, url, client):
                ok += 1
            time.sleep(0.5)

    log.info(f"done: {ok}/{len(entries)} feeds ok")
    # Always exit 0 — partial upstream failures are normal (IP blocks, rate
    # limits). The already-hydrated existing feed on disk is preserved for
    # any source that failed this run, so the feed doesn't disappear.
    return 0


if __name__ == "__main__":
    sys.exit(main())
