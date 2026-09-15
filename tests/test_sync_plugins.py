#!/usr/bin/env python3
"""Checks that the plugin sync (scripts/sync-plugins.py) starts `claude` only
when a plugin actually drifted.

The watch loop runs every PLUGIN_SYNC_INTERVAL seconds (60 by default), for as
long as the container lives. Every `claude` invocation reads the shared
credential file and refreshes an access token near expiry, so a pass that runs
the CLI unconditionally turns the loop into 1440 chances a day to perform the
one OAuth rotation — outside the discipline every other framework spawner
follows, and, on 2026-09-14, the process that performed the fatal one
(docs/claude-auth.md). Drift detection is plain file I/O and must therefore
come first; the refresh happens once, before the first CLI call of a pass that
has real work.

    python3 tests/test_sync_plugins.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_sync(tmp: Path):
    # Sandbox the credential file: claude_auth reads it at import, and an
    # inherited value would point the test at the real sign-in.
    os.environ["CLAUDE_CRED_FILE"] = str(tmp / "claude" / ".credentials.json")
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "sync_plugins_under_test", SCRIPTS_DIR / "sync-plugins.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plugin_tree(root: Path, name: str, body: str) -> Path:
    source = root / "chambers" / name / ".retinue"
    (source / "agents").mkdir(parents=True, exist_ok=True)
    (source / "agents" / f"{name}.md").write_text(body, encoding="utf-8")
    return source


def _workspace(tmp: Path) -> tuple[Path, Path]:
    """A marketplace with one plugin, installed and byte-identical to source."""
    root = tmp / "workspace"
    source = _plugin_tree(root, "ari", "the agent\n")
    marketplace = root / ".claude-plugin" / "marketplace.json"
    marketplace.parent.mkdir(parents=True, exist_ok=True)
    marketplace.write_text(json.dumps(
        {"plugins": [{"name": "ari", "source": "chambers/ari/.retinue"}]}),
        encoding="utf-8")

    cached = tmp / "cache" / "ari" / "0.0.0"
    cached.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, cached)

    installed = tmp / "installed_plugins.json"
    installed.write_text(json.dumps({"plugins": {"ari@retinue": [
        {"scope": "user", "installPath": str(cached)}]}}), encoding="utf-8")
    return marketplace, installed


def _install_fakes(mod, marketplace: Path, installed: Path):
    """Record every CLI command and credential refresh, in order."""
    events = []
    mod.MARKETPLACE = marketplace
    mod.INSTALLED = installed

    def fake_run(cmd):
        events.append(("cli", " ".join(cmd[1:])))
        return True

    def fake_ensure_fresh(**kwargs):
        events.append(("refresh",))
        assert callable(kwargs.get("log")), kwargs
        return {"action": "fresh"}

    mod.run = fake_run
    mod.claude_auth.ensure_fresh_credentials = fake_ensure_fresh
    return events


def test_clean_pass_starts_no_claude(mod, marketplace, installed):
    events = _install_fakes(mod, marketplace, installed)
    assert mod.sync() == 0
    assert events == [], f"a pass with no drift touched the CLI: {events}"


def test_drift_refreshes_once_then_reinstalls(mod, marketplace, installed, source):
    events = _install_fakes(mod, marketplace, installed)
    (source / "agents" / "ari.md").write_text("the agent, edited\n", encoding="utf-8")
    assert mod.sync() == 1
    assert events == [
        ("refresh",),
        ("cli", "plugin marketplace update retinue"),
        ("cli", "plugin uninstall ari@retinue"),
        ("cli", "plugin install ari@retinue"),
    ], events


def test_force_refreshes_before_the_cli_too(mod, marketplace, installed):
    events = _install_fakes(mod, marketplace, installed)
    assert mod.sync(force=True) == 1
    assert events[0] == ("refresh",), events
    assert ("cli", "plugin install ari@retinue") in events, events


def test_missing_source_is_not_drift(mod, marketplace, installed, source):
    events = _install_fakes(mod, marketplace, installed)
    shutil.rmtree(source)
    assert mod.sync() == 0
    assert events == [], f"a missing source still started the CLI: {events}"


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        marketplace, installed = _workspace(tmp)
        source = marketplace.parent.parent / "chambers" / "ari" / ".retinue"
        mod = _load_sync(tmp)
        test_clean_pass_starts_no_claude(mod, marketplace, installed)
        test_force_refreshes_before_the_cli_too(mod, marketplace, installed)
        test_drift_refreshes_once_then_reinstalls(mod, marketplace, installed, source)
        test_missing_source_is_not_drift(mod, marketplace, installed, source)
    print("all plugin-sync tests passed")


if __name__ == "__main__":
    main()
