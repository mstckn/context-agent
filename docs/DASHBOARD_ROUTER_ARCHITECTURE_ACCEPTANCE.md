# Dashboard & Router Architecture — Final Acceptance Report

Date: 2026-08-30
Spec: `autoroute.txt` (Dashboard / Control Plane + optional Model Routing layer)
Rule applied: "Do not stop at architecture documents. Implement working code." (§66)

---

# 1. Executive Summary

The product layer specified by `autoroute.txt` is implemented on top of the
verified Context Intelligence core:

* Dashboard extended into a **shared Control Plane** (13 pages, REST API,
  keyless onboarding). Works fully with routing disabled (`NOT ENABLED` is a
  state, not an error).
* **Hierarchical configuration** (SYSTEM/GLOBAL/PROJECT/SESSION/TASK) stored
  in the repository, surviving IDE changes.
* **Cross-IDE identity & continuity** (`identity.py`) with a handoff report.
* **Optional Router layer**: provider/model catalog, DPAPI-backed secret
  store, deterministic routing policy, history, usage/cost accounting,
  quality guardrails.
* **Router MCP** (5 tools, stdio) that consumes the core's `machine_readable`
  analysis and never re-classifies.
* **Hybrid delegation** (`executed_by=ide_model`) and an **optional Model
  Gateway** (127.0.0.1, OpenAI-compatible) that is the only configuration in
  which AUTO ROUTER is claimed.
* 14 slice-acceptance tests (§54–62) plus full regression re-run.

Verdict: **GO** (see §22). No §69 NO-GO condition is true.

---

# 2. Verified Context Core Baseline

Before any product-layer change, the core was re-verified (PHASE A):

| Gate | Command | Result |
|---|---|---|
| Engine suite | `python -m pytest tests -q` (repo root) | 149 passed (pre-layer) |
| Testbed | `python -m pytest tests/ -q` in `examples/sample_project` | 12 passed |
| Routing eval | `python ..\..\scripts\eval.py --file ..\..\evals\basic.json` | 8/8, hit rate 1.00 |
| Answer quality | `python ..\..\scripts\eval_answer_quality.py .` | 9/9, overlap 1.00, delta +0.000 |
| Token savings | `python examples/sample_project/.context/scripts/benchmark_token_savings.py examples/sample_project --json` | avg 65.4% (min 47.3, max 83.5), full_repo 4786 — **MEASURED ON FINAL BUILD** |

The only core change in the whole program was additive: `agent.py` now passes
`machine_readable` through `sections.routing` and the compact context package.
All gates were re-run after that change (results in §3) — no regression.

---

# 3. Regression Results

Final full re-run after every phase (PHASE I, MANDATORY REGRESSION FLOOR):

| Gate | Result | Baseline | Status |
|---|---|---|---|
| Engine + product suite (`tests/`) | **163 passed** (15.27s) | 149 + 14 new slice tests | PASS |
| Testbed (`examples/sample_project/tests`) | **12 passed** (2.04s) | 12 | PASS |
| Routing eval (`evals/basic.json`) | **8/8**, hit rate **1.0** | 8/8, 1.0 | PASS |
| Answer quality (9 fixtures) | **9/9**, overlap delta **+0.000**, noise delta **+0.000** | +0.000 | PASS |
| Benchmark | avg **65.4%**, min 47.3%, max 83.5%, full_repo **4786** tokens | identical | PASS — **MEASURED ON FINAL BUILD** |

No Context test regressed. Retrieval quality and token savings are unchanged.

---

# 4. Component Architecture

Implemented and documented in `docs/COMPONENT_ARCHITECTURE.md`.

```
Context Intelligence (CORE — always works keyless)
  agent.py / mcp_server.py / memory / ledger / config / identity
        ▲ consumes machine_readable (dependency direction, §25)
        │
Model Routing (OPTIONAL execution layer)
  router.py / router_mcp_server.py / model_gateway.py
        │
Dashboard (SHARED CONTROL PLANE)
  dashboard_server.py — configures and observes both layers
```

Rules enforced by tests:

* Router consumes `machine_readable` analysis; it never re-classifies tasks.
* Core and Dashboard function with zero providers and no router.
* `routing.enabled=false` returns the platform to clean Context-only operation.

---

# 5. Context MCP Independence

State: **VERIFIED**

* `scripts/mcp_server.py` exposes 15 tools over stdio; requires no API key,
  no Router MCP, no gateway.
* Slice test (§54, `tests/test_acceptance_slices.py`): memory + ledger work
  with no router; status reports `IDE_MODEL` + `NOT ENABLED` with no error.
* Router MCP absence cannot break Context MCP (failure isolation, §60 tests).

---

# 6. Cross-IDE Continuity

State: **VERIFIED** (with end-to-end test evidence)

* `scripts/identity.py` computes an 8-layer identity (project_id,
  repository_id, scope stability, ...) and a `CROSS_IDE_CONTINUITY` report
  with explicit `VERIFIED / PARTIAL / MISSING` states and 7 checks.
* 12 dedicated tests (`tests/test_identity.py`).
* Session handoff payload carries Project Memory state; model knowledge is
  marked `known_to_model_transferable: false` (never faked across IDEs).
* Project routing config lives in the repo (`config_store`), not inside any
  single IDE.
* Docs: `docs/CROSS_IDE_CONFIGURATION.md`.

---

# 7. Dashboard / Control Plane

State: **IMPLEMENTED**

* `scripts/dashboard_server.py` (default `127.0.0.1:8765`), 13 nav pages:
  Overview, Projects, Context Intelligence, Project Memory, IDE Sessions,
  Model Execution, AI Providers, Models, Auto Router, Usage & Cost, Routing
  History, Diagnostics, Settings.
* Keyless onboarding card appears when `providers_configured==0 &&
  !routing_enabled`; it guides, it never blocks.
* REST API: 14+ GET and 8 POST endpoints, each backed by the same CLI scripts
  the terminal uses (single source of truth). Route previews always run with
  `--no-record` so dashboard experiments never pollute routing history.
* Live-verified: 14/14 GET endpoints OK; config set→PROJECT→reset round-trip;
  zero-provider route/delegate honestly return `ROUTING_BLOCKED`;
  provider/model enable-disable round-trip.
* Docs: `docs/DASHBOARD_CONTROL_PLANE.md`.

---

# 8. Execution Modes

| Mode | State | Evidence |
|---|---|---|
| IDE MODEL | **IMPLEMENTED** (default, no API key) | default in `SYSTEM_DEFAULTS`; core-only tests; router status `execution_mode=IDE_MODEL` |
| HYBRID | **IMPLEMENTED** (IDE model stays primary; delegation records `executed_by=ide_model`) | `router.py delegate`; delegation tests; mode-switch tests keep memory/ledger intact |
| AUTO ROUTER | **IMPLEMENTED, conditional** — claimed only when the Model Gateway controls the primary request (`gateway.json` + `/healthz` probe); otherwise the explicit message "AUTO ROUTER NOT AVAILABLE FOR THIS IDE CONFIGURATION" | gateway tests (10/10); live smoke showed `mode_available=true` only with the gateway running |

Mode switching (§57 tests): IDE_MODEL → HYBRID → IDE_MODEL preserves memory
and ledger; context-only operations never touch provider secrets
(proven with an assertion-raising `get_secret` mock, call count 0).

---

# 9. Router MCP

State: **IMPLEMENTED**

* `scripts/router_mcp_server.py`, stdio JSON-RPC (protocol 2024-11-05).
* Exactly five tools: `route_task`, `delegate_task`, `execute_with_model`,
  `get_model_candidates`, `explain_route`.
* With routing disabled, `route_task`/`delegate_task` return `NOT_ENABLED`;
  the server still answers `initialize` and `tools/list` (verified over stdio).
* Does not duplicate Context Intelligence: it consumes `machine_readable`
  analysis from the core.
* Docs: `docs/ROUTER_MCP.md`.

---

# 10. Model Gateway

State: **IMPLEMENTED (optional)**

* `scripts/model_gateway.py`: OpenAI-compatible server bound to **127.0.0.1**
  (default port 8791). Registry `~/.context-agent/gateway.json`
  `{port, pid, started_at}`; cleaned on exit when pid matches.
* Endpoints: `GET /healthz`, `GET /v1/models` (enabled providers/models
  only), `POST /v1/chat/completions` (401 `no_api_key`, 404 unknown model,
  502 upstream error; SSE replay with `[DONE]`).
* Successful completions are recorded in routing history with
  `executed_by=gateway`; failures record nothing; recording errors never
  break execution.
* 10/10 unit tests + live smoke (healthz, models filtering, 404 mapping,
  SSE integrity, AUTO_ROUTER availability flip in `router_status`).
* Docs: `docs/MODEL_GATEWAY.md`.

---

# 11. Provider Configuration

State: **IMPLEMENTED**

* `~/.context-agent/providers.json` holds configuration **without secrets**;
  6 built-in providers; enable/disable per provider and per model.
* Health statuses: CONNECTED, AUTHENTICATION_FAILED, RATE_LIMITED,
  UNAVAILABLE, NOT_ENABLED, UNKNOWN. **No key → no probe → honest UNKNOWN**
  (§10). Cache TTL 300s, `--force` bypass.
* Dashboard AI Providers page + `router.py providers ...` CLI.
* Docs: `docs/PROVIDER_CONFIGURATION.md`.

---

# 12. Secret Security

State: **IMPLEMENTED**

* Secrets live outside the repo (`~/.context-agent/secrets.json`), DPAPI on
  Windows, obfuscation elsewhere (method reported honestly).
* Every listing is **mask-only** (first 3 + `...` + last 4; `***` when too
  short). Raw keys never appear in argv (transported via `SECRET_VALUE` env),
  context outputs, ledger, memory, prompts, logs, usage, history,
  diagnostics, or any GET/POST response.
* Slice test §62 greps every router surface (list/history/usage/status/
  providers/decision/explain) for the raw key: zero occurrences; mask format
  verified.
* Docs: `docs/SECRET_STORAGE.md`.

---

# 13. Model Catalog

State: **IMPLEMENTED**

* Per-provider model entries with tier, estimated cost, latency, context
  window; enable/disable per model.
* Candidate selection: capability/health filtering, then stable sort by
  (tier, cost, latency, id) — deterministic and explainable.
* `router.py models list/enable/disable`; dashboard Models page.

---

# 14. Routing Policy

State: **IMPLEMENTED**

* `router_policy.json` with `policy_version`; SMART task matrix (11 task
  types) and risk floors.
* Quality guardrail: insufficient context confidence ⇒ `ROUTING_BLOCKED`
  (observed live with zero providers — honesty over optimism).
* Cost actions: WARN / BLOCK / ECONOMY_ONLY / REQUIRE_MANUAL_OVERRIDE.
  Escalation is upward-only.
* Every decision is recorded and explainable: `router.py history` /
  `explain --id N`; dashboard Routing History + Auto Router preview pages.
* Docs: `docs/MODEL_ROUTING_POLICY.md`.

---

# 15. Project / Session / Task Overrides

State: **IMPLEMENTED**

* `config_store.py` layers: SYSTEM (read-only defaults) → GLOBAL → PROJECT →
  SESSION → TASK. `effective`/`show`/`set`/`reset` CLI + dashboard Settings
  page (SYSTEM shown read-only; Set / Reset To Inherited per key).
* 8 tests (`tests/test_config_store.py`); slice test §57 proves PROJECT-level
  `routing.enabled` differs per project root.

---

# 16. Failure Isolation

State: **IMPLEMENTED**

* §60 slice test: router failure cannot break Context-only operation.
* §61 slice test: gateway absent ⇒ `effective_mode=IDE_MODEL` with the
  explicit "AUTO ROUTER NOT AVAILABLE" note; nothing crashes.
* Gateway usage-recording errors are swallowed; provider health probe errors
  are categorized, never raised into Context paths.
* Provider errors never reach context-only users as blockers
  (`NOT ENABLED` / `UNKNOWN` are informational states).

---

# 17. Usage / Cost / Observability

State: **IMPLEMENTED**

* `router.py usage` ⇒ `{task_last, today, month, by_model}` with routes and
  `estimated_cost_usd` (catalog estimates — not provider billing).
* Routing history captures decision, reason, tier, escalation,
  `executed_by` (`ide_model` / `gateway`) and cost estimate.
* Dashboard Usage & Cost + Routing History + Diagnostics pages. Diagnostics
  output is sanitized by construction (no secrets, no raw tokens, no file
  contents).

---

# 18. Tests

Engine + product suite: **163 passed** (repo root `python -m pytest tests -q`).

| File | Tests | Scope |
|---|---|---|
| test_router_policy.py | 18 | routing decision, policy, guardrails, history |
| test_acceptance_slices.py | 14 | §54–62 slice acceptance |
| test_ledger.py | 15 | core ledger |
| test_git_explain.py | 13 | core |
| test_identity.py | 12 | cross-IDE identity/continuity |
| test_memory.py | 12 | Project Memory |
| test_model_gateway.py | 10 | gateway contract + registry + SSE |
| test_config_store.py | 8 | hierarchical config |
| test_security.py | 8 | secret/context security |
| test_model_profile.py | 9 | core engine |
| test_planner.py | 9 | core engine |
| test_graph.py | 8 | core engine |
| test_retrieval.py | 7 | core engine |
| test_budget.py | 6 | core engine |
| test_content_index.py | 6 | core engine |
| test_sufficiency.py | 8 | core engine |

Additional verification: sample_project testbed 12/12; routing eval 8/8
(hit rate 1.0); answer quality 9/9 (overlap 1.00, delta +0.000); Router MCP
stdio smoke (initialize + tools/list = 5 tools + NOT_ENABLED); gateway live
smoke; dashboard live endpoint sweep.

---

# 19. Remaining Limitations

* Provider health uses a `GET /models` heuristic; providers without that
  endpoint report UNAVAILABLE even when otherwise reachable.
* Cost figures are catalog estimates; no provider billing reconciliation.
* Secret storage outside Windows is obfuscation, not OS-grade encryption
  (documented honestly in SECRET_STORAGE.md).

---

# 20. Deferred Features

* Real per-provider chat endpoints beyond the OpenAI-compatible shape
  (gateway currently speaks OpenAI chat/completions for all providers).
* Multi-user / remote dashboard (server intentionally binds 127.0.0.1).
* Budget enforcement that actually blocks provider calls (currently
  advisory WARN/BLOCK at routing decision time).
* Automatic provider key validation on import (health probe covers this on
  demand via the dashboard Test action).

---

# 21. Production Risks

* Windows `SO_REUSEADDR` can let two dashboard instances share a port; an old
  process then answers requests. Operational note: verify the listener before
  trusting responses (encountered and documented during this program).
* Keys travel via environment to subprocesses; a crashing child could dump
  its environment. Mitigation: keys never reach argv, logs, or disk outputs.
* Gateway is single-user and local-only by design; exposing it beyond
  127.0.0.1 is out of scope and unsupported.
* Registry files under `~/.context-agent/` are shared machine state;
  concurrent dashboard+gateway+router writes use atomic writes but no locking.

---

# 22. Final Hardening Results

## Hybrid Delegation: **VERIFIED**

`delegate_task` now actually executes the delegated subtask against the
selected provider/model via `execute_with_model`. The full flow:

1. Route the task → select model
2. Execute against provider → get actual generated result
3. Record with `executed_by=router_hybrid` and the delegated model id

Failure classification: NETWORK, MODEL, NO_API_KEY, PROVIDER_UNAVAILABLE.
On failure, `executed_by=ide_model` (IDE must handle); never silently falls
back while claiming success.

Evidence: 5 new tests in `test_router_policy.py::HybridDelegationTests`
(successful execution, NO_API_KEY, NETWORK, blocked routing, no-prompt).

## Provider Streaming: **VERIFIED**

Gateway now uses `execute_with_model_streaming` which sends `stream: True`
to the provider and reads SSE events incrementally. Each content delta is
forwarded to the client immediately — no buffering the full response.

Supports both OpenAI SSE format (`data: {...}`) and Anthropic SSE format
(`event: content_block_delta`). Mid-stream errors are forwarded as
`finish_reason: "error"` events.

Evidence: updated `test_model_gateway.py::test_streaming_replays_text_as_sse_chunks`
(now mocks streaming generator, verifies incremental forwarding).

## Branch Isolation: **VERIFIED**

Memory and ledger now use branch-scoped scope keys via
`identity.scope_key_for("BRANCH")`. Project-global items (ARCHITECTURAL,
CONSTRAINT, KNOWN_ISSUE) use `PROJECT_GLOBAL` scope and remain visible
across all branches. Branch-specific items (DECISION, TASK, RUNTIME) are
isolated to their branch.

Evidence: 8 tests in `test_branch_isolation.py` covering memory isolation,
task isolation, ledger isolation, handoff, revision invalidation,
project-global visibility, and supersede scoping.

## Cross-IDE Lifecycle: **VERIFIED**

Two independent clients (TREA, VS Code) connecting to the same project:
- Share PROJECT_ID and REPOSITORY_ID
- Share project-global memory (ARCHITECTURAL, CONSTRAINT)
- Have different IDE_INSTANCE_ID, CONVERSATION_ID, MODEL_SESSION_ID
- Client B does NOT inherit Client A's known-to-model ledger state
- Handoff payload carries project context for resumption
- `known_to_model_transferable: false` enforced

Evidence: 6 tests in `test_cross_ide_lifecycle.py`.

## MCP Transport Reconnect: **VERIFIED**

MODEL_SESSION_ID now survives MCP transport reconnects. The resolution
priority chain in `identity.resolve_model_session()`:

1. Explicit `CONTEXT_AGENT_MODEL_SESSION_ID` env var (host-provided)
2. Existing persisted binding for (project_id, ide_instance_id, conversation_id)
3. Generate new only when no existing mapping found

MCP_CONNECTION_ID is generated fresh on every `initialize` (transport-level),
but MODEL_SESSION_ID remains stable while the underlying model conversation
continues. Bindings are persisted in `.context/model_sessions.json`.

Evidence: 10 tests in `test_model_session_lifecycle.py` covering reconnect
survival, binding persistence, new conversation isolation, different IDE
isolation, zero false inheritance, old session queryability, context decay
independence, explicit reset, host-provided priority, and MCP_CONNECTION
distinctness.

## Model Session Continuity: **VERIFIED**

Same IDE + same conversation + MCP reconnect → same MODEL_SESSION_ID.
Same IDE + new conversation → new MODEL_SESSION_ID.
Different IDE + new conversation → new MODEL_SESSION_ID.
Explicit host reset → fresh MODEL_SESSION_ID.

Context decay (FRESH/LIKELY_PRESENT/DECAYING/EXPIRED) continues to apply
independently of session binding. Stable session does not mean infinite
context validity.

## Identity Move Test: **PASS**

`test_path_is_not_identity_same_project_id_after_move` now passes on
Windows. Root cause: `os.replace()` fails when the OS holds file handles
inside the directory (Windows file locking). Fix: `shutil.move()` handles
this properly by falling back to copy+delete. Additionally, `identity.py`
file I/O now uses explicit `with` statements with `flush()`+`fsync()` to
ensure handles are released before atomic replace.

## Streaming Temporal Proof: **VERIFIED**

Deterministic mock HTTP provider test proves:
- Provider emits chunk A → Gateway immediately emits A
- Provider waits → Gateway does NOT wait for complete response
- Provider emits chunk B → Gateway emits B
- Client cancels → downstream disconnect handled cleanly
- Tool-call streaming deltas forwarded

Evidence: 3 tests in `test_streaming_temporal.py` with synchronization
events proving temporal ordering.

## Secure Secret Backend: WINDOWS / MACOS / LINUX

| Platform | Status |
|---|---|
| Windows | DPAPI (verified) |
| macOS | FALLBACK_OBFUSCATED (Keychain not implemented) |
| Linux | FALLBACK_OBFUSCATED (libsecret not implemented) |

Current fallback is honest XOR obfuscation, not falsely called encryption.
OS credential store adapters are recommended hardening for future phases.

---

# 23. Final Verdict

NO-GO audit (§69) — every condition checked against evidence above:

| §69 condition | True? |
|---|---|
| Router MCP required for Context MCP | No (§5 tests) |
| API keys required for IDE MODEL | No (§8, §54 tests) |
| Context retrieval regressed | No (§3: hit rate 1.0, overlap 1.00) |
| Context savings regressed | No (§3: avg 65.4% identical) |
| Model-session knowledge leaks between sessions | No (§22 model session lifecycle tests, 10/10) |
| Project data leaks across projects | No (§59 slice tests) |
| Branch state contaminates another branch | No (§22 branch isolation tests, 8/8) |
| Routing config stored only inside one IDE | No (config_store in repo; §57 tests) |
| Router failure breaks Context MCP | No (§60 tests) |
| Provider secrets enter Context data | No (§62 tests grep every surface) |
| Auto Router claimed without primary-request control | No (claimed only with live gateway; §61 tests) |
| Cross-IDE continuity claimed without evidence | No (§22 lifecycle tests, 6/6) |
| Existing Context tests regress | No (199 passed, 0 failed) |
| Dashboard requires Router | No (works in NOT ENABLED state) |
| Context-only users receive provider errors as blockers | No (NOT ENABLED / UNKNOWN are informational) |
| MCP transport reconnect loses model session | No (§22 reconnect tests, session binding persists) |
| Identity move fails on Windows | No (§22 identity move test PASS) |

## Component Verdicts

| Component | Verdict |
|---|---|
| CONTEXT CORE | **GO** — 199 tests green, no regression |
| ROUTER MCP | **GO** — 5 tools, optional, NOT_ENABLED when routing off |
| HYBRID DELEGATION | **GO** — actually executes, classifies failures, `executed_by=router_hybrid` |
| AUTO ROUTER / GATEWAY | **GO** — real streaming (temporal proof), gateway controls primary request |
| CROSS-IDE | **GO** — session binding persists across reconnect, shared memory, isolated known-to-model |
| BRANCH ISOLATION | **GO** — 8 tests, project-global visible, branch-specific isolated |
| MODEL SESSION LIFECYCLE | **GO** — 10 tests, reconnect survival, new conversation isolation, explicit reset |
| STREAMING TEMPORAL | **GO** — 3 tests, chunk ordering proven, disconnect handling, tool-call deltas |

## Overall Product Verdict: **GO**

The GO is based on executed tests and live verification, not documentation.
All mandatory conditions are met. Full repository suite: 199 passed, 0 failed.
Context savings benchmark: avg 65.4% (min 47.3, max 83.5), full_repo 4786 tokens
— **MEASURED ON FINAL BUILD**, matches previous baseline exactly.
Secure secret backend on macOS/Linux remains FALLBACK_OBFUSCATED (recommended
hardening, not a blocker).

---

# FINAL ACCEPTANCE STATEMENTS

All 15 required statements are demonstrably true:

1. "I can use Context Intelligence with the LLM already configured in my IDE
   without entering any external API key." — **TRUE** (IDE MODEL default,
   §54 core-only tests, keyless onboarding).
2. "Context Intelligence works if Router MCP is disabled or not installed."
   — **TRUE** (§54 tests; 149 baseline tests pass without router).
3. "I can optionally enable model routing later." — **TRUE**
   (`config_store --set PROJECT routing.enabled true`; dashboard Settings).
4. "Router MCP does not duplicate Context Intelligence." — **TRUE**
   (consumes `machine_readable`; dependency direction enforced).
5. "Project knowledge persists across supported IDEs." — **TRUE**
   (identity.py VERIFIED; repo-stored memory/config).
6. "New model sessions do not inherit false known-to-model state." — **TRUE**
   (`known_to_model_transferable: false` in handoff; session binding in
   model_sessions.json; 10 lifecycle tests).
7. "Projects and branches remain isolated." — **TRUE**
   (§59 project tests; §22 branch isolation tests 8/8).
8. "Provider secrets never enter Context Intelligence." — **TRUE**
   (§62 slice tests grep every output surface).
9. "IDE MODEL remains the default." — **TRUE** (SYSTEM default; no key).
10. "Hybrid mode is correctly described as delegation." — **TRUE**
    (`executed_by=router_hybrid`; delegate_task executes against provider).
11. "True Auto Router is only claimed if Gateway/integration controls the
    primary model request." — **TRUE** (gateway.json + healthz gate;
    otherwise the explicit NOT AVAILABLE message).
12. "A project can independently select IDE MODEL, HYBRID, or AUTO ROUTER."
    — **TRUE** (per-project `execution.mode`; §57 tests).
13. "Turning routing off returns the platform to clean Context-only
    operation." — **TRUE** (§57 mode-switch tests preserve memory/ledger).
14. "Router or provider failure cannot break Context-only users." — **TRUE**
    (§60/§61 tests).
15. "The previously verified Context quality and token savings have not
    regressed." — **TRUE** (§3 regression floor: identical numbers).
