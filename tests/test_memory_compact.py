#!/usr/bin/env python3
"""Checks for the scheduled memory compaction job (scripts/memory-compact.py).

No store, no network, no Claude: the job's only store access is
`memory._query`, which every case replaces with a fake answering from canned
SPARQL JSON bindings; the session spawn (`run_session`) and the alert push
are replaced by recorders. What is pinned here is the job's own judgement —
which entries the gate asks for, how they are clustered and batched, what the
session is handed, which model it runs on, and when a failing streak reaches
the user — because a mistake in any of them either compacts the wrong
entries or silently never compacts at all.

    python3 tests/test_memory_compact.py
"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "memory-compact.py"

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
PFX = "urn:retinue:memory:"
KB = "https://w3id.org/retinue/kb#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

ENV_VARS = ("RETINUE_FRONTIER_MODEL", "RETINUE_ROUTER_MODEL", "RETINUE_CLAUDE_MODEL",
            "RETINUE_SESSION_MODEL", "RETINUE_MEMORY")


def load(env=None):
    """Load the job fresh, with `env` set while its module constants are read."""
    saved = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        spec = importlib.util.spec_from_file_location("memory_compact", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


def uri(v):
    return {"type": "uri", "value": v}


def lit(v):
    return {"type": "literal", "value": str(v)}


def days_ago(n, minutes=0):
    t = NOW - dt.timedelta(days=n, minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def row(entry_id, t, tags, content=None, *, corrected=(), superseded=(),
        questioned=(), gen=None, expires=None, relevance=None, reiterations=0,
        model="claude-opus-5"):
    """One recall binding row, shaped as memory._recall_sparql returns it."""
    r = {
        "m": uri(PFX + entry_id),
        "content": lit(content or f"content of {entry_id}"),
        "t": lit(t),
        "actor": uri("urn:retinue:actor:ara"),
        "tags": lit(", ".join(tags)),
        "reiterations": lit(reiterations),
        "model": lit(model),
    }
    if relevance is not None:
        r["relevance"] = lit(relevance)
    if gen is not None:
        r["gen"] = lit(gen)
        r["coversFrom"] = lit(days_ago(60))
        r["coversTo"] = lit(days_ago(30))
    if expires:
        r["expires"] = lit(expires)
    if reiterations:
        r["lastReiterated"] = lit(days_ago(16))
    for key, ids in (("correctedBy", corrected), ("supersededBy", superseded),
                     ("questionedBy", questioned)):
        if ids:
            r[key] = lit(", ".join(PFX + i for i in ids))
    return r


class FakeStore:
    """The gate query returns `rows`; a VALUES-bounded query (the challengers)
    returns the matching `extra` rows. Records every query."""

    def __init__(self, rows=(), extra=(), down=False):
        self.rows, self.extra, self.down = list(rows), list(extra), down
        self.queries = []

    def query(self, sparql):
        self.queries.append(sparql)
        if self.down:
            raise OSError("connection refused")
        if "VALUES ?m" in sparql:
            return [r for r in self.extra if f"<{r['m']['value']}>" in sparql]
        return list(self.rows)


def member(entry_id, t, tags):
    """A payload member as the clustering functions see it."""
    return {"id": entry_id, "recorded_at": t, "tags": list(tags)}


@contextlib.contextmanager
def harness(mod, store, env=None, spawn=None):
    """Fake store, temp state and memory directories, a clean tier
    environment, a recording spawn and a recording alert push."""
    tmp = Path(tempfile.mkdtemp(prefix="memory-compact-test-"))
    saved = {k: os.environ.get(k) for k in ENV_VARS}
    for k in ENV_VARS:
        os.environ.pop(k, None)
    os.environ.update(env or {})
    mem = mod.memory
    old = (mem._query, mem._now, mem.MEMORY_DIR, mod.STATE_DIR,
           mod.run_session, mod.push_alert)
    rec = {"spawns": [], "alerts": [], "tmp": tmp}

    def fake_spawn(cmd, session_env):
        rec["spawns"].append({"cmd": cmd, "env": session_env})
        return spawn(rec, cmd, session_env) if spawn else (1, "boom")

    def fake_push(n, reason):
        rec["alerts"].append((n, reason))
        return True

    mem._query = store.query
    mem._now = lambda: NOW
    mem.MEMORY_DIR = tmp / "memory"
    mem.MEMORY_DIR.mkdir()
    mod.STATE_DIR = tmp / "state"
    mod.run_session = fake_spawn
    mod.push_alert = fake_push
    try:
        yield rec
    finally:
        (mem._query, mem._now, mem.MEMORY_DIR, mod.STATE_DIR,
         mod.run_session, mod.push_alert) = old
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def run_main(mod, argv=()):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mod.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def write_summary(rec, *ids):
    """What a successful session leaves behind: one compaction file."""
    lines = [f"<{PFX}{i}> <{RDF_TYPE}> <{KB}MemorySummary> ." for i in ids]
    path = rec["tmp"] / "memory" / f"compaction-test-{len(list((rec['tmp'] / 'memory').iterdir()))}.nt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cluster_rows(tag, n, start=20, prefix=None):
    """n eligible rows tagged `tag`, oldest first, older than the freeze age."""
    prefix = prefix or tag
    return [row(f"2026090{1 + i // 10}T0{i % 10}0000Z-{prefix}{i:02d}",
                days_ago(start + n - i), [tag]) for i in range(n)]


# ---------------------------------------------------------------- gate query


def test_gate_query(mod):
    print("gate query: eligibility is the store's half")
    q = mod.eligible_sparql(NOW)
    check("freeze cutoff is 14 days before now",
          'FILTER(?t < "2026-09-23T12:00:00Z"^^xsd:dateTime)' in q, True)
    check("compacted into a standing summary is excluded",
          "kb:compactedInto ?m_summary" in q and "?m_summary kb:correctedBy" in q, True)
    check("corrected entries are not excluded (they are retired)",
          "FILTER NOT EXISTS { ?m kb:correctedBy" in q, False)
    check("superseded entries are not excluded",
          "FILTER NOT EXISTS { ?m kb:supersededBy" in q, False)
    check("expired entries are not excluded", "?m_expires <" in q, False)
    check("no limit", "LIMIT" in q, False)
    check("it is memory.py's recall query (one definition)",
          "GROUP_CONCAT(DISTINCT STR(?cb)" in q and "ORDER BY DESC(?gen) DESC(?t)" in q,
          True)

    knob = load({"RETINUE_MEMORY_FREEZE_DAYS": "30", "RETINUE_MEMORY_MIN_CLUSTER": "3",
                 "RETINUE_MEMORY_BATCH": "7", "RETINUE_MEMORY_MAX_BATCHES": "2",
                 "RETINUE_MEMORY_ALERT_AFTER": "5"})
    check("env knobs read", (knob.FREEZE_DAYS, knob.MIN_CLUSTER, knob.BATCH_SIZE,
                             knob.MAX_BATCHES, knob.ALERT_AFTER), (30, 3, 7, 2, 5))
    check("freeze knob moves the cutoff",
          '"2026-09-07T12:00:00Z"' in knob.eligible_sparql(NOW), True)
    bad = load({"RETINUE_MEMORY_MIN_CLUSTER": "zero", "RETINUE_MEMORY_BATCH": "-4"})
    check("unreadable or non-positive knobs fall back",
          (bad.MIN_CLUSTER, bad.BATCH_SIZE), (5, 40))

    # Rows come back as payload members, deduplicated, oldest first.
    st = FakeStore(rows=[
        row("b", days_ago(20), ["x"]),
        row("a", days_ago(30), ["x", "y"], relevance="0.7"),
        row("a", days_ago(30), ["x", "y"], relevance="0.3"),
    ])
    with harness(mod, st):
        entries = mod.eligible_entries(NOW)
    check("deduplicated, oldest first", [e["id"] for e in entries], ["a", "b"])
    check("first row wins", entries[0]["relevance"], 0.7)


# ---------------------------------------------------------------- clustering


def test_clusters(mod):
    print("clustering: one cluster per entry")
    # Seven entries tagged insurance, three of them also deadline; four more
    # tagged deadline only. Raw counts: insurance 7, deadline 7 -> the tie
    # goes to "deadline" alphabetically, which takes all seven of its entries;
    # insurance keeps the four it has left, below the minimum.
    ins = [member(f"i{n}", days_ago(30 - n), ["insurance"]) for n in range(4)]
    both = [member(f"b{n}", days_ago(25 - n), ["insurance", "deadline"]) for n in range(3)]
    dl = [member(f"d{n}", days_ago(20 - n), ["deadline"]) for n in range(4)]
    clusters = mod.assign_clusters(ins + both + dl, 5)
    check("tie broken alphabetically, insurance left below the minimum",
          [(t, len(m)) for t, m in clusters], [("deadline", 7)])

    # Most members wins: the shared entries join the larger tag.
    ins2 = ins + [member("i9", days_ago(40), ["insurance"])]
    clusters = mod.assign_clusters(ins2 + both + dl[:3], 5)
    check("larger tag takes the shared entries",
          [(t, sorted(e["id"] for e in m)) for t, m in clusters],
          [("insurance", ["b0", "b1", "b2", "i0", "i1", "i2", "i3", "i9"])])

    # Each entry once, even when both tags form a cluster.
    many = ([member(f"x{n}", days_ago(30), ["alpha", "beta"]) for n in range(5)]
            + [member(f"y{n}", days_ago(30), ["beta"]) for n in range(5)]
            + [member(f"z{n}", days_ago(30), ["alpha"]) for n in range(5)])
    clusters = mod.assign_clusters(many, 5)
    ids = [e["id"] for _, m in clusters for e in m]
    check("both tags clustered, alpha first (tie, 10 each)",
          [(t, len(m)) for t, m in clusters], [("alpha", 10), ("beta", 5)])
    check("every entry exactly once", sorted(ids), sorted(e["id"] for e in many))

    # Greedy recount: a tag's raw count includes entries a larger tag takes.
    # "gamma" has 5 raw, but 3 of them go to the 6-strong "delta" first, so
    # gamma is never built below the minimum.
    g = ([member(f"g{n}", days_ago(30), ["gamma", "delta"]) for n in range(3)]
         + [member(f"h{n}", days_ago(30), ["gamma"]) for n in range(2)]
         + [member(f"k{n}", days_ago(30), ["delta"]) for n in range(3)])
    check("clusters are counted after earlier ones took their members",
          [(t, len(m)) for t, m in mod.assign_clusters(g, 5)], [("delta", 6)])

    # Identifier tags never form a cluster.
    senders = [member(f"s{n}", days_ago(30), ["sender-example-com"]) for n in range(8)]
    check("identifier-only entries join no cluster",
          mod.assign_clusters(senders, 5), [])
    mixed = [member(f"m{n}", days_ago(30), ["sender-example-com", "billing"])
             for n in range(5)]
    check("identifier plus topic clusters under the topic",
          [t for t, _ in mod.assign_clusters(mixed + senders, 5)], ["billing"])
    check("untagged entries join nothing",
          mod.assign_clusters([member("u", days_ago(30), [])] * 6, 5), [])

    print("batches: minimum, size, oldest first, cap")
    four = [member(f"f{n}", days_ago(30), ["solo"]) for n in range(4)]
    check("a cluster below the minimum waits", mod.assign_clusters(four, 5), [])
    fortyfive = [member(f"e{n:02d}", days_ago(100 - n), ["big"]) for n in range(45)]
    batches = mod.split_batches(list(reversed(fortyfive)), 40, 5)
    check("45 -> 40 + 5", [len(b) for b in batches], [40, 5])
    check("first batch holds the 40 oldest",
          [m["id"] for m in batches[0]], [f"e{n:02d}" for n in range(40)])
    check("43 -> 40, the newest 3 wait",
          [len(b) for b in mod.split_batches(fortyfive[:43], 40, 5)], [40])

    a = [member(f"a{n:02d}", days_ago(200 - n), ["alpha"]) for n in range(85)]
    b = [member(f"b{n:02d}", days_ago(100 - n), ["beta"]) for n in range(10)]
    planned, deferred = mod.plan_batches(a + b, 2)
    check("round-robin under the cap: every cluster's first batch first",
          [(p["topic"], p["batch"], len(p["members"])) for p in planned],
          [("alpha", 1, 40), ("beta", 1, 10)])
    check("the rest is deferred",
          [(p["topic"], p["batch"], len(p["members"])) for p in deferred],
          [("alpha", 2, 40), ("alpha", 3, 5)])
    check("batch numbering and cluster size",
          (planned[0]["of"], planned[0]["cluster_size"]), (3, 85))
    planned, deferred = mod.plan_batches(a + b, 10)
    check("no cap hit: four batches, nothing deferred", (len(planned), deferred), (4, []))


def test_tags(mod):
    print("suggested tags")
    ms = [member("1", days_ago(30), ["ludmila", "signal", "photo"]),
          member("2", days_ago(30), ["ludmila", "signal", "sender-ludmila-example-org"]),
          member("3", days_ago(30), ["ludmila", "voice"])]
    check("tags occurring twice plus the topic",
          mod.suggested_tags("ludmila", ms), ["ludmila", "signal"])
    check("the topic is always there",
          mod.suggested_tags("zzz", ms[:1]), ["zzz"])
    planned, _ = mod.plan_batches(ms + [member(str(n), days_ago(30), ["ludmila"])
                                        for n in range(4, 7)], 10)
    check("identifier tags listed for the batch",
          planned[0]["identifier_tags"], ["sender-ludmila-example-org"])


# ---------------------------------------------------------------- main


def eligible_store():
    """Six eligible insurance entries: one corrected, one questioned, one
    expired, one a previous summary; plus three that cluster nowhere."""
    rows = cluster_rows("insurance", 6)
    rows[0] = row("20260801T090000Z-aaaaaa", days_ago(60), ["insurance", "deadline"],
                  "IV filing due on the 8th; Ludmila files it.",
                  corrected=["20261001T090000Z-cccccc"])
    rows[1] = row("20260802T090000Z-bbbbbb", days_ago(59), ["insurance", "sender-iv-ch"],
                  "Rule: answer the IV by letter, never by e-mail.",
                  questioned=["20261002T090000Z-qqqqqq"], reiterations=3,
                  relevance="1.0")
    rows[2] = row("20260803T090000Z-dddddd", days_ago(58), ["insurance"],
                  "Reply to the insurer queued for approval.",
                  expires=days_ago(50))
    rows[3] = row("20260804T090000Z-eeeeee", days_ago(40), ["insurance", "deadline"],
                  "Insurance summary, generation 1.", gen=1)
    rows += [row("20260805T090000Z-ffffff", days_ago(30), ["garden"]),
             row("20260806T090000Z-gggggg", days_ago(30), ["sender-x-example-com"]),
             row("20260807T090000Z-hhhhhh", days_ago(30), ["garden"])]
    extra = [row("20261001T090000Z-cccccc", days_ago(6), ["insurance"],
                 "The filing is due on the 15th, not the 8th."),
             row("20261002T090000Z-qqqqqq", days_ago(5), ["insurance"],
                 "The IV now accepts e-mail; the letter rule may be obsolete.")]
    return FakeStore(rows=rows, extra=extra)


def test_nothing_to_do(mod):
    print("main: nothing to do")
    st = FakeStore(rows=cluster_rows("insurance", 4) + cluster_rows("garden", 3))
    with harness(mod, st) as rec:
        rc, out, err = run_main(mod)
        state_written = (rec["tmp"] / "state" / "state.json").exists()
    check("exit 0", rc, 0)
    check("nothing spawned", rec["spawns"], [])
    check("one log line", err.strip().count("\n"), 0)
    check("says why", "no topic with 5+" in err and "nothing spawned" in err, True)
    check("no state written", state_written, False)
    check("one query only (the gate)", len(st.queries), 1)

    with harness(mod, FakeStore(), {"RETINUE_MEMORY": "0"}) as rec:
        rc, _, err = run_main(mod)
    check("memory disabled: exit 0, no query, no spawn",
          (rc, rec["spawns"]), (0, []))


def test_payload(mod):
    print("payload: what the session is handed")
    st = eligible_store()
    with harness(mod, st, {"RETINUE_FRONTIER_MODEL": "claude-opus-5"},
                 spawn=lambda rec, cmd, env: (write_summary(rec, "S1"), (0, "ok"))[1]) as rec:
        rc, _, err = run_main(mod)
        payload = json.loads((rec["tmp"] / "state" / "compaction-payload.json")
                             .read_text(encoding="utf-8"))
        state = json.loads((rec["tmp"] / "state" / "state.json").read_text("utf-8")) \
            if (rec["tmp"] / "state" / "state.json").exists() else None
    check("clean run exits 0", rc, 0)
    check("logs the summary ids written", "wrote 1 of 1 summar(y/ies): S1" in err, True)
    check("logs the session's reply", "session: ok" in err, True)
    check("a clean run leaves the failure state empty", state, {})
    check("one batch", len(payload["batches"]), 1)
    b = payload["batches"][0]
    check("topic", b["topic"], "insurance")
    check("all six members, oldest first",
          [m["id"] for m in b["members"]][:4],
          ["20260801T090000Z-aaaaaa", "20260802T090000Z-bbbbbb",
           "20260803T090000Z-dddddd", "20260804T090000Z-eeeeee"])
    check("six members", len(b["members"]), 6)
    m0, m1, m2, m3 = b["members"][:4]
    check("member fields", sorted(m0), sorted([
        "id", "content", "tags", "recorded_at", "actor", "relevance", "model",
        "reiterations", "last_reiterated", "expires", "expired", "corrected_by",
        "superseded_by", "questioned_by", "summary", "generation", "covers_from",
        "covers_to"]))
    check("ids are bare", m0["id"].startswith("urn:"), False)
    check("corrected_by, bare", m0["corrected_by"], ["20261001T090000Z-cccccc"])
    check("questioned member with its reiterations",
          (m1["questioned_by"], m1["reiterations"], m1["relevance"]),
          (["20261002T090000Z-qqqqqq"], 3, 1.0))
    check("expired flagged", (m2["expired"], m0["expired"]), (True, False))
    check("previous summary: generation and coverage",
          (m3["summary"], m3["generation"], m3["covers_from"] is not None),
          (True, 1, True))
    check("challengers' content included", b["challengers"], {
        "20261001T090000Z-cccccc": {
            "content": "The filing is due on the 15th, not the 8th.",
            "recorded_at": days_ago(6), "tags": ["insurance"]},
        "20261002T090000Z-qqqqqq": {
            "content": "The IV now accepts e-mail; the letter rule may be obsolete.",
            "recorded_at": days_ago(5), "tags": ["insurance"]},
    })
    check("suggested tags", b["suggested_tags"], ["deadline", "insurance"])
    check("identifier tags", b["identifier_tags"], ["sender-iv-ch"])
    check("thresholds", payload["thresholds"], {
        "freeze_days": 14, "frozen_before": "2026-09-23T12:00:00Z",
        "min_cluster": 5, "batch_size": 40, "max_batches": 10})
    check("plan path in the state dir",
          payload["plan_path"].endswith("compaction-plan.json"), True)
    check("compact command names the plan",
          payload["compact_command"],
          f"python3 /workspace/scripts/memory.py compact --plan {payload['plan_path']}")
    check("challengers fetched with one bounded query",
          sum("VALUES ?m" in q for q in st.queries), 1)

    cmd = rec["spawns"][0]["cmd"]
    check("claude -p with JSON output", cmd[:3], ["claude", "-p", "--output-format=json"])
    check("tools named", cmd[cmd.index("--allowed-tools") + 1], "Bash,Read,Write")
    check("prompt after --", cmd[-2], "--")
    prompt = cmd[-1]
    check("prompt names payload and plan",
          "compaction-payload.json" in prompt and "compaction-plan.json" in prompt, True)
    check("prompt carries the rules",
          all(s in prompt for s in ("Never invent", "language most of",
                                    "exactly one of the two", "1.0", "0.7", "0.3",
                                    "suggested_tags", "doubt", "Never pass --force")),
          True)


def test_model(mod):
    print("frontier model resolution")
    check("frontier wins", mod.frontier_model(
        {"RETINUE_FRONTIER_MODEL": " claude-opus-5 ", "RETINUE_CLAUDE_MODEL": "x"}),
        "claude-opus-5")
    check("falls back to RETINUE_CLAUDE_MODEL",
          mod.frontier_model({"RETINUE_CLAUDE_MODEL": "claude-sonnet-5",
                              "RETINUE_ROUTER_MODEL": "claude-haiku-4"}),
          "claude-sonnet-5")
    check("neither: empty (no --model flag)", mod.frontier_model({}), "")

    ok = lambda rec, cmd, env: (write_summary(rec, "S1"), (0, ""))[1]  # noqa: E731
    with harness(mod, eligible_store(), {"RETINUE_FRONTIER_MODEL": "claude-opus-5",
                                         "RETINUE_ROUTER_MODEL": "claude-haiku-4"},
                 spawn=ok) as rec:
        run_main(mod)
    s = rec["spawns"][0]
    check("--model frontier", s["cmd"][s["cmd"].index("--model") + 1], "claude-opus-5")
    check("session stamped frontier", s["env"].get("RETINUE_SESSION_MODEL"), "claude-opus-5")
    check("compact will accept it", mod.memory.session_tier(s["env"]),
          mod.memory.FRONTIER)

    with harness(mod, eligible_store(), {"RETINUE_CLAUDE_MODEL": "claude-sonnet-5"},
                 spawn=ok) as rec:
        run_main(mod)
    s = rec["spawns"][0]
    check("fallback model used", s["cmd"][s["cmd"].index("--model") + 1], "claude-sonnet-5")

    with harness(mod, eligible_store(), {}, spawn=ok) as rec:
        rc, _, _ = run_main(mod)
    s = rec["spawns"][0]
    check("untiered: no --model, no stamp, still spawned",
          ("--model" in s["cmd"], "RETINUE_SESSION_MODEL" in s["env"], rc),
          (False, False, 0))

    with harness(mod, eligible_store(), {"RETINUE_ROUTER_MODEL": "claude-haiku-4"},
                 spawn=ok) as rec:
        rc, _, err = run_main(mod)
    check("router tier only: no frontier, refused before spawning",
          (rc, rec["spawns"], "no frontier model" in err), (1, [], True))


def test_outcomes(mod):
    print("outcomes: partial, empty, failure streak")
    env = {"RETINUE_FRONTIER_MODEL": "claude-opus-5"}
    big = FakeStore(rows=cluster_rows("alpha", 50) + cluster_rows("beta", 6))
    mod.MAX_BATCHES, saved = 1, mod.MAX_BATCHES
    try:
        with harness(mod, big, env,
                     spawn=lambda rec, cmd, e: (write_summary(rec, "S9"), (0, ""))[1]) as rec:
            rc, _, err = run_main(mod)
    finally:
        mod.MAX_BATCHES = saved
    check("cap cut batches: exit 75 (partial)", rc, 75)
    check("says it resumes", "deferred by the cap" in err, True)

    with harness(mod, eligible_store(), env, spawn=lambda rec, cmd, e: (0, "done")) as rec:
        rc, _, err = run_main(mod)
        state = json.loads((rec["tmp"] / "state" / "state.json").read_text("utf-8"))
    check("clean exit without a summary is a failure", rc, 1)
    check("counted", state["consecutive_failures"], 1)
    check("not in flight any more", state.get("in_flight"), None)

    # Three failing runs in a row: the alert goes out once, on the third.
    with harness(mod, eligible_store(), env) as rec:
        rcs = [run_main(mod)[0] for _ in range(4)]
        state = json.loads((rec["tmp"] / "state" / "state.json").read_text("utf-8"))
        alerts_after_four = list(rec["alerts"])
        # A success resets the streak.
        rec_spawn = mod.run_session
        mod.run_session = lambda cmd, e: (write_summary(rec, "S2"), (0, ""))[1]
        rc_ok = run_main(mod)[0]
        state_after = json.loads((rec["tmp"] / "state" / "state.json").read_text("utf-8"))
        mod.run_session = rec_spawn
        rcs_again = [run_main(mod)[0] for _ in range(3)]
        alerts_after_new_streak = list(rec["alerts"])
    check("failed runs exit non-zero", rcs, [1, 1, 1, 1])
    check("one alert, on the third failure", alerts_after_four, [(3, "session exited 1")])
    check("counter at four", state["consecutive_failures"], 4)
    check("success resets the state", (rc_ok, state_after), (0, {}))
    check("a new streak alerts again on its third failure",
          alerts_after_new_streak, [(3, "session exited 1"), (3, "session exited 1")])

    # The gate failing counts as well.
    with harness(mod, FakeStore(down=True), env) as rec:
        rcs = [run_main(mod)[0] for _ in range(3)]
    check("store down: failures, alert on the third",
          (rcs, [n for n, _ in rec["alerts"]], rec["spawns"]), ([1, 1, 1], [3], []))

    # A run the scheduler's timeout killed never reported back; the next run
    # counts it (once) and may alert for it.
    with harness(mod, FakeStore(rows=cluster_rows("x", 2)), env) as rec:
        (rec["tmp"] / "state").mkdir()
        (rec["tmp"] / "state" / "state.json").write_text(json.dumps(
            {"consecutive_failures": 3, "in_flight": True}), encoding="utf-8")
        rc, _, err = run_main(mod)
        state_file = rec["tmp"] / "state" / "state.json"
        state = json.loads(state_file.read_text("utf-8")) if state_file.exists() else {}
    check("killed run: alert for the third failure",
          [n for n, _ in rec["alerts"]], [3])
    check("killed run: the next clean gate resets the streak", (rc, state), (0, {}))


def test_alert_push(mod):
    print("alert push")
    calls = []

    class FakeSubprocess:
        CalledProcessError = __import__("subprocess").CalledProcessError

        @staticmethod
        def run(cmd, check=False):
            calls.append(cmd)

    real = mod.subprocess
    mod.subprocess = FakeSubprocess
    mod.memory._now, saved_now = (lambda: NOW), mod.memory._now
    try:
        ok = mod.push_alert(3, "session exited 1")
    finally:
        mod.subprocess = real
        mod.memory._now = saved_now
    cmd = calls[0]
    check("pushed", ok, True)
    check("through conversation-push.py", cmd[1].endswith("conversation-push.py"), True)
    check("titled", cmd[cmd.index("--title") + 1], "Memory compaction failing")
    check("keyed", cmd[cmd.index("--key") + 1],
          "memory-compact-failing-2026-10-07T12:00:00Z")
    msg = cmd[-1]
    check("message: count, reason, chips",
          ("3 runs in a row" in msg, "session exited 1" in msg,
           msg.count("[[chip:")), (True, True, 2))


def main():
    mod = load()
    for t in (test_gate_query, test_clusters, test_tags, test_nothing_to_do,
              test_payload, test_model, test_outcomes, test_alert_push):
        t(mod)
    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
