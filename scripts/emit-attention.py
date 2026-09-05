#!/usr/bin/env python3
"""Emit the attention properties of the open threads and chats into the life
store at boot (chambers/_generated/attention/items.nt).

The web-gateway keeps this file current on its own tick once it runs; this
script covers the moments before it does — the container just started, the
gateway not yet listening — so the store never boots without the properties
of what is already waiting. Same discipline as discover-agents.py: sorted
N-Triples, no blank nodes, write-if-changed (an unchanged file triggers no
qlever-dir rebuild). Projects are not emitted here: their properties come
from chamber frontmatter through the chambers' own converters.

Reads what the gateway reads — CONVERSATIONS_DIR and CHAT_STATE_DIR — and
needs no store, no network and no token. Non-fatal by design: the entrypoint
runs it with `|| echo warning`.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import attention as policy  # noqa: E402
import attention_store  # noqa: E402

CONVERSATIONS_DIR = Path(os.environ.get("CONVERSATIONS_DIR", "/tmp/web-tab-conversations"))
CHAT_STATE_DIR = Path(os.environ.get("CHAT_STATE_DIR", "/tmp/web-chat-state"))
ATTENTION_DIR = Path(os.environ.get("ATTENTION_DIR", str(CONVERSATIONS_DIR.parent / "attention")))
CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR", "/workspace/chambers"))
OUTPUT = Path(os.environ.get("ATTENTION_EMIT_PATH",
                             str(attention_store.default_emit_path(CHAMBERS_DIR))))
_CONV_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _docs(directory: Path) -> list[dict]:
    out = []
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def items() -> list[dict]:
    profile = attention_store.AttentionStore(ATTENTION_DIR).profile()
    found = []
    for conv in _docs(CONVERSATIONS_DIR):
        if not _CONV_ID_RE.match(str(conv.get("id", ""))):
            continue
        if attention_store.thread_wants_attention(conv):
            found.append(attention_store.thread_item(conv, profile))
    for doc in _docs(CHAT_STATE_DIR):
        block = doc.get("attention") or {}
        if not doc.get("id") or not block or block.get("state", "open") != "open" or doc.get("archived"):
            continue
        parts = doc["id"].split(":", 1)
        row = {"id": doc["id"], "name": doc.get("name") or (parts[1] if len(parts) > 1 else doc["id"]),
               "channel": parts[0], "unread": 0, "last": {}, "archived": False,
               "muted": bool(doc.get("muted")), "group": doc.get("group")}
        found.append(attention_store.chat_item(row, doc, profile))
    return found


def main() -> int:
    changed = attention_store.emit(items(), OUTPUT)
    print(f"[emit-attention] {'wrote' if changed else 'unchanged'} {OUTPUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
