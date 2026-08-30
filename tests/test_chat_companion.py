#!/usr/bin/env python3
"""Checks for a messenger chat's companion thread — the conversation where the
user works out a reply with Ara.

The companion is deliberately an *ordinary* conversation: the dashboard drives
it through /conversations, so almost nothing new exists on that side. What is
new is the link (a `chat` field and kind "companion"), the create-or-get that
keeps exactly one per chat, and the context block that tells Ara which chat a
turn is about and what to do with her answer.

Covers: creation is idempotent and recorded in chat state; a recorded id whose
thread is gone is replaced rather than handed back; the engage prompt carries
the chat's name, its recent messages with who spoke, the shared draft, the
draft-don't-send instruction and the persona rule; the message excerpt is
capped and says so; a store outage degrades honestly; a non-companion thread is
untouched; a group chat is named as one; companion threads stay out of the
default conversation listing; and
Ara's reply in one lands like any other — unread, with a Web Push.

    python3 tests/test_chat_companion.py
"""
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

CHAT = "signal:+41794456312"
NAME = "Mara"


def _load_gateway(tmp: Path):
    """Load scripts/web-gateway.py with sandboxed state, as the sibling
    web-gateway tests do."""
    for var in ("RETINUE_CONVERSATION_MODELS", "RETINUE_LITELLM_URL",
                "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"):
        os.environ.pop(var, None)
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["CHAT_STATE_DIR"] = str(tmp / "chat-state")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["CHAT_COMPANION_CONTEXT_MESSAGES"] = "5"
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_companion_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _msg(direction, text, ts, **kw):
    m = {"id": ts, "chat": CHAT, "direction": direction, "text": text, "ts": ts}
    m.update(kw)
    return m


# One exchange covering every speaker the ledger distinguishes.
MESSAGES = [
    _msg("in", "Kommst du am Samstag?", "2026-08-27T07:00:00Z",
         sender="+41794456312", sender_name=NAME),
    _msg("out", "Ich schau mal", "2026-08-27T07:05:00Z", author="device"),
    _msg("out", "Bis Freitag sage ich Bescheid", "2026-08-27T07:06:00Z",
         author="user"),
    _msg("out", "Termin bestätigt.", "2026-08-27T07:07:00Z", author="agent",
         agent="Coach"),
    _msg("in", "", "2026-08-27T07:12:00Z", sender="+41794456312",
         sender_name=NAME, attachments=[{"id": "a" * 32, "url": "/x"}]),
]


def _serve(wg, messages):
    wg._chat_messages_payload = lambda cid, before=None: {"messages": messages}


def test_create_is_idempotent_and_stored(wg):
    _serve(wg, MESSAGES)
    cid, created = wg._chat_companion(CHAT)
    assert created is True and cid
    assert wg._CHAT_STATE.get(CHAT)["companion"] == cid, "not recorded on the chat"
    again, created2 = wg._chat_companion(CHAT)
    assert again == cid and created2 is False, "second call created a second thread"

    conv = wg._load_conv(cid)
    assert conv["kind"] == "companion"
    assert conv["chat"] == CHAT, "the thread does not point back at its chat"
    # Opening the pane must not badge the thread the user just opened.
    assert conv["unread"] is False
    summary = wg._conv_summary(conv)
    assert summary["kind"] == "companion" and summary["chat"] == CHAT
    print("PASS test_create_is_idempotent_and_stored")


def test_deleted_thread_is_replaced(wg):
    _serve(wg, MESSAGES)
    other = "signal:+41780000000"
    cid, _ = wg._chat_companion(other)
    (Path(os.environ["CONVERSATIONS_DIR"]) / f"{cid}.json").unlink()
    fresh, created = wg._chat_companion(other)
    assert created is True and fresh != cid, "handed back an unreadable id"
    assert wg._CHAT_STATE.get(other)["companion"] == fresh
    print("PASS test_deleted_thread_is_replaced")


def test_engage_prompt_carries_chat_context(wg):
    _serve(wg, MESSAGES)
    wg._CHAT_STATE.note_message(CHAT, name=NAME)
    wg._CHAT_STATE.set_draft(CHAT, "Samstag passt", author="user")
    cid, _ = wg._chat_companion(CHAT)
    wg._conv_add_message(cid, "user", "Was soll ich ihr antworten?")
    conv = wg._load_conv(cid)

    prompt = wg._conv_engage_prompt(conv, False)
    assert f'the signal chat "{NAME}"' in prompt
    assert CHAT in prompt, "the chat id is what chat-draft.py needs"
    # Both directions, each with who actually spoke.
    assert "Kommst du am Samstag?" in prompt
    assert f"{NAME}: Kommst du am Samstag?" in prompt
    assert "The user (sent from their own phone): Ich schau mal" in prompt
    assert "The user (sent from the dashboard): Bis Freitag" in prompt
    assert "Coach: Termin bestätigt." in prompt
    # An image-only message is not silently blank.
    assert "[1 attachment]" in prompt
    # The live shared draft, and who wrote it.
    assert '"Samstag passt"' in prompt and "written by the user" in prompt
    # How to act: stage, don't send — with the runnable command.
    assert "chat-draft.py" in prompt and "--chat" in prompt
    assert "send press" in prompt
    # The persona rule from CLAUDE.md, both halves.
    assert "agents/secretary.md" in prompt
    assert "chambers/*/style/secretary.md" in prompt

    # A live-value note must ride on the fresh-session turn too, or Ara keeps
    # answering from a chat state that has since moved on.
    fresh_prompt = wg._conv_engage_prompt(conv, True)
    assert "Kommst du am Samstag?" in fresh_prompt
    assert "Was soll ich ihr antworten?" in fresh_prompt
    print("PASS test_engage_prompt_carries_chat_context")


def test_group_chat_is_named_as_one(wg):
    """Advice for a group differs from advice for one person, so say which.

    Group-ness falls back to the channel's own key shape when chat state has
    never been stamped — the same fallback the ChatSummary uses."""
    group = "whatsapp:123456@g.us"
    _serve(wg, [_msg("in", "Wer kommt mit?", "2026-08-27T08:00:00Z",
                     sender="4179@s.whatsapp.net", sender_name="Ida")])
    assert wg._CHAT_STATE.get(group)["group"] is None, "unstamped, as intended"
    cid, _ = wg._chat_companion(group)
    prompt = wg._conv_engage_prompt(wg._load_conv(cid), False)
    assert "(a group)" in prompt
    assert "Ida: Wer kommt mit?" in prompt
    print("PASS test_group_chat_is_named_as_one")


def test_context_is_capped_not_summarized(wg):
    many = [_msg("in", f"m{i}", f"2026-08-27T09:{i:02d}:00Z",
                 sender="+41794456312") for i in range(40)]
    _serve(wg, many)
    cid, _ = wg._chat_companion(CHAT)
    conv = wg._load_conv(cid)
    prompt = wg._conv_engage_prompt(conv, False)
    # CHAT_COMPANION_CONTEXT_MESSAGES is 5 in this harness.
    assert wg.CHAT_COMPANION_CONTEXT_MESSAGES == 5
    assert "m39" in prompt and "m35" in prompt
    assert "m34" not in prompt and "m0:" not in prompt
    assert "The 5 most recent messages" in prompt
    # It must not pass truncation off as a summary.
    assert "a cap, not a summary" in prompt
    print("PASS test_context_is_capped_not_summarized")


def test_store_outage_is_admitted(wg):
    def _boom(cid, before=None):
        raise RuntimeError("life store unreachable")
    wg._chat_messages_payload = _boom
    cid, _ = wg._chat_companion(CHAT)
    conv = wg._load_conv(cid)
    prompt = wg._conv_engage_prompt(conv, False)
    assert "could not be read just now" in prompt
    assert "Say so rather than answering as if you had seen them" in prompt
    print("PASS test_store_outage_is_admitted")


def test_plain_thread_gets_no_chat_note(wg):
    _serve(wg, MESSAGES)
    conv = wg._new_conv("user", "reto", "Normal", "user", "hallo")
    prompt = wg._conv_engage_prompt(wg._load_conv(conv["id"]), False)
    assert "Kommst du am Samstag?" not in prompt
    assert "chat-draft.py" not in prompt
    assert wg._conv_chat_note(conv) == ""
    print("PASS test_plain_thread_gets_no_chat_note")


def test_absent_from_default_listing(wg):
    _serve(wg, MESSAGES)
    wg._new_conv("user", "reto", "Normal", "user", "hallo")
    cid, _ = wg._chat_companion(CHAT)
    listed = {c["id"] for c in wg._list_convs("all", "chat")}
    assert cid not in listed, "a companion thread leaked into the normal list"
    assert listed, "the normal list lost its ordinary threads"
    assert cid in {c["id"] for c in wg._list_convs("all", "companion")}
    assert cid in {c["id"] for c in wg._list_convs("all", "all")}
    print("PASS test_absent_from_default_listing")


def test_reply_lands_like_any_thread(wg):
    """A companion is a real conversation: its turns badge and push.

    Nothing in the reply path is kind-aware, which is the point — this pins
    that, so a later "hide companion threads" shortcut cannot quietly make the
    pane's answers silent."""
    _serve(wg, MESSAGES)
    pushes = []
    wg.push_notify.enabled = lambda: True
    wg.push_notify.subscription_count = lambda: 2
    wg.push_notify.notify_async = lambda *a, **k: pushes.append((a, k))
    wg.send_message = lambda *a, **k: {"response": "Ich habe einen Entwurf gestellt."}

    cid, _ = wg._chat_companion(CHAT)
    wg._conv_add_message(cid, "user", "Sag ihr zu.")
    wg._conv_set_flags(cid, unread=False)
    wg._conv_worker(cid, f"conv:{cid}")

    conv = wg._load_conv(cid)
    assert conv["messages"][-1]["role"] == "assistant"
    assert conv["messages"][-1]["text"] == "Ich habe einen Entwurf gestellt."
    assert conv["unread"] is True, "the reply landed without an unread badge"
    assert pushes, "the reply notified no device"
    assert pushes[-1][1].get("tag") == cid
    print("PASS test_reply_lands_like_any_thread")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        test_create_is_idempotent_and_stored(wg)
        test_deleted_thread_is_replaced(wg)
        test_engage_prompt_carries_chat_context(wg)
        test_group_chat_is_named_as_one(wg)
        test_context_is_capped_not_summarized(wg)
        test_store_outage_is_admitted(wg)
        test_plain_thread_gets_no_chat_note(wg)
        test_absent_from_default_listing(wg)
        test_reply_lands_like_any_thread(wg)
    print("all companion-thread tests passed")


if __name__ == "__main__":
    main()
