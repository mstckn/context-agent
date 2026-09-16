#!/usr/bin/env python3
"""
watch.py - Lightweight polling watcher for incremental indexing.

Usage:
  python .context/scripts/watch.py
  python .context/scripts/watch.py --once
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from safe_io import atomic_write_json, file_lock
except ImportError:
    atomic_write_json = None
    file_lock = None

SUPPORTED_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".cs",
    ".php", ".rb", ".md", ".json", ".yaml", ".yml", ".toml",
}
SKIP_DIRS = {
    ".context", ".git", "node_modules", "vendor", ".venv", "venv", "env",
    "__pycache__", "dist", "build", "out", ".next", "target",
}


def find_root():
    from paths import find_project_root
    return find_project_root()


def should_skip(path):
    return any(part in SKIP_DIRS for part in path.parts)


def iter_project_files(root):
    for dirpath, dirs, files in os.walk(str(root)):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        
        dir_path = Path(dirpath)
        for f in files:
            full = dir_path / f
            if full.suffix.lower() not in SUPPORTED_EXTS:
                continue
            try:
                yield full.relative_to(root), full
            except ValueError:
                continue


def state_path(root):
    return root / ".context" / "watch_state.json"


def load_state(root):
    path = state_path(root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(root, state):
    if atomic_write_json:
        atomic_write_json(state_path(root), state)
    else:
        state_path(root).write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_index(root, rel_paths):
    if not rel_paths:
        return {}
    script = root / ".context" / "scripts" / "index.py"
    cmd = [sys.executable, str(script), "--file"] + [str(p) for p in rel_paths]
    result = subprocess.run(
        cmd,
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return {
        "files": len(rel_paths),
        "ok": result.returncode == 0,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
    }


def scan_once(root):
    lock_path = root / ".context" / "watch.lock"
    lock = file_lock(lock_path, timeout=1.0) if file_lock else None
    if lock:
        with lock:
            return scan_once_locked(root)
    return scan_once_locked(root)


def scan_once_locked(root):
    previous = load_state(root)
    current = {}
    changed = []

    for rel, full in iter_project_files(root):
        key = str(rel)
        try:
            stat = full.stat()
            stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
            current[key] = stamp
            if previous.get(key) != stamp:
                changed.append(rel)
        except OSError:
            continue

    results = []
    if changed:
        batch = changed[:100]
        results.append(run_index(root, batch))

    save_state(root, current)
    return {
        "root": str(root),
        "changed": len(changed),
        "indexed": sum(item.get("files", 0) for item in results if item.get("ok")),
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    root = find_root()
    if args.once:
        print(json.dumps(scan_once(root), indent=2, ensure_ascii=False))
        return

    while True:
        try:
            scan_once(root)
        except Exception as exc:
            print(f"[context-agent-watch] {exc}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
