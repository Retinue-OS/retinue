#!/usr/bin/env python3
"""Push an outbound Telegram message through the telegram-gateway.

The retinue-side client for the gateway's `/send` endpoint — the Telegram sibling
of signal-push.py / whatsapp-push.py. Agents use it to *initiate* Telegram
messages (escalations, alerts, briefings). The gateway owns the bot token, so no
MCP tool schema enters the context and the credential stays isolated.

Outbound is gated by TELEGRAM_SEND_POLICY (keyed by the gateway's own bot
identity): a `verify` bot queues the message as a pending send that must be
approved on the web gateway's /sends page; a `trust` bot sends directly only with
--user-approved. On a queued send this prints the approval URL.

Examples:
    telegram-push.py "Ari: reply to Mara failed — check scheduler.log"
    telegram-push.py --recipient 123456789 --image /tmp/chart.png "Today's summary"

Configuration (environment):
    TELEGRAM_GATEWAY_SEND_URL   default http://telegram-gateway:8093/send
    TELEGRAM_GATEWAY_TOKEN      optional bearer token (must match the gateway)
    TELEGRAM_DEFAULT_RECIPIENT  optional fallback chat_id when --recipient omitted
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = os.environ.get("TELEGRAM_GATEWAY_SEND_URL", "http://telegram-gateway:8093/send")
TOKEN = os.environ.get("TELEGRAM_GATEWAY_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("TELEGRAM_GATEWAY_TIMEOUT", "60"))


def _encode_image(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"telegram-push: image not found: {path}")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"filename": path.name, "data": data}


def main() -> int:
    parser = argparse.ArgumentParser(description="Push a Telegram message via the telegram-gateway.")
    parser.add_argument("message", nargs="?", default="", help="message body")
    parser.add_argument("--recipient", help="Telegram chat_id (numeric) or @username. "
                                            "Defaults to the gateway's configured recipient.")
    parser.add_argument("--image", action="append", default=[], metavar="PATH",
                        help="attach an image (repeatable)")
    parser.add_argument("--user-approved", action="store_true",
                        help="assert that the user has already approved this send; "
                             "bypasses the verify flow for 'trust'-category bots")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"gateway send URL (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    args = parser.parse_args()

    if not args.message and not args.image:
        parser.error("provide a message and/or at least one --image")

    payload: dict = {"message": args.message}
    if args.recipient:
        payload["recipient"] = args.recipient
    elif os.environ.get("TELEGRAM_DEFAULT_RECIPIENT", "").strip():
        payload["recipient"] = os.environ["TELEGRAM_DEFAULT_RECIPIENT"].strip()
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
            print(f"telegram-push: send queued for approval (id={body.get('request_id', '?')})")
            approval_url = body.get("approval_url", "")
            if approval_url:
                print(f"telegram-push: approve or deny at {approval_url}")
            note = body.get("note", "")
            if note:
                print(f"telegram-push: {note}")
            return 0
        print(f"telegram-push: sent to {body.get('recipient', '?')}")
        return 0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("error", "")
        except ValueError:
            detail = raw.strip()[:200]
        print(f"telegram-push: gateway returned {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"telegram-push: could not reach gateway at {args.url}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
