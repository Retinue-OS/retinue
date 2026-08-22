#!/usr/bin/env python3
"""Emit the offered conversation-model list into the life store at boot.

The dashboard's per-conversation model picker reads its options from a
hand-editable JSON-LD source, `config/conversation-models.jsonld` (the web
gateway loads it as plain JSON — no RDF dependency on the serving path). This
script derives the same list into N-Triples so the life store indexes it too,
*without* any deployment having to declare a `jsonld` converter or copy the file
into a chamber.

It is the exact sibling of `discover-agents.py`: a framework-owned boot emitter
that writes into `chambers/_generated/`. That directory lives under the chambers
volume (which QLever mounts read-only at /data) but in no chamber's git repo, so
the triples are indexed while the derived artifact never lands in a data repo.
The hand-editable source stays in `config/`; the generated `.nt` is disposable
output — never edit it, it is overwritten on the next boot.

Two properties keep it safe to run every boot (same as discover-agents.py):

  * Deterministic output. Triples are sorted, blank-node-free N-Triples, so
    identical inputs always produce a byte-identical file. Model nodes get
    stable IRIs derived from their id (no rdflib blank-node churn).
  * Write-if-changed. The file is rewritten only when its bytes differ, so an
    unchanged model list never touches the filesystem — and qlever-dir never
    rebuilds the store for nothing.

Source resolution mirrors the gateway's own loader so the store reflects what is
actually served: an inline `RETINUE_CONVERSATION_MODELS` JSON array wins over the
file (path overridable via `RETINUE_CONVERSATION_MODELS_FILE`); an empty/invalid
source falls back to the built-in default.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

# Same vocabulary as config/conversation-models.jsonld's @context, so the
# emitted triples carry the source document's intended semantics.
RN = "https://retinue-os.github.io/ns/conversation#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

LIST_IRI = f"{RN}conversationModelList"

CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR") or "/workspace/chambers")
WORKSPACE = Path(os.environ.get("RETINUE_WORKSPACE") or "/workspace")

_DEFAULT_CONVERSATION_MODELS = [
    {"id": "", "label": "Default"},
    {"id": "opus", "label": "Opus (deepest reasoning)"},
    {"id": "sonnet", "label": "Sonnet (balanced)"},
    {"id": "haiku", "label": "Haiku (fastest)"},
]

# Where the registry is written: under the chambers volume but in a
# framework-owned directory, so it lands in no chamber's git repo. Overridable
# for tests.
OUTPUT = Path(
    os.environ.get("CONVERSATION_MODELS_NT_PATH")
    or (CHAMBERS_DIR / "_generated" / "conversation-models.nt")
)


def _source_file() -> str:
    return os.environ.get(
        "RETINUE_CONVERSATION_MODELS_FILE",
        str(WORKSPACE / "config" / "conversation-models.jsonld"),
    )


def _coerce(parsed: object) -> list[dict]:
    """Normalise a parsed models value into validated {"id","label"} dicts.

    Accepts the bare array or a JSON-LD document wrapping it under `models` —
    the same shapes the gateway's loader accepts. Returns [] when nothing usable
    is present so the caller can fall back."""
    if isinstance(parsed, dict):
        parsed = parsed.get("models", [])
    if not isinstance(parsed, list):
        return []
    models: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict) or "id" not in item:
            continue
        mid = str(item["id"]).strip()
        label = str(item.get("label") or mid or "Default").strip()
        models.append({"id": mid, "label": label})
    return models


def load_models() -> list[dict]:
    """Resolve the offered model list, mirroring the gateway's own precedence."""
    raw = os.environ.get("RETINUE_CONVERSATION_MODELS", "").strip()
    if raw:
        try:
            models = _coerce(json.loads(raw))
            if models:
                return models
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    path = _source_file()
    try:
        with open(path, encoding="utf-8") as fh:
            models = _coerce(json.load(fh))
        if models:
            return models
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError, ValueError):
        print(f"[emit-conversation-models] invalid {path}; using default list",
              file=sys.stderr)
    return list(_DEFAULT_CONVERSATION_MODELS)


def _slug(model_id: str) -> str:
    """Stable, injective IRI local part for a model.

    Percent-encodes every character outside the IRI-safe set, so distinct ids
    always produce distinct slugs. Earlier this defaulted the empty id to the
    literal string "default" before sanitising, which let two *different* ids
    (`""`, the gateway-default option, and an explicit `"default"`) collapse
    onto the same node, and let `/` and `:` both fold onto `_`, colliding e.g.
    `anthropic/claude-opus-4` with `anthropic:claude-opus-4` — a hand-written
    id never gets a node of its own by accident. The empty id now yields a
    slug-less node (`#model-`), a legal though minimal IRI."""
    return quote(model_id, safe="")


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


def render(models: list[dict]) -> str:
    """Render the model list as sorted, deterministic N-Triples."""
    lines: list[str] = [
        f"<{LIST_IRI}> <{RDF_TYPE}> <{RN}ConversationModelList> ."
    ]
    for m in models:
        subj = f"<{RN}model-{_slug(m['id'])}>"
        lines.append(f"<{LIST_IRI}> <{RN}offersModel> {subj} .")
        lines.append(f"{subj} <{RDF_TYPE}> <{RN}ConversationModel> .")
        lines.append(f"{subj} <{RN}modelId> {_nt_string(m['id'])} .")
        lines.append(f"{subj} <{RDFS}label> {_nt_string(m['label'])} .")
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
    models = load_models()
    changed = write_if_changed(render(models), OUTPUT)
    verb = "wrote" if changed else "unchanged"
    print(
        f"[emit-conversation-models] {verb} {OUTPUT} ({len(models)} model(s))",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
