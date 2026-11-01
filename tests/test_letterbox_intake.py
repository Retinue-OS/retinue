#!/usr/bin/env python3
"""Checks for the letterbox's typed intake (scripts/letterbox-intake.py).

Runs against a synthetic letterbox and chambers in a temp dir — the chambers
real git repositories with a bare remote, so commit and push are the real
thing — with the clock faked, so stability and the commit debounce are
stepped through deterministically:

- the default manifest is written into an empty letterbox;
- hidden, sync-tool and in-flight names are never taken;
- a file is taken only once its size and mtime held still;
- `accepts` matching: glob, `match.json` top-level equality, no match (left
  for the sweep), more than one (left, logged, one alert), declarations in
  the letterbox's own manifest ignored;
- filing at <path>/<YYYY>/<MM>/<DD>/<name> (UTC arrival date), an identical
  resend dropped, a different file of the same name kept with a suffix;
- on_receive: cwd the chamber root, the filed path as last argument, a
  failure logged with the file staying filed;
- the commit debounce: the first filing commits on the next pass, later ones
  inside the interval wait for it, a failed commit stays pending, and a
  restarted watcher commits what the old one left pending.

    python3 tests/test_letterbox_intake.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


DERIVE = """\
import json, os, sys
from pathlib import Path
path = Path(sys.argv[-1])
Path("derived").mkdir(exist_ok=True)
with open("derived/calls.jsonl", "a") as f:
    f.write(json.dumps({"cwd": os.getcwd(), "argv": sys.argv[1:]}) + "\\n")
if json.loads(path.read_text()).get("fail_derive"):
    sys.exit(3)
"""


class Harness:
    def __init__(self, root: Path):
        self.root = root
        self.chambers = root / "chambers"
        self.chambers.mkdir()
        self.box = self.chambers / "inbox"
        os.environ["LETTERBOX_DIR"] = str(self.box)
        import letterbox
        letterbox.LETTERBOX_DIR = self.box
        spec = importlib.util.spec_from_file_location(
            "letterbox_intake", REPO_ROOT / "scripts" / "letterbox-intake.py")
        self.mod = mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.letterbox = letterbox
        mod.CHAMBERS_DIR = self.chambers
        mod.STATE_PATH = root / "state" / "state.json"
        mod.STABLE_SECONDS = 3
        mod.COMMIT_INTERVAL = 600
        self.now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        mod._now = lambda: self.now
        self.alerts = []
        mod.alert = lambda title, text: self.alerts.append(text)
        self.watcher = mod.Watcher(self.box)

    def chamber(self, name, accepts, repo=True):
        c = self.chambers / name
        c.mkdir()
        (c / ".inbox.json").write_text(json.dumps({"inboxes": [], "accepts": accepts}))
        (c / ".retinue/scripts").mkdir(parents=True)
        (c / ".retinue/scripts/derive.py").write_text(DERIVE)
        if repo:
            remote = self.root / f"{name}.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            git(c, "init", "-q", "-b", "main")
            git(c, "config", "user.name", "Test")
            git(c, "config", "user.email", "test@example.com")
            git(c, "remote", "add", "origin", str(remote))
            git(c, "add", "-A")
            git(c, "commit", "-q", "-m", "init")
            git(c, "push", "-q", "-u", "origin", "main")
        return c

    def drop(self, name, doc):
        p = self.box / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        return p

    def settle(self):
        """Two passes STABLE_SECONDS apart: seen, then taken."""
        self.watcher.tick()
        self.now += 4
        self.watcher.tick()

    def restart(self):
        self.watcher = self.mod.Watcher(self.box)


def accept(glob="*.sensor.json", json_match=None, path="data/raw/", derive=True):
    entry = {"name": "sensor", "match": {"glob": glob}, "path": path}
    if json_match is not None:
        entry["match"]["json"] = json_match
    if derive:
        entry["on_receive"] = f"{sys.executable} .retinue/scripts/derive.py"
    return entry


def commits(c):
    return int(git(c, "rev-list", "--count", "HEAD").strip())


def test_basics(root):
    print("manifest, ignored names, stability, matching")
    h = Harness(root)
    h.box.mkdir()
    h.watcher.tick()
    manifest = json.loads((h.box / ".inbox.json").read_text())
    check("default manifest written, root is the inbox", manifest["inboxes"][0]["path"], ".")
    c = h.chamber("lab", [accept(json_match={"schema": "s/1"}, derive=False)], repo=False)
    for name in (".syncthing.a.sensor.json.tmp", "._x.sensor.json", "x.sensor.json.part",
                 ".stfolder", ".DS_Store"):
        h.drop(name, {"schema": "s/1"})
    (h.box / ".stversions").mkdir()
    (h.box / ".stversions" / "old.sensor.json").write_text('{"schema": "s/1"}')
    h.settle()
    check("hidden, sync-tool and in-flight files are left alone",
          sorted(p.name for p in h.box.iterdir() if p.is_file()),
          sorted([".DS_Store", ".inbox.json", "._x.sensor.json", ".stfolder",
                  ".syncthing.a.sensor.json.tmp", "x.sensor.json.part"]))
    check("nothing filed", (c / "data").exists(), False)

    h.drop("a.sensor.json", {"schema": "s/1", "n": 1})
    h.watcher.tick()
    check("a fresh file is not taken at once", (h.box / "a.sensor.json").exists(), True)
    h.now += 2
    (h.box / "a.sensor.json").write_text(json.dumps({"schema": "s/1", "n": 12}))
    h.watcher.tick()
    h.now += 2
    h.watcher.tick()
    check("a file still growing is not taken", (h.box / "a.sensor.json").exists(), True)
    h.now += 2
    h.watcher.tick()
    check("taken once it held still", (h.box / "a.sensor.json").exists(), False)
    filed = c / "data/raw/2026/11/01/a.sensor.json"
    check("filed at <path>/<YYYY>/<MM>/<DD>/<name>, UTC date of arrival", filed.is_file(), True)

    # Seen two seconds before midnight UTC, taken after it: filed under the
    # day it arrived.
    h.now = datetime(2026, 11, 1, 23, 59, 58, tzinfo=timezone.utc).timestamp()
    h.drop("late.sensor.json", {"schema": "s/1"})
    h.settle()
    check("the UTC date is the arrival's, not the filing's",
          (c / "data/raw/2026/11/01/late.sensor.json").is_file(), True)

    h.drop("b.sensor.json", {"schema": "other/1"})
    h.drop("c.txt", "hello")
    h.settle()
    check("match.json mismatch and glob mismatch stay for the sweep",
          ((h.box / "b.sensor.json").exists(), (h.box / "c.txt").exists()), (True, True))
    h.drop("big.sensor.json", {"schema": "s/1", "pad": "x" * 100})
    h.mod.JSON_MATCH_MAX = 50
    h.settle()
    check("too large to inspect for match.json: no match",
          (h.box / "big.sensor.json").exists(), True)


def test_ambiguous_and_own_manifest(root):
    print("ambiguity, the letterbox's own manifest")
    h = Harness(root)
    h.box.mkdir()
    h.chamber("one", [accept(derive=False)], repo=False)
    h.chamber("two", [accept(derive=False)], repo=False)
    h.drop("a.sensor.json", {})
    h.settle()
    check("two matches: left in place", (h.box / "a.sensor.json").exists(), True)
    check("…and the user alerted, naming both",
          len(h.alerts) == 1 and "one" in h.alerts[0] and "two" in h.alerts[0], True)
    h.now += 4
    h.watcher.tick()
    check("…once, not every pass", len(h.alerts), 1)

    h2 = Harness(root / "x") if (root / "x").mkdir() is None else None
    h2.box.mkdir()
    h2.letterbox_manifest = h2.box / ".inbox.json"
    h2.letterbox_manifest.write_text(json.dumps(
        {"inboxes": [{"id": "l", "path": "."}], "accepts": [accept(path="evil/", derive=False)]}))
    h2.drop("a.sensor.json", {})
    h2.settle()
    check("accepts in the letterbox's own manifest are ignored",
          (h2.box / "a.sensor.json").exists(), True)


def test_filing_derive_commit(root):
    print("collisions, on_receive, commit debounce")
    h = Harness(root)
    h.box.mkdir()
    c = h.chamber("lab", [accept()])
    base = commits(c)
    h.drop("a.sensor.json", {"n": 1})
    h.settle()
    filed = c / "data/raw/2026/11/01/a.sensor.json"
    check("filed", filed.is_file(), True)
    calls = [json.loads(line) for line in (c / "derived/calls.jsonl").read_text().splitlines()]
    check("on_receive ran with the chamber root as cwd",
          Path(calls[0]["cwd"]).resolve(), c.resolve())
    check("…and the filed path as last argument", Path(calls[0]["argv"][-1]).resolve(),
          filed.resolve())
    check("first filing committed in the same pass", commits(c), base + 1)
    check("…with the derived output, and pushed",
          (git(c, "status", "--porcelain"),
           git(c, "rev-parse", "HEAD") == git(c, "rev-parse", "origin/main")), ("", True))

    h.drop("a.sensor.json", {"n": 1})
    h.settle()
    check("identical resend: letterbox copy dropped, nothing added",
          ((h.box / "a.sensor.json").exists(),
           sorted(p.name for p in filed.parent.iterdir())), (False, ["a.sensor.json"]))
    h.drop("a.sensor.json", {"n": 2})
    h.settle()
    check("different content, same name: kept beside it with a suffix",
          sorted(p.name for p in filed.parent.iterdir()), ["a-2.sensor.json", "a.sensor.json"])
    check("inside the interval: no commit yet", commits(c), base + 1)
    check("…but the files are in the chamber already", (filed.parent / "a-2.sensor.json").is_file(),
          True)
    h.now += 600
    h.watcher.tick()
    check("the interval up: one commit for what waited", commits(c), base + 2)
    check("…everything", git(c, "status", "--porcelain"), "")

    h.drop("f.sensor.json", {"fail_derive": True})
    h.settle()
    check("on_receive failing: the file is filed anyway",
          any(p.name == "f.sensor.json" for p in (c / "data/raw").rglob("*.json")), True)


def test_failure_and_restart(root):
    print("failed commit, restart")
    h = Harness(root)
    h.box.mkdir()
    c = h.chamber("lab", [accept()])
    base = commits(c)
    real = h.mod._run

    def failing(argv, **kw):
        if "commit" in argv:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="index.lock exists")
        return real(argv, **kw)

    h.mod._run = failing
    h.drop("a.sensor.json", {})
    h.settle()
    check("commit failed: nothing committed", commits(c), base)
    pending = json.loads(h.mod.STATE_PATH.read_text())["chambers"][str(c)]["pending"]
    check("…filed and derived paths stay pending",
          sorted(pending), ["data/raw/2026/11/01/a.sensor.json", "derived/calls.jsonl"])
    h.mod._run = real
    h.now += 60
    h.restart()
    h.watcher.tick()
    check("a restarted watcher waits out the interval", commits(c), base)
    h.now += 600
    h.watcher.tick()
    check("…then commits what the old one left", (commits(c), git(c, "status", "--porcelain")),
          (base + 1, ""))


def main():
    for test in (test_basics, test_ambiguous_and_own_manifest, test_filing_derive_commit,
                 test_failure_and_restart):
        with tempfile.TemporaryDirectory() as tmp:
            test(Path(tmp))
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
