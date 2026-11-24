# Contributing from inside the container — PRs, layouts, updates

*Reference depth for the "Branch policy" and environment digests in
`CLAUDE.md`. The tier definitions themselves live there; this doc carries the
mechanics: how to open a PR from inside the container, the mount layouts, the
language-convention rationale, and how the stack updates itself.*

## How to PR the retinue repo from inside the container

The framework checkout is mounted read-write, so normally no `/tmp` clone is
needed — branch, commit, and push straight from the live checkout, except in
the third layout below, where a plain clone is the documented fallback. Always
work on a feature branch; never leave `main` dirty on this checkout, since it
is also what's deployed.

**First, find where the framework checkout is.** Three mount layouts are
possible, depending on the deployment:

- **Bare framework** (default `docker-compose.yml`): the framework's own live
  checkout is mounted at `/workspace/deployment`. That directory *is* the
  `retinue-os/retinue` repo.
- **Nested deployment** (a deployment repo like `my-retinue` that clones the
  framework into a `retinue/` subfolder and overrides the mount to bind its
  **root** at `/workspace/deployment`): then `/workspace/deployment` is the
  private deployment repo, and the framework checkout is one level down at
  `/workspace/deployment/retinue`.
- **Submodule mount without its parent's `.git`**: the framework is checked
  out as a git submodule, but only the submodule's own directory is mounted —
  its `.git` file points at a gitdir under the parent repo's
  `.git/modules/...`, which isn't reachable inside this container. The files
  land on disk at one of the two paths above, but neither is a usable git
  checkout in place. When the detection below reports this, don't work around
  it — clone into `/tmp` instead
  (`git clone https://github.com/retinue-os/retinue /tmp/retinue-fix`) and
  branch/commit/push/PR from there.

Don't assume — detect by content, not by asking git: a checkout whose gitdir
isn't mounted makes a git-based check (e.g. reading `origin`) fail silently
rather than error, which is exactly the third layout above. The framework dir
is whichever candidate has both `chambers.example.json` and `Dockerfile` at
its root; fail loudly instead of defaulting if neither does, or if the
resolved path isn't actually a usable git checkout:

```bash
# Resolve the framework checkout regardless of layout, by content — not by asking git:
FW=
for cand in /workspace/deployment /workspace/deployment/retinue; do
  if [ -f "$cand/chambers.example.json" ] && [ -f "$cand/Dockerfile" ]; then FW=$cand; break; fi
done
[ -n "$FW" ] || { echo "error: no framework checkout under /workspace/deployment" >&2; exit 1; }
git -C "$FW" rev-parse --git-dir >/dev/null 2>&1 || {
  echo "error: $FW is not a usable git checkout (gitdir not mounted?)" >&2; exit 1; }
cd "$FW" || exit 1

git config user.email "you@example.com" && git config user.name "Ara (Claude)"
git checkout -b fix/my-change
# edit files, then:
git add <files> && git commit -m "fix: ..." && git push -u origin fix/my-change
gh pr create --title "..." --body "..."
git checkout main   # return the live checkout to main once the PR is out
```

(In the nested layout, `/workspace/deployment` itself is the deployment repo —
e.g. `you/my-retinue`, private — holding the tracked `docker-compose.override.yml`,
`chambers.json`, `start.sh` and secrets. Commit host-specific changes there, not
in the framework.)

The repositories at a glance:

| Repo | Mounted at | Purpose |
|------|------------|---------|
| framework — `retinue-os/retinue` (formerly `health-agents`) | baked into the image as `/workspace`; live checkout also RW-mounted — at `/workspace/deployment` (bare-framework layout) or `/workspace/deployment/retinue` (nested-deployment layout, where the deployment repo owns `/workspace/deployment`); if neither gitdir is reachable (submodule mount without its parent's `.git`), fall back to a `/tmp` clone | Infrastructure: core agents, skills, scripts, settings, Dockerfile, repo/plugin manifests |
| each chamber — its own git repo | cloned or linked at startup as `/workspace/chambers/<name>` | That chamber's data, plus its `.retinue/` plugin (subagents, skills) and its `INSTRUCTIONS.md` |

## No owner specifics in the framework

The framework is content-neutral: it never names the owner, their accounts,
mailbox folders, contacts, chambers, hosts or the agents a chamber provides.
Any of those is **chamber or deployment configuration**:

- A data source, its fetch script and its quirks (where a passcode mail
  lands, which folder a server rule files it in) belong to the chamber that
  consumes the data — `.retinue/scripts/`, declared in its `.refresh.json` or
  `.schedule.json`, documented in its `INSTRUCTIONS.md`.
- A credential such a script needs is passed by the deployment's
  `docker-compose.override.yml` and named in `RETINUE_SESSION_ENV_EXTRA`
  (see "Session environment" below) — never added to the framework's compose
  file, `.env.example` or the session allowlist.
- A setting with a sensible general default (a folder, a sender) is a
  parameter with that default, never the owner's value hard-coded.
- Examples, comments, prompts and test fixtures use placeholders — "the
  owner", "an agent persona", `owner@owner.example`, `<chamber>` — not real
  names, addresses or chamber names. Invented demo data (`webapp/data/`) and
  copyright notices are exempt.

When a framework file has to know something about one chamber to work, the
knowledge is in the wrong repository: move it there and leave a generic hook.

## Language convention — the rationale

The rule itself is in `CLAUDE.md`: all non-user-facing natural language
(comments, commit messages, issues, PRs, mechanics documentation) is English,
and the project has **no preferred natural languages other than English** — a
feature is either multilingual by design or English-only. The reasoning behind
the second half, for reviews:

Concretely, when a feature needs to reason about the language of some content
(speech-synthesis language tags, locale-aware formatting, detection, …):

- **Do not** hand-code a bias toward one language — e.g. a German word list, an
  umlaut check, or a `de`-vs-`en` special case. That privileges a single
  non-English language, which this project does not do.
- **Do** use a language-agnostic mechanism that treats every language uniformly
  (a general detector, a proper locale API, per-item language metadata), or keep
  it English-only if multilingual support isn't warranted yet.

This applies even though the *user* often communicates in German: answering the
user in their own language (user-facing content) is a per-message response, not
a structural preference baked into the system. Apply the convention going
forward; retroactively fixing existing issues or PRs is not required.

## Self-update (the `updater` sidecar)

To rebuild and restart the whole stack (`git pull && docker compose build &&
docker compose up -d`) without SSHing into the host — e.g. after merging a
Tier 3 PR — run `python3 /workspace/scripts/self-update.py`. It pokes the
`updater` sidecar (a separate compose service, since the `retinue` container
recreating itself mid-`up -d` would kill the process issuing the command);
the same endpoint is reachable over HTTPS from the phone, token- and
basic-auth-gated. See `docker-compose.yml` (`updater` service) and
`updater/update-server.py`.

The `updater` runs an **operator-configured** recipe, not a hard-coded one: it
reads `UPDATE_COMMAND` from its environment and, when unset, defaults to the
framework's own `git pull && docker compose build && docker compose up -d`. This
keeps the dependency direction right — the generic framework never names a
specific deployment. A deployment that updates differently (e.g. the nested
`my-retinue`, which owns both the deployment repo and the framework clone and
updates via its own `start.sh update`) injects its recipe by setting
`UPDATE_COMMAND` in its override/`.env`; config flows deployment → framework.
The HTTP caller can never supply the command — only the operator's environment.

### Which build is actually running

A merge is not a deployment. Until `self-update.py` runs, every container keeps
serving whatever was built last time — and that reads as a logic bug, because
behaviour the code no longer contains goes on happening. It has cost a
debugging session twice.

So every image says what it is, on its `/health`:

```json
"build": {"sha": "b5ff537e3237b00d…", "framework": "701e3b415854"}
```

- **`sha`** — the commit the image was built from, when the build passed one.
  The built-in recipe reads `HEAD` after the pull and hands it to
  `docker compose build` as `RETINUE_BUILD_SHA`; compose forwards it to each
  image's `ARG`. `null` means nobody stamped this build, which is reported
  rather than guessed at.
- **`framework`** — a digest of the shared modules *as baked in*, computed at
  runtime by `scripts/build_stamp.py`. It needs nothing from the build
  pipeline, so it is always there. The retinue container and every channel
  gateway — Signal, WhatsApp, Telegram, SMS and the mail gateway — copy the
  same modules (`build_stamp.FRAMEWORK_MODULES`), so **a gateway whose digest
  differs from the dashboard's was not rebuilt** — which the `/gateways` page
  says on the card, in as many words, even while the gateway is connected and
  otherwise looks well. The mail gateway also bakes modules no messenger
  carries (`email_client.py`, `mail_threads.py`, `mail_trim.py`); those are
  outside the digest, so a mail gateway stale in them alone shows only by its
  `sha`. A new gateway image copies every module in `FRAMEWORK_MODULES` and
  joins the lists `tests/test_build_stamp.py` pins (its Dockerfile, its compose
  service, the health loop).

To check a deployment: open `/gateways`, or compare by hand —

```bash
python3 scripts/build_stamp.py              # what this checkout bakes
curl -s "$GATEWAY/health" | jq .build       # what is running
```

**A deployment with its own `UPDATE_COMMAND` stamps its own builds**, because
only that recipe knows where its pull ends: put `RETINUE_BUILD_SHA=$(git
rev-parse HEAD)` in front of the build command. Skipping it costs only the
`sha` — the framework digest, and with it the stale-gateway check, keeps
working regardless.

### Upgrading the Claude Code CLI

A deployment may disable Claude Code's in-container auto-updater
(`DISABLE_AUTOUPDATER=1`) — it unpacks the new release beside the old one and
swaps it in, and a restart landing mid-swap leaves neither, with the damage in
the container's writable layer so every subsequent restart replays it. Where
it is disabled, the image is the only place the version moves.

That makes the Dockerfile's pin load-bearing: `ARG CLAUDE_CODE_VERSION`, used
by the `npm install -g @anthropic-ai/claude-code@${…}` line. **Do not drop the
version and let npm resolve `latest`** — it looks like it tracks upstream and
does the opposite. That layer depends on no file in the repo, so
`docker compose build` reuses the cache forever and the version silently
freezes at whatever was current the day the layer was first built (this is how
the runtime ended up months behind, on a version too old for a newly released
model). Naming the version changes the `RUN` command, which invalidates the
layer, which is what makes `self-update.py` actually deliver the upgrade.

`.github/workflows/check-claude-code.yml` polls the npm registry daily and
opens a bump PR against that ARG, closing any older still-open bump PR so a
late merge cannot pin the version backwards. Merging one is a normal Tier 3
merge followed by `python3 /workspace/scripts/self-update.py`.

## Claude sign-in monitoring

The OAuth sign-in every Claude Code process shares
(`/root/.claude/.credentials.json`) is watched the same way the messenger
links are: `scripts/claude-auth-monitor.py` (forked by the entrypoint, plain
file reads, no credits) opens a dashboard conversation — Web-Pushing the user
— days **before** the sign-in's refresh token expires, and immediately when
the credentials die early (token-rotation clobber with a rejected backup).
The notice points at the dashboard's **`/claude-auth`** page, where the user
re-logs in from the browser: open the authorize link, approve, paste the
displayed code — the gateway writes the credential file and restarts the
container. Do **not** build ad-hoc login checks; if the user asks about
the Claude login or agents failing to authenticate, check
`python3 /workspace/scripts/claude_auth.py status` and point them at
`/claude-auth` (that script's `login` subcommand is the console fallback —
prefer it over running `claude` via `docker exec`, which rotates tokens under
the live session and is itself a cause of early sign-outs). Every `claude` the
framework starts first runs that script's pre-spawn refresh under a lock all
spawners share, so its own processes never race for the rotation; before
starting one by hand beside the live system, run
`python3 /workspace/scripts/claude_auth.py refresh`. Details:
`/workspace/docs/claude-auth.md`.

## Session environment — the allowlist

Every `claude -p` the framework spawns — dashboard turns, the transcript
cleanup and the presentation lint (`scripts/web-gateway.py`), scheduler jobs
and the sessions the base-job scripts start themselves (`scripts/scheduler.py`,
`agent-self-review.py`, `news-curate.py`, `inbox-sweep.py`,
`memory-compact.py`, `migrate-triage.py`), Ask-Ara answers
(`scripts/ara-mcp-server.py`) — gets its environment from
`scripts/session_env.py`, never from a copy of the spawning daemon's. The
daemons are forked before the entrypoint's scrub and hold whatever `.env` put
into the container, mailbox passwords included; a copied environment inherits
all of it (retinue-os/retinue#15). The module is an allowlist, not a denylist,
because a denylist rots: a new secret leaks until someone extends the list and
nothing fails when they forget, whereas a forgotten *non*-secret fails loudly
in the script that reads it.

What passes: process basics (`PATH`, `HOME`, locale, `TZ`), the egress-proxy
and CA variables, `ANTHROPIC_*` (the model credential and endpoint —
gateway-spawned sessions run in API-key mode), `CLAUDE_*`, `RETINUE_*` (minus
`RETINUE_LITELLM_KEY`), `SPARQL_ENDPOINT_*`, `NEWS_*`, the two delivery-gate
policy locations (`TRIAGE_MESSENGER_DIR`, `TRIAGE_EMAIL_WHITELIST_PATH` — named
one by one, not by a prefix; the second is the e-mail policy file's path, whose
variable name predates the whitelist's retirement), the
capability tokens with their service URLs (`EMAIL_BACKEND_*`,
`CONVERSATION_BACKEND_*`, `NEWS_INGEST_*`, `CHATS_INGEST_*`, `UPDATER_*`), the
messenger, mail and calendar gateways' client side (`*_GATEWAY_TOKEN`,
`*_GATEWAY_BASE_URL`, `*_GATEWAY_SEND_URL`, `*_DEFAULT_RECIPIENT`, …;
`MAIL_GATEWAY_*` among them — the mail gateway's own `MAIL_*` settings are
not, and live on its service only).
Everything else is dropped — `EMAIL_PASS*`,
`LITELLM_*`, `OPENROUTER_API_KEY`, `TRAEFIK_BASIC_AUTH_USERS`, `GITHUB_TOKEN`
(`git` keeps working through the credential helper the entrypoint configures;
`gh` is unauthenticated in a spawned session, while the remote-control main
session still carries the token) and any secret nobody has named yet. One
credential passes on purpose: the model credential in `ANTHROPIC_*`, without
which a spawned session cannot run. A chamber's own credentials (a data
source's login for its `.refresh.json` fetch, say) pass only when the
deployment names them, below. `RETINUE_SESSION_MODEL`
and `RETINUE_ESCALATE_FILE` are set per spawn, never inherited, and
`EMAIL_BACKEND_URL` is pointed at the gateway's backend whenever the spawner
holds the token, so `email_client.py` proxies in every session.

A deployment whose chamber scripts read variables the framework does not know
names them in `RETINUE_SESSION_ENV_EXTRA` (comma- or space-separated; a
trailing `*` admits a prefix, `MYCHAMBER_*`) — on the `retinue` service in its
override, where the variables themselves must be listed too, since the service
no longer loads `.env` wholesale (`docker-compose.yml`, the `retinue` service,
names what it passes). `python3 /workspace/scripts/session_env.py` prints the
names the current environment would pass, `--dropped` the names it withholds
— names only, never a value. The remote-control main session is not spawned
this way: it keeps the entrypoint's own scrub (mail credentials and the API
key, for OAuth). What the allowlist cannot do is stop a session from reading a
daemon's `/proc/<pid>/environ`, since everything runs as one uid; that is the
sidecar work tracked separately (`SECURITY.md`, "Known limitations").

## The triage migration (one-time)

`scripts/migrate-triage.py` moves what e-mail triage knew — the whitelist and
the `sender:` memories — onto contacts, as their instruction. What it does and
why is `docs/mail-chats.md`, "The migration"; this is how an operator runs it.

**It runs by itself.** The entrypoint secures the legacy whitelist triples
first thing (`--step snapshot`, which also fixes the cutoff: only `sender:`
memories recorded before the new code first ran are triage's; it is kept in
the marker, in `.migration-cutoff` beside it and in the snapshot) and starts the
migration in the background once the daemons are up (`--boot`): the pending
steps, a few tries where the life store or the model is not there yet, polls
while the mail mirror does its first backfill (at most
`MIGRATE_TRIAGE_MIRROR_WAIT`, 6 h by default), then one dashboard thread with
the report — followed up in the same thread if it had to say "not finished".
A boot that finds it complete does nothing. Its log lines carry
`[migrate-triage]`. A `--step snapshot` that exits non-zero with "not secured
in full" means a policy file is there and cannot be read: both steps wait
until it reads, and nothing is taken for "no whitelist".

**Set `MAIL_GATEWAY_TOKEN` as part of this upgrade.** Triage retires whether
or not the mail gateway runs, and the gateway mirrors and gates nothing until
the token is set (`docs/messaging.md`, "E-mail (the mail gateway)"): a
deployment without it has **no e-mail handling at all** after the upgrade.
The migration then moves nothing (it needs the mirror), the report says so
first, a job left calling `triage-gate.py` says so in its log, and the boot
run opens one dashboard thread about it (on a deployment that had e-mail
triage). Set the token (`openssl rand -hex 32`) and restart; the migration
runs once the mirror has its first backfill.

**To look before it acts**, keep the boot run from doing anything and read a
dry run — which writes nothing and spawns no model:

```bash
# before the upgrade's first start: either the file …
docker compose run --rm --entrypoint sh retinue -c \
  'mkdir -p /root/.retinue/triage && touch /root/.retinue/triage/.migration-hold'
# … or RETINUE_TRIAGE_MIGRATION=off on the retinue service (override file)

docker exec <retinue> python3 /workspace/scripts/migrate-triage.py --dry-run
```

Remove the hold and restart, or run it by hand:

```bash
docker exec <retinue> python3 /workspace/scripts/migrate-triage.py            # what is pending, then the report
docker exec <retinue> python3 /workspace/scripts/migrate-triage.py --step report [--json]
docker exec <retinue> python3 /workspace/scripts/migrate-triage.py --step memories [--no-model]
```

Run it with `docker exec` on the `retinue` container rather than from an
agent session where you can: the user's own addresses get no contact, and the
script knows them by the mailbox variables a session's environment does not
carry (a session gets `TRIAGE_STATE_DIR`, `MAIL_OWN_ADDRESSES`, the
script's tunables and the two verdict timeouts its report states,
`MAIL_INSTRUCTION_TIMEOUT` and `CHAT_INSTRUCTION_TIMEOUT` — enough for the
thread's "show the report" — but never
`EMAIL_USER*`; beyond those the script knows them only by what the mirror
shows: its accounts, and the addresses the user's mail went out under). A run
by hand opens no thread; it prints the report. The memories step starts a
`claude` process the way every framework spawner does — it refreshes the
sign-in under the shared lock first — so it needs no `claude_auth.py refresh`
beforehand.

- **A full run skips the steps the marker has complete; `--step <name>` runs
  that step again whatever the marker says.** That is how to have it look
  again after fixing something: `--step memories` re-reads the store and puts
  what a model verdict left alone to the model once more — only memories
  from before the cutoff, never a later one. A whitelist entry that was
  settled and a memory that was moved are never touched twice; a rule that
  is on a contact and whose memory waits to be superseded is not written
  again, so an edit on the contact meanwhile stands.
- **Exit status** 0: everything asked for is complete; 75: something is
  pending, and the report says what; 1: the marker, the cutoff file or the
  snapshot could not be read — nothing was changed, and the log line names
  the file and its way out:
  - **the marker** (`.migrated-to-contacts.json`): move it aside to start
    its journal afresh (every step is safe to repeat), but **keep
    `.migration-cutoff` and `.legacy-email-whitelist.json`**: the cutoff they
    hold is what keeps the sender memories stored since the upgrade out of
    the migration, and a marker that is missing adopts it from them;
  - **the cutoff file** (`.migration-cutoff`): where the marker or the
    snapshot still holds the cutoff (the log line says so, with the time),
    move it aside — the next run writes it back from them; where neither
    does, restore it from a backup (one line, the time it held), since a run
    with none of the three takes a new cutoff;
  - **the snapshot** (`.legacy-email-whitelist.json`): restore it from a
    backup rather than move it aside — it holds the legacy whitelist, which
    the policy file loses on its next write; moved aside, only what the
    policy files still hold is secured again.
- **The model only classifies.** Either way an instruction's text is the
  memory's own, first — for a filing memory whose text names a document and
  filing it, followed by one fixed line that yields to those words
  ("Unless the note above says otherwise: file the document …"), placed as
  the instruction's last paragraph and known by that opening however you
  edit or move it — a later note goes in front of it, and it is never added
  twice; nothing the model writes reaches a contact,
  and nothing is ever put in front of the user's words or in their place.
- **`--no-model`** carries every sender rule over verbatim. Nothing then
  judges whether a memory's text was the user's instruction or something a
  message said: read the instructions it wrote (the report lists them).
- **An instruction is the person's, on every channel.** A person the
  migration briefed — an existing contact who held a whitelisted address, the
  owner of a whitelisted domain — has each Signal, WhatsApp, Telegram and SMS
  message worked by a model turn from then on, its ring waiting up to
  `CHAT_INSTRUCTION_TIMEOUT`; a mail's ring waits up to
  `MAIL_INSTRUCTION_TIMEOUT` (600 s by default; raise it where filing turns
  run longer). Briefed means the migration set the person's first
  instruction, as its journal recorded at the write (a contact it created
  by the creation time it journaled: one you file by hand after a kill is
  read as it stands). The report names the
  briefed people who hold messenger handles or a phone, and lists apart
  those of them whose instruction the user has edited since (taking it off
  would take those edits too). A person who already carried an instruction
  of the user's is not among them: where a rule was appended to it, the
  report lists them apart again — take such a rule back off by editing the
  instruction, never with `--no-instruction`, which takes the user's own
  words too.
- **What is left for a person:** the scheduled jobs that run `triage-gate.py`
  (to remove) and the prompt jobs that only mention triage (to check — a
  messenger recovery job still belongs), both named in the report; a rule the
  report lists as left alone that should stand (`contacts.py update <id>
  --instruction …`, then `memory.py store --supersedes <memory id> …`); two
  contacts the report says share a name; and taking the instruction off any
  contact the migration briefed whose messages do not need a model turn each
  time (`contacts.py update <id> --no-instruction`, or the Contacts page).
- **Nothing is deleted, and the way back is by hand.** A contact it created
  is deleted on the Contacts page; an instruction is edited there. A
  superseded memory is still in the log (`memory.py recall --tag
  'sender:<address>' --include-superseded`) but cannot be made live again —
  store it anew if it is wanted. The snapshot
  (`.legacy-email-whitelist.json`), the marker and the old status store stay
  in the state directory (`TRIAGE_STATE_DIR`, default `/root/.retinue/triage`).
