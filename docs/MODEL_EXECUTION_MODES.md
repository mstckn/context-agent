# Model Execution Modes

Three execution modes exist (autoroute spec §2). The mode is a layered
configuration value (`execution.mode`), not an IDE setting: opening the
same project from another IDE keeps the mode.

| Mode | Default | Requires providers/keys | Requires Model Gateway |
|---|---|---|---|
| `IDE_MODEL` | yes | no | no |
| `HYBRID` | no | yes (at least one usable model + `routing.enabled`) | no |
| `AUTO_ROUTER` | no | yes | yes |

Default (SYSTEM layer): `execution.mode = IDE_MODEL`,
`routing.enabled = false`.

## MODE 1 — IDE MODEL (default)

"Use the model selected in my IDE."

- Context Intelligence: ON
- Router execution: OFF
- Model Gateway: NOT REQUIRED
- External API keys: NOT REQUIRED

Flow: IDE-selected LLM -> Context MCP -> optimized context -> the same
IDE-selected LLM continues the work. The core product is complete in this
mode; no routing code path runs.

## MODE 2 — HYBRID

"Keep my IDE model as the primary agent, but allow optional delegation to
other configured models."

- The IDE's own LLM is invoked first and remains the primary executor.
- The router may select a delegated model for subtasks (repetitive edits,
  mechanical refactoring, secondary analysis, inexpensive review, explicit
  user delegation).
- Telemetry is honest about execution: `delegate_task()` records
  `executed_by = "ide_model"` and a delegation block
  (`mode: "HYBRID"`, `primary_executor: "ide_model"`). It never claims the
  router replaced the IDE's primary model call.

Availability: `mode_available.HYBRID = routing.enabled AND usable_models > 0`
(usable = enabled model on an enabled, key-configured provider; see
PROVIDER_CONFIGURATION.md). If HYBRID is configured but unavailable, the
effective mode falls back to `IDE_MODEL` with the note
"HYBRID not configured (routing disabled or no usable model); IDE MODEL active".

## MODE 3 — AUTO ROUTER

"Automatically choose the most appropriate configured model before
execution."

A true Auto Router requires a Model Gateway (or another integration) that
controls the primary model request. MCP alone cannot intercept and replace
the IDE's primary model call, and the system never fakes this.

- Gateway detection (`gateway_available()`): reads
  `~/.context-agent/gateway.json` (written by `model_gateway.py` with
  `port` and `pid`) and probes `GET http://127.0.0.1:<port>/healthz`
  (1.5 s timeout). `mode_available.AUTO_ROUTER` is true only when the
  probe returns 200.
- If `execution.mode = AUTO_ROUTER` but no gateway is reachable, the
  effective mode is `IDE_MODEL` and `router_status()` reports:

  ```
  AUTO ROUTER NOT AVAILABLE FOR THIS IDE CONFIGURATION (no Model Gateway); falling back to IDE MODEL
  ```

  IDE MODEL and HYBRID remain allowed.

## Inspecting modes

```powershell
python scripts\router.py status
```

`router_status()` returns: `execution_mode` (configured value),
`effective_mode` (what actually runs after availability checks),
`mode_note`, `mode_available` (per mode), `routing_enabled`, provider
counts (`providers_total` / `providers_enabled` / `providers_configured`),
`usable_models`, `gateway`, and `router_component`
(`ENABLED` / `NOT ENABLED`). `NOT ENABLED` is a feature-detection result,
never an error.

## Setting the mode

The mode is stored in the GLOBAL or PROJECT configuration layer
(see PROVIDER_CONFIGURATION.md, layered configuration):

```powershell
python scripts\config_store.py --set PROJECT execution.mode HYBRID
python scripts\config_store.py --set PROJECT routing.enabled true
python scripts\config_store.py --effective --key execution.mode
python scripts\config_store.py --reset PROJECT execution.mode
```

Values: `IDE_MODEL | HYBRID | AUTO_ROUTER` (case-insensitive; normalized
to upper case).

## Delegation CLI (HYBRID)

```powershell
python scripts\router.py delegate --analysis-json "{\"task_type\":\"NEW_FEATURE\",\"risk\":\"MEDIUM\"}" --task "add feature"
```

The decision is recorded in `.context/router.db` (`routing_history`) with
`executed_by = "ide_model"`.
