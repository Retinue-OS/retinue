#!/usr/bin/env python3
"""Checks that an outbound messenger attachment is a file of any type.

The push CLIs and the gateways once spoke of images only: a PDF or a video
could be forced through, but it reached the recipient under a temp-file name
and, on WhatsApp, a video arrived as a file to download. Pinned here:

- `inbound_store.materialize_outbound` writes the file under the sender's own
  (sanitized) name, states its content type, and treats an entry with neither
  name nor type as the JPEG it used to be;
- the three push CLIs take `--attach` (and `--image`, its older name) and send
  each file with its name and content type;
- each gateway's `_push` hands the channel a file with the real name, stores it
  for the ledger with that name and type, and cleans up afterwards;
- WhatsApp sends a video as a video, a PDF as a document, and falls back to a
  document when the video cannot be built.

    python3 tests/test_outbound_attachments.py
"""
import base64
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

PDF = b"%PDF-1.4 not really"
B64 = base64.b64encode(PDF).decode("ascii")


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_materialize_outbound():
    import inbound_store as ib
    with tempfile.TemporaryDirectory() as tmp:
        path, ctype, name = ib.materialize_outbound(
            {"data": B64, "filename": "../../Quarterly report.pdf"}, tmp)
        assert path.name == "Quarterly report.pdf" and name == path.name
        assert Path(tmp) in path.parents, "the name must not steer the path"
        assert ctype == "application/pdf" and path.read_bytes() == PDF
        ib.discard_outbound(path)
        assert not path.exists() and not path.parent.exists()

        # Declared type and no name: the type wins and supplies an extension.
        path, ctype, name = ib.materialize_outbound(
            {"data": B64, "content_type": "video/mp4; codecs=avc1"}, tmp)
        assert (path.suffix, ctype, name) == (".mp4", "video/mp4", None)
        ib.discard_outbound(path)

        # Neither: what such an entry always was.
        path, ctype, name = ib.materialize_outbound({"data": B64}, tmp)
        assert (path.name, ctype, name) == ("image.jpg", "image/jpeg", None)
        ib.discard_outbound(path)

        # A name whose type nothing knows is still a file.
        path, ctype, _ = ib.materialize_outbound({"data": B64, "filename": "x.qqq7"}, tmp)
        assert ctype == "application/octet-stream"
        ib.discard_outbound(path)
        assert os.listdir(tmp) == []

        for bad in ("nope", {}, {"data": "not!!base64"}, {"data": 3}):
            try:
                ib.materialize_outbound(bad, tmp)
            except ValueError:
                continue
            raise AssertionError(f"accepted {bad!r}")

        # Any other temp file of a send is simply unlinked, its directory kept.
        other = Path(tmp) / "voice.ogg"
        other.write_bytes(b"x")
        ib.discard_outbound(other)
        assert not other.exists() and Path(tmp).is_dir()
    print("ok: materialize_outbound names, types and cleans up")


def test_push_clis_attach_any_file():
    import json
    import urllib.request
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "report.pdf"
        pdf.write_bytes(PDF)
        clip = Path(tmp) / "clip.mp4"
        clip.write_bytes(b"\x00\x00")
        for cli in ("signal", "whatsapp", "telegram"):
            mod = _load(f"{cli}_push_under_test", f"{cli}-push.py")
            sent = []

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def read(self):
                    return b'{"status": "sent", "recipient": "x"}'

            def _urlopen(request, timeout=None, _sent=sent):
                _sent.append(json.loads(request.data.decode("utf-8")))
                return _Resp()

            real_urlopen, real_argv = urllib.request.urlopen, sys.argv
            urllib.request.urlopen = _urlopen
            sys.argv = [f"{cli}-push.py", "--recipient", "x",
                        "--attach", str(pdf), "--image", str(clip)]
            try:
                assert mod.main() == 0
            finally:
                urllib.request.urlopen, sys.argv = real_urlopen, real_argv
            assert sent[0]["images"] == [
                {"filename": "report.pdf", "data": B64, "content_type": "application/pdf"},
                {"filename": "clip.mp4", "data": base64.b64encode(b"\x00\x00").decode(),
                 "content_type": "video/mp4"},
            ], (cli, sent)
    print("ok: push CLIs attach any file, with name and type")


def _gateway(channel: str, tmp: str):
    upper = channel.upper()
    os.environ[f"{upper}_PENDING_SENDS_DIR"] = str(Path(tmp) / "pending")
    os.environ[f"{upper}_DATA_DIR"] = str(Path(tmp) / "data")
    os.environ[f"{upper}_TMP_DIR"] = str(Path(tmp) / "tmp")
    os.environ[f"{upper}_ATTACHMENTS_DIR"] = str(Path(tmp) / "att")
    os.environ["PIPER_DATA_DIR"] = str(Path(tmp) / "models")
    os.environ[f"{upper}_GATEWAY_MODE"] = "inbox"
    # The Signal gateway imports langdetect at load; nothing here detects.
    if "langdetect" not in sys.modules:
        stub = types.ModuleType("langdetect")
        stub.detect = lambda *a, **k: "en"
        stub.detect_langs = lambda *a, **k: []
        stub.LangDetectException = type("LangDetectException", (Exception,), {})
        sys.modules["langdetect"] = stub
    try:
        return _load(f"{channel}_gateway_attachments_under_test", f"{channel}-gateway.py")
    finally:
        os.environ.pop(f"{upper}_GATEWAY_MODE", None)


def test_gateways_send_the_named_file():
    entries = [{"data": B64, "filename": "report.pdf", "content_type": "application/pdf"}]
    for channel, sender in (("signal", "_signal_send"), ("whatsapp", "_wa_send"),
                            ("telegram", "_tg_send")):
        with tempfile.TemporaryDirectory() as tmp:
            gw = _gateway(channel, tmp)
            seen, stored = [], []

            def _send(recipient, *args, _seen=seen, **kw):
                paths = kw.get("attachments") or kw.get("media_paths") or []
                _seen.extend((Path(p).name, Path(p).read_bytes()) for p in paths)
                _seen.append(list(paths))
                return "id1", 1.0

            def _store(data, content_type, file_name=None, _stored=stored):
                _stored.append((data, content_type, file_name))
                return "urn:retinue:media:x:" + "ab" * 16

            setattr(gw, sender, _send)
            gw._store_media_ref = _store
            result = gw._push("+15551234567", "see attached", images=entries, voice=False)
            assert seen[0] == ("report.pdf", PDF), (channel, seen)
            assert stored == [(PDF, "application/pdf", "report.pdf")], (channel, stored)
            assert result[2] == ["urn:retinue:media:x:" + "ab" * 16]
            assert not any(Path(p).exists() or Path(p).parent.exists() for p in seen[-1]), \
                f"{channel}: the sent file was left behind"
    print("ok: gateways send, store and clean up the named file")


def test_whatsapp_presents_each_kind():
    with tempfile.TemporaryDirectory() as tmp:
        gw = _gateway("whatsapp", tmp)

        class _Client:
            def __init__(self, video_ok=True):
                self.video_ok = video_ok
                self.built = []

            def build_image_message(self, data, caption=""):
                self.built.append(("image", caption))
                return "img"

            def build_video_message(self, data, caption=""):
                if not self.video_ok:
                    raise RuntimeError("ffmpeg: unreadable")
                self.built.append(("video", caption))
                return "vid"

            def build_document_message(self, data, filename=None, caption="", mimetype=None):
                self.built.append(("document", filename, mimetype, caption))
                return "doc"

            def send_message(self, jid, text=None, message=None):
                return None

        files = {}
        for name in ("a.png", "b.mp4", "c.pdf"):
            files[name] = Path(tmp) / name
            files[name].write_bytes(b"x")

        gw._wa_client = client = _Client()
        for name in ("a.png", "b.mp4", "c.pdf"):
            gw._run_send_op("15551234567@s.whatsapp.net",
                            {"kind": "media", "path": files[name], "caption": "hi"})
        assert client.built == [("image", "hi"), ("video", "hi"),
                                ("document", "c.pdf", "application/pdf", "hi")], client.built

        gw._wa_client = client = _Client(video_ok=False)
        gw._run_send_op("15551234567@s.whatsapp.net",
                        {"kind": "media", "path": files["b.mp4"], "caption": ""})
        assert client.built == [("document", "b.mp4", "video/mp4", "")], client.built
    print("ok: whatsapp sends a video as a video, a PDF as a document")


if __name__ == "__main__":
    test_materialize_outbound()
    test_push_clis_attach_any_file()
    test_gateways_send_the_named_file()
    test_whatsapp_presents_each_kind()
    print("all outbound-attachment tests passed")
