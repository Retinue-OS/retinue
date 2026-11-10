#!/usr/bin/env python3
"""Checks for how the web gateway spawns `claude`.

Every dashboard turn spawns a fresh `claude -p`. Claude Code's npm auto-updater
swaps /usr/bin/claude while it runs, so a spawn landing in that window raises
FileNotFoundError; _run_claude() has to outlive the swap instead of surfacing it
as "Sorry, an error occurred" in the user's conversation.

Covers: the transient window is absorbed, the wait is bounded by a deadline
(not a retry count), the deadline is env-tunable, a permanently missing
binary still raises, and every spawn first runs the pre-spawn credential
refresh (scripts/claude_auth.py) exactly once, before the first attempt.

Also covers the environment a spawned session gets (scripts/session_env.py):
the gateway is forked before the entrypoint's scrub and holds the mailbox
credentials for its e-mail backend plus whatever else .env carries, and none
of it may reach a session — a dashboard turn, the transcript cleanup or the
presentation lint. The session gets the allowlist: the capability tokens, the
model credential, e-mail routed through the backend, the model stamp set per
spawn, and Ara junior's escalation flag only below the frontier tier. A
dashboard-thread turn also gets a reply-attachment manifest, fresh per spawn,
so the files a discarded run (junior's, before an escalation) listed never
reach the reply that is kept.

    python3 tests/test_web_gateway_claude_spawn.py
"""
import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gateway(tmp: Path, env: dict[str, str]):
    """Load scripts/web-gateway.py with sandboxed state and a controlled env."""
    for var in ("CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS",
                "RETINUE_CONVERSATION_MODELS", "RETINUE_LITELLM_URL",
                "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
                "RETINUE_ROUTER_MODEL", "RETINUE_FRONTIER_MODEL",
                "RETINUE_DASHBOARD_MODEL",
                "RETINUE_CLAUDE_MODEL", "TRANSCRIPT_CLEANUP_MODEL",
                "PRESENTATION_LINT_MODEL", "CONVERSATION_BASE_URL"):
        os.environ.pop(var, None)
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    # Keep the pre-spawn credential refresh off the real credential file —
    # unconditionally, since an inherited value would point it at one. The
    # module reads this at import, so the first load pins it for the process.
    os.environ["CLAUDE_CRED_FILE"] = str(tmp / "claude" / ".credentials.json")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    os.environ.update(env)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_claude_spawn_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeClock:
    """Monotonic clock the test advances itself, so no test ever sleeps."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _install_fakes(wg, fail_times):
    """Make the next `fail_times` spawns raise ENOENT; count all attempts, and
    record how many attempts had been made whenever the pre-spawn credential
    refresh is called."""
    clock = _FakeClock()
    calls = {"n": 0, "auth": []}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise FileNotFoundError(2, "No such file or directory", cmd[0])
        return f"ran after {calls['n']} attempt(s)"

    def fake_ensure_fresh(**kwargs):
        calls["auth"].append(calls["n"])
        return {"action": "fresh"}

    wg.subprocess.run = fake_run
    wg.claude_auth.ensure_fresh_credentials = fake_ensure_fresh
    wg.time.monotonic = clock.monotonic
    wg.time.sleep = clock.sleep
    return clock, calls


def test_absorbs_transient_window(wg):
    """A swap lasting ~11 s — longer than the old 5 x 1 s budget — succeeds."""
    clock, calls = _install_fakes(wg, fail_times=22)  # 22 * 0.5 s = 11 s
    start = clock.now
    assert wg._run_claude(["/usr/bin/claude", "-p"]) == "ran after 23 attempt(s)"
    assert calls["n"] == 23, calls
    assert clock.now - start == 11.0, clock.now - start


def test_bounded_by_deadline_not_retry_count(wg):
    """A binary that never returns raises once the deadline passes."""
    clock, calls = _install_fakes(wg, fail_times=10_000)
    start = clock.now
    try:
        wg._run_claude(["/usr/bin/claude", "-p"])
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError after the deadline")
    elapsed = clock.now - start
    assert elapsed <= wg.CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS + 1.0, elapsed
    # Far more attempts than the old fixed budget of 5.
    assert calls["n"] > 100, calls


def test_first_attempt_does_not_sleep(wg):
    """The common case — binary present — costs no extra latency."""
    clock, calls = _install_fakes(wg, fail_times=0)
    start = clock.now
    assert wg._run_claude(["/usr/bin/claude", "-p"]) == "ran after 1 attempt(s)"
    assert calls["n"] == 1, calls
    assert clock.now == start


def test_refreshes_credentials_once_before_the_first_attempt(wg):
    """The pre-spawn refresh runs once per spawn, ahead of the first attempt —
    and is not repeated on the ENOENT retries."""
    clock, calls = _install_fakes(wg, fail_times=3)
    assert wg._run_claude(["/usr/bin/claude", "-p"]) == "ran after 4 attempt(s)"
    assert calls["auth"] == [0], calls


def test_deadline_is_env_tunable(wg_short):
    assert wg_short.CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS == 5.0
    clock, calls = _install_fakes(wg_short, fail_times=10_000)
    start = clock.now
    try:
        wg_short._run_claude(["/usr/bin/claude", "-p"])
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError after the deadline")
    assert clock.now - start <= 6.0, clock.now - start


# ── The spawned session's environment ────────────────────────────────────────

# What the gateway's own environment holds that a session must never see …
_SECRETS = {"EMAIL_PASS": "mail-pw", "EMAIL_PASS_ARI": "ari-pw",
            "LITELLM_MASTER_KEY": "sk-master", "LITELLM_SALT_KEY": "salt",
            "OPENROUTER_API_KEY": "sk-or", "RETINUE_LITELLM_KEY": "sk-picker",
            "TRAEFIK_BASIC_AUTH_USERS": "u:$apr1$h", "GITHUB_TOKEN": "ghp_x",
            "CALDAV_PASSWORD": "caldav-pw", "REPLY_TOKEN_KEY": "hmac"}
# … and what a session needs from it.
_NEEDED = {"EMAIL_BACKEND_TOKEN": "email-cap", "WEB_GATEWAY_PORT": "8080",
           "ANTHROPIC_API_KEY": "sk-ant", "CONVERSATION_BACKEND_TOKEN": "conv-cap",
           "NEWS_INGEST_TOKEN": "news-cap", "SIGNAL_GATEWAY_TOKEN": "signal-cap",
           "SPARQL_ENDPOINT_LIFE": "http://qlever-life:7001",
           "RETINUE_SESSION_MODEL": "stale-stamp",
           "RETINUE_ESCALATE_FILE": "/tmp/stale-flag",
           "RETINUE_REPLY_ATTACHMENTS_FILE": "/tmp/stale-manifest"}


class _daemon_environment:
    """os.environ as the gateway daemon sees it, for the duration of a check."""

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in {**_SECRETS, **_NEEDED}}
        os.environ.update(_SECRETS)
        os.environ.update(_NEEDED)

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _capture_spawns(wg, stdout: str):
    """Replace subprocess.run with a fake that records every spawn's argv and
    kwargs and answers with `stdout` as a successful `claude` run."""
    spawns = []

    def fake_run(cmd, **kwargs):
        spawns.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    wg.subprocess.run = fake_run
    wg.claude_auth.ensure_fresh_credentials = lambda **kw: {"action": "fresh"}
    return spawns


def _assert_allowlisted(env):
    assert env is not None, "spawned without an explicit environment"
    for name in _SECRETS:
        assert name not in env, f"{name} inherited by the session"
    for name in ("EMAIL_BACKEND_TOKEN", "ANTHROPIC_API_KEY",
                 "CONVERSATION_BACKEND_TOKEN", "NEWS_INGEST_TOKEN",
                 "SIGNAL_GATEWAY_TOKEN", "SPARQL_ENDPOINT_LIFE"):
        assert env[name] == _NEEDED[name], name
    # The gateway itself never had this (it is forked before the entrypoint
    # exports it), so a session used to run email_client.py against the
    # credentials it inherited. Now it has no credentials and the backend URL.
    assert env["EMAIL_BACKEND_URL"] == "http://localhost:8080/internal/email"
    assert env["PATH"] == os.environ["PATH"]


def test_dashboard_turn_env_is_allowlisted(wg):
    """A conversation turn: no secret, the stamp per spawn, no escalation flag
    when no frontier tier is configured."""
    spawns = _capture_spawns(wg, json.dumps({"result": "hi", "session_id": "s-1"}))
    with _daemon_environment():
        out = wg.send_message("hello", session_key="env-check", model="sonnet")
    assert out.get("response") == "hi", out
    assert len(spawns) == 1, spawns
    cmd, kwargs = spawns[0]
    assert kwargs["cwd"] == "/workspace"
    env = kwargs["env"]
    _assert_allowlisted(env)
    assert env["RETINUE_SESSION_MODEL"] == "sonnet"
    assert "RETINUE_ESCALATE_FILE" not in env, "senior has nobody to escalate to"
    assert "RETINUE_REPLY_ATTACHMENTS_FILE" not in env, "not a thread turn"
    assert "--model" in cmd and "sonnet" in cmd


def test_thread_turn_collects_reply_attachments(wg):
    """A thread turn's session lists files in its own manifest; they come
    back as reply_files and the manifest is gone afterwards."""
    manifests = []

    def attach_and_answer(cmd, **kwargs):
        manifest = kwargs["env"]["RETINUE_REPLY_ATTACHMENTS_FILE"]
        manifests.append(manifest)
        Path(manifest).write_text("/tmp/a.png\n/tmp/b.pdf\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": "", "session_id": "s-5"}), stderr="")

    _capture_spawns(wg, "")
    wg.subprocess.run = attach_and_answer
    with _daemon_environment():
        out = wg.send_message("chart please", session_key="env-reply",
                              model="sonnet", reply_attachments=True)
    assert out.get("reply_files") == ["/tmp/a.png", "/tmp/b.pdf"], out
    assert manifests[0] != "/tmp/stale-manifest", manifests
    assert not Path(manifests[0]).exists(), "manifest left behind"


def test_thread_turn_collects_shell_actions(wg):
    """A thread turn's session lists shell actions (scripts/shell-type.py) in
    its own manifest; the valid ones come back as reply_shell_actions, the
    invalid ones are dropped, and the manifest is gone afterwards. A turn that
    is not a thread turn gets no manifest at all."""
    manifests = []
    good = {"kind": "keyboard-type", "id": "a" * 32, "text": "hello", "enter": False}
    submit = {"kind": "keyboard-type", "id": "c" * 32, "text": "ls", "enter": True}

    def act_and_answer(cmd, **kwargs):
        manifest = kwargs["env"]["RETINUE_REPLY_SHELL_ACTIONS_FILE"]
        manifests.append(manifest)
        Path(manifest).write_text(
            json.dumps(good) + "\n" + json.dumps({"kind": "nope", "id": "b" * 32}) + "\n"
            + json.dumps(submit) + "\n",
            encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": "typing it", "session_id": "s-7"}),
            stderr="")

    _capture_spawns(wg, "")
    wg.subprocess.run = act_and_answer
    with _daemon_environment():
        out = wg.send_message("type hello", session_key="env-shell",
                              model="sonnet", reply_attachments=True)
    # Validated and canonical; whether the page may run them without a tap is
    # the thread worker's decision (it knows the thread and the turn).
    assert out.get("reply_shell_actions") == [good, submit], out
    assert not Path(manifests[0]).exists(), "manifest left behind"

    spawns = _capture_spawns(wg, json.dumps({"result": "hi", "session_id": "s-8"}))
    with _daemon_environment():
        wg.send_message("hello", session_key="env-shell-2", model="sonnet")
    assert "RETINUE_REPLY_SHELL_ACTIONS_FILE" not in spawns[0][1]["env"], "not a thread turn"


def test_shell_actions_ride_on_the_stored_reply(wg):
    """The thread worker stores the turn's shell actions on Ara's reply, and a
    replayed transcript tells later turns what was handed over."""
    conv = wg._new_conv("user", "reto", "t", "user", "type hello on the quest",
                        via_edge=True)
    action = {"kind": "keyboard-type", "id": "c" * 32, "text": "hello", "enter": True}
    secret = {"kind": "keyboard-secret", "id": "d" * 32, "label": "Meta account",
              "enter": False}
    real = wg.send_message
    wg.send_message = lambda *a, **k: {"response": "Typing it now.",
                                       "reply_shell_actions": [action, secret]}
    try:
        with _daemon_environment():
            assert wg._conv_worker(conv["id"], f"conv:{conv['id']}", push=False)
    finally:
        wg.send_message = real
    stored = wg._load_conv(conv["id"])
    reply = stored["messages"][-1]
    assert reply["role"] == "assistant" and reply["shell_actions"] == [
        dict(action, auto=False), dict(secret, auto=False)], reply
    note = wg._conv_render_messages(stored, stored["messages"])
    assert "type 5 characters + Enter: 'hello'" in note, note
    assert "password-manager field for 'Meta account'" in note, note


def test_sensitive_text_masked_purged_and_never_logged(wg):
    """A sensitive typing action: stored for the phone, never in a replayed
    transcript nor in the gateway's log, deleted once reported typed, and in
    any case gone from every read after the TTL."""
    import contextlib
    import io
    conv = wg._new_conv("user", "reto", "t", "user", "my PIN is 4711, type it",
                        via_edge=True)
    sens = {"kind": "keyboard-type", "id": "e" * 32, "text": "4711", "enter": True,
            "sensitive": True}
    real = wg.send_message
    wg.send_message = lambda *a, **k: {"response": "Typing it now.",
                                       "reply_shell_actions": [dict(sens)]}
    log = io.StringIO()
    try:
        with _daemon_environment(), contextlib.redirect_stdout(log):
            assert wg._conv_worker(conv["id"], f"conv:{conv['id']}", push=False)
    finally:
        wg.send_message = real
    assert "4711" not in log.getvalue().replace("my PIN is 4711, type it", ""), \
        "the sensitive text reached the log"
    stored = wg._load_conv(conv["id"])
    reply = stored["messages"][-1]
    assert reply["shell_actions"][0]["text"] == "4711", "the phone still gets it"
    note = wg._conv_render_messages(stored, [reply])
    assert "4711" not in note and "sensitive text" in note, note
    # The user's message shows its id, so Ara can offer to redact it.
    user_msg = stored["messages"][0]
    assert f"[message id: {user_msg['id']}]" in wg._conv_render_messages(stored, [user_msg])

    # Reported typed: deleted from the file at once.
    assert wg._conv_persist_purge(conv["id"], {sens["id"]}) is not None
    raw = (wg.CONVERSATIONS_DIR / f"{conv['id']}.json").read_text()
    assert "4711" not in raw.replace("my PIN is 4711, type it", ""), raw
    gone = wg._load_conv(conv["id"])["messages"][-1]["shell_actions"][0]
    assert gone["text"] is None and gone["purged"] is True, gone

    # Not reported: no read after the TTL returns it, file rewritten or not.
    conv2 = wg._new_conv("user", "reto", "t2", "user", "type the code")
    wg._conv_add_message(conv2["id"], "assistant", "ok",
                         shell_actions_=[dict(sens, id="f" * 32)])
    path = wg.CONVERSATIONS_DIR / f"{conv2['id']}.json"
    data = json.loads(path.read_text())
    data["messages"][-1]["ts"] = "2000-01-01T00:00:00+00:00"   # long expired
    path.write_text(json.dumps(data))
    served = wg._load_conv(conv2["id"])["messages"][-1]["shell_actions"][0]
    assert served["text"] is None and served["purged"], served
    wg._conv_persist_purge(conv2["id"])
    assert "4711" not in path.read_text()
    print("ok: a sensitive text rides to the phone, never into a transcript or the "
          "log, and is purged once typed or after the TTL")


def test_redact_a_user_message(wg):
    conv = wg._new_conv("user", "reto", "t", "user", "my password is hunter2")
    stored = wg._load_conv(conv["id"])
    mid = stored["messages"][0]["id"]
    wg._conv_add_message(conv["id"], "assistant", "Typed.")
    ara_id = wg._load_conv(conv["id"])["messages"][-1]["id"]
    out, status, why = wg._conv_redact_message(conv["id"], ara_id)
    assert out is None and status == 400 and "user's own" in why, why
    out, status, why = wg._conv_redact_message(conv["id"], "0" * 12)
    assert out is None and status == 404 and why == "no such message", why
    out, status, why = wg._conv_redact_message(conv["id"], mid)
    assert out is not None and status == 200 and why == "", why
    raw = (wg.CONVERSATIONS_DIR / f"{conv['id']}.json").read_text()
    assert "hunter2" not in raw, raw
    m = wg._load_conv(conv["id"])["messages"][0]
    assert m["text"] == wg.REDACTED_TEXT and m["redacted"] is True
    note = wg._conv_render_messages(wg._load_conv(conv["id"]), [m])
    assert "hunter2" not in note and "[message id:" not in note, note
    print("ok: a user message is redacted for good, by its id, and only a user's")


def test_only_a_dashboard_turn_can_type(wg):
    """Only a turn the user started from the dashboard (a message that came
    through the reverse proxy, `via_edge`) gets a shell-action manifest: not a
    chat arrival, not a "user" message from inside the container. Whatever
    such a turn returns anyway is dropped. And only a plain chat the user
    opened lets plain text run without a tap."""
    spawns = _capture_spawns(wg, json.dumps({"result": "hi", "session_id": "s-a"}))
    with _daemon_environment():
        wg.send_message("x", session_key="env-arrival", model="sonnet",
                        reply_attachments=True, reply_shell_actions=False)
    env = spawns[0][1]["env"]
    assert "RETINUE_REPLY_ATTACHMENTS_FILE" in env, "files still ride"
    assert "RETINUE_REPLY_SHELL_ACTIONS_FILE" not in env, "no typing for this turn"

    seen = []
    plain = {"kind": "keyboard-type", "id": "a" * 32, "text": "hello", "enter": False}
    tab = {"kind": "keyboard-type", "id": "b" * 32, "text": "me@x.org\t ", "enter": False}

    def stub(*a, **k):
        seen.append(k.get("reply_shell_actions"))
        return {"response": "Noted.", "reply_shell_actions": [dict(plain), dict(tab)]}

    real = wg.send_message
    wg.send_message = stub
    try:
        with _daemon_environment():
            dash = wg._new_conv("user", "reto", "t", "user", "type hello", via_edge=True)
            assert wg._conv_worker(dash["id"], f"conv:{dash['id']}",
                                   arrival="A chat message arrived.", push=False)
            assert wg._conv_worker(dash["id"], f"conv:{dash['id']}", push=False)
            inside = wg._new_conv("user", "reto", "t", "user", "type hello")  # not via the edge
            assert wg._conv_worker(inside["id"], f"conv:{inside['id']}", push=False)
            agent = wg._new_conv("agent", "reto", "Triage", "agent", "An e-mail arrived")
            wg._conv_add_message(agent["id"], "user", "type the code", via_edge=True)
            assert wg._conv_worker(agent["id"], f"conv:{agent['id']}", push=False)
    finally:
        wg.send_message = real
    assert seen == [False, True, False, True], seen
    replies = [m for m in wg._load_conv(dash["id"])["messages"] if m["role"] == "assistant"]
    assert "shell_actions" not in replies[0], "an arrival turn's actions are dropped"
    assert replies[1]["shell_actions"] == [dict(plain, auto=True), dict(tab, auto=False)], \
        replies[1]["shell_actions"]
    inside_reply = wg._load_conv(inside["id"])["messages"][-1]
    assert "shell_actions" not in inside_reply, "an in-container user message cannot type"
    agent_reply = wg._load_conv(agent["id"])["messages"][-1]
    assert [a["auto"] for a in agent_reply["shell_actions"]] == [False, False], \
        "in a thread an agent opened, nothing runs without a tap"
    print("ok: only a dashboard turn can type, and only a plain chat runs text by itself")


def _session_with_transcript(wg, tmp: Path, cid: str, sid: str) -> Path:
    """Record a Claude session for thread `cid` and a transcript file for it."""
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp / "claude-config")
    transcript = tmp / "claude-config" / "projects" / "-workspace" / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text('{"text": "my PIN is 4711"}\n', encoding="utf-8")
    wg._update_session_entry(f"conv:{cid}", {"session_id": sid, "last_activity": 1.0})
    return transcript


def _sensitive_run(sid: str, *, rc: int = 0, escalate: bool = False):
    """A fake `claude` run that attaches a sensitive action (as shell-type.py
    --sensitive would) and reports session `sid`."""
    def run(cmd, **kwargs):
        env = kwargs["env"]
        Path(env["RETINUE_REPLY_SHELL_ACTIONS_FILE"]).write_text(json.dumps(
            {"kind": "keyboard-type", "id": "e" * 32, "text": "4711", "enter": False,
             "sensitive": True}) + "\n", encoding="utf-8")
        if escalate:
            Path(env["RETINUE_ESCALATE_FILE"]).touch()
        return subprocess.CompletedProcess(
            cmd, rc, stdout=json.dumps({"result": "Typing it.", "session_id": sid}),
            stderr="" if rc == 0 else "boom")
    return run


def test_sensitive_turn_never_resumable(wg, tmp: Path):
    """A turn that attached a sensitive text never leaves a resumable session:
    send_message drops it under the session lock — so a turn queued behind it
    cannot resume it — with every session the key had, and erases their
    transcripts; on a failed run too. A plain turn keeps its session and
    remembers the one it replaced."""
    key = "conv:" + "1" * 32
    old = _session_with_transcript(wg, tmp, "1" * 32, "11111111-0000-0000-0000-000000000000")
    new = old.parent / "22222222-0000-0000-0000-000000000000.jsonl"
    new.write_text("{}\n", encoding="utf-8")
    _capture_spawns(wg, "")
    wg.subprocess.run = _sensitive_run(new.stem)
    with _daemon_environment():
        out = wg.send_message("my PIN is 4711, type it", session_key=key, model="sonnet",
                              reply_attachments=True)
    assert out.get("session_reset") is True, out
    assert wg._get_session_entry(key) == {}, "nothing to resume"
    assert not old.exists() and not new.exists(), "every transcript erased"
    assert out["reply_shell_actions"][0]["sensitive"] is True, "the phone still gets it"

    # Failed run: the same.
    key2 = "conv:" + "2" * 32
    old2 = _session_with_transcript(wg, tmp, "2" * 32, "33333333-0000-0000-0000-000000000000")
    wg.subprocess.run = _sensitive_run("44444444-0000-0000-0000-000000000000", rc=1)
    with _daemon_environment():
        out = wg.send_message("my PIN is 4711", session_key=key2, model="sonnet",
                              reply_attachments=True)
    assert "error" in out and wg._get_session_entry(key2) == {} and not old2.exists(), out

    # A plain turn: recorded, with the session it replaced remembered.
    key3 = "conv:" + "3" * 32
    _session_with_transcript(wg, tmp, "3" * 32, "55555555-0000-0000-0000-000000000000")
    wg._record_turn_session(key3, {"session_id": "66666666-0000-0000-0000-000000000000",
                                   "last_activity": 2.0})
    entry = wg._get_session_entry(key3)
    assert entry["session_id"].startswith("6666") and \
        entry["previous_session_ids"] == ["55555555-0000-0000-0000-000000000000"], entry
    print("ok: a sensitive turn's session is never resumable, failed or not")


def test_escalated_junior_sensitive_and_fresh_session(wg_tiered, tmp: Path):
    """Junior attached a sensitive text, then escalated: her reply is discarded
    but her session typed it, so the turn's session is dropped all the same —
    and her own fresh session's transcript erased."""
    wg = wg_tiered
    key = "conv:" + "4" * 32
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp / "claude-config")
    proj = tmp / "claude-config" / "projects" / "-workspace"
    proj.mkdir(parents=True, exist_ok=True)
    junior_t = proj / "77777777-0000-0000-0000-000000000000.jsonl"
    junior_t.write_text("{}\n", encoding="utf-8")
    runs = []

    def run(cmd, **kwargs):
        runs.append(cmd)
        if len(runs) == 1:
            return _sensitive_run(junior_t.stem, escalate=True)(cmd, **kwargs)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": "senior", "session_id": "88888888-0000-0000-0000-000000000000"}),
            stderr="")

    _capture_spawns(wg, "")
    wg.subprocess.run = run
    with _daemon_environment():
        out = wg.send_message("type my PIN", session_key=key, reply_attachments=True)
    assert out.get("escalated") and out.get("session_reset"), out
    assert "reply_shell_actions" not in out, "junior's actions go with her reply"
    assert wg._get_session_entry(key) == {}
    assert not junior_t.exists(), "junior's fresh session is erased"
    print("ok: an escalated junior's sensitive action still ends the session")


def test_a_new_session_gets_the_whole_thread(wg):
    """A turn whose stored session went while it waited (reset, expired) starts
    a new one — with the whole thread (restart_message), not the resumed
    session's fragment."""
    spawns = _capture_spawns(wg, json.dumps({"result": "ok", "session_id": "s-new"}))
    with _daemon_environment():
        wg.send_message("only the new bit", session_key="conv:" + "5" * 32, model="sonnet",
                        restart_message="THE WHOLE THREAD", reply_attachments=True)
    assert spawns[0][0][-1] == "THE WHOLE THREAD", spawns[0][0][-3:]
    # A thread's prompt is built once the lock is held, for the session as it
    # is then: what the turn read before waiting does not reach the model.
    spawns.clear()
    built = []

    def build(fresh):
        built.append(fresh)
        return f"built fresh={fresh}"

    with _daemon_environment():
        wg.send_message("stale", session_key="conv:" + "6" * 32, model="sonnet",
                        restart_message="stale too", prompt_builder=build)
    assert spawns[0][0][-1] == "built fresh=False" and built == [False], (built, spawns[0][0][-1])
    wg._record_turn_session("conv:" + "6" * 32, {"session_id": "s-6", "last_activity": wg._now_ts()})
    spawns.clear(); built.clear()
    with _daemon_environment():
        wg.send_message("stale", session_key="conv:" + "6" * 32, model="sonnet",
                        prompt_builder=build)
    cmd = spawns[0][0]
    assert cmd[cmd.index("--resume") + 1] == "s-6" and cmd[-1] == "built fresh=True", cmd[-4:]
    assert built == [True, False], "and the whole thread, should the resume be refused"
    print("ok: a new session is shown the whole thread, built under the lock")


def test_redaction_reaches_every_copy(wg, tmp: Path):
    """Redaction takes the text out of the message, its attachment files, a
    title derived from it, the session log and the Claude session — and a turn
    running meanwhile does not bring the session back."""
    secret = "my PIN is 4711, type it"
    blob = base64.b64encode(b"voice: four seven one one").decode()
    conv = wg._new_conv("user", "reto", None, "user", secret,
                        first_attachments=[{"data": blob, "filename": "memo.ogg",
                                            "content_type": "audio/ogg"}])
    cid = conv["id"]
    stored = wg._load_conv(cid)
    msg = stored["messages"][0]
    att = msg["attachments"][0]
    att_file = wg.CONVERSATION_ATTACHMENTS_DIR / cid / f"{att['id']}{att['suffix']}"
    assert att_file.exists() and stored["title"] == secret
    wg._append_entry(secret, "Typing it.")
    wg._append_entry("something else", "ok")
    sid = "31111111-2222-3333-4444-555555555555"
    transcript = _session_with_transcript(wg, tmp, cid, sid)

    # Redacted from inside a running turn of the thread.
    with wg._conv_pending_lock:
        wg._conv_pending_turns[cid] = 1
    try:
        out, status, why = wg._conv_redact_message(cid, msg["id"])
    finally:
        with wg._conv_pending_lock:
            wg._conv_pending_turns.pop(cid, None)
    assert status == 200, why
    after = wg._load_conv(cid)
    assert after["title"] == wg.REDACTED_TEXT, after["title"]
    assert "4711" not in json.dumps(after), after
    assert not att_file.exists(), "the attachment file goes too"
    log = json.dumps(wg._load_conversation(wg._today()))
    assert "4711" not in log and "something else" in log, log
    assert wg._get_session_entry(f"conv:{cid}") == {} and not transcript.exists()
    # The running turn ends by recording its session: refused once, erased.
    late = tmp / "claude-config" / "projects" / "-workspace" / ("4" * 8 + "-1111-2222-3333-444444444444.jsonl")
    late.write_text("{}\n", encoding="utf-8")
    wg._update_session_entry(f"conv:{cid}", {"session_id": late.stem, "last_activity": 2.0})
    assert wg._get_session_entry(f"conv:{cid}") == {} and not late.exists()
    # The next one records normally.
    wg._update_session_entry(f"conv:{cid}", {"session_id": "fresh-0000", "last_activity": 3.0})
    assert wg._get_session_entry(f"conv:{cid}").get("session_id") == "fresh-0000"
    # A turn running when the redaction landed logs its question afterwards:
    # it is logged redacted.
    wg._append_entry(secret, "Typed, and deleted your message.")
    log = json.dumps(wg._load_conversation(wg._today()))
    assert "4711" not in log, log
    print("ok: redaction reaches the title, attachments, session log and session")


def test_redaction_erases_every_session_the_thread_had(wg, tmp: Path):
    conv = wg._new_conv("user", "reto", "t", "user", "my PIN is 1234")
    cid = conv["id"]
    first = _session_with_transcript(wg, tmp, cid, "99999999-0000-0000-0000-000000000001")
    second = first.parent / "99999999-0000-0000-0000-000000000002.jsonl"
    second.write_text("{}\n", encoding="utf-8")
    # The first session expired and was replaced; it still holds the PIN.
    wg._record_turn_session(f"conv:{cid}", {"session_id": second.stem, "last_activity": 9.0})
    mid = wg._load_conv(cid)["messages"][0]["id"]
    assert wg._conv_redact_message(cid, mid)[1] == 200
    assert not first.exists() and not second.exists(), "both transcripts erased"
    print("ok: redaction erases every session a thread had")


def test_legacy_messages_can_be_redacted(wg):
    """A message stored before messages had ids gets a stable derived one."""
    conv = wg._new_conv("user", "reto", "t", "user", "old one: 0000")
    path = wg.CONVERSATIONS_DIR / f"{conv['id']}.json"
    data = json.loads(path.read_text())
    data["messages"][0].pop("id")
    path.write_text(json.dumps(data))
    c = wg._load_conv(conv["id"])
    derived = wg._conv_message_id(c["messages"][0])
    assert derived == wg._conv_message_id(wg._load_conv(conv["id"])["messages"][0]), "stable"
    assert f"[message id: {derived}]" in wg._conv_render_messages(c, c["messages"])
    out, status, why = wg._conv_redact_message(conv["id"], derived)
    assert status == 200, why
    assert "0000" not in path.read_text()
    print("ok: a legacy message is redacted by its derived id")


def test_sensitive_reply_neutral_title(wg):
    """A title derived from the user's message may carry the very text typed:
    a reply with a sensitive action replaces it, before any push."""
    conv = wg._new_conv("user", "reto", None, "user", "Type my PIN 4711 on the TV",
                        via_edge=True)
    sens = {"kind": "keyboard-type", "id": "e" * 32, "text": "4711", "enter": False,
            "sensitive": True}
    pushed = []
    real_send, real_push = wg.send_message, wg._push_conv_notification
    wg.send_message = lambda *a, **k: {"response": "Typing it.", "reply_shell_actions": [dict(sens)]}
    wg._push_conv_notification = lambda c, text, *a, **k: pushed.append(c.get("title")) or 1
    try:
        with _daemon_environment():
            assert wg._conv_worker(conv["id"], f"conv:{conv['id']}", push=True)
    finally:
        wg.send_message, wg._push_conv_notification = real_send, real_push
    after = wg._load_conv(conv["id"])
    assert after["title"] == wg.SENSITIVE_THREAD_TITLE and "4711" not in after["title"]
    assert pushed == [wg.SENSITIVE_THREAD_TITLE], pushed
    assert after["messages"][-1]["shell_actions"][0]["auto"] is False, "a sensitive text waits for a tap"
    print("ok: a sensitive reply gets a neutral title before it is pushed")



def test_resumed_turn_sees_message_id_and_outcomes(wg):
    """A resumed session gets only the new message — with its id, so Ara can
    offer to redact it, and with what became of her last reply's actions."""
    conv = wg._new_conv("user", "reto", "t", "user", "type hello on the quest")
    action = {"kind": "keyboard-type", "id": "a" * 32, "text": "hello", "enter": False,
              "auto": True}
    wg._conv_add_message(conv["id"], "assistant", "Typing it.", shell_actions_=[action])
    stored, found = wg._conv_record_shell_outcome(
        conv["id"], action["id"], {"state": "error", "error": "not-connected"})
    assert found
    assert not wg._conv_record_shell_outcome(conv["id"], "b" * 32, {"state": "typed"})[1]
    wg._conv_add_message(conv["id"], "user", "my PIN is 4711, type it")
    c = wg._load_conv(conv["id"])
    mid = c["messages"][-1]["id"]
    prompt = wg._conv_engage_prompt(c, True)
    assert prompt.startswith("my PIN is 4711, type it"), prompt
    assert f"[message id: {mid}]" in prompt, prompt
    assert "(not typed: not-connected)" in prompt, prompt
    replay = wg._conv_engage_prompt(c, False)
    assert f"[message id: {mid}]" in replay and "(not typed: not-connected)" in replay
    print("ok: a resumed turn sees the new message's id and the last actions' outcomes")


def test_outcome_purges_and_startup_sweep(wg):
    """A sensitive text reported typed goes at once; at startup, an expired one
    goes from the file and an unexpired one is scheduled."""
    conv = wg._new_conv("user", "reto", "t", "user", "type the code")
    sens = {"kind": "keyboard-type", "id": "e" * 32, "text": "4711", "enter": False,
            "sensitive": True, "auto": True}
    wg._conv_add_message(conv["id"], "assistant", "ok", shell_actions_=[dict(sens)])
    _, found = wg._conv_record_shell_outcome(conv["id"], sens["id"], {"state": "typed"})
    path = wg.CONVERSATIONS_DIR / f"{conv['id']}.json"
    raw = json.loads(path.read_text())
    a = raw["messages"][-1]["shell_actions"][0]
    assert found and a["text"] is None and a["purged"] and a["outcome"]["state"] == "typed", a

    old = wg._new_conv("user", "reto", "t", "user", "x")
    young = wg._new_conv("user", "reto", "t", "user", "y")
    for c, sid in ((old, "1" * 32), (young, "2" * 32)):
        wg._conv_add_message(c["id"], "assistant", "ok", shell_actions_=[dict(sens, id=sid)])
    p_old = wg.CONVERSATIONS_DIR / f"{old['id']}.json"
    data = json.loads(p_old.read_text())
    data["messages"][-1]["ts"] = "2000-01-01T00:00:00+00:00"
    p_old.write_text(json.dumps(data))
    waiting = wg._conv_sensitive_sweep()
    assert "4711" not in p_old.read_text(), "expired: gone from the file at startup"
    assert waiting >= 1, waiting
    assert "4711" in (wg.CONVERSATIONS_DIR / f"{young['id']}.json").read_text(), \
        "young: still there for the phone, deletion scheduled"
    assert wg._CONV_SHELL_OUTCOME_RE.match(f"/conversations/{'a' * 32}/shell-actions/{'b' * 32}/outcome")
    # Two in one thread: the purge of the first schedules the second.
    two = wg._new_conv("user", "reto", "t", "user", "z")
    wg._conv_add_message(two["id"], "assistant", "a", shell_actions_=[dict(sens, id="3" * 32)])
    wg._conv_add_message(two["id"], "assistant", "b", shell_actions_=[dict(sens, id="4" * 32)])
    p_two = wg.CONVERSATIONS_DIR / f"{two['id']}.json"
    data = json.loads(p_two.read_text())
    data["messages"][-2]["ts"] = "2000-01-01T00:00:00+00:00"   # the first, expired
    p_two.write_text(json.dumps(data))
    assert wg._conv_purge_and_reschedule(two["id"]) is True, "the second is still due"
    stored = json.loads(p_two.read_text())["messages"]
    assert stored[-2]["shell_actions"][0]["text"] is None
    assert stored[-1]["shell_actions"][0]["text"] == "4711"
    print("ok: a typed sensitive text goes at once; a restart leaves none behind")


def test_dashboard_turn_below_frontier_gets_the_escalation_flag(wg_tiered):
    """With a frontier tier configured, a router-tier turn is handed the flag;
    the re-run on the frontier tier is not."""
    spawns = _capture_spawns(wg_tiered,
                             json.dumps({"result": "ok", "session_id": "s-2"}))
    with _daemon_environment():
        out = wg_tiered.send_message("hello", session_key="env-tiered")
    assert out.get("response") == "ok", out
    _, kwargs = spawns[0]
    env = kwargs["env"]
    _assert_allowlisted(env)
    assert env["RETINUE_SESSION_MODEL"] == "haiku"
    flag = env["RETINUE_ESCALATE_FILE"]
    assert flag != "/tmp/stale-flag" and "retinue-escalate-" in flag, flag
    # Junior escalates: the same turn is re-run on the frontier tier, with the
    # frontier stamp and no flag.
    spawns.clear()

    def escalate_then_answer(cmd, **kwargs):
        spawns.append((cmd, kwargs))
        if len(spawns) == 1:
            Path(kwargs["env"]["RETINUE_ESCALATE_FILE"]).touch()
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": "senior", "session_id": "s-3"}),
            stderr="")

    wg_tiered.subprocess.run = escalate_then_answer
    with _daemon_environment():
        out = wg_tiered.send_message("harder", session_key="env-tiered-2")
    assert out.get("escalated") is True and out.get("response") == "senior", out
    assert len(spawns) == 2, spawns
    junior_env, senior_env = spawns[0][1]["env"], spawns[1][1]["env"]
    _assert_allowlisted(junior_env)
    _assert_allowlisted(senior_env)
    assert junior_env["RETINUE_SESSION_MODEL"] == "haiku"
    assert "RETINUE_ESCALATE_FILE" in junior_env
    assert senior_env["RETINUE_SESSION_MODEL"] == "opus"
    assert "RETINUE_ESCALATE_FILE" not in senior_env

    # With reply attachments: junior's listed file is discarded with her
    # reply; only the re-run's own manifest counts.
    spawns.clear()

    def attach_escalate_then_answer(cmd, **kwargs):
        spawns.append((cmd, kwargs))
        manifest = Path(kwargs["env"]["RETINUE_REPLY_ATTACHMENTS_FILE"])
        if len(spawns) == 1:
            manifest.write_text("/tmp/junior.png\n", encoding="utf-8")
            Path(kwargs["env"]["RETINUE_ESCALATE_FILE"]).touch()
        else:
            manifest.write_text("/tmp/senior.png\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": "senior", "session_id": "s-6"}),
            stderr="")

    wg_tiered.subprocess.run = attach_escalate_then_answer
    with _daemon_environment():
        out = wg_tiered.send_message("harder", session_key="env-tiered-3",
                                     reply_attachments=True)
    assert out.get("escalated") is True, out
    assert out.get("reply_files") == ["/tmp/senior.png"], out
    junior_m, senior_m = (spawns[0][1]["env"]["RETINUE_REPLY_ATTACHMENTS_FILE"],
                          spawns[1][1]["env"]["RETINUE_REPLY_ATTACHMENTS_FILE"])
    assert junior_m != senior_m, "each spawn needs its own manifest"
    assert not Path(junior_m).exists() and not Path(senior_m).exists()

    # Shell actions likewise: junior's are discarded with her reply.
    spawns.clear()
    junior_action = {"kind": "keyboard-key", "id": "e" * 32, "key": "enter"}
    senior_action = {"kind": "keyboard-key", "id": "f" * 32, "key": "tab"}

    def act_escalate_then_answer(cmd, **kwargs):
        spawns.append((cmd, kwargs))
        manifest = Path(kwargs["env"]["RETINUE_REPLY_SHELL_ACTIONS_FILE"])
        if len(spawns) == 1:
            manifest.write_text(json.dumps(junior_action) + "\n", encoding="utf-8")
            Path(kwargs["env"]["RETINUE_ESCALATE_FILE"]).touch()
        else:
            manifest.write_text(json.dumps(senior_action) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": "senior", "session_id": "s-9"}),
            stderr="")

    wg_tiered.subprocess.run = act_escalate_then_answer
    with _daemon_environment():
        out = wg_tiered.send_message("type", session_key="env-tiered-4",
                                     reply_attachments=True)
    assert out.get("escalated") is True, out
    assert out.get("reply_shell_actions") == [senior_action], out
    for _, kw in spawns:
        assert not Path(kw["env"]["RETINUE_REPLY_SHELL_ACTIONS_FILE"]).exists()


def _run_thread_turn(wg, *, escalated: bool = False, **conv_kwargs) -> str | None:
    """Run one dashboard-thread turn and return the model it asked for."""
    conv = wg._new_conv("user", "reto", "t", "user", "hello", **conv_kwargs)
    if escalated:
        wg._conv_set_flags(conv["id"], escalated=True)
    picked = {}

    def fake_send(prompt, **kwargs):
        picked["model"] = kwargs.get("model")
        return {"response": "ok", "session_id": "s"}

    send, lint, push = wg.send_message, wg._lint_presentation, wg._push_conv_notification
    wg.send_message = fake_send
    wg._lint_presentation = lambda text, **kw: text
    wg._push_conv_notification = lambda *a, **kw: None
    try:
        assert wg._conv_worker(conv["id"], f"conv:{conv['id']}") is True
    finally:
        wg.send_message, wg._lint_presentation, wg._push_conv_notification = send, lint, push
    return picked["model"]


def test_dashboard_model_governs_threads_and_only_threads(wg_dashboard):
    """RETINUE_DASHBOARD_MODEL is the dashboard door's own tier: it governs
    unpinned threads, and nothing else the gateway spawns."""
    # An unpinned thread runs the dashboard model, not the router tier.
    assert _run_thread_turn(wg_dashboard) == "sonnet"
    # A per-thread pin still wins over it.
    assert _run_thread_turn(wg_dashboard, model="opus") == "opus"

    # POST /message — the messenger channels' inbound turns and the async jobs
    # they spawn — defers to send_message(), which keeps the router tier so a
    # cheap-dispatch deployment does not pay dashboard prices for chat traffic.
    spawns = _capture_spawns(wg_dashboard,
                             json.dumps({"result": "ok", "session_id": "s-4"}))
    with _daemon_environment():
        out = wg_dashboard.send_message("hello", session_key="env-message")
    assert out.get("response") == "ok", out
    cmd, kwargs = spawns[0]
    assert "--model" in cmd and "haiku" in cmd, cmd
    assert kwargs["env"]["RETINUE_SESSION_MODEL"] == "haiku"
    # Below the frontier tier, so that path keeps junior's escalation flag.
    assert "RETINUE_ESCALATE_FILE" in kwargs["env"]

    # The picker's "(default)" row names what unpinned threads actually run…
    marked = wg_dashboard._mark_default([{"id": "opus", "label": "Opus"},
                                         {"id": "sonnet", "label": "Sonnet"}])
    assert marked[1].get("default") is True and "default" in marked[1]["label"]
    assert "default" not in marked[0]
    # …and, when the configured model is not offered, gets a row of its own
    # rather than mislabelling another one or leaving the picker to show a
    # bare "Default" that names no model.
    unoffered = wg_dashboard._mark_default([{"id": "opus", "label": "Opus"},
                                            {"id": "haiku", "label": "Haiku"}])
    assert unoffered == [{"id": "sonnet", "label": "sonnet (default)",
                          "default": True},
                         {"id": "opus", "label": "Opus"},
                         {"id": "haiku", "label": "Haiku"}], unoffered

    # The lint's default tier is the router model, untouched by the split.
    assert wg_dashboard.PRESENTATION_LINT_MODEL == "haiku"


def test_escalated_thread_stays_with_senior(wg_dashboard):
    """An escalated thread keeps running on the frontier tier — the dashboard
    default must not take that decision back."""
    assert _run_thread_turn(wg_dashboard, escalated=True) == "opus"


def test_cleanup_and_lint_sessions_are_allowlisted_too(wg):
    """The tool-less helper sessions are `claude` processes all the same."""
    spawns = _capture_spawns(wg, json.dumps({"result": "hello world"}))
    with _daemon_environment():
        assert wg._cleanup_transcript("helo wrld") == "hello world"
    assert len(spawns) == 1, spawns
    _, kwargs = spawns[0]
    _assert_allowlisted(kwargs["env"])
    assert kwargs["env"]["RETINUE_SESSION_MODEL"] == wg.TRANSCRIPT_CLEANUP_MODEL
    assert "--no-session-persistence" in spawns[0][0], "a dictation keeps no transcript"

    spawns.clear()
    text = "Approve or deny the pending send at /sends/signal-gateway/abc when you can."
    wg.subprocess.run = lambda cmd, **kw: (
        spawns.append((cmd, kw)) or subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": text}), stderr=""))
    with _daemon_environment():
        assert wg._lint_presentation(text) == text
    assert len(spawns) == 1, spawns
    _, kwargs = spawns[0]
    _assert_allowlisted(kwargs["env"])
    assert kwargs["env"]["RETINUE_SESSION_MODEL"] == wg.PRESENTATION_LINT_MODEL
    assert "--no-session-persistence" in spawns[0][0]


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wg = _load_gateway(tmp / "a", {})
        assert wg.CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS == 60.0, \
            wg.CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS
        test_absorbs_transient_window(wg)
        test_bounded_by_deadline_not_retry_count(wg)
        test_first_attempt_does_not_sleep(wg)
        test_refreshes_credentials_once_before_the_first_attempt(wg)
        test_dashboard_turn_env_is_allowlisted(wg)
        test_thread_turn_collects_reply_attachments(wg)
        test_thread_turn_collects_shell_actions(wg)
        test_shell_actions_ride_on_the_stored_reply(wg)
        test_sensitive_text_masked_purged_and_never_logged(wg)
        test_redact_a_user_message(wg)
        test_only_a_dashboard_turn_can_type(wg)
        test_sensitive_turn_never_resumable(wg, tmp)
        test_a_new_session_gets_the_whole_thread(wg)
        test_redaction_reaches_every_copy(wg, tmp)
        test_redaction_erases_every_session_the_thread_had(wg, tmp)
        test_legacy_messages_can_be_redacted(wg)
        test_sensitive_reply_neutral_title(wg)
        test_resumed_turn_sees_message_id_and_outcomes(wg)
        test_outcome_purges_and_startup_sweep(wg)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        test_cleanup_and_lint_sessions_are_allowlisted_too(wg)

        wg_short = _load_gateway(
            tmp / "b", {"CLAUDE_SPAWN_ENOENT_DEADLINE_SECONDS": "5"})
        test_deadline_is_env_tunable(wg_short)

        wg_tiered = _load_gateway(
            tmp / "c", {"RETINUE_ROUTER_MODEL": "haiku",
                        "RETINUE_FRONTIER_MODEL": "opus"})
        test_dashboard_turn_below_frontier_gets_the_escalation_flag(wg_tiered)
        test_escalated_junior_sensitive_and_fresh_session(wg_tiered, tmp)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

        # Three distinct tiers, so every assertion below names exactly one.
        wg_dashboard = _load_gateway(
            tmp / "d", {"RETINUE_ROUTER_MODEL": "haiku",
                        "RETINUE_DASHBOARD_MODEL": "sonnet",
                        "RETINUE_FRONTIER_MODEL": "opus"})
        test_dashboard_model_governs_threads_and_only_threads(wg_dashboard)
        test_escalated_thread_stays_with_senior(wg_dashboard)
    print("all web-gateway claude-spawn tests passed")


if __name__ == "__main__":
    main()
