#!/usr/bin/env python3
"""Checks which parts `email_client.py` counts as attachments.

The old filter took `Content-Disposition: inline` to mean "decoration" and
skipped every such part, so a sender that attaches the actual document inline
made `read`/`list`/`fetch-attachment` all report zero attachments — seen with
a Just Eat invoice and a Schubiger QR-bill, both of which had to be recovered
by pulling the raw MIME by hand. The disposition does not carry that meaning;
a `Content-ID` does, because that is how an HTML body addresses an image it
embeds. These tests pin the distinction down to the part level.

    python3 tests/test_email_inline_attachments.py
"""
import importlib.util
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


def _mail(*parts):
    """An HTML mail carrying the given (payload, maintype, subtype, kwargs)."""
    msg = EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.org"
    msg["Subject"] = "Test"
    msg.set_content("body")
    msg.add_alternative("<p>body</p>", subtype="html")
    for payload, maintype, subtype, kwargs in parts:
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, **kwargs)
    return msg


def _names(ec, msg):
    return [p.get_filename() for _, p in ec._iter_attachments(msg)]


def test_plain_attachment(ec):
    msg = _mail((b"%PDF-", "application", "pdf", {"filename": "invoice.pdf"}))
    assert _names(ec, msg) == ["invoice.pdf"], _names(ec, msg)
    print("PASS disposition=attachment is surfaced")


def test_inline_document_is_surfaced(ec):
    """The regression: an inline PDF with no Content-ID is the payload."""
    msg = _mail((b"%PDF-", "application", "pdf",
                 {"filename": "Attachment1.pdf", "disposition": "inline"}))
    assert _names(ec, msg) == ["Attachment1.pdf"], _names(ec, msg)
    print("PASS inline document without Content-ID is surfaced")


def test_inline_image_with_cid_is_skipped(ec):
    """An image the HTML body embeds stays hidden, as before."""
    msg = _mail((b"\x89PNG", "image", "png",
                 {"filename": "logo.png", "disposition": "inline",
                  "cid": "<logo@example.com>"}))
    assert _names(ec, msg) == [], _names(ec, msg)
    print("PASS inline image with Content-ID is skipped")


def test_mixed_mail_yields_only_the_document(ec):
    """Numbering must stay dense: the embedded logo takes no index."""
    msg = _mail(
        (b"\x89PNG", "image", "png",
         {"filename": "logo.png", "disposition": "inline",
          "cid": "<logo@example.com>"}),
        (b"%PDF-", "application", "pdf",
         {"filename": "invoice.pdf", "disposition": "inline"}),
    )
    found = list(ec._iter_attachments(msg))
    assert [i for i, _ in found] == [1], found
    assert [p.get_filename() for _, p in found] == ["invoice.pdf"], found
    print("PASS embedded logo consumes no attachment index")


def test_unnamed_part_is_not_an_attachment(ec):
    """A part with neither filename nor attachment disposition is body."""
    msg = EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.org"
    msg["Subject"] = "Test"
    msg.set_content("body")
    msg.add_alternative("<p>body</p>", subtype="html")
    assert _names(ec, msg) == [], _names(ec, msg)
    print("PASS unnamed body parts are not attachments")


def main():
    ec = _load_email_client()
    test_plain_attachment(ec)
    test_inline_document_is_surfaced(ec)
    test_inline_image_with_cid_is_skipped(ec)
    test_mixed_mail_yields_only_the_document(ec)
    test_unnamed_part_is_not_an_attachment(ec)
    print("all inline-attachment tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
