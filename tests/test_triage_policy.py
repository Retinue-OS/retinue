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
    # `*@*.domain` reading the whitelist relies on.
    assert not tp.email_group_member("someone@sgcarney.substack.com", set(), wilds)
    assert tp.email_group_member("someone@sgcarney.substack.com", set(),
                                 {"*@*.substack.com"})
    # No accidental suffix matches.
    assert not tp.email_group_member("notsubstack.com", set(), wilds)
    assert not tp.email_group_member("", set(), wilds)
    # An exact entry wins regardless of shape.
    assert tp.email_group_member("Members.List.Example.org",
                                 {"members.list.example.org"}, set())


def test_email_gate_decision_table():
    pol = tp.EmailPolicy(
        addresses={"peer@list.example.org"},
        wildcards=set(),
        news={"digest.list.example.org"},
        news_wildcards={"*@substack.com"},
        quieted={"digest.list.example.org"},
        quieted_wildcards=set(),
        ignored={"noise.list.example.org"},
        ignored_wildcards={"*@substack.com"},
    )

    def dec(sender, list_id=""):
        return tp.email_gate_decision(sender, list_id, pol=pol)

    # Whitelisted sender: triaged now, whatever the list says.
    d = dec("peer@list.example.org", "<noise.list.example.org>")
    assert (d["triage_now"], d["daily"], d["reason"]) == (True, True, "whitelisted")
    # Ignored group: never a model turn.
    d = dec("stranger@x.com", "<noise.list.example.org>")
    assert (d["triage_now"], d["daily"]) == (False, False), d
    # Quieted group: the daily sweep, not the frequent run.
    d = dec("stranger@x.com", "<digest.list.example.org>")
    assert (d["triage_now"], d["daily"], d["news"]) == (False, True, True), d
    # Unknown group: same as quieted on a pull channel — nothing is lost.
    d = dec("stranger@x.com", "<other.list.example.org>")
    assert (d["triage_now"], d["daily"], d["news"]) == (False, True, False), d
    # news + ignored (the read-only newsletter): filed, never triaged.
    d = dec("no-reply@substack.com", "<sgcarney.substack.com>")
    assert (d["news"], d["triage_now"], d["daily"]) == (True, False, False), d
    assert d["group"] == "sgcarney.substack.com", d


def test_legacy_address_level_news_still_matches_a_list_mail():
    """A news entry written before List-Id detection keeps working.

    The entries in the live policy name sender addresses; once the gate started
    reading `List-Id`, the group for those same mails became the list. Checking
    news against the sender as well as the group is what stops that migration
    silently un-filing a newsletter.
    """
    pol = tp.EmailPolicy(addresses=set(), wildcards=set(),
                         news={"bulletin@news.example"}, news_wildcards=set())
    d = tp.email_gate_decision("bulletin@news.example",
                               "<letters.news.example>", pol=pol)
    assert d["news"] is True, d
    assert d["group"] == "letters.news.example", d


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
    sync replaces them wholesale, hand-set VIPs and the Sent-derived whitelist
    stay, writers that know nothing of them keep them, and a loader that reads
    only the predicate (a gateway built earlier) sees them too."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TRIAGE_MESSENGER_DIR"] = str(Path(tmp) / "messenger")
        os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(Path(tmp) / "email.nt")
        try:
            (Path(tmp) / "messenger" / "signal").mkdir(parents=True)
            tp._mutate_messenger("signal", vip_add=["+41790000001"])
            tp._mutate_email(add_addresses=["boss@work.com"])
            written = tp.sync_contacts({"signal": {"+41790000002"}, "matrix": set()}, {"Mara@Example.org"})
            assert len(written) == 2, written
            pol = tp.load_messenger_policy("signal")
            assert pol.vip == {"+41790000001"} and pol.vip_contacts == {"+41790000002"}, pol
            assert tp.gate_decision("signal", "+41790000002")["vip"] is True
            assert not (Path(tmp) / "messenger" / "matrix").exists(), "no directory for a channel with nobody"
            email = tp.load_email_policy()
            assert email.addresses == {"boss@work.com"} and email.contact_addresses == {"mara@example.org"}
            assert tp.email_gate_decision("mara@example.org")["triage_now"] is True
            assert "mara@example.org" in tp.load_email_whitelist()[0]
            # Writers that know nothing of the address book keep its members.
            tp._mutate_messenger("signal", news_add=["group-x"])
            tp._mutate_email(add_addresses=["peer@partner.com"])
            assert tp.load_messenger_policy("signal").vip_contacts == {"+41790000002"}
            assert tp.load_email_policy().contact_addresses == {"mara@example.org"}
            # An older loader reads the predicate, not the subject.
            text = tp.messenger_policy_path("signal").read_text(encoding="utf-8")
            vips = {lit for _s, p, lit in tp._parse(tp.messenger_policy_path("signal")) if p == tp.P_VIP_HANDLE}
            assert vips == {"+41790000001", "+41790000002"}, text
            # A sync that says nobody clears the projection only; unchanged, it writes nothing.
            tp.sync_contacts({}, set())
            assert tp.load_messenger_policy("signal").vip == {"+41790000001"}
            assert tp.load_messenger_policy("signal").vip_contacts == set()
            assert tp.load_email_policy().addresses == {"boss@work.com", "peer@partner.com"}
            assert tp.sync_contacts({}, set()) == []
        finally:
            del os.environ["TRIAGE_MESSENGER_DIR"]
            del os.environ["TRIAGE_EMAIL_WHITELIST_PATH"]


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
