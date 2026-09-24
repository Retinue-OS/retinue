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

Also covers the files Ara attaches to her own reply (conversation-push.py
--reply-attach): an image's intrinsic size is recorded at store time (the
dashboard reserves its inline preview's box with it), the turn's manifest is
consumed exactly once, every bad entry is skipped without costing the others,
and _conv_worker puts the files on the reply message itself — a files-only
reply staying text-less.

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


def _png(w: int, h: int) -> bytes:
    """A PNG signature plus an IHDR chunk header — enough for the size sniff."""
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")


def test_image_dimensions_are_recorded(wg):
    stored = wg._store_attachments("6" * 32, [
        {"filename": "chart.png", "content_type": "image/png", "data": _b64(_png(640, 480))},
        {"filename": "notes.txt", "content_type": "text/plain", "data": _b64(b"hello")},
    ])
    assert (stored[0]["width"], stored[0]["height"]) == (640, 480), stored[0]
    assert "width" not in stored[1] and "height" not in stored[1], stored[1]


def test_reply_manifest_is_consumed_once(wg, tmp: Path):
    manifest = tmp / "manifest"
    manifest.write_text("/a/one.png\n\n  /b/two.pdf  \n", encoding="utf-8")
    assert wg._read_reply_manifest(manifest) == ["/a/one.png", "/b/two.pdf"]
    assert not manifest.exists(), "a manifest must never ride on a later turn"
    assert wg._read_reply_manifest(manifest) == []  # nothing attached: common case
    assert wg._read_reply_manifest(None) == []


def test_reply_files_are_stored_and_bad_entries_skipped(wg, tmp: Path):
    cid = "7" * 32
    good = tmp / "plot.png"
    good.write_bytes(_png(100, 50))
    doc = tmp / "summary.pdf"
    doc.write_bytes(b"%PDF reply")
    empty = tmp / "empty.txt"
    empty.write_bytes(b"")
    big = tmp / "big.bin"
    big.write_bytes(b"x" * (wg.MAX_ATTACHMENT_BYTES + 1))
    fifo = tmp / "pipe"
    os.mkfifo(fifo)  # must be refused without blocking on open()
    stored = wg._store_reply_files(cid, [
        str(good), "relative/path.png", str(tmp / "missing.png"), str(tmp),
        str(empty), str(big), str(fifo), str(doc), str(good),  # last: duplicate
    ])
    assert [a["filename"] for a in stored] == ["plot.png", "summary.pdf"], stored
    png, pdf = stored
    assert png["content_type"] == "image/png" and (png["width"], png["height"]) == (100, 50)
    assert pdf["content_type"] == "application/pdf" and pdf["suffix"] == ".pdf"
    on_disk = wg.CONVERSATION_ATTACHMENTS_DIR / cid / f"{pdf['id']}.pdf"
    assert on_disk.read_bytes() == b"%PDF reply"  # a copy, independent of the source
    doc.unlink()
    assert on_disk.is_file()


def _run_worker(wg, result: dict) -> dict:
    """One dashboard-thread turn whose session returns `result`; the thread."""
    conv = wg._new_conv("user", "owner", "t", "user", "make me a chart")
    seen = {}

    def fake_send(prompt, **kwargs):
        seen.update(kwargs)
        return result

    send, lint, push = wg.send_message, wg._lint_presentation, wg._push_conv_notification
    wg.send_message = fake_send
    wg._lint_presentation = lambda text, **kw: text
    pushed = []
    wg._push_conv_notification = lambda conv, text: pushed.append(text)
    try:
        assert wg._conv_worker(conv["id"], f"conv:{conv['id']}") is True
    finally:
        wg.send_message, wg._lint_presentation, wg._push_conv_notification = send, lint, push
    assert seen.get("reply_attachments") is True, seen
    return {"conv": wg._load_conv(conv["id"]), "pushed": pushed}


def test_worker_attaches_reply_files_to_the_reply(wg, tmp: Path):
    chart = tmp / "chart.png"
    chart.write_bytes(_png(10, 10))
    out = _run_worker(wg, {"response": "Here is the chart.",
                           "reply_files": [str(chart), "not/absolute"]})
    reply = out["conv"]["messages"][-1]
    assert reply["role"] == "assistant" and reply["text"] == "Here is the chart."
    assert [a["filename"] for a in reply["attachments"]] == ["chart.png"], reply
    assert out["pushed"] == ["Here is the chart."]

    # Files only: no "(no reply)" above them, and the push names the file.
    out = _run_worker(wg, {"response": "", "reply_files": [str(chart)]})
    reply = out["conv"]["messages"][-1]
    assert reply["text"] == "" and len(reply["attachments"]) == 1, reply
    assert out["pushed"] == ["chart.png"], out["pushed"]

    # No files and no text is still the placeholder it always was.
    out = _run_worker(wg, {"response": ""})
    reply = out["conv"]["messages"][-1]
    assert reply["text"] == "(no reply)" and "attachments" not in reply, reply


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
        test_image_dimensions_are_recorded(wg)
        scratch = Path(td) / "scratch"
        scratch.mkdir()
        test_reply_manifest_is_consumed_once(wg, scratch)
        test_reply_files_are_stored_and_bad_entries_skipped(wg, scratch)
        test_worker_attaches_reply_files_to_the_reply(wg, scratch)
    print("all web-gateway attachment tests passed")


if __name__ == "__main__":
    main()
