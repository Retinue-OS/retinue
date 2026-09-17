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
from datetime import datetime, timedelta, timezone
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
        self.due = []

    def __call__(self, mode, messages, due=0):
        self.calls.append((mode, list(messages)))
        self.due.append(due)
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


def _record(gate, message_id, status="omnibus_pending"):
    """Write a triage status file for `message_id`, as a triage run would."""
    path = gate._status_path(message_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": status, "message_id": message_id}))


def _omnibus_sent(gate, ago_seconds=0):
    """Stamp the marker the skill writes when a digest goes out."""
    stamp = datetime.now(timezone.utc) - timedelta(seconds=ago_seconds)
    path = gate.TRIAGE_STATE_DIR / ".last-omnibus"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stamp.strftime("%Y-%m-%dT%H:%M:%SZ"))


def test_recorded_mail_does_not_arm_the_gate():
    # Triage never marks mail read, so everything it classified stays unread in
    # the INBOX until its disposition is executed. Without this, every tick
    # re-spawns a session over the same settled stack.
    for status in ("proposed", "engaged", "omnibus_pending", "resolved"):
        with tempfile.TemporaryDirectory() as tmp:
            gate = _fresh(tmp)
            gate.tp.write_if_changed(
                gate.tp.render_email_whitelist(set(), {"*@work.com"}),
                gate.tp.email_whitelist_path(),
            )
            gate.unread_inbox = lambda: [
                {"from": "boss@work.com", "subject": "s", "message_id": "<a@work.com>"}
            ]
            _record(gate, "<a@work.com>", status)
            # A bundle whose digest went out moments ago is in progress, not
            # owed — the due path below is what covers the other case.
            _omnibus_sent(gate)
            rec = Recorder()
            gate.spawn = rec
            assert gate.run_frequent() == 0
            assert rec.calls == [], f"spawned over a message already {status}"
            gate.refresh_whitelist_from_sent = lambda: 0
            assert gate.run_daily() == 0
            assert rec.calls == [], f"daily spawned over a message already {status}"
    print("PASS test_recorded_mail_does_not_arm_the_gate")


def test_a_due_omnibus_digest_arms_the_gate_on_its_own():
    # The accrual deadlock: `omnibus_pending` is an open status, so the mail on
    # it does not arm the gate — and the gate is the only thing that spawns a
    # triage session. Without this path a bundle goes out only when unrelated
    # new mail happens to arm a run, or days later via the stall backstop.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        _whitelist_all(gate)
        msg = {"from": "boss@work.com", "subject": "s", "message_id": "<a@work.com>"}
        gate.unread_inbox = lambda: [msg]
        _record(gate, "<a@work.com>", "omnibus_pending")
        _omnibus_sent(gate, ago_seconds=gate.OMNIBUS_INTERVAL + 60)
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == 0
        assert len(rec.calls) == 1, "a due omnibus digest must arm the gate"
        assert rec.due == [1], f"the spawn was not told the digest is due: {rec.due}"
        ids = {m["message_id"] for m in rec.calls[0][1]}
        assert ids == {"<a@work.com>"}, f"the bundled mail was not handed over: {ids}"
    print("PASS test_a_due_omnibus_digest_arms_the_gate_on_its_own")


def test_a_pending_omnibus_stays_quiet_inside_its_interval():
    # The other half of the bargain: accrual is the whole point of the omnibus,
    # so a bundle whose digest is not yet due must not buy a model turn. This
    # is what keeps the user from being pinged several times a day.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        _whitelist_all(gate)
        gate.unread_inbox = lambda: [
            {"from": "boss@work.com", "subject": "s", "message_id": "<a@work.com>"}
        ]
        _record(gate, "<a@work.com>", "omnibus_pending")
        _omnibus_sent(gate, ago_seconds=gate.OMNIBUS_INTERVAL / 2)
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == 0
        assert rec.calls == [], "a bundle inside its interval must stay quiet"
    print("PASS test_a_pending_omnibus_stays_quiet_inside_its_interval")


def test_a_missing_omnibus_marker_counts_as_due():
    # No marker means no digest on record. Erring towards "due" costs one
    # digest; erring the other way leaves bundled mail unseen indefinitely.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        _whitelist_all(gate)
        gate.unread_inbox = lambda: [
            {"from": "boss@work.com", "subject": "s", "message_id": "<a@work.com>"}
        ]
        _record(gate, "<a@work.com>", "omnibus_pending")
        assert not (gate.TRIAGE_STATE_DIR / ".last-omnibus").exists()
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == 0
        assert len(rec.calls) == 1, "a bundle with no marker must arm the gate"
    print("PASS test_a_missing_omnibus_marker_counts_as_due")


def test_a_due_digest_arms_even_when_its_sender_is_not_whitelisted():
    # A bundle accrued by a daily run holds mail from senders the frequent pass
    # does not trust. The digest is still owed on time, so due-ness is read off
    # the whole unread listing rather than off the whitelisted subset.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)  # no whitelist at all
        gate.unread_inbox = lambda: [
            {"from": "stranger@random.io", "subject": "s", "message_id": "<a@x.io>"}
        ]
        _record(gate, "<a@x.io>", "omnibus_pending")
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == 0
        assert len(rec.calls) == 1, "a due digest must arm regardless of sender"
        assert rec.due == [1]
        ids = {m["message_id"] for m in rec.calls[0][1]}
        assert ids == {"<a@x.io>"}, f"the bundled mail was not handed over: {ids}"
    print("PASS test_a_due_digest_arms_even_when_its_sender_is_not_whitelisted")


def test_the_prompt_tells_a_due_run_to_send_the_digest():
    # A run armed only by a due digest has to say so, or the session reads it as
    # an ordinary collect-and-propose run and the bundle accrues another cycle.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        messages = [{"from": "a@b.c", "subject": "s", "message_id": "<1>"}]
        plain = gate.build_prompt("frequent", messages)
        due = gate.build_prompt("frequent", messages, 1)
        assert "omnibus_pending" not in plain, "the plain prompt should not mention it"
        assert "omnibus_pending" in due and "Phase 4b" in due, (
            "the due prompt must name the pending bundle and the phase that sends it"
        )
    print("PASS test_the_prompt_tells_a_due_run_to_send_the_digest")


def test_new_mail_arms_and_the_payload_still_carries_the_recorded_ones():
    # Narrowing what *arms* the gate must not narrow what the session sees:
    # reconciliation and Phase 5 still need the whole routed set.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate.tp.write_if_changed(
            gate.tp.render_email_whitelist(set(), {"*@work.com"}),
            gate.tp.email_whitelist_path(),
        )
        gate.unread_inbox = lambda: [
            {"from": "boss@work.com", "subject": "old", "message_id": "<a@work.com>"},
            {"from": "boss@work.com", "subject": "new", "message_id": "<b@work.com>"},
        ]
        _record(gate, "<a@work.com>", "proposed")
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == 0
        assert len(rec.calls) == 1, "one new message should have armed the gate"
        ids = {m["message_id"] for m in rec.calls[0][1]}
        assert ids == {"<a@work.com>", "<b@work.com>"}, f"payload narrowed: {ids}"
    print("PASS test_new_mail_arms_and_the_payload_still_carries_the_recorded_ones")


def test_message_without_an_id_always_arms():
    # No Message-ID means no status file can ever exist for it, so it must fall
    # on the side of getting a look rather than being silently skipped.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate.tp.write_if_changed(
            gate.tp.render_email_whitelist(set(), {"*@work.com"}),
            gate.tp.email_whitelist_path(),
        )
        gate.unread_inbox = lambda: [
            {"from": "boss@work.com", "subject": "s", "message_id": ""}
        ]
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == 0
        assert len(rec.calls) == 1, "an id-less message must still arm the gate"
    print("PASS test_message_without_an_id_always_arms")


def _record_at(gate, message_id, status, stamp):
    """A status record whose newest timestamp is `stamp` (ISO-8601, UTC)."""
    path = gate._status_path(message_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"status": status, "message_id": message_id, "proposed": stamp}))


def _whitelist_all(gate):
    gate.tp.write_if_changed(
        gate.tp.render_email_whitelist(set(), {"*@work.com"}),
        gate.tp.email_whitelist_path(),
    )


def test_stalled_non_terminal_mail_re_arms_the_gate():
    # The inbox-zero stall: a proposal whose thread the user archived without
    # deciding keeps a non-terminal status forever, and the old "any record at
    # all settles it" rule meant nothing ever looked at that mail again. After
    # TRIAGE_STALL_DAYS an unfinished item is abandoned, not in progress.
    for status in ("proposed", "omnibus", "omnibus_pending", "deferred", "engaged"):
        with tempfile.TemporaryDirectory() as tmp:
            gate = _fresh(tmp)
            _whitelist_all(gate)
            gate.unread_inbox = lambda: [
                {"from": "boss@work.com", "subject": "s", "message_id": "<a@work.com>"}
            ]
            _record_at(gate, "<a@work.com>", status, "2020-01-01T00:00:00Z")
            rec = Recorder()
            gate.spawn = rec
            assert gate.run_frequent() == 0
            assert len(rec.calls) == 1, f"a stalled {status} item must re-arm"
    print("PASS test_stalled_non_terminal_mail_re_arms_the_gate")


def test_a_settled_status_never_re_arms_however_old():
    # The flip side, and the reason this is an allowlist of *unfinished* states:
    # `resolved` mail, and mail owned by another rail, must stay quiet forever.
    # Otherwise every old record in the store buys a model turn on every tick.
    for status in ("resolved", "status_filed", "self_filed", "abstain"):
        with tempfile.TemporaryDirectory() as tmp:
            gate = _fresh(tmp)
            _whitelist_all(gate)
            gate.unread_inbox = lambda: [
                {"from": "boss@work.com", "subject": "s", "message_id": "<a@work.com>"}
            ]
            _record_at(gate, "<a@work.com>", status, "2020-01-01T00:00:00Z")
            rec = Recorder()
            gate.spawn = rec
            assert gate.run_frequent() == 0
            assert rec.calls == [], f"an ancient {status} item must not re-arm"
    print("PASS test_a_settled_status_never_re_arms_however_old")


def test_a_recent_non_terminal_item_is_left_alone():
    # Re-arming is a backstop, not an override of Phase 5: an item that is
    # genuinely waiting on the user, and being nudged, must not be re-collected.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        _whitelist_all(gate)
        gate.unread_inbox = lambda: [
            {"from": "boss@work.com", "subject": "s", "message_id": "<a@work.com>"}
        ]
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _record_at(gate, "<a@work.com>", "proposed", now)
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == 0
        assert rec.calls == [], "a fresh proposal must not re-arm the gate"
    print("PASS test_a_recent_non_terminal_item_is_left_alone")


def test_an_unreadable_record_re_arms_rather_than_hiding_the_mail():
    # Records have been written brace-less in the past; a record nobody can
    # parse must fall on the side of getting a look, since the alternative is
    # mail that is invisible to every future run.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        _whitelist_all(gate)
        gate.unread_inbox = lambda: [
            {"from": "boss@work.com", "subject": "s", "message_id": "<a@work.com>"}
        ]
        path = gate._status_path("<a@work.com>")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"status": "omnibus",\n"disposition": "archive",\n')
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == 0
        assert len(rec.calls) == 1, "an unparseable record must re-arm the gate"
    print("PASS test_an_unreadable_record_re_arms_rather_than_hiding_the_mail")


def test_a_corrupt_record_re_arms_instead_of_crashing_the_tick():
    # Fail open for every shape of corruption, not just invalid JSON: a
    # non-UTF-8 file and a record whose `status` is not a string must re-arm,
    # never raise out of the gate and abort the whole tick.
    for body in (b"\xff\xfe not utf-8", b'{"status": 123}'):
        with tempfile.TemporaryDirectory() as tmp:
            gate = _fresh(tmp)
            _whitelist_all(gate)
            gate.unread_inbox = lambda: [
                {"from": "boss@work.com", "subject": "s", "message_id": "<a@work.com>"}
            ]
            path = gate._status_path("<a@work.com>")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            rec = Recorder()
            gate.spawn = rec
            assert gate.run_frequent() == 0
            assert len(rec.calls) == 1, f"a corrupt record must re-arm: {body!r}"
    print("PASS test_a_corrupt_record_re_arms_instead_of_crashing_the_tick")


def test_a_saturated_scan_window_widens_instead_of_hiding_old_mail():
    # The listing is newest-first, so stopping at the limit hides the *oldest*
    # unread mail — permanently, and precisely once the backlog is big enough to
    # matter. Saturation must trigger a widened re-scan.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate.INBOX_SCAN_LIMIT = 3
        gate.INBOX_SCAN_MAX = 10
        everything = [{"message_id": f"<{i}@work.com>"} for i in range(7)]
        limits = []

        def fake_client(*args):
            limit = int(args[args.index("--limit") + 1])
            limits.append(limit)
            return {"messages": everything[:limit]}

        gate._email_client = fake_client
        got = gate.unread_inbox()
        assert limits == [3, 10], f"expected a widened re-scan, got limits {limits}"
        assert len(got) == 7, f"widened scan returned {len(got)} of 7"
    print("PASS test_a_saturated_scan_window_widens_instead_of_hiding_old_mail")


def test_an_unsaturated_scan_does_not_pay_for_a_second_listing():
    # The common case is a near-empty INBOX; it must stay one round trip.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate.INBOX_SCAN_LIMIT = 3
        limits = []

        def fake_client(*args):
            limits.append(int(args[args.index("--limit") + 1]))
            return {"messages": [{"message_id": "<a@work.com>"}]}

        gate._email_client = fake_client
        assert len(gate.unread_inbox()) == 1
        assert limits == [3], f"unsaturated scan should list once, got {limits}"
    print("PASS test_an_unsaturated_scan_does_not_pay_for_a_second_listing")


def test_a_failed_widened_rescan_keeps_the_narrow_result():
    # Triaging the newest INBOX_SCAN_LIMIT beats triaging nothing when the
    # second listing fails.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate.INBOX_SCAN_LIMIT = 2
        calls = []

        def fake_client(*args):
            calls.append(args)
            if len(calls) == 1:
                return {"messages": [{"message_id": "<a@x>"}, {"message_id": "<b@x>"}]}
            return None

        gate._email_client = fake_client
        got = gate.unread_inbox()
        assert len(got) == 2, f"narrow result should survive a failed re-scan: {got}"
    print("PASS test_a_failed_widened_rescan_keeps_the_narrow_result")


def test_the_prompt_listing_is_capped_and_says_so():
    # A long backlog must cost a truncated listing, never unseen mail: the
    # prompt has to admit the truncation so the session works from the mailbox.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate.PROMPT_LIST_LIMIT = 5
        messages = [
            {"from": "a@work.com", "subject": f"s{i}", "message_id": f"<{i}@work.com>"}
            for i in range(12)
        ]
        prompt = gate.build_prompt("daily", messages)
        assert prompt.count("\n  - ") == 5, "listing should stop at the cap"
        assert "and 7 more" in prompt, "truncation must be stated"
        assert "work from the mailbox" in prompt
    print("PASS test_the_prompt_listing_is_capped_and_says_so")


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
    test_recorded_mail_does_not_arm_the_gate()
    test_a_due_omnibus_digest_arms_the_gate_on_its_own()
    test_a_pending_omnibus_stays_quiet_inside_its_interval()
    test_a_missing_omnibus_marker_counts_as_due()
    test_a_due_digest_arms_even_when_its_sender_is_not_whitelisted()
    test_the_prompt_tells_a_due_run_to_send_the_digest()
    test_new_mail_arms_and_the_payload_still_carries_the_recorded_ones()
    test_message_without_an_id_always_arms()
    test_stalled_non_terminal_mail_re_arms_the_gate()
    test_a_settled_status_never_re_arms_however_old()
    test_a_recent_non_terminal_item_is_left_alone()
    test_an_unreadable_record_re_arms_rather_than_hiding_the_mail()
    test_a_corrupt_record_re_arms_instead_of_crashing_the_tick()
    test_a_saturated_scan_window_widens_instead_of_hiding_old_mail()
    test_an_unsaturated_scan_does_not_pay_for_a_second_listing()
    test_a_failed_widened_rescan_keeps_the_narrow_result()
    test_the_prompt_listing_is_capped_and_says_so()
    print("all triage-gate tests passed")
