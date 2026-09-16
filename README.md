# 🧠 Context Agent

> **Deterministic, LLM-free context intelligence for coding agents.**
>
> Give MCP-capable IDEs the smallest useful slice of a codebase — with project memory, token-aware retrieval, optional routing, and privacy-first local state.

<p align="center">
  <strong>🔎 Discover</strong> · <strong>🧩 Understand</strong> · <strong>📦 Compress</strong> · <strong>🚀 Deliver</strong>
</p>

Context Agent is a local context engine and [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for coding agents. It indexes a host project, understands files and symbols, ranks relevant implementation details, and returns compact context through a stable MCP interface.

The goal is simple: help coding agents see the right parts of a repository without repeatedly loading the entire codebase into a conversation.

## ✨ Why Context Agent?

Coding agents are most effective when they can see the project structure, the relevant implementation details, and the decisions made earlier in a task. Broad, repeated repository reads create noise, waste tokens, and make long-running work harder to control.

Context Agent adds a local, inspectable context layer between the host project and the coding agent:

```text
┌────────────────────┐      ┌──────────────────────┐      ┌──────────────────┐
│  Coding IDE /      │ MCP  │  Context Agent       │      │  Host project    │
│  MCP-capable agent │◄────►│  index + retrieval   │◄────►│  source + symbols│
└────────────────────┘      └──────────┬───────────┘      └──────────────────┘
                                       │
                         ┌─────────────┴─────────────┐
                         │ Optional Router / Gateway │
                         └───────────────────────────┘
```

## 🎯 Core capabilities

| Capability | What it provides |
| --- | --- |
| 🔬 **Deterministic indexing** | Builds a project map and symbol index without an LLM or external provider. |
| 🎯 **Relevant retrieval** | Ranks files and symbols for a task instead of dumping the whole repository. |
| 🧮 **Token-aware context** | Deduplicates and assembles results inside an explicit budget. |
| 🔗 **Code-aware expansion** | Searches symbols, inspects source ranges, and follows related files on demand. |
| 🧠 **Project memory** | Stores architectural decisions, constraints, risks, and durable project facts. |
| 📝 **Task continuity** | Keeps the active task, decisions, and progress compact across long IDE sessions. |
| 🔌 **MCP integration** | Exposes a stdio JSON-RPC server with tools, resources, and prompts. |
| ⚡ **Stable launcher** | Bootstraps the target project and prepares context in the background. |
| 🛣️ **Optional routing** | Adds policy-aware routing and provider-backed workflows when explicitly enabled. |
| 🛡️ **Secret isolation** | Keeps runtime state and credentials outside the public source tree. |

## 🧭 How a context request works

```text
1. Connect      MCP client connects through the stable launcher
2. Bootstrap    Project state and the index are checked or refreshed
3. Understand   The task is analyzed against files, symbols, and memory
4. Retrieve     The smallest relevant context is selected within budget
5. Expand       The agent asks for exact symbols, ranges, or related files
6. Continue     The Context Capsule preserves task continuity
```

The default execution mode is **IDE MODEL**: the host IDE's configured model performs reasoning and implementation while Context Agent supplies compact, relevant context. The core workflow does not require a provider API key.

## 🛠️ Install into a host project

**Requirements:** Python 3.10 or newer.

The installer copies the runtime into the target project's `.context/` directory. Generated state remains project-scoped and is ignored by Git.

### macOS / Linux

```bash
python scripts/install.py /path/to/your/project
cd /path/to/your/project
python .context/scripts/index.py
```

### Windows

```powershell
py scripts\install.py C:\path\to\your\project
py C:\path\to\your\project\.context\scripts\index.py
```

The install operation is repeatable. The launcher can also bootstrap and index a target project automatically when the MCP client connects.

## 🔌 MCP configuration

Point an MCP-capable client at the stable launcher and set the target project explicitly:

```json
{
  "mcpServers": {
    "context-agent": {
      "command": "python",
      "args": ["/absolute/path/to/context-agent/scripts/mcp_launcher.py"],
      "env": {
        "CONTEXT_AGENT_PROJECT_ROOT": "/absolute/path/to/your/project"
      }
    }
  }
}
```

The launcher responds to `initialize` and `tools/list` immediately, then primes the project in the background. The first tool call waits for bootstrap completion when necessary, so the client receives a usable context surface instead of an indefinite preparation state.

## 🧰 MCP tool surface

The core server includes tools for:

- 🩺 **Health and lifecycle** — status, bootstrap, index, usage, and session state.
- 🧭 **Task understanding** — routing and token-budgeted context assembly.
- ⚙️ **Execution modes** — optional provider-backed execution and one-shot workflows.
- 📚 **Knowledge continuity** — directives, project memory, and the active Context Capsule.
- 🔍 **Code navigation** — symbol search, source ranges, and related-file expansion.
- 📊 **Quality workflows** — log-to-context conversion and context-quality evaluation.

The server also exposes project-map and capsule resources plus prompts that guide an MCP client through the retrieve-then-expand workflow.

## 🧩 Optional components

The core Context MCP is intentionally independent from provider credentials. Optional components can be added when a project needs them:

| Component | Use case |
| --- | --- |
| 🌐 **HTTP/SSE transport** | Connect environments that cannot use stdio. |
| 🛣️ **Router MCP** | Apply routing policy and inspect explainable routing history. |
| 🤖 **Model Gateway** | Add provider-backed execution modes. |
| 🖥️ **Cross-IDE lifecycle** | Install and maintain the project-scoped runtime across MCP-capable IDEs. |

When optional routing is disabled or unavailable, the core remains usable and reports the effective mode instead of claiming a provider-backed path that is not active.

## 🔐 Privacy and secret isolation

The public repository contains source, documentation, tests, and a synthetic sample project — not a user's project state.

Runtime indexes, session history, project identity, logs, local databases, and provider credentials must remain outside the repository. The root `.gitignore` excludes these artifacts, along with local environment files and key material.

The core Context MCP does **not** require an API key. Provider-backed features are opt-in and use the local secret-storage/configuration mechanisms documented in [`docs/SECRET_STORAGE.md`](docs/SECRET_STORAGE.md).

## 🧪 Development

Install development dependencies and run the test suite:

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

### Useful entry points

| File | Purpose |
| --- | --- |
| `scripts/mcp_server.py` | Core stdio JSON-RPC MCP server |
| `scripts/mcp_launcher.py` | IDE-friendly launcher and lifecycle entry point |
| `scripts/index.py` | Build or refresh the project index |
| `scripts/agent.py` | Context assembly and retrieval engine |
| `scripts/mcp_server_http.py` | Optional HTTP/SSE transport |
| `scripts/router_mcp_server.py` | Optional Router MCP server |
| `scripts/install.py` | Install into a host project |

The `examples/sample_project/` tree is a small multi-module benchmark fixture used by the tests. The `evals/` directory contains local evaluation inputs and results for context-quality experiments.

## 🗂️ Repository map

```text
.
├── scripts/                  # Runtime, MCP servers, indexing, retrieval, and routing
├── tests/                    # Unit, lifecycle, MCP, security, and isolation tests
├── examples/sample_project/  # Synthetic multi-module benchmark project
├── evals/                    # Context-quality evaluation fixtures
├── directives/               # Example project conventions and playbooks
└── docs/                     # Architecture, operations, configuration, and security
```

## 📖 Documentation

- [`docs/CONTEXT_MCP.md`](docs/CONTEXT_MCP.md) — MCP protocol surface and client behavior
- [`docs/COMPONENT_ARCHITECTURE.md`](docs/COMPONENT_ARCHITECTURE.md) — component boundaries and optional layers
- [`docs/CONTEXT_INTELLIGENCE_ARCHITECTURE.md`](docs/CONTEXT_INTELLIGENCE_ARCHITECTURE.md) — indexing, retrieval, and budgeting
- [`docs/CROSS_IDE_CONFIGURATION.md`](docs/CROSS_IDE_CONFIGURATION.md) — IDE configuration and lifecycle behavior
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — operational checks and troubleshooting
- [`docs/SECRET_STORAGE.md`](docs/SECRET_STORAGE.md) — provider configuration and secret isolation

## 🗺️ Project direction

The project is being developed around four practical priorities:

1. **Reliable context** — return relevant, explainable repository context.
2. **Efficient sessions** — control token budgets and preserve task continuity.
3. **Safe integrations** — keep provider use optional and secrets isolated.
4. **Portable adoption** — make the MCP surface straightforward to use across IDEs.

## 🤝 Contributing

Issues, focused pull requests, reproducible evaluations, and documentation improvements are welcome. Please keep runtime state, credentials, local databases, and private project files out of commits.

## 📄 License

See the repository for the current licensing status before redistributing or embedding the project.

