#!/usr/bin/env python3
"""Checks for the mail gateway's deployment plumbing.

The mail gateway (docs/mail-chats.md, "The mail gateway") is a compose service,
an image and a volume shared three ways. Each of those is a place where one
forgotten line breaks a fresh deployment and nothing else: a volume the
qlever-life container cannot mount, a module the image does not carry, a
policy file the two sides look for under different names. What these pin:

- the service names every variable it reads and loads no env_file (it holds a
  mailbox password; .env holds every other secret too), names the send policy
  and the erase capability it needs to write (and the approval-link variables
  a queued send builds its URL from) but no setting or credential it does not
  read (the news rail's), runs without a profile, and mounts its ledger and
  its state volume;
- the retinue entrypoint places the e-mail policy file before anything slow,
  since the mirror does not start without it;
- the ledger volume is mounted by the retinue service and read-only by
  qlever-life, and volumes-init creates its mountpoint first;
- the retinue service enrols the gateway and keeps the mailbox credentials
  its own e-mail backend still uses;
- the e-mail policy path the gateway is told is the very file the retinue
  side writes by default, seen through the other mount;
- the image copies every local module its code imports, lazily imported
  ones included.

Text-based, so it runs without PyYAML; with PyYAML the service's environment
is also checked for duplicate names.

    python3 tests/test_mail_gateway_deploy.py
"""
import importlib.util
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
DOCKERFILE = (REPO_ROOT / "mail-gateway" / "Dockerfile").read_text(encoding="utf-8")


def _service_block(name: str) -> str:
    """The lines of one service in docker-compose.yml, comments included."""
    lines = COMPOSE.splitlines()
    start = lines.index(f"  {name}:")
    out = []
    for line in lines[start + 1:]:
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line) or re.match(r"^\S", line):
            break
        out.append(line)
    return "\n".join(out)


def _entries(block: str) -> set[str]:
    """Every `- item` line of a block, comments dropped."""
    return {m.group(1).strip() for m in re.finditer(r"^\s+- (?!#)(.+)$", block, re.M)}


def _env_names(block: str) -> set[str]:
    return {e.split("=", 1)[0] for e in _entries(block) if re.match(r"^[A-Z][A-Z0-9_]*(=|$)", e)}


_ENV_READ_RE = re.compile(
    r"(?:os\.environ\.get|os\.getenv|_env_int|_env_float)\(\s*\"([A-Z][A-Z0-9_]*)\"")


def _names_read() -> set[str]:
    """The variables the gateway's own code reads: mail-gateway.py and the
    trim it runs (the shared modules' rails are checked by name above)."""
    out = set()
    for name in ("mail-gateway.py", "mail_trim.py"):
        out |= set(_ENV_READ_RE.findall((SCRIPTS_DIR / name).read_text(encoding="utf-8")))
    return out


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_service_names_what_it_reads_and_nothing_else():
    block = _service_block("mail-gateway")
    keys = {m.group(1) for m in re.finditer(r"^    ([a-z_]+):", block, re.M)}
    assert "env_file" not in keys, "the mail gateway must not load .env wholesale"
    assert "profiles" not in keys, \
        "an unconfigured mail gateway idles with configured: false; it runs everywhere"
    names = _env_names(block)
    # The mailbox, as email_client.Config reads it, and the interface's tunables.
    mailbox = {"EMAIL_USER", "EMAIL_PASS", "EMAIL_FROM_NAME", "IMAP_HOST", "IMAP_PORT",
               "SMTP_HOST", "SMTP_PORT", "SENT_FOLDER", "SMTP_SAVE_SENT"}
    tunables = {"MAIL_ACCOUNT_NAME", "MAIL_OWN_ADDRESSES", "MAIL_ARCHIVE_FOLDER",
                "MAIL_TRASH_FOLDER", "MAIL_POLL_SECONDS", "MAIL_BACKFILL_DAYS",
                "MAIL_BACKFILL_BATCH", "MAIL_BACKFILL_PAUSE_SECONDS", "MAIL_IMAP_TIMEOUT",
                "MAIL_MAX_PART_BYTES", "MAIL_MAX_MAIL_BYTES", "MAIL_TRIM_WANTING_CHARS",
                "MAIL_DATA_DIR", "MAIL_GATEWAY_HTTP_PORT", "MAIL_GATEWAY_TOKEN",
                "MAIL_POLICY_WAIT_SECONDS", "MAIL_RECENT_CHATS_MAX", "MAIL_RAIL_TIMEOUT",
                # Writing: how long its own send waits for the Sent copy.
                "MAIL_SENT_GRACE_SECONDS",
                # The default account idles where the channel set leaves out email.
                "MESSENGER_BUILTIN_CHANNELS"}
    rails = {"CHATS_INGEST_URL", "CHATS_INGEST_TOKEN",
             "INBOUND_STORE_DIR", "TRIAGE_EMAIL_WHITELIST_PATH"}
    # The send policy it applies, the approval link a queued send returns, and
    # the capability the user's delete presents.
    writing = {"EMAIL_SEND_POLICY", "SEND_APPROVAL_BASE_URL", "SEND_APPROVAL_SLUG",
               "CHAT_ERASE_TOKEN"}
    missing = (mailbox | tunables | rails | writing) - names
    assert not missing, f"mail-gateway does not name {sorted(missing)}"
    entries = _entries(block)
    for fixed in ("MAIL_GATEWAY_HTTP_PORT=8096", "INBOUND_STORE_DIR=/inbound",
                  "CHATS_INGEST_URL=http://retinue:8080/internal/chats/inbound",
                  "mail-data:/root/.local/share/mail", "messenger-email:/inbound"):
        assert fixed in entries, f"mail-gateway: expected `{fixed}`"
    # Secrets it has no use for stay out — and so does the one a later phase
    # reads: it files no news yet (e-mail triage still does). The container
    # that parses mail from strangers holds no credential it has no use for.
    for foreign in ("LITELLM_MASTER_KEY", "OPENROUTER_API_KEY", "CALDAV_PASSWORD",
                    "SMS_SERVER_PASSWORD", "TELEGRAM_API_HASH",
                    "NEWS_INGEST_URL", "NEWS_INGEST_TOKEN"):
        assert foreign not in names, foreign
    # Both ways for the gateway's own settings: every MAIL_* it names is one
    # the gateway's code reads (a value-less name the code ignores would be a
    # setting that silently does nothing), and every variable the gateway's
    # own code reads is named (a value in .env would otherwise never arrive).
    read = _names_read()
    dead = {n for n in names if n.startswith("MAIL_")} - read
    assert not dead, f"mail-gateway names {sorted(dead)}, which nothing in it reads"
    # TRIAGE_MESSENGER_DIR is the alternative to TRIAGE_EMAIL_WHITELIST_PATH,
    # which compose sets.
    unwired = read - names - {"TRIAGE_MESSENGER_DIR"}
    assert not unwired, f"the mail gateway reads {sorted(unwired)}, which compose does not pass"
    try:
        import yaml  # noqa: PLC0415 - optional
    except ImportError:
        print("PASS test_the_service_names_what_it_reads_and_nothing_else (text scan)")
        return
    env = yaml.safe_load(COMPOSE)["services"]["mail-gateway"]["environment"]
    keys = [e.split("=", 1)[0] for e in env]
    assert len(keys) == len(set(keys)), f"duplicate variables: {keys}"
    print("PASS test_the_service_names_what_it_reads_and_nothing_else")


def test_the_ledger_volume_is_shared_three_ways():
    retinue = _entries(_service_block("retinue"))
    assert "messenger-email:/workspace/chambers/_generated/messenger/email" in retinue
    qlever = _entries(_service_block("qlever-life"))
    assert "messenger-email:/data/_generated/messenger/email:ro" in qlever, \
        "qlever-life indexes the ledger read-only"
    init = _entries(_service_block("volumes-init"))
    assert "/chambers/_generated/messenger/email" in init, (
        "volumes-init must create the mountpoint, or a fresh chambers volume "
        "cannot take the nested mount under qlever-life's read-only /data")
    top = COMPOSE.split("\nvolumes:\n", 1)[1].split("\nnetworks:\n", 1)[0]
    for volume in ("mail-data", "messenger-email"):
        assert re.search(rf"^  {volume}:\s*$", top, re.M), f"volume {volume} is not declared"
    print("PASS test_the_ledger_volume_is_shared_three_ways")


def test_the_retinue_side_enrols_it_and_keeps_its_own_credentials():
    block = _service_block("retinue")
    entries = _entries(block)
    assert "MAIL_GATEWAY_BASE_URL=http://mail-gateway:8096" in entries
    assert "MAIL_GATEWAY_SEND_URL=http://mail-gateway:8096/send" in entries
    assert "MAIL_GATEWAY_TOKEN" in entries
    no_proxy = next(e for e in entries if e.startswith("NO_PROXY="))
    assert "mail-gateway" in no_proxy.split("=", 1)[1].split(","), \
        "retinue → mail-gateway must bypass the egress proxy"
    # email_client.py's backend still runs in this container.
    for name in ("EMAIL_USER", "EMAIL_PASS", "IMAP_HOST", "SMTP_HOST"):
        assert name in entries, f"retinue lost {name}; its e-mail backend needs it"
    # The user's delete presents the erase capability to the gateway, and the
    # e-mail backend applies the same send policy the gateway does.
    for name in ("CHAT_ERASE_TOKEN", "EMAIL_SEND_POLICY", "SEND_APPROVAL_BASE_URL"):
        assert name in entries, f"retinue does not name {name}"
    # The base URL the registry enrols, and the port the gateway listens on.
    sys.path.insert(0, str(SCRIPTS_DIR))
    mg = _load("messenger_gateways_deploy_under_test", SCRIPTS_DIR / "messenger_gateways.py")
    saved = {k: os.environ.pop(k, None) for k in
             ("MAIL_GATEWAY_BASE_URL", "MESSENGER_BUILTIN_CHANNELS", "MESSENGER_GATEWAYS",
              "SIGNAL_GATEWAY_BASE_URL", "WHATSAPP_GATEWAY_BASE_URL",
              "TELEGRAM_GATEWAY_BASE_URL", "SMS_GATEWAY_BASE_URL")}
    try:
        os.environ["MAIL_GATEWAY_BASE_URL"] = "http://mail-gateway:8096"
        registry = mg.channel_gateways("[test]")
    finally:
        for key, value in saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
    assert registry["mail-gateway"]["channel"] == "email", registry
    print("PASS test_the_retinue_side_enrols_it_and_keeps_its_own_credentials")


# What the retinue-side code reads for the mail channel: the web gateway's
# hop, queue reads and hook memory, mail-push.py's timeout. The retinue service
# has no env_file, so a variable it does not name never reaches those
# processes — a value in .env would do nothing.
_RETINUE_SIDE_MAIL = {"MAIL_SEND_TIMEOUT", "CHAT_PENDING_TIMEOUT",
                      "CHAT_PENDING_RETRY_SECONDS", "CHAT_PENDING_SYNC_SECONDS",
                      "MAILBOX_NONE_SECONDS", "MAIL_GATEWAY_TIMEOUT",
                      "MAIL_ORIGINAL_MAX_BYTES", "MAIL_ORIGINAL_INLINE_MAX_BYTES"}


def test_the_retinue_service_passes_the_mail_tunables_its_code_reads():
    names = _env_names(_service_block("retinue"))
    missing = _RETINUE_SIDE_MAIL - names
    assert not missing, f"retinue does not name {sorted(missing)}"
    # Whatever else the retinue-side mail code reads is named too: a new
    # tunable cannot be documented without being wired.
    pattern = re.compile(
        r"(?:os\.environ\.get|os\.getenv)\(\s*\"((?:MAIL_|MAILBOX_|CHAT_PENDING_)[A-Z0-9_]*)\"")
    read = set()
    for name in ("web-gateway.py", "mail-push.py", "email_client.py"):
        read |= set(pattern.findall((SCRIPTS_DIR / name).read_text(encoding="utf-8")))
    unwired = {n for n in read if n not in names}
    assert not unwired, f"the retinue side reads {sorted(unwired)}, which compose does not pass"
    # A session may need the push CLI's timeout; the web gateway's own knobs
    # stay out of sessions (scripts/session_env.py).
    sys.path.insert(0, str(SCRIPTS_DIR))
    se = _load("session_env_deploy_under_test", SCRIPTS_DIR / "session_env.py")
    built = se.build({"PATH": "/usr/bin", "MAIL_GATEWAY_TIMEOUT": "300",
                      "MAIL_SEND_TIMEOUT": "300", "CHAT_PENDING_SYNC_SECONDS": "5",
                      "MAILBOX_NONE_SECONDS": "5"})
    assert built.get("MAIL_GATEWAY_TIMEOUT") == "300", built
    for daemon_only in ("MAIL_SEND_TIMEOUT", "CHAT_PENDING_SYNC_SECONDS", "MAILBOX_NONE_SECONDS"):
        assert daemon_only not in built, daemon_only
    print("PASS test_the_retinue_service_passes_the_mail_tunables_its_code_reads")


def test_both_sides_name_the_same_policy_file():
    """The retinue side writes the e-mail policy at its default path under the
    ledger mount; the gateway reads the path compose gives it under its own.
    Mapped through the two mounts, they must be one file."""
    tp = _load("triage_policy_deploy_under_test", SCRIPTS_DIR / "triage_policy.py")
    saved = (tp.CHAMBERS_DIR, {k: os.environ.pop(k, None) for k in
                               ("TRIAGE_MESSENGER_DIR", "TRIAGE_EMAIL_WHITELIST_PATH")})
    try:
        tp.CHAMBERS_DIR = Path("/workspace/chambers")  # the retinue image's default
        retinue_side = tp.email_whitelist_path()
    finally:
        tp.CHAMBERS_DIR = saved[0]
        for key, value in saved[1].items():
            if value is not None:
                os.environ[key] = value
    retinue_mount = Path("/workspace/chambers/_generated/messenger/email")
    gateway_side = next(e.split("=", 1)[1] for e in _entries(_service_block("mail-gateway"))
                        if e.startswith("TRIAGE_EMAIL_WHITELIST_PATH="))
    assert Path("/inbound") / retinue_side.relative_to(retinue_mount) == Path(gateway_side), \
        (retinue_side, gateway_side)
    print("PASS test_both_sides_name_the_same_policy_file")


_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)


def test_the_image_carries_every_module_it_imports():
    """A module imported lazily (email_client imports triage_policy inside a
    function) fails only when that path first runs, in production. So the
    closure is computed over every import statement, at any indentation."""
    copied = dict(re.findall(r"^COPY\s+scripts/(\S+\.py)\s+/app/(\S+\.py)\s*$", DOCKERFILE, re.M))
    assert all(src == dst for src, dst in copied.items()), copied
    assert re.search(r'^CMD \["python3", "/app/mail-gateway.py"\]$', DOCKERFILE, re.M)
    local = {p.stem for p in SCRIPTS_DIR.glob("*.py")}
    todo = [name for name in copied if (SCRIPTS_DIR / name).exists()]
    if "mail-gateway.py" not in todo:
        print("  note: scripts/mail-gateway.py is not in this checkout; "
              "checking the closure of the modules the image copies")
    seen, missing = set(), set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        text = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
        for module in set(_IMPORT_RE.findall(text)) & local:
            file = f"{module}.py"
            if file not in copied:
                missing.add(f"{file} (imported by {name})")
            elif file not in seen:
                todo.append(file)
    assert not missing, f"mail-gateway/Dockerfile does not COPY {sorted(missing)}"
    lines = DOCKERFILE.splitlines()
    last_copy = max(i for i, l in enumerate(lines) if l.startswith("COPY "))
    env = lines.index("ENV RETINUE_BUILD_SHA=${RETINUE_BUILD_SHA}")
    assert env > last_copy, "the build sha comes last, or every commit rebuilds the COPY layers"
    print("PASS test_the_image_carries_every_module_it_imports")


def test_the_entrypoint_places_the_policy_file_before_anything_slow():
    """The mirror does not start while the e-mail policy file is absent, and
    a backfill without the `news` flags would make every newsletter of the
    window a chat for good. So the retinue side puts the file in place first
    thing — before the chamber clones, the plugin installs, the pip installs
    and the web gateway (which places it too, but late)."""
    lines = [l for l in (SCRIPTS_DIR / "entrypoint.sh").read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.lstrip().startswith("#")]

    def first(pattern: str) -> int:
        hit = next((i for i, l in enumerate(lines) if re.search(pattern, l)), None)
        assert hit is not None, f"entrypoint.sh: no line matches {pattern!r}"
        return hit

    place = first(r"triage_policy\.py place-email-policy")
    for slow in (r"git clone", r"sync-plugins\.py", r"pip install", r"web-gateway\.py &"):
        assert place < first(slow), f"the policy file must be placed before `{slow}`"
    # Best effort: a failure warns and the boot goes on (set -e is in force).
    assert re.search(r"\|\|\s*echo", lines[place + 1] + lines[place + 2]), \
        lines[place:place + 3]
    tp = _load("triage_policy_cli_under_test", SCRIPTS_DIR / "triage_policy.py")
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as tmp:
        saved = os.environ.get("TRIAGE_EMAIL_WHITELIST_PATH")
        os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(Path(tmp) / "policy" / "email-policy.nt")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                assert tp.main(["place-email-policy"]) == 0
        finally:
            if saved is None:
                os.environ.pop("TRIAGE_EMAIL_WHITELIST_PATH", None)
            else:
                os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = saved
        assert (Path(tmp) / "policy" / "email-policy.nt").read_bytes() == b""
    print("PASS test_the_entrypoint_places_the_policy_file_before_anything_slow")


def main() -> int:
    test_the_service_names_what_it_reads_and_nothing_else()
    test_the_entrypoint_places_the_policy_file_before_anything_slow()
    test_the_ledger_volume_is_shared_three_ways()
    test_the_retinue_side_enrols_it_and_keeps_its_own_credentials()
    test_the_retinue_service_passes_the_mail_tunables_its_code_reads()
    test_both_sides_name_the_same_policy_file()
    test_the_image_carries_every_module_it_imports()
    print("all mail-gateway deployment tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
