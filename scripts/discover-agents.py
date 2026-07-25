#!/usr/bin/env python3
"""Emit a deterministic N-Triples registry of the system's AI agents.

Discovery is the same filesystem walk the entrypoint already does to autodetect
plugins, widened to the whole agent roster (which is broader than the plugin
marketplace — core personas are not plugins). For every agent definition found
in the three canonical locations it emits, into the chambers volume so the life
store indexes it:

    <urn:retinue:actor:NAME> a               kb:AiAgent .
    <urn:retinue:actor:NAME> kb:name         "NAME" .
    <urn:retinue:actor:NAME> kb:description  "..." .   # only when known

The actor URI is derived from the definition's basename, which *is* the
`current_actor` convention documented in CLAUDE.md (`coach.md` ->
`urn:retinue:actor:coach`). No hand-maintained registry: add a persona file or
mount a chamber and its agent is in the store on the next boot; nothing to keep
in sync.

Two properties make this safe to run on every boot:

  * Deterministic output. Triples are sorted and the format is N-Triples (one
    triple per line, no blank nodes, no prefix state), so identical inputs
    always produce a byte-identical file.
  * Write-if-changed. The file is only rewritten when its bytes actually differ,
    so an unchanged roster never touches the filesystem — and qlever-dir never
    rebuilds the store for nothing.

This is intentionally *not* folded into marketplace.json generation: the two
artifacts have different scopes (the marketplace lists chamber plugins; this
lists all agents including non-plugin core personas) and, per the design
discussion, the store must never sit on the boot-critical path — data flows
filesystem -> {marketplace.json, agents.nt}, never filesystem -> store ->
marketplace.
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

KB = "https://w3id.org/retinue/kb#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
ACTOR_PREFIX = "urn:retinue:actor:"

CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR") or "/workspace/chambers")
WORKSPACE = Path(os.environ.get("RETINUE_WORKSPACE") or "/workspace")

# Ara is the main-session persona, defined in CLAUDE.md rather than as an agent
# definition file, so the directory walk cannot find her. She is nonetheless an
# AI agent and the most likely owner of projects (`current_actor: ara`), so she
# is registered unconditionally. Any name here is seeded before the filesystem
# walk, which may still enrich it with a description if a file appears later.
ALWAYS_AGENTS = {
    "ara": "Coordinator of Retinue; the main-session persona. Routes work to "
           "the right agent, maintains the system, and owns cross-cutting "
           "projects not delegated to a subagent.",
}

# Where the registry is written. Under the chambers volume (which QLever mounts
# read-only at /data) but in a framework-owned directory, so it lands in no
# chamber's git repo. Overridable for tests.
OUTPUT = Path(
    os.environ.get("AGENTS_TTL_PATH") or (CHAMBERS_DIR / "_generated" / "agents.nt")
)


def _agent_dirs() -> list[Path]:
    """The three canonical agent locations, same set the entrypoint knows."""
    return [
        WORKSPACE / "agents",              # core personas (no frontmatter)
        WORKSPACE / ".claude" / "agents",  # core subagent (archivist)
        # chamber agents provided as plugins
        *(Path(p) for p in sorted(
            glob.glob(str(CHAMBERS_DIR / "*" / ".retinue" / "agents"))
        )),
    ]


def _frontmatter_description(md: Path) -> str:
    """Return the YAML frontmatter `description:` value, or "" if none.

    Deliberately tiny: core personas carry no frontmatter at all, and the ones
    that do keep description on a single line. We do not pull in a YAML parser
    for one optional field.
    """
    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    for line in text[3:end].splitlines():
        stripped = line.strip()
        if stripped.startswith("description:"):
            return stripped[len("description:"):].strip().strip('"').strip("'")
    return ""


def _nt_string(value: str) -> str:
    """Escape a Python string as an N-Triples literal (RDF 1.1 §7.2)."""
    out = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{out}"'


def discover() -> dict[str, str]:
    """Map agent name -> description (possibly "") for every agent found.

    Keyed by name so the same basename in two locations collapses to one actor;
    a description-bearing definition wins over a bare one.
    """
    agents: dict[str, str] = dict(ALWAYS_AGENTS)
    for d in _agent_dirs():
        if not d.is_dir():
            continue
        for md in sorted(d.glob("*.md")):
            name = md.stem
            desc = _frontmatter_description(md)
            if name not in agents or (desc and not agents[name]):
                agents[name] = desc
    return agents


def render(agents: dict[str, str]) -> str:
    """Render the registry as sorted, deterministic N-Triples."""
    lines: list[str] = []
    for name in sorted(agents):
        subj = f"<{ACTOR_PREFIX}{name}>"
        lines.append(f"{subj} <{RDF_TYPE}> <{KB}AiAgent> .")
        lines.append(f"{subj} <{KB}name> {_nt_string(name)} .")
        desc = agents[name]
        if desc:
            lines.append(f"{subj} <{KB}description> {_nt_string(desc)} .")
    lines.sort()
    return "".join(line + "\n" for line in lines)


def write_if_changed(content: str, path: Path) -> bool:
    """Write only when bytes differ. Returns True if the file was (re)written."""
    data = content.encode("utf-8")
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass  # missing/unreadable -> write
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return True


def main() -> int:
    agents = discover()
    content = render(agents)
    changed = write_if_changed(content, OUTPUT)
    verb = "wrote" if changed else "unchanged"
    print(
        f"[discover-agents] {verb} {OUTPUT} ({len(agents)} agent(s))",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
