#!/usr/bin/env python3
"""Checks for news-curate.py: the session gets the news directory, and a run
that exits cleanly but scores nothing counts as a failure.

    python3 tests/test_news_curate.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def load_curate(tmp: Path):
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "news_curate_under_test", SCRIPTS_DIR / "news-curate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.store.NEWS_DIR = tmp
    tmp.mkdir(parents=True, exist_ok=True)
    mod.claude_auth.ensure_fresh_credentials = lambda log=None: None
    mod.session_env.build = lambda model=None: {}
    return mod


def seed(mod, n: int) -> None:
    items = [{"id": f"i{k}", "title": f"t{k}", "source": "s",
              "url": f"https://example.org/{k}",
              "published": "2026-11-25T00:00:00Z"}
             for k in range(n)]
    mod.store.save_items(items)


def run(mod, score: bool, tap: bool = False):
    """One curation run with the session faked: ``score`` has the "Herald"
    score an item; ``tap`` has the user's up/down tap set an importance on
    the first item while the run is under way, the Herald scoring nothing."""
    calls = []

    def fake_run(cmd, cwd=None, env=None):
        calls.append(cmd)
        if score or tap:
            items = mod.store.load_items()
            items[0]["importance"] = 3.0
            if score:  # news-score.py stamps what it scores; a tap does not
                items[0]["scored_at"] = mod.store.iso(mod.store.now())
            mod.store.save_items(items)
        return SimpleNamespace(returncode=0)

    mod.subprocess.run = fake_run
    sys.argv = ["news-curate.py"]
    return mod.main(), calls


def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "news"

        mod = load_curate(tmp)
        seed(mod, 3)
        rc, calls = run(mod, score=False)
        cmd = calls[0]
        i = cmd.index("--add-dir")
        if cmd[i + 1] != str(tmp):
            print(f"FAIL: --add-dir is {cmd[i + 1]!r}, want {str(tmp)!r}")
            failures += 1
        if rc == 0:
            print("FAIL: a clean exit that scored nothing returned 0")
            failures += 1
        # The prompt must follow "--": --add-dir takes several directories
        # and would swallow it, and the CLI would then exit with no input.
        if cmd[-2] != "--" or not cmd[-1].strip() or "--" in cmd[:-2]:
            print(f"FAIL: the prompt does not follow a single '--': {cmd[-3:]!r}")
            failures += 1
        if cmd.index("--") < i:
            print("FAIL: --add-dir after the '--'")
            failures += 1

        # A tap of the user's during the run is no scoring by the Herald.
        seed(mod, 3)
        rc, _ = run(mod, score=False, tap=True)
        if rc == 0:
            print("FAIL: a run whose only importance came from a tap returned 0")
            failures += 1

        seed(mod, 3)

        rc, _ = run(mod, score=True)
        if rc != 0:
            print(f"FAIL: a run that scored an item returned {rc}")
            failures += 1

    print("ok" if not failures else f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
