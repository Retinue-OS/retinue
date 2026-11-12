#!/usr/bin/env python3
"""Checks for the news store: ranking, decay, dated items, feedback, pruning.

The ranking is the part a reader has to be able to predict, so it is the part
under test here — sampled at explicit instants, never at "now".

    python3 tests/test_news_store.py
"""
import importlib.util
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

T0 = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def load_store(tmp: Path):
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "news_store_under_test", SCRIPTS_DIR / "news_store.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.NEWS_DIR = tmp
    tmp.mkdir(parents=True, exist_ok=True)
    # The store's clock is pinned to T0. The ranking checks below sample
    # explicit instants already, but pruning reads the clock itself, and it
    # runs inside add_items: once the calendar moved MAX_AGE_DAYS past T0
    # every fixture item was dropped on the way in and the suite went red on
    # its own, with nothing changed. A test's day must not be its input.
    mod.now = lambda: T0
    return mod


def item(**kw):
    base = {
        "id": kw.pop("id", "x"),
        "title": kw.pop("title", "Title"),
        "url": "https://example.invalid/x",
        "source": "Feed",
        "published": kw.pop("published", T0.isoformat()),
        "importance": kw.pop("importance", 3.0),
    }
    base.update(kw)
    return base


def test_default_importance_for_unscored(store):
    fresh = item(id="a", importance=None)
    assert abs(store.score(fresh, T0) - store.DEFAULT_IMPORTANCE) < 1e-9, \
        "an unscored item ranks at the neutral default"
    print("ok: unscored items rank at the default importance")


def test_half_life_decay(store):
    it = item(id="b", importance=4.0, half_life_hours=24)
    assert abs(store.score(it, T0) - 4.0) < 1e-9
    assert abs(store.score(it, T0 + timedelta(hours=24)) - 2.0) < 1e-9
    assert abs(store.score(it, T0 + timedelta(hours=48)) - 1.0) < 1e-9
    print("ok: an item halves per half-life")


def test_source_half_life_beats_default(store):
    slow = item(id="c", importance=4.0, half_life_hours=336)
    fast = item(id="d", importance=4.0, half_life_hours=6)
    a_day = T0 + timedelta(hours=24)
    assert store.score(slow, a_day) > store.score(fast, a_day), \
        "an evergreen source keeps its weight while a wire item fades"
    print("ok: per-item half-life overrides the default")


def test_dated_item_holds_then_drops(store):
    event = item(id="e", importance=3.0,
                 published=(T0 - timedelta(days=10)).isoformat(),
                 expires=(T0 + timedelta(days=5)).isoformat())
    # Ten days old but still pending: no decay at all.
    assert abs(store.score(event, T0) - 3.0) < 1e-9
    assert abs(store.score(event, T0 + timedelta(days=4)) - 3.0) < 1e-9
    # One second past the date it is worthless, in one step.
    assert store.score(event, T0 + timedelta(days=5, seconds=1)) == 0.0
    print("ok: a dated item holds full weight, then leaves in one step")


def test_read_is_damped_hidden_is_zero(store):
    read = item(id="f", importance=4.0, read=True)
    hidden = item(id="g", importance=4.0, hidden=True)
    assert abs(store.score(read, T0) - 4.0 * store.READ_FACTOR) < 1e-9
    assert store.score(hidden, T0) == 0.0
    print("ok: opened items are damped, hidden items score zero")


def test_ranked_orders_and_filters(store):
    store.save_items([
        item(id="hot", importance=5.0),
        item(id="cold", importance=1.0),
        item(id="seen", importance=5.0, read=True),
        item(id="gone", importance=5.0, hidden=True),
    ])
    feed = [i["id"] for i in store.ranked("feed", at=T0)]
    assert feed == ["hot", "cold"], feed
    assert [i["id"] for i in store.ranked("read", at=T0)] == ["seen"]
    assert [i["id"] for i in store.ranked("hidden", at=T0)] == ["gone"]
    assert len(store.ranked("all", at=T0)) == 3, "hidden stays out of 'all' (score 0)"
    print("ok: ranked() orders by score and honours the scopes")


def test_add_items_is_idempotent(store):
    store.save_items([])
    first = store.add_items([item(id="k1"), item(id="k2")])
    # Re-fetching a feed re-delivers everything; nothing may be duplicated or
    # reset, or a scored/read item would come back unscored on every hour.
    store.update_item("k1", importance=5.0, read=True)
    second = store.add_items([item(id="k1"), item(id="k2"), item(id="k3")])
    ids = sorted(i["id"] for i in store.load_items())
    kept = next(i for i in store.load_items() if i["id"] == "k1")
    assert (first, second) == (2, 1), (first, second)
    assert ids == ["k1", "k2", "k3"], ids
    assert kept["importance"] == 5.0 and kept["read"] is True, kept
    print("ok: re-fetching adds only new ids and preserves item state")


def test_prune_drops_stale_and_lapsed(store):
    old = item(id="old", published=(store.now() - timedelta(days=90)).isoformat())
    lapsed = item(id="lapsed", published=store.now().isoformat(),
                  expires=(store.now() - timedelta(days=5)).isoformat())
    live = item(id="live", published=store.now().isoformat())
    kept = [i["id"] for i in store._prune([old, lapsed, live])]
    assert kept == ["live"], kept
    print("ok: pruning drops long-stale and long-lapsed items")


def test_feedback_nudges_and_logs(store):
    store.save_items([item(id="n1", importance=3.0)])
    store.record_feedback("n1", "up")
    assert store.load_items()[0]["importance"] == 4.0
    store.record_feedback("n1", "down")
    after = store.load_items()[0]
    assert after["importance"] == 3.0 and after["read"] is True, after
    store.record_feedback(None, "note", "less crypto")
    pending, cursor = store.pending_feedback()
    assert [e["signal"] for e in pending] == ["up", "down", "note"], pending
    assert pending[-1]["note"] == "less crypto"
    store.ack_feedback(cursor)
    assert store.pending_feedback() == ([], cursor)
    print("ok: feedback nudges the item, logs it, and the cursor consumes it")


def test_a_link_is_a_web_link_or_none(store):
    """An item's link is where its headline on the news page leads, and it is
    text from outside. One rule for every producer and for the feed's reader:
    an absolute http(s) URL with a host is a link, anything else is none."""
    link = store.web_link
    # Kept as given, bar surrounding whitespace: item ids derive from the link.
    for url in ("https://example.invalid/a", "http://example.invalid",
                "HTTPS://Example.invalid/A?b=1#c", "https://user@example.invalid:8443/x",
                "https://[2001:db8::1]/x", "https://example.invalid/a b",
                "https://zürich.example/straße"):
        assert link(url) == url, url
        assert link(f"  {url}\n") == url, url
    for none in (None, "", "   ", 7, [], {"url": "https://example.invalid/"}, b"https://x/",
                 # relative to wherever it is shown
                 "/sends/default/1", "news.html", "../x", "?scope=all", "#new",
                 "//example.invalid/x", "example.invalid/x", "www.example.invalid",
                 "https:example.invalid", "https:/example.invalid", "http:x",
                 "https:\\\\example.invalid", "https://example.invalid\\@other.invalid/",
                 # no host
                 "https://", "https:///x", "https://?x", "https://:80/", "https://@/x",
                 "https://example.invalid:port/", "https://example.invalid:99999/",
                 "https://exa mple.invalid/",
                 # another kind of address
                 "javascript:alert(1)", "JAVASCRIPT:alert(1)", "data:text/html,x",
                 "file:///etc/hosts", "ftp://example.invalid/x", "mailto:a@example.invalid",
                 "tel:+41441234567", "intent://x#Intent;scheme=https;end", "about:blank",
                 # a control character inside
                 "https://example.invalid/\nx", "https://exa\tmple.invalid/",
                 "ht\ntps://example.invalid/", "java\tscript:alert(1)",
                 "\x00https://example.invalid/", "https://example.invalid/\x7f"):
        assert link(none) == "", repr(none)

    # A feed may write its links relative to itself: resolved against the
    # feed's own URL, as a reader of the feed would.
    feed = "https://feed.invalid/blog/rss.xml"
    assert link("/2026/fest", feed) == "https://feed.invalid/2026/fest"
    assert link("fest.html", feed) == "https://feed.invalid/blog/fest.html"
    assert link("../fest", feed) == "https://feed.invalid/fest"
    assert link("?p=12", feed) == "https://feed.invalid/blog/rss.xml?p=12"
    assert link("//other.invalid/x", feed) == "https://other.invalid/x"
    # A web link is not touched by the base, and nothing else becomes one.
    assert link("http://Other.invalid/x", feed) == "http://Other.invalid/x"
    for none in ("javascript:alert(1)", "data:text/html,x", "mailto:a@example.invalid",
                 "/a\nb", "", "   ", None):
        assert link(none, feed) == "", repr(none)
    # A base that is no web address gives no web link.
    assert link("/x", "file:///srv/feed.xml") == ""
    assert link("/x", "") == "" and link("/x", "not a url") == ""
    print("ok: a link is an absolute http(s) URL with a host, or none")


def test_unknown_item_feedback_raises(store):
    try:
        store.record_feedback("nope", "up")
    except KeyError:
        print("ok: feedback on an unknown item raises")
        return
    raise AssertionError("expected KeyError for an unknown item id")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir) / "news"
        store = load_store(tmp)
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn(store)
    print("\nAll news store tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
