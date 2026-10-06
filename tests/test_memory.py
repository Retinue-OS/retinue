#!/usr/bin/env python3
"""Checks for the session memory CLI (scripts/memory.py).

No store and no network: `_query` and `_ask` are the module's only network
functions, and every case replaces them with a fake that answers from canned
SPARQL JSON bindings and records the queries it was sent. Entries are written
into a temporary RETINUE_MEMORY_DIR. What is pinned here is everything the
store cannot check for us — which writes a guard refuses, which exclusions a
recall asks for, and the exact triples a compaction leaves behind — because
getting those wrong either loses memories silently or floods every dispatch
prompt with stale ones.

    python3 tests/test_memory.py
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
SCRIPT = REPO_ROOT / "scripts" / "memory.py"

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
PFX = "urn:retinue:memory:"

TIER_VARS = ("RETINUE_FRONTIER_MODEL", "RETINUE_ROUTER_MODEL", "RETINUE_CLAUDE_MODEL",
             "RETINUE_SESSION_MODEL", "RETINUE_MEMORY", "RETINUE_MEMORY_SESSION",
             "RETINUE_MEMORY_ACTOR")


def load():
    spec = importlib.util.spec_from_file_location("memory", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
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


class FakeStore:
    """Answers memory.py's queries by their shape and records them.

    tags:      tag -> count (the tag guard and the `tags` subcommand)
    entries:   (id, content) live entries returned to the duplicate guard
    rows:      canned recall bindings
    facts:     id -> {"t", "gen", "from", "to"} for compaction members
    compacted: id -> [summary ids] already standing
    known:     ids an ASK reports as existing (None: every id exists)
    down:      every call raises, as an unreachable store would
    """

    def __init__(self, tags=None, entries=(), rows=(), facts=None,
                 compacted=None, known=None, down=False):
        self.tags = dict(tags or {})
        self.entries = list(entries)
        self.rows = list(rows)
        self.facts = dict(facts or {})
        self.compacted = dict(compacted or {})
        self.known = known
        self.down = down
        self.queries = []
        self.asks = []

    def query(self, sparql):
        self.queries.append(sparql)
        if self.down:
            raise OSError("connection refused")
        if "AS ?n)" in sparql:
            return [{"tag": lit(t), "n": lit(n)} for t, n in self.tags.items()]
        if "SELECT DISTINCT ?m ?content" in sparql:
            return [{"m": uri(PFX + i), "content": lit(c)} for i, c in self.entries]
        if "SELECT ?m ?summary" in sparql:
            return [{"m": uri(PFX + i), "summary": uri(PFX + s)}
                    for i, ss in self.compacted.items() for s in ss if i in sparql]
        if "SELECT ?m ?t ?gen ?from ?to" in sparql:
            out = []
            for i, f in self.facts.items():
                if i not in sparql:
                    continue
                row = {"m": uri(PFX + i), "t": lit(f["t"])}
                for key in ("gen", "from", "to"):
                    if f.get(key):
                        row[key] = lit(f[key])
                out.append(row)
            return out
        return list(self.rows)

    def ask(self, sparql):
        self.asks.append(sparql)
        if self.down:
            raise OSError("connection refused")
        if self.known is None:
            return True
        return any(f"<{PFX}{i}>" in sparql for i in self.known)


TWENTY_TAGS = {f"topic-{n:02d}": 1 for n in range(18)}
TWENTY_TAGS.update({"insurance": 9, "signal": 4, "ludmila": 3})


def run(mod, argv, store, env=None, tmp=None):
    """Run memory.py's main with the fake store and a clean tier environment.
    Returns (exit code, stdout, stderr)."""
    saved = {k: os.environ.get(k) for k in TIER_VARS}
    for k in TIER_VARS:
        os.environ.pop(k, None)
    os.environ.update(env or {})
    mod._query, mod._ask = store.query, store.ask
    mod._now = lambda: NOW
    if tmp is not None:
        mod.MEMORY_DIR = Path(tmp)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod.main(argv)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return rc, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def tempdir():
    d = tempfile.mkdtemp(prefix="memory-test-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def nt_files(d):
    return sorted(p for p in Path(d).iterdir() if p.suffix == ".nt")


LOWER_ENV = {"RETINUE_FRONTIER_MODEL": "claude-opus-5",
             "RETINUE_ROUTER_MODEL": "claude-haiku-4",
             "RETINUE_SESSION_MODEL": "claude-haiku-4"}
FRONTIER_ENV = dict(LOWER_ENV, RETINUE_SESSION_MODEL="anthropic/claude-opus-5")

RULE = "Ludmila prefers Signal voice notes over text messages for anything longer"


# ---------------------------------------------------------------- pure helpers


def test_parse_expires(mod):
    print("parse_expires")
    p = lambda s: mod.parse_expires(s, NOW)  # noqa: E731
    check("a date means the end of that day",
          p("2026-10-20"), dt.datetime(2026, 10, 20, 23, 59, 59, tzinfo=UTC))
    check("a dateTime in UTC", p("2026-10-20T08:00:00Z"),
          dt.datetime(2026, 10, 20, 8, 0, 0, tzinfo=UTC))
    check("a dateTime with an offset is converted", p("2026-10-20T10:00:00+02:00"),
          dt.datetime(2026, 10, 20, 8, 0, 0, tzinfo=UTC))
    check("a dateTime without an offset is UTC", p("2026-10-20T08:00:00"),
          dt.datetime(2026, 10, 20, 8, 0, 0, tzinfo=UTC))
    check("bare number is days", p("10"), NOW + dt.timedelta(days=10))
    check("explicit days", p("10d"), NOW + dt.timedelta(days=10))
    check("weeks", p("2w"), NOW + dt.timedelta(weeks=2))
    check("calendar months", p("3m"), dt.datetime(2027, 1, 6, 12, 0, 0, tzinfo=UTC))
    check("spaced and upper case", p(" 2 W "), NOW + dt.timedelta(weeks=2))
    check("months clamp to a valid day",
          mod.parse_expires("1m", dt.datetime(2026, 1, 31, tzinfo=UTC)),
          dt.datetime(2026, 2, 28, tzinfo=UTC))
    for bad in ("soon", "3y", "-3d", "2026-13-01", "2026-10-20T25:00:00Z", ""):
        check(f"rejects {bad!r}", p(bad), None)


def test_normalize_model(mod):
    print("normalize_model")
    check("provider prefix dropped", mod.normalize_model("anthropic/claude-opus-5"),
          "claude-opus-5")
    check("lowercased and stripped", mod.normalize_model(" Claude-Opus-5 "),
          "claude-opus-5")
    check("only the first slash", mod.normalize_model("bedrock/eu/opus"), "eu/opus")
    check("an alias stays itself", mod.normalize_model("opus"), "opus")
    check("empty", mod.normalize_model(""), "")
    check("None", mod.normalize_model(None), "")


def test_session_tier(mod):
    print("session_tier")
    F, L = mod.FRONTIER, mod.LOWER
    check("untiered deployment is frontier", mod.session_tier({}), F)
    check("untiered even without a stamp",
          mod.session_tier({"RETINUE_SESSION_MODEL": "claude-haiku-4"}), F)
    check("frontier match across prefix spellings",
          mod.session_tier(FRONTIER_ENV), F)
    check("frontier match across case and prefix on the variable",
          mod.session_tier(dict(LOWER_ENV, RETINUE_FRONTIER_MODEL="Anthropic/Claude-Opus-5",
                                RETINUE_SESSION_MODEL="claude-opus-5")), F)
    check("router session is lower", mod.session_tier(LOWER_ENV), L)
    check("a job's own model is lower",
          mod.session_tier(dict(LOWER_ENV, RETINUE_SESSION_MODEL="sonnet")), L)
    no_stamp = {k: v for k, v in LOWER_ENV.items() if k != "RETINUE_SESSION_MODEL"}
    check("a missing stamp is lower (unknown is not trusted)",
          mod.session_tier(no_stamp), L)
    only_router = {"RETINUE_ROUTER_MODEL": "claude-haiku-4",
                   "RETINUE_CLAUDE_MODEL": "claude-opus-5",
                   "RETINUE_SESSION_MODEL": "claude-opus-5"}
    check("frontier falls back to RETINUE_CLAUDE_MODEL",
          mod.session_tier(only_router), F)
    check("only the environment counts; a --model naming the frontier is a stamp",
          mod.session_tier(dict(LOWER_ENV, RETINUE_SESSION_MODEL="claude-haiku-4")), L)


def test_word_overlap(mod):
    print("word_overlap")
    check("identical", mod.word_overlap(RULE, RULE), 1.0)
    check("disjoint", mod.word_overlap("alpha beta", "gamma delta"), 0.0)
    check("three of five words", mod.word_overlap("alpha beta gamma delta",
                                                  "alpha beta gamma epsilon"), 0.6)
    check("case does not matter", mod.word_overlap("Alpha BETA", "alpha beta"), 1.0)
    check("short words ignored", mod.word_overlap("an ox is at it", "an ox is at it"), 0.0)
    check("any script tokenizes", mod.word_overlap("Grüße aus Zürich", "zürich grüße"),
          2 / 3)
    check("empty side", mod.word_overlap("", RULE), 0.0)


def test_classify_and_closest(mod):
    print("classify_duplicates / closest_tags / sort_tag_counts")
    hits = [("a", RULE), ("b", RULE + " always"), ("c", "Ludmila prefers Signal voice notes"),
            ("d", "nothing in common at all")]
    near, similar = mod.classify_duplicates(RULE, hits, set())
    check("near-duplicates, best first", [h[0] for h in near], ["a", "b"])
    check("similar ones", [h[0] for h in similar], ["c"])
    near, _ = mod.classify_duplicates(RULE, hits, {"a"})
    check("a linked entry is not a duplicate", [h[0] for h in near], ["b"])
    counts = {"insurance": 9, "insurance-claims": 2, "signal": 4, "health": 1}
    hint = mod.closest_tags("insurence", counts)
    check("a typo finds its tag", hint[0], "insurance")
    check("a substring finds a more specific tag",
          "insurance-claims" in mod.closest_tags("claims", counts), True)
    check("nothing close", mod.closest_tags("zzz", counts), [])
    check("sorted by count, then name",
          mod.sort_tag_counts({"b": 2, "a": 2, "c": 5}), [("c", 5), ("a", 2), ("b", 2)])
    check("prefix is slugged like tags",
          mod.sort_tag_counts({"sender-x-com": 1, "signal": 2}, prefix="sender:"),
          [("sender-x-com", 1)])
    check("contains", mod.sort_tag_counts({"insurance": 1, "signal": 2}, contains="sur"),
          [("insurance", 1)])


def test_liveness(mod):
    print("liveness_patterns")
    pats = "\n".join(mod.liveness_patterns(now=NOW))
    check("corrected excluded", "?m kb:correctedBy" in pats, True)
    check("superseded excluded", "?m kb:supersededBy" in pats, True)
    check("expired excluded against an injected literal",
          '"2026-10-06T12:00:00Z"^^xsd:dateTime' in pats, True)
    check("no NOW()", "NOW()" in pats, False)
    check("compacted into an uncorrected summary excluded",
          "kb:compactedInto ?m_summary" in pats
          and "?m_summary kb:correctedBy" in pats, True)
    check("each flag lifts its exclusion",
          mod.liveness_patterns(include_superseded=True, include_expired=True,
                                include_compacted=True, now=NOW), [])
    other = "\n".join(mod.liveness_patterns("?x", now=NOW))
    check("variables follow the subject", "?x_expires" in other and "?m_" not in other,
          True)


# ---------------------------------------------------------------- store guards


def test_duplicate_guard(mod):
    print("store: duplicate guard")
    near = [("20260801T100000Z-aaaaaa", RULE)]
    content = RULE + " today"
    with tempdir() as d:
        st = FakeStore(tags=TWENTY_TAGS, entries=near)
        rc, _, err = run(mod, ["store", "--tag", "ludmila", content], st, LOWER_ENV, d)
        check("lower tier: refused", rc, 1)
        check("lower tier: nothing written", nt_files(d), [])
        check("lists the matched id", "20260801T100000Z-aaaaaa" in err, True)
        check("with an excerpt", "Ludmila prefers Signal" in err, True)
        check("offers reinforce", "reinforce 20260801T100000Z-aaaaaa" in err, True)
        check("offers the challenge flags", "--supersedes / --corrects / --questions" in err,
              True)
        check("offers escalation", "escalate to Ara senior" in err, True)
        check("does not offer the frontier flag", "--duplicate-ok" in err, False)
        check("the duplicate query spans every live entry with a shared tag",
              any("SELECT DISTINCT ?m ?content" in q and "kb:compactedInto" in q
                  and '"ludmila"' in q and "recordedAt ?t" not in q
                  for q in st.queries), True)

        rc, _, err = run(mod, ["store", "--tag", "ludmila", "--duplicate-ok", content],
                         FakeStore(tags=TWENTY_TAGS, entries=near), LOWER_ENV, d)
        check("lower tier: --duplicate-ok does not help", rc, 1)

        rc, _, err = run(mod, ["store", "--tag", "ludmila", "--supersedes",
                               "20260801T100000Z-aaaaaa", content],
                         FakeStore(tags=TWENTY_TAGS, entries=near), LOWER_ENV, d)
        check("a linked near-duplicate is intended", rc, 0)
        written = nt_files(d)[0].read_text(encoding="utf-8") if nt_files(d) else ""
        check("and the link is written",
              f"<{PFX}20260801T100000Z-aaaaaa> <{mod.KB}supersededBy>" in written, True)

    with tempdir() as d:
        rc, _, err = run(mod, ["store", "--tag", "ludmila", content],
                         FakeStore(tags=TWENTY_TAGS, entries=near), FRONTIER_ENV, d)
        check("frontier: refused without the flag", rc, 1)
        check("frontier: told about --duplicate-ok", "--duplicate-ok" in err, True)
        check("frontier: not told to escalate", "escalate" in err, False)
        rc, _, err = run(mod, ["store", "--tag", "ludmila", "--duplicate-ok", content],
                         FakeStore(tags=TWENTY_TAGS, entries=near), FRONTIER_ENV, d)
        check("frontier: allowed with --duplicate-ok", rc, 0)
        check("frontier: still warned", "near-duplicate of 20260801T100000Z-aaaaaa" in err,
              True)
        check("frontier: written", len(nt_files(d)), 1)

    with tempdir() as d:
        similar = [("20260801T100000Z-bbbbbb", "Ludmila Signal voice notes are fine")]
        rc, _, err = run(mod, ["store", "--tag", "ludmila", RULE],
                         FakeStore(tags=TWENTY_TAGS, entries=similar), LOWER_ENV, d)
        check("similar only: proceeds", rc, 0)
        check("similar only: warns", "similar to 20260801T100000Z-bbbbbb" in err, True)
        check("similar only: written", len(nt_files(d)), 1)

    with tempdir() as d:
        rc, _, err = run(mod, ["store", "--tag", "ludmila", content],
                         FakeStore(down=True), LOWER_ENV, d)
        check("unreachable store: proceeds", rc, 0)
        check("unreachable store: says so", "duplicates not checked" in err, True)


def test_new_tag_guard(mod):
    print("store: new-tag guard")
    with tempdir() as d:
        few = {"insurance": 3, "signal": 1}
        rc, _, _ = run(mod, ["store", "--tag", "brand-new", "fresh deployment fact"],
                       FakeStore(tags=few), LOWER_ENV, d)
        check("bootstrap: under 20 tags the guard is off", rc, 0)

    with tempdir() as d:
        rc, _, err = run(mod, ["store", "--tag", "insurence", "the claim was filed"],
                         FakeStore(tags=TWENTY_TAGS), LOWER_ENV, d)
        check("lower tier: a new tag is refused", rc, 1)
        check("lower tier: closest existing tag hinted", "insurance" in err, True)
        check("lower tier: points at `tags`", "memory.py tags" in err, True)
        check("lower tier: nothing written", nt_files(d), [])
        rc, _, _ = run(mod, ["store", "--tag", "insurence", "--new-tag", "the claim was filed"],
                       FakeStore(tags=TWENTY_TAGS), LOWER_ENV, d)
        check("lower tier: --new-tag does not help", rc, 1)
        rc, _, _ = run(mod, ["store", "--tag", "insurence", "--force", "the claim was filed"],
                       FakeStore(tags=TWENTY_TAGS), LOWER_ENV, d)
        check("--force is not a tag override", rc, 1)
        rc, _, _ = run(mod, ["store", "--tag", "sender:noreply@example.com",
                             "always name the receiving address"],
                       FakeStore(tags=TWENTY_TAGS), LOWER_ENV, d)
        check("identifier tags (sender:…) are exempt", rc, 0)

    with tempdir() as d:
        rc, _, err = run(mod, ["store", "--tag", "insurence", "the claim was filed"],
                         FakeStore(tags=TWENTY_TAGS), FRONTIER_ENV, d)
        check("frontier: refused without --new-tag", rc, 1)
        check("frontier: told about --new-tag", "--new-tag" in err, True)
        rc, _, err = run(mod, ["store", "--tag", "insurence", "--new-tag",
                               "the claim was filed"],
                         FakeStore(tags=TWENTY_TAGS), FRONTIER_ENV, d)
        check("frontier: allowed with --new-tag", rc, 0)
        check("frontier: hint printed as a warning",
              "warning: new tag 'insurence'" in err and "insurance" in err, True)

    with tempdir() as d:
        st = FakeStore(tags=TWENTY_TAGS, entries=[("x", RULE)])
        rc, _, _ = run(mod, ["store", "--tag", "nope-nope", RULE], st, LOWER_ENV, d)
        check("tags are checked before duplicates",
              (rc, any("SELECT DISTINCT ?m ?content" in q for q in st.queries)), (1, False))


def test_store_writes(mod):
    print("store: written triples")
    kb = mod.KB
    with tempdir() as d:
        env = dict(FRONTIER_ENV, RETINUE_MEMORY_SESSION="sess-1")
        rc, _, err = run(mod, ["store", "--tag", "Insurance", "--tag", "signal",
                               "--relevance", "0.3", "--expires", "2w",
                               "Mail to the insurer queued for approval"],
                         FakeStore(tags=TWENTY_TAGS), env, d)
        check("stored", rc, 0)
        files = nt_files(d)
        check("into the session's file", [p.name for p in files], ["sess-1.nt"])
        text = files[0].read_text(encoding="utf-8") if files else ""
        check("typed kb:Memory", f"<{kb}Memory> ." in text, True)
        check("expires two weeks out",
              f'<{kb}expires> "2026-10-20T12:00:00Z"^^<{mod.XSD}dateTime> .' in text, True)
        check("model stamp normalized", f'<{kb}model> "claude-opus-5" .' in text, True)
        check("tags slugged", f'<{kb}tag> "insurance" .' in text, True)
        check("relevance", f'<{kb}relevance> "0.3"^^<{mod.XSD}decimal> .' in text, True)
        check("recordedAt", f'<{kb}recordedAt> "2026-10-06T12:00:00Z"' in text, True)
        check("only the entry file and the store lock",
              sorted(p.name for p in Path(d).iterdir()), [".memory.lock", "sess-1.nt"])

    with tempdir() as d:
        rc, _, err = run(mod, ["store", "--tag", "insurance", "--expires", "soon", "x y z"],
                         FakeStore(tags=TWENTY_TAGS), FRONTIER_ENV, d)
        check("unreadable --expires refused", (rc, nt_files(d)), (1, []))
        rc, _, err = run(mod, ["store", "--tag", "insurance", "--expires", "2026-10-01",
                               "x y z"], FakeStore(tags=TWENTY_TAGS), FRONTIER_ENV, d)
        check("past --expires refused", (rc, "already past" in err), (1, True))
        rc, _, _ = run(mod, ["store", "--tag", "insurance", "a rule"],
                       FakeStore(tags=TWENTY_TAGS), {"RETINUE_MEMORY": "0"}, d)
        check("disabled: a no-op", (rc, nt_files(d)), (0, []))


# ---------------------------------------------------------------- recall


SUMMARY_ROW = {
    "m": uri(PFX + "20261005T120000Z-sum001"), "content": lit("Ludmila: Signal only."),
    "t": lit("2026-10-05T12:00:00Z"), "actor": uri("urn:retinue:actor:ara"),
    "relevance": lit("0.8"), "gen": lit("1"), "tags": lit("ludmila, signal"),
    "coversFrom": lit("2026-08-30T09:00:00Z"), "coversTo": lit("2026-09-20T18:00:00Z"),
    "summarizedCount": lit("8"), "retiredCount": lit("4"),
    "summarizes": lit(f"{PFX}a1, {PFX}a2"), "retires": lit(f"{PFX}r1"),
    "reiterations": lit("0"),
}
EXPIRED_ROW = {
    "m": uri(PFX + "20260901T080000Z-exp001"), "content": lit("Mail queued."),
    "t": lit("2026-09-01T08:00:00Z"), "actor": uri("urn:retinue:actor:ara"),
    "tags": lit("insurance"), "expires": lit("2026-09-03T23:59:59Z"),
    "reiterations": lit("0"),
}
COMPACTED_ROW = {
    "m": uri(PFX + "20260830T090000Z-cmp001"), "content": lit("Old rule."),
    "t": lit("2026-08-30T09:00:00Z"), "actor": uri("urn:retinue:actor:ara"),
    "tags": lit("ludmila"), "compactedInto": lit(PFX + "20261005T120000Z-sum001"),
    "reiterations": lit("0"),
}


def test_recall_query(mod):
    print("recall: query shape")
    st = FakeStore()
    rc, _, _ = run(mod, ["recall", "--tag", "ludmila"], st)
    q = st.queries[-1] if st.queries else ""
    check("ran", rc, 0)
    check("corrected excluded by default", "FILTER NOT EXISTS { ?m kb:correctedBy" in q, True)
    check("superseded excluded by default", "FILTER NOT EXISTS { ?m kb:supersededBy" in q,
          True)
    check("expired excluded by default", "FILTER NOT EXISTS { ?m kb:expires" in q, True)
    check("compacted excluded by default", "FILTER NOT EXISTS { ?m kb:compactedInto" in q,
          True)
    check("compacted-into reports standing summaries only",
          "OPTIONAL { ?m kb:compactedInto ?ci .\n"
          "             FILTER NOT EXISTS { ?ci kb:correctedBy ?ci_undone } }" in q,
          True)
    check("summaries first, then newest", "ORDER BY DESC(?gen) DESC(?t)" in q, True)
    check("?gen grouped", "GROUP BY ?m ?content ?t ?actor ?relevance ?model ?gen" in q, True)
    check("member counts", "COUNT(DISTINCT ?sm)" in q and "COUNT(DISTINCT ?rt)" in q, True)
    check("IRI concatenations wrapped in STR()",
          all(f"STR(?{v})" in q for v in ("cb", "sb", "qb", "sm", "rt", "ci")), True)
    check("default limit 20", "LIMIT 20" in q, True)

    for flag, gone in (("--include-superseded", "kb:correctedBy ?m_corrected"),
                       ("--include-expired", "kb:expires ?m_expires"),
                       ("--include-compacted", "kb:compactedInto ?m_summary")):
        st = FakeStore()
        run(mod, ["recall", flag], st)
        q = st.queries[-1]
        check(f"{flag} lifts its exclusion", gone in q, False)
        others = {"kb:correctedBy ?m_corrected", "kb:expires ?m_expires",
                  "kb:compactedInto ?m_summary"} - {gone}
        check(f"{flag} keeps the others", all(o in q for o in others), True)


def test_recall_render(mod):
    print("recall: rendering")
    st = FakeStore(rows=[SUMMARY_ROW, EXPIRED_ROW, COMPACTED_ROW])
    rc, out, _ = run(mod, ["recall", "--include-expired", "--include-compacted"], st)
    lines = out.splitlines()
    check("ran", rc, 0)
    check("summary row",
          lines[0], "- 2026-10-05 12:00:00Z (ara; ludmila, signal; relevance 0.8; "
                    "SUMMARY gen 1 of 12 entries, 4 retired, covers 2026-08-30..2026-09-20)"
                    " [20261005T120000Z-sum001]")
    check("content follows", lines[1], "  Ludmila: Signal only.")
    check("expired labeled", "EXPIRED 2026-09-03" in lines[2], True)
    check("compacted labeled", "COMPACTED into 20261005T120000Z-sum001" in lines[4], True)

    rc, out, _ = run(mod, ["recall", "--json"], FakeStore(rows=[SUMMARY_ROW, EXPIRED_ROW]))
    rows = json.loads(out)
    s, e = rows[0], rows[1]
    check("json: summary flag", (s["summary"], e["summary"]), (True, False))
    check("json: generation", (s["generation"], e["generation"]), (1, None))
    check("json: covers", (s["covers_from"], s["covers_to"]),
          ("2026-08-30T09:00:00Z", "2026-09-20T18:00:00Z"))
    check("json: member ids", (s["summarizes"], s["retires"]), (["a1", "a2"], ["r1"]))
    check("json: expires", e["expires"], "2026-09-03T23:59:59Z")
    check("json: compacted_into", e["compacted_into"], [])


def test_expand(mod):
    print("recall --expand")
    kept = dict(COMPACTED_ROW, role=lit("kept"))
    retired = dict(EXPIRED_ROW, role=lit("retired"))
    st = FakeStore(rows=[kept, retired])
    rc, out, _ = run(mod, ["recall", "--expand", "20261005T120000Z-sum001"], st)
    q = st.queries[-1]
    check("ran", rc, 0)
    check("kept members",
          f"<{PFX}20261005T120000Z-sum001> kb:summarizes ?m" in q, True)
    check("retired members", "UNION" in q and
          f"<{PFX}20261005T120000Z-sum001> kb:retires ?m" in q, True)
    check("no exclusions",
          any(v in q for v in ("?m_corrected", "?m_expires", "?m_summary")), False)
    check("no limit unless asked", "LIMIT" in q, False)
    check("role grouped", "?coversTo ?role" in q, True)
    lines = out.splitlines()
    check("kept label", lines[0].startswith("- 2026-08-30 09:00:00Z (kept; ara;"), True)
    check("retired label", lines[2].startswith("- 2026-09-01 08:00:00Z (retired; ara;"),
          True)
    st = FakeStore()
    rc, _, err = run(mod, ["recall", "--expand", "20261005T120000Z-sum001",
                           "--tag", "ludmila"], st)
    check("does not combine with filters", (rc, st.queries), (1, []))
    rc, _, _ = run(mod, ["recall", "--expand", "x", "--include-compacted"], FakeStore())
    check("nor with the include flags", rc, 1)
    rc, out, _ = run(mod, ["recall", "--expand", "s1", "--json"],
                     FakeStore(rows=[kept]))
    check("json carries the role", json.loads(out)[0]["role"], "kept")


# ---------------------------------------------------------------- tags


def test_tags(mod):
    print("tags")
    counts = {"signal": 4, "insurance": 9, "ludmila": 4, "sender-x-com": 1}
    st = FakeStore(tags=counts)
    rc, out, _ = run(mod, ["tags"], st)
    check("ran", rc, 0)
    check("count desc, then name", out.splitlines(),
          ["9  insurance", "4  ludmila", "4  signal", "1  sender-x-com"])
    check("live entries by default", "kb:compactedInto" in st.queries[-1], True)
    st = FakeStore(tags=counts)
    run(mod, ["tags", "--all"], st)
    check("--all counts everything", "FILTER NOT EXISTS" in st.queries[-1], False)
    rc, out, _ = run(mod, ["tags", "--prefix", "sender:", "--json"], FakeStore(tags=counts))
    check("--prefix and --json", json.loads(out), [{"tag": "sender-x-com", "count": 1}])
    rc, _, _ = run(mod, ["tags"], FakeStore(down=True))
    check("unreachable store fails", rc, 1)


# ---------------------------------------------------------------- compact


def plan_item(**kw):
    item = {"topic": "ludmila", "content": "Ludmila: Signal only, voice notes preferred.",
            "tags": ["ludmila", "signal"], "relevance": 0.8,
            "summarizes": ["20260830T090000Z-aaaaaa"], "retires": ["20260901T080000Z-bbbbbb"]}
    item.update(kw)
    return item


def test_validate_plan(mod):
    print("compact: plan validation")
    items, errors = mod.validate_plan(plan_item(topic="Ludmila Family", tags=["signal"]))
    check("a valid plan", errors, [])
    check("the topic joins the tags", items[0]["tags"], ["ludmila-family", "signal"])
    _, errors = mod.validate_plan([plan_item()])
    check("a list is fine", errors, [])

    def errs(plan):
        return mod.validate_plan(plan)[1]
    check("overlapping lists", any("both summarized and retired" in e for e in errs(
        plan_item(retires=["20260830T090000Z-aaaaaa"]))), True)
    check("no members", any("both empty" in e for e in errs(
        plan_item(summarizes=[], retires=[]))), True)
    check("empty content", any("content is empty" in e for e in errs(
        plan_item(content="  "))), True)
    check("no tags", any("at least one tag" in e for e in errs(
        plan_item(topic=None, tags=[]))), True)
    check("bad id", any("not a memory id" in e for e in errs(
        plan_item(summarizes=["../etc/passwd"]))), True)
    check("relevance out of range", any("relevance" in e for e in errs(
        plan_item(relevance=1.5))), True)
    check("relevance not a bool", any("relevance" in e for e in errs(
        plan_item(relevance=True))), True)
    check("unknown key", any("unknown keys" in e for e in errs(plan_item(retire=[]))), True)
    check("empty plan", errs([]), ["the plan holds no summaries"])
    check("not an object", errs("x"), ["the plan must be a JSON object or a list of objects"])
    check("an entry joins one summary only", any("already a member of summary 1" in e
          for e in errs([plan_item(), plan_item(retires=[])])), True)
    check("unreadable covers", any("covers_from" in e for e in errs(
        plan_item(covers_from="last week"))), True)
    check("covers in order", any("after covers_to" in e for e in errs(
        plan_item(covers_from="2026-09-10", covers_to="2026-09-01"))), True)
    check("all errors at once", len(errs(plan_item(content="", relevance=3,
                                                    summarizes=[], retires=[]))), 3)


def test_compact_writes(mod):
    print("compact: written file")
    kb, xsd = mod.KB, mod.XSD
    facts = {
        "20260830T090000Z-aaaaaa": {"t": "2026-08-30T09:00:00Z"},
        "20260901T080000Z-bbbbbb": {"t": "2026-09-01T08:00:00Z"},
        "20260915T080000Z-cccccc": {"t": "2026-09-15T08:00:00Z"},
        # A gen-1 summary written on 1 Sept covering August.
        "20260901T000000Z-sum000": {"t": "2026-09-01T00:00:00Z", "gen": "1",
                                    "from": "2026-08-01T07:00:00Z",
                                    "to": "2026-08-28T20:00:00Z"},
    }
    plan = [
        plan_item(),
        {"topic": "insurance", "content": "Insurance: the IV rules.", "tags": [],
         "summarizes": ["20260901T000000Z-sum000", "20260915T080000Z-cccccc"]},
    ]
    with tempdir() as d:
        planfile = d / "plan.json"
        planfile.write_text(json.dumps(plan), encoding="utf-8")
        st = FakeStore(facts=facts)
        rc, out, err = run(mod, ["compact", "--plan", str(planfile)], st,
                           {"RETINUE_SESSION_MODEL": "anthropic/claude-opus-5"}, d)
        check("compacted", rc, 0)
        ids = out.split()
        check("summary ids on stdout", len(ids), 2)
        files = nt_files(d)
        check("one compaction file", [p.name.startswith("compaction-") for p in files], [True])
        check("no .tmp left behind", [p.name for p in d.iterdir() if p.suffix == ".tmp"], [])
        text = files[0].read_text(encoding="utf-8") if files else ""
        s1, s2 = (f"<{PFX}{i}>" for i in ids)
        check("typed Memory", f"{s1} <{mod.RDF_TYPE}> <{kb}Memory> ." in text, True)
        check("typed MemorySummary", f"{s1} <{mod.RDF_TYPE}> <{kb}MemorySummary> ." in text,
              True)
        check("raw members: generation 1",
              f'{s1} <{kb}generation> "1"^^<{xsd}integer> .' in text, True)
        check("a summary member: generation 2",
              f'{s2} <{kb}generation> "2"^^<{xsd}integer> .' in text, True)
        check("covers from the members",
              f'{s1} <{kb}coversFrom> "2026-08-30T09:00:00Z"' in text
              and f'{s1} <{kb}coversTo> "2026-09-01T08:00:00Z"' in text, True)
        check("a summary member contributes its own coverage",
              f'{s2} <{kb}coversFrom> "2026-08-01T07:00:00Z"' in text
              and f'{s2} <{kb}coversTo> "2026-09-15T08:00:00Z"' in text, True)
        check("summarizes", f"{s1} <{kb}summarizes> <{PFX}20260830T090000Z-aaaaaa> ." in text,
              True)
        check("retires", f"{s1} <{kb}retires> <{PFX}20260901T080000Z-bbbbbb> ." in text, True)
        check("every member links back",
              all(f"<{PFX}{m}> <{kb}compactedInto> {s} ." in text for m, s in (
                  ("20260830T090000Z-aaaaaa", s1), ("20260901T080000Z-bbbbbb", s1),
                  ("20260901T000000Z-sum000", s2), ("20260915T080000Z-cccccc", s2))), True)
        check("nothing else written for a retired entry",
              [line for line in text.splitlines()
               if line.startswith(f"<{PFX}20260901T080000Z-bbbbbb>")],
              [f"<{PFX}20260901T080000Z-bbbbbb> <{kb}compactedInto> {s1} ."])
        check("topic tag", f'{s2} <{kb}tag> "insurance" .' in text, True)
        check("model normalized", f'{s1} <{kb}model> "claude-opus-5" .' in text, True)
        check("relevance only where given",
              (f"{s1} <{kb}relevance>" in text, f"{s2} <{kb}relevance>" in text),
              (True, False))

    with tempdir() as d:
        planfile = d / "plan.json"
        planfile.write_text(json.dumps(plan_item(covers_from="2026-08-01",
                                                 covers_to="2026-09-30")),
                            encoding="utf-8")
        rc, out, _ = run(mod, ["compact", "--plan", str(planfile)],
                         FakeStore(facts=facts), {}, d)
        text = nt_files(d)[0].read_text(encoding="utf-8") if rc == 0 else ""
        check("plan covers win",
              '"2026-08-01T00:00:00Z"' in text and '"2026-09-30T23:59:59Z"' in text, True)

    with tempdir() as d:
        planfile = d / "plan.json"
        planfile.write_text(json.dumps(plan_item()), encoding="utf-8")
        rc, _, err = run(mod, ["compact", "--plan", str(planfile)],
                         FakeStore(facts=facts,
                                   compacted={"20260830T090000Z-aaaaaa": ["old-sum"]}), {}, d)
        check("already compacted: refused", (rc, nt_files(d)), (1, []))
        check("already compacted: names the summary", "old-sum" in err, True)
        rc, _, err = run(mod, ["compact", "--plan", str(planfile)],
                         FakeStore(facts=facts, known={"20260830T090000Z-aaaaaa"}), {}, d)
        check("unknown member: refused", (rc, nt_files(d)), (1, []))
        check("unknown member: named", "20260901T080000Z-bbbbbb" in err, True)
        rc, _, _ = run(mod, ["compact", "--plan", str(planfile), "--force"],
                       FakeStore(facts=facts, known=set()), {}, d)
        check("--force skips the existence check", rc, 0)

    with tempdir() as d:
        planfile = d / "plan.json"
        planfile.write_text(json.dumps(plan_item()), encoding="utf-8")
        rc, _, err = run(mod, ["compact", "--plan", str(planfile)], FakeStore(down=True), {}, d)
        check("unreachable store: refused, nothing written", (rc, nt_files(d)), (1, []))
        planfile.write_text(json.dumps(plan_item(retires=["20260830T090000Z-aaaaaa"])),
                            encoding="utf-8")
        st = FakeStore(facts=facts)
        rc, _, err = run(mod, ["compact", "--plan", str(planfile)], st, {}, d)
        check("invalid plan: refused before any query", (rc, st.queries, st.asks),
              (1, [], []))

    with tempdir() as d:
        saved_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(plan_item()))
        try:
            rc, out, _ = run(mod, ["compact", "--plan", "-"], FakeStore(facts=facts), {}, d)
        finally:
            sys.stdin = saved_stdin
        check("plan from stdin", (rc, len(out.split())), (0, 1))


def test_review_followups(mod):
    """What the first review of memory v2 asked for: no traceback on absurd
    durations, no compaction from a lower tier, and the index lag closed by
    reading the last minutes' files."""
    print("review follow-ups")
    check("oversized days refused", mod.parse_expires("99999999999d", NOW), None)
    check("oversized months refused", mod.parse_expires("999999999m", NOW), None)

    with tempdir() as d:
        rc, _, _ = run(mod, ["store", "--tag", "ludmila", RULE],
                       FakeStore(tags=TWENTY_TAGS), LOWER_ENV, d)
        check("first store written", rc, 0)
        rc, _, err = run(mod, ["store", "--tag", "ludmila", RULE + " today"],
                         FakeStore(tags=TWENTY_TAGS), LOWER_ENV, d)
        check("seconds later: the store knows nothing, the file does", rc, 1)
        check("names the unindexed entry", "near-duplicate of live memories" in err, True)
        rc, _, _ = run(mod, ["store", "--tag", "signal", "Signal gateway restarted"],
                       FakeStore(tags=TWENTY_TAGS), LOWER_ENV, d)
        check("a different tag is not compared", rc, 0)
        check("guard and write ran under the lock", (d / ".memory.lock").exists(), True)
        rc, _, err = run(mod, ["store", "--tag", "ludmila", RULE + " today"],
                         FakeStore(down=True), LOWER_ENV, d)
        check("store down: a local near-duplicate only warns", rc, 0)
        check("store down: names it", "in a recent local file" in err, True)

    with tempdir() as d:
        rc, _, err = run(mod, ["store", "--tag", "ludmila", "--model", "claude-opus-5",
                               "--new-tag", "--tag", "brand-new", RULE],
                         FakeStore(tags=TWENTY_TAGS), LOWER_ENV, d)
        check("--model naming the frontier does not buy its flags", rc, 1)
        check("the refusal is the lower-tier one", "only Ara senior can coin" in err, True)

    with tempdir() as d:
        rc, _, _ = run(mod, ["store", "--tag", "brand-new", "--new-tag",
                             "A new topic begins here"],
                       FakeStore(tags=TWENTY_TAGS), FRONTIER_ENV, d)
        check("frontier coins a tag", rc, 0)
        rc, _, err = run(mod, ["store", "--tag", "brand-new", "Second note on the new topic"],
                         FakeStore(tags=TWENTY_TAGS), LOWER_ENV, d)
        check("lower tier may use it before the store indexes it", rc, 0)

    with tempdir() as d:
        env = dict(LOWER_ENV, RETINUE_MEMORY_SESSION="sess-9")
        rc, _, err = run(mod, ["store", "--tag", "ludmila", RULE],
                         FakeStore(tags=TWENTY_TAGS), env, d)
        first = err.split("stored ")[1].split()[0]
        rc, _, _ = run(mod, ["store", "--tag", "ludmila", "--corrects", first,
                             "Ludmila now wants plain text, no voice notes"],
                       FakeStore(tags=TWENTY_TAGS), env, d)
        check("correction written into the same session file", rc, 0)
        rc, _, _ = run(mod, ["store", "--tag", "ludmila", RULE + " today"],
                       FakeStore(tags=TWENTY_TAGS), env, d)
        check("a locally corrected entry is not a live duplicate", rc, 0)

    with tempdir() as d:
        old = PFX + "20260101T000000Z-oldold"
        (d / "old.nt").write_text(
            f'<{old}> <{mod.RDF_TYPE}> <{mod.KB}Memory> .\n'
            f'<{old}> <{mod.KB}content> "{RULE}" .\n'
            f'<{old}> <{mod.KB}tag> "ludmila" .\n'
            f'<{old}> <{mod.KB}recordedAt> "2026-01-01T00:00:00Z"^^<{mod.XSD}dateTime> .\n',
            encoding="utf-8")
        rc, _, _ = run(mod, ["store", "--tag", "ludmila", RULE + " today"],
                       FakeStore(tags=TWENTY_TAGS), LOWER_ENV, d)
        check("an old entry in a freshly touched file is the store's business", rc, 0)

    with tempdir() as d:
        gone = PFX + "20261006T115800Z-expird"
        (d / "expired.nt").write_text(
            f'<{gone}> <{mod.RDF_TYPE}> <{mod.KB}Memory> .\n'
            f'<{gone}> <{mod.KB}content> "{RULE}" .\n'
            f'<{gone}> <{mod.KB}tag> "ludmila" .\n'
            f'<{gone}> <{mod.KB}recordedAt> "2026-10-06T11:58:00Z"^^<{mod.XSD}dateTime> .\n'
            f'<{gone}> <{mod.KB}expires> "2026-10-06T11:59:00Z"^^<{mod.XSD}dateTime> .\n',
            encoding="utf-8")
        rc, _, _ = run(mod, ["store", "--tag", "ludmila", RULE + " today"],
                       FakeStore(tags=TWENTY_TAGS), LOWER_ENV, d)
        check("an expired unindexed entry is not a live duplicate", rc, 0)

    facts = {"20260830T090000Z-aaaaaa": {"t": "2026-08-30T09:00:00Z"},
             "20260901T080000Z-bbbbbb": {"t": "2026-09-01T08:00:00Z"}}
    with tempdir() as d:
        planfile = d / "plan.json"
        planfile.write_text(json.dumps(plan_item()), encoding="utf-8")
        rc, _, err = run(mod, ["compact", "--plan", str(planfile)],
                         FakeStore(facts=facts), LOWER_ENV, d)
        check("lower tier: compaction refused", rc, 1)
        check("lower tier: told why", "frontier work" in err, True)
        check("lower tier: nothing written", nt_files(d), [])
        rc, _, _ = run(mod, ["compact", "--plan", str(planfile)],
                       FakeStore(facts=facts), FRONTIER_ENV, d)
        check("frontier: compacted", rc, 0)
        rc, _, err = run(mod, ["compact", "--plan", str(planfile)],
                         FakeStore(facts=facts), FRONTIER_ENV, d)
        check("again inside the index lag: refused from the file", rc, 1)
        check("names the claim", "is already compacted into" in err, True)
        check("still one compaction file", len(nt_files(d)), 1)

    with tempdir() as d:
        planfile = d / "plan.json"
        planfile.write_text(json.dumps(plan_item()), encoding="utf-8")
        half = {"20260830T090000Z-aaaaaa": facts["20260830T090000Z-aaaaaa"]}
        rc, _, err = run(mod, ["compact", "--plan", str(planfile), "--force"],
                         FakeStore(facts=half), FRONTIER_ENV, d)
        check("--force does not cover an unindexed member", rc, 1)
        check("says which", "20260901T080000Z-bbbbbb is not indexed yet" in err, True)
        check("nothing written over it", nt_files(d), [])

    with tempdir() as d:
        planfile = d / "plan.json"
        planfile.write_text(json.dumps(plan_item()), encoding="utf-8")
        (d / "late.nt").write_text(
            f'<{PFX}20260830T090000Z-aaaaaa> <{mod.KB}correctedBy> '
            f'<{PFX}20261006T115900Z-cccccc> .\n', encoding="utf-8")
        rc, _, err = run(mod, ["compact", "--plan", str(planfile)],
                         FakeStore(facts=facts), FRONTIER_ENV, d)
        check("a kept member corrected locally after planning refuses the plan", rc, 1)
        check("says so", "since the plan was drawn up" in err, True)
        check("compaction not written", [p.name for p in nt_files(d)], ["late.nt"])

    with tempdir() as d:
        planfile = d / "plan.json"
        planfile.write_text(json.dumps(plan_item()), encoding="utf-8")
        challenged = [{"m": uri(PFX + "20260830T090000Z-aaaaaa"),
                       "p": uri(mod.KB + "supersededBy")}]
        rc, _, err = run(mod, ["compact", "--plan", str(planfile)],
                         FakeStore(facts=facts, rows=challenged), FRONTIER_ENV, d)
        check("a kept member superseded in the store refuses the plan", rc, 1)
        check("names the link", "supersededBy" in err, True)
        rc, _, _ = run(mod, ["compact", "--plan", str(planfile)],
                       FakeStore(facts=facts, rows=challenged), FRONTIER_ENV, d)
        plan_retired = plan_item(summarizes=[], retires=["20260830T090000Z-aaaaaa",
                                                        "20260901T080000Z-bbbbbb"])
        planfile.write_text(json.dumps(plan_retired), encoding="utf-8")
        rc, _, _ = run(mod, ["compact", "--plan", str(planfile)],
                       FakeStore(facts=facts, rows=challenged), FRONTIER_ENV, d)
        check("retiring the challenged member is fine", rc, 0)


def main():
    mod = load()
    for t in (test_parse_expires, test_normalize_model, test_session_tier,
              test_word_overlap, test_classify_and_closest, test_liveness,
              test_duplicate_guard, test_new_tag_guard, test_store_writes,
              test_recall_query, test_recall_render, test_expand, test_tags,
              test_validate_plan, test_compact_writes,
              test_review_followups):
        t(mod)
    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
