#!/usr/bin/env python3
"""Focused checks for the web gateway's project pages and edit-conversations.

Covers the pure logic behind the new endpoints without running an HTTP server
or a SPARQL store: project-URI -> source-file resolution (including the path
guards), the optimistic-concurrency file write, conversation kind/project
storage and list filtering, the project context injected into Ara's engage
prompt, and the RETINUE_OWNER_ACTOR-driven mine/waiting split _fetch_projects()
feeds the dashboard's projects card.

    python3 tests/test_web_gateway_projects.py
"""
import hashlib
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gateway(tmp: Path):
    """Load scripts/web-gateway.py with sandboxed state/data directories."""
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    # markdown_it is present in the runtime image but not necessarily where the
    # tests run; the module only uses it for the per-day log pages.
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_project_source(wg, chambers: Path):
    proj = chambers / "ops" / "projects" / "roof.md"
    proj.parent.mkdir(parents=True, exist_ok=True)
    proj.write_text("---\nid: roof\ntitle: Fix the roof\n---\nBody\n")

    calls = []

    def fake_bindings(query):
        calls.append(query)
        return [{"g": {"value": "file:ops/projects/roof.md"},
                 "title": {"value": "Fix the roof"}}]

    wg._sparql_bindings = fake_bindings
    rel, full, title = wg._resolve_project_source("urn:retinue:project:roof")
    assert rel == "ops/projects/roof.md", rel
    assert full == proj.resolve(), full
    assert title == "Fix the roof", title
    assert "<urn:retinue:project:roof>" in calls[0]

    # Malformed ids never reach the store (also the SPARQL injection guard).
    for bad in ("", "no-scheme", "urn:retinue:project:x> } ",
                "urn:retinue:evil>", "urn:sp ace", "x" * 600):
        calls.clear()
        assert wg._resolve_project_source(bad) is None, bad
        assert not calls, f"store queried for malformed id {bad!r}"

    # A graph outside the chambers base URI is not a file we may touch.
    wg._sparql_bindings = lambda q: [{"g": {"value": "https://elsewhere/x.md"}}]
    assert wg._resolve_project_source("urn:retinue:project:roof") is None

    # Path traversal via a hostile graph name must be contained.
    wg._sparql_bindings = lambda q: [{"g": {"value": "file:../../etc/passwd"}}]
    assert wg._resolve_project_source("urn:retinue:project:roof") is None

    # Unknown project: no bindings.
    wg._sparql_bindings = lambda q: []
    assert wg._resolve_project_source("urn:retinue:project:ghost") is None
    print("ok: resolve_project_source")


def test_item_payload_and_write(wg, chambers: Path):
    proj = chambers / "ops" / "projects" / "roof.md"
    original = proj.read_text()
    wg._sparql_bindings = lambda q: [{"g": {"value": "file:ops/projects/roof.md"},
                                      "title": {"value": "Fix the roof"}}]
    # Committing is exercised only as "does not blow up" — the temp chamber is
    # not a git repo, and failure there is by design non-fatal.
    item = wg._project_item_payload("urn:retinue:project:roof")
    assert item["markdown"] == original
    assert item["path"] == "ops/projects/roof.md"
    assert item["sha256"] == hashlib.sha256(original.encode()).hexdigest()

    # Stale base_sha -> 409 carrying the current state, file untouched.
    status, body = wg._write_project_file(
        "urn:retinue:project:roof", "new content\n", base_sha="0" * 64)
    assert status == 409, (status, body)
    assert body["markdown"] == original
    assert proj.read_text() == original

    # Matching base_sha -> the write lands.
    status, body = wg._write_project_file(
        "urn:retinue:project:roof", "---\nid: roof\n---\nNew body\n",
        base_sha=item["sha256"])
    assert status == 200, (status, body)
    assert proj.read_text() == "---\nid: roof\n---\nNew body\n"
    assert body["sha256"] == hashlib.sha256(proj.read_bytes()).hexdigest()

    # No base_sha -> unconditional write (used after a deliberate overwrite).
    status, _ = wg._write_project_file("urn:retinue:project:roof", original, None)
    assert status == 200
    assert proj.read_text() == original

    # Oversized content is rejected before touching the file.
    status, _ = wg._write_project_file(
        "urn:retinue:project:roof", "x" * (wg.MAX_PROJECT_FILE_BYTES + 1), None)
    assert status == 413
    assert proj.read_text() == original

    wg._sparql_bindings = lambda q: []
    status, _ = wg._write_project_file("urn:retinue:project:ghost", "x", None)
    assert status == 404
    print("ok: item payload + optimistic write")


def test_conversation_kinds(wg):
    chat = wg._new_conv("user", "Web", None, "user", "hello there")
    edit = wg._new_conv("user", "Web", "Edit: Roof", "user", "mark it paused",
                        kind="edit", project="urn:retinue:project:roof",
                        project_title="Fix the roof")
    linked = wg._new_conv("user", "Web", None, "user", "let's discuss the roof",
                          project="urn:retinue:project:roof",
                          project_title="Fix the roof")

    # Default listing: edit threads are invisible.
    ids = {c["id"] for c in wg._list_convs()}
    assert chat["id"] in ids and linked["id"] in ids and edit["id"] not in ids

    # kind=edit shows only them; kind=all shows everything.
    ids = {c["id"] for c in wg._list_convs("active", "edit")}
    assert ids == {edit["id"]}
    ids = {c["id"] for c in wg._list_convs("active", "all")}
    assert {chat["id"], edit["id"], linked["id"]} <= ids

    # The project filter collects a project's threads across kinds.
    ids = {c["id"] for c in wg._list_convs("active", "all", "urn:retinue:project:roof")}
    assert ids == {edit["id"], linked["id"]}

    # Summaries carry the marker; threads from before this feature (no "kind"
    # key at all) must read as plain chats.
    summary = wg._conv_summary(edit)
    assert summary["kind"] == "edit"
    assert summary["project_title"] == "Fix the roof"
    legacy = dict(chat)
    legacy.pop("kind", None)
    assert wg._conv_summary(legacy)["kind"] == "chat"
    print("ok: conversation kinds + filtering")


def test_engage_prompt_context(wg, chambers: Path):
    wg._sparql_bindings = lambda q: [{"g": {"value": "file:ops/projects/roof.md"},
                                      "title": {"value": "Fix the roof"}}]
    edit = wg._new_conv("user", "Web", "Edit: Roof", "user", "mark it paused",
                        kind="edit", project="urn:retinue:project:roof",
                        project_title="Fix the roof")
    prompt = wg._conv_engage_prompt(edit, fresh=False)
    assert "Fix the roof" in prompt
    assert "ops/projects/roof.md" in prompt
    assert "quick edit command" in prompt
    assert "mark it paused" in prompt

    chat = wg._new_conv("user", "Web", None, "user", "just chatting")
    prompt = wg._conv_engage_prompt(chat, fresh=False)
    assert "quick edit command" not in prompt and "[Context:" not in prompt

    # Life store down: the note degrades to title+id instead of failing the turn.
    def boom(q):
        raise OSError("store down")
    wg._sparql_bindings = boom
    prompt = wg._conv_engage_prompt(edit, fresh=False)
    assert "Fix the roof" in prompt and "source file" not in prompt
    print("ok: engage prompt context")


def test_fetch_projects_owner_binding(tmp: Path):
    """RETINUE_OWNER_ACTOR (PR #224 review, finding 3) drives _fetch_projects()'s
    mine/waiting split for the dashboard's projects card -- previously untested:
    tests/test_project_vocabulary.py only compares the module's vocabulary
    constants and never calls _fetch_projects() or sets this variable.

    `_OWNER_ACTOR` is read once at import time (`os.environ.get(...)` at module
    scope), so each case here loads its own fresh copy of the module -- same
    sandboxing recipe as `_load_gateway` -- with the owner variable set, or
    deliberately absent, *before* the module executes, matching how the real
    process boots.
    """
    def bindings(owner: str, other: str) -> list[dict]:
        return [
            {"p": {"value": "urn:retinue:project:mine"},
             "title": {"value": "Mine"},
             "actor": {"value": owner}},
            {"p": {"value": "urn:retinue:project:theirs"},
             "title": {"value": "Theirs"},
             "actor": {"value": other}},
        ]

    owner = "urn:retinue:actor:owner-fixture"
    someone_else = "urn:retinue:actor:someone-else"

    # Explicit owner set: the project whose currentActor matches lands in
    # "mine", the other in "waiting on <them>".
    os.environ["RETINUE_OWNER_ACTOR"] = owner
    try:
        wg = _load_gateway(tmp / "explicit")
    finally:
        os.environ.pop("RETINUE_OWNER_ACTOR", None)  # never leak into the next load
    assert wg._OWNER_ACTOR == owner, wg._OWNER_ACTOR
    wg._sparql_bindings = lambda q: bindings(owner, someone_else)
    result = wg._fetch_projects()
    assert [i["id"] for i in result["mine"]] == ["urn:retinue:project:mine"], result["mine"]
    assert [i["id"] for i in result["waiting"]] == ["urn:retinue:project:theirs"], result["waiting"]
    assert result["waiting"][0]["waitingOn"] == "Someone Else", result["waiting"]

    # Variable unset: fails safe into "everything is waiting on someone else",
    # per .env.example's own documented contract. The two bindings are
    # unchanged from above -- one of them is even the *same* actor string the
    # explicit-owner case above just matched on -- so this also pins that only
    # an explicit RETINUE_OWNER_ACTOR match ever populates "mine", never the
    # module's own internal fallback constant leaking a false positive.
    wg2 = _load_gateway(tmp / "unset")
    assert wg2._OWNER_ACTOR == "urn:retinue:actor:owner", wg2._OWNER_ACTOR
    wg2._sparql_bindings = lambda q: bindings(owner, someone_else)
    result2 = wg2._fetch_projects()
    assert result2["mine"] == [], result2["mine"]
    assert {i["id"] for i in result2["waiting"]} == {
        "urn:retinue:project:mine", "urn:retinue:project:theirs"}, result2["waiting"]
    print("ok: fetch_projects owner binding (explicit match + unset fail-safe)")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wg = _load_gateway(tmp)
        chambers = tmp / "chambers"
        test_resolve_project_source(wg, chambers)
        test_item_payload_and_write(wg, chambers)
        test_conversation_kinds(wg)
        test_engage_prompt_context(wg, chambers)
        test_fetch_projects_owner_binding(tmp)
    print("all web-gateway project tests passed")


if __name__ == "__main__":
    main()
