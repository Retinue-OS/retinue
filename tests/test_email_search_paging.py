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
    ec._summary = lambda M, uid: {"uid": uid.decode()}
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
    print("PASS no cursor -> newest `limit`, criteria ALL")


def test_a_cursor_lists_the_newest_limit_at_or_below_it():
    ec = _load()
    got = _search(ec, uid_max=25)
    assert [m["uid"] for m in got["messages"]] == ["25", "24", "23", "22", "21"], got
    assert _M.criteria == ["UID", "1:25"], _M.criteria
    print("PASS a cursor -> newest `limit` with UID <= cursor")


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
    print("all email search paging tests passed")
    sys.exit(0)
