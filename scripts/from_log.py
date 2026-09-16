#!/usr/bin/env python3
"""
from_log.py - Error log aware routing.

Usage:
  python from_log.py --file error.log
  echo "<traceback>" | python from_log.py
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FILE_LINE_PATTERNS = [
    re.compile(r'File "([^"]+)", line (\d+)'),
    re.compile(r'([A-Za-z]:\\[^\s:]+?\.[A-Za-z0-9]+):(\d+)'),
    re.compile(r'((?:\.{0,2}/)?[^\s:]+?\.[A-Za-z0-9]+):(\d+)'),
]

ERROR_WORDS = re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Failure|Timeout|Warning))")

def find_root():
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / ".context").exists():
            return parent
    return Path.cwd()

def run_script(name, *args):
    root = find_root()
    script = root / ".context" / "scripts" / f"{name}.py"
    result = subprocess.run(
        [sys.executable, str(script), *[str(a) for a in args if a is not None]],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode != 0:
        return {"error": "script_failed", "stderr": result.stderr[-1000:], "stdout": result.stdout[-1000:]}
    if not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"raw": result.stdout}

def normalize_file(path):
    root = find_root()
    p = Path(path)
    if p.is_absolute():
        try:
            return str(p.relative_to(root))
        except ValueError:
            return str(p)
    return str(p)

def parse_log(text):
    frames = []
    seen = set()
    for pattern in FILE_LINE_PATTERNS:
        for match in pattern.finditer(text):
            file_path = normalize_file(match.group(1))
            line = int(match.group(2))
            key = (file_path, line)
            if key not in seen:
                seen.add(key)
                frames.append({"file": file_path, "line": line})

    errors = list(dict.fromkeys(ERROR_WORDS.findall(text)))[:10]
    last_lines = [line.strip() for line in text.splitlines() if line.strip()][-8:]
    return {"frames": frames[:12], "errors": errors, "last_lines": last_lines}

def context_from_log(text, radius=40, budget=4000):
    parsed = parse_log(text)
    context_items = []
    for frame in parsed["frames"][:6]:
        start = max(1, frame["line"] - radius)
        end = frame["line"] + radius
        item = run_script("get_range", frame["file"], start, end)
        if "content" in item:
            item["source"] = "stack_trace"
            item["focus_line"] = frame["line"]
            context_items.append(item)

    task = "Debug " + " ".join(parsed["errors"] or parsed["last_lines"][-2:])
    route = run_script("route", task, "--budget", budget)
    return {
        "task": task,
        "parsed": parsed,
        "context_items": context_items,
        "route": route,
        "log_tail": parsed["last_lines"],
        "next_commands": [
            f"python .context/scripts/get_related.py {item['file']}"
            for item in context_items[:4]
        ],
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file")
    parser.add_argument("--radius", type=int, default=40)
    parser.add_argument("--budget", type=int, default=4000)
    args = parser.parse_args()

    text = Path(args.file).read_text(encoding="utf-8-sig", errors="replace") if args.file else sys.stdin.read()
    print(json.dumps(context_from_log(text, args.radius, args.budget), indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
