#!/usr/bin/env python3
"""Checks for the letterbox's WebDAV share (scripts/webdav_share.py).

Serves a temp directory through the module on a loopback port — the same
`serve(handler, root)` the web gateway calls for every request under /inbox —
and talks to it with http.client:

- OPTIONS advertises DAV class 1; PROPFIND lists (depth 0/1) without hidden
  entries;
- PUT with a Content-Length and chunked, 201 then 204 on overwrite, never a
  partial file visible under the real name, 409 without the parent, 413 over
  the cap;
- GET answers as a download that cannot render on the dashboard's origin;
- MKCOL, MOVE (Overwrite: F), DELETE;
- hidden names, `..` and the share root itself are refused.

    python3 tests/test_webdav_share.py
"""
import http.client
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import webdav_share  # noqa: E402

failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


ROOT: Path


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _dav(self):
        if webdav_share.handles(self.path):
            webdav_share.serve(self, ROOT)
        else:
            self.send_error(404)

    do_GET = do_HEAD = do_PUT = do_DELETE = do_MKCOL = do_MOVE = do_PROPFIND = do_OPTIONS = \
        do_POST = _dav


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), data


def main():
    global ROOT
    with tempfile.TemporaryDirectory() as tmp:
        ROOT = Path(tmp)
        (ROOT / ".inbox.json").write_text("{}")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        s, h, _ = req(port, "OPTIONS", "/inbox/")
        check("OPTIONS: DAV 1", (s, h.get("DAV")), (200, "1"))

        s, _, _ = req(port, "PUT", "/inbox/a.json", b'{"x": 1}')
        check("PUT new: 201", s, 201)
        check("PUT wrote the bytes", (ROOT / "a.json").read_bytes(), b'{"x": 1}')
        s, _, _ = req(port, "PUT", "/inbox/a.json", b'{"x": 2}')
        check("PUT over: 204", s, 204)
        check("no upload temp left behind",
              sorted(p.name for p in ROOT.iterdir()), [".inbox.json", "a.json"])

        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.putrequest("PUT", "/inbox/chunked%20name.json")
        c.putheader("Transfer-Encoding", "chunked")
        c.endheaders()
        for part in (b'{"y":', b' 3}'):
            c.send(b"%x\r\n%s\r\n" % (len(part), part))
        c.send(b"0\r\n\r\n")
        r = c.getresponse()
        r.read()
        c.close()
        check("chunked PUT: 201 and the decoded body",
              (r.status, (ROOT / "chunked name.json").read_bytes()), (201, b'{"y": 3}'))

        s, _, _ = req(port, "PUT", "/inbox/missing/a.json", b"x")
        check("PUT without the parent: 409", s, 409)
        old = webdav_share.MAX_UPLOAD
        webdav_share.MAX_UPLOAD = 4
        s, _, _ = req(port, "PUT", "/inbox/big.bin", b"12345")
        webdav_share.MAX_UPLOAD = old
        check("PUT over the cap: 413, nothing stored", (s, (ROOT / "big.bin").exists()), (413, False))

        s, h, data = req(port, "GET", "/inbox/a.json")
        check("GET: the bytes", (s, data), (200, b'{"x": 2}'))
        check("GET: a download that cannot render here",
              (h.get("Content-Type"), h.get("Content-Disposition"), h.get("X-Content-Type-Options")),
              ("application/octet-stream", "attachment", "nosniff"))
        check("GET: sandboxed", "sandbox" in (h.get("Content-Security-Policy") or ""), True)

        s, _, data = req(port, "PROPFIND", "/inbox/", headers={"Depth": "1"})
        text = data.decode()
        check("PROPFIND depth 1: 207 listing the files", (s, "a.json" in text, "chunked%20name.json" in text),
              (207, True, True))
        check("PROPFIND hides hidden entries", ".inbox.json" in text, False)
        s, _, data = req(port, "PROPFIND", "/inbox/a.json", headers={"Depth": "0"})
        check("PROPFIND depth 0 on a file", (s, "<D:getcontentlength>8<" in data.decode()), (207, True))

        check("MKCOL: 201", req(port, "MKCOL", "/inbox/sub")[0], 201)
        check("MKCOL again: 405", req(port, "MKCOL", "/inbox/sub")[0], 405)
        s, _, _ = req(port, "MOVE", "/inbox/a.json",
                      headers={"Destination": f"http://127.0.0.1:{port}/inbox/sub/b.json"})
        check("MOVE: 201", (s, (ROOT / "sub/b.json").is_file(), (ROOT / "a.json").exists()),
              (201, True, False))
        req(port, "PUT", "/inbox/c.json", b"c")
        s, _, _ = req(port, "MOVE", "/inbox/c.json",
                      headers={"Destination": "/inbox/sub/b.json", "Overwrite": "F"})
        check("MOVE onto an existing file with Overwrite: F: 412", s, 412)
        s, _, _ = req(port, "MOVE", "/inbox/c.json", headers={"Destination": "/inbox/.hidden"})
        check("MOVE to a hidden name: 403", (s, (ROOT / "c.json").exists()), (403, True))
        check("DELETE a file: 204", req(port, "DELETE", "/inbox/c.json")[0], 204)
        check("DELETE a collection: 204", (req(port, "DELETE", "/inbox/sub")[0],
                                            (ROOT / "sub").exists()), (204, False))
        check("DELETE something missing: 404", req(port, "DELETE", "/inbox/nope")[0], 404)

        check("PUT a hidden name: 403", req(port, "PUT", "/inbox/.schedule.json", b"{}")[0], 403)
        check("…not even the manifest", req(port, "PUT", "/inbox/.inbox.json", b"{}")[0], 403)
        check("GET the manifest: 403", req(port, "GET", "/inbox/.inbox.json")[0], 403)
        check("`..`: 403", req(port, "GET", "/inbox/%2e%2e/etc")[0], 403)
        check("DELETE the share: 403", req(port, "DELETE", "/inbox/")[0], 403)
        check("POST: 405", req(port, "POST", "/inbox/x", b"x")[0], 405)
        check("manifest untouched", (ROOT / ".inbox.json").read_text(), "{}")
        srv.shutdown()

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
