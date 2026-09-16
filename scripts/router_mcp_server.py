#!/usr/bin/env python3
"""
router_mcp_server.py — OPTIONAL Router MCP (autoroute spec §4, §25).

A standalone MCP server (stdio, JSON-RPC) exposing exactly five tools:

  route_task            deterministic routing decision from Context
                        Intelligence machine-readable analysis
  delegate_task         HYBRID delegation (IDE model stays primary)
  execute_with_model    direct execution through a configured provider
  get_model_candidates  ranked candidate models for a task analysis
  explain_route         full audit record for a recorded route id

Hard separation (§46): this process never touches the Context core. If it
crashes, is disabled, or is not installed, Context Intelligence keeps full
functionality. Dependency direction is router -> Context Intelligence only.
"""

from __future__ import annotations

import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from paths import find_project_root
    import router
except ImportError:  # pragma: no cover
    from scripts.paths import find_project_root
    from scripts import router

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "route_task",
        "description": (
            "Deterministic model routing decision. Consumes Context Intelligence "
            "machine-readable analysis (task_type, complexity, risk, "
            "reasoning_requirement, context_confidence). Never re-classifies the "
            "task. Returns ROUTED / ROUTING_BLOCKED / MANUAL_OVERRIDE_REQUIRED."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "analysis": {
                    "type": "object",
                    "description": "machine_readable block from context_agent route output",
                },
                "task": {"type": "string"},
                "context_tokens": {"type": "integer"},
                "record": {"type": "boolean", "default": True},
            },
            "required": ["analysis"],
        },
    },
    {
        "name": "delegate_task",
        "description": (
            "HYBRID mode delegation: route the task, then EXECUTE the delegated "
            "subtask against the selected provider/model. The IDE model remains "
            "the primary agent; the router is the delegated executor. Returns "
            "the actual generated result, usage, and cost. Telemetry records "
            "executed_by=router_hybrid."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "analysis": {"type": "object"},
                "task": {"type": "string"},
                "context_tokens": {"type": "integer"},
                "prompt": {"type": "string", "description": "Actual prompt to send to the delegated model. Falls back to task if omitted."},
                "system": {"type": "string", "description": "Optional system instruction for the delegated model."},
            },
            "required": ["analysis"],
        },
    },
    {
        "name": "execute_with_model",
        "description": (
            "Execute a prompt with a configured provider model. Requires an "
            "enabled provider and stored secret; otherwise returns a clean error."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "catalog model_id"},
                "prompt": {"type": "string"},
                "system": {"type": "string"},
                "max_tokens": {"type": "integer", "default": 2048},
            },
            "required": ["model", "prompt"],
        },
    },
    {
        "name": "get_model_candidates",
        "description": (
            "Ranked usable models for a task analysis: sufficient tier first, "
            "then lowest expected cost. Includes why each candidate qualifies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"analysis": {"type": "object"}},
            "required": ["analysis"],
        },
    },
    {
        "name": "explain_route",
        "description": "Return the recorded routing-history entry for a route id.",
        "inputSchema": {
            "type": "object",
            "properties": {"route_id": {"type": "integer"}},
            "required": ["route_id"],
        },
    },
]


def call_tool(name, args):
    args = args or {}
    try:
        root = find_project_root()
    except Exception as exc:
        if exc.__class__.__name__ == "ProjectRootSelectionRequired" and hasattr(exc, "to_dict"):
            return exc.to_dict()
        raise

    status = router.router_status(root)
    if name == "route_task":
        if not status["routing_enabled"]:
            return {"decision": "NOT_ENABLED",
                    "message": "Router is disabled. Context Intelligence works "
                               "without it; enable routing in Settings to use this tool."}
        return router.route_task(root, args.get("analysis") or {},
                                 record=bool(args.get("record", True)),
                                 task_text=args.get("task", ""),
                                 context_tokens=int(args.get("context_tokens") or 0))
    if name == "delegate_task":
        if not status["routing_enabled"]:
            return {"decision": "NOT_ENABLED",
                    "message": "Router is disabled; HYBRID delegation unavailable."}
        return router.delegate_task(root, args.get("analysis") or {},
                                    task_text=args.get("task", ""),
                                    context_tokens=int(args.get("context_tokens") or 0),
                                    prompt=args.get("prompt", ""),
                                    system=args.get("system", ""))
    if name == "execute_with_model":
        return router.execute_with_model(root, args.get("model", ""),
                                         args.get("prompt", ""),
                                         system=args.get("system", ""),
                                         max_tokens=int(args.get("max_tokens") or 2048))
    if name == "get_model_candidates":
        analysis = args.get("analysis") or {}
        policy = router.load_policy(root)
        required, reasons = router.required_tier_for(analysis, policy)
        usable = router._usable_models(policy, root)
        ranked = [m for m in usable
                  if router.TIER_ORDER[m["quality_tier"]] >= router.TIER_ORDER[required]]
        ranked.sort(key=lambda m: (router.TIER_ORDER[m["quality_tier"]],
                                   float(m.get("input_cost", 0)), m["model_id"]))
        return {
            "required_tier": required,
            "reasons": reasons,
            "candidates": [
                {k: m.get(k) for k in ("model_id", "display_name", "quality_tier",
                                       "input_cost", "output_cost", "context_window",
                                       "max_output", "latency_class", "provider_id")}
                | {"provider_health": m.get("provider_health")}
                for m in ranked[:10]
            ],
        }
    if name == "explain_route":
        return router.explain_route(root, int(args.get("route_id") or 0)) or {"error": "not_found"}
    return {"error": f"unknown_tool: {name}"}


def response(request_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": request_id}
    if error:
        payload["error"] = error
    else:
        payload["result"] = result if result is not None else {}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def content_result(data):
    return {"content": [{"type": "text",
                         "text": json.dumps(data, ensure_ascii=False, indent=2)}]}


def handle(request):
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        response(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "context-agent-router", "version": "1.0.0"},
            "instructions": (
                "Optional routing layer for Context Intelligence. Requires "
                "routing.enabled=true in project/global configuration. The "
                "Context core works fully without this server."
            ),
        })
    elif method == "notifications/initialized":
        return
    elif method == "tools/list":
        response(request_id, {"tools": TOOLS})
    elif method == "tools/call":
        try:
            response(request_id, content_result(
                call_tool(params.get("name"), params.get("arguments") or {})))
        except Exception as exc:
            response(request_id, content_result(
                {"error": "router_failed", "message": str(exc)[:500]}))
    elif method == "ping":
        response(request_id, {})
    else:
        response(request_id, error={"code": -32601,
                                    "message": f"Method not found: {method}"})


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            handle(json.loads(line))
        except Exception as exc:
            response(None, error={"code": -32603, "message": str(exc)})


if __name__ == "__main__":
    main()
