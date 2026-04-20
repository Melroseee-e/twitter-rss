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
    html: str                      # rendered HTML for <content:encoded>
    published: datetime            # UTC
    is_retweet: bool = False
    retweeter: Optional[str] = None   # profile user when is_retweet
    author_avatar: Optional[str] = None  # direct pbs.twimg avatar URL
    media: list[Media] = field(default_factory=list)
    quoted: Optional["Entry"] = None
    categories: list[str] = field(default_factory=list)  # hashtags + @mentions


@dataclass
class ProfileMeta:
    handle: str
    full_name: str
    bio: str
    avatar: str  # large avatar URL (pbs.twimg orig)
    site_url: str  # x.com profile URL


def _ua() -> str:
    return random.choice(UAS)


_ANUBIS_RE = re.compile(
    r'<script id="anubis_challenge" type="application/json">(.+?)</script>', re.S
)
_ANUBIS_BASEPREFIX_RE = re.compile(
    r'<script id="anubis_base_prefix" type="application/json">(.+?)</script>', re.S
)


def _solve_anubis(challenge: dict) -> tuple[str, int]:
    """Find nonce s.t. SHA-256(randomData + nonce) has the required leading zero bits.

    Spec (Anubis fast algo, v1.25): bytes-level check —
      requiredZeroBytes = difficulty // 2 must all be 0; if difficulty is odd,
      next byte's high nibble must be 0.
    """
    import hashlib
    data: str = challenge["randomData"]
    diff: int = int(challenge.get("difficulty") or challenge.get("rules", {}).get("difficulty") or 4)
    req = diff // 2
    odd = diff % 2 != 0
    base = data.encode()
    nonce = 0
    while True:
        h = hashlib.sha256(base + str(nonce).encode()).digest()
        ok = all(h[i] == 0 for i in range(req)) and (not odd or h[req] >> 4 == 0)
        if ok:
            return h.hex(), nonce
        nonce += 1


def _anubis_pass(client: httpx.Client, base: str, html: str, original_url: str, ua: str) -> bool:
    """Solve Anubis challenge and submit; on success the client cookies are set."""
    m = _ANUBIS_RE.search(html)
    if not m:
        return False
    try:
        wrapper = json.loads(m.group(1))
    except Exception:
        return False
    rules = wrapper.get("rules") or {}
    challenge = wrapper.get("challenge") or {}
    if "difficulty" not in challenge:
        challenge["difficulty"] = rules.get("difficulty", 4)
    bp_m = _ANUBIS_BASEPREFIX_RE.search(html)
    base_prefix = ""
    if bp_m:
        try:
            base_prefix = json.loads(bp_m.group(1)) or ""
        except Exception:
            pass

    t0 = time.time()
    try:
        h, nonce = _solve_anubis(challenge)
    except Exception as e:
        log.warning(f"[anubis] solve failed: {e}")
        return False
    elapsed_ms = int((time.time() - t0) * 1000)
    log.info(f"[anubis] solved diff={challenge['difficulty']} in {elapsed_ms}ms (nonce={nonce})")

    pass_url = f"{base}{base_prefix}/.within.website/x/cmd/anubis/api/pass-challenge"
    params = {
        "id": challenge["id"],
        "response": h,
        "nonce": str(nonce),
        "redir": original_url,
        "elapsedTime": str(elapsed_ms),
    }
    try:
        r = client.get(pass_url, params=params, headers={"User-Agent": ua, "Referer": original_url},
                        timeout=20, follow_redirects=True)
        return r.status_code in (200, 302) and "Verifying your request" not in r.text and "Making sure you" not in r.text
    except Exception as e:
        log.warning(f"[anubis] pass-challenge failed: {e}")
        return False


def fetch_xcancel_html(user: str, client: httpx.Client) -> Optional[str]:
    for base in XCANCEL_BASES:
        url = f"{base}/{user}"
        ua = _ua()
        try:
            r = client.get(url, headers={"User-Agent": ua}, timeout=25, follow_redirects=True)
            text = r.text
            # 1) Anubis challenge? solve and refetch
            if ("anubis_challenge" in text) or ("Verifying your request" in text) or ("Making sure you" in text):
                log.info(f"[xcancel] {user}: Anubis challenge from {base}, solving…")
                if _anubis_pass(client, base, text, url, ua):
                    r = client.get(url, headers={"User-Agent": ua}, timeout=25, follow_redirects=True)
                    text = r.text
            if r.status_code == 200 and 'class="timeline-item' in text:
                log.info(f"[xcancel] {user}: OK ({len(text)} bytes from {r.url})")
                return text
            log.warning(f"[xcancel] {user}: HTTP {r.status_code} no timeline in {len(text)}b from {url}")
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


def _hq_avatar(url: str) -> str:
    """Upgrade profile image to a large variant (Twitter serves _400x400; _bigger = 73px)."""
    if not url:
        return url
    # Normalize to 400x400 quality for RSS header
    return re.sub(r"_(normal|bigger|mini|x96|400x400)\.(jpg|jpeg|png|webp)", r"_400x400.\2", url)


def parse_profile_meta(html: str, profile_user: str) -> ProfileMeta:
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", attrs={"property": "og:image"})
    avatar = og["content"] if og and og.get("content") else ""
    fullname_el = soup.select_one("a.profile-card-fullname")
    full_name = fullname_el.get_text(strip=True) if fullname_el else profile_user
    bio_el = soup.select_one("div.profile-bio")
    bio = bio_el.get_text(" ", strip=True) if bio_el else ""
    return ProfileMeta(
        handle=profile_user,
        full_name=full_name,
        bio=bio or f"@{profile_user} on X",
        avatar=_hq_avatar(avatar),
        site_url=f"https://x.com/{profile_user}",
    )


def _extract_categories(text: str) -> list[str]:
    tags = re.findall(r"#(\w{1,50})", text)
    mentions = re.findall(r"@(\w{1,15})", text)
    out: list[str] = []
    seen = set()
    for t in tags + mentions:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out[:10]


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
        avatar_img = item.select_one("a.tweet-avatar img")
        author_avatar = _hq_avatar(avatar_img["src"]) if avatar_img and avatar_img.get("src") else None

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
            author_avatar=author_avatar,
            media=media,
            quoted=quoted,
            categories=_extract_categories(text + " " + (quoted.text if quoted else "")),
        )
        e.html = render_html(e)
        entries.append(e)
    return entries


def render_html(e: Entry) -> str:
    parts: list[str] = []
    if e.is_retweet:
        parts.append(f'<p><em>🔁 @{e.retweeter} retweeted</em></p>')
    # author row with avatar
    avatar_img = ""
    if e.author_avatar:
        avatar_img = (f'<img src="{escape(e.author_avatar)}" alt="" width="32" height="32" '
                      f'style="border-radius:50%;vertical-align:middle;margin-right:6px"/>')
    parts.append(
        f'<p>{avatar_img}<strong>{escape(e.author_name)}</strong> '
        f'(<a href="https://x.com/{e.author_handle}">@{e.author_handle}</a>)</p>'
    )
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


def _plain_summary(text: str, limit: int = 240) -> str:
    s = text.strip().replace("\n", " ")
    return s[: limit - 1] + "…" if len(s) > limit else s


def build_rss(profile: ProfileMeta, entries: list[Entry]) -> bytes:
    """Folo-flavored RSS 2.0: feed <image>, <content:encoded>, <dc:creator>, <category>, <ttl>."""
    NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"
    NS_DC = "http://purl.org/dc/elements/1.1/"
    NS_ATOM = "http://www.w3.org/2005/Atom"
    ET.register_namespace("content", NS_CONTENT)
    ET.register_namespace("dc", NS_DC)
    ET.register_namespace("atom", NS_ATOM)

    rss = ET.Element("rss", attrib={"version": "2.0"})
    ch = ET.SubElement(rss, "channel")
    title = f"{profile.full_name} (@{profile.handle}) on X"
    ET.SubElement(ch, "title").text = title
    ET.SubElement(ch, "link").text = profile.site_url
    ET.SubElement(ch, "description").text = profile.bio
    ET.SubElement(ch, "language").text = "en"
    ET.SubElement(ch, "ttl").text = "15"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    # feed-level avatar (Folo reads <image><url>)
    if profile.avatar:
        img_el = ET.SubElement(ch, "image")
        ET.SubElement(img_el, "url").text = profile.avatar
        ET.SubElement(img_el, "title").text = title
        ET.SubElement(img_el, "link").text = profile.site_url

    for e in entries:
        it = ET.SubElement(ch, "item")
        it_title = _plain_summary(e.text, 120) or f"Tweet {e.id}"
        if e.is_retweet:
            it_title = f"🔁 RT @{e.author_handle}: {it_title}"
        ET.SubElement(it, "title").text = it_title
        ET.SubElement(it, "link").text = e.url
        ET.SubElement(it, "guid", isPermaLink="true").text = e.url
        ET.SubElement(it, "pubDate").text = format_datetime(e.published)
        # Folo reads <dc:creator>
        ET.SubElement(it, f"{{{NS_DC}}}creator").text = f"{e.author_name} (@{e.author_handle})"
        # Plain text summary for <description>, rich HTML for <content:encoded>
        ET.SubElement(it, "description").text = _plain_summary(e.text, 280) or it_title
        ET.SubElement(it, f"{{{NS_CONTENT}}}encoded").text = e.html
        for cat in e.categories:
            ET.SubElement(it, "category").text = cat
        # First media as <enclosure> (Folo RSS parser only reads one enclosure)
        if e.media:
            m0 = e.media[0]
            mime = "video/mp4" if m0.kind == "video" else "image/jpeg"
            ET.SubElement(it, "enclosure", attrib={
                "url": m0.url, "type": mime, "length": "0",
            })
    ET.indent(rss, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="utf-8")


def build_json_feed(profile: ProfileMeta, entries: list[Entry]) -> bytes:
    """JSON Feed 1.1 — Folo reads per-item authors with avatar. Great for retweets."""
    items = []
    for e in entries:
        authors = [{
            "name": f"{e.author_name} (@{e.author_handle})",
            "url": f"https://x.com/{e.author_handle}",
        }]
        if e.author_avatar:
            authors[0]["avatar"] = e.author_avatar
        item = {
            "id": e.url,
            "url": e.url,
            "title": _plain_summary(e.text, 120) or f"Tweet {e.id}",
            "content_html": e.html,
            "summary": _plain_summary(e.text, 280),
            "date_published": e.published.isoformat().replace("+00:00", "Z"),
            "authors": authors,
            "tags": e.categories or None,
        }
        if e.is_retweet:
            item["title"] = f"🔁 RT @{e.author_handle}: {item['title']}"
        # media (Folo JSON Feed reads top-level `image` as photo)
        imgs = [m for m in e.media if m.kind == "image"]
        if imgs:
            item["image"] = imgs[0].url
        attachments = []
        for m in e.media:
            attachments.append({
                "url": m.url,
                "mime_type": "video/mp4" if m.kind == "video" else "image/jpeg",
            })
        if attachments:
            item["attachments"] = attachments
        items.append({k: v for k, v in item.items() if v is not None})
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": f"{profile.full_name} (@{profile.handle}) on X",
        "home_page_url": profile.site_url,
        "feed_url": f"https://melroseee-e.github.io/twitter-rss/{profile.handle}.json",
        "description": profile.bio,
        "icon": profile.avatar,
        "language": "en",
        "authors": [{
            "name": profile.full_name,
            "url": profile.site_url,
            "avatar": profile.avatar,
        }],
        "items": items,
    }
    return json.dumps(feed, ensure_ascii=False, indent=2).encode("utf-8")


def load_state(user: str) -> dict:
    p = STATE_DIR / f"{user}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"seen_ids": []}


def save_state(user: str, state: dict) -> None:
    (STATE_DIR / f"{user}.json").write_text(json.dumps(state, indent=2))


def process_user(user: str, client: httpx.Client) -> bool:
    html = fetch_xcancel_html(user, client)
    profile: Optional[ProfileMeta] = None
    if html:
        profile = parse_profile_meta(html, user)
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
    if profile is None:
        profile = ProfileMeta(
            handle=user, full_name=f"@{user}", bio=f"@{user} on X",
            avatar="", site_url=f"https://x.com/{user}",
        )

    # Folo dedupes by guid; emit latest 50 fresh each run.
    entries.sort(key=lambda e: e.published, reverse=True)
    latest = entries[:50]

    xml = build_rss(profile, latest)
    (FEEDS_DIR / f"{user}.xml").write_bytes(xml)
    js = build_json_feed(profile, latest)
    (FEEDS_DIR / f"{user}.json").write_bytes(js)
    log.info(f"{user}: wrote {len(latest)} entries (xml + json)")

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
