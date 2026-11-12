#!/usr/bin/env python3
"""Checks for the page the Android app embeds in its settings page.

The app serves its own settings page at a reserved address inside its WebView
and embeds this instance's settings as its last section, from
/settings-instance.html. What these pin down:

- the gateway serves the page with a framing policy that admits this origin and
  the app's page, and nothing else, so no other site can frame it;
- the policy names the same origin the page posts its height to, and the app's
  page lives at (APP_SETTINGS_ORIGIN);
- the page carries the instance branding in its title, like every page;
- links in it leave the frame (`<base target="_top">`), so the app can open
  them in the dashboard instead of inside the small frame;
- every other page is served as before, with no framing header added.

    python3 tests/test_gateway_settings_embed.py
"""
import importlib.util
import io
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
WEBAPP = REPO_ROOT / "webapp"


def _load_web_gateway(tmp: Path):
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["RETINUE_INSTANCE_FILE"] = str(tmp / "instance.json")
    # The repository's own webapp, not the image's /workspace copy.
    os.environ["WEBAPP_DIR"] = str(WEBAPP)
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_settings_embed_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Captured:
    """The handler's response, captured instead of written to a socket."""

    def __init__(self):
        self.status = None
        self.headers = {}
        self.body = b""


def _get(gw, path: str) -> _Captured:
    out = _Captured()
    handler = object.__new__(gw.Handler)
    handler.path = path
    handler.wfile = io.BytesIO()
    handler.send_response = lambda code, message=None: setattr(out, "status", code)
    handler.send_header = lambda name, value: out.headers.__setitem__(name, value)
    handler.end_headers = lambda: None
    served = handler._maybe_serve_dashboard()
    out.body = handler.wfile.getvalue()
    assert served, f"{path} was not served"
    return out


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["RETINUE_INSTANCE_NAME"] = "Admin"
        os.environ.pop("RETINUE_INSTANCE_COLOR", None)
        gw = _load_web_gateway(Path(tmp))

        page = _get(gw, "/settings-instance.html")
        assert page.status == 200
        policy = page.headers.get("Content-Security-Policy")
        assert policy == f"frame-ancestors 'self' {gw.APP_SETTINGS_ORIGIN}", policy
        assert gw.APP_SETTINGS_ORIGIN == "https://appassets.androidplatform.net"
        html = page.body.decode("utf-8")
        assert "<title>Retinue (Admin) — Instance settings</title>" in html, "branded like every page"
        assert '<base target="_top">' in html, "links leave the frame"
        # The page posts its height to the same origin the policy admits.
        assert f"'{gw.APP_SETTINGS_ORIGIN}'" in html
        assert "<retinue-instance-settings>" in html
        print("ok: the embed page is served with a framing policy for this origin and the app only")

        for other in ("/settings.html", "/index.html", "/components/instance-settings.js"):
            res = _get(gw, other)
            assert res.status == 200, other
            assert "Content-Security-Policy" not in res.headers, other
        print("ok: other pages are served as before, without a framing header")

        # The settings page names the shared element and keeps the per-device group.
        settings = (WEBAPP / "settings.html").read_text(encoding="utf-8")
        assert "<retinue-instance-settings></retinue-instance-settings>" in settings
        assert "data-device-group" in settings and "data-app-settings" in settings
        print("ok: the settings page shows this browser's group and this instance's section")
    os.environ.pop("RETINUE_INSTANCE_NAME", None)
    print("All settings embed checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
