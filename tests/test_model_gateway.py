"""Tests for scripts/model_gateway.py (PHASE F).

Acceptance coverage:
  * optional gateway: registry file write/remove, /healthz probe contract
  * OpenAI-compatible surface: /v1/models, /v1/chat/completions (+stream)
  * honest usage recording: executed_by=gateway in router.db
  * clean error mapping (unknown model 404, no_api_key 401)
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import model_gateway  # noqa: E402
import router  # noqa: E402
import config_store  # noqa: E402
import secret_store  # noqa: E402


class GatewayHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.agent_dir = self.home / ".context-agent"
        self.project = base / "proj"
        (self.project / ".context").mkdir(parents=True)

        self._patches = [
            mock.patch.object(Path, "home", return_value=self.home),
            mock.patch.object(router, "AGENT_DIR", self.agent_dir),
            mock.patch.object(router, "PROVIDERS_PATH", self.agent_dir / "providers.json"),
            mock.patch.object(router, "MODELS_PATH", self.agent_dir / "models.json"),
            mock.patch.object(router, "HEALTH_PATH", self.agent_dir / "provider_health.json"),
            mock.patch.object(router, "GATEWAY_PATH", self.agent_dir / "gateway.json"),
        ]
        for patch in self._patches:
            patch.start()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), model_gateway.GatewayHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        model_gateway._register(self.port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get_json(self, path: str) -> dict:
        with urllib.request.urlopen(self.url(path), timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.url(path), data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Context-Project-Root": str(self.project)})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def fake_catalog(self):
        providers = [{"id": "openai", "enabled": True, "needs_key": True,
                      "type": "openai", "base_url": "https://api.openai.com/v1"}]
        models = [
            {"model_id": "gpt-4o-mini", "provider_id": "openai", "enabled": True,
             "remote_model_name": "gpt-4o-mini-2024", "quality_tier": "BALANCED"},
            {"model_id": "off-model", "provider_id": "openai", "enabled": False,
             "remote_model_name": "off", "quality_tier": "STRONG"},
        ]
        self._patches.append(mock.patch.object(router, "list_providers", return_value=providers))
        self._patches.append(mock.patch.object(
            router, "get_provider", side_effect=lambda pid: providers[0] if pid == "openai" else None))
        self._patches.append(mock.patch.object(router, "list_models", return_value=models))
        for patch in self._patches[-3:]:
            patch.start()


class RegistryAndHealthTests(GatewayHarness):
    def test_registry_file_written_with_port_and_pid(self):
        data = json.loads(router.GATEWAY_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["port"], self.port)
        self.assertIn("pid", data)

    def test_healthz_contract(self):
        payload = self.get_json("/healthz")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "context-agent-gateway")
        self.assertTrue(payload["primary_routing"])

    def test_unregister_removes_registry(self):
        import os
        model_gateway._unregister(os.getpid())
        self.assertFalse(router.GATEWAY_PATH.exists())

    def test_gateway_available_detects_running_gateway(self):
        status = router.gateway_available()
        self.assertTrue(status["available"])
        self.assertEqual(status["port"], self.port)


class ModelsEndpointTests(GatewayHarness):
    def test_lists_only_enabled_models_of_enabled_providers(self):
        self.fake_catalog()
        payload = self.get_json("/v1/models")
        self.assertEqual(payload["object"], "list")
        ids = [item["id"] for item in payload["data"]]
        self.assertEqual(ids, ["gpt-4o-mini"])
        self.assertEqual(payload["data"][0]["owned_by"], "openai")


class CompletionsEndpointTests(GatewayHarness):
    def test_unknown_model_returns_404(self):
        self.fake_catalog()
        status, payload = self.post("/v1/chat/completions",
                                    {"model": "nope", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "unknown_or_disabled_model")

    def test_no_api_key_maps_to_401(self):
        self.fake_catalog()
        with mock.patch.object(router, "execute_with_model",
                               return_value={"error": "no_api_key", "provider_id": "openai"}):
            status, payload = self.post(
                "/v1/chat/completions",
                {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["type"], "no_api_key")

    def test_successful_completion_returns_openai_shape_and_records_gateway_usage(self):
        self.fake_catalog()
        result = {"ok": True, "model_id": "gpt-4o-mini", "text": "hello from model",
                  "usage": {"input_tokens": 12, "output_tokens": 7},
                  "estimated_cost_usd": 0.00042, "duration_ms": 321}
        with mock.patch.object(router, "execute_with_model", return_value=result):
            status, payload = self.post(
                "/v1/chat/completions",
                {"model": "gpt-4o-mini",
                 "messages": [{"role": "system", "content": "be brief"},
                              {"role": "user", "content": "say hello"}]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["message"]["content"], "hello from model")
        self.assertEqual(payload["usage"]["prompt_tokens"], 12)
        self.assertEqual(payload["usage"]["completion_tokens"], 7)

        rows = router.history(self.project)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["executed_by"], "gateway")
        self.assertEqual(rows[0]["final_model"], "gpt-4o-mini")
        self.assertEqual(rows[0]["decision"], "EXECUTED")
        self.assertAlmostEqual(rows[0]["estimated_cost_usd"], 0.00042)

    def test_streaming_replays_text_as_sse_chunks(self):
        self.fake_catalog()
        # Mock execute_with_model_streaming to yield chunks incrementally.
        def fake_streaming(*args, **kwargs):
            text = "A" * 100
            for i in range(0, len(text), 10):
                yield text[i:i+10]
        with mock.patch.object(router, "execute_with_model_streaming", side_effect=fake_streaming):
            request = urllib.request.Request(
                self.url("/v1/chat/completions"),
                data=json.dumps({"model": "gpt-4o-mini", "stream": True,
                                 "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "X-Context-Project-Root": str(self.project)})
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertIn("text/event-stream", response.headers.get("Content-Type", ""))
                raw = response.read().decode("utf-8")
        lines = [line[len("data: "):] for line in raw.strip().split("\n") if line.startswith("data: ")]
        self.assertEqual(lines[-1], "[DONE]")
        chunks = [json.loads(line) for line in lines[:-1]]
        content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        self.assertEqual(content, "A" * 100)
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

    def test_gateway_preserves_tool_call_deltas_and_finish_reason(self):
        self.fake_catalog()

        def fake_streaming(*args, **kwargs):
            yield {"tool_calls": [{"index": 0, "id": "call_1",
                                   "function": {"name": "read_file",
                                                "arguments": "{\"path\":\"a.py\"}"}}]}

        with mock.patch.object(router, "execute_with_model_streaming",
                               side_effect=fake_streaming):
            request = urllib.request.Request(
                self.url("/v1/chat/completions"),
                data=json.dumps({"model": "gpt-4o-mini", "stream": True,
                                 "messages": [{"role": "user", "content": "read a.py"}]}).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "X-Context-Project-Root": str(self.project)})
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read().decode("utf-8")

        lines = [line[len("data: "):] for line in raw.splitlines()
                 if line.startswith("data: ") and line != "data: [DONE]"]
        chunks = [json.loads(line) for line in lines]
        tool_chunks = [c for c in chunks
                       if c["choices"][0]["delta"].get("tool_calls")]
        self.assertEqual(len(tool_chunks), 1)
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_auto_router_selects_before_provider_execution(self):
        self.fake_catalog()
        config_store.set_value("PROJECT", "execution.mode", "AUTO_ROUTER", self.project)
        config_store.set_value("PROJECT", "routing.enabled", True, self.project)
        result = {"ok": True, "model_id": "gpt-4o-mini", "text": "routed",
                  "usage": {"input_tokens": 4, "output_tokens": 2},
                  "estimated_cost_usd": 0.0, "duration_ms": 10}
        with mock.patch.object(secret_store, "has_secret", return_value=True), \
             mock.patch.object(router, "execute_with_model", return_value=result) as execute:
            status, payload = self.post(
                "/v1/chat/completions",
                {"model": "client-selected-expensive-model",
                 "messages": [{"role": "user", "content": "fix a typo"}]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "gpt-4o-mini")
        self.assertEqual(execute.call_args.args[1], "gpt-4o-mini")
        record = router.history(self.project, limit=1)[0]
        self.assertEqual(record["decision"], "AUTO_ROUTED_EXECUTED")
        self.assertEqual(record["executed_by"], "gateway_auto_router")

    def test_error_execution_never_records_usage(self):
        self.fake_catalog()
        with mock.patch.object(router, "execute_with_model",
                               return_value={"error": "execution_failed", "detail": "x"}):
            status, _ = self.post(
                "/v1/chat/completions",
                {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 502)
        self.assertEqual(router.history(self.project), [])


if __name__ == "__main__":
    unittest.main()
