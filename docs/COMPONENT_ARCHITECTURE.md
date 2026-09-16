# Component Architecture

Context Agent is one product with separable internals. Context Intelligence is
the CORE and is always functional; model routing is an OPTIONAL layer that can
be enabled later without changing the core.

The engine internals (retrieval, ledger, budgeting, memory, storage schemas)
are documented in `docs/CONTEXT_INTELLIGENCE_ARCHITECTURE.md`; day-2 commands
live in `docs/OPERATIONS.md`. This document covers the component boundaries
and the rules that keep them separable.

## 1. Component map

```
                 CONTEXT PLATFORM
                        |
         +--------------+--------------+
         |                             |
 Context Intelligence            Model Routing
       CORE                       OPTIONAL
         |                             |
    Context MCP               Router MCP + Model Gateway
    (mcp_server.py)        (router.py, router_mcp_server.py,
         |                        model_gateway.py)
         |                             |
         +------------- Dashboard -------------+
                   (dashboard_server.py)
```

| Component | Entry points | Status |
|---|---|---|
| Context Intelligence (core) | `scripts/agent.py`, `route.py`, `index.py`, retrieval/memory/ledger modules | required |
| Context MCP | `scripts/mcp_server.py` (stdio JSON-RPC) | required |
| Router engine + Router MCP | `scripts/router.py`, `scripts/router_mcp_server.py` | optional |
| Model Gateway | `scripts/model_gateway.py` (local OpenAI-compatible HTTP) | optional |
| Dashboard / control plane | `scripts/dashboard_server.py` | optional |
| Shared substrate | `scripts/paths.py`, `config_store.py`, `identity.py`, `secret_store.py`, `git_meta.py` | required |

## 2. Dependency rule

Dependency direction is strictly:

```
Router MCP / Gateway  ->  Context Intelligence
```

never the reverse. Enforced in code:

* `router.py` is a standalone module; the Context core never imports it.
  Importing or running the router cannot break the Context core (failure
  isolation).
* The router CONSUMES the machine-readable task analysis produced by
  Context Intelligence and never re-classifies the task itself.
  `route.py` emits a `machine_readable` block
  (`task_type`, `complexity`, `risk`, `reasoning_requirement`,
  `context_confidence`, `context_confidence_score`, `safe_to_implement`,
  `domains`, `source: "context_intelligence"`); `agent.py` passes it through
  in the package `routing` section; `router.route_task()` and the Router MCP
  `route_task` tool take that block as their `analysis` input.
* Turning routing off (`routing.enabled=false`) returns the platform to clean
  Context-only operation; the router then reports `NOT ENABLED` and does not
  read provider secrets.

## 3. Core independence guarantees

Context Intelligence works fully when:

* Router MCP is disabled, unavailable, or not installed,
* the Model Gateway is unavailable,
* no AI provider is configured,
* no API key is configured.

The default execution mode is IDE MODEL, which requires no external API key:
the IDE's own LLM consumes Context Agent output (see
`config_store.SYSTEM_DEFAULTS`: `execution.mode=IDE_MODEL`,
`routing.enabled=false`). Provider errors are therefore never blockers for
Context-only users.

## 4. Execution modes

Resolved by `router.router_status()` from layered configuration
(`execution.mode`, see `docs/CROSS_IDE_CONFIGURATION.md`):

| Mode | Meaning | Availability check |
|---|---|---|
| IDE MODEL (default) | IDE-selected LLM does the work; Context MCP supplies optimized context | always available |
| HYBRID | IDE model stays primary; router may be delegated subtasks; telemetry records `executed_by=ide_model` | requires `routing.enabled=true` and at least one usable model |
| AUTO ROUTER | Gateway controls the primary model request | requires a live Model Gateway (`~/.context-agent/gateway.json` + `/healthz`) |

If a configured mode is unavailable, `router_status()` falls back to
IDE MODEL with an explanatory `mode_note` (e.g. "AUTO ROUTER NOT AVAILABLE
FOR THIS IDE CONFIGURATION"). It never claims a mode that is not actually in
control of the primary request.

## 5. Router layer (optional)

`router.py` is deterministic, ML-free:

* Tiers: `ECONOMY < BALANCED < STRONG < FRONTIER`.
* Required tier = policy matrix (`task_type` -> preferred tier) raised by the
  risk floor (`LOW->ECONOMY, MEDIUM->BALANCED, HIGH/CRITICAL->STRONG`) and by
  `reasoning_requirement=high` (forces at least STRONG).
* Candidate sort is stable: sufficient tier, then input cost, then latency
  class, then model id. Goal is lowest expected cost to a correct verified
  result, not the cheapest model.
* Quality guardrail: if no enabled+configured model reaches the required
  tier, the decision is `ROUTING_BLOCKED` - a weaker model is never presented
  as sufficient.
* Cost guardrails (per-task / daily / monthly) with actions `WARN`, `BLOCK`,
  `ECONOMY_ONLY`, `REQUIRE_MANUAL_OVERRIDE`.
* Decisions: `ROUTED`, `ROUTING_BLOCKED`, `MANUAL_OVERRIDE_REQUIRED`; every
  recorded route lands in `.context/router.db` (`routing_history`) and is
  inspectable via `router.py history` / `explain --id N`.

Router MCP (`router_mcp_server.py`) exposes exactly five tools, mirroring the
spec: `route_task`, `delegate_task`, `execute_with_model`,
`get_model_candidates`, `explain_route`. When routing is disabled,
`route_task`/`delegate_task` return `NOT_ENABLED` instead of erroring.

## 6. Model Gateway (optional)

`model_gateway.py` binds a local OpenAI-compatible HTTP server
(`127.0.0.1` only): `GET /healthz`, `GET /v1/models`,
`POST /v1/chat/completions`. It registers itself in
`~/.context-agent/gateway.json` (`port`, `pid`) and removes the registry on
exit; `router.gateway_available()` feature-detects it. Without the gateway,
AUTO ROUTER is reported unavailable and the system falls back to IDE MODEL;
nothing breaks. Context Intelligence is never moved into the gateway - the
gateway consumes it.

## 7. Storage map

| Location | Owner | Content |
|---|---|---|
| `<project>/.context/symbols.db` | core | index, graph, ledger, project memory (single file) |
| `<project>/.context/map.json` | core | project map summary |
| `<project>/.context/project_id.json` | identity | path-independent project id + scope name |
| `<project>/.context/identity.json` | identity | repository/checkout/IDE-instance/conversation ids |
| `<project>/.context/config.json` | config | PROJECT layer settings |
| `<project>/.context/sessions/<scope>/...` | core/config | sessions, SESSION layer config |
| `<project>/.context/router_policy.json` | router | routing policy + version |
| `<project>/.context/router.db` | router | routing history / usage |
| `~/.context-agent/config.json` | config | GLOBAL layer settings |
| `~/.context-agent/providers.json` | router | provider config (no secrets) |
| `~/.context-agent/models.json` | router | editable model catalog |
| `~/.context-agent/provider_health.json` | router | cached provider health |
| `~/.context-agent/gateway.json` | gateway | gateway registry |
| `~/.context-agent/secrets.json` | secret store | encrypted provider keys (never in context output) |

## 8. Failure isolation

* Router MCP crash / provider outage / gateway crash: Context MCP stays
  healthy; IDE MODEL remains usable.
* Router disabled: no code path reads provider secrets.
* Provider secrets never enter context data, ledger, memory, prompts, usage
  records, or diagnostics.
* Dashboard treats intentionally disabled components as `NOT ENABLED` /
  `NOT CONFIGURED`, not as errors (feature detection, not failure).

## 9. Related documents

* `docs/CONTEXT_INTELLIGENCE_ARCHITECTURE.md` - core engine design,
  retrieval scoring, ledger lifecycle, storage schemas.
* `docs/CONTEXT_MCP.md` - the Context MCP server surface (tools, resources,
  bootstrap).
* `docs/CROSS_IDE_CONFIGURATION.md` - identity, cross-IDE continuity, and
  the layered configuration model used by the execution modes above.
* `docs/OPERATIONS.md` - install, index, CLI, diagnostics, troubleshooting.
