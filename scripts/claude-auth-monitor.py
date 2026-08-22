#!/usr/bin/env python3
"""Claude Code sign-in monitor.

The OAuth sign-in that authenticates every Claude Code process in this
deployment dies in two ways: predictably (the refresh token has a fixed
expiry, recorded in the credentials file) and abruptly (a concurrent session
rotates the tokens and the losing side clears the file; when the entrypoint's
backup is then rejected too, the system is unauthenticated). Both used to be
discovered only when someone noticed the agents had gone quiet — days of
silent outage, then an SSH session to re-login from a console. This daemon
closes both gaps:

  * Every CLAUDE_AUTH_MONITOR_INTERVAL seconds (default 300) it classifies the
    credential files via claude_auth.credential_status() — plain file reads,
    no Claude session, no credits, and deliberately **no token refresh of its
    own** (an out-of-band refresh would race the live session's rotation and
    cause the very clobbering it is meant to prevent).
  * A sign-in that is about to expire (default: within CLAUDE_AUTH_WARN_DAYS
    of the refresh token's recorded expiry) opens a dashboard conversation —
    which Web-Pushes the user's devices — pointing at the /claude-auth page,
    where they can re-login from the browser *before* anything breaks.
  * A broken sign-in (credentials gone and the backup already rejected, or
    the refresh token expired) does the same at a higher cadence, since every
    scheduled job and dashboard turn is failing while it lasts.
  * Recovery (a re-login through the page, the console, or `claude` itself)
    is reported in the same thread.

Deployments that authenticate through a Claude-compatible gateway instead of
OAuth (ANTHROPIC_BASE_URL set, RETINUE_GATEWAY_USES_CLAUDE_OAUTH unset) have
nothing to monitor here; the daemon detects that and exits, like the messenger
gateway monitor does with no gateways. CLAUDE_AUTH_MONITOR=0 disables it
explicitly.

State (current level, open thread id, notification timestamps) persists in
CLAUDE_AUTH_MONITOR_STATE_DIR (default /root/.retinue/claude-auth-monitor, on
the persistent /root volume) so a container restart — including the restart a
re-login itself triggers — neither re-notifies a known incident nor forgets
the thread that carries it.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_auth  # noqa: E402
import conversation_notify  # noqa: E402
from conversation_notify import ConversationNotifier  # noqa: E402

LOG = "[claude-auth-monitor]"

INTERVAL = float(os.environ.get("CLAUDE_AUTH_MONITOR_INTERVAL", "") or "300")
# Consecutive non-ok checks before the user is notified. Claude's own token
# refresh replaces the file atomically, but a rotation-then-restore episode
# passes through transient bad states — two ticks of debounce outlast those.
FAIL_THRESHOLD = int(os.environ.get("CLAUDE_AUTH_MONITOR_FAILURES", "") or "2")
# Reminder cadence while the incident lasts: hours for a broken sign-in
# (everything is failing), a day for a merely expiring one (still working).
REMIND_BROKEN = float(os.environ.get("CLAUDE_AUTH_MONITOR_REMIND_SECONDS", "") or str(6 * 3600))
REMIND_WARN = float(os.environ.get("CLAUDE_AUTH_MONITOR_WARN_REMIND_SECONDS", "") or str(24 * 3600))

STATE_DIR = Path(os.environ.get("CLAUDE_AUTH_MONITOR_STATE_DIR", "")
                 or "/root/.retinue/claude-auth-monitor")
STATE_PATH = STATE_DIR / "state.json"

# Where user-facing links point — same fallback chain as gateway-monitor.py,
# so no new deployment config is needed.
PUBLIC_BASE_URL = os.environ.get("CLAUDE_AUTH_PUBLIC_BASE_URL", "").rstrip("/") \
    or os.environ.get("GATEWAY_MONITOR_PUBLIC_BASE_URL", "").rstrip("/") \
    or os.environ.get("SEND_APPROVAL_BASE_URL", "").rstrip("/") \
    or os.environ.get("CONVERSATION_BASE_URL", "").rstrip("/")


def signin_link(label: str = "the Claude sign-in page") -> str:
    """The re-login target as a Markdown link — never a bare URL or path
    (dashboard conversations render Markdown; see the dashboard-composing
    skill)."""
    return f"[{label}]({PUBLIC_BASE_URL + '/claude-auth' if PUBLIC_BASE_URL else '/claude-auth'})"


def level_of(status: dict) -> str:
    """Reduce a credential_status() dict to ok | warn | broken."""
    state = status.get("state")
    if state == "needs_login":
        return "broken"
    if state in ("expiring", "stale"):
        return "warn"
    return "ok"


def broken_message(reason: str) -> str:
    return (
        "The Claude sign-in of the agent system is broken — scheduled jobs, "
        "dashboard conversations and the remote-control session cannot "
        "authenticate until it is renewed.\n\n"
        f"{reason}\n\n"
        f"Open {signin_link()} to sign in again from this browser (no console "
        "needed). The agent session restarts itself afterwards."
    )


def warn_message(reason: str) -> str:
    return (
        "Heads-up: the Claude sign-in of the agent system needs attention "
        "soon. Everything still works right now.\n\n"
        f"{reason}\n\n"
        f"Open {signin_link()} to renew the sign-in from this browser at a "
        "convenient moment — that avoids the outage entirely."
    )


def reminder_message(level: str, reason: str, since: float, now: float) -> str:
    hours = max(1, int((now - since) // 3600))
    if level == "broken":
        return (f"Reminder: the Claude sign-in is still broken (for about "
                f"{hours} hour(s)); the agents cannot work. {reason}\n\n"
                f"Renew it on {signin_link()}.")
    return (f"Reminder: the Claude sign-in still needs renewal. {reason}\n\n"
            f"Renew it on {signin_link()}.")


def change_message(level: str, reason: str) -> str:
    if level == "broken":
        return ("The situation escalated: the Claude sign-in is now broken and "
                f"the agents cannot authenticate. {reason}\n\n"
                f"Renew it on {signin_link()}.")
    return f"Update on the Claude sign-in: {reason}\n\nRenew it on {signin_link()}."


def recovery_message() -> str:
    return "The Claude sign-in is healthy again. No further action needed."


class AuthMonitorEngine:
    """Folds one credential_status() verdict per tick into persistent state
    and performs any due notification via the injected notifier. Kept free of
    I/O beyond the notifier, so tests drive it with synthetic statuses.

    State:
      level       "ok" | "warn" | "broken"   (last *reported* level)
      streak      consecutive non-ok ticks while still officially ok
      since       when the incident began (first non-ok tick of the streak)
      thread_id   dashboard conversation carrying this incident
      notified    the first notice actually reached the backend
      last_notice timestamp of the last message posted for this incident
    """

    def __init__(self, notifier, state: dict | None = None,
                 fail_threshold: int = FAIL_THRESHOLD,
                 remind_broken: float = REMIND_BROKEN,
                 remind_warn: float = REMIND_WARN):
        self.notifier = notifier
        self.state = state if state is not None else {}
        self.fail_threshold = fail_threshold
        self.remind_broken = remind_broken
        self.remind_warn = remind_warn

    def step(self, status: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        level = level_of(status)
        reason = str(status.get("reason") or "")
        entry = self.state

        if level == "ok":
            if entry.get("level") in ("warn", "broken"):
                if entry.get("notified") and entry.get("thread_id"):
                    self.notifier.append(entry["thread_id"], recovery_message())
                print(f"{LOG} sign-in recovered", flush=True)
            entry.clear()
            entry["level"] = "ok"
            return

        entry["streak"] = int(entry.get("streak", 0)) + 1
        entry.setdefault("since", now)
        if entry.get("level") not in ("warn", "broken"):
            if entry["streak"] < self.fail_threshold:
                return
            print(f"{LOG} sign-in {level.upper()}: {reason}", flush=True)

        if not entry.get("notified"):
            title = ("Claude sign-in broken" if level == "broken"
                     else "Claude sign-in expires soon")
            message = broken_message(reason) if level == "broken" else warn_message(reason)
            thread_id = self.notifier.open_thread(title, message)
            if thread_id:
                entry.update(thread_id=thread_id, notified=True,
                             last_notice=now, level=level)
            # else: backend unreachable — retry on the next tick
            return

        if level != entry.get("level"):
            # warn → broken (the predicted outage arrived) or broken → warn
            # (partial recovery): say so right away, in the same thread.
            if self.notifier.append(entry["thread_id"], change_message(level, reason)):
                entry.update(level=level, last_notice=now)
            return

        remind_after = self.remind_broken if level == "broken" else self.remind_warn
        if now - float(entry.get("last_notice", 0)) >= remind_after:
            if self.notifier.append(entry["thread_id"],
                                    reminder_message(level, reason, entry.get("since", now), now)):
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


def main() -> None:
    if os.environ.get("CLAUDE_AUTH_MONITOR", "").strip() == "0":
        print(f"{LOG} disabled via CLAUDE_AUTH_MONITOR=0, exiting", flush=True)
        return
    if not claude_auth.oauth_in_use():
        print(f"{LOG} deployment authenticates via a Claude-compatible gateway, "
              f"not OAuth — nothing to monitor, exiting", flush=True)
        return
    if not conversation_notify.DEFAULT_BACKEND_TOKEN:
        print(f"{LOG} CONVERSATION_BACKEND_TOKEN is not set — notifications "
              f"cannot be delivered; monitoring runs log-only", flush=True)
    print(f"{LOG} watching {claude_auth.CRED_FILE} every {INTERVAL:.0f}s "
          f"(warn {claude_auth.WARN_DAYS:g} day(s) before sign-in expiry)", flush=True)

    engine = AuthMonitorEngine(ConversationNotifier(log_prefix=LOG), state=_load_state())
    while True:
        try:
            engine.step(claude_auth.credential_status())
            _save_state(engine.state)
        except Exception as exc:  # noqa: BLE001 - one bad tick must not kill the daemon
            print(f"{LOG} tick failed: {exc}\n{traceback.format_exc()}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
