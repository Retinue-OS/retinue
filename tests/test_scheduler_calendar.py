#!/usr/bin/env python3
"""Calendar triggers (scripts/calendar_trigger.py) and app jobs in the
scheduler (scripts/scheduler.py).

Checks, with a pinned clock:

- slots in an IANA zone: weekdays, several times a day, one-off dates;
- daylight saving: a daily time keeps its wall-clock hour across the change,
  a time inside the spring-forward gap fires once, the gap's length later,
  and a time that occurs twice in autumn fires once;
- the missed-slot rule: a slot found within its lead fires once, a slot
  found later is skipped and recorded as missed, and slots from before the
  job was first seen never fire;
- snoozes (extra), skipped days and skipped slots;
- app jobs: loaded from the per-app schedule files with app:<chamber>:<app>:<id>
  ids, a reminder fired by one request to the gateway (and retried while in
  its lead when the gateway is down), a prompt job prefixed with its app, a
  command refused; and --list naming the owner.

    python3 tests/test_scheduler_calendar.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
ZRH = ZoneInfo("Europe/Zurich")
NYC = ZoneInfo("America/New_York")

failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


class FakeGateway(BaseHTTPRequestHandler):
    received: list = []
    status = 200

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        FakeGateway.received.append((self.path, self.headers.get("X-Conversation-Backend-Token"),
                                     json.loads(body)))
        self.send_response(FakeGateway.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def load(tmp: Path, fire_url: str):
    os.environ["SCHEDULER_STATE_DIR"] = str(tmp / "state")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["BASE_SCHEDULE"] = str(tmp / "no-base.json")
    os.environ["CHAMBER_APPS_STATE_DIR"] = str(tmp / "apps")
    os.environ["CLAUDE_CRED_FILE"] = str(tmp / "claude" / ".credentials.json")
    os.environ["APPS_REMINDER_FIRE_URL"] = fire_url
    os.environ["CONVERSATION_BACKEND_TOKEN"] = "tok"
    os.environ["ATTENTION_TZ"] = "Europe/Zurich"
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("scheduler_cal_under_test", SCRIPTS_DIR / "scheduler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def at(mod, when: datetime):
    mod.now = lambda: when.timestamp()


def main() -> int:

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        mod = load(tmp, f"http://127.0.0.1:{server.server_address[1]}/internal/apps/reminders/fire")
        import calendar_trigger as ct  # noqa: F811
        import chamber_apps

        print("slots in a zone")
        t = ct.parse({"at": ["07:00", "19:30"], "days": "mon,wed"}, ZRH)
        got = [s.isoformat() for s in t.slots(datetime(2026, 11, 23, tzinfo=ZRH), datetime(2026, 11, 26, tzinfo=ZRH))]
        check("mon/wed twice a day", got, ["2026-11-23T07:00:00+01:00", "2026-11-23T19:30:00+01:00",
                                           "2026-11-25T07:00:00+01:00", "2026-11-25T19:30:00+01:00"])
        t = ct.parse({"at": "09:00", "tz": "America/New_York"}, ZRH)
        check("the job's own zone wins", t.next_after(datetime(2026, 11, 24, 12, tzinfo=timezone.utc)).isoformat(),
              "2026-11-24T09:00:00-05:00")
        t = ct.parse({"at": "10:00", "date": ["2026-12-01", "2026-12-24"]}, ZRH)
        check("one-off dates", [s.date().isoformat() for s in t.slots(datetime(2026, 11, 1, tzinfo=ZRH),
                                                                         datetime(2027, 1, 1, tzinfo=ZRH))],
              ["2026-12-01", "2026-12-24"])
        check("a date past its last slot has no next", t.next_after(datetime(2026, 12, 25, tzinfo=ZRH)), None)
        for bad in ({"at": "25:00"}, {"at": "7"}, {"at": "07:00", "tz": "Mars/Base"},
                    {"at": "07:00", "days": "someday"}, {"at": "07:00", "lead": "forever"},
                    {"at": "07:00", "days": "mon", "date": "2026-12-01"}, {"at": "07:00", "date": "2026-02-30"}):
            try:
                ct.parse(bad, ZRH)
                check(f"refused {bad}", "accepted", "ValueError")
            except ValueError:
                check(f"refused {bad}", "ValueError", "ValueError")

        print("daylight saving")
        t = ct.parse({"at": "07:00"}, ZRH)
        got = [s.astimezone(timezone.utc).strftime("%m-%d %H:%M") for s in
               t.slots(datetime(2026, 3, 28, tzinfo=ZRH), datetime(2026, 3, 30, 12, tzinfo=ZRH))]
        check("07:00 stays 07:00 local (06:00 → 05:00 UTC)", got, ["03-28 06:00", "03-29 05:00", "03-30 05:00"])
        t = ct.parse({"at": "02:30"}, ZRH)
        got = [s.isoformat() for s in t.slots(datetime(2026, 3, 29, tzinfo=ZRH), datetime(2026, 3, 29, 23, tzinfo=ZRH))]
        check("02:30 in the gap fires once at 03:30", got, ["2026-03-29T03:30:00+02:00"])
        got = [ct.occurrence_key(s) for s in t.slots(datetime(2026, 10, 25, tzinfo=ZRH),
                                                     datetime(2026, 10, 25, 23, tzinfo=ZRH))]
        check("02:30 that occurs twice fires once, the first", got, ["2026-10-25T00:30:00Z"])
        t = ct.parse({"at": "02:30", "tz": "America/New_York"}, ZRH)
        got = [s.isoformat() for s in t.slots(datetime(2026, 3, 8, tzinfo=NYC), datetime(2026, 3, 8, 23, tzinfo=NYC))]
        check("New York's gap too", got, ["2026-03-08T03:30:00-04:00"])

        print("snooze, skip")
        t = ct.parse({"at": "10:00", "extra": ["2026-11-24T10:15:00Z"], "skip_dates": ["2026-11-25"],
                      "skip_slots": ["2026-11-26T09:00:00Z"]}, ZRH)
        got = [ct.occurrence_key(s) for s in t.slots(datetime(2026, 11, 24, tzinfo=ZRH), datetime(2026, 11, 27, 23, tzinfo=ZRH))]
        check("extra fires, skipped day and slot do not", got,
              ["2026-11-24T09:00:00Z", "2026-11-24T10:15:00Z", "2026-11-27T09:00:00Z"])

        print("the missed-slot rule")
        job = {"id": "daily", "prompt": "p", "at": "07:00", "lead": "30m"}
        job["_trigger"] = ct.parse(job, ZRH)
        at(mod, datetime(2026, 11, 24, 7, 10, tzinfo=ZRH))
        check("first sighting after a slot: not fired (before it was seen)", mod.due_slot(job), None)
        at(mod, datetime(2026, 11, 25, 7, 0, 20, tzinfo=ZRH))
        slot = mod.due_slot(job)
        check("next day's slot fires", slot and slot.isoformat(), "2026-11-25T07:00:00+01:00")
        mod.write_state("daily", "success", last_slot=ct.occurrence_key(slot))
        at(mod, datetime(2026, 11, 25, 7, 1, tzinfo=ZRH))
        check("fires once", mod.due_slot(job), None)
        at(mod, datetime(2026, 11, 26, 7, 25, tzinfo=ZRH))
        check("a slot 25 min late, within lead 30m: fires on start",
              (mod.due_slot(job) or datetime.min).isoformat(), "2026-11-26T07:00:00+01:00")
        at(mod, datetime(2026, 11, 28, 9, 0, tzinfo=ZRH))
        check("two hours late: skipped", mod.due_slot(job), None)
        state = mod.read_state("daily")
        check("recorded as missed", (state["status"], state["last_slot"]), ("missed", "2026-11-28T06:00:00Z"))
        check("and not fired later", mod.due_slot(job), None)
        reg = {"id": "reg", "prompt": "p", "at": "07:00",
               "registered_at": datetime(2026, 11, 24, 6, 50, tzinfo=ZRH).isoformat()}
        reg["_trigger"] = ct.parse(reg, ZRH)
        at(mod, datetime(2026, 11, 24, 7, 0, 40, tzinfo=ZRH))
        check("registered before the slot, first seen after: fires",
              (mod.due_slot(reg) or datetime.min).isoformat(), "2026-11-24T07:00:00+01:00")
        check("disabled never fires", mod.due_slot(dict(reg, enabled=False)), None)

        print("app jobs")
        chamber_apps.save_schedule("health", "hands", {"jobs": [
            {"id": "hand-1", "kind": "reminder", "at": "10:30", "title": "Hand session",
             "registered_at": "2026-11-24T08:00:00+00:00"},
            {"id": "report", "kind": "job", "prompt": "Summarise the week.", "at": "18:00", "days": "fri"},
            {"id": "evil", "kind": "job", "command": "rm -rf /", "at": "10:00"},
            {"id": "noprompt", "kind": "job", "at": "10:00"},
            {"id": "bad", "kind": "reminder", "at": "nope", "title": "x"},
        ]})
        (tmp / "chambers" / "health").mkdir()
        (tmp / "chambers" / "health" / ".schedule.json").write_text(json.dumps({"jobs": [
            {"id": "weekly", "prompt": "Weekly review.", "at": "08:40", "days": "mon"},
            {"id": "old", "command": "true", "interval_seconds": 60}]}))
        jobs = {j["id"]: j for j in mod.load_jobs()}
        check("loaded ids", sorted(jobs), ["app:health:hands:hand-1", "app:health:hands:report", "old", "weekly"])
        check("prompt job names its app", jobs["app:health:hands:report"]["prompt"].startswith(
            "Scheduled job of the chamber app health/hands"), True)
        rem = jobs["app:health:hands:hand-1"]
        at(mod, datetime(2026, 11, 24, 10, 30, 5, tzinfo=ZRH))
        slot = mod.due_slot(rem)
        check("reminder due", slot and slot.isoformat(), "2026-11-24T10:30:00+01:00")
        FakeGateway.status = 503
        mod.run_job(rem, slot)
        check("gateway down: slot not consumed", mod.due_slot(rem) is not None, True)
        FakeGateway.status = 200
        mod.run_job(rem, mod.due_slot(rem))
        path, token, body = FakeGateway.received[-1]
        check("fired at the gateway", (path, token, body), (
            "/internal/apps/reminders/fire", "tok",
            {"chamber": "health", "app": "hands", "id": "hand-1", "occurrence": "2026-11-24T09:30:00Z"}))
        check("then handled", mod.due_slot(rem), None)
        rows = {r["id"]: r for r in mod.list_jobs()}
        check("--list owner", rows["app:health:hands:hand-1"]["owner"], "app health/hands")
        check("--list kind", rows["app:health:hands:hand-1"]["kind"], "reminder")
        check("--list next", rows["app:health:hands:hand-1"]["next"], "2026-11-25T10:30:00+01:00")
        check("--list chamber owner", rows["weekly"]["owner"], "chamber health")
        server.shutdown()
    if failures:
        print(f"{len(failures)} calendar trigger check(s) FAILED")
        return 1
    print("all scheduler calendar-trigger tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
