#!/usr/bin/env python3
"""Checks that scripts/recent.py reads the mail gateway's mirror right.

A mirrored mail lands in the same ledger the messenger gateways write
(chambers/_generated/messenger/email/messages/), so recent.py's chat scan
already reaches it. What differs is the record's shape: the chat key is the
thread's root Message-ID, which names nobody, the person is an address, and
the subject is where a subject word often is. These pin that a mail is found
by its sender's contact address, by a person on its To/Cc, by a word of its
subject, and is shown with the sender's name and the subject rather than a
Message-ID; that a tombstone says what it is; and that a Message-ID never
matches a name.

    python3 tests/test_recent_mail.py
"""
import contextlib
import importlib.util
import io
import os
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(recent, *argv) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert recent.main(list(argv)) == 0
    return out.getvalue()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chambers = root / "chambers"
        mail = chambers / "_generated" / "messenger" / "email"
        store = _load("inbound_store")
        now = time.time()
        # From a contact whose address shares nothing with their name.
        store.write_message(
            mail, channel="email", sender="rb@work.example", sender_name="Roger\nBrändle",
            chat="CAF8xRoot@mail.example", account="me@example.org",
            message_id="m1@work.example", timestamp=now - 60, text="The numbers are attached.",
            subject="Quarterly numbers", recipients=["me@example.org"], trim="deterministic")
        # The user's own mail to someone else, copying the contact.
        store.write_outbound(
            mail, channel="email", chat="CAF8xRoot@mail.example", author="device",
            account="me@example.org", message_id="m2@example.org", timestamp=now - 30,
            text="Thanks, looks good.", sender="me@example.org", subject="Re: Quarterly numbers",
            recipients=["ann@other.example"], cc=["rb@work.example"])
        # A word only the subject carries.
        store.write_message(
            mail, channel="email", sender="news@shop.example", chat="x1@shop.example",
            account="me@example.org", message_id="x1@shop.example", timestamp=now - 20,
            text="See inside.", subject="Eignungsabklärung termin")
        # A mail since deleted in a client.
        _urn, gone = store.write_message(
            mail, channel="email", sender="rb@work.example", chat="gone@work.example",
            account="me@example.org", message_id="gone@work.example", timestamp=now - 10,
            text="Secret draft numbers", subject="Old figures")
        assert store.tombstone_message(gone)
        # A Message-ID whose local part happens to be a name.
        store.write_message(
            mail, channel="email", sender="someone@else.example", chat="roger@ids.example",
            account="me@example.org", message_id="roger@ids.example", timestamp=now - 5,
            text="Unrelated.", subject="Unrelated")

        contacts = chambers / "notes" / "contacts"
        contacts.mkdir(parents=True)
        _load("contacts").ContactBook(str(chambers), None).create(
            "notes", "Roger Brändle", [("email", "rb@work.example")])

        os.environ["CHAMBERS_DIR"] = str(chambers)
        os.environ["CONVERSATIONS_DIR"] = str(root / "conversations")
        os.environ["CHAMBERS_MANIFEST"] = str(root / "absent.json")
        os.environ.pop("TRIAGE_MESSENGER_DIR", None)
        os.environ.pop("RETINUE_MEMORY_DIR", None)
        recent = _load("recent")

        out = _run(recent, "Brändle", "--source", "chats")
        assert "email ← Roger Brändle <rb@work.example> «Quarterly numbers»: " \
               "The numbers are attached." in out, out
        # Found under the contact although only the Cc names them; shown by
        # whom it was written to.
        assert "email → ann@other.example, rb@work.example «Re: Quarterly numbers»: " \
               "Thanks, looks good." in out, out
        assert "«Old figures»: [deleted from the mailbox]" in out, out
        assert "Secret draft numbers" not in out, out
        assert "CAF8xRoot" not in out, "the chat key is never shown as the party"

        # "roger" is the local part of the last mail's chat key and Message-ID,
        # which is how a messenger chat key would match it.
        out = _run(recent, "Roger", "--source", "chats")
        assert "Quarterly numbers" in out, out
        assert "Unrelated" not in out, "a Message-ID is not a person"

        out = _run(recent, "eignungsabklarung", "--source", "chats")
        assert "«Eignungsabklärung termin»: See inside." in out, out
    print("ok")


if __name__ == "__main__":
    main()
