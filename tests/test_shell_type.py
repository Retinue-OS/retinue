#!/usr/bin/env python3
"""Checks for scripts/shell-type.py and scripts/shell_actions.py.

Inside a dashboard thread's turn the gateway hands the session a manifest path
(RETINUE_REPLY_SHELL_ACTIONS_FILE); shell-type.py appends one validated JSON
action per call, and the gateway stores what validates on the reply when the
turn ends (shell_actions.parse_manifest). No request is made by the CLI.

Covers: each kind writes its canonical action; calls accumulate; Enter is off
unless asked; the call fails outside a thread turn; bad input (two modes,
--enter with --key, oversized or control-character text, unknown key) writes
nothing; and the gateway-side parser keeps valid lines, drops bad ones and
duplicate ids, and caps the count.

    python3 tests/test_shell_type.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "shell-type.py"

spec = importlib.util.spec_from_file_location(
    "shell_actions_under_test", REPO_ROOT / "scripts" / "shell_actions.py")
sa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sa)


def _run(args, manifest=None, stdin=None):
    env = {k: v for k, v in os.environ.items() if k != sa.MANIFEST_VAR}
    if manifest is not None:
        env[sa.MANIFEST_VAR] = str(manifest)
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, input=stdin,
                          capture_output=True, text=True, timeout=30)


def _lines(manifest: Path) -> list[dict]:
    return [json.loads(l) for l in manifest.read_text().splitlines() if l.strip()]


def test_each_kind(tmp: Path):
    m = tmp / "m1"
    assert _run(["Hello Quest"], m).returncode == 0
    assert _run(["--enter", "search terms"], m).returncode == 0
    assert _run(["--key", "tab"], m).returncode == 0
    r = _run(["--secret", "  Meta   account ", "--enter"], m)
    assert r.returncode == 0, r.stderr
    assert _run(["-"], m, stdin="from\nstdin").returncode == 0
    got = _lines(m)
    assert [a["kind"] for a in got] == ["keyboard-type", "keyboard-type", "keyboard-key",
                                        "keyboard-secret", "keyboard-type"], got
    assert got[0]["text"] == "Hello Quest" and got[0]["enter"] is False
    assert got[1]["enter"] is True
    assert got[2] == {"kind": "keyboard-key", "id": got[2]["id"], "key": "tab"}
    assert got[3]["label"] == "Meta account" and got[3]["enter"] is True
    assert "text" not in got[3], "a secret action carries no text"
    assert got[4]["text"] == "from\nstdin"
    assert len({a["id"] for a in got}) == 5
    for a in got:
        assert sa.normalize(a) == a, a
    print("ok: every kind is written in its canonical form; Enter only when asked")


def test_outside_a_turn():
    r = _run(["hi"])
    assert r.returncode == 2 and sa.MANIFEST_VAR in r.stderr, r.stderr
    print("ok: outside a dashboard thread turn the call fails")


def test_bad_input_writes_nothing(tmp: Path):
    m = tmp / "m2"
    for args, stdin in ((["hi", "--key", "enter"], None),
                        (["--key", "enter", "--enter"], None),
                        ([], None),
                        (["x" * (sa.MAX_TEXT + 1)], None),
                        (["bell\x07"], None),
                        (["-"], ""),
                        (["--secret", "   "], None)):
        r = _run(args, m, stdin=stdin)
        assert r.returncode == 2, (args, r.returncode, r.stderr)
    r = _run(["--key", "f13"], m)
    assert r.returncode != 0
    assert not m.exists(), "a refused call must not write"
    print("ok: refused input writes nothing")


def test_sensitive(tmp: Path):
    m = tmp / "m3"
    r = _run(["--sensitive", "--enter", "4711"], m)
    assert r.returncode == 0, r.stderr
    assert "4711" not in r.stdout + r.stderr, "the CLI never echoes a sensitive text"
    assert "do not repeat it" in r.stdout, r.stdout
    got = _lines(m)
    assert got[0]["sensitive"] is True and got[0]["text"] == "4711" and got[0]["enter"] is True
    assert _run(["--sensitive", "--key", "enter"], tmp / "m4").returncode == 2
    assert _run(["--sensitive", "--secret", "Wi-Fi"], tmp / "m4").returncode == 2
    assert not (tmp / "m4").exists()
    # A plain action stays as it was: no `sensitive` key at all.
    assert "sensitive" not in sa.normalize({"kind": "keyboard-type", "id": "e" * 32, "text": "x"})
    print("ok: --sensitive marks a typing action, goes with TEXT only, is never echoed")


def test_purge_and_describe():
    a = {"kind": "keyboard-type", "id": "a" * 32, "text": "hunter2", "enter": False,
         "sensitive": True}
    plain = {"kind": "keyboard-type", "id": "b" * 32, "text": "hello", "enter": False}
    msg = {"ts": "2026-11-06T15:00:00+00:00", "shell_actions": [dict(a), dict(plain)]}
    t0 = sa._age_seconds(msg["ts"], 0) * -1  # epoch of ts
    assert not sa.purge_sensitive(msg, t0 + 60), "young: kept"
    assert msg["shell_actions"][0]["text"] == "hunter2"
    assert sa.purge_sensitive(msg, t0 + sa.SENSITIVE_TTL_SECONDS)
    gone, kept = msg["shell_actions"]
    assert gone["text"] is None and gone["purged"] is True and gone["sensitive"] is True
    assert kept["text"] == "hello", "a plain text is never purged"
    msg2 = {"ts": "2026-11-06T15:00:00+00:00", "shell_actions": [dict(a)]}
    assert sa.purge_sensitive(msg2, t0, ids={"a" * 32}), "by id: at once"
    assert msg2["shell_actions"][0]["text"] is None
    assert sa.purge_sensitive({"ts": "garbage", "shell_actions": [dict(a)]}, t0), \
        "an unreadable time counts as expired"
    line = sa.describe(a)
    assert "hunter2" not in line and "sensitive" in line, line
    assert "'hello'" in sa.describe(plain)
    print("ok: sensitive text is purged after the TTL or by id, never plain text; "
          "the transcript line never carries it")


def test_parse_manifest():
    good = {"kind": "keyboard-type", "id": "a" * 32, "text": "x\r\ny", "enter": 1}
    raw = "\n".join([
        json.dumps(good),
        json.dumps(good),                                   # duplicate id
        "not json",
        json.dumps({"kind": "keyboard-type", "id": "b" * 32, "text": "\x1b[2J"}),
        json.dumps({"kind": "rm -rf", "id": "c" * 32}),
        json.dumps({"kind": "keyboard-key", "id": "XYZ", "key": "enter"}),
        json.dumps({"kind": "keyboard-secret", "id": "d" * 32, "label": "Wi-Fi",
                    "text": "leaked?"}),
    ])
    actions, problems = sa.parse_manifest(raw)
    assert actions == [
        {"kind": "keyboard-type", "id": "a" * 32, "text": "x\ny", "enter": True},
        {"kind": "keyboard-secret", "id": "d" * 32, "label": "Wi-Fi", "enter": False},
    ], actions
    assert len(problems) == 4, problems
    many = "\n".join(json.dumps({"kind": "keyboard-key", "id": f"{i:032x}", "key": "tab"})
                     for i in range(sa.MAX_ACTIONS + 3))
    actions, problems = sa.parse_manifest(many)
    assert len(actions) == sa.MAX_ACTIONS and problems, (len(actions), problems)
    print("ok: the gateway's parser keeps valid actions only, canonical, capped, "
          "and never carries text on a secret")


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_each_kind(tmp)
        test_outside_a_turn()
        test_bad_input_writes_nothing(tmp)
        test_parse_manifest()
        test_sensitive(tmp)
        test_purge_and_describe()
    print("all shell-type tests passed")


if __name__ == "__main__":
    main()
