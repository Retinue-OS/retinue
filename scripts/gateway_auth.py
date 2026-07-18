#!/usr/bin/env python3
"""
Authentication helpers for the web gateway's Traefik forward-auth endpoint.

The public ``agents`` router accepts EITHER of two credentials, as alternatives:

  1. A TLS **client certificate** (mutual TLS). Traefik terminates TLS and, when
     configured with an optional ``clientAuth`` (``VerifyClientCertIfGiven``)
     against our client CA, verifies any presented certificate at the handshake.
     A certificate that reaches the backend is therefore already CA-verified, so
     the ``passTLSClientCert`` middleware forwarding it is sufficient proof.
  2. HTTP **basic auth** (the existing ``htpasswd`` users), as a fallback for
     browsers/devices without the certificate installed.

Traefik enforces this for the whole router via a ``forwardAuth`` middleware that
calls ``GET /auth`` on this gateway. ``decide()`` returns the HTTP status (and,
on failure, a ``WWW-Authenticate`` challenge) that Traefik relays to the client:
a 401 makes the browser show its password prompt, a 2xx lets the request through.

Internal container-to-container traffic never passes through Traefik, so it is
unaffected by this — only the public edge is gated.

Password verification is pure standard library (no third-party dependency): the
Apache ``$apr1$`` MD5 format produced by ``htpasswd -nb`` is implemented here and
checked in constant time. ``{SHA}`` and plaintext are also supported, plus
``$5$``/``$6$`` (crypt) and bcrypt when those are available.
"""

import base64
import hashlib
import hmac
import os

_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _to64(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ITOA64[value & 0x3F])
        value >>= 6
    return "".join(out)


def apr1_crypt(password: str, salt: str) -> str:
    """Compute the Apache ``$apr1$`` (MD5) crypt hash for ``password``+``salt``.

    Mirrors ``openssl passwd -apr1`` / ``htpasswd -m``. Pure stdlib.
    """
    pw = password.encode("utf-8") if isinstance(password, str) else password
    sp = salt.encode("ascii") if isinstance(salt, str) else salt
    magic = b"$apr1$"

    ctx = hashlib.md5(pw + magic + sp)
    alt = hashlib.md5(pw + sp + pw).digest()
    i = len(pw)
    while i > 0:
        ctx.update(alt[:i] if i < 16 else alt)
        i -= 16
    i = len(pw)
    while i:
        ctx.update(b"\x00" if (i & 1) else pw[:1])
        i >>= 1
    final = ctx.digest()
    for r in range(1000):
        c = hashlib.md5()
        c.update(pw if (r & 1) else final)
        if r % 3:
            c.update(sp)
        if r % 7:
            c.update(pw)
        c.update(final if (r & 1) else pw)
        final = c.digest()

    out = _to64((final[0] << 16) | (final[6] << 8) | final[12], 4)
    out += _to64((final[1] << 16) | (final[7] << 8) | final[13], 4)
    out += _to64((final[2] << 16) | (final[8] << 8) | final[14], 4)
    out += _to64((final[3] << 16) | (final[9] << 8) | final[15], 4)
    out += _to64((final[4] << 16) | (final[10] << 8) | final[5], 4)
    out += _to64(final[11], 2)
    return magic.decode() + salt + "$" + out


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against one ``htpasswd`` hash."""
    if not stored:
        return False
    if stored.startswith("$apr1$"):
        parts = stored.split("$")
        # ["", "apr1", salt, hash]
        if len(parts) < 4:
            return False
        salt = parts[2]
        return hmac.compare_digest(apr1_crypt(password, salt), stored)
    if stored.startswith("{SHA}"):
        digest = base64.b64encode(hashlib.sha1(password.encode("utf-8")).digest()).decode()
        return hmac.compare_digest("{SHA}" + digest, stored)
    if stored.startswith(("$2y$", "$2a$", "$2b$")):
        # bcrypt — only if a library is available; never silently pass.
        try:
            import bcrypt  # type: ignore

            return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
        except Exception:
            try:
                from passlib.hash import bcrypt as _pb  # type: ignore

                return bool(_pb.verify(password, stored))
            except Exception:
                return False
    if stored.startswith(("$5$", "$6$", "$1$")):
        try:
            import crypt  # deprecated but present on 3.12

            return hmac.compare_digest(crypt.crypt(password, stored), stored)
        except Exception:
            return False
    # Plaintext ``user:password`` entry (discouraged, but supported).
    return hmac.compare_digest(password, stored)


def load_users(raw: str) -> dict:
    """Parse an ``htpasswd``-style ``user:hash`` blob into a dict.

    Entries may be separated by newlines and/or commas (Traefik's
    ``basicauth.users`` uses commas; ``htpasswd`` files use newlines). The first
    colon splits user from hash, so hashes containing ``$`` and ``/`` are safe.
    """
    users = {}
    if not raw:
        return users
    for chunk in raw.replace("\r", "\n").replace(",", "\n").split("\n"):
        entry = chunk.strip()
        if not entry or entry.startswith("#") or ":" not in entry:
            continue
        user, _, h = entry.partition(":")
        user = user.strip()
        if user:
            users[user] = h.strip()
    return users


def check_basic_auth(authorization: str, users: dict) -> bool:
    """True if an ``Authorization: Basic`` header matches a configured user."""
    if not authorization or not users:
        return False
    try:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "basic" or not token:
            return False
        decoded = base64.b64decode(token).decode("utf-8")
        user, _, password = decoded.partition(":")
    except Exception:
        return False
    stored = users.get(user)
    if stored is None:
        return False
    return verify_password(password, stored)


def _cn_matches(info_header: str, allowed_cn: str) -> bool:
    """Best-effort CN check against Traefik's passTLSClientCert *info* header."""
    if not allowed_cn:
        return True
    if not info_header:
        return False
    # Traefik renders subject info as e.g. Subject="CN=reto,O=Retinue";...
    needle = f"CN={allowed_cn}"
    return needle in info_header


def decide(headers, users, *, cert_header: str, cert_info_header: str,
           allowed_cn: str = "", realm: str = "Retinue"):
    """Forward-auth decision.

    ``headers`` is a mapping-like object (case-insensitive ``.get``) of the
    incoming request headers that Traefik forwarded.

    Returns ``(status, response_headers)``. ``status`` 200 authorizes the
    request; 401 (with a ``WWW-Authenticate`` challenge) makes the browser prompt
    for a password — preserving basic auth as the fallback when no certificate is
    installed.
    """
    # 1. Client certificate (already CA-verified by Traefik if present).
    #
    # SECURITY: we trust the mere presence of this header. That is safe because
    # (a) Traefik's passTLSClientCert middleware Del()s any client-supplied value
    # and only re-Set()s it from the real TLS handshake state, so a forged header
    # cannot survive the edge; and (b) /auth is reachable only through Traefik on
    # the internal network (never published), so a client cannot inject the header
    # by calling /auth directly. The gateway cannot re-prove private-key
    # possession from a forwarded PEM, so this edge guarantee is the boundary.
    cert = headers.get(cert_header, "") or ""
    if cert.strip():
        info = headers.get(cert_info_header, "") or ""
        if _cn_matches(info, allowed_cn):
            return 200, {}
        # A certificate was presented but its CN is not allowed: reject outright
        # rather than fall back to a password prompt.
        return 403, {}

    # 2. Basic auth fallback.
    if check_basic_auth(headers.get("Authorization", ""), users):
        return 200, {}

    return 401, {"WWW-Authenticate": f'Basic realm="{realm}"'}


def config_from_env() -> dict:
    """Build the auth configuration from environment variables."""
    raw_users = (
        os.environ.get("GATEWAY_BASIC_AUTH_USERS")
        or os.environ.get("TRAEFIK_BASIC_AUTH_USERS")
        or ""
    )
    return {
        "users": load_users(raw_users),
        "cert_header": os.environ.get(
            "GATEWAY_CLIENT_CERT_HEADER", "X-Forwarded-Tls-Client-Cert"
        ),
        "cert_info_header": os.environ.get(
            "GATEWAY_CLIENT_CERT_INFO_HEADER", "X-Forwarded-Tls-Client-Cert-Info"
        ),
        "allowed_cn": os.environ.get("GATEWAY_CLIENT_CERT_CN", ""),
        "realm": os.environ.get("GATEWAY_AUTH_REALM", "Retinue"),
    }
