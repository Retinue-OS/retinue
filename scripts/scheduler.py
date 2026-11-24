#!/usr/bin/env python3
"""Periodic task scheduler for the Retinue system.

Long-running daemon (forked by the entrypoint in remote-control mode) that runs
declared jobs on a fixed interval. Jobs are declared *per mounted chamber* in
`<chamber>/.schedule.json`, so each chamber owns its own schedule — the same
spirit as `.refresh.json` (see refresh.py), but for recurring agent tasks rather
than data freshness.

A job either:
  - **dispatches an agent task** via `prompt` → `claude -p "<prompt>"` (a fresh
    headless session, so it reads CLAUDE.md and Ara can route to a subagent;
    an access token about to expire is refreshed first, under the lock every
    framework spawner shares — scripts/claude_auth.py), or
  - runs a shell **`command`**.

Per-job run state lives under a state dir *outside* the chambers, so the scheduler
never creates git noise and survives restarts.

Manifest format  (`/workspace/chambers/<chamber>/.schedule.json`):
  {
    "jobs": [
      {
        "id": "ari-mailbox",
        "prompt": "Dispatch the ari subagent: check the mailbox ...",
        "interval_seconds": 1800,
        "enabled": true,
        "run_at_start": false
      }
    ]
  }

`interval_seconds` measures from the *previous run's completion* to the next
run's start, not start-to-start: `write_state` (below) is called once the job
has finished, so a job that takes real wall-clock time to run is spaced out by
that much extra, and a job whose run time varies does not compress its own gap
when it happens to run long.

A prompt job may pin the model its `claude -p` session runs on with an optional
`"model"` field. It takes precedence over the global RETINUE_CLAUDE_MODEL and
supports `${VAR:-default}` shell-style expansion, so a chamber can default a job
to a model while letting the deployment override it via one env var — without
naming the chamber in this framework file. Example:
  {"id": "triage", "prompt": "...", "model": "${RETINUE_TRIAGE_MODEL:-sonnet}"}

A job may pin its own wall-clock budget with an optional `"timeout_seconds"`,
overriding the global SCHEDULER_JOB_TIMEOUT for that job alone. One global
default cannot fit both a 10-second health check and a catch-all that has to
work through a whole mailbox: the long job is killed mid-flight every run, and
because it never finishes it never reduces its own backlog, so the next run is
slower still. Give such a job the budget its work actually needs. Example:
  {"id": "triage-daily", "command": "...", "interval_seconds": 86400,
   "timeout_seconds": 3600}

A job may also carry an optional `"retry_after_seconds"`, consulted only when
the last recorded run did not end in `"success"`: the job is then due as soon
as that many seconds have passed, instead of waiting out the full
`interval_seconds`. Leaving it unset means a failed run is due at exactly the
same point a successful one would be (the safe default — most failures deserve
a look before a bare retry, not a tight retry loop). Example, for a job whose
failures are usually transient (a rate limit, a flaky upstream):
  {"id": "herald-fetch", "command": "...", "interval_seconds": 86400,
   "retry_after_seconds": 900}

A command job that works through a backlog in bounded slices exits with
EXIT_PARTIAL (75, sysexits' EX_TEMPFAIL) to say "this slice is done and there
is more". That is recorded as status "partial" -- not a failure, not
"success". A job pairs it with `"resume_after_seconds"`: after a partial run
it is due again after that many seconds instead of its full interval, so a
backlog drains in successive runs rather than in one run that has to fit a
single budget. `resume_after_seconds` is consulted for "partial" *only* -- a
job whose model session fails must not be re-spawned every few minutes on
the strength of a knob meant for resuming honest work, which is why this is
not simply `retry_after_seconds` (that one still covers "partial" too, as any
non-success, for a job that wants one knob for both). Without either, a
partial run waits its interval like any other. Example:
  {"id": "triage-daily", "command": "...", "interval_seconds": 86400,
   "resume_after_seconds": 600}

A job may instead be timed by the **calendar** (scripts/calendar_trigger.py):
`at` (a time of day, or a list), with `days` (weekdays, as the focus schedule
writes them) or `date` (one-off dates), in `tz` (an IANA zone; default the
instance's), with a `lead` (how late a slot may still fire, default 15m).
Such a job needs no `interval_seconds`. A slot fires once; a slot found more
than `lead` late (the container was down) is skipped and logged as missed,
never fired late, and slots from before the job was first seen never fire.
Example:
  {"id": "weekly-review", "prompt": "...", "at": "08:40", "days": "mon",
   "tz": "Europe/Zurich", "lead": "2h"}

Chamber apps (docs/chamber-apps.md) keep their jobs in one file per app,
`$CHAMBER_APPS_STATE_DIR/<chamber>/<app>/schedule.json`, written by the web
gateway; they are loaded here beside the manifests, with ids
`app:<chamber>:<app>:<id>`. An app job is either a prompt (`kind: "job"`) or
a **reminder** (`kind: "reminder"`): a reminder runs no agent — firing it is
one request to the gateway (`/internal/apps/reminders/fire`), which makes it
an attention item. `python3 scheduler.py --list` shows every job, its trigger,
its next run and who owns it.

State files  (`$SCHEDULER_STATE_DIR/<job-id>.json`):
  {"last_run": "2026-06-14T16:00:00+00:00", "status": "success"}
  a calendar job adds "since" (when it was first seen) and "last_slot" (the
  last slot it fired or skipped).

Environment:
  SCHEDULER_TICK_SECONDS        loop granularity (default 30)
  SCHEDULER_JOB_TIMEOUT         per-job timeout in seconds (default 900)
  SCHEDULER_KILL_GRACE_SECONDS  SIGTERM->SIGKILL grace period for a timed-out
                                 job's process group, in seconds (default 10)
  SCHEDULER_STATE_DIR           state/log dir (default /root/.retinue/scheduler)
  CLAUDE_PERMISSION_MODE        permission mode for `claude -p` (default acceptEdits)
  RETINUE_ROUTER_MODEL          router-tier model for prompt jobs (Ara junior
                                 turns — see docs/model-routing.md); falls back
                                 to RETINUE_CLAUDE_MODEL when unset
  RETINUE_CLAUDE_MODEL          global fallback model (a job's own "model"
                                 field overrides both)
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_models  # noqa: E402
import claude_auth  # noqa: E402
import session_env  # noqa: E402
import letterbox  # noqa: E402
import calendar_trigger  # noqa: E402
import chamber_apps  # noqa: E402

CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR") or "/workspace/chambers")
# Framework-owned base manifest, always loaded alongside the per-chamber ones.
# Holds cross-cutting jobs that belong to the framework itself (e.g. agent
# self-review), not to any single chamber.
BASE_SCHEDULE = Path(os.environ.get("BASE_SCHEDULE") or "/workspace/.schedule.json")
TICK = int(os.environ.get("SCHEDULER_TICK_SECONDS", "30"))
JOB_TIMEOUT = int(os.environ.get("SCHEDULER_JOB_TIMEOUT", "900"))
# The exit code a command job uses for "done with this slice, more remains";
# see the module docstring. sysexits' EX_TEMPFAIL, chosen because it already
# means "try again later" to everything else on a Unix box.
EXIT_PARTIAL = 75
# Grace period between SIGTERM and SIGKILL when a timed-out job's process
# group has to be killed outright.
KILL_GRACE_SECONDS = int(os.environ.get("SCHEDULER_KILL_GRACE_SECONDS", "10"))
STATE_DIR = Path(os.environ.get("SCHEDULER_STATE_DIR", "/root/.retinue/scheduler"))
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")
# Prompt jobs are routing-shaped Ara turns (dispatch, relay), so the router
# tier wins when the deployment declares one; a job's own "model" field still
# overrides either (docs/model-routing.md).
CLAUDE_MODEL = (
    os.environ.get("RETINUE_ROUTER_MODEL", "").strip()
    or os.environ.get("RETINUE_CLAUDE_MODEL", "").strip()
)
LOG_FILE = STATE_DIR / "scheduler.log"
# Where a reminder is fired: the web gateway's internal endpoint, gated by
# the same shared token as /internal/conversations.
_GATEWAY_PORT = os.environ.get("WEB_GATEWAY_PORT", "8080")
REMINDER_FIRE_URL = os.environ.get(
    "APPS_REMINDER_FIRE_URL",
    f"http://localhost:{_GATEWAY_PORT}/internal/apps/reminders/fire")


_VAR_DEFAULT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: str) -> str:
    """Expand `${VAR}` / `${VAR:-default}` against the environment.

    Lets a job manifest carry a self-defaulting model reference like
    `${RETINUE_TRIAGE_MODEL:-sonnet}` — the deployment can override via the env
    var, and with nothing set *or set empty* the default applies, matching real
    shell `:-` semantics (a plain `dict.get` default only fires when the key is
    absent, not when it is present-but-empty). Unknown `${VAR}` with no default
    expands to empty (same as a missing global model: no --model flag).
    """
    def _sub(m):
        val = os.environ.get(m.group(1))
        if val:
            return val
        return m.group(2) if m.group(2) is not None else ""
    return _VAR_DEFAULT.sub(_sub, value)


def job_model(job: dict) -> str:
    """Model for a prompt job: its own `model` field (env-expanded) wins over the
    global RETINUE_CLAUDE_MODEL; empty string means 'no --model flag'."""
    raw = job.get("model")
    if raw:
        return expand_env(str(raw)).strip()
    return CLAUDE_MODEL


def job_env(model: str = "") -> dict:
    """Environment for spawned jobs.

    Scheduled jobs run agents (`claude -p`) or scripts that must not hold mailbox
    credentials — or any other secret this daemon's environment carries (it is
    forked by the entrypoint before the remote-control scrub, so it holds the
    whole container environment). The environment is therefore built from the
    allowlist in scripts/session_env.py, the same one every framework spawner
    uses, rather than copied and stripped: EMAIL_PASS* is absent by
    construction, and when EMAIL_BACKEND_TOKEN is set, email_client.py is
    pointed at the web gateway so it proxies instead. A command job gets the
    same environment as a prompt job: the base-job scripts spawn `claude -p`
    themselves and chamber commands run in the same trust position.

    `model` advertises the model the spawned session runs on (a session cannot
    introspect its own --model flag), so memory entries can be stamped with it
    (scripts/memory.py). Absent rather than inherited when this job passes no
    --model, so a stale value can never mislabel a session.
    """
    return session_env.build(model=model)


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def now() -> float:
    return time.time()


def _state_path(job_id: str) -> Path:
    return STATE_DIR / f"{job_id}.json"


def read_last_run(job_id: str) -> float | None:
    try:
        with open(_state_path(job_id), encoding="utf-8") as fh:
            ts = json.load(fh).get("last_run")
        return datetime.fromisoformat(ts).timestamp() if ts else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def read_last_status(job_id: str) -> str | None:
    try:
        with open(_state_path(job_id), encoding="utf-8") as fh:
            return json.load(fh).get("status")
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def read_state(job_id: str) -> dict | None:
    try:
        with open(_state_path(job_id), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def write_state(job_id: str, status: str, **fields) -> None:
    """Record a run. The calendar's own keys (``since``, ``last_slot``) are
    kept from the previous state unless ``fields`` sets them."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {k: v for k, v in (read_state(job_id) or {}).items() if k in ("since", "last_slot")}
    state.update({"last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "status": status})
    state.update(fields)
    tmp = _state_path(job_id).with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    tmp.replace(_state_path(job_id))


def load_jobs() -> list[dict]:
    """Collect and validate jobs from every repo's .schedule.json."""
    jobs: list[dict] = []
    seen: set[str] = set()
    # Framework base manifest first, then every chamber's. A chamber cannot
    # shadow a base job id (first-seen wins, and the duplicate is logged).
    manifests = [str(BASE_SCHEDULE)] if BASE_SCHEDULE.is_file() else []
    # Never the letterbox's: it is written from outside (scripts/letterbox.py).
    manifests += [str(c / ".schedule.json") for c in letterbox.mounted_chambers(CHAMBERS_DIR)
                  if (c / ".schedule.json").is_file()]
    for manifest in manifests:
        try:
            with open(manifest, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            log(f"[warn] cannot read {manifest}: {e}")
            continue
        for job in data.get("jobs", []):
            jid = job.get("id")
            if not jid:
                log(f"[warn] job without id in {manifest}, skipping")
                continue
            if jid in seen:
                log(f"[warn] duplicate job id {jid!r} ({manifest}), skipping")
                continue
            if not _usable_timing(job, jid, manifest):
                continue
            if not (job.get("prompt") or job.get("command")):
                log(f"[warn] job {jid!r} has neither prompt nor command, skipping")
                continue
            job["_source"] = manifest
            seen.add(jid)
            jobs.append(job)
    jobs.extend(load_app_jobs(seen))
    return jobs


_warned_timing: set[tuple[str, str]] = set()


def _usable_timing(job: dict, jid: str, source: str) -> bool:
    """A job runs on its interval or on a calendar trigger; one that has
    neither, or whose trigger cannot be read, is skipped with a warning (once
    per process and message, not on every tick)."""
    def warn(msg: str) -> bool:
        if (jid, msg) not in _warned_timing:
            _warned_timing.add((jid, msg))
            log(f"[warn] job {jid!r} ({source}) {msg}, skipping")
        return False
    if calendar_trigger.is_calendar(job):
        try:
            job["_trigger"] = calendar_trigger.parse(job, chamber_apps.default_zone())
        except ValueError as exc:
            return warn(f"has an unusable calendar trigger: {exc}")
        return True
    if not job.get("interval_seconds"):
        return warn("has no interval_seconds and no calendar trigger (at)")
    return True


def load_app_jobs(seen: set[str]) -> list[dict]:
    """The jobs of every chamber app (one schedule file per app, written by
    the web gateway), with ids ``app:<chamber>:<app>:<id>``. An app job runs
    a prompt or fires a reminder, never a command: the gateway refuses one,
    and one written here by other means is skipped all the same."""
    jobs = []
    for chamber, app, path in chamber_apps.all_schedules():
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            log(f"[warn] cannot read {path}: {e}")
            continue
        for raw in (data.get("jobs") or []) if isinstance(data, dict) else []:
            if not isinstance(raw, dict) or not chamber_apps.JOB_ID_RE.match(str(raw.get("id") or "")):
                continue
            jid = chamber_apps.full_id(chamber, app, raw["id"])
            if jid in seen:
                continue
            kind = raw.get("kind") or "reminder"
            if raw.get("command") is not None or kind not in chamber_apps.JOB_KINDS:
                if (jid, "kind") not in _warned_timing:
                    _warned_timing.add((jid, "kind"))
                    log(f"[warn] app job {jid!r} is neither a reminder nor a prompt job, skipping")
                continue
            if kind == "job" and not raw.get("prompt"):
                continue
            job = dict(raw, id=jid, kind=kind)
            job["_app"] = {"chamber": chamber, "app": app, "id": raw["id"]}
            if kind == "job":
                job["prompt"] = (f"Scheduled job of the chamber app {chamber}/{app} "
                                 f"(job {raw['id']}):\n\n{raw['prompt']}")
            if not _usable_timing(job, jid, str(path)):
                continue
            job["_source"] = str(path)
            seen.add(jid)
            jobs.append(job)
    return jobs


_warned_fields: set[tuple[str, str]] = set()


def _seconds_field(job: dict, name: str) -> int | None:
    """A job's optional positive-seconds field, or None when absent or unusable.

    is_due() runs for every job on every tick inside one try/except, so a
    value that raised here (`"soon"`) would not just misconfigure its own
    job: the loop's catch would skip every job after it, on every tick, until
    the manifest was fixed. Unusable reads as unset, with one warning per
    process rather than one per tick.
    """
    raw = job.get(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        key = (job.get("id", "?"), name)
        if key not in _warned_fields:
            _warned_fields.add(key)
            log(f"[warn] job {key[0]!r} has an unusable {name} ({raw!r}), ignoring it")
        return None
    return value


def due_slot(job: dict, at: datetime | None = None) -> datetime | None:
    """For a calendar job: the slot to fire now, or None.

    Slots before the job was first seen (``registered_at`` for an app job,
    else the first tick that saw it) never fire; a slot fires once. The
    latest slot not yet handled fires if it is at most ``lead`` old; older,
    it is recorded as missed and skipped — so a container that was down over
    a slot fires it on start only while it still means something."""
    if not job.get("enabled", True):
        return None
    trigger = job.get("_trigger") or calendar_trigger.parse(job, chamber_apps.default_zone())
    current = at or datetime.fromtimestamp(now(), timezone.utc)
    state = read_state(job["id"]) or {}
    since = calendar_trigger.parse_instant(state.get("since"))
    if since is None:
        registered = calendar_trigger.parse_instant(job.get("registered_at"))
        since = min(registered, current) if registered else current
        write_state(job["id"], "scheduled", since=since.isoformat(timespec="seconds"))
    floor = since
    last_slot = calendar_trigger.parse_instant(state.get("last_slot"))
    if last_slot is not None and last_slot > floor:
        floor = last_slot
    slot = trigger.latest(current, floor)
    if slot is None:
        return None
    if current - slot > trigger.lead:
        log(f"[missed] {job['id']} slot {slot.isoformat()} is "
            f"{int((current - slot).total_seconds() // 60)} min old, past its lead -- skipped")
        write_state(job["id"], "missed", last_slot=calendar_trigger.occurrence_key(slot))
        return None
    return slot


def is_due(job: dict) -> bool:
    if not job.get("enabled", True):
        return False
    if calendar_trigger.is_calendar(job):
        return due_slot(job) is not None
    last = read_last_run(job["id"])
    if last is None:
        if job.get("run_at_start"):
            return True
        # First sighting without run_at_start: start the clock from now so the
        # job fires one full interval later, not immediately.
        write_state(job["id"], "scheduled")
        return False
    elapsed = now() - last
    if elapsed >= int(job["interval_seconds"]):
        return True
    # A run that did not succeed may retry sooner than interval_seconds, but
    # only if the job opts in -- an unset retry_after_seconds leaves a failed
    # run due at exactly the same point a successful one would be, which is
    # deliberate (see the module docstring): most failures are worth
    # investigating before a bare retry, not worth hammering. A job that knows
    # its own failures are often transient (a rate limit, a flaky upstream)
    # can shorten that wait explicitly.
    #
    # "scheduled" is excluded here even though it is not "success": it is the
    # bookkeeping status the block above writes on a job's first sighting, to
    # start the interval clock -- not the record of an actual run. Without
    # this exclusion, a job seen for the first time reads back as a
    # non-success "last run" on the very next tick, so a job with
    # retry_after_seconds set would fire after that short delay instead of
    # ever waiting out its documented full interval_seconds for its first run.
    last_status = read_last_status(job["id"])
    # A partial run (see the module docstring) resumes on its own, shorter
    # clock, and only a partial one: a failure never rides this knob.
    resume_after = _seconds_field(job, "resume_after_seconds")
    if resume_after and last_status == "partial" and elapsed >= resume_after:
        return True
    retry_after = _seconds_field(job, "retry_after_seconds")
    if retry_after and last_status not in ("success", "scheduled"):
        return elapsed >= retry_after
    return False


def spawn_process(cmd, *, retry_enoent, **kwargs):
    """Popen, tolerant of the brief ENOENT window while Claude Code's
    auto-updater swaps the `claude` symlink (only used for agent prompts, where
    a missing binary is transient rather than a config error)."""
    attempts = 5 if retry_enoent else 1
    for attempt in range(attempts):
        try:
            return subprocess.Popen(cmd, **kwargs)
        except FileNotFoundError:
            if attempt == attempts - 1:
                raise
            time.sleep(1.0)


def _kill_group(pid: int, sig: int) -> None:
    """Signal a job's whole process group, tolerating a group that is already
    gone (the job finished in the race between the timeout firing and this
    call) or was never its own group leader."""
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def fire_reminder(job: dict, slot: datetime) -> bool:
    """A reminder costs no model turn: firing it is one request to the web
    gateway, which turns the occurrence into an attention item."""
    token = os.environ.get("CONVERSATION_BACKEND_TOKEN", "").strip()
    app = job["_app"]
    body = json.dumps({"chamber": app["chamber"], "app": app["app"], "id": app["id"],
                       "occurrence": calendar_trigger.occurrence_key(slot)}).encode()
    req = urllib.request.Request(REMINDER_FIRE_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Conversation-Backend-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.getcode() < 300
    except urllib.error.HTTPError as exc:
        log(f"[fail] {job['id']}: the gateway answered {exc.code}")
        # 404/410: the reminder is gone from the app's file since this tick
        # read it; nothing to retry.
        return exc.code in (404, 410)
    except (urllib.error.URLError, OSError) as exc:
        log(f"[fail] {job['id']}: gateway unreachable ({exc})")
        return False


def run_job(job: dict, slot: datetime | None = None) -> None:
    jid = job["id"]
    model = ""
    slot_fields = {"last_slot": calendar_trigger.occurrence_key(slot)} if slot else {}
    if job.get("kind") == "reminder" and job.get("_app"):
        if slot is None:
            return
        log(f"[run] {jid} (reminder) slot {slot.isoformat()}")
        if fire_reminder(job, slot):
            write_state(jid, "success", **slot_fields)
        else:
            # The slot stays unhandled: the next tick tries again, for as
            # long as the slot is within its lead.
            write_state(jid, "failed")
        return
    if job.get("prompt"):
        cmd = ["claude", "-p", "--output-format=json",
               "--permission-mode", PERMISSION_MODE, job["prompt"]]
        model = job_model(job)
        if model:
            cmd[2:2] = ["--model", model]
        # Per-agent model overrides for the subagents the job dispatches
        # (scripts/agent_models.py); [] when none is set.
        cmd[-1:-1] = agent_models.cli_args()
        kind = "prompt"
    else:
        cmd = job["command"]
        kind = "command"
    model_note = f", model={model}" if model else ""
    log(f"[run] {jid} ({kind}{model_note}) from {Path(job['_source']).parent.name}")
    started = now()
    # A job may buy itself more wall clock than the global default; see the
    # module docstring on "timeout_seconds". A non-positive or unparseable
    # value falls back rather than disabling the timeout — an un-killable job
    # would wedge the whole single-threaded tick loop. An omitted or null
    # field is not a mistake and says nothing; a field that is present but
    # unusable is a malformed manifest and must be visible, rather than
    # falling back as quietly as an absent one would.
    raw_timeout = job.get("timeout_seconds")
    timeout = JOB_TIMEOUT
    if raw_timeout is not None:
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError):
            log(f"[warn] job {jid!r} has an unusable timeout_seconds "
                f"({raw_timeout!r}), using {JOB_TIMEOUT}s")
            timeout = JOB_TIMEOUT
        if timeout <= 0:
            log(f"[warn] job {jid!r} has a non-positive timeout_seconds "
                f"({raw_timeout!r}), using {JOB_TIMEOUT}s")
            timeout = JOB_TIMEOUT
    try:
        # A `claude` started on an access token about to expire refreshes it
        # at once, racing every other claude process for the one rotation
        # (docs/claude-auth.md). Refresh first — once, under the lock all
        # framework spawners share — so the child never has to; a failure only
        # logs, and the child then refreshes for itself.
        #
        # Command jobs get this too, even though most of them never load a
        # model: a job script that does spawn `claude` may be a chamber's, and
        # a chamber cannot be required to know about claude_auth — one
        # unguarded hourly spawn is enough to sign the whole deployment out.
        # The call is idempotent and, away from expiry, one file read, so the
        # scripts that already refresh for themselves pay nothing for it.
        claude_auth.ensure_fresh_credentials(
            log=lambda msg: log(f"[auth] {jid}: {msg}"))
        proc = spawn_process(
            cmd,
            retry_enoent=(kind == "prompt"),
            shell=isinstance(cmd, str),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd="/workspace",
            env=job_env(model),
            # Own process group, so a timeout can reach descendants too --
            # subprocess.run's timeout path only ever signalled the direct
            # child, leaving any grandchildren it spawned running.
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc.pid, signal.SIGTERM)
            try:
                proc.communicate(timeout=KILL_GRACE_SECONDS)
                log(f"[timeout] {jid} exceeded {timeout}s -- group terminated")
            except subprocess.TimeoutExpired:
                _kill_group(proc.pid, signal.SIGKILL)
                proc.communicate()
                log(f"[timeout] {jid} exceeded {timeout}s -- group killed")
            write_state(jid, "timeout", **slot_fields)
            return
        dur = now() - started
        if proc.returncode == 0:
            log(f"[ok] {jid} in {dur:.0f}s")
            write_state(jid, "success", **slot_fields)
        elif proc.returncode == EXIT_PARTIAL:
            # A slice finished and the job says more is waiting: not a
            # failure, so no error text -- but not "success" either, so a
            # resume_after_seconds on the job brings the next slice forward.
            # Report the clock is_due() will actually apply: the shorter of
            # the two opt-in waits (both cover a partial run), else the
            # interval -- and only usable values, as is_due() reads them.
            clocks = [c for c in (_seconds_field(job, "resume_after_seconds"),
                                  _seconds_field(job, "retry_after_seconds"))
                      if c]
            wait = min(clocks) if clocks else job.get("interval_seconds")
            log(f"[partial] {jid} in {dur:.0f}s -- more to do, due again after "
                f"{wait}s")
            write_state(jid, "partial", **slot_fields)
        else:
            errtxt = (err or out or "").strip().replace("\n", " ")
            log(f"[fail] {jid} rc={proc.returncode} in {dur:.0f}s: {errtxt[:300]}")
            write_state(jid, "failed", **slot_fields)
    except Exception as e:  # never let one job kill the daemon
        log(f"[error] {jid}: {e}")
        write_state(jid, "error", **slot_fields)


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"scheduler started (tick={TICK}s, timeout={JOB_TIMEOUT}s, "
        f"permission-mode={PERMISSION_MODE}, chambers={CHAMBERS_DIR})")
    while True:
        try:
            jobs = load_jobs()
            for job in jobs:
                if calendar_trigger.is_calendar(job):
                    slot = due_slot(job)
                    if slot is not None:
                        run_job(job, slot)
                elif is_due(job):
                    run_job(job)
        except Exception as e:  # keep the loop alive no matter what
            log(f"[error] scheduler loop: {e}")
        time.sleep(TICK)


def list_jobs() -> list[dict]:
    """Every job as ``--list`` shows it: id, kind, trigger, next run, owner."""
    rows = []
    current = datetime.fromtimestamp(now(), timezone.utc)
    for job in load_jobs():
        trigger = job.get("_trigger")
        if trigger is not None:
            when = calendar_trigger.describe(trigger)
            nxt = trigger.next_after(current)
            nxt = nxt.isoformat() if nxt else None
        else:
            when = f"every {job['interval_seconds']}s"
            last = read_last_run(job["id"])
            nxt = (datetime.fromtimestamp(last, timezone.utc)
                   + timedelta(seconds=int(job["interval_seconds"]))).isoformat(timespec="seconds") \
                if last else None
        app = job.get("_app")
        owner = (f"app {app['chamber']}/{app['app']}" if app
                 else "base" if job["_source"] == str(BASE_SCHEDULE)
                 else f"chamber {Path(job['_source']).parent.name}")
        kind = job.get("kind") if app else ("prompt" if job.get("prompt") else "command")
        rows.append({"id": job["id"], "kind": kind, "when": when, "next": nxt,
                     "enabled": job.get("enabled", True) is not False, "owner": owner})
    return rows


if __name__ == "__main__":
    if "--list" in sys.argv[1:]:
        for row in list_jobs():
            flag = "" if row["enabled"] else "  (disabled)"
            print(f"{row['id']}\t{row['kind']}\t{row['when']}\tnext {row['next'] or '-'}"
                  f"\t{row['owner']}{flag}")
        sys.exit(0)
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
