#!/usr/bin/env python3
"""
HTTP/SSE Tabanlı MCP Server

Uzaktan erişim için HTTP/SSE (Server-Sent Events) tabanlı MCP server.
Bu mod sayesinde:
- Yerel ağdaki uzak kullanıcılar Context Agent'ı kullanabilir
- Manuel kurulum gerektirmez
- Token-based authentication
- Rate limiting
- Connection pooling

Kullanım:
  python mcp_server_http.py --host 0.0.0.0 --port 8765
  python mcp_server_http.py --mode sse --port 8766
  python mcp_server_http.py --mode websocket --port 8767
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import uuid
import time
import hashlib
import hmac
import secrets
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, Set, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import threading

try:
    from aiohttp import web, WSMsgType
    from aiohttp_sse import EventSourceResponse
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

WEBSOCKETS_AVAILABLE = importlib.util.find_spec("websockets") is not None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_PORT = 8765
DEFAULT_HOST = "0.0.0.0"


def project_root_selection_data(exc: Exception) -> Optional[Dict[str, Any]]:
    if exc.__class__.__name__ == "ProjectRootSelectionRequired" and hasattr(exc, "to_dict"):
        return exc.to_dict()
    return None


def add_project_root_selection(payload: Dict[str, Any], selection: Dict[str, Any]) -> Dict[str, Any]:
    payload["projectRootSelection"] = selection
    payload["instructions"] = (
        payload.get("instructions", "")
        + "\n\n"
        + selection.get("message", "Birden fazla proje kökü bulundu.")
        + " CONTEXT_AGENT_PROJECT_ROOT değerini adaylardan biriyle ayarlayın."
    ).strip()
    return payload

class ServerMode(Enum):
    HTTP = "http"
    SSE = "sse"
    WEBSOCKET = "websocket"
    STREAMABLE_HTTP = "streamable_http"

@dataclass
class AuthToken:
    token: str
    created_at: datetime
    expires_at: datetime
    project_path: Optional[str] = None
    permissions: List[str] = field(default_factory=list)
    
    def is_valid(self) -> bool:
        return datetime.now() < self.expires_at
    
    def is_admin(self) -> bool:
        return "admin" in self.permissions

@dataclass 
class ClientConnection:
    client_id: str
    mode: ServerMode
    project_root: Optional[Path]
    connected_at: datetime
    last_activity: datetime
    subscriptions: Set[str] = field(default_factory=set)
    request_count: int = 0
    
    def is_active(self, timeout_seconds: int = 300) -> bool:
        return (datetime.now() - self.last_activity).total_seconds() < timeout_seconds

class RateLimiter:
    def __init__(self, max_requests: int = 100, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: Dict[str, List[float]] = {}
    
    def is_allowed(self, client_id: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        
        if client_id not in self.requests:
            self.requests[client_id] = []
        
        self.requests[client_id] = [
            ts for ts in self.requests[client_id] if ts > cutoff
        ]
        
        if len(self.requests[client_id]) >= self.max_requests:
            return False
        
        self.requests[client_id].append(now)
        return True
    
    def reset(self, client_id: str):
        if client_id in self.requests:
            del self.requests[client_id]

class MCPServerHTTP:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        mode: ServerMode = ServerMode.SSE,
        auth_required: bool = True,
        max_connections: int = 50,
        context_agent_path: Optional[Path] = None
    ):
        self.host = host
        self.port = port
        self.mode = mode
        self.auth_required = auth_required
        self.max_connections = max_connections
        
        self.context_agent_path = context_agent_path or self._find_context_agent()
        self.tokens: Dict[str, AuthToken] = {}
        self.connections: Dict[str, ClientConnection] = {}
        self.client_sessions: Dict[str, Dict[str, str]] = {}
        self.rate_limiter = RateLimiter()
        self.lock = threading.Lock()
        self._auto_bootstrap_started: Set[str] = set()
        self._auto_bootstrap_lock = threading.Lock()
        
        self.app = None
        self.runner = None
        self.site = None
        
    def _find_context_agent(self) -> Path:
        """Context Agent kurulum yolunu bul"""
        env_path = os.getenv("CONTEXT_AGENT_PATH")
        if env_path:
            return Path(env_path)
        
        current = Path(__file__).resolve().parent
        for parent in [current] + list(current.parents):
            if (parent / ".context").exists():
                return parent
        
        home_launcher = Path.home() / ".context-agent" / "launcher.py"
        if home_launcher.exists():
            return home_launcher.parent
        
        return current
    
    def _generate_token(self, project_path: Optional[str] = None) -> str:
        """Güvenli token üret"""
        token_data = f"{uuid.uuid4()}{time.time()}{secrets.token_bytes(32)}"
        return hashlib.sha256(token_data.encode()).hexdigest()

    def _static_admin_token(self) -> str:
        return os.getenv("CONTEXT_AGENT_HTTP_TOKEN", "").strip()
    
    def _validate_token(self, token: str) -> Optional[AuthToken]:
        """Token doğrula"""
        static = self._static_admin_token()
        if static and hmac.compare_digest(token or "", static):
            now = datetime.now()
            return AuthToken(
                token=token,
                created_at=now,
                expires_at=now + timedelta(days=3650),
                permissions=["admin", "read", "index", "search"],
            )
        auth_token = self.tokens.get(token)
        if auth_token and auth_token.is_valid():
            return auth_token
        return None

    def _authorize_request(self, request: web.Request) -> Optional[AuthToken]:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        return self._validate_token(auth_header.removeprefix("Bearer ").strip())
    
    def _create_auth_token(
        self,
        project_path: Optional[str] = None,
        expires_hours: int = 24,
        permissions: Optional[List[str]] = None
    ) -> AuthToken:
        """Yeni auth token oluştur"""
        token = self._generate_token()
        now = datetime.now()
        auth_token = AuthToken(
            token=token,
            created_at=now,
            expires_at=now + timedelta(hours=expires_hours),
            project_path=project_path,
            permissions=permissions or ["read", "index", "search"]
        )
        
        with self.lock:
            self.tokens[token] = auth_token
        
        return auth_token
    
    def _get_project_root(self, request: web.Request) -> Path:
        """İstekten proje kökünü çıkar"""
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        
        if token:
            auth_token = self._validate_token(token)
            if auth_token and auth_token.project_path:
                return Path(auth_token.project_path).resolve()
        
        project_root = request.headers.get("X-Project-Root")
        if project_root:
            return Path(project_root).resolve()
        
        workspace = request.headers.get("X-Workspace-Path")
        if workspace:
            return self._resolve_project_root(Path(workspace))
        
        return self._resolve_project_root(Path.cwd())

    def _resolve_project_root(self, start: Path) -> Path:
        try:
            try:
                from scripts.paths import find_project_root
            except ImportError:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from paths import find_project_root

            return find_project_root(start)
        except Exception as exc:
            if project_root_selection_data(exc):
                raise
            return start.resolve()
    
    async def _handle_jsonrpc_request(
        self,
        method: str,
        params: Dict[str, Any],
        project_root: Path,
        client_id: str = "",
    ) -> Dict[str, Any]:
        """JSON-RPC isteğini işle"""
        if method == "initialize":
            transport_id = client_id if client_id.startswith(("sse_", "ws_")) \
                else f"http_{uuid.uuid4().hex[:16]}"
            model_session = self._resolve_client_model_session(
                project_root, params, transport_id)
            meta = params.get("_meta") or {}
            conversation = (params.get("conversationId")
                            or meta.get("conversationId")
                            or f"conversation-{transport_id}")
            project_identity = self._project_identity(project_root)
            binding = {"transport_session_id": transport_id,
                       "model_session_id": model_session,
                       "client_name": str((params.get("clientInfo") or {}).get("name") or
                                          params.get("clientName") or "http-mcp"),
                       "conversation_id": str(conversation),
                       "project_root": str(project_root.resolve()),
                       "project_id": project_identity.get("project_id", "")}
            self.client_sessions[transport_id] = binding
            if client_id.startswith(("sse_", "ws_")):
                self.client_sessions[client_id] = binding
            bootstrap = self._start_auto_bootstrap(project_root, "initialize")
            payload = {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {
                    "name": "context-agent",
                    "version": "1.0.0"
                },
                "capabilities": {
                    "tools": True,
                    "resources": True,
                    "prompts": True
                },
                "instructions": (
                    "Context Agent starts install/index priming automatically on MCP initialize. "
                    "For coding tasks, call context_agent_build_context first with the user's task "
                    "and a token budget before reading broad files."
                ),
                "mcpSessionId": transport_id,
                "modelSessionId": model_session,
                "project": {
                    "root": str(project_root.resolve()),
                    "project_id": project_identity.get("project_id", ""),
                    "scope": project_identity.get("scope", ""),
                },
            }
            if bootstrap.get("status") == "needs_project_selection":
                add_project_root_selection(payload, bootstrap)
            return payload
        
        elif method == "tools/list":
            return await self._list_tools(project_root)
        
        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            binding = self.client_sessions.get(client_id)
            if (binding and binding.get("project_root")
                    and Path(binding["project_root"]).resolve()
                    != project_root.resolve()):
                # A transport session is bound to exactly one project. Never
                # carry its model-known ledger into another project root.
                binding = None
            return await self._call_tool(tool_name, tool_args, project_root,
                                         binding)
        
        elif method == "resources/list":
            return await self._list_resources(project_root)
        
        elif method == "resources/read":
            uri = params.get("uri")
            return await self._read_resource(uri, project_root)
        
        else:
            return {
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}"
                }
            }
    
    async def _list_tools(self, project_root: Path) -> Dict[str, Any]:
        """Mevcut tool'ları listele"""
        try:
            sys.path.insert(0, str(self.context_agent_path / ".context" / "scripts"))
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import mcp_server
            return {"tools": mcp_server.tool_schema()}
        except Exception:
            return {
                "tools": [
                    {"name": "context_agent_status", "description": "Context Agent durumu"},
                    {"name": "context_agent_index", "description": "Proje indeksleme"},
                    {"name": "context_agent_route", "description": "Dosya yönlendirme"},
                    {"name": "context_agent_build_context", "description": "Bağlam oluşturma"},
                    {"name": "context_agent_search", "description": "Sembol arama"},
                    {"name": "context_agent_get_symbol", "description": "Sembol bilgisi"},
                    {"name": "context_agent_get_range", "description": "Dosya aralığı"},
                    {"name": "context_agent_get_related", "description": "İlgili dosyalar"},
                    {"name": "context_agent_capsule", "description": "Aktif hafıza"},
                    {"name": "context_agent_usage", "description": "Kullanım metrikleri"},
                ]
            }
    
    async def _call_tool(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        project_root: Path,
        session_binding: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Tool çağır"""
        script_map = {
            "context_agent_status": "agent",
            "context_agent_bootstrap_status": "",
            "context_agent_index": "index",
            "context_agent_route": "route",
            "context_agent_build_context": "agent",
            "context_agent_execute": "one_shot",
            "context_agent_one_shot": "one_shot",
            "context_agent_directives": "directives",
            "context_agent_memory": "memory",
            "context_agent_search": "search",
            "context_agent_get_symbol": "get_symbol",
            "context_agent_get_range": "get_range",
            "context_agent_get_related": "get_related",
            "context_agent_from_log": "from_log",
            "context_agent_eval": "eval",
            "context_agent_capsule": "capsule",
            "context_agent_usage": "usage",
        }
        
        script_name = script_map.get(tool_name)
        if not script_name:
            if tool_name == "context_agent_bootstrap_status":
                return self._bootstrap_status(project_root)
            return {"error": f"Unknown tool: {tool_name}"}
        
        script_path = self._resolve_script_path(project_root, script_name)
        if not script_path.exists():
            return {"error": f"Script not found: {script_name}"}
        
        try:
            import subprocess
            args = [sys.executable, str(script_path)]
            
            if tool_name == "context_agent_status":
                args.append("--status")
            elif tool_name == "context_agent_index":
                if "file" in tool_args:
                    args.extend(["--file", str(tool_args["file"])])
                if tool_args.get("force"):
                    args.append("--force")
            elif tool_name == "context_agent_route":
                args.append(tool_args.get("task", ""))
                if "budget" in tool_args:
                    args.extend(["--budget", str(tool_args["budget"])])
                if "top" in tool_args:
                    args.extend(["--top", str(tool_args["top"])])
            elif tool_name == "context_agent_build_context":
                args.append(tool_args.get("task", ""))
                args.extend(["--budget", str(tool_args.get("budget", 8000))])
                args.extend(["--model", str(tool_args.get("model", "claude-haiku"))])
                if int(tool_args.get("strict_quality", 0) or 0) > 0:
                    args.extend(["--strict-quality", str(tool_args["strict_quality"])])
                if tool_args.get("directive_budget") is not None:
                    args.extend(["--directive-budget", str(tool_args["directive_budget"])])
                if tool_args.get("raw"):
                    args.append("--raw")
                if tool_args.get("diagnostics"):
                    args.append("--diagnostics")
            elif tool_name in ("context_agent_execute", "context_agent_one_shot"):
                args.append(tool_args.get("task", ""))
                args.extend(["--model", str(tool_args.get("model", "auto"))])
                args.extend(["--max-output-tokens", str(tool_args.get("max_output_tokens", 4096))])
                args.extend(["--strict-quality", str(tool_args.get("strict_quality", 0))])
                args.extend(["--directive-budget", str(tool_args.get("directive_budget", 1200))])
                args.extend(["--max-content-chars", str(tool_args.get("max_content_chars", 5000))])
                if tool_args.get("budget") is not None:
                    args.extend(["--budget", str(tool_args["budget"])])
                if tool_args.get("allow_insufficient"):
                    args.append("--allow-insufficient")
            elif tool_name == "context_agent_directives":
                if tool_args.get("action") == "list":
                    args.append("--list")
                else:
                    args.extend(["--budget", str(tool_args.get("budget", 1200))])
                    if tool_args.get("task"):
                        args.extend(["--task", str(tool_args["task"])])
            elif tool_name == "context_agent_memory":
                action = str(tool_args.get("action") or "sheet")
                if action == "add":
                    args.extend(["--add", "--type", str(tool_args.get("memory_type") or "TASK"),
                                 "--content", str(tool_args.get("content") or "")])
                    if tool_args.get("source"):
                        args.extend(["--source", str(tool_args["source"])])
                    if tool_args.get("confidence") is not None:
                        args.extend(["--confidence", str(tool_args["confidence"])])
                    for path in tool_args.get("files") or []:
                        args.extend(["--file", str(path)])
                    for symbol in tool_args.get("symbols") or []:
                        args.extend(["--symbol", str(symbol)])
                    if tool_args.get("supersedes") is not None:
                        args.extend(["--supersedes", str(tool_args["supersedes"])])
                elif action == "list":
                    args.append("--list")
                    if tool_args.get("memory_type"):
                        args.extend(["--type", str(tool_args["memory_type"])])
                elif action == "status":
                    args.extend(["--status", str(tool_args.get("id")),
                                 str(tool_args.get("status") or "STALE")])
                elif action == "supersede":
                    args.extend(["--supersede", str(tool_args.get("old_id")),
                                 str(tool_args.get("new_id"))])
                elif action == "verify":
                    args.append("--verify")
                else:
                    args.extend(["--sheet", "--max-chars",
                                 str(int(tool_args.get("max_chars", 4800)))])
            elif tool_name == "context_agent_search":
                args.append(tool_args.get("query", ""))
                if "limit" in tool_args:
                    args.extend(["--limit", str(tool_args["limit"])])
                if tool_args.get("type"):
                    args.extend(["--type", str(tool_args["type"])])
                if tool_args.get("file_only"):
                    args.append("--file-only")
            elif tool_name == "context_agent_get_symbol":
                args.append(tool_args.get("name", ""))
                if tool_args.get("fuzzy"):
                    args.append("--fuzzy")
                if tool_args.get("type"):
                    args.extend(["--type", str(tool_args["type"])])
                if tool_args.get("file"):
                    args.extend(["--file", str(tool_args["file"])])
            elif tool_name == "context_agent_get_range":
                args.append(tool_args.get("file", ""))
                if tool_args.get("start") is not None:
                    args.append(str(tool_args["start"]))
                if tool_args.get("end") is not None:
                    args.append(str(tool_args["end"]))
            elif tool_name == "context_agent_get_related":
                args.append(tool_args.get("file", ""))
                args.extend(["--max", str(tool_args.get("max", 15))])
                if tool_args.get("reverse"):
                    args.append("--reverse")
            elif tool_name == "context_agent_from_log":
                args.extend(["--radius", str(tool_args.get("radius", 40))])
                args.extend(["--budget", str(tool_args.get("budget", 4000))])
            elif tool_name == "context_agent_eval":
                args.extend(["--top", str(tool_args.get("top", 5))])
                args.extend(["--budget", str(tool_args.get("budget", 8000))])
                if tool_args.get("file"):
                    args.extend(["--file", str(tool_args["file"])])
                if tool_args.get("fail_under") is not None:
                    args.extend(["--fail-under", str(tool_args["fail_under"])])
            elif tool_name == "context_agent_capsule":
                action = tool_args.get("action", "context")
                if action == "current":
                    args.append("--current")
                elif action == "context":
                    args.append("--context")
                elif action == "event":
                    args.extend(["--event", str(tool_args.get("kind", "note")), str(tool_args.get("text", ""))])
                elif action == "complete":
                    args.extend(["--complete", str(tool_args.get("text", ""))])
                elif action == "list":
                    args.append("--list")
                else:
                    return {"error": f"unknown_capsule_action: {action}"}
            elif tool_name == "context_agent_usage":
                args.append("--summary")

            input_text = tool_args.get("log", "") if tool_name == "context_agent_from_log" else None
            
            child_env = os.environ.copy()
            child_env["CONTEXT_AGENT_PROJECT_ROOT"] = str(project_root.resolve())
            if session_binding:
                child_env["CONTEXT_AGENT_SESSION"] = session_binding["model_session_id"]
                child_env["CONTEXT_AGENT_MODEL_SESSION_ID"] = session_binding[
                    "model_session_id"]
                child_env["CONTEXT_AGENT_CLIENT"] = session_binding.get(
                    "client_name", "http-mcp")
                child_env["CONTEXT_AGENT_IDE"] = session_binding.get(
                    "client_name", "http-mcp")
                child_env["CONTEXT_AGENT_CONVERSATION_ID"] = session_binding.get(
                    "conversation_id", "")
                child_env["CONTEXT_AGENT_MCP_CONNECTION"] = session_binding[
                    "transport_session_id"]
            else:
                # No initialize/session header: isolate rather than sharing a
                # false default known-to-model ledger.
                child_env["CONTEXT_AGENT_SESSION"] = f"session-{uuid.uuid4().hex[:16]}"
                child_env["CONTEXT_AGENT_MCP_CONNECTION"] = f"http-{uuid.uuid4().hex[:12]}"
            result = subprocess.run(
                args,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(project_root),
                env=child_env,
                timeout=120 if tool_name in {"context_agent_build_context", "context_agent_execute", "context_agent_one_shot", "context_agent_index", "context_agent_eval", "context_agent_from_log"} else 30,
            )
            
            if result.stdout.strip():
                try:
                    return json.loads(result.stdout)
                except json.JSONDecodeError:
                    return {"result": result.stdout}
            
            return {"error": result.stderr or "No output"}
        
        except subprocess.TimeoutExpired:
            return {"error": "Tool timeout"}
        except Exception as e:
            return {"error": str(e)}

    def _resolve_script_path(self, project_root: Path, script_name: str) -> Path:
        filename = f"{script_name}.py"
        candidates = [
            project_root / ".context" / "scripts" / filename,
            self.context_agent_path / ".context" / "scripts" / filename,
            self.context_agent_path / "scripts" / filename,
            self.context_agent_path / filename,
            Path(__file__).resolve().parent / filename,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    def _bootstrap_status(self, project_root: Path) -> Dict[str, Any]:
        status_path = project_root / ".context" / "mcp_bootstrap.json"
        if status_path.exists():
            try:
                data = json.loads(status_path.read_text(encoding="utf-8"))
            except Exception as exc:
                data = {"status": "unreadable", "error": str(exc)}
        else:
            data = {"status": "not_started"}
        ctx = project_root / ".context"
        identity = self._project_identity(project_root)
        data.update({
            "root": str(project_root),
            "project_id": identity.get("project_id", ""),
            "scope": identity.get("scope", ""),
            "index_exists": (ctx / "symbols.db").exists(),
            "map_exists": (ctx / "map.json").exists(),
        })
        return data

    def _auto_bootstrap_enabled(self) -> bool:
        return os.environ.get("CONTEXT_AGENT_AUTO_BOOTSTRAP", "1") not in ("0", "false", "False")

    def _write_bootstrap_status(self, project_root: Path, status: str, **extra: Any) -> None:
        try:
            ctx = project_root / ".context"
            ctx.mkdir(parents=True, exist_ok=True)
            payload = {
                "status": status,
                "at": datetime.now().isoformat(),
                "root": str(project_root),
                **extra,
            }
            (ctx / "mcp_bootstrap.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _project_identity(self, project_root: Path) -> Dict[str, str]:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from paths import ensure_project_id, project_scope_key

            return {
                "project_id": ensure_project_id(project_root),
                "scope": project_scope_key(project_root),
            }
        except Exception:
            return {"project_id": "", "scope": ""}

    def _resolve_client_model_session(self, project_root: Path,
                                      params: Dict[str, Any],
                                      transport_id: str) -> str:
        """Resolve identity under a short env lock; transport != model session."""
        keys = ("CONTEXT_AGENT_IDE", "CONTEXT_AGENT_CONVERSATION_ID",
                "CONTEXT_AGENT_MODEL_SESSION_ID", "CONTEXT_AGENT_SESSION")
        meta = params.get("_meta") or {}
        client_name = str((params.get("clientInfo") or {}).get("name") or
                          params.get("clientName") or "http-mcp")
        conversation = (params.get("conversationId") or meta.get("conversationId") or
                        f"conversation-{transport_id}")
        host_session = params.get("modelSessionId") or meta.get("modelSessionId")
        with self.lock:
            previous = {key: os.environ.get(key) for key in keys}
            try:
                os.environ["CONTEXT_AGENT_IDE"] = client_name
                os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = str(conversation)
                os.environ.pop("CONTEXT_AGENT_SESSION", None)
                if host_session:
                    os.environ["CONTEXT_AGENT_MODEL_SESSION_ID"] = str(host_session)
                else:
                    os.environ.pop("CONTEXT_AGENT_MODEL_SESSION_ID", None)
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from identity import resolve_model_session
                return resolve_model_session(project_root)
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def _start_auto_bootstrap(self, project_root: Path, reason: str) -> Dict[str, str]:
        root = project_root.resolve()
        root_key = str(root)
        if not self._auto_bootstrap_enabled():
            self._write_bootstrap_status(root, "disabled", reason=reason)
            return {"status": "disabled", "root": root_key}

        with self._auto_bootstrap_lock:
            if root_key in self._auto_bootstrap_started:
                return {"status": "already_started", "root": root_key}
            self._auto_bootstrap_started.add(root_key)

        thread = threading.Thread(target=self._auto_bootstrap_worker, args=(root, reason), daemon=True)
        thread.start()
        return {"status": "starting", "root": root_key}

    def _auto_bootstrap_worker(self, project_root: Path, reason: str) -> None:
        self._write_bootstrap_status(project_root, "starting", reason=reason)
        try:
            self._project_identity(project_root)
            self._ensure_project_install(project_root)
            self._ensure_project_index(project_root)
            self._write_bootstrap_status(project_root, "ready", reason=reason)
        except Exception as exc:
            self._write_bootstrap_status(project_root, "error", reason=reason, error=str(exc)[-4000:])

    def _ensure_project_install(self, project_root: Path) -> None:
        scripts_dir = project_root / ".context" / "scripts"
        required = {"agent.py", "mcp_server.py", "index.py", "route.py", "git_meta.py", "paths.py"}
        if scripts_dir.exists() and all((scripts_dir / name).exists() for name in required):
            return
        install_script = self._resolve_script_path(project_root, "install")
        if not install_script.exists():
            return
        subprocess.run(
            [sys.executable, str(install_script), str(project_root)],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
            env={**os.environ, "CONTEXT_AGENT_PROJECT_ROOT": str(project_root.resolve())},
        )

    def _ensure_project_index(self, project_root: Path) -> None:
        ctx = project_root / ".context"
        if (ctx / "symbols.db").exists() and (ctx / "map.json").exists():
            return
        index_script = self._resolve_script_path(project_root, "index")
        if not index_script.exists():
            return
        subprocess.run(
            [sys.executable, str(index_script)],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
            env={**os.environ, "CONTEXT_AGENT_PROJECT_ROOT": str(project_root.resolve())},
        )
    
    async def _list_resources(self, project_root: Path) -> Dict[str, Any]:
        """Mevcut resource'ları listele"""
        return {
            "resources": [
                {"uri": "context-agent://rules", "name": "Context Agent Kuralları"},
                {"uri": "context-agent://project-map", "name": "Proje Haritası"},
                {"uri": "context-agent://status", "name": "Sistem Durumu"},
                {"uri": "context-agent://capsule", "name": "Aktif Hafıza"},
            ]
        }
    
    async def _read_resource(self, uri: str, project_root: Path) -> Dict[str, Any]:
        """Resource oku"""
        map_path = project_root / ".context" / "map.json"

        if "project-map" in uri and map_path.exists():
            content = map_path.read_text(encoding="utf-8", errors="replace")
            try:
                return {"contents": [{"uri": uri, "mimeType": "application/json", "text": content}]}
            except Exception:
                pass
        
        if "status" in uri:
            status_data = {
                "indexed": map_path.exists(),
                "project": str(project_root),
                "timestamp": datetime.now().isoformat()
            }
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(status_data)}]}
        
        return {"error": "Resource not found"}
    
    async def handle_request(self, request: web.Request) -> web.Response:
        """HTTP isteğini işle"""
        client_id = request.remote or "unknown"
        
        if not self.rate_limiter.is_allowed(client_id):
            return web.json_response(
                {"error": "Rate limit exceeded"},
                status=429
            )
        
        if request.path == "/health":
            return web.json_response({
                "status": "healthy",
                "mode": self.mode.value,
                "connections": len(self.connections),
                "timestamp": datetime.now().isoformat()
            })
        
        if request.path == "/auth/token":
            if request.method == "POST":
                if self.auth_required:
                    auth = self._authorize_request(request)
                    if not auth or not auth.is_admin():
                        return web.json_response(
                            {
                                "error": "Admin bearer token required",
                                "hint": "Set CONTEXT_AGENT_HTTP_TOKEN and send Authorization: Bearer <token>, or run with --no-auth for trusted local use.",
                            },
                            status=401,
                        )
                data = await request.json()
                project_path = data.get("project_path")
                expires_hours = data.get("expires_hours", 24)
                
                token = self._create_auth_token(project_path, expires_hours)
                
                return web.json_response({
                    "token": token.token,
                    "expires_at": token.expires_at.isoformat(),
                    "project_path": token.project_path
                })
            else:
                return web.json_response(
                    {"error": "Method not allowed"},
                    status=405
                )
        
        if self.auth_required and not self._authorize_request(request):
            return web.json_response(
                {"error": "Authentication required"},
                status=401,
            )

        try:
            project_root = self._get_project_root(request)
            request_session_id = request.headers.get("Mcp-Session-Id", "").strip()
            rpc_client_id = request_session_id or client_id
            if request.content_type == "application/json":
                data = await request.json()
                
                if isinstance(data, list):
                    results = []
                    response_session_id = ""
                    for item in data:
                        result = await self._handle_jsonrpc_request(
                            item.get("method", ""),
                            item.get("params", {}),
                            project_root,
                            rpc_client_id,
                        )
                        if result.get("mcpSessionId"):
                            response_session_id = result["mcpSessionId"]
                            rpc_client_id = response_session_id
                        results.append({
                            "jsonrpc": "2.0",
                            "id": item.get("id"),
                            "result": result if "error" not in result else None,
                            "error": result.get("error") if "error" in result else None
                        })
                    headers = ({"Mcp-Session-Id": response_session_id}
                               if response_session_id else {})
                    return web.json_response(results, headers=headers)
                
                else:
                    result = await self._handle_jsonrpc_request(
                        data.get("method", ""),
                        data.get("params", {}),
                        project_root,
                        rpc_client_id,
                    )
                    headers = {}
                    if result.get("mcpSessionId"):
                        headers["Mcp-Session-Id"] = result["mcpSessionId"]
                    return web.json_response({
                        "jsonrpc": "2.0",
                        "id": data.get("id"),
                        "result": result if "error" not in result else None,
                        "error": result.get("error") if "error" in result else None
                    }, headers=headers)
            
            else:
                return web.json_response(
                    {"error": "Content-Type must be application/json"},
                    status=400
                )
        
        except Exception as e:
            selection = project_root_selection_data(e)
            if selection:
                return web.json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "result": selection,
                    },
                    status=409,
                )
            return web.json_response(
                {"error": str(e)},
                status=500
            )
    
    async def handle_sse(self, request: web.Request) -> web.Response:
        """SSE (Server-Sent Events) bağlantısı"""
        client_id = f"sse_{uuid.uuid4().hex[:8]}"
        try:
            project_root = self._get_project_root(request)
        except Exception as exc:
            selection = project_root_selection_data(exc)
            if selection:
                return web.json_response(selection, status=409)
            raise
        
        connection = ClientConnection(
            client_id=client_id,
            mode=ServerMode.SSE,
            project_root=project_root,
            connected_at=datetime.now(),
            last_activity=datetime.now()
        )
        
        with self.lock:
            if len(self.connections) >= self.max_connections:
                return web.json_response(
                    {"error": "Max connections reached"},
                    status=503
                )
            self.connections[client_id] = connection
        
        response = EventSourceResponse()
        await response.prepare(request)
        
        try:
            await response.send(json.dumps({
                "event": "connected",
                "client_id": client_id,
                "project_root": str(project_root)
            }), event="system")
            
            heartbeat = 0
            async for line in request.content:
                connection.last_activity = datetime.now()
                heartbeat += 1
                
                if heartbeat % 30 == 0:
                    await response.send(json.dumps({"event": "heartbeat"}), event="system")
                
                try:
                    data = json.loads(line.decode())
                    result = await self._handle_jsonrpc_request(
                        data.get("method", ""),
                        data.get("params", {}),
                        project_root,
                        client_id,
                    )
                    
                    await response.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": data.get("id"),
                        "result": result
                    }))
                except json.JSONDecodeError:
                    pass
        
        finally:
            with self.lock:
                if client_id in self.connections:
                    del self.connections[client_id]
                self.client_sessions.pop(client_id, None)
        
        return response
    
    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket bağlantısı"""
        if not WEBSOCKETS_AVAILABLE:
            raise web.HTTPServiceUnavailable(text="WebSocket support not available")
        
        try:
            project_root = self._get_project_root(request)
        except Exception as exc:
            selection = project_root_selection_data(exc)
            if selection:
                return web.json_response(selection, status=409)
            raise

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        client_id = f"ws_{uuid.uuid4().hex[:8]}"
        
        connection = ClientConnection(
            client_id=client_id,
            mode=ServerMode.WEBSOCKET,
            project_root=project_root,
            connected_at=datetime.now(),
            last_activity=datetime.now()
        )
        
        with self.lock:
            if len(self.connections) >= self.max_connections:
                await ws.close()
                return ws
            self.connections[client_id] = connection
        
        try:
            await ws.send_json({
                "event": "connected",
                "client_id": client_id
            })
            
            async for msg in ws:
                connection.last_activity = datetime.now()
                connection.request_count += 1
                
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        result = await self._handle_jsonrpc_request(
                            data.get("method", ""),
                            data.get("params", {}),
                            project_root,
                            client_id,
                        )
                        
                        await ws.send_json({
                            "jsonrpc": "2.0",
                            "id": data.get("id"),
                            "result": result
                        })
                    
                    except json.JSONDecodeError:
                        await ws.send_json({
                            "error": "Invalid JSON"
                        })
                
                elif msg.type == WSMsgType.ERROR:
                    break
        
        finally:
            with self.lock:
                if client_id in self.connections:
                    del self.connections[client_id]
                self.client_sessions.pop(client_id, None)
        
        return ws
    
    async def cleanup_connections(self):
        """Zaman aşımına uğrayan bağlantıları temizle"""
        with self.lock:
            expired = [
                cid for cid, conn in self.connections.items()
                if not conn.is_active()
            ]
            for cid in expired:
                del self.connections[cid]
        
        expired_tokens = [
            token for token, auth in self.tokens.items()
            if not auth.is_valid()
        ]
        with self.lock:
            for token in expired_tokens:
                del self.tokens[token]
    
    async def start(self):
        """Server'ı başlat"""
        if not AIOHTTP_AVAILABLE:
            print("ERROR: aiohttp is required for HTTP/SSE mode")
            print("Install with: pip install aiohttp aiohttp-sse")
            return
        
        self.app = web.Application()
        
        self.app.router.add_route("GET", "/health", self.handle_request)
        self.app.router.add_route("POST", "/auth/token", self.handle_request)
        
        if self.mode == ServerMode.SSE:
            self.app.router.add_route("GET", "/sse", self.handle_sse)
            self.app.router.add_route("POST", "/", self.handle_request)
        elif self.mode == ServerMode.WEBSOCKET:
            self.app.router.add_route("GET", "/ws", self.handle_websocket)
            self.app.router.add_route("POST", "/", self.handle_request)
        else:
            self.app.router.add_route("*", "/{path:.*}", self.handle_request)
        
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        
        print("Context Agent MCP Server started:")
        print(f"  Mode: {self.mode.value}")
        print(f"  Host: {self.host}")
        print(f"  Port: {self.port}")
        print(f"  Auth Required: {self.auth_required}")
        print("\nEndpoints:")
        print(f"  Health: http://{self.host}:{self.port}/health")
        print(f"  Token: POST http://{self.host}:{self.port}/auth/token")
        print(f"  RPC: POST http://{self.host}:{self.port}/")
        if self.mode == ServerMode.SSE:
            print(f"  SSE: http://{self.host}:{self.port}/sse")
        elif self.mode == ServerMode.WEBSOCKET:
            print(f"  WS: ws://{self.host}:{self.port}/ws")
    
    async def _cleanup_loop(self):
        """Periyodik temizlik"""
        while True:
            await asyncio.sleep(60)
            await self.cleanup_connections()
    
    async def stop(self):
        """Server'ı durdur"""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()

def parse_args():
    """Komut satırı argümanlarını parse et"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Context Agent HTTP/SSE MCP Server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port number")
    parser.add_argument("--mode", choices=["http", "sse", "websocket"], default="sse", help="Server mode")
    parser.add_argument("--no-auth", action="store_true", help="Disable authentication")
    parser.add_argument("--max-connections", type=int, default=50, help="Max concurrent connections")
    parser.add_argument("--context-agent-path", type=Path, help="Context Agent installation path")
    
    return parser.parse_args()

async def main():
    args = parse_args()
    
    server = MCPServerHTTP(
        host=args.host,
        port=args.port,
        mode=ServerMode(args.mode),
        auth_required=not args.no_auth,
        max_connections=args.max_connections,
        context_agent_path=args.context_agent_path
    )
    
    try:
        await server.start()
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        await server.stop()

if __name__ == "__main__":
    if not AIOHTTP_AVAILABLE:
        print("Installing dependencies...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "aiohttp", "aiohttp-sse"],
            check=False,
        )
    
    asyncio.run(main())
