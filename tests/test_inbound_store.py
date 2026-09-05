#!/usr/bin/env python3
"""Focused checks for the per-message inbound store (delivery ledger).

`scripts/inbound_store.py` persists each inbound messenger message as one
deterministic N-Triples file on the gateway volume, and owns the `delivered`
flag: `undelivered()` is the sole mutator, returning held messages and marking
them delivered in one pass. This exercises the round-trip, determinism, the
delivered semantics (drain once, never twice), the `since` filter, and the
"persist but do not forward" path (delivered=True at write time).

Standalone, no third-party deps:

    python3 tests/test_inbound_store.py
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


def test_write_and_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        subj, path = ist.write_message(
            tmp, channel="signal", sender="+41790000000",
            text='hi "there"\nsecond line', message_id="m1", timestamp=1000.0,
        )
        assert path.is_file()
        assert subj.startswith("urn:retinue:inbound:signal:")
        text = path.read_text(encoding="utf-8")
        # Deterministic: sorted, one triple per line, well-formed, no blank nodes.
        lines = [l for l in text.splitlines() if l]
        assert lines == sorted(lines), "not sorted"
        assert all(l.endswith(" .") for l in lines)
        assert "_:" not in text
        # Typed dateTime for receivedAt.
        assert "^^<http://www.w3.org/2001/XMLSchema#dateTime>" in text
        # Fresh message is owed to triage.
        fields = ist._parse(text)
        assert fields["delivered"] is False
        assert fields["sender"] == "+41790000000"
        assert fields["text"] == 'hi "there"\nsecond line'
    print("PASS test_write_and_roundtrip")


def test_account_marks_who_received_it():
    """kb:account: the identity the record belongs to, on inbound.

    A chat key names a peer only within one account, and a channel's message
    volume is shared by every account on it — so without this, two accounts
    writing to the same peer are indistinguishable in the store, which is what
    merged their conversations. Optional, because records written before the
    predicate existed carry none and a gateway that does not know its own
    account yet (Telegram before the session authorizes) writes none.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _s, path = ist.write_message(
            tmp, channel="signal", sender="+41790000000", text="hoi",
            chat="+41790000000", account="+41766029556", timestamp=1000.0)
        text = path.read_text(encoding="utf-8")
        assert ist.P_ACCOUNT in text
        fields = ist._parse(text)
        assert fields["account"] == "+41766029556"
        assert fields["chat"] == "+41790000000"
        # Nothing is invented: re-rendering reproduces the file byte for byte.
        assert ist._render(fields) == text

        # Same peer, other account: two records that a reader can tell apart
        # even though their chat key is identical.
        _s2, path2 = ist.write_message(
            tmp, channel="signal", sender="+41790000000", text="hoi",
            chat="+41790000000", account="+41765761976", timestamp=1001.0)
        f2 = ist._parse(path2.read_text(encoding="utf-8"))
        assert f2["chat"] == fields["chat"]
        assert f2["account"] != fields["account"]

        # Unset: the predicate is absent rather than written empty, so the
        # record is honestly unattributed instead of claiming an empty account.
        _s3, path3 = ist.write_message(
            tmp, channel="signal", sender="x", text="hoi", chat="x",
            timestamp=1002.0)
        text3 = path3.read_text(encoding="utf-8")
        assert ist.P_ACCOUNT not in text3
        assert ist._parse(text3)["account"] is None
    print("PASS test_account_marks_who_received_it")


def test_undelivered_drains_once():
    with tempfile.TemporaryDirectory() as tmp:
        ist.write_message(tmp, channel="signal", sender="a", text="one", timestamp=10.0)
        ist.write_message(tmp, channel="signal", sender="b", text="two", timestamp=20.0)
        first = ist.undelivered(tmp)
        assert [m["text"] for m in first] == ["one", "two"], "not oldest-first"
        # A second drain returns nothing — the flag was flipped in place.
        assert ist.undelivered(tmp) == []
        # But the files (and their content) remain — history stays browsable.
        for p in ist.messages_dir(tmp).glob("*.nt"):
            fields = ist._parse(p.read_text(encoding="utf-8"))
            assert fields["delivered"] is True
    print("PASS test_undelivered_drains_once")


def test_mark_delivered_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        # Persist-before-forward: two messages land delivered=false.
        _, path_a = ist.write_message(tmp, channel="signal", sender="a",
                                      text="accounted", timestamp=10.0)
        ist.write_message(tmp, channel="signal", sender="b",
                          text="failed-forward", timestamp=20.0)
        # "a" was actually accounted for (forward succeeded) → flip it.
        assert ist.mark_delivered(path_a) is True
        # Idempotent: a second flip on an already-true message still reports True
        # and does not re-write it into the drain.
        assert ist.mark_delivered(path_a) is True
        # The drain now surfaces only "b" — "a" is no longer owed, while "b",
        # left delivered=false (its forward failed), is still surfaced.
        got = ist.undelivered(tmp)
        assert [m["text"] for m in got] == ["failed-forward"]
        # Safe on a missing file: returns False, never raises.
        assert ist.mark_delivered(ist.messages_dir(tmp) / "nope.nt") is False
    print("PASS test_mark_delivered_roundtrip")


def test_since_filter():
    with tempfile.TemporaryDirectory() as tmp:
        ist.write_message(tmp, channel="wa", sender="a", text="old", timestamp=100.0)
        ist.write_message(tmp, channel="wa", sender="b", text="new", timestamp=200.0)
        # ISO cutoff between the two.
        cutoff = ist._iso(150.0)
        got = ist.undelivered(tmp, since=cutoff)
        assert [m["text"] for m in got] == ["new"]
        # The filtered-out "old" was NOT marked delivered, so a later unfiltered
        # drain still yields it.
        rest = ist.undelivered(tmp)
        assert [m["text"] for m in rest] == ["old"]
    print("PASS test_since_filter")


def test_persist_but_not_forward():
    with tempfile.TemporaryDirectory() as tmp:
        # A blacklisted / no-action-class message is stored already delivered.
        ist.write_message(tmp, channel="tg", sender="spam", text="x",
                          delivered=True, timestamp=5.0)
        assert ist.undelivered(tmp) == [], "delivered=True leaked into the drain"
        # Still on disk and browsable.
        files = list(ist.messages_dir(tmp).glob("*.nt"))
        assert len(files) == 1
    print("PASS test_persist_but_not_forward")


def test_group_and_optional_fields():
    with tempfile.TemporaryDirectory() as tmp:
        ist.write_message(tmp, channel="signal", sender="a", text="hey",
                          group="grp123", timestamp=1.0)
        ist.write_message(tmp, channel="signal", sender="b", text="ho",
                          timestamp=2.0)
        got = ist.undelivered(tmp)
        by_text = {m["text"]: m for m in got}
        assert by_text["hey"]["group"] == "grp123"
        assert by_text["ho"]["group"] is None
        assert by_text["ho"]["message_id"] is None
    print("PASS test_group_and_optional_fields")


def test_media_roundtrip_and_undelivered():
    with tempfile.TemporaryDirectory() as tmp:
        # A voice note persisted before transcription: empty text, a media ref.
        subj, path = ist.write_message(
            tmp, channel="whatsapp", sender="a", text="",
            media="/data/media/deadbeef.ogg", timestamp=1.0,
        )
        fields = ist._parse(path.read_text(encoding="utf-8"))
        assert fields["media"] == "/data/media/deadbeef.ogg"
        assert fields["text"] == ""
        assert fields["delivered"] is False
        # media survives the drain and is handed to the caller.
        got = ist.undelivered(tmp)
        assert len(got) == 1
        assert got[0]["media"] == "/data/media/deadbeef.ogg"
        assert got[0]["text"] == ""
    print("PASS test_media_roundtrip_and_undelivered")


def test_update_message_fills_transcript_and_clears_media():
    with tempfile.TemporaryDirectory() as tmp:
        subj, path = ist.write_message(
            tmp, channel="signal", sender="a", text="",
            media="/data/media/abc.ogg", timestamp=1.0,
        )
        # Transcription succeeded: fill the text, clear the media ref, learn which
        # file to unlink (the prior media value).
        prev = ist.update_message(path, text="hallo welt", clear_media=True)
        assert prev == "/data/media/abc.ogg"
        fields = ist._parse(path.read_text(encoding="utf-8"))
        assert fields["text"] == "hallo welt"
        assert fields["media"] is None
        # delivered flag is untouched by an update.
        assert fields["delivered"] is False
        # A second clear finds nothing left to unlink.
        assert ist.update_message(path, clear_media=True) is None
    print("PASS test_update_message_fills_transcript_and_clears_media")


def test_update_message_missing_file_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        assert ist.update_message(Path(tmp) / "nope.nt", text="x") is None
    print("PASS test_update_message_missing_file_returns_none")


def test_write_without_media_has_no_media_predicate():
    with tempfile.TemporaryDirectory() as tmp:
        _, path = ist.write_message(tmp, channel="tg", sender="a", text="plain",
                                    timestamp=1.0)
        text = path.read_text(encoding="utf-8")
        assert ist.P_MEDIA not in text
        assert ist._parse(text)["media"] is None
    print("PASS test_write_without_media_has_no_media_predicate")


def _png(w, h):
    """The smallest thing the store's sniffer reads as a PNG of w x h."""
    return (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")


def test_attachment_metadata_is_stated_in_the_record():
    """What the gateway knows about a blob it stored is in the record itself.

    A reader needs the content type before fetching (an image and a voice note
    are different elements) and the pixel size to reserve the box. Those are
    facts about the gateway's own store, so the gateway states them — on the
    media IRI, in the message's record — and no reader has to look at another
    service's files. The record's own subject stays the message."""
    with tempfile.TemporaryDirectory() as tmp:
        pic = ist.store_media(tmp, _png(320, 420), "image/png")
        note = ist.store_media(tmp, b"OggS" + b"\0" * 100, "audio/ogg")
        doc = ist.store_media(tmp, b"%PDF-1.7 x", "application/pdf",
                              file_name="../Rechnung\t2026.pdf")
        refs = [f"urn:retinue:media:signal:{pic}",
                f"urn:retinue:media:signal:{note}",
                f"urn:retinue:media:signal:{'ab' * 16}",  # not in this store
                f"urn:retinue:media:signal:{doc}"]
        subj, path = ist.write_message(
            tmp, channel="signal", sender="+41790000000", text="",
            timestamp=1000.0, attachment_urls=refs, media="/tmp/retained")
        text = path.read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if l]
        assert lines == sorted(lines), "still deterministic"
        assert f"<{refs[0]}> <{ist.P_CONTENT_TYPE}> \"image/png\" ." in text
        assert f"<{refs[0]}> <{ist.P_WIDTH}> \"320\"^^<{ist.XSD_INTEGER}> ." in text
        assert f"<{refs[0]}> <{ist.P_HEIGHT}> \"420\"^^<{ist.XSD_INTEGER}> ." in text
        assert f"<{refs[0]}> <{ist.P_BYTE_SIZE}> \"{len(_png(320, 420))}\"^^" in text
        assert f"<{refs[1]}> <{ist.P_CONTENT_TYPE}> \"audio/ogg\" ." in text
        assert f"<{refs[1]}> <{ist.P_WIDTH}>" not in text, "no guessed pixel size for audio"
        assert refs[2] in text and f"<{refs[2]}> <{ist.P_CONTENT_TYPE}>" not in text, \
            "a blob this store does not hold gets no statement — nothing is invented"
        # A document carries the name the sender gave it, made safe: base name
        # only, control characters gone; a photo has none.
        assert f"<{refs[3]}> <{ist.P_FILE_NAME}> \"Rechnung2026.pdf\" ." in text
        assert f"<{refs[0]}> <{ist.P_FILE_NAME}>" not in text
        # The parse keeps the message as the subject, whatever sorts last.
        fields = ist._parse(text)
        assert fields["subject"] == subj
        assert sorted(fields["attachments"]) == sorted(refs)  # the record is sorted
        assert fields["attachment_meta"][refs[0]] == {
            "content_type": "image/png", "size": len(_png(320, 420)),
            "width": 320, "height": 420}
        assert fields["attachment_meta"][refs[1]] == {"content_type": "audio/ogg", "size": 104}
        assert refs[2] not in fields["attachment_meta"]
        assert fields["attachment_meta"][refs[3]] == {
            "content_type": "application/pdf", "size": 10, "file_name": "Rechnung2026.pdf"}
        # What a sender may call a file, reduced to what is safe to show.
        assert ist.safe_file_name("C:\\Users\\x\\Bericht.docx") == "Bericht.docx"
        assert ist.safe_file_name("..") is None and ist.safe_file_name("  ") is None
        long = ist.safe_file_name("a" * 300 + ".pdf")
        assert len(long) == 200 and long.endswith(".pdf")
        # The statements survive every in-place rewrite of the record.
        ist.update_message(path, text="transcript", clear_media=True)
        assert ist.mark_delivered(path)
        again = ist._parse(path.read_text(encoding="utf-8"))
        assert again["text"] == "transcript" and again["delivered"] is True
        assert again["attachment_meta"] == fields["attachment_meta"]
        assert again["media"] is None
        # Outbound records state the same about the blobs they reference.
        _subj, opath = ist.write_outbound(
            tmp, channel="signal", chat="+41790000000", text="", author="device",
            timestamp=1001.0, attachment_urls=[refs[0]])
        assert f"<{refs[0]}> <{ist.P_CONTENT_TYPE}> \"image/png\" ." in opath.read_text()
    print("PASS test_attachment_metadata_is_stated_in_the_record")


def test_backfill_states_metadata_on_older_records():
    """Records written before the metadata existed get it from the same store.

    Each gateway backfills its own store at startup: a record that references
    a blob the store holds but says less about it than the store knows is
    rewritten; everything else is left alone. Idempotent, so a restart costs
    nothing. Legacy ``http://<service>/media/<id>`` references name the same
    blobs and get the same statements."""
    with tempfile.TemporaryDirectory() as tmp:
        pic = ist.store_media(tmp, _png(8, 6), "image/png")
        note = ist.store_media(tmp, b"OggS" + b"\0" * 10, "audio/ogg",
                               file_name="memo.ogg")
        urn = f"urn:retinue:media:signal:{pic}"
        legacy = f"http://signal-gateway:8090/media/{note}"
        gone = f"urn:retinue:media:signal:{'cd' * 16}"

        def old_record(ts, **kw):
            # A record as an older gateway wrote it: the reference, no statements.
            _s, path = ist.write_message(tmp, channel="signal", sender="+4179",
                                         text="x", timestamp=ts, **kw)
            fields = ist._parse(path.read_text(encoding="utf-8"))
            fields["attachment_meta"] = {}
            path.write_text(ist._render(fields), encoding="utf-8")
            return path

        p_urn = old_record(1000.0, attachment_urls=[urn])
        p_legacy = old_record(1001.0, attachment_urls=[legacy])
        p_gone = old_record(1002.0, attachment_urls=[gone])
        p_plain = old_record(1003.0)
        before = {p.name: p.read_text() for p in (p_urn, p_legacy, p_gone, p_plain)}
        assert ist.P_CONTENT_TYPE not in before[p_urn.name]

        assert ist.backfill_media_meta(tmp) == 2
        assert f"<{urn}> <{ist.P_CONTENT_TYPE}> \"image/png\" ." in p_urn.read_text()
        assert f"<{urn}> <{ist.P_WIDTH}> \"8\"^^" in p_urn.read_text()
        assert f"<{legacy}> <{ist.P_CONTENT_TYPE}> \"audio/ogg\" ." in p_legacy.read_text()
        assert f"<{legacy}> <{ist.P_FILE_NAME}> \"memo.ogg\" ." in p_legacy.read_text()
        assert p_gone.read_text() == before[p_gone.name], "nothing to state, untouched"
        assert p_plain.read_text() == before[p_plain.name]
        # Idempotent: the second pass rewrites nothing.
        assert ist.backfill_media_meta(tmp) == 0
        # A record that already states everything is not rewritten either.
        _s, fresh = ist.write_message(tmp, channel="signal", sender="+4179", text="y",
                                      timestamp=1004.0, attachment_urls=[urn])
        stamp = fresh.stat().st_mtime_ns
        assert ist.backfill_media_meta(tmp) == 0 and fresh.stat().st_mtime_ns == stamp
        # No store at all: nothing to do, no error.
        assert ist.backfill_media_meta(Path(tmp) / "nope") == 0
        # The reference reader behind it all.
        assert ist.media_id_of(urn) == pic and ist.media_id_of(legacy) == note
        assert ist.media_id_of("https://example.org/pic.jpg") is None
        assert ist.media_id_of("") is None
    print("PASS test_backfill_states_metadata_on_older_records")


def test_missing_dir_is_empty():
    with tempfile.TemporaryDirectory() as tmp:
        assert ist.undelivered(Path(tmp) / "nope") == []
    print("PASS test_missing_dir_is_empty")


def test_since_epoch_and_iso_equivalent():
    with tempfile.TemporaryDirectory() as tmp:
        ist.write_message(tmp, channel="signal", sender="a", text="m", timestamp=300.0)
        # Epoch and ISO cutoffs agree.
        assert ist._parse_since(250.0) == 250.0
        assert abs(ist._parse_since(ist._iso(250.0)) - 250.0) < 1.0
        got = ist.undelivered(tmp, since=250.0)
        assert [m["text"] for m in got] == ["m"]
    print("PASS test_since_epoch_and_iso_equivalent")


if __name__ == "__main__":
    test_write_and_roundtrip()
    test_account_marks_who_received_it()
    test_undelivered_drains_once()
    test_mark_delivered_roundtrip()
    test_since_filter()
    test_persist_but_not_forward()
    test_group_and_optional_fields()
    test_media_roundtrip_and_undelivered()
    test_update_message_fills_transcript_and_clears_media()
    test_update_message_missing_file_returns_none()
    test_write_without_media_has_no_media_predicate()
    test_attachment_metadata_is_stated_in_the_record()
    test_backfill_states_metadata_on_older_records()
    test_missing_dir_is_empty()
    test_since_epoch_and_iso_equivalent()
    print("all inbound_store tests passed")
