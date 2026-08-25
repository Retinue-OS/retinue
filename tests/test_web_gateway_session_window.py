#!/usr/bin/env python3
"""Checks how long a session stays resumable, and what happens when it isn't.

A dashboard thread is a standing conversation; expiring its Claude session after
an hour threw away the very context the thread exists to hold, and the wall
clock was the only criterion. Threads now keep a week-long window while
messenger keys keep the hour — which only works if a session Claude no longer
has (its transcripts expire after roughly 30 days) restarts instead of failing
the turn, and if the restart is handed the full transcript rather than the
resume-shaped prompt that assumed the session already had it.

Covers: the per-kind idle windows and their env overrides; the refusal
signature is recognised while an unrelated failure is not; a refused resume
respawns without --resume and with the caller's restart prompt; a non-resume
failure is not retried; and the state file creates its parent directory so it
can live on the persistent volume.

    python3 tests/test_web_gateway_session_window.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gateway(tmp: Path, env: dict[str, str] | None = None):
    """Load scripts/web-gateway.py with sandboxed state, as the sibling
    web-gateway tests do."""
    for var in ("RETINUE_CONVERSATION_MODELS", "RETINUE_LITELLM_URL",
                "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
                "SESSION_MAX_IDLE_SECONDS", "CONV_SESSION_MAX_IDLE_SECONDS",
                "RETINUE_CLAUDE_MODEL"):
        os.environ.pop(var, None)
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state" / "state.json")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    os.environ.update(env or {})
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_session_window_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Result:
    """The subset of CompletedProcess the gateway reads."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ok(session_id="new-session"):
    return _Result(stdout=json.dumps({"session_id": session_id, "result": "done"}))


# The verbatim line `claude --resume <unknown-id>` exits 1 with.
_REFUSAL = "No conversation found with session ID: 00000000-0000-4000-8000-000000000000"


def check_windows_differ_by_session_kind(gw):
    """A thread keeps a week; a messenger key keeps the hour it always had."""
    assert gw._max_idle_for("conv:abc") == 7 * 24 * 3600, gw._max_idle_for("conv:abc")
    assert gw._max_idle_for("signal:+41000000000") == 3600
    assert gw._max_idle_for(gw.DEFAULT_SESSION_KEY) == 3600
    assert gw._max_idle_for(None) == 3600

    # And _session_is_fresh applies them: same entry, opposite verdicts.
    entry = {"session_id": "s1", "last_activity": gw._now_ts() - 7200}
    assert gw._session_is_fresh(entry, "conv:abc") is True
    assert gw._session_is_fresh(entry, "signal:+41000000000") is False
    print("PASS thread sessions get a week, messenger sessions an hour")


def check_windows_are_env_tunable(tmp: Path):
    gw = _load_gateway(tmp / "env", {"SESSION_MAX_IDLE_SECONDS": "60",
                                     "CONV_SESSION_MAX_IDLE_SECONDS": "120"})
    assert gw._max_idle_for("conv:abc") == 120
    assert gw._max_idle_for("Web") == 60
    print("PASS both windows are env-tunable")


def check_refusal_is_recognised(gw):
    """Narrow match: only the resume refusal restarts, not any failure."""
    assert gw._resume_refused(_Result(1, stderr=_REFUSAL)) is True
    # Claude sometimes reports on stdout instead — both are read.
    assert gw._resume_refused(_Result(1, stdout=_REFUSAL)) is True
    assert gw._resume_refused(_Result(1, stderr="OAuth token has expired")) is False
    assert gw._resume_refused(_Result(0, stdout="{}")) is False
    print("PASS the refusal signature is matched, unrelated failures are not")


def _spy(gw, results):
    """Record every spawn and answer with the queued results in order."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return results[len(calls) - 1]

    gw._run_claude = fake_run
    return calls


def check_refused_resume_restarts_with_full_prompt(gw):
    key = "conv:restart"
    gw._update_session_entry(key, {"session_id": "gone", "last_activity": gw._now_ts()})
    calls = _spy(gw, [_Result(1, stderr=_REFUSAL), _ok("fresh-session")])

    out = gw.send_message("just the new message", session_key=key,
                          restart_message="the whole transcript")

    assert len(calls) == 2, calls
    assert "--resume" in calls[0] and "gone" in calls[0]
    assert calls[0][-1] == "just the new message"
    assert "--resume" not in calls[1], calls[1]
    assert calls[1][-1] == "the whole transcript", calls[1][-1]
    assert out["session_action"] == "restarted", out
    assert "error" not in out, out
    # The new session id replaces the dead one, so the next turn resumes it.
    assert gw._get_session_entry(key)["session_id"] == "fresh-session"
    print("PASS a refused resume restarts with the caller's full-context prompt")


def check_other_failure_is_not_retried(gw):
    """Re-running a turn cannot fix an expired sign-in — and costs a full turn."""
    key = "conv:noretry"
    gw._update_session_entry(key, {"session_id": "s2", "last_activity": gw._now_ts()})
    calls = _spy(gw, [_Result(1, stderr="OAuth token has expired")])

    out = gw.send_message("hello", session_key=key, restart_message="everything")

    assert len(calls) == 1, calls
    assert out["session_action"] == "resumed", out
    assert "error" in out, out
    print("PASS an unrelated failure is reported, not retried")


def check_state_file_creates_its_directory(gw):
    """The deployment points STATE_FILE at /root/.retinue/, which may not exist."""
    path = Path(gw.STATE_FILE)
    assert path.exists(), f"{path} was not written"
    assert json.loads(path.read_text()), "state file is empty"
    print("PASS the state file creates its parent directory")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        gw = _load_gateway(Path(tmp) / "main")
        check_windows_differ_by_session_kind(gw)
        check_windows_are_env_tunable(Path(tmp))
        check_refusal_is_recognised(gw)
        check_refused_resume_restarts_with_full_prompt(gw)
        check_other_failure_is_not_retried(gw)
        check_state_file_creates_its_directory(gw)
    print("all session-window checks passed")


if __name__ == "__main__":
    main()
