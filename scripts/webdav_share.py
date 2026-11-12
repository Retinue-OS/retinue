#!/usr/bin/env python3
"""A minimal WebDAV share of the letterbox, at ``/inbox/`` on the dashboard.

The web gateway hands every request under ``/inbox`` here (scripts/web-gateway.py)
with the letterbox directory as root (scripts/letterbox.py). It speaks the part
of WebDAV (RFC 4918, class 1) that dropping files needs — OPTIONS, PROPFIND
(depth 0 and 1), GET/HEAD, PUT, DELETE, MKCOL and MOVE — and nothing else: no
locks, no COPY, no properties of its own. That is enough for the phone shell,
for `curl -T`, and for the WebDAV clients of file managers and sync apps that
only upload.

Authentication is the dashboard's own and happens before a request gets here:
Traefik's forward-auth asks the gateway's ``/auth`` (scripts/gateway_auth.py)
— a client certificate, or one of the basic-auth users, host-scoped by
``GATEWAY_BASIC_AUTH_SCOPES`` like everywhere else. Nothing here adds a login.

What the share refuses, because the folder is written from outside and read
by the framework:

- **Hidden names** (any path segment starting with a dot) and **names the
  framework reads** (``letterbox.DENIED_NAMES``: ``CLAUDE.md``, ``AGENTS.md``,
  ``requirements.txt``, ``accepted-requesters.txt``, …) — 403. The
  letterbox's own ``.inbox.json`` stays out of reach, so do side files like
  macOS's ``._*``, and nobody can plant a file that the next session, or the
  entrypoint, would take as instructions or configuration.
- **Paths out of the root** — ``..``, symlinks: 403/404.
- **Rendering on this origin.** A GET always answers as a download
  (``Content-Disposition: attachment``, ``application/octet-stream``,
  ``nosniff``, a sandboxing CSP), so an uploaded HTML file cannot run script
  under the dashboard's origin.

An upload is written to a hidden sibling and renamed into place when complete,
so the intake watcher and the inbox sweep (which skip hidden names) never see
half a file.
"""
from __future__ import annotations

import email.utils
import errno
import os
import re
import secrets
import shutil
import sys
import urllib.parse
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import letterbox  # noqa: E402

PREFIX = "/inbox"
_CHUNK_SIZE_RE = re.compile(rb"^[0-9A-Fa-f]{1,16}$")
MAX_UPLOAD = int(os.environ.get("LETTERBOX_MAX_UPLOAD", str(1024 * 1024 * 1024)))
METHODS = "OPTIONS, PROPFIND, GET, HEAD, PUT, DELETE, MKCOL, MOVE"


def handles(raw_path: str) -> bool:
    path = raw_path.split("?", 1)[0]
    return path == PREFIX or path.startswith(PREFIX + "/")


class _Refused(Exception):
    def __init__(self, status: int, text: str = ""):
        super().__init__(text)
        self.status = status
        self.text = text


def _segments(url_path: str) -> list[str]:
    """The path below the prefix as validated segments."""
    path = urllib.parse.urlsplit(url_path).path
    if not handles(path):
        raise _Refused(403, "outside the share")
    rest = path[len(PREFIX):]
    segs = []
    for raw in rest.split("/"):
        seg = urllib.parse.unquote(raw)
        if seg in ("", "."):
            continue
        if seg == ".." or "/" in seg or "\\" in seg or "\0" in seg:
            raise _Refused(403, "invalid path")
        if seg.startswith("."):
            raise _Refused(403, "hidden names are not accepted")
        if letterbox.denied(seg):
            raise _Refused(403, "that name is reserved")
        segs.append(seg)
    return segs


def _resolve(root: Path, segs: list[str]) -> Path:
    target = root.joinpath(*segs) if segs else root
    # Every existing ancestor must be a real directory inside the root: no
    # symlink is followed out of (or within) the share.
    probe = root
    for seg in segs:
        probe = probe / seg
        if probe.is_symlink():
            raise _Refused(404)
    return target


def _href(segs: list[str], is_dir: bool) -> str:
    href = PREFIX + "/" + "/".join(urllib.parse.quote(s) for s in segs)
    if is_dir and not href.endswith("/"):
        href += "/"
    return href


def _propstat(segs: list[str], path: Path) -> str:
    st = path.stat()
    is_dir = path.is_dir()
    modified = email.utils.formatdate(st.st_mtime, usegmt=True)
    name = escape(segs[-1]) if segs else "inbox"
    props = [f"<D:displayname>{name}</D:displayname>",
             f"<D:getlastmodified>{modified}</D:getlastmodified>"]
    if is_dir:
        props.append("<D:resourcetype><D:collection/></D:resourcetype>")
    else:
        props += ["<D:resourcetype/>",
                  f"<D:getcontentlength>{st.st_size}</D:getcontentlength>",
                  "<D:getcontenttype>application/octet-stream</D:getcontenttype>",
                  f'<D:getetag>"{st.st_size:x}-{st.st_mtime_ns:x}"</D:getetag>']
    return (f"<D:response><D:href>{escape(_href(segs, is_dir))}</D:href>"
            f"<D:propstat><D:prop>{''.join(props)}</D:prop>"
            "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


def _visible_children(path: Path) -> list[Path]:
    try:
        return sorted(c for c in path.iterdir()
                      if not c.name.startswith(".") and not c.is_symlink())
    except OSError:
        return []


def _read_body(handler, limit: int, sink) -> None:
    """Copy the request body into ``sink`` (a binary file), Content-Length or
    chunked; refuse more than ``limit`` bytes."""
    if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
        total = 0
        while True:
            line = handler.rfile.readline(1024)
            # Strictly hex digits: int(…, 16) would also take a sign or an
            # underscore, and a negative first chunk would walk past the cap.
            m = _CHUNK_SIZE_RE.match(line.split(b";", 1)[0].strip())
            if not m:
                raise _Refused(400, "malformed chunked body")
            size = int(m.group(0), 16)
            if size == 0:
                while handler.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                    pass  # trailers
                return
            total += size
            if total > limit:
                raise _Refused(413, "upload too large")
            _copy(handler.rfile, sink, size)
            handler.rfile.readline(8)  # the CRLF after the chunk
    length = _content_length(handler)
    if length > limit:
        raise _Refused(413, "upload too large")
    _copy(handler.rfile, sink, length)


def _content_length(handler) -> int:
    raw = (handler.headers.get("Content-Length") or "0").strip()
    if not raw.isascii() or not raw.isdigit():
        raise _Refused(400, "Content-Length is not a number")
    return int(raw) if len(raw) <= 18 else MAX_UPLOAD + 1


def _copy(src, dst, n: int) -> None:
    while n > 0:
        chunk = src.read(min(n, 1024 * 1024))
        if not chunk:
            raise _Refused(400, "body shorter than announced")
        dst.write(chunk)
        n -= len(chunk)


def _send(handler, status: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
          headers: dict | None = None) -> None:
    handler.send_response(status)
    for k, v in (headers or {}).items():
        handler.send_header(k, v)
    if body or status not in (204, 304):
        handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if body and handler.command != "HEAD":
        handler.wfile.write(body)


def serve(handler, root: Path) -> None:
    """Answer one request under /inbox (``handler`` is the gateway's
    BaseHTTPRequestHandler)."""
    try:
        if not root.is_dir():
            raise _Refused(503, "the letterbox is not mounted")
        method = handler.command
        fn = _METHODS.get(method)
        if fn is None:
            raise _Refused(405, "method not supported")
        try:
            fn(handler, root)
        except _Refused:
            raise
        except ValueError as exc:
            # A header or a path the parsers refused: the client's fault.
            raise _Refused(400, f"bad request: {exc}") from None
        except OSError as exc:
            # A full, read-only or vanished mount must still get an answer, or
            # the client sees a dropped connection and retries forever.
            raise _Refused(_os_status(exc), f"{exc.strerror or exc}") from None
    except _Refused as r:
        _send(handler, r.status, (r.text + "\n").encode() if r.text else b"",
              headers={"Allow": METHODS} if r.status == 405 else None)


def _os_status(exc: OSError) -> int:
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return 507  # Insufficient Storage (RFC 4918)
    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
        return 409
    if exc.errno in (errno.EEXIST, errno.ENOTEMPTY, errno.EISDIR):
        return 409
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return 403
    return 500


def _options(handler, root: Path) -> None:
    _send(handler, 200, headers={"DAV": "1", "Allow": METHODS, "MS-Author-Via": "DAV"})


def _propfind(handler, root: Path) -> None:
    # The request body (which properties) is read and ignored: every answer
    # carries the same few live properties.
    n = _content_length(handler)
    if n:
        handler.rfile.read(min(n, 1024 * 1024))
    segs = _segments(handler.path)
    target = _resolve(root, segs)
    if not target.exists():
        raise _Refused(404)
    depth = (handler.headers.get("Depth") or "1").strip()
    parts = [_propstat(segs, target)]
    if target.is_dir() and depth != "0":
        for child in _visible_children(target):
            try:
                parts.append(_propstat([*segs, child.name], child))
            except OSError:
                continue
    body = ('<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">'
            + "".join(parts) + "</D:multistatus>").encode("utf-8")
    _send(handler, 207, body, ctype="application/xml; charset=utf-8")


def _get(handler, root: Path) -> None:
    segs = _segments(handler.path)
    target = _resolve(root, segs)
    if target.is_dir():
        names = [c.name + ("/" if c.is_dir() else "") for c in _visible_children(target)]
        _send(handler, 200, ("\n".join(names) + ("\n" if names else "")).encode("utf-8"),
              headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
        return
    if not target.is_file():
        raise _Refused(404)
    size = target.stat().st_size
    handler.send_response(200)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Content-Length", str(size))
    handler.send_header("Content-Disposition", "attachment")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
    handler.send_header("Cache-Control", "private, no-store")
    handler.end_headers()
    if handler.command == "HEAD":
        return
    with open(target, "rb") as f:
        shutil.copyfileobj(f, handler.wfile, 1024 * 1024)


def _put(handler, root: Path) -> None:
    segs = _segments(handler.path)
    if not segs:
        raise _Refused(405, "cannot PUT the share itself")
    target = _resolve(root, segs)
    if target.is_dir():
        raise _Refused(405, "a collection is in the way")
    if not target.parent.is_dir():
        raise _Refused(409, "the parent collection does not exist")
    existed = target.exists()
    tmp = target.parent / f".{target.name}.upload-{secrets.token_hex(4)}"
    try:
        with open(tmp, "wb") as f:
            _read_body(handler, MAX_UPLOAD, f)
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    _send(handler, 204 if existed else 201)


def _delete(handler, root: Path) -> None:
    segs = _segments(handler.path)
    if not segs:
        raise _Refused(403, "cannot delete the share itself")
    target = _resolve(root, segs)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    else:
        raise _Refused(404)
    _send(handler, 204)


def _mkcol(handler, root: Path) -> None:
    if _content_length(handler):
        raise _Refused(415, "MKCOL takes no body")
    segs = _segments(handler.path)
    if not segs:
        raise _Refused(405, "the share exists")
    target = _resolve(root, segs)
    if target.exists():
        raise _Refused(405, "already exists")
    if not target.parent.is_dir():
        raise _Refused(409, "the parent collection does not exist")
    target.mkdir()
    _send(handler, 201)


def _move(handler, root: Path) -> None:
    segs = _segments(handler.path)
    if not segs:
        raise _Refused(403, "cannot move the share itself")
    source = _resolve(root, segs)
    if not source.exists():
        raise _Refused(404)
    dest_header = handler.headers.get("Destination") or ""
    if not dest_header:
        raise _Refused(400, "no Destination")
    dsegs = _segments(dest_header)
    if not dsegs:
        raise _Refused(403, "cannot replace the share itself")
    if dsegs[:len(segs)] == segs:
        raise _Refused(403, "cannot move a collection into itself")
    dest = _resolve(root, dsegs)
    if not dest.parent.is_dir():
        raise _Refused(409, "the destination's parent collection does not exist")
    existed = dest.exists()
    if existed:
        if (handler.headers.get("Overwrite") or "T").strip().upper() == "F":
            raise _Refused(412, "destination exists")
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    os.replace(source, dest)
    _send(handler, 204 if existed else 201)


_METHODS = {
    "OPTIONS": _options, "PROPFIND": _propfind, "GET": _get, "HEAD": _get,
    "PUT": _put, "DELETE": _delete, "MKCOL": _mkcol, "MOVE": _move,
}
