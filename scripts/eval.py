#!/usr/bin/env python3
"""
eval.py - Context Agent routing kalitesini fixture'larla ölçer.

Kullanım:
  python .context/scripts/eval.py
  python .context/scripts/eval.py --file evals/basic.json --fail-under 0.70
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def find_root():
    from paths import find_project_root
    return find_project_root()


def script_path(root, name):
    installed = root / ".context" / "scripts" / f"{name}.py"
    if installed.exists():
        return installed
    source = root / "scripts" / f"{name}.py"
    return source


def normalize_path(value):
    return str(value or "").replace("\\", "/").strip().lower()


def default_fixture(root):
    candidates = [
        root / "evals" / "basic.json",
        Path(__file__).resolve().parents[1] / "evals" / "basic.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def ensure_index(root):
    db = root / ".context" / "symbols.db"
    if db.exists():
        return {"indexed": True, "refreshed": False}

    index_script = script_path(root, "index")
    if not index_script.exists():
        return {"indexed": False, "error": f"index script not found: {index_script}"}

    result = subprocess.run(
        [sys.executable, str(index_script)],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if result.returncode != 0:
        return {
            "indexed": False,
            "error": "index_failed",
            "stderr": result.stderr[-2000:],
            "stdout": result.stdout[-2000:],
        }
    return {"indexed": True, "refreshed": True}


def run_route(root, task, budget):
    route_script = script_path(root, "route")
    result = subprocess.run(
        [sys.executable, str(route_script), task, "--budget", str(budget)],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode != 0:
        return {
            "error": "route_failed",
            "stderr": result.stderr[-2000:],
            "stdout": result.stdout[-2000:],
        }
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "route_json_decode_failed", "stdout": result.stdout[-2000:]}


def evaluate_fixture(root, fixture, top, budget):
    task = fixture.get("task", "")
    expected = [normalize_path(path) for path in fixture.get("expected_files", [])]
    route = run_route(root, task, budget)
    if route.get("error"):
        return {
            "task": task,
            "passed": False,
            "expected_files": fixture.get("expected_files", []),
            "got_files": [],
            "error": route,
        }

    relevant = route.get("relevant_files", [])[:top]
    got = [item.get("file") for item in relevant]
    normalized_got = [normalize_path(path) for path in got]
    passed = any(
        any(got_path == exp or got_path.endswith(f"/{exp}") for got_path in normalized_got)
        for exp in expected
    )

    return {
        "task": task,
        "passed": passed,
        "expected_files": fixture.get("expected_files", []),
        "got_files": got,
        "confidence": route.get("confidence", {}),
        "top_score": relevant[0].get("score") if relevant else 0,
        "top_reasons": relevant[0].get("reasons", [])[:3] if relevant else [],
    }


def run_eval(root, fixture_path, top, budget):
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
    if isinstance(fixtures, dict):
        fixtures = fixtures.get("evals", [])

    index_status = ensure_index(root)
    if not index_status.get("indexed"):
        return {
            "ok": False,
            "root": str(root),
            "fixture_file": str(fixture_path),
            "index": index_status,
            "total": 0,
            "passed": 0,
            "hit_rate": 0,
            "results": [],
        }

    results = [evaluate_fixture(root, fixture, top, budget) for fixture in fixtures]
    passed = sum(1 for item in results if item.get("passed"))
    total = len(results)
    hit_rate = round(passed / total, 4) if total else 0

    return {
        "ok": True,
        "root": str(root),
        "fixture_file": str(fixture_path),
        "top": top,
        "budget": budget,
        "index": index_status,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "hit_rate": hit_rate,
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", dest="fixture_file", help="Eval fixture JSON dosyasi.")
    parser.add_argument("--top", type=int, default=5, help="Top N dosya icinde hit say.")
    parser.add_argument("--budget", type=int, default=8000, help="Route token butcesi.")
    parser.add_argument("--fail-under", type=float, default=None, help="Hit rate bunun altindaysa exit 1.")
    args = parser.parse_args()

    root = find_root()
    fixture_path = Path(args.fixture_file).resolve() if args.fixture_file else default_fixture(root)
    if not fixture_path.exists():
        print(json.dumps({
            "ok": False,
            "error": "fixture_not_found",
            "fixture_file": str(fixture_path),
        }, ensure_ascii=False, indent=2))
        sys.exit(2)

    report = run_eval(root, fixture_path, args.top, args.budget)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.fail_under is not None and report.get("hit_rate", 0) < args.fail_under:
        sys.exit(1)


if __name__ == "__main__":
    main()
