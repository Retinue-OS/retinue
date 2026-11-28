"""Only the frontier tier may make something ring.

A ring costs the user an interruption, and every unneeded one devalues the
next. The judgement "this must interrupt the user now" is therefore taken by
the strongest model the deployment has (docs/model-routing.md), never by a
cheaper tier on its own: a declaration that can ring, made from a session
below the frontier tier, is refused here before it reaches the gateway.

What can ring is decided by the attention model (docs/attention-model.md): an
item declared critical rings in every mode, and one of importance 3.5 or more
reaches time-sensitive once its deadline is near — the level every shipped
mode's threshold admits. Below 3.5 an item is at most active, which waits for
the digest; that stays open to every tier.

How the refusal ends depends on the session:

- **Escalation available** (``RETINUE_ESCALATE_FILE`` is set — a dashboard,
  chat or Ask-Ara turn of Ara junior): the CLI exits 3 and says to escalate.
  The turn is re-run on the frontier tier, which decides afresh.
- **No escalation path** (a scheduled job on a lower tier): the declaration is
  lowered to one that cannot ring — ``critical`` dropped, importance capped at
  3 — so the item still arrives and is held for the digest, and the CLI says
  so. A job whose findings must be able to ring pins the frontier model.

Only model sessions are guarded: the check applies when ``CLAUDECODE`` is set
(Claude Code sets it for every command a session runs). The framework's own
monitors are Python processes with fixed rules (a broken sign-in is critical
by construction) and pass unchanged. Like the memory guards, this protects
against a prompt that forgot the rule, not against a session that works around
it on purpose — the environment is the spawner's, and the gateway does not
second-guess it.

The tier comes from ``memory.session_tier``: untiered deployments are one
model doing everything and count as frontier; otherwise only a session whose
spawner-set ``RETINUE_SESSION_MODEL`` is the frontier model does.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

RING_IMPORTANCE = 3.5   # attention.LEVEL_TABLE row 0: time-sensitive when due
CAPPED_IMPORTANCE = 3.0
EXIT_ESCALATE = 3


def can_ring(importance: float | None, critical: bool | None) -> bool:
    """Whether a declaration could make the item ring in a shipped mode."""
    return bool(critical) or (importance is not None and importance >= RING_IMPORTANCE)


def guarded(env: Mapping[str, str] | None = None) -> bool:
    """True when the calling session is a model session below the frontier
    tier — the one case the guard acts on."""
    env = os.environ if env is None else env
    if not env.get("CLAUDECODE", "").strip():
        return False
    import memory  # stdlib-only; imported late so the CLIs stay cheap
    return memory.session_tier(env) != memory.FRONTIER


def check(prog: str, importance: float | None, critical: bool | None,
          env: Mapping[str, str] | None = None) -> tuple[int | None, float | None, bool | None]:
    """Apply the guard to one declaration.

    Returns ``(exit_code, importance, critical)``: ``exit_code`` is None when
    the caller goes on — with the declaration as given, or lowered so it cannot
    ring — and EXIT_ESCALATE when it must stop and the session escalate.
    """
    env = os.environ if env is None else env
    if not can_ring(importance, critical) or not guarded(env):
        return None, importance, critical
    if env.get("RETINUE_ESCALATE_FILE", "").strip():
        print(f"{prog}: refused — only the frontier tier may make something ring "
              f"(--critical, or importance {RING_IMPORTANCE:g}+), and this session runs "
              "below it. Escalate: run  touch \"$RETINUE_ESCALATE_FILE\"  and end the "
              "turn; Ara senior decides afresh whether it rings.", file=sys.stderr)
        return EXIT_ESCALATE, importance, critical
    lowered = min(importance, CAPPED_IMPORTANCE) if importance is not None else None
    print(f"{prog}: lowered — only the frontier tier may make something ring, and this "
          "session runs below it with no way to escalate. Sent without --critical and "
          f"with importance at most {CAPPED_IMPORTANCE:g}: the item is held for the "
          "digest. A job whose findings must ring pins the frontier model.",
          file=sys.stderr)
    return None, lowered, False if critical else critical
