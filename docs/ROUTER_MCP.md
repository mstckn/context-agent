# Router MCP (optional)

`scripts/router_mcp_server.py` — OPTIONAL standalone MCP server
(autoroute spec §4, §25). Stdio JSON-RPC, protocol version
`2024-11-05`, `serverInfo` = `context-agent-router` v1.0.0.

Positioning:

- The Router MCP is optional. Dependency direction is
  router -> Context Intelligence only; this process never touches the
  Context core (hard separation, spec §46). If it crashes, is disabled,
  or is not installed, Context Intelligence keeps full functionality.
- The routing decision tools require `routing.enabled=true` in
  project/global configuration. When routing is disabled they return
  `NOT_ENABLED`; the server `initialize` instructions state that the
  Context core works fully without this server.

Run: `python scripts\router_mcp_server.py` (stdio, one JSON-RPC
message per line).

## Tools (exactly five)

### `route_task`

Deterministic routing decision. Consumes Context Intelligence
machine-readable analysis (`task_type`, `complexity`, `risk`,
`reasoning_requirement`, `context_confidence`); never re-classifies the
task.

| Input | Required | Notes |
|---|---|---|
| `analysis` | yes | `machine_readable` block from Context Intelligence route output |
| `task` | no | task text, stored in the history record |
| `context_tokens` | no | integer, used for cost projection |
| `record` | no | boolean, default `true` (write to `router.db`) |

Returns `ROUTED` / `ROUTING_BLOCKED` / `MANUAL_OVERRIDE_REQUIRED`.
When routing is disabled returns
`{"decision": "NOT_ENABLED", "message": "Router is disabled. Context Intelligence works without it; enable routing in Settings to use this tool."}`.

### `delegate_task`

HYBRID mode delegation: chooses the delegated model while the IDE model
remains the primary executor; telemetry records `executed_by=ide_model`
honestly. Inputs: `analysis` (required), `task`, `context_tokens`.
Returns `NOT_ENABLED` when routing is disabled.

### `execute_with_model`

Direct execution through a configured provider. Inputs: `model`
(catalog `model_id`, required), `prompt` (required), `system`,
`max_tokens` (default 2048). Requires an enabled provider and a stored
secret; otherwise returns a clean error (`unknown_model`,
`provider_not_enabled`, `no_api_key`, `http_error`,
`execution_failed`). This tool is not gated by `routing.enabled`.

### `get_model_candidates`

Ranked usable models for a task analysis: sufficient tier first, then
lowest expected cost. Input: `analysis` (required). Returns
`required_tier`, `reasons`, and up to 10 `candidates`
(`model_id`, `display_name`, `quality_tier`, `input_cost`,
`output_cost`, `context_window`, `max_output`, `latency_class`,
`provider_id`, `provider_health`), sorted by tier order, input cost,
then `model_id`. Not gated by `routing.enabled`.

### `explain_route`

Returns the recorded routing-history entry for a route id. Input:
`route_id` (integer, required). Looks up
`<project>/.context/router.db` history (last 500 rows); returns the
entry or `{"error": "not_found"}`. Not gated by `routing.enabled`.

## Decision semantics (router.py)

- Required tier = policy matrix preferred tier for `task_type`, raised
  by the risk floor (`LOW->ECONOMY`, `MEDIUM->BALANCED`,
  `HIGH/CRITICAL->STRONG`); `reasoning_requirement=high` raises the
  tier to at least `STRONG`.
- Quality guardrail: if no enabled+configured model at the required
  tier exists, the decision is `ROUTING_BLOCKED` — a weaker model is
  never presented as sufficient.
- Cost guardrails: per-task / daily / monthly limits with actions
  `WARN` (default), `BLOCK`, `ECONOMY_ONLY`, `REQUIRE_MANUAL_OVERRIDE`
  (the last yields `MANUAL_OVERRIDE_REQUIRED` unless the analysis
  carries `manual_override`).
- Candidate selection is a deterministic stable sort: lowest sufficient
  tier, then lowest input cost, then latency class (`local`, `fast`,
  `medium`, `slow`), then `model_id`.
- Escalation chain for quality failures only:
  `ECONOMY -> BALANCED -> STRONG -> FRONTIER`.

## Protocol behavior

- Supported methods: `initialize`, `notifications/initialized`,
  `tools/list`, `tools/call`, `ping`. Unknown methods answer JSON-RPC
  error `-32601`; unparseable input answers `-32603`.
- `tools/call` results are wrapped as MCP content:
  `{"content": [{"type": "text", "text": "<tool result JSON>"}]}`.
  Exceptions inside a tool call become
  `{"error": "router_failed", "message": "..."}` (message truncated to
  500 chars).
- If no project root can be resolved, the tool result is the
  `ProjectRootSelectionRequired` payload.

## Underlying CLI (router.py)

The server delegates to `scripts/router.py`, which is also usable
directly:

```powershell
python scripts\router.py status
python scripts\router.py providers list|enable|disable|health [--id ID] [--force]
python scripts\router.py models list|enable|disable --id MODEL_ID
python scripts\router.py policy show|mode SMART|CUSTOM|custom-set '<json>'
python scripts\router.py route --analysis-json '<json>' [--task T] [--context-tokens N] [--no-record]
python scripts\router.py delegate --analysis-json '<json>' [--task T] [--context-tokens N]
python scripts\router.py execute --model MODEL_ID --prompt P [--system S]
python scripts\router.py history [--limit N]
python scripts\router.py explain --id N
python scripts\router.py usage
```

## Storage

| Path | Content |
|---|---|
| `~/.context-agent/providers.json` | provider config (no secrets) |
| `~/.context-agent/models.json` | editable model catalog |
| `~/.context-agent/provider_health.json` | cached provider health |
| `~/.context-agent/gateway.json` | Model Gateway registry (see MODEL_GATEWAY.md) |
| `<project>/.context/router_policy.json` | project policy + `policy_version` |
| `<project>/.context/router.db` | `routing_history` + usage accounting |

Secrets live only in the secret store (masked in every listing);
provider health is never probed without a stored key.
