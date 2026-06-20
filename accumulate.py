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
        head = r.content[:2000]
        # 接受 RSS 2.0(<rss>)与 Atom(<feed>,如 YouTube 原生 feed)。
        if r.status_code == 200 and len(r.content) > 100 and (b"<rss" in head or b"<feed" in head):
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


def _parse_juya_chapters(text: str) -> tuple[str | None, list[tuple[str, list[str]]]]:
    """YouTube 章节式简介 → (cover, [(标题,[来源url])])。不像章节列表则 (None, [])."""
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
    return cover, items


def _parse_prev_formatted(ce_text: str) -> tuple[str | None, list[tuple[str, list[str]]]]:
    """从本脚本旧版排版的 content:encoded 里反解出 (cover,[(标题,[url])]),便于换版重排。"""
    cover = None
    m = re.search(r'<img[^>]+src="([^"]+)"', ce_text or "")
    if m:
        cover = m.group(1)
    items: list[tuple[str, list[str]]] = []
    # 旧版: <h3>标题</h3><ul>..<a href=url>..</ul>  /  新版: <li>标题 <a href=url>↗</a> #N</li>
    for hm in re.finditer(r"<h3>(.*?)</h3>\s*<ul>(.*?)</ul>", ce_text or "", re.S):
        t = html.unescape(re.sub(r"<[^>]+>", "", hm.group(1))).strip()
        us = re.findall(r'href="([^"]+)"', hm.group(2))
        if t and us:
            items.append((t, us))
    if not items:
        for li in re.finditer(r"<li>(.*?)</li>", ce_text or "", re.S):
            chunk = li.group(1)
            us = re.findall(r'href="([^"]+)"', chunk)
            t = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r'<a\b.*?</a>', "", chunk))).strip()
            t = re.sub(r"#\d+\s*$", "", t).strip()
            if t and us:
                items.append((t, us))
    return cover, items


def _render_juya(cover: str | None, items: list[tuple[str, list[str]]],
                 date: str | None, video_url: str | None) -> str:
    """排成跟橘鸦老 GitHub 早报一致的版式: 封面 + H1 日期 + 视频版 + 概览 + 带 ↗ 的条目列表。"""
    parts: list[str] = []
    if cover:
        parts.append(f'<p><img src="{html.escape(cover)}"/></p>')
    if date:
        parts.append(f"<h1>AI 早报 {html.escape(date)}</h1>")
    if video_url:
        parts.append(f'<p><strong>视频版</strong>：'
                     f'<a href="{html.escape(video_url)}">YouTube</a></p>')
    parts.append("<h2>概览</h2>")
    lis = []
    for i, (t, us) in enumerate(items, 1):
        arrows = " ".join(f'<a href="{html.escape(u)}">↗</a>' for u in us)
        lis.append(f"<li>{html.escape(t)} {arrows} <code>#{i}</code></li>")
    parts.append("<ul>\n" + "\n".join(lis) + "\n</ul>")
    return "\n".join(parts)


def _strip_inline_styles(it: ET.Element) -> bool:
    """剥掉 content:encoded 里所有内联 style 属性,返回是否改过。
    橘鸦 2026-06-19 起改版,把正文套进 <div style="...color:#18181b;max-width:760px">,
    硬编码近黑字色 → 深色模式阅读器黑字黑底看不见。剥掉 style 让阅读器主题接管,
    回到 6/18 及更早那种干净结构(结构/链接不动,仅去样式)。幂等。"""
    ce = it.find(_CONTENT_ENC)
    if ce is None or not ce.text or 'style="' not in ce.text:
        return False
    ce.text = re.sub(r'\s*style="[^"]*"', "", ce.text)
    return True


def juya_format_item(it: ET.Element) -> None:
    """规整一条橘鸦 item 的 <content:encoded>。两条来源分别处理,幂等可重跑:
    - daily.juya.uk 新富文本(2026-06-19 改版,带死样式 div)→ 剥内联 style。
    - 早期镜像 YouTube(guid=watch URL)→ 重排成老 GitHub 早报版式。
    - 橘鸦旧 GitHub 富文本(imjuya, 无 style)→ 不动。"""
    guid = it.findtext("guid") or ""
    if "youtube.com/watch" not in guid:
        _strip_inline_styles(it)  # daily.juya.uk 新版去样式;旧 GitHub 无 style 时 no-op
        return  # 非 youtube 来源 → 仅去样式,不重排
    title_txt = it.findtext("title") or ""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", title_txt)   # 从标题【AI 早报 2026-06-12】抽日期
    date = m.group(1) if m else None
    cover, items = _parse_juya_chapters(it.findtext("description") or "")
    if not items:                                      # description 已是摘要 → 从旧 content 反解重排
        ce = it.find(_CONTENT_ENC)
        if ce is not None and ce.text:
            cover, items = _parse_prev_formatted(ce.text)
    if not items:
        return
    body = _render_juya(cover, items, date, guid)
    ce = it.find(_CONTENT_ENC)
    if ce is None:
        ce = ET.SubElement(it, _CONTENT_ENC)
    ce.text = body
    summary = "｜".join(t for t, _ in items)
    desc = it.find("description")
    if desc is None:
        desc = ET.SubElement(it, "description")
    desc.text = summary


# --- YouTube 原生 Atom feed → RSS 2.0 ----------------------------------------
# RSSHub 的 youtube 路由在数据中心 IP 上整体 502(它退回抓 youtube.com 原生 feed,
# 而 YouTube 对数据中心 IP 回 404)。但 GitHub Actions 的 Azure IP 没被封,能直接
# 200 拿到原生 feed——所以这里跳过 RSSHub,直抓原生 Atom feed 自己转成 RSS。
# guid 用视频 watch URL,与历史里 RSSHub 时期写入的条目 guid 一致,合并去重无缝。
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_MRSS_NS = "{http://search.yahoo.com/mrss/}"
_YT_NS = "{http://www.youtube.com/xml/schemas/2015}"


def youtube_atom_to_rss(atom_bytes: bytes) -> bytes:
    """YouTube 原生 Atom feed → accumulate 能吃的 RSS 2.0 bytes。
    每条 <entry> → <item>;description = 缩略图<img> + media:description 原文。"""
    feed = ET.fromstring(atom_bytes)
    ftitle = feed.findtext(f"{_ATOM_NS}title") or "YouTube"
    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = ftitle
    ET.SubElement(ch, "link").text = "https://www.youtube.com/"
    ET.SubElement(ch, "description").text = ftitle
    for entry in feed.findall(f"{_ATOM_NS}entry"):
        href = ""
        for ln in entry.findall(f"{_ATOM_NS}link"):
            if ln.get("rel") == "alternate" and ln.get("href"):
                href = ln.get("href")
                break
        if not href:
            vid = entry.findtext(f"{_YT_NS}videoId") or ""
            href = f"https://www.youtube.com/watch?v={vid}" if vid else ""
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = entry.findtext(f"{_ATOM_NS}title") or ""
        ET.SubElement(it, "link").text = href
        g = ET.SubElement(it, "guid")
        g.text = href
        g.set("isPermaLink", "true")
        pub = entry.findtext(f"{_ATOM_NS}published") or ""
        try:
            ET.SubElement(it, "pubDate").text = format_datetime(datetime.fromisoformat(pub))
        except Exception:
            pass
        grp = entry.find(f"{_MRSS_NS}group")
        desc = (grp.findtext(f"{_MRSS_NS}description") if grp is not None else "") or ""
        thumb = ""
        if grp is not None:
            th = grp.find(f"{_MRSS_NS}thumbnail")
            if th is not None:
                thumb = th.get("url") or ""
        body = (f'<img src="{thumb}"/>\n' if thumb else "") + desc
        ET.SubElement(it, "description").text = body
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="utf-8")


def merge(source_bytes: bytes, existing_path: Path, limit: int = MAX_ITEMS,
          item_transform=None, drop_pred=None) -> bytes:
    """Parse source RSS 2.0; merge its items with existing file (if any).
    drop_pred(item)->bool: 命中的合并后条目会被剔除(用于清掉历史遗留来源)。"""
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

    if drop_pred:
        new_items = [it for it in new_items if not drop_pred(it)]

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
    # YouTube 原生 feed 是 Atom,先转成 RSS 2.0 再走统一的 merge 管线。
    if "youtube.com/feeds/videos.xml" in url:
        try:
            source = youtube_atom_to_rss(source)
        except Exception as e:
            log.error(f"{name}: youtube atom→rss failed: {e}")
            return False
    transform = juya_format_item if name == "juya-ai-daily" else None
    # juya 已换回橘鸦官方富文本 RSS;清掉历史里早期镜像 YouTube 时留下的条目
    # (guid=watch URL),免得跟官方版同一天的早报重复。
    drop_pred = (lambda it: "youtube.com/watch" in (item_guid(it) or "")) \
        if name == "juya-ai-daily" else None
    try:
        merged = merge(source, FEEDS_DIR / f"{name}.xml",
                       item_transform=transform, drop_pred=drop_pred)
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
