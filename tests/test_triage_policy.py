#!/usr/bin/env python3
"""Focused checks for the triage delivery-gate policy library.

`scripts/triage_policy.py` is the single source of truth for who is worth a model
turn — the per-channel policy, `email` among the channels, and the e-mail group
flags. The routing model has two axes (see the triage_policy module docstring):

  * sender: VIP, instructed, or neither — exact handles, and on `email` also
    `*@domain` wildcards
  * group: three independent flags — news, quieted, ignored

The legacy ``triageBlockedGroup`` predicate is read as ``ignored`` and migrated
on write. The e-mail whitelist is retired: its triples are not read and drop
out of a file on its next write, and nothing of its API is left.

This exercises wildcard semantics (especially the freemail guarantee), the `.nt`
round-trip, write-if-changed, the full routing matrix, the news flag's
orthogonality, the legacy migration, the retired whitelist, the sender axis
for mail (exact then wildcard, a wildcard on no other channel, an unreadable
policy), the contact projection, and the CLI.

Standalone, no third-party deps:

    python3 tests/test_triage_policy.py
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
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
# E-mail groups                                                               #
# --------------------------------------------------------------------------- #

def test_email_news_sender_matching():
    news = {"bulletin@news.example"}
    wilds = {"*@substack.com", "*@*.ms-society.example"}
    # Exact, case-insensitive.
    assert tp.email_news_sender("Bulletin@News.Example", news, wilds)
    # A wildcard covers its domain; `*@*.domain` the apex and what is below.
    assert tp.email_news_sender("anyone@substack.com", news, wilds)
    assert tp.email_news_sender("info@sektion.ms-society.example", news, wilds)
    assert tp.email_news_sender("info@ms-society.example", news, wilds)
    # A neighbour of a declared sender is not news.
    assert not tp.email_news_sender("someone@news.example", news, wilds)
    assert not tp.email_news_sender("x@notsubstack.com", news, wilds)
    assert not tp.email_news_sender("", news, wilds)
    assert not tp.email_news_sender("no-at-sign", news, wilds)


def test_email_policy_roundtrip_and_determinism():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "gen" / "email-policy.nt"
        pol = tp.EmailPolicy(
            news={"bulletin@news.example", "a.list.example"},
            news_wildcards={"*@substack.com"},
            quieted={"digest.list.example"},
            ignored_wildcards={"*@*.noise.example"},
        )
        content = tp.render_email_policy(pol)
        # Deterministic: sorted, well-formed N-Triples, nested mkdir on write.
        lines = [l for l in content.splitlines() if l]
        assert lines == sorted(lines), "output not sorted"
        assert all(l.endswith(" .") for l in lines)
        assert tp.write_if_changed(content, path) is True
        assert path.parent.is_dir()
        got = tp.load_email_policy(path)
        assert got == pol, got
        # The classes are independent: a news group is not thereby quieted.
        assert not tp.email_group_member("bulletin@news.example",
                                         got.quieted, got.quieted_wildcards)


def test_the_empty_policy_and_its_fields():
    """`EmailPolicy()` is the empty policy, and the tuple holds the three
    group flags and nothing else — no whitelist fields to pass by position."""
    assert tp.EmailPolicy._fields == (
        "news", "news_wildcards", "quieted", "quieted_wildcards",
        "ignored", "ignored_wildcards")
    empty = tp.EmailPolicy()
    assert all(getattr(empty, name) == frozenset() for name in tp.EmailPolicy._fields)
    assert tp.render_email_policy(empty) == ""
    flags = tp.email_gate_decision("anyone@x.org", "<some.list.example>", pol=empty)
    assert flags == {"group": "some.list.example", "news": False,
                     "quieted": False, "ignored": False}, flags


def test_mutate_email_sorts_news_by_shape():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(Path(tmp) / "e.nt")
        try:
            tp._mutate_email(news_add=["*@substack.com", "Bulletin@News.example"])
            pol = tp.load_email_policy()
            assert pol.news == {"bulletin@news.example"}, pol
            assert pol.news_wildcards == {"*@substack.com"}, pol
            # Flagging another group later must not drop the news senders:
            # one file holds every class.
            tp._mutate_email(ignore_add=["noise.list.example"])
            pol = tp.load_email_policy()
            assert pol.ignored == {"noise.list.example"}, pol
            assert pol.news == {"bulletin@news.example"}, pol
            assert pol.news_wildcards == {"*@substack.com"}, pol
            tp._mutate_email(news_remove=["*@substack.com"])
            assert tp.load_email_policy().news_wildcards == set()
        finally:
            del os.environ["TRIAGE_EMAIL_WHITELIST_PATH"]


def test_write_if_changed():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.nt"
        content = tp.render_email_policy(tp.EmailPolicy(news={"a@x.com"}))
        assert tp.write_if_changed(content, path) is True
        mtime = path.stat().st_mtime_ns
        assert tp.write_if_changed(content, path) is False, "identical content rewrote"
        assert path.stat().st_mtime_ns == mtime, "file was touched"
        assert tp.write_if_changed(content + "\n", path) is True


def test_missing_file_is_empty():
    assert tp.load_email_policy(Path("/nonexistent/x.nt")) == tp.EmailPolicy()
    pol = tp.load_messenger_policy("signal", Path("/nonexistent/x.nt"))
    assert pol.vip == set() and pol.news == set()
    assert pol.ignored == set() and pol.quieted == set() and pol.news == set()


# --------------------------------------------------------------------------- #
# Messenger policy — round-trip, classification, escaping                     #
# --------------------------------------------------------------------------- #

def _policy_file(tmp: Path, **kw) -> Path:
    """Render a policy .nt into tmp and return its path."""
    pol = tp.MessengerPolicy(
        ignored=set(kw.get("ignored", [])),
        quieted=set(kw.get("quieted", [])),
        news=set(kw.get("news", [])),
        vip=set(kw.get("vip", [])),
    )
    path = tmp / "policy.nt"
    path.write_text(tp.render_messenger_policy(CH, pol), encoding="utf-8")
    return path


def _gate(path, sender, group=None):
    return tp.gate_decision(CH, sender, group, path=path)


def test_messenger_policy_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "signal" / "policy.nt"
        pol = tp.MessengerPolicy(
            ignored={"group.ign=="},
            quieted={"group.qui=="},
            news={"group.news=="},
            vip={"+41791112233"},
        )
        content = tp.render_messenger_policy("signal", pol)
        lines = [l for l in content.splitlines() if l]
        assert lines == sorted(lines), "output not sorted"
        tp.write_if_changed(content, path)
        got = tp.load_messenger_policy("signal", path)
        assert got == pol, got


def test_a_retired_whitelist_entry_is_simply_not_read():
    """The messenger whitelist and blacklist are gone; old files still load.

    They decided whose message was worth a session to notify about, and the
    chat surface notifies for free. Nothing reads their triples any more, and
    a policy file written before they were retired must load as if they were
    not there rather than raise — it drops them on the next write."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "policy.nt"
        subj = tp._channel_subject("signal")
        legacy = "".join(line + "\n" for line in [
            tp._triple(subj, tp.KB + "triageWhitelistHandle", "+41791112233"),
            tp._triple(subj, tp.KB + "triageBlacklistHandle", "+41790000000"),
            tp._triple(subj, tp.P_VIP_HANDLE, "+41795555555"),
            tp._triple(subj, tp.P_NEWS_GROUP, GROUP),
        ])
        path.write_text(legacy, encoding="utf-8")
        pol = tp.load_messenger_policy("signal", path)
        assert pol.vip == {"+41795555555"} and pol.news == {GROUP}, pol
        # The blacklisted handle is nobody special now — and nor is the
        # whitelisted one, which is the point.
        for handle in ("+41791112233", "+41790000000"):
            g = tp.gate_decision("signal", handle, None, path=path)
            assert g["forward"] is True and g["vip"] is False, (handle, g)


def test_literal_escaping_roundtrip():
    # A handle with quotes and a backslash must survive the .nt round-trip.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.nt"
        weird = 'odd"name\\x'
        pol = tp.MessengerPolicy(set(), set(), set(), {weird})
        path.write_text(tp.render_messenger_policy("signal", pol), encoding="utf-8")
        got = tp.load_messenger_policy("signal", path)
        assert got.vip == {weird.lower()}


# --------------------------------------------------------------------------- #
# Messenger policy — three-axis routing matrix                                #
# --------------------------------------------------------------------------- #

def test_vip_follows_the_sender_and_only_the_sender():
    """The one axis that buys a model turn, and the one that is about a person.

    Everything else here is about attention — whether an arrival is worth
    interrupting the user for — and is read off the group as much as the
    sender. A VIP is the user saying "I want this person's messages dealt
    with", which is true of the person wherever they write, so being one voice
    in a room of forty must not dilute it."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        # In a 1:1 and in a group: the same person, the same answer. And the
        # group's own flags do not touch it — `ignored` still means nobody is
        # interrupted, which is a different question.
        path = _policy_file(tmp, vip=["+41791112233"], ignored=[GROUP])
        assert _gate(path, "+41791112233", None)["vip"] is True
        assert _gate(path, "+41791112233", GROUP)["vip"] is True
        # …while the room itself is nobody: a VIP group id is not a thing.
        assert _gate(path, "+41799999999", GROUP)["vip"] is False
        assert _gate(path, GROUP, GROUP)["vip"] is False

        # Independent of the group's own flags in both directions: being a VIP
        # does not make the room loud, and a quiet room does not make the VIP
        # ordinary.
        path = _policy_file(tmp, vip=["+41795555555"], quieted=[GROUP])
        quiet = _gate(path, "+41795555555", GROUP)
        assert quiet["vip"] is True and quiet["forward"] is False, quiet
        assert _gate(path, "+41799999999", GROUP)["vip"] is False

        # Case- and whitespace-insensitive, like every other handle here, and
        # it survives a render/load round trip.
        path = _policy_file(tmp, vip=["@Nina"])
        assert _gate(path, " @nina ", None)["vip"] is True
        assert tp.load_messenger_policy(CH, path=path).vip == {"@nina"}

        # Nobody is a VIP by default.
        assert _gate(_policy_file(tmp), "+41791112233", None)["vip"] is False

        # …but the gate turned off still means every message is worked. That
        # switch has always meant "forward everything"; since `vip` is what
        # decides a turn now, it has to say so, or turning the gate off would
        # quietly turn off the handling it exists to force on.
        off = tp.gate_decision(CH, "+41791112233", None,
                               path=_policy_file(tmp), enabled=False)
        assert off["forward"] is True and off["vip"] is True, off


def test_routing_matrix():
    """What is left of the attention axis: the group, and nothing else.

    No sender is special here any more. Not wanting to hear from a person is a
    chat one mutes — on the chat, in the interface — and the policy file has no
    opinion about people except which of them are VIPs."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        # No flags at all → the arrival is worth the user's attention.
        path = _policy_file(tmp)
        g = _gate(path, "+41795555555", None)
        assert g["forward"] and g["reason"] == "open", g

        # …and a sender is never the reason it is not.
        path = _policy_file(tmp, vip=["+41791112233"])
        assert _gate(path, "+41791112233", None)["forward"] is True

        # Quieted group → silent, and left for the fallback drain.
        path = _policy_file(tmp, quieted=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert not g["forward"], g
        assert g["delivered_if_held"] is False and g["reason"] == "group-quieted", g

        # Ignored group → silent, and accounted for.
        path = _policy_file(tmp, ignored=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert not g["forward"], g
        assert g["delivered_if_held"] is True and g["reason"] == "group-ignored", g

        # A group's flags reach only messages sent in it.
        assert _gate(path, "+41795555555", None)["forward"] is True


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

        # news alone (no quiet/ignore): the arrival still reaches the user.
        path = _policy_file(tmp, news=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert g["news"] is True and g["forward"], g

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


def test_email_list_id_normalisation():
    # The RFC shape, bracketed and not, case-folded.
    assert tp.email_list_id("<sgcarney.substack.com>") == "sgcarney.substack.com"
    assert tp.email_list_id("Members <members.List.Example.org>") \
        == "members.list.example.org"
    assert tp.email_list_id("  plain.list.example.org  ") == "plain.list.example.org"
    # Real-world junk that must NOT become a group: a display name in the
    # brackets (seen verbatim in the wild), a bare token with no dot, an empty
    # header, and an absurdly long value.
    assert tp.email_list_id("799706515 <Brack News>") == ""
    assert tp.email_list_id("<newsletter>") == ""
    assert tp.email_list_id("") == ""
    assert tp.email_list_id(None) == ""
    assert tp.email_list_id("<" + "a." * 200 + "com>") == ""


def test_email_group_id_falls_back_to_the_sender():
    # A real list keys on the list, whoever posted to it.
    assert tp.email_group_id("<members.list.example.org>", "alice@x.com") \
        == "members.list.example.org"
    # A listless newsletter is its own group of one — the whole point of the
    # fallback, so `news`/`quieted`/`ignored` need no second mechanism for it.
    assert tp.email_group_id("", "News@Bulletin.example") == "news@bulletin.example"
    # An unusable List-Id is treated as no List-Id at all.
    assert tp.email_group_id("799706515 <Brack News>", "no-reply@brack.ch") \
        == "no-reply@brack.ch"


def test_group_wildcard_covers_the_lists_beneath_it():
    wilds = {"*@substack.com"}
    # The platform's own address, and a per-publication list under it: one
    # wildcard covers both, because a List-Id is a namespace, not a mailbox.
    assert tp.email_group_member("no-reply@substack.com", set(), wilds)
    assert tp.email_group_member("sgcarney.substack.com", set(), wilds)
    assert tp.email_group_member("substack.com", set(), wilds)
    # An address *at* a subdomain is still not covered — that stays the strict
    # `*@*.domain` reading the sender axis relies on.
    assert not tp.email_group_member("someone@sgcarney.substack.com", set(), wilds)
    assert tp.email_group_member("someone@sgcarney.substack.com", set(),
                                 {"*@*.substack.com"})
    # No accidental suffix matches.
    assert not tp.email_group_member("notsubstack.com", set(), wilds)
    assert not tp.email_group_member("", set(), wilds)
    # An exact entry wins regardless of shape.
    assert tp.email_group_member("Members.List.Example.org",
                                 {"members.list.example.org"}, set())


def test_legacy_address_level_news_still_matches_a_list_mail():
    """A news entry written before List-Id detection keeps working.

    The entries in the live policy name sender addresses; once the gate started
    reading `List-Id`, the group for those same mails became the list. Checking
    news against the sender as well as the group is what stops that migration
    silently un-filing a newsletter.
    """
    pol = tp.EmailPolicy(news={"bulletin@news.example"}, news_wildcards=set())
    d = tp.email_gate_decision("bulletin@news.example",
                               "<letters.news.example>", pol=pol)
    assert d["news"] is True, d
    assert d["group"] == "letters.news.example", d


def test_email_gate_decision_answers_the_group_axis():
    """What the mail gateway reads: the group, and its three flags — whoever
    the sender is, and nothing else."""
    pol = tp.EmailPolicy(
        news={"digest.list.example.org"}, news_wildcards={"*@substack.com"},
        quieted={"digest.list.example.org", "sgcarney.substack.com"},
        ignored={"noise.list.example.org"}, ignored_wildcards={"*@substack.com"},
    )

    def axis(sender, list_id=""):
        return tp.email_gate_decision(sender, list_id, pol=pol)

    assert axis("stranger@x.com") == {
        "group": "stranger@x.com", "news": False, "quieted": False, "ignored": False}
    assert axis("stranger@x.com", "<digest.list.example.org>") == {
        "group": "digest.list.example.org", "news": True, "quieted": True, "ignored": False}
    assert axis("stranger@x.com", "<noise.list.example.org>") == {
        "group": "noise.list.example.org", "news": False, "quieted": False, "ignored": True}
    # A listless sender is its own group, and a wildcard flags it.
    assert axis("No-Reply@Substack.com") == {
        "group": "no-reply@substack.com", "news": True, "quieted": False, "ignored": True}
    # A group a file puts in both classes — an exact entry in one, a wildcard
    # in the other — reads as ignored, never as both.
    assert axis("no-reply@substack.com", "<sgcarney.substack.com>") == {
        "group": "sgcarney.substack.com", "news": True, "quieted": False, "ignored": True}
    # A news group that is neither quieted nor ignored.
    assert axis("anyone@x.com", "<digest.list.example.org>")["news"] is True
    # Nothing here knows a sender class — that is gate_decision("email", …) —
    # and nothing of e-mail triage's two runs is answered any more.
    d = tp.email_gate_decision("peer@list.example.org", "<noise.list.example.org>", pol=pol)
    assert set(d) == {"group", "news", "quieted", "ignored"}, d


def test_the_retired_whitelist_is_not_read_and_drops_out_on_a_write():
    """The e-mail whitelist is gone. A policy file written before still holds
    its triples — the user's entries, and the address book's old projection
    under a subject of its own. They load as if they were not there, and the
    next write of the file drops them and keeps every group flag."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "email-policy.nt"
        os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(path)
        os.environ["TRIAGE_MESSENGER_DIR"] = str(Path(tmp) / "messenger")
        try:
            path.write_text("".join(line + "\n" for line in [
                tp._triple(tp.EMAIL_SUBJECT, tp.KB + "triageWhitelistAddress", "boss@work.com"),
                tp._triple(tp.EMAIL_SUBJECT, tp.KB + "triageWhitelistWildcard", "*@owner.example"),
                tp._triple(tp.EMAIL_SUBJECT, tp.P_NEWS_WILDCARD, "*@substack.com"),
                tp._triple(tp.EMAIL_SUBJECT + tp.CONTACTS_SUFFIX,
                           tp.KB + "triageWhitelistAddress", "vip@x.org"),
            ]), encoding="utf-8")
            pol = tp.load_email_policy()
            assert pol == tp.EmailPolicy(news_wildcards={"*@substack.com"}), pol
            # Nobody is special to the gate for having been whitelisted: the
            # sender axis reads the `email` channel's policy, which is empty.
            for sender in ("boss@work.com", "anyone@owner.example", "vip@x.org"):
                assert set(tp.email_gate_decision(sender)) == {"group", "news", "quieted", "ignored"}
                g = tp.gate_decision("email", sender)
                assert (g["vip"], g["instructed"]) == (False, False), (sender, g)
            # A sync leaves the file alone, so a migration can still read it raw…
            tp.sync_contacts({})
            raw = {lit for _s, p, lit in tp._parse(path) if "Whitelist" in p}
            assert raw == {"boss@work.com", "*@owner.example", "vip@x.org"}, raw
            # …and any write of it drops the stale lines.
            tp._mutate_email(news_add=["bulletin@news.example"])
            text = path.read_text(encoding="utf-8")
            assert "Whitelist" not in text and tp.CONTACTS_SUFFIX + ">" not in text, text
            assert "*@substack.com" in text and "bulletin@news.example" in text, text
        finally:
            del os.environ["TRIAGE_EMAIL_WHITELIST_PATH"]
            del os.environ["TRIAGE_MESSENGER_DIR"]


def test_nothing_of_the_whitelist_api_is_left():
    """No loader, renderer, matcher or mutation of the whitelist survives, and
    its CLI commands are refused rather than silently flagging a group."""
    for name in ("P_ADDRESS", "P_WILDCARD", "load_email_whitelist", "render_email_whitelist",
                 "email_whitelisted", "recipients_from_sent"):
        assert not hasattr(tp, name), name
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(Path(tmp) / "e.nt")
        try:
            for stale in ({"add_addresses": ["boss@work.com"]}, {"add_wildcards": ["*@x.org"]},
                          {"remove": ["boss@work.com"]}):
                try:
                    tp._mutate_email(**stale)
                    raise AssertionError(f"a whitelist mutation was taken: {stale}")
                except TypeError:
                    pass
            try:
                tp._mutate_email_locked(["boss@work.com"])
                raise AssertionError("a positional whitelist address was taken")
            except TypeError:
                pass
            assert tp.load_email_policy() == tp.EmailPolicy()
        finally:
            del os.environ["TRIAGE_EMAIL_WHITELIST_PATH"]
        env = dict(os.environ, TRIAGE_MESSENGER_DIR=str(Path(tmp) / "messenger"),
                   TRIAGE_EMAIL_WHITELIST_PATH=str(Path(tmp) / "e.nt"))
        for cmd in ("email-add-address", "email-add-wildcard", "email-remove"):
            proc = subprocess.run([sys.executable, str(SCRIPT), cmd, "boss@work.com"],
                                  capture_output=True, text=True, env=env)
            assert proc.returncode == 2 and "invalid choice" in proc.stderr, (cmd, proc.stderr)
        # show-email lists the group flags, and no whitelist line.
        subprocess.run([sys.executable, str(SCRIPT), "email-news-add", "*@substack.com"],
                       check=True, env=env)
        proc = subprocess.run([sys.executable, str(SCRIPT), "show-email"],
                              capture_output=True, text=True, env=env)
        assert proc.stdout == "news\t*@substack.com\n", proc.stdout


def test_quiet_and_ignore_wildcards_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(Path(tmp) / "e.nt")
        try:
            tp._mutate_email(news_add=["*@substack.com"],
                             ignore_add=["*@substack.com", "noise.list.example"],
                             quiet_add=["digest.list.example"])
            pol = tp.load_email_policy()
            assert pol.ignored == {"noise.list.example"}, pol
            assert pol.ignored_wildcards == {"*@substack.com"}, pol
            assert pol.quieted == {"digest.list.example"}, pol
            # The classes are exclusive: quieting an ignored group moves it.
            tp._mutate_email(quiet_add=["noise.list.example"])
            pol = tp.load_email_policy()
            assert pol.ignored == set(), pol
            assert "noise.list.example" in pol.quieted, pol
            # And the file still round-trips deterministically.
            content = tp.render_email_policy(pol)
            lines = [l for l in content.splitlines() if l]
            assert lines == sorted(lines), "output not sorted"
        finally:
            del os.environ["TRIAGE_EMAIL_WHITELIST_PATH"]


def test_contact_owned_members():
    """The address book's VIP persons live under a subject of their own: a
    sync replaces them wholesale, hand-set VIPs stay, writers that know
    nothing of them keep them, and a loader that reads only the predicate (a
    gateway built earlier) sees them too. Their e-mail addresses are handles
    of the `email` channel, and the group flags' file is none of the sync's
    business."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TRIAGE_MESSENGER_DIR"] = str(Path(tmp) / "messenger")
        os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(Path(tmp) / "email.nt")
        try:
            (Path(tmp) / "messenger" / "signal").mkdir(parents=True)
            tp._mutate_messenger("signal", vip_add=["+41790000001"])
            tp._mutate_email(quiet_add=["digest.list.example"])
            flags = (Path(tmp) / "email.nt").read_bytes()
            written = tp.sync_contacts({"signal": {"+41790000002"}, "matrix": {"@mara:example.org"},
                                        "email": {"Mara@Example.org"}})
            assert sorted(written) == sorted(str(tp.messenger_policy_path(c)) for c in ("signal", "email")), written
            pol = tp.load_messenger_policy("signal")
            assert pol.vip == {"+41790000001"} and pol.vip_contacts == {"+41790000002"}, pol
            assert tp.gate_decision("signal", "+41790000002")["vip"] is True
            assert not (Path(tmp) / "messenger" / "matrix").exists(), "no directory for a channel no gateway reads"
            # Mail: a VIP handle of its channel, and nothing in the flags' file.
            assert tp.load_messenger_policy("email").vip_contacts == {"mara@example.org"}
            assert tp.gate_decision("email", "mara@example.org")["vip"] is True
            assert (Path(tmp) / "email.nt").read_bytes() == flags, "the sync does not write the e-mail policy"
            # Writers that know nothing of the address book keep its members.
            tp._mutate_messenger("signal", news_add=["group-x"])
            tp._mutate_messenger("email", vip_add=["hand@x.org"])
            assert tp.load_messenger_policy("signal").vip_contacts == {"+41790000002"}
            assert tp.load_messenger_policy("email").vip_contacts == {"mara@example.org"}
            # An older loader reads the predicate, not the subject.
            text = tp.messenger_policy_path("signal").read_text(encoding="utf-8")
            vips = {lit for _s, p, lit in tp._parse(tp.messenger_policy_path("signal")) if p == tp.P_VIP_HANDLE}
            assert vips == {"+41790000001", "+41790000002"}, text
            # A sync that says nobody clears the projection only; unchanged, it writes nothing.
            tp.sync_contacts({})
            assert tp.load_messenger_policy("signal").vip == {"+41790000001"}
            assert tp.load_messenger_policy("signal").vip_contacts == set()
            assert tp.load_messenger_policy("email").vip == {"hand@x.org"}
            assert tp.load_messenger_policy("email").vip_contacts == set()
            assert tp.load_email_policy().quieted == {"digest.list.example"}
            assert tp.sync_contacts({}) == []
            # A known messenger service gets its directory when it has a VIP.
            tp.sync_contacts({"whatsapp": {"+41790000003"}})
            assert tp.load_messenger_policy("whatsapp").vip_contacts == {"+41790000003"}
            # Every read-modify-write holds the file's lock (a sibling .lock).
            assert tp.messenger_policy_path("signal").with_name("policy.nt.lock").exists()
            # The second positional argument was once the addresses projected
            # into the whitelist: a caller still passing them fails loudly
            # instead of instructing them.
            try:
                tp.sync_contacts({}, {"mara@example.org"})
                raise AssertionError("a positional second argument was taken")
            except TypeError:
                pass
        finally:
            del os.environ["TRIAGE_MESSENGER_DIR"]
            del os.environ["TRIAGE_EMAIL_WHITELIST_PATH"]


def _email_policy(tmp: Path, **kw) -> Path:
    """Render an `email` channel policy into tmp and return its path."""
    pol = tp.MessengerPolicy(set(), set(), set(), set(kw.get("vip", [])),
                             set(kw.get("vip_contacts", [])), set(kw.get("instructed", [])))
    path = tmp / "email" / "policy" / "policy.nt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tp.render_messenger_policy("email", pol), encoding="utf-8")
    return path


def test_sync_writes_the_email_channel_in_the_messenger_shape():
    """`email` is a channel sync_contacts writes like the messengers': the
    mail gateway's policy.nt, the contact-owned subject, the two handle
    predicates, addresses and wildcards alike — and beside it, untouched, the
    file of the group flags."""
    with _PolicyEnv() as env:
        path = env.new.with_name("policy.nt")
        assert tp.messenger_policy_path("email") == path
        # Nobody to project: nothing is written — but the flags' file is put
        # in place, which the mail gateway waits for.
        assert tp.sync_contacts({}) == [] and not path.exists()
        assert env.new.read_bytes() == b""
        tp._mutate_email(news_add=["*@substack.com"], ignore_add=["noise.list.example"])
        flags = env.new.read_bytes()
        written = tp.sync_contacts(
            {"email": {"Lou@Example.org", "*@acme.example"}},
            instructed_handles={"email": {"team@spitex.example", "*@*.shop.example"}})
        assert written == [str(path)], written
        assert path.read_text(encoding="utf-8").splitlines() == [
            '<urn:retinue:triage:email:contacts> <https://w3id.org/retinue/kb#triageInstructedHandle> "*@*.shop.example" .',
            '<urn:retinue:triage:email:contacts> <https://w3id.org/retinue/kb#triageInstructedHandle> "team@spitex.example" .',
            '<urn:retinue:triage:email:contacts> <https://w3id.org/retinue/kb#triageVipHandle> "*@acme.example" .',
            '<urn:retinue:triage:email:contacts> <https://w3id.org/retinue/kb#triageVipHandle> "lou@example.org" .',
        ]
        assert env.new.read_bytes() == flags, "the group flags are a file of their own"
        # Read back through the path the gateway names, as it reads it.
        lou = tp.gate_decision("email", "lou@example.org", path=path)
        assert (lou["vip"], lou["instructed"]) == (True, False), lou
        shop = tp.gate_decision("email", "orders@eu.shop.example", path=path)
        assert (shop["vip"], shop["instructed"]) == (False, True), shop
        # Replaced wholesale, like any channel's projection.
        tp.sync_contacts({"email": {"lou@example.org"}})
        pol = tp.load_messenger_policy("email")
        assert (pol.vip_contacts, pol.instructed_contacts) == ({"lou@example.org"}, set()), pol
    with tempfile.TemporaryDirectory() as tmp:
        # A deployment whose e-mail policy is pinned elsewhere has no `email`
        # folder under the messenger root: the channel gets one with its
        # first member, like a messenger service.
        os.environ["TRIAGE_MESSENGER_DIR"] = str(Path(tmp) / "messenger")
        os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(Path(tmp) / "pinned.nt")
        try:
            assert tp.sync_contacts({}) == []
            assert not (Path(tmp) / "messenger" / "email").exists()
            assert tp.sync_contacts({"email": {"lou@example.org"}}) == [
                str(Path(tmp) / "messenger" / "email" / "policy" / "policy.nt")]
        finally:
            del os.environ["TRIAGE_MESSENGER_DIR"]
            del os.environ["TRIAGE_EMAIL_WHITELIST_PATH"]


def test_email_sender_axis_exact_then_wildcard():
    """On `email` a sender matches an exact address, then a `*@domain` /
    `*@*.domain` wildcard with the matcher the group flags use — per class,
    whatever the group, and never for something that is no address."""
    with tempfile.TemporaryDirectory() as d:
        path = _email_policy(Path(d), vip_contacts=["mara@example.org", "*@acme.example"],
                             instructed=["*@*.epfl.ch", "billing@acme.example"],
                             vip=["*@hand.example"])

        def classes(sender, group=None):
            g = tp.gate_decision("email", sender, group, path=path)
            return g["vip"], g["instructed"]

        # Exact, case- and whitespace-insensitive; the freemail guarantee
        # holds: one address at a domain says nothing of its neighbours.
        assert classes(" Mara@Example.ORG ") == (True, False)
        assert classes("mallory@example.org") == (False, False)
        # A domain: every address there, and nobody at a subdomain or at a
        # domain that merely ends the same.
        assert classes("anyone@acme.example") == (True, False)
        assert classes("anyone@eu.acme.example") == (False, False)
        assert classes("anyone@notacme.example") == (False, False)
        # A subdomain wildcard: the apex and everything below it.
        assert classes("carol@epfl.ch") == (False, True)
        assert classes("dan@cs.epfl.ch") == (False, True)
        assert classes("dan@notepfl.ch") == (False, False)
        # The two classes are independent, and an address can be in both —
        # one by its domain, one by itself.
        assert classes("billing@acme.example") == (True, True)
        # A wildcard set by hand (vip-add) is one like any other.
        assert classes("x@hand.example") == (True, False)
        # The sender axis knows no group: a list the mail came over changes
        # nothing, and a List-Id is nobody.
        assert classes("anyone@acme.example", "members.list.example.org") == (True, False)
        assert classes("acme.example") == (False, False)
        # Text from outside: nothing that is not one address falls under a
        # domain, however it ends.
        for junk in ("", "no-at-sign", "@acme.example", "a@b@acme.example", "a b@acme.example",
                     "evil@x.org <x@acme.example", '"x"@acme.example', "x@acme.example@evil.org",
                     "x@", "*@acme.example.evil.org"):
            assert classes(junk) == (False, False), junk
        # The gate switched off is the messengers' switch, unchanged.
        off = tp.gate_decision("email", "stranger@x.org", path=path, enabled=False)
        assert (off["vip"], off["instructed"]) == (True, False), off


def test_a_wildcard_never_matches_on_a_messenger_channel():
    """A `*@domain` handle is a wildcard on `email` and nowhere else: on a
    messenger it is a handle like any other, which only its literal self
    matches — so nothing a contact's domain projects, and nothing typed into
    the wrong channel's policy, can make a class of senders VIP there."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for channel in tp.MESSENGER_CHANNELS + ("matrix",):
            pol = tp.MessengerPolicy(set(), set(), set(), {"*@acme.example", "*@*.epfl.ch"},
                                     {"*@acme.example"}, {"*@acme.example", "*@*.epfl.ch"})
            path = tmp / f"{channel}.nt"
            path.write_text(tp.render_messenger_policy(channel, pol), encoding="utf-8")
            for sender in ("anyone@acme.example", "dan@cs.epfl.ch", "carol@epfl.ch", "acme.example"):
                g = tp.gate_decision(channel, sender, None, path=path)
                assert (g["vip"], g["instructed"]) == (False, False), (channel, sender, g)
            # Exact matching is what it always was — the literal handle.
            g = tp.gate_decision(channel, "*@ACME.example", None, path=path)
            assert (g["vip"], g["instructed"]) == (True, True), (channel, g)
        # The very same file read as the `email` channel's does match.
        g = tp.gate_decision("email", "anyone@acme.example", None, path=tmp / "signal.nt")
        assert (g["vip"], g["instructed"]) == (True, True), g


def test_an_unreadable_policy_buys_no_mail_a_turn():
    """The sender axis fails closed for mail: a policy file that is there and
    cannot be read answers nobody special — no VIP, no instructed sender, so
    no model turn — without raising, under a reason the gateway can log. A
    messenger keeps its contract: it raises, and its gateway fails open."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        good = _email_policy(tmp, vip_contacts=["mara@example.org"], instructed=["mara@example.org"])
        ok = tp.gate_decision("email", "mara@example.org", path=good)
        assert (ok["vip"], ok["instructed"], ok["reason"]) == (True, True, "open"), ok
        # Not UTF-8, and a directory where the file should be.
        garbled = tmp / "garbled.nt"
        garbled.write_bytes(good.read_bytes() + b"\xff\xfe\x00 not text\n")
        folder = tmp / "folder.nt"
        folder.mkdir()
        for bad in (garbled, folder):
            g = tp.gate_decision("email", "mara@example.org", "members.list.example.org", path=bad)
            assert g == {"forward": True, "delivered_if_held": True, "news": False, "vip": False,
                         "instructed": False, "reason": "policy-unreadable"}, (bad.name, g)
        # No file at all is no policy, not a failure: nobody is special yet.
        none = tp.gate_decision("email", "mara@example.org", path=tmp / "absent.nt")
        assert (none["vip"], none["instructed"], none["reason"]) == (False, False, "open"), none
        # The group axis is a file of its own; with no flags it holds nothing back.
        flags = tp.email_gate_decision("mara@example.org", pol=tp.EmailPolicy())
        assert (flags["news"], flags["quieted"], flags["ignored"]) == (False, False, False), flags
        # A messenger channel still raises on the same bytes.
        try:
            tp.gate_decision("signal", "+41790000001", path=garbled)
            raise AssertionError("a messenger gate swallowed an unreadable policy")
        except UnicodeDecodeError:
            pass
        # …and reads a file it cannot open as empty, as before.
        assert tp.gate_decision("signal", "+41790000001", path=folder)["vip"] is False


def test_check_email_reports_the_sender_class():
    """`check-email` says where a mail lands on each axis: its group and that
    group's flags, and the sender's class — vip, instructed, both or neither."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, TRIAGE_MESSENGER_DIR=str(Path(tmp) / "messenger"),
                   TRIAGE_EMAIL_WHITELIST_PATH=str(Path(tmp) / "email-policy.nt"))

        def cli(*args):
            proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)
            return proc.returncode, dict(line.split("\t")[:2] for line in proc.stdout.splitlines()), proc.stderr

        assert cli("email-quiet-add", "members.list.example.org")[0] == 0
        assert cli("email-news-add", "*@substack.com")[0] == 0
        assert cli("vip-add", "--channel", "email", "--handle", "Boss@Work.com")[0] == 0
        os.environ.update({k: env[k] for k in ("TRIAGE_MESSENGER_DIR", "TRIAGE_EMAIL_WHITELIST_PATH")})
        try:
            tp.sync_contacts({"email": {"*@acme.example"}},
                             instructed_handles={"email": {"*@acme.example", "team@spitex.example"}})
        finally:
            del os.environ["TRIAGE_MESSENGER_DIR"]
            del os.environ["TRIAGE_EMAIL_WHITELIST_PATH"]
        code, out, _ = cli("check-email", "stranger@x.com", "--list-id", "<members.list.example.org>")
        assert code == 3 and out["group"] == "members.list.example.org", (code, out)
        assert (out["news"], out["attention"], out["sender"]) == ("no", "quieted", "neither"), out
        code, out, _ = cli("check-email", "boss@work.com")
        assert code == 0 and (out["attention"], out["sender"]) == ("open", "vip"), (code, out)
        code, out, _ = cli("check-email", "team@spitex.example")
        assert code == 0 and out["sender"] == "instructed", (code, out)
        code, out, _ = cli("check-email", "billing@acme.example")
        assert code == 0 and out["sender"] == "vip+instructed", (code, out)
        code, out, _ = cli("check-email", "no-reply@substack.com")
        assert code == 3 and (out["news"], out["sender"]) == ("yes", "neither"), (code, out)
        # The same question, asked of the channel: a wildcard counts there too.
        proc = subprocess.run([sys.executable, str(SCRIPT), "check-handle", "--channel", "email",
                               "billing@acme.example"], capture_output=True, text=True, env=env)
        assert proc.returncode == 0 and proc.stdout.split() == ["vip", "instructed"], proc.stdout
        # …and the channel's members are listed like any channel's.
        proc = subprocess.run([sys.executable, str(SCRIPT), "show", "--channel", "email"],
                              capture_output=True, text=True, env=env)
        assert "vip\tboss@work.com\n" in proc.stdout and "vip\t*@acme.example\t(address book)" in proc.stdout, proc.stdout
        assert "instructed\tteam@spitex.example\t(address book)" in proc.stdout, proc.stdout


def test_instructed_handles():
    """A person with a standing instruction projects as `instructed`: its own
    class beside VIP, sender-only, replaced wholesale by a sync, kept by every
    hand edit, and never implied by the gate switched off."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TRIAGE_MESSENGER_DIR"] = str(Path(tmp) / "messenger")
        os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(Path(tmp) / "email.nt")
        try:
            tp.sync_contacts({"signal": {"+41790000002"}},
                             instructed_handles={"signal": {"+41790000002", "+41790000005"},
                                                 "sms": {"+41790000005"}})
            pol = tp.load_messenger_policy("signal")
            assert pol.instructed_contacts == {"+41790000002", "+41790000005"}, pol
            text = tp.messenger_policy_path("signal").read_text(encoding="utf-8")
            assert ('<urn:retinue:triage:signal:contacts> <https://w3id.org/retinue/kb#triageInstructedHandle> '
                    '"+41790000005" .') in text, text
            # The two axes are independent: VIP and instructed, instructed
            # alone, and neither — in a group exactly as in a 1:1.
            both = tp.gate_decision("signal", "+41790000002")
            assert (both["vip"], both["instructed"]) == (True, True), both
            briefed = tp.gate_decision("signal", "+41790000005", "some-group")
            assert (briefed["vip"], briefed["instructed"]) == (False, True), briefed
            other = tp.gate_decision("signal", "+41790000009")
            assert (other["vip"], other["instructed"]) == (False, False), other
            assert tp.gate_decision("sms", "+41790000005")["instructed"] is True
            # A hand edit knows nothing of the projection and keeps it.
            tp._mutate_messenger("signal", vip_add=["+41790000001"], q_add=["grp"])
            assert tp.load_messenger_policy("signal").instructed_contacts == {"+41790000002", "+41790000005"}
            # Switched off, the gate makes everyone a VIP and nobody instructed:
            # the web gateway reads a VIP's instruction off the address book.
            off = tp.gate_decision("signal", "+41790000005", enabled=False)
            assert (off["vip"], off["instructed"]) == (True, False), off
            # The CLI's check says so on a line of its own.
            proc = subprocess.run([sys.executable, str(SCRIPT), "check-handle", "--channel", "signal",
                                   "+41790000005"], capture_output=True, text=True, env=os.environ)
            assert proc.stdout.split() == ["not-vip", "instructed"], proc.stdout
            # A sync without instructed handles clears the class only.
            tp.sync_contacts({"signal": {"+41790000002"}})
            pol = tp.load_messenger_policy("signal")
            assert pol.instructed_contacts == set() and pol.vip_contacts == {"+41790000002"}, pol
            assert pol.vip == {"+41790000001"}, pol
            # Another channel's projection gets its directory only when it has
            # members, as VIPs do.
            tp.sync_contacts({}, instructed_handles={"telegram": {"@lea_graf"}})
            assert tp.load_messenger_policy("telegram").instructed_contacts == {"@lea_graf"}
        finally:
            del os.environ["TRIAGE_MESSENGER_DIR"]
            del os.environ["TRIAGE_EMAIL_WHITELIST_PATH"]


# --------------------------------------------------------------------------- #
# Where the e-mail policy file lives                                          #
# --------------------------------------------------------------------------- #

class _PolicyEnv:
    """A temp chambers tree with the module's paths pointed into it.

    CHAMBERS_DIR is read at import, so the module constant is swapped for the
    duration; TRIAGE_MESSENGER_DIR and the override are cleared so the
    defaults are what is tested."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._saved = (tp.CHAMBERS_DIR, {k: os.environ.pop(k, None) for k in
                                         ("TRIAGE_MESSENGER_DIR", "TRIAGE_EMAIL_WHITELIST_PATH")})
        tp.CHAMBERS_DIR = root
        self.new = root / "_generated" / "messenger" / "email" / "policy" / "email-policy.nt"
        self.old = root / "_generated" / "triage" / "email-whitelist.nt"
        return self

    def __exit__(self, *exc):
        tp.CHAMBERS_DIR, env = self._saved
        for key, value in env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        self._tmp.cleanup()

    def write_old(self, content: str) -> None:
        self.old.parent.mkdir(parents=True, exist_ok=True)
        self.old.write_text(content, encoding="utf-8")


def _policy_with(**sets) -> str:
    return tp.render_email_policy(tp.EmailPolicy(**sets))


def test_email_policy_lives_beside_the_mail_channel_policy():
    with _PolicyEnv() as env:
        assert tp.email_whitelist_path() == env.new
        # The mail gateway's ledger volume: the same policy/ folder as the
        # channel's own policy file.
        assert tp.email_whitelist_path().parent == tp.messenger_policy_path("email").parent
        os.environ["TRIAGE_MESSENGER_DIR"] = str(env.new.parents[3] / "elsewhere")
        assert tp.email_whitelist_path() == env.new.parents[3] / "elsewhere" / "email" / \
            "policy" / "email-policy.nt"
        os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = "/inbound/policy/email-policy.nt"
        assert tp.email_whitelist_path() == Path("/inbound/policy/email-policy.nt")


def test_ensure_moves_the_old_file_content_and_all():
    with _PolicyEnv() as env:
        content = _policy_with(news={"bulletin@news.example"}, news_wildcards={"*@substack.com"},
                               ignored={"spam.list.example"})
        env.write_old(content)
        # Before the move a default read sees the policy in force, not nothing.
        assert tp.load_email_policy().news == {"bulletin@news.example"}
        got = tp.ensure_email_policy_location()
        assert got == {"path": str(env.new), "action": "moved", "legacy_left": None}, got
        assert env.new.read_text(encoding="utf-8") == content, "moved byte for byte"
        assert not env.old.exists()
        pol = tp.load_email_policy()
        assert pol.ignored == {"spam.list.example"} and pol.news_wildcards == {"*@substack.com"}
        # Idempotent.
        assert tp.ensure_email_policy_location()["action"] == "present"
        assert env.new.read_text(encoding="utf-8") == content


def test_ensure_creates_an_empty_file_when_there_is_none():
    """An empty file is "no flags"; no file is "not set up yet" — the mail
    gateway waits on the second before its first backfill."""
    with _PolicyEnv() as env:
        got = tp.ensure_email_policy_location()
        assert got["action"] == "created" and env.new.read_bytes() == b"", got
        assert tp.load_email_policy() == tp.EmailPolicy()
        assert tp.ensure_email_policy_location()["action"] == "present"
        assert not env.old.exists(), "nothing is created at the old place"


def test_ensure_leaves_a_differing_old_file_and_reports_it():
    with _PolicyEnv() as env:
        new = _policy_with(quieted={"new.list.example"})
        env.new.parent.mkdir(parents=True)
        env.new.write_text(new, encoding="utf-8")
        env.write_old(_policy_with(quieted={"old.list.example"}))
        got = tp.ensure_email_policy_location()
        assert got == {"path": str(env.new), "action": "present",
                       "legacy_left": str(env.old)}, got
        assert env.new.read_text(encoding="utf-8") == new, "the new file wins"
        assert env.old.exists(), "a flag only the old file holds is never deleted"
        # The same bytes on both sides is a move that crashed after its write.
        env.write_old(new)
        assert tp.ensure_email_policy_location()["legacy_left"] is None
        assert not env.old.exists()


def test_ensure_moves_nothing_for_an_explicit_path():
    """TRIAGE_EMAIL_WHITELIST_PATH is the operator saying where the file is;
    the default old location is then not this function's to empty."""
    with _PolicyEnv() as env:
        env.write_old(_policy_with(quieted={"old.list.example"}))
        pinned = env.new.parents[3] / "pinned.nt"
        os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(pinned)
        assert tp.ensure_email_policy_location()["action"] == "created"
        assert pinned.read_bytes() == b"" and env.old.exists()
        assert tp.load_email_policy() == tp.EmailPolicy(), "an explicit path is read as given"


def test_a_write_before_the_move_strands_no_flag():
    """A retinue-side writer that runs before anything placed the file (a
    flag set by hand, the contacts projection) moves it first, so the old
    flags travel with its change."""
    with _PolicyEnv() as env:
        env.write_old(_policy_with(news={"bulletin@news.example"}, quieted={"list.example"}))
        tp._mutate_email(news_add=["letters@news.example"])
        pol = tp.load_email_policy(env.new)
        assert pol.news == {"bulletin@news.example", "letters@news.example"}, pol
        assert pol.quieted == {"list.example"}, pol
        assert not env.old.exists()
    with _PolicyEnv() as env:
        # The contacts projection writes the `email` channel's own policy file
        # beside it, and still places this one: the mail gateway waits for it.
        content = _policy_with(news={"bulletin@news.example"}, quieted={"list.example"})
        env.write_old(content)
        tp.sync_contacts({"email": {"vip@x.org"}})
        assert env.new.read_text(encoding="utf-8") == content and not env.old.exists()
        assert tp.load_messenger_policy("email").vip_contacts == {"vip@x.org"}
        assert tp.messenger_policy_path("email") == env.new.with_name("policy.nt")


def test_the_cli_places_the_file():
    with _PolicyEnv() as env:
        env.write_old(_policy_with(ignored={"spam.list.example"}))
        assert tp.main(["show", "--channel", "signal"]) == 0
        assert env.new.exists() and not env.old.exists()


def test_place_email_policy_says_what_it_did_and_fails_loudly():
    """The entrypoint's first step: the placement alone, its result on stdout,
    a non-zero exit when the file cannot be put in place (the entrypoint
    then warns), never a silent success."""
    import contextlib
    import io
    with _PolicyEnv() as env:
        content = _policy_with(news_wildcards={"*@substack.com"})
        env.write_old(content)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert tp.main(["place-email-policy"]) == 0
        assert out.getvalue() == f"{env.new}: moved\n", out.getvalue()
        assert env.new.read_text(encoding="utf-8") == content and not env.old.exists()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert tp.main(["place-email-policy"]) == 0
        assert out.getvalue() == f"{env.new}: present\n", out.getvalue()
    with _PolicyEnv() as env:
        # A file where the policy folder should be: the new path cannot be
        # written.
        blocker = env.new.parents[1]
        blocker.parent.mkdir(parents=True)
        blocker.write_text("not a directory", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            assert tp.main(["place-email-policy"]) == 1
        assert "could not place the e-mail policy file" in err.getvalue(), err.getvalue()


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
