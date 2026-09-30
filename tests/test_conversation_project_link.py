#!/usr/bin/env python3
"""Checks that an agent can link a dashboard thread to its project.

The project page lists only threads that carry a `project` link, and agents
opened most threads without one — the page stayed empty although discussions
about the project were running. Covers:

  * web-gateway: POST /internal/conversations/<id>/flags accepts
    {project, project_title}, and the thread then shows up in the project's
    listing (_list_convs(..., project=...)); a malformed project is rejected.
  * conversation-push.py: --thread <id> --project <uri> is a flags-only call
    to /flags carrying the link; combining it with a message is refused.

    python3 tests/test_conversation_project_link.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_reply_token_handoff import (  # noqa: E402
    _fake_agent_request, _load_conversation_push, _load_web_gateway,
    _run_cli_capturing_request,
)

PID = "urn:retinue:project:saeule-3a"


def test_gateway_links_existing_thread():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_web_gateway(Path(tmp))
        conv = wg._new_conv("agent", "Web", None, "user", "Unterlagen fehlen noch")
        assert wg._list_convs("all", "chat", PID) == []
        fake = _fake_agent_request(wg, {"project": PID, "project_title": "Säule 3a"})
        wg.Handler._handle_agent_conversation_flags(fake, conv["id"])
        assert fake.status == 200, (fake.status, fake.response_body)
        assert fake.response_body["project"] == PID
        listed = wg._list_convs("all", "chat", PID)
        assert [t["id"] for t in listed] == [conv["id"]], listed
        assert listed[0]["project_title"] == "Säule 3a"

        bad = _fake_agent_request(wg, {"project": "  "})
        wg.Handler._handle_agent_conversation_flags(bad, conv["id"])
        assert bad.status == 400, bad.status
        assert wg._load_conv(conv["id"])["project"] == PID
    print("ok: the flags endpoint links an existing thread to its project")


def test_cli_links_existing_thread():
    mod = _load_conversation_push()
    tid = "d" * 32
    code, req = _run_cli_capturing_request(
        mod, ["--thread", tid, "--project", PID, "--project-title", "Säule 3a"])
    assert code == 0, code
    assert req.full_url.endswith(f"/{tid}/flags"), req.full_url
    assert json.loads(req.data.decode("utf-8")) == {
        "project": PID, "project_title": "Säule 3a"}

    code, req = _run_cli_capturing_request(
        mod, ["--thread", tid, "--project", PID, "a message"])
    assert code == 2 and req is None, (code, req)

    code, req = _run_cli_capturing_request(mod, ["--project", PID, "new thread"])
    assert code == 0, code
    body = json.loads(req.data.decode("utf-8"))
    assert body["project"] == PID and body["message"] == "new thread", body
    print("ok: conversation-push --thread --project links an existing thread")


def main():
    test_gateway_links_existing_thread()
    test_cli_links_existing_thread()
    print("\nAll project-link checks passed.")


if __name__ == "__main__":
    main()
