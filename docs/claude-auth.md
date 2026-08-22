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
   pair on refresh. Two Claude processes sharing the file (the remote-control
   session plus a `claude` run via `docker exec`, or a `--resume` session)
   race: the loser holds stale tokens, notices, and clears the credential
   file. The entrypoint keeps a backup (`.credentials.json.bak`) and a
   watcher that restores it and restarts the container; when the backup's
   tokens have themselves been rotated away, the server rejects them, the
   watcher records that in the `.restored-expiry` marker and gives up — the
   system is then signed out early, with no warning possible. Avoid this
   class entirely by not running extra `claude` processes against the same
   credentials while remote-control is active.

## The monitor (`scripts/claude-auth-monitor.py`)

Forked by the entrypoint alongside the messenger gateway monitor; it costs no
Claude credits — each tick is a handful of file reads via
`claude_auth.credential_status()`. It deliberately performs **no token refresh
of its own**: an out-of-band refresh would join the rotation race above and
cause the clobbering it is meant to prevent.

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
CLAUDE_OAUTH_SCOPES=…
```

The failure mode is graceful either way: the exchange surfaces the server's
error on the page, and the console paths keep working.
