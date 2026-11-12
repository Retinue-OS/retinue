#!/usr/bin/env python3
"""Checks for the news endpoints and the curation gate.

Covers the pure logic behind the new surfaces without running an HTTP server:
the gateway's feed/preferences payloads, the score writer's validation, and the
gate that decides whether a Claude session is spawned at all. One thing is
checked from end to end, because it is a rule about what comes in from
outside: an item's link is a web link or none — as the feed serves it, as the
messenger rail (POST /internal/news, on a local port) files it, and as
news-add.py takes it.

    python3 tests/test_news_gateway.py
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load(name: str, filename: str):
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_gateway(tmp: Path):
    os.environ["NEWS_DIR"] = str(tmp / "news")
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    # The news rail is open unless a token is configured, and is used so here.
    os.environ.pop("NEWS_INGEST_TOKEN", None)
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    # markdown_it ships in the runtime image but not necessarily where the tests
    # run; the gateway only uses it to render the per-day log pages, which none
    # of these checks touch.
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            class _StubMd:
                def __init__(self, *_a, **_kw):
                    pass

                def enable(self, *_a, **_kw):
                    return self

                def render(self, text):
                    return text

            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = _StubMd
            sys.modules["markdown_it"] = stub
    return _load("web_gateway_under_test", "web-gateway.py")


def item(**kw):
    base = {"id": "i1", "title": "T", "url": "https://example.invalid/a",
            "source": "Feed", "published": "2026-08-08T12:00:00+00:00",
            "importance": 3.0}
    base.update(kw)
    return base


def test_feed_payload(wg, store):
    store.save_items([item(id="a", importance=5.0),
                      item(id="b", importance=1.0),
                      item(id="c", importance=5.0, hidden=True)])
    body = wg._news_payload("feed", None)
    assert [i["id"] for i in body["items"]] == ["a", "b"], body["items"]
    assert body["items"][0]["score"] >= body["items"][1]["score"]
    assert "generated" in body
    limited = wg._news_payload("feed", 1)
    assert len(limited["items"]) == 1
    print("ok: GET /news ranks live, honours scope and limit")


def test_feed_serves_a_web_link_or_none(wg, store):
    """An item leaves the store with a web link or with none.

    The link is where the headline on the news page leads. Producers file
    only web links, and the feed applies the same rule on the way out, so an
    item that is in the store with anything else (filed before the rule, or
    written by hand) is served without a link. The stored item is not
    rewritten: the feed is a read."""
    urls = {
        "web": "https://example.invalid/a?b=1",
        "padded": "  http://example.invalid/b \n",
        "none": "",
        "page": "/sends/default/752",
        "bare": "example.invalid/c",
        "script": "javascript:alert(1)",
        "data": "data:text/html,x",
        "broken": "https://exa\nmple.invalid/",
        "number": 7,
    }
    # Published now: an item of the fixture's fixed date fades with the
    # calendar, and this check is about links, not about rank.
    now = store.iso(store.now())
    stored = [item(id=key, url=url, published=now) for key, url in urls.items()]
    stored.append({k: v for k, v in item(id="absent", published=now).items() if k != "url"})
    store.save_items(stored)
    for scope in ("feed", "all"):
        served = {i["id"]: i["url"] for i in wg._news_payload(scope, None)["items"]}
        assert served == {
            "web": "https://example.invalid/a?b=1", "padded": "http://example.invalid/b",
            "none": "", "absent": "", "page": "", "bare": "", "script": "", "data": "",
            "broken": "", "number": "",
        }, served
    assert [i.get("url") for i in store.load_items()] == [i.get("url") for i in stored]
    print("ok: GET /news serves an item's link only when it is a web link")


def _post_news(base: str, payload: dict):
    """POST one message to the news rail: (status, body)."""
    req = urllib.request.Request(
        base + "/internal/news", data=json.dumps(payload).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_news_rail_files_a_web_link_or_none(wg, store):
    """The rail takes a link when it is a web link, and files the item anyway.

    POST /internal/news carries a message from a channel the deployment does
    not control, and is open by default. Its ``url`` is kept only as an
    absolute http(s) link; anything else is dropped and the item filed
    without one, exactly as a post that carries no link is: keyed by its
    source and text."""
    store.save_items([])
    text, source = "Sommerfest am 30. Juli\nOpen-air, 18:00.", "Quartierverein"
    linkless_id = hashlib.sha1(f"{source}\n{text}".encode("utf-8")).hexdigest()[:16]
    server = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def filed(**extra):
        status, body = _post_news(base, {"channel": "telegram", "source": source,
                                         "text": text, **extra})
        assert status == 200 and body["ok"] is True, (status, body)
        return next(i for i in store.load_items() if i["id"] == body["id"]), body

    try:
        link = "https://t.me/quartier/1234"
        it, body = filed(url=f" {link} ")
        assert it["url"] == link and body["added"] == 1, it
        assert it["id"] == hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]
        assert it["title"] == "Sommerfest am 30. Juli" and it["source"] == source

        # No link, and whatever is no web link, is one and the same item.
        it, body = filed()
        assert it["url"] == "" and it["id"] == linkless_id and body["added"] == 1, it
        for url in ("", "javascript:alert(1)", "/sends/default/752", "t.me/quartier/1234",
                    "data:text/html,x", "file:///etc/hosts", "https://exa\nmple.invalid/",
                    42, ["https://example.invalid/"], {"href": "https://example.invalid/"}):
            it, body = filed(url=url)
            assert it["url"] == "" and it["id"] == linkless_id, (url, it)
            assert body["added"] == 0, "the same post without a link, filed once"
    finally:
        server.shutdown()
        server.server_close()
    assert sorted(i["url"] for i in store.load_items()) == ["", link]
    assert all(i["url"] == store.web_link(i["url"]) for i in store.load_items())
    print("ok: the news rail keeps a web link and files anything else without one")


def test_news_add_refuses_what_is_no_web_link(store):
    """news-add.py files a web link, and says so when it is given none."""
    store.save_items([])
    script = str(SCRIPTS_DIR / "news-add.py")
    env = dict(os.environ, NEWS_DIR=str(store.NEWS_DIR))

    def run(url):
        return subprocess.run(
            [sys.executable, script, "--title", "Sommerfest", "--source", "Quartierverein",
             "--url", url], env=env, capture_output=True, text=True, timeout=60)

    for url in ("javascript:alert(1)", "/sends/default/752", "t.me/quartier/1234",
                "mailto:info@example.invalid", "https://", " "):
        proc = run(url)
        assert proc.returncode == 2, (url, proc.returncode, proc.stderr)
        assert "--url is not a web link" in proc.stderr and "http://" in proc.stderr, proc.stderr
    assert store.load_items() == [], "a refused item is not filed"
    proc = run("https://t.me/quartier/1234")
    assert proc.returncode == 0, proc.stderr
    assert [i["url"] for i in store.load_items()] == ["https://t.me/quartier/1234"]
    # The same link typed with blanks around it is the same item, not a second one.
    proc = run("  https://t.me/quartier/1234 ")
    assert proc.returncode == 0, proc.stderr
    assert [i["url"] for i in store.load_items()] == ["https://t.me/quartier/1234"], store.load_items()
    print("ok: news-add.py refuses a --url that is no web link, with exit code 2")


def test_preferences_payload_defaults(wg, store):
    body = wg._news_preferences_payload()
    assert "News preferences" in body["markdown"], \
        "a store with no profile yet still answers with the template"
    store.save_preferences("# News preferences\n\n- cycling\n")
    body = wg._news_preferences_payload()
    assert "cycling" in body["markdown"] and body["updated"], body
    print("ok: GET /news/preferences serves the profile (template until written)")


def test_score_writer_validates(score, store):
    store.save_items([item(id="s1", importance=None), item(id="s2", importance=None)])
    applied, skipped = score.apply([
        {"id": "s1", "importance": 9, "reason": "clamped", "tags": "events"},
        {"id": "s2", "importance": "not a number"},
        {"id": "missing", "importance": 3},
        {"no-id": True},
    ])
    s1 = next(i for i in store.load_items() if i["id"] == "s1")
    assert (applied, skipped) == (1, 3), (applied, skipped)
    assert s1["importance"] == store.MAX_IMPORTANCE, "out-of-range importance is clamped"
    assert s1["tags"] == ["events"] and s1["scored_at"], s1
    assert next(i for i in store.load_items() if i["id"] == "s2")["importance"] is None
    print("ok: news-score.py clamps, rejects junk, and skips unknown ids")


def test_score_writer_normalizes_expiry(score, store):
    store.save_items([item(id="e1", importance=None)])
    applied, _ = score.apply([{"id": "e1", "importance": 4, "expires": "2026-07-30"}])
    e1 = store.load_items()[0]
    assert applied == 1 and e1["expires"].startswith("2026-07-30T00:00:00"), e1
    print("ok: a bare date expiry is normalized to a full timestamp")


def test_gate_is_silent_when_idle(curate, store):
    store.save_items([item(id="done", importance=4.0)])
    store.ack_feedback(len(store.all_feedback()))
    assert curate.unscored_items(40) == [], "nothing unscored"
    assert store.pending_feedback()[0] == [], "no unconsumed feedback"
    print("ok: a fully scored feed with no new feedback spawns nothing")


def test_gate_collects_work(curate, store):
    store.save_items([item(id="u1", importance=None, published="2026-08-08T09:00:00+00:00"),
                      item(id="u2", importance=None, published="2026-08-08T11:00:00+00:00"),
                      item(id="skip", importance=None, hidden=True),
                      item(id="scored", importance=2.0)])
    pending = curate.unscored_items(40)
    assert [i["id"] for i in pending] == ["u2", "u1"], \
        "newest first — the most visible items get scored first"
    store.record_feedback(None, "note", "less crypto")
    payload = curate.build_payload(pending, store.pending_feedback()[0])
    assert [i["id"] for i in payload["items"]] == ["u2", "u1"]
    assert payload["feedback"][-1]["note"] == "less crypto"
    assert set(payload["items"][0]) == {"id", "title", "source", "url", "summary",
                                        "published", "lang"}, \
        "the payload hands over references only — no scores, no internal flags"
    prompt = curate.build_prompt(Path("/tmp/p.json"), 2, 1)
    assert "herald" in prompt and "/tmp/p.json" in prompt
    print("ok: the gate collects unscored items plus pending feedback for the Herald")


def test_curate_batch_cap(curate, store):
    store.save_items([item(id=f"n{i}", importance=None) for i in range(10)])
    assert len(curate.unscored_items(3)) == 3
    print("ok: a curation run is capped at a batch")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        wg = load_gateway(tmp)
        store = wg.news_store
        score = _load("news_score_under_test", "news-score.py")
        curate = _load("news_curate_under_test", "news-curate.py")
        # All three modules import the same news_store instance from sys.path,
        # but each got its own copy via importlib; point them at one directory.
        for mod in (score, curate):
            mod.store.NEWS_DIR = store.NEWS_DIR

        test_feed_payload(wg, store)
        test_feed_serves_a_web_link_or_none(wg, store)
        test_news_rail_files_a_web_link_or_none(wg, store)
        test_news_add_refuses_what_is_no_web_link(store)
        test_preferences_payload_defaults(wg, store)
        test_score_writer_validates(score, store)
        test_score_writer_normalizes_expiry(score, store)
        test_gate_is_silent_when_idle(curate, store)
        test_gate_collects_work(curate, store)
        test_curate_batch_cap(curate, store)
    print("\nAll news gateway tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
