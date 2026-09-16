#!/usr/bin/env python3
"""Build isolated compact context and execute one model request.

The inference is intentionally stateless: every invocation gets a fresh model
session id so Context Agent never omits files that only another IDE/model saw.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

try:
    import mcp_server
    import router
    from paths import find_project_root
except ImportError:  # pragma: no cover
    from scripts import mcp_server, router
    from scripts.paths import find_project_root


DEFAULT_MODEL = "auto"


def execution_readiness(root: Path, model_id: str) -> dict | None:
    model = next((item for item in router.list_models()
                  if item.get("model_id") == model_id), None)
    if not model:
        return {"error": "unknown_model", "model_id": model_id}
    if not router.model_enabled_for_project(root, model):
        return {"error": "model_disabled_globally", "model_id": model_id}
    provider = router.get_provider(model.get("provider_id"))
    if not provider or not provider.get("enabled"):
        return {"error": "provider_not_enabled",
                "provider_id": model.get("provider_id"), "model_id": model_id}
    if provider.get("needs_key") and not router.secret_store.has_secret(
            router.provider_secret_id(provider)):
        return {"error": "no_api_key", "provider_id": provider["id"],
                "model_id": model_id}
    return None


def build_isolated_context(root: Path, task: str, model: str, budget: int | None,
                           strict_quality: int, directive_budget: int,
                           session_id: str = "") -> dict:
    script = root / ".context" / "scripts" / "agent.py"
    cmd = [sys.executable, str(script), task, "--model", model,
           "--strict-quality", str(strict_quality),
           "--directive-budget", str(directive_budget)]
    if budget is not None:
        cmd += ["--budget", str(budget)]
    env = dict(os.environ)
    # A one-shot provider has no earlier conversational state. A fresh scope
    # prevents cross-turn dedup from withholding context known only to an IDE.
    session_id = session_id or f"oneshot-{uuid.uuid4().hex}"
    env["CONTEXT_AGENT_MODEL_SESSION_ID"] = session_id
    # agent.ledger_session_id uses CONTEXT_AGENT_SESSION while runtime-scope
    # isolation prefers MODEL_SESSION_ID. Set both to the same fresh identity.
    env["CONTEXT_AGENT_SESSION"] = session_id
    env["CONTEXT_AGENT_CLIENT"] = "one-shot"
    result = subprocess.run(cmd, cwd=str(root), env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=120)
    if result.returncode != 0:
        return {"error": "context_build_failed", "returncode": result.returncode,
                "stderr": result.stderr[-2000:]}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "invalid_context_output", "stdout": result.stdout[-1000:]}


def run_one_shot(task: str, model: str = DEFAULT_MODEL, budget: int | None = None,
                 max_output_tokens: int = 4096, strict_quality: int = 0,
                 directive_budget: int = 1200, allow_insufficient: bool = False,
                 max_content_chars: int = 5000) -> dict:
    root = find_project_root()
    try:
        from config_store import effective_value
        routing_enabled = bool(effective_value(root, "routing.enabled", False))
        context_enabled = bool(effective_value(root, "context.optimization_enabled", True))
    except Exception:
        routing_enabled, context_enabled = False, True
    if not routing_enabled:
        return {"error": "provider_routing_disabled", "project": str(root)}
    if not context_enabled:
        return {"error": "context_optimization_required",
                "message": "Provider routing için Context Optimization açık olmalıdır."}

    requested_model = str(model or "auto")
    build_model = requested_model if requested_model != "auto" else "claude-haiku"
    if requested_model != "auto":
        readiness = execution_readiness(root, requested_model)
        if readiness:
            return readiness
    provider_session_id = f"oneshot-{uuid.uuid4().hex}"
    package = build_isolated_context(root, task, build_model, budget, strict_quality,
                                     directive_budget, session_id=provider_session_id)
    if package.get("error"):
        return package
    compact = mcp_server.compact_context_package(package, max_content_chars)
    sufficiency = compact.get("sufficiency", {})
    strict = compact.get("strict_quality", {})
    if compact.get("known_to_model"):
        return {
            "error": "context_isolation_failure",
            "message": "One-shot modeline ait olmayan cross-turn context algılandı; çağrı yapılmadı.",
            "model_id": model,
        }
    strict_failed = bool(strict.get("enabled")) and strict.get("reached") is not True
    if (sufficiency.get("sufficient") is not True or strict_failed) and not allow_insufficient:
        return {
            "error": "context_insufficient",
            "message": "Model çağrısı yapılmadı; eksik context ile kalite riske atılmadı.",
            "sufficiency": sufficiency,
            "strict_quality": strict,
            "model_id": requested_model,
        }

    routing_decision = None
    if requested_model == "auto":
        analysis = package.get("sections", {}).get("routing", {}).get("machine_readable", {})
        usage_event = package.get("sections", {}).get("usage", {})
        routing_decision = router.route_task(
            root, analysis, record=False, task_text=task,
            context_tokens=int(usage_event.get("context_tokens", 0) or 0))
        if routing_decision.get("decision") != "ROUTED":
            return {"error": "auto_routing_blocked", "routing": routing_decision}
        selected_model = routing_decision.get("final_model", "")
    else:
        selected_model = requested_model

    readiness = execution_readiness(root, selected_model)
    if readiness:
        return readiness

    # Diagnostics, timestamps, scopes, budgets and response metadata help the
    # caller but not the inference. Keep them out of the paid token path.
    inference_context = {
        "routing_analysis": compact.get("routing", {}).get("analysis", {}),
        "context_items": compact.get("context_items", []),
        "directives": compact.get("directives", {}),
        "project_memory": compact.get("project_memory", {}),
    }
    context_json = json.dumps(inference_context, ensure_ascii=False,
                              separators=(",", ":"))
    system = (
        "You are a coding assistant receiving a self-contained, token-optimized project "
        "context package. Solve the user's task using only reliable evidence in the package. "
        "Do not claim files were edited or commands were run; this is a single inference with "
        "no tools. When implementation is requested, return an actionable patch or exact code. "
        "If evidence is insufficient, state exactly what must be read next."
    )
    prompt = f"USER TASK:\n{task}\n\nCOMPACT PROJECT CONTEXT:\n{context_json}"
    attempted_models = []
    candidate_models = [selected_model]
    if routing_decision:
        candidate_models.extend(routing_decision.get("candidates_considered", []))
    result = {}
    for candidate in dict.fromkeys(candidate_models):
        readiness = execution_readiness(root, candidate)
        if readiness:
            continue
        attempted_models.append(candidate)
        result = router.execute_with_model(
            root, candidate, prompt, system=system, max_tokens=max_output_tokens,
            session_id=provider_session_id)
        if not result.get("error"):
            selected_model = candidate
            break
    if result.get("error"):
        result["context"] = {"sufficiency": sufficiency,
                             "budget": compact.get("budget", {})}
        result["attempted_models"] = attempted_models
        return result

    usage_event = package.get("sections", {}).get("usage", {})
    repo_tokens = int(usage_event.get("repo_tokens", 0) or 0)
    context_tokens = int(usage_event.get("context_tokens", 0) or 0)
    retrieval_savings = round((1 - context_tokens / repo_tokens) * 100, 2) if repo_tokens else None
    provider_input = int(result.get("usage", {}).get("input_tokens", 0) or 0)
    end_to_end_savings = (
        round((1 - provider_input / repo_tokens) * 100, 2)
        if repo_tokens and provider_input else None
    )
    return {
        "ok": True,
        "mode": "provider_routing" if routing_decision else "explicit_provider",
        "model_id": selected_model,
        "routing": routing_decision,
        "attempted_models": attempted_models,
        "response": result.get("text", ""),
        "provider_usage": result.get("usage", {}),
        "estimated_cost_usd": result.get("estimated_cost_usd", 0.0),
        "duration_ms": result.get("duration_ms", 0),
        "api_style": result.get("api_style"),
        "privacy_notice": result.get("privacy_notice"),
        "context_metrics": {
            "repo_tokens": repo_tokens,
            "sent_context_tokens": context_tokens,
            "retrieval_savings_pct": retrieval_savings,
            "provider_input_tokens": provider_input,
            "conservative_end_to_end_savings_vs_repo_pct": end_to_end_savings,
            "quality_score": usage_event.get("quality"),
            "sufficiency": sufficiency,
            "isolated_model_session": True,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Context Agent one-shot model execution")
    parser.add_argument("task")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--budget", type=int)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--strict-quality", type=int, default=0)
    parser.add_argument("--directive-budget", type=int, default=1200)
    parser.add_argument("--max-content-chars", type=int, default=5000)
    parser.add_argument("--allow-insufficient", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_one_shot(
        args.task, args.model, args.budget, args.max_output_tokens,
        args.strict_quality, args.directive_budget, args.allow_insufficient,
        args.max_content_chars), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
