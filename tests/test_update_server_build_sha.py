#!/usr/bin/env python3
"""The built-in update recipe stamps the build it is about to make.

`/health` can only name the commit an image was built from if the build was
told; the updater is the one place that knows, because it owns the pull. So
the built-in recipe reads `HEAD` **between** the pull and the build and hands
it to `docker compose build` as `RETINUE_BUILD_SHA`.

The ordering is the whole point, and it is what this pins: read before the
pull, the stamp would name the *previous* commit — an image labelled with code
it does not contain, which is worse than no label at all. A missing or
unreadable sha must also never fail an update: the images still carry their
framework digest (scripts/build_stamp.py), which is what the stale-gateway
check on /gateways actually compares.

Runs `_run_update` directly with a fake `subprocess.run`, so no git or docker
is involved — what is under test is which environment each step is handed.

    python3 tests/test_update_server_build_sha.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SHA = "b5ff537e3237b00dad9cc2cd993f52436a6e7b6c"
PREVIOUS = "e51e6d015eda68e493d98f06ee9490154bf77285"


class _Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _load(tmp: Path, update_command: str | None):
    os.environ["UPDATER_TOKEN"] = "test-token"
    os.environ["PROJECT_DIR"] = str(tmp)
    os.environ["UPDATE_LOG_PATH"] = str(tmp / "update.log")
    os.environ["UPDATE_TIMEOUT"] = "30"
    os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("RETINUE_BUILD_SHA", None)
    if update_command is None:
        os.environ.pop("UPDATE_COMMAND", None)
    else:
        os.environ["UPDATE_COMMAND"] = update_command
    spec = importlib.util.spec_from_file_location(
        "update_server_build_sha_under_test", REPO_ROOT / "updater" / "update-server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _record(mod, head_sha: str | None):
    """Replace subprocess.run: record each step's command and RETINUE_BUILD_SHA.

    `git rev-parse HEAD` answers `head_sha`, or fails when it is None — which
    is what an unreadable checkout looks like.
    """
    steps = []

    def fake_run(cmd, **kwargs):
        shown = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        if "rev-parse" in shown:
            if head_sha is None:
                return _Result(returncode=128, stdout="")
            return _Result(stdout=head_sha + "\n")
        steps.append((shown, (kwargs.get("env") or {}).get("RETINUE_BUILD_SHA")))
        return _Result()

    mod.subprocess.run = fake_run
    return steps


def test_the_build_step_is_stamped_and_the_pull_is_not():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        mod = _load(tmp, None)          # the framework's built-in recipe
        steps = _record(mod, SHA)
        mod._run_update(run_id=1)

        shown = [s for s, _ in steps]
        assert any("pull" in s for s in shown) and any("build" in s for s in shown), shown
        by_step = dict(steps)
        pull = next(s for s in shown if "pull" in s)
        build = next(s for s in shown if "build" in s)
        up = next(s for s in shown if s.endswith("up -d"))

        assert by_step[pull] is None, \
            "the pull must not be stamped — before it, HEAD is the previous commit"
        assert by_step[build] == SHA, f"the build carries the sha it is building: {by_step}"
        assert by_step[up] == SHA, "and it stays set for the rest of the recipe"

        log = (tmp / "update.log").read_text(encoding="utf-8")
        assert f"building {SHA}" in log, log
    print("PASS test_the_build_step_is_stamped_and_the_pull_is_not")


def test_an_unreadable_head_leaves_the_build_unstamped_and_still_runs():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        mod = _load(tmp, None)
        steps = _record(mod, None)      # `git rev-parse` fails
        mod._run_update(run_id=1)

        assert len(steps) == 3, f"every step still ran: {steps}"
        assert all(sha is None for _, sha in steps), steps
        log = (tmp / "update.log").read_text(encoding="utf-8")
        assert "building unstamped" in log, log
        # The run's own verdict — `_state` is only written back for a run
        # dispatched through do_POST, so the log is the record here.
        assert "=== update finished (exit 0) ===" in log, \
            "a missing sha is not an update failure"
    print("PASS test_an_unreadable_head_leaves_the_build_unstamped_and_still_runs")


def test_an_operator_recipe_is_left_alone():
    """Only the recipe knows where its pull ends, so the updater does not guess.

    A deployment with its own UPDATE_COMMAND stamps its own build (documented
    in docs/contributing.md); stamping it from here would mean reading HEAD
    before the recipe has pulled, i.e. labelling the image with the commit it
    is replacing.
    """
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        mod = _load(tmp, "true")
        steps = _record(mod, SHA)
        mod._run_update(run_id=1)
        assert steps == [("true", None)], steps
    print("PASS test_an_operator_recipe_is_left_alone")


def main():
    test_the_build_step_is_stamped_and_the_pull_is_not()
    test_an_unreadable_head_leaves_the_build_unstamped_and_still_runs()
    test_an_operator_recipe_is_left_alone()
    print("all updater build-sha tests passed")


if __name__ == "__main__":
    sys.exit(main())
