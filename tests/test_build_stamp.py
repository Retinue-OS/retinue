#!/usr/bin/env python3
"""Checks for the build stamp — "what code is this container actually running?"

A merge is not a deployment, and twice a stale image has read as a logic bug:
the behaviour the code no longer contains kept happening. `scripts/build_stamp.py`
puts two identifiers on every `/health` — the commit, when the build passes one,
and a digest of the shared framework modules as they were actually baked in —
and the /gateways page compares the second against the dashboard's own.

What these pin down:

- the digest depends on **contents**, not paths, so a gateway's /app copy and
  the retinue container's /workspace/scripts copy of the same module agree —
  which is the whole comparison;
- it is order-independent, and framed so two files cannot be run together into
  a third's contents;
- an incomplete set is `None`, never a hash of the part that was there: a
  partial set would hash to *something*, and that reads as "different code" on
  a container that legitimately carries a subset (the CalDAV gateway carries
  none of them);
- `sha` is read from the environment and is `None` when unset — an image cannot
  know its own provenance unless the build tells it, and guessing is worse;
- **every module the stamp names is copied by every messenger gateway's
  Dockerfile.** Adding one to FRAMEWORK_MODULES without adding the COPY line
  turns each gateway's digest into None — the stamp stops reporting, silently,
  which is precisely the failure it exists to catch.

    python3 tests/test_build_stamp.py
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
GATEWAY_DOCKERFILES = {
    "signal": REPO_ROOT / "signal-gateway" / "Dockerfile",
    "telegram": REPO_ROOT / "telegram-gateway" / "Dockerfile",
    "whatsapp": REPO_ROOT / "whatsapp-gateway" / "Dockerfile",
}
# Every image that reports a build, including the main one.
STAMPED_DOCKERFILES = dict(GATEWAY_DOCKERFILES, retinue=REPO_ROOT / "Dockerfile")
# The line that actually carries a build arg into the running container's
# environment, where build_stamp.build_sha() reads it. An ARG alone does not:
# it exists only during the build, so an image built with --build-arg would
# still report sha: null — silently, and exactly when someone went to the
# trouble of stamping it.
ENV_FROM_ARG = "ENV RETINUE_BUILD_SHA=${RETINUE_BUILD_SHA}"
# Absolute paths the gateways create at *import* time unless redirected. A test
# that forgets one writes outside its temp dir: on a CI runner that fails
# outright (`/models` is not creatable there), while as root it silently
# succeeds — so the suite goes green locally and red in CI, which is how this
# got pushed once already.
IMPORT_TIME_DEFAULT_DIRS = (Path("/models"), Path("/tmp/signal-attachments"))


def _load_web_gateway(tmp: Path):
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_build_stamp_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load():
    spec = importlib.util.spec_from_file_location(
        "build_stamp_under_test", SCRIPTS_DIR / "build_stamp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(directory: Path, names, body=b"x"):
    for name in names:
        (directory / name).write_bytes(body)


def test_same_contents_different_directories_agree():
    """The comparison the /gateways page makes: /app vs /workspace/scripts."""
    bs = _load()
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        for i, name in enumerate(bs.FRAMEWORK_MODULES):
            body = f"# module {i}\n".encode()
            (Path(a) / name).write_bytes(body)
            (Path(b) / name).write_bytes(body)
        assert bs.framework_stamp(a) == bs.framework_stamp(b) is not None, \
            "the same modules under two paths must stamp the same"

        # One byte of one module, and the two builds are told apart.
        (Path(b) / "triage_policy.py").write_bytes(b"# module 6 (changed)\n")
        assert bs.framework_stamp(a) != bs.framework_stamp(b), \
            "a changed module must change the digest"
    print("PASS test_same_contents_different_directories_agree")


def test_an_incomplete_set_is_none_not_a_hash():
    """The CalDAV gateway carries none of these; an older one carries some."""
    bs = _load()
    with tempfile.TemporaryDirectory() as tmp:
        assert bs.framework_stamp(tmp) is None, "no modules at all is None"
        _write(Path(tmp), bs.FRAMEWORK_MODULES[:-1])
        assert bs.framework_stamp(tmp) is None, \
            "a partial set must be None — a hash of the part reads as 'different code'"
        _write(Path(tmp), bs.FRAMEWORK_MODULES[-1:])
        assert bs.framework_stamp(tmp) is not None, "the complete set stamps"
    print("PASS test_an_incomplete_set_is_none_not_a_hash")


def test_the_digest_cannot_be_confused_by_run_together_files():
    """Framing: two modules' contents must not add up to a third's."""
    bs = _load()
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        names = list(bs.FRAMEWORK_MODULES)
        _write(Path(a), names, b"")
        _write(Path(b), names, b"")
        (Path(a) / names[0]).write_bytes(b"ab")
        (Path(a) / names[1]).write_bytes(b"")
        (Path(b) / names[0]).write_bytes(b"a")
        (Path(b) / names[1]).write_bytes(b"b")
        assert bs.framework_stamp(a) != bs.framework_stamp(b)
    print("PASS test_the_digest_cannot_be_confused_by_run_together_files")


def test_sha_is_the_environment_or_none():
    bs = _load()
    before = os.environ.get("RETINUE_BUILD_SHA")
    try:
        os.environ.pop("RETINUE_BUILD_SHA", None)
        assert bs.build_sha() is None, "an unstamped build says so"
        os.environ["RETINUE_BUILD_SHA"] = "  "
        assert bs.build_sha() is None, "whitespace is not a commit"
        os.environ["RETINUE_BUILD_SHA"] = "b5ff537e3237b00dad9cc2cd993f52436a6e7b6c"
        assert bs.build_sha() == "b5ff537e3237b00dad9cc2cd993f52436a6e7b6c"
    finally:
        os.environ.pop("RETINUE_BUILD_SHA", None)
        if before is not None:
            os.environ["RETINUE_BUILD_SHA"] = before
    print("PASS test_sha_is_the_environment_or_none")


def test_build_info_is_json_shaped_and_not_shared():
    """It goes straight into a /health body, and is cached — so hand out copies."""
    bs = _load()
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp), bs.FRAMEWORK_MODULES)
        info = bs.build_info(tmp)
        assert set(info) == {"sha", "framework"}, info
        assert isinstance(info["framework"], str)
        info["framework"] = "tampered"
        assert bs.build_info(tmp)["framework"] != "tampered", \
            "a caller mutating one /health body must not poison the next"
    print("PASS test_build_info_is_json_shaped_and_not_shared")


def test_every_stamped_module_is_actually_copied_into_every_gateway():
    """The guard that keeps the list and the Dockerfiles from drifting apart.

    A module added to FRAMEWORK_MODULES but not to a gateway's Dockerfile makes
    that gateway's digest None: the stamp stops answering, without failing, and
    the next stale image goes unnoticed again.
    """
    bs = _load()
    for name, dockerfile in GATEWAY_DOCKERFILES.items():
        text = dockerfile.read_text(encoding="utf-8")
        copied = set(re.findall(r"^COPY\s+scripts/(\S+\.py)\s", text, re.M))
        missing = [m for m in bs.FRAMEWORK_MODULES if m not in copied]
        assert not missing, (
            f"{name}-gateway/Dockerfile does not COPY {missing} — every module in "
            f"FRAMEWORK_MODULES must be baked into every messenger gateway, or its "
            f"digest is None and the stale-build check goes quiet")
        assert "build_stamp.py" in copied, \
            f"{name}-gateway/Dockerfile must COPY scripts/build_stamp.py"
    print("PASS test_every_stamped_module_is_actually_copied_into_every_gateway")


def test_every_image_turns_the_build_arg_into_an_environment_variable():
    """ARG is build-time only; ENV is what the process can read.

    Without the ENV line an image built with --build-arg reports sha: null
    anyway — the one case where someone did stamp the build and the stamp is
    thrown away.
    """
    for name, dockerfile in STAMPED_DOCKERFILES.items():
        text = dockerfile.read_text(encoding="utf-8")
        assert "ARG RETINUE_BUILD_SHA" in text, f"{name}: no ARG RETINUE_BUILD_SHA"
        assert ENV_FROM_ARG in text, (
            f"{name}: the Dockerfile must carry the ARG into the environment as "
            f"`{ENV_FROM_ARG}`, or the build sha never reaches /health")
    print("PASS test_every_image_turns_the_build_arg_into_an_environment_variable")


def test_compose_forwards_the_sha_to_every_stamped_image():
    """`docker compose build` passes nothing it was not told to pass."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    try:
        import yaml  # noqa: PLC0415 - optional; the check degrades to a text scan
    except ImportError:
        assert compose.count("RETINUE_BUILD_SHA: ${RETINUE_BUILD_SHA:-}") == 4, \
            "all four stamped services must forward the build arg"
        print("PASS test_compose_forwards_the_sha_to_every_stamped_image (text scan)")
        return
    services = yaml.safe_load(compose)["services"]
    for service in ("retinue", "signal-gateway", "telegram-gateway", "whatsapp-gateway"):
        build = services[service].get("build")
        assert isinstance(build, dict), f"{service}: build must be a mapping to carry args"
        assert build.get("args", {}).get("RETINUE_BUILD_SHA") == "${RETINUE_BUILD_SHA:-}", \
            (f"{service}: compose must forward RETINUE_BUILD_SHA (with the `:-` default, "
             f"so an unset variable is an empty stamp and not a compose error)")
    print("PASS test_compose_forwards_the_sha_to_every_stamped_image")


def test_the_repo_itself_stamps():
    """Run against this checkout — the same call an image makes against /app."""
    bs = _load()
    stamp = bs.framework_stamp(SCRIPTS_DIR)
    assert stamp and len(stamp) == bs.DIGEST_CHARS, stamp
    assert stamp == bs.framework_stamp(), "the default is this module's own directory"
    print("PASS test_the_repo_itself_stamps")


def _load_gateway(name: str, tmp: Path):
    """Load one messenger gateway module, with just enough stubbed to import.

    langdetect is a real dependency of the Signal gateway and is not installed
    everywhere this suite runs; the health snapshot does not use it.
    """
    if "langdetect" not in sys.modules:
        try:
            import langdetect  # noqa: F401,PLC0415
        except ImportError:
            stub = types.ModuleType("langdetect")
            stub.detect = lambda text: "en"
            stub.detect_langs = lambda text: []
            stub.LangDetectException = type("LangDetectException", (Exception,), {})
            sys.modules["langdetect"] = stub
    upper = name.upper()
    os.environ[f"{upper}_SEND_POLICY"] = "[]"
    os.environ[f"{upper}_ACCOUNT"] = "+15551234567"
    os.environ[f"{upper}_PENDING_SENDS_DIR"] = str(tmp / "pending")
    os.environ[f"{upper}_DATA_DIR"] = str(tmp / "data")
    os.environ[f"{upper}_TMP_DIR"] = str(tmp / "tmp")
    # These two default to absolute paths (`/models`, `/tmp/signal-attachments`)
    # and are created at *import* time, so they must be redirected before the
    # module is loaded or the import writes outside the temp dir — which fails
    # outright on a CI runner that may not create `/models`, and silently
    # succeeds as root, hiding the problem locally. They do not follow the
    # `{CHANNEL}_` naming the loop above covers, so they are set by name.
    os.environ["PIPER_DATA_DIR"] = str(tmp / "models")
    os.environ["SIGNAL_ATTACHMENTS_DIR"] = str(tmp / "attachments")
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        f"{name}_gateway_build_stamp_under_test", SCRIPTS_DIR / f"{name}-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_messenger_gateway_publishes_its_build_on_health():
    """The wiring, not just the helper.

    `build_stamp` can be perfect and the mechanism still dead: all it takes is
    one gateway whose `_health_snapshot()` does not carry the block. Nothing
    else would fail — the digest would simply never be reported, which is the
    silence this whole change exists to end. So load each gateway for real and
    read what it would actually serve.
    """
    escaped_before = {d for d in IMPORT_TIME_DEFAULT_DIRS if d.exists()}
    stamps = {}
    for name in ("signal", "telegram", "whatsapp"):
        with tempfile.TemporaryDirectory() as raw:
            gw = _load_gateway(name, Path(raw))
            snapshot = gw._health_snapshot()
            assert "build" in snapshot, \
                f"{name}-gateway's /health carries no build block"
            build = snapshot["build"]
            assert set(build) == {"sha", "framework"}, (name, build)
            assert build["framework"], \
                f"{name}-gateway reports no framework digest from a full checkout"
            json.dumps(snapshot)  # it has to survive being a JSON body
            stamps[name] = build["framework"]
    assert len(set(stamps.values())) == 1, \
        f"the gateways bake the same modules and must agree: {stamps}"
    escaped = {d for d in IMPORT_TIME_DEFAULT_DIRS if d.exists()} - escaped_before
    assert not escaped, (
        f"loading a gateway created {sorted(map(str, escaped))} outside the temp "
        f"dir — redirect it in _load_gateway. Running as root this only leaves a "
        f"stray directory; on a CI runner it is a PermissionError and a red suite")
    print("PASS test_every_messenger_gateway_publishes_its_build_on_health")


def test_the_gateways_page_names_a_stale_gateway():
    """The five-second check: a connected gateway on older code says so.

    The case that misleads is not a gateway that is down — it is one that is
    up, answering, serving behaviour the merged code no longer contains. So the
    note has to appear on a *connected* card, and it has to appear only on a
    real disagreement: a gateway that reports no digest (older build, or the
    CalDAV gateway, which bakes none of these modules) is "cannot say", and a
    warning nobody can act on is worse than a quiet card.
    """
    with tempfile.TemporaryDirectory() as raw:
        wg = _load_web_gateway(Path(raw))
        mine = wg.build_stamp.framework_stamp()
        assert mine, "the checkout under test must itself stamp"

        up = {"configured": True, "connected": True, "mode": "inbox"}
        assert wg._stale_build_note(dict(up, build={"framework": mine})) is None, \
            "the same build is not a warning"
        assert wg._stale_build_note(up) is None, \
            "a gateway that predates the field is 'cannot say', not 'stale'"
        assert wg._stale_build_note(dict(up, build={"framework": None})) is None, \
            "no digest (e.g. the CalDAV gateway) is 'cannot say' too"
        note = wg._stale_build_note(dict(up, build={"framework": "0" * 12}))
        assert note and "self-update" in note, note
        assert "0" * 12 in note and mine in note, \
            "name both builds, so the difference can be checked rather than believed"

        # And it reaches the page, on a card that looks perfectly healthy.
        html_out = wg._render_gateways_html([
            {"slug": "signal", "label": "Signal",
             "health": dict(up, build={"framework": "0" * 12})},
            {"slug": "telegram", "label": "Telegram",
             "health": dict(up, build={"framework": mine})},
        ])
        # The class also appears once in the stylesheet — count the cards.
        assert html_out.count('class="meta gw-stale"') == 1, \
            "exactly the stale gateway is flagged, and it is flagged while connected"
        assert "connected</span>" in html_out
    print("PASS test_the_gateways_page_names_a_stale_gateway")


def test_the_dashboard_publishes_its_own_build():
    """Its /health carries the digest the gateways are compared against."""
    with tempfile.TemporaryDirectory() as raw:
        wg = _load_web_gateway(Path(raw))
        info = wg.build_stamp.build_info()
        assert set(info) == {"sha", "framework"} and info["framework"], info
    print("PASS test_the_dashboard_publishes_its_own_build")


def main():
    test_same_contents_different_directories_agree()
    test_an_incomplete_set_is_none_not_a_hash()
    test_the_digest_cannot_be_confused_by_run_together_files()
    test_sha_is_the_environment_or_none()
    test_build_info_is_json_shaped_and_not_shared()
    test_every_stamped_module_is_actually_copied_into_every_gateway()
    test_every_image_turns_the_build_arg_into_an_environment_variable()
    test_compose_forwards_the_sha_to_every_stamped_image()
    test_the_repo_itself_stamps()
    test_every_messenger_gateway_publishes_its_build_on_health()
    test_the_gateways_page_names_a_stale_gateway()
    test_the_dashboard_publishes_its_own_build()
    print("all build-stamp tests passed")


if __name__ == "__main__":
    sys.exit(main())
