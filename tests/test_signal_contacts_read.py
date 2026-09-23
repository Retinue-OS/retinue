#!/usr/bin/env python3
"""Checks for the Signal gateway's read API (contacts & groups parsing).

Runnable without signal-cli or the heavy runtime deps: `langdetect` is stubbed
and `_signal_cli_json` is monkeypatched to return canned signal-cli output, so
the parsing/normalization in `_list_contacts` / `_list_groups` is exercised in
isolation.

    python3 tests/test_signal_contacts_read.py
"""
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_signal_gateway(tmp):
    if "langdetect" not in sys.modules:
        stub = types.ModuleType("langdetect")
        stub.detect = lambda *a, **k: "en"
        stub.detect_langs = lambda *a, **k: []
        stub.LangDetectException = type("LangDetectException", (Exception,), {})
        sys.modules["langdetect"] = stub
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    os.environ["PIPER_DATA_DIR"] = str(Path(tmp) / "models")
    os.environ["SIGNAL_ATTACHMENTS_DIR"] = str(Path(tmp) / "attachments")
    os.environ.setdefault("SIGNAL_ACCOUNT", "+15550000000")
    spec = importlib.util.spec_from_file_location(
        "signal_gateway_contacts_under_test", SCRIPTS_DIR / "signal-gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_list_contacts_normalizes_fields():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        # Cover the field-name variants seen across signal-cli versions.
        sg._signal_cli_json = lambda args: [
            {"number": "+15551112222", "uuid": "u-1", "name": "Jane Doe"},
            {"phoneNumber": "+15553334444", "profileName": "John Roe"},
            {"number": "+15555556666",
             "profile": {"givenName": "Max", "familyName": "Müller"}},
            {"number": "+15557778888"},  # no name at all
            "not-a-dict",                # skipped
            {"note": "no number or uuid"},  # skipped (no identifier)
        ]
        contacts = sg._list_contacts()
        assert len(contacts) == 4, contacts
        by_number = {c["number"]: c for c in contacts if c["number"]}
        assert by_number["+15551112222"]["name"] == "Jane Doe"
        assert by_number["+15551112222"]["uuid"] == "u-1"
        assert by_number["+15553334444"]["name"] == "John Roe"
        assert by_number["+15555556666"]["name"] == "Max Müller"
        assert by_number["+15557778888"]["name"] is None
    print("ok: list contacts normalizes fields across signal-cli versions")


def test_list_groups_normalizes_fields():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        sg._signal_cli_json = lambda args: [
            {"id": "g-1", "name": "Family"},
            {"groupId": "g-2", "name": "Book Club"},
            {"name": "no id"},  # skipped
            42,                 # skipped
        ]
        groups = sg._list_groups()
        assert groups == [
            {"id": "g-1", "name": "Family"},
            {"id": "g-2", "name": "Book Club"},
        ], groups
    print("ok: list groups normalizes fields")


def test_group_name_resolution_is_cached():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        calls = []

        def _fake(args):
            calls.append(args)
            return [{"id": "g-1", "name": "Family"}]

        sg._signal_cli_json = _fake
        assert sg._resolve_group_name("g-1") == "Family"
        assert sg._resolve_group_name("g-1") == "Family"
        # An unknown id right after a refresh does not re-run signal-cli.
        assert sg._resolve_group_name("g-unknown") is None
        assert len(calls) == 1, calls
        # Once the miss window has passed, a miss refreshes the roster.
        sg._group_names_at -= sg._GROUP_NAMES_MISS_RETRY + 1
        assert sg._resolve_group_name("g-unknown") is None
        assert len(calls) == 2, calls
    print("ok: group names resolved from a cached roster")


def test_group_name_first_lookup_right_after_boot():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        sg._signal_cli_json = lambda args: [{"id": "g-1", "name": "Family"}]
        # A monotonic clock still below the miss window (fresh host boot) must
        # not make the never-filled cache look fresh.
        sg.time = types.SimpleNamespace(monotonic=lambda: 5.0)
        assert sg._resolve_group_name("g-1") == "Family"
    print("ok: first group-name lookup fetches even right after boot")


def test_group_name_failed_refresh_is_throttled():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        roster = [{"id": "g-1", "name": "Family"}]
        calls = []

        def _fake(args):
            calls.append(args)
            if roster is None:
                raise RuntimeError("signal-cli down")
            return roster

        sg._signal_cli_json = _fake
        assert sg._resolve_group_name("g-1") == "Family"
        # The TTL expires and signal-cli is failing: one attempt, then the
        # attempt itself throttles further calls and the last map is kept.
        sg._group_names_at -= sg._GROUP_NAMES_TTL + 1
        roster = None
        assert sg._resolve_group_name("g-1") == "Family"
        assert sg._resolve_group_name("g-1") == "Family"
        assert sg._resolve_group_name("g-other") is None
        assert len(calls) == 2, calls
    print("ok: a failing roster refresh is throttled and keeps the last names")


def test_chat_event_carries_group_name():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        sg._signal_cli_json = lambda args: [{"id": "g-1", "name": "Family"}]
        sent = []
        sg._chats.chats_enabled = lambda: True
        sg._chats.notify_chat_event = lambda **kw: sent.append(kw)
        started = []
        # Run the rail thread inline (replacing the module's reference only).
        sg.threading = types.SimpleNamespace(
            Thread=lambda target, **kw: types.SimpleNamespace(
                start=lambda: (started.append(1), target())))
        sg._notify_chat_event_async(group_id="g-1", direction="in", chat="group:g-1")
        sg._notify_chat_event_async(direction="in", chat="+15551112222")
        assert started == [1, 1], started
        assert sent[0]["chat_name"] == "Family", sent
        assert sent[1]["chat_name"] is None, sent
    print("ok: chats-rail events carry the group's display name")


def test_signal_cli_json_nonzero_raises():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "boom"

        sg._run = lambda *a, **k: _Proc()
        raised = False
        try:
            sg._signal_cli_json(["listContacts"])
        except RuntimeError as exc:
            raised = "boom" in str(exc)
        assert raised, "expected RuntimeError carrying signal-cli stderr"
    print("ok: signal_cli_json surfaces non-zero exit")


def _envelope(**fields):
    return {"envelope": fields}


def test_recent_senders_recorded_most_recent_first():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        sg.SIGNAL_RECENT_CHATS_PATH = Path(tmp) / "recent-chats.json"

        sg._record_recent_sender(_envelope(
            sourceNumber="+15551112222", sourceUuid="u-1", sourceName="Jane Doe"))
        sg._record_recent_sender(_envelope(
            sourceNumber="+15553334444", sourceName="John Roe"))

        recent = sg._list_recent_chats()
        # Most-recent-first: the last-recorded sender leads.
        assert [e["name"] for e in recent] == ["John Roe", "Jane Doe"], recent
        assert recent[1]["number"] == "+15551112222"
        assert recent[1]["uuid"] == "u-1"
        assert all("last_seen" in e for e in recent)
    print("ok: recent senders recorded most-recent-first")


def test_recent_sender_dedups_and_moves_to_front():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        sg.SIGNAL_RECENT_CHATS_PATH = Path(tmp) / "recent-chats.json"

        sg._record_recent_sender(_envelope(sourceNumber="+15551112222", sourceName="Jane"))
        sg._record_recent_sender(_envelope(sourceNumber="+15559990000", sourceName="Other"))
        # Same person again by UUID, no name this time — must merge, keep the name,
        # and jump to the front rather than create a second entry.
        sg._record_recent_sender(_envelope(sourceNumber="+15551112222", sourceUuid="u-9"))

        recent = sg._list_recent_chats()
        numbers = [e["number"] for e in recent]
        assert numbers.count("+15551112222") == 1, recent
        assert recent[0]["number"] == "+15551112222", recent
        assert recent[0]["name"] == "Jane", recent  # carried forward
        assert recent[0]["uuid"] == "u-9", recent      # newly learned id merged in
    print("ok: recent sender dedups by shared id and moves to front")


def test_recent_chats_cap_enforced():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        sg.SIGNAL_RECENT_CHATS_PATH = Path(tmp) / "recent-chats.json"
        sg.SIGNAL_RECENT_CHATS_MAX = 3

        for i in range(6):
            sg._record_recent_sender(_envelope(sourceNumber=f"+1555000000{i}"))
        recent = sg._list_recent_chats()
        assert len(recent) == 3, recent
        # Newest kept, oldest dropped.
        assert recent[0]["number"] == "+15550000005"
        assert recent[-1]["number"] == "+15550000003"
    print("ok: recent-chats list capped at max")


def main():
    test_list_contacts_normalizes_fields()
    test_list_groups_normalizes_fields()
    test_group_name_resolution_is_cached()
    test_group_name_first_lookup_right_after_boot()
    test_group_name_failed_refresh_is_throttled()
    test_chat_event_carries_group_name()
    test_signal_cli_json_nonzero_raises()
    test_recent_senders_recorded_most_recent_first()
    test_recent_sender_dedups_and_moves_to_front()
    test_recent_chats_cap_enforced()
    print("\nAll Signal read-API checks passed.")


if __name__ == "__main__":
    main()
