#!/usr/bin/env python3
"""Checks for the project waker: lead-time parsing and the wake decision.

No store and no filesystem: the gate query is exercised nowhere here (that needs
QLever), but everything downstream of it — which date a resting project wakes
on, and whether a given "today" has reached it — is pure and worth pinning,
because getting it wrong means a deadline passes in silence.

    python3 tests/test_recurring_projects.py
"""
import datetime as dt
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "recurring-projects.py"


def load():
    spec = importlib.util.spec_from_file_location("recurring_projects", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


def test_parse_lead(rp):
    print("parse_lead")
    check("empty -> default days", rp.parse_lead(""), ("d", rp.DEFAULT_LEAD_DAYS))
    check("default is same-day", rp.DEFAULT_LEAD_DAYS, 0)
    check("bare number is days", rp.parse_lead("10"), ("d", 10))
    check("explicit days", rp.parse_lead("10d"), ("d", 10))
    check("weeks", rp.parse_lead("2w"), ("w", 2))
    check("months, spaced and upper", rp.parse_lead(" 3 M "), ("m", 3))
    check("garbage is rejected", rp.parse_lead("soon"), None)
    check("negative is rejected", rp.parse_lead("-3d"), None)
    check("unknown unit is rejected", rp.parse_lead("3y"), None)


def test_minus_lead(rp):
    print("minus_lead")
    d = dt.date(2028, 10, 1)
    check("days", rp.minus_lead(d, "d", 14), dt.date(2028, 9, 17))
    check("weeks", rp.minus_lead(d, "w", 2), dt.date(2028, 9, 17))
    check("months", rp.minus_lead(d, "m", 3), dt.date(2028, 7, 1))
    check("across the year boundary",
          rp.minus_lead(dt.date(2027, 2, 15), "m", 4), dt.date(2026, 10, 15))
    # 31 March minus one month is not 31 February.
    check("clamps to a valid day",
          rp.minus_lead(dt.date(2027, 3, 31), "m", 1), dt.date(2027, 2, 28))
    check("clamps into a leap February",
          rp.minus_lead(dt.date(2028, 3, 31), "m", 1), dt.date(2028, 2, 29))


def test_wake_plan(rp):
    print("wake_plan")
    # A cadence wakes on next_due, exactly as before this feature existed.
    check("cadence",
          rp.wake_plan({"recurring": "monthly", "next_due": "2026-09-08"}, {}),
          ("cadence", dt.date(2026, 9, 8), dt.date(2026, 9, 8)))
    # A cadence wins over a deadline the same project also carries: expected_by
    # is then the end of the standing arrangement, not the next occurrence.
    check("cadence wins over expected_by",
          rp.wake_plan({"recurring": "quarterly", "next_due": "2026-09-30",
                        "expected_by": "2030-01-01"}, {}),
          ("cadence", dt.date(2026, 9, 30), dt.date(2026, 9, 30)))
    # The one-off deadline: on the date itself unless the project asks earlier.
    check("deadline, default lead",
          rp.wake_plan({"expected_by": "2028-10-01"}, {}),
          ("deadline", dt.date(2028, 10, 1), dt.date(2028, 10, 1)))
    check("deadline, own lead",
          rp.wake_plan({"expected_by": "2028-10-01", "remind_before": "3m"}, {}),
          ("deadline", dt.date(2028, 7, 1), dt.date(2028, 10, 1)))
    # Store row as fallback when the frontmatter reader saw nothing.
    check("falls back to the store row",
          rp.wake_plan({}, {"expectedBy": "2027-05-05"}),
          ("deadline", dt.date(2027, 5, 5), dt.date(2027, 5, 5)))
    # Nothing to wake on: silent skip, not an error.
    check("no dates at all", rp.wake_plan({}, {}), None)
    check("recurring but no next_due",
          rp.wake_plan({"recurring": "monthly"}, {}), "bad next_due ''")
    # An unrecognised cadence is not a cadence — falls through to the deadline
    # branch rather than being treated as due every run.
    check("unknown cadence falls through",
          rp.wake_plan({"recurring": "yearly", "expected_by": "2027-01-31"}, {}),
          ("deadline", dt.date(2027, 1, 31), dt.date(2027, 1, 31)))
    # A malformed lead is reported, never silently defaulted.
    plan = rp.wake_plan({"expected_by": "2028-10-01", "remind_before": "soon"}, {})
    check("bad remind_before is reported", isinstance(plan, str), True)


def test_is_finished(rp):
    """A finished project stays asleep even when its file reaches its date.

    The store-side exclusions are not enough on their own: a chamber's converter
    maps the keys it chose, so `resolved: true` may never reach the store.
    """
    print("is_finished")
    check("live project", rp.is_finished({"paused": "true"}), False)
    check("resolved flag", rp.is_finished({"resolved": "true"}), True)
    check("resolved false", rp.is_finished({"resolved": "false"}), False)
    check("status done", rp.is_finished({"status": "done"}), True)
    check("status active", rp.is_finished({"status": "active"}), False)


def test_due_comparison(rp):
    """The main loop's own test: today >= wake_on."""
    print("wake timing")
    kind, wake_on, due = rp.wake_plan(
        {"expected_by": "2028-10-01", "remind_before": "3m"}, {})
    check("still asleep the day before", dt.date(2028, 6, 30) >= wake_on, False)
    check("wakes on the lead date", dt.date(2028, 7, 1) >= wake_on, True)
    check("still wakes past the deadline", dt.date(2028, 11, 1) >= wake_on, True)
    check("due date is the deadline itself", due, dt.date(2028, 10, 1))
    check("kind", kind, "deadline")


def test_reminder_fallback(rp):
    """No reminder_message in the file -> a neutral, generic English line."""
    print("reminder fallback")
    title, msg = rp.reminder_text(
        {}, "", "VAT return", dt.date(2026, 9, 30), "deadline", dt.date(2026, 9, 16))
    check("title falls back", title, "Due: VAT return")
    check("names the deadline", "2026-09-30" in msg, True)
    check("says how far off", "in 14 days" in msg, True)
    _, msg2 = rp.reminder_text(
        {}, "", "VAT return", dt.date(2026, 9, 30), "cadence", dt.date(2026, 9, 30))
    check("cadence keeps its own wording", "recurring project" in msg2, True)
    # A project that carries its own wording keeps it verbatim, in any language.
    _, msg3 = rp.reminder_text(
        {"reminder_message": "Fristablauf naht."}, "", "X",
        dt.date(2026, 9, 30), "deadline", dt.date(2026, 9, 16))
    check("own wording wins", msg3, "Fristablauf naht.")


def main():
    rp = load()
    for t in (test_parse_lead, test_minus_lead, test_wake_plan,
              test_is_finished, test_due_comparison, test_reminder_fallback):
        t(rp)
    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
