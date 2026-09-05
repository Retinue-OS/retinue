#!/usr/bin/env python3
"""Open a conversation tab in the Retinue dashboard from a retinue agent.

A "conversation tab" is a chat thread with Ara shown on the dashboard. Besides
threads the user starts, an agent can *initiate* one when it hits a decision it
should not make alone — e.g. an RSVP, an ambiguous e-mail, a calendar clash:

    conversation-push.py --title "Party RSVP" "You've got an invitation to Mara's birthday party on Saturday. Shall I confirm you'll attend and add it to your agenda, or politely decline? [[chip: Confirm | Yes, confirm and add it to my agenda.]] · [[chip: Decline | Please decline politely.]]"

Message text is rendered as Markdown in the dashboard; compose it per the
dashboard-composing skill (.claude/skills/dashboard-composing/SKILL.md): a
click-to-fill [[chip: Label | prefill]] for every offered option, linked
PR/issue labels, and no bare URLs — always [label](url).

Use --attach to hand the user a file to download from the thread (e.g. an
e-mail attachment forwarded into the dashboard):

    conversation-push.py --title "Invoice" --attach /tmp/BEL14603717.PDF \
        "Here's the Eier Meier invoice — CHF 57.00. PDF attached to download."

Use --thread to append to a thread that already exists, instead of opening a new
one — so a file lands in the conversation the user is already reading:

    conversation-push.py --thread 42ecb0113a3d48ac87be514cfaf99a7c \
        --attach /tmp/termine.ics "Here are the appointments as an .ics file."

Appending this way un-archives the thread, so news filed into an archived thread
is actually seen — unless the thread is *muted*, which is how "archive this and
keep it archived" is expressed. Set the flags (no message) with:

    conversation-push.py --thread 42ecb0113a3d48ac87be514cfaf99a7c --archive --mute

Use --context to hand machine-usable context to the Ara sessions that will
later serve this thread, without showing it to the user — canonically the
exact reply command (reply token included) for a proposed messenger reply:

    conversation-push.py --title "WhatsApp von Mara" \
        --context 'Reply via: python3 /workspace/scripts/whatsapp-push.py --reply-to <token> "<text>"' \
        "Neue Nachricht von Mara: … Entwurf-Antwort: … Senden, anpassen oder verwerfen?"

The context is stored on the message and replayed in every later engage of the
thread, so the session acting on the user's approval replies by token instead
of re-resolving the sender's name (which can land on the wrong account).

The thread appears on the dashboard with an unread badge; when the user replies,
Ara picks up the thread (with full context) and carries out what they approve.

**Say how much it matters.** The dashboard's home screen is an attention list
(docs/attention-model.md): every thread carries an importance (0–5), an
optional deadline against a lead time, a sphere and tags, and the gateway
decides from those and the user's focus mode whether the thread rings now, waits
for the next digest, or is merely listed. Declare them, or the thread gets the
mid importance and no deadline — listed, never pushed:

    conversation-push.py --title "Quote for Müller AG" --importance 4 \
        --due 2026-09-04T17:00 --kind "customer request" --sphere customers \
        --tag finance "Draft ready for review; Müller expects it today by 17:00."

    --importance 0–5 · --due ISO date-time or YYYY-MM-DD (17:00 that day)
    --lead 2h|3d|2w (else the kind's default) · --sphere customers|admin|health|
    friends|family|system · --tag (repeatable) · --kind "appointment"|"tax filing"|…
    --critical (rings in every mode; declare, never infer) · --actor NAME (parked
    on someone: listed under Waiting, not pushed)

The reply reports the decision (`attention.delivery`: push / hold / list /
waiting, with the reason and, for a hold, until when) — relay it honestly when
the user would otherwise assume they were notified.

A request that times out has not necessarily failed — the gateway may have
committed the write and lost only the response. Opening a thread is therefore
always keyed (with `--key`, or a throwaway key when none is given) and retried
once, so the retry is handed the thread the first attempt opened instead of
raising a second one. Treat a non-zero exit as "the user has not seen this":
alert, or leave the item for the next run — never re-push a shortened version.

This is the retinue-side client for the gateway's token-gated
`/internal/conversations` endpoint — analogous to signal-push.py for Signal.
The token keeps the endpoint reachable only from in-container agents, not from
the authenticated-but-public dashboard.

Configuration (environment):
    CONVERSATION_BACKEND_URL    default http://localhost:${WEB_GATEWAY_PORT}/internal/conversations
    CONVERSATION_BACKEND_TOKEN  shared secret gating the endpoint (set by the entrypoint)
"""
import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

_PORT = os.environ.get("WEB_GATEWAY_PORT", "8080")
DEFAULT_URL = os.environ.get(
    "CONVERSATION_BACKEND_URL", f"http://localhost:{_PORT}/internal/conversations"
)
TOKEN = os.environ.get("CONVERSATION_BACKEND_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("CONVERSATION_BACKEND_TIMEOUT", "30"))
_THREAD_ID_RE = re.compile(r"[0-9a-f]{32}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Open a dashboard conversation tab with the user."
    )
    parser.add_argument("message", nargs="?", default="",
                        help="the message/question to show the user")
    parser.add_argument("--title", help="short tab title (derived from the message if omitted)")
    parser.add_argument("--thread", metavar="ID",
                        help="append to this existing thread instead of opening a new one")
    parser.add_argument("--archive", dest="archived", action="store_true", default=None,
                        help="archive --thread (drop it from the active list)")
    parser.add_argument("--unarchive", dest="archived", action="store_false",
                        help="restore --thread to the active list")
    parser.add_argument("--mute", dest="muted", action="store_true", default=None,
                        help="mute --thread: keep it where it is when news is filed into it")
    parser.add_argument("--unmute", dest="muted", action="store_false",
                        help="unmute --thread")
    parser.add_argument("--on-behalf-of", dest="on_behalf_of",
                        help="requester identity that owns the thread (defaults to the dashboard user)")
    parser.add_argument("--agent",
                        help="subagent name to show as the message sender (e.g. Coach), "
                             "when a relay answers on its behalf")
    parser.add_argument("--key", metavar="KEY",
                        help="idempotency key: opening a thread twice under the "
                             "same key reuses the first one instead of creating "
                             "a duplicate, so a re-run of this turn — an "
                             "escalation, a redelivered message — cannot open a "
                             "second thread. For a messenger message, pass the "
                             "thread_key the gateway handed you (in the forwarded "
                             "prompt, or on the drained record) VERBATIM; never "
                             "build one from the channel's own message id, which "
                             "is unique only within a chat and would collide "
                             "across chats and accounts. Otherwise pass any "
                             "identity that names this subject globally and the "
                             "same way every time. New threads only; ignored "
                             "with --thread. Omitting it still opens the thread "
                             "under a throwaway key, so a timed-out attempt "
                             "cannot be retried into a second thread — but that "
                             "key is fresh every run and dedupes nothing across "
                             "runs, which is what a real key is for.")
    parser.add_argument("--context", metavar="TEXT",
                        help="agent-only context stored with the message: replayed to every "
                             "later Ara session in this thread but never rendered to the "
                             "user. Canonical use: the exact reply command (reply token "
                             "included) for a proposed messenger reply, so the session "
                             "acting on the user's approval replies by token instead of "
                             "resolving the sender's name")
    parser.add_argument("--attach", action="append", default=[], metavar="PATH",
                        help="attach a file the user can download from the thread (repeatable)")
    att = parser.add_argument_group("attention", "how much this matters (docs/attention-model.md)")
    att.add_argument("--importance", type=float, metavar="0-5",
                     help="importance 0–5; 4+ is active without a deadline, 2–3 needs one to ring")
    att.add_argument("--due", metavar="WHEN",
                     help="deadline: ISO date-time, or YYYY-MM-DD for 17:00 that day")
    att.add_argument("--lead", metavar="SPAN",
                     help="lead time before the deadline in which it becomes urgent: 90m, 2h, 3d, 2w "
                          "(default: the kind's)")
    att.add_argument("--sphere", metavar="NAME",
                     help="the primary sphere: customers, admin, health, friends, family, system")
    att.add_argument("--tag", action="append", default=[], metavar="NAME",
                     help="a further sphere this also belongs to (repeatable)")
    att.add_argument("--kind", metavar="LABEL",
                     help="the kind of item, which supplies the lead-time default: "
                          "\"customer request\", \"invitation\", \"appointment\", \"tax filing\", …")
    att.add_argument("--critical", action="store_true",
                     help="declared critical: rings in every mode, Off included")
    att.add_argument("--actor", metavar="NAME",
                     help="who the thread waits on when not the user (listed under Waiting, not pushed)")
    parser.add_argument("--url", default=None, help=f"endpoint URL (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    args = parser.parse_args()

    message = args.message.strip()
    flags_only = args.archived is not None or args.muted is not None
    if not message and not args.attach and not flags_only:
        print("conversation-push: empty message", file=sys.stderr)
        return 2
    if not TOKEN:
        print("conversation-push: CONVERSATION_BACKEND_TOKEN is not set", file=sys.stderr)
        return 2

    if args.thread and not _THREAD_ID_RE.fullmatch(args.thread):
        print(f"conversation-push: not a thread id: {args.thread}", file=sys.stderr)
        return 2
    if args.thread and args.title:
        print("conversation-push: --title applies only to a new thread", file=sys.stderr)
        return 2
    if flags_only and not args.thread:
        print("conversation-push: --archive/--mute need --thread", file=sys.stderr)
        return 2
    # Flag changes are their own request; mixing them with a message would make
    # the ordering (does the append wake the thread before or after the mute?)
    # implicit. Send the flags first, then the message.
    if flags_only and (message or args.attach):
        print("conversation-push: --archive/--mute cannot be combined with a message",
              file=sys.stderr)
        return 2
    # Context rides with a message; on a flags-only call there is no message
    # to carry it.
    if flags_only and args.context:
        print("conversation-push: --context cannot be combined with --archive/--mute",
              file=sys.stderr)
        return 2

    url = args.url or DEFAULT_URL
    if args.thread and not args.url:
        suffix = "flags" if flags_only else "messages"
        url = f"{DEFAULT_URL.rstrip('/')}/{args.thread}/{suffix}"

    payload: dict = {}
    if flags_only:
        if args.archived is not None:
            payload["archived"] = args.archived
        if args.muted is not None:
            payload["muted"] = args.muted
    else:
        payload["message"] = message
        if args.agent:
            payload["agent"] = args.agent
        if args.context:
            payload["context"] = args.context
        # Only a new thread can be deduplicated; appending to a named thread is
        # already addressed by its id. Without a caller-supplied key, open under
        # a throwaway one anyway: it cannot dedupe a later run (it is generated
        # fresh each time), but it makes *this* run's retry-after-timeout land
        # in the thread the first attempt opened.
        if not args.thread:
            payload["key"] = args.key or f"auto:{uuid.uuid4().hex}"
            payload["key_ephemeral"] = not args.key
    if args.title:
        payload["title"] = args.title
    attention = {}
    if args.importance is not None:
        if not 0 <= args.importance <= 5:
            print("conversation-push: --importance must be between 0 and 5", file=sys.stderr)
            return 2
        attention["importance"] = args.importance
    if args.due:
        attention["due"] = args.due
    if args.lead:
        attention["lead"] = args.lead
    if args.sphere:
        attention["sphere"] = args.sphere
    if args.tag:
        attention["tags"] = args.tag
    if args.kind:
        attention["kind"] = args.kind
    if args.critical:
        attention["critical"] = True
    if args.actor:
        attention["actor"] = args.actor
    if attention:
        if flags_only:
            print("conversation-push: attention flags cannot be combined with --archive/--mute",
                  file=sys.stderr)
            return 2
        payload["attention"] = attention
    if args.on_behalf_of:
        payload["on-behalf-of"] = args.on_behalf_of
    if args.attach:
        attachments = []
        for spec in args.attach:
            path = Path(spec)
            if not path.is_file():
                print(f"conversation-push: attachment not found: {spec}", file=sys.stderr)
                return 2
            attachments.append({
                "filename": path.name,
                "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            })
        payload["attachments"] = attachments

    data = json.dumps(payload).encode("utf-8")

    def post() -> dict:
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Conversation-Backend-Token": TOKEN,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # A timed-out request is not a failed one: the gateway may have committed
    # the write and lost only the response. An agent that reads that as
    # "rejected" retries with less and less content, and the user ends up with
    # several threads of which the last is the poorest — so retry here instead,
    # where a repeat is provably harmless. A keyed open is folded into the
    # thread the first attempt raised, and a flags-only call is idempotent by
    # nature. A bare append has neither guarantee: it reports the ambiguity
    # rather than risking the same message twice.
    idempotent = bool(payload.get("key")) or flags_only
    retried = False
    while True:
        try:
            body = post()
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            print(f"conversation-push: HTTP {exc.code}: {detail}", file=sys.stderr)
            return 1
        except (urllib.error.URLError, OSError) as exc:
            if idempotent and not retried:
                retried = True
                print(f"conversation-push: request failed ({exc}); retrying once — "
                      "the first attempt may have landed", file=sys.stderr)
                continue
            print(f"conversation-push: request failed: {exc}", file=sys.stderr)
            if not idempotent:
                print("conversation-push: the message may still have been posted — "
                      "check the thread before sending it again", file=sys.stderr)
            return 1

    print(json.dumps(body, ensure_ascii=False))
    if body.get("deduplicated"):
        if retried and payload.get("key_ephemeral"):
            print("conversation-push: the timed-out attempt had landed after all — "
                  "reusing its thread, nothing was posted twice", file=sys.stderr)
        else:
            print("conversation-push: this key already opened a thread — reusing it, "
                  "nothing was posted", file=sys.stderr)
    decision = body.get("attention") or {}
    if decision.get("delivery") and decision["delivery"] != "push":
        until = decision.get("until")
        print("conversation-push: attention — "
              f"{decision['delivery']}" + (f" until {until}" if until else "")
              + f" ({decision.get('level', '')}): {decision.get('reason', '')}. "
              "The user has NOT been notified; the thread is on their list.",
              file=sys.stderr)
    if body.get("push_subscribers") == 0:
        print("conversation-push: warning — no device is subscribed to push; "
              "this thread will only be seen if the dashboard is opened",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
