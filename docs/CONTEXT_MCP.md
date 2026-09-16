# Context MCP

Context MCP is the core product surface: a stdio JSON-RPC (MCP protocol
version `2024-11-05`) server that exposes Context Intelligence to any
MCP-capable IDE or coding agent. It is implemented in
`scripts/mcp_server.py`.

```
python .context/scripts/mcp_server.py          # run from the project root
```

Context MCP is the CORE component. It requires no external AI provider, no
API key, no Router MCP, and no Model Gateway, and it keeps full functionality
when those are absent or disabled. It is deterministic and local-first; the
engine internals it exposes are documented in
`docs/CONTEXT_INTELLIGENCE_ARCHITECTURE.md`. Component boundaries and the
optional routing layer are covered in `docs/COMPONENT_ARCHITECTURE.md`.

## 1. Responsibilities

Per the architecture spec, Context MCP owns: project and repository identity,
project memory, task state, content retrieval, structural/dependency graph,
context selection, the context ledger and decay, sufficiency and progressive
expansion, token budgeting, git state, project continuity, diagnostics, and
token optimization.

## 2. Tools (15)

One-line descriptions are maintained in `docs/OPERATIONS.md` section 2.
Grouped by workflow:

| Group | Tools |
|---|---|
| Lifecycle | `context_agent_status`, `context_agent_bootstrap_status`, `context_agent_index` |
| Task pipeline | `context_agent_route`, `context_agent_build_context` |
| Provider execution | `context_agent_execute` (`context_agent_one_shot` compatibility alias) |
| Project knowledge | `context_agent_memory`, `context_agent_directives`, `context_agent_capsule` |
| Lookup / expansion | `context_agent_search`, `context_agent_get_symbol`, `context_agent_get_range`, `context_agent_get_related`, `context_agent_from_log` |
| Verification / metrics | `context_agent_eval`, `context_agent_usage` |

Intended flow: `context_agent_route(task)` first (routing plan, confidence,
~5 files), then `context_agent_build_context(task)` for the ready-to-use,
token-budgeted package. `build_context` accepts `budget`, `model`,
`strict_quality`, `directive_budget`, `raw`, `diagnostics`,
`max_content_chars`, `detail`. When `budget` is omitted it is derived from
the model context profile and task type.

Routing results include the additive `machine_readable` analysis block
consumed by the optional Router layer; Context MCP itself does not depend on
that layer (see `docs/COMPONENT_ARCHITECTURE.md`).

## 3. Resources and prompts

Resources (`resources/list`):

| URI | Content |
|---|---|
| `context-agent://rules` | usage rules incl. bypass/escape guidance; also carries stable project directives |
| `context-agent://project-map` | compact project anatomy from `.context/map.json` |
| `context-agent://status` | index/budget/session status (`agent.py --status`) |
| `context-agent://bootstrap` | auto-bootstrap/index/memory priming health |
| `context-agent://capsule` | active task memory for long-session continuity |
| `context-agent://dashboard` | local dashboard URL info (`.context/dashboard.json`) |

Prompts (`prompts/list`): `context-agent-coding-task` (task, budget) and
`context-agent-error-log` (log).

## 4. Startup behavior

On `initialize`:

1. The MCP client name (`clientInfo.name`) becomes `CONTEXT_AGENT_CLIENT`
   and the IDE identity for this connection.
2. Every transport connection gets a distinct MCP connection id. The host
   conversation/session id is resolved to a model session; when the host
   supplies none, a connection-local conversation is created instead of
   reusing persisted project state.
3. Project-stable state uses the project scope, while mutable budget, dedup,
   capsule, and active-session state uses project + model-session runtime
   scope (see `docs/CROSS_IDE_CONFIGURATION.md`).
4. Auto-priming starts in a background thread (unless
   `CONTEXT_AGENT_AUTO_BOOTSTRAP=0`): it indexes the project when
   `symbols.db` or `map.json` is missing, then primes capsule and session
   context. Progress is written to `.context/mcp_bootstrap.json` and is
   visible via `context_agent_bootstrap_status` /
   `context-agent://bootstrap`.

If the workspace contains multiple plausible project roots, the server
returns a `project_root_selection_required` payload listing candidates; set
`CONTEXT_AGENT_PROJECT_ROOT` to one of them.

Script timeouts: 60 s default, 120 s for long-running scripts
(`agent`, `index`, `eval`, `from_log`), overridable via
`CONTEXT_AGENT_SCRIPT_TIMEOUT` or `CONTEXT_AGENT_<NAME>_TIMEOUT`.

## 5. Compact responses and escape hatches

`detail="compact"` (default) post-processes packages:

* at most 8 context items; extra items counted in `response_meta`;
* per-file content windowed to `max_content_chars` (default 3000) around
  relevance anchors from the task analysis; head-window fallback when no
  anchor matches; window metadata in `content_window`;
* partial items (windowed, outline, preview, symbol-only) are flagged with
  `partial: true`, `full_path`, and `expand_commands` so the host model can
  fetch full bodies in one step;
* repository text matching known prompt-injection patterns is annotated with
  `prompt_injection_warning` and must be treated as untrusted data;
* `raw=true` disables all token-saving (no skeleton, no windowing, no
  stable-directive dedup) and returns full file bodies.

`detail="full"` returns the raw package. `retrieval_diagnostics` appears only
when `diagnostics=true`; normal responses carry zero diagnostics overhead.

The rules resource states the operating policy explicitly: Context Agent is
the default first step, not a hard gate. On errors, low sufficiency, or
repeated misses, the host model bypasses the engine and reads files directly.

## 6. Usage accounting

Successful calls to `context_agent_route`, `context_agent_search`,
`context_agent_get_symbol`, `context_agent_get_range`,
`context_agent_get_related`, `context_agent_from_log`, and
`context_agent_capsule` are recorded through `usage.py` with `kind=tool`
(response-size token estimate). Full package builds are recorded by
`agent.py` itself; `context_agent_usage` returns the per-client summary.

## Two independent project features

Each project owns these switches in `.context/config.json`:

1. `context.optimization_enabled` (default `true`) — the IDE model remains the
   executor and receives selected compact context instead of a repository dump.
2. `routing.enabled` (default `false`) — `context_agent_execute` uses compact
   context and routes the task to the cheapest project-allowed provider model
   that satisfies the task's quality, risk, capability, and cost guardrails.

Provider credentials, provider availability, and model inclusion are
machine-level. Disabling a model removes it from routing for every project.
The two feature switches remain project-level. Provider routing requires
context optimization; turning context optimization off also disables provider
routing in the dashboard.

## OpenCode Zen provider execution

`context_agent_execute` creates a fresh isolated model session, builds the
same compact quality-checked context package, lets the router select a model,
and sends exactly one inference request. It does not directly edit files or
run a model tool loop; it returns code/patch for the IDE agent to apply and
verify. If context
sufficiency is false, the provider request is blocked unless
`allow_insufficient=true` is explicitly supplied.

OpenCode Zen is shipped as a disabled optional provider. Store its API key in
the existing secret store, then enable it:

```powershell
python .context/scripts/secret_store.py --set opencode
python .context/scripts/router.py providers enable --id opencode
```

The curated free Zen models are available under `opencode/...`, including
`opencode/big-pickle`, `opencode/mimo-v2.5-free`, and
`opencode/muse-spark-1.3-contributor-free`. Free Zen models may use submitted
data for model improvement; the one-shot result carries this privacy notice.

## 7. Environment variables

| Variable | Effect |
|---|---|
| `CONTEXT_AGENT_PROJECT_ROOT` (or legacy `MCP_PROJECT_ROOT`) | pins the project root; resolves multi-root ambiguity |
| `CONTEXT_AGENT_CLIENT` | client label for usage accounting (auto-set from MCP `clientInfo`) |
| `CONTEXT_AGENT_SCOPE` | overrides the default project-stable scope |
| `CONTEXT_AGENT_SESSION` | pins the ledger session id |
| `CONTEXT_AGENT_RUNTIME_SCOPE` | overrides mutable per-session state scope |
| `CONTEXT_AGENT_MODEL_SESSION_ID` | pins the host model-session identity |
| `CONTEXT_AGENT_CONVERSATION_ID` | reconnect-stable conversation identity |
| `CONTEXT_AGENT_AUTO_BOOTSTRAP` | `0`/`false` disables auto-priming |
| `CONTEXT_AGENT_SCRIPT_TIMEOUT`, `CONTEXT_AGENT_<NAME>_TIMEOUT` | subprocess timeout overrides |
| `CONTEXT_AGENT_RAW` | `1`/`true` forces fidelity mode in builds |

## 8. Related documents

* `docs/OPERATIONS.md` - installation, indexing, CLI usage, tool table,
  troubleshooting.
* `docs/CONTEXT_INTELLIGENCE_ARCHITECTURE.md` - retrieval, ledger,
  sufficiency, and storage design behind every tool.
* `docs/COMPONENT_ARCHITECTURE.md` - core/optional component boundaries.
* `docs/CROSS_IDE_CONFIGURATION.md` - identity and scope behavior that makes
  Context MCP state survive IDE changes.
