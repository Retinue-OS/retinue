#!/usr/bin/env python3
"""The service worker's shell list (webapp/sw.js, SHELL_ASSETS) is complete.

The dashboard opens offline from that list: shell files are served cache-first
from what the worker stored at install, and a file not on the list is fetched
from the network every time. So a module that a listed page or module imports
and that is not on the list breaks every page importing it the moment the
network is gone — the whole module graph fails with it. These checks walk the
static imports of every listed page and module (a page's `<script src>` and
stylesheet, a module's `import … from '…'`) and require each target on the
list too; and every page or module named there must exist.

Standalone like the rest of the suite; no dependencies.
"""
import os
import posixpath
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEBAPP = os.path.normpath(os.path.join(HERE, "..", "webapp"))

# Paths the gateway serves from another file than the path says.
ALIASES = {"/": "/index.html", "/sync-health": "/sync-health.html"}

STATIC_IMPORT = re.compile(r"""^\s*(?:import|export)\s[^'"]*?\bfrom\s*['"]([^'"]+)['"]|^\s*import\s*['"]([^'"]+)['"]""", re.M)
SCRIPT_SRC = re.compile(r"""<script\b[^>]*\bsrc=["']([^"']+)["']""", re.I)
STYLESHEET = re.compile(r"""<link\b[^>]*\brel=["']stylesheet["'][^>]*\bhref=["']([^"']+)["']|<link\b[^>]*\bhref=["']([^"']+)["'][^>]*\brel=["']stylesheet["']""", re.I)
INLINE_MODULE = re.compile(r"""<script\b[^>]*type=["']module["'][^>]*>(.*?)</script>""", re.I | re.S)


def shell_assets() -> list[str]:
    with open(os.path.join(WEBAPP, "sw.js"), encoding="utf-8") as f:
        source = f.read()
    block = re.search(r"const SHELL_ASSETS = \[(.*?)\];", source, re.S)
    assert block, "SHELL_ASSETS not found in sw.js"
    return re.findall(r"'([^']+)'", block.group(1))


def file_for(path: str) -> str:
    return os.path.join(WEBAPP, ALIASES.get(path, path).lstrip("/"))


def resolve(base: str, ref: str) -> str | None:
    """The site path `ref` names from the page or module at `base`; None for another origin."""
    if re.match(r"^[a-z][a-z0-9+.-]*:", ref, re.I) or ref.startswith("//"):
        return None
    ref = ref.split("#", 1)[0].split("?", 1)[0]
    if ref.startswith("/"):
        return posixpath.normpath(ref)
    directory = posixpath.dirname(ALIASES.get(base, base))
    return posixpath.normpath(posixpath.join(directory, ref))


def references(path: str, text: str) -> set[str]:
    refs = set()
    if path.endswith(".js") or path.endswith(".mjs"):
        for a, b in STATIC_IMPORT.findall(text):
            refs.add(a or b)
    else:
        refs.update(SCRIPT_SRC.findall(text))
        for a, b in STYLESHEET.findall(text):
            refs.add(a or b)
        for body in INLINE_MODULE.findall(text):
            for a, b in STATIC_IMPORT.findall(body):
                refs.add(a or b)
    resolved = {resolve(path, r) for r in refs}
    return {r for r in resolved if r}


def main() -> int:
    assets = shell_assets()
    listed = set(assets)
    failures = []

    missing_files = [a for a in assets if not os.path.isfile(file_for(a))]
    if missing_files:
        failures.append(f"listed but not in webapp/: {missing_files}")
    else:
        print(f"ok: all {len(assets)} listed files exist")

    unlisted = {}
    for asset in assets:
        if not (asset.endswith((".html", ".js", ".mjs")) or asset in ALIASES):
            continue
        with open(file_for(asset), encoding="utf-8") as f:
            text = f.read()
        for target in sorted(references(asset, text)):
            # The vendored libraries are cached on first use (sw.js, /vendor/).
            if target.startswith("/vendor/"):
                continue
            if target not in listed:
                unlisted.setdefault(target, []).append(asset)
    if unlisted:
        for target, users in sorted(unlisted.items()):
            failures.append(f"{target} is imported by {', '.join(users)} but not in SHELL_ASSETS")
    else:
        print("ok: every page's and module's static imports are in the shell list")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("All service worker shell checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
