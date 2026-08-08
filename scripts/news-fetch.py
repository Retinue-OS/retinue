#!/usr/bin/env python3
"""Collect news items from the feeds each chamber declares. Zero Claude credits.

A chamber lists its feeds in a **`.news.json`** at its root — the same
per-chamber manifest convention as `.refresh.json` (data freshness) and
`.schedule.json` (scheduled jobs), so a deployment adds a news source by editing
its own chamber, never this framework:

    {
      "feeds": [
        {"id": "nzz", "url": "https://www.nzz.ch/recent.rss", "label": "NZZ"},
        {"id": "club", "url": "https://club.example/feed.xml",
         "label": "Club", "half_life_hours": 168}
      ]
    }

Optional per-feed keys: `label` (what the feed is called in the UI, defaults to
the id), `half_life_hours` (how fast this source's items should fade — a weekly
club bulletin ages slower than a news wire), `lang` (BCP-47, used by the
dashboard's read-aloud when an item carries no language of its own), and
`enabled: false` to park a feed without deleting it.

What lands in the store is a *reference*: title, source, the excerpt the feed
itself published (truncated), and the link. The article is read at the source,
never mirrored here.

    python3 scripts/news-fetch.py             # every chamber manifest
    python3 scripts/news-fetch.py --manifest /path/.news.json --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_store as store  # noqa: E402

CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR", "/workspace/chambers"))
FETCH_TIMEOUT = float(os.environ.get("NEWS_FETCH_TIMEOUT", "20"))
MAX_FEED_BYTES = 8 * 1024 * 1024
SUMMARY_CHARS = 400
USER_AGENT = "retinue-news/1.0 (+https://github.com/retinue-os/retinue)"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?…»)\]])")


def log(msg: str) -> None:
    print(f"[news-fetch] {msg}", file=sys.stderr, flush=True)


# ── manifests ────────────────────────────────────────────────────────────────

def discover_manifests() -> list[Path]:
    if not CHAMBERS_DIR.is_dir():
        return []
    return sorted(p for p in CHAMBERS_DIR.glob("*/.news.json") if p.is_file())


def read_manifest(path: Path) -> list[dict]:
    """Return the feed entries of one manifest, tagged with their chamber.

    A broken manifest is reported and skipped: one chamber's typo must not stop
    every other chamber's news."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"skipping unreadable manifest {path}: {exc}")
        return []
    feeds = data.get("feeds") if isinstance(data, dict) else None
    if not isinstance(feeds, list):
        log(f"manifest {path} has no 'feeds' list; skipping")
        return []
    chamber = path.parent.name
    out = []
    for raw in feeds:
        if not isinstance(raw, dict) or not raw.get("url"):
            continue
        if raw.get("enabled") is False:
            continue
        feed = dict(raw)
        feed.setdefault("id", hashlib.sha1(feed["url"].encode()).hexdigest()[:8])
        feed.setdefault("label", feed["id"])
        feed["chamber"] = chamber
        out.append(feed)
    return out


# ── feed parsing ─────────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    """Element tag without its namespace — RSS 2.0, RSS 1.0/RDF and Atom differ
    only in namespace for the fields we read, so one traversal covers all
    three."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _find(parent, *names):
    for child in parent:
        if _local(child.tag) in names:
            return child
    return None


def _find_all(root, *names):
    return [el for el in root.iter() if _local(el.tag) in names]


def clean_summary(raw: str) -> str:
    """Feed descriptions carry markup and boilerplate; the card wants a sentence
    or two. Strip tags, unescape, collapse whitespace, cut at a word boundary."""
    text = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", raw or ""))).strip()
    # Replacing a tag with a space leaves "highway ." wherever markup hugged
    # punctuation; close those gaps so the excerpt reads like the source did.
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    if len(text) <= SUMMARY_CHARS:
        return text
    cut = text[:SUMMARY_CHARS].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:—-") + "…"


def _entry_link(entry) -> str:
    """The URL to read this item at — an RSS <link> body or an Atom
    <link rel="alternate" href>. This is the whole point of an item, so an entry
    without one is dropped by the caller."""
    alternate = ""
    for child in entry:
        if _local(child.tag) != "link":
            continue
        href = child.get("href")
        if href:
            rel = child.get("rel") or "alternate"
            if rel == "alternate":
                return href.strip()
            alternate = alternate or href.strip()
        elif _text(child):
            return _text(child)
    if alternate:
        return alternate
    guid = _find(entry, "guid", "id")
    text = _text(guid)
    return text if text.startswith(("http://", "https://")) else ""


def _entry_time(entry) -> str | None:
    for name in ("published", "pubDate", "updated", "date", "modified"):
        el = _find(entry, name)
        text = _text(el)
        if not text:
            continue
        parsed = store.parse_time(text)
        if parsed is None:
            try:  # RSS 2.0 dates are RFC 822, not ISO 8601
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed is not None:
            return store.iso(parsed)
    return None


def parse_feed(xml_text: str, feed: dict) -> list[dict]:
    """Turn one feed document into news items. Unparseable XML yields nothing."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log(f"feed '{feed['id']}' is not valid XML: {exc}")
        return []

    # Language, if the feed states one: the dashboard's read-aloud picks a voice
    # from it. Declared per feed wins over the document's own <language>.
    declared = _find_all(root, "language")
    feed_lang = feed.get("lang") or (_text(declared[0]) if declared else "")
    fetched = store.iso(store.now())
    items = []
    for entry in _find_all(root, "item", "entry"):
        url = _entry_link(entry)
        title = _text(_find(entry, "title"))
        if not url or not title:
            continue
        summary_el = _find(entry, "description", "summary", "content")
        item_lang = entry.get("{http://www.w3.org/XML/1998/namespace}lang") or feed_lang
        items.append({
            "id": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
            "title": _WS_RE.sub(" ", html.unescape(title)).strip(),
            "url": url,
            "summary": clean_summary(_text(summary_el)),
            "source": feed.get("label") or feed["id"],
            "source_id": feed["id"],
            "chamber": feed.get("chamber"),
            "lang": item_lang or None,
            "published": _entry_time(entry) or fetched,
            "fetched": fetched,
            "half_life_hours": feed.get("half_life_hours"),
            "importance": None,   # unscored until the Herald looks at it
            "read": False,
            "hidden": False,
        })
    return items


def fetch_feed(feed: dict) -> list[dict]:
    req = urllib.request.Request(feed["url"], headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            raw = resp.read(MAX_FEED_BYTES)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"feed '{feed['id']}' unreachable: {exc}")
        return []
    return parse_feed(raw.decode("utf-8", errors="replace"), feed)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch declared news feeds.")
    ap.add_argument("--manifest", action="append", default=[],
                    help="explicit .news.json path (repeatable); default: every chamber's")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, write nothing")
    args = ap.parse_args()

    paths = [Path(p) for p in args.manifest] or discover_manifests()
    if not paths:
        log("no .news.json manifest found; nothing to fetch")
        return 0

    feeds = [f for path in paths for f in read_manifest(path)]
    if not feeds:
        log("manifests declare no enabled feeds")
        return 0

    collected: list[dict] = []
    for feed in feeds:
        items = fetch_feed(feed)
        log(f"{feed['id']}: {len(items)} item(s)")
        collected.extend(items)

    if args.dry_run:
        print(json.dumps(collected, ensure_ascii=False, indent=1))
        return 0

    added = store.add_items(collected)
    log(f"{added} new item(s) of {len(collected)} fetched from {len(feeds)} feed(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
