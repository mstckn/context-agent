#!/usr/bin/env python3
"""
model_profile.py - Model-aware adaptive token budgeting (spec PHASE 6).

ModelContextProfile {
  model, context_window, safe_input_budget, reserved_output,
  system_overhead, tool_overhead, reasoning_reserve
}

Efektif retrieval bütçesi:

  context_window
  - reserved_output
  - system_overhead
  - tool_overhead
  - reasoning_reserve
  - task_tokens
  = retrieval budget   (safe_input_budget ile de sınırlanır)

Profiller:
  1. .context/config/model_profiles.json (kullanıcı/proje yapılandırması)
  2. Yerleşik aile varsayılanları (prefix eşleşmesi)
  3. Bilinmeyen model → muhafazakâr FALLBACK (asla hata yok)

Çekirdek mantığa her model SABİTLENMEZ; config genişletilebilir.

Kullanım:
  python model_profile.py claude-haiku
  python model_profile.py gpt-4o --fraction 0.35
"""

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MIN_RETRIEVAL_BUDGET = 1500
MAX_RETRIEVAL_BUDGET = 40000

# Yerleşik aile varsayılanları (muhafazakâr; config ile override edilir).
DEFAULT_PROFILES = {
    "claude-haiku": {
        "context_window": 200_000, "safe_input_budget": 60_000,
        "reserved_output": 8_192, "system_overhead": 2_000,
        "tool_overhead": 3_000, "reasoning_reserve": 2_000,
    },
    "claude-sonnet": {
        "context_window": 200_000, "safe_input_budget": 80_000,
        "reserved_output": 8_192, "system_overhead": 2_500,
        "tool_overhead": 4_000, "reasoning_reserve": 4_000,
    },
    "claude-opus": {
        "context_window": 200_000, "safe_input_budget": 100_000,
        "reserved_output": 16_384, "system_overhead": 2_500,
        "tool_overhead": 4_000, "reasoning_reserve": 6_000,
    },
    "gpt-4o": {
        "context_window": 128_000, "safe_input_budget": 60_000,
        "reserved_output": 16_384, "system_overhead": 2_000,
        "tool_overhead": 4_000, "reasoning_reserve": 2_000,
    },
    "gpt-4": {
        "context_window": 128_000, "safe_input_budget": 60_000,
        "reserved_output": 16_384, "system_overhead": 2_000,
        "tool_overhead": 4_000, "reasoning_reserve": 2_000,
    },
    "gemini": {
        "context_window": 1_000_000, "safe_input_budget": 60_000,
        "reserved_output": 8_192, "system_overhead": 2_000,
        "tool_overhead": 3_000, "reasoning_reserve": 2_000,
    },
    "ollama": {
        "context_window": 8_192, "safe_input_budget": 4_000,
        "reserved_output": 2_048, "system_overhead": 500,
        "tool_overhead": 500, "reasoning_reserve": 500,
    },
}

FALLBACK_PROFILE = {
    "context_window": 32_768, "safe_input_budget": 12_000,
    "reserved_output": 4_096, "system_overhead": 2_000,
    "tool_overhead": 3_000, "reasoning_reserve": 2_000,
}

_PROFILE_FIELDS = ("context_window", "safe_input_budget", "reserved_output",
                   "system_overhead", "tool_overhead", "reasoning_reserve")


def _config_path():
    try:
        try:
            from paths import find_context_dir, find_project_root
        except ImportError:
            from scripts.paths import find_context_dir, find_project_root
        found = find_context_dir()
        context_dir = found[0] if isinstance(found, tuple) else found
        if context_dir:
            return Path(context_dir) / "config" / "model_profiles.json"
        # symbols.db henüz oluşturulmamış olabilir; config yine de okunabilmeli.
        return find_project_root() / ".context" / "config" / "model_profiles.json"
    except Exception:
        pass
    return None


def _load_config_profiles():
    path = _config_path()
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _normalize(name):
    return (name or "").strip().lower()


def profile_for(model):
    """Model adı → ModelContextProfile (deterministik, asla hata vermez)."""
    config = _load_config_profiles()
    model = _normalize(model)

    # 1) Config'da tam eşleşme
    if model in config and isinstance(config[model], dict):
        base = dict(FALLBACK_PROFILE)
        base.update({k: v for k, v in config[model].items()
                     if k in _PROFILE_FIELDS and isinstance(v, (int, float))})
        base["model"] = model
        base["source"] = "config"
        return base

    # 2) Yerleşiklerde tam veya prefix eşleşmesi
    matched_key = None
    for key in DEFAULT_PROFILES:
        if model == key or model.startswith(key) or key in model:
            matched_key = key
            break
    if matched_key:
        base = dict(DEFAULT_PROFILES[matched_key])
        # Config aynı AİLE için kısmi override vermişse birleştir.
        override = config.get(matched_key)
        if isinstance(override, dict):
            base.update({k: v for k, v in override.items()
                         if k in _PROFILE_FIELDS and isinstance(v, (int, float))})
        base["model"] = model or matched_key
        base["source"] = "builtin"
        return base

    # 3) Bilinmeyen model → muhafazakâr fallback.
    base = dict(FALLBACK_PROFILE)
    base["model"] = model or "unknown"
    base["source"] = "fallback"
    return base


def retrieval_budget(profile, task_fraction=None, task_tokens=0):
    """Efektif context retrieval bütçesi (token).

    task_fraction: görev tipi bütçe oranı (planner çıktısı); verilirse
    bütçe bu oranla ölçeklenir.
    """
    base = (int(profile["context_window"])
            - int(profile["reserved_output"])
            - int(profile["system_overhead"])
            - int(profile["tool_overhead"])
            - int(profile["reasoning_reserve"])
            - max(0, int(task_tokens or 0)))
    base = min(base, int(profile["safe_input_budget"]))
    if task_fraction:
        frac = max(0.0, min(1.0, float(task_fraction)))
        base = int(base * frac)
    return max(MIN_RETRIEVAL_BUDGET, min(base, MAX_RETRIEVAL_BUDGET))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Model context profile")
    parser.add_argument("model", help="Model adı")
    parser.add_argument("--fraction", type=float, default=None)
    parser.add_argument("--task-tokens", type=int, default=0)
    args = parser.parse_args()

    profile = profile_for(args.model)
    out = dict(profile)
    out["retrieval_budget"] = retrieval_budget(
        profile, task_fraction=args.fraction, task_tokens=args.task_tokens)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
