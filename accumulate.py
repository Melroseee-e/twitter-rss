#!/usr/bin/env python3
"""Mirror + accumulate external RSS feeds.

For each entry in `accumulate.txt`, fetch the source RSS, merge its <item>s
with the locally-stored feed file (hydrated beforehand from Pages), dedupe by
GUID, sort by pubDate desc, keep top N. Write back to feeds/<name>.xml.

Handles RSS 2.0. Atom is not handled here (none of our sources are Atom).
"""
from __future__ import annotations

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

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4"
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
        r = client.get(url, headers={"User-Agent": UA}, timeout=60)
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


def merge(source_bytes: bytes, existing_path: Path, limit: int = MAX_ITEMS) -> bytes:
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
    try:
        merged = merge(source, FEEDS_DIR / f"{name}.xml")
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
    return 0 if ok == len(entries) else 1


if __name__ == "__main__":
    sys.exit(main())
