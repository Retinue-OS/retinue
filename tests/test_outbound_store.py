#!/usr/bin/env python3
"""Focused checks for the outbound half of the message ledger.

`scripts/inbound_store.py` now persists outbound messages (`kb:OutboundMessage`,
`write_outbound`) beside the inbound ones, both stamped with the `kb:chat` chat
key. This exercises the outbound round-trip, the kb:author enum validation, and
— the correctness hazard the type filter exists for — that a store holding both
directions never hands an outbound record to the triage drain: `undelivered()`
and `mark_delivered()` act on `kb:InboundMessage` only. Also covers the
`RecentSends` echo-dedup memory the gateways use to record each send once.

Standalone, no third-party deps:

    python3 tests/test_outbound_store.py
"""
import importlib.util
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "inbound_store.py"


def _load():
    spec = importlib.util.spec_from_file_location("inbound_store", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ist = _load()


def test_outbound_write_and_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        subj, path = ist.write_outbound(
            tmp, channel="signal", chat="group:abc123", text='sure "why not"\nsee you',
            author="user", message_id="1724832000000", timestamp=2000.0,
            attachment_urls=["http://gw:1/media/aa", "http://gw:1/media/aa"],
        )
        assert path.is_file()
        assert subj.startswith("urn:retinue:outbound:signal:")
        text = path.read_text(encoding="utf-8")
        # Deterministic: sorted, one triple per line, well-formed, no blank nodes.
        lines = [l for l in text.splitlines() if l]
        assert lines == sorted(lines), "not sorted"
        assert all(l.endswith(" .") for l in lines)
        assert "_:" not in text
        # Outbound carries sentAt (typed), never the inbound-only predicates.
        assert ist.P_SENT_AT in text
        assert "^^<http://www.w3.org/2001/XMLSchema#dateTime>" in text
        assert ist.P_DELIVERED not in text
        assert ist.P_SENDER not in text
        assert ist.P_RECEIVED_AT not in text
        fields = ist._parse(text)
        assert fields["type"] == ist.T_OUTBOUND
        assert fields["chat"] == "group:abc123"
        assert fields["author"] == "user"
        assert fields["text"] == 'sure "why not"\nsee you'
        assert fields["message_id"] == "1724832000000"
        assert fields["sent_at"] == ist._iso(2000.0)
        assert fields["attachments"] == ["http://gw:1/media/aa"], "not deduped"
        # The parser reads back everything the writer emits: re-rendering the
        # parsed fields reproduces the file byte for byte.
        assert ist._render(fields) == text
    print("PASS test_outbound_write_and_roundtrip")


def test_author_enum_validation():
    with tempfile.TemporaryDirectory() as tmp:
        for author in ist.AUTHORS:
            _, path = ist.write_outbound(tmp, channel="tg", chat="123",
                                         text="ok", author=author, timestamp=1.0)
            assert ist._parse(path.read_text(encoding="utf-8"))["author"] == author
        try:
            ist.write_outbound(tmp, channel="tg", chat="123", text="x",
                               author="assistant", timestamp=2.0)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid author must raise ValueError")
    print("PASS test_author_enum_validation")


def test_undelivered_ignores_outbound():
    with tempfile.TemporaryDirectory() as tmp:
        # One conversation, both directions in the same messages/ dir.
        ist.write_message(tmp, channel="whatsapp", sender="4179@s.whatsapp.net",
                          text="are we on for 3pm?", chat="4179@s.whatsapp.net",
                          timestamp=10.0)
        _, out_path = ist.write_outbound(tmp, channel="whatsapp",
                                         chat="4179@s.whatsapp.net", text="yes, 3pm",
                                         author="device", timestamp=20.0)
        before = out_path.read_text(encoding="utf-8")
        # The drain must surface ONLY the inbound message — an outbound record
        # parses with delivered defaulting to False, so without the type filter
        # it would be handed to triage as if it were inbound mail.
        got = ist.undelivered(tmp)
        assert [m["text"] for m in got] == ["are we on for 3pm?"]
        assert got[0]["chat"] == "4179@s.whatsapp.net"
        # A second drain is empty, and the outbound file was never rewritten
        # (no delivered flag grew on it).
        assert ist.undelivered(tmp) == []
        assert out_path.read_text(encoding="utf-8") == before
        assert ist.P_DELIVERED not in before
    print("PASS test_undelivered_ignores_outbound")


def test_mark_delivered_inbound_only():
    with tempfile.TemporaryDirectory() as tmp:
        _, in_path = ist.write_message(tmp, channel="signal", sender="+417900",
                                       text="hi", chat="+417900", timestamp=10.0)
        _, out_path = ist.write_outbound(tmp, channel="signal", chat="+417900",
                                         text="hello back", timestamp=20.0)
        # Inbound: the persist-before-forward flip still works.
        assert ist.mark_delivered(in_path) is True
        assert ist._parse(in_path.read_text(encoding="utf-8"))["delivered"] is True
        # Outbound: refused, and the file stays byte-identical.
        before = out_path.read_text(encoding="utf-8")
        assert ist.mark_delivered(out_path) is False
        assert out_path.read_text(encoding="utf-8") == before
    print("PASS test_mark_delivered_inbound_only")


def test_inbound_chat_key_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        ist.write_message(tmp, channel="telegram", sender="42", text="ping",
                          chat="-1002467", group="-1002467", timestamp=1.0)
        # A record written without a chat key (pre-chat files) keeps parsing.
        _, legacy = ist.write_message(tmp, channel="telegram", sender="7",
                                      text="old", timestamp=2.0)
        assert ist.P_CHAT not in legacy.read_text(encoding="utf-8")
        got = ist.undelivered(tmp)
        by_text = {m["text"]: m for m in got}
        assert by_text["ping"]["chat"] == "-1002467"
        assert by_text["old"]["chat"] is None
    print("PASS test_inbound_chat_key_roundtrip")


def test_file_without_type_is_legacy_inbound():
    with tempfile.TemporaryDirectory() as tmp:
        # Every file this store ever wrote carries an explicit type; one without
        # it can only predate kb:OutboundMessage and must keep the old contract
        # (drainable inbound), never be mistaken for a send.
        mdir = ist.messages_dir(tmp)
        mdir.mkdir(parents=True)
        subj = "urn:retinue:inbound:signal:feedbeef00000000"
        (mdir / "0000000000001000-feedbeef00000000.nt").write_text(
            f'<{subj}> <{ist.P_CHANNEL}> "signal" .\n'
            f'<{subj}> <{ist.P_DELIVERED}> "false" .\n'
            f'<{subj}> <{ist.P_RECEIVED_AT}> "1970-01-01T00:00:01Z"'
            f"^^<{ist.XSD_DATETIME}> .\n"
            f'<{subj}> <{ist.P_SENDER}> "+417900" .\n'
            f'<{subj}> <{ist.P_TEXT}> "untyped" .\n',
            encoding="utf-8",
        )
        got = ist.undelivered(tmp)
        assert [m["text"] for m in got] == ["untyped"]
    print("PASS test_file_without_type_is_legacy_inbound")


def test_recent_sends_dedup():
    rs = ist.RecentSends(maxlen=3)
    # Id-keyed: the echo of the gateway's own send matches; a different id from
    # the same chat/text (a coincidentally identical own-device send) does not,
    # because the txt fallback is only stored when no id was known.
    rs.note("m1", chat="+417900", text="ok")
    assert rs.seen("m1") is True
    assert rs.seen("m2", chat="+417900", text="ok") is False
    # Fallback-keyed: a send whose client reported no id is still recognized by
    # (chat, text) — even when the echo carries an id of its own.
    rs.note(None, chat="group:g1", text="on my way")
    assert rs.seen("m9", chat="group:g1", text="on my way") is True
    assert rs.seen(None, chat="group:g1", text="different") is False
    # Bounded: the oldest entries are evicted past maxlen.
    rs.note("a"); rs.note("b"); rs.note("c"); rs.note("d")
    assert rs.seen("a") is False and rs.seen("d") is True
    # Windowed: a stale entry no longer matches (backdated directly — the
    # window exists so the txt fallback cannot swallow an identical message
    # sent much later).
    rs2 = ist.RecentSends(window=60.0)
    rs2.note(None, chat="c", text="t")
    with rs2._lock:
        for key in rs2._entries:
            rs2._entries[key] = time.time() - 120.0
    assert rs2.seen(None, chat="c", text="t") is False
    print("PASS test_recent_sends_dedup")


def test_outbound_account_separates_identical_sends():
    """Two accounts sending the same words to the same peer are two events.

    For a send the account is not bookkeeping but the record of *who the
    recipient saw*: a message from the control account arrives as a stranger's
    contact request, one from the user's own account arrives in their thread.
    The ledger has to be able to say which happened.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _s, p1 = ist.write_outbound(tmp, channel="signal", chat="+41790000000",
                                    text="bis bald", author="agent",
                                    account="+41766029556", timestamp=100.0)
        _s, p2 = ist.write_outbound(tmp, channel="signal", chat="+41790000000",
                                    text="bis bald", author="user",
                                    account="+41765761976", timestamp=200.0)
        f1 = ist._parse(p1.read_text(encoding="utf-8"))
        f2 = ist._parse(p2.read_text(encoding="utf-8"))
        assert ist.P_ACCOUNT in p1.read_text(encoding="utf-8")
        assert f1["account"] == "+41766029556"
        assert f2["account"] == "+41765761976"
        assert f1["chat"] == f2["chat"] == "+41790000000"
        assert f1["text"] == f2["text"]
        # Round-trips byte for byte, like every other field.
        assert ist._render(f1) == p1.read_text(encoding="utf-8")

        # An outbound record still never reaches the triage drain, account or
        # not — the delivered ledger is inbound bookkeeping only.
        assert ist.undelivered(tmp) == []

        # Unset stays absent rather than empty (see the inbound sibling).
        _s, p3 = ist.write_outbound(tmp, channel="signal", chat="x", text="y",
                                    timestamp=300.0)
        assert ist.P_ACCOUNT not in p3.read_text(encoding="utf-8")
        assert ist._parse(p3.read_text(encoding="utf-8"))["account"] is None
    print("PASS test_outbound_account_separates_identical_sends")


def test_both_directions_share_one_timeline():
    with tempfile.TemporaryDirectory() as tmp:
        ist.write_message(tmp, channel="signal", sender="a", text="first",
                          chat="a", timestamp=100.0)
        ist.write_outbound(tmp, channel="signal", chat="a", text="second",
                           timestamp=200.0)
        ist.write_message(tmp, channel="signal", sender="a", text="third",
                          chat="a", timestamp=300.0)
        # One directory, filenames sorted by epoch millis = the conversation
        # in order, regardless of direction.
        texts = []
        for p in sorted(ist.messages_dir(tmp).glob("*.nt")):
            texts.append(ist._parse(p.read_text(encoding="utf-8"))["text"])
        assert texts == ["first", "second", "third"]
    print("PASS test_both_directions_share_one_timeline")


if __name__ == "__main__":
    test_outbound_write_and_roundtrip()
    test_author_enum_validation()
    test_undelivered_ignores_outbound()
    test_mark_delivered_inbound_only()
    test_inbound_chat_key_roundtrip()
    test_file_without_type_is_legacy_inbound()
    test_recent_sends_dedup()
    test_outbound_account_separates_identical_sends()
    test_both_directions_share_one_timeline()
    print("all outbound-store tests passed")
