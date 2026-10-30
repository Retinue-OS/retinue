#!/usr/bin/env python3
"""Checks for scripts/contacts.py — channel-independent contacts, one person
per file, stored in the chamber the manifest names (docs/contacts.md).

Covers: manifest locations (a declared path, the default where none is
declared, the opt-out, a declared path that would leave its chamber); handles of every kind (e-mail, SMS,
messenger accounts) and their normalization; lookup by handle, phone and name;
the uniqueness of accounts and addresses; lossless rewrites that keep lines the
module did not write; the standing instruction and its projection into the
gate; the CLI; and the best-effort commit in the chamber repo.

Standalone (stdlib only):

    python3 tests/test_contacts.py
"""
import json
import os
import subprocess
import sys
import tempfile
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
    for bad in (("email", "no-at-sign"), ("sms", "not a number"), ("Not A Channel", "x"), ("signal", " ")):
        try:
            contacts.normalize_handle(*bad)
            raise AssertionError(f"accepted {bad}")
        except contacts.ContactError:
            pass
    print("ok test_handles")


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
    print("ok test_telegram_ids")


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
    handles, emails = contacts.policy_projection(book)
    assert handles == {"telegram": {"@lou_berg"}} and emails == {"lou@example.org"}, (handles, emails)
    for bad in ({"importance": 7}, {"importance": "high"}, {"permits": ["not a mode!"]}):
        try:
            book.update(lou["key"], **bad)
            raise AssertionError(f"accepted {bad}")
        except contacts.ContactError:
            pass
    lou = book.update(lou["key"], importance=None, permits=[], vip=False)
    assert (lou["importance"], lou["permits"], lou["vip"]) == (None, [], False), lou
    assert "kb#vip" not in (chambers / lou["path"]).read_text(encoding="utf-8")
    assert contacts.policy_projection(book) == ({}, set())
    book.delete(lou["key"])
    # A handle filed before a normalization rule existed projects in its
    # current form — the one the gateway compares against.
    old = book.create("work", "Old Filing", [("telegram", "@old_filing")], vip=True)
    path = chambers / old["path"]
    path.write_text(path.read_text(encoding="utf-8").replace('"@old_filing"', '"41791234567@s.whatsapp.net"')
                    .replace('"telegram"', '"whatsapp"'), encoding="utf-8")
    handles, _ = contacts.policy_projection(book)
    assert handles == {"whatsapp": {"+41791234567"}}, handles
    book.delete(old["key"])
    print("ok test_attention_and_vip")


def test_instruction(chambers, manifest):
    """A person carries the user's standing instruction (kb:instruction): kept
    in their file, projected into the gate as the `instructed` class by their
    messenger handles alone, and found again from a handle the way the gate
    matched it."""
    book = contacts.ContactBook(chambers, manifest)
    brief = "Draft a reply to every message.\nRing me only if a visit moves to today."
    spitex = book.create("work", "Spitex Team", [("signal", "+41790000077"), ("email", "team@spitex.example")],
                         instruction="  " + brief + "  ")
    assert spitex["instruction"] == brief, spitex
    assert contacts.public(spitex)["instruction"] == brief
    text = (chambers / spitex["path"]).read_text(encoding="utf-8")
    assert '<https://w3id.org/retinue/kb#instruction> "Draft a reply to every message.\\nRing me' in text, text
    # Not a VIP: the instruction is its own class, and the e-mail address is
    # not whitelisted by it.
    assert contacts.policy_projection(book) == ({}, set())
    projected = contacts.instruction_projection(book)
    assert projected == {"signal": {"+41790000077"}, "sms": {"+41790000077"}}, projected
    # The lookup matches as the gate does — the projected form, any case.
    assert contacts.instructed_person(book, "signal", "+41 79 000 00 77")["key"] == spitex["key"]
    assert contacts.instructed_person(book, "sms", "0041790000077")["key"] == spitex["key"]
    assert contacts.instructed_person(book, "whatsapp", "+41790000077") is None
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
    # --vip projects the person's handles into the gate's policy files.
    vip = run("update", eva["id"], "--vip", "--importance", "4", "--permit", "focused")
    assert vip["vip"] is True and vip["importance"] == 4 and vip["permits"] == ["focused"], vip
    policy = (Path(env["TRIAGE_EMAIL_WHITELIST_PATH"])).read_text(encoding="utf-8")
    assert '"eva@example.org"' in policy and "email-whitelist:contacts" in policy, policy
    sms = Path(env["TRIAGE_MESSENGER_DIR"]) / "sms" / "policy" / "policy.nt"
    assert '"+41790000000"' in sms.read_text(encoding="utf-8")
    run("update", eva["id"], "--no-vip", "--importance", "", "--no-permits")
    assert '"eva@example.org"' not in Path(env["TRIAGE_EMAIL_WHITELIST_PATH"]).read_text(encoding="utf-8")
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
    # A name or a handle belongs to one person; contradictory flags are
    # refused; an unknown id changes nobody.
    run("update", eva["id"], max_["id"], "--name", "Twins", code=2)
    run("update", eva["id"], "--instruction", "x", "--no-instruction", code=2)
    run("update", eva["id"], "nobody-here", "--no-instruction", code=2)
    assert run("show", eva["id"])["instruction"] == lea["instruction"]
    assert run("update", eva["id"], max_["id"], "--no-instruction")[0]["instruction"] is None
    assert "triageInstructedHandle" not in signal.read_text(encoding="utf-8")
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
        test_telegram_ids(chambers, manifest)
        test_create_find_update(chambers, manifest)
        test_hand_written_person(chambers, manifest)
        test_attention_and_vip(chambers, manifest)
        test_instruction(chambers, manifest)
        test_cli(chambers, manifest)
        test_commit(tmp)
    print("all contacts checks passed")


if __name__ == "__main__":
    main()
