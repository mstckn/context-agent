#!/usr/bin/env python3
"""
router.py — Deterministic model routing engine (autoroute spec §4, §6-9,
§18-27, §46). OPTIONAL layer: Context Intelligence never depends on this file.

Design rules:

* Dependency direction: router -> Context Intelligence. Never the reverse.
* The router CONSUMES Context Intelligence's machine-readable task analysis
  (task_type, complexity, risk, reasoning_requirement, context_confidence,
  safe_to_implement). It never re-classifies tasks itself.
* Deterministic-first: no ML. Selection = policy matrix + risk floor +
  capability/health filtering + stable sort (tier, cost, latency, id).
* Goal is LOWEST EXPECTED COST TO A CORRECT VERIFIED RESULT, not the
  cheapest model.
* Quality guardrail: if no sufficient model is available -> ROUTING BLOCKED.
  Never pretend a weaker model is enough.
* Cost guardrails: per-task / daily / monthly with WARN / BLOCK /
  ECONOMY_ONLY / REQUIRE_MANUAL_OVERRIDE actions.
* Escalation path ECONOMY -> BALANCED -> STRONG -> FRONTIER for quality
  failures only; never for network/environment/setup errors.
* Failure isolation: this module is standalone; importing/running it can
  never break the Context core.

Storage:
  ~/.context-agent/providers.json        provider config (NO secrets here)
  ~/.context-agent/models.json           model catalog (editable)
  ~/.context-agent/provider_health.json  cached health
  <root>/.context/router_policy.json     project policy + policy_version
  <root>/.context/router.db              routing_history + usage accounting
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from paths import find_project_root
except ImportError:  # pragma: no cover
    from scripts.paths import find_project_root

try:
    import secret_store
except ImportError:  # pragma: no cover
    from scripts import secret_store

TIERS = ("ECONOMY", "BALANCED", "STRONG", "FRONTIER")
TIER_ORDER = {tier: i for i, tier in enumerate(TIERS)}

AGENT_DIR = Path.home() / ".context-agent"
PROVIDERS_PATH = AGENT_DIR / "providers.json"
MODELS_PATH = AGENT_DIR / "models.json"
HEALTH_PATH = AGENT_DIR / "provider_health.json"
GATEWAY_PATH = AGENT_DIR / "gateway.json"
CATALOG_VERSION = 3
HTTP_USER_AGENT = "context-agent/0.1 (+https://opencode.ai)"
OPENCODE_SESSION_HEADER = "X-Opencode-Session"

DEFAULT_PROVIDERS = [
    {"id": "openai", "type": "openai", "display_name": "OpenAI",
     "base_url": "https://api.openai.com/v1", "enabled": False, "needs_key": True},
    {"id": "anthropic", "type": "anthropic", "display_name": "Anthropic",
     "base_url": "https://api.anthropic.com/v1", "enabled": False, "needs_key": True},
    {"id": "dashscope", "type": "dashscope", "display_name": "Alibaba DashScope",
     "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
     "enabled": False, "needs_key": True},
    {"id": "openrouter", "type": "openrouter", "display_name": "OpenRouter",
     "base_url": "https://openrouter.ai/api/v1", "enabled": False, "needs_key": True},
    {"id": "ollama", "type": "ollama", "display_name": "Ollama (local)",
     "base_url": "http://localhost:11434/v1", "enabled": False, "needs_key": False},
    {"id": "openai_compatible", "type": "openai_compatible",
     "display_name": "OpenAI-compatible endpoint", "base_url": "",
     "enabled": False, "needs_key": True},
    {"id": "opencode", "type": "opencode_zen", "display_name": "OpenCode Zen",
     "base_url": "https://opencode.ai/zen/v1", "enabled": False,
     "needs_key": True, "models_url": "https://opencode.ai/zen/v1/models",
     "privacy_note": "Free models may use submitted data for model improvement."},
    {"id": "opencode_go", "type": "opencode_go", "display_name": "OpenCode Go",
     "base_url": "https://opencode.ai/zen/go/v1", "enabled": False,
     "needs_key": True, "secret_id": "opencode",
     "models_url": "https://opencode.ai/zen/go/v1/models",
     "privacy_note": "Retention and training terms vary by selected Go model."},
]

DEFAULT_MODELS = [
    {"model_id": "openai/gpt-4o-mini", "provider_id": "openai", "display_name": "GPT-4o mini",
     "remote_model_name": "gpt-4o-mini", "context_window": 128000, "max_output": 16384,
     "input_cost": 0.15, "output_cost": 0.60, "latency_class": "fast",
     "quality_tier": "BALANCED", "capabilities": ["chat", "tools"], "enabled": True},
    {"model_id": "openai/gpt-4o", "provider_id": "openai", "display_name": "GPT-4o",
     "remote_model_name": "gpt-4o", "context_window": 128000, "max_output": 16384,
     "input_cost": 2.50, "output_cost": 10.00, "latency_class": "fast",
     "quality_tier": "STRONG", "capabilities": ["chat", "tools", "vision"], "enabled": True},
    {"model_id": "openai/gpt-4.1", "provider_id": "openai", "display_name": "GPT-4.1",
     "remote_model_name": "gpt-4.1", "context_window": 1047576, "max_output": 32768,
     "input_cost": 2.00, "output_cost": 8.00, "latency_class": "medium",
     "quality_tier": "FRONTIER", "capabilities": ["chat", "tools"], "enabled": True},
    {"model_id": "anthropic/claude-3-5-haiku", "provider_id": "anthropic",
     "display_name": "Claude 3.5 Haiku", "remote_model_name": "claude-3-5-haiku-latest",
     "context_window": 200000, "max_output": 8192, "input_cost": 0.80, "output_cost": 4.00,
     "latency_class": "fast", "quality_tier": "ECONOMY", "capabilities": ["chat", "tools"],
     "enabled": True},
    {"model_id": "anthropic/claude-sonnet-4", "provider_id": "anthropic",
     "display_name": "Claude Sonnet 4", "remote_model_name": "claude-sonnet-4-0",
     "context_window": 200000, "max_output": 64000, "input_cost": 3.00, "output_cost": 15.00,
     "latency_class": "fast", "quality_tier": "STRONG", "capabilities": ["chat", "tools"],
     "enabled": True},
    {"model_id": "dashscope/qwen-turbo", "provider_id": "dashscope",
     "display_name": "Qwen Turbo", "remote_model_name": "qwen-turbo",
     "context_window": 131072, "max_output": 8192, "input_cost": 0.05, "output_cost": 0.20,
     "latency_class": "fast", "quality_tier": "ECONOMY", "capabilities": ["chat"],
     "enabled": True},
    {"model_id": "dashscope/qwen-plus", "provider_id": "dashscope",
     "display_name": "Qwen Plus", "remote_model_name": "qwen-plus",
     "context_window": 131072, "max_output": 8192, "input_cost": 0.40, "output_cost": 1.20,
     "latency_class": "fast", "quality_tier": "BALANCED", "capabilities": ["chat"],
     "enabled": True},
    {"model_id": "openrouter/auto", "provider_id": "openrouter",
     "display_name": "OpenRouter Auto", "remote_model_name": "openrouter/auto",
     "context_window": 128000, "max_output": 8192, "input_cost": 0.50, "output_cost": 2.00,
     "latency_class": "medium", "quality_tier": "BALANCED", "capabilities": ["chat"],
     "enabled": True},
    {"model_id": "ollama/qwen2.5-coder:7b", "provider_id": "ollama",
     "display_name": "Qwen 2.5 Coder 7B (local)", "remote_model_name": "qwen2.5-coder:7b",
     "context_window": 32768, "max_output": 8192, "input_cost": 0.0, "output_cost": 0.0,
     "latency_class": "local", "quality_tier": "ECONOMY", "capabilities": ["chat"],
     "enabled": True},
    # OpenCode Zen's free catalog.  Protocol is model-specific: Muse uses the
    # OpenAI Responses API; the remaining entries use Chat Completions.
    {"model_id": "opencode/big-pickle", "provider_id": "opencode",
     "display_name": "Big Pickle (free)", "remote_model_name": "big-pickle",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.0, "output_cost": 0.0, "latency_class": "fast",
     "quality_tier": "BALANCED", "capabilities": ["chat"], "enabled": True,
     "free_data_use_notice": True},
    {"model_id": "opencode/mimo-v2.5-free", "provider_id": "opencode",
     "display_name": "MiMo V2.5 Free", "remote_model_name": "mimo-v2.5-free",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.0, "output_cost": 0.0, "latency_class": "fast",
     "quality_tier": "BALANCED", "capabilities": ["chat"], "enabled": True,
     "free_data_use_notice": True},
    {"model_id": "opencode/ling-3.0-flash-fin-free", "provider_id": "opencode",
     "display_name": "Ling 3.0 Flash Free", "remote_model_name": "ling-3.0-flash-fin-free",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.0, "output_cost": 0.0, "latency_class": "fast",
     "quality_tier": "ECONOMY", "capabilities": ["chat"], "enabled": True,
     "free_data_use_notice": True},
    {"model_id": "opencode/nemotron-3-ultra-free", "provider_id": "opencode",
     "display_name": "Nemotron 3 Ultra Free", "remote_model_name": "nemotron-3-ultra-free",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.0, "output_cost": 0.0, "latency_class": "medium",
     "quality_tier": "STRONG", "capabilities": ["chat"], "enabled": True,
     "free_data_use_notice": True},
    {"model_id": "opencode/nemotron-3.5-lightning-free", "provider_id": "opencode",
     "display_name": "Nemotron 3.5 Lightning Free", "remote_model_name": "nemotron-3.5-lightning-free",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.0, "output_cost": 0.0, "latency_class": "fast",
     "quality_tier": "BALANCED", "capabilities": ["chat"], "enabled": True,
     "free_data_use_notice": True},
    {"model_id": "opencode/muse-spark-1.3-contributor-free", "provider_id": "opencode",
     "display_name": "Muse Spark 1.3 Contributor Free",
     "remote_model_name": "muse-spark-1.3-contributor-free", "api_style": "responses",
     "context_window": 128000, "max_output": 8192, "input_cost": 0.0,
     "output_cost": 0.0, "latency_class": "medium", "quality_tier": "STRONG",
     "capabilities": ["chat"], "enabled": True, "free_data_use_notice": True},
    {"model_id": "opencode/muse-spark-1.2-contributor-free", "provider_id": "opencode",
     "display_name": "Muse Spark 1.2 Contributor Free",
     "remote_model_name": "muse-spark-1.2-contributor-free", "api_style": "responses",
     "context_window": 128000, "max_output": 8192, "input_cost": 0.0,
     "output_cost": 0.0, "latency_class": "medium", "quality_tier": "STRONG",
     "capabilities": ["chat"], "enabled": True, "free_data_use_notice": True},

    # OpenCode Go subscription catalog. routing_priority is an explicit
    # quality/cost preference inside a tier; lower is preferred. This avoids
    # accidental alphabetical choices (for example Muse 1.2 before 1.3).
    {"model_id": "opencode-go/mimo-v2.5", "provider_id": "opencode_go",
     "display_name": "MiMo V2.5", "remote_model_name": "mimo-v2.5",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.14, "output_cost": 0.28, "latency_class": "fast",
     "quality_tier": "ECONOMY", "routing_priority": 10,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/longcat-2.0", "provider_id": "opencode_go",
     "display_name": "LongCat 2.0", "remote_model_name": "longcat-2.0",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.30, "output_cost": 1.20, "latency_class": "fast",
     "quality_tier": "ECONOMY", "routing_priority": 20,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/hy3", "provider_id": "opencode_go",
     "display_name": "Hy3", "remote_model_name": "hy3", "api_style": "chat_completions",
     "context_window": 128000, "max_output": 8192, "input_cost": 0.14,
     "output_cost": 0.58, "latency_class": "fast", "quality_tier": "ECONOMY",
     "routing_priority": 25, "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/omen-alpha", "provider_id": "opencode_go",
     "display_name": "Omen Alpha", "remote_model_name": "omen-alpha",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.20, "output_cost": 0.66, "latency_class": "fast",
     "quality_tier": "ECONOMY", "routing_priority": 30,
     "capabilities": ["chat"], "enabled": True},

    {"model_id": "opencode-go/qwen3.8-flash", "provider_id": "opencode_go",
     "display_name": "Qwen3.8 Flash", "remote_model_name": "qwen3.8-flash",
     "api_style": "anthropic", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.15, "output_cost": 0.47, "latency_class": "fast",
     "quality_tier": "BALANCED", "routing_priority": 10,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/glm-5.3-flash", "provider_id": "opencode_go",
     "display_name": "GLM 5.3 Flash", "remote_model_name": "glm-5.3-flash",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.15, "output_cost": 0.50, "latency_class": "fast",
     "quality_tier": "BALANCED", "routing_priority": 15,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/deepseek-v4-flash", "provider_id": "opencode_go",
     "display_name": "DeepSeek V4 Flash", "remote_model_name": "deepseek-v4-flash",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.44, "output_cost": 1.32, "latency_class": "fast",
     "quality_tier": "BALANCED", "routing_priority": 20,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/gpt-5.6-luna", "provider_id": "opencode_go",
     "display_name": "GPT 5.6 Luna", "remote_model_name": "gpt-5.6-luna",
     "api_style": "responses", "context_window": 272000, "max_output": 8192,
     "input_cost": 0.20, "output_cost": 1.20, "latency_class": "fast",
     "quality_tier": "BALANCED", "routing_priority": 25,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/qwen3.7-plus", "provider_id": "opencode_go",
     "display_name": "Qwen3.7 Plus", "remote_model_name": "qwen3.7-plus",
     "api_style": "anthropic", "context_window": 256000, "max_output": 8192,
     "input_cost": 0.40, "output_cost": 1.60, "latency_class": "fast",
     "quality_tier": "BALANCED", "routing_priority": 30,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/minimax-m2.7", "provider_id": "opencode_go",
     "display_name": "MiniMax M2.7", "remote_model_name": "minimax-m2.7",
     "api_style": "anthropic", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.30, "output_cost": 1.20, "latency_class": "fast",
     "quality_tier": "BALANCED", "routing_priority": 40,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/minimax-m2.5", "provider_id": "opencode_go",
     "display_name": "MiniMax M2.5", "remote_model_name": "minimax-m2.5",
     "api_style": "anthropic", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.30, "output_cost": 1.20, "latency_class": "fast",
     "quality_tier": "BALANCED", "routing_priority": 50,
     "capabilities": ["chat"], "enabled": True},

    {"model_id": "opencode-go/muse-spark-1.3-contributor", "provider_id": "opencode_go",
     "display_name": "Muse Spark 1.3 Contributor", "remote_model_name": "muse-spark-1.3-contributor",
     "api_style": "responses", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.10, "output_cost": 0.20, "latency_class": "medium",
     "quality_tier": "STRONG", "routing_priority": 10,
     "capabilities": ["chat"], "enabled": True, "training_data_notice": True},
    {"model_id": "opencode-go/mimo-v2.5-pro", "provider_id": "opencode_go",
     "display_name": "MiMo V2.5 Pro", "remote_model_name": "mimo-v2.5-pro",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.435, "output_cost": 0.87, "latency_class": "fast",
     "quality_tier": "STRONG", "routing_priority": 20,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/minimax-m3", "provider_id": "opencode_go",
     "display_name": "MiniMax M3", "remote_model_name": "minimax-m3",
     "api_style": "anthropic", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.30, "output_cost": 1.20, "latency_class": "fast",
     "quality_tier": "STRONG", "routing_priority": 25,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/kimi-k2.7-code", "provider_id": "opencode_go",
     "display_name": "Kimi K2.7 Code", "remote_model_name": "kimi-k2.7-code",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.95, "output_cost": 4.00, "latency_class": "medium",
     "quality_tier": "STRONG", "routing_priority": 30,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/glm-5.3", "provider_id": "opencode_go",
     "display_name": "GLM 5.3", "remote_model_name": "glm-5.3",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 1.40, "output_cost": 4.40, "latency_class": "medium",
     "quality_tier": "STRONG", "routing_priority": 35,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/qwen3.6-plus", "provider_id": "opencode_go",
     "display_name": "Qwen3.6 Plus", "remote_model_name": "qwen3.6-plus",
     "api_style": "anthropic", "context_window": 256000, "max_output": 8192,
     "input_cost": 0.50, "output_cost": 3.00, "latency_class": "medium",
     "quality_tier": "STRONG", "routing_priority": 40,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/muse-spark-1.2-contributor", "provider_id": "opencode_go",
     "display_name": "Muse Spark 1.2 Contributor", "remote_model_name": "muse-spark-1.2-contributor",
     "api_style": "responses", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.10, "output_cost": 0.20, "latency_class": "medium",
     "quality_tier": "STRONG", "routing_priority": 90,
     "capabilities": ["chat"], "enabled": True, "training_data_notice": True},

    {"model_id": "opencode-go/deepseek-v4-pro", "provider_id": "opencode_go",
     "display_name": "DeepSeek V4 Pro", "remote_model_name": "deepseek-v4-pro",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 1.32, "output_cost": 3.96, "latency_class": "medium",
     "quality_tier": "FRONTIER", "routing_priority": 10,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/qwen3.8-max", "provider_id": "opencode_go",
     "display_name": "Qwen3.8 Max", "remote_model_name": "qwen3.8-max",
     "api_style": "anthropic", "context_window": 256000, "max_output": 8192,
     "input_cost": 2.00, "output_cost": 6.00, "latency_class": "medium",
     "quality_tier": "FRONTIER", "routing_priority": 15,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/grok-4.6", "provider_id": "opencode_go",
     "display_name": "Grok 4.6", "remote_model_name": "grok-4.6",
     "api_style": "responses", "context_window": 200000, "max_output": 8192,
     "input_cost": 2.00, "output_cost": 6.00, "latency_class": "medium",
     "quality_tier": "FRONTIER", "routing_priority": 20,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/kimi-k3", "provider_id": "opencode_go",
     "display_name": "Kimi K3", "remote_model_name": "kimi-k3",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 3.00, "output_cost": 15.00, "latency_class": "slow",
     "quality_tier": "FRONTIER", "routing_priority": 25,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/glm-5.2", "provider_id": "opencode_go",
     "display_name": "GLM 5.2", "remote_model_name": "glm-5.2",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 1.40, "output_cost": 4.40, "latency_class": "medium",
     "quality_tier": "FRONTIER", "routing_priority": 30,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/qwen3.7-max", "provider_id": "opencode_go",
     "display_name": "Qwen3.7 Max", "remote_model_name": "qwen3.7-max",
     "api_style": "anthropic", "context_window": 256000, "max_output": 8192,
     "input_cost": 2.50, "output_cost": 7.50, "latency_class": "medium",
     "quality_tier": "FRONTIER", "routing_priority": 40,
     "capabilities": ["chat"], "enabled": True},

    {"model_id": "opencode-go/deepseek-v4-flash-vision-exp", "provider_id": "opencode_go",
     "display_name": "DeepSeek V4 Flash Vision Exp", "remote_model_name": "deepseek-v4-flash-vision-exp",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.44, "output_cost": 1.32, "latency_class": "fast",
     "quality_tier": "BALANCED", "routing_priority": 21,
     "capabilities": ["chat", "vision"], "enabled": True},
    {"model_id": "opencode-go/kimi-k2.6", "provider_id": "opencode_go",
     "display_name": "Kimi K2.6", "remote_model_name": "kimi-k2.6",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.95, "output_cost": 4.00, "latency_class": "medium",
     "quality_tier": "STRONG", "routing_priority": 45,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/glm-5.1", "provider_id": "opencode_go",
     "display_name": "GLM 5.1", "remote_model_name": "glm-5.1",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 1.40, "output_cost": 4.40, "latency_class": "medium",
     "quality_tier": "STRONG", "routing_priority": 50,
     "capabilities": ["chat"], "enabled": True},
    {"model_id": "opencode-go/hy4-preview", "provider_id": "opencode_go",
     "display_name": "Hy4 Preview", "remote_model_name": "hy4-preview",
     "api_style": "chat_completions", "context_window": 128000, "max_output": 8192,
     "input_cost": 0.834, "output_cost": 2.501, "latency_class": "medium",
     "quality_tier": "STRONG", "routing_priority": 55,
     "capabilities": ["chat"], "enabled": True},
]

# SMART default policy: task type -> preferred tier + fallback tier.
SMART_MATRIX = {
    "QUICK_EDIT": {"preferred": "ECONOMY", "fallback": "BALANCED"},
    "FIX": {"preferred": "ECONOMY", "fallback": "BALANCED"},
    "TEST_GENERATION": {"preferred": "ECONOMY", "fallback": "BALANCED"},
    "DOCUMENTATION": {"preferred": "ECONOMY", "fallback": "BALANCED"},
    "REFACTOR": {"preferred": "BALANCED", "fallback": "STRONG"},
    "NEW_FEATURE": {"preferred": "BALANCED", "fallback": "STRONG"},
    "DATABASE_CHANGE": {"preferred": "BALANCED", "fallback": "STRONG"},
    "DEBUGGING": {"preferred": "BALANCED", "fallback": "STRONG"},
    "ARCHITECTURE": {"preferred": "STRONG", "fallback": "FRONTIER"},
    "SECURITY": {"preferred": "STRONG", "fallback": "FRONTIER"},
    "UNKNOWN": {"preferred": "BALANCED", "fallback": "STRONG"},
}

RISK_FLOOR = {"LOW": "ECONOMY", "MEDIUM": "BALANCED", "HIGH": "STRONG", "CRITICAL": "FRONTIER"}

ESCALATION_CHAIN = ["ECONOMY", "BALANCED", "STRONG", "FRONTIER"]


# ── storage helpers ────────────────────────────────────────────────────


def _read_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _catalog(path: Path, key: str, defaults: list[dict], id_key: str) -> list[dict]:
    """Load user catalog and add newly shipped built-ins exactly once.

    Saved catalogs carry a version, so deliberate user subsets are preserved.
    Older installations (which had no version) receive additive migration.
    """
    data = _read_json(path, None)
    if data is None:
        return [dict(item) for item in defaults]
    items = list(data.get(key, []))
    if int(data.get("catalog_version", 0) or 0) < CATALOG_VERSION:
        seen = {item.get(id_key) for item in items}
        items.extend(dict(item) for item in defaults if item.get(id_key) not in seen)
        _write_json(path, {key: items, "catalog_version": CATALOG_VERSION,
                           "updated_at": datetime.now().isoformat()})
    return items


def list_providers() -> list[dict]:
    return _catalog(PROVIDERS_PATH, "providers", DEFAULT_PROVIDERS, "id")


def save_providers(providers: list[dict]) -> None:
    _write_json(PROVIDERS_PATH, {"providers": providers, "catalog_version": CATALOG_VERSION,
                                 "updated_at": datetime.now().isoformat()})


def get_provider(provider_id: str) -> dict | None:
    provider_id = str(provider_id or "").lower()
    return next((p for p in list_providers() if p.get("id") == provider_id), None)


def provider_secret_id(provider: dict) -> str:
    """Secret-store key, allowing related provider surfaces to share a key."""
    return str(provider.get("secret_id") or provider.get("id") or "").lower()


def list_models() -> list[dict]:
    return _catalog(MODELS_PATH, "models", DEFAULT_MODELS, "model_id")


def save_models(models: list[dict]) -> None:
    _write_json(MODELS_PATH, {"models": models, "catalog_version": CATALOG_VERSION,
                              "updated_at": datetime.now().isoformat()})


def load_health() -> dict:
    return _read_json(HEALTH_PATH, {})


def save_health(health: dict) -> None:
    _write_json(HEALTH_PATH, health)


# ── policy ─────────────────────────────────────────────────────────────


def policy_path(root: Path) -> Path:
    return Path(root) / ".context" / "router_policy.json"


def default_policy() -> dict:
    return {
        "policy_version": 1,
        "mode": "SMART",                      # SMART | CUSTOM
        "matrix": json.loads(json.dumps(SMART_MATRIX)),
        "risk_floor": dict(RISK_FLOOR),
        "escalation_enabled": True,
        "escalation_chain": list(ESCALATION_CHAIN),
        "guardrails": {
            "cost": {"action": "WARN", "task_limit_usd": 1.0,
                     "daily_limit_usd": 5.0, "monthly_limit_usd": 50.0},
            "quality": {"enforce": True},
        },
        "updated_at": datetime.now().isoformat(),
    }


def load_policy(root: Path) -> dict:
    data = _read_json(policy_path(root), None)
    if not isinstance(data, dict):
        return default_policy()
    base = default_policy()
    base.update(data)
    return base


def save_policy(root: Path, policy: dict) -> dict:
    policy["policy_version"] = int(policy.get("policy_version", 1)) + 1
    policy["updated_at"] = datetime.now().isoformat()
    path = policy_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, policy)
    return policy


# ── router db (history + usage) ────────────────────────────────────────


def _router_db(root: Path) -> sqlite3.Connection:
    db_path = Path(root) / ".context" / "router.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS routing_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT, task TEXT, task_type TEXT, risk TEXT, complexity TEXT,
        required_tier TEXT, initial_model TEXT, escalated_to TEXT, final_model TEXT,
        provider_id TEXT, decision TEXT, reasons TEXT, policy_version INTEGER,
        context_tokens INTEGER DEFAULT 0, model_tokens INTEGER DEFAULT 0,
        estimated_cost_usd REAL DEFAULT 0, duration_ms INTEGER DEFAULT 0,
        executed_by TEXT DEFAULT 'none', outcome TEXT DEFAULT 'routed')""")
    return conn


def record_route(root: Path, entry: dict) -> int:
    conn = _router_db(root)
    try:
        cur = conn.execute(
            "INSERT INTO routing_history (created_at, task, task_type, risk, complexity, "
            "required_tier, initial_model, escalated_to, final_model, provider_id, decision, "
            "reasons, policy_version, context_tokens, model_tokens, estimated_cost_usd, "
            "duration_ms, executed_by, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry.get("created_at", datetime.now().isoformat()), entry.get("task", ""),
             entry.get("task_type", ""), entry.get("risk", ""), entry.get("complexity", ""),
             entry.get("required_tier", ""), entry.get("initial_model", ""),
             entry.get("escalated_to", ""), entry.get("final_model", ""),
             entry.get("provider_id", ""), entry.get("decision", ""),
             json.dumps(entry.get("reasons", []), ensure_ascii=False),
             int(entry.get("policy_version", 1)), int(entry.get("context_tokens", 0) or 0),
             int(entry.get("model_tokens", 0) or 0),
             float(entry.get("estimated_cost_usd", 0.0) or 0.0),
             int(entry.get("duration_ms", 0) or 0), entry.get("executed_by", "none"),
             entry.get("outcome", "routed")))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def history(root: Path, limit: int = 50) -> list[dict]:
    conn = _router_db(root)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM routing_history ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["reasons"] = json.loads(item.get("reasons") or "[]")
            except Exception:
                item["reasons"] = []
            out.append(item)
        return out
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def explain_route(root: Path, route_id: int) -> dict | None:
    rows = [r for r in history(root, limit=500) if r.get("id") == int(route_id)]
    return rows[0] if rows else None


def usage_summary(root: Path) -> dict:
    conn = _router_db(root)
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        month = today[:7]
        def total(where, params):
            row = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(estimated_cost_usd),0) "
                f"FROM routing_history WHERE {where}", params).fetchone()
            return {"routes": row[0], "estimated_cost_usd": round(row[1], 6)}
        return {
            "task_last": total("id=(SELECT MAX(id) FROM routing_history)", ()),
            "today": total("date(created_at)=?", (today,)),
            "month": total("strftime('%Y-%m', created_at)=?", (month,)),
            "by_model": [
                {"final_model": r[0], "routes": r[1],
                 "estimated_cost_usd": round(r[2], 6)}
                for r in conn.execute(
                    "SELECT final_model, COUNT(*), COALESCE(SUM(estimated_cost_usd),0) "
                    "FROM routing_history WHERE final_model != '' "
                    "GROUP BY final_model ORDER BY COUNT(*) DESC LIMIT 20")
            ],
        }
    except sqlite3.OperationalError:
        return {"task_last": {"routes": 0, "estimated_cost_usd": 0},
                "today": {"routes": 0, "estimated_cost_usd": 0},
                "month": {"routes": 0, "estimated_cost_usd": 0}, "by_model": []}
    finally:
        conn.close()


# ── provider health (§11) ──────────────────────────────────────────────


def _opencode_session_id(root: Path | None = None, session_id: str = "",
                         purpose: str = "conversation") -> str:
    """Return an opaque, project-isolated id stable for one conversation."""
    source = str(session_id or "").strip()
    if not source:
        for name in (
            "CONTEXT_AGENT_CONVERSATION_ID",
            "CONTEXT_AGENT_MODEL_SESSION_ID",
            "CONTEXT_AGENT_SESSION",
            "CONTEXT_AGENT_MCP_CONNECTION",
        ):
            source = os.environ.get(name, "").strip()
            if source:
                break

    project = "no-project"
    if root is not None:
        resolved = Path(root).resolve()
        try:
            from paths import ensure_project_id
        except ImportError:  # pragma: no cover
            from scripts.paths import ensure_project_id
        try:
            project = ensure_project_id(resolved)
        except Exception:
            project = str(resolved)

    if not source:
        # Health/catalog probes have no chat turn but every OpenCode request
        # still requires a stable, non-empty session header.
        source = f"context-agent-{purpose}"
    digest = hashlib.sha256(
        f"context-agent\0{project}\0{purpose}\0{source}".encode("utf-8")
    ).hexdigest()[:32]
    return f"ca-{digest}"


def _opencode_headers(provider: dict, root: Path | None = None,
                      session_id: str = "", purpose: str = "conversation") -> dict:
    if provider.get("type") not in ("opencode_zen", "opencode_go") and not str(
            provider.get("id") or "").startswith("opencode"):
        return {}
    return {OPENCODE_SESSION_HEADER: _opencode_session_id(
        root, session_id=session_id, purpose=purpose)}


def _http_get(url: str, headers: dict, timeout: float = 4.0):
    request_headers = {"User-Agent": HTTP_USER_AGENT, "Accept": "application/json",
                       **headers}
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    return urllib.request.urlopen(request, timeout=timeout)


def check_provider_health(provider: dict, force: bool = False, max_age_s: int = 300) -> dict:
    provider_id = provider.get("id", "")
    health = load_health()
    cached = health.get(provider_id, {})
    if cached and not force:
        try:
            age = (datetime.now() - datetime.fromisoformat(cached.get("checked_at"))).total_seconds()
            if age < max_age_s:
                return cached
        except Exception:
            pass

    result = {"provider_id": provider_id, "status": "UNKNOWN",
              "checked_at": datetime.now().isoformat(),
              "last_error_category": None,
              "last_success_at": cached.get("last_success_at")}
    if not provider.get("enabled"):
        result["status"] = "NOT_ENABLED"
    elif provider.get("needs_key") and not secret_store.has_secret(provider_secret_id(provider)):
        # Never probe without a key (§10): no key -> honest UNKNOWN.
        result["status"] = "UNKNOWN"
        result["last_error_category"] = "no_api_key"
    else:
        base_url = str(provider.get("base_url") or "").rstrip("/")
        if not base_url:
            result["status"] = "UNAVAILABLE"
            result["last_error_category"] = "no_base_url"
        else:
            headers = {}
            key = (secret_store.get_secret(provider_secret_id(provider))
                   if provider.get("needs_key") else None)
            if key:
                if provider.get("type") == "anthropic":
                    headers["x-api-key"] = key
                    headers["anthropic-version"] = "2023-06-01"
                else:
                    headers["Authorization"] = f"Bearer {key}"
            headers.update(_opencode_headers(provider, purpose="provider-health"))
            try:
                with _http_get(f"{base_url}/models", headers) as response:
                    status_code = response.status
                if status_code == 200:
                    result["status"] = "CONNECTED"
                    result["last_success_at"] = result["checked_at"]
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    result["status"] = "AUTHENTICATION_FAILED"
                    result["last_error_category"] = "auth"
                elif exc.code == 429:
                    result["status"] = "RATE_LIMITED"
                    result["last_error_category"] = "rate_limited"
                else:
                    result["status"] = "UNAVAILABLE"
                    result["last_error_category"] = f"http_{exc.code}"
            except Exception:
                result["status"] = "UNAVAILABLE"
                result["last_error_category"] = "network"

    health[provider_id] = result
    save_health(health)
    return result


# ── routing decision (§6-8, §18-24) ───────────────────────────────────


def _norm(value, allowed, fallback):
    text = str(value or "").strip().upper()
    return text if text in allowed else fallback


def required_tier_for(analysis: dict, policy: dict) -> tuple[str, list[str]]:
    task_type = _norm(analysis.get("task_type"), set(policy["matrix"].keys()), "UNKNOWN")
    risk = _norm(analysis.get("risk"), set(policy["risk_floor"].keys()), "LOW")
    reasons = []
    entry = policy["matrix"].get(task_type, policy["matrix"]["UNKNOWN"])
    preferred = entry["preferred"]
    floor = policy["risk_floor"][risk]
    required = preferred if TIER_ORDER[preferred] >= TIER_ORDER[floor] else floor
    reasons.append(f"task_type={task_type} -> preferred {preferred}")
    if TIER_ORDER[floor] > TIER_ORDER[preferred]:
        reasons.append(f"risk={risk} raises floor to {floor}")
    reasoning = str(analysis.get("reasoning_requirement", "")).lower()
    if reasoning in ("high", "true", "1") and TIER_ORDER[required] < TIER_ORDER["STRONG"]:
        required = "STRONG"
        reasons.append("reasoning_requirement=high raises tier to STRONG")
    complexity = str(analysis.get("complexity", "")).strip().upper()
    complexity_floor = None
    if complexity in ("CRITICAL", "EXTREME", "VERY_HIGH"):
        complexity_floor = "FRONTIER"
    elif complexity in ("HIGH", "COMPLEX"):
        complexity_floor = "STRONG"
    if complexity_floor and TIER_ORDER[required] < TIER_ORDER[complexity_floor]:
        required = complexity_floor
        reasons.append(f"complexity={complexity} raises tier to {complexity_floor}")
    return required, reasons


def model_enabled_for_project(root: Path | None, model: dict) -> bool:
    """Compatibility helper: model participation is intentionally global."""
    return bool(model.get("enabled", True))


def configured_models() -> list[dict]:
    return [{**model,
             "catalog_enabled": bool(model.get("enabled", True)),
             "global_enabled": bool(model.get("enabled", True)),
             "selection_scope": "GLOBAL"}
            for model in list_models()]


def _usable_models(policy: dict, root: Path | None = None) -> list[dict]:
    providers = {p["id"]: p for p in list_providers()}
    health = load_health()
    out = []
    for model in list_models():
        if not model_enabled_for_project(root, model):
            continue
        provider = providers.get(model.get("provider_id"))
        if not provider or not provider.get("enabled"):
            continue
        provider_id = provider["id"]
        provider_health = health.get(provider_id, {}).get("status", "UNKNOWN")
        if provider_health == "AUTHENTICATION_FAILED":
            continue
        if provider.get("needs_key") and not secret_store.has_secret(provider_secret_id(provider)):
            continue
        if model.get("quality_tier") not in TIERS:
            continue
        out.append({**model, "provider": provider, "provider_health": provider_health})
    return out


def _required_capabilities(analysis: dict) -> set[str]:
    required = {str(v).strip().lower() for v in
                (analysis.get("required_capabilities") or []) if str(v).strip()}
    boolean_signals = {
        "requires_tools": "tools",
        "requires_vision": "vision",
        "requires_streaming": "streaming",
        "requires_structured_output": "structured_output",
        "requires_long_context": "long_context",
    }
    for key, capability in boolean_signals.items():
        if analysis.get(key):
            required.add(capability)
    return required


def estimate_cost_usd(model: dict, input_tokens: int, output_tokens: int) -> float:
    return round(
        (int(input_tokens) * float(model.get("input_cost", 0)) +
         int(output_tokens) * float(model.get("output_cost", 0))) / 1_000_000, 6)


def route_task(root: Path, analysis: dict, record: bool = True,
               task_text: str = "", context_tokens: int = 0) -> dict:
    """Deterministic routing decision. Consumes Context Intelligence analysis;
    never re-classifies the task."""
    root = Path(root or find_project_root())
    policy = load_policy(root)
    required, reasons = required_tier_for(analysis, policy)
    base_usable = _usable_models(policy, root)
    required_capabilities = _required_capabilities(analysis)
    usable = []
    for model in base_usable:
        capabilities = {str(v).lower() for v in (model.get("capabilities") or [])}
        if not required_capabilities.issubset(capabilities):
            continue
        context_window = int(model.get("context_window", 0) or 0)
        max_output = int(model.get("max_output", 0) or 0)
        if context_window and int(context_tokens or 0) + max_output > context_window:
            continue
        usable.append(model)

    decision: dict = {
        "task": task_text or analysis.get("task", ""),
        "task_type": _norm(analysis.get("task_type"), set(policy["matrix"].keys()), "UNKNOWN"),
        "risk": _norm(analysis.get("risk"), set(policy["risk_floor"].keys()), "LOW"),
        "complexity": str(analysis.get("complexity", "")),
        "required_tier": required,
        "policy_version": policy.get("policy_version", 1),
        "policy_mode": policy.get("mode", "SMART"),
        "context_tokens": int(context_tokens or 0),
        "required_capabilities": sorted(required_capabilities),
    }

    sufficient = [m for m in usable if TIER_ORDER[m["quality_tier"]] >= TIER_ORDER[required]]

    # quality guardrail (§22): never pretend a weaker model is enough
    if not sufficient:
        if policy["guardrails"]["quality"].get("enforce", True):
            blocked_reason = "quality_guardrail"
            detail = f"no enabled+configured model at tier >= {required}"
            if required_capabilities and base_usable and not usable:
                blocked_reason = "capability_guardrail"
                detail = ("no enabled+configured model provides required capabilities: "
                          + ", ".join(sorted(required_capabilities)))
            elif base_usable and not usable and context_tokens:
                blocked_reason = "context_window_guardrail"
                detail = "no enabled+configured model can fit the requested context"
            decision.update({
                "decision": "ROUTING_BLOCKED",
                "blocked": True,
                "reason": blocked_reason,
                "final_model": None, "provider_id": None,
                "reasons": reasons + [
                    f"{detail}; "
                    "routing blocked instead of routing to an insufficient model"],
            })
            if record:
                record_route(root, decision)
            return decision

    # cost guardrails (§21)
    usage = usage_summary(root)
    cost_cfg = policy["guardrails"]["cost"]
    task_limit = float(cost_cfg.get("task_limit_usd", 1.0))
    daily_limit = float(cost_cfg.get("daily_limit_usd", 5.0))
    monthly_limit = float(cost_cfg.get("monthly_limit_usd", 50.0))
    action = str(cost_cfg.get("action", "WARN")).upper()

    # Deterministic candidate order: lowest sufficient tier, explicit routing
    # preference (benchmarked/curated quality-cost utility), then cost,
    # latency and stable id. Missing priorities sort last, preserving backward
    # compatibility without letting alphabetical order decide model quality.
    latency_rank = {"local": 0, "fast": 1, "medium": 2, "slow": 3}
    sufficient.sort(key=lambda m: (
        TIER_ORDER[m["quality_tier"]], int(m.get("routing_priority", 1000)),
        float(m.get("input_cost", 0)),
        latency_rank.get(m.get("latency_class"), 9), m["model_id"]))
    candidates = sufficient

    monthly_cost = usage["month"]["estimated_cost_usd"]
    daily_cost = usage["today"]["estimated_cost_usd"]
    projected = max(estimate_cost_usd(candidates[0], context_tokens, 4000), 0.0001)
    over = []
    if projected > task_limit:
        over.append(f"projected task cost {projected:.4f} > task limit {task_limit}")
    if daily_cost + projected > daily_limit:
        over.append(f"daily spend {daily_cost:.2f}+{projected:.4f} > {daily_limit}")
    if monthly_cost + projected > monthly_limit:
        over.append(f"monthly spend {monthly_cost:.2f}+{projected:.4f} > {monthly_limit}")

    if over:
        if action == "BLOCK":
            decision.update({"decision": "ROUTING_BLOCKED", "blocked": True,
                             "reason": "cost_guardrail", "final_model": None,
                             "provider_id": None,
                             "reasons": reasons + [f"cost guardrail BLOCK: {o}" for o in over]})
            if record:
                record_route(root, decision)
            return decision
        if action == "ECONOMY_ONLY":
            economy_only = [m for m in candidates if m["quality_tier"] == "ECONOMY"]
            if not economy_only:
                decision.update({"decision": "ROUTING_BLOCKED", "blocked": True,
                                 "reason": "cost_guardrail_economy_only",
                                 "final_model": None, "provider_id": None,
                                 "reasons": reasons + ["ECONOMY_ONLY enforced but no ECONOMY model configured"]})
                if record:
                    record_route(root, decision)
                return decision
            candidates = economy_only
            reasons.append("cost guardrail ECONOMY_ONLY: restricted to ECONOMY tier")
        elif action == "REQUIRE_MANUAL_OVERRIDE":
            if not analysis.get("manual_override"):
                decision.update({"decision": "MANUAL_OVERRIDE_REQUIRED", "blocked": True,
                                 "reason": "cost_guardrail_manual_override",
                                 "final_model": None, "provider_id": None,
                                 "reasons": reasons + [f"manual override required: {o}" for o in over]})
                if record:
                    record_route(root, decision)
                return decision
        else:  # WARN
            reasons.extend([f"cost guardrail WARN: {o}" for o in over])

    chosen = candidates[0]
    escalation = []
    if policy.get("escalation_enabled", True):
        start = TIER_ORDER[chosen["quality_tier"]]
        seen_tiers = set()
        for tier in policy.get("escalation_chain", ESCALATION_CHAIN):
            tier = str(tier).upper()
            if tier not in TIER_ORDER or tier in seen_tiers:
                continue
            seen_tiers.add(tier)
            if TIER_ORDER[tier] > start:
                escalation.append(tier)
        escalation.sort(key=lambda tier: TIER_ORDER[tier])

    decision.update({
        "decision": "ROUTED",
        "blocked": False,
        "initial_model": chosen["model_id"],
        "final_model": chosen["model_id"],
        "escalated_to": None,
        "escalation_chain": escalation,
        "provider_id": chosen["provider_id"],
        "model": {k: chosen.get(k) for k in (
            "model_id", "display_name", "remote_model_name", "context_window",
            "max_output", "input_cost", "output_cost", "quality_tier", "latency_class",
            "routing_priority", "capabilities")},
        "provider_health": chosen.get("provider_health"),
        "candidates_considered": [m["model_id"] for m in candidates[:5]],
        "estimated_cost_usd": projected,
        "executed_by": "none",
        "reasons": reasons + [
            f"selected {chosen['model_id']}: lowest sufficient tier "
            f"({chosen['quality_tier']} >= {required}) with best routing priority "
            f"({chosen.get('routing_priority', 1000)})"],
    })
    if record:
        record_route(root, decision)
    return decision


# ── execution modes (§2) ───────────────────────────────────────────────


def gateway_available() -> dict:
    data = _read_json(GATEWAY_PATH, None)
    if not data or not data.get("port"):
        return {"available": False}
    try:
        with _http_get(f"http://127.0.0.1:{data['port']}/healthz", {}, timeout=1.5) as response:
            alive = response.status == 200
            try:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            except Exception:
                payload = {}
    except Exception:
        alive = False
        payload = {}
    return {"available": alive, "port": data.get("port"),
            "primary_routing": bool(payload.get("primary_routing"))}


def router_status(root: Path | None = None) -> dict:
    """Feature detection for the control plane. NOT ENABLED is never an error."""
    root = root or find_project_root()
    try:
        from config_store import effective_value
        mode = str(effective_value(root, "execution.mode", "IDE_MODEL")).upper()
        routing_enabled = bool(effective_value(root, "routing.enabled", False))
        context_enabled = bool(effective_value(root, "context.optimization_enabled", True))
    except Exception:
        mode, routing_enabled, context_enabled = "IDE_MODEL", False, True

    providers = list_providers()
    enabled_providers = [p for p in providers if p.get("enabled")]
    configured_with_key = [
        p for p in enabled_providers
        if not p.get("needs_key") or secret_store.has_secret(provider_secret_id(p))
    ]
    gateway = gateway_available()
    usable_models = len(_usable_models(load_policy(root), root))

    mode_available = {"IDE_MODEL": True}
    mode_available["HYBRID"] = routing_enabled and usable_models > 0
    mode_available["PROVIDER_ROUTING"] = bool(
        routing_enabled and context_enabled and usable_models > 0)
    mode_available["AUTO_ROUTER"] = bool(
        gateway.get("available") and gateway.get("primary_routing")
        and routing_enabled and usable_models > 0)

    if mode == "PROVIDER_ROUTING" and not mode_available["PROVIDER_ROUTING"]:
        effective_mode = "IDE_MODEL"
        if not context_enabled:
            mode_note = "Provider routing requires Context Optimization; falling back to IDE MODEL"
        elif not usable_models:
            mode_note = "Provider routing has no usable project-selected model; falling back to IDE MODEL"
        else:
            mode_note = "Provider routing is disabled; IDE MODEL active"
    elif mode == "AUTO_ROUTER" and not mode_available["AUTO_ROUTER"]:
        effective_mode = "IDE_MODEL"
        mode_note = "AUTO ROUTER NOT AVAILABLE FOR THIS IDE CONFIGURATION (no Model Gateway); falling back to IDE MODEL"
    elif mode == "HYBRID" and not mode_available["HYBRID"]:
        effective_mode = "IDE_MODEL"
        mode_note = "HYBRID not configured (routing disabled or no usable model); IDE MODEL active"
    else:
        effective_mode = mode
        mode_note = ""

    return {
        "execution_mode": mode,
        "effective_mode": effective_mode,
        "mode_note": mode_note,
        "mode_available": mode_available,
        "context_optimization_enabled": context_enabled,
        "provider_routing_enabled": routing_enabled,
        "routing_enabled": routing_enabled,
        "providers_total": len(providers),
        "providers_enabled": len(enabled_providers),
        "providers_configured": len(configured_with_key),
        "usable_models": usable_models,
        "gateway": gateway,
        "router_component": "NOT ENABLED" if not routing_enabled else "ENABLED",
    }


# ── execution (§25 execute_with_model / §MODE 3) ──────────────────────


def _api_style(provider: dict, model: dict) -> str:
    style = str(model.get("api_style") or "").strip().lower()
    if style:
        return style
    return "anthropic" if provider.get("type") == "anthropic" else "chat_completions"


def _request_parts(provider: dict, model: dict, prompt: str, system: str,
                   max_tokens: int, key: str | None, stream: bool = False,
                   root: Path | None = None, session_id: str = ""):
    base_url = str(provider.get("base_url") or "").rstrip("/")
    style = _api_style(provider, model)
    remote = model["remote_model_name"]
    if style == "anthropic":
        body = {"model": remote, "max_tokens": int(max_tokens),
                "messages": [{"role": "user", "content": prompt}], "stream": stream}
        if system:
            body["system"] = system
        headers = {"content-type": "application/json", "User-Agent": HTTP_USER_AGENT,
                   "Accept": "application/json", "x-api-key": key or "",
                   "anthropic-version": "2023-06-01"}
        headers.update(_opencode_headers(provider, root, session_id))
        return style, f"{base_url}/messages", headers, body
    if style == "responses":
        body = {"model": remote, "input": prompt,
                "max_output_tokens": int(max_tokens), "stream": stream}
        if system:
            body["instructions"] = system
        headers = {"content-type": "application/json", "User-Agent": HTTP_USER_AGENT,
                   "Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        headers.update(_opencode_headers(provider, root, session_id))
        return style, f"{base_url}/responses", headers, body

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {"model": remote, "messages": messages,
            "max_tokens": int(max_tokens), "stream": stream}
    headers = {"content-type": "application/json", "User-Agent": HTTP_USER_AGENT,
               "Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    headers.update(_opencode_headers(provider, root, session_id))
    return "chat_completions", f"{base_url}/chat/completions", headers, body


def _response_text_usage(payload: dict, style: str):
    usage = payload.get("usage", {}) or {}
    if style == "anthropic":
        text = "".join(block.get("text", "") for block in payload.get("content", []))
        return text, usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    if style == "responses":
        text = payload.get("output_text", "")
        if not text:
            parts = []
            for output in payload.get("output", []) or []:
                for block in output.get("content", []) or []:
                    if block.get("type") in ("output_text", "text"):
                        parts.append(block.get("text", ""))
            text = "".join(parts)
        return text, usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    text = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def execute_with_model(root: Path, model_id: str, prompt: str,
                       system: str = "", max_tokens: int = 2048,
                       session_id: str = "") -> dict:
    models = {m["model_id"]: m for m in list_models()}
    model = models.get(model_id)
    if not model:
        return {"error": "unknown_model", "model_id": model_id}
    if not model_enabled_for_project(root, model):
        return {"error": "model_disabled_globally", "model_id": model_id}
    provider = get_provider(model.get("provider_id"))
    if not provider or not provider.get("enabled"):
        return {"error": "provider_not_enabled", "provider_id": model.get("provider_id")}
    if provider.get("needs_key") and not secret_store.has_secret(provider_secret_id(provider)):
        return {"error": "no_api_key", "provider_id": provider["id"]}

    key = (secret_store.get_secret(provider_secret_id(provider))
           if provider.get("needs_key") else None)
    started = datetime.now()

    try:
        style, url, headers, body = _request_parts(
            provider, model, prompt, system, max_tokens, key, stream=False,
            root=root, session_id=session_id)

        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))

        text, in_tok, out_tok = _response_text_usage(payload, style)
        if not isinstance(text, str) or not text.strip():
            choices = payload.get("choices") or []
            finish_reason = choices[0].get("finish_reason") if choices else None
            return {"error": "no_visible_output", "model_id": model_id,
                    "finish_reason": finish_reason,
                    "usage": {"input_tokens": in_tok, "output_tokens": out_tok}}

        duration_ms = int((datetime.now() - started).total_seconds() * 1000)
        return {
            "ok": True, "model_id": model_id, "executed_by": model_id,
            "text": text, "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
            "estimated_cost_usd": estimate_cost_usd(model, in_tok, out_tok),
            "duration_ms": duration_ms,
            "api_style": style,
            "privacy_notice": (provider.get("privacy_note")
                               if (model.get("free_data_use_notice") or
                                   model.get("training_data_notice")) else None),
        }
    except urllib.error.HTTPError as exc:
        return {"error": "http_error", "status": exc.code, "model_id": model_id}
    except Exception as exc:
        detail = str(exc)
        if key:
            detail = detail.replace(key, "[REDACTED]")
        return {"error": "execution_failed", "detail": detail[:300], "model_id": model_id}


def execute_with_model_streaming(root: Path, model_id: str, prompt: str,
                                 system: str = "", max_tokens: int = 2048,
                                 session_id: str = ""):
    """Generator that yields content-delta strings as the provider streams them.

    Falls back to a single-chunk yield when the provider does not support
    streaming.  Each yielded value is a plain-text fragment (not SSE-formatted);
    the caller is responsible for SSE framing.

    On error yields a single dict {"error": ...} instead of str.
    """
    models = {m["model_id"]: m for m in list_models()}
    model = models.get(model_id)
    if not model:
        yield {"error": "unknown_model", "model_id": model_id}
        return
    if not model_enabled_for_project(root, model):
        yield {"error": "model_disabled_globally", "model_id": model_id}
        return
    provider = get_provider(model.get("provider_id"))
    if not provider or not provider.get("enabled"):
        yield {"error": "provider_not_enabled", "provider_id": model.get("provider_id")}
        return
    if provider.get("needs_key") and not secret_store.has_secret(provider_secret_id(provider)):
        yield {"error": "no_api_key", "provider_id": provider["id"]}
        return

    key = (secret_store.get_secret(provider_secret_id(provider))
           if provider.get("needs_key") else None)
    base_url = str(provider.get("base_url") or "").rstrip("/")
    if not base_url:
        yield {"error": "no_base_url"}
        return

    style = _api_style(provider, model)
    if style == "responses":
        # Responses SSE has a different event vocabulary. Preserve correctness
        # by using the verified parser and yielding one complete chunk.
        result = execute_with_model(root, model_id, prompt, system, max_tokens,
                                    session_id=session_id)
        if result.get("error"):
            yield result
        elif result.get("text"):
            yield result["text"]
        return

    is_anthropic = style == "anthropic"

    try:
        _, url, headers, body = _request_parts(
            provider, model, prompt, system, max_tokens, key, stream=True,
            root=root, session_id=session_id)

        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        response = urllib.request.urlopen(request, timeout=120)

        content_type = str(response.headers.get("Content-Type", "")).lower()
        if "text/event-stream" not in content_type:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
            if is_anthropic:
                text = "".join(block.get("text", "")
                               for block in payload.get("content", []))
                if text:
                    yield text
            else:
                message = (payload.get("choices") or [{}])[0].get("message", {})
                text = message.get("content", "")
                if text:
                    yield text
                if message.get("tool_calls"):
                    yield {"tool_calls": message["tool_calls"]}
            response.close()
            return

        # Read SSE stream line by line.
        accumulated_text = []
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            if is_anthropic:
                # Anthropic SSE: event: content_block_delta\ndata: {...}
                if line.startswith("data: "):
                    data_str = line[6:]
                    try:
                        evt = json.loads(data_str)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    evt_type = evt.get("type", "")
                    if evt_type == "content_block_delta":
                        text = evt.get("delta", {}).get("text", "")
                        if text:
                            accumulated_text.append(text)
                            yield text
                    elif evt_type == "message_stop":
                        break
            else:
                # OpenAI SSE: data: {...}\ndata: [DONE]
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        evt = json.loads(data_str)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    choices = evt.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta", {})
                        text = delta.get("content", "")
                        if text:
                            accumulated_text.append(text)
                            yield text
                        # Forward tool-call deltas
                        tool_calls = delta.get("tool_calls")
                        if tool_calls:
                            yield {"tool_calls": tool_calls}
                        finish = choices[0].get("finish_reason")
                        if finish in ("stop", "tool_calls"):
                            break

        response.close()

    except urllib.error.HTTPError as exc:
        yield {"error": "http_error", "status": exc.code, "model_id": model_id}
    except Exception as exc:
        detail = str(exc)
        if key:
            detail = detail.replace(key, "[REDACTED]")
        yield {"error": "execution_failed", "detail": detail[:300], "model_id": model_id}


def delegate_task(root: Path, analysis: dict, task_text: str = "",
                  context_tokens: int = 0, prompt: str = "",
                  system: str = "") -> dict:
    """HYBRID mode (§MODE 2): route the task, then EXECUTE the delegated
    subtask against the selected provider/model.  The IDE model remains the
    primary agent; the router is the delegated executor.

    Telemetry records executed_by=router_hybrid with the actual model id.
    """
    decision = route_task(root, analysis, record=False, task_text=task_text,
                          context_tokens=context_tokens)

    # If routing was blocked, return the decision without execution.
    if decision.get("decision") != "ROUTED":
        decision["delegation"] = {"mode": "HYBRID", "primary_executor": "ide_model",
                                  "executed": False,
                                  "reason": decision.get("decision", "UNKNOWN")}
        decision["executed_by"] = "ide_model"
        record_route(root, decision)
        return decision

    final_model = decision.get("final_model", "")
    if not final_model:
        decision["delegation"] = {"mode": "HYBRID", "primary_executor": "ide_model",
                                  "executed": False, "reason": "no_model_selected"}
        decision["executed_by"] = "ide_model"
        record_route(root, decision)
        return decision

    # Execute the delegated task against the selected provider/model.
    exec_prompt = prompt or task_text or ""
    if not exec_prompt:
        decision["delegation"] = {"mode": "HYBRID", "primary_executor": "ide_model",
                                  "executed": False, "reason": "no_prompt"}
        decision["executed_by"] = "ide_model"
        record_route(root, decision)
        return decision

    exec_result = execute_with_model(root, final_model, exec_prompt, system=system)

    # Classify failures (GAP 1 contract).
    if exec_result.get("error"):
        err = exec_result["error"]
        if err == "no_api_key":
            failure_class = "NO_API_KEY"
        elif err in ("provider_not_enabled", "unknown_model"):
            failure_class = "PROVIDER_UNAVAILABLE"
        elif err == "http_error":
            code = exec_result.get("status", 0)
            if code in (401, 403):
                failure_class = "NO_API_KEY"
            elif code == 429:
                failure_class = "PROVIDER_UNAVAILABLE"
            elif code >= 500:
                failure_class = "NETWORK"
            else:
                failure_class = "MODEL"
        elif err == "execution_failed":
            detail = str(exec_result.get("detail", "")).lower()
            if "timeout" in detail or "connect" in detail or "network" in detail:
                failure_class = "NETWORK"
            else:
                failure_class = "MODEL"
        else:
            failure_class = "MODEL"

        decision["delegation"] = {
            "mode": "HYBRID", "primary_executor": "ide_model",
            "delegated_executor": final_model, "executed": False,
            "failure_class": failure_class, "error": exec_result,
        }
        decision["executed_by"] = "ide_model"  # execution failed; IDE must handle
        decision["outcome"] = "delegation_failed"
        record_route(root, decision)
        return decision

    # Successful execution — merge result into decision.
    decision["delegation"] = {
        "mode": "HYBRID",
        "primary_executor": "ide_model",
        "delegated_executor": final_model,
        "executed": True,
        "result": exec_result.get("text", ""),
        "usage": exec_result.get("usage", {}),
        "estimated_cost_usd": exec_result.get("estimated_cost_usd", 0.0),
        "duration_ms": exec_result.get("duration_ms", 0),
    }
    decision["executed_by"] = "router_hybrid"
    decision["delegated_model"] = final_model
    decision["outcome"] = "delegated"
    decision["duration_ms"] = exec_result.get("duration_ms", 0)
    decision["model_tokens"] = int(exec_result.get("usage", {}).get("output_tokens", 0))
    record_route(root, decision)
    return decision


# ── CLI ────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Deterministic model router (optional layer)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")

    p = sub.add_parser("providers"); p.add_argument("action", choices=["list", "enable", "disable", "health"])
    p.add_argument("--id", default=""); p.add_argument("--type", default="openai")
    p.add_argument("--display-name", default=""); p.add_argument("--base-url", default="")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("models"); p.add_argument(
        "action", choices=["list", "enable", "disable"])
    p.add_argument("--id", default="")

    p = sub.add_parser("policy"); p.add_argument("action", choices=["show", "mode", "custom-set"])
    p.add_argument("value", nargs="?", default="")

    p = sub.add_parser("route"); p.add_argument("--analysis-json", default="{}")
    p.add_argument("--task", default=""); p.add_argument("--context-tokens", type=int, default=0)
    p.add_argument("--no-record", action="store_true")

    p = sub.add_parser("delegate"); p.add_argument("--analysis-json", default="{}")
    p.add_argument("--task", default=""); p.add_argument("--context-tokens", type=int, default=0)
    p.add_argument("--prompt", default=""); p.add_argument("--system", default="")

    p = sub.add_parser("execute"); p.add_argument("--model", required=True)
    p.add_argument("--prompt", required=True); p.add_argument("--system", default="")

    p = sub.add_parser("history"); p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("explain"); p.add_argument("--id", type=int, required=True)
    sub.add_parser("usage")

    args = parser.parse_args()
    root = find_project_root()

    if args.cmd == "status":
        print(json.dumps(router_status(root), indent=2))
    elif args.cmd == "providers":
        providers = list_providers()
        if args.action == "list":
            health = load_health()
            masks = {s["provider_id"]: s.get("mask") for s in secret_store.list_secrets()}
            for provider in providers:
                secret_id = provider_secret_id(provider)
                provider["has_secret"] = secret_store.has_secret(secret_id)
                provider["mask"] = masks.get(secret_id)
                cached = health.get(provider["id"], {})
                provider["health"] = cached.get("status", "UNKNOWN")
                provider["health_checked_at"] = cached.get("checked_at")
            print(json.dumps({"providers": providers}, indent=2))
        elif args.action in ("enable", "disable"):
            for provider in providers:
                if provider["id"] == args.id.lower():
                    provider["enabled"] = args.action == "enable"
            save_providers(providers)
            print(json.dumps({"ok": True, "id": args.id.lower(), "enabled": args.action == "enable"}))
        elif args.action == "health":
            targets = [p for p in providers if not args.id or p["id"] == args.id.lower()]
            out = [check_provider_health(p, force=args.force) for p in targets]
            print(json.dumps({"health": out}, indent=2))
    elif args.cmd == "models":
        models = list_models()
        if args.action == "list":
            print(json.dumps({"models": configured_models(),
                              "selection_scope": "GLOBAL"}, indent=2))
        else:
            for model in models:
                if model["model_id"] == args.id:
                    model["enabled"] = args.action == "enable"
            save_models(models)
            print(json.dumps({"ok": True, "model_id": args.id, "enabled": args.action == "enable"}))
    elif args.cmd == "policy":
        policy = load_policy(root)
        if args.action == "show":
            print(json.dumps(policy, indent=2))
        elif args.action == "mode":
            if args.value.upper() not in ("SMART", "CUSTOM"):
                print(json.dumps({"error": "mode must be SMART or CUSTOM"}))
            else:
                policy["mode"] = args.value.upper()
                save_policy(root, policy)
                print(json.dumps({"ok": True, "mode": policy["mode"],
                                  "policy_version": policy["policy_version"]}))
        elif args.action == "custom-set":
            try:
                patch = json.loads(args.value or "{}")
            except json.JSONDecodeError:
                print(json.dumps({"error": "invalid json"}))
                return
            policy["mode"] = "CUSTOM"
            for key in ("matrix", "risk_floor", "escalation_enabled", "escalation_chain", "guardrails"):
                if key in patch:
                    policy[key] = patch[key]
            save_policy(root, policy)
            print(json.dumps({"ok": True, "policy_version": policy["policy_version"]}))
    elif args.cmd == "route":
        analysis = json.loads(args.analysis_json or "{}")
        print(json.dumps(route_task(root, analysis, record=not args.no_record,
                                    task_text=args.task,
                                    context_tokens=args.context_tokens), indent=2, ensure_ascii=False))
    elif args.cmd == "delegate":
        analysis = json.loads(args.analysis_json or "{}")
        print(json.dumps(delegate_task(root, analysis, task_text=args.task,
                                       context_tokens=args.context_tokens,
                                       prompt=args.prompt, system=args.system),
                         indent=2, ensure_ascii=False))
    elif args.cmd == "execute":
        print(json.dumps(execute_with_model(root, args.model, args.prompt, args.system),
                         indent=2, ensure_ascii=False))
    elif args.cmd == "history":
        print(json.dumps({"history": history(root, args.limit)}, indent=2, ensure_ascii=False))
    elif args.cmd == "explain":
        print(json.dumps(explain_route(root, args.id) or {"error": "not_found"}, indent=2, ensure_ascii=False))
    elif args.cmd == "usage":
        print(json.dumps(usage_summary(root), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
