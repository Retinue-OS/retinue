#!/usr/bin/env python3
"""Checks for the news collector: manifest reading and feed parsing.

No network: the feed documents are inline fixtures, one per syndication format
this has to cope with (RSS 2.0, Atom, RSS 1.0/RDF), because "the source you were
given publishes the other kind" is the normal case, not the exotic one. What an
entry may carry as its link has checks of its own: a web link or none, and a
link written relative to the feed read against the feed's URL (one of them
fetches a fixture from a server on a local port, to follow a redirect).

    python3 tests/test_news_fetch.py
"""
import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    # And the id is that of the link as the feed published it: knowing where
    # the feed lives must not change the link, or every item already in the
    # store would come in a second time.
    c = fetch.parse_feed(RSS2, {"id": "qv", "url": "https://Feed.invalid/rss.xml"})[0]
    assert c["url"] == a["url"] == "https://example.invalid/fest"
    assert c["id"] == a["id"] == hashlib.sha1(b"https://example.invalid/fest").hexdigest()[:16]
    print("ok: item ids derive from the URL, so re-syndication deduplicates")


# An item's link is where its headline on the news page leads, and a feed puts
# into a link whatever it likes. The three checks below cover the rule: a web
# link or none, a relative link read as the feed means it, and the search
# through an entry's candidates going on past one that is no web link.

RELATIVE = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Links relative to the feed</title>
  <item><title>Root-relative</title><link>/2026/fest</link></item>
  <item><title>Path-relative</title><link> notes/42.html </link></item>
  <item><title>Absolute</title><link>https://elsewhere.invalid/x</link></item>
</channel></rss>
"""

ODD_LINKS_ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Odd links</title>
  <entry>
    <title>The next alternate link</title>
    <link rel="alternate" href="javascript:void(0)"/>
    <link rel="alternate" href="https://example.invalid/second"/>
    <id>https://example.invalid/id-1</id>
  </entry>
  <entry>
    <title>A link of another relation</title>
    <link href="data:text/html,x"/>
    <link rel="related" href="https://example.invalid/related"/>
    <id>https://example.invalid/id-2</id>
  </entry>
  <entry>
    <title>The id, when it is a web link</title>
    <link href="mailto:editor@example.invalid"/>
    <id>https://example.invalid/id-3</id>
  </entry>
  <entry>
    <title>No web link anywhere</title>
    <link href="javascript:alert(1)"/>
    <link rel="enclosure" href="file:///srv/audio.mp3"/>
    <id>urn:uuid:1225c695-cfb8-4ebb-aaaa-80da344efa6a</id>
  </entry>
</feed>
"""

ODD_LINKS_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Odd links</title>
  <item><title>The guid</title>
    <link>javascript:alert(1)</link><guid>https://example.invalid/guid-1</guid></item>
  <item><title>The second link element</title>
    <link> </link><link>ftp://example.invalid/x</link><link>https://example.invalid/two</link></item>
  <item><title>A guid is a name</title>
    <link>intent://example.invalid/#Intent;scheme=https;end</link><guid>/not-a-link</guid></item>
</channel></rss>
"""


def test_relative_links_resolve_against_the_feed(fetch):
    feed = {"id": "rel", "url": "https://feed.invalid/blog/rss.xml"}
    assert [i["url"] for i in fetch.parse_feed(RELATIVE, feed)] == [
        "https://feed.invalid/2026/fest",
        "https://feed.invalid/blog/notes/42.html",
        "https://elsewhere.invalid/x"]
    # Where the document was really read from (a redirect) is what counts.
    moved = fetch.parse_feed(RELATIVE, feed, "https://moved.invalid/feeds/rss.xml")
    assert [i["url"] for i in moved] == [
        "https://moved.invalid/2026/fest",
        "https://moved.invalid/feeds/notes/42.html",
        "https://elsewhere.invalid/x"]
    # With no address to read them against they are no links, and no items.
    assert [i["url"] for i in fetch.parse_feed(RELATIVE, {"id": "rel"})] == [
        "https://elsewhere.invalid/x"]
    assert [i["url"] for i in fetch.parse_feed(
        RELATIVE, {"id": "rel", "url": "file:///srv/rss.xml"})] == [
        "https://elsewhere.invalid/x"]
    print("ok: a link relative to the feed is resolved against the feed's URL")


def test_a_fetched_feed_is_read_against_where_it_came_from(fetch):
    """The one check that fetches, from a server on a local port: a feed that
    has moved answers with a redirect, and its relative links then belong to
    the place the document came from, not to the address in the manifest."""
    class Feed(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path == "/rss.xml":
                self.send_response(302)
                self.send_header("Location", "/moved/rss.xml")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = RELATIVE.encode("utf-8")
            self.send_response(200 if self.path == "/moved/rss.xml" else 404)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Feed)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        items = fetch.fetch_feed({"id": "moved", "url": origin + "/rss.xml"})
        direct = fetch.fetch_feed({"id": "direct", "url": origin + "/moved/rss.xml"})
    finally:
        server.shutdown()
        server.server_close()
    want = [origin + "/2026/fest", origin + "/moved/notes/42.html",
            "https://elsewhere.invalid/x"]
    assert [i["url"] for i in items] == want, items
    assert [i["url"] for i in direct] == want, direct
    print("ok: a fetched feed's relative links resolve against where it was read from")


def test_a_link_that_is_no_web_link_is_passed_over(fetch):
    feed = {"id": "odd", "url": "https://feed.invalid/atom.xml"}
    atom = fetch.parse_feed(ODD_LINKS_ATOM, feed)
    assert [(i["title"], i["url"]) for i in atom] == [
        ("The next alternate link", "https://example.invalid/second"),
        ("A link of another relation", "https://example.invalid/related"),
        ("The id, when it is a web link", "https://example.invalid/id-3"),
    ], atom
    rss = fetch.parse_feed(ODD_LINKS_RSS, feed)
    assert [(i["title"], i["url"]) for i in rss] == [
        ("The guid", "https://example.invalid/guid-1"),
        ("The second link element", "https://example.invalid/two"),
    ], rss
    print("ok: a candidate that is no web link is passed over; the search goes on")


def test_every_item_carries_a_web_link(fetch):
    feed = {"id": "all", "url": "https://feed.invalid/rss.xml"}
    for doc in (RSS2, ATOM, RDF, RELATIVE, ODD_LINKS_ATOM, ODD_LINKS_RSS):
        for it in fetch.parse_feed(doc, feed):
            assert fetch.store.web_link(it["url"]) == it["url"], it
            assert it["id"] == hashlib.sha1(it["url"].encode("utf-8")).hexdigest()[:16]
    print("ok: what the fetcher files is a web link, and the id is the link's")


# A feed response is untrusted input: the URL is owner-controlled chamber config,
# but the bytes it returns on any given hour are the remote host's choice. The
# four checks below cover the DTD guard — the attack it stops, the false positive
# it must not cause, and the legitimate prologs it must let through.


def test_amplifying_feed_is_refused(fetch):
    # Measured against this parser: 2 MB of input expands to 101 MB of text
    # while staying under expat's 100x running-amplification limit, so expat
    # alone does NOT stop this. Scaled down here to keep the test instant; the
    # point is that it never reaches ET.fromstring at all.
    bomb = ('<?xml version="1.0"?>\n<!DOCTYPE rss [<!ENTITY big "%s">]>\n'
            '<rss><channel><item><title>%s</title>'
            '<link>https://example.invalid/x</link></item></channel></rss>'
            % ("x" * 500, ("&big;" + " " * 5) * 2000))
    assert fetch.has_doctype(bomb)
    assert fetch.parse_feed(bomb, {"id": "bomb"}) == []
    print("ok: an entity-amplifying feed is refused before it is parsed")


def test_html_error_page_is_refused(fetch):
    page = "<!DOCTYPE html>\n<html><body>502 Bad Gateway</body></html>"
    assert fetch.parse_feed(page, {"id": "oops"}) == []
    print("ok: an HTML error page served instead of a feed is refused")


def test_doctype_inside_content_still_parses(fetch):
    # A tech feed quoting HTML in an item body is not an attack — the guard must
    # look at the prolog only, or it would reject legitimate feeds.
    quoting = RSS2.replace("<description>dropped</description>",
                           "<description>&lt;!DOCTYPE html&gt; is how it starts"
                           "</description><link>https://example.invalid/doc</link>")
    assert not fetch.has_doctype(quoting)
    assert len(fetch.parse_feed(quoting, {"id": "qv"})) == 2
    print("ok: '<!DOCTYPE' inside an item is content, not a DTD")


def test_prolog_variants(fetch):
    assert not fetch.has_doctype('<?xml version="1.0"?><rss/>')
    assert not fetch.has_doctype('﻿\n <?xml version="1.0"?>\n<!-- hi --><rss/>')
    assert not fetch.has_doctype('<rss/>')
    assert not fetch.has_doctype('')
    assert fetch.has_doctype('﻿ <?xml version="1.0"?><!-- c --> <!DOCTYPE rss><rss/>')
    print("ok: the prolog scan walks BOM, XML declaration and comments")


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
