#!/usr/bin/env python3
"""Checks for the ENOENT-tolerant `claude` spawn helper in the web gateway.

Every dashboard turn spawns a fresh `claude -p`. Claude Code's npm auto-updater
swaps /usr/bin/claude while it runs, so a spawn landing in that window raises
FileNotFoundError; _run_claude() has to outlive the swap instead of surfacing it
as "Sorry, an error occurred" in the user's conversation.

Covers: the transient window is absorbed, the wait is bounded by a deadline
(not a retry count), the deadline is env-tunable, and a permanently missing
binary still raises.

    python3 tests/test_web_gateway_claude_spawn.py
"""
import importlib.util
import os
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
                "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"):
        os.environ.pop(var, None)
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
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
    """Make the next `fail_times` spawns raise ENOENT; count all attempts."""
    clock = _FakeClock()
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise FileNotFoundError(2, "No such file or directory", cmd[0])
        return f"ran after {calls['n']} attempt(s)"

    wg.subprocess.run = fake_run
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


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wg = _load_gateway(tmp / "a", {})
        assert wg.CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS == 60.0, \
            wg.CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS
        test_absorbs_transient_window(wg)
        test_bounded_by_deadline_not_retry_count(wg)
        test_first_attempt_does_not_sleep(wg)

        wg_short = _load_gateway(
            tmp / "b", {"CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS": "5"})
        test_deadline_is_env_tunable(wg_short)
    print("all web-gateway claude-spawn tests passed")


if __name__ == "__main__":
    main()
