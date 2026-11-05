#!/usr/bin/env python3
"""Minimal per-instance branding of the dashboard PWA.

One person may run several Retinue deployments — a main one, an admin one, a
demo — and install each one's dashboard as a PWA. Every deployment serves the
same baked manifest and icons, so the home screen shows identical "Retinue"
icons and the task switcher shows identical windows. Two optional settings
tell them apart; a single-instance deployment needs neither:

    name    a short label for this instance ("Admin"). The PWA's home-screen
            label becomes the label, its full name and the page titles
            "Retinue (Admin)". It also becomes the Ask-Ara connector's default
            identity, "Ara (Admin)", unless ARA_MCP_IDENTITY names one
            explicitly (scripts/ara-mcp-server.py), and it is what the Android
            shell lists this instance under: the shell reads the manifest's
            short_name and shows it in its endpoint menu.
    color   the accent colour as #rrggbb (or #rgb): the icons' ring and the
            dashboard's own accent (send buttons, links, focus rings,
            highlights). An invalid value is ignored, keeping the stock blue.
            The send button's glyph stays dark, so pick a light or medium tone.

Two sources, dashboard over deployment, as for the agent models:

    RETINUE_INSTANCE_NAME / RETINUE_INSTANCE_COLOR   the deployment defaults.
    RETINUE_INSTANCE_FILE (default /root/.retinue/instance.json, the persistent
    volume)   what the dashboard's Settings › Instance card writes:
              {"name": "Admin", "color": "#f59e0b"}. A key that is present
              and not empty wins over the variable; one that is absent or
              empty leaves the variable in force. The file is re-read when it
              changes (stat per lookup, cheap), so a save on the Settings page
              reaches the next response — and, through the service worker's
              shell hash, every client.

Everything here works on the response only: the baked files under webapp/ are
never mutated, exactly like the service worker's shell-hash substitution. The
icons are recoloured without an imaging library — they are plain 8-bit RGB
PNGs of two colours (background and accent), and each pixel is re-projected
onto the line from the background to the new accent, so anti-aliased edges
stay smooth.
"""
from __future__ import annotations

import html
import json
import os
import re
import struct
import tempfile
import threading
import zlib
from pathlib import Path

DEFAULT_NAME = "Retinue"

# The dashboard's choices (see the module docstring); the variables are the defaults.
DEFAULT_SETTINGS_FILE = "/root/.retinue/instance.json"
ENV_NAME = "RETINUE_INSTANCE_NAME"
ENV_COLOR = "RETINUE_INSTANCE_COLOR"

# A label is one short line: what fits a home-screen icon and a menu entry.
MAX_NAME_LENGTH = 40
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
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


def settings_path() -> Path:
    """Where the dashboard's choices live (RETINUE_INSTANCE_FILE, read per call so tests can point elsewhere)."""
    return Path(os.environ.get("RETINUE_INSTANCE_FILE") or DEFAULT_SETTINGS_FILE)


# The stored choices, re-read when the file's identity (inode), mtime or size
# changes. A save replaces the file (a new inode), so it is seen even when it
# lands within the filesystem's mtime granularity of the last one.
# One (key, data) pair, replaced in one assignment so a reader never pairs a new key with old data.
_STORED_CACHE: list = [(None, {})]

# One save at a time: two devices saving the name and the colour at once must
# not each write back the document they read, dropping the other's change. The
# gateway is the only writer, so a lock in its process is enough.
_WRITE_LOCK = threading.Lock()


def _file_key(path: Path) -> tuple:
    try:
        st = path.stat()
        return (str(path), st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), None, None, None)


def stored() -> dict:
    """The dashboard's stored choices, {} when none (or unreadable). Cached by the file's identity and mtime."""
    path = settings_path()
    key = _file_key(path)
    cached_key, cached = _STORED_CACHE[0]
    if cached_key == key:
        return cached
    data: dict = {}
    if key[1] is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            data = {}
    _STORED_CACHE[0] = (key, data)
    return data


def valid_name(raw) -> str | None:
    """[raw] as a label: one line, trimmed, at most MAX_NAME_LENGTH characters; None for nothing usable."""
    if not isinstance(raw, str):
        return None
    label = " ".join(_CONTROL_RE.sub(" ", raw).split())
    if not label:
        return None
    return label[:MAX_NAME_LENGTH]


def normalize_color(raw) -> str | None:
    """[raw] as "#rrggbb" (lowercase), or None for anything that is not a colour."""
    if not isinstance(raw, str):
        return None
    m = _COLOR_RE.fullmatch(raw.strip())
    if not m:
        return None
    hexval = m.group(1).lower()
    if len(hexval) == 3:
        hexval = "".join(ch * 2 for ch in hexval)
    return "#" + hexval


def stored_name() -> str:
    """The label the dashboard set, or ""."""
    return valid_name(stored().get("name")) or ""


def stored_color() -> str:
    """The colour the dashboard set ("#rrggbb"), or ""."""
    return normalize_color(stored().get("color")) or ""


def env_name() -> str:
    """The deployment's default label (RETINUE_INSTANCE_NAME), or ""."""
    return valid_name(os.environ.get(ENV_NAME, "")) or ""


def env_color() -> str:
    """The deployment's default colour (RETINUE_INSTANCE_COLOR) as "#rrggbb", or ""."""
    return normalize_color(os.environ.get(ENV_COLOR, "")) or ""


def instance_name() -> str:
    """The instance label in force — the dashboard's, else the deployment's — or "" for an unbranded deployment."""
    return stored_name() or env_name()


def display_name() -> str:
    """The app's full name: "Retinue", or "Retinue (<label>)"."""
    label = instance_name()
    return f"{DEFAULT_NAME} ({label})" if label else DEFAULT_NAME


def accent_color() -> tuple[int, int, int] | None:
    """The accent in force — the dashboard's, else the deployment's — as an RGB triple, or None for the stock icons."""
    hexval = (stored_color() or env_color()).lstrip("#")
    if not hexval:
        return None
    rgb = tuple(int(hexval[i:i + 2], 16) for i in (0, 2, 4))
    return None if rgb == STOCK_ACCENT else rgb


def accent_hex() -> str:
    """The accent in force as "#rrggbb", or "" for the stock colour."""
    rgb = accent_color()
    return "#%02x%02x%02x" % rgb if rgb else ""


def status() -> dict:
    """What the Settings › Instance card shows: the stored choices, the deployment defaults and what is in force."""
    return {
        "name": stored_name(),
        "color": stored_color(),
        "defaults": {"name": env_name(), "color": env_color()},
        "effective": {"name": instance_name(), "color": accent_hex(), "display_name": display_name()},
        "stock_color": "#%02x%02x%02x" % STOCK_ACCENT,
        "max_name_length": MAX_NAME_LENGTH,
    }


def write_setting(name: str | None = None, color: str | None = None) -> dict:
    """Store the dashboard's choices: a value given replaces the stored one ("" clears it, so the
    deployment default is in force again), None leaves it alone. Atomic (temp file + rename), so a
    reader never sees half a document, and serialised, so two saves at once do not lose either.
    Returns status(). Raises ValueError for a colour that is not one; OSError when the file cannot
    be written."""
    with _WRITE_LOCK:
        _write_setting_locked(name, color)
    return status()


def _write_setting_locked(name: str | None, color: str | None) -> None:
    data = dict(stored())
    if name is not None:
        label = valid_name(name)
        if label:
            data["name"] = label
        else:
            data.pop("name", None)
    if color is not None:
        if color.strip():
            value = normalize_color(color)
            if value is None:
                raise ValueError(f"not a colour: {color!r}")
            data["color"] = value
        else:
            data.pop("color", None)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".instance-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # What was just written is what is stored: no need to read it back.
    _STORED_CACHE[0] = (_file_key(path), data)


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
    # The label is configuration, not markup: escaped like any other text.
    title = "<title>" + html.escape(display_name(), quote=False)
    return _TITLE_RE.sub(lambda _m: title, text, count=1).encode("utf-8")


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
