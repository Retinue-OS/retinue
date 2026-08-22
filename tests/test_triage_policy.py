#!/usr/bin/env python3
"""Focused checks for the triage delivery-gate policy library.

`scripts/triage_policy.py` is the single source of truth for who is worth a model
turn — the e-mail whitelist (exact addresses + `*@domain` wildcards) and the
per-channel messenger policy. The messenger side is a three-axis routing model
(see the triage_policy module docstring):

  * sender: whitelisted / blacklisted / unknown
  * group: three independent flags — news, quieted, ignored

Whitelist/blacklist win over the group flags; quieted/ignored bite only for
unknown senders; news is orthogonal to the triage decision. The legacy
``triageBlockedGroup`` predicate is read as ``ignored`` and migrated on write.

This exercises wildcard semantics (especially the freemail guarantee), the `.nt`
round-trip, write-if-changed, Sent-folder derivation, the full routing matrix,
the news flag's orthogonality, the legacy migration, and the CLI mutators.

Standalone, no third-party deps:

    python3 tests/test_triage_policy.py
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "triage_policy.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("triage_policy", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tp = _load_module()

CH = "telegram"
GROUP = "-100123"


# --------------------------------------------------------------------------- #
# E-mail whitelist                                                            #
# --------------------------------------------------------------------------- #

def test_email_exact_and_wildcard():
    addresses = {"alice@gmail.com"}
    wildcards = {"*@factsmission.com", "*@*.epfl.ch"}
    # Exact match, case-insensitive.
    assert tp.email_whitelisted("Alice@Gmail.com", addresses, wildcards)
    # Freemail guarantee: another gmail address is NOT trusted by the exact entry.
    assert not tp.email_whitelisted("mallory@gmail.com", addresses, wildcards)
    # Plain domain wildcard.
    assert tp.email_whitelisted("bob@factsmission.com", addresses, wildcards)
    assert not tp.email_whitelisted("bob@evil.com", addresses, wildcards)
    # Subdomain wildcard covers both apex and any subdomain.
    assert tp.email_whitelisted("carol@epfl.ch", addresses, wildcards)
    assert tp.email_whitelisted("dan@cs.epfl.ch", addresses, wildcards)
    assert not tp.email_whitelisted("dan@notepfl.ch", addresses, wildcards)
    # Garbage inputs never match.
    assert not tp.email_whitelisted("", addresses, wildcards)
    assert not tp.email_whitelisted("no-at-sign", addresses, wildcards)


def test_email_nt_roundtrip_and_determinism():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "gen" / "email-whitelist.nt"
        addresses = {"b@x.com", "a@x.com"}
        wildcards = {"*@z.com"}
        content = tp.render_email_whitelist(addresses, wildcards)
        # Deterministic: sorted, well-formed N-Triples, nested mkdir on write.
        lines = [l for l in content.splitlines() if l]
        assert lines == sorted(lines), "output not sorted"
        assert all(l.endswith(" .") for l in lines)
        assert tp.write_if_changed(content, path) is True
        assert path.parent.is_dir()
        got_addr, got_wild = tp.load_email_whitelist(path)
        assert got_addr == {a.lower() for a in addresses}
        assert got_wild == wildcards


def test_email_news_sender_matching():
    news = {"bulletin@news.example"}
    wilds = {"*@substack.com", "*@*.ms-society.example"}
    # Exact, case-insensitive.
    assert tp.email_news_sender("Bulletin@News.Example", news, wilds)
    # A wildcard behaves exactly as it does in the whitelist — same matcher.
    assert tp.email_news_sender("anyone@substack.com", news, wilds)
    assert tp.email_news_sender("info@sektion.ms-society.example", news, wilds)
    assert tp.email_news_sender("info@ms-society.example", news, wilds)
    # A neighbour of a declared sender is not news.
    assert not tp.email_news_sender("someone@news.example", news, wilds)
    assert not tp.email_news_sender("x@notsubstack.com", news, wilds)
    assert not tp.email_news_sender("", news, wilds)
    assert not tp.email_news_sender("no-at-sign", news, wilds)


def test_email_policy_holds_both_classes():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "email-whitelist.nt"
        pol = tp.EmailPolicy(
            addresses={"boss@work.com"},
            wildcards={"*@factsmission.com"},
            news={"bulletin@news.example"},
            news_wildcards={"*@substack.com"},
        )
        content = tp.render_email_policy(pol)
        lines = [l for l in content.splitlines() if l]
        assert lines == sorted(lines), "output not sorted"
        tp.write_if_changed(content, path)
        got = tp.load_email_policy(path)
        assert got == pol, got
        # The two classes are independent: being a news sender does not
        # whitelist the address for a triage turn.
        assert not tp.email_whitelisted("bulletin@news.example",
                                        got.addresses, got.wildcards)
        assert not tp.email_news_sender("boss@work.com",
                                        got.news, got.news_wildcards)


def test_mutate_email_sorts_news_by_shape():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(Path(tmp) / "e.nt")
        try:
            tp._mutate_email(add_addresses=["boss@work.com"],
                             news_add=["*@substack.com", "Bulletin@News.example"])
            pol = tp.load_email_policy()
            assert pol.news == {"bulletin@news.example"}, pol
            assert pol.news_wildcards == {"*@substack.com"}, pol
            # Adding a whitelist entry later must not drop the news senders:
            # one file holds both classes.
            tp._mutate_email(add_addresses=["peer@partner.com"])
            pol = tp.load_email_policy()
            assert pol.addresses == {"boss@work.com", "peer@partner.com"}, pol
            assert pol.news == {"bulletin@news.example"}, pol
            assert pol.news_wildcards == {"*@substack.com"}, pol
            tp._mutate_email(news_remove=["*@substack.com"])
            assert tp.load_email_policy().news_wildcards == set()
        finally:
            del os.environ["TRIAGE_EMAIL_WHITELIST_PATH"]


def test_write_if_changed():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.nt"
        content = tp.render_email_whitelist({"a@x.com"}, set())
        assert tp.write_if_changed(content, path) is True
        mtime = path.stat().st_mtime_ns
        assert tp.write_if_changed(content, path) is False, "identical content rewrote"
        assert path.stat().st_mtime_ns == mtime, "file was touched"
        assert tp.write_if_changed(content + "\n", path) is True


def test_recipients_from_sent():
    messages = [
        {"to": "Alice <alice@x.com>, bob@y.com"},
        {"to": "carol@z.com", "cc": "dan@z.com", "bcc": "eve@w.com"},
        {"to": ""},          # empty header ignored
        {"subject": "no recipients"},
    ]
    got = tp.recipients_from_sent(messages)
    assert got == {"alice@x.com", "bob@y.com", "carol@z.com", "dan@z.com", "eve@w.com"}


def test_missing_file_is_empty():
    got_addr, got_wild = tp.load_email_whitelist(Path("/nonexistent/x.nt"))
    assert got_addr == set() and got_wild == set()
    pol = tp.load_messenger_policy("signal", Path("/nonexistent/x.nt"))
    assert pol.whitelist == set() and pol.blacklist == set()
    assert pol.ignored == set() and pol.quieted == set() and pol.news == set()


# --------------------------------------------------------------------------- #
# Messenger policy — round-trip, classification, escaping                     #
# --------------------------------------------------------------------------- #

def _policy_file(tmp: Path, **kw) -> Path:
    """Render a policy .nt into tmp and return its path."""
    pol = tp.MessengerPolicy(
        whitelist=set(kw.get("whitelist", [])),
        blacklist=set(kw.get("blacklist", [])),
        ignored=set(kw.get("ignored", [])),
        quieted=set(kw.get("quieted", [])),
        news=set(kw.get("news", [])),
    )
    path = tmp / "policy.nt"
    path.write_text(tp.render_messenger_policy(CH, pol), encoding="utf-8")
    return path


def _gate(path, sender, group=None):
    return tp.gate_decision(CH, sender, group, path=path)


def test_messenger_policy_roundtrip_and_classify():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "signal" / "policy.nt"
        pol = tp.MessengerPolicy(
            whitelist={"+41791112233"},
            blacklist={"+41790000000"},
            ignored={"group.ign=="},
            quieted={"group.qui=="},
            news={"group.news=="},
        )
        content = tp.render_messenger_policy("signal", pol)
        lines = [l for l in content.splitlines() if l]
        assert lines == sorted(lines), "output not sorted"
        tp.write_if_changed(content, path)
        got = tp.load_messenger_policy("signal", path)
        assert got == pol, got
        # Classification, with blacklist winning over whitelist.
        assert tp.handle_status("+41791112233", got.whitelist, got.blacklist) == "whitelisted"
        assert tp.handle_status("+41790000000", got.whitelist, got.blacklist) == "blacklisted"
        assert tp.handle_status("+41799999999", got.whitelist, got.blacklist) == "unknown"
        both_wl, both_bl = {"x"}, {"x"}
        assert tp.handle_status("x", both_wl, both_bl) == "blacklisted"


def test_literal_escaping_roundtrip():
    # A handle with quotes and a backslash must survive the .nt round-trip.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.nt"
        weird = 'odd"name\\x'
        pol = tp.MessengerPolicy({weird}, set(), set(), set(), set())
        path.write_text(tp.render_messenger_policy("signal", pol), encoding="utf-8")
        got = tp.load_messenger_policy("signal", path)
        assert got.whitelist == {weird.lower()}


# --------------------------------------------------------------------------- #
# Messenger policy — three-axis routing matrix                                #
# --------------------------------------------------------------------------- #

def test_routing_matrix():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        # Whitelisted handle → immediate forward, regardless of group flags.
        path = _policy_file(tmp, whitelist=["+41791112233"],
                            ignored=[GROUP], quieted=[GROUP])
        g = _gate(path, "+41791112233", GROUP)
        assert g["forward"] and not g["flagged_unknown"], g
        assert g["reason"] == "whitelisted", g

        # Blacklisted handle → held, drained daily (delivered False).
        path = _policy_file(tmp, blacklist=["+41790000000"])
        g = _gate(path, "+41790000000", None)
        assert not g["forward"] and g["delivered_if_held"] is False, g
        assert g["reason"] == "blacklisted", g

        # Unknown, no group → forward + flagged for the whitelist ask-flow.
        path = _policy_file(tmp)
        g = _gate(path, "+41795555555", None)
        assert g["forward"] and g["flagged_unknown"], g
        assert g["reason"] == "unknown", g

        # Unknown, quieted group → held but drained daily (delivered False).
        path = _policy_file(tmp, quieted=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert not g["forward"] and not g["flagged_unknown"], g
        assert g["delivered_if_held"] is False and g["reason"] == "group-quieted", g

        # Unknown, ignored group → held, never drained (delivered True).
        path = _policy_file(tmp, ignored=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert not g["forward"], g
        assert g["delivered_if_held"] is True and g["reason"] == "group-ignored", g


def test_news_flag_is_orthogonal():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        # news + ignored (a feed-only broadcast source, no personal interaction):
        # held from triage, but flagged for the news rail.
        path = _policy_file(tmp, news=[GROUP], ignored=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert g["news"] is True and not g["forward"], g
        assert g["reason"] == "group-ignored", g

        # news + quieted: reaches triage on the daily drain, and the news feed.
        path = _policy_file(tmp, news=[GROUP], quieted=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert g["news"] is True and not g["forward"], g
        assert g["delivered_if_held"] is False, g

        # news alone (no quiet/ignore): unknown sender still forwards to triage.
        path = _policy_file(tmp, news=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert g["news"] is True and g["forward"] and g["flagged_unknown"], g

        # A group not flagged news never sets the news flag.
        path = _policy_file(tmp, quieted=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert g["news"] is False, g


def test_legacy_blocked_group_reads_as_ignored():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        path = tmp / "policy.nt"
        subj = tp._channel_subject(CH)
        path.write_text(tp._triple(subj, tp.P_BLOCKED_GROUP, GROUP) + "\n",
                        encoding="utf-8")
        pol = tp.load_messenger_policy(CH, path=path)
        assert GROUP in pol.ignored and not pol.quieted, pol
        g = _gate(path, "+41795555555", GROUP)
        assert not g["forward"] and g["reason"] == "group-ignored", g


def test_disabled_gate_forwards_all_without_news():
    with tempfile.TemporaryDirectory() as d:
        path = _policy_file(Path(d), news=[GROUP], ignored=[GROUP])
        g = tp.gate_decision(CH, "+41795555555", GROUP, path=path, enabled=False)
        assert g["forward"] and g["news"] is False and g["reason"] == "gate-disabled", g


def test_render_is_deterministic_and_migrates_legacy():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # Legacy file with the old predicate.
        path = tmp / "policy.nt"
        subj = tp._channel_subject(CH)
        path.write_text(tp._triple(subj, tp.P_BLOCKED_GROUP, GROUP) + "\n",
                        encoding="utf-8")
        pol = tp.load_messenger_policy(CH, path=path)
        rendered = tp.render_messenger_policy(CH, pol)
        # Re-render migrates blocked → ignored predicate.
        assert tp.P_IGNORED_GROUP in rendered and tp.P_BLOCKED_GROUP not in rendered
        # Deterministic: same input → identical bytes.
        assert rendered == tp.render_messenger_policy(CH, pol)


def test_mutate_quiet_and_ignore_are_exclusive():
    with tempfile.TemporaryDirectory() as d:
        os.environ["TRIAGE_MESSENGER_DIR"] = d
        try:
            tp._mutate_messenger(CH, ig_add=[GROUP], news_add=[GROUP])
            pol = tp.load_messenger_policy(CH)
            assert pol.ignored == {GROUP} and pol.news == {GROUP} and not pol.quieted, pol

            # Quieting an ignored group moves it, never doubles it.
            tp._mutate_messenger(CH, q_add=[GROUP])
            pol = tp.load_messenger_policy(CH)
            assert pol.quieted == {GROUP} and not pol.ignored, pol
            assert pol.news == {GROUP}, pol  # news is untouched by the move

            # And back the other way.
            tp._mutate_messenger(CH, ig_add=[GROUP])
            pol = tp.load_messenger_policy(CH)
            assert pol.ignored == {GROUP} and not pol.quieted, pol
        finally:
            tp.messenger_policy_path(CH).unlink(missing_ok=True)
            del os.environ["TRIAGE_MESSENGER_DIR"]


def test_auto_whitelist_on_send():
    """Outbound send promotes the recipient to a known sender — and nothing else.

    Regression guard: this used to unpack `load_messenger_policy()` into three
    names and call `render_messenger_policy()` with four arguments, both of which
    raise against the three-axis `MessengerPolicy`. The only caller wraps it in a
    broad `except`, so every WhatsApp send silently failed to whitelist its
    recipient. Exercising it end-to-end is what catches that class of drift.
    """
    with tempfile.TemporaryDirectory() as d:
        os.environ["TRIAGE_MESSENGER_DIR"] = d
        try:
            tp._mutate_messenger(CH, wl_add=["41791112233"], bl_add=["41790000000"],
                                 ig_add=[GROUP], news_add=[GROUP])

            # The gateway hands over bare JID users — a phone number and its LID
            # counterpart — so both identities of one contact become known.
            added = tp.auto_whitelist_on_send(CH, ["41791234567", "100000000000001"])
            assert added == ["100000000000001", "41791234567"], added

            pol = tp.load_messenger_policy(CH)
            assert {"41791234567", "100000000000001"} <= pol.whitelist, pol
            # Every other axis survives the write untouched.
            assert pol.blacklist == {"41790000000"}, pol
            assert pol.ignored == {GROUP} and pol.news == {GROUP}, pol

            # Idempotent: a known handle adds nothing.
            assert tp.auto_whitelist_on_send(CH, ["41791234567"]) == []
            # An explicit block survives an outbound send.
            assert tp.auto_whitelist_on_send(CH, ["41790000000"]) == []
            assert "41790000000" not in tp.load_messenger_policy(CH).whitelist
            # Nothing to do is not an error.
            assert tp.auto_whitelist_on_send(CH, ["", None]) == []
        finally:
            tp.messenger_policy_path(CH).unlink(missing_ok=True)
            del os.environ["TRIAGE_MESSENGER_DIR"]


def _run() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
