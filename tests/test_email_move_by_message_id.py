#!/usr/bin/env python3
"""Focused checks for `email_client.move_by_message_id`.

The web gateway calls it after a reply's approval on /sends, to take the mail
the reply answers out of the INBOX. It must move exactly that mail (IMAP
HEADER search is a substring match) and must not report a move whose source
copy is still there.

    python3 tests/test_email_move_by_message_id.py
"""
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_email_client():
    spec = importlib.util.spec_from_file_location(
        "email_client_move_by_mid_under_test", SCRIPTS_DIR / "email_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeIMAP:
    """A selected folder of {uid: Message-ID}, with substring HEADER search."""

    def __init__(self, messages, *, move_ok=True, expunge_ok=True):
        self.messages = dict(messages)
        self.move_ok = move_ok
        self.expunge_ok = expunge_ok
        self.moved = []
        self.copied = []
        self.flagged = []
        self.logged_out = False

    def select(self, folder, readonly=False):
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        command = command.lower()
        if command == "search":
            needle = args[-1].strip('"')
            hits = [u for u, mid in self.messages.items() if needle in mid]
            return "OK", [" ".join(hits).encode()]
        if command == "fetch":
            uid = args[0]
            mid = self.messages.get(uid)
            if mid is None:
                return "OK", [None]
            header = f"Message-ID: {mid}\r\n\r\n".encode()
            return "OK", [(f"{uid} (BODY[HEADER.FIELDS (MESSAGE-ID)] "
                           f"{{{len(header)}}}".encode(), header), b")"]
        if command == "move":
            if not self.move_ok:
                return "NO", [b"MOVE not supported"]
            self.moved.append(args[0])
            return "OK", [b""]
        if command == "copy":
            self.copied.append(args[0])
            return "OK", [b""]
        if command == "store":
            self.flagged.append(args[0])
            return "OK", [b""]
        raise AssertionError(f"unexpected UID command {command}")

    def expunge(self):
        return ("OK", [b""]) if self.expunge_ok else ("NO", [b"expunge refused"])

    def logout(self):
        self.logged_out = True


def _wire(ec, fake):
    ec.imap_connect = lambda cfg: fake


def test_exact_match_wins_over_a_substring_neighbour(ec):
    # uid 7 (<11@host>) is later in the folder and also matches "1@host".
    fake = _FakeIMAP({"3": "<1@host>", "7": "<11@host>"})
    _wire(ec, fake)
    uid = ec.move_by_message_id(None, "<1@host>", "INBOX", "Archive")
    assert uid == "3", uid
    assert fake.moved == ["3"], fake.moved
    assert fake.logged_out


def test_only_a_neighbour_present_moves_nothing(ec):
    fake = _FakeIMAP({"7": "<11@host>", "8": " <x1@host> "})
    _wire(ec, fake)
    uid = ec.move_by_message_id(None, "<1@host>", "INBOX", "Archive")
    assert uid is None, uid
    assert not (fake.moved or fake.copied or fake.flagged)


def test_the_last_of_several_exact_matches_moves(ec):
    fake = _FakeIMAP({"3": "<1@host>", "5": "<11@host>", "9": "1@host"})
    _wire(ec, fake)
    assert ec.move_by_message_id(None, "1@host", "INBOX", "Archive") == "9"
    assert fake.moved == ["9"], fake.moved


def test_copy_fallback_with_failed_expunge_raises(ec):
    fake = _FakeIMAP({"3": "<1@host>"}, move_ok=False, expunge_ok=False)
    _wire(ec, fake)
    try:
        ec.move_by_message_id(None, "<1@host>", "INBOX", "Archive")
    except ec.EmailError as exc:
        assert "expunge failed" in str(exc), exc
    else:
        raise AssertionError("a failed expunge must not report a move")
    assert fake.copied == ["3"] and fake.flagged == ["3"]
    assert fake.logged_out


def test_copy_fallback_with_expunge_reports_the_uid(ec):
    fake = _FakeIMAP({"3": "<1@host>"}, move_ok=False)
    _wire(ec, fake)
    assert ec.move_by_message_id(None, "<1@host>", "INBOX", "Archive") == "3"
    assert fake.copied == ["3"] and fake.flagged == ["3"]


def main():
    ec = _load_email_client()
    test_exact_match_wins_over_a_substring_neighbour(ec)
    test_only_a_neighbour_present_moves_nothing(ec)
    test_the_last_of_several_exact_matches_moves(ec)
    test_copy_fallback_with_failed_expunge_raises(ec)
    test_copy_fallback_with_expunge_reports_the_uid(ec)
    print("all email move_by_message_id tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
