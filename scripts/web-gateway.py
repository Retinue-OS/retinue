#!/usr/bin/env python3
"""
HTTP gateway that routes incoming messages to a named Claude Code session.

POST /message
  Body (JSON): {"message": "...", "question": "...(optional display text)",
                "files": [{"filename", "content_type", "data"(base64)}, ...]
                (optional — e.g. an image a messenger gateway forwards; each
                file is materialized under MESSAGE_FILES_DIR and its on-disk
                path appended to the prompt so the session can read it)}
  Body (plain text): the message itself

GET /conversation
  Returns an HTML index listing all days with recorded conversations,
  each linking to /conversation/YYYY-MM-DD.

GET /conversation/YYYY-MM-DD
  Returns the conversation for that UTC day as a human-readable HTML page.
  Each entry has a stable anchor (e.g. #entry-1749567890123) so callers can
  link directly to a specific exchange.

Conversation tabs (dashboard chat threads, distinct from the per-day log):
  GET  /conversations                 -> {"conversations": [summary, ...]}
                                         Optional filters: ?kind=chat|edit|all
                                         (default chat — project edit-command
                                         threads are hidden from normal lists)
                                         and ?project=<uri>.
  GET  /conversations/<id>            -> full thread {id,title,messages,...}
  POST /conversations                 -> open a new thread (body {message};
                                         optional kind: "chat"|"edit", project:
                                         <uri>, project_title). Ara answers
                                         asynchronously (poll the thread).

Projects (dashboard project pages):
  GET  /projects                      -> live card data (SPARQL over the life store)
  GET  /projects/item?id=<uri>        -> one project's raw Markdown + sha256
  POST /projects/item                 -> save an edited project file (body
                                         {id, content, base_sha}); 409 + current
                                         content on a concurrent change.
  POST /conversations/<id>/messages   -> user reply (body {message}); async reply.
  POST /conversations/<id>/read       -> clear the thread's unread flag.
  POST /internal/conversations        -> a retinue agent opens a thread that needs
                                         the user's decision. Token-gated via
                                         CONVERSATION_BACKEND_TOKEN (header
                                         X-Conversation-Backend-Token). Optional
                                         {kind: "cowork"} for the MCP connector's
                                         audit trail and {quiet: true} to append
                                         without an unread badge or Web Push.
  POST /internal/conversations/<id>/messages
                                      -> a retinue agent appends a message (with
                                         attachments) to an existing thread. Same
                                         token gate; same optional {quiet: true}.
                                         A non-quiet append un-archives the
                                         thread unless it is muted.
  POST /internal/conversations/<id>/flags
                                      -> a retinue agent sets {archived, muted}
                                         (either or both). Same token gate. The
                                         only way to set `muted`, which is what
                                         keeps a thread archived when new
                                         messages are filed into it.

News feed (dashboard news page; see scripts/news_store.py):
  GET  /news                          -> {"generated", "items": [...]} ranked at
                                         read time. ?scope=feed|read|hidden|all
                                         (default feed), ?limit=<n>.
  GET  /news/preferences              -> {"markdown", "updated"} — the Herald's
                                         memory of what the user cares about.
  POST /news/preferences              -> replace it (body {markdown}); the user
                                         may edit their own profile by hand.
  POST /news/feedback                 -> one user signal (body {id?, signal, note?};
                                         signal: up|down|read|hide|note). Nudges
                                         that item now and logs it for the Herald.

Push notifications (dashboard PWA; see scripts/push_notify.py):
  GET  /push/config                   -> {"enabled": bool, "publicKey": <VAPID>}
  POST /push/subscribe                -> register a PushSubscription (body is the
                                         browser's subscription JSON verbatim).
  POST /push/unsubscribe              -> drop one (body {endpoint}); idempotent.

Session logic:
- Conversations are keyed by requester identity (the "on-behalf-of" field, e.g.
  the Signal sender). Each key gets its own Claude session, state entry and lock,
  so a conversation is serialized within a key while different keys run in
  parallel. Requests without an identity share the default "Web" key.
- For each key, if a session exists and was used less than
  SESSION_MAX_IDLE_SECONDS ago, resume it with --resume <session_id>.
  Otherwise start a fresh session.
- Total concurrency is bounded by a small worker pool (WEB_GATEWAY_MAX_CONCURRENCY)
  to keep CPU/memory and subprocess count sane on a personal box.

State is persisted in STATE_FILE (a map of session-key -> {session_id,
last_activity}) so restarts survive as long as the sessions themselves are still
valid on the Claude Code side.

Conversation log:
- Every exchange is appended to a per-day JSON file under CONVERSATION_DIR.
- Set CONVERSATION_BASE_URL to the public URL prefix (e.g. https://retinue.example.com)
  to also include an "entry_url" (format: /conversation/YYYY-MM-DD#entry-{ts}) in
  each POST /message response.
- Files are stored in CONVERSATION_DIR (default /tmp/web-conversations/), one file
  per UTC day named YYYY-MM-DD.json.
"""

import base64
import binascii
import hashlib
import html
import hmac
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from markdown_it import MarkdownIt
from requester_identity import normalize_requester_identity
import email_client as ec
import gateway_auth
import messenger_gateways
import news_store
import push_notify


# Claude Code ships as an npm package whose auto-updater briefly swaps the
# `claude` symlink; a subprocess spawned in that window fails with ENOENT
# ([Errno 2] No such file or directory: 'claude'). Retry a few times with a
# short backoff so a mid-update race is invisible instead of surfacing as an
# error in the user's conversation.
CLAUDE_SPAWN_RETRIES = 5
CLAUDE_SPAWN_BACKOFF_SECONDS = 1.0
CLAUDE_BIN = "/usr/bin/claude"


def _run_claude(cmd, **kwargs):
    """subprocess.run for the `claude` binary, tolerant of the transient
    ENOENT window while Claude Code's auto-updater replaces it."""
    for attempt in range(CLAUDE_SPAWN_RETRIES):
        try:
            return subprocess.run(cmd, **kwargs)
        except FileNotFoundError:
            if attempt == CLAUDE_SPAWN_RETRIES - 1:
                raise
            time.sleep(CLAUDE_SPAWN_BACKOFF_SECONDS)

STATE_FILE = os.environ.get("WEB_GATEWAY_STATE", "/tmp/web-session-state.json")
PORT = int(os.environ.get("WEB_GATEWAY_PORT", "8080"))
CLAUDE_PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")
CLAUDE_MODEL = os.environ.get("RETINUE_CLAUDE_MODEL", "").strip()

# ── Per-conversation model selection ───────────────────────────────────────────
# A dashboard thread runs a fresh `claude -p` per turn (no long-lived in-memory
# state pinned to a model), so which model answers a thread is a free per-turn
# choice — pickable at creation and switchable mid-thread, effective from the
# next turn. The picker governs Ara's OWN turn only: dispatched subagents (Coach,
# Medic, Archivist, Ari) run on their own hard-wired models regardless.
#
# The list of offered models comes from LiteLLM when the deployment routes
# through it: the gateway reads GET <RETINUE_LITELLM_URL>/model/info (default:
# ANTHROPIC_BASE_URL, which already points at the LiteLLM service) and offers
# every route whose `model_info` carries `retinue_picker: true`, labeled by
# `retinue_label`. That makes LiteLLM the single place models are managed —
# a model added there (config or admin UI, flags settable in both) shows up in
# the picker with no gateway config, and turns route through the same proxy
# that advertised it, so the picker can never offer a model that isn't served.
# The list is cached for RETINUE_MODELS_CACHE_SECONDS (default 60); on a failed
# refresh the last good list keeps serving.
#
# Deployments not routing through LiteLLM keep the static sources, which also
# back the picker whenever LiteLLM is unreachable or advertises no flagged
# route: RETINUE_CONVERSATION_MODELS (an inline JSON array — an explicit
# override that also WINS over LiteLLM), else the JSON-LD document
# `config/conversation-models.jsonld` (path override:
# RETINUE_CONVERSATION_MODELS_FILE; also derived into the life store by
# scripts/emit-conversation-models.py), else the built-in default below.
#
# Either way the list holds {"id","label"} objects. `id` is passed to
# `claude --model` (for LiteLLM-sourced entries the id is the route's
# model_name, which `claude` sends verbatim); `label` is what the dashboard
# shows. The empty-string id means "use the gateway's configured default"
# (CLAUDE_MODEL / whatever `claude` resolves) — always offered first so a
# thread can defer.
_DEFAULT_MODEL_ENTRY = {"id": "", "label": "Default"}

_DEFAULT_CONVERSATION_MODELS = [
    {"id": "", "label": "Default"},
    {"id": "opus", "label": "Opus (deepest reasoning)"},
    {"id": "sonnet", "label": "Sonnet (balanced)"},
    {"id": "haiku", "label": "Haiku (fastest)"},
]

_DEFAULT_CONVERSATION_MODELS_FILE = str(
    Path(__file__).resolve().parent.parent / "config" / "conversation-models.jsonld"
)


def _conversation_models_file() -> str:
    return os.environ.get(
        "RETINUE_CONVERSATION_MODELS_FILE", _DEFAULT_CONVERSATION_MODELS_FILE
    )


def _coerce_conversation_models(parsed: object) -> list[dict]:
    """Normalise a parsed models array into validated {"id","label"} dicts.

    Accepts the array itself or a JSON-LD document wrapping it under `models`.
    Returns [] when nothing usable is present, so callers can fall back."""
    if isinstance(parsed, dict):
        parsed = parsed.get("models", [])
    if not isinstance(parsed, list):
        return []
    models = []
    for item in parsed:
        if not isinstance(item, dict) or "id" not in item:
            continue
        mid = str(item["id"]).strip()
        label = str(item.get("label") or mid or "Default").strip()
        models.append({"id": mid, "label": label})
    return models


def _env_conversation_models() -> list[dict] | None:
    """The RETINUE_CONVERSATION_MODELS inline override, or None if unset/invalid.

    An explicit inline list is the deployment saying "offer exactly this", so it
    wins over everything — including a reachable LiteLLM."""
    raw = os.environ.get("RETINUE_CONVERSATION_MODELS", "").strip()
    if not raw:
        return None
    try:
        models = _coerce_conversation_models(json.loads(raw))
        if models:
            return models
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    print("[web-gateway] invalid RETINUE_CONVERSATION_MODELS; falling back to LiteLLM/file/default",
          flush=True)
    return None


def _load_static_conversation_models() -> list[dict]:
    # The JSON-LD file, read as plain JSON.
    path = _conversation_models_file()
    try:
        with open(path, encoding="utf-8") as fh:
            models = _coerce_conversation_models(json.load(fh))
        if models:
            return models
        print(f"[web-gateway] no models in {path}; using default list", flush=True)
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError, ValueError):
        print(f"[web-gateway] invalid {path}; using default list", flush=True)
    return list(_DEFAULT_CONVERSATION_MODELS)


_ENV_CONVERSATION_MODELS = _env_conversation_models()
_STATIC_CONVERSATION_MODELS = _load_static_conversation_models()

# ── LiteLLM-advertised models ──────────────────────────────────────────────────
# RETINUE_LITELLM_URL points the picker at a LiteLLM proxy; it defaults to
# ANTHROPIC_BASE_URL, which in a LiteLLM deployment is already the proxy. When
# the deployment talks straight to Anthropic instead, /model/info simply fails
# and the static list serves — but skip the known-external API host outright so
# the cache refresh never phones out of the stack.
_LITELLM_URL = (
    os.environ.get("RETINUE_LITELLM_URL", "").strip()
    or os.environ.get("ANTHROPIC_BASE_URL", "").strip()
)
if "api.anthropic.com" in _LITELLM_URL:
    _LITELLM_URL = ""
_LITELLM_MODELS_CACHE_SECONDS = float(
    os.environ.get("RETINUE_MODELS_CACHE_SECONDS", "60")
)
_LITELLM_PICKER_FLAG = "retinue_picker"
_LITELLM_LABEL_KEY = "retinue_label"


def _litellm_headers() -> dict[str, str]:
    """Auth headers for LiteLLM management calls.

    RETINUE_LITELLM_KEY wins when set; otherwise reuse ANTHROPIC_CUSTOM_HEADERS
    ("Name: Value" per line) — the exact headers Claude Code already sends to
    the proxy, which carry the key in the proxy's configured key header."""
    key = os.environ.get("RETINUE_LITELLM_KEY", "").strip()
    if key:
        value = key if key.lower().startswith("bearer ") else f"Bearer {key}"
        return {"x-litellm-api-key": value, "Authorization": value}
    headers = {}
    for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip() and value.strip():
            headers[name.strip()] = value.strip()
    return headers


def _coerce_litellm_models(parsed: object) -> list[dict]:
    """Picker entries from a LiteLLM GET /model/info response.

    Only routes flagged `retinue_picker` in their model_info are offered —
    routing plumbing (wildcards, fallback routes) stays invisible without the
    flag, and wildcard names are dropped even if flagged since a pattern is not
    a model id. Duplicate names (config + DB copy of a route) collapse to the
    first occurrence.

    Same-label routes collapse too. A picker route also surfaces under its
    target id (`claude-opus-5` and `anthropic/claude-opus-5` are two names for
    one model, both carrying the same model_info), so name-only dedup let every
    entry appear twice. The label is what the user picks by, so two entries
    reading "Opus (deepest reasoning)" are a bug whatever their ids; the first
    occurrence wins, which is the route as the config names it."""
    if not isinstance(parsed, dict):
        return []
    models, seen, labelled = [], set(), set()
    for item in parsed.get("data") or []:
        if not isinstance(item, dict):
            continue
        info = item.get("model_info")
        if not isinstance(info, dict) or not info.get(_LITELLM_PICKER_FLAG):
            continue
        mid = str(item.get("model_name") or "").strip()
        if not mid or "*" in mid or mid in seen:
            continue
        label = str(info.get(_LITELLM_LABEL_KEY) or mid).strip()
        if label in labelled:
            continue
        seen.add(mid)
        labelled.add(label)
        models.append({"id": mid, "label": label})
    return models


def _fetch_litellm_models() -> list[dict]:
    url = _LITELLM_URL.rstrip("/") + "/model/info"
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", **_litellm_headers()}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return _coerce_litellm_models(json.load(resp))


_litellm_models_lock = threading.Lock()
_litellm_models_cache: dict = {"fetched": None, "models": None, "failed": False}


def _litellm_conversation_models(force: bool = False) -> list[dict] | None:
    """LiteLLM's flagged models, cached; None when there is nothing usable.

    None covers "not configured", "unreachable with no last-good list" and
    "reachable but zero flagged routes" alike — in each case the caller falls
    back to the static list. A refresh failure keeps serving the last good
    list, so a LiteLLM restart doesn't collapse threads to the default model.

    The lock guards only the cache dict, never the HTTP fetch — a slow
    upstream must not stall cache-hit readers behind it. Two threads may
    therefore fetch concurrently after a simultaneous expiry; that duplicate
    is cheaper than a serialized stall."""
    if not _LITELLM_URL:
        return None
    with _litellm_models_lock:
        fetched = _litellm_models_cache["fetched"]
        if (not force and fetched is not None
                and time.monotonic() - fetched < _LITELLM_MODELS_CACHE_SECONDS):
            return _litellm_models_cache["models"]
    models = error = None
    try:
        models = _fetch_litellm_models() or None
    except (OSError, ValueError) as exc:
        # URLError/HTTPError are OSErrors; JSONDecodeError is a ValueError.
        error = exc
    with _litellm_models_lock:
        if error is None:
            _litellm_models_cache["models"] = models
            _litellm_models_cache["failed"] = False
        else:
            # Log transitions only, not every quiet minute LiteLLM is absent.
            if not _litellm_models_cache["failed"]:
                print(f"[web-gateway] LiteLLM model list unavailable ({error}); "
                      "using last good/static list", flush=True)
            _litellm_models_cache["failed"] = True
        _litellm_models_cache["fetched"] = time.monotonic()
        return _litellm_models_cache["models"]


def _conversation_models(force: bool = False) -> list[dict]:
    """The list the picker offers right now, in precedence order:
    env override > LiteLLM-advertised > file > built-in default."""
    if _ENV_CONVERSATION_MODELS:
        return _ENV_CONVERSATION_MODELS
    dynamic = _litellm_conversation_models(force=force)
    if dynamic:
        # LiteLLM knows nothing of "use the gateway default" — synthesize it.
        return [dict(_DEFAULT_MODEL_ENTRY), *dynamic]
    return _STATIC_CONVERSATION_MODELS


def _model_offered(mid: str, refresh: bool = False) -> bool:
    """Whether a non-empty model id is currently on the offered list.

    With refresh=True a miss forces one cache refresh before rejecting, so a
    model just added in LiteLLM is selectable immediately rather than after
    the cache TTL. Reserve that for the moments a human just picked a model
    (thread creation, the picker POST): routine lookups — _conv_summary runs
    one per thread on every list request — must stay cache-only, or each
    thread pinned to a since-dropped model would turn one GET /conversations
    into that many upstream fetches, unbounded by the TTL."""
    if any(m["id"] == mid for m in _conversation_models()):
        return True
    if not refresh:
        return False
    return any(m["id"] == mid for m in _conversation_models(force=True))

SESSION_MAX_IDLE_SECONDS = 3600  # 1 hour
REQUESTER_ALLOWLIST_PATH = os.environ.get("ACCEPTED_REQUESTERS_PATH", "")
CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR", "/workspace/chambers"))
REQUESTER_BLOCK_MESSAGE = (
    "Sorry, this requester is not authorised to use this gateway. "
    "Please ask the system owner to add this requester to the allowlist."
)
CONVERSATION_BASE_URL = os.environ.get("CONVERSATION_BASE_URL", "").rstrip("/")
CONVERSATION_DIR = Path(os.environ.get("CONVERSATION_DIR", "/tmp/web-conversations"))
CONVERSATION_DIR.mkdir(parents=True, exist_ok=True)

# ── Conversation tabs ──────────────────────────────────────────────────────────
# Each "tab" is a standalone chat thread with Ara, distinct from the per-day
# transcript log above. A thread can be opened by the user (from the dashboard)
# or by a retinue agent that needs a decision (e.g. "RSVP to this party — confirm
# or decline?"). Threads persist as one JSON file per id under CONVERSATIONS_DIR
# and each maps to its own Claude session (key "conv:<id>") so context is kept
# per thread. Agent-initiated threads use the token-gated /internal/conversations
# endpoint (CONVERSATION_BACKEND_TOKEN), mirroring the e-mail backend isolation.
# The deployment points CONVERSATIONS_DIR at the persistent /root volume (see
# docker-compose.yml) so threads survive container recreation; the /tmp default
# below is only for ad-hoc/dev runs that mount no volume.
CONVERSATIONS_DIR = Path(os.environ.get("CONVERSATIONS_DIR", "/tmp/web-tab-conversations"))
CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
# Files a thread carries so the user can download them from the dashboard (e.g.
# an e-mail attachment an agent forwards into a thread). Stored on disk under a
# per-thread directory, keyed by a server-generated id — the untrusted original
# filename is kept only as metadata, never used as a path component. Lives beside
# the thread JSON on the same persistent volume so downloads survive restarts.
CONVERSATION_ATTACHMENTS_DIR = CONVERSATIONS_DIR / "attachments"
CONVERSATION_ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
# Cap a single attachment's decoded size to keep memory and disk bounded.
MAX_ATTACHMENT_BYTES = int(os.environ.get("CONVERSATION_MAX_ATTACHMENT_BYTES", str(25 * 1024 * 1024)))
# Files forwarded with POST /message (e.g. an image received on a messenger
# channel — the gateways run in their own containers, so the bytes must travel
# with the request). They are materialized here so the answering session can
# read them from disk; deliberately ephemeral working data (default under /tmp,
# cleared on restart), unlike thread attachments the user downloads later.
MESSAGE_FILES_DIR = Path(os.environ.get("MESSAGE_FILES_DIR", "/tmp/web-gateway/message-files"))
# Drop stored message files after this long; swept opportunistically on the next
# store, so no timer thread is needed for what is best-effort hygiene anyway.
MESSAGE_FILES_TTL_SECONDS = float(os.environ.get("MESSAGE_FILES_TTL_SECONDS", str(7 * 86400)))
CONVERSATION_BACKEND_TOKEN = os.environ.get("CONVERSATION_BACKEND_TOKEN", "")
# Voice input: the dashboard uploads recorded audio here and we proxy it to the
# shared STT service (scripts/stt-service.py), which owns the Whisper model — so
# this image ships no ASR stack. Empty URL disables the feature (the endpoint
# then answers 503) and the dashboard hides its microphone button.
STT_SERVICE_URL = os.environ.get("STT_SERVICE_URL", "").strip()
STT_TOKEN = os.environ.get("STT_TOKEN", "").strip()
TRANSCRIBE_TIMEOUT = float(os.environ.get("TRANSCRIBE_TIMEOUT", "120"))
# Transcript cleanup. Whisper's raw output lands verbatim in the composer, so on
# the dashboard — unlike Signal, where the agent reads the transcript and answers
# what was meant — every recognition error is the user's to repair by hand. We
# run the transcript through a small model first, with the thread so far and the
# user's contact names as context (that is what fixes mangled names). Best
# effort: any failure returns the raw transcript unchanged.
TRANSCRIPT_CLEANUP = os.environ.get("TRANSCRIPT_CLEANUP", "1").strip().lower() not in ("0", "false", "no")
TRANSCRIPT_CLEANUP_MODEL = os.environ.get("TRANSCRIPT_CLEANUP_MODEL", "haiku").strip()
TRANSCRIPT_CLEANUP_TIMEOUT = float(os.environ.get("TRANSCRIPT_CLEANUP_TIMEOUT", "45"))
# How much of the thread to show the cleanup model, and how far a cleaned
# transcript may drift in length before we distrust it (a model that starts
# answering instead of correcting returns something much longer).
TRANSCRIPT_CLEANUP_CONTEXT_MESSAGES = 6
TRANSCRIPT_CLEANUP_MAX_GROWTH = 1.6
_CONV_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ATT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CONV_GET_RE = re.compile(r"^/conversations/([0-9a-f]{32})/?$")
_CONV_ATT_RE = re.compile(r"^/conversations/([0-9a-f]{32})/attachments/([0-9a-f]{32})/?$")
_CONV_MSG_RE = re.compile(r"^/conversations/([0-9a-f]{32})/messages/?$")
_INTERNAL_CONV_MSG_RE = re.compile(r"^/internal/conversations/([0-9a-f]{32})/messages/?$")
_INTERNAL_CONV_FLAGS_RE = re.compile(r"^/internal/conversations/([0-9a-f]{32})/flags/?$")
_CONV_READ_RE = re.compile(r"^/conversations/([0-9a-f]{32})/read/?$")
_CONV_ARCHIVE_RE = re.compile(r"^/conversations/([0-9a-f]{32})/archive/?$")
_CONV_UNARCHIVE_RE = re.compile(r"^/conversations/([0-9a-f]{32})/unarchive/?$")
_CONV_MODEL_RE = re.compile(r"^/conversations/([0-9a-f]{32})/model/?$")

# ── Push notifications ─────────────────────────────────────────────────────────
# The unread badge only exists while the dashboard is open, which is precisely
# not the case when Ara opens a thread that needs a decision. Web Push is what
# reaches an installed PWA with no page running. Keys and device subscriptions
# live beside the conversations, so they inherit the deployment's persistent
# volume; see scripts/push_notify.py. Push is optional: with pywebpush absent the
# endpoints report disabled and the dashboard hides its opt-in button.
PUSH_DIR = Path(os.environ.get("PUSH_DIR", str(CONVERSATIONS_DIR.parent / "push")))
push_notify.init(PUSH_DIR)

# ── Dashboard (PWA) static assets ──────────────────────────────────────────────
# The dashboard front-end is a static PWA served at the site root. Its shell
# (HTML/JS/CSS/icons) lives in WEBAPP_DIR; the curated JSON it renders lives in
# DASHBOARD_DATA_DIR and is served under /data/ (kept separate so a refresh job
# can write data without touching the baked-in shell).
WEBAPP_DIR = Path(os.environ.get("WEBAPP_DIR", "/workspace/webapp"))
DASHBOARD_DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_DIR", str(WEBAPP_DIR / "data")))

# Content hash of the whole shell tree, used as the service worker's cache name
# (see _serve_service_worker). Computed over every file under WEBAPP_DIR so ANY
# webapp change moves the hash automatically — no hand-bumped version to forget.
# Cached and only recomputed when a file's path/size/mtime changes, so the hot
# path stays a cheap stat sweep rather than a full re-read on every request.
# `data/` is excluded: it is curated JSON served no-store, not part of the
# cached shell, and it changes on its own cadence.
_SHELL_HASH_CACHE: dict[str, str] = {}


def _shell_hash() -> str:
    """Return a short content hash identifying the current shell-asset set.

    The signature is a stat sweep (relative path + size + mtime_ns of every
    file under WEBAPP_DIR, excluding data/); when it is unchanged we return the
    memoised digest, otherwise we hash the signature afresh. Falls back to a
    fixed but valid token if the tree cannot be walked, so the worker always
    gets a usable cache name."""
    try:
        data_dir = DASHBOARD_DATA_DIR.resolve()
        entries = []
        for p in sorted(WEBAPP_DIR.rglob("*")):
            if not p.is_file():
                continue
            try:
                if p.resolve() == data_dir or data_dir in p.resolve().parents:
                    continue
            except OSError:
                continue
            st = p.stat()
            entries.append(f"{p.relative_to(WEBAPP_DIR)}:{st.st_size}:{st.st_mtime_ns}")
    except OSError:
        return "static"
    signature = "\n".join(entries)
    cached = _SHELL_HASH_CACHE.get(signature)
    if cached is not None:
        return cached
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
    # Single-signature cache: replace wholesale so a changed tree cannot leak
    # unbounded entries over the process lifetime.
    _SHELL_HASH_CACHE.clear()
    _SHELL_HASH_CACHE[signature] = digest
    return digest
# Read-only SPARQL endpoint of the "life" triple store. The projects card
# (GET /projects) computes its content live from this, so there is no static
# projects.json and no extractor job: project/goal frontmatter is already
# indexed as triples by the qlever-dir Markdown converter, and the card is just
# a query result over it.
QLEVER_LIFE_URL = os.environ.get("QLEVER_LIFE_URL", "http://qlever-life:7001").rstrip("/")
QLEVER_TIMEOUT = float(os.environ.get("QLEVER_TIMEOUT", "8"))
# qlever-dir synthesizes each file's named graph as <BASE_URI + path relative
# to the chambers root> (BASE_URI is "file:" in docker-compose.yml). Inverting
# that mapping is how a project URI resolves back to its editable source file.
QLEVER_GRAPH_BASE = os.environ.get("QLEVER_GRAPH_BASE", "file:")
# Cap for a project file written through the dashboard editor.
MAX_PROJECT_FILE_BYTES = int(os.environ.get("MAX_PROJECT_FILE_BYTES", str(512 * 1024)))
_STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml; charset=utf-8",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
# Conversations are keyed by requester identity. Requests that carry no
# "on-behalf-of" identity share this default key.
DEFAULT_SESSION_KEY = "Web"
# Upper bound on concurrent `claude` subprocesses across all sessions, to keep
# CPU/memory and subprocess count sane on a personal box. Different users run in
# parallel up to this limit; the same user is always serialized.
MAX_CONCURRENCY = max(1, int(os.environ.get("WEB_GATEWAY_MAX_CONCURRENCY", "2")))

# Internal e-mail backend: agents run email_client.py with no mailbox
# credentials and EMAIL_BACKEND_URL pointed here, so the gateway (which keeps
# the credentials in its own environment) is the only process that can reach
# SMTP/IMAP. The shared token gates the endpoint; when unset the endpoint is
# disabled and agents fall back to using credentials directly.
EMAIL_BACKEND_TOKEN = os.environ.get("EMAIL_BACKEND_TOKEN", "")
EMAIL_CLIENT_PATH = ec.__file__

# Messenger channel gateways (Signal, WhatsApp, …) — each is a sibling service
# exposing the same pending-send approval API (/pending-sends) plus /health and
# /qr. When a channel's base URL (*_GATEWAY_BASE_URL) is set, /sends aggregates
# its pending sends from that API and proxies /sends/<channel>/{id}/approve|reject
# actions to it; /gateways shows its live link state. The channel slug is the
# `account` segment in the /sends/<account>/<id> URLs.
#
# Registry of configured channel gateways, keyed by the slug used in /sends and
# /gateways URLs — the Docker service hostname of each gateway's base URL
# (`signal-gateway`, `signal-gateway-personal`, …), the same name the gateway
# derives from the Host header when it emits an approval link, so the two agree
# with no slug configuration. The three built-ins are enrolled when their base
# URL is set; a deployment adds any further gateways (extra accounts, extra
# channels — e.g. a second Signal identity like `signal-gateway-personal`) via
# MESSENGER_GATEWAYS, a JSON array of {base_url, token?, label?} objects.
# Deployment-declared extras win on slug collision. The discovery is shared
# with the gateway-monitor (scripts/messenger_gateways.py) so /sends, /gateways
# and the connection monitoring all see exactly the same set of gateways.
# Slug lookups go through _channel_gateway() so legacy shortened slugs
# ("signal", "signal-personal") in pre-upgrade links still resolve.
_CHANNEL_GATEWAYS = messenger_gateways.channel_gateways("[web-gateway]")


def _channel_gateway(slug: str):
    """Resolve a URL slug to (canonical_slug, gateway) or (None, None)."""
    return messenger_gateways.resolve(_CHANNEL_GATEWAYS, slug)

# Edge authentication (Traefik forward-auth). The public `agents` router is
# guarded by a forwardAuth middleware that calls GET /auth here. We accept a TLS
# client certificate (verified by Traefik against our client CA and forwarded via
# passTLSClientCert) OR — as a fallback — HTTP basic auth against the existing
# htpasswd users. Internal container-to-container calls never hit Traefik and so
# are never gated by this. See scripts/gateway_auth.py for the decision logic.
AUTH_CONFIG = gateway_auth.config_from_env()

# Concurrency model:
# - `_session_locks` holds one lock per session key, so a single conversation is
#   serialized while different conversations proceed in parallel.
# - `_worker_pool` bounds the total number of concurrent `claude` subprocesses.
# - `_state_lock` guards read-modify-write access to the shared STATE_FILE.
# - `_conversation_lock` guards the append to the per-day conversation log.
_session_locks: dict[str, threading.Lock] = {}
_session_locks_guard = threading.Lock()
_worker_pool = threading.BoundedSemaphore(MAX_CONCURRENCY)
_state_lock = threading.Lock()
_conversation_lock = threading.Lock()
# Guards read-modify-write of the per-thread conversation-tab files.
_conversations_lock = threading.Lock()
# html=False escapes raw HTML in answers. MarkdownIt() with no argument selects
# the "commonmark" preset, which sets html=True -- so a bare tag in an answer
# (e.g. the literal text "<title>") was emitted into the page unescaped. A
# <title> or other RCDATA/RAWTEXT element with no closing tag then swallows the
# rest of the document. Answers are model output that can quote untrusted text
# (e-mail, messages), so this is also the XSS boundary for the log pages.
_md = MarkdownIt("commonmark", {"html": False}).enable("table")
_URL_RE = re.compile(r'https?://[^\s<]+')

# ── Async job store ───────────────────────────────────────────────────────────
# POST /message can request async handling; the request returns a job handle
# immediately and the client polls GET /jobs/{id}. The heavy `claude` call runs
# in a background worker thread, serialized per session key (so the same user's
# messages stay ordered) and bounded by the worker pool, so the HTTP server
# stays responsive to polls and other requests instead of blocking for minutes.
JOB_RETENTION_SECONDS = int(os.environ.get("JOB_RETENTION_SECONDS", "3600"))
_JOB_RE = re.compile(r"^/jobs/([0-9a-f]{32})/?$")
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _prune_jobs() -> None:
    cutoff = _now_ts() - JOB_RETENTION_SECONDS
    with _jobs_lock:
        stale = [
            jid for jid, job in _jobs.items()
            if job["status"] != "pending" and job.get("finished", 0) < cutoff
        ]
        for jid in stale:
            _jobs.pop(jid, None)


def _create_job() -> str:
    _prune_jobs()
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "pending", "created": _now_ts()}
    return job_id


def _get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job is not None else None


def _finish_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)
            job["finished"] = _now_ts()


def _run_job(job_id: str, message: str, display_question: str | None,
             session_key: str) -> None:
    try:
        result = send_message(message, display_question=display_question,
                              session_key=session_key)
    except Exception as exc:  # noqa: BLE001 - report any failure to the poller
        _finish_job(job_id, status="error", error=str(exc))
        return
    if "error" in result:
        _finish_job(job_id, status="error", result=result)
    else:
        _finish_job(job_id, status="done", result=result)



def _render_answer(raw: str) -> str:
    """Render Markdown to HTML; auto-link bare http/https URLs as <a href=URL>URL</a>."""
    rendered = _md.render(raw)
    # Split on existing <a> tags so we never double-link markdown-rendered links.
    parts = re.split(r'(<a\b[^>]*>.*?</a>)', rendered, flags=re.DOTALL)

    def _linkify(text: str) -> str:
        def _sub(m: re.Match) -> str:
            url = m.group(0).rstrip('.,;:!?)')
            return f'<a href="{url}">{url}</a>'
        return _URL_RE.sub(_sub, text)

    return ''.join(_linkify(p) if i % 2 == 0 else p for i, p in enumerate(parts))


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _load_state() -> dict:
    """Load the session-key -> entry map, migrating the legacy single-session format."""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Legacy format: a single flat entry, i.e. a top-level dict with
    # "session_id"/"last_activity" keys (no per-key nesting). Migrate it to the
    # default session key so existing sessions keep resuming.
    if "session_id" in data or "last_activity" in data:
        return {DEFAULT_SESSION_KEY: data}
    return data


def _save_state(state: dict) -> None:
    tmp_state_file = f"{STATE_FILE}.tmp"
    with open(tmp_state_file, "w") as f:
        json.dump(state, f)
    os.replace(tmp_state_file, STATE_FILE)


def _get_session_entry(session_key: str) -> dict:
    with _state_lock:
        return dict(_load_state().get(session_key, {}))


def _update_session_entry(session_key: str, entry: dict) -> None:
    with _state_lock:
        state = _load_state()
        state[session_key] = entry
        _save_state(state)


def _session_lock_for(session_key: str) -> threading.Lock:
    with _session_locks_guard:
        lock = _session_locks.get(session_key)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_key] = lock
        return lock


def _session_is_fresh(state: dict) -> bool:
    if not state.get("session_id") or not state.get("last_activity"):
        return False
    age = _now_ts() - state["last_activity"]
    return age < SESSION_MAX_IDLE_SECONDS


def _allowlist_paths() -> list[Path]:
    # A single explicit file wins; otherwise every chamber may contribute an
    # accepted-requesters.txt — all chambers are equal.
    if REQUESTER_ALLOWLIST_PATH:
        return [Path(REQUESTER_ALLOWLIST_PATH)]
    return sorted(CHAMBERS_DIR.glob("*/accepted-requesters.txt"))


def _load_requester_allowlist() -> set[str]:
    entries: set[str] = set()
    for path in _allowlist_paths():
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"[web-gateway] warning: could not read whitelist file {path}: {exc}", flush=True)
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for token in line.split(","):
                candidate = token.strip()
                if candidate:
                    entries.add(normalize_requester_identity(candidate))
    return entries


def _is_allowed_requester(identity: str) -> bool:
    entries = _load_requester_allowlist()
    if not entries:
        return False
    return normalize_requester_identity(identity) in entries


def _extract_on_behalf_of(payload: dict) -> str | None:
    candidate = payload.get("on-behalf-of")
    if candidate is None:
        return None
    value = normalize_requester_identity(str(candidate))
    return value or None


# ── Conversation log ──────────────────────────────────────────────────────────

_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day_file(date_str: str) -> Path:
    return CONVERSATION_DIR / f"{date_str}.json"


def _all_day_dates() -> list[str]:
    """Return all stored day dates sorted ascending."""
    dates = [
        p.stem for p in sorted(CONVERSATION_DIR.glob("*.json"))
        if _DATE_RE.match(p.stem)
    ]
    return dates


def _load_conversation(date_str: str) -> list[dict]:
    try:
        with open(_day_file(date_str)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_conversation(entries: list[dict], date_str: str) -> None:
    with open(_day_file(date_str), "w") as f:
        json.dump(entries, f, ensure_ascii=False)


def _append_entry(question: str, answer: str) -> tuple[str, str]:
    """Append one Q&A entry and return (date_str, anchor).

    Guarded by `_conversation_lock` so parallel sessions don't lose entries when
    appending to the same per-day file concurrently.
    """
    with _conversation_lock:
        date_str = _today()
        entries = _load_conversation(date_str)
        ts_ms = int(_now_ts() * 1000)
        anchor = f"entry-{ts_ms}"
        entries.append({
            "anchor": anchor,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "question": question,
            "answer": answer,
        })
        _save_conversation(entries, date_str)
        return date_str, anchor


# Shared head for all server-rendered pages (pending sends, approval pages, the
# session log). It mirrors the dashboard PWA's dark palette (webapp/styles.css)
# so moving between the dashboard and these pages feels like one application.
_HTML_HEAD = (
    "<!DOCTYPE html>\n"
    '<html lang="en">\n'
    "<head>\n"
    '<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
    '<meta name="color-scheme" content="dark">\n'
    '<meta name="theme-color" content="#0b0d12">\n'
    "<style>\n"
    "  :root{--bg:#0b0d12;--card:#151922;--card-2:#1c2230;--fg:#e7ebf2;--muted:#8b93a3;"
    "--accent:#6ea8fe;--high:#ff6b6b;--ok:#57c785;--line:rgba(231,235,242,.08)}\n"
    "  body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:800px;"
    "margin:0 auto;padding:calc(env(safe-area-inset-top,0px) + 1.25rem) 1rem "
    "calc(env(safe-area-inset-bottom,0px) + 2.5rem)}\n"
    "  h1{font-size:1.3rem;font-weight:650;letter-spacing:-.01em;margin:0 0 .25rem}\n"
    "  nav{font-size:.9rem;margin-bottom:1.5rem;color:var(--muted)}\n"
    "  nav a{color:var(--accent);text-decoration:none;margin-right:1rem}\n"
    "  nav a:hover{text-decoration:underline}\n"
    "  .meta{color:var(--muted)}\n"
    "  section{border-left:3px solid var(--accent);padding:.75rem 1rem;margin-bottom:1.5rem;"
    "background:var(--card);border-radius:0 12px 12px 0}\n"
    "  time{font-size:.8rem;color:var(--muted);display:block;margin-bottom:.5rem}\n"
    "  .question{font-weight:600;margin-bottom:.75rem;white-space:pre-wrap}\n"
    "  .answer{line-height:1.6}\n"
    "  .answer p{margin:.4rem 0}\n"
    "  .answer h1,.answer h2,.answer h3,.answer h4{margin:1rem 0 .25rem}\n"
    "  .answer h1{font-size:1.3rem}.answer h2{font-size:1.1rem}.answer h3{font-size:1rem}\n"
    "  .answer table{border-collapse:collapse;margin:.75rem 0;font-size:.9rem;width:100%}\n"
    "  .answer th,.answer td{border:1px solid var(--line);padding:.3rem .6rem;text-align:left;vertical-align:top}\n"
    "  .answer th{background:var(--card-2);font-weight:600}\n"
    "  .answer tr:nth-child(even) td{background:rgba(231,235,242,.03)}\n"
    "  .answer code{background:var(--card-2);padding:.1rem .3rem;border-radius:3px;font-size:.85em;font-family:monospace}\n"
    "  .answer pre{background:var(--card-2);padding:.75rem;border-radius:8px;overflow-x:auto;margin:.5rem 0}\n"
    "  .answer pre code{background:none;padding:0}\n"
    "  .answer ul,.answer ol{margin:.4rem 0;padding-left:1.5rem}\n"
    "  .answer li{margin:.2rem 0}\n"
    "  .answer a{color:var(--accent)}\n"
    "  .answer blockquote{border-left:3px solid var(--muted);margin:.5rem 0;padding:.25rem .75rem;"
    "color:var(--muted);font-style:italic}\n"
    "  .answer hr{border:none;border-top:1px solid var(--line);margin:1rem 0}\n"
    "  ul.days{list-style:none;padding:0}\n"
    "  ul.days li{background:var(--card);border:1px solid var(--line);border-radius:12px;"
    "padding:.6rem .8rem;margin:.5rem 0}\n"
    "  ul.days a{color:var(--accent);text-decoration:none;font-size:1.05rem}\n"
    "  ul.days a:hover{text-decoration:underline}\n"
    "  .msg-body{white-space:pre-wrap;background:var(--card);border:1px solid var(--line);"
    "border-radius:12px;padding:.9rem;line-height:1.5;font-family:inherit;font-size:1rem}\n"
    "  .actions{display:flex;gap:.75rem;margin-top:1rem;flex-wrap:wrap}\n"
    "  .btn{display:inline-block;border:0;padding:.7rem 1.4rem;border-radius:12px;font-size:1rem;"
    "font-weight:600;cursor:pointer;text-decoration:none;text-align:center}\n"
    "  .btn-allow{background:var(--ok);color:#0b0d12}\n"
    "  .btn-deny{background:var(--high);color:#0b0d12}\n"
    "  .btn-skip{background:transparent;color:var(--muted);border:1px solid var(--line)}\n"
    "</style>\n"
    "</head>\n"
)

# Every server-rendered page starts its nav with a link home: inside the
# installed PWA there is no URL bar, so without it a user sent to an approval
# URL has no way back to the dashboard.
_NAV_HOME = '<a href="/">⌂ Dashboard</a>'


def _render_day_html(entries: list[dict], date_str: str, all_dates: list[str]) -> str:
    items: list[str] = []
    for entry in entries:
        anchor = html.escape(entry.get("anchor", ""))
        ts = entry.get("timestamp", "")[:19].replace("T", " ") + " UTC"
        q = html.escape(entry.get("question", ""))
        a = _render_answer(entry.get("answer", ""))
        items.append(
            f'  <section id="{anchor}">\n'
            f'    <time>{ts}</time>\n'
            f'    <div class="question">{q}</div>\n'
            f'    <div class="answer">{a}</div>\n'
            f'  </section>'
        )
    body = "\n".join(items) if items else "  <p>No entries yet.</p>"

    # prev / next navigation
    nav_parts = [_NAV_HOME, '<a href="/conversation">\u2191 All days</a>']
    if date_str in all_dates:
        idx = all_dates.index(date_str)
        if idx > 0:
            nav_parts.append(f'<a href="/conversation/{all_dates[idx - 1]}">\u2190 {all_dates[idx - 1]}</a>')
        if idx < len(all_dates) - 1:
            nav_parts.append(f'<a href="/conversation/{all_dates[idx + 1]}">{all_dates[idx + 1]} \u2192</a>')
    nav = "<nav>" + "".join(nav_parts) + "</nav>\n"

    return (
        _HTML_HEAD
        + f"<title>Retinue Conversation — {html.escape(date_str)}</title>\n"
        + "<body>\n"
        + f"<h1>Retinue Conversation — {html.escape(date_str)}</h1>\n"
        + nav
        + body + "\n"
        + "</body>\n</html>\n"
    )


def _render_index_html(all_dates: list[str]) -> str:
    if all_dates:
        items = "".join(
            f'  <li><a href="/conversation/{html.escape(d)}">{html.escape(d)}</a></li>\n'
            for d in reversed(all_dates)
        )
        body = f"<ul class=\"days\">\n{items}</ul>"
    else:
        body = "<p>No entries yet.</p>"
    return (
        _HTML_HEAD
        + "<title>Retinue Conversation</title>\n"
        + "<body>\n"
        + "<h1>Retinue Conversation</h1>\n"
        + f"<nav>{_NAV_HOME}</nav>\n"
        + body + "\n"
        + "</body>\n</html>\n"
    )


# ── Conversation tabs ─────────────────────────────────────────────────────────
# A conversation tab is a standalone chat thread with Ara. The user opens one
# from the dashboard, or a retinue agent opens one (token-gated) when it needs a
# decision. Each thread is a JSON file under CONVERSATIONS_DIR and maps to its
# own Claude session (key "conv:<id>").

_CONV_ROLES = {"user", "assistant", "agent"}
# Max length of a derived thread title before it's truncated with an ellipsis.
_TITLE_MAX_LEN = 60


def _derive_title(text: str) -> str:
    """A short, single-line title derived from the first message of a thread."""
    line = " ".join((text or "").split())
    if len(line) > _TITLE_MAX_LEN:
        # Reserve room for the trailing one-character ellipsis ("\u2026").
        line = line[:_TITLE_MAX_LEN - 1].rstrip() + "\u2026"
    return line or "Conversation"


def _load_conv(cid: str) -> dict | None:
    # Re-validate here so the path guard dominates the open() in this scope.
    if not _CONV_ID_RE.fullmatch(cid):
        return None
    base = os.path.realpath(CONVERSATIONS_DIR)
    path = os.path.realpath(os.path.join(base, f"{cid}.json"))
    try:
        if os.path.commonpath([base, path]) != base:
            return None
    except ValueError:
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _save_conv(conv: dict) -> None:
    """Atomically write a conversation file."""
    cid = conv["id"]
    # Re-validate here so the path guard dominates the writes in this scope.
    if not _CONV_ID_RE.fullmatch(cid):
        raise ValueError("invalid conversation id")
    base = os.path.realpath(CONVERSATIONS_DIR)
    path = os.path.realpath(os.path.join(base, f"{cid}.json"))
    try:
        contained = os.path.commonpath([base, path]) == base
    except ValueError:
        contained = False
    if not contained:
        raise ValueError("conversation path escapes store")
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(conv, f, ensure_ascii=False)
    os.replace(tmp, path)


def _conv_summary(conv: dict) -> dict:
    messages = conv.get("messages", [])
    last = messages[-1] if messages else {}
    return {
        "id": conv.get("id"),
        "title": conv.get("title", ""),
        "initiator": conv.get("initiator", "user"),
        # "chat" is the default and what every pre-existing thread means; "edit"
        # marks quick edit commands issued from a project page, which the
        # dashboard hides from the normal conversation list.
        "kind": conv.get("kind") or "chat",
        "project": conv.get("project"),
        "project_title": conv.get("project_title"),
        # The thread's model choice (validated; empty string => gateway default),
        # so the picker can show the current selection without a second fetch.
        "model": _conv_model(conv) or "",
        "created": conv.get("created"),
        "updated": conv.get("updated"),
        "unread": bool(conv.get("unread")),
        "archived": bool(conv.get("archived")),
        # A muted thread stays where the user put it: an agent filing news into
        # it never un-archives it, and (once notification filtering exists) it
        # is the flag that silences it. Independent of `archived` — either can
        # be set alone.
        "muted": bool(conv.get("muted")),
        "pending": bool(conv.get("pending")),
        "pending_since": conv.get("pending_since"),
        "pending_status": conv.get("pending_status"),
        "message_count": len(messages),
        "last_preview": _derive_title(last.get("text", "")),
    }


def _list_convs(scope: str = "active", kind: str = "chat",
                project: str | None = None) -> list[dict]:
    """List thread summaries, newest first.

    `scope` selects which threads to include:
      - "active"   (default): only non-archived threads — what the dashboard card
        and the existing API consumers expect.
      - "archived": only archived threads — for the dedicated all-conversations
        view's archive filter.
      - "all":      every thread regardless of archive state.

    `kind` filters by thread kind:
      - "chat" (default): normal conversations only. Edit-command and cowork
        threads are deliberately absent from every default listing.
      - "edit": only project edit-command threads.
      - "cowork": only the audit threads written by the Ask-Ara MCP connector.
      - "all":  every kind.

    `project` (a project URI) restricts the list to threads linked to that
    project — what the project page shows as the project's own activity.
    """
    summaries: list[dict] = []
    for path in CONVERSATIONS_DIR.glob("*.json"):
        if not _CONV_ID_RE.match(path.stem):
            continue
        conv = _load_conv(path.stem)
        if conv is None:
            continue
        archived = bool(conv.get("archived"))
        if scope == "active" and archived:
            continue
        if scope == "archived" and not archived:
            continue
        conv_kind = conv.get("kind") or "chat"
        if kind != "all" and conv_kind != kind:
            continue
        if project and conv.get("project") != project:
            continue
        summaries.append(_conv_summary(conv))
    summaries.sort(key=lambda s: s.get("updated") or "", reverse=True)
    return summaries


def _store_attachments(cid: str, raw_atts) -> list[dict]:
    """Persist agent-provided attachments for thread ``cid`` and return metadata.

    Each input item is ``{"filename", "content_type", "data"(base64)}``. Files
    are written under ``CONVERSATION_ATTACHMENTS_DIR/<cid>/<att_id>`` with a
    server-generated id, so an untrusted filename never becomes a path
    component; the original name survives only as metadata (used for the
    download's Content-Disposition). Malformed or oversized items are skipped.
    Returns the metadata dicts (without the bytes) to embed in the message."""
    stored: list[dict] = []
    if not isinstance(raw_atts, list):
        return stored
    conv_dir = CONVERSATION_ATTACHMENTS_DIR / cid
    for item in raw_atts:
        if not isinstance(item, dict) or not isinstance(item.get("data"), str):
            continue
        try:
            blob = base64.b64decode(item["data"], validate=True)
        except (binascii.Error, ValueError):
            continue
        if not blob or len(blob) > MAX_ATTACHMENT_BYTES:
            continue
        att_id = uuid.uuid4().hex
        conv_dir.mkdir(parents=True, exist_ok=True)
        (conv_dir / att_id).write_bytes(blob)
        filename = os.path.basename(str(item.get("filename") or "attachment")) or "attachment"
        stored.append({
            "id": att_id,
            "filename": filename,
            "content_type": str(item.get("content_type") or "application/octet-stream"),
            "size": len(blob),
        })
    return stored


def _sweep_message_files() -> None:
    """Best-effort removal of stored message files older than the TTL."""
    cutoff = time.time() - MESSAGE_FILES_TTL_SECONDS
    try:
        for path in MESSAGE_FILES_DIR.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _store_message_files(raw_files) -> list[dict]:
    """Materialize files sent with POST /message and return their metadata.

    Each input item is ``{"filename", "content_type", "data"(base64)}`` — the
    same shape as conversation attachments. Files are written under
    MESSAGE_FILES_DIR with a server-generated name, so an untrusted filename
    never becomes a path component; only a plain short extension (derived from
    the filename, else the content type) survives, since a file extension is
    what lets the reading session type the file. Malformed or oversized items
    are skipped. Returns ``[{"path", "content_type", "size"}, ...]``."""
    stored: list[dict] = []
    if not isinstance(raw_files, list):
        return stored
    for item in raw_files:
        if not isinstance(item, dict) or not isinstance(item.get("data"), str):
            continue
        try:
            blob = base64.b64decode(item["data"], validate=True)
        except (binascii.Error, ValueError):
            continue
        if not blob or len(blob) > MAX_ATTACHMENT_BYTES:
            continue
        content_type = str(item.get("content_type") or "application/octet-stream")
        suffix = Path(os.path.basename(str(item.get("filename") or ""))).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
            suffix = mimetypes.guess_extension(content_type) or ""
        MESSAGE_FILES_DIR.mkdir(parents=True, exist_ok=True)
        _sweep_message_files()
        path = MESSAGE_FILES_DIR / f"{uuid.uuid4().hex}{suffix}"
        path.write_bytes(blob)
        stored.append({"path": str(path), "content_type": content_type, "size": len(blob)})
    return stored


def _message_files_note(stored: list[dict]) -> str:
    """A note listing the files a /message request carried, with their on-disk
    paths — the counterpart of _conv_attachment_note for gateway-forwarded
    files. The untrusted original filename is deliberately absent: the reading
    session needs only the server-named path, type and size."""
    if not stored:
        return ""
    lines = [
        f"- {f['path']} ({f['content_type']}, {f['size']} bytes)"
        for f in stored
    ]
    return ("\n\nThe message carries the following forwarded file(s); read them "
            "from disk if relevant (you run in the same container):\n"
            + "\n".join(lines))


# Content types the browser may render in place (``Content-Disposition: inline``)
# when a request asks for it. Deliberately narrow: anything the browser executes
# in our origin — text/html, image/svg+xml, XML — stays a download, so a file
# pushed into a thread can never become script running behind the dashboard's
# auth. Everything not listed here is served as an attachment regardless.
_INLINE_SAFE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
    "application/pdf", "text/plain",
})


def _content_disposition(filename: str, inline: bool = False) -> str:
    """Build a Content-Disposition header that survives non-ASCII filenames
    (RFC 6266: an ASCII fallback plus a UTF-8 ``filename*``)."""
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "'")
    quoted = urllib.parse.quote(filename, safe="")
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def _new_conv(initiator: str, owner: str, title: str | None,
              first_role: str, first_text: str,
              first_attachments=None, kind: str = "chat",
              project: str | None = None,
              project_title: str | None = None,
              model: str | None = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    cid = uuid.uuid4().hex
    first_msg = {"role": first_role, "text": first_text, "ts": now}
    atts = _store_attachments(cid, first_attachments or [])
    if atts:
        first_msg["attachments"] = atts
    conv = {
        "id": cid,
        "title": title or _derive_title(first_text),
        "created": now,
        "updated": now,
        "initiator": initiator,
        "owner": owner,
        # "chat" is a normal conversation; "edit" is a quick edit command from a
        # project page — marked so default listings can leave it out.
        "kind": kind,
        # An agent-initiated thread arrives unread (it needs the user's
        # attention); a user starting their own thread has already seen it.
        "unread": initiator == "agent",
        "pending": False,
        "messages": [first_msg],
    }
    if project:
        conv["project"] = project
        if project_title:
            conv["project_title"] = project_title
    # Persist a validated model choice on the thread; None (the default) is left
    # unset so existing threads keep behaving exactly as before.
    valid_model = _valid_model_id(model)
    if valid_model:
        conv["model"] = valid_model
    with _conversations_lock:
        _save_conv(conv)
    return conv


def _detect_lang(text: str) -> str | None:
    """Best-effort BCP-47 language tag for a reply, so the dashboard's speech
    synthesis reads it with a matching voice instead of the browser default.

    Language-agnostic by design: no language is privileged. Detection is handed
    to `langdetect` (~55 languages, all treated equally); we return whatever it
    reports (e.g. 'en', 'de', 'fr', 'it'), or None when there is too little
    signal or the detector is unavailable — in which case the message carries no
    `lang` and the client falls back to the browser default voice.
    """
    s = str(text or "")
    if len(s.strip()) < 12:  # too little signal to classify reliably
        return None
    try:
        from langdetect import detect  # type: ignore
        from langdetect import DetectorFactory
        DetectorFactory.seed = 0  # deterministic output
        code = detect(s)
    except Exception:
        return None
    return code or None


def _conv_add_message(cid: str, role: str, text: str, *,
                      unread: bool | None = None,
                      pending: bool | None = None,
                      attachments=None,
                      model_name: str | None = None,
                      cost_usd: float | None = None,
                      agent: str | None = None,
                      wake: bool = False) -> dict | None:
    """Append a message to a thread and update its flags. Returns the thread.

    `model_name`/`cost_usd` carry the answering turn's metadata (short model
    label and whole-turn list-price cost) so the dashboard can show it in the
    bubble header; both are byproducts of the answer call and cost nothing extra
    to surface. `agent` overrides the displayed sender name (e.g. "Coach") when
    a relay answers on a subagent's behalf.

    `wake` marks an append that carries something new for the user (an agent
    filing an inbound message into an existing thread). Such an append
    un-archives the thread unless it is muted: archived + unread is invisible —
    it drops out of the active list while claiming to want attention — so a
    thread that receives news has to come back or the news is lost. Muting is
    the explicit opt-out, set when the user asks for a thread to be archived for
    good. Ara's own reply and the user's own reply do not wake a thread: neither
    is news arriving from outside.
    """
    now = datetime.now(timezone.utc).isoformat()
    stored = _store_attachments(cid, attachments or [])
    with _conversations_lock:
        conv = _load_conv(cid)
        if conv is None:
            return None
        message = {"role": role, "text": text, "ts": now}
        lang = _detect_lang(text)
        if lang:
            message["lang"] = lang
        if stored:
            message["attachments"] = stored
        if model_name:
            message["model_name"] = model_name
        if isinstance(cost_usd, (int, float)):
            message["cost_usd"] = float(cost_usd)
        if agent:
            message["agent"] = agent
        conv.setdefault("messages", []).append(message)
        conv["updated"] = now
        if wake and conv.get("archived") and not conv.get("muted"):
            conv["archived"] = False
        if unread is not None:
            conv["unread"] = unread
        if pending is not None:
            conv["pending"] = pending
            if pending:
                conv.setdefault("pending_since", now)
            else:
                conv.pop("pending_since", None)
                conv.pop("pending_status", None)
                conv.pop("pending_error", None)
        _save_conv(conv)
        return conv


def _valid_model_id(model: str | None, refresh: bool = False) -> str | None:
    """Return a model id only if it's one the gateway offers, else None.

    None means "use the gateway default" — both the absence of a choice and an
    id no longer on the offered list collapse to it, so a stale/hostile value can
    never reach `claude --model`."""
    if model is None:
        return None
    model = str(model).strip()
    if model and _model_offered(model, refresh=refresh):
        return model
    return None


def _conv_model(conv: dict) -> str | None:
    """The validated model a thread should run on, or None for the default."""
    return _valid_model_id(conv.get("model"))


def _conv_set_flags(cid: str, **flags) -> dict | None:
    with _conversations_lock:
        conv = _load_conv(cid)
        if conv is None:
            return None
        if flags.get("pending") is True:
            now = datetime.now(timezone.utc).isoformat()
            flags.setdefault("pending_since", now)
            flags.setdefault("pending_status", "Ara is working on this")
            flags.pop("pending_error", None)
        elif flags.get("pending") is False:
            for key in ("pending_since", "pending_status", "pending_error"):
                flags.pop(key, None)
                conv.pop(key, None)
        conv.update(flags)
        _save_conv(conv)
        return conv


def _conv_attachment_note(conv: dict, msg: dict) -> str:
    """A note listing the files a message carries, with their on-disk paths.

    Ara runs in the same container, so pointing her at the stored path lets her
    actually open a file the user attached (a PDF, a CSV, …) rather than only
    knowing one exists."""
    atts = msg.get("attachments") or []
    if not atts:
        return ""
    cid = conv.get("id", "")
    lines = []
    for att in atts:
        path = CONVERSATION_ATTACHMENTS_DIR / cid / str(att.get("id", ""))
        lines.append(
            f"- {att.get('filename', 'attachment')} "
            f"({att.get('content_type', 'application/octet-stream')}, "
            f"{att.get('size', 0)} bytes) — saved at {path}"
        )
    return ("\n\nThe user attached the following file(s); read them from disk if "
            "relevant (you run in the same container):\n" + "\n".join(lines))


def _conv_project_note(conv: dict) -> str:
    """Context block for a thread linked to a project.

    Resolves the project's source file through the life store so Ara works on
    the real file rather than from memory. For "edit" threads it also carries
    the contract of the project page's quick-edit lane: apply the change to the
    file directly and answer with one short confirmation."""
    pid = conv.get("project")
    if not pid:
        return ""
    title = conv.get("project_title") or _humanize_slug(pid)
    lines = [f'This thread is about the project "{title}" ({pid}).']
    try:
        src = _resolve_project_source(pid)
    except Exception as exc:  # life store down — still give Ara the id
        print(f"[web-gateway] project source lookup failed for {pid}: {exc}", flush=True)
        src = None
    if src:
        lines.append(f"The project's source file is {src[1]} — read it for "
                     "current state before answering or acting.")
    if (conv.get("kind") or "chat") == "edit":
        lines.append(
            "This is a quick edit command issued from the project's dashboard "
            "page, not a discussion. Apply the requested change directly to the "
            "project file (frontmatter and/or body), keeping the file's existing "
            "conventions, and commit it per the branch policy for chamber data. "
            "Then reply with a single short sentence confirming what changed — "
            "no headings, no elaboration. If the command is ambiguous or would "
            "lose information, do not guess: reply with one short question "
            "instead."
        )
    return "\n\n[Context: " + "\n".join(lines) + "]"


def _conv_engage_prompt(conv: dict, fresh: bool) -> str:
    """Build the prompt for Ara's next turn in a thread.

    When the Claude session is still fresh we send just the latest user message
    (Claude already holds the context, including any project note sent on the
    first turn). Otherwise — a new or expired session, or an agent-initiated
    thread Ara has never seen — we replay the transcript so Ara has full
    context."""
    messages = conv.get("messages", [])
    latest_msg = messages[-1] if messages else {}
    latest = latest_msg.get("text", "")
    note = _conv_attachment_note(conv, latest_msg)
    if fresh:
        return (latest + note) or latest
    who = {"user": "User", "assistant": "You (Ara)", "agent": "Retinue agent"}
    transcript = "\n".join(
        f"{who.get(m.get('role'), m.get('role'))}: {m.get('text', '')}"
        for m in messages
    )
    return (
        "You are Ara, continuing a conversation tab in the Retinue dashboard. "
        "Here is the conversation so far:\n\n" + transcript + "\n\n"
        "Reply to the user's latest message in your own voice. If they approve a "
        "concrete action (e.g. updating the agenda, sending a reply, declining an "
        "invitation), carry it out with your tools and confirm what you did."
        + _conv_project_note(conv) + note
    )


# A conversation counts as stalled once it has seen no activity — no message
# and no read — for this long (#66's definition: inactive for more than 10 min).
_STALLED_AFTER_SECONDS = 600


def _conv_event_mode(conv: dict) -> str:
    """Classify the agent turn just appended to a thread for push filtering.

    Returns one of the event kinds `push_notify.notify` matches against each
    device's notification_mode:

      * "new"     — the first message of a thread the agent opened; the only
                    event a `new_only` subscriber wants.
      * "stalled" — a message landing after the thread was inactive for more
                    than _STALLED_AFTER_SECONDS.
      * "reply"   — any other turn of an active exchange; delivered only to
                    "all" subscribers.

    Called after the message was appended, so messages[-1] is the message being
    pushed; the previous message and the user's last read are the activity
    anchors. Anything unparseable degrades to "reply", the quietest class.
    """
    messages = conv.get("messages", [])
    if len(messages) <= 1:
        return "new" if conv.get("initiator") == "agent" else "reply"
    anchors = []
    for raw in (messages[-2].get("ts"), conv.get("read_at")):
        try:
            ts = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is not None:
            anchors.append(ts)
    if anchors:
        idle = (datetime.now(timezone.utc) - max(anchors)).total_seconds()
        if idle > _STALLED_AFTER_SECONDS:
            return "stalled"
    return "reply"


def _push_conv_notification(conv: dict, text: str) -> None:
    """Notify the user's devices that a thread needs their attention.

    Called for every agent→user turn that lands unread: a thread Ara opens, a
    file an agent appends, and Ara's own reply (which arrives after her session
    ends, long after the user may have closed the app). Which devices are
    notified depends on each device's stored preferences: the event kind
    (see _conv_event_mode) and — for archived threads, which notify by
    default — an explicit opt-out. Best effort — a push failure never affects
    the conversation itself."""
    if not push_notify.enabled():
        return
    cid = conv.get("id", "")
    title = conv.get("title") or "Retinue"
    body = " ".join(str(text or "").split())
    if len(body) > 160:
        body = body[:157].rstrip() + "…"
    push_notify.notify_async(title, body, url=f"/#conversation-{cid}", tag=cid,
                             mode=_conv_event_mode(conv),
                             archived=bool(conv.get("archived")))


def _conv_worker(cid: str, session_key: str) -> None:
    """Background worker: ask Ara for the next turn in a thread and store it."""
    try:
        _conv_set_flags(cid, pending=True, pending_status="Ara is running in the background")
        conv = _load_conv(cid)
        if conv is None:
            return
        messages = conv.get("messages", [])
        latest = messages[-1]["text"] if messages else ""
        fresh = _session_is_fresh(_get_session_entry(session_key))
        prompt = _conv_engage_prompt(conv, fresh)
        result = send_message(prompt, display_question=latest, session_key=session_key,
                              model=_conv_model(conv))
        if "error" in result:
            reply = ("Sorry, I couldn't reply just now "
                     f"({result['error']}). Please try again.")
        else:
            reply = result.get("response") or "(no reply)"
    except Exception as exc:  # noqa: BLE001 - always surface a turn back to the UI
        print(f"[web-gateway] conversation {cid} worker failed: {exc!r}", flush=True)
        reply = f"Sorry, an error occurred: {exc}"
        result = {}
    # Only a successful turn has cost/model metadata; an error reply carries none.
    conv = _conv_add_message(cid, "assistant", reply, unread=True, pending=False,
                             model_name=result.get("model_name"),
                             cost_usd=result.get("cost_usd"))
    if conv is not None:
        _push_conv_notification(conv, reply)


def _start_conv_turn(cid: str) -> None:
    """Mark a thread pending and spawn Ara's reply worker."""
    _conv_set_flags(cid, pending=True, pending_status="Ara is queued to reply")
    session_key = f"conv:{cid}"
    threading.Thread(
        target=_conv_worker,
        args=(cid, session_key),
        name=f"conv-{cid[:8]}",
        daemon=True,
    ).start()


# ── Send approval (sender-address send-control) ───────────────────────────────
# An additional view onto the IMAP Drafts folder: pending send requests created
# by email_client.py's verify/trust-fallback flow are non-deleted drafts keyed
# by their IMAP UID. This frontend lists them and drives
# approve (send the draft) / reject (delete it) per request.

_SEND_SINGLE_RE = re.compile(r"^/sends/([^/]+)/([^/]+?)/?$")
_SEND_ACTION_RE = re.compile(r"^/sends/([^/]+)/([^/]+)/(approve|reject)/?$")
_GATEWAY_HEALTH_RE = re.compile(r"^/gateways/([A-Za-z0-9._-]+)/health/?$")
_GATEWAY_QR_RE = re.compile(r"^/gateways/([A-Za-z0-9._-]+)/qr/?$")


def _ec_config(account: str):
    acc = None if account in (None, "", "default") else account
    return ec.Config(acc)


def _channel_pending_sends(channel: str, gw: dict) -> list[dict]:
    """Fetch pending sends from a channel gateway's /pending-sends API.

    Returns an empty list when the gateway is unreachable. Maps the gateway's
    fields to the display fields the email pending-send renderer uses (subject,
    to, category, request_id) and tags each entry with the channel slug.
    """
    url = f"{gw['base_url']}/pending-sends"
    headers = {}
    if gw.get("token"):
        headers["Authorization"] = "Bearer " + gw["token"]
    label = gw.get("label", channel.title())
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        items = []
        for entry in body.get("pending") or []:
            entry = dict(entry)
            entry.setdefault("account", channel)
            if "request_id" not in entry:
                entry["request_id"] = entry.get("id", "")
            if "to" not in entry:
                entry["to"] = entry.get("recipient", "")
            if "subject" not in entry:
                msg = (entry.get("message") or "").strip()
                entry["subject"] = (msg[:60] + "…") if len(msg) > 60 else msg or f"({label} message)"
            items.append(entry)
        return items
    except Exception as exc:
        print(f"[web-gateway] {label} pending scan failed: {exc}", flush=True)
        return []


def _all_pending() -> list[dict]:
    """Aggregate pending send requests across e-mail accounts and every channel."""
    items: list[dict] = []
    for account in ec.policy_accounts():
        label = account or "default"
        try:
            cfg = _ec_config(account)
            for entry in ec.list_pending_sends(cfg):
                entry = dict(entry)
                entry["account"] = label
                items.append(entry)
        except ec.EmailError as exc:
            print(f"[web-gateway] pending scan failed for {label}: {exc}", flush=True)
    for channel, gw in _CHANNEL_GATEWAYS.items():
        items.extend(_channel_pending_sends(channel, gw))
    return items


def _render_sends_index_html(pending: list[dict]) -> str:
    if pending:
        rows = []
        for p in pending:
            acc = html.escape(p.get("account", "default"))
            rid = html.escape(p.get("request_id", ""))
            subj = html.escape(p.get("subject") or "(no subject)")
            to = html.escape(p.get("to") or "")
            cat = html.escape(p.get("category") or "")
            rows.append(
                f'  <li><a href="/sends/{acc}/{rid}">{subj}</a>'
                f'<span class="meta"> — {to} · <em>{cat}</em></span></li>'
            )
        body = '<ul class="days">\n' + "\n".join(rows) + "\n</ul>"
    else:
        body = "<p>No pending send requests.</p>"
    return (
        _HTML_HEAD
        + "<title>Retinue — Pending Sends</title>\n"
        + "<body>\n"
        + "<h1>Pending Sends</h1>\n"
        + f'<nav>{_NAV_HOME}<a href="/conversation">Session log</a></nav>\n'
        + body + "\n"
        + "</body>\n</html>\n"
    )


def _render_send_single_html(detail: dict, account: str, next_url: str | None) -> str:
    acc = html.escape(account)
    rid = html.escape(detail.get("request_id", ""))
    subj = html.escape(detail.get("subject") or "(no subject)")
    # Which identity sends this mail (e.g. "Your Name <you@…>" vs Ari's mailbox).
    # It matters whether a message goes out as the user's business address or as
    # an agent persona, so surface it prominently at the top of the approval card.
    frm = html.escape(detail.get("from") or account)
    to = html.escape(detail.get("to") or "")
    cc = html.escape(detail.get("cc") or "")
    bcc = html.escape(detail.get("bcc") or "")
    cat = html.escape(detail.get("category") or "")
    body = html.escape(detail.get("body") or "")
    attachments = detail.get("attachments") or []
    att = ", ".join(html.escape(a or "") for a in attachments) if attachments else "—"
    skip = html.escape(next_url) if next_url else "/sends"
    meta_rows = [
        f"<tr><th>From</th><td>{frm} <span class=\"meta\">({acc})</span></td></tr>",
        f"<tr><th>To</th><td>{to}</td></tr>",
    ]
    if cc:
        meta_rows.append(f"<tr><th>Cc</th><td>{cc}</td></tr>")
    if bcc:
        meta_rows.append(f"<tr><th>Bcc</th><td>{bcc}</td></tr>")
    meta_rows.append(f"<tr><th>Subject</th><td>{subj}</td></tr>")
    meta_rows.append(f"<tr><th>Category</th><td>{cat}</td></tr>")
    meta_rows.append(f"<tr><th>Attachments</th><td>{att}</td></tr>")
    return (
        _HTML_HEAD
        + f"<title>Retinue — Approve Send {rid}</title>\n"
        + "<body>\n"
        + "<h1>Approve Send</h1>\n"
        + f'<nav>{_NAV_HOME}<a href="/sends">\u2191 All pending sends</a></nav>\n'
        + '<table class="answer">\n' + "\n".join(meta_rows) + "\n</table>\n"
        + f'<pre class="msg-body">{body}</pre>\n'
        + '<div class="actions">\n'
        + f'  <form method="post" action="/sends/{acc}/{rid}/approve" id="form-approve">'
          f'<button type="submit" id="btn-approve" class="btn btn-allow">Allow</button></form>\n'
        + f'  <form method="post" action="/sends/{acc}/{rid}/reject" id="form-reject">'
          f'<button type="submit" id="btn-reject" class="btn btn-deny">Deny</button></form>\n'
        + f'  <a href="{skip}" id="btn-skip" class="btn btn-skip">Skip</a>\n'
        + "</div>\n"
        + "<script>\n"
          "(function(){\n"
          "  function lockButtons(activeLabel){\n"
          "    ['btn-approve','btn-reject'].forEach(function(id){\n"
          "      var btn=document.getElementById(id);\n"
          "      if(!btn)return;\n"
          "      btn.disabled=true;\n"
          "      btn.style.opacity='0.5';\n"
          "      btn.style.cursor='not-allowed';\n"
          "    });\n"
          "    var skip=document.getElementById('btn-skip');\n"
          "    if(skip){skip.style.pointerEvents='none';skip.style.opacity='0.5';}\n"
          "    var active=document.getElementById(activeLabel);\n"
          "    if(active){active.textContent='Processing...';}\n"
          "  }\n"
          "  var fa=document.getElementById('form-approve');\n"
          "  if(fa){fa.addEventListener('submit',function(){lockButtons('btn-approve');});}\n"
          "  var fr=document.getElementById('form-reject');\n"
          "  if(fr){fr.addEventListener('submit',function(){lockButtons('btn-reject');});}\n"
          "})();\n"
          "</script>\n"
        + "</body>\n</html>\n"
    )


def _render_channel_send_html(detail: dict, channel: str, request_id: str, next_url: str | None) -> str:
    """Render the page for a channel (Signal/WhatsApp/Telegram) pending send.

    A "pending" entry gets the Allow/Deny approval UI. Any other status renders
    as a status page instead: gateways execute an approved send asynchronously
    (issue #116), so right after approval the entry is "sending" — the page
    auto-refreshes until the gateway records the terminal status, and an
    "error" entry shows the gateway's real error string (e.g. a usync timeout)
    rather than a generic failure.
    """
    label = _CHANNEL_GATEWAYS.get(channel, {}).get("label", channel.title())
    rid = html.escape(request_id)
    chan = html.escape(channel)
    label_e = html.escape(label)
    recipient = html.escape(detail.get("recipient") or detail.get("to") or "")
    cat = html.escape(detail.get("category") or "")
    msg = html.escape(detail.get("message") or "")
    skip = html.escape(next_url) if next_url else "/sends"
    meta_rows = [
        f"<tr><th>Channel</th><td>{label_e}</td></tr>",
        f"<tr><th>To</th><td>{recipient}</td></tr>",
        f"<tr><th>Category</th><td>{cat}</td></tr>",
    ]
    status = detail.get("status") or "pending"
    if status != "pending":
        if status == "sending":
            headline = "Sending…"
            note = ("The gateway accepted the send and is delivering it in the "
                    "background. This page refreshes until it completes.")
            extra_head = '<meta http-equiv="refresh" content="2">\n'
        elif status == "approved":
            headline = "Sent ✓"
            note = "The message was delivered to the channel."
            extra_head = ""
        elif status == "rejected":
            headline = "Rejected"
            note = "The message was discarded without sending."
            extra_head = ""
        else:  # "error"
            headline = "Send failed"
            note = ("The gateway could not deliver the message: "
                    + (detail.get("error") or "unknown error"))
            extra_head = ""
        return (
            _HTML_HEAD
            + f"<title>Retinue — {label_e} Send {rid}</title>\n"
            + extra_head
            + "<body>\n"
            + f"<h1>{label_e} send: {html.escape(headline)}</h1>\n"
            + f'<nav>{_NAV_HOME}<a href="/sends">↑ All pending sends</a></nav>\n'
            + '<table class="answer">\n' + "\n".join(meta_rows) + "\n</table>\n"
            + f'<pre class="msg-body">{msg}</pre>\n'
            + f'<p class="meta">{html.escape(note)}</p>\n'
            + '<div class="actions">\n'
            + '  <a href="/sends/next" class="btn btn-skip">Next pending send</a>\n'
            + "</div>\n"
            + "</body>\n</html>\n"
        )
    return (
        _HTML_HEAD
        + f"<title>Retinue — Approve {label_e} Send {rid}</title>\n"
        + "<body>\n"
        + f"<h1>Approve {label_e} Send</h1>\n"
        + f'<nav>{_NAV_HOME}<a href="/sends">\u2191 All pending sends</a></nav>\n'
        + '<table class="answer">\n' + "\n".join(meta_rows) + "\n</table>\n"
        + f'<pre class="msg-body">{msg}</pre>\n'
        + '<div class="actions">\n'
        + f'  <form method="post" action="/sends/{chan}/{rid}/approve" id="form-approve">'
          f'<button type="submit" id="btn-approve" class="btn btn-allow">Allow</button></form>\n'
        + f'  <form method="post" action="/sends/{chan}/{rid}/reject" id="form-reject">'
          f'<button type="submit" id="btn-reject" class="btn btn-deny">Deny</button></form>\n'
        + f'  <a href="{skip}" id="btn-skip" class="btn btn-skip">Skip</a>\n'
        + "</div>\n"
        + "<script>\n"
          "(function(){\n"
          "  function lockButtons(activeLabel){\n"
          "    ['btn-approve','btn-reject'].forEach(function(id){\n"
          "      var btn=document.getElementById(id);\n"
          "      if(!btn)return;\n"
          "      btn.disabled=true;\n"
          "      btn.style.opacity='0.5';\n"
          "      btn.style.cursor='not-allowed';\n"
          "    });\n"
          "    var skip=document.getElementById('btn-skip');\n"
          "    if(skip){skip.style.pointerEvents='none';skip.style.opacity='0.5';}\n"
          "    var active=document.getElementById(activeLabel);\n"
          "    if(active){active.textContent='Processing...';}\n"
          "  }\n"
          "  var fa=document.getElementById('form-approve');\n"
          "  if(fa){fa.addEventListener('submit',function(){lockButtons('btn-approve');});}\n"
          "  var fr=document.getElementById('form-reject');\n"
          "  if(fr){fr.addEventListener('submit',function(){lockButtons('btn-reject');});}\n"
          "})();\n"
          "</script>\n"
        + "</body>\n</html>\n"
    )


# ── Messenger gateway status & re-pairing (/gateways) ─────────────────────────
# The page the gateway-monitor's outage notifications point at: live link state
# of every configured channel gateway, and — for a disconnected one — the
# pairing QR code, proxied from the gateway service so the user can re-pair
# straight from the phone. The proxies add the per-gateway token server-side
# (the QR is a live pairing credential, token-gated on the gateway itself);
# the page sits behind the same edge auth as the rest of the dashboard.

GATEWAY_HEALTH_TIMEOUT = float(os.environ.get("GATEWAY_HEALTH_TIMEOUT", "") or "8")

# Where the phone's "scan QR" screen lives, per channel family. Keyed by the
# leading channel name in the slug so service-name slugs (signal-gateway,
# signal-gateway-personal, …) inherit their family's instructions.
_PAIRING_HINTS = {
    "signal": "On the phone: Signal → Settings → Linked devices → Link new device.",
    "whatsapp": "On the phone: WhatsApp → Settings → Linked devices → Link a device.",
    "telegram": "On the phone: Telegram → Settings → Devices → Link Desktop Device.",
}


def _gateway_request(gw: dict, path: str, timeout: float):
    headers = {}
    if gw.get("token"):
        headers["Authorization"] = "Bearer " + gw["token"]
    req = urllib.request.Request(f"{gw['base_url']}{path}", headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def _fetch_gateway_health(gw: dict) -> dict:
    """Fetch a gateway's /health; an unreachable gateway reports as down."""
    try:
        with _gateway_request(gw, "/health", GATEWAY_HEALTH_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if isinstance(body, dict):
                return body
            return {"connected": False, "error": "malformed health response"}
    except Exception as exc:  # noqa: BLE001 - unreachable is a health verdict, not a crash
        return {"connected": False, "reachable": False, "error": f"gateway unreachable: {exc}"}


def _pairing_hint(slug: str) -> str:
    for family, hint in _PAIRING_HINTS.items():
        if slug == family or slug.startswith(family + "-"):
            return hint
    return "Scan this code from the messenger app on the phone (linked-devices screen)."


def _render_gateways_html(statuses: list[dict]) -> str:
    """Render the /gateways page. `statuses` = [{slug, label, health}, ...]."""
    cards: list[str] = []
    any_qr = False
    for st in statuses:
        slug = st["slug"]
        label_e = html.escape(st["label"])
        h = st["health"]
        configured = h.get("configured", True)
        connected = bool(h.get("connected"))
        if not configured:
            badge = '<span class="gw-badge gw-off">not configured</span>'
        elif connected:
            badge = '<span class="gw-badge gw-up">connected</span>'
        else:
            badge = '<span class="gw-badge gw-down">disconnected</span>'
        rows = [f"<h2>{label_e} {badge}</h2>"]
        error = h.get("error")
        if error and configured and not connected:
            rows.append(f'<p class="meta">{html.escape(str(error))}</p>')
        # Offer the pairing QR only when the gateway says re-pairing is the
        # remedy. Not every outage is fixed by scanning: a WhatsApp IQ wedge or
        # a Telegram transport drop keeps the device linked — showing a QR
        # there (which the gateway cannot even produce) renders as a broken
        # image and sends the user chasing the wrong fix. Older gateways
        # without the field keep the previous behaviour (QR whenever down).
        needs_repair = h.get("needs_repair")
        if needs_repair is None:
            needs_repair = configured and not connected
        if configured and not connected and needs_repair:
            any_qr = True
            rows.append(
                '<div class="qr-wrap">'
                # Hidden until a refresh actually delivers an image: while the
                # gateway is still generating the code, /qr answers JSON and a
                # visible <img> would render as a broken-image icon.
                f'<img class="qr" alt="pairing QR for {label_e}" src="/gateways/{slug}/qr" '
                'style="display:none" '
                "onload=\"this.style.display=''\" "
                'onerror="this.style.display=\'none\'">'
                '<p class="qr-note meta">If no code shows yet, the gateway is still generating one — '
                'this page refreshes automatically.</p>'
                f"<p>{html.escape(_pairing_hint(slug))}</p>"
                "</div>"
            )
        elif configured and not connected:
            rows.append('<p class="meta">The device is still paired — no QR scan needed; '
                        'the gateway recovers on its own or reports the error above.</p>')
        elif connected:
            # A connected gateway can still be degraded: WhatsApp reports
            # recipient_lookup_ok: false while outbound sends to uncached
            # (first-contact) recipients fail their device-list lookup, even
            # though the link — and the own-JID probe — are fine (issue #120).
            if h.get("recipient_lookup_ok") is False:
                rl_err = h.get("recipient_lookup_error") or "device-list resolution is failing"
                rows.append('<p class="meta">⚠ Outbound sends to new (first-contact) recipients '
                            'are currently failing: ' + html.escape(str(rl_err)) + '</p>')
            age = h.get("last_ok_age")
            if isinstance(age, (int, float)):
                rows.append(f'<p class="meta">Last verified {int(age)}s ago.</p>')
        cards.append('<section class="gw-card">' + "\n".join(rows) + "</section>")
    if not cards:
        cards.append("<section><p>No messenger gateways are configured in this deployment.</p></section>")
    refresh_js = (
        "<script>\n"
        "setInterval(function(){\n"
        "  document.querySelectorAll('img.qr').forEach(function(img){\n"
        "    var u=new URL(img.getAttribute('src'),location);\n"
        "    u.searchParams.set('ts',Date.now());\n"
        "    img.src=u.toString();\n"
        "  });\n"
        "}, 20000);\n"
        "setTimeout(function(){location.reload();}, 60000);\n"
        "</script>\n"
    ) if any_qr else '<meta http-equiv="refresh" content="60">\n'
    return (
        _HTML_HEAD
        + "<title>Retinue — Messenger Gateways</title>\n"
        + "<style>\n"
          "  .gw-badge{font-size:.75rem;font-weight:600;border-radius:999px;padding:.15rem .6rem;"
          "vertical-align:middle;margin-left:.4rem}\n"
          "  .gw-up{background:var(--ok);color:#0b0d12}\n"
          "  .gw-down{background:var(--high);color:#0b0d12}\n"
          "  .gw-off{background:var(--card-2);color:var(--muted)}\n"
          "  .gw-card h2{font-size:1.05rem;margin:.1rem 0 .4rem}\n"
          "  .qr-wrap{margin-top:.6rem}\n"
          "  .qr-wrap img.qr{max-width:min(320px,100%);border-radius:8px;background:#fff;display:block}\n"
        "</style>\n"
        + "<body>\n"
        + "<h1>Messenger gateways</h1>\n"
        + f"<nav>{_NAV_HOME}</nav>\n"
        + '<p class="meta">Connection state of each messaging channel. A disconnected gateway shows '
          "its pairing QR code here — scan it from the phone to re-link.</p>\n"
        + "\n".join(cards) + "\n"
        + refresh_js
        + "</body>\n</html>\n"
    )


# ── Message dispatch ──────────────────────────────────────────────────────────

def _short_model_name(canonical: str) -> str:
    """A one-word label for a model id, for the message header.

    Language-agnostic and provider-agnostic: pick the recognisable family word
    out of a canonical id like "claude-sonnet-5" or "claude-haiku-4-5" →
    "Sonnet"/"Haiku". Falls back to a cleaned-up form of whatever is there, so
    an unfamiliar model still shows *something* rather than nothing.
    """
    cid = str(canonical or "").lower()
    for fam in ("opus", "sonnet", "haiku", "fable"):
        if fam in cid:
            return fam.capitalize()
    # Unknown id: drop a leading vendor token and title-case the rest.
    tail = cid.split("/")[-1]
    tail = re.sub(r"^claude[-_]?", "", tail)
    tail = re.sub(r"[-_]\d.*$", "", tail)  # trim trailing version segments
    return tail.capitalize() if tail else ""


def _envelope_model_name(data: dict) -> str | None:
    """Short model name for a turn, from the `claude -p` JSON envelope.

    The envelope carries no single top-level model, but `modelUsage` maps each
    model id used to its own `{costUSD, canonicalModel, …}`. A turn that
    dispatched a subagent therefore lists more than one model; we attribute the
    bubble to the model that did the most work by cost (falling back to output
    tokens, then to the first entry). Returns None when the breakdown is absent.
    """
    usage = data.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return None

    def _weight(entry: dict) -> float:
        if not isinstance(entry, dict):
            return 0.0
        cost = entry.get("costUSD")
        if isinstance(cost, (int, float)):
            return float(cost)
        out = entry.get("outputTokens")
        return float(out) if isinstance(out, (int, float)) else 0.0

    best_id, best_entry, best_w = None, None, -1.0
    for mid, entry in usage.items():
        w = _weight(entry)
        if w > best_w:
            best_id, best_entry, best_w = mid, entry, w
    canonical = ""
    if isinstance(best_entry, dict):
        canonical = best_entry.get("canonicalModel") or ""
    canonical = canonical or best_id or ""
    return _short_model_name(canonical) or None


def send_message(message: str, display_question: str | None = None,
                 session_key: str = DEFAULT_SESSION_KEY,
                 model: str | None = None) -> dict:
    """Send message to the session for `session_key` (resume or new) and return result.

    Serialized per session key so one conversation stays ordered, while different
    keys run in parallel up to the worker-pool bound.

    `model` overrides the model for this turn (a validated per-thread choice);
    when None the gateway's configured default (CLAUDE_MODEL) applies. Since each
    turn is a fresh `claude -p`, switching models between turns is free.
    """
    # Hold the per-session lock first (so the same key's messages stay ordered
    # and queued requests don't occupy a worker slot), then acquire a worker slot
    # to bound the number of concurrent `claude` subprocesses.
    with _session_lock_for(session_key):
        with _worker_pool:
            state = _get_session_entry(session_key)

            # Grant the session read access both to composer uploads and to
            # thread attachments. The latter (CONVERSATION_ATTACHMENTS_DIR, under
            # CONVERSATIONS_DIR) is where files pushed into a thread — including
            # the user's own — are stored; without it, opening such an attachment
            # hits a permission prompt while a composer upload works, which looks
            # like flaky behaviour.
            cmd = [CLAUDE_BIN, "-p", "--output-format=json", "--permission-mode", CLAUDE_PERMISSION_MODE,
                   "--add-dir", "/root/.claude/uploads",
                   "--add-dir", str(CONVERSATION_ATTACHMENTS_DIR)]
            # A per-thread model choice (validated by the caller) wins over the
            # gateway default. An explicit empty string means "defer to default".
            effective_model = CLAUDE_MODEL if model is None else model
            if effective_model:
                cmd += ["--model", effective_model]

            if _session_is_fresh(state):
                cmd += ["--resume", state["session_id"]]
                session_action = "resumed"
            else:
                session_action = "new"

            # End option parsing with "--" so a user-supplied message that starts
            # with "-" is always treated as the prompt, never as a `claude` flag.
            cmd.extend(["--", message])

            result = _run_claude(
                cmd,
                capture_output=True,
                text=True,
                cwd="/workspace",
            )

            if result.returncode != 0:
                err_detail = result.stderr.strip()
                # Claude often writes the real failure (e.g. OAuth expiry)
                # to stdout as JSON even though it exits non-zero.
                try:
                    fail_data = json.loads(result.stdout)
                    if fail_data.get("result"):
                        err_detail = fail_data["result"]
                except json.JSONDecodeError:
                    pass
                if not err_detail:
                    err_detail = "claude exited non-zero"
                print(
                    f"[web-gateway] claude failed (rc={result.returncode}, "
                    f"action={session_action}): {err_detail}",
                    flush=True,
                )
                if not result.stderr.strip() and result.stdout.strip():
                    print(
                        f"[web-gateway] claude stdout was: {result.stdout[:2000]!r}",
                        flush=True,
                    )
                return {
                    "error": err_detail,
                    "session_action": session_action,
                }

            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                return {
                    "error": "could not parse claude output",
                    "raw": result.stdout[:500],
                    "session_action": session_action,
                }

            new_state = {
                "session_id": data.get("session_id", state.get("session_id")),
                "last_activity": _now_ts(),
            }
            _update_session_entry(session_key, new_state)

            response_text = data.get("result", "")
            out: dict = {
                "response": response_text,
                "session_id": new_state["session_id"],
                "session_action": session_action,
                # The whole-turn aggregate cost (already includes any subagent
                # sidechain the turn spawned). Attributed as-is to the single
                # bubble this turn produces — the acting agent — so the relay
                # overhead is folded into that agent's cost rather than shown
                # separately. It is a list-price estimate, not what the
                # subscription bills, so the UI marks it "~$".
                "cost_usd": data.get("total_cost_usd"),
                # Short model name for the header (e.g. "Sonnet"), derived from
                # the dominant-cost entry of the per-model usage breakdown.
                "model_name": _envelope_model_name(data),
            }

            if response_text:
                shown_question = display_question or message
                date_str, anchor = _append_entry(shown_question, response_text)
                if CONVERSATION_BASE_URL:
                    out["entry_url"] = f"{CONVERSATION_BASE_URL}/conversation/{date_str}#{anchor}"

            return out


# ── Transcript cleanup ────────────────────────────────────────────────────────

# Literal objects of any *name predicate in a chamber's contacts graph — the
# people the user is likely to dictate about, and the words Whisper most often
# mangles. Cached against the source files' mtimes.
_NAME_LITERAL_RE = re.compile(r'[Nn]ame\s+"([^"\n]{2,80})"')
_contact_names_cache: tuple[float, list[str]] | None = None
_contact_names_lock = threading.Lock()


def _contact_names(limit: int = 200) -> list[str]:
    global _contact_names_cache
    try:
        sources = sorted(CHAMBERS_DIR.glob("*/contacts/*.ttl"))
        stamp = sum(p.stat().st_mtime for p in sources)
    except OSError:
        return []
    with _contact_names_lock:
        if _contact_names_cache and _contact_names_cache[0] == stamp:
            return _contact_names_cache[1]
        names: list[str] = []
        seen: set[str] = set()
        for path in sources:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for name in _NAME_LITERAL_RE.findall(text):
                key = name.casefold()
                if key not in seen:
                    seen.add(key)
                    names.append(name)
        names = names[:limit]
        _contact_names_cache = (stamp, names)
        return names


_CLEANUP_SYSTEM_PROMPT = (
    "You repair speech-recognition transcripts. The user dictated a message; a "
    "speech-to-text model produced the transcript below, and it contains "
    "recognition errors — wrong or invented words, mangled names, missing "
    "punctuation.\n\n"
    "Return the message the user meant to dictate: fix misrecognised words and "
    "names, add sentence punctuation and capitalisation. Keep the user's own "
    "wording, language and register — do not translate, summarise, rephrase, "
    "shorten, answer, or comment. If a passage is beyond repair, leave it as it "
    "is rather than inventing content.\n\n"
    "Output only the corrected message text, nothing else."
)


def _cleanup_context(thread_id: str) -> str:
    """The tail of the thread, as context for what the dictation is about."""
    conv = _load_conv(thread_id) if thread_id else None
    if not conv:
        return ""
    lines = []
    for msg in (conv.get("messages") or [])[-TRANSCRIPT_CLEANUP_CONTEXT_MESSAGES:]:
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        who = "User" if msg.get("role") == "user" else "Ara"
        lines.append(f"{who}: {text[:600]}")
    return "\n".join(lines)


def _cleanup_transcript(raw: str, thread_id: str = "") -> str:
    """Repair a raw transcript with a small model. Returns `raw` on any failure."""
    if not TRANSCRIPT_CLEANUP or not raw.strip():
        return raw
    parts = []
    names = _contact_names()
    if names:
        parts.append("Names the user may have dictated (use the exact spelling):\n"
                     + ", ".join(names))
    context = _cleanup_context(thread_id)
    if context:
        parts.append("The conversation so far, for context:\n" + context)
    parts.append("Raw transcript to repair:\n" + raw)
    prompt = "\n\n".join(parts)

    cmd = [
        "claude", "-p", "--output-format=json",
        "--model", TRANSCRIPT_CLEANUP_MODEL,
        # A correction pass needs no tools, no MCP servers and no project
        # context — excluding them is what keeps it cheap and fast.
        "--allowed-tools", "",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--exclude-dynamic-system-prompt-sections",
        "--system-prompt", _CLEANUP_SYSTEM_PROMPT,
        "--", prompt,
    ]
    try:
        with _worker_pool:
            result = _run_claude(
                cmd, capture_output=True, text=True,
                timeout=TRANSCRIPT_CLEANUP_TIMEOUT,
                cwd=tempfile.gettempdir(),  # away from /workspace, so no CLAUDE.md is loaded
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[web-gateway] transcript cleanup failed: {exc}", flush=True)
        return raw
    if result.returncode != 0:
        print(f"[web-gateway] transcript cleanup exited {result.returncode}", flush=True)
        return raw
    try:
        cleaned = (json.loads(result.stdout).get("result") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        return raw
    # A model that starts answering rather than correcting produces something far
    # longer than the transcript; in that case the raw text is the safer answer.
    if not cleaned or len(cleaned) > max(80, len(raw) * TRANSCRIPT_CLEANUP_MAX_GROWTH):
        return raw
    return cleaned


# ── Projects (live SPARQL over the life store) ────────────────────────────────

# The retinue knowledge-base namespace the qlever-dir Markdown converter emits
# for project/goal frontmatter (see the chambers' .qlever/md2ttl.py).
_KB = "https://w3id.org/retinue/kb#"
_RETO = "urn:retinue:actor:reto"

# One query returns every active project with the fields the card needs. Paused
# projects and non-active statuses are excluded so the dashboard shows only what
# is actually running. currentActor drives the split: reto == "your move",
# anyone else == "waiting on <them>".
_PROJECTS_SPARQL = """
PREFIX k: <%s>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT ?p ?title ?actor ?next ?since ?expected ?status WHERE {
  ?p rdf:type k:Project .
  OPTIONAL { ?p k:title ?title }
  OPTIONAL { ?p k:currentActor ?actor }
  OPTIONAL { ?p k:currentNextAction ?next }
  OPTIONAL { ?p k:waitingSince ?since }
  OPTIONAL { ?p k:expectedBy ?expected }
  OPTIONAL { ?p k:status ?status }
  OPTIONAL { ?p k:paused ?paused }
  FILTER (!BOUND(?paused) || ?paused = false)
  FILTER (!BOUND(?status) || ?status != "done")
} ORDER BY ?title
""" % _KB


def _humanize_slug(uri: str) -> str:
    """Turn a urn:retinue:...:some-slug (or bare slug) into a display label:
    'urn:retinue:actor:jane-doe' -> 'Jane Doe'. Used until actors
    carry an explicit label in the store."""
    tail = uri.rsplit(":", 1)[-1] if uri else ""
    # Project ids in the notes chamber carry a redundant 'project-' prefix.
    for pfx in ("project-", "goal-"):
        if tail.startswith(pfx):
            tail = tail[len(pfx):]
    return " ".join(w.capitalize() for w in tail.replace("_", "-").split("-") if w)


def _sparql_bindings(query: str) -> list[dict]:
    """POST a SPARQL query to the life store and return its result bindings.
    Raises on any transport/parse error so callers can surface an honest 502."""
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        QLEVER_LIFE_URL,
        data=data,
        headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=QLEVER_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("results", {}).get("bindings", [])


def _fetch_projects() -> dict:
    """Query the life store and shape the result into the card's JSON. Returns
    {"generated": iso, "mine": [...], "waiting": [...]} on success. Raises on any
    transport/parse error so the caller can surface an honest 502."""
    mine, waiting = [], []
    for b in _sparql_bindings(_PROJECTS_SPARQL):
        def val(key):
            cell = b.get(key)
            return cell.get("value") if cell else None
        pid = val("p") or ""
        actor = val("actor")
        item = {
            "id": pid,
            "title": val("title") or _humanize_slug(pid),
            "next": val("next"),
            "expected": val("expected"),
        }
        if actor == _RETO:
            mine.append(item)
        else:
            item["waitingOn"] = _humanize_slug(actor) if actor else None
            item["since"] = val("since")
            waiting.append(item)

    mine.sort(key=lambda i: i["title"].lower())
    waiting.sort(key=lambda i: i["title"].lower())
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mine": mine,
        "waiting": waiting,
    }


# ── Project detail: URI -> source file, read and write ───────────────────────
# The named graph a project's type triple lives in IS its source file (graph
# IRIs are QLEVER_GRAPH_BASE + the path relative to the chambers root), so the
# store itself maps a project id to the file the dashboard editor works on.
# The client only ever sends the project URI — never a path.

# A single absolute IRI, with the characters RDF forbids in IRIs excluded —
# which is also exactly what keeps an interpolated <id> from breaking out of
# a SPARQL IRI literal (no whitespace, no '>', no quotes, no backslash).
_PROJECT_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:[^\s<>\"{}|\\^`]+$")

_PROJECT_GRAPH_SPARQL = """
PREFIX k: <%s>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT ?g ?title WHERE {
  GRAPH ?g { <%%s> rdf:type k:Project }
  OPTIONAL { <%%s> k:title ?title }
} LIMIT 1
""" % _KB


def _resolve_project_source(pid: str) -> tuple[str, Path, str] | None:
    """Map a project URI to (relative path, absolute path, title).

    Returns None when the id is malformed, unknown to the store, its graph is
    not a chamber file, or the resolved path escapes CHAMBERS_DIR."""
    if not _PROJECT_URI_RE.fullmatch(pid or "") or len(pid) > 512:
        return None
    bindings = _sparql_bindings(_PROJECT_GRAPH_SPARQL % (pid, pid))
    if not bindings:
        return None
    graph = (bindings[0].get("g") or {}).get("value", "")
    title_cell = bindings[0].get("title")
    title = title_cell.get("value") if title_cell else _humanize_slug(pid)
    if not graph.startswith(QLEVER_GRAPH_BASE):
        return None
    rel = graph[len(QLEVER_GRAPH_BASE):].lstrip("/")
    base = CHAMBERS_DIR.resolve()
    full = (base / rel).resolve()
    if base != full and base not in full.parents:
        return None
    return rel, full, title


def _project_item_payload(pid: str) -> dict | None:
    """The GET /projects/item body: the project's raw Markdown plus enough
    metadata for optimistic-concurrency writes (sha256 of what was served)."""
    src = _resolve_project_source(pid)
    if src is None:
        return None
    rel, full, title = src
    try:
        raw = full.read_bytes()
    except OSError:
        return None
    return {
        "id": pid,
        "title": title,
        "path": rel,
        "markdown": raw.decode("utf-8", errors="replace"),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _commit_project_file(full: Path, rel: str) -> None:
    """Best-effort git commit + push of one edited chamber file.

    Chamber data paths carry standing permission for direct commits (branch
    policy Tiers 1-2; a dashboard edit is user-initiated by definition). The
    in-container `git` is the serializing wrapper (git-serialize.sh), so
    concurrent agent commits in the same chamber don't race. Failure is logged,
    never surfaced: the file on disk is already the new truth and the store
    rebuild picks it up regardless."""
    chamber = CHAMBERS_DIR / rel.split("/", 1)[0]
    inner = str(full.relative_to(chamber))
    try:
        subprocess.run(["git", "-C", str(chamber), "add", inner],
                       check=True, capture_output=True, timeout=60)
        diff = subprocess.run(["git", "-C", str(chamber), "diff", "--cached", "--quiet"],
                              capture_output=True, timeout=60)
        if diff.returncode == 0:
            return  # no-op edit: nothing staged, nothing to commit
        subprocess.run(["git", "-C", str(chamber), "commit",
                        "-m", f"chore(projects): edit {inner} via dashboard"],
                       check=True, capture_output=True, timeout=60)
        subprocess.run(["git", "-C", str(chamber), "push"],
                       check=True, capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        print(f"[web-gateway] project commit failed for {rel}: {exc}", flush=True)


def _write_project_file(pid: str, content: str, base_sha: str | None) -> tuple[int, dict]:
    """Overwrite a project's source file. Returns (http_status, body).

    `base_sha` is the sha256 the editor loaded; a mismatch means someone (or
    some agent) changed the file meanwhile — answer 409 with the current state
    so the client can merge instead of silently clobbering it."""
    src = _resolve_project_source(pid)
    if src is None:
        return 404, {"error": "unknown project"}
    rel, full, _title = src
    data = content.encode("utf-8")
    if len(data) > MAX_PROJECT_FILE_BYTES:
        return 413, {"error": "content too large"}
    try:
        current = full.read_bytes()
    except OSError:
        return 404, {"error": "project file unreadable"}
    current_sha = hashlib.sha256(current).hexdigest()
    if base_sha and base_sha != current_sha:
        return 409, {
            "error": "conflict",
            "sha256": current_sha,
            "markdown": current.decode("utf-8", errors="replace"),
        }
    tmp = full.with_suffix(full.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, full)
    threading.Thread(target=_commit_project_file, args=(full, rel),
                     name=f"project-commit-{rel.rsplit('/', 1)[-1]}",
                     daemon=True).start()
    return 200, {"ok": True, "sha256": hashlib.sha256(data).hexdigest()}


# ── News feed ────────────────────────────────────────────────────────────────
# The feed is ranked here, at read time, from one number per item (see
# scripts/news_store.py): importance × decay, sampled now. Nothing is
# pre-sorted and nothing is rewritten as time passes — an item fades because the
# clock moved, not because a job re-scored it. The store is a plain JSON file on
# the persistent volume, so this costs a file read.

# The Herald's memory is prose the user can also edit by hand in the dashboard;
# bound the write so a runaway client cannot fill the volume with it.
MAX_PREFERENCES_BYTES = 64 * 1024


def _news_payload(scope: str, limit: int | None) -> dict:
    items = news_store.ranked(scope=scope, limit=limit)
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": scope,
        "items": items,
    }


def _news_preferences_payload() -> dict:
    path = news_store.preferences_file()
    try:
        updated = datetime.fromtimestamp(path.stat().st_mtime,
                                         timezone.utc).isoformat(timespec="seconds")
    except OSError:
        updated = None
    return {"markdown": news_store.load_preferences(), "updated": updated}


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress default access log noise
        pass

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, status: int, body: str) -> None:
        """Send a dynamically rendered page.

        Every page that reaches here is generated per request and behind the
        gateway's auth: the conversation log (which grows with each message),
        the send-approval pages, and their error pages. None of them may be
        cached. Without this header they carry no expiry and no validator at
        all, which lets a browser or intermediary serve a stale conversation
        page whose permalink anchors do not exist yet — and lets an approval
        page be re-served from history after the request it approves is gone.
        The static shell (webapp/) is served elsewhere and stays cacheable.
        """
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_static_file(self, path: Path, base: Path, cache: str = "no-cache") -> bool:
        """Serve a file from within ``base``, guarding against path traversal.

        Returns True if a file was sent, False otherwise (caller emits 404)."""
        try:
            full = path.resolve()
            base = base.resolve()
            if full != base and base not in full.parents:
                return False
            if not full.is_file():
                return False
            data = full.read_bytes()
        except (OSError, ValueError):
            return False
        ctype = _STATIC_CONTENT_TYPES.get(full.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)
        return True

    def _serve_conversation_attachment(self, cid: str, att_id: str,
                                       inline: bool = False) -> None:
        """Stream a thread attachment, as a download or (``?inline=1``) for
        display in the browser's own viewer.

        Inline is what makes a file re-openable: an ``attachment`` disposition
        forces a fresh save on every tap, so reading the same invoice twice
        leaves ``invoice(1).pdf`` behind. It is honoured only for
        ``_INLINE_SAFE_TYPES``; anything else falls back to a download.

        Only files referenced by that thread's stored metadata are served, and
        the on-disk path is rebuilt from validated hex ids (never the client
        path), so this cannot be used to read arbitrary files. Access control is
        the dashboard's own (Traefik basic-auth / client cert) — the same gate
        that already protects every thread's contents."""
        if not (_CONV_ID_RE.fullmatch(cid) and _ATT_ID_RE.fullmatch(att_id)):
            self._send_json(404, {"error": "not found"})
            return
        conv = _load_conv(cid)
        meta = None
        if conv is not None:
            for msg in conv.get("messages", []):
                for att in msg.get("attachments") or []:
                    if att.get("id") == att_id:
                        meta = att
                        break
                if meta:
                    break
        if meta is None:
            self._send_json(404, {"error": "not found"})
            return
        base = os.path.realpath(CONVERSATION_ATTACHMENTS_DIR)
        path = os.path.realpath(os.path.join(base, cid, att_id))
        try:
            if os.path.commonpath([base, path]) != base or not os.path.isfile(path):
                self._send_json(404, {"error": "not found"})
                return
            data = Path(path).read_bytes()
        except (OSError, ValueError):
            self._send_json(404, {"error": "not found"})
            return
        ctype = meta.get("content_type") or "application/octet-stream"
        inline = inline and ctype.split(";", 1)[0].strip().lower() in _INLINE_SAFE_TYPES
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         _content_disposition(meta.get("filename") or "attachment", inline))
        # The declared type is caller-supplied metadata; forbid MIME sniffing so
        # a mislabelled file cannot be re-interpreted as HTML and rendered.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(data)

    def _maybe_serve_dashboard(self) -> bool:
        """Serve the dashboard PWA at the site root plus its static assets.

        Curated data lives under DASHBOARD_DATA_DIR and is served at /data/."""
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._serve_static_file(WEBAPP_DIR / "index.html", WEBAPP_DIR)
        if path == "/sw.js":
            return self._serve_service_worker()
        if path.startswith("/data/"):
            rel = path[len("/data/"):]
            return self._serve_static_file(DASHBOARD_DATA_DIR / rel, DASHBOARD_DATA_DIR, cache="no-store")
        rel = path.lstrip("/")
        if not rel:
            return False
        return self._serve_static_file(WEBAPP_DIR / rel, WEBAPP_DIR)

    def _serve_service_worker(self) -> bool:
        """Serve sw.js with its cache name stamped from a content hash of the
        whole shell tree.

        The service worker caches shell assets cache-first, so a changed asset
        only reaches the browser once the worker's own bytes change. Rather than
        rely on someone hand-bumping a version constant on every webapp edit
        (which is easy to forget), we substitute the `__SHELL_HASH__` token with
        a hash computed over every file under WEBAPP_DIR. Any shell change moves
        the hash, which moves the worker bytes, which is exactly what triggers
        the browser to install the new worker and drop the stale cache. The file
        (baked, read-only) is never mutated on disk — the substitution is done on
        the response only. Served no-cache so the worker itself is always
        revalidated."""
        sw = WEBAPP_DIR / "sw.js"
        try:
            text = sw.read_text(encoding="utf-8")
        except OSError:
            return False
        body = text.replace("__SHELL_HASH__", _shell_hash()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_POST(self):
        if self.path == "/message":
            self._handle_message()
            return
        if self.path in ("/internal/email", "/internal/email/"):
            self._handle_internal_email()
            return
        if self.path in ("/internal/conversations", "/internal/conversations/"):
            self._handle_agent_conversation()
            return
        internal_msg_match = _INTERNAL_CONV_MSG_RE.match(self.path)
        if internal_msg_match:
            self._handle_agent_conversation_message(internal_msg_match.group(1))
            return
        internal_flags_match = _INTERNAL_CONV_FLAGS_RE.match(self.path)
        if internal_flags_match:
            self._handle_agent_conversation_flags(internal_flags_match.group(1))
            return
        if self.path.split("?", 1)[0].rstrip("/") == "/push/subscribe":
            self._handle_push_subscribe()
            return
        if self.path.split("?", 1)[0].rstrip("/") == "/push/unsubscribe":
            self._handle_push_unsubscribe()
            return
        if self.path.split("?", 1)[0].rstrip("/") == "/conversations/transcribe":
            self._handle_transcribe()
            return
        if self.path.split("?", 1)[0].rstrip("/") == "/projects/item":
            self._handle_project_write()
            return
        if self.path.split("?", 1)[0].rstrip("/") == "/news/feedback":
            self._handle_news_feedback()
            return
        if self.path.split("?", 1)[0].rstrip("/") == "/news/preferences":
            self._handle_news_preferences_write()
            return
        if self.path in ("/conversations", "/conversations/"):
            self._handle_conversation_create()
            return
        msg_match = _CONV_MSG_RE.match(self.path)
        if msg_match:
            self._handle_conversation_reply(msg_match.group(1))
            return
        read_match = _CONV_READ_RE.match(self.path)
        if read_match:
            self._handle_conversation_read(read_match.group(1))
            return
        archive_match = _CONV_ARCHIVE_RE.match(self.path)
        if archive_match:
            self._handle_conversation_archive(archive_match.group(1), True)
            return
        unarchive_match = _CONV_UNARCHIVE_RE.match(self.path)
        if unarchive_match:
            self._handle_conversation_archive(unarchive_match.group(1), False)
            return
        model_match = _CONV_MODEL_RE.match(self.path)
        if model_match:
            self._handle_conversation_model(model_match.group(1))
            return
        action = _SEND_ACTION_RE.match(self.path)
        if action:
            self._handle_send_action(action.group(1), action.group(2), action.group(3))
            return
        self._send_json(404, {"error": "not found"})

    def _handle_internal_email(self) -> None:
        # Privileged e-mail backend for agents that hold no mailbox credentials.
        # Runs email_client.py with the gateway's own (credential-bearing) env.
        if not EMAIL_BACKEND_TOKEN:
            self._send_json(403, {"error": "email backend disabled"})
            return
        token = self.headers.get("X-Email-Backend-Token", "")
        if not hmac.compare_digest(token, EMAIL_BACKEND_TOKEN):
            self._send_json(403, {"error": "forbidden"})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
            argv = payload["argv"]
        except (json.JSONDecodeError, KeyError, TypeError):
            self._send_json(400, {"error": "invalid request"})
            return
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            self._send_json(400, {"error": "argv must be a list of strings"})
            return
        env = dict(os.environ)
        env.pop("EMAIL_BACKEND_URL", None)  # the backend must not re-proxy
        try:
            proc = subprocess.run(
                ["python3", EMAIL_CLIENT_PATH, *argv],
                capture_output=True, text=True, timeout=180, env=env,
            )
        except subprocess.TimeoutExpired:
            self._send_json(504, {"error": "email backend timed out"})
            return
        self._send_json(200, {
            "exit": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        })

    def _handle_send_action(self, account: str, request_id: str, verb: str) -> None:
        channel, _gw = _channel_gateway(account)
        if channel:
            self._handle_channel_send_action(channel, request_id, verb)
            return
        try:
            cfg = _ec_config(account)
            if verb == "approve":
                ec.approve_pending_send(cfg, request_id)
            else:
                ec.delete_pending_draft(cfg, request_id)
        except ec.EmailError as exc:
            self._send_html(400, _HTML_HEAD + "<body><h1>Send action failed</h1><p>"
                            + html.escape(str(exc)) + '</p><p><a href="/sends">Back</a></p>'
                            + "</body></html>")
            return
        # Move on to the next pending request for quick one-click processing.
        self._redirect("/sends/next")

    def _handle_channel_send_action(self, channel: str, request_id: str, verb: str) -> None:
        """Proxy approve/reject for a channel pending send to its gateway."""
        gw = _CHANNEL_GATEWAYS.get(channel)
        label = gw.get("label", channel.title()) if gw else channel.title()
        if not gw:
            self._send_html(503, _HTML_HEAD + f"<body><h1>{html.escape(label)} gateway not configured</h1>"
                            + '<p><a href="/sends">Back</a></p></body></html>')
            return
        url = f"{gw['base_url']}/pending-sends/{request_id}/{verb}"
        headers = {"Content-Length": "0"}
        if gw.get("token"):
            headers["Authorization"] = "Bearer " + gw["token"]
        try:
            req = urllib.request.Request(url, data=b"", headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30):
                pass
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            self._send_html(exc.code, _HTML_HEAD + f"<body><h1>{html.escape(label)} send action failed</h1><p>"
                            + html.escape(body[:300]) + '</p><p><a href="/sends">Back</a></p>'
                            + "</body></html>")
            return
        except Exception as exc:
            self._send_html(502, _HTML_HEAD + f"<body><h1>{html.escape(label)} gateway unreachable</h1><p>"
                            + html.escape(str(exc)) + '</p><p><a href="/sends">Back</a></p>'
                            + "</body></html>")
            return
        if verb == "approve":
            # Approval is asynchronous on the gateway (issue #116): it answers
            # "sending" immediately and executes in the background. Land on the
            # send's own page, which live-refreshes until the terminal status
            # (sent, or the gateway's real error string) — instead of jumping
            # straight to the next pending send with no outcome feedback.
            self._redirect(f"/sends/{channel}/{request_id}")
        else:
            self._redirect("/sends/next")

    def _handle_message(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")

        content_type = self.headers.get("Content-Type", "")
        on_behalf_of = None
        display_question = None
        if "application/json" in content_type:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON"})
                return
            message = (payload.get("message") or "").strip()
            on_behalf_of = _extract_on_behalf_of(payload)
            display_question = (payload.get("question") or "").strip() or None
            want_async = bool(payload.get("async"))
            raw_files = payload.get("files")
        else:
            message = raw.strip()
            want_async = False
            raw_files = None

        if not message:
            self._send_json(400, {"error": "empty message"})
            return

        # Files forwarded with the message (e.g. an image received on a
        # messenger channel) are materialized to disk here, in the container
        # the answering session runs in, and their paths are appended to the
        # prompt so the session can actually open them.
        if raw_files:
            message += _message_files_note(_store_message_files(raw_files))

        if on_behalf_of and not _is_allowed_requester(on_behalf_of):
            self._send_json(403, {
                "error": "forbidden",
                "response": REQUESTER_BLOCK_MESSAGE,
                "session_action": "blocked",
                "on_behalf_of": on_behalf_of,
                "allowed": False,
            })
            return

        # Key the conversation by requester identity so different users run in
        # parallel; anonymous requests share the default session key.
        session_key = on_behalf_of or DEFAULT_SESSION_KEY

        if want_async:
            job_id = _create_job()
            threading.Thread(
                target=_run_job,
                args=(job_id, message, display_question, session_key),
                name=f"job-{job_id[:8]}",
                daemon=True,
            ).start()
            self._send_json(202, {
                "status": "pending",
                "job_id": job_id,
                "job_url": f"/jobs/{job_id}",
            })
            return

        result = send_message(message, display_question=display_question,
                              session_key=session_key)
        status = 500 if "error" in result else 200
        self._send_json(status, result)

    # ── Conversation tabs ─────────────────────────────────────────────────────

    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _handle_news_feedback(self) -> None:
        """One user signal on the news feed: 👍/👎, opened, hidden, or a note.

        Two effects, both wanted. The item is nudged immediately, so the feed
        visibly reacts to the tap instead of waiting for the next curation run;
        and the signal is appended to the feedback log, which is what the Herald
        later generalizes into the preferences file. A note with no item id is
        the user speaking about the feed as a whole ("less crypto") — the most
        useful signal there is, so it needs no item to attach to."""
        payload = self._read_json_body()
        if payload is None:
            self._send_json(400, {"error": "invalid JSON"})
            return
        signal = (payload.get("signal") or "").strip()
        item_id = (payload.get("id") or "").strip() or None
        note = (payload.get("note") or "").strip()[:2000]
        if signal not in news_store.VALID_SIGNALS:
            self._send_json(400, {"error": "unknown signal",
                                  "valid": list(news_store.VALID_SIGNALS)})
            return
        if signal == "note" and not note:
            self._send_json(400, {"error": "a note needs text"})
            return
        if signal != "note" and not item_id:
            self._send_json(400, {"error": "id is required for this signal"})
            return
        try:
            entry = news_store.record_feedback(item_id, signal, note)
        except KeyError:
            self._send_json(404, {"error": "unknown item"})
            return
        except OSError as exc:
            self._send_json(500, {"error": "could not record feedback",
                                  "detail": str(exc)})
            return
        self._send_json(200, {"ok": True, "feedback": entry})

    def _handle_news_preferences_write(self) -> None:
        """Replace the Herald's memory with what the user typed.

        The profile is deliberately a plain Markdown file rather than hidden
        model state: the user can read why their feed looks the way it does, and
        correct it directly. The Herald is instructed to merge with what it
        finds here rather than clobber it."""
        payload = self._read_json_body()
        if payload is None:
            self._send_json(400, {"error": "invalid JSON"})
            return
        markdown = payload.get("markdown")
        if not isinstance(markdown, str):
            self._send_json(400, {"error": "markdown is required"})
            return
        if len(markdown.encode("utf-8")) > MAX_PREFERENCES_BYTES:
            self._send_json(413, {"error": "preferences too large"})
            return
        try:
            news_store.save_preferences(markdown)
        except OSError as exc:
            self._send_json(500, {"error": "could not save preferences",
                                  "detail": str(exc)})
            return
        self._send_json(200, _news_preferences_payload())

    def _handle_transcribe(self) -> None:
        """Voice input: proxy uploaded audio to the shared STT service.

        The browser POSTs the recorded audio as the raw request body (its
        MediaRecorder MIME type in Content-Type). We forward it verbatim to the
        STT service — which owns the Whisper model — then repair the transcript
        (see _cleanup_transcript) before returning it. The reply is
        {"text", "raw_text", "lang"}: `text` is what the dashboard puts in the
        composer, `raw_text` what Whisper actually heard. A `?thread=<id>` query
        param gives the cleanup pass the thread as context; `?cleanup=0` skips
        the pass. Access is the dashboard's own edge auth; the hop to the STT
        service carries the shared Bearer token."""
        if not STT_SERVICE_URL:
            self._send_json(503, {"error": "transcription not configured"})
            return
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send_json(400, {"error": "empty audio"})
            return
        if length > MAX_ATTACHMENT_BYTES:
            self._send_json(413, {"error": "audio too large"})
            return
        audio = self.rfile.read(length)
        ctype = self.headers.get("Content-Type") or "application/octet-stream"
        req = urllib.request.Request(
            STT_SERVICE_URL, data=audio, method="POST",
            headers={"Content-Type": ctype, "Content-Length": str(len(audio))},
        )
        if STT_TOKEN:
            req.add_header("Authorization", f"Bearer {STT_TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=TRANSCRIBE_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            print(f"[web-gateway] transcription upstream error {exc.code}", flush=True)
            self._send_json(502, {"error": "transcription failed"})
            return
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            print(f"[web-gateway] transcription unavailable: {exc}", flush=True)
            self._send_json(502, {"error": "transcription service unavailable"})
            return
        raw_text = (body.get("text") or "").strip() if isinstance(body, dict) else ""
        lang = body.get("lang") if isinstance(body, dict) else None

        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        thread_id = (params.get("thread") or [""])[0]
        if not _CONV_ID_RE.match(thread_id):
            thread_id = ""
        wants_cleanup = (params.get("cleanup") or ["1"])[0] != "0"
        text = _cleanup_transcript(raw_text, thread_id) if wants_cleanup else raw_text
        self._send_json(200, {"text": text, "raw_text": raw_text, "lang": lang})

    def _handle_project_write(self) -> None:
        """Save a project file edited on its dashboard page.

        Body: {"id": <project URI>, "content": <full markdown>,
               "base_sha": <sha256 the editor loaded, optional>}.
        The path is always re-resolved server-side from the id via the life
        store — the client can never name a file. base_sha makes the write
        optimistic-concurrency-safe: on mismatch the reply is 409 with the
        current content so the editor can offer a merge instead of clobbering
        a change made elsewhere (an agent, another device)."""
        payload = self._read_json_body()
        if payload is None:
            self._send_json(400, {"error": "invalid JSON"})
            return
        pid = (payload.get("id") or "").strip()
        content = payload.get("content")
        if not pid or not isinstance(content, str):
            self._send_json(400, {"error": "id and content are required"})
            return
        base_sha = (payload.get("base_sha") or "").strip() or None
        try:
            status, body = _write_project_file(pid, content, base_sha)
        except Exception as exc:  # life store down — honest 502, like /projects
            self._send_json(502, {"error": "life store unreachable",
                                  "detail": str(exc)})
            return
        self._send_json(status, body)

    def _handle_conversation_create(self) -> None:
        """User opens a new thread from the dashboard and Ara replies (async)."""
        payload = self._read_json_body()
        if payload is None:
            self._send_json(400, {"error": "invalid JSON"})
            return
        message = (payload.get("message") or "").strip()
        attachments = payload.get("attachments")
        # A message may consist of text, attachments, or both — but not nothing.
        if not message and not (isinstance(attachments, list) and attachments):
            self._send_json(400, {"error": "empty message"})
            return
        on_behalf_of = _extract_on_behalf_of(payload)
        if on_behalf_of and not _is_allowed_requester(on_behalf_of):
            self._send_json(403, {"error": "forbidden", "allowed": False})
            return
        owner = on_behalf_of or DEFAULT_SESSION_KEY
        title = (payload.get("title") or "").strip() or (None if message else "Attachment")
        kind = (payload.get("kind") or "chat").strip()
        if kind not in ("chat", "edit"):
            self._send_json(400, {"error": "invalid kind"})
            return
        project = (payload.get("project") or "").strip() or None
        if project and (len(project) > 512 or not _PROJECT_URI_RE.fullmatch(project)):
            self._send_json(400, {"error": "invalid project"})
            return
        # An edit command without a project has no file to apply itself to.
        if kind == "edit" and not project:
            self._send_json(400, {"error": "edit threads need a project"})
            return
        project_title = (str(payload.get("project_title") or "").strip() or None)
        if project_title:
            project_title = project_title[:120]
        # An invalid/absent model id is simply dropped (thread runs on the
        # gateway default) — never an error, so an old client keeps working.
        # refresh: the user just picked this id, so a cache miss warrants one
        # upstream re-check before dropping it.
        model = _valid_model_id(payload.get("model"), refresh=True)
        conv = _new_conv("user", owner, title, "user", message,
                         first_attachments=attachments, kind=kind,
                         project=project, project_title=project_title,
                         model=model)
        _start_conv_turn(conv["id"])
        self._send_json(201, _conv_set_flags(conv["id"], pending=True) or conv)

    def _handle_conversation_reply(self, cid: str) -> None:
        """User replies within an existing thread; Ara answers (async)."""
        payload = self._read_json_body()
        if payload is None:
            self._send_json(400, {"error": "invalid JSON"})
            return
        message = (payload.get("message") or "").strip()
        attachments = payload.get("attachments")
        if not message and not (isinstance(attachments, list) and attachments):
            self._send_json(400, {"error": "empty message"})
            return
        conv = _conv_add_message(cid, "user", message, unread=False,
                                 attachments=attachments)
        if conv is None:
            self._send_json(404, {"error": "not found"})
            return
        _start_conv_turn(cid)
        self._send_json(200, _load_conv(cid) or conv)

    def _handle_conversation_read(self, cid: str) -> None:
        conv = _conv_set_flags(cid, unread=False, read_at=datetime.now(timezone.utc).isoformat())
        if conv is None:
            self._send_json(404, {"error": "not found"})
            return
        self._send_json(200, conv)

    def _handle_conversation_model(self, cid: str) -> None:
        """Switch a thread's model mid-conversation; takes effect next turn.

        Body {"model": <id>}. An empty string (or an id not on the offered list)
        resets the thread to the gateway default. Because each turn spawns a
        fresh `claude -p`, no running state is disturbed — the change simply
        selects `--model` for the next turn."""
        payload = self._read_json_body()
        if payload is None:
            self._send_json(400, {"error": "invalid JSON"})
            return
        raw = str(payload.get("model") or "").strip()
        # "" is a legitimate reset to default; any other value must be offered.
        # refresh: the user just picked this id from the dropdown.
        if raw and not _model_offered(raw, refresh=True):
            self._send_json(400, {"error": "unknown model"})
            return
        conv = _conv_set_flags(cid, model=raw)
        if conv is None:
            self._send_json(404, {"error": "not found"})
            return
        self._send_json(200, _conv_summary(conv))

    def _handle_conversation_archive(self, cid: str, archived: bool) -> None:
        """Archive or unarchive a thread. Archived threads drop out of the
        dashboard card's active list but stay available in the dedicated
        all-conversations view (and via GET /conversations?archived=1).

        This is the dashboard's own button, i.e. the user archiving a thread
        by hand — it leaves `muted` untouched, so a thread archived this way
        comes back when something new is filed into it. Archiving *for good*
        is the muted variant, which an agent sets via /internal/… when the
        user asks for the thread to be archived and stay archived."""
        conv = _conv_set_flags(cid, archived=archived)
        if conv is None:
            self._send_json(404, {"error": "not found"})
            return
        self._send_json(200, _conv_summary(conv))

    def _handle_agent_conversation_flags(self, cid: str) -> None:
        """A retinue agent sets a thread's `archived`/`muted` flags.

        The agent-side counterpart to the dashboard's archive button, and the
        only way `muted` can be set today (there is deliberately no UI for it
        yet): when the user tells Ara to archive a thread, she archives *and*
        mutes it, so a later inbound message does not resurrect it. Either flag
        may also be set on its own — muting a thread the user keeps active is
        a valid request."""
        payload = self._agent_conversation_payload()
        if payload is None:
            return
        flags = {key: bool(payload[key])
                 for key in ("archived", "muted") if key in payload}
        if not flags:
            self._send_json(400, {"error": "no flags given"})
            return
        conv = _conv_set_flags(cid, **flags)
        if conv is None:
            self._send_json(404, {"error": "not found"})
            return
        self._send_json(200, _conv_summary(conv))

    def _agent_conversation_payload(self) -> dict | None:
        """Authorize an agent conversation call and return its JSON body.

        Token-gated (CONVERSATION_BACKEND_TOKEN) so only in-container agents,
        not external callers, can post on the user's behalf — mirroring the
        e-mail backend isolation. Sends the error response and returns None
        when the call is rejected."""
        if not CONVERSATION_BACKEND_TOKEN:
            self._send_json(403, {"error": "conversation backend disabled"})
            return None
        token = self.headers.get("X-Conversation-Backend-Token", "")
        if not hmac.compare_digest(token, CONVERSATION_BACKEND_TOKEN):
            self._send_json(403, {"error": "forbidden"})
            return None
        payload = self._read_json_body()
        if payload is None:
            self._send_json(400, {"error": "invalid JSON"})
            return None
        return payload

    # ── Push notification endpoints ────────────────────────────────────────
    # These sit behind the dashboard's own auth (Traefik basic auth / forward
    # auth), same as the conversation endpoints the PWA already calls. They are
    # deliberately *not* token-gated like /internal/*: the browser calls them.

    def _handle_push_subscribe(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            self._send_json(400, {"error": "invalid JSON"})
            return
        if not push_notify.subscribe(payload):
            self._send_json(400, {"error": "invalid subscription"})
            return
        self._send_json(201, {"status": "subscribed"})

    def _handle_push_unsubscribe(self) -> None:
        payload = self._read_json_body() or {}
        push_notify.unsubscribe((payload.get("endpoint") or "").strip())
        # Idempotent: removing an unknown subscription is a success, not a 404.
        self._send_json(200, {"status": "unsubscribed"})

    def _handle_agent_conversation(self) -> None:
        """A retinue agent opens a thread that needs the user's decision.

        The message is stored verbatim (the agent has already composed it); Ara
        only engages once the user replies."""
        payload = self._agent_conversation_payload()
        if payload is None:
            return
        message = (payload.get("message") or "").strip()
        if not message:
            self._send_json(400, {"error": "empty message"})
            return
        owner = _extract_on_behalf_of(payload) or DEFAULT_SESSION_KEY
        title = (payload.get("title") or "").strip() or None
        # Agents may open a "cowork" thread — the Ask-Ara MCP connector's audit
        # trail. Like edit threads it stays out of the default listing; unlike
        # them it is a record rather than a request, so it is normally quiet
        # (no unread badge, no Web Push) and the user reads it when curious.
        kind = (payload.get("kind") or "chat").strip()
        if kind not in ("chat", "cowork"):
            self._send_json(400, {"error": "invalid kind"})
            return
        quiet = bool(payload.get("quiet"))
        conv = _new_conv("agent", owner, title, "agent", message,
                         first_attachments=payload.get("attachments"),
                         kind=kind)
        if quiet:
            _conv_set_flags(conv["id"], unread=False)
        else:
            _push_conv_notification(conv, message)
        body = {"id": conv["id"], "title": conv["title"]}
        if CONVERSATION_BASE_URL:
            body["url"] = f"{CONVERSATION_BASE_URL}/#conversation-{conv['id']}"
        self._send_json(201, body)

    def _handle_agent_conversation_message(self, cid: str) -> None:
        """A retinue agent appends a message to an existing thread.

        The counterpart to opening a thread: it lets an agent deliver a file
        into the thread the user is already reading, instead of stranding it in
        a fresh tab. Text may be empty when attachments carry the payload."""
        payload = self._agent_conversation_payload()
        if payload is None:
            return
        message = (payload.get("message") or "").strip()
        attachments = payload.get("attachments") or []
        if not message and not attachments:
            self._send_json(400, {"error": "empty message"})
            return
        # Check the thread up front: _conv_add_message persists attachments
        # before it loads the thread, so an unknown id would leave orphan files.
        if _load_conv(cid) is None:
            self._send_json(404, {"error": "not found"})
            return
        # A quiet append is a record, not a request for attention: no unread
        # badge and no Web Push. Used by the cowork audit trail, which would
        # otherwise buzz the user's phone on every question the MCP connector
        # relays.
        quiet = bool(payload.get("quiet"))
        # A non-quiet append is news for the user, so it wakes an archived
        # thread (unless muted): otherwise the message lands unread in a thread
        # that no longer appears in the active list, and is never seen.
        conv = _conv_add_message(cid, "agent", message, unread=not quiet,
                                 attachments=attachments, wake=not quiet)
        if conv is None:
            self._send_json(404, {"error": "not found"})
            return
        if not quiet:
            _push_conv_notification(conv, message or "Sent you a file")
        body = {"id": conv["id"], "title": conv["title"]}
        if CONVERSATION_BASE_URL:
            body["url"] = f"{CONVERSATION_BASE_URL}/#conversation-{conv['id']}"
        self._send_json(201, body)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/auth":
            # Traefik forward-auth endpoint for every public router that points
            # its forwardAuth middleware here. Returns 200 to authorize, 401
            # (with a Basic challenge) to make the browser prompt for a password,
            # or 403 for a presented-but-rejected certificate — or for a valid
            # basic-auth user that is scoped to other hosts than this one.
            status, extra = gateway_auth.decide(
                self.headers,
                AUTH_CONFIG["users"],
                cert_header=AUTH_CONFIG["cert_header"],
                cert_info_header=AUTH_CONFIG["cert_info_header"],
                allowed_cn=AUTH_CONFIG["allowed_cn"],
                realm=AUTH_CONFIG["realm"],
                scopes=AUTH_CONFIG["scopes"],
                host_header=AUTH_CONFIG["host_header"],
            )
            self.send_response(status)
            for k, v in extra.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/health":
            state = _load_state()
            sessions = {
                key: {
                    "session_id": entry.get("session_id"),
                    "session_fresh": _session_is_fresh(entry),
                    "last_activity": entry.get("last_activity"),
                }
                for key, entry in state.items()
            }
            self._send_json(200, {
                "status": "ok",
                "max_concurrency": MAX_CONCURRENCY,
                "sessions": sessions,
            })
            return
        job_match = _JOB_RE.match(self.path)
        if job_match:
            self._handle_job_status(job_match.group(1))
            return
        conv_path, _, conv_query = self.path.partition("?")
        if conv_path in ("/push/config", "/push/config/"):
            # The PWA reads the application server key from here before it can
            # subscribe. `enabled: false` makes it hide the opt-in button.
            self._send_json(200, {
                "enabled": push_notify.enabled(),
                "publicKey": push_notify.public_key(),
            })
            return
        if conv_path in ("/conversation-models", "/conversation-models/"):
            # The models the picker offers (see _conversation_models — LiteLLM
            # when it advertises flagged routes, static sources otherwise). The
            # dashboard fetches this to build the dropdown, so adding a flagged
            # model in LiteLLM changes the UI with no client change.
            self._send_json(200, {"models": _conversation_models()})
            return
        if conv_path in ("/conversations", "/conversations/"):
            params = urllib.parse.parse_qs(conv_query)
            if "all" in params:
                scope = "all"
            elif "archived" in params:
                scope = "archived"
            else:
                scope = "active"
            kind = (params.get("kind") or ["chat"])[0]
            if kind not in ("chat", "edit", "cowork", "all"):
                kind = "chat"
            project = (params.get("project") or [None])[0]
            self._send_json(200, {"conversations": _list_convs(scope, kind, project)})
            return
        if conv_path in ("/projects/item", "/projects/item/"):
            params = urllib.parse.parse_qs(conv_query)
            pid = (params.get("id") or [""])[0]
            try:
                item = _project_item_payload(pid)
            except Exception as exc:  # life store down — honest 502, like /projects
                self._send_json(502, {"error": "life store unreachable",
                                      "detail": str(exc)})
                return
            if item is None:
                self._send_json(404, {"error": "unknown project"})
            else:
                self._send_json(200, item)
            return
        if conv_path in ("/news", "/news/"):
            params = urllib.parse.parse_qs(conv_query)
            scope = (params.get("scope") or ["feed"])[0]
            if scope not in ("feed", "read", "hidden", "all"):
                scope = "feed"
            try:
                limit = int((params.get("limit") or ["0"])[0])
            except ValueError:
                limit = 0
            self._send_json(200, _news_payload(scope, limit if limit > 0 else None))
            return
        if conv_path in ("/news/preferences", "/news/preferences/"):
            self._send_json(200, _news_preferences_payload())
            return
        if conv_path in ("/projects", "/projects/"):
            # Live projects view, computed from the life store on demand. No
            # static file, no extractor job — the .md frontmatter is the source
            # and the triples fall out of the ~15 s qlever-dir rebuild.
            try:
                self._send_json(200, _fetch_projects())
            except Exception as exc:  # transport/parse — be honest, don't fake data
                self._send_json(502, {"error": "life store unreachable",
                                      "detail": str(exc)})
            return
        att_match = _CONV_ATT_RE.match(conv_path)
        if att_match:
            inline = "inline" in urllib.parse.parse_qs(conv_query)
            self._serve_conversation_attachment(att_match.group(1), att_match.group(2),
                                                inline=inline)
            return
        conv_match = _CONV_GET_RE.match(conv_path)
        if conv_match:
            conv = _load_conv(conv_match.group(1))
            if conv is None:
                self._send_json(404, {"error": "not found"})
            else:
                self._send_json(200, conv)
            return
        if conv_path.rstrip("/") == "/gateways":
            statuses = [
                {"slug": slug, "label": gw.get("label") or slug.title(),
                 "health": _fetch_gateway_health(gw)}
                for slug, gw in sorted(_CHANNEL_GATEWAYS.items())
            ]
            self._send_html(200, _render_gateways_html(statuses))
            return
        gw_health_match = _GATEWAY_HEALTH_RE.match(conv_path)
        if gw_health_match:
            _slug, gw = _channel_gateway(gw_health_match.group(1))
            if not gw:
                self._send_json(404, {"error": "unknown gateway"})
            else:
                self._send_json(200, _fetch_gateway_health(gw))
            return
        gw_qr_match = _GATEWAY_QR_RE.match(conv_path)
        if gw_qr_match:
            self._handle_gateway_qr(gw_qr_match.group(1))
            return
        if self.path in ("/sends", "/sends/"):
            self._send_html(200, _render_sends_index_html(_all_pending()))
        elif self.path in ("/sends/next", "/sends/next/"):
            pending = _all_pending()
            if pending:
                first = pending[0]
                self._redirect(f"/sends/{first['account']}/{first['request_id']}")
            else:
                self._send_html(200, _render_sends_index_html([]))
        elif _SEND_SINGLE_RE.match(self.path):
            m = _SEND_SINGLE_RE.match(self.path)
            self._handle_send_single(m.group(1), m.group(2))
        elif self.path == "/conversation":
            all_dates = _all_day_dates()
            self._send_html(200, _render_index_html(all_dates))
        elif self.path.startswith("/conversation/"):
            date_str = self.path[len("/conversation/"):].split("?")[0].rstrip("/")
            if not _DATE_RE.match(date_str):
                self._send_json(404, {"error": "not found"})
                return
            if not _day_file(date_str).exists():
                self._send_json(404, {"error": "not found"})
                return
            entries = _load_conversation(date_str)
            all_dates = _all_day_dates()
            self._send_html(200, _render_day_html(entries, date_str, all_dates))
        else:
            if not self._maybe_serve_dashboard():
                self._send_json(404, {"error": "not found"})

    def _handle_job_status(self, job_id: str) -> None:
        job = _get_job(job_id)
        if job is None:
            self._send_json(404, {"error": "unknown or expired job"})
            return
        status = job["status"]
        if status == "pending":
            self._send_json(200, {"status": "pending", "job_id": job_id})
            return
        if status == "error":
            body = {"status": "error"}
            if "result" in job:
                body.update(job["result"])
            elif "error" in job:
                body["error"] = job["error"]
            self._send_json(200, body)
            return
        # done
        body = {"status": "done"}
        body.update(job.get("result", {}))
        self._send_json(200, body)

    def _handle_send_single(self, account: str, request_id: str) -> None:
        channel, _gw = _channel_gateway(account)
        if channel:
            self._handle_channel_send_single(channel, request_id)
            return
        try:
            cfg = _ec_config(account)
            detail = ec.get_pending_send(cfg, request_id)
        except ec.EmailError as exc:
            self._send_html(400, _HTML_HEAD + "<body><h1>Cannot load request</h1><p>"
                            + html.escape(str(exc)) + '</p><p><a href="/sends">Back</a></p>'
                            + "</body></html>")
            return
        if detail is None:
            self._send_html(404, _HTML_HEAD + "<body><h1>Request not found</h1>"
                            + '<p><a href="/sends">Back to pending sends</a></p></body></html>')
            return
        # Compute the "Skip" target: the next pending request after this one.
        pending = _all_pending()
        next_url = None
        for idx, p in enumerate(pending):
            if p["account"] == account and p["request_id"] == request_id:
                if idx + 1 < len(pending):
                    nxt = pending[idx + 1]
                    next_url = f"/sends/{nxt['account']}/{nxt['request_id']}"
                break
        self._send_html(200, _render_send_single_html(detail, account, next_url))

    def _handle_channel_send_single(self, channel: str, request_id: str) -> None:
        """Fetch and render a channel pending send approval page."""
        gw = _CHANNEL_GATEWAYS.get(channel)
        label = gw.get("label", channel.title()) if gw else channel.title()
        if not gw:
            self._send_html(503, _HTML_HEAD + f"<body><h1>{html.escape(label)} gateway not configured</h1>"
                            + '<p><a href="/sends">Back</a></p></body></html>')
            return
        url = f"{gw['base_url']}/pending-sends/{request_id}"
        headers = {}
        if gw.get("token"):
            headers["Authorization"] = "Bearer " + gw["token"]
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                detail = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self._send_html(404, _HTML_HEAD + "<body><h1>Request not found</h1>"
                                + '<p><a href="/sends">Back to pending sends</a></p></body></html>')
            else:
                self._send_html(exc.code, _HTML_HEAD + "<body><h1>Cannot load request</h1>"
                                + '<p><a href="/sends">Back</a></p></body></html>')
            return
        except Exception as exc:
            self._send_html(502, _HTML_HEAD + f"<body><h1>{html.escape(label)} gateway unreachable</h1><p>"
                            + html.escape(str(exc)) + '</p><p><a href="/sends">Back</a></p>'
                            + "</body></html>")
            return
        pending = _all_pending()
        next_url = None
        for idx, p in enumerate(pending):
            if p.get("account") == channel and p.get("request_id") == request_id:
                if idx + 1 < len(pending):
                    nxt = pending[idx + 1]
                    next_url = f"/sends/{nxt['account']}/{nxt['request_id']}"
                break
        self._send_html(200, _render_channel_send_html(detail, channel, request_id, next_url))

    def _handle_gateway_qr(self, slug: str) -> None:
        """Proxy a gateway's pairing QR (adding the token) to the /gateways page.

        Passes the gateway's own response through verbatim — a PNG when a code
        is ready, JSON progress/errors otherwise — so the page's <img> either
        renders the code or falls back to its "not ready yet" note."""
        _slug, gw = _channel_gateway(slug)
        if not gw:
            self._send_json(404, {"error": "unknown gateway"})
            return
        try:
            with _gateway_request(gw, "/qr", GATEWAY_HEALTH_TIMEOUT) as resp:
                data = resp.read()
                content_type = resp.headers.get("Content-Type", "application/json")
                status = resp.status
        except urllib.error.HTTPError as exc:
            data = exc.read()
            content_type = exc.headers.get("Content-Type", "application/json")
            status = exc.code
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"error": f"gateway unreachable: {exc}"})
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    # ThreadingHTTPServer so quick requests (job polls, /health) are never
    # blocked head-of-line behind a long-running job. Actual `claude` concurrency
    # is still bounded by the worker pool inside send_message().
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[web-gateway] listening on port {PORT} (max concurrency {MAX_CONCURRENCY})", flush=True)
    server.serve_forever()
