#!/usr/bin/env python3
"""What the mail gateway makes of a mail (scripts/mail-gateway.py).

A mail is text from outside, in every shape a client or an attacker writes:
this pins what becomes of each — plain, HTML-only, multipart/alternative,
mixed with attachments, inline ``cid:`` images, a tracking pixel, a nested
message, odd and unknown charsets, a missing or overlong Message-ID, RFC 2047
names, hostile file names, content types, addresses and ids — and the size
caps. Every part is kept; nothing a mail says becomes more than data.

And what one mail may cost the reader: the bounds on the parser's work, the
deadline on a reading and the reader process that keeps it, the canaries
that say whose fault a failed reading is, and what becomes of a mail that
could not be read — its placeholder, and that being read again.

Uses the fake mailbox of tests/test_mail_gateway.py.

    python3 tests/test_mail_gateway_mime.py
"""
import base64
import importlib.util
import os
import re
import sys
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
        assert mirror._placeholders == set()
    print("ok: lines of a body or an attachment that begin with \"--\" are not counted as "
          "boundary lines")


def test_the_work_of_parsing_one_mail_is_bounded():
    """What the parser builds of a mail is bounded by its parts; what the
    parsing costs was not. The parser tests every line against the boundary
    of every multipart the line lies in — lines times levels — so 850 kB
    (900 levels around 800,000 empty lines) held the poll thread for over a
    minute, and parsed in the end, so nothing flagged it. A mail that asks
    for more such tests than MAX_PARSE_WORK, or nests deeper than
    MAX_NESTING, is not parsed to the end: it is a placeholder."""
    def nested(depth: int, body: bytes) -> bytes:
        return (HEAD + b"Message-ID: <work@x.org>\r\n" + b"".join(
            b"Content-Type: multipart/mixed; boundary=b%d\r\n\r\n--b%d\r\n" % (n, n)
            for n in range(depth)) + b"Content-Type: text/plain\r\n\r\n" + body)

    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)

        def refused(raw: bytes, why: str) -> bool:
            try:
                gw.parse_mail(raw)
            except gw.UnreadableMail as exc:
                assert why in str(exc), exc
                return True
            raise AssertionError("a mail that asks for this much work must not be parsed")

        # A sixth of the mail that was reported, and that mail — however the
        # lines end: refused when the nesting is seen, before a line is tested.
        assert refused(nested(150, b"\n" * 400_000), "more work")
        assert refused(nested(900, b"\n" * 800_000), "more work")
        assert refused(nested(150, b"\r" * 400_000), "more work")
        # The arithmetic, on a small scale: lines times levels, with what
        # reading a line costs at all counted as PARSE_LINE_COST levels more …
        gw.MAX_PARSE_WORK = 100_000
        assert gw.PARSE_LINE_COST == 4
        body = b"x\r\n" * 10_000
        assert gw.parse_mail(nested(5, body)).plain.count("x") == 10_000
        assert refused(nested(6, body), "nested 6 deep")
        # … a line more for every 64 bytes, since a line that begins like a
        # long boundary costs that boundary's length to compare …
        long = (b"y" * 6398 + b"\r\n") * 100
        assert gw.parse_mail(nested(5, long)).plain and refused(nested(6, long), "more work")
        # … and a mail of no parts pays for its lines alone.
        assert gw.parse_mail(HEAD + b"\r\n" + b"x\r\n" * 22_000).plain
        assert refused(HEAD + b"\r\n" + b"x\r\n" * 26_000, "lines: more work")
        gw.MAX_PARSE_WORK = 50_000_000
        # Nesting has a bound of its own, far beyond what a client writes
        # and far short of where the interpreter ends the parser's recursion
        # — which is no way to learn that a mail is not one.
        for kind in (b"message/rfc822", b"multipart/mixed"):
            assert refused(_nested("deep@evil.org", kind), "nested more than 200 deep")
        assert gw.parse_mail(_nested("deep@evil.org", depth=199)).message_id == "deep@evil.org"
        # What clients do write is read as before: a mail forwarded as an
        # attachment ten times over, a megabyte attached at the bottom.
        blob = b64(b"\x00\x01\x02\x03" * 250_000)
        raw = (HEAD + b"Message-ID: <m0@x.org>\r\nContent-Type: multipart/mixed; boundary=z\r\n"
               b"\r\n--z\r\nContent-Type: text/plain\r\n\r\nThe file.\r\n--z\r\n"
               b"Content-Type: application/zip\r\nContent-Transfer-Encoding: base64\r\n"
               b"Content-Disposition: attachment; filename=a.zip\r\n\r\n" + blob + b"--z--\r\n")
        for n in range(1, 11):
            raw = (HEAD + b"Message-ID: <m%d@x.org>\r\n"
                   b"Content-Type: multipart/mixed; boundary=f%d\r\n\r\n--f%d\r\n"
                   b"Content-Type: text/plain\r\n\r\nFwd %d\r\n--f%d\r\n"
                   b"Content-Type: message/rfc822\r\n\r\n" % (n, n, n, n, n)
                   + raw + b"\r\n--f%d--\r\n" % n)
        mail = gw.parse_mail(raw)
        assert mail.message_id == "m10@x.org" and mail.plain == "Fwd 10"
        assert [p.content_type for p in mail.parts] == ["message/rfc822"]
        assert b"<m0@x.org>" in mail.parts[0].data
        assert blob[:4000].replace(b"\r\n", b"\n") in mail.parts[0].data
        # In the mirror, the mail that asks for too much is a placeholder.
        record, mirror, _box = ingest(gw, nested(150, b"\n" * 400_000))
        assert record["text"] == gw.UNREADABLE_TEXT and record["message_id"] == "work@x.org"
        assert record["sender"] == "ann@x.org" and mirror._placeholders == {"work@x.org"}
    print("ok: the work parsing one mail may cost is bounded — lines times levels — and "
          "so is the nesting")


def test_a_nested_mail_is_written_back_field_by_field():
    """A nested mail is one part, written back from what the parser made of
    it. The legacy policy wraps every header field anew on the way, and its
    wrapping takes time with the square of the line breaks a value holds —
    a form feed is one to it, so MAX_PARSE_WORK counts none of them: a
    nested Subject of 2 MB held the poll for 28 seconds, one of 4 MB for 139.
    The fields are written back as they were read instead."""
    import email.header as stdlib
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        inner = (b"From: Billing <billing@shop.example>\r\n"
                 b"Subject: a" + b"\x0c\t" * 20_000 + b"\r\n"
                 b"X-Long: " + b"word " * 400 + b"\r\n"
                 b"X-Folded: one\r\n\ttwo\r\n three\r\n"
                 b"X-Blank: x\r\n" + b"\t\r\n" * 2000 +
                 b"X-Raw: caf\xc3\xa9 \xff\r\n"
                 b"Content-Type: text/plain; charset=utf-8\r\n"
                 b"\r\nPlease pay 10.\r\n")
        raw = (HEAD + b"Message-ID: <fwd@x.org>\r\nSubject: Fwd: Invoice\r\n"
               b"Content-Type: message/rfc822\r\n\r\n" + inner)
        wrapped = []
        real = stdlib.Header.encode

        def encode(self, *args, **kwargs):
            wrapped.append(len(str(self)))
            return real(self, *args, **kwargs)
        stdlib.Header.encode = encode
        try:
            mail = gw.read_mail(raw, t.NOW)
        finally:
            stdlib.Header.encode = real
        assert wrapped == [], "no field of a nested mail is wrapped anew"
        # As read — byte for byte but for the line ends, which are the ones
        # the rest of the nested mail is written with: a long line stays
        # long, a fold stays where it was, raw bytes stay raw.
        assert [p.content_type for p in mail.parts] == ["message/rfc822"]
        assert mail.parts[0].data == inner.replace(b"\r\n", b"\n")
        # With any kind of line end, and inside a multipart.
        for end in (b"\n", b"\r"):
            nested = gw.parse_mail(
                HEAD + b"Content-Type: multipart/mixed; boundary=B\r\n\r\n--B\r\n"
                b"Content-Type: message/rfc822\r\n\r\n"
                + b"Subject: one" + end + b" two" + end + b"X: y" + end + end
                + b"body\r\n--B--\r\n")
            assert nested.parts[0].data == b"Subject: one\n two\nX: y\n\nbody"
    print("ok: a nested mail's header fields are written back as they were read, "
          "not wrapped anew")


def test_an_encoded_word_that_never_ends_is_read_word_by_word():
    """The standard decoder of encoded words reads on to the end of the line
    from every word that begins and never ends, and then begins again at the
    next: 36,000 such beginnings in a Subject of 250 kB held the poll for 52
    seconds, and every part's file name goes through the same call. A value
    with such a word is not given to it: it is read word by word, which the
    gateway's own pattern does in one pass."""
    import email.header as stdlib
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        asked = []
        real = stdlib.decode_header

        def decode_header(value):
            if isinstance(value, str):
                asked.append(len(value))
            return real(value)
        stdlib.decode_header = decode_header
        try:
            opened = "=?a?q?x" * 2000
            assert gw._decode_words(opened) == opened and asked == []
            # The words that do end are still decoded — each by itself.
            mixed = "=?utf-8?q?caf=C3=A9?= =?iso-8859-1?b?4A==?= la =?utf-8?q?cart " + opened
            assert gw._decode_words(mixed) == "caféà la =?utf-8?q?cart " + opened
            assert asked and max(asked) < 30, asked
            # In a mail: the Subject, a display name, a part's file name.
            raw = (HEAD.replace(b"Ann Example", opened.encode()[:700])
                   + b"Message-ID: <w@x.org>\r\nSubject: " + opened.encode() + b"\r\n"
                   b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
                   b"--B\r\nContent-Type: text/plain\r\n\r\nThe text.\r\n"
                   b"--B\r\nContent-Type: application/pdf; name=\"" + opened.encode()[:3500]
                   + b"\"\r\n\r\n%PDF\r\n--B--\r\n")
            del asked[:]
            mail = gw.read_mail(raw, t.NOW)
            assert max(asked, default=0) < 30, asked
            assert mail.subject == opened[:998] and mail.sender == "ann@x.org"
            assert mail.sender_name == opened[:200]
            assert mail.plain == "The text." and mail.parts[0].file_name == opened[:3500]
            # What the standard decoder reads in proportion is still its
            # own: a long subject of real words, and a word laxer than the
            # gateway's own pattern takes one for (a blank inside it).
            long = "=?utf-8?q?caf=C3=A9?= " * 600
            assert gw._decode_words(long).strip() == "café" * 600
            assert gw._decode_words("=?utf-8?q?hello world?= again") == "hello world again"
            assert len(long) in asked
        finally:
            stdlib.decode_header = real
        # Whether every word ends is asked line by line, as the decoder
        # splits lines — at a form feed too — and of whatever it takes for
        # the beginning of a word: any charset, a blank one included.
        assert gw._words_end("") and gw._words_end("no words") and gw._words_end("a ?= b =? c")
        assert gw._words_end("=?utf-8?q?a?= =?utf-8?b?w6A=?=")
        assert gw._words_end("=?utf-8?q?a b?c?=")
        assert not gw._words_end("=?utf-8?q?a")
        assert not gw._words_end("=?utf-8?q?a?= =?utf-8?q?b")
        assert not gw._words_end("?= =?utf-8?q?a")
        assert not gw._words_end("=? ?Q?x" * 3)
        assert not gw._words_end("=?a?q?x\x0c" * 3 + "?=")
        assert not gw._words_end("=?a?q?x\n?=") and gw._words_end("=?a?q?x?=\n=?a?q?y?=")
    print("ok: a value with an encoded word that never ends is read word by word, never "
          "by the standard decoder")


def test_the_parameters_of_all_a_mails_parts_are_bounded():
    """MAX_FIELD_BYTES bounds one Content-Type or Content-Disposition field,
    not the sum over a mail's parts: the parser's parameter splitter counts
    the quotes before every semicolon it meets, and the reader asks it up to
    three times a part. 6,050 parts with two fields of 4,000 semicolons each
    held the poll for 131 seconds on Python 3.12. What those fields cost —
    semicolons times length — is summed as the parser takes them off the
    mail, and a mail that asks for more than MAX_PARAMETER_WORK is a
    placeholder."""
    import email.message as stdlib

    def fields(semicolons: int) -> tuple:
        junk = b"x=\"" + b";" * semicolons
        return b"text/plain; " + junk, b"inline; " + junk

    def parts(n: int, semicolons: int = 4000) -> bytes:
        kind, disposition = fields(semicolons)
        return (HEAD + b"Message-ID: <params@x.org>\r\n"
                b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
                + (b"--B\r\nContent-Type: " + kind + b"\r\n"
                   b"Content-Disposition: " + disposition + b"\r\n\r\nhi\r\n") * n
                + b"--B--\r\n")

    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        assert gw.MAX_PARAMETER_WORK == 1_000_000_000 and gw.PARAMETER_STEP_COST == 256

        def work(semicolons: int) -> int:
            # Semicolons times length — and what a step costs at all.
            return sum(value.count(b";") * (len(value) + 256) for value in fields(semicolons))
        gw.MAX_PARAMETER_WORK = 4 * work(4000) + 1000
        spent = []
        real = stdlib._parseparam
        stdlib._parseparam = lambda s: (spent.append(str(s).count(";") * len(str(s))), real(s))[1]
        try:
            # Up to the bound a mail is read as before …
            mail = gw.parse_mail(parts(4))
            assert mail.plain == "\n\n".join(["hi"] * 4)
            # … and the splitter did no more than three times what was
            # counted (a part's file name, twice, and its charset).
            assert 4 * work(4000) < sum(spent) <= 3 * gw.MAX_PARAMETER_WORK, sum(spent)
            # Beyond it the mail is refused as the parser takes the field,
            # before anything splits it.
            del spent[:]
            try:
                gw.parse_mail(parts(5))
                raise AssertionError("a mail that asks for this much work must not be parsed")
            except gw.UnreadableMail as exc:
                assert "parameters" in str(exc) and exc.kind == gw.OVER_A_BOUND
            assert sum(spent) < work(4000), "refused before a field of it was split"
            # Many cheap fields add up as well: it is the sum that is bounded.
            fit = gw.MAX_PARAMETER_WORK // work(100)
            assert fit > 1000
            assert gw.parse_mail(parts(fit - 1, semicolons=100)).plain.count("hi") == fit - 1
            try:
                gw.parse_mail(parts(fit + 1, semicolons=100))
                raise AssertionError("a mail that asks for this much work must not be parsed")
            except gw.UnreadableMail as exc:
                assert "parameters" in str(exc)
        finally:
            stdlib._parseparam = real
        gw.MAX_PARAMETER_WORK = 1_000_000_000
        # What clients write is nowhere near: two hundred attachments, each
        # under the longest name spelled the longest way (RFC 2231, in
        # pieces), and a mail of ten thousand small parts.
        name = b"".join(b";\r\n filename*%d*=%s" % (n, b"%C3%A9" * 9) for n in range(1, 50))
        raw = (HEAD + b"Message-ID: <many@x.org>\r\nContent-Type: multipart/mixed; boundary=B\r\n"
               b"\r\n--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nThe files.\r\n"
               + b"".join(b"--B\r\nContent-Type: application/pdf\r\n"
                          b"Content-Disposition: attachment; filename*0*=utf-8''f%d" % n
                          + name + b"\r\n\r\n%PDF\r\n" for n in range(200))
               + b"--B--\r\n")
        mail = gw.parse_mail(raw)
        assert len(mail.parts) == 200 and mail.parts[7].file_name == "f7" + "é" * 9 * 49
        small = (HEAD + b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
                 + b"--B\r\nContent-Type: text/plain; charset=utf-8; format=flowed\r\n\r\nhi\r\n"
                 * 10_000 + b"--B--\r\n")
        assert gw.parse_mail(small).plain.count("hi") == 10_000
        # In the mirror, the mail that asks for too much is a placeholder.
        gw.MAX_PARAMETER_WORK = 4 * work(4000) + 1000
        record, mirror, _box = ingest(gw, parts(5))
        assert record["text"] == gw.UNREADABLE_TEXT and record["message_id"] == "params@x.org"
        assert mirror.placeholders() == {"standing": 1, "made": 1, "why": {gw.OVER_A_BOUND: 1}}
    print("ok: what the parameters of all a mail's Content-Type and Content-Disposition "
          "fields cost to parse is bounded")


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
        assert mirror.health_notice() is None

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
        assert mirror._folder("inbox")["hwm"] == 5
    print("ok: a mail the reader chokes on is mirrored as a placeholder, and read again "
          "at the next start; the folder goes on")


def test_a_mail_the_trim_chokes_on_is_a_placeholder_too():
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
                # Memory is the mail's too, while there is enough for the canary.
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
            # The trim is part of reading: nothing was written of the mail
            # before it failed. One record, its one blob.
            media = [p for p in gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir()
                     if "." not in p.name]
            assert len(media) == 1 + 2
            assert mirror.index.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0
            box.put("INBOX", t.msg("huge@x.org", subject="huge", day=2.5))
            mirror.poll()
            assert t.by_id(gw)["huge@x.org"]["text"] == gw.UNREADABLE_TEXT
            assert mirror._folder("inbox")["hwm"] == 3
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
        assert t.records(gw) == [] and mirror._placeholders == set()
        mirror.poll()
        assert t.by_id(gw)["a@x.org"]["text"] == "The numbers are in."
    print("ok: a mail the trim fails on is a placeholder; a full disk is not the mail's fault")


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


def _monitor():
    spec = importlib.util.spec_from_file_location("gateway_monitor_for_mail_faults",
                                                  t.SCRIPTS_DIR / "gateway-monitor.py")
    monitor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(monitor)
    return monitor


def _nested(mid: str, kind: bytes = b"message/rfc822", depth: int = 2000) -> bytes:
    """Sixty kilobytes no parser gets to the bottom of: ``depth`` mails, or
    multiparts, one inside the other."""
    levels = b"".join(
        b"Content-Type: message/rfc822\r\n\r\n" if kind == b"message/rfc822"
        else b"Content-Type: multipart/mixed; boundary=b%d\r\n\r\n--b%d\r\n" % (n, n)
        for n in range(depth))
    return (f"Message-ID: <{mid}>\r\nFrom: Mallory <m@evil.org>\r\nTo: me@example.org\r\n"
            "Subject: hello\r\nDate: Mon, 17 Nov 2025 10:00:00 +0000\r\n"
            "MIME-Version: 1.0\r\n").encode() + levels + (
                b"Content-Type: text/plain\r\n\r\nhi\r\n")


def test_the_canary_tells_a_mails_fault_from_the_readers():
    """Reading fails on a mail's account exactly when the reader still reads
    a mail known to be good: the built-in canary, put through the same
    reading — the parse, the walk of its parts, the trim."""
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        # The canary is a mail the reader reads, with a step for each of its
        # features: encoded words, a thread, two bodies, a quote, a file.
        canary = gw.read_mail(gw.CANARY_MAIL)
        assert canary.message_id == "answer@canary.mail.invalid"
        assert canary.subject == "Re: Still singing?" and canary.sender_name == "The Canary"
        assert canary.cited == ["question@canary.mail.invalid"] and canary.html
        assert [(p.file_name, p.data) for p in canary.parts] == [("canary.bin", b"canary")]
        assert canary.reading == ("Still singing — the reader works.", "deterministic")
        gw._read_canary()
        good = t.msg("good@x.org", body="Fine.\n")
        assert gw.read_mail(good, t.NOW).reading == ("Fine.", "deterministic")
        real_parse, real_trim = gw.parse_mail, gw._trim.trim
        try:
            # Every way a reader fails on one mail, the canary still read:
            # the mail's fault, whatever was raised — and said with its cause.
            for failure in (RecursionError("maximum recursion depth exceeded"), MemoryError(),
                            ValueError("a parser bug"), LookupError("unknown encoding: x"),
                            OSError("a codec that reads a file"), KeyError("k")):
                def parse(raw, internal_date=None, failure=failure):
                    if b"poison" in raw:
                        raise failure
                    return real_parse(raw, internal_date)
                gw.parse_mail = parse
                try:
                    gw.read_mail(t.msg("p@x.org", subject="poison"), t.NOW)
                    raise AssertionError("a mail the parser fails on is unreadable")
                except gw.UnreadableMail as exc:
                    assert type(failure).__name__ in str(exc) and exc.__cause__ is failure
                assert gw.read_mail(good, t.NOW).plain == "Fine.\n"
            gw.parse_mail = real_parse

            def trim(plain, html, subject=None, **evidence):
                if subject == "poison":
                    raise RecursionError("the trim, on this mail")
                return real_trim(plain, html, subject=subject, **evidence)
            gw._trim.trim = trim
            try:
                gw.read_mail(t.msg("p@x.org", subject="poison"), t.NOW)
                raise AssertionError("a mail the trim fails on is unreadable")
            except gw.UnreadableMail as exc:
                assert "the trim, on this mail" in str(exc)
            # The canary fails too: the reader is at fault, and no mail is.
            gw._trim.trim = lambda *a, **kw: (_ for _ in ()).throw(MemoryError())
            try:
                gw.read_mail(good, t.NOW)
                raise AssertionError("a reader that reads nothing is the mirror's fault")
            except gw.MirrorFault as exc:
                assert "own test mail" in str(exc) and "MemoryError" in str(exc)
            gw._trim.trim = real_trim
            gw.parse_mail = lambda raw, internal_date=None: 1 / 0
            try:
                gw.read_mail(good, t.NOW)
                raise AssertionError("a reader that reads nothing is the mirror's fault")
            except gw.MirrorFault as exc:
                assert "ZeroDivisionError" in str(exc)
            # So does a reader that raises on one mail and makes nothing of
            # the canary: reading it without an error is not reading it.
            gw.parse_mail = lambda raw, internal_date=None: (
                1 / 0 if b"poison" in raw else gw.Mail(message_id="x@x.org"))
            try:
                gw.read_mail(t.msg("p@x.org", subject="poison"), t.NOW)
                raise AssertionError("a reader that misreads the canary is at fault")
            except gw.MirrorFault as exc:
                assert "misread" in str(exc)
            # A bound the reader set itself needs no canary.
            gw.parse_mail = lambda raw, internal_date=None: (_ for _ in ()).throw(
                gw.UnreadableMail("more than a mail may ask for"))
            try:
                gw.read_mail(good, t.NOW)
                raise AssertionError("refused on purpose")
            except gw.UnreadableMail as exc:
                assert str(exc) == "more than a mail may ask for"
        finally:
            gw.parse_mail, gw._trim.trim = real_parse, real_trim
    print("ok: a reading that fails is the mail's fault when the canary still reads, "
          "and the mirror's when it does not")


def test_a_fault_of_the_mirror_is_not_laid_at_the_mails_door():
    """Failing closed per mail must not absorb a fault of the mirror itself.
    One that sets aside whatever it fails on turns a corrupt index, a bug
    every mail brings out, or memory gone for all of them into a mailbox of
    placeholders with every mark moved on — and /health still "connected".
    The pass ends instead, at the first mail: nothing is set aside, marked
    or skipped, and /health names the mirror, not the mailbox."""
    import sqlite3
    monitor = _monitor()
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
            assert snap["blocked"] == "mirror" and t.records(gw) == []
            assert mirror._folder("inbox")["hwm"] == 0, "no mark moves past unmirrored mail"
            assert monitor.classify_health(200, snap)[0] == "faulty"
        # The fault goes away, and no restart is needed: the next poll has it all.
        mirror.threads.resolve = real
        snap = _looped(gw, mirror, loop)
        assert snap["connected"] is True and snap["error"] is None and snap["blocked"] is None
        assert _whole(gw) == [f"n{n}@x.org" for n in range(5)]
        assert not any("backfill" in e for e in t.inbound(events))
        assert gw._environment(sqlite3.DatabaseError("file is not a database"))
        assert gw._environment(sqlite3.OperationalError("database is locked"))
        assert not gw._environment(sqlite3.IntegrityError("NOT NULL constraint failed"))
        assert not gw._environment(MemoryError()) and not gw._environment(TypeError("x"))
    for fault in (RuntimeError("a bug every mail brings out"), MemoryError()):
        with tempfile.TemporaryDirectory() as tmp:
            # By what the reader does with the canary: a fault no exception
            # names. It fails on the mirror's own test mail as on every
            # other — none is set aside, none skipped, and one mail a poll is
            # fetched to learn it again.
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
                for n in range(2):
                    snap = _looped(gw, mirror, loop)
                    assert snap["connected"] is False and snap["blocked"] == "mirror", snap
                    assert "the mirror's own test mail" in snap["error"]
                    assert "'INBOX' UID 1 is not set aside" in snap["error"]
                    assert t.records(gw) == [] and mirror._placeholders == set()
                    assert mirror._folder("inbox")["hwm"] == 0
                    assert mirror.index.one("SELECT COUNT(*) AS n FROM locations")["n"] == 0
                    assert len(box.fetched) == n + 1
            finally:
                gw._trim.trim = real
            snap = _looped(gw, mirror, loop)
            assert snap["connected"] is True and snap["error"] is None
            assert _whole(gw) == ["n0@x.org", "n1@x.org", "n2@x.org"]
            assert mirror._folder("inbox")["hwm"] == 3 and len(t.inbound(events)) == 3
    with tempfile.TemporaryDirectory() as tmp:
        # The fault begins while the mirror runs. That the mail before was
        # mirrored whole speaks for no mail after it: not one of them is a
        # placeholder, the mark stays where it was — and when the fault has
        # passed they are all on record, whole.
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
            assert snap["connected"] is False and snap["blocked"] == "mirror", snap
            assert sorted(t.by_id(gw)) == ["a@x.org"] and mirror._placeholders == set()
            assert mirror._folder("inbox")["hwm"] == 1
        finally:
            gw._trim.trim = real
        snap = _looped(gw, mirror, loop)
        assert snap["connected"] is True and snap["error"] is None
        assert _whole(gw) == ["a@x.org", "b@x.org", "c@x.org", "d@x.org"]
        assert len(t.records(gw)) == 4 and mirror._folder("inbox")["hwm"] == 4
        assert [e["message_id"] for e in t.inbound(events)] == [
            "a@x.org", "b@x.org", "c@x.org", "d@x.org"]
    print("ok: a corrupt index, a bug or memory gone for every mail is the mirror's fault: "
          "nothing is set aside or skipped on it, and /health says so")


def test_a_write_that_fails_is_never_the_mails_fault():
    """Writing — the ledger, the indexes, the blobs — fails on the mirror's
    account, whatever mail was in hand: next to nothing of a mail decides
    whether its record can be written. So no mail is set aside or marked
    over it, not even the one mail a bug of the writer shows on: the pass
    ends there, /health says the mirror is at fault and at which mail, and
    that mail is mirrored whole once the writer works."""
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        t._write_policy(gw)
        events = t.rail(gw)
        mirror, box = t.started(gw)
        loop: dict = {}
        box.put("INBOX", t.msg("a@x.org", day=39.9))
        box.put("INBOX", t.msg("worse@x.org", subject="worse", day=39.9))
        box.put("INBOX", t.msg("after@x.org", day=39.9))
        real_write = gw._ibstore.write_message

        def write(store, **kw):
            if kw.get("message_id") == "worse@x.org":
                raise TypeError("a writer bug")
            return real_write(store, **kw)
        gw._ibstore.write_message = write
        try:
            for _ in range(2):
                snap = _looped(gw, mirror, loop)
                assert snap["connected"] is False and snap["blocked"] == "mirror", snap
                assert "'INBOX' UID 2 could not be written" in snap["error"]
                assert "a writer bug" in snap["error"] and "not set aside" in snap["error"]
                # Not a placeholder, not a mark, and the folder's own mark
                # below it: the mail is simply still to be mirrored.
                assert sorted(t.by_id(gw)) == ["a@x.org"] and mirror._placeholders == set()
                assert mirror._folder("inbox")["hwm"] == 1
                assert [row["uid"] for row in mirror.index.rows(
                    "SELECT uid FROM locations")] == [1]
                # And nothing of the attempt is left behind.
                media = [p for p in gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir()
                         if "." not in p.name]
                assert len(media) == 2
                assert mirror.index.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0
            try:
                mirror.poll()
                raise AssertionError("a write that fails must fail the pass")
            except gw.MirrorFault as exc:
                assert isinstance(exc.__cause__, TypeError)
        finally:
            gw._ibstore.write_message = real_write
        snap = _looped(gw, mirror, loop)
        assert snap["connected"] is True and snap["blocked"] is None
        assert _whole(gw) == ["a@x.org", "after@x.org", "worse@x.org"]
        assert [e["message_id"] for e in t.inbound(events)] == [
            "a@x.org", "worse@x.org", "after@x.org"]
    with tempfile.TemporaryDirectory() as tmp:
        # A writer that writes nothing at all, placeholders included: the
        # same, for a mail that can be read and for one that cannot.
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
            box.put("INBOX", _nested("p@evil.org"), date=t.NOW - 5)
            box.put("INBOX", t.msg("c@x.org", day=39.9))
            snap = _looped(gw, mirror, loop)
            assert snap["connected"] is False and snap["blocked"] == "mirror", snap
            assert "'INBOX' UID 2 could not be written" in snap["error"]
            assert sorted(t.by_id(gw)) == ["a@x.org"]
            assert mirror._folder("inbox")["hwm"] == 1
        finally:
            gw._ibstore.write_message = real
        assert _looped(gw, mirror, loop)["connected"] is True
        assert _whole(gw) == ["a@x.org", "c@x.org"] and mirror._placeholders == {"p@evil.org"}
    with tempfile.TemporaryDirectory() as tmp:
        # A placeholder whose mail reads now, and whose record then cannot be
        # written: the placeholder was dropped for it, so for a moment the
        # mail has no record — and must not keep a place that says it has.
        # It is read again, and is on record once the writer works.
        gw = t.load(tmp)
        t._write_policy(gw)
        events = t.rail(gw)
        clock = _Clock()
        box = t.FakeMailbox(gw)
        mirror = gw.Mirror(box, clock=clock)
        mirror.poll()
        box.put("INBOX", t.msg("a@x.org", day=39.8))
        box.put("INBOX", t.msg("flaky@x.org", subject="flaky", body="Read at last.\n", day=39.9))
        real_trim, real_write = gw._trim.trim, gw._ibstore.write_message
        gw._trim.trim = lambda plain, html, subject=None, **kw: (
            1 / 0 if subject == "flaky" else real_trim(plain, html, subject=subject, **kw))
        try:
            mirror.poll()
            assert mirror._placeholders == {"flaky@x.org"}
            gw._trim.trim = real_trim
            gw._ibstore.write_message = lambda *a, **kw: (_ for _ in ()).throw(
                TypeError("a writer bug"))
            clock.now += 3600
            for _ in range(2):
                try:
                    mirror.poll()
                    raise AssertionError("a write that fails must fail the pass")
                except gw.MirrorFault:
                    pass
                assert sorted(t.by_id(gw)) == ["a@x.org"]
            assert mirror.index.rows("SELECT uid FROM locations WHERE message_id = ?",
                                     ("flaky@x.org",)) == []
        finally:
            gw._trim.trim, gw._ibstore.write_message = real_trim, real_write
        while mirror.poll():
            pass
        assert _whole(gw) == ["a@x.org", "flaky@x.org"] and len(t.records(gw)) == 2
        assert t.by_id(gw)["flaky@x.org"]["text"] == "Read at last."
        assert mirror._placeholders == set() and mirror.backfill_status()["done"] is True
        assert t.inbound(events)[-1]["message_id"] == "flaky@x.org"
        # The same after a stop in that moment: the next start finds a place
        # with no record behind it, and has the mail read again.
        uid = box.put("INBOX", t.msg("late@x.org", day=39.95))
        mirror.index.run("UPDATE folders SET hwm = ? WHERE role = 'inbox'", (uid,))
        mirror._locate("inbox", uid, "late@x.org")
        mirror = gw.Mirror(box, clock=clock)
        while mirror.poll():
            pass
        assert _whole(gw) == ["a@x.org", "flaky@x.org", "late@x.org"]
    with tempfile.TemporaryDirectory() as tmp:
        # An index an older build left behind: it marked the mail it could
        # not write even a placeholder for, and moved on. No mail is set
        # aside over a failed write any more, and that one is read again.
        gw = t.load(tmp)
        t.rail(gw)
        mirror, box = t.started(gw)
        uid = box.put("INBOX", t.msg("marked@x.org", day=39.9))
        mirror.index.run("UPDATE folders SET hwm = ? WHERE role = 'inbox'", (uid,))
        mirror.index.set("top:inbox", uid)
        mirror._locate("inbox", uid, None, "unreadable")
        mirror.poll()
        assert t.records(gw) == []
        mirror = gw.Mirror(box, clock=lambda: t.NOW)
        while mirror.poll():
            pass
        assert _whole(gw) == ["marked@x.org"]
        assert "unmirrored" not in gw._health_snapshot()
    print("ok: a write that fails ends the pass, for one mail or for all: no mail is set "
          "aside or marked over it, and none keeps a place without a record")


def test_every_path_through_the_reader_has_a_canary():
    """A failed reading is the mail's fault when the canaries still read —
    so a fault of the mirror's own on a path no canary takes is laid at the
    door of every mail that takes it, with /health still connected. One
    canary, whose plain part reads, never reached the HTML conversion: with
    that broken, every HTML-only mail (of ninety days, in a first backfill)
    became a placeholder. There is a canary for each path now: a plain reply
    beside its HTML, HTML alone, an inline image and a nested mail."""
    monitor = _monitor()
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        page = gw.read_mail(gw.CANARY_HTML_MAIL)
        assert page.subject == "Grüße from the canary" and page.plain is None
        assert page.reading == ("Only HTML here — the converter works.\n\n- one\n- two",
                                "wanting")
        forward = gw.read_mail(gw.CANARY_FORWARD_MAIL)
        assert [(p.content_type, p.file_name, p.content_id, p.inline_image)
                for p in forward.parts] == [
                    ("image/png", None, "sign@canary.mail.invalid", True),
                    ("message/rfc822", "früher.eml", None, False)]
        assert forward.parts[0].data == png(8, 8)
        assert forward.parts[1].data == (
            b"From: mirror@canary.mail.invalid\nSubject: Still\n\tsinging?\n"
            b"Message-ID: <question@canary.mail.invalid>\n\nStill singing?")
        assert "The mail below" in forward.reading[0] and forward.html

        # Each step of the reader is one the canaries take: broken, they do
        # not read — whether it raises or quietly makes something else.
        def missed(owner, name, broken) -> bool:
            real = getattr(owner, name)
            setattr(owner, name, broken)
            try:
                gw._read_canary()
            except Exception:  # noqa: BLE001
                return False
            finally:
                setattr(owner, name, real)
            return True

        def boom(*_args, **_kwargs):
            raise AssertionError("changed under us")
        for owner, name, broken in (
                (gw, "parse_mail", boom), (gw._trim, "trim", boom),
                (gw._trim, "_convert", boom),                       # the HTML conversion
                (gw._trim, "_convert", lambda *a, **kw: ""),
                (gw._BoundedPolicy, "fold", boom),                  # a nested mail written back
                (gw, "_decode_words", lambda value: value),         # encoded words
                (gw, "_parameter", lambda *a, **kw: ""),            # a file name, RFC 2231
                (gw, "_charset", lambda name: None),                # a charset that is not UTF-8
                (gw, "_part_bytes", lambda part: b""),              # a transfer encoding
                (gw, "_file_name", lambda part: None),
                (gw, "_subparts", lambda part: None)):              # the MIME tree
            assert not missed(owner, name, broken), f"no canary takes {name}"
        gw._read_canary()
    with tempfile.TemporaryDirectory() as tmp:
        # The conversion breaks for every mail (a bug, a changed html.parser):
        # the mirror's fault, at the first HTML-only mail. Not one becomes a
        # placeholder, no mark moves, and /health says what stopped.
        gw = t.load(tmp)
        t._write_policy(gw)
        t.rail(gw)
        mirror, box = t.started(gw)
        loop: dict = {}
        for n in range(40):
            box.put("INBOX", HEAD + b"Message-ID: <h%d@x.org>\r\nSubject: Order %d\r\n"
                    b"Content-Type: text/html; charset=utf-8\r\n\r\n"
                    b"<p>Your order %d has shipped.</p>\r\n" % (n, n, n), date=t.NOW - 5)
        box.put("INBOX", t.msg("plain@x.org", day=39.9))
        real = gw._trim._convert
        gw._trim._convert = boom
        try:
            for _ in range(2):
                snap = _looped(gw, mirror, loop)
                assert snap["connected"] is False and snap["blocked"] == "mirror", snap
                assert "the mirror's own test mail" in snap["error"]
                assert monitor.classify_health(200, snap)[0] == "faulty"
                assert t.records(gw) == [] and mirror._placeholders == set()
                assert mirror._folder("inbox")["hwm"] == 0
                assert snap["placeholders"] == {"standing": 0, "made": 0, "why": {}}
        finally:
            gw._trim._convert = real
        snap = _looped(gw, mirror, loop)
        assert snap["connected"] is True and snap["error"] is None
        assert len(_whole(gw)) == 41 and mirror._folder("inbox")["hwm"] == 41
        assert t.by_id(gw)["h7@x.org"]["text"] == "Your order 7 has shipped."
    print("ok: every path a mail takes through the reader is taken by a canary, so a "
          "broken HTML conversion is the mirror's fault and no mail's")


def test_health_counts_the_placeholders_and_says_why():
    """A mail that cannot be read is a placeholder, and the mirror as
    healthy as it was — so nothing on /health said that mail was being
    turned into placeholders at all, however many and whatever the cause;
    the log was the only trace. /health says how many stand, how many were
    made since this start, and why."""
    import json
    import time
    none = {"standing": 0, "made": 0, "why": {}}
    with tempfile.TemporaryDirectory() as tmp:
        assert t.load(tmp, configured=False)._health_snapshot()["placeholders"] == none
        gw = t.load(tmp)
        t._write_policy(gw, news={"news@shop.example"})
        t.rail(gw)
        assert gw._health_snapshot()["placeholders"] == none
        clock = _Clock()
        box = t.FakeMailbox(gw)
        mirror = gw.Mirror(box, clock=clock)
        mirror.poll()
        loop: dict = {}
        assert _looped(gw, mirror, loop)["placeholders"] == none
        real_trim, real_bytes = gw._trim.trim, gw._part_bytes
        broken = {"poison"}

        def trim(plain, html, subject=None, **evidence):
            if subject in broken:
                raise RecursionError("this mail, for now")
            return real_trim(plain, html, subject=subject, **evidence)

        def part_bytes(part):
            data = real_bytes(part)
            if data == b"crawl":
                time.sleep(0.05)
            return data
        gw._trim.trim, gw._part_bytes, gw.READ_SECONDS = trim, part_bytes, 0.3
        try:
            box.put("INBOX", _nested("p1@evil.org"), date=t.NOW - 5)
            box.put("INBOX", _nested("p2@evil.org", b"multipart/mixed"), date=t.NOW - 5)
            box.put("INBOX", t.msg("p3@x.org", subject="poison", day=39.9))
            box.put("INBOX", t.msg("p4@x.org", day=39.9, files=[
                (f"{n}.bin", "application/octet-stream", b"crawl") for n in range(60)]))
            box.put("INBOX", t.msg("good@x.org", day=39.9))
            # (Unreadable news is still news: no chat, so no placeholder.)
            box.put("INBOX", t.msg("n@shop.example", sender="news@shop.example",
                                   subject="poison", day=39.9))
            snap = _looped(gw, mirror, loop)
            assert snap["connected"] is True and snap["error"] is None, snap
            assert snap["placeholders"] == {"standing": 4, "made": 4, "why": {
                gw.OVER_A_BOUND: 2, "RecursionError": 1, gw.TOO_SLOW: 1}}, snap
            assert _whole(gw) == ["good@x.org"] and len(t.records(gw)) == 5
            json.dumps(snap)
            # Read again in vain, they are the placeholders already counted;
            # one that reads now stands no longer, and was still made.
            clock.now += 3600
            assert _looped(gw, mirror, loop)["placeholders"]["made"] == 4
            broken.clear()
            mirror._retried.clear()
            clock.now += 3600
            snap = _looped(gw, mirror, loop)
            assert _whole(gw) == ["good@x.org", "p3@x.org"]
            assert snap["placeholders"] == {"standing": 3, "made": 4, "why": {
                gw.OVER_A_BOUND: 2, "RecursionError": 1, gw.TOO_SLOW: 1}}, snap
            # A new start has made none yet; the ones on record stand.
            mirror = gw.Mirror(box, clock=clock)
            gw.MIRROR = mirror
            assert gw._health_snapshot()["placeholders"] == {"standing": 3, "made": 0, "why": {}}
        finally:
            gw._trim.trim, gw._part_bytes = real_trim, real_bytes
    print("ok: /health says how many placeholders stand, how many were made since the "
          "start, and why")


def test_a_mail_that_takes_too_long_to_read_is_a_placeholder():
    """Each bound closes one way a mail made the standard library's mail
    code spend the square of something its sender chose, and each was found
    when a mail had held the mirror's one thread for a minute. The next such
    way is not known yet, so reading as a whole has a deadline: a mail that
    is not read in its time is a placeholder — as long as the canaries are
    still read in theirs. When they are not, the machine is too slow for any
    mail, which is the mirror's fault: no wall of placeholders."""
    import time
    monitor = _monitor()
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        # A mail gets a base, and a share for every megabyte of it; the
        # canaries, a tenth of the base for their few milliseconds.
        assert gw._read_seconds(0) == gw.READ_SECONDS == 30.0
        assert gw._read_seconds(50_000_000) == 30.0 + 50 * gw.READ_SECONDS_PER_MB == 80.0
        assert gw.CANARY_SECONDS == 3.0
        began = time.monotonic()
        gw._read_canary()
        assert time.monotonic() - began < gw.CANARY_SECONDS / 10
        crawling = [b"crawl"]
        real = gw._part_bytes

        def part_bytes(part):
            data = real(part)
            if any(data.startswith(mark) for mark in crawling):
                time.sleep(0.05)
            return data
        gw._part_bytes = part_bytes
        crawl = t.msg("crawl@x.org", body="See attached.\n", day=39.9, files=[
            (f"{n}.bin", "application/octet-stream", b"crawl") for n in range(100)])
        good = t.msg("good@x.org", body="Fine.\n", day=39.9, files=[
            ("a.bin", "application/octet-stream", b"fine")])
        gw.READ_SECONDS = 0.3
        try:
            began = time.monotonic()
            try:
                gw.read_mail(crawl, t.NOW)
                raise AssertionError("a hundred parts at a crawl are read in 0.3 s?")
            except gw.UnreadableMail as exc:
                assert exc.kind == gw.TOO_SLOW and str(exc).startswith(gw.TOO_SLOW), exc
                assert "0 s for a mail of " in str(exc)
                assert isinstance(exc.__cause__, gw.TookTooLong)
            assert time.monotonic() - began < 2.5, "five seconds of parts, cut short"
            # Time that is up is up for the whole mail — it is not read
            # "whole but for the parts the clock ran out on": the reader
            # catches an Exception part by part, and this is none.
            assert not issubclass(gw.TookTooLong, Exception)
            assert gw.read_mail(good, t.NOW).reading == ("Fine.", "deterministic")
            gw._tick()      # outside a reading, nothing is on the clock
            # The placeholder is read off the header block the reading may
            # have run out of time over, so it is read against the clock
            # too — and is, at worst, a placeholder nothing was parsed for.
            assert gw.read_stub(crawl, t.NOW, "why") == gw.stub_mail(crawl, t.NOW, "why")
            head = crawl.split(b"\n\n")[0]
            assert gw.read_head(head)["id"] == "crawl@x.org"
            real_identify, seconds = gw._identify, gw.CANARY_SECONDS
            gw._identify = lambda msg, head=b"": (time.sleep(0.2), real_identify(msg, head))[1]
            gw.CANARY_SECONDS = 0.05
            try:
                bare = gw.read_stub(crawl, t.NOW, "why")
            finally:
                gw._identify, gw.CANARY_SECONDS = real_identify, seconds
            assert bare.message_id.endswith("@no-message-id.mail.invalid") and bare.sender == ""
            assert bare.unreadable == "why" and bare.date == bare.arrived == t.NOW
            assert bare.headers == gw.stub_mail(crawl, None, "").headers
            # A machine too slow for any mail: the canaries crawl as well.
            crawling.append(b"")
            gw.CANARY_SECONDS = 0.05
            try:
                gw.read_mail(crawl, t.NOW)
                raise AssertionError("a reader too slow for its own test mail")
            except gw.MirrorFault as exc:
                assert "too slow" in str(exc) and "own test mail" in str(exc)

            gw = t.load(tmp)
            gw._part_bytes, gw.READ_SECONDS, gw.CANARY_SECONDS = part_bytes, 0.3, 0.05
            t._write_policy(gw)
            events = t.rail(gw)
            clock = _Clock()
            box = t.FakeMailbox(gw)
            mirror = gw.Mirror(box, clock=clock)
            mirror.poll()
            loop: dict = {}
            box.put("INBOX", t.msg("a@x.org", day=39.9))
            box.put("INBOX", crawl)
            box.put("INBOX", good)
            for _ in range(2):
                snap = _looped(gw, mirror, loop)
                assert snap["connected"] is False and snap["blocked"] == "mirror", snap
                assert "the mail reader is too slow" in snap["error"]
                assert "'INBOX' UID 2 is not set aside" in snap["error"]
                assert monitor.classify_health(200, snap)[0] == "faulty"
                assert _whole(gw) == ["a@x.org"] and mirror._placeholders == set()
                assert mirror._folder("inbox")["hwm"] == 1
                assert snap["placeholders"]["made"] == 0
            # The machine is itself again, and one mail still crawls: that
            # one is a placeholder at once, and the folder goes on.
            del crawling[1:]
            gw.CANARY_SECONDS = 3.0
            snap = _looped(gw, mirror, loop)
            assert snap["connected"] is True and snap["error"] is None, snap
            assert _whole(gw) == ["a@x.org", "good@x.org"]
            assert mirror._placeholders == {"crawl@x.org"}
            assert snap["placeholders"] == {"standing": 1, "made": 1, "why": {gw.TOO_SLOW: 1}}
            assert mirror._folder("inbox")["hwm"] == 3
            # Like every placeholder it is read again at the hour, and takes
            # the placeholder's place once it reads in time.
            del crawling[:]
            clock.now += 3600
            mirror.poll()
            assert _whole(gw) == ["a@x.org", "crawl@x.org", "good@x.org"]
            assert len(t.by_id(gw)["crawl@x.org"]["attachments"]) == 100
            assert [e.get("backfill") for e in t.inbound(events)] == [None, None, None, True]
            # A mail whose header block is what takes too long: its
            # placeholder is not made by reading that block over again with
            # no deadline. Nothing of it is parsed, and the folder goes on.
            stuck = [True]
            real_identify = gw._identify
            gw._identify = lambda msg, head=b"": (
                time.sleep(0.5) if stuck and b"stuck@" in head else None,
                real_identify(msg, head))[1]
            gw.CANARY_SECONDS = 0.2
            box.put("INBOX", t.msg("stuck@x.org", body="Worth the wait.\n", day=39.9))
            box.put("INBOX", t.msg("after@x.org", day=39.9))
            began = time.monotonic()
            snap = _looped(gw, mirror, loop)
            assert time.monotonic() - began < 2.5
            assert snap["connected"] is True and "after@x.org" in _whole(gw)
            assert snap["placeholders"]["why"] == {gw.TOO_SLOW: 2}
            (bare,) = [r for r in t.records(gw) if r["text"] == gw.UNREADABLE_TEXT]
            assert bare["message_id"].endswith("@no-message-id.mail.invalid")
            assert b"Message-ID: <stuck@x.org>" in t.blob(gw, bare["header_part"])
            # When it reads, the mail is on record under its own id, and the
            # placeholder goes the way of a mail that left.
            del stuck[:]
            clock.now += 3600
            mirror.poll()
            mirror.poll()
            assert "stuck@x.org" in _whole(gw) and mirror._placeholders == set()
            assert t.by_id(gw)[bare["message_id"]]["removed_at"]
        finally:
            gw._part_bytes = real
    print("ok: a mail not read in its time is a placeholder; a reader too slow for its "
          "own test mail is the mirror's fault")


# A reader process for the tests: the gateway's own, with a mail's ways to
# go wrong in one call that never looks at the clock.
_READER_CHILD = '''\
import importlib.util, os, sys, time
gateway, flag = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.dirname(gateway))
spec = importlib.util.spec_from_file_location("mail_gateway_reader_under_test", gateway)
gw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gw)
with open(flag + ".env", "w") as out:
    out.write("\\n".join(sorted(os.environ)))
real_words, real_canary, real_identify = gw._decode_words, gw._read_canary, gw._identify


def decode_words(value):
    if "hang" in value and os.path.exists(flag):
        time.sleep(600)
    if "crawl" in value:
        time.sleep(0.05)
    if "die" in value:
        os._exit(3)
    if "boom" in value:
        raise RuntimeError("a parser bug")
    return real_words(value)


def read_canary():
    if os.path.exists(flag + ".canary"):
        time.sleep(600)
    real_canary()


def identify(msg, head=b""):
    if b"stuck" in head and os.path.exists(flag):
        time.sleep(600)
    return real_identify(msg, head)


gw._decode_words, gw._read_canary, gw._identify = decode_words, read_canary, identify
gw._serve_reader(int(sys.argv[3]))
'''


def _reader(gw, tmp):
    """``(a Reader on the test's reader process, its flag file)``."""
    script, flag = Path(tmp) / "reader_child.py", Path(tmp) / "hang"
    script.write_text(_READER_CHILD, encoding="utf-8")
    flag.write_text("")
    return gw.Reader([sys.executable, str(script), str(t.SCRIPTS_DIR / "mail-gateway.py"),
                      str(flag)]), flag


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_the_reader_process_ends_what_no_look_at_the_clock_can():
    """The reader's own looks at the clock keep the deadline wherever its
    code is entered — and cannot end one call into the standard library
    that does not come back, which is what a Subject of encoded words (52 s)
    and a nested header (139 s) each were. The running gateway reads its
    mail in a process of its own for that: a process can be ended."""
    import time
    monitor = _monitor()
    with tempfile.TemporaryDirectory() as tmp:
        # The gateway's own reader process reads a mail as the gateway
        # would: every field of it, part for part. (It answers on a pipe of
        # its own — what the script prints when it is loaded, here that a
        # setting is no number, is for the log and no part of an answer.)
        gw = t.load(tmp, MAIL_BACKFILL_BATCH="many")
        gw.READER = gw.Reader()
        try:
            for raw in (t.msg("a@x.org", html="<p>The numbers are in.</p>", refs=["z@x.org"],
                              cc="Bob <bob@y.org>", list_id="Dev <dev.lists.x.org>",
                              files=[("x.pdf", "application/pdf", b"%PDF" * 5000),
                                     ("logo.png", "image/png", png(8, 8))]),
                        gw.CANARY_MAIL, gw.CANARY_HTML_MAIL, gw.CANARY_FORWARD_MAIL,
                        b"\xff\xfe no mail at all"):
                there = gw.read_mail(raw, t.NOW)
                assert there == gw._reading(raw, t.NOW) and there.reading[0]
                assert gw.read_mail(raw).arrived is None
            pid = gw.READER._process.pid
            # A bound is the reader's own word, there as here.
            try:
                gw.read_mail(_nested("deep@evil.org"), t.NOW)
                raise AssertionError("refused by a bound")
            except gw.UnreadableMail as exc:
                assert "nested more than 200 deep" in str(exc) and exc.kind == gw.OVER_A_BOUND
            assert gw.READER._process.pid == pid
            gw.READER.canary(gw.CANARY_SECONDS)
        finally:
            gw.READER.close()
        assert _gone(pid)

        gw.READER, flag = _reader(gw, tmp)
        gw.READ_SECONDS, gw.READER_GRACE_SECONDS, gw.CANARY_SECONDS = 0.5, 0.5, 5.0
        try:
            good = t.msg("good@x.org", body="Fine.\n")
            assert gw.read_mail(good, t.NOW).reading == ("Fine.", "deterministic")
            pid = gw.READER._process.pid
            # It reads text from outside, and is given none of the secrets.
            names = set(Path(str(flag) + ".env").read_text().split())
            assert "MAIL_DATA_DIR" in names and "EMAIL_USER" in names
            assert not {"EMAIL_PASS", "MAIL_GATEWAY_TOKEN"} & names
            assert "EMAIL_PASS" in os.environ and "MAIL_GATEWAY_TOKEN" in os.environ

            def unreadable(subject: str) -> tuple:
                began = time.monotonic()
                try:
                    gw.read_mail(t.msg("m@x.org", subject=subject), t.NOW)
                except gw.UnreadableMail as exc:
                    return exc.kind, str(exc), time.monotonic() - began
                raise AssertionError(f"{subject!r} was read")
            # What the reader raises there is the mail's, by name …
            kind, why, _took = unreadable("boom")
            assert (kind, why) == ("RuntimeError", "RuntimeError: a parser bug")
            # … and its own look at the clock ends a reading that crawls
            # through its code: the process answers for itself, and stays.
            crawl = t.msg("c@x.org", to=", ".join(f"crawl {n} <c{n}@x.org>" for n in range(100)))
            try:
                gw.read_mail(crawl, t.NOW)
                raise AssertionError("a hundred names at a crawl are read in half a second?")
            except gw.UnreadableMail as exc:
                assert exc.kind == gw.TOO_SLOW
            assert gw.READER._process.pid == pid and not _gone(pid)
            # One call that does not come back: no look at the clock ends
            # it. The process is ended, a new one reads the canaries, and
            # the mail is a placeholder — in its time, not in ten minutes.
            kind, why, took = unreadable("hang")
            assert kind == gw.TOO_SLOW and why.startswith(gw.TOO_SLOW), why
            assert 1.0 <= took < 20.0, took
            assert _gone(pid) and gw.READER._process.pid != pid
            assert gw.read_mail(good, t.NOW).reading == ("Fine.", "deterministic")
            # A process that ends by itself on a mail: the mail's, likewise.
            pid = gw.READER._process.pid
            kind, why, _took = unreadable("die")
            assert kind == gw.READER_ENDED and _gone(pid)
            assert gw.read_mail(good, t.NOW).plain == "Fine.\n"
            # One that went while nothing was asked of it is nobody's mail.
            gw.READER._process.kill()
            gw.READER._process.wait()
            assert gw.read_mail(good, t.NOW).plain == "Fine.\n"
            # The canaries not read in their time either: the mirror's.
            gw.CANARY_SECONDS = 0.5
            Path(str(flag) + ".canary").write_text("")
            try:
                gw.read_mail(crawl, t.NOW)
                raise AssertionError("a reader too slow for its own test mail")
            except gw.MirrorFault as exc:
                assert "too slow" in str(exc), exc
            Path(str(flag) + ".canary").unlink()
            # A header block is read there too — for a placeholder, and to
            # find a mail again — with the canaries' time, and ended alike.
            head = good.split(b"\n\n")[0]
            assert gw.read_head(head) == gw._head_facts(head) == {
                "id": "good@x.org", "message_id": "<good@x.org>", "resent": False}
            assert gw.read_stub(good, t.NOW, "why") == gw.stub_mail(good, t.NOW, "why")
            pid = gw.READER._process.pid
            stuck = t.msg("stuck@x.org", body="Worth the wait.\n", day=39.9)
            began = time.monotonic()
            assert gw.read_head(stuck.split(b"\n\n")[0]) is None
            bare = gw.read_stub(stuck, t.NOW, "why")
            assert 1.0 <= time.monotonic() - began < 20.0 and _gone(pid)
            # (Nothing of it was parsed: an id of its bytes, and no sender.)
            assert bare.message_id.endswith("@no-message-id.mail.invalid") and bare.sender == ""
            assert bare.unreadable == "why" and "Message-ID: <stuck@x.org>" in bare.headers
            gw.CANARY_SECONDS = 5.0

            # In the mirror: the mail that hangs is a placeholder, the folder
            # goes on, /health counts it — and it is read again at the hour.
            t._write_policy(gw)
            events = t.rail(gw)
            clock = _Clock()
            box = t.FakeMailbox(gw)
            mirror = gw.Mirror(box, clock=clock)
            mirror.poll()
            loop: dict = {}
            box.put("INBOX", t.msg("a@x.org", day=39.9))
            box.put("INBOX", t.msg("hang@x.org", subject="hang", body="Worth the wait.\n",
                                   day=39.9, files=[("x.pdf", "application/pdf", b"%PDF")]))
            box.put("INBOX", t.msg("c@x.org", day=39.9))
            snap = _looped(gw, mirror, loop)
            assert snap["connected"] is True and snap["error"] is None, snap
            assert monitor.classify_health(200, snap)[0] == "up"
            assert _whole(gw) == ["a@x.org", "c@x.org"] and len(t.records(gw)) == 3
            # (Its placeholder was read there as well: who sent it, and when.)
            hung = t.by_id(gw)["hang@x.org"]
            assert hung["text"] == gw.UNREADABLE_TEXT and hung["sender"] == "ann@x.org"
            assert snap["placeholders"] == {"standing": 1, "made": 1, "why": {gw.TOO_SLOW: 1}}
            assert mirror._folder("inbox")["hwm"] == 3 and len(box.fetched) == 3
            flag.unlink()
            clock.now += 3600
            mirror.poll()
            hung = t.by_id(gw)["hang@x.org"]
            assert hung["text"] == "Worth the wait." and len(hung["attachments"]) == 1
            assert mirror._placeholders == set() and len(t.inbound(events)) == 4
        finally:
            gw.READER.close()

        # A reader process that cannot be started is nobody's mail, and not
        # the canaries' to decide: the mirror's surroundings, like a full
        # disk. The pass ends; nothing is set aside.
        gw.READER = gw.Reader([sys.executable, "-c", "raise SystemExit(3)"])
        try:
            try:
                gw.read_mail(good, t.NOW)
                raise AssertionError("read without a reader?")
            except gw.ReaderDown as exc:
                assert isinstance(exc, OSError) and gw._environment(exc)
                assert "could not be started" in str(exc)
            box.put("INBOX", t.msg("d@x.org", day=39.9))
            snap = _looped(gw, mirror, loop)
            assert snap["connected"] is False and snap["blocked"] == "mirror", snap
            assert "reader process could not be started" in snap["error"]
            assert mirror._folder("inbox")["hwm"] == 3 and mirror._placeholders == set()
        finally:
            gw.READER.close()
        gw.READER = None
        assert _looped(gw, mirror, loop)["connected"] is True
        assert "d@x.org" in _whole(gw)
        # The running gateway reads its mail that way: main() makes the
        # reader with the mirror (and starts it with the first mail).
        gw = t.load(tmp)
        gw.Mailbox, gw.Mirror = (lambda cfg: "mailbox"), (lambda mailbox: "mirror")
        gw._mirror_loop = gw._serve_http = lambda *args: None
        assert gw.READER is None
        gw.main()
        assert isinstance(gw.READER, gw.Reader) and gw.READER._process is None
        assert gw.MIRROR == "mirror"
    print("ok: the reader process is ended when a mail outlasts its deadline in one call, "
          "and the mail is a placeholder")


def test_mail_that_cannot_be_read_is_a_placeholder_at_once():
    """Whoever can send a mail can send one the reader fails on — and a
    second one, in the same minute. Each is the mail's fault as long as the
    canary reads, however many in a row and whatever was mirrored before: a
    placeholder at once, the mark past it, the mail fetched once and not at
    every poll — and the mirror as healthy as it was, on /health and for the
    monitor."""
    monitor = _monitor()
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        t._write_policy(gw)
        events = t.rail(gw)
        mirror, box = t.started(gw)
        loop: dict = {}
        # Right after a start, with no mail mirrored yet, two in one minute:
        # sixty kilobytes of mails nested two thousand deep.
        box.put("INBOX", _nested("p1@evil.org"), date=t.NOW - 5)
        box.put("INBOX", _nested("p2@evil.org", b"multipart/mixed"), date=t.NOW - 4)
        for n in range(3):
            snap = _looped(gw, mirror, loop)
            assert snap["connected"] is True and snap["error"] is None, snap
            assert snap["blocked"] is None and monitor.classify_health(200, snap)[0] == "up"
            assert len(box.fetched) == 2, "read once, not at every poll"
        recs = t.by_id(gw)
        assert sorted(recs) == ["p1@evil.org", "p2@evil.org"] and _whole(gw) == []
        assert all(r["text"] == gw.UNREADABLE_TEXT and r["sender"] == "m@evil.org"
                   for r in recs.values())
        assert mirror._folder("inbox")["hwm"] == 2 and len(t.inbound(events)) == 2
        # The same where the parser itself gives up — a recursion the
        # interpreter ends — and for whatever else a reader can raise.
        gw.MAX_NESTING = 10 ** 6
        try:
            gw.parse_mail(_nested("deep@evil.org"))
            raise AssertionError("the parser gets to the bottom of this?")
        except RecursionError:
            pass
        real = gw.parse_mail
        gw.parse_mail = lambda raw, internal_date=None: (
            (_ for _ in ()).throw(MemoryError()) if b"poison" in raw
            else real(raw, internal_date))
        try:
            box.put("INBOX", _nested("p3@evil.org"), date=t.NOW - 3)
            box.put("INBOX", t.msg("p4@x.org", subject="poison", day=39.9))
            box.put("INBOX", t.msg("p5@x.org", subject="poison", day=39.9))
            box.put("INBOX", t.msg("good@x.org", day=39.9))
            box.put("INBOX", t.msg("p6@x.org", subject="poison", day=39.9))
            snap = _looped(gw, mirror, loop)
        finally:
            gw.parse_mail = real
        assert snap["connected"] is True and snap["error"] is None, snap
        assert _whole(gw) == ["good@x.org"] and len(t.records(gw)) == 7
        assert mirror._folder("inbox")["hwm"] == 7 and len(box.fetched) == 7
        assert mirror._placeholders == {f"p{n}@evil.org" for n in (1, 2, 3)} | {
            f"p{n}@x.org" for n in (4, 5, 6)}
        assert len(t.inbound(events)) == len(t.records(gw))
    print("ok: mail that cannot be read is a placeholder at once, however many in a row, "
          "and /health stays connected")


def test_the_backfill_is_not_held_up_by_mail_that_cannot_be_read():
    with tempfile.TemporaryDirectory() as tmp:
        # A first backfill with unreadable mails in its window — six in a
        # row, with nothing new arriving to speak for anyone — and seven
        # more that arrive while it runs: it reads on, and is done.
        gw = t.load(tmp)
        t.rail(gw)
        box = t.FakeMailbox(gw)
        for n in range(4):
            box.put("INBOX", t.msg(f"g{n}@x.org", day=1 + n))
        for n in range(6):
            box.put("INBOX", _nested(f"old{n}@evil.org"), date=t.T0 + (5 + n) * t.DAY)
        for n in range(4, 12):
            box.put("INBOX", t.msg(f"g{n}@x.org", day=10 + n))
        mirror = gw.Mirror(box, clock=lambda: t.NOW)
        assert mirror.poll(backfill_limit=2) is True
        assert _whole(gw) == ["g0@x.org", "g1@x.org"]
        for n in range(7):
            box.put("INBOX", _nested(f"new{n}@evil.org"), date=t.NOW - 5)
        for n in range(7):
            # Every look at the folders reads its batch of old mail.
            assert mirror.poll(backfill_limit=2) is True, n
            assert mirror.backfill_status()["pending"] == 16 - 2 * (n + 1), n
        assert mirror.poll(backfill_limit=2) is False
        assert mirror.backfill_status() == {"done": True, "pending": 0, "days": 90}
        assert _whole(gw) == sorted(f"g{n}@x.org" for n in range(12))
        assert len(mirror._placeholders) == 13 and len(t.records(gw)) == 25
        assert len(box.fetched) == 25, "each mail was fetched once"
    with tempfile.TemporaryDirectory() as tmp:
        # A backfill under a mirror that reads nothing stops at the first
        # mail instead of fetching the whole window to learn it again.
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
                try:
                    mirror.poll(backfill_limit=10)
                    raise AssertionError("a reader that reads nothing must fail the pass")
                except gw.MirrorFault as exc:
                    assert "own test mail" in str(exc)
            assert len(box.fetched) == 3 and t.records(gw) == []
            assert mirror.backfill_status()["done"] is False
        finally:
            gw.parse_mail = real
        while mirror.poll(backfill_limit=10):
            pass
        assert len(_whole(gw)) == 40 and mirror.backfill_status()["done"] is True
    print("ok: unreadable mail holds back no old mail; a reader that reads nothing ends "
          "the pass at the first")


def test_mail_the_mirror_could_not_mirror_is_read_wherever_it_has_gone():
    """No mail is ever without a record, without a place and behind the mark
    all at once. While the mirror is at fault, the mail it could not mirror
    stays above the folder's mark; archived on the phone meanwhile — while
    the gateway runs, or while it is down — it is a mail that came and went,
    and is read in the archive once the mirror works."""
    for down in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            gw = t.load(tmp)
            t._write_policy(gw)
            events = t.rail(gw)
            mirror, box = t.started(gw)
            loop: dict = {}
            box.put("INBOX", t.msg("a@x.org", day=39.9), date=t.NOW - 5)
            assert _looped(gw, mirror, loop)["connected"] is True
            real = gw._trim.trim
            gw._trim.trim = lambda *a, **kw: (_ for _ in ()).throw(MemoryError())
            try:
                for mid in ("b@x.org", "c@x.org", "d@x.org", "e@x.org"):
                    box.put("INBOX", t.msg(mid, subject="S " + mid, day=39.95), date=t.NOW - 5)
                box.put("Sent", t.msg("s@example.org", sender=t.ME, to="ann@x.org",
                                      subject="Mine", day=39.95), date=t.NOW - 5)
                for _ in range(2):
                    assert _looped(gw, mirror, loop)["connected"] is False
                    assert mirror._folder("inbox")["hwm"] == 1
                # The user reads on the phone, and files three of them away.
                box.move_mail("INBOX", box.uid_of("INBOX", "c@x.org"), "Archive")
                box.move_mail("INBOX", box.uid_of("INBOX", "d@x.org"), "Archive")
                box.move_mail("Sent", box.uid_of("Sent", "s@example.org"), "Archive")
                if not down:
                    assert _looped(gw, mirror, loop)["connected"] is False
            finally:
                gw._trim.trim = real
            if down:
                mirror = gw.Mirror(box, clock=lambda: t.NOW)
            snap = _looped(gw, mirror, loop)
            assert snap["connected"] is True and snap["error"] is None, snap
            recs = t.by_id(gw)
            assert _whole(gw) == ["a@x.org", "b@x.org", "c@x.org", "d@x.org", "e@x.org",
                                  "s@example.org"], sorted(recs)
            assert recs["s@example.org"]["type"] == gw._ibstore.T_OUTBOUND
            assert recs["c@x.org"]["type"] == gw._ibstore.T_INBOUND
            assert sorted(e["message_id"] for e in t.inbound(events)) == sorted(recs)
            assert mirror.index.get("archive_due") is None
            # They are followed like any mail from here on.
            assert sorted((row["role"], row["message_id"]) for row in mirror.index.rows(
                "SELECT role, message_id FROM locations WHERE role = 'archive'")) == [
                    ("archive", "c@x.org"), ("archive", "d@x.org"), ("archive", "s@example.org")]
            _looped(gw, mirror, loop)
            assert not any(r["removed_at"] for r in t.records(gw))
    print("ok: a mail the mirror could not mirror is read in the archive if it went there "
          "meanwhile, also across a restart")


def test_set_aside_mail_is_read_again_while_the_gateway_runs():
    """Not only at the next start: what made a mail unreadable may have been
    a moment the program was in. A placeholder is read again every hour —
    less often each time it changes nothing — and a mail that reads now
    takes its placeholder's place."""
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
        box.put("INBOX", t.msg("d@x.org", day=39.9))
        real_trim = gw._trim.trim
        broken = {"flaky", "never"}

        def trim(plain, html, subject=None, **evidence):
            if subject in broken:
                raise RecursionError("this mail, for now")
            return real_trim(plain, html, subject=subject, **evidence)
        gw._trim.trim = trim
        try:
            mirror.poll()
            mirror.poll()
            recs = t.by_id(gw)
            assert _whole(gw) == ["a@x.org", "b@x.org", "c@x.org", "d@x.org"]
            assert recs["flaky@example.org"]["type"] == gw._ibstore.T_OUTBOUND
            assert mirror._placeholders == {"flaky@example.org", "never@x.org"}
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
            assert len(box.fetched) == fetched + 2 and len(t.records(gw)) == 6
            again = [e for e in t.inbound(events)[told:]]
            assert [(e["message_id"], e["direction"], e.get("backfill")) for e in again] == [
                ("flaky@example.org", "out", True)]
            # The other is as it was: the same record, byte for byte, and
            # nothing said to the rail.
            assert before == {p.name: p.read_text() for p in Path(
                gw._ibstore.messages_dir(gw.INBOUND_STORE_DIR)).glob("*.nt")
                if "never@x.org" in p.read_text()}
            assert mirror._placeholders == {"never@x.org"}
            # Each try fetches the mail whole, so one that changes nothing is
            # tried less and less often: not after one more hour, but after two.
            fetched = len(box.fetched)
            clock.now += 3600
            mirror.poll()
            assert len(box.fetched) == fetched
            clock.now += 3600
            mirror.poll()
            assert len(box.fetched) == fetched + 1
            assert t.by_id(gw)["never@x.org"]["text"] == gw.UNREADABLE_TEXT
            assert mirror._placeholders == {"never@x.org"}
            # A placeholder whose mail is in the archive by now is read there
            # — but not while the archive does not answer: that is no reason
            # to fail a pass that needs only INBOX and Sent.
            box.move_mail("INBOX", box.uid_of("INBOX", "never@x.org"), "Archive")
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
            assert mirror._placeholders == set()
        finally:
            gw._trim.trim = real_trim
        # Nothing of a replaced placeholder is left in the media store.
        named = {gw._ibstore.media_id_of(ref) for r in t.records(gw)
                 for ref in gw._ibstore._media_refs(r)}
        on_disk = {p.name for p in gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir()
                   if "." not in p.name}
        assert on_disk == named
    print("ok: set-aside mail is read again every hour while the gateway runs, and a mail "
          "that reads now replaces its placeholder")


def test_a_placeholder_gives_way_only_to_the_copy_it_was_made_from():
    """A placeholder is under its mail's Message-ID, and an id is its
    sender's to choose: whoever was sent a mail knows its id. So a mail that
    arrives under a placeholder's id replaces nothing — or an outsider's
    words would stand in the chat as what the user wrote. Only the copy the
    placeholder was made from does, read again: the mail whose header block
    the placeholder's record keeps."""
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        t._write_policy(gw)
        events = t.rail(gw)
        clock = _Clock()
        box = t.FakeMailbox(gw)
        mirror = gw.Mirror(box, clock=clock)
        mirror.poll()
        box.put("INBOX", t.msg("hello@evil.org", sender="Mallory <m@evil.org>",
                               subject="Have a look", day=39.8))
        mirror.poll()
        real_trim = gw._trim.trim
        broken = {"flaky"}

        def trim(plain, html, subject=None, **evidence):
            if subject in broken:
                raise RecursionError("this mail, for now")
            return real_trim(plain, html, subject=subject, **evidence)
        gw._trim.trim = trim
        ledger = Path(gw._ibstore.messages_dir(gw.INBOUND_STORE_DIR))

        def on_disk() -> dict:
            return {p.name: p.read_text() for p in ledger.glob("*.nt")}

        def places(mid) -> list:
            return [(row["role"], row["uid"], row["skipped"]) for row in mirror.index.rows(
                "SELECT role, uid, skipped FROM locations WHERE message_id = ? "
                "ORDER BY role, uid", (mid,))]
        try:
            # The user sends it on to Bob, Mallory on Cc; their own mail in
            # Sent cannot be read just now. Nor can a mail of Ann's.
            box.put("Sent", t.msg("fwd@example.org", sender=t.ME, to="Bob <bob@y.org>",
                                  cc="Mallory <m@evil.org>", subject="flaky",
                                  body="Bob, is this genuine?\n", refs=["hello@evil.org"],
                                  day=39.9))
            mirror.poll()
            box.put("INBOX", t.msg("note@x.org", day=39.9))
            box.put("INBOX", t.msg("ann@x.org", subject="flaky", body="Lunch on Friday?\n",
                                   day=39.9))
            mirror.poll()
            assert mirror._placeholders == {"ann@x.org", "fwd@example.org"}
            mine = t.by_id(gw)["fwd@example.org"]
            assert mine["type"] == gw._ibstore.T_OUTBOUND and mine["author"] == "device"
            before, told = on_disk(), len(events)
            # Mallory was on Cc and knows the id: a mail under it arrives,
            # and one under the id of Ann's, which went to a list Mallory reads.
            wire = "Bob, please wire the 40,000 to the new account today.\n"
            box.put("INBOX", t.msg("fwd@example.org", sender="Mallory <m@evil.org>",
                                   to="me@example.org", subject="Fwd: have a look",
                                   body=wire, day=39.95))
            box.put("INBOX", t.msg("ann@x.org", sender="Ann Example <ann@x.org>",
                                   subject="Lunch", body="Send me your card number.\n",
                                   day=39.95))
            mirror.poll()
            # Nothing is replaced: the records are the placeholders they
            # were, byte for byte, the rail hears nothing, and Mallory's
            # words are nowhere in the ledger — least of all as the user's.
            assert on_disk() == before and len(events) == told
            assert mirror._placeholders == {"ann@x.org", "fwd@example.org"}
            blobs = b"".join(p.read_bytes() for p in
                             gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir())
            assert b"wire the 40,000" not in blobs and b"card number" not in blobs
            # Each is one more place of the mail on record, as a second copy
            # of any mail is — and known not to be the placeholder's own.
            assert places("fwd@example.org") == [("inbox", 4, "other"), ("sent", 1, None)]
            assert places("ann@x.org") == [("inbox", 3, None), ("inbox", 5, "other")]
            assert mirror._folder("inbox")["hwm"] == 5
            # The hour comes: the placeholders' own copies are read again,
            # the others are not.
            fetched = len(box.fetched)
            clock.now += 3600
            mirror.poll()
            assert sorted(box.fetched[fetched:]) == [
                ("INBOX", 3, gw.MAIL_MAX_MAIL_BYTES), ("Sent", 1, gw.MAIL_MAX_MAIL_BYTES)]
            assert on_disk() == before
            # Nor does it help to be read again from a place on record — as
            # it is once places were found anew by Message-ID, which finds
            # the newest mail under an id. What decides is the copy: Ann's
            # own mail (the first under its id) reads now and takes its
            # placeholder's place; Mallory's, read in place of the user's,
            # replaces nothing and is marked again.
            mirror.index.run("UPDATE locations SET skipped = NULL")
            broken.clear()
            mirror._retried.clear()
            fetched = len(box.fetched)
            clock.now += 3600
            mirror.poll()
            assert sorted(box.fetched[fetched:]) == [
                ("INBOX", 3, gw.MAIL_MAX_MAIL_BYTES), ("INBOX", 4, gw.MAIL_MAX_MAIL_BYTES)]
            recs = t.by_id(gw)
            assert recs["ann@x.org"]["text"] == "Lunch on Friday?"
            assert recs["fwd@example.org"] == mine and mirror._placeholders == {"fwd@example.org"}
            assert places("fwd@example.org") == [("inbox", 4, "other"), ("sent", 1, None)]
        finally:
            gw._trim.trim = real_trim
        # A start reads the placeholder again, from its own copy — known by
        # what the record keeps, so across a restart: now it reads, and takes
        # the placeholder's place as the user's mail, with what the user wrote.
        mirror = gw.Mirror(box, clock=clock)
        while mirror.poll():
            pass
        recs = t.by_id(gw)
        assert mirror._placeholders == set() and len(recs) == 4
        mine = recs["fwd@example.org"]
        assert mine["type"] == gw._ibstore.T_OUTBOUND and mine["author"] == "device"
        assert mine["text"] == "Bob, is this genuine?" and mine["sender"] == "me@example.org"
        assert mine["recipients"] == ["bob@y.org"] and mine["cc"] == ["m@evil.org"]
        assert [p["address"] for p in mirror.thread(mine["chat"])["participants"]] == [
            "m@evil.org", "bob@y.org"]
        assert [(e["message_id"], e["direction"], e["text"]) for e in t.inbound(events)[told:]] == [
            ("ann@x.org", "in", "Lunch on Friday?"),
            ("fwd@example.org", "out", "Bob, is this genuine?")]
        assert b"wire the 40,000" not in b"".join(
            p.read_bytes() for p in gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir())
    print("ok: a mail that arrives under a placeholder's id replaces nothing; only the "
          "copy the placeholder was made from does")


# --------------------------------------------------------------------------
def _flaky(gw, broken: set):
    """Make the trim fail on every mail whose Subject is in ``broken`` — a
    fault that passes when the set is emptied. Returns the undo."""
    real = gw._trim.trim

    def trim(plain, html, subject=None, **evidence):
        if subject in broken:
            raise RecursionError("this mail, for now")
        return real(plain, html, subject=subject, **evidence)
    gw._trim.trim = trim
    return lambda: setattr(gw._trim, "trim", real)


def _places(mirror, message_id=None) -> list:
    return [(row["role"], row["uid"], row["skipped"]) for row in mirror.index.rows(
        "SELECT role, uid, skipped FROM locations WHERE message_id IS ? "
        "ORDER BY role, uid", (message_id,))]


def test_a_mail_that_cannot_be_read_is_no_placeholders_copy_either():
    """A mail under a placeholder's id that is not the copy the placeholder
    was made from is marked as another mail's — but it was, only when it
    could be read. One that could not got a place like the copy's own, and
    the reading again goes to INBOX before Sent: whoever was sent a mail
    knows its id, so a recipient could pin the placeholder of a mail the
    user sent for good, with one mail nested too deep to read."""
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        t._write_policy(gw)
        t.rail(gw)
        clock = _Clock()
        box = t.FakeMailbox(gw)
        mirror = gw.Mirror(box, clock=clock)
        mirror.poll()
        broken = {"flaky"}
        undo = _flaky(gw, broken)
        try:
            box.put("Sent", t.msg("own@example.org", sender=t.ME, to="Bob <bob@y.org>",
                                  cc="Mallory <m@evil.org>", subject="flaky",
                                  body="Bob, the offer stands.\n", day=39.9))
            mirror.poll()
            assert mirror._placeholders == {"own@example.org"}
            mine = t.by_id(gw)["own@example.org"]
            # Mallory, on Cc, sends a mail under that id that nobody reads.
            box.put("INBOX", _nested("own@example.org", depth=300), date=t.NOW - 5)
            mirror.poll()
            assert _places(mirror, "own@example.org") == [("inbox", 1, "other"),
                                                         ("sent", 1, None)]
            assert t.by_id(gw)["own@example.org"] == mine and len(t.records(gw)) == 1
            # The hour comes: it is the placeholder's own copy that is read
            # again — in vain, and its place stays what it was.
            fetched = len(box.fetched)
            clock.now += 3600
            mirror.poll()
            assert box.fetched[fetched:] == [("Sent", 1, gw.MAIL_MAX_MAIL_BYTES)]
            assert _places(mirror, "own@example.org") == [("inbox", 1, "other"),
                                                         ("sent", 1, None)]
            assert t.by_id(gw)["own@example.org"] == mine
            # The fault passes, and the mail the user sent is on record.
            broken.clear()
            mirror._retried.clear()
            clock.now += 3600
            mirror.poll()
        finally:
            undo()
        mine = t.by_id(gw)["own@example.org"]
        assert mine["text"] == "Bob, the offer stands." and mirror._placeholders == set()
        assert mine["type"] == gw._ibstore.T_OUTBOUND and mine["sender"] == "me@example.org"
        # No placeholder any more: what else lies under the id is one more
        # place of the mail on record, as a second copy of any mail is.
        assert _places(mirror, "own@example.org") == [("inbox", 1, None), ("sent", 1, None)]
    print("ok: an unreadable mail under a placeholder's id is marked as another mail's, "
          "and the placeholder is read again from its own copy")


def test_a_placeholder_whose_own_copy_is_gone_gives_way_to_what_is_left():
    """The INBOX copy of a mail to oneself has trace fields its Sent copy
    has not, so beside a placeholder made from the Sent copy it is marked
    as another mail's. With the Sent copy deleted, the placeholder was then
    neither read again (its only place was the marked one) nor called gone
    (it had a place): unreadable for good, with a readable copy in the
    mailbox. A marked place is no place of the mail on record: the
    placeholder leaves with its own copy, and what is left is read as the
    mail it is."""
    def to_self(gw, box, mirror, broken):
        raw = t.msg("self@example.org", sender=t.ME, to=t.ME, subject="flaky",
                    body="Note to self.\n", day=39.9)
        sent = box.put("Sent", raw)
        mirror.poll()
        assert mirror._placeholders == {"self@example.org"}
        assert t.by_id(gw)["self@example.org"]["type"] == gw._ibstore.T_OUTBOUND
        broken.clear()      # the fault passes; the hour has not come
        box.put("INBOX", b"Return-Path: <me@example.org>\r\n"
                b"Received: by mx.example.org; Mon, 17 Nov 2025 10:00:00 +0000\r\n" + raw)
        mirror.poll()
        assert mirror._placeholders == {"self@example.org"}
        assert _places(mirror, "self@example.org") == [("inbox", 1, "other"), ("sent", 1, None)]
        return sent

    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        t._write_policy(gw)
        events = t.rail(gw)
        clock = _Clock()
        box = t.FakeMailbox(gw)
        mirror = gw.Mirror(box, clock=clock)
        mirror.poll()
        broken = {"flaky"}
        undo = _flaky(gw, broken)
        try:
            sent = to_self(gw, box, mirror, broken)
            # The user deletes the Sent copy and keeps the one in INBOX.
            box.remove("Sent", sent)
            told = len(events)
            mirror.poll()
            assert [kind for kind, _payload in events[told:]] == ["removed", "inbound"]
            for _ in range(3):
                record = t.by_id(gw)["self@example.org"]
                assert record["text"] == "Note to self." and not record["removed_at"]
                assert record["type"] == gw._ibstore.T_INBOUND and len(t.records(gw)) == 1
                assert mirror._placeholders == set()
                assert _places(mirror, "self@example.org") == [("inbox", 1, None)]
                assert _places(mirror) == [], "nothing is left noted to be read again"
                # … and so it stays: after hours, and after a restart.
                clock.now += 3600
                mirror = gw.Mirror(box, clock=clock)
                while mirror.poll():
                    pass
            assert len(events) == told + 2
        finally:
            undo()
    with tempfile.TemporaryDirectory() as tmp:
        # With its own copy still there the placeholder heals at the hour,
        # as before — and then the other copy is a second place like any
        # mail's: deleting the first leaves the mail on record.
        gw = t.load(tmp)
        t._write_policy(gw)
        events = t.rail(gw)
        clock = _Clock()
        box = t.FakeMailbox(gw)
        mirror = gw.Mirror(box, clock=clock)
        mirror.poll()
        broken = {"flaky"}
        undo = _flaky(gw, broken)
        try:
            sent = to_self(gw, box, mirror, broken)
            clock.now += 3600
            mirror.poll()
            record = t.by_id(gw)["self@example.org"]
            assert record["text"] == "Note to self." and mirror._placeholders == set()
            assert record["type"] == gw._ibstore.T_OUTBOUND
            assert _places(mirror, "self@example.org") == [("inbox", 1, None), ("sent", 1, None)]
            box.remove("Sent", sent)
            told = len(events)
            mirror.poll()
            mirror.poll()
            assert t.by_id(gw)["self@example.org"] == record and len(events) == told
            assert _places(mirror, "self@example.org") == [("inbox", 1, None)]
        finally:
            undo()
    print("ok: a placeholder whose own copy is gone leaves a tombstone, and the copy "
          "that is left is read as the mail it is")


def test_a_mail_in_the_archive_that_lost_its_record_is_read_again():
    """A placeholder's mail that reads now has its record dropped to be
    written anew; when that write fails, its places say nothing true, and
    the next pass clears them and has the folders read again. In INBOX and
    Sent the backfill does that — the archive is read by none, and its mark
    is long past the UID: the mail stayed in the mailbox with no record and
    no place, for good. In the archive the place is kept as a note to read
    that UID again."""
    for failure in (OSError(28, "No space left on device"), ValueError("a writer bug")):
        with tempfile.TemporaryDirectory() as tmp:
            gw = t.load(tmp)
            t._write_policy(gw)
            events = t.rail(gw)
            clock = _Clock()
            box = t.FakeMailbox(gw)
            mirror = gw.Mirror(box, clock=clock)
            mirror.poll()
            broken = {"flaky"}
            undo = _flaky(gw, broken)
            real_write = gw._ibstore.write_message
            failures = [failure]

            def write(store, **fields):
                if failures:
                    raise failures.pop()
                return real_write(store, **fields)
            try:
                uid = box.put("INBOX", t.msg("m1@x.org", subject="flaky", day=39.9))
                mirror.poll()
                assert mirror._placeholders == {"m1@x.org"}
                box.move_mail("INBOX", uid, "Archive")
                mirror.poll()
                assert _places(mirror, "m1@x.org") == [("archive", 1, None)]
                # The fault passes; at the hour the mail reads, its record is
                # dropped — and the write of the new one fails, this once.
                broken.clear()
                gw._ibstore.write_message = write
                clock.now += 3600
                try:
                    mirror.poll()
                    raise AssertionError("a write that fails ends the pass")
                except (OSError, gw.MirrorFault):
                    pass
                assert t.records(gw) == [] and failures == []
                # The next pass reads it where it lies, quietly, as old mail.
                fetched, told = len(box.fetched), len(events)
                mirror.poll()
                assert box.fetched[fetched:] == [("Archive", 1, gw.MAIL_MAX_MAIL_BYTES)]
                record = t.by_id(gw)["m1@x.org"]
                assert record["text"] == "The numbers are in." and record["subject_line"] == "flaky"
                assert _places(mirror, "m1@x.org") == [("archive", 1, None)]
                assert _places(mirror) == [] and mirror._placeholders == set()
                assert [(e["message_id"], e.get("backfill")) for e in
                        t.inbound(events)[-1:]] == [("m1@x.org", True)] and len(events) == told + 1
                # Once: nothing reads it a third time.
                fetched = len(box.fetched)
                mirror = gw.Mirror(box, clock=clock)
                while mirror.poll():
                    pass
                assert len(box.fetched) == fetched and len(t.records(gw)) == 1
            finally:
                undo()
                gw._ibstore.write_message = real_write
    with tempfile.TemporaryDirectory() as tmp:
        # The same for the mark an older build left on an archive UID it
        # could not write a record for, and for the archive place of a
        # record that is gone from the ledger: read again, there.
        gw = t.load(tmp)
        t._write_policy(gw)
        t.rail(gw)
        mirror, box = t.started(gw)
        kept = box.put("INBOX", t.msg("kept@x.org", day=39.9))
        mirror.poll()
        box.move_mail("INBOX", kept, "Archive")
        old = box.put("Archive", t.msg("old@x.org", subject="Filed at once", day=39.9))
        gone = box.put("Archive", t.msg("gone@x.org", day=39.9))
        mirror.poll()
        assert _places(mirror, "kept@x.org") == [("archive", 1, None)] and len(t.records(gw)) == 1
        mirror.index.run("INSERT INTO locations (role, gen, uid, message_id, skipped) "
                         "VALUES ('archive', 1, ?, 'old@x.org', 'unreadable')", (old,))
        mirror.index.run("INSERT INTO locations (role, gen, uid, message_id, skipped) "
                         "VALUES ('archive', 1, ?, NULL, 'unreadable')", (gone,))
        next(path for path, fields in gw._ibstore.iter_records(gw.INBOUND_STORE_DIR)
             if fields["message_id"] == "kept@x.org").unlink()
        # (A UID noted for a mail the folder no longer holds is just dropped.)
        box.remove("Archive", gone)
        mirror = gw.Mirror(box, clock=lambda: t.NOW)
        assert sorted(_places(mirror)) == [("archive", 1, "again"), ("archive", 2, "again"),
                                           ("archive", 3, "again")]
        while mirror.poll():
            pass
        assert _whole(gw) == ["kept@x.org", "old@x.org"] and _places(mirror) == []
        assert _places(mirror, "old@x.org") == [("archive", 2, None)]
        assert _places(mirror, "kept@x.org") == [("archive", 1, None)]
    print("ok: a mail in the archive whose record was dropped and not written anew is "
          "read again there, as is one an older build had marked")


def test_reading_placeholders_again_has_its_time_each_round():
    """Reading a placeholder again reads its mail whole, and a mail that is
    a placeholder because reading it took too long takes that long again,
    with the mirror's one thread. A round of re-reading has a time of its
    own (SET_ASIDE_ROUND_SECONDS): what it does not get to waits for the
    next round, and those tried least often come first — so however many
    such mails a mailbox holds, each has its turn and the mirror its time."""
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        t._write_policy(gw)
        t.rail(gw)
        clock = _Clock()
        box = t.FakeMailbox(gw)
        mirror = gw.Mirror(box, clock=clock)
        mirror.poll()
        undo = _flaky(gw, {"never"})
        try:
            for n in (1, 2, 3):
                box.put("INBOX", t.msg(f"p{n}@x.org", subject="never", day=39.9))
            mirror.poll()
            assert len(mirror._placeholders) == 3 and gw.SET_ASIDE_ROUND_SECONDS == 120.0

            def round_reads() -> list:
                fetched = len(box.fetched)
                clock.now += 3600
                while mirror.poll():
                    pass
                return [uid for _folder, uid, _limit in box.fetched[fetched:]]
            # A round whose time is used up by its first reading: one mail a
            # round, each in its turn before any has a second.
            gw.SET_ASIDE_ROUND_SECONDS = 0.0
            assert [round_reads() for _ in range(4)] == [[1], [2], [3], [1]]
            # With time enough, a round reads all that are due.
            gw.SET_ASIDE_ROUND_SECONDS = 120.0
            mirror._retried.clear()
            assert round_reads() == [1, 2, 3]
        finally:
            undo()
    print("ok: a round of reading placeholders again has its time, and every "
          "placeholder its turn")


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
