# CONTEXT_SYSTEM_AUDIT.md

**Audit scope:** `.context` installation of the Context Agent system ("Context Agent" / internal name `LocalContextAgent2LLM`)
**Audit date:** 2026-08-30
**Mode:** READ-ONLY audit. No code was modified.
**Evidence base:** Full read of all 31 Python modules in `scripts/`, both eval fixtures, directives, `system_prompt.md`, runtime state (`symbols.db`, `project_id.json`, `install_meta.json`), and live DB inspection.

---

# 1. Executive Summary

This project is a **deterministic, LLM-free context engine** that attaches to any MCP-capable coding IDE as a tool server. Its job: given a natural-language task, select the minimum-sufficient set of files/symbols/snippets, package them under a token budget, and attach compact task memory ("Context Capsule") and playbook directives — so the host LLM (Claude/GPT/etc., which lives *outside* this system) never needs the whole repository in context.

**What it genuinely is:**

- An MCP stdio/HTTP server (`scripts/mcp_server.py`, `scripts/mcp_server_http.py`) exposing 13 tools.
- A task router (`scripts/route.py`) that classifies tasks by keyword and scores files using SQLite FTS5 + LIKE heuristics against a symbol index.
- An orchestrator (`scripts/agent.py`) that assembles a token-budgeted context package with tiered loading (full file / skeleton outline / focus symbols), freshness checks, dedup, quality scoring, and a sufficiency escape hatch.
- A lightweight memory layer (`scripts/capsule.py`, `scripts/session.py`) of JSON files that persist decisions, notes, touched files, and events across turns/sessions.
- A human-authored playbook layer (`scripts/directives.py` + `.context/directives/*.md`).
- Install/distribution machinery for many IDEs (`install.py`, `global_install.py`, `ide_detector.py`, `mcp_launcher.py`, `lifecycle_manager.py`, `dashboard_server.py`).

**Headline findings:**

1. **The system makes zero LLM calls and has zero embeddings.** All retrieval is lexical (FTS5 BM25 over paths/summaries/symbol names + substring LIKE). There is no semantic search, no code-content index, no call graph, no symbol-reference analysis.
2. **Retrieval quality is the bottleneck.** Import edges are resolved by string-pattern matching of import text against file paths (`route.find_neighbor_files`, `get_related.find_imported_file`), which is unreliable. Only Python and JS/TS imports are extracted, max 20 per file, regex-based.
3. **Memory is agent-written, append-only, and shallow.** Nothing reads IDE chat history. Decisions/constraints survive only if the host LLM explicitly calls `context_agent_capsule`/`session` tools. Recall of past capsules is keyword-scored, capped at 5 entries truncated to 220 chars (`capsule.recent_capsule_lines`). No compaction, no contradiction handling, no staleness invalidation.
4. **Token budgeting is a per-package ledger, not a context-window manager.** `budget.py` tracks spending against a fixed limit (default 8000, caller-supplied). It knows nothing about the actual model window, reserved output tokens, system-prompt cost, or tool-definition cost. Allocation is *not* adaptive to task type beyond a cosmetic model recommendation.
5. **Quality guardrails are real and unusually thoughtful** for this maturity level: freshness re-indexing, relevance-windowed truncation instead of blind cuts, skeleton mode for dependency files, explicit `partial`/escape-hatch signals, `strict_quality` reinforcement loop, `sufficiency` flag, `raw=true` fidelity mode, prompt-injection warnings.
6. **But omissions are silent.** When a file doesn't fit the budget it is downgraded or skipped with only a stderr trace (`agent.build_context_package`); the package does not prominently enumerate what was *not* loaded, which is exactly where under-context quality loss hides.
7. **Testing proves routing hit-rate only** (`scripts/eval.py`, `evals/basic.json`), plus proxy metrics (`eval_answer_quality.py`, `benchmark_token_savings.py`). There are **no unit tests at all**, and no test proves reduced context preserves coding quality on cross-file/long-horizon tasks.

**Verdict in one line:** A well-engineered **LEVEL 3 (project-aware retrieval)** system with early LEVEL 4 orchestration traits — production-usable for small/medium Python/TS repos, but not yet a trustworthy "intelligent context OS" because retrieval is purely lexical, memory is passive, and budgeting ignores real model windows.

---

# 2. Current Architecture

## 2.1 Component inventory

| Layer | Files | Role |
|---|---|---|
| Entry points | `scripts/mcp_server.py` (stdio JSON-RPC), `scripts/mcp_server_http.py` (HTTP/SSE), `scripts/mcp_launcher.py` | MCP protocol; tool schemas; response compaction; auto-prime on `initialize` |
| Orchestration | `scripts/agent.py` | `build_context_package()` — the single context assembly pipeline |
| Task analysis | `scripts/route.py` | Keyword classification, file scoring, context plan, confidence, model recommendation |
| Index | `scripts/index.py`, `scripts/watch.py` | SQLite `symbols.db` (files/symbols/imports + FTS5), `map.json`, optional polling watcher |
| Retrieval primitives | `scripts/search.py`, `scripts/get_symbol.py`, `scripts/get_range.py`, `scripts/get_related.py`, `scripts/from_log.py` | Symbol/file lookup, line ranges, import neighbors, stack-trace routing |
| Budget | `scripts/budget.py`, `scripts/token_count.py` | Per-package token ledger; tiktoken-or-heuristic counting |
| Dedup | `scripts/dedup.py` | `session_context` table keyed by scope+file+action+symbols+hash |
| Memory | `scripts/capsule.py`, `scripts/session.py` | JSON task memory (capsules) and session notes/decisions |
| Guidance | `scripts/directives.py`, `.context/directives/*.md` | Scored playbook retrieval with frontmatter matching |
| Log handling | `scripts/compress_log.py`, `scripts/from_log.py` | Log noise compression; stack-trace → code windows |
| Accounting | `scripts/usage.py` | Per-client savings/quality event ledger (`usage.json`, 200-event ring) |
| Ops/UX | `scripts/dashboard_server.py`, `scripts/lifecycle_manager.py`, `scripts/ide_detector.py`, `scripts/install.py`, `scripts/global_install.py` | Local dashboard, lifecycle/watchdog, IDE config writers, installers |
| Utilities | `scripts/paths.py`, `scripts/safe_io.py`, `scripts/git_meta.py` | Root/scope/project-id resolution, atomic writes + file locks, git branch/commit metadata |

## 2.2 Storage layout (per project, under `.context/`)

```
.context/
├── scripts/            # copied engine (install.py, sha-checked via install_meta.json)
├── symbols.db          # SQLite: files, symbols, imports, *_fts (FTS5), session_context (dedup)
├── map.json            # generated project map: modules, per-file summaries, commands, top symbols
├── project_id.json     # stable project identity (project_id, scope_name, root_hint)
├── budget.json         # OR budgets/<scope>.json — per-scope token ledger
├── capsules/<scope>/   # current.json + archived <task-id>.json (task memory)
├── sessions/<scope>/   # current.json + archived sessions + stable_<scope>.json (directive dedup marker)
├── usage.json          # savings/quality event ledger (ring buffer, 200 events)
├── directives/*.md     # human-authored playbooks (frontmatter-matched)
├── mcp_bootstrap.json  # auto-prime status
└── watch_state.json    # optional watcher mtime/size state
```

Observed live state of this instance: `symbols.db` exists with full schema (files/symbols/imports/FTS5/triggers) but **0 rows** — indexing has not run here; `map.json`, `capsules/`, `sessions/` are absent until first use. `project_id.json` shows `scope_name: opt-86e65b007f`, `root_hint: /opt`.

## 2.3 ASCII architecture diagram

```
                ┌──────────────────────────── IDE (Claude/GPT/Qwen host agent) ───────────────────────────┐
                │  chat history lives HERE, inside the IDE — Context Agent never sees it                  │
                └───────▲──────────────────────────────────────────────────────────────┬───────────────────┘
                        │ MCP JSON-RPC (stdio or HTTP/SSE)                              │ tool calls
                        │ compact packages / sufficiency flags / escape hints            │
┌───────────────────────┴──────────────────────────────────────────────────────────────▼───────────────────┐
│ mcp_server.py / mcp_server_http.py / mcp_launcher.py                                                      │
│  • initialize → auto_prime_worker (bg thread: index + capsule/session warm-up) → mcp_bootstrap.json        │
│  • tools/list (13 tools) • compact_context_package(): ≤8 items, relevance-windowed content (3000 chars)    │
│  • prompt-injection pattern scan • usage tracking of tool responses                                        │
└───────┬───────────────┬──────────────┬───────────────┬──────────────────┬─────────────────┬──────────────┘
        │ subprocess     │              │               │                  │                 │
┌───────▼──────┐  ┌──────▼──────┐ ┌─────▼─────┐  ┌──────▼──────┐  ┌────────▼───────┐ ┌───────▼───────┐
│ agent.py     │  │ route.py    │ │ search.py │  │ get_symbol  │  │ from_log.py    │ │ compress_log  │
│ orchestrator │──▶ classify    │ │ get_range │  │ get_related │  │ stack-trace    │ │ noise filter  │
│ build_context│  │ score files │ │ get_range │  │ line/symbol │  │ routing        │ └───────────────┘
│ _package()   │  │ plan+budget │ └─────┬─────┘  └──────┬──────┘  └────────┬───────┘
└───┬───┬───┬──┘  └─────────────┘       │               │                  │
    │   │   │                           ▼               ▼                  ▼
    │   │   │                ┌─────────────────────────────────────────────────────┐
    │   │   │                │ symbols.db (SQLite + WAL + FTS5)        map.json     │
    │   │   │                │ files(path,hash,lang,summary,tokens)    modules      │
    │   │   │                │ symbols(name,type,file,lines,sig,doc)   commands     │
    │   │   │                │ imports(file,imports_from)              top symbols  │
    │   │   │                │ session_context (dedup ledger)                       │
    │   │   │                └──────────────────▲──────────────────────────────────┘
    │   │   │                                   │ index.py (AST for Py, regex for JS/TS)
    │   │   │                                   │ watch.py (optional 2s poller)
    │   │   └────────────▶ budget.py   (per-package ledger: init/spend/status, cost est.)
    │   └────────────────▶ dedup.py    (scope+file+action+symbols+hash keys; CLEARED per build)
    └────────────────────▶ capsule.py / session.py  (JSON task memory; events/decisions/files)
                           directives.py            (frontmatter-scored playbooks, stable-dedup)
                           usage.py                 (savings/quality accounting)
```

Key architectural property: **every tool call spawns Python subprocesses** (`run_script` in `mcp_server.py` and `agent.py`). A single `build_context` executes dozens of subprocess invocations (budget status per file, dedup check/add per file, capsule append per file, get_range per file). Simple and robust; latency is O(items) process spawns.

---

# 3. End-to-End Request / Context Flow

## 3.1 Actual lifecycle (as implemented)

The assumed lifecycle in the brief differs from reality in important ways. The real flow:

```
USER REQUEST (in IDE chat)
   │
   ▼
HOST LLM (external) decides to call Context Agent     ← classification happens in the LLM, not here
   │
   ▼
[MCP initialize, once]  mcp_server.handle("initialize")
   │  • sets CONTEXT_AGENT_CLIENT / CONTEXT_AGENT_SCOPE (project-stable scope via paths.project_scope_key)
   │  • start_auto_prime → bg thread: index (if symbols.db/map.json missing) + capsule/session warm-up
   │  • writes mcp_bootstrap.json; returns server instructions ("call build_context first")
   ▼
[MCP tools/call] context_agent_build_context(task, budget=8000, model, strict_quality?, raw?)
   │  (or context_agent_route first; or context_agent_from_log for stack traces)
   ▼
mcp_server.call_tool → subprocess: agent.py <task> --budget N --model M
   │
   ▼
agent.build_context_package():
   ├─ 1. capsule --start <task>          (archives previous capsule if task string differs)
   ├─ 2. budget --init N; budget --model M; dedup --clear     ← dedup state RESET every build
   ├─ 3. read map.json → compact module summaries → budget spend("map.json")
   ├─ 4. route.py <task> --budget N
   │       classify_task(): domains (TASK_PATTERNS), complexity (COMPLEXITY_INDICATORS),
   │                        op_type (bugfix/feature/refactor/modify/test), keywords (+CONCEPT_ALIASES)
   │       find_relevant_files(): FTS5 files_fts (path+summary) + LIKE on symbols.name/signature
   │                              scoring: path-segment 24 / path 10 / symbol-exact 12 / symbol 7 /
   │                                       domain 5 / file-summary 4 / nearby-test 6
   │       find_neighbor_files(): imports → LIKE-match import text vs file paths; imported_by via stem
   │       build_context_plan(): budget-fit steps (primary ≤3, then secondary, then small related)
   │       calculate_confidence(): top score + symbol/related bonuses → high/medium/low
   │       recommend_model(): heuristic cost/model suggestion (does NOT change budget)
   │       (route failure → _fallback_search_items: direct LIKE on files/symbols, confidence=low)
   ├─ 5. session --context --task + capsule --context --task   (recent memories, keyword-ranked)
   ├─ 6. FOR each planned file (max 8):
   │       tier_action(): primary → load_file; non-primary >150 tok → load_outline (skeleton)
   │       ensure_fresh_file(): MD5 vs files.hash → reindex single file if changed
   │       dedup --check (scope|client|file|action|symbols|hash)
   │       budget --status; if estimate > remaining:
   │           load_file → downgrade to load_symbols (est 250); else SKIP (stderr only)
   │       build_context_item():
   │           load_file    → get_range (full file; >200 lines → "preview": first 50 lines only)
   │           load_outline → signatures+docstrings from symbols table + import head lines
   │           load_symbols → first 25 lines + up to 5 focus symbol bodies
   │       dedup --add; budget --spend(estimate); capsule --add-context
   ├─ 7. context_quality_score(): confidence base + tests/deps/symbols/fresh/content bonuses → 0..100
   │     if strict_quality>0: apply_strict_quality loop loads up to 4 extra candidates
   │     (route related_files, nearby tests, same-folder companions) until score ≥ target
   ├─ 8. select_task_directives(): score playbooks (op 30 / glob 25 / domain 15 / keyword 8;
   │     always=1000) within directive_budget (default 1200); stable directives deduped per scope
   ├─ 9. build_context_sufficiency(): sufficient = score≥45 AND confidence≠low AND items>0
   │     if not → recommendation + read_directly file list (escape hatch)
   ├─10. budget --status → budget summary; usage --record (repo_tokens, context_tokens, quality)
   └─11. generate_system_prompt(): budget bar + modules + loaded files + scripts + session/capsule
         context + directives + task  → returned as package["system_prompt"]
   ▼
mcp_server.compact_context_package():
   • ≤8 items; per-item content ≤3000 chars via relevance_window()
     (anchor-based windowing around keyword-dense lines; head-window fallback)
   • partial:true + full_path + escape note for truncated/outline items
   • prompt_injection_warnings() on content
   ▼
track_tool_usage() → usage --record (kind=tool)
   ▼
HOST LLM receives package → works; may call get_symbol/get_range/get_related/search/capsule
   incrementally; may bypass everything and read files directly (explicit escape-hatch policy)
   ▼
MEMORY UPDATE: only if host LLM calls capsule --event/--complete or session --note/--decision/--done
   (deterministic code NEVER extracts decisions from conversation; there is no conversation access)
```

## 3.2 Stage-by-stage responsibility map

| Stage | File / function | Input → Output | Failure mode |
|---|---|---|---|
| Request intake | `mcp_server.handle`, `call_tool` | JSON-RPC → subprocess | Script timeout (60/120s) → generic error |
| Task classification | `route.classify_task` | task text → domains/complexity/op_type/keywords | Substring matching misclassifies ("migrate a test" → complexity high *and* op test) |
| File discovery | `route.find_relevant_files` | keywords → scored files | Lexical miss ⇒ empty/low-confidence set; fallback marks `confidence=low` |
| Dependency expansion | `route.find_neighbor_files`, `get_related` | file → 1-hop import/imported-by | LIKE-based path matching: false positives and false negatives |
| Memory retrieval | `session.get_context`, `capsule.context_text` | task → ranked recent lines | Keyword overlap only; caps (5/3 entries, 220 chars) |
| Budget planning | `route.build_context_plan`, `budget.py` | files+limit → steps | Estimates vs actuals drift; overspend silently not recorded (`budget.spend_locked` refuses) |
| Context assembly | `agent.build_context_item`, `tier_action` | steps → content items | Large file silently becomes 50-line preview (`get_range` preview mode) |
| Quality gate | `agent.context_quality_score`, `apply_strict_quality` | items → 0-100 score | Score measures presence of signals, not task coverage |
| Sufficiency | `agent.build_context_sufficiency` | score+confidence → bool | Advisory only; host LLM may ignore |
| Compaction | `mcp_server.compact_context_package`, `relevance_window` | package → ≤8 items, ≤3000 chars each | Anchor miss → head window keeps wrong region |
| Persistence | `capsule.py`, `session.py`, `usage.py` | events → JSON files | Only what the host LLM chose to record |

---

# 4. Project Memory & Summary Sheet

There is **no single "summary sheet"**. Persistent project awareness is split across four mechanisms:

## 4.1 `map.json` — the project anatomy sheet

- **Created/updated by:** `index.build_map` on every indexing run (deterministic code, no LLM).
- **Schema:** `generated_at`, `root`, `stats{total_files,total_tokens,symbol_counts}`, `scripts` (how-to), `commands` (detected test/lint/build via `detect_project_commands`), `modules{<dir>: {files{path:{lang,lines,summary,tokens}}, total_lines,total_tokens}}`, `symbols` (first 100 alphabetically — *not* "most referenced" despite the comment in `build_map`).
- **File summaries** come from `index.generate_summary`: rule-based — first comment line + symbol type counts + first 8 symbol names + line count. No semantics.
- **Read by:** `agent.build_context_package` step 2 (compacted to one summary per module) and the `context-agent://project-map` resource (12 files/module cap).
- **Staleness:** regenerated on each index run; single-file refresh via `index.py --file`. Between runs it can lag reality; `ensure_fresh_file` only protects files that are actually being loaded.
- **Assessment:** Good as a table of contents. It contains **no architecture description, no decisions, no constraints, no open issues** — an LLM reading only `map.json` learns *what exists*, not *what the project is about or what was agreed*.

## 4.2 Context Capsule (`capsules/<scope>/current.json`) — task memory

- **Schema** (`capsule.start_locked`): `id, task, status, started_at, updated_at, context_items[], files[], events[], verification[], summary, meta{project_id, scope, git{branch,commit,...}}`.
- **Created:** at the start of *every* `build_context_package` (`capsule --start task`). If the task string differs from the current capsule's, the old one is archived (`archive_current`).
- **Written by:** deterministic code (context_items/files appended automatically per loaded file) **and** the host LLM (`--event decision|note|risk ...`, `--complete <summary>`).
- **Read by:** `capsule --context` inside every package build and via `context-agent://capsule` resource. Rendering: last 8 context items + last 8 events + summary + up to 4 keyword-ranked archived capsules (line cap 220 chars) — `capsule.context_text`, `recent_capsule_lines`.
- **Git awareness:** each record carries branch/commit (`git_meta.git_info`); rendered memory annotates entries from other branches (`[branch:X != current:Y]`) — a genuinely nice anti-contamination signal.
- **Weaknesses:**
  - `verification[]` field exists but **nothing ever writes it** — dead schema.
  - `summary` is free text supplied only at `--complete`; nothing compacts events into it.
  - No LRU/expiry: archives accumulate forever; recall is only the top 4-5 by keyword score.
  - Contradictory decisions can both persist ("use redis" / "use memcached") — last-writer wins nowhere; there is no supersession.
  - Task identity is the raw task string: slight rephrasings create new capsules and orphan the old memory into "recent" recall.

## 4.3 Sessions (`sessions/<scope>/current.json`)

- **Schema:** `name, started_at, notes[], decisions[], files_touched[], tasks_completed[], meta`.
- **Written only by explicit host-LLM calls** (`session --note/--decision/--touch/--done`). `agent.py` never records notes/decisions automatically; it only *reads* `--context` and never calls `--touch`/`--done` either (touched-file tracking is effectively unused by the engine itself).
- **Rendering caps** (`session.get_context`): last 5 decisions, 5 notes, 8 touched files, 3 completed tasks, plus 3 recent archived sessions.
- Overlaps heavily with capsules; both are injected into every package (`sections.session_context` and `sections.capsule_context`) — duplicated memory channels.

## 4.4 Directives (`.context/directives/*.md`)

Human-authored playbooks with frontmatter matching (`match_op`, `match_domains`, `match_keywords`, `match_globs`, `always`, `priority`, `budget_tokens`). Scored by `directives.score_directive` (weights: glob 25, op 30, domain 15, keyword 8, always 1000), budget-fitted (default 1200 tokens, max 6 items), stable (`always:true`) directives deduplicated per scope via marker file `sessions/stable_<scope>.json` and cached in the `context-agent://rules` resource. This is the closest thing to *durable project conventions*, but it is manually maintained and only an example file ships with the install.

## 4.5 Verdict on memory sufficiency

Can an LLM regain project awareness after a long absence from these sheets?

| Need | Covered? | Evidence |
|---|---|---|
| What files/modules exist | ✅ | `map.json` |
| Build/test commands | ✅ | `detect_project_commands` |
| Current task + recently loaded files | ✅ | capsule current |
| Recent decisions/risks | ⚠️ partial | only if host LLM recorded them; recall capped and keyword-ranked |
| Architectural decisions (long-term) | ❌ | no durable store; sessions archive but are not semantically retrievable |
| Unresolved issues / TODOs | ❌ | no mechanism |
| Constraints & conventions | ⚠️ manual | directives, if authored |
| What was completed | ⚠️ partial | `tasks_completed` (LLM-written), capsule `--complete` summaries |
| Staleness detection | ⚠️ partial | file hashes only; memory entries never expire or get revalidated |

**The sheet is not sufficient for true long-horizon project awareness.** It preserves *activity traces*, not *project truth*.

---

# 5. File and Code Retrieval System

## 5.1 What exists

| Mechanism | Present? | Where |
|---|---|---|
| Filename/path matching | ✅ | `route.find_relevant_files` (path-segment scoring), `search.py --file-only` |
| Lexical/BM25 search | ✅ partial | SQLite FTS5 `files_fts` (**path + summary only**) and `symbols_fts` (name, file, docstring, signature). `ORDER BY rank` used in `route.fetch_file_matches` and `search.py`. |
| Symbol index (AST) | ✅ Python (true AST), JS/TS (regex), others: none (`index.extract_symbols` returns [] for go/rs/java/…) |
| Import/dependency graph | ⚠️ naive | `imports` table; regex-extracted (Python `from/import`, JS `import/require`), max 20/file; resolution = LIKE-matching import string to file paths |
| Symbol references / call graph | ❌ | not implemented anywhere |
| Semantic search / embeddings | ❌ | no embedding model, no vector store (verified: no provider/vector imports in codebase) |
| Code-content full-text search | ❌ | **file bodies are not indexed anywhere** — queries can only hit paths, summaries, symbol names, signatures, docstrings |
| Git diff / recently modified | ⚠️ | `watch.py` optional poller; git branch/commit stored as memory metadata only, not used for retrieval |
| Currently open files / IDE state | ❌ | never received from the IDE |
| Stack traces | ✅ | `from_log.py` (frame regexes → ±40-line windows + route) |
| LLM-based selection | ❌ | by design — the engine is LLM-free |
| Deterministic heuristics | ✅ | all scoring is hand-tuned weights |

## 5.2 The actual selection algorithm (`route.py`)

1. `tokenize` task → words ≥3 chars minus STOPWORDS; `expand_keywords` via a 14-entry hand-written `CONCEPT_ALIASES` map (throttle→rate/limit, login→auth…, etc.).
2. Classify: `TASK_PATTERNS` (10 domains), `COMPLEXITY_INDICATORS` (substring), `op_type` (keyword buckets).
3. Score files:
   - per keyword: FTS prefix match on `files_fts` → +24 if keyword equals path stem or is a path segment, else +10;
   - per domain: pattern matches → +5 (includes summaries);
   - per keyword: LIKE on `symbols.name`/`signature` → +12 exact / +7 partial to the owning file; FTS on files incl. summary → +4;
   - test-op tasks: nearby `*test*<stem>*` files +6.
4. Top files get `rank_symbols` (exact name > substring > signature; classes floored), `focus_symbols` (≤5), and `find_neighbor_files` (≤4 imports + ≤4 imported-by via LIKE patterns).
5. `build_context_plan` fits steps into the budget: first ≤3 as `primary` (load_file if ≤1200 tokens else load_symbols), rest `secondary`, then small related files (≤800 tokens) as `related`; overflow → `defer`.
6. `calculate_confidence` = top score + symbol bonus (8) + related bonus (4) + margin over runner-up; thresholds 35/18 → high/medium/low.

## 5.3 Failure scenarios (evaluated against code)

| Scenario | Outcome | Why |
|---|---|---|
| Change function in A; contract lives in B; no shared keywords, B doesn't import A directly | **FAIL** — B is invisible unless it imports/imports-by A (LIKE-resolved) or shares keywords | no symbol-reference search, no content index |
| Frontend component changes; shared API type elsewhere | **FAIL-likely** — only if path/summary keywords overlap | no cross-language type graph |
| DB schema change; migrations + consumers | **FAIL** — `migrations` is in `DEFAULT_IGNORE` (`index.py` L23-25) so migrations are never indexed; consumers only via keyword luck | ignore list + lexical retrieval |
| Bug deep in a dependency chain (trace available) | **PARTIAL** — `from_log.py` handles stack traces well (±40 lines per frame + route), but non-trace "it's slow/broken" cases get pure keyword routing | trace-only deep retrieval |
| User references something discussed 50 messages ago | **FAIL** — the system never sees chat history; only capsule/session events the LLM happened to record | no conversation ingestion |
| Task conflicts with a decision made days earlier | **FAIL-likely** — old decisions surface only via keyword overlap within top-3/5 recent sessions/capsules, truncated to 220 chars | shallow recall, no contradiction check |
| Synonyms outside the 14-entry alias map (e.g., "billing" vs "invoicing", "retry" vs "backoff") | **FAIL** | no embeddings |
| Behavior question ("where do we validate permissions?") | **FAIL-likely** — bodies not indexed; docstrings often empty (JS/TS extraction sets `docstring:""`) | no content FTS |

---

# 6. Conversation Memory System

**There is no chat-history processing in this system at all.** Findings per checklist item:

| Capability | Status |
|---|---|
| Full-history retention | ❌ not accessible — history lives in the IDE/host LLM |
| Summarization / hierarchical / rolling summaries | ❌ none |
| Task summaries | ⚠️ manual: capsule `--complete "<summary>"` written by the host LLM |
| Decision extraction | ⚠️ manual: `capsule --event decision` / `session --decision` — host LLM must decide to call it |
| User preference extraction | ❌ |
| Important-message preservation | ❌ |
| Raw-message retrieval | ❌ |
| Semantic retrieval of historical messages | ❌ — only keyword-scored JSON memory entries |

The design implicitly assumes the host LLM's own context window holds the conversation, and Context Agent only needs to persist *artifacts* (files loaded, decisions). That is coherent, but it means:

- **Exact error messages, API contracts, numerical parameters, rejected approaches, user corrections** survive only as free-text `--event` strings if the host LLM chose to record them. Nothing prompts or verifies this.
- The auto-prime step (`capsule --context`, `session --context` during `initialize`) merely warms caches; it cannot recover lost conversation.

**Summarization cannot destroy information here because no summarization exists — the risk is the opposite: information is never captured in the first place.**

---

# 7. Token Budgeting

## 7.1 What exists (`budget.py` + `agent.py` + `route.build_context_plan`)

- **Ledger model:** per-scope JSON (`budget.json` or `budgets/<scope>.json`) with `max_tokens`, `used_tokens`, `entries[]`, `model`. Initialized to the caller's `--budget` (default **8000**) at the start of *every* package build (`agent.build_context_package` step: `budget --init`).
- **Spending:** `budget --spend <n> <reason>` per section (map.json, session context, each file). `spend_locked` **refuses** to record spends beyond remaining (prints `budget_exceeded`) — overspend is prevented by refusal, but the pre-checks in `agent.py` use *estimates*, so actual content can drift from ledger values.
- **Warnings:** 50%/75%/90% thresholds embedded in spend/status responses.
- **Cost:** static `COSTS_PER_1K` table (claude-sonnet 0.003, haiku 0.00025, gemini-flash 0.0001, ollama 0).
- **Counting:** `token_count.py` — tiktoken `cl100k_base` if installed, else heuristic (words × 1.3 ASCII / × 2.0 non-ASCII). Note: many hot paths bypass it and inline `int(len(text.split())*1.3)` (`agent.file_outline_text`, `capsule.context_text`, `session.get_context`, `get_range`, `get_symbol`, `mcp_server.estimate_text_tokens`).
- **Per-file enforcement in assembly:** estimate > remaining ⇒ downgrade `load_file → load_symbols` (fixed est. 250) ⇒ else skip.
- **Directive sub-budget:** separate 1200-token budget (`directives.select_directives`).
- **Retrieval limits:** ≤8 relevant files loaded, ≤8 items returned, ≤3000 chars/content window, ≤5 focus symbols, symbol bodies truncated.

## 7.2 What is missing

1. **No awareness of the actual model context window.** `budget` is whatever the client passes (default 8000). The package is assembled identically for a 128k-window model and a 8k one.
2. **No reserved output tokens, no system-prompt cost accounting, no tool-definition cost accounting.** The generated `system_prompt` itself (modules + loaded list + session + capsule + directives) is never counted against the budget.
3. **No dynamic allocation by task type.** `complexity`/`op_type` only influence `recommend_model` (a *suggestion string*) and one risk note. A CSS tweak and an architectural refactor get the same default budget and the same pipeline; nothing raises budget or switches strategy (e.g., outlines-everywhere vs raw-everything) automatically. `raw=true` and `strict_quality` exist but must be requested explicitly.
4. **Budget resets per build** — there is no conversation-level budget model (how much context has this session already consumed / what should be evicted).
5. **Estimate/actual drift:** spends are recorded from estimates; real item tokens are computed later in `build_context_item` and only reflected in `usage --record` totals, not reconciled with the ledger.

---

# 8. Context Selection Algorithm

The complete selection policy (consolidated from §5.2 and `agent.py`):

```
INPUT: task string, budget B
1. KEYWORD-SCORE files (FTS5 + LIKE heuristics) → ranked list with reasons
2. 1-HOP NEIGHBORS via naive import/imported-by LIKE matching
3. PLAN: greedy budget-fit
     primary   = top-3 (full file if ≤1200 tok, else focus-symbols)
     secondary = next ranked (deferred when budget full)
     related   = small (≤800 tok) neighbors of top-3
4. TIER POLICY (agent.tier_action):
     primary            → load_file  (full body; >200 lines → 50-line preview!)
     non-primary >150t  → load_outline (signatures + docstrings + import head)
     tiny non-primary   → load_file
5. PER-ITEM GUARDS: freshness reindex → dedup → budget (downgrade/skip)
6. CONTENT WINDOWING at MCP layer: ≤3000 chars anchored on task keywords
   (relevance_window; head-window fallback); partial flag + escape hatch
7. QUALITY LOOP (opt-in strict_quality): add related/tests/companions until score ≥ target
8. DIRECTIVES: frontmatter-scored playbooks within 1200 tokens
9. SUFFICIENCY: score≥45 ∧ confidence≠low ∧ items>0, else "read these directly" advice
```

**Determinism:** fully deterministic given index state + task string. No randomness, no LLM.

**What the algorithm optimizes:** keyword coverage per token. **What it does not model:** task semantics, symbol usage, control/data flow, edit impact, test coupling, schema coupling, recency, user intent history.

---

# 9. Quality Preservation Mechanisms

Every existing protection, with evidence:

1. **Freshness guarantee per loaded file** — `agent.ensure_fresh_file` compares MD5 against `files.hash` and reindexes the single file before loading if stale. Prevents answering from outdated symbol data.
2. **Relevance windowing instead of blind truncation** — `mcp_server.relevance_window` centers the 3000-char window on anchor-term-dense lines; falls back to head window only when no anchors match.
3. **Skeleton mode sends contracts, not bodies** — `agent.file_outline_text` + `_imports_preview`: dependency files contribute signatures/docstrings/imports (~10× cheaper) with `expand_commands` to fetch bodies.
4. **Explicit partiality signaling** — `compact_context_item` sets `partial:true`, `full_path`, escape instructions; `get_range` preview mode returns `mode:"preview"` + `get_full` command. The agent is never left guessing that content was cut.
5. **Sufficiency escape hatch** — `agent.build_context_sufficiency` + `rules_text()` + `system_prompt.md` "Kaçış yolu": when `sufficient=false`/confidence low/tool errors, the documented policy is to bypass the engine and read files directly. The engine is explicitly "an optimization, not a gate".
6. **Strict-quality reinforcement** — `agent.apply_strict_quality`: loads up to 4 extra items (route neighbors → nearby tests → same-folder companions) until `context_quality_score` reaches the target; `discover_nearby_test_files` biases for tests.
7. **Quality score transparency** — `context_quality_score` formula exposed in-package (`confidence base + has_tests 15 + has_dependencies 15 + has_focus_symbols 10 + all_fresh 10 + has_content 10`).
8. **Raw fidelity mode** — `raw=true` disables skeletons, windowing and stable-directive dedup; full bodies, no token savings. A guaranteed quality ceiling.
9. **Fallback on router failure** — `agent._fallback_search_items` builds keyword context directly from the DB and flags `fallback:true`, `confidence:low`.
10. **Prompt-injection defense** — `_PROMPT_INJECTION_PATTERNS` scan on all retrieved repo content; warnings attached to items; rules resource tells the agent to treat repo text as untrusted.
11. **Dedup hash-change detection** — `dedup.add_to_context` re-sends a file whose hash changed instead of counting it duplicate.
12. **Log-aware deep entry point** — `from_log.py` turns stack traces into precise ±radius windows plus a route pass — a high-precision channel for debugging.
13. **Branch annotations in memory** — `capsule.recent_capsule_lines` / `session.recent_session_lines` mark memories from other git branches, reducing cross-branch contamination.
14. **Multi-root safety** — `paths.find_project_root` + `ProjectRootSelectionRequired` prevent silently indexing/serving the wrong project in multi-root workspaces.
15. **Concurrency safety** — `safe_io.atomic_write_json` + `file_lock` around budget/capsule/usage writes; SQLite WAL + `busy_timeout`.

---

# 10. Quality Degradation Risks

| Severity | Risk | Cause | Scenario | Existing Protection | Gap |
|---|---|---|---|---|---|
| CRITICAL | Missing cross-file contract (interface/type/schema in file B) | Lexical-only retrieval; no symbol-reference or call graph; file bodies not indexed | Edit `AuthService.login` while the interface it must satisfy lives in `types.ts` with no keyword overlap | `related_files` if an import edge happens to resolve; agent can read more | No usage/reference graph; no content search; nothing flags "contract may be elsewhere" |
| CRITICAL | Unretrievable chat-referenced context | No chat-history ingestion or summarization at all | "Do it like we discussed" (50 msgs ago) | Capsule/session events *if* recorded | Entirely dependent on host LLM discipline; no verification |
| HIGH | Silent under-context under budget pressure | Oversized files downgraded/skipped; skips logged only to stderr | 8k budget, 6 large primaries → last files skipped; package looks complete | `load_symbols` downgrade; sufficiency flag (only if score/confidence low) | Package doesn't enumerate skipped files/reasons to the LLM prominently; sufficiency formula doesn't know what was dropped |
| HIGH | Wrong/missed import edges | `imports` resolved via LIKE of import text against paths; regex extraction; max 20 imports; Py+JS/TS only | `from mypkg.auth.service import x` stored as text; `get_related` matches first path containing the pattern — may bind wrong file or none | imported_by adds recall | No real resolver (package roots, aliases, tsconfig paths, barrel files) |
| HIGH | Stale/contradictory long-term memory | Append-only JSON; no expiry, supersession, or validation | Overturned decision ("use redis" → "use SQS") both surface later | Branch annotation; keyword ranking | No contradiction detection; no TTL; no revalidation against code |
| HIGH | Index blind spots | `migrations`, `fixtures` ignored by default; non-Py/TS languages get no symbols; >500KB files skipped; `.md/.json/.yaml` not indexed | Schema migration task misses migration files; Go/Java edits get no symbol support | `.aiignore` override; ignore list is configurable-ish | Defaults actively exclude exactly the files schema tasks need |
| HIGH | Over-truncation of primary files | `get_range` returns only first 50 lines when file >200 lines and no range given (`mode:preview`) | Primary 500-line service file arrives as head preview; core logic mid-file invisible | `partial` flag + `get_full` command; relevance windowing at MCP layer | Preview cut is position-blind (head), unlike the anchor windowing used later |
| MEDIUM | Keyword classification errors | Substring-based `op_type`/complexity ("update the migration tests" → modify+high+test) | Wrong directives/model recommendation, wrong risk notes | Weights are conservative | No semantic classification |
| MEDIUM | Retrieval noise displacing signal | Domain patterns add +5 broadly (e.g., any "token" file for auth tasks) | Top-8 slots filled with tangential files | Ranking by total score; confidence margin | No negative signals (e.g., generated/test boilerplate demotion beyond tests) |
| MEDIUM | Memory-recall truncation destroys specifics | 220-char line cap; top 4-5 entries; keyword scoring | Exact error text/param values from old task not recalled | `capsule --list` gives raw JSON if agent thinks to ask | No semantic recall over archives |
| MEDIUM | Estimate/actual token drift | Ledger spends use pre-load estimates | Budget bar misreports; planned deferrals can be wrong | Post-hoc usage recording | No reconciliation pass |
| MEDIUM | Repeated identical context across turns | `dedup --clear` at start of every build | Same 5 files re-sent verbatim each turn (tokens, not quality) | Within-build dedup | No cross-turn delta encoding ("already sent, unchanged") |
| LOW | Prompt injection | Retrieved repo/docs content | Malicious README instructions | Pattern scan + untrusted-data rule | Pattern list is small; no structural sandboxing |
| LOW | Stale `map.json` for never-loaded new files | Indexing on-demand/watcher optional | New file invisible to routing | `watch.py` available | Watcher not default; no git-hook integration |
| LOW | Wrong project root in polyrepo | Marker heuristics | Context from sibling repo | `ProjectRootSelectionRequired` prompt | Requires env-var follow-up |

---

# 11. Token Waste Analysis

No per-category instrumentation exists (usage.py records only aggregate `repo_tokens`/`context_tokens` per package + per-tool response estimates). Approximate analysis from code structure:

**Where tokens go per `build_context` package (default 8000 budget):**

| Category | Approx. share | Notes |
|---|---|---|
| File contents (context_items) | ~55-75% | the intended payload; skeletons reduce dependency files ~10× |
| Routing metadata (analysis, reasons, scores, execution plan, model recommendation, suggested commands) | ~8-15% | `relevant_files` carries reasons arrays, key_symbols, related_files — `compact_route_result` trims but keeps a lot |
| Session + capsule context | ~3-8% | re-sent every build; duplicated: once as `sections.session_context`/`capsule_context`, again inside generated `system_prompt` |
| Project map summary | ~3-6% | one summary per module every build |
| Directives | up to 1200 tokens | separate sub-budget; stable-dedup mitigates `always:` items |
| Budget/usage/system_prompt scaffolding | ~2-5% | budget bar, script list, rules |

**Identified waste:**

1. **No cross-turn dedup (largest waste source).** `agent.build_context_package` runs `dedup --clear` at step 2, so every new task-string rebuild re-sends the same primary files even if unchanged. The hash-aware dedup machinery (`dedup.add_to_context`'s "updated" path) is effectively unreachable across turns.
2. **Double embedding of memory:** session/capsule text appears both as package sections and inside `generate_system_prompt`. If the host pastes both, memory tokens are paid twice.
3. **Routing verbosity:** `why`/`reasons`, `suggested_first_commands`, `expand_commands` strings repeated per item, `model_recommendation` with alternatives — useful for debugging, heavy at scale. `detail:"compact"` mitigates but keeps these fields.
4. **`map.json` module summaries re-sent every build** instead of once per session (only stable directives get that treatment).
5. **Estimate-based spends** can over-reserve (outline items capped at 220 planning tokens regardless of real size).

**Looks redundant but is essential:**

- `partial`/`escape` metadata — small, prevents large quality loss.
- Import-preview lines in skeletons — cheap anti-hallucination anchor.
- `reasons` (at least one per file) — lets the host LLM decide whether to expand; removing them blindly would hurt.
- Directives — "method" context frequently prevents wrong-approach rework.

---

# 12. Existing Test Coverage

| Artifact | What it proves | What it does not prove |
|---|---|---|
| `scripts/eval.py` + `evals/basic.json` (8 fixtures) | Routing hit-rate: ≥1 expected file in top-N (default 5) for simple, keyword-aligned tasks; `--fail-under` gate | Cross-file completeness (only *any*-of-expected), budget behavior, content correctness |
| `scripts/eval_answer_quality.py` + `evals/answer_quality.json` (9 fixtures) | Overlap & noise proxies vs whole-repo baseline (retrieved set covers expected files with less noise) | Actual LLM answer quality — explicitly no LLM is called; overlap is a proxy |
| `scripts/benchmark_token_savings.py` | Deterministic savings % (full repo tokens vs retrieved tokens per query) | Quality preservation; multi-turn behavior |
| Fixtures reference `auth/`, `payments/`, `analytics/`, `db/`, `utils/`, `cli/` modules | — | These files **do not exist in this repo**; evals assume an external sample project (`examples/sample_project` referenced in docstrings). In-place `eval.py` here would index this repo and fail the fixtures. |

**Absent (checked: zero `test_*.py`/`conftest.py` anywhere, no pytest/unittest):**

- Retrieval correctness under synonyms/cross-file contracts; memory correctness (capsule/session lifecycle, archive corruption, concurrent writes); summary/rendering correctness; long-conversation degradation; context-window overflow; stale-memory contamination; hallucinated-file detection; regression suites; CI of any kind.

**Conclusion: the project does not currently prove that reduced context preserves coding quality.** It proves routing hits on easy fixtures and measures token deltas.

---

# 13. Scalability Assessment

| Repo size | Assessment |
|---|---|
| ≤1k files (this class of project) | Fully fine. Sub-second routing; index tiny. |
| ~10k files | **Workable with degradation.** FTS5 + SQLite WAL handle this. Concerns: full `rglob` re-scan per full index run; `map.json` stores per-file entries for all files (multi-MB), though only a compact slice is sent; `route`'s per-keyword `LIKE '%kw%'` on `symbols` **cannot use indexes** (leading wildcard) → table scans per keyword; `discover_companion_files` LIKE scans per primary file. Subprocess-per-call architecture multiplies latency. |
| ~100k files | **Not viable as-is.** Single-file SQLite still possible, but: full-scan indexing time, `map.json` size, per-turn `rglob`-free but scan-heavy LIKE queries, no parallelism, no incremental commit graph, 500KB file cap hides large generated/schema files. Would need shard/partition strategy, real inverted index for content, and incremental graph maintenance. |
| Millions of LOC | **Not viable.** No distributed storage, no embeddings index, no caching layer beyond SQLite page cache. |

Additional scaling-relevant facts: files >500KB skipped entirely (`index.should_ignore`); symbols only for Py/JS/TS; imports capped at 20/file; `usage.json` ring-buffered to 200 events (bounded); capsule archives unbounded (dir glob per recall — grows linearly with task count).

---

# 14. Architectural Maturity Level

**LEVEL 3 — Project-aware retrieval, with emerging LEVEL 4 traits.**

Justification against the rubric:

- Far beyond LEVEL 0-1: no truncation-only behavior; "summaries" are rule-based but retrieval is index-driven and task-conditioned.
- Not LEVEL 2 merely: it has task classification, dependency-neighbor expansion, execution plans, confidence, model recommendation — project awareness, not just query→chunks.
- LEVEL 4 traits present: orchestrated pipeline (route → plan → tier → quality loop → sufficiency), escape-hatch policy, strict-quality feedback loop, directive layer, usage accounting.
- Why not LEVEL 4: adaptation is shallow (budget and strategy are not truly task-elastic), memory is passive/manual, retrieval has no semantic or structural depth, and there is no closed loop where the system observes outcomes and adjusts (no learning, no self-repair, no verification of its own sufficiency beyond proxy scores).
- Not LEVEL 5: no autonomy, no self-improvement, no cross-session reasoning.

---

# 15. What Is Already Good

1. **Clean separation and stdlib-only discipline** — every module runs with no third-party deps (tiktoken optional); trivially portable, easy to reason about.
2. **The escape-hatch philosophy** (`rules_text`, `system_prompt.md`): the engine never blocks the agent; "read it directly" is a first-class, documented fallback. This single design decision caps worst-case quality damage.
3. **Freshness-by-hash per loaded file** — a guarantee many bigger systems lack.
4. **Tiered loading (full/outline/symbols) + relevance windowing** — genuinely smart token shaping that preserves contracts.
5. **Sufficiency + quality score surfaced in-package** — the host LLM receives honest self-assessment, not silent best-effort.
6. **Strict-quality loop** — an actual (opt-in) feedback mechanism targeting a measurable score.
7. **Directive/playbook layer** — harness-independent, offline-authored, budget-fitted guidance with stable-dedup; a premium feature rarely seen at this maturity.
8. **Multi-client/multi-project hygiene** — project-stable scopes (`project_id.json`), client attribution, per-scope storage, atomic writes, file locks, multi-root disambiguation.
9. **Stack-trace routing (`from_log`)** — high-value, well-targeted entry point.
10. **Honest measurement tooling** — shared `token_count.py` across indexer/router/benchmark ("the only way the savings ratio is honest"), plus an answer-quality proxy benchmark.
11. **Prompt-injection awareness** on retrieved content.
12. **Rich delivery layer** — 8+ IDE detection, global install, HTTP/SSE mode with token auth, dashboard, lifecycle manager.

---

# 16. What Is Missing

**Retrieval intelligence**
1. Semantic search (embeddings) or at minimum code-content full-text index.
2. Real import resolution (package roots, aliases, tsconfig paths) and a symbol-reference/call graph (who calls/uses X).
3. Edit-impact expansion: given files likely to change, auto-include consumers/contracts.
4. Recency signals: git diff / recently-modified / open-editor files as retrieval features.

**Memory intelligence**
5. Conversation ingestion or at least structured extraction hooks (decisions, constraints, rejected approaches, exact errors) — currently 100% manual.
6. Memory lifecycle: compaction of completed tasks, supersession of contradicted decisions, TTL/staleness revalidation against current code, semantic recall over archives.
7. A true persistent project-state sheet (architecture, constraints, open issues, decisions log) beyond `map.json` + capsules.

**Budgeting intelligence**
8. Model-window awareness: max context, reserved output, system-prompt + tool-schema costs; per-model profiles.
9. Task-elastic allocation: complexity/op_type → budget multipliers and strategy presets (CSS tweak vs refactor vs migration vs debug).
10. Conversation-level budget with eviction/delta encoding ("unchanged since last turn" references instead of re-sends).
11. Reconciliation of estimated vs actual tokens.

**Quality assurance**
12. Prominent "not loaded / deferred / why" manifest in every package.
13. Verification that recorded memory still matches code (e.g., decision references file X — does X still exist/contain Y?).
14. Automated regression tests and quality-preservation evals (see §12).

**Indexing**
15. Symbol extraction for Go/Java/Rust/C#/… (currently empty), docstring capture for TS, content indexing, configurable un-ignore of `migrations`.
16. Default-on incremental indexing (git hooks/watcher) instead of on-demand.

---

# 17. Top 10 Highest-Impact Improvements

*(Ranked by (quality protection × token savings) / effort. **Not implemented** — design inputs for Phase 2.)*

1. **Symbol-reference & call graph + content FTS** — index file bodies and symbol usages; let route expand "files that reference the symbols this task touches". Kills the CRITICAL cross-file-contract risk. (`index.py`, `route.py`, new `graph` table)
2. **Real import resolution** — resolve Python module paths and TS/JS aliases/barrels to actual files; store typed edges. Fixes `get_related`/neighbor reliability. (`index.extract_imports`, `route.find_neighbor_files`)
3. **Cross-turn context ledger with delta encoding** — stop `dedup --clear` per build; track what the client already has (by content hash) and send references/updates instead of re-sends. Largest single token win. (`agent.build_context_package`, `dedup.py`, `mcp_server`)
4. **Explicit omission manifest + hard sufficiency contract** — every package lists deferred/skipped files with reasons; sufficiency formula consumes skip count; host policy: `sufficient=false` ⇒ mandatory expansion step. (`agent.build_context_sufficiency`)
5. **Structured long-term project-state sheet** — durable `project_state.json`/SQLite (architecture notes, decisions with supersession, constraints, open issues, verified facts) with LLM-assisted compaction at task completion; semantic recall over archives. (`capsule.py`, new `memory.py`)
6. **Model-aware adaptive budgeting** — model profiles (window, output reserve, $/1k), task-type presets (complexity/op_type → budget & strategy), reconciliation pass. (`budget.py`, `route.recommend_model`)
7. **Semantic retrieval layer (optional embeddings)** — embed task + symbol/file summaries; hybrid score with existing lexical signals; cache embeddings in SQLite/vector file. Guards synonym misses. (`route.py`, new `embed.py`)
8. **Conversation capture hooks** — MCP tools + rules for the host LLM to persist decisions/corrections/errors *as they happen* (structured schemas), plus package-time reminders when task keywords match past unverified decisions. (`capsule.py`, `mcp_server` schemas)
9. **Index coverage fixes** — un-ignore migrations by default (opt-out), add Go/Java/Rust/C# symbol extractors, index .md/.yaml/.json as docs, default-on watcher/git-hook reindex. (`index.py`, `watch.py`)
10. **Quality-preservation eval harness** — mutation-style tests: remove-a-file, synonym-task, cross-file-contract, stale-memory, long-session suites wired to `eval.py` gates; plus LLM-judge answer-quality loop for the fixtures in `evals/`. (`eval.py`, new `eval_quality.py`, CI)

---

# 18. Files / Components Most Important for Phase 2

A future implementation agent must inspect these first (exact paths relative to this `.context` root):

| Priority | File | Why |
|---|---|---|
| 1 | `scripts/agent.py` | The pipeline itself: `build_context_package`, `tier_action`, `build_context_item`, `context_quality_score`, `apply_strict_quality`, `build_context_sufficiency`, dedup-clear behavior |
| 2 | `scripts/route.py` | All retrieval/scoring logic: `classify_task`, `find_relevant_files`, `find_neighbor_files`, `build_context_plan`, `calculate_confidence` |
| 3 | `scripts/index.py` | Schema creation (`init_db`), extractors (`extract_python_symbols`, `extract_js_ts_symbols`, `extract_imports`), `DEFAULT_IGNORE`, `build_map` |
| 4 | `scripts/mcp_server.py` | Tool schemas, `compact_context_package`, `relevance_window`, auto-prime, rules/escape-hatch text, usage tracking |
| 5 | `scripts/budget.py` + `scripts/token_count.py` | Ledger mechanics, spend refusal, cost table, counting backends |
| 6 | `scripts/capsule.py` + `scripts/session.py` | All persistent memory: schemas, render caps, recall ranking, archive lifecycle |
| 7 | `scripts/dedup.py` | `session_context` schema, hash-change semantics, scope keys |
| 8 | `scripts/directives.py` + `directives/*.md` | Guidance layer scoring, stable-dedup marker |
| 9 | `scripts/get_related.py`, `scripts/get_symbol.py`, `scripts/get_range.py` | Expansion primitives and their heuristics/limits (preview mode, end-line heuristics) |
| 10 | `scripts/from_log.py`, `scripts/compress_log.py` | Debug entry path |
| 11 | `scripts/usage.py`, `scripts/paths.py`, `scripts/safe_io.py`, `scripts/git_meta.py` | Accounting, scope/identity, concurrency, git metadata |
| 12 | `evals/basic.json`, `evals/answer_quality.json`, `scripts/eval.py`, `scripts/eval_answer_quality.py`, `scripts/benchmark_token_savings.py` | Current proof base to extend |
| 13 | `system_prompt.md` | The contract/rules the host agent is told to follow |

---

# 19. Final Verdict

## Scores (0–10)

| Dimension | Score | Rationale |
|---|---|---|
| Context Efficiency | **7** | Tiered loading, skeletons, windowing, log compression, directive budgets — strong token shaping; dinged by per-build dedup reset and duplicated memory sections |
| Context Accuracy | **5** | Right files for keyword-aligned tasks; lexical-only; naive imports; no content search |
| Project Awareness | **5** | Good anatomy map + commands; no architecture/decisions/issues sheet |
| Long-Term Memory | **4** | Append-only capsules/sessions, manual writes, capped keyword recall, no compaction/supersession |
| Retrieval Quality | **5** | Decent scoring + FTS bm25; fails synonyms, behavior queries, deep dependency chains |
| Cross-File Awareness | **4** | 1-hop LIKE-resolved imports only; no references/call graph; migrations ignored |
| Token Budgeting | **5** | Honest per-package ledger + downgrade policy; no model-window awareness, no adaptive allocation, estimate drift |
| Quality Preservation | **6** | Best-in-class guardrails at this maturity (freshness, partiality signals, escape hatches, strict mode, raw mode); silent skips remain |
| Scalability | **5** | Fine ≤10k files; structural walls beyond (full scans, LIKE scans, map.json growth, subprocess latency) |
| Observability | **6** | usage ledger, bootstrap status, dashboard, budget warnings; no per-item drop traces or retrieval explanations over time |
| Testability | **3** | Routing-hit evals + proxy benchmarks only; zero unit tests; fixtures depend on an absent sample project |
| Production Readiness | **5** | Solid install/lifecycle/multi-IDE plumbing, atomic IO, locks; unproven quality guarantees and no CI |

## CURRENT SYSTEM VERDICT

**FUNCTIONAL**

A dependable, well-designed LEVEL-3 context engine that measurably reduces tokens on keyword-aligned tasks in small/medium Python/TS repositories, with honest escape hatches that prevent catastrophic quality loss. It is not yet STRONG because retrieval depth, memory durability, and adaptive budgeting — the three pillars of a premium context layer — are all still heuristic-thin, and nothing *proves* quality preservation under reduction.

## BIGGEST RISK TO MODEL QUALITY

**Silent under-context from lexical-only retrieval plus budget downgrades.** When the interface contract, schema, or consumer of the code being edited shares no keywords with the task and no LIKE-resolvable import edge exists, the file simply never appears; and when the budget is tight, planned files are downgraded or skipped with only stderr traces. The host LLM then implements against an imagined contract — hallucinated interfaces and cross-file inconsistency — while the package still looks complete. Evidence: `route.find_relevant_files` (keyword scoring only), `agent.build_context_package` skip paths, `get_range` 50-line preview for >200-line primaries.

## BIGGEST TOKEN WASTE

**Re-sending identical context every turn.** `agent.build_context_package` calls `dedup --clear` at the start of each build, so the hash-aware cross-turn dedup machinery is never actually used across turns; unchanged primary files, map summaries, and session/capsule text (embedded twice — sections and generated system prompt) are paid for again on every request.

## MOST IMPORTANT ARCHITECTURAL IMPROVEMENT

**Give the engine structural and semantic sight: a symbol-reference/import graph plus content index feeding hybrid retrieval, and a persistent cross-turn context ledger.** Concretely: index file bodies and symbol usages; resolve imports for real; expand tasks through "who references these symbols"; and remember per-client what has already been delivered (by content hash) so turns ship deltas, not duplicates. This single upgrade converts the system from "keyword router with good manners" into a genuine context-intelligence layer, and it directly attacks both the biggest quality risk and the biggest token waste simultaneously.

---

# Appendix A — Answers to the Investigation Questions

1. **What is the project doing well?** Token shaping (tiers/skeletons/windowing), honest escape hatches, freshness guarantees, sufficiency transparency, directive layer, multi-IDE delivery, deterministic and dependency-free engineering.
2. **Biggest architectural weaknesses?** Lexical-only retrieval with naive import resolution; passive/manual memory; budget ledger disconnected from real model windows; per-build dedup reset; no tests.
3. **Where can LLM quality degrade from missing context?** Cross-file contracts (no reference graph), behavior questions (no content index), chat-referenced history (no ingestion), budget-skipped files, migrations (ignored), non-Py/TS symbols.
4. **Where are tokens wasted?** Per-turn full re-sends (dedup cleared), duplicated memory sections, routing metadata verbosity, re-sent map summaries.
5. **Is the summary/project-state sheet enough?** No — `map.json` covers structure only; capsules/sessions capture activity traces, not durable architecture/decisions/issues/constraints.
6. **Can it reliably recover omitted information?** Partially: `partial` flags, `expand_commands`, `get_*` tools and the bypass policy make recovery *possible*, but recovery depends on the host LLM noticing and acting; nothing verifies recovery happened.
7. **Can it identify cross-file/module dependencies?** Only 1-hop, regex-extracted, LIKE-resolved imports for Python/JS/TS. No call graph, no references, no schema/typing coupling.
8. **Does it distinguish local vs architectural tasks?** Rudimentarily — `complexity`/`op_type` keywords add risk notes and change model *recommendation* only; budget and strategy are unchanged.
9. **Is token allocation adaptive?** Mostly static: caller-supplied budget (default 8000) with fixed caps; opt-in `strict_quality`/`raw` are manual overrides, not adaptation.
10. **Is there "not enough context" detection?** Yes, partial: `build_context_sufficiency` (score/confidence/items proxy) + `confidence` levels + fallback flagging. It does not know *what* is missing (skips are invisible to the formula).
11. **Can the agent auto-request more context?** Semi-automatically: documented escape hatches, `expand_commands`, and the opt-in strict-quality loop; no autonomous agentic expansion by default.
12. **Can stale memory contaminate future work?** Yes — append-only stores, no supersession/expiry/revalidation; only branch annotations mitigate cross-branch confusion.
13. **Scale to 10k / 100k files / millions of LOC?** ~10k workable with degradation; 100k not viable without rework (LIKE scans, full-scan indexing, map growth); millions impossible as-is.
14. **Would it preserve a strong model's performance vs full manual context?** On keyword-aligned, single-domain tasks in small repos: largely yes. On cross-file, semantic, or history-dependent tasks: no — it can withhold exactly the files a human would have included.
15. **What prevents premium-grade status today?** (a) no structural/semantic retrieval, (b) no durable verified project-state memory, (c) no model-aware adaptive budgeting with cross-turn delta delivery, (d) no proof suite showing quality is preserved under reduction.

---

*End of audit. No code was modified during this investigation.*
