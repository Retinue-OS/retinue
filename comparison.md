# Choosing a Personal Agent System: Retinue vs. OpenClaw vs. Hermes Agent

*A comparison for someone deciding how to set up a self-hosted personal AI
agent — an assistant that reads your messages, answers on your channels,
remembers your life, and runs scheduled tasks. Written July 2026. External
facts (stars, CVEs, pricing) are as reported in public sources at that date
and move fast; verify before relying on them.*

The three systems compared:

| | **Retinue** | **OpenClaw** | **Hermes Agent** |
|---|---|---|---|
| Origin | Reto Gmür's personal-agent framework (this repo), Apr 2026– | Peter Steinberger; now OpenClaw Foundation (501(c)(3), Jul 2026) | Nous Research, Feb 2026 |
| Language / runtime | Python glue around **Claude Code** as the agent runtime; Docker Compose stack | TypeScript/Node daemon embedding the minimal **Pi** runtime | Python framework, own runtime |
| License | Open source (repo), effectively a framework + your deployment repo | MIT | MIT |
| Community size | One author; no releases, no installer | ~383k GitHub stars, 80k forks, foundation with paid staff | ~217k stars, 1,859 contributors, v0.18.x |
| Model backends | Anthropic (Claude Code login/API), any Claude-compatible gateway (Ollama, OpenRouter), LiteLLM subscription→OpenRouter failover | Anthropic, OpenAI (incl. subscription OAuth reuse), Copilot, OpenRouter, Ollama/LM Studio, fallback chains | Model-agnostic: Nous Portal, OpenRouter, OpenAI, any endpoint; pairs naturally with open-weight Hermes models |
| Channels | Signal, WhatsApp, Telegram, e-mail (IMAP/SMTP), PWA dashboard, voice notes both ways | ~29 channels incl. WhatsApp, Telegram, Signal, iMessage, Discord, Slack, Teams, Matrix, IRC | 20+ channels incl. Telegram, Discord, Slack, WhatsApp, Signal, Matrix, SMS, Home Assistant; e-mail via IMAP/SMTP |
| Memory | Git repositories (markdown + RDF triples), SPARQL queries over life data | Markdown files (`MEMORY.md` + daily notes), optional vector search | Agent-curated memory, SQLite FTS5 recall, Honcho user modeling |
| Scheduling | Per-chamber `.schedule.json` + refresh dispatchers; each job a fresh session | Cron + heartbeat mechanism | Cron + heartbeat/daily-journal/weekly-review triggers |
| Outbound message control | **Per-send human approval queue** (`/sends`), policy keyed by sending identity, fail-closed | Tool/exec approval gates; **no per-message approval queue** — sends go out as your real accounts | Approval checks exist but were found disabled in default container config (audit, Apr–May 2026) |
| Credential placement | **Isolated per-channel gateway containers**; the model's context never holds keys | In the daemon's config/process on your machine (`~/.openclaw/`) | In the framework's config on your machine |
| Setup effort | Highest: Docker Compose, ~30 env vars, Traefik, CA ceremonies, account linking per channel | `npm install -g openclaw` + onboarding wizard; hardening is on you | Install + config; runs on "a $5 VPS," Termux, serverless; six execution backends |
| Cost | Your Claude subscription/API spend + a small always-on server | Free OSS + your API keys or subscription reuse + an always-on machine | Free OSS + your model spend (Nous Portal $0–200/mo, or open weights on your own GPU) |

---

## The short version

- **OpenClaw** is the mainstream choice: the biggest community, the most
  channels, the fastest setup, the most integrations — and the most scar
  tissue. It became "2026's first major AI security crisis" (40k exposed
  gateways found by SecurityScorecard, a one-click RCE CVE, 341 malicious
  skills uploaded to its marketplace in three days). It has since added real
  mitigations and a security-audit command, but its defaults history means
  *you* are the security team.
- **Hermes Agent** is the challenger with the most interesting autonomy story
  (self-improving skills, agent-curated memory, heartbeat-driven behavior) and
  true model freedom — including fully local open weights, which neither
  competitor matches as cleanly. Its security posture is younger and its
  flagship feature (agent-written skills that persist) is also its documented
  main attack vector ("skill poisoning").
- **Retinue** is not a product in the same sense — it is one person's
  framework with no installer, no releases, and no community. What it offers
  instead is an *architecture* the other two lack: messaging credentials
  locked in sidecar containers the model can't read, every outbound message
  gated by an identity-keyed policy with a human approval queue, trust
  boundaries fixed by configuration rather than message content, and all data
  in git repos you own. If those properties matter more to you than
  ecosystem, and you can operate a Docker Compose stack, it is the most
  security-conscious design of the three. If they don't, its setup cost buys
  you little.

---

## 1. What you're actually choosing between

These are three different *shapes* of system, not three brands of the same
thing.

**OpenClaw is an assistant appliance.** One Node daemon (the Gateway) owns
sessions, channels, and routing; the agent core is deliberately tiny (Pi:
four tools, ~1,000-token system prompt). You install it, link WhatsApp, and
have a working assistant in an afternoon. Everything — memory, config,
credentials — lives as flat files under `~/.openclaw/` on the machine running
it. Its enormous skill marketplace (ClawHub) and node apps (iOS/Android/macOS
device control, Canvas live UI) reflect a project optimizing for *capability
breadth*.

**Hermes Agent is an autonomy framework.** Nous built it around the idea of
an agent that "grows with you": it writes its own skills, curates its own
memory, models the user (Honcho), and wakes itself with heartbeats and
journal/review routines. It is aggressively model-agnostic — the natural
companion to Nous's open-weight Hermes 4 / 4.3 models (Apache 2.0 at
14B/36B), which makes it the only path here to a *fully local, no-API-key*
stack if you own a GPU. Execution backends range from local shell to Docker,
SSH, Modal, and Daytona.

**Retinue is a security-partitioned deployment.** It doesn't have its own
agent runtime at all — it orchestrates Claude Code, inheriting its tools,
permissions, plugins, and subagents, and spends its own ~13k lines on the
things a runtime doesn't give you: per-account gateway containers that own
the Signal/WhatsApp/Telegram/e-mail credentials, an approval pipeline for
outbound sends, a scheduler, an egress-audit proxy, a PWA dashboard, and a
convention ("chambers") for keeping every domain's data and agents in its own
git repository. It is single-user by design and assumes you run a small
server with Traefik in front.

## 2. Setup and operations

**OpenClaw** wins decisively on time-to-first-conversation:
`npm install -g openclaw@latest && openclaw onboard --install-daemon` on a
Mac mini, VPS, or Raspberry Pi. The flip side: a maintainer has publicly
warned it is "far too dangerous" for non-CLI users, and safe operation
(loopback binding, trusted proxies, pairing codes, tool deny-lists, sandbox
profiles) is your reading assignment. Docker is supported but optional, and
much of the ecosystem assumes host access (iMessage needs macOS).

**Hermes Agent** is nearly as quick and much lighter than Retinue: one
framework, one config, runs on tiny hardware. Its April–May 2026 independent
audit (4 Critical / 9 High findings in the default configuration —
unrestricted shell, approval checks disabled in containers) means the same
caveat applies: defaults are not the safe configuration.

**Retinue** is the heaviest by an order of magnitude: a 12-service Compose
stack, ~30 environment variables, per-channel account linking (signal-cli
registration or device-link QR, WhatsApp QR, Telegram MTProto login), a
Traefik edge with basic-auth or client certificates, and an egress CA. There
is no onboarding wizard; the README is good but long. In exchange, day-2
operations are unusually disciplined: data updates are git commits, the stack
self-updates via a token-gated sidecar, plugins hot-sync from edited repos,
and the SPARQL index rebuilds itself. Realistic audience today: people who
run their own infrastructure happily — or who are the author.

## 3. Channels and daily interaction

All three cover the big messengers. Differences that matter in practice:

- **Acting as you vs. talking to you.** OpenClaw and Retinue both support
  sending *as your own accounts* (WhatsApp linked device, Telegram user
  client, Signal linked device). OpenClaw treats your own number as
  implicitly trusted and sends directly; Retinue's whole design revolves
  around *not* letting sends from your identity happen without approval
  (below). Hermes covers similar channels via its gateway with a more
  bot-centric flavor.
- **Breadth**: OpenClaw's ~29 channels (including iMessage, Discord, Slack,
  Teams, IRC, WeChat) dwarf the others. If your life runs through Discord or
  iMessage, that decides it.
- **Voice**: Retinue is strongest here for messaging — inbound voice notes
  are transcribed (shared Whisper service) and replies come back as spoken
  audio (Piper) on Signal; the dashboard takes dictation with LLM transcript
  cleanup. OpenClaw has "Talk" voice mode and device nodes; Hermes has TTS
  via the Nous Portal tool gateway.
- **A curated home screen**: Retinue's installable PWA dashboard (threaded
  conversations with attachments both directions, approval queue, data
  cards) is a differentiator if you want a *place* rather than only chats.
  OpenClaw's Canvas/Web UI is more of a live surface the agent draws on.

## 4. Memory and data ownership

- **Retinue**: everything is a git repository you own — markdown, RDF
  triples, contact files. History, diffs, and rollback come free; a SPARQL
  endpoint queries across domains. Cost: agents must write well-formed
  triples, and the graph machinery is heavy for what it currently delivers.
- **OpenClaw**: plain Markdown (`MEMORY.md`, daily notes) — transparent,
  greppable, trivially portable, no history unless you git it yourself.
- **Hermes**: the most *automated* memory (agent-curated, FTS5 search, user
  modeling) and therefore the least inspectable — you are trusting the
  agent's own curation loop, which the audit flagged as a persistence vector.

If "my data outlives my agent stack" is a requirement, Retinue's git-native
model is the strongest answer of the three; OpenClaw's flat files are a close,
simpler second; Hermes's curated store is the most capable and the most
opaque.

## 5. Security — where the three philosophies actually diverge

This is the dimension where the products differ most, and where marketing
and reality diverge most, so it deserves the detail.

**OpenClaw's record:** SecurityScorecard found **40,214 internet-exposed
instances** (~63% assessed vulnerable), largely from reverse-proxy
misconfigurations that made external traffic look like trusted localhost;
CVE-2026-25253 (CVSS 8.8) allowed one-click RCE via the Control UI leaking
the gateway token; the "ClawHavoc" campaign planted 341 malicious skills on
ClawHub in three days. The project responded seriously — fail-closed gateway
auth, loopback defaults, DM pairing codes, exec approvals, sandbox profiles,
`openclaw security audit --fix` — and its current documentation is frank that
prompt injection is unsolved. But the design keeps credentials and the agent
in one trust domain on your machine, and outbound messages from your real
accounts have **no built-in per-message human approval**. One misconfigured
proxy or one malicious skill is a bad afternoon.

**Hermes Agent's record:** younger, with the same lessons arriving on
schedule: 3 public CVEs (path traversal/symlink, WebUI), an audit finding
approval checks disabled in default containers, and open issues on "skill
poisoning" — the self-improvement loop doubling as an attacker's persistence
mechanism — and on prompt injection in tool outputs. Self-hosting is the
privacy pitch; hardening (Docker sandboxing, no YOLO mode, isolated VMs) is
on the user.

**Retinue's approach** is architectural rather than reactive, three layers:

1. *The model never holds credentials.* Signal/WhatsApp/Telegram keys and
   mail passwords live in dedicated containers exposing thin token-gated
   APIs; the entrypoint strips mail credentials from the agent's environment.
   A prompt-injected session cannot leak keys it cannot read.
2. *Outbound sends are governed per identity, fail-closed.* Every channel has
   an `allow`/`trust`/`verify` policy keyed by the *sending* account; an
   undeclared account defaults to `verify` (human approval on the `/sends`
   page); an agent can never approve its own send. Neither competitor has an
   equivalent for message content.
3. *Trust is configuration, not inference.* An account is a control channel
   or an inbox by deployment config, never by what a message claims; control
   channels additionally require an explicit sender allowlist. Plus an
   egress-audit MITM proxy with an anomaly watcher — rare in this space.

Honest caveats (argued at length in [review.md](review.md)): the egress audit
is advisory rather than network-enforced; the agent session itself still runs
with broad shell access over untrusted inbound content; and the hand-rolled
web gateway is untested. Retinue has had no public security incident — but it
also has had no public exposure. Its advantage is *design posture*, not a
proven track record; the other two's disadvantage is a proven track record.

A structural note that applies to all three: any system that reads untrusted
messages with a capable model and any outbound path is prompt-injectable.
The differentiator is blast radius — and Retinue's partitioning gives it the
smallest one on paper, OpenClaw the largest install base for attackers to
scan for, and Hermes the most novel persistence surface (self-written
skills).

## 6. Models, cost, and lock-in

- **Retinue** is Claude Code-shaped. That buys a best-in-class harness
  (tools, permissions, subagents, plugins, phone remote-control) and costs
  lock-in to Anthropic's proprietary, fast-moving CLI — softened by support
  for Claude-compatible gateways (Ollama, OpenRouter via LiteLLM), though the
  remote-control session then degrades. Expect a Claude Pro/Max subscription
  or API spend, plus a small server.
- **OpenClaw** is model-flexible with clever subscription reuse (Claude CLI
  or ChatGPT OAuth), fallback chains, and local models via Ollama — but its
  ecosystem gravity assumes frontier hosted models.
- **Hermes Agent** has the cleanest open-model story: Apache-2.0 Hermes
  weights (14B fits a consumer GPU; 4.3-36B in ~24–48 GB) mean a genuinely
  sovereign, offline-capable stack — the only one of the three where no
  outside company needs to know your agent exists. Hosted convenience via
  Nous Portal ($20–200/mo tiers) if you want it.

## 7. Community and longevity risk

- **OpenClaw**: fastest-growing repo in GitHub history, now under a funded
  nonprofit foundation with 24+ core maintainers across major vendors.
  Longevity risk low; churn risk (CalVer release train, rapid change) real.
- **Hermes**: 1,859 contributors and a well-funded company (raising at a
  reported $1.5B valuation) behind it, but the company's center of gravity is
  models and its crypto-adjacent training network gives some evaluators
  pause. Risk moderate.
- **Retinue**: bus factor of one, no releases, no external users. As a
  *product choice* this is the dominant risk: you are not adopting a product,
  you are adopting a codebase you must be able to maintain yourself. (As a
  source of ideas — gateway isolation, send approval, config-fixed trust — it
  is valuable even if you deploy something else.)

## 8. Decision guide

**Choose OpenClaw if…** you want the most capable assistant this week, your
channels include Discord/iMessage/Slack, you value a huge skill ecosystem,
and you are willing to do (and keep doing) the hardening work yourself —
loopback binding, pairing, sandboxes, and never exposing the gateway.

**Choose Hermes Agent if…** model sovereignty is the point: you want open
weights, possibly fully local inference, an agent that develops its own
skills and memory, and you accept a younger project where the boldest
features are also the sharpest edges (disable self-written skill trust,
sandbox execution).

**Choose Retinue if…** you are a self-hosting engineer whose non-negotiables
are: credentials the model can never see, no message leaving as you without
your click, and a life record kept as git repositories — and you accept a
single-maintainer framework, Claude-centric operation, and a weekend (or
three) of setup. Today that is a narrow audience; it matches the system's
actual origin as a personal deployment.

**Consider none of the above if…** you would not run a server at all. A
hosted assistant (Claude, ChatGPT with connectors) with per-integration OAuth
is genuinely safer for a non-operator than a mis-hardened self-hosted agent —
the OpenClaw exposure scans are 40,214 data points for that claim.

---

## Sources

External claims draw on public reporting as of mid-July 2026, including:
[openclaw.ai](https://openclaw.ai) and [docs.openclaw.ai](https://docs.openclaw.ai),
the [OpenClaw Wikipedia article](https://en.wikipedia.org/wiki/OpenClaw),
SecurityScorecard's exposed-instance research, The Hacker News on
CVE-2026-25253, [github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
and its docs, the CSA research note and Repello threat model on Hermes Agent
CVEs, [portal.nousresearch.com](https://portal.nousresearch.com), Hugging Face
model cards for Hermes 4 / 4.3, and TechCrunch coverage of both projects.
Retinue facts come from this repository at the stated commit range.
