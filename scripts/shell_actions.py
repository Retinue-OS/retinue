"""Shell actions: what a dashboard reply asks the user's Android shell to do.

A shell action rides on one of Ara's thread replies, the way a file does
(conversation-push.py --reply-attach), and is carried out by the Ara Android
app (ara-android) when the reply reaches it. The only kind there is so far is
the **keyboard**: the app is paired with another device (a VR headset, a TV, a
computer) as a Bluetooth keyboard, and types what the action carries.

    keyboard-type    {"kind", "id", "text", "enter", "sensitive"}
                     type `text`, then Enter if asked
    keyboard-key     {"kind", "id", "key"}             press one named key
    keyboard-secret  {"kind", "id", "label", "enter"}  ask the user for a secret

Two ways to type a password, PIN or one-time code:

- `keyboard-type` with `sensitive: true` — a secret the user dictated or Ara
  fetched (an SMS code). The text reaches the phone, but the dashboard only
  ever shows it masked, transcripts replayed to later sessions never carry
  it, and the gateway deletes it from the stored message as soon as the page
  reports it typed, and at the latest SENSITIVE_TTL_SECONDS after the reply
  (`purge_sensitive`).
- `keyboard-secret` — the convenient path for a password the user keeps in a
  password manager: it carries only a label ("Meta account"); the app opens a
  native field that Android autofill fills, types it and forgets it. The page
  only learns that it was typed or cancelled.

This module is shared by scripts/shell-type.py (which writes actions into the
turn's manifest) and scripts/web-gateway.py (which reads the manifest at turn
end and stores what validates on the reply). Both validate with
`normalize()`, so what the CLI accepts is exactly what the gateway keeps.
No I/O at import time; standard library only.
"""
from __future__ import annotations

import json
import re
import uuid

MANIFEST_VAR = "RETINUE_REPLY_SHELL_ACTIONS_FILE"

KINDS = ("keyboard-type", "keyboard-key", "keyboard-secret")
KEYS = ("enter", "tab", "backspace", "escape", "up", "down", "left", "right",
        "home", "end", "delete", "space")

# The longest text one action types. Typing runs at a pace the receiving
# device keeps up with (a few ms per key), so this is already ~half a minute.
MAX_TEXT = 4000
# A sensitive text is kept no longer than this after its reply was written —
# the same window in which the page runs a typing action by itself.
SENSITIVE_TTL_SECONDS = 120
# What the dashboard and replays say instead of a purged sensitive text.
PURGED = None
MAX_LABEL = 80
# How many actions one reply may carry; the rest of a runaway manifest is
# dropped.
MAX_ACTIONS = 10
# A manifest bigger than this is not one.
MAX_MANIFEST_BYTES = 64 * 1024

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Control characters other than newline and tab cannot be typed on a
# keyboard; refusing them keeps escape sequences out of the target device.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class InvalidAction(ValueError):
    """An action that does not validate; the message says why."""


def new_id() -> str:
    return uuid.uuid4().hex


def normalize(raw: object) -> dict:
    """Validate one action and return its canonical form, or raise InvalidAction."""
    if not isinstance(raw, dict):
        raise InvalidAction("an action is a JSON object")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise InvalidAction(f"unknown kind {kind!r}")
    aid = raw.get("id")
    if not isinstance(aid, str) or not _ID_RE.match(aid):
        raise InvalidAction("id must be 32 lowercase hex characters")
    out: dict = {"kind": kind, "id": aid}
    if kind == "keyboard-type":
        text = raw.get("text")
        if not isinstance(text, str) or not text:
            raise InvalidAction("keyboard-type needs a non-empty text")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if len(text) > MAX_TEXT:
            raise InvalidAction(f"text is longer than {MAX_TEXT} characters")
        if _CONTROL_RE.search(text):
            raise InvalidAction("text contains control characters")
        out["text"] = text
        out["enter"] = bool(raw.get("enter"))
        if raw.get("sensitive"):
            out["sensitive"] = True
    elif kind == "keyboard-key":
        key = raw.get("key")
        if key not in KEYS:
            raise InvalidAction(f"unknown key {key!r}; one of {', '.join(KEYS)}")
        out["key"] = key
    else:  # keyboard-secret
        label = raw.get("label")
        if not isinstance(label, str) or not label.strip():
            raise InvalidAction("keyboard-secret needs a label")
        label = " ".join(label.split())
        if len(label) > MAX_LABEL:
            raise InvalidAction(f"label is longer than {MAX_LABEL} characters")
        out["label"] = label
        out["enter"] = bool(raw.get("enter"))
    return out


def parse_manifest(raw: str) -> tuple[list[dict], list[str]]:
    """The valid actions of a manifest (one JSON object per line), and the
    reasons the others were dropped. Duplicate ids keep the first."""
    actions: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    for n, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if len(actions) >= MAX_ACTIONS:
            problems.append(f"more than {MAX_ACTIONS} actions — the rest ignored")
            break
        try:
            action = normalize(json.loads(line))
        except (json.JSONDecodeError, InvalidAction) as exc:
            problems.append(f"line {n}: {exc}")
            continue
        if action["id"] in seen:
            continue
        seen.add(action["id"])
        actions.append(action)
    return actions, problems


def purge_sensitive(message: dict, now: float, *, ids: "set[str] | None" = None) -> bool:
    """Delete the text of the message's sensitive actions; True if any went.

    Without `ids`, the ones older than SENSITIVE_TTL_SECONDS (by the message's
    `ts`; an unreadable `ts` counts as expired); with `ids`, exactly those.
    The action keeps its kind, id, enter and sensitive marks, and gets
    `"purged": true` and `"text": null` — so the page can say "Text deleted"
    and nothing can type it again."""
    actions = message.get("shell_actions") if isinstance(message, dict) else None
    if not isinstance(actions, list):
        return False
    expired = None
    changed = False
    for a in actions:
        if not (isinstance(a, dict) and a.get("kind") == "keyboard-type"
                and a.get("sensitive") and a.get("text") is not None):
            continue
        if ids is not None:
            if a.get("id") not in ids:
                continue
        else:
            if expired is None:
                expired = _age_seconds(message.get("ts"), now) >= SENSITIVE_TTL_SECONDS
            if not expired:
                continue
        a["text"] = PURGED
        a["purged"] = True
        changed = True
    return changed


def _age_seconds(ts: object, now: float) -> float:
    from datetime import datetime
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return float("inf")
    return now - when


def describe(action: dict) -> str:
    """One line for a replayed transcript. Never the text of a sensitive
    action (nor, having none, of a secret)."""
    kind = action.get("kind")
    if kind == "keyboard-type":
        enter = " + Enter" if action.get("enter") else ""
        if action.get("sensitive"):
            state = ("typed" if action.get("outcome") == "typed"
                     else "deleted" if action.get("purged") else "pending")
            return f"- a sensitive text{enter} ({state}; not shown)"
        return (f"- type {len(action.get('text') or '')} characters{enter}"
                f": {action.get('text', '')!r}")
    if kind == "keyboard-key":
        return f"- press {action.get('key')}"
    if kind == "keyboard-secret":
        enter = " + Enter" if action.get("enter") else ""
        return f"- password-manager field for {action.get('label')!r}{enter}"
    return ""
