# Using Retinue with Ollama

Retinue still runs every conversation turn and scheduled job as `claude -p`.
Ollama does not replace that harness. It only supplies the model behind
Claude Code's Anthropic-compatible Messages API.

Two supported shapes:

1. **Direct.** Point Claude Code at an Ollama server that speaks that API.
2. **Via LiteLLM** (the shipped default proxy). Point Claude Code at
   `http://litellm:4000` and let LiteLLM remap `ollama/<tag>` to Ollama.

Use LiteLLM when you want a conversation-model picker, several local tags,
or to keep the same `ANTHROPIC_BASE_URL` the rest of the stack already uses.

## What works, and what does not

A short completion against Ollama (`/api/chat` or LiteLLM `/v1/messages`
with a one-line prompt) is not the same as a dashboard turn.

Dashboard turns send Claude Code's full Ara prefix: `CLAUDE.md`, tool
schemas, memory, and often 4k–16k input tokens. Many pulled Ollama tags
cannot carry that load:

| Typical tag | Why it fails as Ara |
|---|---|
| `llama3:latest` (8B, 8k context) | Prompt overflows or the model ramble-fills the output budget with word salad. The gateway then stores `(no reply)` or the garbage. |
| Thinking models (Qwen3, Gemma 4, …) | Default `think` spends the output budget on a hidden trace. Visible content is empty. |
| Tags without a tools capability | Claude Code always advertises agent tools. Ollama then 400s (`does not support tools`). |

Local tags that answer a short “Who was the last Democratic president…?”
probe can still produce empty or nonsense bubbles in the dashboard. Prefer
a larger-context instruct model (for example a current Qwen 3.x tag) for
Ara turns. Treat 8B / 8k chat models as probes, not as the default.

Do **not** continue a thread whose last assistant bubble is `(no reply)`
or word salad. That text is re-injected into the next prompt. Start a
new thread.

## Direct Ollama (no LiteLLM)

Ollama must be reachable from the `retinue` container on port 11434.
Desktop installs often bind `127.0.0.1` only; a container then cannot
reach them. Bind the host to an address the Compose network can use
(`0.0.0.0`, or the host IP you put in `extra_hosts`), and set:

```dotenv
ANTHROPIC_AUTH_TOKEN=ollama
ANTHROPIC_API_KEY=
ANTHROPIC_BASE_URL=http://host.docker.internal:11434
RETINUE_CLAUDE_MODEL=qwen3.6:latest
```

`RETINUE_CLAUDE_MODEL` must be a tag `ollama list` actually shows.
Omit `RETINUE_GATEWAY_USES_CLAUDE_OAUTH` on this path — remote-control
sessions need a Claude.ai login.

If the container talks to the host through an HTTP proxy (the shipped
egress-audit sidecar), bypass it for Ollama (`NO_PROXY` must include
`host.docker.internal` and the host IP). A proxied `GET /api/tags` often
returns 502 even when Ollama is up.

### Windows + WSL2

Docker's default `host.docker.internal` → `host-gateway` is the WSL
`docker0` bridge, not the Windows host where Ollama Desktop listens.
Point `extra_hosts` at the Windows address of the WSL vEthernet adapter
(the WSL default gateway) and publish Windows `11434` on that address
if Desktop only bound localhost.

## LiteLLM + Ollama

Keep Claude Code on the in-stack proxy and send only the proxy key:

```dotenv
ANTHROPIC_API_KEY=<LITELLM_MASTER_KEY>
ANTHROPIC_AUTH_TOKEN=<LITELLM_MASTER_KEY>
ANTHROPIC_BASE_URL=http://litellm:4000
ANTHROPIC_CUSTOM_HEADERS=x-litellm-api-key: Bearer <LITELLM_MASTER_KEY>
RETINUE_CLAUDE_MODEL=retinue-claude
LITELLM_PRIMARY_MODEL=ollama_chat/qwen3.6:latest
```

Do not set `RETINUE_GATEWAY_USES_CLAUDE_OAUTH` when the primary is local
Ollama. The `/login` error means `ANTHROPIC_BASE_URL` is unset and Claude
Code is still calling Anthropic.

### Routes that make local tags usable

Add (or override) LiteLLM routes so a picked `ollama/<tag>` is a chat
completion, not a generate-path or a thinking dump:

```yaml
- model_name: retinue-claude
  litellm_params:
    model: os.environ/LITELLM_PRIMARY_MODEL
    api_base: http://host.docker.internal:11434
    think: false
    additional_drop_params: ["tools", "tool_choice", "functions"]
    num_predict: 512
  model_info:
    max_output_tokens: 512
    max_input_tokens: 8192

- model_name: ollama/*
  litellm_params:
    model: ollama_chat/*
    api_base: http://host.docker.internal:11434
    think: false
    additional_drop_params: ["tools", "tool_choice", "functions"]
    num_predict: 512
  model_info:
    max_output_tokens: 512
    max_input_tokens: 8192
```

Why each knob exists:

- **`ollama_chat/*` remap** — `/api/chat` is the path that honours
  `think` and message roles. A leftover `ollama/*` generate route will
  ignore that.
- **`think: false`** — otherwise Qwen3 / Gemma 4 spend Claude Code's
  output budget on a hidden thinking field. The dashboard stores
  `(no reply)`.
- **`additional_drop_params: [tools, …]`** — Claude Code always sends
  tool schemas. Tags without a tools capability (`llama3:latest` and
  many others) then 400.
- **`num_predict` / `model_info.max_output_tokens`** — Claude Code
  otherwise advertises ~32k output tokens. A small local model will
  fill that budget with garbage for a long time. Cap it (256–512 is
  enough for a chat bubble). Raise it only after the model is known
  to stay coherent.

`api_base` must be the same host:port the `litellm` service can reach.
On WSL2 + Windows Desktop that is usually `host.docker.internal` plus
an `extra_hosts` override, not Docker's default `host-gateway`.

### Conversation picker

The dashboard list is `GET /conversation-models`. With LiteLLM it is
built from `GET /model/info` and `GET /v1/models`. Plumbing aliases
(`retinue-claude`, wildcards) stay hidden. An Ollama primary also
drops leftover Claude catalog seeds.

If the picker still shows only Claude ids, set an explicit list. That
wins over LiteLLM:

```dotenv
RETINUE_CONVERSATION_MODELS=[{"id":"ollama/qwen3.6:latest","label":"qwen3.6"},{"id":"ollama/gemma4:12b","label":"gemma4"}]
```

A small helper that turns `GET /api/tags` into that JSON is enough to
keep the dropdown aligned with pulled models. Recreate the `retinue`
service after changing the env var (the list is read at process start
and cached briefly).

## Checking a tag before you chat

From a host that can reach Ollama:

```bash
curl -s http://127.0.0.1:11434/api/tags
curl -s http://127.0.0.1:11434/api/show -d '{"name":"qwen3.6:latest"}'
```

Confirm `context_length` and `capabilities`. Then a one-line Messages
probe through LiteLLM (not `claude -p`) should return visible text:

```bash
curl -s http://localhost:4000/v1/messages \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "x-litellm-api-key: Bearer $LITELLM_MASTER_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"ollama/qwen3.6:latest","max_tokens":64,"messages":[{"role":"user","content":"Reply with exactly the word pong."}]}'
```

If this is empty, fix `think` / `ollama_chat` first. If this is `pong`
but the dashboard is still empty or nonsense, the failure is the Ara
prefix, not Ollama reachability.

`claude -p --bare` (or `CLAUDE_CODE_SIMPLE=1`) skips `CLAUDE.md` and
most harness. That is a useful A/B for “does this tag answer at all?”
Do not set `CLAUDE_CODE_SIMPLE` on the whole `retinue` service: it
would also strip Ara from scheduler and triage jobs.

## Operational checklist

1. Ollama is up and `GET /api/tags` works from the process that will
   call it (LiteLLM container, not only the Windows/WSL localhost).
2. `ANTHROPIC_BASE_URL` is the proxy or Ollama URL the `retinue`
   container can resolve. Unset → `/login`.
3. Picked ids exist as LiteLLM routes (`ollama/*` wildcard or an
   explicit list).
4. Thinking is off; tools are dropped; output is capped.
5. The thread model is a tag that can survive a several-thousand-token
   system prompt. Start a **new** thread after any failed turn.

See also the short Ollama / LiteLLM notes in the [root README](../README.md#claude-compatible-model-gateways).
