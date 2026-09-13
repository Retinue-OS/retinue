#!/usr/bin/env python3
"""Checks that HTML->text rendering does not silently drop link targets.

Issue #174: `_HTMLTextExtractor.handle_starttag` discarded `attrs` entirely,
so every `<a href>` vanished on the way to plain text — an e-mail whose only
call to action was a link ("Rechnungskopie einsehen") came back with the
anchor text but no way to act on it, and nothing signalled that anything was
lost. This bites hardest on HTML-only senders (most transactional mail),
because `_body_text` only falls back to rendering HTML when there is no
genuine `text/plain` part.

These tests pin down: the target is folded into the rendered text as
``label <url>``, the same target is available separately via
`_render_html()`'s links list (what `read --uid ...` surfaces as its `links`
field), and the "redundant" cases (anchor text already is the URL, a
mailto:/tel: repeating the visible address/number, no links at all) stay
exactly as before — quiet, not padded with noise. Also a follow-up review
finding on the same PR: a mailto:'s query string (?subject=/body=/cc=) is
part of the action and must survive even when the address alone repeats the
visible text.

    python3 tests/test_email_html_links.py
"""
import importlib.util
import sys
from email import policy
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


def _html_only_message(html):
    """An HTML-only mail (no text/plain alternative) — the case that bites
    hardest, per the issue: `_body_text` has nothing else to fall back to."""
    msg = EmailMessage(policy=policy.default)
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.org"
    msg["Subject"] = "Test"
    msg.set_content(html, subtype="html")
    return msg


def test_link_only_call_to_action_survives(ec):
    """The exact reproduction from the issue."""
    html = ('<p>Hallo</p><p><a href="https://portal.example.com/doc/abc123">'
            'Rechnungskopie einsehen</a></p>')
    text = ec._html_to_text(html)
    assert "https://portal.example.com/doc/abc123" in text, text
    assert text.startswith("Hallo\n\nRechnungskopie einsehen"), text

    # And the same target is available as structured data, for `read`'s
    # `links` field, without needing to scrape it back out of the text.
    rendered_text, links = ec._render_html(html)
    assert rendered_text == text
    assert links == [{"text": "Rechnungskopie einsehen",
                       "url": "https://portal.example.com/doc/abc123"}], links

    # And it survives the full _body_text() path an HTML-only sender hits.
    msg = _html_only_message(html)
    body = ec._body_text(msg)
    assert "https://portal.example.com/doc/abc123" in body, body
    print("PASS link-only call to action keeps its URL")


def test_anchor_text_equal_to_href_is_not_padded(ec):
    """A link whose visible text already *is* the URL needs no annotation."""
    html = '<p>See <a href="https://example.com/x">https://example.com/x</a></p>'
    text, links = ec._render_html(html)
    assert text == "See https://example.com/x", text
    assert text.count("https://example.com/x") == 1, text
    assert links == [], links
    print("PASS anchor text equal to its href is not duplicated")


def test_mailto_repeating_the_address_is_not_padded(ec):
    html = '<p>Contact <a href="mailto:jane@example.com">jane@example.com</a></p>'
    text, links = ec._render_html(html)
    assert "mailto:" not in text, text
    assert links == [], links
    print("PASS mailto: repeating the visible address is not padded")


def test_mailto_with_distinct_text_is_kept(ec):
    """A mailto: link is still a link when its text doesn't already say so."""
    html = '<p><a href="mailto:jane@example.com">Email Jane</a></p>'
    text, links = ec._render_html(html)
    assert "mailto:jane@example.com" in text, text
    assert links == [{"text": "Email Jane", "url": "mailto:jane@example.com"}], links
    print("PASS mailto: with distinct visible text is kept")


def test_mailto_with_query_is_kept_even_if_address_repeats(ec):
    """A mailto: query is part of the action, not decoration.

    `mailto:jane@example.com?subject=Invoice` with visible text
    `jane@example.com` used to fall into the "address repeats the visible
    text" suppression: the query was stripped before the comparison, matched,
    and the whole target — subject line included — was dropped. Only a
    mailto: with *no* query left to lose is redundant with a repeated
    address.
    """
    html = ('<p>Contact <a href="mailto:jane@example.com?subject=Invoice">'
            'jane@example.com</a></p>')
    text, links = ec._render_html(html)
    assert "mailto:jane@example.com?subject=Invoice" in text, text
    assert links == [{"text": "jane@example.com",
                       "url": "mailto:jane@example.com?subject=Invoice"}], links
    print("PASS mailto: with a query is kept even when the address repeats")


def test_no_links_stays_quiet(ec):
    """A body with no links at all gets no links list and no stray markup."""
    html = "<p>Hallo, keine Links hier.</p>"
    text, links = ec._render_html(html)
    assert text == "Hallo, keine Links hier.", text
    assert links == [], links

    msg = _html_only_message(html)
    assert ec._body_text(msg) == text
    print("PASS body with no links produces no link noise")


def test_empty_anchor_is_skipped(ec):
    """An anchor with no visible text has nothing to hang a label on."""
    html = '<p><a href="https://example.com"></a>text</p>'
    text, links = ec._render_html(html)
    assert "https://example.com" not in text, text
    assert links == [], links
    print("PASS empty anchor is skipped")


def main():
    ec = _load_email_client()
    test_link_only_call_to_action_survives(ec)
    test_anchor_text_equal_to_href_is_not_padded(ec)
    test_mailto_repeating_the_address_is_not_padded(ec)
    test_mailto_with_distinct_text_is_kept(ec)
    test_mailto_with_query_is_kept_even_if_address_repeats(ec)
    test_no_links_stays_quiet(ec)
    test_empty_anchor_is_skipped(ec)
    print("all email HTML-link tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
