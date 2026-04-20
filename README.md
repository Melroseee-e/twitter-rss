# twitter-rss

Free Twitter/X → RSS via `xcancel.com` HTML scraping (Jina Reader fallback),
deployed on GitHub Actions + Pages.

## How it works

1. GitHub Actions runs `scraper.py` every ~15 min (cron `*/15 * * * *`).
2. The scraper fetches each handle in `users.txt` from xcancel.com, parses
   tweets (text, images, videos, quotes, retweets), and writes an RSS 2.0 XML
   with `media:` namespace to `feeds/<handle>.xml`.
3. The workflow uploads `feeds/` as a Pages artifact; Pages serves it at
   `https://<user>.github.io/<repo>/<handle>.xml`.

## Add / remove subscriptions

Edit `users.txt` (one handle per line, no `@`), commit & push — the workflow
runs automatically on push.

## Local dev

```bash
pip install -r requirements.txt
python3 scraper.py --user elonmusk -v
xmllint --noout feeds/elonmusk.xml
```

## Notes

- Data source: **xcancel.com** (Nitter fork maintained by `unixfox/nitter-fork`).
- Fallback: Jina Reader (`r.jina.ai`) parses the same xcancel page as markdown.
- Retweets, quoted tweets, images and direct video (`video.twimg.com`) links
  are preserved in the RSS output.
