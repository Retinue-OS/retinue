#!/usr/bin/env python3
"""Focused checks for the media dimension sniffer and the .meta sidecar.

`inbound_store.store_media()` sniffs an image blob's intrinsic size from its
header (PNG IHDR, GIF logical screen descriptor, JPEG first SOF, WebP
VP8/VP8L/VP8X) and persists it in a `<id>.meta` JSON sidecar; the chat surface
serves it as the attachment's width/height so clients reserve the image box
before the bytes arrive. Sniffing is best-effort by contract: garbage,
truncation and non-images yield no sidecar and never an exception.

Standalone, no third-party deps:

    python3 tests/test_media_meta.py
"""
import importlib.util
import json
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "inbound_store.py"


def _load():
    spec = importlib.util.spec_from_file_location("inbound_store", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ist = _load()


def _png(w, h):
    return (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00" + b"\x00" * 4)


def _gif(w, h, version=b"GIF89a"):
    return version + w.to_bytes(2, "little") + h.to_bytes(2, "little") + b"\x00\x00\x00"


def _jpeg(w, h, sof_marker=b"\xff\xc0", leading=b""):
    # SOI, optional leading segments, then a SOF whose payload is
    # precision(1) height(2) width(2) components(1) + 3x3 component specs.
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9
    sof = (sof_marker + (17).to_bytes(2, "big") + b"\x08"
           + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03" + b"\x00" * 9)
    return b"\xff\xd8" + app0 + leading + sof + b"\xff\xd9"


def _webp_vp8x(w, h):
    payload = (b"\x00" + b"\x00" * 3
               + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little"))
    chunk = b"VP8X" + len(payload).to_bytes(4, "little") + payload
    return b"RIFF" + (4 + len(chunk)).to_bytes(4, "little") + b"WEBP" + chunk


def _webp_vp8(w, h):
    payload = (b"\x00\x00\x00" + b"\x9d\x01\x2a"
               + w.to_bytes(2, "little") + h.to_bytes(2, "little") + b"\x00" * 4)
    return (b"RIFF" + (12 + len(payload)).to_bytes(4, "little") + b"WEBP"
            + b"VP8 " + len(payload).to_bytes(4, "little") + payload)


def _webp_vp8l(w, h):
    bits = (w - 1) | ((h - 1) << 14)
    payload = b"\x2f" + bits.to_bytes(4, "little") + b"\x00" * 8  # pad past the guard
    return (b"RIFF" + (12 + len(payload)).to_bytes(4, "little") + b"WEBP"
            + b"VP8L" + len(payload).to_bytes(4, "little") + payload)


def test_sniffer_formats():
    dims = ist._image_dimensions
    assert dims(_png(320, 420)) == (320, 420)
    assert dims(_gif(64, 48)) == (64, 48)
    assert dims(_gif(64, 48, version=b"GIF87a")) == (64, 48)
    assert dims(_jpeg(1024, 768)) == (1024, 768)
    # Progressive (SOF2), and a DHT segment before the SOF must be skipped —
    # DHT's marker (C4) sits inside the SOF range and must not be misread.
    assert dims(_jpeg(800, 600, sof_marker=b"\xff\xc2")) == (800, 600)
    dht = b"\xff\xc4" + (5).to_bytes(2, "big") + b"\x00" * 3
    assert dims(_jpeg(12, 34, leading=dht)) == (12, 34)
    assert dims(_webp_vp8x(2000, 1500)) == (2000, 1500)
    assert dims(_webp_vp8(640, 480)) == (640, 480)
    assert dims(_webp_vp8l(333, 77)) == (333, 77)
    print("PASS test_sniffer_formats")


def test_sniffer_garbage_is_none():
    dims = ist._image_dimensions
    assert dims(b"") is None
    assert dims(b"\x00" * 64) is None
    assert dims(b"hello, not an image at all - just text bytes") is None
    assert dims(b"\x89PNG\r\n\x1a\n") is None                  # truncated PNG
    assert dims(b"\xff\xd8\xff\xd9") is None                   # JPEG with no SOF
    assert dims(b"\xff\xd8\xff\xc0\x00\x01") is None           # absurd segment length
    assert dims(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 20) is None  # RIFF, not WEBP
    assert dims(_png(0, 10)) is None                           # zero dimension
    # OGG audio magic (a voice note) never matches an image sniff.
    assert dims(b"OggS" + b"\x00" * 40) is None
    print("PASS test_sniffer_garbage_is_none")


def test_store_media_meta_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        mid = ist.store_media(tmp, _png(320, 420), "image/png")
        d = ist.media_dir(tmp)
        meta = json.loads((d / (mid + ".meta")).read_text(encoding="utf-8"))
        assert meta == {"width": 320, "height": 420}
        # The blob and the .type sidecar are unchanged by the addition.
        assert (d / mid).read_bytes() == _png(320, 420)
        assert (d / (mid + ".type")).read_text(encoding="utf-8").strip() == "image/png"
        # A non-image blob gets no sidecar — absent meta means unknown,
        # exactly the pre-meta behaviour.
        mid2 = ist.store_media(tmp, b"OggS" + b"\x00" * 40, "audio/ogg")
        assert not (d / (mid2 + ".meta")).exists()
        assert ist.load_media(tmp, mid2)[1] == "audio/ogg"
    print("PASS test_store_media_meta_roundtrip")


if __name__ == "__main__":
    test_sniffer_formats()
    test_sniffer_garbage_is_none()
    test_store_media_meta_roundtrip()
    print("all media-meta tests passed")
