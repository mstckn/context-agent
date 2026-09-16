#!/usr/bin/env python3
"""
capsule.py - Small task memory for Context Agent.

Usage:
  python capsule.py --start "Fix login timeout"
  python capsule.py --add-context src/auth.py --reason "task match" --tokens 420
  python capsule.py --event decision "Use existing AuthService path"
  python capsule.py --context
"""

import argparse
import json
import re
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

def scope_key():
    try:
        from paths import resolve_runtime_scope
    except ImportError:
        from scripts.paths import resolve_runtime_scope
    return resolve_runtime_scope()


def project_meta():
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
        "scope": scope_key(),
        "git": git_info(root),
    }


def capsule_dir():
    base = find_root() / ".context" / "capsules"
    scope = scope_key()
    path = base if scope == "default" else (base / scope)
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_path():
    return capsule_dir() / "current.json"


def slugify(text):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "task"


def now():
    return datetime.now().isoformat()


def task_terms(task=""):
    return {
        word
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]+|\w+", str(task).lower())
        if len(word) >= 3
    }


def load_current():
    path = current_path()
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_current(data):
    data["updated_at"] = now()
    if atomic_write_json:
        atomic_write_json(current_path(), data)
    else:
        current_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def capsule_lock():
    lock_path = find_root() / ".context" / f"capsule-{scope_key()}.lock"
    return file_lock(lock_path) if file_lock else None


def archive_current(existing):
    if not existing:
        return None
    archive = capsule_dir() / f"{existing.get('id', slugify(existing.get('task', 'task')))}.json"
    if atomic_write_json:
        atomic_write_json(archive, existing)
    else:
        archive.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return archive


def start(task):
    lock = capsule_lock()
    if lock:
        with lock:
            return start_locked(task)
    return start_locked(task)


def start_locked(task):
    existing = load_current()
    if existing and existing.get("task") != task:
        archive_current(existing)

    capsule = {
        "id": f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(task)}",
        "task": task,
        "status": "active",
        "started_at": now(),
        "updated_at": now(),
        "context_items": [],
        "files": [],
        "events": [],
        "verification": [],
        "summary": "",
        "meta": project_meta(),
    }
    save_current(capsule)
    return {"status": "started", "capsule": compact(capsule)}


def compact(capsule):
    if not capsule:
        return None
    return {
        "id": capsule.get("id"),
        "task": capsule.get("task"),
        "status": capsule.get("status"),
        "started_at": capsule.get("started_at"),
        "updated_at": capsule.get("updated_at"),
        "context_count": len(capsule.get("context_items", [])),
        "file_count": len(capsule.get("files", [])),
        "event_count": len(capsule.get("events", [])),
        "verification_count": len(capsule.get("verification", [])),
    }


def add_context(file_path, reason="", tokens=0, action="load_file"):
    lock = capsule_lock()
    if lock:
        with lock:
            return add_context_locked(file_path, reason, tokens, action)
    return add_context_locked(file_path, reason, tokens, action)


def add_context_locked(file_path, reason="", tokens=0, action="load_file"):
    capsule = load_current()
    if not capsule:
        capsule = start("auto")["capsule"]
        capsule = load_current()

    item = {
        "file": file_path,
        "reason": reason,
        "tokens": int(tokens or 0),
        "action": action,
        "at": now(),
        "meta": project_meta(),
    }
    capsule["context_items"] = [x for x in capsule.get("context_items", []) if x.get("file") != file_path]
    capsule["context_items"].append(item)

    existing = next((x for x in capsule.get("files", []) if x.get("file") == file_path), None)
    if existing:
        existing["last_action"] = action
        existing["tokens"] = int(tokens or existing.get("tokens", 0))
        existing["at"] = now()
    else:
        capsule.setdefault("files", []).append({
            "file": file_path,
            "last_action": action,
            "tokens": int(tokens or 0),
            "at": now(),
        })

    save_current(capsule)
    return {"status": "context_added", "capsule": compact(capsule), "item": item}


def add_contexts(items):
    """Add a complete context package with one lock, metadata read and write."""
    lock = capsule_lock()
    if lock:
        with lock:
            return add_contexts_locked(items)
    return add_contexts_locked(items)


def add_contexts_locked(items):
    capsule = load_current()
    if not capsule:
        start_locked("auto")
        capsule = load_current()
    meta = project_meta()
    timestamp = now()
    added = []
    for raw in items or []:
        file_path = str(raw.get("file") or "")
        if not file_path:
            continue
        reason = raw.get("reason", "")
        tokens = int(raw.get("tokens", 0) or 0)
        action = raw.get("action", "load_file")
        item = {
            "file": file_path, "reason": reason, "tokens": tokens,
            "action": action, "at": timestamp, "meta": meta,
        }
        capsule["context_items"] = [
            x for x in capsule.get("context_items", [])
            if x.get("file") != file_path
        ]
        capsule["context_items"].append(item)
        existing = next(
            (x for x in capsule.get("files", [])
             if x.get("file") == file_path), None)
        if existing:
            existing.update({
                "last_action": action, "tokens": tokens, "at": timestamp,
            })
        else:
            capsule.setdefault("files", []).append({
                "file": file_path, "last_action": action,
                "tokens": tokens, "at": timestamp,
            })
        added.append(file_path)
    save_current(capsule)
    return {
        "status": "context_added", "added": added,
        "capsule": compact(capsule),
    }


def add_event(kind, text):
    lock = capsule_lock()
    if lock:
        with lock:
            return add_event_locked(kind, text)
    return add_event_locked(kind, text)


def add_event_locked(kind, text):
    capsule = load_current()
    if not capsule:
        start("auto")
        capsule = load_current()
    capsule.setdefault("events", []).append({
        "kind": kind,
        "text": text,
        "at": now(),
        "meta": project_meta(),
    })
    save_current(capsule)
    return {"status": "event_added", "capsule": compact(capsule)}


def complete(summary=""):
    lock = capsule_lock()
    if lock:
        with lock:
            return complete_locked(summary)
    return complete_locked(summary)


def complete_locked(summary=""):
    capsule = load_current()
    if not capsule:
        return {"status": "no_capsule"}
    capsule["status"] = "completed"
    if summary:
        capsule["summary"] = summary
    save_current(capsule)
    archive_current(capsule)
    return {"status": "completed", "capsule": compact(capsule)}


def context_text(task=""):
    capsule = load_current()
    if not capsule:
        recent = recent_capsule_lines(limit=5, task=task)
        if not recent:
            return {"context": "No active capsule.", "token_estimate": 5}
        text = "# Recent Context Memory\n" + "\n".join(recent)
        return {"context": text, "token_estimate": int(len(text.split()) * 1.3), "recent_count": len(recent)}

    lines = [
        f"# Context Capsule: {capsule.get('task', '')}",
        f"Status: {capsule.get('status', 'active')}",
        f"Updated: {capsule.get('updated_at', '')[:19]}",
    ]

    items = capsule.get("context_items", [])[-8:]
    if items:
        lines.append("\n## Context Used")
        for item in items:
            reason = item.get("reason") or item.get("action", "")
            lines.append(f"- {item.get('file')} ({item.get('tokens', 0)} tokens): {reason}")

    events = capsule.get("events", [])[-8:]
    if events:
        lines.append("\n## Events")
        for event in events:
            lines.append(f"- {event.get('kind')}: {event.get('text')}")

    if capsule.get("summary"):
        lines.append("\n## Summary")
        lines.append(capsule["summary"])

    recent = recent_capsule_lines(limit=4, exclude_id=capsule.get("id"), task=task or capsule.get("task", ""))
    if recent:
        lines.append("\n## Recent Memory")
        lines.extend(recent)

    text = "\n".join(lines)
    return {
        "context": text,
        "token_estimate": int(len(text.split()) * 1.3),
        "capsule": compact(capsule),
    }


def recent_capsule_lines(limit=4, exclude_id=None, task=""):
    candidates = []
    current = current_path().resolve()
    current_meta = project_meta()
    current_git = current_meta.get("git", {})
    terms = task_terms(task)
    for path in sorted(capsule_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.resolve() == current:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if exclude_id and data.get("id") == exclude_id:
            continue
        capsule_task = data.get("task", "task")
        status = data.get("status", "active")
        updated = (data.get("updated_at", "") or "")[:19]
        data_git = (data.get("meta") or {}).get("git", {})
        branch_note = ""
        if (
            current_git.get("available")
            and data_git.get("available")
            and data_git.get("branch") != current_git.get("branch")
        ):
            branch_note = f" [branch:{data_git.get('branch')} != current:{current_git.get('branch')}]"
        summary = (data.get("summary") or "").strip()
        events = [
            f"{event.get('kind')}: {event.get('text')}"
            for event in data.get("events", [])[-2:]
            if event.get("text")
        ]
        files = [item.get("file") for item in data.get("files", [])[-3:] if item.get("file")]
        detail = summary or "; ".join(events) or (f"files: {', '.join(files)}" if files else "")
        line = f"- {updated} [{status}] {capsule_task}{branch_note}" + (f": {detail[:220]}" if detail else "")
        haystack = f"{capsule_task} {summary} {' '.join(events)} {' '.join(files)}".lower()
        score = sum(1 for term in terms if term in haystack)
        candidates.append((score, path.stat().st_mtime, line))
    candidates.sort(key=lambda item: (-item[0], -item[1]))
    return [line for _, _, line in candidates[:limit]]


def list_capsules(limit=20):
    items = []
    for path in sorted(capsule_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items.append(compact(data))
        except Exception:
            pass
        if len(items) >= limit:
            break
    return {"capsules": items, "count": len(items)}


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--start", metavar="TASK")
    group.add_argument("--current", action="store_true")
    group.add_argument("--context", action="store_true")
    group.add_argument("--list", action="store_true")
    group.add_argument("--complete", nargs="?", const="", metavar="SUMMARY")
    group.add_argument("--add-context", metavar="FILE")
    group.add_argument("--event", nargs=2, metavar=("KIND", "TEXT"))
    parser.add_argument("--reason", default="")
    parser.add_argument("--tokens", type=int, default=0)
    parser.add_argument("--action", default="load_file")
    parser.add_argument("--task", default="", help="Optional task string for relevance-ranked memory.")
    args = parser.parse_args()

    if args.start:
        output = start(args.start)
    elif args.current:
        data = load_current()
        output = {"capsule": compact(data), "detail": data}
    elif args.context:
        output = context_text(args.task)
    elif args.list:
        output = list_capsules()
    elif args.complete is not None:
        output = complete(args.complete)
    elif args.add_context:
        output = add_context(args.add_context, args.reason, args.tokens, args.action)
    elif args.event:
        output = add_event(args.event[0], args.event[1])
    else:
        output = {"error": "no_action"}

    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
