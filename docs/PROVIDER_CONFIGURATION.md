# Provider Configuration

Provider and model settings belong to the optional routing layer
(`scripts/router.py`). The Context Intelligence core never reads them.
Secrets are stored separately; see SECRET_STORAGE.md.

## Storage files

| File | Content |
|---|---|
| `~/.context-agent/providers.json` | Provider list (NO secrets here) |
| `~/.context-agent/models.json` | Editable model catalog |
| `~/.context-agent/provider_health.json` | Cached health checks |
| `~/.context-agent/gateway.json` | Written by `model_gateway.py` (`port`, `pid`) |
| `<root>/.context/router_policy.json` | Routing policy + `policy_version` |
| `<root>/.context/router.db` | `routing_history` + usage accounting |

All JSON writes are atomic (temp file + `os.replace`).

## Built-in providers

Defaults are used until `providers.json` exists. Every provider starts
`enabled: false`.

| id | type | display_name | base_url | needs_key |
|---|---|---|---|---|
| `openai` | `openai` | OpenAI | `https://api.openai.com/v1` | yes |
| `anthropic` | `anthropic` | Anthropic | `https://api.anthropic.com/v1` | yes |
| `dashscope` | `dashscope` | Alibaba DashScope | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | yes |
| `openrouter` | `openrouter` | OpenRouter | `https://openrouter.ai/api/v1` | yes |
| `ollama` | `ollama` | Ollama (local) | `http://localhost:11434/v1` | no |
| `openai_compatible` | `openai_compatible` | OpenAI-compatible endpoint | user-supplied | yes |

## Model catalog fields

Each entry in `models.json` carries: `model_id`, `provider_id`,
`display_name`, `remote_model_name`, `context_window`, `max_output`,
`input_cost` / `output_cost` (USD per 1M tokens), `latency_class`
(`local` / `fast` / `medium` / `slow`), `quality_tier`
(`ECONOMY` / `BALANCED` / `STRONG` / `FRONTIER`), `capabilities`,
`enabled`.

Default catalog (9 models): `openai/gpt-4o-mini` (BALANCED),
`openai/gpt-4o` (STRONG), `openai/gpt-4.1` (FRONTIER),
`anthropic/claude-3-5-haiku` (ECONOMY), `anthropic/claude-sonnet-4`
(STRONG), `dashscope/qwen-turbo` (ECONOMY), `dashscope/qwen-plus`
(BALANCED), `openrouter/auto` (BALANCED), `ollama/qwen2.5-coder:7b`
(ECONOMY, cost 0).

A model is routable only when: the model is enabled, its provider is
enabled, the provider health is not `AUTHENTICATION_FAILED`, a secret
exists when `needs_key` is true, and `quality_tier` is one of the four
tiers.

## Health checks

```powershell
python scripts\router.py providers health            # all providers
python scripts\router.py providers health --id openai --force
```

The check calls `GET {base_url}/models` (4 s timeout) and caches the
result for 300 s unless `--force`. Rules:

- Disabled provider -> `NOT_ENABLED`.
- `needs_key` but no stored secret -> never probed; status `UNKNOWN` with
  `last_error_category: "no_api_key"` (spec §10).
- HTTP 200 -> `CONNECTED`; 401/403 -> `AUTHENTICATION_FAILED`;
  429 -> `RATE_LIMITED`; other HTTP or network errors -> `UNAVAILABLE`
  with a category (`auth`, `rate_limited`, `http_<code>`, `network`,
  `no_base_url`).

Auth headers are built from the secret store only at probe time:
`Authorization: Bearer <key>` for OpenAI-style APIs,
`x-api-key` + `anthropic-version: 2023-06-01` for Anthropic.

OpenCode Go/Zen requests also include `x-opencode-session`. Context Agent
derives an opaque value from the project and host conversation/model-session
identity: it remains stable within one conversation, differs across projects
and conversations, and never sends a raw local path or IDE identity.
OpenCode Go is configured as a separate `opencode_go` provider at
`https://opencode.ai/zen/go/v1`; it safely reuses the `opencode` secret-store
entry while keeping Go and free Zen model toggles independent.

## CLI

```powershell
python scripts\router.py providers list              # + has_secret, mask, health per provider
python scripts\router.py providers enable --id openai
python scripts\router.py providers disable --id openai
python scripts\router.py models list
python scripts\router.py models enable --id openai/gpt-4o
python scripts\router.py models disable --id openai/gpt-4o
python scripts\router.py execute --model openai/gpt-4o-mini --prompt "..." [--system "..."]
```

`providers list` never prints raw keys: only `has_secret` and the masked
value from the secret store.

`execute` returns errors, never exceptions: `unknown_model`,
`provider_not_enabled`, `no_api_key`, `http_error` (with `status`),
`execution_failed`.

## Layered configuration (SYSTEM / GLOBAL / PROJECT / SESSION / TASK)

`scripts/config_store.py` implements the hierarchical config (spec §15-17).
Highest layer wins; every effective value carries its source layer.

| Layer | Storage | Writable |
|---|---|---|
| SYSTEM | built-in defaults, never on disk | no |
| GLOBAL | `~/.context-agent/config.json` | yes |
| PROJECT | `<root>/.context/config.json` | yes |
| SESSION | `<root>/.context/sessions/<scope>[/<session>]/config.json` | yes |
| TASK | runtime overrides, passed at call time | no (never persisted) |

Routing preferences live in PROJECT/GLOBAL, never in an IDE. RESET TO
INHERITED deletes one key (or a whole layer) so the inherited value
applies again; task overrides never become defaults.

CLI:

```powershell
python scripts\config_store.py --effective [--key K] [--session S]   # VIEW EFFECTIVE CONFIGURATION
python scripts\config_store.py --show PROJECT
python scripts\config_store.py --set PROJECT execution.mode HYBRID [--session S]
python scripts\config_store.py --reset PROJECT [execution.mode] [--session S]
```

`--effective` returns `{key: {value, source, known}}`. SYSTEM and TASK
reject writes (`layer_not_writable`) and resets (`layer_not_resettable`).
Values are coerced to the SYSTEM default's type for known keys.

### SYSTEM default keys

| Key | Default | Values |
|---|---|---|
| `execution.mode` | `IDE_MODEL` | `IDE_MODEL` / `HYBRID` / `AUTO_ROUTER` |
| `routing.enabled` | `false` | bool |
| `routing.policy_mode` | `SMART` | `SMART` / `CUSTOM` |
| `routing.escalation_enabled` | `true` | bool |
| `routing.fallback_model` | `""` | model id |
| `budget.default_tokens` | `8000` | int |
| `budget.strict_quality` | `80` | int |
| `cost.guardrail_action` | `WARN` | `WARN` / `BLOCK` / `ECONOMY_ONLY` / `REQUIRE_MANUAL_OVERRIDE` |
| `cost.task_limit_usd` | `1.0` | float |
| `cost.daily_limit_usd` | `5.0` | float |
| `cost.monthly_limit_usd` | `50.0` | float |
| `context.diagnostics` | `false` | bool |
| `context.auto_index` | `true` | bool |

Unknown keys set from higher layers are kept and flagged `known: false`.
