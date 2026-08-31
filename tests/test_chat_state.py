#!/usr/bin/env python3
"""Focused checks for the chat state store and the live overlay.

`scripts/chat_state.py` holds everything about a messenger chat that is not a
channel message: the read watermark (forward-only), the version-guarded shared
draft, archive/mute, cached display metadata — plus the in-memory overlay that
bridges the life store's indexing lag. This exercises the draft guard (stale
version rejected; agent staging never clobbers user text), watermark and
unread-classification semantics, path-safe state filenames, and the overlay's
merge/dedup/expiry contract.

Standalone, no third-party deps:

    python3 tests/test_chat_state.py
"""
import importlib.util
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "chat_state.py"


def _load():
    spec = importlib.util.spec_from_file_location("chat_state", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cs = _load()


def test_split_and_filename_safety():
    # Split at the FIRST colon only — chat keys carry their own colons.
    assert cs.split_chat_id("signal:group:R2xh/cnVz=") == ("signal", "group:R2xh/cnVz=")
    assert cs.split_chat_id("whatsapp:4179@s.whatsapp.net") == ("whatsapp", "4179@s.whatsapp.net")
    assert cs.split_chat_id("telegram:-1001455667788") == ("telegram", "-1001455667788")
    assert cs.split_chat_id("nochannel") is None
    assert cs.split_chat_id("signal:") is None
    # Filenames are digests: path-hostile key characters never touch the fs,
    # and distinct ids never collide on a file.
    a = cs.state_filename("signal:group:R2xh/../../etc=")
    b = cs.state_filename("signal:group:R2xh/other=")
    assert a != b and "/" not in a and a.endswith(".json")
    print("PASS test_split_and_filename_safety")


def test_draft_version_guard():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "signal:+41790001122"
        # First user write is based on version 0 (no draft yet).
        ok, doc = store.set_draft(cid, "hoi", author="user", base_version=0)
        assert ok and doc["draft"]["text"] == "hoi"
        assert doc["draft"]["author"] == "user" and doc["draft_version"] == 1
        # A writer that still holds version 0 is stale → rejected, state intact.
        ok, doc = store.set_draft(cid, "clobber", author="user", base_version=0)
        assert not ok and doc["draft"]["text"] == "hoi"
        # The current version succeeds; empty text clears the draft AND its
        # author tag, but the counter keeps counting (a clear is guarded too).
        ok, doc = store.set_draft(cid, "", author="user", base_version=1)
        assert ok and doc["draft"] is None and doc["draft_version"] == 2
        print("PASS test_draft_version_guard")


def test_agent_staging_never_clobbers_user_text():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "telegram:774301992"
        # Agent stages onto an empty draft without knowing a version: accepted.
        ok, doc = store.set_draft(cid, "Draft A", author="agent", agent="Ara",
                                  require_free=True)
        assert ok and doc["draft"]["author"] == "agent"
        assert doc["draft"]["agent"] == "Ara"
        # Re-staging over its own draft is fine (the agent revises itself)…
        ok, doc = store.set_draft(cid, "Draft B", author="agent", require_free=True)
        assert ok
        # …but once the user typed, a versionless agent write is refused.
        ok, doc = store.set_draft(cid, "mine", author="user",
                                  base_version=doc["draft_version"])
        assert ok
        ok, doc = store.set_draft(cid, "agent again", author="agent",
                                  require_free=True)
        assert not ok and doc["draft"]["text"] == "mine"
        # With the current version the agent asserts an informed overwrite.
        ok, doc = store.set_draft(cid, "agent again", author="agent",
                                  base_version=doc["draft_version"])
        assert ok and doc["draft"]["text"] == "agent again"
        # Invalid author is a programming error.
        try:
            store.set_draft(cid, "x", author="assistant")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid author must raise")
        print("PASS test_agent_staging_never_clobbers_user_text")


def test_last_read_forward_only_and_unread_modes():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "whatsapp:4179@s.whatsapp.net"
        # First inbound on a read chat: no unread before → "new".
        doc, had = store.mark_unread(cid, 1000.0)
        assert not had and doc["unread_since"] == cs.iso_z(1000.0)
        # A second inbound while still unread → "reply".
        _, had = store.mark_unread(cid, 1010.0)
        assert had
        # Catching up clears the watermark…
        doc = store.advance_last_read(cid, 1010.0)
        assert doc["last_read"] == cs.iso_z(1010.0)
        assert doc["unread_since"] is None
        # …and the watermark never regresses (a stale client cannot resurrect
        # read messages).
        doc = store.advance_last_read(cid, 500.0)
        assert doc["last_read"] == cs.iso_z(1010.0)
        # The next inbound is "new" again.
        _, had = store.mark_unread(cid, 1020.0)
        assert not had
        print("PASS test_last_read_forward_only_and_unread_modes")


def test_flags_and_note_message():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "signal:+41794456312"
        doc = store.set_flags(cid, archived=True, muted=True)
        assert doc["archived"] and doc["muted"]
        doc = store.set_flags(cid, archived=False)
        assert not doc["archived"] and doc["muted"], "muted must survive un-archive"
        doc = store.note_message(cid, name="Mara Meier", group=False,
                                 gateway="signal-gateway-personal",
                                 sender="+41794456312", sender_name="Mara Meier")
        assert doc["name"] == "Mara Meier"
        assert doc["gateway"] == "signal-gateway-personal"
        assert doc["roster"]["+41794456312"] == "Mara Meier"
        assert doc["roster_refreshed"] is not None
        # all() round-trips by id.
        assert store.all()[cid]["name"] == "Mara Meier"
        print("PASS test_flags_and_note_message")


def test_companion_link():
    with tempfile.TemporaryDirectory() as tmp:
        store = cs.ChatStateStore(tmp)
        cid = "whatsapp:123456@g.us"
        assert store.get(cid)["companion"] is None, "no companion by default"
        doc = store.set_companion(cid, "deadbeef")
        assert doc["companion"] == "deadbeef"
        # Survives an unrelated write, and round-trips through all().
        store.set_flags(cid, archived=True)
        assert store.get(cid)["companion"] == "deadbeef"
        assert store.all()[cid]["companion"] == "deadbeef"
        assert store.set_companion(cid, None)["companion"] is None
        print("PASS test_companion_link")


def test_overlay_merge_dedup_expiry():
    ov = cs.ChatOverlay(ttl=60.0)
    cid = "signal:+41790001122"
    ov.insert({"chat_id": cid, "direction": "in", "text": "a",
               "ts": cs.iso_z(100.0), "message_id": "m1"})
    # Re-delivery of the same message id replaces, never duplicates.
    ov.insert({"chat_id": cid, "direction": "in", "text": "a (edited)",
               "ts": cs.iso_z(100.0), "message_id": "m1"})
    # No id → the (chat, ts, text) fallback key dedups.
    ov.insert({"chat_id": cid, "direction": "out", "author": "user",
               "text": "b", "ts": cs.iso_z(200.0)})
    ov.insert({"chat_id": cid, "direction": "out", "author": "user",
               "text": "b", "ts": cs.iso_z(200.0)})
    ov.insert({"chat_id": "telegram:1", "direction": "in", "text": "other",
               "ts": cs.iso_z(150.0), "message_id": "m2"})
    got = ov.entries(cid)
    assert [e["text"] for e in got] == ["a (edited)", "b"], got
    assert all("_inserted" not in e for e in got)
    assert len(ov.entries()) == 3
    # Expiry: a stale entry drops out (backdated directly, as the RecentSends
    # test does — the clock is the contract, not a sleep).
    with ov._lock:
        for key in list(ov._entries):
            if ov._entries[key].get("message_id") == "m1":
                ov._entries[key]["_inserted"] = time.time() - 120.0
    assert [e["text"] for e in ov.entries(cid)] == ["b"]
    print("PASS test_overlay_merge_dedup_expiry")


def test_iso_z_normalizes():
    assert cs.iso_z(0) == "1970-01-01T00:00:00Z"
    assert cs.iso_z("2026-08-27T09:12:00+02:00") == "2026-08-27T07:12:00Z"
    assert cs.iso_z("2026-08-27T07:12:00Z") == "2026-08-27T07:12:00Z"
    # ISO strings compare as instants once normalized.
    assert cs.iso_z(10.0) < cs.iso_z(20.0)
    print("PASS test_iso_z_normalizes")


if __name__ == "__main__":
    test_split_and_filename_safety()
    test_draft_version_guard()
    test_agent_staging_never_clobbers_user_text()
    test_last_read_forward_only_and_unread_modes()
    test_flags_and_note_message()
    test_companion_link()
    test_overlay_merge_dedup_expiry()
    test_iso_z_normalizes()
    print("all chat-state tests passed")
