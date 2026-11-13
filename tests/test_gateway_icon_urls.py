#!/usr/bin/env python3
"""What the gateway serves for an instance with a colour of its own.

An installed PWA keeps the icon it fetched for a URL: desktop Chrome (143,
"predictable app updating") refreshes an installed app's icons — and the
taskbar and Start-menu shortcuts made from them — only when the manifest names
other icon URLs. So the colour has to show in the URLs, not only in the bytes
(scripts/instance_branding.py). What these pin down, through the request
handler itself:

- the manifest, every page's icon links and the service worker name the icons
  with the colour as a version (…png?v=rrggbb);
- such a URL is served, recoloured: the gateway ignores the query;
- the service worker's precache list and notification icon follow, and its
  cache name moves with the colour;
- a change of colour on the Settings page moves every one of those URLs on
  the next response;
- with no colour the plain URLs are served as before.

    python3 tests/test_gateway_icon_urls.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from test_gateway_settings_embed import WEBAPP, _get, _load_web_gateway  # noqa: E402

ICON_REF = re.compile(r"/icons/[A-Za-z0-9._-]+\.png(\?v=[0-9a-f]{6})?")


def _icon_refs(text: str) -> set[str]:
    return {m.group(0) for m in ICON_REF.finditer(text)}


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.pop("RETINUE_INSTANCE_NAME", None)
        os.environ["RETINUE_INSTANCE_COLOR"] = "#cb6cfe"
        gw = _load_web_gateway(Path(tmp))
        ib = gw.instance_branding

        manifest = json.loads(_get(gw, "/manifest.webmanifest").body)
        srcs = [icon["src"] for icon in manifest["icons"]]
        assert srcs and all(s.endswith("?v=cb6cfe") for s in srcs), srcs
        for page in ("/", "/settings.html", "/chat.html", "/settings-instance.html"):
            refs = _icon_refs(_get(gw, page).body.decode("utf-8"))
            assert all(r.endswith("?v=cb6cfe") for r in refs), (page, refs)
        sw = _get(gw, "/sw.js").body.decode("utf-8")
        assert "'/icons/icon-192.png?v=cb6cfe'" in sw and "icon: '/icons/icon-192.png?v=cb6cfe'" in sw
        assert all(r.endswith("?v=cb6cfe") for r in _icon_refs(sw)), _icon_refs(sw)
        print("ok: manifest, pages and service worker name the icons with the colour")

        stock = (WEBAPP / "icons" / "icon-192.png").read_bytes()
        icon = _get(gw, "/icons/icon-192.png?v=cb6cfe")
        assert icon.status == 200 and icon.headers.get("Content-Type") == "image/png"
        assert icon.body != stock
        _w, _h, px = ib._decode_rgb_png(icon.body)
        assert bytes((0xCB, 0x6C, 0xFE)) in {px[i:i + 3] for i in range(0, len(px), 3)}
        print("ok: a versioned icon URL is served, recoloured")

        cache_before = re.search(r"retinue-shell-[0-9a-f]+", sw).group(0)
        ib.write_setting(color="#22c55e")
        manifest = json.loads(_get(gw, "/manifest.webmanifest").body)
        assert all(icon["src"].endswith("?v=22c55e") for icon in manifest["icons"]), manifest["icons"]
        assert all(r.endswith("?v=22c55e") for r in _icon_refs(_get(gw, "/").body.decode("utf-8")))
        sw = _get(gw, "/sw.js").body.decode("utf-8")
        assert all(r.endswith("?v=22c55e") for r in _icon_refs(sw))
        assert re.search(r"retinue-shell-[0-9a-f]+", sw).group(0) != cache_before
        print("ok: a colour saved on the Settings page moves every icon URL and the cache name")

        ib.write_setting(color="")
        os.environ.pop("RETINUE_INSTANCE_COLOR", None)
        manifest_raw = _get(gw, "/manifest.webmanifest").body
        assert manifest_raw == (WEBAPP / "manifest.webmanifest").read_bytes()
        assert "?v=" not in _get(gw, "/").body.decode("utf-8")
        assert "?v=" not in _get(gw, "/sw.js").body.decode("utf-8")
        assert _get(gw, "/icons/icon-192.png").body == stock
        print("ok: without a colour the plain URLs and stock icons are served as before")
    print("All gateway icon URL checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
