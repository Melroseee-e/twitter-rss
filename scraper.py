#!/usr/bin/env python3
"""
Twitter/X user timeline → RSS feed via xcancel.com.

Usage:
    python3 scraper.py                      # run for all users in users.txt
    python3 scraper.py --user elonmusk      # run for single user
    python3 scraper.py --user elonmusk --verbose

Output: feeds/<user>.xml (RSS 2.0 with media: namespace)
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
FEEDS_DIR = ROOT / "feeds"
STATE_DIR = ROOT / "state"
USERS_FILE = ROOT / "users.txt"

XCANCEL_BASES = [
    "https://xcancel.com",
    "https://nitter.tiekoetter.com",
]
JINA_PREFIX = "https://r.jina.ai/"

UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

log = logging.getLogger("twrss")


@dataclass
class Media:
    kind: str  # "image" | "video"
    url: str
    poster: Optional[str] = None


@dataclass
class Entry:
    id: str                        # numeric tweet id (string)
    url: str                       # canonical x.com URL
    author_handle: str             # without @
    author_name: str
    text: str                      # plain text
    html: str                      # rendered HTML for <description>
    published: datetime            # UTC
    is_retweet: bool = False
    retweeter: Optional[str] = None  # profile user when is_retweet
    media: list[Media] = field(default_factory=list)
    quoted: Optional["Entry"] = None


def _ua() -> str:
    return random.choice(UAS)


def fetch_xcancel_html(user: str, client: httpx.Client) -> Optional[str]:
    for base in XCANCEL_BASES:
        url = f"{base}/{user}"
        try:
            r = client.get(url, headers={"User-Agent": _ua()}, timeout=25, follow_redirects=True)
            if r.status_code == 200 and 'class="timeline-item' in r.text:
                log.info(f"[xcancel] {user}: OK ({len(r.text)} bytes from {r.url})")
                return r.text
            log.warning(f"[xcancel] {user}: HTTP {r.status_code} from {url}")
        except Exception as e:
            log.warning(f"[xcancel] {user}: {e}")
    return None


def fetch_via_jina(user: str, client: httpx.Client) -> Optional[str]:
    """Fallback: ask Jina Reader to render xcancel as markdown."""
    url = f"{JINA_PREFIX}https://xcancel.com/{user}"
    try:
        r = client.get(url, headers={"User-Agent": _ua(), "Accept": "text/plain"}, timeout=60)
        if r.status_code == 200 and len(r.text) > 500:
            log.info(f"[jina] {user}: OK ({len(r.text)} bytes)")
            return r.text
        log.warning(f"[jina] {user}: HTTP {r.status_code}")
    except Exception as e:
        log.warning(f"[jina] {user}: {e}")
    return None


_DATE_RE = re.compile(r"([A-Za-z]{3}) (\d{1,2}), (\d{4}) · (\d{1,2}):(\d{2}) ([AP]M) UTC")
_MONTHS = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}


def _parse_date(title: str) -> Optional[datetime]:
    m = _DATE_RE.match(title.strip())
    if not m:
        return None
    mon, d, y, hh, mm, ampm = m.groups()
    h = int(hh) % 12 + (12 if ampm == "PM" else 0)
    return datetime(int(y), _MONTHS[mon], int(d), h, int(mm), tzinfo=timezone.utc)


def _clean_xcancel_url(u: str) -> str:
    """Strip xcancel proxy prefixes; return direct pbs.twimg / video.twimg URL when possible."""
    if not u:
        return u
    # Nitter-style "/pic/orig/..." or "/video/.../video.twimg.com/..."
    for prefix in ("/pic/orig/", "/pic/"):
        if u.startswith(prefix):
            rest = u[len(prefix):]
            # URL-decoded twimg path
            import urllib.parse
            return "https://" + urllib.parse.unquote(rest)
    m = re.search(r"/video/[^/]+/(video\.twimg\.com/.+)$", u)
    if m:
        return "https://" + m.group(1)
    # already absolute
    if u.startswith("http"):
        return u
    return u


def _absolutize(base: str, u: str) -> str:
    if u.startswith("http"):
        return u
    if u.startswith("/"):
        return base + u
    return u


def parse_xcancel_html(html: str, profile_user: str) -> list[Entry]:
    soup = BeautifulSoup(html, "html.parser")
    entries: list[Entry] = []
    for item in soup.select("div.timeline-item"):
        # skip "show more" etc
        tl = item.select_one("a.tweet-link")
        if not tl or not tl.get("href"):
            continue
        href = tl["href"]  # /screen_name/status/ID#m
        m = re.match(r"^/([^/]+)/status/(\d+)", href)
        if not m:
            continue
        author_handle, tid = m.group(1), m.group(2)

        is_rt = item.select_one("div.retweet-header") is not None
        fullname_el = item.select_one("a.fullname")
        author_name = fullname_el.get_text(strip=True) if fullname_el else author_handle

        date_el = item.select_one("span.tweet-date a")
        pub = _parse_date(date_el.get("title", "")) if date_el else None
        if not pub:
            pub = datetime.now(timezone.utc)

        content_el = item.select_one("div.tweet-content")
        text = content_el.get_text("\n", strip=True) if content_el else ""

        media: list[Media] = []
        # images
        for img in item.select("div.attachments a.still-image img, div.attachments img"):
            src = img.get("src") or ""
            # prefer orig via parent <a href>
            parent = img.find_parent("a")
            if parent and parent.get("href"):
                src = parent["href"]
            src = _clean_xcancel_url(src)
            if src and "twimg.com" in src:
                # upgrade ?name=small to ?name=orig
                src = re.sub(r"([?&])name=[^&]+", r"\1name=orig", src)
                if "name=" not in src and "?" not in src:
                    src += "?name=orig"
                media.append(Media("image", src))
        # videos
        for vid in item.select("video"):
            poster = _clean_xcancel_url(vid.get("poster") or "")
            src_el = vid.select_one("source")
            if src_el and src_el.get("src"):
                vurl = _clean_xcancel_url(src_el["src"])
                media.append(Media("video", vurl, poster=poster))
            elif poster:
                # gif fallback
                media.append(Media("image", poster))

        # quoted tweet (best-effort: pull text + link)
        quoted: Optional[Entry] = None
        q = item.select_one("div.quote")
        if q:
            qlink = q.select_one("a.quote-link")
            qtext_el = q.select_one("div.quote-text")
            quser_el = q.select_one("a.username")
            qfull_el = q.select_one("a.fullname")
            qhref = qlink.get("href") if qlink else None
            qm = re.match(r"^/([^/]+)/status/(\d+)", qhref) if qhref else None
            if qm:
                qmedia: list[Media] = []
                for img in q.select("img"):
                    src = img.get("src") or ""
                    src = _clean_xcancel_url(src)
                    if "twimg.com" in src:
                        qmedia.append(Media("image", src))
                for vid in q.select("video"):
                    poster = _clean_xcancel_url(vid.get("poster") or "")
                    src_el = vid.select_one("source")
                    vurl = _clean_xcancel_url(src_el["src"]) if src_el and src_el.get("src") else ""
                    if vurl:
                        qmedia.append(Media("video", vurl, poster=poster))
                quoted = Entry(
                    id=qm.group(2),
                    url=f"https://x.com/{qm.group(1)}/status/{qm.group(2)}",
                    author_handle=qm.group(1),
                    author_name=qfull_el.get_text(strip=True) if qfull_el else qm.group(1),
                    text=qtext_el.get_text("\n", strip=True) if qtext_el else "",
                    html="",
                    published=datetime.now(timezone.utc),
                    media=qmedia,
                )

        e = Entry(
            id=tid,
            url=f"https://x.com/{author_handle}/status/{tid}",
            author_handle=author_handle,
            author_name=author_name,
            text=text,
            html="",
            published=pub,
            is_retweet=is_rt,
            retweeter=profile_user if is_rt else None,
            media=media,
            quoted=quoted,
        )
        e.html = render_html(e)
        entries.append(e)
    return entries


def render_html(e: Entry) -> str:
    parts: list[str] = []
    if e.is_retweet:
        parts.append(f'<p><em>🔁 @{e.retweeter} retweeted</em></p>')
    parts.append(f'<p><strong>{escape(e.author_name)}</strong> (<a href="https://x.com/{e.author_handle}">@{e.author_handle}</a>)</p>')
    if e.text:
        # convert \n to <br>
        safe = escape(e.text).replace("\n", "<br>")
        # autolink bare https URLs
        safe = re.sub(r'(https?://[^\s<]+)', r'<a href="\1">\1</a>', safe)
        parts.append(f'<p>{safe}</p>')
    for m in e.media:
        if m.kind == "image":
            parts.append(f'<p><img src="{escape(m.url)}" style="max-width:100%"/></p>')
        elif m.kind == "video":
            poster_attr = f' poster="{escape(m.poster)}"' if m.poster else ""
            parts.append(
                f'<p><video controls{poster_attr} style="max-width:100%">'
                f'<source src="{escape(m.url)}" type="video/mp4"/>'
                f'Your reader does not support video. <a href="{escape(m.url)}">Download</a>'
                f'</video></p>'
            )
    if e.quoted:
        q = e.quoted
        qparts = [f'<blockquote><p><strong>{escape(q.author_name)}</strong> '
                  f'(<a href="https://x.com/{q.author_handle}">@{q.author_handle}</a>):</p>']
        if q.text:
            qparts.append(f'<p>{escape(q.text).replace(chr(10), "<br>")}</p>')
        for m in q.media:
            if m.kind == "image":
                qparts.append(f'<p><img src="{escape(m.url)}" style="max-width:100%"/></p>')
            elif m.kind == "video":
                qparts.append(f'<p><a href="{escape(m.url)}">[video]</a></p>')
        qparts.append(f'<p><a href="{q.url}">View quoted tweet →</a></p></blockquote>')
        parts.append("".join(qparts))
    parts.append(f'<p><a href="{e.url}">View on X →</a></p>')
    return "\n".join(parts)


def build_rss(user: str, entries: list[Entry]) -> bytes:
    ns = {"media": "http://search.yahoo.com/mrss/", "atom": "http://www.w3.org/2005/Atom"}
    ET.register_namespace("media", ns["media"])
    ET.register_namespace("atom", ns["atom"])
    rss = ET.Element("rss", attrib={"version": "2.0"})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = f"@{user} on X (via xcancel)"
    ET.SubElement(ch, "link").text = f"https://x.com/{user}"
    ET.SubElement(ch, "description").text = f"Posts, retweets and quotes from @{user}"
    ET.SubElement(ch, "language").text = "en"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    for e in entries:
        it = ET.SubElement(ch, "item")
        title = e.text.strip().replace("\n", " ")[:120] or f"Tweet {e.id}"
        if e.is_retweet:
            title = f"🔁 RT @{e.author_handle}: {title}"
        ET.SubElement(it, "title").text = title
        ET.SubElement(it, "link").text = e.url
        ET.SubElement(it, "guid", isPermaLink="true").text = e.url
        ET.SubElement(it, "pubDate").text = format_datetime(e.published)
        ET.SubElement(it, "author").text = f"{e.author_handle}@x.com ({e.author_name})"
        ET.SubElement(it, "description").text = e.html
        for m in e.media:
            attrs = {"url": m.url, "medium": "image" if m.kind == "image" else "video"}
            if m.kind == "video":
                attrs["type"] = "video/mp4"
            ET.SubElement(it, f"{{{ns['media']}}}content", attrib=attrs)
            if m.kind == "video" and m.poster:
                ET.SubElement(it, f"{{{ns['media']}}}thumbnail", attrib={"url": m.poster})
    # pretty print
    ET.indent(rss, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="utf-8")


def load_state(user: str) -> dict:
    p = STATE_DIR / f"{user}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"seen_ids": []}


def save_state(user: str, state: dict) -> None:
    (STATE_DIR / f"{user}.json").write_text(json.dumps(state, indent=2))


def process_user(user: str, client: httpx.Client) -> bool:
    html = fetch_xcancel_html(user, client)
    if html:
        entries = parse_xcancel_html(html, user)
    else:
        md = fetch_via_jina(user, client)
        if not md:
            log.error(f"{user}: all sources failed")
            return False
        entries = parse_jina_markdown(md, user)
    if not entries:
        log.warning(f"{user}: 0 entries parsed")
        return False

    # merge with previously seen entries? simplest: just overwrite with latest 50
    # Folo dedupes by guid so we can emit fresh each time.
    entries.sort(key=lambda e: e.published, reverse=True)
    xml = build_rss(user, entries[:50])
    out = FEEDS_DIR / f"{user}.xml"
    out.write_bytes(xml)
    log.info(f"{user}: wrote {len(entries)} entries -> {out}")

    st = load_state(user)
    ids = [e.id for e in entries]
    new_ids = [i for i in ids if i not in st["seen_ids"]]
    st["seen_ids"] = (new_ids + st["seen_ids"])[:500]
    save_state(user, st)
    if new_ids:
        log.info(f"{user}: {len(new_ids)} new tweets")
    return True


# --- Jina fallback parser (best-effort) -----------------------------------

def parse_jina_markdown(md: str, profile_user: str) -> list[Entry]:
    """Parse r.jina.ai rendered xcancel markdown.

    Heuristic: split by handle line pattern like "Name@handle\n\n<time>". Best-effort only.
    """
    entries: list[Entry] = []
    # Extremely simple heuristic: find links of form /user/status/ID in the markdown.
    # xcancel markdown typically contains [time ago](/user/status/ID#m)
    id_link_re = re.compile(r"\((/([A-Za-z0-9_]{1,15})/status/(\d+)[^)]*)\)")
    seen = set()
    lines = md.splitlines()
    for i, ln in enumerate(lines):
        for m in id_link_re.finditer(ln):
            tid = m.group(3)
            if tid in seen:
                continue
            seen.add(tid)
            handle = m.group(2)
            # grab a few lines around as text
            ctx_start = max(0, i - 8)
            text_lines = [l for l in lines[ctx_start:i] if l.strip() and not l.startswith("#")]
            text = "\n".join(text_lines[-6:])
            # media: scan near-context for twimg URLs
            media: list[Media] = []
            ctx = "\n".join(lines[max(0, i - 10):i + 5])
            for murl in re.findall(r"https://pbs\.twimg\.com/[^\s)\"]+", ctx):
                media.append(Media("image", murl))
            for vurl in re.findall(r"https://video\.twimg\.com/[^\s)\"]+\.mp4", ctx):
                media.append(Media("video", vurl))

            is_rt = handle.lower() != profile_user.lower()
            e = Entry(
                id=tid,
                url=f"https://x.com/{handle}/status/{tid}",
                author_handle=handle,
                author_name=handle,
                text=text[:800],
                html="",
                published=datetime.now(timezone.utc),  # Jina md loses precise timestamps
                is_retweet=is_rt,
                retweeter=profile_user if is_rt else None,
                media=media,
            )
            e.html = render_html(e)
            entries.append(e)
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="single handle (overrides users.txt)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.user:
        users = [args.user]
    else:
        if not USERS_FILE.exists():
            log.error(f"{USERS_FILE} missing. Create it (one handle per line).")
            sys.exit(2)
        users = [ln.strip().lstrip("@") for ln in USERS_FILE.read_text().splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]

    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(http2=True) as client:
        ok = 0
        for u in users:
            if process_user(u, client):
                ok += 1
            time.sleep(random.uniform(1.0, 3.0))
    log.info(f"done: {ok}/{len(users)} users ok")


if __name__ == "__main__":
    main()
