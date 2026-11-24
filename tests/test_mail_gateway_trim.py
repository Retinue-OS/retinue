#!/usr/bin/env python3
"""The mail gateway's half of the model trim (scripts/mail-gateway.py;
docs/mail-chats.md, "The model trim").

``POST /messages/trim-source`` hands out what a model may choose a
``wanting`` mail's own message from — the deterministic trim's own lines,
rebuilt from the record's parts with the evidence the ingest used — and
``POST /messages/trim-result`` takes the choice back and makes it the
record's text from the gateway's own copy of those lines. This pins every
answer of both against the fake mailbox of tests/test_mail_gateway.py and the
real HTTP handler:

- the source: lines equal to the text the ingest trimmed (so the evidence —
  the subject, a cited parent, bulk headers that only the header block still
  says — is the ingest's), a settled mail, a mail too long to hand over
  (settled there and then), the parts unreadable (nothing settled);
- the result: a valid choice, an invalid one, ``failed``, a stale digest, a
  mail already settled, malformed bodies, an oversized one;
- which record: another account's, an unknown mail, a mail named under a
  chat that is not its own, one named under a chat merged into its own since
  (it lands on the record), one removed from the mailbox (410), before and
  between the two calls;
- the lock: a pass over the mailbox that holds it answers 503, and nothing
  is written;
- and after every path, the record's parts — every blob — byte-identical,
  and nothing on the record but its text and its trim changed.

    python3 tests/test_mail_gateway_trim.py
"""
import hashlib
import importlib.util
import json
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("mail_gateway_helpers",
                                               HERE / "test_mail_gateway.py")
t = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t)

msg, ACCOUNT, TOKEN = t.msg, t.ACCOUNT, t.TOKEN

PARA = ("A paragraph the author wrote themselves, long enough that a few of "
        "them make a mail the rules call long.")
LONG_BODY = "\n\n".join(f"{i}. {PARA}" for i in range(40)) + "\n"


def rig(tmp):
    """A gateway with a mirror, its HTTP server, and a handful of mails:
    ``(gw, mirror, box, base, server)``."""
    gw = t.load(tmp)
    t.rail(gw)
    mirror, box = t.started(gw)
    gw.MIRROR = mirror
    gw.EXCLUSIVE_SECONDS = 0.2
    # Short and plain: settled by the rules.
    box.put("INBOX", msg("short@x.org", subject="Lunch", body="Noon works.\n", day=1))
    # Long: wanting, every line the author's.
    box.put("INBOX", msg("long@x.org", subject="Notes", body=LONG_BODY, day=2))
    # HTML only.
    html_only = (b"From: Ann Example <ann@x.org>\r\nTo: me@example.org\r\n"
                 b"Subject: Poster\r\nMessage-ID: <html@x.org>\r\n"
                 b"Date: Fri, 03 Oct 2025 10:00:00 +0000\r\nMIME-Version: 1.0\r\n"
                 b"Content-Type: text/html; charset=utf-8\r\n\r\n"
                 b"<p>Hello there,</p><p>the poster is ready.</p>"
                 b"<p>Kind regards<br>Ann</p>\r\n")
    box.put("INBOX", html_only)
    # A reply with history no ">" marks: cut only because the ingest knew it
    # cites a parent and is subjected as a reply; long, so still wanting.
    reply = (LONG_BODY + "\n-----Original Message-----\nFrom: Me\nSent: Monday\n"
             "To: Ann\nSubject: Notes\n\nThe question I asked.\n")
    box.put("INBOX", msg("reply@x.org", subject="RE: Notes", body=reply, irt="long@x.org",
                         refs=["long@x.org"], day=4))
    # Bulk by List-Unsubscribe alone — no List-Id on the record to say so —
    # with the footer only bulk mail may lose.
    bulk = (LONG_BODY + "\nYou are receiving this because you subscribed.\n"
            "Unsubscribe: https://lists.example.org/unsubscribe?u=1\n")
    box.put("INBOX", msg("bulk@x.org", subject="Digest", body=bulk, day=5,
                         headers=[("List-Unsubscribe", "<https://lists.example.org/u>")]))
    # More than may be handed to a model.
    huge = "\n\n".join(f"{i}. {PARA}" for i in range(700)) + "\n"
    box.put("INBOX", msg("huge@x.org", subject="Everything", body=huge, day=6))
    mirror.poll()
    server, base = t.serve(gw)
    return gw, mirror, box, base, server


def post(base, path, payload, token=TOKEN):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    status, _h, raw = t.call(base + path, body=body, token=token)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def source(base, mid, chat=None, account=ACCOUNT):
    return post(base, "/messages/trim-source",
                {"chat": chat or mid, "account": account, "message_id": mid})


def result(base, mid, digest, chat=None, account=ACCOUNT, **rest):
    return post(base, "/messages/trim-result",
                {"chat": chat or mid, "account": account, "message_id": mid,
                 "digest": digest, **rest})


def snapshot(gw, mid):
    """What of a mail must never change: every blob the record references,
    and the record's fields but for its text and its trim."""
    fields = t.by_id(gw)[mid]
    blobs = {}
    for ref in [fields.get(k) for k in ("body_part", "html_part", "header_part")] \
            + list(fields["attachments"]):
        if ref:
            blobs[ref] = hashlib.sha256(t.blob(gw, ref)).hexdigest()
    rest = {k: v for k, v in fields.items() if k not in ("text", "trim")}
    return blobs, rest


def unchanged_but_reading(gw, mid, before):
    assert snapshot(gw, mid) == before, mid


def test_the_source_is_what_the_ingest_trimmed():
    with tempfile.TemporaryDirectory() as tmp:
        gw, _mirror, _box, base, server = rig(tmp)
        try:
            recs = t.by_id(gw)
            assert recs["short@x.org"]["trim"] == "deterministic"
            for mid in ("long@x.org", "html@x.org", "reply@x.org", "bulk@x.org", "huge@x.org"):
                assert recs[mid]["trim"] == "wanting", (mid, recs[mid]["trim"])
            for mid, subject, is_reply, bulk in (
                    ("long@x.org", "Notes", False, False),
                    ("html@x.org", "Poster", False, False),
                    ("reply@x.org", "RE: Notes", True, False),
                    ("bulk@x.org", "Digest", False, True)):
                before = snapshot(gw, mid)
                status, answer = source(base, mid)
                assert status == 200 and answer["state"] == "wanting", (mid, status, answer)
                # The text the ingest trimmed, line for line: the same code
                # path, given the same evidence.
                assert "\n".join(answer["lines"]) == recs[mid]["text"], mid
                assert answer["digest"] == hashlib.sha256(
                    "\n".join(answer["lines"]).encode()).hexdigest()
                assert (answer["subject"], answer["is_reply"], answer["bulk"]) == (
                    subject, is_reply, bulk), (mid, answer)
                assert answer["sender"] == "ann@x.org", answer
                # Read-only.
                unchanged_but_reading(gw, mid, before)
                assert t.by_id(gw)[mid]["trim"] == "wanting"
                assert t.by_id(gw)[mid]["text"] == recs[mid]["text"]
                # And asked again, the same.
                assert source(base, mid) == (status, answer)
            # The evidence did its work: the history and the footer are not offered.
            reply_lines = source(base, "reply@x.org")[1]["lines"]
            assert "-----Original Message-----" not in reply_lines
            bulk_lines = source(base, "bulk@x.org")[1]["lines"]
            assert not any("Unsubscribe" in line for line in bulk_lines), bulk_lines
            # Settled by the rules: nothing to do.
            assert source(base, "short@x.org") == (200, {"state": "deterministic"})
        finally:
            server.shutdown()
    print("ok: trim-source offers the very lines the ingest trimmed, with its evidence")


def test_a_mail_too_long_to_hand_over_is_settled_by_asking():
    with tempfile.TemporaryDirectory() as tmp:
        gw, _mirror, _box, base, server = rig(tmp)
        try:
            before = snapshot(gw, "huge@x.org")
            text = t.by_id(gw)["huge@x.org"]["text"]
            assert len(text) > gw._trim.MODEL_SOURCE_MAX_CHARS
            assert source(base, "huge@x.org") == (
                200, {"state": "deterministic", "settled": "too_long"})
            record = t.by_id(gw)["huge@x.org"]
            assert record["trim"] == "deterministic" and record["text"] == text
            unchanged_but_reading(gw, "huge@x.org", before)
            assert source(base, "huge@x.org") == (200, {"state": "deterministic"})
            # A result for it is about nothing that was ever handed out.
            assert result(base, "huge@x.org", "0" * 64, keep=[[1, 1]])[0] == 409
            assert t.by_id(gw)["huge@x.org"]["text"] == text
        finally:
            server.shutdown()
    print("ok: a mail too long for a model is settled deterministic when asked, text unchanged")


def test_a_choice_becomes_the_text_and_settles_the_mail():
    with tempfile.TemporaryDirectory() as tmp:
        gw, _mirror, _box, base, server = rig(tmp)
        try:
            mid = "long@x.org"
            before = snapshot(gw, mid)
            got = source(base, mid)[1]
            lines, digest = got["lines"], got["digest"]
            keep = [[1, 1], [5, 7]]
            expected = gw._trim.apply_keep(lines, keep)
            status, answer = result(base, mid, digest, keep=keep)
            assert (status, answer) == (200, {"state": "model", "text": expected}), answer
            record = t.by_id(gw)[mid]
            assert record["trim"] == "model" and record["text"] == expected
            unchanged_but_reading(gw, mid, before)
            # Every line of it is one the gateway handed out.
            assert all(line == "" or line in lines for line in expected.split("\n"))
            # Settled: nothing more to ask, and no second answer taken.
            assert source(base, mid) == (200, {"state": "model"})
            assert result(base, mid, digest, keep=[[1, 2]]) == (
                409, {"error": "stale", "state": "model"})
            assert result(base, mid, digest, failed="unparseable")[0] == 409
            assert t.by_id(gw)[mid]["text"] == expected
            unchanged_but_reading(gw, mid, before)
        finally:
            server.shutdown()
    print("ok: a valid choice is the record's text, kb:trim model, and settles it")


def test_what_is_no_choice_settles_the_mail_as_the_rules_left_it():
    with tempfile.TemporaryDirectory() as tmp:
        gw, mirror, box, base, server = rig(tmp)
        try:
            cases = [
                ("long@x.org", {"keep": [[0, 1]]}),
                ("html@x.org", {"keep": [[2, 1]]}),
                ("reply@x.org", {"keep": "everything"}),
                ("bulk@x.org", {"failed": "unparseable"}),
            ]
            for mid, answer_of_model in cases:
                before = snapshot(gw, mid)
                text = t.by_id(gw)[mid]["text"]
                digest = source(base, mid)[1]["digest"]
                status, answer = result(base, mid, digest, **answer_of_model)
                assert (status, answer) == (200, {"state": "deterministic", "text": text}), (
                    mid, status, answer)
                record = t.by_id(gw)[mid]
                assert record["trim"] == "deterministic" and record["text"] == text
                unchanged_but_reading(gw, mid, before)
                assert source(base, mid) == (200, {"state": "deterministic"})
            # More ranges than a choice may name, a keep of nulls.
            box.put("INBOX", msg("more@x.org", subject="More", body=LONG_BODY, day=7))
            box.put("INBOX", msg("null@x.org", subject="Null", body=LONG_BODY, day=8))
            box.put("INBOX", msg("gave@x.org", subject="Gave", body=LONG_BODY, day=9))
            mirror.poll()
            # The web gateway's word that the mail had all its turns and none
            # of them came to a judgement: settled as any failed answer is.
            text = t.by_id(gw)["gave@x.org"]["text"]
            digest = source(base, "gave@x.org")[1]["digest"]
            assert result(base, "gave@x.org", digest, failed="gave_up") == (
                200, {"state": "deterministic", "text": text})
            assert t.by_id(gw)["gave@x.org"]["trim"] == "deterministic"
            for mid, keep in (("more@x.org", [[i, i] for i in range(1, 202)]),
                              ("null@x.org", None)):
                digest = source(base, mid)[1]["digest"]
                assert result(base, mid, digest, keep=keep)[1]["state"] == "deterministic"
        finally:
            server.shutdown()
    print("ok: an invalid choice and a failed answer settle deterministic, text unchanged")


def test_a_stale_digest_is_refused_and_nothing_changes():
    with tempfile.TemporaryDirectory() as tmp:
        gw, _mirror, _box, base, server = rig(tmp)
        try:
            mid = "long@x.org"
            before = snapshot(gw, mid)
            text = t.by_id(gw)[mid]["text"]
            other = source(base, "html@x.org")[1]["digest"]
            for digest in ("0" * 64, other, "not hex", "é" * 64):
                assert result(base, mid, digest, keep=[[1, 1]]) == (
                    409, {"error": "stale", "state": "wanting"}), digest
                assert result(base, mid, digest, failed="unparseable")[0] == 409
            record = t.by_id(gw)[mid]
            assert record["trim"] == "wanting" and record["text"] == text
            unchanged_but_reading(gw, mid, before)
        finally:
            server.shutdown()
    print("ok: a result for another source than the mail's is refused 409 stale")


def test_bodies_that_are_no_call():
    with tempfile.TemporaryDirectory() as tmp:
        gw, _mirror, _box, base, server = rig(tmp)
        try:
            mid = "long@x.org"
            before = snapshot(gw, mid)
            digest = source(base, mid)[1]["digest"]
            for path in ("/messages/trim-source", "/messages/trim-result"):
                # The token, as on every endpoint.
                assert post(base, path, {"chat": mid, "account": ACCOUNT,
                                         "message_id": mid}, token=None)[0] == 401
                assert post(base, path, {}, token="wrong")[0] == 401
                for body in (b"[]", b"not json", b"", {}, {"chat": mid, "account": ACCOUNT},
                             {"message_id": mid, "account": ACCOUNT},
                             {"chat": 1, "message_id": mid, "account": ACCOUNT},
                             {"chat": mid, "message_id": ["x"], "account": ACCOUNT},
                             {"chat": " ", "message_id": mid, "account": ACCOUNT}):
                    assert post(base, path, body)[0] == 400, (path, body)
                # Oversized: refused before anything is read.
                assert post(base, path, b" " * (gw.MAX_BODY_BYTES + 1))[0] == 400
            base_body = {"chat": mid, "account": ACCOUNT, "message_id": mid}
            for extra in ({}, {"keep": [[1, 1]]}, {"digest": 7, "keep": [[1, 1]]},
                          {"digest": "", "keep": [[1, 1]]},
                          {"digest": digest},
                          {"digest": digest, "keep": [[1, 1]], "failed": "x"},
                          {"digest": digest, "failed": ""}, {"digest": digest, "failed": 3},
                          {"digest": digest, "failed": "   "}):
                assert post(base, "/messages/trim-result", {**base_body, **extra})[0] == 400, extra
            # Pairs by the thousand do not fit in a body (a million would
            # not even be read: the answer comes before the body is sent).
            huge = json.dumps({**base_body, "digest": digest,
                               "keep": [[1, 1]] * (gw.MAX_BODY_BYTES // 8 + 1)}).encode()
            assert len(huge) > gw.MAX_BODY_BYTES
            assert post(base, "/messages/trim-result", huge)[0] == 400
            record = t.by_id(gw)[mid]
            assert record["trim"] == "wanting"
            unchanged_but_reading(gw, mid, before)
            gw.MIRROR = None
            assert source(base, mid)[0] == 503
            assert result(base, mid, digest, keep=[[1, 1]])[0] == 503
        finally:
            server.shutdown()
    print("ok: a body that names no mail, or no one answer, is refused before anything is read")


def test_only_this_accounts_mail_and_only_under_its_chat():
    with tempfile.TemporaryDirectory() as tmp:
        gw, _mirror, _box, base, server = rig(tmp)
        try:
            mid = "long@x.org"
            before = snapshot(gw, mid)
            digest = source(base, mid)[1]["digest"]
            for account in ("other@example.org", "", None):
                assert source(base, mid, account=account) == (
                    404, {"error": "not this gateway's account"}), account
                assert result(base, mid, digest, account=account, keep=[[1, 1]])[0] == 404
            # The account is the address, however it is cased.
            assert source(base, mid, account="Me@Example.ORG")[0] == 200
            # No such mail; a mail named under another thread's chat, or one
            # that is no chat at all.
            assert source(base, "nobody@x.org") == (404, {"error": "not found"})
            assert source(base, mid, chat="short@x.org") == (404, {"error": "not found"})
            assert source(base, mid, chat="nothing@x.org")[0] == 404
            assert result(base, mid, digest, chat="short@x.org", keep=[[1, 1]])[0] == 404
            assert result(base, "nobody@x.org", digest, keep=[[1, 1]])[0] == 404
            # Bracketed, as a header has it: the same mail.
            assert source(base, "<long@x.org>", chat=mid)[0] == 200
            assert t.by_id(gw)[mid]["trim"] == "wanting"
            unchanged_but_reading(gw, mid, before)
        finally:
            server.shutdown()
    print("ok: another account's mail, an unknown one, one under the wrong chat: 404")


def test_a_merged_chat_lands_on_the_right_record():
    with tempfile.TemporaryDirectory() as tmp:
        gw, mirror, box, base, server = rig(tmp)
        try:
            # A reply to a mail never mirrored: a chat of its own, keyed by
            # that parent's id.
            box.put("INBOX", msg("b@x.org", subject="Re: Offer", body=LONG_BODY,
                                 irt="lost@x.org", day=7))
            box.put("INBOX", msg("a@x.org", subject="Offer", day=7))
            mirror.poll()
            assert t.by_id(gw)["b@x.org"]["chat"] == "lost@x.org"
            before = snapshot(gw, "b@x.org")
            got = source(base, "b@x.org", chat="lost@x.org")[1]
            assert got["state"] == "wanting"
            # Then a mail that cites both: the two chats are one, under a@x.org.
            box.put("INBOX", msg("c@x.org", subject="Re: Offer", refs=["a@x.org", "lost@x.org"],
                                 irt="lost@x.org", day=8))
            mirror.poll()
            assert t.by_id(gw)["b@x.org"]["chat"] == "a@x.org"
            # Named under the chat it was in, or the one it is in now: the same record.
            assert source(base, "b@x.org", chat="lost@x.org")[1] == got
            assert source(base, "b@x.org", chat="a@x.org")[1] == got
            status, answer = result(base, "b@x.org", got["digest"], chat="lost@x.org",
                                    keep=[[1, 3]])
            assert status == 200 and answer["state"] == "model", answer
            record = t.by_id(gw)["b@x.org"]
            assert record["chat"] == "a@x.org" and record["trim"] == "model"
            assert record["text"] == gw._trim.apply_keep(got["lines"], [[1, 3]])
            assert len([r for r in t.records(gw) if r["message_id"] == "b@x.org"]) == 1
            blobs, rest = before
            assert snapshot(gw, "b@x.org") == (blobs, {**rest, "chat": "a@x.org"})
            # But not under a chat it never was in.
            assert source(base, "b@x.org", chat="short@x.org")[0] == 404
        finally:
            server.shutdown()
    print("ok: a mail merged into another chat is still the one record a result lands on")


def test_a_mail_gone_from_the_mailbox_is_410():
    with tempfile.TemporaryDirectory() as tmp:
        gw, mirror, box, base, server = rig(tmp)
        try:
            # Gone before anything was asked.
            box.remove("INBOX", box.uid_of("INBOX", "html@x.org"))
            mirror.poll()
            assert t.by_id(gw)["html@x.org"]["removed_at"]
            assert source(base, "html@x.org")[0] == 410
            assert result(base, "html@x.org", "0" * 64, keep=[[1, 1]])[0] == 410
            # Gone between the two calls: refused, and no word put back.
            got = source(base, "long@x.org")[1]
            box.remove("INBOX", box.uid_of("INBOX", "long@x.org"))
            mirror.poll()
            gone = t.by_id(gw)["long@x.org"]
            assert gone["removed_at"] and gone["text"] == ""
            assert result(base, "long@x.org", got["digest"], keep=[[1, 1]]) == (
                410, {"error": "the mail is gone from the mailbox"})
            assert result(base, "long@x.org", got["digest"], failed="unparseable")[0] == 410
            after = t.by_id(gw)["long@x.org"]
            assert after["text"] == "" and after["body_part"] is None
            assert after["trim"] == gone["trim"]
        finally:
            server.shutdown()
    print("ok: a removed mail answers 410, before or between the calls, and nothing is written")


def test_settled_meanwhile_and_an_unreadable_part():
    with tempfile.TemporaryDirectory() as tmp:
        gw, _mirror, _box, base, server = rig(tmp)
        try:
            # Two turns for one mail (a restart, a retry): the first settles
            # it, the second is stale.
            got = source(base, "long@x.org")[1]
            assert result(base, "long@x.org", got["digest"], keep=[[1, 1]])[0] == 200
            text = t.by_id(gw)["long@x.org"]["text"]
            assert result(base, "long@x.org", got["digest"], keep=[[1, 5]]) == (
                409, {"error": "stale", "state": "model"})
            assert t.by_id(gw)["long@x.org"]["text"] == text
            # A part that cannot be read says nothing of the mail: 503, and
            # it stays wanting.
            record = t.by_id(gw)["html@x.org"]
            media_id = gw._ibstore.media_id_of(record["html_part"])
            # The blob itself, not one of its sidecars (.type, .name, .meta).
            blob_path = gw._ibstore.media_dir(gw.INBOUND_STORE_DIR) / media_id
            held = blob_path.read_bytes()
            blob_path.unlink()
            try:
                assert source(base, "html@x.org")[0] == 503
                assert result(base, "html@x.org", "0" * 64, keep=[[1, 1]])[0] == 503
                assert t.by_id(gw)["html@x.org"]["trim"] == "wanting"
            finally:
                blob_path.write_bytes(held)
            assert source(base, "html@x.org")[1]["state"] == "wanting"
        finally:
            server.shutdown()
    print("ok: a mail settled meanwhile is stale; an unreadable part settles nothing")


def rewrite_body(gw, mid, data: bytes) -> None:
    """The record written anew from another plain part (as a placeholder read
    again is), keeping its file, its id and its state."""
    path = gw.MIRROR._record_path(gw.MIRROR.index.one(
        "SELECT record FROM mails WHERE message_id = ?", (mid,))["record"])
    fields = gw._ibstore.read_record(path)
    media_id = gw._ibstore.store_media(gw.INBOUND_STORE_DIR, data, "text/plain; charset=utf-8")
    fields["body_part"] = gw._MEDIA_REF + media_id
    gw._ibstore._atomic_write(gw._ibstore._render(fields), path)


def test_a_record_written_anew_meanwhile_is_stale():
    with tempfile.TemporaryDirectory() as tmp:
        gw, mirror, _box, base, server = rig(tmp)
        try:
            # Other parts, another source: the digest handed out names none.
            got = source(base, "long@x.org")[1]
            rewrite_body(gw, "long@x.org", (LONG_BODY + "\nOne more line.\n").encode())
            assert result(base, "long@x.org", got["digest"], keep=[[1, 1]]) == (
                409, {"error": "stale", "state": "wanting"})
            assert t.by_id(gw)["long@x.org"]["trim"] == "wanting"
            # The same text under another blob, written between the look and
            # the write: the digest still matches, the parts on record do
            # not — refused under the lock, where it counts.
            got = source(base, "html@x.org")[1]
            same = t.blob(gw, t.by_id(gw)["html@x.org"]["body_part"] or
                          t.by_id(gw)["html@x.org"]["html_part"])
            real = gw.Mirror._trim_source_of

            def racing(fields, evidence):
                answer = real(fields, evidence)
                mirror._trim_source_of = real
                path = mirror._record_path(mirror.index.one(
                    "SELECT record FROM mails WHERE message_id = ?", ("html@x.org",))["record"])
                record = gw._ibstore.read_record(path)
                record["html_part"] = gw._MEDIA_REF + gw._ibstore.store_media(
                    gw.INBOUND_STORE_DIR, same, "text/html; charset=utf-8")
                gw._ibstore._atomic_write(gw._ibstore._render(record), path)
                return answer
            mirror._trim_source_of = racing
            assert result(base, "html@x.org", got["digest"], keep=[[1, 1]]) == (
                409, {"error": "stale", "state": "wanting"})
            assert t.by_id(gw)["html@x.org"]["trim"] == "wanting"
            # Asked again, it is that record's own source — the same lines.
            again = source(base, "html@x.org")[1]
            assert again["digest"] == got["digest"]
            assert result(base, "html@x.org", again["digest"], keep=[[1, 1]])[0] == 200
        finally:
            server.shutdown()
    print("ok: a record written anew from other parts meanwhile refuses the result, 409")


def test_a_pass_that_holds_the_lock_does_not_make_it_wait():
    """A pass over the mailbox holds the mirror's lock for as long as it
    takes — minutes on a first sync. A model's judgement is not made to wait
    it out (it was paid for): its one write takes the record lock, which a
    writer holds around one record's write and no longer."""
    with tempfile.TemporaryDirectory() as tmp:
        gw, mirror, _box, base, server = rig(tmp)
        try:
            mid = "long@x.org"
            before = snapshot(gw, mid)
            got = source(base, mid)[1]
            held, release = threading.Event(), threading.Event()

            def hold(lock):
                with lock:
                    held.set()
                    release.wait(10)
            worker = threading.Thread(target=hold, args=(mirror._lock,))
            worker.start()
            held.wait(5)
            try:
                assert source(base, mid)[1] == got
                status, answer = result(base, mid, got["digest"], keep=[[1, 1]])
                assert (status, answer["state"]) == (200, "model"), (status, answer)
                assert source(base, "huge@x.org")[1]["settled"] == "too_long"
            finally:
                release.set()
                worker.join()
            assert t.by_id(gw)[mid]["trim"] == "model"
            assert t.by_id(gw)["huge@x.org"]["trim"] == "deterministic"
            unchanged_but_reading(gw, mid, before)
            # A record write in flight is waited for, briefly, then 503 — and
            # nothing is written.
            mid = "html@x.org"
            got = source(base, mid)[1]
            held.clear()
            release.clear()
            worker = threading.Thread(target=hold, args=(mirror._records,))
            worker.start()
            held.wait(5)
            try:
                status, answer = result(base, mid, got["digest"], keep=[[1, 1]])
                assert status == 503 and "try again" in answer["error"], answer
            finally:
                release.set()
                worker.join()
            assert t.by_id(gw)[mid]["trim"] == "wanting"
            assert result(base, mid, got["digest"], keep=[[1, 1]])[0] == 200
        finally:
            server.shutdown()
    print("ok: a pass holding the mirror's lock does not hold up a result; a record write does")


def test_a_pass_writes_records_under_the_record_lock():
    """What lets the trim's write go past a pass: every record write of the
    pass takes the record lock too, so the trim's check-then-write under it
    is never interleaved with one (a tombstone, a merge, a record written
    anew could otherwise be undone by the trim's rewrite)."""
    with tempfile.TemporaryDirectory() as tmp:
        gw, mirror, box, base, server = rig(tmp)
        try:
            box.put("INBOX", msg("late@x.org", subject="Late", body="Soon.\n", day=9))
            held, release = threading.Event(), threading.Event()

            def hold():
                with mirror._records:
                    held.set()
                    release.wait(10)
            holder = threading.Thread(target=hold)
            holder.start()
            held.wait(5)
            passing = threading.Thread(target=mirror.poll)
            try:
                passing.start()
                passing.join(1.0)
                assert passing.is_alive(), "the pass wrote a record past the record lock"
                assert "late@x.org" not in t.by_id(gw)
            finally:
                release.set()
                holder.join()
                passing.join(10)
            assert "late@x.org" in t.by_id(gw)
        finally:
            server.shutdown()
    print("ok: a pass's record writes wait for the record lock")


def raw_mail(mid, subject, head_extra: bytes, body: str) -> bytes:
    return (b"From: Ann Example <ann@x.org>\r\nTo: me@example.org\r\n"
            b"Subject: " + subject.encode() + b"\r\nMessage-ID: <" + mid.encode() + b">\r\n"
            b"Date: Sat, 04 Oct 2025 10:00:00 +0000\r\n" + head_extra
            + b"\r\n" + body.replace("\n", "\r\n").encode())


def test_the_evidence_is_what_the_ingest_read():
    """The bulk evidence is read again off the stored header block, which
    was stored with its CRLFs folded to LF. A CR CR LF ended the block for
    the ingest's parser (List-Unsubscribe after it was body, not a header);
    read again unfolded, it is a line end and the header block goes on — the
    source would be cut as bulk mail, without the footer its bubble shows.
    Folded back, the evidence is the ingest's and the lines are the text."""
    footer = ("\nYou are receiving this because you subscribed.\n"
              "Unsubscribe: https://lists.example.org/unsubscribe?u=1\n")
    with tempfile.TemporaryDirectory() as tmp:
        gw, mirror, box, base, server = rig(tmp)
        try:
            box.put("INBOX", raw_mail(
                "crcr@x.org", "Digest",
                b"X-A: b\r\r\nList-Unsubscribe: <https://lists.example.org/u>\r\n",
                LONG_BODY + footer))
            mirror.poll()
            record = t.by_id(gw)["crcr@x.org"]
            assert record["trim"] == "wanting" and "Unsubscribe:" in record["text"]
            status, answer = source(base, "crcr@x.org")
            assert status == 200 and answer["state"] == "wanting", answer
            assert answer["bulk"] is False, answer
            assert "\n".join(answer["lines"]) == record["text"]
            assert any("Unsubscribe:" in line for line in answer["lines"])
        finally:
            server.shutdown()
    print("ok: the bulk evidence read again is the evidence the ingest read")


def test_an_unreadable_header_part_is_503():
    """The header part is a part like the bodies: one that cannot be read
    says nothing of the mail. Read as "not bulk" instead, a mail bulk by its
    List-Unsubscribe alone would be offered the footer the rules had cut."""
    with tempfile.TemporaryDirectory() as tmp:
        gw, _mirror, _box, base, server = rig(tmp)
        try:
            mid = "bulk@x.org"
            got = source(base, mid)[1]
            assert got["bulk"] is True
            media_id = gw._ibstore.media_id_of(t.by_id(gw)[mid]["header_part"])
            blob_path = gw._ibstore.media_dir(gw.INBOUND_STORE_DIR) / media_id
            held = blob_path.read_bytes()
            blob_path.unlink()
            try:
                assert source(base, mid) == (503, {"error": "a part of the mail could not be read"})
                assert result(base, mid, got["digest"], keep=[[1, 1]])[0] == 503
                assert t.by_id(gw)[mid]["trim"] == "wanting"
            finally:
                blob_path.write_bytes(held)
            assert source(base, mid)[1] == got
        finally:
            server.shutdown()
    print("ok: an unreadable header part is 503 and settles nothing")


def set_text(gw, mirror, mid, text) -> None:
    """The record's kb:text as another version of the rules left it."""
    path = mirror._record_path(mirror.index.one(
        "SELECT record FROM mails WHERE message_id = ?", (mid,))["record"])
    fields = gw._ibstore.read_record(path)
    fields["text"] = text
    gw._ibstore._atomic_write(gw._ibstore._render(fields), path)


def test_a_source_that_is_not_the_records_text_is_never_handed_out():
    """The model chooses from what the bubble shows. Where the text rebuilt
    from the parts is not the record's kb:text — the rules have changed since
    the mail was read, or its evidence reads otherwise now — the lines could
    hold what the rules had cut, and a choice of them would bring it back:
    the mail is settled as it stands, its text unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        gw, mirror, _box, base, server = rig(tmp)
        try:
            # As another version of the rules left it: the history it now cuts.
            mid = "reply@x.org"
            got = source(base, mid)[1]
            older = "\n".join(got["lines"]) + "\n\n-----Original Message-----\nFrom: Me"
            set_text(gw, mirror, mid, older)
            before = snapshot(gw, mid)
            # A judgement on the lines handed out before is about no text the
            # record has: refused, nothing written.
            assert result(base, mid, got["digest"], keep=[[1, 1]]) == (
                409, {"error": "stale", "state": "wanting"})
            assert t.by_id(gw)[mid]["text"] == older
            assert source(base, mid) == (
                200, {"state": "deterministic", "settled": "source_differs"})
            record = t.by_id(gw)[mid]
            assert record["trim"] == "deterministic" and record["text"] == older
            unchanged_but_reading(gw, mid, before)
            assert source(base, mid) == (200, {"state": "deterministic"})
            # The other way: a record whose text the rules of today would not
            # cut so far — lines the bubble never had are not offered either.
            mid = "long@x.org"
            lines = source(base, mid)[1]["lines"]
            set_text(gw, mirror, mid, "\n".join(lines[:3]))
            assert source(base, mid)[1].get("settled") == "source_differs"
            assert t.by_id(gw)[mid]["text"] == "\n".join(lines[:3])
            # A record capped at ingest is compared as the ingest capped it.
            mid = "bulk@x.org"
            got = source(base, mid)[1]
            saved = gw.MAX_RECORD_TEXT_CHARS
            gw.MAX_RECORD_TEXT_CHARS = 300
            try:
                set_text(gw, mirror, mid, "\n".join(got["lines"])[:300].rstrip() + "\n…")
                assert source(base, mid)[1] == got
                set_text(gw, mirror, mid, "\n".join(got["lines"])[:200].rstrip() + "\n…")
                assert source(base, mid)[1].get("settled") == "source_differs"
            finally:
                gw.MAX_RECORD_TEXT_CHARS = saved
        finally:
            server.shutdown()
    print("ok: a source that is not the record's own text is settled, never handed out")


def test_a_failed_write_is_503_and_not_a_settled_mail():
    with tempfile.TemporaryDirectory() as tmp:
        gw, _mirror, _box, base, server = rig(tmp)
        real = gw._ibstore.update_message
        try:
            mid = "long@x.org"
            got = source(base, mid)[1]
            # The ledger volume will not take the rewrite: update_message
            # swallows it, the read-back sees it.
            gw._ibstore.update_message = lambda *a, **kw: None
            assert result(base, mid, got["digest"], keep=[[1, 1]])[0] == 503
            assert t.by_id(gw)[mid]["trim"] == "wanting"
        finally:
            gw._ibstore.update_message = real
            server.shutdown()
    print("ok: a rewrite that did not happen is 503, never a 200 for an unchanged record")


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
        print(f"\n{failures} mail gateway trim check(s) failed.")
        return 1
    print("\nAll mail gateway trim checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
