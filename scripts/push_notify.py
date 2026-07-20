"""Web Push (VAPID) fan-out for the Retinue dashboard PWA.

Why this exists: the dashboard already shows an unread badge, but only while it
is open — `conversations.js` polls every few seconds and stops the moment the
app is swiped away. When Ara opens a thread that needs a decision, the user is
by definition *not* looking at the dashboard. Web Push is the only channel that
reaches an installed PWA with no page running.

Design notes:

  * **Keys are generated once and persisted.** The VAPID keypair identifies this
    server to the browser push services; regenerating it invalidates every
    existing subscription. It is written to PUSH_DIR (which defaults to a
    sibling of CONVERSATIONS_DIR, so it inherits the deployment's persistent
    /root volume) with 0600 permissions.
  * **Subscriptions are one JSON file per endpoint**, keyed by a hash of the
    endpoint URL, matching how conversations are stored. A push service that
    answers 404/410 has permanently dropped the subscription (app uninstalled,
    permission revoked), so we delete it — that is the only supported way to
    learn a subscription is dead.
  * **Everything is best effort.** A failing push must never break the API call
    that triggered it, so `notify()` swallows errors and returns a count.
  * **pywebpush is optional.** If it is not installed the module reports itself
    disabled, the dashboard hides its opt-in button, and the rest of the gateway
    is unaffected. This keeps ad-hoc/dev runs of web-gateway.py working without
    the dependency.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from pathlib import Path

try:  # pywebpush pulls in cryptography, http-ece and py-vapid
    from pywebpush import WebPushException, webpush
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    _AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure just disables push
    _AVAILABLE = False

# Contact address embedded in the VAPID claim. Push services want a way to reach
# the operator about a misbehaving sender; they never expose it to the user.
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")
# How long a push may sit in the push service's queue before being dropped.
PUSH_TTL = int(os.environ.get("PUSH_TTL", "86400"))

_lock = threading.Lock()
_state_dir: Path | None = None
_public_key_b64: str | None = None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def init(state_dir: Path) -> None:
    """Prepare the key and subscription store. Safe to call more than once."""
    global _state_dir, _public_key_b64
    if not _AVAILABLE:
        return
    with _lock:
        _state_dir = state_dir
        (_state_dir / "subscriptions").mkdir(parents=True, exist_ok=True)
        _public_key_b64 = _load_or_create_keys()


def _key_path() -> Path:
    assert _state_dir is not None
    return _state_dir / "vapid_private.pem"


def _load_or_create_keys() -> str | None:
    """Return the base64url application server key, creating it if needed."""
    path = _key_path()
    try:
        if path.exists():
            private = serialization.load_pem_private_key(path.read_bytes(), password=None)
        else:
            private = ec.generate_private_key(ec.SECP256R1())
            pem = private.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            # Write then tighten: the key authenticates this server to every
            # push service the user's browser talks to.
            path.write_bytes(pem)
            os.chmod(path, 0o600)
        # The browser's applicationServerKey is the raw uncompressed EC point.
        point = private.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        return _b64url(point)
    except Exception as exc:  # noqa: BLE001
        print(f"[push] could not prepare VAPID keys: {exc!r}", flush=True)
        return None


def enabled() -> bool:
    return _AVAILABLE and _state_dir is not None and _public_key_b64 is not None


def public_key() -> str | None:
    return _public_key_b64


def _sub_path(endpoint: str) -> Path:
    assert _state_dir is not None
    digest = hashlib.sha256(endpoint.encode()).hexdigest()
    return _state_dir / "subscriptions" / f"{digest}.json"


def subscribe(subscription: dict) -> bool:
    """Store a PushSubscription as handed over by the browser."""
    if not enabled():
        return False
    endpoint = (subscription or {}).get("endpoint")
    keys = (subscription or {}).get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return False
    record = {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}
    with _lock:
        path = _sub_path(endpoint)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        tmp.replace(path)
    return True


def unsubscribe(endpoint: str) -> bool:
    if not enabled() or not endpoint:
        return False
    with _lock:
        path = _sub_path(endpoint)
        if path.exists():
            path.unlink()
            return True
    return False


def _all_subscriptions() -> list[dict]:
    assert _state_dir is not None
    out = []
    for path in sorted((_state_dir / "subscriptions").glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - skip a corrupt record, keep the rest
            continue
    return out


def subscription_count() -> int:
    return len(_all_subscriptions()) if enabled() else 0


def notify(title: str, body: str, url: str = "/", tag: str | None = None) -> int:
    """Push a notification to every registered device. Returns how many got it.

    Best effort by contract: callers invoke this from request handlers and must
    not fail because a push service is down.
    """
    if not enabled():
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag or url})
    sent = 0
    for sub in _all_subscriptions():
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=str(_key_path()),
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=PUSH_TTL,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            # 404/410 mean the subscription is permanently gone (uninstalled,
            # permission revoked). Any other error is transient — keep it.
            if status in (404, 410):
                unsubscribe(sub.get("endpoint", ""))
                print(f"[push] dropped expired subscription ({status})", flush=True)
            else:
                print(f"[push] send failed ({status}): {exc}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[push] send failed: {exc!r}", flush=True)
    return sent


def notify_async(title: str, body: str, url: str = "/", tag: str | None = None) -> None:
    """Fan out in the background so the triggering HTTP response isn't delayed."""
    if not enabled():
        return
    threading.Thread(
        target=notify,
        args=(title, body, url, tag),
        name="push-notify",
        daemon=True,
    ).start()
