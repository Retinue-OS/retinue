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
  * After GATEWAY_MONITOR_FAILURES consecutive bad checks (default 5, about
    five minutes, debouncing restarts and transient blips) it notifies the user through the existing
    inbound-message mechanism: it opens a dashboard conversation via the
    token-gated /internal/conversations endpoint, which fans out a Web Push
    notification to the user's devices — exactly like a new incoming message.
    The notice links to the /gateways page, where the user sees the gateway's
    status and, for a messenger, the pairing QR code to scan. What the notice
    says follows the gateway's channel (see WORDING): a mailbox has no phone
    to re-pair. And it follows what the gateway says holds it (``blocked`` in
    its /health): one whose link is fine and which waits for the deployment's
    setup (the mail gateway waiting for the e-mail policy file), or which
    stopped on a fault of its own (the mail mirror that cannot read or write
    mail), is reported as that, never as a lost connection.
  * While a gateway stays down it re-reminds in the same thread every
    GATEWAY_MONITOR_REMIND_SECONDS (default 6 h); when the link comes back it
    records the recovery in that thread quietly — no push, no unread badge —
    and archives and mutes it, the alert being obsolete (a thread the user
    starred is left where it is); a later outage opens a thread of its own. An outage that turns into one of
    another kind — the gateway that waited for its setup now cannot log in —
    is a second fault, and is told in that thread when it is seen (debounced
    like the first), not at the next reminder.
  * A gateway that works may still say that it cannot do part of its work:
    the mail gateway counts the mail it could not read and shows as
    placeholders (``placeholders`` in its /health). One such mail now and then
    is the mail's own doing; a run of them is a reader that broke, and with
    the gateway "connected" nobody would see it. It counts as well the mail it
    had mirrored and lost the records of (``lost``: a ledger restored from an
    older backup), which it looks for in the mailbox again. So when more of
    these stand than the user was last told of, by UNREAD_STEP or more, a
    notice says so — a notice, never an outage: its own thread, lower in
    importance, at most one per reminder interval.

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
# Mail a working gateway cannot read is worth knowing and nothing to be woken
# for: every mail is still in the mailbox, and each is read again by itself.
UNREAD_ATTENTION = {"importance": 3, "sphere": "system", "kind": "system notice"}


INTERVAL = float(os.environ.get("GATEWAY_MONITOR_INTERVAL", "") or "60")
# Consecutive failed checks before the user is notified (debounce). Five at
# the default interval: a link that heals within about five minutes — a
# gateway restart, a phone briefly offline — never reaches the user.
FAIL_THRESHOLD = int(os.environ.get("GATEWAY_MONITOR_FAILURES", "") or "5")
# While a gateway stays down, re-remind in the incident thread this often.
REMIND_SECONDS = float(os.environ.get("GATEWAY_MONITOR_REMIND_SECONDS", "") or str(6 * 3600))
HEALTH_TIMEOUT = float(os.environ.get("GATEWAY_MONITOR_HEALTH_TIMEOUT", "") or "10")
# How many more mails than the user was last told of a working mail gateway
# may be unable to read (or be looking for again) before a notice says so
# (see unread_message).
UNREAD_STEP = 5
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


# What a gateway names in ``blocked`` when what holds it is the gateway itself.
_OWN_FAULTS = frozenset({"mirror"})


def classify_health(status: int | None, body: dict | None, error: str | None = None):
    """Reduce one health check to (verdict, reason).

    verdict is one of:
      "up"           — the gateway reports a working link
      "down"         — reachable but the link is broken (or gateway unreachable)
      "blocked"      — reachable, and not working for a reason that is not its
                       link: it waits for something the deployment's setup
                       owes it, and its ``error`` says what
      "faulty"       — reachable, its link fine, and stopped on a fault of its
                       own (it names itself as what holds it: the mail
                       gateway's ``blocked: "mirror"``); its ``error`` says
                       on what
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
        held = body.get("blocked")
        return ("faulty" if held in _OWN_FAULTS else "blocked" if held else "down"), reason
    return "up", None


def unread_mail(body) -> dict | None:
    """What a gateway's /health says of mail it could not read (the mail
    gateway's ``placeholders``: how many stand, how many it made since it
    started, and why) and of mail whose records it lost and looks for again
    (its ``lost``: how many), or None where it says nothing of the kind."""
    seen = body.get("placeholders") if isinstance(body, dict) else None
    if not isinstance(seen, dict):
        return None
    try:
        counts = {key: max(0, int(seen.get(key) or 0)) for key in ("standing", "made")}
        lost = max(0, int(body.get("lost") or 0))
    except (TypeError, ValueError):
        return None
    why = seen.get("why") if isinstance(seen.get("why"), dict) else {}
    return {**counts, "why": {str(reason)[:80]: count for reason, count in why.items()
                              if isinstance(count, int) and not isinstance(count, bool)},
            "lost": lost}


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


# A gateway that is reachable and held by something that is not its link (see
# classify_health) has not lost its connection, and these words blame no
# login, no server and no phone. What holds it is the gateway's to say — its
# reported problem names it and where to look — so every notice, reminders
# included, carries the reported problem. By verdict: one that waits for the
# deployment's setup, and one that stopped on a fault of its own.
_HELD = {
    "blocked": {
        "title": "waiting for its setup",
        "state": "it is waiting for its setup",
        "since": "It has been waiting",
        "still": "still waiting for its setup",
        "cause": ("Its connection is fine. What it waits for is part of the deployment's "
                  "setup, not of the account, and the reported problem says what; {link} "
                  "shows what the gateway reports."),
    },
    "faulty": {
        "title": "stopped on a fault of its own",
        "state": "it stopped on a fault of its own",
        "since": "It has been stopped",
        "still": "still stopped on a fault of its own",
        "cause": ("Its connection is fine and its setup is complete: the gateway itself "
                  "cannot do its work, and the reported problem says on what. Its log has "
                  "the details; {link} shows what the gateway reports."),
    },
}


def wording(channel: str | None) -> dict:
    """The words for a gateway serving this registry channel (a messenger's
    when the channel is unknown or not stated)."""
    return WORDING.get((channel or "").strip().lower(), _MESSENGER_WORDING)


def _held(blocked) -> dict | None:
    """The words for a held gateway, None for one that lost its link.
    ``blocked`` is the verdict ("blocked", "faulty"; True reads as the first)."""
    return _HELD.get("blocked" if blocked is True else blocked)


def outage_title(label: str, blocked=False) -> str:
    held = _held(blocked)
    return f"{label} gateway {held['title'] if held else 'disconnected'}"


def outage_message(label: str, reason: str | None, since: float | None = None,
                   channel: str | None = None, blocked=False) -> str:
    words, held = wording(channel), _held(blocked)
    if held:
        lines = [f"The {label} {words['noun']} is not running: {held['state']} — "
                 f"{words['lost']}"]
    else:
        lines = [f"The {label} {words['noun']} has lost its connection — {words['lost']}"]
    if reason:
        lines.append(f"Reported problem: {reason}")
    lines.append((held["cause"] if held else words["cause"]).format(link=repair_link()))
    if since:
        mins = max(1, int((time.time() - since) // 60))
        lines.insert(1, f"{held['since']} for about {mins} minute(s)." if held
                     else f"The connection has been down for about {mins} minute(s).")
    return "\n\n".join(lines)


def reminder_message(label: str, since: float, channel: str | None = None,
                     reason: str | None = None, blocked=False) -> str:
    words, held = wording(channel), _held(blocked)
    hours = max(1, int((time.time() - since) // 3600))
    if held:
        return (
            f"Reminder: the {label} {words['noun']} is {held['still']} "
            f"(for about {hours} hour(s)). {words['still']} "
            + (f"Reported problem: {reason}. " if reason else "")
            + f"{repair_link()} shows what the gateway reports."
        )
    return (
        f"Reminder: the {label} {words['noun']} is still disconnected "
        f"(for about {hours} hour(s)). {words['still']} "
        f"{words['remedy'].format(link=repair_link())}"
    )


def recovery_message(label: str, channel: str | None = None, blocked=False) -> str:
    return (f"The {label} {wording(channel)['noun']} is "
            f"{'running' if _held(blocked) else 'connected'} again. No further action needed.")


def unread_title(label: str) -> str:
    return f"{label} gateway cannot read some mail"


def unread_message(label: str, unread: dict, channel: str | None = None) -> str:
    """The notice for a gateway that works and cannot read some of its mail,
    or looks for mail whose records it lost. It blames no login, no server
    and no phone, and reports no outage: mail is flowing. It says how many,
    why as the gateway counts them, that nothing is lost — and that a run of
    them is the gateway's to explain."""
    standing, made, lost = unread["standing"], unread["made"], unread.get("lost", 0)
    noun = wording(channel)["noun"]
    lines = []
    if standing or not lost:
        lines.append(
            f"The {label} {noun} is running, but {standing} mail(s) could not be read "
            f"and stand in their chats as placeholders ({made} made since the gateway "
            f"started). Nothing is lost: each mail is unchanged in the mailbox, and the "
            f"gateway reads it again every hour.")
    if lost:
        lines.append(
            (f"And {lost}" if lines else f"The {label} {noun} is running, but {lost}")
            + " mail(s) it had mirrored are missing from its records (restored from an "
            "older backup, or a record file removed). Nothing is lost: it looks for each "
            "in the mailbox by its Message-ID, and puts it back in its chat once found.")
    why = sorted(unread["why"].items(), key=lambda item: (-item[1], item[0]))
    if standing and why:
        lines.append("Why, as the gateway counts them: "
                     + "; ".join(f"{reason} ({count})" for reason, count in why[:6]) + ".")
    if standing or not lost:
        lines.append(
            f"One such mail now and then is that mail's own doing. A run of them means "
            f"the gateway's reader fails on mail it should read: its log names each mail "
            f"and what failed. {repair_link()} shows the gateway's status.")
    else:
        lines.append(f"{repair_link()} shows the gateway's status.")
    return "\n\n".join(lines)


class MonitorEngine:
    """Per-gateway outage state machine, separated from I/O for testability.

    ``step(slug, label, verdict, reason, now, channel)`` folds one health
    verdict into the persistent state and performs any due notification via
    the injected notifier; ``channel`` (the registry's) picks the wording.
    A "blocked" or "faulty" gateway runs through the same states as one that
    is down — debounced, one thread, reminders, a recovery — in its own
    words. And one incident may change its kind: a gateway that waited for
    its setup and now cannot log in has a second fault, which the user is
    told of in the same thread once it has been seen ``fail_threshold``
    times in a row (a restart answers "unreachable" once), in that kind's
    words and with its reason — not at the next reminder, in words of an
    outage that is over.
    State per slug:
      status      "up" | "down"
      fails       consecutive bad checks while still officially up
      since       when the outage began (first failed check of the streak),
                  or turned into the kind it is now
      thread_id   dashboard conversation carrying this incident
      notified    the outage notice actually reached the backend
      told        the verdict the user was last told ("down", "blocked",
                  "faulty"); every later notice is worded by it
      turning     another verdict the checks say now, ``turned`` of them in a
                  row, since ``turn_since`` — not told yet
      last_notice timestamp of the last message posted for this outage
      unread      what the user was told of mail the gateway cannot read (see
                  ``_note_unread``): ``told`` such mails, at ``last_notice``,
                  in ``thread_id``. Kept across outages: it is no part of one.
    """

    def __init__(self, notifier, state: dict | None = None,
                 fail_threshold: int = FAIL_THRESHOLD, remind_seconds: float = REMIND_SECONDS,
                 unread_step: int = UNREAD_STEP):
        self.notifier = notifier
        self.state = state if state is not None else {}
        self.fail_threshold = fail_threshold
        self.remind_seconds = remind_seconds
        self.unread_step = unread_step

    def _note_unread(self, slug: str, label: str, unread: dict | None, now: float,
                     channel: str | None) -> None:
        """A gateway that is up, and what it says of mail it could not read
        (``unread_mail``) — placeholders, and mail whose records it lost and
        looks for again, counted together. The user is told when
        ``unread_step`` or more of them stand beyond those last told of —
        once, then not again before a reminder interval has passed, however
        fast they come. Mails that were read since (placeholders heal, lost
        mail is found) count no more, so a later run is news again. Never an
        outage: the gateway's state stays "up"."""
        entry = self.state[slug]
        standing = unread["standing"] + unread.get("lost", 0) if unread else 0
        if not unread or not (standing or entry.get("unread")):
            return
        seen = entry.setdefault("unread", {})
        seen["told"] = min(int(seen.get("told", 0)), standing)
        last = seen.get("last_notice")
        if standing - seen["told"] < self.unread_step \
                or (last is not None and now - float(last) < self.remind_seconds):
            return
        text = unread_message(label, unread, channel)
        if seen.get("thread_id"):
            sent = self.notifier.append(seen["thread_id"], text)
        else:
            sent = seen["thread_id"] = self.notifier.open_thread(
                unread_title(label), text, attention=UNREAD_ATTENTION)
        if sent:
            print(f"{LOG} {slug}: {unread['standing']} mail(s) it cannot read, "
                  f"{unread.get('lost', 0)} it looks for again", flush=True)
            seen.update(told=standing, last_notice=now)
        # else: backend unreachable — retry on the next tick

    def step(self, slug: str, label: str, verdict: str, reason: str | None,
             now: float | None = None, channel: str | None = None,
             unread: dict | None = None) -> None:
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
                        recovery_message(label, channel, blocked=entry.get("told")),
                        quiet=True)
                    # The alert is obsolete: archive it for good (muted, so
                    # nothing wakes it) and settle its attention item. A
                    # later outage opens a thread of its own. A thread the
                    # user starred stays where they put it.
                    self.notifier.set_flags(entry["thread_id"], archived=True,
                                            muted=True, unless_starred=True)
                print(f"{LOG} {slug}: recovered", flush=True)
            self.state[slug] = {"status": "up", "fails": 0,
                                **({"unread": entry["unread"]} if entry.get("unread") else {})}
            self._note_unread(slug, label, unread, now, channel)
            return

        # verdict == "down", "blocked" or "faulty"
        entry["fails"] = int(entry.get("fails", 0)) + 1
        entry["reason"] = reason
        if entry.get("status") != "down":
            if entry["fails"] == 1:
                entry["since"] = now
            if entry["fails"] < self.fail_threshold:
                return
            entry["status"] = "down"
            print(f"{LOG} {slug}: {verdict.upper()} ({reason})", flush=True)

        if not entry.get("notified"):
            thread_id = self.notifier.open_thread(
                outage_title(label, verdict),
                outage_message(label, reason, entry.get("since"), channel, blocked=verdict),
                attention=OUTAGE_ATTENTION,
            )
            if thread_id:
                entry["thread_id"] = thread_id
                entry["notified"] = True
                entry["told"] = verdict
                entry["last_notice"] = now
            # else: backend unreachable — retry on the next tick
            return

        # (An incident opened by a build that did not keep "told" was told
        # as a lost connection, or as a setup wait where it noted one.)
        told = entry.get("told") or ("blocked" if entry.get("blocked") else "down")
        entry["told"] = told
        if verdict != told:
            if entry.get("turning") != verdict:
                entry.update(turning=verdict, turned=0, turn_since=now)
            entry["turned"] = int(entry.get("turned", 0)) + 1
            if entry["turned"] >= self.fail_threshold and entry.get("thread_id") \
                    and self.notifier.append(
                        entry["thread_id"],
                        outage_message(label, reason, None, channel, blocked=verdict)):
                print(f"{LOG} {slug}: {told.upper()} turned {verdict.upper()} ({reason})",
                      flush=True)
                entry.update(told=verdict, since=entry["turn_since"], last_notice=now)
                for key in ("turning", "turned", "turn_since"):
                    entry.pop(key, None)
            # (Not told yet — a blip, or the backend is unreachable: no
            # reminder in the old kind's words meanwhile.)
            return
        for key in ("turning", "turned", "turn_since"):
            entry.pop(key, None)

        if now - float(entry.get("last_notice", 0)) >= self.remind_seconds:
            if entry.get("thread_id") and self.notifier.append(
                    entry["thread_id"], reminder_message(label, entry.get("since", now),
                                                         channel, reason=reason,
                                                         blocked=told)):
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


def check_gateway(gw: dict) -> tuple[str, str | None, dict | None]:
    """GET <base_url>/health and classify the result: ``(verdict, reason,
    unread)`` — the last is what the gateway says of mail it could not read
    (``unread_mail``), None for one that says nothing of the kind."""
    url = f"{gw['base_url']}/health"
    headers = {}
    if gw.get("token"):
        headers["Authorization"] = "Bearer " + gw["token"]
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return (*classify_health(resp.status, body), unread_mail(body))
    except urllib.error.HTTPError as exc:
        return (*classify_health(exc.code, None), None)
    except Exception as exc:  # noqa: BLE001 - unreachable counts as down
        return (*classify_health(None, None, error=str(exc)), None)


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
                verdict, reason, unread = check_gateway(gw)
                engine.step(slug, gw.get("label") or slug.title(), verdict, reason,
                            channel=gw.get("channel"), unread=unread)
            except Exception as exc:  # noqa: BLE001 - one gateway must not stall the loop
                print(f"{LOG} error checking {slug}: {exc}\n{traceback.format_exc()}", flush=True)
        _save_state(engine.state)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
