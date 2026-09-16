# Context Quality Evaluation

This document records the evaluation methodology and the before/after results
of the LEVEL 3 → LEVEL 5 premium upgrade. Raw machine-readable benchmark
output: [docs/bench_after.json](./bench_after.json). Baseline definition:
[docs/PREMIUM_UPGRADE_BASELINE.md](./PREMIUM_UPGRADE_BASELINE.md).

## 1. Methodology

Four independent layers, all deterministic and re-runnable:

1. **Routing eval** (`scripts/eval.py --file evals/basic.json`, run from the
   indexed project root): 8 fixture tasks with expected files. Metrics:
   `hit_rate` (expected file anywhere in top-5) and **top-1 accuracy**.
2. **Answer-quality eval** (`scripts/eval_answer_quality.py <project>`):
   9 question fixtures; measures byte-weighted **overlap** of Context Agent
   top-3 candidates vs. expected files, and **noise** (wrong-file share),
   benchmarked against dumping the whole repo.
3. **Token-savings benchmark** (`scripts/benchmark_token_savings.py`):
   full-repo cost vs. retrieved cost per query; per-query savings stats.
   Queries now include the two baseline gates: a migrations query and a
   large-file query.
4. **Test suites**: repo unit tests (`python -m pytest tests/`) and the
   sample-project testbed (`examples/sample_project`, `python -m pytest
   tests/`).

Quality rule (from the spec): **accuracy beats savings** — a retrieval
change is only accepted if routing/answer-quality gates stay green.

## 2. Baseline (LEVEL 3, before upgrade)

From `PREMIUM_UPGRADE_BASELINE.md` §3–§5:

| Metric | Baseline |
|---|---|
| Routing hit rate | 1.00 |
| Top-1 accuracy | **0.875** (7/8) |
| Answer-quality overlap | 1.00 (9 fixtures) |
| Avg per-query savings | 59.1% |
| Full repo cost (sample project) | ~3807 tokens |
| Known weaknesses | migrations/ blind spot; large-file blindness; cross-turn re-sends; lexical-only retrieval |

**Gate (baseline §5):** hit rate ≥ 1.00 AND top-1 0.875 → 1.00; testbed
pytest 12/12; all MCP tools functional; ledger reduces cross-turn repeat
tokens; migrations/ and large-file queue retrieved correctly.

## 3. Results after upgrade (LEVEL 5)

| Metric | Before | After | Verdict |
|---|---|---|---|
| Routing hit rate | 1.00 | **1.00** (8/8) | held |
| Top-1 accuracy | 0.875 | **1.00** (8/8) | gate passed |
| Answer-quality overlap | 1.00 | **1.00** (9/9) | held |
| Answer-quality noise | 0.00 | **0.00** | held |
| Avg per-query savings | 59.1% | **65.4%** (min 47.8%, max 87.3%) | improved |
| Full repo cost | 3807 | 4786 tokens (project grew: reporting/ + migrations now indexed) | n/a |
| Repo unit tests | 0 | **101 passed** | new |
| Testbed pytest | 12/12 | **12/12 passed** | gate passed |
| Ledger cross-turn savings | n/a | turn N→N+1: known 5→8 files, **421 → 1121 tokens saved** | gate passed |
| Migrations retrieval | blind spot | `migrations/0001_initial.py` **top-1** for the migration query | gate passed |
| Large-file handling | dumped/blind | 247-line file → `mode: preview` + `chunk_map` | gate passed |

### Retrieval regression fixed during benchmarking

The LEVEL 5 scoring overhaul initially regressed two fixtures
(`verify token` → test file top-1; `wrap a write…` → decorators instead of
`db/connection.py`). Root causes and principled fixes (no per-fixture
tuning):

* **FTS prefix false positives** — `wrap*` matched `wrapper`. Fix: separate
  exact-token (`"kw"`) and prefix (`kw*`) FTS layers with different points.
* **Symbol substring inflation** — scoring now requires the keyword to be an
  identifier token of the symbol name/signature.
* **Domain pattern inflation** — 9 patterns could add +45 to one file. Fix:
  `DOMAIN_CAP = 8` per file (patterns are recall aids, not rankers).
* **Test-file dominance** — ×0.85 damping for test files on non-test tasks
  (recall preserved, implementations rank top-1).
* **Generic verb exact-names** (`symbol:get` +12) and **plural mismatch**
  (`migration` vs `migrations`) — demotion and `_norm_plural` respectively.

Final routing evidence for the previously failing fixture: `logger factory`
now ranks `utils/logging.py` top-1 via summary exact-token (2×10) +
`get_logger` name-token matches (2×8).

## 4. Gate checklist (baseline §5)

| Gate | Status |
|---|---|
| Hit rate ≥ 1.00 | ✅ 1.00 |
| Top-1 accuracy 0.875 → 1.00 | ✅ 1.00 |
| Testbed pytest 12/12 | ✅ 12 passed |
| All MCP tools functional | ✅ 16 tools exercised in tests + E2E |
| Ledger reduces cross-turn repeat tokens | ✅ 421 → 1121 saved, known 5 → 8 |
| migrations/ retrieved | ✅ top-1 (`apply the latest database migration`) |
| Large-file queue retrieved | ✅ preview + chunk_map (247-line module) |

**All gates passed.**

## 5. Reproduce

```powershell
cd examples\sample_project
python .context\scripts\index.py
python ..\..\scripts\eval.py --file ..\..\evals\basic.json
python ..\..\scripts\eval_answer_quality.py .
python -m pytest tests/ -q                      # testbed (12)
cd ..\..
python -m pytest tests/ -q                      # repo (101)
python scripts\benchmark_token_savings.py examples\sample_project --json
```
