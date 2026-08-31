# Ask Ara — the MCP connector

*Reference depth for the "Ask Ara" digest in `CLAUDE.md`. Read this before
enabling, configuring, or debugging the connector, or attaching a second
Retinue instance to the same client.*

Everything Ara knows lives in this container. A Claude session running somewhere
else — a local cowork session, the desktop app — has none of it, so it has to
interrupt the user for facts the user already told Ara. `scripts/ara-mcp-server.py`
closes that gap: an MCP server the outside client attaches to as a remote
connector, so it asks **Ara** first and the user only as a fallback.

It runs **in this container**, forked by the entrypoint on `ARA_MCP_PORT`
(default 8110) exactly like the web gateway — no new image, and it can reach the
chambers and the `claude` CLI natively. It is **opt-in**: nothing starts unless
the deployment sets `ARA_MCP_ENABLED`.

Protocol: MCP over Streamable HTTP, `POST /mcp`, stateless (no session id), plus
`GET /health`. Its `initialize` reply carries an `instructions` field that most
clients inject into their system context — that text is what actually retrains
the client to consult Ara before the user, so treat it as the load-bearing part.

Five tools, all read-only bar the last: `ask_ara(question, context?)` (runs one
`claude -p` here and answers; slow answers hand back a job id for `get_answer`),
`list_projects()` and `get_project(id)` (proxy the gateway's `/projects` and
`/projects/item`, so there is no second copy of the SPARQL), and `tell_ara(note)`
(opens a dashboard thread). The answering session runs with `Write`, `Edit` and
`NotebookEdit` removed and in the CLI's default permission mode, where `claude -p`
auto-denies anything the settings allowlist does not already permit. `Bash` stays
— without it the session cannot query the life store — so the boundary is the
allowlist plus the prompt, **not a sandbox**. It runs on the router tier and
escalates to the frontier tier like any door turn (`docs/model-routing.md`);
`ARA_MCP_MODEL` pins it instead. Every exchange is appended to a per-day
dashboard thread of kind `cowork`, quietly (no unread badge, no Web Push), as an
audit trail the user reads when curious.

**Auth is Traefik's**, as for the dashboard: no credential of its own, because a
client can send only one `Authorization` header and the edge already claims it.
The one thing that matters is that the connector's password is handed to a
third-party client, so it gets its **own htpasswd user, scoped to the MCP host**:

```
TRAEFIK_BASIC_AUTH_USERS=owner:$apr1$...,ara-mcp:$apr1$...
GATEWAY_BASIC_AUTH_SCOPES=ara-mcp:ara.example.com
```

`GATEWAY_BASIC_AUTH_SCOPES` (see `scripts/gateway_auth.py`) confines a named user
to named hosts; on any other router it gets a **403**, not a 401, since
re-prompting for a correct-but-out-of-scope password only loops the browser. A
user **not** named there stays unrestricted, so scoping is opt-in per credential
and no existing deployment changes behaviour. Client certificates are the owner's
own credential and are never scoped. Traefik labels: see
`docker-compose.override.example.yml`.

**More than one instance.** A client may attach several Retinue deployments at
once — a private one and a work one. Nothing collides technically: the client
namespaces every tool by connector name, so both `ask_ara` tools stay distinct.
What collides is meaning. Left at the default, both instances introduce
themselves with the same name and the same claim to "the user's projects", so
the model has nothing to route on and picks one — and the wrong instance answers
plausibly from its own, unrelated data. Two variables fix that, and a
single-instance deployment needs neither:

- **`ARA_MCP_IDENTITY`** — the name this instance answers under (`Ara (work)`).
  It flows into `serverInfo`, the `instructions` text, and every tool
  description. The wire-level server name is slugified from it (`ara-work`).
- **`ARA_MCP_SCOPE_HINT`** — one line on what this instance covers
  (`the company: invoices, the board, staff`). It is stated in the handshake
  *and* handed to the answering session, so an out-of-remit question comes back
  as "not held here, that belongs to …" rather than as a confident wrong answer.
