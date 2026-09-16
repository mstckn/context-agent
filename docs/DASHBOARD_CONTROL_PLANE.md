# Dashboard / Control Plane

`scripts/dashboard_server.py` — live Context Agent dashboard. It serves
a single HTML UI at `/` backed by a JSON API, acting as the shared
control plane for Context Intelligence and the optional router /
gateway layers (autoroute spec §12-14).

```powershell
python .context\scripts\dashboard_server.py [--host 127.0.0.1] [--port 8765]
```

Run from inside a project; the server root is resolved via
`find_project_root()`. Defaults: host `127.0.0.1`, port `8765`.

## Core properties

- Shared control plane. It works without the router: a disabled router
  is reported as `NOT ENABLED` / `router_component: "NOT ENABLED"`,
  which is a state, never an error. All Context Intelligence pages
  keep full functionality.
- Keyless onboarding. When no provider is configured and routing is
  disabled, the Overview page shows a zero-config onboarding card
  stating the active mode needs no API keys. IDE MODEL mode requires
  no keys at all; the Secrets page explicitly says having no stored
  keys is not an error.
- Multi-project. Registered projects live in
  `~/.context-agent/dashboard_registry.json`
  (`{"projects": [{"root", "url", "port"}]}`); the server registers
  its own root on startup. Any folder containing a `.context`
  directory can be added. Every endpoint accepts `?root=<path>` to
  target another project; unknown/invalid roots fall back to the
  server root. Each project stays isolated in its own `.context`
  directory (index, memory, config, router data).

## How it works

API handlers run scripts from the target project's `.context/scripts/`
as subprocesses (`cwd` = project root, default timeout 120 s) and
return their JSON stdout. Non-zero exit yields
`{"error": "script_failed", "stderr": ..., "stdout": ...}` (each
truncated to 2000 chars).

Preview isolation: context builds run with
`CONTEXT_AGENT_CLIENT=dashboard`, `CONTEXT_AGENT_SCOPE=dashboard-preview`
and `--skip-usage`, and `POST /api/router/route` always passes
`--no-record` — dashboard previews never pollute usage accounting or
routing history.

## UI pages

Overview, Projects, Context Intelligence, Project Memory, IDE Sessions,
Model Execution, AI Providers, Models, Auto Router, Usage & Cost,
Routing History, Diagnostics, Settings.

- Model Execution shows configured vs effective mode and the gateway
  state, and switches `execution.mode` at the PROJECT layer
  (`IDE_MODEL` / `HYBRID` / `AUTO_ROUTER`). If the gateway is offline,
  AUTO ROUTER falls back to IDE MODEL with the note
  "AUTO ROUTER NOT AVAILABLE FOR THIS IDE CONFIGURATION (no Model
  Gateway); falling back to IDE MODEL".
- Route Preview builds/reuses the last context package's
  `machine_readable` analysis and calls the route/delegate endpoints.
- Diagnostics produces a sanitized report (identity/continuity, router
  status, config, gateway, context status) — no secrets, tokens, or
  file contents.
- Settings shows effective config with source layer (SYSTEM is
  read-only) and can set/reset keys at the PROJECT layer.

## API reference

All endpoints accept `?root=<path>` to select the target project.

### GET

| Endpoint | Backing call |
|---|---|
| `/` | HTML UI |
| `/api/projects` | `{current_root, projects}` from the registry |
| `/api/status` | `agent.py --status` |
| `/api/eval` | `eval.py` (timeout 180 s) |
| `/api/capsule` | `capsule.py --current` |
| `/api/usage` | `usage.py --summary` |
| `/api/context` | 405 with hint "Use POST /api/context" |
| `/api/identity` | `identity.py --show` |
| `/api/continuity` | `identity.py --continuity` |
| `/api/handoff` | `identity.py --handoff [--session NAME]` |
| `/api/memory` | `memory.py --list` |
| `/api/sessions` | `session.py --show` + `session.py --history` |
| `/api/config` | `config_store.py --effective` |
| `/api/providers` | `router.py providers list` |
| `/api/models` | `router.py models list` |
| `/api/policy` | `router.py policy show` |
| `/api/secrets` | `secret_store.py --list` (masks only) |
| `/api/router/status` | `router.py status` |
| `/api/router/history?limit=N` | `router.py history --limit N` (default 50) |
| `/api/router/usage` | `router.py usage` |
| `/api/gateway` | `model_gateway.py status` |
| unknown path | 404 `{"error": "not_found"}` |

### POST

| Endpoint | Body / behavior |
|---|---|
| `/api/projects` | `{"add": "<path>"}` — path must contain `.context`, else 400 `context_not_found` |
| `/api/index` | runs `index.py` (timeout 300 s) |
| `/api/context` | `{"task" (required), "budget" (default "8000"), "fresh" (default true), "strict_quality" (0-100)}`. With `fresh`, clears dedup/budget first. Runs `agent.py <task> --budget N --skip-usage [--strict-quality S]` and returns the compact package: `task`, `scope`, `routing` (`confidence`, `context_plan`, `execution_plan`, `machine_readable`), `context_items`, `context_quality`, `strict_quality`, `capsule`, `usage`, `client`, `context_tokens`, `repo_tokens`, `budget` |
| `/api/config` | `{"action": "set"|"reset", "layer", "key", "value"}` -> `config_store.py --set/--reset`; 400 without layer+key |
| `/api/providers` | `{"action": "enable"|"disable"|"health", "id", "force"}` -> `router.py providers ...` (`health` may omit `id` to check all) |
| `/api/models` | `{"action": "enable"|"disable", "id"}` -> `router.py models ...` |
| `/api/secrets` | `{"action": "set"|"delete"|"test", "provider_id", "value"}`. `set` passes the key via the `SECRET_VALUE` env var only — never in argv or API responses. `delete` calls `secret_store.py --delete`. `test` runs a forced provider health probe (`router.py providers health --id <id> --force`) and returns `{provider_id, status, last_error_category, checked_at}` |
| `/api/policy` | `{"action": "mode"|"custom-set", "value"}` -> `router.py policy ...` |
| `/api/router/route`, `/api/router/delegate` | `{"analysis", "task", "context_tokens"}` -> `router.py route/delegate`; `route` always runs with `--no-record` |
| unknown path | 404 `{"error": "not_found"}` |

## Router status semantics

`GET /api/router/status` returns `router.router_status()`:
`execution_mode`, `effective_mode`, `mode_note`, `mode_available`
(`IDE_MODEL` always true; `HYBRID` needs routing enabled + usable
models; `AUTO_ROUTER` needs a live gateway), `routing_enabled`,
`providers_total/enabled/configured`, `usable_models`, `gateway`,
`router_component` (`"ENABLED"` / `"NOT ENABLED"`). `NOT ENABLED` is
never an error — it simply means routing is off and Context
Intelligence continues to work fully.

## Security notes

- Default bind is `127.0.0.1`.
- Secrets appear only masked; setting a key transmits it via env var,
  never in process argv or responses.
- The Diagnostics report is sanitized by design.
