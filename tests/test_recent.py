#!/usr/bin/env python3
"""Checks for scripts/recent.py: the cross-source "what happened lately" lookup.

Builds a throw-away chambers dir (messenger store, memory, a project) and a
conversations dir, then asserts that a term finds its hits in every source,
that a contact's handle matches a chat whose text never names them, that
matching is accent-insensitive, and that the window excludes old entries.

Standalone, no third-party deps:

    python3 tests/test_recent.py
"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixture(root: Path) -> tuple[Path, Path]:
    now = dt.datetime.now(dt.timezone.utc)
    chambers, convs = root / "chambers", root / "conversations"
    store = _load("inbound_store")
    wa = chambers / "_generated" / "messenger" / "whatsapp"
    # Inbound from the contact's handle; the text never names him.
    store.write_message(wa, channel="whatsapp", sender="111222@lid", chat="111222@lid",
                        account="+41000", text="Termin am 24.11. steht.", message_id="m1")
    # Unrelated chat that mentions the subject word.
    store.write_message(wa, channel="whatsapp", sender="999@lid", chat="999@lid",
                        account="+41000", text="Die Eignungsabklärung ist verschoben.",
                        message_id="m2")
    mem = chambers / "_generated" / "memory"
    mem.mkdir(parents=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    (mem / f"{stamp}-abc123.nt").write_text(
        f'<urn:retinue:memory:{stamp}-abc123> <https://w3id.org/retinue/kb#content> '
        f'"Roger booked the assessment." .\n', encoding="utf-8")
    old = (now - dt.timedelta(days=40)).strftime("%Y%m%dT%H%M%SZ")
    (mem / f"{old}-old000.nt").write_text(
        f'<urn:retinue:memory:{old}-old000> <https://w3id.org/retinue/kb#content> '
        f'"Roger, long ago." .\n', encoding="utf-8")
    proj = chambers / "notes" / "projects"
    proj.mkdir(parents=True)
    (proj / "driving.md").write_text("- 2026-11-12 — new date with Roger Peng\n", encoding="utf-8")
    contacts = chambers / "notes" / "contacts"
    contacts.mkdir(parents=True)
    convs.mkdir()
    (convs / "t1.json").write_text(json.dumps({
        "id": "t1", "title": "Companion: Fahrschule",
        "messages": [{"role": "agent", "ts": now.isoformat(), "text": "Roger hat einen neuen Termin."},
                     {"role": "agent", "ts": (now - dt.timedelta(days=30)).isoformat(),
                      "text": "Roger old message."}]}), encoding="utf-8")
    return chambers, convs


def _run(recent, *argv) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert recent.main(list(argv)) == 0
    return out.getvalue()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        chambers, convs = _fixture(Path(tmp))
        os.environ["CHAMBERS_DIR"] = str(chambers)
        os.environ["CONVERSATIONS_DIR"] = str(convs)
        os.environ["CHAMBERS_MANIFEST"] = str(Path(tmp) / "absent.json")
        os.environ.pop("TRIAGE_MESSENGER_DIR", None)
        recent = _load("recent")
        # Contact handles are resolved via contacts.py; stub them for the fixture.
        recent.contact_handles = lambda term, _d: (["Roger (Fahrschule)"], {"111222"}) \
            if "roger" in term.lower() else ([], set())

        out = _run(recent, "Roger")
        assert "Termin am 24.11." in out, out           # matched by handle, not text
        assert "Roger hat einen neuen Termin" in out, out
        assert "Roger old message" not in out, out      # outside the 7-day window
        assert "Roger booked the assessment" in out, out
        assert "long ago" not in out, out
        assert "driving.md" in out, out
        assert "Eignungsabklärung" not in out, out      # unrelated chat stays out

        out = _run(recent, "eignungsabklarung", "--source", "chats")  # accent-insensitive
        assert "Eignungsabklärung ist verschoben" in out, out
        assert "## threads" not in out, out

        assert recent.fold("BRÄNDLE") == "brandle"
    print("ok")


if __name__ == "__main__":
    main()
