# Migration Guide — LEVEL 3 → LEVEL 5

Upgrading an existing Context Agent installation. The upgrade is **additive
and backward compatible**: no destructive DB migrations, no removed CLI
flags, no changed MCP tool signatures.

## 1. What changes

| Area | Change |
|---|---|
| `scripts/*.py` | Engine upgraded (route scoring, ledger, planner, sufficiency, memory 2.0, model profiles, git_meta, content index, graph, chunk_map previews) |
| `.context/symbols.db` | New tables auto-created on first index (`content_chunks`, `chunks_meta`, `edges`, `graph_meta`, `context_ledger`, `ledger_sessions`, `project_memory`) |
| `.aiignore` template | `migrations/` is **no longer ignored** (schema/contract files are retrieval-critical). Existing projects must remove the line manually if present |
| MCP schema | `context_agent_build_context` gains optional `diagnostics` boolean (default `false`) — legacy clients unaffected |
| CLI | New optional flags only (`--diagnostics`, `--new-session`, `--skip-usage`, ...). Existing invocations unchanged |

## 2. Upgrade steps

```powershell
# 1) Refresh engine scripts (re-running the installer copies the new set)
python <repo>\scripts\install.py <project-root>

# 2) Check your .aiignore: remove a "migrations/" line if present
#    (LEVEL 5 indexes migrations on purpose)

# 3) Rebuild the index (incremental is fine; --force for a clean rebuild)
cd <project-root>
python .context\scripts\index.py          # or: index.py --force

# 4) Verify
python .context\scripts\agent.py --status
```

The index step creates the new tables automatically
(`CREATE TABLE IF NOT EXISTS`), builds the content-chunk index and the
structural graph, and leaves existing `files`/`symbols` rows intact.

## 3. Behavioral changes to expect

* **Cross-turn reuse**: repeated `build_context` calls in the same session
  now return `known_to_model` markers instead of re-sending unchanged
  files. To force a full resend start a fresh session
  (`--new-session <name>`).
* **Large files (> 200 lines)** arrive as `mode: preview` + `chunk_map`
  with `get_cmd` entries instead of full bodies. Fetch exact ranges with
  `get_range`.
* **Migrations files are indexed** and can win routing for database tasks.
* **Optional diagnostics**: request `diagnostics=true` to see
  included/known/omitted explanations; default output stays lean.

## 4. Rollback

Roll back by restoring the previous `scripts/` directory. The new DB tables
remain but are inert without the LEVEL 5 code; no data loss either way.
Ledger/memory state survives both directions.

## 5. Post-upgrade checks

1. `python -m pytest tests/ -q` in the engine repo — expect all green.
2. Run `scripts/eval.py` against your eval fixtures — hit rate and top-1
   must not regress. Keep evaluation inputs and results with the change when
   a retrieval behavior change needs to be reviewed.
3. Two consecutive `build_context` calls with the same task: the second
   should report `known_count > 0` in `context_reuse`.
