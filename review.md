# Retinue — Architecture & Design Review

*An honest assessment of the current approach, its strengths, its weaknesses, and
whether it is worth pursuing. Written July 2026, against the state of `main`
(~295 commits since April 2026, ~13.5k lines of Python/JS/shell outside docs).*

---

## Verdict up front

**The approach is worth pursuing.** Retinue's core architectural bets —
credential isolation in sidecar gateways, configuration-fixed trust boundaries,
human-approved outbound sends, and git-as-memory — are genuinely differentiated
and address exactly the failure modes that have plagued the personal-agent
scene (exposed gateways, prompt-injection-to-exfiltration, credentials sitting
in the model's context). No comparable open project combines these properties
today.

But the assessment comes with three serious caveats:

1. **The egress story is oversold to itself.** The audit layer observes; it
   does not enforce. The gap between "we log outbound traffic" and "outbound
   traffic is constrained" is where the whole prompt-injection defense either
   holds or doesn't.
2. **The bespoke, security-critical surface is untested.** The 2,167-line
   hand-rolled web gateway is the public edge and the approval authority for
   outbound sends, and it has zero test coverage and no CI.
3. **The system is coupled to non-contractual internals of Claude Code.** This
   is simultaneously its superpower and its largest strategic risk.

None of these is a reason to abandon the design. All three are addressable, and
the recommendations at the end are ordered by how much risk they retire per
unit of effort.

---

## 1. What the system is

Retinue is a Docker Compose stack (~12 services) that turns Claude Code into a
persistent, multi-channel personal agent system:

- **`retinue`** — the main container. Claude Code runs in remote-control mode
  as the interactive session; every dashboard message and scheduled job spawns
  a fresh `claude -p` subprocess. Ara (the coordinator persona) routes work to
  in-context personas (Secretary, Academic, Publisher) and isolated subagents
  (Archivist, plus chamber-provided ones like Medic, Coach, Ari).
- **Chambers** — mounted git repositories carrying both *data* and *agents*
  (as Claude Code plugins under `.retinue/`). The framework is content-neutral;
  a deployment declares its chambers in `chambers.json`.
- **Messaging gateways** — one compose service per Signal/WhatsApp/Telegram
  account, each owning its credentials (signal-cli, whatsmeow, Telethon) and
  exposing thin token-gated HTTP APIs (`/send`, `/contacts`, `/pending-sends`)
  to the agent.
- **Support services** — a shared Whisper STT service, QLever SPARQL over all
  chamber RDF (`qlever-life`, auto-reindexing), a LiteLLM proxy with
  subscription→OpenRouter failover, an updater sidecar holding the Docker
  socket, and an egress-audit MITM proxy with a log viewer and anomaly agent.
- **A PWA dashboard** served by `scripts/web-gateway.py`: conversation threads
  with attachments both ways, voice input with LLM transcript cleanup, curated
  data cards, and the `/sends` approval queue.

Long-term memory is *files in git* (markdown, RDF triples), queried via grep or
SPARQL. There is no vector store, no database of record beyond the repos.

## 2. What is genuinely good

### 2.1 Credential isolation is best-in-class for this space

The single best decision in the codebase: **the model's context never contains
messaging credentials.** Signal keys, the WhatsApp session, the Telegram MTProto
session, and SMTP/IMAP passwords live in dedicated containers; the agent talks
to thin HTTP APIs. The entrypoint even strips `EMAIL_PASS*` from the agent's
environment and routes `email_client.py` through the gateway
([entrypoint.sh:397-402](scripts/entrypoint.sh#L397-L402)). Compare this with
the common pattern — an MCP server whose token sits in the same process tree as
the model, or worse, credentials pasted into config the model can read — and
this is a categorical improvement. A prompt-injected agent here cannot steal
what it never sees.

### 2.2 Trust boundaries are configuration, not content

The `control` vs `inbox` account modes are a quietly excellent primitive: *what
an account is for is a property of the deployment, never inferred from message
content*. An unconfigured account defaults to `inbox` (cannot drive the
system); control mode additionally requires the sender to be on an explicit
allowlist. This eliminates an entire class of "the message claimed to be an
instruction" confusions by construction rather than by prompt engineering. The
same discipline shows in the send policies: keyed by *sending identity*, with
undeclared accounts failing safe to `verify`, and the invariant that an agent
can never approve its own send. These defaults are consistently fail-closed
(unset `UPDATER_TOKEN` → reject all; no allowlist entry → reject).

### 2.3 Human-in-the-loop where it actually matters

Rather than gating everything (unusable) or nothing (dangerous), the system
gates the one action class that is both irreversible and outward-facing:
messages leaving as the user's identity. The `verify`/`trust`/`allow` triage,
the `/sends` approval page spanning all channels, and the distinction between
a dedicated agent number (`allow`) and the user's own number (`verify`) show
real threat-model thinking, not checkbox security.

### 2.4 Data sovereignty and auditability

Everything the system knows and does is a file in a git repository the user
owns. Observations, therapy notes, contacts, even agent definitions — all
diffable, revertable, greppable, backed up by `git push`. The three-tier branch
policy maps write risk to process (operational output → direct, clinical
content → consent, system changes → PR). This is a real answer to "what is the
agent doing to my data" that no database-backed competitor gives.

### 2.5 Leverage: a small codebase for a large feature surface

~13.5k lines buy: multi-channel messaging with voice in/out, a PWA dashboard
with threaded conversations and attachments, scheduling, data refresh, plugin
hot-sync, SPARQL over life data, model failover, egress auditing, and
self-update. That economy comes from standing on Claude Code (tools,
permissions, sessions, plugins, subagents come free) and from disciplined
scoping — e.g. exactly one Whisper model in the whole stack, one gateway
service per account instead of a multiplexing monolith.

### 2.6 Documentation as institutional memory

The compose file and README are among the best-commented infrastructure files
I have reviewed. Comments explain *why*, cite past regressions ("Regressed once
in da6c1d8"), and record dependency-direction reasoning (`UPDATE_COMMAND`
flowing deployment → framework). CLAUDE.md doubles as an operations manual.
This materially lowers the bus factor for a one-person project.

## 3. Where it is weak

### 3.1 The prompt-injection defense has a hole in the middle

The perimeter is strong; the interior is soft. The main session and every
triage/scheduler job run with `Bash(*)`, `Write(**)`, `Edit(**)` allowed
([.claude/settings.json](.claude/settings.json)) while processing **untrusted
input** — inbound email, WhatsApp, Signal, Telegram. A hostile message cannot
steal credentials (2.1) and cannot silently send messages (2.3), but it can
still induce the agent to:

- **read everything** — there is no compartmentalization between chambers
  inside a session. Triage of a stranger's email runs with the health chamber,
  the operations chamber, and every contact list readable;
- **corrupt memory** — write and *push* poisoned content into chambers
  (Tier 1 paths are explicitly push-without-review), poisoning future sessions
  that read those files;
- **exfiltrate over allowed channels** — any `allow`-policy account, any
  HTTP fetch (logged by the egress audit, but logging is post-hoc), or a git
  push to a remote;
- **trigger the updater** — `UPDATER_TOKEN` sits in the agent's own
  environment, so "rebuild the stack from whatever `git pull` brings" is one
  `curl` away. The recipe is operator-fixed, but pull-and-rebuild on attacker
  demand is still a lever.

None of this makes the design wrong — every agentic system with real
capabilities has this tension — but the *asymmetry* should be acknowledged:
enormous care went into the outbound-send boundary while the session itself
runs close to unrestricted. The cheapest wins: run triage/inbox processing in a
subagent with a reduced tool set and only the chambers it needs; keep
`UPDATER_TOKEN` out of the default agent environment; consider read-only
mounts for chambers a given job has no business writing.

### 3.2 Egress audit is observability, not enforcement

The MITM sidecar works via `HTTP_PROXY`/`HTTPS_PROXY` environment variables
([docker-compose.yml:114-119](docker-compose.yml#L114-L119)). Environment
variables are advisory: any process — including a bash line the agent writes —
can unset them, use a raw socket, or speak a non-HTTP protocol, and the
`agents` bridge network has ordinary outbound internet access. The anomaly
agent then never sees the flow at all. As built, the layer is valuable
*telemetry* (and the log viewer + anomaly agent are a genuinely rare feature),
but the README/compose language ("route all outbound HTTP/HTTPS") reads
stronger than what is enforced.

Fix is structural, not heroic: put `retinue` on an `internal: true` network
whose only route out is the proxy (plus explicit internal services), or add an
iptables/nftables egress rule in the container. Then the audit log becomes a
complete record instead of a record of the polite traffic.

### 3.3 The security-critical surface is hand-rolled and untested

- `web-gateway.py` is **2,167 lines** in one file: public edge auth
  (forward-auth for Traefik), the `/sends` approval authority, attachment
  storage/serving, conversation state with hand-managed threading locks,
  transcript cleanup, an email backend proxy, SPARQL dashboards. It is built
  directly on `BaseHTTPRequestHandler` with string-assembled HTML.
- Test coverage: **four test files (~730 lines)**, all on send-policy and
  contact-lookup logic. The web gateway, the entrypoint's credential dance, the
  scheduler, refresh, and the conversation/attachment layer have none.
- CI: the only workflow checks the pinned signal-cli version. **No workflow
  runs the tests.**

Hand-implementing `$apr1$` verification in constant time
([gateway_auth.py](scripts/gateway_auth.py)) is admirably dependency-free, but
the combination *hand-rolled HTTP + hand-rolled auth + hand-rolled HTML + no
tests + public exposure* concentrates classic vulnerability classes (path
traversal in static/attachment serving, CSRF on approval actions, header
injection) in exactly the component an attacker reaches first. Either this
component earns a test suite and a security pass, or it should shrink onto a
boring, hardened base (a minimal framework, templates with autoescaping, and a
CSRF token on `/sends` actions).

### 3.4 Coupling to Claude Code's non-contractual behavior

The entrypoint contains a 40-line background watcher that detects OAuth token
rotation by polling `.credentials.json`, restores from backup, and **kills
PID 1 to force a container restart**
([entrypoint.sh:313-372](scripts/entrypoint.sh#L313-L372)) — with a marker-file
protocol to avoid infinite restart loops. There is a plugin-cache
drift-detection daemon (`sync-plugins.py --watch`) because `claude plugin
install` is a no-op on identical versions. There is a retry loop for the
ENOENT window while Claude Code's auto-updater swaps its own symlink. Each of
these is a clever, well-documented workaround for behavior Anthropic never
promised to keep stable — which means each is a time bomb with an unknown
timer. The LiteLLM subscription-failover path adds terms-of-service gray area
on top.

Strategically this is the project's deepest dependency: Claude Code provides
perhaps 80% of the feature surface for free, and in exchange the project
inherits a moving, proprietary foundation it can only observe, not pin.
Mitigations worth pursuing: pin the Claude Code version in the image and
upgrade deliberately (the auto-updater workaround suggests it currently
floats); migrate the programmatic paths (`claude -p` in web-gateway/scheduler)
to the Agent SDK's structured interfaces, which carry at least an implicit
compatibility promise; and keep the gateway/dashboard layer runtime-agnostic so
that a different harness could in principle sit behind `/message`.

### 3.5 Triplicated gateways

`signal-gateway.py` (1,350), `whatsapp-gateway.py` (1,081), and
`telegram-gateway.py` (993) reimplement the same machinery — pending-send
store, policy evaluation, recent-chats persistence, token auth, push HTTP
server — three times, confirmed by the three parallel policy test files. Every
bugfix and every policy semantic change must land three times or the channels
drift. The transport bindings genuinely differ; the send-control core does not.
Extract a shared module (the tests can largely be unified with it).

### 3.6 The session model is expensive and slow by construction

Every dashboard message, scheduled job, and control-channel prompt is a fresh
`claude -p` that loads CLAUDE.md, plugins, and skills before doing anything.
The codebase itself documents the symptoms: 600-second gateway timeouts,
localized "this is taking a while" notice messages, replies appended only
after the session ends (no streaming), and a subscription-with-failover proxy
built to blunt the cost. This is an acceptable trade for correctness (fresh
sessions always see current plugins/data) but it caps responsiveness — a
control-channel Signal exchange competes with chat apps whose baseline is two
seconds, not two minutes. Worth exploring: a warm resumable session per
channel (the `--resume` machinery already exists in `send_message`), and
trimming what a scheduled job must load to answer.

### 3.7 The RDF/SPARQL layer has unproven ROI

Two QLever services, a submodule, blue-green reindexing, and agents
hand-authoring N-Triples currently power: one dashboard card and archivist
ingestion. The semantic-web design is intellectually coherent (named graphs
per file, chambers indexed uniformly) and may pay off if cross-domain queries
become load-bearing ("what did I eat the week my sleep score dropped"). But
today it is the stack's heaviest infrastructure per delivered feature, and
LLM-written triples are a quietly error-prone ingestion path with no schema
validation. Recommendation: keep it, but define within a quarter the queries
that justify it — or demote it to an optional deployment service.

### 3.8 Operational fragility for a system of record

- No backup story for the named volumes that are *not* git: conversations,
  scheduler state, and above all the messaging-account keys (losing
  `signal-data` means re-linking every account).
- ~30 environment variables, a manual CA ceremony for client certs, per-account
  volume discipline, Traefik file-provider wiring — all documented, none
  automated. Fine for the author; a wall for a second user.
- Single host, single container of authority, everything as root inside
  containers; the updater's docker socket is root-equivalent on the host
  (acknowledged in comments, but worth restating as the blast radius of any
  updater bug).

## 4. Is it worth pursuing?

**Yes — on its merits, not out of sunk cost.** The honest comparison point is
the current landscape: hosted assistants have no meaningful data sovereignty
and shallow channel access; the popular open personal-agent projects
(OpenClaw et al.) have the channel access but have repeatedly demonstrated the
failure modes Retinue's architecture was built to prevent. Retinue's
combination — capable-model harness, credentials outside the context,
identity-keyed human-approved sends, config-fixed trust boundaries, git-owned
memory — is a defensible and, so far, uncrowded position.

The main risks to that position are not conceptual but executional:

- if the egress boundary stays advisory, the security story is a story;
- if the web gateway stays a 2.2k-line untested monolith, the strongest
  criticism of the neighbors applies here too;
- if Claude Code shifts under it, months of workaround shims need rework on
  someone else's schedule.

And one strategic decision is being deferred by the framework/deployment
split's very tidiness: **is this a personal system or a product?** The
engineering (content-neutral framework, example chambers, deployment
overrides) says product; the onboarding cost, single-user assumptions, and
absent CI say personal tool. Both are legitimate. But the next hundred hours
are spent very differently depending on the answer — a product needs the
setup wall torn down and the bespoke surface hardened; a personal system
should stop paying generality tax (e.g. 3.5, 3.7) it never collects on.

## 5. Recommendations, in order

| # | Action | Effort | Risk retired |
|---|--------|--------|--------------|
| 1 | Enforce egress at the network layer (internal-only network; proxy as sole route out) | S | Turns the flagship security feature from telemetry into a boundary |
| 2 | CI that runs the existing tests on every PR | XS | Stops silent regression of the one tested area (send policies) |
| 3 | CSRF token + method discipline on `/sends`; path-traversal tests for static/attachment serving | S | Hardens the approval authority — the linchpin of the send-control model |
| 4 | Run inbox/triage processing in a reduced-privilege subagent (no `Bash(*)`, scoped chamber access); remove `UPDATER_TOKEN` from the default agent env | M | Shrinks the injection blast radius where untrusted content is processed |
| 5 | Extract the shared gateway core (pending sends, policy, recent-chats, auth) into one module with one test suite | M | Ends the ×3 maintenance and drift |
| 6 | Split `web-gateway.py` (edge/auth, conversations, sends, dashboard-data) and add tests per piece | M–L | Makes the public edge reviewable |
| 7 | Pin the Claude Code version; move programmatic invocations to the Agent SDK | M | Converts undocumented coupling into supported interfaces |
| 8 | Volume backup/restore runbook (messaging keys, conversations, scheduler state) | S | System-of-record credibility |
| 9 | Decide product vs. personal; if product, invest in a guided setup path; if personal, deliberately de-scope generality | — | Focus |
| 10 | Set a concrete bar for the SPARQL layer (queries it must answer by a date) or make it optional | S | Stops paying for unproven infrastructure |

*S/M/L = small/medium/large relative to this codebase's usual change size.*

---

### Bottom line

Retinue is an unusually thoughtful system with a real architectural thesis:
*capability without credential custody, autonomy without send authority,
memory without a database you don't own.* The thesis holds. The gap is between
designed security and enforced security, and between one careful author and
code that can survive other hands. Close those, and this is not just worth
pursuing — it is ahead of the field it is about to be compared against.
