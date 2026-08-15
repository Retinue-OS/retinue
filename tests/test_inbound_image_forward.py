#!/usr/bin/env python3
"""Checks that inbound messenger images reach the agent.

Covers the whole forwarding chain without any bridge library or HTTP server:

  * web-gateway: POST /message ``files`` are materialized to disk with a
    server-generated name and their paths appended to the prompt.
  * whatsapp-gateway: an image message is detected (real sub-message, not an
    empty proto), downloaded, and forwarded as a ``files`` payload.
  * signal-gateway: attachments are partitioned by contentType — ``image/*``
    becomes a ``files`` payload, audio keeps the voice-note transcription path.
  * telegram-gateway: a downloaded inbound image becomes a ``files`` payload.
  * all three gateways: ``_forward_to_inbox`` puts the files into the POST
    /message body alongside the triage prompt.

    python3 tests/test_inbound_image_forward.py
"""
import base64
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-image-data"
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


def _load(module_name: str, script: str):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_langdetect():
    if "langdetect" not in sys.modules:
        stub = types.ModuleType("langdetect")
        stub.detect = lambda *a, **k: "en"
        stub.detect_langs = lambda *a, **k: []
        stub.LangDetectException = type("LangDetectException", (Exception,), {})
        sys.modules["langdetect"] = stub


def _capture_post(module):
    """Replace module.requests.post with a recorder returning HTTP 202."""
    calls = []

    class _Resp:
        status_code = 202

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "pending"}

    fake_requests = types.SimpleNamespace(
        post=lambda url, json=None, timeout=None: calls.append({"url": url, "json": json}) or _Resp(),
        exceptions=module.requests.exceptions,
    )
    module.requests = fake_requests
    return calls


# ── web-gateway ───────────────────────────────────────────────────────────────

def _load_web_gateway(tmp: Path):
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["MESSAGE_FILES_DIR"] = str(tmp / "message-files")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    return _load("web_gateway_files_under_test", "web-gateway.py")


def test_web_gateway_stores_message_files():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_web_gateway(Path(tmp))
        stored = wg._store_message_files([
            {"filename": "photo.png", "content_type": "image/png", "data": PNG_B64},
            {"filename": "../../etc/passwd", "content_type": "image/jpeg", "data": PNG_B64},
            {"filename": "noext", "content_type": "image/png", "data": PNG_B64},
            {"filename": "bad.png", "content_type": "image/png", "data": "!!not-base64!!"},
            "not-a-dict",
            {"filename": "nodata.png", "content_type": "image/png"},
        ])
        assert len(stored) == 3, stored
        for entry in stored:
            path = Path(entry["path"])
            # Every file lands inside MESSAGE_FILES_DIR under a server name —
            # an untrusted filename never becomes a path component.
            assert path.parent == wg.MESSAGE_FILES_DIR, path
            assert path.read_bytes() == PNG_BYTES
            assert entry["size"] == len(PNG_BYTES)
        # Extension survives from the name when plain, else from content type.
        assert stored[0]["path"].endswith(".png")
        assert stored[2]["path"].endswith(".png")
        assert "passwd" not in stored[1]["path"]
    print("ok: web gateway stores message files under server-generated names")


def test_web_gateway_note_and_size_cap():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_web_gateway(Path(tmp))
        assert wg._message_files_note([]) == ""
        stored = wg._store_message_files(
            [{"filename": "a.png", "content_type": "image/png", "data": PNG_B64}])
        note = wg._message_files_note(stored)
        assert stored[0]["path"] in note
        assert "image/png" in note
        # Oversized files are skipped, not truncated.
        wg.MAX_ATTACHMENT_BYTES = 4
        assert wg._store_message_files(
            [{"filename": "big.png", "content_type": "image/png", "data": PNG_B64}]) == []
    print("ok: web gateway file note lists paths; oversized files are dropped")


# ── whatsapp-gateway ──────────────────────────────────────────────────────────

def _load_whatsapp_gateway(tmp: Path):
    os.environ["WHATSAPP_DATA_DIR"] = str(tmp / "data")
    os.environ["WHATSAPP_TMP_DIR"] = str(tmp / "tmp")
    os.environ["WHATSAPP_PENDING_SENDS_DIR"] = str(tmp / "pending")
    os.environ["INBOUND_STORE_DIR"] = str(tmp / "inbound")
    os.environ["WHATSAPP_REPLY_TOKENS_DIR"] = str(tmp / "reply-tokens")
    return _load("whatsapp_gateway_images_under_test", "whatsapp-gateway.py")


class _FakeProtoMessage:
    """Mimics a neonize protobuf message: sub-messages exist as attributes even
    when unset; HasField is the presence oracle."""

    def __init__(self, set_fields: dict):
        self._set = set_fields
        self.imageMessage = set_fields.get(
            "imageMessage", types.SimpleNamespace(URL="", mimetype=""))

    def HasField(self, name: str) -> bool:
        if name not in ("imageMessage",):
            raise ValueError(name)
        return name in self._set


def test_whatsapp_image_detection():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(Path(tmp))
        image = types.SimpleNamespace(URL="https://mmg.whatsapp.net/x", mimetype="image/jpeg")
        assert wg._extract_image(_FakeProtoMessage({"imageMessage": image})) is not None
        # An unset (empty-proto) image field must NOT count as an image.
        assert wg._extract_image(_FakeProtoMessage({})) is None
        assert wg._extract_image(None) is None
        # Without HasField, presence falls back to the download coordinates.
        assert wg._extract_image(
            types.SimpleNamespace(imageMessage=image)) is not None
        assert wg._extract_image(
            types.SimpleNamespace(imageMessage=types.SimpleNamespace(URL="", mimetype=""))) is None
    print("ok: whatsapp image detection distinguishes real images from empty protos")


def test_whatsapp_inbound_image_files():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(Path(tmp))
        image = types.SimpleNamespace(URL="https://mmg.whatsapp.net/x", mimetype="image/png")
        message = types.SimpleNamespace(imageMessage=image)

        media = Path(tmp) / "downloaded"
        media.write_bytes(PNG_BYTES)
        wg._download_media = lambda m: media
        files = wg._inbound_image_files(message)
        assert len(files) == 1, files
        assert files[0]["content_type"] == "image/png"
        assert files[0]["filename"].endswith(".png")
        assert base64.b64decode(files[0]["data"]) == PNG_BYTES
        assert not media.exists()  # temp file cleaned up

        # Oversized image → message forwarded without it.
        media.write_bytes(PNG_BYTES)
        wg.MAX_INBOUND_FILE_BYTES = 4
        assert wg._inbound_image_files(message) == []
        # No image in the message → no download attempted.
        assert wg._inbound_image_files(types.SimpleNamespace()) == []
    print("ok: whatsapp inbound image becomes a files payload")


def test_whatsapp_forward_includes_files():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(Path(tmp))
        calls = _capture_post(wg)
        files = [{"filename": "whatsapp-image.jpg", "content_type": "image/jpeg", "data": PNG_B64}]
        wg._forward_to_inbox("look at this", "en", "+15551234567",
                             origin="+15551234567@s.whatsapp.net", files=files)
        assert len(calls) == 1, calls
        payload = calls[0]["json"]
        assert payload["files"] == files
        assert "1 attached image(s)" in payload["message"]
        # A message without images must not carry the key at all.
        wg._forward_to_inbox("plain text", "en", "+15551234567")
        assert "files" not in calls[1]["json"]
    print("ok: whatsapp forward carries files in the POST /message payload")


# ── signal-gateway ────────────────────────────────────────────────────────────

def _load_signal_gateway(tmp: Path):
    _stub_langdetect()
    os.environ["PIPER_DATA_DIR"] = str(tmp / "models")
    os.environ["SIGNAL_ATTACHMENTS_DIR"] = str(tmp / "attachments")
    os.environ["SIGNAL_DATA_DIR"] = str(tmp / "signal-data")
    os.environ["INBOUND_STORE_DIR"] = str(tmp / "inbound")
    os.environ["SIGNAL_REPLY_TOKENS_DIR"] = str(tmp / "reply-tokens")
    os.environ.setdefault("SIGNAL_ACCOUNT", "+15550000000")
    return _load("signal_gateway_images_under_test", "signal-gateway.py")


def _signal_event(attachments):
    return {"envelope": {"dataMessage": {"message": "", "attachments": attachments}}}


def test_signal_split_attachments():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(Path(tmp))
        att_dir = Path(tmp) / "attachments"
        image_file = att_dir / "img.jpg"
        image_file.write_bytes(PNG_BYTES)
        voice_file = att_dir / "note.ogg"
        voice_file.write_bytes(b"fake-ogg")

        voice, images = sg._split_attachments(_signal_event([
            {"contentType": "image/jpeg", "file": str(image_file)},
            {"contentType": "audio/ogg", "file": str(voice_file)},
            {"contentType": "image/png", "file": str(att_dir / "missing.png")},
        ]))
        assert voice == voice_file
        assert len(images) == 1, images
        assert images[0]["content_type"] == "image/jpeg"
        assert images[0]["filename"].endswith(".jpg")
        assert base64.b64decode(images[0]["data"]) == PNG_BYTES

        # Legacy: an attachment with no contentType keeps the voice-note path.
        voice, images = sg._split_attachments(_signal_event([
            {"file": str(voice_file)},
        ]))
        assert voice == voice_file and images == []

        # Oversized image → dropped from the payload.
        sg.MAX_INBOUND_FILE_BYTES = 4
        voice, images = sg._split_attachments(_signal_event([
            {"contentType": "image/jpeg", "file": str(image_file)},
        ]))
        assert voice is None and images == []
    print("ok: signal attachments split into voice vs. image payloads")


def test_signal_forward_includes_files():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(Path(tmp))
        calls = _capture_post(sg)
        files = [{"filename": "signal-image.jpg", "content_type": "image/jpeg", "data": PNG_B64}]
        sg._forward_to_inbox("check this out", "en", "+15551234567", files=files)
        assert len(calls) == 1, calls
        payload = calls[0]["json"]
        assert payload["files"] == files
        assert "1 attached image(s)" in payload["message"]
        sg._forward_to_inbox("plain", "en", "+15551234567")
        assert "files" not in calls[1]["json"]
    print("ok: signal forward carries files in the POST /message payload")


# ── telegram-gateway ──────────────────────────────────────────────────────────

def _load_telegram_gateway(tmp: Path):
    _stub_langdetect()
    os.environ["TELEGRAM_TMP_DIR"] = str(tmp / "tmp")
    os.environ["TELEGRAM_DATA_DIR"] = str(tmp / "data")
    os.environ["TELEGRAM_PENDING_SENDS_DIR"] = str(tmp / "pending")
    os.environ["INBOUND_STORE_DIR"] = str(tmp / "inbound")
    os.environ["TELEGRAM_REPLY_TOKENS_DIR"] = str(tmp / "reply-tokens")
    return _load("telegram_gateway_images_under_test", "telegram-gateway.py")


def test_telegram_inbound_image_files():
    with tempfile.TemporaryDirectory() as tmp:
        tg = _load_telegram_gateway(Path(tmp))
        image = Path(tmp) / "tg-img"
        image.write_bytes(PNG_BYTES)
        files = tg._inbound_image_files(str(image), "image/png")
        assert len(files) == 1, files
        assert files[0]["content_type"] == "image/png"
        assert files[0]["filename"].endswith(".png")
        assert base64.b64decode(files[0]["data"]) == PNG_BYTES
        assert not image.exists()  # temp file cleaned up

        assert tg._inbound_image_files(None, None) == []
        image.write_bytes(PNG_BYTES)
        tg.MAX_INBOUND_FILE_BYTES = 4
        assert tg._inbound_image_files(str(image), "image/png") == []
    print("ok: telegram inbound image becomes a files payload")


def test_telegram_forward_includes_files():
    with tempfile.TemporaryDirectory() as tmp:
        tg = _load_telegram_gateway(Path(tmp))
        calls = _capture_post(tg)
        files = [{"filename": "telegram-image.png", "content_type": "image/png", "data": PNG_B64}]
        tg._forward_to_inbox("see attached", "en", "12345", files=files)
        assert len(calls) == 1, calls
        payload = calls[0]["json"]
        assert payload["files"] == files
        assert "1 attached image(s)" in payload["message"]
        tg._forward_to_inbox("plain", "en", "12345")
        assert "files" not in calls[1]["json"]
    print("ok: telegram forward carries files in the POST /message payload")


def main():
    test_web_gateway_stores_message_files()
    test_web_gateway_note_and_size_cap()
    test_whatsapp_image_detection()
    test_whatsapp_inbound_image_files()
    test_whatsapp_forward_includes_files()
    test_signal_split_attachments()
    test_signal_forward_includes_files()
    test_telegram_inbound_image_files()
    test_telegram_forward_includes_files()
    print("\nAll inbound-image forwarding checks passed.")


if __name__ == "__main__":
    main()
