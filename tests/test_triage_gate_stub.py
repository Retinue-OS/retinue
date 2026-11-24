#!/usr/bin/env python3
"""The retired e-mail triage gate is a stub (docs/mail-chats.md, "What retires").

`scripts/triage-gate.py` used to list the INBOX, file news, archive answered
mail and spawn triage sessions. The jobs that call it live in chambers'
`.schedule.json`, so the file stays until operators have removed them — and
must then do nothing, quietly: one line of notice, exit 0, whatever it is
called with. A job left behind must not fail, and must not touch a mailbox
the mail gateway now owns.

Standalone, no third-party deps:

    python3 tests/test_triage_gate_stub.py
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "triage-gate.py"

# Every way a chamber's job, or a hand, may still call it.
INVOCATIONS = (
    ["frequent"], ["daily"], ["news"], ["derive-whitelist"],
    [], ["--help"], ["no-such-mode", "--flag", "value"],
)


def _run(args, env):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, env=env, timeout=30)


def test_any_invocation_prints_one_notice_and_exits_zero():
    with tempfile.TemporaryDirectory() as tmp:
        # A home and a state directory of its own, and no way to a mailbox, a
        # backend or `claude`: the stub must not need any of them.
        env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp,
               "TRIAGE_STATE_DIR": str(Path(tmp) / "triage"),
               "CHAMBERS_DIR": str(Path(tmp) / "chambers"),
               "EMAIL_BACKEND_URL": "http://127.0.0.1:9/unreachable"}
        for args in INVOCATIONS:
            proc = _run(args, env)
            assert proc.returncode == 0, (args, proc.returncode, proc.stderr)
            lines = proc.stdout.splitlines()
            assert len(lines) == 1, (args, proc.stdout)
            notice = lines[0]
            assert "retired" in notice and "mail gateway" in notice, notice
            assert "docs/mail-chats.md" in notice and ".schedule.json" in notice, notice
            assert proc.stderr == "", (args, proc.stderr)
        # Nothing was created: no status store, no policy file, no chambers tree.
        assert sorted(p.name for p in Path(tmp).iterdir()) == [], list(Path(tmp).iterdir())


def test_the_stub_imports_nothing_that_could_act():
    """No mailbox client, no policy, no session spawn: the stub's only import
    is `sys` (and the annotations future), so nothing it could call exists."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported == {"__future__", "sys"}, imported


def test_the_exit_code_is_not_the_partial_one():
    """Exit 75 asks the scheduler to come back early (`resume_after_seconds`);
    a stub that answered it would be re-run on that cadence for ever."""
    sched = (REPO_ROOT / "scripts" / "scheduler.py").read_text(encoding="utf-8")
    assert "EXIT_PARTIAL = 75" in sched, "the scheduler's partial exit code moved"
    assert _run(["daily"], dict(os.environ)).returncode == 0


def _main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
