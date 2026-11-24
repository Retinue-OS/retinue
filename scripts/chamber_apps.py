"""Chamber apps: discovery, their schedule files, and the reminder records.

A chamber app is a Progressive Web App a chamber ships in
``<chamber>/.retinue/apps/<app>/`` (docs/chamber-apps.md). This module is the
part of the mechanism that the web gateway and the scheduler share, kept free
of HTTP so both — and the tests — call the same code:

- **Discovery.** Which apps the mounted chambers ship, read from their
  ``manifest.webmanifest`` files, and the rows of ``GET /apps/index.json``.
- **Schedule files.** One per app, ``$CHAMBER_APPS_STATE_DIR/<chamber>/<app>/
  schedule.json`` (outside the chambers, so no git noise), written by the
  gateway when an app calls ``Retinue.schedule`` and read by the scheduler
  beside the chamber manifests. Jobs are validated here, once, on the way in.
- **Reminder records.** What the gateway keeps about each reminder's latest
  occurrence: its attention block (a reminder is an attention item of kind
  ``reminder``), the occurrences a shell confirmed it delivered, and a push
  that waits for such a confirmation.

There are no tokens and no per-app permissions: an app has the trust of the
chamber that ships it (its directory is Tier 3 there). What this module does
check is shape — names, paths, sizes — so that nothing an app sends can name
a file outside its place.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import attention as attention_policy
import attention_store
import calendar_trigger

APPS_SUBDIR = Path(".retinue") / "apps"
MANIFEST_NAME = "manifest.webmanifest"
STATE_DIR = Path(os.environ.get("CHAMBER_APPS_STATE_DIR") or "/root/.retinue/apps")

# A chamber, an app and a job id: one path segment each, no dots, so none can
# climb out of its directory or hide as a dotfile. A chamber whose directory
# name does not fit simply has no apps served.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Path segments the API's routes take for themselves: no chamber of these
# names has apps served.
RESERVED_CHAMBERS = {"api"}
JOB_KINDS = ("reminder", "job")
MAX_JOBS_PER_APP = 200
MAX_TITLE = 120
MAX_BODY = 500
MAX_PROMPT = 4000
MAX_EXTRA = 20
# The query parameter the deep link of a reminder carries, so the app page
# (webapp/apps/retinue.js) can report the reminder opened and drop it from
# the address.
REMINDER_PARAM = "retinue-reminder"


def default_zone():
    """The instance's zone, as the attention model reads it."""
    return attention_store.zone()


# ── Discovery ──────────────────────────────────────────────────────────────────

def _chambers(chambers_dir: Path) -> list[Path]:
    try:
        entries = sorted(Path(chambers_dir).iterdir())
    except OSError:
        return []
    return [p for p in entries
            if p.is_dir() and NAME_RE.match(p.name) and p.name not in RESERVED_CHAMBERS
            and not p.name.startswith("_")]


def app_dir(chambers_dir: Path, chamber: str, app: str) -> Path | None:
    """The directory of one app, or None when there is no such app (both
    names must be plain segments, and the app must carry a manifest)."""
    if not (NAME_RE.match(chamber or "") and NAME_RE.match(app or "")):
        return None
    if chamber in RESERVED_CHAMBERS:
        return None
    root = Path(chambers_dir)
    base = root / chamber / APPS_SUBDIR / app
    try:
        # The app must stay inside its chamber, whatever a symlink says.
        resolved = base.resolve()
        chamber_root = (root / chamber).resolve()
        if chamber_root not in resolved.parents:
            return None
        if not (resolved / MANIFEST_NAME).is_file():
            return None
    except OSError:
        return None
    return base


def read_manifest(directory: Path) -> dict:
    try:
        data = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def discover(chambers_dir: Path) -> list[dict]:
    """Every app the mounted chambers ship: ``{chamber, id, dir, manifest}``,
    sorted by chamber and app."""
    found = []
    for chamber in _chambers(chambers_dir):
        apps_root = chamber / APPS_SUBDIR
        try:
            entries = sorted(apps_root.iterdir())
        except OSError:
            continue
        for entry in entries:
            directory = app_dir(chambers_dir, chamber.name, entry.name)
            if directory is None:
                continue
            found.append({"chamber": chamber.name, "id": entry.name, "dir": directory,
                          "manifest": read_manifest(directory)})
    return found


def app_url(chamber: str, app: str) -> str:
    return f"/apps/{chamber}/{app}/"


def _app_relative(base: str, src) -> str | None:
    """A manifest URL as a path on this origin inside the app, or None for
    anything else (another origin, a scheme, a climb out)."""
    if not isinstance(src, str) or not src.strip():
        return None
    resolved = urllib.parse.urljoin("https://app.invalid" + base, src.strip())
    parts = urllib.parse.urlsplit(resolved)
    if parts.netloc != "app.invalid" or not parts.path.startswith(base):
        return None
    return parts.path


def _text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def index_entry(app: dict) -> dict:
    """One row of /apps/index.json: what the Apps list shows."""
    m = app.get("manifest") or {}
    base = app_url(app["chamber"], app["id"])
    retinue = m.get("retinue") if isinstance(m.get("retinue"), dict) else {}
    icons = []
    for icon in m.get("icons") or []:
        if not isinstance(icon, dict):
            continue
        src = _app_relative(base, icon.get("src"))
        if src:
            icons.append({k: v for k, v in (("src", src), ("sizes", _text(icon.get("sizes"), 40)),
                                            ("type", _text(icon.get("type"), 40))) if v})
    start = _app_relative(base, m.get("start_url") or "./") or base
    name = _text(m.get("name") or m.get("short_name") or app["id"], 80)
    return {
        "chamber": app["chamber"], "id": app["id"],
        "name": name, "short_name": _text(m.get("short_name") or name, 40),
        "description": _text(retinue.get("description") or m.get("description"), 300),
        "sphere": attention_policy.sphere_id(retinue.get("sphere")) if retinue.get("sphere") else None,
        "url": base, "start_url": start, "icons": icons,
        "theme_color": _text(m.get("theme_color"), 20) or None,
    }


def index(chambers_dir: Path) -> dict:
    return {"apps": [index_entry(a) for a in discover(chambers_dir)]}


def app_file(chambers_dir: Path, chamber: str, app: str, rel: str) -> Path | None:
    """The file a request under /apps/<chamber>/<app>/ names, or None. No
    segment may be empty-climbing (``..``) or a dotfile; the file must lie
    inside the app's directory after symlinks are resolved."""
    base = app_dir(chambers_dir, chamber, app)
    if base is None:
        return None
    segments = [s for s in rel.split("/") if s != ""]
    if any(s.startswith(".") or "\\" in s or "\x00" in s for s in segments):
        return None
    target = base.joinpath(*segments) if segments else base
    try:
        resolved_base = base.resolve()
        resolved = target.resolve()
        if resolved != resolved_base and resolved_base not in resolved.parents:
            return None
        if resolved.is_dir():
            resolved = resolved / "index.html"
        if not resolved.is_file():
            return None
    except OSError:
        return None
    return resolved


# ── Writing files into a chamber (Retinue.submit) ──────────────────────────────

_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+,=@-]{0,199}$")


def chamber_inbox(chambers_dir: Path, chamber: str) -> str | None:
    """The chamber's first declared inbox (``.inbox.json``), relative to it."""
    try:
        data = json.loads((Path(chambers_dir) / chamber / ".inbox.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for box in (data.get("inboxes") or []) if isinstance(data, dict) else []:
        if isinstance(box, dict) and isinstance(box.get("path"), str) and box["path"].strip():
            return box["path"].strip().strip("/")
    return None


def submit_target(chambers_dir: Path, chamber: str, segments: list[str]) -> tuple[Path | None, str]:
    """Where ``PUT /apps/api/files/<chamber>/<segments…>`` writes, as
    ``(path, "")``, or ``(None, why)``.

    One segment is a file name for the chamber's inbox; more are a directory
    relative to the chamber and the name last. Refused: an unknown chamber,
    a dot segment anywhere (``.retinue/`` holds the chamber's apps, plugin and
    manifests — a file written there could become a script the dashboard
    serves), ``..``, a file in the chamber's root (its STRUCTURE.md and
    manifests live there), and any path that resolves outside the chamber."""
    if not NAME_RE.match(chamber or "") or chamber.startswith("_"):
        return None, "unknown chamber"
    root = Path(chambers_dir) / chamber
    if not root.is_dir():
        return None, "unknown chamber"
    segs = [urllib.parse.unquote(s) for s in segments if s != ""]
    if not segs:
        return None, "name the file"
    for s in segs:
        if s in (".", "..") or s.startswith(".") or "/" in s or "\\" in s or "\x00" in s:
            return None, "a path segment may not start with a dot"
    name = segs[-1]
    if not _FILE_NAME_RE.match(name):
        return None, "invalid file name"
    if len(segs) == 1:
        inbox = chamber_inbox(chambers_dir, chamber)
        if not inbox:
            return None, "the chamber declares no inbox: name a directory"
        dir_segs = [s for s in inbox.split("/") if s]
        if any(s.startswith(".") or s == ".." for s in dir_segs):
            return None, "the chamber's inbox is not a usable path"
    else:
        dir_segs = segs[:-1]
    if len(dir_segs) > 12:
        return None, "the directory is nested too deep"
    target = root.joinpath(*dir_segs, name)
    try:
        root_resolved = root.resolve()
        parent = target.parent
        # The deepest part of the directory that exists must lie inside the
        # chamber: a symlinked directory cannot carry the write elsewhere.
        probe = parent
        while not probe.exists():
            probe = probe.parent
        if root_resolved != probe.resolve() and root_resolved not in probe.resolve().parents:
            return None, "outside the chamber"
        if target.is_symlink():
            return None, "the target is a symbolic link"
    except OSError:
        return None, "unusable path"
    return target, ""


# ── Schedule files ─────────────────────────────────────────────────────────────

_schedule_lock = threading.RLock()


def schedule_path(chamber: str, app: str, state_dir: Path | None = None) -> Path:
    return Path(state_dir or STATE_DIR) / chamber / app / "schedule.json"


def full_id(chamber: str, app: str, job_id: str) -> str:
    return f"app:{chamber}:{app}:{job_id}"


def split_full_id(value: str) -> tuple[str, str, str] | None:
    parts = str(value or "").split(":")
    if len(parts) != 4 or parts[0] != "app":
        return None
    _, chamber, app, job_id = parts
    if NAME_RE.match(chamber) and NAME_RE.match(app) and JOB_ID_RE.match(job_id):
        return chamber, app, job_id
    return None


def load_schedule(chamber: str, app: str, state_dir: Path | None = None) -> dict:
    try:
        data = json.loads(schedule_path(chamber, app, state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"jobs": []}
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return {"jobs": []}
    return data


def save_schedule(chamber: str, app: str, data: dict, state_dir: Path | None = None) -> None:
    path = schedule_path(chamber, app, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def all_schedules(state_dir: Path | None = None) -> list[tuple[str, str, Path]]:
    """Every app schedule file: ``(chamber, app, path)``."""
    root = Path(state_dir or STATE_DIR)
    out = []
    try:
        chambers = sorted(root.iterdir())
    except OSError:
        return []
    for c in chambers:
        if not (c.is_dir() and NAME_RE.match(c.name)):
            continue
        try:
            apps = sorted(c.iterdir())
        except OSError:
            continue
        for a in apps:
            f = a / "schedule.json"
            if a.is_dir() and NAME_RE.match(a.name) and f.is_file():
                out.append((c.name, a.name, f))
    return out


def _open_path(value) -> str:
    """The ``open`` of a reminder: a fragment (``#session/1``) or a path
    relative to the app (``session.html?x=1#y``). No scheme, no leading
    slash, no climb: the link always lands inside the app."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError("open must be a fragment or a path inside the app")
    v = value.strip()
    if any(c in v for c in "\\\x00\r\n\t ") or v.startswith("/"):
        raise ValueError("open must be a fragment or a path inside the app")
    head = re.split(r"[?#]", v, maxsplit=1)[0]
    if ":" in head or any(seg in ("..", ".") or seg.startswith(".") for seg in head.split("/") if seg):
        raise ValueError("open must be a fragment or a path inside the app")
    return v


def normalize_job(payload: dict, now: datetime, default_tz=None) -> dict:
    """A job as an app declares it (``Retinue.schedule(job)``), checked and
    reduced to the fields the scheduler and the gateway read. ValueError
    with a message for the app when something is unusable."""
    if not isinstance(payload, dict):
        raise ValueError("a job is a JSON object")
    job_id = str(payload.get("id") or "").strip()
    if not JOB_ID_RE.match(job_id):
        raise ValueError("id must be 1-64 of A-Z a-z 0-9 . _ - , starting with a letter or digit")
    kind = str(payload.get("kind") or "reminder").strip().lower()
    if kind not in JOB_KINDS:
        raise ValueError("kind must be reminder or job")
    if payload.get("command") is not None:
        raise ValueError("an app job runs a prompt, never a command")
    job: dict = {"id": job_id, "kind": kind}
    tz = default_tz or default_zone()
    if calendar_trigger.is_calendar(payload):
        for key in calendar_trigger.CALENDAR_FIELDS:
            if payload.get(key) not in (None, "", []):
                job[key] = payload[key]
        calendar_trigger.parse(job, tz)  # raises on an unusable field
    elif kind == "job" and payload.get("interval_seconds") is not None:
        try:
            seconds = int(payload["interval_seconds"])
        except (TypeError, ValueError):
            seconds = 0
        if seconds < 300:
            raise ValueError("interval_seconds must be a whole number, at least 300")
        job["interval_seconds"] = seconds
    else:
        raise ValueError("say when: at (with days or date), or for a job interval_seconds")
    if kind == "reminder":
        title = _text(payload.get("title"), MAX_TITLE)
        if not title:
            raise ValueError("a reminder needs a title")
        job["title"] = title
        body = _text(payload.get("body"), MAX_BODY)
        if body:
            job["body"] = body
        job["open"] = _open_path(payload.get("open"))
        if payload.get("importance") is not None:
            try:
                job["importance"] = max(0.0, min(5.0, float(payload["importance"])))
            except (TypeError, ValueError):
                raise ValueError("importance must be a number from 0 to 5")
        if payload.get("sphere"):
            sphere = attention_policy.sphere_id(payload["sphere"])
            if not sphere:
                raise ValueError("sphere must be a word")
            job["sphere"] = sphere
        tags = payload.get("tag", payload.get("tags"))
        if tags is not None:
            if isinstance(tags, str):
                tags = [tags]
            if not isinstance(tags, list):
                raise ValueError("tag must be a word or a list of words")
            job["tags"] = [t for t in (attention_policy.sphere_id(x) for x in tags[:10]) if t]
    else:
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("a job needs a prompt")
        if len(prompt) > MAX_PROMPT:
            raise ValueError(f"the prompt is longer than {MAX_PROMPT} characters")
        job["prompt"] = prompt
    if payload.get("enabled") is False:
        job["enabled"] = False
    job["registered_at"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    return job


def put_job(chamber: str, app: str, job: dict, state_dir: Path | None = None) -> dict:
    """Register a job, replacing one of the same id (its snoozes and skips
    go with it: the app declared its plan afresh)."""
    with _schedule_lock:
        data = load_schedule(chamber, app, state_dir)
        jobs = [j for j in data["jobs"] if j.get("id") != job["id"]]
        if len(jobs) >= MAX_JOBS_PER_APP:
            raise ValueError(f"an app keeps at most {MAX_JOBS_PER_APP} jobs")
        jobs.append(job)
        data["jobs"] = jobs
        save_schedule(chamber, app, data, state_dir)
        return job


def get_job(chamber: str, app: str, job_id: str, state_dir: Path | None = None) -> dict | None:
    for j in load_schedule(chamber, app, state_dir)["jobs"]:
        if j.get("id") == job_id:
            return j
    return None


def remove_job(chamber: str, app: str, job_id: str, state_dir: Path | None = None) -> bool:
    with _schedule_lock:
        data = load_schedule(chamber, app, state_dir)
        jobs = [j for j in data["jobs"] if j.get("id") != job_id]
        if len(jobs) == len(data["jobs"]):
            return False
        data["jobs"] = jobs
        save_schedule(chamber, app, data, state_dir)
        return True


def update_job(chamber: str, app: str, job_id: str, change, state_dir: Path | None = None) -> dict | None:
    """Apply ``change(job)`` to one stored job under the lock; the job as
    saved, or None when there is none."""
    with _schedule_lock:
        data = load_schedule(chamber, app, state_dir)
        for j in data["jobs"]:
            if j.get("id") == job_id:
                change(j)
                save_schedule(chamber, app, data, state_dir)
                return j
        return None


def _prune(job: dict, now: datetime) -> None:
    """Drop snoozes, skipped days and skipped slots that lie in the past."""
    today = now.astimezone(calendar_trigger.zone_named(job.get("tz"), default_zone())).date()
    job["extra"] = [x for x in job.get("extra") or []
                    if (calendar_trigger.parse_instant(x) or now) > now - timedelta(days=1)][-MAX_EXTRA:]
    job["skip_dates"] = sorted({d for d in job.get("skip_dates") or [] if str(d) >= today.isoformat()})
    job["skip_slots"] = [x for x in job.get("skip_slots") or []
                         if (calendar_trigger.parse_instant(x) or now) > now - timedelta(days=1)][-MAX_EXTRA:]
    for key in ("extra", "skip_dates", "skip_slots"):
        if not job[key]:
            job.pop(key)


def snooze_job(job: dict, at: datetime, now: datetime) -> str:
    """One extra one-off occurrence at ``at``; returns its occurrence key."""
    _prune(job, now)
    key = calendar_trigger.occurrence_key(at)
    extra = list(job.get("extra") or [])
    if key not in extra:
        extra.append(key)
    job["extra"] = extra[-MAX_EXTRA:]
    return key


def skip_today(job: dict, now: datetime) -> str:
    """Nothing more fires on today's local date (in the job's zone): the
    snoozes of today go too. Returns the date skipped."""
    tz = calendar_trigger.zone_named(job.get("tz"), default_zone())
    today = now.astimezone(tz).date()
    _prune(job, now)
    job["skip_dates"] = sorted(set(job.get("skip_dates") or []) | {today.isoformat()})
    job["extra"] = [x for x in job.get("extra") or []
                    if (calendar_trigger.parse_instant(x) or now).astimezone(tz).date() != today]
    if not job["extra"]:
        job.pop("extra")
    return today.isoformat()


def skip_slot(job: dict, slot: datetime, now: datetime) -> str:
    _prune(job, now)
    key = calendar_trigger.occurrence_key(slot)
    job["skip_slots"] = list(dict.fromkeys(list(job.get("skip_slots") or []) + [key]))[-MAX_EXTRA:]
    return key


def trigger_of(job: dict):
    """The job's calendar trigger, or None for an interval job or an
    unusable one."""
    if not calendar_trigger.is_calendar(job):
        return None
    try:
        return calendar_trigger.parse(job, default_zone())
    except ValueError:
        return None


def deep_link(chamber: str, app: str, job: dict) -> str:
    """Where a reminder opens: the app at its ``open``, carrying the
    reminder's id so the page can report it opened (REMINDER_PARAM)."""
    base = app_url(chamber, app)
    target = job.get("open") or ""
    rest, _, fragment = target.partition("#")
    path, _, query = rest.partition("?")
    marker = urllib.parse.urlencode({REMINDER_PARAM: job["id"]})
    query = f"{query}&{marker}" if query else marker
    return base + path + "?" + query + (("#" + fragment) if fragment else "")


# ── Reminder records (the gateway's) ───────────────────────────────────────────

class ReminderStore:
    """``reminders.json``: one record per reminder (its full id), holding the
    latest occurrence and its attention block. Single writer (the gateway),
    one lock, atomic writes."""

    MAX_DELIVERED = 50

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def _load(self) -> dict:
        doc = attention_policy.load_json(self.path, {})
        doc.setdefault("reminders", {})
        return doc

    def _save(self, doc: dict) -> None:
        attention_policy.save_json(self.path, doc)

    def all(self) -> dict:
        with self.lock:
            return self._load()["reminders"]

    def get(self, rid: str) -> dict | None:
        with self.lock:
            return self._load()["reminders"].get(rid)

    def put(self, rid: str, record: dict | None) -> None:
        with self.lock:
            doc = self._load()
            if record is None:
                doc["reminders"].pop(rid, None)
            else:
                record["delivered"] = list(record.get("delivered") or [])[-self.MAX_DELIVERED:]
                doc["reminders"][rid] = record
            self._save(doc)

    def update(self, rid: str, change) -> dict | None:
        with self.lock:
            doc = self._load()
            rec = doc["reminders"].get(rid)
            if rec is None:
                return None
            change(rec)
            rec["delivered"] = list(rec.get("delivered") or [])[-self.MAX_DELIVERED:]
            self._save(doc)
            return rec

    def shell_armed_at(self) -> datetime | None:
        with self.lock:
            return calendar_trigger.parse_instant(self._load().get("shell_armed_at"))

    def mark_shell_armed(self, at: datetime) -> None:
        with self.lock:
            doc = self._load()
            doc["shell_armed_at"] = at.astimezone(timezone.utc).isoformat(timespec="seconds")
            self._save(doc)


__all__ = [
    "APPS_SUBDIR", "MANIFEST_NAME", "STATE_DIR", "NAME_RE", "JOB_ID_RE", "REMINDER_PARAM",
    "default_zone", "app_dir", "read_manifest", "discover", "app_url", "index_entry", "index",
    "app_file", "chamber_inbox", "submit_target", "schedule_path", "full_id", "split_full_id",
    "load_schedule", "save_schedule", "all_schedules", "normalize_job", "put_job", "get_job",
    "remove_job", "update_job", "snooze_job", "skip_today", "skip_slot", "trigger_of",
    "deep_link", "ReminderStore",
]
