#!/usr/bin/env python3
"""Checks that an inbound message is marked delivered only once triage ran.

The gateways forward with ``async: true``, so the retinue gateway answers
**202 Accepted** with a ``job_url`` — acceptance, not completion. Flipping the
``delivered`` flag on that 202 would hide a job that later fails from the daily
``/undelivered`` drain, losing the message. These checks pin the corrected
contract:

  * job_delivery.await_job: True only for ``status: done``; ``error``, a 404
    (expired job) and the poll deadline all return False, and a transport blip
    is retried rather than treated as failure.
  * all three inbox gateways: a forward that yields a ``job_url`` leaves the
    persisted message ``delivered=false`` until the job reports done — and a
    job that errors leaves it undelivered for the drain.
  * a synchronous answer (no ``job_url``) still marks delivered immediately.

    python3 tests/test_delivery_confirmation.py
"""
import importlib.util
import os
import sys
import tempfile
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load(module_name: str, script: str):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_langdetect():
    if "langdetect" not in sys.modules:
        stub = types.ModuleType("langdetect")
        stub.detect = lambda *a, **k: "en"
        stub.detect_langs = lambda *a, **k: []
        stub.LangDetectException = type("LangDetectException", (Exception,), {})
        sys.modules["langdetect"] = stub


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _stub_job_polls(jobs_module, responses):
    """Serve `responses` (one per GET) to job_delivery's poller."""
    remaining = list(responses)

    def _get(url, timeout=None):
        item = remaining.pop(0) if remaining else responses[-1]
        if isinstance(item, Exception):
            raise item
        return item

    jobs_module.requests = types.SimpleNamespace(
        get=_get, exceptions=jobs_module.requests.exceptions)


# ── job_delivery ──────────────────────────────────────────────────────────────

def test_await_job_outcomes():
    jobs = _load("job_delivery_under_test", "job_delivery.py")
    real_requests = jobs.requests
    opts = dict(timeout=2, interval=0.01, interval_max=0.01, backoff=1, http_timeout=1)

    _stub_job_polls(jobs, [_Resp(200, {"status": "pending"}), _Resp(200, {"status": "done"})])
    assert jobs.await_job("http://x/jobs/1", **opts) is True

    # A failed turn is NOT a delivery — the drain must still see the message.
    _stub_job_polls(jobs, [_Resp(200, {"status": "error", "error": "boom"})])
    assert jobs.await_job("http://x/jobs/1", **opts) is False

    # The in-memory job record expired before the turn finished.
    _stub_job_polls(jobs, [_Resp(404, {})])
    assert jobs.await_job("http://x/jobs/1", **opts) is False

    # A transport blip is transient: retry, don't give up.
    _stub_job_polls(jobs, [real_requests.exceptions.ConnectionError("down"),
                           _Resp(200, {"status": "done"})])
    assert jobs.await_job("http://x/jobs/1", **opts) is True

    # Never resolves → the deadline expires and the message stays undelivered.
    _stub_job_polls(jobs, [_Resp(200, {"status": "pending"})])
    assert jobs.await_job("http://x/jobs/1", timeout=0.1, interval=0.01,
                          interval_max=0.01, backoff=1, http_timeout=1) is False
    print("ok: await_job confirms only a completed job")


def test_confirm_delivery_runs_callback_on_success_only():
    jobs = _load("job_delivery_callback_under_test", "job_delivery.py")
    opts = dict(timeout=2, interval=0.01, interval_max=0.01, backoff=1, http_timeout=1)

    marked = []
    _stub_job_polls(jobs, [_Resp(200, {"status": "done"})])
    jobs.confirm_delivery("http://x/jobs/1", lambda: marked.append(1), **opts).join(5)
    assert marked == [1], marked

    _stub_job_polls(jobs, [_Resp(200, {"status": "error"})])
    jobs.confirm_delivery("http://x/jobs/1", lambda: marked.append(2), **opts).join(5)
    assert marked == [1], marked
    print("ok: confirm_delivery flips the flag only for a completed job")


# ── gateways ──────────────────────────────────────────────────────────────────

def _poll_env():
    os.environ["RETINUE_POLL_INTERVAL"] = "0.01"
    os.environ["RETINUE_POLL_INTERVAL_MAX"] = "0.01"
    os.environ["RETINUE_POLL_BACKOFF"] = "1"
    os.environ["RETINUE_GATEWAY_TIMEOUT"] = "5"
    os.environ["RETINUE_POLL_HTTP_TIMEOUT"] = "1"


def _load_whatsapp_gateway(tmp: Path):
    _poll_env()
    os.environ["WHATSAPP_DATA_DIR"] = str(tmp / "data")
    os.environ["WHATSAPP_TMP_DIR"] = str(tmp / "tmp")
    os.environ["WHATSAPP_PENDING_SENDS_DIR"] = str(tmp / "pending")
    os.environ["INBOUND_STORE_DIR"] = str(tmp / "inbound")
    os.environ["WHATSAPP_REPLY_TOKENS_DIR"] = str(tmp / "reply-tokens")
    return _load("whatsapp_gateway_delivery_under_test", "whatsapp-gateway.py")


def _load_signal_gateway(tmp: Path):
    _stub_langdetect()
    _poll_env()
    os.environ["PIPER_DATA_DIR"] = str(tmp / "models")
    os.environ["SIGNAL_ATTACHMENTS_DIR"] = str(tmp / "attachments")
    os.environ["SIGNAL_DATA_DIR"] = str(tmp / "signal-data")
    os.environ["INBOUND_STORE_DIR"] = str(tmp / "inbound")
    os.environ["SIGNAL_REPLY_TOKENS_DIR"] = str(tmp / "reply-tokens")
    os.environ.setdefault("SIGNAL_ACCOUNT", "+15550000000")
    return _load("signal_gateway_delivery_under_test", "signal-gateway.py")


def _load_telegram_gateway(tmp: Path):
    _stub_langdetect()
    _poll_env()
    os.environ["TELEGRAM_TMP_DIR"] = str(tmp / "tmp")
    os.environ["TELEGRAM_DATA_DIR"] = str(tmp / "data")
    os.environ["TELEGRAM_PENDING_SENDS_DIR"] = str(tmp / "pending")
    os.environ["INBOUND_STORE_DIR"] = str(tmp / "inbound")
    os.environ["TELEGRAM_REPLY_TOKENS_DIR"] = str(tmp / "reply-tokens")
    return _load("telegram_gateway_delivery_under_test", "telegram-gateway.py")


def _accept_post(module, body):
    """Stub the gateway's POST /message with a 202 carrying `body`."""
    calls = []
    module.requests = types.SimpleNamespace(
        post=lambda url, json=None, timeout=None: calls.append(json) or _Resp(202, body),
        exceptions=module.requests.exceptions,
    )
    return calls


def _stored_flags(tmp: Path) -> list[bool]:
    files = sorted((tmp / "inbound").rglob("*.nt"))
    flags = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "#delivered>" in line:
                flags.append('"true"' in line)
    return flags


def _await_flag(tmp: Path, expected: bool, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        flags = _stored_flags(tmp)
        if flags and all(f is expected for f in flags):
            return True
        time.sleep(0.02)
    return False


def _check_gateway(name: str, loader, forward):
    # 1. Accepted job that later fails → the message stays undelivered.
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        gw = loader(tmp)
        _accept_post(gw, {"status": "pending", "job_id": "j1", "job_url": "/jobs/j1"})
        _stub_job_polls(gw._jobs, [_Resp(200, {"status": "error", "error": "model outage"})])
        forward(gw)
        assert _stored_flags(tmp) == [False], _stored_flags(tmp)
        time.sleep(0.3)  # give the poller time to (wrongly) flip it
        assert _stored_flags(tmp) == [False], _stored_flags(tmp)

    # 2. Accepted job that completes → delivered, but only after `done`.
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        gw = loader(tmp)
        _accept_post(gw, {"status": "pending", "job_id": "j2", "job_url": "/jobs/j2"})
        _stub_job_polls(gw._jobs, [_Resp(200, {"status": "pending"}),
                                   _Resp(200, {"status": "done"})])
        forward(gw)
        assert _await_flag(tmp, True), _stored_flags(tmp)

    # 3. Synchronous answer (no job_url) → the turn already ran; flip at once.
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        gw = loader(tmp)
        _accept_post(gw, {"response": "handled"})
        forward(gw)
        assert _stored_flags(tmp) == [True], _stored_flags(tmp)

    print(f"ok: {name} marks delivered only once the triage job reports done")


def test_whatsapp_delivery_confirmation():
    _check_gateway(
        "whatsapp", _load_whatsapp_gateway,
        lambda gw: gw._forward_to_inbox("hello", "en", "+15551234567",
                                        origin="+15551234567@s.whatsapp.net"))


def test_signal_delivery_confirmation():
    _check_gateway(
        "signal", _load_signal_gateway,
        lambda gw: gw._forward_to_inbox("hello", "en", "+15551234567"))


def test_telegram_delivery_confirmation():
    _check_gateway(
        "telegram", _load_telegram_gateway,
        lambda gw: gw._forward_to_inbox("hello", "en", "12345"))


def main():
    test_await_job_outcomes()
    test_confirm_delivery_runs_callback_on_success_only()
    test_whatsapp_delivery_confirmation()
    test_signal_delivery_confirmation()
    test_telegram_delivery_confirmation()
    print("\nAll delivery-confirmation checks passed.")


if __name__ == "__main__":
    main()
