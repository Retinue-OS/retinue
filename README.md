# retinue

Infrastructure repository for **Retinue** (formerly *health-agents*) — a team
of personal agents, coordinated by Ara. The core is a small generic harness
(the **frame**); domain capabilities arrive as **chambers** mounted into it. A
*chamber* is one mounted repository: a self-contained collection of data **and**
agents/skills, each of which can be a Claude Code plugin (scoped to its
`.retinue/` subdirectory).

Retinue is content-neutral: which chambers exist is declared by the
**deployment**, not baked into the framework. Two runnable example chambers ship
under [`examples/chambers/`](examples/chambers/) (Westworld/Dolores and
Hitchhiker/Marvin) as the canonical "how to author a chamber" reference.

Defines these core compose services:

- **`retinue`** — the main container: the Claude Code runtime, the core agent
  definitions (Archivist, Academic, Publisher, Secretary), and the
  chamber-mounting harness. (Domain content lives in chambers, not in this
  service.)
- **`signal-gateway`** — dedicated Signal bridge (Signal CLI + Piper)
  for one Signal account. Depending on the account's configured mode
  (see [Messaging accounts](#messaging-accounts)) it either runs incoming
  messages as prompts to `retinue` and replies in audio (**control** mode), or
  hands them to the user's triage as incoming mail (**inbox** mode). Incoming
  voice notes are transcribed via the `stt` service. It also
  exposes an outbound `/send` HTTP endpoint so `retinue` can *initiate*
  Signal messages (alerts, escalations, daily briefings) via
  `scripts/signal-push.py`, each with a spoken rendering and optional images.
- **`stt`** — a small speech-to-text microservice that owns the single Whisper
  model in the stack and exposes `POST /transcribe`. Shared by the
  `signal-gateway` (inbound voice notes) and the web gateway (dashboard voice
  input), so exactly one ASR model is loaded system-wide.
- **`qlever-life`** — a live SPARQL endpoint over the shared chambers volume,
  served by [qlever-dir](https://github.com/retinue-os/qlever-dir) (included as a
  submodule). Every chamber's RDF files are indexed equally; rebuilds
  automatically on filesystem changes. See
  [`docs/triple-stores.md`](docs/triple-stores.md) for what this makes possible
  — querying Markdown frontmatter, sensor CSVs at scale, and why some data gets
  its own store.

A deployment can add further services in its override — for example a
`qlever-static/`-based endpoint for one large, rarely-changing N-Triples file
(see [`docker-compose.override.example.yml`](docker-compose.override.example.yml)).
Such SPARQL endpoints are advertised to agents through `SPARQL_ENDPOINT_<NAME>`
(and optional `SPARQL_ENDPOINT_<NAME>_DESC`) environment variables on the
`retinue` service, with their hostnames listed in `SPARQL_NO_PROXY` (in `.env`)
so queries bypass the egress-audit proxy.

Your chamber data lives in separate repositories and is cloned into the shared
`chambers` volume at first start. Chambers to mount are declared in
`chambers.json`; each chamber's plugin (if any) is autodetected from its
`.retinue/.claude-plugin/plugin.json`, and the entrypoint generates
`.claude-plugin/marketplace.json` from those at startup.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with Compose
- A [Claude.ai](https://claude.ai) account (used to log in to Claude Code on first start)
- A GitHub personal access token with `repo` scope (for cloning and pushing the mounted chambers)
- A repository for each private chamber you want to mount (declared in `chambers.json`)

## Installation

```bash
git clone --recurse-submodules https://github.com/retinue-os/retinue.git
cd retinue
cp .env.example .env
```

If you already cloned without `--recurse-submodules`:

```bash
git submodule update --init
```

Edit `.env` and fill in your values:

```
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...
TRAEFIK_BASIC_AUTH_USERS=user:$$apr1$$...$$...
SIGNAL_ACCOUNT=+15551234567
```

### Claude-compatible model gateways

Retinue invokes Claude Code for its interactive session, web gateway, and
scheduled agent jobs. Claude Code can use a Claude-compatible endpoint in place
of Anthropic. This keeps Retinue's tools, plugins, permissions, and workflows
unchanged while allowing an Ollama local or cloud model to provide inference.

For Ollama, add the following to `.env` and select a model available from your
Ollama server or Ollama Cloud:

```dotenv
ANTHROPIC_AUTH_TOKEN=ollama
ANTHROPIC_API_KEY=
ANTHROPIC_BASE_URL=http://ollama:11434
RETINUE_CLAUDE_MODEL=qwen3.5
```

`ANTHROPIC_BASE_URL` may instead point at any Claude-compatible gateway. The
optional `RETINUE_CLAUDE_MODEL` is passed as `--model` to every Claude Code
process Retinue starts, so dashboard conversations and scheduled jobs use the
same selected model. Claude Code remote-control sessions are tied to a
Claude.ai login and are therefore disabled when a gateway is configured. Omit
all four settings to retain the default Claude Code authentication and
remote-control session.

OpenRouter exposes a Claude-compatible Messages API. For example, to use
OpenAI's GPT-4o through OpenRouter:

```dotenv
ANTHROPIC_AUTH_TOKEN=sk-or-v1-...
ANTHROPIC_API_KEY=
ANTHROPIC_BASE_URL=https://openrouter.ai/api
RETINUE_CLAUDE_MODEL=openai/gpt-4o
```

Keep the OpenRouter token only in the deployment's untracked `.env` file.

#### Claude Code subscription first, OpenRouter fallback

The included `litellm` service supports Claude Code Pro/Max subscriptions with
OpenRouter failover. It forwards Claude Code's OAuth token only to Anthropic;
LiteLLM retains the OpenRouter key and retries there when the subscription
request fails (for example, due to a burst-rate limit or upstream error).

```dotenv
ANTHROPIC_AUTH_TOKEN=
ANTHROPIC_API_KEY=
ANTHROPIC_BASE_URL=http://litellm:4000
ANTHROPIC_CUSTOM_HEADERS=x-litellm-api-key: Bearer sk-retinue-...
RETINUE_CLAUDE_MODEL=retinue-claude
RETINUE_GATEWAY_USES_CLAUDE_OAUTH=true
LITELLM_MASTER_KEY=sk-retinue-...
LITELLM_PRIMARY_MODEL=anthropic/claude-sonnet-4-20250514
LITELLM_FALLBACK_MODEL=openrouter/anthropic/claude-sonnet-4
OPENROUTER_API_KEY=sk-or-v1-...
```

After starting the stack, run `docker compose run --rm retinue interactive`,
start `claude`, and choose **Claude account with subscription** to authorize
the container. The OAuth token stays in the persistent `retinue-root` volume.

The LiteLLM Admin UI is available at `https://litellm.<your-domain>/ui` when a
deployment routes the `litellm` service through Traefik. It uses the same
client-certificate/basic-auth middleware as the Retinue dashboard. Its
PostgreSQL database is internal-only and stores LiteLLM configuration and logs.

Any chamber repository URLs your deployment clones are supplied via
`chambers.json` (a `url`, or a `url_env` naming an environment variable you set
here) — see [Deployment](#deployment-declaring-your-chambers).

Generate `TRAEFIK_BASIC_AUTH_USERS` with an `htpasswd`-compatible hash (for
example via `htpasswd -nb <user> <password>` and doubling `$` signs when
copying into `.env`). This guards only the public gateway; container-to-container
access on the internal Docker network still reaches the backend `retinue` service
directly without authentication.

#### Client-certificate auth (alternative to the password)

The public router accepts **either** a TLS client certificate **or** the basic-auth
password above. Install a certificate in your browser and you skip the password
prompt entirely. Mechanics:

- Traefik verifies any presented client certificate against a small client CA and
  forwards it to the gateway; the gateway's `/auth` endpoint authorizes on a valid
  certificate and otherwise falls back to the basic-auth prompt. Certificates are
  *optional* (`VerifyClientCertIfGiven`), so existing password access is unchanged.
- Issue a browser `.p12` with `scripts/gen-client-cert.sh` (creates the CA on first
  run). Optionally pin the certificate's CN via `GATEWAY_CLIENT_CERT_CN`.
- One-time Traefik wiring (a file-provider TLS option + the client CA) is described
  in [`deploy/traefik/README.md`](deploy/traefik/README.md) — TLS options cannot be
  set through Docker labels, so they live there.

## Messaging accounts

A messaging account (a Signal number, a linked WhatsApp device, or a Telegram
bot) has exactly one purpose, fixed by configuration and never inferred from a
message's content. Set it with `SIGNAL_GATEWAY_MODE` (Signal),
`WHATSAPP_GATEWAY_MODE` (WhatsApp), or `TELEGRAM_GATEWAY_MODE` (Telegram):

| Mode | The account is… | Inbound handling | Reply to sender? |
|------|-----------------|------------------|------------------|
| `control` | a control channel for operating Retinue (the classic gateway) | run as a prompt to Ara | yes — voice/text on the same channel |
| `inbox` *(default)* | one of the user's own message sources, like an e-mail inbox | forwarded to triage as the user's incoming mail, surfaced on the dashboard as a push notification | no |

The default is `inbox`, so an account left unconfigured **cannot drive the
system** — exposure defaults closed. Turning an account into a control channel is
an explicit opt-in (`SIGNAL_GATEWAY_MODE=control`) and still requires the sender
to be on the accepted-requesters allowlist (below). Because the mode is a
property of the account, the triage skill never has to decide whether an incoming
message is a system instruction or user mail.

### Linking a Signal account

Each account must be authenticated once with signal-cli before its gateway can
send or receive. Run the command against the account's service so it writes into
that service's `signal-data` volume (below), where it then persists:

- **A number you control exclusively** (e.g. Ara's own control number) — register
  it, then confirm with the SMS/voice code:

  ```bash
  docker compose run --rm signal-gateway signal-cli -a +15551234567 register
  docker compose run --rm signal-gateway signal-cli -a +15551234567 verify 123456
  ```

- **The user's existing Signal account** (typical for an `inbox` source) — link
  the gateway as a secondary device:

  ```bash
  docker compose run --rm signal-gateway signal-cli -a +15557654321 link -n retinue
  ```

  This prints a `sgnl://linkdevice?...` URI; render it as a QR code and scan it
  from the phone's Signal app under *Settings → Linked devices*.

For an account added in the override, use its service name in place of
`signal-gateway` (e.g. `signal-gateway-personal`).

### Account data (the `signal-data` volume)

Each gateway runs one [signal-cli](https://github.com/AsamK/signal-cli) instance,
which keeps that account's registration — identity keys, the linked-device
session, and delivery state — under `/root/.local/share/signal-cli`. The base
compose mounts the named `signal-data` volume there so this state **survives
container restarts**; without it you would have to re-link the account (see
[Linking a Signal account](#linking-a-signal-account)) after every restart.

**Every account needs its own volume.** Do not point two gateways at the same
`signal-data` volume: signal-cli takes an exclusive lock on the data directory,
so two processes sharing it would collide, and the second account's keys would
overwrite the first. When you add an account (below) you give its service a
fresh named volume mounted at the same path.

### Adding more accounts

The base `signal-gateway` service is account #1, configured from `.env`
(`SIGNAL_ACCOUNT`, `SIGNAL_GATEWAY_MODE`, …). To add further accounts, declare
one extra service per account in your deployment's `docker-compose.override.yml`
— the same file that carries the rest of your deployment wiring. Each extra
service reuses the base image via `extends`, then overrides just what differs:
its own `SIGNAL_ACCOUNT`, its `SIGNAL_GATEWAY_MODE`, and its **own** named
`signal-data` volume.

For example, to keep Ara's control number as account #1 (in `.env`) and add the
user's personal number as an `inbox` source:

```yaml
# docker-compose.override.yml
services:
  # Account #2: the user's personal number, an inbox source that feeds triage.
  signal-gateway-personal:
    extends:
      file: docker-compose.yml
      service: signal-gateway
    environment:
      - SIGNAL_ACCOUNT=+15557654321      # this account's own number (E.164)
      - SIGNAL_GATEWAY_MODE=inbox        # forward to triage, no reply
    volumes:
      - signal-data-personal:/root/.local/share/signal-cli   # its OWN volume
      - piper-data:/models
    networks:
      - agents

volumes:
  signal-data-personal:
```

`extends` inherits the build, ports, and remaining environment from the base
service; the block above only names what is different for account #2. Add as
many such services as you have accounts — a full example is in
[`docker-compose.override.example.yml`](docker-compose.override.example.yml).
The `whatsapp-gateway` and `telegram-gateway` services follow the same
one-service-per-account shape (each with its own `*_GATEWAY_MODE` and its own
data volume).

### WhatsApp accounts

WhatsApp is reached through the sibling `whatsapp-gateway` service, which owns a
linked-device WhatsApp Web session (via the neonize/whatsmeow bridge) the same
way `signal-gateway` owns a signal-cli account. It has the identical `control`
vs `inbox` mode model (`WHATSAPP_GATEWAY_MODE`, default `inbox`), the identical
email-style outbound send-control (`WHATSAPP_SEND_POLICY`, keyed — like
`EMAIL_SEND_POLICY` — by the **sending** identity, this gateway's own
`WHATSAPP_ACCOUNT` number, not the recipient; an undeclared account defaults to
`verify`), and surfaces its pending sends on the same `/sends` approval page.
Link the device once: start the
service and scan the pairing QR it prints to its logs
(`docker compose logs -f whatsapp-gateway`) from the phone under
*Settings → Linked devices*. The session persists in the `whatsapp-data` volume.
Agents send with `scripts/whatsapp-push.py` and resolve contacts with
`scripts/whatsapp-contacts.py`.

### Telegram accounts

Telegram is reached through the sibling `telegram-gateway` service, which logs in
as the user's **own Telegram account** — an MTProto user client (Telethon), not a
bot. Acting as the user is what makes it fit for purpose: it can message any of
the user's contacts *as them*, read the user's own incoming DMs (so `inbox` mode
genuinely triages the user's Telegram mail), and enumerate the real contact
directory — the same account access an MCP user client has, but with the
credentials isolated in the container. Same `control`/`inbox` mode model
(`TELEGRAM_GATEWAY_MODE`, default `inbox`) and the same email-style send-control
(`TELEGRAM_SEND_POLICY`, keyed by the **sending** identity — this account,
`TELEGRAM_ACCOUNT`, defaulting to the account's `@username`/phone; since it is the
user's own account the fail-safe default means every send needs approval unless
granted), surfaced on the same `/sends` page.

Set it up once:

1. Create an MTProto app at [my.telegram.org](https://my.telegram.org) → *API
   development tools*; put the `api_id`/`api_hash` in `TELEGRAM_API_ID` /
   `TELEGRAM_API_HASH` and the number in `TELEGRAM_PHONE`.
2. Log in interactively (writes the session into the `telegram-data` volume;
   prompts for the code Telegram sends, and the 2FA password if set):

   ```bash
   docker compose run --rm -it telegram-gateway python3 /app/telegram-gateway.py login
   ```

Thereafter the service starts non-interactively from the stored session. Agents
send with `scripts/telegram-push.py` and resolve chats with
`scripts/telegram-contacts.py` (recent chats first, contact directory as
fallback). Session state persists in the `telegram-data` volume.

Each new account must be linked once before it can send or receive — see
[Linking a Signal account](#linking-a-signal-account).

### Sending from a specific account

Outbound pushes are addressed to a **gateway service**, not to an account
number: each `signal-gateway` service owns one account and exposes its own
`/send` endpoint on the `agents` network at `http://<service-name>:8090/send`.
So the choice of "which account do I send from" is the choice of *which
gateway's send URL you post to*:

- **Default** — `scripts/signal-push.py` posts to `SIGNAL_GATEWAY_SEND_URL`
  (default `http://signal-gateway:8090/send`, i.e. account #1). This is the
  right identity for system messages (alerts, escalations, daily briefings).
- **A specific account** — point the client at that account's service with
  `--url`, e.g. to send from the personal account added above:

  ```bash
  scripts/signal-push.py --url http://signal-gateway-personal:8090/send \
      --recipient +15551112222 "…"
  ```

`--recipient` chooses *who receives* the message (defaulting to
`SIGNAL_DEFAULT_RECIPIENT`); `--url` chooses *which account it comes from*.

### Outbound send-control (`SIGNAL_SEND_POLICY`)

Outbound Signal messages are gated by a policy that mirrors `EMAIL_SEND_POLICY`
exactly. As with e-mail, the category is keyed by the **sending identity** — this
gateway's own `SIGNAL_ACCOUNT` number — **not** the recipient: what governs
whether an agent may post autonomously is which number the message goes out *as*,
not who it is addressed to. (Who may message *in* to drive the system is the
separate inbound control — the accepted-requesters allowlist, control mode only.)
`SIGNAL_SEND_POLICY` is a JSON array of `{number, category}` entries keyed by the
sending account (E.164 number, `"*"` as a wildcard default):

- `allow` — send directly, no confirmation (e.g. a dedicated agent number).
- `trust` — send directly only when `signal-push.py` passes `--user-approved`
  (used when the user explicitly requested the send in a conversation);
  otherwise the send falls back to the `verify` flow.
- `verify` — register the message as a **pending send**; it is transmitted only
  after explicit approval on the web gateway's `/sends` page. An agent can never
  approve its own send.

An account matching no entry and no `"*"` wildcard falls back to `verify`
(fail-safe, same default as e-mail), so an undeclared account can never post
autonomously. So: a dedicated agent number (say Ari's) can be `allow` while the
user's own linked number stays `verify`.

```bash
# Recipients matched by a verify/trust policy return a pending-approval notice
scripts/signal-push.py --recipient +15551112222 "Draft reply to review"
# → signal-push: send queued for approval (id=…)
#   signal-push: approve or deny at https://agents.example.com/sends/signal/…
```

Pending Signal sends appear on `/sends` alongside e-mail approvals; the
web-gateway fetches them from the signal-gateway's token-gated `/pending-sends`
API (`SIGNAL_GATEWAY_BASE_URL`) and proxies the allow/deny action back to it.
`SEND_APPROVAL_BASE_URL` sets the public host used to build the approval link
returned to the caller.

### Reading the roster (`/recent-chats`, `/contacts`, `/groups`)

Sending a message first requires resolving a name (e.g. "Jane Doe") to a
number — the contact-lookup step. The gateway exposes the account's own roster
for this over three token-gated `GET` endpoints on the `agents` network:

- `GET http://<service-name>:8090/recent-chats` → `{"recent_chats": [{number, uuid, name, last_seen}, …]}`
- `GET http://<service-name>:8090/contacts` → `{"contacts": [{number, uuid, name}, …]}`
- `GET http://<service-name>:8090/groups` → `{"groups": [{id, name}, …]}`

`/contacts` and `/groups` are backed by `signal-cli listContacts` / `listGroups`
and serialized through the same lock as the receive loop, so they never race it.
`/recent-chats` is different: signal-cli keeps no queryable message history, so
the gateway builds its own — it records each inbound sender (identifiers, the
envelope's name, a last-seen time) as messages arrive, most-recent-first,
persisted on the pending-sends volume. This is the gateway's stand-in for
"recent conversations".

The [messaging-contact-lookup](.claude/skills/messaging-contact-lookup) skill
mandates **recent conversations first, the contact directory only as a
fallback**. `scripts/signal-contacts.py` (mirrors `signal-push.py`: `--url`
picks the account/gateway, `SIGNAL_GATEWAY_TOKEN` authorizes) implements exactly
that order — a name query hits `/recent-chats` first and only falls back to
`/contacts` on a miss. Each result carries a `source` field so the caller knows
which layer answered:

```bash
# Resolve a name: recent chats first, directory as fallback (the default)
scripts/signal-contacts.py --url http://signal-gateway-personal:8090 --query doe
# → [ { "number": "+15551112222", "uuid": "…", "name": "Jane Doe",
#       "last_seen": 1752…, "source": "recent-chats" } ]

scripts/signal-contacts.py --query doe --contacts  # force the directory, skip recent
scripts/signal-contacts.py --all               # dump recent chats
scripts/signal-contacts.py --all --contacts    # dump the contact directory
scripts/signal-contacts.py --groups            # list groups on account #1
```

This is what lets an agent look up a contact and send in one flow. The gateway
is the sole Signal contact path and works in scheduled/headless sessions. The
endpoints are **read-only** — they never send.

### Accepted requesters

In **`control`** mode the gateway only answers requests from allowlisted
requesters when `on-behalf-of` is present. Any chamber may contribute an
`accepted-requesters.txt` at its root (one requester identity URI per line);
the gateway unions them all. Empty
lines and `#` comments are ignored; comma-separated values on a line are
supported. For phone-number identities, use `tel:` URIs (for example
`tel:+15551234567`). The Signal gateway sends sender numbers as `tel:`
URIs in `on-behalf-of`; requests without `on-behalf-of` skip this check. If no
allowlist entry exists, requests with `on-behalf-of` are rejected. To use a
single explicit file instead, set `ACCEPTED_REQUESTERS_PATH` in `.env`.

**`inbox`** accounts do not use the allowlist: their messages are the user's own
incoming mail, processed under the owner's session and never run as prompts, so
the external sender is never treated as an authorised requester.

## First start

Builds the image, mounts the chambers into the container, and
drops into an interactive shell so you can authenticate Claude Code:

```bash
docker compose run --rm retinue interactive
```

Inside the container, run:

```bash
claude
```

Follow the prompts to log in and trust the `/workspace` folder. Once done, exit
the shell (`Ctrl-D`).

## Normal start

After the first-time setup, start the system in remote-control mode (detached,
auto-restarts on failure or reboot):

```bash
docker compose up -d
```

To stop it:

```bash
docker compose down
```

Claude Code will be reachable via the Claude.ai interface or any configured
remote-control client.

## What happens at startup

1. Each chamber declared in `chambers.json` is made available at
   `/workspace/chambers/<name>`, inside the shared `chambers` volume. If that
   directory already has content — a previous clone, or a directory the
   deployment mounts there — it is used as-is; otherwise the entrypoint clones
   the chamber's `url`, or symlinks a local `path`. The base `docker-compose.yml`
   mounts `chambers.example.json` by default, booting the two example chambers.
   Deployments override this mount in `docker-compose.override.yml`.
2. Each chamber that carries a plugin (`.retinue/.claude-plugin/plugin.json`) is
   autodetected; the entrypoint generates `.claude-plugin/marketplace.json` from
   the identity template and installs the plugins, making their subagents
   available in every session.
3. Python dependencies from every chamber's `requirements.txt` (if present)
   are installed in the `retinue` container.
4. The `qlever-life` service indexes every `.nt`/`.ttl`/`.n3` file in the shared
   chambers volume — all chambers equally — and serves it on `qlever-life:7001`
   (network; publish a host port via the deployment override). It watches for
   filesystem changes and rebuilds blue-green within ~15 s.
5. Git hooks are installed in every chamber that is a git repository.
6. For every chamber with a `.refresh.json`, the background refresh dispatcher
   is started (see `scripts/refresh.py`).
7. The selected mode launches (`interactive` or `remote-control`).
8. The `signal-gateway` service polls Signal messages and sends spoken replies,
   and serves the outbound `/send` endpoint for agent-initiated pushes.

## Deployment (declaring your chambers)

This framework is content-neutral. A deployment supplies its own chambers and
edge wiring without forking the framework:

1. Provide a `chambers.json` (content-only: `name`, optional `url`/`url_env`, or
   a local `path`). See [`chambers.example.json`](chambers.example.json) and the
   [example chambers](examples/chambers/).
2. Copy [`docker-compose.override.example.yml`](docker-compose.override.example.yml)
   to `docker-compose.override.yml` (Compose merges it automatically; it is
   git-ignored). It bind-mounts your `chambers.json` over the shipped example and
   supplies the deployment-specific bits the base compose leaves out: the public
   Traefik router/host rule and basic-auth/client-cert middlewares, the external
   `web` network, and any published QLever host ports.

A typical deployment lives in its own repo that pins `retinue-os/retinue` as a git
submodule (reproducible) and contains that override, `chambers.json`, and `.env`.

## Mounting a chamber from the host (e.g. for secrets)

Chambers in `chambers.json` are normally cloned into the container on first
start. If a chamber needs **host-managed, uncommitted files** — typically a
project-scoped secrets file — provide it as a host bind-mount instead. The
entrypoint detects an already-present chamber (`.git` or non-empty directory) and
uses it as-is rather than cloning, so your working copy and its secrets stay put.

Keep this host-specific wiring out of the committed compose by using a
**`docker-compose.override.yml`** (Compose merges it automatically; it is
git-ignored):

```yaml
# docker-compose.override.yml  (not committed)
services:
  retinue:
    volumes:
      - /abs/host/path/to/ari:/workspace/chambers/ari
  qlever-life:
    volumes:
      - /abs/host/path/to/ari:/data/ari:ro
```

Both mounts are required: `retinue` needs the chamber for agent code and data,
and `qlever-life` needs it so the chamber's RDF files are indexed by the live
SPARQL store. The `qlever-life` mount uses the same bind-source but targets
`/data/<chamber-name>` (read-only).

Then on the host:

```bash
git clone git@github.com:you/assistant.git /abs/host/path/to/ari
cd /abs/host/path/to/ari
cp .secrets.env.example .secrets.env   # fill in credentials (gitignored)
```

Because the chamber is pre-mounted, the entrypoint will not clone or pull it —
update it yourself with `git pull` on the host.

## Layout

```
retinue/
  CLAUDE.md            ← session instructions baked into the runtime image
  agents/              ← core agent role definitions (Archivist, Academic, …)
  chambers.example.json ← example chamber manifest (deployment provides chambers.json)
  examples/chambers/   ← runnable example chambers (Westworld, Hitchhiker)
  .claude-plugin/      ← marketplace identity template (marketplace.json generated at runtime)
  scripts/             ← operational scripts (ingestion, hooks, refresh)
  Dockerfile           ← image for the `retinue` service
  signal-gateway/      ← image definition for the `signal-gateway` service
  docker-compose.yml   ← deployment-neutral base composition
  docker-compose.override.example.yml ← deployment-specific wiring template
  qlever-dir/          ← submodule: live SPARQL-over-directory service
  qlever-static/       ← single-file SPARQL service (optional, deployment override)
```

Domain agents are not defined here: each mounted chamber ships its own
(the example `westworld` chamber provides `.retinue/agents/dolores.md` as a
Claude Code plugin).

## Updating the image

To pick up changes to agents, scripts, or dependencies:

```bash
git pull
docker compose build
```

Your chamber data lives in named volumes and is unaffected by rebuilds.
