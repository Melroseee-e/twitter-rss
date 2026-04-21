# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Free Twitter/X timeline → RSS + JSON Feed, hosted on GitHub Pages. No server,
no Twitter API, no login. Built for consumption in [Folo](https://app.folo.is)
and any other reader.

- **Live**: https://melroseee-e.github.io/twitter-rss/
- **Repo**: https://github.com/Melroseee-e/twitter-rss
- **Cron**: GitHub Actions every 15 min (`.github/workflows/scrape.yml`)

## Common commands

```bash
# Local dev (writes feeds/<user>.xml + .json)
pip install -r requirements.txt
python3 scraper.py --user elonmusk -v       # single user, verbose
python3 scraper.py                          # all users in users.txt

# Validate output
xmllint --noout feeds/elonmusk.xml
python3 -m json.tool feeds/elonmusk.json >/dev/null

# Add a new account: edit users.txt (one handle per line, no @), git push.
# Workflow runs on every push to scraper.py / users.txt / workflow file.

# Trigger workflow manually
gh workflow run scrape.yml

# Watch latest run
gh run watch $(gh run list --limit 1 --json databaseId --jq '.[0].databaseId')
```

## Architecture (one file: `scraper.py`)

Pipeline per user (~5–30 s, runs sequentially with 1–3 s jitter between users):

```
fetch_xcancel_html(user)            # HTML scrape, NOT xcancel /rss (whitelist-gated)
  ├─ try https://xcancel.com/<user>
  ├─ try https://nitter.tiekoetter.com/<user>
  └─ each may return 503 + Anubis POW challenge
       → _anubis_pass() solves SHA-256 PoW (~30 ms at difficulty 4),
         submits /api/pass-challenge, cookie persists in httpx.Client
         so subsequent users in the same run don't re-pay the cost
  fallback: fetch_via_jina(user)    # r.jina.ai/https://xcancel.com/<user>
                                    # parses markdown via parse_jina_markdown()

parse_profile_meta(html, user)      # → ProfileMeta(handle, full_name, bio, avatar, site_url)
parse_xcancel_html(html, user)      # → list[Entry] (BS4 over div.timeline-item)
   ├─ extracts: id, url, author_handle/name/avatar, text, published (UTC),
   │            is_retweet, retweeter, media[], quoted (recursive Entry)
   ├─ media URL cleaning: strip nitter /pic/* and /video/* proxy prefixes
   │   so feed contains direct pbs.twimg.com / video.twimg.com URLs
   └─ image quality upgrade: ?name=small → ?name=orig

build_rss(profile, entries)         # Folo-flavored RSS 2.0
  Channel: <title>, <description>=bio, <ttl>15, <image><url> = avatar
  Item:    <dc:creator>, <content:encoded> rich HTML, <description> plain summary,
           <category> from #tags + @mentions, <enclosure> first media

build_json_feed(profile, entries)   # JSON Feed 1.1 — has per-item authorAvatar
                                    # which RSS cannot represent (matters for retweets)
```

Outputs `feeds/<user>.xml` and `feeds/<user>.json`. Workflow stages them into
`_site/`, generates an index.html, then `actions/deploy-pages` publishes.

## Key design decisions (don't undo without reason)

- **HTML scrape, not xcancel `/rss`** — the RSS endpoint requires per-reader
  email whitelisting. The HTML page works without auth.
- **Anubis solver is native Python** — `_solve_anubis()` is a direct port of
  Anubis `web/js/worker/sha256-webcrypto.ts` fast algo. Don't replace with a
  headless browser; native is 100× faster and runs on Actions in seconds.
- **`media:` namespace was removed** — Folo's RSS parser doesn't read MRSS.
  Media is delivered via inline `<img>`/`<video>` in `<content:encoded>` plus
  the first item as `<enclosure>`.
- **JSON Feed exists alongside RSS** — only JSON Feed lets each entry carry a
  different `authorAvatar` (essential for retweets where the surfaced author
  is not the profile owner).
- **Folo caches feed metadata by URL** — once a URL is registered, Folo never
  re-reads `<title>`/`<image>`/`<description>`. To refresh, append a query
  string (`?v=2`) to force registration as a "new" feed.

## Adding a new user

1. `echo handle >> users.txt` (no `@`, case matters)
2. `git add users.txt && git commit -m 'add @handle' && git push`
3. Wait ~1 min for the workflow; subscribe `https://melroseee-e.github.io/twitter-rss/<handle>.xml`
