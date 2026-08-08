#!/usr/bin/env python3
"""Write the Herald's scores back into the news store. The agent's only writer.

One call applies a batch, so scoring forty items is one command, not forty:

    python3 scripts/news-score.py --file /root/.retinue/news/scores.json
    echo '[{"id":"a1b2","importance":4}]' | python3 scripts/news-score.py

Each entry needs an `id` and an `importance` (0–5); optional `half_life_hours`
(how fast this one should fade — a bulletin about a standing arrangement ages
slower than a headline), `expires` (an ISO date after which the item is
worthless — the event has happened, the deadline has passed), `reason` (one
short line, shown to the user so a ranking is never unexplained) and `tags`.

Ids that are not in the store are reported and skipped rather than failing the
batch: an item can be pruned between the payload being written and the agent
answering, and losing thirty-nine good scores to one stale id would be silly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_store as store  # noqa: E402

ALLOWED = ("importance", "half_life_hours", "expires", "reason", "tags")


def log(msg: str) -> None:
    print(f"[news-score] {msg}", file=sys.stderr, flush=True)


def coerce(entry: dict) -> dict:
    """Keep only the fields the agent may set, in the types the store expects."""
    out: dict = {}
    if "importance" in entry and entry["importance"] is not None:
        try:
            out["importance"] = max(0.0, min(store.MAX_IMPORTANCE,
                                             float(entry["importance"])))
        except (TypeError, ValueError):
            raise ValueError(f"importance must be a number: {entry['importance']!r}")
    if entry.get("half_life_hours") is not None:
        try:
            hl = float(entry["half_life_hours"])
        except (TypeError, ValueError):
            raise ValueError("half_life_hours must be a number")
        if hl > 0:
            out["half_life_hours"] = hl
    if entry.get("expires"):
        parsed = store.parse_time(str(entry["expires"]))
        if parsed is None:
            raise ValueError(f"expires is not a date: {entry['expires']!r}")
        out["expires"] = store.iso(parsed)
    if entry.get("reason"):
        out["reason"] = str(entry["reason"])[:280]
    if entry.get("tags"):
        tags = entry["tags"]
        if isinstance(tags, str):
            tags = [tags]
        out["tags"] = [str(t)[:40] for t in list(tags)[:8]]
    out["scored_at"] = store.iso(store.now())
    return out


def apply(entries: list[dict]) -> tuple[int, int]:
    applied = skipped = 0
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("id"):
            skipped += 1
            continue
        try:
            fields = coerce(entry)
        except ValueError as exc:
            log(f"{entry.get('id')}: {exc}")
            skipped += 1
            continue
        if store.update_item(str(entry["id"]), **fields) is None:
            log(f"unknown item id, skipped: {entry['id']}")
            skipped += 1
        else:
            applied += 1
    return applied, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply news scores.")
    ap.add_argument("--file", help="JSON file: a list of score entries")
    ap.add_argument("--json", help="JSON string: a list of score entries")
    args = ap.parse_args()

    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8")
    elif args.json:
        raw = args.json
    else:
        raw = sys.stdin.read()
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        log(f"input is not valid JSON: {exc}")
        return 2
    if isinstance(entries, dict):
        entries = entries.get("scores") or entries.get("items") or [entries]
    if not isinstance(entries, list):
        log("expected a JSON list of score entries")
        return 2

    applied, skipped = apply(entries)
    log(f"{applied} scored, {skipped} skipped")
    return 0 if applied or not entries else 1


if __name__ == "__main__":
    raise SystemExit(main())
