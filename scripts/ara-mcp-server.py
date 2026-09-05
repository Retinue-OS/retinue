#!/usr/bin/env python3
"""Ask-Ara: an MCP server that lets an outside Claude client consult Ara.

Retinue's knowledge — the chambers, the life store, the projects, the running
state of the system — lives inside this container. A Claude client working
somewhere else (a local cowork session, the desktop app, another agent) has none
of it, so it has to interrupt the user for facts the user has already told Ara.
This server closes that gap: the outside client asks Ara, and only falls back to
asking the human when Ara cannot answer.

It speaks MCP over Streamable HTTP (JSON-RPC 2.0 on ``POST /mcp``, JSON
responses, no server-initiated stream) and is stateless: no session id, so any
request stands on its own and a reconnecting client loses nothing.

Read-only by intent
-------------------
The tools answer questions and read project files. None of them sends a message,
commits, or edits anything: an outside client can learn what Retinue knows, not
act as it. The one thing that leaves a mark is ``tell_ara``, which drops a note
into the dashboard for the user to read.

That intent is enforced in depth, not by hope: ``Write``/``Edit``/``NotebookEdit``
are removed from the answering session outright, and it runs in the CLI's default
(ask) permission mode, where ``claude -p`` auto-denies anything the settings
allowlist does not already permit. ``Bash`` stays available — without it Ara
cannot query the life store or read the system's own state, which is most of her
value — so the boundary is the settings allowlist plus the prompt, not a
hermetic sandbox. Treat the credential accordingly.

Authentication
--------------
None of its own. Like the web gateway, it is published only through Traefik,
whose ``forwardAuth`` middleware calls the gateway's ``/auth`` before any request
reaches this process. Give the connector its own htpasswd user and scope that
user to this host with ``GATEWAY_BASIC_AUTH_SCOPES`` (see scripts/gateway_auth.py)
so the credential opens the MCP router and nothing else. A second token here
would be unusable anyway — a client can only send one ``Authorization`` header,
and Traefik already claims it.

More than one instance
----------------------
A client can attach several Retinue deployments at once — a private one and a
company one, say. They share no data, and the client namespaces the tools by
connector, so nothing collides technically. What does collide is meaning: two
servers introducing themselves in identical words leave the model to pick one at
random, and the wrong instance answers plausibly from its own, unrelated data
rather than admitting the question is not its own. ``ARA_MCP_IDENTITY`` and
``ARA_MCP_SCOPE_HINT`` give each instance a name and a remit, in the handshake,
in the tool descriptions and in the answering prompt, so the choice is made on
subject matter instead of order.

Configuration (environment):
    ARA_MCP_IDENTITY          what this instance calls itself (default "Ara")
    ARA_MCP_SCOPE_HINT        one line on what it covers, for clients with
                              several instances attached (default: unset)
    ARA_MCP_PORT              listen port (default 8110)
    ARA_MCP_WORKDIR           cwd for the answering session (default /workspace)
    ARA_MCP_MODEL             --model for the answering session (default: unset)
    ARA_MCP_TIMEOUT           seconds before an answering session is killed (600)
    ARA_MCP_SYNC_WAIT         seconds to wait before handing back a job handle (60)
    ARA_MCP_MAX_CONCURRENCY   concurrent answering sessions (2)
    ARA_MCP_RATE_LIMIT        max questions per rate-limit window (30)
    ARA_MCP_RATE_WINDOW       rate-limit window in seconds (3600)
    ARA_MCP_AUDIT             0 disables the cowork audit trail (default on)
    ARA_MCP_STATE_DIR         where the audit thread id is remembered
    WEB_GATEWAY_PORT          the local web gateway, for project reads (8080)
    CONVERSATION_BACKEND_TOKEN / CONVERSATION_BASE_URL  as for conversation-push.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_auth  # noqa: E402

IDENTITY = os.environ.get("ARA_MCP_IDENTITY", "").strip() or "Ara"
SCOPE_HINT = os.environ.get("ARA_MCP_SCOPE_HINT", "").strip()
PORT = int(os.environ.get("ARA_MCP_PORT", "8110"))
WORKDIR = os.environ.get("ARA_MCP_WORKDIR", "/workspace")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# The answering session is Ara at the door: an explicit ARA_MCP_MODEL wins,
# else the router tier answers (Ara junior) and may escalate to the frontier
# tier (docs/model-routing.md). Unset everything = untiered, as before.
MODEL = (os.environ.get("ARA_MCP_MODEL", "").strip()
         or os.environ.get("RETINUE_ROUTER_MODEL", "").strip())
FRONTIER_MODEL = os.environ.get("RETINUE_FRONTIER_MODEL", "").strip()
TIMEOUT = float(os.environ.get("ARA_MCP_TIMEOUT", "600"))
SYNC_WAIT = float(os.environ.get("ARA_MCP_SYNC_WAIT", "60"))
MAX_CONCURRENCY = int(os.environ.get("ARA_MCP_MAX_CONCURRENCY", "2"))
RATE_LIMIT = int(os.environ.get("ARA_MCP_RATE_LIMIT", "30"))
RATE_WINDOW = float(os.environ.get("ARA_MCP_RATE_WINDOW", "3600"))
AUDIT = os.environ.get("ARA_MCP_AUDIT", "1").strip() not in ("0", "false", "no")
STATE_DIR = Path(os.environ.get("ARA_MCP_STATE_DIR", "/root/.retinue/ara-mcp"))
JOB_RETENTION_SECONDS = int(os.environ.get("ARA_MCP_JOB_RETENTION", "3600"))

_GW_PORT = os.environ.get("WEB_GATEWAY_PORT", "8080")
GATEWAY_URL = os.environ.get("ARA_MCP_GATEWAY_URL", f"http://localhost:{_GW_PORT}")
CONVERSATION_TOKEN = os.environ.get("CONVERSATION_BACKEND_TOKEN", "").strip()
CONVERSATION_BASE_URL = os.environ.get("CONVERSATION_BASE_URL", "").rstrip("/")

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = PROTOCOL_VERSIONS[0]
VERSION = "1.1.0"


def _slug(name: str) -> str:
    """A protocol-safe server name derived from the identity."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "ara"


def _server_info(identity: str | None = None) -> dict:
    name = identity or IDENTITY
    return {"name": _slug(name), "title": f"Ask {name}", "version": VERSION}

# Tools the answering session may never have. Bash is deliberately not on this
# list — see the module docstring for why, and what the real boundary is.
FORBIDDEN_TOOLS = ("Write", "Edit", "NotebookEdit")

# Handed to the client on initialize and, for most clients, injected into its
# system context. This is the whole point of the connector: it changes the
# client's default from "interrupt the human" to "ask Ara, then the human".
# Written around the configured identity rather than a fixed name, and without a
# pronoun for it: a deployment names its own instance, and nothing here should
# decide what that name should be called.

def _instructions(identity: str | None = None, scope: str | None = None) -> str:
    name = identity or IDENTITY
    scope = SCOPE_HINT if scope is None else scope.strip()
    remit = f" It covers: {scope}." if scope else ""
    paragraphs = [
        f"{name} coordinates Retinue, the user's personal agent system, and has "
        "what you do not: the user's projects and their current state, their "
        "calendar and correspondence, the mounted chambers and their data, the "
        "life triple store, and the history of decisions already taken.",

        "When something about the user's situation, data, history, or "
        "preferences is unclear — before you guess, and before you interrupt "
        f"the user — ask {name} with `ask_ara`. Ask in plain prose, the way you "
        "would ask a colleague who has been in every previous meeting; include "
        "what you are trying to do, so the answer can address the question "
        f"behind the question. Only put it to the user if {name} answers that "
        "this is not known here, or if the decision is genuinely theirs to make "
        "(a preference, an approval, a commitment on their behalf).",

        f"{name} answers, and does not act: no message will be sent, no code "
        "committed, no file changed on your behalf. If something needs doing, "
        "you will be told what and by whom, and you or the user carry it out.",

        "This client may have several Retinue instances attached at once, each "
        "holding different data and each presenting these tools under its own "
        f"connector prefix. This one is {name}.{remit} They share nothing, so a "
        "question that belongs to another instance has to go to that one: "
        f"{name} will tell you it is not held here rather than answer from "
        "unrelated data of its own. Pick the connector whose remit fits the "
        "question, and if none plainly does, ask the user which.",

        "Also available: `list_projects` and `get_project` read the user's "
        "project files directly — cheaper and more precise than asking when you "
        "already know which project you mean. `tell_ara` leaves the user a note "
        "in their dashboard; use it to report something worth their attention "
        "rather than to ask a question.",
    ]
    # Wrapped on render, not in the source: the identity is substituted in, so
    # hand-wrapped lines would go ragged for any name but the default.
    return "\n\n".join(textwrap.fill(p, 79) for p in paragraphs) + "\n"


SERVER_INFO = _server_info()
SERVER_INSTRUCTIONS = _instructions()

TOOLS = [
    {
        "name": "ask_ara",
        "title": f"Ask {IDENTITY}",
        "description": (
            f"Ask {IDENTITY} — the user's personal agent"
            + (f", covering {SCOPE_HINT}" if SCOPE_HINT else "")
            + " — a question about the user's "
            "projects, data, correspondence, schedule, preferences, or the state "
            "of their system. Use this instead of asking the user whenever the "
            f"answer is something {IDENTITY} would already know. If several "
            "Retinue connectors are attached, pick the one whose remit fits the "
            "question. Answers can take a "
            "while: if this returns status \"pending\", poll get_answer with the "
            "returned job_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question, in plain prose.",
                },
                "context": {
                    "type": "string",
                    "description": (
                        "What you are working on and why you are asking. Lets "
                        "Ara answer the question behind the question."
                    ),
                },
            },
            "required": ["question"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "get_answer",
        "title": "Get a pending answer",
        "description": (
            "Retrieve the answer to an earlier ask_ara call that returned "
            "status \"pending\". Poll every few seconds; status is one of "
            "pending, done, error."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_projects",
        "title": "List the user's projects",
        "description": (
            "List the user's active projects with their status, current actor "
            "and next action. Cheap and immediate — prefer it over ask_ara when "
            "you only need the project list."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_project",
        "title": "Read one project",
        "description": (
            "Read one project's full source (frontmatter and body) as Markdown. "
            "Takes the project id/URI as returned by list_projects."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "tell_ara",
        "title": "Leave the user a note",
        "description": (
            f"Leave a note for the user in the {IDENTITY} dashboard — something "
            "they should know or decide about, arising from the work you are "
            "doing. Not for questions you need answered now; use ask_ara for "
            "those. Returns a link to the thread."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "The note, in prose."},
                "title": {"type": "string", "description": "Short thread title."},
            },
            "required": ["note"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
]

_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_worker_pool = threading.BoundedSemaphore(MAX_CONCURRENCY)
_rate_hits: deque = deque()
_rate_lock = threading.Lock()
_audit_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[ara-mcp] {msg}", flush=True)


# ── Rate limiting ─────────────────────────────────────────────────────────────
# One credential drives this connector, so a single global bucket is the right
# granularity: it caps what a leaked or looping client can spend, which is the
# thing worth capping.

def _rate_ok() -> bool:
    if RATE_LIMIT <= 0:
        return True
    now = time.time()
    with _rate_lock:
        while _rate_hits and _rate_hits[0] < now - RATE_WINDOW:
            _rate_hits.popleft()
        if len(_rate_hits) >= RATE_LIMIT:
            return False
        _rate_hits.append(now)
        return True


# ── Job store ─────────────────────────────────────────────────────────────────

def _prune_jobs() -> None:
    cutoff = time.time() - JOB_RETENTION_SECONDS
    with _jobs_lock:
        for jid in [j for j, job in _jobs.items()
                    if job["status"] != "pending" and job.get("finished", 0) < cutoff]:
            _jobs.pop(jid, None)


def _finish_job(job_id: str, status: str, answer: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(status=status, answer=answer, finished=time.time())
            job["event"].set()


# ── The answering session ─────────────────────────────────────────────────────

def _build_prompt(question: str, context: str, identity: str | None = None,
                  scope: str | None = None) -> str:
    name = identity or IDENTITY
    scope = SCOPE_HINT if scope is None else scope.strip()
    parts = [
        "An outside Claude client is working with the user and has consulted "
        "you through the Ask-Ara MCP connector. Answer from what Retinue knows: "
        "the chambers, the life store, the project files, the system's own state.",
        "",
    ]
    if scope:
        # Several instances may be attached to the same client, and the client
        # can misroute. Saying "not mine" is then the useful answer: it sends
        # the client to the instance that holds it, where a confident answer
        # from adjacent local data would send it nowhere.
        parts += [
            f"You are {name}, the Retinue instance covering: {scope}. The client "
            "may have other Retinue instances attached, holding data you do not. "
            "If this question falls outside your remit, say so plainly in one "
            "sentence and name what it would belong to — do not answer it from "
            "adjacent data of your own.",
            "",
        ]
    parts += [
        "This is an advisory, read-only query. Do not send messages, do not "
        "commit, do not modify any file. If something needs doing, say what and "
        "by whom — the client or the user will carry it out.",
        "",
        "Answer in prose, directly and concretely, and cite the file or source "
        "you took each fact from. If you do not know, say so plainly in one "
        "sentence — the client will then ask the user, which is the correct "
        "fallback. Do not speculate to fill the gap.",
        "",
        f"Question: {question}",
    ]
    if context:
        parts += ["", f"What the client is working on: {context}"]
    return "\n".join(parts)


def _run_once(prompt: str, model: str,
              escalate_flag: Path | None) -> tuple[str, str]:
    """One `claude -p` answering run on `model`. Returns ``(status, text)``."""
    cmd = [CLAUDE_BIN, "-p", "--output-format=json"]
    if model:
        cmd += ["--model", model]
    for tool in FORBIDDEN_TOOLS:
        cmd += ["--disallowed-tools", tool]
    # RETINUE_SESSION_MODEL advertises the model this session runs on (for
    # memory stamping); cleared rather than inherited so a stale value never
    # mislabels a session. RETINUE_ESCALATE_FILE is junior's escape hatch.
    env = dict(os.environ)
    env.pop("RETINUE_SESSION_MODEL", None)
    env.pop("RETINUE_ESCALATE_FILE", None)
    if model:
        env["RETINUE_SESSION_MODEL"] = model
    if escalate_flag is not None:
        env["RETINUE_ESCALATE_FILE"] = str(escalate_flag)
    # The prompt goes on stdin, never as a trailing argument: --disallowed-tools
    # is variadic, so a positional prompt after it is swallowed as one more tool
    # name and the session dies with "Input must be provided either through
    # stdin or as a prompt argument when using --print".
    # Refresh an access token about to expire before the session starts —
    # once, under the lock every framework spawner shares (docs/claude-auth.md).
    claude_auth.ensure_fresh_credentials(log=log)
    try:
        proc = subprocess.run(cmd, cwd=WORKDIR, input=prompt, capture_output=True,
                              text=True, timeout=TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return "error", f"Ara did not answer within {int(TIMEOUT)}s."
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as-is
        return "error", f"Could not start the answering session: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-800:]
        return "error", f"The answering session failed: {detail or 'no output'}"
    try:
        payload = json.loads(proc.stdout)
        text = (payload.get("result") or "").strip()
    except Exception:
        text = (proc.stdout or "").strip()
    return ("done", text) if text else ("error", "Ara returned an empty answer.")


def _run_claude(prompt: str) -> tuple[str, str]:
    """Run one answering session, escalating junior to senior when signalled.

    Below the frontier tier the session gets RETINUE_ESCALATE_FILE; if it
    creates that file the junior answer is discarded and the same prompt is
    re-run on the frontier tier. The existing slow-answer/job-id flow absorbs
    the extra latency of an escalated answer.
    """
    escalatable = bool(FRONTIER_MODEL) and MODEL != FRONTIER_MODEL
    flag = (Path(tempfile.gettempdir()) / f"ara-mcp-escalate-{uuid.uuid4().hex}"
            if escalatable else None)
    try:
        status, text = _run_once(prompt, MODEL, flag)
        if flag is not None and flag.exists():
            print(f"[ara-mcp] junior escalated — re-running on {FRONTIER_MODEL}",
                  file=sys.stderr, flush=True)
            flag.unlink(missing_ok=True)
            status, text = _run_once(prompt, FRONTIER_MODEL, None)
        return status, text
    finally:
        if flag is not None:
            flag.unlink(missing_ok=True)


def _answer_worker(job_id: str, question: str, context: str) -> None:
    with _worker_pool:
        status, text = _run_claude(_build_prompt(question, context))
    _finish_job(job_id, status, text)
    _audit(question, context, status, text)


# ── Audit trail ───────────────────────────────────────────────────────────────
# Every exchange is appended to a dashboard thread of kind "cowork", one per day.
# Quiet: no unread badge, no Web Push — it is a record the user can read when
# curious, not a thing that should buzz their phone each time a client asks
# whether the invoice was paid. Failure to audit never fails the answer.

def _gateway_post(path: str, payload: dict) -> dict | None:
    if not CONVERSATION_TOKEN:
        return None
    req = urllib.request.Request(
        f"{GATEWAY_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Conversation-Backend-Token": CONVERSATION_TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _audit_thread_id() -> str | None:
    """The id of today's cowork thread, opening it on the first use of the day."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    marker = STATE_DIR / f"thread-{day}.json"
    with _audit_lock:
        try:
            if marker.exists():
                return json.loads(marker.read_text()).get("id")
        except Exception as exc:  # noqa: BLE001
            log(f"audit: unreadable state {marker}: {exc}")
        try:
            body = _gateway_post("/internal/conversations", {
                "title": f"Cowork · {day}",
                "message": ("Questions relayed to Ara by an outside Claude "
                            "client through the Ask-Ara MCP connector."),
                "kind": "cowork",
                "quiet": True,
            })
        except Exception as exc:  # noqa: BLE001
            log(f"audit: could not open today's thread: {exc}")
            return None
        cid = (body or {}).get("id")
        if cid:
            try:
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps({"id": cid}))
            except Exception as exc:  # noqa: BLE001
                log(f"audit: could not persist state: {exc}")
        return cid


def _audit(question: str, context: str, status: str, answer: str) -> None:
    if not AUDIT:
        return
    try:
        cid = _audit_thread_id()
        if not cid:
            return
        lines = [f"**Asked:** {question}"]
        if context:
            lines.append(f"*Context:* {context}")
        label = "Answered" if status == "done" else "Failed"
        lines += ["", f"**{label}:**", answer]
        _gateway_post(f"/internal/conversations/{cid}/messages",
                      {"message": "\n".join(lines), "quiet": True})
    except Exception as exc:  # noqa: BLE001 — auditing must never break answering
        log(f"audit: append failed: {exc}")


# ── Tool implementations ──────────────────────────────────────────────────────

def _gateway_get(path: str) -> dict:
    with urllib.request.urlopen(f"{GATEWAY_URL}{path}", timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _tool_ask_ara(args: dict) -> dict:
    question = (args.get("question") or "").strip()
    if not question:
        return {"error": "question is required"}
    if not _rate_ok():
        return {"error": (f"Rate limit reached ({RATE_LIMIT} questions per "
                          f"{int(RATE_WINDOW)}s). Ask the user directly.")}
    context = (args.get("context") or "").strip()
    _prune_jobs()
    job_id = uuid.uuid4().hex
    event = threading.Event()
    with _jobs_lock:
        _jobs[job_id] = {"status": "pending", "answer": "",
                         "created": time.time(), "event": event}
    threading.Thread(target=_answer_worker, args=(job_id, question, context),
                     daemon=True).start()
    # Most questions come back well inside the sync window, so hold the call
    # open briefly and answer in one round trip; hand back a handle only when
    # Ara is actually taking her time.
    if event.wait(SYNC_WAIT):
        with _jobs_lock:
            job = _jobs.get(job_id, {})
        return {"status": job.get("status", "error"),
                "answer": job.get("answer", "")}
    return {"status": "pending", "job_id": job_id,
            "note": "Ara is still working. Poll get_answer with this job_id."}


def _tool_get_answer(args: dict) -> dict:
    job_id = (args.get("job_id") or "").strip()
    if not _JOB_ID_RE.match(job_id):
        return {"error": "unknown job_id"}
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return {"error": "unknown or expired job_id"}
        return {"status": job["status"], "answer": job["answer"]}


def _tool_list_projects(_args: dict) -> dict:
    try:
        return _gateway_get("/projects")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not read the project list: {exc}"}


def _tool_get_project(args: dict) -> dict:
    pid = (args.get("id") or "").strip()
    if not pid:
        return {"error": "id is required"}
    try:
        return _gateway_get("/projects/item?id=" + urllib.parse.quote(pid, safe=""))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"error": f"no project with id {pid}"}
        return {"error": f"could not read project {pid}: HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not read project {pid}: {exc}"}


def _tool_tell_ara(args: dict) -> dict:
    note = (args.get("note") or "").strip()
    if not note:
        return {"error": "note is required"}
    if not CONVERSATION_TOKEN:
        return {"error": "the dashboard backend is not configured"}
    title = (args.get("title") or "").strip() or None
    try:
        body = _gateway_post("/internal/conversations",
                             {"message": note, "title": title}) or {}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not open the thread: {exc}"}
    return {"status": "delivered", "thread_id": body.get("id"),
            "url": body.get("url", "")}


TOOL_IMPL = {
    "ask_ara": _tool_ask_ara,
    "get_answer": _tool_get_answer,
    "list_projects": _tool_list_projects,
    "get_project": _tool_get_project,
    "tell_ara": _tool_tell_ara,
}


# ── JSON-RPC dispatch ─────────────────────────────────────────────────────────

def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _result(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def handle_rpc(msg: dict) -> dict | None:
    """Handle one JSON-RPC message. Returns None for notifications."""
    rid = msg.get("id")
    method = msg.get("method") or ""
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if method == "initialize":
        asked = (params.get("protocolVersion") or "").strip()
        version = asked if asked in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
        client = (params.get("clientInfo") or {}).get("name", "unknown")
        log(f"initialize from {client!r} (protocol {version})")
        return _result(rid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": SERVER_INSTRUCTIONS,
        })

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if is_notification:
        return None

    if method == "ping":
        return _result(rid, {})

    if method == "tools/list":
        return _result(rid, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name") or ""
        impl = TOOL_IMPL.get(name)
        if impl is None:
            return _error(rid, -32602, f"unknown tool: {name}")
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}
        try:
            payload = impl(args)
        except Exception as exc:  # noqa: BLE001 — a tool fault is not a protocol fault
            log(f"tool {name} raised: {exc}")
            payload = {"error": f"{name} failed: {exc}"}
        # Errors are reported as isError content, not a JSON-RPC error: the
        # model should see and reason about them, not have the call vanish.
        return _result(rid, {
            "content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
            "isError": bool(payload.get("error")),
        })

    return _error(rid, -32601, f"method not found: {method}")


# ── HTTP ──────────────────────────────────────────────────────────────────────

MAX_BODY = 1 << 20


class Handler(BaseHTTPRequestHandler):
    server_version = f"ara-mcp/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter than the default access log
        pass

    def _send(self, status: int, body: dict | None, ctype="application/json") -> None:
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if raw:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw and self.command != "HEAD":
            self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            with _jobs_lock:
                pending = sum(1 for j in _jobs.values() if j["status"] == "pending")
            self._send(200, {"status": "ok", "service": "ara-mcp",
                             "identity": IDENTITY, "scope": SCOPE_HINT,
                             "tools": [t["name"] for t in TOOLS],
                             "pending": pending, "audit": AUDIT})
            return
        if path in ("/mcp", "/"):
            # Streamable HTTP allows a server with no server-initiated stream to
            # refuse the GET; clients fall back to plain POST request/response.
            self._send(405, {"error": "this server does not offer an SSE stream"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path not in ("/mcp", "/"):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send(400, _error(None, -32600, "missing or oversized body"))
            return
        try:
            msg = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send(400, _error(None, -32700, "parse error"))
            return

        # A batch is a JSON array; each element is answered independently and
        # notifications contribute no element to the reply.
        if isinstance(msg, list):
            replies = [r for r in (handle_rpc(m) for m in msg if isinstance(m, dict))
                       if r is not None]
            self._send(202 if not replies else 200, replies or None)
            return
        if not isinstance(msg, dict):
            self._send(400, _error(None, -32600, "invalid request"))
            return
        reply = handle_rpc(msg)
        if reply is None:
            self._send(202, None)  # notification: accepted, nothing to say
            return
        self._send(200, reply)


def main() -> None:
    log(f"identity={IDENTITY!r} ({SERVER_INFO['name']}) "
        f"scope={SCOPE_HINT or '(unset)'!r}")
    log(f"workdir={WORKDIR} model={MODEL or '(default)'} audit={AUDIT} "
        f"concurrency={MAX_CONCURRENCY} rate={RATE_LIMIT}/{int(RATE_WINDOW)}s")
    if not CONVERSATION_TOKEN:
        log("CONVERSATION_BACKEND_TOKEN unset — tell_ara and the audit trail "
            "are disabled; ask_ara still works.")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log(f"listening on :{PORT} (POST /mcp)")
    server.serve_forever()


if __name__ == "__main__":
    main()
