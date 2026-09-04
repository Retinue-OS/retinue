#!/usr/bin/env python3
"""Zero-credit gate for agent self-review of their own open projects.

The problem this closes: every other scheduled job is *reactive* — it fires on
inbound mail, an inbound message, or a calendar date. Nothing wakes an agent to
work down projects where the ball is already in *its* court, so such a project
stays invisible until a human pokes it. (A real card sat 11 days for exactly
this reason.)

This runs as a scheduler `command` job, so the scheduler spends no Claude
credits to invoke it. The gate itself is a plain SPARQL SELECT against the life
store — also free. Only when it finds at least one project pending on an AI
agent does it spawn a single `claude -p` session, handing it the already-fetched
tuples so the agent does not re-query. An empty backlog costs nothing beyond one
HTTP round-trip.

"Is an AI agent" is a store-native fact: discover-agents.py types each agent URI
`kb:AiAgent`, so human/external actors (reto, iv-stelle, a correspondent) simply
never match — the join lives in the store, not in a list maintained here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_auth  # noqa: E402

KB = "https://w3id.org/retinue/kb#"
ENDPOINT = os.environ.get("SPARQL_ENDPOINT_LIFE", "http://qlever-life:7001")
# Self-review is supervision — an Ara senior job — so the frontier tier wins
# when the deployment declares one (docs/model-routing.md).
CLAUDE_MODEL = (
    os.environ.get("RETINUE_FRONTIER_MODEL", "").strip()
    or os.environ.get("RETINUE_CLAUDE_MODEL", "").strip()
)
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")

# One query answers the whole gate: unresolved, unpaused projects whose current
# actor is typed as an AI agent. FILTER NOT EXISTS handles projects with no
# `resolved` triple at all (the common case) as well as `resolved = false`, and
# the same for `paused` -- a project explicitly parked with `paused: true`
# (recurring-projects.py's resting state) is not the sweep's business either.
QUERY = f"""
PREFIX kb: <{KB}>
SELECT ?project ?actor ?actorName ?title ?nextAction ?waitingSince WHERE {{
  GRAPH ?g {{
    ?project a kb:Project ;
             kb:currentActor ?actor ;
             kb:title ?title .
    OPTIONAL {{ ?project kb:currentNextAction ?nextAction }}
    OPTIONAL {{ ?project kb:waitingSince ?waitingSince }}
    FILTER NOT EXISTS {{ ?project kb:resolved true }}
    FILTER NOT EXISTS {{ ?project kb:paused true }}
  }}
  ?actor a kb:AiAgent .
  OPTIONAL {{ ?actor kb:name ?actorName }}
}}
ORDER BY ?actor ?project
"""


def query(sparql: str) -> list[dict]:
    data = urllib.parse.urlencode({"query": sparql}).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=data,
        headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    rows = []
    for b in payload.get("results", {}).get("bindings", []):
        rows.append({k: v["value"] for k, v in b.items()})
    return rows


def build_prompt(rows: list[dict]) -> str:
    """Hand the agent the fetched tuples so it need not re-query."""
    by_actor: dict[str, list[dict]] = {}
    for r in rows:
        by_actor.setdefault(r.get("actorName") or r["actor"], []).append(r)

    lines = [
        "You are running the scheduled agent self-review. Open projects exist "
        "where the ball is in an AI agent's court and no inbound event will ever "
        "surface them. Work each one down.",
        "",
        "For each project below:",
        "  - Read its source file (resolve the project URI to its file via the "
        "life store's named graph, as the dashboard does).",
        "  - If you can complete the next action now, do it and update the "
        "project file (set current_actor away from the agent, or resolved: true "
        "when done).",
        "  - If it needs Reto's input, open a dashboard conversation with a "
        "concrete proposal (conversation-push.py). Do not nag: no thread for "
        "work you can simply do.",
        "  - Route each project to its owning agent: handle Ara's directly; "
        "dispatch the owning subagent (Ari, Coach, Medic, ...) for theirs and "
        "relay/escalate as usual.",
        "",
        "Projects pending on an AI agent:",
    ]
    for actor, items in sorted(by_actor.items()):
        lines.append(f"\n## {actor}")
        for r in items:
            lines.append(f"- **{r['title']}**")
            lines.append(f"  - project: {r['project']}")
            if r.get("waitingSince"):
                lines.append(f"  - waiting since: {r['waitingSince']}")
            if r.get("nextAction"):
                lines.append(f"  - next action: {r['nextAction']}")
    return "\n".join(lines)


def main() -> int:
    try:
        rows = query(QUERY)
    except Exception as e:  # store slow/down -> skip this tick, never crash
        print(f"[agent-self-review] gate query failed, skipping: {e}",
              file=sys.stderr)
        return 0

    if not rows:
        print("[agent-self-review] no projects pending on an AI agent; "
              "nothing spawned", file=sys.stderr)
        return 0

    print(f"[agent-self-review] {len(rows)} project(s) pending on an AI agent; "
          "spawning session", file=sys.stderr)
    cmd = ["claude", "-p", "--output-format=json",
           "--permission-mode", PERMISSION_MODE, build_prompt(rows)]
    # Advertise the session's model so memory entries can be stamped with it
    # (scripts/memory.py); cleared when no --model is passed, never inherited.
    env = dict(os.environ)
    env.pop("RETINUE_SESSION_MODEL", None)
    if CLAUDE_MODEL:
        cmd[2:2] = ["--model", CLAUDE_MODEL]
        env["RETINUE_SESSION_MODEL"] = CLAUDE_MODEL
    # Refresh an access token about to expire before the session starts —
    # once, under the lock every framework spawner shares (docs/claude-auth.md).
    claude_auth.ensure_fresh_credentials(
        log=lambda msg: print(f"[agent-self-review] {msg}", file=sys.stderr))
    result = subprocess.run(cmd, cwd="/workspace", env=env)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
