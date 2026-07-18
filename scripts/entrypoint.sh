#!/usr/bin/env bash
# Container entrypoint for the Retinue system.
# Mounts the chambers declared in /workspace/chambers.json (a "chamber" is one
# mounted repository: data plus its agents/skills) into /workspace/chambers/<name>
# (if not already present), autodetects and registers each chamber's Claude Code
# plugin, configures git, then drops into the requested mode: interactive
# (default) or remote-control. QLever runs in separate compose services.
set -euo pipefail

# Chamber locations.
CHAMBERS_DIR="${CHAMBERS_DIR:-/workspace/chambers}"
CHAMBERS_MANIFEST="${CHAMBERS_MANIFEST:-/workspace/chambers.json}"
export CHAMBERS_DIR CHAMBERS_MANIFEST

# ── Ensure egress-audit CA exists (auto-generate if missing) ────────
# The MITM proxy needs a CA. Generating it at container start means a
# deployment can update to a version that includes egress auditing without
# having to run a manual one-time setup step on the host. The private key
# lives on a persistent volume so the same CA survives container recreation.
EGRESS_CERT_DIR="/etc/egress-audit/certs"
mkdir -p "$EGRESS_CERT_DIR"
if [[ ! -f "$EGRESS_CERT_DIR/mitmproxy-ca.pem" ]]; then
  echo "[egress-audit] Generating CA in $EGRESS_CERT_DIR ..."
  openssl req -x509 -newkey rsa:2048 \
    -keyout "$EGRESS_CERT_DIR/egress-ca-key.pem" \
    -out "$EGRESS_CERT_DIR/egress-ca-cert.pem" \
    -days 3650 -nodes \
    -subj "/CN=Retinue Egress Audit CA/O=Retinue" \
    -addext "subjectKeyIdentifier=hash" \
    -addext "authorityKeyIdentifier=keyid:always,issuer" \
    -addext "basicConstraints=critical,CA:TRUE"
  cat "$EGRESS_CERT_DIR/egress-ca-key.pem" "$EGRESS_CERT_DIR/egress-ca-cert.pem" \
    > "$EGRESS_CERT_DIR/mitmproxy-ca.pem"
  chmod 600 "$EGRESS_CERT_DIR/mitmproxy-ca.pem" "$EGRESS_CERT_DIR/egress-ca-key.pem"
  chmod 644 "$EGRESS_CERT_DIR/egress-ca-cert.pem"
  echo "[egress-audit] CA generated."
fi
# Also keep a copy under /root so the same CA is backed up on the persistent
# /root volume even if the dedicated certs volume is ever reset.
mkdir -p /root/.retinue/egress-audit/certs
if [[ ! -f /root/.retinue/egress-audit/certs/egress-ca-cert.pem ]]; then
  cp "$EGRESS_CERT_DIR/egress-ca-cert.pem" /root/.retinue/egress-audit/certs/egress-ca-cert.pem
fi

# ── Git configuration ───────────────────────────────────────────────
git config --global --add safe.directory /workspace
# Route git HTTPS traffic through the egress-audit proxy and trust the proxy CA.
# Without this, git fails TLS verification because it sees per-host certs issued
# by the MITM proxy instead of the real remote certificates.
git config --global http.proxy "${HTTP_PROXY:-}"
git config --global https.proxy "${HTTPS_PROXY:-}"
git config --global http.sslCAInfo "$EGRESS_CERT_DIR/egress-ca-cert.pem"

if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  git config --global credential.helper store
  echo "https://x-access-token:${GITHUB_TOKEN}@github.com" > ~/.git-credentials
  chmod 600 ~/.git-credentials
  echo "[git] Credential helper configured."
fi

# ── Mount the chambers declared in chambers.json ─────────────────────
mkdir -p "$CHAMBERS_DIR"
while IFS= read -r entry; do
  [[ -z "$entry" ]] && continue
  name="$(jq -r '.name // ""' <<<"$entry")"
  url="$(jq -r '.url // ""' <<<"$entry")"
  url_env="$(jq -r '.url_env // ""' <<<"$entry")"
  path="$(jq -r '.path // ""' <<<"$entry")"
  [[ -z "$name" ]] && continue
  target="$CHAMBERS_DIR/$name"
  # A `path` entry mounts a local directory (relative to /workspace) in place of
  # cloning — used by the bundled example chambers and host-mounted chambers.
  if [[ -n "$path" ]]; then
    src="/workspace/$path"
    if [[ -e "$target" || -L "$target" ]]; then
      echo "[fs] Chamber '$name' already present at $target."
    elif [[ -d "$src" ]]; then
      ln -s "$src" "$target"
      echo "[fs] Linked chamber '$name' -> $src."
    else
      echo "[error] Chamber '$name' declares path '$path' but $src does not exist." >&2
      exit 1
    fi
    git config --global --add safe.directory "$target"
    continue
  fi
  # An env var named in url_env overrides the manifest URL when set.
  if [[ -n "$url_env" && -n "${!url_env:-}" ]]; then
    url="${!url_env}"
  fi
  git config --global --add safe.directory "$target"
  if [[ -d "$target/.git" ]]; then
    echo "[git] Chamber '$name' already present at $target."
  elif [[ -d "$target" ]] && [[ -n "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "[fs] Using pre-mounted contents for '$name' at $target."
  elif [[ -z "$url" ]]; then
    echo "[error] No URL for chamber '$name' (manifest url empty, $url_env unset) and no mounted contents found." >&2
    exit 1
  else
    echo "[git] Cloning '$name' from $url ..."
    git clone "$url" "$target"
    echo "[git] Clone complete."
  fi
done < <(jq -c '.chambers[]' "$CHAMBERS_MANIFEST")

cd /workspace

# ── Generate the plugin marketplace by autodetecting chamber plugins ─
# Each chamber that carries a plugin subdirectory (by convention .retinue/,
# holding .claude-plugin/plugin.json) provides agents/skills to the session.
# The marketplace identity comes from the template; one entry per chamber whose
# plugin is autodetected is appended, with name/description read from its
# plugin.json (their natural home — not duplicated in chambers.json).
MARKETPLACE="/workspace/.claude-plugin/marketplace.json"
MARKETPLACE_TEMPLATE="/workspace/.claude-plugin/marketplace.template.json"
generate_marketplace() {
  local plugins="[]"
  local dir pj pname pdesc cname
  for dir in "$CHAMBERS_DIR"/*/; do
    [[ -d "$dir" ]] || continue
    pj="${dir}.retinue/.claude-plugin/plugin.json"
    [[ -f "$pj" ]] || continue
    cname="$(basename "$dir")"
    pname="$(jq -r '.name // empty' "$pj")"
    [[ -z "$pname" ]] && pname="$cname"
    pdesc="$(jq -r '.description // empty' "$pj")"
    plugins="$(jq \
      --arg n "$pname" \
      --arg s "./chambers/$cname/.retinue" \
      --arg d "$pdesc" \
      '. + [ {name: $n, source: $s} + (if $d == "" then {} else {description: $d} end) ]' \
      <<<"$plugins")"
  done
  jq --argjson plugins "$plugins" '.plugins = $plugins' "$MARKETPLACE_TEMPLATE" > "$MARKETPLACE"
}
generate_marketplace
echo "[plugin] Generated marketplace.json ($(jq '.plugins | length' "$MARKETPLACE") chamber plugin(s))."

# ── Register chamber plugins (marketplace lives at /workspace) ───────
# Non-fatal: on first-ever start claude may not be configured yet.
# `claude plugin install` is a no-op once the plugin's version is cached, and the
# cache sits on the persistent /root volume — so a plain install would leave an
# edited agent definition stale indefinitely. sync-plugins.py compares the cached
# copy against the chamber and reinstalls the ones that drifted.
if command -v claude >/dev/null 2>&1; then
  if claude plugin marketplace add /workspace >/dev/null 2>&1; then
    echo "[plugin] Marketplace 'retinue' registered from /workspace."
    python3 /workspace/scripts/sync-plugins.py || \
      echo "[plugin][warn] Plugin sync reported a problem." >&2
  else
    echo "[plugin][warn] Could not register marketplace (first run before login?)." >&2
  fi
fi

# ── Install Python dependencies from every chamber's requirements.txt ─
REQ_FILES=()
for dir in "$CHAMBERS_DIR"/*/; do
  [[ -f "${dir}requirements.txt" ]] && REQ_FILES+=("${dir}requirements.txt")
done
if (( ${#REQ_FILES[@]} > 0 )); then
  VENV_DIR="/root/.venv"
  if [[ ! -d "$VENV_DIR" ]]; then
    echo "[pip] Creating virtual environment at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python3" -m ensurepip --upgrade || true
    "$VENV_DIR/bin/python3" -m pip install --upgrade pip
  fi
  # Only reinstall when the set of requirement files changed since last install
  REQ_HASH_FILE="$VENV_DIR/.requirements.md5"
  REQ_HASH=$(cat "${REQ_FILES[@]}" | md5sum | cut -d' ' -f1)
  if [[ ! -f "$REQ_HASH_FILE" ]] || [[ "$(cat "$REQ_HASH_FILE")" != "$REQ_HASH" ]]; then
    echo "[pip] Installing/updating Python dependencies (${#REQ_FILES[@]} chamber requirement file(s)) ..."
    for req in "${REQ_FILES[@]}"; do
      # Strip --hash= annotations: they go stale when packages are re-published
      # and the container image itself is the security boundary.
      STRIPPED_REQ=$(mktemp)
      python3 - "$req" "$STRIPPED_REQ" <<'EOF'
import re, sys
with open(sys.argv[1]) as f:
    content = f.read()
# Remove line-continuation + --hash=... blocks
content = re.sub(r'[ \t]*\\\s*\n[ \t]*--hash=[^\n]+', '', content)
# Remove any remaining standalone --hash= entries
content = re.sub(r'[ \t]*--hash=[^\n]+\n?', '', content)
with open(sys.argv[2], 'w') as f:
    f.write(content)
EOF
      "$VENV_DIR/bin/python3" -m pip install -q -r "$STRIPPED_REQ"
      rm -f "$STRIPPED_REQ"
    done
    echo "$REQ_HASH" > "$REQ_HASH_FILE"
    echo "[pip] Done."
  else
    echo "[pip] Dependencies up to date, skipping install."
  fi
  export PATH="$VENV_DIR/bin:$PATH"
fi

# ── Git write serialization shim ────────────────────────────────────
# Parallel agents (web-gateway sessions, scheduler, refresh, main session)
# share the working trees under /workspace/chambers/*. Put a `git` wrapper ahead
# of the real git on PATH so index/ref/remote-mutating operations are
# serialized per repository (flock), preventing index.lock and push races.
GIT_SHIM_DIR="/usr/local/lib/retinue-git-shim"
mkdir -p "$GIT_SHIM_DIR"
ln -sf /workspace/scripts/git-serialize.sh "$GIT_SHIM_DIR/git"
chmod +x /workspace/scripts/git-serialize.sh 2>/dev/null || true
export PATH="$GIT_SHIM_DIR:$PATH"

# ── Git hooks (every chamber that is a git repository) ──────────────
for dir in "$CHAMBERS_DIR"/*/; do
  [[ -d "${dir}.git" ]] || continue
  (cd "$dir" && bash /workspace/scripts/install-hooks.sh 2>/dev/null) || true
done

# ── Restore Claude config if missing (survives in the volume backup) ─
if [[ ! -f /root/.claude.json ]]; then
  latest_backup=$(ls -t /root/.claude/backups/.claude.json.backup.* 2>/dev/null | head -1)
  if [[ -n "$latest_backup" ]]; then
    cp "$latest_backup" /root/.claude.json
    echo "[claude] Restored config from backup: $(basename "$latest_backup")"
  fi
fi

# ── OAuth credential backup / restore ────────────────────────────────────
# A concurrent `claude --resume` (run via docker exec) shares OAuth tokens
# with the remote-control session.  When that session exits, Claude rotates
# the tokens; the remote-control process then detects its old tokens are
# stale and clears .credentials.json — leaving the system unauthenticated.
# Fix: keep a backup of the last known-good credentials file.  On startup,
# restore from the backup when the live file has been cleared.
CRED_FILE="/root/.claude/.credentials.json"
CRED_BAK="${CRED_FILE}.bak"
_cred_has_token() {
  [[ -f "$1" ]] || return 1
  [[ -n "$(jq -r '.claudeAiOauth.refreshToken // ""' "$1" 2>/dev/null)" ]]
}
if _cred_has_token "$CRED_FILE"; then
  cp "$CRED_FILE" "$CRED_BAK"
  echo "[oauth] Credential backup up to date."
elif _cred_has_token "$CRED_BAK"; then
  cp "$CRED_BAK" "$CRED_FILE"
  echo "[oauth] Credentials restored from backup (were cleared by a previous token rotation)."
fi

# ── Refresh external data sources (background, non-blocking) ────────
# Any chamber may declare refreshable sources in its .refresh.json.
for dir in "$CHAMBERS_DIR"/*/; do
  [[ -f "${dir}.refresh.json" ]] || continue
  echo "[refresh] Manifest found in $(basename "$dir") — starting background refresh dispatcher ..."
  mkdir -p "${dir}.refresh"
  python3 /workspace/scripts/refresh.py --data-dir "$dir" \
    >>"${dir}.refresh/startup.log" 2>&1 &
  disown
done

# ── Mode selection ──────────────────────────────────────────────────
MODE="${1:-interactive}"

case "$MODE" in
  interactive)
    echo ""
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║  Retinue — Interactive Mode                             ║"
    echo "║                                                         ║"
    echo "║  Run 'claude' to log in and trust this folder.          ║"
    echo "║  Once configured, restart with: remote-control          ║"
    echo "╚══════════════════════════════════════════════════════════╝"
    echo ""
    exec bash
    ;;
  remote-control)
    SESSION_NAME="$(date +%Y%m%d-%H%M%S)-${HOSTNAME}"
    DEBUG_LOG="/tmp/session-main-debug.log"
    CLAUDE_MODEL_ARGS=()
    if [[ -n "${RETINUE_CLAUDE_MODEL:-}" ]]; then
      CLAUDE_MODEL_ARGS=(--model "$RETINUE_CLAUDE_MODEL")
      echo "[claude] Using configured model: $RETINUE_CLAUDE_MODEL"
    fi
    if [[ -n "${ANTHROPIC_BASE_URL:-}" ]]; then
      echo "[claude] Using Claude-compatible gateway: $ANTHROPIC_BASE_URL"
    fi
    # Auto-generate the e-mail backend token when none is supplied: there is no
    # legitimate reason for an agent to bypass the send-control policy, so
    # credential isolation is always on. Generated before forking the gateway
    # and scheduler so all three processes share the same token via the env.
    if [ -z "${EMAIL_BACKEND_TOKEN:-}" ]; then
      EMAIL_BACKEND_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
      echo "[claude] Generated EMAIL_BACKEND_TOKEN (none supplied)."
    fi
    export EMAIL_BACKEND_TOKEN
    # Token gating the conversation-tabs backend (/internal/conversations) so only
    # in-container agents can open a dashboard thread on the user's behalf. Shared
    # with the gateway, scheduler and main agent via the env, like the e-mail one.
    if [ -z "${CONVERSATION_BACKEND_TOKEN:-}" ]; then
      CONVERSATION_BACKEND_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
      echo "[claude] Generated CONVERSATION_BACKEND_TOKEN (none supplied)."
    fi
    export CONVERSATION_BACKEND_TOKEN
    echo "[claude] Starting web gateway on port ${WEB_GATEWAY_PORT:-8080}..."
    python3 /workspace/scripts/web-gateway.py &
    echo "[claude] Starting task scheduler..."
    python3 /workspace/scripts/scheduler.py &
    # Chambers move under a running container (git pull, agents committing their
    # own files), so a boot-time sync is not enough. Each scheduler job spawns a
    # fresh `claude -p`, which picks up a resynced plugin on its next run.
    echo "[claude] Starting chamber plugin watcher..."
    python3 /workspace/scripts/sync-plugins.py --watch &
    if [[ -n "${ANTHROPIC_BASE_URL:-}" && "${RETINUE_GATEWAY_USES_CLAUDE_OAUTH:-}" != "true" ]]; then
      echo "[claude] Claude-compatible gateway mode: dashboard and scheduled jobs use the gateway; Claude.ai remote-control is disabled."
      exec tail -f /dev/null
    fi
    echo "[claude] Starting remote-control mode (session: $SESSION_NAME)..."
    # Background credential watcher: captures every non-empty write to the
    # credentials file (updated backup), then restores + restarts when the
    # file is cleared mid-session by a concurrent --resume token rotation.
    # The restart (SIGTERM to PID 1 = the claude process after exec) causes
    # Docker to restart the container; the new session starts with the
    # restored credentials and re-authenticates cleanly.
    #
    # Guards against infinite restart loops when the backup itself holds an
    # already-invalidated refresh token (HTTP 400 from Anthropic):
    #   • A persistent marker file records the expiresAt of the last backup
    #     we restored from.  If the backup's expiresAt matches the marker, the
    #     same credentials have already been tried and rejected — don't restore.
    #   • The marker is cleared when Claude itself writes new (different)
    #     credentials, proving the current tokens work.
    #
    # Debounce: require 5 consecutive 3-second polls of empty credentials
    # (~15 s) before triggering — avoids false-positives from Claude's own
    # atomic token-refresh writes.
    CRED_MARKER="${CRED_FILE}.restored-expiry"
    {
      last_seen_expiry=""
      empty_count=0
      while true; do
        sleep 3
        if _cred_has_token "$CRED_FILE"; then
          cp "$CRED_FILE" "$CRED_BAK"
          cur_expiry=$(jq -r '.claudeAiOauth.expiresAt // "0"' "$CRED_FILE" 2>/dev/null)
          if [[ -n "$cur_expiry" && "$cur_expiry" != "0" ]]; then
            # Claude wrote valid credentials; clear the "already-tried" marker
            # if the expiry is new (tokens were refreshed successfully).
            if [[ "$cur_expiry" != "$last_seen_expiry" ]]; then
              rm -f "$CRED_MARKER"
              last_seen_expiry="$cur_expiry"
            fi
          fi
          empty_count=0
        else
          (( empty_count++ )) || true
          if [[ $empty_count -ge 5 ]] && _cred_has_token "$CRED_BAK"; then
            bak_expiry=$(jq -r '.claudeAiOauth.expiresAt // "0"' "$CRED_BAK" 2>/dev/null)
            tried_expiry=$(cat "$CRED_MARKER" 2>/dev/null || echo "")
            if [[ "$bak_expiry" == "$tried_expiry" ]]; then
              # These exact credentials were already restored and rejected —
              # stop looping; the user must run: docker compose run --rm retinue interactive
              echo "[oauth] Backup credentials rejected by server. Log in again:" >&2
              echo "  docker compose stop retinue" >&2
              echo "  docker compose run --rm retinue interactive  → then: claude" >&2
              break  # exit watcher loop without restarting
            fi
            cp "$CRED_BAK" "$CRED_FILE"
            echo "$bak_expiry" > "$CRED_MARKER"
            echo "[oauth] Token rotation detected — credentials restored, restarting session." >&2
            kill -TERM 1 2>/dev/null
            break
          fi
        fi
      done
    } &
    disown
    # Warn if starting without valid credentials (the backup restore above
    # already attempted recovery; this fires only when no backup existed).
    if ! _cred_has_token "$CRED_FILE"; then
      echo "" >&2
      echo "━━━ [oauth] WARNING: No valid OAuth credentials ━━━━━━━━━━━━━━━━━━━━━" >&2
      echo "  Claude will start but cannot authenticate. To fix:" >&2
      echo "    docker compose stop retinue" >&2
      echo "    docker compose run --rm retinue interactive" >&2
      echo "  Then inside the container: claude" >&2
      echo "  NOTE: Never run 'claude' via 'docker exec' while remote-control" >&2
      echo "  is active — it rotates OAuth tokens and causes this state." >&2
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >&2
      echo "" >&2
    fi
    # Unset ANTHROPIC_API_KEY so that `claude` authenticates via OAuth
    # (stored in /root/.claude) rather than API-key mode.  API-key mode
    # connects but does not associate with the user's account — the session
    # would not appear in the mobile/desktop app.
    # The web-gateway (already forked above) retains the key in its own env.
    unset ANTHROPIC_API_KEY
    # Keep SMTP/IMAP credentials out of the agent's environment: route the
    # agent's email_client.py through the web gateway (forked above, which keeps
    # the credentials in its own env). The agent then cannot read EMAIL_PASS* nor
    # bypass the send-control policy by talking to SMTP/IMAP directly. The
    # EMAIL_BACKEND_TOKEN is always set (generated above when not supplied), so
    # this isolation is always active.
    export EMAIL_BACKEND_URL="http://localhost:${WEB_GATEWAY_PORT:-8080}/internal/email"
    for var in $(env | sed -n 's/^\(EMAIL_PASS[^=]*\)=.*/\1/p'); do
      unset "$var"
    done
    # Use --remote-control flag (not the subcommand) so that an interactive
    # session is created immediately and appears in the Claude app sidebar
    # without the user needing to connect via a URL first.
    CLAUDE_BIN="/usr/bin/claude"
    if [[ ! -x "$CLAUDE_BIN" ]]; then
      echo "[claude] Claude Code executable not found at $CLAUDE_BIN" >&2
      exit 127
    fi
    exec "$CLAUDE_BIN" --remote-control "$SESSION_NAME" --name "$SESSION_NAME" \
      "${CLAUDE_MODEL_ARGS[@]}" \
      --permission-mode "${CLAUDE_PERMISSION_MODE:-acceptEdits}" \
      --add-dir /root/.claude/uploads \
      --verbose --debug-file "$DEBUG_LOG"
    ;;
  *)
    echo "Unknown mode: $MODE" >&2
    echo "Usage: docker run ... retinue [interactive|remote-control]" >&2
    exit 1
    ;;
esac
