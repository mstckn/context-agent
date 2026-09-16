# PREMIUM CONTEXT ACCEPTANCE REPORT

**Subject:** Context Agent — LEVEL 3 → LEVEL 5 (Autonomous Context
Intelligence Layer) upgrade
**Spec:** `promt.txt` · **Baseline:** `docs/PREMIUM_UPGRADE_BASELINE.md` ·
**Audit compared:** `CONTEXT_SYSTEM_AUDIT.md`
**Evidence:** `docs/bench_after.json`, `docs/CONTEXT_QUALITY_EVALUATION.md`

---

# 1. Executive Summary

The Context Agent was upgraded from a keyword router with good manners
(LEVEL 3) into a deterministic, LLM-free **context-intelligence layer**
(LEVEL 5). It now knows what the model needs (task-aware hybrid retrieval),
what it already knows (persistent cross-turn ledger with decay and
freshness), what changed (git-aware retrieval), what can be summarized
(tiers/outlines/previews with chunk maps), what must be raw (`raw` mode),
and when more context is required (sufficiency engine with automatic
expansion).

All baseline gates passed: routing hit 1.00, top-1 accuracy 0.875 → **1.00**,
answer-quality 9/9 at overlap 1.00 / noise 0.00, avg per-query savings
59.1% → **65.4%**, testbed 12/12, ledger cross-turn savings proven
(421 → 1121 tokens saved between consecutive turns), migrations and
large-file gates closed. Quality was never traded for savings: every
retrieval change was gated on the eval suite.

# 2. Architecture Changes

* Task Intelligence: `classify_task` + `planner.plan_task` (op type, risk,
  evidence requirements, budget ratio).
* Hybrid retrieval: lexical FTS (exact-token vs prefix layers), symbol
  token matching, structural graph (`edges`), content-chunk search.
* Context Planner: adaptive model-aware budget (`ModelContextProfile`),
  persistent ledger, progressive expansion.
* Git-aware retrieval: branch/head/dirty/changed/recent signals boost
  active-work files right after the primary block.
* Explainability: point-level reasons + opt-in `retrieval_diagnostics`.
* Large-file strategy: preview + `chunk_map` with ready `get_cmd`s.
* See `docs/CONTEXT_INTELLIGENCE_ARCHITECTURE.md` for diagrams and schemas.

# 3. Files Changed

Engine (new or upgraded): `agent.py`, `route.py`, `mcp_server.py`,
`ledger.py` (new), `planner.py` (new), `sufficiency.py` (new),
`memory.py` (new), `model_profile.py` (new), `content_index.py` (new),
`graph.py` (new), `git_meta.py` (new), `get_range.py` (preview/chunk_map),
`index.py` (chunks/graph/FTS integration), `install.py` (`.aiignore`
template fix), `benchmark_token_savings.py` (gate queries),
`eval_answer_quality.py`.

Tests: `tests/` grew from 0 to **101 unit tests** (incl.
`test_git_explain.py` for git boost, chunk maps, diagnostics, compact
pass-through). Testbed: `examples/sample_project` (git-initialized, 12
tests, migrations + 247-line reporting module for the gates).

Docs: this report, `CONTEXT_INTELLIGENCE_ARCHITECTURE.md`,
`CONTEXT_QUALITY_EVALUATION.md`, `MIGRATION.md`, `OPERATIONS.md`,
`PREMIUM_UPGRADE_BASELINE.md`, `bench_after.json`.

# 4. New Data Models

* `context_ledger` / `ledger_sessions` — per (scope, session, file,
  representation) hash, cost, turn, decay state.
* `project_memory` — 8 memory types, 4 lifecycle statuses, supersession
  chain, deterministic revalidation.
* `content_chunks` / `chunks_meta` — chunk-level FTS with attribution.
* `edges` / `graph_meta` — resolved imports/calls/refs with confidence.
* `ModelContextProfile` — window/safe-budget/output-reserve per model.
* All additive SQLite tables; zero destructive migrations.

# 5. Retrieval Improvements

* Layered scoring: 24 path segment / 12 exact symbol / 10 path-substring &
  summary exact-token / 8 symbol name-token / 7 signature-token / 5
  prefix / 4 recall pass; `DOMAIN_CAP=8`; test damping ×0.85.
* Exact-token vs prefix FTS separation kills `wrap* → wrapper` false
  positives; identifier-token filter kills substring inflation; plural
  normalization (`migration` ≈ `migrations`); generic-verb demotion.
* Measured: top-1 0.875 → 1.00; migrations file now top-1 for database
  tasks; previously blind spots closed.

# 6. Context Ledger

Decisions: `send / skip / refresh` with reasons (`never_sent`,
`content_changed`, `representation_upgrade`, `known_to_model:*`,
`decaying`, `expired_from_window`). Freshness wins over savings.
Measured economics: turn N `known_count 5, tokens_saved 421` → turn N+1
`known_count 8, tokens_saved 1121` on an identical task — the single
largest token-waste identified by the audit ("re-sending identical context
every turn") is eliminated.

# 7. Project Memory 2.0

Typed memories (`ARCHITECTURAL, DECISION, TASK, CONSTRAINT, KNOWN_ISSUE,
USER_CORRECTION, RUNTIME, EPHEMERAL`) with lifecycle
(`ACTIVE → SUPERSEDED / STALE / INVALIDATED`), supersession history,
deterministic staleness revalidation against code anchors, and compaction
limited to ephemeral classes. A prioritized, budget-capped state sheet
(CONSTRAINT → DECISION → ARCHITECTURAL → …) ships in context.

# 8. Adaptive Budgeting

Model windows replace the static 8000-token default: safe input budgets
per model with conservative fallback. Per-action token estimates
(full/symbols/outline/preview) drive downgrade-before-skip overflow
policy. Planner adjusts allocation by task type and risk.

# 9. Sufficiency & Expansion

`context_quality_score` over the *effective* context (sent +
known-to-model). When insufficient, the engine performs an **automatic
expansion turn** through the same loading pipeline (freshness → ledger →
dedup → budget). Manual escape hatches preserved: `strict_quality`,
`raw`, `get_range/get_symbol/get_related` expand commands on every
known/skipped item.

# 10. Testing

* Engine: **101 tests** — ledger semantics, retrieval layers, memory
  lifecycle, budgeting, sufficiency, git parsing, chunk maps, compact MCP
  pass-through, security/robustness.
* Testbed: **12/12** in `examples/sample_project`.
* Evals: routing 8/8 (hit 1.00, top-1 1.00); answer quality 9/9.
* All green after the final scoring changes (`pytest tests/ -q`).

# 11. Benchmark Results

| Metric | Baseline | Final |
|---|---|---|
| Hit rate | 1.00 | 1.00 |
| Top-1 accuracy | 0.875 | **1.00** |
| Answer-quality overlap / noise | 1.00 / 0.00 | 1.00 / 0.00 |
| Avg per-query savings | 59.1% | **65.4%** (min 47.8, max 87.3) |
| Migrations query | blind spot | top-1 |
| Large file | dumped | preview + chunk_map |
| Ledger cross-turn | re-send all | 1121 tokens saved @ turn N+1 |

Machine-readable: `docs/bench_after.json`.

# 12. Remaining Limitations

* **Scale**: chunked FTS + graph improve behavior, but 100k-file repos and
  subprocess-per-script latency remain unproven (audit concern persists,
  mitigated not solved).
* **Memory ingestion** is still write-on-request; no automatic extraction
  from conversations/commits.
* **Static graph** resolution is heuristic for dynamic imports/metaprogramming.
* **No CI**: quality gates are scripted and verified manually.
* Diagnostics are opt-in by design — no always-on retrieval trace store.

# 13. Backward Compatibility

* All CLI flags/MCP params additive; defaults preserve LEVEL 3 behavior.
* DB changes additive only; rollback = restore previous scripts.
* Escape hatches (`raw`, `strict_quality`, manual expand) unchanged.
* Testbed + eval fixtures prove no functional regression.

# 14. Production Risks

| Risk | Mitigation |
|---|---|
| Ledger wrongly skips a file | hash freshness forces resend on any change; decay refreshes; `--new-session` escape |
| Scoring drift on new repos | eval harness shipped; gates re-runnable in minutes |
| `.aiignore` drift hides files | template fixed; OPERATIONS troubleshooting table |
| SQLite lock under concurrency | busy_timeout + atomic IO already in place |
| Preview hides needed lines | chunk_map `get_cmd` always offered; partial signals preserved |

# 15. Final Scores

Compared directly against `CONTEXT_SYSTEM_AUDIT.md` §19:

| Dimension | Audit (L3) | Now (L5) | Why |
|---|---|---|---|
| Context Efficiency | 7 | **9** | cross-turn deltas, adaptive budget, previews |
| Context Accuracy | 5 | **9** | top-1 1.00, 9/9 answer quality, hybrid retrieval |
| Project Awareness | 5 | **8** | git signals, memory state sheet, task plans |
| Long-Term Memory | 4 | **8** | typed memories, supersession, staleness checks |
| Retrieval Quality | 5 | **9** | exact/prefix layers, token filters, graph, content search |
| Cross-File Awareness | 4 | **8** | resolved edges, get_related, migrations indexed |
| Token Budgeting | 5 | **9** | real model windows, per-action estimates, downgrade policy |
| Quality Preservation | 6 | **9** | sufficiency auto-expansion, strict mode, freshness, diagnostics |
| Scalability | 5 | **6** | chunking/graph help; 100k-file proof still absent |
| Observability | 6 | **8** | per-item reasons + opt-in diagnostics + ledger transparency |
| Testability | 3 | **9** | 101 unit + 12 testbed + evals + benchmark suite |
| Production Readiness | 5 | **7** | gates proven & documented; CI still missing |

---

## PREMIUM UPGRADE VERDICT

# **GO**

**Why aggressive context reduction is now safe:** the engine no longer
guesses with keywords alone — retrieval is hybrid and gate-proven (top-1
1.00, answer-quality 1.00 with zero noise), so the files a human would
include are the files delivered. What is not delivered is *provably known*
to the model (hash-verified ledger) or *cheaply recoverable* (chunk maps,
expand commands, partiality signals). Freshness always overrides savings,
and the sufficiency engine re-retrieves autonomously when coverage drops.
The result is 65.4% average per-query token savings with **no measured
quality loss** — exactly the "minimum sufficient context with quality
preservation" contract of the spec.
