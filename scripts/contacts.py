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
chamber file. A chamber is a directory mounted under ``CHAMBERS_DIR`` (not the
framework's own ``_generated/`` output); its entry in the deployment's
``chambers.json``, if it has one, may declare where its contacts go::

    {"name": "private", "url": "…", "contacts": "people"}

``contacts`` is a directory relative to the chamber root; where nothing is
declared, it is :data:`DEFAULT_PATH` — holding contacts is the default.
``"contacts": false`` opts a chamber out. The order of
the locations is the manifest's order, then any chamber it does not list, by
name. Creating a contact always names its chamber; where one is pre-selected
(the dashboard) or must be picked without asking (the migration of old
cards), it is :meth:`ContactBook.default_chamber` — the chamber the last
contact was created in, else the first location.

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
  interrupt (``kb:permit <urn:retinue:mode:<id>>``); whether they are a
  VIP (``kb:vip true``), whose messages a model works the moment they arrive;
  the user's standing **instruction** for them (``kb:instruction``, free
  text), the brief a model works each of their messages by; and the e-mail
  **domains** they write from (``kb:emailDomain``, see *Domains* below);
- each messaging-service handle: a ``foaf:OnlineAccount`` linked by
  ``foaf:account``, with ``foaf:accountName``, ``foaf:accountServiceHomepage``
  and ``kb:channel`` (the literal the message ledger carries, so an account
  joins to its messages).

Handles
-------
Code speaks of *handles*: ``(channel, handle)`` pairs, whatever the channel.
``email`` maps to ``vcard:hasEmail``, ``sms`` to ``vcard:hasTelephone`` (an SMS
number is a telephone, not an account), every other channel to an account. An
SMS sender that is no number — an alphanumeric sender id ("HBL", "DHL") or a
short code — is an account on the ``sms`` channel: it reaches nobody, it only
names who wrote. An account whose name is an E.164 number also gives the
person that telephone. An account or an e-mail address belongs to at most one
person across all chambers; telephones may be shared (a family landline).

Domains
-------
An organisation, a shop that sends receipts, a team behind many mailboxes
writes from a *domain*, not from one address. A person can carry such domains
(record key ``domains``): ``example.org`` covers every address
``@example.org``; ``*.example.org`` covers every address at ``example.org``
or at any domain below it — the reading the delivery gate gives the same
wildcard (``triage_policy._domain_matches_wildcard``). A domain is no handle:
nothing can be sent to it, and it is not a ``(channel, handle)`` pair. It is a
fallback for *who wrote this mail*: a lookup of an e-mail address answers the
person who holds that exact address, else the person with the most specific
domain covering it (``example.org`` before ``*.example.org``, a longer domain
before a shorter one), and marks such a hit ``match: "domain"``. Like an
address, one spelling of a domain belongs to at most one person.

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
    contacts.py update <id> --add-domain example.org --add-domain '*.example.org'
    contacts.py update <id> --vip --importance 4 --permit focused
    contacts.py update <id> <id> --instruction "Draft a reply; ring if it is about today."

Writes are committed and pushed in the owning chamber (best effort, like the
dashboard's project edits).
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import letterbox  # noqa: E402

DEFAULT_PATH = "contacts"
# The framework's own derived output under CHAMBERS_DIR — in no chamber's
# repository, so no place for a person.
GENERATED = "_generated"

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
TELEGRAM = "telegram"
_TG_ID_RE = re.compile(r"^-?[0-9]+$")
_TG_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_PHONE_RE = re.compile(r"^\+[0-9]{6,20}$")
_WA_PN_SUFFIX = "@s.whatsapp.net"
_PHONE_PUNCT_RE = re.compile(r"[\s\-().\u00a0/]")
_EMAIL_RE = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+$")
# An SMS sender that is no phone number, in the shape sms-gateway.py's
# _normalize_sender delivers it: an alphanumeric sender id of at most 16
# characters with a letter in it ("HBL", "Swisscom"), or a short code. A
# longer run of digits is a national number, refused rather than guessed.
_SMS_SENDER_ID_RE = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9 ._&-]{1,16}$")
_SMS_SHORT_CODE_RE = re.compile(r"^[0-9]{3,6}$")
# Second-level labels under which country registries hand out names to anyone
# ("co.uk", "com.au", "or.jp"). A domain of that shape is a public suffix, not
# one sender's domain: filed on a VIP it would make a VIP of everyone there.
# The public suffix list itself is not at hand (this module is stdlib-only),
# so the rule is the shape — which also refuses the rare real domain of that
# shape; its addresses are then filed one by one.
_PUBLIC_SECOND_LEVELS = frozenset({
    "ac", "co", "com", "edu", "go", "gob", "gov", "gv", "mil", "ne", "net", "nom", "or", "org"})
_DOMAIN_MAX = 253

_HEADER = ("# Contact managed by scripts/contacts.py (docs/contacts.md).\n"
           "# Lines it did not write are kept on every rewrite: add your own freely.\n")

# An instruction is a brief, not a document: it rides in every prompt a
# message of this person's buys, so it is kept to what a brief needs.
INSTRUCTION_MAX = 2000


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
        if _is_wildcard_address(value):
            # "*@example.org" is the gate's spelling of a domain; filed as an
            # address it would match nothing but itself.
            raise ContactError(f"not an e-mail address: {handle!r} (a whole domain is filed as a domain)")
    elif channel == SMS:
        value = normalize_phone(value)
        if not _PHONE_RE.match(value):
            value = " ".join(value.split())
            if not is_sms_sender_id(value):
                raise ContactError(f"not an E.164 phone number, nor an SMS sender id: {handle!r}")
    elif channel == TELEGRAM and not _TG_ID_RE.match(value) and not _PHONE_RE.match(normalize_phone(value)):
        # A username is case-insensitive and written with or without its @:
        # one spelling, so the same person is not filed twice.
        name = value[1:] if value.startswith("@") else value
        if not _TG_USERNAME_RE.match(name):
            raise ContactError(f"not a Telegram username, user id or phone number: {handle!r}")
        value = "@" + name.lower()
    else:
        if channel == "whatsapp" and value.lower().endswith(_WA_PN_SUFFIX):
            # A phone-number JID (a chat key) is the number itself; a LID JID
            # (``…@lid``) is no number and stays as it is.
            value = "+" + value[:-len(_WA_PN_SUFFIX)].lstrip("+")
        maybe = normalize_phone(value)
        if _PHONE_RE.match(maybe):
            value = maybe
    if not value:
        raise ContactError("empty handle")
    return channel, value


def _is_wildcard_address(address: str) -> bool:
    """Whether an "address" is a wildcard over a domain rather than a mailbox:
    ``*`` for the whole local part, or anywhere in the domain. (Inside a
    local part ``*`` is an ordinary character, rare as it is.)"""
    local, _sep, domain = address.rpartition("@")
    return local == "*" or "*" in domain


def is_sms_sender_id(handle: str) -> bool:
    """Whether an SMS handle names a sender that is no phone number — an
    alphanumeric sender id or a short code, which cannot be written back to."""
    return bool(_SMS_SENDER_ID_RE.match(handle) or _SMS_SHORT_CODE_RE.match(handle))


def _is_telephone(channel: str, handle: str) -> bool:
    """Whether a pair is stored as a telephone rather than as an account."""
    return channel == SMS and not is_sms_sender_id(handle)


def parse_handle(spec: str) -> tuple[str, str]:
    """``channel:handle`` (the CLI's and a chat id's shape) → a normalized pair."""
    channel, sep, handle = str(spec or "").partition(":")
    if not sep:
        raise ContactError(f"expected channel:handle, got {spec!r}")
    return normalize_handle(channel, handle)


def normalize_domain(value: str) -> str:
    """A domain a person writes e-mail from, as it is filed: ``example.org``
    (every address ``@example.org``) or ``*.example.org`` (every address at
    ``example.org`` or below it), lowercased. The gate's spelling of the same
    two shapes (``*@example.org``, ``*@*.example.org``) and a leading ``@``
    are read as what they mean. Refused: a wildcard anywhere but as that one
    leading label, an address, a single label (``com``) and the shape of a
    public suffix (``co.uk``) — a domain says "everyone there", so it has to
    be somebody's."""
    spec = str(value or "").strip().lower()
    if spec.startswith("*@"):
        spec = spec[2:]
    elif spec.startswith("@"):
        spec = spec[1:]
    below = spec.startswith("*.")
    base = spec[2:] if below else spec
    labels = base.split(".")
    if "@" in base:
        raise ContactError(f"not a domain but an address: {value!r}")
    if len(base) > _DOMAIN_MAX or not all(_is_domain_label(label) for label in labels):
        raise ContactError(f"not an e-mail domain: {value!r} (expected example.org or *.example.org)")
    if len(labels) < 2 or (len(labels) == 2 and len(labels[1]) == 2 and labels[0] in _PUBLIC_SECOND_LEVELS):
        raise ContactError(f"{base!r} is a public suffix, not one sender's domain")
    if labels[-1].isdigit():
        raise ContactError(f"not an e-mail domain: {value!r} (an IP address is no domain)")
    return ("*." if below else "") + base


def _is_domain_label(label: str) -> bool:
    # Letters and digits of any script — a domain is kept as it was written,
    # like an address — and hyphens inside only.
    return (0 < len(label) <= 63 and label[0] != "-" and label[-1] != "-"
            and all(c.isalnum() or c == "-" for c in label))


def domain_wildcard(spec: str) -> str:
    """A filed domain in the delivery gate's spelling: ``*@example.org``,
    ``*@*.example.org`` (triage_policy's wildcard forms)."""
    return "*@" + spec


def _domain_rank(spec: str, domain: str) -> tuple[int, int] | None:
    """How specifically a filed domain covers an address's domain — None when
    it does not. The domain itself outranks any wildcard, and among wildcards
    the longer one wins; a wildcard also covers its own apex, as the gate
    reads it (triage_policy._domain_matches_wildcard)."""
    if not spec.startswith("*."):
        return (1, len(spec)) if domain == spec else None
    base = spec[2:]
    return (0, len(base)) if domain == base or domain.endswith("." + base) else None


def _canonical(channel: str, handle: str) -> tuple[str, str]:
    """A filed handle in today's spelling — the form a lookup compares. Filed
    handles predate some normalization rules (a Telegram username kept as it
    was typed, ``@Mara_K``); one that no longer normalizes is compared as filed."""
    try:
        return normalize_handle(channel, handle)
    except ContactError:
        return channel, handle


def _match_key(channel: str, handle: str) -> tuple[str, str]:
    """What two account handles are compared by: the canonical spelling, and an
    SMS sender id case-folded, as the delivery gate matches it ("HBL", "hbl")."""
    channel, handle = _canonical(channel, handle)
    if channel == SMS and is_sms_sender_id(handle):
        return channel, handle.casefold()
    return channel, handle


def account_iri(channel: str, handle: str) -> str:
    return ACCOUNT_PREFIX + urllib.parse.quote(f"{channel}:{handle}", safe="")


def _im_uri(channel: str, handle: str) -> str | None:
    if channel == "signal" and _PHONE_RE.match(handle):
        return "sgnl://signal.me/#p/" + handle
    if channel == "whatsapp" and _PHONE_RE.match(handle):
        return "https://wa.me/" + handle[1:]
    if channel == TELEGRAM:
        name = handle[1:] if handle.startswith("@") else handle
        if _TG_USERNAME_RE.match(name):
            return "https://t.me/" + name
    return None


def _telegram_lookup(query: str) -> dict | None:
    """Ask the Telegram gateway who a username or phone is (GET /resolve)."""
    import urllib.error
    import urllib.request
    base = os.environ.get("TELEGRAM_GATEWAY_BASE_URL", "http://telegram-gateway:8093").rstrip("/")
    token = os.environ.get("TELEGRAM_GATEWAY_TOKEN", "").strip()
    request = urllib.request.Request(base + "/resolve?" + urllib.parse.urlencode({"q": query}),
                                     headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ContactError(f"the Telegram gateway could not resolve {query}: HTTP {exc.code}", 502)
    except (OSError, ValueError) as exc:
        raise ContactError(f"the Telegram gateway could not resolve {query}: {exc}", 502)


def with_telegram_ids(pairs, lookup=_telegram_lookup) -> list[tuple[str, str]]:
    """``pairs``, plus the numeric user id of every Telegram username or phone
    among them. Telegram delivers a message under the sender's id only, so a
    person filed by name or number alone would never match one: the delivery
    gate, the chat card and the instruction all compare the id. The name stays
    too — it is what a person recognizes, and it gives the t.me link. A name
    Telegram does not know is refused rather than filed unreachable."""
    out = list(pairs)
    for channel, handle in pairs:
        if channel != TELEGRAM or _TG_ID_RE.match(handle):
            continue
        user = lookup(handle)
        if not user or user.get("id") is None:
            # Telegram finds a number only among the account's own contacts.
            where = " among the account's Telegram contacts" if _PHONE_RE.match(handle) else ""
            raise ContactError(f"Telegram knows no user {handle}{where}", 404)
        pair = (TELEGRAM, str(user["id"]))
        if pair not in out:
            out.append(pair)
    return out


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
    if record.get("instruction"):
        out.add((s, _iri(KB + "instruction"), _lit(record["instruction"])))
    for address in record.get("emails") or []:
        out.add((s, _iri(VCARD + "hasEmail"), _iri("mailto:" + address)))
    for spec in record.get("domains") or []:
        out.add((s, _iri(KB + "emailDomain"), _lit(spec)))
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


_VIP_SPELLINGS = {"true": True, "1": True, "false": False, "0": False}


def _vip_value(term: str) -> bool | None:
    """What a ``kb:vip`` literal says, or None for a spelling this module
    does not read (it then says nothing)."""
    return _VIP_SPELLINGS.get((_lit_value(term) or "").strip().lower())


def _instruction_texts(terms) -> list[str]:
    """The texts of a person's ``kb:instruction`` literals that say anything,
    each once, in a fixed order (the literals' own, so that a rewrite that
    re-sorts the file reads them the same)."""
    texts = [(_lit_value(term) or "").replace("\r\n", "\n").strip() for term in sorted(terms)]
    return list(dict.fromkeys(t for t in texts if t))


def _read_spellings(record: dict, triples, new: dict | None = None) -> set[tuple[str, str, str]]:
    """The person's triples a read TOOK into ``record`` from a spelling other
    than the one _person_triples renders — a domain written by hand as
    ``*@acme.org`` or ``ACME.org``, ``mailto:Mara@…``, ``tel:+41 79 …``, a VIP
    flag as ``"1"``, an importance as ``"4.50"``, every instruction literal
    the record's instruction was joined from (which only a rewrite that
    changes the instruction replaces: _write). A rewrite takes them away with
    the rest; otherwise a value the user removed stays in the file, and the
    next read brings it back. ``new`` is what the rewrite writes (None: the
    person is deleted).

    A literal the read did not take stays exactly as it was written: a
    domain that is none, an importance or VIP flag in a spelling this module
    does not read, an empty instruction — and a second importance that the
    record's (the highest) is not a spelling of, as long as the rewrite
    leaves the importance as it was. A rewrite that changes the importance
    replaces every literal it was read from: the user set the value they
    were shown, and a lower one left behind would come back in its place."""
    s = _iri(record["iri"])
    phones = set(record.get("all_phones") or record.get("phones") or [])
    out = set()
    for triple in triples:
        if triple[0] != s:
            continue
        pred, obj = _iri_value(triple[1]), triple[2]
        if pred == KB + "emailDomain":
            try:
                hit = normalize_domain(_lit_value(obj) or "") in (record.get("domains") or [])
            except ContactError:
                hit = False
        elif pred == VCARD + "hasEmail":
            value = _iri_value(obj) or ""
            hit = value.lower().startswith("mailto:") and value[7:].lower() in (record.get("emails") or [])
        elif pred == VCARD + "hasTelephone":
            value = _iri_value(obj) or ""
            hit = value.lower().startswith("tel:") and normalize_phone(value[4:]) in phones
        elif pred == KB + "vip":
            # Read as "any literal says true": every literal that says what
            # the record holds is a spelling of it. One that says the other
            # value decides nothing (a "false" beside a "true") and stays.
            said = _vip_value(obj)
            hit = said is not None and (new is None or said == bool(record.get("vip")))
        elif pred == KB + "instruction":
            # The record holds every literal that says anything, joined.
            hit = bool(_instruction_texts([obj]))
        elif pred == KB + "importance":
            try:
                value = _importance(_lit_value(obj))
            except ContactError:
                value = None
            hit = value is not None and (
                value == record.get("importance") or new is None
                or new.get("importance") != record.get("importance"))
        else:
            hit = False
        if hit:
            out.add(triple)
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
                  "permits": [], "vip": False, "instruction": None, "domains": []}
        tels = []
        importances = []
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
                    value = _importance(_lit_value(o))
                except ContactError:
                    value = None
                if value is not None:
                    importances.append(value)
            elif pred == KB + "permit" and (_iri_value(o) or "").startswith(MODE_PREFIX):
                record["permits"].append(urllib.parse.unquote(_iri_value(o)[len(MODE_PREFIX):]))
            elif pred == KB + "vip":
                record["vip"] = record["vip"] or bool(_vip_value(o))
            elif pred == VCARD + "hasEmail" and (_iri_value(o) or "").lower().startswith("mailto:"):
                record["emails"].append(_iri_value(o)[7:].lower())
            elif pred == KB + "emailDomain":
                # A value written by hand that is no domain is not matched on:
                # it could cover far more than its author meant.
                try:
                    record["domains"].append(normalize_domain(_lit_value(o) or ""))
                except ContactError:
                    pass
            elif pred == VCARD + "hasTelephone" and (_iri_value(o) or "").lower().startswith("tel:"):
                tels.append(normalize_phone(_iri_value(o)[4:]))
            elif pred == FOAF + "account" and _iri_value(o):
                acct = dict(by_subject.get(o, []))
                channel = _lit_value(acct.get(_iri(KB + "channel"), "") or "")
                handle = _lit_value(acct.get(_iri(FOAF + "accountName"), "") or "")
                if channel and handle:
                    record["accounts"].append((channel, handle))
        # One value from however many literals a hand edit or a merge left:
        # the highest importance, and every instruction, joined — no word of
        # the user's is dropped, and a rewrite that changes it writes one.
        record["importance"] = max(importances) if importances else None
        record["instruction"] = "\n\n".join(_instruction_texts(
            o for p, o in props if _iri_value(p) == KB + "instruction")) or None
        # A telephone an account already implies is not a number of its own.
        implied = {h for _c, h in record["accounts"] if _PHONE_RE.match(h)}
        record["phones"] = sorted({t for t in tels if t not in implied})
        record["all_phones"] = sorted(set(tels) | implied)
        record["emails"] = sorted(set(record["emails"]))
        record["domains"] = sorted(set(record["domains"]))
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


def _instruction(value) -> str | None:
    """A standing instruction as the person keeps it: the user's own words,
    trimmed, or None for none."""
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").strip()
    if len(text) > INSTRUCTION_MAX:
        raise ContactError(f"an instruction is at most {INSTRUCTION_MAX} characters, not {len(text)}")
    return text or None


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
            "domains": list(record.get("domains") or []),
            "importance": record.get("importance"), "permits": list(record.get("permits") or []),
            "vip": bool(record.get("vip")), "instruction": record.get("instruction") or None,
            "created": record.get("created")}


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

    def _manifest_entries(self) -> list[dict]:
        """What chambers.json declares about chambers, in its order. It
        declares; it does not make a chamber — a chamber it does not list
        simply has nothing declared."""
        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = data.get("chambers") if isinstance(data, dict) else None
        return [e for e in entries if isinstance(e, dict) and e.get("name")] if isinstance(entries, list) else []

    def chambers(self) -> list[str]:
        """Every chamber: each directory mounted under CHAMBERS_DIR, except the
        framework's generated output and the letterbox (scripts/letterbox.py),
        which keeps nobody. Manifest order first, then by name."""
        mounted = {p.name for p in letterbox.mounted_chambers(self.chambers_dir)
                   if p.name != GENERATED}
        listed = [str(e["name"]) for e in self._manifest_entries() if str(e["name"]) in mounted]
        return list(dict.fromkeys(listed)) + sorted(mounted - set(listed))

    def locations(self) -> list[dict]:
        """``[{chamber, path, dir}]``, one per chamber in preference order: the
        ``contacts`` directory its manifest entry declares, or
        :data:`DEFAULT_PATH` where nothing is declared; a chamber declaring
        ``"contacts": false`` has none. A declared path must stay inside its
        chamber; one that leads out of it is refused, and the default is used
        instead."""
        entries: dict[str, dict] = {}
        for entry in self._manifest_entries():
            entries.setdefault(str(entry["name"]), entry)
        out = []
        for name in self.chambers():
            entry = entries.get(name, {})
            if entry.get("contacts") is False:
                continue
            root = self.chambers_dir / name
            declared = entry.get("contacts")
            rel = str(declared).strip().strip("/") if isinstance(declared, str) else ""
            target = (root / rel).resolve() if rel else None
            if target is not None and root.resolve() not in target.parents:
                print(f"[contacts] chamber {name!r}: contacts path {declared!r} leaves the chamber; "
                      f"using {DEFAULT_PATH}/", file=sys.stderr, flush=True)
                rel = ""
            rel = rel or DEFAULT_PATH
            out.append({"chamber": name, "path": rel, "dir": root / rel})
        return out

    def location(self, chamber: str) -> dict:
        for loc in self.locations():
            if loc["chamber"] == chamber:
                return loc
        if chamber in self.chambers():
            raise ContactError(f"chamber {chamber!r} keeps no contacts (\"contacts\": false in chambers.json)")
        raise ContactError(f"no chamber {chamber!r} (none is mounted under {self.chambers_dir})")

    def default_chamber(self) -> str | None:
        """Where a new contact goes unless told otherwise: the chamber the
        last contact was created in (``dcterms:created``), so the pick carries
        over from one to the next — on every device, and from the CLI too.
        With nobody created yet, the first location."""
        locs = [loc["chamber"] for loc in self.locations()]
        if not locs:
            return None
        created = [(_when(r.get("created")), r["chamber"]) for r in self.all() if r["chamber"] in locs]
        created = [c for c in created if c[0] is not None]
        return max(created)[1] if created else locs[0]

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

    def find(self, channel: str, handle: str, *, exact: bool = False) -> dict | None:
        """The person this handle belongs to, if anyone's. An e-mail address
        nobody holds falls back to the person with the most specific domain
        covering it; such a hit is a copy marked ``match: "domain"`` (and
        ``match_domain``, the domain it came by), because it says who the
        mail is from, not whose address it is. ``exact=True`` leaves the
        fallback out — the question a writer asks before it files or moves
        the address itself."""
        channel, handle = normalize_handle(channel, handle)
        key = _match_key(channel, handle)  # a sender id compares case-insensitively
        for record in self.all():
            if channel == EMAIL and handle in record["emails"]:
                return record
            if channel == SMS and handle in record["all_phones"]:
                return record
            if any(_match_key(*account) == key for account in record["accounts"]):
                return record
        if channel == EMAIL and not exact:
            return next(iter(self._by_domain(handle)), None)
        return None

    def _by_domain(self, address: str) -> list[dict]:
        """The people whose domains cover a normalized address, the most
        specific first; each a marked copy (see :meth:`find`). Where a file
        edited by hand gives two people the same domain, the first in the
        book's order stands, as for a handle."""
        domain = address.rsplit("@", 1)[1]
        hits = []
        for order, record in enumerate(self.all()):
            ranked = [(rank, spec) for spec in record.get("domains") or []
                      if (rank := _domain_rank(spec, domain)) is not None]
            if ranked:
                rank, spec = max(ranked)
                hits.append((rank, -order, dict(record, match="domain", match_domain=spec)))
        return [hit for _rank, _order, hit in sorted(hits, key=lambda h: h[:2], reverse=True)]

    def email_matches(self, address: str) -> list[dict]:
        """Everyone a mail from this address may be worked as, the most
        specific first: the holder of the address, then the people whose
        domains cover it (:meth:`find`'s order, continued). The delivery gate
        calls a sender VIP or instructed if *any* of them is, so the reader
        of a class walks this list to the first person who carries it."""
        _channel, address = normalize_handle(EMAIL, address)
        holder = self.find(EMAIL, address, exact=True)
        return ([holder] if holder else []) + [
            hit for hit in self._by_domain(address) if not holder or hit["iri"] != holder["iri"]]

    def suggest(self, channel: str, handle: str) -> list[dict]:
        """People this handle may belong to: its owner first, then anyone with
        the same phone number on another channel — or, for an e-mail address
        nobody holds, the person whose domain covers it."""
        try:
            channel, handle = normalize_handle(channel, handle)
        except ContactError:
            return []
        exact = self.find(channel, handle, exact=True)
        out = [dict(exact, exact=True)] if exact else []
        if _PHONE_RE.match(handle):
            for record in self.all():
                if (not exact or record["iri"] != exact["iri"]) and handle in record["all_phones"]:
                    out.append(dict(record, exact=False))
        if channel == EMAIL and not exact:
            out += [dict(record, exact=False) for record in self._by_domain(handle)[:1]]
        return out

    def search(self, text: str, limit: int = 20) -> list[dict]:
        needle = " ".join(str(text or "").split()).casefold()
        if not needle:
            return []
        hits = [r for r in self.all()
                if needle in r["name"].casefold() or any(needle in e for e in r["emails"])
                or any(needle in d for d in r.get("domains") or [])]
        return sorted(hits, key=lambda r: (not r["name"].casefold().startswith(needle), r["name"]))[:limit]

    # Writing

    def _claimed(self, channel: str, handle: str, but: str | None = None) -> dict | None:
        if _is_telephone(channel, handle):
            return None  # telephones may be shared
        # Exact: an address inside somebody's domain is still free to be a
        # person's own — the more specific filing wins every lookup.
        owner = self.find(channel, handle, exact=True)
        return owner if owner and owner["iri"] != but else None

    def _domain_claimed(self, spec: str, but: str | None = None) -> dict | None:
        """Who else files this very domain. ``example.org`` and
        ``*.example.org`` are two filings — the lookup ranks them — so only
        the same spelling is a conflict."""
        for record in self.all():
            if spec in (record.get("domains") or []) and record["iri"] != but:
                return record
        return None

    def _write(self, path: Path, old: dict | None, new: dict | None) -> None:
        try:
            triples, other = _parse_nt(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            triples, other = [], []
        keep = set(triples)
        if old:
            keep -= _person_triples(old) | _read_spellings(old, triples, new)
        if new:
            keep |= _person_triples(new)
        if old and new and old.get("instruction") == new.get("instruction"):
            # A rewrite that leaves the instruction leaves its literals as the
            # user wrote them — two of them stay two, not one joined text.
            s, pred = _iri(old["iri"]), _iri(KB + "instruction")
            keep = {t for t in keep if t[:2] != (s, pred)} | {t for t in triples if t[:2] == (s, pred)}
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
               vip: bool = False, instruction: str | None = None, domains=()) -> dict:
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
            specs = sorted({normalize_domain(d) for d in domains})
            for spec in specs:
                owner = self._domain_claimed(spec)
                if owner:
                    raise ContactError(f"the domain {spec} already belongs to {owner['name']} ({owner['key']})", 409)
            uid = str(uuid.uuid4())
            record = _blank(PERSON_PREFIX + uid, name, sphere, tags,
                            created or datetime.now(timezone.utc).replace(microsecond=0).isoformat())
            record.update(importance=_importance(importance), permits=_modes(permits), vip=bool(vip),
                          instruction=_instruction(instruction), domains=specs)
            for pair in pairs:
                _add_pair(record, pair)
            path = loc["dir"] / f"{_slug(name)}-{uid[:8]}.nt"
            self._write(path, None, record)
            return self.get(uid) or record

    def update(self, key: str, *, name: str | None = None, sphere: str | None | bool = False,
               tags=None, add=(), remove=(), importance=False, permits=None,
               vip: bool | None = None, instruction: str | None | bool = False,
               domains=None, add_domains=(), remove_domains=()) -> dict:
        """Change a person. ``sphere=False`` / ``importance=False`` /
        ``instruction=False`` leave the field; None (or an empty instruction)
        clears it. ``tags`` / ``permits`` / ``domains`` None leave them; a
        list replaces them, and ``add_domains`` / ``remove_domains`` then
        change what stands."""
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
            if instruction is not False:
                new["instruction"] = _instruction(instruction)
            for channel, handle in (normalize_handle(c, h) for c, h in remove):
                _remove_pair(new, (channel, handle))
            for channel, handle in (normalize_handle(c, h) for c, h in add):
                owner = self._claimed(channel, handle, but=old["iri"])
                if owner:
                    raise ContactError(f"{channel}:{handle} already belongs to {owner['name']} ({owner['key']})", 409)
                _add_pair(new, (channel, handle))
            specs = set(new.get("domains") or []) if domains is None else {normalize_domain(d) for d in domains}
            specs -= {normalize_domain(d) for d in remove_domains}
            specs |= {normalize_domain(d) for d in add_domains}
            for spec in sorted(specs - set(old.get("domains") or [])):
                owner = self._domain_claimed(spec, but=old["iri"])
                if owner:
                    raise ContactError(f"the domain {spec} already belongs to {owner['name']} ({owner['key']})", 409)
            new["domains"] = sorted(specs)
            self._write(Path(old["file"]), old, new)
            return self.get(old["key"]) or new

    def delete(self, key: str) -> dict:
        with self._lock:
            old = self.get(key)
            if old is None:
                raise ContactError(f"no contact {key!r}", 404)
            self._write(Path(old["file"]), old, None)
            return old


def _when(value) -> datetime | None:
    """A ``dcterms:created`` value as an aware datetime, or None."""
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _blank(iri, name, sphere, tags, created) -> dict:
    sphere = (str(sphere).strip().lower() or None) if sphere else None
    return {"iri": iri, "name": name, "sphere": sphere,
            "tags": sorted({str(t).strip().lower() for t in tags if str(t).strip()} - {sphere}),
            "emails": [], "phones": [], "accounts": [], "created": created,
            "importance": None, "permits": [], "vip": False, "instruction": None, "domains": []}


def _copy(record: dict) -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in record.items()}


def _add_pair(record: dict, pair: tuple[str, str]) -> None:
    channel, handle = pair
    if channel == EMAIL:
        record["emails"] = sorted(set(record["emails"]) | {handle})
    elif _is_telephone(channel, handle):
        record["phones"] = sorted(set(record["phones"]) | {handle})
    else:
        # The pair is normalized; an older spelling of the same handle gives way.
        key = _match_key(*pair)
        record["accounts"] = sorted({a for a in record["accounts"] if _match_key(*a) != key} | {pair})


def _remove_pair(record: dict, pair: tuple[str, str]) -> None:
    channel, handle = pair
    if channel == EMAIL:
        record["emails"] = [e for e in record["emails"] if e != handle]
    elif _is_telephone(channel, handle):
        record["phones"] = [p for p in record["phones"] if p != handle]
    else:
        key = _match_key(*pair)
        record["accounts"] = [a for a in record["accounts"] if _match_key(*a) != key]


# ── The delivery gate ───────────────────────────────────────────────────────

def _gate_handles(record: dict):
    """Every handle of one person as the delivery gate compares it: their
    accounts, their telephones for SMS, and on the ``email`` channel their
    addresses and their domains — the latter in the gate's wildcard spelling
    (:func:`domain_wildcard`), which only that channel reads as a wildcard."""
    for channel, handle in record.get("accounts") or []:
        # Project the current form, which is the one the gateways deliver.
        yield _canonical(channel, handle)
    for number in record.get("all_phones") or []:
        yield SMS, number
    for address in record.get("emails") or []:
        # A wildcard written as an address (a file edited by hand) names
        # nobody here, and must not turn into a domain at the gate.
        if not _is_wildcard_address(address):
            yield EMAIL, address
    for spec in record.get("domains") or []:
        yield EMAIL, domain_wildcard(spec)


def policy_projection(book: ContactBook) -> dict[str, set[str]]:
    """What the address book's VIP persons mean to the delivery gate:
    ``{channel: handles}`` — the handles of each on every messenger channel
    (their telephones count for SMS), and under ``email`` their addresses and
    domain wildcards. docs/triage-delivery-gate.md, docs/mail-chats.md."""
    handles: dict[str, set[str]] = {}
    for record in book.all():
        if not record.get("vip"):
            continue
        for channel, handle in _gate_handles(record):
            handles.setdefault(channel, set()).add(handle)
    return handles


def instruction_projection(book: ContactBook) -> dict[str, set[str]]:
    """The handles of every person the user wrote an instruction for — the
    gate's ``instructed`` class, e-mail included: each message of theirs buys
    a turn with the instruction as its brief. Only the handle reaches the
    gate; the words stay with the person, where the turn reads them
    (:func:`instructed_person`)."""
    handles: dict[str, set[str]] = {}
    for record in book.all():
        if record.get("instruction"):
            for channel, handle in _gate_handles(record):
                handles.setdefault(channel, set()).add(handle)
    return handles


def instructed_person(book: ContactBook, channel: str, handle: str) -> dict | None:
    """The person whose instruction applies to a message from this handle, or
    None. Matched exactly as the gate matched it — the projected form,
    compared case-insensitively — so a message the gate called instructed
    finds the instruction it was called that for. For a mail that is the most
    specific instructed person: the holder of the address, else whoever's
    domain covers it most closely (a marked copy, as :meth:`ContactBook.find`
    gives one) — an organisation's brief stands for a member the book knows
    by name but gives no brief of their own."""
    try:
        channel, handle = normalize_handle(channel, handle)
    except ContactError:
        return None
    if channel == EMAIL:
        return next((r for r in book.email_matches(handle) if r.get("instruction")), None)
    want = (channel, handle.strip().lower())
    for record in book.all():
        if not record.get("instruction"):
            continue
        if any((c, h.strip().lower()) == want for c, h in _gate_handles(record)):
            return record
    return None


def sync_policy(book: ContactBook) -> list[str]:
    """Write the projection into the gate's policy files (triage_policy's
    contact-owned members). Idempotent and write-if-changed; returns the
    files it wrote."""
    import triage_policy
    return triage_policy.sync_contacts(policy_projection(book),
                                       instructed_handles=instruction_projection(book))


# ── Committing ──────────────────────────────────────────────────────────────

def commit(chambers_dir: str | Path, rel_path: str, message: str) -> bool:
    """Best-effort ``git add`` + commit + push of one contact file in its
    chamber (Tier 1: operational data, user-initiated). The in-container git
    is the serializing wrapper, so concurrent commits in a chamber do not race.
    A failure is logged, never raised: the file on disk is already the truth.
    Only the chamber's own repository is committed to: a chamber directory
    that is no repository of its own is never committed into one around it."""
    chamber_name, _sep, inner = str(rel_path).partition("/")
    chamber = Path(chambers_dir) / chamber_name
    try:
        top = subprocess.run(["git", "-C", str(chamber), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=60)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != chamber.resolve():
            print(f"[contacts] {chamber_name} is no git repository of its own; {inner} not committed",
                  file=sys.stderr, flush=True)
            return False
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
        handles = ", ".join([f"{h['channel']}:{h['handle']}" for h in row["handles"]]
                            + [f"email-domain:{d}" for d in row.get("domains") or []]) or "—"
        spheres = " · ".join(filter(None, [row.get("sphere")] + row.get("tags", [])
                                    + (["VIP"] if row.get("vip") else [])))
        note = {"phone": " [same phone number, other channel]",
                "domain": f" [by the domain {row.get('match_domain')}, not this address]"}.get(row.get("match"), "")
        print(f"[{row['id']}] {row['name']} ({row['chamber']}){' — ' + spheres if spheres else ''}{note}\n    {handles}")
        if row.get("instruction"):
            print("    instruction: " + " ".join(row["instruction"].split()))


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
        p.add_argument("--instruction",
                       help="the user's standing brief for their messages: a model works each one "
                            "by it, and its verdict decides whether the message rings")
        p.add_argument("--instruction-from", metavar="ID",
                       help="copy the instruction another contact carries")
    p_add.add_argument("--chamber", required=True, help="the chamber the contact is stored in")
    p_add.add_argument("--email", action="append", default=[])
    p_add.add_argument("--phone", action="append", default=[], help="a telephone (also reachable by SMS)")
    p_add.add_argument("--handle", action="append", default=[], help="channel:handle (repeatable)")
    for p in (p_add, p_upd):
        p.add_argument("--add-domain", *(["--domain"] if p is p_add else []), action="append", default=[],
                       metavar="DOMAIN",
                       help="an e-mail domain they write from: example.org for every address there, "
                            "'*.example.org' for every address there or below it (repeatable)")
    p_upd.add_argument("--remove-domain", action="append", default=[], metavar="DOMAIN")
    p_upd.add_argument("id", nargs="+",
                       help="the contact; several to give a team the same facts (no name or handle changes then)")
    p_upd.add_argument("--no-instruction", action="store_true", help="remove the instruction")
    p_upd.add_argument("--add-handle", action="append", default=[], help="channel:handle; email:… and sms:… too")
    p_upd.add_argument("--remove-handle", action="append", default=[])
    p_upd.add_argument("--add-email", action="append", default=[])
    p_upd.add_argument("--remove-email", action="append", default=[])
    p_upd.add_argument("--add-phone", action="append", default=[])
    p_upd.add_argument("--remove-phone", action="append", default=[])
    p_upd.add_argument("--no-permits", action="store_true", help="clear every Focus-mode permit")
    p_del = sub.add_parser("delete")
    p_del.add_argument("id")
    sub.add_parser("telegram-ids", help="file the user id next to every Telegram username or phone "
                                        "filed without one (a contact filed before ids were resolved)")
    args = ap.parse_args(argv)
    book = ContactBook(args.chambers_dir, args.manifest)

    def instruction_arg():
        """The instruction the flags ask for: False leaves it, None clears it."""
        given = [f for f, on in (("--instruction", args.instruction is not None),
                                 ("--instruction-from", args.instruction_from is not None),
                                 ("--no-instruction", getattr(args, "no_instruction", False))) if on]
        if len(given) > 1:
            raise ContactError(f"{' and '.join(given)} contradict each other: give one")
        if args.instruction_from is not None:
            source = book.get(args.instruction_from)
            if source is None:
                raise ContactError(f"no contact {args.instruction_from!r}", 404)
            if not source.get("instruction"):
                raise ContactError(f"{source['name']} carries no instruction to copy")
            return source["instruction"]
        if getattr(args, "no_instruction", False):
            return None
        return False if args.instruction is None else args.instruction

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
                # another channel — marked, since that is a guess to confirm —
                # and for an address nobody holds, whoever's domain covers it:
                # marked too, since that says who wrote, not whose address.
                hits = book.suggest(*pair)
            if not hits:
                print("no match", file=sys.stderr)
                return 1
            _print(args, [dict(public(r), match="exact" if r.get("exact", True) else r.get("match", "phone"),
                               **({"match_domain": r["match_domain"]} if r.get("match") == "domain" else {}))
                          for r in hits])
        elif args.cmd == "show":
            record = book.get(args.id)
            if record is None:
                raise ContactError(f"no contact {args.id!r}", 404)
            _print(args, public(record))
        elif args.cmd == "add":
            if not args.name:
                raise ContactError("--name is required")
            pairs = with_telegram_ids([("email", e) for e in args.email] + [("sms", p) for p in args.phone]
                                      + [parse_handle(h) for h in args.handle])
            instruction = instruction_arg()
            record = book.create(args.chamber, args.name, pairs, sphere=args.sphere, tags=args.tag or (),
                                 importance=args.importance or None, permits=args.permit or (),
                                 vip=bool(args.vip), instruction=instruction or None,
                                 domains=args.add_domain)
            sync_policy(book)
            commit(book.chambers_dir, record["path"], f"chore(contacts): add {record['name']}")
            _print(args, public(record))
        elif args.cmd == "update":
            add = with_telegram_ids([parse_handle(h) for h in args.add_handle]
                                    + [("email", e) for e in args.add_email]
                                    + [("sms", p) for p in args.add_phone])
            remove = ([parse_handle(h) for h in args.remove_handle] + [("email", e) for e in args.remove_email]
                      + [("sms", p) for p in args.remove_phone])
            ids = list(dict.fromkeys(args.id))
            # Several people take the same person-level facts — a team that
            # shares one instruction, say. A name, a handle or a domain
            # belongs to one person, so those changes name exactly one.
            if len(ids) > 1 and (args.name is not None or add or remove
                                 or args.add_domain or args.remove_domain):
                raise ContactError("a name, a handle or a domain belongs to one person: "
                                   "update them one at a time")
            instruction = instruction_arg()
            # Every id is looked up before anything is written, so a typo in
            # the third one does not leave the first two changed.
            missing = [i for i in ids if book.get(i) is None]
            if missing:
                raise ContactError(f"no contact {missing[0]!r}", 404)
            records = []
            for key in ids:
                record = book.update(key, name=args.name,
                                     sphere=args.sphere if args.sphere is not None else False,
                                     tags=args.tag, add=add, remove=remove,
                                     importance=False if args.importance is None else (args.importance or None),
                                     permits=[] if args.no_permits else args.permit, vip=args.vip,
                                     instruction=instruction, add_domains=args.add_domain,
                                     remove_domains=args.remove_domain)
                records.append(record)
            sync_policy(book)
            for record in records:
                commit(book.chambers_dir, record["path"], f"chore(contacts): update {record['name']}")
            _print(args, public(records[0]) if len(records) == 1 else [public(r) for r in records])
        elif args.cmd == "telegram-ids":
            # Every lookup comes before any write, so a gateway that fails
            # halfway leaves every contact as it was. What cannot be done — a
            # name Telegram does not know, a handle that is no name at all, an
            # id someone else already holds — is reported; the rest is
            # written, synced and committed.
            plan, problems = [], []
            for record in book.all():
                have = []
                for channel, handle in record["accounts"]:
                    if channel != TELEGRAM:
                        continue
                    try:
                        pair = normalize_handle(channel, handle)
                    except ContactError as exc:  # filed before handles were checked
                        problems.append(f"{record['name']} ({record['key']}): {exc}")
                        continue
                    if pair not in have:
                        have.append(pair)
                try:
                    add = [p for p in with_telegram_ids(have) if p not in have]
                except ContactError as exc:
                    if exc.status != 404:
                        raise
                    problems.append(f"{record['name']} ({record['key']}): {exc}")
                    continue
                if add:
                    plan.append((record, add))
            changed = []
            for record, add in plan:
                try:
                    changed.append(book.update(record["key"], add=add))
                except ContactError as exc:
                    problems.append(f"{record['name']} ({record['key']}): {exc}")
            if changed:
                sync_policy(book)
            for record in changed:
                commit(book.chambers_dir, record["path"], f"chore(contacts): Telegram id for {record['name']}")
            _print(args, [public(r) for r in changed])
            for problem in problems:
                print(f"contacts: {problem}", file=sys.stderr)
            if problems:
                return 2
        elif args.cmd == "delete":
            record = book.delete(args.id)
            sync_policy(book)
            commit(book.chambers_dir, record["path"], f"chore(contacts): remove {record['name']}")
            _print(args, public(record))
    except ContactError as exc:
        print(f"contacts: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
