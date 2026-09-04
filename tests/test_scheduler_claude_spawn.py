#!/usr/bin/env python3
"""Checks for the pre-spawn credential refresh in the scheduler (scripts/scheduler.py).

A prompt job spawns a fresh `claude -p`. Before it does, the scheduler
refreshes an access token about to expire under the lock every framework
spawner shares (scripts/claude_auth.py) — once, ahead of the spawn — so the
session never starts with a refresh that races the gateway's turns or the
remote-control session for the token rotation. A command job runs a shell
command, which refreshes for itself if it spawns `claude` (the base-job
scripts do), so the scheduler must not do it there.

    python3 tests/test_scheduler_claude_spawn.py
"""
import importlib.util
import json
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
    os.environ.setdefault("CLAUDE_CRED_FILE", str(tmp / "claude" / ".credentials.json"))
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "scheduler_under_test", SCRIPTS_DIR / "scheduler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeProc:
    pid = 4242
    returncode = 0

    def communicate(self, timeout=None):
        return "{}", ""


def _install_fakes(sched):
    """Record the order of credential refreshes and spawns."""
    events = []

    def fake_spawn(cmd, **kwargs):
        events.append(("spawn", cmd if isinstance(cmd, str) else cmd[0]))
        return _FakeProc()

    def fake_ensure_fresh(**kwargs):
        events.append(("refresh",))
        # The scheduler hands its own logger in, so a refresh shows up in the
        # scheduler log with the job id.
        assert callable(kwargs.get("log")), kwargs
        kwargs["log"]("access token refreshed before spawn")
        return {"action": "refreshed"}

    sched.spawn_process = fake_spawn
    sched.claude_auth.ensure_fresh_credentials = fake_ensure_fresh
    return events


def test_prompt_job_refreshes_once_before_spawning(sched, tmp):
    events = _install_fakes(sched)
    sched.run_job({"id": "ask", "prompt": "hello", "interval_seconds": 60,
                   "_source": str(tmp / "chambers" / "x" / ".schedule.json")})
    assert events == [("refresh",), ("spawn", "claude")], events
    state = json.loads((tmp / "state" / "ask.json").read_text())
    assert state["status"] == "success", state
    log = (tmp / "state" / "scheduler.log").read_text()
    assert "[auth] ask: access token refreshed before spawn" in log, log


def test_command_job_does_not_refresh(sched, tmp):
    events = _install_fakes(sched)
    sched.run_job({"id": "fetch", "command": "true", "interval_seconds": 60,
                   "_source": str(tmp / "chambers" / "x" / ".schedule.json")})
    assert events == [("spawn", "true")], events


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sched = _load_scheduler(tmp)
        test_prompt_job_refreshes_once_before_spawning(sched, tmp)
        test_command_job_does_not_refresh(sched, tmp)
    print("all scheduler claude-spawn tests passed")


if __name__ == "__main__":
    main()
