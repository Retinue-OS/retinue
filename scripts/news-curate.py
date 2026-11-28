#!/usr/bin/env python3
"""Zero-credit gate for news curation — the agent half of the news feed.

Same shape as `agent-self-review.py`: this runs as a scheduler **command** job,
so the scheduler spends no Claude credits to invoke it, and the gate itself is a
file read. Only when there is actual work — items nobody has scored, or user
feedback nobody has folded into the preferences yet — does it spawn a single
`claude -p` session, which dispatches the **Herald** subagent.

That keeps the loop honest: a quiet news day costs nothing, and the model is
only asked the question it is actually good at ("does the owner care about this?"),
never the ranking arithmetic, which is a formula in `news_store.py`.

The payload (unscored items, pending feedback, current preferences) is written
to a file the agent reads, rather than pasted into the prompt: a hundred items
is a lot of prompt, and the agent needs the file path anyway to write back.

    python3 scripts/news-curate.py            # gate, spawn if there is work
    python3 scripts/news-curate.py --dry-run  # report what it would do
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_auth  # noqa: E402
import agent_models  # noqa: E402
import news_store as store  # noqa: E402
import session_env  # noqa: E402

# This turn only dispatches the Herald (which pins its own model), so it is a
# router-tier Ara junior turn (docs/model-routing.md).
CLAUDE_MODEL = (
    os.environ.get("RETINUE_ROUTER_MODEL", "").strip()
    or os.environ.get("RETINUE_CLAUDE_MODEL", "").strip()
)
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")
# Scoring a whole backlog in one session is slower and no better than scoring
# the freshest slice; the rest comes round on the next run an hour later.
MAX_ITEMS_PER_RUN = int(os.environ.get("NEWS_CURATE_BATCH", "40"))


def log(msg: str) -> None:
    print(f"[news-curate] {msg}", file=sys.stderr, flush=True)


def unscored_items(limit: int) -> list[dict]:
    """Items the Herald has not looked at yet, newest first — the same order the
    feed shows them in, so the most visible ones get scored first."""
    pending = [i for i in store.load_items()
               if not isinstance(i.get("importance"), (int, float))
               and not i.get("hidden")]
    pending.sort(key=lambda i: i.get("published") or i.get("fetched") or "",
                 reverse=True)
    return pending[:limit]


def score_stamps(ids: list[str]) -> dict:
    """The ``scored_at`` stamp of each given item now (None where unscored).
    news-score.py stamps every score it writes; a user's up/down tap changes
    ``importance`` but never ``scored_at``."""
    wanted = set(ids)
    return {i.get("id"): i.get("scored_at") for i in store.load_items()
            if i.get("id") in wanted}


def count_scored(ids: list[str], before: dict) -> int:
    """How many of the given items the session scored: their ``scored_at``
    is set and differs from ``before`` (score_stamps taken before the run) —
    read back from the store after the session, since a clean exit says
    nothing about scoring, and an importance alone may be the user's tap."""
    after = score_stamps(ids)
    return sum(1 for k in ids if after.get(k) and after.get(k) != before.get(k))


def build_payload(items: list[dict], feedback: list[dict]) -> dict:
    return {
        "generated": store.iso(store.now()),
        "preferences_file": str(store.preferences_file()),
        "score_command": "python3 /workspace/scripts/news-score.py --file <json>",
        "items": [
            {
                "id": i["id"],
                "title": i.get("title"),
                "source": i.get("source"),
                "url": i.get("url"),
                "summary": i.get("summary"),
                "published": i.get("published"),
                "lang": i.get("lang"),
            }
            for i in items
        ],
        "feedback": feedback,
    }


def build_prompt(payload_path: Path, n_items: int, n_feedback: int) -> str:
    parts = [
        "You are running the scheduled news curation.",
        "",
        f"Dispatch the `herald` subagent (Agent tool, subagent_type `herald`). "
        f"Its payload is the file `{payload_path}`: "
        f"{n_items} unscored news item(s) and {n_feedback} piece(s) of user "
        "feedback. Tell it to read that file and follow its own instructions: "
        "score each item, then fold the feedback into "
        f"`{store.preferences_file()}`.",
        "",
        "Relay its one-line summary. Do not open a dashboard conversation for "
        "this — curation is routine, and the result is visible on the news page.",
    ]
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Gate and run news curation.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the gate result, spawn nothing")
    args = ap.parse_args()

    items = unscored_items(MAX_ITEMS_PER_RUN)
    feedback, cursor = store.pending_feedback()
    if not items and not feedback:
        log("nothing unscored, no new feedback; nothing spawned")
        return 0

    payload = build_payload(items, feedback)
    path = store.curation_payload_file()
    if args.dry_run:
        log(f"would curate {len(items)} item(s) and {len(feedback)} feedback entry(ies)")
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0

    store._write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=1))
    log(f"{len(items)} item(s) to score, {len(feedback)} feedback entry(ies); "
        "spawning session")

    # The payload, the items and the preferences all live under NEWS_DIR, on
    # the persistent volume outside the session's working directory. Without
    # --add-dir the Herald's Read on them is denied; a background-dispatched
    # Herald then (rightly) refuses to work around the denial, and the run ends
    # cleanly having scored nothing — which is how scoring silently stopped
    # from Claude Code 2.1.317 on.
    cmd = ["claude", "-p", "--output-format=json",
           "--permission-mode", PERMISSION_MODE,
           "--add-dir", str(store.NEWS_DIR)]
    if CLAUDE_MODEL:
        cmd[2:2] = ["--model", CLAUDE_MODEL]
    # Per-agent model overrides for the Herald this session dispatches
    # (scripts/agent_models.py); [] when none is set.
    cmd += agent_models.cli_args()
    # The prompt after "--": --add-dir takes any number of directories and
    # would otherwise read the prompt as one more, leaving the session with
    # no input at all (as web-gateway.py builds its own commands).
    cmd += ["--", build_prompt(path, len(items), len(feedback))]
    # The handed-over items' score stamps before the run: what changes them is
    # the session's scoring, never the user's up/down tap meanwhile.
    handed = [i["id"] for i in items]
    before = score_stamps(handed)
    # The allowlisted environment (scripts/session_env.py), never a copy of
    # this process's own: under the scheduler that is already clean, but run
    # by hand from a session it would not be. It also stamps the session's
    # model so memory entries can record it (scripts/memory.py); no --model,
    # no stamp — never an inherited one.
    env = session_env.build(model=CLAUDE_MODEL)
    # Refresh an access token about to expire before the session starts —
    # once, under the lock every framework spawner shares (docs/claude-auth.md).
    claude_auth.ensure_fresh_credentials(log=log)
    result = subprocess.run(cmd, cwd="/workspace", env=env)
    if result.returncode != 0:
        log(f"curation session exited {result.returncode}; feedback left pending")
        return result.returncode
    # A clean exit is not success: a session whose Herald could not read the
    # payload exits 0 too. Items were handed over, so some must come back
    # scored; otherwise the run fails, and the scheduler shows it.
    if handed:
        scored = count_scored(handed, before)
        if scored == 0:
            log(f"curation session exited 0 but scored none of {len(handed)} "
                "item(s); feedback left pending")
            return 1
        log(f"scored {scored} of {len(handed)} item(s)")
    # Only advance the cursor on a successful run: a failed session must see
    # the same feedback again rather than lose it.
    store.ack_feedback(cursor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
