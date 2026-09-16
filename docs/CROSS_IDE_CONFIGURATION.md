# Cross-IDE Configuration and Continuity

Project state must follow the project, not the IDE and not the filesystem
path. This document covers the identity system (`scripts/identity.py`,
`scripts/paths.py`) and the layered configuration store
(`scripts/config_store.py`). Session and ledger mechanics are documented in
`docs/OPERATIONS.md` (sections 4); engine-side memory in
`docs/CONTEXT_INTELLIGENCE_ARCHITECTURE.md`.

## 1. Identity model

Eight logical identities, all resolvable from `.context/` plus live git
state:

| Identity | Source | Stability |
|---|---|---|
| PROJECT_ID | `.context/project_id.json` (created on first use; `CONTEXT_AGENT_PROJECT_ID` env overrides) | survives moves, clones, mount changes |
| REPOSITORY_ID | sha1 of normalized git remote (`repo-...`); falls back to project-id when no remote | same across checkouts/clones of one repo |
| CHECKOUT_ID | uuid persisted in `.context/identity.json` | unique per working copy/worktree |
| BRANCH / REVISION | live git (`git_meta`) | current state |
| IDE_INSTANCE_ID | registry in `identity.json` keyed by `<ide>@<host>` (`ide_detector` auto-detects; `CONTEXT_AGENT_IDE` forces type; `CONTEXT_AGENT_IDE_INSTANCE_ID` pins) | stable per IDE installation, distinct per IDE on one machine |
| CONVERSATION_ID | env `CONTEXT_AGENT_CONVERSATION_ID`, else bridge to the active session, else persisted in `identity.json` | per chat/thread |
| MODEL_SESSION_ID | ledger session key (`CONTEXT_AGENT_SESSION` or active session; see `agent.ledger_session_id`) | per model session |
| TASK_ID | `CONTEXT_AGENT_TASK_ID` env or freshly generated | per task invocation |

Rules implemented:

* Path is never the sole identity; project identity travels with `.context/`.
* `identity.json` writes are atomic (temp file + replace) with an optimistic
  version check, so concurrently open IDEs never silently overwrite each
  other; writers retry on version conflict.
* Identity digests are read-only: `identity.py` never mutates ledger,
  memory, or session state (ledger/memory reads use read-only SQLite
  connections).

CLI:

```
python .context/scripts/identity.py --show                     # full identity block
python .context/scripts/identity.py --continuity               # continuity report
python .context/scripts/identity.py --handoff [--session NAME] # SessionHandoff payload
python .context/scripts/identity.py --scope-for LEVEL          # scope key for a level
python .context/scripts/identity.py --new-conversation         # rotate conversation id
```

## 2. Continuity report (VERIFIED / PARTIAL / MISSING)

`identity.py --continuity` reports `CROSS_IDE_CONTINUITY` with per-check
proof:

| Check | Source | Statuses |
|---|---|---|
| `project_id` | `.context/project_id.json` | present / missing |
| `repository_id` | `identity.json` or derivable from project id | present / missing |
| `checkout_id` | `identity.json` | present / derivable |
| `branch` | git | present / not_applicable |
| `ide_instance` | `identity.json` registry | present / derivable |
| `session_store` | `.context/sessions/<scope>/` | present / empty |
| `scope_stability` | project-id-derived scope | present / missing |

Required checks: `project_id`, `repository_id`, `scope_stability`.

* VERIFIED - all required present and no optional check missing.
* PARTIAL - required present but some optional check missing, or only part of
  the required set present.
* MISSING - all required checks absent; the report advises running any
  Context Agent command to create identity anchors.

The report also lists `missing_required` and advice text, so a PARTIAL result
shows exactly which pieces to implement.

## 3. SessionHandoff

`identity.py --handoff` produces everything another IDE/session needs to
continue without re-deriving project knowledge: `handoff_version`,
`generated_at`, full identity block, session digest (name, recent decisions,
notes, touched files), model-session ledger digest, and ACTIVE project-memory
counts by type.

Project Memory and Model Knowledge stay separated (`semantics` block):

* `project_memory_is_model_knowledge: false` - project knowledge travels with
  the project and is shared across IDEs.
* `known_to_model_transferable: false` - ledger state ("file X already sent")
  belongs to one model session only. A new session starts with an empty
  ledger and is never told it already saw anything.

## 4. Scope levels

`identity.py --scope-for LEVEL` maps isolation levels to concrete scope keys.
All narrower levels compose with the project-stable base key
(`project_scope_key`), so branch/task state never leaks upward:

| Level | Key |
|---|---|
| PROJECT_GLOBAL | base project scope |
| REPOSITORY | base + repository id |
| CHECKOUT | base + checkout id |
| BRANCH | base + branch name (`-nobranch` when git unavailable) |
| TASK | base + task id |
| CONVERSATION | base + conversation id |
| MODEL_SESSION | base + session id |

Branch-scoped state therefore cannot contaminate other branches, and
worktrees of the same repo share project identity while keeping distinct
checkout identities.

## 5. Layered configuration (config_store.py)

Five layers, highest wins: `SYSTEM -> GLOBAL -> PROJECT -> SESSION -> TASK`.

| Layer | Storage |
|---|---|
| SYSTEM | built-in defaults, never written to disk |
| GLOBAL | `~/.context-agent/config.json` (all projects on this machine) |
| PROJECT | `<root>/.context/config.json` (travels with the project) |
| SESSION | `<root>/.context/sessions/<scope>/[<session>/]config.json` |
| TASK | runtime overrides, never persisted |

SYSTEM defaults (safe starting point, zero keys required):

| Key | Default |
|---|---|
| `execution.mode` | `IDE_MODEL` (alternatives: `HYBRID`, `AUTO_ROUTER`) |
| `routing.enabled` | `false` |
| `routing.policy_mode` | `SMART` |
| `routing.escalation_enabled` | `true` |
| `budget.default_tokens` | `8000` |
| `budget.strict_quality` | `80` |
| `cost.guardrail_action` | `WARN` |
| `cost.task_limit_usd` / `daily` / `monthly` | `1.0` / `5.0` / `50.0` |
| `context.diagnostics` | `false` |
| `context.auto_index` | `true` |

CLI:

```
python .context/scripts/config_store.py --effective [--key K] [--session S]
python .context/scripts/config_store.py --show LAYER
python .context/scripts/config_store.py --set LAYER KEY VALUE [--session S]
python .context/scripts/config_store.py --reset LAYER [KEY] [--session S]
```

* Every effective value reports its `source` layer and whether the key is a
  known SYSTEM key (VIEW EFFECTIVE CONFIGURATION).
* `--reset` implements RESET TO INHERITED: it removes one key (or a whole
  layer) so the inherited value applies again. Task overrides never become
  defaults.
* SYSTEM and TASK layers are not writable through `--set`/`--reset`; TASK
  overrides are passed at call time only.
* File writes are atomic, matching the `identity.json` concurrency model.

Routing preferences live at PROJECT/GLOBAL layers, not inside any IDE:
opening the same project from another IDE keeps its execution mode and
routing configuration. IDE MODEL is the default everywhere, and enabling
routing later never requires changing the core (see
`docs/COMPONENT_ARCHITECTURE.md`).

## 6. What travels across IDEs

| Follows the project | Does NOT transfer |
|---|---|
| project identity and scope (`project_id.json`) | `known_to_model` ledger state (per model session) |
| project memory (ACTIVE facts, decisions, constraints) | conversation/chat history (never scraped or inferred) |
| index, map, structural graph (`symbols.db`) - reused where valid | IDE-instance state (registered per IDE installation) |
| project/global configuration incl. execution mode | TASK overrides (single invocation only) |
| project memory and durable decisions | mutable budget, dedup, capsule and active-session state (per model session) |

When identity evidence is ambiguous (e.g. an unrelated copied directory),
isolation wins over merging.

## 7. Environment variables

| Variable | Effect |
|---|---|
| `CONTEXT_AGENT_PROJECT_ROOT` | pins project root (multi-root workspaces) |
| `CONTEXT_AGENT_PROJECT_ID` | overrides persisted project id |
| `CONTEXT_AGENT_SCOPE` | overrides the project-stable scope |
| `CONTEXT_AGENT_RUNTIME_SCOPE` | explicitly overrides the mutable per-session scope |
| `CONTEXT_AGENT_SESSION` | pins model session id (ledger + MODEL_SESSION scope) |
| `CONTEXT_AGENT_MODEL_SESSION_ID` | explicit host model-session identity |
| `CONTEXT_AGENT_CLIENT` | client label recorded in usage/handoff context |
| `CONTEXT_AGENT_IDE` / `CONTEXT_AGENT_IDE_INSTANCE_ID` | force IDE type / pin IDE instance |
| `CONTEXT_AGENT_CONVERSATION_ID` / `CONTEXT_AGENT_TASK_ID` | pin conversation / task identity |

For concurrent IDEs, configure each MCP command with
`--project-root ${workspaceFolder}` (or the host's equivalent workspace
variable). Project identity, index, configuration, and project memory are
shared deliberately. Budget, dedup, capsules, active sessions, and
known-to-model state are keyed by project plus model session, so one IDE
cannot claim another IDE's context or token budget.

## 8. Related documents

* `docs/COMPONENT_ARCHITECTURE.md` - execution modes resolved from this
  configuration model; core/optional boundaries.
* `docs/CONTEXT_MCP.md` - how MCP `initialize` applies client name and scope
  defaults.
* `docs/OPERATIONS.md` - sessions and ledger usage (`--new-session`,
  `context_reuse`).
* `docs/CONTEXT_INTELLIGENCE_ARCHITECTURE.md` - Project Memory 2.0 and the
  ledger that underpin the digests above.
