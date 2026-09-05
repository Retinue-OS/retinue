#!/usr/bin/env python3
"""Checks for the attention policy (scripts/attention.py): the level table with
lead-time urgency, admission by sphere, tag and permit, breakpoints and the
sweep, corrections feeding the profile, and the life-store emit.

    python3 tests/test_attention.py
"""
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
    deep = A.mode_at(focus, at(10, 5))
    assert deep["id"] == "deep" and A.level(beat, at(10, 5)) == "time-sensitive"
    assert not A.breaks_through(beat, deep, profile, at(10, 5))
    work = A.mode_at(focus, at(14, 0))
    assert work["id"] == "work" and A.breaks_through(beat, work, profile, at(14, 0))
    social = A.mode_at(focus, at(19, 40))
    nda = item(importance=4, sphere="customers", sender="Beat Frei", due=at(22), lead=timedelta(days=2))
    assert not A.breaks_through(nda, social, profile, at(19, 40))
    assert A.set_permit(profile, "Beat Frei", "social", True, at(20, 30), focus["modes"])
    assert A.breaks_through(nda, social, profile, at(20, 30))
    assert "permit" in A.admission_reason(nda, social, profile, at(20, 30))
    insurance = item(importance=4, sphere="admin", tags=["health"], due=at(12, d=1), lead=timedelta(days=2))
    assert A.admitted(insurance, social, profile) and "tag health" in A.admission_reason(insurance, social, profile, at(19))
    assert A.set_admission(focus, "customers", "social", True) and A.admitted(beat, A.mode_at(focus, at(19)), profile)


def test_breakpoints():
    focus = A.default_focus()
    assert A.next_breakpoint(focus, at(6, 40)) == at(8)      # leaving Off at 07:00 is not a breakpoint
    assert A.next_breakpoint(focus, at(10)) == at(12)
    assert A.next_breakpoint(focus, at(15)) == at(17)
    assert A.next_breakpoint(focus, at(22, 30)) == at(8, d=1)
    focus["manual"] = "off"
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
    # Off: a breakpoint releases nothing
    off = A.breakpoint([mum], focus, at(6, 50))
    assert off["digest"] is None and not mum["released"]
    # the morning digest carries it
    bp = A.breakpoint([mum, alert, newsletter], focus, at(8, 0))
    assert bp["digest"] and [i["id"] for i in bp["digest"]["items"]] == ["chat:Mum"] and mum["released"]
    # the sweep escalates a released appointment into the next band and pushes it in Work
    physio = item(id="thr-physio", sphere="health", importance=4, kind_label="appointment", lead=timedelta(hours=2), due=at(15, 30), released=True, last_level="active")
    assert A.sweep([physio], focus, profile, at(13, 0)) == []
    effects = A.sweep([physio], focus, profile, at(13, 30))
    assert effects and effects[0]["type"] == "push" and physio["pushed"] == [at(13, 30)]
    # a held customer item stays held in Deep work even when it climbs
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
