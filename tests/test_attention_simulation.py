#!/usr/bin/env python3
"""The example day (examples/attention-simulation) replays on the real gateway
without a beat being skipped: every scripted action finds the state the story
expects — the held message to pull, the chat to answer, the thread to tap,
the stranger to file as a contact — and the day ends with the numbers the
brief describes.

    python3 tests/test_attention_simulation.py
"""
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples" / "attention-simulation"))

import simulate as S  # noqa: E402


def main():
    tmp = Path(tempfile.mkdtemp(prefix="attention-simulation-test-"))
    sim = S.Simulation(tmp, 8769)
    sim.boot()
    sim.start()
    sim.advance_to(S.DAY, holds=False)
    skipped = [f for f in sim.feed if f.get("skipped")]
    assert not skipped, "skipped beats: " + "; ".join(f["text"] for f in skipped)
    assert sim.ended and sim.index == len(S.story.SCRIPT)
    who = {f["who"] for f in sim.feed}
    assert {"narrator", "you", "system", "push", "learn", "ara"} <= who, who
    st = sim.stats
    assert st["digests"] == 3, st           # 08:00, 12:00 and the manual release at 16:15
    # backup, physio (sweep), Nadia's second message (a sender the contact
    # card made known), Luca, NDA (permit). The VAT return climbs to
    # time-sensitive on the corrected lead but is admin, outside the
    # afternoon's scope: it tops Now at 17:00 without ringing.
    assert st["pushes"] == 5, st
    assert st["corrections"] == 3, st       # the lead time, the contact card, the permit
    digests = [f["text"] for f in sim.feed if f.get("digest")]
    assert any("Anna Keller" in d and "Beat Frei" in d for d in digests), digests
    learned = [f["text"] for f in sim.feed if f["who"] == "learn"]
    assert any("tax filing" in x for x in learned) and any("Beat Frei" in x for x in learned), learned
    # The stranger: screened on arrival, named by the contact card, and a
    # known sender by the time she writes again (docs/attention-model.md,
    # docs/triage-delivery-gate.md).
    system = [f["text"] for f in sim.feed if f["who"] == "system"]
    assert any("held until Sun 17:00 — Focused on customers — this is not" in x and "+41791000042" in x for x in system), system
    assert any("+41791000042 — unknown" in x and "flagged as an unknown sender" in x
               for x in system), system
    assert any("+41791000042 — whitelisted" in x for x in system), system
    assert any("sphere customers + friends" in x for x in system), system
    assert any("whitelist" in x and "+41791000042" in x for x in learned), learned
    end = sim.snapshot()["attention"]["counts"]
    assert end["now"] == 0 and end["waiting"] == 2, end
    # Seeking back replays cleanly to the same state.
    sim.seek(12 * 60 + 5)
    mid = sim.snapshot()
    assert mid["time"] == "12:05" and mid["attention"]["mode"]["name"] == "Chores", mid["attention"]["mode"]
    assert not any(f.get("skipped") for f in sim.feed)
    sim.server.shutdown()
    print("ok: the day replays on the real gateway without a skipped beat")


if __name__ == "__main__":
    main()
