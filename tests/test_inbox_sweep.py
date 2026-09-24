#!/usr/bin/env python3
"""Checks for the inbox sweep's gate: what counts as pending, and when it spawns.

Runs against a synthetic chamber tree in a temp dir, with the credential
refresh and the `claude -p` subprocess stubbed -- so the manifest validation,
symlink handling and the re-spawn guard (including the retry after a failed
session) are exercised without spending anything.

    python3 tests/test_inbox_sweep.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "inbox-sweep.py"


def load():
    spec = importlib.util.spec_from_file_location("inbox_sweep", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


class Harness:
    """A chamber tree, a state file, and a fake `claude -p`."""

    def __init__(self, mod, root: Path):
        self.mod = mod
        self.chambers = root / "chambers"
        self.chambers.mkdir()
        mod.CHAMBERS_DIR = self.chambers
        mod.STATE_PATH = root / "state" / "state.json"
        self.spawns = []
        self.exit_code = 0
        self.now = 1_000_000.0
        mod.claude_auth = types.SimpleNamespace(
            ensure_fresh_credentials=lambda log=None: None)
        mod.session_env = types.SimpleNamespace(build=lambda model=None: {})
        mod.time = types.SimpleNamespace(time=lambda: self.now)

        def fake_run(cmd, cwd=None, env=None):
            self.spawns.append(cmd[-1])
            return types.SimpleNamespace(returncode=self.exit_code)

        mod.subprocess = types.SimpleNamespace(run=fake_run)

    def chamber(self, name, manifest):
        c = self.chambers / name
        c.mkdir()
        text = manifest if isinstance(manifest, str) else json.dumps(manifest)
        (c / ".inbox.json").write_text(text)
        return c

    def tick(self):
        """Run one sweep; return whether it spawned a session."""
        before = len(self.spawns)
        self.mod.main()
        return len(self.spawns) > before


def inbox_manifest(path="inbox"):
    return {"inboxes": [{"id": "in", "path": path}]}


def test_gate(mod, tmp: Path):
    print("gate and re-spawn guard")
    h = Harness(mod, tmp)
    c = h.chamber("docs", inbox_manifest())
    inbox = c / "inbox"
    inbox.mkdir()

    check("empty inbox spawns nothing", h.tick(), False)
    (inbox / ".gitkeep").write_text("")
    check("only .gitkeep spawns nothing", h.tick(), False)

    f = inbox / "letter.pdf"
    f.write_bytes(b"abc")
    check("a real file spawns", h.tick(), True)
    check("prompt lists the file", "letter.pdf" in h.spawns[-1], True)
    check("unchanged listing is guarded", h.tick(), False)

    # Same size, mtime moved by less than a second: whole-second mtimes
    # would miss this overwrite.
    st = f.stat()
    f.write_bytes(b"xyz")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1000))
    check("same-size overwrite within a second spawns", h.tick(), True)

    f.unlink()
    check("drained inbox spawns nothing", h.tick(), False)
    check("draining clears the guard",
          json.loads(mod.STATE_PATH.read_text()), {})


def test_duplicate_ids(mod, tmp: Path):
    print("inboxes sharing an id keep separate guards")
    h = Harness(mod, tmp)
    c = h.chamber("docs", {"inboxes": [{"id": "in", "path": "a"},
                                       {"id": "in", "path": "b"}]})
    (c / "a").mkdir()
    (c / "b").mkdir()
    (c / "a" / "one.pdf").write_text("1")
    (c / "b" / "two.pdf").write_text("2")
    check("first sweep spawns", h.tick(), True)
    (c / "a" / "three.pdf").write_text("3")
    check("a change in the first inbox spawns again", h.tick(), True)


def test_state_write(mod, tmp: Path):
    print("state is replaced atomically")
    h = Harness(mod, tmp)
    c = h.chamber("docs", inbox_manifest())
    (c / "inbox").mkdir()
    (c / "inbox" / "a.csv").write_text("1")
    h.tick()
    check("no temp file left behind",
          sorted(p.name for p in mod.STATE_PATH.parent.iterdir()),
          ["state.json"])


def test_failed_session(mod, tmp: Path):
    print("failed session is retried with backoff")
    h = Harness(mod, tmp)
    c = h.chamber("docs", inbox_manifest())
    (c / "inbox").mkdir()
    (c / "inbox" / "a.csv").write_text("1")

    h.exit_code = 1
    check("first attempt spawns", h.tick(), True)
    h.now += 3600
    check("retried on the next tick", h.tick(), True)
    h.now += 3600
    check("second failure backs off", h.tick(), False)
    h.now += 3600
    check("retried after two hours", h.tick(), True)

    h.exit_code = 0
    h.now += 4 * 3600
    check("retry after backoff succeeds", h.tick(), True)
    h.now += 3600
    check("a clean exit settles the guard", h.tick(), False)


def test_killed_session(mod, tmp: Path):
    print("a session killed by the scheduler timeout still backs off")
    h = Harness(mod, tmp)
    c = h.chamber("docs", inbox_manifest())
    (c / "inbox").mkdir()
    (c / "inbox" / "a.csv").write_text("1")

    # The scheduler kills the whole process group: subprocess.run never
    # returns, so nothing after it runs.
    class Killed(BaseException):
        pass

    def killed_run(cmd, cwd=None, env=None):
        h.spawns.append(cmd[-1])
        raise Killed()

    mod.subprocess = types.SimpleNamespace(run=killed_run)
    try:
        h.tick()
    except Killed:
        pass
    check("the attempt was recorded as a failure",
          json.loads(mod.STATE_PATH.read_text()).get("failures"), 1)
    h.now += 60
    check("the next tick does not spawn again at once", h.tick(), False)
    h.now += 3600
    try:
        spawned = h.tick()
    except Killed:
        spawned = True
    check("retried once the backoff has elapsed", spawned, True)


def test_manifests(mod, tmp: Path):
    print("manifest validation")
    h = Harness(mod, tmp)
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")

    h.chamber("a-broken", "{not json")
    h.chamber("b-null", {"inboxes": None})
    h.chamber("c-strings", {"inboxes": ["inbox"]})
    h.chamber("d-list", ["inbox"])
    h.chamber("e-absolute", inbox_manifest(str(outside)))
    h.chamber("f-parent", inbox_manifest("../../outside"))
    g = h.chamber("g-linked", inbox_manifest("inbox"))
    (g / "inbox").symlink_to(outside, target_is_directory=True)
    h.chamber("h-root", inbox_manifest("."))
    for name, dest in (("i-dest-abs", str(outside)),
                       ("j-dest-parent", "../outside"),
                       ("k-dest-root", ".")):
        d = h.chamber(name, {**inbox_manifest(),
                             "destinations": [{"path": "filed/"},
                                              {"path": dest}]})
        (d / "inbox").mkdir()
    h.chamber("l-dest-shape", {**inbox_manifest(), "destinations": "filed/"})
    ok = h.chamber("z-good", {**inbox_manifest(),
                              "destinations": [{"path": "filed/",
                                                "source": "any"}]})
    (ok / "inbox").mkdir()

    found = mod.declared_inboxes()
    check("only well-formed, contained inboxes are declared",
          [i["chamber"] for i in found], ["z-good"])

    (ok / "inbox" / "doc.pdf").write_text("x")
    check("a good chamber is still swept past broken ones", h.tick(), True)
    check("nothing from outside reaches the prompt",
          "secret.txt" in h.spawns[-1], False)


def test_symlinks(mod, tmp: Path):
    print("symlinks inside an inbox")
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    inbox = tmp / "inbox"
    inbox.mkdir()
    (inbox / "link.txt").symlink_to(outside / "secret.txt")
    (inbox / "linkdir").symlink_to(outside, target_is_directory=True)
    (inbox / "sub").mkdir()
    (inbox / "sub" / "real.txt").write_text("x")

    check("symlinked files and dirs are skipped",
          [p.relative_to(inbox).as_posix() for p in mod.pending_files(inbox)],
          ["sub/real.txt"])


def test_hidden(mod, tmp: Path):
    print("hidden entries inside an inbox")
    inbox = tmp / "inbox"
    (inbox / ".git" / "objects" / "ab").mkdir(parents=True)
    (inbox / ".git" / "objects" / "ab" / "cdef").write_text("x")
    (inbox / ".stfolder").mkdir()
    (inbox / ".stfolder" / "marker").write_text("x")
    (inbox / ".syncthing.letter.pdf.tmp").write_text("x")
    (inbox / "._letter.pdf").write_text("x")
    (inbox / "letter.pdf").write_text("x")

    check("hidden files and directories are skipped",
          [p.relative_to(inbox).as_posix() for p in mod.pending_files(inbox)],
          ["letter.pdf"])


def main():
    for test in (test_gate, test_duplicate_ids, test_state_write,
                 test_failed_session, test_killed_session,
                 test_manifests, test_symlinks, test_hidden):
        with tempfile.TemporaryDirectory() as d:
            test(load(), Path(d))
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
