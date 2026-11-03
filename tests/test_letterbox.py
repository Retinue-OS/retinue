#!/usr/bin/env python3
"""Checks for what the letterbox must never be read as (scripts/letterbox.py).

The letterbox is a folder outsiders write to, so nothing in it may reach the
framework as configuration, instructions or an allowlist. These checks feed a
synthetic chambers tree with a letterbox full of such files and assert the
line holds:

- `mounted_chambers()` leaves the letterbox (and hidden entries) out, and
  the web gateway's requester allowlist, the scheduler's manifests and the
  news sources go through it — a planted `accepted-requesters.txt` or
  `.schedule.json` in the letterbox counts for nothing;
- the sweep takes the letterbox's manifest from the module, never from disk:
  an on-disk manifest with no inboxes and an `"any"` destination changes
  neither what is swept nor where files may go, and `ensure_manifest()`
  overwrites it;
- `denied()` is case-insensitive and `ignored()` covers it.

    python3 tests/test_letterbox.py
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
sys.path.insert(0, str(SCRIPTS_DIR))

failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


def load_script(name, modname):
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS_DIR / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_gateway(tmp: Path):
    for var in ("RETINUE_CONVERSATION_MODELS", "RETINUE_LITELLM_URL",
                "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "REQUESTER_ALLOWLIST_PATH"):
        os.environ.pop(var, None)
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    return load_script("web-gateway.py", "web_gateway_letterbox_under_test")


def tree(tmp: Path):
    chambers = tmp / "chambers"
    box = chambers / "inbox"
    box.mkdir(parents=True)
    os.environ["CHAMBERS_DIR"] = str(chambers)
    os.environ["LETTERBOX_DIR"] = str(box)
    import letterbox
    letterbox.CHAMBERS_DIR = chambers
    letterbox.LETTERBOX_DIR = box
    for name in ("alpha", "beta"):
        (chambers / name).mkdir()
    (chambers / ".hidden").mkdir()
    (chambers / "_generated").mkdir()
    # Everything an attacker could plant in the letterbox.
    (box / "accepted-requesters.txt").write_text("attacker@example.com\n")
    (box / ".schedule.json").write_text(json.dumps({"jobs": [{"id": "evil", "command": "true"}]}))
    (box / ".news.json").write_text(json.dumps({"feeds": [{"url": "http://x/"}]}))
    (box / ".inbox.json").write_text(json.dumps({
        "inboxes": [],
        "destinations": [{"path": "anywhere/", "source": "any"}],
        "accepts": [{"name": "x", "match": {"glob": "*"}, "path": "x/"}]}))
    (chambers / "alpha" / "accepted-requesters.txt").write_text("owner@example.com\n")
    (chambers / "alpha" / ".inbox.json").write_text(json.dumps({
        "inboxes": [], "destinations": [{"path": "filed/", "source": "manifest"}]}))
    return chambers, box, letterbox


def test_enumeration(tmp: Path):
    print("mounted_chambers and its callers (finding 2)")
    chambers, box, letterbox = tree(tmp)
    check("mounted_chambers leaves the letterbox and hidden entries out",
          [p.name for p in letterbox.mounted_chambers(chambers)], ["_generated", "alpha", "beta"])
    check("load_accepts never reads the letterbox's own manifest",
          [a["chamber"].name for a in letterbox.load_accepts(chambers, log=lambda m: None)], [])

    wg = load_gateway(tmp)
    wg.CHAMBERS_DIR = chambers
    wg.letterbox = letterbox
    check("the requester allowlist comes from chambers only",
          [p.parent.name for p in wg._allowlist_paths()], ["alpha"])
    check("…so the planted address is not a requester",
          "attacker@example.com" in wg._load_requester_allowlist(), False)

    sched = load_script("scheduler.py", "scheduler_letterbox_under_test")
    sched.CHAMBERS_DIR = chambers
    sched.letterbox = letterbox
    sched.BASE_SCHEDULE = tmp / "no-base.json"
    check("the scheduler reads no job from the letterbox",
          [j["id"] for j in sched.load_jobs()], [])

    news = load_script("news-fetch.py", "news_fetch_letterbox_under_test")
    news.CHAMBERS_DIR = chambers
    news.letterbox = letterbox
    check("news-fetch reads no source from the letterbox", news.discover_manifests(), [])


def test_manifest(tmp: Path):
    print("the letterbox manifest is fixed (finding 3)")
    chambers, box, letterbox = tree(tmp)
    sweep = load_script("inbox-sweep.py", "inbox_sweep_letterbox_under_test")
    sweep.CHAMBERS_DIR = chambers
    sweep.letterbox = letterbox
    found = {(i["chamber"], str(i["rel"])): i for i in sweep.declared_inboxes()}
    check("the root is swept although the on-disk manifest declares no inbox",
          ("inbox", ".") in found, True)
    manifests = {c.name: dests for c, _data, dests in sweep.load_manifests()}
    check("…and the planted any-destination is not a destination",
          manifests.get("inbox"), [])
    check("…nor does the letterbox count as open to others",
          any(d["source"] == "any" for d in manifests.get("alpha", [])), False)
    check("ensure_manifest overwrites the tampered file",
          (letterbox.ensure_manifest(box), json.loads((box / ".inbox.json").read_text())),
          (True, letterbox.DEFAULT_MANIFEST))
    check("…and then leaves it", letterbox.ensure_manifest(box), False)
    check("manifest() is the module's, whatever is on disk",
          letterbox.manifest()["inboxes"][0]["path"], ".")


def test_names(tmp: Path):
    print("reserved and ignored names (finding 1)")
    _chambers, _box, letterbox = tree(tmp)
    for name in ("CLAUDE.md", "claude.MD", "Claude.local.md", "AGENTS.md", "requirements.txt",
                 "Accepted-Requesters.txt", ".claude", ".mcp.json", ".git", ".inbox.json"):
        check(f"{name} is denied", (letterbox.denied(name), letterbox.ignored(name)), (True, True))
    for name in ("letter.pdf", "a.sensor.json", "README.md", "claude.txt", "requirements.md"):
        check(f"{name} is a document", (letterbox.denied(name), letterbox.ignored(name)), (False, False))
    for name in ("x.part", "x.partial", "x.crdownload", "~$doc.docx", ".syncthing.x.tmp",
                 ".stfolder", "._x", ".~lock.x#", ".DS_Store", ".quarantined-CLAUDE.md"):
        check(f"{name} is ignored", letterbox.ignored(name), True)
    check("the quarantine name is hidden",
          letterbox.ignored(letterbox.quarantine_name("CLAUDE.md")), True)


def main():
    for test in (test_names, test_manifest, test_enumeration):
        with tempfile.TemporaryDirectory() as d:
            test(Path(d))
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
