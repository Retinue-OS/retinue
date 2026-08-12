#!/usr/bin/env python3
"""Focused checks for the per-message inbound store (delivery ledger).

`scripts/inbound_store.py` persists each inbound messenger message as one
deterministic N-Triples file on the gateway volume, and owns the `delivered`
flag: `undelivered()` is the sole mutator, returning held messages and marking
them delivered in one pass. This exercises the round-trip, determinism, the
delivered semantics (drain once, never twice), the `since` filter, and the
"persist but do not forward" path (delivered=True at write time).

Standalone, no third-party deps:

    python3 tests/test_inbound_store.py
"""
import importlib.util
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "inbound_store.py"


def _load():
    spec = importlib.util.spec_from_file_location("inbound_store", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ist = _load()


def test_write_and_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        subj, path = ist.write_message(
            tmp, channel="signal", sender="+41790000000",
            text='hi "there"\nsecond line', message_id="m1", timestamp=1000.0,
        )
        assert path.is_file()
        assert subj.startswith("urn:retinue:inbound:signal:")
        text = path.read_text(encoding="utf-8")
        # Deterministic: sorted, one triple per line, well-formed, no blank nodes.
        lines = [l for l in text.splitlines() if l]
        assert lines == sorted(lines), "not sorted"
        assert all(l.endswith(" .") for l in lines)
        assert "_:" not in text
        # Typed dateTime for receivedAt.
        assert "^^<http://www.w3.org/2001/XMLSchema#dateTime>" in text
        # Fresh message is owed to triage.
        fields = ist._parse(text)
        assert fields["delivered"] is False
        assert fields["sender"] == "+41790000000"
        assert fields["text"] == 'hi "there"\nsecond line'
    print("PASS test_write_and_roundtrip")


def test_undelivered_drains_once():
    with tempfile.TemporaryDirectory() as tmp:
        ist.write_message(tmp, channel="signal", sender="a", text="one", timestamp=10.0)
        ist.write_message(tmp, channel="signal", sender="b", text="two", timestamp=20.0)
        first = ist.undelivered(tmp)
        assert [m["text"] for m in first] == ["one", "two"], "not oldest-first"
        # A second drain returns nothing — the flag was flipped in place.
        assert ist.undelivered(tmp) == []
        # But the files (and their content) remain — history stays browsable.
        for p in ist.messages_dir(tmp).glob("*.nt"):
            fields = ist._parse(p.read_text(encoding="utf-8"))
            assert fields["delivered"] is True
    print("PASS test_undelivered_drains_once")


def test_since_filter():
    with tempfile.TemporaryDirectory() as tmp:
        ist.write_message(tmp, channel="wa", sender="a", text="old", timestamp=100.0)
        ist.write_message(tmp, channel="wa", sender="b", text="new", timestamp=200.0)
        # ISO cutoff between the two.
        cutoff = ist._iso(150.0)
        got = ist.undelivered(tmp, since=cutoff)
        assert [m["text"] for m in got] == ["new"]
        # The filtered-out "old" was NOT marked delivered, so a later unfiltered
        # drain still yields it.
        rest = ist.undelivered(tmp)
        assert [m["text"] for m in rest] == ["old"]
    print("PASS test_since_filter")


def test_persist_but_not_forward():
    with tempfile.TemporaryDirectory() as tmp:
        # A blacklisted / noise-class message is stored already delivered.
        ist.write_message(tmp, channel="tg", sender="spam", text="x",
                          delivered=True, timestamp=5.0)
        assert ist.undelivered(tmp) == [], "delivered=True leaked into the drain"
        # Still on disk and browsable.
        files = list(ist.messages_dir(tmp).glob("*.nt"))
        assert len(files) == 1
    print("PASS test_persist_but_not_forward")


def test_group_and_optional_fields():
    with tempfile.TemporaryDirectory() as tmp:
        ist.write_message(tmp, channel="signal", sender="a", text="hey",
                          group="grp123", timestamp=1.0)
        ist.write_message(tmp, channel="signal", sender="b", text="ho",
                          timestamp=2.0)
        got = ist.undelivered(tmp)
        by_text = {m["text"]: m for m in got}
        assert by_text["hey"]["group"] == "grp123"
        assert by_text["ho"]["group"] is None
        assert by_text["ho"]["message_id"] is None
    print("PASS test_group_and_optional_fields")


def test_missing_dir_is_empty():
    with tempfile.TemporaryDirectory() as tmp:
        assert ist.undelivered(Path(tmp) / "nope") == []
    print("PASS test_missing_dir_is_empty")


def test_since_epoch_and_iso_equivalent():
    with tempfile.TemporaryDirectory() as tmp:
        ist.write_message(tmp, channel="signal", sender="a", text="m", timestamp=300.0)
        # Epoch and ISO cutoffs agree.
        assert ist._parse_since(250.0) == 250.0
        assert abs(ist._parse_since(ist._iso(250.0)) - 250.0) < 1.0
        got = ist.undelivered(tmp, since=250.0)
        assert [m["text"] for m in got] == ["m"]
    print("PASS test_since_epoch_and_iso_equivalent")


if __name__ == "__main__":
    test_write_and_roundtrip()
    test_undelivered_drains_once()
    test_since_filter()
    test_persist_but_not_forward()
    test_group_and_optional_fields()
    test_missing_dir_is_empty()
    test_since_epoch_and_iso_equivalent()
    print("all inbound_store tests passed")
