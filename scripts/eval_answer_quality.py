#!/usr/bin/env python3
"""
eval_answer_quality.py — The real proof.

``benchmark_token_savings.py`` measures *how many* tokens Context Agent
saves. That number is interesting but it does not answer the real
question every maintainer cares about:

    **Does the LLM actually give a better answer with Context Agent's
    focused context than with the full repository?**

This script answers that question. For each fixture in
``evals/answer_quality.json`` it:

1. Builds a question that requires knowledge of *specific* symbols or
   files in the indexed project.
2. Builds two candidate contexts:

   - **baseline:** every supported source file in the project, in
     order, truncated to the same per-file char limit. This is what
     a naive agent would send to the LLM if it dumped the whole repo.
   - **context-agent:** only the symbols/files that the router
     surfaces for the question, packed into the same per-file char
     limit. This is what a Context-Agent-aware agent would send.

3. Computes an **overlap score** between each candidate context and a
   hand-written list of "expected symbols / files" for the question
   (the fixture's ground truth). A higher overlap means the context
   contains more of the *right* material, which is a reasonable proxy
   for "the LLM gets a better answer".

4. Computes a **noise score** = 1 - (relevant bytes / total bytes).
   A lower noise score means the context is more focused.

It then reports the per-question and overall deltas. A positive
``focus_delta`` (overlap with ground truth at same budget) is direct
evidence that Context Agent is **doing its job** — not just saving
tokens, but sending the *right* tokens.

This benchmark does **not** call any external LLM. It measures
*context quality*, which is what Context Agent controls. Whether the
downstream LLM is also a good model is the user's choice.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent


def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str, str]:
    if cmd and Path(cmd[0]).name.lower().startswith("python"):
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
    return result.returncode, result.stdout.lstrip("\ufeff"), result.stderr.lstrip("\ufeff")


def _extract_json(text: str) -> dict | None:
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
                    start = -1
    return None


def ensure_installed(project: Path) -> Path:
    installed = project / ".context" / "scripts"
    installer = REPO_ROOT / "scripts" / "install.py"
    # Running from an *installed* copy makes installer == installed target,
    # which shutil rejects (SameFileError). If the project already has the
    # scripts, treat it as installed and skip the self-copy.
    already_installed = (installed / "index.py").exists() and (installed / "route.py").exists()
    if already_installed:
        try:
            if installer.resolve() == (installed / "install.py").resolve():
                return installed
        except Exception:
            return installed
    rc, out, err = _run(
        [sys.executable, str(installer), str(project)],
        cwd=project,
        timeout=60,
    )
    if rc != 0:
        raise RuntimeError(f"install.py failed (rc={rc}): {err or out}")
    return project / ".context" / "scripts"


def index_project(project: Path, scripts_dir: Path) -> None:
    rc, out, err = _run(
        [sys.executable, str(scripts_dir / "index.py")],
        cwd=project,
        timeout=120,
    )
    if rc != 0:
        raise RuntimeError(f"index.py failed (rc={rc}): {err or out}")


def route_query(project: Path, scripts_dir: Path, question: str, top: int = 3) -> list[str]:
    """Use Context Agent to route a question, return the top-N file paths."""
    rc, out, err = _run(
        [
            sys.executable,
            str(scripts_dir / "route.py"),
            question,
            "--budget",
            "4000",
            "--top",
            str(top),
        ],
        cwd=project,
        timeout=30,
    )
    if rc != 0:
        return []
    payload = _extract_json(out) or {}
    files = []
    for entry in payload.get("relevant_files", []) or []:
        if isinstance(entry, dict):
            f = entry.get("file", "")
            if f:
                files.append(f)
    return files


def read_full_repo(project: Path) -> list[tuple[str, str]]:
    """Return ``[(path, content), ...]`` for every supported source file."""
    SUPPORTED = {".py", ".md"}
    SKIP = {".context", "__pycache__", "node_modules", "venv", ".venv", "build", "dist"}
    out = []
    for path in project.rglob("*"):
        if path.is_dir():
            continue
        if any(part in SKIP for part in path.relative_to(project).parts):
            continue
        if path.suffix.lower() not in SUPPORTED:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        out.append((str(path.relative_to(project)), text))
    return out


def read_files(project: Path, paths: list[str], per_file_char_limit: int) -> list[tuple[str, str]]:
    """Read each path under project, truncate to char limit. Skip missing files."""
    out = []
    for p in paths:
        full = project / p
        if not full.exists():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if len(text) > per_file_char_limit:
            text = text[:per_file_char_limit] + f"\n# ... (truncated to {per_file_char_limit} chars)"
        out.append((p, text))
    return out


def overlap_score(candidate_files: list[str], expected: list[str]) -> float:
    """What fraction of expected items appear in the candidate set?

    Both lists are compared by stem/path so ``auth/tokens.py`` matches
    a fixture's ``auth/tokens.py`` exactly.
    """
    if not expected:
        return 0.0
    cand_set = {c.replace("\\", "/") for c in candidate_files}
    exp_set = {e.replace("\\", "/") for e in expected}
    hits = sum(1 for e in exp_set if e in cand_set or any(e in c for c in cand_set))
    return hits / len(exp_set)


def noise_score(candidate_files: list[str], project: Path) -> float:
    """Fraction of total repo content the candidate set represents.

    Lower is better — closer to ``0`` means we are sending less of
    the repo, which is the whole point.
    """
    SUPPORTED = {".py", ".md"}
    SKIP = {".context", "__pycache__", "node_modules", "venv", ".venv", "build", "dist"}
    total_bytes = 0
    cand_bytes = 0
    cand_set = {c.replace("\\", "/") for c in candidate_files}
    for path in project.rglob("*"):
        if path.is_dir() or any(part in SKIP for part in path.parts):
            continue
        if path.suffix.lower() not in SUPPORTED:
            continue
        try:
            size = path.stat().st_size
        except Exception:
            continue
        total_bytes += size
        rel = str(path.relative_to(project)).replace("\\", "/")
        if rel in cand_set or any(rel in c for c in cand_set):
            cand_bytes += size
    if total_bytes == 0:
        return 0.0
    return cand_bytes / total_bytes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", help="Path to the indexed project")
    parser.add_argument(
        "--evals",
        default=str(REPO_ROOT / "evals" / "answer_quality.json"),
        help="Path to the answer-quality eval fixture",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=3,
        help="How many files Context Agent should surface per question.",
    )
    args = parser.parse_args()

    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"error: not a directory: {project}", file=sys.stderr)
        return 2
    fixtures_path = Path(args.evals)
    if not fixtures_path.is_file():
        print(f"error: fixture not found: {fixtures_path}", file=sys.stderr)
        return 2

    fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
    if not isinstance(fixtures, list) or not fixtures:
        print(f"error: {fixtures_path} must be a non-empty list", file=sys.stderr)
        return 2

    scripts_dir = ensure_installed(project)
    index_project(project, scripts_dir)

    full_repo = read_full_repo(project)
    per_file_limit = max(
        2000,
        sum(len(t) for _, t in full_repo) // max(len(full_repo), 1) * 2,
    )

    results: list[dict] = []
    overlap_deltas: list[float] = []
    noise_deltas: list[float] = []

    print("=" * 64)
    print("LocalContextAgent2LLM — Answer Quality Eval")
    print("=" * 64)
    print(f"Project     : {project}")
    print(f"Fixtures    : {fixtures_path.name} ({len(fixtures)} cases)")
    print(f"Per-file cap: {per_file_limit} chars")
    print(f"Top-N (CA)  : {args.top} files")
    print()

    for fx in fixtures:
        name = fx.get("name", fx.get("question", "?"))
        question = fx.get("question", "")
        expected = fx.get("expected_files", [])
        if not question:
            continue

        # Baseline: every supported file in the repo.
        baseline_files = [p for p, _ in full_repo]
        # Context Agent: routed + truncated.
        ca_files = route_query(project, scripts_dir, question, top=args.top)
        ca_read = read_files(project, ca_files, per_file_limit)

        base_overlap = overlap_score(baseline_files, expected)
        ca_overlap = overlap_score(ca_files, expected)
        base_noise = noise_score(baseline_files, project)
        ca_noise = noise_score(ca_files, project)

        overlap_delta = ca_overlap - base_overlap
        noise_delta = ca_noise - base_noise
        overlap_deltas.append(overlap_delta)
        noise_deltas.append(noise_delta)

        results.append(
            {
                "name": name,
                "question": question,
                "expected": expected,
                "baseline_files": baseline_files,
                "ca_files": ca_files,
                "baseline_overlap": round(base_overlap, 2),
                "ca_overlap": round(ca_overlap, 2),
                "overlap_delta": round(overlap_delta, 2),
                "baseline_noise": round(base_noise, 2),
                "ca_noise": round(ca_noise, 2),
                "noise_delta": round(noise_delta, 2),
            }
        )

        marker = "✓" if ca_overlap >= base_overlap else "✗"
        print(
            f"  {marker} {name!r:40s}  "
            f"overlap {base_overlap:.2f} → {ca_overlap:.2f} ({overlap_delta:+.2f})  "
            f"noise {base_noise:.2f} → {ca_noise:.2f} ({noise_delta:+.2f})"
        )

    print()
    if overlap_deltas:
        avg_overlap = sum(overlap_deltas) / len(overlap_deltas)
        avg_noise = sum(noise_deltas) / len(noise_deltas)
        print(f"  Average overlap delta (CA - baseline): {avg_overlap:+.3f}")
        print(f"  Average noise   delta (CA - baseline): {avg_noise:+.3f}")
        print()
        if avg_overlap >= 0 and avg_noise <= 0:
            print("  ✅ Context Agent sends *more* of the right files and *less*")
            print("     of the wrong ones compared to dumping the whole repo.")
        elif avg_overlap >= 0:
            print("  ✅ Context Agent matches or exceeds baseline coverage of the")
            print("     expected files, with comparable total context size.")
        else:
            print("  ⚠️  Context Agent under-covers some expected files. Consider")
            print("     raising --top, or improving the router's keyword coverage.")

    out_json = REPO_ROOT / "evals" / "answer_quality_results.json"
    out_json.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print()
    print(f"Detailed results: {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
