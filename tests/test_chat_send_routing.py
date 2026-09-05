#!/usr/bin/env python3
"""Which account a messenger chat sends as — the incident this file exists for.

A deployment running two Signal accounts (the system bot and the user's own
number) had every chat stamped with the *built-in's* slug: each gateway used to
derive its own registry identity from a self-declared base URL that defaulted to
the built-in service name, so the personal account reported the system account's
slug on every rail event. A chat send then went out as the bot — over a
conversation the user cannot see, with the reply landing where they never look.
A gateway no longer states any address or slug about itself at all; it reports
only the account it sends as, and the reader matches that against the registry
it already holds.

The fix is identity-first, fail-closed routing, and this pins all of it:

- rail events are attributed by the *account* the gateway reports, not by the
  slug it derived about itself, and that is the only writer of a chat's stamp;
- only inbox-mode accounts may own a chat (control accounts carry prompts to
  Ara, never the user's correspondence);
- a stamp counts as authoritative only when its provenance says it came from a
  reported account. Mode alone would not do: the poisoned stamps named the
  built-in service, which may itself be inbox-mode, so an inbox check passes
  every one of them and the fix would be inert;
- ambiguity refuses the send instead of guessing an identity.

Standalone:

    python3 tests/test_chat_send_routing.py
"""
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

SYSTEM = "+41766020000"   # the control-mode bot account
PERSONAL = "+41791112233"  # the user's own number
CHAT = "signal:+41794456312"


def _load_gateway(tmp: Path):
    os.environ["CHAT_STATE_DIR"] = str(tmp / "chat-state")
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["PUSH_DIR"] = str(tmp / "push")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    for var in ("SIGNAL_GATEWAY_BASE_URL", "WHATSAPP_GATEWAY_BASE_URL",
                "TELEGRAM_GATEWAY_BASE_URL", "MESSENGER_GATEWAYS"):
        os.environ.pop(var, None)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_routing_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _registry(wg, **health):
    """Install a fake gateway registry plus the /health each one answers."""
    wg._CHANNEL_GATEWAYS = {
        slug: {"base_url": f"http://{slug}:8090", "token": "", "label": "Signal"}
        for slug in health
    }
    wg._fetch_gateway_health = lambda gw: dict(
        health[gw["base_url"].split("//", 1)[1].split(":", 1)[0]])
    with wg._gw_identity_lock:
        wg._gw_identity.clear()


def _inbox(account):
    return {"connected": True, "mode": "inbox", "account": account}


def _control(account):
    return {"connected": True, "mode": "control", "account": account}


def _stamp(source="account", slug="signal-gateway-personal"):
    return {"gateway": slug, "gateway_source": source}


def test_account_derived_stamp_is_used():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        _registry(wg, **{"signal-gateway": _control(SYSTEM),
                         "signal-gateway-personal": _inbox(PERSONAL)})
        slug, gw, err = wg._chat_gateway(_stamp(), "signal")
        assert err is None and slug == "signal-gateway-personal" and gw is not None
        # The same slug without provenance is not authoritative — it falls
        # through to re-derivation (here: the one inbox account, same answer).
        slug, gw, err = wg._chat_gateway(_stamp(source=None), "signal")
        assert err is None and slug == "signal-gateway-personal"
    print("PASS test_account_derived_stamp_is_used")


def test_wrong_but_inbox_stamp_is_not_trusted():
    """The gap that would have made this fix inert.

    The poisoning stamped the built-in's slug onto chats owned by another
    account. Where the built-in is itself inbox-mode, a mode-only check treats
    that stamp as legitimate and keeps sending as the wrong account. With two
    inbox candidates and no provenance the only honest answer is to refuse."""
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        _registry(wg, **{"signal-gateway": _inbox(SYSTEM),
                         "signal-gateway-personal": _inbox(PERSONAL)})
        poisoned = {"gateway": "signal-gateway"}  # no gateway_source
        slug, gw, err = wg._chat_gateway(poisoned, "signal")
        assert gw is None and slug is None, "an unmarked stamp must not route"
        assert "cannot tell which account" in err
        # The repair clears it even though it resolves to an inbox gateway.
        wg._CHAT_STATE.set_gateway(CHAT, "signal-gateway")
        assert wg.repair_chat_gateway_stamps() == 1
        assert wg._CHAT_STATE.get(CHAT)["gateway"] is None
        # A marked stamp for the same wrong-looking slug IS honoured: it was
        # established from an account, so it is that chat's account.
        wg._CHAT_STATE.set_gateway(CHAT, "signal-gateway", source="account")
        doc = wg._CHAT_STATE.get(CHAT)
        assert wg._chat_gateway(doc, "signal")[0] == "signal-gateway"
        assert wg.repair_chat_gateway_stamps() == 0, "must not clobber a marked stamp"
    print("PASS test_wrong_but_inbox_stamp_is_not_trusted")


def test_stamped_control_is_discarded_and_rerouted():
    """The incident, end to end: the poisoned stamp names the control account."""
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        _registry(wg, **{"signal-gateway": _control(SYSTEM),
                         "signal-gateway-personal": _inbox(PERSONAL)})
        # The chat as the incident left it: the built-in's slug, no provenance.
        wg._CHAT_STATE.set_gateway(CHAT, "signal-gateway")
        doc = wg._CHAT_STATE.get(CHAT)
        slug, gw, err = wg._chat_gateway(doc, "signal")
        assert err is None, err
        assert slug == "signal-gateway-personal", "must not send as the bot"
        # And the repair pass clears the bad stamp for good…
        assert wg.repair_chat_gateway_stamps() == 1
        assert wg._CHAT_STATE.get(CHAT)["gateway"] is None
        # …idempotently, and without touching an account-derived stamp.
        assert wg.repair_chat_gateway_stamps() == 0
        wg._CHAT_STATE.set_gateway(CHAT, "signal-gateway-personal", source="account")
        assert wg.repair_chat_gateway_stamps() == 0
        assert wg._CHAT_STATE.get(CHAT)["gateway"] == "signal-gateway-personal"
        # An unmarked stamp is cleared on the next pass, marked ones survive.
        wg._CHAT_STATE.set_gateway("signal:+41790001111", "signal-gateway-personal")
        assert wg.repair_chat_gateway_stamps() == 1
        assert wg.repair_chat_gateway_stamps() == 0
        assert wg._CHAT_STATE.get(CHAT)["gateway"] == "signal-gateway-personal"
    print("PASS test_stamped_control_is_discarded_and_rerouted")


def test_single_inbox_needs_no_stamp():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        _registry(wg, **{"signal-gateway": _inbox(PERSONAL)})
        slug, gw, err = wg._chat_gateway({}, "signal")
        assert err is None and slug == "signal-gateway"
    print("PASS test_single_inbox_needs_no_stamp")


def test_two_inbox_accounts_refuse():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        _registry(wg, **{"signal-gateway": _inbox(SYSTEM),
                         "signal-gateway-personal": _inbox(PERSONAL)})
        slug, gw, err = wg._chat_gateway({}, "signal")
        assert gw is None and slug is None
        assert "cannot tell which account" in err
        assert "signal-gateway" in err and "signal-gateway-personal" in err
    print("PASS test_two_inbox_accounts_refuse")


def test_chat_account_outranks_every_stamp():
    """A chat whose id names its account routes by that, not by state.

    The account in the id came from the kb:account the writing gateway stamped
    on this chat's own ledger records — a fact carried by the messages
    themselves. The gateway stamp is a cache this process maintains, and the
    incident was a poisoned one. So where both exist the id wins, and a wrong
    stamp cannot reach the send path at all.
    """
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        _registry(wg, **{"signal-gateway": _inbox(SYSTEM),
                         "signal-gateway-personal": _inbox(PERSONAL)})
        # The stamp names the system bot; the chat belongs to the user's own
        # number. Without the account this send went out as the bot.
        doc = _stamp(source="account", slug="signal-gateway")
        slug, gw, err = wg._chat_gateway(doc, "signal", PERSONAL)
        assert err is None and slug == "signal-gateway-personal", (slug, err)
        # And the other way round, so the id is doing the work rather than a
        # coincidence of ordering.
        slug, _gw, err = wg._chat_gateway(
            _stamp(source="account", slug="signal-gateway-personal"),
            "signal", SYSTEM)
        assert err is None and slug == "signal-gateway"
    print("PASS test_chat_account_outranks_every_stamp")


def test_two_inbox_accounts_route_when_the_chat_names_one():
    """The ambiguity that refuses a send is resolved by the id, not by a guess.

    With two inbox accounts on a channel an unattributed chat must refuse — the
    reader genuinely cannot tell whose it is. A chat that names its account is
    not ambiguous at all, and refusing it would leave the user unable to answer
    their own correspondence.
    """
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        _registry(wg, **{"signal-gateway": _inbox(SYSTEM),
                         "signal-gateway-personal": _inbox(PERSONAL)})
        # Unattributed: still refused, exactly as before.
        _slug, gw, err = wg._chat_gateway({}, "signal")
        assert gw is None and "cannot tell which account" in err
        # Named: routed, each to its own.
        for account, want in ((SYSTEM, "signal-gateway"),
                              (PERSONAL, "signal-gateway-personal")):
            slug, gw, err = wg._chat_gateway({}, "signal", account)
            assert err is None and slug == want, (account, slug, err)
    print("PASS test_two_inbox_accounts_route_when_the_chat_names_one")


def test_named_account_never_falls_back():
    """An id naming an account the registry cannot serve refuses outright.

    Falling through to the single-inbox rule would answer a known account's
    conversation as a different identity — the exact failure this file exists
    for, reintroduced through the back door.
    """
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        _registry(wg, **{"signal-gateway": _inbox(PERSONAL)})
        # Unknown account, and exactly one inbox gateway that would otherwise
        # have been picked without hesitation.
        slug, gw, err = wg._chat_gateway({}, "signal", "+15559990000")
        assert gw is None and slug is None
        assert "+15559990000" in err and "not a configured gateway" in err, err
        # The same account in control mode: the deployment has said this
        # identity is not for chats, so its history stays readable and
        # unsendable rather than being answered from somewhere else.
        _registry(wg, **{"signal-gateway": _control(SYSTEM),
                         "signal-gateway-personal": _inbox(PERSONAL)})
        slug, gw, err = wg._chat_gateway({}, "signal", SYSTEM)
        assert gw is None and slug is None
        assert "no longer an inbox-mode account" in err, err
    print("PASS test_named_account_never_falls_back")


def test_no_inbox_account_refuses():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        _registry(wg, **{"signal-gateway": _control(SYSTEM)})
        slug, gw, err = wg._chat_gateway({}, "signal")
        assert gw is None and err == "no inbox-mode gateway for channel signal"
        # A stamp naming that same control account changes nothing — marked or
        # not: provenance makes a stamp authoritative, it does not make a
        # control account eligible.
        slug, gw, err = wg._chat_gateway({"gateway": "signal-gateway"}, "signal")
        assert gw is None and "no inbox-mode gateway" in err
        slug, gw, err = wg._chat_gateway(_stamp(source="account",
                                                slug="signal-gateway"), "signal")
        assert gw is None and "no inbox-mode gateway" in err
    print("PASS test_no_inbox_account_refuses")


def test_health_blip_keeps_last_known_good():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        _registry(wg, **{"signal-gateway": _inbox(PERSONAL)})
        assert wg._chat_gateway({}, "signal")[0] == "signal-gateway"
        # The gateway goes unreachable: the cached identity carries the send.
        wg._fetch_gateway_health = lambda gw: {"connected": False, "reachable": False,
                                               "error": "gateway unreachable"}
        with wg._gw_identity_lock:  # force a re-probe rather than wait out the TTL
            for entry in wg._gw_identity.values():
                entry["at"] = 0.0
        slug, gw, err = wg._chat_gateway({}, "signal")
        assert err is None and slug == "signal-gateway", "a blip must not block sending"
    print("PASS test_health_blip_keeps_last_known_good")


def test_unknown_mode_is_never_eligible():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        # Never answered at all…
        _registry(wg, **{"signal-gateway": _inbox(PERSONAL)})
        wg._fetch_gateway_health = lambda gw: {"connected": False, "reachable": False}
        slug, gw, err = wg._chat_gateway({}, "signal")
        assert gw is None and "no inbox-mode gateway" in err
        # …and a gateway too old to report a mode is equally not eligible.
        _registry(wg, **{"signal-gateway": {"connected": True, "configured": True}})
        slug, gw, err = wg._chat_gateway({}, "signal")
        assert gw is None and "no inbox-mode gateway" in err
    print("PASS test_unknown_mode_is_never_eligible")


def test_rail_attributes_by_account_not_self_slug():
    """The root cause: the personal gateway reports the built-in's slug."""
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        _registry(wg, **{"signal-gateway": _control(SYSTEM),
                         "signal-gateway-personal": _inbox(PERSONAL)})
        # The account decides; the slug the event also carries (the built-in's,
        # for this container) is deliberately never consulted.
        assert wg._rail_gateway_slug("signal", PERSONAL) == "signal-gateway-personal"
        # A differently formatted number still matches (normalized compare).
        assert wg._rail_gateway_slug("signal", "+41 79 111 22 33") == \
            "signal-gateway-personal"
        # An account nothing serves leaves the chat unstamped rather than wrong.
        assert wg._rail_gateway_slug("signal", "+15550009999") is None
        # A gateway too old to report an account stamps nothing at all, rather
        # than falling back to the slug that caused the incident.
        assert wg._rail_gateway_slug("signal", None) is None
        assert wg._rail_gateway_slug("signal", "") is None
    print("PASS test_rail_attributes_by_account_not_self_slug")


def test_media_references_resolve_through_the_chats_account():
    """Both reference shapes are served through the chat's own account.

    A gateway records ``urn:retinue:media:<channel>:<id>`` — the blob, not a
    host. Records written when gateways still declared their own URL are the
    legacy ``http://<service>:<port>/media/<id>`` form and must keep
    rendering. Neither is trusted for *where*: the serving gateway is the
    chat's resolved account."""
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        mid = "ab" * 16
        urn = f"urn:retinue:media:signal:{mid}"
        legacy = f"http://signal-gateway:8090/media/{mid}"
        assert wg._parse_media_reference(urn) == (mid, None)
        assert wg._parse_media_reference(legacy) == (mid, "signal-gateway")
        assert wg._parse_media_reference("https://example.org/pic.jpg") == (None, None)
        assert wg._parse_media_reference("") == (None, None)

        # Resolved account wins for both shapes — including over the service
        # name a legacy record recorded about itself, which is the one that was
        # wrong for every extra account.
        for ref in (urn, legacy):
            att = wg._shape_chat_attachments(
                [ref], serving_slug="signal-gateway-personal")[0]
            assert att["url"] == f"/chats/media/signal-gateway-personal/{mid}"
            assert att["id"] == mid

        # No gateway to ask (a channel with none in the registry): a legacy
        # record falls back to its recorded host, a host-free one passes
        # through and plainly fails to load. Both are honest failures —
        # there is nobody to ask. (An *ambiguous* account is not this case:
        # see test_two_inbox_accounts_still_serve_media.)
        assert wg._shape_chat_attachments([legacy])[0]["url"] == \
            f"/chats/media/signal-gateway/{mid}"
        assert wg._shape_chat_attachments([urn])[0]["url"] == urn
    print("PASS test_media_references_resolve_through_the_chats_account")


def test_two_inbox_accounts_still_serve_media():
    """Serving a blob is not sending as an account.

    The defect: the send resolver's refusal ("cannot tell which account") was
    handed down as "no gateway may serve this chat's media", so every legacy
    chat of a two-account channel rendered broken pictures. A blob needs a
    gateway that *has* it, and a gateway asked for one it lacks says so —
    so the media resolver ranks the channel's gateways, most likely first,
    and refuses nothing. Sending keeps refusing exactly as before."""
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        _registry(wg, **{"signal-gateway": _inbox(SYSTEM),
                         "signal-gateway-personal": _inbox(PERSONAL)})
        _slug, gw, err = wg._chat_gateway({}, "signal")
        assert gw is None and "cannot tell which account" in err
        slugs = lambda *a, **k: [s for s, _ in wg._media_gateways(*a, **k)]  # noqa: E731
        # Unattributed: every inbox gateway of the channel, registry order.
        assert slugs("signal") == ["signal-gateway", "signal-gateway-personal"]
        # The chat's account names the gateway that stored its records: first.
        assert slugs("signal", PERSONAL) == ["signal-gateway-personal", "signal-gateway"]
        assert slugs("signal", SYSTEM) == ["signal-gateway", "signal-gateway-personal"]
        # An account-derived stamp ranks next; any other stamp is ignored.
        assert slugs("signal", None, _stamp()) == ["signal-gateway-personal", "signal-gateway"]
        assert slugs("signal", None, _stamp("legacy")) == ["signal-gateway", "signal-gateway-personal"]
        # Unknown account, unknown stamp: still the channel's gateways, never [].
        assert slugs("signal", "+41000000000", _stamp(slug="nope")) == \
            ["signal-gateway", "signal-gateway-personal"]
        # Another channel, or none: nobody to ask.
        assert slugs("whatsapp") == [] and slugs("") == []
        # The slug → channel reading the media handler uses to find siblings.
        assert wg._slug_channel("signal-gateway-personal") == "signal"
        assert wg._slug_channel("nope") is None

        # A control account never serves ledger media, not even ranked last.
        _registry(wg, **{"signal-gateway": _control(SYSTEM),
                         "signal-gateway-personal": _inbox(PERSONAL)})
        assert slugs("signal") == ["signal-gateway-personal"]
        assert slugs("signal", SYSTEM) == ["signal-gateway-personal"]
        # The whole ranking feeds the message payload: the first slug serves.
        mid = "ab" * 16
        att = wg._shape_chat_attachments([f"urn:retinue:media:signal:{mid}"],
                                         serving_slug=slugs("signal")[0])[0]
        assert att["url"] == f"/chats/media/signal-gateway-personal/{mid}"
    print("PASS test_two_inbox_accounts_still_serve_media")


def test_channel_membership():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        gw = {"base_url": "http://x:1", "label": "Signal (personal)"}
        assert wg._gateway_in_channel("signal-gateway", gw, "signal")
        assert wg._gateway_in_channel("signal-gateway-personal", gw, "signal")
        assert wg._gateway_in_channel("signal", gw, "signal")
        # Matched by label when the hostname says nothing about the channel.
        assert wg._gateway_in_channel("sig-box", gw, "signal")
        assert not wg._gateway_in_channel("whatsapp-gateway",
                                          {"label": "WhatsApp"}, "signal")
        assert not wg._gateway_in_channel("signalish-gateway",
                                          {"label": "Other"}, "signal")
    print("PASS test_channel_membership")


if __name__ == "__main__":
    test_account_derived_stamp_is_used()
    test_wrong_but_inbox_stamp_is_not_trusted()
    test_stamped_control_is_discarded_and_rerouted()
    test_single_inbox_needs_no_stamp()
    test_two_inbox_accounts_refuse()
    test_chat_account_outranks_every_stamp()
    test_two_inbox_accounts_route_when_the_chat_names_one()
    test_named_account_never_falls_back()
    test_no_inbox_account_refuses()
    test_health_blip_keeps_last_known_good()
    test_unknown_mode_is_never_eligible()
    test_rail_attributes_by_account_not_self_slug()
    test_media_references_resolve_through_the_chats_account()
    test_two_inbox_accounts_still_serve_media()
    test_channel_membership()
    print("all chat send-routing tests passed")
