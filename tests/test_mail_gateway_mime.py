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
    print("ok: a mail without a Message-ID gets one derived from its headers")


def test_an_overlong_message_id_is_digested_consistently():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        t.rail(gw)
        mirror, box = t.started(gw)
        long_id = "x" * 400 + "@x.org"
        box.put("INBOX", t.msg(long_id, day=1))
        box.put("INBOX", t.msg("reply@x.org", refs=[long_id], irt=long_id, day=2))
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
        t.rail(gw)
        mirror, box = t.started(gw)
        box.put("INBOX", t.msg("a@x.org", day=1))
        box.put("INBOX", t.msg("bad@x.org", subject="poison", day=2))
        box.put("INBOX", t.msg("c@x.org", day=3))
        real = gw.parse_mail

        def parse(raw, internal_date=None):
            if b"poison" in raw:
                raise RuntimeError("a parser bug")
            return real(raw, internal_date)
        gw.parse_mail = parse
        mirror.poll()
        assert sorted(r["message_id"] for r in t.records(gw)) == ["a@x.org", "c@x.org"]
        # Noted, so it is not read on every poll for ever.
        fetched = len(box.fetched)
        mirror.poll()
        assert len(box.fetched) == fetched
    print("ok: a mail the reader chokes on is set aside; the folder goes on")


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
