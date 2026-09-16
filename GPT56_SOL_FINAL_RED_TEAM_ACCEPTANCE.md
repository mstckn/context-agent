# GPT-5.6 Final Independent Red-Team Acceptance

Date: 2026-08-31 (Europe/Istanbul)

# 1. Executive Verdict

**GO**

The initially presented build was not releasable: independent reproduction found a broken fresh-install manifest, project-root path traversal, false Auto Router execution semantics, broken tool-call streaming, public MCP session-lifecycle gaps, concurrency races, and a severe 10k-file indexing path. Each verified release-impacting defect was repaired with bounded changes and regression coverage. The final release floor is green, the post-fix benchmark was re-measured, and no known critical correctness, isolation, session, secret, or context-quality defect remains.

# 2. Repository / Revision Tested

- Repository snapshot: `<project-root>`
- Fixture: `examples\sample_project`
- Revision: unavailable. The supplied directory is not a Git worktree (`git rev-parse` returns “not a git repository”), so no commit SHA can be truthfully reported.
- Tested artifact: the local filesystem snapshot after the fixes documented below, on Python 3.11 / pytest 7.4.3 / Windows.
- The final installer was run again against the sample project, so its installed `.context\scripts` copy matches the tested source scripts.

# 3. Architecture Independently Observed

The implementation matches the intended separation of concerns:

- Context Intelligence core: `agent.py`, `route.py`, `planner.py`, `sufficiency.py`, `index.py`, `content_index.py`, `graph.py`, `identity.py`, `memory.py`, `ledger.py`, and model-aware budgeting/profile modules.
- Public Context MCP surfaces: stdio server, launcher, HTTP/SSE/WebSocket transport, JSON-RPC tool discovery/calls, and lifecycle binding.
- Optional routing plane: `router.py`, Router MCP, provider/model catalog, policy/guardrails, and delegation execution.
- Primary execution plane: `model_gateway.py`; true Auto Router selection now happens before provider execution.
- Control plane: dashboard server reads/writes effective configuration and runtime state; routing decisions remain in the router/gateway.
- Persistence: project index/graph/chunks are SQLite-backed; identity, model-session bindings, project memory, ledger, routing history, usage and configuration have explicit scope boundaries.
- Secrets: provider credentials are stored outside project context; Windows uses the platform protection backend. Core IDE MODEL mode does not require a provider or key.
- Installation: `install.py` copies a declared runtime manifest and preserves idempotent/source-equals-destination behavior.

# 4. Baseline Reproduction

| Gate | Previous claimed result | Independently measured before fixes | Final measured result | Status |
|---|---:|---:|---:|---|
| Repository tests | 199 passed | 199 passed, 33.47 s | 222 passed, 32.03 s | PASS |
| Sample testbed | 12 passed | 12 passed | 12 passed, 2.04 s | PASS |
| Routing evaluation | 8/8, hit rate 1.0 | 8/8, 1.0 | 8/8, 1.0 | PASS |
| Answer-quality evaluation | 9/9, delta +0.000 | 9/9, +0.000 | 9/9, +0.000 | PASS/no regression |
| Average token savings | 65.4% | 65.4% | 67.1% | PASS |
| Minimum / maximum savings | 47.3% / 83.5% | 47.3% / 83.5% | 47.3% / 89.3% | PASS |
| 10k-file cold index | not claimed | timed out after 180 s | 17.72 s wall, 0 errors | PASS after repair |
| 10k unchanged re-index | not claimed | 5.99 s after first partial optimization | 4.69 s wall | PASS |

Baseline results were captured before production changes. The final results are fresh runs against the final code, not copied historical output.

# 5. Findings

| ID | Severity | Subsystem | Description / reproduction | Root cause | Fix | Regression test | Status |
|---|---|---|---|---|---|---|---|
| RT-001 | CRITICAL | Install | A genuinely fresh project received only 31 scripts and omitted core runtime modules such as identity, ledger, memory, planner and sufficiency. Existing fixture residue hid the failure. | Incomplete hard-coded install manifest. | Expanded the manifest to all 44 required core/optional runtime scripts. | `test_install_into_new_project` now asserts runtime modules. | FIXED |
| RT-002 | HIGH | Retrieval security | `get_range` accepted absolute/parent paths and could return a file outside the project. | Fallback path resolution had no root-containment check. | Resolve canonically, require `relative_to(project_root)`, and reject symlink escape as `path_outside_project`. | `test_get_range_rejects_parent_traversal` | FIXED |
| RT-003 | HIGH | MCP lifecycle | Public launcher reconnects bypassed model-session binding; reconnect/new-conversation behavior could collapse into a default session. | Launcher created transport state without `resolve_model_session`. | Separated transport and model-session IDs and bound conversation/IDE through the public initialize path. | Public launcher JSON-RPC reconnect/new-conversation coverage in model-session lifecycle suite. | FIXED |
| RT-004 | HIGH | HTTP MCP | HTTP/SSE child processes could share default ledger state; transport/model IDs were conflated and advertised directives/memory calls were not dispatched. | Session metadata was neither persisted per client nor propagated to child environments; tool dispatch was incomplete. | Added client bindings, conversation/IDE-aware resolution, `Mcp-Session-Id`, per-child environment and missing dispatch paths. | `test_http_transport_session_is_distinct_and_model_binding_is_conversation_aware` | FIXED |
| RT-005 | HIGH | Auto Router | Gateway proxied the requested model unchanged while product state could imply automatic primary routing. | Auto-routing policy was not on the gateway execution path. | Gateway now analyzes/routes before provider lookup, ignores the client-selected primary model in Auto mode, and reports `AUTO_ROUTED_EXECUTED`; health truthfully exposes primary routing. | `test_auto_router_selects_before_provider_execution`; Auto availability tests. | FIXED |
| RT-006 | HIGH | Streaming | Tool-call deltas became dictionary “content”, could fail string joining, and ended with `stop`. | Gateway normalized streams as text-only. | Preserve structured `delta.tool_calls`, join text only, and propagate `finish_reason=tool_calls`. | `test_gateway_preserves_tool_call_deltas_and_finish_reason`; `test_tool_call_streaming` | FIXED |
| RT-007 | MEDIUM | Streaming | A provider that ignored streaming and returned buffered JSON produced no useful stream. | Router assumed every success response was SSE. | Inspect content type and normalize buffered JSON text/tool calls as a fallback. | Provider/gateway streaming suite. | FIXED |
| RT-008 | HIGH | Provider security | A provider exception containing its API key could surface that raw key in an execution error. | Exception strings were not redacted at the adapter boundary. | Redact the active provider key in synchronous and streaming failure paths. | `test_execution_exception_redacts_provider_key` | FIXED |
| RT-009 | HIGH | Ledger concurrency | Two writers initializing the same session could hit a unique constraint; range merges could lose atomicity. | Check-then-insert and non-immediate transactions. | `ON CONFLICT DO NOTHING`, atomic turn increment and `BEGIN IMMEDIATE` around range merge. | `test_concurrent_session_init_and_range_merge` | FIXED |
| RT-010 | HIGH | Memory concurrency | Concurrent identical memory writes could throw a unique-constraint error. | Non-idempotent insert race. | `INSERT OR IGNORE`, then read/update the canonical record. | `test_concurrent_duplicate_add_is_idempotent` | FIXED |
| RT-011 | MEDIUM | Router | A tools-required task could select a configured tools-incapable model. | Model capability metadata existed but was not an eligibility gate. | Added provider-independent tools, vision, streaming, structured-output and context-window filtering with explicit block reasons. | `test_required_tools_never_routes_to_incapable_model` | FIXED |
| RT-012 | MEDIUM | Router | Invalid/cyclic escalation configuration could form repeated or downward fallback chains. | Raw configured tiers were consumed without normalization. | Bound, deduplicate, validate and upward-sort escalation tiers. | `test_invalid_or_cyclic_escalation_chain_is_bounded` | FIXED |
| RT-013 | MEDIUM | Model profiles | Public CLI/MCP schemas rejected an unknown/new IDE model although internal budgeting had a conservative fallback; output also overwrote the actual requested model with a recommendation. | Static public enum/argparse choices and mixed “executor” vs “recommendation” fields. | Accept arbitrary model IDs, apply conservative fallback, and keep the actual IDE model identity. | `test_public_agent_cli_accepts_unknown_model`; `test_mcp_schema_does_not_reject_unknown_model` | FIXED |
| RT-014 | MEDIUM | Context quality | The tiny border-radius task was classified as a broad feature and returned eight unrelated files; substring matches also classified “account profiles” as performance and “latest” as test-related. | Unbounded substring/stem rules, unrelated dirty-file boosts, and repository-wide test/dependency expansion. | Word-boundary signals, explicit local-edit/security signals, targeted dirty boosts, and local dependency/test sufficiency rules. | Planner, Git-explain and sufficiency regressions including `test_css_value_change_is_local_edit`. | FIXED |
| RT-015 | MEDIUM | Sufficiency | A generic jobs/deduplication task expanded to an unrelated API contract and could report misleading coverage. | Contract evidence was inferred repository-wide rather than from task/API scope. | Require task-area/API relevance; waive absent boundaries and keep missing high-risk evidence explicit. | `test_non_api_feature_does_not_expand_to_unrelated_contract` | FIXED |
| RT-016 | MEDIUM | Risk classification | A small authorization/admin change was not reliably treated as security-sensitive. | Security signals lost to generic feature/performance classification. | Security classification now wins and requires config/middleware/entry-point/secrets evidence; missing evidence remains insufficient. | `test_authorization_change_uses_security_evidence` | FIXED |
| RT-017 | MEDIUM | Large-file retrieval | A match near line 2100 returned only the file header/first 50 lines. | Preview path did not expand the query-matched symbol. | Retain the chunk map and fetch the matched symbol body/range. | `test_large_file_uses_query_matched_symbol_body_not_header_only` | FIXED |
| RT-018 | MEDIUM | Index coverage | Markdown, JSON/YAML/TOML/config, SQL migrations and related contracts were absent from the supported index set. | Indexer extension allowlist was code-only. | Added documentation/configuration/migration formats to supported content indexing. | `test_docs_config_and_sql_migrations_are_indexed_and_searchable` | FIXED |
| RT-019 | MEDIUM | Benchmark integrity | Savings compared one full-repository count against retrieval across five queries, making aggregate “saved tokens” zero/misleading. | Numerator/denominator represented different query counts. | Added `full_repo_tokens_across_queries`; aggregate savings now compares equal workloads. | `test_benchmark_aggregate_compares_equal_query_counts` | FIXED |
| RT-020 | LOW | Test quality | The “genuine copy failure” test contained only `pass` and could never detect swallowed errors. | Placeholder test presented as coverage. | Mock a real `PermissionError` and assert it propagates. | `test_genuine_copy_failure_not_swallowed` | FIXED |
| RT-021 | HIGH | Scalability | A 10k-file cold index exceeded 180 s. After removing per-file auxiliary rebuilds it still took 146.47 s. | Per-file schema commits/full-file scans (O(N²)), four reads per file, repeated missing-tokenizer imports, and serial Windows opens. | Bulk auxiliary transactions with pre-known graph paths, one file read, negative tokenizer-load cache, one directory traversal, and bounded 8-worker/32-file prefetch. | Focused index/graph/content tests plus real 10k-file cold/warm benchmark. | FIXED (17.72 s) |

# 6. Context Quality Assessment

All adversarial slices were exercised against code or realistic fixtures. The tiny CSS edit now produces a LOCAL_EDIT package centered on `frontend/components/Button.tsx`, without repository-wide expansion. Cross-file feature/API/DB cases retrieve implementation, consumers, schemas/migrations and tests. Security work expands risk evidence and remains explicitly insufficient if security dependencies are missing. Semantic “prevent jobs from being executed twice” retrieval finds jobs/queue, lease/idempotency/dedup logic and relevant tests without inventing an unrelated API boundary. Stack-trace routing, malicious repository text and document/config/migration retrieval remain covered by the final suite.

The large-file case now selects the matched symbol around the relevant late-file location rather than returning only a header preview. No meaningful quality regression appeared in the 8 routing fixtures or 9 answer-quality fixtures.

# 7. Token Efficiency Assessment

The ledger avoids resending unchanged content, detects changed hashes, represents partial ranges, and applies FRESH/LIKELY_PRESENT/DECAYING/EXPIRED retention. The final five-query benchmark saved 16,048 of 23,930 comparable full-context tokens (67.1%). The local-edit and sufficiency repairs also remove observed unnecessary file expansion. Budgeting remains model-window-aware, reserves system/tool/user/output/reasoning capacity, prioritizes contracts under pressure, and now safely accepts unknown model IDs using conservative defaults.

# 8. Context Ledger / Reconnect Assessment

Unchanged content is skipped, modified hashes refresh it, partial ranges merge, representation upgrades work, expired items can be resent, and explicit reset clears session counters. Reconnect binding is based on project + IDE + real conversation, not the transport connection. Same conversation/reconnect reuses the model session; a new conversation, another IDE or reset receives a fresh model session. Concurrent initialization/range writes are now atomic.

# 9. Cross-IDE Assessment

Separate clients can recover project-scoped architecture, decisions, constraints, work state and validation status through handoff/project memory. Model knowledge is intentionally not transferred: the second IDE gets a new model session and an empty known-to-model ledger. Public launcher and HTTP lifecycle paths, not only internal DB calls, now validate the distinction.

# 10. Branch / Worktree Isolation

Branch-specific tasks, decisions, implementation state and ledger entries are scoped by branch/revision; project-global constraints remain visible where intended. The final suite covers main/feature branch separation, branch switches, revision changes, supersession and handoff. Checkout IDs distinguish worktrees while repository/project identity preserves the intended relationship. No branch contamination was found.

# 11. Project Memory Assessment

PROJECT, BRANCH, TASK and SESSION scopes are distinct. Decisions can be superseded, states can become STALE/INVALIDATED, file-backed facts are revalidated, and active state sheets prioritize observable facts/results rather than hidden reasoning. “Do not use Redis” supersession semantics do not resurrect the old active decision. Concurrent duplicate adds are idempotent.

# 12. Sufficiency / Progressive Expansion

The engine reports required, satisfied and missing evidence plus expansion candidates/history. API consumers, tests, schemas, graph relations, runtime evidence and security evidence are expanded when relevant. It does not silently mark the authorization fixture safe when middleware/config/secrets evidence is absent, and it does not burn budget on unrelated repository tests/contracts for a leaf edit. Budget exhaustion remains explicit rather than silently lowering correctness requirements.

# 13. Router MCP Assessment

Router MCP is optional. With no router, provider or API key, Context MCP, retrieval, memory, ledger, dashboard Context health and IDE MODEL mode remain usable. Decisions are deterministic, risk/reasoning floors and capability/context limits filter eligibility, cost/quality guards can return ROUTING BLOCKED, and invalid escalation graphs are bounded. Router status does not fake successful execution.

# 14. Hybrid Delegation Assessment

`delegate_task` routes and actually invokes the selected provider/model. Telemetry distinguishes the IDE primary executor from `delegated_executor` and records `router_hybrid`. Success, no-key, provider/network failure, guardrail block, no-prompt and fallback behavior are covered. A recommendation without execution is not reported as delegated success.

# 15. Model Gateway / Auto Router Assessment

In AUTO ROUTER mode, the gateway now controls the primary request: context analysis and router selection happen before provider execution, and the client-supplied model does not execute first. Health/availability requires a live gateway that advertises primary routing plus enabled routing and a usable model. A bare gateway or Router MCP alone no longer enables/falsely labels Auto Router.

# 16. Streaming / Tool Calls

The temporal test proves provider chunk A reaches the gateway client while provider B is still blocked, then releases B. Disconnect handling, mid-path failure behavior, normal finish, buffered JSON fallback, structured tool name/argument deltas, and `finish_reason=tool_calls` are preserved. This is real incremental forwarding, not buffered replay.

# 17. Provider Security

Raw provider keys are absent from context packages, memory, ledger, routing history, usage, status/provider listings, explain output, diagnostics and exception results. Explicit exception-echo testing now redacts the key. Malicious repository text is marked untrusted data and cannot obtain the secret. On Windows the intended protected backend is used. The macOS/Linux fallback is obfuscation, not secure encryption; this is documented as a non-blocking hardening limitation.

# 18. Dashboard / Configuration Truthfulness

The dashboard remains a control plane over shared runtime configuration/state. Layer precedence is SYSTEM → GLOBAL → PROJECT → SESSION → TASK; project/session/task overrides do not mutate broader scopes or leak to another project. Runtime health distinguishes optional-disabled from failed, Gateway unavailable from active, and IDE MODEL/HYBRID/AUTO ROUTER based on actual execution capability.

# 19. Failure Isolation

Context-only operation imports and runs without Router MCP or provider secrets. Router/Gateway/provider unavailability degrades optional routing/delegation and returns classified errors/blocks without breaking context retrieval, memory or ledger. Semantic/graph expansions are guarded so their failure does not make the core server unavailable. No optional subsystem was found to be a hidden core dependency.

# 20. Test Quality Review

Critical coverage was challenged for false mocks and non-failing tests. The copy-failure placeholder was replaced; Auto Router now asserts the actual provider model executed; tool streaming asserts structured deltas; temporal streaming uses synchronization rather than timestamp coincidence; lifecycle tests use public JSON-RPC/HTTP boundaries; concurrent memory/ledger tests use real writers. The final repository contains 222 passing tests, 23 more than the supplied baseline.

# 21. Scalability / Performance Risks

The original full index path performed per-file schema commits, repeatedly scanned the files table and later rebuilt the same auxiliary indexes. A real 10,000-file fixture exceeded 180 s. The repaired path completed cold indexing in 17.72 s (10,000 indexed, 0 errors, 20,000 chunk rows, 10,000 graph metadata rows, 10.86 MiB DB) and an unchanged run in 4.69 s. Prefetch is bounded to 32 file payloads, so the speedup does not require unbounded repository contents in memory.

Remaining scaling concerns are non-blocking: `map.json` still grows with the indexed file list, SQLite remains a single-host design, and this pass did not target million-file monorepos (explicitly out of scope).

# 22. Remaining Limitations

- The answer-quality fixture is small and saturated: both baseline and Context Agent score 1.00 overlap / 0.00 noise, so it proves no regression but not superiority on difficult repositories.
- The standard token fixture is only 30 benchmarked source files (31 records in the index). The separate 10k-file run supplements scalability evidence but is not an answer-quality corpus.
- No paid/live third-party provider was called; provider behavior was verified with deterministic local mocks and actual gateway/provider adapter boundaries.
- macOS/Linux secret fallback is obfuscation rather than OS credential-vault encryption.
- No Git revision can be pinned because the delivered root is not a Git worktree.

These are production-hardening/measurement limitations, not known violations of the strict release gates.

# 23. Final Regression Results

| Command | Working directory | Result | Duration |
|---|---|---:|---:|
| `python -m pytest tests -q` | repository root | 222 passed | 32.03 s |
| Critical release-floor subset (branch, cross-IDE, model session, HTTP MCP, router, gateway, streaming, install, security, ledger, memory) | repository root | 110 passed | 26.73 s |
| `python ..\..\scripts\install.py .` | `examples\sample_project` | 44 scripts installed; no error | included in 3.0 s combined run |
| `python -m pytest tests -q` | `examples\sample_project` | 12 passed | 2.04 s |
| `python .context\scripts\eval.py` | `examples\sample_project` | 8/8, hit rate 1.0 | PASS |
| `python .context\scripts\eval_answer_quality.py .` | `examples\sample_project` | 9/9, average delta +0.000 | PASS |
| `python .context\scripts\benchmark_token_savings.py . --json` | `examples\sample_project` | 5 queries, 67.1% average | 1.64 s reported |
| Generated 10k-file cold + unchanged run | isolated temporary directory | 10,000/10,000, 0 errors; 17.72 s / 4.69 s | 22.41 s measured runs |

No final command has an unexplained test failure. The last scale harness printed its valid metrics but initially exited non-zero only because its own temporary SQLite inspection handle was still open during cleanup; after the handle closed, the verified temporary directory was removed. Product indexing itself returned exit 0 in both runs.

# 24. Final Benchmark

Fixture: `<project-root>\examples\sample_project`

Command: `python .context\scripts\benchmark_token_savings.py . --json`

| Metric | Final value |
|---|---:|
| Files benchmarked | 30 |
| Index records / symbols | 31 / 150 |
| Queries | 5 |
| Full repository tokens (one copy) | 4,786 |
| Comparable full tokens across 5 queries | 23,930 |
| Retrieved tokens | 7,882 |
| Saved tokens | 16,048 |
| Average savings | 67.1% |
| Minimum savings | 47.3% |
| Maximum savings | 89.3% |
| Index total token estimate | 5,340 |
| Benchmark elapsed | 1.64 s |

Per-query retrieved totals were 1,732; 2,524; 2,324; 512; and 790 tokens. Aggregate savings now compares five full-repository copies with five retrieval queries.

# 25. Final Verdict

**GO.** All strict verdict conditions are met on the supplied Windows artifact: no known critical issue remains; project/branch/model-session isolation passes; reconnect preserves only valid ledger knowledge; Context-only mode is keyless and independent; Hybrid executes; Auto Router is truthful and gateway-controlled; streaming is temporal and tool-aware; secrets are isolated; all tests are green; and context quality/benchmark gates were re-measured after the final fixes.

# Final Scorecard

| Dimension | Score / 10 | Rationale |
|---|---:|---|
| Context Efficiency | 9 | 67.1% measured average savings and narrow local-edit behavior. |
| Context Accuracy | 9 | Risk and task classification defects repaired; adversarial cases pass. |
| Retrieval Quality | 9 | Symbol/body, semantic, docs/config/migration and runtime evidence covered. |
| Dependency Awareness | 9 | Structural graph plus scoped contracts/consumers/tests. |
| Context Sufficiency | 9 | Explicit evidence/missing/expansion and no unsafe silent downgrade. |
| Project Awareness | 9 | Stable project/repository/checkout identity and handoff state. |
| Long-Term Memory | 9 | Scoped, supersedable, stale-aware, concurrency-safe observable memory. |
| Cross-Turn Continuity | 9 | Hash/range/decay-aware ledger and reconnect invariants pass. |
| Cross-IDE Continuity | 9 | Project memory transfers while model knowledge remains isolated. |
| Branch Isolation | 9 | Branch/revision/task/ledger separation passes. |
| Token Budgeting | 9 | Model-aware reserves, contract priority and conservative unknown fallback. |
| Quality Preservation | 8 | No regression, but the 9-case quality fixture is small and saturated. |
| Router Correctness | 9 | Deterministic eligibility, risk, capability, cost and bounded fallback. |
| Hybrid Delegation | 9 | Actual selected-model execution and honest telemetry/failures. |
| Gateway / Auto Router | 9 | Selection is now before primary provider execution and health is truthful. |
| Streaming | 9 | Temporal delivery, cancellation, buffered fallback and tool calls covered. |
| Security | 8 | Windows path/key/prompt-injection gates pass; non-Windows vault fallback remains obfuscation. |
| Observability | 8 | Runtime/usage/routing/context state is exposed truthfully; no browser-level end-to-end UI run was performed. |
| Testability | 9 | 222 core + 12 testbed, public-boundary, temporal and concurrency coverage. |
| Scalability | 8 | 10k cold index improved to 17.72 s; map growth and very large monorepos remain unproven. |
| Production Readiness | 9 | Strict release floor is green with only explicitly non-blocking limitations. |

Every score below 9 is explained in its row and in Section 22.
