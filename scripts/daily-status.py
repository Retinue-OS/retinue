#!/usr/bin/env python3
"""
Daily system-status briefing.

Gathers a compact, fixed-structure status block and (optionally) sends it to the
owner over Signal *from the system account* (``signal-push.py`` with no
``--url`` → the system ``signal-gateway``), then verifies the message actually
arrived on the owner's *personal* account by reading that gateway's recent
chats. If the send cannot be verified — or any monitored subsystem is unhealthy
— the caller is told so it can open a dashboard conversation and follow up.

The block is deterministic and free of any Claude credits: it reads the Garmin
refresh state file, probes each gateway's ``/health``, pings the life store over
SPARQL, and counts conversations / projects / Ari's sent mail. An orchestrating
agent may append prose (e.g. a Coach health note) below the block via
``--extra-file``.

Structure (fixed; prose, if any, follows the blank line):

    🩺 Retinue Status — Mi 30.07. 07:30
    Garmin-Sync:                 ✅ ok (12:23)
    Gateways:                    Signal ✅  WhatsApp ✅  Telegram ✅  STT ✅  Life ✅
    Offene Gespräche:            4
    Projekte (mein Zug / total): 8 / 13
    Ari gesendet (24 h):         1
    ⚠️  Probleme → Dashboard: <link>          (only when there are problems)

    <optional prose>

Usage:
    # Dry run — print the block, gather everything, send nothing:
    python3 scripts/daily-status.py

    # Send from the system account and verify receipt on the personal account:
    python3 scripts/daily-status.py --send --verify

    # Once-per-day gate (for the scheduler, which ticks every ~30 min):
    python3 scripts/daily-status.py --send --verify --once-per-day --at 07:30

    # Machine-readable result (what was found + whether the send verified):
    python3 scripts/daily-status.py --send --verify --json
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Configuration from the environment ───────────────────────────────────────

KB = "https://w3id.org/retinue/kb#"
LIFE_ENDPOINT = os.environ.get("SPARQL_ENDPOINT_LIFE", "http://qlever-life:7001")
CONVERSATIONS_DIR = Path(os.environ.get("CONVERSATIONS_DIR", "/root/.retinue/conversations"))
HEALTH_CHAMBER = Path(os.environ.get("HEALTH_CHAMBER", "/workspace/chambers/health"))
SCRIPTS = Path(__file__).resolve().parent

SYSTEM_ACCOUNT = os.environ.get("SIGNAL_ACCOUNT", "")            # the "from" identity, e.g. +41766029556
PERSONAL_GATEWAY = os.environ.get(
    "SIGNAL_PERSONAL_GATEWAY_URL", "http://signal-gateway-personal:8090"
)
STATE_DIR = Path(os.environ.get("DAILY_STATUS_STATE_DIR", "/root/.retinue/daily-status"))

# Gateways probed for the health line. Life is checked over SPARQL, not /health.
GATEWAYS = [
    ("Signal", os.environ.get("SIGNAL_GATEWAY_BASE_URL", "http://signal-gateway:8090")),
    ("WhatsApp", os.environ.get("WHATSAPP_GATEWAY_BASE_URL", "http://whatsapp-gateway:8092")),
    ("Telegram", os.environ.get("TELEGRAM_GATEWAY_BASE_URL", "http://telegram-gateway:8093")),
    ("STT", (os.environ.get("STT_SERVICE_URL", "http://stt:8100/transcribe")).rsplit("/", 1)[0]),
]

# German weekday abbreviations — the briefing is user-facing German, and the
# day label is a fixed lookup, not a locale bias in program logic.
WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

OK = "✅"
BAD = "❌"


# ── Helpers ──────────────────────────────────────────────────────────────────

def http_ok(url: str, timeout: float = 5.0) -> bool:
    """True iff a GET returns 2xx."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def sparql(query: str, timeout: float = 8.0):
    """Run a SELECT against the life store; return the bindings list or None on error."""
    try:
        data = urllib.parse.urlencode({"query": query}).encode()
        req = urllib.request.Request(
            LIFE_ENDPOINT, data=data,
            headers={"Accept": "application/sparql-results+json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)["results"]["bindings"]
    except Exception:
        return None


# ── Gatherers ────────────────────────────────────────────────────────────────

def gather_garmin() -> dict:
    """Read the Garmin refresh state written by refresh.py."""
    state = HEALTH_CHAMBER / ".refresh" / "garmin.json"
    try:
        d = json.loads(state.read_text())
        ts = d.get("last_run", "")
        hhmm = ""
        if ts:
            try:
                hhmm = datetime.fromisoformat(ts).strftime("%H:%M")
            except ValueError:
                hhmm = ""
        return {"status": d.get("status", "unknown"), "last_run": ts, "hhmm": hhmm}
    except Exception:
        return {"status": "unknown", "last_run": "", "hhmm": ""}


def gather_gateways() -> dict:
    """Probe every gateway /health plus the life store SPARQL endpoint."""
    health = {name: http_ok(f"{base}/health") for name, base in GATEWAYS}
    health["Life"] = sparql("SELECT ?s WHERE { ?s ?p ?o } LIMIT 1") is not None
    return health


def gather_conversations() -> int:
    """Count active (non-archived) conversation threads."""
    n = 0
    if CONVERSATIONS_DIR.is_dir():
        for f in CONVERSATIONS_DIR.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            if d.get("archived"):
                continue
            # Edit threads are an internal channel, not a real open conversation.
            if d.get("kind") == "edit":
                continue
            n += 1
    return n


def gather_projects() -> dict:
    """Count unfinished projects total and those whose move is the owner's ('mein Zug')."""
    total_q = f"""
    PREFIX kb: <{KB}>
    SELECT (COUNT(DISTINCT ?p) AS ?n) WHERE {{
      GRAPH ?g {{ ?p a kb:Project . FILTER NOT EXISTS {{ ?p kb:resolved true }} }}
    }}"""
    mine_q = f"""
    PREFIX kb: <{KB}>
    SELECT (COUNT(DISTINCT ?p) AS ?n) WHERE {{
      GRAPH ?g {{
        ?p a kb:Project ;
           kb:currentActor <urn:retinue:actor:reto> .
        FILTER NOT EXISTS {{ ?p kb:resolved true }}
      }}
    }}"""

    def count(q):
        b = sparql(q)
        if not b:
            return None
        try:
            return int(b[0]["n"]["value"])
        except (KeyError, ValueError, IndexError):
            return None

    return {"mine": count(mine_q), "total": count(total_q)}


def gather_ari_sent(hours: int = 24) -> dict:
    """Count messages added to Ari's Sent folder within the last `hours`.

    IMAP date search has day granularity, so we search since yesterday's date
    and then filter to the precise cutoff by each message's timestamp.
    """
    folder = os.environ.get("SENT_FOLDER_ARI", "[Gmail]/Gesendet")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    since = (cutoff - timedelta(days=1)).strftime("%d-%b-%Y")  # widen by a day for TZ slack
    try:
        out = subprocess.run(
            [sys.executable, str(SCRIPTS / "email_client.py"), "--account", "ari",
             "search", "--folder", folder, "--since", since, "--limit", "200"],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return {"count": None, "error": out.stderr.strip()[:200]}
        msgs = json.loads(out.stdout).get("messages", [])
        n = 0
        for m in msgs:
            ds = m.get("date", "")
            try:
                if datetime.fromisoformat(ds) >= cutoff:
                    n += 1
            except ValueError:
                n += 1  # unparseable date → count it rather than silently drop
        return {"count": n}
    except Exception as e:
        return {"count": None, "error": str(e)[:200]}


# ── Problem detection ────────────────────────────────────────────────────────

def detect_problems(data: dict) -> list[str]:
    """Return human-readable problem lines (empty when all healthy)."""
    problems = []
    if data["garmin"]["status"] != "success":
        problems.append(f"Garmin-Sync: {data['garmin']['status']}")
    for name, ok in data["gateways"].items():
        if not ok:
            problems.append(f"Gateway {name} nicht erreichbar")
    if data["ari_sent"].get("count") is None:
        problems.append("Ari-Postausgang nicht lesbar")
    if data["projects"]["total"] is None:
        problems.append("Projekt-Statistik (Life-Store) nicht abfragbar")
    return problems


# ── Formatting ───────────────────────────────────────────────────────────────

def format_block(data: dict, now: datetime, dashboard_link: str | None) -> str:
    g = data["garmin"]
    garmin_line = (
        f"{OK} ok ({g['hhmm']})" if g["status"] == "success"
        else f"{BAD} {g['status']} ({g['hhmm']})" if g["hhmm"]
        else f"{BAD} {g['status']}"
    )
    gw = data["gateways"]
    gw_line = "  ".join(f"{name} {OK if ok else BAD}" for name, ok in gw.items())

    proj = data["projects"]
    mine = "?" if proj["mine"] is None else proj["mine"]
    total = "?" if proj["total"] is None else proj["total"]
    ari = data["ari_sent"].get("count")
    ari_s = "?" if ari is None else ari

    label = f"{WEEKDAYS_DE[now.weekday()]} {now.strftime('%d.%m.')} {now.strftime('%H:%M')}"
    lines = [
        f"🩺 Retinue Status — {label}",
        f"Garmin-Sync:                 {garmin_line}",
        f"Gateways:                    {gw_line}",
        f"Offene Gespräche:            {data['conversations']}",
        f"Projekte (mein Zug / total): {mine} / {total}",
        f"Ari gesendet (24 h):         {ari_s}",
    ]
    if dashboard_link:
        lines.append(f"⚠️  Probleme → Dashboard: {dashboard_link}")
    return "\n".join(lines)


# ── Send + verify ────────────────────────────────────────────────────────────

def open_conversation(title: str, message: str, timeout: float = 30.0) -> str | None:
    """Open a dashboard thread; return its URL (or bare id) so it can be linked.

    Used both for a problems thread (so the Signal message can link it) and for
    the fallback thread opened when a send cannot be verified.
    """
    try:
        out = subprocess.run(
            [sys.executable, str(SCRIPTS / "conversation-push.py"),
             "--title", title, message],
            capture_output=True, text=True, timeout=timeout + 15,
        )
        if out.returncode != 0:
            return None
        body = json.loads(out.stdout)
        return body.get("url") or body.get("id")
    except Exception:
        return None


def send_signal(body: str, timeout: float = 180.0) -> bool:
    """Send the briefing from the system account (no --url → system gateway).

    --no-voice: a status block is column-aligned and would be mangled by TTS;
    the owner reads these, they are not read aloud.
    """
    try:
        out = subprocess.run(
            [sys.executable, str(SCRIPTS / "signal-push.py"), "--no-voice",
             "--timeout", str(timeout), body],
            capture_output=True, text=True, timeout=timeout + 30,
        )
        return out.returncode == 0
    except Exception:
        return False


def verify_delivery(timeout: float = 10.0) -> bool | None:
    """Confirm the briefing reached the owner's personal account.

    Reads the personal gateway's recent chats and looks for the system account
    as a recent sender. Returns True/False, or None if the check itself failed
    (gateway unreachable) — the caller treats None as 'could not verify'.
    """
    if not SYSTEM_ACCOUNT:
        return None
    try:
        out = subprocess.run(
            [sys.executable, str(SCRIPTS / "signal-contacts.py"),
             "--url", PERSONAL_GATEWAY, "--all", "--timeout", str(timeout)],
            capture_output=True, text=True, timeout=timeout + 15,
        )
        if out.returncode != 0:
            return None
        digits = "".join(c for c in SYSTEM_ACCOUNT if c.isdigit())
        return digits in "".join(c if c.isdigit() else "" for c in out.stdout) \
            or SYSTEM_ACCOUNT in out.stdout
    except Exception:
        return None


# ── Once-per-day gate ────────────────────────────────────────────────────────

def already_sent_today(now: datetime) -> bool:
    f = STATE_DIR / "last-sent.json"
    try:
        d = json.loads(f.read_text())
        return d.get("date") == now.strftime("%Y-%m-%d")
    except Exception:
        return False


def mark_sent_today(now: datetime) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "last-sent.json").write_text(
        json.dumps({"date": now.strftime("%Y-%m-%d"), "at": now.isoformat()})
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Daily system-status briefing")
    ap.add_argument("--send", action="store_true", help="send via Signal from the system account")
    ap.add_argument("--verify", action="store_true", help="verify receipt on the personal account")
    ap.add_argument("--once-per-day", action="store_true",
                    help="skip (exit 0, no send) if already sent today or before --at")
    ap.add_argument("--at", default="07:30", metavar="HH:MM",
                    help="earliest time of day to send when --once-per-day (default 07:30)")
    ap.add_argument("--extra-file", metavar="PATH",
                    help="append this file's contents as prose below the block")
    ap.add_argument("--dashboard-link", metavar="URL",
                    help="link shown on the problem line (a thread you opened for problems)")
    ap.add_argument("--json", action="store_true", help="print a machine-readable result to stdout")
    args = ap.parse_args()

    now = datetime.now().astimezone()

    # Once-per-day gate: the scheduler ticks often; only fire once, at/after --at.
    if args.once_per_day and args.send:
        try:
            hh, mm = (int(x) for x in args.at.split(":"))
        except ValueError:
            hh, mm = 7, 30
        if now.hour < hh or (now.hour == hh and now.minute < mm):
            if args.json:
                print(json.dumps({"skipped": "before --at", "at": args.at}))
            return 0
        if already_sent_today(now):
            if args.json:
                print(json.dumps({"skipped": "already sent today"}))
            return 0

    # Gather everything (deterministic, no credits).
    data = {
        "garmin": gather_garmin(),
        "gateways": gather_gateways(),
        "conversations": gather_conversations(),
        "projects": gather_projects(),
        "ari_sent": gather_ari_sent(),
    }
    problems = detect_problems(data)

    result = {"problems": problems, "sent": False, "verified": None,
              "problem_thread": None, "verify_thread": None}

    # If anything needs fixing, open a dashboard thread first so the Signal
    # message can carry its link (the user's requirement). --dashboard-link, if
    # given, wins over the auto-opened one.
    dashboard_link = args.dashboard_link
    if problems and args.send and not dashboard_link:
        detail = "Beim heutigen Statuslauf sind Probleme aufgetreten:\n\n" + \
            "\n".join(f"• {p}" for p in problems) + \
            "\n\nSoll ich mich darum kümmern?"
        link = open_conversation("Systemstatus: Probleme", detail)
        if link:
            dashboard_link = link
            result["problem_thread"] = link

    block = format_block(data, now, dashboard_link)
    body = block
    if args.extra_file:
        try:
            prose = Path(args.extra_file).read_text().strip()
            if prose:
                body = f"{block}\n\n{prose}"
        except Exception:
            pass

    if args.send:
        sent = send_signal(body)
        result["sent"] = sent
        if sent and args.once_per_day:
            mark_sent_today(now)
        if args.verify:
            verified = verify_delivery() if sent else None
            result["verified"] = verified
            # Could not confirm the briefing reached the personal account →
            # fall back to a dashboard thread carrying the full block + status.
            if verified is not True:
                why = ("Der Signal-Versand ist fehlgeschlagen."
                       if not sent else
                       "Der Versand lief durch, aber die Zustellung auf dein "
                       "persönliches Konto konnte ich nicht bestätigen.")
                fb = (f"{why} Hier das heutige Briefing zur Sicherheit im "
                      f"Dashboard:\n\n{body}")
                result["verify_thread"] = open_conversation(
                    "Systemstatus: Zustellung unbestätigt", fb)
    else:
        # Dry run: show what would go out.
        print(body)

    if args.json:
        result["block"] = block
        result["data"] = data
        print(json.dumps(result, ensure_ascii=False, indent=2))

    # Non-zero exit signals "needs attention" so a command-only caller notices;
    # an orchestrating agent uses the richer --json instead.
    delivery_failed = args.send and (not result["sent"] or result["verified"] is False)
    return 1 if (problems or delivery_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
