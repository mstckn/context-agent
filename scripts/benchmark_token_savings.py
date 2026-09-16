#!/usr/bin/env python3
"""
benchmark_token_savings.py — Reproducible token-savings benchmark.

Usage
-----
    python scripts/benchmark_token_savings.py examples/sample_project
    python scripts/benchmark_token_savings.py /path/to/any/python/project --json
    python scripts/benchmark_token_savings.py examples/sample_project --queries 3

What it does
------------
1. Indexes the target project with ``scripts/index.py``.
2. Counts the **full repository** token cost (sum of tokens in every
   source file the indexer sees).
3. Runs a small fixed set of representative coding tasks through
   ``scripts/route.py`` to find the relevant files.
4. Pulls the *symbols actually returned* for each task and sums the
   ``token_estimate`` reported by ``scripts/get_range.py`` /
   ``scripts/get_symbol.py``.
5. Prints (and optionally writes as JSON) a comparison:

    {
      "full_repo_tokens": 1234,
      "retrieved_tokens":  287,
      "saved_tokens":      947,
      "savings_pct":       76.7,
      "queries": [...],
    }

The benchmark is **deterministic** — given the same project tree, it
will produce the same numbers every time, because the indexer, router,
and symbol fetcher are all pure functions of the file content.
"""

from __future__ import annotations

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

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DEFAULT_QUERIES = [
    "verify an authentication token",
    "add rate limiting to the login endpoint",
    "refactor the user model",
    # Baseline gate coverage: migrations/ and large-file retrieval must work.
    "apply the latest database migration",
    "generate the monthly usage report",
]


def run(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr).

    We always invoke the child Python with ``-X utf8`` so Windows doesn't
    fall back to UTF-16/CP-encodings for the captured pipes, and we
    strip a leading UTF-8 BOM if the child wrote one.
    """
    if cmd and Path(cmd[0]).name.lower() in {"python", "python.exe", "python3", "python3.exe"}:
        cmd = [cmd[0], "-X", "utf8", *cmd[1:]]
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    out = result.stdout.lstrip("\ufeff")
    err = result.stderr.lstrip("\ufeff")
    return result.returncode, out, err


def count_full_repo_tokens(project: Path) -> tuple[int, int, list[dict]]:
    """Walk the project, count tokens of every supported file.

    Uses the same heuristic as ``scripts/index.py`` (words * 1.3) so the
    numbers are directly comparable to what the indexer reports.
    """
    SUPPORTED = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".rb", ".php", ".cs", ".md"}
    SKIP = {".git", ".context", "__pycache__", "node_modules", "venv", ".venv", "build", "dist"}

    per_file: list[dict] = []
    total = 0
    for path in project.rglob("*"):
        if path.is_dir():
            continue
        rel_parts = path.relative_to(project).parts
        if any(part in SKIP for part in rel_parts):
            continue
        if path.suffix.lower() not in SUPPORTED:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        tokens = int(len(text.split()) * 1.3)
        total += tokens
        per_file.append({"file": str(path.relative_to(project)), "tokens": tokens})
    return total, len(per_file), per_file


def index_project(project: Path, scripts_dir: Path) -> dict:
    """Run the indexer in the per-project ``.context/scripts`` and return the parsed JSON.

    The indexer's ``--stats`` flag is a *read-only* mode that only reports
    the current DB size — it does not actually walk the project. We
    therefore run a full pass first, then re-run with ``--stats`` to
    collect a clean summary.
    """
    rc, out, err = run(
        [sys.executable, str(scripts_dir / "index.py")],
        cwd=project,
        timeout=120,
    )
    if rc != 0:
        raise RuntimeError(f"index.py (full) failed (rc={rc}): {err or out}")
    rc, out, err = run(
        [sys.executable, str(scripts_dir / "index.py"), "--stats"],
        cwd=project,
        timeout=30,
    )
    if rc != 0:
        raise RuntimeError(f"index.py --stats failed (rc={rc}): {err or out}")
    return _parse_index_stats(out)


def _parse_index_stats(text: str) -> dict:
    """Parse the human-readable output of ``index.py --stats``.

    The current indexer prints a single JSON blob on the last line; if
    that is missing we fall back to a small extracted summary so the
    benchmark still produces a useful number.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[-2000:]}


def _extract_json(out: str) -> dict | None:
    """Find the first top-level JSON object in ``out``.

    The CLI tools sometimes prepend a human-readable header, log lines,
    or a BOM, so we cannot rely on ``out[-1]``. Instead we walk the
    text character by character, tracking brace depth **while respecting
    string literals and backslash escapes**, and parse the slice once
    the depth returns to zero. This avoids the false-positive of the
    naive "every ``{`` is a candidate" approach, which can incorrectly
    match a ``{`` inside a string.
    """
    text = out.strip()
    depth = 0
    start = -1
    in_string = False
    escape = False
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return decoder.decode(text[start : i + 1])
                except json.JSONDecodeError:
                    # Try again from the next ``{``.
                    start = -1
    return None


def query_route(project: Path, task: str, scripts_dir: Path, budget: int = 4000) -> dict:
    """Run route.py in the per-project ``.context/scripts`` and return its JSON."""
    rc, out, err = run(
        [
            sys.executable,
            str(scripts_dir / "route.py"),
            task,
            "--budget",
            str(budget),
        ],
        cwd=project,
        timeout=30,
    )
    if rc != 0 and not out.strip():
        return {"error": err.strip() or f"route.py rc={rc}"}
    parsed = _extract_json(out) if out else None
    if parsed is None:
        return {"error": err.strip() or "no_json_output", "raw_tail": out[-200:]}
    return parsed


def sum_retrieved_tokens(route_result: dict) -> tuple[int, list[dict]]:
    """Sum the ``token_estimate`` reported by the router for each file.

    The router already counts tokens per candidate file using the same
    heuristic as the indexer, so we can reuse those numbers directly.
    This is the *retrieved* baseline: the symbols the LLM would
    actually receive for this query, *not* the whole repo.
    """
    totals: list[dict] = []
    grand = 0
    for entry in route_result.get("relevant_files", []) or []:
        if not isinstance(entry, dict):
            continue
        filepath = entry.get("file", "")
        tokens = int(entry.get("token_estimate", 0) or 0)
        grand += tokens
        totals.append({"file": filepath, "tokens": tokens, "score": entry.get("score")})
    return grand, totals


def ensure_installed(project: Path) -> Path:
    """Install Context Agent into ``project`` and return ``.context/scripts`` dir.

    Idempotent: re-running is cheap. ``install.py`` refreshes stale script
    copies when the source is newer than the installed one.
    """
    rc, out, err = run(
        [sys.executable, str(REPO_ROOT / "scripts" / "install.py"), str(project)],
        cwd=project,
        timeout=60,
    )
    if rc != 0:
        raise RuntimeError(f"install.py failed (rc={rc}): {err or out}")
    return project / ".context" / "scripts"


def benchmark(project: Path, queries: list[str]) -> dict:
    started = time.time()
    full_tokens, file_count, per_file = count_full_repo_tokens(project)
    scripts_dir = ensure_installed(project)
    index_summary = index_project(project, scripts_dir)
    results: list[dict] = []
    retrieved_total = 0
    for q in queries:
        route = query_route(project, q, scripts_dir)
        if not isinstance(route, dict) or "error" in route:
            results.append(
                {
                    "query": q,
                    "error": route.get("error", "route_failed") if isinstance(route, dict) else "route_failed",
                    "retrieved_tokens": 0,
                }
            )
            continue
        tokens, breakdown = sum_retrieved_tokens(route)
        relevant = [b["file"] for b in breakdown if b.get("file")]
        retrieved_total += tokens
        results.append(
            {
                "query": q,
                "files": relevant,
                "retrieved_tokens": tokens,
                "breakdown": breakdown,
                "context_plan_steps": len(route.get("context_plan", {}).get("steps", []) or []),
            }
        )
    successful = [r for r in results if "error" not in r]
    full_tokens_across_queries = full_tokens * len(successful)
    saved = max(full_tokens_across_queries - retrieved_total, 0)
    pct = (round(100.0 * saved / full_tokens_across_queries, 1)
           if full_tokens_across_queries else 0.0)
    # Per-query metrics are the more honest numbers — a real LLM agent
    # makes one routing decision at a time, not three in parallel.
    if successful and full_tokens:
        per_query_savings = [
            round(
                100.0
                * max(full_tokens - r["retrieved_tokens"], 0)
                / full_tokens,
                1,
            )
            for r in successful
            if full_tokens
        ]
        avg_per_query_savings_pct = round(sum(per_query_savings) / len(per_query_savings), 1)
        min_per_query_savings_pct = min(per_query_savings) if per_query_savings else 0.0
        max_per_query_savings_pct = max(per_query_savings) if per_query_savings else 0.0
    else:
        avg_per_query_savings_pct = 0.0
        min_per_query_savings_pct = 0.0
        max_per_query_savings_pct = 0.0
    return {
        "project": str(project),
        "files_indexed": file_count,
        "full_repo_tokens": full_tokens,
        "full_repo_tokens_across_queries": full_tokens_across_queries,
        "retrieved_tokens": retrieved_total,
        "saved_tokens": saved,
        "savings_pct": pct,
        "avg_per_query_savings_pct": avg_per_query_savings_pct,
        "min_per_query_savings_pct": min_per_query_savings_pct,
        "max_per_query_savings_pct": max_per_query_savings_pct,
        "per_file_tokens": sorted(per_file, key=lambda r: -r["tokens"])[:10],
        "index_summary": index_summary,
        "queries": results,
        "elapsed_seconds": round(time.time() - started, 2),
    }


def render_text(report: dict) -> str:
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("LocalContextAgent2LLM — Token Savings Benchmark")
    lines.append("=" * 60)
    lines.append(f"Project         : {report['project']}")
    lines.append(f"Files indexed   : {report['files_indexed']}")
    lines.append("")
    lines.append("--- Full repository cost (no agent) ---")
    lines.append(f"  {report['full_repo_tokens']:>8} tokens")
    lines.append("")
    lines.append("--- Retrieved cost (with agent, per query) ---")
    for r in report["queries"]:
        if "error" in r:
            lines.append(f"  ! {r['query']!r}: {r['error']}")
            continue
        lines.append(f"  - {r['query']!r}")
        lines.append(f"      files: {', '.join(r['files'])}")
        lines.append(f"      tokens: {r['retrieved_tokens']}")
    lines.append(f"  total retrieved across all queries: {report['retrieved_tokens']}")
    lines.append("")
    lines.append("--- Summary ---")
    lines.append(f"  full repo tokens : {report['full_repo_tokens']}")
    lines.append(f"  full repo tokens across queries: {report['full_repo_tokens_across_queries']}")
    lines.append(f"  retrieved tokens : {report['retrieved_tokens']}")
    lines.append(f"  saved tokens     : {report['saved_tokens']}")
    lines.append(f"  savings %        : {report['savings_pct']}%")
    lines.append("")
    lines.append("--- Per-query (typical single-turn agent decision) ---")
    lines.append(f"  avg per-query savings : {report['avg_per_query_savings_pct']}%")
    lines.append(f"  min per-query savings : {report['min_per_query_savings_pct']}%")
    lines.append(f"  max per-query savings : {report['max_per_query_savings_pct']}%")
    lines.append("")
    lines.append(f"  benchmark took   : {report['elapsed_seconds']}s")
    lines.append("=" * 60)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Token-savings benchmark")
    parser.add_argument("project", help="Path to the project to index")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument(
        "--queries",
        type=int,
        default=len(DEFAULT_QUERIES),
        help="How many of the default queries to run (default: all)",
    )
    args = parser.parse_args()

    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"error: not a directory: {project}", file=sys.stderr)
        return 2

    queries = DEFAULT_QUERIES[: max(0, args.queries)]
    if not queries:
        print("error: --queries must be >= 1", file=sys.stderr)
        return 2

    report = benchmark(project, queries)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
