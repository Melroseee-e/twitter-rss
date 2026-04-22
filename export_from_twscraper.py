#!/usr/bin/env python3
"""Bridge: read twscraper's SQLite DB and write Folo-flavored RSS + JSON Feed
into this project's feeds/ directory.

Reuses scraper.py's Entry / ProfileMeta / build_rss / build_json_feed /
render_html / merge helpers so the output schema stays identical — a reader
subscribed to the same URL doesn't care where the items came from.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse all serializers from the local scraper
sys.path.insert(0, str(Path(__file__).parent))
from scraper import (
    Entry,
    Media,
    ProfileMeta,
    _build_merged_json,
    _build_merged_rss,
    _existing_json_items,
    _existing_xml_items,
    _extract_categories,
    _hq_avatar,
    render_html,
)

DEFAULT_DB = Path("/Users/melrose/Headquarter/Intel/twitter_scraper/data/tweets.db")
FEEDS_DIR = Path(__file__).parent / "feeds"
# Very high cap — twscraper can yield 1000+ per user and we want everything
# in the feed so re-subscribing always picks up full history.
DEEP_LIMIT = 5000

log = logging.getLogger("export_twscraper")


def _row_to_entry(r: sqlite3.Row, quoted_map: dict) -> Entry:
    media = []
    for m in json.loads(r["media_json"] or "[]"):
        media.append(Media(
            kind=m.get("kind") or m.get("type") or "image",
            url=m["url"],
            poster=m.get("poster"),
        ))

    quoted = None
    qid = r["quoted_id"]
    if qid and quoted_map.get(qid):
        qr = quoted_map[qid]
        qmedia = [
            Media(
                kind=m.get("kind") or m.get("type") or "image",
                url=m["url"],
                poster=m.get("poster"),
            )
            for m in json.loads(qr["media_json"] or "[]")
        ]
        quoted = Entry(
            id=qr["id"],
            url=f"https://x.com/{qr['author_handle']}/status/{qr['id']}",
            author_handle=qr["author_handle"],
            author_name=qr["author_name"] or qr["author_handle"],
            text=qr["text"] or "",
            html="",
            published=datetime.fromtimestamp(qr["created_at"] or 0, timezone.utc),
            author_avatar=_hq_avatar(qr["author_avatar"] or ""),
            media=qmedia,
        )

    text = r["text"] or ""
    e = Entry(
        id=r["id"],
        url=f"https://x.com/{r['author_handle']}/status/{r['id']}",
        author_handle=r["author_handle"],
        author_name=r["author_name"] or r["author_handle"],
        text=text,
        html="",
        published=datetime.fromtimestamp(r["created_at"] or 0, timezone.utc),
        is_retweet=bool(r["is_retweet"]),
        retweeter=r["retweeter"],
        author_avatar=_hq_avatar(r["author_avatar"] or ""),
        media=media,
        quoted=quoted,
        categories=_extract_categories(text + " " + (quoted.text if quoted else "")),
    )
    e.html = render_html(e)
    return e


def load_entries(conn: sqlite3.Connection, handle: str, limit: int = DEEP_LIMIT) -> list[Entry]:
    h = handle.lower()
    rows = conn.execute(
        """SELECT * FROM tweets
           WHERE author_handle = ? OR retweeter = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (h, h, limit),
    ).fetchall()
    # Prefetch any quoted tweets referenced
    quoted_ids = {r["quoted_id"] for r in rows if r["quoted_id"]}
    quoted_map: dict = {}
    if quoted_ids:
        placeholders = ",".join("?" * len(quoted_ids))
        for qr in conn.execute(
            f"SELECT * FROM tweets WHERE id IN ({placeholders})",
            tuple(quoted_ids),
        ).fetchall():
            quoted_map[qr["id"]] = qr
    return [_row_to_entry(r, quoted_map) for r in rows]


def load_profile(conn: sqlite3.Connection, handle: str) -> ProfileMeta:
    r = conn.execute(
        "SELECT * FROM users WHERE handle = ?", (handle.lower(),)
    ).fetchone()
    if r:
        return ProfileMeta(
            handle=handle,
            full_name=r["display_name"] or handle,
            bio=r["bio"] or f"@{handle} on X",
            avatar=_hq_avatar(r["avatar_url"] or ""),
            site_url=f"https://x.com/{handle}",
        )
    return ProfileMeta(
        handle=handle, full_name=handle, bio=f"@{handle} on X",
        avatar="", site_url=f"https://x.com/{handle}",
    )


def export_one(handle: str, conn: sqlite3.Connection) -> bool:
    entries = load_entries(conn, handle)
    if not entries:
        log.warning(f"{handle}: 0 entries in DB — skip")
        return False
    profile = load_profile(conn, handle)

    # Merge with whatever's already published so nothing the Nitter-based
    # scraper produced gets lost (the two sources are complementary).
    old_xml = _existing_xml_items(handle)
    old_json = _existing_json_items(handle)

    xml = _build_merged_rss(profile, entries, old_xml, limit=DEEP_LIMIT)
    (FEEDS_DIR / f"{handle}.xml").write_bytes(xml)
    js = _build_merged_json(profile, entries, old_json, limit=DEEP_LIMIT)
    (FEEDS_DIR / f"{handle}.json").write_bytes(js)

    oldest = min(e.published for e in entries).date()
    newest = max(e.published for e in entries).date()
    log.info(f"{handle}: wrote {len(entries)} DB entries, {oldest} → {newest}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("handles", nargs="*", default=["elonmusk", "karpathy", "shao__meng", "Morris_LT"])
    ap.add_argument("--db", default=str(DEFAULT_DB), help="twscraper SQLite DB path")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ok = 0
    for h in args.handles:
        if export_one(h, conn):
            ok += 1
    log.info(f"done: {ok}/{len(args.handles)} handles ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
