#!/usr/bin/env python3
"""Checks for the allowlisted session environment (scripts/session_env.py).

Every `claude -p` session the framework spawns used to inherit a copy of the
spawning daemon's environment, and the daemons — forked by the entrypoint
before its scrub — carry everything the container was started with: mailbox
passwords, the LiteLLM keys, the OpenRouter key, the htpasswd line
(retinue-os/retinue#15). A session runs untrusted input with a Bash tool.

Covers: the module drops every such secret and keeps what a session needs,
withholds the one secret that sits under an allowed prefix, clears the
per-spawn stamps, honours the RETINUE_SESSION_ENV_EXTRA escape hatch and points
email_client.py at the gateway backend; and that the three spawners — the web
gateway (dashboard turns plus the cheap cleanup/lint passes), the scheduler
and the Ask-Ara MCP server — hand their child exactly that environment, with
the model and escalation stamps set per spawn.

    python3 tests/test_session_env.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import session_env as se  # noqa: E402

# What a deployment's .env carries that no session may ever see. The last one
# stands for whatever the next sidecar brings: an allowlist drops it unnamed.
SECRETS = {
    "EMAIL_PASS": "mailbox-password",
    "EMAIL_PASS_ARI": "ari-mailbox-password",
    "EMAIL_USER": "you@example.com",
    "IMAP_HOST": "imap.example.com",
    "SMTP_HOST": "smtp.example.com",
    "CALDAV_PASSWORD": "calendar-password",
    "CALDAV_USERNAME": "you@example.com",
    "LITELLM_MASTER_KEY": "sk-master",
    "LITELLM_SALT_KEY": "salt",
    "LITELLM_DB_PASSWORD": "db-password",
    "DATABASE_URL": "postgresql://litellm:db-password@litellm-db/litellm",
    "OPENROUTER_API_KEY": "sk-or-v1-secret",
    "RETINUE_LITELLM_KEY": "sk-picker",
    "TRAEFIK_BASIC_AUTH_USERS": "user:$apr1$hash",
    "GATEWAY_BASIC_AUTH_USERS": "user:$apr1$hash",
    "VAPID_PRIVATE_KEY": "-----BEGIN EC PRIVATE KEY-----",
    "TELEGRAM_API_HASH": "0123456789abcdef",
    "STT_TOKEN": "stt-secret",
    "CHATS_INGEST_TOKEN": "chats-rail-secret",
    "SOME_FUTURE_SECRET": "still-dropped",
}

# What a session needs: process basics, the egress proxy, the model endpoint,
# the framework's own settings, and the capability tokens its scripts use.
# Values are compared verbatim — the allowlist copies, it does not rewrite.
NEEDED = {
    "PATH": "/root/.venv/bin:/usr/local/lib/retinue-git-shim:/usr/bin",
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TERM": "xterm",
    "TZ": "Europe/Zurich",
    "HTTP_PROXY": "http://egress-audit:8080",
    "HTTPS_PROXY": "http://egress-audit:8080",
    "NO_PROXY": "localhost,127.0.0.1,retinue,litellm",
    "NODE_EXTRA_CA_CERTS": "/etc/egress-audit/certs/egress-ca-cert.pem",
    "SSL_CERT_FILE": "/etc/egress-audit/certs/egress-ca-cert.pem",
    "REQUESTS_CA_BUNDLE": "/etc/egress-audit/certs/egress-ca-cert.pem",
    "ANTHROPIC_API_KEY": "sk-ant-model-credential",
    "ANTHROPIC_AUTH_TOKEN": "gateway-token",
    # The known-external host, so the gateway's model picker never phones out
    # of the test (it skips api.anthropic.com by design).
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    "ANTHROPIC_CUSTOM_HEADERS": "x-litellm-api-key: Bearer sk-retinue",
    "ANTHROPIC_MODEL": "retinue-claude",
    "RETINUE_CLAUDE_MODEL": "retinue-claude",
    "RETINUE_TRIAGE_MODEL": "sonnet",
    "RETINUE_MEMORY": "1",
    "CLAUDE_PERMISSION_MODE": "acceptEdits",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "DISABLE_AUTOUPDATER": "1",
    "SPARQL_ENDPOINT_LIFE": "http://qlever-life:7001",
    "SPARQL_ENDPOINT_LIFE_DESC": "the life store",
    "CHAMBERS_DIR": "/workspace/chambers",
    "WEB_GATEWAY_PORT": "8080",
    "CONVERSATION_BACKEND_TOKEN": "conversation-capability",
    "CONVERSATION_BASE_URL": "https://agents.example.com",
    "SEND_APPROVAL_BASE_URL": "https://agents.example.com",
    "EMAIL_BACKEND_TOKEN": "email-capability",
    "SENT_FOLDER": "Sent",
    "NEWS_INGEST_URL": "http://retinue:8080/internal/news",
    "NEWS_INGEST_TOKEN": "news-capability",
    "NEWS_DIR": "/root/.retinue/news",
    "TRIAGE_STATE_DIR": "/root/.retinue/triage",
    "UPDATER_URL": "http://updater:9000/update",
    "UPDATER_TOKEN": "updater-capability",
    "SIGNAL_GATEWAY_SEND_URL": "http://signal-gateway:8090/send",
    "SIGNAL_GATEWAY_BASE_URL": "http://signal-gateway:8090",
    "SIGNAL_GATEWAY_TOKEN": "signal-capability",
    "SIGNAL_DEFAULT_RECIPIENT": "+15551234567",
    "WHATSAPP_GATEWAY_SEND_URL": "http://whatsapp-gateway:8092/send",
    "WHATSAPP_GATEWAY_BASE_URL": "http://whatsapp-gateway:8092",
    "WHATSAPP_GATEWAY_TOKEN": "whatsapp-capability",
    "TELEGRAM_GATEWAY_SEND_URL": "http://telegram-gateway:8093/send",
    "TELEGRAM_GATEWAY_BASE_URL": "http://telegram-gateway:8093",
    "TELEGRAM_GATEWAY_TOKEN": "telegram-capability",
    "CALDAV_GATEWAY_TOKEN": "caldav-capability",
    "GITHUB_TOKEN": "ghp_repo-token",
    "GARMIN_EMAIL": "you@example.com",
    "GARMIN_PASSWORD": "garmin-password",
}

# Set by the gateway backend rewrite, never copied from the spawner.
EMAIL_BACKEND_URL = "http://localhost:8080/internal/email"


def _assert_session_env(env: dict, where: str) -> None:
    """The contract every spawner has to meet."""
    leaked = sorted(name for name in SECRETS if name in env)
    assert not leaked, f"{where}: secrets reached the session: {leaked}"
    for name, value in NEEDED.items():
        assert env.get(name) == value, f"{where}: {name} missing or changed: {env.get(name)!r}"
    assert env.get("EMAIL_BACKEND_URL") == EMAIL_BACKEND_URL, \
        f"{where}: email_client.py is not pointed at the gateway backend"


# ── The module on a plain mapping ────────────────────────────────────────────

def test_drops_secrets_and_keeps_what_a_session_needs():
    source = {**SECRETS, **NEEDED, "http_proxy": "http://egress-audit:8080"}
    env = se.session_environment(source)
    _assert_session_env(env, "module")
    # curl reads the lowercase name; the compose file sets the uppercase one.
    assert env["http_proxy"] == "http://egress-audit:8080"
    # Nothing unlisted rides along: the output is the allowlist and the one
    # rewrite, not "the input minus a denylist".
    assert set(env) == set(NEEDED) | {"http_proxy", "EMAIL_BACKEND_URL"}, \
        sorted(set(env) - set(NEEDED))
    print("PASS secrets are dropped, the needed variables pass verbatim")


def test_withholds_the_secret_under_an_allowed_prefix():
    env = se.session_environment({"RETINUE_LITELLM_KEY": "sk-picker",
                                  "RETINUE_LITELLM_URL": "http://litellm:4000",
                                  "RETINUE_ROUTER_MODEL": "haiku"})
    assert "RETINUE_LITELLM_KEY" not in env
    assert env["RETINUE_ROUTER_MODEL"] == "haiku"
    assert env["RETINUE_LITELLM_URL"] == "http://litellm:4000"
    print("PASS RETINUE_LITELLM_KEY is withheld although RETINUE_* passes")


def test_clears_the_per_spawn_stamps():
    """A stale stamp would mislabel memories or offer an escalation to the
    tier that has nobody above it; the spawner sets them itself."""
    env = se.session_environment({"RETINUE_SESSION_MODEL": "stale",
                                  "RETINUE_ESCALATE_FILE": "/tmp/stale-flag",
                                  "RETINUE_CLAUDE_MODEL": "opus"})
    assert "RETINUE_SESSION_MODEL" not in env
    assert "RETINUE_ESCALATE_FILE" not in env
    assert env["RETINUE_CLAUDE_MODEL"] == "opus"
    print("PASS the per-spawn stamps are never inherited")


def test_escape_hatch_passes_named_variables():
    source = {"RETINUE_SESSION_ENV_EXTRA": " MY_CHAMBER_API_KEY, ,OTHER_KEY ",
              "MY_CHAMBER_API_KEY": "chamber-secret",
              "OTHER_KEY": "other",
              "NOT_LISTED": "dropped"}
    env = se.session_environment(source)
    assert env["MY_CHAMBER_API_KEY"] == "chamber-secret"
    assert env["OTHER_KEY"] == "other"
    assert "NOT_LISTED" not in env
    # The list itself travels (a RETINUE_* name), so a session-side script
    # that spawns its own `claude -p` sees the operator's decision too.
    assert env["RETINUE_SESSION_ENV_EXTRA"] == source["RETINUE_SESSION_ENV_EXTRA"]
    # An explicit operator decision overrides the withheld set.
    env = se.session_environment({"RETINUE_SESSION_ENV_EXTRA": "RETINUE_LITELLM_KEY",
                                  "RETINUE_LITELLM_KEY": "sk-picker"})
    assert env["RETINUE_LITELLM_KEY"] == "sk-picker"
    # Unset or empty: nothing extra, no error.
    assert "X" not in se.session_environment({"X": "1", "RETINUE_SESSION_ENV_EXTRA": ""})
    assert "X" not in se.session_environment({"X": "1"})
    print("PASS RETINUE_SESSION_ENV_EXTRA passes exactly the named variables")


def test_points_email_client_at_the_gateway_backend():
    """A session holds no mailbox password, so it must reach mail through the
    process that does — on the gateway's actual port."""
    env = se.session_environment({"EMAIL_BACKEND_TOKEN": "t", "WEB_GATEWAY_PORT": "9090",
                                  "EMAIL_BACKEND_URL": "http://stale:1/internal/email"})
    assert env["EMAIL_BACKEND_URL"] == "http://localhost:9090/internal/email"
    assert env["EMAIL_BACKEND_TOKEN"] == "t"
    # Default port when the variable is absent.
    assert se.session_environment({"EMAIL_BACKEND_TOKEN": "t"})["EMAIL_BACKEND_URL"] \
        == "http://localhost:8080/internal/email"
    # No backend token, no rewrite: whatever the spawner had passes through.
    assert "EMAIL_BACKEND_URL" not in se.session_environment({"WEB_GATEWAY_PORT": "8080"})
    env = se.session_environment({"EMAIL_BACKEND_URL": "http://elsewhere/internal/email"})
    assert env["EMAIL_BACKEND_URL"] == "http://elsewhere/internal/email"
    print("PASS email_client.py is pointed at the gateway backend")


def test_default_source_is_this_process():
    os.environ["RETINUE_SESSION_ENV_PROBE"] = "seen"
    try:
        assert se.session_environment()["RETINUE_SESSION_ENV_PROBE"] == "seen"
    finally:
        del os.environ["RETINUE_SESSION_ENV_PROBE"]
    print("PASS the default source is os.environ")


# ── The three spawners ───────────────────────────────────────────────────────

def _seed_process_environment(tmp: Path) -> None:
    """Make this process look like a daemon forked with the whole .env."""
    os.environ.update(SECRETS)
    os.environ.update(NEEDED)
    # Sandbox the pre-spawn credential refresh and every state file the
    # modules read at import (the sibling spawn tests do the same).
    os.environ["CLAUDE_CRED_FILE"] = str(tmp / "claude" / ".credentials.json")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    NEEDED["CHAMBERS_DIR"] = os.environ["CHAMBERS_DIR"]


def _load(name: str, filename: str):
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Proc:
    pid = 4242
    returncode = 0

    def communicate(self, timeout=None):
        return "{}", ""


def check_scheduler(tmp: Path):
    os.environ["SCHEDULER_STATE_DIR"] = str(tmp / "sched-state")
    os.environ["BASE_SCHEDULE"] = str(tmp / "no-base-schedule.json")
    os.environ["RETINUE_ROUTER_MODEL"] = "haiku"
    sched = _load("scheduler_session_env_under_test", "scheduler.py")

    env = sched.job_env("sonnet")
    _assert_session_env(env, "scheduler job_env")
    assert env["RETINUE_SESSION_MODEL"] == "sonnet"
    assert "RETINUE_SESSION_MODEL" not in sched.job_env(""), "a job without --model carries no stamp"
    assert "RETINUE_ESCALATE_FILE" not in env

    # And a real prompt job hands exactly that environment to Popen.
    spawned = {}

    def fake_spawn(cmd, **kwargs):
        spawned["cmd"], spawned["env"] = cmd, kwargs["env"]
        return _Proc()

    sched.spawn_process = fake_spawn
    sched.claude_auth.ensure_fresh_credentials = lambda **kw: {"action": "fresh"}
    sched.run_job({"id": "probe", "prompt": "hello", "interval_seconds": 60,
                   "_source": str(tmp / "chambers" / "x" / ".schedule.json")})
    _assert_session_env(spawned["env"], "scheduler run_job")
    assert spawned["env"]["RETINUE_SESSION_MODEL"] == "haiku", spawned["env"].get("RETINUE_SESSION_MODEL")
    # A command job (a script, not claude) is spawned with the same environment.
    sched.run_job({"id": "probe-cmd", "command": "true", "interval_seconds": 60,
                   "_source": str(tmp / "chambers" / "x" / ".schedule.json")})
    _assert_session_env(spawned["env"], "scheduler command job")
    assert "RETINUE_SESSION_MODEL" not in spawned["env"]
    print("PASS the scheduler spawns jobs with the allowlisted environment")


def check_ara_mcp(tmp: Path):
    os.environ["ARA_MCP_STATE_DIR"] = str(tmp / "ara-mcp")
    os.environ["ARA_MCP_AUDIT"] = "0"
    os.environ["ARA_MCP_SYNC_WAIT"] = "1"
    mcp = _load("ara_mcp_session_env_under_test", "ara-mcp-server.py")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        raise RuntimeError("stop before exec")

    mcp.subprocess.run = fake_run
    mcp.claude_auth.ensure_fresh_credentials = lambda **kw: {"action": "fresh"}
    flag = tmp / "ara-mcp-escalate-flag"
    status, _text = mcp._run_once("what is due?", "haiku", flag)
    assert status == "error", status  # the fake stopped it; the env was built first
    _assert_session_env(captured["env"], "ara-mcp _run_once")
    assert captured["env"]["RETINUE_SESSION_MODEL"] == "haiku"
    assert captured["env"]["RETINUE_ESCALATE_FILE"] == str(flag)
    # Senior's re-run: no model stamp when none is pinned, no escape hatch.
    mcp._run_once("what is due?", "", None)
    assert "RETINUE_SESSION_MODEL" not in captured["env"]
    assert "RETINUE_ESCALATE_FILE" not in captured["env"]
    print("PASS the Ask-Ara answering session gets the allowlisted environment")


def check_web_gateway(tmp: Path):
    for var in ("RETINUE_CONVERSATION_MODELS", "RETINUE_LITELLM_URL"):
        os.environ.pop(var, None)
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state" / "state.json")
    os.environ["RETINUE_ROUTER_MODEL"] = "haiku"
    os.environ["RETINUE_FRONTIER_MODEL"] = "opus"
    wg = _load("web_gateway_session_env_under_test", "web-gateway.py")
    wg.claude_auth.ensure_fresh_credentials = lambda **kw: {"action": "fresh"}
    spawns = []

    def fake_run(cmd, **kwargs):
        env = kwargs["env"]
        spawns.append((cmd, env))
        # Junior escalates on her first turn: she creates the flag file.
        if len(spawns) == 1 and env.get("RETINUE_ESCALATE_FILE"):
            Path(env["RETINUE_ESCALATE_FILE"]).write_text("")
        return _Result(stdout=json.dumps({"session_id": "s1", "result": "done"}))

    wg.subprocess.run = fake_run
    out = wg.send_message("hello", session_key="conv:session-env")
    assert out.get("escalated") is True, out
    assert len(spawns) == 2, [c[:4] for c, _ in spawns]
    junior_cmd, junior_env = spawns[0]
    senior_cmd, senior_env = spawns[1]
    _assert_session_env(junior_env, "web-gateway dashboard turn (junior)")
    _assert_session_env(senior_env, "web-gateway dashboard turn (senior)")
    assert junior_env["RETINUE_SESSION_MODEL"] == "haiku" and "--model" in junior_cmd
    assert junior_env["RETINUE_ESCALATE_FILE"], "junior must be offered the escape hatch"
    assert senior_env["RETINUE_SESSION_MODEL"] == "opus"
    assert "RETINUE_ESCALATE_FILE" not in senior_env, "senior has nobody to escalate to"

    # The cheap passes spawn through _run_claude without an env of their own
    # and must get the allowlist by default, not the gateway's environment.
    spawns.clear()

    def fake_lint(cmd, **kwargs):
        spawns.append((cmd, kwargs["env"]))
        message = cmd[-1].split("Message to lint:\n", 1)[-1]
        return _Result(stdout=json.dumps({"result": message}))

    wg.subprocess.run = fake_lint
    text = "Approve the pending send at /sends when you have a moment, please."
    assert wg._lint_presentation(text) == text
    _assert_session_env(spawns[-1][1], "web-gateway presentation lint")
    assert "RETINUE_ESCALATE_FILE" not in spawns[-1][1]

    def fake_cleanup(cmd, **kwargs):
        spawns.append((cmd, kwargs["env"]))
        return _Result(stdout=json.dumps({"result": "raw transcript, repaired."}))

    wg.subprocess.run = fake_cleanup
    assert wg._cleanup_transcript("raw transcript repaired") == "raw transcript, repaired."
    _assert_session_env(spawns[-1][1], "web-gateway transcript cleanup")
    print("PASS the web gateway spawns every claude with the allowlisted environment")


def main():
    test_drops_secrets_and_keeps_what_a_session_needs()
    test_withholds_the_secret_under_an_allowed_prefix()
    test_clears_the_per_spawn_stamps()
    test_escape_hatch_passes_named_variables()
    test_points_email_client_at_the_gateway_backend()
    test_default_source_is_this_process()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_process_environment(tmp)
        check_scheduler(tmp / "sched")
        check_ara_mcp(tmp / "mcp")
        check_web_gateway(tmp / "wg")
    print("all session-env tests passed")


if __name__ == "__main__":
    main()
