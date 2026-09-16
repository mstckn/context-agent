# PREMIUM_UPGRADE_BASELINE.md

Phase 0 baseline for the premium Context Intelligence Layer upgrade.
Recorded **before** any architectural changes, per `promt.txt` PHASE 0.

- Date: 2026-08-30
- Environment: Windows, Python 3.11.0, pytest 7.4.3
- tiktoken: **not installed** → token counting uses the heuristic (words×1.3)
- Source of diagnosis: `CONTEXT_SYSTEM_AUDIT.md` (verified against current code)

---

## 1. Testbed

The repository shipped without any runnable sample project: `evals/basic.json`
and `evals/answer_quality.json` reference modules (`auth/`, `payments/`,
`analytics/`, `db/`, `utils/`, `cli/`) that did not exist anywhere in the repo,
so legacy evals had nothing realistic to measure against.

Created `examples/sample_project/` — a small but realistic multi-module app:

| Module | Role |
|---|---|
| `auth/` | tokens (contract), service, decorators, user model |
| `payments/` | invoices/refunds, depends on `db` + `analytics` |
| `analytics/` | event tracking |
| `db/` | connection pool + `transaction()` contract |
| `jobs/` | job queue (~296 lines; idempotency/lock logic near the END of the file) |
| `api/` | HTTP handlers producing the `{"user_id", "token"}` response contract |
| `frontend/` | TypeScript `api_client.ts` (consumer of the API contract) + `Button.tsx` |
| `cli/` | command table |
| `migrations/` | `0001_initial.py` (schema DDL — consumers: db/payments/auth/jobs) |
| `tests/` | pytest suite (12 tests) |

Properties deliberately built in:

- Cross-file contract chain: `api/handlers.py` → `frontend/api_client.ts`
  (both use the `user_id` field; renaming is a breaking change).
- DB contract chain: `db/connection.transaction` used by `auth`, `payments`, `jobs`.
- `jobs/queue.py` relevant logic near end of a large file (scenario E).
- Idempotency wording ("prevent running the same logical job twice") that does
  NOT contain the phrase "duplicate job execution" (semantic scenario I).
- `migrations/` directory — **ignored by the legacy indexer** (blind spot).

Testbed health: `python -m pytest tests -q` → **12 passed**.

## 2. Pre-existing bugs fixed to make the baseline measurable

Only two minimal fixes were required just to run the legacy benchmarks at all
(documented here; both are legacy defects, not upgrade changes):

1. `scripts/benchmark_token_savings.py` — `count_full_repo_tokens` matched the
   SKIP set against **absolute** path parts; any project living under a
   directory named `.context` (this repo) counted 0 files → `ZeroDivisionError`.
   Fixed to use project-relative parts. Also guarded the `full_tokens == 0`
   division.
2. `scripts/eval_answer_quality.py` — `ensure_installed` invoked `install.py`
   from the *installed* copy onto itself → `shutil.SameFileError`; now skips
   self-install when already installed. Same absolute-path SKIP bug in
   `read_full_repo` fixed.

## 3. Baseline measurements (legacy system)

All measured on `examples/sample_project` (24 indexed files, 3,826 tokens in
index; 3,807 counted by the benchmark walker).

### 3.1 Routing quality (`scripts/eval.py`, `evals/basic.json`, top-5)

| Metric | Value |
|---|---|
| Hit rate (any expected file in top-5) | **8/8 = 1.00** |
| Top-1 accuracy | **7/8 = 0.875** |
| Wrong top-1 case | "wrap a write in a database transaction" → `auth/decorators.py` ranks above `db/connection.py` (symbol `wrapper` keyword match beats the actual contract) |

### 3.2 Answer-quality proxy (`scripts/eval_answer_quality.py`)

| Metric | Value |
|---|---|
| Overlap with ground truth (9 fixtures) | 1.00 on all |
| Noise delta vs full-repo dump | 0.00 |

**Note:** the testbed is small enough that this eval saturates (top-3 always
contains the answer). It does not discriminate quality at this scale; the
premium phase must add a harder suite (Phase 18 requirement).

### 3.3 Token economics (`scripts/benchmark_token_savings.py`)

| Metric | Value |
|---|---|
| Full repo tokens | 3,807 |
| Retrieved tokens (3 queries summed) | 4,672 |
| Avg per-query savings | 59.1% |
| Per-query retrieved | 1,720 / 1,722 / 1,230 |

Observation: summed retrieved tokens **exceed the whole repo** — the same files
are re-sent across queries with no cross-turn/cross-query reuse. This is the
token-waste the context ledger must eliminate.

### 3.4 Build latency (`scripts/agent.py`, one `build_context_package`)

| Metric | Value |
|---|---|
| Task | "verify an authentication token", budget 6000 |
| Wall-clock latency | **7.67 s** on a 24-file project |
| Package JSON size | 58,966 bytes (~15k tokens of JSON envelope) |

The latency is subprocess orchestration (dozens of `python` spawns per build).
Evidence for PHASE 20 (performance) work.

### 3.5 Baseline artifacts

- `baseline_bench.json` — benchmark raw output
- `baseline_answer_quality.json` — answer-quality eval output
- `baseline_latency.txt` — latency measurement
- `examples/sample_project/.context/evals/answer_quality_results.json`

## 4. Legacy weaknesses re-confirmed in code (with evidence)

| # | Weakness | Evidence |
|---|---|---|
| 1 | Per-build dedup wipe defeats cross-turn reuse | `agent.py` `build_context_package` calls `run_script("dedup", "--clear")` on every build |
| 2 | `migrations/` and `fixtures/` never indexed | `index.py` `DEFAULT_IGNORE["dirs"]` |
| 3 | Large-file blindness: no range → first 50 lines only | `get_range.py` `get_range()`: `if total_lines > 200: preview = lines[:50]` |
| 4 | Import resolution = LIKE string matching | `route.py` `find_neighbor_files`, `get_related.py` |
| 5 | No code-body/content search | FTS tables cover only path/summary/symbol name+docstring+signature (`index.py` `init_db`) |
| 6 | Budget has no model-window awareness | `budget.py`: fixed `max_tokens`, caller-supplied; no output reserve / tool overhead |
| 7 | No chat-history ingestion | no script reads conversation events; memory only via explicit capsule/session calls |
| 8 | `verification[]` capsule field is dead schema | `capsule.py` never writes it |
| 9 | Zero unit tests | no `tests/` dir, no `test_*` files in repo (before this phase) |

## 5. Baseline gate for the upgrade

The premium system must, at minimum:

- keep `evals/basic.json` hit rate ≥ 1.00 AND improve top-1 accuracy (0.875 → 1.00),
- keep testbed pytest green (12/12),
- keep all existing MCP tools working,
- reduce repeated-context tokens across turns (ledger),
- retrieve `migrations/` and large-file tail content correctly,
- and do all of the above with the new automated test suite passing.
