#!/usr/bin/env python3
"""`email_client search --uid-max`: the paging cursor the triage gate walks a
large INBOX with. Only UIDs at or below the cursor, so a caller that saw the
newest `limit` can ask for the `limit` before them until a page is short.

    python3 tests/test_email_search_paging.py
"""
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location(
        "email_client_search_under_test", SCRIPTS_DIR / "email_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _M:
    criteria = None

    def uid(self, verb, charset, *criteria):
        _M.criteria = list(criteria)
        cap = 30
        if "UID" in criteria:
            cap = int(criteria[criteria.index("UID") + 1].split(":")[1])
        return "OK", [b" ".join(str(u).encode() for u in range(1, cap + 1))]

    def logout(self):
        return None


def _search(ec, **kw):
    ec.imap_connect = lambda cfg: _M()
    ec.imap_select = lambda M, folder, readonly=True: None
    ec._summaries = lambda M, uids: [{"uid": u.decode()} for u in uids]
    fields = dict(folder="INBOX", from_=None, subject=None, text=None,
                  since=None, unseen=False, limit=5, uid_max=None)
    fields.update(kw)
    args = SimpleNamespace(**fields)
    out = io.StringIO()
    with redirect_stdout(out):
        ec.cmd_search(None, args)
    return json.loads(out.getvalue())


def test_without_a_cursor_the_newest_limit_is_listed():
    ec = _load()
    got = _search(ec)
    assert [m["uid"] for m in got["messages"]] == ["30", "29", "28", "27", "26"], got
    assert _M.criteria == ["ALL"], _M.criteria
    assert got["scanned"] == 5 and got["min_uid"] == 26, got
    print("PASS no cursor -> newest `limit`, criteria ALL")


def test_a_cursor_lists_the_newest_limit_at_or_below_it():
    ec = _load()
    got = _search(ec, uid_max=25)
    assert [m["uid"] for m in got["messages"]] == ["25", "24", "23", "22", "21"], got
    assert _M.criteria == ["UID", "1:25"], _M.criteria
    print("PASS a cursor -> newest `limit` with UID <= cursor")


def test_page_completeness_is_reported_from_what_the_server_matched():
    # A summary that fails must not make a full page look short: `scanned`
    # and `min_uid` come from the matched UIDs, the messages from what could
    # be read.
    ec = _load()
    ec._summaries = lambda M, uids: [{"uid": u.decode()} for u in uids if u != b"28"]
    ec.imap_connect = lambda cfg: _M()
    ec.imap_select = lambda M, folder, readonly=True: None
    args = SimpleNamespace(folder="INBOX", from_=None, subject=None, text=None,
                           since=None, unseen=False, limit=5, uid_max=None)
    out = io.StringIO()
    with redirect_stdout(out):
        ec.cmd_search(None, args)
    got = json.loads(out.getvalue())
    assert got["count"] == 4 and got["scanned"] == 5 and got["min_uid"] == 26, got
    print("PASS scanned/min_uid describe the matched page, not the readable one")


def test_bulk_summaries_match_by_uid_and_fill_gaps_singly():
    # One FETCH per chunk; replies matched by the UID in each envelope (the
    # server's order is not the request's), trailing FLAGS items honoured,
    # and anything the bulk reply lacked fetched singly.
    ec = _load()
    hdr = lambda mid: (f"From: a@b.c\r\nSubject: s\r\nMessage-ID: <{mid}>\r\n\r\n").encode()

    class _Bulk:
        def uid(self, verb, arg, items):
            assert verb == "fetch" and items == ec._SUMMARY_ITEMS
            if arg == b"7":
                return "OK", [(b"3 (UID 7 FLAGS (\\Seen) BODY[HEADER.FIELDS (X)] {5}", hdr("m7")), b")"]
            assert arg == "5,6,7", arg
            return "OK", [
                (b"2 (UID 6 BODY[HEADER.FIELDS (X)] {5}", hdr("m6")), b" FLAGS (\\Seen))",
                (b"1 (UID 5 FLAGS () BODY[HEADER.FIELDS (X)] {5}", hdr("m5")), b")",
            ]

    got = ec._summaries(_Bulk(), [b"5", b"6", b"7"])
    assert [m["uid"] for m in got] == ["5", "6", "7"], got
    assert [m["message_id"] for m in got] == ["<m5>", "<m6>", "<m7>"], got
    assert got[0]["unread"] is True and got[1]["unread"] is False and got[2]["unread"] is False, got
    print("PASS bulk summaries are matched by UID, flags honoured, gaps filled")


def test_a_non_positive_cursor_is_an_error_not_an_unbounded_search():
    ec = _load()
    for bad in (0, -3):
        try:
            _search(ec, uid_max=bad)
        except ec.EmailError as exc:
            assert "positive UID" in str(exc), exc
        else:
            raise AssertionError(f"uid_max={bad} was accepted")
    print("PASS a non-positive cursor is refused rather than scanning ALL")


if __name__ == "__main__":
    test_without_a_cursor_the_newest_limit_is_listed()
    test_a_cursor_lists_the_newest_limit_at_or_below_it()
    test_a_non_positive_cursor_is_an_error_not_an_unbounded_search()
    test_page_completeness_is_reported_from_what_the_server_matched()
    test_bulk_summaries_match_by_uid_and_fill_gaps_singly()
    print("all email search paging tests passed")
    sys.exit(0)
