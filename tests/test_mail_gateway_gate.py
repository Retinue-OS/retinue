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
        assert gw._health_snapshot()["waiting"] == {"rail": 1, "since": T0 + 39 * DAY, "news": 0,
                                                  "failing": 0, "failing_since": None}
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
        assert gw._health_snapshot()["waiting"] == {"rail": 0, "since": None, "news": 0,
                                                  "failing": 0, "failing_since": None}
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
        assert mirror.waiting() == {"rail": 0, "since": None, "news": 0,
                                    "failing": 0, "failing_since": None}
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
        # Gone: tombstoned before the rail ever took it. A mail that no
        # longer exists is offered to nobody — its tombstone keeps saying it
        # was owed, for the day it comes back (see the next test).
        gw = t.load(tmp)
        state = {"answer": "down"}
        events = t.rail(gw, lambda kind, payload: state["answer"])
        mirror, box, clock = begin(gw)
        uid = box.put("INBOX", msg("a@x.org", day=39))
        mirror.poll()
        box.remove("INBOX", uid)
        mirror.poll()
        record = t.by_id(gw)["a@x.org"]
        assert record["removed_at"] and record["delivered"] is False
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
            # and offered as what arrived (a stranger's — a VIP's would wait
            # for its reading). The chat takes one of them.
            box.put("INBOX", msg("owed@x.org", sender="ann@x.org", subject="owed",
                                 body="The real words.\n", day=39))
            box.put("INBOX", msg("taken@x.org", sender="ann@x.org", subject="taken",
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
            assert told["owed@x.org"]["gate_sender"] == "ann@x.org"
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
            # List-Archive names the list, not the mail: no link (the feed
            # keys an item on its link).
            {"source": "Dee", "title": "[dev] Release", "url": None,
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
        if type(self).status == 0:
            # A handler that died on the event: the connection closes with
            # no answer at all.
            self.close_connection = True
            return
        try:
            self.send_response(type(self).status)
            self.send_header("Content-Length", str(len(type(self).body)))
            self.end_headers()
            self.wfile.write(type(self).body)
        except OSError:
            pass   # the gateway stopped waiting for this answer

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
                    # A failure the web gateway answered with is about this
                    # mail; a URL that is not the rail, or the token, is not.
                    (500, b"{}", ("unanswered", {})), (503, b"{}", ("unanswered", {})),
                    (429, b"{}", ("unanswered", {})),
                    (404, b"{}", ("down", {})), (403, b"{}", ("down", {}))):
                _Rail.status, _Rail.body = status, body
                assert gw._rail_offer({"chat": "a", "handover": True}) == expected, (status, body)
            assert _Rail.seen[0] == ("/internal/chats/inbound", {"chat": "a", "handover": True})
            # No answer in time: whether it landed is not known.
            _Rail.status, _Rail.body, _Rail.delay = 200, b'{"accepted": true}', 0.6
            assert gw._rail_post("inbound", {}, 0.2) == ("uncertain", {})
            # An event that is only told reads it the same: the mirror asks
            # whether the rail is up (Mirror._rail_heard).
            gw.MAIL_RAIL_TIMEOUT = 0.2
            assert gw._rail_send("merge", {}) == "uncertain"
            # A connection closed with no answer: as unknown as a timeout —
            # and the rail, asked something that takes nothing, answers.
            _Rail.status, _Rail.delay = 0, 0.0
            assert gw._rail_post("inbound", {}, 2.0) == ("uncertain", {})
            assert gw._rail_send("inbound", {}) == "uncertain"
            assert gw._rail_alive() is True
            assert _Rail.seen[-1] == ("/internal/chats/inbound", None)
        finally:
            server.shutdown()
        gw._chats.CHATS_INGEST_URL = "http://127.0.0.1:9/internal/chats/inbound"
        assert gw._rail_offer({}) == ("down", {})
        # Nobody there: down, and the look finds nobody either.
        assert gw._rail_alive() is False
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
        assert gw._health_snapshot()["waiting"] == {"rail": 0, "since": None, "news": 0,
                                                  "failing": 0, "failing_since": None}
        state = {"answer": "down"}
        t.rail(gw, lambda kind, payload: state["answer"])
        mirror, box, clock = begin(gw)
        gw.MIRROR = mirror
        box.put("INBOX", msg("a@x.org", day=38))
        box.put("INBOX", msg("b@x.org", subject="B", day=39))
        mirror.poll()
        assert gw._health_snapshot()["waiting"] == {"rail": 2, "since": T0 + 38 * DAY, "news": 0,
                                                  "failing": 0, "failing_since": None}
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
        assert gw._health_snapshot()["waiting"] == {"rail": 0, "since": None, "news": 0,
                                                  "failing": 0, "failing_since": None}
        json.dumps(gw._health_snapshot())
    print("ok: /health counts the mails waiting for the rail and for the feed")


# --------------------------------------------------------------------------
# Who the gate takes for the sender; one mail never holds back another
# --------------------------------------------------------------------------

def raw(headers: str, mid: str = "m@x.org", body: str = "Hello.\n") -> bytes:
    """A mail written by hand, header by header — for the forms a library
    would not write (two From headers, a group)."""
    return (f"Message-ID: <{mid}>\r\nTo: {ACCOUNT}\r\nSubject: S {mid}\r\n"
            f"Date: {t.email.utils.formatdate(T0 + 39 * DAY)}\r\n{headers}"
            f"\r\n{body}").encode("utf-8")


def test_the_gate_sender_is_the_one_address_of_the_one_from():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        cases = {
            "From: The Boss <Boss@Corp.Example>\r\n": "boss@corp.example",
            "From: =?utf-8?q?Boss=2C_The?= <boss@corp.example>\r\n": "boss@corp.example",
            # The Sender header never stands in for the From.
            "From: Payroll:;\r\nSender: boss@corp.example\r\n": "",
            "From:\r\nSender: boss@corp.example\r\n": "",
            "Sender: boss@corp.example\r\n": "",
            "From: =?utf-8?q?nobody?=\r\nSender: boss@corp.example\r\n": "",
            "From: \"attacker\"@evil.example\r\nSender: boss@corp.example\r\n": "",
            "From: Eve <\"e v e\"@evil.example>\r\nSender: boss@corp.example\r\n": "",
            "From: Attacker\r\nSender: boss@corp.example\r\n": "",
            # Nor the first of several, nor the one of two headers, nor the
            # one a rejected entry left, nor a group's one member.
            "From: boss@corp.example, Mallory <mallory@evil.example>\r\n": "",
            "From: boss@corp.example\r\nFrom: mallory@evil.example\r\n": "",
            "From: boss@corp.example, \"x y\"@evil.example\r\n": "",
            "From: Staff: boss@corp.example;\r\n": "",
            # A lookalike that folds into ASCII on some reading names nobody.
            "From: boss@corK.example\r\n": "",
            "From: boss@corp．example\r\n": "",
        }
        for headers, expected in cases.items():
            mail = gw.parse_mail(raw(headers))
            assert mail.gate_sender == expected, (headers, mail.gate_sender)
            # A placeholder is gated by the same rule.
            assert gw.stub_mail(raw(headers), None, "test").gate_sender == expected, headers
        # What the chat shows may still come from the Sender header.
        assert gw.parse_mail(raw("Sender: boss@corp.example\r\n")).sender == "boss@corp.example"
        # Mirrored: the record and the rail carry both, and only the gate
        # sender decides.
        senders(gw, vip={"boss@corp.example"}, instructed={"*@corp.example"})
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        box.put("INBOX", raw("From: Payroll:;\r\nSender: boss@corp.example\r\n", "spoof@x.org"))
        box.put("INBOX", raw("From: boss@corp.example, m@evil.example\r\n", "two@x.org"))
        box.put("INBOX", raw("From: The Boss <boss@corp.example>\r\n", "real@x.org"))
        mirror.poll()
        got = {p["message_id"]: p for _k, p in events}
        records = t.by_id(gw)
        for mid in ("spoof@x.org", "two@x.org"):
            assert got[mid]["gate"] == OPEN and got[mid]["gate_sender"] == "", got[mid]
            assert got[mid]["sender"] == "boss@corp.example" and "files" not in got[mid]
            assert records[mid]["gate_sender"] is None and records[mid]["delivered"] is True
        assert got["real@x.org"]["gate"]["vip"] is True
        assert got["real@x.org"]["gate_sender"] == "boss@corp.example"
        assert records["real@x.org"]["gate_sender"] == "boss@corp.example"
        # A record written without the field (before it existed) is from
        # nobody: no turn on a guess.
        fields = dict(records["real@x.org"], gate_sender=None)
        assert mirror._offer_event("real@x.org", fields)["gate"]["vip"] is False
    print("ok: the gate goes by the one address of the one From alone — never the Sender "
          "header, the first of several, or a lookalike")


def test_a_cut_header_block_names_nobody():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp, MAIL_MAX_MAIL_BYTES=str(64 * 1024))
        senders(gw, vip={"boss@corp.example"})
        events = t.rail(gw)

        def padded(lines, mid, second=True):
            pad = "".join(f"X-Pad-{n:06d}: {'a' * 60}\r\n" for n in range(lines))
            return raw("From: boss@corp.example\r\n" + pad
                       + ("From: Mallory <mallory@evil.example>\r\n" if second else ""), mid)
        # The second From lies past MAX_HEADER_BYTES: the parser never sees
        # it, so "exactly one From" cannot be told — and the mail is from
        # nobody, whichever From it carries second, or none.
        beyond = padded(3700, "cut@x.org")
        assert beyond.index(b"From: Mallory") > gw.MAX_HEADER_BYTES
        assert len(t.email.message_from_bytes(beyond).get_all("From")) == 2
        for mail in (beyond, padded(3700, "cut1@x.org", second=False)):
            assert gw.parse_mail(mail).gate_sender == ""
            assert gw.stub_mail(mail, None, "test").gate_sender == ""
            # What the chat shows still comes from the first From.
            assert gw.parse_mail(mail).sender == "boss@corp.example"
        # A whole header block of the same mail's kind is gated as ever.
        assert gw.parse_mail(padded(100, "whole@x.org", second=False)).gate_sender \
            == "boss@corp.example"
        # Mirrored, and fetched only up to MAIL_MAX_MAIL_BYTES: a header
        # block that goes on past what was fetched names nobody either.
        mirror, box, _clock = begin(gw)
        box.put("INBOX", beyond)
        box.put("INBOX", padded(1500, "fetched@x.org"))
        box.put("INBOX", padded(100, "small@x.org", second=False))
        mirror.poll()
        got = {p["message_id"]: p for _k, p in events}
        for mid in ("cut@x.org", "fetched@x.org"):
            assert got[mid]["gate_sender"] == "" and got[mid]["gate"] == OPEN, got[mid]["gate"]
        assert got["small@x.org"]["gate"]["vip"] is True
    print("ok: a mail whose header block was cut at its bound, or fetched short of its "
          "end, has no gate sender")


def test_an_internationalised_address_is_a_gate_sender():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        cases = {
            # Letters of their own: a case mapping that holds ASCII letters
            # beside others, or several of them, is no lookalike.
            "From: Boss <info@straße.example>\r\n": "info@straße.example",
            "From: Boss <bılal@corp.example>\r\n": "bılal@corp.example",
            "From: Boss <info@İstanbul.example>\r\n": "info@İstanbul.example",
            "From: Boss <boss@münchen.example>\r\n": "boss@münchen.example",
            # A character that turns into ASCII on its own names nobody.
            "From: boss@cor\u212a.example\r\n": "",         # KELVIN SIGN: "k"
            "From: boss@corp．example\r\n": "",             # FULLWIDTH FULL STOP: "."
            "From: boſs@corp.example\r\n": "",              # LONG S: "s"
            "From: ｂoss@corp.example\r\n": "",              # FULLWIDTH b
            "From: oﬃce@corp.example\r\n": "",              # the ligature "ffi"
        }
        for headers, expected in cases.items():
            assert gw.parse_mail(raw(headers)).gate_sender == expected, \
                (headers, gw.parse_mail(raw(headers)).gate_sender)
        senders(gw, vip={"info@straße.example"})
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        box.put("INBOX", raw("From: Boss <info@straße.example>\r\n", "sz@x.org"))
        mirror.poll()
        assert events[-1][1]["gate_sender"] == "info@straße.example"
        assert events[-1][1]["gate"]["vip"] is True
    print("ok: an address with ß, ı or İ is a gate sender; one with a character that "
          "stands in for ASCII is not")


def test_news_is_linked_by_archived_at_alone():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw, news={"weekly.lists.example"}, ignored={"weekly.lists.example"})
        t.rail(gw)
        mirror, box, clock = begin(gw)
        # Every issue of the list carries the list's archive: two issues are
        # two items, each filed and each taken out of the INBOX.
        for n in (1, 2):
            box.put("INBOX", msg(f"issue{n}@x.org", sender="weekly@letters.example",
                                 subject=f"Issue {n}", body=f"Issue {n} text.\n",
                                 list_id="<weekly.lists.example>", day=38 + n,
                                 headers=[("List-Archive", "<https://weekly.example/archive>")]))
            mirror.poll()
        assert [(item["title"], item["url"]) for item in gw.filed] == [
            ("Issue 1", None), ("Issue 2", None)]
        assert gw.filed[0]["text"] != gw.filed[1]["text"]
        assert sorted(held_in(box, "Archive")) == ["issue1@x.org", "issue2@x.org"]
    print("ok: a news item is linked only by the mail's own Archived-At, never by the "
          "list's archive")


def test_the_two_axes_stay_independent_for_news():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        senders(gw, vip={"boss@corp.example"}, instructed={"clerk@corp.example"})
        flags(gw, news_wildcards={"*@corp.example"}, ignored={"letters.lists.example"},
              news={"letters.lists.example"})
        events = t.rail(gw)
        mirror, box, _clock = begin(gw)
        box.put("INBOX", msg("vip@x.org", sender="boss@corp.example", subject="Call me",
                             day=39))
        box.put("INBOX", msg("clerk@x.org", sender="clerk@corp.example", subject="Invoice",
                             day=39))
        box.put("INBOX", msg("viplist@x.org", sender="boss@corp.example", subject="Letter",
                             list_id="<letters.lists.example>", day=39))
        box.put("INBOX", msg("promo@x.org", sender="promo@corp.example", subject="Sale",
                             day=39))
        mirror.poll()
        got = {p["message_id"]: p for _k, p in events}
        # A VIP's and an instructed sender's mail get their chat, offer and
        # turn — and are filed as well. Anyone else of the news group is
        # news alone.
        assert sorted(got) == ["clerk@x.org", "vip@x.org", "viplist@x.org"]
        assert got["vip@x.org"]["gate"]["vip"] is True and got["vip@x.org"]["handover"]
        assert got["clerk@x.org"]["gate"]["instructed"] is True
        assert got["viplist@x.org"]["gate"] == {
            "forward": False, "vip": True, "instructed": False, "reason": "vip+group-ignored"}
        assert sorted(item["title"] for item in gw.filed) == [
            "Call me", "Invoice", "Letter", "Sale"]
        # In a chat, so never taken out of the INBOX by the news rail — even
        # where the group is ignored.
        assert "viplist@x.org" in held_in(box, "INBOX") and held_in(box, "Archive") == []
        assert not seen(box, "INBOX", "viplist@x.org")
        assert sorted(t.by_id(gw)) == ["clerk@x.org", "vip@x.org", "viplist@x.org"]
    print("ok: a news group never swallows a VIP's or an instructed sender's mail")


def test_a_record_not_read_for_a_moment_is_not_gone():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        senders(gw, vip={"boss@corp.example"})
        events = t.rail(gw)
        mirror, box, clock = begin(gw)
        real, calls = gw._ibstore.read_record, []

        def flaky(path):
            calls.append(path)
            return None if len(calls) == 1 else real(path)   # EIO, EMFILE, ...
        gw._ibstore.read_record = flaky
        try:
            box.put("INBOX", msg("v@x.org", sender="boss@corp.example", day=39))
            mirror.poll()
        finally:
            gw._ibstore.read_record = real
        assert calls and offered(events) == [] and "v@x.org" in offers(mirror)
        assert t.by_id(gw)["v@x.org"]["delivered"] is False
        clock[0] += gw.RETRY_SECONDS
        mirror.poll()
        assert offered(events) == ["v@x.org"] and events[-1][1]["gate"]["vip"] is True
        assert t.by_id(gw)["v@x.org"]["delivered"] is True and offers(mirror) == {}
    print("ok: a record that could not be read just now is offered again, not dropped")


def test_turns_at_work_take_no_slot_of_the_re_offers():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        senders(gw, instructed={"*@bills.example"})
        jobs = FakeJobs(gw)
        try:
            state = {"plain": False}

            def answer(payload):
                if payload["gate"]["instructed"]:
                    return {"accepted": True, "job_url": "/jobs/" + payload["message_id"]}
                return {"accepted": state["plain"]}
            events = t.rail(gw, "ok", answer)
            mirror, box, clock = begin(gw)
            for n in range(gw.OFFER_BATCH + 5):
                box.put("INBOX", msg(f"r{n:02d}@x.org", sender=f"b{n}@bills.example",
                                     subject=f"Receipt {n}", day=30))
            box.put("INBOX", msg("plain@x.org", sender="ann@x.org", subject="Lunch?", day=31))
            mirror.poll()
            assert len(jobs.asked) == gw.OFFER_BATCH + 5
            state["plain"] = True
            clock[0] += gw.RETRY_SECONDS
            mirror.poll()
            assert offered(events)[-1] == "plain@x.org"
            assert t.by_id(gw)["plain@x.org"]["delivered"] is True
            # The turns at work were not offered again meanwhile.
            assert offered(events).count("r00@x.org") == 1
        finally:
            jobs.restore()
    print("ok: mails whose turns are at work take no slot of a pass's re-offers")


def test_one_mail_the_rail_fails_on_holds_back_no_other():
    for failing in ("unanswered", "uncertain"):
        with tempfile.TemporaryDirectory() as tmp:
            gw = t.load(tmp)
            senders(gw, vip={"boss@corp.example"})
            # The web gateway fails on one mail (a 5xx; a handler that died
            # on it and closed the connection) and takes every other — and
            # answers when it is asked whether it is up.
            events = t.rail(gw, lambda kind, payload: failing if payload.get(
                "message_id") == "poison@x.org" else "ok", alive=True)
            mirror, box, clock = begin(gw)
            box.put("INBOX", msg("poison@x.org", sender="mallory@evil.example", day=30))
            mirror.poll()
            for n in range(3):
                box.put("INBOX", msg(f"vip{n}@x.org", sender="boss@corp.example",
                                     subject=f"Urgent {n}", day=31 + n))
            clock[0] += 1
            mirror.poll()
            assert offered(events) == ["poison@x.org"] + [f"vip{n}@x.org" for n in range(3)], \
                (failing, offered(events))
            records = t.by_id(gw)
            assert all(records[f"vip{n}@x.org"]["delivered"] for n in range(3))
            # The one it fails on waits its own, growing, time — in its
            # place in the outbox, where only its own chat's later events
            # would wait for it.
            assert records["poison@x.org"]["delivered"] is False
            marker = mirror.index.one("SELECT kind, attempts, due FROM outbox")
            assert (marker["kind"], marker["attempts"]) == ("offer", 1), dict(marker)
            assert marker["due"] == NOW + gw.RETRY_SECONDS, (marker["due"], NOW)
            assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 1
            assert mirror.waiting()["rail"] == 1 and mirror._rail_down == 0
            clock[0] = NOW + gw.RETRY_SECONDS
            mirror.poll()
            assert offered(events)[-1] == "poison@x.org", failing
            assert mirror.index.one("SELECT attempts FROM outbox")["attempts"] == 2
    with tempfile.TemporaryDirectory() as tmp:
        # A rail that answers nothing at all is down: the order holds, and
        # nothing behind the mail goes.
        gw = t.load(tmp)
        events = t.rail(gw, "uncertain", alive=False)
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("a@x.org", day=30))
        box.put("INBOX", msg("b@x.org", subject="Other", day=31))
        mirror.poll()
        assert offered(events) == ["a@x.org"] and mirror._rail_down == 1
        assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 2
    print("ok: one mail the rail fails on waits its own time; only a rail that answers "
          "nothing holds every mail back")


def test_one_event_the_rail_fails_on_holds_back_only_its_chat():
    for failing in ("unanswered", "uncertain"):
        with tempfile.TemporaryDirectory() as tmp:
            gw = t.load(tmp)
            senders(gw, vip={"boss@corp.example"})
            state = {"heal": False}
            # The web gateway fails on the user's own mail (a 5xx from its
            # handler; a handler that hung on it) and takes every other.
            events = t.rail(gw, lambda kind, payload: "ok" if state["heal"] or payload.get(
                "message_id") != "s@example.org" else failing, alive=True)
            mirror, box, clock = begin(gw)
            box.put("Sent", msg("s@example.org", sender=ME, to="ann@x.org", subject="Plans",
                                day=38))
            mirror.poll()
            # The correspondent answers it; a VIP writes in a thread of its own.
            clock[0] += 1
            box.put("INBOX", msg("b@x.org", subject="Re: Plans", refs=["s@example.org"],
                                 irt="s@example.org", day=39))
            box.put("INBOX", msg("v@x.org", sender="boss@corp.example", subject="Urgent",
                                 day=39))
            mirror.poll()
            records = t.by_id(gw)
            assert records["b@x.org"]["chat"] == records["s@example.org"]["chat"]
            # The VIP's mail is not held back by another chat's event ...
            assert offered(events) == ["v@x.org"], (failing, offered(events))
            assert records["v@x.org"]["delivered"] is True
            # ... and the reply waits behind the mail it answers.
            assert records["b@x.org"]["delivered"] is False
            told = mirror.index.one("SELECT attempts, due FROM outbox WHERE kind = 'inbound'")
            assert told["attempts"] == 1 and told["due"] == NOW + gw.RETRY_SECONDS, dict(told)
            assert mirror._rail_down == 0 and mirror.health_notice() is None, failing
            # Not tried again before its own wait is over; then in order.
            mirror.poll()
            assert [p["message_id"] for _k, p in events].count("s@example.org") == 1
            state["heal"] = True
            events.clear()
            clock[0] = NOW + gw.RETRY_SECONDS
            mirror.poll()
            assert [(p["message_id"], p["direction"]) for _k, p in events] == [
                ("s@example.org", "out"), ("b@x.org", "in")], (failing, events)
            assert t.by_id(gw)["b@x.org"]["delivered"] is True
            assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
    print("ok: a told event the rail fails on waits its own time, and holds back only its "
          "own chat's later events")


def _until_due(mirror, clock):
    """Move the clock to the next time a waiting rail event or re-offer is
    due, and poll."""
    due = mirror.index.one(
        "SELECT MIN(due) AS due FROM (SELECT due FROM outbox WHERE attempts > 0 "
        "UNION ALL SELECT due FROM offers WHERE queued = 0 AND attempts > 0)")["due"]
    clock[0] = max(clock[0] + 1, due or 0)
    mirror.poll()


def test_a_failing_event_steps_aside():
    with tempfile.TemporaryDirectory() as tmp:
        # A mail the web gateway fails on (a 5xx), every time; then a VIP's
        # reply in the same thread.
        gw = t.load(tmp)
        hold = gw.RAIL_HOLD_ATTEMPTS
        assert hold == 3
        senders(gw, vip={"boss@corp.example"})
        state = {"heal": False}
        events = t.rail(gw, lambda kind, payload: "ok" if state["heal"] or payload.get(
            "message_id") != "big@x.org" else "unanswered", alive=True)
        mirror, box, clock = begin(gw)
        gw.MIRROR = mirror
        box.put("INBOX", msg("big@x.org", sender="ann@x.org", subject="Contract", day=38))
        mirror.poll()
        clock[0] += 1
        box.put("INBOX", msg("vip@x.org", sender="boss@corp.example", subject="Re: Contract",
                             irt="big@x.org", refs=["big@x.org"], day=39))
        mirror.poll()
        records = t.by_id(gw)
        assert records["vip@x.org"]["chat"] == records["big@x.org"]["chat"]
        # While the failure is young the reply waits behind the mail it
        # answers, its marker naming the chat it was queued in ...
        marker = json.loads(mirror.index.one(
            "SELECT payload FROM outbox WHERE kind = 'offer' ORDER BY seq DESC")["payload"])
        assert marker == {"message_id": "vip@x.org", "chat": records["big@x.org"]["chat"]}
        while offered(events).count("big@x.org") < hold:
            assert "vip@x.org" not in offered(events), offered(events)
            assert mirror.health_notice() is None
            _until_due(mirror, clock)
        # ... and once the mail has failed RAIL_HOLD_ATTEMPTS times it steps
        # aside: the reply is offered in the same pass, and rings (a VIP's).
        assert offered(events) == ["big@x.org"] * hold + ["vip@x.org"], offered(events)
        assert [p for _k, p in events][-1]["gate"]["vip"] is True
        assert t.by_id(gw)["vip@x.org"]["delivered"] is True
        assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
        row = offers(mirror)["big@x.org"]
        assert (row["queued"], row["attempts"], row["failed"]) == (0, hold, NOW), row
        # /health says one event keeps failing, and since when.
        assert gw._health_snapshot()["waiting"]["failing"] == 1
        assert gw._health_snapshot()["waiting"]["failing_since"] == NOW
        notice = mirror.health_notice()
        assert notice.startswith("1 rail event(s) keep failing while others are taken"), notice
        # It is still offered, on its own clock, holding nothing back: a
        # later mail of the thread goes at once.
        clock[0] += 1
        box.put("INBOX", msg("ann2@x.org", sender="ann@x.org", subject="Re: Contract",
                             irt="vip@x.org", refs=["big@x.org", "vip@x.org"], day=39))
        mirror.poll()
        assert offered(events)[-1] == "ann2@x.org" and t.by_id(gw)["ann2@x.org"]["delivered"]
        _until_due(mirror, clock)
        assert offered(events).count("big@x.org") == hold + 1
        assert offers(mirror)["big@x.org"]["attempts"] == hold + 1
        # Healed: taken, and /health says nothing more.
        state["heal"] = True
        _until_due(mirror, clock)
        assert t.by_id(gw)["big@x.org"]["delivered"] is True and offers(mirror) == {}
        assert mirror.health_notice() is None
    with tempfile.TemporaryDirectory() as tmp:
        # The same behind the user's own sent mail, which the web gateway
        # fails on every time: the VIP's reply to it is not held for good.
        gw = t.load(tmp)
        senders(gw, vip={"boss@corp.example"})
        state = {"heal": False}
        events = t.rail(gw, lambda kind, payload: "ok" if state["heal"] or payload.get(
            "message_id") != "s@example.org" else "unanswered", alive=True)
        mirror, box, clock = begin(gw)
        gw.MIRROR = mirror
        box.put("Sent", msg("s@example.org", sender=ME, to="boss@corp.example",
                            subject="Plans", day=38))
        mirror.poll()
        clock[0] += 1
        box.put("INBOX", msg("b@x.org", sender="boss@corp.example", subject="Re: Plans",
                             irt="s@example.org", refs=["s@example.org"], day=39))
        mirror.poll()
        told = lambda: [p["message_id"] for _k, p in events].count("s@example.org")  # noqa: E731
        while told() < hold:
            assert offered(events) == [], offered(events)
            _until_due(mirror, clock)
        assert offered(events) == ["b@x.org"] and events[-1][1]["gate"]["vip"] is True
        assert t.by_id(gw)["b@x.org"]["delivered"] is True
        # The told event keeps its row and its own clock, and holds nothing.
        row = mirror.index.one("SELECT kind, attempts, failed FROM outbox")
        assert (row["kind"], row["attempts"], row["failed"]) == ("inbound", hold, NOW), dict(row)
        assert mirror.health_notice().startswith("1 rail event(s) keep failing"), \
            mirror.health_notice()
        clock[0] += 1
        box.put("INBOX", msg("b2@x.org", sender="boss@corp.example", subject="Re: Plans",
                             irt="b@x.org", refs=["s@example.org", "b@x.org"], day=39))
        mirror.poll()
        assert offered(events)[-1] == "b2@x.org" and t.by_id(gw)["b2@x.org"]["delivered"]
        _until_due(mirror, clock)
        assert told() == hold + 1
        state["heal"] = True
        _until_due(mirror, clock)
        assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
        assert mirror.health_notice() is None
    print("ok: an event the rail keeps failing on holds its chat for RAIL_HOLD_ATTEMPTS "
          "tries, then steps aside — /health counts it")


def test_a_merge_keeps_an_offer_in_its_old_chats_hold():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        senders(gw, vip={"boss@corp.example"})
        state = {"heal": False}
        events = t.rail(gw, lambda kind, payload: "ok" if state["heal"] or payload.get(
            "message_id") != "s@example.org" else "unanswered", alive=True)
        mirror, box, clock = begin(gw)
        # An old chat, taken.
        box.put("INBOX", msg("root@x.org", sender="boss@corp.example", subject="Plans", day=30))
        mirror.poll()
        # The user's own mail, answering a mail the mirror never saw: the
        # web gateway fails on it (young: it holds its chat).
        clock[0] += 1
        box.put("Sent", msg("s@example.org", sender=ME, to="boss@corp.example",
                            subject="Re: Plans", irt="lost@x.org", refs=["lost@x.org"], day=38))
        mirror.poll()
        # The VIP answers it: its marker is queued in the held chat.
        clock[0] += 1
        box.put("INBOX", msg("b@x.org", sender="boss@corp.example", subject="Re: Plans",
                             irt="s@example.org", refs=["lost@x.org", "s@example.org"], day=39))
        mirror.poll()
        lost = t.by_id(gw)["s@example.org"]["chat"]
        assert t.by_id(gw)["b@x.org"]["chat"] == lost != t.by_id(gw)["root@x.org"]["chat"]
        # A mail that joins the two chats: b's mail is in the other chat now.
        clock[0] += 1
        box.put("INBOX", msg("c@x.org", sender="ann@x.org", subject="Re: Plans",
                             refs=["root@x.org", "lost@x.org"], irt="root@x.org", day=39))
        mirror.poll()
        records = t.by_id(gw)
        assert records["b@x.org"]["chat"] == records["root@x.org"]["chat"] != lost
        # Still held: neither b nor the merge goes ahead of s.
        assert "b@x.org" not in offered(events), offered(events)
        assert not [k for k, _p in events if k == "merge"], events
        state["heal"] = True
        events.clear()
        _until_due(mirror, clock)
        # Then in the order they were queued: the mail answered first, the
        # reply (offered under the chat it is in now), the merge.
        heard = [(k, p.get("message_id")) for k, p in events]
        assert heard == [("inbound", "s@example.org"), ("inbound", "b@x.org"),
                         ("merge", None), ("inbound", "c@x.org")], heard
        assert events[1][1]["chat"] == records["root@x.org"]["chat"]
        assert t.by_id(gw)["b@x.org"]["delivered"] is True
    print("ok: a merge does not take an offer out of the hold of the chat it was queued in")


def test_a_removal_the_rail_fails_on_keeps_its_place():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        # On the wire: a 5xx (or 408, 429) for a merge or a removal is a
        # failure about that event; only a 404 is an older web gateway that
        # does not know the endpoint.
        server = _served(gw)
        try:
            for status, expected in ((500, "unanswered"), (503, "unanswered"),
                                     (429, "unanswered"), (408, "unanswered"),
                                     (404, "later")):
                _Rail.status = status
                for kind in ("removed", "merge"):
                    assert gw._rail_send(kind, {}) == expected, (kind, status)
        finally:
            server.shutdown()
        # A removal the web gateway fails on, and the mail coming back: the
        # chat hears them in order.
        gw = t.load(tmp)
        state = {"heal": False}
        events = t.rail(gw, lambda kind, payload: "unanswered" if kind == "removed"
                        and not state["heal"] else "ok", alive=True)
        mirror, box, clock = begin(gw)
        raw_mail = msg("a@x.org", sender="ann@x.org", day=39)
        uid = box.put("INBOX", raw_mail)
        mirror.poll()
        events.clear()
        box.remove("INBOX", uid)
        mirror.poll()
        assert [k for k, _p in events] == ["removed"], events
        box.put("INBOX", raw_mail)
        mirror.poll()
        assert [k for k, _p in events] == ["removed"], events
        state["heal"] = True
        _until_due(mirror, clock)
        assert [k for k, _p in events] == ["removed", "removed", "inbound"], events
        assert t.by_id(gw)["a@x.org"]["removed_at"] is None
        assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
    print("ok: a merge or removal the rail fails on keeps its place; only a 404 holds nothing")


class _Stop(BaseException):
    """The process is killed here."""


def _stopped(mirror, sql: str, *, after: bool):
    """Kill the process at the first outbox write that starts with ``sql``:
    just before it, or just after it is committed. Returns the undo."""
    real = mirror.index.run

    def run(statement, args=()):
        if statement.startswith(sql):
            mirror.index.run = real
            if not after:
                raise _Stop()
            real(statement, args)
            raise _Stop()
        return real(statement, args)
    mirror.index.run = run
    return lambda: setattr(mirror.index, "run", real)


def _rail_kinds(events, since=0) -> list:
    return [(k, p.get("message_id") if k == "inbound" else None)
            for k, p in events[since:]]


def test_a_late_event_is_sent_as_its_mail_is_now():
    hold = None
    for stop in (None, "before", "after"):
        with tempfile.TemporaryDirectory() as tmp:
            # A removal the web gateway fails on until it has stepped aside;
            # then the mail comes back — announced at once, the removal
            # holding nothing any more — and the fault is fixed. The late
            # removal is not heard: the mail is there, the chat must not be
            # archived nor the mail forgotten. (And so wherever a stop comes
            # at the write that drops it, with a restart.)
            gw = t.load(tmp)
            hold = gw.RAIL_HOLD_ATTEMPTS
            state = {"heal": False}
            events = t.rail(gw, lambda kind, payload: "unanswered" if kind == "removed"
                            and not state["heal"] else "ok", alive=True)
            mirror, box, clock = begin(gw)
            raw = msg("a@x.org", sender="ann@x.org", day=39)
            uid = box.put("INBOX", raw)
            mirror.poll()
            events.clear()
            box.remove("INBOX", uid)
            mirror.poll()
            while [k for k, _p in events].count("removed") < hold:
                _until_due(mirror, clock)
            assert mirror.index.one("SELECT attempts FROM outbox")["attempts"] == hold
            box.put("INBOX", raw)
            mirror.poll()
            assert _rail_kinds(events)[-1] == ("inbound", "a@x.org"), events
            assert t.by_id(gw)["a@x.org"]["removed_at"] is None
            state["heal"] = True
            heard = len(events)
            if stop:
                undo = _stopped(mirror, "DELETE FROM outbox", after=stop == "after")
                try:
                    _until_due(mirror, clock)
                    raise AssertionError("no stop")
                except _Stop:
                    pass
                undo()
                mirror = again(gw, box, clock)
            _until_due(mirror, clock)
            _until_due(mirror, clock)
            assert _rail_kinds(events, heard) == [], events[heard:]
            assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
    with tempfile.TemporaryDirectory() as tmp:
        # Two mails of a chat leave together; the removal steps aside, one of
        # them comes back: the late removal names the other alone, and the
        # chat's live count as it is now.
        gw = t.load(tmp)
        state = {"heal": False}
        events = t.rail(gw, lambda kind, payload: "unanswered" if kind == "removed"
                        and not state["heal"] else "ok", alive=True)
        mirror, box, clock = begin(gw)
        raw_a = msg("a@x.org", sender="ann@x.org", subject="Plans", day=38)
        raw_b = msg("b@x.org", sender="ann@x.org", subject="Re: Plans", irt="a@x.org",
                    refs=["a@x.org"], day=39)
        uids = [box.put("INBOX", raw_a), box.put("INBOX", raw_b)]
        mirror.poll()
        chat = t.by_id(gw)["a@x.org"]["chat"]
        assert t.by_id(gw)["b@x.org"]["chat"] == chat
        events.clear()
        for uid in uids:
            box.remove("INBOX", uid)
        mirror.poll()
        assert [p for k, p in events if k == "removed"][0]["live"] == 0, events
        while [k for k, _p in events].count("removed") < hold:
            _until_due(mirror, clock)
        box.put("INBOX", raw_b)
        mirror.poll()
        state["heal"] = True
        heard = len(events)
        _until_due(mirror, clock)
        late = [p for k, p in events[heard:] if k == "removed"]
        assert late == [{"channel": "email", "account": ACCOUNT, "chat": chat,
                         "message_ids": ["a@x.org"], "live": 1}], late
    with tempfile.TemporaryDirectory() as tmp:
        # The user's own sent mail, which the web gateway fails on until it
        # has stepped aside; then the user deletes it in a client, and the
        # removal is taken. When the fault is fixed the told event is not
        # heard: the chat must not show a mail that is gone.
        gw = t.load(tmp)
        state = {"heal": False}
        events = t.rail(gw, lambda kind, payload: "unanswered" if payload.get(
            "message_id") == "s@example.org" and not state["heal"] else "ok", alive=True)
        mirror, box, clock = begin(gw)
        uid = box.put("Sent", msg("s@example.org", sender=ME, to="ann@x.org",
                                  subject="Lunch", day=39))
        mirror.poll()
        told = lambda: [p.get("message_id") for _k, p in events].count(  # noqa: E731
            "s@example.org")
        while told() < hold:
            _until_due(mirror, clock)
        box.remove("Sent", uid)
        mirror.poll()
        assert t.by_id(gw)["s@example.org"]["removed_at"]
        assert [k for k, _p in events][-1] == "removed", events
        state["heal"] = True
        heard = len(events)
        _until_due(mirror, clock)
        assert _rail_kinds(events, heard) == [], events[heard:]
        assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
    with tempfile.TemporaryDirectory() as tmp:
        # One that is there is told as it is now — under the chat a merge
        # has put it in since — and a young failure keeps what it was queued
        # with (test_a_removal_the_rail_fails_on_keeps_its_place).
        gw = t.load(tmp)
        state = {"heal": False}
        events = t.rail(gw, lambda kind, payload: "unanswered" if payload.get(
            "message_id") == "s@example.org" and not state["heal"] else "ok", alive=True)
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("p@x.org", sender="bob@x.org", subject="Plans", day=37))
        mirror.poll()
        box.put("Sent", msg("s@example.org", sender=ME, to="ann@x.org", subject="Re: Plans",
                            irt="lost@x.org", refs=["lost@x.org"], day=38))
        mirror.poll()
        before = t.by_id(gw)["s@example.org"]["chat"]
        assert before != t.by_id(gw)["p@x.org"]["chat"]
        told = lambda: [p.get("message_id") for _k, p in events].count(  # noqa: E731
            "s@example.org")
        while told() < hold:
            _until_due(mirror, clock)
        box.put("INBOX", msg("c@x.org", sender="bob@x.org", subject="Re: Plans",
                             irt="p@x.org", refs=["p@x.org", "lost@x.org"], day=39))
        mirror.poll()
        now = t.by_id(gw)["s@example.org"]["chat"]
        assert "merge" in [k for k, _p in events], events
        state["heal"] = True
        heard = len(events)
        _until_due(mirror, clock)
        late = [p for k, p in events[heard:] if p.get("message_id") == "s@example.org"]
        assert len(late) == 1 and late[0]["chat"] == now and late[0]["direction"] == "out", \
            (before, now, late)
        assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
    print("ok: an event that stepped aside is sent as its mail is now — a removal names "
          "what is still gone, a told mail that is gone is not told, also over a stop")


def test_a_removal_waits_for_the_merge_of_its_chat():
    for case in ("plain", "back", "stop-before", "stop-after"):
        with tempfile.TemporaryDirectory() as tmp:
            # Two chats, and a mail that joins them: the merge the web
            # gateway fails on until it has stepped aside. Then a mail of the
            # absorbed chat is deleted in a client: its removal names the
            # surviving chat, where the web gateway does not have the mail
            # yet — it waits for the merge, whatever the merge's attempts,
            # and holds nothing else back.
            gw = t.load(tmp)
            hold = gw.RAIL_HOLD_ATTEMPTS
            state = {"heal": False}
            events = t.rail(gw, lambda kind, payload: "unanswered" if kind == "merge"
                            and not state["heal"] else "ok", alive=True)
            mirror, box, clock = begin(gw)
            box.put("INBOX", msg("root@x.org", sender="ann@x.org", subject="Plans", day=37))
            mirror.poll()
            raw_m = msg("m@x.org", sender="bob@x.org", subject="Re: Plans", irt="lost@x.org",
                        refs=["lost@x.org"], day=38)
            uid_m = box.put("INBOX", raw_m)
            mirror.poll()
            absorbed = t.by_id(gw)["m@x.org"]["chat"]
            assert absorbed != t.by_id(gw)["root@x.org"]["chat"]
            box.put("INBOX", msg("c@x.org", sender="ann@x.org", subject="Re: Plans",
                                 irt="root@x.org", refs=["root@x.org", "lost@x.org"], day=39))
            mirror.poll()
            records = t.by_id(gw)
            chat = records["c@x.org"]["chat"]
            assert records["m@x.org"]["chat"] == records["root@x.org"]["chat"] == chat
            assert chat != absorbed
            while [k for k, _p in events].count("merge") < hold:
                _until_due(mirror, clock)
            box.remove("INBOX", uid_m)
            if case.startswith("stop"):
                undo = _stopped(mirror, "UPDATE outbox SET payload",
                                after=case == "stop-after")
                try:
                    mirror.poll()
                    raise AssertionError("no stop")
                except _Stop:
                    pass
                undo()
                mirror = again(gw, box, clock)
            mirror.poll()
            assert t.by_id(gw)["m@x.org"]["removed_at"]
            assert "removed" not in [k for k, _p in events], events
            row = mirror.index.one("SELECT payload FROM outbox WHERE kind = 'removed'")
            assert json.loads(row["payload"]).get(gw.OVERTAKEN) is True, dict(row)
            # Its chat's later events go on meanwhile.
            clock[0] += 1
            box.put("INBOX", msg("d@x.org", sender="ann@x.org", subject="Re: Plans",
                                 irt="c@x.org", refs=["root@x.org", "c@x.org"], day=39))
            mirror.poll()
            assert offered(events)[-1] == "d@x.org" and t.by_id(gw)["d@x.org"]["delivered"]
            assert "removed" not in [k for k, _p in events], events
            if case == "back":
                # The mail comes back while the removal waits, and is told so
                # (holding nothing): the removal is not heard after the merge.
                box.put("INBOX", raw_m)
                mirror.poll()
                assert t.by_id(gw)["m@x.org"]["removed_at"] is None
            state["heal"] = True
            heard = len(events)
            _until_due(mirror, clock)
            _until_due(mirror, clock)
            kinds = [k for k, _p in events[heard:]]
            if case == "back":
                assert kinds == ["merge"], events[heard:]
            else:
                assert kinds == ["merge", "removed"], events[heard:]
                assert events[-1][1] == {"channel": "email", "account": ACCOUNT, "chat": chat,
                                         "message_ids": ["m@x.org"], "live": 3}, events[-1]
            assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
    with tempfile.TemporaryDirectory() as tmp:
        # Nothing else waits for a merge that stepped aside: the user's own
        # mail in the chat, and a removal of another chat, go on.
        gw = t.load(tmp)
        events = t.rail(gw, lambda kind, payload: "unanswered" if kind == "merge" else "ok",
                        alive=True)
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("root@x.org", sender="ann@x.org", subject="Plans", day=36))
        uid_x = box.put("INBOX", msg("x@y.org", sender="zed@y.org", subject="Other", day=36))
        mirror.poll()
        box.put("INBOX", msg("m@x.org", sender="bob@x.org", subject="Re: Plans",
                             irt="lost@x.org", refs=["lost@x.org"], day=37))
        mirror.poll()
        box.put("INBOX", msg("c@x.org", sender="ann@x.org", subject="Re: Plans",
                             irt="root@x.org", refs=["root@x.org", "lost@x.org"], day=38))
        mirror.poll()
        while [k for k, _p in events].count("merge") < gw.RAIL_HOLD_ATTEMPTS:
            _until_due(mirror, clock)
        box.put("Sent", msg("s@example.org", sender=ME, to="ann@x.org", subject="Re: Plans",
                            irt="c@x.org", refs=["root@x.org", "c@x.org"], day=39))
        box.remove("INBOX", uid_x)
        mirror.poll()
        assert ("inbound", "s@example.org") in _rail_kinds(events), events
        assert [p["chat"] for k, p in events if k == "removed"] == [
            t.by_id(gw)["x@y.org"]["chat"]], events
    with tempfile.TemporaryDirectory() as tmp:
        # Decision AV: a removal queued BEFORE a merge of its chat does not
        # wait for it — it names its chat as the web gateway still has it,
        # and goes in its order however long the merge fails.
        gw = t.load(tmp)
        state = {"down": False}
        events = t.rail(gw, lambda kind, payload: "down" if state["down"] else (
            "unanswered" if kind == "merge" else "ok"), alive=True)
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("root@x.org", sender="ann@x.org", subject="Plans", day=36))
        mirror.poll()
        uid_m = box.put("INBOX", msg("m@x.org", sender="ann@x.org", subject="Re: Plans",
                                     irt="root@x.org", refs=["root@x.org"], day=37))
        mirror.poll()
        box.put("INBOX", msg("o@x.org", sender="bob@x.org", subject="Other",
                             irt="lost@x.org", refs=["lost@x.org"], day=37))
        mirror.poll()
        chat = t.by_id(gw)["m@x.org"]["chat"]
        assert chat != t.by_id(gw)["o@x.org"]["chat"]
        state["down"] = True
        box.remove("INBOX", uid_m)
        mirror.poll()
        box.put("INBOX", msg("c@x.org", sender="ann@x.org", subject="Re: Plans",
                             irt="root@x.org", refs=["root@x.org", "lost@x.org"], day=38))
        mirror.poll()
        kinds = [r["kind"] for r in mirror.index.rows("SELECT kind FROM outbox ORDER BY seq")]
        assert kinds.index("removed") < kinds.index("merge"), kinds
        state["down"] = False
        clock[0] += 3600
        heard = len(events)
        for _ in range(gw.RAIL_HOLD_ATTEMPTS + 1):
            _until_due(mirror, clock)
        removed = [p for k, p in events[heard:] if k == "removed"]
        assert len(removed) == 1 and removed[0]["chat"] == chat \
            and removed[0]["message_ids"] == ["m@x.org"], events[heard:]
        assert not mirror.index.one("SELECT seq FROM outbox WHERE kind = 'removed'")
        assert mirror.index.one("SELECT attempts FROM outbox WHERE kind = 'merge'")[
            "attempts"] >= gw.RAIL_HOLD_ATTEMPTS
    print("ok: a removal waits for a merge of its chat that stepped aside — nothing else "
          "does, and only one queued before it — and is then sent as its mails are, also over a stop")


def test_a_quiet_outage_does_not_blame_one_mail():
    with tempfile.TemporaryDirectory() as tmp:
        # The web gateway fails on every event, and the mailbox is quiet: a
        # mail every six hours, polls every five minutes — too few calls a
        # pass for "takes no mail". /health never says others are taken.
        gw = t.load(tmp)
        state = {"answer": "unanswered"}
        events = t.rail(gw, lambda kind, payload: state["answer"], alive=True)
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("before@x.org", day=30))
        state["answer"] = "ok"
        mirror.poll()
        assert t.by_id(gw)["before@x.org"]["delivered"]   # taken before the fault
        state["answer"] = "unanswered"
        clock[0] += 60
        said = set()
        for step in range(12 * 12):
            if step % 72 == 0:
                box.put("INBOX", msg(f"q{step}@x.org", sender=f"p{step}@corp.example",
                                     day=31 + step / 72))
            mirror.poll()
            notice = mirror.health_notice()
            if notice and "rail event(s) keep failing" in notice:
                said.add(notice.split(":")[0])
            assert not notice or "while others are taken" not in notice, (step, notice)
            clock[0] += 300
        assert any("keep failing and the rail has taken nothing since" in n for n in said), said
        # A restart does not forget when the rail last took something.
        mirror = again(gw, box, clock)
        mirror.poll()
        assert "has taken nothing since" in mirror.health_notice()
        # Another mail is taken while the others still fail: now one mail is
        # what keeps failing — across a restart too.
        events[:] = []
        t.rail(gw, lambda kind, payload: "ok" if payload.get(
            "message_id") == "fresh@x.org" else "unanswered", alive=True)
        box.put("INBOX", msg("fresh@x.org", day=33))
        mirror.poll()
        assert t.by_id(gw)["fresh@x.org"]["delivered"]
        assert mirror.health_notice().startswith("2 rail event(s) keep failing while others "
                                                 "are taken"), mirror.health_notice()
        mirror = again(gw, box, clock)
        assert "while others are taken" in mirror.health_notice()
    print("ok: /health says others are taken only when the rail took one since the oldest "
          "failing event first failed")


def test_the_notice_is_worded_from_the_newest_failing_event():
    with tempfile.TemporaryDirectory() as tmp:
        # A poison mail has stepped aside for a day while other mail is
        # taken; then the web gateway fails on everything, on a quiet
        # mailbox. The rail took something after the poison first failed,
        # but nothing after the newest failing event began to fail: the
        # notice is about the rail, not about one mail.
        gw = t.load(tmp)
        state = {"outage": False}

        def outcome(kind, payload):
            if state["outage"] or payload.get("message_id") == "poison@x.org":
                return "unanswered"
            return "ok"
        t.rail(gw, outcome, alive=True)
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("poison@x.org", sender="pp@corp.example", subject="P", day=39))
        k = 0
        for step in range(24 * 12):
            if step % 72 == 10:
                box.put("INBOX", msg(f"ok{k}@x.org", sender=f"q{k}@corp.example", day=39))
                k += 1
            mirror.poll()
            clock[0] += 300
        assert mirror.health_notice().startswith(
            "1 rail event(s) keep failing while others are taken"), mirror.health_notice()
        state["outage"] = True
        said = []
        for step in range(24 * 12):
            if step % 72 == 0:
                box.put("INBOX", msg(f"m{k}@x.org", sender=f"p{k}@corp.example", day=39))
                k += 1
            mirror.poll()
            notice = mirror.health_notice() or ""
            if mirror.waiting()["failing"] > 1 and not mirror._rail_refusing:
                said.append(notice)
            clock[0] += 300
        assert said, "the outage's events never stepped aside"
        assert all("has taken nothing since the newest of them began to fail" in n
                   for n in said), sorted(set(n[:80] for n in said))
        # A restart keeps it so; a mail taken again makes it about the mails.
        mirror = again(gw, box, clock)
        assert "has taken nothing since the newest" in mirror.health_notice()
        state["outage"] = False
        box.put("INBOX", msg("fresh@x.org", day=39))
        mirror.poll()
        assert t.by_id(gw)["fresh@x.org"]["delivered"]
        assert "while others are taken" in mirror.health_notice(), mirror.health_notice()
    print("ok: /health says others are taken only when one was taken after the newest "
          "failing event began to fail")


def test_an_index_without_a_take_says_nothing_of_one():
    with tempfile.TemporaryDirectory() as tmp:
        # An index from before the gateway kept when the rail last took a
        # mail: a poison mail has stepped aside while others were taken.
        # Until the first take, /health says neither that others are taken
        # nor that nothing is.
        gw = t.load(tmp)
        state = {"answer": None}

        def outcome(kind, payload):
            if state["answer"]:
                return state["answer"]
            return "unanswered" if payload.get("message_id") == "poison@x.org" else "ok"
        t.rail(gw, outcome, alive=True)
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("poison@x.org", sender="pp@corp.example", subject="P", day=39))
        for step in range(60):
            if step % 10 == 5:
                box.put("INBOX", msg(f"ok{step}@x.org", sender=f"q{step}@corp.example", day=39))
            mirror.poll()
            clock[0] += 300
        assert "while others are taken" in mirror.health_notice()
        mirror.index.run("DELETE FROM meta WHERE key = 'rail_took'")
        mirror = again(gw, box, clock)
        for _ in range(12):
            mirror.poll()
            notice = mirror.health_notice()
            assert notice.startswith("1 rail event(s) keep failing — the oldest for "), notice
            assert "taken" not in notice, notice
            clock[0] += 300
        # A whole pass that takes nothing says so, as ever.
        state["answer"] = "unanswered"
        for n in range(gw.RAIL_DOWN_NOTICE):
            box.put("INBOX", msg(f"d{n}@x.org", sender=f"d{n}@corp.example", day=39))
        mirror.poll()
        assert mirror.health_notice().startswith(
            "the web gateway's chats rail answers but takes no mail"), mirror.health_notice()
        # The first take is recorded: the notice is worded from it again.
        state["answer"] = None
        box.put("INBOX", msg("fresh@x.org", day=39))
        clock[0] += 300
        mirror.poll()
        assert t.by_id(gw)["fresh@x.org"]["delivered"]
        assert mirror.index.get("rail_took") is not None
        mirror = again(gw, box, clock)
        assert "while others are taken" in mirror.health_notice(), mirror.health_notice()
    print("ok: with no take ever recorded, /health is worded neutrally until the first take "
          "or a pass that takes nothing")


def test_an_older_index_is_upgraded_column_by_column():
    columns = [("outbox", "attempts", "INTEGER NOT NULL DEFAULT 0"),
               ("outbox", "due", "REAL NOT NULL DEFAULT 0"), ("outbox", "failed", "REAL"),
               ("offers", "failed", "REAL"), ("lost", "owed", "INTEGER NOT NULL DEFAULT 0"),
               ("mails", "backfill", "INTEGER"), ("mails", "seen", "INTEGER NOT NULL DEFAULT 0")]
    lost_owed = ", owed INTEGER NOT NULL DEFAULT 0\n)"
    mails_seen = ",\n    backfill INTEGER, seen INTEGER NOT NULL DEFAULT 0\n)"
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        older = gw._INDEX_SCHEMA.replace(lost_owed, "\n)", 1).replace(
            mails_seen, "\n)", 1).replace(
            ",\n    attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0, "
            "failed REAL\n);", "\n);", 1).replace(
            ", queued INTEGER NOT NULL DEFAULT 0, failed REAL\n)",
            ", queued INTEGER NOT NULL DEFAULT 0\n)", 1).replace(
            ", removed INTEGER NOT NULL DEFAULT 0, arrived REAL\n)",
            ", removed INTEGER NOT NULL DEFAULT 0\n)", 1)
        mails_table = older.split("CREATE TABLE IF NOT EXISTS mails (", 1)[1].split(";", 1)[0]
        assert older.count("failed") == 0 and "arrived" not in mails_table
        assert "owed" not in older and "seen" not in mails_table
        # An index made before the columns, and one whose upgrade stopped
        # after each of them (every ALTER commits by itself).
        for done in range(len(columns) + 1):
            path = Path(tmp) / f"older{done}.sqlite"
            conn = gw.sqlite3.connect(str(path), isolation_level=None)
            conn.executescript(older)
            conn.execute("PRAGMA user_version = 1")
            conn.execute("INSERT INTO outbox (kind, payload) VALUES ('inbound', '{}')")
            conn.execute("INSERT INTO offers (message_id) VALUES ('a@x.org')")
            conn.execute("INSERT INTO lost (message_id, noticed) VALUES ('b@x.org', 1)")
            for table, column, kind in columns[:done]:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            conn.close()
            for _start in range(2):
                index = gw.MailboxIndex(path)
                assert [dict(r) for r in index.rows(
                    "SELECT attempts, due, failed FROM outbox")] == [
                    {"attempts": 0, "due": 0.0, "failed": None}], done
                assert index.one("SELECT failed FROM offers")["failed"] is None
                assert "arrived" in {r["name"] for r in index.rows("PRAGMA table_info(mails)")}
                assert index.one("SELECT owed FROM lost")["owed"] == 0
                assert {"backfill", "seen"} <= {r["name"] for r in index.rows(
                    "PRAGMA table_info(mails)")}
                index.close()
        # And the index of the mirror without the gate (the writing phase,
        # with the reader's `lost` hints, the aliases, the settling and the
        # note of a move under way): no offers, no news, an outbox of three
        # columns — opened as it is, its rows kept, and with no take on
        # record (what it took is not known: Mirror.health_notice).
        # (Its `lost` kept no `owed`, its `mails` no `backfill` and `seen`.)
        cut = gw._INDEX_SCHEMA.split("CREATE TABLE IF NOT EXISTS outbox (", 1)[0] \
            .replace(lost_owed, "\n)", 1).replace(mails_seen, "\n)", 1)
        writing = cut + ("CREATE TABLE IF NOT EXISTS outbox (\n    seq INTEGER PRIMARY KEY "
                         "AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL\n);\n")
        assert "offers" not in writing and "news" not in writing and "lost" in writing
        assert "owed" not in writing and mails_seen not in writing
        path = Path(tmp) / "writing.sqlite"
        conn = gw.sqlite3.connect(str(path), isolation_level=None)
        conn.executescript(writing)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO mails (message_id, record, direction, chat, ts, arrived) "
                     "VALUES ('a@x.org', 'r.nt', 'in', 'a@x.org', 1.0, 1.0)")
        conn.execute("INSERT INTO lost (message_id, chat, noticed) VALUES ('b@x.org', 'a@x.org', 1)")
        conn.execute("INSERT INTO aliases VALUES ('p@provider.example', 'm@x.org')")
        conn.execute("INSERT INTO meta VALUES ('moving', '{}')")
        conn.execute("INSERT INTO outbox (kind, payload) VALUES ('removed', '{}')")
        conn.close()
        for _start in range(2):
            index = gw.MailboxIndex(path)
            assert index.one("SELECT COUNT(*) AS n FROM offers")["n"] == 0
            assert index.one("SELECT COUNT(*) AS n FROM news")["n"] == 0
            assert [dict(r) for r in index.rows("SELECT kind, attempts, due, failed FROM outbox")] \
                == [{"kind": "removed", "attempts": 0, "due": 0.0, "failed": None}]
            assert dict(index.one("SELECT chat, owed FROM lost")) == {"chat": "a@x.org",
                                                                      "owed": 0}
            assert index.get("moving") == "{}" and index.get("rail_took") is None
            assert dict(index.one("SELECT backfill, seen FROM mails")) == {"backfill": None,
                                                                           "seen": 0}
            assert index.one("SELECT message_id FROM aliases")["message_id"] == "m@x.org"
            index.close()
    print("ok: an older index gains each column on its own test, also after a stop between "
          "two — and one of the mirror before the gate opens as it is")


def test_a_rail_that_takes_no_mail_says_so_and_is_bounded():
    with tempfile.TemporaryDirectory() as tmp:
        # The web gateway answers, and fails on every mail (a broken chat
        # store; a proxy in front of a stopped backend).
        gw = t.load(tmp)
        senders(gw, vip={"boss@corp.example"})
        state = {"answer": "unanswered"}
        events = t.rail(gw, lambda kind, payload: state["answer"])
        mirror, box, clock = begin(gw)
        gw.MIRROR = mirror
        for n in range(5):
            box.put("INBOX", msg(f"vip{n}@x.org", sender="boss@corp.example",
                                 subject=f"Urgent {n}", day=31 + n))
        mirror.poll()
        assert len(offered(events)) == 5 and mirror._rail_down == 0
        notice = mirror.health_notice()
        assert notice and "answers but takes no mail" in notice and "5 mail(s)" in notice, notice
        assert gw._health_snapshot()["waiting"]["rail"] == 5
        for _ in range(12):
            clock[0] += 3600
            mirror.poll()
        assert "answers but takes no mail" in mirror.health_notice()
        # The fault is fixed: what waited over it — each wait grown to an
        # hour by now — is offered at once, in the pass that sees a mail taken.
        state["answer"] = "ok"
        clock[0] += gw.RETRY_MAX_SECONDS
        events.clear()
        mirror.poll()
        assert sorted(offered(events)) == [f"vip{n}@x.org" for n in range(5)], offered(events)
        assert all(r["delivered"] for r in t.records(gw))
        assert mirror.health_notice() is None
    with tempfile.TemporaryDirectory() as tmp:
        # One mail failing, alone, says nothing of the rail — only of itself,
        # once it has stepped aside (test_a_failing_event_steps_aside).
        gw = t.load(tmp)
        events = t.rail(gw, lambda kind, payload: "unanswered" if payload.get(
            "message_id") == "poison@x.org" else "ok")
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("poison@x.org", day=30))
        for _ in range(5):
            clock[0] += gw.RETRY_MAX_SECONDS
            mirror.poll()
        notice = mirror.health_notice()
        assert "answers but takes no mail" not in notice, notice
        # (Nothing else was there to be taken: the notice does not claim
        # that others are — test_a_quiet_outage_does_not_blame_one_mail.)
        assert notice.startswith("1 rail event(s) keep failing and the rail has taken "
                                 "nothing since"), notice
        box.put("INBOX", msg("fine@x.org", day=31))
        mirror.poll()
        notice = mirror.health_notice()
        assert notice.startswith("1 rail event(s) keep failing while others are taken"), notice
    with tempfile.TemporaryDirectory() as tmp:
        # Up (it answers a look), and no answer in time for any event: a
        # pass spends at most RAIL_SILENT_MAX timeouts, and the rail counts
        # as not answering — /health says so after RAIL_DOWN_NOTICE passes.
        gw = t.load(tmp)
        events = t.rail(gw, "uncertain", alive=True)
        mirror, box, clock = begin(gw)
        for n in range(12):
            box.put("INBOX", msg(f"m{n}@x.org", sender=f"p{n}@corp.example",
                                 subject=f"S{n}", day=39))
        mirror.poll()
        assert len(events) == gw.RAIL_SILENT_MAX, len(events)
        assert mirror._rail_down == 1
        for _ in range(gw.RAIL_DOWN_NOTICE - 1):
            clock[0] += gw.RAIL_RETRY_MAX_SECONDS
            mirror.poll()
        assert len(events) == gw.RAIL_SILENT_MAX * gw.RAIL_DOWN_NOTICE
        assert "chats rail is not answering" in mirror.health_notice()
    with tempfile.TemporaryDirectory() as tmp:
        # The same on the wire: a web gateway whose GET answers at once and
        # whose inbound handler outlasts MAIL_RAIL_TIMEOUT for every mail.
        gw = t.load(tmp, MAIL_RAIL_TIMEOUT="1")
        mirror, box, clock = begin(gw)
        for n in range(12):
            box.put("INBOX", msg(f"m{n}@x.org", sender=f"p{n}@corp.example",
                                 subject=f"S{n}", day=39))
        server = _served(gw)
        _Rail.delay = 1.5
        try:
            started = time.monotonic()
            mirror.poll()
            took = time.monotonic() - started
        finally:
            server.shutdown()
        posts = [path for path, body in _Rail.seen if body is not None]
        assert len(posts) == gw.RAIL_SILENT_MAX, _Rail.seen
        assert took < gw.RAIL_SILENT_MAX * (1 + gw.RAIL_PROBE_TIMEOUT) + 2, took
        assert mirror._rail_down == 1
    print("ok: a rail that answers and takes no mail says so on /health, and one too slow "
          "for every event costs a pass a bounded time")


def test_what_has_arrived_is_offered_while_the_mailbox_is_away():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw)
        senders(gw, vip={"boss@corp.example"})
        state = {"answer": "down"}
        events = t.rail(gw, lambda kind, payload: state["answer"])
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("v@x.org", sender="boss@corp.example", day=39))
        mirror.poll()
        assert t.by_id(gw)["v@x.org"]["delivered"] is False
        # The web gateway is back; the mailbox is not.
        state["answer"] = "ok"
        box.fail = gw.MailboxError("IMAP login failed")
        mirror._rail_wait_until = 0.0
        loop: dict = {}
        assert gw._loop_step(mirror, loop) == gw.MAIL_POLL_SECONDS
        assert offered(events)[-1] == "v@x.org" and events[-1][1]["gate"]["vip"] is True
        assert t.by_id(gw)["v@x.org"]["delivered"] is True
        assert gw._health_snapshot()["connected"] is False
    print("ok: a mailbox that does not answer holds back what arrives, not what has")


def test_a_mail_that_left_while_owed_is_owed_when_it_comes_back():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        senders(gw, vip={"boss@corp.example"})
        state = {"answer": "down"}
        events = t.rail(gw, lambda kind, payload: state["answer"])
        mirror, box, clock = begin(gw)
        mail = msg("v@x.org", sender="boss@corp.example", subject="Urgent", day=39)
        box.put("INBOX", mail)
        mirror.poll()
        # Moved out of the INBOX while the web gateway is away (a junk
        # filter), and back once it answers again.
        box.remove("INBOX", box.uid_of("INBOX", "v@x.org"))
        clock[0] += 120
        mirror.poll()
        assert t.by_id(gw)["v@x.org"]["removed_at"] and offers(mirror) == {}
        state["answer"] = "ok"
        box.put("INBOX", mail)
        clock[0] += gw.RAIL_RETRY_MAX_SECONDS
        mirror.poll()
        record = t.by_id(gw)["v@x.org"]
        assert record["removed_at"] is None and record["delivered"] is True
        last = [p for _k, p in events if p.get("message_id") == "v@x.org"][-1]
        assert last.get("handover") is True and last["gate"]["vip"] is True
    print("ok: a mail that left the mailbox while owed is offered when it comes back")


def test_a_news_mail_that_could_not_be_read_is_read_again():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw, news={"news.lists.example"})
        events = t.rail(gw)
        mirror, box, clock = begin(gw)
        real = gw._reading

        def reading(data, date):
            if b"issue1@" in data:
                raise ValueError("a moment the reader was in")
            return real(data, date)
        gw._reading = reading
        try:
            box.put("INBOX", msg("issue1@x.org", sender="news@pub.example", subject="Issue 1",
                                 list_id="<news.lists.example>", day=39))
            mirror.poll()
        finally:
            gw._reading = real
        # Not read: nothing filed, and no chat — an unreadable newsletter is
        # still none.
        assert gw.filed == [] and t.records(gw) == [] and events == []
        clock[0] += gw.SET_ASIDE_RETRY_SECONDS + 1
        mirror.poll()
        assert [item["title"] for item in gw.filed] == ["Issue 1"]
        assert t.records(gw) == [] and events == []
        assert mirror.index.one("SELECT skipped FROM locations WHERE message_id = ?",
                                ("issue1@x.org",))["skipped"] == "news"
    print("ok: a news mail that could not be read is read again, and filed once it reads")


def test_a_job_is_polled_on_the_rail_and_nowhere_else():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        base = "http://rail.invalid"
        assert gw._job_url("/jobs/ab12") == f"{base}/jobs/ab12"
        assert gw._job_url("/jobs/ab12?x=1") == f"{base}/jobs/ab12?x=1"
        for bad in ("/\t/evil.example/x", "/\n/evil.example/x", "/\r/evil.example/x",
                    "//evil.example/x", "http://evil.example/jobs/1", "jobs/1",
                    "/jobs/\x00", "/jobs/ 1", "/\\evil.example/x", "/jobs/é", "",
                    None, 7, "/" + "a" * 2000):
            assert gw._job_url(bad) is None, bad
        # On the rail: such an answer is no acceptance, and nothing is polled.
        jobs = FakeJobs(gw)
        try:
            senders(gw, vip={"boss@corp.example"})
            events = t.rail(gw, answer={"accepted": True, "job_url": "/\t/evil.example/x"})
            mirror, box, _clock = begin(gw)
            box.put("INBOX", msg("v@x.org", sender="boss@corp.example", day=39))
            mirror.poll()
            assert offered(events) == ["v@x.org"] and jobs.asked == []
            assert t.by_id(gw)["v@x.org"]["delivered"] is False
        finally:
            jobs.restore()
    print("ok: a job is polled where the rail is, checked as the URL is parsed")


def test_the_retired_news_names_are_read_as_fallbacks():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        assert gw.MAIL_NEWS_FOLDER is None and gw.MAIL_NEWS_EXCERPT_CHARS == 600
        # An empty folder keeps news in the INBOX — and stays apart from unset.
        gw = t.load(tmp, TRIAGE_NEWS_FOLDER="", TRIAGE_NEWS_EXCERPT_CHARS="120")
        assert gw.MAIL_NEWS_FOLDER == "" and gw.MAIL_NEWS_EXCERPT_CHARS == 120
        gw = t.load(tmp, TRIAGE_NEWS_FOLDER="Newsletters")
        assert gw.MAIL_NEWS_FOLDER == "Newsletters"
        # The new name wins where both are set.
        gw = t.load(tmp, TRIAGE_NEWS_FOLDER="Old", MAIL_NEWS_FOLDER="New",
                    TRIAGE_NEWS_EXCERPT_CHARS="120", MAIL_NEWS_EXCERPT_CHARS="300")
        assert gw.MAIL_NEWS_FOLDER == "New" and gw.MAIL_NEWS_EXCERPT_CHARS == 300
    print("ok: TRIAGE_NEWS_FOLDER / TRIAGE_NEWS_EXCERPT_CHARS are read where the new "
          "names are not set")


# --------------------------------------------------------------------------
# The gate on the mirror's own mechanisms: a record written again in its
# place, a record lost from the ledger, a provider's id, a move followed
# --------------------------------------------------------------------------

def _vip_turns(payload):
    """What the rail answers a mail the test lets the chat take: a VIP's is
    taken by a turn (a job), anyone else's plainly."""
    if payload["gate"]["vip"]:
        return {"accepted": True, "job_url": f"/jobs/{payload['message_id']}"}
    return {"accepted": True}


def test_a_placeholder_buys_no_turn_and_its_reading_is_offered_only_if_owed():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw)
        senders(gw, vip={"boss@corp.example"})
        jobs = FakeJobs(gw)
        take = {"taken@x.org", "boss-read@x.org", "boss-unread@x.org"}
        events = t.rail(gw, answer=lambda p: _vip_turns(p) if p["message_id"] in take else {})
        mirror, box, clock = begin(gw)
        real_trim = gw._trim.trim
        broken = {"owed", "taken", "boss-read", "boss-unread"}
        wait = gw.MAIL_UNREAD_OFFER_WAIT

        def trim(plain, html, subject=None, **evidence):
            if subject in broken:
                raise RecursionError("this mail, for now")
            return real_trim(plain, html, subject=subject, **evidence)
        gw._trim.trim = trim
        try:
            # A stranger's two, and a VIP's two.
            box.put("INBOX", msg("owed@x.org", sender="ann@x.org", subject="owed",
                                 body="The real words.\n", day=39))
            box.put("INBOX", msg("taken@x.org", sender="ann@x.org", subject="taken",
                                 body="Other words.\n", day=39))
            box.put("INBOX", msg("boss-read@x.org", sender="boss@corp.example",
                                 subject="boss-read", body="Boss words.\n", day=39))
            box.put("INBOX", msg("boss-unread@x.org", sender="boss@corp.example",
                                 subject="boss-unread", body="Late words.\n", day=39))
            mirror.poll()
            # The stranger's placeholders are each offered as they arrived —
            # from nobody: a turn would work a mail it cannot read, and its
            # done would deliver it. The chat takes one of them, plainly.
            first = {p["message_id"]: p for _k, p in events}
            assert sorted(first) == ["owed@x.org", "taken@x.org"], sorted(first)
            for mid in ("owed@x.org", "taken@x.org"):
                assert first[mid]["handover"] is True and first[mid]["gate"] == OPEN, first[mid]
                assert first[mid]["gate_sender"] == "" and "files" not in first[mid]
                assert t.by_id(gw)[mid]["gate_sender"] is None
            # (Changed: the VIP's are not.) Their header blocks name the VIP:
            # each keeps that gate sender, owed, and its offer waits for its
            # reading — no place in the outbox, due at the bound.
            for mid in ("boss-read@x.org", "boss-unread@x.org"):
                assert t.by_id(gw)[mid]["gate_sender"] == "boss@corp.example"
                assert t.by_id(gw)[mid]["delivered"] is False
                assert offers(mirror)[mid]["queued"] == 0
                assert offers(mirror)[mid]["due"] == clock[0] + wait, offers(mirror)[mid]
            assert jobs.asked == [] and sorted(offers(mirror)) == [
                "boss-read@x.org", "boss-unread@x.org", "owed@x.org"]
            # Half the wait on: the VIP's are read again, before their offers
            # are due. The one that reads now is offered as the mail it is —
            # by its gate sender, one ring and one turn on its real text.
            broken.discard("boss-read")
            events.clear()
            clock[0] += wait / 2
            mirror.poll()
            # (The stranger's that was not taken is offered again meanwhile.)
            assert offered(events) == ["boss-read@x.org", "owed@x.org"], events
            read = [p for _k, p in events if p.get("handover")][0]
            assert read["gate"]["vip"] is True and read["text"] == "Boss words."
            assert [job.url for job in jobs.asked] == ["http://rail.invalid/jobs/boss-read@x.org"]
            # The other has not read by the bound: offered as the placeholder
            # it is, under the gate sender its header block names — the VIP's
            # ring and turn go out for a mail that says it could not be shown.
            events.clear()
            clock[0] += wait / 2
            mirror.poll()
            assert offered(events) == ["boss-unread@x.org", "owed@x.org"], events
            late = [p for _k, p in events if p.get("handover")][0]
            assert late["gate"]["vip"] is True and late["gate_sender"] == "boss@corp.example"
            assert late["text"] == gw.UNREADABLE_TEXT
            assert len(jobs.asked) == 2
            for job in jobs.asked:
                job.done()
            # Read at last, each record written in its placeholder's place:
            # the one still owed is offered — by its gate sender now — and
            # the ones the chat took are told, once, as old mail: no second
            # offer, no second ring, no turn.
            broken.clear()
            events.clear()
            take.add("owed@x.org")
            # (The VIP's late one was read again in vain once already: its
            # next reading is a round later, as any placeholder's.)
            for _ in range(3):
                clock[0] += gw.SET_ASIDE_RETRY_SECONDS
                mirror.poll()
            assert offered(events) == ["owed@x.org"], events
            for mid, text in (("taken@x.org", "Other words."),
                              ("boss-unread@x.org", "Late words.")):
                told = [p for _k, p in events if p["message_id"] == mid]
                assert len(told) == 1 and told[0].get("backfill") is True, (mid, told)
                assert "handover" not in told[0] and told[0]["text"] == text
            owed = [p for _k, p in events if p["message_id"] == "owed@x.org"][-1]
            assert owed["gate"] == OPEN and owed["gate_sender"] == "ann@x.org"
            assert t.by_id(gw)["owed@x.org"]["delivered"] is True and offers(mirror) == {}
            assert len(jobs.asked) == 2
            assert [r["text"] for r in t.records(gw)].count(gw.UNREADABLE_TEXT) == 0
        finally:
            gw._trim.trim = real_trim
            jobs.restore()
    print("ok: a stranger's placeholder is offered from nobody, a VIP's waits for its reading "
          "and rings once; a reading is offered only if it was still owed — else told once")


def test_a_placeholder_read_under_another_id_is_owed_what_it_was():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw)
        senders(gw, vip={"boss@corp.example"})
        take = {"taken@x.org"}
        events = t.rail(gw, answer=lambda p: {"accepted": True} if p["message_id"] in take
                        else {})
        mirror, box, clock = begin(gw)
        raws = {mid: msg(mid, sender="boss@corp.example", subject=mid, day=39)
                for mid in ("owed@x.org", "taken@x.org")}
        bare = {mid: gw._bare_id(gw._head_of(raw)) for mid, raw in raws.items()}
        take.add(bare["taken@x.org"])
        real_reading, real_stub = gw._reading, gw.stub_mail

        def reading(data, date):
            if b"boss@corp.example" in data:
                raise ValueError("not this moment")
            return real_reading(data, date)

        def stub(head, date, reason):
            if b"boss@corp.example" in head:
                raise ValueError("not even its header block")
            return real_stub(head, date, reason)
        gw._reading, gw.stub_mail = reading, stub
        try:
            for raw in raws.values():
                box.put("INBOX", raw)
            mirror.poll()
        finally:
            gw._reading, gw.stub_mail = real_reading, real_stub
        # Not even the header blocks read: each a placeholder under an id of
        # its bytes, offered from nobody. The chat takes the one, not the
        # other.
        assert sorted(r["message_id"] for r in t.records(gw)) == sorted(bare.values())
        assert sorted(offers(mirror)) == [bare["owed@x.org"]]
        assert all(p["gate_sender"] == "" for _k, p in events)
        events.clear()
        take = {"owed@x.org", "taken@x.org"}
        clock[0] += gw.SET_ASIDE_RETRY_SECONDS
        mirror.poll()
        # Read at last, under their own ids, each in its placeholder's
        # stead (which is buried): the one still owed is offered under its
        # own id, by the gate; the one the chat took is told, not offered.
        records = {mid: r for mid, r in t.by_id(gw).items() if not r["removed_at"]}
        assert sorted(records) == ["owed@x.org", "taken@x.org"], sorted(records)
        assert offered(events) == ["owed@x.org"], events
        owed = [p for _k, p in events if p.get("message_id") == "owed@x.org"][-1]
        assert owed["gate"]["vip"] is True and owed["gate_sender"] == "boss@corp.example"
        taken = [p for _k, p in events if p.get("message_id") == "taken@x.org"]
        assert len(taken) == 1 and "handover" not in taken[0], taken
        assert sorted(p["message_ids"][0] for k, p in events if k == "removed") \
            == sorted(bare.values())
        assert all(r["delivered"] is True for r in records.values()) and offers(mirror) == {}
    print("ok: a placeholder under an id of its bytes, read under its own, is owed what it was")


def test_a_lost_records_mail_comes_back_told_and_rings_no_second_time():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw)
        senders(gw, vip={"boss@corp.example"})
        events = t.rail(gw)
        mirror, box, clock = begin(gw)
        box.put("INBOX", msg("v@x.org", sender="boss@corp.example", day=39))
        mirror.poll()
        assert offered(events) == ["v@x.org"] and t.by_id(gw)["v@x.org"]["delivered"] is True
        # The ledger restored from a backup without the record: the index
        # keeps what the row said as hints, and the mail is looked for by
        # its Message-ID — found, it is the mail it was, already heard of.
        for path, fields in list(gw._ibstore.iter_records(gw.INBOUND_STORE_DIR)):
            path.unlink()
        mirror.index.close()
        mirror.threads.close()
        mirror = again(gw, box, clock)
        assert mirror.lost() == 1
        events.clear()
        for _ in range(3):
            clock[0] += 61
            mirror.poll()
        assert mirror.lost() == 0 and t.by_id(gw)["v@x.org"]["delivered"] is True
        assert offered(events) == [], events
        told = [p for kind, p in events if kind == "inbound"]
        assert len(told) == 1 and told[0]["backfill"] is True and told[0]["gate"] == HELD
        assert offers(mirror) == {}
    print("ok: a lost record's mail found again is told as old mail, not offered a second time")


def test_a_record_lost_while_owed_comes_back_owed():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw)
        senders(gw, vip={"boss@corp.example"})
        jobs = FakeJobs(gw)
        up = [False]
        events = t.rail(gw, outcome=lambda kind, payload: "ok" if up[0] else "down",
                        answer=_vip_turns)
        try:
            mirror, box, clock = begin(gw)
            box.put("INBOX", msg("v@x.org", sender="boss@corp.example", day=39))
            mirror.poll()
            # The web gateway away: the VIP's mail is owed, never heard.
            assert sorted(offers(mirror)) == ["v@x.org"] and jobs.asked == []
            assert t.by_id(gw)["v@x.org"]["delivered"] is False
            # Its record gone from the ledger, and a restart: what the row
            # said is kept as hints — that it was owed among them.
            for path, _fields in list(gw._ibstore.iter_records(gw.INBOUND_STORE_DIR)):
                path.unlink()
            mirror.index.close()
            mirror.threads.close()
            mirror = again(gw, box, clock)
            assert mirror.index.one("SELECT owed FROM lost WHERE message_id = 'v@x.org'")["owed"]
            up[0] = True
            events.clear()
            for _ in range(4):
                clock[0] += 61
                mirror.poll()
            # Found again: owed still — offered once, by its gate sender, and
            # worked; delivered when its turn is done.
            assert mirror.lost() == 0 and offered(events) == ["v@x.org"], events
            offer = [p for _k, p in events if p.get("handover")][0]
            assert offer["gate"]["vip"] is True and offer["gate_sender"] == "boss@corp.example"
            assert len(jobs.asked) == 1 and t.by_id(gw)["v@x.org"]["delivered"] is False
            jobs.asked[0].done()
            assert t.by_id(gw)["v@x.org"]["delivered"] is True and offers(mirror) == {}
        finally:
            jobs.restore()
    print("ok: a record lost while still owed comes back owed, and is offered once")


def test_a_strangers_mail_under_a_sends_provider_id_is_an_ordinary_arrival():
    spec = importlib.util.spec_from_file_location("mail_gateway_send_helpers",
                                                  HERE / "test_mail_gateway_send.py")
    s = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s)
    for how in ("a stranger's mail", "the copy moved out and back"):
        w = s.world(save_sent=False)
        try:
            gw, box, mirror = w.gw, w.box, w.mirror
            M, P, _raw, uid = s._rewritten_send(w)
            before = len(w.events)
            if how == "a stranger's mail":
                box.put("INBOX", t.msg(P, sender="Mallory <m@evil.org>", subject="Re: Plans",
                                       body="Not the user's.\n", day=s.later(60)))
            else:
                uid = box.move_mail("Sent", uid, "Archive")
                mirror.poll()
                mirror.poll()
                box.move_mail("Archive", uid, "Sent")
            for _ in range(3):
                mirror.poll()
            after = w.events[before:]
            if how == "a stranger's mail":
                # Its own record under the provider's id, offered as any mail
                # that arrives — by its own From.
                assert offered(after) == [P], after
                offer = [p for _k, p in after if p.get("handover")][0]
                assert offer["direction"] == "in" and offer["gate_sender"] == "m@evil.org"
                assert t.by_id(gw)[P]["delivered"] is True
            else:
                # The send's own copy: one more place of the send, under the id
                # the gateway minted — nothing offered, nothing under P.
                assert offered(after) == [] and P not in t.by_id(gw)
                assert all(p["message_id"] == M for k, p in after if k == "inbound"), after
            assert t.by_id(gw)[M]["author"] == "user" and offers(mirror) == {}
        finally:
            s.done(w)
    print("ok: a stranger's mail under a send's provider id is offered as its own; the send's "
          "copy is offered nothing")


def test_a_mail_whose_move_is_not_told_stays_owed_and_is_buried_once():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw)
        events = t.rail(gw, answer={})
        mirror, box, clock = begin(gw)
        box.can_expunge, box.names_uids = False, False
        box.put("INBOX", msg("a@x.org", day=39))
        mirror.poll()
        assert sorted(offers(mirror)) == ["a@x.org"]
        # Archived on a server with neither MOVE nor UIDPLUS, which names no
        # UID — and whose archive cannot say just then where the copy is:
        # its only place is the leftover, and it settles.
        real_find = box.find
        box.find = lambda folder, message_id: gw.Matches((), False)
        try:
            assert mirror.archive("a@x.org") == {"moved": 1, "flagged": 1}
        finally:
            box.find = real_find
        assert mirror.index.one("SELECT 1 FROM settling WHERE message_id = 'a@x.org'")
        events.clear()
        for _ in range(3):
            clock[0] += gw.RETRY_MAX_SECONDS
            mirror.poll()
        # Looked for, found — and all the while no removal is told, and the
        # mail is still owed, and offered again.
        assert [k for k, _p in events if k == "removed"] == []
        assert not t.by_id(gw)["a@x.org"]["removed_at"] and sorted(offers(mirror)) == ["a@x.org"]
        assert offered(events) and set(offered(events)) == {"a@x.org"}
        # Gone from every folder: buried, told once, and owed to nobody.
        box.remove("Archive", box.uid_of("Archive", "a@x.org"))
        box.remove("INBOX", box.uid_of("INBOX", "a@x.org"))
        events.clear()
        for _ in range(3):
            clock[0] += gw.RETRY_MAX_SECONDS
            mirror.poll()
        removed = [p for k, p in events if k == "removed"]
        assert [p["message_ids"] for p in removed] == [["a@x.org"]], removed
        assert offers(mirror) == {} and offered(events) == []
        assert t.by_id(gw)["a@x.org"]["removed_at"]
    print("ok: a mail whose move was not told is looked for, owed, and told gone once buried")


def test_a_record_written_again_is_told_the_seen_of_its_first_sighting():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw)
        events = t.rail(gw)
        box = t.FakeMailbox(gw)
        real_trim = gw._trim.trim
        broken = {"read", "unread", "live"}

        def trim(plain, html, subject=None, **evidence):
            if subject in broken:
                raise RecursionError("this mail, for now")
            return real_trim(plain, html, subject=subject, **evidence)
        gw._trim.trim = trim
        try:
            box.put("INBOX", msg("read@x.org", subject="read", day=10), flags=["\\Seen"])
            box.put("INBOX", msg("unread@x.org", subject="unread", day=11))
            clock = [NOW]
            mirror = gw.Mirror(box, clock=lambda: clock[0])
            while mirror.poll():
                pass
            box.put("INBOX", msg("live@x.org", subject="live", day=39))
            mirror.poll()
            # Three placeholders: two old mails, told with the \Seen the
            # mirror first saw them with, and one that arrived — the chat
            # took it, and counted it unread.
            first = {p["message_id"]: p for _k, p in events}
            assert first["read@x.org"]["seen"] is True and first["unread@x.org"]["seen"] is False
            assert first["live@x.org"]["handover"] is True and "seen" not in first["live@x.org"]
            assert all(p["text"] == gw.UNREADABLE_TEXT for p in first.values())
            # A client turns the mailbox's \Seen about meanwhile.
            mails = box.boxes["INBOX"]["mails"]
            for mid, now_seen in (("read@x.org", False), ("unread@x.org", True),
                                  ("live@x.org", True)):
                mail = mails[box.uid_of("INBOX", mid)]
                mail["flags"] = frozenset({"\\Seen"} if now_seen else ())
            broken.clear()
            events.clear()
            clock[0] += gw.SET_ASIDE_RETRY_SECONDS
            mirror.poll()
            # Read at last, each told as old mail with what was told of it
            # first — never the mailbox's \Seen of the moment, which would
            # read (or unread) the chat from the mailbox.
            told = {p["message_id"]: p for _k, p in events}
            assert sorted(told) == ["live@x.org", "read@x.org", "unread@x.org"], events
            assert all(p["backfill"] is True and p["text"] == "The numbers are in."
                       for p in told.values()), told
            assert {mid: p["seen"] for mid, p in told.items()} == {
                "read@x.org": True, "unread@x.org": False, "live@x.org": False}
        finally:
            gw._trim.trim = real_trim
    print("ok: a record written again is told the \\Seen of its first sighting, never the "
          "mailbox's of the moment")


def test_a_news_mail_in_its_chat_that_arrived_unreadable_is_filed_once_it_reads():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw, news={"boss@corp.example", "letters.list.example"},
              ignored={"letters.list.example"})
        senders(gw, vip={"boss@corp.example"})
        t.rail(gw)
        box = t.FakeMailbox(gw)
        real_trim = gw._trim.trim
        broken = {"old letter", "letter", "reply"}

        def trim(plain, html, subject=None, **evidence):
            if subject in broken:
                raise RecursionError("this mail, for now")
            return real_trim(plain, html, subject=subject, **evidence)
        gw._trim.trim = trim
        try:
            # Old mail: a VIP's news mail the backfill finds unreadable.
            box.put("INBOX", msg("old@corp.example", sender="boss@corp.example",
                                 subject="old letter", day=10))
            clock = [NOW]
            mirror = gw.Mirror(box, clock=lambda: clock[0])
            while mirror.poll():
                pass
            # Arrivals: a VIP's news mail, and a list's news mail in a thread
            # that is mirrored — each unreadable, each in its chat as a
            # placeholder, and neither filed yet.
            box.put("INBOX", msg("t@x.org", sender="ann@x.org", subject="Plans", day=38))
            mirror.poll()
            box.put("INBOX", msg("letter@corp.example", sender="boss@corp.example",
                                 subject="letter", body="Words.\n", day=39))
            box.put("INBOX", msg("reply@list.example", sender="post@list.example",
                                 subject="reply", body="Listed words.\n", day=39,
                                 list_id="<letters.list.example>", refs=["t@x.org"],
                                 irt="t@x.org"))
            mirror.poll()
            records = t.by_id(gw)
            assert {mid: records[mid]["text"] for mid in
                    ("old@corp.example", "letter@corp.example", "reply@list.example")} \
                == dict.fromkeys(("old@corp.example", "letter@corp.example",
                                  "reply@list.example"), gw.UNREADABLE_TEXT)
            assert gw.filed == []
            broken.clear()
            for _ in range(3):
                clock[0] += gw.SET_ASIDE_RETRY_SECONDS
                mirror.poll()
            # Read at last: the two that arrived are filed — once — and stay
            # in the INBOX, being in a chat; the old one is not filed.
            assert sorted(item["title"] for item in gw.filed) == ["letter", "reply"], gw.filed
            texts = {mid: r["text"] for mid, r in t.by_id(gw).items()}
            assert texts["letter@corp.example"] == "Words."
            assert texts["reply@list.example"] == "Listed words."
            assert gw.UNREADABLE_TEXT not in texts.values()
            assert {"letter@corp.example", "reply@list.example"} <= set(held_in(box, "INBOX"))
            assert mirror.waiting()["news"] == 0
        finally:
            gw._trim.trim = real_trim
    print("ok: a news mail in its chat that arrived unreadable is filed once it reads, and "
          "left in the INBOX; an old one is not filed")


def test_news_is_filed_whatever_became_of_the_rest():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        flags(gw, news={"news@shop.example"})
        t.rail(gw)
        mirror, box, clock = begin(gw)

        def stuck(policy, limit):
            raise gw.MirrorFault("the mail at 'INBOX' UID 9 could not be written (EIO)")
        # One mail of the rest that fails at every pass — a reading again
        # whose write always fails.
        mirror._read_noted = stuck
        box.put("INBOX", msg("n1@shop.example", sender="news@shop.example", subject="Sale",
                             body="x\n", day=39))
        for n in range(2):
            clock[0] += 3600
            mirror.poll(sync=True, work=False)
            try:
                mirror.poll(sync=False)
            except gw.MirrorFault as exc:
                assert "UID 9" in str(exc), exc
            else:
                raise AssertionError("the rest's fault was not raised")
            # Filed all the same, at the first such pass, and once.
            assert [item["title"] for item in gw.filed] == ["Sale"], (n, gw.filed)
            assert mirror.waiting()["news"] == 0
        # Both failing: the rest's fault is the one raised.
        box.put("INBOX", msg("n2@shop.example", sender="news@shop.example", subject="Sale 2",
                             body="y\n", day=39))
        mirror.poll(sync=True, work=False)

        def broken(limit):
            raise RuntimeError("the news step's own")
        mirror._file_news = broken
        try:
            mirror.poll(sync=False)
        except gw.MirrorFault as exc:
            assert "UID 9" in str(exc), exc
        else:
            raise AssertionError("the rest's fault was not raised")
    print("ok: news is filed whatever became of the rest of the work, whose fault is raised")


def test_a_news_move_stopped_after_the_server_leaves_one_archive_copy():
    for expunge in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            gw = t.load(tmp)
            flags(gw, news={"news@shop.example"}, ignored={"news@shop.example"})
            t.rail(gw)
            mirror, box, clock = begin(gw)
            # Without MOVE and UIDPLUS the server copies the mail out and
            # keeps it here, flagged \Deleted.
            box.can_expunge = expunge
            real_move = box.move

            def move(*args, **kwargs):
                box.move = real_move
                real_move(*args, **kwargs)
                raise _Stop("stopped between the server's move and the index")
            box.move = move
            box.put("INBOX", msg("n@shop.example", sender="news@shop.example", subject="N",
                                 day=39))
            try:
                mirror.poll()
            except _Stop:
                pass
            else:
                raise AssertionError("the move was not stopped")
            mirror.index.close()
            mirror.threads.close()
            mirror = again(gw, box, clock)
            for _ in range(3):
                clock[0] += 3600
                mirror.poll()
            # Copied out once, filed once, and nothing owed any more.
            assert held_in(box, "Archive") == ["n@shop.example"], (expunge, box.writes)
            assert [w for w in box.writes if w[0] == "move"] == [
                ("move", "INBOX", (1,), "Archive")], (expunge, box.writes)
            assert len(gw.filed) == 1 and mirror.waiting()["news"] == 0
            assert mirror.index.get("moving") is None
            left = mirror.index.one("SELECT skipped FROM locations WHERE role = 'inbox' "
                                    "AND message_id = 'n@shop.example'")
            assert (left and left["skipped"]) == (None if expunge else "moved"), dict(left or {})
    print("ok: a news move stopped after the server leaves one archive copy, with or without "
          "MOVE and UIDPLUS")


def _killed_at(gw, mirror, n: int, *, after: bool):
    """Kill the process at the ``n``-th write of the mirror — an index
    statement or transaction, or a ledger record — just before it, or just
    after it stands. Returns a list that says whether it was reached, and the
    undo."""
    reached, count = [], [0]
    index, store = mirror.index, gw._ibstore
    real = {"run": index.run, "atomic": index.atomic, "write_message": store.write_message}

    def wrap(owner, name):
        def write(*args, **kwargs):
            count[0] += 1
            if count[0] != n:
                return real[name](*args, **kwargs)
            reached.append(name)
            if not after:
                raise _Stop()
            real[name](*args, **kwargs)
            raise _Stop()
        setattr(owner, name, write)
    wrap(index, "run")
    wrap(index, "atomic")
    wrap(store, "write_message")

    def undo():
        index.run, index.atomic = real["run"], real["atomic"]
        store.write_message = real["write_message"]
    return reached, undo


def test_a_kill_at_every_write_leaves_an_unread_vip_mail_owed_once():
    # Wherever the process is killed — the placeholder written, or the record
    # written in its place when it reads in time — the mail is owed exactly
    # once after a restart, and offered once: the offers row is the wait.
    for reads in (False, True):
        for after in (False, True):
            n = 0
            while True:
                n += 1
                with tempfile.TemporaryDirectory() as tmp:
                    gw = t.load(tmp)
                    flags(gw)
                    senders(gw, vip={"boss@corp.example"})
                    events = t.rail(gw)
                    mirror, box, clock = begin(gw)
                    wait = gw.MAIL_UNREAD_OFFER_WAIT
                    real_trim = gw._trim.trim
                    broken = {"late"}

                    def trim(plain, html, subject=None, **evidence):
                        if subject in broken:
                            raise RecursionError("this mail, for now")
                        return real_trim(plain, html, subject=subject, **evidence)
                    gw._trim.trim = trim
                    try:
                        box.put("INBOX", msg("v@x.org", sender="boss@corp.example",
                                             subject="late", body="Real words.\n", day=39))
                        if reads:
                            mirror.poll()
                            broken.clear()
                            clock[0] += wait / 2
                        reached, undo = _killed_at(gw, mirror, n, after=after)
                        try:
                            mirror.poll()
                        except _Stop:
                            pass
                        undo()
                        if not reached:
                            break   # past the last write of the pass
                        mirror.index.close()
                        mirror.threads.close()
                        mirror = again(gw, box, clock)
                        where = (reads, after, n, reached)
                        mirror.poll()
                        if not reads:
                            assert offered(events) == [], (where, events)  # its wait holds
                        for _ in range(3):
                            clock[0] += wait
                            mirror.poll()
                        records = [r for r in t.records(gw) if r["message_id"] == "v@x.org"]
                        assert len(records) == 1 and records[0]["delivered"] is True, where
                        # Offered as one thing, never as both: the placeholder
                        # or the mail it reads as. (An offer whose answer a
                        # stop cut off is made again — a repeat, which the web
                        # gateway takes once, by its message id.)
                        made = [p for _k, p in events if p.get("handover")]
                        assert made and set(offered(events)) == {"v@x.org"}, (where, events)
                        assert {p["text"] for p in made} == {
                            "Real words." if reads else gw.UNREADABLE_TEXT}, (where, made)
                        assert all(p["gate"]["vip"] is True for p in made), (where, made)
                        assert offers(mirror) == {}, where
                    finally:
                        gw._trim.trim = real_trim
            assert n > 3, (reads, after, n)
    print("ok: a kill at every write leaves a VIP's unread mail owed once, and offered once")


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
