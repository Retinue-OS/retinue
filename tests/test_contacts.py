#!/usr/bin/env python3
"""Checks for scripts/contacts.py — channel-independent contacts, one person
per file, stored in the chamber the manifest names (docs/contacts.md).

Covers: manifest locations (a declared path, the default where none is
declared, the opt-out, a declared path that would leave its chamber); handles of every kind (e-mail, SMS,
messenger accounts) and their normalization, a handle filed in an older spelling
among them; the Telegram user id filed next to a name, and its backfill; lookup
by handle, phone and name; the uniqueness of accounts and addresses; lossless rewrites that keep lines the
module did not write; e-mail domains (validation, the lookup's fallback from
an address to the most specific domain, conflicts); the standing instruction
and the projection into the gate, e-mail included; the CLI; and the
best-effort commit in the chamber repo.

Standalone (stdlib only):

    python3 tests/test_contacts.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import contacts  # noqa: E402

CLI = [sys.executable, str(REPO_ROOT / "scripts" / "contacts.py")]


def _setup(tmp: Path) -> tuple[Path, Path]:
    chambers = tmp / "chambers"
    for name in ("private", "work", "archive", "sneaky", "attic", "_generated"):
        (chambers / name).mkdir(parents=True)
    manifest = tmp / "chambers.json"
    manifest.write_text(json.dumps({"chambers": [
        {"name": "private", "path": "x", "contacts": "people"},
        {"name": "work"},
        {"name": "archive", "contacts": False},
        {"name": "sneaky", "contacts": "../private"},
    ]}), encoding="utf-8")
    return chambers, manifest


def test_locations(chambers, manifest):
    book = contacts.ContactBook(chambers, manifest)
    # Declared, or the default; an opted-out chamber has none; a path that
    # would leave the chamber is refused for the default. `attic` is mounted
    # but not listed: a chamber all the same, after the listed ones.
    # `_generated` is the framework's output, no chamber.
    assert [(l["chamber"], l["path"]) for l in book.locations()] == [
        ("private", "people"), ("work", "contacts"), ("sneaky", "contacts"), ("attic", "contacts")]
    assert book.default_chamber() == "private"
    for name, why in (("archive", "keeps no contacts"), ("elsewhere", "no chamber")):
        try:
            book.location(name)
            raise AssertionError(f"{name} has a contact location")
        except contacts.ContactError as exc:
            assert why in str(exc), exc
    # The default is the chamber the last contact was created in, else the
    # first location.
    assert book.default_chamber() == "private"
    older = book.create("private", "Older One", created="2026-01-01T09:00:00+00:00")
    newer = book.create("work", "Newer One", created="2026-02-01T09:00:00Z")
    assert book.default_chamber() == "work"
    book.delete(older["key"]); book.delete(newer["key"])
    # No manifest at all: every chamber, by name, at the default path.
    bare = contacts.ContactBook(chambers, chambers.parent / "no-manifest.json")
    assert [(l["chamber"], l["path"]) for l in bare.locations()] == [
        (n, "contacts") for n in ("archive", "attic", "private", "sneaky", "work")]
    print("ok test_locations")


def test_handles():
    assert contacts.normalize_handle("email", " Mara@Example.ORG ") == ("email", "mara@example.org")
    assert contacts.normalize_handle("email", "mailto:a@b.ch") == ("email", "a@b.ch")
    assert contacts.normalize_handle("sms", "0041 79 123 45 67") == ("sms", "+41791234567")
    assert contacts.normalize_handle("Signal", "+41 (79) 123-45-67") == ("signal", "+41791234567")
    assert contacts.normalize_handle("whatsapp", "123456@lid") == ("whatsapp", "123456@lid")
    # A phone-number JID (a WhatsApp chat key) is the number: the form the
    # gateway delivers a sender in, so a VIP filed from a chat matches.
    assert contacts.normalize_handle("whatsapp", "41791234567@s.whatsapp.net") == ("whatsapp", "+41791234567")
    assert contacts.normalize_handle("matrix", "@mara:example.org") == ("matrix", "@mara:example.org")
    assert contacts.parse_handle("telegram:@mara_k") == ("telegram", "@mara_k")
    # An SMS sender that is no number: an alphanumeric sender id or a short code.
    assert contacts.normalize_handle("sms", " HBL ") == ("sms", "HBL")
    assert contacts.normalize_handle("sms", "Post  CH") == ("sms", "Post CH")
    assert contacts.normalize_handle("sms", "4636") == ("sms", "4636")
    # Any character the SMS gateway keeps may lead, as long as a letter follows.
    assert contacts.normalize_handle("sms", ".HBL") == ("sms", ".HBL")
    # An address is no wildcard: "*@example.org" is a domain, filed as one.
    # (Inside a local part `*` is a character like another.)
    assert contacts.normalize_handle("email", "ma*a@example.org") == ("email", "ma*a@example.org")
    for bad in (("email", "no-at-sign"), ("sms", "not a number at all"), ("sms", "079 123 45 67"),
                ("sms", "<b>HBL</b>"), ("sms", "12"), ("Not A Channel", "x"), ("signal", " "),
                ("email", "*@example.org"), ("email", "*@*.example.org"), ("email", "mara@*.example.org")):
        try:
            contacts.normalize_handle(*bad)
            raise AssertionError(f"accepted {bad}")
        except contacts.ContactError:
            pass
    print("ok test_handles")


def test_domain_spelling():
    """A domain is filed as `example.org` or `*.example.org`, lowercased; the
    gate's spelling of the two is read as what it means; and nothing that
    would cover more than one sender's own domain is taken."""
    for given, filed in (("Example.ORG", "example.org"), (" *.Example.org ", "*.example.org"),
                         ("*@example.org", "example.org"), ("*@*.example.org", "*.example.org"),
                         ("@example.org", "example.org"), ("mail.example.co.uk", "mail.example.co.uk"),
                         ("example.co.uk", "example.co.uk"), ("xn--mnchen-3ya.de", "xn--mnchen-3ya.de"),
                         ("münchen.de", "münchen.de"), ("b-2.example", "b-2.example")):
        assert contacts.normalize_domain(given) == filed, given
    for bad in ("", " ", "*", "*.", "com", "*.com", "*@com", "co.uk", "*.co.uk", "*@*.com.au", "or.jp",
                "ex*ample.org", "*.*.example.org", "a.*.org", "example.*", "*example.org",
                "mara@example.org", "*@mara@example.org", "example.org.", ".example.org", "a..org",
                "-a.org", "a-.org", "exa mple.org", "example.org/path", "<example.org>", "[127.0.0.1]",
                "192.168.0.1", "a" * 64 + ".org", ".".join(["abcdefgh"] * 30)):
        try:
            contacts.normalize_domain(bad)
            raise AssertionError(f"accepted the domain {bad!r}")
        except contacts.ContactError:
            pass
    assert contacts.domain_wildcard("example.org") == "*@example.org"
    assert contacts.domain_wildcard("*.example.org") == "*@*.example.org"
    print("ok test_domain_spelling")


def test_telegram_ids(chambers, manifest):
    # One spelling per username: with or without @, any case.
    for spelled in ("@Mara_K", "mara_k", " @mara_k "):
        assert contacts.normalize_handle("telegram", spelled) == ("telegram", "@mara_k")
    assert contacts.normalize_handle("telegram", "123456789") == ("telegram", "123456789")
    assert contacts.normalize_handle("telegram", "-1001234") == ("telegram", "-1001234")
    assert contacts.normalize_handle("telegram", "0041 79 123 45 67") == ("telegram", "+41791234567")
    try:
        contacts.normalize_handle("telegram", "@no way")
        raise AssertionError("accepted a username with a space")
    except contacts.ContactError:
        pass

    # Telegram delivers the sender's id only: a name or phone gets its id
    # filed next to it; an id needs no lookup; an unknown name is refused.
    known = {"@mara_k": 4242, "+41791234567": 4243}
    asked = []

    def lookup(q):
        asked.append(q)
        return {"id": known[q]} if q in known else None

    pairs = [("telegram", "@mara_k"), ("email", "m@x.ch"), ("telegram", "99")]
    assert contacts.with_telegram_ids(pairs, lookup) == pairs + [("telegram", "4242")]
    assert asked == ["@mara_k"]
    assert contacts.with_telegram_ids([("telegram", "+41791234567")], lookup) == [
        ("telegram", "+41791234567"), ("telegram", "4243")]
    try:
        contacts.with_telegram_ids([("telegram", "@nobody")], lookup)
        raise AssertionError("filed an unknown username")
    except contacts.ContactError as exc:
        assert exc.status == 404

    # Filed with its id, the person is who the gate and the instruction match.
    book = contacts.ContactBook(chambers, manifest)
    record = book.create("private", "Mara", contacts.with_telegram_ids(
        [contacts.parse_handle("telegram:@Mara_K")], lookup), instruction="Ring for anything urgent.")
    assert contacts.instructed_person(book, "telegram", "4242")["name"] == "Mara"
    assert book.find("telegram", "4242")["name"] == "Mara"
    assert "4242" in contacts.instruction_projection(book)["telegram"]
    book.delete(record["key"])

    # Filed before usernames were normalized, in the spelling it was typed:
    # found, owned and removed by today's spelling all the same.
    old = book.create("private", "Mara")
    book._write(Path(old["file"]), old, dict(old, accounts=[("telegram", "@Mara_K")]))
    for spelled in ("@Mara_K", "mara_k", "@mara_k"):
        assert book.find("telegram", spelled)["key"] == old["key"], spelled
    try:
        book.create("work", "Someone Else", [("telegram", "@mara_k")])
        raise AssertionError("the username was claimed twice")
    except contacts.ContactError as exc:
        assert exc.status == 409
    # Filing it again rewrites the old spelling; removing it takes it away.
    again = book.update(old["key"], add=[("telegram", "@mara_k")])
    assert again["accounts"] == [("telegram", "@mara_k")], again
    text = (chambers / old["path"]).read_text(encoding="utf-8")
    assert "Mara_K" not in text and "https://t.me/mara_k" in text, text
    book._write(Path(old["file"]), again, dict(again, accounts=[("telegram", "@Mara_K")]))
    assert book.update(old["key"], remove=[("telegram", "@mara_k")])["accounts"] == []
    assert "Mara_K" not in (chambers / old["path"]).read_text(encoding="utf-8")
    book.delete(old["key"])
    print("ok test_telegram_ids")


class _FakeTelegramGateway:
    """GET /resolve as scripts/telegram-gateway.py answers it: 200 with the
    user (a username in any case, as Telegram has it), 404 for no such user,
    502 for a lookup its client fails."""

    def __init__(self, users: dict, token: str):
        self.users, self.asked, self.failing = users, [], set()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                query = (urllib.parse.parse_qs(url.query).get("q") or [""])[0]
                if self.headers.get("Authorization") != f"Bearer {token}":
                    return self._answer(401, {"error": "unauthorized"})
                fake.asked.append(query)
                if query.lower() in fake.failing:
                    return self._answer(502, {"error": "resolve failed: a flood wait"})
                if url.path != "/resolve" or query.lower() not in fake.users:
                    return self._answer(404, {"error": f"no Telegram user {query!r}"})
                return self._answer(200, {"id": fake.users[query.lower()], "username": None, "phone": None,
                                          "name": query})

            def _answer(self, status, body):
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


def test_telegram_backfill(chambers, manifest):
    """`contacts.py telegram-ids` files the id next to every Telegram name
    filed without one: every lookup before any write, and a contact it cannot
    complete holds up only itself."""
    gateway = _FakeTelegramGateway({"@ines_m": 5001, "+41790000033": 5003, "@twin_b": 5005}, "tg-token")
    env = dict(os.environ, TELEGRAM_GATEWAY_BASE_URL=gateway.url, TELEGRAM_GATEWAY_TOKEN="tg-token",
               TRIAGE_MESSENGER_DIR=str(chambers.parent / "backfill-policy" / "messenger"),
               TRIAGE_EMAIL_WHITELIST_PATH=str(chambers.parent / "backfill-policy" / "email.nt"))

    def backfill():
        proc = subprocess.run(CLI + ["--chambers-dir", str(chambers), "--manifest", str(manifest), "--json",
                                     "telegram-ids"], capture_output=True, text=True, env=env)
        return proc.returncode, json.loads(proc.stdout or "null"), proc.stderr

    # Filed before ids were resolved: a username in the spelling it was
    # typed, a number, and a name whose id another person already holds.
    book = contacts.ContactBook(chambers, manifest)
    ines = book.create("work", "Ines Moser", instruction="Ring for anything urgent.")
    book._write(Path(ines["file"]), ines, dict(ines, accounts=[("telegram", "@Ines_M")]))
    otto = book.create("work", "Otto Frei", [("telegram", "+41790000033")])
    twin_a = book.create("work", "Twin A", [("telegram", "5005")])
    twin_b = book.create("work", "Twin B", [("telegram", "@twin_b")])
    people = [ines, otto, twin_a, twin_b]
    try:
        # A gateway that fails on the last name changes nobody — not even
        # the people looked up before it.
        files = {p["key"]: (chambers / p["path"]).read_text(encoding="utf-8") for p in people}
        gateway.failing = {"@twin_b"}
        code, out, err = backfill()
        assert code == 2 and "HTTP 502" in err, (code, out, err)
        assert {k: (chambers / book.get(k)["path"]).read_text(encoding="utf-8") for k in files} == files
        gateway.failing = set()

        # And a name Telegram no longer knows, and a link filed by hand
        # before handles were checked — no name to look up.
        gone = book.create("work", "Gone Person", [("telegram", "@gone_away")])
        linked = book.create("work", "Link Only")
        book._write(Path(linked["file"]), linked, dict(linked, accounts=[("telegram", "t.me/link_only")]))
        people += [gone, linked]
        code, out, err = backfill()
        assert code == 2, (code, out, err)
        assert sorted((p["name"], h["handle"]) for p in out for h in p["handles"]) == [
            ("Ines Moser", "5001"), ("Ines Moser", "@Ines_M"),
            ("Otto Frei", "+41790000033"), ("Otto Frei", "5003")], out
        assert "@ines_m" in gateway.asked, "looked up in today's spelling"
        assert "Gone Person" in err and "knows no user @gone_away" in err, err
        assert "Twin B" in err and "already belongs to Twin A" in err, err
        assert "Link Only" in err and "not a Telegram username" in err, err
        assert not [q for q in gateway.asked if "link_only" in q], gateway.asked
        assert book.get(gone["key"])["accounts"] == [("telegram", "@gone_away")]
        assert book.get(twin_b["key"])["accounts"] == [("telegram", "@twin_b")]
        # Written is synced: the gate learns Ines's id for her instruction.
        policy = Path(env["TRIAGE_MESSENGER_DIR"]) / "telegram" / "policy" / "policy.nt"
        assert '<https://w3id.org/retinue/kb#triageInstructedHandle> "5001"' in policy.read_text(encoding="utf-8")

        # Once those are settled, a second run finds nothing left to do.
        for person in (gone, linked, twin_b):
            book.delete(person["key"])
        assert backfill()[:2] == (0, [])
    finally:
        gateway.server.shutdown()
        for person in people:
            if book.get(person["key"]):
                book.delete(person["key"])
    print("ok test_telegram_backfill")


def test_create_find_update(chambers, manifest):
    book = contacts.ContactBook(chambers, manifest)
    mara = book.create("private", "Mara Keller", [("email", "mara@example.org"), ("signal", "+41791234567")],
                       sphere="friends", tags=["customers", "friends"])
    assert mara["chamber"] == "private" and mara["path"].startswith("private/people/mara-keller-"), mara
    assert mara["tags"] == ["customers"], "the main sphere is not a further one"
    text = (chambers / mara["path"]).read_text(encoding="utf-8")
    for needle in ('<http://www.w3.org/2006/vcard/ns#Individual>', '<mailto:mara@example.org>',
                   '<tel:+41791234567>', '<sgnl://signal.me/#p/+41791234567>',
                   '<http://xmlns.com/foaf/0.1/accountServiceHomepage> <https://signal.org/>',
                   '<https://w3id.org/retinue/kb#channel> "signal"'):
        assert needle in text, (needle, text)
    # Found by every handle, and by the number on a channel she was not filed on.
    assert book.find("email", "MARA@example.org")["key"] == mara["key"]
    assert book.find("signal", "+41 79 123 45 67")["key"] == mara["key"]
    assert book.find("sms", "+41791234567")["key"] == mara["key"], "an account's number is a telephone"
    assert book.find("whatsapp", "+41791234567") is None
    assert [(r["key"], r["exact"]) for r in book.suggest("whatsapp", "+41791234567")] == [(mara["key"], False)]
    assert [r["key"] for r in book.search("kell")] == [mara["key"]]
    assert [r["key"] for r in book.search("mara@")] == [mara["key"]]
    # An account or an address belongs to one person; a telephone may be shared.
    for pairs in ([("email", "mara@example.org")], [("signal", "+41791234567")]):
        try:
            book.create("work", "Someone", pairs)
            raise AssertionError("a handle was claimed twice")
        except contacts.ContactError as exc:
            assert exc.status == 409
    home = book.create("work", "Mara's flatmate", [("sms", "+41791234567")])
    assert home["phones"] == ["+41791234567"]
    # An alphanumeric SMS sender is an account of its own: no telephone, found
    # case-insensitively (as the gate matches it), and claimed by one person.
    bank = book.create("work", "HBL", [("sms", "HBL")])
    assert bank["accounts"] == [("sms", "HBL")] and bank["phones"] == [], bank
    text = (chambers / bank["path"]).read_text(encoding="utf-8")
    assert '<https://w3id.org/retinue/kb#channel> "sms"' in text and "<tel:" not in text, text
    assert book.find("sms", "hbl")["key"] == bank["key"]
    assert {"channel": "sms", "handle": "HBL"} in contacts.public(book.get(bank["key"]))["handles"]
    assert ("sms", "HBL") in set(contacts._gate_handles(book.get(bank["key"])))
    try:
        book.create("private", "Someone", [("sms", "HBL")])
        raise AssertionError("a sender id was claimed twice")
    except contacts.ContactError as exc:
        assert exc.status == 409
    # Another spelling of the same sender id replaces it rather than adding a
    # second handle, and removes it whatever its case.
    assert book.update(bank["key"], add=[("sms", "hbl")])["accounts"] == [("sms", "hbl")]
    assert book.update(bank["key"], remove=[("sms", "HBL")])["accounts"] == []
    book.delete(bank["key"])
    # A line somebody else wrote survives every rewrite.
    path = chambers / mara["path"]
    extra = f'<{mara["iri"]}> <http://www.w3.org/2006/vcard/ns#bday> "1990-04-01" .'
    path.write_text(path.read_text(encoding="utf-8") + extra + "\n# a note\n", encoding="utf-8")
    moved = book.update(mara["key"], name="Mara K.", sphere=None, tags=["family"],
                        add=[("whatsapp", "+41791234567")], remove=[("signal", "+41791234567")])
    assert moved["name"] == "Mara K." and moved["sphere"] is None and moved["tags"] == ["family"], moved
    assert moved["accounts"] == [("whatsapp", "+41791234567")], moved
    text = path.read_text(encoding="utf-8")
    assert extra in text and "# a note" in text, text
    assert "sgnl://" not in text and "https://wa.me/41791234567" in text, text
    assert "<tel:+41791234567>" in text, "the WhatsApp account keeps the number"
    assert path.name.startswith("mara-keller-"), "the file name stays put across a rename"
    # Removing the person keeps what others wrote into the file.
    book.delete(mara["key"])
    assert path.exists() and extra in path.read_text(encoding="utf-8")
    assert book.get(mara["key"]) is None
    try:
        book.create("private", "", [])
        raise AssertionError("a nameless contact")
    except contacts.ContactError:
        pass
    print("ok test_create_find_update")


def test_hand_written_person(chambers, manifest):
    """A person a human wrote (foaf:Person, a phone and an address in plain
    vCard) is found by those handles too."""
    book = contacts.ContactBook(chambers, manifest)
    path = chambers / "work" / "contacts" / "hand.nt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<https://example.org/people#jo> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://xmlns.com/foaf/0.1/Person> .\n'
        '<https://example.org/people#jo> <http://xmlns.com/foaf/0.1/name> "Jo Imhof" .\n'
        '<https://example.org/people#jo> <http://www.w3.org/2006/vcard/ns#hasEmail> <mailto:jo@example.org> .\n'
        '<https://example.org/people#jo> <http://www.w3.org/2006/vcard/ns#hasTelephone> <tel:+41 44 000 00 00> .\n',
        encoding="utf-8")
    jo = book.find("email", "jo@example.org")
    assert jo and jo["name"] == "Jo Imhof" and jo["chamber"] == "work", jo
    assert book.find("sms", "+41440000000")["key"] == jo["key"]
    updated = book.update(jo["key"], add=[("telegram", "@jo_imhof")])
    assert ("telegram", "@jo_imhof") in updated["accounts"] and updated["name"] == "Jo Imhof", updated
    assert '"Jo Imhof"' in path.read_text(encoding="utf-8")
    print("ok test_hand_written_person")


def _refused(call, status: int) -> None:
    try:
        call()
    except contacts.ContactError as exc:
        assert exc.status == status, (exc, exc.status)
    else:
        raise AssertionError("accepted what should have been refused")


def test_domains(chambers, manifest):
    """A person can carry the e-mail domains they write from (kb:emailDomain).
    The lookup of an address falls back from the address to the most specific
    domain and marks such a hit; an address inside a domain can still be a
    person's own; a domain belongs to one person, like a handle; and what the
    gate is given agrees with what the lookup answers."""
    book = contacts.ContactBook(chambers, manifest)
    acme = book.create("work", "ACME GmbH", [("email", "office@acme.example")],
                       domains=["ACME.example", "*@acme.example"], instruction="File the invoice.")
    assert acme["domains"] == ["acme.example"], acme
    text = (chambers / acme["path"]).read_text(encoding="utf-8")
    assert f'<{acme["iri"]}> <https://w3id.org/retinue/kb#emailDomain> "acme.example" .' in text, text
    shown = contacts.public(acme)
    # A domain is no handle — nothing can be sent to it — and is listed apart.
    assert shown["domains"] == ["acme.example"], shown
    assert shown["handles"] == [{"channel": "email", "handle": "office@acme.example"}], shown

    # The address itself: its holder, unmarked. Any other address there: the
    # same person by the domain, marked, and nobody when only the exact owner
    # is asked for.
    hit = book.find("email", "office@acme.example")
    assert hit["key"] == acme["key"] and "match" not in hit, hit
    hit = book.find("email", " Billing@ACME.example ")
    assert (hit["key"], hit["match"], hit["match_domain"]) == (acme["key"], "domain", "acme.example"), hit
    assert book.find("email", "billing@acme.example", exact=True) is None
    assert "match" not in book.get(acme["key"]), "the mark is on a copy, not on the book's record"
    # A plain domain covers its own addresses only: no subdomain, no look-alike.
    for other in ("x@eu.acme.example", "x@notacme.example", "x@acme.example.evil.org", "x@example"):
        assert book.find("email", other) is None, other
    # No other channel has such a fallback.
    assert book.find("signal", "+41790000123") is None
    assert book.find("matrix", "@x:acme.example") is None

    # The most specific filing wins: the domain itself before a wildcard that
    # also covers it, a longer wildcard before a shorter one — and a wildcard
    # covers its own apex, as the gate reads it.
    group = book.create("work", "ACME Group", domains=["*.acme.example"], vip=True)
    eu = book.create("work", "ACME Europe", domains=["*.eu.acme.example"])
    assert book.find("email", "x@acme.example")["key"] == acme["key"]
    assert book.find("email", "x@us.acme.example")["key"] == group["key"]
    for address in ("x@eu.acme.example", "x@de.eu.acme.example"):
        hit = book.find("email", address)
        assert (hit["key"], hit["match_domain"]) == (eu["key"], "*.eu.acme.example"), (address, hit)
    assert [r["key"] for r in book.email_matches("x@de.eu.acme.example")] == [eu["key"], group["key"]]
    assert [r["key"] for r in book.email_matches("office@acme.example")] == [acme["key"], group["key"]]
    assert book.email_matches("x@elsewhere.example") == []

    # An address inside somebody's domain is free to be a person's own, and
    # is then that person's — exactly, with no mark.
    jo = book.create("private", "Jo at ACME", [("email", "jo@acme.example")])
    hit = book.find("email", "jo@acme.example")
    assert hit["key"] == jo["key"] and "match" not in hit, hit
    # The card's suggestions: the owner alone — or, for an address nobody
    # holds, the person whose domain covers it, as a guess to confirm.
    assert [(r["key"], r["exact"]) for r in book.suggest("email", "jo@acme.example")] == [(jo["key"], True)]
    assert [(r["key"], r["exact"], r["match"]) for r in book.suggest("email", "sales@acme.example")] == [
        (acme["key"], False, "domain")]
    assert book.suggest("email", "x@elsewhere.example") == []
    assert {r["key"] for r in book.search("acme.exam")} == {acme["key"], group["key"], eu["key"], jo["key"]}

    # The instruction that applies to a mail is the most specific person's
    # who carries one: an organisation's brief stands for a member the book
    # knows by name but gives no brief of their own.
    hit = contacts.instructed_person(book, "email", "jo@acme.example")
    assert (hit["key"], hit["match"]) == (acme["key"], "domain"), hit
    assert contacts.instructed_person(book, "email", "x@us.acme.example") is None, "a VIP is not a brief"
    book.update(group["key"], instruction="Group brief.")
    assert contacts.instructed_person(book, "email", "x@eu.acme.example")["key"] == group["key"]
    book.update(jo["key"], instruction="Ring me.")
    hit = contacts.instructed_person(book, "email", "JO@acme.example")
    assert hit["key"] == jo["key"] and "match" not in hit, hit

    # The projection: addresses and the domains' wildcard forms, on `email`.
    assert contacts.policy_projection(book) == {"email": {"*@*.acme.example"}}
    assert contacts.instruction_projection(book) == {"email": {
        "office@acme.example", "*@acme.example", "jo@acme.example", "*@*.acme.example"}}
    # And the gate, given exactly that, says of every sender what the lookup
    # says: instructed where a brief is found, VIP where someone in the chain is.
    import triage_policy
    with tempfile.TemporaryDirectory() as tmp:
        policy = Path(tmp) / "policy.nt"
        policy.write_text(triage_policy.render_messenger_policy("email", triage_policy.MessengerPolicy(
            set(), set(), set(), set(), contacts.policy_projection(book)["email"],
            contacts.instruction_projection(book)["email"])), encoding="utf-8")
        for sender in ("office@acme.example", "jo@acme.example", "x@acme.example", "x@us.acme.example",
                       "x@de.eu.acme.example", "x@notacme.example", "x@acme.example.evil.org",
                       "x@elsewhere.example"):
            gate = triage_policy.gate_decision("email", sender, path=policy)
            assert gate["instructed"] == (contacts.instructed_person(book, "email", sender) is not None), sender
            assert gate["vip"] == any(r["vip"] for r in book.email_matches(sender)), sender

    # A domain claimed by two people is a conflict like a handle claimed
    # twice — in any spelling, on create and on update, and nothing is written.
    _refused(lambda: book.create("private", "Other", domains=["acme.example"]), 409)
    _refused(lambda: book.update(jo["key"], add_domains=["*@*.ACME.example"], name="Renamed"), 409)
    _refused(lambda: book.update(jo["key"], domains=["*.eu.acme.example"]), 409)
    assert book.get(jo["key"])["domains"] == [] and book.get(jo["key"])["name"] == "Jo at ACME"
    assert not [r for r in book.all() if r["name"] == "Other"]
    # One's own domain again is no conflict; a list replaces, then add and
    # remove apply; None leaves them and an empty list clears them.
    assert book.update(acme["key"], add_domains=["acme.example"])["domains"] == ["acme.example"]
    assert book.update(acme["key"], domains=["acme.example", "acme-mail.example"])["domains"] == [
        "acme-mail.example", "acme.example"]
    changed = book.update(acme["key"], remove_domains=["*@acme-mail.example"], add_domains=["*.acme-pay.example"])
    assert changed["domains"] == ["*.acme-pay.example", "acme.example"], changed
    assert "acme-mail.example" not in (chambers / acme["path"]).read_text(encoding="utf-8")
    for bad in ("com", "co.uk", "*.com", "sales@acme.example", "ac*me.example"):
        _refused(lambda: book.update(acme["key"], add_domains=[bad]), 400)
        _refused(lambda: book.create("work", "Too Wide", domains=[bad]), 400)
    assert book.update(acme["key"], name="ACME AG")["domains"] == ["*.acme-pay.example", "acme.example"]
    assert book.update(eu["key"], domains=[])["domains"] == []
    assert "emailDomain" not in (chambers / eu["path"]).read_text(encoding="utf-8")
    assert book.find("email", "x@eu.acme.example")["key"] == group["key"]

    # Written by hand: a domain is read in today's spelling; a value that is
    # none (a public suffix) is not matched on, and stays in the file; and an
    # "address" with a wildcard in it never reaches the gate as a domain.
    path = chambers / "work" / "contacts" / "hand-org.nt"
    person = "<https://example.org/orgs#hand>"
    path.write_text(
        f'{person} <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://xmlns.com/foaf/0.1/Person> .\n'
        f'{person} <http://xmlns.com/foaf/0.1/name> "Hand Org" .\n'
        f'{person} <https://w3id.org/retinue/kb#emailDomain> "Hand.Example" .\n'
        f'{person} <https://w3id.org/retinue/kb#emailDomain> "com" .\n'
        f'{person} <http://www.w3.org/2006/vcard/ns#hasEmail> <mailto:*@wild.example> .\n',
        encoding="utf-8")
    hand = book.find("email", "a@hand.example")
    assert (hand["name"], hand["domains"], hand["match"]) == ("Hand Org", ["hand.example"], "domain"), hand
    assert book.find("email", "a@anything.com") is None
    assert book.find("email", "a@wild.example") is None
    book.update(hand["key"], vip=True)
    text = path.read_text(encoding="utf-8")
    assert '"com"' in text, "a line this module would not write is kept"
    assert '"Hand.Example"' not in text and '"hand.example"' in text, "rewritten in today's spelling"
    assert contacts.policy_projection(book)["email"] == {"*@*.acme.example", "*@hand.example"}

    # A value read from a spelling of its own is removed like any other: the
    # domain written in the gate's spelling, an address in capitals, a spaced
    # telephone, a VIP flag "1" — none comes back on the next read.
    spelled = chambers / "work" / "contacts" / "hand-spelled.nt"
    person = "<https://example.org/orgs#spelled>"
    spelled.write_text(
        f'{person} <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://xmlns.com/foaf/0.1/Person> .\n'
        f'{person} <http://xmlns.com/foaf/0.1/name> "Spelled Org" .\n'
        f'{person} <https://w3id.org/retinue/kb#emailDomain> "*@spelled.example" .\n'
        f'{person} <https://w3id.org/retinue/kb#vip> "1" .\n'
        f'{person} <https://w3id.org/retinue/kb#importance> "4.50" .\n'
        f'{person} <http://www.w3.org/2006/vcard/ns#hasEmail> <mailto:Info@Spelled.example> .\n'
        f'{person} <http://www.w3.org/2006/vcard/ns#hasTelephone> <tel:+41 44 111 11 11> .\n',
        encoding="utf-8")
    sp = book.find("email", "a@spelled.example")
    assert (sp["domains"], sp["vip"], sp["emails"], sp["phones"], sp["importance"]) == (
        ["spelled.example"], True, ["info@spelled.example"], ["+41441111111"], 4.5), sp
    assert "*@spelled.example" in contacts.policy_projection(book)["email"]
    book.update(sp["key"], remove_domains=["spelled.example"], vip=False, importance=None,
                remove=[("email", "info@spelled.example"), ("sms", "+41441111111")])
    book._cache.clear()
    sp = book.get(sp["key"])
    assert (sp["domains"], sp["vip"], sp["emails"], sp["phones"], sp["importance"]) == (
        [], False, [], [], None), sp
    assert "*@spelled.example" not in contacts.policy_projection(book).get("email", set())
    text = spelled.read_text(encoding="utf-8")
    for gone in ("emailDomain", "kb#vip", "importance", "Info@", "tel:"):
        assert gone not in text, (gone, text)
    assert '"Spelled Org"' in text
    book.delete(sp["key"])
    assert not spelled.exists() or "emailDomain" not in spelled.read_text(encoding="utf-8")

    for person in (acme, group, eu, jo, hand):
        book.delete(person["key"])
    path.unlink()
    assert contacts.policy_projection(book) == {} and contacts.instruction_projection(book) == {}
    print("ok test_domains")


def test_attention_and_vip(chambers, manifest):
    """The person carries the attention model's facts and the VIP flag, and
    the VIPs project into the delivery gate's policy."""
    book = contacts.ContactBook(chambers, manifest)
    lou = book.create("work", "Lou Berg", [("telegram", "@lou_berg"), ("email", "lou@example.org")],
                      importance=4.5, permits=["Focused", "social"], vip=True)
    assert (lou["importance"], lou["permits"], lou["vip"]) == (4.5, ["focused", "social"], True), lou
    text = (chambers / lou["path"]).read_text(encoding="utf-8")
    assert '<https://w3id.org/retinue/kb#importance> "4.5"^^<http://www.w3.org/2001/XMLSchema#decimal>' in text
    assert "<urn:retinue:mode:focused>" in text and '"true"^^<http://www.w3.org/2001/XMLSchema#boolean>' in text
    # One table, channel → handles: her address is a handle of the `email`
    # channel like any other (no second return value, no whitelist).
    handles = contacts.policy_projection(book)
    assert handles == {"telegram": {"@lou_berg"}, "email": {"lou@example.org"}}, handles
    for bad in ({"importance": 7}, {"importance": "high"}, {"permits": ["not a mode!"]}):
        try:
            book.update(lou["key"], **bad)
            raise AssertionError(f"accepted {bad}")
        except contacts.ContactError:
            pass
    lou = book.update(lou["key"], importance=None, permits=[], vip=False)
    assert (lou["importance"], lou["permits"], lou["vip"]) == (None, [], False), lou
    assert "kb#vip" not in (chambers / lou["path"]).read_text(encoding="utf-8")
    assert contacts.policy_projection(book) == {}
    book.delete(lou["key"])
    # A handle filed before a normalization rule existed projects in its
    # current form — the one the gateway compares against.
    old = book.create("work", "Old Filing", [("telegram", "@old_filing")], vip=True)
    path = chambers / old["path"]
    path.write_text(path.read_text(encoding="utf-8").replace('"@old_filing"', '"41791234567@s.whatsapp.net"')
                    .replace('"telegram"', '"whatsapp"'), encoding="utf-8")
    handles = contacts.policy_projection(book)
    assert handles == {"whatsapp": {"+41791234567"}}, handles
    book.delete(old["key"])
    print("ok test_attention_and_vip")


def test_instruction(chambers, manifest):
    """A person carries the user's standing instruction (kb:instruction): kept
    in their file, projected into the gate as the `instructed` class by every
    handle they have, e-mail included, and found again from a handle the way
    the gate matched it."""
    book = contacts.ContactBook(chambers, manifest)
    brief = "Draft a reply to every message.\nRing me only if a visit moves to today."
    spitex = book.create("work", "Spitex Team", [("signal", "+41790000077"), ("email", "team@spitex.example")],
                         instruction="  " + brief + "  ")
    assert spitex["instruction"] == brief, spitex
    assert contacts.public(spitex)["instruction"] == brief
    text = (chambers / spitex["path"]).read_text(encoding="utf-8")
    assert '<https://w3id.org/retinue/kb#instruction> "Draft a reply to every message.\\nRing me' in text, text
    # Not a VIP: the instruction is its own class.
    assert contacts.policy_projection(book) == {}
    projected = contacts.instruction_projection(book)
    assert projected == {"signal": {"+41790000077"}, "sms": {"+41790000077"},
                         "email": {"team@spitex.example"}}, projected
    # The lookup matches as the gate does — the projected form, any case.
    assert contacts.instructed_person(book, "signal", "+41 79 000 00 77")["key"] == spitex["key"]
    assert contacts.instructed_person(book, "sms", "0041790000077")["key"] == spitex["key"]
    assert contacts.instructed_person(book, "whatsapp", "+41790000077") is None
    assert contacts.instructed_person(book, "email", " Team@Spitex.example ")["key"] == spitex["key"]
    assert contacts.instructed_person(book, "email", "other@spitex.example") is None
    assert contacts.instructed_person(book, "email", "not an address") is None
    # A handle filed before a normalization rule still finds its person.
    old = book.create("work", "Old Filing", [("telegram", "@old_filing")], instruction="Flag everything.")
    path = chambers / old["path"]
    path.write_text(path.read_text(encoding="utf-8").replace('"@old_filing"', '"41791234567@s.whatsapp.net"')
                    .replace('"telegram"', '"whatsapp"'), encoding="utf-8")
    assert contacts.instructed_person(book, "whatsapp", "+41791234567")["key"] == old["key"]
    book.delete(old["key"])
    # Too long is refused; an empty one, and None, remove it; False leaves it.
    try:
        book.update(spitex["key"], instruction="x" * (contacts.INSTRUCTION_MAX + 1))
        raise AssertionError("accepted an instruction past the cap")
    except contacts.ContactError:
        pass
    assert book.update(spitex["key"], vip=True)["instruction"] == brief
    assert book.update(spitex["key"], instruction="   ")["instruction"] is None
    assert "kb#instruction" not in (chambers / spitex["path"]).read_text(encoding="utf-8")
    assert contacts.instruction_projection(book) == {}
    assert contacts.instructed_person(book, "signal", "+41790000077") is None
    book.delete(spitex["key"])
    print("ok test_instruction")


def test_written_by_hand_twice(chambers, manifest):
    """Every value the read widened to a spelling of its own, written by hand
    twice: a rewrite takes away only a literal the read took into the record
    (in another spelling, or joined into it), and an unrelated update leaves
    every literal the record did not take exactly as the user wrote it."""
    book = contacts.ContactBook(chambers, manifest)
    path = chambers / "work" / "contacts" / "hand-twice.nt"
    person = "<https://example.org/people#twice>"
    kb = "https://w3id.org/retinue/kb#"
    vc = "http://www.w3.org/2006/vcard/ns#"
    unread = [  # literals the read does not take: they never change
        f'{person} <{kb}emailDomain> "com" .',
        f'{person} <{kb}vip> "yes" .',
        f'{person} <{kb}importance> "high" .',
        f'{person} <{kb}instruction> "" .',
    ]
    house = f'{person} <{kb}instruction> "Ring me for the house sale." .'
    invoices = f'{person} <{kb}instruction> "Also: forward invoices to my accountant." .'
    lower = f'{person} <{kb}importance> "2" .'
    path.write_text("\n".join([
        f'{person} <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://xmlns.com/foaf/0.1/Person> .',
        f'{person} <http://xmlns.com/foaf/0.1/name> "Twice Person" .',
        f'{person} <{kb}emailDomain> "*@twice.example" .',
        f'{person} <{kb}emailDomain> "Twice.Example" .',
        f'{person} <{vc}hasEmail> <mailto:Mara@Twice.example> .',
        f'{person} <{vc}hasEmail> <mailto:mara@twice.example> .',
        f'{person} <{vc}hasTelephone> <tel:+41 44 222 22 22> .',
        f'{person} <{vc}hasTelephone> <tel:0041442222222> .',
        f'{person} <{kb}vip> "1" .',
        f'{person} <{kb}vip> "TRUE" .',
        f'{person} <{kb}importance> "4.50" .',
        lower, house, invoices, *unread]) + "\n", encoding="utf-8")
    rec = book.find("email", "mara@twice.example")
    assert (rec["domains"], rec["emails"], rec["phones"], rec["vip"], rec["importance"]) == (
        ["twice.example"], ["mara@twice.example"], ["+41442222222"], True, 4.5), rec
    # Both instructions are the record's: neither is hidden behind the other.
    assert rec["instruction"] == ("Also: forward invoices to my accountant.\n\n"
                                  "Ring me for the house sale."), rec["instruction"]

    # An unrelated update: what the record took is written once, in today's
    # spelling; the two instructions and everything unread stay as written.
    book.update(rec["key"], add_domains=["twice.org"])
    text = path.read_text(encoding="utf-8")
    for line in (house, invoices, lower, *unread):
        assert line in text.splitlines(), (line, text)
    for gone in ('"*@twice.example"', '"Twice.Example"', "mailto:Mara@", "tel:+41 44", "tel:0041",
                 '"1"', '"TRUE"', '"4.50"'):
        assert gone not in text, (gone, text)
    assert text.count("kb#emailDomain> \"twice.example\"") == 1, text
    assert text.count("<mailto:mara@twice.example>") == 1 and text.count("<tel:+41442222222>") == 1, text
    book._cache.clear()
    again = book.get(rec["key"])
    for field in ("emails", "phones", "vip", "importance", "instruction"):
        assert again[field] == rec[field], (field, again[field], rec[field])
    assert again["domains"] == ["twice.example", "twice.org"], again

    # The migration's way: one more rule on the instruction the user was
    # shown. Both of theirs are in it; the rewrite writes one literal.
    rule = "Archive their newsletters."
    book.update(rec["key"], instruction=again["instruction"] + "\n\n" + rule)
    text = path.read_text(encoding="utf-8")
    assert house not in text and invoices not in text, text
    book._cache.clear()
    joined = book.get(rec["key"])["instruction"]
    assert "Ring me for the house sale." in joined and "forward invoices" in joined and rule in joined, joined

    # Changing a value replaces every literal it was read from: neither a
    # lower importance nor a second VIP spelling comes back in its place.
    book.update(rec["key"], importance=1, vip=False)
    book._cache.clear()
    after = book.get(rec["key"])
    assert (after["importance"], after["vip"]) == (1.0, False), after
    assert lower not in path.read_text(encoding="utf-8").splitlines()
    book.update(rec["key"], importance=None, instruction=None,
                remove=[("email", "mara@twice.example"), ("sms", "+41442222222")],
                remove_domains=["twice.example", "twice.org"])
    book._cache.clear()
    after = book.get(rec["key"])
    assert (after["importance"], after["vip"], after["instruction"], after["emails"], after["phones"],
            after["domains"]) == (None, False, None, [], [], []), after
    text = path.read_text(encoding="utf-8")
    for line in unread:
        assert line in text.splitlines(), (line, text)
    book.delete(rec["key"])
    path.unlink(missing_ok=True)
    print("ok test_written_by_hand_twice")


def test_a_joined_instruction_past_the_limit(chambers, manifest):
    """Two instruction literals of a hand edit or a merge that join past
    INSTRUCTION_MAX: read whole, never trapped. Every write that leaves the
    instruction — the editors send back the text they showed — succeeds and
    keeps both literals as written; a write that changes it is held to the
    limit, and the refusal says how long the one they carry is."""
    book = contacts.ContactBook(chambers, manifest)
    first = "Ring me for the house sale. " * 50
    rec = book.create("work", "Long Brief", [("email", "long@brief.example")], instruction=first)
    path = chambers / rec["path"]
    second = "Forward invoices to my accountant. " * 30
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f'<{rec["iri"]}> <{contacts.KB}instruction> "{second}" .\n')
    book._cache.clear()
    shown = book.get(rec["key"])["instruction"]
    assert len(shown) > contacts.INSTRUCTION_MAX, len(shown)
    literals = sorted(line for line in path.read_text(encoding="utf-8").splitlines() if "kb#instruction" in line)
    # What the Contacts page and the contact card send on any save: the
    # instruction they showed, unchanged — with \r\n line ends, too.
    after = book.update(rec["key"], name="Long Brief Renamed", vip=True,
                        instruction=shown.replace("\n", "\r\n") + "  ")
    assert (after["name"], after["vip"], after["instruction"]) == ("Long Brief Renamed", True, shown), after
    assert sorted(line for line in path.read_text(encoding="utf-8").splitlines()
                  if "kb#instruction" in line) == literals
    assert book.update(rec["key"], importance=3)["instruction"] == shown
    # A change is held to the limit, and the refusal states the length now.
    for changed in (shown + " More.", shown[:-10]):
        try:
            book.update(rec["key"], instruction=changed)
            raise AssertionError("accepted a changed instruction past the cap")
        except contacts.ContactError as exc:
            assert f"not {len(changed.strip())}; the one they carry now is {len(shown)}" in str(exc), str(exc)
    # Copying it to someone else is a change there: refused the same way.
    other = book.create("work", "Copy Target", [("email", "copy@brief.example")])
    try:
        book.update(other["key"], instruction=shown)
        raise AssertionError("copied an instruction past the cap")
    except contacts.ContactError as exc:
        assert f"at most {contacts.INSTRUCTION_MAX} characters, not {len(shown)}" in str(exc), str(exc)
    # Cut to the limit, it is saved as one literal; taken off, it is gone.
    cut = book.update(rec["key"], instruction=shown[:contacts.INSTRUCTION_MAX])
    assert cut["instruction"] == shown[:contacts.INSTRUCTION_MAX].strip(), cut
    assert sum("kb#instruction" in line for line in path.read_text(encoding="utf-8").splitlines()) == 1
    assert book.update(rec["key"], instruction=None)["instruction"] is None
    book.delete(rec["key"])
    book.delete(other["key"])
    print("ok test_a_joined_instruction_past_the_limit")


def test_cli(chambers, manifest):
    env = dict(os.environ,
               TRIAGE_MESSENGER_DIR=str(chambers.parent / "policy" / "messenger"),
               TRIAGE_EMAIL_WHITELIST_PATH=str(chambers.parent / "policy" / "email.nt"))
    base = CLI + ["--chambers-dir", str(chambers), "--manifest", str(manifest), "--json"]

    def run(*args, code=0):
        proc = subprocess.run(base + list(args), capture_output=True, text=True, env=env)
        assert proc.returncode == code, (args, proc.returncode, proc.stdout, proc.stderr)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    assert run("locations") == [{"chamber": "private", "path": "people"}, {"chamber": "work", "path": "contacts"},
                                {"chamber": "sneaky", "path": "contacts"}, {"chamber": "attic", "path": "contacts"}]
    # Creating needs a chamber: argparse refuses without one.
    run("add", "--name", "No Chamber", code=2)
    eva = run("add", "--chamber", "private", "--name", "Eva Roth", "--email", "eva@example.org",
              "--phone", "+41 79 000 00 00", "--handle", "whatsapp:+41790000001", "--sphere", "family")
    assert eva["chamber"] == "private" and eva["sphere"] == "family", eva
    assert {"channel": "email", "handle": "eva@example.org"} in eva["handles"], eva
    assert run("find", "--email", "EVA@example.org")[0]["id"] == eva["id"]
    hits = run("find", "--handle", "signal:+41790000001")
    assert hits[0]["id"] == eva["id"] and hits[0]["match"] == "phone", hits
    assert run("find", "--name", "roth")[0]["id"] == eva["id"]
    run("find", "--email", "nobody@example.org", code=1)
    upd = run("update", eva["id"], "--add-email", "eva@work.example", "--remove-handle", "whatsapp:+41790000001")
    assert {h["channel"] for h in upd["handles"]} == {"email", "sms"}, upd
    run("add", "--chamber", "elsewhere", "--name", "X", code=2)
    # --vip projects the person's handles into the gate's policy files: her
    # addresses are VIP handles of the `email` channel, in the mail gateway's
    # policy file — and no longer whitelist entries in the e-mail policy.
    vip = run("update", eva["id"], "--vip", "--importance", "4", "--permit", "focused")
    assert vip["vip"] is True and vip["importance"] == 4 and vip["permits"] == ["focused"], vip
    mail = Path(env["TRIAGE_MESSENGER_DIR"]) / "email" / "policy" / "policy.nt"
    for address in ("eva@example.org", "eva@work.example"):
        assert ('<urn:retinue:triage:email:contacts> <https://w3id.org/retinue/kb#triageVipHandle> '
                f'"{address}" .') in mail.read_text(encoding="utf-8"), mail.read_text(encoding="utf-8")
    assert Path(env["TRIAGE_EMAIL_WHITELIST_PATH"]).read_text(encoding="utf-8") == ""
    sms = Path(env["TRIAGE_MESSENGER_DIR"]) / "sms" / "policy" / "policy.nt"
    assert '"+41790000000"' in sms.read_text(encoding="utf-8")
    run("update", eva["id"], "--no-vip", "--importance", "", "--no-permits")
    assert '"eva@example.org"' not in mail.read_text(encoding="utf-8")
    # An instruction, given to a team at once: one update, several ids, and
    # the gate learns each member's handles as `instructed`.
    max_ = run("add", "--chamber", "work", "--name", "Max Weber", "--handle", "signal:+41790000002")
    lea = run("add", "--chamber", "work", "--name", "Lea Graf", "--handle", "telegram:7311",
              "--instruction", "Draft a reply; ring me if it is about today.")
    assert lea["instruction"] == "Draft a reply; ring me if it is about today.", lea
    team = run("update", eva["id"], max_["id"], "--instruction-from", lea["id"])
    assert [p["instruction"] for p in team] == [lea["instruction"]] * 2, team
    signal = Path(env["TRIAGE_MESSENGER_DIR"]) / "signal" / "policy" / "policy.nt"
    nt = signal.read_text(encoding="utf-8")
    assert '<https://w3id.org/retinue/kb#triageInstructedHandle> "+41790000002"' in nt, nt
    assert "kb#triageVipHandle" not in nt, "an instruction does not make anyone a VIP"
    # The instruction reaches e-mail: Eva's addresses are instructed handles.
    nt = mail.read_text(encoding="utf-8")
    assert '<https://w3id.org/retinue/kb#triageInstructedHandle> "eva@example.org"' in nt, nt
    assert "kb#triageVipHandle" not in nt, nt
    # A name or a handle belongs to one person; contradictory flags are
    # refused; an unknown id changes nobody.
    run("update", eva["id"], max_["id"], "--name", "Twins", code=2)
    run("update", eva["id"], "--instruction", "x", "--no-instruction", code=2)
    run("update", eva["id"], "nobody-here", "--no-instruction", code=2)
    assert run("show", eva["id"])["instruction"] == lea["instruction"]
    assert run("update", eva["id"], max_["id"], "--no-instruction")[0]["instruction"] is None
    assert "triageInstructedHandle" not in signal.read_text(encoding="utf-8")
    assert "triageInstructedHandle" not in mail.read_text(encoding="utf-8")
    # Domains: filed with --add-domain (on add also --domain), in either
    # spelling; an address nobody holds is then found by its domain, marked;
    # and the gate gets the wildcard forms.
    org = run("add", "--chamber", "work", "--name", "ACME GmbH", "--add-domain", "ACME.example",
              "--domain", "*@*.acme-pay.example", "--vip")
    assert org["domains"] == ["*.acme-pay.example", "acme.example"] and org["handles"] == [], org
    hits = run("find", "--email", "Billing@acme.example")
    assert [(h["id"], h["match"], h["match_domain"]) for h in hits] == [(org["id"], "domain", "acme.example")], hits
    hit = run("find", "--email", "eva@example.org")[0]
    assert hit["match"] == "exact" and "match_domain" not in hit, hit
    run("find", "--email", "x@eu.acme.example", code=1)
    assert run("find", "--name", "acme-pay")[0]["id"] == org["id"]
    plain = subprocess.run(CLI + ["--chambers-dir", str(chambers), "--manifest", str(manifest),
                                  "find", "--email", "billing@acme.example"], capture_output=True, text=True, env=env)
    assert "[by the domain acme.example, not this address]" in plain.stdout, plain.stdout
    assert "email-domain:acme.example" in plain.stdout, plain.stdout
    nt = mail.read_text(encoding="utf-8")
    for wildcard in ("*@acme.example", "*@*.acme-pay.example"):
        assert f'<https://w3id.org/retinue/kb#triageVipHandle> "{wildcard}" .' in nt, nt
    upd = run("update", org["id"], "--remove-domain", "*.acme-pay.example", "--add-domain", "mail.acme.example")
    assert upd["domains"] == ["acme.example", "mail.acme.example"], upd
    assert "acme-pay" not in mail.read_text(encoding="utf-8")
    # Refused: a public suffix, a wildcard typed as an address, a domain
    # somebody already has, and a domain for several people at once.
    run("update", org["id"], "--add-domain", "co.uk", code=2)
    run("update", org["id"], "--add-email", "*@acme.example", code=2)
    run("add", "--chamber", "work", "--name", "Rival", "--add-domain", "*@ACME.example", code=2)
    run("update", org["id"], max_["id"], "--add-domain", "team.example", code=2)
    assert run("show", org["id"])["domains"] == ["acme.example", "mail.acme.example"]
    run("delete", org["id"])
    assert "acme" not in mail.read_text(encoding="utf-8")
    print("ok test_cli")


def test_commit(tmp: Path):
    """The file is committed and pushed in its chamber's repository."""
    remote = tmp / "remote.git"
    chambers = tmp / "git-chambers"
    repo = chambers / "private"
    git = lambda *a, cwd=None: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)  # noqa: E731
    git("init", "--bare", "-q", str(remote))
    git("clone", "-q", str(remote), str(repo))
    for key, value in (("user.name", "Test"), ("user.email", "test@example.org")):
        git("config", key, value, cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    git("push", "-q", "origin", "HEAD", cwd=repo)
    book = contacts.ContactBook(chambers, tmp / "no-manifest.json")
    record = book.create("private", "Ada Muster", [("email", "ada@example.org")])
    assert contacts.commit(chambers, record["path"], "chore(contacts): add Ada Muster") is True
    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--name-only", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    assert "chore(contacts): add Ada Muster" in log and record["path"].split("/", 1)[1] in log, log
    assert contacts.commit(chambers, record["path"], "again") is False, "nothing to commit"
    # A chamber directory that is no repository of its own is not committed
    # into the repository around it.
    plain = repo / "nested"
    (plain / "contacts").mkdir(parents=True)
    (plain / "contacts" / "x.nt").write_text("# x\n", encoding="utf-8")
    assert contacts.commit(repo, "nested/contacts/x.nt", "wrong repo") is False
    assert "wrong repo" not in subprocess.run(["git", "-C", str(repo), "log", "--format=%s"],
                                              capture_output=True, text=True).stdout
    print("ok test_commit")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        chambers, manifest = _setup(tmp)
        test_locations(chambers, manifest)
        test_handles()
        test_domain_spelling()
        test_telegram_ids(chambers, manifest)
        test_telegram_backfill(chambers, manifest)
        test_create_find_update(chambers, manifest)
        test_hand_written_person(chambers, manifest)
        test_domains(chambers, manifest)
        test_attention_and_vip(chambers, manifest)
        test_instruction(chambers, manifest)
        test_written_by_hand_twice(chambers, manifest)
        test_a_joined_instruction_past_the_limit(chambers, manifest)
        test_cli(chambers, manifest)
        test_commit(tmp)
    print("all contacts checks passed")


if __name__ == "__main__":
    main()
