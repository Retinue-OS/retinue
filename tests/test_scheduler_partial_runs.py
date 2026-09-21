#!/usr/bin/env python3
"""Checks for the scheduler's `partial` run state (scripts/scheduler.py).

A command job that works through a backlog in bounded slices exits with
EXIT_PARTIAL (75) to say "this slice is done and more remains". That must be
recorded as its own state: not a failure (no error text, no `[fail]` line to
chase), but not `success` either, so a job that pairs it with
`retry_after_seconds` is due again after that short wait rather than after its
full interval — which is what lets a backlog drain across runs instead of
having to fit one run's budget.

    python3 tests/test_scheduler_partial_runs.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_scheduler(tmp: Path):
    os.environ["SCHEDULER_STATE_DIR"] = str(tmp / "state")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["BASE_SCHEDULE"] = str(tmp / "no-base-schedule.json")
    os.environ["CLAUDE_CRED_FILE"] = str(tmp / "claude" / ".credentials.json")
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "scheduler_partial_under_test", SCRIPTS_DIR / "scheduler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Proc:
    pid = 4242

    def __init__(self, rc):
        self.returncode = rc

    def communicate(self, timeout=None):
        return "", "more to do"


def _run(sched, rc, job):
    logged = []
    sched.spawn_process = lambda cmd, **kw: _Proc(rc)
    sched.claude_auth.ensure_fresh_credentials = lambda **kw: {"action": "noop"}
    sched.log = lambda msg: logged.append(msg)
    sched.run_job({"id": "j", "_source": "/x/.schedule.json", **job})
    return logged


def test_exit_partial_is_recorded_as_partial_not_failed():
    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        logged = _run(sched, sched.EXIT_PARTIAL,
                      {"command": "true", "retry_after_seconds": 600})
        assert sched.read_last_status("j") == "partial"
        assert not [m for m in logged if m.startswith("[fail]")], logged
        parts = [m for m in logged if m.startswith("[partial]")]
        assert parts and "600s" in parts[0], parts
    print("  ok   exit 75 -> status partial, logged as such")


def test_a_partial_run_is_due_again_after_retry_after_seconds():
    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        job = {"id": "j", "_source": "/x/.schedule.json", "command": "true",
               "interval_seconds": 86400, "retry_after_seconds": 600}
        _run(sched, sched.EXIT_PARTIAL, job)
        # Just ran: not due yet under either clock.
        assert not sched.is_due(job)
        # Pretend 601s have passed: the short clock says due, the interval
        # would not for another day.
        real_now = sched.now
        sched.now = lambda: real_now() + 601
        try:
            assert sched.is_due(job), "partial run not brought forward"
        finally:
            sched.now = real_now
    print("  ok   a partial run is due after retry_after_seconds")


def test_without_retry_after_a_partial_run_waits_its_interval():
    # The safe default holds: a job that did not opt in is not hammered.
    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        job = {"id": "j", "_source": "/x/.schedule.json", "command": "true",
               "interval_seconds": 86400}
        _run(sched, sched.EXIT_PARTIAL, job)
        real_now = sched.now
        sched.now = lambda: real_now() + 3600
        try:
            assert not sched.is_due(job)
        finally:
            sched.now = real_now
    print("  ok   without retry_after_seconds a partial run waits its interval")


def test_any_other_nonzero_exit_is_still_a_failure():
    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        logged = _run(sched, 1, {"command": "false"})
        assert sched.read_last_status("j") == "failed"
        assert [m for m in logged if m.startswith("[fail]")], logged
    print("  ok   rc=1 is still recorded as failed")


if __name__ == "__main__":
    test_exit_partial_is_recorded_as_partial_not_failed()
    test_a_partial_run_is_due_again_after_retry_after_seconds()
    test_without_retry_after_a_partial_run_waits_its_interval()
    test_any_other_nonzero_exit_is_still_a_failure()
    print("all scheduler partial-run tests passed")
