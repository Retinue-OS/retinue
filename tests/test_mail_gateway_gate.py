#!/usr/bin/env python3
"""The mail gateway as mail's delivery gate (scripts/mail-gateway.py;
docs/mail-chats.md, "The gate").

The messengers' inbound flow, with a mailbox behind it: a mail that arrives is
persisted ``delivered: false``, gated on its group and on its sender, and
offered to the chats rail with a handover — and it is delivered only by a real
acceptance. This pins every part of that against the fake mailbox and a fake
rail:

- every gate class: a VIP, an instructed sender, neither; a quieted and an
  ignored group; news, and news that is also ignored;
- every answer of the rail: accepted, accepted with a job (done, failed,
  never finishing), no acceptance, a refusal, no answer in time, and a web
  gateway that is away;
- that an undelivered mail is offered again on later polls — bounded, the
  oldest first, backing off — across a restart and a lost state volume, since
  the ledger's flag is the queue;
- that a mail's files ride along only for a VIP or an instructed sender,
  within the rail's limits, and under no name of the sender's;
- the policy unreadable: open on the group axis, closed on the sender axis;
- the news rail: filed with the link the mail itself declares, consumed only
  where the group is also ignored and only once the feed has it, untouched and
  retried when the feed refuses;
- that old mail stays silent and delivered, that a mail gone or merged while
  owed is not offered under a stale chat or after it is gone — and that
  nothing is ever forwarded to triage.

Uses the fake mailbox of tests/test_mail_gateway.py.

    python3 tests/test_mail_gateway_gate.py
"""
import base64
import importlib.util
import json
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("mail_gateway_helpers",
                                               HERE / "test_mail_gateway.py")
t = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t)

msg, NOW, DAY, T0, ME, ACCOUNT = t.msg, t.NOW, t.DAY, t.T0, t.ME, t.ACCOUNT
OPEN = {"forward": True, "vip": False, "instructed": False, "reason": "open"}
HELD = {"forward": False, "vip": False, "instructed": False, "reason": "mirror"}


def begin(gw, box=None, start=NOW):
    """A mirror that has seen its (still empty) folders once, on a clock the
    test moves: ``(mirror, box, clock)`` with ``clock[0]`` the time."""
    box = box or t.FakeMailbox(gw)
    clock = [start]
    mirror = gw.Mirror(box, clock=lambda: clock[0])
    assert mirror.poll() is False
    return mirror, box, clock


def again(gw, box, clock):
    """The process restarted: a new mirror over the same volumes."""
    return gw.Mirror(box, clock=lambda: clock[0])


def flags(gw, **sets):
    """Write the e-mail group flags (news / quieted / ignored)."""
    t._write_policy(gw, **sets)


def senders(gw, vip=(), instructed=()):
    """Write the ``email`` channel's sender policy, as the address book's
    projection does: the handles of VIP and of instructed people."""
    tp = gw._triage
    policy = tp.load_messenger_policy("email", path=gw.SENDER_POLICY_PATH)
    gw.SENDER_POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    gw.SENDER_POLICY_PATH.write_text(tp.render_messenger_policy("email", policy._replace(
        vip_contacts=frozenset(vip), instructed_contacts=frozenset(instructed))),
        encoding="utf-8")


def offers(mirror) -> dict:
    return {row["message_id"]: dict(row) for row in
            mirror.index.rows("SELECT * FROM offers")}


def offered(events) -> list:
    """The ids of the mails offered with a handover, in order."""
    return [p["message_id"] for kind, p in events if kind == "inbound" and p.get("handover")]


def held_in(box, folder) -> list:
    return [t.email.message_from_bytes(mail["raw"]).get("Message-ID").strip("<>")
            for _uid, mail in sorted(box.boxes[folder]["mails"].items())]


def seen(box, folder, message_id) -> bool:
    return "\\Seen" in box.boxes[folder]["mails"][box.uid_of(folder, message_id)]["flags"]


class FakeJobs:
    """Stands in for job_delivery.confirm_delivery: the jobs asked for, each
    a thread-like the test ends by hand (``done`` runs the gateway's own
    callback, as a job that reported done does; ``fail`` just ends it)."""

    class Job:
        def __init__(self, url, on_done, options):
            self.url, self.on_done, self.options, self.alive = url, on_done, options, True

        def is_alive(self):
            return self.alive

        def done(self):
            self.alive = False
            self.on_done()

        def fail(self):
            self.alive = False

    def __init__(self, gw):
        self.asked = []
        self.gw, self.real = gw, gw._jobs.confirm_delivery
        gw._jobs.confirm_delivery = self.confirm

    def confirm(self, url, on_done, **options):
        self.asked.append(self.Job(url, on_done, options))
        return self.asked[-1]

    def restore(self):
        # job_delivery is imported once per process, like chat_ingest.
        self.gw._jobs.confirm_delivery = self.real


# --------------------------------------------------------------------------
# Persist, gate, offer
# --------------------------------------------------------------------------

def test_a_mail_that_arrives_is_persisted_undelivered_and_delivered_by_acceptance():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        state = {"answer": "down"}
        events = t.rail(gw, lambda kind, payload: state["answer"])
        mirror, box, clock = begin(gw)
        gw.MIRROR = mirror
        box.put("INBOX", msg("a@x.org", day=39))
        mirror.poll()
        # Persisted before anything is decided — and owed: the web gateway is
        # away, and no failure after the write can lose the mail.
        record = t.by_id(gw)["a@x.org"]
        assert record["delivered"] is False and record["text"]
        assert offers(mirror)["a@x.org"]["queued"] == 1
        assert gw._health_snapshot()["waiting"] == {"rail": 1, "since": T0 + 39 * DAY, "news": 0}
        assert offered(events) == ["a@x.org"]
        # The web gateway answers: the chat takes the mail, and only that
        # turns the record delivered.
        state["answer"] = "ok"
        mirror = gw.MIRROR = again(gw, box, clock)
        mirror.poll()
        assert offered(events) == ["a@x.org", "a@x.org"]
        event = events[-1][1]
        assert event["handover"] is True and event["gate"] == OPEN and "files" not in event
        assert event["direction"] == "in" and event["chat"] == "a@x.org"
        assert event["sender"] == "ann@x.org" and "backfill" not in event
        assert t.by_id(gw)["a@x.org"]["delivered"] is True
        assert offers(mirror) == {}
        assert gw._health_snapshot()["waiting"] == {"rail": 0, "since": None, "news": 0}
        # Delivered once: nothing offers it again.
        clock[0] += 10 * DAY
        mirror.poll()
        assert len(events) == 2
    print("ok: a mail is persisted undelivered, offered with a handover, and delivered "
          "by the rail's acceptance alone")


def test_every_gate_class():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        # The sender axis, as the address book projects it: exact addresses
        # and the wildcards a person's e-mail domain becomes.
        assert gw.SENDER_POLICY_PATH == Path(tmp) / "inbound" / "policy" / "policy.nt"
        senders(gw, vip={"boss@corp.example", "*@vip.example", "chief@corp.example"},
                instructed={"clerk@firm.example", "*@*.bills.example", "chief@corp.example"})
        # The group axis: a quieted list, an ignored sender.
        flags(gw, quieted={"dev.lists.x.org"}, ignored={"noise@spam.example"})
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        mails = {
            "plain@x.org": dict(sender="Ann <ann@x.org>"),
            "vip@x.org": dict(sender="The Boss <Boss@Corp.example>"),
            "vipdomain@x.org": dict(sender="anyone@vip.example"),
            "instructed@x.org": dict(sender="clerk@firm.example"),
            "instructeddomain@x.org": dict(sender="invoices@pay.bills.example"),
            "both@x.org": dict(sender="chief@corp.example"),
            "quieted@x.org": dict(sender="dee@x.org", list_id="Dev <dev.lists.x.org>"),
            "ignored@x.org": dict(sender="noise@spam.example"),
            # The two axes are independent: a quieted list does not stop a
            # VIP's turn, an ignored sender who is instructed is still worked.
            "vipquieted@x.org": dict(sender="boss@corp.example", list_id="<dev.lists.x.org>"),
        }
        for n, (mid, kwargs) in enumerate(mails.items()):
            box.put("INBOX", msg(mid, subject=f"Subject {n}", day=30, **kwargs))
        mirror.poll()
        gates = {p["message_id"]: p["gate"] for _k, p in events}

        def gate(forward, vip, instructed, reason):
            return {"forward": forward, "vip": vip, "instructed": instructed, "reason": reason}
        assert gates == {
            "plain@x.org": OPEN,
            "vip@x.org": gate(True, True, False, "vip"),
            "vipdomain@x.org": gate(True, True, False, "vip"),
            "instructed@x.org": gate(True, False, True, "instructed"),
            "instructeddomain@x.org": gate(True, False, True, "instructed"),
            "both@x.org": gate(True, True, True, "vip+instructed"),
            "quieted@x.org": gate(False, False, False, "group-quieted"),
            "ignored@x.org": gate(False, False, False, "group-ignored"),
            "vipquieted@x.org": gate(False, True, False, "vip+group-quieted"),
        }, gates
        # Every one of them is offered — held classes too: the chat shows
        # them all, silently — and every one the chat took is delivered.
        assert sorted(offered(events)) == sorted(mails)
        assert all(r["delivered"] is True for r in t.records(gw))
        assert offers(mirror) == {}
    print("ok: the gate — VIP, instructed, neither, by address and by domain; quieted, "
          "ignored; the two axes independent")


def test_the_gate_is_asked_when_the_offer_is_made():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        state = {"answer": "down"}
        events = t.rail(gw, lambda kind, payload: state["answer"])
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("a@x.org", sender="boss@corp.example", day=39))
        mirror.poll()
        assert events[-1][1]["gate"] == OPEN
        # While the mail waited, the user made its sender a VIP.
        senders(gw, vip={"boss@corp.example"})
        state["answer"] = "ok"
        clock[0] += 3600
        mirror.poll()
        assert events[-1][1]["gate"]["vip"] is True
    print("ok: a mail that waited is judged by the policy as it stands")


def test_files_ride_along_only_for_a_vip_or_an_instructed_sender():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp, MAIL_RAIL_FILE_BYTES=1000)
        senders(gw, vip={"boss@corp.example"}, instructed={"clerk@firm.example"})
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        pdf, big = b"%PDF-1.7 small", b"x" * 2000
        attached = [("Angebot März.PDF", "application/pdf", pdf),
                    ("huge.bin", "application/octet-stream", big),
                    ("a.tar.gz;rm -rf", "application/x-evil", b"data")]
        box.put("INBOX", msg("plain@x.org", sender="ann@x.org", files=attached, day=30))
        box.put("INBOX", msg("vip@x.org", sender="boss@corp.example", subject="B",
                             files=attached, day=31))
        box.put("INBOX", msg("clerk@x.org", sender="clerk@firm.example", subject="C",
                             files=attached[:1], day=32))
        mirror.poll()
        got = {p["message_id"]: p for _k, p in events}
        # Nobody's VIP, nobody's instruction: nothing travels. The chat shows
        # the attachments from the ledger either way.
        assert "files" not in got["plain@x.org"] and len(got["plain@x.org"]["attachments"]) == 3
        files = got["vip@x.org"]["files"]
        assert len(got["vip@x.org"]["attachments"]) == 3
        # Within the rail's limits: the file over the cap stays in the ledger.
        by_data = {base64.b64decode(f["data"]): f for f in files}
        assert set(by_data) == {pdf, b"data"} and len(files) == 2
        # Under no name of the sender's: a name of the gateway's own, and of
        # the sender's only a short plain extension — where it has one.
        assert sorted(f["filename"].split(".")[0] for f in files) == [
            "attachment-1", "attachment-2"]
        assert by_data[pdf]["filename"].endswith(".PDF")
        assert "." not in by_data[b"data"]["filename"]
        # The type as the ledger keeps it: one clean line.
        assert by_data[pdf]["content_type"] == "application/pdf"
        assert by_data[b"data"]["content_type"] == "application/x-evil"
        assert set(files[0]) == {"filename", "content_type", "data"}
        assert [f["filename"] for f in got["clerk@x.org"]["files"]] == ["attachment-1.PDF"]
        # The text parts are the mail, not files of it.
        record = t.by_id(gw)["vip@x.org"]
        assert record["body_part"] and len(files) < len(gw._ibstore._media_refs(record))
    with tempfile.TemporaryDirectory() as tmp:
        # So many files, of so much in all, an event carries.
        gw = t.load(tmp)
        gw.RAIL_FILES_MAX, gw.RAIL_FILES_BYTES = 3, 250
        senders(gw, vip={"boss@corp.example"})
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        box.put("INBOX", msg("many@x.org", sender="boss@corp.example", day=30, files=[
            (f"f{n}.txt", "text/plain", b"y" * 100) for n in range(6)]))
        mirror.poll()
        assert len(events[-1][1]["files"]) == 2 and len(events[-1][1]["attachments"]) == 6
    print("ok: a mail's files travel only for a VIP or an instructed sender, within the "
          "rail's limits, under a name of the gateway's own")


def test_an_unreadable_policy_is_open_for_the_chat_and_closed_for_turns():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        senders(gw, vip={"boss@corp.example"})
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        box.put("INBOX", msg("a@x.org", sender="boss@corp.example", day=30))
        mirror.poll()
        assert events[-1][1]["gate"]["vip"] is True
        # The sender policy cannot be read (a directory stands where the
        # file was): nobody is special — a mail must not buy a model turn
        # because a file could not be read — and the mail reaches its chat.
        gw.SENDER_POLICY_PATH.unlink()
        gw.SENDER_POLICY_PATH.mkdir()
        box.put("INBOX", msg("b@x.org", sender="boss@corp.example", subject="B", day=31,
                             files=[("f.pdf", "application/pdf", b"%PDF")]))
        mirror.poll()
        event = events[-1][1]
        assert event["gate"] == {"forward": True, "vip": False, "instructed": False,
                                 "reason": "policy-unreadable"}
        assert event["handover"] is True and "files" not in event
        assert t.by_id(gw)["b@x.org"]["delivered"] is True
        # A policy file that is not there is no failure: an empty policy.
        gw.SENDER_POLICY_PATH.rmdir()
        box.put("INBOX", msg("c@x.org", sender="boss@corp.example", subject="C", day=32))
        mirror.poll()
        assert events[-1][1]["gate"] == OPEN
    with tempfile.TemporaryDirectory() as tmp:
        # The group flags unreadable: no flags, as the mirror reads them —
        # the mail is offered like any other, and a VIP's still rings.
        gw = t.load(tmp)
        senders(gw, vip={"boss@corp.example"})
        flags(gw, ignored={"boss@corp.example"})
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        box.put("INBOX", msg("a@x.org", sender="boss@corp.example", day=30))
        mirror.poll()
        assert events[-1][1]["gate"] == {"forward": False, "vip": True, "instructed": False,
                                         "reason": "vip+group-ignored"}
        real = gw._triage.email_gate_decision
        gw._triage.email_gate_decision = lambda *a, **kw: 1 / 0
        try:
            box.put("INBOX", msg("b@x.org", sender="boss@corp.example", subject="B", day=31))
            mirror.poll()
        finally:
            gw._triage.email_gate_decision = real
        assert events[-1][1]["gate"] == {"forward": True, "vip": True, "instructed": False,
                                         "reason": "vip"}
    print("ok: the policy unreadable — open on the group axis, closed on the sender axis")


# --------------------------------------------------------------------------
# The rail's answers
# --------------------------------------------------------------------------

def test_every_answer_but_an_acceptance_leaves_the_mail_owed():
    answers = {
        "nothing": {},                                   # an older web gateway, a switch off
        "ok only": {"ok": True, "pushed": False},
        "a string for true": {"accepted": "true"},
        "a job with no acceptance": {"job_url": "/jobs/1"},
        "a job that is no path on the rail": {"accepted": True,
                                              "job_url": "http://evil.example/jobs/1"},
        "a job with no host of its own": {"accepted": True, "job_url": "//evil.example/j"},
    }
    for name, body in answers.items():
        with tempfile.TemporaryDirectory() as tmp:
            gw = t.load(tmp)
            jobs = FakeJobs(gw)
            try:
                state = {"body": body}
                events = t.rail(gw, answer=lambda payload: state["body"])
                mirror, box, clock = begin(gw)
                box.put("INBOX", msg("a@x.org", day=39))
                mirror.poll()
                assert t.by_id(gw)["a@x.org"]["delivered"] is False, name
                assert jobs.asked == [], name
                row = offers(mirror)["a@x.org"]
                assert (row["attempts"], row["queued"]) == (1, 0), name
                assert row["due"] == clock[0] + gw.RETRY_SECONDS, name
                # Its first offer is made; the mail holds nothing back, and
                # is not offered again before its wait is over.
                assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0, name
                mirror.poll()
                assert len(events) == 1, name
                # Then it is — and an acceptance delivers it.
                clock[0] += gw.RETRY_SECONDS
                state["body"] = {"accepted": True}
                mirror.poll()
                assert len(events) == 2 and events[-1][1]["handover"] is True, name
                assert t.by_id(gw)["a@x.org"]["delivered"] is True and offers(mirror) == {}, name
            finally:
                jobs.restore()
    print("ok: no acceptance — in any shape — leaves a mail undelivered and offered again")


def test_a_refusal_no_answer_in_time_and_a_web_gateway_that_is_away():
    with tempfile.TemporaryDirectory() as tmp:
        # Refused as malformed: the mail is not dropped with the event.
        gw = t.load(tmp)
        state = {"answer": "drop"}
        events = t.rail(gw, lambda kind, payload: state["answer"])
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("a@x.org", day=39))
        mirror.poll()
        assert t.by_id(gw)["a@x.org"]["delivered"] is False
        assert offers(mirror)["a@x.org"]["attempts"] == 1
        clock[0] += gw.RETRY_SECONDS
        state["answer"] = "ok"
        mirror.poll()
        assert t.by_id(gw)["a@x.org"]["delivered"] is True and len(events) == 2
    for away in ("uncertain", "down"):
        with tempfile.TemporaryDirectory() as tmp:
            # No answer in time — whether the event landed is not known — and
            # no answer at all. Neither is a yes: the mail stays undelivered,
            # keeps its place in the queue, and holds what is behind it, so
            # the order stands.
            gw = t.load(tmp)
            state = {"answer": away}
            events = t.rail(gw, lambda kind, payload: state["answer"])
            mirror, box, clock = begin(gw)
            box.put("INBOX", msg("a@x.org", day=38))
            box.put("INBOX", msg("b@x.org", subject="Other", day=39))
            mirror.poll()
            assert offered(events) == ["a@x.org"], (away, "one try, not one per mail")
            assert [r["delivered"] for r in t.records(gw)] == [False, False], away
            assert {r["attempts"] for r in offers(mirror).values()} == {0}, away
            assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 2, away
            # Backing off while it is away: nothing is tried within the wait,
            # and the wait grows.
            mirror.poll()
            clock[0] += gw.RAIL_RETRY_SECONDS - 1
            mirror.poll()
            assert len(events) == 1, away
            clock[0] += 1
            mirror.poll()
            assert len(events) == 2, away
            clock[0] += gw.RAIL_RETRY_SECONDS
            mirror.poll()
            assert len(events) == 2, (away, "the second wait is twice the first")
            clock[0] += gw.RAIL_RETRY_SECONDS
            mirror.poll()
            assert len(events) == 3 and mirror._rail_down == 3, away
            # It answers again: both mails, in order, each once.
            clock[0] += gw.RAIL_RETRY_MAX_SECONDS
            state["answer"] = "ok"
            events.clear()
            mirror.poll()
            assert offered(events) == ["a@x.org", "b@x.org"], away
            assert [r["delivered"] for r in t.records(gw)] == [True, True], away
            assert mirror._rail_down == 0 and offers(mirror) == {}, away
    print("ok: a refusal, no answer in time and no answer at all leave the mail owed, "
          "in its place, and the gateway backs off")


def test_a_turns_job_decides_the_delivery():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        senders(gw, vip={"boss@corp.example"}, instructed={"clerk@firm.example"})
        jobs = FakeJobs(gw)
        try:
            state = {"body": {"accepted": True, "job_url": "/jobs/7"}}
            events = t.rail(gw, answer=lambda payload: state["body"])
            mirror, box, clock = begin(gw)
            gw.MIRROR = mirror
            box.put("INBOX", msg("done@x.org", sender="boss@corp.example", day=37))
            box.put("INBOX", msg("failed@x.org", sender="clerk@firm.example", subject="F",
                                 day=38))
            box.put("INBOX", msg("never@x.org", sender="boss@corp.example", subject="N",
                                 day=39))
            mirror.poll()
            # Accepted, and not delivered: a turn is working each of them.
            assert [r["delivered"] for r in t.records(gw)] == [False, False, False]
            done, failed, never = jobs.asked
            # The job is polled where the rail is, for as long as the
            # deployment says.
            assert done.url == "http://rail.invalid/jobs/7"
            assert done.options["timeout"] == gw.MAIL_RAIL_JOB_TIMEOUT == 3600.0
            assert gw._health_snapshot()["waiting"]["rail"] == 3
            # While a job runs, its mail is not offered again, however long.
            clock[0] += 5 * gw.RETRY_MAX_SECONDS
            mirror.poll()
            assert len(events) == 3 and len(jobs.asked) == 3
            # Done: delivered.
            done.done()
            assert t.by_id(gw)["done@x.org"]["delivered"] is True
            assert sorted(offers(mirror)) == ["failed@x.org", "never@x.org"]
            # Failed (or expired, or timed out): the record stays as it is,
            # and the mail is offered again — to the rail, never to triage.
            failed.fail()
            assert t.by_id(gw)["failed@x.org"]["delivered"] is False
            state["body"] = {"accepted": True}
            clock[0] += gw.RETRY_MAX_SECONDS
            mirror.poll()
            assert offered(events)[3:] == ["failed@x.org"]
            assert t.by_id(gw)["failed@x.org"]["delivered"] is True
            # Never finishing: still owed, still not offered twice, and
            # /health counts it.
            clock[0] += 30 * DAY
            mirror.poll()
            assert len(events) == 4 and never.is_alive()
            assert t.by_id(gw)["never@x.org"]["delivered"] is False
            assert gw._health_snapshot()["waiting"]["rail"] == 1
            # A restart forgets the job, not the mail: once its wait is
            # over it is offered again, and the web gateway hands a repeat
            # its job back.
            state["body"] = {"accepted": True, "job_url": "/jobs/9"}
            mirror = gw.MIRROR = again(gw, box, clock)
            mirror.poll()
            assert len(events) == 4
            clock[0] += gw.RETRY_SECONDS
            mirror.poll()
            assert offered(events)[4:] == ["never@x.org"] and jobs.asked[-1].url.endswith("/jobs/9")
            jobs.asked[-1].done()
            assert t.by_id(gw)["never@x.org"]["delivered"] is True and offers(mirror) == {}
        finally:
            jobs.restore()
    with tempfile.TemporaryDirectory() as tmp:
        # And through job_delivery itself: the thread it starts waits for the
        # job and flips the flag under the mirror's lock.
        gw = t.load(tmp)
        real, polled = gw._jobs.await_job, []
        gw._jobs.await_job = lambda url, **kw: (polled.append(url), True)[1]
        try:
            t.rail(gw, answer={"accepted": True, "job_url": "/jobs/1"})
            mirror, box, _clock = begin(gw)
            box.put("INBOX", msg("a@x.org", day=39))
            mirror.poll()
            mirror._jobs["a@x.org"].join(10)
            assert polled == ["http://rail.invalid/jobs/1"]
            assert t.by_id(gw)["a@x.org"]["delivered"] is True and offers(mirror) == {}
            # A job that does not report done flips nothing.
            gw._jobs.await_job = lambda url, **kw: False
            box.put("INBOX", msg("b@x.org", subject="B", day=39))
            mirror.poll()
            mirror._jobs["b@x.org"].join(10)
            assert t.by_id(gw)["b@x.org"]["delivered"] is False
            assert offers(mirror)["b@x.org"]["attempts"] == 1
        finally:
            gw._jobs.await_job = real
    print("ok: a mail a turn works is delivered when its job is done — a job that fails "
          "or never finishes leaves it owed")


def test_a_record_that_cannot_be_marked_is_offered_again():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        events = t.rail(gw)
        mirror, box, clock = begin(gw)
        real = gw._ibstore.mark_delivered
        gw._ibstore.mark_delivered = lambda path: False     # the ledger volume is full
        try:
            box.put("INBOX", msg("a@x.org", day=39))
            mirror.poll()
        finally:
            gw._ibstore.mark_delivered = real
        # Accepted, but the flag did not flip: the flag is the queue.
        assert t.by_id(gw)["a@x.org"]["delivered"] is False and "a@x.org" in offers(mirror)
        clock[0] += gw.RETRY_SECONDS
        mirror.poll()
        assert len(events) == 2 and t.by_id(gw)["a@x.org"]["delivered"] is True
    print("ok: delivered is what the ledger says, not what the rail answered")


# --------------------------------------------------------------------------
# Offered again: across polls, a restart, a lost state volume
# --------------------------------------------------------------------------

def test_undelivered_mail_is_offered_again_across_polls_and_a_restart():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        state = {"body": {}}
        events = t.rail(gw, answer=lambda payload: (
            {"accepted": True} if payload["message_id"] == "taken@x.org" else state["body"]))
        mirror, box, clock = begin(gw)
        # UID order is not arrival order.
        for mid, day in (("m3@x.org", 39), ("m1@x.org", 37), ("m2@x.org", 38)):
            box.put("INBOX", msg(mid, subject=mid, day=day))
        mirror.poll()
        assert sorted(offered(events)) == ["m1@x.org", "m2@x.org", "m3@x.org"]
        assert {r["attempts"] for r in offers(mirror).values()} == {1}
        events.clear()
        # Bounded attempts per poll, the oldest mail first.
        gw.OFFER_BATCH = 2
        clock[0] += gw.RETRY_SECONDS
        mirror.poll()
        assert offered(events) == ["m1@x.org", "m2@x.org"]
        mirror.poll()
        assert offered(events) == ["m1@x.org", "m2@x.org", "m3@x.org"]
        # Each waits longer every time it is not taken, up to a limit.
        rows = offers(mirror)
        assert {r["attempts"] for r in rows.values()} == {2}
        assert rows["m1@x.org"]["due"] == clock[0] + 2 * gw.RETRY_SECONDS
        assert gw._retry_after(1) == gw.RETRY_SECONDS and gw._retry_after(3) == 4 * gw.RETRY_SECONDS
        assert gw._retry_after(50) == gw.RETRY_MAX_SECONDS
        events.clear()
        clock[0] += gw.RETRY_SECONDS
        mirror.poll()
        assert events == [], "not before its own wait is over"

        # A restart: the wait and the mails are on the state volume.
        mirror.index.close()
        mirror.threads.close()
        mirror = again(gw, box, clock)
        mirror.poll()
        assert events == [] and len(offers(mirror)) == 3
        clock[0] += gw.RETRY_SECONDS
        gw.OFFER_BATCH = 20
        mirror.poll()
        assert offered(events) == ["m1@x.org", "m2@x.org", "m3@x.org"]
        assert [r["delivered"] for r in t.records(gw)] == [False, False, False]

        # The state volume is lost: the ledger's flag is the queue. Every
        # undelivered mail is offered again, the oldest first — and nothing
        # that was delivered is.
        box.put("INBOX", msg("taken@x.org", subject="taken", day=39))
        clock[0] += gw.RETRY_MAX_SECONDS
        mirror.poll()
        assert t.by_id(gw)["taken@x.org"]["delivered"] is True
        mirror.index.close()
        mirror.threads.close()
        shutil.rmtree(Path(tmp) / "data")
        events.clear()
        state["body"] = {"accepted": True}
        mirror = again(gw, box, clock)
        assert sorted(offers(mirror)) == ["m1@x.org", "m2@x.org", "m3@x.org"]
        while mirror.poll():
            pass
        assert offered(events) == ["m1@x.org", "m2@x.org", "m3@x.org"]
        assert all(r["delivered"] is True for r in t.records(gw)) and offers(mirror) == {}
        assert len(t.records(gw)) == 4
    print("ok: an undelivered mail is offered again — bounded, oldest first, backing off — "
          "across a restart and a lost state volume")


def test_the_order_holds_while_the_web_gateway_is_away():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        state = {"answer": "down"}
        events = t.rail(gw, lambda kind, payload: state["answer"])
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("a@x.org", day=37))
        mirror.poll()
        # The user answers from a mail client; then the correspondent writes
        # again. The reply reads the chat up to it — heard before the mail it
        # answers, it would leave that mail unread for good.
        box.put("Sent", msg("s@example.org", sender=ME, to="ann@x.org",
                            subject="Re: Quarterly numbers", refs=["a@x.org"], irt="a@x.org",
                            day=38))
        mirror.poll()
        box.put("INBOX", msg("b@x.org", subject="Re: Quarterly numbers",
                             refs=["a@x.org", "s@example.org"], irt="s@example.org", day=39))
        mirror.poll()
        events.clear()
        state["answer"] = "ok"
        clock[0] += gw.RAIL_RETRY_MAX_SECONDS
        mirror.poll()
        assert [(p["message_id"], p["direction"], p.get("handover")) for _k, p in events] == [
            ("a@x.org", "in", True), ("s@example.org", "out", None), ("b@x.org", "in", True)]
    print("ok: offers and events leave in the order the mails came")


def test_a_stop_between_an_acceptance_and_its_note_is_put_right():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        events = t.rail(gw)
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("a@x.org", day=39))
        real_atomic = mirror.index.atomic

        def atomic(statements):
            # The process dies right after the record was marked delivered.
            if any(s and s[0].startswith("DELETE FROM offers") for s in statements):
                raise KeyboardInterrupt
            return real_atomic(statements)
        mirror.index.atomic = atomic
        try:
            mirror.poll()
            raise AssertionError("no stop")
        except KeyboardInterrupt:
            pass
        assert t.by_id(gw)["a@x.org"]["delivered"] is True and "a@x.org" in offers(mirror)
        mirror.index.atomic = real_atomic
        mirror = again(gw, box, clock)
        # The ledger says delivered: nothing is owed, and nothing offered.
        assert offers(mirror) == {}
        mirror.poll()
        assert len(events) == 1
        assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
    print("ok: the index's queue follows the ledger's flag after a stop")


# --------------------------------------------------------------------------
# What is never offered
# --------------------------------------------------------------------------

def test_old_mail_is_silent_and_delivered():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        senders(gw, vip={"boss@corp.example"}, instructed={"clerk@firm.example"})
        flags(gw, news={"news@shop.example"}, ignored={"news@shop.example"})
        events = t.rail(gw)
        box = t.FakeMailbox(gw)
        box.put("INBOX", msg("vip@x.org", sender="boss@corp.example", day=10,
                             files=[("f.pdf", "application/pdf", b"%PDF")]))
        box.put("INBOX", msg("clerk@x.org", sender="clerk@firm.example", subject="C", day=11),
                flags=["\\Seen"])
        box.put("INBOX", msg("news@x.org", sender="news@shop.example", subject="N", day=12))
        clock = [NOW]
        mirror = gw.Mirror(box, clock=lambda: clock[0])
        while mirror.poll():
            pass
        # The backfill: written delivered, told without a handover, held —
        # whoever sent it. No turn for ninety days of a VIP's mail.
        recs = t.by_id(gw)
        assert sorted(recs) == ["clerk@x.org", "vip@x.org"]
        assert all(r["delivered"] is True for r in recs.values()) and offers(mirror) == {}
        assert len(events) == 2
        for _kind, event in events:
            assert event["backfill"] is True and "handover" not in event
            assert event["gate"] == HELD and "files" not in event
        # And old news is neither filed nor moved about the mailbox.
        assert gw.filed == [] and held_in(box, "Archive") == []
        assert not seen(box, "INBOX", "news@x.org")
        assert mirror.waiting() == {"rail": 0, "since": None, "news": 0}
    print("ok: backfilled mail is delivered, silent and unfiled, whoever sent it")


def test_a_mail_that_came_back_is_not_offered_again():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        senders(gw, vip={"boss@corp.example"})
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        raw = msg("a@x.org", sender="boss@corp.example", day=39)
        uid = box.put("INBOX", raw)
        mirror.poll()
        assert offered(events) == ["a@x.org"]
        # Moved out of the mirrored folders in a client, and back.
        box.remove("INBOX", uid)
        mirror.poll()
        assert t.by_id(gw)["a@x.org"]["removed_at"]
        box.put("INBOX", raw)
        mirror.poll()
        record = t.by_id(gw)["a@x.org"]
        assert record["removed_at"] is None and record["delivered"] is True
        # Told, so the chat has it again — but held: it rang when it came.
        last = events[-1][1]
        assert last["message_id"] == "a@x.org" and "handover" not in last and last["gate"] == HELD
        assert offered(events) == ["a@x.org"] and offers(mirror) == {}
    print("ok: a mail that left the mailbox and came back is shown again, not gated again")


def test_a_mail_gone_or_merged_while_owed():
    with tempfile.TemporaryDirectory() as tmp:
        # Gone: tombstoned before the rail ever took it. Nothing is owed for
        # a mail that no longer exists, and it is offered to nobody.
        gw = t.load(tmp)
        state = {"answer": "down"}
        events = t.rail(gw, lambda kind, payload: state["answer"])
        mirror, box, clock = begin(gw)
        uid = box.put("INBOX", msg("a@x.org", day=39))
        mirror.poll()
        box.remove("INBOX", uid)
        mirror.poll()
        record = t.by_id(gw)["a@x.org"]
        assert record["removed_at"] and record["delivered"] is True
        assert offers(mirror) == {}
        events.clear()
        state["answer"] = "ok"
        clock[0] += gw.RAIL_RETRY_MAX_SECONDS
        mirror.poll()
        assert offered(events) == [] and [k for k, _p in events] == ["removed"]
        assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
    with tempfile.TemporaryDirectory() as tmp:
        # Gone after a first offer that was not taken: the same.
        gw = t.load(tmp)
        events = t.rail(gw, answer={})
        mirror, box, clock = begin(gw)
        uid = box.put("INBOX", msg("a@x.org", day=39))
        mirror.poll()
        assert "a@x.org" in offers(mirror)
        box.remove("INBOX", uid)
        clock[0] += gw.RETRY_MAX_SECONDS
        mirror.poll()
        assert len(offered(events)) <= 2 and offers(mirror) == {}
        events.clear()
        clock[0] += gw.RETRY_MAX_SECONDS
        mirror.poll()
        assert events == []
    with tempfile.TemporaryDirectory() as tmp:
        # Merged: its chat was absorbed into another while the offer was
        # owed. It is offered under the chat it is in NOW — an offer under
        # the old key would bring the absorbed chat back to life.
        gw = t.load(tmp)
        state = {"answer": "ok"}
        events = t.rail(gw, lambda kind, payload: state["answer"])
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("root@x.org", day=30))
        mirror.poll()
        state["answer"] = "down"
        box.put("INBOX", msg("b@x.org", subject="Re: Quarterly numbers", irt="lost@x.org",
                             day=38))
        mirror.poll()
        assert t.by_id(gw)["b@x.org"]["chat"] == "lost@x.org"
        clock[0] += gw.RAIL_RETRY_MAX_SECONDS
        box.put("INBOX", msg("c@x.org", subject="Re: Quarterly numbers",
                             refs=["root@x.org", "lost@x.org"], irt="b@x.org", day=39))
        mirror.poll()
        assert t.by_id(gw)["b@x.org"]["chat"] == "root@x.org"
        events.clear()
        state["answer"] = "ok"
        clock[0] += gw.RAIL_RETRY_MAX_SECONDS
        mirror.poll()
        assert [(k, p.get("message_id"), p.get("chat")) for k, p in events] == [
            ("inbound", "b@x.org", "root@x.org"), ("merge", None, None),
            ("inbound", "c@x.org", "root@x.org")]
        assert all(r["delivered"] is True for r in t.records(gw))
    print("ok: a mail gone while owed is offered to nobody, one merged while owed in the "
          "chat it is in now")


def test_a_placeholder_read_at_last_keeps_what_was_owed():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw)
        senders(gw, vip={"boss@corp.example"})
        state = {"body": {}}
        events = t.rail(gw, answer=lambda payload: (
            {"accepted": True} if payload["message_id"] == "taken@x.org" else state["body"]))
        mirror, box, clock = begin(gw)
        real_trim = gw._trim.trim
        broken = {"owed", "taken"}

        def trim(plain, html, subject=None, **evidence):
            if subject in broken:
                raise RecursionError("this mail, for now")
            return real_trim(plain, html, subject=subject, **evidence)
        gw._trim.trim = trim
        try:
            # Two mails the reader cannot read yet: mirrored as placeholders,
            # and offered as what arrived. The chat takes one of them.
            box.put("INBOX", msg("owed@x.org", sender="boss@corp.example", subject="owed",
                                 body="The real words.\n", day=39))
            box.put("INBOX", msg("taken@x.org", sender="boss@corp.example", subject="taken",
                                 body="Other words.\n", day=39))
            mirror.poll()
            recs = t.by_id(gw)
            assert recs["owed@x.org"]["text"] == gw.UNREADABLE_TEXT
            assert (recs["owed@x.org"]["delivered"], recs["taken@x.org"]["delivered"]) == (
                False, True)
            assert sorted(offers(mirror)) == ["owed@x.org"]
            # The moment passes: both are read again, and each record that
            # takes a placeholder's place is owed exactly what that was.
            broken.clear()
            events.clear()
            clock[0] += gw.SET_ASIDE_RETRY_SECONDS
            state["body"] = {"accepted": True}
            mirror.poll()
            recs = t.by_id(gw)
            assert recs["owed@x.org"]["text"] == "The real words."
            assert recs["taken@x.org"]["text"] == "Other words."
            told = {p["message_id"]: p for _k, p in events}
            # The one the chat had taken is replaced quietly, as old mail …
            assert told["taken@x.org"].get("backfill") is True
            assert "handover" not in told["taken@x.org"] and told["taken@x.org"]["gate"] == HELD
            # … the one still owed is offered — now with its words, and by
            # the gate — and delivered when the chat takes it.
            assert told["owed@x.org"]["handover"] is True
            assert told["owed@x.org"]["text"] == "The real words."
            assert told["owed@x.org"]["gate"]["vip"] is True
            assert all(r["delivered"] is True for r in recs.values()) and offers(mirror) == {}
        finally:
            gw._trim.trim = real_trim
    print("ok: a mail read at last is owed what its placeholder was, and no more")


def test_the_users_own_mail_is_never_owed():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        state = {"body": {}}
        events = t.rail(gw, answer=lambda payload: state["body"])
        mirror, box, clock = begin(gw)
        # Recorded from its INBOX copy — and owed — before the Sent copy
        # shows: it is the user's own mail, and nothing is owed for that.
        box.put("INBOX", msg("s@example.org", sender=ME, to=ACCOUNT, day=39))
        mirror.poll()
        assert "s@example.org" in offers(mirror)
        box.put("Sent", msg("s@example.org", sender=ME, to=ACCOUNT, day=39))
        mirror.poll()
        record = t.by_id(gw)["s@example.org"]
        assert record["type"] == gw._ibstore.T_OUTBOUND and offers(mirror) == {}
        events.clear()
        clock[0] += gw.RETRY_MAX_SECONDS
        mirror.poll()
        assert events == []
    print("ok: a mail that turns out to be the user's own is owed to nobody")


def test_with_no_rail_nothing_is_owed():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp, rail=False)
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        box.put("INBOX", msg("a@x.org", day=39))
        mirror.poll()
        # Nobody is there to take it: the reader has the ledger.
        assert t.by_id(gw)["a@x.org"]["delivered"] is True
        assert events == [] and offers(mirror) == {}
        assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
    print("ok: with no rail configured a mail is written delivered and owed to nobody")


# --------------------------------------------------------------------------
# The news rail
# --------------------------------------------------------------------------

def test_news_is_filed_with_the_link_the_mail_declares():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw, news={"news@shop.example", "dev.lists.x.org", "weekly@letters.example"})
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        gw.MIRROR = mirror
        box.put("INBOX", msg(
            "n1@shop.example", sender="The Shop <news@shop.example>", subject="Spring sale",
            body="Everything must go.\n\n\nhttps://track.example/click?id=1\n"
                 + "More words. " * 100, day=39,
            headers=[("Archived-At", "<https://shop.example/letters/42>"),
                     ("List-Archive", "<https://shop.example/letters/>")]))
        box.put("INBOX", msg("l1@x.org", sender="Dee <dee@x.org>", subject="[dev] Release",
                             list_id="Dev list <dev.lists.x.org>", body="Out now.\n", day=39,
                             headers=[("List-Archive", "<https://lists.x.org/dev/>")]))
        # No declared link: none — never one picked out of the body. And a
        # link that is not http(s) is no link.
        box.put("INBOX", msg("n2@letters.example", sender="weekly@letters.example",
                             subject="Weekly", body="Read at https://letters.example/7\n",
                             day=39, headers=[("Archived-At", "<javascript:alert(1)>")]))
        mirror.poll()
        assert gw.filed == [
            {"source": "The Shop", "title": "Spring sale", "url": "https://shop.example/letters/42",
             "source_id": "email:news@shop.example", "text": gw.filed[0]["text"]},
            {"source": "Dee", "title": "[dev] Release", "url": "https://lists.x.org/dev/",
             "source_id": "email:dee@x.org", "text": "[dev] Release\n\nOut now."},
            {"source": "weekly@letters.example", "title": "Weekly", "url": None,
             "source_id": "email:weekly@letters.example",
             "text": "Weekly\n\nRead at https://letters.example/7"}], gw.filed
        # Subject first, then a feed-sized excerpt with no blank runs.
        text = gw.filed[0]["text"]
        assert text.startswith("Spring sale\n\nEverything must go.\nhttps://track.example/")
        assert text.endswith("…") and len(text) <= len("Spring sale\n\n") + 600 + 1
        # A news mail starts no chat: no record, nothing told to the rail.
        assert t.records(gw) == [] and events == []
        # Its group is not ignored: the mail stays where and as it arrived.
        assert sorted(held_in(box, "INBOX")) == ["l1@x.org", "n1@shop.example",
                                                 "n2@letters.example"]
        assert not seen(box, "INBOX", "n1@shop.example") and box.writes == []
        assert gw._health_snapshot()["waiting"]["news"] == 0
        # Filed once: later polls neither file nor read it again.
        fetched = len(box.fetched)
        mirror.poll()
        mirror = again(gw, box, _clock)
        mirror.poll()
        assert len(gw.filed) == 3 and len(box.fetched) == fetched
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp, MAIL_NEWS_EXCERPT_CHARS=20)
        flags(gw, news={"news@shop.example"})
        t.rail(gw)
        mirror, box, _clock = begin(gw)
        box.put("INBOX", msg("n@shop.example", sender="news@shop.example", subject="S",
                             body="0123456789" * 5, day=39))
        mirror.poll()
        assert gw.filed[0]["text"] == "S\n\n01234567890123456789…"
    print("ok: a news mail is filed to the feed with its subject, an excerpt and the link "
          "it declares — and starts no chat")


def test_news_that_is_also_ignored_is_consumed_once_the_feed_has_it():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw, news={"news@shop.example", "quiet@shop.example"},
              ignored={"news@shop.example"}, quieted={"quiet@shop.example"})
        events = t.rail(gw)
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("n@shop.example", sender="news@shop.example", subject="N", day=39))
        # News and quieted: in the feed, and left in the INBOX.
        box.put("INBOX", msg("q@shop.example", sender="quiet@shop.example", subject="Q", day=39))
        box.put("INBOX", msg("ok@x.org", day=39))
        mirror.poll()
        assert [item["title"] for item in gw.filed] == ["N", "Q"]
        # Marked read and moved to the archive folder — by the gateway's own
        # write half, and only this mail.
        assert box.writes == [("seen", "INBOX", (1,)), ("move", "INBOX", (1,), "Archive")]
        assert sorted(held_in(box, "INBOX")) == ["ok@x.org", "q@shop.example"]
        assert held_in(box, "Archive") == ["n@shop.example"]
        assert seen(box, "Archive", "n@shop.example")
        assert not seen(box, "INBOX", "q@shop.example")
        # The mirror follows its own move: still news, in the archive; no
        # chat, no tombstone, nothing read again.
        place = mirror.index.one(
            "SELECT role, skipped FROM locations WHERE message_id = 'n@shop.example'")
        assert (place["role"], place["skipped"]) == ("archive", "news")
        fetched, told = len(box.fetched), len(events)
        for _ in range(3):
            clock[0] += 60
            mirror.poll()
        mirror = again(gw, box, clock)
        mirror.poll()
        assert len(box.fetched) == fetched and len(events) == told and len(gw.filed) == 2
        assert list(t.by_id(gw)) == ["ok@x.org"] and len(box.writes) == 2
        assert mirror.waiting()["news"] == 0
    print("ok: news of an ignored group is marked read and moved once filed; the mirror "
          "follows its own move")


def test_a_feed_that_refuses_leaves_the_mail_untouched_and_is_asked_again():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw, news={"news@shop.example"}, ignored={"news@shop.example"})
        t.rail(gw)
        state = {"takes": False}
        t.feed(gw, lambda item: state["takes"])
        mirror, box, clock = begin(gw)
        gw.MIRROR = mirror
        box.put("INBOX", msg("n1@shop.example", sender="news@shop.example", subject="One",
                             day=38))
        box.put("INBOX", msg("n2@shop.example", sender="news@shop.example", subject="Two",
                             day=39))
        mirror.poll()
        # Refused: nothing was done to the mail — not read, not moved — and
        # the feed was asked once this pass, not once a mail.
        assert [item["title"] for item in gw.filed] == ["One"]
        assert box.writes == [] and len(held_in(box, "INBOX")) == 2
        assert not seen(box, "INBOX", "n1@shop.example")
        assert gw._health_snapshot()["waiting"]["news"] == 2
        assert t.records(gw) == []
        # The refused one waits its own time; the one behind it is tried.
        mirror.poll()
        assert [item["title"] for item in gw.filed] == ["One", "Two"]
        mirror.poll()
        assert len(gw.filed) == 2
        # A restart keeps the queue; when the feed takes them, they are
        # filed — the very same items — and only then consumed.
        mirror = gw.MIRROR = again(gw, box, clock)
        state["takes"] = True
        clock[0] += gw.RETRY_SECONDS
        mirror.poll()
        assert gw.filed[2:] == gw.filed[:2]
        assert held_in(box, "INBOX") == [] and len(held_in(box, "Archive")) == 2
        assert gw._health_snapshot()["waiting"]["news"] == 0
    with tempfile.TemporaryDirectory() as tmp:
        # The feed has it, the mailbox will not move it: filed once, and the
        # move is tried again.
        gw = t.load(tmp)
        flags(gw, news={"news@shop.example"}, ignored={"news@shop.example"})
        t.rail(gw)
        mirror, box, clock = begin(gw)
        box.no_writes.add("INBOX")
        box.put("INBOX", msg("n@shop.example", sender="news@shop.example", day=39))
        mirror.poll()
        assert len(gw.filed) == 1 and held_in(box, "INBOX") == ["n@shop.example"]
        assert mirror.waiting()["news"] == 1
        box.no_writes.clear()
        clock[0] += gw.RETRY_SECONDS
        mirror.poll()
        assert len(gw.filed) == 1 and held_in(box, "Archive") == ["n@shop.example"]
        assert mirror.waiting()["news"] == 0
    print("ok: a feed that refuses leaves the mail untouched and is asked again; the move "
          "comes strictly after the filing")


def test_where_a_consumed_news_mail_goes():
    with tempfile.TemporaryDirectory() as tmp:
        # Set and empty: marked read, left where it is.
        gw = t.load(tmp, MAIL_NEWS_FOLDER="")
        flags(gw, news={"news@shop.example"}, ignored={"news@shop.example"})
        t.rail(gw)
        mirror, box, _clock = begin(gw)
        box.put("INBOX", msg("n@shop.example", sender="news@shop.example", day=39))
        mirror.poll()
        assert box.writes == [("seen", "INBOX", (1,))] and seen(box, "INBOX", "n@shop.example")
        assert mirror.waiting()["news"] == 0
    with tempfile.TemporaryDirectory() as tmp:
        # A folder of its own: made (and subscribed, by the adapter) when
        # missing. It is not mirrored, and the mail was never a chat: gone
        # from the mirror's folders without a tombstone.
        gw = t.load(tmp, MAIL_NEWS_FOLDER="Newsletters")
        flags(gw, news={"news@shop.example"}, ignored={"news@shop.example"})
        events = t.rail(gw)
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("n@shop.example", sender="news@shop.example", day=39))
        mirror.poll()
        assert ("create", "Newsletters") in box.writes
        assert held_in(box, "Newsletters") == ["n@shop.example"] and held_in(box, "INBOX") == []
        box.put("INBOX", msg("n2@shop.example", sender="news@shop.example", subject="2", day=39))
        clock[0] += 60
        mirror.poll()
        assert box.writes.count(("create", "Newsletters")) == 1
        assert len(held_in(box, "Newsletters")) == 2 and events == [] and t.records(gw) == []
    with tempfile.TemporaryDirectory() as tmp:
        # No archive folder yet: made, by the rule every archive move goes by.
        gw = t.load(tmp, MAIL_ARCHIVE_FOLDER="Filed")
        flags(gw, news={"news@shop.example"}, ignored={"news@shop.example"})
        t.rail(gw)
        box = t.FakeMailbox(gw, folders=("INBOX", "Sent"))
        box.roles["archive"] = "Filed"
        mirror, box, clock = begin(gw, box)
        box.put("INBOX", msg("n@shop.example", sender="news@shop.example", day=39))
        mirror.poll()
        assert held_in(box, "Filed") == ["n@shop.example"]
        clock[0] += 60
        mirror.poll()
        assert t.records(gw) == []
    with tempfile.TemporaryDirectory() as tmp:
        # A server with neither MOVE nor UIDPLUS keeps the original, flagged:
        # still a place of the mail, never one to move or flag again.
        gw = t.load(tmp)
        flags(gw, news={"news@shop.example"}, ignored={"news@shop.example"})
        t.rail(gw)
        mirror, box, clock = begin(gw)
        box.can_expunge = False
        box.put("INBOX", msg("n@shop.example", sender="news@shop.example", day=39))
        mirror.poll()
        writes = len(box.writes)
        for _ in range(3):
            clock[0] += gw.RETRY_MAX_SECONDS
            mirror.poll()
        assert len(box.writes) == writes and len(gw.filed) == 1
        assert held_in(box, "Archive") == ["n@shop.example"] and t.records(gw) == []
    print("ok: a consumed news mail goes to the archive folder, to a folder of its own, "
          "or nowhere — by the gateway's own folder rules")


def test_a_news_mail_in_a_chat_is_filed_and_left_to_the_chat():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw, news={"dev.lists.x.org"}, ignored={"dev.lists.x.org"})
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        # The user wrote to the list; the list answers in that thread.
        box.put("Sent", msg("mine@example.org", sender=ME, to="dev@lists.x.org",
                            subject="[dev] Question", day=38))
        mirror.poll()
        box.put("INBOX", msg("l2@x.org", sender="Dee <dee@x.org>", subject="Re: [dev] Question",
                             list_id="<dev.lists.x.org>", refs=["mine@example.org"],
                             irt="mine@example.org", day=39))
        mirror.poll()
        # In the feed, and in the chat — offered, silently (the group is
        # ignored) — and its mailbox copy is the chat's to deal with.
        assert [item["title"] for item in gw.filed] == ["Re: [dev] Question"]
        record = t.by_id(gw)["l2@x.org"]
        assert record["chat"] == "mine@example.org" and record["delivered"] is True
        assert events[-1][1]["gate"] == {"forward": False, "vip": False, "instructed": False,
                                         "reason": "group-ignored"}
        assert box.writes == [] and held_in(box, "INBOX") == ["l2@x.org"]
        assert mirror.waiting()["news"] == 0
    print("ok: a news mail that joins a chat is filed and shown, and never moved under it")


def test_with_no_news_rail_a_news_mail_is_mirrored_like_any_other():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp, news=False)
        flags(gw, news={"news@shop.example"}, ignored={"news@shop.example"})
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        box.put("INBOX", msg("n@shop.example", sender="news@shop.example", day=39))
        mirror.poll()
        # No feed can take it, so it must not be shown nowhere at all.
        assert list(t.by_id(gw)) == ["n@shop.example"] and gw.filed == []
        assert events[-1][1]["gate"]["reason"] == "group-ignored" and box.writes == []
    print("ok: with the news rail off, the news flag hides no mail")


# --------------------------------------------------------------------------
# The wire, health, and what is never done
# --------------------------------------------------------------------------

class _Rail(BaseHTTPRequestHandler):
    seen: list = []
    status, body, delay = 200, b"{}", 0.0

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).seen.append((self.path, json.loads(payload)))
        time.sleep(type(self).delay)
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def do_GET(self):
        type(self).seen.append((self.path, None))
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _served(gw):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Rail)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    gw._chats.CHATS_INGEST_URL = f"{base}/internal/chats/inbound"
    _Rail.seen, _Rail.status, _Rail.body, _Rail.delay = [], 200, b"{}", 0.0
    return server


def test_an_offer_on_the_wire_and_what_each_answer_reads_as():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        server = _served(gw)
        try:
            for status, body, expected in (
                    (200, b'{"ok": true, "accepted": true}', ("ok", {"ok": True, "accepted": True})),
                    (202, b'{"accepted": true, "job_url": "/jobs/3"}',
                     ("ok", {"accepted": True, "job_url": "/jobs/3"})),
                    (200, b"", ("ok", {})), (200, b"not json", ("ok", {})),
                    (200, b'["accepted"]', ("ok", {})),
                    (400, b"{}", ("drop", {})), (413, b"{}", ("drop", {})),
                    (500, b"{}", ("down", {})), (404, b"{}", ("down", {})),
                    (403, b"{}", ("down", {}))):
                _Rail.status, _Rail.body = status, body
                assert gw._rail_offer({"chat": "a", "handover": True}) == expected, (status, body)
            assert _Rail.seen[0] == ("/internal/chats/inbound", {"chat": "a", "handover": True})
            # No answer in time: whether it landed is not known.
            _Rail.status, _Rail.body, _Rail.delay = 200, b'{"accepted": true}', 0.6
            assert gw._rail_post("inbound", {}, 0.2) == ("uncertain", {})
            # An event that is only told reads that as "not answering".
            gw.MAIL_RAIL_TIMEOUT = 0.2
            assert gw._rail_send("merge", {}) == "down"
        finally:
            server.shutdown()
        gw._chats.CHATS_INGEST_URL = "http://127.0.0.1:9/internal/chats/inbound"
        assert gw._rail_offer({}) == ("down", {})
    with tempfile.TemporaryDirectory() as tmp:
        # Nothing that is not text leaves by an offer either.
        gw = t.load(tmp)
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        box.put("INBOX", msg("a@x.org", day=39))
        real = mirror._inbound_event
        mirror._inbound_event = lambda **kw: {**real(**kw), "text": "a\ud800b"}
        mirror.poll()
        json.dumps(events[-1][1]).encode("utf-8")
        assert "\ud800" not in events[-1][1]["text"]
    print("ok: an offer is one POST to the rail, and only an explicit acceptance reads "
          "as one")


def test_nothing_is_ever_forwarded_to_triage():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        senders(gw, vip={"boss@corp.example"}, instructed={"clerk@firm.example"})
        flags(gw, quieted={"dee@x.org"}, ignored={"noise@spam.example"})
        server = _served(gw)
        real_offer, real_send = gw._rail_offer, gw._rail_send
        # Every way out of the process: urllib (the rails) and requests (the
        # job poll, and what a messenger gateway forwards to triage with).
        opened, real_open = [], gw.urllib.request.urlopen
        requests = gw._jobs.requests
        real_post, real_get = requests.post, requests.get

        def urlopen(request, *args, **kwargs):
            opened.append(request.full_url)
            return real_open(request, *args, **kwargs)
        gw.urllib.request.urlopen = urlopen
        requests.post = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError(f"a POST by requests: {a}"))
        requests.get = lambda url, **kw: (opened.append(url), real_get(url, **kw))[1]
        try:
            # The real wire, and a web gateway that takes no mail: switched
            # off, older, or unable to start the turn.
            mirror, box, clock = begin(gw)
            for n, sender in enumerate(("ann@x.org", "boss@corp.example", "clerk@firm.example",
                                        "dee@x.org", "noise@spam.example")):
                box.put("INBOX", msg(f"m{n}@x.org", sender=sender, subject=f"S{n}", day=39))
            for _ in range(4):
                mirror.poll()
                clock[0] += gw.RETRY_MAX_SECONDS
            assert len(_Rail.seen) == 20
            # A turn's job that fails is polled on the rail, and fails there.
            _Rail.body = b'{"accepted": true, "job_url": "/jobs/1"}'
            mirror.poll()
            for thread in list(mirror._jobs.values()):
                thread.join(20)
        finally:
            gw.urllib.request.urlopen = real_open
            requests.post, requests.get = real_post, real_get
            server.shutdown()
        base = gw._chats.CHATS_INGEST_URL.rsplit("/internal/", 1)[0]
        assert set(opened) == {f"{base}/internal/chats/inbound", f"{base}/jobs/1"}, set(opened)
        assert {path for path, _body in _Rail.seen} == {"/internal/chats/inbound", "/jobs/1"}
        # Whoever sent it, whatever its group: still undelivered, still owed.
        assert [r["delivered"] for r in t.records(gw)] == [False] * 5
        assert len(offers(mirror)) == 5
        assert (gw._rail_offer, gw._rail_send) == (real_offer, real_send)
    # And the gateway has no way to: no retinue gateway address, no prompt.
    source = (t.SCRIPTS_DIR / "mail-gateway.py").read_text(encoding="utf-8")
    assert "RETINUE_GATEWAY_URL" not in source and "triage skill" not in source
    print("ok: a mail the rail does not take goes nowhere else — there is no triage for mail")


def test_health_says_what_waits():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        assert gw._health_snapshot()["waiting"] == {"rail": 0, "since": None, "news": 0}
        state = {"answer": "down"}
        t.rail(gw, lambda kind, payload: state["answer"])
        mirror, box, clock = begin(gw)
        gw.MIRROR = mirror
        box.put("INBOX", msg("a@x.org", day=38))
        box.put("INBOX", msg("b@x.org", subject="B", day=39))
        mirror.poll()
        assert gw._health_snapshot()["waiting"] == {"rail": 2, "since": T0 + 38 * DAY, "news": 0}
        assert mirror.health_notice() is None
        # Away for a while: an operator is told, while mails wait.
        for _ in range(2):
            clock[0] += gw.RAIL_RETRY_MAX_SECONDS
            mirror.poll()
        notice = mirror.health_notice()
        assert "chats rail is not answering" in notice and "2 mail(s)" in notice
        state["answer"] = "ok"
        clock[0] += gw.RAIL_RETRY_MAX_SECONDS
        mirror.poll()
        assert mirror.health_notice() is None
        assert gw._health_snapshot()["waiting"] == {"rail": 0, "since": None, "news": 0}
        json.dumps(gw._health_snapshot())
    print("ok: /health counts the mails waiting for the rail and for the feed")


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
        print(f"\n{failures} mail gateway gate check(s) failed.")
        return 1
    print("\nAll mail gateway gate checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
