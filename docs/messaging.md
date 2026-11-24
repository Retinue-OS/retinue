# Messaging — Signal, WhatsApp, Telegram, SMS, e-mail

*Reference depth for the "Messaging" digest in `CLAUDE.md`. Read this before
sending on an unfamiliar channel or account, changing send policies, enrolling
gateways, or debugging a channel that seems dead. Contact lookup itself is the
`messaging-contact-lookup` skill.*

## Signal push

The Signal gateway is bidirectional. Beyond answering inbound messages, it
exposes an outbound `/send` endpoint so agents can initiate Signal messages —
error escalations from subagents, alerts, and proactive daily briefings.
Use the client script:

```bash
# Text + spoken audio to the default recipient (the owner)
python3 /workspace/scripts/signal-push.py "Agent: failed to send reply to Mara — check scheduler.log"

# A briefing with a chart, German voice, to a specific number
python3 /workspace/scripts/signal-push.py --lang de --attach /tmp/glucose.png \
  "Guten Morgen! Hier ist dein Tagesbriefing …"

# Text only, no voice note
python3 /workspace/scripts/signal-push.py --no-voice "Quick note"
```

Each push delivers the text body **plus a spoken rendering** of it (same
Piper/ffmpeg pipeline as replies) and any `--attach` attachments. The gateway
owns the Signal account, so this is the correct "from" identity for all Signal
traffic — inbound and outbound, both the system's own messages and the user's
personal chats.

**Attachments are files of any type** on Signal, WhatsApp and Telegram alike:
`--attach PATH` (repeatable; `--image` is the flag's older name and still
works) sends an image, a video, a PDF or any other document, and the recipient
sees it under the file's own name. Each channel presents it natively — an image
or a video inline, anything else as a document; on WhatsApp a video that ffmpeg
cannot read still goes out, as a document. The first attachment carries the
message text as its caption where the channel has captions. One send carries
about 18 MB of attachments in total (the gateways' `*_GATEWAY_MAX_BODY_BYTES`,
25 MiB of base64 JSON by default); a larger one is refused with a 413 rather
than truncated. On the wire the list is the `/send` payload's `images` key — a
historical name, kept so a CLI and a gateway from different builds still
understand each other; each entry is `{data, filename?, content_type?}`.

To **resolve a name to a Signal number before sending** (the contact-lookup
step), read the gateway's roster — it works in scheduled/headless sessions and
is the only Signal contact path. The gateway exposes three token-gated read
endpoints:
`GET /recent-chats` (senders it has seen, most-recent-first — its stand-in for
"recent conversations", since signal-cli keeps no queryable history),
`GET /contacts` (the full contact directory), and `GET /groups`.

Use the client `scripts/signal-contacts.py` (like `signal-push.py`, `--url`
picks the account's gateway — `http://signal-gateway-personal:8090` for the
user's personal account). Per the messaging-contact-lookup skill, a name query
consults **recent chats first and only falls back to the contact directory on a
miss** — the default behaviour; each result carries a `source` field showing
which layer answered:

```bash
# Resolve a name: recent chats first, directory as fallback (the default)
python3 /workspace/scripts/signal-contacts.py \
  --url http://signal-gateway-personal:8090 --query doe

# Force the full directory / dump a whole roster / list groups
python3 /workspace/scripts/signal-contacts.py --query doe --contacts
python3 /workspace/scripts/signal-contacts.py --all          # recent chats
python3 /workspace/scripts/signal-contacts.py --groups
```

**When an autonomous agent (a chamber subagent, a scheduled job) hits an error that prevents it from
completing its task, it should call this script to alert the user** rather than
only writing to a log. Routine success needs no push; problems do.

## Send policies

Outbound sends can be gated by `SIGNAL_SEND_POLICY`, which — like
`EMAIL_SEND_POLICY` — keys the category off the **sending identity** (this
gateway's own `SIGNAL_ACCOUNT` number), not the recipient: `allow` sends
directly, `verify` queues the message as a pending send that must be approved on
the web gateway's `/sends` page, and `trust` sends directly only when you pass
`--user-approved` (assert the user has already approved this specific send). An
undeclared account defaults to `verify`. When a send is queued, `signal-push.py`
prints a pending-approval notice with the approval URL instead of confirming
delivery — the message goes out only once the user allows it at `/sends`.

That printed URL (`approve or deny at …`, of the form `/sends/<gateway>/<id>`,
absolute where a base URL is configured) addresses **that one pending send**,
not the page as a whole, and it is the only place it appears: nothing else
surfaces it later. So an agent reporting a queued send relays it as a labeled
link in the same message (the `dashboard-composing` skill, rule 5). A report
that names `/sends` without the link makes the user find the page and the entry
themselves — and a queued send nobody approves is a message never sent.

Every channel, e-mail included, ends an **Allow** the same way: the page shows
the outcome (a spinner while a messenger gateway delivers, then a green check,
or the gateway's error, which stays on screen), and a moment after success
moves on — to the next pending send, or, with none left, back to the page the
approval link was tapped on (a dashboard link opens in place, so that page is
in the window's history). A page opened on its own, from another app, has
nothing to go back to: it closes itself, or lands on the dashboard where the
browser will not close it — never on the empty `/sends` list. **Deny** goes
straight on to the next pending send, or, with none left, back the same way.

A send queued from a dashboard thread (see below) also carries **Back to
<thread title>** at the head of its approval and status pages. When that
thread is where the tab came from — the latest history entry that is not a
`/sends` page — the link is the browser's own Back; otherwise it opens the
thread. Either way the thread opens at the message the reader was at when they
tapped the approval link: the thread notes that place as the link is tapped
(per tab, in `sessionStorage`), and the approval page hands it on in the
shelf's restore format, so it holds even when the browser reloads the page
rather than bringing it back from its cache. The automatic return after a
decision hands it on the same way. The note is for that visit only: a way
back uses it up, and one left behind lapses after half an hour.

### Retracting a queued send

Until the user decides, the agent that queued a send may take it back — the
message is superseded, the facts changed, or it went to the wrong person. Every
gateway with a pending store supports it, with the id the push script printed
when it queued the send (`send queued for approval (id=…)`):

```bash
python3 /workspace/scripts/signal-push.py   --retract <id>
python3 /workspace/scripts/whatsapp-push.py --retract <id>
python3 /workspace/scripts/telegram-push.py --retract <id>
python3 /workspace/scripts/sms-push.py      --retract <id>
python3 /workspace/scripts/mail-push.py     --retract <id>   # the mail gateway's queue
python3 /workspace/scripts/caldav-push.py   --retract <id>   # a queued event
python3 /workspace/scripts/email_client.py  retract --request-id <id>
```

Pass the same `--url` the send used when it went to a non-default gateway. On
a messenger or calendar gateway this is `POST /pending-sends/<id>/retract`: the
entry moves to the terminal status `retracted` (distinct from the user's
`rejected`), leaves the `/sends` list, and its approval page says the agent
took it back; e-mail's `email_client.py` deletes the pending draft (the mail gateway's own
queue, `mail-push.py`, works like a messenger's). A retraction the user has
already overtaken is not silent: approved (or being sent) exits non-zero with
"too late", denied exits zero with "nothing was sent". After retracting, say
so wherever the approval link was relayed — the link now leads to a
retracted entry, not a message waiting for the user.

### The decision comes back to the thread

A send queued during a dashboard-thread turn is linked to that thread: the
web-gateway hands every thread turn its id as `RETINUE_THREAD_ID`, every push
CLI (and `email_client.py`, through its backend proxy) stamps it on the request
as `thread`, and the gateway keeps it on the pending entry (for e-mail, the
draft's `X-Send-Request-Thread` header, stripped before sending). When the user
allows or denies the send on `/sends`, the web-gateway notes the decision in
that thread as a *Retinue* message:

- **Denied** — "🚫 You denied the Signal message to … — nothing was sent."
- **Allowed** — the note waits for the gateway's terminal status (approval
  executes asynchronously) and says it was sent, or quotes the real error. A
  failed send marks the thread unread and wakes it; the other notes are a
  record of the user's own decision and badge nothing. A send still running
  after `SEND_DECISION_OUTCOME_TIMEOUT` seconds (default 120) is reported as
  allowed with a link to its approval page.

The note's agent context names the request (`<gateway>/<id>` and its status),
so the next turn in the thread knows what became of the message it proposed.
A job outside a thread turn that opened a thread for its proposal links the
send explicitly with `--thread <id>` on the push CLI (`mail-push.py` takes it too;
`email_client.py` has no such flag: set `RETINUE_THREAD_ID=<id>` in its environment instead); a send
with no thread is simply not reported. Retractions are the agent's own act and are not noted.

## Multiple gateways per channel

The `/sends` page enrols the built-in
gateways when their `*_GATEWAY_BASE_URL` is set and their name is included in
`MESSENGER_BUILTIN_CHANNELS` (default: all five — `signal`, `whatsapp`,
`telegram`, `sms`, `email`; SMS additionally needs `SMS_GATEWAY_BASE_URL`, which the base
compose leaves unset — see "Gateway connection monitoring" below — and
`email` is enrolled by `MAIL_GATEWAY_BASE_URL`, its gateway being the mail
gateway, which reads the variable too — see "E-mail (the mail gateway)"), but a deployment often runs
*more than one* gateway on a channel — most commonly a second Signal identity,
the user's **personal** account (`signal-gateway-personal`) alongside the
system one. Those extra gateways are enrolled by the deployment via
**`MESSENGER_GATEWAYS`** (read by `web-gateway.py`), a JSON array of
`{base_url, token?, label?, channel?}` objects; each becomes its own `/sends/<slug>/<id>`
account on the approval page. `channel` names the chat channel the gateway
serves (`signal`, `email`, …) where its service name does not say it — every
registry entry carries one (`scripts/messenger_gateways.py`), and one left
unstated is read from the slug and the label as before; a mail account added
this way must state `"channel": "email"`. The slug is the gateway's Docker **service name**
and needs no configuration on either side: the web-gateway takes the hostname
of each registered `base_url` verbatim, and the gateway derives the same name
from the Host header of the `/send` request that queued the message — so
`signal-push.py --url http://signal-gateway-personal:8090/send` prints a
`/sends/signal-gateway-personal/<id>` URL that resolves by construction, for
any account a deployment adds (config flows deployment → framework; the
framework names no specific deployment). Legacy shortened slugs (`signal`,
`signal-personal`) from links queued before this scheme still resolve as
aliases.

## WhatsApp (the same model)

WhatsApp works exactly like Signal, through its own dedicated service
(`whatsapp-gateway`) that owns the linked-device WhatsApp Web session — the keys
live only in that container, never in agent context, and there is no
`mcp__*_whatsapp__*` tool. Send with the thin CLI:

```bash
# Text to the default recipient
python3 /workspace/scripts/whatsapp-push.py "Agent: reply to Mara failed — check scheduler.log"

# With attachments, to a specific number
python3 /workspace/scripts/whatsapp-push.py --recipient +15551234567 \
  --attach /tmp/chart.png --attach /tmp/report.pdf "Summary"
```

Resolve a name to a WhatsApp number with `scripts/whatsapp-contacts.py` (same
recent-chats-first, directory-fallback contract as `signal-contacts.py`; `--url`
picks the account's gateway). Outbound is gated by `WHATSAPP_SEND_POLICY` — the
same `allow`/`trust`/`verify` categories and the same `/sends` approval flow as
e-mail. As with `EMAIL_SEND_POLICY`, the category is keyed by the **sending
identity** (the gateway's own `WHATSAPP_ACCOUNT` number), *not* the recipient:
what governs an autonomous send is which number it goes out as. A dedicated agent
number can be `allow`, while the user's own number stays `verify`; an account
matching no entry (and no `*` wildcard) defaults to `verify` (fail-safe). Who may
message *in* to drive the system is the separate inbound control (the
accepted-requesters allowlist, control mode only). Pending WhatsApp sends appear
on `/sends` alongside e-mail and Signal ones. There is no voice/Piper rendering
on WhatsApp (text plus optional attachments only).

## Telegram (the same model)

Telegram works the same way, through its own `telegram-gateway` service — but
unlike a bot, it logs in as the user's **own Telegram account** (an MTProto user
client via Telethon), so it acts *as the user*: it messages the user's contacts
as them, reads the user's own incoming DMs (so `inbox` mode genuinely triages the
user's Telegram mail), and sees the real contact directory. The credentials
(`TELEGRAM_API_ID`/`TELEGRAM_API_HASH` and the login session) live only in that
container — there is no `mcp__*_telegram__*` tool in agent context. Send with the
thin CLI:

```bash
# Text to the default recipient
python3 /workspace/scripts/telegram-push.py "Agent: reply to Mara failed — check scheduler.log"

# With an attachment, to a specific chat (chat_id or @username)
python3 /workspace/scripts/telegram-push.py --recipient @mara --attach /tmp/clip.mp4 "Summary"
```

Resolve a name with `scripts/telegram-contacts.py` — same recent-chats-first,
contact-directory-fallback contract as `signal-contacts.py` (the user client has
both). `GET /resolve?q=<@username|phone>` answers a user's numeric id, the
only identity inbound messages carry; the address book files it next to a
username (docs/contacts.md, "Telegram ids"). A phone number resolves only for
someone among the account's contacts; a name that is no user (unknown, or a
group or channel) answers 404, a failing client 502. Outbound is gated by `TELEGRAM_SEND_POLICY`, keyed — like
`EMAIL_SEND_POLICY` — by the **sending identity** (this account,
`TELEGRAM_ACCOUNT`), not the recipient chat; since it is the user's own account,
the fail-safe default means every send needs approval unless a policy entry grants
it. Pending Telegram sends appear on `/sends` with the others. Text plus optional
attachments only (no voice rendering).

## SMS (the same model, opt-in)

SMS runs through the user's own Android phone: the
[SMS Gateway for Android](https://github.com/capcom6/android-sms-gateway) app,
paired with a **self-hosted**
[android-sms-gateway server](https://github.com/android-sms-gateway/server).
Four services under the `sms` compose profile: `sms-db` (the server's MariaDB),
`sms-server` (the private server), `sms-server-worker` (its background tasks —
hashing processed message content and cleaning up old records) and
`sms-gateway` (`scripts/sms-gateway.py`, the channel gateway — same `/send`,
`/pending-sends`, `/undelivered`, `/health` contract as the others, same
ledger, delivery gate and chat surface). Send with the thin CLI — text only:

```bash
python3 /workspace/scripts/sms-push.py --recipient +41791234567 "Running ten minutes late"
python3 /workspace/scripts/sms-push.py --reply-to <token> "Thanks, see you then"
```

Outbound is gated by `SMS_SEND_POLICY`, keyed by the phone's own number
(`SMS_ACCOUNT`), default `verify`. "Sent" means the server accepted the message
for the phone; the phone transmits it on its next contact with the server.
There is no contact directory: `/contacts` and `/recent-chats` both answer from
the numbers that wrote in or were written to, so name lookup for SMS goes
through the chambers' own contacts.

Two deliberate differences from the other channels:

- **Inbox mode only.** SMS sender ids are trivially forged, so an
  accepted-requesters allowlist keyed on one would authenticate nothing; an SMS
  account is never a `control` channel. Every inbound SMS is untrusted
  external data — on the triage fallback path it reaches the model only
  HTML-escaped inside `<external_message>`, framed as data, never as
  instructions, and nothing ever replies to a sender by itself.
- **Inbound is a signed webhook.** The phone POSTs each received SMS to
  `SMS_WEBHOOK_URL`, which the deployment routes to `sms-gateway`'s
  `POST /webhook` — outside the dashboard's edge auth, since the app can present
  neither a client certificate nor the basic-auth password. The app signs every
  webhook (HMAC-SHA256 over body + `X-Timestamp`) under its signing key; the
  gateway refuses anything unsigned, wrongly signed or older than three days
  (the app's retry window), and refuses everything while
  `SMS_WEBHOOK_SIGNING_KEY` is unset. Redeliveries are deduplicated by message
  id. The gateway registers the webhook with the server itself at startup.

For the chat mirror this means two things. The gateway hears only
`sms:received`, so an SMS the user sends from the phone's own SMS app is not
seen: such a chat stays *Unread* on the dashboard until it is answered from
there or marked read (docs/messenger-chats.md, "Unread · Read · Archived").
And a received SMS carries the phone's receipt time, which a delayed or
retried webhook delivers later — it is unread by the order it arrived in,
whatever that stamp says.

**Setup.**

1. In `.env`: `COMPOSE_PROFILES=sms`, `SMS_GATEWAY_BASE_URL=http://sms-gateway:8095`
   (this is what enrols the channel on `/sends`, `/gateways`, the chat surface
   and the monitor), `SMS_DB_PASSWORD`, `SMS_SERVER_PRIVATE_TOKEN`,
   `SMS_GATEWAY_TOKEN` and `SMS_ACCOUNT` (see `.env.example`).
2. Route `sms.<domain>` in `docker-compose.override.yml`: `PathPrefix(/api/mobile/)`
   to `sms-server:3000` and exactly `POST /webhook` to `sms-gateway:8095`, both
   over HTTPS and without the dashboard's auth middlewares (example in
   `docker-compose.override.example.yml`).
3. In the app: Settings → Cloud Server → API URL
   `https://sms.<domain>/api/mobile/v1`, Private Token =
   `SMS_SERVER_PRIVATE_TOKEN`, and — to keep the vendor out entirely (below) —
   the notification channel set to SSE only. Once registered, the app shows a username and
   password: put them in `SMS_SERVER_USERNAME` / `SMS_SERVER_PASSWORD`.
4. In the app: Settings → Webhooks → Signing Key → copy it into
   `SMS_WEBHOOK_SIGNING_KEY`; set `SMS_WEBHOOK_URL=https://sms.<domain>/webhook`.
   Restart `sms-gateway`; `/gateways` shows it connected once the phone checks in.

**Keeping the vendor out.** Messages never pass through a third party, but
*wake-ups* can: the server tells the phone about a queued send through the
vendor's push relay (`api.sms-gate.app`) whenever the phone registered with a
push token, and that relay is the server's built-in default — it cannot be
switched off from the server side. A phone registered with the app's
notification channel set to SSE only has no push token, and the server then
sends the event over the phone's own server-sent-events connection to
`sms-server` instead, so nothing leaves this host. Choose it before
registering (or re-register after switching). There is no QR re-pairing: a "not seen recently" on `/gateways` is fixed
on the phone (is the app running, allowed in the background, online?).

## E-mail (the mail gateway)

The `mail-gateway` service (`scripts/mail-gateway.py`, image
`mail-gateway/Dockerfile`, port 8096) mirrors **one mailbox** into the same
per-message ledger the messenger gateways write, so each mail thread is a chat
on the Chats page — trimmed to its own new text, with the full mail one tap
away (**Original**) — and writes back: it sends under the e-mail send policy
and the mailbox follows what the user does in the chat. The design and its rules (threading, the record, the trim)
are `docs/mail-chats.md`; this section is how to run it. It is the `email`
channel's gateway: a built-in of the registry, enrolled by
`MAIL_GATEWAY_BASE_URL`, which the base compose sets, and **not** behind a
profile — a deployment with no mailbox configured runs it idle.

**Opt-in: `MAIL_GATEWAY_TOKEN`.** The container runs everywhere, but it
mirrors nothing and serves nothing until `MAIL_GATEWAY_TOKEN` is set: one
random value in `.env` (`openssl rand -hex 32`), which compose passes to both
the gateway and the `retinue` service. Without it only `/health` answers, with
`configured: false` and an `error` that names the token, so `/gateways` shows
the channel as not set up and the monitor raises no outage. An upgrade that
changes nothing in `.env` therefore mirrors nothing, although the mailbox
credentials are already there; setting the token is the decision to mirror
the mailbox (90 days back, by default) into the chat ledger and the life
store.

**Upgrading a deployment that pinned `MESSENGER_BUILTIN_CHANNELS`.** The
default account's gateway reads that variable too and idles as not
configured when it is set and does not list `email` — so a deployment that
named its channels before e-mail was one gets no mail chats until it adds
`email`. Set it in `.env`: a value set only on the `retinue` service in the
override does not reach `mail-gateway` (or set it on both services). To run no
mail gateway container at all, see "Never run the container at all" below.

It reads the mailbox credentials `scripts/email_client.py` reads, in its own
container, and uses `email_client.py` as its IMAP and MIME library. The same
credentials stay on the `retinue` service as well: `email_client.py`, its
backend and e-mail triage go on running there (see `SECURITY.md`).

**Writing.** The gateway sends, and the mailbox follows the chat. Replies,
new mail and an agent's mail leave through its `POST /send` under the e-mail
send policy (below); Mark as read, Archive and Delete in the chat reach the
mailbox ("The mailbox follows the chat"). It still pushes nothing and starts no
model turn for an arriving mail — that is the gate phase — and e-mail triage,
the whitelist and the e-mail approvals on `/sends` keep running beside it (see
"Two queues on /sends").

### Sending

Three ways in, one policy:

- **The composer** on a mail chat, and ＋ → *E-mail* on the Chats page: the web
  gateway posts `/chats/<id>/send` or `/chats/compose` to the gateway with
  `author: "user"` and `user_approved: true`. A group thread offers *Reply all*
  or *Sender only*.
- **An agent**, with `scripts/mail-push.py` — the sibling of `sms-push.py`:

  ```bash
  # A reply in a mirrored thread (--chat takes the chat id or its key)
  python3 /workspace/scripts/mail-push.py --chat <chat id> --reply all "Thanks, Tuesday works."
  python3 /workspace/scripts/mail-push.py --chat <chat id> --reply sender --file /tmp/reply.txt
  # A new mail
  python3 /workspace/scripts/mail-push.py --to ann@example.org --cc bob@example.org \
    --subject "Tuesday" --attach /tmp/plan.pdf "Hello Ann, ..."
  # Take a queued one back
  python3 /workspace/scripts/mail-push.py --retract <id>
  ```

  The text is the positional argument or `--file`; `--user-approved`,
  `--thread <id>` and `--url` (another account's gateway) work as on the other
  push CLIs. It reads `MAIL_GATEWAY_SEND_URL` and `MAIL_GATEWAY_TOKEN`, which
  compose gives the `retinue` service and `scripts/session_env.py` passes on to
  sessions. A queued send prints its approval URL, to be relayed as a labeled
  link like any other. An agent addresses a reply by chat id: `--reply-to
  <token>` is there for a gateway prompt that carries a token, and nothing
  issues one for mail yet. **Which tool an agent uses** depends on how the mail
  reached it: a mail it holds as a chat (a mail chat's companion note names the
  chat id and the command) is answered with `mail-push.py`; a mail e-mail
  triage hands it (a UID or `Message-ID`, no chat id) is answered with
  `email_client.py reply --uid`, as the `triage` skill says — its status store
  records the `sent_uid` and the draft queue that `reply` returns — until
  triage retires in the gate phase. A chat id is never guessed from a UID.
- **`email_client.py send/reply/forward`**, as before — see "Two queues on
  /sends" for how it differs.

A reply is built from the gateway's own records of the chat, never from the
caller: `From` the account (with `EMAIL_FROM_NAME`), `Subject` `Re: <base
subject>`, `In-Reply-To` the latest live mail of the thread that is not the
user's own (else the latest mail), `References` its chain plus its id. *Reply
all* (the default) goes to that mail's `Reply-To` or sender with everyone else
on its `To`/`Cc` — the account's own addresses excepted — in `Cc`; *sender
only* goes to the first alone. When the latest mail is the user's own, a
reply-all goes to its recipients again. A `400` answers no recipient, an unknown chat or
an address that does not parse; a `409` a chat with no live mail to reply to.
The `Message-ID` is minted when the request is accepted, before anything is
queued, so a new mail's chat is known at once.

**The recipients the user sees are the recipients the gateway uses.** The
browser does not work out a reply's addressing: the gateway says what `/send`
would do, by the same function. `POST /reply-preview {chat, account}` (bearer
token like `/send`; read-only, it never sends) answers `{all, sender, choice,
subject}`, where `all` and `sender` are each `{to: [{address, name}], cc:
[{address, name}]}` — exactly the addressing `/send` would use for that
reply mode right now, `name` the display name on record or `null` — `choice`
says whether the two modes differ, and `subject` is the `Re: …`. It answers
`400` or `409` as `/send` does for a chat it cannot reply in, and the error
sentence is shown. The web gateway serves the same body at `GET
/chats/<id>/reply-preview` (behind the dashboard's auth like every chat read),
with the names of people the address book knows added as `contact`; it
answers `409 {error}` where no reply is possible (a chat that holds only a
queued mail). Every mail message the web gateway serves also carries
`reply_to` (the addresses of its `Reply-To`).

The composer draws its To/Cc line and its send button's label from that
answer. It asks on load, after every poll that changed the thread (a mail
added, changed or removed, a pending change, new participants) and again at
the press, and **refuses to send** if the answer is missing or older than the
thread on screen. Every recipient is shown by its address, not only a name, and
one that is not the `From` of the mail being answered says why (its
`Reply-To`). *Reply all* / *Sender only* is offered exactly when `choice` is
true, and the mode sent is the one on screen at the press.

**A mail that may have gone out is never reported as not sent.** `POST /send`
answers as soon as the SMTP server has taken the mail; the copy in the Sent
folder is filed afterwards by the mirror loop, so a slow IMAP server or a
busy mirror never holds the answer. Every `/send` may carry a `request_key`
(an opaque token of at most 64 characters, minted per press by the caller):
a repeat with a key the gateway has seen within 24 hours answers what the
first answered — the same `message_id` — and sends nothing. The web gateway's
hop to `/send` waits `MAIL_SEND_TIMEOUT` (120 s by default) and passes the
browser's `request_key`; when the hop times out, drops after the request was
written, or the gateway answers `502 {unconfirmed: true}`, the browser gets
`202 {unconfirmed: true, message_id (when known), request_key}` — never a 5xx
that reads as "not sent". The chat page keeps the bubble as *Sending — not
confirmed yet*, does not put the words back into the composer, and reconciles
the bubble by message id once the mail shows. One `request_key` serves one
press of Send (a reply or a compose) and is reused if that same press is
retried.

**A send that failed, or was partly refused, does not disappear.** `GET
/pending-sends` (and `?chat=<key>`) lists entries with status `pending`,
`sending` and `error`; an `error` entry carries `error`, a sentence, and
`outcome_unknown`, which says the SMTP hand-over may have happened. `POST
/pending-sends/<id>/dismiss` removes a failed one. A send is never re-sent from
an `error` entry. Addresses the SMTP server refused while taking the rest are
on the entry and in the `200` answer as `refused: [addresses]`, and the
record's recipients are those the server took. In the chat the web gateway
gives a pending entry `status` (`pending`, `sending` or `failed`), `error`,
`outcome_unknown` and `refused`, a sent message carries `refused`, and `POST
/chats/<id>/pending/<request id>/dismiss` (same-origin) clears a failed one.
A failed bubble says so with the reason, offers *Copy text back to the
composer* and *Dismiss*, and for `outcome_unknown` says the mail may have gone
out; refused recipients are named on the bubble.

**The policy** is `EMAIL_SEND_POLICY`, resolved for the account's own address
(`email_client.resolve_category`; the same JSON array `email_client.py` reads,
now also on the `mail-gateway` service): `verify` queues, `trust` sends when
the request carries `user_approved` and queues otherwise, `allow` sends.
`author: "user"` grants nothing by itself. So the user's own press in the
composer sends under `trust` and `allow`, **and queues under `verify`** — on
the messengers a press is the approval, on e-mail it is not — and the web
gateway never approves a queued mail on the user's behalf. The answer is `200
{status: "sent", …}` or `202 {status: "pending_approval", request_id,
approval_url, message_id, chat}`; the approval URL is built as on the other
gateways, from `SEND_APPROVAL_BASE_URL` and the `/sends/<slug>/<id>` segment
(`SEND_APPROVAL_SLUG` overrides the slug, normally unset).

A queued send **shows in its chat as a pending bubble** with its approval link,
whoever queued it (composer, companion, `mail-push.py`), until it is sent,
rejected or retracted; a new mail that exists only as a queued send is listed
on the Chats page. Allow, Deny and a retract update the chat on its next poll.

**What a send writes.** SMTP through `email_client.py`'s helpers, and a copy
appended to the Sent folder (by the mirror loop, after the send has answered)
unless `SMTP_SAVE_SENT=false` (Gmail files its own). The gateway writes the
outbound record (`author` as given, the trimmed text the message itself) at
once and tells the chats rail, so the bubble does not wait for the next poll;
the Sent-folder pickup then finds the same `Message-ID` and writes nothing. Until that copy has had
`MAIL_SENT_GRACE_SECONDS` (600) to appear, a record the gateway wrote for its
own send is not treated as gone from the mailbox; with `SMTP_SAVE_SENT=false`
and no copy ever appearing, it stays a live record — the mail was sent. The
gateway never writes policy: `email_client.py`'s whitelist-on-send is disabled
inside it, so a mail sent through `mail-push.py` or the composer does not add
its recipient to the e-mail whitelist (one sent with `email_client.py` still
does until the whitelist retires).

### Two queues on /sends

`/sends` lists both, and they are different things:

| | the gateway's | `email_client.py`'s |
|---|---|---|
| Queued by | the composer, `mail-push.py`, a companion | `email_client.py send/reply/forward` under `verify` (or `trust` without `--user-approved`) |
| Parked as | an entry in the gateway's pending store (`mail-data`, with `/pending-sends`) | a draft in the IMAP Drafts folder (`DRAFTS_FOLDER`) |
| Approval URL | `/sends/mail-gateway/<id>` (the gateway's service name) | `/sends/<account>/<id>` |
| Sent by | the gateway, once approved | the web gateway's e-mail backend |
| Shown in the chat | as a pending bubble (a failed one stays, with its reason, until dismissed) | not until it is sent and in the Sent folder |
| Taken back with | `mail-push.py --retract <id>` | `email_client.py retract --request-id <id>` |
| Linked to a dashboard thread by | `--thread <id>` (the request's `thread`) | `RETINUE_THREAD_ID` |

Both are governed by the same `EMAIL_SEND_POLICY`, and both end the same way:
the mail goes out and appears in its chat. After an `email_client.py` send — direct, or an approved
draft — the backend tells the gateway whose mail it was
(`POST /outbound-note`, best-effort and never failing the send, only where
`MAIL_GATEWAY_BASE_URL` is set and only for the account that gateway serves),
so the bubble says *Ara* rather than *mail client*. A retraction of the wrong
kind finds nothing: the id says which queue.

### The mailbox follows the chat

| In the chat | In the mailbox | Gateway endpoint |
|---|---|---|
| Mark as read, a reply | `\Seen` on every mail of the thread in INBOX or the archive folder | `POST /chats/read` |
| Archive | the thread's INBOX mails move to the archive folder (`MAIL_ARCHIVE_FOLDER`, created and subscribed if absent); un-archiving moves nothing back | `POST /chats/archive` |
| Delete | every mail of the thread, in every mirrored folder, moves to the account's Trash (below), **then** the mirror is erased; nothing is expunged | `POST /chats/delete`, `X-Chat-Erase-Token` |

All three take `{chat, account}`; the account must be the gateway's own (else
`404`), and an unknown chat answers zero. The web gateway calls them after its
own state change, best-effort: a failure is logged and retried on the next such
action, and a gateway that answers `404`/`501` (a channel with no mailbox) is
not an error. Read and archive carry the `message_ids` of the mails the action
covers — for a read mark the chat's inbound mails up to the mark, for an archive
the inbound mails present when it was archived — and the gateway acts on those
mails only, so a mail that arrived after the user's action is neither marked
nor moved. **Delete is the exception**: it is the whole thread, the gateway
must succeed before the dashboard erases its own state, and if any move to the
Trash fails, or a mail of the thread cannot be placed, nothing is erased and
the answer reports `errors > 0`.

The web gateway treats `POST /chats/<id>/read` and `POST /chats/<id>/flags` —
the two endpoints whose effect now reaches the real mailbox — as the user's own
act, exactly as it treats send, compose and delete: same-origin only, so a page
on another site cannot mark mail read or archive it.

**Folders.** The account's Trash is **never invented**: it is the folder the
server marks `\Trash`, else the one `MAIL_TRASH_FOLDER` names; where neither
exists Delete is refused (`errors > 0`, nothing erased) and `MAIL_TRASH_FOLDER`
is the setting to fix. The archive folder is created when it is missing, and
a folder the gateway creates is **subscribed**, so a mail client lists it. A
name set explicitly wins over the server's SPECIAL-USE marks: with
`MAIL_ARCHIVE_FOLDER` or `MAIL_TRASH_FOLDER` set, that name is the folder; left
unset, the server's own `\Archive` (else `\All`) or `\Trash` folder is used,
else the default name. `CHAT_ERASE_TOKEN` is the same
capability the messenger gateways demand — `503` while it is unset on the
gateway, `403` on a mismatch — and sessions never hold it, so only the user's
two-tap delete erases a chat. A mail that later turns up again in a mirrored
folder (restored from the Trash) starts the chat anew.

IMAP writes are `UID STORE` and `UID MOVE`; where the server has no `MOVE`
the fallback is `UID COPY`, `\Deleted` and `UID EXPUNGE` of exactly those UIDs
when `UIDPLUS` allows, else the `\Deleted` copy is left flagged and reported.
Never a whole-folder `EXPUNGE`. A client's `\Seen` is still ignored by the
mirror: the chat is the truth and the mailbox follows it.

What it mirrors: **INBOX** (inbound records) and the **Sent** folder (outbound
ones), polled every `MAIL_POLL_SECONDS`, and the **archive folder**, which it
only follows — a mail moved there from INBOX is still that mail. A mail that
is in none of them any more (deleted or moved away in a client) leaves a
tombstone in its chat; a chat whose last mail went is archived. (The
mirror's own moves are not that: an Archive from the chat moves mail between
mirrored folders and leaves no tombstone.) Every MIME
part is kept as a blob in the ledger's media store — plain text, HTML, the
header block, attachments and inline images — and served only through the
gateway's token-gated `GET /media/<id>`, which the web gateway proxies. A
remote image is never fetched; a tracking pixel is dropped. A mail of a `news`
sender or list (`triage_policy.py email-news-add`) starts no chat: the gateway
skips it, and e-mail triage files it to the news feed as before — the gateway
files news itself only from the gate phase on (`docs/mail-chats.md`).

**Configuration** — on the `mail-gateway` service, all named in
`docker-compose.yml` (it loads no `env_file`):

| Variable | Default | |
|---|---|---|
| `EMAIL_USER`, `EMAIL_PASS`, `IMAP_HOST`, `IMAP_PORT`, `SMTP_HOST`, `SMTP_PORT`, `SENT_FOLDER`, `EMAIL_FROM_NAME`, `SMTP_SAVE_SENT`, `DRAFTS_FOLDER` | as `email_client.py` | the default account — the same values `.env` already holds for it |
| `MAIL_ACCOUNT_NAME` | *(empty)* | serve a named account: read the `_<NAME>`-suffixed set instead (below) |
| `MESSENGER_BUILTIN_CHANNELS` | *(unset: all)* | as on `retinue`; set without `email`, the default account's gateway idles as not configured |
| `MAIL_OWN_ADDRESSES` | *(empty)* | every address of the user's that may appear on this mailbox's mail, comma-separated: its aliases **and** the addresses of the user's other mirrored accounts. Never a participant, never a chat's peer; one value listing all of them serves every mail gateway |
| `MAIL_ARCHIVE_FOLDER` | `Archive` | the archive folder the mirror follows, and where Archive moves a chat's INBOX mails. Unset: the server's `\Archive` (else `\All`) folder, else this name; set: always this name (created and subscribed if absent) |
| `MAIL_TRASH_FOLDER` | `Trash` | where Delete moves a chat's mails. Unset: the server's `\Trash` folder, else this name; set: always this name. Never created: with neither, Delete is refused |
| `MAIL_SENT_GRACE_SECONDS` | `600` | how long a record the gateway wrote for its own send waits for its Sent copy before it counts as gone from the mailbox |
| `EMAIL_SEND_POLICY` | *(unset: `verify`)* | the send policy, keyed by the account's address — as `email_client.py` reads it, and the same `.env` value |
| `SEND_APPROVAL_BASE_URL` | | public base of the approval links a queued send returns; unset, a relative path |
| `SEND_APPROVAL_SLUG` | *(unset)* | overrides the `/sends/<slug>/<id>` segment; normally unset, the slug is the service name, as on the other gateways |
| `CHAT_ERASE_TOKEN` | | the capability the user's delete presents (`X-Chat-Erase-Token`); unset, `/chats/delete` answers `503`. One value for `retinue` and every gateway |
| `MAIL_POLL_SECONDS` | `60` | poll interval |
| `MAIL_BACKFILL_DAYS` | `90` | how far back INBOX and Sent are mirrored on first run; `0` mirrors only new mail |
| `MAIL_BACKFILL_BATCH` | `50` | mails per backfill batch |
| `MAIL_BACKFILL_PAUSE_SECONDS` | `2` | pause between backfill batches |
| `MAIL_IMAP_TIMEOUT` | `60` | IMAP socket timeout, seconds |
| `MAIL_MAX_PART_BYTES` | 25 MiB | an attachment larger than this is not stored |
| `MAIL_MAX_MAIL_BYTES` | 50 MiB | a larger mail keeps its text parts and no attachments |
| `MAIL_TRIM_WANTING_CHARS` | `2500` | the trim's "still long" threshold (`scripts/mail_trim.py`) |
| `MAIL_POLICY_WAIT_SECONDS` | *(unset: no limit)* | a positive number: how long the mirror waits for the e-mail policy file before it starts as with no flags |
| `MAIL_RECENT_CHATS_MAX` | `200` | how many chats `/recent-chats` answers at most |
| `MAIL_RAIL_TIMEOUT` | `10` | timeout of the chats rail's POSTs, seconds |
| `MAIL_GATEWAY_TOKEN` | | **required** — the opt-in (above) and the bearer token of its HTTP API; compose passes the one `.env` value to `retinue` too |

**The retinue side** reads a few tunables of its own. The `retinue` service has
no `env_file`, so each is named under its `environment:` in `docker-compose.yml`
and a value in `.env` reaches it:

| Variable | Default | Read by | |
|---|---|---|---|
| `MAIL_SEND_TIMEOUT` | `120` | the web gateway | how long its hop to the mail gateway's `/send` waits (an SMTP hand-over with a large attachment is slow); past it the answer is *unconfirmed*, not failed |
| `MAIL_GATEWAY_TIMEOUT` | `120` | `mail-push.py` | how long the push CLI waits for `/send`; sessions inherit it (`_GATEWAY_TIMEOUT`) |
| `CHAT_PENDING_TIMEOUT` | `4` | the web gateway | how long one look at a gateway's pending queue may take; it rides on a page's poll |
| `CHAT_PENDING_RETRY_SECONDS` | `15` | the web gateway | how long a gateway that could not answer is left alone, the chat's copy standing in |
| `CHAT_PENDING_SYNC_SECONDS` | `20` | the web gateway | how often the chat list has every mail gateway's queue read; `0` never does |
| `MAILBOX_NONE_SECONDS` | `600` | the web gateway | how long a gateway that answered `404`/`501` to a mailbox call (no mailbox) is not asked again |
| `MAIL_ORIGINAL_MAX_BYTES` | 4 MiB | the web gateway | how much of each text part *Original* reads |
| `MAIL_ORIGINAL_INLINE_MAX_BYTES` | 8 MiB | the web gateway | how many bytes of inline images one *Original* view inlines |

`CHATS_INGEST_*` and `INBOUND_STORE_DIR` are wired by compose as on the other
gateways. `EMAIL_SEND_POLICY` and `CHAT_ERASE_TOKEN` are named since this
phase sends and erases, and nothing else is: no `NEWS_INGEST_*` (it files no
news until the gate phase adds the rail), no other service's credential — the
container that parses mail from strangers holds only what it uses. Volumes: `messenger-email` is
the ledger (the gateway's `/inbound`, the retinue side's
`chambers/_generated/messenger/email`, qlever-life read-only), `mail-data` its
own state. Losing the thread index, the index of where each mail lives and
the outbound notes costs a rescan; the **pending sends are data** — the mails
waiting for approval with their attachments, and the journal of sends in
flight — and recreating the volume drops them.

**Backfill.** On first run the gateway mirrors INBOX and Sent for
`MAIL_BACKFILL_DAYS`, merged and ingested oldest first so a reply finds its
parent. Backfilled mail pushes nothing, starts nothing and does not wake an
archived chat; a mail already read in a client (`\Seen`) starts read, so ninety
days of mail do not arrive as ninety days of unread chats. From then on a
client's `\Seen` is ignored. `/health.backfill` says how far it got
(`{done, pending, days}`).

**The e-mail policy file moved.** The group flags (`news` / `quieted` /
`ignored`) and, until the gate phase, the whitelist live in one `.nt`, which
the gateway must read at ingest — so it lives on the gateway's ledger volume
now: `chambers/_generated/messenger/email/policy/email-policy.nt` on the
retinue side, `/inbound/policy/email-policy.nt` in the gateway. It used to be
`chambers/_generated/triage/email-whitelist.nt`; the retinue entrypoint moves
it (content and all) first thing at start, before the chamber clones and
installs (`triage_policy.py place-email-policy`), the web gateway does again
when it starts, and any retinue-side write of the policy moves it first. A
deployment that pinned `TRIAGE_EMAIL_WHITELIST_PATH` keeps its path and is not
moved, and must give the gateway the same file under its own mount. The mirror
does not start until the file is there (`/health.error` says so meanwhile,
with `blocked: "policy"`; after ten minutes also `connected: false`, as a
matter of the deployment's setup — where to look is in the error — and not of
the mailbox): its first backfill decides once, at ingest, which mail is news,
so a backfill without the flags would make every newsletter of the window a
chat for good.
By default it waits as long as it takes; a positive
`MAIL_POLICY_WAIT_SECONDS` makes it give up after that long and go on as with
no flags. `python3 scripts/triage_policy.py place-email-policy` (or any other
command of it) places the file too, and says if it left an old file that
differs from the new one.

**`/gateways`** shows the mail gateway as a card like a messenger's: whether
it is connected and, if not, the reason its `/health` gives (whose `account`
is the mailbox address) — the IMAP server refused the login, or cannot be
reached. There is no
QR code to scan: a mail gateway is fixed in the mailbox settings (host, port,
user, an app password that was changed or revoked) or by waiting out the
provider. The connection monitor words its notices for this case
(`scripts/gateway-monitor.py`, `WORDING`). One reason is not the mailbox's at
all: the mirror waiting for the e-mail policy file (`blocked: "policy"` in
`/health`, above) — the card shows that reason, and the monitor's notice and
reminders say "waiting for its setup" with it, never "check the login". Nor
is a mirror that stopped on a fault of its own (`blocked: "mirror"` — it
cannot read or write mail: a corrupt index, a full disk, a bug): the notice
says "stopped on a fault of its own", with the gateway's reason, and the
gateway's log has the rest.
Unconfigured (`configured: false`) is intentional, never an outage.

**A named account** (`EMAIL_USER_ARI`, …) is one more gateway: a service that
`extends` `mail-gateway` with `MAIL_ACCOUNT_NAME=ARI`, that account's
suffixed variables listed, the default account's password blanked, and its
**own** `mail-data` volume — it shares the `messenger-email` ledger, whose
records carry their account. Enrol it in `MESSENGER_GATEWAYS` with
`"channel": "email"`, since its service name does not say which channel it
serves. One thread reaching two of the user's accounts is two chats, one per
account — and in each, the user's other account is the user, not a
correspondent, only if `MAIL_OWN_ADDRESSES` lists it: let that one value
(which `extends` hands every mail gateway) name every address of the user's.
The full example is in `docker-compose.override.example.yml`.

## What an agent session can see

The gateways are sidecars for credential isolation: the Signal keys, the
WhatsApp session, the Telegram login and the CalDAV password live in their own
containers (the mailbox password, for now, in two: the mail gateway's and the
`retinue` container's, see "E-mail (the mail gateway)"), and an agent reaches each through a thin script that authenticates
with a **capability token** (`SIGNAL_GATEWAY_TOKEN`, `CALDAV_GATEWAY_TOKEN`, …)
— a token buys one request behind the send policy, where a password would buy
the account. Those tokens are also the *only* secrets a session inherits: every
`claude -p` the framework spawns starts from the allowlist in
`scripts/session_env.py`, which passes the tokens, the framework's own settings
and the model credential (`ANTHROPIC_*`, `ANTHROPIC_CUSTOM_HEADERS` included
when it carries a LiteLLM virtual key) and withholds everything else — mailbox
passwords, the LiteLLM master and picker keys, the repo token. What passes,
the `RETINUE_SESSION_ENV_EXTRA` escape hatch and the remaining gaps:
`docs/contributing.md`, "Session environment — the allowlist".

## Gateway connection monitoring

Linked-device sessions (Signal, WhatsApp, Telegram) die silently — the phone
unlinks the device, a session gets revoked — and so does a mailbox login (the
mail gateway's IMAP server refuses a changed password, or is unreachable). The
framework watches for this itself: every gateway's `GET /health` reports its real link state, and
`scripts/gateway-monitor.py` (forked by the entrypoint) polls them once a
minute. On a sustained failure it opens a dashboard conversation (which
Web-Pushes the user like any incoming message) pointing at the dashboard's
**`/gateways`** page, where the user sees each gateway's status and — for a
disconnected one — the pairing QR code to scan (proxied from the gateway's
token-gated `GET /qr`). For the mail gateway the notice says what is wrong
instead — the server refused the login or cannot be reached — and asks for
no QR scan: there is no phone to re-pair (`gateway-monitor.py` words each
notice by the registry entry's channel). A gateway whose `/health` names
what holds it when that is not its link (`blocked` — the mail mirror waiting
for the e-mail policy file) is reported as waiting for its setup, and one
that names itself (`blocked: "mirror"`) as stopped on a fault of its own —
with the gateway's own reason in the notice and in every reminder. An outage
that turns into one of another kind (the gateway that waited for its setup
now cannot log in) is told in the same thread once as many checks in a row
have seen it as open an outage (`GATEWAY_MONITOR_FAILURES`), in the new
kind's words and with its reason, not at the next reminder. Recovery is
reported in the same thread. So: do **not**
build ad-hoc liveness checks for these channels; if a user reports a channel
seems dead, check `/gateways` (or the gateways' `/health`) first, and treat a
`SIGNAL_SEND_POLICY`-style unconfigured channel (`configured: false`) as
intentional, not broken.

A healthy Signal link with an **empty roster** (`signal-contacts.py --groups`
and `--contacts` both return nothing, group chats titled by their raw id) is
not a dead link but a phone that has never synced: a linked device only gets
contacts and groups when the primary sends them, on request. The gateway
sends that request (`signal-cli sendSyncRequest`) on start, after a (re)link,
and throttled on every unknown group id (`SIGNAL_ROSTER_SYNC_INTERVAL`,
default one hour). If the roster stays empty, the phone was offline when
asked — a gateway restart asks again.

**A channel that answers but behaves like an older build** is the other thing
to look for there, and it is not an outage: each `/health` carries a `build`
block, and `/gateways` flags any gateway whose shared-module digest differs
from the dashboard's — that one was not rebuilt, and the remedy is
`scripts/self-update.py`, not a QR scan. See *Which build is actually running*
in `docs/contributing.md`.

**A deployment that doesn't use a given channel at all** — never runs its
container, not even unpaired — must say so explicitly, or the monitor has no
way to tell that apart from a real outage. The base `docker-compose.yml`
always points the `retinue` service at the four always-on built-in gateways via
`SIGNAL_GATEWAY_BASE_URL` / `WHATSAPP_GATEWAY_BASE_URL` /
`TELEGRAM_GATEWAY_BASE_URL` / `MAIL_GATEWAY_BASE_URL`; if the matching container is never started, the
monitor's health check fails DNS resolution — indistinguishable, from inside
this container, from that same container having crashed — and it would open a
"gateway disconnected" thread on every restart. There are two supported ways
to avoid that, and they mean different things:

- **Leave the container running, just unpaired.** Its own `/health` then
  reports `configured: false`, which `/sends`, `/gateways` and the monitor all
  already skip — no config needed, and the channel stays visibly "not set up"
  on `/gateways` if the user ever wants to pair it later. Preferred when the
  resource cost of an idle container is a non-issue.
- **Never run the container at all.** Then set `MESSENGER_BUILTIN_CHANNELS` on
  the `retinue` service in the deployment's `docker-compose.override.yml` (see
  `scripts/messenger_gateways.py` and the example there) to the comma-separated
  subset of `signal`, `whatsapp`, `telegram`, `sms`, `email` this deployment actually
  runs — e.g. `MESSENGER_BUILTIN_CHANNELS=signal` for a Signal-only deployment,
  or set it explicitly **empty** for none — *unset* means all of them (SMS
  still enrolling only where `SMS_GATEWAY_BASE_URL` is set). Naming a channel there is what enrols it into the
  shared registry `/sends`, `/gateways` and the monitor all read from
  regardless of what `*_GATEWAY_BASE_URL` happens to be wired to; leaving a
  channel out drops it from all three at once, same as a chamber that was
  never mounted. One variable states the deployment's whole channel set, so it
  reads as a deliberate choice rather than a row of easy-to-forget blanks.
  The mail gateway reads the variable too (from `.env`; a value set only on
  the `retinue` service does not reach it) and idles where it leaves `email`
  out; to keep its container from starting at all, also give `mail-gateway`
  a profile nobody enables in the override (`profiles: ["off"]`). It then
  holds no mailbox password either.

`GATEWAY_MONITOR_IGNORE` (comma-separated slugs) is the narrower tool: it
silences the monitor alone while leaving the channel enrolled everywhere else
— for a gateway the deployment deliberately runs unlinked and doesn't want
reminders about, not for one it never runs at all.
