#!/usr/bin/env python3
"""Checks for a job's optional `timeout_seconds` (scripts/scheduler.py).

Until now every job shared one global SCHEDULER_JOB_TIMEOUT. One number cannot
fit both a ten-second health check and a catch-all that has to work through a
whole mailbox, and the failure mode of the mismatch is not a slow job but a
job that never finishes at all: the daily triage sweep was killed at the 900s
wall on every run, so it never reduced its own backlog, so the next run had
more to do and was killed earlier in the work. `timeout_seconds` lets such a
job state the budget its work actually needs.

The value must not be able to disable the timeout: the tick loop is
single-threaded, so an un-killable job wedges every other job behind it. A
non-positive or unparseable value therefore falls back to the global default
rather than being honoured.

    python3 tests/test_scheduler_job_timeout.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_scheduler(tmp: Path, *, global_timeout="900"):
    os.environ["SCHEDULER_STATE_DIR"] = str(tmp / "state")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["BASE_SCHEDULE"] = str(tmp / "no-base-schedule.json")
    os.environ["SCHEDULER_JOB_TIMEOUT"] = global_timeout
    # Sandbox the pre-spawn credential refresh: the module reads this at
    # import, and an inherited value would point the test at a real file.
    os.environ["CLAUDE_CRED_FILE"] = str(tmp / "claude" / ".credentials.json")
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "scheduler_timeout_under_test", SCRIPTS_DIR / "scheduler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeProc:
    """Records the timeout it was waited on with."""

    pid = 4242
    returncode = 0

    def __init__(self, seen):
        self.seen = seen

    def communicate(self, timeout=None):
        self.seen.append(timeout)
        return "{}", ""


def _run(sched, job):
    """Run one job against fakes; return (timeouts_waited_on, log_lines)."""
    seen, logged = [], []
    sched.spawn_process = lambda cmd, **kw: _FakeProc(seen)
    sched.claude_auth.ensure_fresh_credentials = lambda **kw: {"action": "noop"}
    real_log = sched.log
    sched.log = lambda msg: (logged.append(msg), real_log(msg))[0]
    sched.run_job({"id": "j", "_source": "/x/.schedule.json", **job})
    return seen, logged


def test_a_job_without_the_field_uses_the_global_default():
    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        seen, _ = _run(sched, {"command": "true"})
        assert seen == [900], f"expected the global default, got {seen}"
    print("  ok   no timeout_seconds -> SCHEDULER_JOB_TIMEOUT")


def test_a_job_can_buy_itself_more_wall_clock():
    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        seen, _ = _run(sched, {"command": "true", "timeout_seconds": 3600})
        assert seen == [3600], f"per-job budget ignored: {seen}"
    print("  ok   timeout_seconds overrides the global default")


def test_a_job_can_also_ask_for_less():
    # The override is not a "longer only" knob: a job that should never run
    # long is worth killing sooner than the global default would.
    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        seen, _ = _run(sched, {"command": "true", "timeout_seconds": 30})
        assert seen == [30], f"shorter budget ignored: {seen}"
    print("  ok   timeout_seconds may shorten the budget too")


def test_a_string_value_from_json_is_accepted():
    # Manifests are hand-edited JSON; "3600" is a plausible slip and means the
    # same thing. Coercing beats silently running on the wrong budget.
    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        seen, _ = _run(sched, {"command": "true", "timeout_seconds": "3600"})
        assert seen == [3600], f"string value not coerced: {seen}"
    print("  ok   a string timeout_seconds is coerced")


def test_a_nonsense_value_falls_back_and_says_so():
    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        seen, logged = _run(sched, {"command": "true", "timeout_seconds": "soon"})
        assert seen == [900], f"expected fallback, got {seen}"
        assert any("unusable timeout_seconds" in m for m in logged), logged
    print("  ok   an unparseable value falls back to the default, with a warning")


def test_zero_and_negative_never_disable_the_timeout():
    # The loop is single-threaded: a job that cannot be killed wedges every
    # other job behind it, so "no timeout" must not be expressible.
    for value in (0, -1, None):
        with tempfile.TemporaryDirectory() as tmp:
            sched = _load_scheduler(Path(tmp))
            seen, _ = _run(sched, {"command": "true", "timeout_seconds": value})
            assert seen == [900], f"{value!r} produced {seen}, not the default"
    print("  ok   0 / negative / null fall back rather than disabling the timeout")


def test_the_kill_log_reports_the_budget_that_was_actually_applied():
    # A timeout line naming the global default while the job ran on its own
    # budget would send the next reader looking for the wrong number.
    class _Timeout(Exception):
        pass

    with tempfile.TemporaryDirectory() as tmp:
        sched = _load_scheduler(Path(tmp))
        logged = []
        real_log = sched.log
        sched.log = lambda msg: (logged.append(msg), real_log(msg))[0]

        class _HangingProc:
            pid = 4242
            returncode = None

            def __init__(self):
                self.waits = 0

            def communicate(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise sched.subprocess.TimeoutExpired("cmd", timeout)
                return "", ""

        sched.spawn_process = lambda cmd, **kw: _HangingProc()
        sched.claude_auth.ensure_fresh_credentials = lambda **kw: {"action": "noop"}
        sched._kill_group = lambda pid, sig: None
        sched.run_job({"id": "j", "_source": "/x/.schedule.json",
                       "command": "sleep 99", "timeout_seconds": 3600})
        kills = [m for m in logged if m.startswith("[timeout]")]
        assert kills and "3600s" in kills[0], kills
        assert sched.read_last_status("j") == "timeout"
    print("  ok   the timeout log line names the job's own budget")


if __name__ == "__main__":
    test_a_job_without_the_field_uses_the_global_default()
    test_a_job_can_buy_itself_more_wall_clock()
    test_a_job_can_also_ask_for_less()
    test_a_string_value_from_json_is_accepted()
    test_a_nonsense_value_falls_back_and_says_so()
    test_zero_and_negative_never_disable_the_timeout()
    test_the_kill_log_reports_the_budget_that_was_actually_applied()
    print("all scheduler job-timeout tests passed")
