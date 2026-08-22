#!/usr/bin/env python3
"""Focused checks for the credit-free e-mail triage gate.

`scripts/triage-gate.py` decides — for free — whether unread INBOX mail warrants
a `claude -p` triage spawn: in `frequent` mode only for whitelisted senders, in
`daily` mode for any sender (after refreshing the whitelist from Sent). Before
either decides, the news rail diverts mail from declared news senders into the
feed. This exercises the spawn/no-spawn decision, sender filtering, Sent-folder
whitelist refresh, and the news rail (filing, inbox hygiene, status file, and
the guarantee that a newsletter never buys a model turn), with the IMAP backend,
the news ingest and the model spawn all mocked so no network or credits are
touched.

Standalone, no third-party deps:

    python3 tests/test_triage_gate.py
"""
import importlib.util
import json
import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fresh(tmp):
    """Load a fresh gate module bound to temp policy/status paths."""
    os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(Path(tmp) / "email-whitelist.nt")
    os.environ["CHAMBERS_DIR"] = tmp
    os.environ["TRIAGE_STATE_DIR"] = str(Path(tmp) / "triage")
    gate = _load("triage_gate", REPO_ROOT / "scripts" / "triage-gate.py")
    return gate


class NewsRail:
    """Stand-in for news_ingest.forward_news(); records what was filed."""

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.ok


def _arm_news(gate, tmp, entries, *, ok=True, detail=None, moves=None,
              ignore=True):
    """Declare `entries` as a news group and mock the rail's dependencies.

    `ignore` also flags them `ignored`, which is the read-only newsletter case:
    feed yes, triage never. Pass ignore=False for a list that is read *and*
    answered, where the feed must not consume the mail.

    Returns (rail, calls) where `calls` records every _email_client invocation,
    so a test can assert on the flag/move hygiene as well as the filing.
    """
    gate.tp._mutate_email(news_add=entries,
                          ignore_add=entries if ignore else ())
    rail = NewsRail(ok=ok)
    gate.news_ingest.forward_news = rail
    gate.news_ingest.news_enabled = lambda: True
    calls = []

    def fake_client(*args):
        calls.append(args)
        if args[0] == "read":
            return dict(detail or {})
        if args[0] == "move":
            return None if moves == "fail" else {"ok": True}
        return {"ok": True}

    gate._email_client = fake_client
    return rail, calls


class Recorder:
    """Stand-in for spawn(); records calls instead of launching claude."""

    def __init__(self):
        self.calls = []

    def __call__(self, mode, messages):
        self.calls.append((mode, list(messages)))
        return 0


def test_frequent_spawns_only_for_whitelisted():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        # Whitelist one exact address and one domain wildcard.
        gate.tp.write_if_changed(
            gate.tp.render_email_whitelist({"boss@work.com"}, {"*@factsmission.com"}),
            gate.tp.email_whitelist_path(),
        )
        inbox = [
            {"from": "Boss <boss@work.com>", "subject": "hi", "message_id": "<1>"},
            {"from": "spam@random.io", "subject": "sale", "message_id": "<2>"},
            {"from": "Reto <reto@factsmission.com>", "subject": "re", "message_id": "<3>"},
        ]
        gate.unread_inbox = lambda: inbox
        rec = Recorder()
        gate.spawn = rec
        rc = gate.run_frequent()
        assert rc == 0
        assert len(rec.calls) == 1, "expected exactly one spawn"
        mode, msgs = rec.calls[0]
        assert mode == "frequent"
        ids = {m["message_id"] for m in msgs}
        assert ids == {"<1>", "<3>"}, f"wrong messages spawned: {ids}"
    print("PASS test_frequent_spawns_only_for_whitelisted")


def test_frequent_no_whitelisted_no_spawn():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        # Empty whitelist → nothing is trusted.
        gate.unread_inbox = lambda: [
            {"from": "a@x.com", "subject": "s", "message_id": "<9>"}
        ]
        rec = Recorder()
        gate.spawn = rec
        rc = gate.run_frequent()
        assert rc == 0
        assert rec.calls == [], "spawned despite no whitelisted sender"
    print("PASS test_frequent_no_whitelisted_no_spawn")


def test_frequent_empty_inbox_no_spawn():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate.unread_inbox = lambda: []
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == 0
        assert rec.calls == []
    print("PASS test_frequent_empty_inbox_no_spawn")


def test_daily_spawns_for_any_sender_and_refreshes():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        refreshed = {"n": 0}

        def fake_refresh():
            # Simulate deriving one address from Sent into the whitelist.
            addrs, wilds = gate.tp.load_email_whitelist()
            addrs |= {"someone@sent.com"}
            gate.tp.write_if_changed(
                gate.tp.render_email_whitelist(addrs, wilds),
                gate.tp.email_whitelist_path(),
            )
            refreshed["n"] += 1
            return len(addrs)

        gate.refresh_whitelist_from_sent = fake_refresh
        gate.unread_inbox = lambda: [
            {"from": "stranger@nowhere.com", "subject": "hello", "message_id": "<7>"}
        ]
        rec = Recorder()
        gate.spawn = rec
        rc = gate.run_daily()
        assert rc == 0
        assert refreshed["n"] == 1, "daily did not refresh the whitelist"
        assert len(rec.calls) == 1 and rec.calls[0][0] == "daily"
        # Even a non-whitelisted sender is triaged by the daily catch-all.
        assert rec.calls[0][1][0]["message_id"] == "<7>"
    print("PASS test_daily_spawns_for_any_sender_and_refreshes")


def test_daily_empty_inbox_no_spawn():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate.refresh_whitelist_from_sent = lambda: 0
        gate.unread_inbox = lambda: []
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_daily() == 0
        assert rec.calls == []
    print("PASS test_daily_empty_inbox_no_spawn")


def test_refresh_derives_addresses_only():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        # Pre-seed a hand-added wildcard; refresh must preserve it and add only
        # exact addresses from Sent — never a domain.
        gate.tp.write_if_changed(
            gate.tp.render_email_whitelist(set(), {"*@trusted.org"}),
            gate.tp.email_whitelist_path(),
        )
        gate._email_client = lambda *a: {
            "messages": [
                {"to": "Client <client@gmail.com>"},
                {"to": "peer@partner.com"},
            ]
        }
        n = gate.refresh_whitelist_from_sent()
        assert n == 2
        addrs, wilds = gate.tp.load_email_whitelist()
        assert addrs == {"client@gmail.com", "peer@partner.com"}
        assert wilds == {"*@trusted.org"}, "hand-added wildcard was lost"
        # Crucially: emailing one gmail address did NOT whitelist all of gmail.com.
        assert not gate.tp.email_whitelisted("other@gmail.com", addrs, wilds)
    print("PASS test_refresh_derives_addresses_only")


def test_refresh_backend_down_returns_minus_one():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate._email_client = lambda *a: None  # backend unavailable
        assert gate.refresh_whitelist_from_sent() == -1
    print("PASS test_refresh_backend_down_returns_minus_one")


def test_news_sender_is_filed_and_never_spawns():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        rail, calls = _arm_news(
            gate, tmp, ["*@newsletter.example"],
            detail={
                "subject": "Herbstprogramm 2026",
                "from": "MS-Gesellschaft <info@newsletter.example>",
                "message_id": "<news-1@newsletter.example>",
                "body": "Kurse und Veranstaltungen.\n\n\nJetzt anmelden.",
                "archived_at": "<https://example.org/archive/42>",
            },
        )
        gate.unread_inbox = lambda: [
            {"uid": "10", "from": "info@newsletter.example",
             "subject": "Herbstprogramm 2026", "message_id": "<news-1@newsletter.example>"},
        ]
        rec = Recorder()
        gate.spawn = rec

        gate.refresh_whitelist_from_sent = lambda: 0
        assert gate.run_daily() == 0
        # A newsletter is not correspondence: it must never buy a model turn,
        # not even on the daily catch-all where every other sender does.
        assert rec.calls == [], "a news sender spawned a triage session"

        assert len(rail.calls) == 1, "newsletter was not filed to the feed"
        item = rail.calls[0]
        assert item["channel"] == "email"
        assert item["title"] == "Herbstprogramm 2026"
        assert item["source_id"] == "email:info@newsletter.example"
        # The declared web version (RFC 5064), not a link guessed from the body.
        assert item["url"] == "https://example.org/archive/42"
        assert item["text"].startswith("Herbstprogramm 2026\n\n")
        assert "\n\n\n" not in item["text"], "blank-line runs not collapsed"

        # Inbox hygiene: marked read, then moved out — in that order.
        verbs = [c[0] for c in calls]
        assert verbs == ["read", "flag", "move"], verbs
        assert calls[2][-1] == "Archive"

        # And the status store knows, terminally, so triage never re-proposes it.
        status = Path(tmp) / "triage" / "news-1@newsletter.example"
        rec_json = json.loads(status.read_text())
        assert rec_json["status"] == "resolved"
        assert rec_json["disposition"] == "news"
        assert rec_json["folder"] == "Archive"
    print("PASS test_news_sender_is_filed_and_never_spawns")


def test_news_rail_leaves_other_mail_to_triage():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        rail, _ = _arm_news(
            gate, tmp, ["bulletin@news.example"],
            detail={"subject": "Weekly", "from": "bulletin@news.example",
                    "message_id": "<n@news.example>", "body": "text"},
        )
        gate.tp._mutate_email(add_addresses=["boss@work.com"])
        gate.unread_inbox = lambda: [
            {"uid": "1", "from": "bulletin@news.example", "subject": "Weekly",
             "message_id": "<n@news.example>"},
            {"uid": "2", "from": "Boss <boss@work.com>", "subject": "hi",
             "message_id": "<b@work.com>"},
        ]
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == 0
        assert len(rail.calls) == 1
        assert len(rec.calls) == 1
        ids = {m["message_id"] for m in rec.calls[0][1]}
        assert ids == {"<b@work.com>"}, f"newsletter leaked into triage: {ids}"
    print("PASS test_news_rail_leaves_other_mail_to_triage")


def test_news_filing_failure_falls_back_to_triage():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        rail, calls = _arm_news(
            gate, tmp, ["*@news.example"], ok=False,
            detail={"subject": "Weekly", "from": "bulletin@news.example",
                    "message_id": "<n@news.example>", "body": "text"},
        )
        gate.refresh_whitelist_from_sent = lambda: 0
        gate.unread_inbox = lambda: [
            {"uid": "1", "from": "bulletin@news.example", "subject": "Weekly",
             "message_id": "<n@news.example>"},
        ]
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_daily() == 0
        # The feed rejected it, so the mail is untouched and a model turn gets
        # the chance to deal with it — never silently swallowed.
        assert len(rec.calls) == 1
        assert "flag" not in [c[0] for c in calls]
        assert not (Path(tmp) / "triage").exists()
    print("PASS test_news_filing_failure_falls_back_to_triage")


def test_news_move_failure_stays_non_terminal():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        rail, calls = _arm_news(
            gate, tmp, ["bulletin@news.example"], moves="fail",
            detail={"subject": "Weekly", "from": "bulletin@news.example",
                    "message_id": "<n@news.example>", "body": "text"},
        )
        gate.unread_inbox = lambda: [
            {"uid": "1", "from": "bulletin@news.example", "subject": "Weekly",
             "message_id": "<n@news.example>"},
        ]
        gate.spawn = Recorder()
        gate.route(gate.unread_inbox(), "news")
        assert len(rail.calls) == 1, "item should still reach the feed"
        # Filed but still in the INBOX → not terminal, or Phase 1 would never
        # repair the missing move.
        rec_json = json.loads((Path(tmp) / "triage" / "n@news.example").read_text())
        assert rec_json["status"] == "deferred"
        assert "folder" not in rec_json
    print("PASS test_news_move_failure_stays_non_terminal")


def test_refresh_from_sent_preserves_news_senders():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate.tp._mutate_email(news_add=["*@substack.com", "bulletin@news.example"],
                              ignore_add=["*@substack.com"],
                              quiet_add=["list.example.org"])
        gate._email_client = lambda *a: {"messages": [{"to": "peer@partner.com"}]}
        assert gate.refresh_whitelist_from_sent() == 1
        pol = gate.tp.load_email_policy()
        assert pol.addresses == {"peer@partner.com"}
        # Whitelist, news and group flags share one file: a whitelist write that
        # rendered only its own half would erase all of these.
        assert pol.news == {"bulletin@news.example"}
        assert pol.news_wildcards == {"*@substack.com"}
        assert pol.ignored_wildcards == {"*@substack.com"}
        assert pol.quieted == {"list.example.org"}
    print("PASS test_refresh_from_sent_preserves_news_senders")


def test_news_and_quieted_is_filed_but_left_for_triage():
    """A list one reads *and* answers: feed yes, mailbox untouched."""
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        rail, calls = _arm_news(
            gate, tmp, ["discuss.example.org"], ignore=False,
            detail={"subject": "Re: agenda", "from": "peer@example.org",
                    "message_id": "<d1@example.org>", "body": "text"},
        )
        gate.tp._mutate_email(quiet_add=["discuss.example.org"])
        inbox = [{"uid": "1", "from": "peer@example.org", "subject": "Re: agenda",
                  "message_id": "<d1@example.org>",
                  "list_id": "Discuss <discuss.example.org>"}]
        gate.unread_inbox = lambda: inbox
        rec = Recorder()
        gate.spawn = rec

        # Frequent run: the sender is not whitelisted, so no spawn — but the
        # feed gets it right away rather than waiting for the daily sweep.
        assert gate.run_frequent() == 0
        assert rec.calls == []
        assert len(rail.calls) == 1
        # Nothing was consumed: no flag, no move, no status record — triage
        # still owes this mail a look.
        assert [c[0] for c in calls] == ["read"], calls
        assert not (Path(tmp) / "triage").exists()

        # Daily run: now it does reach triage, still exactly once in the feed
        # per tick (the store dedups by item id, so re-filing is a no-op).
        gate.refresh_whitelist_from_sent = lambda: 0
        assert gate.run_daily() == 0
        assert len(rec.calls) == 1
        assert {m["message_id"] for m in rec.calls[0][1]} == {"<d1@example.org>"}
    print("PASS test_news_and_quieted_is_filed_but_left_for_triage")


def test_wildcard_covers_the_lists_under_a_platform_domain():
    """`*@substack.com` has to catch per-publication lists, not just the address."""
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        rail, calls = _arm_news(
            gate, tmp, ["*@substack.com"],
            detail={"subject": "Weekly Letter",
                    "from": "Author <author@substack.com>",
                    "message_id": "<s1@substack.com>", "body": "essay"},
        )
        gate.unread_inbox = lambda: [
            # The sender address is the publication's, not the platform's; only
            # the List-Id ties it to substack.com.
            {"uid": "1", "from": "Author <author@sgcarney.substack.com>",
             "subject": "Weekly Letter", "message_id": "<s1@substack.com>",
             "list_id": "<sgcarney.substack.com>"},
        ]
        gate.refresh_whitelist_from_sent = lambda: 0
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_daily() == 0
        assert len(rail.calls) == 1, "per-publication list missed by the wildcard"
        assert rec.calls == [], "a read-only newsletter bought a model turn"
    print("PASS test_wildcard_covers_the_lists_under_a_platform_domain")


def test_whitelisted_sender_beats_an_ignored_list():
    """Sender and list are orthogonal: a colleague writing to a muted list is
    still correspondence."""
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate.tp._mutate_email(add_addresses=["boss@work.com"],
                              ignore_add=["announce.example.org"])
        gate._email_client = lambda *a: {"ok": True}
        gate.news_ingest.news_enabled = lambda: False
        gate.unread_inbox = lambda: [
            {"uid": "1", "from": "Boss <boss@work.com>", "subject": "read this",
             "message_id": "<b1@work.com>",
             "list_id": "<announce.example.org>"},
            {"uid": "2", "from": "someone@else.org", "subject": "fyi",
             "message_id": "<x1@else.org>",
             "list_id": "<announce.example.org>"},
        ]
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == 0
        assert {m["message_id"] for m in rec.calls[0][1]} == {"<b1@work.com>"}
        # And the unknown sender on that list stays out of triage entirely, even
        # on the catch-all run.
        rec.calls.clear()
        gate.refresh_whitelist_from_sent = lambda: 0
        assert gate.run_daily() == 0
        assert {m["message_id"] for m in rec.calls[0][1]} == {"<b1@work.com>"}
    print("PASS test_whitelisted_sender_beats_an_ignored_list")


if __name__ == "__main__":
    test_frequent_spawns_only_for_whitelisted()
    test_frequent_no_whitelisted_no_spawn()
    test_frequent_empty_inbox_no_spawn()
    test_daily_spawns_for_any_sender_and_refreshes()
    test_daily_empty_inbox_no_spawn()
    test_refresh_derives_addresses_only()
    test_refresh_backend_down_returns_minus_one()
    test_news_sender_is_filed_and_never_spawns()
    test_news_rail_leaves_other_mail_to_triage()
    test_news_filing_failure_falls_back_to_triage()
    test_news_move_failure_stays_non_terminal()
    test_refresh_from_sent_preserves_news_senders()
    test_news_and_quieted_is_filed_but_left_for_triage()
    test_wildcard_covers_the_lists_under_a_platform_domain()
    test_whitelisted_sender_beats_an_ignored_list()
    print("all triage-gate tests passed")
