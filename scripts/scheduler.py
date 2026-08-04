#!/usr/bin/env python3
"""Periodic task scheduler for the Retinue system.

Long-running daemon (forked by the entrypoint in remote-control mode) that runs
declared jobs on a fixed interval. Jobs are declared *per mounted chamber* in
`<chamber>/.schedule.json`, so each chamber owns its own schedule — the same
spirit as `.refresh.json` (see refresh.py), but for recurring agent tasks rather
than data freshness.

A job either:
  - **dispatches an agent task** via `prompt` → `claude -p "<prompt>"` (a fresh
    headless session, so it reads CLAUDE.md and Ara can route to a subagent), or
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

A prompt job may pin the model its `claude -p` session runs on with an optional
`"model"` field. It takes precedence over the global RETINUE_CLAUDE_MODEL and
supports `${VAR:-default}` shell-style expansion, so a chamber can default a job
to a model while letting the deployment override it via one env var — without
naming the chamber in this framework file. Example:
  {"id": "triage", "prompt": "...", "model": "${RETINUE_TRIAGE_MODEL:-sonnet}"}

State files  (`$SCHEDULER_STATE_DIR/<job-id>.json`):
  {"last_run": "2026-06-14T16:00:00+00:00", "status": "success"}

Environment:
  SCHEDULER_TICK_SECONDS   loop granularity (default 30)
  SCHEDULER_JOB_TIMEOUT    per-job timeout in seconds (default 900)
  SCHEDULER_STATE_DIR      state/log dir (default /root/.retinue/scheduler)
  CLAUDE_PERMISSION_MODE   permission mode for `claude -p` (default acceptEdits)
  RETINUE_CLAUDE_MODEL     global model for all prompt jobs (a job's own "model"
                           field overrides it)
"""

import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR") or "/workspace/chambers")
# Framework-owned base manifest, always loaded alongside the per-chamber ones.
# Holds cross-cutting jobs that belong to the framework itself (e.g. agent
# self-review), not to any single chamber.
BASE_SCHEDULE = Path(os.environ.get("BASE_SCHEDULE") or "/workspace/.schedule.json")
TICK = int(os.environ.get("SCHEDULER_TICK_SECONDS", "30"))
JOB_TIMEOUT = int(os.environ.get("SCHEDULER_JOB_TIMEOUT", "900"))
STATE_DIR = Path(os.environ.get("SCHEDULER_STATE_DIR", "/root/.retinue/scheduler"))
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")
CLAUDE_MODEL = os.environ.get("RETINUE_CLAUDE_MODEL", "").strip()
LOG_FILE = STATE_DIR / "scheduler.log"


_VAR_DEFAULT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: str) -> str:
    """Expand `${VAR}` / `${VAR:-default}` against the environment.

    Lets a job manifest carry a self-defaulting model reference like
    `${RETINUE_TRIAGE_MODEL:-sonnet}` — the deployment can override via the env
    var, and with nothing set the default applies. Unknown `${VAR}` with no
    default expands to empty (same as a missing global model: no --model flag).
    """
    return _VAR_DEFAULT.sub(
        lambda m: os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else ""),
        value,
    )


def job_model(job: dict) -> str:
    """Model for a prompt job: its own `model` field (env-expanded) wins over the
    global RETINUE_CLAUDE_MODEL; empty string means 'no --model flag'."""
    raw = job.get("model")
    if raw:
        return expand_env(str(raw)).strip()
    return CLAUDE_MODEL


def job_env() -> dict:
    """Environment for spawned jobs.

    Scheduled jobs run agents (`claude -p`) or scripts that must not hold mailbox
    credentials. When EMAIL_BACKEND_TOKEN is set, strip EMAIL_PASS* and point
    email_client.py at the web gateway so it proxies instead (mirrors the
    entrypoint's remote-control setup, since the scheduler is forked before it).
    """
    env = dict(os.environ)
    if env.get("EMAIL_BACKEND_TOKEN"):
        port = env.get("WEB_GATEWAY_PORT", "8080")
        env["EMAIL_BACKEND_URL"] = f"http://localhost:{port}/internal/email"
        for key in [k for k in env if k.startswith("EMAIL_PASS")]:
            del env[key]
    return env


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


def write_state(job_id: str, status: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _state_path(job_id).with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "status": status}, fh)
    tmp.replace(_state_path(job_id))


def load_jobs() -> list[dict]:
    """Collect and validate jobs from every repo's .schedule.json."""
    jobs: list[dict] = []
    seen: set[str] = set()
    # Framework base manifest first, then every chamber's. A chamber cannot
    # shadow a base job id (first-seen wins, and the duplicate is logged).
    manifests = [str(BASE_SCHEDULE)] if BASE_SCHEDULE.is_file() else []
    manifests += sorted(glob.glob(str(CHAMBERS_DIR / "*" / ".schedule.json")))
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
            if not job.get("interval_seconds"):
                log(f"[warn] job {jid!r} has no interval_seconds, skipping")
                continue
            if not (job.get("prompt") or job.get("command")):
                log(f"[warn] job {jid!r} has neither prompt nor command, skipping")
                continue
            job["_source"] = manifest
            seen.add(jid)
            jobs.append(job)
    return jobs


def is_due(job: dict) -> bool:
    if not job.get("enabled", True):
        return False
    last = read_last_run(job["id"])
    if last is None:
        if job.get("run_at_start"):
            return True
        # First sighting without run_at_start: start the clock from now so the
        # job fires one full interval later, not immediately.
        write_state(job["id"], "scheduled")
        return False
    return (now() - last) >= int(job["interval_seconds"])


def run_claude(cmd, **kwargs):
    """subprocess.run tolerant of the brief ENOENT window while Claude Code's
    auto-updater swaps the `claude` symlink (only used for agent prompts, where
    a missing binary is transient rather than a config error)."""
    for attempt in range(5):
        try:
            return subprocess.run(cmd, **kwargs)
        except FileNotFoundError:
            if attempt == 4:
                raise
            time.sleep(1.0)


def run_job(job: dict) -> None:
    jid = job["id"]
    model = ""
    if job.get("prompt"):
        cmd = ["claude", "-p", "--output-format=json",
               "--permission-mode", PERMISSION_MODE, job["prompt"]]
        model = job_model(job)
        if model:
            cmd[2:2] = ["--model", model]
        kind = "prompt"
    else:
        cmd = job["command"]
        kind = "command"
    model_note = f", model={model}" if model else ""
    log(f"[run] {jid} ({kind}{model_note}) from {Path(job['_source']).parent.name}")
    started = now()
    try:
        spawn = run_claude if kind == "prompt" else subprocess.run
        result = spawn(
            cmd,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            cwd="/workspace",
            timeout=JOB_TIMEOUT,
            env=job_env(),
        )
        dur = now() - started
        if result.returncode == 0:
            log(f"[ok] {jid} in {dur:.0f}s")
            write_state(jid, "success")
        else:
            err = (result.stderr or result.stdout or "").strip().replace("\n", " ")
            log(f"[fail] {jid} rc={result.returncode} in {dur:.0f}s: {err[:300]}")
            write_state(jid, "failed")
    except subprocess.TimeoutExpired:
        log(f"[timeout] {jid} exceeded {JOB_TIMEOUT}s")
        write_state(jid, "timeout")
    except Exception as e:  # never let one job kill the daemon
        log(f"[error] {jid}: {e}")
        write_state(jid, "error")


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"scheduler started (tick={TICK}s, timeout={JOB_TIMEOUT}s, "
        f"permission-mode={PERMISSION_MODE}, chambers={CHAMBERS_DIR})")
    while True:
        try:
            jobs = load_jobs()
            for job in jobs:
                if is_due(job):
                    run_job(job)
        except Exception as e:  # keep the loop alive no matter what
            log(f"[error] scheduler loop: {e}")
        time.sleep(TICK)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
