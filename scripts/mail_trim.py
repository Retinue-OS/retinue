"""The deterministic mail trim: a mail reduced to its own new text.

A mail chat shows each mail like a message, so the bubble wants what this mail
*adds* — not the thread it drags along, not the sender's signature block, not
the legal notice their server glued on. This module is that reduction, plus
the two HTML services the mail surface needs beside it:

  * :func:`trim` — ``(plain, html, subject=None, *, is_reply=None,
    bulk=None) -> TrimResult(text, state, removed)``.
  * :func:`html_to_text` — a readable plain rendering of an HTML part.
  * :func:`sanitize_html` — the HTML part made safe for the *Original* view.
  * :func:`is_tracking_pixel` — the size test the gateway applies to images.

Pure and stdlib-only (``html.parser``, ``re``): no I/O, no network, importable
in a slim gateway image, and never raising on a malformed mail — a parser that
trips yields the untrimmed text, not an exception.

What the trim removes, in this order
====================================

1. **Quoted history** — ``>`` blocks with their attribution line; and, in a
   mail known to be a reply, Outlook's "-----Original Message-----"
   separator, the ``From:/Sent:/To:/Subject:`` header block and (when the
   text comes from HTML) the containers the clients write around them.
2. **Signatures** — the ``-- `` delimiter and the mobile "Sent from my …" line.
3. **Footers and disclaimers** — confidentiality notices; and, in bulk mail,
   unsubscribe blocks and mailing-list footers. At the END of the mail only.
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
"forwarded" rather than "original", which subject prefix is a reply's, what
"Sent from my" is in Danish — they live in the data tables below, keyed by
language, covering English, German, French, Italian, Spanish, Dutch,
Portuguese, Danish, Norwegian, Swedish, Finnish, Polish and Czech (the
subject prefixes a dozen more). No language is privileged: every table is
folded (case and diacritics) and consulted as one set. Extending a language
is adding a row.

**A language outside the tables fails safe.** Wherever a word decides, it
decides for cutting: the word must be there for text to go. A mail in a
language no table knows keeps what a table would have removed, and its state
says ``wanting``. No table is ever the reason text is lost.

Rules that outrank recall
=========================

**Never cut what the author wrote.** Every rule here is tuned to miss rather
than to over-reach, and where a shape alone cannot tell a quote, a signature
or a footer from the message, the rule asks for evidence from outside the
text — and without it leaves the text in and the state ``wanting``:

  * *Interleaved replies.* Only the quoted lines go. Text between, above and
    below quote blocks stays, so top-, bottom- and inline-posting all keep the
    author's lines.
  * *A lead-in is not an attribution.* "As you said:" before a quote has no
    address and no date, so it stays; only the quote under it goes.
  * *History without ``>`` needs a reply.* A separator, a header block, an
    attribution above bare text, Outlook's ``#divRplyFwdMsg``: the same shapes
    are the CONTENT of a form mailer's notification, a helpdesk update, a
    memo, a mail pasted to ask about it — and of a forward. They are cut, to
    the end, only when the caller says the mail cites an earlier one
    (``is_reply``) and its subject carries a reply's prefix. Either alone is
    not enough: Exchange puts In-Reply-To on forwards too, and "Re:" is also
    how people head a new letter.
  * *A header block alone needs to be the whole block.* With no separator
    above it, it must open with From or Sent, name a To and a Subject, and
    carry a time of day: "From: Zurich / To: Bern / Date: …" is an itinerary,
    "From / Date / Subject / Phone" a web form.
  * *An attribution above bare text* (some clients quote without ``>``) must
    also carry the verb, end on the verb, the address or the verb and the
    name, and open its paragraph — a sentence that happens to hold a date and
    "wrote" does not.
  * *A forward's body is the content.* A forward marker ("---------- Forwarded
    message ---------", "Begin forwarded message:", and their kin) ends the
    trimming: the intro above it is trimmed like any text, the forwarded
    message below is kept whole (Apple Mail's ``>`` quoting of it removed). An
    Outlook forward carries no marker of its own and looks exactly like a
    reply; a ``Fwd:``-style prefix on the subject settles it, in which case a
    plain marker line is written above the kept message so the result reads
    as a forward. With a prefix no table knows, it is kept as it stands.
  * *A signature is under the standard's delimiter, once.* ``-- `` with its
    trailing blank, the only one within reach of the end. Two within reach
    are dividers between sections; a bare ``--`` is as often a divider someone
    typed; and a delimiter with nothing above it has the whole mail under
    it, which then is the message. A phone's line is the mail's last
    paragraph, short and without a clause, and beside the table's words it
    holds the name of the sender's device or of a product, and names only —
    "Sent from the warehouse this morning: 14 boxes." is the message, and so
    is "Sent from my iPhone to yours yesterday".
  * *A legal notice is what the mail ends on, under the sign-off.* The
    paragraph names the mail itself, confidentiality and a recipient, opens
    on two of them, has nothing but more notices after it, and stands under
    a line that closes the author's text. A paragraph about a confidential
    document with "Kind regards" still to come is the message.
  * *An unsubscribe block needs bulk mail.* "Please unsubscribe me, my
    address is …" is a request, and nothing in its shape tells it from a
    footer: unsubscribe blocks and list footers go only when the caller says
    the mail's headers call it bulk (``bulk``). A person's post that a list
    passed on is bulk mail too, so the block must also have the shape
    software gives it: it ends on the link or the list server's address it
    hands over, stands under a message, and has nothing under it but more
    of its kind and the sender's imprint.
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
comments a sender wrote *into* such a history are lost with it — as is what
stands under a client's whole header block that the author of a reply pasted
in; in bulk mail a last paragraph that names unsubscribing and ends on its
link is taken for the footer it all but always is; a quoted line a client
re-wrapped leaves its overflow word behind as if the author wrote it; a
signature without the ``-- `` delimiter is the author's sign-off and stays.

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

Of an HTML part the first ``MAX_HTML_CHARS`` characters of markup are read
(see "Cost") — not counting what is never text: the payload of an image
written into the part as a ``data:`` URL. Beside a plain part a longer one
is not consulted. Where it is the source, the text ends where the reading
did — in the middle of a paragraph, if that is where the bound falls — on a
line of its own that says so (``CUT_MARK``), and what stands above that line
is not taken for the end of the mail: quotes go, a signature or a footer is
not looked for. The whole of the part is the caller's to show.

State
=====

``deterministic`` unless the trim could not settle, which is ``wanting`` — a
model trim is due on first view: the mail is HTML-only (or its plain part a
stub), or more than ``WANTING_CHARS`` remain (``MAIL_TRIM_WANTING_CHARS``,
default 2500), or the mail is more than the trim works on (see "Cost"), or
an HTML part was read that was not read to its end, or a quote marker
survived, or a rule saw its shape and held back for want of evidence
(history in a mail not known to be a reply, an unsubscribe block in mail not
known to be bulk, a bare ``--``, a notice with no sign-off above it, an
attribution in a language without a verb here).

Idempotence
===========

``trim(trim(x).text, None).text == trim(x).text``. The pipeline runs to a
fixpoint inside one call, and whatever it keeps is kept in a form the next
call recognises the same way — a call with no evidence cuts nothing that a
call with evidence kept. A text built never to settle gets no trim at all,
and neither does one that is more than the trim works on: the same answer
however often it is asked for.

Cost
====

Bounded, whatever a mail holds — it is anyone's to write, and the caller
has one thread for all of them. But a bound is on what a mail can cost, not
on how long it may be: a short reply above megabytes of thread, a log sent
in answer, a digest are ordinary mail, and are trimmed like any.

  * A source of more than ``MAX_SOURCE_LINES`` lines or ``MAX_SOURCE_CHARS``
    characters gets no trim: it is answered as it came, ``wanting``, and no
    line of it is looked at.
  * The pipeline runs over a source at most ``_MAX_PASSES`` times, and one
    call reads a mail at most four ways (the plain part; the HTML part with
    what the markup knows, and without; the text alone, when that left
    nothing). After a reading that did not settle, only the last is still
    tried.
  * A pass skips the lines it finds to be quoted history, or cuts them from
    one line on to the end, at next to no cost. Every other line it reads
    closely, and one reading reads at most ``MAX_READ_LINES`` of them, with
    ``MAX_READ_CHARS`` characters, counted once for each pass. That is all
    the passes for a text of its author's that is long, and the two a mail
    as clients write it takes — one that trims, one that finds nothing more
    to trim — for one twice as long. A reading that would read more is
    given up like one that does not settle.
  * Of an HTML part ``MAX_HTML_CHARS`` characters of markup are parsed, and
    of an image written into it no more than the tag.

Within the bounds, linear, for hostile mail too. Every look-ahead and
look-back is bounded by a count of lines AND by the length an attribution or
a footer can have, so one long line is never read once per short line beside
it; a judgement that came out "no" is not asked again of the same lines; the
stages that repeat work only on the end of the text; element nesting is
tracked through an index, not by walking a stack; and what quoting and
listing add to the text made from HTML — the prefixes — is bounded by the
size of the markup, whatever a mail nests. (:func:`html_to_text` and
:func:`sanitize_html` read the whole of what they are given, once.)
"""
from __future__ import annotations

import html as _html
import os
import re
import unicodedata
from bisect import bisect_left, bisect_right
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

#: What one call works on is bounded, whatever a mail holds (see "Cost" in the
#: module docstring). A source with more lines or more characters than this
#: is not read at all: it is answered as it came, state ``wanting``. Five
#: megabytes are a thread of several thousand replies, quoted in full. Read
#: at call time, like the bounds below.
MAX_SOURCE_LINES = 200_000
MAX_SOURCE_CHARS = 5_000_000

#: Of a source within that, the lines that are quoted history are skipped,
#: and cost next to nothing. Every other line is read closely, and of those
#: one reading reads at most this many, with this many characters — counted
#: once for each pass that reads them. So 12,500 lines of the author's own
#: (a megabyte of them) can have all the passes there are, and 25,000 lines
#: (two megabytes) the two that a mail as clients write it takes: one that
#: trims it, one that confirms it. A reading that would take more is given
#: up, and the mail gets no trim.
MAX_READ_LINES = 50_000
MAX_READ_CHARS = 4_000_000

#: Of an HTML part, this much markup is read (what does not count as markup:
#: see _markup_read). Where a longer one is the source, the text ends where
#: the reading did, on a line that says so — and under which no signature
#: and no footer is looked for, in this call or in a later one: the end of
#: what was read is not the end of the mail.
MAX_HTML_CHARS = 1_000_000
CUT_MARK = "[…]"

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
    if text.isascii():
        return text.casefold()      # nothing to decompose: most lines
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
    "de": ("schrieb", "geschrieben"),
    "fr": ("a écrit",),
    "it": ("ha scritto",),
    "es": ("escribió",),
    "nl": ("schreef", "geschreven"),
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

# The subject prefix a client puts on a forward, and the one it puts on a
# reply. Only the outermost prefix counts: "Re: Fwd: x" is a reply. A prefix
# that is a forward's in one language and a reply's in another ("VS": Danish
# Videresendt, Finnish Vastaus) says neither.
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
    "cs": ("FW", "Př"),
    "tr": ("İLT",),
    "id": ("TRS",),
    "cy": ("YML",),
    "el": ("ΠΡΘ",),
    "ru": ("Пересл",),
    "he": ("העבר",),
    "zh": ("转发", "轉寄", "轉發"),
}
_REPLY_PREFIXES = {
    "en": ("Re",),
    "de": ("AW",),
    "fr": ("RE", "Réf"),
    "it": ("R", "RIF"),
    "es": ("RE",),
    "nl": ("Antw",),
    "pt": ("RES",),
    "da": ("SV",),
    "nb": ("SV",),
    "sv": ("SV",),
    "fi": ("VS",),
    "pl": ("Odp",),
    "cs": ("Odp",),
    "tr": ("YNT",),
    "id": ("BLS",),
    "lv": ("ATB",),
    "lt": ("ATS",),
    "el": ("ΑΠ", "ΣΧΕΤ"),
    "ru": ("Отв",),
    "he": ("השב",),
    "zh": ("回复", "回覆", "答复"),
}

# The line a phone or an app signs with, as the line STARTS; `*` stands for a
# short run of anything (the device's name). These are how little but a
# signing line starts ("Sent from my …", "Get Outlook for …") …
_MOBILE_SIGNATURES = {
    "en": ("Sent from my", "Get Outlook for",
           "Sent with Proton Mail secure email"),
    "de": ("Von meinem * gesendet", "Gesendet von meinem",
           "Diese Nachricht wurde von meinem * gesendet",
           "Outlook für * beziehen", "Outlook für * herunterladen"),
    "fr": ("Envoyé de mon", "Envoyé depuis mon", "Envoyé à partir de mon",
           "Télécharger Outlook pour", "Obtenir Outlook pour"),
    "it": ("Inviato dal mio", "Inviato dalla mia", "Scarica Outlook per"),
    "es": ("Enviado desde mi", "Obtener Outlook para"),
    "nl": ("Verzonden vanaf mijn", "Verstuurd vanaf mijn",
           "Outlook voor * downloaden"),
    "pt": ("Enviado do meu", "Enviado a partir do meu",
           "Obter o Outlook para"),
    "da": ("Sendt fra min", "Hent Outlook til"),
    "nb": ("Sendt fra min", "Skaff deg Outlook for"),
    "sv": ("Skickat från min", "Hämta Outlook för"),
    "pl": ("Wysłane z mojego", "Wysłano z mojego"),
    "cs": ("Odesláno z mého",),
}
# … and these are how any sentence about a shipment starts too ("Sent from
# the warehouse"). Neither signs a mail by its words alone: the line must
# also name what sent it (below) — "Sent from my office." is an answer.
_MOBILE_PARTICIPLES = {
    "en": ("Sent from", "Sent with", "Sent via", "Sent using"),
    "de": ("Gesendet von", "Gesendet mit", "Gesendet über"),
    "fr": ("Envoyé de", "Envoyé depuis", "Envoyé à partir de", "Envoyé avec"),
    "it": ("Inviato da", "Inviato dal", "Inviato dall", "Inviato con"),
    "es": ("Enviado desde", "Enviado con"),
    "nl": ("Verzonden vanaf", "Verstuurd vanaf", "Verzonden met",
           "Verstuurd met"),
    "pt": ("Enviado do", "Enviado a partir do", "Enviado com"),
    "da": ("Sendt fra", "Sendt med"),
    "nb": ("Sendt fra", "Sendt med"),
    "sv": ("Skickat från", "Skickat med"),
    "fi": ("Lähetetty",),
    "pl": ("Wysłane z", "Wysłano z"),
    "cs": ("Odesláno z",),
}
# What sent it. Names, not words of a language: the devices and the mail apps
# that sign. Matched as the start of a word, so "iPhonesta" (Finnish) is an
# iPhone …
_MOBILE_PRODUCTS = (
    "iphone", "ipad", "ipod", "ios", "android", "galaxy", "samsung", "huawei",
    "xiaomi", "nokia", "blackberry", "windows", "outlook", "yahoo", "proton",
    "gmx", "web.de", "gmail", "aol", "thunderbird", "mailbird", "superhuman",
    "tutanota", "zoho", "fastmail", "mail.ru", "pixel", "oneplus", "fairphone",
    "motorola", "sony", "xperia", "oppo", "honor", "lumia")
# … and the words for a device, where the line names no make ("Sent from my
# mobile device"). Stems, cut where the language inflects.
_MOBILE_DEVICES = {
    "en": ("phone", "smartphone", "mobile", "device", "tablet", "handheld"),
    "de": ("handy", "telefon", "mobiltelefon", "smartphone", "tablet",
           "gerät", "mobilgerät"),
    "fr": ("téléphone", "mobile", "portable", "appareil", "smartphone",
           "tablette"),
    "it": ("telefono", "cellulare", "dispositivo", "smartphone", "tablet"),
    "es": ("teléfono", "móvil", "celular", "dispositivo", "smartphone",
           "tableta"),
    "nl": ("telefoon", "mobiel", "toestel", "smartphone", "tablet"),
    "pt": ("telemóvel", "telefone", "celular", "dispositivo", "smartphone",
           "tablet"),
    "da": ("telefon", "mobil", "enhed", "smartphone", "tablet"),
    "nb": ("telefon", "mobil", "enhet", "smarttelefon", "nettbrett"),
    "sv": ("telefon", "mobil", "enhet", "smartphone", "surfplatta"),
    "fi": ("puhelim", "älypuhelim", "laitte", "tablet"),
    "pl": ("telefon", "smartfon", "urządzeni", "tablet"),
    "cs": ("telefon", "mobil", "zařízení", "tablet"),
}
# The word that joins two names on a signing line ("Yahoo Mail on Android",
# "Mail für Windows", "mit der GMX Mail App"). It counts only right before a
# name of the two tables above: "on Monday" and "for Bob" make a sentence.
_MOBILE_JOINERS = {
    "en": ("for", "on", "the"),
    "de": ("für", "auf", "mit", "der", "dem"),
    "fr": ("pour", "sur", "le", "la"),
    "it": ("per", "su", "il", "la"),
    "es": ("para", "en", "el", "la"),
    "nl": ("voor", "op", "de", "het"),
    "pt": ("para", "no", "o", "a"),
    "da": ("til", "på", "for"),
    "nb": ("for", "på", "til"),
    "sv": ("för", "på", "till"),
    "pl": ("dla", "na"),
    "cs": ("pro", "na"),
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

# A legal notice names ALL FOUR of these: the mail itself, that it is
# confidential, whom it is meant for, and what a reader it was not meant for
# is to do. Fewer are a sentence of the author's ("the papers are
# confidential, you are the only recipient"; "please treat this mail as
# confidential, the recipient of the parcel does not know yet").
_MESSAGE_STEMS = {
    "en": ("e-mail", "email", "mail", "message", "communication",
           "transmission"),
    "de": ("e-mail", "mail", "nachricht", "mitteilung"),
    "fr": ("message", "courriel", "e-mail", "mail", "courrier"),
    "it": ("messaggio", "e-mail", "mail", "comunicazione"),
    "es": ("mensaje", "correo", "e-mail", "comunicación"),
    "nl": ("bericht", "e-mail", "mail"),
    "pt": ("mensagem", "e-mail", "correio", "comunicação"),
    "da": ("e-mail", "mail", "meddelelse", "besked"),
    "nb": ("e-post", "melding", "e-mail"),
    "sv": ("meddelande", "e-post", "mejl", "mail"),
    "fi": ("viesti", "sähköposti"),
    "pl": ("wiadomość", "e-mail", "mail"),
    "cs": ("zpráva", "e-mail", "mail"),
}
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
# "… received it in error … notify the sender … delete it."
_MISDIRECTED_STEMS = {
    "en": ("in error", "notify", "sender", "delete", "destroy"),
    "de": ("irrtümlich", "versehentlich", "absender", "lösch", "vernicht"),
    "fr": ("par erreur", "expéditeur", "supprim", "détrui"),
    "it": ("per errore", "mittente", "cancell", "elimin", "distrugg"),
    "es": ("por error", "remitente", "elimin", "borr", "destru"),
    "nl": ("ten onrechte", "abusievelijk", "per vergissing", "afzender",
           "verwijder", "vernietig"),
    "pt": ("por engano", "por erro", "remetente", "elimin", "apag", "destru"),
    "da": ("ved en fejl", "fejlagtigt", "afsender", "slet"),
    "nb": ("ved en feil", "feilaktig", "avsender", "slett"),
    "sv": ("av misstag", "avsändare", "radera", "förstör"),
    "fi": ("vahingossa", "erehdyksessä", "lähettäjä", "poista", "tuhoa"),
    "pl": ("omyłkowo", "pomyłkowo", "nadawc", "usuń", "usun", "zniszcz"),
    "cs": ("omylem", "odesílatel", "smažte", "vymažte", "odstraňte", "zničte"),
}


def _stems(table: dict) -> tuple:
    return tuple(sorted({_fold(word) for words in table.values() for word in words}))


def _alternation(stems) -> str:
    # Longest first, so "e-mail" is tried before "e-" could end a shorter one.
    return "|".join(re.escape(stem).replace(r"\ ", r"\s+")
                    for stem in sorted(stems, key=len, reverse=True))


def _words_re(table: dict) -> "re.Pattern":
    """The table's entries as whole words."""
    return re.compile(r"(?<!\w)(?:%s)(?!\w)" % _alternation(_stems(table)))


def _stems_re(table: dict) -> "re.Pattern":
    """The table's entries as the START of a word: a stem is cut where its
    language starts to inflect ("confidenci", "afmeld"), but it never begins
    in the middle of another word ("Paketempfänger" is not an "Empfänger")."""
    return re.compile(r"(?<!\w)(?:%s)" % _alternation(_stems(table)))


def _subject_re(prefixes) -> "re.Pattern":
    """A subject whose outermost prefix (list tags aside) is one of these —
    counted ("Re[2]:"), abbreviated with a dot, with any colon and spacing."""
    return re.compile(
        r"^\s*(?:\[[^\]]{0,60}\]\s*)*(?:%s)\s*(?:\[\d+\]|\(\d+\)|\^\d+)?"
        r"\.?\s*[:：]" % _alternation(prefixes))


def _starts_re(table: dict) -> "re.Pattern":
    # What stands for an entry's `*` is captured: it is the sender's text.
    return re.compile(r"^\W{0,3}(?:%s)(?!\w)" % "|".join(
        "(.{1,40}?)".join(re.escape(part).replace(r"\ ", r"\s+")
                          for part in entry.split("*"))
        for entry in _stems(table)))


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
_UNSUBSCRIBE_RE = _stems_re(_UNSUBSCRIBE_STEMS)
_MESSAGE_RE = _stems_re(_MESSAGE_STEMS)
_CONFIDENTIAL_RE = _stems_re(_CONFIDENTIAL_STEMS)
_RECIPIENT_RE = _stems_re(_RECIPIENT_STEMS)
_MISDIRECTED_RE = _stems_re(_MISDIRECTED_STEMS)
_AMBIGUOUS_PREFIXES = set(_stems(_FORWARD_PREFIXES)) & set(_stems(_REPLY_PREFIXES))
_FORWARD_SUBJECT_RE = _subject_re(
    set(_stems(_FORWARD_PREFIXES)) - _AMBIGUOUS_PREFIXES)
_REPLY_SUBJECT_RE = _subject_re(
    set(_stems(_REPLY_PREFIXES)) - _AMBIGUOUS_PREFIXES)
_MOBILE_OWN_RE = _starts_re(_MOBILE_SIGNATURES)
_MOBILE_SENT_RE = _starts_re(_MOBILE_PARTICIPLES)
_MOBILE_NAME_RE = re.compile(r"(?<!\w)(?:%s)" % _alternation(
    _MOBILE_PRODUCTS + _stems(_MOBILE_DEVICES)))
_MOBILE_JOINER = frozenset(_stems(_MOBILE_JOINERS))


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


def _no_marks(text: str) -> str:
    """The text without a mark in it. (Not taken out by a pattern, which
    stops at every mark it finds: a part can be fifty megabytes of them, and
    this is done to all of a part, whatever else is.)"""
    return text.replace(_ATTR, "").replace(_BOUNDARY, "")


# Up to three spaces of indent: four make a code block, and code that starts
# with ">" is not a quote.
_QUOTE_RE = re.compile(r"^ {0,3}>")
# Every quote level a line starts with, each as `_unquote` takes one off.
_QUOTE_LEVELS_RE = re.compile(r"(?: {0,3}> ?)*")
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
# The time of day on a quoted mail's Sent line; also "14.02", which in a
# sentence could as well be a date and is therefore not in _TIME_RE.
_CLOCK_RE = re.compile(
    _TIME_RE.pattern + r"|(?<![\d.])[0-2]?\d\.[0-5]\d(?![\d.])", re.I)
# No attribution is longer than this, folded lines joined.
_ATTRIBUTION_MAX = 320
_URL_RE = re.compile(r"https?://\S+|www\.\S+|mailto:\S+", re.I)
# The link a client hangs on its app's line ("Get Outlook for iOS<https://…>").
_HUNG_LINK_RE = re.compile(r"[<(\[]?\s*https?://\S+", re.I)
# What a sentence has and a signing line has not: a clause, a second
# sentence, a question, a number worth writing down.
_PROSE_RE = re.compile(r"[,;:?!]|\.\s+\S|\d{3}")
# A line that is nothing but a link, or nothing but an address.
_BARE_URL_RE = re.compile(r"<?(?:https?://\S+|www\.\S+)>?", re.I)
_BARE_ADDRESS_RE = re.compile(r"<?[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+>?")
# The names a list server gives the address that takes a reader off the list
# ("dev+unsubscribe@…", "news-leave@…"): a protocol's, not a language's.
_LIST_COMMAND_RE = re.compile(
    r"unsub|leave|signoff|sign-off|optout|opt-out|remove")
# … and to the place the list lives at ("lists.example.org/mailman/listinfo").
_LIST_PLACE_RE = re.compile(r"list|mailman|group|sympa", re.I)
# What stands under an unsubscribe block is the sender's imprint — a postal
# address, a copyright line, more links — and has a number, a link or a sign
# in it. Plain words under the block are the author's: a sign-off, the next
# sentence. So are the steps of a list ("1. log in").
_IMPRINT_RE = re.compile(r"\d|[©®™|•·@]|https?://|www\.", re.I)
_LIST_ITEM_RE = re.compile(r"\s*(?:\d{1,3}[.)]|[-*])\s")
# What may still stand after the link a footer hands over.
_LINK_CLOSERS = " .)>]\"'»"
# "Label: value". The colon must end the line or be followed by whitespace,
# which is what keeps "https://…" and "12:30" from being labels.
_LABEL_RE = re.compile(
    r"^\s*[*_]{0,2}([^\W\d_][^:：*\n]{0,40}?)[*_]{0,2}\s?[:：][*_]{0,2}"
    r"(?:[ \t\xa0]+(.*))?$")
_RULER_RE = re.compile(r"^\s*[-_=*~]{5,}\s*$")
_LIST_RULER_RE = re.compile(r"^_{16,}$")


def _dashed(line: str) -> str | None:
    """The words of "----- Some words -----", or None: the shape of every
    client's separator line, on a line with no blanks around it.

    What ``^[-_=]{3,}\\s*([^\\W_].{1,60}?)\\s*[-_=]{3,}$`` captures — found
    by hand, because the pattern reads the dashes of a long line once for
    every length the words could have.
    """
    body = line.lstrip("-_=")
    if len(line) - len(body) < 3:
        return None
    start = len(line) - len(body.lstrip())
    if start == len(line) or not line[start].isalnum():
        return None
    words = line.rstrip("-_=")
    if len(line) - len(words) < 3:
        return None
    # Two characters at least, to where the blanks before the closing dashes
    # begin — and three of the dashes are left after them.
    end = max(start + 2, len(words.rstrip()))
    if end > len(line) - 3 or end - start > 61:
        return None
    return line[start:end]


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
        text = _no_marks(text)
    return _squeeze([_rstrip(line) for line in text.split("\n")])


#: The signature delimiter as RFC 3676 writes it: dash, dash, space. The one
#: line whose trailing blank means something, so the pipeline carries it as
#: it came and only the finished text loses the blank.
SIGNATURE_DELIMITER = "-- "


def _rstrip(line: str) -> str:
    return line if line == SIGNATURE_DELIMITER else line.rstrip()


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


def _header_block(lines: list, index: int, known: dict | None = None) -> int:
    """Does a quoted mail's header block start at `lines[index]`? 0, 1 or 2.

    1 — a run of ``Label: value`` lines whose labels cover a From, a
    Sent/Date and one more header in some language of the table. Requiring
    the Subject (or an address on the From line) tells a quoted mail from an
    itinerary's "From: … To: … Date: …"; requiring the date tells it from two
    or three headers an author pasted to talk about them. Enough under a
    separator or inside an element only a quoting client writes.

    2 — the block as a client writes it and little else is written: it opens
    with the From or the Sent line, has a To and a Subject as well, and its
    Sent line carries a time of day. A form mailer's "From / Date / Subject /
    Phone", a helpdesk's "From / Date / Subject" and a memo's "TO / FROM /
    DATE" all stop at 1. Only this stands for itself, with no separator.

    `known` remembers what each line was found to be: in a mail of nothing
    but headers every line is looked at from each of the sixteen above it,
    and is read once.
    """
    if known is None:
        known = {}

    def header(position: int) -> tuple:
        # (role, and for a From its address, for a Sent its time of day)
        found = known.get(position)
        if found is None:
            match = _LABEL_RE.match(lines[position])
            role = match and _HEADER_ROLE.get(
                " ".join(_fold(match.group(1)).split()))
            value = match.group(2) or "" if role else ""
            found = known[position] = (role or None, bool(
                role == "from" and _ADDRESS_RE.search(value)
                or role == "sent" and _CLOCK_RE.search(value)))
        return found

    first = header(index)[0]
    if first is None:
        return 0
    roles = set()
    from_has_address = timed = False
    position = index
    # A header block is a dozen lines at most; the bound also keeps a mail
    # made of nothing but "From:" lines from costing a scan per line.
    last = min(len(lines), index + 16)
    while position < last and lines[position].strip():
        role, vouched = header(position)
        if role:
            roles.add(role)
            if vouched and role == "from":
                from_has_address = True
            elif vouched:
                timed = True
        elif not any(header(ahead)[0] for ahead in
                     range(position + 1, min(position + 4, len(lines)))):
            # Neither a header nor the wrapped tail of one (a long To: list
            # runs over several lines before the next label).
            break
        position += 1
    if not ("from" in roles and "sent" in roles and len(roles) >= 3
            and ("subject" in roles or from_has_address)):
        return 0
    if (first in ("from", "sent") and timed and "to" in roles
            and "subject" in roles):
        return 2
    return 1


def _verb_then_name(line: str, folded: str) -> bool:
    """Does the line end on its verb and then a name? ("… schrieb Bob Example:")

    The languages that put the verb before the name end an attribution so.
    Nothing but a name may follow: a few words, each capitalised — which
    "wrote off the balance:" is not.
    """
    match = None
    for match in _VERB_RE.finditer(folded):
        pass
    if match is None:
        return False
    count = len(folded[match.end():].rstrip(" :：").split())
    if not 1 <= count <= 4:
        return False
    words = line.rstrip(" :：").split()[-count:]
    return all(word[0].isupper() or not word[0].isalpha() for word in words)


def _is_attribution(line: str, need_verb: bool = False) -> bool:
    """The shape of "On <date>, <someone> wrote:" in any language.

    Ends in a colon and carries an address in brackets, or a date and a time;
    the verb table widens that to a bare address or a lone date. `need_verb`
    is the stricter test for an attribution that no ``>`` block follows, where
    being wrong would cut the rest of the mail.
    """
    line = line.strip()
    if len(line) > _ATTRIBUTION_MAX or not _COLON_END_RE.search(line):
        return False
    bracketed = bool(_BRACKET_ADDRESS_RE.search(line))
    dated = bool(_DATE_RE.search(line)) and bool(_TIME_RE.search(line))
    folded = _fold(line)
    verb = bool(_VERB_RE.search(folded))
    if need_verb:
        # … and the line must END as an attribution does — on the verb, on
        # the address, or on the verb and the name: "the invoice of
        # 03.03.2025 14:02 wrote off the balance:" has the verb and the date
        # and is a sentence.
        return verb and (bracketed or dated) and bool(
            _VERB_END_RE.search(folded) or _ADDRESS_END_RE.search(line)
            or _verb_then_name(line, folded))
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


def _attribution_tail(kept: list, marked: list, need_verb: bool = False) -> int:
    """How many trailing lines of `kept` are an attribution (0: none).

    `kept` are lines without marks, a blank one being ``""``; `marked` says
    of each whether the HTML marked it an attribution. Trailing blank lines
    count into the answer, so the caller can cut them with it. A marked line
    is an attribution outright; otherwise the last line must have the shape,
    alone or as the end of a folded line — and a folded line must start its
    paragraph and not end a sentence, so the author's own closing words are
    never read into it.

    The cost is that of a few short lines: nothing longer than an attribution
    is looked into, however long the line above one is.
    """
    end = len(kept)
    while end and not kept[end - 1]:
        end -= 1
    if not end:
        return 0
    if marked[end - 1]:
        start = end
        while start and marked[start - 1]:
            start -= 1
        return len(kept) - start
    last = kept[end - 1]
    if len(last) > _ATTRIBUTION_MAX or not _COLON_END_RE.search(last):
        return 0
    # The folded forms first: "…, Ann Example\n<ann@example.org> wrote:" has a
    # last line that passes on its own and would leave its head behind.
    for span in (2, 3):
        start = end - span
        if start < 0:
            break
        group = kept[start:end]
        head = group[0]
        if (not head or sum(map(len, group)) > _ATTRIBUTION_MAX
                or _QUOTE_RE.match(head) or _SENTENCE_END_RE.search(head)):
            break
        if start and kept[start - 1]:
            continue              # not where the paragraph begins
        lead = " ".join(group[:-1])
        if not (_DATE_RE.search(lead) or _TIME_RE.search(lead)):
            continue              # an attribution's head carries its date
        if not _wrapped(group):
            continue
        if _is_attribution(" ".join(part.strip() for part in group), need_verb):
            return len(kept) - start
    if need_verb and end > 1 and kept[end - 2]:
        return 0                  # mid-paragraph: a sentence of the author's
    if _is_attribution(last, need_verb):
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
    words = _dashed(line)
    if words:
        return _has_stem(words, _FORWARD)
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
    # All the levels at once: taking them off one by one copies the line once
    # per level, and a line can be nothing but ">".
    quoting = _QUOTE_LEVELS_RE.match(lines[first])
    levels = quoting.group().count(">")
    if not levels or _label_role(lines[first][quoting.end():]) is None:
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


class _Evidence:
    """What the caller knows of the mail, and what a pass had to leave open.

    `forward` — the subject carries a forward's prefix. `history` — the mail
    cites an earlier one AND its subject carries a reply's prefix: the two
    together license cutting history that no ``>`` marks. `bulk` — the mail's
    headers say it is bulk mail.

    `unsettled` is set by a rule that saw its shape and held back for want of
    that evidence; it makes the state ``wanting``, so what the rule could not
    decide a model may. Each pass starts it afresh, since only what the text
    still shows at the end is open. `held` is the same for what only the
    FIRST pass can see — a mark the HTML left, gone from the text after it —
    and is kept for the whole trim.
    """

    __slots__ = ("forward", "history", "bulk", "unsettled", "held")

    def __init__(self, forward: bool = False, history: bool = False,
                 bulk: bool = False):
        self.forward = forward
        self.history = history
        self.bulk = bulk
        self.unsettled = False
        self.held = False


class _Spent(Exception):
    """A reading has read all it may (MAX_READ_LINES, MAX_READ_CHARS)."""


class _Budget:
    """What one reading may still read closely, over all its passes."""

    __slots__ = ("lines", "chars")

    def __init__(self):
        self.lines = MAX_READ_LINES
        self.chars = MAX_READ_CHARS

    def spend(self, lines: int, chars: int) -> None:
        self.lines -= lines
        self.chars -= chars
        if self.lines < 0 or self.chars < 0:
            raise _Spent()


def _history_event(lines: list, quoted: list, index: int, kept: list,
                   marked: list, evidence: _Evidence,
                   known: dict) -> int | None:
    """Does quoted history that carries no ``>`` start at `lines[index]`?

    Answers how many trailing lines of `kept` belong to it (0 normally; the
    folded head of an attribution otherwise), or None when it does not.
    """
    line = lines[index].strip()
    if not line:
        return None
    words = _dashed(line)
    if words:
        if _has_stem(words, _ORIGINAL):
            return 0
        following = _next_content(lines, index + 1, 2)
        if following is not None and _header_block(lines, following, known):
            return 0
        return None
    block = _header_block(lines, index, known)
    if block == 2:
        return 0
    if block:
        # Headers, but not the block a client writes: a form mailer's, a
        # helpdesk's, a memo's, a mail pasted to ask about it. They stay.
        evidence.unsettled = True
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
            marked.append(False)
            span = _attribution_tail(kept, marked, need_verb=True)
            kept.pop()
            marked.pop()
            if span:
                return span - 1
    return None


def _boundary_confirmed(lines: list, index: int, hard: bool,
                        known: dict) -> bool:
    """Is the element the HTML marked at `index` really a quoted header?"""
    found = []
    # A header block stands right under its mark. The window is the one
    # _header_block looks through; without it a mail of nothing but marked
    # elements (which read as blank lines) is scanned to its end from each.
    last = min(len(lines), index + 17)
    position = index + 1
    while position < last and len(found) < 6:
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
    if _header_block(lines, found[0], known):
        return True
    first = _LABEL_RE.match(lines[found[0]])
    return bool(first and len(labels) >= 3
                and _BRACKET_ADDRESS_RE.search(first.group(2) or ""))


def _strip_quotes(raw: list, evidence: _Evidence, budget: _Budget):
    """Stage 1. Answers ``(intro, forwarded, removed_chars, end_cut)``.

    `intro` is the author's own lines; `forwarded` is None, or the forwarded
    message from its marker on, which is content and is kept as it stands.
    `end_cut` says that what stood under the intro's last line was cut.

    ``>`` blocks go on their own evidence. History that carries no ``>`` —
    a separator, a header block, an attribution above bare text, an element
    the HTML marked — is cut to the end only when `evidence.history` says
    the mail is a reply; under a forward's subject it is the message being
    passed on; with neither it stays where it is and the pass is unsettled.

    What is read closely or kept is paid for from `budget`, line by line: a
    line of the author's, a quote block (as one line: its first is looked
    at), a forwarded message. Lines that are skipped or cut are not — which
    is all that a long thread under a short reply consists of. Raises _Spent
    where the budget ends.
    """
    def kept_whole(body: list) -> list:
        budget.spend(len(body), sum(map(len, body)))
        return body

    lines = [_unmark(line) for line in raw]
    quoted = _quote_flags(lines)
    lone = sum(quoted) == 1
    kept: list = []         # the author's lines so far; a blank one is ""
    marked: list = []       # of each: did the HTML mark it an attribution?
    judged = -1             # len(kept) when its tail last proved to be none
    known: dict = {}        # see _header_block
    removed = 0
    end_cut = False
    index = 0
    total = len(lines)

    def keep(line: str, mark: bool = False) -> None:
        # Never two blank lines in a row: the look-back for an attribution
        # skips the blank tail of `kept`, and must not have a long one to skip.
        nonlocal end_cut
        if line.strip():
            kept.append(line)
            marked.append(mark)
            end_cut = False
        elif kept and kept[-1]:
            kept.append("")
            marked.append(False)

    while index < total:
        if quoted[index]:
            end = index
            while end < total and quoted[end]:
                end += 1
            if index and (_DIFF_HUNK_RE.match(lines[index - 1].strip())
                          or (lone and _mid_sentence(lines, index))):
                for line in kept_whole(lines[index:end]):   # content, not a quote
                    keep(line)
                index = end
                continue
            budget.spend(1, 0)
            inner = [_unquote(line) for line in lines[index:end]]
            first = _next_content(inner, 0)
            if first is not None and _is_forward_marker(inner, first):
                # Apple Mail (HTML) and Yahoo quote the whole forward, marker
                # included.
                return (kept, kept_whole(_forward_body(
                    inner[first:] + lines[end:])), removed, False)
            # A tail already found to be no attribution is not asked again
            # by the next quote block: a mail of a thousand blocks under one
            # line would ask a thousand times.
            drop = 0 if len(kept) == judged else _attribution_tail(kept, marked)
            if drop:
                removed += _chars(kept[len(kept) - drop:])
                del kept[len(kept) - drop:]
                del marked[len(marked) - drop:]
            removed += _chars(lines[index:end])
            keep("")                # a paragraph break where the quote stood
            end_cut = True
            if not drop:
                judged = len(kept)
            index = end
            continue
        budget.spend(1, len(lines[index]))
        event = None
        if raw[index].startswith(_BOUNDARY):
            if _boundary_confirmed(lines, index, raw[index] == _MARK_HARD,
                                   known):
                event = 0
        elif _is_forward_marker(lines, index):
            return (kept, kept_whole(_forward_body(lines[index:])), removed,
                    False)
        else:
            event = _history_event(lines, quoted, index, kept, marked,
                                   evidence, known)
        if event is not None and not (evidence.forward or evidence.history):
            # History by its shape — but nothing says this mail answers
            # another, and in a notification, a memo or a forward whose
            # subject no table knows, the same shape is the content.
            evidence.unsettled = True
            if raw[index].startswith(_BOUNDARY):
                evidence.held = True
            event = None
        if event is None:
            keep(lines[index], raw[index].startswith(_ATTR))
            index += 1
            continue
        rest = kept[len(kept) - event:] if event else []
        if event:
            del kept[len(kept) - event:]
            del marked[len(marked) - event:]
        rest += lines[index:]
        if evidence.forward:
            # An Outlook forward: the "history" is the message being passed
            # on. Its rule or separator would read as a reply's on the next
            # pass, so the plain forward marker replaces it.
            start = _next_content(rest, 0)
            while start is not None and (
                    _RULER_RE.match(rest[start])
                    or _dashed(rest[start].strip())):
                start = _next_content(rest, start + 1)
            rest = rest[start:] if start is not None else []
            return kept, [FORWARD_MARKER] + kept_whole(rest), removed, False
        removed += _chars(rest)
        end_cut = True
        break
    return kept, None, removed, end_cut


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
# which a mail built to be slow could otherwise turn into hours. They are
# handed squeezed lines: a blank line is "", and never two in a row.

_WORD_EDGES = ".,;:!?()[]<>\"'«»*_"


def _names_only(words: list) -> bool:
    """Are these words names and nothing else?

    A device or a product of the tables; a word with a capital or a digit in
    it, which is how a make and a model are written ("Galaxy S8", "iMac");
    or a joining word right before a table's name ("for iPhone"). Any other
    word is a word of a sentence: "… to yours yesterday", "… on Monday".
    """
    for position, word in enumerate(words):
        word = word.strip(_WORD_EDGES)
        folded = _fold(word)
        if (not word or _MOBILE_NAME_RE.search(folded)
                or word != word.lower() or any(ch.isdigit() for ch in word)):
            continue
        if not (folded in _MOBILE_JOINER and position + 1 < len(words)
                and _MOBILE_NAME_RE.search(_fold(words[position + 1]))):
            return False
    return True


def _mobile_line(lines: list, index: int) -> int:
    """Is `lines[index]` the line a phone or an app signs with?

    2 — yes: the table's words, the shape of a signing line (short, no
    clause, no second sentence, no number), a paragraph of its own, the name
    of what sent it — a product, or a word for a device — and beside the
    table's words nothing but names. 1 — it has the words and the shape, and
    lacks the paragraph or the names ("Sent from Hamburg on Monday", "Sent
    from my office", "Sent from my iPhone to yours yesterday"): not cut, but
    not settled. 0 — a sentence that starts like one ("Sent with DHL,
    tracking 12345").
    """
    line = lines[index]
    if len(line) > 120:
        return 0
    bare = _HUNG_LINK_RE.sub("", line).strip()
    # Folded word by word, so that a place in the folded line is a word of
    # the line as it was written, capitals and all.
    words, parts = [], []
    for word in bare.split():
        part = "".join(_fold(word).split())
        if part:
            words.append(word)
            parts.append(part)
    folded = " ".join(parts)
    match = _MOBILE_OWN_RE.match(folded)
    own = match is not None
    if not own:
        match = _MOBILE_SENT_RE.match(folded)
        if match is None:
            return 0
    if _PROSE_RE.search(bare) or len(words) > (12 if own else 8):
        return 1 if own else 0
    if index and lines[index - 1]:
        return 1                    # under a line of text: maybe a sentence
    if not _MOBILE_NAME_RE.search(folded):
        return 1
    # The sender's own words in the line: what stands for an entry's `*`,
    # and whatever follows the table's words.
    after = sum(1 for part in folded[match.end():].split()
                if part.strip(_WORD_EDGES))
    said = words[len(words) - after:] if after else []
    if match.lastindex:
        start = folded.count(" ", 0, match.start(match.lastindex))
        count = len(match.group(match.lastindex).split())
        said = words[start:start + count] + said
    return 2 if _names_only(said) else 1


def _strip_signature(lines: list, evidence: _Evidence,
                     more: bool = False) -> int:
    """Stage 2, in place: the ``-- `` delimiter, then the phone's closing line.

    Walking up from the end, a delimiter counts while no more than
    SIGNATURE_MAX_LINES lines separate it from the end. It cuts when it is
    the only one within that reach and is written as the standard writes it,
    with its trailing blank. Two within reach of each other are dividers
    between sections; a bare ``--`` is as likely a divider someone typed as
    a delimiter a relay stripped. Both stay, and leave the pass unsettled.

    So does a delimiter that is the first line of the text: a signature
    under nothing is the mail (a post that is an attachment and the sender's
    name), as _under_a_message says of footers. Cut, it would leave nothing,
    and the answer to nothing is the mail as it came — with the footer that
    was rightly cut from under it. Unless `more` follows these lines: above
    a forwarded message, a signature alone is the whole of an intro.
    """
    removed = 0
    found = []
    below = 0
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if line == SIGNATURE_DELIMITER or line == "--":
            found.append(index)
            if len(found) == 2:
                break
            below = 0
        elif line:
            below += 1
            if below > SIGNATURE_MAX_LINES:
                break
    if (len(found) == 1 and lines[found[0]] == SIGNATURE_DELIMITER
            and (found[0] or more)):
        removed += _drop_from(lines, found[0])
    elif found:
        evidence.unsettled = True
    if lines:
        verdict = _mobile_line(lines, len(lines) - 1)
        if verdict == 2:
            removed += _drop_from(lines, len(lines) - 1)
        elif verdict:
            evidence.unsettled = True
    return removed


# No footer paragraph is longer than this many lines, or characters.
_FOOTER_MAX_LINES = 40
_FOOTER_MAX_CHARS = 6000
# How many lines may still follow an unsubscribe paragraph: the sender's
# postal address under the link.
_UNSUBSCRIBE_MAX_AFTER = 6


def _tail_paragraphs(lines: list, count: int) -> list:
    """``(start, end)`` of the last `count` runs of non-blank lines, in order.

    The walk up ends at a run longer than a footer can be: it is none, and
    more follows everything above it than may follow a footer. So the walk
    is short however the mail is built — one paragraph of a million lines
    included.
    """
    spans = []
    end = None
    for index in range(len(lines) - 1, -2, -1):
        if index >= 0 and lines[index]:
            if end is None:
                end = index + 1
            elif end - index > _FOOTER_MAX_LINES:
                spans.append((index, end))
                break
        elif end is not None:
            spans.append((index + 1, end))
            end = None
            if len(spans) == count:
                break
    return spans[::-1]


def _paragraph(lines: list, start: int, end: int) -> str:
    """The paragraph as one text; none when it is too long to be a footer."""
    if (end - start > _FOOTER_MAX_LINES
            or sum(map(len, lines[start:end])) > _FOOTER_MAX_CHARS):
        return ""
    return " ".join(lines[start:end])


_FIRST_SENTENCE_RE = re.compile(r".{0,300}?[.!?…](?=\s|$)", re.S)


def _notice_cues(text: str) -> int:
    """How much of a legal notice is this text? 0, 2 or 3.

    3 — it names the mail itself, confidentiality, a recipient and what a
    reader it was not meant for is to do, and it OPENS as a notice: two of
    the first three in its first sentence ("This e-mail is confidential …").
    2 — it speaks of confidentiality and of a recipient: a notice in words
    no table has for the rest, or a sentence about a document. Never cut,
    but not settled either.
    """
    if len(text) < 100:
        return 0
    folded = _fold(text)
    if not (_CONFIDENTIAL_RE.search(folded) and _RECIPIENT_RE.search(folded)):
        return 0
    if not (_MESSAGE_RE.search(folded) and _MISDIRECTED_RE.search(folded)):
        return 2
    first = _FIRST_SENTENCE_RE.match(folded)
    opening = first.group() if first else folded[:300]
    kinds = sum(1 for cue in (_MESSAGE_RE, _CONFIDENTIAL_RE, _RECIPIENT_RE)
                if cue.search(opening))
    return 3 if kinds >= 2 else 2


def _signs_off(line: str) -> bool:
    """A name, "Kind regards", a company: short, and no finished sentence."""
    line = line.strip()
    return (len(line) < 50 and not _SENTENCE_END_RE.search(line)
            and not _COLON_END_RE.search(line))


def _closes(lines: list, index: int) -> bool:
    """Does `lines[index]` close the author's text?

    A sign-off or the phone's line — and not the first line of the mail,
    which is short for another reason: it is the greeting.
    """
    return index > 0 and (_signs_off(lines[index])
                          or _mobile_line(lines, index) == 2)


def _notice_start(lines: list, start: int, end: int) -> int:
    """Where in its paragraph a legal notice begins.

    A server glues its notice right under the sender's last line as often as
    it leaves a blank line, so the paragraph can open with "Regards / Bob".
    The notice begins at the lowest line that follows a sign-off, starts a
    sentence and, with what is under it, is a whole notice still — so the
    author's lines above it stay. Failing that the paragraph is the notice.
    """
    for first in range(end - 1, start, -1):
        if (_signs_off(lines[first - 1])
                and not lines[first].lstrip()[:1].islower()
                and _notice_cues(" ".join(lines[first:end])) == 3):
            return first
    return start


def _unsubscribe_cue(text: str) -> int:
    """How much of an unsubscribe block is this text? 0, 1 or 2.

    0 — none: the word alone is prose ("tell me if you want to unsubscribe").
    2 — the word and the means, which is what a footer hands over: a link,
    or the address of a list server. 1 — the word and somebody's address,
    which is how a request reads ("please unsubscribe me, my address is …"):
    never cut, but not settled.
    """
    if not text or not _UNSUBSCRIBE_RE.search(_fold(text)):
        return 0
    if _URL_RE.search(text):
        return 2
    cue = 0
    for match in _ADDRESS_RE.finditer(text):
        if _LIST_COMMAND_RE.search(match.group().partition("@")[0].lower()):
            return 2
        cue = 1
    return cue


def _unsubscribe_start(lines: list, start: int, end: int) -> int:
    """Where in its paragraph an unsubscribe block begins.

    At its first line that carries the word, taken back over the lines of
    the sentence the word stands in — a full-width line above, or a line
    this one continues in lower case — and never over a short, finished
    line, which is a sign-off. Unless that line is the delimiter a list
    writes above its footer ("-- ", as Google Groups does): it goes with it.
    """
    for first in range(start, end):
        if _UNSUBSCRIBE_RE.search(_fold(lines[first])):
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
    if _unsubscribe_cue(" ".join(lines[first:end])) == 2:
        # The delimiter a list writes above its footer is the footer's, not
        # a sign-off: left behind, it stands as a second one under the
        # sender's own, and two within reach are dividers that both stay.
        if first > start and lines[first - 1] in (SIGNATURE_DELIMITER, "--"):
            first -= 1
        return first
    return start


def _under_a_message(lines: list, cut: int) -> bool:
    """Do two lines of text, at least, stand above `cut`?

    A footer stands under a mail. Under a greeting alone, or under nothing,
    the block is what the mail is about.
    """
    above = [line for line in lines[max(0, cut - 4):cut] if line]
    return len(above) >= 2


def _list_footer(lines: list):
    """``(start, whole)`` of a mailing list's footer at the end, or None.

    A long underscore rule, then a few lines that say where the list lives,
    one of them nothing but its URL or address. `whole` — it is the footer as
    the list software writes it: the list's name in words, its address and
    its page, each on a line of its own. A postscript under a rule ("see
    also", and a link; two links) is not.
    """
    tail = []
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        if _LIST_RULER_RE.match(line):
            urls = sum(1 for item in tail if _BARE_URL_RE.fullmatch(item))
            addresses = sum(1 for item in tail
                            if _BARE_ADDRESS_RE.fullmatch(item))
            if not urls and not addresses:
                return None
            # … and one of the two is a list server's: under the same rule,
            # a name, an address and a homepage are somebody's contact block.
            placed = any(_LIST_PLACE_RE.search(item) for item in tail
                         if _BARE_URL_RE.fullmatch(item)
                         or _BARE_ADDRESS_RE.fullmatch(item))
            return index, bool(urls and addresses and placed
                               and urls + addresses < len(tail))
        if line:
            tail.append(line)
            if len(tail) > 6:
                break
    return None


def _ends_on_link(line: str) -> bool:
    """Does the line end on a link or an address, closing marks aside?"""
    words = line.rstrip(_LINK_CLOSERS).rsplit(None, 1)
    return bool(words) and bool(
        _URL_RE.search(words[-1]) or _ADDRESS_RE.search(words[-1]))


def _judged(lines: list, span: tuple, judged: dict) -> tuple:
    """``(notice cues, unsubscribe cue, imprint?, ends on a link?)`` of the
    paragraph at `span`, worked out once: the tail stages only ever cut the
    END off `lines`, so what stands at a span is the same for as long as it
    stands there — however often the stages are repeated under it."""
    verdict = judged.get(span)
    if verdict is None:
        text = _paragraph(lines, *span)
        start, end = span
        verdict = judged[span] = (
            _notice_cues(text), _unsubscribe_cue(text),
            bool(text and _IMPRINT_RE.search(text) and not any(
                _LIST_ITEM_RE.match(line) for line in lines[start:end])),
            bool(text) and _ends_on_link(lines[end - 1]))
    return verdict


def _closes_a_footer(verdict: tuple) -> bool:
    """May this paragraph stand under an unsubscribe block and go with it?

    Another block that hands over its link, a whole legal notice, or the
    sender's imprint. Not a paragraph that merely mentions unsubscribing or
    confidentiality: under a footer or not, that is somebody's sentence.
    """
    notice, cue, imprint, linked = verdict
    if cue:
        return cue == 2 and linked
    return notice == 3 or (not notice and imprint)


def _strip_footer(lines: list, evidence: _Evidence, judged: dict) -> int:
    """Stage 3, in place: one footer per call, the caller repeats.

    *Legal notices* — the run of notice paragraphs the mail ENDS on, and
    only under a line that closes the author's text. A paragraph that
    mentions confidentiality with the sign-off still to come, or right under
    a finished sentence, is what the author had to say.

    *Unsubscribe blocks and list footers* — only in mail whose headers say
    it is bulk mail, and only in the shape software gives them: the block
    hands over the means and ends on it, stands under a message and has
    nothing under it but more of its kind and an imprint. In a person's mail
    "please unsubscribe me, my address is …" and a line of dashes above a
    link are the message — and a person's post that a list passed on is bulk
    mail by its headers: "can someone unsubscribe me? https://… does not
    work" goes on after its link, and stays, also above the list's own
    block.

    Whatever looks like one of these and stays leaves the pass unsettled.
    """
    spans = _tail_paragraphs(lines, FOOTER_MAX_PARAGRAPHS)
    found = [_judged(lines, span, judged) for span in spans]
    cut = None
    for (start, end), verdict in zip(reversed(spans), reversed(found)):
        if verdict[0] < 3:
            break
        cut = _notice_start(lines, start, end)
        if cut > start:
            break                   # the author's lines open this paragraph
    if cut:
        above = cut - 1 if lines[cut - 1] else cut - 2
        if _closes(lines, above):
            return _drop_from(lines, cut)
    if any(verdict[0] for verdict in found):
        evidence.unsettled = True
    # "At the end" is enforced, not assumed: an unsubscribe line at the top
    # of a notification, with the news under it, is not a footer. Nor is one
    # with words under it — a sign-off, the next step of a how-to.
    for position, (start, end) in enumerate(spans):
        cue, linked = found[position][1], found[position][3]
        if not cue:
            continue
        if sum(1 for line in lines[end:] if line) > _UNSUBSCRIBE_MAX_AFTER:
            continue
        if (evidence.bulk and cue == 2 and linked and all(
                _closes_a_footer(verdict) for verdict in found[position + 1:])):
            cut = _unsubscribe_start(lines, start, end)
            if _under_a_message(lines, cut):
                return _drop_from(lines, cut)
        # Not a footer — which the next paragraph with the word may still be.
        evidence.unsettled = True
    footer = _list_footer(lines)
    removed = 0
    # (Footers stacked under one another go in one call.)
    while (footer and footer[1] and evidence.bulk
           and _under_a_message(lines, footer[0])):
        removed += _drop_from(lines, footer[0])
        footer = _list_footer(lines)
    if footer and not removed:
        evidence.unsettled = True
    return removed


def _strip_tail(lines: list, removed: dict, end_cut: bool,
                evidence: _Evidence, more: bool = False) -> None:
    """Stages 2 and 3, repeated until the end of the text is settled.

    They uncover work for each other — a notice under the phone's closing
    line hides that line from the signature stage until the footer stage has
    run — so neither is done before both find nothing.

    `end_cut`: what stood under these lines was quoted history, and is gone.
    `more`: a forwarded message stands under them, and stays.

    Not for a text that was cut off (CUT_MARK): where that ends is not where
    the mail ended, and a signature or a footer is one at the END only.
    """
    if lines and lines[-1] == CUT_MARK:
        evidence.unsettled = True
        return
    counter = "quote"
    judged: dict = {}       # see _judged
    while True:
        size = len(lines)
        # The rule a client draws above the history it appends (or a sender
        # above their footer) stays behind when that goes; it belongs to
        # what was cut. A rule nothing was cut under is the author's line.
        while end_cut and lines and _RULER_RE.match(lines[-1]):
            removed[counter] += _drop_from(lines, len(lines) - 1)
        before = len(lines)
        removed["signature"] += _strip_signature(lines, evidence, more)
        removed["footer"] += _strip_footer(lines, evidence, judged)
        end_cut, counter = len(lines) < before, "footer"
        if len(lines) == size:
            return


def _pass(raw: list, evidence: _Evidence, removed: dict,
          budget: _Budget) -> list:
    evidence.unsettled = False      # what the LAST pass leaves open counts
    intro, forwarded, cut, end_cut = _strip_quotes(raw, evidence, budget)
    removed["quote"] += cut
    intro = _squeeze(intro)
    _strip_tail(intro, removed, end_cut, evidence, forwarded is not None)
    if forwarded is not None:
        intro = intro + [""] + forwarded
    return _squeeze([_rstrip(line) for line in intro])


#: Passes after which a text that still changes is given up on. A mail as
#: clients write it is changed by the first and confirmed by the second;
#: this allows for three that change it.
_MAX_PASSES = 4


def _trim_lines(raw: list, evidence: _Evidence, removed: dict) -> list | None:
    """The pipeline, to its fixpoint.

    Removing the end of a text can change how the rest reads (a quote that
    was "inside a fenced block" until the closing fence went with the
    signature), so the pipeline runs until a pass changes nothing — which is
    what makes the trim idempotent by construction. A real mail settles on
    the second pass. One that has not settled after `_MAX_PASSES` was built
    not to, and gets no trim at all (None): the untrimmed text is the one
    answer that is the same however often it is asked for. Nor does one
    whose passes would read more, between them, than a reading may (see
    MAX_READ_LINES): the first pass leaves a mail's quoted history behind,
    so that is a mail with more text of its author's than the trim takes on
    — or one built to be read at length.
    """
    budget = _Budget()
    lines = raw
    try:
        for _ in range(_MAX_PASSES):
            after = _pass(lines, evidence, removed, budget)
            if after == lines:
                return lines
            lines = after
    except _Spent:
        pass
    return None


def _marker_survives(lines: list) -> bool:
    """Did quoting the trim could not settle stay in the text?

    A ``>`` line; or a line with the shape of an attribution where one
    stands — opening its paragraph, or hanging at the very end. Such a line
    with text under it and no ``>`` is how a client quotes in a language no
    table here has the verb of: it was kept, and the state must say so.
    """
    last = None
    for index, line in enumerate(lines):
        if not line:
            continue
        last = line
        if _QUOTE_RE.match(line):
            return True
        if ((not index or not lines[index - 1])
                and len(line) <= _ATTRIBUTION_MAX and _is_attribution(line)):
            return True
    return bool(last is not None and len(last) <= _ATTRIBUTION_MAX
                and _is_attribution(last))


def is_forward_subject(subject: str | None) -> bool:
    """Is the subject's outermost prefix a forward's ("Fwd:", "WG:", "TR:", …)?"""
    return bool(isinstance(subject, str)
                and _FORWARD_SUBJECT_RE.match(_fold(subject)))


def is_reply_subject(subject: str | None) -> bool:
    """Is the subject's outermost prefix a reply's ("Re:", "AW:", "SV:", …)?"""
    return bool(isinstance(subject, str)
                and _REPLY_SUBJECT_RE.match(_fold(subject)))


def _settle(raw: list, evidence: _Evidence) -> tuple:
    """``(lines, removed, unsettled)`` of one source. No lines when the trim
    left nothing; None for them when it failed, did not settle or would have
    read more than a reading may — after which the caller tries no reading
    but its last, each of them costing up to that much again."""
    removed = {"quote": 0, "signature": 0, "footer": 0, "pixels": 0}
    evidence.held = False
    try:
        lines = _trim_lines(raw, evidence, removed)
    except Exception:       # a bubble with too much in it beats no bubble
        lines = None
    return lines, removed, evidence.unsettled or evidence.held or not lines


def _as_it_came(text: str) -> str:
    """A plain part with nothing done to it but what every answer has done:
    one kind of line end, no mark, no empty line at either end."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _no_marks(text).strip("\n")


def _oversize(text: str) -> bool:
    """More text than a source may be? (MAX_SOURCE_LINES, …_CHARS)"""
    return (len(text) > MAX_SOURCE_CHARS
            or text.count("\n") >= MAX_SOURCE_LINES)


def _text_of(lines: list) -> str:
    """The lines as the text they are answered as."""
    return "\n".join(line.rstrip() for line in lines)


# A quote container some client wrote into the HTML part: worth a parse when
# the plain part showed no quote. (The last is desktop Outlook's: a div with
# a rule above it around the quoted header block.)
_HTML_QUOTE_HINT_RE = re.compile(
    r"divRplyFwdMsg|appendonsend|gmail_quote|yahoo_quoted|protonmail_quote"
    r"|moz-cite-prefix|type\s*=\s*[\"']?cite"
    r"|border-top\s*:(?!\s*(?:none|0|hidden))", re.I)

#: A plain part shorter than this, beside an HTML part that says
#: `STUB_RATIO` times as much (and a good deal in absolute terms), is a stub
#: — "This mail needs an HTML viewer", a bare link — not the mail's text.
STUB_CHARS = 400
STUB_RATIO = 8


def _is_stub(plain: str, marked: str) -> bool:
    said = len(" ".join(plain.split()))
    # What the HTML part says to a reader: a line that only marks where
    # history may begin says nothing, however many of them there are.
    says = len(" ".join(" ".join(_unmarked(marked.split("\n"))).split()))
    return says > 1000 and says > STUB_RATIO * said


# A ``data:`` URL where markup holds one — an attribute's value, a url() of
# a style — and what ends it there when no quote does.
_DATA_URL_RE = re.compile(r"data:", re.I)
_DATA_URL_END_RE = re.compile(r"[\s\"'<>()&]")
_DATA_URL_OPENERS = ("&quot;", "&#34;", "&#39;", "&apos;")
#: A ``data:`` URL longer than this is an image, a font: never text of the
#: mail, and not counted as markup that is read (MAX_HTML_CHARS).
DATA_URL_CHARS = 4096
# How many candidates for one are looked at in a part. A mail of nothing
# but the word is not searched to its end for an image.
_MAX_DATA_URLS = 20_000
_CHARREF_END_RE = re.compile(r"[\s;<]")


def _markup_read(html: str) -> tuple:
    """``(markup, cut)``: what a call reads of an HTML part, and whether
    that is not all of it.

    The first MAX_HTML_CHARS characters — of markup that can be text, or can
    say where the text is. An image a client wrote into the part itself is
    neither, and a megabyte of it would use up the bound before the first
    word (or after the second, and take the rest of the mail with it): the
    payload of a long ``data:`` URL is left out, not counted, and the tag
    that held it stays.

    A longer part is cut at the bound, wherever that falls: the parser is
    handed what was read and not told that it ends there (see _convert), so
    a tag, a comment or a character reference the bound falls into is
    dropped, and nothing of it is taken for text. Cutting back to where the
    last tag begins instead would drop all of an element that is longer than
    the bound allows — a log in a <pre> — from its first word on.
    """
    limit = MAX_HTML_CHARS
    if len(html) <= limit:
        return html, False
    kept: list = []
    size = done = at = 0
    bare_end = 0            # where the unquoted value last looked at ends
    for _ in range(_MAX_DATA_URLS):
        # (Looked for from where the last payload ended: one that was left
        # out is not searched for more of them.)
        match = _DATA_URL_RE.search(html, at)
        if match is None:
            break
        start, at = match.span()
        if size + start - done >= limit:
            break                   # the bound falls before this one
        # In an attribute or a url(): right after "=" or "(", a quote or not.
        before = html[max(done, start - 10):start]
        quote = ""
        if before[-1:] in ("\"", "'"):
            quote, before = before[-1], before[:-1]
        else:
            for opener in _DATA_URL_OPENERS:
                if before.endswith(opener):
                    before = before[:-len(opener)]
                    break
        if (not before.rstrip().endswith(("=", "("))
                or html.find(",", start, start + 128) < 0):
            continue                # no URL: it has no comma where one has
        end = html.find(quote, start) if quote else -1
        if end < 0:
            # (One search per value, not per "data:" that stands in it.)
            if start >= bare_end:
                ended = _DATA_URL_END_RE.search(html, start)
                bare_end = ended.start() if ended else len(html)
            end = bare_end
        # A payload, and not a sentence someone began with the word: neither
        # a blank nor a tag stands anywhere in it.
        if (end - start <= DATA_URL_CHARS
                or html.find(" ", start, end) >= 0
                or html.find("<", start, end) >= 0
                or html.find(">", start, end) >= 0):
            continue
        kept.append(html[done:at])
        size += at - done
        done = at = end
    kept.append(html[done:done + max(0, limit - size) + 1])
    markup = "".join(kept)
    if len(markup) <= limit:
        return markup, False
    markup = markup[:limit]
    # A character reference the bound falls into would be read as the text
    # it begins with, and a "<" at the very end as a character.
    reference = markup.rfind("&", max(0, limit - 34))
    if reference >= 0 and not _CHARREF_END_RE.search(markup, reference):
        markup = markup[:reference]
    return markup.rstrip("<"), True


def trim(plain: str | None, html: str | None, subject: str | None = None, *,
         is_reply: bool | None = None, bulk: bool | None = None) -> TrimResult:
    """Reduce a mail to its own new text. See the module docstring.

    `plain` and `html` are the mail's two body parts, either may be None. The
    plain part is the source whenever it is the mail's text; see "Which part
    is the source" in the module docstring for when it is not.

    The rest is evidence, and all of it optional: what is not given is not
    known, and what is not known is never a reason to cut.

    `subject` — the mail's Subject. A forward's prefix on it makes history
    that no marker tells from a reply's the forwarded message it is; a
    reply's prefix is one half of what lets such history be cut.

    `is_reply` — whether the mail cites an earlier one (an In-Reply-To or a
    References header). The other half: history that carries no ``>`` is cut
    only in a mail that cites one AND is subjected as a reply.

    `bulk` — whether the mail's headers call it bulk mail (List-Unsubscribe,
    List-Id, ``Precedence: bulk``). Unsubscribe blocks and list footers are
    cut only then.
    """
    evidence = _Evidence(
        forward=is_forward_subject(subject),
        history=is_reply is True and is_reply_subject(subject),
        bulk=bulk is True)
    has_plain = isinstance(plain, str) and bool(plain.strip())
    has_html = isinstance(html, str) and bool(html.strip())
    removed = {"quote": 0, "signature": 0, "footer": 0, "pixels": 0}
    if has_plain:
        whole = _as_it_came(plain)
        if _oversize(whole):
            # More than a source may be: no trim, and no line of it is so
            # much as looked at. Asked again, the answer is the same.
            return TrimResult(whole, "wanting", removed)
    cut = False
    if has_html:
        html, cut = _markup_read(html)
    converted = None            # (marked text, pixels) of the HTML part, once

    def html_source():
        nonlocal converted
        if converted is None:
            converted = _convert(html, marks=True, cut=cut)
        return converted

    if (has_plain and has_html and len(plain.strip()) < STUB_CHARS
            and len(html) > 2 * STUB_CHARS
            and _is_stub(plain, html_source()[0])):
        has_plain = False       # the plain part is not the mail

    lines: list | None = []
    untrimmed: list = []
    unsettled = False
    if has_plain:
        # (Of the part as it came, which is within the bounds: the part
        # itself can be fifty megabytes of marks around a few lines.)
        untrimmed = _clean_lines(whole)
        lines, removed, unsettled = _settle(untrimmed, evidence)
    consult = has_html and (not has_plain or (
        lines is not None and not removed["quote"]
        and bool(_HTML_QUOTE_HINT_RE.search(html))))
    if consult and has_plain and cut:
        # Markup that was not read to its end cannot stand in for a text
        # that was; what it may have known stays open.
        consult, unsettled = False, True
    if consult:
        marked, pixels = html_source()
        raw = _clean_lines(marked, keep_marks=True)
        if cut:
            raw += ["", CUT_MARK] if raw else [CUT_MARK]
        bare = _unmarked(raw)
        if _oversize(_text_of(bare)):
            # More than a source may be, as above: no trim.
            candidate, open_ = bare, True
            counted = {"quote": 0, "signature": 0, "footer": 0, "pixels": 0}
        else:
            candidate, counted, open_ = _settle(raw, evidence)
            if candidate == []:
                # The marks made the trim take everything. What the text
                # alone supports is what a later trim of this result would
                # arrive at, so it is the answer now.
                candidate, counted, open_ = _settle(bare, evidence)
        counted["pixels"] = pixels
        if not has_plain:
            untrimmed = bare
            lines, removed, unsettled = candidate, counted, open_
        elif candidate and counted["quote"] and (
                not lines or _chars(candidate) < _chars(lines)):
            lines, removed, unsettled = candidate, counted, open_
        else:
            # Not the source — but what the markup showed and no rule could
            # act on (history, in a mail not known to be a reply) is as
            # open in the plain part, which merely could not see it.
            unsettled = unsettled or open_
    if not lines:
        # Greedy to the point of nothing: the mail was all quote, or all
        # signature — or it did not settle. The answer is what a later trim
        # of the mail as it came would make of it — its text alone, no
        # evidence beside it — and, if that is nothing too, the mail as it
        # came.
        pixels = removed["pixels"]
        untrimmed = [line.rstrip() for line in untrimmed]
        lines, removed, _ = _settle(untrimmed, _Evidence())
        if not lines:
            lines = untrimmed
            removed.update(quote=0, signature=0, footer=0)
        removed["pixels"] = pixels
        unsettled = True
    if cut and converted is not None:
        # Whatever was decided by a part that was not read to its end — that
        # the plain part is no stub beside it, too — is open: what the rest
        # of it says, nobody has seen.
        unsettled = True
    # The delimiter's blank and (never, by now) a mark go last.
    text = _no_marks("\n".join(line.rstrip() for line in lines))
    wanting = bool(text) and (
        not has_plain or unsettled or len(text) > WANTING_CHARS
        or _marker_survives(lines))
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
# What a second <a> does not end with the link it stands in, as a browser
# reads the two. An element of HTML's "special" category (a block, a list, a
# table and its parts, …) is moved out from under the first link and stays
# open, and a formatting element (the sanitiser's _CARRIED_TAGS) is opened
# again: one of either kind that hides what is in it goes on hiding it.
_LASTING_TAGS = (
    _PARAGRAPH_TAGS | _BREAK_TAGS | _CLOSES_P | _TABLE_TAGS | frozenset((
        "button", "dir", "hgroup", "listing", "marquee", "menu", "search",
        "a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small",
        "strike", "strong", "tt", "u"))) - frozenset(("legend", "option"))
# And inside these an <a> is no link at all to a browser: text of a script,
# a tag that a <select> drops, an element of another kind in an <svg>. (Not
# so in the muted elements whose content is ordinary markup that merely is
# not shown.)
_OPAQUE_TAGS = _MUTE_TAGS - frozenset(("audio", "video", "canvas", "datalist"))
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
# mail nests a few dozen levels (layout tables); deeper elements are passed
# through untracked.
_MAX_DEPTH = 256
# How many containers deep a line is finished as part of a block of its own
# and then handed outwards — prefixed with "> " by a quote, indented by a list
# item, copied by a cell. This, not _MAX_DEPTH, bounds what nesting can cost:
# each line is copied at most this often.
_MAX_BLOCKS = 16
# … and what it can make of a mail: all the prefixes of a conversion together
# may be this many characters per character of markup (and a little for a
# short mail). A line of real mail has more markup around it than quote
# marks before it; "x<br>" a hundred thousand times inside sixteen quotes
# has not, and its innermost quotes are then written as paragraphs.
_PREFIX_ALLOWANCE = 2
_PREFIX_FLOOR = 4096
_MAX_ANCHORS = 4


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
    # Stripped and measured first: a pattern with blanks to skip on either
    # side of an optional unit takes the square of a long blank value.
    text = str(value).strip()
    if len(text) > 16:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s?(?:px)?", text, re.I)
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
    # A target is the sender's text like any other: no mark and no padding
    # of theirs reaches a line through it — nor through what it decodes to.
    href = (href or "").translate(_INVISIBLE).strip()
    if not href or not text:
        return None
    low = href.lower()
    if low.startswith("mailto:"):
        address, _, query = href[7:].partition("?")
        address = unquote(address).translate(_INVISIBLE).strip()
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
        line = "".join(self.cur)
        self.emit(line if line == SIGNATURE_DELIMITER else line.rstrip(" "))
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

    def __init__(self, marks: bool = False, allowance: int = _PREFIX_FLOOR):
        super().__init__(convert_charrefs=True)
        self.marks = marks
        self.pixels = 0
        self._blocks = [_Block()]
        self._allowance = allowance     # prefix characters still to give out
        self._stack: list = []
        # Where each tag is open in the stack (indices, ascending), and where
        # the open cells and tables are: every question the parser asks of
        # the stack is answered from these, so none of them walks it.
        self._where: dict = {}
        self._walls: list = []
        # Where the elements are open that hide what is in them — the one
        # that mutes, and those inside it that would — and which of them a
        # second <a> does not end (_LASTING_TAGS); where the outermost is in
        # which an <a> is none (_OPAQUE_TAGS; -1: nowhere); and where the
        # links are that a second <a> has ended all the same (_end_link).
        self._veils: list = []
        self._lasting: list = []
        self._opaque = -1
        self._stale: list = []
        self._inert = 0         # depth inside muted or hidden elements
        self._overflow = None   # [tag, depth] of a muted element past _MAX_DEPTH
        self._pre = 0
        self._anchors: list = []

    @property
    def _blk(self) -> _Block:
        return self._blocks[-1]

    # ── stack discipline ──

    def _push(self, element: _Element, veil: bool = False) -> None:
        self._where.setdefault(element.tag, []).append(len(self._stack))
        if element.kind in ("cell", "table"):
            self._walls.append(len(self._stack))
        if veil:
            self._veils.append(len(self._stack))
            if element.tag in _LASTING_TAGS:
                self._lasting.append(len(self._stack))
            elif self._opaque < 0 and element.tag in _OPAQUE_TAGS:
                self._opaque = len(self._stack)
        self._stack.append(element)

    def _pop_to(self, index: int) -> None:
        while len(self._stack) > index:
            element = self._stack.pop()
            self._where[element.tag].pop()
            here = len(self._stack)
            if self._walls and self._walls[-1] == here:
                self._walls.pop()
            if self._veils and self._veils[-1] == here:
                self._veils.pop()
                if self._lasting and self._lasting[-1] == here:
                    self._lasting.pop()
                if self._opaque == here:
                    self._opaque = -1
            if self._stale and self._stale[-1] == here:
                self._stale.pop()
            self._close(element)

    def _last(self, tags) -> int:
        """The innermost open element with one of these tags, or -1."""
        where = self._where
        best = -1
        for tag in tags:
            open_at = where.get(tag)
            if open_at and open_at[-1] > best:
                best = open_at[-1]
        return best

    def _find(self, tags, barriers=()) -> int | None:
        """The innermost open element with one of `tags` that no element with
        one of `barriers` stands inside of; None when there is none."""
        found = self._last(tags)
        if found < 0:
            return None
        # (Nothing stands inside the element that was opened last.)
        if found < len(self._stack) - 1 and self._last(barriers) > found:
            return None
        return found

    def _leave_head(self) -> None:
        """An unclosed <head> ends at the first thing that cannot stand in
        one — text, a <br>, an image — as it does in a browser. Only when the
        head itself is the open element: text inside its <title> is the
        title's."""
        if self._stack and self._stack[-1].tag == "head":
            self._pop_to(len(self._stack) - 1)

    def _implied_end(self, tag: str) -> None:
        """Close what this start tag closes implicitly, as a browser would."""
        if self._stack and tag != "head":
            head = self._last(("head",))
            if head >= 0 and (tag == "body" or (
                    self._stack[-1].tag == "head"
                    and tag not in _HEAD_TAGS and ":" not in tag)):
                self._pop_to(head)
        if tag == "li":
            found = self._find(("li",), ("ul", "ol", "td", "th", "table"))
        elif tag in ("td", "th"):
            found = self._find(("td", "th"), ("tr", "table"))
        elif tag in ("tr", "thead", "tbody", "tfoot"):
            found = self._find(("tr",), ("table",))
        elif tag == "a":
            # A link does not nest: a second <a> ends the first. (Else every
            # word of a mail could be written into 256 open links at once.)
            # With the first it ends what was opened in it, and with that
            # what hides the second — if the first link does, or a <span>
            # and its like opened in it: the rest of what they hid is shown
            # by a browser and in the Original view, and so here.
            found = self._find(("a",), ("td", "th", "table"))
            if self._inert:
                # Not if the hiding is from further out, or where an <a> is
                # no link to a browser: such a tag ends nothing, takes no
                # text and has no part in what is shown.
                if (found is None or self._veils[0] < found
                        or self._opaque > found
                        or (self._stale and self._stale[-1] == found)):
                    return
                # Nor if an element in the link hides that the browser
                # keeps (_LASTING_TAGS: a mobile duplicate in a <div>, a
                # <font> with display:none). The first link ends, what it
                # holds stays open, and hidden.
                if self._lasting and self._lasting[-1] > found:
                    self._end_link(found)
                    return
        elif tag in _CLOSES_P:
            found = self._find(("p",), _P_BARRIERS)
        else:
            return
        if found is not None:
            self._pop_to(found)

    def _end_link(self, index: int) -> None:
        """End the link at `index` for a second <a> in it that stays hidden.

        The link is left on the stack, for what was opened in it: it takes
        no more text, and is no link to end again. What hides is, from here
        on, the first element in the link that the browser keeps — whatever
        hid around that one has ended with the link, so the hiding is over
        when that element is.
        """
        element = self._stack[index]
        if element.kind == "link":
            self._close(element)
            element.kind = "inline"
        self._stale.append(index)
        keeper = self._lasting[bisect_right(self._lasting, index)]
        if self._veils[0] != keeper:
            self._stack[self._veils[0]].kind = "inert"
            self._stack[keeper].kind = "mute"
            del self._veils[:bisect_left(self._veils, keeper)]
            del self._lasting[:bisect_left(self._lasting, keeper)]

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
            if tag not in _HEAD_TAGS:
                self._leave_head()
            if not self._inert:
                self._void(tag, a)
            return
        self._implied_end(tag)
        mute = tag in _MUTE_TAGS or _is_hidden(a)
        if len(self._stack) >= _MAX_DEPTH:
            # Deeper than any mail nests: the element is not tracked. What it
            # would have hidden is still hidden, by counting its own tag
            # until it closes.
            if mute and not self._inert:
                self._overflow = [tag, 1]
            return
        if self._inert:
            self._push(_Element(tag, "inert"), mute)
        elif mute:
            self._push(_Element(tag, "mute"), True)
            self._inert += 1
        else:
            self._push(self._open(tag, a))

    def _open(self, tag: str, a: dict) -> _Element:
        blk = self._blk
        classes = _classes_of(a)
        ident = (a.get("id") or "").lower()
        # Marks are written at the top level only, where a line of the text is
        # a line of the block: inside a quote, a list item or a table cell the
        # line gets a prefix, and a mark that is not at its start is no mark.
        marks = self.marks and len(self._blocks) == 1
        # A container that is finished as a block of its own (a quote, a list
        # item, a cell) hands each of its lines to the one around it, with a
        # prefix. Past this depth containers are written into the block they
        # stand in, so a line is copied and prefixed a bounded number of
        # times whatever a mail nests.
        deep = len(self._blocks) > _MAX_BLOCKS
        if marks:
            if ident.endswith(_HISTORY_IDS):
                blk.para()
                blk.emit(_MARK_HARD)
            elif tag == "div" and _BORDER_TOP_RE.search(_style_of(a)):
                blk.para()
                blk.emit(_MARK_SOFT)
        cited = classes & _QUOTE_ANY_CLASSES or ident.startswith("yahoo_quoted")
        # type=cite is what the mail clients write. A cite attribute alone is
        # HTML's way to name the source of any quotation — a testimonial in a
        # newsletter — unless it names a mail ("mid:", as Thunderbird does).
        if tag == "blockquote" and (
                (a.get("type") or "").lower() == "cite"
                or (a.get("cite") or "").lower().startswith("mid:")
                or classes & _QUOTE_BLOCK_CLASSES):
            cited = True
        if cited:
            blk.para()
            if deep:                # still inside the outer quotes' ">" lines
                return _Element(tag, "paragraph")
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
            if deep:
                return _Element(tag, "block")
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
            if deep:
                blk.brk()
                return _Element(tag, "block")
            if self._find(("tr",), ("table",)) is None:
                self._push(_Element("tr", "row", []))           # a row-less cell
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
        open_at = self._where.get(tag)
        if not open_at:
            return
        index = open_at[-1]
        # A stray </div> inside a cell must not close the table around it.
        if tag not in _TABLE_TAGS and self._walls and self._walls[-1] > index:
            return
        if self._stale and self._stale[-1] == index:
            return                  # a link that has ended: a stray </a>
        self._pop_to(index)

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
                if self._afford(2, lines):
                    lines = ["> " + line if line else ">" for line in lines]
                blk.add(lines)
                blk.gap = True
            elif kind == "attribution":
                blk.add([_ATTR + line if line else line for line in lines])
            elif kind == "item":
                marker = element.data
                pad = " " * len(marker)
                if self._afford(len(marker), lines):
                    lines = [(marker if n == 0 else pad) + line if line else line
                             for n, line in enumerate(lines)]
                blk.add(lines)
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

    def _afford(self, width: int, lines: list) -> bool:
        """May these lines each get a prefix of `width`? (_PREFIX_ALLOWANCE)"""
        cost = width * len(lines)
        if cost > self._allowance:
            return False
        self._allowance -= cost
        return True

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
        # A link inside a link needs a table between them; the innermost few
        # are all that ever take the text, however many a mail stacks.
        for anchor in self._anchors[-_MAX_ANCHORS:]:
            anchor.data["parts"].append(data)

    def handle_data(self, data):
        if data.strip(" \t\n\r\f"):
            self._leave_head()
        if not self._inert and not self._overflow:
            self._write(data.translate(_INVISIBLE))

    def finish(self, whole: bool = True) -> list:
        if whole:
            self.close()
        self._pop_to(0)
        return self._blocks[0].done()


def _convert(html: str, marks: bool = False, cut: bool = False):
    """``(text, tracking pixels)`` of an HTML part.

    `cut`: the markup is the beginning of a longer part (see _markup_read).
    The parser is then not told that it has ended. Told so, it would write
    out what it holds back as unfinished — a tag, a comment, as far as they
    got — as text, or not, by its version; this way none does. Only text is
    not to wait for more of itself, which a blank sees to.
    """
    converter = _TextConverter(
        marks, _PREFIX_ALLOWANCE * len(html) + _PREFIX_FLOOR)
    try:
        converter.feed(html + " " if cut else html)
        lines = converter.finish(whole=not cut)
    except Exception:
        # A document that trips the parser still has words in it.
        lines = _html.unescape(re.sub(r"<[^<>]*>", " ", html)).split("\n")
    out = []
    for line in lines:
        # The trim is handed the signature delimiter with its blank; the
        # public rendering has no trailing blanks at all.
        line = line.replace("\xa0", " ")
        line = _rstrip(line) if marks else line.rstrip()
        out.append(line if line.strip(" \u200c\u200d\u2060") else "")
    text = "\n".join(_squeeze(out))
    if not marks:
        text = _no_marks(text)      # belt: no input reaches a line raw
    return text, converter.pixels


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
# pieces (see _clean_stylesheet for why a browser cannot read it differently
# than it was built). So the browser's parse of the output cannot differ from
# the parse made here.

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
# End tags a mail may leave out, and a browser then supplies: a paragraph
# ends where the next begins, a list item at the next item, a cell at the
# next cell, a row at the next row. The sanitiser writes them, or it would
# count a long mail written that way as nested once per paragraph and unwrap
# everything past _MAX_DEPTH. It writes one only where a browser reading the
# output would end the very same element at the very same place — the tree
# of the document is the mail's with the tag and without it:
#
#   * <p> ends the open paragraph, and so does the start tag of one of
#     these — a rule, a heading, a list, a table, a <div> — as long as
#     nothing but inline markup has been opened since the paragraph began.
#     A part of a table ends it in a table only: outside one a browser drops
#     that tag, the paragraph is as open as before, and its own </p> is
#     still its end.
_ENDS_P = frozenset((
    "address", "article", "aside", "blockquote", "caption", "center",
    "colgroup", "dd", "details", "div", "dl", "dt", "figcaption", "figure",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
    "main", "nav", "ol", "pre", "section", "summary", "table", "tbody", "td",
    "tfoot", "th", "thead", "tr", "ul"))
#   * <li> ends the open item, <dt> and <dd> the open term or description —
#     unless one of these was opened inside it, which makes the new one its
#     descendant. (HTML's rule: a <div> or a <p> in between does not.)
_ITEM_ENDS = {"li": ("li",), "dd": ("dd", "dt"), "dt": ("dd", "dt")}
_ITEM_STOPS = tuple(sorted(_ENDS_P - {"address", "div", "hr"}))
#   * Neither a paragraph nor an item is ended with one of these open in
#     it: a browser opens them again in the next paragraph or item, and
#     only it can. That end is left to the browser, and the element keeps
#     its level. (The known limit: a mail that leaves a <b> or a <font> open
#     in every paragraph still nests once for each, and past _MAX_DEPTH its
#     paragraphs run together. Writing those ends would mean reopening what
#     the browser carries over, exactly as it does.)
_CARRIED_TAGS = (
    "a", "b", "big", "code", "em", "font", "i", "s", "small", "strike",
    "strong", "tt", "u")
#   * In a table, a cell ends the cell before it, a row the row, a group of
#     rows the group: the outermost of them that belongs to the innermost
#     open table. (A cell ends what was opened in it, carried or not.
#     Outside a table a browser drops these tags, so nothing ends there.)
_TABLE_ENDS = {
    "td": (("td", "th"),),
    "th": (("td", "th"),),
    "tr": (("tr",), ("td", "th")),
    "thead": (("thead", "tbody", "tfoot"), ("tr",), ("td", "th")),
    "tbody": (("thead", "tbody", "tfoot"), ("tr",), ("td", "th")),
    "tfoot": (("thead", "tbody", "tfoot"), ("tr",), ("td", "th")),
}
_TABLE_PARTS = ("td", "th", "tr", "thead", "tbody", "tfoot")
# … and every tag that is one only in a table: outside, a browser drops it.
_TABLE_ONLY = _TABLE_PARTS + ("caption", "colgroup")
# What an end tag does not reach across: a stray </div> inside a cell must
# not close the table around it, nor a stray </li> the list it stands in,
# nor a stray </tr> a row of the table around this one. (With the end tags
# above written, an end tag written twice no longer meets an element that
# was left open, and would otherwise end the next one out.) To a paragraph's
# end tag a cell is a wall in a table only: outside one a browser drops the
# <td>, and </p> ends the paragraph — dropped behind such a wall, it would
# leave what follows a hidden paragraph written into it. (Unless a carried
# element is open in the paragraph: that end is the browser's, as above.)
_END_WALLS = ("td", "th", "table")
_ITEM_END_WALLS = _END_WALLS + ("ul", "ol")
_PART_END_WALLS = ("table",)
_SIZE_ATTR_RE = re.compile(r"\d{1,5}(?:\.\d+)?(?:%|px)?")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_CSS_PROPERTY_RE = re.compile(r"-?[a-z][a-z0-9-]*")
# A value, by the characters one is made of. No colon, backslash, angle
# bracket, brace, semicolon or at-sign can reach it: no URL scheme, no CSS
# escape, no at-rule, no way out of a block. In a style ATTRIBUTE a value may
# hold strings (the attribute ends where HTML says, whatever CSS makes of
# it); in the style SHEET it may not — see _clean_stylesheet.
_CSS_VALUE_RE = re.compile(r"[\w\s#%.,()\-+/'\"!*]*")
_CSS_BARE_VALUE_RE = re.compile(r"[\w\s#%.,()\-+/!*]*")
# A string that says no more than the words in it (a font's name, a class):
# in the sheet it is written without its quotes.
_CSS_QUOTED_RE = re.compile(r"\"([\w \-]*)\"|'([\w \-]*)'")
_CSS_QUOTED_WORD_RE = re.compile(r"\"([\w-]+)\"|'([\w-]+)'")
# The name before a parenthesis. Anchored where a name starts: a pattern
# free to start anywhere reads a long word once from each of its letters.
_CSS_FUNCTION_RE = re.compile(r"(?<![\w-])([\w-]*)\s*\(")
_CSS_FUNCTIONS = frozenset((
    "rgb", "rgba", "hsl", "hsla", "calc", "min", "max", "clamp",
    "linear-gradient", "radial-gradient", "repeating-linear-gradient",
    "repeating-radial-gradient"))
_CSS_BAD_WORDS = ("expression", "behavior", "binding", "script")
_CSS_BAD_PROPERTIES = frozenset(("behavior", "-moz-binding"))
# A selector, by the characters one is made of — none of which can open a
# tag, a string or a comment, close a block or start an escape.
_CSS_SELECTOR_RE = re.compile(r"[\w\s.#:,>+~*\[\]=()^$|%-]{1,500}")
_CSS_SELECTOR_FUNCTIONS = frozenset((
    "not", "is", "where", "lang", "dir", "nth-child", "nth-last-child",
    "nth-of-type", "nth-last-of-type"))
# "@media", and the query after it. The keyword ends where a name would:
# a mail may write its query right up against it ("@media(max-width:600px)").
_CSS_MEDIA_RE = re.compile(r"@media(?![\w-])([\w\s(),:.%/-]{0,300})", re.I)
_CSS_BRACE_RE = re.compile(r"[{}]")
# What the finished sheet must not hold (the last word on _clean_stylesheet).
_CSS_SHEET_FORBIDDEN_RE = re.compile(r"[<\\\"']|/\*|@(?!media[\s({])", re.I)
_CSS_SHEET_CALL_RE = re.compile(r"(?<![\w-])([\w-]*)\(")
_CSS_SHEET_CALLS = _CSS_FUNCTIONS | _CSS_SELECTOR_FUNCTIONS | {""}
#: A mail's style sheets beyond this size are dropped whole.
_CSS_MAX_CHARS = 200_000
#: How much the document may grow by showing inline images AGAIN: each is
#: written out once, and repeated only while the repeats fit in this.
_IMAGE_REPEAT_CHARS = 1_000_000
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
    has no end, which a sheet of nothing but ``/*`` turns into minutes. The
    pieces are joined by a blank, so what stood on either side of a comment
    cannot meet and spell the start of a new one.
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


def _css_balanced(text: str) -> bool:
    """Does every string, parenthesis and bracket in `text` close within it?

    One left open reads on into whatever is written after it: a string
    swallows the brace that ends its rule, and what was checked as the next
    rule's selector becomes the rest of this rule's value.
    """
    quote = None
    closers = []
    for char in text:
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "(":
            closers.append(")")
        elif char == "[":
            closers.append("]")
        elif char in ")]":
            if not closers or closers.pop() != char:
                return False
    return quote is None and not closers


def _css_unquoted(match) -> str:
    return match.group(1) or match.group(2) or ""


def _clean_style(value: str, strings: bool = True) -> str:
    """Style declarations with everything that can load or run taken out.

    Each declaration is kept only if its property is a plain name and its
    value is made of harmless characters and allow-listed functions, with
    every string and parenthesis closed — so no ``url()``, ``image-set()``,
    ``expression()``, ``@import``, ``behavior``, no escape that could spell
    one of them. ``position: fixed`` goes too: a mail has no business pinning
    itself over its own text.

    Without `strings` (the style sheet) a value holds no quote at all: a
    string that is only words loses its quotes, any other loses its
    declaration.
    """
    allowed = _CSS_VALUE_RE if strings else _CSS_BARE_VALUE_RE
    kept = []
    for declaration in _css_uncomment(value).split(";"):
        prop, colon, val = declaration.partition(":")
        prop = prop.strip().lower()
        val = " ".join(val.split())
        if (not colon or not val or len(val) > 300
                or not _CSS_PROPERTY_RE.fullmatch(prop)
                or prop in _CSS_BAD_PROPERTIES):
            continue
        if not strings:
            val = _CSS_QUOTED_RE.sub(_css_unquoted, val).strip()
        if not val or not allowed.fullmatch(val) or not _css_balanced(val):
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


def _css_selector(prelude: str) -> str | None:
    """The selector as it may be written into the sheet, or None.

    Made of selector characters only, its brackets and parentheses closed,
    its pseudo-class functions on the list, and no quote in it: an attribute
    value that is one word is written bare, any other costs the rule.
    """
    selector = _CSS_QUOTED_WORD_RE.sub(_css_unquoted, prelude)
    if (not _CSS_SELECTOR_RE.fullmatch(selector)
            or not _css_balanced(selector)
            or any(name.lower() not in _CSS_SELECTOR_FUNCTIONS
                   for name in _CSS_SHEET_CALL_RE.findall(selector))):
        return None
    return selector


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
            # Its QUERY may call nothing — the keyword is not part of it, and
            # is written with a blank after it, so that the finished sheet
            # holds no "media(" for a call either.
            media = None if nested else _CSS_MEDIA_RE.fullmatch(prelude)
            query = media.group(1).strip() if media else ""
            if (media and _css_balanced(query)
                    and not any(_CSS_SHEET_CALL_RE.findall(query))):
                inner: list = []
                _css_rules(body, inner, True)
                if inner:
                    out.append("%s{%s}" % (
                        ("@media " + query).rstrip(), "".join(inner)))
        elif "{" not in body:
            selector = _css_selector(prelude)
            declarations = _clean_style(body, strings=False) if selector else ""
            if declarations:
                out.append("%s{%s}" % (selector, declarations))
        position = close + 1


def _css_sheet_is_inert(sheet: str) -> bool:
    """The last word on a rebuilt sheet, asked of the text itself.

    No ``<``, no backslash, no quote, no ``/*``, no at-sign but ``@media``,
    and no name before a ``(`` that is not on a list. Whatever the code that
    built the sheet did, a text of which this holds has no string, no
    comment, no escape and no ``url()`` in it.
    """
    return not _CSS_SHEET_FORBIDDEN_RE.search(sheet) and all(
        name.lower() in _CSS_SHEET_CALLS
        for name in _CSS_SHEET_CALL_RE.findall(sheet))


def _clean_stylesheet(css: str) -> str:
    """A mail's ``<style>`` content, rebuilt from the rules that are harmless.

    Not filtered but rebuilt: every rule emitted is a selector made of
    selector characters, and declarations that passed :func:`_clean_style`.
    Nothing else of the sheet survives. What does survive is what makes a
    mail look like itself on a phone: its classes and its ``@media`` rules.

    Why a browser reads the result exactly as it was built. CSS has four
    constructs that change what the text after them means: a string, a
    comment, an escape and an unquoted ``url(``. The rebuilt sheet has no
    quote character, no ``/*``, no backslash, and no function name off the
    lists — so none of the four can begin in it. What is left to nest is
    brackets, and every piece closes its own: ``(`` and ``[`` within the
    selector or the value they open in, ``{`` by the rule that wrote it.
    The rules a browser sees are therefore the rules checked here, each
    with the selector and the declarations checked here — and no piece
    holds ``<`` (the ``<style>`` element cannot end early), ``url()``,
    ``@import`` or ``@font-face``. :func:`_css_sheet_is_inert` asks all of
    that of the finished text once more, and a sheet that fails is dropped
    whole.

    (The parse made HERE of the mail's sheet is not string-aware, and need
    not be: a brace inside a string of the mail's can only make this code
    cut the sheet into different pieces than the mail's author meant, and
    each piece is then judged on its own.)
    """
    if len(css) > _CSS_MAX_CHARS:
        return ""
    css = _css_uncomment(css)
    css = css.replace("<!--", " ").replace("-->", " ")   # Outlook's wrapping
    out: list = []
    _css_rules(css, out, False)
    sheet = "".join(out)
    return sheet if _css_sheet_is_inert(sheet) else ""


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


def _cid_key(content_id) -> str:
    """A content-id as it is compared: no brackets, no case."""
    return str(content_id).strip().strip("<>").lower()


def _is_image_url(source) -> bool:
    """A ``data:`` URL of an image, with nothing in it that could end the
    attribute it is written into."""
    return bool(isinstance(source, str)
                and re.match(r"data:image/[\w.+-]+[;,]", source, re.I)
                and not _CONTROL_RE.search(source)
                and not re.search(r"[\s\"'<>]", source))


def _cid_reference(value: str | None) -> str | None:
    """The key (see :func:`_cid_key`) a ``cid:`` reference names, or None."""
    if not value:
        return None
    value = value.strip()
    if value[:4].lower() != "cid:":
        return None
    return _cid_key(unquote(value[4:]))


class _Sanitizer(HTMLParser):
    def __init__(self, cid_map=None):
        super().__init__(convert_charrefs=True)
        # Keyed and judged once for the whole mail: a mail of a thousand
        # images and a thousand parts is a thousand look-ups, and no image is
        # read through once per reference to it.
        self._cids: dict = {}
        for key, source in (cid_map or {}).items():
            if _is_image_url(source):
                self._cids.setdefault(_cid_key(key), source)
        self._shown: set = set()
        self._repeats = _IMAGE_REPEAT_CHARS
        self._out: list = []
        self._open: list = []       # emitted and not yet closed
        self._open_at: dict = {}    # tag → where in _open it is open
        # Where in _open stands what a browser has ended by itself and no
        # end tag was written for (see _ENDS_P): not to be ended again.
        self._ended: set = set()
        # The outermost open element whose content is dropped, and how many
        # of its own tag are open inside it. Leaving the state early only
        # ever hands more markup to the allow-lists, never past them.
        self._drop = None
        self._drop_depth = 0
        # Inside a dropped <head>: the element of its own (title, style, …)
        # whose text is being read, if any.
        self._head_child = None
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

    def _image(self, value: str | None) -> str | None:
        """The ``data:`` URL of a ``cid:`` image, when the mail carries it.

        An image is written out where the mail first shows it. Shown again,
        it is written again only within an allowance: a bullet may repeat a
        hundred times, but twenty bytes of markup must not each become
        another megabyte of the document.
        """
        key = _cid_reference(value)
        source = self._cids.get(key) if key is not None else None
        if source is None:
            return None
        if key in self._shown:
            if len(source) > self._repeats:
                return None
            self._repeats -= len(source)
        self._shown.add(key)
        return source

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
            elif (self._drop == "head" and self._head_child is None
                    and tag not in _VOID_TAGS):
                self._head_child = tag
            return
        if tag in _DROP_TAGS:
            self._drop, self._drop_depth = tag, 1
            self._head_child = None
            return
        if tag not in _SAFE_TAGS:
            return                  # unwrapped: the tag goes, its content stays
        if self._open:
            self._implied_end(tag)
        if tag not in _SAFE_VOID and len(self._open) >= _MAX_DEPTH:
            return                  # deeper than any mail nests: unwrapped too
        extra = ""
        if tag == "img":
            if is_tracking_pixel(None, None, attrs):
                return
            source = self._image(dict(attrs).get("src"))
            if source:
                extra = ' src="%s"' % _html.escape(source, quote=True)
        elif tag == "a":
            href = _safe_href(dict(attrs).get("href"))
            if href:
                extra = (' href="%s" target="_blank" rel="noopener noreferrer"'
                         % _html.escape(href, quote=True))
        self._out.append("<%s%s%s>" % (tag, extra, self._attrs(tag, attrs)))
        if tag not in _SAFE_VOID:
            self._open_at.setdefault(tag, []).append(len(self._open))
            self._open.append(tag)

    def _close_to(self, index: int) -> None:
        while len(self._open) > index:
            tag = self._open.pop()
            self._open_at[tag].pop()
            self._ended.discard(len(self._open))
            self._out.append("</%s>" % tag)

    def _last(self, tags) -> int:
        """Where the innermost open element with one of these tags is, or -1."""
        best = -1
        for tag in tags:
            open_at = self._open_at.get(tag)
            if open_at and open_at[-1] > best:
                best = open_at[-1]
        return best

    def _implied_end(self, tag: str) -> None:
        """Write the end tag this start tag stands for (see _ENDS_P).

        Asked of the index like every other question about what is open,
        and of the innermost element first: in a mail written without end
        tags that is the paragraph, the item or the cell before this one.
        """
        if tag in _TABLE_ENDS:
            self._implied_table_end(tag)
        targets = _ITEM_ENDS.get(tag)
        if targets and self._open:
            top = len(self._open) - 1
            found = top if self._open[top] in targets else self._last(targets)
            if found < 0 or found in self._ended:
                pass
            elif found == top:
                self._close_to(found)
            elif self._last(_ITEM_STOPS) > found:
                pass                # the new one is its descendant
            elif self._last(_CARRIED_TAGS) > found:
                self._ended.add(found)
            else:
                self._close_to(found)
        if tag == "p" or tag in _ENDS_P:
            self._implied_paragraph_end(tag)

    def _implied_paragraph_end(self, tag: str) -> None:
        """The same for the open paragraph, which this start tag ends.

        Its end tag is written wherever a browser ends it with all that was
        opened in it — which is every time but one: with a carried element
        open in it, the paragraph is only known to be ended. (A mail whose
        paragraphs end at a rule, a heading, a list or a table would
        otherwise count as nested once for each, like one without </p>.)
        """
        paragraphs = self._open_at.get("p")
        if not paragraphs or paragraphs[-1] in self._ended:
            return
        found = paragraphs[-1]
        in_table = bool(self._open_at.get("table"))
        if not in_table and tag in _TABLE_ONLY:
            return                  # no table: a browser drops this tag
        if found < len(self._open) - 1:
            if in_table and self._last(_END_WALLS + ("caption",)) > found:
                return              # the paragraph is not in this cell
            if (self._last(_CARRIED_TAGS) > found
                    or self._last(_TABLE_ONLY) > found):
                # (A part of a table in a paragraph that has not ended is
                # one the browser dropped. No end tag is written across it:
                # its own would then meet another element.)
                self._ended.add(found)
                return
        self._close_to(found)

    def _implied_table_end(self, tag: str) -> None:
        """The same for a cell, a row or a group of rows (see _TABLE_ENDS)."""
        tables = self._open_at.get("table")
        if not tables:
            return
        for targets in _TABLE_ENDS[tag]:
            found = self._last(targets)
            if found > tables[-1]:
                break
        else:
            return
        if found in self._ended:
            return
        if (self._last(_CARRIED_TAGS) > found
                and self._carried_outside_a_cell(found)):
            # Opened in the row itself, not in a cell: the browser ends the
            # row and carries it over. Left to the browser, all of it.
            for part in _TABLE_PARTS:
                for at in reversed(self._open_at.get(part, ())):
                    if at < found:
                        break
                    self._ended.add(at)
            return
        self._close_to(found)

    def _carried_outside_a_cell(self, found: int) -> bool:
        """Is a carried element open above `found` that no cell above
        `found` holds? (The lowest of them, against the lowest cell.)"""
        lowest = len(self._open)
        for tag in _CARRIED_TAGS:
            open_at = self._open_at.get(tag)
            if open_at and open_at[-1] > found:
                lowest = min(lowest, open_at[bisect_right(open_at, found)])
        for tag in ("td", "th"):
            open_at = self._open_at.get(tag)
            if open_at and open_at[-1] >= found:
                if open_at[bisect_left(open_at, found)] < lowest:
                    return False
        return True

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False
        if tag == self._head_child:
            self._head_child = None
        if self._drop:
            if tag == self._drop:
                self._drop_depth -= 1
                if not self._drop_depth:
                    self._drop = None
            return
        if tag in _SAFE_VOID or tag not in _SAFE_TAGS:
            return
        # Asked of the index, not walked: a mail of stray end tags inside a
        # deep nest would walk the nest for each.
        open_at = self._open_at.get(tag)
        if not open_at:
            return
        index = open_at[-1]
        if tag == "li":
            walls = _ITEM_END_WALLS
        elif tag not in _TABLE_TAGS:
            walls = _END_WALLS
        else:
            walls = () if tag == "table" else _PART_END_WALLS
        for wall in walls:
            inside = self._open_at.get(wall)
            if inside and inside[-1] > index:
                if (tag == "p" and not self._open_at.get("table")
                        and self._last(_CARRIED_TAGS) < index):
                    break           # no table, no cell: see _END_WALLS
                return
        if index in self._ended and index < len(self._open) - 1:
            # Ended by the browser long ago: this end tag is a stray one to
            # it, and would here end what was opened since instead.
            return
        self._close_to(index)

    def handle_data(self, data):
        if self._in_style:
            self.css.append(data)
        elif (self._drop == "head" and self._head_child is None
                and data.strip(" \t\n\r\f")):
            # An unclosed head ends at the first text that is not its own —
            # as it does in a browser, which shows that text.
            self._drop = None
        if not self._drop:
            self._out.append(_html.escape(_CONTROL_RE.sub("", data), quote=False))

    def finish(self) -> str:
        self.close()
        self._close_to(0)
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

    Written: the end tags HTML lets a mail leave out — of paragraphs, list
    items, table cells and rows — where a browser would supply them, so a
    long mail without them is a row of paragraphs and not a nest of them.

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
