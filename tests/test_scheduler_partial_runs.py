#!/usr/bin/env python3
"""Checks for the scheduler's `partial` run state (scripts/scheduler.py).

A command job that works through a backlog in bounded slices exits with
EXIT_PARTIAL (75) to say "this slice is done and more remains". That must be
recorded as its own state: not a failure (no error text, no `[fail]` line to
chase), and resumed on the job's `resume_after_seconds` rather than its full
interval — which is what lets a backlog drain across runs instead of having
to fit one run's budget. That knob applies to `partial` only: a failing
session must not ride it into a ten-minute retry loop. (`retry_after_seconds`,
which covers any non-success, brings a partial run forward too.)

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
                      {"command": "true", "resume_after_seconds": 600})
        assert sched.read_last_status("j") == "partial"
        assert not [m for m in logged if m.startswith("[fail]")], logged
        parts = [m for m in logged if m.startswith("[partial]")]
        assert parts and "600s" in parts[0], parts
    print("  ok   exit 75 -> status partial, logged as such")


def test_a_partial_run_is_due_again_after_resume_after_seconds():
    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        job = {"id": "j", "_source": "/x/.schedule.json", "command": "true",
               "interval_seconds": 86400, "resume_after_seconds": 600}
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
    print("  ok   a partial run is due after resume_after_seconds")


def test_resume_after_never_applies_to_a_failed_run():
    # The reason this is its own knob: a job whose model session fails must
    # wait its interval (or an explicit retry_after_seconds), never be
    # re-spawned every few minutes on the strength of the resume clock.
    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        job = {"id": "j", "_source": "/x/.schedule.json", "command": "false",
               "interval_seconds": 86400, "resume_after_seconds": 600}
        _run(sched, 1, job)
        assert sched.read_last_status("j") == "failed"
        real_now = sched.now
        sched.now = lambda: real_now() + 3600
        try:
            assert not sched.is_due(job), "a failed run rode the resume clock"
        finally:
            sched.now = real_now
    print("  ok   resume_after_seconds ignores a failed run")


def test_retry_after_still_covers_a_partial_run():
    # One-knob deployments: partial is not success, so retry_after_seconds
    # brings it forward as it always has for any non-success.
    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        job = {"id": "j", "_source": "/x/.schedule.json", "command": "true",
               "interval_seconds": 86400, "retry_after_seconds": 600}
        _run(sched, sched.EXIT_PARTIAL, job)
        real_now = sched.now
        sched.now = lambda: real_now() + 601
        try:
            assert sched.is_due(job)
        finally:
            sched.now = real_now
    print("  ok   retry_after_seconds still brings a partial run forward")


def test_without_either_knob_a_partial_run_waits_its_interval():
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
    print("  ok   without a knob a partial run waits its interval")


def test_a_malformed_resume_or_retry_value_never_breaks_the_tick():
    # is_due runs for every job inside the loop's one try/except: a value that
    # raised would skip every job after this one, on every tick. Unusable
    # reads as unset -- the job waits its interval -- and warns once.
    for field in ("resume_after_seconds", "retry_after_seconds"):
        for bad in ("soon", "", 0, -5, [1]):
            with tempfile.TemporaryDirectory() as tmp:
                sched = _load_scheduler(Path(tmp))
                job = {"id": "j", "_source": "/x/.schedule.json", "command": "true",
                       "interval_seconds": 86400, field: bad}
                # The run's own log (the [partial] line reads the field too)
                # and the due checks' log are one list: warned once overall.
                logged = _run(sched, sched.EXIT_PARTIAL, job)
                sched.log = lambda msg: logged.append(msg)
                real_now = sched.now
                sched.now = lambda: real_now() + 3600
                try:
                    assert not sched.is_due(job), (field, bad)
                    assert not sched.is_due(job), (field, bad)
                finally:
                    sched.now = real_now
                warns = [m for m in logged if "unusable " + field in m]
                assert len(warns) == 1, (field, bad, warns)
    print("  ok   a malformed resume/retry value is ignored, warned once, never raises")


def test_the_partial_log_line_names_the_clock_that_actually_applies():
    # Both opt-in waits cover a partial run, so the shorter one wins in
    # is_due(); the log must say that one, and never echo an unusable value.
    cases = (
        ({"resume_after_seconds": 600, "retry_after_seconds": 300}, "300s"),
        ({"resume_after_seconds": 300, "retry_after_seconds": 600}, "300s"),
        ({"resume_after_seconds": "soon"}, "86400s"),
        ({}, "86400s"),
    )
    for fields, expected in cases:
        with tempfile.TemporaryDirectory() as tmp:
            sched = _load_scheduler(Path(tmp))
            logged = _run(sched, sched.EXIT_PARTIAL,
                          {"command": "true", "interval_seconds": 86400, **fields})
            parts = [m for m in logged if m.startswith("[partial]")]
            assert parts and expected in parts[0] and "soons" not in parts[0], (fields, parts)
    print("  ok   the partial log line reports the clock is_due applies")


def test_any_other_nonzero_exit_is_still_a_failure():
    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        logged = _run(sched, 1, {"command": "false"})
        assert sched.read_last_status("j") == "failed"
        assert [m for m in logged if m.startswith("[fail]")], logged
    print("  ok   rc=1 is still recorded as failed")


if __name__ == "__main__":
    test_exit_partial_is_recorded_as_partial_not_failed()
    test_a_partial_run_is_due_again_after_resume_after_seconds()
    test_resume_after_never_applies_to_a_failed_run()
    test_retry_after_still_covers_a_partial_run()
    test_without_either_knob_a_partial_run_waits_its_interval()
    test_a_malformed_resume_or_retry_value_never_breaks_the_tick()
    test_the_partial_log_line_names_the_clock_that_actually_applies()
    test_any_other_nonzero_exit_is_still_a_failure()
    print("all scheduler partial-run tests passed")
