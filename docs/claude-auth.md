# Claude sign-in: why it ends, and how it is renewed from the browser

Every Claude Code process in a Retinue deployment — the remote-control
session, each scheduled `claude -p` job, every dashboard conversation turn —
authenticates through one shared OAuth credential file,
`/root/.claude/.credentials.json`. When that sign-in dies, the whole system
goes quiet at once, and historically the only fix was an SSH session to the
host: stop the stack, run an interactive container, run `claude`, log in,
restart. This document covers what actually kills the sign-in, the monitoring
that now announces it (ideally *before* it happens), and the browser re-login
that replaces the console procedure.

## Why sign-ins end

Two distinct mechanisms:

1. **The refresh token has a fixed lifetime.** The credential file records it
   (`claudeAiOauth.refreshTokenExpiresAt`). Access tokens are refreshed
   automatically for as long as the refresh token lives; when *it* expires, no
   process can refresh anything and only a fresh interactive sign-in helps.
   This is the predictable "every couple of weeks it logs out" experience —
   predictable enough to warn about days in advance, which is exactly what the
   monitor does.

2. **Concurrent sessions rotate each other out.** Anthropic rotates the token
   pair on refresh, and every Claude process refreshes on its own once the
   access token is within five minutes of expiry. The framework runs several
   such processes beside the remote-control session — scheduled jobs,
   dashboard turns, the base-job scripts, Ask-Ara answers — all on the same
   file, so near expiry they race for the one rotation. Claude Code
   arbitrates among its own processes with a lock (see the next section), but
   a process that gives up waiting fails its turn on the expired token, and
   the loser of an unarbitrated race (older versions, a lock gone stale)
   holds stale tokens, notices, and clears the credential file. The
   entrypoint keeps a backup (`.credentials.json.bak`) and a watcher that
   restores it and restarts the container; when the backup's tokens have
   themselves been rotated away, the server rejects them, the watcher records
   that in the `.restored-expiry` marker and gives up — the system is then
   signed out early, with no warning possible. The framework keeps its own
   processes out of this race with the pre-spawn refresh below; what remains
   is never to start an extra `claude` process by hand against the same
   credentials while remote-control is active without running
   `claude_auth.py refresh` first.

## The pre-spawn refresh (`claude_auth.py refresh`)

Every `claude` process the framework starts — the scheduler's prompt jobs
(`scripts/scheduler.py`), every gateway spawn (`_run_claude()` in
`scripts/web-gateway.py`: conversation turns, transcript cleanup, the
presentation lint), the base-job scripts (`agent-self-review.py`,
`news-curate.py`, `triage-gate.py`), Ask-Ara answers (`ara-mcp-server.py`),
the remote-control session itself (the entrypoint, right before `exec`) and
sub-sessions from the `spawn-session` skill — first calls
`claude_auth.ensure_fresh_credentials()`. It reads the credential file and,
only when the access token expires within `CLAUDE_AUTH_REFRESH_AHEAD_SECONDS`
(default 900):

1. takes an `flock` on `.credentials.json.lock` — one lock for every
   framework spawner, released by the kernel if the holder dies;
2. waits while Claude Code itself holds *its* refresh lock (a live
   `<config-dir>/.oauth_refresh.lock` or legacy `<config-dir>.lock`
   directory — the CLI's proper-lockfile lock, touched every 5 s while held
   and stale after 60 s), so no competing refresh is ever sent with a pair a
   session is rotating at that moment;
3. re-reads the file — a spawner that waited usually finds the job done and
   **adopts** the fresh pair;
4. otherwise performs the one refresh (the CLI's own `refresh_token` grant,
   with the stored scopes) and installs the reply like a re-login does:
   atomic write, backup renewed, rejected-restore marker cleared.

The child then starts on a token good for hours and refreshes nothing. The
common case — a token with more than the margin left — costs one file read
and takes no lock. Every outcome is non-fatal: the spawn goes ahead
regardless, and a failure (`failed`, `lock_timeout`, `expired`) only logs, in
the spawner's own log, why the session will be refreshing for itself. Nothing
on this path ever clears credentials. `CLAUDE_AUTH_REFRESH_AHEAD_SECONDS=0`
disables it; `CLAUDE_AUTH_LOCK_WAIT_SECONDS` (default 60) bounds the wait for
either lock.

This is not the out-of-band refresh the monitor refuses to perform: the
rotation is exactly the one the child would trigger seconds later, moved
before the spawn and under a lock — the number of rotations does not change,
only who performs them and how many at a time. The margin is wider than the
CLI's own 300 s on purpose, so the spawner gets there first; the long-lived
remote-control session, which refreshes for itself, then finds the newer pair
on disk at its next refresh and adopts it (Claude Code re-reads the file
under its lock before refreshing — verified against 2.1.261).

What Claude Code does on its own, for reference (2.1.261): a refresh is
attempted when the access token is within 300 s of expiry; the lock above is
retried five times with 1–2 s pauses, after which the process gives up
(`lock_busy`) and the request goes out on the expired token; under the lock
the file is re-read and a newer pair adopted; a refresh rejected with
`invalid_grant` marks the pair dead and clears it on disk. The pre-spawn
refresh removes the framework's processes from that contention altogether;
it cannot cover a `claude` started by hand — hence the rule above.

## The monitor (`scripts/claude-auth-monitor.py`)

Forked by the entrypoint alongside the messenger gateway monitor; it costs no
Claude credits — each tick is a handful of file reads via
`claude_auth.credential_status()`. It deliberately performs **no token refresh
of its own**: an out-of-band refresh — one at a time of the monitor's choosing,
with no session about to need it — would join the rotation race above and
cause the clobbering it is meant to prevent. (The pre-spawn refresh is the
opposite case: a session is about to start and would refresh at once.)

Verdicts and what they trigger (dashboard conversation → Web Push, like an
incoming message; the notice links to `/claude-auth`):

| State | Meaning | Notification |
|-------|---------|--------------|
| `ok` | Refresh token present, nothing due | none; recovery is reported when an incident ends |
| `expiring` | Refresh token expires within `CLAUDE_AUTH_WARN_DAYS` (default 3) | "expires soon" thread, reminded daily |
| `stale` | Live file cleared with a valid backup (auto-restore should fix it), or access token unrefreshed for over `CLAUDE_AUTH_STALE_HOURS` (default 24) | same warning thread |
| `needs_login` | No credentials anywhere, backup rejected by the server, or refresh token expired | "sign-in broken" thread, reminded every 6 h |

Old credential files without `refreshTokenExpiresAt` classify as `ok` — no
false alarms, just no advance warning until the file is rewritten by a login
that records it.

Debounce (two consecutive bad ticks, interval default 300 s) keeps the
mid-rotation window and the watcher's restore-restart cycle from paging
anybody. Incident state persists in `/root/.retinue/claude-auth-monitor/` so
the restart a re-login triggers does not re-notify.

Deployments that authenticate through a Claude-compatible gateway
(`ANTHROPIC_BASE_URL` set, `RETINUE_GATEWAY_USES_CLAUDE_OAUTH` unset) have no
OAuth sign-in; the monitor detects that and exits. `CLAUDE_AUTH_MONITOR=0`
disables it explicitly.

## Browser re-login (`/claude-auth`)

The dashboard page (linked from settings and from `/gateways`) shows the
credential state — subscription, sign-in valid-until, access-token expiry,
backup state, whether the remote-control process is running — and performs
the re-login:

1. **Sign in again** asks the gateway for a fresh authorize URL (the same
   authorization-code + PKCE flow the CLI's `/login` performs; the PKCE
   verifier stays in gateway memory only).
2. The user opens that URL — on any device, typically the phone the
   notification arrived on — approves, and Anthropic's callback page displays
   a code.
3. Pasting the code back completes the exchange; the gateway writes
   `.credentials.json` (atomic, mode 0600, unknown fields preserved), renews
   the entrypoint's backup, clears the rejected-restore marker, and — by
   default — restarts the container so every process starts on the fresh
   credentials. The page reconnects by itself once the gateway is back.

The restart matters even when re-logging in proactively: a running session
keeps its old tokens in memory, and its next refresh would rotate the *old*
family and overwrite the new file. Restarting immediately is the same
recovery path the entrypoint's own watcher uses.

Console equivalents, for completeness (both write the same file):

```bash
# without stopping anything, from any shell in the container:
python3 /workspace/scripts/claude_auth.py login    # prints URL, prompts for code
python3 /workspace/scripts/claude_auth.py status   # diagnosis
python3 /workspace/scripts/claude_auth.py refresh  # the pre-spawn refresh, by hand

# the original procedure still works:
docker compose stop retinue && docker compose run --rm retinue interactive  # then: claude
```

## Security notes

- The page and its endpoints sit behind the same Traefik edge auth as the
  rest of the dashboard; completing a sign-in grants exactly what approving
  `/sends` grants — action in the user's name. No tokens are ever displayed
  or logged; the status JSON carries expiry metadata only.
- Sign-in attempts (PKCE verifier + state) live in gateway memory with a
  30-minute TTL and die with the process. The pasted code is single-use and
  bound to the attempt's `state`; a code from a different attempt is
  rejected.
- The POST endpoints reject cross-site requests via `Sec-Fetch-Site` (the
  dashboard's basic-auth credential is ambient, so CSRF is otherwise
  possible; absent header — curl, old engines — is allowed, matching the
  other routes).

## Coupling caveat

The OAuth endpoints, client id, scopes, and the credential-file schema are
Claude Code's, not a published API (verified against Claude Code 2.1.240 —
authorize `https://claude.com/cai/oauth/authorize`, token
`https://platform.claude.com/v1/oauth/token`, manual-redirect
`https://platform.claude.com/oauth/code/callback`). If Anthropic moves any of
them, override without a code change:

```dotenv
CLAUDE_OAUTH_AUTHORIZE_URL=…   CLAUDE_OAUTH_TOKEN_URL=…
CLAUDE_OAUTH_REDIRECT_URI=…    CLAUDE_OAUTH_CLIENT_ID=…
CLAUDE_OAUTH_SCOPES=…          CLAUDE_OAUTH_USER_AGENT=…
```

The failure mode is graceful either way: the exchange surfaces the server's
error on the page, and the console paths keep working.

The pre-spawn refresh adds two more couplings of the same kind, both
verified against 2.1.261 and both harmless when they drift: the CLI's 300 s
refresh margin (ours is wider, so a narrower one on their side changes
nothing) and the names of its lock directories (`<config-dir>/.oauth_refresh.lock`,
legacy `<config-dir>.lock`). If those move, the framework merely stops
noticing a CLI refresh in flight and the CLI's own re-read-and-adopt covers
that overlap, as it does today.

One such coupling is load-bearing enough to name: Cloudflare fronts the OAuth
endpoints and rejects generic HTTP clients outright — **"Token endpoint
returned HTTP 403: error code: 1010"** is Cloudflare's browser-signature ban,
not an Anthropic error. The exchange therefore identifies itself as the Claude
Code CLI (`claude-cli/<installed version> (external, cli)` — it is the CLI's
own sign-in flow being performed). If that signature is ever banned too, set
`CLAUDE_OAUTH_USER_AGENT` to whatever the then-current CLI sends.
