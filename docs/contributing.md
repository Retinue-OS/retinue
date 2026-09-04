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
