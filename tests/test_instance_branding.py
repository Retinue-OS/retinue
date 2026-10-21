#!/usr/bin/env python3
"""Checks for the per-instance dashboard branding (scripts/instance_branding.py).

What these pin down:

- unset variables change nothing — every byte of the stock manifest, pages and
  icons is served as baked;
- a name reaches the manifest (short_name = label, name = "Retinue (label)")
  and the page title, and only the first, brand-bearing <title>;
- a colour recolours every stock icon into a valid PNG of the same size whose
  accent pixels are the new colour and whose background stays untouched —
  i.e. the decoder really handles the filter types the shipped icons use;
- an invalid colour, and the stock colour itself, count as "no colour";
- the signature moves with either variable, so the service-worker cache
  name moves too.

    python3 tests/test_instance_branding.py
"""
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import instance_branding as ib  # noqa: E402

WEBAPP = REPO_ROOT / "webapp"
ICONS = ["icon-192.png", "icon-512.png", "icon-maskable-512.png"]


def _env(name="", color=""):
    os.environ["RETINUE_INSTANCE_NAME"] = name
    os.environ["RETINUE_INSTANCE_COLOR"] = color


def test_unbranded_is_identity():
    _env()
    assert not ib.branded()
    assert ib.display_name() == "Retinue"
    manifest = (WEBAPP / "manifest.webmanifest").read_bytes()
    assert ib.brand_manifest(manifest) == manifest
    page = (WEBAPP / "index.html").read_bytes()
    assert ib.brand_html(page) == page
    icon = (WEBAPP / "icons" / ICONS[0]).read_bytes()
    assert ib.recolor_png(icon) == icon
    print("ok: unset branding serves the baked files unchanged")


def test_name_reaches_manifest_and_title():
    _env(name="Admin")
    data = json.loads(ib.brand_manifest((WEBAPP / "manifest.webmanifest").read_bytes()))
    assert data["short_name"] == "Admin"
    assert data["name"] == "Retinue (Admin)"
    assert data["icons"], "the rest of the manifest survives"
    page = ib.brand_html((WEBAPP / "news.html").read_bytes()).decode()
    assert "<title>Retinue (Admin) — News</title>" in page
    print("ok: the name reaches the manifest and the page title")


def test_color_parsing():
    for raw, want in [("#f59e0b", (0xF5, 0x9E, 0x0B)), ("f59e0b", (0xF5, 0x9E, 0x0B)),
                      ("#f90", (0xFF, 0x99, 0x00)), ("orange", None), ("#12345", None),
                      ("#6ea8fe", None), ("", None)]:
        _env(color=raw)
        assert ib.accent_color() == want, (raw, ib.accent_color())
    print("ok: colours parse; invalid and stock colours count as none")


def test_recolor_every_stock_icon():
    _env(color="#f59e0b")
    for name in ICONS:
        raw = (WEBAPP / "icons" / name).read_bytes()
        w, h, before = ib._decode_rgb_png(raw)
        out = ib.recolor_png(raw)
        assert out != raw
        w2, h2, after = ib._decode_rgb_png(out)
        assert (w2, h2) == (w, h)
        colors = {after[i:i + 3] for i in range(0, len(after), 3)}
        assert bytes(ib.STOCK_BACKGROUND) in colors, name
        assert bytes((0xF5, 0x9E, 0x0B)) in colors, name
        assert bytes(ib.STOCK_ACCENT) not in colors, name
        # Background pixels stay where they were.
        for i in range(0, len(before), 3):
            if before[i:i + 3] == bytes(ib.STOCK_BACKGROUND):
                assert after[i:i + 3] == bytes(ib.STOCK_BACKGROUND)
    assert ib.recolor_png(b"not a png") == b"not a png"
    print("ok: every stock icon recolours into a valid PNG")


def test_signature_moves():
    _env()
    a = ib.signature()
    _env(name="Admin")
    b = ib.signature()
    _env(name="Admin", color="#f59e0b")
    c = ib.signature()
    assert len({a, b, c}) == 3
    print("ok: the signature moves with name and colour")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    _env()
