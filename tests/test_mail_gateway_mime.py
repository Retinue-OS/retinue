#!/usr/bin/env python3
"""What the mail gateway makes of a mail (scripts/mail-gateway.py).

A mail is text from outside, in every shape a client or an attacker writes:
this pins what becomes of each — plain, HTML-only, multipart/alternative,
mixed with attachments, inline ``cid:`` images, a tracking pixel, a nested
message, odd and unknown charsets, a missing or overlong Message-ID, RFC 2047
names, hostile file names, content types, addresses and ids — and the size
caps. Every part is kept; nothing a mail says becomes more than data.

Uses the fake mailbox of tests/test_mail_gateway.py.

    python3 tests/test_mail_gateway_mime.py
"""
import base64
import importlib.util
import re
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("mail_gateway_helpers",
                                               HERE / "test_mail_gateway.py")
t = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t)

DATE = "Date: Fri, 10 Oct 2025 08:53:20 +0000\r\n"     # one day after t.T0
HEAD = ("From: Ann Example <ann@x.org>\r\nTo: me@example.org\r\n" + DATE).encode()


def png(width: int, height: int) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + width.to_bytes(4, "big") + height.to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00")


def b64(data: bytes) -> bytes:
    return base64.encodebytes(data).replace(b"\n", b"\r\n")


def ingest(gw, raw: bytes, folder="INBOX"):
    """Mirror one mail; returns (its record, the mirror, the mailbox)."""
    t.rail(gw)
    mirror, box = t.started(gw)
    box.put(folder, raw, date=t.T0 + t.DAY)
    mirror.poll()
    recs = t.records(gw)
    assert len(recs) == 1, [r["message_id"] for r in recs]
    return recs[0], mirror, box


def files(gw, record) -> list:
    """``[(file name, content type, bytes, content id)]`` of a record's attachments."""
    out = []
    for ref in record["attachments"]:
        meta = record["attachment_meta"].get(ref, {})
        out.append((meta.get("file_name"), meta.get("content_type"), t.blob(gw, ref),
                    meta.get("content_id")))
    return sorted(out, key=lambda item: (item[0] or "", item[1] or ""))


# --------------------------------------------------------------------------
# Bodies
# --------------------------------------------------------------------------

def test_plain():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        raw = (HEAD + b"Message-ID: <p@x.org>\r\nSubject: Gr=?iso-8859-1?q?=FC=DFe?=\r\n"
               b"Content-Type: text/plain; charset=iso-8859-1\r\n"
               b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
               b"Sch=F6ne Gr=FC=DFe,\r\nAnn\r\n")
        record, _mirror, _box = ingest(gw, raw)
        assert record["text"] == "Schöne Grüße,\nAnn" and record["trim"] == "deterministic"
        assert record["subject_line"] == "Grüße"
        # The plain part is kept whole, as UTF-8 whatever it came in.
        assert t.blob(gw, record["body_part"]) == "Schöne Grüße,\nAnn\n".encode("utf-8")
        assert record["html_part"] is None and record["attachments"] == []
        header = t.blob(gw, record["header_part"]).decode()
        assert header.startswith("From: Ann Example <ann@x.org>\nTo: me@example.org\n")
        assert "Sch=F6ne" not in header and header.endswith("quoted-printable")
    print("ok: a plain mail: its text, its body part, its header block")


def test_html_only():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        raw = (HEAD + b"Message-ID: <h@x.org>\r\nSubject: Offer\r\n"
               b"Content-Type: text/html; charset=utf-8\r\n\r\n"
               b"<html><body><p>Hello <b>there</b>,</p><p>the offer stands.</p>"
               b"<script>steal()</script></body></html>\r\n")
        record, _mirror, _box = ingest(gw, raw)
        assert record["text"] == "Hello there,\n\nthe offer stands.", record["text"]
        # Nothing settled the text but a rendering: a model trim is due on first view.
        assert record["trim"] == "wanting"
        assert record["body_part"] is None
        # The HTML is stored as it came (the Original view sanitises it when it serves it).
        assert b"<script>steal()</script>" in t.blob(gw, record["html_part"])
        assert (record["attachment_meta"][record["html_part"]]["content_type"]
                == "text/html; charset=utf-8")
    print("ok: an HTML-only mail keeps its HTML part and shows a rendering")


def test_multipart_alternative():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        raw = t.msg("alt@x.org", body="Plain side.\n\n-- \nAnn\nACME\n",
                    html="<p>HTML side.</p>")
        record, _mirror, _box = ingest(gw, raw)
        assert record["text"] == "Plain side." and record["trim"] == "deterministic"
        assert t.blob(gw, record["body_part"]).decode() == "Plain side.\n\n-- \nAnn\nACME\n"
        assert b"<p>HTML side.</p>" in t.blob(gw, record["html_part"])
        assert record["attachments"] == []
    print("ok: multipart/alternative keeps both sides; the plain one is the text")


def test_several_text_parts_and_a_text_attachment():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        raw = (HEAD + b"Message-ID: <mix@x.org>\r\nSubject: Minutes\r\n"
               b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
               b"--B\r\nContent-Type: text/plain\r\n\r\nThe minutes are attached.\r\n"
               b"--B\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; "
               b"filename=minutes.txt\r\n\r\nItem one.\r\n"
               b"--B\r\nContent-Type: text/plain\r\n\r\nSecond inline part.\r\n"
               b"--B--\r\n")
        record, _mirror, _box = ingest(gw, raw)
        # Inline text parts are the body, in order; an attached text file is a file.
        assert (t.blob(gw, record["body_part"]).decode()
                == "The minutes are attached.\n\nSecond inline part.")
        assert "Item one" not in record["text"]
        assert files(gw, record) == [("minutes.txt", "text/plain", b"Item one.", None)]
    print("ok: inline text parts are the body; an attached text file is an attachment")


# --------------------------------------------------------------------------
# Attachments
# --------------------------------------------------------------------------

def test_mixed_with_attachments():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        raw = t.msg("att@x.org", body="See attached.\n", files=[
            ("Q3 report.pdf", "application/pdf", b"%PDF-1.7 report"),
            ("chart.png", "image/png", png(640, 480)),
            ("data.csv", "text/csv", b"a,b\n1,2\n")])
        record, _mirror, _box = ingest(gw, raw)
        assert record["text"] == "See attached."
        got = files(gw, record)
        assert [(name, kind) for name, kind, _d, _c in got] == [
            ("Q3 report.pdf", "application/pdf"), ("chart.png", "image/png"),
            ("data.csv", "text/csv")]
        assert got[0][2] == b"%PDF-1.7 report" and got[2][2] == b"a,b\n1,2\n"
        chart = next(m for m in record["attachment_meta"].values()
                     if m.get("file_name") == "chart.png")
        assert (chart["width"], chart["height"], chart["size"]) == (640, 480, len(png(640, 480)))
    with tempfile.TemporaryDirectory() as tmp:
        # The rail names the attachments, never the part blobs.
        gw = t.load(tmp)
        events = t.rail(gw)
        mirror, box = t.started(gw)
        box.put("INBOX", t.msg("att@x.org", html="<p>x</p>",
                               files=[("a.pdf", "application/pdf", b"%PDF")]))
        mirror.poll()
        record = t.records(gw)[0]
        assert events[0][1]["attachments"] == record["attachments"] != []
        assert record["body_part"] not in events[0][1]["attachments"]
        assert record["html_part"] not in events[0][1]["attachments"]
    print("ok: attachments are kept under the type and name their sender declared")


def _related(image: bytes, *, content_id=b"<logo@x.org>", disposition=b"inline",
             extra=b"") -> bytes:
    return (HEAD + b"Message-ID: <rel@x.org>\r\nSubject: Logo\r\n"
            b"Content-Type: multipart/related; boundary=R\r\n\r\n"
            b"--R\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            b"<p>Hi</p><img src=\"cid:logo@x.org\">\r\n"
            b"--R\r\nContent-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n"
            + (b"Content-ID: " + content_id + b"\r\n" if content_id else b"")
            + (b"Content-Disposition: " + disposition + b"\r\n" if disposition else b"")
            + b"\r\n" + b64(image) + extra + b"--R--\r\n")


def test_inline_cid_images_and_tracking_pixels():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        record, _mirror, _box = ingest(gw, _related(png(40, 20)))
        # An inline image that belongs to the text is media, with its Content-ID.
        assert files(gw, record) == [(None, "image/png", png(40, 20), "logo@x.org")]
        assert record["html_part"] and record["text"] == "Hi"
    for pixel in (png(1, 1), png(2, 2), png(1, 2)):
        with tempfile.TemporaryDirectory() as tmp:
            gw = t.load(tmp)
            record, _mirror, _box = ingest(gw, _related(pixel))
            assert record["attachments"] == [], "a tracking pixel is not an attachment"
            # Its blob is gone too: the two bodies' parts are all that is stored.
            media = gw._ibstore.media_dir(gw.INBOUND_STORE_DIR)
            assert len([p for p in media.iterdir() if "." not in p.name]) == 2
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        # No Content-ID and no disposition at all: still an inline image, still a pixel.
        record, _mirror, _box = ingest(gw, _related(png(1, 1), content_id=None, disposition=None))
        assert record["attachments"] == []
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        # A spacer is not a pixel, and a tiny image the sender attached is their file.
        record, _mirror, _box = ingest(gw, _related(png(1, 600)))
        assert len(record["attachments"]) == 1
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        record, _mirror, _box = ingest(gw, _related(
            png(1, 1), content_id=None, disposition=b"attachment; filename=dot.png"))
        assert files(gw, record) == [("dot.png", "image/png", png(1, 1), None)]
    print("ok: inline images are kept with their Content-ID; tracking pixels are dropped")


def test_nested_message():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        raw = (HEAD + b"Message-ID: <fwd@x.org>\r\nSubject: Fwd: Invoice\r\n"
               b"Content-Type: multipart/mixed; boundary=M\r\n\r\n"
               b"--M\r\nContent-Type: text/plain\r\n\r\nFYI, see the attached mail.\r\n"
               b"--M\r\nContent-Type: message/rfc822\r\n"
               b"Content-Disposition: attachment; filename=\"original.eml\"\r\n\r\n"
               b"From: Billing <billing@shop.example>\r\nSubject: Invoice 42\r\n"
               b"Message-ID: <inner@shop.example>\r\nContent-Type: multipart/mixed; boundary=I\r\n"
               b"\r\n--I\r\nContent-Type: text/plain\r\n\r\nPlease pay 10.\r\n"
               b"--I\r\nContent-Type: application/pdf\r\n"
               b"Content-Disposition: attachment; filename=inv.pdf\r\n\r\n%PDF\r\n--I--\r\n"
               b"--M--\r\n")
        record, _mirror, _box = ingest(gw, raw)
        # The nested mail is one part, whole; its body and its file are not this mail's.
        assert record["text"] == "FYI, see the attached mail."
        assert record["message_id"] == "fwd@x.org" and record["sender"] == "ann@x.org"
        got = files(gw, record)
        assert len(got) == 1 and got[0][:2] == ("original.eml", "message/rfc822")
        inner = got[0][2]
        assert b"Subject: Invoice 42" in inner and b"Please pay 10." in inner
        assert b"filename=inv.pdf" in inner
    print("ok: a nested message is kept whole as one attachment")


def test_every_other_part_is_kept():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        raw = (HEAD + b"Message-ID: <cal@x.org>\r\nSubject: Invite\r\n"
               b"Content-Type: multipart/mixed; boundary=C\r\n\r\n"
               b"--C\r\nContent-Type: text/plain\r\n\r\nJoin us.\r\n"
               b"--C\r\nContent-Type: text/calendar; method=REQUEST\r\n\r\nBEGIN:VCALENDAR\r\n"
               b"--C\r\nContent-Type: application/pkcs7-signature; name=smime.p7s\r\n"
               b"Content-Transfer-Encoding: base64\r\n\r\n" + b64(b"\x30\x82sig") +
               b"--C\r\nContent-Type: message/delivery-status\r\n\r\n"
               b"Reporting-MTA: dns; mx.x.org\r\n\r\nFinal-Recipient: rfc822; a@b.org\r\n"
               b"--C--\r\n")
        record, _mirror, _box = ingest(gw, raw)
        kinds = sorted(kind for _name, kind, _data, _cid in files(gw, record))
        assert kinds == ["application/pkcs7-signature", "message/delivery-status",
                         "text/calendar"], kinds
        status = next(d for _n, kind, d, _c in files(gw, record)
                      if kind == "message/delivery-status")
        assert b"Reporting-MTA" in status and b"Final-Recipient" in status
    print("ok: parts that are neither body nor file to a mail client are kept all the same")


# --------------------------------------------------------------------------
# Charsets and names
# --------------------------------------------------------------------------

def test_odd_charsets_never_raise():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        cases = {
            "koi8-r": ("Привет".encode("koi8-r"), "Привет"),
            "x-no-such-charset": ("caf\u00e9".encode("utf-8"), "café"),
            "x-unknown": (b"caf\xe9 au lait", "café au lait"),     # not UTF-8: read as cp1252
            "utf-8": (b"ok \xff\xfe bytes", "ok \ufffd\ufffd bytes"),
        }
        for charset, (body, expected) in cases.items():
            raw = (HEAD + b"Message-ID: <c@x.org>\r\nSubject: s\r\n"
                   b"Content-Type: text/plain; charset=" + charset.encode() + b"\r\n"
                   b"Content-Transfer-Encoding: 8bit\r\n\r\n" + body + b"\r\n")
            assert gw.parse_mail(raw).plain == expected + "\n", (charset, gw.parse_mail(raw).plain)
        # Raw 8-bit bytes in a header, and an encoded word in a charset nobody knows.
        raw = (b"From: J\xc3\xbcrgen <j@x.org>\r\nTo: me@example.org\r\n"
               b"Subject: =?x-nope?Q?caf=C3=A9?= and =?utf-8?B?!!!not-base64?=\r\n"
               b"Message-ID: <e@x.org>\r\n\r\nbody\r\n")
        mail = gw.parse_mail(raw)
        assert mail.sender == "j@x.org" and mail.sender_name == "Jürgen"
        # The word that can be read is; the one that cannot stays as written.
        assert mail.subject == "café and =?utf-8?B?!!!not-base64?=", mail.subject
        # A broken transfer encoding yields what can be read, not an exception.
        raw = (HEAD + b"Message-ID: <b@x.org>\r\nContent-Type: text/plain\r\n"
               b"Content-Transfer-Encoding: base64\r\n\r\n@@@ not base64 @@@\r\n")
        assert isinstance(gw.parse_mail(raw).plain, str)
    print("ok: unknown charsets, raw 8-bit headers and broken encodings are read, not raised")


def test_a_codec_the_sender_names_cannot_poison_the_mirror():
    """An encoded word or a body names its own charset, and some (utf-7,
    unicode_escape) decode to lone surrogates — which no database, file or
    JSON reader takes. Each of these mails used to stop the folder for good."""
    base = [b"From: Ann Example <ann@x.org>", b"To: me@example.org", DATE.encode().strip()]

    def raw(headers, body=b"hello\r\n"):
        return b"\r\n".join(base[1:] + headers) + b"\r\n\r\n" + body

    ann = base[0]
    cases = {
        "subject, utf-7": raw([ann, b"Subject: =?utf-7?Q?+2AA-?= hi"]),
        "subject, unicode_escape": raw([ann, b"Subject: =?unicode_escape?Q?=5Cud800?= hi"]),
        "sender name": raw([b"From: =?utf-7?q?+2AA-x?= <ann@x.org>", b"Subject: s"]),
        "recipient name": raw([ann, b"Subject: s",
                               b"Cc: =?unicode_escape?Q?=5Cud800?= <bob@y.org>"]),
        "body, utf-7": raw([ann, b"Subject: s", b"Content-Type: text/plain; charset=utf-7"],
                           b"abc +2AA- def\r\n"),
        "body, unicode_escape": raw(
            [ann, b"Subject: s", b"Content-Type: text/html; charset=unicode_escape"],
            b"<p>abc \\udfff def</p>\r\n"),
        "file name, RFC 2047": raw(
            [ann, b"Subject: s", b"Content-Type: application/pdf",
             b"Content-Disposition: attachment; filename=\"=?unicode_escape?Q?=5Cud800?=.pdf\""],
            b"%PDF\r\n"),
        "file name, RFC 2231": raw(
            [ann, b"Subject: s", b"Content-Type: application/pdf",
             b"Content-Disposition: attachment; filename*=utf-7''+2AA-.pdf"], b"%PDF\r\n"),
        "list id": raw([ann, b"Subject: s", b"List-Id: =?utf-7?Q?+2AA-?= <l.x.org>"]),
    }
    for label, mail in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            gw = t.load(tmp)
            events = t.rail(gw)
            mirror, box = t.started(gw)
            box.put("INBOX", b"Message-ID: <p@x.org>\r\n" + mail, date=t.T0 + t.DAY)
            box.put("INBOX", t.msg("after@x.org", day=2))
            mirror.poll()                        # raised, every time, before
            recs = t.by_id(gw)
            assert sorted(recs) == ["after@x.org", "p@x.org"], (label, sorted(recs))
            record = recs["p@x.org"]
            assert record["text"] != gw.UNREADABLE_TEXT, (label, "mirrored, not set aside")
            assert record["sender"] == "ann@x.org", label
            # Nothing that left the reader holds a surrogate: every string can
            # be written as UTF-8, in the record and on the rail.
            import json
            json.dumps(events, ensure_ascii=False).encode("utf-8")
            json.dumps(record, ensure_ascii=False).encode("utf-8")
            json.dumps(mirror.thread(record["chat"]), ensure_ascii=False).encode("utf-8")
            for ref in gw._ibstore._media_refs(record):
                name = record["attachment_meta"].get(ref, {}).get("file_name")
                (name or "").encode("utf-8")
            assert mirror.index.one("SELECT hwm FROM folders WHERE role = 'inbox'")["hwm"] == 2
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        mail = gw.parse_mail(b"Message-ID: <p@x.org>\r\n" + cases["subject, utf-7"])
        assert mail.subject == "� hi", repr(mail.subject)
        # An id with a surrogate in it is no id; the mail gets a synthetic one.
        assert gw._threads.normalize_id("<a\ud800@x.org>") == ""
        # The store's own last line: a name with one is still a name it can write.
        assert gw._ibstore.safe_file_name("a\ud800.pdf") == "a�.pdf"
    print("ok: a surrogate from a sender-named codec is scrubbed where it is decoded")


def test_only_a_charset_is_a_charset():
    """A charset is the sender's to name, and Python's codec registry holds
    more than charsets. punycode takes time with the square of what it is
    given — a megabyte body held the poll for minutes, the fifty a mail may
    have for days, and on every pass again — and the parser applies whatever
    a part or a parameter names. Nothing of a mail is decoded under a codec
    that is not a charset: not by the reader, and not by the parser for it."""
    import encodings.punycode as punycode
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        for name, codec in (("UTF-8", "utf-8"), ("latin1", "iso8859-1"), ("utf-7", "utf-7"),
                            ("Shift_JIS", "shift_jis"), ("\"windows-1252\"", "cp1252")):
            assert gw._charset(name) == codec, (name, gw._charset(name))
        for name in ("punycode", "idna", "unicode_escape", "Unicode-Escape", "raw_unicode_escape",
                     "undefined", "rot13", "zlib", "hex", "base64", "x-nope", "", None,
                     "utf-8\x00", "utf-8 ; x", "a" * 200, "../../etc"):
            assert gw._charset(name) is None, name

        digits = b"a-" + b"9" * 400
        mails = {
            "body": (b"Content-Type: text/plain; charset=punycode\r\n\r\n" + digits),
            "8-bit body": (b"Content-Type: text/plain; charset=punycode\r\n\r\n\xff" + digits),
            "8-bit attachment": (b"Content-Type: application/x-y; charset=punycode\r\n"
                                 b"Content-Disposition: attachment\r\n\r\n\xff" + digits),
            "subject": b"Subject: =?punycode?Q?" + digits[:60] + b"?=\r\n\r\nbody",
            "file name, RFC 2231": (b"Content-Type: application/pdf\r\nContent-Disposition: "
                                    b"attachment; filename*=punycode''" + digits + b"\r\n\r\nx"),
            "file name, RFC 2047": (b"Content-Type: application/pdf; name=\"=?punycode?Q?"
                                    + digits[:60] + b"?=\"\r\n\r\nx"),
            "charset, RFC 2231": (b"Content-Type: text/plain; charset*=punycode''" + digits
                                  + b"\r\n\r\n\xffbody"),
            "a part's file name": (b"Content-Type: multipart/mixed; boundary=B\r\n\r\n--B\r\n"
                                   b"Content-Type: a/b\r\nContent-Disposition: attachment; "
                                   b"filename*=punycode''" + digits + b"\r\n\r\nx\r\n--B--\r\n"),
            "a nested mail": (b"Content-Type: message/rfc822\r\n\r\nSubject: =?punycode?Q?a-9?=\r\n"
                              b"Content-Type: text/plain; charset=punycode\r\n\r\n\xff" + digits),
        }
        calls = []
        real = punycode.punycode_decode
        punycode.punycode_decode = lambda *a, **kw: (calls.append(a[0][:20]), real(*a, **kw))[1]
        try:
            parsed = {label: gw.parse_mail(HEAD + b"Message-ID: <p@x.org>\r\n" + raw + b"\r\n")
                      for label, raw in mails.items()}
            assert digits.decode("punycode", "replace") != digits.decode() and len(calls) == 1
            del calls[:]
            record, _mirror, _box = ingest(gw, HEAD + b"Message-ID: <i@x.org>\r\n"
                                           + mails["8-bit body"] + b"\r\n")
        finally:
            punycode.punycode_decode = real
        assert calls == [], calls
        # Read as what stands there, like any unknown charset.
        assert parsed["body"].plain == digits.decode() + "\n"
        assert parsed["8-bit body"].plain == "ÿ" + digits.decode() + "\n"
        assert record["text"] == "ÿ" + digits.decode()
        assert parsed["8-bit attachment"].parts[0].data.startswith(b"\xffa-9")
        assert parsed["subject"].subject == digits[:60].decode()
        assert parsed["file name, RFC 2231"].parts[0].file_name == digits.decode()
        assert parsed["a part's file name"].parts[0].file_name == digits.decode()
        assert parsed["charset, RFC 2231"].plain == "ÿbody\n"
        # The escape codecs make text of what a sender spells out; not here.
        raw = (HEAD + b"Message-ID: <u@x.org>\r\nSubject: =?unicode_escape?Q?=5Cu0041=5Cud800?=\r\n"
               b"Content-Type: text/plain; charset=raw_unicode_escape\r\n\r\nabc \\u0041 \\udfff\r\n")
        mail = gw.parse_mail(raw)
        assert mail.subject == "\\u0041\\ud800" and mail.plain == "abc \\u0041 \\udfff\n"

        # A real charset on an RFC 2231 parameter is still read.
        long_name = "会" * 250 + ".pdf"
        quoted = "".join(f"%{byte:02X}" for byte in long_name.encode("utf-8"))
        pieces = [quoted[i:i + 60] for i in range(0, len(quoted), 60)]
        spelled = "".join(f";\r\n filename*{n}*={'utf-8' + chr(39) * 2 if n == 0 else ''}{piece}"
                          for n, piece in enumerate(pieces)).encode()
        assert 2500 < len(spelled) < gw.MAX_FIELD_BYTES, "the longest name, the longest way"
        raw = (HEAD + b"Message-ID: <r@x.org>\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
               b"--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment" + spelled
               + b"\r\n\r\n%PDF\r\n--B\r\nContent-Type: text/plain; charset*=iso-8859-1''iso-8859-1"
               b"\r\nContent-Disposition: attachment; filename*=iso-8859-1''caf%E9.txt\r\n\r\n"
               b"caf\xe9\r\n--B\r\nContent-Type: text/plain; charset*=''koi8-r\r\n\r\n"
               + "Привет".encode("koi8-r") + b"\r\n--B--\r\n")
        mail = gw.parse_mail(raw)
        assert [p.file_name for p in mail.parts] == [long_name, "café.txt"]
        assert mail.plain == "Привет"
    print("ok: a mail is decoded under charsets only — never under a codec that is none")


def test_the_fields_whose_parameters_are_parsed_are_bounded():
    """Content-Type and Content-Disposition are the fields whose parameters
    are parsed, by the reader and by the MIME parser itself — which, before
    its fix, took time with the square of the field's length (half a minute
    for 200 kB, on every pass). A part's header block lies in the body, where
    the cap on the mail's own header block does not reach."""
    import email.message as stdlib
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        junk = b"; a=\"" + b";" * 300_000
        folded = b"".join(b";\r\n x%d=\"%s" % (n, b";" * 60) for n in range(4000))
        raw = (HEAD + b"Message-ID: <q@x.org>\r\nSubject: Bounded\r\n"
               b"Content-Type: multipart/mixed; boundary=B" + junk[:100_000] + b"\r\n\r\n"
               b"--B\r\nContent-Type: text/plain; charset=utf-8" + junk + b"\r\n\r\nThe text.\r\n"
               b"--B\r\nContent-Type: application/pdf; name=\"a.pdf\"" + junk + b"\r\n"
               b"content-disposition: attachment; filename=\"a.pdf\"" + folded + b"\r\n\r\n%PDF\r\n"
               b"--B\r\nCONTENT-TYPE: multipart/mixed; boundary=C" + folded + b"\r\n\r\n"
               b"--C\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; "
               b"filename=in.txt\r\n\r\ninner\r\n--C--\r\n--B--\r\n")
        assert len(raw) > 1_200_000
        # Wherever the parser reads a header block: a nested mail's, and each
        # block of a delivery report — with any kind of line end, and as the
        # very first field.
        nested = (HEAD + b"Message-ID: <n@x.org>\r\nContent-Type: multipart/mixed; boundary=B\r\n"
                  b"\r\n--B\r\nContent-Type: message/rfc822\r\n\r\n"
                  b"From: a@x.org\r\nContent-Type: text/plain; charset=utf-8" + junk + b"\r\n"
                  b"\r\ninner text\r\n"
                  b"--B\r\nContent-Type: message/delivery-status\r\n\r\n"
                  b"Reporting-MTA: dns; x.org\r\n\r\n"
                  b"Content-Disposition: inline" + junk + b"\r\nStatus: 5.0.0\r\n"
                  b"\r\n--B--\r\n")
        bare = b"A: b\rContent-Type: text/x; a=\"" + b";" * 9000 + b"\r\tmore\rB: c\r\rbody"
        first = b"content-disposition: x" + b";" * 9000 + b"\r\nA: b\r\n\r\nbody"
        seen = []
        real = stdlib._parseparam
        stdlib._parseparam = lambda s: (seen.append(len(str(s))), real(s))[1]
        try:
            mail = gw.parse_mail(raw)
            stub = gw.stub_mail(raw, None, "x")
            inner = gw.parse_mail(nested)
            for part in gw._parse_bytes(nested).walk():
                part.get_content_type(), part.get_content_disposition()
            odd = [gw.parse_mail(bare), gw.parse_mail(first)]
        finally:
            stdlib._parseparam = real
        # (The parser puts one ";" before the value it parses.)
        assert len(seen) > 12 and max(seen) <= gw.MAX_FIELD_BYTES + 1, max(seen)
        # What stood before the cut is read, and the mail is whole.
        assert mail.message_id == stub.message_id == "q@x.org" and mail.subject == "Bounded"
        assert mail.plain == "The text."
        assert [(p.file_name, p.data) for p in mail.parts] == [("a.pdf", b"%PDF"),
                                                              ("in.txt", b"inner")]
        assert [p.content_type for p in inner.parts] == ["message/rfc822",
                                                         "message/delivery-status"]
        assert b"inner text" in inner.parts[0].data
        assert len(inner.parts[0].data) < gw.MAX_FIELD_BYTES + 200
        assert [p.data for p in odd[0].parts] == [b"body"] and odd[1].plain == "body"
    print("ok: a Content-Type or Content-Disposition field is cut to its bound before "
          "anything parses its parameters")


def test_a_line_that_begins_like_a_field_is_cut_only_where_headers_are():
    """The bound on the two structured fields is the parser's own reading of
    a header block. A line of a body or of an attachment that begins with
    ``Content-Type:`` — a pasted HTTP capture, a log, a form dump — is
    content: every part is kept, byte for byte."""
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        indented = "".join(f"    line {n}: indented text that belongs to the body\n"
                           for n in range(200))
        body = ("See the capture below.\n\nContent-Type: application/json\n" + indented
                + "\nContent-Disposition: " + "x" * 6000 + "\nRegards\n")
        assert len(body) > 3 * gw.MAX_FIELD_BYTES
        raw = (HEAD + b"Message-ID: <capture@x.org>\r\nSubject: Capture\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
               + body.replace("\n", "\r\n").encode())
        mail = gw.parse_mail(raw)
        assert mail.plain == body, (len(mail.plain), len(body))
        # The same in an attachment that is not base64: a log, with one long
        # tab-continued line after such a line, and one with bare line ends.
        log = (b"name: x\r\nContent-Disposition: form-data\r\n" + b"\tvalue " * 2000
               + b"\r\nend\r\n")
        bare = b"a\rContent-Type: text/x" + b";" * 9000 + b"\r\tmore\rz"
        multi = (HEAD + b"Message-ID: <log@x.org>\r\nSubject: Log\r\n"
                 b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
                 b"--B\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
                 b"--B\r\nContent-Type: text/x-log\r\n"
                 b"Content-Disposition: attachment; filename=dump.log\r\n\r\n" + log
                 + b"\r\n--B\r\nContent-Type: application/octet-stream\r\n"
                 b"Content-Disposition: attachment; filename=bare.bin\r\n\r\n" + bare
                 + b"\r\n--B--\r\n")
        parts = {p.file_name: p.data for p in gw.parse_mail(multi).parts}
        assert parts == {"dump.log": log, "bare.bin": bare}, {k: len(v) for k, v in parts.items()}
        # And so it is mirrored: the body blob and the stored file are the mail's.
        record, _mirror, _box = ingest(gw, raw)
        assert t.blob(gw, record["body_part"]).decode() == body
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        record, _mirror, _box = ingest(gw, multi)
        assert [(name, data) for name, _kind, data, _cid in files(gw, record)] == [
            ("bare.bin", bare), ("dump.log", log)]
    print("ok: a line of a body or an attachment that begins like a header field is kept whole")


def test_a_charset_the_parser_raises_on_is_still_read():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        # The standard library applies the declared charset when it hands a
        # leaf over, and catches only "unknown": these make it raise.
        for charset in (b"undefined", b"idna", b"utf-8\x00x", b"unicode_internal"):
            raw = (HEAD + b"Message-ID: <c@x.org>\r\nSubject: s\r\n"
                   b"Content-Type: text/plain; charset=" + charset + b"\r\n"
                   b"Content-Transfer-Encoding: 8bit\r\n\r\n"
                   + "Please pay €500".encode("utf-8") + b"\r\n")
            mail = gw.parse_mail(raw)
            assert mail.plain == "Please pay €500\n", (charset, mail.plain)
        record, _mirror, _box = ingest(gw, raw)
        assert record["text"] == "Please pay €500"
        # One part that cannot be read costs that part, not the mail.
        whole = t.msg("two@x.org", body="Text.\n",
                      files=[("a.bin", "application/octet-stream", b"aa"),
                             ("b.bin", "application/octet-stream", b"bb")])
        real = gw._part_bytes

        def part_bytes(part):
            if part.get_filename() == "a.bin":
                raise RuntimeError("a part the reader chokes on")
            return real(part)
        gw._part_bytes = part_bytes
        mail = gw.parse_mail(whole)
        assert mail.plain == "Text.\n" and [p.file_name for p in mail.parts] == ["b.bin"]
        assert mail.parts_dropped == 1
        # A Date no clock reaches is no date, on every Python version.
        raw = (b"From: a@x.org\r\nMessage-ID: <d@x.org>\r\n"
               b"Date: Mon, 1 Jan 2024 00:00:00 +99999999999999999999\r\n\r\nx\r\n")
        assert gw.parse_mail(raw, internal_date=5.0).date == 5.0
    print("ok: a charset or a date the standard library raises on does not cost the mail")


def test_addresses_hold_nothing_unseen():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        # A right-to-left override makes "‮moc.knab@oec" read as ceo@bank.com.
        raw = (b"From: \xe2\x80\xaemoc.knab@oec\r\nTo: me@example.org, "
               b"p\xe2\x80\x8bal@y.org, ok@y.org, a\xc2\xadb@y.org, b@\xef\xbb\xbfy.org\r\n"
               b"Cc: c\xc2\x85d@y.org\r\n"
               b"Message-ID: <rlo@x.org>\r\nSubject: s\r\n\r\nbody\r\n")
        mail = gw.parse_mail(raw)
        # Not stripped into some other address: not an address at all.
        assert mail.sender == "" and mail.sender_name is None, repr(mail.sender)
        assert mail.to == [("me@example.org", None), ("ok@y.org", None)], mail.to
        assert mail.cc == []
        record, mirror, _box = ingest(gw, raw)
        assert record["sender"] == "unknown" and record["recipients"] == [
            "me@example.org", "ok@y.org"]
        assert mirror.thread(record["chat"])["peer"] == {"address": "ok@y.org", "name": None}
        # An internationalised address is one: letters are seen.
        mail = gw.parse_mail("From: Jürgen <jürgen@bücher.example>\r\n"
                             "Message-ID: <i@x.org>\r\n\r\nx\r\n".encode("utf-8"))
        assert mail.sender == "jürgen@bücher.example"
    print("ok: an address with an invisible or direction-changing character is no address")


def test_what_one_mail_may_ask_for_is_bounded():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        assert gw.MAX_PARTS == 200
        # A part costs its sender thirty bytes; the ledger, two files each.
        extra = 50
        parts = b"".join(b"--B\r\nContent-Type: application/x-thing\r\n"
                         b"Content-Disposition: attachment; filename=\"f%d.bin\"\r\n\r\nx\r\n" % n
                         for n in range(gw.MAX_PARTS + extra))
        raw = (HEAD + b"Message-ID: <many@x.org>\r\nSubject: Many\r\n"
               b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
               b"--B\r\nContent-Type: text/plain\r\n\r\nThe text.\r\n" + parts + b"--B--\r\n")
        mail = gw.parse_mail(raw)
        assert len(mail.parts) == gw.MAX_PARTS and mail.parts_dropped == extra
        assert mail.plain == "The text."
        record, _mirror, _box = ingest(gw, raw)
        got = files(gw, record)
        assert len(got) == gw.MAX_PARTS + 1
        note = next(data for name, _k, data, _c in got if name == "attachments.not-mirrored.txt")
        note = note.decode()
        assert f"{extra} further part(s)" in note and "f200.bin" in note and "f219.bin" in note
        assert "f220.bin" not in note, "the note names a few, not all"
        blobs = [p for p in gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir()
                 if "." not in p.name]
        assert len(blobs) == gw.MAX_PARTS + 1 + 2      # the parts, the note, body + headers
        assert record["text"] == "The text."
    with tempfile.TemporaryDirectory() as tmp:
        # Beyond what is worth parsing at all (the parser builds an object
        # per part, and they are counted as it does): a placeholder.
        gw = t.load(tmp)
        gw.MAX_PARSED_PARTS = 300
        raw = (HEAD + b"Message-ID: <flood@x.org>\r\nSubject: Flood\r\n"
               b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
               + b"--B\r\n\r\nx\r\n" * 301 + b"--B--\r\n")
        # The same flood under a boundary a part declares, and as the blocks
        # of a delivery report (which need no boundary line at all).
        inner = (HEAD + b"Message-ID: <inner@x.org>\r\n"
                 b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
                 b"--B\r\nContent-Type: multipart/digest; boundary=\"in ner\"\r\n\r\n"
                 + b"--in ner\r\n\r\nx\r\n" * 301 + b"--in ner--\r\n--B--\r\n")
        report = (HEAD + b"Message-ID: <dsn@x.org>\r\n"
                  b"Content-Type: message/delivery-status\r\n\r\n"
                  + b"Status: 5.0.0\r\n\r\n" * 301)
        for flood in (raw, inner, report):
            try:
                gw.parse_mail(flood)
                raise AssertionError("a mail of more parts than that must not be parsed")
            except gw.UnreadableMail:
                pass
        record, _mirror, _box = ingest(gw, raw)
        assert record["text"] == gw.UNREADABLE_TEXT and record["sender"] == "ann@x.org"
        assert record["message_id"] == "flood@x.org" and record["attachments"] == []
        # Dashes in a header block are not boundary lines.
        gw.parse_mail(HEAD + b"X-A: b\r\n --c\r\n" * 400 + b"Message-ID: <h@x.org>\r\n\r\nx\r\n")
        # Nor are the lines of a mail that has no parts: a diff, a dump or a
        # log sent as plain text is read as what it is.
        dump = b"-- a comment\r\n--- a/file\r\n" * 400
        mail = gw.parse_mail(HEAD + b"Message-ID: <dump@x.org>\r\n\r\n" + dump)
        assert mail.plain == dump.decode().replace("\r\n", "\n") and mail.parts == []
    with tempfile.TemporaryDirectory() as tmp:
        # A header block is read up to MAX_HEADER_BYTES, at a whole line; the
        # mail behind it is still a mail.
        gw = t.load(tmp)
        gw.MAX_HEADER_BYTES = 4096
        junk = b"".join(b"X-Junk-%d: %s\r\n" % (n, b"j" * 60) for n in range(200))
        raw = (HEAD + b"Message-ID: <head@x.org>\r\nSubject: Head\r\n" + junk
               + b"X-Late: beyond\r\n\r\nThe body.\r\n")
        mail = gw.parse_mail(raw)
        assert mail.message_id == "head@x.org" and mail.subject == "Head"
        assert mail.plain == "The body.\n"
        assert len(mail.headers) <= 4096 and "X-Late" not in mail.headers
        assert mail.headers.endswith("j" * 60), "cut at a line end"
        # What is found again by its headers is identified the same way.
        assert gw.stub_mail(raw, None, "x").message_id == "head@x.org"
        # An inline part's Content-ID too long to be one is not carried.
        raw = (HEAD + b"Message-ID: <cid@x.org>\r\nContent-Type: multipart/related; boundary=B\r\n"
               b"\r\n--B\r\nContent-Type: text/html\r\n\r\n<p>x</p>\r\n--B\r\n"
               b"Content-Type: image/png\r\nContent-ID: <" + b"c" * 5000 + b"@x.org>\r\n\r\n"
               + png(40, 40) + b"\r\n--B--\r\n")
        assert [p.content_id for p in gw.parse_mail(raw).parts] == [None]
    print("ok: the parts of one mail, kept and parsed, and its header block are bounded")


def test_lines_that_begin_with_dashes_are_not_boundary_lines():
    """The bound on a mail's parts counts the parts the parser makes of it —
    the lines of the mail's declared boundaries — and no line that merely
    begins with ``--``. A dump of sixty thousand SQL comments, a long diff or
    a log, attached 8bit, is an ordinary mail of two parts."""
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        dump = b"".join(b"-- comment %d\r\n--XyZ12\r\n--XyZ123x\r\nSELECT %d;\r\n" % (n, n)
                        for n in range(30_000))
        raw = (HEAD + b"Message-ID: <sql@x.org>\r\nSubject: The dump\r\n"
               b"Content-Type: multipart/mixed; boundary=\"XyZ123\"\r\n\r\n"
               b"--XyZ123\r\nContent-Type: text/plain\r\n\r\n"
               b"Here is the dump you asked for.\r\n"
               b"--XyZ123\r\nContent-Type: application/sql\r\n"
               b"Content-Transfer-Encoding: 8bit\r\n"
               b"Content-Disposition: attachment; filename=dump.sql\r\n\r\n" + dump
               + b"\r\n--XyZ123--\r\n")
        assert raw.count(b"\n--") > gw.MAX_PARSED_PARTS
        mail = gw.parse_mail(raw)
        assert mail.plain == "Here is the dump you asked for."
        assert [(p.file_name, p.data) for p in mail.parts] == [("dump.sql", dump)]
        record, mirror, _box = ingest(gw, raw)
        assert record["text"] == "Here is the dump you asked for." and record["body_part"]
        assert [(name, data) for name, _kind, data, _cid in files(gw, record)] == [
            ("dump.sql", dump)]
        assert mirror.unmirrored() == 0 and mirror._placeholders == set()
    print("ok: lines of a body or an attachment that begin with \"--\" are not counted as "
          "boundary lines")


def test_rfc2047_names():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        raw = (b"From: =?UTF-8?B?TcO8bGxlciwgSsO8cmdlbg==?= <jm@x.org>\r\n"
               b"To: =?iso-8859-1?Q?Andr=E9?= <andre@y.org>,\r\n"
               b" \"Doe, Jane\" <jane@z.org>, plain@z.org, me@example.org\r\n"
               b"Cc: =?UTF-8?Q?=E5=B1=B1=E7=94=B0?= <yamada@j.example>\r\n"
               b"Subject: =?UTF-8?B?UsOpc3Vtw6k=?=\r\n =?UTF-8?B?IGRlIHLDqXVuaW9u?=\r\n"
               b"Message-ID: <n@x.org>\r\n" + DATE.encode() + b"\r\nbody\r\n")
        mail = gw.parse_mail(raw)
        # The comma inside the decoded name does not split the address.
        assert (mail.sender, mail.sender_name) == ("jm@x.org", "Müller, Jürgen")
        assert mail.to == [("andre@y.org", "André"), ("jane@z.org", "Doe, Jane"),
                           ("plain@z.org", None), ("me@example.org", None)]
        assert mail.cc == [("yamada@j.example", "山田")]
        assert mail.subject == "Résumé de réunion"
        record, mirror, _box = ingest(gw, raw)
        assert record["sender_name"] == "Müller, Jürgen" and record["subject_line"] == mail.subject
        assert record["recipients"] == ["andre@y.org", "jane@z.org", "me@example.org",
                                        "plain@z.org"]     # a set, in the ledger
        thread = mirror.thread("n@x.org")
        assert thread["participants"][0] == {"address": "jm@x.org", "name": "Müller, Jürgen"}
        assert {"address": "yamada@j.example", "name": "山田"} in thread["participants"]
    print("ok: RFC 2047 names and subjects are decoded after the addresses are split")


# --------------------------------------------------------------------------
# Text from outside
# --------------------------------------------------------------------------

def test_a_hostile_file_name_and_content_type_stay_data():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp, token="tok")
        rlo = "\u202e"
        raw = (HEAD + b"Message-ID: <evil@x.org>\r\nSubject: files\r\n"
               b"Content-Type: multipart/mixed; boundary=E\r\n\r\n"
               b"--E\r\nContent-Type: text/plain\r\n\r\nfiles\r\n"
               # A path, with a line break smuggled in through RFC 2231.
               b"--E\r\nContent-Type: application/octet-stream\r\n"
               b"Content-Disposition: attachment;\r\n"
               b" filename*=utf-8''..%2F..%2Fetc%2Fpass%0D%0AX-Injected%3A%201wd\r\n\r\nA\r\n"
               # A type that is markup, and a name that reads backwards.
               b"--E\r\nContent-Type: text/html<script>alert(1)</script>;\r\n\tx=\"y\"\r\n"
               b"Content-Disposition: attachment; filename=\"invoice"
               + rlo.encode() + b"fdp.exe\"\r\n\r\nB\r\n"
               # A type with raw control bytes and no name at all.
               b"--E\r\nContent-Type: image/png\x00\x1b[31m\r\n"
               b"Content-Disposition: attachment\r\n\r\nC\r\n"
               # A Content-ID that is not an id.
               b"--E\r\nContent-Type: image/gif\r\nContent-ID: <a b\"><script>>\r\n"
               b"Content-Disposition: attachment; filename=\"C:\\\\Users\\\\x\\\\..\\\\y.gif\"\r\n"
               b"\r\nD\r\n--E--\r\n")
        record, mirror, _box = ingest(gw, raw)
        assert len(record["attachments"]) == 4
        for ref in record["attachments"]:
            meta = record["attachment_meta"][ref]
            name, kind = meta.get("file_name"), meta["content_type"]
            # One line of printable ASCII, whatever was declared …
            assert kind and all(0x20 < ord(ch) < 0x7f or ch == " " for ch in kind), repr(kind)
            assert "\n" not in kind and "\r" not in kind and "\x00" not in kind
            # … and a bare name: no path, no control character, no direction override.
            if name is not None:
                assert "/" not in name and "\\" not in name and name not in (".", ".."), name
                assert not any(ord(ch) < 0x20 or ch == rlo for ch in name), repr(name)
            # A Content-ID is one token or nothing.
            assert re.fullmatch(r"[^\s<>()\",]*", meta.get("content_id") or ""), meta
        names = sorted(m.get("file_name") or "" for m in record["attachment_meta"].values()
                       if m.get("file_name"))
        assert names == ["invoicefdp.exe", "passX-Injected: 1wd", "y.gif"], names
        # The blob is addressed by its own id only, and served with a one-line type.
        gw.MIRROR = mirror
        server, base = t.serve(gw)
        try:
            for ref in record["attachments"]:
                status, headers, _body = t.call(
                    f"{base}/media/{gw._ibstore.media_id_of(ref)}", token="tok")
                assert status == 200 and "X-Injected" not in headers
                assert headers["Content-Type"] == record["attachment_meta"][ref]["content_type"]
        finally:
            server.shutdown()
        stored = [p.name for p in gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir()]
        assert all(len(n.split(".")[0]) == 32 for n in stored), stored
    print("ok: a declared file name or content type never becomes a path or a header line")


def test_hostile_headers_are_reduced_to_their_shape():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        rlo, zw = "\u202e", "\u2028"
        raw = (("From: \"Bank" + rlo + " Support\x07" + zw + "</b><script>\" <a@bank.example>\r\n"
                "To: not-an-address, <x@y.org\r\n Bcc: z@evil.example>, \"N\" <ok@y.org>\r\n"
                "Subject: Line one\r\n\tLine two\x00\x1b[2J" + zw + "three\r\n"
                "Message-ID: <id with spaces@x.org>\r\n"
                "In-Reply-To: (see <fake@x.org>) <real@x.org>\r\n"
                "References: prose about a@b and nothing else\r\n"
                "List-Id: <evil list\x00.example.org>\r\n"
                ).encode("utf-8") + DATE.encode() + b"\r\nbody\r\n")
        mail = gw.parse_mail(raw)
        assert mail.sender == "a@bank.example"
        assert mail.sender_name == "Bank Support </b><script>", repr(mail.sender_name)
        # A header that does not read as an address list is nobody's address:
        # no part of it is guessed at.
        assert mail.to == [], mail.to
        assert mail.subject == "Line one Line two [2J three", repr(mail.subject)
        # Not a usable id: the mail gets a synthetic one, and cites what really is one.
        assert mail.message_id.endswith("@no-message-id.mail.invalid")
        assert mail.in_reply_to == "real@x.org" and mail.references == []
        record, _mirror, _box = ingest(gw, raw)
        assert record["list_id"] is None and record["chat"] == "real@x.org"
        # No sender at all, or an unreadable one: "unknown", never a guess.
        nobody = gw.parse_mail(b"Subject: x\r\nMessage-ID: <nb@x.org>\r\n\r\nbody\r\n")
        assert nobody.sender == "" and nobody.sender_name is None
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        record, _mirror, _box = ingest(
            gw, b"Subject: x\r\nMessage-ID: <nb@x.org>\r\n\r\nbody\r\n")
        assert record["sender"] == "unknown" and record["subject_line"] == "x"
    print("ok: names, subjects, addresses and ids are reduced to what their field is")


def test_a_missing_message_id_gets_a_stable_one():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        events = t.rail(gw)
        mirror, box = t.started(gw)
        raw = t.msg(None, sender=t.ME, to=t.ACCOUNT, subject="Note", day=1)
        first = gw.parse_mail(raw).message_id
        assert first.endswith("@no-message-id.mail.invalid") and len(first) < 80
        assert gw.parse_mail(raw).message_id == first, "the same mail, the same id"
        other = gw.parse_mail(t.msg(None, sender=t.ME, to=t.ACCOUNT, subject="Note 2", day=1))
        assert other.message_id != first
        # So its two folder copies are still one mail, and a restart reads it once.
        box.put("Sent", raw)
        box.put("INBOX", raw)
        box.put("INBOX", t.msg(None, subject="Another", day=2))
        mirror.poll()
        assert sorted(r["message_id"] == first for r in t.records(gw)) == [False, True]
        mirror = gw.Mirror(box, clock=lambda: t.NOW)
        mirror.poll()
        assert len(t.records(gw)) == 2 and len(events) == 2
        # It can be followed into the archive although the server cannot search for it.
        uid = next(u for u, m in box.boxes["INBOX"]["mails"].items() if b"Another" in m["raw"])
        box.move_mail("INBOX", uid, "Archive")
        mirror.poll()
        assert not any(r["removed_at"] for r in t.records(gw))
    with tempfile.TemporaryDirectory() as tmp:
        # Mails with none of the headers the id is derived from — never there,
        # or pushed beyond what is read of a header block — are not all "the
        # same mail": each is told by its header block, and each is mirrored.
        gw = t.load(tmp)
        t.rail(gw)
        mirror, box = t.started(gw)

        def buried(n: int) -> bytes:
            return (b"Received: from mx%d.example by mail.example.org; " % n + DATE.encode()[6:]
                    + b"X-Filler: " + b"x" * (gw.MAX_HEADER_BYTES + 1000) + b"\r\n"
                    + b"Message-ID: <buried%d@x.org>\r\nFrom: someone%d@x.org\r\n\r\n" % (n, n)
                    + b"Body %d.\r\n" % n)
        bare = [b"Received: by mail.example.org id %d\r\n\r\nNo headers %d.\r\n" % (n, n)
                for n in (1, 2)]
        ids = {gw.parse_mail(raw).message_id for raw in (buried(1), buried(2), *bare)}
        assert len(ids) == 4 and all(i.endswith("@no-message-id.mail.invalid") for i in ids)
        assert gw.parse_mail(buried(1)).message_id == gw.parse_mail(buried(1)).message_id
        uids = [box.put("INBOX", raw, date=t.NOW) for raw in (buried(1), buried(2), *bare)]
        mirror.poll()
        assert sorted(r["text"] for r in t.records(gw)) == [
            "Body 1.", "Body 2.", "No headers 1.", "No headers 2."]
        # Followed like any mail: the peek at a header block gives the same id.
        box.move_mail("INBOX", uids[0], "Archive")
        box.move_mail("INBOX", uids[2], "Archive")
        box.remove("INBOX", uids[3])
        for _ in range(3):
            mirror.poll()
        assert sorted(r["text"] for r in t.records(gw) if not r["removed_at"]) == [
            "Body 1.", "Body 2.", "No headers 1."]
        assert mirror.index.one("SELECT COUNT(*) AS n FROM locations WHERE role = 'archive'")[
            "n"] == 2
    print("ok: a mail without a Message-ID gets one derived from its headers")


def test_an_overlong_message_id_is_digested_consistently():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        t.rail(gw)
        mirror, box = t.started(gw)
        long_id = "x" * 400 + "@x.org"
        # (Put in as bytes: how a header this long is written out by the
        # library that builds the test mails differs between its versions.)
        box.put("INBOX", t.msg("stand-in@x.org", day=1).replace(
            b"stand-in@x.org", long_id.encode()))
        box.put("INBOX", t.msg("reply@x.org", refs=["stand-in@x.org"], irt="stand-in@x.org",
                               day=2).replace(b"stand-in@x.org", long_id.encode()))
        mirror.poll()
        recs = t.records(gw)
        key = recs[0]["message_id"]
        assert key.endswith("@overlong-message-id.mail.invalid") and len(key) <= gw.MAX_ID_CHARS
        # The reply cites the same digest, so the thread holds.
        assert {r["chat"] for r in recs} == {key}
        assert recs[1]["in_reply_to"] == key and recs[1]["references"] == key
        # And the mail is still followed, by its headers.
        box.move_mail("INBOX", 1, "Archive")
        mirror.poll()
        assert not any(r["removed_at"] for r in t.records(gw))
    print("ok: an overlong Message-ID is replaced by a digest wherever it appears")


def test_the_date_is_the_senders_claim():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        arrived = t.T0 + 5 * t.DAY

        def dated(header: bytes):
            raw = b"From: a@x.org\r\nMessage-ID: <d@x.org>\r\n" + header + b"\r\nbody\r\n"
            return gw.parse_mail(raw, arrived).date
        # Not later than the server received it; a forged future date cannot
        # pin a mail to the top of its chat.
        assert dated(b"Date: Mon, 01 Jan 2035 00:00:00 +0000\r\n") == arrived
        assert dated(b"Date: Sat, 11 Oct 2025 08:53:20 +0000\r\n") == t.T0 + 2 * t.DAY
        assert dated(b"") == arrived and dated(b"Date: yesterday-ish\r\n") == arrived
        assert dated(b"Date: Thu, 01 Jan 1900 00:00:00 +0000\r\n") == arrived
        assert gw.parse_mail(b"From: a@x.org\r\n\r\nbody\r\n").date is None
        # With no date from anywhere, the record is stamped when it was mirrored.
        t.rail(gw)
        mirror, box = t.started(gw)
        box.boxes["INBOX"]["mails"][1] = {"raw": b"From: a@x.org\r\n\r\nbody\r\n",
                                          "flags": frozenset(), "date": None}
        box.boxes["INBOX"]["next"] = 2
        # (the fake's SINCE needs a date to compare; new mail is found by UID)
        mirror.poll()
        assert t.records(gw)[0]["received_at"] == gw._ibstore._iso(t.NOW)
    print("ok: a mail's Date is used only where it is not later than its arrival")


def test_broken_structures_are_still_read():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        # A multipart with no boundary to be found: the raw body is the text.
        raw = (HEAD + b"Message-ID: <nb@x.org>\r\nContent-Type: multipart/mixed\r\n\r\n"
               b"just text after all\r\n")
        assert gw.parse_mail(raw).plain == "just text after all\n"
        # Cut off inside an attachment (what a capped fetch returns).
        whole = t.msg("cut@x.org", body="Text first.\n",
                      files=[("big.bin", "application/octet-stream", b"\x00" * 5000)])
        mail = gw.parse_mail(whole[:len(whole) - 3000])
        assert mail.plain == "Text first.\n" and len(mail.parts) == 1
        # Nesting far beyond anything a client writes ends, without a crash.
        deep = b"".join(b"Content-Type: multipart/mixed; boundary=b%d\r\n\r\n--b%d\r\n" % (i, i)
                        for i in range(120))
        mail = gw.parse_mail(HEAD + b"Message-ID: <deep@x.org>\r\n" + deep
                             + b"Content-Type: text/plain\r\n\r\nbottom\r\n")
        assert mail.message_id == "deep@x.org"
        assert gw.parse_mail(b"").message_id.endswith("@no-message-id.mail.invalid")
        assert gw.parse_mail(b"\x00\xff\xfe garbage").plain is not None
    print("ok: a multipart without parts, a cut-off mail and absurd nesting are read")


def test_one_unreadable_mail_does_not_stop_the_folder():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        events = t.rail(gw)
        mirror, box = t.started(gw)
        box.put("INBOX", t.msg("a@x.org", day=1))
        box.put("INBOX", t.msg("bad@x.org", subject="poison", body="Please pay 500.\n",
                               refs=["a@x.org"], day=2,
                               files=[("x.pdf", "application/pdf", b"%PDF")]))
        box.put("INBOX", t.msg("c@x.org", day=3))
        real = gw.parse_mail

        def parse(raw, internal_date=None):
            if b"poison" in raw:
                raise RuntimeError("a parser bug")
            return real(raw, internal_date)
        gw.parse_mail = parse
        mirror.poll()
        recs = t.by_id(gw)
        assert sorted(recs) == ["a@x.org", "bad@x.org", "c@x.org"]
        # It does not vanish either: a placeholder says a mail from this
        # sender, at this time, could not be shown — under the mail's own id,
        # in its thread, with its header block and nothing else of it.
        bad = recs["bad@x.org"]
        assert bad["text"] == gw.UNREADABLE_TEXT and bad["trim"] == "deterministic"
        assert bad["sender"] == "ann@x.org" and bad["chat"] == "a@x.org"
        assert bad["received_at"] == gw._ibstore._iso(t.T0 + 2 * t.DAY)
        assert bad["references"] == "a@x.org" and bad["delivered"] is True
        assert bad["subject_line"] is None and bad["sender_name"] is None
        assert bad["body_part"] is None and bad["html_part"] is None and bad["attachments"] == []
        assert b"Message-ID: <bad@x.org>" in t.blob(gw, bad["header_part"])
        event = next(e for e in t.inbound(events) if e["message_id"] == "bad@x.org")
        assert event["text"] == gw.UNREADABLE_TEXT and event["chat"] == "a@x.org"
        assert mirror.thread("a@x.org")["messages"] == 2
        # Recorded, so it is not read on every poll for ever, and followed
        # like any mail: archived it stays, deleted it leaves a tombstone.
        fetched = len(box.fetched)
        mirror.poll()
        assert len(box.fetched) == fetched and len(t.records(gw)) == 3
        assert mirror.unmirrored() == 0 and mirror.health_notice() is None

        # The next start reads it again — the fault may have been the reader's,
        # and a reader that is fixed must not leave the placeholder standing.
        # Still unreadable, it stays exactly as it is: nothing is dropped to
        # try, and the rail hears nothing.
        before = (Path(gw._ibstore.messages_dir(gw.INBOUND_STORE_DIR)) / "x").parent
        before = {p.name: p.read_text() for p in before.glob("*.nt")}
        told = len(events)
        mirror = gw.Mirror(box, clock=lambda: t.NOW)
        mirror.poll()
        assert box.fetched[-1][:2] == ("INBOX", box.uid_of("INBOX", "bad@x.org"))
        assert t.by_id(gw)["bad@x.org"]["text"] == gw.UNREADABLE_TEXT, "still unreadable"
        assert len(t.records(gw)) == 3 and len(events) == told
        assert before == {p.name: p.read_text() for p in Path(
            gw._ibstore.messages_dir(gw.INBOUND_STORE_DIR)).glob("*.nt")}
        gw.parse_mail = real
        mirror = gw.Mirror(box, clock=lambda: t.NOW)
        while mirror.poll():
            pass
        recs = t.by_id(gw)
        assert sorted(recs) == ["a@x.org", "bad@x.org", "c@x.org"] and len(t.records(gw)) == 3
        bad = recs["bad@x.org"]
        assert bad["text"] == "Please pay 500." and bad["subject_line"] == "poison"
        assert bad["body_part"] and len(bad["attachments"]) == 1 and bad["chat"] == "a@x.org"
        assert mirror.backfill_status()["done"] is True
        # Nothing of the placeholder is left behind in the media store.
        named = {gw._ibstore.media_id_of(ref) for r in t.records(gw)
                 for ref in gw._ibstore._media_refs(r)}
        on_disk = {p.name for p in gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir()
                   if "." not in p.name}
        assert on_disk == named
    with tempfile.TemporaryDirectory() as tmp:
        # An unreadable newsletter is still a newsletter: what decides that
        # (its sender, its List-Id) is read off the header block, and it
        # starts no chat as a placeholder either.
        gw = t.load(tmp)
        t._write_policy(gw, news={"dev.lists.x.org", "news@shop.example"})
        t.rail(gw)
        mirror, box = t.started(gw)
        box.put("INBOX", t.msg("first@x.org", day=1))
        box.put("INBOX", t.msg("l@x.org", sender="Dee <dee@x.org>", subject="poison",
                               list_id="Dev list <dev.lists.x.org>", day=1))
        box.put("INBOX", t.msg("s@shop.example", sender="news@shop.example",
                               subject="poison", day=1))
        box.put("INBOX", t.msg("p@x.org", subject="poison", day=1))
        box.put("INBOX", t.msg("last@x.org", day=1))
        real = gw.parse_mail
        gw.parse_mail = lambda raw, internal_date=None: (
            1 / 0 if b"poison" in raw else real(raw, internal_date))
        try:
            mirror.poll()
        finally:
            gw.parse_mail = real
        recs = t.by_id(gw)
        assert sorted(recs) == ["first@x.org", "last@x.org", "p@x.org"]
        assert recs["p@x.org"]["text"] == gw.UNREADABLE_TEXT
        assert sorted(row["message_id"] for row in mirror.index.rows(
            "SELECT message_id FROM locations WHERE skipped = 'news'")) == [
                "l@x.org", "s@shop.example"]
        assert mirror.in_doubt() is None and mirror._folder("inbox")["hwm"] == 5
    print("ok: a mail the reader chokes on is mirrored as a placeholder, and read again "
          "at the next start; the folder goes on")


def test_a_mail_whose_record_cannot_be_written_is_a_placeholder_too():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        t.rail(gw)
        mirror, box = t.started(gw)
        box.put("INBOX", t.msg("bad@x.org", subject="poison", day=1))
        box.put("INBOX", t.msg("ok@x.org", day=2))
        real = gw._trim.trim

        def trim(plain, html, subject=None, **evidence):
            if subject == "poison":
                raise RecursionError("a fault that this mail brings out, every time")
            if subject == "huge":
                # Memory is the mail's too: every other mail is mirrored.
                raise MemoryError()
            return real(plain, html, subject=subject, **evidence)
        gw._trim.trim = trim
        try:
            mirror.poll()
            recs = t.by_id(gw)
            assert sorted(recs) == ["bad@x.org", "ok@x.org"]
            assert recs["bad@x.org"]["text"] == gw.UNREADABLE_TEXT
            assert recs["bad@x.org"]["sender"] == "ann@x.org"
            assert recs["ok@x.org"]["text"] == "The numbers are in."
            # The first attempt left nothing behind: one record, its one blob.
            media = [p for p in gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir()
                     if "." not in p.name]
            assert len(media) == 1 + 2
            assert mirror.index.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0
            box.put("INBOX", t.msg("huge@x.org", subject="huge", day=2.5))
            mirror.poll()
            assert t.by_id(gw)["huge@x.org"]["text"] == gw.UNREADABLE_TEXT

            # Not even a placeholder can be written (a fault in the writer
            # itself, here): the mail is counted, logged, and the folder goes on.
            box.put("INBOX", t.msg("worse@x.org", subject="poison", day=3))
            box.put("INBOX", t.msg("after@x.org", day=4))
            real_write = gw._ibstore.write_message

            def write(store, **kw):
                if kw.get("message_id") == "worse@x.org":
                    raise TypeError("a writer bug")
                return real_write(store, **kw)
            gw._ibstore.write_message = write
            try:
                mirror.poll()
            finally:
                gw._ibstore.write_message = real_write
            assert sorted(t.by_id(gw)) == ["after@x.org", "bad@x.org", "huge@x.org", "ok@x.org"]
            assert mirror.unmirrored() == 1 and "1 mail(s)" in mirror.health_notice()
            gw.MIRROR = mirror
            assert gw._health_snapshot()["unmirrored"] == 1
            fetched = len(box.fetched)
            mirror.poll()
            assert len(box.fetched) == fetched
            # Deleted from the mailbox, it is no longer counted.
            box.remove("INBOX", box.uid_of("INBOX", "worse@x.org"))
            mirror.poll()
            assert mirror.unmirrored() == 0
        finally:
            gw._trim.trim = real
    with tempfile.TemporaryDirectory() as tmp:
        # What the disk or the mailbox raises is not about the mail: the pass
        # fails, nothing is set aside, and the next pass mirrors the mail.
        gw = t.load(tmp)
        t.rail(gw)
        mirror, box = t.started(gw)
        box.put("INBOX", t.msg("a@x.org", day=1))
        real_store = gw._ibstore.store_media
        gw._ibstore.store_media = lambda *a, **kw: (_ for _ in ()).throw(
            OSError(28, "No space left on device"))
        try:
            mirror.poll()
            raise AssertionError("a full disk must fail the pass")
        except OSError:
            pass
        finally:
            gw._ibstore.store_media = real_store
        assert t.records(gw) == [] and mirror.unmirrored() == 0
        mirror.poll()
        assert t.by_id(gw)["a@x.org"]["text"] == "The numbers are in."
    print("ok: a mail whose record cannot be written is a placeholder; a full disk is not "
          "the mail's fault")


class _Clock:
    def __init__(self):
        self.now = t.NOW

    def __call__(self):
        return self.now


def _looped(gw, mirror, loop: dict) -> dict:
    """One turn of the mirror loop with a look at the folders; what /health
    says after it."""
    gw.MIRROR = mirror
    loop.pop("synced", None)
    gw._loop_step(mirror, loop)
    return gw._health_snapshot()


def _whole(gw) -> list:
    return sorted(mid for mid, r in t.by_id(gw).items() if r["text"] != gw.UNREADABLE_TEXT)


def test_a_fault_of_the_mirror_is_not_laid_at_the_mails_door():
    """Failing closed per mail must not absorb a fault of the mirror itself.
    One that sets aside whatever it fails on turns a corrupt index, a bug
    every mail brings out, or memory gone for all of them into a mailbox of
    placeholders with every mark moved on — and /health still "connected"."""
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        # By its type: an index file that is corrupt is nobody's mail.
        gw = t.load(tmp)
        t._write_policy(gw)
        events = t.rail(gw)
        mirror, box = t.started(gw)
        loop: dict = {}
        real = mirror.threads.resolve
        mirror.threads.resolve = lambda *a, **kw: (_ for _ in ()).throw(
            sqlite3.DatabaseError("database disk image is malformed"))
        for n in range(5):
            box.put("INBOX", t.msg(f"n{n}@x.org", subject=f"s{n}", day=39.9))
        for _ in range(2):
            snap = _looped(gw, mirror, loop)
            assert snap["connected"] is False and "malformed" in snap["error"], snap
            assert snap["unmirrored"] == 0 and t.records(gw) == []
            assert mirror._folder("inbox")["hwm"] == 0, "no mark moves past unmirrored mail"
        # The fault goes away, and no restart is needed: the next poll has it all.
        mirror.threads.resolve = real
        snap = _looped(gw, mirror, loop)
        assert snap["connected"] is True and snap["error"] is None
        assert _whole(gw) == [f"n{n}@x.org" for n in range(5)]
        assert not any("backfill" in e for e in t.inbound(events))
        assert gw._environment(sqlite3.DatabaseError("file is not a database"))
        assert gw._environment(sqlite3.OperationalError("database is locked"))
        assert not gw._environment(sqlite3.IntegrityError("NOT NULL constraint failed"))
        assert not gw._environment(MemoryError()) and not gw._environment(TypeError("x"))
    for fault in (RuntimeError("a bug every mail brings out"), MemoryError()):
        with tempfile.TemporaryDirectory() as tmp:
            # By what it does to other mail: a fault no exception names. A
            # mirror that has mirrored nothing yet has nothing to show that a
            # failure is the mail's — none is set aside, none skipped.
            gw = t.load(tmp)
            t._write_policy(gw)
            events = t.rail(gw)
            mirror, box = t.started(gw)
            loop = {}
            real = gw._trim.trim
            gw._trim.trim = lambda *a, **kw: (_ for _ in ()).throw(fault)
            try:
                for n in range(3):
                    box.put("INBOX", t.msg(f"n{n}@x.org", subject=f"s{n}", day=39.9))
                for _ in range(2):
                    snap = _looped(gw, mirror, loop)
                    assert snap["connected"] is False, snap
                    assert "3 mail(s) could not be mirrored" in snap["error"]
                    assert "the mirror itself may be at fault" in snap["error"]
                    assert t.records(gw) == [] and snap["unmirrored"] == 0
                    assert mirror._folder("inbox")["hwm"] == 0
                    assert mirror.index.one("SELECT COUNT(*) AS n FROM locations")["n"] == 0
            finally:
                gw._trim.trim = real
            snap = _looped(gw, mirror, loop)
            assert snap["connected"] is True and snap["error"] is None
            assert _whole(gw) == ["n0@x.org", "n1@x.org", "n2@x.org"]
            assert mirror._folder("inbox")["hwm"] == 3 and len(t.inbound(events)) == 3
    with tempfile.TemporaryDirectory() as tmp:
        # The fault begins while the mirror runs. The mail before the first
        # failure was mirrored whole, which speaks for that one failure and
        # for no other: the next in a row are left in doubt, behind a mark
        # that does not move — and when the fault has passed, the one that
        # was set aside on that word is read again at once.
        gw = t.load(tmp)
        t._write_policy(gw)
        events = t.rail(gw)
        mirror, box = t.started(gw)
        loop = {}
        box.put("INBOX", t.msg("a@x.org", day=39.9))
        assert _looped(gw, mirror, loop)["connected"] is True
        real = gw._trim.trim
        gw._trim.trim = lambda *a, **kw: (_ for _ in ()).throw(MemoryError())
        try:
            for mid in ("b@x.org", "c@x.org", "d@x.org"):
                box.put("INBOX", t.msg(mid, day=39.9))
            snap = _looped(gw, mirror, loop)
            assert snap["connected"] is False and "2 mail(s)" in snap["error"], snap
            recs = t.by_id(gw)
            assert sorted(recs) == ["a@x.org", "b@x.org"]
            assert recs["b@x.org"]["text"] == gw.UNREADABLE_TEXT
            assert mirror._folder("inbox")["hwm"] == 2
        finally:
            gw._trim.trim = real
        snap = _looped(gw, mirror, loop)
        assert snap["connected"] is True and snap["error"] is None
        assert _whole(gw) == ["a@x.org", "b@x.org", "c@x.org", "d@x.org"]
        assert len(t.records(gw)) == 4 and mirror._folder("inbox")["hwm"] == 4
        assert mirror._placeholders == set()
    with tempfile.TemporaryDirectory() as tmp:
        # Not even a placeholder can be written, for any mail: next to
        # nothing of a mail goes into one, so this is never decided on the
        # word of the mail before. Nothing is marked unreadable and skipped.
        gw = t.load(tmp)
        t._write_policy(gw)
        t.rail(gw)
        mirror, box = t.started(gw)
        loop = {}
        box.put("INBOX", t.msg("a@x.org", day=39.9))
        _looped(gw, mirror, loop)
        real = gw._ibstore.write_message
        gw._ibstore.write_message = lambda *a, **kw: (_ for _ in ()).throw(
            TypeError("a writer bug"))
        try:
            box.put("INBOX", t.msg("b@x.org", day=39.9))
            box.put("INBOX", t.msg("c@x.org", day=39.9))
            snap = _looped(gw, mirror, loop)
            assert snap["connected"] is False and "2 mail(s)" in snap["error"], snap
            assert snap["unmirrored"] == 0 and sorted(t.by_id(gw)) == ["a@x.org"]
            assert mirror._folder("inbox")["hwm"] == 1
        finally:
            gw._ibstore.write_message = real
        assert _looped(gw, mirror, loop)["connected"] is True
        assert _whole(gw) == ["a@x.org", "b@x.org", "c@x.org"]
    print("ok: a corrupt index, a bug or memory gone for every mail is the mirror's fault: "
          "nothing is set aside or skipped on it, and /health says so")


def test_mail_in_doubt_holds_back_no_mail_behind_it():
    """The other side of it: mails that really cannot be read, several in a
    row, are set aside as soon as a mail behind them is mirrored — which is
    read all the same, while the mark waits below the ones in doubt."""
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        t._write_policy(gw)
        events = t.rail(gw)
        mirror, box = t.started(gw)
        loop: dict = {}
        real = gw.parse_mail
        gw.parse_mail = lambda raw, internal_date=None: (
            1 / 0 if b"poison" in raw else real(raw, internal_date))
        try:
            # A lone unreadable mail, and no other yet: the mirror cannot say
            # whose fault it is, and says that. The mail is read again …
            box.put("INBOX", t.msg("p1@x.org", subject="poison", day=39.9))
            for _ in range(2):
                snap = _looped(gw, mirror, loop)
                assert snap["connected"] is False and "1 mail(s)" in snap["error"]
            assert t.records(gw) == [] and len(box.fetched) == 2
            # … and more of them arrive, and then a mail that can be read.
            box.put("INBOX", t.msg("p2@x.org", subject="poison", day=39.9))
            box.put("INBOX", t.msg("p3@x.org", subject="poison", day=39.9))
            box.put("INBOX", t.msg("good@x.org", day=39.9))
            snap = _looped(gw, mirror, loop)
            assert snap["connected"] is True and snap["error"] is None, snap
            recs = t.by_id(gw)
            assert sorted(recs) == ["good@x.org", "p1@x.org", "p2@x.org", "p3@x.org"]
            assert _whole(gw) == ["good@x.org"] and mirror._folder("inbox")["hwm"] == 4
            assert mirror._doubt == {}
            # From here on one more unreadable mail is set aside at once, on
            # the word of the mail before it.
            box.put("INBOX", t.msg("p4@x.org", subject="poison", day=39.9))
            assert _looped(gw, mirror, loop)["connected"] is True
            assert t.by_id(gw)["p4@x.org"]["text"] == gw.UNREADABLE_TEXT
            # A mail in doubt that is deleted is nobody's fault any more.
            box.put("INBOX", t.msg("p5@x.org", subject="poison", day=39.9))
            assert _looped(gw, mirror, loop)["connected"] is False
            box.remove("INBOX", box.uid_of("INBOX", "p5@x.org"))
            assert _looped(gw, mirror, loop)["connected"] is True
            assert "p5@x.org" not in t.by_id(gw) and mirror._doubt == {}
            # While nothing vouches, only so many are read again per look.
            for n in range(gw.DOUBT_REREADS + 3):
                box.put("INBOX", t.msg(f"q{n}@x.org", subject="poison", day=39.9))
            _looped(gw, mirror, loop)
            fetched = len(box.fetched)
            snap = _looped(gw, mirror, loop)
            assert len(box.fetched) == fetched + gw.DOUBT_REREADS
            assert f"{gw.DOUBT_REREADS + 3} mail(s)" in snap["error"]
            box.put("INBOX", t.msg("good2@x.org", day=39.9))
            assert _looped(gw, mirror, loop)["connected"] is True
            assert len(t.records(gw)) == 5 + gw.DOUBT_REREADS + 3 + 1
        finally:
            gw.parse_mail = real
        assert len(t.inbound(events)) == len(t.records(gw))
    with tempfile.TemporaryDirectory() as tmp:
        # The same in a first backfill: the oldest mails cannot be read, the
        # ones after them can — the backfill reads on, and is done.
        gw = t.load(tmp)
        t.rail(gw)
        box = t.FakeMailbox(gw)
        for n in range(3):
            box.put("INBOX", t.msg(f"p{n}@x.org", subject="poison", day=1 + n))
        for n in range(4):
            box.put("INBOX", t.msg(f"g{n}@x.org", day=10 + n))
        real = gw.parse_mail
        gw.parse_mail = lambda raw, internal_date=None: (
            1 / 0 if b"poison" in raw else real(raw, internal_date))
        try:
            mirror = gw.Mirror(box, clock=lambda: t.NOW)
            while mirror.poll(backfill_limit=2):
                pass
            assert mirror.backfill_status()["done"] is True and mirror.in_doubt() is None
            assert _whole(gw) == [f"g{n}@x.org" for n in range(4)] and len(t.records(gw)) == 7
        finally:
            gw.parse_mail = real
    with tempfile.TemporaryDirectory() as tmp:
        # And a backfill under a mirror that reads nothing stops after a few
        # mails instead of fetching the whole window to learn it again.
        gw = t.load(tmp)
        t.rail(gw)
        box = t.FakeMailbox(gw)
        for n in range(40):
            box.put("INBOX", t.msg(f"o{n}@x.org", day=1 + n / 2))
        real = gw.parse_mail
        gw.parse_mail = lambda raw, internal_date=None: 1 / 0
        try:
            mirror = gw.Mirror(box, clock=lambda: t.NOW)
            for _ in range(3):
                assert mirror.poll(backfill_limit=10) is False
            assert len(box.fetched) == 3 * gw.DOUBT_REREADS and t.records(gw) == []
            assert mirror.backfill_status()["done"] is False and "may be at fault" in mirror.in_doubt()
        finally:
            gw.parse_mail = real
        while mirror.poll(backfill_limit=10):
            pass
        assert len(_whole(gw)) == 40 and mirror.backfill_status()["done"] is True
    print("ok: mail that cannot be read is set aside once a mail behind it is mirrored; "
          "mail in doubt holds none back and is not fetched without end")


def test_set_aside_mail_is_read_again_while_the_gateway_runs():
    """Not only at the next start: what made a mail unreadable may have been
    a moment the program was in. A placeholder, and a mail that could not
    even be one, are read again every hour — less often each time it changes
    nothing — and a mail that reads now takes its placeholder's place."""
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        t._write_policy(gw)
        events = t.rail(gw)
        clock = _Clock()
        box = t.FakeMailbox(gw)
        mirror = gw.Mirror(box, clock=clock)
        mirror.poll()
        box.put("INBOX", t.msg("a@x.org", day=39.8))
        box.put("Sent", t.msg("flaky@example.org", sender=t.ME, to="ann@x.org",
                              subject="flaky", body="What I wrote.\n", refs=["a@x.org"], day=39.9,
                              files=[("x.pdf", "application/pdf", b"%PDF")]))
        box.put("INBOX", t.msg("b@x.org", day=39.9))
        box.put("INBOX", t.msg("never@x.org", subject="never", day=39.9))
        box.put("INBOX", t.msg("c@x.org", day=39.9))
        box.put("INBOX", t.msg("worse@x.org", subject="never", day=39.9))
        box.put("INBOX", t.msg("d@x.org", day=39.9))
        real_trim, real_write = gw._trim.trim, gw._ibstore.write_message
        broken = {"flaky", "never"}

        def trim(plain, html, subject=None, **evidence):
            if subject in broken:
                raise RecursionError("this mail, for now")
            return real_trim(plain, html, subject=subject, **evidence)

        def write(store, **kw):
            if kw.get("message_id") == "worse@x.org" and "worse" not in fixed:
                raise TypeError("a writer bug")
            return real_write(store, **kw)
        fixed: set = set()
        gw._trim.trim, gw._ibstore.write_message = trim, write
        try:
            mirror.poll()
            mirror.poll()
            recs = t.by_id(gw)
            assert _whole(gw) == ["a@x.org", "b@x.org", "c@x.org", "d@x.org"]
            assert recs["flaky@example.org"]["type"] == gw._ibstore.T_OUTBOUND
            assert mirror._placeholders == {"flaky@example.org", "never@x.org"}
            assert mirror.unmirrored() == 1
            # Within the hour nothing is read again, however often it polls.
            fetched, told = len(box.fetched), len(events)
            clock.now += 1800
            mirror.poll()
            assert len(box.fetched) == fetched and len(events) == told
            # The moment has passed for one of them: after the hour it is
            # read, and the mail is on record whole — still what the user
            # sent, in its thread, announced quietly as old mail.
            broken.discard("flaky")
            before = Path(gw._ibstore.messages_dir(gw.INBOUND_STORE_DIR))
            before = {p.name: p.read_text() for p in before.glob("*.nt")
                      if "never@x.org" in p.read_text()}
            clock.now += 1800
            mirror.poll()
            flaky = t.by_id(gw)["flaky@example.org"]
            assert flaky["text"] == "What I wrote." and flaky["type"] == gw._ibstore.T_OUTBOUND
            assert flaky["chat"] == "a@x.org" and len(flaky["attachments"]) == 1
            assert len(box.fetched) == fetched + 3 and len(t.records(gw)) == 6
            again = [e for e in t.inbound(events)[told:]]
            assert [(e["message_id"], e["direction"], e.get("backfill")) for e in again] == [
                ("flaky@example.org", "out", True)]
            # The others are as they were: the same record, byte for byte,
            # the same count, and nothing said to the rail.
            assert before == {p.name: p.read_text() for p in Path(
                gw._ibstore.messages_dir(gw.INBOUND_STORE_DIR)).glob("*.nt")
                if "never@x.org" in p.read_text()}
            assert mirror._placeholders == {"never@x.org"} and mirror.unmirrored() == 1
            # Each try fetches the mail whole, so one that changes nothing is
            # tried less and less often: not after one more hour, but after two.
            fetched = len(box.fetched)
            clock.now += 3600
            mirror.poll()
            assert len(box.fetched) == fetched
            fixed.add("worse")
            clock.now += 3600
            mirror.poll()
            assert len(box.fetched) == fetched + 2 and mirror.unmirrored() == 0
            assert t.by_id(gw)["worse@x.org"]["text"] == gw.UNREADABLE_TEXT
            assert t.by_id(gw)["never@x.org"]["text"] == gw.UNREADABLE_TEXT
            assert mirror._placeholders == {"never@x.org", "worse@x.org"}
            # A placeholder whose mail is in the archive by now is read there
            # — but not while the archive does not answer: that is no reason
            # to fail a pass that needs only INBOX and Sent.
            box.move_mail("INBOX", box.uid_of("INBOX", "never@x.org"), "Archive")
            box.remove("INBOX", box.uid_of("INBOX", "worse@x.org"))
            mirror.poll()
            mirror._retried.clear()
            box.refused = {"Archive"}
            fetched = len(box.fetched)
            clock.now += 3600
            mirror.poll()
            assert len(box.fetched) == fetched and "would not open" in mirror.health_notice()
            box.refused = set()
            broken.discard("never")
            clock.now += 3600
            mirror.poll()
            assert box.fetched[fetched:] == [("Archive", 1, gw.MAIL_MAX_MAIL_BYTES)]
            assert t.by_id(gw)["never@x.org"]["text"] == "The numbers are in."
            assert mirror._placeholders == set() and t.by_id(gw)["worse@x.org"]["removed_at"]
        finally:
            gw._trim.trim, gw._ibstore.write_message = real_trim, real_write
        # Nothing of a replaced placeholder is left in the media store.
        named = {gw._ibstore.media_id_of(ref) for r in t.records(gw)
                 for ref in gw._ibstore._media_refs(r)}
        on_disk = {p.name for p in gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir()
                   if "." not in p.name}
        assert on_disk == named
    print("ok: set-aside mail is read again every hour while the gateway runs, and a mail "
          "that reads now replaces its placeholder")


# --------------------------------------------------------------------------
# Size caps
# --------------------------------------------------------------------------

def test_an_attachment_above_the_part_cap_is_not_stored():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp, MAIL_MAX_PART_BYTES=1000)
        raw = t.msg("big@x.org", body="Two files.\n" + "long text " * 500, files=[
            ("small.bin", "application/octet-stream", b"s" * 1000),
            ("../huge video.mov", "video/quicktime", b"v" * 1001)])
        record, _mirror, _box = ingest(gw, raw)
        # The text parts are always kept, cap or no cap.
        assert len(t.blob(gw, record["body_part"])) > 5000
        got = files(gw, record)
        assert [name for name, _k, _d, _c in got] == ["huge video.mov.not-mirrored.txt",
                                                      "small.bin"]
        assert got[1][2] == b"s" * 1000
        # In place of the file: a small text file under its name that says so.
        name, kind, note, _cid = got[0]
        assert kind == "text/plain; charset=utf-8" and len(note) < 600
        note = note.decode()
        assert "was not mirrored" in note and "huge video.mov" in note
        assert "video/quicktime" in note and "1.0 kB" in note and "../" not in note
        assert b"v" * 1001 not in b"".join(
            p.read_bytes() for p in gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir())
    print("ok: an attachment above the cap is replaced by a note that says so")


def test_a_mail_above_the_mail_cap_keeps_its_text_only():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp, MAIL_MAX_MAIL_BYTES=65536)
        assert gw.MAIL_MAX_MAIL_BYTES == 65536
        raw = t.msg("fat@x.org", body="The film is attached.\n", html="<p>The film.</p>",
                    files=[("first.pdf", "application/pdf", b"%PDF" + b"p" * 2000),
                           ("film.mov", "video/quicktime", b"m" * 200_000)])
        assert len(raw) > 65536
        record, _mirror, box = ingest(gw, raw)
        # Only the head of the mail was fetched.
        assert box.fetched == [("INBOX", 1, 65536)]
        assert record["text"] == "The film is attached."
        assert record["body_part"] and record["html_part"] and record["header_part"]
        # No attachment — not even the one that fitted — and a note that says why.
        got = files(gw, record)
        assert [(name, kind) for name, kind, _d, _c in got] == [
            ("attachments.not-mirrored.txt", "text/plain; charset=utf-8")]
        note = got[0][2].decode()
        assert "were not mirrored" in note and "first.pdf" in note and "film.mov" in note
        stored = sum(p.stat().st_size for p in
                     gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir())
        assert stored < 5000
    with tempfile.TemporaryDirectory() as tmp:
        # A mail of exactly the cap is whole.
        gw = t.load(tmp, MAIL_MAX_MAIL_BYTES=65536)
        raw = t.msg("fits@x.org", files=[("a.bin", "application/octet-stream", b"a" * 30_000)])
        assert len(raw) < 65536
        record, _mirror, _box = ingest(gw, raw)
        assert files(gw, record)[0][2] == b"a" * 30_000
    print("ok: a mail above the cap keeps its text parts and says its attachments were left out")


def test_the_trim_is_given_its_evidence():
    """The trim cuts history that carries no ">" only from a mail that cites a
    parent, and a footer only from mail that says itself it is bulk — so the
    gateway has to hand it both facts (scripts/mail_trim.py)."""
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        history = (b"Yes, noon works.\r\n\r\n-----Original Message-----\r\n"
                   b"From: Me <me@example.org>\r\nSent: Monday, November 2, 2026 9:00 AM\r\n"
                   b"To: Ann <a@x.org>\r\nSubject: Lunch\r\n\r\nLunch at noon?\r\n")

        def mail(*headers: bytes):
            raw = (b"From: Ann <a@x.org>\r\nTo: me@example.org\r\nSubject: Re: Lunch\r\n"
                   b"Message-ID: <2@x.org>\r\n" + b"".join(h + b"\r\n" for h in headers)
                   + b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + history)
            return gw.parse_mail(raw, t.NOW)

        # A reply (it cites its parent): the Outlook-style history goes.
        reply = mail(b"In-Reply-To: <1@x.org>", b"References: <1@x.org>")
        assert reply.cited and not reply.bulk
        assert gw.Mirror._reading_text(reply) == ("Yes, noon works.", "deterministic")
        # The same words in a mail that cites nothing are not known to be
        # history: they stay, and the state says a better trim is wanted.
        alone = mail()
        text, state = gw.Mirror._reading_text(alone)
        assert not alone.cited and "Lunch at noon?" in text and state == "wanting"
        # Bulk is what the mail says of itself, by any of the three headers.
        assert mail(b"List-Id: <news.x.org>").bulk
        assert mail(b"List-Unsubscribe: <mailto:leave@x.org>").bulk
        assert mail(b"Precedence: BULK").bulk
        assert not mail(b"Precedence: first-class").bulk
    print("ok: the trim is told whether a mail is a reply and whether it is bulk")


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            print(f"FAIL {test.__name__}: {exc}")
            traceback.print_exc()
    if failures:
        print(f"\n{failures} mail gateway MIME check(s) failed.")
        return 1
    print("\nAll mail gateway MIME checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
