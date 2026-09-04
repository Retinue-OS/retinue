# Retinue — containerised runtime
# Bundles Claude Code CLI, Signal CLI, Git, and the core agents/scripts.
# QLever runs as separate services (see docker-compose.yml).
# Chambers (mounted repositories, declared in chambers.json) are mounted at
# runtime into /workspace/chambers/<name>; each may ship its own agents as a
# Claude Code plugin under .retinue/. The framework ships chambers.example.json;
# a deployment bind-mounts its own chambers.json over it.
#
# Build:  docker compose build
# Run:    docker compose run --rm retinue interactive   (first-time login)
#         docker compose up -d                          (headless, auto-restart)

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# ── System packages ──────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    git git-lfs curl wget ca-certificates gnupg sudo less vim jq \
    python3 python3-pip python3-venv \
    openjdk-21-jre-headless \
    ffmpeg \
    cron \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js 22 LTS ──────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/nodesource_setup.sh \
    && bash /tmp/nodesource_setup.sh \
    && apt-get install -y --no-install-recommends nodejs \
    && rm /tmp/nodesource_setup.sh \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

# ── GitHub CLI ───────────────────────────────────────────────────────
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# ── Claude Code CLI ─────────────────────────────────────────────────
# Pinned, and deliberately so. The in-container auto-updater is disabled in
# the deployment (DISABLE_AUTOUPDATER=1) because a restart landing mid-swap
# leaves neither the old nor the new install and every subsequent restart
# replays the damage — so the image is the only place the version moves.
#
# Unpinned (`npm install -g @anthropic-ai/claude-code`) that did not work:
# the layer depends on no file in the repo, so `docker compose build` reuses
# the cache indefinitely and the version silently freezes at whatever was
# latest on the day the layer was first built. Naming the version makes the
# RUN command change, which invalidates the layer, which is what makes an
# upgrade actually take effect on rebuild.
#
# .github/workflows/check-claude-code.yml watches the npm registry and opens
# a bump PR against this ARG when a newer version appears.
ARG CLAUDE_CODE_VERSION=2.1.260
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# ── Core Python dependencies ────────────────────────────────────────
# pywebpush (with cryptography, http-ece, py-vapid) powers the dashboard's Web
# Push notifications; see scripts/push_notify.py.
# langdetect gives the dashboard's speech synthesis a language-agnostic tag for
# stored replies (~55 languages, no per-language bias); see web-gateway.py.
RUN python3 -m pip install --break-system-packages --no-cache-dir markdown-it-py pywebpush langdetect

# ── Agent logic, scripts, and session instructions (baked into image)
COPY agents/         /workspace/agents/
COPY scripts/        /workspace/scripts/
COPY .claude/        /workspace/.claude/
COPY .claude-plugin/ /workspace/.claude-plugin/
COPY CLAUDE.md       /workspace/CLAUDE.md
COPY examples/       /workspace/examples/
COPY docs/           /workspace/docs/
COPY webapp/         /workspace/webapp/
RUN chmod +x /workspace/scripts/*.sh && mkdir -p /workspace/chambers

# ── Entrypoint ──────────────────────────────────────────────────────
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["remote-control"]
