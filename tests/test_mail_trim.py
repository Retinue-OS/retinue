#!/usr/bin/env python3
"""The deterministic mail trim, against a corpus of mails as clients write them.

`scripts/mail_trim.py` turns a mail into "its own new text" for the chat
bubble, renders an HTML part as text, and makes it safe for the Original view.
Its heuristics are only as good as the mails they were tried on, so this file
is mostly corpus: synthetic mails in the shapes Gmail (web and app), Apple
Mail, Outlook (desktop, web, mobile), Thunderbird, Yahoo and Proton produce —
plain, HTML and multipart; top-, bottom- and inline-posted; replies and
forwards; in the languages the word tables cover and in some they do not.

Every corpus case is also checked for the two properties that must hold for
any mail whatsoever:

  * **idempotence** — trimming the trimmed text again changes nothing;
  * **nothing invented** — every line of a plain mail's result is a line of
    that mail (or one of its quoted lines unquoted, for a forward), so the
    trim can lose words but never add any.

What matters more than recall is pinned down by cases of its own: an author's
lines between quotes, a lead-in above a quote, a forward's body, ">" lines
that are code, an itinerary that looks like a header block, the word
"confidential" in a sentence — all of it must stay.

No real people's data: every name, address and company is made up.

    python3 tests/test_mail_trim.py
"""
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# The scheme that runs code, assembled so this file never holds it whole.
JS = "java" + "script" + ":"


def _load():
    spec = importlib.util.spec_from_file_location(
        "mail_trim", SCRIPTS_DIR / "mail_trim.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mail_trim"] = mod
    spec.loader.exec_module(mod)
    return mod


mt = _load()

ANN = "Ann Example <ann@example.org>"
BOB = "Bob Example <bob@example.net>"
ATTRIBUTION = "On Mon, 3 Mar 2025 at 14:02, %s wrote:" % ANN


# ── The corpus ───────────────────────────────────────────────────────────────

CASES = []


def case(name, plain=None, html=None, subject=None, text=None, keeps=(),
         drops=(), state=None, removed=(), unchanged=False):
    """One mail and what its trim must be.

    `text` is the exact result; `unchanged` says the result is the mail itself
    (trailing blanks aside); `keeps`/`drops` are substrings that must / must
    not be in it; `removed` names the counters that must be above zero.
    """
    CASES.append(dict(name=name, plain=plain, html=html, subject=subject,
                      text=text, keeps=keeps, drops=drops, state=state,
                      removed=removed, unchanged=unchanged))


# ── Replies, plain text ──

case("gmail web, plain, top-posted",
     plain="Sounds good, Tuesday works.\n\n" + ATTRIBUTION + "\n\n"
           "> Can we meet next week?\n>\n"
           "> Tuesday or Wednesday would suit me.\n>\n",
     text="Sounds good, Tuesday works.", state="deterministic",
     removed=("quote",))

case("gmail, attribution folded before the verb",
     plain="Yes, that is fine.\n\n"
           "On Mon, Mar 3, 2025 at 2:02 PM Ann Margaret Example <ann.example@example.org>\n"
           "wrote:\n\n> Is the draft ok?\n",
     text="Yes, that is fine.")

case("gmail, attribution folded inside the address",
     plain="Done.\n\n"
           "On Mon, 3 Mar 2025 at 14:02, Ann Example via Project List <\n"
           "project-list@lists.example.org> wrote:\n\n> Please review.\n",
     text="Done.")

case("gmail, attribution folded before the address",
     plain="Done.\n\n"
           "On Mon, 3 Mar 2025 at 14:02, Ann Margaret Example-Longname-Verylongname\n"
           "<ann.example@example.org> wrote:\n\n> Please review.\n",
     text="Done.")

case("apple mail on iPhone, attribution inside the quote",
     plain="Perfect, see you then.\n\nSent from my iPhone\n\n"
           "> On 3 Mar 2025, at 14:02, %s wrote:\n> \n"
           "> \ufeffShall we say 10 o'clock?\n" % ANN,
     text="Perfect, see you then.", removed=("quote", "signature"))

case("apple mail on the desktop",
     plain="Thanks Ann!\n\n"
           "> On 3 Mar 2025, at 14:02, %s wrote:\n> \n"
           "> Here is the file.\n> \n> Ann\n" % ANN,
     text="Thanks Ann!")

case("thunderbird, bottom-posted, with a signature",
     plain="On 3/3/25 14:02, Ann Example wrote:\n"
           "> Could you send me the quarterly numbers?\n>\n> Thanks, Ann\n\n"
           "Sure, they are attached.\n\nBob\n\n"
           "-- \nBob Example\nExample GmbH\n",
     text="Sure, they are attached.\n\nBob", removed=("quote", "signature"))

case("interleaved reply keeps every answer",
     plain="Hi Ann,\n\n"
           "On 03.03.2025 14:02, Ann Example wrote:\n"
           "> Can you make Tuesday?\n\n"
           "Yes, any time after ten.\n\n"
           "> And will you bring the contract?\n\n"
           "I will, two copies.\n\n"
           "> One more thing: parking is behind the building.\n\n"
           "Good to know.\n\nBob\n",
     text="Hi Ann,\n\nYes, any time after ten.\n\nI will, two copies.\n\n"
          "Good to know.\n\nBob")

case("interleaved reply with no blank lines at all",
     plain="> Can you make Tuesday?\nYes.\n"
           "> Will you bring the contract?\nNo, Carol has it.\n",
     text="Yes.\n\nNo, Carol has it.")

case("nested quotes go with their attributions",
     plain="Agreed.\n\n"
           "Am 03.03.2025 um 14:02 schrieb Ann Example:\n"
           "> Ich bin dafür.\n>\n"
           "> Am 02.03.2025 um 09:15 schrieb Bob Example:\n"
           ">> Sollen wir das Budget erhöhen?\n"
           ">>> Eine frühere Nachricht\n",
     text="Agreed.")

# The attribution line in the languages of the table, and in two that are
# not in it: the shape alone (a colon at the end, an address in brackets or a
# date and a time) carries those.
for _lang, _line in (
        ("de", "Am 03.03.2025 um 14:02 schrieb %s:" % ANN),
        ("de, no address", "Ann Example schrieb am 03.03.2025 um 14:02:"),
        ("fr", "Le lun. 3 mars 2025 à 14:02, %s a écrit\u00a0:" % ANN),
        ("fr, no address", "Le 03/03/2025 à 14h02, Ann Example a écrit :"),
        ("it", "Il giorno lun 3 mar 2025 alle ore 14:02 %s ha scritto:" % ANN),
        ("es", "El lun, 3 mar 2025 a las 14:02, Ann Example (<ann@example.org>) escribió:"),
        ("nl", "Op ma 3 mrt 2025 om 14:02 schreef %s:" % ANN),
        ("pt", "Em seg., 3 de mar. de 2025 às 14:02, %s escreveu:" % ANN),
        ("da", "Den man. 3. mar. 2025 kl. 14.02 skrev %s:" % ANN),
        ("sv", "Den mån 3 mars 2025 kl. 14:02 skrev %s:" % ANN),
        ("nb", "man. 3. mars 2025 kl. 14:02 skrev %s:" % ANN),
        ("fi", "ma 3. maalisk. 2025 klo 14.02 Ann Example (ann@example.org) kirjoitti:"),
        ("pl", "pon., 3 mar 2025 o 14:02 %s napisał(a):" % ANN),
        ("en, date only", "On 2025-03-03, Ann Example wrote:"),
        ("hu, not in the table", "2025. márc. 3., H 14:02 dátummal %s ezt írta:" % ANN),
        ("ja, not in the table", "2025年3月3日(月) 14:02 %s:" % ANN),
        ("ja, full-width colon", "2025年3月3日(月) 14:02 %s：" % ANN)):
    case("attribution by shape — " + _lang,
         plain="OK.\n\n%s\n\n> quoted line one\n> quoted line two\n" % _line,
         text="OK.", state="deterministic")

case("a lead-in above a quote stays",
     plain="Hi Ann,\n\nas you said:\n\n> the budget is fixed for this year\n\n"
           "I agree, so let us plan within it.\n",
     text="Hi Ann,\n\nas you said:\n\nI agree, so let us plan within it.")

case("a lead-in that mentions times stays",
     plain="Here are the two slots you offered (10:30 and 14:00):\n"
           "> Monday 10:30\n> Tuesday 14:00\n\nI take Tuesday.\n",
     text="Here are the two slots you offered (10:30 and 14:00):\n\n"
          "I take Tuesday.")

case("a lead-in with the verb but no date stays",
     plain="As you wrote:\n> we need two more weeks\n\nThat is fine with us.\n",
     text="As you wrote:\n\nThat is fine with us.")

case("the author's own line above an attribution is not folded into it",
     plain="I will look into the numbers and come back to you later this week\n"
           + ATTRIBUTION + "\n> When can I expect the report?\n",
     text="I will look into the numbers and come back to you later this week")

case("only a quote and +1",
     plain="+1\n\n" + ATTRIBUTION + "\n> Shall we order pizza for the team?\n"
           "> I would take two large ones.\n",
     text="+1", state="deterministic")

case("only a quote: falls back to the untrimmed mail",
     plain=ATTRIBUTION + "\n> Shall we order pizza?\n",
     unchanged=True, state="wanting")

case("only a phone signature and a quote: falls back too",
     plain="Sent from my iPhone\n\n"
           "> On 3 Mar 2025, at 14:02, %s wrote:\n> \n> Photo attached?\n" % ANN,
     unchanged=True, state="wanting")

case("yahoo, plain: the earlier mail quoted as bare text",
     plain="Great, thanks!\n\n"
           "    On Monday, March 3, 2025 at 02:02:11 PM GMT+1, %s wrote:  \n"
           " \n Here is the report you asked for.\nAnn\n" % ANN,
     text="Great, thanks!", removed=("quote",))

case("proton, plain",
     plain="Understood.\n\nSent with Proton Mail secure email.\n\n"
           "On Monday, March 3rd, 2025 at 2:02 PM, %s wrote:\n\n"
           "> Please keep this between us.\n" % ANN,
     text="Understood.", removed=("quote", "signature"))

case("CRLF line endings",
     plain="Fine.\r\n\r\n" + ATTRIBUTION + "\r\n> Hello\r\n> there\r\n",
     text="Fine.")

# ── Outlook, plain text ──

case("outlook desktop, -----Original Message-----",
     plain="Hi Ann,\n\nWorks for me.\n\nRegards,\nBob\n\n"
           "-----Original Message-----\n"
           "From: %s\nSent: Monday, March 3, 2025 2:02 PM\nTo: %s\n"
           "Subject: Meeting\n\nCan we meet next week?\n" % (ANN, BOB),
     text="Hi Ann,\n\nWorks for me.\n\nRegards,\nBob", removed=("quote",))

case("outlook web, the rule and a folded Cc list",
     plain="Works for me.\n\n________________________________\n"
           "From: %s\nSent: Monday, March 3, 2025 2:02 PM\nTo: %s\n"
           "Cc: Carol Example <carol@example.net>; Dave Example <dave@example.net>; Erin\n"
           "Example <erin@example.net>\n"
           "Subject: Meeting\n\nCan we meet next week?\n" % (ANN, BOB),
     text="Works for me.")

case("outlook mobile, app line above the rule",
     plain="Ok, will do.\n\nGet Outlook for iOS<https://aka.ms/o0ukef>\n"
           "________________________________\n"
           "From: %s\nSent: Monday, March 3, 2025 2:02:11 PM\nTo: %s\n"
           "Subject: Re: Meeting\n\nPlease confirm.\n" % (ANN, BOB),
     text="Ok, will do.", removed=("quote", "signature"))

case("outlook header block without any address",
     plain="Fine.\n\nFrom: Ann Example\nSent: Monday, March 3, 2025 2:02 PM\n"
           "To: Bob Example\nSubject: Meeting\n\nCan we meet?\n",
     text="Fine.")

case("header block with bold markers, as a plain rendering of HTML has them",
     plain="See below.\n\n*From:* %s\n*Sent:* Monday, March 3, 2025 2:02 PM\n"
           "*To:* %s\n*Subject:* Meeting\n\nCan we meet?\n" % (ANN, BOB),
     text="See below.")

for _lang, _block in (
        ("de", "Von: %s\nGesendet: Montag, 3. März 2025 14:02\nAn: %s\nBetreff: Besprechung"),
        ("de, GMX order", "Gesendet: Montag, 03. März 2025 um 14:02 Uhr\nVon: %s\nAn: %s\nBetreff: Besprechung"),
        ("fr", "De\u00a0: %s\nEnvoyé\u00a0: lundi 3 mars 2025 14:02\nÀ\u00a0: %s\nObjet\u00a0: Réunion"),
        ("it", "Da: %s\nInviato: lunedì 3 marzo 2025 14:02\nA: %s\nOggetto: Riunione"),
        ("es", "De: %s\nEnviado el: lunes, 3 de marzo de 2025 14:02\nPara: %s\nAsunto: Reunión"),
        ("nl", "Van: %s\nVerzonden: maandag 3 maart 2025 14:02\nAan: %s\nOnderwerp: Vergadering"),
        ("pt", "De: %s\nEnviada em: segunda-feira, 3 de março de 2025 14:02\nPara: %s\nAssunto: Reunião"),
        ("da", "Fra: %s\nSendt: 3. marts 2025 14:02\nTil: %s\nEmne: Møde"),
        ("nb", "Fra: %s\nSendt: mandag 3. mars 2025 14:02\nTil: %s\nEmne: Møte"),
        ("sv", "Från: %s\nSkickat: den 3 mars 2025 14:02\nTill: %s\nÄmne: Möte"),
        ("fi", "Lähettäjä: %s\nLähetetty: maanantai 3. maaliskuuta 2025 14.02\nVastaanottaja: %s\nAihe: Kokous")):
    case("outlook header block — " + _lang,
         plain="OK.\n\n" + _block % (ANN, BOB) + "\n\nEarlier text.\n",
         text="OK.", state="deterministic", removed=("quote",))

for _lang, _line in (
        ("de", "-----Ursprüngliche Nachricht-----"),
        ("fr", "-----Message d'origine-----"),
        ("nl", "-----Oorspronkelijk bericht-----"),
        ("es", "-----Mensaje original-----"),
        ("samsung", "-------- Original message --------")):
    case("original-message separator — " + _lang,
         plain="OK.\n\n%s\nFrom: %s\nDate: 03/03/2025 14:02 (GMT+01:00)\n"
               "To: %s\nSubject: Meeting\n\nEarlier text.\n" % (_line, ANN, BOB),
         text="OK.")

case("an itinerary is not a header block",
     plain="Your trip is booked.\n\nFrom: Zurich HB\nTo: Bern\n"
           "Date: 03.03.2025 08:15\nSeat: 42\n\nHave a good journey.\n",
     unchanged=True, state="deterministic")

case("a meeting block is not a header block",
     plain="Let us meet.\n\nDate: 03.03.2025 14:00\nPlace: Room 5\n"
           "Topic: Budget\n\nSee you there.\n",
     unchanged=True)

case("headers pasted to talk about them are not a header block",
     plain="Look at the headers of the spam I got:\n\n"
           "From: Prince <prince@example.org>\nSubject: Urgent\nTo: me\n\n"
           "Clearly fake.\n",
     unchanged=True)

case("a sentence with a date, a time and 'wrote' is not an attribution",
     plain="Hi,\n\n1) yes\n2) no\n\nOn another note: the invoice from "
           "03.03.2025 14:02 wrote off the balance:\nall fine now.\n\nBob\n",
     unchanged=True)

case("nor is it one when it stands alone but does not end like one",
     plain="Ann wrote on 03.03.2025 at 14:02 that the following is open:\n\n"
           "the contract, the invoice and the handover.\n",
     unchanged=True)

case("a section heading between dashes is not a separator",
     plain="Notes from today.\n\n----- Agenda -----\n1. Budget\n2. Hiring\n\n"
           "----- Decisions -----\nBudget approved.\n",
     unchanged=True)

# ── Forwards ──

_FORWARDED = ("From: %s\nDate: Mon, 3 Mar 2025 at 14:02\n"
              "Subject: Quarterly numbers\nTo: %s\n\n\n"
              "Hi Bob,\n\nthe numbers are attached.\n\nAnn\n" % (ANN, BOB))

case("gmail forward keeps the forwarded message",
     plain="FYI, see below.\n\n---------- Forwarded message ---------\n" + _FORWARDED,
     unchanged=True, state="deterministic",
     keeps=("the numbers are attached.",))

case("a forward with no intro at all",
     plain="---------- Forwarded message ---------\n" + _FORWARDED,
     unchanged=True, keeps=("the numbers are attached.",))

case("apple mail forward: the client's quoting comes off",
     plain="Have a look.\n\nSent from my iPhone\n\nBegin forwarded message:\n\n"
           "> From: %s\n> Date: 3 March 2025 at 14:02:11 CET\n> To: %s\n"
           "> Subject: Quarterly numbers\n> \n> Hi Bob,\n> \n"
           "> the numbers are attached.\n" % (ANN, BOB),
     text="Have a look.\n\nBegin forwarded message:\n\n"
          "From: %s\nDate: 3 March 2025 at 14:02:11 CET\nTo: %s\n"
          "Subject: Quarterly numbers\n\nHi Bob,\n\n"
          "the numbers are attached." % (ANN, BOB),
     state="deterministic", removed=("signature",))

case("thunderbird forward, German",
     plain="Zur Info.\n\n-------- Weitergeleitete Nachricht --------\n"
           "Betreff: \tQuartalszahlen\nDatum: \tMon, 3 Mar 2025 14:02:11 +0100\n"
           "Von: \t%s\nAn: \t%s\n\nHallo Bob,\n\ndie Zahlen sind im Anhang.\n"
           % (ANN, BOB),
     unchanged=True)

for _lang, _line in (
        ("fr", "---------- Message transféré ---------"),
        ("es", "---------- Mensaje reenviado ---------"),
        ("it", "---------- Messaggio inoltrato ---------"),
        ("nl", "---------- Doorgestuurd bericht ---------"),
        ("pt", "---------- Mensagem encaminhada ---------"),
        ("sv", "---------- Vidarebefordrat meddelande ---------"),
        ("da", "---------- Videresendt meddelelse ---------"),
        ("yahoo", "----- Forwarded Message -----"),
        ("proton", "------- Forwarded Message -------")):
    case("forward marker — " + _lang,
         plain="Voilà.\n\n%s\n%s" % (_line, _FORWARDED),
         unchanged=True)

_OUTLOOK_FORWARD = ("FYI\n\nFrom: %s\nSent: Monday, March 3, 2025 2:02 PM\n"
                    "To: %s\nSubject: Quarterly numbers\n\n"
                    "Hi Bob, the numbers are attached.\n" % (ANN, BOB))

case("outlook forward, told apart by its subject",
     plain=_OUTLOOK_FORWARD, subject="FW: Quarterly numbers",
     text="FYI\n\n---------- Forwarded message ----------\n"
          "From: %s\nSent: Monday, March 3, 2025 2:02 PM\nTo: %s\n"
          "Subject: Quarterly numbers\n\n"
          "Hi Bob, the numbers are attached." % (ANN, BOB),
     state="deterministic")

case("the same mail without a subject reads as a reply",
     plain=_OUTLOOK_FORWARD, text="FYI")

case("a reply to a forward is a reply",
     plain=_OUTLOOK_FORWARD, subject="RE: FW: Quarterly numbers", text="FYI")

case("outlook forward, German prefix, separator replaced by the marker",
     plain="Zur Info\n\n-----Ursprüngliche Nachricht-----\n"
           "Von: %s\nGesendet: Montag, 3. März 2025 14:02\nAn: %s\n"
           "Betreff: Zahlen\n\nHallo Bob, anbei die Zahlen.\n" % (ANN, BOB),
     subject="WG: Zahlen",
     text="Zur Info\n\n---------- Forwarded message ----------\n"
          "Von: %s\nGesendet: Montag, 3. März 2025 14:02\nAn: %s\n"
          "Betreff: Zahlen\n\nHallo Bob, anbei die Zahlen." % (ANN, BOB))

case("outlook forward with no intro and no subject: all of it stays",
     plain="From: %s\nSent: Monday, March 3, 2025 2:02 PM\nTo: %s\n"
           "Subject: Quarterly numbers\n\nHi Bob, the numbers are attached.\n"
           % (ANN, BOB),
     unchanged=True)

case("a quoted forward under a reply goes with the quote",
     plain="Thanks for passing this on.\n\n" + ATTRIBUTION + "\n"
           "> FYI\n>\n> ---------- Forwarded message ---------\n"
           "> From: Carol Example <carol@example.net>\n> Subject: Offer\n>\n"
           "> Here is the offer.\n",
     text="Thanks for passing this on.")

case("'I forwarded this' is a sentence, not a marker",
     plain="I forwarded this to Bob:\n\nhe will answer by Friday.\n",
     unchanged=True)

# ── Signatures ──

case("the signature delimiter",
     plain="See you.\n\n-- \nBob Example\nExample GmbH\n+41 44 000 00 00\n",
     text="See you.", removed=("signature",))

case("the delimiter after a client stripped its blank",
     plain="See you.\n\n--\nBob Example\n", text="See you.")

case("a dash line with a long tail is not a signature",
     plain="Agenda\n--\n" + "\n".join("Point %d of the agenda" % n
                                      for n in range(1, 16)) + "\n",
     unchanged=True)

case("mailing-list footer under the delimiter",
     plain="I think the patch is fine.\n\n-- \n"
           "You received this message because you are subscribed to the "
           "Google Groups \"dev\" group.\nTo unsubscribe from this group, "
           "send an email to dev+unsubscribe@lists.example.org.\n",
     text="I think the patch is fine.")

for _line in (
        "Sent from my iPhone",
        "Sent from my Galaxy",
        "Sent from Yahoo Mail for iPhone",
        "Sent from Mail for Windows",
        "Get Outlook for Android<https://aka.ms/AAb9ysg>",
        "Von meinem iPhone gesendet",
        "Gesendet mit der GMX Mail App",
        "Diese Nachricht wurde von meinem Android Mobiltelefon mit WEB.DE Mail gesendet.",
        "Envoyé de mon iPhone",
        "Envoyé depuis mon appareil Galaxy",
        "Inviato da iPhone",
        "Inviato dal mio Galaxy",
        "Enviado desde mi iPhone",
        "Verzonden vanaf mijn iPhone",
        "Verstuurd vanaf mijn iPhone",
        "Enviado do meu iPhone",
        "Sendt fra min iPhone",
        "Skickat från min iPhone",
        "Lähetetty iPhonesta"):
    case("phone signature — " + _line,
         plain="Ok, see you at ten.\n\n%s\n" % _line,
         text="Ok, see you at ten.", state="deterministic",
         removed=("signature",))

case("'sent from' in the middle of a mail is a sentence",
     plain="The parcel was\nsent from the warehouse on Monday and should "
           "arrive on Friday.\n\nBest,\nBob\n",
     unchanged=True)

# ── Footers and disclaimers ──

_DISCLAIMERS = {
    "en": "This e-mail and any attachments are confidential and intended solely for the addressee. If you are not the intended recipient, please notify the sender immediately and delete this message.",
    "de": "Diese E-Mail enthält vertrauliche Informationen und ist ausschließlich für den Empfänger bestimmt. Sollten Sie nicht der richtige Adressat sein, informieren Sie bitte den Absender.",
    "fr": "Ce message et ses pièces jointes sont confidentiels et destinés exclusivement à leur destinataire. Si vous l'avez reçu par erreur, merci de le détruire et d'en avertir l'expéditeur.",
    "it": "Questo messaggio e i suoi allegati sono riservati e destinati esclusivamente al destinatario indicato. Se lo avete ricevuto per errore, vi preghiamo di cancellarlo.",
    "es": "Este mensaje y sus anexos son confidenciales y están dirigidos exclusivamente a su destinatario. Si lo ha recibido por error, le rogamos que lo elimine.",
    "nl": "Dit bericht is vertrouwelijk en uitsluitend bestemd voor de geadresseerde. Indien u dit bericht ten onrechte ontvangt, wordt u verzocht de afzender te informeren.",
    "pt": "Esta mensagem e os seus anexos são confidenciais e destinam-se exclusivamente ao destinatário. Se a recebeu por engano, por favor elimine-a e avise o remetente.",
    "da": "Denne e-mail er fortrolig og kun beregnet til den angivne modtager. Hvis du har modtaget den ved en fejl, bedes du slette den og give afsenderen besked.",
    "nb": "Denne e-posten er konfidensiell og kun ment for den angitte mottakeren. Hvis du har mottatt den ved en feil, ber vi deg slette den og varsle avsenderen.",
    "sv": "Detta meddelande är konfidentiellt och endast avsett för den angivna mottagaren. Om du har fått det av misstag ber vi dig radera det och meddela avsändaren.",
}
for _lang, _notice in _DISCLAIMERS.items():
    case("confidentiality notice — " + _lang,
         plain="Please find the offer attached.\n\nKind regards\nBob\n\n%s\n" % _notice,
         text="Please find the offer attached.\n\nKind regards\nBob",
         state="deterministic", removed=("footer",))

case("a bilingual notice, hard-wrapped",
     plain="Anbei das Angebot.\n\nBob\n\n"
           "Diese E-Mail enthält vertrauliche Informationen und ist\n"
           "ausschließlich für den Empfänger bestimmt. Sollten Sie nicht der\n"
           "richtige Adressat sein, informieren Sie bitte den Absender.\n\n"
           "This e-mail is confidential and intended solely for the\n"
           "addressee. If you are not the intended recipient, please notify\n"
           "the sender and delete it.\n",
     text="Anbei das Angebot.\n\nBob")

case("a notice glued right under the sign-off leaves the sign-off",
     plain="Please find the offer attached.\n\nKind regards\nBob\n%s\n"
           % _DISCLAIMERS["en"],
     text="Please find the offer attached.\n\nKind regards\nBob",
     removed=("footer",))

case("a glued notice, folded narrow: its first line goes with it",
     plain="Please find the offer attached.\n\nKind regards\nBob\n"
           "This e-mail and any attachments are\n"
           "confidential and intended solely for\n"
           "the addressee. If you are not the\n"
           "intended recipient, please notify the\nsender and delete it.\n",
     text="Please find the offer attached.\n\nKind regards\nBob")

case("an unsubscribe line glued under the sign-off",
     plain="The new price list is attached.\n\nBest,\nBob\n"
           "Unsubscribe: https://news.example.com/u/abc\n",
     text="The new price list is attached.\n\nBest,\nBob")

case("'confidential' in a sentence is the author's",
     plain="The attached report is confidential and should not leave the "
           "team until the board has approved it next week, so please be "
           "careful where you store it.\n\nBob\n",
     unchanged=True)

case("a notice above outlook history goes with it",
     plain="Agreed.\n\nBob\n\n%s\n\nFrom: %s\nSent: Monday, March 3, 2025 2:02 PM\n"
           "To: %s\nSubject: Offer\n\nIs the offer acceptable?\n"
           % (_DISCLAIMERS["en"], ANN, BOB),
     text="Agreed.\n\nBob", removed=("quote", "footer"))

case("a notice under the phone's line: both go",
     plain="Ok.\n\nSent from my iPhone\n\n%s\n" % _DISCLAIMERS["en"],
     text="Ok.", removed=("signature", "footer"))

case("a plain-text newsletter's footer",
     plain="ACME Weekly\n\nThis week: three new features and a bug fix.\n\n"
           "Read more: https://news.example.com/weekly/42\n\n"
           "You are receiving this email because you signed up at example.com.\n"
           "Unsubscribe: https://news.example.com/u/abc123\n\n"
           "ACME Inc., 1 Example Road, Springfield\n",
     text="ACME Weekly\n\nThis week: three new features and a bug fix.\n\n"
          "Read more: https://news.example.com/weekly/42",
     removed=("footer",))

for _lang, _line in (
        ("de", "Newsletter abbestellen: https://news.example.com/u/abc"),
        ("fr", "Se désabonner : https://news.example.com/u/abc"),
        ("it", "Per annullare l'iscrizione: https://news.example.com/u/abc"),
        ("es", "Para darse de baja haga clic aquí: https://news.example.com/u/abc"),
        ("nl", "Uitschrijven: https://news.example.com/u/abc"),
        ("pt", "Cancelar a subscrição: https://news.example.com/u/abc"),
        ("da", "Afmeld nyhedsbrevet: https://news.example.com/u/abc"),
        ("nb", "Meld deg av her: https://news.example.com/u/abc"),
        ("sv", "Avsluta prenumerationen: https://news.example.com/u/abc"),
        ("en, by mail", "To opt out, write to leave@news.example.com")):
    case("unsubscribe block — " + _lang,
         plain="Three new features this week.\n\n%s\n\nACME Inc., 1 Example Road\n" % _line,
         text="Three new features this week.", removed=("footer",))

case("'unsubscribe' in a sentence is the author's",
     plain="If you would rather not get these updates, just tell me and I "
           "will unsubscribe you.\n\nBob\n",
     unchanged=True)

case("an unsubscribe line at the top is not a footer",
     plain="To unsubscribe visit https://news.example.com/u/abc\n\n"
           + "\n".join("News item %d: something happened." % n
                       for n in range(1, 11)) + "\n",
     unchanged=True)

case("a mailman footer",
     plain="I think the patch is fine.\n\nBob\n"
           "_______________________________________________\n"
           "Users mailing list\nUsers@lists.example.org\n"
           "https://lists.example.org/mailman/listinfo/users\n",
     text="I think the patch is fine.\n\nBob", removed=("footer",))

# ── ">" that is not a quote ──

case("a fenced block keeps its > lines",
     plain="Run this in the shell:\n\n```\n> npm install\n> npm test\n```\n\n"
           "It should print OK.\n",
     unchanged=True, state="wanting")

case("an interpreter session keeps its >>> lines",
     plain="This is what I get:\n\n>>> import this\n>>> 1 + 1\n2\n\n"
           "Odd, isn't it?\n",
     unchanged=True, state="wanting")

case("indented code keeps its > lines",
     plain="Example:\n\n    > make all\n    > make install\n\nDone.\n",
     unchanged=True)

case("a classic diff keeps its > lines",
     plain="Here is the change:\n\n3c3\n< old line\n---\n> new line\n\n"
           "OK to commit?\n",
     unchanged=True, state="wanting")

case("a comparison at the start of a folded line",
     plain="The threshold is\n>= 5 items per order.\n\nBob\n",
     unchanged=True)

case("a folded sentence whose second line starts with >",
     plain="For the discount the order value must be\n"
           "> 100 francs before tax, which yours is.\n\nBob\n",
     unchanged=True, state="wanting")

case("terse inline answers are not folded sentences",
     plain="> Can you make Tuesday?\nYes\n> Will you bring the contract?\nNo\n",
     text="Yes\n\nNo")

case("a quote on a paragraph of its own goes, attribution or not",
     plain="> the budget is fixed for this year\n\nUnderstood, we plan within it.\n",
     text="Understood, we plan within it.")

case("a code-review notification: quoted context goes, the comment stays",
     plain="@bob commented on this pull request.\n\n"
           "> +    def total(self):\n> +        return sum(self.items)\n\n"
           "Should this be cached?\n\n-- \n"
           "Reply to this email directly or view it on the forge:\n"
           "https://git.example.com/acme/shop/pull/7#discussion_r42\n",
     text="@bob commented on this pull request.\n\nShould this be cached?")

case("a signature above the quote, top-posted",
     plain="Thanks, that helps.\n\n-- \nBob Example\nExample GmbH\n\n"
           + ATTRIBUTION + "\n> Here is the explanation you asked for.\n",
     text="Thanks, that helps.", removed=("quote", "signature"))

case("code in a fence, and a real quote in the same mail",
     plain="```\n> npm test\n```\n\nfails for me too.\n\n" + ATTRIBUTION
           + "\n> Does the test suite pass for you?\n",
     text="```\n> npm test\n```\n\nfails for me too.", state="wanting")

# ── Whole mails that need nothing ──

case("a plain message stays as it is",
     plain="Hi Ann,\n\nlunch on Friday?\n\nBob\n",
     unchanged=True, state="deterministic")

case("more text than WANTING_CHARS is wanting",
     plain="\n\n".join("Paragraph %d. " % n + "word " * 60 for n in range(12)),
     unchanged=True, state="wanting")

# ── HTML ──

_GMAIL_QUOTE_STYLE = ("margin:0px 0px 0px 0.8ex;border-left:1px solid "
                      "rgb(204,204,204);padding-left:1ex")
_GMAIL_ATTR = ('<div dir="ltr" class="gmail_attr">On Mon, 3 Mar 2025 at 14:02, '
               'Ann Example &lt;<a href="mailto:ann@example.org">'
               'ann@example.org</a>&gt; wrote:<br></div>')

_GMAIL_HTML = (
    '<div dir="ltr">Sounds good, Tuesday works.<div><br></div><div>Bob</div></div>'
    '<br><div class="gmail_quote gmail_quote_container">' + _GMAIL_ATTR
    + '<blockquote class="gmail_quote" style="%s"><div dir="ltr">Can we meet '
      'next week?<div><br></div><div>Ann</div></div>\n</blockquote></div>\n'
    % _GMAIL_QUOTE_STYLE)

case("gmail web, HTML only",
     html=_GMAIL_HTML,
     text="Sounds good, Tuesday works.\n\nBob", state="wanting",
     removed=("quote",))

case("gmail multipart: the plain part is the source",
     plain="Sounds good, Tuesday works.\n\nBob\n\n" + ATTRIBUTION
           + "\n\n> Can we meet next week?\n>\n> Ann\n",
     html=_GMAIL_HTML,
     text="Sounds good, Tuesday works.\n\nBob", state="deterministic")

case("a blank plain part hands over to the HTML part",
     plain=" \n\n", html=_GMAIL_HTML,
     text="Sounds good, Tuesday works.\n\nBob", state="wanting")

case("gmail HTML, inline replies inside the quote container",
     html='<div dir="ltr"><div class="gmail_quote">' + _GMAIL_ATTR
          + '<blockquote class="gmail_quote" style="%s"><div>Can you make '
            'Tuesday?</div></blockquote><div><br></div><div>Yes, after ten.'
            '</div><div>\u00a0</div><blockquote class="gmail_quote" style="%s">'
            '<div>Will you bring the contract?</div></blockquote><div><br>'
            '</div><div>I will.</div></div></div>'
          % (_GMAIL_QUOTE_STYLE, _GMAIL_QUOTE_STYLE),
     text="Yes, after ten.\n\nI will.")

case("gmail HTML forward",
     html='<div dir="ltr">FYI<br><br><div class="gmail_quote"><div dir="ltr" '
          'class="gmail_attr">---------- Forwarded message ---------<br>From: '
          '<strong class="gmail_sendername" dir="auto">Ann Example</strong> '
          '<span dir="auto">&lt;<a href="mailto:ann@example.org">'
          'ann@example.org</a>&gt;</span><br>Date: Mon, 3 Mar 2025 at 14:02<br>'
          'Subject: Quarterly numbers<br>To: Bob Example &lt;<a '
          'href="mailto:bob@example.net">bob@example.net</a>&gt;<br></div><br>'
          '<br><div dir="ltr">Hi Bob,<div><br></div><div>the numbers are '
          'attached.</div></div></div></div>',
     text="FYI\n\n---------- Forwarded message ---------\n"
          "From: %s\nDate: Mon, 3 Mar 2025 at 14:02\n"
          "Subject: Quarterly numbers\nTo: %s\n\nHi Bob,\n\n"
          "the numbers are attached." % (ANN, BOB),
     state="wanting")

case("apple mail HTML reply: the attribution is inside the blockquote",
     html='<html><head><meta http-equiv="content-type" content="text/html; '
          'charset=utf-8"></head><body style="overflow-wrap: break-word; '
          '-webkit-nbsp-mode: space; line-break: after-white-space;">Thanks '
          'Ann, that works.<br><div><br><blockquote type="cite"><div>On 3 Mar '
          '2025, at 14:02, Ann Example &lt;ann@example.org&gt; wrote:</div>'
          '<br class="Apple-interchange-newline"><div><meta '
          'http-equiv="content-type" content="text/html; charset=utf-8"><div '
          'style="overflow-wrap: break-word;">Here is the file.<div><br></div>'
          '<div>Ann</div></div></div></blockquote></div><br></body></html>',
     text="Thanks Ann, that works.")

case("apple mail HTML forward: the marker is inside the blockquote",
     html='<html><head><meta http-equiv="content-type" content="text/html; '
          'charset=utf-8"></head><body>Have a look.<br><div><br><blockquote '
          'type="cite"><div>Begin forwarded message:</div><br '
          'class="Apple-interchange-newline"><div style="margin: 0px;"><span '
          'style="font-family: -webkit-system-font, Helvetica Neue, Helvetica, '
          'sans-serif;"><b>From: </b></span><span>Ann Example &lt;<a '
          'href="mailto:ann@example.org">ann@example.org</a>&gt;<br></span>'
          '</div><div style="margin: 0px;"><span><b>Subject: </b></span><span>'
          '<b>Quarterly numbers</b><br></span></div><div style="margin: 0px;">'
          '<span><b>Date: </b></span><span>3 March 2025 at 14:02:11 CET<br>'
          '</span></div><div style="margin: 0px;"><span><b>To: </b></span>'
          '<span>Bob Example &lt;<a href="mailto:bob@example.net">'
          'bob@example.net</a>&gt;<br></span></div><br><div>Hi Bob,<div><br>'
          '</div><div>the numbers are attached.</div></div></blockquote></div>'
          '<br></body></html>',
     text="Have a look.\n\nBegin forwarded message:\n\n"
          "From: %s\nSubject: Quarterly numbers\n"
          "Date: 3 March 2025 at 14:02:11 CET\nTo: %s\n\nHi Bob,\n\n"
          "the numbers are attached." % (ANN, BOB))

_OUTLOOK_DESKTOP_HTML = (
    '<html xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-'
    'microsoft-com:office:office"><head><meta http-equiv=Content-Type '
    'content="text/html; charset=utf-8"><meta name=Generator content="Microsoft '
    'Word 15 (filtered medium)"><style><!--\n/* Font Definitions */\n@font-face'
    '\n\t{font-family:Calibri;}\np.MsoNormal {margin:0in;font-size:11.0pt;}\n'
    '--></style><!--[if gte mso 9]><xml>\n<o:shapedefaults v:ext="edit" '
    'spidmax="1026" />\n</xml><![endif]--></head><body lang=EN-US link="#0563C1"'
    ' vlink="#954F72"><div class=WordSection1><p class=MsoNormal>Hi Ann,<o:p>'
    '</o:p></p><p class=MsoNormal><o:p>&nbsp;</o:p></p><p class=MsoNormal>Works'
    ' for me.<o:p></o:p></p><p class=MsoNormal>Bob<o:p></o:p></p><p '
    'class=MsoNormal><o:p>&nbsp;</o:p></p><div><div style=\'border:none;'
    'border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in\'><p '
    'class=MsoNormal><b>From:</b> Ann Example &lt;ann@example.org&gt; <br><b>'
    'Sent:</b> Monday, March 3, 2025 2:02 PM<br><b>To:</b> Bob Example &lt;'
    'bob@example.net&gt;<br><b>Subject:</b> Meeting<o:p></o:p></p></div></div>'
    '<p class=MsoNormal><o:p>&nbsp;</o:p></p><p class=MsoNormal>Can we meet next'
    ' week?<o:p></o:p></p></div></body></html>')

case("outlook desktop HTML (Word markup)",
     html=_OUTLOOK_DESKTOP_HTML,
     text="Hi Ann,\n\nWorks for me.\nBob", removed=("quote",))

_WORD_BULLET = (
    "<p class=MsoListParagraph style='text-indent:-18.0pt;mso-list:l0 level1 "
    "lfo1'><![if !supportLists]><span style='font-family:Symbol'><span "
    "style='mso-list:Ignore'>&middot;<span style='font:7.0pt \"Times New "
    "Roman\"'>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; </span></span>"
    "</span><![endif]>%s<o:p></o:p></p>")

case("outlook desktop HTML: Word's list paragraphs, a notice under the name",
     html="<html><body lang=EN-US><div class=WordSection1><p class=MsoNormal>"
          "Hi all,<o:p></o:p></p><p class=MsoNormal><o:p>&nbsp;</o:p></p><p "
          "class=MsoNormal>the agenda:<o:p></o:p></p>"
          + _WORD_BULLET % "Budget" + _WORD_BULLET % "Hiring"
          + "<p class=MsoNormal><o:p>&nbsp;</o:p></p><p class=MsoNormal>Regards"
            "<o:p></o:p></p><p class=MsoNormal>Bob<o:p></o:p></p><p "
            "class=MsoNormal><span style='font-size:8.0pt;color:gray'>%s"
            "</span><o:p></o:p></p></div></body></html>" % _DISCLAIMERS["en"],
     text="Hi all,\n\nthe agenda:\n· Budget\n· Hiring\n\nRegards\nBob",
     removed=("footer",))


def _outlook_web(labels, intro="Works for me, see you Tuesday."):
    return (
        '<html><head><meta http-equiv="Content-Type" content="text/html; '
        'charset=utf-8"><style type="text/css" style="display:none;"> P '
        '{margin-top:0;margin-bottom:0;} </style></head><body dir="ltr"><div '
        'class="elementToProof" style="font-family: Aptos, Calibri, Helvetica, '
        'sans-serif; font-size: 12pt; color: rgb(0, 0, 0);">%s</div><div '
        'id="appendonsend"></div><hr style="display:inline-block;width:98%%" '
        'tabindex="-1"><div id="divRplyFwdMsg" dir="ltr"><font face="Calibri, '
        'sans-serif" style="font-size:11pt" color="#000000"><b>%s:</b> Ann '
        'Example &lt;ann@example.org&gt;<br><b>%s:</b> Monday, March 3, 2025 '
        '2:02 PM<br><b>%s:</b> Bob Example &lt;bob@example.net&gt;<br><b>%s:'
        '</b> Meeting</font><div>&nbsp;</div></div><div class="BodyFragment">'
        '<font size="2"><span style="font-size:11pt;"><div class="PlainText">'
        'Can we meet next week?</div></span></font></div></body></html>'
        % ((intro,) + labels))


case("outlook web HTML (#appendonsend, #divRplyFwdMsg)",
     html=_outlook_web(("From", "Sent", "To", "Subject")),
     text="Works for me, see you Tuesday.", removed=("quote",))

case("outlook web HTML in a language the tables do not know",
     html=_outlook_web(("Feladó", "Elküldve", "Címzett", "Tárgy")),
     text="Works for me, see you Tuesday.")

_HU_LABELS = ("Feladó", "Elküldve", "Címzett", "Tárgy")
_HU_PLAIN = ("Rendben.\n\n________________________________\n"
             "Feladó: %s\nElküldve: Monday, March 3, 2025 2:02 PM\n"
             "Címzett: %s\nTárgy: Meeting\n\nCan we meet next week?\n" % (ANN, BOB))

case("outlook's rule and header block in a language the tables do not know",
     plain=_HU_PLAIN, text="Rendben.", state="deterministic",
     removed=("quote",))

# Without the rule and without an address nothing in the TEXT says this is a
# header block rather than four lines the author wrote …
_HU_BARE = ("Rendben.\n\nFeladó: Ann Example\nElküldve: Monday, March 3, 2025 "
            "2:02 PM\nCímzett: Bob Example\nTárgy: Meeting\n\n"
            "Can we meet next week?\n")

case("an unknown header block with nothing to vouch for it is left alone",
     plain=_HU_BARE, unchanged=True)

# … but the markup of the same mail's HTML part does.
case("multipart: the plain part shows no quote, the HTML part's markup does",
     plain=_HU_BARE, html=_outlook_web(_HU_LABELS, intro="Rendben."),
     text="Rendben.", state="deterministic", removed=("quote",))

case("a rule above a postscript with a link is not a list footer",
     plain="See you there.\n\n________________________________\n"
           "PS: the slides are at https://files.example.com/slides\n",
     unchanged=True)

case("a rule above a meeting block is not outlook history",
     plain="Let us meet.\n\n________________________________\n"
           "Date: 03.03.2025 14:00\nPlace: Room 5\nTopic: Budget\n"
           "Contact: ann@example.org\n",
     unchanged=True)

case("multipart forward: the HTML part's container changes nothing",
     plain="FYI, see below.\n\n---------- Forwarded message ---------\n" + _FORWARDED,
     html='<div dir="ltr">FYI, see below.<br><br><div class="gmail_quote"><div '
          'dir="ltr" class="gmail_attr">---------- Forwarded message ---------'
          '<br>From: Ann Example &lt;ann@example.org&gt;<br>Date: Mon, 3 Mar '
          '2025 at 14:02<br>Subject: Quarterly numbers<br>To: Bob Example &lt;'
          'bob@example.net&gt;<br></div><br><br>Hi Bob,<br><br>the numbers are '
          'attached.<br><br>Ann</div></div>',
     unchanged=True, state="deterministic")

case("outlook HTML whose ids Exchange has prefixed",
     html=_outlook_web(("From", "Sent", "To", "Subject"))
     .replace('id="divRplyFwdMsg"', 'id="x_divRplyFwdMsg"')
     .replace('id="appendonsend"', 'id="x_appendonsend"'),
     text="Works for me, see you Tuesday.")

case("gmail app HTML",
     html='<div dir="auto">On my way.</div><br><div class="gmail_quote"><div '
          'dir="ltr" class="gmail_attr">Ann Example &lt;<a '
          'href="mailto:ann@example.org">ann@example.org</a>&gt; schrieb am '
          'Mo., 3. März 2025, 14:02:<br></div><blockquote class="gmail_quote" '
          'style="margin:0 0 0 .8ex;border-left:1px #ccc solid;padding-left:'
          '1ex"><div dir="ltr">Wo bist du?</div></blockquote></div>',
     text="On my way.")

case("outlook web HTML forward, told apart by its subject",
     html=_outlook_web(("From", "Sent", "To", "Subject"), intro="FYI"),
     subject="Fw: Meeting",
     text="FYI\n\n---------- Forwarded message ----------\n"
          "From: %s\nSent: Monday, March 3, 2025 2:02 PM\nTo: %s\n"
          "Subject: Meeting\n\nCan we meet next week?" % (ANN, BOB))

case("outlook mobile HTML",
     html='<html><head><meta http-equiv="Content-Type" content="text/html; '
          'charset=utf-8"></head><body><div dir="ltr"><div></div><div><div>Ok, '
          'will do.</div><div dir="ltr"><br></div><div '
          'id="ms-outlook-mobile-signature"><div style="direction:ltr">Get <a '
          'href="https://aka.ms/o0ukef">Outlook for iOS</a></div></div></div>'
          '</div><hr style="display:inline-block;width:98%" tabindex="-1"><div '
          'id="divRplyFwdMsg" dir="ltr"><font face="Calibri, sans-serif" '
          'style="font-size:11pt" color="#000000"><b>From:</b> Ann Example &lt;'
          'ann@example.org&gt;<br><b>Sent:</b> Monday, March 3, 2025 2:02:11 PM'
          '<br><b>To:</b> Bob Example &lt;bob@example.net&gt;<br><b>Subject:'
          '</b> Re: Meeting</font><div>&nbsp;</div></div><div dir="ltr">Please '
          'confirm.</div></body></html>',
     text="Ok, will do.", removed=("quote", "signature"))

case("thunderbird HTML: a name-only attribution, known by its class",
     html='<html><head><meta http-equiv="Content-Type" content="text/html; '
          'charset=UTF-8"></head><body><p>Sure, they are attached.</p><p>Bob'
          '<br></p><div class="moz-cite-prefix">Ann Example wrote:<br></div>'
          '<blockquote type="cite" cite="mid:abc123@example.org"><meta '
          'http-equiv="content-type" content="text/html; charset=UTF-8"><p>'
          'Could you send me the quarterly numbers?</p></blockquote><br><div '
          'class="moz-signature">-- <br>Bob Example<br>Example GmbH</div>'
          '</body></html>',
     text="Sure, they are attached.\n\nBob",
     removed=("quote", "signature"))

case("yahoo HTML (.yahoo_quoted)",
     html='<html><head></head><body><div class="ydp6d0a4e1ayahoo-style-wrap" '
          'style="font-family:Helvetica Neue, Helvetica, Arial, sans-serif;'
          'font-size:13px;"><div dir="ltr" data-setdir="false">Great, thanks!'
          '</div><div><br></div></div><div id="yahoo_quoted_1234567890" '
          'class="yahoo_quoted"><div style="font-family:\'Helvetica Neue\', '
          'Helvetica, Arial, sans-serif;font-size:13px;color:#26282a;"><div>On '
          'Monday, March 3, 2025 at 02:02:11 PM GMT+1, Ann Example &lt;'
          'ann@example.org&gt; wrote:</div><div><br></div><div><br></div><div>'
          '<div dir="ltr">Here is the report you asked for.</div><div '
          'dir="ltr">Ann</div></div></div></div></body></html>',
     text="Great, thanks!")

case("proton HTML (.protonmail_quote)",
     html='<div style="font-family: Arial, sans-serif; font-size: 14px;">'
          'Understood.</div><div style="font-family: Arial, sans-serif; '
          'font-size: 14px;"><br></div><div class="protonmail_signature_block" '
          'style="font-family: Arial, sans-serif; font-size: 14px;"><div '
          'class="protonmail_signature_block-proton">Sent with <a '
          'target="_blank" href="https://proton.me/mail/home">Proton Mail</a> '
          'secure email.</div></div><div style="font-family: Arial, '
          'sans-serif; font-size: 14px;"><br></div><div '
          'class="protonmail_quote">On Monday, March 3rd, 2025 at 2:02 PM, Ann '
          'Example &lt;ann@example.org&gt; wrote:<br><blockquote '
          'class="protonmail_quote" type="cite"><div style="font-family: '
          'Arial, sans-serif; font-size: 14px;">Please keep this between us.'
          '</div></blockquote><br></div>',
     text="Understood.", removed=("quote", "signature"))

_MARKETING_HTML = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>ACME '
    'Weekly</title><style>body{margin:0} .preheader{display:none}</style>'
    '</head><body><div style="display:none;max-height:0;overflow:hidden;">'
    'Hidden teaser text&zwnj;&nbsp;&zwnj;&nbsp;</div><span class="preheader">'
    'Do not show me</span><table role="presentation" width="100%"><tr><td '
    'align="center"><table role="presentation" width="600"><tr><td><img '
    'src="https://cdn.example.com/logo.png" alt="ACME" width="120" height="40">'
    '</td></tr><tr><td><h1>Three new features</h1><p>This week we shipped dark '
    'mode, export and search.</p><p><a href="https://news.example.com/c/'
    'abc123?u=42">Read the full story</a></p></td></tr><tr><td '
    'style="font-size:12px;color:#888"><p>You are receiving this email because '
    'you signed up at example.com.<br><a href="https://news.example.com/u/'
    'abc123">Unsubscribe</a> | <a href="https://news.example.com/p/abc123">'
    'Manage preferences</a></p><p>ACME Inc., 1 Example Road, Springfield</p>'
    '</td></tr></table></td></tr></table><img src="https://track.example.com/o/'
    'abc123.gif" width="1" height="1" alt=""><script>window.track && '
    'window.track()</script></body></html>')

case("HTML-only marketing mail",
     html=_MARKETING_HTML,
     text="[ACME]\n\nThree new features\n\nThis week we shipped dark mode, "
          "export and search.\n\nRead the full story "
          "(https://news.example.com/c/abc123?u=42)",
     state="wanting", removed=("footer", "pixels"),
     drops=("Hidden teaser", "Do not show me", "Unsubscribe", "window.track"))

case("an indenting blockquote is not a quote",
     html='<p>Quote of the day:</p><blockquote style="margin-left:40px">'
          'Simplicity is prerequisite for reliability.</blockquote><p>Bob</p>',
     text="Quote of the day:\n\nSimplicity is prerequisite for reliability."
          "\n\nBob")

case("a transactional mail's tables read row by row",
     html='<html><body><p>Thank you for your order.</p><table border="1"><tr>'
          '<th>Item</th><th>Qty</th><th>Price</th></tr><tr><td>Widget</td><td>'
          '2</td><td>10.00</td></tr></table><table><tr><td>Order number:</td>'
          '<td>12345</td></tr><tr><td>Order date:</td><td>03.03.2025 14:02'
          '</td></tr></table><p>Track it <a href="https://shop.example.com/t/'
          '12345">here</a>.</p></body></html>',
     text="Thank you for your order.\n\nItem | Qty | Price\n"
          "Widget | 2 | 10.00\n\nOrder number: 12345\n"
          "Order date: 03.03.2025 14:02\n\n"
          "Track it here (https://shop.example.com/t/12345).",
     state="wanting")

case("a border-top div above an order summary is not outlook history",
     html='<p>Thank you for your order.</p><div style="border-top:1px solid '
          '#ccc"><p>Order: 12345<br>Date: 03.03.2025 14:02<br>Total: 30.00</p>'
          '</div><p>See you soon.</p>',
     text="Thank you for your order.\n\nOrder: 12345\nDate: 03.03.2025 14:02\n"
          "Total: 30.00\n\nSee you soon.")


# ── Running the corpus ───────────────────────────────────────────────────────

def _normal(text):
    """The mail as the trim shows it when it removes nothing."""
    lines = [line.rstrip() for line in
             text.replace("\r\n", "\n").split("\n")]
    out = []
    for line in lines:
        if line.strip() or (out and out[-1]):
            out.append(line if line.strip() else "")
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def _source_lines(plain):
    """Every line a plain mail's trim may contain: its own lines, and those
    lines with quote levels taken off (a forward is unquoted)."""
    allowed = {mt.FORWARD_MARKER, ""}       # "": where a quote stood
    for line in plain.replace("\r\n", "\n").split("\n"):
        line = line.rstrip()
        allowed.add(line)
        while line.lstrip().startswith(">"):
            line = line.lstrip()[1:]
            line = line[1:] if line.startswith(" ") else line
            allowed.add(line.rstrip())
    return allowed


def check_case(c):
    result = mt.trim(c["plain"], c["html"], subject=c["subject"])
    where = "case %r" % c["name"]
    assert isinstance(result, mt.TrimResult) and isinstance(result, tuple), where
    assert set(result.removed) == {"quote", "signature", "footer", "pixels"}, (
        where, result.removed)
    if c["text"] is not None:
        assert result.text == c["text"], "%s\n--- got ---\n%s\n--- want ---\n%s" % (
            where, result.text, c["text"])
    if c["unchanged"]:
        want = _normal(c["plain"])
        assert result.text == want, "%s\n--- got ---\n%s\n--- want ---\n%s" % (
            where, result.text, want)
    for needle in c["keeps"]:
        assert needle in result.text, (where, "lost", needle, result.text)
    for needle in c["drops"]:
        assert needle not in result.text, (where, "kept", needle, result.text)
    if c["state"]:
        assert result.state == c["state"], (where, result.state, result.text)
    assert result.state in ("deterministic", "wanting"), where
    for key in c["removed"]:
        assert result.removed[key] > 0, (where, key, result.removed)
    if c["unchanged"]:
        assert not any(result.removed.values()), (where, result.removed)

    # Idempotence: the trimmed text trims to itself, without the subject and
    # without the HTML — which is how a later pass would see it.
    again = mt.trim(result.text, None)
    assert again.text == result.text, "%s is not idempotent\n--- first ---\n%s\n--- second ---\n%s" % (
        where, result.text, again.text)

    # Nothing invented: a plain mail's result is made of that mail's lines.
    if c["plain"] and c["plain"].strip():
        allowed = _source_lines(c["plain"])
        for line in result.text.split("\n"):
            assert line in allowed, (where, "invented line", line)

    # No mark the HTML conversion leaves for the trim ever reaches the text.
    assert "\x01" not in result.text and "\x02" not in result.text, where


def test_corpus():
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)), "duplicate case names"
    assert len(CASES) >= 40, len(CASES)
    for c in CASES:
        check_case(c)
    print("PASS corpus: %d mails trimmed, each idempotent" % len(CASES))


# ── trim(): the edges ────────────────────────────────────────────────────────

def test_empty_inputs():
    for plain, html in ((None, None), ("", ""), ("   \n", None), (None, " \n ")):
        result = mt.trim(plain, html)
        assert result.text == "", (plain, html, result)
        assert result.state == "deterministic", result
        assert result.removed == {"quote": 0, "signature": 0, "footer": 0,
                                  "pixels": 0}, result
    print("PASS empty inputs: empty text, deterministic")


def test_wanting_threshold():
    """More than WANTING_CHARS left is `wanting`; the constant is read at call
    time and the environment variable sets it at import."""
    text = "word " * 100                       # 499 characters once stripped
    assert mt.WANTING_CHARS == 2500, mt.WANTING_CHARS
    assert mt.trim(text, None).state == "deterministic"
    saved = mt.WANTING_CHARS
    try:
        mt.WANTING_CHARS = 499
        assert mt.trim(text, None).state == "deterministic"   # not MORE than
        mt.WANTING_CHARS = 498
        assert mt.trim(text, None).state == "wanting"
    finally:
        mt.WANTING_CHARS = saved

    for value, want in (("120", "120"), ("", "2500"), ("junk", "2500"),
                        ("-5", "2500")):
        env = dict(os.environ, MAIL_TRIM_WANTING_CHARS=value)
        out = subprocess.run(
            [sys.executable, "-c",
             "import mail_trim; print(mail_trim.WANTING_CHARS)"],
            cwd=str(SCRIPTS_DIR), env=env, capture_output=True, text=True,
            check=True).stdout.strip()
        assert out == want, (value, out)
    print("PASS WANTING_CHARS: threshold, call-time read, environment override")


def test_html_only_is_wanting():
    """An HTML-only mail is always wanting, however short and clean."""
    result = mt.trim(None, "<p>Lunch on Friday?</p>")
    assert result.text == "Lunch on Friday?", result
    assert result.state == "wanting", result
    # … but a mail with nothing to show wants no model either.
    assert mt.trim(None, '<img src="https://x.example/a.png">').state == "deterministic"
    print("PASS HTML-only: wanting")


def test_stub_plain_part():
    """A plain part that is a placeholder beside a real HTML part is not the
    mail: the HTML part is the source, and the mail counts as HTML-only."""
    article = "".join("<p>Paragraph %d of the newsletter, with a sentence or "
                      "two in it so that it has some length.</p>" % n
                      for n in range(1, 21))
    html = "<html><body><h1>ACME Weekly</h1>%s</body></html>" % article
    for stub in ("This message requires an HTML-capable mail client.",
                 "View this mail in your browser: https://news.example.com/v/1",
                 "ACME Weekly"):
        result = mt.trim(stub, html)
        assert result.text.startswith("ACME Weekly\n\nParagraph 1 of"), (stub, result.text[:80])
        assert "Paragraph 20 of the newsletter" in result.text
        assert result.state == "wanting", result.state
        assert mt.trim(result.text, None).text == result.text
    # A short mail is not a stub because its HTML twin carries markup …
    short_html = ("<html><head><style>%s</style></head><body><p>See you at "
                  "ten.</p></body></html>" % ("p{margin:0}" * 800))
    result = mt.trim("See you at ten.", short_html)
    assert result.text == "See you at ten." and result.state == "deterministic", result
    # … nor a longer plain part because the HTML says a little more.
    plain = "\n\n".join("Paragraph %d of the newsletter." % n for n in range(1, 21))
    result = mt.trim(plain, html)
    assert result.text == plain and result.state == "deterministic", result.text[:80]
    print("PASS a stub plain part hands over to the HTML part")


def test_surviving_marker_is_wanting():
    kept_quote = mt.trim("```\n> ls\n```\n", None)
    assert kept_quote.state == "wanting", kept_quote
    hanging = mt.trim("Thanks!\n\n" + ATTRIBUTION + "\n", None)
    # An attribution with no quote under it is cut when the verb vouches…
    assert hanging.text == "Thanks!", hanging
    # … and one the trim cannot be sure of stays, and says so.
    unsure = mt.trim("Thanks!\n\n2025-03-03 14:02 GMT+01:00 %s:\n" % ANN, None)
    assert "2025-03-03" in unsure.text and unsure.state == "wanting", unsure
    print("PASS a surviving quote marker: wanting")


def test_tracking_pixels_are_counted():
    html = ('<p>Hello</p><img src="https://t.example/1.gif" width="1" height="1">'
            '<img src="https://t.example/2.gif" style="width:1px;height:1px">'
            '<img src="https://t.example/3.gif" style="display:none">'
            '<img src="cid:photo@x" width="640" height="480" alt="The view">')
    result = mt.trim(None, html)
    assert result.removed["pixels"] == 3, result
    assert result.text == "Hello\n\n[The view]", result
    # The plain part being the source, the HTML is not parsed at all.
    assert mt.trim("Hello", html).removed["pixels"] == 0
    print("PASS tracking pixels: counted when the HTML is the source")


def test_marks_cannot_be_forged():
    """The control characters the HTML conversion uses to mark attributions
    and boundaries are stripped from input, so a mail cannot plant one."""
    plain = "Real text.\n\x02H\nLabel: a\nOther: b\n\x01Looks like an attribution:\n> q\n"
    result = mt.trim(plain, None)
    assert "Label: a" in result.text and "Looks like an attribution:" in result.text, result
    html = "<p>Real text.</p><p>\x02H</p><p>Label: a<br>Other: b</p>"
    result = mt.trim(None, html)
    assert "Label: a" in result.text, result
    assert "\x02" not in result.text and "\x01" not in result.text
    print("PASS marks: stripped from input, never in output")


def test_marks_in_nested_blocks_do_not_leak():
    """The marks are written at the top level only. A marked element inside a
    quote, a list item or a table cell (where the line gets a prefix) must
    leave no trace, and a mail the marks trimmed to nothing must come back as
    what a later trim of the same text would make of it."""
    for html in (
            '<blockquote type="cite"><div id="divRplyFwdMsg">From: a<br>To: b</div></blockquote>',
            '<ul><li>item<div id="divRplyFwdMsg">x</div></li></ul>',
            '<table><tr><td><div id="appendonsend"></div>cell</td></tr></table>',
            '<blockquote type="cite"><div style="border-top:solid 1pt">x</div></blockquote>',
            '<ul><li><div class="gmail_attr">Ann wrote:</div></li></ul>',
            '<div class="yahoo_quoted"><div class="moz-cite-prefix">Ann wrote:</div>q</div>'):
        for subject in (None, "Fwd: x"):
            result = mt.trim(None, html, subject=subject)
            assert "\x01" not in result.text and "\x02" not in result.text, (html, result.text)
            assert mt.trim(result.text, None).text == result.text, (html, result.text)
    # Trimmed to nothing with the help of a mark: the answer is what the text
    # alone supports, so asking again gives the same.
    html = ('<div class="moz-cite-prefix">Ann wrote:</div>'
            '<blockquote type="cite">quoted</blockquote>')
    result = mt.trim(None, html)
    assert result.text == "Ann wrote:", result
    assert mt.trim(result.text, None).text == result.text
    print("PASS marks: top level only, nothing leaks from nested blocks")


_FUZZ_PLAIN = [
    "Hi Ann,", "Thanks!", "Yes.", "No", "See below:", "as you said:", "Bob",
    "", "", "", "", "> quoted line", "> another quoted line", ">", ">> deeper",
    ">>> deepest", ">>> import this", ">= 5 items", "    > indented", "```",
    ATTRIBUTION, "Am 03.03.2025 um 14:02 schrieb Ann Example:",
    "On Mon, Mar 3, 2025 at 2:02 PM Ann Margaret Example <ann.example@example.org>",
    "wrote:", "<ann@example.org> wrote:", "Ann wrote:",
    "-----Original Message-----", "---------- Forwarded message ---------",
    "Begin forwarded message:", "_" * 32, "-" * 20, "--", "---", "3c3", "< old",
    "From: " + ANN, "Sent: Monday, March 3, 2025 2:02 PM", "To: " + BOB,
    "Subject: Meeting", "Date: 03.03.2025 14:02", "Von: Ann", "Betreff: x",
    "> From: " + ANN, "> Subject: x", "Label: value", "Feladó: " + ANN,
    "Sent from my iPhone", "Von meinem iPhone gesendet",
    "This e-mail is confidential and intended solely for the addressee. If "
    "you are not the intended recipient, please notify the sender.",
    "Unsubscribe: https://news.example.com/u/abc",
    "https://lists.example.org/mailman/listinfo/users",
    "a line that ends with a colon:", "the value must be", "> 100 francs",
    "\x01marked", "\x02H", "trailing blanks   ", "\tTabbed", "   > three spaces",
]
_FUZZ_HTML = [
    "<p>Hi</p>", "<div>text</div>", "<br>", "text ", "<b>bold</b>",
    '<blockquote type="cite">', "</blockquote>", '<div class="gmail_quote">',
    '<div class="gmail_attr">On Mon, 3 Mar 2025 at 14:02, Ann &lt;'
    'ann@example.org&gt; wrote:</div>',
    '<blockquote class="gmail_quote">', "</div>", '<div id="divRplyFwdMsg">',
    '<div id="appendonsend"></div>', "<b>From:</b> Ann &lt;ann@example.org&gt;<br>",
    "<b>Sent:</b> Monday, March 3, 2025 2:02 PM<br>", "<b>Subject:</b> x<br>",
    '<div style="border-top:solid 1pt">', "<hr>", "<table>", "<tr>", "<td>",
    "</td>", "</tr>", "</table>", "<ul>", "<li>", "</ul>",
    '<a href="https://x.example/a">link</a>',
    '<img src="https://t.example/p.gif" width=1 height=1>',
    '<span style="display:none">', "</span>", "<pre>", "</pre>",
    "&gt; quoted-looking", "-- <br>", "Sent from my iPhone",
    "<div class='yahoo_quoted'>", "<div class='moz-cite-prefix'>Ann wrote:</div>",
    "---------- Forwarded message ---------<br>", "Begin forwarded message:<br>",
    "<style>p{color:red}</style>", "<script>x</script>",
    "<p class=MsoNormal>&nbsp;</p>",
]


def test_random_mails_hold_the_invariants():
    """Mails thrown together from every piece the trim reacts to — plain,
    HTML and both, with and without a forward's subject. Whatever comes out:
    trimming it again changes nothing, a plain mail's result is made of that
    mail's lines and is never empty, no mark leaks, and the sanitiser's
    answer is a document. (Seeded: the same mails on every run.)"""
    import random
    rng = random.Random(20250303)
    rounds = 6000
    for n in range(rounds):
        mode = n % 4
        plain = html = None
        if mode != 0:
            plain = "\n".join(rng.choice(_FUZZ_PLAIN)
                              for _ in range(rng.randint(1, 24)))
        if mode in (0, 3):
            html = "".join(rng.choice(_FUZZ_HTML)
                           for _ in range(rng.randint(1, 24)))
        subject = rng.choice((None, None, "Re: x", "Fwd: x", "WG: x"))
        result = mt.trim(plain, html, subject=subject)
        context = (n, plain, html, subject, result.text)
        assert mt.trim(result.text, None).text == result.text, context
        assert "\x01" not in result.text and "\x02" not in result.text, context
        assert result.state in ("deterministic", "wanting"), context
        if mode in (1, 2):
            allowed = _source_lines(plain.replace("\x01", "").replace("\x02", ""))
            assert all(line in allowed for line in result.text.split("\n")), context
            assert result.text or not plain.strip("\n\t \x01\x02"), context
        if html:
            assert mt.sanitize_html(html).endswith("</body></html>"), context
            assert isinstance(mt.html_to_text(html), str)
    print("PASS %d random mails: idempotent, nothing invented, no mark leaks" % rounds)


def test_forward_subject_prefixes():
    yes = ("Fwd: x", "FW: x", "fw: x", "WG: x", "TR: x", "I: x", "RV: x",
           "Doorst: x", "ENC: x", "VS: x", "VB: x", "VL: x", "PD: x",
           "[list] Fwd: x", "  Fwd : x")
    no = ("Re: Fwd: x", "AW: WG: x", "Forward planning", "x", "", None,
          "Re: x", "Invoice: x")
    for subject in yes:
        assert mt.is_forward_subject(subject), subject
    for subject in no:
        assert not mt.is_forward_subject(subject), subject
    print("PASS forward subjects: the outermost prefix decides")


def test_tables_cover_the_languages():
    """Multilingual by design: every word table has a row for each language
    the module promises, and every header row names every role."""
    required = {"en", "de", "fr", "it", "es", "nl", "pt", "da", "nb", "sv"}
    tables = {name: getattr(mt, name) for name in (
        "_HEADER_LABELS", "_ATTRIBUTION_VERBS", "_ORIGINAL_STEMS",
        "_FORWARD_STEMS", "_FORWARD_PREFIXES", "_MOBILE_SIGNATURES",
        "_UNSUBSCRIBE_STEMS", "_CONFIDENTIAL_STEMS", "_RECIPIENT_STEMS")}
    for name, table in tables.items():
        missing = required - set(table)
        assert not missing, (name, missing)
        assert all(table[lang] for lang in table), name
    for lang, labels in mt._HEADER_LABELS.items():
        assert set(labels) == {"from", "sent", "to", "cc", "subject"}, lang
    # No word means two different headers across languages: the role map is
    # one flat lookup.
    seen = {}
    for lang, labels in mt._HEADER_LABELS.items():
        for role, words in labels.items():
            for word in words:
                key = mt._fold(word)
                assert seen.setdefault(key, role) == role, (lang, word)
    print("PASS word tables: %d languages, every role in each" % len(mt._HEADER_LABELS))


def test_never_raises_and_stays_fast():
    """Malformed, hostile and huge input: an answer, never an exception."""
    nasty = [
        "<", "<<<<", "<div", "<a href=", "</p></p></div>", "<!--", "<![CDATA[",
        "<p>" * 5000, "</div>" * 5000, "<blockquote type=cite>" * 3000 + "x",
        "<table><tr><td>" * 2000 + "x", "<ul><li>" * 2000 + "x",
        "<b><i>text</b></i>", "<table><td>a<td>b<tr><td>c</table>d",
        "&#xZZ; &bogus; &#99999999999;", "\x00\x01\x02", ">" * 10000,
        ">\n" * 5000, ":\n" * 5000, "--\n" * 3000, "From: a\n" * 3000,
        "----- x -----\n" * 2000, "```\n" * 2001,
    ]
    started = time.time()
    for item in nasty:
        for plain, html in ((item, None), (None, item)):
            result = mt.trim(plain, html)
            assert isinstance(result.text, str)
            assert mt.trim(result.text, None).text == result.text, repr(item[:40])
        assert isinstance(mt.html_to_text(item), str)
        assert isinstance(mt.sanitize_html(item), str)
    # A long thread: a hundred levels of history under one line.
    thread = "Top.\n\n" + "".join(
        "%sOn 3/3/25 14:%02d, Ann Example wrote:\n%s Level %d\n"
        % (">" * n, n % 60, ">" * (n + 1), n) for n in range(100)) * 20
    assert mt.trim(thread, None).text == "Top."
    elapsed = time.time() - started
    assert elapsed < 20, "too slow: %.1fs" % elapsed
    print("PASS robustness: %d hostile inputs, no exception (%.1fs)" % (
        len(nasty), elapsed))


def test_cost_grows_with_size_not_its_square():
    """A mail built to be slow must not be: four times the mail may take
    about four times as long, never sixteen. Each shape below is one that a
    look-back, a look-ahead or a repeated stage could turn quadratic."""
    disclaimer = _DISCLAIMERS["en"]
    shapes = {
        "signature delimiters": lambda n: "--\n" * n,
        "lines ending in a colon": lambda n: "a:\n" * n,
        "quote, blank, quote": lambda n: "> a\n\n" * n,
        "header labels": lambda n: "From: a\n" * n,
        "phone line and notice, repeated":
            lambda n: ("Sent from my iPhone\n\n%s\n\n" % disclaimer) * (n // 4),
        "fences": lambda n: "```\n> a\n" * n,
        "separators": lambda n: "----- x -----\n" * n,
        "attributions": lambda n: (ATTRIBUTION + "\n") * n,
        "one long token": lambda n: "a" * (40 * n) + ":",
        "addresses": lambda n: "a@" * (10 * n),
    }
    markup = {
        "open divs": lambda n: "<div>" * n + "x",
        "stray end tags": lambda n: "<b>" * n + "</i>" * n,
        "cells without a table": lambda n: "<td>a" * n,
        "items without a list": lambda n: "<li>a" * n,
        "nested hidden": lambda n: "<div hidden>" * n + "x" + "</div>" * n,
        "nested quotes": lambda n: '<blockquote type="cite">' * n + "x",
        "style comments": lambda n: "<style>" + "/*" * (4 * n) + "</style>x",
        "style braces": lambda n: "<style>" + "{" * (4 * n) + "</style>x",
        "inline style": lambda n: '<p style="' + "/*a:b;" * n + '">x</p>',
    }

    def cost(run, text):
        started = time.perf_counter()
        run(text)
        return time.perf_counter() - started

    def check(name, make, run):
        small = cost(run, make(5000))
        large = cost(run, make(20000))
        # Linear is 4x; quadratic 16x. The floor is for the timer's noise.
        assert large < max(1.5, 9 * small), (
            "%s: %.2fs for 5000, %.2fs for 20000" % (name, small, large))

    for name, make in shapes.items():
        check(name, make, lambda text: mt.trim(text, None))
    for name, make in markup.items():
        check(name + " (trim)", make, lambda text: mt.trim(None, text))
        check(name + " (sanitize)", make, mt.sanitize_html)
    print("PASS cost: linear on %d hostile shapes" % (len(shapes) + len(markup)))


# ── html_to_text() ───────────────────────────────────────────────────────────

def test_html_to_text_structure():
    h = mt.html_to_text
    assert h(None) == "" and h("") == "" and h("  ") == ""
    assert h("<p>One</p><p>Two</p>") == "One\n\nTwo"
    assert h("<div>One</div><div>Two</div>") == "One\nTwo"
    assert h("One<br>Two<br><br>Three") == "One\nTwo\n\nThree"
    assert h("<div>One<br></div><div>Two</div>") == "One\nTwo"
    assert h("<div>One</div><div><br></div><div>Two</div>") == "One\n\nTwo"
    assert h("<h1>Title</h1>Text") == "Title\n\nText"
    assert h("a   b\n\n c") == "a b c"
    assert h("<p>a <b>bold</b> and <i>it</i>.</p>") == "a bold and it."
    assert h("<p>a<p>b") == "a\n\nb"                      # implied </p>
    assert h("<pre>  keep\n    this  </pre>after") == "  keep\n    this\n\nafter"
    assert h("x<hr>y") == "x\n\n" + "-" * 20 + "\n\ny"
    assert h("&lt;tag&gt; &amp; &quot;q&quot; &euro;&nbsp;5 &#8364; &#x20AC;") == \
        '<tag> & "q" € 5 € €'
    # Hard blanks: padding is one space, an otherwise empty paragraph a blank
    # line, and neither leaves a hard blank in the text.
    assert h("a&nbsp;&nbsp;&nbsp;&nbsp;b&nbsp;") == "a b"
    assert h("<div>a</div><div>&nbsp;</div><div>b</div>") == "a\n\nb"
    assert h("<p class=MsoNormal>a</p><p class=MsoNormal>&nbsp;</p>"
             "<p class=MsoNormal>b</p><p class=MsoNormal>c</p>") == "a\n\nb\nc"
    assert h("<p style='margin:0'>a</p><p style='margin:0'>b</p>") == "a\nb"
    assert " " not in h("x&nbsp;<b>&nbsp;</b>&nbsp;y<p>&nbsp;</p>&nbsp;z")
    print("PASS html_to_text: blocks, breaks, whitespace, entities")


def test_html_to_text_lists():
    h = mt.html_to_text
    assert h("<ul><li>one</li><li>two</li></ul>") == "- one\n- two"
    assert h("<ol><li>one<li>two<li>three</ol>") == "1. one\n2. two\n3. three"
    assert h('<ol start="4"><li>four</li><li>five</li></ol>') == "4. four\n5. five"
    assert h("<ul><li>fruit<ul><li>apple</li><li>pear</li></ul></li>"
             "<li>veg</li></ul>") == "- fruit\n  - apple\n  - pear\n- veg"
    assert h("<ul><li><p>para one</p><p>para two</p></li></ul>") == \
        "- para one\n\n  para two"
    assert h("Before<ul><li>x</li></ul>After") == "Before\n\n- x\n\nAfter"
    print("PASS html_to_text: lists, ordered, nested")


def test_html_to_text_links():
    h = mt.html_to_text
    assert h('<a href="https://portal.example.com/doc/1">View the invoice</a>') == \
        "View the invoice (https://portal.example.com/doc/1)"
    # The text already is the target: nothing to add.
    assert h('<a href="https://example.com/">https://example.com</a>') == \
        "https://example.com"
    assert h('<a href="https://www.example.com">example.com</a>') == "example.com"
    assert h('<a href="http://example.com/a">Visit example.com/a now</a>') == \
        "Visit example.com/a now"
    # A different target than the text shows is exactly what must be shown.
    assert h('<a href="https://evil.example/x">https://bank.example</a>') == \
        "https://bank.example (https://evil.example/x)"
    assert h('<a href="mailto:ann@example.org">ann@example.org</a>') == \
        "ann@example.org"
    assert h('<a href="mailto:ann@example.org">Write to Ann</a>') == \
        "Write to Ann (ann@example.org)"
    assert h('<a href="mailto:ann@example.org?subject=Hi">ann@example.org</a>') == \
        "ann@example.org (mailto:ann@example.org?subject=Hi)"
    assert h('<a href="tel:+41440000000">+41 44 000 00 00</a>') == "+41 44 000 00 00"
    assert h('<a href="tel:+41440000000">Call us</a>') == "Call us (+41440000000)"
    # No destination worth writing.
    assert h('<a href="#top">Back to top</a>') == "Back to top"
    assert h('<a href="' + JS + 'void(0)">Click</a>') == "Click"
    assert h('<a name="x">Anchor</a>') == "Anchor"
    assert h('<a href="https://example.com/x"></a>after') == "after"
    assert h('See <a href="https://example.com/x">this</a>, please.') == \
        "See this (https://example.com/x), please."
    print("PASS html_to_text: a link's target only when it adds information")


def test_html_to_text_tables():
    h = mt.html_to_text
    assert h("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr>"
             "</table>") == "A | B\n1 | 2"
    assert h("<table><tr><td>Total:</td><td>30.00</td></tr></table>") == "Total: 30.00"
    # Unclosed cells and rows, as hand-written mail has them.
    assert h("<table><tr><td>a<td>b<tr><td>c<td>d</table>z") == "a | b\nc | d\n\nz"
    # A layout table: the cells are blocks, not columns of one line.
    assert h("<table><tr><td><p>Left one</p><p>Left two</p></td><td>Right</td>"
             "</tr></table>") == "Left one\n\nLeft two\n\nRight"
    # Nested layout tables do not multiply blank lines.
    assert h("<table><tr><td><table><tr><td>Inner one</td></tr><tr><td>Inner "
             "two</td></tr></table></td></tr></table>") == "Inner one\nInner two"
    # An empty cell (a spacer, an image column) is no column.
    assert h("<table><tr><td></td><td>Only</td><td> </td></tr></table>") == "Only"
    # A stray end tag inside a cell does not close the table around it.
    assert h("<div><table><tr><td>a</div>b</td><td>c</td></tr></table></div>") == \
        "ab | c"
    print("PASS html_to_text: tables row by row")


def test_html_to_text_drops_what_is_not_shown():
    h = mt.html_to_text
    assert h("<html><head><title>T</title><style>p{color:red}</style>"
             "<script>var a = '<p>x</p>';</script></head><body>Body</body>"
             "</html>") == "Body"
    assert h("a<script>alert(1)</script>b<style>.x{}</style>c") == "abc"
    assert h("a<!-- comment -->b<!--[if mso]><p>Outlook only</p><![endif]-->c") == "abc"
    # An unclosed head ends where the body begins, tagged or not.
    assert h("<head><meta charset=utf-8><title>T</title><body>Body") == "Body"
    assert h("<head><meta charset=utf-8><div>Body</div>") == "Body"
    for hidden in (
            '<div style="display:none">X</div>',
            '<div style="display: none !important">X</div>',
            '<span style="visibility:hidden">X</span>',
            '<div style="max-height:0;overflow:hidden">X</div>',
            '<div style="font-size:0">X</div>',
            '<span style="opacity:0;color:#fff">X</span>',
            '<div style="width:0;height:0;overflow:hidden">X</div>',
            '<span class="preheader">X</span>',
            '<div id="preview-text">X</div>',
            '<div hidden>X</div>',
            '<div style="display:none"><p>X</p><div>X<b>X</b></div></div>'):
        assert h("a" + hidden + "b") == "ab", hidden
    # Not hidden: sizes that merely contain a zero, Outlook-only hiding.
    for shown in ('<div style="font-size:10px">X</div>',
                  '<div style="max-height:100px">X</div>',
                  '<div style="opacity:0.8">X</div>',
                  '<div style="height:0">X</div>',
                  '<div style="mso-hide:all">X</div>'):
        assert "X" in h(shown), shown
    # Invisible padding characters.
    assert h("a\u200b\ufeff\u00adb") == "ab"
    assert h("<p>a</p><p>\u200c\u00a0\u200c\u00a0</p><p>b</p>") == "a\n\nb"
    print("PASS html_to_text: no script, style, head, comments, hidden elements")


def test_html_to_text_images_and_quotes():
    h = mt.html_to_text
    assert h('<img src="x.png" alt="A chart">') == "[A chart]"
    assert h('<img src="x.png">') == ""
    assert h('Thanks <img src="e.png" alt="\U0001F600">') == "Thanks \U0001F600"
    assert h('<img src="p.gif" width="1" height="1" alt="tracker">') == ""
    assert h('<blockquote type="cite">quoted<br>lines</blockquote>mine') == \
        "> quoted\n> lines\n\nmine"
    assert h('<blockquote type="cite">one<blockquote type="cite">two'
             '</blockquote></blockquote>') == "> one\n>\n> > two"
    assert h('<blockquote>indented</blockquote>') == "indented"
    # The public rendering carries none of the trim's marks.
    text = h(_outlook_web(("From", "Sent", "To", "Subject")))
    assert "\x01" not in text and "\x02" not in text
    assert "From: Ann Example <ann@example.org>" in text
    assert "Can we meet next week?" in text
    text = h(_GMAIL_HTML)
    assert "\x01" not in text and "> Can we meet next week?" in text, text
    print("PASS html_to_text: images by their alt text, cited quotes as >")


# ── is_tracking_pixel() ──────────────────────────────────────────────────────

def test_is_tracking_pixel():
    p = mt.is_tracking_pixel
    assert p(1, 1) and p(2, 2) and p(0, 0) and p(1, 2)
    assert p("1", "1") and p("1px", "1px") and p(1.0, 2.0)
    assert not p(3, 3) and not p(1, 600) and not p(600, 1) and not p(120, 40)
    assert not p() and not p(None, None) and not p(None, None, {})
    assert not p("100%", "auto") and not p(True, True)
    # One side known is enough when it is tiny.
    assert p(1, None) and p(None, 2) and not p(None, 3)
    # Declared attributes, as a dict or as the parser's pair list.
    assert p(attrs={"width": "1", "height": "1"})
    assert p(attrs=[("width", "1"), ("height", "1"), ("src", "x")])
    assert p(attrs=[("WIDTH", "0"), ("HEIGHT", "0")])
    assert p(attrs={"style": "width:1px; height:1px"})
    assert p(attrs={"style": "HEIGHT: 1px; WIDTH: 1px; border: 0"})
    assert p(attrs={"width": "1", "style": "height:1px !important"})
    assert not p(attrs={"width": "120", "height": "40"})
    assert not p(attrs={"style": "max-width:1px"})
    assert not p(attrs={"alt": "Logo"})
    assert not p(attrs={"width": "100%"})
    # An image its own markup hides is loaded for one reason only.
    assert p(attrs={"style": "display:none", "width": "600"})
    assert p(attrs={"style": "visibility: hidden"})
    assert p(attrs=[("hidden", None)])
    # The real size wins over what the markup declares.
    assert not p(640, 480, {"width": "1", "height": "1"})
    assert p(1, 1, {"width": "640", "height": "480"})
    print("PASS is_tracking_pixel: two pixels or fewer a side")


# ── sanitize_html() ──────────────────────────────────────────────────────────

def _body(document):
    """The part of a sanitised document that came from the mail."""
    start = document.index("<body")
    start = document.index(">", start) + 1
    assert document.endswith("</body></html>"), document[-40:]
    return document[start:-len("</body></html>")]


def _clean(html, cid_map=None):
    document = mt.sanitize_html(html, cid_map)
    low = document.lower()
    # The invariants of every answer, whatever went in.
    assert document.startswith("<!doctype html>"), document[:40]
    assert JS not in low, document
    assert "<script" not in low and "</script" not in low, document
    assert low.count("<style") == 1 and low.count("<meta") == 1, document
    for word in ("<iframe", "<object", "<embed", "<form", "<input", "<button",
                 "<textarea", "<select", "<base", "<link", "<svg", "<math",
                 "http-equiv", "srcset", "url(", "expression(", "@import"):
        assert word not in low, (word, document)
    import re
    assert not re.search(r"<[^>]*\son\w+\s*=", low), document
    for source in re.findall(r'\ssrc="([^"]*)"', document):
        assert source.startswith("data:image/"), source
    for target in re.findall(r'\shref="([^"]*)"', document):
        assert re.match(r"(?i)(https?://|mailto:)", target), target
    return _body(document)


def test_sanitize_document_shape():
    document = mt.sanitize_html("<p>Hello</p>")
    assert document.startswith('<!doctype html>\n<html><head><meta charset="utf-8">')
    assert document.endswith("<body><p>Hello</p></body></html>"), document
    assert _clean(None) == "" and _clean("") == ""
    # The mail's own <body> attributes survive where they are harmless.
    document = mt.sanitize_html(
        '<html><body bgcolor="#eeeeee" style="margin:0;background:url(x.png)" '
        'onload="track()" background="http://t.example/bg.png"><p>x</p></body>'
        '</html>')
    assert '<body bgcolor="#eeeeee" style="margin:0">' in document, document
    assert "onload" not in document and "t.example" not in document
    print("PASS sanitize: a whole document, body attributes filtered")


def test_sanitize_scripts_and_handlers():
    assert _clean("<p>a</p><script>alert(1)</script><p>b</p>") == "<p>a</p><p>b</p>"
    assert _clean("<script src='https://evil.example/x.js'></script>ok") == "ok"
    assert _clean("<SCRIPT>alert(1)</SCRIPT>ok") == "ok"
    # An unclosed script swallows the rest: fail closed.
    assert _clean("ok<script>alert(1)") == "ok"
    assert _clean('<p onclick="alert(1)" ONMOUSEOVER="x()" onfocus=y()>t</p>') == "<p>t</p>"
    assert _clean('<img src="x" onerror="alert(1)" alt="pic">') == '<img alt="pic">'
    assert _clean('<details ontoggle="alert(1)" open><summary>s</summary>d</details>') == \
        "<details><summary>s</summary>d</details>"
    assert _clean('<div style="x" onpointerdown="a()" formaction="b">t</div>') == "<div>t</div>"
    # A handler smuggled behind a slash, a tab or a duplicated attribute.
    assert "onerror" not in _clean('<img/src=x/onerror=alert(1)>')
    assert "onload" not in _clean('<p\tonload\t=\talert(1)>t</p>')
    assert _clean('<p title="a" title="b" onclick="c">t</p>') == '<p title="a">t</p>'
    # Text that merely looks like markup is escaped, not obeyed.
    assert _clean("1 &lt; 2 &amp;&amp; 3 &gt; 2") == "1 &lt; 2 &amp;&amp; 3 &gt; 2"
    assert _clean("&lt;script&gt;alert(1)&lt;/script&gt;") == \
        "&lt;script&gt;alert(1)&lt;/script&gt;"
    print("PASS sanitize: no script, no event handler")


def test_sanitize_script_scheme_links():
    """The scheme that runs code, spelled every way a filter has been fooled:
    none of them leaves a link, and the literal never leaves the function."""
    spellings = [
        JS + "alert(1)",
        JS.upper() + "alert(1)",
        "JaVa" + "ScRiPt" + ":alert(1)",
        "  " + JS + "alert(1)",
        "\x01\x02 " + JS + "alert(1)",
        "java\tscript" + ":alert(1)",
        "java\nscript" + ":alert(1)",
        "java\r\nscript" + ":alert(1)",
        "java&#9;script" + ":alert(1)",
        "java&#x0A;script" + ":alert(1)",
        "&#106;avascript" + ":alert(1)",
        "&#x6A;&#x61;&#x76;&#x61;script" + ":alert(1)",
        "javascript&colon;alert(1)",
        "javascript&#58;alert(1)",
        "javascript&#x3a;alert(1)",
        "java\x00script" + ":alert(1)",
        "vb" + "script" + ":msgbox(1)",
        "data:text/html,<script>alert(1)</script>",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "//evil.example/x", "/relative", "relative.html", "#fragment",
        "ftp://files.example/x", "file:///etc/passwd", "tel:+41440000000",
        "blob:https://x.example/1", "about:blank", "http:", "https://",
        "http:evil.example", "http:/evil.example", "\\\\evil.example\\x",
        "mailto:", " ", "",
    ]
    for spelling in spellings:
        for quote in ('"', "'"):
            if quote in spelling:
                continue
            body = _clean("<a href=%s%s%s>link</a>" % (quote, spelling, quote))
            assert body == "<a>link</a>", (spelling, body)
    # The words as TEXT are a reader's business; they stay readable, with the
    # colon written as an entity so the literal is nowhere in the document.
    body = _clean("<p>Type " + JS + "void(0) into the address bar.</p>")
    assert body == "<p>Type java" + "script&#58;void(0) into the address bar.</p>", body
    body = _clean('<p title="' + JS + 'x">t</p>')
    assert JS not in body
    # … and inside an allowed link's query string it is inert.
    body = _clean('<a href="https://example.com/?next=' + JS + 'alert(1)">x</a>')
    assert body.startswith('<a href="https://example.com/?next=java'), body
    print("PASS sanitize: %d ways to spell a script link, none survives" % len(spellings))


def test_sanitize_links():
    forced = ' target="_blank" rel="noopener noreferrer"'
    assert _clean('<a href="https://example.com/a?b=1&amp;c=2">x</a>') == \
        '<a href="https://example.com/a?b=1&amp;c=2"%s>x</a>' % forced
    assert _clean('<a href="HTTP://Example.com">x</a>') == \
        '<a href="HTTP://Example.com"%s>x</a>' % forced
    assert _clean('<a href="mailto:ann@example.org?subject=Hi">x</a>') == \
        '<a href="mailto:ann@example.org?subject=Hi"%s>x</a>' % forced
    # The mail's own target/rel never survive; ours are forced.
    assert _clean('<a href="https://example.com" target="_self" rel="opener" '
                  'ping="https://t.example/p" download>x</a>') == \
        '<a href="https://example.com"%s>x</a>' % forced
    assert _clean('<a href=" https://example.com/a b ">x</a>') == \
        '<a href="https://example.com/a%%20b"%s>x</a>' % forced
    # A quote in a URL cannot break out of the attribute.
    body = _clean("<a href='https://example.com/\"onmouseover=\"x'>x</a>")
    assert body == '<a href="https://example.com/&quot;onmouseover=&quot;x"%s>x</a>' % forced, body
    assert _clean('<a xlink:href="https://example.com">x</a>') == "<a>x</a>"
    print("PASS sanitize: http(s) and mailto links, new tab, no opener")


def test_sanitize_images():
    cid = {"logo@example.org": "data:image/png;base64,iVBORw0KGgo="}
    assert _clean('<img src="cid:logo@example.org" alt="Logo" width="120">', cid) == \
        '<img src="data:image/png;base64,iVBORw0KGgo=" alt="Logo" width="120">'
    # Content-ids are compared as mail writes them: brackets, case, escapes.
    assert 'src="data:image/png' in _clean('<img src="CID:Logo@Example.org">', cid)
    assert 'src="data:image/png' in _clean('<img src="cid:logo%40example.org">', cid)
    assert 'src="data:image/png' in _clean(
        '<img src="cid:logo@example.org">', {"<logo@example.org>": cid["logo@example.org"]})
    # Every other source is removed: nothing remote can load, no pixel fires.
    for source in ("https://cdn.example.com/a.png", "http://t.example/p.gif",
                   "//cdn.example.com/a.png", "/a.png", "a.png",
                   "data:image/png;base64,AAAA", "data:image/svg+xml,<svg/>",
                   "cid:unknown@example.org", JS + "alert(1)", "blob:x"):
        body = _clean('<img src="%s" alt="A">' % source, cid)
        assert body == '<img alt="A">', (source, body)
    assert _clean('<img src="cid:logo@example.org">') == "<img>"      # no map
    assert _clean('<img src="cid:logo@example.org">', {}) == "<img>"
    # The map is checked too: only an image data: URL is ever written.
    for bad in ("https://evil.example/x.png", "data:text/html,<script>",
                JS + "x", 'data:image/png;base64,AA" onerror="x', None, 7):
        assert _clean('<img src="cid:a">', {"a": bad}) == "<img>", bad
    # Tracking pixels go altogether, resolvable or not.
    assert _clean('<img src="https://t.example/p.gif" width="1" height="1">') == ""
    assert _clean('<img src="cid:logo@example.org" width="1" height="1">', cid) == ""
    assert _clean('<img src="x" style="display:none">') == ""
    # No other attribute can load an image either.
    body = _clean('<img srcset="https://t.example/a.png 1x" lowsrc="https://t.example/b" '
                  'dynsrc="https://t.example/c" longdesc="https://t.example/d" alt="A">')
    assert body == '<img alt="A">', body
    assert _clean('<table background="https://t.example/bg.png"><tr><td '
                  'background="https://t.example/b.png">x</td></tr></table>') == \
        "<table><tr><td>x</td></tr></table>"
    assert _clean('<picture><source srcset="https://t.example/a.webp"><img '
                  'src="https://t.example/a.png" alt="A"></picture>') == '<img alt="A">'
    assert _clean('<video src="https://t.example/v.mp4" poster="https://t.example/p.png">'
                  'fallback</video>ok') == "ok"
    print("PASS sanitize: only cid: images the mail carries, as data: URLs")


def test_sanitize_styles():
    assert _clean('<p style="color:red; font-size: 14px">x</p>') == \
        '<p style="color:red;font-size:14px">x</p>'
    assert _clean('<p style="color: rgb(204, 204, 204); margin:0 auto">x</p>') == \
        '<p style="color:rgb(204, 204, 204);margin:0 auto">x</p>'
    assert _clean('<p style="font-family:\'Helvetica Neue\', Arial">x</p>') == \
        '<p style="font-family:&#x27;Helvetica Neue&#x27;, Arial">x</p>'
    # Whatever can load or run is dropped, declaration by declaration.
    for bad in (
            "background:url(https://t.example/bg.png)",
            "background-image: URL( 'https://t.example/bg.png' )",
            "background:u\\72l(https://t.example/bg.png)",
            "background:u/**/rl(https://t.example/bg.png)",
            "background-image:image-set('https://t.example/a.png' 1x)",
            "width:expression(alert(1))",
            "width:expr/**/ession(alert(1))",
            "behavior:url(evil.htc)",
            "-moz-binding:url(https://t.example/x.xml#x)",
            "@import 'https://t.example/x.css'",
            "background:" + JS + "alert(1)",
            "list-style-image:url(" + JS + "alert(1))",
            "content:attr(title)",
            "position:fixed",
            "color:red}body{background:url(x)",
            "font-family:x</style><script>alert(1)</script>"):
        body = _clean('<p style="color:red;%s;margin:0">x</p>' % bad)
        assert body == '<p style="color:red;margin:0">x</p>', (bad, body)
    assert _clean('<p style="background:url(x)">x</p>') == "<p>x</p>"
    # !important is a flag, not an import; relative positioning is layout.
    assert _clean('<p style="margin:0 !important;position:relative">x</p>') == \
        '<p style="margin:0 !important;position:relative">x</p>'
    assert _clean('<p style="position:STICKY;top:0">x</p>') == '<p style="top:0">x</p>'
    # A linked sheet cannot load, so it is not there.
    assert _clean('<link rel="stylesheet" href="https://t.example/x.css"><p>x</p>') == "<p>x</p>"
    print("PASS sanitize: inline styles kept, nothing in them loads or runs")


def _sheet(html):
    """The mail's style rules as they stand in the sanitised document."""
    _clean(html)                    # the invariants
    document = mt.sanitize_html(html)
    start = document.index("<style>") + len("<style>")
    sheet = document[start:document.index("</style>")]
    assert sheet.startswith(mt._DOCUMENT_STYLE), sheet
    assert "<" not in sheet, sheet
    return sheet[len(mt._DOCUMENT_STYLE):]


def test_sanitize_stylesheets():
    """A mail's <style> is rebuilt from its harmless rules: classes and
    @media survive (a mail looks like itself, on a phone too), and nothing
    that loads, runs or could end the <style> element does."""
    assert _sheet("<style>p{color:red}</style><p>x</p>") == "p{color:red}"
    assert _clean("<style>p{color:red}</style><p>x</p>") == "<p>x</p>"
    assert _sheet("<style>\n.a , td.b > span { color : red ; margin:0 }\n</style>") == \
        ".a , td.b > span{color:red;margin:0}"
    assert _sheet("<html><head><style>h1{font-size:20px}</style></head><body>"
                  "<style>.x{color:blue}</style></body></html>") == \
        "h1{font-size:20px}.x{color:blue}"
    # What the rules select by stays on the elements.
    assert _clean('<p class="lead big" id="intro">x</p>') == \
        '<p class="lead big" id="intro">x</p>'
    # Media queries, one level: the phone layout.
    assert _sheet("<style>@media only screen and (max-width: 600px) { .col "
                  "{ width:100% !important; display:block } }</style>") == \
        "@media only screen and (max-width: 600px){.col{width:100% !important;display:block}}"
    # Outlook wraps its sheet in an HTML comment.
    assert _sheet("<style><!--\n/* Font Definitions */\n@font-face\n\t"
                  "{font-family:Calibri;}\np.MsoNormal {margin:0in;font-size:"
                  "11.0pt;}\n--></style>") == "p.MsoNormal{margin:0in;font-size:11.0pt}"
    # A class-hidden preheader stays hidden in the Original view.
    assert ".preheader{display:none}" in _sheet(_MARKETING_HTML)
    assert '<span class="preheader">' in _clean(_MARKETING_HTML)
    # Everything that loads or runs is gone, rule by rule and at-rule by at-rule.
    hostile = (
        "@import url(https://t.example/a.css);"
        "@import 'https://t.example/b.css';"
        "@charset \"utf-8\";"
        "@namespace svg url(http://www.w3.org/2000/svg);"
        "@font-face{font-family:x;src:url(https://t.example/f.woff)}"
        "a{color:red;background:url(https://t.example/p.gif)}"
        "b{background-image:image-set('https://t.example/a.png' 1x)}"
        "i{width:expression(alert(1))}"
        "u{behavior:url(x.htc)}"
        "s{-moz-binding:url(x.xml#x)}"
        "em{background:u\\72l(https://t.example/q.gif)}"
        "input[value^=a]{background:url(https://t.example/leak?a)}"
        "@keyframes k{from{left:0}to{left:10px}}"
        "@supports (display:grid){p{color:green}}"
        "@media screen{@media print{p{color:pink}} @import url(x); q{color:teal}}"
        "p{position:fixed;top:0}"
        "x<y{color:red}"
        "sel\\ector{color:red}"
        "nested{color:red;{color:blue}}"
        "ok{color:blue}"
        "unclosed{color:red")
    sheet = _sheet("<style>%s</style>" % hostile)
    assert sheet == ("a{color:red}@media screen{q{color:teal}}p{top:0}"
                     "ok{color:blue}unclosed{color:red}"), sheet
    # No way out of the <style> element: its text holds no "<" at all.
    for breakout in (
            'a::before{content:"</style><script>alert(1)</script>"}',
            "a{color:red}</style ><script>alert(1)</script><style>",
            "a{color:red}<!--</style><script>alert(1)</script>-->",
            "</STYLE><img src=x onerror=alert(1)>",
            "a{font-family:'</style><script>alert(1)</script>'}",
            "@media (max-width:1px){a{color:red}</style><script>alert(1)</script>}",
            "a{color:red}/*</style><script>alert(1)</script>*/"):
        _sheet("<style>%s</style><p>x</p>" % breakout)
    # A sheet too big to be a mail's is dropped whole; so is one inside
    # something that is dropped itself.
    assert _sheet("<style>%s</style>" % ("a{color:red}" * 20000)) == ""
    assert _sheet("<svg><style>a{color:red}</style></svg>") == ""
    print("PASS sanitize: style sheets rebuilt from harmless rules only")


def test_sanitize_forms_frames_and_meta():
    assert _clean('<form action="https://evil.example/login" method="post">Your '
                  'password: <input type="password" name="p"><input type="submit">'
                  '<button formaction="https://evil.example">Go</button><select>'
                  '<option>a</option></select><textarea>t</textarea></form>ok') == \
        "Your password: ok"
    assert _clean('<iframe src="https://evil.example"></iframe>ok') == "ok"
    assert _clean('<iframe srcdoc="<script>alert(1)</script>">inner</iframe>ok') == "ok"
    assert _clean('<object data="https://evil.example/x.swf"><param name="a" '
                  'value="b">fallback</object>ok') == "ok"
    assert _clean('<embed src="https://evil.example/x.swf">ok') == "ok"
    assert _clean('<frameset><frame src="https://evil.example"></frameset>ok') == "ok"
    assert _clean('<base href="https://evil.example/"><a href="x">l</a>') == "<a>l</a>"
    assert _clean('<meta http-equiv="refresh" content="0;url=https://evil.example">ok') == "ok"
    assert _clean('<html><head><meta http-equiv="refresh" content="0;url=https://'
                  'evil.example"><base href="https://evil.example/"><title>T'
                  '</title></head><body>ok</body></html>') == "ok"
    assert _clean('<svg onload="alert(1)"><script>alert(1)</script><a '
                  'xlink:href="' + JS + 'x"><text>t</text></a></svg>ok') == "ok"
    # An unclosed <svg> holds what follows it, in a browser as here …
    assert _clean('<svg/onload=alert(1)>inside') == ""
    # … and one that closes itself holds nothing.
    assert _clean('<svg onload="alert(1)"/>ok') == "ok"
    assert _clean("<math><mtext><table><mglyph><style><img src=x "
                  "onerror=alert(1)></style></mglyph></table></mtext></math>ok") == "ok"
    # Conditional comments never reach the output, whatever they hold.
    assert _clean('a<!--[if mso]><v:image src="https://t.example/x.png"/>'
                  '<![endif]-->b<!-- <script>alert(1)</script> -->c') == "abc"
    print("PASS sanitize: no forms, frames, objects, base, refresh, SVG, MathML")


def test_sanitize_malformed_markup():
    """Nested and broken markup: the output is well-formed and holds nothing
    the allow-lists do not name."""
    assert _clean("<b><i>text</b></i>") == "<b><i>text</i></b>"
    assert _clean("<div><p>hello<table><tr><td>x") == \
        "<div><p>hello<table><tr><td>x</td></tr></table></p></div>"
    assert _clean("</div></p>text</span>") == "text"
    assert _clean("<unknown-tag foo=bar>kept</unknown-tag>") == "kept"
    assert _clean("<o:p>&nbsp;</o:p>") == "\u00a0"
    # A stray end tag inside a cell does not close the table around it.
    assert _clean("<div><table><tr><td>a</div>b</td></tr></table></div>") == \
        "<div><table><tr><td>ab</td></tr></table></div>"
    # Tags split, nested and stuffed to slip past a filter.
    for hostile in (
            "<scr<script>ipt>alert(1)</scr</script>ipt>",
            "<<script>alert(1)//<</script>",
            "<script<b>>alert(1)</script>",
            "<img src=x onerror=alert(1)//",
            '<img src="x" onerror="alert(1)"',
            "<p onclick=alert(1)",
            '<a href="' + JS + 'alert(1)"',
            '"><script>alert(1)</script>',
            "<title><img src=x onerror=alert(1)></title>",
            "<textarea></textarea><script>alert(1)</script>",
            "<noscript><p title=\"</noscript><img src=x onerror=alert(1)>\">",
            "<xmp><b title=\"</xmp><script>alert(1)</script>\">t</b>",
            "<plaintext><script>alert(1)</script>",
            "<style><style/><img src=x onerror=alert(1)>",
            "<!--><script>alert(1)</script>-->",
            "<![CDATA[<script>alert(1)</script>]]>",
            "<?xml version='1.0'?><script>alert(1)</script>",
            "<svg><style><img src=x onerror=alert(1)></style></svg>",
            "<table><tr><td><form><input onfocus=alert(1) autofocus>",
            "<body onload=alert(1)><body onpageshow=alert(2)>",
            "<html onmouseover=alert(1)><head onclick=alert(2)>",
            "<div " + "x=y " * 500 + "onclick=alert(1)>t</div>",
            "<a href=\"https://ok.example\" href=\"" + JS + "alert(1)\">x</a>",
            "<a href=\"" + JS + "alert(1)\" href=\"https://ok.example\">x</a>",
            "<p>" * 3000 + "deep" + "</p>" * 3000,
            "<div><span><b>" * 2000 + "unclosed"):
        _clean(hostile)             # the invariants are asserted in there
    # Every element opened is closed, in order.
    import re
    body = _clean("<div><ul><li>a<li>b<table><tr><td><b>c")
    opened = []
    for closing, tag in re.findall(r"<(/?)([a-z0-9]+)[^>]*>", body):
        if closing:
            assert opened and opened.pop() == tag, body
        elif tag not in ("br", "hr", "img", "wbr", "col"):
            opened.append(tag)
    assert not opened, body
    print("PASS sanitize: malformed and hostile markup, invariants hold")


def test_sanitize_keeps_a_real_mail_readable():
    """The other half of the job: a mail still looks like itself."""
    body = _clean(_MARKETING_HTML)
    assert "<h1>Three new features</h1>" in body, body
    assert 'href="https://news.example.com/c/abc123?u=42"' in body
    assert 'width="600"' in body and '<img alt="ACME" width="120" height="40">' in body
    assert "track.example.com" not in body and "cdn.example.com" not in body
    assert "window.track" not in body and "ACME Weekly" not in body   # <title>
    body = _clean(_OUTLOOK_DESKTOP_HTML)
    assert "Works for me." in body and "<b>From:</b>" in body
    assert '<p class="MsoNormal">' in body
    document = mt.sanitize_html(_OUTLOOK_DESKTOP_HTML)
    assert "p.MsoNormal{margin:0in;font-size:11.0pt}" in document
    assert "font-face" not in document and "shapedefaults" not in document
    body = _clean(_GMAIL_HTML)
    assert '<blockquote class="gmail_quote" style=' in body
    assert "Can we meet next week?" in body
    assert 'href="mailto:ann@example.org"' in body
    print("PASS sanitize: a real mail keeps its structure and its links")


def test_module_is_standalone():
    """Importable in a slim gateway image: stdlib only, no sibling modules."""
    code = ("import sys; before = set(sys.modules); import mail_trim; "
            "new = set(sys.modules) - before; "
            "print(sorted(m for m in new if m.split('.')[0] not in "
            "sys.stdlib_module_names and m != 'mail_trim'))")
    out = subprocess.run([sys.executable, "-S", "-c", code], cwd=str(SCRIPTS_DIR),
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]", out
    source = (SCRIPTS_DIR / "mail_trim.py").read_text(encoding="utf-8")
    assert JS not in source.lower(), "the module must not hold the literal scheme"
    print("PASS standalone: stdlib-only import")


def main():
    test_corpus()
    test_empty_inputs()
    test_wanting_threshold()
    test_html_only_is_wanting()
    test_stub_plain_part()
    test_surviving_marker_is_wanting()
    test_tracking_pixels_are_counted()
    test_marks_cannot_be_forged()
    test_marks_in_nested_blocks_do_not_leak()
    test_random_mails_hold_the_invariants()
    test_forward_subject_prefixes()
    test_tables_cover_the_languages()
    test_never_raises_and_stays_fast()
    test_cost_grows_with_size_not_its_square()
    test_html_to_text_structure()
    test_html_to_text_lists()
    test_html_to_text_links()
    test_html_to_text_tables()
    test_html_to_text_drops_what_is_not_shown()
    test_html_to_text_images_and_quotes()
    test_is_tracking_pixel()
    test_sanitize_document_shape()
    test_sanitize_scripts_and_handlers()
    test_sanitize_script_scheme_links()
    test_sanitize_links()
    test_sanitize_images()
    test_sanitize_styles()
    test_sanitize_stylesheets()
    test_sanitize_forms_frames_and_meta()
    test_sanitize_malformed_markup()
    test_sanitize_keeps_a_real_mail_readable()
    test_module_is_standalone()
    print("all mail-trim tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
