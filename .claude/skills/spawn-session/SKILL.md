---
name: spawn-session
description: Start (or reattach to) a remote-control Claude session with a given name, visible in the mobile/web Claude app. Use when the user asks to "kreiere eine neue Sub-Session <Name>", "starte Subsession <Name>", "neue Claude-Instanz <Name>", or similar. Takes a name as argument.
---

# spawn-session

Starts a named Claude session with remote control enabled in the **main session's working directory** (`/workspace`) so it appears in the mobile or web Claude app. If a session with the same name is already running, do not spawn a duplicate — reattach instead.

## How to invoke

The user typically says something like:
- "Kreiere eine neue Sub-Session Aspirin"
- "Starte Subsession Aspirin"
- "Spawn a new claude instance called Foo"

Extract the name. If they did not give one, ask.

## Step 1 — Check whether a session with that name is already running

```bash
NAME="<the name>"
EXISTING_PID=$(pgrep -f "\-\-remote-control.*${NAME}" | head -1)
```

If `EXISTING_PID` is non-empty:
- The session is still alive. Report to the user: session already running, it should be visible in the mobile/web app. **Do not spawn another instance.**

If `EXISTING_PID` is empty: proceed to step 2.

## Step 2 — Spawn a fresh session in /workspace

```bash
WORKDIR="/workspace"                              # main session cwd — shared memory + CLAUDE.md
DEBUG_LOG="/tmp/session-${NAME}-debug.log"

cd "$WORKDIR" && setsid env -u ANTHROPIC_API_KEY script -q -c "claude --remote-control '${NAME}' --name '${NAME}' --permission-mode dontAsk --debug-file '${DEBUG_LOG}' --verbose" /dev/null >/dev/null 2>&1 &
disown
```

> **Why `env -u ANTHROPIC_API_KEY`?** When the API key is set, the CLI uses API-key mode which connects but does not register it with the user's account on claude.ai. Unsetting it forces OAuth authentication (stored in `/root/.claude/`) so the session appears in the mobile/desktop app.

Wait ~6 seconds, then verify:

```bash
sleep 6
ps -ef | grep -E "\-\-remote-control.*${NAME}" | grep -v grep
grep -E "Connected|server title" ${DEBUG_LOG} | tail -3
```

Report to the user:
- Session running, name `${NAME}`
- It should now appear in the mobile/web Claude app sidebar

## Why these choices matter

- **cwd = `/workspace`** (not `/workspace/chambers/health`): so the sub-session shares the same `CLAUDE.md`, agents directory (`/workspace/agents/`), and auto-memory path (`/root/.claude/projects/-workspace/memory/`) as the main session. Starting in `/workspace/chambers/health` creates a parallel memory namespace — fine for isolated experiments, bad for continuity.
- **`claude --remote-control '${NAME}'`**: uses the flag form (not the subcommand) to start an interactive session with remote control enabled. This creates an immediately visible session in the Claude app — no URL extraction needed.
- **`--name '${NAME}'`**: sets the display name shown in the app sidebar.
- **`--debug-file ... --verbose`**: structured logs you can grep; without them, only the TUI is observable.
- **`script -q -c "..." /dev/null`**: allocates a pseudo-TTY so the Claude TUI can start. Without a PTY the process detects no terminal and exits immediately after startup. `script` from util-linux provides the PTY; its typescript output goes to `/dev/null`.
- **`setsid … &` + `disown`**: fully detach so the process survives this Bash invocation.
- **`env -u ANTHROPIC_API_KEY`**: when set, the CLI uses API-key auth which connects but does not register it with the user's claude.ai account — so the session never appears in the mobile/desktop app. Unsetting forces OAuth.
- **`--permission-mode dontAsk`**: subsessions run autonomously in the background — permission prompts block the session until answered, which causes hangs in remote-control mode. `dontAsk` silently enforces the `settings.json` allowlist without interrupting the user. The security boundary is the allowlist, not the permission-mode. (`bypassPermissions` was tested but exits silently during startup in remote-control mode.)

## On "resume by name"

There is no server-side name uniqueness. Across restarts there is no native way to pick up where a previous session left off. What this skill provides instead:

- **Within a single container lifetime**: if the process is still alive, reattach to it (step 1). The app shows the full conversation history.
- **Across container restarts / killed sessions**: a fresh session is created. Conversational continuity comes via the **shared filesystem** — auto-memory, observations, journal, therapy notes — which every session in `/workspace` reads. That is the intended continuity mechanism.

If the user explicitly wants conversation-level history continuity that survives restarts, that is not currently supported by the CLI and should be flagged.

## Killing a named session

```bash
pkill -f "\-\-remote-control.*${NAME}"
```

## Notes

- All sessions in `/workspace` share the same Anthropic auth (via the user's logged-in account), the same SPARQL endpoints (the life store at `http://qlever-life:7001` plus any deployment-defined stores advertised via `SPARQL_ENDPOINT_*` env vars), and the same filesystem.
- They will pick up `/workspace/CLAUDE.md` (Ara role + branch policy) on their own start.
- PID 1 in this container is the original session; this skill spawns additional siblings.
