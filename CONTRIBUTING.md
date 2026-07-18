# Contributing to Retinue

Thanks for looking. This is an early, small project — which means your
contribution has unusually high leverage, and also that the setup cost is real.
We'd rather tell you that up front than have you discover it at step nine.

## Before you invest much time

Read [`review.md`](review.md). It is a candid architecture review of this
codebase, including the parts that are weak, and it will tell you faster than
anything else whether this project is worth your time. If you're deciding what
to work on, its recommendations table is effectively the roadmap.

Then read [`docs/triple-stores.md`](docs/triple-stores.md) if you want to
understand the part of the design that is genuinely unusual.

## Good first contributions

The highest-value work right now is mostly *hardening*, not features:

- **Tests for untested surfaces.** The web gateway, the entrypoint's credential
  handling, the scheduler, and the conversation/attachment layer have none.
  Path-traversal tests for attachment and static serving are especially wanted.
- **Extracting the shared gateway core.** `signal-gateway.py`,
  `whatsapp-gateway.py` and `telegram-gateway.py` reimplement the same
  pending-send store, policy evaluation and token auth three times. Every fix
  currently has to land three times.
- **Making the egress boundary real** — an `internal: true` network with the
  proxy as sole route out, replacing the advisory `HTTP_PROXY` approach.
- **Splitting `web-gateway.py`.** It is a large single file that is also the
  public edge and the send-approval authority.
- **Documentation and setup friction.** If you got stuck, that's a bug; a PR
  fixing the docs where you got stuck is genuinely welcome.

## Development

There is no build step. The stack is Docker Compose; the agent logic is
Markdown and Python.

```bash
git clone --recurse-submodules https://github.com/retinue-os/retinue.git
cd retinue
cp .env.example .env      # then fill it in — see README.md
docker compose up -d
```

Run the tests before opening a PR. They're standalone scripts with no
dependencies to install:

```bash
for t in tests/test_*.py; do python3 "$t" || echo "FAILED: $t"; done
```

Keep them that way — no pytest, no fixtures requiring the full runtime. A test
that needs signal-cli installed is a test that won't run in CI.

## Conventions

**All non-user-facing natural language is English** — code comments, commit
messages, issue and PR titles and bodies, and the mechanical parts of skill and
agent documentation. User-facing content and agent persona/voice definitions
follow their own language rules.

**Comments explain *why*, not *what*.** The existing infrastructure files are
heavily commented with the reasoning behind decisions and references to past
regressions. Match that. If you remove a guard, check whether a comment explains
why it's there first.

**Match the surrounding code.** Same naming, same idiom, same comment density.

## Change tiers

Not everything needs the same process:

| Tier | What | Process |
|---|---|---|
| 1 | Operational output — data, generated content | Direct to `main` |
| 2 | Content changes the maintainer asked for in-session | Direct to `main` |
| 3 | Anything that changes how the system works — `scripts/`, `Dockerfile`, `docker-compose.yml`, `CLAUDE.md`, `agents/`, `.claude/`, `webapp/` | Feature branch + PR |

**External contributions are always Tier 3.** Branch, PR, and expect review.

## Security

Do not open a public issue for a vulnerability. See [`SECURITY.md`](SECURITY.md)
— and note the "known limitations" section, which lists documented design gaps
that are not news.

## A note on agents

Parts of this project are written by AI agents, including its own. Commits
carry co-author trailers where that's the case. Contributions from agents are
welcome on exactly the same terms as from humans: they go through PR review, and
a human is accountable for them.

The project's own advocate agent, Aros, may show up in issues and discussions.
He identifies himself as an agent, he does not have commit rights, and he
escalates anything requiring a decision to the maintainer. If he ever behaves
otherwise, that's a bug — please report it.
