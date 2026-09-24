#!/usr/bin/env python3
"""Push an outbound SMS through the sms-gateway.

The retinue-side client for the gateway's `/send` endpoint — the SMS sibling of
telegram-push.py. The gateway holds the SMS server's credentials, so this CLI
carries only a capability token. SMS is text only (no --image).

Outbound is gated by SMS_SEND_POLICY (keyed by the gateway's own sending number,
not the recipient): a `verify` account queues the message as a pending send that
must be approved on the web gateway's /sends page; a `trust` account sends
directly only with --user-approved. On a queued send this prints the approval URL.

Examples:
    sms-push.py "Ari: reply to Mara failed — check scheduler.log"
    sms-push.py --recipient +41791234567 "Running ten minutes late"
    sms-push.py --reply-to <token> "Thanks, see you then"

Configuration (environment):
    SMS_GATEWAY_SEND_URL   default http://sms-gateway:8095/send
    SMS_GATEWAY_TOKEN      optional bearer token (must match the gateway)
    SMS_DEFAULT_RECIPIENT  optional fallback number when --recipient omitted
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("SMS_GATEWAY_SEND_URL", "http://sms-gateway:8095/send")
TOKEN = os.environ.get("SMS_GATEWAY_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("SMS_GATEWAY_TIMEOUT", "60"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Push an SMS via the sms-gateway.")
    parser.add_argument("message", help="message body")
    parser.add_argument("--recipient", help="phone number (E.164, e.g. +41791234567). "
                                            "Defaults to the gateway's configured recipient.")
    parser.add_argument("--reply-to", metavar="TOKEN",
                        help="reply-token from a forwarded inbox message; addresses the reply "
                             "back to the number it arrived from. Overrides --recipient.")
    parser.add_argument("--user-approved", action="store_true",
                        help="assert that the user has already approved this send; "
                             "bypasses the verify flow for 'trust'-category accounts")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"gateway send URL (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    args = parser.parse_args()

    if not args.message.strip():
        parser.error("provide a non-empty message")

    payload: dict = {"message": args.message}
    if args.reply_to:
        payload["reply_to"] = args.reply_to
    elif args.recipient:
        payload["recipient"] = args.recipient
    elif os.environ.get("SMS_DEFAULT_RECIPIENT", "").strip():
        payload["recipient"] = os.environ["SMS_DEFAULT_RECIPIENT"].strip()
    if args.user_approved:
        payload["user_approved"] = True

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
            print(f"sms-push: send queued for approval (id={body.get('request_id', '?')})")
            approval_url = body.get("approval_url", "")
            # Absolutize a relative approval path, as telegram-push.py does.
            if approval_url.startswith("/"):
                base = (os.environ.get("SEND_APPROVAL_BASE_URL")
                        or os.environ.get("CONVERSATION_BASE_URL", "")).rstrip("/")
                if base:
                    approval_url = base + approval_url
            if approval_url:
                print(f"sms-push: approve or deny at {approval_url}")
            note = body.get("note", "")
            if note:
                print(f"sms-push: {note}")
            return 0
        print(f"sms-push: queued to {body.get('recipient', '?')} "
              f"(the phone sends it on its next contact with the SMS server)")
        return 0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("error", "")
        except ValueError:
            detail = raw.strip()[:200]
        print(f"sms-push: gateway returned {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"sms-push: could not reach gateway at {args.url}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
