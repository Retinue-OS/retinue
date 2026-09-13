#!/usr/bin/env python3
"""Checks for `is_due`'s optional `retry_after_seconds` (scripts/scheduler.py).

`write_state` persists `{"last_run", "status"}` on every run, success or not,
but until now nothing ever read `status` back: a job that failed three seconds
into its run became due again at exactly the same instant as one that
succeeded, so a transient failure (a rate limit, a flaky upstream) cost a
whole `interval_seconds` slot before the next attempt. `retry_after_seconds`
lets a job opt into a shorter wait after a non-success run, without changing
the default (no field set => unchanged behaviour) or making *any* non-success
status due at the very next tick, which would just trade one failure mode
(a burned slot) for another (a retry storm). It also must not fire early
for a job that has simply never run yet: a first sighting writes a
"scheduled" bookkeeping status (so the interval clock starts immediately
rather than on the first tick after the process restarts), which is not
"success" either, but is not a failed run to retry.

    python3 tests/test_scheduler_retry.py
"""
import datetime as dt
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_scheduler(tmp: Path):
    import os
    os.environ["SCHEDULER_STATE_DIR"] = str(tmp / "state")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["BASE_SCHEDULE"] = str(tmp / "no-base-schedule.json")
    os.environ["CLAUDE_CRED_FILE"] = str(tmp / "claude" / ".credentials.json")
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "scheduler_under_test_retry", SCRIPTS_DIR / "scheduler.py")
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


def _seed_state(tmp: Path, job_id: str, status: str, seconds_ago: float) -> None:
    """Write a state file as if the job last ran `seconds_ago` seconds ago --
    bypassing `write_state`, which always stamps "now", so a fixed elapsed
    time can be pinned for the check."""
    state_dir = tmp / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")
    (state_dir / f"{job_id}.json").write_text(json.dumps({"last_run": ts, "status": status}))


def test_failed_without_retry_after_waits_full_interval(sched, tmp):
    print("failed job, no retry_after_seconds: waits out the full interval")
    _seed_state(tmp, "j1", "failed", seconds_ago=400)
    job = {"id": "j1", "interval_seconds": 1800}
    check("not due before interval_seconds elapses", sched.is_due(job), False)


def test_failed_with_retry_after_elapsed(sched, tmp):
    print("failed job, retry_after_seconds elapsed: due early")
    _seed_state(tmp, "j2", "failed", seconds_ago=400)
    job = {"id": "j2", "interval_seconds": 1800, "retry_after_seconds": 300}
    check("due once retry_after_seconds has elapsed", sched.is_due(job), True)


def test_failed_with_retry_after_not_yet_elapsed(sched, tmp):
    print("failed job, retry_after_seconds not yet elapsed: still waits")
    _seed_state(tmp, "j3", "failed", seconds_ago=100)
    job = {"id": "j3", "interval_seconds": 1800, "retry_after_seconds": 300}
    check("not due before retry_after_seconds elapses", sched.is_due(job), False)


def test_success_ignores_retry_after_seconds(sched, tmp):
    print("successful job: retry_after_seconds never shortens the interval")
    _seed_state(tmp, "j4", "success", seconds_ago=400)
    job = {"id": "j4", "interval_seconds": 1800, "retry_after_seconds": 300}
    check("a success run is due only at interval_seconds", sched.is_due(job), False)


def test_interval_elapsed_wins_regardless_of_status(sched, tmp):
    print("interval_seconds elapsed: due whether or not retry_after_seconds is set")
    _seed_state(tmp, "j5", "failed", seconds_ago=2000)
    check("failed + interval elapsed, no retry_after_seconds",
          sched.is_due({"id": "j5", "interval_seconds": 1800}), True)
    _seed_state(tmp, "j6", "failed", seconds_ago=2000)
    check("failed + interval elapsed, retry_after_seconds set too",
          sched.is_due({"id": "j6", "interval_seconds": 1800, "retry_after_seconds": 300}), True)


def test_disabled_job_never_due(sched, tmp):
    print("a disabled job stays not-due even once retry_after_seconds elapses")
    _seed_state(tmp, "j7", "failed", seconds_ago=400)
    job = {"id": "j7", "interval_seconds": 1800, "retry_after_seconds": 300, "enabled": False}
    check("disabled beats retry_after_seconds", sched.is_due(job), False)


def test_first_sighting_with_retry_after_waits_full_interval(sched, tmp):
    print("first sighting of a job with retry_after_seconds: waits out the full "
          "interval, not just retry_after_seconds")
    job = {"id": "j8", "interval_seconds": 1800, "retry_after_seconds": 300}
    # No state file yet: is_due writes a "scheduled" bookkeeping entry (to
    # start the interval clock from now) and reports not due -- the job has
    # never actually run, so there is nothing to retry.
    check("first tick, no prior state: not due", sched.is_due(job), False)
    got_status = sched.read_last_status("j8")
    check("first tick wrote a 'scheduled' bookkeeping status", got_status, "scheduled")
    # Past retry_after_seconds (300s) but nowhere near interval_seconds
    # (1800s): if "scheduled" were mistaken for a failed run, the job would
    # wrongly fire here already, on what is really still its first run.
    _seed_state(tmp, "j8", "scheduled", seconds_ago=400)
    check("past retry_after_seconds on an unstarted first run: still not due",
          sched.is_due(job), False)
    # Only once the full interval_seconds has elapsed does the first run
    # become due -- confirming the fix does not also break the ordinary
    # first-run-after-one-interval case.
    _seed_state(tmp, "j8", "scheduled", seconds_ago=2000)
    check("past interval_seconds on an unstarted first run: due",
          sched.is_due(job), True)


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sched = _load_scheduler(tmp)
        test_failed_without_retry_after_waits_full_interval(sched, tmp)
        test_failed_with_retry_after_elapsed(sched, tmp)
        test_failed_with_retry_after_not_yet_elapsed(sched, tmp)
        test_success_ignores_retry_after_seconds(sched, tmp)
        test_interval_elapsed_wins_regardless_of_status(sched, tmp)
        test_disabled_job_never_due(sched, tmp)
        test_first_sighting_with_retry_after_waits_full_interval(sched, tmp)
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        sys.exit(1)
    print("all scheduler retry_after_seconds tests passed")


if __name__ == "__main__":
    main()
