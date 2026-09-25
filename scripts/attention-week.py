#!/usr/bin/env python3
"""Show or change the week the focus modes follow: its day plans and holidays.

The dashboard's focus mode follows a schedule (docs/attention-model.md), and
the schedule is a week of a few *day plans* rather than seven copies of one
day: each plan is a day's schedule plus the days it rules — ``mon-fri``,
``sat, sun`` — and every weekday belongs to exactly one plan. One plan also
claims ``holiday``, and the dates in the holiday list follow it whatever
weekday they fall on, so time off is a date range told to the system, not a
new schedule. This is how an agent makes the change the user asks for:

    attention-week.py                                   # the week, the holidays, today's plan
    attention-week.py holiday add 2026-12-24..2027-01-02 --name Christmas
    attention-week.py holiday remove Christmas          # or a date inside it
    attention-week.py plan Friday --days fri \\
        --schedule "07:00 chores, 08:00 focused, 14:00 social, 22:00 rest"
    attention-week.py plan Workday --days mon-fri       # Friday back; the emptied plan goes
    attention-week.py plan "Day off" --digests "09:00, 18:00"
    attention-week.py digests "08:00, 12:00, 17:00, 21:00"   # for plans that name none

Days: mon … sun, ranges (mon-fri, fri-mon), weekdays, weekend, daily,
holiday. A schedule entry is "HH:MM mode" or "HH:MM focused <sphere>"; modes
are the ids rest, focused, chores, social (and any a deployment adds). A plan
without a 00:00 entry starts the day in the mode the night before ended in.
The gateway checks every change — each weekday in exactly one plan, a plan
that takes the holidays — and answers what changed or why not.

Configuration (environment): ATTENTION_URL, else the web-gateway on
localhost:WEB_GATEWAY_PORT (8080).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import attention as policy  # noqa: E402
import attention_store  # noqa: E402

BASE = os.environ.get("ATTENTION_URL", f"http://localhost:{os.environ.get('WEB_GATEWAY_PORT', '8080')}").rstrip("/")
TIMEOUT = 30


def _call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            reason = json.loads(exc.read().decode("utf-8")).get("error") or exc.reason
        except ValueError:
            reason = exc.reason
        sys.exit(f"attention-week: {reason}")
    except urllib.error.URLError as exc:
        sys.exit(f"attention-week: the gateway at {BASE} did not answer ({exc.reason})")


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def show() -> int:
    focus = _call("GET", "/attention/profile")["focus"]
    default_digests = focus.get("digest_times") or policy.DEFAULT_DIGEST_TIMES
    for plan in policy.week_of(focus):
        days = ", ".join(plan.get("days") or [])
        entries = " · ".join(" ".join([_hhmm(e[0]), *[str(x) for x in e[1:]]]) for e in plan.get("schedule") or [])
        digests = plan.get("digest_times")
        print(f"{plan['name']:<12} {days:<22} {entries}")
        print(f"{'':<12} {'':<22} digests {', '.join(_hhmm(t) for t in digests or default_digests)}"
              f"{'' if digests else ' (the default)'}")
    holidays = focus.get("holidays") or []
    print(f"{'Holidays':<12} " + ("; ".join(policy.fmt_holiday(h) for h in holidays) if holidays else "none"))
    today = datetime.now(attention_store.zone()).date()
    h = policy.holiday_on(focus, today)
    print(f"{'Today':<12} {today:%a %Y-%m-%d}: {policy.day_plan(focus, today)['name']}"
          + (f" ({h.get('name') or 'holiday'})" if h else ""))
    return 0


def change(patch: dict) -> int:
    out = _call("POST", "/attention/modes", patch)
    changed = out.get("changed") or []
    print("\n".join(changed) if changed else "nothing changed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Show or change the week of day plans and the holidays.")
    sub = ap.add_subparsers(dest="cmd")
    hol = sub.add_parser("holiday", help="add or remove holidays")
    hol.add_argument("action", choices=["add", "remove"])
    hol.add_argument("what", help="YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD to add; a name or a date inside it to remove")
    hol.add_argument("--name", help="what the holiday is called (add)")
    plan = sub.add_parser("plan", help="change a day plan, or add one")
    plan.add_argument("name")
    plan.add_argument("--days", help="the days it rules: mon-fri, sat, sun, holiday, …; they move here from other plans")
    plan.add_argument("--schedule", help='"07:00 chores, 08:00 focused, 13:00 focused customers, 22:00 rest"')
    plan.add_argument("--digests", help='its own digest times: "09:00, 18:00"')
    plan.add_argument("--default-digests", action="store_true", help="drop its own digest times")
    plan.add_argument("--rename", metavar="NEW")
    dig = sub.add_parser("digests", help="the default digest times, for plans that name none")
    dig.add_argument("times", help='"08:00, 12:00, 17:00, 21:00"')
    args = ap.parse_args()

    if args.cmd is None:
        return show()
    if args.cmd == "holiday":
        if args.action == "add":
            return change({"holiday_add": f"{args.what} {args.name}".strip() if args.name else args.what})
        return change({"holiday_remove": args.what})
    if args.cmd == "plan":
        patch = {"plan": args.name}
        for key, value in (("days", args.days), ("schedule", args.schedule), ("rename", args.rename)):
            if value:
                patch[key] = value
        if args.digests:
            patch["digest_times"] = args.digests
        elif args.default_digests:
            patch["digest_times"] = []
        return change(patch)
    return change({"digest_times": args.times})


if __name__ == "__main__":
    sys.exit(main())
