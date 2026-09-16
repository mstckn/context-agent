#!/usr/bin/env python3
"""
sufficiency.py - Context Sufficiency Engine 2.0 (spec PHASE 10).

Heuristic "yeterli mi" skorunun yerine AÇIK coverage muhakemesi:

  ContextCoverage {
    task_type
    required_evidence[]
    satisfied_evidence[]
    missing_evidence[]
    waived_evidence[]
    coverage
    confidence
    recommended_expansion[]   ← somut, çalıştırılabilir adaylar
  }

Her evidence kategorisi için üç durum:
  satisfied  — paket içeriğinde (veya modelin bildiklerinde) mevcut
  available  — indekste adayı var; OTOMATİK expansion ile kapatılabilir
  waived     — bu ortamda karşılığı yok (ör. git yok, runtime kanıtı dışarıda)

LLM YOK — tamamen deterministik sorgular.

Kullanım:
  python sufficiency.py "BUG_FIX" --files auth/service.py --known auth/models.py
"""

import json
import re
import sqlite3
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

COVERAGE_TARGET = 0.85
MAX_EXPANSION_FILES = 4

_TEST_RE = re.compile(r"(^|/)(tests?/|test_[^/]+\.|[^/]+_test\.|[^/]+\.test\.|[^/]+\.spec\.)",
                      re.IGNORECASE)
_RUNTIME_EVIDENCE_RE = re.compile(
    r"traceback|stack ?trace|error:|exception|panic:|at \w+ \(", re.IGNORECASE)


# ── yardımcı sorgular ──────────────────────────────────────────────────

def _index_files(conn, like_patterns, limit=6):
    """files tablosunda LIKE ile aday dosya bul."""
    if conn is None:
        return []
    out = []
    for pat in like_patterns:
        try:
            rows = conn.execute(
                "SELECT path, token_estimate FROM files WHERE path LIKE ? LIMIT ?",
                (pat, limit)).fetchall()
        except sqlite3.Error:
            rows = []
        for path, est in rows:
            if path not in [o["file"] for o in out]:
                out.append({"file": path, "est_tokens": int(est or 120)})
    return out


def _has_table(conn, table):
    if conn is None:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _edge_degree(conn, paths):
    """Verilen dosyaların graftaki (self olmayan) kenar sayısı."""
    if not paths or not _has_table(conn, "edges"):
        return 0
    total = 0
    for p in paths[:12]:
        try:
            total += conn.execute(
                "SELECT COUNT(*) FROM edges WHERE (src_file=? AND dst_file IS NOT "
                "NULL AND dst_file != ?) OR (dst_file=? AND src_file != ?)",
                (p, p, p, p)).fetchone()[0]
        except sqlite3.Error:
            pass
    return total


def _path_hit(paths, like_patterns):
    """SQL LIKE desenlerini (% _) dosya yollarıyla karşılaştırır."""
    import fnmatch
    for p in paths:
        for pat in like_patterns:
            glob = pat.replace("%", "*").replace("_", "?")
            if fnmatch.fnmatch(p.lower(), glob.lower()):
                return p
    return None


# ── kategori dedektörleri ─────────────────────────────────────────────
# ctx = {conn, loaded, known, all_paths, task_text, flags}
# Dönüş: (status, satisfied_by, candidates)

_PRIMARY_CONTENT = (
    "primary_symbol", "symbol_body", "implementation", "target_symbols",
    "target_files", "tested_symbols", "hot_path", "diff_targets",
    "execution_path",
)


def _detect_primary_content(ctx, category):
    loaded = ctx["loaded"]
    has_content = any(
        it.get("content") or it.get("symbols") for it in loaded
        if it.get("action") != "known_to_model")
    if has_content:
        return "satisfied", [it.get("file") for it in loaded[:3]], []
    # Model bu dosyaları daha önce gövde olarak gördüyse içerik zaten zihninde.
    body_known = [e.get("file") for e in ctx.get("known_entries", [])
                  if e.get("representation") in ("full", "range")]
    if body_known:
        return "satisfied", [f"known_to_model:{p}" for p in body_known[:3]], []
    if ctx["known"]:
        return "available", list(ctx["known"])[:3], []
    return "missing", [], []


def _detect_graph_relations(ctx, category):
    paths = ctx["all_paths"]
    degree = _edge_degree(ctx["conn"], paths)
    if degree > 0:
        return "satisfied", [f"{degree} graph edge"], []
    primary = ctx["loaded"][:1] or []
    cmds = [
        {"kind": "command",
         "cmd": f"python .context/scripts/get_related.py {it.get('file')}",
         "why": f"{category}: callers/callees + impact_set"}
        for it in primary if it.get("file")
    ]
    if _has_table(ctx["conn"], "edges"):
        return "available", [], cmds
    return "missing", [], cmds


def _detect_local_dependencies(ctx, category):
    """Treat an indexed leaf file as valid evidence for a local edit."""
    if not _has_table(ctx["conn"], "edges"):
        return "missing", [], []
    degree = _edge_degree(ctx["conn"], ctx["all_paths"])
    if degree > 0:
        return "satisfied", [f"{degree} local graph edge"], []
    return "waived", ["indexed target has no local dependency edges"], []


def _detect_tests(ctx, category):
    for p in ctx["all_paths"]:
        if _TEST_RE.search(p):
            return "satisfied", [p], []
    candidates = []
    rows = []
    if ctx["conn"] is not None:
        try:
            rows = ctx["conn"].execute(
                "SELECT path, token_estimate FROM files WHERE path LIKE '%test%' "
                "OR path LIKE '%spec%' LIMIT 6").fetchall()
        except sqlite3.Error:
            pass
    for path, est in rows:
        if _TEST_RE.search(path):
            candidates.append({"file": path, "est_tokens": int(est or 150)})
    if candidates:
        return "available", [], candidates[:3]
    return "missing", [], []


def _detect_nearby_test(ctx, category):
    """Find tests related by target stem/directory, never an arbitrary test."""
    primary_paths = [it.get("file", "") for it in ctx["loaded"]
                     if it.get("file") and not _TEST_RE.search(it.get("file", ""))]
    if not primary_paths:
        return "waived", ["no primary file for nearby-test matching"], []

    def related(test_path):
        test_norm = str(test_path).replace("\\", "/").lower()
        test_tokens = set(re.findall(r"[a-z0-9]+", test_norm))
        for primary in primary_paths[:4]:
            norm = str(primary).replace("\\", "/").lower()
            stem = norm.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            parent = norm.rsplit("/", 1)[0].rsplit("/", 1)[-1] if "/" in norm else ""
            if stem in test_tokens or (len(parent) >= 3 and parent in test_tokens):
                return True
        return False

    for path in ctx["all_paths"]:
        if _TEST_RE.search(path) and related(path):
            return "satisfied", [path], []

    rows = []
    if ctx["conn"] is not None:
        try:
            rows = ctx["conn"].execute(
                "SELECT path, token_estimate FROM files WHERE path LIKE '%test%' "
                "OR path LIKE '%spec%' LIMIT 200").fetchall()
        except sqlite3.Error:
            pass
    candidates = [
        {"file": path, "est_tokens": int(est or 150)}
        for path, est in rows if _TEST_RE.search(path) and related(path)
    ]
    if candidates:
        return "available", [], candidates[:3]
    return "waived", ["no nearby test indexed for the target"], []


def _detect_contracts(ctx, category):
    """Contract evidence must belong to the retrieved task area."""
    contract_re = re.compile(r"(^|/)(api|routes?|contracts?|openapi)(/|\.|$)|handler",
                             re.IGNORECASE)
    loaded_paths = [it.get("file", "") for it in ctx["loaded"] if it.get("file")]
    hits = [path for path in loaded_paths if contract_re.search(path)]
    if hits:
        return "satisfied", hits[:3], []

    task_explicit = re.search(r"\b(api|endpoint|contract|openapi|request|response)\b",
                              ctx.get("task_text", ""), re.IGNORECASE)
    if not task_explicit:
        return "waived", ["task has no API/contract boundary"], []

    candidates = _index_files(
        ctx["conn"], ["%handler%", "%api%", "%contract%", "%openapi%", "%route%"],
        limit=6)[:3]
    if candidates:
        return "available", [], candidates
    return "missing", [], []


def _make_index_detector(like_patterns, require_content=False):
    def detect(ctx, category):
        hit = _path_hit(ctx["all_paths"], like_patterns)
        if hit:
            return "satisfied", [hit], []
        candidates = _index_files(ctx["conn"], like_patterns, limit=6)[:3]
        if candidates:
            return "available", [], candidates
        return "missing", [], []
    return detect


def _detect_recent_modifications(ctx, category):
    if ctx["flags"].get("git_available"):
        return "available", [], [{
            "kind": "command",
            "cmd": "git log -n 8 --stat --oneline",
            "why": "recent_modifications: son değişiklikler",
        }]
    return "waived", ["git yok"], []


def _detect_runtime_evidence(ctx, category):
    if _RUNTIME_EVIDENCE_RE.search(ctx["task_text"] or ""):
        return "satisfied", ["task içinde runtime kanıtı var"], []
    return "waived", ["runtime kanıtı görevde verilmedi"], []


def _detect_project_map(ctx, category):
    if ctx["flags"].get("has_project_map"):
        return "satisfied", ["map.json"], []
    return "available", [], [{
        "kind": "command",
        "cmd": "python .context/scripts/index.py",
        "why": "project_map: index + harita üret",
    }]


def _detect_dependency_graph(ctx, category):
    if _has_table(ctx["conn"], "edges"):
        try:
            n = ctx["conn"].execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        except sqlite3.Error:
            n = 0
        if n > 0:
            return "satisfied", [f"{n} edge"], []
    return "available", [], [{
        "kind": "command",
        "cmd": "python .context/scripts/index.py",
        "why": "dependency_graph: graf yeniden kurulmalı",
    }]


def _detect_conventions(ctx, category):
    # Konvansiyonlar paketin kendisinden (stil/direktifler) çıkarılır;
    # ayrıca dosya kanıtı gerekmez.
    return "waived", ["paket direktifleri yeterli"], []


DETECTORS = {
    **{c: _detect_primary_content for c in _PRIMARY_CONTENT},
    "callers_callees": _detect_graph_relations,
    "dependents": _detect_graph_relations,
    "consumers": _detect_graph_relations,
    "local_dependencies": _detect_local_dependencies,
    # Ordinary implementation work needs tests for the selected target, not
    # arbitrary repository tests. Broad test-pattern discovery remains for an
    # explicitly TESTING task via ``test_patterns`` below.
    "tests": _detect_nearby_test,
    "nearby_test": _detect_nearby_test,
    "test_patterns": _detect_tests,
    "fixtures": _make_index_detector(["%conftest%", "%fixture%"]),
    "recent_modifications": _detect_recent_modifications,
    "schema": _make_index_detector(["%schema%", "%models.py"]),
    "models": _make_index_detector(["%models.py", "%models/%"]),
    "migrations": _make_index_detector(["%migration%", "%alembic%"]),
    "queries": _make_index_detector(
        ["%query%", "%queries%", "%repository%", "%repositories%", "%dao%",
         "%service.py"]),
    "input_schema": _make_index_detector(
        ["%handler%", "%api%", "%serial%", "%schema%", "%route%"]),
    "output_schema": _make_index_detector(
        ["%handler%", "%api%", "%serial%", "%schema%", "%route%"]),
    "contracts": _detect_contracts,
    "project_map": _detect_project_map,
    "module_summaries": _detect_project_map,
    "major_modules": _detect_project_map,
    "dependency_graph": _detect_dependency_graph,
    "config": _make_index_detector(
        ["%config%", "%settings%", "%package.json", "%pyproject%",
         "%requirements%", "%tsconfig%", "%vite%"]),
    "build_config": _make_index_detector(
        ["%package.json", "%pyproject%", "%requirements%", "%webpack%",
         "%vite%", "%Makefile%", "%tsconfig%"]),
    "auth_boundaries": _make_index_detector(
        ["%auth%", "%permission%", "%policy%", "%guard%"]),
    "middleware": _make_index_detector(["%middleware%", "%interceptor%"]),
    "entry_points": _make_index_detector(
        ["%main.py", "%app.py", "%server.py", "%index.ts", "%manage.py"]),
    "secrets_handling": _make_index_detector(
        ["%secret%", "%env%", "%credential%", "%vault%"]),
    "error_output": _detect_runtime_evidence,
    "logs": _detect_runtime_evidence,
    "benchmarks": _make_index_detector(["%bench%", "%perf%"]),
    "existing_docs": _make_index_detector(["%.md", "docs/%"]),
    "conventions": _detect_conventions,
    "dependencies": _make_index_detector(
        ["%package.json", "%pyproject%", "%requirements%", "%lock%"]),
    "related_symbols": _detect_primary_content,
}


def detect_evidence(conn, required, loaded_items, known_entries,
                    task_text="", flags=None):
    """required kategorilerin her biri için durum tespiti.

    known_entries: dict listesi ({"file", "representation", ...}) veya
    düz yol listesi (geriye uyum).
    """
    flags = flags or {}
    loaded = loaded_items or []
    known_norm = []
    for entry in known_entries or []:
        if isinstance(entry, dict):
            known_norm.append(entry)
        else:
            known_norm.append({"file": entry, "representation": "full"})
    known_paths = [e.get("file") for e in known_norm if e.get("file")]
    loaded_paths = [it.get("file") for it in loaded if it.get("file")]
    ctx = {
        "conn": conn,
        "loaded": loaded,
        "known": known_paths,
        "known_entries": known_norm,
        "all_paths": loaded_paths + known_paths,
        "task_text": task_text,
        "flags": flags,
    }
    result = {}
    for category in required:
        detector = DETECTORS.get(category)
        if detector is None:
            result[category] = {"status": "waived", "by": ["bilinmeyen kategori"],
                                "candidates": []}
            continue
        status, by, candidates = detector(ctx, category)
        result[category] = {"status": status, "by": by, "candidates": candidates}
    return result


def build_coverage(task_plan, evidence, quality=None, confidence_level="medium"):
    """Evidence tespiti → machine-readable ContextCoverage."""
    required = task_plan.get("required_evidence", []) if task_plan else []
    satisfied, missing, waived, expansion = [], [], [], []
    for category in required:
        entry = evidence.get(category, {"status": "waived", "by": [], "candidates": []})
        if entry["status"] == "satisfied":
            satisfied.append(category)
        elif entry["status"] == "waived":
            waived.append(category)
        else:
            missing.append(category)
            for cand in entry.get("candidates", []):
                cand = dict(cand)
                cand["for"] = category
                expansion.append(cand)

    effective = len(required) - len(waived)
    coverage = (len(satisfied) / effective) if effective > 0 else 1.0

    # Dosya adaylarını token tahminine göre sırala (ucuz olan önce).
    file_cands = [c for c in expansion if "file" in c]
    cmd_cands = [c for c in expansion if "cmd" in c]
    file_cands.sort(key=lambda c: c.get("est_tokens", 999))
    seen_files = set()
    deduped = []
    for cand in file_cands:
        if cand["file"] not in seen_files:
            seen_files.add(cand["file"])
            deduped.append(cand)
    recommended = deduped[:MAX_EXPANSION_FILES] + cmd_cands[:2]

    quality_score = int((quality or {}).get("score", 0) or 0)
    sufficient = bool(missing == []) or (
        coverage >= COVERAGE_TARGET and quality_score >= 45)

    return {
        "task_type": (task_plan or {}).get("task_type", "unknown"),
        "required_evidence": required,
        "satisfied_evidence": satisfied,
        "missing_evidence": missing,
        "waived_evidence": waived,
        "coverage": round(coverage, 2),
        "coverage_target": COVERAGE_TARGET,
        "confidence": confidence_level,
        "quality_score": quality_score,
        "sufficient": bool(sufficient),
        "recommended_expansion": recommended,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Context sufficiency engine 2.0")
    parser.add_argument("task_type", help="ör. BUG_FIX")
    parser.add_argument("--files", nargs="*", default=[], help="yüklenmiş dosyalar")
    parser.add_argument("--known", nargs="*", default=[], help="modelin bildiği dosyalar")
    parser.add_argument("--task", default="", help="görev metni")
    parser.add_argument("--db", default=None, help="symbols.db yolu")
    args = parser.parse_args()

    try:
        from planner import TYPE_PROFILES
    except ImportError:
        from scripts.planner import TYPE_PROFILES
    profile = TYPE_PROFILES.get(args.task_type) or TYPE_PROFILES["FEATURE"]
    task_plan = {"task_type": args.task_type,
                 "required_evidence": list(profile["evidence"])}

    conn = None
    if args.db:
        conn = sqlite3.connect(args.db)
    try:
        evidence = detect_evidence(conn, task_plan["required_evidence"],
                                   [{"file": f} for f in args.files],
                                   args.known, task_text=args.task)
        print(json.dumps(build_coverage(task_plan, evidence),
                         indent=2, ensure_ascii=False))
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    main()
