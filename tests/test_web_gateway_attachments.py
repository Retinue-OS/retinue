#!/usr/bin/env python3
"""Checks that thread attachments are stored with a safe file extension.

_store_attachments() used to write every file as a bare uuid4 hex with no
extension, so a session reading it back (e.g. a PDF with a compressed content
stream) got mojibake instead of a rendered document — images were fine because
they're content-sniffed, PDFs are not. The fix derives a suffix from the
filename/content-type the same way the sibling _store_message_files() already
does, while keeping the "id" field (used verbatim as the download URL's path
segment, and validated against a bare-hex regex) unchanged.

Covers: the suffix is derived and appended on disk; _conv_attachment_note()
and _serve_conversation_attachment() both resolve the suffixed path; a
pre-existing attachment recorded without a "suffix" key (stored before this
fix) still resolves to its original, extensionless path.

    python3 tests/test_web_gateway_attachments.py
"""
import base64
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gateway(tmp: Path):
    """Load scripts/web-gateway.py with sandboxed state, as the other
    web-gateway tests do."""
    for var in ("RETINUE_CONVERSATION_MODELS", "RETINUE_LITELLM_URL",
                "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"):
        os.environ.pop(var, None)
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_attachments_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeHandler:
    """Just enough of BaseHTTPRequestHandler for _serve_conversation_attachment."""

    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status, message=None):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def _send_json(self, status, body):
        self.status = status
        self.wfile.write(json.dumps(body).encode())


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_suffix_derived_from_filename(wg):
    stored = wg._store_attachments("c" * 32, [
        {"filename": "invoice.pdf", "content_type": "application/pdf",
         "data": _b64(b"%PDF-1.4 fake")},
    ])
    assert len(stored) == 1, stored
    att = stored[0]
    assert att["suffix"] == ".pdf", att
    assert wg._ATT_ID_RE.fullmatch(att["id"]), att["id"]  # id itself stays bare hex
    on_disk = wg.CONVERSATION_ATTACHMENTS_DIR / ("c" * 32) / f"{att['id']}.pdf"
    assert on_disk.is_file(), on_disk
    assert on_disk.read_bytes() == b"%PDF-1.4 fake"


def test_suffix_derived_from_content_type_when_filename_has_none(wg):
    stored = wg._store_attachments("d" * 32, [
        {"filename": "photo", "content_type": "image/png", "data": _b64(b"\x89PNG fake")},
    ])
    att = stored[0]
    assert att["suffix"] == ".png", att
    on_disk = wg.CONVERSATION_ATTACHMENTS_DIR / ("d" * 32) / f"{att['id']}.png"
    assert on_disk.is_file(), on_disk


def test_unknown_type_falls_back_to_no_suffix(wg):
    """Matches pre-fix behaviour when nothing usable is available."""
    stored = wg._store_attachments("e" * 32, [
        {"filename": "blob", "content_type": "application/x-totally-unknown",
         "data": _b64(b"raw bytes")},
    ])
    att = stored[0]
    assert att["suffix"] == "", att
    on_disk = wg.CONVERSATION_ATTACHMENTS_DIR / ("e" * 32) / att["id"]
    assert on_disk.is_file(), on_disk


def test_conv_attachment_note_resolves_suffixed_path(wg):
    cid = "f" * 32
    stored = wg._store_attachments(cid, [
        {"filename": "report.pdf", "content_type": "application/pdf",
         "data": _b64(b"%PDF fake report")},
    ])
    conv = {"id": cid}
    msg = {"attachments": stored}
    note = wg._conv_attachment_note(conv, msg)
    expected_path = wg.CONVERSATION_ATTACHMENTS_DIR / cid / f"{stored[0]['id']}.pdf"
    assert str(expected_path) in note, note
    assert expected_path.is_file(), expected_path


def test_conv_attachment_note_backward_compatible_without_suffix(wg):
    """An attachment recorded before this fix carries no "suffix" key; the
    note must still point at its original, extensionless on-disk file."""
    cid = "1" * 32
    att_id = "2" * 32
    legacy_dir = wg.CONVERSATION_ATTACHMENTS_DIR / cid
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / att_id).write_bytes(b"legacy bytes, no extension")
    conv = {"id": cid}
    msg = {"attachments": [
        {"id": att_id, "filename": "old.pdf", "content_type": "application/pdf", "size": 10},
    ]}
    note = wg._conv_attachment_note(conv, msg)
    expected_path = wg.CONVERSATION_ATTACHMENTS_DIR / cid / att_id
    assert str(expected_path) in note, note
    assert f"{expected_path}.pdf" not in note


def test_serve_conversation_attachment_new_style(wg):
    cid = "3" * 32
    stored = wg._store_attachments(cid, [
        {"filename": "invoice.pdf", "content_type": "application/pdf",
         "data": _b64(b"%PDF new-style")},
    ])
    conv = {"id": cid, "messages": [{"attachments": stored}]}
    wg._save_conv(conv)
    fake = _FakeHandler()
    wg.Handler._serve_conversation_attachment(fake, cid, stored[0]["id"])
    assert fake.status == 200, fake.status
    assert fake.wfile.getvalue() == b"%PDF new-style"
    assert fake.headers.get("Content-Type") == "application/pdf"


def test_serve_conversation_attachment_legacy_style(wg):
    """A pre-fix attachment (no "suffix" metadata, bare-hex on disk) still
    serves correctly — the fix must not break already-stored files."""
    cid = "4" * 32
    att_id = "5" * 32
    legacy_dir = wg.CONVERSATION_ATTACHMENTS_DIR / cid
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / att_id).write_bytes(b"legacy png bytes")
    conv = {"id": cid, "messages": [{"attachments": [
        {"id": att_id, "filename": "old.png", "content_type": "image/png", "size": 17},
    ]}]}
    wg._save_conv(conv)
    fake = _FakeHandler()
    wg.Handler._serve_conversation_attachment(fake, cid, att_id)
    assert fake.status == 200, fake.status
    assert fake.wfile.getvalue() == b"legacy png bytes"


def main():
    with tempfile.TemporaryDirectory() as td:
        wg = _load_gateway(Path(td))
        test_suffix_derived_from_filename(wg)
        test_suffix_derived_from_content_type_when_filename_has_none(wg)
        test_unknown_type_falls_back_to_no_suffix(wg)
        test_conv_attachment_note_resolves_suffixed_path(wg)
        test_conv_attachment_note_backward_compatible_without_suffix(wg)
        test_serve_conversation_attachment_new_style(wg)
        test_serve_conversation_attachment_legacy_style(wg)
    print("all web-gateway attachment tests passed")


if __name__ == "__main__":
    main()
