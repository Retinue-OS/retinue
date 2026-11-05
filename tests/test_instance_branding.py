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
- a colour also becomes the dashboard accent: the --accent definition and
  every rgba() tint of the stock accent, in styles.css and all components,
  leaving no stock accent behind;
- a label is escaped into the title, never served as markup;
- an invalid colour, and the stock colour itself, count as "no colour";
- the signature moves with either variable, so the service-worker cache
  name moves too;
- the dashboard's stored choices (Settings › Instance, RETINUE_INSTANCE_FILE)
  win over the variables while set, hand back to them when cleared, are
  validated (one short line; a real colour), written atomically and re-read
  when the file changes.

    python3 tests/test_instance_branding.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# The stored choices live in a scratch file here, never in the real one.
_TMP = Path(tempfile.mkdtemp(prefix="instance-branding-test-"))
os.environ["RETINUE_INSTANCE_FILE"] = str(_TMP / "instance.json")

import instance_branding as ib  # noqa: E402

WEBAPP = REPO_ROOT / "webapp"
ICONS = ["icon-192.png", "icon-512.png", "icon-maskable-512.png"]


def _env(name="", color=""):
    os.environ["RETINUE_INSTANCE_NAME"] = name
    os.environ["RETINUE_INSTANCE_COLOR"] = color
    # The variables are the defaults: a test of them starts without stored choices.
    try:
        ib.settings_path().unlink()
    except FileNotFoundError:
        pass


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
    # The gateway's generated pages (_send_html) carry the same brand.
    for raw, want in [("<title>Retinue — Pending Sends</title>",
                       "<title>Retinue (Admin) — Pending Sends</title>"),
                      ("<title>Retinue Conversation</title>",
                       "<title>Retinue (Admin) Conversation</title>")]:
        assert ib.brand_html(raw.encode()).decode() == want, raw
    print("ok: the name reaches the manifest and the page title")


def test_name_is_escaped_in_titles():
    _env(name="</title><script>alert(1)</script>")
    page = ib.brand_html(b"<title>Retinue \xe2\x80\x94 News</title>").decode()
    assert "<script>" not in page, page
    assert page.startswith("<title>Retinue (&lt;/title&gt;&lt;script&gt;"), page
    # JSON escapes on its own: the manifest keeps the label verbatim.
    data = json.loads(ib.brand_manifest((WEBAPP / "manifest.webmanifest").read_bytes()))
    assert data["short_name"] == "</title><script>alert(1)</script>"
    print("ok: a label is text, never markup, in a page title")


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


def test_color_reaches_the_stylesheets():
    _env(color="#f59e0b")
    css = ib.brand_css((WEBAPP / "styles.css").read_bytes()).decode()
    assert "--accent: #f59e0b;" in css
    for path in [WEBAPP / "styles.css", *sorted((WEBAPP / "components").glob("*.js"))]:
        out = ib.brand_css(path.read_bytes()).decode()
        assert not ib._ACCENT_RGB_RE.search(out), path.name
        assert not ib._ACCENT_DEF_RE.search(out), path.name
    tints = ib.brand_css(b".x { background: rgba(110, 168, 254, .2); }")
    assert tints == b".x { background: rgba(245, 158, 11, .2); }"
    _env()
    assert ib.brand_css(b"rgba(110, 168, 254, .2)") == b"rgba(110, 168, 254, .2)"
    print("ok: the colour becomes the dashboard accent")


def test_signature_moves():
    _env()
    a = ib.signature()
    _env(name="Admin")
    b = ib.signature()
    _env(name="Admin", color="#f59e0b")
    c = ib.signature()
    assert len({a, b, c}) == 3
    print("ok: the signature moves with name and colour")


def test_stored_choices_win_over_the_variables():
    _env(name="Env", color="#112233")
    assert ib.status() == {
        "name": "", "color": "",
        "defaults": {"name": "Env", "color": "#112233"},
        "effective": {"name": "Env", "color": "#112233", "display_name": "Retinue (Env)"},
        "stock_color": "#6ea8fe", "max_name_length": 40,
    }
    before = ib.signature()
    # The dashboard's name wins; the colour it has not set stays the variable's.
    st = ib.write_setting(name="  Admin  ")
    assert st["name"] == "Admin" and st["effective"]["name"] == "Admin"
    assert st["effective"]["color"] == "#112233"
    assert ib.instance_name() == "Admin" and ib.display_name() == "Retinue (Admin)"
    data = json.loads(ib.brand_manifest((WEBAPP / "manifest.webmanifest").read_bytes()))
    assert data["short_name"] == "Admin"
    assert ib.signature() != before
    # The colour, in either spelling, normalised; the stock colour counts as none.
    st = ib.write_setting(color="F90")
    assert st["color"] == "#ff9900" and ib.accent_color() == (0xFF, 0x99, 0x00)
    ib.write_setting(color="#6ea8fe")
    assert ib.accent_color() is None and ib.accent_hex() == ""
    assert ib.status()["color"] == "#6ea8fe"
    # Clearing hands back to the variables; a key not given is left alone.
    st = ib.write_setting(color="")
    assert st["color"] == "" and st["effective"]["color"] == "#112233"
    assert st["name"] == "Admin"
    st = ib.write_setting(name="")
    assert st["name"] == "" and st["effective"]["name"] == "Env"
    assert json.loads(ib.settings_path().read_text()) == {}
    print("ok: the dashboard's choices win over the variables and hand back when cleared")


def test_stored_choices_are_validated():
    _env()
    # A label is one short line: control characters and runs of space go, the length is capped.
    st = ib.write_setting(name="Ad\nmin\t  one" + "x" * 100)
    assert st["name"].startswith("Ad min one") and len(st["name"]) == 40
    assert ib.valid_name("   ") is None and ib.valid_name(None) is None and ib.valid_name(12) is None
    # A colour that is not one is refused, and nothing changes.
    ib.write_setting(color="#f59e0b")
    for bad in ("orange", "#12345", "rgb(1,2,3)"):
        try:
            ib.write_setting(color=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    assert ib.status()["color"] == "#f59e0b"
    assert ib.normalize_color("#ABC") == "#aabbcc" and ib.normalize_color(None) is None
    # A document that is not an object, or no file at all, counts as nothing stored.
    ib.settings_path().write_text("[1, 2]")
    assert ib.stored() == {} and ib.status()["name"] == ""
    ib.settings_path().write_text("not json")
    assert ib.stored() == {}
    ib.settings_path().unlink()
    assert ib.stored() == {} and not ib.branded()
    print("ok: stored choices are validated and a broken file counts as none")


def test_stored_file_is_reread_when_it_changes():
    _env()
    ib.write_setting(name="One")
    assert ib.instance_name() == "One"
    # Another process (another gateway worker) writes the file: the next lookup sees it.
    time.sleep(0.01)
    ib.settings_path().write_text(json.dumps({"name": "Two", "color": "#f59e0b"}))
    assert ib.instance_name() == "Two" and ib.accent_hex() == "#f59e0b"
    assert "Two" in ib.signature()
    ib.settings_path().unlink()
    print("ok: a changed file is re-read")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    _env()
