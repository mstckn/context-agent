# Context Intelligence Architecture (LEVEL 5)

> Deterministic, LLM-free context engine. Mission: **MINIMUM SUFFICIENT
> CONTEXT WITH QUALITY PRESERVATION** — when accuracy and savings conflict,
> accuracy wins.

## 1. Positioning

The engine sits between the IDE / coding agent and the host LLM. It decides,
per task and per turn:

* WHAT the model needs (task-aware retrieval),
* what the model ALREADY KNOWS (cross-turn context ledger),
* what HAS CHANGED (freshness hashes + git signals),
* what can safely be SUMMARIZED (tiered actions, outlines, previews),
* what must be provided RAW (`raw` mode, primary files),
* and WHEN MORE CONTEXT IS REQUIRED (sufficiency engine + auto-expansion).

No LLM runs inside the engine. Every step is reproducible and testable.

## 2. Architecture

```
IDE / Coding Agent
        |
        v
+----------------------------+
| MCP / Context API          |   mcp_server.py (16 tools, compact pass-through)
+----------------------------+
        |
        v
+----------------------------+
| Task Intelligence          |   route.classify_task + planner.plan_task
| - intent / op_type         |
| - complexity / risk        |
| - evidence requirements    |
+----------------------------+
        |
        v
+----------------------------+
| Context Planner            |   agent.build_context_package
| - adaptive budget          |   model_profile.ModelContextProfile
| - known-to-model ledger    |   ledger.ContextLedger (decay + freshness)
| - progressive expansion    |   sufficiency engine (auto re-retrieval)
+----------------------------+
        |
   +----+---------------------+---------------------+
   v                          v                     v
Lexical Retrieval      Structural Graph      Content Search
(FTS5 exact/prefix,    (edges: imports /    (content_chunks FTS5,
 symbol tokens,         calls / refs via     chunk-level hits with
 LIKE recall)           graph.py)            file attribution)
   |                          |                     |
   +--------------------------+---------------------+
                              |
                              v
                    +--------------------+
                    | SQLite symbols.db  |  single file, stdlib-only
                    +--------------------+
                              |
              +---------------+----------------+
              v               v                v
        Git Signals     Project Memory   Explainability
        (git_meta.py)   (memory.py 2.0)  (retrieval_diagnostics,
        branch/dirty/   typed, dated,    opt-in via --diagnostics)
        changed/recent  superseded
```

### Pipeline (one build)

1. **Task analysis** — keywords, domains, op_type, complexity.
2. **Routing** — layered scoring over FTS/symbols/paths/graph (below).
3. **Session context** — capsule/session state.
4. **Item loading** — per file: freshness hash → ledger decision
   (send / skip / refresh) → dedup → budget gate → tier action
   (`load_file` / `load_symbols` / `load_outline`) → content, outline,
   or **preview + chunk_map** for large files.
5. **Git-aware boost** — dirty/recently changed files inserted right after
   the primary block (see §6).
6. **Quality & sufficiency** — `context_quality_score`, optional strict
   quality loop, sufficiency engine with automatic expansion turn.
7. **Budget accounting** — model-aware adaptive budget (see §7).
8. **Ledger record** — every sent representation is recorded with its
   content hash for future turns.

## 3. Storage schemas (`.context/symbols.db`)

All tables are additive (`CREATE TABLE IF NOT EXISTS`); no destructive
migrations ever.

| Table | Purpose |
|---|---|
| `files` | path, hash, lang, line_count, summary, token_estimate, indexed_at |
| `symbols` | name, type, file, start/end line, signature, docstring |
| `imports` | raw extracted import edges per file |
| `files_fts` | FTS5 over path + summary (bm25 ranked) |
| `content_chunks` | chunk-level FTS index of file bodies (content search) |
| `chunks_meta` | chunk boundaries / hashes for incremental rebuild |
| `edges` | resolved structural graph: imports / calls / refs with kind + confidence |
| `graph_meta` | graph build state |
| `context_ledger` | per (scope, session, file, representation): content_hash, token_cost, turn_sent, last_seen |
| `ledger_sessions` | session bookkeeping for decay windows |
| `project_memory` | typed memories (Memory 2.0) |
| `session_context` | per-build dedup keys |

## 4. Retrieval scoring (route.py)

Deterministic, layered, explainable (every point has a recorded reason).

| Layer | Points | Rationale |
|---|---|---|
| Direct path segment (`kw == stem` or token in path parts, plural-normalized) | **24** | strongest structural signal; `migration` ≈ `migrations` |
| Exact-name symbol match (name longer than 3 chars) | **12** | the target symbol itself; generic verbs (`get/set/run`) excluded from the bonus |
| Path substring match | **10** | |
| Summary exact-token match (FTS `"kw"`) | **10** | precision layer — full token in summary/content |
| Symbol name-token match (`kw` is a token of the symbol name) | **8** | `get_logger` contains `logger` |
| Symbol signature-token / prefix-only FTS match | **7 / 5** | recall layers |
| file/summary LIKE recall pass | **4** | keeps recall broad |
| Domain patterns (TASK_PATTERNS) | up to **+8 per file** (`DOMAIN_CAP`) | recall aid only — never a ranking inflator |
| Nearby test file (test tasks) | +6 | auxiliary evidence |
| Test-file damping (non-test tasks) | ×0.85 | tests stay in the set but implementations rank top-1 |

Design rules proven by the eval suite:

* **Exact token vs prefix** are distinct FTS queries (`"kw"` vs `kw*`) so
  `wrap*` no longer credits `wrapper`.
* **Token-based symbol filter**: substring LIKE keeps recall, but scoring
  requires `kw` to be an identifier token of the name/signature.
* **Plural normalization** (`_norm_plural`) and **generic-verb demotion**
  prevent both false negatives (`migration`/`migrations`) and false
  positives (`get` as a function name flooding scores).

## 5. Context lifecycle (cross-turn ledger)

```
never sent ─────────────► SEND (load, record hash+cost)
sent, unchanged, fresh ──► SKIP  (known_to_model marker + expand_commands)
sent, unchanged, decaying► REFRESH (cheap skeleton keeps it in window)
sent, HASH CHANGED ──────► SEND   (content_changed; freshness wins)
decayed out of window ───► SEND   (expired_from_window)
requested repr > known ──► SEND   (representation_upgrade)
```

Decay states are deterministic functions of turns elapsed. The ledger is the
engine's main economics lever: unchanged context is referenced, not re-sent.

## 6. Git-aware retrieval (PHASE 13)

`git_meta.py` reads `branch`, `head`, dirty status, changed files
(porcelain parse incl. renames) and recently changed files.

* A `git_context` section is attached to the package.
* Dirty/relevant changed files are **boosted immediately after the primary
  block** so they survive top-N slicing; directories, `.context/` paths and
  noise extensions (`.pyc/.log/.lock/.min.js`, node_modules) are filtered;
  at most 3 additions; never duplicates an existing entry.

## 7. Adaptive token budgeting (PHASE 11)

`model_profile.ModelContextProfile` maps models to real windows
(`context_window`, `safe_input_budget`, `reserved_output`, ...) with a
conservative default profile. The planner allocates budget by task type and
risk; per-item estimates are action-specific (full file vs outline vs
preview), and overflow downgrades `load_file → load_symbols` before
skipping. Skips are always explainable.

## 8. Large files (blind spot closed)

Files > 200 lines are never dumped whole into context. `get_range` in
preview mode returns:

* a **symbol map** from `symbols.db` (or a coarse 150-line grid fallback),
* each entry carries a ready-to-run `get_cmd`
  (`python .context/scripts/get_range.py <file> <start> <end>`).

The compact MCP output passes `chunk_map` through, so the host model can
pull exactly the range it needs.

## 9. Explainability (PHASE 16)

* Every retrieved item carries `reasons` (point-level).
* `retrieval_diagnostics` (included / known_to_model / omitted with
  reasons) is produced **only** when `diagnostics=True`
  (CLI `--diagnostics`, MCP `diagnostics` param) — normal model context
  pays zero overhead.

## 10. Project Memory 2.0 (PHASE 8)

Typed, dated, lifecycle-managed memory in `project_memory`:

* Types: `ARCHITECTURAL, DECISION, TASK, CONSTRAINT, KNOWN_ISSUE,
  USER_CORRECTION, RUNTIME, EPHEMERAL`
* Statuses: `ACTIVE → SUPERSEDED / STALE / INVALIDATED`
* Supersession keeps history (`superseded_by`); deterministic revalidation
  marks memories `STALE` when their code anchors vanish; compaction prunes
  only `EPHEMERAL`/`RUNTIME`.
* A prioritized state sheet (CONSTRAINT → DECISION → ARCHITECTURAL → ...)
  ships in context within a fixed character budget.

## 11. Determinism & backward compatibility

* No LLM calls inside the engine; all behavior reproducible.
* SQLite single-file storage, Python stdlib only.
* All CLI flags and MCP schemas are additive (new optional params default
  to legacy behavior).
* Escape hatches preserved: `raw` mode (full fidelity), `strict_quality`
  loop, manual `get_range/get_symbol/get_related` expansion commands.
