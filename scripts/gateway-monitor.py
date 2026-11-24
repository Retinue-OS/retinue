#!/usr/bin/env python3
"""Channel gateway connection monitor.

The Signal / WhatsApp / Telegram gateways occasionally lose their link to the
messaging service (the phone unlinks the device, the session gets revoked, …),
and the mail gateway its IMAP login (the server refuses the password, or is
unreachable). Without monitoring, nobody notices until a correspondent
complains that their messages go unanswered. This daemon closes that gap:

  * Every GATEWAY_MONITOR_INTERVAL seconds (default 60) it polls the /health
    endpoint of every configured channel gateway — the same registry the
    web-gateway uses for /sends and /gateways (see messenger_gateways.py).
  * After GATEWAY_MONITOR_FAILURES consecutive bad checks (default 2, debouncing
    restarts and transient blips) it notifies the user through the existing
    inbound-message mechanism: it opens a dashboard conversation via the
    token-gated /internal/conversations endpoint, which fans out a Web Push
    notification to the user's devices — exactly like a new incoming message.
    The notice links to the /gateways page, where the user sees the gateway's
    status and, for a messenger, the pairing QR code to scan. What the notice
    says follows the gateway's channel (see WORDING): a mailbox has no phone
    to re-pair. And it follows what the gateway says holds it: one whose link
    is fine and which waits for the deployment's setup (``blocked`` in its
    /health — the mail gateway waiting for the e-mail policy file) is
    reported as that, never as a lost connection.
  * While a gateway stays down it re-reminds in the same thread every
    GATEWAY_MONITOR_REMIND_SECONDS (default 6 h); when the link comes back it
    records the recovery in that thread quietly — no push, no unread badge,
    and an archived thread stays archived.

Gateways that report ``configured: false`` (a channel the deployment simply
does not use) are skipped, as are slugs listed in GATEWAY_MONITOR_IGNORE.
The daemon is forked by the entrypoint in remote-control mode; it keeps no
Claude session and costs no credits — it is plain HTTP polling.

State (per-gateway status, open thread id, notification timestamps) persists in
GATEWAY_MONITOR_STATE_DIR (default /root/.retinue/gateway-monitor, on the
persistent /root volume) so a container restart neither re-notifies a known
outage nor forgets an open incident thread.
"""
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conversation_notify  # noqa: E402
import messenger_gateways  # noqa: E402
from conversation_notify import ConversationNotifier  # noqa: E402

LOG = "[gateway-monitor]"
# How the outage thread presents itself to the dashboard's attention model
# (docs/attention-model.md): a system alert of high importance, urgent within
# the hour. It rings in modes that admit the system sphere and waits for the
# next digest in the others — a dead channel is serious, not critical: nothing
# is lost, messages queue at the channel until it is back.
OUTAGE_ATTENTION = {"importance": 5, "sphere": "system", "kind": "system alert", "lead": "60m"}


INTERVAL = float(os.environ.get("GATEWAY_MONITOR_INTERVAL", "") or "60")
# Consecutive failed checks before the user is notified (debounce).
FAIL_THRESHOLD = int(os.environ.get("GATEWAY_MONITOR_FAILURES", "") or "2")
# While a gateway stays down, re-remind in the incident thread this often.
REMIND_SECONDS = float(os.environ.get("GATEWAY_MONITOR_REMIND_SECONDS", "") or str(6 * 3600))
HEALTH_TIMEOUT = float(os.environ.get("GATEWAY_MONITOR_HEALTH_TIMEOUT", "") or "10")
# Comma-separated slugs to exclude from monitoring (e.g. a deliberately
# unlinked channel the deployment keeps around).
IGNORE = {s.strip() for s in os.environ.get("GATEWAY_MONITOR_IGNORE", "").split(",") if s.strip()}

STATE_DIR = Path(os.environ.get("GATEWAY_MONITOR_STATE_DIR", "") or "/root/.retinue/gateway-monitor")
STATE_PATH = STATE_DIR / "state.json"

# Where user-facing links point: the public dashboard base URL. Reuses the same
# deployment settings the send-approval links use, so no new config is needed.
# CONVERSATION_BASE_URL is the last fallback because it is the one always set in
# this container (same chain as signal-push.py / email_client.py) — without it
# the notice would carry a site-root path, which is not clickable in a thread.
PUBLIC_BASE_URL = os.environ.get("GATEWAY_MONITOR_PUBLIC_BASE_URL", "").rstrip("/") \
    or os.environ.get("SEND_APPROVAL_BASE_URL", "").rstrip("/") \
    or os.environ.get("CONVERSATION_BASE_URL", "").rstrip("/")


def classify_health(status: int | None, body: dict | None, error: str | None = None):
    """Reduce one health check to (verdict, reason).

    verdict is one of:
      "up"           — the gateway reports a working link
      "down"         — reachable but the link is broken (or gateway unreachable)
      "blocked"      — reachable, and not working for a reason that is not its
                       link: it waits for something the deployment's setup
                       owes it, and its ``error`` says what
      "unconfigured" — the channel is deliberately not set up; never notify
    """
    if error is not None:
        return "down", f"gateway unreachable: {error}"
    if status != 200 or not isinstance(body, dict):
        return "down", f"health check returned HTTP {status}"
    if body.get("configured") is False:
        return "unconfigured", None
    # A gateway that doesn't report link state (older build) counts as up as
    # long as it answers — no false alarms during a rolling upgrade.
    if body.get("connected") is False:
        reason = str(body.get("error") or "connection lost")
        return ("blocked" if body.get("blocked") else "down"), reason
    return "up", None


def repair_url() -> str:
    path = "/gateways"
    return (PUBLIC_BASE_URL + path) if PUBLIC_BASE_URL else path


def repair_link(label: str = "the gateways page") -> str:
    """The repair target as a Markdown link — never a bare URL or path.

    These messages land in a dashboard conversation, which renders Markdown:
    a bare "/gateways" is plain text there (not clickable, and read aloud as
    text), so the link target always carries a short human label instead. See
    the dashboard-composing skill.
    """
    return f"[{label}]({repair_url()})"


# What the user is told, per kind of gateway. A messenger gateway is a linked
# device: it goes down because the phone unlinked it or the session was
# revoked, and the fix is a QR scan on /gateways. The mail gateway logs in to
# an IMAP server: it goes down because the server refused the login or cannot
# be reached, and no phone or QR code has anything to do with it — telling the
# user to re-pair would send them looking for a fix that does not exist. Every
# notice below takes its channel-specific words from here, so they live in one
# place. Keyed by the registry's "channel"; anything else reads as a messenger.
_MESSENGER_WORDING = {
    "noun": "messenger gateway",
    "lost": ("incoming messages on this channel are NOT being received and "
             "outgoing ones cannot be sent."),
    "cause": ("It most likely needs to be re-paired with the phone. Open {link} "
              "to see the gateway status and scan the pairing QR code."),
    "still": "Messages on this channel are not flowing.",
    "remedy": "Re-pair it on {link}.",
}
WORDING = {
    "email": {
        # The label is "E-mail", so the bare noun reads right: "The E-mail
        # gateway …".
        "noun": "gateway",
        "lost": ("new mail on this account is NOT reaching the chats. Nothing is "
                 "lost: it stays in the mailbox and is mirrored once the gateway "
                 "is back."),
        "cause": ("The mail server most likely refused the gateway's login (a "
                  "changed or revoked app password) or cannot be reached. Check "
                  "the account's mailbox settings and the provider's status; "
                  "{link} shows what the gateway reports."),
        "still": "New mail is not reaching the chats.",
        "remedy": ("Check the mailbox login and the mail server; {link} shows "
                   "what the gateway reports."),
    },
}


# A blocked gateway (see classify_health) has not lost anything: its link is
# fine and it waits for the deployment's setup. What is missing is the
# gateway's to say — its reported problem names it and where to look — so
# these words blame no login, no server and no phone, and every notice,
# reminders included, carries the reported problem.
_BLOCKED_CAUSE = ("Its connection is fine. What it waits for is part of the deployment's "
                  "setup, not of the account, and the reported problem says what; {link} "
                  "shows what the gateway reports.")


def wording(channel: str | None) -> dict:
    """The words for a gateway serving this registry channel (a messenger's
    when the channel is unknown or not stated)."""
    return WORDING.get((channel or "").strip().lower(), _MESSENGER_WORDING)


def outage_title(label: str, blocked: bool = False) -> str:
    return f"{label} gateway waiting for its setup" if blocked else f"{label} gateway disconnected"


def outage_message(label: str, reason: str | None, since: float | None = None,
                   channel: str | None = None, blocked: bool = False) -> str:
    words = wording(channel)
    if blocked:
        lines = [f"The {label} {words['noun']} is not running: it is waiting for its "
                 f"setup — {words['lost']}"]
    else:
        lines = [f"The {label} {words['noun']} has lost its connection — {words['lost']}"]
    if reason:
        lines.append(f"Reported problem: {reason}")
    lines.append((_BLOCKED_CAUSE if blocked else words["cause"]).format(link=repair_link()))
    if since:
        mins = max(1, int((time.time() - since) // 60))
        lines.insert(1, f"It has been waiting for about {mins} minute(s)." if blocked
                     else f"The connection has been down for about {mins} minute(s).")
    return "\n\n".join(lines)


def reminder_message(label: str, since: float, channel: str | None = None,
                     reason: str | None = None, blocked: bool = False) -> str:
    words = wording(channel)
    hours = max(1, int((time.time() - since) // 3600))
    if blocked:
        return (
            f"Reminder: the {label} {words['noun']} is still waiting for its setup "
            f"(for about {hours} hour(s)). {words['still']} "
            + (f"Reported problem: {reason}. " if reason else "")
            + f"{repair_link()} shows what the gateway reports."
        )
    return (
        f"Reminder: the {label} {words['noun']} is still disconnected "
        f"(for about {hours} hour(s)). {words['still']} "
        f"{words['remedy'].format(link=repair_link())}"
    )


def recovery_message(label: str, channel: str | None = None, blocked: bool = False) -> str:
    return (f"The {label} {wording(channel)['noun']} is "
            f"{'running' if blocked else 'connected'} again. No further action needed.")


class MonitorEngine:
    """Per-gateway outage state machine, separated from I/O for testability.

    ``step(slug, label, verdict, reason, now, channel)`` folds one health
    verdict into the persistent state and performs any due notification via
    the injected notifier; ``channel`` (the registry's) picks the wording.
    A "blocked" gateway runs through the same states as one that is down —
    debounced, one thread, reminders, a recovery — in its own words.
    State per slug:
      status      "up" | "down"
      blocked     the last bad check said "blocked", not "down"
      fails       consecutive bad checks while still officially up
      since       when the outage began (first failed check of the streak)
      thread_id   dashboard conversation carrying this incident
      notified    the outage notice actually reached the backend
      last_notice timestamp of the last message posted for this outage
    """

    def __init__(self, notifier, state: dict | None = None,
                 fail_threshold: int = FAIL_THRESHOLD, remind_seconds: float = REMIND_SECONDS):
        self.notifier = notifier
        self.state = state if state is not None else {}
        self.fail_threshold = fail_threshold
        self.remind_seconds = remind_seconds

    def step(self, slug: str, label: str, verdict: str, reason: str | None,
             now: float | None = None, channel: str | None = None) -> None:
        if verdict == "unconfigured":
            self.state.pop(slug, None)
            return
        now = time.time() if now is None else now
        entry = self.state.setdefault(slug, {"status": "up", "fails": 0})

        if verdict == "up":
            if entry.get("status") == "down":
                if entry.get("notified") and entry.get("thread_id"):
                    self.notifier.append(
                        entry["thread_id"],
                        recovery_message(label, channel, blocked=bool(entry.get("blocked"))),
                        quiet=True)
                print(f"{LOG} {slug}: recovered", flush=True)
            self.state[slug] = {"status": "up", "fails": 0}
            return

        # verdict == "down" or "blocked"
        blocked = verdict == "blocked"
        entry["fails"] = int(entry.get("fails", 0)) + 1
        entry["reason"] = reason
        entry["blocked"] = blocked
        if entry.get("status") != "down":
            if entry["fails"] == 1:
                entry["since"] = now
            if entry["fails"] < self.fail_threshold:
                return
            entry["status"] = "down"
            print(f"{LOG} {slug}: {'BLOCKED' if blocked else 'DOWN'} ({reason})", flush=True)

        if not entry.get("notified"):
            thread_id = self.notifier.open_thread(
                outage_title(label, blocked),
                outage_message(label, reason, entry.get("since"), channel, blocked=blocked),
                attention=OUTAGE_ATTENTION,
            )
            if thread_id:
                entry["thread_id"] = thread_id
                entry["notified"] = True
                entry["last_notice"] = now
            # else: backend unreachable — retry on the next tick
            return

        if now - float(entry.get("last_notice", 0)) >= self.remind_seconds:
            if entry.get("thread_id") and self.notifier.append(
                    entry["thread_id"], reminder_message(label, entry.get("since", now),
                                                         channel, reason=reason,
                                                         blocked=blocked)):
                entry["last_notice"] = now


def _load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except OSError as exc:
        print(f"{LOG} could not persist state: {exc}", flush=True)


def check_gateway(gw: dict) -> tuple[str, str | None]:
    """GET <base_url>/health and classify the result."""
    url = f"{gw['base_url']}/health"
    headers = {}
    if gw.get("token"):
        headers["Authorization"] = "Bearer " + gw["token"]
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return classify_health(resp.status, body)
    except urllib.error.HTTPError as exc:
        return classify_health(exc.code, None)
    except Exception as exc:  # noqa: BLE001 - unreachable counts as down
        return classify_health(None, None, error=str(exc))


def main() -> None:
    if not conversation_notify.DEFAULT_BACKEND_TOKEN:
        print(f"{LOG} CONVERSATION_BACKEND_TOKEN is not set — outage notifications "
              f"cannot be delivered; monitoring runs log-only", flush=True)
    gateways = messenger_gateways.channel_gateways(LOG)
    if not gateways:
        print(f"{LOG} no messenger gateways configured — nothing to monitor, exiting", flush=True)
        return
    watched = {slug: gw for slug, gw in gateways.items() if slug not in IGNORE}
    print(f"{LOG} monitoring {', '.join(sorted(watched))} every {INTERVAL:.0f}s "
          f"(threshold {FAIL_THRESHOLD}, remind every {REMIND_SECONDS / 3600:.1f}h)", flush=True)

    engine = MonitorEngine(ConversationNotifier(log_prefix=LOG), state=_load_state())
    while True:
        for slug, gw in watched.items():
            try:
                verdict, reason = check_gateway(gw)
                engine.step(slug, gw.get("label") or slug.title(), verdict, reason,
                            channel=gw.get("channel"))
            except Exception as exc:  # noqa: BLE001 - one gateway must not stall the loop
                print(f"{LOG} error checking {slug}: {exc}\n{traceback.format_exc()}", flush=True)
        _save_state(engine.state)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
