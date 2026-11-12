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


def _entry(eid, content, recorded, *extra):
    """The triples memory.py store writes for one entry (content as given)."""
    subj = f"<urn:retinue:memory:{eid}>"
    kb = "https://w3id.org/retinue/kb#"
    lines = [f'{subj} <{kb}content> "{content}" .',
             f'{subj} <{kb}recordedAt> "{recorded:%Y-%m-%dT%H:%M:%SZ}"'
             f'^^<http://www.w3.org/2001/XMLSchema#dateTime> .']
    lines += [f"{subj} <{kb}{p}> {o} ." for p, o in extra]
    return "\n".join(lines) + "\n"


def _memory_cases(mem: Path, now: dt.datetime) -> None:
    """Memory files that are not named after one timestamp, and entries that
    no longer stand."""
    day = dt.timedelta(days=1)
    stamp = lambda t: t.strftime("%Y%m%dT%H%M%SZ")
    # A session-labelled file holds entries of several days.
    (mem / "sess-1.nt").write_text(
        _entry(f"{stamp(now - 30 * day)}-sess01", "Anna old session entry.", now - 30 * day)
        + _entry(f"{stamp(now)}-sess02", "Anna new session entry.", now), encoding="utf-8")
    # A compaction file's standing summary is a live entry too.
    (mem / f"compaction-{stamp(now)}-c0ffee.nt").write_text(
        _entry(f"{stamp(now)}-sum001", "Anna summary of older notes.", now), encoding="utf-8")
    # Backslash-u together with non-ASCII text, and an escaped backslash.
    (mem / "sess-2.nt").write_text(
        _entry(f"{stamp(now)}-esc001", "Bob: café C:\\\\users\\\\Brändle\\tx", now), encoding="utf-8")
    # Corrected, superseded and expired entries are not current.
    a, b, c, d = (f"{stamp(now - day)}-{x}" for x in ("corr01", "sup001", "exp001", "new001"))
    (mem / f"{a}.nt").write_text(
        _entry(a, "Carla lives in Basel.", now - day,
               ("correctedBy", f"<urn:retinue:memory:{stamp(now)}-corr02>")), encoding="utf-8")
    (mem / f"{stamp(now)}-corr02.nt").write_text(
        _entry(f"{stamp(now)}-corr02", "Carla lives in Bern.", now), encoding="utf-8")
    (mem / f"{b}.nt").write_text(
        _entry(b, "Carla works at Acme.", now - day,
               ("supersededBy", f"<urn:retinue:memory:{d}>")), encoding="utf-8")
    (mem / f"{d}.nt").write_text(_entry(d, "Carla works at Globex.", now), encoding="utf-8")
    soon = (now - day).strftime("%Y-%m-%dT%H:%M:%SZ")
    (mem / f"{c}.nt").write_text(
        _entry(c, "Carla visits on Friday.", now - 2 * day,
               ("expires", f'"{soon}"^^<http://www.w3.org/2001/XMLSchema#dateTime>')), encoding="utf-8")


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
    _memory_cases(mem, now)
    contacts = chambers / "notes" / "contacts"
    contacts.mkdir(parents=True)
    # A real contact, so the lookup of its handles through contacts.py is tested too.
    _load("contacts").ContactBook(str(chambers), None).create(
        "notes", "Roger Peng", [("whatsapp", "111222@lid")])
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
        os.environ.pop("RETINUE_MEMORY_DIR", None)
        recent = _load("recent")
        assert recent.contact_handles("Roger", chambers) == (["Roger Peng"], {"111222"})

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

        out = _run(recent, "Anna", "--source", "memory")  # session-labelled and compaction files
        assert "Anna new session entry" in out, out
        assert "Anna summary of older notes" in out, out
        assert "old session entry" not in out, out      # per-entry time, not the file's
        assert "-sess02:" in out and "sess-1" not in out, out  # labelled with the entry id

        out = _run(recent, "Carla", "--source", "memory")
        assert "Carla lives in Bern" in out and "Carla works at Globex" in out, out
        assert "Basel" not in out and "Acme" not in out, out   # corrected, superseded
        assert "Friday" not in out, out                         # expired

        out = _run(recent, "brandle", "--source", "memory")     # unescaped, not mojibake
        assert "café C:\\users\\Brändle x" in out, out

        # memory.py writes to RETINUE_MEMORY_DIR when set; the lookup must read
        # the same place, not the default under the chambers dir.
        custom = Path(tmp) / "custom-mem"
        custom.mkdir()
        now = dt.datetime.now(dt.timezone.utc)
        (custom / "e.nt").write_text(
            _entry(f"{now:%Y%m%dT%H%M%SZ}-cust01", "Zoltan lives in Graz", now), encoding="utf-8")
        os.environ["RETINUE_MEMORY_DIR"] = str(custom)
        try:
            out = _run(recent, "Zoltan", "--source", "memory")
            assert "Zoltan lives in Graz" in out, out
            out = _run(recent, "Carla", "--source", "memory")   # default dir is not read
            assert "## memory (0)" in out, out
        finally:
            os.environ.pop("RETINUE_MEMORY_DIR", None)

        assert recent.fold("BRÄNDLE") == "brandle"

        # A blank term or an absurd window is a usage error, not a match-all
        # listing or a traceback.
        for bad in (["  "], [""], ["x", "--days", "1e9"], ["x", "--days", "0"],
                    ["x", "--days", "-1"], ["x", "--days", "nan"]):
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    recent.main(bad)
            except SystemExit as exc:
                assert exc.code == 2, (bad, exc.code)
            else:
                raise AssertionError(f"accepted {bad}")
            assert "error:" in err.getvalue(), (bad, err.getvalue())
    print("ok")


if __name__ == "__main__":
    main()
