#!/usr/bin/env python3
"""
Project-aware Context Agent MCP launcher.

Stable IDE entrypoint:
  python C:/path/to/context-agent/scripts/mcp_launcher.py

Enhanced with:
- IDE automatic detection
- Lifecycle management
- Global installation support
- HTTP/SSE remote access mode
- Multi-project support

This launcher answers MCP initialize/tools/list immediately, starts project
bootstrap in the background, and still blocks on first tool call if the
background job has not finished. That prevents IDEs from getting stuck in
"Preparing" while ensuring MCP attachment itself begins the context process.
"""

import importlib.util
import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROTOCOL_VERSION = "2024-11-05"
SOURCE_DIR = Path(__file__).resolve().parent
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

INSTALL_SCRIPT = SOURCE_DIR / "install.py"
MCP_SERVER_SOURCE = SOURCE_DIR / "mcp_server.py"
LIFECYCLE_SCRIPT = SOURCE_DIR / "lifecycle_manager.py"

_SERVER = None
_SERVER_MTIME_NS = 0
_BOOTSTRAPPED_ROOTS = set()
_AUTO_BOOTSTRAP_STARTED_ROOTS = set()
_AUTO_BOOTSTRAP_LOCK = threading.Lock()
_DASHBOARD_ROOTS = set()
_WATCH_ROOTS = set()
REQUIRED_PROJECT_SCRIPTS = {
    "install.py",
    "agent.py",
    "index.py",
    "route.py",
    "mcp_server.py",
    "usage.py",
    "dedup.py",
    "budget.py",
    "session.py",
    "capsule.py",
    "git_meta.py",
    "directives.py",
    "dashboard_server.py",
}

IDE_ENVIRONMENT_VARS = {
    "VSCODE_GIT_ASKPASS": "vscode",
    "VSCODE_INJECTION": "vscode",
    "CURSOR_SETTINGS": "cursor",
    "JETBRAINS_PRODUCT_VERSION": "jetbrains",
    "IDEA_INITIAL_DIRECTORY": "jetbrains",
    "NVIM": "neovim",
    "MYVIMRC": "neovim",
    "WINDSURF": "windsurf",
    "TRAE_IDE": "trae",
    "GITHUB_COPILOT": "github_copilot",
}

try:
    from safe_io import atomic_write_json, file_lock
except ImportError:
    atomic_write_json = None
    file_lock = None

try:
    from ide_detector import IDEDetector
except ImportError:
    IDEDetector = None

try:
    from lifecycle_manager import LifecycleManager
except ImportError:
    LifecycleManager = None

_IDE_DETECTOR = None
_LIFECYCLE_MANAGER = None

def project_root():
    # Use the centralized resolver in paths.py so the launcher, mcp_server,
    # and every project script agree on the same project root. This is the
    # first place the root is locked in for a tool call, so divergence here
    # is what caused Route to escape to the wrong project when several
    # projects (or several IDEs) were open at once.
    from paths import find_project_root
    return find_project_root()

def run_bootstrap_command(cmd, cwd):
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "bootstrap failed")[-4000:])

def ensure_installed(root):
    scripts_dir = root / ".context" / "scripts"
    missing = [name for name in REQUIRED_PROJECT_SCRIPTS if not (scripts_dir / name).exists()]
    if not missing and not install_is_stale(root):
        return
    run_bootstrap_command([sys.executable, str(INSTALL_SCRIPT), str(root)], root)

def source_scripts_hash():
    digest = hashlib.sha256()
    script_names = sorted(REQUIRED_PROJECT_SCRIPTS)
    try:
        spec = importlib.util.spec_from_file_location("context_agent_install_meta", INSTALL_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        script_names = list(getattr(module, "SCRIPTS", script_names))
    except Exception:
        pass
    for name in script_names:
        path = SOURCE_DIR / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()

def install_is_stale(root):
    meta_path = root / ".context" / "install_meta.json"
    if not meta_path.exists():
        return True
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta.get("scripts_sha256") != source_scripts_hash()
    except Exception:
        return True

def ensure_indexed(root):
    symbols = root / ".context" / "symbols.db"
    map_json = root / ".context" / "map.json"
    if symbols.exists() and map_json.exists():
        return
    index_script = root / ".context" / "scripts" / "index.py"
    run_bootstrap_command([sys.executable, str(index_script)], root)

def port_is_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0

def find_dashboard_port(root):
    info_path = root / ".context" / "dashboard.json"
    if info_path.exists():
        try:
            data = json.loads(info_path.read_text(encoding="utf-8"))
            port = int(data.get("port", 0))
            if port and port_is_open(port):
                return port
        except Exception:
            pass

    preferred = int(os.environ.get("CONTEXT_AGENT_DASHBOARD_PORT", "8765"))
    for port in range(preferred, preferred + 50):
        if not port_is_open(port):
            return port
    raise RuntimeError("No free dashboard port found in dashboard port range")

def ensure_dashboard(root):
    if os.environ.get("CONTEXT_AGENT_DASHBOARD", "1") in ("0", "false", "False"):
        return None

    root_key = str(root)
    if root_key in _DASHBOARD_ROOTS:
        info_path = root / ".context" / "dashboard.json"
        if info_path.exists():
            return json.loads(info_path.read_text(encoding="utf-8"))
        return None

    dashboard_script = root / ".context" / "scripts" / "dashboard_server.py"
    if not dashboard_script.exists():
        return None

    lock = file_lock(root / ".context" / "dashboard.lock", timeout=5.0) if file_lock else None
    if lock:
        with lock:
            info = ensure_dashboard_started(root, dashboard_script)
    else:
        info = ensure_dashboard_started(root, dashboard_script)
    _DASHBOARD_ROOTS.add(root_key)
    return info

def write_json(path, data):
    if atomic_write_json:
        atomic_write_json(path, data)
    else:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def registry_path():
    return Path.home() / ".context-agent" / "dashboard_registry.json"


def register_dashboard(info):
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"projects": []}
    except Exception:
        data = {"projects": []}

    projects = [item for item in data.get("projects", []) if item.get("root") != info.get("root")]
    projects.append(info)
    data["projects"] = sorted(projects, key=lambda item: item.get("root", "").lower())
    write_json(path, data)


def ensure_dashboard_started(root, dashboard_script):
    port = find_dashboard_port(root)
    url = f"http://127.0.0.1:{port}"
    if not port_is_open(port):
        subprocess.Popen(
            [sys.executable, str(dashboard_script), "--port", str(port),
             "--root", str(root)],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )

    info = {"url": url, "port": port, "root": str(root)}
    (root / ".context").mkdir(exist_ok=True)
    write_json(root / ".context" / "dashboard.json", info)
    register_dashboard(info)
    return info

def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False

def ensure_watcher(root):
    if os.environ.get("CONTEXT_AGENT_WATCH", "1") in ("0", "false", "False"):
        return None

    root_key = str(root)
    if root_key in _WATCH_ROOTS:
        return None

    watch_script = root / ".context" / "scripts" / "watch.py"
    if not watch_script.exists():
        return None

    info_path = root / ".context" / "watch.json"
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            if info.get("pid") and pid_alive(info["pid"]):
                _WATCH_ROOTS.add(root_key)
                return info
        except Exception:
            pass

    lock = file_lock(root / ".context" / "watch-launch.lock", timeout=5.0) if file_lock else None
    if lock:
        with lock:
            info = ensure_watcher_started(root, watch_script, info_path)
    else:
        info = ensure_watcher_started(root, watch_script, info_path)
    _WATCH_ROOTS.add(root_key)
    return info

def ensure_watcher_started(root, watch_script, info_path):
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            if info.get("pid") and pid_alive(info["pid"]):
                return info
        except Exception:
            pass

    proc = subprocess.Popen(
        [sys.executable, str(watch_script), "--interval", os.environ.get("CONTEXT_AGENT_WATCH_INTERVAL", "2")],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    info = {"pid": proc.pid, "root": str(root), "interval": os.environ.get("CONTEXT_AGENT_WATCH_INTERVAL", "2")}
    (root / ".context").mkdir(exist_ok=True)
    write_json(info_path, info)
    return info

def ensure_ready():
    global _IDE_DETECTOR, _LIFECYCLE_MANAGER

    if _IDE_DETECTOR is None and IDEDetector is not None:
        _IDE_DETECTOR = IDEDetector()
        ide_info = _IDE_DETECTOR.detected
        
        os.environ["CONTEXT_AGENT_IDE_TYPE"] = ide_info.ide_type.value
        os.environ["CONTEXT_AGENT_IDE_NAME"] = ide_info.name
        
        if hasattr(_IDE_DETECTOR, '_detect_mcp_plugins'):
            extensions = _IDE_DETECTOR._detected.extensions
            mcp_plugins = _IDE_DETECTOR._detect_mcp_plugins(extensions)
            os.environ["CONTEXT_AGENT_MCP_PLUGINS_COUNT"] = str(len(mcp_plugins))
            
            for i, plugin in enumerate(mcp_plugins[:5]):
                os.environ[f"CONTEXT_AGENT_MCP_PLUGIN_{i}"] = plugin.get("name", "unknown")
    
    if _LIFECYCLE_MANAGER is None and LifecycleManager is not None:
        _LIFECYCLE_MANAGER = LifecycleManager()
    
    root = project_root()
    root_key = str(root)
    
    if root_key in _BOOTSTRAPPED_ROOTS:
        return root
    
    ensure_installed(root)
    ensure_indexed(root)
    ensure_dashboard(root)
    ensure_watcher(root)
    _BOOTSTRAPPED_ROOTS.add(root_key)
    
    if _LIFECYCLE_MANAGER is not None:
        session = next(
            (
                item
                for item in _LIFECYCLE_MANAGER.sessions.values()
                if item.project_path.resolve() == root.resolve() and item.is_active()
            ),
            None,
        )
        if session is None:
            _LIFECYCLE_MANAGER.create_session(root)
    
    return root

def auto_bootstrap_enabled():
    return os.environ.get("CONTEXT_AGENT_AUTO_BOOTSTRAP", "1") not in ("0", "false", "False")

def bootstrap_status_path(root):
    return root / ".context" / "mcp_bootstrap.json"

def write_bootstrap_status(root, status, **extra):
    try:
        (root / ".context").mkdir(parents=True, exist_ok=True)
        payload = {
            "status": status,
            "at": datetime.now().isoformat(),
            "root": str(root),
            **extra,
        }
        write_json(bootstrap_status_path(root), payload)
    except Exception:
        pass

def auto_bootstrap_worker(root, reason):
    write_bootstrap_status(root, "starting", reason=reason)
    try:
        ready_root = ensure_ready()
        write_bootstrap_status(ready_root, "ready", reason=reason)
    except Exception as exc:
        write_bootstrap_status(root, "error", reason=reason, error=str(exc)[-4000:])

def start_auto_bootstrap(reason="initialize"):
    if not auto_bootstrap_enabled():
        try:
            root = project_root()
        except Exception as exc:
            selection = project_root_selection_data(exc)
            if selection:
                return {"status": "needs_project_selection", **selection}
            raise
        write_bootstrap_status(root, "disabled", reason=reason)
        return {"status": "disabled", "root": str(root)}

    try:
        root = project_root()
    except Exception as exc:
        selection = project_root_selection_data(exc)
        if selection:
            return {"status": "needs_project_selection", **selection}
        raise
    root_key = str(root)
    if root_key in _BOOTSTRAPPED_ROOTS:
        return {"status": "ready", "root": root_key}

    with _AUTO_BOOTSTRAP_LOCK:
        if root_key in _AUTO_BOOTSTRAP_STARTED_ROOTS:
            return {"status": "already_started", "root": root_key}
        _AUTO_BOOTSTRAP_STARTED_ROOTS.add(root_key)

    thread = threading.Thread(target=auto_bootstrap_worker, args=(root, reason), daemon=True)
    thread.start()
    return {"status": "starting", "root": root_key}

def load_server():
    global _SERVER, _SERVER_MTIME_NS
    try:
        source_mtime = MCP_SERVER_SOURCE.stat().st_mtime_ns
    except OSError:
        source_mtime = 0
    if _SERVER is None or source_mtime != _SERVER_MTIME_NS:
        importlib.invalidate_caches()
        spec = importlib.util.spec_from_file_location("context_agent_mcp_server", MCP_SERVER_SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SERVER = module
        _SERVER_MTIME_NS = source_mtime
    return _SERVER

def response(request_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": request_id}
    if error:
        payload["error"] = error
    else:
        payload["result"] = result if result is not None else {}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def normalize_scope(value):
    try:
        from paths import normalize_scope as _normalize_scope
    except ImportError:
        from scripts.paths import normalize_scope as _normalize_scope
    return _normalize_scope(value, fallback="session")

def default_project_scope():
    try:
        from paths import project_scope_key
    except ImportError:
        from scripts.paths import project_scope_key
    return project_scope_key(project_root())

def content_result(data):
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(data, ensure_ascii=False, indent=2),
            }
        ]
    }


def project_root_selection_data(exc):
    if exc.__class__.__name__ == "ProjectRootSelectionRequired" and hasattr(exc, "to_dict"):
        return exc.to_dict()
    return None


def add_project_root_selection(payload, selection):
    payload["projectRootSelection"] = selection
    payload["instructions"] = (
        payload.get("instructions", "")
        + "\n\n"
        + selection.get("message", "Birden fazla proje kökü bulundu.")
        + " CONTEXT_AGENT_PROJECT_ROOT değerini adaylardan biriyle ayarlayın."
    ).strip()
    return payload

def handle(request):
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    server = load_server()

    if method == "initialize":
        client_info = params.get("clientInfo") or {}
        client_name = client_info.get("name") or params.get("clientName")
        if client_name:
            os.environ["CONTEXT_AGENT_CLIENT"] = str(client_name)
            os.environ["CONTEXT_AGENT_IDE"] = str(client_name)
            selection = None
        else:
            selection = None
        # The launcher is a real MCP entry point, so it must establish the
        # same transport-independent model-session binding as mcp_server.py.
        import uuid as _uuid
        connection_id = f"conn-{_uuid.uuid4().hex[:12]}"
        os.environ["CONTEXT_AGENT_MCP_CONNECTION"] = connection_id
        meta = params.get("_meta") or {}
        conversation = params.get("conversationId") or meta.get("conversationId")
        host_session = params.get("modelSessionId") or meta.get("modelSessionId")
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = str(
            conversation or f"conversation-{connection_id}")
        if host_session:
            os.environ["CONTEXT_AGENT_MODEL_SESSION_ID"] = str(host_session)
        else:
            os.environ.pop("CONTEXT_AGENT_MODEL_SESSION_ID", None)
        os.environ.pop("CONTEXT_AGENT_SESSION", None)
        try:
            from identity import resolve_model_session
            resolve_model_session(project_root())
        except Exception:
            os.environ["CONTEXT_AGENT_SESSION"] = f"session-{_uuid.uuid4().hex[:16]}"
        if selection is None:
            bootstrap = start_auto_bootstrap("initialize")
            if bootstrap.get("status") == "needs_project_selection":
                selection = bootstrap
        payload = server.initialize_payload("context-agent-launcher", "0.2.0")
        if selection:
            add_project_root_selection(payload, selection)
        response(request_id, payload)
    elif method == "notifications/initialized":
        start_auto_bootstrap("initialized")
        return
    elif method == "tools/list":
        response(request_id, {"tools": server.tool_schema()})
    elif method == "tools/call":
        try:
            ensure_ready()
            name = params.get("name")
            arguments = params.get("arguments") or {}
            response(request_id, content_result(server.call_tool(name, arguments)))
        except Exception as exc:
            selection = project_root_selection_data(exc)
            if selection:
                response(request_id, content_result(selection))
            else:
                response(request_id, content_result({"error": "context_agent_bootstrap_failed", "message": str(exc)}))
    elif method == "resources/list":
        response(request_id, {"resources": server.resource_schema()})
    elif method == "resources/read":
        try:
            ensure_ready()
            response(request_id, server.read_resource(params.get("uri", "")))
        except Exception as exc:
            selection = project_root_selection_data(exc)
            text = json.dumps(selection or {"error": str(exc)}, ensure_ascii=False)
            response(request_id, {"contents": [{"uri": params.get("uri", ""), "mimeType": "application/json", "text": text}]})
    elif method == "prompts/list":
        response(request_id, {"prompts": server.prompt_schema()})
    elif method == "prompts/get":
        response(request_id, server.get_prompt(params.get("name", ""), params.get("arguments") or {}))
    elif method == "ping":
        response(request_id, {})
    else:
        response(request_id, error={"code": -32601, "message": f"Method not found: {method}"})

def serve():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            handle(json.loads(line))
        except Exception as exc:
            response(None, error={"code": -32603, "message": str(exc)})

def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--project-root", default="")
    args = parser.parse_args()
    if args.project_root and "${" not in args.project_root:
        root = Path(args.project_root).expanduser().resolve()
        if root.exists() and root.is_dir():
            os.environ["CONTEXT_AGENT_PROJECT_ROOT"] = str(root)
    serve()

if __name__ == "__main__":
    main()
