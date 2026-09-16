"""Public HTTP MCP lifecycle isolation tests."""

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mcp_server_http  # noqa: E402


def test_http_transport_session_is_distinct_and_model_binding_is_conversation_aware():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        (root / ".context").mkdir(parents=True)
        (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        server = mcp_server_http.MCPServerHTTP(auth_required=False)

        async def initialize(client_id, ide, conversation):
            return await server._handle_jsonrpc_request(
                "initialize",
                {"clientInfo": {"name": ide}, "conversationId": conversation},
                root, client_id)

        with mock.patch.object(server, "_start_auto_bootstrap",
                               return_value={"status": "ready"}):
            first = asyncio.run(initialize("remote-a", "VS Code", "conv-1"))
            reconnect = asyncio.run(initialize("remote-b", "VS Code", "conv-1"))
            new_conversation = asyncio.run(
                initialize("remote-c", "VS Code", "conv-2"))
            other_ide = asyncio.run(initialize("remote-d", "TREA", "conv-1"))

        assert first["mcpSessionId"] != first["modelSessionId"]
        assert first["mcpSessionId"] != reconnect["mcpSessionId"]
        assert first["modelSessionId"] == reconnect["modelSessionId"]
        assert first["modelSessionId"] != new_conversation["modelSessionId"]
        assert first["modelSessionId"] != other_ide["modelSessionId"]

        binding = server.client_sessions[first["mcpSessionId"]]
        completed = SimpleNamespace(stdout="{}", stderr="", returncode=0)
        with mock.patch.object(server, "_resolve_script_path",
                               return_value=Path(sys.executable)), \
             mock.patch.object(mcp_server_http.subprocess, "run",
                               return_value=completed) as run:
            asyncio.run(server._call_tool(
                "context_agent_status", {}, root, binding))
        child_env = run.call_args.kwargs["env"]
        assert child_env["CONTEXT_AGENT_SESSION"] == first["modelSessionId"]
        assert child_env["CONTEXT_AGENT_MODEL_SESSION_ID"] == first["modelSessionId"]
        assert child_env["CONTEXT_AGENT_MCP_CONNECTION"] == first["mcpSessionId"]
        assert child_env["CONTEXT_AGENT_PROJECT_ROOT"] == str(root.resolve())
        assert child_env["CONTEXT_AGENT_IDE"] == "VS Code"
        assert child_env["CONTEXT_AGENT_CONVERSATION_ID"] == "conv-1"
