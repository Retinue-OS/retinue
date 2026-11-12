#!/usr/bin/env python3
"""Where `email_client.py fetch-attachment` writes a file without --out.

Without --out the file is saved under the name the attachment carries, and
that name is what the mail's sender declared: it may hold a path ("../..",
an absolute one, backslashes), a hidden name, control characters, or simply
the name of a file that is already there. These checks pin that such a name
is only ever a base name in the working directory, and that an existing file
is never overwritten; an explicit --out stays the operator's own choice.
The mailbox is a fake IMAP connection serving one message.

    python3 tests/test_email_fetch_attachment.py
"""
import argparse
import base64
import importlib.util
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_email_client():
    spec = importlib.util.spec_from_file_location(
        "email_client_fetch_under_test", SCRIPTS_DIR / "email_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeIMAP:
    def __init__(self, raw: bytes):
        self.raw = raw

    def uid(self, command, uid, what):
        assert command == "fetch", command
        return "OK", [(b"1 (RFC822 {%d}" % len(self.raw), self.raw), b")"]

    def logout(self):
        pass


def _mail(filename: str, payload: bytes) -> bytes:
    """One message with one attachment, its name as a sender may encode it
    (RFC 2231), so it can hold anything, line breaks included."""
    encoded = urllib.parse.quote(filename, safe="")
    return (
        "From: sender@example.com\r\nTo: recipient@example.org\r\n"
        "Subject: Test\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        "--b\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        "--b\r\nContent-Type: application/octet-stream\r\n"
        f"Content-Disposition: attachment; filename*=UTF-8''{encoded}\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        f"{base64.b64encode(payload).decode()}\r\n--b--\r\n"
    ).encode("ascii")


def _fetch(ec, filename: str, payload: bytes, out=None, capsys=None) -> dict:
    raw = _mail(filename, payload)
    ec.imap_connect = lambda cfg: _FakeIMAP(raw)
    ec.imap_select = lambda M, folder, readonly=False: None
    args = argparse.Namespace(uid="7", folder="INBOX", part=1, out=out)
    from io import StringIO
    buf, real = StringIO(), sys.stdout
    sys.stdout = buf
    try:
        ec.cmd_fetch_attachment({}, args)
    finally:
        sys.stdout = real
    return json.loads(buf.getvalue())


def test_a_declared_path_is_a_base_name_here(ec, work: Path):
    # The working directory is two levels inside the test's own directory, so
    # "../../evil" and the absolute name below point into it as well.
    outside = work.parent.parent / "evil"
    absolute = work.parent.parent / "skills" / "x"
    absolute.mkdir(parents=True)
    cases = [
        ("../../evil", "evil"),
        (f"{absolute}/SKILL.md", "SKILL.md"),
        ("..\\..\\win.txt", "win.txt"),
        (".bashrc", "bashrc"),
        ("...hidden", "hidden"),
        ("a\r\nb\x00c.pdf", "abc.pdf"),
        ("..", "attachment-1"),
        ("../", "attachment-1"),
        ("\x1b", "attachment-1"),
        (". ..", "attachment-1"),
        (". .", "attachment-1"),
        (".. .", "attachment-1"),
        ("x/. ..", "attachment-1"),
        (" . notes.txt . ", "notes.txt"),
    ]
    for given, want in cases:
        payload = f"bytes of {given!r}".encode()
        out = _fetch(ec, given, payload)
        assert out["saved"] == want, (given, out)
        assert (work / want).read_bytes() == payload, given
        (work / want).unlink()
    assert not outside.exists() and not (absolute / "SKILL.md").exists()
    assert sorted(os.listdir(work)) == [], os.listdir(work)
    print("PASS a declared path is saved as a base name in the working directory")


def test_an_existing_file_is_never_overwritten(ec, work: Path):
    keep = work / "invoice.pdf"
    keep.write_bytes(b"mine")
    try:
        _fetch(ec, "invoice.pdf", b"theirs")
    except ec.EmailError as exc:
        assert "invoice.pdf" in str(exc) and "--out" in str(exc), exc
    else:
        raise AssertionError("an existing file was written over")
    assert keep.read_bytes() == b"mine"
    # Nor through a path the name spells.
    try:
        _fetch(ec, "../work/invoice.pdf", b"theirs")
    except ec.EmailError:
        pass
    else:
        raise AssertionError("an existing file was written over")
    assert keep.read_bytes() == b"mine"
    print("PASS an existing file is never overwritten without --out")


def test_out_is_the_operators_choice(ec, work: Path):
    target = work / "chosen.bin"
    target.write_bytes(b"old")
    out = _fetch(ec, "../../evil", b"new", out=str(target))
    assert out["saved"] == str(target), out
    assert target.read_bytes() == b"new"
    # The sender's name is still reported, as declared.
    assert out["filename"] == "../../evil", out
    print("PASS an explicit --out is written as given")


def main():
    ec = _load_email_client()
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "home" / "work"
        work.mkdir(parents=True)
        os.chdir(work)
        try:
            test_a_declared_path_is_a_base_name_here(ec, work)
            test_an_existing_file_is_never_overwritten(ec, work)
            test_out_is_the_operators_choice(ec, work)
        finally:
            os.chdir(cwd)
    print("all fetch-attachment tests passed")


if __name__ == "__main__":
    main()
