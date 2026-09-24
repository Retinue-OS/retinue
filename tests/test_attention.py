#!/usr/bin/env python3
"""Checks for the attention policy (scripts/attention.py): the level table with
lead-time urgency, admission by sphere, tag and permit, breakpoints and the
sweep, corrections feeding the profile, and the life-store emit.

    python3 tests/test_attention.py
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import attention as A  # noqa: E402

TZ = timezone(timedelta(hours=2))
DAY0 = datetime(2026, 9, 3, 0, 0, tzinfo=TZ)


def at(h, m=0, d=0):
    return DAY0 + timedelta(days=d, hours=h, minutes=m)


def item(**kw):
    base = {"id": "x", "kind": "thread", "title": "x", "sphere": "customers", "tags": [], "importance": 4.0, "importance_from": "agent",
            "due": None, "lead": timedelta(days=3), "lead_from": "kind default", "kind_label": None, "actor": "you", "waiting_since": None,
            "sender": None, "critical": False, "state": "open", "released": False, "snoozed_until": None, "boost": 0, "last_level": None, "pushed": []}
    base.update(kw)
    return base


def test_level_table():
    now = at(10)
    assert A.level(item(importance=4, due=None), now) == "active"
    assert A.level(item(importance=4, due=at(10, d=4)), now) == "active"           # more than the 3 d lead left
    assert A.level(item(importance=4, due=at(10, d=2)), now) == "time-sensitive"   # within the lead
    assert A.level(item(importance=3, due=at(10, d=2)), now) == "active"
    assert A.level(item(importance=3, due=at(12)), now) == "active"                # within a third
    assert A.level(item(importance=1, due=at(12)), now) == "active"
    assert A.level(item(importance=1, due=at(10, d=2)), now) == "passive"
    assert A.level(item(importance=1, due=None), now) == "passive"
    assert A.level(item(importance=2, due=at(9)), now) == "active"                 # overdue
    assert A.level(item(importance=1, critical=True), now) == "critical"
    assert A.level(item(importance=1, due=None, boost=1), now) == "active"         # a repeat climbed one level


def test_lead_time_urgency():
    physio = item(importance=4, kind_label="appointment", lead=timedelta(hours=2), due=at(15, 30))
    assert A.level(physio, at(13, 0)) == "active"
    assert A.level(physio, at(13, 30)) == "time-sensitive"
    vat = item(importance=4, kind_label="tax filing", lead=timedelta(days=14), due=at(17, d=22))
    assert A.level(vat, at(16, 30)) == "active"
    profile = A.default_profile()
    learned = A.correct(vat, profile, {"lead": 28 * A.DAY}, at(16, 30))
    assert A.level(vat, at(16, 30)) == "time-sensitive"
    assert profile["leads"]["tax filing"] == 28 * A.DAY and learned and "tax filing" in learned[0]


def test_modes_and_admission():
    focus, profile = A.default_focus(), A.default_profile()
    beat = item(importance=4, sphere="customers", sender="Beat Frei", due=at(12, d=1), kind_label="customer request", lead=timedelta(days=2))
    # Focused on nothing in the morning (only critical rings), on customers
    # in the afternoon by the schedule's third element.
    morning = A.mode_at(focus, at(10, 5))
    assert morning["id"] == "focused" and not morning.get("subject") and A.level(beat, at(10, 5)) == "time-sensitive"
    assert not A.breaks_through(beat, morning, profile, at(10, 5))
    assert "on nothing" in A.admission_reason(beat, morning, profile, at(10, 5))
    focused = A.mode_at(focus, at(14, 0))
    assert focused["admits"] == ["customers"] and focused["subject"]["id"] == "customers"
    assert A.breaks_through(beat, focused, profile, at(14, 0))
    # Entered by hand with a subject, Focused admits that one sphere for the
    # stint — health still reaches it — and the stored rule is untouched.
    focus["manual"], focus["subject"] = "focused", "board-games"
    hobby = A.mode_at(focus, at(14, 0))
    assert hobby["admits"] == ["board-games"] and hobby["subject"]["id"] == "board-games"
    assert not A.breaks_through(beat, hobby, profile, at(14, 0))
    physio = item(sphere="health", importance=4, due=at(15, 30), lead=timedelta(hours=2))
    assert A.admitted(physio, hobby, profile) and "health everywhere" in A.admission_reason(physio, hobby, profile, at(14))
    assert focus["modes"]["focused"]["admits"] == []
    # …or on one project: what is about it gets through, the rest of its sphere does not.
    focus["subject"] = {"kind": "project", "id": "urn:retinue:project:brochure", "title": "Brochure"}
    one = A.mode_at(focus, at(14, 0))
    assert one["admits"] == [] and one["project"] == "urn:retinue:project:brochure"
    thread = item(sphere="customers", importance=4, due=at(16), lead=timedelta(hours=2), project="urn:retinue:project:brochure")
    assert A.admitted(thread, one, profile) and not A.admitted(beat, one, profile)
    assert A.admission_reason(thread, one, profile, at(14)) == "Focused on Brochure: this is about it"
    assert A.admission_reason(beat, one, profile, at(14)) == "Focused on Brochure — this is not"
    focus["manual"], focus["subject"] = None, None
    social = A.mode_at(focus, at(19, 40))
    nda = item(importance=4, sphere="customers", sender="Beat Frei", due=at(22), lead=timedelta(days=2))
    assert not A.breaks_through(nda, social, profile, at(19, 40))
    assert A.set_permit(profile, "Beat Frei", "social", True, at(20, 30), focus["modes"])
    assert A.breaks_through(nda, social, profile, at(20, 30))
    assert "permit" in A.admission_reason(nda, social, profile, at(20, 30))
    insurance = item(importance=4, sphere="admin", tags=["health"], due=at(12, d=1), lead=timedelta(days=2))
    assert A.admitted(insurance, social, profile) and "health everywhere" in A.admission_reason(insurance, social, profile, at(19))
    assert A.set_admission(focus, "customers", "social", True) and A.admitted(beat, A.mode_at(focus, at(19)), profile)


def test_breakpoints():
    focus = A.default_focus()
    assert A.next_breakpoint(focus, at(6, 40)) == at(8)      # leaving Rest at 07:00 is not a breakpoint
    assert A.next_breakpoint(focus, at(10)) == at(12)
    assert A.next_breakpoint(focus, at(15)) == at(17)
    assert A.next_breakpoint(focus, at(22, 30)) == at(8, d=1)
    focus["manual"] = "rest"
    assert A.next_breakpoint(focus, at(15)) == at(17)        # digest times still count under a manual mode


def test_arrival_breakpoint_sweep():
    focus, profile = A.default_focus(), A.default_profile()
    mum = item(id="chat:Mum", sphere="family", sender="Mum", importance=3, due=at(9), lead=timedelta(days=3))
    d = A.on_arrival(mum, focus, profile, at(6, 40))
    assert d["deliver"] == "hold" and d["until"] == at(8) and not mum["released"]
    alert = item(id="thr-backup", sphere="system", critical=True, importance=5)
    assert A.on_arrival(alert, focus, profile, at(10, 40))["deliver"] == "push" and alert["pushed"] == [at(10, 40)]
    newsletter = item(id="n", sphere="friends", importance=1)
    assert A.on_arrival(newsletter, focus, profile, at(10, 41))["deliver"] == "list" and newsletter["released"]
    # Rest: a breakpoint releases nothing
    rest = A.breakpoint([mum], focus, at(6, 50))
    assert rest["digest"] is None and not mum["released"]
    # the morning digest carries it, stamped with the digest it came in
    bp = A.breakpoint([mum, alert, newsletter], focus, at(8, 0))
    assert bp["digest"] and [i["id"] for i in bp["digest"]["items"]] == ["chat:Mum"] and mum["released"]
    assert mum["digest_at"] == at(8) and A.item_to_attention(mum)["digest_at"] == at(8).isoformat()
    # the sweep escalates a released appointment into the next band and pushes it in Work
    physio = item(id="thr-physio", sphere="health", importance=4, kind_label="appointment", lead=timedelta(hours=2), due=at(15, 30), released=True, last_level="active")
    assert A.sweep([physio], focus, profile, at(13, 0)) == []
    effects = A.sweep([physio], focus, profile, at(13, 30))
    assert effects and effects[0]["type"] == "push" and physio["pushed"] == [at(13, 30)]
    # a held customer item stays held in Flow even when it climbs
    beat = item(id="c", sphere="customers", sender="Beat Frei", importance=4, due=at(11, 30), lead=timedelta(hours=2), last_level="active")
    eff = A.sweep([beat], focus, profile, at(10, 0))
    assert eff and eff[0]["type"] == "climb" and not beat["released"]
    # snooze and pull
    until = A.snooze(alert, focus, at(10, 42), "next")
    assert until == at(12) and not alert["released"]
    assert A.pull(alert) and alert["released"]


def test_sections_and_explain():
    focus, profile = A.default_profile(), None
    focus = A.default_focus(); profile = A.default_profile()
    now = at(12, 5)
    items = [
        item(id="quote", importance=4, due=at(17), lead=timedelta(days=2), released=True, pushed=[]),
        item(id="vat", importance=4, due=at(17, d=22), lead=timedelta(days=14), released=True),
        item(id="mum", sphere="family", importance=3, due=at(18, d=1), lead=timedelta(days=3), released=True),
        item(id="held", importance=4, due=at(12, d=1), lead=timedelta(days=2), released=False),
        item(id="wait", actor="the accountant", waiting_since=at(10), importance=5),
    ]
    s = A.sections(items, focus, profile, now)
    assert [i["id"] for i in s["now"]] == ["quote"]
    assert [i["id"] for i in s["next"]] == ["vat", "mum"]
    assert [i["id"] for i in s["held"]] == ["held"] and [i["id"] for i in s["waiting"]] == ["wait"]
    x = A.explain(items[0], focus, profile, now)
    assert x["level"] == "time-sensitive" and x["importance"].startswith("4/5") and "in Now" in x["delivery"]
    assert "held until" in A.explain(items[3], focus, profile, now)["delivery"]
    assert "waiting on the accountant" in A.explain(items[4], focus, profile, now)["delivery"]


def test_fold_and_rules():
    focus, profile = A.default_focus(), A.default_profile()
    now = at(9, 0)  # Focused on nothing: admits nothing, lists only what it admits
    assert A.mode_at(focus, now)["only_admitted"]
    items = [
        item(id="quote", importance=4, due=at(17), lead=timedelta(days=2), released=True),
        item(id="alert", sphere="system", importance=5, critical=True, released=True),
        item(id="mum", sphere="family", sender="Mum", importance=3, released=True),
        item(id="card", importance=4, due=at(12, d=1), lead=timedelta(days=2), released=False),
        item(id="mine", importance=2.5, released=True, own=True),
    ]
    s = A.sections(items, focus, profile, now)
    assert [i["id"] for i in s["now"]] == ["alert"] and [i["id"] for i in s["held"]] == ["card"]
    assert [i["id"] for i in s["next"]] == ["mine"], "the user's own thread is never folded"
    assert [i["id"] for i in s["not_now"]] == ["quote", "mum"]
    # a pull keeps the item visible; a permit admits the sender
    assert A.pull(items[3]) and items[3]["pulled"]
    A.set_permit(profile, "Mum", "focused", True, now, focus["modes"])
    s = A.sections(items, focus, profile, now)
    assert [i["id"] for i in s["next"]] == ["card", "mum", "mine"] and [i["id"] for i in s["not_now"]] == ["quote"]
    # a fresh arrival or a snooze judges it afresh
    A.snooze(items[3], focus, now, "next")
    assert not items[3]["pulled"]
    A.on_arrival(items[3], focus, profile, now)
    assert not items[3]["pulled"] and A.item_to_attention(items[3])["pulled"] is False
    # the fold is a per-mode rule; the same patch changes the rest of the rules
    spheres = ["customers", "admin", "health", "friends", "family", "system"]
    assert A.apply_rules(focus, {"mode": "focused", "only_admitted": False}, spheres) == ["Focused lists everything"]
    assert [i["id"] for i in A.sections(items, focus, profile, now)["not_now"]] == []
    assert A.apply_rules(focus, {"mode": "focused", "only_admitted": False}, spheres) == []
    assert A.apply_rules(focus, {"mode": "chores", "deny": ["admin"], "tag_on": ["finance"]}, spheres) == ["Chores no longer admits admin", "Chores admits the tag finance"]
    assert focus["modes"]["chores"]["admits"] == ["customers", "health", "friends", "family", "system"] and focus["modes"]["chores"]["admit_tags"] == ["finance"]
    assert A.apply_rules(focus, {"mode": "social", "threshold": "active", "admits": ["family"]}, spheres) == ["Social rings from active", "Social no longer admits friends"]
    for bad in ({"mode": "nope"}, {"mode": "focused", "threshold": "passive"}, {"mode": "focused", "admit": ["pets"]},
                {"plan": "Workday", "schedule": [["25:00", "focused"]]}, {"plan": "Workday", "schedule": [["09:00", "focused", "pets"]]},
                {"plan": "Workday", "schedule": [["09:00", "chores", "customers"]]}, {"digest_times": ["noon"]},
                {"schedule": [["09:00", "chores"]]}):                 # two plans: say which
        try:
            A.apply_rules(focus, bad, spheres)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    assert A.apply_rules(focus, {"plan": "Workday", "schedule": [["07:30", "chores"], [540, "focused", "Customers"], ["22:00", "rest"]]}, spheres) \
        == ["Workday: the schedule changed"]
    assert A.apply_rules(focus, {"digest_times": ["08:00", "12:30", 1260]}, spheres) == ["digest times → 08:00, 12:30, 21:00"]
    assert focus["week"][0]["schedule"] == [[450, "chores"], [540, "focused", "customers"], [1320, "rest"]] and focus["digest_times"] == [480, 750, 1260]
    # No 00:00 entry: the day starts where the night before left off.
    assert A.day_schedule(focus, at(0).date())[0] == [0, "rest"]
    assert A.mode_at(focus, at(10))["admits"] == ["customers"]
    assert A.parse_minute("8") == 480 and A.parse_minute(True) is None


def test_week():
    """Day plans rule the days they name — ranges, lists, holidays — and
    every schedule question (the mode, the breakpoints, until when) is asked
    of the date's own plan, across midnight."""
    assert A.parse_days("mon-fri") == {"mon", "tue", "wed", "thu", "fri"}
    assert A.fmt_days(A.parse_days("fri-mon")) == ["mon", "fri-sun"]
    assert A.fmt_days(A.parse_days(["Monday", "tues", "Wed", "holidays"])) == ["mon-wed", "holiday"]
    assert A.fmt_days(A.parse_days("weekend")) == ["sat", "sun"] and A.parse_days("daily") == set(A.WEEKDAYS)
    for bad in ("funday", "mo-fr"):
        try:
            A.parse_days(bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    focus, spheres = A.default_focus(), ["customers", "admin", "health", "friends", "family", "system"]
    fri, sat, mon = at(0, d=1), at(0, d=2), at(0, d=4)      # DAY0 is a Thursday
    assert A.day_plan(focus, sat.date())["name"] == "Day off" and A.day_plan(focus, fri.date())["name"] == "Workday"
    assert A.mode_at(focus, at(10, d=2))["id"] == "social" and A.mode_at(focus, at(10, d=1))["id"] == "focused"
    # Friday night runs into Saturday's plan: its first digest, its Social.
    assert A.next_breakpoint(focus, at(22, 30, d=1)) == at(9, d=2)
    assert A.scheduled_until(focus, at(22, 30, d=1)) == at(9, d=2)
    assert A.next_breakpoint(focus, at(22, 30, d=3)) == at(8, d=4)       # Sunday night: Monday's 08:00
    assert A.due_events(focus, at(9, d=2)) == {"digest", "sweep"} and A.due_events(focus, at(8, d=2)) == {"sweep"}   # the workday digest is not Saturday's
    assert A.due_events(focus, at(0, d=4)) == {"sweep"}                  # Rest into Rest at midnight: no change
    item_ = item(id="h", sphere="customers")
    assert A.snooze(item_, focus, at(20, d=1), "tomorrow") == at(9, d=2)
    # Holidays follow the plan that claims them, whatever the weekday.
    assert A.apply_rules(focus, {"holiday_add": [f"{mon.date()}..{at(0, d=8).date()} Autumn break"]}, spheres, today=DAY0.date()) \
        == [f"holiday Autumn break, {mon.date()} – {at(0, d=8).date()}"]
    assert A.day_plan(focus, at(0, d=6).date())["name"] == "Day off" and A.mode_at(focus, at(10, d=6))["id"] == "social"
    assert A.next_breakpoint(focus, at(22, 30, d=3)) == at(9, d=4)       # the break starts on Monday
    # A plan of its own takes its days from the plan that had them; given
    # back, the emptied plan is gone. Every weekday stays in one plan.
    assert A.apply_rules(focus, {"plan": "Friday", "days": "fri", "schedule": "07:00 chores, 08:00 focused, 14:00 social, 22:00 rest"}, spheres) \
        == ["new day plan Friday: fri"]
    assert [p["days"] for p in focus["week"]] == [["mon-thu"], ["sat", "sun", "holiday"], ["fri"]]
    assert A.mode_at(focus, at(15, d=1))["id"] == "social"
    for bad in ({"plan": "Workday", "days": "mon-wed"}, {"plan": "Weekend", "days": "sat"},
                {"plan": "Friday", "rename": "workday"}, {"plan": "Friday", "schedule": "08:00 chores, 8:00 rest"},
                {"week": [{"name": "Only", "days": "daily", "schedule": "00:00 rest"}], "holiday_add": "2026-12-24"},
                {"holiday_add": "2026-12-24..2026-12-01"}, {"holiday_remove": "Easter"}):
        before = json.dumps(focus, sort_keys=True, default=str)
        try:
            A.apply_rules(focus, bad, spheres)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
        assert json.dumps(focus, sort_keys=True, default=str) == before, f"{bad} changed the rules before it was refused"
    assert A.apply_rules(focus, {"plan": "workday", "days": "weekdays", "holiday_remove": str(at(0, d=5).date())}, spheres) \
        == ["Workday: mon-fri", "Friday is gone — no days left", f"holiday Autumn break, {mon.date()} – {at(0, d=8).date()} removed"]
    assert [p["name"] for p in focus["week"]] == ["Workday", "Day off"] and focus["holidays"] == []
    # Holidays that are over go when the list changes.
    A.apply_rules(focus, {"holidays": ["2026-01-01 New Year", "2026-12-25"]}, spheres, today=DAY0.date())
    assert focus["holidays"] == [{"from": "2026-12-25", "to": "2026-12-25"}]
    # Before the week: a chosen schedule rules every day; the shipped one
    # gives way to the shipped week.
    assert A.upgrade_focus({"schedule": A.DEFAULT_SCHEDULE}) == {}
    old = A.upgrade_focus({"schedule": [[0, "rest"], [480, "chores"]]})
    assert old["week"] == [{"name": "Every day", "days": ["mon-sun", "holiday"], "schedule": [[0, "rest"], [480, "chores"]]}]
    assert A.mode_at({**A.default_focus(), **old}, at(10, d=2))["id"] == "chores"


def test_digest():
    """The digest is ranked as the list is, and says in one line per item
    why it is there; past five lines it counts the rest."""
    focus = A.default_focus()
    quiet = item(id="q", title="Newsletter", importance=2, preview="  Autumn   issue: what is new in the workshop programme this year and next  ")
    invite = item(id="i", title="Anna Keller", sphere="friends", importance=4, due=at(19), lead=timedelta(days=2))
    clause = item(id="c", title="Beat Frei", importance=4, due=at(12, d=1), lead=timedelta(hours=6))
    late = item(id="l", title="Card renewal", importance=3, due=at(7), lead=timedelta(days=3))
    alert = item(id="a", title="Backup failed", critical=True, importance=5)
    bp = A.breakpoint([quiet, invite, clause, late, alert], focus, at(12, 0, ) + timedelta(seconds=30))
    ranked = [i["id"] for i in bp["digest"]["items"]]
    assert ranked == ["a", "i", "c", "l", "q"], ranked                     # critical, time-sensitive, active by importance, passive
    assert bp["digest"]["at"] == at(12) and quiet["digest_at"] == at(12)   # to the minute: the link matches the rows
    title, body = A.digest_text(bp["digest"], at(12))
    assert title == "Digest 12:00 · 5 things waited"
    assert body.split("\n") == ["Backup failed — critical", "Anna Keller — due 19:00", "Beat Frei — due tomorrow 12:00",
                                "Card renewal — overdue since 07:00",
                                "Newsletter — Autumn issue: what is new in the workshop programme this…"], body
    many = {"at": at(12), "items": [item(id=str(k), title=f"T{k}") for k in range(7)]}
    assert A.digest_text(many, at(12))[1].split("\n")[-1] == "… and 2 more"
    assert A.fmt_when(at(9, d=3), at(12)) == at(9, d=3).strftime("%a 09:00") and A.fmt_when(at(9, d=30), at(12)) == f"{at(9, d=30).day} {at(9, d=30):%b}"
    # Judged again — a new arrival, a snooze, a pull — it is no longer the digest's.
    A.on_arrival(invite, focus, A.default_profile(), at(12, 5))
    A.snooze(clause, focus, at(12, 5), "next")
    assert invite["digest_at"] is None and clause["digest_at"] is None


def test_hand_set_modes():
    """Into Focused by hand is no breakpoint; any other change is. A timed
    mode brings its own breakpoints — every 55 minutes past an hour, and its
    end — and holds the day's digest times back while it runs."""
    focus = A.default_focus()
    assert A.set_manual(focus, "focused", None, at(14)) is False
    assert A.set_manual(focus, "chores", None, at(14)) is True and A.set_manual(focus, None, None, at(14)) is True
    A.set_manual(focus, "focused", None, at(14, 0) + timedelta(seconds=40), minutes=180)
    assert focus["manual_until"] == at(17).isoformat() and focus["breaks"] == [at(14, 55).isoformat(), at(15, 50).isoformat()]
    assert A.next_breakpoint(focus, at(14, 10)) == at(14, 55) and A.next_breakpoint(focus, at(16)) == at(17)
    assert A.due_events(focus, at(14, 55)) == {"break"} and A.due_events(focus, at(15, 0)) == {"sweep"}
    assert not A.manual_expired(focus, at(16, 59)) and A.manual_expired(focus, at(17))
    for minutes, breaks, mode, want in ((60, None, "focused", []), (120, False, "focused", []), (120, None, "rest", []),
                                       (70, None, "social", [at(14, 55)])):
        A.set_manual(focus, mode, None, at(14), minutes=minutes, breaks=breaks)
        assert focus["breaks"] == [b.isoformat() for b in want], (minutes, breaks, mode, focus["breaks"])
    # Across a digest time: 11:30 for an hour keeps 12:00 back; its end is the breakpoint.
    A.set_manual(focus, "focused", None, at(11, 30), minutes=60)
    assert A.next_breakpoint(focus, at(11, 40)) == at(12, 30) and "digest" not in A.due_events(focus, at(12))
    A.set_manual(focus, "focused", None, at(11, 30))                 # open-ended: the day's digest times count
    assert A.next_breakpoint(focus, at(11, 40)) == at(12) and "digest" in A.due_events(focus, at(12))
    assert A.minutes_until("17:00", at(15)) == 120 and A.minutes_until("08:00", at(15)) == 17 * 60
    assert A.minutes_until(at(16).isoformat(), at(15)) == 60 and A.minutes_until("noon", at(15)) is None
    digest = {"at": at(14, 55), "items": [item(title="Q")], "label": "Break 14:55"}
    assert A.digest_text(digest, at(14, 55))[0] == "Break 14:55 · 1 thing waited"


def test_docs_and_emit():
    profile = A.default_profile()
    doc = {"id": "8f2c", "title": "Quote for Müller AG", "attention": {"importance": 4, "due": "2026-09-03T17:00:00+02:00", "sphere": "customers", "tags": ["finance"], "kind": "customer request", "released": True}}
    it = A.item_from_doc(doc, "thread", profile)
    assert it["lead"] == timedelta(days=2) and it["due"].hour == 17 and it["released"]
    back = A.item_to_attention(it)
    assert back["lead"] == 2 * A.DAY and back["due"].startswith("2026-09-03T17:00")
    nt = A.to_ntriples([it], lambda i: f"urn:retinue:conversation:{i['id']}")
    assert nt == A.to_ntriples([it], lambda i: f"urn:retinue:conversation:{i['id']}")
    assert "<https://w3id.org/retinue/kb#importance>" in nt and "PT2880M" in nt and "urn:retinue:sphere:finance" in nt
    plain = A.item_from_doc({"id": "p", "title": "p"}, "chat", profile)
    assert plain["importance"] == A.DEFAULT_IMPORTANCE and plain["lead"] == timedelta(days=3)


def test_repeat_policy():
    focus = A.default_focus()
    assert A.repeat_policy(item(sphere="family"), A.mode_at(focus, at(6)))["escalate"]
    assert not A.repeat_policy(item(sphere="friends"), A.mode_at(focus, at(10)))["escalate"]
    assert not A.repeat_policy(item(sphere="family"), A.mode_at(focus, at(10)))["escalate"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok {t.__name__}")
    print(f"{len(tests)} checks passed")
