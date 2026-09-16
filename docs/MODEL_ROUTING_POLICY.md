# Model Routing Policy

The router (`scripts/router.py`) is a deterministic, ML-free policy
engine. It CONSUMES Context Intelligence's machine-readable task analysis
(`task_type`, `complexity`, `risk`, `reasoning_requirement`,
`context_confidence`, `safe_to_implement`) and never re-classifies tasks.
Goal: lowest expected cost to a correct, verified result — not the
cheapest model. The router is optional and isolated; the Context core
never imports it.

## Policy storage

`<root>/.context/router_policy.json`. Every `save_policy()` increments
`policy_version` and stamps `updated_at`; recorded history rows carry the
version they were decided under.

Policy structure:

```json
{
  "policy_version": 1,
  "mode": "SMART",
  "matrix": { "...": {"preferred": "ECONOMY", "fallback": "BALANCED"} },
  "risk_floor": {"LOW": "ECONOMY", "MEDIUM": "BALANCED", "HIGH": "STRONG", "CRITICAL": "FRONTIER"},
  "escalation_enabled": true,
  "escalation_chain": ["ECONOMY", "BALANCED", "STRONG", "FRONTIER"],
  "guardrails": {
    "cost": {"action": "WARN", "task_limit_usd": 1.0, "daily_limit_usd": 5.0, "monthly_limit_usd": 50.0},
    "quality": {"enforce": true}
  }
}
```

Tiers, in ascending order: `ECONOMY < BALANCED < STRONG < FRONTIER`.

## SMART default matrix

| task_type | preferred | fallback |
|---|---|---|
| QUICK_EDIT | ECONOMY | BALANCED |
| FIX | ECONOMY | BALANCED |
| TEST_GENERATION | ECONOMY | BALANCED |
| DOCUMENTATION | ECONOMY | BALANCED |
| REFACTOR | BALANCED | STRONG |
| NEW_FEATURE | BALANCED | STRONG |
| DATABASE_CHANGE | BALANCED | STRONG |
| DEBUGGING | BALANCED | STRONG |
| ARCHITECTURE | STRONG | FRONTIER |
| SECURITY | STRONG | FRONTIER |
| UNKNOWN | BALANCED | STRONG |

## Required tier

1. `preferred` comes from the matrix for the normalized `task_type`
   (unknown values map to `UNKNOWN`).
2. The risk floor (`LOW→ECONOMY`, `MEDIUM→BALANCED`, `HIGH→STRONG`,
   `CRITICAL→FRONTIER`) raises the tier when it is higher than `preferred`.
3. `reasoning_requirement` of `high` / `true` / `1` raises the tier to at
   least `STRONG`.
4. `complexity=HIGH|COMPLEX` raises the tier to `STRONG`; `CRITICAL`,
   `EXTREME`, or `VERY_HIGH` raises it to `FRONTIER`.

## Deterministic selection

Usable models (enabled model, enabled provider, health not
`AUTHENTICATION_FAILED`, secret present when required) at tier ≥ required
are sorted by: quality tier, explicit `routing_priority` (lower is
preferred), then `input_cost`, then latency
(`local` < `fast` < `medium` < `slow`), then `model_id`. The first entry
wins; identical inputs always produce identical decisions. Models without
an explicit priority sort after curated models, so alphabetical order never
acts as a quality judgment.
`estimated_cost_usd = (input_tokens * input_cost + output_tokens *
output_cost) / 1,000,000` (rounded to 6 decimals; projection assumes
4000 output tokens).

## Guardrails

Quality guardrail (`guardrails.quality.enforce`, default true): if no
sufficient model exists, the decision is `ROUTING_BLOCKED` with
`reason: "quality_guardrail"`. A weaker model is never presented as
enough.

Cost guardrails (`guardrails.cost`) compare the projected task cost and
the accumulated daily/monthly spend (from `router.db`) against
`task_limit_usd` / `daily_limit_usd` / `monthly_limit_usd`. On breach,
`action` applies:

| Action | Behavior |
|---|---|
| `WARN` | routing proceeds; reasons get `cost guardrail WARN: ...` |
| `BLOCK` | `ROUTING_BLOCKED`, `reason: "cost_guardrail"` |
| `ECONOMY_ONLY` | candidates restricted to ECONOMY; blocked if none configured |
| `REQUIRE_MANUAL_OVERRIDE` | `MANUAL_OVERRIDE_REQUIRED` unless the analysis sets `manual_override` |

## Escalation

Chain `ECONOMY -> BALANCED -> STRONG -> FRONTIER`, upward only (every
listed tier is strictly above the chosen tier). Escalation is a response
to quality failures only — never to network failures, package install
failures, broken test runners, or environment misconfiguration.

## CLI

```powershell
python scripts\router.py policy show
python scripts\router.py policy mode SMART|CUSTOM          # rejected otherwise
python scripts\router.py policy custom-set "{\"matrix\": {...}}"
python scripts\router.py route --analysis-json "{\"task_type\":\"SECURITY\",\"risk\":\"HIGH\"}" --task "..." --context-tokens 5000 [--no-record]
python scripts\router.py delegate --analysis-json "..." --task "..."   # HYBRID; executed_by=ide_model
python scripts\router.py history --limit 50
python scripts\router.py explain --id 12
python scripts\router.py usage
```

`custom-set` parses the value as JSON, switches `mode` to `CUSTOM`, and
applies any of the keys `matrix`, `risk_floor`, `escalation_enabled`,
`escalation_chain`, `guardrails`.

Decision output fields include: `decision`
(`ROUTED` / `ROUTING_BLOCKED` / `MANUAL_OVERRIDE_REQUIRED`), `blocked`,
`required_tier`, `initial_model` / `final_model` / `escalated_to`,
`escalation_chain`, `provider_id`, `model`, `provider_health`,
`candidates_considered` (top 5), `estimated_cost_usd`, `executed_by`,
`reasons`, `policy_version`, `policy_mode`.

## History and usage accounting

Routes are recorded in `<root>/.context/router.db` (table
`routing_history`): task metadata, tiers, models, provider, decision,
reasons (JSON), `policy_version`, token counts, estimated cost, duration,
`executed_by`, `outcome`. `usage` summarizes `task_last` / `today` /
`month` (routes + estimated cost) and top-20 `by_model`.

## Relation to layered configuration

`router_status()` reads `execution.mode` (default `IDE_MODEL`) and
`routing.enabled` (default `false`) through the layered config store;
SYSTEM and PROJECT layers, `--set` / `--reset` / `--effective` are
documented in PROVIDER_CONFIGURATION.md. Cost limits and the guardrail
action live in the routing policy file itself; the SYSTEM defaults
`cost.*` in the config store mirror them.
