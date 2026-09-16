# Context Agent

Context Agent is a deterministic, LLM-free context engine exposed through the Model Context Protocol (MCP). It indexes a host project, selects the smallest useful set of files and symbols for a task, and returns compact context to an MCP-capable coding agent.

The core Context MCP works without an external provider or API key. Router MCP and the local model gateway are optional layers.

## Install into a project

Requirements: Python 3.10 or newer.

```bash
python scripts/install.py /path/to/your/project
cd /path/to/your/project
python .context/scripts/index.py
```

On Windows, the equivalent is:

```powershell
py scripts\install.py C:\path\to\your\project
py C:\path\to\your\project\.context\scripts\index.py
```

The installer copies the runtime scripts into the target project's `.context/` directory and creates project-scoped state there. That generated state is intentionally ignored by Git.

## MCP configuration

Point an MCP client at the stable launcher from this checkout and set the target project explicitly:

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

The launcher answers `initialize` and `tools/list` immediately, then primes the target project in the background. The stdio server is the primary integration; HTTP/SSE, Router MCP, and the model gateway are optional.

## Development

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

Useful entry points:

- `scripts/mcp_server.py` — core stdio JSON-RPC MCP server
- `scripts/mcp_launcher.py` — IDE-friendly launcher and lifecycle entry point
- `scripts/mcp_server_http.py` — optional HTTP/SSE transport
- `scripts/router_mcp_server.py` — optional Router MCP server
- `scripts/install.py` — install into a host project

See `docs/CONTEXT_MCP.md`, `docs/OPERATIONS.md`, and `docs/COMPONENT_ARCHITECTURE.md` for the protocol surface and deployment details.

## Privacy and secrets

Runtime indexes, session history, project identity, logs, local databases, and provider secrets must stay outside the repository. The project `.gitignore` excludes these artifacts, and the core Context MCP does not require a provider key.
