#!/usr/bin/env python3
"""Minimal per-instance branding of the dashboard PWA.

One person may run several Retinue deployments — a main one, an admin one, a
demo — and install each one's dashboard as a PWA. Every deployment serves the
same baked manifest and icons, so the home screen shows identical "Retinue"
icons and the task switcher shows identical windows. Two optional variables
tell them apart; a single-instance deployment needs neither:

    RETINUE_INSTANCE_NAME    a short label for this instance ("Admin"). The
                             PWA's home-screen label becomes the label, its
                             full name and the page titles "Retinue (Admin)".
                             It also becomes the Ask-Ara connector's default
                             identity, "Ara (Admin)", unless ARA_MCP_IDENTITY
                             names one explicitly (scripts/ara-mcp-server.py).
    RETINUE_INSTANCE_COLOR   the accent colour as #rrggbb (or #rgb): the icons'
                             ring and the dashboard's own accent (send buttons,
                             links, focus rings, highlights). An invalid value
                             is ignored, keeping the stock blue. The send
                             button's glyph stays dark, so pick a light or
                             medium tone.

Everything here works on the response only: the baked files under webapp/ are
never mutated, exactly like the service worker's shell-hash substitution. The
icons are recoloured without an imaging library — they are plain 8-bit RGB
PNGs of two colours (background and accent), and each pixel is re-projected
onto the line from the background to the new accent, so anti-aliased edges
stay smooth.
"""
from __future__ import annotations

import json
import os
import re
import struct
import zlib

DEFAULT_NAME = "Retinue"
# The accent of the stock icons in webapp/icons/ (and the background they sit on).
STOCK_ACCENT = (0x6E, 0xA8, 0xFE)
STOCK_BACKGROUND = (0x0B, 0x0D, 0x12)

_COLOR_RE = re.compile(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TITLE_RE = re.compile(r"<title>Retinue")
# The stock accent as the stylesheets spell it: the --accent definition in
# styles.css, and the rgba() tints the components' shadow styles hard-code
# (custom properties cannot be put into rgba(), so those carry the triple).
_ACCENT_DEF_RE = re.compile(r"(--accent:\s*)#6ea8fe", re.IGNORECASE)
_ACCENT_RGB_RE = re.compile(r"rgba\(\s*110\s*,\s*168\s*,\s*254\s*,")


def instance_name() -> str:
    """The configured instance label, or "" for an unbranded deployment."""
    return os.environ.get("RETINUE_INSTANCE_NAME", "").strip()


def display_name() -> str:
    """The app's full name: "Retinue", or "Retinue (<label>)"."""
    label = instance_name()
    return f"{DEFAULT_NAME} ({label})" if label else DEFAULT_NAME


def accent_color() -> tuple[int, int, int] | None:
    """The configured accent as an RGB triple, or None for the stock icons."""
    m = _COLOR_RE.fullmatch(os.environ.get("RETINUE_INSTANCE_COLOR", "").strip())
    if not m:
        return None
    hexval = m.group(1)
    if len(hexval) == 3:
        hexval = "".join(ch * 2 for ch in hexval)
    rgb = tuple(int(hexval[i:i + 2], 16) for i in (0, 2, 4))
    return None if rgb == STOCK_ACCENT else rgb


def branded() -> bool:
    return bool(instance_name() or accent_color())


def signature() -> str:
    """A token that changes whenever the branding does — folded into the
    service worker's shell hash so a changed name or colour evicts the cached
    manifest and icons like any edited shell file would."""
    rgb = accent_color()
    return f"{instance_name()}|{'%02x%02x%02x' % rgb if rgb else ''}"


def brand_manifest(raw: bytes) -> bytes:
    """Return the web manifest with this instance's name in it."""
    label = instance_name()
    if not label:
        return raw
    data = json.loads(raw.decode("utf-8"))
    data["name"] = display_name()
    data["short_name"] = label
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def brand_html(raw: bytes) -> bytes:
    """Return a dashboard page whose <title> names this instance."""
    if not instance_name():
        return raw
    text = raw.decode("utf-8")
    return _TITLE_RE.sub(lambda _m: "<title>" + display_name(), text, count=1).encode("utf-8")


def brand_css(raw: bytes) -> bytes:
    """Return a stylesheet or component module with this instance's accent.

    Rewrites the root --accent definition and every rgba() tint of the stock
    accent; everything that reads var(--accent) follows from the former."""
    rgb = accent_color()
    if not rgb:
        return raw
    text = raw.decode("utf-8")
    text = _ACCENT_DEF_RE.sub(lambda m: m.group(1) + "#%02x%02x%02x" % rgb, text)
    text = _ACCENT_RGB_RE.sub("rgba(%d, %d, %d," % rgb, text)
    return text.encode("utf-8")


def recolor_png(raw: bytes, accent: tuple[int, int, int] | None = None) -> bytes:
    """Recolour a stock icon's accent; anything not decodable is returned as is."""
    accent = accent or accent_color()
    if not accent:
        return raw
    try:
        width, height, pixels = _decode_rgb_png(raw)
    except (ValueError, zlib.error, struct.error):
        return raw
    bg, old = STOCK_BACKGROUND, STOCK_ACCENT
    axis = [o - b for o, b in zip(old, bg)]
    norm = sum(a * a for a in axis)
    lut: dict[tuple[int, int, int], bytes] = {}
    out = bytearray(len(pixels))
    for i in range(0, len(pixels), 3):
        px = (pixels[i], pixels[i + 1], pixels[i + 2])
        mapped = lut.get(px)
        if mapped is None:
            t = sum((p - b) * a for p, b, a in zip(px, bg, axis)) / norm
            t = min(1.0, max(0.0, t))
            mapped = bytes(round(b + t * (n - b)) for b, n in zip(bg, accent))
            lut[px] = mapped
        out[i:i + 3] = mapped
    return _encode_rgb_png(width, height, bytes(out))


def _decode_rgb_png(raw: bytes) -> tuple[int, int, bytes]:
    """Decode a non-interlaced 8-bit RGB PNG into packed RGB bytes."""
    if not raw.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG")
    pos, idat, header = 8, bytearray(), None
    while pos < len(raw):
        (length,) = struct.unpack(">I", raw[pos:pos + 4])
        ctype = raw[pos + 4:pos + 8]
        body = raw[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if header is None:
        raise ValueError("no IHDR")
    width, height, depth, color_type, _comp, _filt, interlace = header
    if depth != 8 or color_type != 2 or interlace != 0:
        raise ValueError("unsupported PNG layout")
    bpp, stride = 3, width * 3
    data = zlib.decompress(bytes(idat))
    prev = bytearray(stride)
    pixels = bytearray()
    i = 0
    for _ in range(height):
        ftype = data[i]
        line = bytearray(data[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if ftype == 1:
                line[x] = (line[x] + a) & 0xFF
            elif ftype == 2:
                line[x] = (line[x] + b) & 0xFF
            elif ftype == 3:
                line[x] = (line[x] + (a + b) // 2) & 0xFF
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 0xFF
            elif ftype != 0:
                raise ValueError(f"bad filter type {ftype}")
        pixels += line
        prev = line
    return width, height, bytes(pixels)


def _encode_rgb_png(width: int, height: int, pixels: bytes) -> bytes:
    stride = width * 3
    rows = b"".join(b"\x00" + pixels[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(ctype: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + ctype + body
                + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))

    return (_PNG_SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9))
            + chunk(b"IEND", b""))
