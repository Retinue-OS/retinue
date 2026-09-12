#!/usr/bin/env python3
"""Environment for the `claude -p` sessions the framework spawns.

Every headless session the framework starts — dashboard turns, the transcript
cleanup and the presentation lint (scripts/web-gateway.py), scheduler prompt
jobs and the sessions the command jobs spawn themselves (scripts/scheduler.py,
agent-self-review.py, news-curate.py, triage-gate.py), Ask-Ara answers
(scripts/ara-mcp-server.py) — gets its environment from build() below, never
from a copy of the spawner's os.environ.

Why an allowlist and not a denylist
-----------------------------------
The spawning daemons (the web gateway, the scheduler, the MCP server) are
forked by the entrypoint before it scrubs anything, so they hold the whole
container environment: the mailbox passwords the gateway's IMAP/SMTP backend
needs, the model-gateway keys, whatever else a deployment's .env carried. A
child that inherits a copy of that environment inherits every secret in it
(retinue-os/retinue#15). A denylist — "strip EMAIL_PASS*, strip the API key" —
is what the entrypoint did for the main session, and it rots: every new secret
(a second mailbox, a new gateway's credential, a key added to .env for some
other service) leaks until someone remembers to extend the list, and nothing
fails when they forget. An allowlist names what a session needs; a new secret
is dropped by construction, and a forgotten *non*-secret fails loudly (the
script that reads it reports it unset), which is the failure mode to prefer.

What passes is data, in one place: exact names, name prefixes and name
suffixes below. Deployments whose chamber scripts read variables the framework
does not know about name them in RETINUE_SESSION_ENV_EXTRA (comma-separated;
a trailing `*` admits a prefix) instead of editing this file.

Two per-session values are never inherited, whatever the source holds:
RETINUE_SESSION_MODEL (the model stamp scripts/memory.py records; a stale one
would mislabel a session) and RETINUE_ESCALATE_FILE (Ara junior's escape
hatch, docs/model-routing.md) — the spawner sets them per spawn through the
`model` and `escalate_file` arguments.

What this does NOT do: a session runs as the same uid as the daemons, so it can
still read a daemon's /proc/<pid>/environ. Keeping the secrets out of the
daemons' environments altogether (sidecars, a separate uid) is tracked
separately; this module only guarantees that no session *inherits* one.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping

# ── The allowlist ─────────────────────────────────────────────────────────────
# Kept as plain data so the tests (tests/test_session_env.py) and an operator
# reading this file see the same list. Comments say why a group is here, so a
# future entry can be judged by the same standard: a session needs it, and it
# is not a credential — capability tokens (which authorise a request to a
# framework service that then applies its own send policy) are deliberately in,
# credentials (which open a third-party account directly) are deliberately out.

SESSION_ENV_NAMES: frozenset[str] = frozenset({
    # Process basics: where the binaries, the Claude config (/root/.claude) and
    # the temp dir are, and how text is encoded. Claude Code will not start
    # without HOME and PATH.
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "HOSTNAME",
    "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR",
    # Egress-audit proxy and its CA (docker-compose.yml, the retinue service):
    # without these a session's HTTP traffic bypasses the audit or fails TLS
    # verification against the MITM certificates. Both spellings, since curl
    # and requests read the lower-case ones.
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    # Runtime knobs an operator may set for node (Claude Code runs on it).
    "NODE_OPTIONS",
    # Claude Code's documented switches that do not carry the CLAUDE_ prefix.
    "DISABLE_AUTOUPDATER", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
    "DISABLE_BUG_COMMAND", "DISABLE_COST_WARNINGS",
    "DISABLE_NON_ESSENTIAL_MODEL_CALLS", "DISABLE_PROMPT_CACHING",
    "MAX_THINKING_TOKENS", "MAX_MCP_OUTPUT_TOKENS", "API_TIMEOUT_MS",
    "MCP_TIMEOUT", "MCP_TOOL_TIMEOUT", "USE_BUILTIN_RIPGREP",
    "BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS", "BASH_MAX_OUTPUT_LENGTH",
    # Where the chambers are (scripts/memory.py, news-fetch.py, refresh.py,
    # triage_policy.py, recurring-projects.py, sync-garmin.py).
    "CHAMBERS_DIR", "CHAMBERS_MANIFEST", "CHAMBERS_ROOT", "CHAMBER_DIR",
    # Capability tokens and the in-container service URLs they authorise
    # against. A token lets a session *ask* a framework service (send this
    # mail, open this thread, file this news item); the service still applies
    # the send policy and holds the credential. That is the whole design of
    # the credential isolation (.env.example, "E-mail credential isolation").
    "EMAIL_BACKEND_TOKEN", "EMAIL_BACKEND_URL", "EMAIL_CLIENT_PATH",
    "CONVERSATION_BACKEND_TOKEN", "CONVERSATION_BACKEND_URL",
    "CONVERSATION_BACKEND_TIMEOUT", "CONVERSATION_BASE_URL",
    "CONVERSATION_PUSH", "CHAT_DRAFT_BACKEND_URL", "SEND_APPROVAL_BASE_URL",
    "CHATS_INGEST_TOKEN", "CHATS_INGEST_URL",
    "UPDATER_TOKEN", "UPDATER_URL", "UPDATER_TIMEOUT",
    "WEB_GATEWAY_PORT",
    # The life store as the gateway's own scripts address it (projects card,
    # recurring-projects.py); the SPARQL_ENDPOINT_ prefix below carries the
    # advertised form.
    "QLEVER_LIFE_URL", "QLEVER_TIMEOUT", "QLEVER_GRAPH_BASE",
    # Triage: the gate lists the Sent folder of the default account by name
    # (scripts/triage-gate.py) and the skill derives its omnibus window from
    # the processing interval. Non-secret mailbox *settings*; the credentials
    # stay with the gateway's backend.
    "SENT_FOLDER", "EMAIL_PROCESSING_INTERVAL",
    "PROJECT_DEADLINE_LEAD_DAYS",
    # Garmin stays for now: scripts/refresh.py --ensure runs sync-garmin.py
    # synchronously inside the agent's own process, so the credential has to
    # be where that process is. Moving the fetch into a sidecar is the
    # separate epic; when it lands, these two lines go.
    "GARMIN_EMAIL", "GARMIN_PASSWORD",
})

SESSION_ENV_PREFIXES: tuple[str, ...] = (
    # Locale.
    "LC_",
    # Python and git configuration a deployment may set (PYTHONPATH,
    # PYTHONUNBUFFERED, GIT_AUTHOR_*, GIT_SSL_CAINFO, GIT_SERIALIZE_LOCK_DIR).
    "PYTHON", "GIT_",
    # The model endpoint and credential: ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
    # ANTHROPIC_BASE_URL, ANTHROPIC_CUSTOM_HEADERS, ANTHROPIC_MODEL, … Sessions
    # the gateway and the scheduler spawn run in API-key/gateway mode (the
    # entrypoint drops the key only for the OAuth main session), so this is
    # the one credential a session legitimately needs.
    "ANTHROPIC_",
    # Claude Code itself (CLAUDE_CODE_*, CLAUDE_CONFIG_DIR) and the framework's
    # own Claude-side settings (CLAUDE_PERMISSION_MODE, CLAUDE_AUTH_*,
    # CLAUDE_OAUTH_*, CLAUDE_CRED_FILE — paths and public identifiers, never
    # a token).
    "CLAUDE_",
    # The framework's configuration namespace: models and tiers, memory,
    # the escape hatch, chamber-defined RETINUE_* settings expanded in
    # .schedule.json manifests. One name in it holds a key and is excluded
    # below.
    "RETINUE_",
    # Read-only store advertisement (CLAUDE.md "SPARQL endpoints").
    "SPARQL_ENDPOINT_",
    # News feed (scripts/news_store.py, news-fetch.py, news-curate.py,
    # news_ingest.py) and triage tunables (scripts/triage-gate.py).
    "NEWS_", "TRIAGE_",
)

SESSION_ENV_SUFFIXES: tuple[str, ...] = (
    # The messenger and calendar gateways' client side (scripts/signal-push.py
    # and siblings, *-contacts.py, caldav-push.py): the shared token that
    # authorises a send request and the in-network URLs. The gateway that owns
    # the account applies the send policy; the account credential itself
    # (SIGNAL's link, TELEGRAM_API_HASH, CALDAV_PASSWORD) never lives in this
    # container. Suffix-matched so a deployment's extra gateway
    # (FOO_GATEWAY_TOKEN) enrols the same way.
    "_GATEWAY_TOKEN", "_GATEWAY_BASE_URL", "_GATEWAY_SEND_URL",
    "_GATEWAY_CREATE_URL", "_GATEWAY_TIMEOUT", "_DEFAULT_RECIPIENT",
)

# A name that matches an allowed prefix but holds a secret. Keep this set at
# one entry if at all possible: a secret named into an allowed namespace is a
# denylist in miniature, with the same rot. RETINUE_LITELLM_KEY is the picker's
# auth override for the model gateway (web-gateway.py); only the gateway reads
# it. New secrets should not be named RETINUE_*.
SESSION_ENV_EXCLUDED: frozenset[str] = frozenset({"RETINUE_LITELLM_KEY"})

# Never inherited: set per spawn (see build()).
_PER_SPAWN: tuple[str, ...] = ("RETINUE_SESSION_MODEL", "RETINUE_ESCALATE_FILE")

EXTRA_VAR = "RETINUE_SESSION_ENV_EXTRA"


def _extra_rules(source: Mapping[str, str]) -> tuple[set[str], tuple[str, ...]]:
    """Parse RETINUE_SESSION_ENV_EXTRA into (exact names, prefixes).

    Comma- or whitespace-separated; an entry ending in `*` is a prefix
    (`MYCHAMBER_*`). The escape hatch is for a deployment whose chamber scripts
    read variables the framework does not know about; it is read from the
    source mapping, so it is itself part of the spawner's environment (and
    passes through under the RETINUE_ prefix, so nested spawns honour it too).
    """
    raw = source.get(EXTRA_VAR, "") or ""
    exact: set[str] = set()
    prefixes: list[str] = []
    for entry in raw.replace(",", " ").split():
        if entry.endswith("*"):
            if len(entry) > 1:
                prefixes.append(entry[:-1])
        else:
            exact.add(entry)
    return exact, tuple(prefixes)


def allowed(name: str, source: Mapping[str, str] | None = None) -> bool:
    """Whether `name` passes from the spawner's environment into a session.

    The per-spawn stamps are never inherited, whatever the operator names —
    the escape hatch admits configuration, not a stale label or another
    session's escalation flag. Below that, an explicit entry in
    RETINUE_SESSION_ENV_EXTRA wins: the operator's stated decision. Otherwise
    the built-in exclusions apply before the built-in rules, so a prefix
    never admits a name listed as excluded.
    """
    if name in _PER_SPAWN:
        return False
    extra_exact, extra_prefixes = _extra_rules(os.environ if source is None else source)
    if name in extra_exact:
        return True
    if name in SESSION_ENV_EXCLUDED:
        return False
    if name in SESSION_ENV_NAMES:
        return True
    if name.startswith(SESSION_ENV_PREFIXES) or name.startswith(extra_prefixes):
        return True
    return name.endswith(SESSION_ENV_SUFFIXES)


def build(source: Mapping[str, str] | None = None, *, model: str = "",
          escalate_file: "str | os.PathLike[str] | None" = None) -> dict[str, str]:
    """Return the environment for one spawned session.

    `source` is the spawner's environment (default os.environ); only the
    allowlisted names are copied. `model` advertises the model the session runs
    on as RETINUE_SESSION_MODEL (a session cannot introspect its own --model
    flag; empty means no stamp, never an inherited one). `escalate_file` hands
    Ara junior her escalation flag path as RETINUE_ESCALATE_FILE; None means
    the session has nobody to escalate to.

    E-mail goes through the gateway's backend: whenever the spawner holds the
    EMAIL_BACKEND_TOKEN (the entrypoint always mints one in remote-control
    mode), EMAIL_BACKEND_URL is pointed at the gateway's /internal/email, so
    scripts/email_client.py proxies instead of looking for EMAIL_PASS* — which
    is not here, by construction. This is the rewrite the scheduler always did
    for its jobs, now applied to every session alike.
    """
    src = os.environ if source is None else source
    env = {name: value for name, value in src.items() if allowed(name, src)}
    if model:
        env["RETINUE_SESSION_MODEL"] = model
    if escalate_file is not None:
        env["RETINUE_ESCALATE_FILE"] = str(escalate_file)
    if env.get("EMAIL_BACKEND_TOKEN"):
        port = env.get("WEB_GATEWAY_PORT", "8080")
        env["EMAIL_BACKEND_URL"] = f"http://localhost:{port}/internal/email"
    return env


def main(argv: list[str] | None = None) -> int:
    """Diagnostic: list the names the current environment would pass to a
    session, or with --dropped the names it withholds. Names only — a value
    is never printed, that is the point."""
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], ["--dropped"]):
        print("usage: session_env.py [--dropped]", file=sys.stderr)
        return 2
    passed = build()
    if args == ["--dropped"]:
        names = sorted(n for n in os.environ if n not in passed)
    else:
        names = sorted(passed)
    print("\n".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
