#!/usr/bin/env python3
"""Channel-independent contacts: one person, one file, in one chamber.

A contact is a *person*, not a handle. Whatever channels reach them — an
e-mail address, a phone number, a Signal, WhatsApp or Telegram account — hang
off that one person, so "who is +41 79 …?", "what is Mara's e-mail?" and "every
chat with Mara" are questions about the same resource (docs/contacts.md).

Where contacts live
-------------------
Every contact belongs to exactly one chamber and is stored inside it, so it is
versioned with that chamber's data and indexed by the life store like any other
chamber file. Each chamber entry in the deployment's ``chambers.json`` may
declare where its contacts go::

    {"name": "private", "url": "…", "contacts": "people"}

``contacts`` is a directory relative to the chamber root; left out, it is
:data:`DEFAULT_PATH`. ``"contacts": false`` says the chamber holds no contacts.
The manifest's order is the preference order: the first location is the
default where one must be picked without asking (the migration of old cards,
the dashboard's pre-selection). Creating a contact always names its chamber.

The file
--------
``<chamber>/<contacts>/<slug>-<id8>.nt`` — N-Triples, so this module can read
and rewrite it losslessly without an RDF library on the serving path, and so a
person or an agent can add triples of their own (an address, a birthday, an
organisation): a rewrite replaces only the triples this module would itself
have written for the old state and keeps every other line. The vocabulary is
the ontology defaults (docs/ontology.md):

- the person: ``vcard:Individual`` with ``vcard:fn``, ``vcard:hasEmail
  <mailto:…>``, ``vcard:hasTelephone <tel:…>``, ``vcard:hasInstantMessage``,
  ``dcterms:created``; spheres as ``kb:sphere`` / ``kb:tag`` pointing at
  ``urn:retinue:sphere:<word>``; what the attention model knows about them —
  their importance prior (``kb:importance``, 0–5) and the Focus modes they may
  interrupt (``kb:permit <urn:retinue:mode:<id>>``); and whether they are a
  VIP (``kb:vip true``), whose messages a model works the moment they arrive;
- each messaging-service handle: a ``foaf:OnlineAccount`` linked by
  ``foaf:account``, with ``foaf:accountName``, ``foaf:accountServiceHomepage``
  and ``kb:channel`` (the literal the message ledger carries, so an account
  joins to its messages).

Handles
-------
Code speaks of *handles*: ``(channel, handle)`` pairs, whatever the channel.
``email`` maps to ``vcard:hasEmail``, ``sms`` to ``vcard:hasTelephone`` (an SMS
number is a telephone, not an account), every other channel to an account. An
account whose name is an E.164 number also gives the person that telephone.
An account or an e-mail address belongs to at most one person across all
chambers; telephones may be shared (a family landline).

Library use (the web-gateway's contact card)::

    book = ContactBook()
    person = book.create("private", "Mara Keller", [("signal", "+41791234567")])
    book.find("email", "mara@example.org")

Command line (agents)::

    contacts.py locations
    contacts.py find --email mara@example.org
    contacts.py find --name mara
    contacts.py add --chamber private --name "Mara Keller" --email mara@example.org
    contacts.py update <id> --add-handle whatsapp:+41791234567
    contacts.py update <id> --vip --importance 4 --permit focused

Writes are committed and pushed in the owning chamber (best effort, like the
dashboard's project edits); ``--no-commit`` or ``CONTACTS_COMMIT=0`` skips it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import unicodedata
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = "contacts"

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
VCARD = "http://www.w3.org/2006/vcard/ns#"
FOAF = "http://xmlns.com/foaf/0.1/"
DCTERMS = "http://purl.org/dc/terms/"
KB = "https://w3id.org/retinue/kb#"
XSD_DATETIME = "http://www.w3.org/2001/XMLSchema#dateTime"
XSD_DECIMAL = "http://www.w3.org/2001/XMLSchema#decimal"
XSD_BOOLEAN = "http://www.w3.org/2001/XMLSchema#boolean"

PERSON_PREFIX = "urn:retinue:person:"
ACCOUNT_PREFIX = "urn:retinue:account:"
SPHERE_PREFIX = "urn:retinue:sphere:"
MODE_PREFIX = "urn:retinue:mode:"

# Classes a hand-written file may use for a person; this module writes the first.
PERSON_CLASSES = (VCARD + "Individual", FOAF + "Person", "http://schema.org/Person")

# Channels this module knows more about than their name. Any other lowercase
# word is a valid channel too (an account with no service homepage).
SERVICE_HOMEPAGES = {
    "signal": "https://signal.org/",
    "whatsapp": "https://www.whatsapp.com/",
    "telegram": "https://telegram.org/",
}
EMAIL = "email"
SMS = "sms"
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_PHONE_RE = re.compile(r"^\+[0-9]{6,20}$")
_PHONE_PUNCT_RE = re.compile(r"[\s\-().\u00a0/]")
_EMAIL_RE = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+$")

_HEADER = ("# Contact managed by scripts/contacts.py (docs/contacts.md).\n"
           "# Lines it did not write are kept on every rewrite: add your own freely.\n")


class ContactError(ValueError):
    """A request the contact book refuses; ``status`` is the HTTP answer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── Handles ─────────────────────────────────────────────────────────────────

def normalize_phone(value: str) -> str:
    """A phone number without punctuation, ``00`` read as ``+``. Anything that
    is not then E.164 is returned stripped, unchanged otherwise."""
    raw = str(value or "").strip()
    if raw.lower().startswith("tel:"):
        raw = raw[4:]
    compact = _PHONE_PUNCT_RE.sub("", raw)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    return compact if _PHONE_RE.match(compact) else raw


def normalize_handle(channel: str, handle: str) -> tuple[str, str]:
    channel = str(channel or "").strip().lower()
    if not _CHANNEL_RE.match(channel):
        raise ContactError(f"not a channel: {channel!r}")
    value = str(handle or "").strip()
    if channel == EMAIL:
        if value.lower().startswith("mailto:"):
            value = value[7:]
        value = value.lower()
        if not _EMAIL_RE.match(value):
            raise ContactError(f"not an e-mail address: {handle!r}")
    elif channel == SMS:
        value = normalize_phone(value)
        if not _PHONE_RE.match(value):
            raise ContactError(f"not an E.164 phone number: {handle!r}")
    else:
        maybe = normalize_phone(value)
        if _PHONE_RE.match(maybe):
            value = maybe
    if not value:
        raise ContactError("empty handle")
    return channel, value


def parse_handle(spec: str) -> tuple[str, str]:
    """``channel:handle`` (the CLI's and a chat id's shape) → a normalized pair."""
    channel, sep, handle = str(spec or "").partition(":")
    if not sep:
        raise ContactError(f"expected channel:handle, got {spec!r}")
    return normalize_handle(channel, handle)


def account_iri(channel: str, handle: str) -> str:
    return ACCOUNT_PREFIX + urllib.parse.quote(f"{channel}:{handle}", safe="")


def _im_uri(channel: str, handle: str) -> str | None:
    if channel == "signal" and _PHONE_RE.match(handle):
        return "sgnl://signal.me/#p/" + handle
    if channel == "whatsapp" and _PHONE_RE.match(handle):
        return "https://wa.me/" + handle[1:]
    if channel == "telegram":
        name = handle[1:] if handle.startswith("@") else handle
        if re.match(r"^[A-Za-z][A-Za-z0-9_]{3,31}$", name):
            return "https://t.me/" + name
    return None


# ── N-Triples, just enough ──────────────────────────────────────────────────
# Terms are kept in their N-Triples spelling ("<iri>", "_:b", '"lit"^^<dt>'),
# so a line this module did not write survives a rewrite byte for byte.

_TERM = r'(?:<[^>]*>|_:[A-Za-z0-9_\-.]+)'
_LITERAL = r'"(?:[^"\\]|\\.)*"(?:\^\^<[^>]*>|@[A-Za-z]+(?:-[A-Za-z0-9]+)*)?'
_LINE_RE = re.compile(rf'^\s*({_TERM})\s+(<[^>]*>)\s+({_TERM}|{_LITERAL})\s*\.\s*$')


def _esc(value: str) -> str:
    return (value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


def _unesc(value: str) -> str:
    return re.sub(r'\\(.)', lambda m: {"n": "\n", "r": "\r", "t": "\t"}.get(m.group(1), m.group(1)), value)


def _iri(value: str) -> str:
    return f"<{value}>"


def _lit(value: str, datatype: str | None = None) -> str:
    return f'"{_esc(value)}"' + (f"^^<{datatype}>" if datatype else "")


def _iri_value(term: str) -> str | None:
    return term[1:-1] if term.startswith("<") and term.endswith(">") else None


def _lit_value(term: str) -> str | None:
    match = re.match(r'^"((?:[^"\\]|\\.)*)"', term)
    return _unesc(match.group(1)) if match else None


def _parse_nt(text: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Triples, and every other non-blank line that is not this module's header."""
    triples, other = [], []
    header = set(_HEADER.splitlines())
    for line in text.splitlines():
        if not line.strip() or line in header:
            continue
        match = _LINE_RE.match(line)
        if match:
            triples.append(match.groups())
        else:
            other.append(line)
    return triples, other


def _render_nt(triples, other) -> str:
    lines = sorted({f"{s} {p} {o} ." for s, p, o in triples})
    return _HEADER + "\n".join(list(other) + lines) + "\n"


# ── The record ──────────────────────────────────────────────────────────────

def _person_triples(record: dict) -> set[tuple[str, str, str]]:
    """Everything this module writes for one person — and so everything a
    rewrite may take away again."""
    s = _iri(record["iri"])
    out = {(s, _iri(RDF_TYPE), _iri(VCARD + "Individual"))}
    if record.get("name"):
        out.add((s, _iri(VCARD + "fn"), _lit(record["name"])))
    if record.get("created"):
        out.add((s, _iri(DCTERMS + "created"), _lit(record["created"], XSD_DATETIME)))
    if record.get("sphere"):
        out.add((s, _iri(KB + "sphere"), _iri(SPHERE_PREFIX + urllib.parse.quote(record["sphere"], safe=""))))
    for tag in record.get("tags") or []:
        out.add((s, _iri(KB + "tag"), _iri(SPHERE_PREFIX + urllib.parse.quote(tag, safe=""))))
    if record.get("importance") is not None:
        out.add((s, _iri(KB + "importance"), _lit("%g" % record["importance"], XSD_DECIMAL)))
    for mode in record.get("permits") or []:
        out.add((s, _iri(KB + "permit"), _iri(MODE_PREFIX + urllib.parse.quote(mode, safe=""))))
    if record.get("vip"):
        out.add((s, _iri(KB + "vip"), _lit("true", XSD_BOOLEAN)))
    for address in record.get("emails") or []:
        out.add((s, _iri(VCARD + "hasEmail"), _iri("mailto:" + address)))
    for number in record.get("phones") or []:
        out.add((s, _iri(VCARD + "hasTelephone"), _iri("tel:" + number)))
    for channel, handle in record.get("accounts") or []:
        a = _iri(account_iri(channel, handle))
        out.add((s, _iri(FOAF + "account"), a))
        out.add((a, _iri(RDF_TYPE), _iri(FOAF + "OnlineAccount")))
        out.add((a, _iri(KB + "channel"), _lit(channel)))
        out.add((a, _iri(FOAF + "accountName"), _lit(handle)))
        if channel in SERVICE_HOMEPAGES:
            out.add((a, _iri(FOAF + "accountServiceHomepage"), _iri(SERVICE_HOMEPAGES[channel])))
        if _PHONE_RE.match(handle):
            out.add((s, _iri(VCARD + "hasTelephone"), _iri("tel:" + handle)))
        im = _im_uri(channel, handle)
        if im:
            out.add((s, _iri(VCARD + "hasInstantMessage"), _iri(im)))
    return out


def _records_from(triples, path: Path, chamber: str, rel: str) -> list[dict]:
    by_subject: dict[str, list[tuple[str, str]]] = {}
    for s, p, o in triples:
        by_subject.setdefault(s, []).append((p, o))
    records = []
    for s, props in by_subject.items():
        iri = _iri_value(s)
        types = {_iri_value(o) for p, o in props if p == _iri(RDF_TYPE)}
        if not iri or not types & set(PERSON_CLASSES):
            continue
        record = {"iri": iri, "name": "", "sphere": None, "tags": [], "emails": [],
                  "phones": [], "accounts": [], "created": None, "importance": None,
                  "permits": [], "vip": False}
        tels = []
        for p, o in sorted(props):
            pred = _iri_value(p)
            if pred in (VCARD + "fn", FOAF + "name", "http://schema.org/name") and not record["name"]:
                record["name"] = _lit_value(o) or ""
            elif pred == DCTERMS + "created":
                record["created"] = _lit_value(o)
            elif pred == KB + "sphere" and (_iri_value(o) or "").startswith(SPHERE_PREFIX):
                record["sphere"] = urllib.parse.unquote(_iri_value(o)[len(SPHERE_PREFIX):])
            elif pred == KB + "tag" and (_iri_value(o) or "").startswith(SPHERE_PREFIX):
                record["tags"].append(urllib.parse.unquote(_iri_value(o)[len(SPHERE_PREFIX):]))
            elif pred == KB + "importance":
                try:
                    record["importance"] = _importance(_lit_value(o))
                except ContactError:
                    pass
            elif pred == KB + "permit" and (_iri_value(o) or "").startswith(MODE_PREFIX):
                record["permits"].append(urllib.parse.unquote(_iri_value(o)[len(MODE_PREFIX):]))
            elif pred == KB + "vip":
                record["vip"] = (_lit_value(o) or "").strip().lower() in ("true", "1")
            elif pred == VCARD + "hasEmail" and (_iri_value(o) or "").lower().startswith("mailto:"):
                record["emails"].append(_iri_value(o)[7:].lower())
            elif pred == VCARD + "hasTelephone" and (_iri_value(o) or "").lower().startswith("tel:"):
                tels.append(normalize_phone(_iri_value(o)[4:]))
            elif pred == FOAF + "account" and _iri_value(o):
                acct = dict(by_subject.get(o, []))
                channel = _lit_value(acct.get(_iri(KB + "channel"), "") or "")
                handle = _lit_value(acct.get(_iri(FOAF + "accountName"), "") or "")
                if channel and handle:
                    record["accounts"].append((channel, handle))
        # A telephone an account already implies is not a number of its own.
        implied = {h for _c, h in record["accounts"] if _PHONE_RE.match(h)}
        record["phones"] = sorted({t for t in tels if t not in implied})
        record["all_phones"] = sorted(set(tels) | implied)
        record["emails"] = sorted(set(record["emails"]))
        record["accounts"] = sorted(set(record["accounts"]))
        record["tags"] = sorted(set(record["tags"]))
        record["permits"] = sorted(set(record["permits"]))
        record.update(chamber=chamber, path=rel, file=str(path), key=person_key(iri))
        records.append(record)
    return records


def person_key(iri: str) -> str:
    """The short id the CLI and the dashboard use: the UUID of an IRI this
    module minted, the quoted IRI of any other."""
    if iri.startswith(PERSON_PREFIX):
        return iri[len(PERSON_PREFIX):]
    return urllib.parse.quote(iri, safe="")


def _importance(value) -> float | None:
    """An importance prior as the attention model keeps it: 0–5, or None."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ContactError(f"not an importance: {value!r}")
    if not 0 <= number <= 5:
        raise ContactError(f"importance is 0–5, not {value!r}")
    return number


def _modes(values) -> list[str]:
    out = set()
    for value in values or ():
        mode = str(value or "").strip().lower()
        if not re.match(r"^[a-z0-9][a-z0-9_-]{0,31}$", mode):
            raise ContactError(f"not a mode id: {value!r}")
        out.add(mode)
    return sorted(out)


def handles_of(record: dict) -> list[dict]:
    out = [{"channel": c, "handle": h} for c, h in record.get("accounts") or []]
    out += [{"channel": EMAIL, "handle": e} for e in record.get("emails") or []]
    out += [{"channel": SMS, "handle": p} for p in record.get("phones") or []]
    return out


def public(record: dict) -> dict:
    """The JSON shape of a person, for the CLI and the gateway."""
    return {"id": record["key"], "iri": record["iri"], "name": record["name"],
            "chamber": record["chamber"], "path": record["path"],
            "sphere": record.get("sphere"), "tags": list(record.get("tags") or []),
            "handles": handles_of(record), "phones": list(record.get("all_phones") or []),
            "importance": record.get("importance"), "permits": list(record.get("permits") or []),
            "vip": bool(record.get("vip")), "created": record.get("created")}


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:48].strip("-") or "contact"


# ── The book ────────────────────────────────────────────────────────────────

class ContactBook:
    """Every contact location the manifest declares, and the people in them."""

    def __init__(self, chambers_dir: str | Path | None = None, manifest: str | Path | None = None):
        self.chambers_dir = Path(chambers_dir or os.environ.get("CHAMBERS_DIR", "/workspace/chambers"))
        self.manifest = Path(manifest or os.environ.get("CHAMBERS_MANIFEST", "/workspace/chambers.json"))
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[tuple, list[dict]]] = {}

    # Locations

    def _manifest_entries(self) -> list[dict] | None:
        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        entries = data.get("chambers") if isinstance(data, dict) else None
        return [e for e in entries if isinstance(e, dict) and e.get("name")] if isinstance(entries, list) else None

    def locations(self) -> list[dict]:
        """``[{chamber, path, dir}]`` in preference order, mounted chambers only.

        Without a readable manifest every mounted chamber (not ``_generated``,
        nothing hidden) is a location at the default path, in name order."""
        entries = self._manifest_entries()
        if entries is None:
            entries = [{"name": p.name} for p in sorted(self.chambers_dir.glob("*"))
                       if p.is_dir() and not p.name.startswith(("_", "."))]
        out = []
        for entry in entries:
            name = str(entry["name"])
            declared = entry.get("contacts", DEFAULT_PATH)
            if declared is False or declared is None:
                continue
            rel = str(declared).strip().strip("/") or DEFAULT_PATH
            root = self.chambers_dir / name
            target = (root / rel).resolve()
            if not root.is_dir() or (target != root.resolve() and root.resolve() not in target.parents):
                continue
            out.append({"chamber": name, "path": rel, "dir": root / rel})
        return out

    def location(self, chamber: str) -> dict:
        for loc in self.locations():
            if loc["chamber"] == chamber:
                return loc
        raise ContactError(f"chamber {chamber!r} holds no contacts (see its contacts entry in chambers.json)")

    def default_chamber(self) -> str | None:
        locs = self.locations()
        return locs[0]["chamber"] if locs else None

    # Reading

    def _read_file(self, path: Path, chamber: str) -> list[dict]:
        try:
            st = path.stat()
        except OSError:
            return []
        stamp = (st.st_mtime_ns, st.st_size)
        cached = self._cache.get(str(path))
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            triples, _other = _parse_nt(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return []
        rel = str(path.relative_to(self.chambers_dir))
        records = _records_from(triples, path, chamber, rel)
        self._cache[str(path)] = (stamp, records)
        return records

    def all(self) -> list[dict]:
        with self._lock:
            out = []
            for loc in self.locations():
                for path in sorted(loc["dir"].glob("*.nt")):
                    out.extend(self._read_file(path, loc["chamber"]))
            return out

    def get(self, key: str) -> dict | None:
        key = str(key or "")
        for record in self.all():
            if key in (record["key"], record["iri"]):
                return record
        return None

    def find(self, channel: str, handle: str) -> dict | None:
        """The person this handle belongs to, if anyone's."""
        channel, handle = normalize_handle(channel, handle)
        for record in self.all():
            if channel == EMAIL and handle in record["emails"]:
                return record
            if channel == SMS and handle in record["all_phones"]:
                return record
            if (channel, handle) in record["accounts"]:
                return record
        return None

    def suggest(self, channel: str, handle: str) -> list[dict]:
        """People this handle may belong to: its owner first, then anyone with
        the same phone number on another channel."""
        try:
            channel, handle = normalize_handle(channel, handle)
        except ContactError:
            return []
        exact = self.find(channel, handle)
        out = [dict(exact, exact=True)] if exact else []
        if _PHONE_RE.match(handle):
            for record in self.all():
                if (not exact or record["iri"] != exact["iri"]) and handle in record["all_phones"]:
                    out.append(dict(record, exact=False))
        return out

    def search(self, text: str, limit: int = 20) -> list[dict]:
        needle = " ".join(str(text or "").split()).casefold()
        if not needle:
            return []
        hits = [r for r in self.all()
                if needle in r["name"].casefold() or any(needle in e for e in r["emails"])]
        return sorted(hits, key=lambda r: (not r["name"].casefold().startswith(needle), r["name"]))[:limit]

    # Writing

    def _claimed(self, channel: str, handle: str, but: str | None = None) -> dict | None:
        if channel == SMS:
            return None  # telephones may be shared
        owner = self.find(channel, handle)
        return owner if owner and owner["iri"] != but else None

    def _write(self, path: Path, old: dict | None, new: dict | None) -> None:
        try:
            triples, other = _parse_nt(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            triples, other = [], []
        keep = set(triples) - (_person_triples(old) if old else set())
        if new:
            keep |= _person_triples(new)
        if not keep and not other:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(_render_nt(keep, other), encoding="utf-8")
            os.replace(tmp, path)
        self._cache.pop(str(path), None)

    def create(self, chamber: str, name: str, handles=(), *, sphere: str | None = None,
               tags=(), created: str | None = None, importance=None, permits=(),
               vip: bool = False) -> dict:
        name = " ".join(str(name or "").split())
        if not name:
            raise ContactError("a contact needs a name")
        if not str(chamber or "").strip():
            raise ContactError("a contact needs a chamber")
        with self._lock:
            loc = self.location(str(chamber).strip())
            pairs = [normalize_handle(c, h) for c, h in handles]
            for channel, handle in pairs:
                owner = self._claimed(channel, handle)
                if owner:
                    raise ContactError(f"{channel}:{handle} already belongs to {owner['name']} ({owner['key']})", 409)
            uid = str(uuid.uuid4())
            record = _blank(PERSON_PREFIX + uid, name, sphere, tags,
                            created or datetime.now(timezone.utc).replace(microsecond=0).isoformat())
            record.update(importance=_importance(importance), permits=_modes(permits), vip=bool(vip))
            for pair in pairs:
                _add_pair(record, pair)
            path = loc["dir"] / f"{_slug(name)}-{uid[:8]}.nt"
            self._write(path, None, record)
            return self.get(uid) or record

    def update(self, key: str, *, name: str | None = None, sphere: str | None | bool = False,
               tags=None, add=(), remove=(), importance=False, permits=None,
               vip: bool | None = None) -> dict:
        """Change a person. ``sphere=False`` / ``importance=False`` leave the
        field; None clears it. ``tags`` / ``permits`` None leave them."""
        with self._lock:
            old = self.get(key)
            if old is None:
                raise ContactError(f"no contact {key!r}", 404)
            new = _copy(old)
            if name is not None:
                clean = " ".join(str(name).split())
                if not clean:
                    raise ContactError("a contact needs a name")
                new["name"] = clean
            if sphere is not False:
                new["sphere"] = (str(sphere).strip().lower() or None) if sphere else None
            if tags is not None:
                new["tags"] = sorted({str(t).strip().lower() for t in tags if str(t).strip()} - {new["sphere"]})
            if importance is not False:
                new["importance"] = _importance(importance)
            if permits is not None:
                new["permits"] = _modes(permits)
            if vip is not None:
                new["vip"] = bool(vip)
            for channel, handle in (normalize_handle(c, h) for c, h in remove):
                _remove_pair(new, (channel, handle))
            for channel, handle in (normalize_handle(c, h) for c, h in add):
                owner = self._claimed(channel, handle, but=old["iri"])
                if owner:
                    raise ContactError(f"{channel}:{handle} already belongs to {owner['name']} ({owner['key']})", 409)
                _add_pair(new, (channel, handle))
            self._write(Path(old["file"]), old, new)
            return self.get(old["key"]) or new

    def delete(self, key: str) -> dict:
        with self._lock:
            old = self.get(key)
            if old is None:
                raise ContactError(f"no contact {key!r}", 404)
            self._write(Path(old["file"]), old, None)
            return old


def _blank(iri, name, sphere, tags, created) -> dict:
    sphere = (str(sphere).strip().lower() or None) if sphere else None
    return {"iri": iri, "name": name, "sphere": sphere,
            "tags": sorted({str(t).strip().lower() for t in tags if str(t).strip()} - {sphere}),
            "emails": [], "phones": [], "accounts": [], "created": created,
            "importance": None, "permits": [], "vip": False}


def _copy(record: dict) -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in record.items()}


def _add_pair(record: dict, pair: tuple[str, str]) -> None:
    channel, handle = pair
    if channel == EMAIL:
        record["emails"] = sorted(set(record["emails"]) | {handle})
    elif channel == SMS:
        record["phones"] = sorted(set(record["phones"]) | {handle})
    else:
        record["accounts"] = sorted(set(record["accounts"]) | {pair})


def _remove_pair(record: dict, pair: tuple[str, str]) -> None:
    channel, handle = pair
    if channel == EMAIL:
        record["emails"] = [e for e in record["emails"] if e != handle]
    elif channel == SMS:
        record["phones"] = [p for p in record["phones"] if p != handle]
    else:
        record["accounts"] = [a for a in record["accounts"] if a != pair]


# ── The delivery gate ───────────────────────────────────────────────────────

def policy_projection(book: ContactBook) -> tuple[dict[str, set[str]], set[str]]:
    """What the address book's VIP persons mean to the triage delivery gate:
    the handles of each on every messenger channel (their telephones count
    for SMS), and their e-mail addresses (whitelisted: worked on the frequent
    run). docs/triage-delivery-gate.md."""
    handles: dict[str, set[str]] = {}
    emails: set[str] = set()
    for record in book.all():
        if not record.get("vip"):
            continue
        for channel, handle in record.get("accounts") or []:
            handles.setdefault(channel, set()).add(handle)
        for number in record.get("all_phones") or []:
            handles.setdefault(SMS, set()).add(number)
        emails |= set(record.get("emails") or [])
    return handles, emails


def sync_policy(book: ContactBook) -> list[str]:
    """Write the projection into the gate's policy files (triage_policy's
    contact-owned members). Idempotent and write-if-changed; returns the
    files it wrote."""
    import triage_policy
    return triage_policy.sync_contacts(*policy_projection(book))


# ── Committing ──────────────────────────────────────────────────────────────

def commit(chambers_dir: str | Path, rel_path: str, message: str) -> bool:
    """Best-effort ``git add`` + commit + push of one contact file in its
    chamber (Tier 1: operational data, user-initiated). The in-container git
    is the serializing wrapper, so concurrent commits in a chamber do not race.
    A failure is logged, never raised: the file on disk is already the truth."""
    if os.environ.get("CONTACTS_COMMIT", "1").strip().lower() in ("0", "false", "no"):
        return False
    chamber_name, _sep, inner = str(rel_path).partition("/")
    chamber = Path(chambers_dir) / chamber_name
    try:
        subprocess.run(["git", "-C", str(chamber), "add", "-A", "--", inner],
                       check=True, capture_output=True, timeout=60)
        if subprocess.run(["git", "-C", str(chamber), "diff", "--cached", "--quiet", "--", inner],
                          capture_output=True, timeout=60).returncode == 0:
            return False
        subprocess.run(["git", "-C", str(chamber), "commit", "-m", message, "--", inner],
                       check=True, capture_output=True, timeout=60)
        subprocess.run(["git", "-C", str(chamber), "push"], check=True, capture_output=True, timeout=120)
        return True
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        print(f"[contacts] commit failed for {rel_path}: {exc}", file=sys.stderr, flush=True)
        return False


# ── CLI ─────────────────────────────────────────────────────────────────────

def _print(args, value) -> None:
    if args.json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    rows = value if isinstance(value, list) else [value]
    for row in rows:
        if "handles" not in row:
            print(f"{row['chamber']}\t{row['path']}")
            continue
        handles = ", ".join(f"{h['channel']}:{h['handle']}" for h in row["handles"]) or "—"
        spheres = " · ".join(filter(None, [row.get("sphere")] + row.get("tags", [])
                                    + (["VIP"] if row.get("vip") else [])))
        note = " [same phone number, other channel]" if row.get("match") == "phone" else ""
        print(f"[{row['id']}] {row['name']} ({row['chamber']}){' — ' + spheres if spheres else ''}{note}\n    {handles}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Channel-independent contacts, stored in chambers.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--chambers-dir")
    ap.add_argument("--manifest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("locations", help="where each chamber keeps its contacts")
    p_list = sub.add_parser("list")
    p_list.add_argument("--chamber")
    p_find = sub.add_parser("find", help="by handle, e-mail, phone or name")
    g = p_find.add_mutually_exclusive_group(required=True)
    g.add_argument("--email")
    g.add_argument("--phone")
    g.add_argument("--handle", help="channel:handle, e.g. signal:+41791234567")
    g.add_argument("--name")
    p_show = sub.add_parser("show")
    p_show.add_argument("id")
    for p in (p_add := sub.add_parser("add"), p_upd := sub.add_parser("update")):
        p.add_argument("--name")
        p.add_argument("--sphere")
        p.add_argument("--tag", action="append", help="a further sphere (repeatable; replaces on update)")
        p.add_argument("--importance", help="the attention model's prior for them, 0–5 ('' clears)")
        p.add_argument("--permit", action="append",
                       help="a Focus mode id they may interrupt (repeatable; replaces on update)")
        p.add_argument("--vip", action=argparse.BooleanOptionalAction, default=None,
                       help="a model works their messages the moment they arrive, on every channel")
        p.add_argument("--no-commit", action="store_true")
    p_add.add_argument("--chamber", required=True, help="the chamber the contact is stored in")
    p_add.add_argument("--email", action="append", default=[])
    p_add.add_argument("--phone", action="append", default=[], help="a telephone (also reachable by SMS)")
    p_add.add_argument("--handle", action="append", default=[], help="channel:handle (repeatable)")
    p_upd.add_argument("id")
    p_upd.add_argument("--add-handle", action="append", default=[], help="channel:handle; email:… and sms:… too")
    p_upd.add_argument("--remove-handle", action="append", default=[])
    p_upd.add_argument("--add-email", action="append", default=[])
    p_upd.add_argument("--remove-email", action="append", default=[])
    p_upd.add_argument("--add-phone", action="append", default=[])
    p_upd.add_argument("--remove-phone", action="append", default=[])
    p_upd.add_argument("--no-permits", action="store_true", help="clear every Focus-mode permit")
    p_del = sub.add_parser("delete")
    p_del.add_argument("id")
    p_del.add_argument("--no-commit", action="store_true")
    args = ap.parse_args(argv)
    book = ContactBook(args.chambers_dir, args.manifest)
    try:
        if args.cmd == "locations":
            _print(args, [{"chamber": l["chamber"], "path": l["path"]} for l in book.locations()])
        elif args.cmd == "list":
            _print(args, [public(r) for r in book.all() if not args.chamber or r["chamber"] == args.chamber])
        elif args.cmd == "find":
            if args.name:
                hits = book.search(args.name)
            else:
                pair = (("email", args.email) if args.email else ("sms", args.phone) if args.phone
                        else parse_handle(args.handle))
                # The owner first; then, for a phone number, whoever has it on
                # another channel — marked, since that is a guess to confirm.
                hits = book.suggest(*pair)
            if not hits:
                print("no match", file=sys.stderr)
                return 1
            _print(args, [dict(public(r), match="exact" if r.get("exact", True) else "phone") for r in hits])
        elif args.cmd == "show":
            record = book.get(args.id)
            if record is None:
                raise ContactError(f"no contact {args.id!r}", 404)
            _print(args, public(record))
        elif args.cmd == "add":
            if not args.name:
                raise ContactError("--name is required")
            pairs = ([("email", e) for e in args.email] + [("sms", p) for p in args.phone]
                     + [parse_handle(h) for h in args.handle])
            record = book.create(args.chamber, args.name, pairs, sphere=args.sphere, tags=args.tag or (),
                                 importance=args.importance or None, permits=args.permit or (),
                                 vip=bool(args.vip))
            sync_policy(book)
            if not args.no_commit:
                commit(book.chambers_dir, record["path"], f"chore(contacts): add {record['name']}")
            _print(args, public(record))
        elif args.cmd == "update":
            add = ([parse_handle(h) for h in args.add_handle] + [("email", e) for e in args.add_email]
                   + [("sms", p) for p in args.add_phone])
            remove = ([parse_handle(h) for h in args.remove_handle] + [("email", e) for e in args.remove_email]
                      + [("sms", p) for p in args.remove_phone])
            record = book.update(args.id, name=args.name,
                                 sphere=args.sphere if args.sphere is not None else False,
                                 tags=args.tag, add=add, remove=remove,
                                 importance=False if args.importance is None else (args.importance or None),
                                 permits=[] if args.no_permits else args.permit, vip=args.vip)
            sync_policy(book)
            if not args.no_commit:
                commit(book.chambers_dir, record["path"], f"chore(contacts): update {record['name']}")
            _print(args, public(record))
        elif args.cmd == "delete":
            record = book.delete(args.id)
            sync_policy(book)
            if not args.no_commit:
                commit(book.chambers_dir, record["path"], f"chore(contacts): remove {record['name']}")
            _print(args, public(record))
    except ContactError as exc:
        print(f"contacts: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
