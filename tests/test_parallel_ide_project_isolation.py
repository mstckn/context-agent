"""Parallel IDE/project isolation regressions for mutable context state."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import budget  # noqa: E402
import capsule  # noqa: E402
import dashboard_server  # noqa: E402
import dedup  # noqa: E402
import global_install  # noqa: E402
import mcp_server  # noqa: E402
import paths  # noqa: E402
import session  # noqa: E402


LIFECYCLE_KEYS = (
    "CONTEXT_AGENT_PROJECT_ROOT", "CONTEXT_AGENT_PROJECT_ID",
    "CONTEXT_AGENT_SCOPE", "CONTEXT_AGENT_RUNTIME_SCOPE",
    "CONTEXT_AGENT_MODEL_SESSION_ID", "CONTEXT_AGENT_SESSION",
    "CONTEXT_AGENT_MCP_CONNECTION", "CONTEXT_AGENT_CONVERSATION_ID",
    "CONTEXT_AGENT_IDE_INSTANCE_ID", "CONTEXT_AGENT_IDE",
    "CONTEXT_AGENT_CLIENT",
)


def make_project(base: Path, name: str) -> Path:
    root = base / name
    (root / ".context").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f"[project]\nname='{name}'\n", encoding="utf-8")
    return root


def clean_env():
    return mock.patch.dict(os.environ, {key: "" for key in LIFECYCLE_KEYS},
                           clear=False)


def test_same_project_model_sessions_have_distinct_mutable_state_paths():
    with tempfile.TemporaryDirectory() as tmp, clean_env():
        root = make_project(Path(tmp), "shared")
        os.environ["CONTEXT_AGENT_PROJECT_ROOT"] = str(root)

        os.environ["CONTEXT_AGENT_SESSION"] = "session-vscode"
        scope_a = paths.resolve_runtime_scope(root=root)
        budget_a = budget.budget_path()
        capsule_a = capsule.current_path()
        session_a = session.current_session_file()
        dedup_a = dedup.scope_key()

        os.environ["CONTEXT_AGENT_SESSION"] = "session-codex"
        scope_b = paths.resolve_runtime_scope(root=root)
        budget_b = budget.budget_path()
        capsule_b = capsule.current_path()
        session_b = session.current_session_file()
        dedup_b = dedup.scope_key()

        assert paths.project_scope_key(root) in scope_a
        assert scope_a != scope_b
        assert budget_a != budget_b
        assert capsule_a != capsule_b
        assert session_a != session_b
        assert dedup_a != dedup_b


def test_same_session_name_in_different_projects_cannot_share_state():
    with tempfile.TemporaryDirectory() as tmp, clean_env():
        base = Path(tmp)
        project_a = make_project(base, "alpha")
        project_b = make_project(base, "beta")
        os.environ["CONTEXT_AGENT_SESSION"] = "session-same-name"

        os.environ["CONTEXT_AGENT_PROJECT_ROOT"] = str(project_a)
        scope_a = paths.resolve_runtime_scope(root=project_a)
        path_a = budget.budget_path()

        os.environ["CONTEXT_AGENT_PROJECT_ROOT"] = str(project_b)
        scope_b = paths.resolve_runtime_scope(root=project_b)
        path_b = budget.budget_path()

        assert paths.ensure_project_id(project_a) != paths.ensure_project_id(project_b)
        assert scope_a != scope_b
        assert path_a != path_b
        assert os.path.samefile(project_a, path_a.parents[2])
        assert os.path.samefile(project_b, path_b.parents[2])


def test_stdio_initialize_without_host_conversation_is_connection_isolated():
    with tempfile.TemporaryDirectory() as tmp, clean_env():
        root = make_project(Path(tmp), "stdio")
        os.environ["CONTEXT_AGENT_PROJECT_ROOT"] = str(root)
        captured = []

        def initialize():
            with mock.patch.object(mcp_server, "response",
                                   side_effect=lambda _id, result=None, **_kw:
                                   captured.append(result)), \
                 mock.patch.object(mcp_server, "start_auto_prime",
                                   return_value={"status": "ready"}):
                mcp_server.handle({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"clientInfo": {"name": "VS Code"}},
                })
            return captured[-1]

        first = initialize()
        second = initialize()
        assert first["mcpConnectionId"] != second["mcpConnectionId"]
        assert first["modelSessionId"] != second["modelSessionId"]
        assert first["project"]["project_id"] == second["project"]["project_id"]
        assert first["project"]["runtime_scope"] != second["project"]["runtime_scope"]


def test_four_parallel_ide_bindings_are_persisted_without_lost_updates():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(Path(tmp), "parallel")
        code = (
            "import identity; "
            f"print(identity.resolve_model_session(r'{root}'))"
        )
        clients = [
            ("vscode", "conv-vscode"),
            ("trae", "conv-trae"),
            ("claude-code", "conv-claude"),
            ("codex", "conv-codex"),
        ]
        processes = []
        for ide, conversation in clients:
            env = os.environ.copy()
            env.update({
                "PYTHONPATH": str(SCRIPTS),
                "CONTEXT_AGENT_PROJECT_ROOT": str(root),
                "CONTEXT_AGENT_IDE": ide,
                "CONTEXT_AGENT_CONVERSATION_ID": conversation,
            })
            for key in ("CONTEXT_AGENT_SESSION",
                        "CONTEXT_AGENT_MODEL_SESSION_ID",
                        "CONTEXT_AGENT_IDE_INSTANCE_ID"):
                env.pop(key, None)
            processes.append(subprocess.Popen(
                [sys.executable, "-c", code], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=env))

        sessions = []
        for proc in processes:
            stdout, stderr = proc.communicate(timeout=20)
            assert proc.returncode == 0, stderr
            sessions.append(stdout.strip())

        bindings = json.loads(
            (root / ".context" / "model_sessions.json").read_text(
                encoding="utf-8"))
        assert len(set(sessions)) == 4
        assert {entry["conversation_id"] for entry in bindings} == {
            conversation for _ide, conversation in clients
        }
        assert {entry["model_session_id"] for entry in bindings} == set(sessions)


def test_dashboard_status_cache_and_project_root_forwarding():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(Path(tmp), "dash")
        dashboard_server._STATUS_CACHE.clear()
        with mock.patch.object(dashboard_server, "run_script",
                               return_value={"root": str(root)}) as run:
            assert dashboard_server.cached_status(root)["root"] == str(root)
            assert dashboard_server.cached_status(root)["root"] == str(root)
            assert run.call_count == 1

        completed = SimpleNamespace(stdout="{}", stderr="", returncode=0)
        script = root / ".context" / "scripts" / "agent.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# test", encoding="utf-8")
        with mock.patch.object(dashboard_server.subprocess, "run",
                               return_value=completed) as run:
            dashboard_server.run_script(root, "agent", "--status")
        assert run.call_args.kwargs["env"]["CONTEXT_AGENT_PROJECT_ROOT"] == str(
            root.resolve())


def test_global_launcher_forwards_explicit_workspace_root():
    with tempfile.TemporaryDirectory() as tmp:
        installer = global_install.GlobalInstaller()
        installer.install_dir = Path(tmp) / ".context-agent"
        installer.source_dir = ROOT
        installer.install_dir.mkdir(parents=True)

        assert installer._create_launcher()
        launcher = (installer.install_dir / "launcher.py").read_text(
            encoding="utf-8")
        compile(launcher, "launcher.py", "exec")

        assert 'if "--project-root" in sys.argv' in launcher
        assert '*sys.argv[1:]' in launcher
