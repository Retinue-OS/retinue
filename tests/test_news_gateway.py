#!/usr/bin/env python3
"""Checks for the news endpoints and the curation gate.

Covers the pure logic behind the new surfaces without running an HTTP server:
the gateway's feed/preferences payloads, the score writer's validation, and the
gate that decides whether a Claude session is spawned at all.

    python3 tests/test_news_gateway.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
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
