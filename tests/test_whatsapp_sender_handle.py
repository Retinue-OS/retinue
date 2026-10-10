#!/usr/bin/env python3
"""The WhatsApp gateway delivers every sender in the address book's form.

WhatsApp addresses one person either by phone number (``…@s.whatsapp.net``)
or by LID (``…@lid``). The delivery gate's VIP check compares the sender
verbatim against the handles filed on VIP contacts (``+41…`` or ``…@lid``), so
a sender reduced to its bare user matched neither and a VIP's message was
never worked. _sender_handle must yield exactly the filed form.

Runnable without neonize: JIDs are plain objects with User/Server here.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load(tmp: str):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    os.environ["WHATSAPP_SEND_POLICY"] = json.dumps([])
    os.environ["WHATSAPP_ACCOUNT"] = "+15551234567"
    os.environ["WHATSAPP_PENDING_SENDS_DIR"] = str(Path(tmp) / "pending")
    os.environ["WHATSAPP_DATA_DIR"] = str(Path(tmp) / "data")
    os.environ["WHATSAPP_TMP_DIR"] = str(Path(tmp) / "tmp")
    spec = importlib.util.spec_from_file_location(
        "whatsapp_gateway_sender_under_test", SCRIPTS_DIR / "whatsapp-gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Jid:
    def __init__(self, user, server):
        self.User, self.Server = user, server


def test_sender_handle_forms(wg):
    assert wg._sender_handle(_Jid("41791234567", "s.whatsapp.net")) == "+41791234567"
    assert wg._sender_handle(_Jid("176570585092335", "lid")) == "176570585092335@lid"
    # A stringified JID with a device suffix is the same person.
    assert wg._sender_handle("176570585092335@lid:12") == "176570585092335@lid"
    assert wg._sender_handle("41791234567@s.whatsapp.net:3") == "+41791234567"
    assert wg._sender_handle(None) is None


def test_sender_matches_filed_handle(wg):
    import contacts
    for jid in (_Jid("41791234567", "s.whatsapp.net"), _Jid("176570585092335", "lid")):
        handle = wg._sender_handle(jid)
        assert contacts.normalize_handle("whatsapp", handle) == ("whatsapp", handle), handle


def test_vip_gate_recognises_lid_sender(wg, tmp):
    import triage_policy
    path = Path(tmp) / "policy.nt"
    triage_policy.write_if_changed(triage_policy.render_messenger_policy(
        "whatsapp", triage_policy.MessengerPolicy(
            set(), set(), set(), set(), {"176570585092335@lid", "+41791234567"})), path)
    for jid in (_Jid("176570585092335", "lid"), _Jid("41791234567", "s.whatsapp.net")):
        gate = triage_policy.gate_decision("whatsapp", wg._sender_handle(jid), None, path=path)
        assert gate.get("vip") is True, (jid.User, gate)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load(tmp)
        tests = [lambda: test_sender_handle_forms(wg),
                 lambda: test_sender_matches_filed_handle(wg),
                 lambda: test_vip_gate_recognises_lid_sender(wg, tmp)]
        names = ["test_sender_handle_forms", "test_sender_matches_filed_handle",
                 "test_vip_gate_recognises_lid_sender"]
        failures = 0
        for name, test in zip(names, tests):
            try:
                test()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
        print(f"{len(tests) - failures}/{len(tests)} passed")
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
