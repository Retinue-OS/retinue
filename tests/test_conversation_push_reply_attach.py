#!/usr/bin/env python3
"""Checks for conversation-push.py --reply-attach.

Inside a dashboard thread's turn the gateway hands the session a manifest path
(RETINUE_REPLY_ATTACHMENTS_FILE); --reply-attach lists files there, and the
gateway attaches them to the reply it appends when the turn ends. No request is
made by the CLI itself.

Covers: paths are appended as absolute paths (the gateway does not share the
session's cwd), repeated calls accumulate, a missing file fails before anything
is written, the call fails outside a thread turn (no manifest variable) with a
pointer to --thread --attach, and it refuses to be mixed with other options.

    python3 tests/test_conversation_push_reply_attach.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "conversation-push.py"


def _run(args, cwd, manifest=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("RETINUE_REPLY_ATTACHMENTS_FILE",)}
    env["CONVERSATION_BACKEND_TOKEN"] = "t"
    # Unroutable, so an accidental request fails loudly instead of posting.
    env["CONVERSATION_BACKEND_URL"] = "http://127.0.0.1:9/internal/conversations"
    if manifest is not None:
        env["RETINUE_REPLY_ATTACHMENTS_FILE"] = str(manifest)
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=30)


def test_appends_absolute_paths(tmp: Path):
    (tmp / "chart.png").write_bytes(b"png")
    (tmp / "data.csv").write_text("a,b\n")
    manifest = tmp / "manifest"
    r = _run(["--reply-attach", "chart.png"], tmp, manifest)
    assert r.returncode == 0, r.stderr
    r = _run(["--reply-attach", "data.csv", "--reply-attach", str(tmp / "chart.png")],
             tmp, manifest)
    assert r.returncode == 0, r.stderr
    lines = manifest.read_text().splitlines()
    assert lines == [str((tmp / "chart.png").resolve()), str((tmp / "data.csv").resolve()),
                     str((tmp / "chart.png").resolve())], lines
    print("ok: --reply-attach appends absolute paths to the turn's manifest")


def test_missing_file_writes_nothing(tmp: Path):
    (tmp / "ok.txt").write_text("x")
    manifest = tmp / "manifest-missing"
    r = _run(["--reply-attach", "ok.txt", "--reply-attach", "nope.txt"], tmp, manifest)
    assert r.returncode == 2 and "not found" in r.stderr, (r.returncode, r.stderr)
    assert not manifest.exists(), "a failed call must not list a partial set"
    print("ok: a missing file fails before anything is listed")


def test_outside_a_thread_turn(tmp: Path):
    (tmp / "ok.txt").write_text("x")
    r = _run(["--reply-attach", "ok.txt"], tmp)
    assert r.returncode == 2, r.returncode
    assert "RETINUE_REPLY_ATTACHMENTS_FILE" in r.stderr and "--thread" in r.stderr, r.stderr
    print("ok: outside a thread turn it fails and points at --thread --attach")


def test_refuses_other_options(tmp: Path):
    (tmp / "ok.txt").write_text("x")
    manifest = tmp / "manifest-mixed"
    for extra in (["some text"], ["--thread", "a" * 32], ["--attach", "ok.txt"],
                  ["--title", "T"], ["--timeout", "1"], ["--importance", "4"],
                  ["--critical"], ["--tag", "friends"]):
        r = _run(["--reply-attach", "ok.txt", *extra], tmp, manifest)
        assert r.returncode == 2, (extra, r.returncode, r.stderr)
    assert not manifest.exists()
    print("ok: --reply-attach is not combinable with other options")


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_appends_absolute_paths(tmp)
        test_missing_file_writes_nothing(tmp)
        test_outside_a_thread_turn(tmp)
        test_refuses_other_options(tmp)
    print("all conversation-push --reply-attach tests passed")


if __name__ == "__main__":
    main()
