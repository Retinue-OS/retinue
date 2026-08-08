#!/usr/bin/env python3
"""Zero-credit gate for news curation — the agent half of the news feed.

Same shape as `agent-self-review.py`: this runs as a scheduler **command** job,
so the scheduler spends no Claude credits to invoke it, and the gate itself is a
file read. Only when there is actual work — items nobody has scored, or user
feedback nobody has folded into the preferences yet — does it spawn a single
`claude -p` session, which dispatches the **Herald** subagent.

That keeps the loop honest: a quiet news day costs nothing, and the model is
only asked the question it is actually good at ("does Reto care about this?"),
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
import news_store as store  # noqa: E402

CLAUDE_MODEL = os.environ.get("RETINUE_CLAUDE_MODEL", "").strip()
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

    cmd = ["claude", "-p", "--output-format=json",
           "--permission-mode", PERMISSION_MODE,
           build_prompt(path, len(items), len(feedback))]
    if CLAUDE_MODEL:
        cmd[2:2] = ["--model", CLAUDE_MODEL]
    result = subprocess.run(cmd, cwd="/workspace")
    if result.returncode == 0:
        # Only advance the cursor on a clean run: a crashed session must see the
        # same feedback again rather than lose it.
        store.ack_feedback(cursor)
    else:
        log(f"curation session exited {result.returncode}; feedback left pending")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
