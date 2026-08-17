#!/usr/bin/env python3
"""Tests for the triage delivery-gate policy — the three-axis routing model.

Run directly (`python3 scripts/test_triage_policy.py`) or under pytest. Stdlib
only, no fixtures beyond a tempdir, matching triage_policy's own discipline.

The axes under test (see triage_policy module docstring):
  * sender: whitelisted / blacklisted / unknown
  * group: three independent flags — news, quieted, ignored
Quieted/ignored bite only for unknown senders; news is orthogonal to triage.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import triage_policy as tp  # noqa: E402

CH = "telegram"
GROUP = "-100123"


def _policy_file(tmp: Path, **kw) -> Path:
    """Render a policy .nt into tmp and return its path."""
    pol = tp.MessengerPolicy(
        whitelist=set(kw.get("whitelist", [])),
        blacklist=set(kw.get("blacklist", [])),
        ignored=set(kw.get("ignored", [])),
        quieted=set(kw.get("quieted", [])),
        news=set(kw.get("news", [])),
    )
    path = tmp / "policy.nt"
    path.write_text(tp.render_messenger_policy(CH, pol), encoding="utf-8")
    return path


def _gate(path, sender, group=None):
    return tp.gate_decision(CH, sender, group, path=path)


def test_routing_matrix() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        # Whitelisted handle → immediate forward, regardless of group flags.
        path = _policy_file(tmp, whitelist=["+41791112233"],
                            ignored=[GROUP], quieted=[GROUP])
        g = _gate(path, "+41791112233", GROUP)
        assert g["forward"] and not g["flagged_unknown"], g
        assert g["reason"] == "whitelisted", g

        # Blacklisted handle → held, drained daily (delivered False).
        path = _policy_file(tmp, blacklist=["+41790000000"])
        g = _gate(path, "+41790000000", None)
        assert not g["forward"] and g["delivered_if_held"] is False, g
        assert g["reason"] == "blacklisted", g

        # Unknown, no group → forward + flagged for the whitelist ask-flow.
        path = _policy_file(tmp)
        g = _gate(path, "+41795555555", None)
        assert g["forward"] and g["flagged_unknown"], g
        assert g["reason"] == "unknown", g

        # Unknown, quieted group → held but drained daily (delivered False).
        path = _policy_file(tmp, quieted=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert not g["forward"] and not g["flagged_unknown"], g
        assert g["delivered_if_held"] is False and g["reason"] == "group-quieted", g

        # Unknown, ignored group → held, never drained (delivered True).
        path = _policy_file(tmp, ignored=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert not g["forward"], g
        assert g["delivered_if_held"] is True and g["reason"] == "group-ignored", g


def test_news_flag_is_orthogonal() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        # news + ignored (a feed-only broadcast source, no personal interaction):
        # held from triage, but flagged for the news rail.
        path = _policy_file(tmp, news=[GROUP], ignored=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert g["news"] is True and not g["forward"], g
        assert g["reason"] == "group-ignored", g

        # news + quieted: reaches triage on the daily drain, and the news feed.
        path = _policy_file(tmp, news=[GROUP], quieted=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert g["news"] is True and not g["forward"], g
        assert g["delivered_if_held"] is False, g

        # news alone (no quiet/ignore): unknown sender still forwards to triage.
        path = _policy_file(tmp, news=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert g["news"] is True and g["forward"] and g["flagged_unknown"], g

        # A group not flagged news never sets the news flag.
        path = _policy_file(tmp, quieted=[GROUP])
        g = _gate(path, "+41795555555", GROUP)
        assert g["news"] is False, g


def test_legacy_blocked_group_reads_as_ignored() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        path = tmp / "policy.nt"
        subj = tp._channel_subject(CH)
        path.write_text(tp._triple(subj, tp.P_BLOCKED_GROUP, GROUP) + "\n",
                        encoding="utf-8")
        pol = tp.load_messenger_policy(CH, path=path)
        assert GROUP in pol.ignored and not pol.quieted, pol
        g = _gate(path, "+41795555555", GROUP)
        assert not g["forward"] and g["reason"] == "group-ignored", g


def test_disabled_gate_forwards_all_without_news() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = _policy_file(Path(d), news=[GROUP], ignored=[GROUP])
        g = tp.gate_decision(CH, "+41795555555", GROUP, path=path, enabled=False)
        assert g["forward"] and g["news"] is False and g["reason"] == "gate-disabled", g


def test_mutate_quiet_and_ignore_are_exclusive() -> None:
    with tempfile.TemporaryDirectory() as d:
        os.environ["TRIAGE_MESSENGER_DIR"] = d
        try:
            tp._mutate_messenger(CH, ig_add=[GROUP], news_add=[GROUP])
            pol = tp.load_messenger_policy(CH)
            assert pol.ignored == {GROUP} and pol.news == {GROUP} and not pol.quieted, pol

            # Quieting an ignored group moves it, never doubles it.
            tp._mutate_messenger(CH, q_add=[GROUP])
            pol = tp.load_messenger_policy(CH)
            assert pol.quieted == {GROUP} and not pol.ignored, pol
            assert pol.news == {GROUP}, pol  # news is untouched by the move

            # And back the other way.
            tp._mutate_messenger(CH, ig_add=[GROUP])
            pol = tp.load_messenger_policy(CH)
            assert pol.ignored == {GROUP} and not pol.quieted, pol
        finally:
            tp.messenger_policy_path(CH).unlink(missing_ok=True)
            del os.environ["TRIAGE_MESSENGER_DIR"]


def test_render_is_deterministic_and_migrates_legacy() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # Legacy file with the old predicate.
        path = tmp / "policy.nt"
        subj = tp._channel_subject(CH)
        path.write_text(tp._triple(subj, tp.P_BLOCKED_GROUP, GROUP) + "\n",
                        encoding="utf-8")
        pol = tp.load_messenger_policy(CH, path=path)
        rendered = tp.render_messenger_policy(CH, pol)
        # Re-render migrates blocked → ignored predicate.
        assert tp.P_IGNORED_GROUP in rendered and tp.P_BLOCKED_GROUP not in rendered
        # Deterministic: same input → identical bytes.
        assert rendered == tp.render_messenger_policy(CH, pol)


def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
