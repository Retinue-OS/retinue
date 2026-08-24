#!/usr/bin/env python3
"""Confirm an inbound message's delivery only once its triage job succeeded.

Shared by the inbox-mode gateways (signal / whatsapp / telegram); it is copied
into each gateway image, like `inbound_store.py` and `triage_policy.py`.

**Why this exists.** A gateway forwards an inbound message by POSTing it to the
retinue gateway with ``async: true``. The answer is **202 Accepted** carrying a
``job_url`` — acceptance, not completion. Flipping the message's ``delivered``
flag on that 202 therefore asserts something that has not happened yet: if the
job's model turn later fails (an upstream outage, a crashed session), the
message is on record as delivered, the daily ``GET /undelivered`` drain skips
it, and nothing ever looks at it again. The message is silently lost — which is
precisely the failure the never-drop invariant exists to prevent.

So the flag is flipped only when ``GET <job_url>`` reports ``status: "done"``.
Every other outcome — ``status: "error"``, a 404 (the in-memory job record
expired before the turn finished), or the poll deadline running out — leaves
``delivered=False``, which is exactly what the daily drain reads to retry. The
bias stays at-least-once: a duplicate triage beats a lost message.

Polling runs on a daemon thread, so a triage turn that takes minutes never
blocks the gateway's receive loop.
"""
import threading
import time

import requests

DEFAULT_TIMEOUT = 3600.0
DEFAULT_INTERVAL = 3.0
DEFAULT_INTERVAL_MAX = 300.0
DEFAULT_BACKOFF = 2.0
DEFAULT_HTTP_TIMEOUT = 30.0


def _discard(_message: str) -> None:
    pass


def await_job(job_url: str, *,
              timeout: float = DEFAULT_TIMEOUT,
              interval: float = DEFAULT_INTERVAL,
              interval_max: float = DEFAULT_INTERVAL_MAX,
              backoff: float = DEFAULT_BACKOFF,
              http_timeout: float = DEFAULT_HTTP_TIMEOUT,
              log=None) -> bool:
    """Poll a retinue job until it resolves. True only for ``status: "done"``.

    A transport error or a non-404 HTTP status is treated as transient and
    retried with backoff until the deadline; every terminal non-success answer
    returns False immediately. Never raises — the caller's fallback (leaving the
    message undelivered) must not itself be able to fail.
    """
    log = log or _discard
    deadline = time.monotonic() + timeout
    wait = interval
    while time.monotonic() < deadline:
        time.sleep(wait)
        try:
            poll = requests.get(job_url, timeout=http_timeout)
        except requests.exceptions.RequestException as exc:
            log(f"job poll failed, retrying: {exc}")
            wait = min(wait * backoff, interval_max)
            continue
        if poll.status_code == 404:
            log("job expired or unknown before completion — leaving it undelivered")
            return False
        if poll.status_code >= 400:
            log(f"job poll returned HTTP {poll.status_code}, retrying")
            wait = min(wait * backoff, interval_max)
            continue
        try:
            body = poll.json() or {}
        except ValueError:
            body = {}
        status = body.get("status")
        if status == "done":
            return True
        if status == "error":
            log(f"triage job failed: {body.get('error')} — leaving it undelivered")
            return False
        wait = min(wait * backoff, interval_max)
    log("triage job timed out while polling — leaving it undelivered")
    return False


def confirm_delivery(job_url: str, on_done, *, log=None,
                     thread_name: str = "job-delivery",
                     **poll_options) -> threading.Thread:
    """Run ``on_done()`` on a daemon thread once the job reports success.

    ``poll_options`` are passed through to :func:`await_job`. Returns the thread
    so a test (or a shutdown path) can join it; callers normally ignore it.
    """
    log = log or _discard

    def _run() -> None:
        if not await_job(job_url, log=log, **poll_options):
            return
        try:
            on_done()
        except Exception as exc:  # noqa: BLE001 - a failed flip must not kill the thread
            log(f"could not confirm delivery: {exc}")

    thread = threading.Thread(target=_run, name=thread_name, daemon=True)
    thread.start()
    return thread
