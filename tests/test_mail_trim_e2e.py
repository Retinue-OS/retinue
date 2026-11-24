#!/usr/bin/env python3
"""The model trim across the seam: the REAL mail gateway behind the REAL web
gateway (docs/mail-chats.md, "The model trim"; docs/dashboard.md, "The model
trim").

tests/test_chat_mail_trim.py runs the web gateway's side against a mock of
the mail gateway, and the mail gateway's own tests run its trim-source and
trim-result by themselves. A defect between the two — a source in a shape the
web gateway does not read, a digest one side computes otherwise, a record the
poll does not see — passes both. This file has no mock of either: the rig of
tests/test_chat_mail_e2e.py (the mail gateway's HTTP handler over the fake
mailbox, the web gateway's handler in front of it, the SPARQL mock answering
from the ledger the gateway writes), and only the model is a stand-in — a
fake `_run_claude` that reads the numbered lines it is handed and chooses the
ones that are no footer, as a model would.

One check: a mail the deterministic trim left `wanting` is asked for by the
page, the turn chooses lines, the gateway writes them as the record's text
with `kb:trim "model"`, and the page's next poll and its next ask show them —
the mail's own parts untouched.

Standalone:

    python3 tests/test_mail_trim_e2e.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import test_chat_mail_e2e as e2e  # noqa: E402  (the Rig: both real gateways)

mail = e2e.mail

AUTHOR = ["Hi,", "", "the venue moved to the old mill; doors open at seven.", "", "Ann"]
# A footer long enough that the deterministic trim, which cuts a legal notice
# only on evidence, leaves the mail long — and so `wanting`.
FOOTER = [f"Disclaimer {i}: this message and its attachments are confidential and "
          "intended solely for the addressee." for i in range(60)]


def _choose(prompt: str) -> str:
    """What the stand-in model answers: the ranges of the lines that are no
    disclaimer, read from the numbered lines of the prompt."""
    rows = re.findall(r'^<l n="(\d+)">(.*)</l>$', prompt, re.MULTILINE)
    keep, run = [], None
    for n, content in rows:
        n = int(n)
        if content.startswith("Disclaimer"):
            run = None
            continue
        if run is None:
            run = [n, n]
            keep.append(run)
        else:
            run[1] = n
    return json.dumps({"keep": keep})


def test_a_wanting_mail_is_trimmed_by_the_model(r: e2e.Rig) -> None:
    calls = []

    def fake_claude(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(
            {"type": "result", "subtype": "success", "is_error": False,
             "result": _choose(cmd[-1])}), "")
    r.wg._run_claude = fake_claude
    root = "trim-e2e-1@x.org"
    r.arrives(root, subject="Party", body="\n".join(AUTHOR + FOOTER) + "\n")
    [shown] = [m for m in r.messages(root)["messages"] if m["id"] == root]
    assert shown["trim"] == "wanting", f"the deterministic trim settled the mail: {shown['trim']}"
    token = shown["original"].rsplit("/", 2)[-2]
    path = "/chats/" + mail._quote(r.chat(root)) + "/messages/" + token + "/trim"
    status, body = r.http("POST", path)
    assert status == 202 and body.get("state") in ("queued", "running"), (status, body)
    deadline = time.time() + 20
    while time.time() < deadline:
        status, body = r.http("POST", path)
        if status == 200:
            break
        assert status == 202, (status, body)
        time.sleep(0.05)
    assert status == 200, (status, body)
    assert body == {"state": "model", "text": "\n".join(AUTHOR)}, body
    assert len(calls) == 1, calls
    # The record says so, and the poll shows it.
    [shown] = [m for m in r.messages(root)["messages"] if m["id"] == root]
    assert (shown["trim"], shown["text"]) == ("model", "\n".join(AUTHOR)), shown
    # The original is the mail as it came.
    status, original = r.http("GET", shown["original"])
    assert status == 200 and "Disclaimer 59" in original["text"], (status, original)
    # Asked again, nothing more is spent.
    assert r.http("POST", path) == (200, {"state": "model", "text": "\n".join(AUTHOR)})
    assert len(calls) == 1


def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        rig = e2e.Rig(Path(tmp))
        try:
            for name, check in [(n, c) for n, c in globals().items()
                                if n.startswith("test_") and callable(c)]:
                try:
                    check(rig)
                    print(f"PASS {name}")
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    print(f"FAIL {name}: {exc}\n     needs the mail gateway's "
                          "POST /messages/trim-source and /messages/trim-result")
                    traceback.print_exc()
        finally:
            rig.close()
    if failures:
        print(f"\n{failures} end-to-end model-trim check(s) failed.")
        return 1
    print("\nall end-to-end model-trim checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
