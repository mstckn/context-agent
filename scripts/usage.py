#!/usr/bin/env python3
"""
usage.py - Per-client Context Agent usage accounting.

Usage:
  python usage.py --record --client "<mcp-client-name>" --task "fix auth" --repo-tokens 20000 --context-tokens 900
  python usage.py --summary
"""

import argparse
import json
import os
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from safe_io import atomic_write_json, file_lock
except ImportError:
    atomic_write_json = None
    file_lock = None


def find_root():
    try:
        from paths import find_project_root
    except ImportError:
        from scripts.paths import find_project_root
    return find_project_root()


def usage_path():
    return find_root() / ".context" / "usage.json"


def normalize_client(value):
    value = (value or "").strip()
    if not value:
        value = os.environ.get("CONTEXT_AGENT_CLIENT", "")
    return value.strip() or "direct"


def normalize_scope():
    try:
        from paths import resolve_scope
    except ImportError:
        from scripts.paths import resolve_scope
    return resolve_scope()


def normalize_runtime_scope():
    try:
        from paths import resolve_runtime_scope
    except ImportError:
        from scripts.paths import resolve_runtime_scope
    return resolve_runtime_scope()


def current_meta():
    root = find_root()
    try:
        from paths import ensure_project_id
    except ImportError:
        from scripts.paths import ensure_project_id
    try:
        from git_meta import git_info
    except ImportError:
        from scripts.git_meta import git_info
    return {
        "project_id": ensure_project_id(root),
        "git": git_info(root),
    }


def load_usage():
    path = usage_path()
    if not path.exists():
        return {"events": [], "clients": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"events": [], "clients": {}}


def save_usage(data):
    if atomic_write_json:
        atomic_write_json(usage_path(), data)
    else:
        usage_path().parent.mkdir(parents=True, exist_ok=True)
        usage_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def savings_pct(repo_tokens, context_tokens):
    if not repo_tokens:
        return 0.0
    return round(max(0, 100 - (context_tokens / repo_tokens * 100)), 2)


def normalize_kind(value):
    kind = (value or "").strip().lower()
    if kind not in {"package", "tool"}:
        kind = "package"
    return kind


def record(client, task, repo_tokens, context_tokens, quality=0, item_count=0, kind="package", tool=""):
    client = normalize_client(client)
    kind = normalize_kind(kind)
    event = {
        "at": datetime.now().isoformat(),
        "client": client,
        "scope": normalize_scope(),
        "runtime_scope": normalize_runtime_scope(),
        "model_session_id": (
            os.environ.get("CONTEXT_AGENT_MODEL_SESSION_ID")
            or os.environ.get("CONTEXT_AGENT_SESSION", "")
        ),
        "ide": os.environ.get("CONTEXT_AGENT_IDE", ""),
        "conversation_id": os.environ.get("CONTEXT_AGENT_CONVERSATION_ID", ""),
        "task": task,
        "kind": kind,
        "tool": (tool or "").strip(),
        "repo_tokens": int(repo_tokens or 0),
        "context_tokens": int(context_tokens or 0),
        "tokens_saved": max(0, int(repo_tokens or 0) - int(context_tokens or 0)),
        "savings_pct": savings_pct(int(repo_tokens or 0), int(context_tokens or 0)),
        "quality": int(quality or 0),
        "item_count": int(item_count or 0),
        "meta": current_meta(),
    }

    lock = file_lock(find_root() / ".context" / "usage.lock") if file_lock else None
    if lock:
        with lock:
            data = load_usage()
            append_event(data, event)
            save_usage(data)
    else:
        data = load_usage()
        append_event(data, event)
        save_usage(data)
    return {"status": "recorded", "event": event}


def append_event(data, event):
    events = data.setdefault("events", [])
    events.append(event)
    del events[:-200]
    rebuild_clients(data)


def rebuild_clients(data):
    events = data.setdefault("events", [])
    clients = {}
    for item in events:
        client = item.get("client", "direct")
        scope = (
            item.get("runtime_scope") or item.get("scope") or "default"
        ).strip() or "default"
        kind = normalize_kind(item.get("kind", "package"))
        agg = clients.setdefault(client, {
            "client": client,
            "events": 0,
            "package_events": 0,
            "tool_events": 0,
            "repo_tokens": 0,
            "context_tokens": 0,
            "package_context_tokens": 0,
            "tool_context_tokens": 0,
            "tokens_saved": 0,
            "last_at": "",
            "last_task": "",
            "last_savings_pct": 0,
            "last_quality": 0,
            "last_scope": "default",
            "last_kind": "package",
            "last_tool": "",
        })
        agg["events"] += 1
        if kind == "tool":
            agg["tool_events"] += 1
            agg["tool_context_tokens"] += int(item.get("context_tokens", 0))
        else:
            agg["package_events"] += 1
            agg["package_context_tokens"] += int(item.get("context_tokens", 0))
        agg["repo_tokens"] += int(item.get("repo_tokens", 0))
        agg["context_tokens"] += int(item.get("context_tokens", 0))
        agg["tokens_saved"] += int(item.get("tokens_saved", 0))
        agg["last_at"] = item.get("at", "")
        agg["last_task"] = item.get("task", "")
        agg["last_savings_pct"] = item.get("savings_pct", 0)
        agg["last_quality"] = item.get("quality", 0)
        agg["last_scope"] = scope
        agg["last_kind"] = kind
        agg["last_tool"] = (item.get("tool") or "").strip()
    for agg in clients.values():
        agg["savings_pct"] = savings_pct(agg["repo_tokens"], agg["context_tokens"])
        agg["package_savings_pct"] = savings_pct(agg["repo_tokens"], agg["package_context_tokens"])
        agg["effective_savings_pct"] = agg["savings_pct"]
    data["clients"] = clients


def rebuild_scopes(data):
    events = data.setdefault("events", [])
    scopes = {}
    for item in events:
        scope = (item.get("scope") or "default").strip() or "default"
        client = item.get("client", "direct")
        kind = normalize_kind(item.get("kind", "package"))
        agg = scopes.setdefault(scope, {
            "scope": scope,
            "project_scope": item.get("scope") or "default",
            "events": 0,
            "package_events": 0,
            "tool_events": 0,
            "repo_tokens": 0,
            "context_tokens": 0,
            "package_context_tokens": 0,
            "tool_context_tokens": 0,
            "tokens_saved": 0,
            "last_at": "",
            "last_client": "",
            "clients": set(),
        })
        agg["events"] += 1
        if kind == "tool":
            agg["tool_events"] += 1
            agg["tool_context_tokens"] += int(item.get("context_tokens", 0))
        else:
            agg["package_events"] += 1
            agg["package_context_tokens"] += int(item.get("context_tokens", 0))
        agg["repo_tokens"] += int(item.get("repo_tokens", 0))
        agg["context_tokens"] += int(item.get("context_tokens", 0))
        agg["tokens_saved"] += int(item.get("tokens_saved", 0))
        agg["last_at"] = item.get("at", "")
        agg["last_client"] = client
        agg["clients"].add(client)

    normalized = []
    for agg in scopes.values():
        normalized.append({
            "scope": agg["scope"],
            "project_scope": agg["project_scope"],
            "events": agg["events"],
            "package_events": agg["package_events"],
            "tool_events": agg["tool_events"],
            "repo_tokens": agg["repo_tokens"],
            "context_tokens": agg["context_tokens"],
            "package_context_tokens": agg["package_context_tokens"],
            "tool_context_tokens": agg["tool_context_tokens"],
            "tokens_saved": agg["tokens_saved"],
            "savings_pct": savings_pct(agg["repo_tokens"], agg["context_tokens"]),
            "package_savings_pct": savings_pct(agg["repo_tokens"], agg["package_context_tokens"]),
            "effective_savings_pct": savings_pct(agg["repo_tokens"], agg["context_tokens"]),
            "last_at": agg["last_at"],
            "last_client": agg["last_client"],
            "client_count": len(agg["clients"]),
        })
    return sorted(normalized, key=lambda x: x.get("last_at", ""), reverse=True)


def is_test_event(item):
    task = (item.get("task") or "").strip().lower()
    return (
        task.startswith("test ")
        or task.startswith("verify ")
        or "source attribution" in task
        or "client scoped dedup" in task
    )


def clear_test_events():
    lock = file_lock(find_root() / ".context" / "usage.lock") if file_lock else None

    def run():
        data = load_usage()
        events = data.get("events", [])
        kept = [item for item in events if not is_test_event(item)]
        removed = len(events) - len(kept)
        data["events"] = kept
        rebuild_clients(data)
        save_usage(data)
        return {"status": "cleaned", "removed": removed, "remaining": len(kept)}

    if lock:
        with lock:
            return run()
    return run()


def summary():
    data = load_usage()
    rebuild_clients(data)
    scopes = rebuild_scopes(data)
    clients = sorted(data.get("clients", {}).values(), key=lambda x: x.get("last_at", ""), reverse=True)
    totals = {
        "events": sum(item.get("events", 0) for item in clients),
        "package_events": sum(item.get("package_events", 0) for item in clients),
        "tool_events": sum(item.get("tool_events", 0) for item in clients),
        "repo_tokens": sum(item.get("repo_tokens", 0) for item in clients),
        "context_tokens": sum(item.get("context_tokens", 0) for item in clients),
        "package_context_tokens": sum(item.get("package_context_tokens", 0) for item in clients),
        "tool_context_tokens": sum(item.get("tool_context_tokens", 0) for item in clients),
        "tokens_saved": sum(item.get("tokens_saved", 0) for item in clients),
    }
    totals["savings_pct"] = savings_pct(totals["repo_tokens"], totals["context_tokens"])
    totals["package_savings_pct"] = savings_pct(totals["repo_tokens"], totals["package_context_tokens"])
    totals["effective_savings_pct"] = totals["savings_pct"]
    return {
        "totals": totals,
        "clients": clients,
        "scopes": scopes,
        "recent": data.get("events", [])[-20:][::-1],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--clear-test-events", action="store_true")
    parser.add_argument("--client", default="")
    parser.add_argument("--task", default="")
    parser.add_argument("--repo-tokens", type=int, default=0)
    parser.add_argument("--context-tokens", type=int, default=0)
    parser.add_argument("--quality", type=int, default=0)
    parser.add_argument("--item-count", type=int, default=0)
    parser.add_argument("--kind", default="package")
    parser.add_argument("--tool", default="")
    args = parser.parse_args()

    if args.record:
        output = record(
            args.client,
            args.task,
            args.repo_tokens,
            args.context_tokens,
            args.quality,
            args.item_count,
            args.kind,
            args.tool,
        )
    elif args.clear_test_events:
        output = clear_test_events()
    elif args.summary:
        output = summary()
    else:
        output = {"error": "usage action required"}
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
