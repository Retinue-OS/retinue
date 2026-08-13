#!/usr/bin/env python3
"""Put one item into the news feed by hand — the door for non-RSS sources.

`news-fetch.py` covers feeds, which is most of it. But plenty of broadcast-style
inbound arrives on channels that are not feeds: a Telegram channel post, an
e-mail newsletter the Secretary meets during triage, a page someone linked. That
class has exactly the same problem the news feed exists to solve — it needs no
reply, fits no project, and today just gets archived and lost — so any agent can
file it here instead:

    python3 /workspace/scripts/news-add.py \
        --title "Sommerfest, 30 July, Zürich" \
        --url https://t.me/somechannel/1234 \
        --source "Quartierverein (Telegram)" \
        --summary "Open-air party at the Werdinsel, 18:00." \
        --expires 2026-07-30T23:00

`--expires` is what makes a dated item behave: it holds full weight until the
date, then drops out of the feed in one step. Leave it off for undated items —
they fade on the source's half-life instead.

The item is a reference like every other: title, source, excerpt, link. Do not
paste an article body in here.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_store as store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Add one news item.")
    ap.add_argument("--title", required=True)
    ap.add_argument("--url", required=True, help="where to read it — the item IS the link")
    ap.add_argument("--source", required=True, help="who broadcast it, as shown in the feed")
    ap.add_argument("--summary", default="", help="one or two sentences, not the article")
    ap.add_argument("--published", help="ISO 8601; defaults to now")
    ap.add_argument("--expires", help="ISO 8601; after this the item leaves the feed")
    ap.add_argument("--importance", type=float, help="0–5; omit to let the Herald score it")
    ap.add_argument("--half-life-hours", type=float, dest="half_life_hours")
    ap.add_argument("--lang", help="BCP-47 tag, used by read-aloud")
    ap.add_argument("--tag", action="append", default=[], dest="tags")
    args = ap.parse_args()

    published = store.parse_time(args.published) if args.published else None
    expires = store.parse_time(args.expires) if args.expires else None
    if args.expires and expires is None:
        print(f"[news-add] --expires is not a date: {args.expires!r}", file=sys.stderr)
        return 2
    now = store.now()

    item = {
        "id": hashlib.sha1(args.url.encode("utf-8")).hexdigest()[:16],
        "title": args.title.strip(),
        "url": args.url.strip(),
        "summary": args.summary.strip(),
        "source": args.source.strip(),
        "source_id": "manual",
        "lang": args.lang,
        "published": store.iso(published or now),
        "fetched": store.iso(now),
        "expires": store.iso(expires) if expires else None,
        "half_life_hours": args.half_life_hours,
        "importance": args.importance,
        "tags": args.tags,
        "read": False,
        "hidden": False,
    }
    added = store.add_items([item])
    if added:
        print(f"[news-add] added {item['id']}: {item['title']}", file=sys.stderr)
    else:
        print(f"[news-add] already in the feed: {item['id']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
