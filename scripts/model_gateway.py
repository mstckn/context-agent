"""Optional local Model Gateway (MODE 3 / AUTO ROUTER infrastructure).

An OpenAI-compatible HTTP endpoint bound to 127.0.0.1 only:

  GET  /healthz              -> liveness probe (used by router.gateway_available)
  GET  /v1/models            -> OpenAI-format list of usable catalog models
  POST /v1/chat/completions  -> forwards to a configured provider model and
                                records usage in router.db (executed_by=gateway)

The gateway is OPTIONAL. Without it the router reports
"AUTO ROUTER NOT AVAILABLE FOR THIS IDE CONFIGURATION" and the system
falls back to IDE MODEL; nothing breaks.

Registry: writes ~/.context-agent/gateway.json {"port": N, "pid": P} on start
and removes it on exit so other components can feature-detect the gateway.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import signal
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import router  # noqa: E402

DEFAULT_PORT = 8791


def _register(port: int) -> None:
    router.AGENT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = router.GATEWAY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"port": int(port), "pid": os.getpid(),
                               "started_at": datetime.now().isoformat()}),
                   encoding="utf-8")
    os.replace(tmp, router.GATEWAY_PATH)


def _unregister(expected_pid: int) -> None:
    try:
        data = json.loads(router.GATEWAY_PATH.read_text(encoding="utf-8"))
        if int(data.get("pid", -1)) == int(expected_pid):
            router.GATEWAY_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _resolve_model(model_name: str, root: Path | None = None) -> dict | None:
    """Resolve by catalog model_id first, then by remote_model_name."""
    models = router.list_models()
    for model in models:
        if model.get("model_id") == model_name and router.model_enabled_for_project(root, model):
            return model
    for model in models:
        if model.get("remote_model_name") == model_name and router.model_enabled_for_project(root, model):
            return model
    return None


def _project_root_for(headers) -> Path | None:
    """Best-effort project root for usage recording. Never fatal."""
    candidate = headers.get("X-Context-Project-Root") or os.environ.get("CONTEXT_PROJECT_ROOT")
    if candidate and Path(candidate).is_dir():
        return Path(candidate)
    try:
        return Path(router.find_project_root())
    except Exception:
        return None


def _conversation_id_for(headers, body: dict, root: Path | None) -> str:
    """Resolve a conversation id without mutating process-global env."""
    for name in ("X-Opencode-Session", "X-Context-Conversation-Id",
                 "X-Context-Session-Id"):
        value = str(headers.get(name) or "").strip()
        if value:
            return value
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    for value in (body.get("conversation_id"), body.get("session_id"),
                  metadata.get("conversation_id"), metadata.get("session_id")):
        if str(value or "").strip():
            return str(value).strip()
    messages = body.get("messages") or []
    first_user = next((str(item.get("content", "")) for item in messages
                       if item.get("role") == "user"), "")
    seed = first_user or str(body.get("user") or "gateway-session")
    return "gateway-" + hashlib.sha256(
        f"{root or 'no-project'}\0{seed}".encode("utf-8")
    ).hexdigest()[:24]


def _record_usage(root: Path | None, model: dict, result: dict, prompt_text: str,
                  routing_decision: dict | None = None) -> None:
    if root is None or result.get("error"):
        return
    try:
        usage = result.get("usage", {})
        routed = routing_decision or {}
        router.record_route(root, {
            "task": prompt_text[:200],
            "task_type": routed.get("task_type", ""), "risk": routed.get("risk", ""),
            "complexity": routed.get("complexity", ""),
            "required_tier": routed.get("required_tier", ""),
            "initial_model": model["model_id"], "escalated_to": "",
            "final_model": model["model_id"], "provider_id": model.get("provider_id", ""),
            "decision": "AUTO_ROUTED_EXECUTED" if routing_decision else "EXECUTED",
            "reasons": routed.get("reasons", []) + ["executed through local model gateway"],
            "policy_version": routed.get("policy_version", 1),
            "context_tokens": int(usage.get("input_tokens", 0) or 0),
            "model_tokens": int(usage.get("output_tokens", 0) or 0),
            "estimated_cost_usd": result.get("estimated_cost_usd", 0.0),
            "duration_ms": result.get("duration_ms", 0),
            "executed_by": "gateway_auto_router" if routing_decision else "gateway",
            "outcome": "executed",
        })
    except Exception:
        pass  # usage recording must never break execution


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "ContextAgentGateway/1.0"

    def log_message(self, fmt, *args):  # quiet stderr; diagnostics stay explicit
        pass

    # ── helpers ────────────────────────────────────────────────────────

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, events: list[str]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for event in events:
            self.wfile.write(f"data: {event}\n\n".encode("utf-8"))
            self.wfile.flush()

    def _send_sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _send_sse_event(self, event_data: str) -> None:
        self.wfile.write(f"data: {event_data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # ── GET ────────────────────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            try:
                from config_store import effective_value
                routing_enabled = bool(effective_value(None, "routing.enabled", False))
            except Exception:
                routing_enabled = False
            self._send_json(200, {"status": "ok", "service": "context-agent-gateway",
                                  "primary_routing": True,
                                  "routing_enabled": routing_enabled,
                                  "time": datetime.now().isoformat()})
        elif path == "/v1/models":
            data = []
            root = _project_root_for(self.headers)
            for model in router.list_models():
                if not router.model_enabled_for_project(root, model):
                    continue
                provider = router.get_provider(model.get("provider_id")) or {}
                if not provider.get("enabled"):
                    continue
                data.append({"id": model["model_id"], "object": "model",
                             "created": 0, "owned_by": model.get("provider_id", "")})
            self._send_json(200, {"object": "list", "data": data})
        elif path == "/":
            self._send_json(200, {"service": "context-agent-gateway",
                                  "endpoints": ["/healthz", "/v1/models",
                                                "/v1/chat/completions"]})
        else:
            self._send_json(404, {"error": "not_found", "path": path})

    # ── POST /v1/chat/completions ─────────────────────────────────────

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/v1/chat/completions":
            self._send_json(404, {"error": "not_found", "path": path})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8", errors="replace"))
        except Exception:
            self._send_json(400, {"error": "invalid_json"})
            return

        messages = body.get("messages") or []
        if not messages:
            self._send_json(400, {"error": "messages_required"})
            return

        system = ""
        prompt_parts = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            if role == "system" and not system:
                system = str(content)
            else:
                prompt_parts.append(f"[{role}] {content}" if role != "user" else str(content))
        prompt_text = "\n\n".join(prompt_parts)
        max_tokens = int(body.get("max_tokens") or 2048)
        stream = bool(body.get("stream"))

        root = _project_root_for(self.headers)
        conversation_id = _conversation_id_for(self.headers, body, root)
        routing_decision = None
        try:
            from config_store import effective_value
            configured_mode = str(effective_value(root, "execution.mode", "IDE_MODEL")).upper()
            routing_enabled = bool(effective_value(root, "routing.enabled", False))
        except Exception:
            configured_mode, routing_enabled = "IDE_MODEL", False

        if configured_mode == "AUTO_ROUTER" and routing_enabled:
            analysis = body.get("context_analysis")
            if not isinstance(analysis, dict):
                try:
                    import route as context_route
                    task_info = context_route.classify_task(prompt_text)
                    analysis = context_route.machine_readable_analysis(
                        task_info, {"level": "medium", "score": 50})
                except Exception:
                    analysis = {"task_type": "UNKNOWN", "risk": "LOW",
                                "complexity": "medium"}
            if body.get("tools"):
                analysis = dict(analysis)
                analysis["requires_tools"] = True
            routing_decision = router.route_task(
                root or Path.cwd(), analysis, record=False,
                task_text=prompt_text, context_tokens=max(1, len(prompt_text) // 4))
            if routing_decision.get("decision") != "ROUTED":
                self._send_json(503, {"error": "auto_routing_blocked",
                                      "routing": routing_decision})
                return
            requested_model = routing_decision.get("final_model", "")
        else:
            requested_model = str(body.get("model", ""))

        model = _resolve_model(requested_model, root)
        if not model:
            self._send_json(404, {"error": "unknown_or_disabled_model",
                                  "model": requested_model})
            return

        started = time.time()
        completion_id = f"chatcmpl-gw-{int(started * 1000)}"
        created = int(started)

        if stream:
            # Real streaming: forward provider chunks as they arrive.
            self._send_sse_start()
            # Initial role chunk.
            self._send_sse_event(json.dumps(
                {"id": completion_id, "object": "chat.completion.chunk",
                 "created": created, "model": model["model_id"],
                 "choices": [{"index": 0, "delta": {"role": "assistant"},
                              "finish_reason": None}]}))
            accumulated = []
            saw_tool_calls = False
            for chunk in router.execute_with_model_streaming(
                    root or Path.cwd(), model["model_id"],
                    prompt_text, system=system, max_tokens=max_tokens,
                    session_id=conversation_id):
                if isinstance(chunk, dict) and chunk.get("error"):
                    # Error mid-stream: send error event and terminate.
                    self._send_sse_event(json.dumps(
                        {"id": completion_id, "object": "chat.completion.chunk",
                         "created": created, "model": model["model_id"],
                         "choices": [{"index": 0, "delta": {},
                                      "finish_reason": "error"}],
                         "error": chunk}))
                    self._send_sse_event("[DONE]")
                    return
                if isinstance(chunk, dict) and chunk.get("tool_calls"):
                    saw_tool_calls = True
                    delta = {"tool_calls": chunk["tool_calls"]}
                else:
                    text_chunk = str(chunk)
                    accumulated.append(text_chunk)
                    delta = {"content": text_chunk}
                self._send_sse_event(json.dumps(
                    {"id": completion_id, "object": "chat.completion.chunk",
                     "created": created, "model": model["model_id"],
                     "choices": [{"index": 0, "delta": delta,
                                  "finish_reason": None}]}))
            # Finish.
            self._send_sse_event(json.dumps(
                {"id": completion_id, "object": "chat.completion.chunk",
                 "created": created, "model": model["model_id"],
                 "choices": [{"index": 0, "delta": {},
                              "finish_reason": "tool_calls" if saw_tool_calls else "stop"}]}))
            self._send_sse_event("[DONE]")
            # Record usage after stream completes.
            full_text = "".join(accumulated)
            _record_usage(root, model, {"text": full_text, "ok": True,
                                        "usage": {"input_tokens": 0, "output_tokens": 0}},
                          prompt_text, routing_decision)
        else:
            result = router.execute_with_model(root or Path.cwd(), model["model_id"],
                                               prompt_text, system=system,
                                               max_tokens=max_tokens,
                                               session_id=conversation_id)
            if result.get("error"):
                status = 401 if result["error"] == "no_api_key" else 502
                self._send_json(status, {"error": {"message": result["error"],
                                                   "type": result["error"],
                                                   "provider_id": result.get("provider_id", "")}})
                return

            _record_usage(root, model, result, prompt_text, routing_decision)
            text = result.get("text", "")
            usage = result.get("usage", {})
            self._send_json(200, {
                "id": completion_id, "object": "chat.completion", "created": created,
                "model": model["model_id"],
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": int(usage.get("input_tokens", 0) or 0),
                          "completion_tokens": int(usage.get("output_tokens", 0) or 0),
                          "total_tokens": (int(usage.get("input_tokens", 0) or 0)
                                           + int(usage.get("output_tokens", 0) or 0))},
            })


def serve(port: int) -> int:
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), GatewayHandler)
    except OSError as exc:
        print(json.dumps({"error": "port_unavailable", "port": port,
                          "detail": str(exc)}))
        return 1
    _register(port)
    atexit.register(_unregister, os.getpid())
    print(json.dumps({"started": True, "port": port, "pid": os.getpid(),
                      "registry": str(router.GATEWAY_PATH)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _unregister(os.getpid())
        server.server_close()
    return 0


def status() -> int:
    print(json.dumps(router.gateway_available(), indent=2))
    return 0


def stop() -> int:
    data = router._read_json(router.GATEWAY_PATH, None)
    if not data or not data.get("pid"):
        print(json.dumps({"running": False}))
        return 0
    pid = int(data["pid"])
    try:
        if sys.platform == "win32":
            os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
        else:
            os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not router.gateway_available().get("available"):
                break
            time.sleep(0.25)
        _unregister(pid)
        print(json.dumps({"stopped": True, "pid": pid}))
    except ProcessLookupError:
        _unregister(pid)
        print(json.dumps({"stopped": True, "stale_registry": True, "pid": pid}))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Optional local model gateway (127.0.0.1 only)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("status")
    sub.add_parser("stop")
    args = parser.parse_args()
    if args.cmd == "start":
        raise SystemExit(serve(args.port))
    if args.cmd == "status":
        raise SystemExit(status())
    raise SystemExit(stop())


if __name__ == "__main__":
    main()
