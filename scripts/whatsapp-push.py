#!/usr/bin/env python3
"""Push an outbound WhatsApp message through the whatsapp-gateway.

The retinue-side client for the gateway's `/send` endpoint — the WhatsApp sibling
of signal-push.py. Agents (Ara, and subagents she dispatches) use it to *initiate*
WhatsApp messages — error escalations, alerts, briefings — rather than only
replying to inbound ones. The gateway owns the linked-device session, so no MCP
tool schema enters the context and the account credentials stay isolated.

Outbound is gated by WHATSAPP_SEND_POLICY (see the gateway): a `verify` recipient
queues the message as a pending send that must be approved on the web gateway's
/sends page; a `trust` recipient sends directly only with --user-approved. On a
queued send this prints the approval URL instead of confirming delivery.

Examples:
    # Simple text alert to the default recipient
    whatsapp-push.py "Ari: failed to send reply to Mara — check scheduler.log"

    # With an image attachment, to a specific number
    whatsapp-push.py --recipient +15551234567 --image /tmp/chart.png "Today's summary"

    # Assert the user already approved (bypasses verify for a 'trust' recipient)
    whatsapp-push.py --user-approved --recipient +15551234567 "Confirmed reply"

Configuration (environment):
    WHATSAPP_GATEWAY_SEND_URL   default http://whatsapp-gateway:8092/send
    WHATSAPP_GATEWAY_TOKEN      optional bearer token (must match the gateway)
    WHATSAPP_DEFAULT_RECIPIENT  optional fallback recipient when --recipient omitted
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

DEFAULT_URL = os.environ.get("WHATSAPP_GATEWAY_SEND_URL", "http://whatsapp-gateway:8092/send")
TOKEN = os.environ.get("WHATSAPP_GATEWAY_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("WHATSAPP_GATEWAY_TIMEOUT", "60"))


def _encode_image(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"whatsapp-push: image not found: {path}")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"filename": path.name, "data": data}


def main() -> int:
    parser = argparse.ArgumentParser(description="Push a WhatsApp message via the whatsapp-gateway.")
    parser.add_argument("message", nargs="?", default="", help="message body")
    parser.add_argument("--recipient", help="WhatsApp recipient (E.164 number or user@server JID). "
                                            "Defaults to the gateway's configured recipient.")
    parser.add_argument("--image", action="append", default=[], metavar="PATH",
                        help="attach an image (repeatable)")
    parser.add_argument("--user-approved", action="store_true",
                        help="assert that the user has already approved this send; "
                             "bypasses the verify flow for 'trust'-category recipients")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"gateway send URL (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    args = parser.parse_args()

    if not args.message and not args.image:
        parser.error("provide a message and/or at least one --image")

    payload: dict = {"message": args.message}
    if args.recipient:
        payload["recipient"] = args.recipient
    elif os.environ.get("WHATSAPP_DEFAULT_RECIPIENT", "").strip():
        payload["recipient"] = os.environ["WHATSAPP_DEFAULT_RECIPIENT"].strip()
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
            print(f"whatsapp-push: send queued for approval (id={body.get('request_id', '?')})")
            approval_url = body.get("approval_url", "")
            if approval_url:
                print(f"whatsapp-push: approve or deny at {approval_url}")
            note = body.get("note", "")
            if note:
                print(f"whatsapp-push: {note}")
            return 0
        print(f"whatsapp-push: sent to {body.get('recipient', '?')}")
        return 0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("error", "")
        except ValueError:
            detail = raw.strip()[:200]
        print(f"whatsapp-push: gateway returned {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"whatsapp-push: could not reach gateway at {args.url}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
