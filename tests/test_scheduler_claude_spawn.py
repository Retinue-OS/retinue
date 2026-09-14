#!/usr/bin/env python3
"""Checks for how the scheduler (scripts/scheduler.py) spawns its jobs.

A prompt job spawns a fresh `claude -p`. Before it does, the scheduler
refreshes an access token about to expire under the lock every framework
spawner shares (scripts/claude_auth.py) — once, ahead of the spawn — so the
session never starts with a refresh that races the gateway's turns or the
remote-control session for the token rotation. A command job runs a shell
command, which refreshes for itself if it spawns `claude` (the base-job
scripts do), so the scheduler must not do it there.

Every job — prompt or command — gets the allowlisted environment from
scripts/session_env.py rather than a copy of the daemon's, which is forked
before the entrypoint's scrub and holds whatever .env put into the container:
the mailbox password is absent by construction, e-mail is routed through the
gateway's backend, and the model stamp is set per job, never inherited.

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
    # Sandbox the pre-spawn credential refresh unconditionally: the module
    # reads this at import, and an inherited value would point the test at
    # a real credential file.
    os.environ["CLAUDE_CRED_FILE"] = str(tmp / "claude" / ".credentials.json")
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
    """Record the order of credential refreshes and spawns, and the
    environment each spawn was handed."""
    events = []
    envs = []

    def fake_spawn(cmd, **kwargs):
        events.append(("spawn", cmd if isinstance(cmd, str) else cmd[0]))
        envs.append(kwargs.get("env"))
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
    return events, envs


# What the daemon's environment holds that a job must never see, next to what
# a job needs; set on os.environ for the duration of a check.
_SECRETS = {"EMAIL_PASS": "mail-pw", "EMAIL_PASS_ARI": "ari-pw",
            "LITELLM_MASTER_KEY": "sk-master", "LITELLM_SALT_KEY": "salt",
            "OPENROUTER_API_KEY": "sk-or", "RETINUE_LITELLM_KEY": "sk-picker",
            "TRAEFIK_BASIC_AUTH_USERS": "u:$apr1$h", "GITHUB_TOKEN": "ghp_x",
            "CALDAV_PASSWORD": "caldav-pw"}
_NEEDED = {"EMAIL_BACKEND_TOKEN": "email-cap", "WEB_GATEWAY_PORT": "8181",
           "ANTHROPIC_API_KEY": "sk-ant", "CONVERSATION_BACKEND_TOKEN": "conv-cap",
           "NEWS_INGEST_TOKEN": "news-cap", "SIGNAL_GATEWAY_TOKEN": "signal-cap",
           "RETINUE_SESSION_MODEL": "stale-stamp",
           "EMAIL_BACKEND_URL": "http://stale:1/internal/email"}


class _daemon_environment:
    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in {**_SECRETS, **_NEEDED}}
        os.environ.update(_SECRETS)
        os.environ.update(_NEEDED)

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _assert_allowlisted(env, model):
    assert env is not None, "spawned without an explicit environment"
    for name in _SECRETS:
        assert name not in env, f"{name} inherited by the job"
    for name in ("EMAIL_BACKEND_TOKEN", "ANTHROPIC_API_KEY",
                 "CONVERSATION_BACKEND_TOKEN", "NEWS_INGEST_TOKEN",
                 "SIGNAL_GATEWAY_TOKEN"):
        assert env[name] == _NEEDED[name], name
    # The rewrite the scheduler always did: e-mail through the gateway, on
    # the gateway's port, replacing a stale inherited URL.
    assert env["EMAIL_BACKEND_URL"] == "http://localhost:8181/internal/email"
    if model:
        assert env["RETINUE_SESSION_MODEL"] == model
    else:
        assert "RETINUE_SESSION_MODEL" not in env, "a stale stamp was inherited"


def test_prompt_job_refreshes_once_before_spawning(sched, tmp):
    events, _ = _install_fakes(sched)
    sched.run_job({"id": "ask", "prompt": "hello", "interval_seconds": 60,
                   "_source": str(tmp / "chambers" / "x" / ".schedule.json")})
    assert events == [("refresh",), ("spawn", "claude")], events
    state = json.loads((tmp / "state" / "ask.json").read_text())
    assert state["status"] == "success", state
    log = (tmp / "state" / "scheduler.log").read_text()
    assert "[auth] ask: access token refreshed before spawn" in log, log


def test_command_job_does_not_refresh(sched, tmp):
    events, _ = _install_fakes(sched)
    sched.run_job({"id": "fetch", "command": "true", "interval_seconds": 60,
                   "_source": str(tmp / "chambers" / "x" / ".schedule.json")})
    assert events == [("spawn", "true")], events


def test_job_env_is_the_allowlist(sched):
    with _daemon_environment():
        _assert_allowlisted(sched.job_env("sonnet"), "sonnet")
        _assert_allowlisted(sched.job_env(""), "")
        _assert_allowlisted(sched.job_env(), "")


def test_every_job_is_spawned_with_the_allowlisted_env(sched, tmp):
    _, envs = _install_fakes(sched)
    source = str(tmp / "chambers" / "x" / ".schedule.json")
    with _daemon_environment():
        sched.run_job({"id": "pinned", "prompt": "hello", "interval_seconds": 60,
                       "model": "${RETINUE_TEST_JOB_MODEL:-sonnet}", "_source": source})
        sched.run_job({"id": "unpinned", "prompt": "hello", "interval_seconds": 60,
                       "_source": source})
        # A command job runs in the same trust position — the base-job
        # scripts spawn `claude -p` themselves — so it gets the same env.
        sched.run_job({"id": "cmd", "command": "true", "interval_seconds": 60,
                       "_source": source})
    assert len(envs) == 3, envs
    _assert_allowlisted(envs[0], "sonnet")
    _assert_allowlisted(envs[1], sched.CLAUDE_MODEL)
    _assert_allowlisted(envs[2], "")


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sched = _load_scheduler(tmp)
        test_prompt_job_refreshes_once_before_spawning(sched, tmp)
        test_command_job_does_not_refresh(sched, tmp)
        test_job_env_is_the_allowlist(sched)
        test_every_job_is_spawned_with_the_allowlisted_env(sched, tmp)
    print("all scheduler claude-spawn tests passed")


if __name__ == "__main__":
    main()
