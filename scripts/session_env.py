#!/usr/bin/env python3
"""The environment every `claude -p` session the framework spawns starts with.

Three daemons spawn agent sessions — the web gateway (dashboard turns, the
transcript cleanup and presentation lint passes), the scheduler (prompt and
command jobs) and the Ask-Ara MCP server (answering sessions). All three used
to hand the child a copy of their own environment, and a daemon forked by the
entrypoint carries everything the container was started with: the mailbox
passwords the gateway needs for its IMAP/SMTP backend, the LiteLLM master key,
the OpenRouter key, the basic-auth htpasswd line — anything a deployment's
`.env` happened to contain (retinue-os/retinue#15). A session processes
untrusted input with a Bash tool, so every one of those was one `env` away.

This module is the single answer to "what does a session inherit": an
**allowlist**, built from the audit of what the scripts a session runs read
from the environment. Everything not named here is dropped by construction.

Why an allowlist and not a denylist: a denylist rots. It knows the secrets of
the day it was written (`EMAIL_PASS*`, `ANTHROPIC_API_KEY`) and lets the next
one through — `CALDAV_PASSWORD`, `LITELLM_SALT_KEY`, whatever the next sidecar
brings — until someone notices. An allowlist fails the other way: a new
variable a session needs is missing until it is added, which is a visible
failure in a log, not an invisible leak. That is the trade this module makes
on purpose; `RETINUE_SESSION_ENV_EXTRA` (below) is the operator's escape hatch
so a deployment whose chamber scripts read custom variables need not edit the
framework to pass them.

What deliberately still reaches a session:

* the model credential (`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`): spawned sessions run in
  API-key or gateway mode and need it. Moving that into a sidecar is the
  separate epic; this module only stops the *unrelated* secrets.
* capability tokens (`EMAIL_BACKEND_TOKEN`, `CONVERSATION_BACKEND_TOKEN`, the
  `*_GATEWAY_TOKEN`s, `NEWS_INGEST_TOKEN`, `UPDATER_TOKEN`): they are what
  the push, contact, conversation and e-mail scripts authenticate with. A
  token buys one capability behind a policy gate; a password buys the account.
* `GITHUB_TOKEN`: the entrypoint already writes it into `~/.git-credentials`,
  which a session can read, and the `gh` CLI the Tier 3 workflow relies on
  reads it from the environment — withholding it would break `gh pr create`
  while hiding nothing.
* the Garmin login, for now: `scripts/refresh.py --ensure` runs the Garmin
  sync synchronously inside the agent's own process.

What this does not do: every process in the container runs as the same uid,
so a session can still read a daemon's `/proc/<pid>/environ`. That is the
sidecar/uid work tracked separately; this module closes the inheritance path.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping

# Comma-separated names a deployment wants passed through in addition to the
# lists below — the operator's explicit word, so it overrides even the
# withheld set. Set it on the retinue service (it is itself a RETINUE_* name,
# so nested spawners see it too).
EXTRA_VAR = "RETINUE_SESSION_ENV_EXTRA"

# Names passed through verbatim, grouped by why a session needs them.
PASSTHROUGH_NAMES: frozenset[str] = frozenset({
    # Process basics: where binaries and the Claude config live, locale, time.
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "HOSTNAME",
    "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR",
    # The egress-audit proxy and the CA it signs with: without these a session
    # either bypasses the audit or fails TLS against the proxy's certificates.
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    # Claude Code's own settings that carry no CLAUDE_ prefix: the auto-updater
    # switch a deployment sets (docs/contributing.md), the API/tool/MCP
    # timeouts, output caps, telemetry switches. Bedrock/Vertex credentials
    # (AWS_*, GOOGLE_*) are not listed — a deployment on those providers names
    # them in RETINUE_SESSION_ENV_EXTRA.
    "DISABLE_AUTOUPDATER", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
    "DISABLE_BUG_COMMAND", "DISABLE_COST_WARNINGS",
    "DISABLE_NON_ESSENTIAL_MODEL_CALLS", "DISABLE_PROMPT_CACHING",
    "FALLBACK_FOR_ALL_PRIMARY_MODELS", "FORCE_HYPERLINK",
    "MAX_THINKING_TOKENS", "MAX_MCP_OUTPUT_TOKENS", "MCP_TIMEOUT",
    "MCP_TOOL_TIMEOUT", "BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS",
    "BASH_MAX_OUTPUT_LENGTH", "API_TIMEOUT_MS", "API_FORCE_IDLE_TIMEOUT",
    "USE_BUILTIN_RIPGREP",
    # Where the chambers are (exported by the entrypoint; recurring-projects.py
    # reads CHAMBERS_ROOT).
    "CHAMBERS_DIR", "CHAMBERS_MANIFEST", "CHAMBERS_ROOT",
    # Reaching the web gateway from inside the container: dashboard threads
    # (conversation-push.py, chat-draft.py), links back to the dashboard, and
    # the e-mail backend that keeps the mailbox credentials on the gateway's
    # side. SENT_FOLDER is a folder name the triage gate lists, not a secret.
    "WEB_GATEWAY_PORT", "CONVERSATION_BACKEND_URL", "CONVERSATION_BACKEND_TOKEN",
    "CONVERSATION_BACKEND_TIMEOUT", "CONVERSATION_BASE_URL", "CONVERSATION_PUSH",
    "CHAT_DRAFT_BACKEND_URL", "SEND_APPROVAL_BASE_URL",
    "EMAIL_BACKEND_URL", "EMAIL_BACKEND_TOKEN", "EMAIL_CLIENT_PATH", "SENT_FOLDER",
    # The messenger and calendar sidecars: the push and contact-lookup scripts
    # authenticate with the shared token; the credentials themselves never
    # leave the sidecar.
    "SIGNAL_GATEWAY_SEND_URL", "SIGNAL_GATEWAY_BASE_URL", "SIGNAL_GATEWAY_TOKEN",
    "SIGNAL_GATEWAY_TIMEOUT", "SIGNAL_DEFAULT_RECIPIENT",
    "WHATSAPP_GATEWAY_SEND_URL", "WHATSAPP_GATEWAY_BASE_URL", "WHATSAPP_GATEWAY_TOKEN",
    "WHATSAPP_GATEWAY_TIMEOUT", "WHATSAPP_DEFAULT_RECIPIENT",
    "TELEGRAM_GATEWAY_SEND_URL", "TELEGRAM_GATEWAY_BASE_URL", "TELEGRAM_GATEWAY_TOKEN",
    "TELEGRAM_GATEWAY_TIMEOUT", "TELEGRAM_DEFAULT_RECIPIENT",
    "CALDAV_GATEWAY_CREATE_URL", "CALDAV_GATEWAY_TOKEN", "CALDAV_GATEWAY_TIMEOUT",
    # The updater sidecar (scripts/self-update.py).
    "UPDATER_URL", "UPDATER_TOKEN", "UPDATER_TIMEOUT",
    # The git shim's lock directory, and the repo token — already on disk in
    # ~/.git-credentials for the same uid; `gh` reads it from the environment.
    "GIT_SERIALIZE_LOCK_DIR", "GITHUB_TOKEN",
    # Project wake-up tuning (recurring-projects.py).
    "PROJECT_DEADLINE_LEAD_DAYS",
    # Garmin: refresh.py --ensure runs sync-garmin.py in the agent's process.
    # Goes away with the sidecar epic.
    "GARMIN_EMAIL", "GARMIN_PASSWORD",
})

# Name prefixes passed through: the framework's own configuration
# (RETINUE_*, including the tier models, memory switches and this module's
# escape hatch), Claude Code and the model endpoint (CLAUDE_*, ANTHROPIC_*),
# the advertised SPARQL stores, locale, and the news-feed and triage tunables
# the scheduled scripts read. A prefix is the compromise between a closed list
# and chamber-defined configuration: a deployment may set any RETINUE_* knob
# without touching this file — which is exactly why no secret may be named
# RETINUE_*; the one that is, is withheld below.
PASSTHROUGH_PREFIXES: tuple[str, ...] = (
    "RETINUE_", "CLAUDE_", "ANTHROPIC_", "SPARQL_ENDPOINT_", "LC_", "XDG_",
    "NEWS_", "TRIAGE_",
)

# Under an allowed prefix, but not for sessions: the picker's LiteLLM key is
# the web gateway's credential for the proxy's management API, nothing a
# session calls.
WITHHELD_NAMES: frozenset[str] = frozenset({
    "RETINUE_LITELLM_KEY",
})

# Per-spawn stamps the spawner sets itself, after building the base
# environment: RETINUE_SESSION_MODEL advertises the model the child runs on
# (a session cannot introspect its own --model flag; scripts/memory.py stamps
# memories with it) and RETINUE_ESCALATE_FILE is Ara junior's escape hatch.
# Both are cleared rather than inherited so a stale value never mislabels a
# session or offers an escalation to a tier that has nobody above it.
PER_SPAWN_NAMES: tuple[str, ...] = ("RETINUE_SESSION_MODEL", "RETINUE_ESCALATE_FILE")


def extra_names(spec: str | None) -> frozenset[str]:
    """The names listed in a RETINUE_SESSION_ENV_EXTRA value."""
    if not spec:
        return frozenset()
    return frozenset(n.strip() for n in spec.split(",") if n.strip())


def allowed(name: str, extra: frozenset[str] = frozenset()) -> bool:
    """Whether a variable of this name may reach a spawned session."""
    if name in extra:
        return True
    if name in WITHHELD_NAMES:
        return False
    if name in PASSTHROUGH_NAMES:
        return True
    return name.startswith(PASSTHROUGH_PREFIXES)


def session_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child environment for a `claude -p` session, from `source`
    (default: this process's environment).

    Beyond filtering, one rewrite every spawner needs: when the container runs
    the e-mail backend (EMAIL_BACKEND_TOKEN is set — the entrypoint always
    generates one), point email_client.py at the gateway's internal endpoint,
    so a session that holds no mailbox password still reads and sends mail
    through the process that does (mirrors the entrypoint's setup for the main
    session; the daemons are forked before that export and never had it).
    """
    src = os.environ if source is None else source
    extra = extra_names(src.get(EXTRA_VAR))
    env = {name: value for name, value in src.items() if allowed(name, extra)}
    for name in PER_SPAWN_NAMES:
        env.pop(name, None)
    if env.get("EMAIL_BACKEND_TOKEN"):
        port = env.get("WEB_GATEWAY_PORT", "8080")
        env["EMAIL_BACKEND_URL"] = f"http://localhost:{port}/internal/email"
    return env


def main(argv: list[str]) -> int:
    """`python3 session_env.py` prints the names a session spawned from this
    environment would receive — the operator's answer to "why does my chamber
    script not see its variable"; `--values` prints them as KEY=VALUE."""
    env = session_environment()
    show_values = "--values" in argv
    for name in sorted(env):
        print(f"{name}={env[name]}" if show_values else name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
