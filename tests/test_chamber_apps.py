#!/usr/bin/env python3
"""Chamber apps in the web gateway (docs/chamber-apps.md).

Runs the gateway's real handler on a loopback port, with the example chamber
app (examples/chamber-app) mounted as a chamber and a fake life store, and
checks:

- discovery: /apps/index.json lists the app, its icons as paths inside it;
- serving: the app's files at /apps/<chamber>/<app>/, the redirect to the
  directory, traversal (.., encoded .., dotfiles, a symlink out, a directory
  without a manifest, reserved and underscore chambers) refused;
- the policy on app pages: the dashboard's directives, enforced even in
  report mode, none in off mode; no inline script in the example's pages;
- no script type from outside an app directory: a chamber's other files are
  unreachable under /apps/, unknown suffixes are downloads, the API answers
  JSON with nosniff, and Retinue.submit cannot write into .retinue/;
- the files API's path safety, size limit, overwrite rule and CSRF refusal;
- the schedule API: validation, registration, listing, removal;
- the reminder item's lifecycle: fired as an attention item of kind
  "reminder" (token-gated), cleared on open, done, skip, snooze and by the
  next occurrence; the push opens the app's deep link and carries snooze
  and skip; done ahead of time and skip today reach the trigger; a shell's
  delivery confirmation keeps the server from pushing, before or after it
  fired; a push waiting for a shell goes out when none confirms;
- the shell's listing (GET /apps/api/schedule?arm=1), ask and sparql.

    python3 tests/test_chamber_apps.py
"""
import http.client
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import types
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
WEBAPP = REPO_ROOT / "webapp"
EXAMPLE = REPO_ROOT / "examples" / "chamber-app"
ZRH = ZoneInfo("Europe/Zurich")
SCRIPT_TYPES = {"application/javascript", "text/javascript", "application/ecmascript",
                "text/ecmascript", "application/x-javascript"}

failures = []


def check(label, ok, detail=""):
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


class FakeStore(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps({"head": {"vars": ["s"]}, "results": {"bindings": []}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/sparql-results+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def load_gateway(tmp: Path, store_port: int):
    env = {
        "CONVERSATIONS_DIR": tmp / "convs", "CONVERSATION_DIR": tmp / "convlog",
        "CHAMBERS_DIR": tmp / "chambers", "WEB_GATEWAY_STATE": tmp / "state.json",
        "RETINUE_INSTANCE_FILE": tmp / "instance.json", "WEBAPP_DIR": WEBAPP,
        "DASHBOARD_DATA_DIR": tmp / "data", "LETTERBOX_DIR": tmp / "letterbox",
        "ATTENTION_DIR": tmp / "attention", "CHAMBER_APPS_STATE_DIR": tmp / "apps",
        "QLEVER_LIFE_URL": f"http://127.0.0.1:{store_port}",
        "SPARQL_ENDPOINT_EXTRA": f"http://127.0.0.1:{store_port}",
    }
    for k, v in env.items():
        os.environ[k] = str(v)
    os.environ["CONVERSATION_BACKEND_TOKEN"] = "tok"
    os.environ["ATTENTION_TZ"] = "Europe/Zurich"
    os.environ["APPS_SHELL_GRACE_SECONDS"] = "90"
    for name in ("WHATSAPP_GATEWAY_BASE_URL", "TELEGRAM_GATEWAY_BASE_URL", "MESSENGER_GATEWAYS",
                 "SIGNAL_GATEWAY_BASE_URL"):
        os.environ.pop(name, None)
    for d in ("chambers", "data", "letterbox", "attention"):
        (tmp / d).mkdir(parents=True, exist_ok=True)
    # Focus off: one mode that admits every sphere and rings from
    # time-sensitive, so a delivery decision depends on the item alone.
    (tmp / "attention" / "focus.json").write_text(json.dumps({"enabled": False}))
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("web_gateway_apps_under_test",
                                                  SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            headers = dict(headers or {}, **{"Content-Type": "application/json"})
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, resp.headers, data
    finally:
        conn.close()


def jbody(data):
    try:
        return json.loads(data)
    except ValueError:
        return None


def ctype(headers):
    return (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        store = serve(FakeStore)
        wg = load_gateway(tmp, store.server_address[1])
        chambers = tmp / "chambers"
        shutil.copytree(EXAMPLE, chambers / "routines", symlinks=True)
        app_dir = chambers / "routines" / ".retinue" / "apps" / "routine"
        # Hostile fixtures.
        (chambers / "routines" / "secret.js").write_text("alert(1)")
        (chambers / "routines" / ".retinue" / "apps" / "nomanifest").mkdir()
        (chambers / "routines" / ".retinue" / "apps" / "nomanifest" / "index.html").write_text("<p>x")
        (app_dir / ".hidden.js").write_text("alert(1)")
        (app_dir / "data.bin").write_bytes(b"\x00alert(1)")
        outside = tmp / "outside"
        outside.mkdir()
        (outside / "x.js").write_text("alert(1)")
        os.symlink(outside, app_dir / "link")
        os.symlink(outside, chambers / "routines" / "linked")
        for name in ("_generated", "api"):
            d = chambers / name / ".retinue" / "apps" / "a"
            d.mkdir(parents=True)
            (d / "manifest.webmanifest").write_text("{}")
            (d / "index.html").write_text("<p>x")

        pushes = []
        wg.push_notify.enabled = lambda: True
        wg.push_notify.subscription_count = lambda: 1
        wg.push_notify.notify_async = lambda title, body, url="/", tag=None, archived=False, \
            urgency=None, topic=None, extra=None: pushes.append(
                {"title": title, "body": body, "url": url, "tag": tag, "extra": extra})
        clock = {"now": datetime(2026, 11, 24, 10, 30, 10, tzinfo=ZRH)}
        wg.ATTENTION_CLOCK = lambda: clock["now"]
        turns = []
        wg._start_conv_turn = lambda cid: turns.append(cid)

        server = serve(wg.Handler)
        port = server.server_address[1]
        try:
            print("discovery")
            st, h, data = req(port, "GET", "/apps/index.json")
            apps = (jbody(data) or {}).get("apps") or []
            check("index lists the example only", [(a["chamber"], a["id"]) for a in apps] == [("routines", "routine")], apps)
            a = apps[0] if apps else {}
            check("name, url, sphere", (a.get("name"), a.get("url"), a.get("sphere")) ==
                  ("Routine timer", "/apps/routines/routine/", "health"), a)
            check("icon as a path inside the app", a.get("icons") == [
                {"src": "/apps/routines/routine/icon.svg", "sizes": "any", "type": "image/svg+xml"}], a.get("icons"))
            check("index is JSON, nosniff", ctype(h) == "application/json" and h.get("X-Content-Type-Options") == "nosniff")
            st, h, _ = req(port, "GET", "/apps/")
            check("/apps/ goes to the Apps page", st == 303 and h.get("Location") == "/apps.html")

            print("serving and traversal")
            st, h, data = req(port, "GET", "/apps/routines/routine")
            check("no trailing slash redirects", st == 303 and h.get("Location") == "/apps/routines/routine/")
            st, h, data = req(port, "GET", "/apps/routines/routine/")
            check("entry page", st == 200 and ctype(h) == "text/html" and b"Routine timer" in data)
            check("served under nosniff", h.get("X-Content-Type-Options") == "nosniff")
            st, h, _ = req(port, "GET", "/apps/routines/routine/app.js")
            check("app script served as a script from its app", st == 200 and ctype(h) == "text/javascript")
            st, h, _ = req(port, "GET", "/apps/routines/routine/sw.js")
            check("the app's own worker", st == 200 and ctype(h) == "text/javascript")
            st, h, _ = req(port, "GET", "/apps/routines/routine/data.bin")
            check("unknown suffix is a download", st == 200 and ctype(h) == "application/octet-stream"
                  and h.get("Content-Disposition") == "attachment")
            st, h, _ = req(port, "GET", "/apps/retinue.js")
            check("the client", st == 200 and ctype(h) == "text/javascript")
            for path in ("/apps/routines/routine/../../../secret.js", "/apps/routines/routine/%2e%2e/%2e%2e/%2e%2e/secret.js",
                         "/apps/routines/routine/..%2f..%2f..%2fsecret.js", "/apps/routines/routine/.hidden.js",
                         "/apps/routines/routine/link/x.js", "/apps/routines/nomanifest/",
                         "/apps/routines/nomanifest/index.html", "/apps/_generated/a/", "/apps/api/a/",
                         "/apps/routines/linked/x.js", "/apps/routines/%2e%2e/routines/secret.js",
                         "/apps/nosuch/routine/", "/apps/routines/routine/%00.js"):
                st, h, data = req(port, "GET", path)
                check(f"refused {path}", st in (404, 405) and ctype(h) != "text/javascript", (st, ctype(h)))

            print("the policy on app pages")
            for mode in ("report", "enforce"):
                os.environ["RETINUE_CSP"] = mode
                st, h, _ = req(port, "GET", "/apps/routines/routine/index.html")
                csp = h.get("Content-Security-Policy") or ""
                check(f"{mode}: enforced on the app page", "script-src 'self';" in csp
                      and "media-src 'self'" in csp and "img-src 'self' blob:" in csp
                      and "frame-src 'none'" in csp and not h.get("Content-Security-Policy-Report-Only"), csp)
            os.environ["RETINUE_CSP"] = "off"
            st, h, _ = req(port, "GET", "/apps/routines/routine/")
            check("off: no policy", not h.get("Content-Security-Policy"))
            os.environ["RETINUE_CSP"] = "report"
            st, h, _ = req(port, "GET", "/")
            check("dashboard pages keep their own mode", bool(h.get("Content-Security-Policy-Report-Only"))
                  and not h.get("Content-Security-Policy"))
            import re as _re
            for page in app_dir.glob("*.html"):
                text = page.read_text()
                inline = [m for m in _re.finditer(r"<script\b([^>]*)>", text) if "src=" not in m.group(1)]
                check(f"no inline script in {page.name}", not inline and not _re.search(r"\son\w+\s*=", text))

            print("no script type outside app directories")
            st, h, data = req(port, "PUT", "/apps/api/files/routines/.retinue/apps/routine/evil.js", b"alert(1)",
                              {"Content-Type": "text/javascript"})
            check("submit into .retinue/ refused", st == 400 and not (app_dir / "evil.js").exists(), st)
            st, h, data = req(port, "PUT", "/apps/api/files/routines/inbox/x.js", b"alert(1)",
                              {"Content-Type": "text/javascript"})
            check("a .js into the inbox is written", st == 201 and (chambers / "routines" / "inbox" / "x.js").exists(), data)
            for path in ("/apps/routines/inbox/x.js", "/apps/routines/x.js", "/apps/routines/secret.js"):
                st, h, _ = req(port, "GET", path)
                check(f"not served: {path}", st == 404 and ctype(h) != "text/javascript", st)
            st, h, _ = req(port, "GET", "/apps/api/schedule")
            check("API answers JSON with nosniff", ctype(h) == "application/json" and h.get("X-Content-Type-Options") == "nosniff")

            print("files API")
            st, h, data = req(port, "PUT", "/apps/api/files/routines/log.json", b'{"a":1}')
            check("one segment goes to the inbox", st == 201 and jbody(data).get("path") == "inbox/log.json", data)
            st, h, data = req(port, "PUT", "/apps/api/files/routines/log.json", b'{"a":2}')
            check("existing file: 409", st == 409)
            st, h, data = req(port, "PUT", "/apps/api/files/routines/log.json?replace=1", b'{"a":2}')
            check("replace=1 replaces", st == 201 and (chambers / "routines" / "inbox" / "log.json").read_text() == '{"a":2}')
            st, h, data = req(port, "PUT", "/apps/api/files/routines/sessions/2026/s1.json", b"{}")
            check("a directory is created inside the chamber", st == 201 and (chambers / "routines" / "sessions" / "2026" / "s1.json").exists())
            bad = {"/apps/api/files/routines/../outside.json": 400,
                   "/apps/api/files/routines/%2e%2e/outside.json": 400,
                   "/apps/api/files/routines/sessions/..%2f..%2fx.json": 400,
                   "/apps/api/files/routines/.git/config": 400,
                   "/apps/api/files/routines/linked/x.json": 400,
                   "/apps/api/files/routines/sessions/.x": 400,
                   "/apps/api/files/nosuch/x.json": 404,
                   "/apps/api/files/_generated/x.json": 404,
                   "/apps/api/files/routines/": 400}
            for path, want in bad.items():
                st, h, data = req(port, "PUT", path, b"{}")
                check(f"refused {path} ({want})", st == want, (st, data))
            check("nothing written outside", not (outside / "x.json").exists() and not (tmp / "outside.json").exists()
                  and not (chambers / "x.json").exists())
            wg.APPS_FILE_MAX_BYTES = 10
            st, h, data = req(port, "PUT", "/apps/api/files/routines/big.bin", b"x" * 11)
            check("too large: 413", st == 413)
            wg.APPS_FILE_MAX_BYTES = 1 << 20
            import socket
            with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                sock.sendall(b"PUT /apps/api/files/routines/cut.json HTTP/1.1\r\nHost: x\r\n"
                             b"Content-Length: 100\r\n\r\n{\"half\":")
                sock.shutdown(socket.SHUT_WR)
                answer = sock.recv(4096)
            check("a body cut short is not published", b" 400 " in answer.split(b"\r\n", 1)[0]
                  and not (chambers / "routines" / "inbox" / "cut.json").exists()
                  and not [p for p in (chambers / "routines" / "inbox").iterdir() if p.name.startswith(".upload-")],
                  answer[:80])
            st, h, data = req(port, "PUT", "/apps/api/files/routines/log.json", b'{"a":3}')
            check("exclusive create keeps the existing file", st == 409
                  and (chambers / "routines" / "inbox" / "log.json").read_text() == '{"a":2}')
            wg.APPS_FILE_MAX_BYTES = 1 << 20
            st, h, data = req(port, "PUT", "/apps/api/files/routines/c.json", b"{}", {"Sec-Fetch-Site": "cross-site"})
            check("another site's page: 403", st == 403)
            (chambers / "routines" / ".inbox.json").unlink()
            st, h, data = req(port, "PUT", "/apps/api/files/routines/n.json", b"{}")
            check("no inbox declared: say a directory", st == 400 and "inbox" in jbody(data)["error"])

            print("schedule API")
            base = "/apps/api/schedule/routines/routine"
            for bad_job in ({"id": "../x", "at": "07:00", "title": "t"}, {"id": "a", "at": "7", "title": "t"},
                            {"id": "a", "at": "07:00", "title": "t", "tz": "Nowhere/Land"},
                            {"id": "a", "kind": "job", "command": "true", "at": "07:00"},
                            {"id": "a", "at": "07:00"}, {"id": "a", "title": "t"},
                            {"id": "a", "at": "07:00", "title": "t", "open": "https://elsewhere.example/"},
                            {"id": "a", "at": "07:00", "title": "t", "open": "/sends"},
                            {"id": "a", "at": "07:00", "title": "t", "open": "../../x"}):
                st, _, data = req(port, "POST", base, bad_job)
                check(f"invalid job refused: {bad_job}", st == 400, (st, data))
            st, _, _ = req(port, "POST", "/apps/api/schedule/routines/nosuch", {"id": "a", "at": "07:00", "title": "t"})
            check("unknown app: 404", st == 404)
            st, _, _ = req(port, "POST", base, {"id": "a", "at": "07:00", "title": "t"}, {"Sec-Fetch-Site": "cross-site"})
            check("cross-site register: 403", st == 403)
            job = {"kind": "reminder", "id": "hand-1", "at": "10:30", "days": "mon-sun", "tz": "Europe/Zurich",
                   "title": "Hand session 1", "body": "Imagine, then try", "open": "#session/hand-1",
                   "importance": 5, "sphere": "health", "tag": ["training"], "lead": "30m"}
            st, _, data = req(port, "POST", base, job)
            got = jbody(data) or {}
            check("registered", st == 201 and got.get("full_id") == "app:routines:routine:hand-1"
                  and got.get("next") == "2026-11-25T10:30:00+01:00", data)
            st, _, data = req(port, "POST", base, {"kind": "job", "id": "weekly", "prompt": "Summarise.", "at": "18:00", "days": "fri"})
            check("agent job registered", st == 201)
            st, _, data = req(port, "GET", base)
            ids = [j["id"] for j in (jbody(data) or {}).get("jobs", [])]
            check("jobs() lists both", ids == ["hand-1", "weekly"], ids)
            st, _, _ = req(port, "DELETE", base + "/weekly")
            check("unschedule", st == 200 and [j["id"] for j in wg.chamber_apps.load_schedule("routines", "routine")["jobs"]] == ["hand-1"])

            print("reminder lifecycle")
            fire = "/internal/apps/reminders/fire"
            occ = "2026-11-24T09:30:00Z"
            body = {"chamber": "routines", "app": "routine", "id": "hand-1", "occurrence": occ}
            st, _, _ = req(port, "POST", fire, body)
            check("fire without the token: 403", st == 403)
            tok = {"X-Conversation-Backend-Token": "tok"}
            st, _, data = req(port, "POST", fire, body, tok)
            check("fired: pushed now", st == 200 and jbody(data).get("delivery") == "push", data)
            items, _ = wg._attention_items(wg._ATTENTION.profile(), clock["now"])
            rem = [i for i in items if i["kind"] == "reminder"]
            check("one reminder item on the list", len(rem) == 1 and rem[0]["id"] == "reminder:app:routines:routine:hand-1")
            check("its link is the app's deep link", rem and rem[0]["href"] ==
                  "/apps/routines/routine/?retinue-reminder=hand-1#session/hand-1", rem and rem[0]["href"])
            p = pushes[-1] if pushes else {}
            check("the push opens the app", p.get("url") == "/apps/routines/routine/?retinue-reminder=hand-1#session/hand-1")
            check("the push carries snooze and skip", [x["action"] for x in (p.get("extra") or {}).get("actions", [])]
                  == ["snooze", "skip"] and p["extra"]["api"] == "/apps/api/schedule/routines/routine/hand-1", p)
            n = len(pushes)
            st, _, data = req(port, "POST", fire, body, tok)
            check("the same occurrence twice changes nothing", st == 200 and len(pushes) == n)
            st, _, data = req(port, "GET", "/attention")
            rows = [r for sec in (jbody(data) or {}).get("sections", {}).values() for r in sec if r["kind"] == "reminder"]
            check("the home lists it", len(rows) == 1, data[:300])

            def open_reminders():
                its, _ = wg._attention_items(wg._ATTENTION.profile(), clock["now"])
                return [i for i in its if i["kind"] == "reminder"]

            st, _, data = req(port, "POST", base + "/hand-1/opened", {})
            check("opened clears it", st == 200 and jbody(data)["cleared"] and not open_reminders())

            clock["now"] = datetime(2026, 11, 25, 10, 30, 5, tzinfo=ZRH)
            req(port, "POST", fire, dict(body, occurrence="2026-11-25T09:30:00Z"), tok)
            clock["now"] = datetime(2026, 11, 26, 10, 30, 5, tzinfo=ZRH)
            req(port, "POST", fire, dict(body, occurrence="2026-11-26T09:30:00Z"), tok)
            its = open_reminders()
            check("the next occurrence replaces the open one", len(its) == 1 and its[0]["occurrence"] == "2026-11-26T09:30:00Z")
            rec = wg._REMINDERS.get("app:routines:routine:hand-1")
            check("and says so", rec.get("replaced") == "2026-11-25T09:30:00Z")

            st, _, data = req(port, "POST", base + "/hand-1/done", {})
            check("done clears it", jbody(data)["cleared"] and not open_reminders())
            clock["now"] = datetime(2026, 11, 27, 8, 0, tzinfo=ZRH)
            st, _, data = req(port, "POST", base + "/hand-1/done", {})
            check("done ahead of time skips today's slot", jbody(data).get("skipped") == "2026-11-27T09:30:00Z", data)
            trig = wg.chamber_apps.trigger_of(wg.chamber_apps.get_job("routines", "routine", "hand-1"))
            check("the trigger no longer fires today", trig.next_after(clock["now"]).isoformat() == "2026-11-28T10:30:00+01:00")

            clock["now"] = datetime(2026, 11, 28, 10, 30, 5, tzinfo=ZRH)
            req(port, "POST", fire, dict(body, occurrence="2026-11-28T09:30:00Z"), tok)
            st, _, data = req(port, "POST", base + "/hand-1/snooze", {})
            got = jbody(data)
            check("snooze clears it and adds one occurrence in 15 min", got["cleared"] and
                  got["occurrence"] == "2026-11-28T09:45:05Z" and not open_reminders(), got)
            trig = wg.chamber_apps.trigger_of(wg.chamber_apps.get_job("routines", "routine", "hand-1"))
            check("the snooze is a slot", wg.calendar_trigger.occurrence_key(trig.next_after(clock["now"]))
                  == "2026-11-28T09:45:05Z")
            clock["now"] = datetime(2026, 11, 28, 10, 45, 10, tzinfo=ZRH)
            req(port, "POST", fire, dict(body, occurrence="2026-11-28T09:45:05Z"), tok)
            check("the snoozed occurrence comes back", len(open_reminders()) == 1)
            st, _, data = req(port, "POST", base + "/hand-1/skip", {})
            got = jbody(data)
            check("skip clears it and skips the day", got["cleared"] and got["date"] == "2026-11-28" and not open_reminders())
            job_now = wg.chamber_apps.get_job("routines", "routine", "hand-1")
            check("skip drops today's snoozes", "2026-11-28" in job_now.get("skip_dates", []) and not job_now.get("extra"), job_now)

            print("mark done from the home")
            clock["now"] = datetime(2026, 11, 29, 10, 30, 5, tzinfo=ZRH)
            req(port, "POST", fire, dict(body, occurrence="2026-11-29T09:30:00Z"), tok)
            check("open", len(open_reminders()) == 1)
            check("marked done like any item", wg._attention_mark("reminder:app:routines:routine:hand-1", "done", "you")
                  and not open_reminders())

            print("a shell's delivery confirmation")
            req(port, "GET", "/apps/api/schedule?arm=1", headers={"Sec-Fetch-Site": "cross-site"})
            check("another site's page cannot arm", wg._REMINDERS.shell_armed_at() is None)
            st, _, data = req(port, "GET", "/apps/api/schedule?arm=1")
            listing = jbody(data) or {}
            r = (listing.get("reminders") or [{}])[0]
            check("the shell's listing", r.get("id") == "app:routines:routine:hand-1"
                  and r.get("url") == "/apps/routines/routine/?retinue-reminder=hand-1#session/hand-1"
                  and r.get("occurrences", [{}])[0] == {"at": "2026-11-30T09:30:00Z", "ring": True}, r)
            n = len(pushes)
            clock["now"] = datetime(2026, 11, 30, 10, 30, 5, tzinfo=ZRH)
            req(port, "POST", fire, dict(body, occurrence="2026-11-30T09:30:00Z"), tok)
            check("a shell armed it: the push waits", len(pushes) == n and
                  wg._REMINDERS.get("app:routines:routine:hand-1").get("push_pending_until"))
            st, _, _ = req(port, "POST", base + "/hand-1/delivered", {"occurrence": "2026-11-30T09:30:00Z"})
            clock["now"] += timedelta(minutes=5)
            wg._reminders_tick()
            check("confirmed: never pushed", st == 200 and len(pushes) == n)
            check("and still on the list", len(open_reminders()) == 1)
            clock["now"] = datetime(2026, 12, 1, 10, 29, 59, tzinfo=ZRH)
            st, _, _ = req(port, "POST", base + "/hand-1/delivered", {"occurrence": "2026-12-01T09:30:00Z"})
            clock["now"] = datetime(2026, 12, 1, 10, 30, 5, tzinfo=ZRH)
            req(port, "POST", fire, dict(body, occurrence="2026-12-01T09:30:00Z"), tok)
            wg._reminders_tick()
            check("confirmed before it fired: never pushed", len(pushes) == n and len(open_reminders()) == 1)
            clock["now"] = datetime(2026, 12, 2, 10, 0, tzinfo=ZRH)
            req(port, "GET", "/apps/api/schedule?arm=1")
            clock["now"] = datetime(2026, 12, 2, 10, 30, 5, tzinfo=ZRH)
            req(port, "POST", fire, dict(body, occurrence="2026-12-02T09:30:00Z"), tok)
            wg._reminders_tick()
            check("not yet", len(pushes) == n)
            clock["now"] += timedelta(seconds=91)
            wg._reminders_tick()
            check("no shell confirmed in time: pushed", len(pushes) == n + 1 and pushes[-1]["tag"]
                  == "reminder:app:routines:routine:hand-1")
            st, _, _ = req(port, "POST", base + "/hand-1/delivered", {"occurrence": "soon"})
            check("a confirmation names an instant", st == 400)

            print("held by the model")
            req(port, "POST", base, dict(job, id="quiet", importance=1))
            clock["now"] = datetime(2026, 12, 3, 10, 30, 5, tzinfo=ZRH)
            n = len(pushes)
            st, _, data = req(port, "POST", fire, dict(body, id="quiet", occurrence="2026-12-03T09:30:00Z"), tok)
            check("an unimportant reminder is listed, not pushed",
                  jbody(data).get("delivery") in ("list", "hold") and len(pushes) == n, data)
            st, _, _ = req(port, "DELETE", base + "/quiet")
            check("unscheduling clears its item", st == 200 and
                  all(i["source_id"] != "app:routines:routine:quiet" for i in open_reminders()))

            print("ask and sparql")
            st, _, data = req(port, "POST", "/apps/api/ask", {"agent": "coach", "text": "Plan next week.",
                                                             "app": {"chamber": "routines", "id": "routine"},
                                                             "importance": 3})
            got = jbody(data) or {}
            conv = wg._load_conv(got.get("thread", "")) if got.get("thread") else None
            check("ask opens a thread and starts a turn", st == 201 and conv and turns[-1] == conv["id"])
            check("with a note naming the app and the agent", conv and "routines/routine" in (conv["messages"][0].get("context") or "")
                  and "'coach'" in conv["messages"][0]["context"])
            check("never via the edge", conv and not conv["messages"][0].get("via_edge"))
            st, _, data = req(port, "POST", "/apps/api/ask", {"agent": "coach", "text": "And then?", "thread": got["thread"]})
            check("ask continues a thread", st == 200 and len(wg._load_conv(got["thread"])["messages"]) == 2)
            st, _, _ = req(port, "POST", "/apps/api/ask", {"agent": "../x", "text": "x"})
            check("an agent is a name", st == 400)
            st, h, data = req(port, "POST", "/apps/api/sparql", {"query": "SELECT ?s WHERE { ?s ?p ?o }"})
            check("sparql answers JSON results", st == 200 and "results" in (jbody(data) or {}) and ctype(h) == "application/json")
            st, _, _ = req(port, "POST", "/apps/api/sparql", {"query": "SELECT * WHERE { ?s ?p ?o }", "endpoint": "extra"})
            check("a deployment's endpoint by name", st == 200)
            st, _, _ = req(port, "POST", "/apps/api/sparql", {"query": "SELECT * WHERE { ?s ?p ?o }", "endpoint": "nope"})
            check("an unknown endpoint: 404", st == 404)
            st, _, _ = req(port, "POST", "/apps/api/sparql",
                           {"query": "SELECT * WHERE { SERVICE <http://x.example/> { ?s ?p ?o } }"})
            check("SERVICE refused", st == 403)
            st, _, _ = req(port, "POST", "/apps/api/sparql", {"query": "SELECT 1"}, {"Sec-Fetch-Site": "cross-site"})
            check("cross-site refused", st == 403)
        finally:
            server.shutdown()
            store.shutdown()
    if failures:
        print(f"{len(failures)} chamber-app check(s) FAILED")
        return 1
    print("all chamber-app gateway tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
