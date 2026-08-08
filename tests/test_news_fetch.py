#!/usr/bin/env python3
"""Checks for the news collector: manifest reading and feed parsing.

No network: the feed documents are inline fixtures, one per syndication format
this has to cope with (RSS 2.0, Atom, RSS 1.0/RDF), because "the source you were
given publishes the other kind" is the normal case, not the exotic one.

    python3 tests/test_news_fetch.py
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

RSS2 = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Quartierverein</title>
  <language>de</language>
  <item>
    <title>Sommerfest am 30. Juli</title>
    <link>https://example.invalid/fest</link>
    <description>&lt;p&gt;Open-air auf der &lt;b&gt;Werdinsel&lt;/b&gt;, 18:00.&lt;/p&gt;</description>
    <pubDate>Thu, 23 Jul 2026 16:43:41 +0000</pubDate>
  </item>
  <item>
    <title>Keine URL, kein Item</title>
    <description>dropped</description>
  </item>
</channel></rss>
"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>The Guide</title>
  <entry>
    <title>Entry updated: Vogons</title>
    <link rel="edit" href="https://example.invalid/edit/1"/>
    <link rel="alternate" href="https://example.invalid/vogons"/>
    <summary>Still the worst poetry in the universe.</summary>
    <updated>2026-08-01T09:30:00Z</updated>
  </entry>
</feed>
"""

RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://example.invalid/"><title>Old school</title></channel>
  <item rdf:about="https://example.invalid/rdf-1">
    <title>RSS 1.0 still exists</title>
    <link>https://example.invalid/rdf-1</link>
    <description>And feeds still use it.</description>
    <dc:date>2026-08-02T08:00:00+02:00</dc:date>
  </item>
</rdf:RDF>
"""


def load_fetch(tmp: Path):
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "news_fetch_under_test", SCRIPTS_DIR / "news-fetch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.store.NEWS_DIR = tmp / "news"
    mod.CHAMBERS_DIR = tmp / "chambers"
    return mod


def test_rss2(fetch):
    items = fetch.parse_feed(RSS2, {"id": "qv", "label": "Quartierverein"})
    assert len(items) == 1, "an entry without a link is not an item"
    it = items[0]
    assert it["title"] == "Sommerfest am 30. Juli"
    assert it["url"] == "https://example.invalid/fest"
    assert it["summary"] == "Open-air auf der Werdinsel, 18:00.", it["summary"]
    assert it["published"].startswith("2026-07-23T16:43:41"), it["published"]
    assert it["lang"] == "de", "the feed's own <language> is used when set"
    assert it["importance"] is None, "a fetched item is unscored until the Herald sees it"
    print("ok: RSS 2.0 items parse, with RFC-822 dates and stripped markup")


def test_atom_prefers_alternate_link(fetch):
    items = fetch.parse_feed(ATOM, {"id": "guide", "label": "The Guide"})
    assert len(items) == 1
    assert items[0]["url"] == "https://example.invalid/vogons", items[0]["url"]
    assert items[0]["published"].startswith("2026-08-01T09:30:00"), items[0]["published"]
    print("ok: Atom entries parse and the alternate link wins over rel=edit")


def test_rss1_rdf(fetch):
    items = fetch.parse_feed(RDF, {"id": "old", "label": "Old school"})
    assert len(items) == 1 and items[0]["url"] == "https://example.invalid/rdf-1"
    assert items[0]["published"].startswith("2026-08-02T06:00:00"), items[0]["published"]
    print("ok: RSS 1.0/RDF parses, dc:date normalized to UTC")


def test_ids_are_stable_per_url(fetch):
    a = fetch.parse_feed(RSS2, {"id": "qv"})[0]
    b = fetch.parse_feed(RSS2, {"id": "other-feed"})[0]
    assert a["id"] == b["id"], \
        "the same article syndicated twice must be one item, not two"
    print("ok: item ids derive from the URL, so re-syndication deduplicates")


def test_broken_xml_yields_nothing(fetch):
    assert fetch.parse_feed("<rss><channel", {"id": "broken"}) == []
    print("ok: a malformed feed yields no items instead of raising")


def test_manifest_discovery(fetch):
    chamber = fetch.CHAMBERS_DIR / "hitchhiker"
    chamber.mkdir(parents=True, exist_ok=True)
    (chamber / ".news.json").write_text(json.dumps({"feeds": [
        {"id": "on", "url": "https://example.invalid/a.xml", "half_life_hours": 12},
        {"id": "off", "url": "https://example.invalid/b.xml", "enabled": False},
        {"id": "no-url"},
    ]}))
    (fetch.CHAMBERS_DIR / "empty").mkdir(parents=True, exist_ok=True)

    paths = fetch.discover_manifests()
    names = [p.parent.name for p in paths]
    assert "hitchhiker" in names and "empty" not in names, names
    feeds = fetch.read_manifest(chamber / ".news.json")
    assert [f["id"] for f in feeds] == ["on"], feeds
    assert feeds[0]["chamber"] == "hitchhiker" and feeds[0]["label"] == "on"
    print("ok: manifests are discovered per chamber; disabled/urlless feeds skipped")


def test_broken_manifest_is_skipped(fetch):
    chamber = fetch.CHAMBERS_DIR / "broken"
    chamber.mkdir(parents=True, exist_ok=True)
    path = chamber / ".news.json"
    path.write_text("{not json")
    assert fetch.read_manifest(path) == [], \
        "one chamber's typo must not stop every other chamber's news"
    print("ok: an unreadable manifest is skipped, not fatal")


def test_summary_truncation(fetch):
    long = "word " * 400
    out = fetch.clean_summary(long)
    assert len(out) <= fetch.SUMMARY_CHARS + 1 and out.endswith("…"), len(out)
    print("ok: summaries are truncated — the item is a reference, not a copy")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        fetch = load_fetch(Path(tmpdir))
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn(fetch)
    print("\nAll news fetch tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
