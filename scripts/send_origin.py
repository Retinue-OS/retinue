"""Link a queued send to the dashboard thread that asked for it.

A send whose policy is `verify` (or `trust` without --user-approved) waits on
/sends for the user's Allow or Deny. When it was queued by a dashboard-thread
turn, the decision is reported back into that thread, so the conversation
shows what became of the message it proposed. The thread travels with the
request: the push CLIs send it as ``thread`` in the /send (or /create-event)
body, the gateway keeps it on the pending entry, and the web-gateway reads it
back when the user decides (``_report_send_decision`` there).

The default is RETINUE_THREAD_ID, which the web-gateway sets for every turn it
spawns for a thread (scripts/session_env.py); ``--thread`` names one
explicitly — a scheduled job that opened a thread and queues the send it
proposes there, for instance. A value that is not a thread id is dropped here
rather than failing the send: the link is a courtesy, the send is the point.
"""
import os
import re

THREAD_ENV = "RETINUE_THREAD_ID"
_THREAD_ID_RE = re.compile(r"[0-9a-f]{32}")


def valid_thread(value) -> str | None:
    """*value* as a thread id, or None when it is not one."""
    value = str(value or "").strip().lower()
    return value if _THREAD_ID_RE.fullmatch(value) else None


def add_argument(parser) -> None:
    """Add ``--thread`` (default: RETINUE_THREAD_ID) to a push CLI's parser."""
    parser.add_argument(
        "--thread", default=os.environ.get(THREAD_ENV, ""), metavar="THREAD_ID",
        help="dashboard thread to report the user's Allow/Deny of a queued send "
             f"into (default: ${THREAD_ENV}, set in dashboard-thread turns)")


def stamp(payload: dict, thread) -> dict:
    """Add the thread to a /send payload when it is a valid id."""
    tid = valid_thread(thread)
    if tid:
        payload["thread"] = tid
    return payload
