"""The deterministic mail trim: a mail reduced to its own new text.

A mail chat shows each mail like a message, so the bubble wants what this mail
*adds* — not the thread it drags along, not the sender's signature block, not
the legal notice their server glued on. This module is that reduction, plus
the two HTML services the mail surface needs beside it:

  * :func:`trim` — ``(plain, html) -> TrimResult(text, state, removed)``.
  * :func:`html_to_text` — a readable plain rendering of an HTML part.
  * :func:`sanitize_html` — the HTML part made safe for the *Original* view.
  * :func:`is_tracking_pixel` — the size test the gateway applies to images.

Pure and stdlib-only (``html.parser``, ``re``): no I/O, no network, importable
in a slim gateway image, and never raising on a malformed mail — a parser that
trips yields the untrimmed text, not an exception.

What the trim removes, in this order
====================================

1. **Quoted history** — ``>`` blocks with their attribution line, Outlook's
   "-----Original Message-----" separator, the ``From:/Sent:/To:/Subject:``
   header block, and (when the text comes from HTML) the quote containers the
   clients write.
2. **Signatures** — the ``-- `` delimiter and the mobile "Sent from my …" line.
3. **Footers and disclaimers** — confidentiality notices, unsubscribe blocks
   and mailing-list footers, at the END of the mail only.
4. **Tracking pixels** — counted when the HTML part is the source.

Structure before words
======================

The trim is language-agnostic by mechanism wherever a structural test exists:

  * a quote is a line that starts with ``>``;
  * an attribution is recognised by SHAPE — a line ending in ``:`` that carries
    an address in angle brackets, or a date and a time, and stands directly
    before a quote block — so "On … wrote:" needs no word list in any language;
  * a separator is a line of text flanked by dashes; a header block is a run of
    ``Label: value`` lines;
  * HTML containers are named by the client that wrote them
    (``blockquote[type=cite]``, ``.gmail_quote``, ``#divRplyFwdMsg``, …).

Where words are unavoidable — which label is "From", which separator says
"forwarded" rather than "original", what "Sent from my" is in Danish — they
live in the data tables below, keyed by language, covering English, German,
French, Italian, Spanish, Dutch, Portuguese, Danish, Norwegian, Swedish,
Finnish, Polish and Czech. No language is privileged: every table is folded
(case and diacritics) and consulted as one set. Extending a language is adding
a row.

Rules that outrank recall
=========================

**Never cut what the author wrote.** Every heuristic here is tuned to miss
rather than to over-reach, and the state says so when it missed:

  * *Interleaved replies.* Only the quoted lines go. Text between, above and
    below quote blocks stays, so top-, bottom- and inline-posting all keep the
    author's lines.
  * *A lead-in is not an attribution.* "As you said:" before a quote has no
    address and no date, so it stays; only the quote under it goes. Where no
    ``>`` block follows at all (some clients quote as bare text), a line cuts
    the rest of the mail only if it also carries the verb, ends on the verb
    or the address, and opens its paragraph — a sentence that happens to
    hold a date and "wrote" does not.
  * *A forward's body is the content.* A forward marker ("---------- Forwarded
    message ---------", "Begin forwarded message:", and their kin) ends the
    trimming: the intro above it is trimmed like any text, the forwarded
    message below is kept whole (Apple Mail's ``>`` quoting of it removed). An
    Outlook forward carries no marker of its own and looks exactly like a
    reply; pass ``subject=`` and a ``Fwd:``-style prefix settles it, in which
    case a plain marker line is written above the kept message so the result
    reads — and re-trims — as a forward.
  * *Header blocks need a From, a date and a Subject.* "From: Zurich / To:
    Bern / Date: …" is an itinerary, and "From: … / Subject: …" pasted to
    talk about it is the author's text.
  * *Footers need two cues, and the end.* A paragraph is a disclaimer only
    when it names both confidentiality and a recipient; an unsubscribe line
    only when it carries a link or an address; either only when little or
    nothing follows it. A notice glued under "Regards / Bob" goes without
    them.
  * *An empty result falls back to the untrimmed text.* A bubble is never
    blank because the trim was greedy.

Known hard case: ``>`` that is not a quote
==========================================

A technical mail can carry lines that start with ``>`` and are content: a
shell prompt, a Python session, a classic ``diff``. Nothing in one line tells
them from a quote, so the trim keeps what structure can vouch for and lets the
rest go:

  * lines inside a closed fenced block (three backticks or tildes) are kept;
  * lines indented four or more spaces, or by a tab, are kept (code);
  * ``>>> `` lines are kept when the mail has no ``>`` or ``>>`` line at all
    (an interpreter session — a real third-level quote all but always comes
    with its shallower levels);
  * a ``>`` run directly under a classic-diff hunk (``3c3``, ``---``, ``< x``)
    is kept;
  * ``>=`` at the start of a line is a comparison;
  * the only ``>`` line of a mail, right under text that ends neither a
    sentence nor a lead-in, is that sentence, folded ("the value must be /
    > 100 for this to work");
  * anything else that starts with ``>`` is a quote and goes.

Every kept ``>`` line makes the state ``wanting``, so the model trim gets a
second look. The opposite error — an unfenced ``> ls`` on a paragraph of its
own — is cut, and is the price of removing quotes that have no attribution.

Other limits: an Outlook-style history (no ``>``) is cut to the end, so
comments a sender wrote *into* such a history are lost with it; a quoted line
a client re-wrapped leaves its overflow word behind as if the author wrote it;
a signature without the ``-- `` delimiter is the author's sign-off and stays.

Which part is the source
========================

The plain part, whenever it is the mail's text. The HTML part in three cases:

  * there is no plain part (an **HTML-only** mail);
  * the plain part is a **stub** — a few words ("this mail needs an HTML
    viewer", a bare link) beside an HTML part that says many times as much;
  * the plain part showed **no quoted history, but the HTML part has a
    client's quote container** in it. The markup then knows what the text
    could not say — Outlook's header block in a language no table here
    covers — and the text derived from it is used when it is the shorter.

The HTML is turned into text with its quotes as ``>`` lines and then goes
through the very same pipeline, so there is one set of rules, not two.

State
=====

``deterministic`` unless the trim could not settle, which is ``wanting`` — a
model trim is due on first view: the mail is HTML-only (or its plain part a
stub), or more than ``WANTING_CHARS`` remain (``MAIL_TRIM_WANTING_CHARS``,
default 2500), or a quote marker survived.

Idempotence
===========

``trim(trim(x).text, None).text == trim(x).text``. The pipeline runs to a
fixpoint inside one call, and whatever it keeps is kept in a form the next
call recognises the same way. A text built never to settle gets no trim at
all, which is the same answer however often it is asked for.

Cost
====

Linear in the size of the mail, for hostile mail too: every look-ahead and
look-back is bounded, element nesting is tracked to a fixed depth, and the
stages that repeat work only on the end of the text.
"""
from __future__ import annotations

import html as _html
import os
import re
import unicodedata
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.parse import unquote


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


#: More text than this left after the trim reads as unsettled: the state is
#: ``wanting``. Read at call time, so a caller (or a test) may reassign it.
WANTING_CHARS = _env_int("MAIL_TRIM_WANTING_CHARS", 2500)

#: A ``-- `` delimiter is a signature delimiter only when what follows is
#: short. A longer tail is body text under a stray dash line.
SIGNATURE_MAX_LINES = 12

#: How many closing paragraphs may be footer. Bilingual legal notices are long,
#: so this is generous; a notice further up than this is left alone.
FOOTER_MAX_PARAGRAPHS = 8

#: The marker written above an Outlook-style forward that has none of its own
#: (see the module docstring). English, as every fixed string of the framework.
FORWARD_MARKER = "---------- Forwarded message ----------"


class TrimResult(NamedTuple):
    """What :func:`trim` answers.

    ``text`` is the bubble text. ``state`` is ``"deterministic"`` or
    ``"wanting"``. ``removed`` reports what was cut, for tests and logging:
    characters of quoted history, signature and footer, and the number of
    tracking pixels (``{"quote": n, "signature": n, "footer": n, "pixels": n}``).
    """

    text: str
    state: str
    removed: dict


# ── Word tables ──────────────────────────────────────────────────────────────
#
# Data, not code: one row per language, every row the same shape. Entries are
# written as the clients write them, accents and all; `_fold` reduces both the
# table and the text to one comparable form at import and at match time.

_FOLD_EXTRA = str.maketrans({
    "ø": "o", "æ": "ae", "œ": "oe", "ł": "l", "đ": "d", "ß": "ss",
    "\u2019": "'", "\xa0": " ", "\u202f": " ",
})


def _fold(text: str) -> str:
    """Case- and diacritic-insensitive form: "Envoyé :" → "envoye :"."""
    text = unicodedata.normalize("NFKD", text.casefold().translate(_FOLD_EXTRA))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


# The labels of a quoted mail's header block, by role. A block is recognised
# by the roles it covers (see `_header_block_at`), never by one word.
_HEADER_LABELS = {
    "en": {"from": ("From",), "sent": ("Sent", "Date"), "to": ("To",),
           "cc": ("Cc", "Bcc", "Reply-To"), "subject": ("Subject",)},
    "de": {"from": ("Von",), "sent": ("Gesendet", "Datum"), "to": ("An",),
           "cc": ("Kopie", "Antwort an"), "subject": ("Betreff",)},
    "fr": {"from": ("De",), "sent": ("Envoyé", "Date"), "to": ("À",),
           "cc": ("Copie", "Répondre à"), "subject": ("Objet",)},
    "it": {"from": ("Da",), "sent": ("Inviato", "Data"), "to": ("A",),
           "cc": ("Cc",), "subject": ("Oggetto",)},
    "es": {"from": ("De",), "sent": ("Enviado", "Enviado el", "Fecha"),
           "to": ("Para",), "cc": ("CC",), "subject": ("Asunto",)},
    "nl": {"from": ("Van",), "sent": ("Verzonden", "Datum"), "to": ("Aan",),
           "cc": ("CC",), "subject": ("Onderwerp",)},
    "pt": {"from": ("De",), "sent": ("Enviado", "Enviada", "Enviado em",
                                    "Enviada em", "Data"),
           "to": ("Para",), "cc": ("Cc",), "subject": ("Assunto",)},
    "da": {"from": ("Fra",), "sent": ("Sendt", "Dato"), "to": ("Til",),
           "cc": ("Cc",), "subject": ("Emne",)},
    "nb": {"from": ("Fra",), "sent": ("Sendt", "Dato"), "to": ("Til",),
           "cc": ("Kopi",), "subject": ("Emne",)},
    "sv": {"from": ("Från",), "sent": ("Skickat", "Datum"), "to": ("Till",),
           "cc": ("Kopia",), "subject": ("Ämne",)},
    "fi": {"from": ("Lähettäjä",), "sent": ("Lähetetty", "Päivämäärä"),
           "to": ("Vastaanottaja",), "cc": ("Kopio",), "subject": ("Aihe",)},
    "pl": {"from": ("Od",), "sent": ("Wysłano", "Data"), "to": ("Do",),
           "cc": ("DW",), "subject": ("Temat",)},
    "cs": {"from": ("Od",), "sent": ("Odesláno", "Datum"), "to": ("Komu",),
           "cc": ("Kopie",), "subject": ("Předmět",)},
}

# The verb of an attribution line ("On … Ann wrote:"). Consulted only to
# widen the shape test — a verb alone never makes an attribution, or "as you
# wrote:" would be one.
_ATTRIBUTION_VERBS = {
    "en": ("wrote",),
    "de": ("schrieb",),
    "fr": ("a écrit",),
    "it": ("ha scritto",),
    "es": ("escribió",),
    "nl": ("schreef",),
    "pt": ("escreveu",),
    "da": ("skrev",),
    "nb": ("skrev",),
    "sv": ("skrev",),
    "fi": ("kirjoitti",),
    "pl": ("napisał", "napisała"),
    "cs": ("napsal", "napsala"),
}

# What a separator ("----- … -----") or an Apple-style lead line says when the
# message below it is quoted history …
_ORIGINAL_STEMS = {
    "en": ("original message",),
    "de": ("ursprüngliche nachricht", "originalnachricht"),
    "fr": ("message d'origine", "message original"),
    "it": ("messaggio originale",),
    "es": ("mensaje original",),
    "nl": ("oorspronkelijk bericht",),
    "pt": ("mensagem original",),
    "da": ("oprindelig meddelelse", "oprindelig besked"),
    "nb": ("opprinnelig melding",),
    "sv": ("ursprungligt meddelande", "originalmeddelande"),
    "fi": ("alkuperäinen viesti",),
    "pl": ("oryginalna wiadomość", "wiadomość oryginalna"),
    "cs": ("původní zpráva",),
}

# … and when it is a forwarded message, which is content.
_FORWARD_STEMS = {
    "en": ("forwarded",),
    "de": ("weitergeleitet",),
    "fr": ("transféré", "réexpédié"),
    "it": ("inoltrat",),
    "es": ("reenviad",),
    "nl": ("doorgestuurd",),
    "pt": ("encaminhad",),          # also covers "reencaminhada"
    "da": ("videresendt",),
    "nb": ("videresendt",),
    "sv": ("vidarebefordra",),
    "fi": ("välitetty",),
    "pl": ("przekazan",),
    "cs": ("přeposlan",),
}

# The subject prefix a client puts on a forward. Only the outermost prefix
# counts: "Re: Fwd: x" is a reply.
_FORWARD_PREFIXES = {
    "en": ("Fwd", "Fw"),
    "de": ("WG",),
    "fr": ("TR",),
    "it": ("I",),
    "es": ("RV", "Reenv"),
    "nl": ("Doorst",),
    "pt": ("ENC",),
    "da": ("VS",),
    "nb": ("VS",),
    "sv": ("VB",),
    "fi": ("VL",),
    "pl": ("PD",),
    "cs": ("FW",),
}

# The line a phone or an app signs with. Each entry is how the line STARTS;
# `*` stands for a short run of anything (the device's name).
_MOBILE_SIGNATURES = {
    "en": ("Sent from", "Sent with", "Sent via", "Sent using",
           "Get Outlook for"),
    "de": ("Von meinem * gesendet", "Gesendet von", "Gesendet mit",
           "Gesendet über", "Diese Nachricht wurde von meinem * gesendet",
           "Outlook für * beziehen", "Outlook für * herunterladen"),
    "fr": ("Envoyé de", "Envoyé depuis", "Envoyé à partir de", "Envoyé avec",
           "Télécharger Outlook pour", "Obtenir Outlook pour"),
    "it": ("Inviato da", "Inviato dal", "Inviato dall", "Inviato con",
           "Scarica Outlook per"),
    "es": ("Enviado desde", "Enviado con", "Obtener Outlook para"),
    "nl": ("Verzonden vanaf", "Verstuurd vanaf", "Verzonden met",
           "Verstuurd met", "Outlook voor * downloaden"),
    "pt": ("Enviado do", "Enviado a partir do", "Enviado com",
           "Obter o Outlook para"),
    "da": ("Sendt fra", "Sendt med", "Hent Outlook til"),
    "nb": ("Sendt fra", "Sendt med", "Skaff deg Outlook for"),
    "sv": ("Skickat från", "Skickat med", "Hämta Outlook för"),
    "fi": ("Lähetetty",),
    "pl": ("Wysłane z", "Wysłano z"),
    "cs": ("Odesláno z",),
}

# The word that makes a closing paragraph an unsubscribe block — together
# with a link or an address in the same paragraph, never alone.
_UNSUBSCRIBE_STEMS = {
    "en": ("unsubscribe", "opt out", "opt-out"),
    "de": ("abbestellen", "abmelden", "austragen"),
    "fr": ("désabonner", "désinscri"),
    "it": ("disiscri", "annulla l'iscrizione", "annullare l'iscrizione"),
    "es": ("darse de baja", "darte de baja", "cancelar la suscripción",
           "cancelar suscripción", "anular la suscripción"),
    "nl": ("uitschrijven", "afmelden"),
    "pt": ("cancelar a subscrição", "cancelar a inscrição",
           "cancelar inscrição", "descadastr"),
    "da": ("afmeld", "frameld"),
    "nb": ("meld deg av", "avslutte abonnement", "avregistrer"),
    "sv": ("avprenumerera", "avsluta prenumeration", "avregistrera"),
    "fi": ("peruuta tilaus", "peru tilaus"),
    "pl": ("wypisz się", "zrezygnuj z subskrypcji"),
    "cs": ("odhlásit",),
}

# A legal notice names BOTH of these: that the mail is confidential, and whom
# it is meant for. One of them is ordinary prose.
_CONFIDENTIAL_STEMS = {
    "en": ("confidential", "privileged"),
    "de": ("vertraulich",),
    "fr": ("confidentiel",),
    "it": ("riservat", "confidenzial"),
    "es": ("confidenci",),          # confidencial, confidenciales
    "nl": ("vertrouwelijk",),
    "pt": ("confidenci", "sigilos"),
    "da": ("fortrolig",),
    "nb": ("konfidensiell", "fortrolig"),
    "sv": ("konfidentiell",),
    "fi": ("luottamukselli",),
    "pl": ("poufn",),
    "cs": ("důvěrn",),
}
_RECIPIENT_STEMS = {
    "en": ("intended recipient", "addressee", "intended solely",
           "intended only"),
    "de": ("empfänger", "adressat"),
    "fr": ("destinataire",),
    "it": ("destinatari",),
    "es": ("destinatari",),
    "nl": ("geadresseerde", "bestemd voor"),
    "pt": ("destinatári",),
    "da": ("modtager",),
    "nb": ("mottaker",),
    "sv": ("mottagare",),
    "fi": ("vastaanottaj",),
    "pl": ("adresat", "odbiorc"),
    "cs": ("adresát", "příjemc"),
}


def _stems(table: dict) -> tuple:
    return tuple(sorted({_fold(word) for words in table.values() for word in words}))


def _words_re(table: dict) -> "re.Pattern":
    return re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(
        re.escape(stem).replace(r"\ ", r"\s+") for stem in _stems(table)))


_HEADER_ROLE = {
    _fold(word): role
    for labels in _HEADER_LABELS.values()
    for role, words in labels.items() for word in words
}
_VERB_RE = _words_re(_ATTRIBUTION_VERBS)
# The verb as the last word before the colon ("… a écrit :", "napisał(a):").
_VERB_END_RE = re.compile(_VERB_RE.pattern + r"\W{0,6}$")
_ORIGINAL = _stems(_ORIGINAL_STEMS)
_FORWARD = _stems(_FORWARD_STEMS)
_UNSUBSCRIBE = _stems(_UNSUBSCRIBE_STEMS)
_CONFIDENTIAL = _stems(_CONFIDENTIAL_STEMS)
_RECIPIENT = _stems(_RECIPIENT_STEMS)
_FORWARD_SUBJECT_RE = re.compile(
    r"^\s*(?:\[[^\]]{0,60}\]\s*)*(?:%s)\s*:" % "|".join(
        re.escape(prefix) for prefix in _stems(_FORWARD_PREFIXES)))
_MOBILE_RE = re.compile(r"^\W{0,3}(?:%s)(?!\w)" % "|".join(
    ".{1,40}?".join(re.escape(part).replace(r"\ ", r"\s+")
                    for part in entry.split("*"))
    for entry in _stems(_MOBILE_SIGNATURES)))


def _has_stem(text: str, stems: tuple) -> bool:
    folded = _fold(text)
    return any(stem in folded for stem in stems)


# ── Shapes ───────────────────────────────────────────────────────────────────

# Marks the HTML conversion leaves for the trim, where the markup knows what a
# line of text cannot say for itself. C0 controls that no mail legitimately
# carries: they are stripped from every input first, so a mail cannot forge
# one, and from every output last.
_ATTR = "\x01"            # prefix: this line is an attribution
_BOUNDARY = "\x02"        # a line of its own: quoted history may start here
_MARK_HARD = _BOUNDARY + "H"   # … by an element only a quoting client writes
_MARK_SOFT = _BOUNDARY + "S"   # … by a styling that others use too
_MARKS_RE = re.compile("[\x01\x02]")

# Up to three spaces of indent: four make a code block, and code that starts
# with ">" is not a quote.
_QUOTE_RE = re.compile(r"^ {0,3}>")
_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
_DIFF_HUNK_RE = re.compile(r"^(?:\d+(?:,\d+)?[acd]\d+(?:,\d+)?|---|< .*|<)$")
_COLON_END_RE = re.compile(r"[:：]\s*$")
_SENTENCE_END_RE = re.compile(r"[.!?…]\s*$")
# An address. The look-behind and the bounded local part are for cost, not
# for strictness: without them a line that is one long token (a base64 blob,
# a URL) is rescanned from each of its characters.
_ADDRESS_RE = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+")
_BRACKET_ADDRESS_RE = re.compile(
    r"[<\[(]\s{0,3}(?:mailto:)?[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+\s{0,3}[>\])]",
    re.I)
# … and the address as the last thing before it ("schrieb Ann <a@b.org>:").
_ADDRESS_END_RE = re.compile(
    r"[<\[(]\s{0,3}(?:mailto:)?[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+\s{0,3}[>\])]"
    r"\W{0,3}$", re.I)
_DATE_RE = re.compile(
    r"(?<!\d)(?:(?:19|20)\d\d|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})(?!\d)")
_TIME_RE = re.compile(
    r"(?<!\d)\d{1,2}:\d{2}(?!\d)"          # 14:02
    r"|(?<!\d)\d{1,2}\s?h\s?\d{2}(?!\d)"   # 14h02
    r"|\bkl\.?\s?\d{1,2}[.:]\d{2}"         # kl. 14.02
    r"|(?<!\d)\d{1,2}\s?[ap]\.?m\b",       # 2 pm
    re.I)
_URL_RE = re.compile(r"https?://\S+|www\.\S+|mailto:\S+", re.I)
_BARE_LINK_RE = re.compile(
    r"<?(?:https?://\S+|www\.\S+|[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+)>?", re.I)
# "Label: value". The colon must end the line or be followed by whitespace,
# which is what keeps "https://…" and "12:30" from being labels.
_LABEL_RE = re.compile(
    r"^\s*[*_]{0,2}([^\W\d_][^:：*\n]{0,40}?)[*_]{0,2}\s?[:：][*_]{0,2}"
    r"(?:[ \t\xa0]+(.*))?$")
# "----- Some words -----": the shape of every client's separator line.
_DASHED_RE = re.compile(r"^[-_=]{3,}\s*([^\W_].{1,60}?)\s*[-_=]{3,}$")
_RULER_RE = re.compile(r"^\s*[-_=*~]{5,}\s*$")
_LIST_RULER_RE = re.compile(r"^_{16,}$")


def _unmark(line: str) -> str:
    if line.startswith(_BOUNDARY):
        return ""
    return line.replace(_ATTR, "") if _ATTR in line else line


def _unmarked(lines: list) -> list:
    """The HTML conversion's lines as plain text would have them."""
    return _squeeze([_unmark(line) for line in lines])


def _squeeze(lines: list) -> list:
    """One blank line at most between paragraphs, none at either end."""
    out: list = []
    for line in lines:
        if line.strip():
            out.append(line)
        elif out and out[-1]:
            out.append("")
    while out and not out[-1]:
        out.pop()
    return out


def _clean_lines(text: str, keep_marks: bool = False) -> list:
    """A text as the lines the pipeline works on.

    `keep_marks` is for the HTML conversion's own output only. Anything else
    has the mark characters taken out of it, whatever stands around them.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not keep_marks:
        text = _MARKS_RE.sub("", text)
    return _squeeze([line.rstrip() for line in text.split("\n")])


def _chars(lines) -> int:
    return sum(len(_unmark(line)) + 1 for line in lines)


def _next_content(lines: list, start: int, limit: int | None = None) -> int | None:
    """Index of the first non-blank line at or after `start` (within `limit`)."""
    end = len(lines) if limit is None else min(len(lines), start + limit)
    for index in range(start, end):
        if lines[index].strip():
            return index
    return None


def _unquote(line: str) -> str:
    """One quote level off: "> > x" → "> x", "> x" → "x"."""
    match = _QUOTE_RE.match(line)
    if not match:
        return line
    rest = line[match.end():]
    return rest[1:] if rest.startswith(" ") else rest


def _label_role(line: str) -> str | None:
    match = _LABEL_RE.match(line)
    if not match:
        return None
    return _HEADER_ROLE.get(" ".join(_fold(match.group(1)).split()))


def _header_block_at(lines: list, index: int) -> bool:
    """Does a quoted mail's header block start at `lines[index]`?

    A run of ``Label: value`` lines whose labels cover a From, a Sent/Date
    and one more header in some language of the table — the block every
    client writes. Requiring the Subject (or an address on the From line)
    tells a quoted mail from an itinerary's "From: … To: … Date: …";
    requiring the date tells it from two or three headers an author pasted
    to talk about them.
    """
    if _label_role(lines[index]) is None:
        return False
    roles = set()
    from_has_address = False
    position = index
    # A header block is a dozen lines at most; the bound also keeps a mail
    # made of nothing but "From:" lines from costing a scan per line.
    last = min(len(lines), index + 16)
    while position < last and lines[position].strip():
        match = _LABEL_RE.match(lines[position])
        role = _label_role(lines[position]) if match else None
        if role:
            roles.add(role)
            if role == "from" and _ADDRESS_RE.search(match.group(2) or ""):
                from_has_address = True
        elif not any(_label_role(line)
                     for line in lines[position + 1:position + 4]):
            # Neither a header nor the wrapped tail of one (a long To: list
            # runs over several lines before the next label).
            break
        position += 1
    return ("from" in roles and "sent" in roles and len(roles) >= 3
            and ("subject" in roles or from_has_address))


def _is_attribution(line: str, need_verb: bool = False) -> bool:
    """The shape of "On <date>, <someone> wrote:" in any language.

    Ends in a colon and carries an address in brackets, or a date and a time;
    the verb table widens that to a bare address or a lone date. `need_verb`
    is the stricter test for an attribution that no ``>`` block follows, where
    being wrong would cut the rest of the mail.
    """
    line = line.strip()
    if len(line) > 320 or not _COLON_END_RE.search(line):
        return False
    bracketed = bool(_BRACKET_ADDRESS_RE.search(line))
    dated = bool(_DATE_RE.search(line)) and bool(_TIME_RE.search(line))
    folded = _fold(line)
    verb = bool(_VERB_RE.search(folded))
    if need_verb:
        # … and the line must END as an attribution does, on the verb or on
        # the address: "the invoice of 03.03.2025 14:02 wrote off the
        # balance:" has the verb and the date and is a sentence.
        return verb and (bracketed or dated) and bool(
            _VERB_END_RE.search(folded) or _ADDRESS_END_RE.search(line))
    if bracketed or dated:
        return True
    return verb and bool(_ADDRESS_RE.search(line) or _DATE_RE.search(line)
                         or _TIME_RE.search(line))


def _wrapped(group: list) -> bool:
    """Do these lines read as ONE line a client folded?

    Either the next word would not have fitted (a real wrap), or the tail is
    the leftover a wrap leaves ("wrote:", "<ann@example.org>:").
    """
    for first, second in zip(group, group[1:]):
        words = second.split()
        if not words:
            return False
        fits = len(first.rstrip()) + 1 + len(words[0]) <= 66
        if fits and len(words) > 3 and not first.rstrip().endswith(("<", ",")):
            return False
    return True


def _attribution_tail(kept: list, need_verb: bool = False) -> int:
    """How many trailing lines of `kept` are an attribution (0: none).

    Trailing blank lines count into the answer, so the caller can cut them
    with it. A line the HTML marked is an attribution outright; otherwise the
    last line must have the shape, alone or as the end of a folded line — and
    a folded line must start its paragraph and not end a sentence, so the
    author's own closing words are never read into it.
    """
    end = len(kept)
    while end and not _unmark(kept[end - 1]).strip():
        end -= 1
    if not end:
        return 0
    if kept[end - 1].startswith(_ATTR):
        start = end
        while start and kept[start - 1].startswith(_ATTR):
            start -= 1
        return len(kept) - start
    if not _COLON_END_RE.search(kept[end - 1]):
        return 0
    # The folded forms first: "…, Ann Example\n<ann@example.org> wrote:" has a
    # last line that passes on its own and would leave its head behind.
    for span in (2, 3):
        start = end - span
        if start < 0:
            break
        group = [_unmark(line) for line in kept[start:end]]
        head = group[0]
        if (not head.strip() or _QUOTE_RE.match(head)
                or _SENTENCE_END_RE.search(head)):
            break
        if start and _unmark(kept[start - 1]).strip():
            continue              # not where the paragraph begins
        lead = " ".join(group[:-1])
        if not (_DATE_RE.search(lead) or _TIME_RE.search(lead)):
            continue              # an attribution's head carries its date
        if not _wrapped(group):
            continue
        if _is_attribution(" ".join(part.strip() for part in group), need_verb):
            return len(kept) - start
    if need_verb and end > 1 and _unmark(kept[end - 2]).strip():
        return 0                  # mid-paragraph: a sentence of the author's
    if _is_attribution(_unmark(kept[end - 1]), need_verb):
        return len(kept) - end + 1
    return 0


def _is_forward_marker(lines: list, index: int) -> bool:
    """Is `lines[index]` the line a client writes above a forwarded message?

    "---------- Forwarded message ---------" by its dashes and its word;
    "Begin forwarded message:" by its word, its colon and the header block
    that follows it — which is what keeps "I forwarded this to Bob:" out.
    """
    line = lines[index].strip()
    if not line or len(line) > 80:
        return False
    match = _DASHED_RE.match(line)
    if match:
        return _has_stem(match.group(1), _FORWARD)
    if (not _COLON_END_RE.search(line) or len(line.split()) > 7
            or not _has_stem(line, _FORWARD)):
        return False
    following = _next_content(lines, index + 1)
    return (following is not None
            and _label_role(_unquote(lines[following])) is not None)


def _forward_body(lines: list) -> list:
    """The forwarded message, from its marker line on, ready to keep.

    Apple Mail quotes a forwarded message with ``>``; that quoting is the
    client's, not the message's, so it comes off. Only when the first quoted
    line is a header: afterwards the first line no longer starts with ``>``,
    so a second pass leaves the text alone.
    """
    first = _next_content(lines, 1)
    if first is None:
        return lines
    head = lines[first]
    levels = 0
    while _QUOTE_RE.match(head):
        head = _unquote(head)
        levels += 1
    if not levels or _label_role(head) is None:
        return lines
    prefix = re.compile(r"(?: {0,3}> ?){1,%d}" % levels)
    return [lines[0]] + [prefix.sub("", line, count=1) if _QUOTE_RE.match(line)
                         else line for line in lines[1:]]


def _quote_flags(lines: list) -> list:
    """Per line: is it a quoted line? (See "Known hard case" above.)"""
    fences = [i for i, line in enumerate(lines) if _FENCE_RE.match(line)]
    fenced = set()
    for opening, closing in zip(fences[0::2], fences[1::2]):
        fenced.update(range(opening, closing + 1))
    candidates = [i for i, line in enumerate(lines)
                  if i not in fenced and _QUOTE_RE.match(line)]
    shallow = any(not lines[i].lstrip().startswith(">>>") for i in candidates)
    flags = [False] * len(lines)
    for i in candidates:
        body = lines[i].lstrip()
        if body.startswith(">="):
            continue
        if (not shallow and body.startswith(">>>")
                and (len(body) == 3 or body[3] == " ")):
            continue
        flags[i] = True
    return flags


def _mid_sentence(lines: list, start: int) -> bool:
    """Is the ``>`` line at `start` the middle of a folded sentence?

    "the value must be / > 100 for this to work": the only ``>`` line of the
    whole mail (the caller has counted), with text right above it that ends
    neither a sentence nor a lead-in. A quote opens its own paragraph or
    follows a line that is finished — and an inline reply quotes more than
    once.
    """
    above = lines[start - 1].rstrip()
    return bool(above) and not (
        _SENTENCE_END_RE.search(above) or _COLON_END_RE.search(above))


def _history_event(lines: list, quoted: list, index: int, kept: list) -> int | None:
    """Does quoted history that carries no ``>`` start at `lines[index]`?

    Answers how many trailing lines of `kept` belong to it (0 normally; the
    folded head of an attribution otherwise), or None when it does not.
    """
    line = lines[index].strip()
    if not line:
        return None
    match = _DASHED_RE.match(line)
    if match:
        if _has_stem(match.group(1), _ORIGINAL):
            return 0
        following = _next_content(lines, index + 1, 2)
        if following is not None and _header_block_at(lines, following):
            return 0
        return None
    if _header_block_at(lines, index):
        return 0
    if _LIST_RULER_RE.match(line) and index + 1 < len(lines):
        # Outlook's rule above its header block, in a language the tables do
        # not cover: the shape carries it — "Label: Someone <address>" right
        # under the rule, and two more "Label: value" lines.
        first = _LABEL_RE.match(lines[index + 1])
        if first and _ADDRESS_RE.search(first.group(2) or ""):
            labels = sum(1 for item in lines[index + 1:index + 7]
                         if _LABEL_RE.match(item))
            if labels >= 3:
                return 0
    if _COLON_END_RE.search(line):
        # An attribution with no ">" block under it: some clients (Yahoo, a
        # few mobile ones) quote the earlier mail as bare text. A ">" block
        # is the caller's case and needs no verb.
        following = _next_content(lines, index + 1)
        if following is None or not quoted[following]:
            kept.append(lines[index])
            span = _attribution_tail(kept, need_verb=True)
            kept.pop()
            if span:
                return span - 1
    return None


def _boundary_confirmed(lines: list, index: int, hard: bool) -> bool:
    """Is the element the HTML marked at `index` really a quoted header?"""
    found = []
    position = index + 1
    while position < len(lines) and len(found) < 6:
        if lines[position].strip():
            found.append(position)
        position += 1
    if not found:
        return False
    labels = [i for i in found if _LABEL_RE.match(lines[i])]
    if hard:
        # #divRplyFwdMsg is Outlook's own; any two "Label: value" lines under
        # it are its header block, in whatever language.
        return len(labels) >= 2
    if _header_block_at(lines, found[0]):
        return True
    first = _LABEL_RE.match(lines[found[0]])
    return bool(first and len(labels) >= 3
                and _BRACKET_ADDRESS_RE.search(first.group(2) or ""))


def _strip_quotes(raw: list, forward_hint: bool):
    """Stage 1. Answers ``(intro, forwarded, removed_chars)``.

    `intro` is the author's own lines; `forwarded` is None, or the forwarded
    message from its marker on, which is content and is kept as it stands.
    """
    lines = [_unmark(line) for line in raw]
    quoted = _quote_flags(lines)
    lone = sum(quoted) == 1
    kept: list = []
    removed = 0
    index = 0
    total = len(lines)

    def keep(line: str) -> None:
        # Never two blank lines in a row: the look-back for an attribution
        # skips the blank tail of `kept`, and must not have a long one to skip.
        if line.strip() or (kept and kept[-1].strip()):
            kept.append(line)

    while index < total:
        if quoted[index]:
            end = index
            while end < total and quoted[end]:
                end += 1
            if index and (_DIFF_HUNK_RE.match(lines[index - 1].strip())
                          or (lone and _mid_sentence(lines, index))):
                kept.extend(lines[index:end])      # content, not a quote
                index = end
                continue
            inner = [_unquote(line) for line in lines[index:end]]
            first = _next_content(inner, 0)
            if first is not None and _is_forward_marker(inner, first):
                # Apple Mail (HTML) and Yahoo quote the whole forward, marker
                # included.
                return kept, _forward_body(inner[first:] + lines[end:]), removed
            drop = _attribution_tail(kept)
            if drop:
                removed += _chars(kept[len(kept) - drop:])
                del kept[len(kept) - drop:]
            removed += _chars(lines[index:end])
            keep("")                # a paragraph break where the quote stood
            index = end
            continue
        event = None
        if raw[index].startswith(_BOUNDARY):
            if _boundary_confirmed(lines, index, raw[index] == _MARK_HARD):
                event = 0
        elif _is_forward_marker(lines, index):
            return kept, _forward_body(lines[index:]), removed
        else:
            event = _history_event(lines, quoted, index, kept)
        if event is None:
            keep(raw[index])
            index += 1
            continue
        rest = [_unmark(line) for line in kept[len(kept) - event:]] if event else []
        if event:
            del kept[len(kept) - event:]
        rest += lines[index:]
        if forward_hint:
            # An Outlook forward: the "history" is the message being passed
            # on. Its rule or separator would read as a reply's on the next
            # pass, so the plain forward marker replaces it.
            start = _next_content(rest, 0)
            while start is not None and (
                    _RULER_RE.match(rest[start])
                    or _DASHED_RE.match(rest[start].strip())):
                start = _next_content(rest, start + 1)
            rest = rest[start:] if start is not None else []
            return kept, [FORWARD_MARKER] + rest, removed
        removed += _chars(rest)
        break
    return kept, None, removed


def _drop_from(lines: list, index: int) -> int:
    """Cut `lines` at `index`, in place; the blank lines above go with it."""
    removed = _chars(lines[index:])
    del lines[index:]
    while lines and not lines[-1].strip():
        lines.pop()
    return removed


# The tail stages below work on the END of the text and look no further up
# than a signature or a footer can reach, so repeating them until nothing
# changes costs what they remove — not a scan of the mail per repetition,
# which a mail built to be slow could otherwise turn into hours.

def _strip_signature(lines: list) -> int:
    """Stage 2, in place: the ``-- `` delimiter, then the phone's closing line.

    Walking up from the end, a delimiter counts while no more than
    SIGNATURE_MAX_LINES lines separate it from the end — or from a delimiter
    already found below it, which is how a signature under a signature goes
    in one go.
    """
    removed = 0
    cut = None
    below = 0
    for index in range(len(lines) - 1, -1, -1):
        # Clients strip trailing blanks as often as not, so "--" counts too.
        if lines[index] == "--":
            cut = index
            below = 0
        elif lines[index].strip():
            below += 1
            if below > SIGNATURE_MAX_LINES:
                break
    if cut is not None:
        removed += _drop_from(lines, cut)
    while lines and len(lines[-1]) <= 120 and _MOBILE_RE.match(_fold(lines[-1])):
        removed += _drop_from(lines, len(lines) - 1)
    return removed


def _tail_paragraphs(lines: list, count: int) -> list:
    """``(start, end)`` of the last `count` runs of non-blank lines, in order."""
    spans = []
    end = None
    for index in range(len(lines) - 1, -2, -1):
        if index >= 0 and lines[index].strip():
            if end is None:
                end = index + 1
        elif end is not None:
            spans.append((index + 1, end))
            end = None
            if len(spans) == count:
                break
    return spans[::-1]


# How many lines may still follow a footer paragraph: the sender's postal
# address under an unsubscribe link; the same notice again in a second
# language under a disclaimer.
_FOOTER_MAX_AFTER = {"unsubscribe": 6, "disclaimer": 25}
# No footer paragraph is longer than this many lines.
_FOOTER_MAX_LINES = 40


def _footer_kind(text: str) -> str | None:
    folded = _fold(text)
    if any(stem in folded for stem in _UNSUBSCRIBE):
        # The word alone is prose ("tell me if you want to unsubscribe"); a
        # footer hands over the means.
        if _URL_RE.search(text) or _ADDRESS_RE.search(text):
            return "unsubscribe"
    if (len(text) >= 100
            and any(stem in folded for stem in _CONFIDENTIAL)
            and any(stem in folded for stem in _RECIPIENT)):
        return "disclaimer"
    return None


def _footer_start(lines: list, start: int, end: int, kind: str) -> int:
    """Where in its paragraph a footer begins.

    A server glues its notice right under the sender's last line as often as
    it leaves a blank line, so the paragraph can open with "Regards / Bob".
    The footer starts at its first line that carries a cue word, taken back
    over the lines of the sentence the cue stands in — a full-width line
    above, or a line this one continues in lower case — and never over a
    short, finished line, which is a sign-off.
    """
    cues = _UNSUBSCRIBE if kind == "unsubscribe" else _CONFIDENTIAL + _RECIPIENT
    for first in range(start, end):
        folded = _fold(lines[first])
        if any(stem in folded for stem in cues):
            break
    else:
        return start
    while first > start:
        above = lines[first - 1].strip()
        continues = (lines[first].lstrip()[:1].islower()
                     and not _SENTENCE_END_RE.search(above)
                     and not _COLON_END_RE.search(above))
        if len(above) < 50 and not continues:
            break
        first -= 1
    # Cut short only if what is cut is still the whole footer.
    if _footer_kind(" ".join(lines[first:end])) == kind:
        return first
    return start


def _strip_footer(lines: list) -> int:
    """Stage 3, in place: legal notices, unsubscribe blocks, list footers.

    "At the end" is enforced, not assumed: a footer paragraph is cut together
    with what follows it only when little follows it. An unsubscribe line at
    the top of a notification, with the news under it, is not a footer.
    """
    removed = 0
    for start, end in _tail_paragraphs(lines, FOOTER_MAX_PARAGRAPHS):
        if end - start > _FOOTER_MAX_LINES:
            continue
        kind = _footer_kind(" ".join(lines[start:end]))
        if not kind:
            continue
        after = sum(1 for line in lines[end:] if line.strip())
        if after <= _FOOTER_MAX_AFTER[kind]:
            removed += _drop_from(lines, _footer_start(lines, start, end, kind))
            break
    # A mailing list's footer: a long underscore rule, then a few lines that
    # say where the list lives — one of them nothing but its URL or address,
    # which a postscript under a rule would not be.
    tail = []
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        if _LIST_RULER_RE.match(line):
            if any(_BARE_LINK_RE.fullmatch(item) for item in tail):
                removed += _drop_from(lines, index)
            break
        if line:
            tail.append(line)
            if len(tail) > 6:
                break
    return removed


def _strip_tail(lines: list, removed: dict, history_cut: bool) -> None:
    """Stages 2 and 3, repeated until the end of the text is settled.

    They uncover work for each other — a notice under the phone's closing
    line hides that line from the signature stage until the footer stage has
    run — so neither is done before both find nothing.
    """
    while True:
        size = len(lines)
        # The rule a client draws above the history it appends stays behind
        # when the history goes; it belongs to what was cut.
        while lines and _RULER_RE.match(lines[-1]):
            removed["quote" if history_cut else "footer"] += _drop_from(
                lines, len(lines) - 1)
        removed["signature"] += _strip_signature(lines)
        removed["footer"] += _strip_footer(lines)
        if len(lines) == size:
            return


def _pass(raw: list, forward_hint: bool, removed: dict) -> list:
    intro, forwarded, cut = _strip_quotes(raw, forward_hint)
    removed["quote"] += cut
    intro = _squeeze([_unmark(line) for line in intro])
    _strip_tail(intro, removed, bool(cut))
    if forwarded is not None:
        intro = intro + [""] + forwarded
    return _squeeze([line.rstrip() for line in intro])


#: Passes after which a text that still changes is given up on.
_MAX_PASSES = 20


def _trim_lines(raw: list, forward_hint: bool, removed: dict) -> list:
    """The pipeline, to its fixpoint.

    Removing the end of a text can change how the rest reads (a quote that
    was "inside a fenced block" until the closing fence went with the
    signature), so the pipeline runs until a pass changes nothing — which is
    what makes the trim idempotent by construction. A real mail settles on
    the second pass. One that has not settled after `_MAX_PASSES` was built
    not to, and gets no trim at all: the untrimmed text is the one answer
    that is the same however often it is asked for.
    """
    lines = raw
    for _ in range(_MAX_PASSES):
        after = _pass(lines, forward_hint, removed)
        if after == lines:
            return lines
        lines = after
    raise RuntimeError("the trim did not settle")


def _marker_survives(lines: list) -> bool:
    """Did quoting the trim could not settle stay in the text?"""
    if any(_QUOTE_RE.match(line) for line in lines):
        return True
    for line in reversed(lines):
        if line.strip():
            # An attribution left hanging at the very end.
            return _is_attribution(line)
    return False


def is_forward_subject(subject: str | None) -> bool:
    """Is the subject's outermost prefix a forward's ("Fwd:", "WG:", "TR:", …)?"""
    return bool(subject and _FORWARD_SUBJECT_RE.match(_fold(subject)))


def _settle(raw: list, forward_hint: bool) -> tuple:
    """``(lines, removed)`` of one source; no lines when the trim failed or
    left nothing."""
    removed = {"quote": 0, "signature": 0, "footer": 0, "pixels": 0}
    try:
        return _trim_lines(raw, forward_hint, removed), removed
    except Exception:       # a bubble with too much in it beats no bubble
        return [], removed


# A quote container some client wrote into the HTML part: worth a parse when
# the plain part showed no quote.
_HTML_QUOTE_HINT_RE = re.compile(
    r"divRplyFwdMsg|appendonsend|gmail_quote|yahoo_quoted|protonmail_quote"
    r"|moz-cite-prefix|type\s*=\s*[\"']?cite", re.I)

#: A plain part shorter than this, beside an HTML part that says
#: `STUB_RATIO` times as much (and a good deal in absolute terms), is a stub
#: — "This mail needs an HTML viewer", a bare link — not the mail's text.
STUB_CHARS = 400
STUB_RATIO = 8


def _is_stub(plain: str, marked: str) -> bool:
    said = len(" ".join(plain.split()))
    says = len(" ".join(_MARKS_RE.sub("", marked).split()))
    return says > 1000 and says > STUB_RATIO * said


def trim(plain: str | None, html: str | None, subject: str | None = None) -> TrimResult:
    """Reduce a mail to its own new text. See the module docstring.

    `plain` and `html` are the mail's two body parts, either may be None. The
    plain part is the source whenever it is the mail's text; see "Which part
    is the source" in the module docstring for when it is not.

    `subject` is optional and only ever widens what is kept: a forward's
    prefix on it makes Outlook-style history, which no marker tells from a
    reply's, the forwarded message it is.
    """
    forward = is_forward_subject(subject)
    has_plain = isinstance(plain, str) and bool(plain.strip())
    has_html = isinstance(html, str) and bool(html.strip())
    converted = None            # (marked text, pixels) of the HTML part, once

    def html_source():
        nonlocal converted
        if converted is None:
            converted = _convert(html, marks=True)
        return converted

    if (has_plain and has_html and len(plain.strip()) < STUB_CHARS
            and len(html) > 2 * STUB_CHARS
            and _is_stub(plain, html_source()[0])):
        has_plain = False       # the plain part is not the mail

    lines: list = []
    untrimmed: list = []
    removed = {"quote": 0, "signature": 0, "footer": 0, "pixels": 0}
    if has_plain:
        untrimmed = _clean_lines(plain)
        lines, removed = _settle(untrimmed, forward)
    if has_html and (not has_plain or (
            not removed["quote"] and _HTML_QUOTE_HINT_RE.search(html))):
        marked, pixels = html_source()
        raw = _clean_lines(marked, keep_marks=True)
        candidate, counted = _settle(raw, forward)
        if not candidate:
            # The marks made the trim take everything. What the text alone
            # supports is what a later trim of this result would arrive at,
            # so it is the answer now.
            candidate, counted = _settle(_unmarked(raw), forward)
        counted["pixels"] = pixels
        if not has_plain:
            untrimmed = _unmarked(raw)
            lines, removed = candidate, counted
        elif candidate and counted["quote"] and (
                not lines or _chars(candidate) < _chars(lines)):
            lines, removed = candidate, counted
    if not lines:
        # Greedy to the point of nothing: the mail was all quote, or all
        # signature. Show it as it came.
        lines = untrimmed
        removed.update(quote=0, signature=0, footer=0)
    text = "\n".join(lines)
    wanting = bool(text) and (
        not has_plain or len(text) > WANTING_CHARS or _marker_survives(lines))
    return TrimResult(text, "wanting" if wanting else "deterministic", removed)


# ── HTML → text ──────────────────────────────────────────────────────────────

_HTML_SPACE_RE = re.compile(r"[ \t\n\r\f\v]+")
# Padding a sender hides in the text: zero-width space, BOM, combining
# grapheme joiner, soft hyphen. (ZWJ/ZWNJ stay: emoji and several scripts need
# them.) The trim's own marks go with them, so no mail can forge one.
_INVISIBLE = {ord(ch): None for ch in "\u200b\ufeff\u034f\xad\x00\x01\x02"}

_VOID_TAGS = frozenset((
    "area", "base", "basefont", "bgsound", "br", "col", "embed", "frame",
    "hr", "img", "input", "keygen", "link", "meta", "param", "source",
    "track", "wbr"))
# Elements whose content is never text of the mail.
_MUTE_TAGS = frozenset((
    "script", "style", "head", "title", "template", "noscript", "svg", "math",
    "object", "iframe", "select", "textarea", "audio", "video", "canvas",
    "applet", "noembed", "noframes", "xml", "datalist"))
# What may stand inside <head>. Anything else means the head was never
# closed and the body has begun — the HTML parsing rule, and the difference
# between a sloppy mail and an empty bubble.
_HEAD_TAGS = frozenset((
    "meta", "link", "title", "style", "script", "base", "basefont",
    "bgsound", "noscript", "template", "xml"))
_PARAGRAPH_TAGS = frozenset((
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "dl", "figure",
    "address", "fieldset", "form"))
_BREAK_TAGS = frozenset((
    "div", "section", "article", "header", "footer", "nav", "main", "aside",
    "center", "dt", "dd", "caption", "figcaption", "legend", "summary",
    "details", "thead", "tbody", "tfoot", "colgroup", "option", "body",
    "html"))
# A start tag of these ends an open <p> (HTML's own rule).
_CLOSES_P = _PARAGRAPH_TAGS | frozenset((
    "div", "ul", "ol", "table", "pre", "hr", "section", "article", "header",
    "footer", "nav", "main", "aside", "center", "li", "details"))
_TABLE_TAGS = frozenset((
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption",
    "colgroup"))
# … unless a container stands between them.
_P_BARRIERS = (_CLOSES_P - {"p"}) | _TABLE_TAGS
# Quote containers, by the client that writes them.
_QUOTE_BLOCK_CLASSES = frozenset((
    "gmail_quote", "protonmail_quote", "tutanota_quote"))
_QUOTE_ANY_CLASSES = frozenset(("yahoo_quoted",))
_ATTRIBUTION_CLASSES = frozenset(("gmail_attr", "moz-cite-prefix"))
# Outlook's own ids. Matched as endings: a mail that passed through
# Exchange again has them prefixed ("x_divRplyFwdMsg").
_HISTORY_IDS = ("divrplyfwdmsg", "appendonsend")
_PREHEADER_RE = re.compile(r"pre-?header|preview-?text", re.I)
# Hidden for every reader. (`mso-hide:all` is not here: it hides from Outlook
# only, and what the other clients show is content.)
_HIDDEN_STYLE_RE = re.compile(
    r"display:none|visibility:hidden"
    r"|(?:^|;)opacity:0(?:\.0+)?(?:;|!|$)"
    r"|(?:^|;)font-size:0(?:\.0+)?(?:px|pt|em|rem|%)?(?:;|!|$)")
# A box of no size hides its content only together with overflow:hidden.
_ZERO_BOX_RE = re.compile(
    r"(?:^|;)(?:max-)?(?:height|width):0(?:\.0+)?(?:px|pt|em|rem|%)?(?:;|!|$)")
_BORDER_TOP_RE = re.compile(r"border-top:(?!none|0|hidden)")
_NO_MARGIN_RE = re.compile(r"(?:^|;)margin(?:-top|-bottom)?:0(?:[a-z%]*)(?:;|!|\s|$)")
_HR_TEXT = "-" * 20
# How deep elements are tracked, in the converter and in the sanitiser. Real
# mail nests a few dozen levels (layout tables); the bound is what keeps the
# cost of a mail linear in its size whatever it nests.
_MAX_DEPTH = 256


def _style_of(attrs: dict) -> str:
    """The style attribute, lower-cased and with all whitespace removed."""
    return "".join((attrs.get("style") or "").lower().split())


def _classes_of(attrs: dict) -> set:
    return set((attrs.get("class") or "").lower().split())


def _is_hidden(attrs: dict) -> bool:
    """Would this element show nothing? (display:none, a preheader, zero size)"""
    if "hidden" in attrs:
        return True
    style = _style_of(attrs)
    if style:
        if _HIDDEN_STYLE_RE.search(style):
            return True
        if "overflow:hidden" in style and _ZERO_BOX_RE.search(style):
            return True
    marker = "%s %s" % (attrs.get("class") or "", attrs.get("id") or "")
    return bool(_PREHEADER_RE.search(marker))


def _pixels(value) -> float | None:
    """A length in CSS pixels, or None when it is not one ("100%", "auto")."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:px)?\s*", str(value), re.I)
    return float(match.group(1)) if match else None


def _style_length(style: str, prop: str) -> float | None:
    match = re.search(r"(?:^|;)%s:(\d+(?:\.\d+)?)(?:px)?(?:;|!|$)" % prop, style)
    return float(match.group(1)) if match else None


def is_tracking_pixel(width=None, height=None, attrs=None) -> bool:
    """Is this image a tracking pixel — two pixels or fewer a side?

    `width` and `height` are the image's real size when the caller knows it
    (a decoded inline part); `attrs` are the attributes its ``<img>`` declared
    (a dict or the parser's pair list), consulted for whichever side is not
    given, ``width="1"`` and ``style="width:1px"`` alike. An image is a pixel
    when at least one side is known and no known side exceeds two — a
    1 × 600 spacer is not one — or when its own markup hides it, since an
    image nobody is meant to see is loaded for one reason only.
    """
    sides = [_pixels(width), _pixels(height)]
    if attrs:
        declared = attrs if isinstance(attrs, dict) else {
            str(key).lower(): value for key, value in attrs}
        style = _style_of(declared)
        if "hidden" in declared or (style and re.search(
                r"display:none|visibility:hidden", style)):
            return True
        for slot, prop in enumerate(("width", "height")):
            if sides[slot] is None:
                sides[slot] = _pixels(declared.get(prop))
            if sides[slot] is None and style:
                sides[slot] = _style_length(style, prop)
    known = [side for side in sides if side is not None]
    return bool(known) and all(side <= 2 for side in known)


def _url_key(url: str) -> str:
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", url.lower()).rstrip("/")


def _link_note(href: str | None, text: str) -> str | None:
    """What to write after a link's text, or None when the target adds nothing.

    A target is worth writing when the text does not already say it: not when
    the text is the URL, not when a ``mailto:`` merely repeats the visible
    address. Only http(s), mailto and tel targets are ever written; a fragment
    or a script pseudo-link is no destination.
    """
    href = (href or "").strip()
    if not href or not text:
        return None
    low = href.lower()
    if low.startswith("mailto:"):
        address, _, query = href[7:].partition("?")
        address = unquote(address).strip()
        if not address:
            return None
        if query:               # ?subject=/&body= is part of the action
            return href
        return None if address.lower() in text.lower() else address
    if low.startswith("tel:"):
        number = href[4:].strip()
        digits = re.sub(r"\D", "", number)
        if not digits or digits in re.sub(r"\D", "", text):
            return None
        return number
    if not low.startswith(("http://", "https://")):
        return None
    key = _url_key(href)
    if not key or key in re.sub(r"https?://|www\.", "", text.lower()):
        return None
    return href


class _Block:
    """Lines under construction: one per container (document, quote, list
    item, table cell), so a container can be finished and then transformed —
    prefixed, indented, joined into a row — as a whole."""

    __slots__ = ("lines", "cur", "gap")

    def __init__(self):
        self.lines: list = []
        self.cur: list = []
        self.gap = False

    def text(self, data: str) -> None:
        if "\xa0" in data:
            if not data.strip(" \t\n\r\f\v\xa0"):
                # Nothing but blanks with a hard one among them. Alone on its
                # line that is how a blank line is written (<p>&nbsp;</p>);
                # after text it is one space.
                if not self.cur:
                    self.cur.append("\xa0")
                elif not self.cur[-1].endswith((" ", "\xa0")):
                    self.cur.append(" ")
                return
            # Runs of them indent and pad; in plain text that is one space.
            data = data.replace("\xa0", " ")
        data = _HTML_SPACE_RE.sub(" ", data)
        if self.cur == ["\xa0"]:
            self.cur = []
        if not self.cur:
            data = data.lstrip(" ")
        elif self.cur[-1].endswith(" ") and data.startswith(" "):
            data = data[1:]
        if data:
            self.cur.append(data)

    def raw(self, data: str) -> None:           # inside <pre>
        first, *rest = data.split("\n")
        if first:
            self.cur.append(first)
        for line in rest:
            self.newline()
            if line:
                self.cur.append(line)

    def emit(self, line: str) -> None:
        if self.gap:
            if self.lines and self.lines[-1]:
                self.lines.append("")
            self.gap = False
        self.lines.append(line)

    def newline(self) -> None:                  # <br>: ends even an empty line
        self.emit("".join(self.cur).rstrip(" "))
        self.cur = []

    def brk(self) -> None:                      # a block edge: ends a begun line
        if self.cur:
            self.newline()

    def para(self) -> None:                     # a paragraph edge
        self.brk()
        self.gap = True

    def add(self, lines: list) -> None:
        self.brk()
        for line in lines:
            self.emit(line)

    def done(self) -> list:
        self.brk()
        return _squeeze(self.lines)


class _Element:
    __slots__ = ("tag", "kind", "data")

    def __init__(self, tag: str, kind: str, data=None):
        self.tag = tag
        self.kind = kind
        self.data = data


class _TextConverter(HTMLParser):
    """HTML → lines of readable text.

    Block structure becomes line breaks, a cited blockquote becomes ``>``
    lines (so the plain-text trim reads an HTML mail's quotes exactly as it
    reads a plain mail's), lists get markers, a table is written row by row,
    a link's target is written after its text when it adds information, and
    what a reader would never see — scripts, styles, the head, hidden
    elements, tracking pixels — is left out.

    With `marks`, the lines also carry what only the markup knows: which line
    is an attribution, where Outlook's quoted header starts.
    """

    def __init__(self, marks: bool = False):
        super().__init__(convert_charrefs=True)
        self.marks = marks
        self.pixels = 0
        self._blocks = [_Block()]
        self._stack: list = []
        self._inert = 0         # depth inside muted or hidden elements
        self._overflow = None   # [tag, depth] of a muted element past _MAX_DEPTH
        self._pre = 0
        self._anchors: list = []

    @property
    def _blk(self) -> _Block:
        return self._blocks[-1]

    # ── stack discipline ──

    def _pop_to(self, index: int) -> None:
        while len(self._stack) > index:
            self._close(self._stack.pop())

    def _find(self, tags, barriers=()) -> int | None:
        for index in range(len(self._stack) - 1, -1, -1):
            tag = self._stack[index].tag
            if tag in tags:
                return index
            if tag in barriers:
                return None
        return None

    def _implied_end(self, tag: str) -> None:
        """Close what this start tag closes implicitly, as a browser would."""
        if self._stack and tag != "head":
            head = self._find(("head",))
            if head is not None and (tag == "body" or (
                    self._stack[-1].tag == "head"
                    and tag not in _HEAD_TAGS and ":" not in tag)):
                self._pop_to(head)
        if tag == "li":
            found = self._find(("li",), ("ul", "ol", "td", "th", "table"))
        elif tag in ("td", "th"):
            found = self._find(("td", "th"), ("tr", "table"))
        elif tag in ("tr", "thead", "tbody", "tfoot"):
            found = self._find(("tr",), ("table",))
        elif tag in _CLOSES_P:
            found = self._find(("p",), _P_BARRIERS)
        else:
            return
        if found is not None:
            self._pop_to(found)

    # ── parser callbacks ──

    def handle_startendtag(self, tag, attrs):
        # "<div/>" opens a div in HTML; only foreign content self-closes.
        if tag in ("svg", "math") or ":" in tag:
            return
        self.handle_starttag(tag, attrs)

    def handle_starttag(self, tag, attrs):
        a = {}
        for key, value in attrs:
            a.setdefault(key, value or "")
        if self._overflow:
            if tag == self._overflow[0]:
                self._overflow[1] += 1
            return
        if tag in _VOID_TAGS:
            if not self._inert:
                self._void(tag, a)
            return
        self._implied_end(tag)
        mute = tag in _MUTE_TAGS or _is_hidden(a)
        if len(self._stack) >= _MAX_DEPTH:
            # Deeper than any mail nests: the element is not tracked, so every
            # walk of the stack stays short. What it would have hidden is
            # still hidden, by counting its own tag until it closes.
            if mute and not self._inert:
                self._overflow = [tag, 1]
            return
        if self._inert:
            self._stack.append(_Element(tag, "inert"))
        elif mute:
            self._stack.append(_Element(tag, "mute"))
            self._inert += 1
        else:
            self._stack.append(self._open(tag, a))

    def _open(self, tag: str, a: dict) -> _Element:
        blk = self._blk
        classes = _classes_of(a)
        ident = (a.get("id") or "").lower()
        # Marks are written at the top level only, where a line of the text is
        # a line of the block: inside a quote, a list item or a table cell the
        # line gets a prefix, and a mark that is not at its start is no mark.
        marks = self.marks and len(self._blocks) == 1
        if marks:
            if ident.endswith(_HISTORY_IDS):
                blk.para()
                blk.emit(_MARK_HARD)
            elif tag == "div" and _BORDER_TOP_RE.search(_style_of(a)):
                blk.para()
                blk.emit(_MARK_SOFT)
        cited = classes & _QUOTE_ANY_CLASSES or ident.startswith("yahoo_quoted")
        if tag == "blockquote" and (
                (a.get("type") or "").lower() == "cite" or "cite" in a
                or classes & _QUOTE_BLOCK_CLASSES):
            cited = True
        if cited:
            blk.para()
            self._blocks.append(_Block())
            return _Element(tag, "quote")
        if marks and classes & _ATTRIBUTION_CLASSES:
            blk.brk()
            self._blocks.append(_Block())
            return _Element(tag, "attribution")
        if tag in ("ul", "ol"):
            nested = self._find(("li",), ("td", "th", "table")) is not None
            blk.brk() if nested else blk.para()
            start = _pixels(a.get("start")) if tag == "ol" else None
            return _Element(tag, "list", {
                "ordered": tag == "ol", "n": int(start) if start else 1,
                "nested": nested})
        if tag == "li":
            blk.brk()
            parent = self._find(("ul", "ol"), ("td", "th", "table"))
            marker = "- "
            data = self._stack[parent].data if parent is not None else None
            if isinstance(data, dict) and data["ordered"]:
                marker = "%d. " % data["n"]
                data["n"] += 1
            self._blocks.append(_Block())
            return _Element(tag, "item", marker)
        if tag == "table":
            blk.para()
            return _Element(tag, "table")
        if tag == "tr":
            blk.brk()
            return _Element(tag, "row", [])
        if tag in ("td", "th"):
            if self._find(("tr",), ("table",)) is None:
                self._stack.append(_Element("tr", "row", []))   # a row-less cell
            self._blocks.append(_Block())
            return _Element(tag, "cell")
        if tag == "pre":
            blk.para()
            self._pre += 1
            return _Element(tag, "pre")
        if tag == "a":
            element = _Element(tag, "link", {"href": a.get("href"), "parts": []})
            self._anchors.append(element)
            return element
        if tag == "p":
            # Outlook's paragraphs have no margin: a line each, not a
            # paragraph each, or its mails come out double-spaced.
            tight = (any(name.startswith("mso") for name in classes)
                     or _NO_MARGIN_RE.search(_style_of(a)))
            blk.brk() if tight else blk.para()
            return _Element(tag, "block" if tight else "paragraph")
        if tag in _PARAGRAPH_TAGS:
            blk.para()
            return _Element(tag, "paragraph")
        if tag in _BREAK_TAGS:
            blk.brk()
            return _Element(tag, "block")
        return _Element(tag, "inline")

    def _void(self, tag: str, a: dict) -> None:
        blk = self._blk
        if tag == "br":
            blk.newline()
        elif tag == "hr":
            self._implied_end(tag)
            blk.para()
            blk.emit(_HR_TEXT)
            blk.gap = True
        elif tag == "img":
            if is_tracking_pixel(None, None, a):
                self.pixels += 1
                return
            alt = " ".join((a.get("alt") or "").translate(_INVISIBLE).split())
            if alt:
                # An emoji a client sent as an image reads as itself; any
                # other picture is named, so the text says one was there.
                bare = len(alt) <= 4 and not any(ch.isalnum() for ch in alt)
                self._write(alt if bare else "[%s]" % alt)

    def handle_endtag(self, tag):
        if self._overflow:
            if tag == self._overflow[0]:
                self._overflow[1] -= 1
                if not self._overflow[1]:
                    self._overflow = None
            return
        if tag in _VOID_TAGS:
            return
        structural = tag in _TABLE_TAGS
        for index in range(len(self._stack) - 1, -1, -1):
            element = self._stack[index]
            if element.tag == tag:
                self._pop_to(index)
                return
            # A stray </div> inside a cell must not close the table around it.
            if not structural and element.kind in ("cell", "table"):
                return

    def _close(self, element: _Element) -> None:
        kind = element.kind
        if kind == "inert" or kind == "inline":
            return
        if kind == "mute":
            self._inert -= 1
            return
        if kind == "link":
            self._anchors.remove(element)
            text = " ".join("".join(element.data["parts"]).split())
            note = _link_note(element.data["href"], text)
            if note:
                self._blk.text(" (%s)" % note)
            return
        if kind in ("quote", "attribution", "item", "cell"):
            lines = self._blocks.pop().done()
            blk = self._blk
            if kind == "quote":
                blk.para()
                blk.add(["> " + line if line else ">" for line in lines])
                blk.gap = True
            elif kind == "attribution":
                blk.add([_ATTR + line if line else line for line in lines])
            elif kind == "item":
                marker = element.data
                pad = " " * len(marker)
                blk.add([(marker if n == 0 else pad) + line if line else line
                         for n, line in enumerate(lines)])
            else:
                row = self._find(("tr",), ("table",))
                if row is not None and self._stack[row].kind == "row":
                    self._stack[row].data.append(lines)
                else:
                    blk.add(lines)
            return
        blk = self._blk
        if kind == "row":
            self._row(element.data)
        elif kind == "list":
            blk.brk() if element.data["nested"] else blk.para()
        elif kind == "pre":
            self._pre -= 1
            blk.para()
        elif kind in ("paragraph", "table"):
            blk.para()
        else:
            blk.brk()

    def _row(self, cells: list) -> None:
        """One table row. Cells of a line each share a line; a cell that is a
        block of its own (a layout table's column) stands as that block."""
        blk = self._blk
        cells = [cell for cell in cells if cell]
        if not cells:
            return
        if all(len(cell) == 1 for cell in cells):
            parts = [cells[0][0]]
            for cell in cells[1:]:
                # "Label:" in the cell before already separates.
                parts.append(" " if parts[-1][-1:] in ":：" else " | ")
                parts.append(cell[0])
            blk.add(["".join(parts)])
            return
        for cell in cells:
            blk.gap = True
            blk.add(cell)
        blk.gap = True

    def _write(self, data: str) -> None:
        if self._pre:
            self._blk.raw(data)
        else:
            self._blk.text(data)
        for anchor in self._anchors:
            anchor.data["parts"].append(data)

    def handle_data(self, data):
        if not self._inert and not self._overflow:
            self._write(data.translate(_INVISIBLE))

    def finish(self) -> list:
        self.close()
        self._pop_to(0)
        return self._blocks[0].done()


def _convert(html: str, marks: bool = False):
    """``(text, tracking pixels)`` of an HTML part."""
    converter = _TextConverter(marks)
    try:
        converter.feed(html)
        lines = converter.finish()
    except Exception:
        # A document that trips the parser still has words in it.
        lines = _html.unescape(re.sub(r"<[^<>]*>", " ", html)).split("\n")
    out = []
    for line in lines:
        line = line.replace("\xa0", " ").rstrip()
        out.append(line if line.strip(" \u200c\u200d\u2060") else "")
    return "\n".join(_squeeze(out)), converter.pixels


def html_to_text(html: str | None) -> str:
    """A faithful readable rendering of an HTML part.

    Block structure as line breaks, cited blockquotes as ``>`` lines, lists
    with markers, tables row by row, a link as ``text (url)`` only when the
    URL adds information, entities decoded; no script, style or head content,
    and nothing a reader would not see (``display:none``, preheader spans,
    zero-size boxes, tracking pixels).
    """
    if not isinstance(html, str) or not html.strip():
        return ""
    return _convert(html)[0]


# ── The sanitiser ────────────────────────────────────────────────────────────
#
# Allow-list throughout: a tag, an attribute, a URL scheme, a CSS function is
# emitted because it is on a list, never because it failed to be on one. The
# output is re-serialised from the token stream with every text node and
# attribute value escaped. Of the elements whose content a browser reads as
# raw text (script, style, textarea, title, xmp, …) it contains exactly one:
# the single <style> in the head, whose content is rebuilt from validated
# tokens and cannot hold a "<". So the browser's parse of the output cannot
# differ from the parse made here.

_SAFE_TAGS = frozenset((
    "a abbr address article aside b bdi bdo big blockquote br caption center "
    "cite code col colgroup dd del details dfn div dl dt em figcaption figure "
    "font footer h1 h2 h3 h4 h5 h6 header hr i img ins kbd li main mark nav "
    "ol p pre q s samp section small span strike strong sub summary sup "
    "table tbody td tfoot th thead time tr tt u ul var wbr").split())
_SAFE_VOID = frozenset(("br", "hr", "img", "wbr", "col"))
# Dropped with everything inside them. (Void offenders — embed, input, link,
# meta, base — are simply not on the allow-list.) A form's own tag is merely
# unwrapped, so the words of a form still read; its controls are gone.
_DROP_TAGS = frozenset((
    "script", "style", "head", "title", "iframe", "object", "applet", "svg",
    "math", "template", "select", "textarea", "button", "noembed",
    "noframes", "frameset", "xml", "audio", "video", "canvas", "datalist",
    "option", "optgroup", "map", "noscript"))
_SAFE_ATTRS = frozenset((
    "align", "valign", "bgcolor", "border", "cellpadding", "cellspacing",
    "color", "colspan", "rowspan", "dir", "face", "height", "width", "lang",
    "size", "title", "alt", "span", "start", "nowrap", "style", "role",
    # What the mail's own style rules select by. Inert without script.
    "class", "id"))
_SAFE_TAG_ATTRS = {
    "ol": frozenset(("type", "reversed")),
    "ul": frozenset(("type",)),
    "li": frozenset(("type", "value")),
    "body": frozenset(("text",)),
}
_SIZE_ATTR_RE = re.compile(r"\d{1,5}(?:\.\d+)?(?:%|px)?")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_CSS_PROPERTY_RE = re.compile(r"-?[a-z][a-z0-9-]*")
# No colon, backslash, angle bracket, brace, semicolon or at-sign can reach a
# value: no URL scheme, no CSS escape, no at-rule, no way out of a <style>.
_CSS_VALUE_RE = re.compile(r"[\w\s#%.,()\-+/'\"!*]*")
_CSS_FUNCTION_RE = re.compile(r"([\w-]*)\s*\(")
_CSS_FUNCTIONS = frozenset((
    "rgb", "rgba", "hsl", "hsla", "calc", "min", "max", "clamp",
    "linear-gradient", "radial-gradient", "repeating-linear-gradient",
    "repeating-radial-gradient"))
_CSS_BAD_WORDS = ("expression", "behavior", "binding", "script")
_CSS_BAD_PROPERTIES = frozenset(("behavior", "-moz-binding"))
# A selector, by the characters one is made of — none of which can open a
# tag, close a block or start an escape.
_CSS_SELECTOR_RE = re.compile(r"[\w\s.#:,>+~*\[\]=\"'()^$|%-]{1,500}")
_CSS_MEDIA_RE = re.compile(r"@media[\w\s(),:.%/-]{0,300}", re.I)
_CSS_BRACE_RE = re.compile(r"[{}]")
#: A mail's style sheets beyond this size are dropped whole.
_CSS_MAX_CHARS = 200_000
_HREF_RE = re.compile(r"(?:https?://[^\s/?#]|mailto:[^\s])", re.I)
# The schemes that run code, spelled so this file never holds either whole.
_SCRIPT_SCHEME_RE = re.compile("(java" "script|vb" "script)(\\s*):", re.I)

# The document the sanitised mail is served in. A mail is designed for a
# white page, whatever the dashboard's theme; images must not overflow a
# phone. The mail's own (cleaned) style rules follow these and may override.
_DOCUMENT_STYLE = (
    "html{color-scheme:light}"
    "body{margin:8px;background:#fff;color:#111;"
    "font-family:system-ui,sans-serif;overflow-wrap:anywhere}"
    "img{max-width:100%;height:auto}")


def _css_uncomment(css: str) -> str:
    """CSS without its comments; an unclosed one takes the rest with it.

    By hand rather than by pattern: a pattern rescans from every ``/*`` that
    has no end, which a sheet of nothing but ``/*`` turns into minutes.
    """
    if "/*" not in css:
        return css
    out = []
    position = 0
    while True:
        start = css.find("/*", position)
        if start < 0:
            out.append(css[position:])
            break
        out.append(css[position:start])
        end = css.find("*/", start + 2)
        if end < 0:
            break
        position = end + 2
    return " ".join(out)


def _clean_style(value: str) -> str:
    """Style declarations with everything that can load or run taken out.

    Each declaration is kept only if its property is a plain name and its
    value is made of harmless characters and allow-listed functions — so no
    ``url()``, ``image-set()``, ``expression()``, ``@import``, ``behavior``,
    no escape that could spell one of them. ``position: fixed`` goes too: a
    mail has no business pinning itself over its own text.
    """
    kept = []
    for declaration in _css_uncomment(value).split(";"):
        prop, colon, val = declaration.partition(":")
        prop = prop.strip().lower()
        val = " ".join(val.split())
        if (not colon or not val or len(val) > 300
                or not _CSS_PROPERTY_RE.fullmatch(prop)
                or prop in _CSS_BAD_PROPERTIES
                or not _CSS_VALUE_RE.fullmatch(val)):
            continue
        low = val.lower()
        if any(word in low for word in _CSS_BAD_WORDS):
            continue
        if prop == "position" and ("fixed" in low or "sticky" in low):
            continue
        if any(name.lower() not in _CSS_FUNCTIONS
               for name in _CSS_FUNCTION_RE.findall(val)):
            continue
        kept.append("%s:%s" % (prop, val))
    return ";".join(kept)


def _css_block_end(css: str, brace: int) -> int:
    """Index of the ``}`` that closes the block opened at `brace` (or the end)."""
    depth = 0
    for match in _CSS_BRACE_RE.finditer(css, brace):
        depth += 1 if match.group() == "{" else -1
        if not depth:
            return match.start()
    return len(css)


def _css_rules(css: str, out: list, nested: bool) -> None:
    position = 0
    while True:
        brace = css.find("{", position)
        if brace < 0:
            return
        # Whatever statement stands before the rule (@import …; @charset …;)
        # ends in a semicolon and is not carried over.
        prelude = " ".join(css[position:brace].rsplit(";", 1)[-1].split())
        close = _css_block_end(css, brace)
        body = css[brace + 1:close]
        if prelude.startswith("@"):
            # @media is the one at-rule a mail needs (its phone layout); one
            # level of it. @font-face, @import, @keyframes and the rest go.
            if not nested and _CSS_MEDIA_RE.fullmatch(prelude):
                inner: list = []
                _css_rules(body, inner, True)
                if inner:
                    out.append("%s{%s}" % (prelude, "".join(inner)))
        elif "{" not in body and _CSS_SELECTOR_RE.fullmatch(prelude):
            declarations = _clean_style(body)
            if declarations:
                out.append("%s{%s}" % (prelude, declarations))
        position = close + 1


def _clean_stylesheet(css: str) -> str:
    """A mail's ``<style>`` content, rebuilt from the rules that are harmless.

    Not filtered but rebuilt: every rule emitted is a selector made of
    selector characters, and declarations that passed :func:`_clean_style`.
    Nothing else of the sheet survives — so the result holds no ``<`` (it
    cannot end the ``<style>`` element it is written into), no ``url()``, no
    ``@import``, no ``@font-face``. What does survive is what makes a mail
    look like itself on a phone: its classes and its ``@media`` rules.
    """
    if len(css) > _CSS_MAX_CHARS:
        return ""
    css = _css_uncomment(css)
    css = css.replace("<!--", " ").replace("-->", " ")   # Outlook's wrapping
    out: list = []
    _css_rules(css, out, False)
    sheet = "".join(out)
    return "" if "<" in sheet else sheet


def _safe_href(value: str | None) -> str | None:
    """The link target if it is http(s) or mailto, else None.

    Judged the way a browser reads it: surrounding blanks and controls
    stripped, tabs and line breaks inside removed — the classic ways to hide
    a scheme from a filter.
    """
    if not value:
        return None
    url = re.sub(r"[\t\r\n]", "", value).strip("".join(map(chr, range(33))))
    if not url or _CONTROL_RE.search(url) or not _HREF_RE.match(url):
        return None
    return url.replace(" ", "%20")


def _cid_source(value: str | None, cid_map) -> str | None:
    """The ``data:`` URL of a ``cid:`` image, when the mail carries it."""
    if not value or not cid_map:
        return None
    value = value.strip()
    if value[:4].lower() != "cid:":
        return None
    wanted = unquote(value[4:]).strip().strip("<>")
    source = cid_map.get(wanted)
    if source is None:
        for key, candidate in cid_map.items():
            if str(key).strip().strip("<>").lower() == wanted.lower():
                source = candidate
                break
    if (not isinstance(source, str)
            or not re.match(r"data:image/[\w.+-]+[;,]", source, re.I)
            or _CONTROL_RE.search(source) or re.search(r"[\s\"'<>]", source)):
        return None
    return source


class _Sanitizer(HTMLParser):
    def __init__(self, cid_map=None):
        super().__init__(convert_charrefs=True)
        self._cids = cid_map
        self._out: list = []
        self._open: list = []       # emitted and not yet closed
        # The outermost open element whose content is dropped, and how many
        # of its own tag are open inside it. Leaving the state early only
        # ever hands more markup to the allow-lists, never past them.
        self._drop = None
        self._drop_depth = 0
        self._seen_body = False
        self._in_style = False
        self.css: list = []         # the mail's <style> contents, raw
        self.body_attrs = ""

    def _attrs(self, tag: str, attrs) -> str:
        allowed = _SAFE_TAG_ATTRS.get(tag, frozenset())
        seen = set()
        out = []
        for name, value in attrs:
            if name in seen or (name not in _SAFE_ATTRS and name not in allowed):
                continue
            seen.add(name)
            value = _CONTROL_RE.sub("", value or "")[:2000]
            if name == "style":
                value = _clean_style(value)
                if not value:
                    continue
            elif name in ("width", "height"):
                value = value.strip()
                if not _SIZE_ATTR_RE.fullmatch(value):
                    continue
            out.append(' %s="%s"' % (name, _html.escape(value, quote=True)))
        return "".join(out)

    def handle_startendtag(self, tag, attrs):
        if tag in ("svg", "math") or ":" in tag:
            return
        self.handle_starttag(tag, attrs)

    def handle_starttag(self, tag, attrs):
        if tag == "style" and self._drop in (None, "head"):
            self._in_style = True   # its text is collected, never emitted
        if tag == "body":
            # An unclosed head ends where the body starts; a body tag inside
            # anything else that is being dropped is part of what is dropped.
            if self._drop not in (None, "head"):
                return
            self._drop = None
            if not self._seen_body:
                self._seen_body = True
                self.body_attrs = self._attrs("body", attrs)
            return
        if self._drop == "head" and tag not in _HEAD_TAGS and ":" not in tag:
            self._drop = None       # … or where the first body element does
        if self._drop:
            if tag == self._drop:
                self._drop_depth += 1
            return
        if tag in _DROP_TAGS:
            self._drop, self._drop_depth = tag, 1
            return
        if tag not in _SAFE_TAGS:
            return                  # unwrapped: the tag goes, its content stays
        if tag not in _SAFE_VOID and len(self._open) >= _MAX_DEPTH:
            return                  # deeper than any mail nests: unwrapped too
        extra = ""
        if tag == "img":
            if is_tracking_pixel(None, None, attrs):
                return
            source = _cid_source(dict(attrs).get("src"), self._cids)
            if source:
                extra = ' src="%s"' % _html.escape(source, quote=True)
        elif tag == "a":
            href = _safe_href(dict(attrs).get("href"))
            if href:
                extra = (' href="%s" target="_blank" rel="noopener noreferrer"'
                         % _html.escape(href, quote=True))
        self._out.append("<%s%s%s>" % (tag, extra, self._attrs(tag, attrs)))
        if tag not in _SAFE_VOID:
            self._open.append(tag)

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False
        if self._drop:
            if tag == self._drop:
                self._drop_depth -= 1
                if not self._drop_depth:
                    self._drop = None
            return
        if tag in _SAFE_VOID or tag not in _SAFE_TAGS:
            return
        structural = tag in _TABLE_TAGS
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index] == tag:
                while len(self._open) > index:
                    self._out.append("</%s>" % self._open.pop())
                return
            # A stray </div> inside a cell must not close the table around it.
            if not structural and self._open[index] in ("td", "th", "table"):
                return

    def handle_data(self, data):
        if self._in_style:
            self.css.append(data)
        if not self._drop:
            self._out.append(_html.escape(_CONTROL_RE.sub("", data), quote=False))

    def finish(self) -> str:
        self.close()
        while self._open:
            self._out.append("</%s>" % self._open.pop())
        return "".join(self._out)


def sanitize_html(html: str | None, cid_map: dict | None = None) -> str:
    """The HTML part as a complete document that is safe to show.

    For the *Original* view, served under ``sandbox; default-src 'none';
    img-src data:; style-src 'unsafe-inline'`` — and safe without that
    policy too, which is the point of doing both.

    Kept: the allow-listed formatting and table tags, their presentational
    attributes, inline styles and the mail's style rules with nothing in
    them that loads or runs (:func:`_clean_style`,
    :func:`_clean_stylesheet`), links to http(s) and mailto targets — each
    forced to open in a new tab without an opener — and images whose ``src``
    is a ``cid:`` reference that `cid_map` (content-id → ``data:`` URL)
    resolves.

    Gone: script and link elements, every ``on*`` attribute, forms' controls,
    frames, objects and embeds, ``base``, ``meta`` (so no refresh), SVG and
    MathML, comments (so no conditional markup), ``url()`` and every at-rule
    but ``@media`` in the styles, every other ``src`` — remote, ``data:``,
    anything — and tracking pixels altogether. Nothing in the output can make
    a request.

    The answer is a whole document (doctype, charset, one ``<style>`` holding
    a minimal base style and the mail's cleaned rules, the mail's own
    ``<body>`` attributes where harmless), so the caller serves it as it is.
    """
    sanitizer = _Sanitizer(cid_map)
    try:
        sanitizer.feed(html if isinstance(html, str) else "")
        body = sanitizer.finish()
        style = _clean_stylesheet("\n".join(sanitizer.css))
    except Exception:
        # Whatever tripped the parser, its text is safe once escaped.
        sanitizer.body_attrs = style = ""
        body = "<pre>%s</pre>" % _html.escape(html_to_text(html), quote=False)
    document = (
        '<!doctype html>\n<html><head><meta charset="utf-8"><style>%s%s</style>'
        "</head><body%s>%s</body></html>"
        % (_DOCUMENT_STYLE, style, sanitizer.body_attrs, body))
    # Belt over the allow-lists: the scheme can only be left as words in the
    # text by now, and there its colon is written as an entity — the reader
    # sees the same, and the literal scheme never leaves this function.
    return _SCRIPT_SCHEME_RE.sub(r"\1\2&#58;", document)
