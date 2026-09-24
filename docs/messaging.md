# Outbound messaging — Signal, WhatsApp, Telegram, SMS

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
python3 /workspace/scripts/signal-push.py "Ari: failed to send reply to Mara — check scheduler.log"

# A briefing with a chart, German voice, to a specific number
python3 /workspace/scripts/signal-push.py --lang de --image /tmp/glucose.png \
  "Guten Morgen! Hier ist dein Tagesbriefing …"

# Text only, no voice note
python3 /workspace/scripts/signal-push.py --no-voice "Quick note"
```

Each push delivers the text body **plus a spoken rendering** of it (same
Piper/ffmpeg pipeline as replies) and any `--image` attachments. The gateway
owns the Signal account, so this is the correct "from" identity for all Signal
traffic — inbound and outbound, both the system's own messages and the user's
personal chats.

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

**When an autonomous agent (e.g. Ari, Coach) hits an error that prevents it from
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

## Multiple gateways per channel

The `/sends` page enrols the built-in
gateways when their `*_GATEWAY_BASE_URL` is set and their name is included in
`MESSENGER_BUILTIN_CHANNELS` (default: all four — `signal`, `whatsapp`,
`telegram`, `sms`; SMS additionally needs `SMS_GATEWAY_BASE_URL`, which the base
compose leaves unset — see "Gateway connection monitoring" below), but a deployment often runs
*more than one* gateway on a channel — most commonly a second Signal identity,
the user's **personal** account (`signal-gateway-personal`) alongside the
system one. Those extra gateways are enrolled by the deployment via
**`MESSENGER_GATEWAYS`** (read by `web-gateway.py`), a JSON array of
`{base_url, token?, label?}` objects; each becomes its own `/sends/<slug>/<id>`
account on the approval page. The slug is the gateway's Docker **service name**
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
python3 /workspace/scripts/whatsapp-push.py "Ari: reply to Mara failed — check scheduler.log"

# With an image, to a specific number
python3 /workspace/scripts/whatsapp-push.py --recipient +15551234567 --image /tmp/chart.png "Summary"
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
on WhatsApp (text plus optional image attachments only).

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
python3 /workspace/scripts/telegram-push.py "Ari: reply to Mara failed — check scheduler.log"

# With an image, to a specific chat (chat_id or @username)
python3 /workspace/scripts/telegram-push.py --recipient @mara --image /tmp/chart.png "Summary"
```

Resolve a name with `scripts/telegram-contacts.py` — same recent-chats-first,
contact-directory-fallback contract as `signal-contacts.py` (the user client has
both). Outbound is gated by `TELEGRAM_SEND_POLICY`, keyed — like
`EMAIL_SEND_POLICY` — by the **sending identity** (this account,
`TELEGRAM_ACCOUNT`), not the recipient chat; since it is the user's own account,
the fail-safe default means every send needs approval unless a policy entry grants
it. Pending Telegram sends appear on `/sends` with the others. Text plus optional
image attachments only.

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

## What an agent session can see

The gateways are sidecars for credential isolation: the Signal keys, the
WhatsApp session, the Telegram login and the CalDAV password live in their own
containers, and an agent reaches each through a thin script that authenticates
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
unlinks the device, a session gets revoked. The framework watches for this
itself: every gateway's `GET /health` reports its real link state, and
`scripts/gateway-monitor.py` (forked by the entrypoint) polls them once a
minute. On a sustained failure it opens a dashboard conversation (which
Web-Pushes the user like any incoming message) pointing at the dashboard's
**`/gateways`** page, where the user sees each gateway's status and — for a
disconnected one — the pairing QR code to scan (proxied from the gateway's
token-gated `GET /qr`). Recovery is reported in the same thread. So: do **not**
build ad-hoc liveness checks for these channels; if a user reports a channel
seems dead, check `/gateways` (or the gateways' `/health`) first, and treat a
`SIGNAL_SEND_POLICY`-style unconfigured channel (`configured: false`) as
intentional, not broken.

**A channel that answers but behaves like an older build** is the other thing
to look for there, and it is not an outage: each `/health` carries a `build`
block, and `/gateways` flags any gateway whose shared-module digest differs
from the dashboard's — that one was not rebuilt, and the remedy is
`scripts/self-update.py`, not a QR scan. See *Which build is actually running*
in `docs/contributing.md`.

**A deployment that doesn't use a given channel at all** — never runs its
container, not even unpaired — must say so explicitly, or the monitor has no
way to tell that apart from a real outage. The base `docker-compose.yml`
always points the `retinue` service at the three always-on built-in gateways via
`SIGNAL_GATEWAY_BASE_URL` / `WHATSAPP_GATEWAY_BASE_URL` /
`TELEGRAM_GATEWAY_BASE_URL`; if the matching container is never started, the
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
  subset of `signal`, `whatsapp`, `telegram`, `sms` this deployment actually
  runs — e.g. `MESSENGER_BUILTIN_CHANNELS=signal` for a Signal-only deployment,
  or set it explicitly **empty** for none — *unset* means all of them (SMS
  still enrolling only where `SMS_GATEWAY_BASE_URL` is set). Naming a channel there is what enrols it into the
  shared registry `/sends`, `/gateways` and the monitor all read from
  regardless of what `*_GATEWAY_BASE_URL` happens to be wired to; leaving a
  channel out drops it from all three at once, same as a chamber that was
  never mounted. One variable states the deployment's whole channel set, so it
  reads as a deliberate choice rather than a row of easy-to-forget blanks.

`GATEWAY_MONITOR_IGNORE` (comma-separated slugs) is the narrower tool: it
silences the monitor alone while leaving the channel enrolled everywhere else
— for a gateway the deployment deliberately runs unlinked and doesn't want
reminders about, not for one it never runs at all.
