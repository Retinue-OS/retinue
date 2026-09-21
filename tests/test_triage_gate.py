#!/usr/bin/env python3
"""Focused checks for the credit-free e-mail triage gate.

`scripts/triage-gate.py` decides — for free — whether INBOX mail warrants
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


def _no_backend(*args):
    """Default backend for a freshly loaded gate: refuse to do anything.

    These tests run in a live container, where the real backend reaches a real
    mailbox. Every test is *supposed* to stub either a wrapper or the
    collection function above it, but a stub is attached by name — so renaming
    the function under test silently un-stubs it and the suite starts issuing
    IMAP moves against the owner's INBOX. (This is not hypothetical: it is how
    `unread_inbox` -> `inbox_messages` was caught.) Failing loudly here turns
    that whole class of accident into an ordinary test failure.

    It is installed on `_email_client_rc` because that is the single choke
    point: `_email_client` is a thin wrapper over it, so a call through either
    one lands here. Installing it on the wrapper instead would leave the gate's
    one deliberate rc-level caller — the `answered` confirmation, which needs
    exit code 3 distinguishable from failure — reaching the live mailbox.
    """
    raise AssertionError(
        f"_email_client_rc{args!r} reached the real backend -- the test must "
        "stub gate._email_client_rc, gate._email_client or gate.inbox_messages")


def _fresh(tmp, *, sent_reconcile=False):
    """Load a fresh gate module bound to temp policy/status paths.

    Sent reconciliation is off unless a test asks for it: it is a second,
    independent behaviour with its own tests below, and leaving it armed would
    make every unrelated spawn test depend on a Sent listing it never set up.
    """
    os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(Path(tmp) / "email-whitelist.nt")
    os.environ["CHAMBERS_DIR"] = tmp
    os.environ["TRIAGE_STATE_DIR"] = str(Path(tmp) / "triage")
    os.environ["TRIAGE_SENT_RECONCILE"] = "1" if sent_reconcile else "0"
    gate = _load("triage_gate", REPO_ROOT / "scripts" / "triage-gate.py")
    gate._email_client_rc = _no_backend
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

    def __init__(self, rc=0):
        self.calls = []
        self.due = []
        self.remaining = []
        self.rc = rc

    def __call__(self, mode, messages, due=0, remaining=0):
        self.calls.append((mode, list(messages)))
        self.due.append(due)
        self.remaining.append(remaining)
        return self.rc


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
        gate.inbox_messages = lambda: inbox
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
        gate.inbox_messages = lambda: [
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
        gate.inbox_messages = lambda: []
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
        gate.inbox_messages = lambda: [
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
        gate.inbox_messages = lambda: []
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
        gate.inbox_messages = lambda: [
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
        gate.inbox_messages = lambda: [
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
        gate.inbox_messages = lambda: [
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
        gate.inbox_messages = lambda: [
            {"uid": "1", "from": "bulletin@news.example", "subject": "Weekly",
             "message_id": "<n@news.example>"},
        ]
        gate.spawn = Recorder()
        gate.route(gate.inbox_messages(), "news")
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
        gate.inbox_messages = lambda: inbox
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
        gate.inbox_messages = lambda: [
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
        gate.inbox_messages = lambda: [
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
            gate.inbox_messages = lambda: [
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
        gate.inbox_messages = lambda: [msg]
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
        gate.inbox_messages = lambda: [
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
        gate.inbox_messages = lambda: [
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
    # the whole INBOX listing rather than off the whitelisted subset.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)  # no whitelist at all
        gate.inbox_messages = lambda: [
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


def test_the_payload_is_the_slice_not_the_whole_stack():
    # The session is handed what armed the run and nothing else. A recorded
    # message is settled as far as this run is concerned; the passes that
    # revisit recorded mail run on the draining run, off the status store,
    # not off the payload. Handing the whole stack over would put the
    # session back to enumerating the mailbox, which is what a bounded slice
    # exists to stop.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate.tp.write_if_changed(
            gate.tp.render_email_whitelist(set(), {"*@work.com"}),
            gate.tp.email_whitelist_path(),
        )
        gate.inbox_messages = lambda: [
            {"from": "boss@work.com", "subject": "old", "message_id": "<a@work.com>"},
            {"from": "boss@work.com", "subject": "new", "message_id": "<b@work.com>"},
        ]
        _record(gate, "<a@work.com>", "proposed")
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == 0
        assert len(rec.calls) == 1, "one new message should have armed the gate"
        ids = {m["message_id"] for m in rec.calls[0][1]}
        assert ids == {"<b@work.com>"}, f"recorded mail leaked into the slice: {ids}"
        assert rec.remaining == [0], rec.remaining
    print("PASS test_the_payload_is_the_slice_not_the_whole_stack")


def test_message_without_an_id_always_arms():
    # No Message-ID means no status file can ever exist for it, so it must fall
    # on the side of getting a look rather than being silently skipped.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate.tp.write_if_changed(
            gate.tp.render_email_whitelist(set(), {"*@work.com"}),
            gate.tp.email_whitelist_path(),
        )
        gate.inbox_messages = lambda: [
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
            gate.inbox_messages = lambda: [
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
            gate.inbox_messages = lambda: [
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
        gate.inbox_messages = lambda: [
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
        gate.inbox_messages = lambda: [
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
            gate.inbox_messages = lambda: [
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
        got = gate.inbox_messages()
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
        assert len(gate.inbox_messages()) == 1
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
        got = gate.inbox_messages()
        assert len(got) == 2, f"narrow result should survive a failed re-scan: {got}"
    print("PASS test_a_failed_widened_rescan_keeps_the_narrow_result")


def test_the_prompt_lists_the_whole_slice_and_owns_the_scope():
    # The list is the scope: every message in the slice is listed, the session
    # is told not to enumerate the mailbox for more, and to record each item
    # as it settles it. A partial run is told to leave the whole-picture
    # passes to the draining run; the draining run is told to do them.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        messages = [
            {"from": "a@work.com", "subject": f"s{i}", "message_id": f"<{i}@work.com>"}
            for i in range(12)
        ]
        partial = gate.build_prompt("daily", messages, remaining=30)
        assert partial.count("\n  - ") == 12, "every message in the slice is listed"
        assert "Do not list the INBOX for more" in partial
        assert "the moment its disposition is settled" in partial
        assert "30 more message(s) wait" in partial
        assert "Skip Phase 1's reconciliation passes" in partial
        assert "work from the mailbox" not in partial
        draining = gate.build_prompt("daily", messages)
        assert "This slice drains the backlog" in draining
        assert "Skip Phase 1" not in draining
    print("PASS test_the_prompt_lists_the_whole_slice_and_owns_the_scope")


# --------------------------------------------------------------------------- #
# Bounded slices: a run that finishes beats one that is killed at the wall     #
# --------------------------------------------------------------------------- #


def _dated(i, day):
    return {"from": "boss@work.com", "subject": f"s{i}",
            "message_id": f"<{i}@work.com>", "date": f"2026-09-{day:02d}T10:00:00Z"}


def test_a_run_takes_the_oldest_slice_and_exits_partial():
    # The listing is newest-first; a run that starts from the top spends its
    # budget on what arrived last and cuts what has waited longest. The slice
    # is therefore the *oldest* BATCH_SIZE, and a run that leaves mail behind
    # says so with EXIT_PARTIAL so the scheduler comes back for the rest.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        _whitelist_all(gate)
        gate.BATCH_SIZE = 2
        gate.inbox_messages = lambda: [
            _dated(1, 20), _dated(2, 3), _dated(3, 15), _dated(4, 1), _dated(5, 9),
        ]
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == gate.EXIT_PARTIAL
        assert [m["message_id"] for m in rec.calls[0][1]] == ["<4@work.com>", "<2@work.com>"]
        assert rec.remaining == [3], rec.remaining
    print("PASS test_a_run_takes_the_oldest_slice_and_exits_partial")


def test_the_next_run_continues_where_the_last_left_off():
    # The whole point: what the session recorded stays recorded, so the next
    # slice is the next oldest, and the run that takes the last of it exits 0.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        _whitelist_all(gate)
        gate.BATCH_SIZE = 2
        gate.inbox_messages = lambda: [_dated(1, 3), _dated(2, 1), _dated(3, 2)]
        for mid in ("<2@work.com>", "<3@work.com>"):  # the previous slice, recorded
            _record(gate, mid, "proposed")
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == 0
        assert [m["message_id"] for m in rec.calls[0][1]] == ["<1@work.com>"]
        assert rec.remaining == [0]
    print("PASS test_the_next_run_continues_where_the_last_left_off")


def test_undated_mail_goes_last_in_a_slice():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        _whitelist_all(gate)
        gate.BATCH_SIZE = 2
        undated = {"from": "boss@work.com", "subject": "?", "message_id": "<u@work.com>"}
        gate.inbox_messages = lambda: [undated, _dated(1, 5), _dated(2, 4)]
        rec = Recorder()
        gate.spawn = rec
        assert gate.run_frequent() == gate.EXIT_PARTIAL
        assert [m["message_id"] for m in rec.calls[0][1]] == ["<2@work.com>", "<1@work.com>"]
    print("PASS test_undated_mail_goes_last_in_a_slice")


def test_a_failed_session_is_reported_as_such_not_as_partial():
    # The session's own failure outranks "more to do": the scheduler must see
    # rc=1 and its error text, not a cheerful partial.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        _whitelist_all(gate)
        gate.BATCH_SIZE = 1
        gate.inbox_messages = lambda: [_dated(1, 1), _dated(2, 2)]
        gate.spawn = Recorder(rc=1)
        assert gate.run_frequent() == 1
    print("PASS test_a_failed_session_is_reported_as_such_not_as_partial")


def test_a_due_digest_rides_along_with_the_slice():
    # The bundle is owed on time whatever slice is being worked; it is merged
    # into the payload and does not count against the slice.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        _whitelist_all(gate)
        gate.BATCH_SIZE = 1
        bundled = {"from": "boss@work.com", "subject": "b", "message_id": "<b@work.com>"}
        gate.inbox_messages = lambda: [_dated(1, 2), _dated(2, 1), bundled]
        _record(gate, "<b@work.com>", "omnibus_pending")
        rec = Recorder()
        gate.spawn = rec
        gate.refresh_whitelist_from_sent = lambda: 0
        assert gate.run_daily() == gate.EXIT_PARTIAL
        ids = [m["message_id"] for m in rec.calls[0][1]]
        assert ids == ["<2@work.com>", "<b@work.com>"], ids
        assert rec.due == [1] and rec.remaining == [1]
    print("PASS test_a_due_digest_rides_along_with_the_slice")


def test_the_slice_never_exceeds_what_the_prompt_can_list():
    # A non-positive BATCH_SIZE means "as many as the prompt lists", never
    # "unbounded": the list is the session's scope, so an unlisted message
    # would be one it was told not to go looking for.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        gate.PROMPT_LIST_LIMIT = 3
        fresh = [_dated(i, i) for i in range(1, 8)]
        for size in (0, -1, 50):
            gate.BATCH_SIZE = size
            batch, remaining = gate.take_batch(fresh)
            assert len(batch) == 3 and remaining == 4, (size, len(batch), remaining)
    print("PASS test_the_slice_never_exceeds_what_the_prompt_can_list")


# --------------------------------------------------------------------------- #
# Scan scope: the INBOX, not the unread subset                                 #
# --------------------------------------------------------------------------- #


def _scope_of(gate, **stub):
    """Run inbox_messages() against a recording client; return the search args."""
    calls = []

    def fake_client(*args):
        calls.append(args)
        return {"messages": stub.get("messages", [])}

    gate._email_client = fake_client
    gate.inbox_messages()
    return calls


def test_the_scan_covers_the_inbox_not_just_the_unread():
    # The defect this fixes: scope was keyed on the `unread` flag, so any mail
    # merely *opened* — by the user in a mail client, or by a triage session
    # reading it to classify it — left the gate's view while still sitting in
    # the INBOX, and was never proposed again. Presence is the mailbox's call;
    # handled-state is the status store's.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp)
        calls = _scope_of(gate)
        assert len(calls) == 1
        assert "--unseen" not in calls[0], f"scan still flag-keyed: {calls[0]}"
        assert calls[0][:3] == ("search", "--folder", "INBOX")
    print("PASS test_the_scan_covers_the_inbox_not_just_the_unread")


# --------------------------------------------------------------------------- #
# Sent reconciliation: a mail already answered leaves the INBOX                #
# --------------------------------------------------------------------------- #


def _arm_sent(gate, sent_messages, *, moves_ok=True, answered=0, receipt=True):
    """Stub the backend with a Sent listing; record every call.

    The stub sits on `_email_client_rc`, beneath both wrappers, so the two-step
    flow is covered end to end: the nominating Sent listing goes through
    `_email_client`, the confirming `answered` check goes through the rc form
    directly, and the real wrapper stays in play for the former.

    `answered` is the exit code the confirmation reports — 0 answered, 3
    genuinely unanswered, anything else inconclusive. `receipt` controls
    whether a successful move returns the uid it claims to have moved.
    """
    calls = []

    def fake_rc(*args):
        calls.append(args)
        if args[0] == "search":
            return 0, {"messages": list(sent_messages)}
        if args[0] == "answered":
            return answered, {"answered": answered == 0}
        if args[0] == "move":
            if not moves_ok:
                return 1, None
            uid = args[args.index("--uid") + 1]
            return 0, {"moved": uid if receipt else ""}
        return 0, {"ok": True}

    gate._email_client_rc = fake_rc
    return calls


def test_an_answered_mail_is_archived_and_recorded_resolved():
    # The invariant the owner asked for: after the daily omnibus, the INBOX
    # holds only what is still open. A mail they have replied to is not open,
    # so it must leave — and leave a status record, so nothing re-proposes it.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp, sent_reconcile=True)
        calls = _arm_sent(gate, [
            {"subject": "Re: Meeting Thursday", "to": "Donat <donat@example.org>",
             "date": "2026-09-17T10:12:00+02:00"},
        ])
        left = gate.reconcile_answered([
            {"uid": "41", "from": "Donat <donat@example.org>",
             "subject": "Meeting Thursday", "message_id": "<m1@example.org>",
             "date": "2026-09-16T08:00:00+02:00"},
        ])
        assert left == [], "the answered mail should not remain to triage"
        checks = [c for c in calls if c[0] == "answered"]
        assert len(checks) == 1 and "<m1@example.org>" in checks[0], (
            f"the exact check was not consulted: {checks}")
        moves = [c for c in calls if c[0] == "move"]
        assert moves == [("move", "--uid", "41", "--from", "INBOX",
                          "--to", "Archive")], f"wrong move: {moves}"
        record = json.loads(gate._status_path("<m1@example.org>").read_text())
        assert record["status"] == "resolved"
        assert record["disposition"] == "answered"
        assert record["folder"] == "Archive"
    print("PASS test_an_answered_mail_is_archived_and_recorded_resolved")


def test_a_reply_to_someone_else_does_not_settle_the_mail():
    # Subject alone is far too weak: two people can write about "Invoice 2026"
    # in unrelated threads. Answering one must not archive the other's.
    # The subject match is exactly what the prefilter keys on, so this mail is
    # *nominated* — and the exact check is what refuses it. That division is
    # the design: nomination is allowed to be wrong, the decision is not.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp, sent_reconcile=True)
        calls = _arm_sent(gate, [
            {"subject": "Re: Invoice 2026", "to": "other@elsewhere.com",
             "date": "2026-09-17T10:00:00+00:00"},
        ], answered=3)
        inbox = [{"uid": "7", "from": "billing@vendor.com",
                  "subject": "Invoice 2026", "message_id": "<inv@vendor.com>",
                  "date": "2026-09-16T09:00:00+00:00"}]
        assert gate.reconcile_answered(inbox) == inbox
        assert [c[0] for c in calls if c[0] == "answered"] == ["answered"]
        assert not [c for c in calls if c[0] == "move"], "archived a stranger's mail"
    print("PASS test_a_reply_to_someone_else_does_not_settle_the_mail")


def test_an_inconclusive_answered_check_settles_nothing():
    # Exit 3 is an answer ("nobody replied"); any other non-zero code is the
    # backend failing to say. Unknown must read as *not* answered, or an IMAP
    # hiccup archives mail nobody has touched.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp, sent_reconcile=True)
        calls = _arm_sent(gate, [
            {"subject": "Re: Contract", "to": "legal@work.com",
             "date": "2026-09-17T10:00:00+00:00"},
        ], answered=1)
        inbox = [{"uid": "12", "from": "legal@work.com", "subject": "Contract",
                  "message_id": "<c@work.com>",
                  "date": "2026-09-16T10:00:00+00:00"}]
        assert gate.reconcile_answered(inbox) == inbox
        assert not [c for c in calls if c[0] == "move"]
        assert gate._status_path("<c@work.com>").exists() is False
    print("PASS test_an_inconclusive_answered_check_settles_nothing")


def test_only_nominated_mail_pays_for_an_exact_check():
    # The prefilter's whole reason to exist: the exact check is an IMAP
    # round-trip per message, and the overwhelming majority of an INBOX has
    # never been replied to. Mail with no matching Sent subject must not
    # reach it at all.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp, sent_reconcile=True)
        calls = _arm_sent(gate, [
            {"subject": "Re: Budget", "to": "cfo@work.com",
             "date": "2026-09-17T10:00:00+00:00"},
        ])
        inbox = [
            {"uid": "1", "from": "cfo@work.com", "subject": "Budget",
             "message_id": "<b@work.com>", "date": "2026-09-16T10:00:00+00:00"},
            {"uid": "2", "from": "news@list.com", "subject": "Weekly digest",
             "message_id": "<d@list.com>", "date": "2026-09-16T10:00:00+00:00"},
        ]
        left = gate.reconcile_answered(inbox)
        assert [m["uid"] for m in left] == ["2"]
        checks = [c for c in calls if c[0] == "answered"]
        assert len(checks) == 1, f"the unanswered mail paid for a check: {checks}"
        assert "<b@work.com>" in checks[0]
    print("PASS test_only_nominated_mail_pays_for_an_exact_check")


def test_the_sent_listing_is_bounded_by_the_oldest_inbox_date():
    # A reply postdates the mail it answers, so Sent mail older than the oldest
    # INBOX message can settle nothing: `--since` that date (less a day of
    # clock slack) is a complete window, however much older Sent mail exists.
    # A count bound would be permanently saturated on any mailbox with a year
    # of Sent behind it, and "saturated" would mean an IMAP login per INBOX
    # message on every tick.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp, sent_reconcile=True)
        calls = _arm_sent(gate, [])
        gate.reconcile_answered([
            {"uid": "1", "from": "a@b.com", "subject": "x", "message_id": "<x>",
             "date": "2026-09-16T10:00:00+00:00"},
            {"uid": "2", "from": "a@b.com", "subject": "y", "message_id": "<y>",
             "date": "2026-03-01T00:30:00+02:00"},
            {"uid": "3", "from": "a@b.com", "subject": "z", "message_id": "<z>"},
        ])
        listing = [c for c in calls if c[0] == "search"]
        assert len(listing) == 1, listing
        since = listing[0][listing[0].index("--since") + 1]
        assert since == "28-Feb-2026", f"expected the day before the oldest mail: {since}"
        assert "--limit" in listing[0] and "--unseen" not in listing[0]
    print("PASS test_the_sent_listing_is_bounded_by_the_oldest_inbox_date")


def test_a_capped_listing_exact_checks_only_mail_older_than_its_horizon():
    # The residual cap can bite when one very old mail is still open. Past it,
    # the listing is the newest CAP messages, so a reply to anything older than
    # the oldest one listed may lie beyond it — those messages, and only those,
    # are exact-checked without a nomination. Everything newer still goes
    # through the subject index, so the cap never turns into a login per
    # message.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp, sent_reconcile=True)
        gate.RECONCILE_LISTING_CAP = 2
        calls = _arm_sent(gate, [
            {"subject": "Re: Something else", "to": "a@b.com",
             "date": "2026-09-17T10:00:00+00:00"},
            {"subject": "Re: Another thread", "to": "c@d.com",
             "date": "2026-09-12T11:00:00+00:00"},
        ], answered=3)
        inbox = [
            {"uid": "11", "from": "ops@work.com", "subject": "Renewal",
             "message_id": "<old@work.com>", "date": "2026-06-01T10:00:00+00:00"},
            {"uid": "12", "from": "ops@work.com", "subject": "Renewal",
             "message_id": "<new@work.com>", "date": "2026-09-16T10:00:00+00:00"},
        ]
        assert gate.reconcile_answered(inbox) == inbox
        checks = [c for c in calls if c[0] == "answered"]
        assert len(checks) == 1 and "<old@work.com>" in checks[0], checks
    print("PASS test_a_capped_listing_exact_checks_only_mail_older_than_its_horizon")


def test_a_reply_that_predates_the_mail_does_not_settle_it():
    # The common real shape: a correspondence where the latest word is theirs.
    # An older reply of ours in the same thread does not answer a newer mail.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp, sent_reconcile=True)
        calls = _arm_sent(gate, [
            {"subject": "Re: Offer", "to": "sales@vendor.com",
             "date": "2026-09-10T09:00:00+00:00"},
        ])
        inbox = [{"uid": "8", "from": "sales@vendor.com", "subject": "Re: Offer",
                  "message_id": "<o2@vendor.com>",
                  "date": "2026-09-15T09:00:00+00:00"}]
        assert gate.reconcile_answered(inbox) == inbox
        # Not even nominated, so it never reaches the exact check — which on
        # its own would say "answered", the reply being in the same thread.
        assert not [c for c in calls if c[0] == "answered"]
    print("PASS test_a_reply_that_predates_the_mail_does_not_settle_it")


def test_a_failed_move_leaves_the_mail_in_the_triage_set():
    # Bookkeeping must never outrun the mailbox: if the move did not happen,
    # the mail is still in the INBOX and still has to be proposed.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp, sent_reconcile=True)
        _arm_sent(gate, [
            {"subject": "Re: Thanks", "to": "peer@work.com",
             "date": "2026-09-17T10:00:00+00:00"},
        ], moves_ok=False)
        inbox = [{"uid": "9", "from": "peer@work.com", "subject": "Thanks",
                  "message_id": "<t@work.com>",
                  "date": "2026-09-16T10:00:00+00:00"}]
        assert gate.reconcile_answered(inbox) == inbox
        assert gate._status_path("<t@work.com>").exists() is False
    print("PASS test_a_failed_move_leaves_the_mail_in_the_triage_set")


def test_a_move_without_its_receipt_leaves_the_mail_in_the_triage_set():
    # Exit 0 is not the same claim as "uid 10 is now in Archive". A backend
    # that answers cheerfully without moving anything would otherwise earn a
    # `resolved` record for a mail still sitting in the INBOX — invisible to
    # triage from then on, because the store is what decides handled-state.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp, sent_reconcile=True)
        _arm_sent(gate, [
            {"subject": "Re: Renewal", "to": "ops@work.com",
             "date": "2026-09-17T10:00:00+00:00"},
        ], receipt=False)
        inbox = [{"uid": "10", "from": "ops@work.com", "subject": "Renewal",
                  "message_id": "<r@work.com>",
                  "date": "2026-09-16T10:00:00+00:00"}]
        assert gate.reconcile_answered(inbox) == inbox
        assert gate._status_path("<r@work.com>").exists() is False
    print("PASS test_a_move_without_its_receipt_leaves_the_mail_in_the_triage_set")


def test_reconciliation_can_be_switched_off():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp, sent_reconcile=False)
        inbox = [{"uid": "1", "from": "a@b.com", "subject": "x",
                  "message_id": "<x@b.com>", "date": "2026-09-16T10:00:00+00:00"}]
        # The backend is the raising stub: proof no listing is even fetched.
        assert gate.reconcile_answered(inbox) == inbox
    print("PASS test_reconciliation_can_be_switched_off")


def test_a_backend_failure_during_reconciliation_settles_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp, sent_reconcile=True)
        gate._email_client_rc = lambda *a: (-1, None)  # backend unavailable
        inbox = [{"uid": "1", "from": "a@b.com", "subject": "x",
                  "message_id": "<x@b.com>", "date": "2026-09-16T10:00:00+00:00"}]
        assert gate.reconcile_answered(inbox) == inbox
    print("PASS test_a_backend_failure_during_reconciliation_settles_nothing")


def test_reply_prefixes_of_several_locales_pair_with_their_original():
    for prefix in ("Re:", "AW:", "Fwd:", "WG:", "Antw:", "RE[2]:", "Re: Re:"):
        with tempfile.TemporaryDirectory() as tmp:
            gate = _fresh(tmp, sent_reconcile=True)
            _arm_sent(gate, [
                {"subject": f"{prefix} Budget", "to": "cfo@work.com",
                 "date": "2026-09-17T10:00:00+00:00"},
            ])
            left = gate.reconcile_answered([
                {"uid": "3", "from": "CFO <cfo@work.com>", "subject": "Budget",
                 "message_id": f"<{prefix}@work.com>",
                 "date": "2026-09-16T10:00:00+00:00"},
            ])
            assert left == [], f"{prefix!r} did not pair with its original"
    print("PASS test_reply_prefixes_of_several_locales_pair_with_their_original")


def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing():
    # Backends are inconsistent about offsets; a missing one must not throw.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp, sent_reconcile=True)
        _arm_sent(gate, [
            {"subject": "Re: Ping", "to": "p@q.com", "date": "2026-09-17T10:00:00"},
        ])
        left = gate.reconcile_answered([
            {"uid": "4", "from": "p@q.com", "subject": "Ping",
             "message_id": "<p@q.com>", "date": "2026-09-16T10:00:00"},
        ])
        assert left == []
    print("PASS test_a_naive_timestamp_is_read_as_utc_rather_than_crashing")


def test_an_undated_mail_is_never_settled():
    # No timestamp means the "reply postdates it" test cannot be made; without
    # it a subject match alone would be enough to archive, which it must not be.
    with tempfile.TemporaryDirectory() as tmp:
        gate = _fresh(tmp, sent_reconcile=True)
        _arm_sent(gate, [
            {"subject": "Re: Hi", "to": "z@z.com", "date": "2026-09-17T10:00:00Z"},
        ])
        inbox = [{"uid": "5", "from": "z@z.com", "subject": "Hi",
                  "message_id": "<z@z.com>"}]
        assert gate.reconcile_answered(inbox) == inbox
    print("PASS test_an_undated_mail_is_never_settled")


def test_both_modes_reconcile_before_they_route():
    # The wiring, not the function. Everything above calls reconcile_answered()
    # directly, so a mode that forgot to call it at all would pass every one of
    # those tests and still re-propose answered mail on every sweep — which is
    # precisely the complaint this whole mechanism answers.
    for mode in ("frequent", "daily"):
        with tempfile.TemporaryDirectory() as tmp:
            gate = _fresh(tmp, sent_reconcile=True)
            gate.tp.write_if_changed(
                gate.tp.render_email_whitelist({"donat@example.org"}, set()),
                gate.tp.email_whitelist_path(),
            )
            # daily refreshes from Sent first; that is not what is under test.
            gate.refresh_whitelist_from_sent = lambda: 1
            gate.inbox_messages = lambda: [
                {"uid": "41", "from": "Donat <donat@example.org>",
                 "subject": "Meeting Thursday", "message_id": "<m1@example.org>",
                 "date": "2026-09-16T08:00:00+02:00"},
            ]
            calls = _arm_sent(gate, [
                {"subject": "Re: Meeting Thursday", "to": "donat@example.org",
                 "date": "2026-09-17T10:12:00+02:00"},
            ])
            rec = Recorder()
            gate.spawn = rec
            assert getattr(gate, f"run_{mode}")() == 0
            # A whitelisted sender: without reconciliation this spawns.
            assert rec.calls == [], f"{mode} spawned over an answered mail"
            assert [c for c in calls if c[0] == "move"], f"{mode} did not archive it"
    print("PASS test_both_modes_reconcile_before_they_route")


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
    test_the_payload_is_the_slice_not_the_whole_stack()
    test_message_without_an_id_always_arms()
    test_stalled_non_terminal_mail_re_arms_the_gate()
    test_a_settled_status_never_re_arms_however_old()
    test_a_recent_non_terminal_item_is_left_alone()
    test_an_unreadable_record_re_arms_rather_than_hiding_the_mail()
    test_a_corrupt_record_re_arms_instead_of_crashing_the_tick()
    test_a_saturated_scan_window_widens_instead_of_hiding_old_mail()
    test_an_unsaturated_scan_does_not_pay_for_a_second_listing()
    test_a_failed_widened_rescan_keeps_the_narrow_result()
    test_the_prompt_lists_the_whole_slice_and_owns_the_scope()
    test_a_run_takes_the_oldest_slice_and_exits_partial()
    test_the_next_run_continues_where_the_last_left_off()
    test_undated_mail_goes_last_in_a_slice()
    test_a_failed_session_is_reported_as_such_not_as_partial()
    test_a_due_digest_rides_along_with_the_slice()
    test_the_slice_never_exceeds_what_the_prompt_can_list()
    test_the_scan_covers_the_inbox_not_just_the_unread()
    test_an_answered_mail_is_archived_and_recorded_resolved()
    test_a_reply_to_someone_else_does_not_settle_the_mail()
    test_an_inconclusive_answered_check_settles_nothing()
    test_only_nominated_mail_pays_for_an_exact_check()
    test_the_sent_listing_is_bounded_by_the_oldest_inbox_date()
    test_a_capped_listing_exact_checks_only_mail_older_than_its_horizon()
    test_a_reply_that_predates_the_mail_does_not_settle_it()
    test_a_failed_move_leaves_the_mail_in_the_triage_set()
    test_a_move_without_its_receipt_leaves_the_mail_in_the_triage_set()
    test_reconciliation_can_be_switched_off()
    test_a_backend_failure_during_reconciliation_settles_nothing()
    test_reply_prefixes_of_several_locales_pair_with_their_original()
    test_a_naive_timestamp_is_read_as_utc_rather_than_crashing()
    test_an_undated_mail_is_never_settled()
    test_both_modes_reconcile_before_they_route()
    print("all triage-gate tests passed")
