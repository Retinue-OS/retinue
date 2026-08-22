#!/usr/bin/env python3
"""Checks for the provider-header workaround in the approval send path.

A pending send is parked in the IMAP Drafts folder until the user approves it,
so it makes a round trip through the provider's IMAP store — and Zoho stamps
`X-ZohoMail-Sender` onto it there, carrying the From display name as raw 8-bit
bytes. Its relay then labels those bytes with the placeholder charset token
`unknown-8bit`, which strict Exchange receivers reject (550 ExchangeDataException).
The direct-send path never sees the header, which is why only approved sends
bounced. These tests pin the stripping down to the header level.

    python3 tests/test_email_strip_headers.py
"""
import importlib.util
import os
import sys
from email.message import EmailMessage
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_email_client():
    spec = importlib.util.spec_from_file_location(
        "email_client", SCRIPTS_DIR / "email_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _draft(**headers):
    """A message as fetched back from Drafts, with provider headers applied."""
    msg = EmailMessage()
    msg["From"] = "Jane Doe <jane@example.com>"
    msg["To"] = "recipient@example.org"
    msg["Subject"] = "Test"
    msg.set_content("body")
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    return msg


def test_default_strips_zoho_header(ec):
    os.environ.pop("SEND_STRIP_HEADERS", None)
    # The raw, un-encoded 8-bit display name Zoho injects.
    msg = _draft(X_ZohoMail_Sender="Jane Döe")
    removed = ec.strip_provider_headers(msg)
    assert removed == ["X-ZohoMail-Sender"], removed
    assert msg.get("X-ZohoMail-Sender") is None
    # Everything else survives untouched.
    assert msg.get("From") == "Jane Doe <jane@example.com>"
    assert msg.get("Subject") == "Test"
    print("PASS default strips X-ZohoMail-Sender")


def test_absent_header_is_a_noop(ec):
    os.environ.pop("SEND_STRIP_HEADERS", None)
    msg = _draft()
    assert ec.strip_provider_headers(msg) == []
    assert msg.get("From") == "Jane Doe <jane@example.com>"
    print("PASS absent header is a no-op")


def test_every_occurrence_removed(ec):
    """A duplicated injection must not leave one copy behind."""
    os.environ.pop("SEND_STRIP_HEADERS", None)
    msg = _draft()
    msg["X-ZohoMail-Sender"] = "Jane Döe"
    msg["X-ZohoMail-Sender"] = "Jane Döe"
    ec.strip_provider_headers(msg)
    assert msg.get_all("X-ZohoMail-Sender") is None
    print("PASS every occurrence removed")


def test_configurable_list(ec):
    os.environ["SEND_STRIP_HEADERS"] = "X-Custom-One, X-Custom-Two"
    msg = _draft(X_Custom_One="a", X_Custom_Two="b",
                 X_ZohoMail_Sender="Jane Döe")
    removed = ec.strip_provider_headers(msg)
    assert removed == ["X-Custom-One", "X-Custom-Two"], removed
    # The configured list replaces the default rather than extending it.
    assert msg.get("X-ZohoMail-Sender") == "Jane Döe"
    print("PASS configurable list replaces the default")


def test_can_be_disabled(ec):
    os.environ["SEND_STRIP_HEADERS"] = ""
    msg = _draft(X_ZohoMail_Sender="Jane Döe")
    assert ec.strip_provider_headers(msg) == []
    assert msg.get("X-ZohoMail-Sender") == "Jane Döe"
    print("PASS empty SEND_STRIP_HEADERS disables stripping")


def main():
    ec = _load_email_client()
    try:
        test_default_strips_zoho_header(ec)
        test_absent_header_is_a_noop(ec)
        test_every_occurrence_removed(ec)
        test_configurable_list(ec)
        test_can_be_disabled(ec)
    finally:
        os.environ.pop("SEND_STRIP_HEADERS", None)
    print("all email strip-header tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
