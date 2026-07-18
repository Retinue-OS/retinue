#!/usr/bin/env python3
"""Push an outbound Signal message through the signal-gateway.

This is the retinue-side client for the gateway's `/send` endpoint. Agents
(Ara, and subagents she dispatches) use it to *initiate* Signal messages —
error escalations, alerts, daily briefings — rather than only replying to
inbound ones. The gateway owns the Signal account and the Piper/ffmpeg voice
pipeline, so the text body is delivered together with a spoken rendering of it
(and any images) exactly like normal gateway replies.

Examples:
    # Simple text + voice alert to the default recipient
    signal-push.py "Ari: failed to send reply to Mara — check scheduler.log"

    # Daily briefing with a chart, to a specific number, German voice
    signal-push.py --lang de --image /tmp/glucose.png \\
        "Guten Morgen! Hier ist dein Tagesbriefing …"

    # Text only, no audio
    signal-push.py --no-voice "Quick note without a voice attachment"

Configuration (environment):
    SIGNAL_GATEWAY_SEND_URL   default http://signal-gateway:8090/send
    SIGNAL_GATEWAY_TOKEN      optional bearer token (must match the gateway)
    SIGNAL_DEFAULT_RECIPIENT  optional fallback recipient when --recipient omitted
                              (the gateway also applies its own default)
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = os.environ.get("SIGNAL_GATEWAY_SEND_URL", "http://signal-gateway:8090/send")
TOKEN = os.environ.get("SIGNAL_GATEWAY_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("SIGNAL_GATEWAY_TIMEOUT", "60"))


def _encode_image(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"signal-push: image not found: {path}")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"filename": path.name, "data": data}


def main() -> int:
    parser = argparse.ArgumentParser(description="Push a Signal message via the signal-gateway.")
    parser.add_argument("message", nargs="?", default="", help="message body (spoken aloud unless --no-voice)")
    parser.add_argument("--recipient", help="Signal recipient (E.164). Defaults to the gateway's configured recipient.")
    parser.add_argument("--image", action="append", default=[], metavar="PATH",
                        help="attach an image (repeatable)")
    parser.add_argument("--lang", help="ISO language code for voice synthesis (auto-detected if omitted)")
    parser.add_argument("--no-voice", action="store_true", help="do not attach a spoken audio rendering")
    parser.add_argument("--user-approved", action="store_true",
                        help="assert that the user has already approved this send; "
                             "bypasses the verify flow for 'trust'-category recipients")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"gateway send URL (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    args = parser.parse_args()

    if not args.message and not args.image:
        parser.error("provide a message and/or at least one --image")

    payload: dict = {"message": args.message, "voice": not args.no_voice}
    if args.recipient:
        payload["recipient"] = args.recipient
    elif os.environ.get("SIGNAL_DEFAULT_RECIPIENT", "").strip():
        payload["recipient"] = os.environ["SIGNAL_DEFAULT_RECIPIENT"].strip()
    if args.lang:
        payload["lang"] = args.lang
    if args.user_approved:
        payload["user_approved"] = True
    if args.image:
        payload["images"] = [_encode_image(Path(p)) for p in args.image]

    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    request = urllib.request.Request(
        args.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("status") == "pending_approval":
            print(f"signal-push: send queued for approval (id={body.get('request_id', '?')})")
            approval_url = body.get("approval_url", "")
            if approval_url:
                print(f"signal-push: approve or deny at {approval_url}")
            note = body.get("note", "")
            if note:
                print(f"signal-push: {note}")
            return 0
        print(f"signal-push: sent to {body.get('recipient', '?')}")
        return 0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("error", "")
        except ValueError:
            detail = raw.strip()[:200]
        print(f"signal-push: gateway returned {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"signal-push: could not reach gateway at {args.url}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
