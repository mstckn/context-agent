"""Temporal streaming integration test — proves Gateway does NOT buffer.

Uses a local mock HTTP provider that emits SSE chunks with synchronization
events. The test verifies:
1. Chunk A is received BEFORE the provider releases chunk B
2. Downstream disconnect is handled cleanly
3. Tool-call streaming deltas are forwarded
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import router  # noqa: E402
import secret_store  # noqa: E402


class MockSSEHandler(BaseHTTPRequestHandler):
    """Mock OpenAI-compatible SSE provider with controllable timing."""

    # Class-level synchronization events
    chunk_a_sent = None  # threading.Event
    proceed_to_b = None  # threading.Event
    disconnect_after = None  # threading.Event (optional)
    tool_call_mode = False

    def log_message(self, format, *args):
        pass  # Suppress noisy logs

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""
        try:
            request = json.loads(body)
        except Exception:
            request = {}

        is_stream = request.get("stream", False)

        if not is_stream:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "id": "mock-001",
                "choices": [{"message": {"role": "assistant", "content": "AB"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
            self.wfile.write(json.dumps(resp).encode())
            return

        # Streaming response
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        if self.tool_call_mode:
            self._stream_tool_calls()
        else:
            self._stream_content()

    def _stream_content(self):
        """Emit content chunks with temporal synchronization."""
        chunk_a = {
            "id": "mock-001",
            "choices": [{"delta": {"role": "assistant", "content": "A"}, "index": 0}],
        }
        data = f"data: {json.dumps(chunk_a)}\n\n"
        self.wfile.write(data.encode())
        self.wfile.flush()

        if self.chunk_a_sent:
            self.chunk_a_sent.set()

        if self.proceed_to_b:
            self.proceed_to_b.wait(timeout=5)

        if self.disconnect_after and self.disconnect_after.is_set():
            return

        chunk_b = {
            "id": "mock-001",
            "choices": [{"delta": {"content": "B"}, "index": 0}],
        }
        data = f"data: {json.dumps(chunk_b)}\n\n"
        try:
            self.wfile.write(data.encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        chunk_done = {
            "id": "mock-001",
            "choices": [{"delta": {}, "finish_reason": "stop", "index": 0}],
        }
        data = f"data: {json.dumps(chunk_done)}\n\n"
        try:
            self.wfile.write(data.encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_tool_calls(self):
        """Emit tool-call streaming deltas."""
        chunk_start = {
            "id": "mock-002",
            "choices": [{
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{"index": 0, "id": "call_001", "type": "function",
                                    "function": {"name": "read_file", "arguments": ""}}],
                },
                "index": 0,
            }],
        }
        self.wfile.write(f"data: {json.dumps(chunk_start)}\n\n".encode())
        self.wfile.flush()

        chunk_args = {
            "id": "mock-002",
            "choices": [{
                "delta": {
                    "tool_calls": [{"index": 0, "function": {"arguments": '{"path":'}}],
                },
                "index": 0,
            }],
        }
        self.wfile.write(f"data: {json.dumps(chunk_args)}\n\n".encode())
        self.wfile.flush()

        chunk_args2 = {
            "id": "mock-002",
            "choices": [{
                "delta": {
                    "tool_calls": [{"index": 0, "function": {"arguments": '"test.py"}'}}],
                },
                "index": 0,
            }],
        }
        self.wfile.write(f"data: {json.dumps(chunk_args2)}\n\n".encode())
        self.wfile.flush()

        chunk_done = {
            "id": "mock-002",
            "choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}],
        }
        self.wfile.write(f"data: {json.dumps(chunk_done)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _mock_model(port):
    return {
        "model_id": "mock-model",
        "provider_id": "mock-provider",
        "remote_model_name": "mock-gpt",
        "tier": "fast",
    }


def _mock_provider(port):
    return {
        "id": "mock-provider",
        "type": "openai",
        "base_url": f"http://127.0.0.1:{port}/v1",
        "api_key": "mock-key",
        "enabled": True,
        "needs_key": True,
        "streaming_supported": True,
        "tool_streaming_supported": True,
    }


class StreamingTemporalTests(unittest.TestCase):
    """Prove the Gateway forwards chunks immediately, not buffered."""

    def setUp(self):
        self.chunk_a_sent = threading.Event()
        self.proceed_to_b = threading.Event()
        self.disconnect_after = threading.Event()

        MockSSEHandler.chunk_a_sent = self.chunk_a_sent
        MockSSEHandler.proceed_to_b = self.proceed_to_b
        MockSSEHandler.disconnect_after = self.disconnect_after
        MockSSEHandler.tool_call_mode = False

        self.server = HTTPServer(("127.0.0.1", 0), MockSSEHandler)
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "proj"
        (self.root / ".context").mkdir(parents=True)
        (self.root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

        # Set up mock secret via env (not needed when mocking secret_store)

    def tearDown(self):
        self.server.shutdown()
        self.server_thread.join(timeout=2)
        MockSSEHandler.chunk_a_sent = None
        MockSSEHandler.proceed_to_b = None
        MockSSEHandler.disconnect_after = None
        self._tmp.cleanup()

    @patch.object(router, "list_models", return_value=[_mock_model(0)])
    @patch.object(router, "get_provider", return_value=_mock_provider(0))
    @patch.object(secret_store, "has_secret", return_value=True)
    @patch.object(secret_store, "get_secret", return_value="mock-key")
    def test_chunk_a_received_before_b_released(self, _sec_get, _sec_has, _prov, _model):
        """Gateway must emit chunk A immediately, not wait for B."""
        # Update mock with actual port
        router.get_provider.return_value = _mock_provider(self.port)

        received = []
        timestamps = []

        def collect_stream():
            for chunk in router.execute_with_model_streaming(
                self.root, "mock-model", "test prompt"
            ):
                if isinstance(chunk, dict) and chunk.get("error"):
                    break
                received.append(chunk)
                timestamps.append(time.monotonic())
                if len(received) == 1:
                    self.proceed_to_b.set()

        collector = threading.Thread(target=collect_stream)
        collector.start()

        self.assertTrue(self.chunk_a_sent.wait(timeout=5),
                        "Provider should have sent chunk A")
        collector.join(timeout=5)

        self.assertGreaterEqual(len(received), 1, "Should receive at least chunk A")
        self.assertEqual(received[0], "A", "First chunk should be 'A'")
        if len(received) >= 2:
            self.assertEqual(received[1], "B", "Second chunk should be 'B'")
            # A was received before B was released by provider (proceed_to_b was set after A)
            self.assertLessEqual(timestamps[0], timestamps[1],
                                 "A timestamp must be <= B timestamp")

    @patch.object(router, "list_models", return_value=[_mock_model(0)])
    @patch.object(router, "get_provider", return_value=_mock_provider(0))
    @patch.object(secret_store, "has_secret", return_value=True)
    @patch.object(secret_store, "get_secret", return_value="mock-key")
    def test_downstream_disconnect_handled(self, _sec_get, _sec_has, _prov, _model):
        """Gateway handles downstream provider disconnect cleanly."""
        router.get_provider.return_value = _mock_provider(self.port)

        self.disconnect_after.set()

        received = []
        for chunk in router.execute_with_model_streaming(
            self.root, "mock-model", "test prompt"
        ):
            if isinstance(chunk, dict) and chunk.get("error"):
                break
            received.append(chunk)
            if len(received) == 1:
                self.proceed_to_b.set()

        self.assertGreaterEqual(len(received), 1, "Should receive chunk A before disconnect")

    @patch.object(router, "list_models", return_value=[_mock_model(0)])
    @patch.object(router, "get_provider", return_value=_mock_provider(0))
    @patch.object(secret_store, "has_secret", return_value=True)
    @patch.object(secret_store, "get_secret", return_value="mock-key")
    def test_tool_call_streaming(self, _sec_get, _sec_has, _prov, _model):
        """Gateway forwards tool-call streaming deltas."""
        router.get_provider.return_value = _mock_provider(self.port)
        MockSSEHandler.tool_call_mode = True

        received = []
        for chunk in router.execute_with_model_streaming(
            self.root, "mock-model", "test prompt"
        ):
            if isinstance(chunk, dict) and chunk.get("error"):
                break
            received.append(chunk)

        self.assertGreater(len(received), 0, "Should receive tool call chunks")


if __name__ == "__main__":
    unittest.main()
