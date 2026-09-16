"""OpenCode Zen transport, migration, and one-shot quality guard tests."""

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mcp_server  # noqa: E402
import one_shot  # noqa: E402
import router  # noqa: E402
import config_store  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
        self.headers = {"Content-Type": "application/json"}
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def test_old_catalog_gets_additive_zen_migration(tmp_path):
    providers = tmp_path / "providers.json"
    providers.write_text(json.dumps({"providers": [{"id": "custom"}]}), encoding="utf-8")
    with mock.patch.object(router, "PROVIDERS_PATH", providers):
        ids = {item["id"] for item in router.list_providers()}
        assert "custom" in ids
        assert "opencode" in ids
        assert json.loads(providers.read_text(encoding="utf-8"))["catalog_version"] == 3


def test_go_catalog_and_shared_zen_secret_are_migrated(tmp_path):
    providers = tmp_path / "providers.json"
    models = tmp_path / "models.json"
    providers.write_text(json.dumps({"providers": [], "catalog_version": 2}), encoding="utf-8")
    models.write_text(json.dumps({"models": [], "catalog_version": 2}), encoding="utf-8")
    with mock.patch.object(router, "PROVIDERS_PATH", providers), \
         mock.patch.object(router, "MODELS_PATH", models):
        go = next(item for item in router.list_providers() if item["id"] == "opencode_go")
        model_ids = {item["model_id"] for item in router.list_models()}
    assert router.provider_secret_id(go) == "opencode"
    assert "opencode-go/muse-spark-1.3-contributor" in model_ids
    assert "opencode-go/qwen3.8-max" in model_ids


def test_routing_priority_prefers_muse_13_over_12(tmp_path):
    (tmp_path / ".context").mkdir()
    provider = next(p for p in router.DEFAULT_PROVIDERS if p["id"] == "opencode_go") | {
        "enabled": True
    }
    models = [m for m in router.DEFAULT_MODELS if m["model_id"] in {
        "opencode-go/muse-spark-1.2-contributor",
        "opencode-go/muse-spark-1.3-contributor",
    }]
    with mock.patch.object(router, "list_providers", return_value=[provider]), \
         mock.patch.object(router, "list_models", return_value=models), \
         mock.patch.object(router, "load_health", return_value={}), \
         mock.patch.object(router.secret_store, "has_secret", return_value=True), \
         mock.patch.object(router, "usage_summary", return_value={
             "task_last": {"estimated_cost_usd": 0},
             "today": {"estimated_cost_usd": 0},
             "month": {"estimated_cost_usd": 0},
         }):
        result = router.route_task(tmp_path, {
            "task_type": "ARCHITECTURE", "risk": "HIGH",
            "reasoning_requirement": "high",
        }, record=False)
    assert result["final_model"] == "opencode-go/muse-spark-1.3-contributor"


def test_zen_responses_transport_and_parser(tmp_path):
    provider = next(p for p in router.DEFAULT_PROVIDERS if p["id"] == "opencode") | {
        "enabled": True
    }
    model = next(m for m in router.DEFAULT_MODELS
                 if m["model_id"] == "opencode/muse-spark-1.3-contributor-free")
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["auth"] = request.headers.get("Authorization")
        captured["user_agent"] = request.headers.get("User-agent")
        captured["session"] = dict((k.lower(), v) for k, v in request.header_items()).get(
            "x-opencode-session")
        return FakeResponse({
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "verified answer"}]}],
            "usage": {"input_tokens": 21, "output_tokens": 4},
        })

    with mock.patch.object(router, "list_models", return_value=[model]), \
         mock.patch.object(router, "get_provider", return_value=provider), \
         mock.patch.object(router.secret_store, "has_secret", return_value=True), \
         mock.patch.object(router.secret_store, "get_secret", return_value="zen-secret"), \
         mock.patch.object(router.urllib.request, "urlopen", side_effect=fake_urlopen):
        result = router.execute_with_model(tmp_path, model["model_id"], "task", system="rules")

    assert captured["url"].endswith("/responses")
    assert captured["body"]["input"] == "task"
    assert captured["body"]["instructions"] == "rules"
    assert captured["auth"] == "Bearer zen-secret"
    assert captured["user_agent"].startswith("context-agent/")
    assert captured["session"].startswith("ca-")
    assert result["text"] == "verified answer"
    assert result["usage"] == {"input_tokens": 21, "output_tokens": 4}
    assert result["privacy_notice"]


def test_zen_chat_completions_transport(tmp_path):
    provider = next(p for p in router.DEFAULT_PROVIDERS if p["id"] == "opencode") | {
        "enabled": True
    }
    model = next(m for m in router.DEFAULT_MODELS
                 if m["model_id"] == "opencode/big-pickle")
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["session"] = dict((k.lower(), v) for k, v in request.header_items()).get(
            "x-opencode-session")
        return FakeResponse({"choices": [{"message": {"content": "ok"}}],
                             "usage": {"prompt_tokens": 7, "completion_tokens": 2}})

    with mock.patch.object(router, "list_models", return_value=[model]), \
         mock.patch.object(router, "get_provider", return_value=provider), \
         mock.patch.object(router.secret_store, "has_secret", return_value=True), \
         mock.patch.object(router.secret_store, "get_secret", return_value="zen-secret"), \
         mock.patch.object(router.urllib.request, "urlopen", side_effect=fake_urlopen):
        result = router.execute_with_model(tmp_path, model["model_id"], "task")

    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["model"] == "big-pickle"
    assert captured["session"].startswith("ca-")
    assert result["text"] == "ok"


def test_opencode_session_is_stable_per_conversation_and_project_isolated(tmp_path):
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    (project_a / ".context").mkdir(parents=True)
    (project_b / ".context").mkdir(parents=True)

    first = router._opencode_session_id(project_a, "conversation-42")
    repeated = router._opencode_session_id(project_a, "conversation-42")
    other_conversation = router._opencode_session_id(project_a, "conversation-43")
    other_project = router._opencode_session_id(project_b, "conversation-42")

    assert first == repeated
    assert first != other_conversation
    assert first != other_project
    assert str(project_a) not in first


def test_opencode_health_probe_has_session_header(tmp_path):
    provider = next(p for p in router.DEFAULT_PROVIDERS if p["id"] == "opencode") | {
        "enabled": True
    }
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured.update(dict((k.lower(), v) for k, v in request.header_items()))
        return FakeResponse({"data": []})

    with mock.patch.object(router, "HEALTH_PATH", tmp_path / "health.json"), \
         mock.patch.object(router.secret_store, "has_secret", return_value=True), \
         mock.patch.object(router.secret_store, "get_secret", return_value="zen-secret"), \
         mock.patch.object(router.urllib.request, "urlopen", side_effect=fake_urlopen):
        result = router.check_provider_health(provider, force=True)

    assert result["status"] == "CONNECTED"
    assert captured["x-opencode-session"].startswith("ca-")


def test_one_shot_blocks_before_provider_when_context_is_insufficient(tmp_path):
    package = {"sections": {"context_sufficiency": {
        "sufficient": False, "read_directly": ["missing.py"]}}}
    def feature_value(_root, key, default=None):
        return True if key in ("routing.enabled", "context.optimization_enabled") else default

    with mock.patch.object(config_store, "effective_value", side_effect=feature_value), \
         mock.patch.object(one_shot, "execution_readiness", return_value=None), \
         mock.patch.object(one_shot, "find_project_root", return_value=tmp_path), \
         mock.patch.object(one_shot, "build_isolated_context", return_value=package), \
         mock.patch.object(one_shot.router, "execute_with_model") as execute:
        result = one_shot.run_one_shot("fix bug")
    assert result["error"] == "context_insufficient"
    execute.assert_not_called()


def test_mcp_exposes_one_shot_tool():
    schema = {item["name"]: item for item in mcp_server.tool_schema()}
    assert "context_agent_one_shot" in schema
    assert schema["context_agent_one_shot"]["inputSchema"]["required"] == ["task"]


def test_one_shot_build_sets_both_fresh_session_ids(tmp_path):
    (tmp_path / ".context" / "scripts").mkdir(parents=True)
    (tmp_path / ".context" / "scripts" / "agent.py").write_text("", encoding="utf-8")
    captured = {}

    def fake_run(*_args, **kwargs):
        captured["env"] = kwargs["env"]
        return type("Result", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()

    with mock.patch.object(one_shot.subprocess, "run", side_effect=fake_run):
        one_shot.build_isolated_context(tmp_path, "task", "model", 1000, 0, 0)
    env = captured["env"]
    assert env["CONTEXT_AGENT_SESSION"] == env["CONTEXT_AGENT_MODEL_SESSION_ID"]
    assert env["CONTEXT_AGENT_SESSION"].startswith("oneshot-")


def test_global_model_toggle_applies_to_all_projects(tmp_path):
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    (project_a / ".context").mkdir(parents=True)
    (project_b / ".context").mkdir(parents=True)
    model = dict(next(m for m in router.DEFAULT_MODELS
                      if m["model_id"] == "opencode/mimo-v2.5-free"))
    assert router.model_enabled_for_project(project_a, model) is True
    assert router.model_enabled_for_project(project_b, model) is True
    model["enabled"] = False
    assert router.model_enabled_for_project(project_a, model) is False
    assert router.model_enabled_for_project(project_b, model) is False


def test_provider_execution_is_off_by_default(tmp_path):
    (tmp_path / ".context").mkdir()
    with mock.patch.object(one_shot, "find_project_root", return_value=tmp_path), \
         mock.patch.object(one_shot, "build_isolated_context") as build:
        result = one_shot.run_one_shot("task")
    assert result["error"] == "provider_routing_disabled"
    build.assert_not_called()


def test_mcp_context_toggle_blocks_context_build():
    with mock.patch.object(mcp_server, "project_feature_flags",
                           return_value={"context": False, "routing": False}), \
         mock.patch.object(mcp_server, "run_script") as run:
        result = mcp_server.call_tool("context_agent_build_context", {"task": "x"})
    assert result["error"] == "context_optimization_disabled"
    run.assert_not_called()
