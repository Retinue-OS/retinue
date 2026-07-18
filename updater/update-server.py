#!/usr/bin/env python3
"""Updater sidecar — HTTP trigger for a full stack rebuild/restart.

The retinue container cannot rebuild/restart itself: the moment
`docker compose up -d` recreates the `retinue` service, the process issuing the
command is killed mid-update. This sidecar runs as a *separate* compose
service so it survives the recreation of every other service, including
itself if unchanged.

It exposes exactly one operation, `POST /update`, which runs an
**operator-configured** recipe — never a request-supplied command. The recipe
comes from the ``UPDATE_COMMAND`` environment variable and defaults to the
framework's standalone behaviour:

    git pull && docker compose build && docker compose up -d

run in the project directory (mounted read-write at ``/repo``), using the host
Docker socket (mounted at ``/var/run/docker.sock``) to drive compose.

Why ``UPDATE_COMMAND`` rather than a hard-coded recipe: this sidecar is part of
the generic **framework** (retinue), but *how* a given host updates is
**deployment**-specific — e.g. a nested deployment (my-retinue) that owns both
the deployment repo and the framework clone updates via its own ``start.sh
update``, pulling two repos instead of one. Hard-coding the framework's
single-repo recipe here would make the generic component know about a specific
deployment — the wrong direction for a dependency. Instead the framework
defines only the *interface* ("run the update command") and the deployment
*injects* its recipe through the environment (config flows deployment →
framework). ``UPDATE_COMMAND`` is set by the operator in compose/``.env``, never
by the HTTP caller, so the endpoint is still not an arbitrary command runner.
When unset, the default keeps a bare framework checkout self-updating exactly as
before.

Auth: the request must carry an ``X-Update-Token`` header, or an
``Authorization`` header using the bearer scheme, whose value matches the
``UPDATER_TOKEN`` environment variable. Unlike the web gateway's internal
backend tokens, this one cannot be auto-generated at container start, because
the retinue container (the caller) and this sidecar (the callee) are separate
processes that would each generate their own value — so it must be set explicitly in
``.env``, shared by both services via ``env_file``. When unset, every request
is rejected (fail closed); there is no legitimate reason to run this sidecar
without a token, since the public HTTP path additionally sits behind Traefik
basic auth, but the internal endpoint alone must never be an open trigger for
rebuilding the whole stack.

The update runs in a background thread so the HTTP response (202 Accepted)
returns immediately; a concurrent request while an update is already running
is rejected with 409 Conflict rather than queued or run twice.
"""
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPDATER_TOKEN = os.environ.get("UPDATER_TOKEN", "").strip()
UPDATER_PORT = int(os.environ.get("UPDATER_PORT", "9000"))
PROJECT_DIR = os.environ.get("PROJECT_DIR", "/repo")
UPDATE_LOG_PATH = os.environ.get("UPDATE_LOG_PATH", "/tmp/update.log")
# Generous ceiling for `git pull && docker compose build && docker compose up -d`
UPDATE_TIMEOUT = float(os.environ.get("UPDATE_TIMEOUT", "1800"))
# The deployment-injected update recipe (see module docstring). Empty => the
# framework's built-in default recipe. When set, it is run through a shell so a
# deployment can pass a full command line (e.g. `./start.sh update`).
UPDATE_COMMAND = os.environ.get("UPDATE_COMMAND", "").strip()
# The project repo is private, and this sidecar has no git credentials of its own:
# the agent container's credential store lives on *its* /root volume, not ours.
# Reuse the token compose already hands us via env_file (see docker-compose.yml).
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

_lock = threading.Lock()
_state = {"running": False, "started_at": None, "finished_at": None,
          "returncode": None, "failed_step": None}


def _git_pull_argv() -> list:
    """`git pull`, authenticated with GITHUB_TOKEN when one is present.

    The token is passed through a credential helper that reads it from the
    *environment*, so it never lands in argv (visible in `ps`), in the log, or
    in .git/config. Without a token we still run a plain `git pull`: a public
    repo needs none, and the failure is explicit rather than silent.
    """
    if not GITHUB_TOKEN:
        return ["git", "pull"]
    helper = '!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f'
    # The empty helper first resets any inherited helper chain.
    return ["git", "-c", "credential.helper=", "-c", f"credential.helper={helper}", "pull"]


def _step_env() -> dict:
    env = dict(os.environ)
    # Never block on an interactive credential prompt: with no tty, git would
    # otherwise fail obscurely. Fail fast and loudly instead.
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _check_token(headers) -> bool:
    if not UPDATER_TOKEN:
        return False
    supplied = (headers.get("X-Update-Token") or "").strip()
    if not supplied:
        auth = headers.get("Authorization") or ""
        scheme, _, rest = auth.partition(" ")
        if scheme.lower() == "bearer":
            supplied = rest.strip()
    if not supplied:
        return False
    return hmac.compare_digest(supplied, UPDATER_TOKEN)


def _run_update():
    started = time.time()
    with _lock:
        _state["running"] = True
        _state["started_at"] = started
        _state["finished_at"] = None
        _state["returncode"] = None
        _state["failed_step"] = None

    # A "step" is (argv_or_command, shell, shown). When UPDATE_COMMAND is set the
    # deployment owns the whole recipe, run as a single shell step; otherwise we
    # run the framework's built-in three-step default as separate argv steps.
    if UPDATE_COMMAND:
        steps = [(UPDATE_COMMAND, True, UPDATE_COMMAND)]
    else:
        steps = [
            (_git_pull_argv(), False, "git pull"),
            (["docker", "compose", "build"], False, "docker compose build"),
            (["docker", "compose", "up", "-d"], False, "docker compose up -d"),
        ]
    returncode = 0
    failed_step = None
    try:
        with open(UPDATE_LOG_PATH, "a") as log:
            log.write(f"\n=== update started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            if UPDATE_COMMAND:
                log.write(f"[updater] using operator-configured UPDATE_COMMAND\n")
            if not GITHUB_TOKEN:
                log.write("[updater] no GITHUB_TOKEN in environment; `git pull` will fail "
                          "against a private remote\n")
            log.flush()
            for cmd, shell, shown in steps:
                # `shown` keeps the credential-helper -c flags out of the log; they
                # carry no secret, but the shorter line is what you want to read.
                log.write(f"$ {shown}\n")
                log.flush()
                result = subprocess.run(
                    cmd,
                    cwd=PROJECT_DIR,
                    shell=shell,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=UPDATE_TIMEOUT,
                    env=_step_env(),
                )
                if result.returncode != 0:
                    returncode = result.returncode
                    failed_step = shown
                    log.write(f"[updater] step failed with exit code {result.returncode}, aborting\n")
                    break
            log.write(f"=== update finished (exit {returncode}) ===\n")
    except Exception as exc:  # noqa: BLE001 - always record failure, never crash the server
        returncode = -1
        failed_step = failed_step or "(exception)"
        try:
            with open(UPDATE_LOG_PATH, "a") as log:
                log.write(f"[updater] exception: {exc!r}\n")
        except OSError:
            pass
    finally:
        with _lock:
            _state["running"] = False
            _state["finished_at"] = time.time()
            _state["returncode"] = returncode
            # Which step failed is the one thing GET /status could not tell you,
            # and the log lives inside this container where the caller cannot read it.
            _state["failed_step"] = failed_step


class Handler(BaseHTTPRequestHandler):
    server_version = "retinue-updater/1"

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib override
        sys.stderr.write("[updater] " + (fmt % args) + "\n")

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        if self.path == "/status":
            with _lock:
                self._send_json(200, dict(_state))
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/update":
            self._send_json(404, {"error": "not found"})
            return
        if not _check_token(self.headers):
            self._send_json(401, {"error": "unauthorized"})
            return
        with _lock:
            if _state["running"]:
                self._send_json(409, {"error": "update already in progress"})
                return
            thread = threading.Thread(target=_run_update, daemon=True)
            thread.start()
        self._send_json(202, {"status": "started"})


def main():
    if not UPDATER_TOKEN:
        sys.stderr.write(
            "[updater] WARNING: UPDATER_TOKEN is not set — every /update request "
            "will be rejected. Set UPDATER_TOKEN in .env to enable the trigger.\n"
        )
    if not GITHUB_TOKEN:
        sys.stderr.write(
            "[updater] WARNING: GITHUB_TOKEN is not set — `git pull` will fail against "
            "a private remote. Set GITHUB_TOKEN in .env (compose passes it via env_file).\n"
        )
    if UPDATE_COMMAND:
        sys.stderr.write(f"[updater] update recipe: operator-configured UPDATE_COMMAND\n")
    else:
        sys.stderr.write("[updater] update recipe: built-in default "
                         "(git pull && docker compose build && docker compose up -d)\n")
    server = ThreadingHTTPServer(("0.0.0.0", UPDATER_PORT), Handler)
    sys.stderr.write(f"[updater] listening on :{UPDATER_PORT}, project dir {PROJECT_DIR}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
