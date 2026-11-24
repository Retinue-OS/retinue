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

The checks:

- a mail the deterministic trim left `wanting` is asked for by the page, the
  turn chooses lines, the gateway writes them as the record's text with
  `kb:trim "model"`, and the page's next poll and its next ask show them —
  the mail's own parts untouched;
- a choice the gateway refuses (ranges that are none) settles the mail as
  the rules trimmed it, text unchanged;
- a mail whose parts changed while its turn ran is refused as stale (409):
  nothing settled, nothing paused;
- a mail too long to hand over is settled by the gateway without a turn;
- a mail removed from the mailbox answers 410 and starts nothing;
- the day's budget spent: nothing started, the page told to wait for the
  local midnight;
- a turn that ends without a judgement leaves the mail wanting, and the next
  ask after its wait trims it;
- a mail whose every turn ends without one is settled after
  MAIL_MODEL_TRIM_ATTEMPTS of them, with no further turn;
- a judgement the gateway could not take (busy: a pass holds its lock) is
  offered again and taken, with no second turn.

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


class Model:
    """The stand-in `_run_claude`: each turn answers as ``answer(prompt)``
    says — the text of its answer, or "timeout" for a turn that runs out of
    time."""

    def __init__(self, answer=_choose):
        self.answer = answer
        self.calls: list = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        said = self.answer(cmd[-1])
        if said == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        return subprocess.CompletedProcess(cmd, 0, json.dumps(
            {"type": "result", "subtype": "success", "is_error": False,
             "result": said, "total_cost_usd": 0.001,
             "usage": {"input_tokens": 900, "output_tokens": 20}}), "")


def _fresh(r: e2e.Rig, model: Model) -> None:
    """What one check leaves of the web gateway's model trim must not decide
    the next."""
    wg = r.wg
    with wg._mail_trim_cond:
        wg._mail_trim_backoff.clear()
        wg._mail_trim_settled.clear()
        wg._mail_trim_attempts.clear()
        wg._mail_trim_held.clear()
        wg._mail_trim_state.update(failures=0, paused_until=0.0, spent=None)
    wg.MAIL_MODEL_TRIM_STATE_FILE.unlink(missing_ok=True)
    wg.MAIL_MODEL_TRIM_DAILY_MAX = 200
    wg.MAIL_MODEL_TRIM_RETRY_SECONDS = 3600.0
    wg._MAIL_TRIM_REOFFER_FIRST = 0.05
    wg._run_claude = model


def _wanting(r: e2e.Rig, root: str, body: str) -> str:
    """A mail arrives that the rules leave `wanting`; the path its page asks
    for its trim at."""
    r.arrives(root, subject="Party", body=body)
    [shown] = [m for m in r.messages(root)["messages"] if m["id"] == root]
    assert shown["trim"] == "wanting", f"the deterministic trim settled the mail: {shown['trim']}"
    token = shown["original"].rsplit("/", 2)[-2]
    return "/chats/" + mail._quote(r.chat(root)) + "/messages/" + token + "/trim"


def _answered(r: e2e.Rig, path: str):
    """Ask, and ask again while a turn is queued or running: the answer that
    is not 202."""
    deadline = time.time() + 20
    while time.time() < deadline:
        status, body = r.http("POST", path)
        if status != 202:
            return status, body
        time.sleep(0.05)
    raise AssertionError("the trim stayed in flight")


def _shown(r: e2e.Rig, root: str) -> dict:
    [shown] = [m for m in r.messages(root)["messages"] if m["id"] == root]
    return shown


def _idle(r: e2e.Rig) -> None:
    deadline = time.time() + 20
    while time.time() < deadline:
        with r.wg._mail_trim_cond:
            if (r.wg._mail_trim_state["running"] is None and not r.wg._mail_trim_queue
                    and not r.wg._mail_trim_held):
                return
        time.sleep(0.02)
    raise AssertionError("the trim worker did not finish")


MAIL = "\n".join(AUTHOR + FOOTER) + "\n"


def test_a_wanting_mail_is_trimmed_by_the_model(r: e2e.Rig) -> None:
    model = Model()
    _fresh(r, model)
    calls = model.calls
    root = "trim-e2e-1@x.org"
    path = _wanting(r, root, MAIL)
    status, body = r.http("POST", path)
    assert status == 202 and body.get("state") in ("queued", "running"), (status, body)
    status, body = _answered(r, path)
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


def test_an_invalid_choice_settles_the_mail_untrimmed(r: e2e.Rig) -> None:
    model = Model(lambda prompt: '{"keep": [[5, 3]]}')
    _fresh(r, model)
    root = "trim-e2e-invalid@x.org"
    path = _wanting(r, root, MAIL)
    before = _shown(r, root)["text"]
    r.http("POST", path)
    status, body = _answered(r, path)
    assert (status, body) == (200, {"state": "deterministic", "text": before}), (status, body)
    assert (_shown(r, root)["trim"], _shown(r, root)["text"]) == ("deterministic", before)
    assert len(model.calls) == 1


def test_a_mail_that_changed_under_its_turn_is_stale(r: e2e.Rig) -> None:
    """The record was written anew from other parts while the turn ran: the
    gateway's digest of its lines is not the one the turn was shown, and
    the choice is refused — it is about lines that are not the mail's."""
    root = "trim-e2e-stale@x.org"
    gw = r.gw

    def changed(prompt):
        path, fields = r.w.mirror._trim_target(root, root)
        media_id = gw._ibstore.media_id_of(fields["body_part"])
        blob = gw._ibstore.media_dir(gw.INBOUND_STORE_DIR) / media_id
        body = blob.read_bytes().decode("utf-8").replace("doors open at seven",
                                                         "doors open at eight")
        blob.write_bytes(body.encode("utf-8"))
        # The record's text follows its parts, as a rewrite would leave it.
        gw._ibstore.update_message(path, text=gw._trim.trim(
            body, None, "Party", is_reply=False, bulk=False).text)
        return _choose(prompt)
    model = Model(changed)
    _fresh(r, model)
    path = _wanting(r, root, MAIL)
    assert r.http("POST", path)[0] == 202
    _idle(r)
    assert len(model.calls) == 1
    assert _shown(r, root)["trim"] == "wanting"
    assert r.wg._mail_trim_state["failures"] == 0
    # Refused as stale by the gateway (not lost on the way): the mail waits
    # as one that changed, not as one whose gateway was away.
    status, body = r.http("POST", path)
    assert status == 200 and body["state"] == "wanting", (status, body)
    assert body["retry_after"] > r.wg._MAIL_TRIM_AWAY_SECONDS, body


def test_a_mail_too_long_to_hand_over_is_settled_without_a_turn(r: e2e.Rig) -> None:
    model = Model()
    _fresh(r, model)
    root = "trim-e2e-long@x.org"
    path = _wanting(r, root, "\n".join(f"Line {i} of the report." for i in range(1700)) + "\n")
    r.http("POST", path)
    status, body = _answered(r, path)
    assert status == 200 and body["state"] == "deterministic", (status, body)
    assert not model.calls
    assert _shown(r, root)["trim"] == "deterministic"


def test_a_mail_removed_from_the_mailbox_is_gone(r: e2e.Rig) -> None:
    model = Model()
    _fresh(r, model)
    root = "trim-e2e-removed@x.org"
    path = _wanting(r, root, MAIL)
    r.box.remove("INBOX", r.box.uid_of("INBOX", root))
    r.w.mirror.poll()
    assert _shown(r, root)["removed"] is True
    assert r.http("POST", path) == (410, {"error": "this mail is gone from the mailbox"})
    _idle(r)
    assert not model.calls


def test_the_days_budget_spent(r: e2e.Rig) -> None:
    model = Model()
    _fresh(r, model)
    r.wg.MAIL_MODEL_TRIM_DAILY_MAX = 0
    root = "trim-e2e-budget@x.org"
    path = _wanting(r, root, MAIL)
    status, body = r.http("POST", path)
    assert status == 200 and body["state"] == "wanting", (status, body)
    _day, to_midnight = r.wg._mail_trim_day()
    assert abs(body["retry_after"] - to_midnight) <= 2, (body, to_midnight)
    _idle(r)
    assert not model.calls and _shown(r, root)["trim"] == "wanting"


def test_no_judgement_then_a_retry(r: e2e.Rig) -> None:
    said = iter(["timeout", None])
    model = Model(lambda prompt: next(said) or _choose(prompt))
    _fresh(r, model)
    r.wg.MAIL_MODEL_TRIM_RETRY_SECONDS = 0.3
    root = "trim-e2e-retry@x.org"
    path = _wanting(r, root, MAIL)
    r.http("POST", path)
    status, body = _answered(r, path)
    assert status == 200 and body["state"] == "wanting", (status, body)
    assert _shown(r, root)["trim"] == "wanting" and len(model.calls) == 1
    time.sleep(0.35)
    r.http("POST", path)
    status, body = _answered(r, path)
    assert (status, body) == (200, {"state": "model", "text": "\n".join(AUTHOR)}), (status, body)
    assert len(model.calls) == 2


def test_a_mail_whose_turns_never_judge_is_given_up(r: e2e.Rig) -> None:
    model = Model(lambda prompt: "timeout")
    _fresh(r, model)
    r.wg.MAIL_MODEL_TRIM_RETRY_SECONDS = 0.1
    root = "trim-e2e-giveup@x.org"
    path = _wanting(r, root, MAIL)
    before = _shown(r, root)["text"]
    for n in range(1, r.wg.MAIL_MODEL_TRIM_ATTEMPTS + 1):
        deadline = time.time() + 10
        while r.http("POST", path)[0] != 202:
            assert time.time() < deadline, n
            time.sleep(0.05)
        _idle(r)
        assert len(model.calls) == n
    assert (_shown(r, root)["trim"], _shown(r, root)["text"]) == ("deterministic", before)
    assert r.http("POST", path) == (200, {"state": "deterministic", "text": before})
    assert len(model.calls) == r.wg.MAIL_MODEL_TRIM_ATTEMPTS


def test_a_busy_gateway_takes_the_judgement_later(r: e2e.Rig) -> None:
    model = Model()
    _fresh(r, model)
    mirror, real = r.w.mirror, r.w.mirror.trim_result
    refused = []

    def busy_once(*args, **kwargs):
        if not refused:
            refused.append(args)
            raise r.gw.Busy("a pass over the mailbox holds the lock")
        return real(*args, **kwargs)
    mirror.trim_result = busy_once
    try:
        root = "trim-e2e-busy@x.org"
        path = _wanting(r, root, MAIL)
        r.http("POST", path)
        status, body = _answered(r, path)
    finally:
        del mirror.trim_result
    assert refused, "the gateway was never busy"
    assert (status, body) == (200, {"state": "model", "text": "\n".join(AUTHOR)}), (status, body)
    assert len(model.calls) == 1
    assert r.wg._mail_trim_state["failures"] == 0


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
