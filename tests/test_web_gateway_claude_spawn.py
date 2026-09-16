#!/usr/bin/env python3
"""Checks for how the web gateway spawns `claude`.

Every dashboard turn spawns a fresh `claude -p`. Claude Code's npm auto-updater
swaps /usr/bin/claude while it runs, so a spawn landing in that window raises
FileNotFoundError; _run_claude() has to outlive the swap instead of surfacing it
as "Sorry, an error occurred" in the user's conversation.

Covers: the transient window is absorbed, the wait is bounded by a deadline
(not a retry count), the deadline is env-tunable, a permanently missing
binary still raises, and every spawn first runs the pre-spawn credential
refresh (scripts/claude_auth.py) exactly once, before the first attempt.

Also covers the environment a spawned session gets (scripts/session_env.py):
the gateway is forked before the entrypoint's scrub and holds the mailbox
credentials for its e-mail backend plus whatever else .env carries, and none
of it may reach a session — a dashboard turn, the transcript cleanup or the
presentation lint. The session gets the allowlist: the capability tokens, the
model credential, e-mail routed through the backend, the model stamp set per
spawn, and Ara junior's escalation flag only below the frontier tier.

    python3 tests/test_web_gateway_claude_spawn.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gateway(tmp: Path, env: dict[str, str]):
    """Load scripts/web-gateway.py with sandboxed state and a controlled env."""
    for var in ("CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS",
                "RETINUE_CONVERSATION_MODELS", "RETINUE_LITELLM_URL",
                "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
                "RETINUE_ROUTER_MODEL", "RETINUE_FRONTIER_MODEL",
                "RETINUE_CLAUDE_MODEL", "TRANSCRIPT_CLEANUP_MODEL",
                "PRESENTATION_LINT_MODEL", "CONVERSATION_BASE_URL"):
        os.environ.pop(var, None)
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    # Keep the pre-spawn credential refresh off the real credential file —
    # unconditionally, since an inherited value would point it at one. The
    # module reads this at import, so the first load pins it for the process.
    os.environ["CLAUDE_CRED_FILE"] = str(tmp / "claude" / ".credentials.json")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    os.environ.update(env)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_claude_spawn_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeClock:
    """Monotonic clock the test advances itself, so no test ever sleeps."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _install_fakes(wg, fail_times):
    """Make the next `fail_times` spawns raise ENOENT; count all attempts, and
    record how many attempts had been made whenever the pre-spawn credential
    refresh is called."""
    clock = _FakeClock()
    calls = {"n": 0, "auth": []}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise FileNotFoundError(2, "No such file or directory", cmd[0])
        return f"ran after {calls['n']} attempt(s)"

    def fake_ensure_fresh(**kwargs):
        calls["auth"].append(calls["n"])
        return {"action": "fresh"}

    wg.subprocess.run = fake_run
    wg.claude_auth.ensure_fresh_credentials = fake_ensure_fresh
    wg.time.monotonic = clock.monotonic
    wg.time.sleep = clock.sleep
    return clock, calls


def test_absorbs_transient_window(wg):
    """A swap lasting ~11 s — longer than the old 5 x 1 s budget — succeeds."""
    clock, calls = _install_fakes(wg, fail_times=22)  # 22 * 0.5 s = 11 s
    start = clock.now
    assert wg._run_claude(["/usr/bin/claude", "-p"]) == "ran after 23 attempt(s)"
    assert calls["n"] == 23, calls
    assert clock.now - start == 11.0, clock.now - start


def test_bounded_by_deadline_not_retry_count(wg):
    """A binary that never returns raises once the deadline passes."""
    clock, calls = _install_fakes(wg, fail_times=10_000)
    start = clock.now
    try:
        wg._run_claude(["/usr/bin/claude", "-p"])
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError after the deadline")
    elapsed = clock.now - start
    assert elapsed <= wg.CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS + 1.0, elapsed
    # Far more attempts than the old fixed budget of 5.
    assert calls["n"] > 100, calls


def test_first_attempt_does_not_sleep(wg):
    """The common case — binary present — costs no extra latency."""
    clock, calls = _install_fakes(wg, fail_times=0)
    start = clock.now
    assert wg._run_claude(["/usr/bin/claude", "-p"]) == "ran after 1 attempt(s)"
    assert calls["n"] == 1, calls
    assert clock.now == start


def test_refreshes_credentials_once_before_the_first_attempt(wg):
    """The pre-spawn refresh runs once per spawn, ahead of the first attempt —
    and is not repeated on the ENOENT retries."""
    clock, calls = _install_fakes(wg, fail_times=3)
    assert wg._run_claude(["/usr/bin/claude", "-p"]) == "ran after 4 attempt(s)"
    assert calls["auth"] == [0], calls


def test_deadline_is_env_tunable(wg_short):
    assert wg_short.CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS == 5.0
    clock, calls = _install_fakes(wg_short, fail_times=10_000)
    start = clock.now
    try:
        wg_short._run_claude(["/usr/bin/claude", "-p"])
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError after the deadline")
    assert clock.now - start <= 6.0, clock.now - start


# ── The spawned session's environment ────────────────────────────────────────

# What the gateway's own environment holds that a session must never see …
_SECRETS = {"EMAIL_PASS": "mail-pw", "EMAIL_PASS_ARI": "ari-pw",
            "LITELLM_MASTER_KEY": "sk-master", "LITELLM_SALT_KEY": "salt",
            "OPENROUTER_API_KEY": "sk-or", "RETINUE_LITELLM_KEY": "sk-picker",
            "TRAEFIK_BASIC_AUTH_USERS": "u:$apr1$h", "GITHUB_TOKEN": "ghp_x",
            "CALDAV_PASSWORD": "caldav-pw", "REPLY_TOKEN_KEY": "hmac"}
# … and what a session needs from it.
_NEEDED = {"EMAIL_BACKEND_TOKEN": "email-cap", "WEB_GATEWAY_PORT": "8080",
           "ANTHROPIC_API_KEY": "sk-ant", "CONVERSATION_BACKEND_TOKEN": "conv-cap",
           "NEWS_INGEST_TOKEN": "news-cap", "SIGNAL_GATEWAY_TOKEN": "signal-cap",
           "SPARQL_ENDPOINT_LIFE": "http://qlever-life:7001",
           "RETINUE_SESSION_MODEL": "stale-stamp",
           "RETINUE_ESCALATE_FILE": "/tmp/stale-flag"}


class _daemon_environment:
    """os.environ as the gateway daemon sees it, for the duration of a check."""

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


def _capture_spawns(wg, stdout: str):
    """Replace subprocess.run with a fake that records every spawn's argv and
    kwargs and answers with `stdout` as a successful `claude` run."""
    spawns = []

    def fake_run(cmd, **kwargs):
        spawns.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    wg.subprocess.run = fake_run
    wg.claude_auth.ensure_fresh_credentials = lambda **kw: {"action": "fresh"}
    return spawns


def _assert_allowlisted(env):
    assert env is not None, "spawned without an explicit environment"
    for name in _SECRETS:
        assert name not in env, f"{name} inherited by the session"
    for name in ("EMAIL_BACKEND_TOKEN", "ANTHROPIC_API_KEY",
                 "CONVERSATION_BACKEND_TOKEN", "NEWS_INGEST_TOKEN",
                 "SIGNAL_GATEWAY_TOKEN", "SPARQL_ENDPOINT_LIFE"):
        assert env[name] == _NEEDED[name], name
    # The gateway itself never had this (it is forked before the entrypoint
    # exports it), so a session used to run email_client.py against the
    # credentials it inherited. Now it has no credentials and the backend URL.
    assert env["EMAIL_BACKEND_URL"] == "http://localhost:8080/internal/email"
    assert env["PATH"] == os.environ["PATH"]


def test_dashboard_turn_env_is_allowlisted(wg):
    """A conversation turn: no secret, the stamp per spawn, no escalation flag
    when no frontier tier is configured."""
    spawns = _capture_spawns(wg, json.dumps({"result": "hi", "session_id": "s-1"}))
    with _daemon_environment():
        out = wg.send_message("hello", session_key="env-check", model="sonnet")
    assert out.get("response") == "hi", out
    assert len(spawns) == 1, spawns
    cmd, kwargs = spawns[0]
    assert kwargs["cwd"] == "/workspace"
    env = kwargs["env"]
    _assert_allowlisted(env)
    assert env["RETINUE_SESSION_MODEL"] == "sonnet"
    assert "RETINUE_ESCALATE_FILE" not in env, "senior has nobody to escalate to"
    assert "--model" in cmd and "sonnet" in cmd


def test_dashboard_turn_below_frontier_gets_the_escalation_flag(wg_tiered):
    """With a frontier tier configured, a router-tier turn is handed the flag;
    the re-run on the frontier tier is not."""
    spawns = _capture_spawns(wg_tiered,
                             json.dumps({"result": "ok", "session_id": "s-2"}))
    with _daemon_environment():
        out = wg_tiered.send_message("hello", session_key="env-tiered")
    assert out.get("response") == "ok", out
    _, kwargs = spawns[0]
    env = kwargs["env"]
    _assert_allowlisted(env)
    assert env["RETINUE_SESSION_MODEL"] == "haiku"
    flag = env["RETINUE_ESCALATE_FILE"]
    assert flag != "/tmp/stale-flag" and "retinue-escalate-" in flag, flag
    # Junior escalates: the same turn is re-run on the frontier tier, with the
    # frontier stamp and no flag.
    spawns.clear()

    def escalate_then_answer(cmd, **kwargs):
        spawns.append((cmd, kwargs))
        if len(spawns) == 1:
            Path(kwargs["env"]["RETINUE_ESCALATE_FILE"]).touch()
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": "senior", "session_id": "s-3"}),
            stderr="")

    wg_tiered.subprocess.run = escalate_then_answer
    with _daemon_environment():
        out = wg_tiered.send_message("harder", session_key="env-tiered-2")
    assert out.get("escalated") is True and out.get("response") == "senior", out
    assert len(spawns) == 2, spawns
    junior_env, senior_env = spawns[0][1]["env"], spawns[1][1]["env"]
    _assert_allowlisted(junior_env)
    _assert_allowlisted(senior_env)
    assert junior_env["RETINUE_SESSION_MODEL"] == "haiku"
    assert "RETINUE_ESCALATE_FILE" in junior_env
    assert senior_env["RETINUE_SESSION_MODEL"] == "opus"
    assert "RETINUE_ESCALATE_FILE" not in senior_env


def test_cleanup_and_lint_sessions_are_allowlisted_too(wg):
    """The tool-less helper sessions are `claude` processes all the same."""
    spawns = _capture_spawns(wg, json.dumps({"result": "hello world"}))
    with _daemon_environment():
        assert wg._cleanup_transcript("helo wrld") == "hello world"
    assert len(spawns) == 1, spawns
    _, kwargs = spawns[0]
    _assert_allowlisted(kwargs["env"])
    assert kwargs["env"]["RETINUE_SESSION_MODEL"] == wg.TRANSCRIPT_CLEANUP_MODEL

    spawns.clear()
    text = "Approve or deny the pending send at /sends/signal-gateway/abc when you can."
    wg.subprocess.run = lambda cmd, **kw: (
        spawns.append((cmd, kw)) or subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": text}), stderr=""))
    with _daemon_environment():
        assert wg._lint_presentation(text) == text
    assert len(spawns) == 1, spawns
    _, kwargs = spawns[0]
    _assert_allowlisted(kwargs["env"])
    assert kwargs["env"]["RETINUE_SESSION_MODEL"] == wg.PRESENTATION_LINT_MODEL


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wg = _load_gateway(tmp / "a", {})
        assert wg.CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS == 60.0, \
            wg.CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS
        test_absorbs_transient_window(wg)
        test_bounded_by_deadline_not_retry_count(wg)
        test_first_attempt_does_not_sleep(wg)
        test_refreshes_credentials_once_before_the_first_attempt(wg)
        test_dashboard_turn_env_is_allowlisted(wg)
        test_cleanup_and_lint_sessions_are_allowlisted_too(wg)

        wg_short = _load_gateway(
            tmp / "b", {"CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS": "5"})
        test_deadline_is_env_tunable(wg_short)

        wg_tiered = _load_gateway(
            tmp / "c", {"RETINUE_ROUTER_MODEL": "haiku",
                        "RETINUE_FRONTIER_MODEL": "opus"})
        test_dashboard_turn_below_frontier_gets_the_escalation_flag(wg_tiered)
    print("all web-gateway claude-spawn tests passed")


if __name__ == "__main__":
    main()
