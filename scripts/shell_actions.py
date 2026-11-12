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
import unicodedata
import uuid

MANIFEST_VAR = "RETINUE_REPLY_SHELL_ACTIONS_FILE"

KINDS = ("keyboard-type", "keyboard-key", "keyboard-secret")
KEYS = ("enter", "tab", "backspace", "escape", "up", "down", "left", "right",
        "home", "end", "delete", "space")

# The longest text one action types. The app paces its key reports so the
# receiving device keeps up (12 ms after each of the two or three reports a
# character takes), so this is already one and a half to two and a half
# minutes of typing.
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
# A manifest bigger than this is not one: MAX_ACTIONS lines of a MAX_TEXT
# text written as \\uXXXX escapes (shell-type.py writes ASCII JSON) fit.
MAX_MANIFEST_BYTES = 512 * 1024

# What the page may report about an action (POST
# /conversations/<id>/shell-actions/<action id>/outcome): the states of the
# app's `keyboard` events, and the error keys the app gives (RetinueShell/5).
OUTCOME_STATES = ("typed", "error", "secret-typed", "secret-cancelled")
OUTCOME_ERRORS = ("not-enabled", "not-connected", "not-in-front", "too-long",
                  "unsupported-char", "busy", "cancelled", "failed")

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Control characters other than newline and tab cannot be typed on a
# keyboard; refusing them keeps escape sequences out of the target device.
# So are the C1 controls, the Unicode line and paragraph separators (which
# would also split a manifest line) and the bidi controls.
_CONTROL_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069]")


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
        # One plain line: control, format (bidi) and separator characters
        # become spaces, so a label cannot spoof or break the phone's dialog.
        label = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c
                        for c in label)
        label = " ".join(label.split())
        if not label:
            raise InvalidAction("keyboard-secret needs a label")
        if len(label) > MAX_LABEL:
            raise InvalidAction(f"label is longer than {MAX_LABEL} characters")
        out["label"] = label
        out["enter"] = bool(raw.get("enter"))
    return out


def normalize_outcome(raw: object) -> "dict | None":
    """A reported outcome in its stored form ({state} or {state, error}), or
    None when it is not one. An unknown error key is stored as `failed`."""
    if not isinstance(raw, dict) or raw.get("state") not in OUTCOME_STATES:
        return None
    out = {"state": raw["state"]}
    if out["state"] == "error":
        err = raw.get("error")
        out["error"] = err if err in OUTCOME_ERRORS else "failed"
    return out


def runs_by_itself(action: dict) -> bool:
    """Whether the page may run `action` without a tap when its reply arrives
    live (webapp/components/shell-keyboard.js, needsTap, applies the same
    rule; the gateway also requires a thread the user opened). Only plain
    text on one line: nothing that can submit or act — `enter`, a line break
    (typed as Shift+Enter, which submits in a terminal or a one-line field),
    a tab (which moves to a form's button) or any single key (Space presses
    a focused button) — and no sensitive text, which goes nowhere the user
    has not just confirmed. Never a password-manager field.

    The Ara app asks on the phone itself, a second time, before Enter, a line
    break, a tab, an empty text, a text with whitespace at either end, or any
    key (everything its onlyWrites rule does not hold for; the phone cannot
    see `sensitive`), and by default (its one-line mode) types plain
    one-line text at once: in that mode, whatever this rule lets run by
    itself the phone does not ask about, and what it asks about this rule
    never lets run by itself. The app's rule must never be the stricter one.
    The phone also asks about plain one-line text after the user pressed
    Stop on its strip, and always on a device set to ask every time; such a
    text may then end as `cancelled` up to 25 s after the page asked, which
    the page takes as the end of that action like any other."""
    if not isinstance(action, dict) or action.get("kind") != "keyboard-type":
        return False
    text = action.get("text")
    # Nor text that is only whitespace or starts or ends with it (a space
    # presses a focused button too). What plain characters do on the device
    # is the device's: a letter can be an app's shortcut — which is why the
    # gateway also requires the text to be a whole line the user has already
    # seen in the thread (_conv_text_was_shown), and marks only a reply's
    # first action.
    return (isinstance(text, str) and bool(text.strip()) and text.strip() == text
            and not action.get("enter") and not action.get("sensitive")
            and not re.search(r"[\r\n\t]", text))


def parse_manifest(raw: str) -> tuple[list[dict], list[str]]:
    """The valid actions of a manifest (one JSON object per line), and the
    reasons the others were dropped. Duplicate ids keep the first."""
    actions: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    # "\n" only: str.splitlines() also splits at U+2028 and friends.
    for n, line in enumerate(raw.split("\n"), 1):
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
                expired = age_seconds(message.get("ts"), now) >= SENSITIVE_TTL_SECONDS
            if not expired:
                continue
        a["text"] = PURGED
        a["purged"] = True
        changed = True
    return changed


def age_seconds(ts: object, now: float) -> float:
    from datetime import datetime
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return float("inf")
    return now - when


# The outcomes that say the action was carried out on the device.
TYPED_STATES = ("typed", "secret-typed")


def record_outcome(action: dict, outcome: dict, at: str) -> None:
    """Store `outcome` (normalize_outcome) on `action`, reported at `at`.

    An action once typed stays typed: a later Retype that does not get
    through (a confirmation on the phone declined or not answered, a device
    gone) cannot undo what the first one did on the device, so it goes into
    `last_attempt` and the stored `outcome` keeps saying typed. A later one
    that types replaces both."""
    new = dict(outcome, at=at)
    stored = action.get("outcome")
    if (isinstance(stored, dict) and stored.get("state") in TYPED_STATES
            and outcome.get("state") not in TYPED_STATES):
        action["last_attempt"] = new
        return
    action["outcome"] = new
    action.pop("last_attempt", None)


def _phrase(o: object) -> str:
    """One stored outcome ({state, error?}) in words."""
    state =o.get("state") if isinstance(o, dict) else None
    if state in TYPED_STATES:
        return "typed"
    if state == "secret-cancelled":
        return "cancelled by the user"
    if state == "error":
        why = o.get("error") or "failed"
        if why == "cancelled":
            # The page's Stop and a confirmation on the phone (asked before
            # anything but plain one-line text: Enter, a line break, a tab,
            # whitespace at either end, an empty text, or a key) that the
            # user declined or did not answer in time end the same way on
            # the wire. Saying
            # "stopped by the user" would tell Ara the user refused, when
            # they may not have looked at the phone.
            return ("not typed: cancelled — stopped, or declined or not "
                    "confirmed in time on the phone")
        return f"not typed: {why}"
    return "no outcome reported yet"


def outcome_phrase(action: dict) -> str:
    """What became of `action`, as the page reported it — and, for one that
    was typed, what a later Retype that did not type came to
    (record_outcome)."""
    if not isinstance(action, dict):
        return _phrase(None)
    phrase = _phrase(action.get("outcome"))
    later = action.get("last_attempt")
    if phrase == "typed" and isinstance(later, dict) and later.get("state") in OUTCOME_STATES:
        return f"typed (a later retype: {_phrase(later)})"
    return phrase


def describe(action: dict) -> str:
    """One line for a transcript shown to Ara, with the outcome the page
    reported. Never the text of a sensitive action (nor, having none, of a
    secret)."""
    kind = action.get("kind")
    outcome = outcome_phrase(action)
    if kind == "keyboard-type":
        enter = " + Enter" if action.get("enter") else ""
        if action.get("sensitive"):
            deleted = "deleted, " if action.get("purged") else ""
            return f"- a sensitive text{enter} ({outcome}; {deleted}not shown)"
        return (f"- type {len(action.get('text') or '')} characters{enter}"
                f": {action.get('text', '')!r} ({outcome})")
    if kind == "keyboard-key":
        return f"- press {action.get('key')} ({outcome})"
    if kind == "keyboard-secret":
        enter = " + Enter" if action.get("enter") else ""
        return f"- password-manager field for {action.get('label')!r}{enter} ({outcome})"
    return ""
