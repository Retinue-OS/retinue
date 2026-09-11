#!/usr/bin/env python3
"""Checks for the session-environment allowlist (scripts/session_env.py).

Every `claude -p` the framework spawns gets its environment from build(),
never from a copy of the spawner's os.environ (retinue-os/retinue#15). These
checks feed build() a fake container environment — the secrets a deployment's
.env carries next to the values a session needs — and assert the line falls
where the module says it does: credentials out by construction, capability
tokens and the model variables in, the per-spawn stamps never inherited, and
the RETINUE_SESSION_ENV_EXTRA escape hatch admitting what a deployment names.

    python3 tests/test_session_env.py
"""
import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

spec = importlib.util.spec_from_file_location(
    "session_env_under_test", SCRIPTS_DIR / "session_env.py")
se = importlib.util.module_from_spec(spec)
spec.loader.exec_module(se)


# What a deployment's .env puts into the container next to what the framework
# sets itself — the secrets a session must never see …
SECRETS = {
    "EMAIL_PASS": "mail-pw",
    "EMAIL_PASS_ARI": "ari-pw",
    "CALDAV_PASSWORD": "caldav-pw",
    "LITELLM_MASTER_KEY": "sk-master",
    "LITELLM_SALT_KEY": "salt",
    "LITELLM_DB_PASSWORD": "db-pw",
    "DATABASE_URL": "postgresql://litellm:db-pw@litellm-db:5432/litellm",
    "OPENROUTER_API_KEY": "sk-or-v1-x",
    "RETINUE_LITELLM_KEY": "sk-picker",
    "TRAEFIK_BASIC_AUTH_USERS": "user:$apr1$hash",
    "GITHUB_TOKEN": "ghp_x",
    "TELEGRAM_API_HASH": "tg-hash",
    "TELEGRAM_2FA_PASSWORD": "tg-2fa",
    "REPLY_TOKEN_KEY": "hmac-key",
    "VAPID_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----",
    # The reason this is an allowlist: a secret nobody has heard of yet.
    "FUTURE_SERVICE_PASSWORD": "not-yet-invented",
    "SOME_NEW_API_KEY": "also-unknown",
}

# … and what a session does need.
NEEDED = {
    # process basics and the egress proxy
    "PATH": "/usr/local/lib/retinue-git-shim:/root/.venv/bin:/usr/bin",
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TERM": "xterm",
    "TZ": "Europe/Zurich",
    "HTTP_PROXY": "http://egress-audit:8080",
    "HTTPS_PROXY": "http://egress-audit:8080",
    "NO_PROXY": "localhost,127.0.0.1,retinue",
    "NODE_EXTRA_CA_CERTS": "/etc/egress-audit/certs/egress-ca-cert.pem",
    "SSL_CERT_FILE": "/etc/egress-audit/certs/egress-ca-cert.pem",
    "REQUESTS_CA_BUNDLE": "/etc/egress-audit/certs/egress-ca-cert.pem",
    # the model credential and endpoint, Claude Code's own settings
    "ANTHROPIC_API_KEY": "sk-ant-api",
    "ANTHROPIC_AUTH_TOKEN": "oauth-or-gateway-token",
    "ANTHROPIC_BASE_URL": "http://litellm:4000",
    "ANTHROPIC_CUSTOM_HEADERS": "x-litellm-api-key: Bearer sk-retinue",
    "ANTHROPIC_MODEL": "retinue-claude",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_PERMISSION_MODE": "acceptEdits",
    "CLAUDE_CRED_FILE": "/root/.claude/.credentials.json",
    "DISABLE_AUTOUPDATER": "1",
    # the framework's own namespaces
    "RETINUE_CLAUDE_MODEL": "retinue-claude",
    "RETINUE_ROUTER_MODEL": "haiku",
    "RETINUE_FRONTIER_MODEL": "opus",
    "RETINUE_MEMORY": "1",
    "SPARQL_ENDPOINT_LIFE": "http://qlever-life:7001",
    "SPARQL_ENDPOINT_LIFE_DESC": "general-purpose life store",
    "CHAMBERS_DIR": "/workspace/chambers",
    "QLEVER_LIFE_URL": "http://qlever-life:7001",
    "WEB_GATEWAY_PORT": "8080",
    # capability tokens and the services they open
    "EMAIL_BACKEND_TOKEN": "email-cap",
    "CONVERSATION_BACKEND_TOKEN": "conv-cap",
    "CONVERSATION_BASE_URL": "https://agents.example.com",
    "SEND_APPROVAL_BASE_URL": "https://agents.example.com",
    "NEWS_INGEST_TOKEN": "news-cap",
    "NEWS_INGEST_URL": "http://retinue:8080/internal/news",
    "NEWS_DIR": "/root/.retinue/news",
    "CHATS_INGEST_TOKEN": "chats-cap",
    "UPDATER_TOKEN": "updater-cap",
    "UPDATER_URL": "http://updater:9000/update",
    "SIGNAL_GATEWAY_TOKEN": "signal-cap",
    "SIGNAL_GATEWAY_SEND_URL": "http://signal-gateway:8090/send",
    "SIGNAL_GATEWAY_BASE_URL": "http://signal-gateway:8090",
    "SIGNAL_DEFAULT_RECIPIENT": "+15551234567",
    "WHATSAPP_GATEWAY_TOKEN": "wa-cap",
    "TELEGRAM_GATEWAY_TOKEN": "tg-cap",
    "CALDAV_GATEWAY_TOKEN": "caldav-cap",
    # non-secret mailbox setting the triage gate lists by name
    "SENT_FOLDER": "Sent",
    "TRIAGE_STATE_DIR": "/root/.retinue/triage",
    # git shim and python
    "GIT_SERIALIZE_LOCK_DIR": "/tmp/git-locks",
    "PYTHONPATH": "/workspace/scripts",
    # Garmin stays until the fetch moves into a sidecar
    "GARMIN_EMAIL": "you@example.com",
    "GARMIN_PASSWORD": "garmin-pw",
}

# Non-secret configuration that only the daemons read: dropped too, because
# nothing in a session needs it — the list is what a session needs, not what
# is harmless.
DAEMON_ONLY = {
    "EMAIL_USER": "you@example.com",
    "IMAP_HOST": "imap.example.com",
    "SMTP_HOST": "smtp.example.com",
    "EMAIL_SEND_POLICY": "[]",
    "MESSENGER_GATEWAYS": '[{"base_url":"http://x:1","token":"t"}]',
    "SCHEDULER_TICK_SECONDS": "30",
    "GATEWAY_BASIC_AUTH_SCOPES": "ara-mcp:ara.example.com",
    "ARA_MCP_ENABLED": "1",
    "VAPID_SUBJECT": "mailto:admin@example.com",
}


def _source(**overrides):
    src = {**SECRETS, **NEEDED, **DAEMON_ONLY}
    src.update(overrides)
    return src


def test_secrets_never_pass():
    env = se.build(_source())
    leaked = sorted(n for n in SECRETS if n in env)
    assert not leaked, f"secrets inherited by the session: {leaked}"
    # Not merely renamed or re-keyed: no secret value survives anywhere.
    joined = "\n".join(env.values())
    for name, value in SECRETS.items():
        assert value not in joined, f"value of {name} reached the session"
    print("ok: no credential in the container environment reaches a session")


def test_needed_variables_pass_verbatim():
    env = se.build(_source())
    missing = sorted(n for n in NEEDED if n not in env)
    assert not missing, f"session lost variables it needs: {missing}"
    for name, value in NEEDED.items():
        assert env[name] == value, (name, env[name])
    print("ok: process basics, proxy/CA, model, framework and capability "
          "variables pass unchanged")


def test_daemon_only_configuration_is_dropped():
    env = se.build(_source())
    passed = sorted(n for n in DAEMON_ONLY if n in env)
    assert not passed, f"daemon-only configuration reached the session: {passed}"
    print("ok: what only the daemons read stays with the daemons")


def test_prefixes_admit_the_framework_namespaces():
    src = _source(RETINUE_TRIAGE_MODEL="sonnet", RETINUE_ANYTHING_NEW="x",
                  SPARQL_ENDPOINT_GENOMICS="http://qlever-genomics:7001",
                  CLAUDE_CODE_FUTURE_FLAG="1", ANTHROPIC_SMALL_FAST_MODEL="haiku",
                  LC_MESSAGES="C", NEWS_MAX_ITEMS="500", TRIAGE_INBOX_SCAN_LIMIT="50",
                  GIT_AUTHOR_NAME="Ara", PYTHONUNBUFFERED="1")
    env = se.build(src)
    for name in ("RETINUE_TRIAGE_MODEL", "RETINUE_ANYTHING_NEW",
                 "SPARQL_ENDPOINT_GENOMICS", "CLAUDE_CODE_FUTURE_FLAG",
                 "ANTHROPIC_SMALL_FAST_MODEL", "LC_MESSAGES", "NEWS_MAX_ITEMS",
                 "TRIAGE_INBOX_SCAN_LIMIT", "GIT_AUTHOR_NAME", "PYTHONUNBUFFERED"):
        assert name in env, name
    # A prefix never admits the one key that lives under an allowed namespace.
    assert "RETINUE_LITELLM_KEY" not in env
    print("ok: the RETINUE_/CLAUDE_/ANTHROPIC_/SPARQL_ENDPOINT_ namespaces pass, "
          "minus the excluded key")


def test_excluded_names_are_prefix_matches():
    """An exclusion that no prefix reaches is dead code — and a sign the
    secret was renamed out from under the list."""
    for name in se.SESSION_ENV_EXCLUDED:
        assert name.startswith(se.SESSION_ENV_PREFIXES), name
        assert name not in se.SESSION_ENV_NAMES, name
    print("ok: every exclusion shadows a prefix match, none is dead")


def test_gateway_client_suffixes_cover_deployment_added_gateways():
    src = _source(FOO_GATEWAY_TOKEN="foo-cap", FOO_GATEWAY_BASE_URL="http://foo:1",
                  FOO_GATEWAY_SEND_URL="http://foo:1/send",
                  FOO_GATEWAY_CREATE_URL="http://foo:1/create",
                  FOO_GATEWAY_TIMEOUT="30", FOO_DEFAULT_RECIPIENT="+1",
                  # the gateway's own side, never in this container by design
                  FOO_GATEWAY_MODE="inbox", FOO_ACCOUNT="+2",
                  FOO_SEND_POLICY="[]", FOO_PASSWORD="pw")
    env = se.build(src)
    for name in ("FOO_GATEWAY_TOKEN", "FOO_GATEWAY_BASE_URL", "FOO_GATEWAY_SEND_URL",
                 "FOO_GATEWAY_CREATE_URL", "FOO_GATEWAY_TIMEOUT",
                 "FOO_DEFAULT_RECIPIENT"):
        assert name in env, name
    for name in ("FOO_GATEWAY_MODE", "FOO_ACCOUNT", "FOO_SEND_POLICY", "FOO_PASSWORD"):
        assert name not in env, name
    print("ok: a deployment's extra gateway enrols its client side by suffix only")


def test_per_spawn_stamps_are_never_inherited():
    src = _source(RETINUE_SESSION_MODEL="stale-model",
                  RETINUE_ESCALATE_FILE="/tmp/stale-flag")
    env = se.build(src)
    assert "RETINUE_SESSION_MODEL" not in env
    assert "RETINUE_ESCALATE_FILE" not in env
    env = se.build(src, model="sonnet", escalate_file=Path("/tmp/flag-1"))
    assert env["RETINUE_SESSION_MODEL"] == "sonnet"
    assert env["RETINUE_ESCALATE_FILE"] == "/tmp/flag-1"
    # An empty model is "no stamp", not the inherited one.
    assert "RETINUE_SESSION_MODEL" not in se.build(src, model="")
    # Nor can the escape hatch bring a stale one back: naming the stamp or
    # the flag there, verbatim or by wildcard, admits configuration only.
    for extra in ("RETINUE_SESSION_MODEL,RETINUE_ESCALATE_FILE", "RETINUE_*"):
        hatch = {**src, "RETINUE_SESSION_ENV_EXTRA": extra}
        env = se.build(hatch, model="")
        assert "RETINUE_SESSION_MODEL" not in env, extra
        assert "RETINUE_ESCALATE_FILE" not in env, extra
        env = se.build(hatch, model="sonnet", escalate_file="/tmp/flag-2")
        assert env["RETINUE_SESSION_MODEL"] == "sonnet", extra
        assert env["RETINUE_ESCALATE_FILE"] == "/tmp/flag-2", extra
    print("ok: the model stamp and the escalation flag are set per spawn, "
          "never inherited — not even through the escape hatch")


def test_email_goes_through_the_gateway_backend():
    # The spawner holds the token: the session is pointed at the backend, and
    # a stale inherited URL does not survive.
    env = se.build(_source(EMAIL_BACKEND_URL="http://stale:1/internal/email",
                           WEB_GATEWAY_PORT="8181"))
    assert env["EMAIL_BACKEND_URL"] == "http://localhost:8181/internal/email"
    assert env["EMAIL_BACKEND_TOKEN"] == "email-cap"
    # The port default matches the entrypoint's.
    src = _source()
    del src["WEB_GATEWAY_PORT"]
    assert se.build(src)["EMAIL_BACKEND_URL"] == "http://localhost:8080/internal/email"
    # No token (interactive mode): nothing is invented, an explicit URL passes.
    src = _source()
    del src["EMAIL_BACKEND_TOKEN"]
    assert "EMAIL_BACKEND_URL" not in se.build(src)
    src["EMAIL_BACKEND_URL"] = "http://elsewhere:1/internal/email"
    assert se.build(src)["EMAIL_BACKEND_URL"] == "http://elsewhere:1/internal/email"
    print("ok: email_client.py in a session is routed through the gateway backend")


def test_escape_hatch_admits_what_a_deployment_names():
    src = _source(MY_CHAMBER_KEY="k", OTHER_THING="t", OTHERX="no",
                  RETINUE_SESSION_ENV_EXTRA="MY_CHAMBER_KEY, OTHER_*")
    env = se.build(src)
    assert env["MY_CHAMBER_KEY"] == "k"
    assert env["OTHER_THING"] == "t"
    assert "OTHERX" not in env, "a wildcard is a prefix, not a substring"
    # The hatch itself travels along, so a session that spawns a session
    # (news-curate.py under the scheduler) applies the same extras.
    assert env["RETINUE_SESSION_ENV_EXTRA"] == "MY_CHAMBER_KEY, OTHER_*"
    # Naming a variable is the operator's decision and wins over the built-in
    # exclusion; a wildcard does not — it is not a decision about that name.
    env = se.build(_source(RETINUE_SESSION_ENV_EXTRA="RETINUE_LITELLM_KEY"))
    assert env["RETINUE_LITELLM_KEY"] == "sk-picker"
    env = se.build(_source(RETINUE_SESSION_ENV_EXTRA="RETINUE_*"))
    assert "RETINUE_LITELLM_KEY" not in env
    # Whitespace-separated and a lone `*` are tolerated, and neither widens
    # the list to everything.
    env = se.build(_source(RETINUE_SESSION_ENV_EXTRA="  MY_CHAMBER_KEY *  ",
                           MY_CHAMBER_KEY="k"))
    assert env["MY_CHAMBER_KEY"] == "k"
    assert "EMAIL_PASS" not in env
    print("ok: RETINUE_SESSION_ENV_EXTRA admits named variables and prefixes only")


def test_default_source_is_the_process_environment():
    os.environ["FUTURE_SERVICE_PASSWORD"] = "leak?"
    os.environ["RETINUE_PROBE"] = "seen"
    try:
        env = se.build()
        assert "FUTURE_SERVICE_PASSWORD" not in env
        assert env["RETINUE_PROBE"] == "seen"
        assert env["PATH"] == os.environ["PATH"]
    finally:
        del os.environ["FUTURE_SERVICE_PASSWORD"]
        del os.environ["RETINUE_PROBE"]
    print("ok: build() without a source reads os.environ")


def test_cli_prints_names_never_values():
    os.environ["FUTURE_SERVICE_PASSWORD"] = "the-secret-value"
    os.environ["RETINUE_PROBE"] = "the-probe-value"
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert se.main([]) == 0
        names = out.getvalue().split()
        assert "RETINUE_PROBE" in names and "FUTURE_SERVICE_PASSWORD" not in names
        assert "the-probe-value" not in out.getvalue()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert se.main(["--dropped"]) == 0
        dropped = out.getvalue().split()
        assert "FUTURE_SERVICE_PASSWORD" in dropped and "RETINUE_PROBE" not in dropped
        assert "the-secret-value" not in out.getvalue()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert se.main(["--values"]) == 2
    finally:
        del os.environ["FUTURE_SERVICE_PASSWORD"]
        del os.environ["RETINUE_PROBE"]
    print("ok: the diagnostic lists names and never a value")


def main():
    test_secrets_never_pass()
    test_needed_variables_pass_verbatim()
    test_daemon_only_configuration_is_dropped()
    test_prefixes_admit_the_framework_namespaces()
    test_excluded_names_are_prefix_matches()
    test_gateway_client_suffixes_cover_deployment_added_gateways()
    test_per_spawn_stamps_are_never_inherited()
    test_email_goes_through_the_gateway_backend()
    test_escape_hatch_admits_what_a_deployment_names()
    test_default_source_is_the_process_environment()
    test_cli_prints_names_never_values()
    print("all session-env tests passed")


if __name__ == "__main__":
    main()
