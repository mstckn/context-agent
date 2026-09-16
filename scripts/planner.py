#!/usr/bin/env python3
"""
planner.py - Task-aware context planner (Phase 1d / spec PHASE 5 + 11).

Statik, tek-tip context planlaması yerine görev tipine göre:
  * task_type sınıflandırması (deterministik anahtar kelime kuralları),
  * risk seviyesi (LOW/MEDIUM/HIGH/CRITICAL),
  * retrieval stratejisi (temsil düzeyi + odak),
  * bütçe aralığı (kullanılabilir context'in yüzdesi),
  * zorunlu evidence kategorileri (sufficiency engine bunu tüketir).

LLM YOK — tamamen kural tabanlı, tekrarlanabilir.

Kullanım:
  python planner.py "Fix the payment bug"
  python planner.py "Auth refactor" --json
"""

import json
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Görev tipi sınıflandırması ────────────────────────────────────────
# Her tip için anahtar kelimeler (EN + TR). Sıralama öncelik bildirir:
# aynı puan durumunda ÖNCE gelen tip kazanır.
TYPE_KEYWORDS = {
    "BUILD_FAILURE": [
        "build fail", "build error", "compile error", "cannot compile",
        "ci fail", "ci pipeline", "install fail", "derleme hatası", "build hatası",
        "compilation", "webpack error", "syntax error",
    ],
    "RUNTIME_FAILURE": [
        "runtime error", "crash", "stack trace", "traceback", "exception",
        "panic", "segfault", "timeout error", "çalışma zamanı", "exception",
        "500 error", "unhandled",
    ],
    "BUG_FIX": [
        "fix", "bug", "broken", "incorrect", "wrong result", "issue",
        "doesn't work", "does not work", "fails", "hata", "düzelt", "bozuk",
        "yanlış", "çalışmıyor", "sorun",
    ],
    "SECURITY": [
        "vulnerab", "exploit", "injection", "xss", "csrf", "auth bypass",
        "security", "harden", "secret", "credential", "authorization",
        "permission", "access control", "admin", "role", "güvenlik", "açık",
        "zafiyet", "sanitize", "owasp",
    ],
    "DATABASE_CHANGE": [
        "migration", "migrate", "schema change", "add column", "drop column",
        "alter table", "database", "db schema", "index on", "veritabanı",
        "tablo", "kolon ekle", "migration",
    ],
    "API_CHANGE": [
        "endpoint", "api contract", "rest api", "response format",
        "status code", "request body", "api change", "breaking change",
        "openapi", "graphql", "endpoint ekle", "api değiştir",
    ],
    "PERFORMANCE": [
        "slow", "performance", "optimize", "latency", "throughput",
        "profiling", "profiler", "n+1", "memory leak", "perf", "performans", "yavaş",
        "hızlandır", "optimizasyon",
    ],
    "ARCHITECTURE": [
        "architect", "redesign", "restructure", "modularize", "system design",
        "entire", "whole project", "mimari", "yeniden yapılandır",
        "mikroservis", "layer",
    ],
    "REFACTOR": [
        "refactor", "clean up", "reorganize", "rename", "extract",
        "simplify", "refaktör", "yeniden düzenle", "temizle", "basitleştir",
    ],
    "TESTING": [
        "test", "spec", "coverage", "assert", "unit test", "integration test",
        "test ekle", "test yaz", "mock", "fixture",
    ],
    "DOCUMENTATION": [
        "document", "readme", "docstring", "comment", "changelog",
        "doküman", "belgele", "açıklama yaz",
    ],
    "CODE_REVIEW": [
        "review", "audit", "code review", "incele", "gözden geçir", "denetle",
    ],
    "REPOSITORY_EXPLORATION": [
        "explore", "how does", "where is", "what is", "understand",
        "structure of", "keşfet", "nasıl çalışır", "nerede", "nedir", "anla",
    ],
    "FEATURE": [
        "add", "implement", "create", "new feature", "build a", "support",
        "ekle", "yeni özellik", "oluştur", "geliştir", "destekle",
    ],
    "LOCAL_EDIT": [
        "typo", "comment", "format", "lint", "small change", "one line",
        "css tweak", "border radius", "font size", "line height",
        "write korre", "ufak", "küçük değişiklik", "tek satır",
    ],
}

# These entries are intentional word stems. All other keywords are matched as
# complete words/phrases so e.g. ``profile`` cannot accidentally match the
# PERFORMANCE stem ``profil`` and ``latest`` cannot match ``test``.
PREFIX_KEYWORDS = {"vulnerab", "architect"}


def _keyword_hit(text, keyword):
    escaped = re.escape(keyword)
    if keyword in PREFIX_KEYWORDS:
        return re.search(rf"(?<!\w){escaped}\w*", text) is not None
    return re.search(rf"(?<!\w){escaped}(?!\w)", text) is not None

# Spesifiklik ağırlığı: dar/özel tipler genel tiplere baskın gelsin.
TYPE_WEIGHT_BONUS = {
    "BUILD_FAILURE": 2, "RUNTIME_FAILURE": 2, "SECURITY": 2,
    "DATABASE_CHANGE": 1, "API_CHANGE": 1, "PERFORMANCE": 1,
    "ARCHITECTURE": 1, "REPOSITORY_EXPLORATION": 1,
}

DEFAULT_TYPE = "FEATURE"

# ── Risk sinyalleri (spec PHASE 11) ───────────────────────────────────
# (desen, ağırlık) — ağırlık 2 = kritik aile, 1 = orta.
RISK_SIGNALS = [
    (r"auth(entication|orization)?\b|login|logout|jwt|session|password|token", 2, "authentication"),
    (r"billing|payment|charge|invoice|subscription|financial|fatura|ödeme", 2, "financial"),
    (r"secret|credential|api[_ ]?key|encryption|encrypt|decrypt|şifre", 2, "secrets/encryption"),
    (r"migrat|schema|alter table|drop|veritabanı|tablo", 2, "database schema"),
    (r"public api|external api|contract|endpoint", 1, "public api"),
    (r"concurren|race|lock|async|thread|parallel", 1, "concurrency"),
    (r"distribut|queue|worker|microservice", 1, "distributed"),
    (r"deploy|infrastructure|docker|kubernetes|ci/cd|prod", 1, "infrastructure"),
    (r"destructive|delete all|truncate|force", 2, "destructive"),
    (r"security|vulnerab|permission|role|yetki", 2, "security"),
]

RISK_LEVELS = [(4, "CRITICAL"), (2, "HIGH"), (1, "MEDIUM"), (0, "LOW")]

# ── Tip başına strateji, bütçe ve evidence ───────────────────────────
# representation: primary dosyalar için hedef temsil düzeyi.
# budget: kullanılabilir context bütçesinin (min, max) oranı.
# evidence: sufficiency engine'in doğrulaması gereken kategoriler.
TYPE_PROFILES = {
    "LOCAL_EDIT": {
        "representation": "range",
        "focus": "primary symbol + immediate neighborhood",
        "budget": (0.05, 0.15),
        "evidence": ["primary_symbol", "local_dependencies", "nearby_test"],
    },
    "BUG_FIX": {
        "representation": "full",
        "focus": "execution path around the failing symbol",
        "budget": (0.15, 0.35),
        "evidence": ["symbol_body", "callers_callees", "tests", "recent_modifications"],
    },
    "FEATURE": {
        "representation": "outline",
        "focus": "target area + integration points + conventions",
        "budget": (0.20, 0.45),
        "evidence": ["implementation", "related_symbols", "tests", "contracts"],
    },
    "REFACTOR": {
        "representation": "outline",
        "focus": "target symbols + all dependents + tests",
        "budget": (0.25, 0.50),
        "evidence": ["target_symbols", "dependents", "tests", "contracts"],
    },
    "ARCHITECTURE": {
        "representation": "symbols",
        "focus": "project map + major modules + dependency graph",
        "budget": (0.35, 0.70),
        "evidence": ["project_map", "major_modules", "dependency_graph", "contracts"],
    },
    "DATABASE_CHANGE": {
        "representation": "full",
        "focus": "schema + models + migrations + consumers",
        "budget": (0.20, 0.45),
        "evidence": ["schema", "models", "migrations", "queries", "consumers", "tests"],
    },
    "API_CHANGE": {
        "representation": "full",
        "focus": "implementation + request/response contract + consumers",
        "budget": (0.20, 0.45),
        "evidence": ["implementation", "input_schema", "output_schema", "consumers", "tests"],
    },
    "SECURITY": {
        "representation": "full",
        "focus": "auth boundaries + config + entry points (aggressive optimization FORBIDDEN)",
        "budget": (0.30, 0.60),
        "evidence": ["auth_boundaries", "config", "middleware", "entry_points",
                     "secrets_handling", "tests"],
    },
    "PERFORMANCE": {
        "representation": "range",
        "focus": "hot path + measurement points",
        "budget": (0.20, 0.45),
        "evidence": ["hot_path", "callers_callees", "benchmarks", "tests"],
    },
    "TESTING": {
        "representation": "range",
        "focus": "system under test + existing test conventions",
        "budget": (0.15, 0.35),
        "evidence": ["tested_symbols", "test_patterns", "fixtures"],
    },
    "BUILD_FAILURE": {
        "representation": "range",
        "focus": "build config + failing module",
        "budget": (0.15, 0.30),
        "evidence": ["build_config", "error_output", "dependencies"],
    },
    "RUNTIME_FAILURE": {
        "representation": "range",
        "focus": "execution path in the stack trace",
        "budget": (0.15, 0.35),
        "evidence": ["execution_path", "symbol_body", "logs", "config"],
    },
    "DOCUMENTATION": {
        "representation": "outline",
        "focus": "target files + existing docs",
        "budget": (0.10, 0.25),
        "evidence": ["target_files", "existing_docs"],
    },
    "CODE_REVIEW": {
        "representation": "range",
        "focus": "changed areas + callers + tests + conventions",
        "budget": (0.15, 0.40),
        "evidence": ["diff_targets", "callers_callees", "tests", "conventions"],
    },
    "REPOSITORY_EXPLORATION": {
        "representation": "symbols",
        "focus": "project map + module summaries",
        "budget": (0.10, 0.30),
        "evidence": ["project_map", "module_summaries"],
    },
}

# HIGH/CRITICAL riskte her plana eklenen evidence.
RISK_EXTRA_EVIDENCE = ["tests", "contracts"]


def classify_task_type(task_text):
    """Deterministik görev tipi sınıflandırması.

    Dönüş: (task_type, matched_keywords)
    """
    text = (task_text or "").lower()
    if not text.strip():
        return "REPOSITORY_EXPLORATION", []

    scores = {}
    matches = {}
    for task_type, keywords in TYPE_KEYWORDS.items():
        hits = [kw for kw in keywords if _keyword_hit(text, kw)]
        if hits:
            # Uzun (daha spesifik) anahtar kelime daha çok puan verir.
            score = sum(1 + 0.5 * (len(kw.split()) - 1) for kw in hits)
            score += TYPE_WEIGHT_BONUS.get(task_type, 0)
            scores[task_type] = score
            matches[task_type] = hits

    if not scores:
        return DEFAULT_TYPE, []

    best = max(scores.values())
    # Eşitlikte TYPE_KEYWORDS sırası (dict insertion order) öncelik verir.
    for task_type in TYPE_KEYWORDS:
        if scores.get(task_type) == best:
            return task_type, matches[task_type]
    return DEFAULT_TYPE, []  # pragma: no cover


def assess_risk(task_text):
    """Risk sinyalleri → (level, matched_signals)."""
    text = (task_text or "").lower()
    matched = []
    total = 0
    for pattern, weight, label in RISK_SIGNALS:
        if re.search(pattern, text):
            matched.append(label)
            total += weight
    for threshold, level in RISK_LEVELS:
        if total >= threshold:
            return level, matched
    return "LOW", matched  # pragma: no cover


def plan_task(task_text, route_analysis=None):
    """Görev metni → TaskPlan (machine-readable dict).

    route_analysis: route.classify_task çıktısı (opsiyonel zenginleştirme).
    """
    task_type, matched = classify_task_type(task_text)
    risk, risk_signals = assess_risk(task_text)
    profile = TYPE_PROFILES[task_type]

    budget_min, budget_max = profile["budget"]
    evidence = list(profile["evidence"])

    # HIGH/CRITICAL: evidence gereksinimleri artar, bütçe tabanı yükselir
    # (agresif optimizasyon YASAK — spec PHASE 11).
    if risk in ("HIGH", "CRITICAL"):
        for extra in RISK_EXTRA_EVIDENCE:
            if extra not in evidence:
                evidence.append(extra)
        budget_min = min(budget_max, max(budget_min, budget_max - 0.15))
    if risk == "CRITICAL":
        budget_min = budget_max

    plan = {
        "task_type": task_type,
        "classification_signals": matched,
        "risk": risk,
        "risk_signals": risk_signals,
        "strategy": {
            "focus": profile["focus"],
            "primary_representation": profile["representation"],
            "use_dependency_graph": task_type in (
                "BUG_FIX", "REFACTOR", "ARCHITECTURE", "API_CHANGE",
                "DATABASE_CHANGE", "CODE_REVIEW", "SECURITY"),
            "use_content_search": task_type in (
                "BUG_FIX", "RUNTIME_FAILURE", "REPOSITORY_EXPLORATION",
                "CODE_REVIEW", "PERFORMANCE"),
        },
        "budget_fraction": {"min": round(budget_min, 2), "max": round(budget_max, 2)},
        "required_evidence": evidence,
        "notes": [],
    }

    if route_analysis and isinstance(route_analysis, dict):
        domains = route_analysis.get("domains") or []
        if domains:
            plan["domains"] = domains
        complexity = route_analysis.get("complexity")
        if complexity == "low" and task_type == DEFAULT_TYPE:
            # Basit değişiklik sinyali varsa küçük bütçeye yaslan.
            plan["budget_fraction"]["min"] = 0.05
            plan["budget_fraction"]["max"] = 0.20

    return plan


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Task-aware context planner")
    parser.add_argument("task", help="Görev metni")
    parser.add_argument("--json", action="store_true", help="Compact JSON çıktı")
    args = parser.parse_args()

    plan = plan_task(args.task)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False))
    else:
        print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
