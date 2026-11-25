#!/usr/bin/env python3
"""Focused checks for the mail gateway's threading library.

`scripts/mail_threads.py` decides which chat a mail belongs to from its
`Message-ID`, `References` and `In-Reply-To` alone — never its subject — and
keeps that decision in a per-account sqlite index. This exercises the header
parsers against real-world junk, the multilingual subject table, and the
threading cases the gateway meets: in order, out of order, backfill, siblings
of an unseen parent, In-Reply-To-only clients, cut-short References and the
merges they force (including the tie rule), idempotence, reopening, forgetting
a chat, rebuilding from ledger records in any order, two accounts staying
independent, and one writer racing several readers.

Standalone, no third-party deps:

    python3 tests/test_mail_threads.py
"""
import importlib.util
import random
import sqlite3
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "mail_threads.py"


def _load():
    spec = importlib.util.spec_from_file_location("mail_threads", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mt = _load()

# A fixed epoch base so dates read as "day N" in the cases below.
DAY = 86400.0
T0 = 1_760_000_000.0


def _index(tmp, name="me@example.org.sqlite"):
    return mt.ThreadIndex(Path(tmp) / name)


def _feed(index, mid, refs="", irt="", day=0, subject=None):
    return index.resolve(mid, mt.cited_ids(refs, irt, mid), T0 + day * DAY, subject)


def _snapshot(path):
    conn = sqlite3.connect(str(path))
    try:
        return (sorted(conn.execute("SELECT * FROM ids")),
                sorted(conn.execute("SELECT * FROM mails")))
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Header parsing
# --------------------------------------------------------------------------

def test_normalize_id():
    cases = [
        ("<a@b>", "a@b"),
        ("a@b", "a@b"),                        # idempotent on the normal form
        ("  <a@b>  ", "a@b"),
        ("<a@b> (sent by a robot)", "a@b"),
        ("(comment) <a@b>", "a@b"),
        ("\r\n <a@b>", "a@b"),                 # a folded header line
        ("<very-long-id\r\n  @example.org>", "very-long-id@example.org"),
        ("< a@b >", "a@b"),
        ("<AbC.123@Example.ORG>", "AbC.123@Example.ORG"),   # case is kept
        ("<abc>", "abc"),                      # no "@" inside brackets is tolerated
        ("abc", "abc"),
        ("<a@b> <c@d>", "a@b"),                # a malformed header names its first id
        ("", ""),
        (None, ""),
        ("   ", ""),
        ("<>", ""),
        ("<a b@c>", ""),                       # whitespace inside an id is junk
        ("message from x@y of Monday", ""),
        ("<a@b", ""),                          # truncated
        ('"<a@b>"', ""),                       # inside a quoted string is prose
        ("<a,b@c>", ""),
        ("<@b>", ""),
        ("<a@>", ""),
    ]
    for raw, want in cases:
        got = mt.normalize_id(raw)
        assert got == want, f"normalize_id({raw!r}) = {got!r}, want {want!r}"
    print("PASS test_normalize_id")


def test_cited_ids_order_and_dedup():
    c = mt.cited_ids
    assert c("<r1@x> <r2@x> <r3@x>", "<r3@x>") == ["r1@x", "r2@x", "r3@x"]
    # In-Reply-To not in References goes last.
    assert c("<r1@x> <r2@x>", "<p@x>") == ["r1@x", "r2@x", "p@x"]
    # Duplicates keep their first position.
    assert c("<r1@x> <r2@x> <r1@x>", "<r2@x>") == ["r1@x", "r2@x"]
    # The mail's own id is never cited.
    assert c("<r1@x> <me@x>", "<me@x>", "<me@x>") == ["r1@x"]
    assert c("<r1@x> <me@x>", "", "me@x") == ["r1@x"]
    # Only In-Reply-To; only References; neither.
    assert c("", "<p@x>") == ["p@x"]
    assert c("<r1@x>", None) == ["r1@x"]
    assert c(None, None) == []
    # Folded References spread over lines.
    assert c("<r1@x>\r\n <r2@x>\r\n\t<r3@x>", "") == ["r1@x", "r2@x", "r3@x"]
    # Case-sensitive ids are different ids.
    assert c("<A@x> <a@x>", "") == ["A@x", "a@x"]
    print("PASS test_cited_ids_order_and_dedup")


def test_cited_ids_junk():
    c = mt.cited_ids
    # Commas between ids (seen from several webmailers).
    assert c("<r1@x>,<r2@x>, <r3@x>", "") == ["r1@x", "r2@x", "r3@x"]
    # Quoted strings and comments around and between ids add nothing.
    assert c('"Joe Smith" <r1@x> (Joe\'s message of "Mon, 1 Jan" <joe@x>)', "") == ["r1@x"]
    # Nested comments.
    assert c("<r1@x> (outer (inner <z@z>) tail) <r2@x>", "") == ["r1@x", "r2@x"]
    # A prose In-Reply-To naming an address produces nothing ...
    assert c("", "message from x@y.org of Mon, 1 Jan 2024 10:00:00 +0100") == []
    # ... and a prose one with a bracketed id keeps the id only.
    assert c("", "<p@x> (message from Joe of Monday)") == ["p@x"]
    assert c("", "Joe's message of Monday <p@x>") == ["p@x"]
    # Bare ids without brackets are accepted only when every token is one.
    assert c("r1@x r2@x", "") == ["r1@x", "r2@x"]
    assert c("r1@x, r2@x", "") == ["r1@x", "r2@x"]
    assert c("see r1@x", "") == []
    assert c("", "p@x") == ["p@x"]
    # Junk ids inside brackets are dropped, the good ones kept.
    assert c("<a b@x> <r1@x> <> <r2@x", "") == ["r1@x"]
    # A truncated References keeps what is complete.
    assert c("<r1@x> <r2@x> <r3@", "") == ["r1@x", "r2@x"]
    # Garbage never raises.
    for junk in ("<<<>>>", ")(", '"unterminated', "(unterminated", "\x00<a@b>",
                 "<\x01@x>", "\\", "<" * 50):
        mt.cited_ids(junk, junk)
    assert c("<<r1@x>", "") == ["r1@x"]
    print("PASS test_cited_ids_junk")


def test_base_subject_table():
    cases = [
        ("Quarterly numbers", "Quarterly numbers"),
        ("Re: Quarterly numbers", "Quarterly numbers"),
        ("RE: Quarterly numbers", "Quarterly numbers"),
        ("re:Quarterly numbers", "Quarterly numbers"),
        ("Fw: x", "x"), ("FWD: x", "x"), ("Fwd: Fwd: x", "x"),
        ("AW: x", "x"), ("WG: x", "x"), ("aw: wg: x", "x"),
        ("SV: x", "x"), ("VS: x", "x"), ("VB: x", "x"),
        ("Antw: x", "x"), ("Doorst: x", "x"),
        ("R: x", "x"), ("RIF: x", "x"), ("I: x", "x"),
        ("TR: x", "x"), ("Réf: x", "x"), ("RÉF : x", "x"),
        ("Odp: x", "x"), ("PD: x", "x"),
        ("RES: x", "x"), ("ENC: x", "x"), ("RV: x", "x"),
        ("ΑΠ: x", "x"), ("απ: x", "x"), ("ΠΡΘ: x", "x"),
        ("回复：x", "x"), ("转发: x", "x"), ("השב: x", "x"),
        # Numbered and bracketed counts.
        ("Re[2]: x", "x"), ("Re(3): x", "x"), ("RE^2: x", "x"), ("Re [4]: x", "x"),
        # French spacing and the full-width colon.
        ("RE : x", "x"), ("Re：x", "x"),
        # Full-width letters, as an East Asian input method types the prefix;
        # the rest of the subject keeps its own.
        ("Ｒｅ： 会議の件", "会議の件"), ("ＲＥ：x", "x"), ("ＦＷＤ：Ｒｅ：x", "x"),
        ("Ｒｅ［２］： x", "x"), ("Ｒｅ：　ｘｙｚ", "ｘｙｚ"),
        ("Ｒｅａｌｌｙ", "Ｒｅａｌｌｙ"), ("Ｒｅ 会議", "Ｒｅ 会議"),
        # Stacks of every kind.
        ("RE: AW: Fwd: WG: Re[2]: x", "x"),
        # List tags are part of the name, kept once, in front.
        ("[dev] Re: x", "[dev] x"),
        ("Re: [dev] x", "[dev] x"),
        ("Re: [dev] Re: [dev] x", "[dev] x"),
        ("[dev] AW: [Dev] x", "[dev] x"),
        ("[a] [b] Re: x", "[a] [b] x"),
        # Whitespace collapses.
        ("  Re:   too    many \t spaces  ", "too many spaces"),
        ("Re: line\r\n folded", "line folded"),
        # Nothing left.
        ("", ""), (None, ""), ("Re:", ""), ("Re: Fwd: ", ""), ("   ", ""),
        # Not prefixes: no colon, part of a word, or a common subject of its own.
        ("Rex: the dog", "Rex: the dog"),
        ("Answers inside", "Answers inside"),
        ("Re-elect me", "Re-elect me"),
        ("Travel plans", "Travel plans"),
        ("PR: fix the build", "PR: fix the build"),
        ("Ref: 12345", "Ref: 12345"),
        ("Meeting re: budget", "Meeting re: budget"),
        ("x [dev] y", "x [dev] y"),
    ]
    for raw, want in cases:
        got = mt.base_subject(raw)
        assert got == want, f"base_subject({raw!r}) = {got!r}, want {want!r}"
    # The table is data and every row strips.
    for prefix in mt.REPLY_FORWARD_PREFIXES:
        assert mt.base_subject(f"{prefix}: x") == "x", prefix
        assert mt.base_subject(f"{prefix.upper()}: x") == "x", prefix
    print("PASS test_base_subject_table")


def test_to_epoch():
    e = mt.to_epoch
    assert e(None) is None and e("") is None and e("not a date") is None
    assert e(T0) == T0 and e(int(T0)) == T0 and e(str(int(T0))) == T0
    assert e("2026-01-01T00:00:00Z") == e("2026-01-01T01:00:00+01:00")
    assert e("Thu, 01 Jan 2026 01:00:00 +0100") == e("2026-01-01T00:00:00Z")
    assert e(float("nan")) is None and e(True) is None
    # "Never raises" holds for what a hostile Date header can hold: an offset
    # of twenty digits (an OverflowError on some Python versions), a year no
    # clock reaches, a number beyond any float.
    for hostile in ("Mon, 1 Jan 2024 00:00:00 +99999999999999999999",
                    "Mon, 1 Jan 2024 00:00:00 -99999999999999999999",
                    "9" * 400, "0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59",
                    "Mon, 31 Dec 9999 23:59:59 -2359", 10 ** 400, "\ud800", "1e999"):
        got = e(hostile)
        assert got is None or isinstance(got, float), (hostile, got)
    print("PASS test_to_epoch")


# --------------------------------------------------------------------------
# Threading
# --------------------------------------------------------------------------

def test_in_order_thread():
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        a = _feed(ix, "<a@x>", day=0, subject="Plans")
        assert a == mt.Resolution("a@x", [], True, False)
        b = _feed(ix, "<b@x>", "<a@x>", "<a@x>", day=1, subject="Re: Plans")
        assert b == mt.Resolution("a@x", [], False, False)
        c = _feed(ix, "<c@x>", "<a@x> <b@x>", "<b@x>", day=2, subject="AW: Re: Plans")
        assert c.chat == "a@x" and not c.merged and not c.new
        assert ix.members("a@x") == ["a@x", "b@x", "c@x"]
        assert ix.dates("a@x") == (T0, T0 + 2 * DAY)
        assert ix.earliest("a@x") == T0 and ix.latest("a@x") == T0 + 2 * DAY
        assert ix.date_of("<b@x>") == T0 + DAY
        assert ix.subject_of("a@x") == "Plans"
        assert ix.chats() == ["a@x"]
    print("PASS test_in_order_thread")


def test_reply_before_parent_lands_without_merge():
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        # The reply names its root; the chat is keyed by it at once.
        b = _feed(ix, "<b@x>", "<a@x>", "<a@x>", day=1, subject="Re: Plans")
        assert b == mt.Resolution("a@x", [], True, False)
        assert ix.chat_of("a@x") == "a@x", "a cited id is in the index"
        assert ix.members("a@x") == ["b@x"]
        assert ix.members("a@x", include_cited=True) == ["b@x", "a@x"]
        # The parent arrives later and simply joins.
        a = _feed(ix, "<a@x>", day=0, subject="Plans")
        assert a == mt.Resolution("a@x", [], False, False)
        assert ix.members("a@x") == ["a@x", "b@x"]
    print("PASS test_reply_before_parent_lands_without_merge")


def test_backfill_oldest_first():
    mails = [
        ("<root@x>", "", "", 0),
        ("<r1@y>", "<root@x>", "<root@x>", 1),
        ("<r2@x>", "<root@x> <r1@y>", "<r1@y>", 2),
        ("<other@z>", "", "", 2.5),
        ("<r3@y>", "<root@x> <r1@y> <r2@x>", "<r2@x>", 3),
        ("<o1@x>", "<other@z>", "<other@z>", 4),
    ]
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        chats = [_feed(ix, m, r, i, d).chat for m, r, i, d in mails]
        assert chats == ["root@x", "root@x", "root@x", "other@z", "root@x", "other@z"]
        assert sorted(ix.chats()) == ["other@z", "root@x"]
    print("PASS test_backfill_oldest_first")


def test_siblings_of_an_unseen_parent():
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        b = _feed(ix, "<b@x>", "", "<p@x>", day=1)
        c = _feed(ix, "<c@x>", "", "<p@x>", day=2)
        assert (b.chat, b.new) == ("p@x", True)
        assert (c.chat, c.new, c.merged) == ("p@x", False, [])
        assert ix.members("p@x") == ["b@x", "c@x"]
    print("PASS test_siblings_of_an_unseen_parent")


def test_in_reply_to_only_client():
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        # In order: the chain of parents is enough.
        _feed(ix, "<a@x>", day=0)
        assert _feed(ix, "<b@x>", "", "<a@x>", day=1).chat == "a@x"
        assert _feed(ix, "<c@x>", "", "<b@x>", day=2).chat == "a@x"
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        # Out of order: c names only b, so it starts chat b; b then links b to a.
        _feed(ix, "<a@x>", day=0)
        c = _feed(ix, "<c@x>", "", "<b@x>", day=2)
        assert (c.chat, c.new) == ("b@x", True)
        b = _feed(ix, "<b@x>", "", "<a@x>", day=1)
        assert b == mt.Resolution("a@x", ["b@x"], False, False)
        assert ix.members("a@x") == ["a@x", "b@x", "c@x"]
        assert ix.chat_of("c@x") == "a@x" and ix.members("b@x") == []
    print("PASS test_in_reply_to_only_client")


def test_references_cut_short_then_merged():
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        _feed(ix, "<a@x>", day=0, subject="Budget")
        # c's client kept only the last reference, b, which was never mirrored.
        c = _feed(ix, "<c@x>", "<b@x>", "<b@x>", day=2, subject="Re: Budget, revised")
        assert (c.chat, c.new) == ("b@x", True)
        assert sorted(ix.chats()) == ["a@x", "b@x"], "two chats until a link arrives"
        # d carries the full chain and links them.
        d = _feed(ix, "<d@x>", "<a@x> <b@x> <c@x>", "<c@x>", day=3)
        assert d == mt.Resolution("a@x", ["b@x"], False, False)
        assert ix.chats() == ["a@x"]
        for mid in ("a@x", "b@x", "c@x", "d@x"):
            assert ix.chat_of(mid) == "a@x", mid
        assert ix.subject_of("a@x") == "Budget"
    print("PASS test_references_cut_short_then_merged")


def test_merge_of_three():
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        _feed(ix, "<x1@x>", "", "<q@x>", day=5, subject="from q")
        _feed(ix, "<x2@x>", "", "<p@x>", day=3, subject="from p")
        _feed(ix, "<x3@x>", "", "<r@x>", day=4, subject="from r")
        _feed(ix, "<x4@x>", "", "<r@x>", day=6)
        assert sorted(ix.chats()) == ["p@x", "q@x", "r@x"]
        m = _feed(ix, "<m@x>", "<q@x> <r@x> <p@x>", "", day=7)
        # p's earliest mail (day 3) is oldest; the others are absorbed.
        assert m == mt.Resolution("p@x", ["q@x", "r@x"], False, False)
        assert ix.members("p@x") == ["x2@x", "x3@x", "x1@x", "x4@x", "m@x"]
        for mid in ("p@x", "q@x", "r@x", "x1@x", "x2@x", "x3@x", "x4@x", "m@x"):
            assert ix.chat_of(mid) == "p@x", mid
        assert ix.dates("p@x") == (T0 + 3 * DAY, T0 + 7 * DAY)
        assert ix.subject_of("p@x") == "from p"
        assert ix.members("q@x") == [] and ix.subject_of("q@x") == ""
    print("PASS test_merge_of_three")


def test_merge_tie_rule():
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        _feed(ix, "<m1@x>", "", "<zeta@x>", day=1)
        _feed(ix, "<m2@x>", "", "<alpha@x>", day=1)
        m = _feed(ix, "<m3@x>", "<zeta@x> <alpha@x>", "", day=2)
        assert m.chat == "alpha@x" and m.merged == ["zeta@x"], m
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        # An undated chat is never older than a dated one, whatever its key.
        ix.resolve("m1@x", ["aaa@x"], None)
        ix.resolve("m2@x", ["zzz@x"], T0)
        m = ix.resolve("m3@x", ["aaa@x", "zzz@x"], T0 + DAY)
        assert m.chat == "zzz@x" and m.merged == ["aaa@x"], m
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        # Two undated chats tie; the smaller key wins.
        ix.resolve("m1@x", ["b@x"], None)
        ix.resolve("m2@x", ["a@x"], None)
        assert ix.resolve("m3@x", ["b@x", "a@x"], None).chat == "a@x"
    print("PASS test_merge_tie_rule")


def test_no_subject_only_threading():
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        a = _feed(ix, "<a@x>", day=0, subject="Hello")
        b = _feed(ix, "<b@y>", day=1, subject="Re: Hello")
        assert a.chat != b.chat and b.new
    print("PASS test_no_subject_only_threading")


def test_idempotence():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ix.sqlite"
        with mt.ThreadIndex(path) as ix:
            _feed(ix, "<a@x>", day=0, subject="S")
            _feed(ix, "<c@x>", "", "<b@x>", day=2)
            first = _feed(ix, "<b@x>", "", "<a@x>", day=1)
            assert first.merged == ["b@x"]
        before = _snapshot(path)
        with mt.ThreadIndex(path) as ix:
            for mid, irt, day in (("<a@x>", "", 0), ("<b@x>", "<a@x>", 1),
                                  ("<c@x>", "<b@x>", 2)):
                again = _feed(ix, mid, "", irt, day=day, subject="S" if day == 0 else None)
                assert again == mt.Resolution("a@x", [], False, True), (mid, again)
        assert _snapshot(path) == before, "a repeat changes nothing"
        with mt.ThreadIndex(path) as ix:
            # A repeat fills in only what the first call lacked.
            ix.resolve("c@x", ["b@x"], T0 + 99 * DAY, "Re: late subject")
            assert ix.date_of("c@x") == T0 + 2 * DAY, "the recorded date stands"
            ix.resolve("n@x", [], None)
            ix.resolve("n@x", [], T0)
            assert ix.date_of("n@x") == T0, "a missing date is filled in"
    print("PASS test_idempotence")


def test_reopen_persists():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ix.sqlite"
        ix = mt.ThreadIndex(path)
        _feed(ix, "<b@x>", "<a@x>", "<a@x>", day=1, subject="Re: Persist")
        ix.close()
        ix = mt.ThreadIndex(path)
        assert ix.chat_of("<b@x>") == "a@x" and ix.chat_of("a@x") == "a@x"
        assert ix.subject_of("a@x") == "Persist"
        assert _feed(ix, "<a@x>", day=0).chat == "a@x"
        ix.close()
        with mt.ThreadIndex(path) as ix:
            assert ix.members("a@x") == ["a@x", "b@x"]
        # An index from a future schema is refused, never silently reused.
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        try:
            mt.ThreadIndex(path)
        except RuntimeError:
            pass
        else:
            raise AssertionError("a foreign schema version must be refused")
    print("PASS test_reopen_persists")


def test_forget_chat_then_thread_restarts():
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        _feed(ix, "<a@x>", day=0)
        _feed(ix, "<b@x>", "<a@x>", "<a@x>", day=1)
        _feed(ix, "<o@y>", day=0)
        ix.forget_chat("a@x")
        assert ix.chat_of("a@x") is None and ix.chat_of("b@x") is None
        assert ix.members("a@x") == [] and ix.chats() == ["o@y"]
        assert ix.chat_of("o@y") == "o@y", "other chats are untouched"
        c = _feed(ix, "<c@x>", "<a@x> <b@x>", "<b@x>", day=2)
        assert c == mt.Resolution("a@x", [], True, False)
        assert ix.members("a@x") == ["c@x"]
        ix.forget_chat("no-such@chat")   # harmless
    print("PASS test_forget_chat_then_thread_restarts")


def test_two_accounts_are_independent():
    with tempfile.TemporaryDirectory() as tmp:
        one = _index(tmp, "one@example.org.sqlite")
        two = _index(tmp, "two@example.org.sqlite")
        _feed(one, "<a@x>", day=0)
        _feed(one, "<b@x>", "<a@x>", "<a@x>", day=1)
        # The same thread on the second account: only b reached it.
        assert _feed(two, "<b@x>", "<a@x>", "<a@x>", day=1).new
        assert two.members("a@x") == ["b@x"] and one.members("a@x") == ["a@x", "b@x"]
        one.forget_chat("a@x")
        assert one.chat_of("b@x") is None and two.chat_of("b@x") == "a@x"
        assert two.chat_of("only-on-two@x") is None
        _feed(two, "<only-on-two@x>", day=3)
        assert one.chat_of("only-on-two@x") is None
        one.close()
        two.close()
    print("PASS test_two_accounts_are_independent")


def test_subject_follows_the_earliest_mail():
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        _feed(ix, "<b@x>", "<a@x>", "<a@x>", day=2, subject="Re: Re: Later wording")
        assert ix.subject_of("a@x") == "Later wording"
        # An older mail arriving later renames the chat.
        _feed(ix, "<a@x>", day=1, subject="Original wording")
        assert ix.subject_of("a@x") == "Original wording"
        # A merge re-evaluates it: the absorbed chat held an even older mail.
        _feed(ix, "<z@x>", "", "<y@x>", day=0, subject="Fwd: The very first")
        _feed(ix, "<m@x>", "<y@x> <a@x>", "", day=3, subject="Re: whatever")
        assert ix.chat_of("a@x") == "y@x"
        assert ix.subject_of("y@x") == "The very first"
        # An empty or prefix-only subject never names a chat another mail names.
        _feed(ix, "<q@x>", day=-1, subject="Re:")
        _feed(ix, "<q2@x>", "<q@x>", "", day=0, subject="Named")
        assert ix.subject_of("q@x") == "Named"
        _feed(ix, "<n@x>", day=0)
        assert ix.subject_of("n@x") == ""
    print("PASS test_subject_follows_the_earliest_mail")


def test_resolve_input_hygiene():
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        for bad in ("", "   ", None, "message from x@y"):
            try:
                ix.resolve(bad, [], T0)
            except ValueError:
                pass
            else:
                raise AssertionError(f"resolve({bad!r}) must be refused")
        # A raw cited list is normalised; junk and the own id are dropped.
        r = ix.resolve("<m@x>", ["<r@x>", "", "junk id", "<m@x>", "r@x"], "2026-01-01")
        assert r.chat == "r@x"
        assert ix.members("r@x", include_cited=True) == ["m@x", "r@x"]
        assert ix.chat_of("junk id") is None and ix.chat_of("") is None
        assert ix.date_of("m@x") == mt.to_epoch("2026-01-01")
    print("PASS test_resolve_input_hygiene")


def test_cited_ids_are_bounded():
    many = " ".join(f"<r{n}@x>" for n in range(5000))
    cited = mt.cited_ids(many, "<parent@x>", "<m@x>")
    assert len(cited) == mt.MAX_CITED_IDS
    # The declared root and the nearest parents; the middle is what goes.
    assert cited[0] == "r0@x" and cited[-1] == "parent@x" and cited[-2] == "r4999@x"
    assert cited[1] == f"r{5001 - (mt.MAX_CITED_IDS - 1)}@x"
    short = " ".join(f"<r{n}@x>" for n in range(mt.MAX_CITED_IDS))
    assert len(mt.cited_ids(short, "", "")) == mt.MAX_CITED_IDS, "at the bound: all kept"
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        # A caller that hands in a raw list is held to the same bound.
        ix.resolve("<m@x>", [f"<r{n}@x>" for n in range(5000)], T0)
        assert len(ix.members("r0@x", include_cited=True)) == mt.MAX_CITED_IDS + 1
        assert ix.chat_of("r4999@x") == "r0@x" and ix.chat_of("r2500@x") is None
    print("PASS test_cited_ids_are_bounded")


def test_one_mail_cannot_weld_many_chats():
    assert mt.MAX_MERGE_CHATS == 4
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        for n in range(8):
            _feed(ix, f"<t{n}@x>", day=n, subject=f"Thread {n}")
        # Four chats in one mail is still a thread found again …
        m = _feed(ix, "<four@x>", "<t0@x> <t1@x> <t2@x> <t3@x>", "<t3@x>", day=20)
        assert (m.chat, m.merged, m.unmerged) == ("t0@x", ["t1@x", "t2@x", "t3@x"], ())
        assert len(ix.chats()) == 5
        # … five is not: nothing is merged, and the mail goes to the chat of
        # the mail it answers. The other chats keep their ids.
        before = sorted(ix.chats())
        w = _feed(ix, "<weld@x>", "<t0@x> <t4@x> <t5@x> <t7@x> <new@x> <t6@x>", "<t6@x>",
                  day=-5, subject="Mine now")
        assert w.chat == "t6@x" and w.merged == [] and w.new is False
        assert w.unmerged == ("t0@x", "t4@x", "t5@x", "t7@x")
        assert sorted(ix.chats()) == before
        for n in (4, 5, 6, 7):
            assert ix.chat_of(f"t{n}@x") == f"t{n}@x"
            assert ix.subject_of(f"t{n}@x") == (f"Thread {n}" if n != 6 else "Mine now")
        # An id nobody had seen follows the mail; a known one stays where it was.
        assert ix.chat_of("new@x") == "t6@x" and ix.chat_of("t0@x") == "t0@x"
        # A repeat of it changes nothing, and still says what it was refused.
        again = _feed(ix, "<weld@x>", "<t0@x> <t4@x> <t5@x> <t7@x> <new@x> <t6@x>", "<t6@x>")
        assert again.chat == "t6@x" and again.repeat and again.merged == []
        assert sorted(ix.chats()) == before
        # A mail a reply already placed stays there, however much it cites.
        _feed(ix, "<reply@x>", "<late@x>", "<late@x>", day=30)
        late = _feed(ix, "<late@x>", "<t4@x> <t5@x> <t6@x> <t7@x>", "<t7@x>", day=29)
        assert late.chat == "late@x" and late.merged == [] and len(late.unmerged) == 4
        snapshot = sorted(ix.chats())

        # A rebuild from the records decides the same: the ledger's keys stand.
        records = [(f"t{n}@x", [], "t0@x" if n < 4 else f"t{n}@x", T0 + n * DAY, None)
                   for n in range(8)]
        records += [("four@x", ["t0@x", "t1@x", "t2@x", "t3@x"], "t0@x", T0 + 20 * DAY, None),
                    ("weld@x", ["t0@x", "t4@x", "t5@x", "t6@x", "t7@x", "new@x"], "t6@x",
                     T0 - 5 * DAY, None),
                    ("reply@x", ["late@x"], "late@x", T0 + 30 * DAY, None),
                    ("late@x", ["t4@x", "t5@x", "t6@x", "t7@x"], "late@x", T0 + 29 * DAY, None)]
        assert ix.rebuild(records) == {}
        assert sorted(ix.chats()) == snapshot
        assert ix.chat_of("weld@x") == "t6@x" and ix.chat_of("late@x") == "late@x"
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        # A subject a caller's decoder left a lone surrogate in is storable.
        r = ix.resolve("<s@x>", [], T0, "bad \ud800 subject")
        assert r.chat == "s@x" and ix.subject_of("s@x") == "bad ? subject"
        # And an id with one is no id.
        assert mt.normalize_id("<a\udfff@x>") == "" and mt.cited_ids("<a\ud800@x> <b@x>", "") == ["b@x"]
    print("PASS test_one_mail_cannot_weld_many_chats")


# --------------------------------------------------------------------------
# Rebuild
# --------------------------------------------------------------------------

def _random_threads(rng, n_threads=12, max_len=7):
    """Mails of several threads with realistic header damage, as tuples
    (mid, references, in_reply_to, day)."""
    mails = []
    day = 0.0
    for t in range(n_threads):
        chain = []
        for k in range(rng.randint(1, max_len)):
            mid = f"<t{t}m{k}@example.org>"
            day += rng.random()
            if not chain:
                refs, irt = "", ""
                if rng.random() < 0.2:          # a root we never mirrored
                    refs = f"<t{t}root@example.org>"
            else:
                parent = rng.choice(chain)
                style = rng.random()
                if style < 0.5:                  # full References
                    refs = " ".join(chain[: chain.index(parent) + 1])
                elif style < 0.75:               # In-Reply-To only
                    refs = ""
                else:                            # References cut short
                    refs = parent
                irt = parent
            mails.append((mid, refs, irt, day))
            chain.append(mid)
    return mails


def _partition(index, ids):
    groups = {}
    for mid in ids:
        groups.setdefault(index.chat_of(mid), set()).add(mt.normalize_id(mid))
    return sorted(sorted(g) for g in groups.values())


def test_rebuild_matches_oldest_first():
    rng = random.Random(20261123)
    for trial in range(25):
        mails = _random_threads(rng)
        ids = [m[0] for m in mails]
        with tempfile.TemporaryDirectory() as tmp:
            oldest = _index(tmp, "oldest.sqlite")
            for mid, refs, irt, day in mails:       # already oldest first
                _feed(oldest, mid, refs, irt, day)

            # Without ledger keys: any order rebuilds the oldest-first index.
            shuffled = list(mails)
            rng.shuffle(shuffled)
            rebuilt = _index(tmp, "rebuilt.sqlite")
            moved = rebuilt.rebuild(
                [(m, mt.cited_ids(r, i, m), None, T0 + d * DAY) for m, r, i, d in shuffled])
            assert moved == {}
            for mid in ids:
                assert rebuilt.chat_of(mid) == oldest.chat_of(mid), (trial, mid)
            assert sorted(rebuilt.chats()) == sorted(oldest.chats())

            # A live index fed in arrival order (not date order), and the
            # ledger it leaves behind (each record re-keyed on every merge).
            live = _index(tmp, "live.sqlite")
            arrival = list(mails)
            rng.shuffle(arrival)
            for mid, refs, irt, day in arrival:
                _feed(live, mid, refs, irt, day)
            ledger = [(m, " ".join(mt.cited_ids(r, i, m)), live.chat_of(m), T0 + d * DAY)
                      for m, r, i, d in arrival]
            rng.shuffle(ledger)
            from_ledger = _index(tmp, "ledger.sqlite")
            assert from_ledger.rebuild(ledger) == {}, "a consistent ledger moves nothing"
            for mid in ids:
                assert from_ledger.chat_of(mid) == live.chat_of(mid), (trial, mid)
            # Whatever the arrival order, the threads are the same threads.
            assert _partition(live, ids) == _partition(oldest, ids), trial
            for ix in (oldest, rebuilt, live, from_ledger):
                ix.close()
    print("PASS test_rebuild_matches_oldest_first")


def test_rebuild_heals_an_interrupted_merge_and_replaces_content():
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        _feed(ix, "<stale@x>", day=0)
        # The gateway crashed after the index merged b into a but before it
        # re-keyed c's record: the ledger still says c is in chat b.
        records = [
            {"id": "<c@x>", "cited": ["b@x"], "chat": "b@x", "date": T0 + 2 * DAY,
             "subject": "Re: Topic"},
            ("<a@x>", [], "a@x", T0, "Topic"),
            ("<b@x>", "a@x", "a@x", T0 + DAY),
            ("", [], "a@x", T0),                       # no id: skipped
        ]
        moved = ix.rebuild(records)
        assert moved == {"b@x": "a@x"}, moved
        assert ix.members("a@x") == ["a@x", "b@x", "c@x"]
        assert ix.chat_of("stale@x") is None, "rebuild replaces, never adds to"
        assert ix.subject_of("a@x") == "Topic"
        try:
            ix.rebuild([("only", "three", "fields")])
        except ValueError:
            pass
        else:
            raise AssertionError("a malformed record must be refused")
        assert ix.members("a@x") == ["a@x", "b@x", "c@x"], "a refused rebuild changes nothing"
    print("PASS test_rebuild_heals_an_interrupted_merge_and_replaces_content")


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------

def test_one_writer_many_readers():
    rng = random.Random(7)
    mails = _random_threads(rng, n_threads=40, max_len=10)
    errors = []
    done = threading.Event()
    with tempfile.TemporaryDirectory() as tmp, _index(tmp) as ix:
        def writer():
            try:
                for mid, refs, irt, day in mails:
                    _feed(ix, mid, refs, irt, day)
            except Exception as exc:   # pragma: no cover - reported below
                errors.append(exc)
            finally:
                done.set()

        def reader():
            try:
                while not done.is_set():
                    mid = rng.choice(mails)[0]
                    chat = ix.chat_of(mid)
                    if chat is not None:
                        # A chat seen mid-flight is always whole: its mails all
                        # point back to it (no half-applied merge is visible).
                        members = ix.members(chat)
                        ix.dates(chat)
                        ix.subject_of(chat)
                        for m in members:
                            now = ix.chat_of(m)
                            assert now is not None
                    ix.chats()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer)] + [
            threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not errors, errors
        assert len(set(ix.chat_of(m[0]) for m in mails)) <= 40
        assert sum(len(ix.members(c)) for c in ix.chats()) == len(mails)
    print("PASS test_one_writer_many_readers")


if __name__ == "__main__":
    test_normalize_id()
    test_cited_ids_order_and_dedup()
    test_cited_ids_junk()
    test_base_subject_table()
    test_to_epoch()
    test_in_order_thread()
    test_reply_before_parent_lands_without_merge()
    test_backfill_oldest_first()
    test_siblings_of_an_unseen_parent()
    test_in_reply_to_only_client()
    test_references_cut_short_then_merged()
    test_merge_of_three()
    test_merge_tie_rule()
    test_no_subject_only_threading()
    test_idempotence()
    test_reopen_persists()
    test_forget_chat_then_thread_restarts()
    test_two_accounts_are_independent()
    test_subject_follows_the_earliest_mail()
    test_resolve_input_hygiene()
    test_cited_ids_are_bounded()
    test_one_mail_cannot_weld_many_chats()
    test_rebuild_matches_oldest_first()
    test_rebuild_heals_an_interrupted_merge_and_replaces_content()
    test_one_writer_many_readers()
    print("all mail-threads tests passed")
