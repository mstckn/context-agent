#!/usr/bin/env python3
"""
route.py - Task'i analiz eder, gerekli dosya/sembol/context planını üretir.

Kullanım:
  python route.py "Add rate limiting to login endpoint"
  python route.py "Fix the payment bug"
  python route.py "Refactor auth module" --dry-run
"""

import sys, json, sqlite3, re, argparse
from pathlib import Path

try:  # run directly: `python route.py` (script dir on sys.path)
    from paths import find_context_dir
except ImportError:  # imported as a package: `from scripts import route`
    from scripts.paths import find_context_dir

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TASK_PATTERNS = {
    "auth": ["login", "logout", "auth", "token", "jwt", "session", "password", "permission", "role"],
    "payment": ["payment", "charge", "stripe", "billing", "invoice", "subscription", "price"],
    "user": ["user", "profile", "account", "register", "signup"],
    "api": ["endpoint", "route", "api", "rest", "http", "request", "response", "controller", "handler"],
    "db": ["database", "query", "migration", "model", "schema", "sql", "repository"],
    "test": ["test", "tests", "testing", "spec", "mock", "fixture", "assert"],
    "config": ["config", "setting", "env", "environment", "variable"],
    "cache": ["cache", "redis", "memcache", "invalidate"],
    "email": ["email", "mail", "smtp", "send", "notification"],
    "file": ["upload", "download", "file", "storage", "s3", "blob"],
}

COMPLEXITY_INDICATORS = {
    "high": ["refactor", "migrate", "redesign", "architect", "entire", "all", "whole"],
    "medium": ["add", "implement", "create", "build", "fix bug", "update"],
    "low": ["rename", "typo", "comment", "format", "lint", "small"],
}

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "your", "you",
    "bir", "ile", "icin", "için", "gibi", "olan", "bunu", "şunu", "sun",
    "add", "fix", "update", "change", "create", "implement", "refactor",
    # Genel doldurucu kod isimleri: "the X module/file" kalıbında taşıyıcı
    # değil, niteleyicidirler; skoru şişirip asıl hedefi düşürürler.
    "module", "modules",
    # Katman/kanıt sözcükleri tek başına her model/service/test/frontend
    # dosyasını eşleştirmemeli. Domain ve graph expansion bunları bağlama
    # özgü biçimde ekler; iş terimleri (refund, account_id, jobs...) sıralar.
    "test", "tests", "testing", "verify", "behavior",
    "model", "models", "service", "services",
    "frontend", "backend", "consumer", "consumers",
}

CONCEPT_ALIASES = {
    "throttle": ["rate", "limit", "limiter", "quota"],
    "throttling": ["rate", "limit", "limiter", "quota"],
    "ratelimit": ["rate", "limit", "limiter", "quota"],
    "login": ["auth", "session", "token", "password"],
    "signin": ["login", "auth", "session"],
    "signup": ["register", "account", "user"],
    "empty": ["placeholder", "fallback", "blank", "none"],
    "state": ["status", "view", "ui"],
    "tenant": ["organization", "workspace", "account", "isolation"],
    "isolation": ["tenant", "scope", "permission", "boundary"],
    "dashboard": ["metrics", "status", "quality", "display"],
    "compiler": ["typecheck", "typescript", "build"],
    "traceback": ["error", "exception", "stack", "log"],
    "timeout": ["deadline", "retry", "latency", "slow"],
    "cache": ["memo", "redis", "ttl", "store"],
}


_PREFIX_INDICATORS = {"architect"}


def _text_has(text, indicator):
    """Match task indicators as words/phrases, not arbitrary substrings."""
    escaped = re.escape(indicator)
    if indicator in _PREFIX_INDICATORS:
        return re.search(rf"(?<!\w){escaped}\w*", text) is not None
    return re.search(rf"(?<!\w){escaped}(?!\w)", text) is not None


def tokenize(text):
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]+|\w+", text.lower())
    return [w for w in words if len(w) >= 3 and w not in STOPWORDS]

def expand_keywords(words):
    expanded = []
    seen = set()
    for word in words:
        for item in [word] + CONCEPT_ALIASES.get(word, []):
            if item not in seen and item not in STOPWORDS:
                seen.add(item)
                expanded.append(item)
    return expanded

def classify_task(task_text):
    task_lower = task_text.lower()
    raw_words = tokenize(task_text)
    words = expand_keywords(raw_words)

    domains = []
    for domain, keywords in TASK_PATTERNS.items():
        if any(_text_has(task_lower, kw) for kw in keywords):
            domains.append(domain)

    complexity = "medium"
    for level, indicators in COMPLEXITY_INDICATORS.items():
        if any(_text_has(task_lower, ind) for ind in indicators):
            complexity = level
            break

    op_type = "unknown"
    if any(_text_has(task_lower, w) for w in ["fix", "bug", "error", "issue", "broken"]):
        op_type = "bugfix"
    elif any(_text_has(task_lower, w) for w in ["add", "implement", "create", "new"]):
        op_type = "feature"
    elif any(_text_has(task_lower, w) for w in ["refactor", "clean", "reorganize"]):
        op_type = "refactor"
    elif any(_text_has(task_lower, w) for w in ["update", "change", "modify", "edit"]):
        op_type = "modify"
    elif any(_text_has(task_lower, w) for w in ["test", "spec"]):
        op_type = "test"

    return {
        "domains": domains,
        "complexity": complexity,
        "op_type": op_type,
        "keywords": words[:20],
        "raw_keywords": raw_words[:20],
    }

def add_score(scores, file_path, points, reason):
    item = scores.setdefault(file_path, {"score": 0, "reasons": []})
    item["score"] += points
    item["reasons"].append({"points": points, "reason": reason})


def _identifier_tokens(text):
    """İdentifier'ı küçük harfli token'lara böler (snake_case + camelCase)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(text or ""))
    return {t for t in re.split(r"[^a-z0-9]+", spaced.lower()) if t}


def _norm_plural(token):
    """Hafif plural normalizasyonu: 'migration' ≈ 'migrations'."""
    token = str(token)
    return token[:-1] if len(token) > 3 and token.endswith("s") else token

def fetch_file_info(conn, path):
    row = conn.execute(
        "SELECT path, lang, line_count, summary, token_estimate FROM files WHERE path=?",
        (path,)
    ).fetchone()
    if not row:
        return None
    return {
        "file": row[0],
        "lang": row[1],
        "lines": row[2],
        "summary": row[3],
        "token_estimate": row[4] or 0,
    }

def rank_symbols(symbols, keywords):
    def symbol_rank(row):
        name, stype, line, signature = row
        text = f"{name} {signature}".lower()
        best = 100
        for kw in keywords:
            if name.lower() == kw:
                best = min(best, 0)
            elif kw in name.lower():
                best = min(best, 1)
            elif kw in text:
                best = min(best, 2)
        if stype == "class" and best > 10:
            best = 10
        return (best, line or 0, name)

    return sorted(symbols, key=symbol_rank)

def focus_symbols(symbols, keywords, limit=5):
    focused = []
    for name, stype, line, signature in symbols:
        text = f"{name} {signature}".lower()
        if any(kw in text for kw in keywords):
            focused.append((name, stype, line, signature))
    return focused[:limit] or symbols[:min(3, len(symbols))]

def find_neighbor_files(conn, file_path, max_neighbors=4):
    neighbors = []
    seen = set()

    # Prefer the AST/import graph. The legacy imports table can contain a
    # malformed multi-line value for consecutive Python imports; graph edges
    # are exact project-file relations and preserve real dependencies.
    try:
        graph_rows = conn.execute(
            """
            SELECT dst_file, 'imports'
            FROM edges
            WHERE src_file=? AND kind='imports' AND dst_file IS NOT NULL
            UNION ALL
            SELECT src_file, 'imported_by'
            FROM edges
            WHERE dst_file=? AND kind='imports' AND src_file != ?
            """,
            (file_path, file_path, file_path),
        ).fetchall()
        # Dependencies first, then test importers, then other consumers.  This
        # keeps the implementation contract and its regression evidence in a
        # small neighbor budget (instead of letting an arbitrary CLI importer
        # displace the test file).
        graph_rows = sorted(
            graph_rows,
            key=lambda row: (
                0 if row[1] == "imports" else
                1 if "test" in str(row[0]).replace("\\", "/").lower() else 2
            ),
        )
        for related_path, relation in graph_rows:
            if related_path in seen or related_path == file_path:
                continue
            row = conn.execute(
                "SELECT path, summary, line_count, token_estimate "
                "FROM files WHERE path=?", (related_path,)
            ).fetchone()
            if not row:
                continue
            seen.add(row[0])
            neighbors.append({
                "file": row[0], "relation": relation,
                "summary": row[1] or "", "lines": row[2],
                "token_estimate": row[3] or 0,
            })
            if len(neighbors) >= max_neighbors:
                break
    except sqlite3.DatabaseError:
        pass

    # An exact project graph is authoritative. Do not supplement it with the
    # legacy basename search: a path such as payments/models.py would then
    # acquire unrelated importers of auth/models.py merely because both stems
    # are named "models".
    if neighbors:
        return neighbors

    imports = conn.execute(
        "SELECT imports_from FROM imports WHERE file=? LIMIT 20",
        (file_path,)
    ).fetchall()

    for (imp,) in imports:
        patterns = [imp.replace(".", "/"), imp.replace(".", "\\"), imp]
        for pattern in patterns:
            row = conn.execute(
                "SELECT path, summary, line_count, token_estimate FROM files WHERE path LIKE ? LIMIT 1",
                (f"%{pattern}%",)
            ).fetchone()
            if row and row[0] not in seen and row[0] != file_path:
                seen.add(row[0])
                neighbors.append({
                    "file": row[0],
                    "relation": "imports",
                    "summary": row[1] or "",
                    "lines": row[2],
                    "token_estimate": row[3] or 0,
                })
                break
        if len(neighbors) >= max_neighbors:
            break

    stem = Path(file_path).stem
    importers = conn.execute("""
        SELECT i.file, f.summary, f.line_count, f.token_estimate
        FROM imports i
        JOIN files f ON f.path = i.file
        WHERE i.imports_from LIKE ?
        LIMIT ?
    """, (f"%{stem}%", max_neighbors)).fetchall()

    for row in importers:
        if row[0] not in seen and row[0] != file_path:
            seen.add(row[0])
            neighbors.append({
                "file": row[0],
                "relation": "imported_by",
                "summary": row[1] or "",
                "lines": row[2],
                "token_estimate": row[3] or 0,
            })
        if len(neighbors) >= max_neighbors:
            break

    return neighbors

def fetch_file_matches(conn, term, include_summary=False, limit=20, prefix=True):
    """Return file rows via FTS, falling back to LIKE if FTS is unavailable or stale.

    prefix=True  → ``term*`` (recall; 'wrap' ⊂ 'wrapper' da döner)
    prefix=False → tam token eşleşmesi (precision; sadece 'wrap' token'ı)
    """
    try:
        # ORDER BY rank → FTS5 bm25 sıralaması: en alakalı eşleşmeler LIMIT'te
        # hayatta kalır (önceden gelişigüzel sırada kesiliyordu).
        query = f"{term}*" if prefix else f'"{term}"'
        return conn.execute("""
            SELECT path FROM files_fts
            WHERE files_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit)).fetchall()
    except sqlite3.DatabaseError:
        if include_summary:
            return conn.execute("""
                SELECT path FROM files
                WHERE lower(path) LIKE ? OR lower(summary) LIKE ?
                LIMIT ?
            """, (f"%{term}%", f"%{term}%", limit)).fetchall()
        return conn.execute("""
            SELECT path FROM files
            WHERE lower(path) LIKE ?
            LIMIT ?
        """, (f"%{term}%", limit)).fetchall()

def find_relevant_files(task_info, conn, limit=10):
    scores = {}
    keywords = task_info["keywords"]

    for kw in keywords:
        # Tam token eşleşmeleri prefix eşleşmelerinden daha güçlü sinyaldir
        # ('logger' tam token'ı güçlü; 'wrap*' → 'wrapper' prefix zayıf).
        exact_paths = {p for (p,) in fetch_file_matches(conn, kw, prefix=False)}
        rows = fetch_file_matches(conn, kw)
        for (path,) in rows:
            stem = Path(path).stem.lower()
            path_text = path.replace("\\", "/").lower()
            path_parts = re.split(r"[^a-z0-9]+", path_text)
            plural_parts = {_norm_plural(p) for p in path_parts}
            if (kw == stem or kw in path_parts
                    or _norm_plural(kw) == _norm_plural(stem)
                    or _norm_plural(kw) in plural_parts):
                points = 24
                reason = f"direct path segment matched '{kw}'"
            elif kw in path_text:
                points = 10
                reason = f"path matched '{kw}'"
            elif path in exact_paths:
                points = 10
                reason = f"summary matched '{kw}'"
            else:
                points = 5
                reason = f"prefix matched '{kw}'"
            add_score(scores, path, points, reason)

    # Domain kalıpları recall yardımıdır, ranking sinyali değil: dosya başına
    # toplam katkısı sınırlanır, yoksa 9 kalıp bir dosyayı +45 şişirir ve
    # asıl hedefi (ör. utils/logging.py) top-N dışına iter.
    DOMAIN_CAP = 8
    domain_points: dict[str, int] = {}
    for domain in task_info["domains"]:
        if domain == "test" and task_info.get("op_type") != "test":
            continue
        for pattern in TASK_PATTERNS.get(domain, []):
            rows = fetch_file_matches(conn, pattern, include_summary=True)
            for (path,) in rows:
                used = domain_points.get(path, 0)
                if used >= DOMAIN_CAP:
                    continue
                pts = min(5, DOMAIN_CAP - used)
                domain_points[path] = used + pts
                add_score(scores, path, pts, f"domain:{domain} matched '{pattern}'")

    for kw in keywords:
        symbol_rows = conn.execute("""
            SELECT name, type, file, start_line, signature
            FROM symbols
            WHERE lower(name) LIKE ? OR lower(signature) LIKE ?
            LIMIT 20
        """, (f"%{kw}%", f"%{kw}%")).fetchall()
        for name, stype, file_path, line, signature in symbol_rows:
            # LIKE substring recall'ını koru ama skorlamada token sınırı uygula:
            # 'wrap' ⊂ 'wrapper' gibi yanlış pozitifler puan almasın.
            name_tokens = _identifier_tokens(name)
            if kw not in name_tokens and kw not in _identifier_tokens(signature):
                continue
            # Tam ad eşleşmesi en güçlü sinyaldir; ancak get/set/run/add gibi
            # kısa jenerik fiil adları her projede yaygındır, tam puan verirse
            # asıl hedefi (ör. utils/logging.py) gömer.
            if name.lower() == kw and len(kw) > 3:
                points = 12
            elif name.lower() == kw:
                points = 7
            elif kw in name_tokens:
                # Anahtar kelime sembol ADININ token'ı: imza eşleşmesinden güçlü.
                points = 8
            else:
                points = 7
            add_score(scores, file_path, points, f"symbol:{name} ({stype}) matched '{kw}'")

        file_rows = fetch_file_matches(conn, kw, include_summary=True)
        for (path,) in file_rows:
            add_score(scores, path, 4, f"file/summary matched '{kw}'")

    # Test tasks should include nearby test files when discovered by naming.
    if task_info["op_type"] == "test" or "test" in task_info["domains"]:
        for path in list(scores):
            stem = Path(path).stem
            rows = conn.execute("""
                SELECT path FROM files
                WHERE lower(path) LIKE ? OR lower(path) LIKE ?
                LIMIT 10
            """, (f"%test%{stem.lower()}%", f"%{stem.lower()}%test%")).fetchall()
            for (test_path,) in rows:
                add_score(scores, test_path, 6, f"nearby test for {path}")

    # Test dosyaları destekleyici kanıttır, birincil hedef değil. Görev test
    # odaklı değilse hafif sönümleme: testler sette kalır (recall korunur),
    # implementasyon dosyaları top-1'e yerleşir.
    if task_info.get("op_type") != "test":
        for path, scored in scores.items():
            low = path.replace("\\", "/").lower()
            stem = Path(path).stem.lower()
            is_test_file = (
                low.startswith(("tests/", "test/"))
                or "/tests/" in low
                or "/test/" in low
                or stem.startswith("test_")
            )
            if is_test_file and scored["score"] > 0:
                damped = max(1, int(scored["score"] * 0.85))
                if damped != scored["score"]:
                    add_score(scores, path, damped - scored["score"],
                              "test file damping (auxiliary evidence)")

    ranked_all = sorted(scores.items(), key=lambda x: (-x[1]["score"], x[0]))
    ranked_all = [
        item for item in ranked_all
        if not (
            Path(item[0]).name == "__init__.py"
            and int((fetch_file_info(conn, item[0]) or {}).get("lines") or 0) <= 2
        )
    ]
    if ranked_all:
        # Low-score tail matches are often generic symbols (for example
        # reporting.error_rate for a login rate-limit task).  Keep strong
        # matches plus one structural anchor for every requested layer; exact
        # dependencies are added separately through the import graph.
        top_score = ranked_all[0][1]["score"]
        cutoff = max(8, int(top_score * 0.30 + 0.999))
        kept = [item for item in ranked_all if item[1]["score"] >= cutoff]
        kept_paths = {path for path, _score in kept}

        role_needles = []
        domains = set(task_info.get("domains", []))
        if "api" in domains:
            role_needles.append(("api/", "handler", "controller", "routes.py"))
        if "db" in domains:
            role_needles.extend([
                ("migration", "schema"),
                ("model", "repository", "db/"),
            ])
        for needles in role_needles:
            candidate = next(
                (
                    item for item in ranked_all
                    if any(
                        needle in (
                            item[0].replace("\\", "/").lower() + " " +
                            str(fetch_file_info(conn, item[0]).get("summary", "")).lower()
                        )
                        for needle in needles
                    )
                ),
                None,
            )
            if candidate and candidate[0] not in kept_paths:
                kept.append(candidate)
                kept_paths.add(candidate[0])
        ranked = sorted(kept, key=lambda x: (-x[1]["score"], x[0]))[:limit]
    else:
        ranked = []
    results = []
    for path, scored in ranked:
        info = fetch_file_info(conn, path)
        if not info:
            continue
        if Path(path).name == "__init__.py" and int(info.get("lines") or 0) <= 2:
            continue
        symbols = conn.execute(
            "SELECT name, type, start_line, signature FROM symbols WHERE file=? LIMIT 500",
            (path,)
        ).fetchall()
        ranked_symbols = rank_symbols(symbols, keywords)[:8]
        focused_symbols = focus_symbols(ranked_symbols, keywords)
        focused_by_query = any(
            any(kw in _identifier_tokens(f"{name} {signature}")
                for kw in keywords)
            for name, _stype, _line, signature in focused_symbols
        )
        info.update({
            "score": scored["score"],
            "reasons": scored["reasons"][:8],
            "key_symbols": [
                {"name": n, "type": t, "line": l, "signature": s}
                for n, t, l, s in ranked_symbols
            ],
            "focus_symbols": [
                {"name": n, "type": t, "line": l, "signature": s}
                for n, t, l, s in focused_symbols
            ],
            "focused_by_query": focused_by_query,
            "related_files": find_neighbor_files(conn, path),
        })
        results.append(info)
    return results

def build_context_plan(relevant_files, budget_limit=8000):
    plan = []
    used = 0

    for idx, file_info in enumerate(relevant_files):
        priority = "primary" if idx < 3 else "secondary"
        action = "load_file" if file_info["token_estimate"] <= 1200 else "load_symbols"
        estimated = min(file_info["token_estimate"], 1200) if action == "load_symbols" else file_info["token_estimate"]
        if used + estimated > budget_limit:
            action = "defer"
            estimated = 0
        else:
            used += estimated
        plan.append({
            "file": file_info["file"],
            "priority": priority,
            "action": action,
            "estimated_tokens": estimated,
            "why": [r["reason"] for r in file_info.get("reasons", [])[:3]],
        })

    # Small directly related files are useful premium context; include after primaries.
    seen = {item["file"] for item in plan}
    for file_info in relevant_files[:5]:
        for rel in file_info.get("related_files", [])[:3]:
            if rel["file"] in seen:
                continue
            estimated = rel.get("token_estimate", 0)
            action = "load_file" if estimated <= 800 and used + estimated <= budget_limit else "defer"
            if action == "load_file":
                used += estimated
            seen.add(rel["file"])
            plan.append({
                "file": rel["file"],
                "priority": "related",
                "action": action,
                "estimated_tokens": estimated if action == "load_file" else 0,
                "why": [f"{rel['relation']} of {file_info['file']}"],
            })

    return {
        "budget_limit": budget_limit,
        "planned_tokens": used,
        "steps": plan,
        "strategy": "map -> scored files -> symbols/ranges -> import neighbors",
    }

def recommend_model(task_info, token_count):
    complexity = task_info["complexity"]
    op_type = task_info["op_type"]

    if op_type == "bugfix" and complexity == "low":
        model = "claude-haiku"
        reason = "Basit bugfix - ucuz model yeterli"
    elif complexity == "high" or op_type == "refactor":
        model = "claude-sonnet"
        reason = "Karmaşık iş - daha güçlü model önerilir"
    elif token_count > 20000:
        model = "gemini-flash"
        reason = "Büyük context - uzun context için ekonomik model önerilir"
    elif op_type in ("feature", "modify") and complexity == "medium":
        model = "claude-haiku"
        reason = "Orta karmaşıklık - önce ucuz model denenebilir"
    else:
        model = "claude-sonnet"
        reason = "Belirsiz iş - güvenli seçim"

    costs = {
        "claude-sonnet": 0.003,
        "claude-haiku": 0.00025,
        "gemini-flash": 0.0001,
        "ollama-local": 0.0,
    }

    return {
        "recommended_model": model,
        "reason": reason,
        "estimated_cost_usd": round(token_count / 1000 * costs.get(model, 0), 5),
        "alternatives": {
            "cheaper": "gemini-flash" if model != "gemini-flash" else "ollama-local",
            "better": "claude-sonnet" if model != "claude-sonnet" else None,
        },
    }

def suggested_commands_for(files):
    commands = []
    for file_info in files[:5]:
        if file_info.get("key_symbols"):
            for sym in file_info["key_symbols"][:2]:
                cmd = f"python .context/scripts/get_symbol.py {sym['name']}"
                if sym.get("type"):
                    cmd += f" --type {sym['type']}"
                if file_info.get("file"):
                    cmd += f" --file {file_info['file']}"
                commands.append(cmd)
        else:
            commands.append(f"python .context/scripts/get_range.py {file_info['file']} 1 120")
    return commands[:8]

def load_project_commands(context_dir):
    map_path = context_dir / "map.json"
    if not map_path.exists():
        return {}
    try:
        return json.loads(map_path.read_text(encoding="utf-8")).get("commands", {})
    except Exception:
        return {}

def calculate_confidence(relevant_files):
    if not relevant_files:
        return {
            "level": "low",
            "score": 0,
            "reason": "No relevant files found",
        }
    top = relevant_files[0].get("score", 0)
    runner_up = relevant_files[1].get("score", 0) if len(relevant_files) > 1 else 0
    has_symbol = any("symbol:" in r.get("reason", "") for r in relevant_files[0].get("reasons", []))
    has_related = bool(relevant_files[0].get("related_files"))
    raw_score = top + (8 if has_symbol else 0) + (4 if has_related else 0) + max(0, top - runner_up)
    score = min(100, raw_score)
    level = "high" if score >= 35 else "medium" if score >= 18 else "low"
    return {
        "level": level,
        "score": score,
        "reason": f"top_score={top}, symbol_match={has_symbol}, related_files={has_related}",
    }

def build_execution_plan(task_info, relevant_files, context_plan, commands):
    likely_edit_files = [
        f["file"] for f in relevant_files
        if not f["file"].lower().startswith(("test", "tests")) and f.get("score", 0) >= 5
    ][:5]
    read_order = [s["file"] for s in context_plan.get("steps", []) if s.get("action") != "defer"][:8]

    verify_commands = []
    if task_info["op_type"] == "test" and commands.get("test"):
        verify_commands.append(commands["test"])
    elif commands.get("test"):
        verify_commands.append(commands["test"])
    if task_info["op_type"] in ("refactor", "modify") and commands.get("lint"):
        verify_commands.append(commands["lint"])
    if commands.get("typecheck"):
        verify_commands.append(commands["typecheck"])

    risk_notes = []
    if any(f.get("related_files") for f in relevant_files[:3]):
        risk_notes.append("Primary files have dependency neighbors; inspect related_files before editing cross-cutting behavior.")
    if any(f.get("token_estimate", 0) > 1200 for f in relevant_files[:3]):
        risk_notes.append("Large file detected; context package may include focused symbols instead of full file.")
    if task_info["complexity"] == "high":
        risk_notes.append("High-complexity task; broaden context before large refactors.")

    return {
        "read_order": read_order,
        "likely_edit_files": likely_edit_files,
        "verify_commands": verify_commands[:4],
        "risk_notes": risk_notes,
    }

def generate_prompt_hint(task_info, files, context_plan):
    file_list = "\n".join(
        f"  - {f['file']} score={f['score']}: {f['summary']}"
        for f in files[:5]
    )
    plan_list = "\n".join(
        f"  - {s['action']} {s['file']} ({s['priority']})"
        for s in context_plan.get("steps", [])[:8]
    )
    return f"""Task type: {task_info['op_type']} | Complexity: {task_info['complexity']}
Domains: {', '.join(task_info['domains']) or 'general'}
Likely relevant files:
{file_list}
Context plan:
{plan_list}
Load only the planned files first; expand through related_files only if needed."""

def machine_readable_analysis(task_info, confidence):
    """Deterministic machine-readable classification for optional consumers.

    Additive output: the optional Router layer (§autoroute) consumes this
    block instead of re-classifying tasks. All fields derive from the
    existing deterministic analysis above; nothing here changes routing.
    """
    op_map = {
        "bugfix": "FIX", "feature": "NEW_FEATURE", "refactor": "REFACTOR",
        "modify": "QUICK_EDIT", "test": "TEST_GENERATION", "unknown": "UNKNOWN",
    }
    task_type = op_map.get(task_info.get("op_type", "unknown"), "UNKNOWN")
    domains = set(task_info.get("domains", []))
    keywords = set(task_info.get("keywords", []))
    security_terms = {"security", "auth", "crypto", "secret", "token",
                      "password", "permission", "vulnerability"}
    if domains & {"security"} or keywords & security_terms:
        risk = "HIGH"
    elif domains & {"db", "payment", "auth"} or task_info.get("complexity") == "high":
        risk = "MEDIUM"
    else:
        risk = "LOW"
    conf_level = (confidence or {}).get("level", "low")
    return {
        "source": "context_intelligence",
        "task_type": task_type,
        "complexity": task_info.get("complexity", "medium"),
        "risk": risk,
        "reasoning_requirement": "high" if task_info.get("complexity") == "high" else "normal",
        "context_confidence": conf_level,
        "context_confidence_score": (confidence or {}).get("score", 0),
        "safe_to_implement": conf_level in ("high", "medium") and task_type != "UNKNOWN",
        "domains": sorted(domains),
    }


def route(task_text, dry_run=False, budget_limit=8000, top_n=None):
    """Route a task to relevant files.

    Parameters
    ----------
    task_text:
        Natural-language description of the coding task.
    dry_run:
        If True, do not actually load file content; just produce the
        plan.
    budget_limit:
        Hard cap on how many tokens the produced context should use.
    top_n:
        Optional cap on how many files to surface in ``relevant_files``.
        Defaults to ``None`` (no cap), but the MCP server defaults this
        to a small number (3) so the LLM only sees the most relevant
        files rather than a noisy long tail.
    """
    context_dir, root = find_context_dir()
    if not context_dir:
        print(json.dumps({"error": "Index bulunamadı. Önce: python .context/scripts/index.py"}))
        sys.exit(1)

    conn = sqlite3.connect(str(context_dir / "symbols.db"), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    task_info = classify_task(task_text)
    relevant_files = find_relevant_files(task_info, conn)
    if top_n is not None and top_n > 0:
        relevant_files = relevant_files[:top_n]
    context_plan = build_context_plan(relevant_files, budget_limit=budget_limit)
    total_tokens = context_plan["planned_tokens"]
    model_rec = recommend_model(task_info, total_tokens)
    commands = load_project_commands(context_dir)
    confidence = calculate_confidence(relevant_files)
    execution_plan = build_execution_plan(task_info, relevant_files, context_plan, commands)

    result = {
        "task": task_text,
        "analysis": task_info,
        "machine_readable": machine_readable_analysis(task_info, confidence),
        "confidence": confidence,
        "relevant_files": relevant_files,
        "context_plan": context_plan,
        "execution_plan": execution_plan,
        "project_commands": commands,
        "total_context_tokens": total_tokens,
        "model_recommendation": model_rec,
        "suggested_first_commands": suggested_commands_for(relevant_files),
        "system_prompt_hint": generate_prompt_hint(task_info, relevant_files, context_plan),
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task", help="Ne yapılacak")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", help="Zorla model seç")
    parser.add_argument("--budget", type=int, default=8000)
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Cap how many files are surfaced in relevant_files. "
        "Lower values produce a less noisy context for the LLM. "
        "Default: no cap (the context plan still respects --budget).",
    )
    args = parser.parse_args()
    route(args.task, args.dry_run, args.budget, args.top)

if __name__ == "__main__":
    main()
