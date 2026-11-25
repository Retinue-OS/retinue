"""Retract a queued send on a channel gateway (the push CLIs' --retract).

A send whose policy category is `verify` (or `trust` without --user-approved)
waits in its gateway's pending store until the user approves or denies it on
/sends. Until then the agent that queued it may take it back — the messenger
counterpart of ``email_client.py retract``: the pending entry moves to the
terminal status "retracted", nothing is sent, and the /sends page stops
offering it. Once the user has approved it there is nothing left to retract:
the answer then says what became of the send — on its way, sent, failed, or
(where the gateway can tell the two apart) of unknown outcome, which is never
worded as "failed".

The gateway endpoint is ``POST /pending-sends/<id>/retract``, next to the
approve/reject pair the web-gateway uses; its base is the push CLI's own
``--url`` with the last path segment (``/send``, ``/create-event``) removed.
"""
import json
import re
import sys
import urllib.error
import urllib.request

_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# What each status the gateway may answer with means for the caller, and the
# exit code: a retraction that did not stop the send is a failure.
_OUTCOMES = {
    "retracted": (0, "retracted; nothing was sent"),
    "rejected": (0, "was already denied by the user; nothing was sent"),
    "sending": (1, "too late: the user approved it and it is being sent"),
    "approved": (1, "too late: the user approved it and it was sent"),
    "error": (1, "too late: the user approved it, but the send failed"),
}
# An "error" is not always a send that failed. A gateway that can tell says
# so on the entry (the mail gateway's ``outcome_unknown``): true is a mail
# that was handed to the mail server and whose answer nobody saw — it may
# have gone out, and whoever reads "failed" would send it again.
_UNKNOWN_OUTCOME = (1, "too late: it was handed over to be sent, and it is NOT KNOWN "
                       "whether it went out. Do not send it again: look in the thread, "
                       "or check with the recipient")


def gateway_base(action_url: str) -> str:
    """The gateway's base URL, from a push CLI's action URL."""
    return action_url.rstrip("/").rsplit("/", 1)[0]


def retract(prog: str, action_url: str, request_id: str, token: str,
            timeout: float, noun: str = "send") -> int:
    """Retract pending *request_id*; print the outcome and return an exit code."""
    request_id = (request_id or "").strip().lower()
    if not _REQUEST_ID_RE.match(request_id):
        print(f"{prog}: not a request id: {request_id!r} "
              f"(expected the 32-hex id printed when the {noun} was queued)",
              file=sys.stderr)
        return 2
    url = f"{gateway_base(action_url)}/pending-sends/{request_id}/retract"
    headers = {"Content-Length": "0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=b"", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("error", "")
        except (ValueError, AttributeError):
            detail = raw.strip()[:200]
        if exc.code == 404:
            print(f"{prog}: no pending {noun} {request_id}", file=sys.stderr)
        else:
            print(f"{prog}: gateway returned {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"{prog}: could not reach gateway at {url}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(body, dict):
        body = {}
    status = str(body.get("status") or "")
    code, text = _OUTCOMES.get(status, (1, f"unexpected status {status!r}"))
    if status == "error" and body.get("outcome_unknown"):
        code, text = _UNKNOWN_OUTCOME
    elif status == "error" and body.get("outcome_unknown") is False:
        # (Said only on the gateway's own word: one that does not tell the
        # two apart is read as before.)
        text += "; nothing was sent"
    print(f"{prog}: pending {noun} {request_id} {text}",
          file=sys.stdout if code == 0 else sys.stderr)
    return code
