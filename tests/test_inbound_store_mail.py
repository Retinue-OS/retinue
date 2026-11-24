#!/usr/bin/env python3
"""The message ledger's mail record (docs/mail-chats.md, "The record").

`scripts/inbound_store.py` learns what the mail gateway needs to mirror a
mailbox: a mail's subject, From name, To/Cc, threading headers, List-Id, its
three parts as blobs, inline Content-IDs, how its text was trimmed, and when
it was found gone. This pins that every one of those survives every rewrite
the module performs — the renderer used to drop silently what it did not know
— and goes further: a well-formed triple whose predicate this version does
not know survives too. Then the new operations: the tombstone, re-keying a
chat on a thread merge, part blobs on a chat erasure, and the reader a
gateway rebuilds its indexes from.

Standalone, no third-party deps:

    python3 tests/test_inbound_store_mail.py
"""
import importlib.util
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "inbound_store.py"


def _load():
    spec = importlib.util.spec_from_file_location("inbound_store", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ist = _load()

ACCOUNT = "me@example.org"
MAIL_PREDICATES = (ist.P_SUBJECT, ist.P_SENDER_NAME, ist.P_RECIPIENT, ist.P_CC,
                   ist.P_REPLY_TO, ist.P_IN_REPLY_TO, ist.P_REFERENCES, ist.P_LIST_ID, ist.P_BODY_PART,
                   ist.P_HTML_PART, ist.P_HEADER_PART, ist.P_CONTENT_ID, ist.P_TRIM,
                   ist.P_REMOVED_AT, ist.P_ARRIVED_AT, ist.P_REFUSED, ist.P_SENT_AS)


def _png(w, h):
    return (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")


def _ref(mid):
    return f"urn:retinue:media:email:{mid}"


def _mail(tmp, *, outbound=False, chat="root@x.org", timestamp=1000.0, **over):
    """One mail with every field, its parts and an inline image stored first
    (so the blob facts are stated). Returns (urn, path, refs)."""
    body = ist.store_media(tmp, b"Hello Ann,\nthe numbers.\n", "text/plain; charset=utf-8")
    html = ist.store_media(tmp, b"<p>Hello Ann,</p>", "text/html")
    head = ist.store_media(tmp, b"From: Ann <a@x.org>\r\n", "text/plain")
    pic = ist.store_media(tmp, _png(40, 20), "image/png", file_name="chart.png")
    doc = ist.store_media(tmp, b"%PDF-1.7", "application/pdf", file_name="Q3.pdf")
    refs = {"body": _ref(body), "html": _ref(html), "head": _ref(head),
            "pic": _ref(pic), "doc": _ref(doc)}
    kw = dict(channel="email", chat=chat, account=ACCOUNT,
              text="the numbers", message_id=f"m{int(timestamp)}@x.org",
              timestamp=timestamp, attachment_urls=[refs["pic"], refs["doc"]],
              subject="Re: Quarterly numbers — Q3", sender_name="Ann Example",
              recipients=["me@example.org", "b@y.org"], cc=["c@z.org"],
              # Whom an answer goes to (a list's address, an assistant's).
              reply_to=["desk@x.org", " ", "second@x.org", "desk@x.org"],
              in_reply_to="root@x.org", references=["root@x.org", "mid@x.org"],
              list_id="team.lists.x.org", body_part=refs["body"], html_part=refs["html"],
              header_part=refs["head"], trim="deterministic",
              content_ids={refs["pic"]: "img1@x.org", "urn:not-an-attachment": "x"},
              # The server received it a minute after the Date it carries.
              arrived_at=timestamp + 60.0)
    kw.update(over)
    if outbound:
        urn, path = ist.write_outbound(tmp, author="device", sender=ACCOUNT, **kw)
    else:
        urn, path = ist.write_message(tmp, sender="a@x.org", **kw)
    return urn, path, refs


def _assert_mail_fields(fields, refs):
    assert fields["subject_line"] == "Re: Quarterly numbers — Q3", fields
    assert fields["sender_name"] == "Ann Example"
    assert sorted(fields["recipients"]) == ["b@y.org", "me@example.org"]
    assert fields["cc"] == ["c@z.org"]
    assert sorted(fields["reply_to"]) == ["desk@x.org", "second@x.org"]
    assert fields["in_reply_to"] == "root@x.org"
    assert fields["references"] == "root@x.org mid@x.org"
    assert fields["list_id"] == "team.lists.x.org"
    record_time = ist._parse_since(fields.get("sent_at") or fields["received_at"])
    assert fields["arrived_at"] == ist._iso(record_time + 60.0), fields["arrived_at"]
    assert (fields["body_part"], fields["html_part"], fields["header_part"]) == (
        refs["body"], refs["html"], refs["head"])
    assert fields["trim"] == "deterministic"
    meta = fields["attachment_meta"]
    assert meta[refs["pic"]]["content_id"] == "img1@x.org"
    assert "content_id" not in meta[refs["doc"]]
    assert "urn:not-an-attachment" not in meta
    # The blob facts are stated for the parts like for an attachment.
    assert meta[refs["body"]] == {"content_type": "text/plain; charset=utf-8", "size": 24}
    assert meta[refs["html"]]["content_type"] == "text/html"
    assert meta[refs["head"]]["size"] == len(b"From: Ann <a@x.org>\r\n")
    assert meta[refs["pic"]]["width"] == 40 and meta[refs["pic"]]["file_name"] == "chart.png"


def test_every_field_is_written_and_read_back():
    with tempfile.TemporaryDirectory() as tmp:
        urn, path, refs = _mail(tmp)
        text = path.read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if l]
        assert lines == sorted(lines) and all(l.endswith(" .") for l in lines)
        assert f'<{urn}> <{ist.P_SUBJECT}> "Re: Quarterly numbers — Q3" .' in text
        assert f'<{urn}> <{ist.P_RECIPIENT}> "b@y.org" .' in text
        assert f'<{urn}> <{ist.P_REFERENCES}> "root@x.org mid@x.org" .' in text
        # Two times: the mail's own (its Date) and when the server received it.
        assert (f'<{urn}> <{ist.P_RECEIVED_AT}> "{ist._iso(1000.0)}"^^<{ist.XSD_DATETIME}> .'
                in text)
        assert (f'<{urn}> <{ist.P_ARRIVED_AT}> "{ist._iso(1060.0)}"^^<{ist.XSD_DATETIME}> .'
                in text)
        # The parts are IRIs, never attachments: a chat shows attachments as
        # files, and the original is not a file the sender attached.
        assert f'<{urn}> <{ist.P_BODY_PART}> <{refs["body"]}> .' in text
        assert f'<{urn}> <{ist.P_ATTACHMENT}> <{refs["body"]}> .' not in text
        assert f'<{refs["pic"]}> <{ist.P_CONTENT_ID}> "img1@x.org" .' in text
        assert f'<{refs["html"]}> <{ist.P_CONTENT_TYPE}> "text/html" .' in text
        fields = ist._parse(text)
        assert fields["subject"] == urn, "the record's own URN stays 'subject'"
        _assert_mail_fields(fields, refs)
        assert sorted(fields["attachments"]) == sorted([refs["pic"], refs["doc"]])
        assert ist._render(fields) == text, "byte for byte"

        # Outbound states its From too, and the same mail fields.
        ourn, opath, orefs = _mail(tmp, outbound=True, timestamp=1001.0)
        otext = opath.read_text(encoding="utf-8")
        assert f'<{ourn}> <{ist.P_SENDER}> "{ACCOUNT}" .' in otext
        assert ist.P_DELIVERED not in otext and ist.P_RECEIVED_AT not in otext
        ofields = ist._parse(otext)
        assert ofields["sender"] == ACCOUNT and ofields["author"] == "device"
        _assert_mail_fields(ofields, orefs)
        assert ist._render(ofields) == otext

        # References given as one string are stored the same way.
        _u, p2, _r = _mail(tmp, timestamp=1002.0, references="  <a@x>\n\t<b@x>  ")
        assert ist._parse(p2.read_text())["references"] == "<a@x> <b@x>", \
            "whitespace folded, ids kept as given"
    print("PASS test_every_field_is_written_and_read_back")


def test_every_field_survives_every_rewrite():
    with tempfile.TemporaryDirectory() as tmp:
        _u, path, refs = _mail(tmp)
        _u, opath, orefs = _mail(tmp, outbound=True, timestamp=1001.0)

        ist.update_message(path, text="the numbers, trimmed better", trim="model")
        f = ist._parse(path.read_text())
        assert f["text"] == "the numbers, trimmed better" and f["trim"] == "model"
        f["trim"] = "deterministic"  # what _assert_mail_fields expects
        _assert_mail_fields(f, refs)

        assert ist.mark_delivered(path)
        _assert_mail_fields({**ist._parse(path.read_text()), "trim": "deterministic"}, refs)

        _u, p3, refs3 = _mail(tmp, timestamp=1003.0)
        drained = ist.undelivered(tmp)
        assert len(drained) == 1 and drained[0]["subject"] == _u
        _assert_mail_fields(ist._parse(p3.read_text()), refs3)

        # The startup backfill: drop the blob facts as an older writer would
        # have; they come back, and the Content-ID (no sidecar holds it) stays.
        for p in (p3, opath):
            fields = ist._parse(p.read_text())
            for ref, meta in fields["attachment_meta"].items():
                fields["attachment_meta"][ref] = (
                    {"content_id": meta["content_id"]} if "content_id" in meta else {})
            p.write_text(ist._render(fields))
        assert ist.P_CONTENT_TYPE not in p3.read_text()
        assert ist.backfill_media_meta(tmp) == 2
        _assert_mail_fields(ist._parse(p3.read_text()), refs3)
        _assert_mail_fields(ist._parse(opath.read_text()), orefs)
        assert ist.backfill_media_meta(tmp) == 0, "a Content-ID forces no rewrite"

        before = opath.read_text()
        ist.update_message(opath, chat="other-root@x.org")
        after = ist._parse(opath.read_text())
        assert after["chat"] == "other-root@x.org"
        _assert_mail_fields(after, orefs)
        was = ist._parse(before)
        for key in ("subject", "sent_at", "message_id", "author", "sender", "account"):
            assert after[key] == was[key], key
    print("PASS test_every_field_survives_every_rewrite")


def test_absent_fields_write_nothing():
    """Every existing caller is unaffected: no mail argument, no mail triple."""
    with tempfile.TemporaryDirectory() as tmp:
        _s, p_in = ist.write_message(tmp, channel="signal", sender="+4179", text="hoi",
                                     chat="+4179", account="+4170", message_id="1",
                                     timestamp=1.0)
        _s, p_out = ist.write_outbound(tmp, channel="signal", chat="+4179", text="ho",
                                       account="+4170", timestamp=2.0)
        # Empty values are absence too, not an empty triple.
        _s, p_empty = ist.write_message(tmp, channel="email", sender="a@x", text="x",
                                        subject="  ", recipients=["", " "], cc=[],
                                        references=[], content_ids={}, trim=None,
                                        timestamp=3.0)
        for p in (p_in, p_out, p_empty):
            text = p.read_text()
            for pred in MAIL_PREDICATES:
                assert f"<{pred}>" not in text, (p.name, pred)
            fields = ist._parse(text)
            assert fields["recipients"] == [] and fields["subject_line"] is None
            assert fields["extra"] == []
            assert ist._render(fields) == text
        assert ist.P_SENDER not in p_out.read_text(), "a messenger send states no From"
        # A trim outside the three values is a programming error.
        for call in (lambda: ist.write_message(tmp, channel="email", sender="a", text="",
                                               trim="greedy"),
                     lambda: ist.update_message(p_in, trim="greedy")):
            try:
                call()
            except ValueError:
                continue
            raise AssertionError("an unknown trim must raise ValueError")
    print("PASS test_absent_fields_write_nothing")


def test_unknown_triples_survive_every_rewrite():
    """A triple this version does not know is kept verbatim, not dropped.

    The renderer used to emit only the fields it knew, so anything a newer
    writer stated vanished the first time an older one marked the record
    delivered. Now a well-formed triple on the record, or on an IRI it
    references, is carried through every rewrite as it stands; one about an
    unrelated subject is still noise, and a malformed line still makes the
    file unreadable, exactly as before."""
    with tempfile.TemporaryDirectory() as tmp:
        urn, path, refs = _mail(tmp)
        future_blob = _ref("ef" * 16)
        foreign = [
            f'<{urn}> <https://w3id.org/retinue/kb#folder> "INBOX" .',
            f'<{urn}> <https://w3id.org/retinue/kb#uid> "4711"^^<{ist.XSD_INTEGER}> .',
            f'<{urn}> <https://w3id.org/retinue/kb#seenAt> "2026-01-01T00:00:00Z"'
            f'^^<{ist.XSD_DATETIME}> .',
            f'<{urn}> <https://w3id.org/retinue/kb#calendarPart> <{future_blob}> .',
            f'<{future_blob}> <{ist.P_CONTENT_TYPE}> "text/calendar" .',
            f'<{refs["pic"]}> <https://w3id.org/retinue/kb#altText> "a chart" .',
            f'<{refs["body"]}> <https://w3id.org/retinue/kb#charset> "utf-8" .',
            f'<{refs["doc"]}> <{ist.P_BYTE_SIZE}> "not a number"^^<{ist.XSD_INTEGER}> .',
        ]
        noise = '<urn:somewhere:else> <https://w3id.org/retinue/kb#x> "y" .'
        path.write_text(path.read_text() + "\n".join(foreign + [noise]) + "\n")
        fields = ist._parse(path.read_text())
        assert len(fields["extra"]) == len(foreign) - 1, fields["extra"]
        assert fields["attachment_meta"][future_blob] == {"content_type": "text/calendar"}

        def check(note):
            text = path.read_text()
            for line in foreign:
                assert line in text, (note, line)
            assert noise not in text, note
            assert ist._parse(text) is not None
            _assert_mail_fields({**ist._parse(text), "trim": "deterministic"}, refs)

        ist.update_message(path, text="t2", trim="model")
        check("update_message")
        ist.update_message(path, chat="new-root@x.org")
        check("re-key")
        assert ist.mark_delivered(path) is True
        check("mark_delivered")
        _u, p2, refs2 = _mail(tmp, timestamp=1001.0)
        p2.write_text(p2.read_text() + foreign[0].replace(urn, _u) + "\n")
        ist.undelivered(tmp)
        assert foreign[0].replace(urn, _u) in p2.read_text(), "undelivered"
        # Force the backfill to rewrite this record (drop one stated fact).
        f = ist._parse(path.read_text())
        f["attachment_meta"][refs["html"]] = {}
        path.write_text(ist._render(f))
        assert ist.backfill_media_meta(tmp) >= 1
        check("backfill_media_meta")
        assert ist._render(ist._parse(path.read_text())) == path.read_text(), "stable"

        # A statement about an attachment the record lets go of goes with it.
        f = ist._parse(path.read_text())
        f["attachments"] = [refs["doc"]]
        out = ist._render(f)
        assert foreign[5] not in out and foreign[7] in out

        # Strictness is unchanged: one line that is no triple, no record.
        broken = path.read_text() + f'<{urn}> <https://w3id.org/retinue/kb#x> "de"@de .\n'
        assert ist._parse(broken) is None
        assert ist._parse(path.read_text() + "garbage\n") is None
    print("PASS test_unknown_triples_survive_every_rewrite")


def test_unsafe_characters_keep_the_record_readable():
    """A mail body carries form feeds, pasted line separators and, from a
    broken header, undecodable bytes. Written raw, a character that splits a
    line would cut its triple in two and the whole record would go dark."""
    with tempfile.TemporaryDirectory() as tmp:
        text = "page one\x0cpage two next para\x85nel\x00\x1b[0m\x7f tab\tend\\u0041"
        _s, path = ist.write_message(tmp, channel="email", sender="a@x", text=text,
                                     subject="bad \udcff byte", timestamp=1.0)
        raw = path.read_text(encoding="utf-8")
        assert len([l for l in raw.split("\n") if l]) == len(raw.splitlines()), \
            "no character a line splitter breaks on is written raw"
        fields = ist._parse(raw)
        assert fields["text"] == text, repr(fields["text"])
        assert fields["subject_line"] == "bad � byte"
        assert ist._render(fields) == raw
        assert ist.mark_delivered(path) and ist._parse(path.read_text())["text"] == text
        # What another writer may have left raw in a literal is still read.
        urn = "urn:retinue:inbound:email:feed"
        hand = (f'<{urn}> <{ist.P_CHANNEL}> "email" .\n'
                f'<{urn}> <{ist.P_TEXT}> "raw\x0cfeed\u2028line" .\n')
        assert ist._parse(hand)["text"] == "raw\x0cfeed\u2028line"
        # A malformed escape reads as it always did.
        assert ist._unesc("\\u12") == "u12" and ist._unesc("\\U0001F600") == "\U0001F600"
    print("PASS test_unsafe_characters_keep_the_record_readable")


def test_tombstone():
    with tempfile.TemporaryDirectory() as tmp:
        urn, path, refs = _mail(tmp)
        # Another record of the store shares the PDF (a forward of it, say).
        _s, sharer = ist.write_outbound(tmp, channel="email", chat="root@x.org",
                                        account=ACCOUNT, text="fwd", author="user",
                                        timestamp=1001.0, attachment_urls=[refs["doc"]])
        before = ist._parse(path.read_text())
        before["extra"].append(f'<{urn}> <https://w3id.org/retinue/kb#summary> "secret" .')
        path.write_text(ist._render(before))
        assert ist.tombstone_message(path, removed_at=2000.0) is True
        text = path.read_text()
        after = ist._parse(text)
        # Kept: who, where, when (both times), and the threading ids.
        for key in ("type", "channel", "chat", "account", "sender", "sender_name",
                    "message_id", "subject_line", "received_at", "arrived_at", "in_reply_to",
                    "references", "list_id", "subject"):
            assert after[key] == before[key], key
        assert after["delivered"] is True, "nothing is owed for a mail that is gone"
        assert after["removed_at"] == ist._iso(2000.0)
        # Lost: what it said and to whom.
        assert f'<{urn}> <{ist.P_TEXT}> "" .' in text, "the empty text triple stays"
        assert after["text"] == "" and after["recipients"] == [] and after["cc"] == []
        assert before["reply_to"] and after["reply_to"] == [] and "replyTo" not in text
        assert after["attachments"] == [] and after["attachment_meta"] == {}
        assert after["body_part"] is after["html_part"] is after["header_part"] is None
        assert after["trim"] is None and after["extra"] == []
        assert "secret" not in text and "chart.png" not in text
        for key in ("body", "html", "head", "pic"):
            mid = ist.media_id_of(refs[key])
            assert not list(ist.media_dir(tmp).glob(mid + "*")), key
        assert ist.load_media(tmp, ist.media_id_of(refs["doc"])), "a shared blob stays"
        assert ist.undelivered(tmp) == []

        # Idempotent: the first removal time is kept, nothing is rewritten.
        stamp = path.stat().st_mtime_ns
        assert ist.tombstone_message(path) is True
        assert path.stat().st_mtime_ns == stamp and ist._parse(path.read_text()) == after
        # A late trim does not put the words back; a merge still re-keys it.
        ist.update_message(path, text="resurrected", trim="model", chat="other@x.org")
        late = ist._parse(path.read_text())
        assert late["text"] == "" and late["trim"] is None and late["chat"] == "other@x.org"

        # Outbound: the same, and the shared blob goes once its last holder does.
        assert ist.tombstone_message(sharer, removed_at="2026-01-02T03:04:05Z") is True
        gone = ist._parse(sharer.read_text())
        assert gone["removed_at"] == "2026-01-02T03:04:05Z" and gone["author"] == "user"
        assert ist.P_DELIVERED not in sharer.read_text()
        assert ist.load_media(tmp, ist.media_id_of(refs["doc"])) is None

        # A blob named by a record that no longer parses is never unlinked.
        _u, p3, refs3 = _mail(tmp, timestamp=1002.0)
        (ist.messages_dir(tmp) / "0000000000000001-broken.nt").write_text(
            f"garbage {ist.media_id_of(refs3['pic'])}\n")
        assert ist.tombstone_message(p3) is True
        assert ist.load_media(tmp, ist.media_id_of(refs3["pic"]))
        assert ist.load_media(tmp, ist.media_id_of(refs3["body"])) is None

        # Never raises: missing, unparseable, or a directory where a file was.
        assert ist.tombstone_message(Path(tmp) / "messages" / "nope.nt") is False
        assert ist.tombstone_message(ist.messages_dir(tmp) / "0000000000000001-broken.nt") is False
        (ist.messages_dir(tmp) / "0000000000000002-dir.nt").mkdir()
        assert ist.tombstone_message(ist.messages_dir(tmp) / "0000000000000002-dir.nt") is False
    print("PASS test_tombstone")


def test_rekey_then_delete_finds_the_new_key():
    with tempfile.TemporaryDirectory() as tmp:
        _u, a1, _r = _mail(tmp, chat="late@x.org", timestamp=1000.0)
        _u, a2, _r = _mail(tmp, chat="late@x.org", timestamp=1001.0, outbound=True)
        _u, b1, _r = _mail(tmp, chat="root@x.org", timestamp=999.0)
        # Same key on another account is another chat.
        _u, other, _r = _mail(tmp, chat="late@x.org", timestamp=1002.0,
                              account="you@example.org")
        times = {p: ist._parse(p.read_text()) for p in (a1, a2)}

        assert ist.rekey_chat(tmp, "late@x.org", ACCOUNT, "root@x.org") == {
            "messages": 2, "errors": 0}
        for p, was in times.items():
            now = ist._parse(p.read_text())
            assert now["chat"] == "root@x.org"
            assert {k: v for k, v in now.items() if k != "chat"} == \
                {k: v for k, v in was.items() if k != "chat"}, "nothing else changes"
        assert ist._parse(other.read_text())["chat"] == "late@x.org"
        assert ist.rekey_chat(tmp, "late@x.org", ACCOUNT, "root@x.org")["messages"] == 0

        # One record by hand, through update_message.
        _u, c1, _r = _mail(tmp, chat="third@x.org", timestamp=1003.0)
        ist.update_message(c1, chat="root@x.org")

        assert ist.delete_chat(tmp, "late@x.org", ACCOUNT)["messages"] == 0, \
            "nothing of this account is left under the old key"
        got = ist.delete_chat(tmp, "root@x.org", ACCOUNT)
        assert got["messages"] == 4 and got["errors"] == 0, got
        assert other.exists() and not any(p.exists() for p in (a1, a2, b1, c1))

        # Fails closed on a record it cannot judge.
        (ist.messages_dir(tmp) / "0000000000000001-broken.nt").write_text(
            f'garbage\n<urn:x> <{ist.P_CHAT}> "late@x.org" .\n')
        assert ist.rekey_chat(tmp, "late@x.org", "you@example.org", "r@x")["errors"] == 1
        assert ist.rekey_chat(tmp, "", ACCOUNT, "r@x") == {"messages": 0, "errors": 0}
    print("PASS test_rekey_then_delete_finds_the_new_key")


def test_a_single_writer_spares_the_ledger_scan():
    """A tombstone asks every record whether it names one of the blobs, and a
    merge reads every record to find the chat's: per call, the whole store.
    A writer that knows the answer says so instead — and the default, which
    the messenger gateways rely on, is untouched."""
    with tempfile.TemporaryDirectory() as tmp:
        urn, path, refs = _mail(tmp)
        _s, sharer = ist.write_outbound(tmp, channel="email", chat="root@x.org",
                                        account=ACCOUNT, text="fwd", author="user",
                                        timestamp=1001.0, attachment_urls=[refs["doc"]])
        _u, bystander, _r = _mail(tmp, timestamp=1002.0)
        # A record nobody can read would make the default keep every blob.
        (ist.messages_dir(tmp) / "0000000000000003-dir.nt").mkdir()
        read = []
        real = ist._blob_ids_referenced_elsewhere

        def scan(*args, **kwargs):
            read.append(args)
            return real(*args, **kwargs)
        ist._blob_ids_referenced_elsewhere = scan
        try:
            # own_blobs: the caller's word that this record's blobs are its
            # own. No other record is looked at, and all of them go.
            assert ist.tombstone_message(path, removed_at=2000.0, own_blobs=True) is True
            assert read == []
            for key in refs:
                assert ist.load_media(tmp, ist.media_id_of(refs[key])) is None, key
            assert ist._parse(path.read_text())["removed_at"] == ist._iso(2000.0)
            # The default still asks, and still fails closed.
            mine = ist._media_refs(ist._parse(bystander.read_text()))
            assert ist.tombstone_message(bystander) is True
            assert len(read) == 1
            assert all(ist.load_media(tmp, ist.media_id_of(ref)) for ref in mine), \
                "an unreadable record may name any blob: none is unlinked"
        finally:
            ist._blob_ids_referenced_elsewhere = real
        assert ist._parse(sharer.read_text())["attachments"] == [refs["doc"]]

    with tempfile.TemporaryDirectory() as tmp:
        _u, a1, _r = _mail(tmp, chat="late@x.org", timestamp=1000.0)
        _u, a2, _r = _mail(tmp, chat="late@x.org", timestamp=1001.0, outbound=True)
        _u, stray, _r = _mail(tmp, chat="late@x.org", timestamp=1002.0)   # not handed in
        _u, other, _r = _mail(tmp, chat="else@x.org", timestamp=1003.0)
        _u, foreign, _r = _mail(tmp, chat="late@x.org", timestamp=1004.0,
                                account="you@example.org")
        # A record elsewhere that cannot be judged is not this merge's concern …
        broken = ist.messages_dir(tmp) / "0000000000000001-broken.nt"
        broken.write_text(f'garbage\n<urn:x> <{ist.P_CHAT}> "late@x.org" .\n')
        opened = []
        real_read = Path.read_text

        def read_text(self, *args, **kwargs):
            opened.append(self.name)
            return real_read(self, *args, **kwargs)
        Path.read_text = read_text
        try:
            got = ist.rekey_chat(tmp, "late@x.org", ACCOUNT, "root@x.org",
                                 paths=[a1, a2, other, foreign,
                                        ist.messages_dir(tmp) / "gone.nt"])
        finally:
            Path.read_text = real_read
        # Only the files named were read; of those, only this chat's moved.
        assert got == {"messages": 2, "errors": 0}, got
        assert set(opened) <= {p.name for p in (a1, a2, other, foreign)} | {"gone.nt"}
        chats = {p: ist._parse(p.read_text())["chat"] for p in (a1, a2, stray, other, foreign)}
        assert chats == {a1: "root@x.org", a2: "root@x.org", stray: "late@x.org",
                         other: "else@x.org", foreign: "late@x.org"}
        assert ist.rekey_chat(tmp, "late@x.org", ACCOUNT, "root@x.org", paths=[a1, a2]) == {
            "messages": 0, "errors": 0}, "a repeat finds them moved"
        # … but one that WAS handed in and cannot be judged is an error,
        # whatever it names: the caller said it is this chat's.
        assert ist.rekey_chat(tmp, "late@x.org", ACCOUNT, "root@x.org",
                              paths=[stray, broken]) == {"messages": 1, "errors": 1}
        (ist.messages_dir(tmp) / "0000000000000002-quiet.nt").write_text("garbage\n")
        assert ist.rekey_chat(tmp, "x@x.org", ACCOUNT, "root@x.org", paths=[
            ist.messages_dir(tmp) / "0000000000000002-quiet.nt"])["errors"] == 1
        # Without paths: the whole store, as before.
        assert ist.rekey_chat(tmp, "else@x.org", ACCOUNT, "root@x.org") == {
            "messages": 1, "errors": 0}
    print("PASS test_a_single_writer_spares_the_ledger_scan")


def test_a_long_literal_is_read_without_a_gigabyte():
    """One mail's text is one literal on one line. Matched with a pattern
    that can backtrack, a literal of five megabytes costs a gigabyte on every
    read of its record — at every start, on every tombstone and re-key."""
    import tracemalloc
    with tempfile.TemporaryDirectory() as tmp:
        text = "".join(f"line {n} with a \"quote\" and a back\\slash\n" for n in range(50_000))
        assert len(text) > 2_000_000
        _urn, path = ist.write_message(tmp, channel="email", sender="a@x.org", text=text,
                                       chat="c@x.org", account=ACCOUNT, message_id="big@x.org",
                                       timestamp=1000.0)
        raw = path.read_text(encoding="utf-8")
        tracemalloc.start()
        try:
            fields = ist._parse(raw)
            _now, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert fields["text"] == text
        # A few copies of the text, not two hundred bytes per character.
        assert peak < 20 * len(raw), f"{peak / len(raw):.0f} bytes per character"
        # The same strictness as ever about what a triple is.
        for bad in ('<urn:a> <urn:p> "unterminated .',
                    '<urn:a> <urn:p> "a" "b" .',
                    '<urn:a> <urn:p> "ends in a backslash\\" .',
                    '<urn:a> <urn:p> "x"^^<urn:t> extra .'):
            assert ist._TRIPLE_RE.match(bad) is None, bad
        for good, literal in (('<urn:a> <urn:p> "" .', ""),
                              ('<urn:a> <urn:p> "a \\"q\\" \\\\ b"^^<urn:t> .', 'a \\"q\\" \\\\ b'),
                              ('<urn:a> <urn:p> "x"  .  ', "x")):
            assert ist._TRIPLE_RE.match(good).group(4) == literal, good
    print("PASS test_a_long_literal_is_read_without_a_gigabyte")


def test_delete_chat_unlinks_part_blobs():
    with tempfile.TemporaryDirectory() as tmp:
        _u, path, refs = _mail(tmp)
        # Another chat's record shares the header blob: it stays.
        ist.write_message(tmp, channel="email", sender="z@x", chat="else@x.org",
                          account=ACCOUNT, text="x", header_part=refs["head"],
                          timestamp=1005.0)
        got = ist.delete_chat(tmp, "root@x.org", ACCOUNT)
        assert (got["messages"], got["media"], got["parts"], got["errors"]) == (1, 4, 2, 0), got
        for key in ("body", "html", "pic", "doc"):
            assert ist.load_media(tmp, ist.media_id_of(refs[key])) is None, key
        assert ist.load_media(tmp, ist.media_id_of(refs["head"])), "shared part stays"
        got = ist.delete_chat(tmp, "else@x.org", ACCOUNT)
        assert (got["media"], got["parts"]) == (1, 1), got
        assert not list(ist.media_dir(tmp).iterdir()), "nothing left behind"
    print("PASS test_delete_chat_unlinks_part_blobs")


def test_the_author_of_an_outbound_record_can_be_corrected():
    """A mail found in the Sent folder is recorded as the own-device echo
    until the gateway is told who wrote it (POST /outbound-note)."""
    with tempfile.TemporaryDirectory() as tmp:
        _u, out, refs = _mail(tmp, outbound=True)
        before = ist._parse(out.read_text())
        assert before["author"] == "device"
        ist.update_message(out, author="agent")
        after = ist._parse(out.read_text())
        assert after["author"] == "agent"
        # Nothing else of the record moved.
        assert {k: v for k, v in after.items() if k != "author"} == {
            k: v for k, v in before.items() if k != "author"}
        _assert_mail_fields(after, refs)
        # It survives the other rewrites, and a tombstone keeps who sent it.
        ist.update_message(out, chat="other@x.org")
        assert ist._parse(out.read_text())["author"] == "agent"
        assert ist.tombstone_message(out, 2000.0)
        assert ist._parse(out.read_text())["author"] == "agent"
        ist.update_message(out, author="user")
        assert ist._parse(out.read_text())["author"] == "user"
        # Only the three authors exist; anything else is a programming error.
        for bad in ("root", "Agent ", "device\nx"):
            try:
                ist.update_message(out, author=bad)
            except ValueError:
                continue
            assert bad.strip() in ist.AUTHORS, f"{bad!r} must be refused"
        # An inbound record has no author, and does not grow one.
        _u, inbound, _refs = _mail(tmp, timestamp=1001.0)
        text = inbound.read_text()
        ist.update_message(inbound, author="agent")
        assert inbound.read_text() == text and ist.P_AUTHOR not in text
        # Absent, the argument changes nothing.
        ist.update_message(out, text="x")
        assert ist._parse(out.read_text())["author"] == "user"
    print("PASS test_the_author_of_an_outbound_record_can_be_corrected")


def test_a_sent_mail_says_whom_the_server_refused_and_what_else_it_is_known_as():
    """Two facts only a mail the gateway sent has: the addresses the SMTP
    server would not take it for (kb:refused — gone from a tombstone, like
    every address), and the Message-ID a provider gave it in place of its
    own (kb:sentAs — identity, kept on a tombstone)."""
    with tempfile.TemporaryDirectory() as tmp:
        urn, path = ist.write_outbound(
            tmp, channel="email", chat="root@x.org", account=ACCOUNT, text="Hello.",
            author="user", message_id="minted@example.org", timestamp=1000.0, sender=ACCOUNT,
            subject="Plans", recipients=["ann@x.org"], cc=["cat@z.org"],
            refused=["typo@y.org", " typo@y.org ", ""], references=["root@x.org"])
        text = path.read_text(encoding="utf-8")
        assert f'<{urn}> <{ist.P_REFUSED}> "typo@y.org" .' in text and text.count("refused") == 1
        fields = ist._parse(text)
        assert fields["refused"] == ["typo@y.org"] and fields["sent_as"] == []
        assert fields["recipients"] == ["ann@x.org"] and fields["cc"] == ["cat@z.org"]
        assert ist._render(fields) == text and ist.P_SENT_AS not in text

        # The id it was sent as is learned later, and added — never replaced.
        ist.update_message(path, sent_as="prov-1@provider.example")
        ist.update_message(path, sent_as=["prov-1@provider.example", " prov-2@provider.example "])
        text = path.read_text(encoding="utf-8")
        fields = ist._parse(text)
        assert sorted(fields["sent_as"]) == ["prov-1@provider.example", "prov-2@provider.example"]
        assert f'<{urn}> <{ist.P_SENT_AS}> "prov-1@provider.example" .' in text
        assert fields["message_id"] == "minted@example.org", "its own id is what it is keyed by"
        assert fields["refused"] == ["typo@y.org"] and ist._render(fields) == text
        # Through the other rewrites.
        ist.update_message(path, author="agent", chat="other@x.org")
        fields = ist._parse(path.read_text())
        assert len(fields["sent_as"]) == 2 and fields["refused"] == ["typo@y.org"]
        assert ist.rekey_chat(tmp, "other@x.org", ACCOUNT, "root@x.org")["errors"] == 0
        assert len(ist._parse(path.read_text())["sent_as"]) == 2
        # A tombstone loses to whom, and keeps every id the mail was known by.
        assert ist.tombstone_message(path, 2000.0)
        gone = ist._parse(path.read_text())
        assert gone["refused"] == [] and gone["recipients"] == []
        assert "refused" not in path.read_text()
        assert sorted(gone["sent_as"]) == ["prov-1@provider.example", "prov-2@provider.example"]
        assert gone["message_id"] == "minted@example.org" and gone["references"] == "root@x.org"

        # An inbound record has neither, and does not grow one.
        _u, inbound, _refs = _mail(tmp, timestamp=1001.0)
        before = inbound.read_text()
        assert ist.P_REFUSED not in before and ist.P_SENT_AS not in before
        ist.update_message(inbound, sent_as="x@provider.example")
        assert inbound.read_text() == before
        # And a record written before either existed reads as having none.
        fields = ist._parse(before)
        assert fields["refused"] == [] and fields["sent_as"] == []
        # A messenger's outbound record writes neither triple.
        _u, sms = ist.write_outbound(tmp, channel="sms", chat="+41790000000", text="Hi")
        assert ist.P_REFUSED not in sms.read_text() and ist.P_SENT_AS not in sms.read_text()
    print("PASS test_a_sent_mail_says_whom_the_server_refused_and_what_else_it_is_known_as")


def test_discard_media():
    """Blobs stored for a record that then could not be written are taken
    back, sidecars and all."""
    with tempfile.TemporaryDirectory() as tmp:
        keep = ist.store_media(tmp, b"keep", "text/plain")
        pic = ist.store_media(tmp, _png(40, 20), "image/png", file_name="chart.png")
        assert len(list(ist.media_dir(tmp).glob(pic + "*"))) == 4   # blob, type, name, meta
        assert ist.discard_media(tmp, pic) is True
        assert not list(ist.media_dir(tmp).glob(pic + "*"))
        assert ist.load_media(tmp, keep) == (b"keep", "text/plain")
        # Idempotent, and never a way out of the media directory.
        assert ist.discard_media(tmp, pic) is True
        outside = Path(tmp) / "outside"
        outside.write_text("x")
        assert ist.discard_media(tmp, "../outside") is True and outside.exists()
        assert ist.discard_media(tmp, "") is True and ist.load_media(tmp, keep)
    print("PASS test_discard_media")


def test_iter_records():
    with tempfile.TemporaryDirectory() as tmp:
        assert list(ist.iter_records(Path(tmp) / "nope")) == []
        _u, p2, _r = _mail(tmp, timestamp=2000.0, outbound=True)
        _u, p1, _r = _mail(tmp, timestamp=1000.0)
        ist.tombstone_message(p1)
        mdir = ist.messages_dir(tmp)
        (mdir / "0000000000001500-broken.nt").write_text("garbage\n")
        (mdir / "0000000000001600-dir.nt").mkdir()
        (mdir / "0000000000001700-bytes.nt").write_bytes(b"\xff\xfe\x00")
        got = list(ist.iter_records(tmp))
        assert [p for p, _f in got] == [p1, p2], got
        assert [f["type"] for _p, f in got] == [ist.T_INBOUND, ist.T_OUTBOUND]
        assert got[0][1]["removed_at"] and got[1][1]["subject_line"]
        # Read-only: the files are untouched.
        stamp = p2.stat().st_mtime_ns
        list(ist.iter_records(tmp))
        assert p2.stat().st_mtime_ns == stamp
        # A file that is no text at all costs the other readers nothing either.
        bytes_file = mdir / "0000000000001700-bytes.nt"
        assert ist.undelivered(tmp) == [] and ist.mark_delivered(bytes_file) is False
        assert ist.update_message(bytes_file, text="x") is None
        assert ist.subject_for(bytes_file) is None and ist.backfill_media_meta(tmp) == 0
        # One record by its path: the same fields, or None for anything else.
        assert ist.read_record(p2) == got[1][1] and ist.read_record(str(p1)) == got[0][1]
        for other in (bytes_file, mdir / "0000000000001500-broken.nt",
                      mdir / "0000000000001600-dir.nt", mdir / "nope.nt"):
            assert ist.read_record(other) is None, other
    print("PASS test_iter_records")


if __name__ == "__main__":
    test_every_field_is_written_and_read_back()
    test_every_field_survives_every_rewrite()
    test_absent_fields_write_nothing()
    test_unknown_triples_survive_every_rewrite()
    test_unsafe_characters_keep_the_record_readable()
    test_tombstone()
    test_rekey_then_delete_finds_the_new_key()
    test_a_single_writer_spares_the_ledger_scan()
    test_a_long_literal_is_read_without_a_gigabyte()
    test_delete_chat_unlinks_part_blobs()
    test_the_author_of_an_outbound_record_can_be_corrected()
    test_a_sent_mail_says_whom_the_server_refused_and_what_else_it_is_known_as()
    test_discard_media()
    test_iter_records()
    print("all inbound_store mail tests passed")
