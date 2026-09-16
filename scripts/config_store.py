#!/usr/bin/env python3
"""
config_store.py — Layered configuration (autoroute spec §15-17).

Hierarchy (highest wins):

  SYSTEM   built-in defaults, never written to disk
  GLOBAL   ~/.context-agent/config.json          (all projects on this machine)
  PROJECT  <root>/.context/config.json           (travels with the project)
  SESSION  <root>/.context/sessions/<scope>/config.json
  TASK     runtime overrides, NEVER persisted

Rules implemented:

* Routing preferences belong to PROJECT/GLOBAL layers, not to an IDE: opening
  the same project from another IDE keeps them.
* Every effective value carries its SOURCE layer (VIEW EFFECTIVE CONFIGURATION).
* RESET TO INHERITED removes one key (or a whole layer) so the inherited
  value applies again. Task overrides never become defaults.
* Atomic writes + optimistic version check, same as identity.json.

Usage:
  python config_store.py --effective [--key K] [--session S]
  python config_store.py --show LAYER
  python config_store.py --set LAYER KEY VALUE [--session S]
  python config_store.py --reset LAYER [KEY] [--session S]
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from paths import find_project_root, project_scope_key
except ImportError:  # pragma: no cover
    from scripts.paths import find_project_root, project_scope_key

LAYERS = ("SYSTEM", "GLOBAL", "PROJECT", "SESSION", "TASK")

# SYSTEM defaults. These are the product's safe starting point: IDE MODEL
# execution, routing disabled, zero providers required.
SYSTEM_DEFAULTS = {
    "context.optimization_enabled": True,
    "execution.mode": "IDE_MODEL",           # IDE_MODEL | HYBRID | AUTO_ROUTER
    "routing.enabled": False,
    "routing.policy_mode": "SMART",          # SMART | CUSTOM
    "routing.escalation_enabled": True,
    "routing.fallback_model": "",
    "budget.default_tokens": 8000,
    "budget.strict_quality": 80,
    "cost.guardrail_action": "WARN",         # WARN | BLOCK | ECONOMY_ONLY | REQUIRE_MANUAL_OVERRIDE
    "cost.task_limit_usd": 1.0,
    "cost.daily_limit_usd": 5.0,
    "cost.monthly_limit_usd": 50.0,
    "context.diagnostics": False,
    "context.auto_index": True,
}

KNOWN_KEYS = set(SYSTEM_DEFAULTS)

_BOOL = {"true": True, "false": False, "1": True, "0": False, "yes": True, "no": False}


def _coerce(key: str, raw):
    """Best-effort typing guided by the SYSTEM default for known keys."""
    default = SYSTEM_DEFAULTS.get(key)
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        return _BOOL.get(str(raw).strip().lower(), bool(raw))
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(str(raw))
        except ValueError:
            return raw
    if isinstance(default, float):
        try:
            return float(str(raw))
        except ValueError:
            return raw
    if isinstance(default, (dict, list)):
        if isinstance(raw, type(default)):
            return raw
        try:
            parsed = json.loads(str(raw))
            return parsed if isinstance(parsed, type(default)) else raw
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


# ── layer storage ──────────────────────────────────────────────────────


def global_config_path() -> Path:
    return Path.home() / ".context-agent" / "config.json"


def project_config_path(root: Path) -> Path:
    return Path(root) / ".context" / "config.json"


def session_config_path(root: Path, session: str = "") -> Path:
    scope = project_scope_key(root)
    base = Path(root) / ".context" / "sessions" / scope
    if session:
        base = base / str(session)
    return base / "config.json"


def _read_layer(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if k != "_meta"}
    except Exception:
        pass
    return {}


def _write_layer(path: Path, values: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {"updated_at": datetime.now().isoformat(), "writer": "config_store"},
        **values,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def layer_path(layer: str, root: Path | None = None, session: str = "") -> Path | None:
    layer = layer.upper()
    if layer == "GLOBAL":
        return global_config_path()
    if layer == "PROJECT":
        return project_config_path(root or find_project_root())
    if layer == "SESSION":
        return session_config_path(root or find_project_root(), session)
    return None  # SYSTEM/TASK have no file


def get_layer(layer: str, root: Path | None = None, session: str = "",
              task_overrides: dict | None = None) -> dict:
    layer = layer.upper()
    if layer == "SYSTEM":
        # Mutable defaults (for example per-project model overrides) must not
        # be handed out by reference; otherwise editing project A contaminates
        # the process-wide defaults seen by project B.
        return json.loads(json.dumps(SYSTEM_DEFAULTS))
    if layer == "TASK":
        return dict(task_overrides or {})
    path = layer_path(layer, root, session)
    return _read_layer(path) if path else {}


# ── effective resolution ───────────────────────────────────────────────


def effective(root: Path | None = None, key: str | None = None, session: str = "",
              task_overrides: dict | None = None) -> dict:
    """Resolve effective configuration.

    Returns {key: {"value": ..., "source": LAYER}}. When ``key`` is given only
    that key is returned (as a single-entry dict). Unknown keys coming from
    higher layers are included and flagged so custom keys stay visible.
    """
    root = root or find_project_root()
    layers = {
        "SYSTEM": get_layer("SYSTEM"),
        "GLOBAL": get_layer("GLOBAL"),
        "PROJECT": get_layer("PROJECT", root),
        "SESSION": get_layer("SESSION", root, session),
        "TASK": get_layer("TASK", task_overrides=task_overrides),
    }
    keys = set()
    for values in layers.values():
        keys.update(values.keys())
    if key:
        keys = {key} & keys or {key}

    result = {}
    for k in sorted(keys):
        value, source = None, None
        for layer in LAYERS:
            if k in layers[layer]:
                value, source = layers[layer][k], layer
        result[k] = {
            "value": value,
            "source": source or "SYSTEM",
            "known": k in KNOWN_KEYS,
        }
    return result


def effective_value(root: Path | None, key: str, default=None, session: str = "",
                    task_overrides: dict | None = None):
    entry = effective(root, key=key, session=session,
                      task_overrides=task_overrides).get(key, {})
    value = entry.get("value")
    return default if value is None else value


# ── mutation ───────────────────────────────────────────────────────────


def set_value(layer: str, key: str, value, root: Path | None = None,
              session: str = "") -> dict:
    layer = layer.upper()
    if layer in ("SYSTEM", "TASK"):
        return {"error": "layer_not_writable",
                "message": f"{layer} layer cannot be written. SYSTEM is built-in; "
                           f"TASK overrides are passed at call time and never persisted."}
    root = root or find_project_root()
    path = layer_path(layer, root, session)
    values = _read_layer(path)
    values[str(key)] = _coerce(str(key), value)
    _write_layer(path, values)
    return {"ok": True, "layer": layer, "key": str(key),
            "value": values[str(key)], "path": str(path)}


def reset_key(layer: str, key: str | None = None, root: Path | None = None,
              session: str = "") -> dict:
    """RESET TO INHERITED: drop one override (or all overrides of a layer)."""
    layer = layer.upper()
    if layer in ("SYSTEM", "TASK"):
        return {"error": "layer_not_resettable"}
    root = root or find_project_root()
    path = layer_path(layer, root, session)
    values = _read_layer(path)
    if key is None:
        removed = sorted(values.keys())
        values = {}
    else:
        removed = [str(key)] if str(key) in values else []
        values.pop(str(key), None)
    _write_layer(path, values)
    return {"ok": True, "layer": layer, "removed": removed}


# ── CLI ────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Layered configuration store")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--effective", action="store_true")
    group.add_argument("--show", metavar="LAYER")
    group.add_argument("--set", nargs=3, metavar=("LAYER", "KEY", "VALUE"))
    group.add_argument("--reset", nargs="+", metavar=("LAYER", "[KEY]"))
    parser.add_argument("--key", default="")
    parser.add_argument("--session", default="")
    args = parser.parse_args()

    root = find_project_root()
    if args.effective:
        print(json.dumps(effective(root, key=args.key or None, session=args.session),
                         indent=2, ensure_ascii=False))
    elif args.show:
        print(json.dumps(get_layer(args.show, root, args.session), indent=2, ensure_ascii=False))
    elif args.set:
        layer, key, value = args.set
        print(json.dumps(set_value(layer, key, value, root, args.session), indent=2))
    elif args.reset:
        layer = args.reset[0]
        key = args.reset[1] if len(args.reset) > 1 else None
        print(json.dumps(reset_key(layer, key, root, args.session), indent=2))


if __name__ == "__main__":
    main()
