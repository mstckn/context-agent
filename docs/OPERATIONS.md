# Operations Manual

Day-2 operations for the LEVEL 5 Context Agent: installation, indexing,
MCP integration, diagnostics, sessions, and troubleshooting.

## 1. Installation & indexing

```powershell
# Install into a project (copies scripts, writes default .aiignore)
python <engine>\scripts\install.py <project-root>

# Build/refresh the index (files, symbols, FTS, content chunks, graph)
cd <project-root>
python .context\scripts\index.py            # incremental
python .context\scripts\index.py --force    # clean rebuild
python .context\scripts\index.py --stats    # index statistics
```

Indexing is hash-incremental; only changed files are re-parsed. Content
chunks and the structural graph rebuild lazily for changed files.

## 2. MCP integration

The server exposes 15 tools (`scripts/mcp_server.py`, stdio):

| Tool | Purpose |
|---|---|
| `context_agent_status` | engine/index health |
| `context_agent_bootstrap_status` | first-run guidance |
| `context_agent_index` | trigger (re)indexing |
| `context_agent_route` | task routing plan only |
| `context_agent_build_context` | full context package; optional `diagnostics: true`, `strict_quality`, `raw`, `budget`, `model` |
| `context_agent_directives` | playbook/directive selection |
| `context_agent_memory` | Project Memory 2.0 CRUD/state sheet |
| `context_agent_search` | content-level search (chunks) |
| `context_agent_get_symbol` | symbol body by name |
| `context_agent_get_range` | exact line range / large-file preview (`chunk_map`) |
| `context_agent_get_related` | structural graph neighbors |
| `context_agent_from_log` | ingest context from logs |
| `context_agent_eval` | run eval fixtures |
| `context_agent_capsule` | capsule context |
| `context_agent_usage` | usage/token accounting |

Compact output passes through `git_context`, `chunk_map` and (when
requested) `retrieval_diagnostics`.

## 3. CLI usage

```powershell
python .context\scripts\agent.py "add rate limiting to the login endpoint"
python .context\scripts\agent.py "<task>" --budget 6000 --model claude-sonnet
python .context\scripts\agent.py "<task>" --diagnostics      # explainability
python .context\scripts\agent.py "<task>" --new-session hotfix-42
python .context\scripts\agent.py "<task>" --skip-usage       # preview/no accounting
python .context\scripts\route.py "<task>" --top 5 --budget 4000
```

## 4. Sessions & the ledger

* The ledger is scoped by `(scope, session)`. Start fresh contexts with
  `--new-session NAME`; reuse the default session for multi-turn work to
  benefit from `known_to_model` skips.
* Unchanged files are referenced, not re-sent. Hash changes force resend
  (`content_changed`). Decayed items refresh cheaply or re-send when they
  fall out of the window.
* Inspect: `context_reuse` section in the package
  (`turn`, `known_count`, `tokens_saved`).

## 5. Diagnostics & observability

* `--diagnostics` / MCP `diagnostics: true` → `retrieval_diagnostics`
  (included + reasons, known_to_model, omitted + reasons). Off by default:
  zero overhead in normal context.
* Every routed file carries point-level `reasons`.
* `context_agent_usage` tracks token accounting per build.
* Budget warnings and partiality signals (`partial`, `expand_commands`)
  remain in every package.

## 6. Quality verification (run after changes)

```powershell
cd examples\sample_project
python ..\..\scripts\eval.py --file ..\..\evals\basic.json   # routing gates
python ..\..\scripts\eval_answer_quality.py .                # 9 fixtures
python -m pytest tests/ -q                                   # testbed
cd ..\..
python -m pytest tests/ -q                                   # engine suite
python scripts\benchmark_token_savings.py examples\sample_project --json
```

Acceptance thresholds: hit rate 1.00, top-1 ≥ 0.875, answer-quality
overlap 1.00, testbed 12/12.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ProjectRootSelectionRequired` | eval/agent run outside an indexed root | run with cwd = project root |
| Expected file never routed | `.aiignore` excludes it (e.g. old `migrations/` line) | remove pattern, re-index |
| FTS returns nothing for a new file | incremental index skipped it / stale FTS | `index.py --force` |
| Big file consumes budget | preview threshold bypassed by `raw` mode | drop `raw`, use `chunk_map` + `get_range` |
| Repeated turns resend everything | different session per call | reuse session / `--new-session` once |
| PowerShell garbles UTF-8 output | console codepage | `python -X utf8`, or `Out-File -Encoding utf8` |
| Old `get_range` copy lacks `--preview` | testbed scripts out of sync | re-copy `scripts\*.py` into `.context\scripts\` |

## 8. Maintenance notes

* Keep `.context/scripts/` copies in sync with the engine repo after
  upgrades (the installer does this).
* The DB is a single file (`.context/symbols.db`); back up together with
  the repo. Deleting it is safe — `index.py --force` rebuilds everything.
* Memory 2.0 hygiene: `memory.py --list --status ACTIVE`; stale entries are
  auto-detected by deterministic revalidation, history is never deleted.

## 9. Control plane: dashboard, Router MCP, Model Gateway

```powershell
python .context\scripts\dashboard_server.py --port 8765      # control-plane UI on 127.0.0.1
python .context\scripts\router.py status                     # router/provider/gateway feature detection
python .context\scripts\model_gateway.py start --port 8791   # optional AUTO ROUTER gateway (127.0.0.1)
```

* The dashboard is the shared control plane and works without the
  router: `NOT ENABLED` is never an error, and IDE MODEL mode needs no
  API keys. See `docs/DASHBOARD_CONTROL_PLANE.md`.
* The Router MCP is an optional stdio server exposing five tools
  (`route_task`, `delegate_task`, `execute_with_model`,
  `get_model_candidates`, `explain_route`); it returns `NOT_ENABLED`
  while routing is disabled. See `docs/ROUTER_MCP.md`.
* The Model Gateway is an optional OpenAI-compatible endpoint
  (`/healthz`, `/v1/models`, `/v1/chat/completions`) bound to
  `127.0.0.1`, default port 8791, registry
  `~/.context-agent/gateway.json`. See `docs/MODEL_GATEWAY.md`.
