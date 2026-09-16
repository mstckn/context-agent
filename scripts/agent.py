#!/usr/bin/env python3
"""
agent.py — Context Agent ana orkestratörü.

LLM'in tek giriş noktası. Bir task verilince:
1. route.py ile ilgili dosyaları tespit eder
2. dedup.py ile daha önce yüklenenler çıkarılır
3. budget.py ile token bütçesi kontrol edilir
4. session.py ile bağlam eklenir
5. LLM'e gönderilecek hazır paketi üretir

Kullanım:
  python agent.py "Add rate limiting to login"
  python agent.py "Fix payment timeout" --budget 4000
  python agent.py "Refactor auth module" --model claude-sonnet
  python agent.py --status        # mevcut durum
  python agent.py --new-session "Sprint 12"
"""

import sys, json, subprocess, argparse, sqlite3, hashlib, os, re, importlib, io
from contextlib import redirect_stdout
from pathlib import Path
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HASH_CACHE = {}
_HASH_CACHE_MAX = 1024

def is_test_path(path):
    text = (path or "").replace("\\", "/").lower()
    name = Path(text).name
    return (
        text.startswith("test/")
        or text.startswith("tests/")
        or "/test/" in text
        or "/tests/" in text
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".spec." in name
        or ".test." in name
    )

def is_meaningful_test_path(path):
    if not is_test_path(path):
        return False
    name = Path((path or "").replace("\\", "/")).name.lower()
    return name not in {"__init__.py", "conftest.py"}

def find_root():
    try:
        from paths import find_project_root
    except ImportError:
        from scripts.paths import find_project_root
    return find_project_root()

def current_scope():
    try:
        from paths import resolve_scope
    except ImportError:
        from scripts.paths import resolve_scope
    return resolve_scope()


def current_runtime_scope():
    try:
        from paths import resolve_runtime_scope
    except ImportError:
        from scripts.paths import resolve_runtime_scope
    return resolve_runtime_scope()


# ── Branch isolation (GAP 3) ──────────────────────────────────────────

# Project-global memory types visible across all branches.
PROJECT_GLOBAL_TYPES = {"ARCHITECTURAL", "CONSTRAINT", "KNOWN_ISSUE"}

def branch_scope():
    """Return the branch-specific scope key for the current git branch."""
    try:
        try:
            from identity import scope_key_for
        except ImportError:
            from scripts.identity import scope_key_for
        return scope_key_for("BRANCH", find_root())
    except Exception:
        return current_scope()

def project_global_scope():
    """Return the project-global scope key (branch-independent)."""
    try:
        try:
            from identity import scope_key_for
        except ImportError:
            from scripts.identity import scope_key_for
        return scope_key_for("PROJECT_GLOBAL", find_root())
    except Exception:
        return current_scope()

def memory_scope_for(memory_type: str) -> str:
    """Return the appropriate scope for a memory type:
    project-global for ARCHITECTURAL/CONSTRAINT/KNOWN_ISSUE,
    branch-specific for everything else."""
    if str(memory_type or "").upper() in PROJECT_GLOBAL_TYPES:
        return project_global_scope()
    return branch_scope()

def run_script(script_name, *args):
    """Lokal script çalıştır, JSON çıktı döndür"""
    root = find_root()
    script = root / ".context" / "scripts" / f"{script_name}.py"

    if not script.exists():
        return {"error": f"Script bulunamadı: {script}"}

    cmd = [sys.executable, str(script)] + [str(a) for a in args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(root),
            timeout=15,
        )
        if result.stdout.strip():
            return json.loads(result.stdout)
        return {"error": result.stderr[:200] if result.stderr else "Çıktı yok"}
    except json.JSONDecodeError:
        return {"raw": result.stdout[:500]}
    except subprocess.TimeoutExpired:
        return {"error": "Script zaman aşımı"}
    except Exception as e:
        return {"error": str(e)}


def call_component(module_name, function_name, *args, **kwargs):
    """Call an installed Context component in-process and normalize JSON output.

    The old build path spawned a new Python interpreter for every budget,
    dedup, capsule and range operation. A normal 8-10 file package therefore
    launched 70-84 child processes. These component APIs are already
    import-safe, so keep their public behavior while removing process startup
    from the hot path.
    """
    try:
        module = importlib.import_module(module_name)
        function = getattr(module, function_name)
        stream = io.StringIO()
        with redirect_stdout(stream):
            value = function(*args, **kwargs)
        if isinstance(value, dict):
            return value
        text = stream.getvalue().strip().lstrip("\ufeff")
        if text:
            return json.loads(text)
        return {} if value is None else value
    except Exception as exc:
        return {"error": f"{module_name}.{function_name} failed: {exc}"}


def component_get_symbol(name, sym_type=None, file_path=None):
    return call_component(
        "get_symbol", "get_symbol", name, False,
        sym_type=sym_type, file_path=file_path,
    )

def file_hash(path):
    try:
        file_path = Path(path)
        stat = file_path.stat()
        key = str(file_path.resolve())
        cached = _HASH_CACHE.get(key)
        if cached and cached.get("mtime_ns") == stat.st_mtime_ns and cached.get("size") == stat.st_size:
            return cached.get("hash", "")

        digest = hashlib.md5(file_path.read_bytes()).hexdigest()
        _HASH_CACHE[key] = {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "hash": digest,
        }
        if len(_HASH_CACHE) > _HASH_CACHE_MAX:
            # Simple cap to avoid unbounded memory growth in long MCP sessions.
            _HASH_CACHE.pop(next(iter(_HASH_CACHE)))
        return digest
    except Exception:
        return ""

def ensure_fresh_file(filepath):
    """Disk değişmişse tek dosyayı yeniden indexle."""
    root = find_root()
    db_path = root / ".context" / "symbols.db"
    full_path = root / filepath
    if not db_path.exists() or not full_path.exists():
        return {"fresh": False, "reason": "missing_db_or_file"}

    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        row = conn.execute("SELECT hash,indexed_at FROM files WHERE path=?", (filepath,)).fetchone()
        current_hash = file_hash(full_path)
        if row and row[0] == current_hash:
            return {"fresh": True, "hash": current_hash, "indexed_at": row[1]}
        result = run_script("index", "--file", filepath)
        return {
            "fresh": True,
            "hash": current_hash,
            "reindexed": True,
            "index_result": result,
        }
    except Exception as exc:
        return {"fresh": False, "reason": str(exc)}

def planned_load_items(route_result):
    """Route context_plan varsa onu kullan, yoksa eski relevant_files akışına dön."""
    files_by_path = {
        item.get("file"): item
        for item in route_result.get("relevant_files", [])
        if item.get("file")
    }
    for item in route_result.get("relevant_files", []):
        for related in item.get("related_files", []):
            if related.get("file") and related["file"] not in files_by_path:
                files_by_path[related["file"]] = related

    steps = route_result.get("context_plan", {}).get("steps", [])
    if not steps:
        return [
            {
                "file": f.get("file"),
                "summary": f.get("summary", ""),
                "token_estimate": f.get("token_estimate", 200),
                "priority": "primary",
                "action": "load_file",
                "why": [],
            }
            for f in route_result.get("relevant_files", [])[:4]
        ]

    items = []
    for step in steps:
        if step.get("action") == "defer":
            continue
        filepath = step.get("file")
        meta = files_by_path.get(filepath, {})
        items.append({
            "file": filepath,
            "summary": meta.get("summary", ""),
            "token_estimate": step.get("estimated_tokens") or meta.get("token_estimate", 200),
            "priority": step.get("priority", "primary"),
            "action": step.get("action", "load_file"),
            "why": step.get("why", []),
            "key_symbols": meta.get("focus_symbols") or meta.get("key_symbols", []),
            "focused_by_query": bool(meta.get("focused_by_query")),
        })
    return items

def _boost_git_changed(relevant_files, changed, task_keywords=None):
    """Dirty dosyalar retrieval listesine eklenir (spec PHASE 13).

    Aktif kodlama görevlerinde üzerinde çalışılan dosya, eski repo-arası
    aramadan daha değerli bir sinyaldir. En fazla 3 dosya eklenir ve
    primary itemlardan HEMEN SONRA yerleştirilir; böylece [:N] diliminde
    secondary kuyruğunun arkasında kaybolmazlar.
    """
    if not changed:
        return relevant_files
    have = set()
    for f in relevant_files:
        p = str(f.get("file") or "").replace("\\", "/")
        if p:
            have.add(p)
    keyword_set = {str(k).lower() for k in (task_keywords or []) if str(k)}
    additions = []
    for ch in changed:
        rel = str(ch.get("path", "")).replace("\\", "/")
        if not rel or rel in have or rel.endswith("/"):
            continue  # dizin girdileri dosya olarak yüklenemez
        low = rel.lower()
        if (low.startswith(".context/") or "/.context/" in low
                or "/__pycache__/" in f"/{low}"
                or "/.pytest_cache/" in f"/{low}"):
            continue  # motorun kendi durumu sinyal değil
        if low.endswith((".pyc", ".log", ".lock", ".min.js")) or "/node_modules/" in rel:
            continue
        if relevant_files and keyword_set:
            rel_tokens = set(re.findall(r"[a-z0-9]+", low))
            if not (rel_tokens & keyword_set):
                continue
        additions.append({
            "file": rel,
            "summary": "Uncommitted change — active work signal",
            "token_estimate": 200,
            "priority": "secondary",
            "action": "load_file",
            "why": [f"git_dirty:{ch.get('status', '?')}"],
            "key_symbols": [],
        })
        have.add(rel)
        if len(additions) >= 3:
            break
    if not additions:
        return relevant_files
    # Primary bloğunun hemen arkasına ekle (öncelik sırası korunur).
    insert_at = 0
    for i, f in enumerate(relevant_files):
        if f.get("priority") == "primary":
            insert_at = i + 1
    return list(relevant_files[:insert_at]) + additions + list(relevant_files[insert_at:])

# Bu eşiğin altındaki dosyalarda iskelet üretmek anlamsız — tüm dosya zaten ucuz.
SKELETON_MIN_TOKENS = 150
# Geniş, dil-bağımsız import/use satırı yakalayıcı (provenance için).
_IMPORT_LINE_RE = re.compile(
    r"^\s*(import\s|from\s.+\simport|#include|using\s|use\s|require\s*\(|"
    r"const\s+\w+\s*=\s*require|export\s+\*|export\s+\{|package\s)",
    re.IGNORECASE,
)


def _symbols_db():
    return find_root() / ".context" / "symbols.db"


# ── Cross-turn context ledger (Phase 1) ─────────────────────────────────
# Modelin daha önce NE gördüğünü kalıcı olarak hatırlar; değişmemiş
# içerik yeniden gönderilmez, decay ile pencereden düşenler yenilenir.

ACTION_TO_REPR = {
    "load_file": "full",
    "load_outline": "outline",
    "load_symbols": "symbols",
}


def _open_ledger():
    """In-process ledger (subprocess yok → gecikme yok). Hata durumunda None."""
    try:
        try:
            import ledger as ledger_mod
        except ImportError:
            from scripts import ledger as ledger_mod
        return ledger_mod.ContextLedger(_symbols_db())
    except Exception:
        return None


def ledger_session_id():
    """Konuşma kimliği: env > aktif session (current.json) > scope fallback."""
    explicit = os.environ.get("CONTEXT_AGENT_SESSION", "").strip()
    if explicit:
        return explicit
    try:
        sessions_root = find_root() / ".context" / "sessions"
        candidates = sorted(
            sessions_root.rglob("current.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for f in candidates:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            started = (data.get("started_at") or "").strip()
            if started:
                return started
    except Exception:
        pass
    return f"scope:{current_scope()}"


def _importance_for(file_info):
    """Decay ağırlığı: primary materyal daha yavaş unutulur."""
    return "important" if file_info.get("priority") == "primary" else "normal"


def _load_file_item(file_info, *, raw, ledger, scope, session_id, task,
                    client="", model="", verbose=False):
    """Tek dosyayı freshness → ledger → dedup → bütçe kapılarından geçirip yükler.

    Ana yükleme döngüsü VE otomatik expansion aynı boru hattını kullanır.

    Dönüş: (kind, context_item, known_entry, symbol_entry)
      kind: "loaded" | "known" (modelde zaten var) | "skip" (bütçe/dedup)
    """
    filepath = file_info["file"]
    # Rol-katmanlı politika: non-primary dosyaları iskelete indir.
    # raw modda atlanır → her dosya tam gövde (sadakat).
    if not raw:
        file_info["action"] = tier_action(file_info)
    if file_info["action"] == "load_outline":
        # İskelet için küçük planlama tahmini (gerçek değeri build sonrası hesaplanır).
        file_info["token_estimate"] = min(int(file_info.get("token_estimate", 220) or 220), 220)
    freshness = ensure_fresh_file(filepath)
    file_hash_value = freshness.get("hash", "") if isinstance(freshness, dict) else ""

    # Ledger kontrolü: model bu dosyayı zaten (değişmemiş haliyle) gördüyse
    # içeriği YENİDEN GÖNDERME; sadece ucuz bir "biliniyor" işaretçisi koy.
    # raw modda sadakat önceliklidir → atlama yok.
    ledger_status = None
    if ledger is not None and not raw:
        requested_repr = ACTION_TO_REPR.get(file_info.get("action", "load_file"), "full")
        try:
            ledger_status = ledger.status(scope, session_id, filepath,
                                          requested=requested_repr,
                                          new_hash=file_hash_value)
        except Exception:
            ledger_status = None
        if ledger_status and ledger_status["decision"] == "skip":
            known = ledger_status.get("known") or {}
            known_entry = {
                "file": filepath,
                "action": "known_to_model",
                "state": ledger_status.get("state", ""),
                "reason": ledger_status.get("reason", ""),
                "content_hash": file_hash_value or known.get("content_hash", ""),
                "representation": known.get("representation", requested_repr),
                "turn_sent": known.get("turn"),
                "token_saved": known.get("token_cost", 0),
                "expand_commands": [
                    f"python .context/scripts/get_range.py {filepath}",
                    f"python .context/scripts/get_related.py {filepath}",
                ],
            }
            if verbose:
                print(f"  -> {filepath} modelde zaten var "
                      f"({ledger_status.get('state')}), yeniden gönderilmedi",
                      file=sys.stderr)
            return "known", None, known_entry, None

    dedup_key = dedup_key_for(file_info, freshness)

    # Dedup kontrolü (build içi çift yükleme koruması)
    dedup_check = call_component("dedup", "check_context", dedup_key)
    if dedup_check.get("exists"):
        if verbose:
            print(f"  -> {filepath} zaten yüklü (dedup), atlandı", file=sys.stderr)
        return "skip", {"file": filepath, "reason": "duplicate_in_build"}, None, None

    # Bütçe kontrolü (action spesifik token üzerinden)
    token_est = file_info.get("token_estimate", 200)
    budget_status = call_component("budget", "status")
    remaining = budget_status.get("remaining", 0)

    if token_est > remaining:
        # Sığmıyorsa load_file yerine load_symbols ile kurtarmayı dene
        if file_info.get("action") == "load_file":
            file_info["action"] = "load_symbols"
            token_est = 250  # Kaba bir symbol load tahmini
            if token_est > remaining:
                if verbose:
                    print(f"  -> {filepath} sembolleri bile bütçeye sığmıyor, atlandı",
                          file=sys.stderr)
                return "skip", {"file": filepath,
                                "reason": f"budget_exhausted (remaining {remaining})"}, None, None
        else:
            if verbose:
                print(f"  -> {filepath} bütçeye sığmıyor, atlandı", file=sys.stderr)
            return "skip", {"file": filepath,
                            "reason": f"budget_exhausted (remaining {remaining})"}, None, None

    # Dedup'a ekle
    call_component("dedup", "add_to_context", dedup_key, dedup_key,
                   filepath, int(token_est))
    call_component("budget", "spend", int(token_est), f"file: {filepath}")
    context_item = build_context_item(file_info)
    context_item["dedup_key"] = dedup_key
    context_item["freshness"] = freshness
    if ledger_status and ledger_status["decision"] == "refresh":
        context_item["ledger_refresh"] = {
            "state": ledger_status.get("state", ""),
            "reason": ledger_status.get("reason", ""),
        }

    # Ledger'a kaydet: model bu temsili şimdi görmüş olacak.
    if ledger is not None:
        actual_repr = ACTION_TO_REPR.get(file_info.get("action", "load_file"), "full")
        if context_item.get("mode") == "preview":
            actual_repr = "range"  # büyük dosya: sadece baş taraf gönderildi
        try:
            ledger.record(
                scope, session_id, filepath, actual_repr,
                content_hash=file_hash_value,
                token_cost=context_item.get("token_estimate") or token_est,
                importance=_importance_for(file_info),
                task=task, client=client, model=model,
            )
        except Exception:
            pass

    symbol_entry = {
        "file": filepath,
        "summary": file_info.get("summary", ""),
        "tokens": context_item.get("token_estimate") or token_est,
        "priority": file_info.get("priority", "primary"),
        "action": file_info.get("action", "load_file"),
        "why": file_info.get("why", [])
    }
    return "loaded", context_item, None, symbol_entry


def _known_pseudo_item(known_entry):
    """known_to_model kaydı → kalite hesabında efektif bağlam olarak sayılır."""
    return {
        "file": known_entry["file"],
        "action": "known_to_model",
        "priority": "known",
        "content": "(already known to model — unchanged)",
        "freshness": {"fresh": True},
        "symbols": [],
        "why": [f"known_to_model ({known_entry.get('state', '')})"],
    }


def file_outline_text(filepath, max_symbols=40):
    """Bir dosyanın iskeletini (imzalar + docstring özeti) indeksten üret.

    Gövdeleri DEĞİL, sözleşmeyi gönderir: bağımlılık dosyalarında LLM'in
    ihtiyacı budur. 2000 token'lık bir dosya ~200 token'a iner."""
    db_path = _symbols_db()
    if not db_path.exists():
        return "", 0
    posix = (filepath or "").replace("\\", "/")
    rows = []
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        rows = conn.execute(
            """
            SELECT name, type, signature, docstring
            FROM symbols
            WHERE file=? OR file=?
            ORDER BY start_line
            LIMIT ?
            """,
            (filepath, posix, max_symbols),
        ).fetchall()
    except Exception:
        return "", 0

    lines = []
    for name, stype, signature, docstring in rows:
        head = (signature or "").strip() or f"{stype or 'symbol'} {name}"
        head = head.splitlines()[0][:160] if head else head
        doc1 = ""
        if docstring:
            doc_lines = [ln.strip() for ln in str(docstring).splitlines() if ln.strip()]
            if doc_lines:
                doc1 = doc_lines[0][:80]
        lines.append(f"{head}" + (f"  # {doc1}" if doc1 else ""))

    outline = "\n".join(lines)
    return outline, len(rows)


def _imports_preview(filepath, max_lines=12, scan_lines=60):
    """Dosya başından import/use satırlarını çek (anti-hallüsinasyon, ucuz)."""
    root = find_root()
    full_path = root / filepath
    if not full_path.exists():
        return ""
    try:
        head = full_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()[:scan_lines]
    except Exception:
        return ""
    picked = [ln for ln in head if _IMPORT_LINE_RE.match(ln)]
    return "\n".join(picked[:max_lines])


def tier_action(file_info):
    """Rol-katmanlı politika: primary dosya tam gövde; non-primary dosya
    (related/dependency) varsayılan olarak iskelet (load_outline) → token tasarrufu.

    Route açıkça load_symbols/defer dediyse ona saygı duy; minik dosyaları
    iskeletlemek anlamsız olduğundan tam yükle."""
    action = file_info.get("action", "load_file")
    if action != "load_file":
        return action
    priority = file_info.get("priority", "primary")
    if priority == "primary":
        return "load_file"
    est = int(file_info.get("token_estimate", 200) or 200)
    if est <= SKELETON_MIN_TOKENS:
        return "load_file"
    return "load_outline"


def build_context_item(file_info):
    """Plan adımını LLM'e gönderilecek gerçek içerik parçasına çevir."""
    filepath = file_info["file"]
    action = file_info.get("action", "load_file")

    if action == "load_outline":
        outline, sym_count = file_outline_text(filepath)
        imports_preview = _imports_preview(filepath)
        if not outline and not imports_preview:
            # İskelet üretilemedi (sembol yok) → küçük bir preview'a düş.
            action = "load_file"
        else:
            parts = []
            if imports_preview:
                parts.append(imports_preview)
            if outline:
                parts.append(f"# --- outline ({sym_count} symbols; bodies omitted) ---")
                parts.append(outline)
            content = "\n".join(parts)
            return {
                "file": filepath,
                "freshness": ensure_fresh_file(filepath),
                "priority": file_info.get("priority", "related"),
                "action": "load_outline",
                "why": file_info.get("why", []),
                "reason": "; ".join(file_info.get("why", [])[:3]),
                "summary": file_info.get("summary", ""),
                "mode": "outline",
                "note": "Skeleton (signatures + docstrings). Use expand_commands for full bodies.",
                "content": content,
                "symbols": [],
                "token_estimate": int(len(content.split()) * 1.3),
                "expand_commands": [
                    f"python .context/scripts/get_range.py {filepath} 1",
                    f"python .context/scripts/get_related.py {filepath}",
                ],
            }

    item = {
        "file": filepath,
        "freshness": ensure_fresh_file(filepath),
        "priority": file_info.get("priority", "primary"),
        "action": action,
        "why": file_info.get("why", []),
        "reason": "; ".join(file_info.get("why", [])[:3]),
        "summary": file_info.get("summary", ""),
        "content": "",
        "symbols": [],
        "token_estimate": 0,
        "expand_commands": [
            f"python .context/scripts/get_range.py {filepath}",
            f"python .context/scripts/get_related.py {filepath}",
        ],
    }

    if action == "load_symbols":
        imports_result = call_component("get_range", "get_range", filepath, 1, 25)
        if "content" in imports_result:
            item["imports_context"] = imports_result.get("content", "")

        for sym in file_info.get("key_symbols", [])[:5]:
            sym_result = component_get_symbol(
                sym.get("name", ""), sym.get("type"), filepath)
            if "content" not in sym_result and isinstance(sym_result.get("results"), list):
                target = filepath.replace("\\", "/")
                for candidate in sym_result["results"]:
                    if str(candidate.get("file", "")).replace("\\", "/") == target:
                        sym_result = candidate
                        break
            if "content" in sym_result:
                item["symbols"].append({
                    "name": sym_result.get("name"),
                    "type": sym_result.get("type"),
                    "signature": sym_result.get("signature"),
                    "lines": sym_result.get("lines"),
                    "content": sym_result.get("content", ""),
                    "token_estimate": sym_result.get("token_estimate", 0),
                })
        item["token_estimate"] = sum(s.get("token_estimate", 0) for s in item["symbols"])
        item["token_estimate"] += imports_result.get("token_estimate", 0)
        return item

    range_result = call_component("get_range", "get_range", filepath)
    if "content" in range_result:
        item["content"] = range_result.get("content", "")
        item["token_estimate"] = range_result.get("token_estimate", 0)
        if range_result.get("mode") == "preview":
            item["mode"] = "preview"
            item["note"] = range_result.get("note", "")
            if range_result.get("chunk_map"):
                item["chunk_map"] = range_result["chunk_map"]
            item["expand_commands"].insert(0, range_result.get("get_full", ""))
            # A query-matched symbol in a large file is more useful than the
            # file header. Pull its actual body while preserving the chunk map
            # for further progressive expansion.
            if file_info.get("focused_by_query") and file_info.get("key_symbols"):
                focus = file_info["key_symbols"][0]
                symbol_result = component_get_symbol(
                    focus.get("name", ""), focus.get("type"), filepath)
                if symbol_result.get("content"):
                    item["content"] = symbol_result["content"]
                    item["token_estimate"] = symbol_result.get(
                        "token_estimate", item["token_estimate"])
                    item["mode"] = "focused_symbol"
                    item["focused_symbol"] = {
                        "name": symbol_result.get("name") or focus.get("name"),
                        "type": symbol_result.get("type") or focus.get("type"),
                        "lines": symbol_result.get("lines"),
                    }
    elif "error" in range_result:
        item["error"] = range_result["error"]

    return item

def dedup_key_for(file_info, freshness):
    """Task-context dedup key: file + mode + focused symbols + file hash."""
    symbols = ",".join(sym.get("name", "") for sym in file_info.get("key_symbols", [])[:8])
    file_hash_value = freshness.get("hash", "") if isinstance(freshness, dict) else ""
    client = os.environ.get("CONTEXT_AGENT_CLIENT", "direct")
    scope = current_scope()
    raw = "|".join([
        scope,
        client,
        file_info.get("file", ""),
        file_info.get("action", "load_file"),
        symbols,
        file_hash_value,
    ])
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()

def load_file_into_context(file_info, loaded_symbols, context_items, verbose=False, raw=False):
    filepath = file_info.get("file", "")
    if not filepath:
        return {"status": "skipped", "reason": "missing_file"}

    # Rol-katmanlı politika: strict aday'lar da non-primary ise iskelete iner.
    # raw modda atlanır.
    if not raw:
        file_info["action"] = tier_action(file_info)
    if file_info["action"] == "load_outline":
        file_info["token_estimate"] = min(int(file_info.get("token_estimate", 220) or 220), 220)

    freshness = ensure_fresh_file(filepath)
    dedup_key = dedup_key_for(file_info, freshness)

    dedup_check = call_component("dedup", "check_context", dedup_key)
    if dedup_check.get("exists"):
        if verbose:
            print(f"  -> {filepath} zaten yüklü (dedup), atlandı", file=sys.stderr)
        return {"status": "skipped", "reason": "dedup", "file": filepath}

    token_est = int(file_info.get("token_estimate", 200) or 200)
    budget_status = call_component("budget", "status")
    remaining = budget_status.get("remaining", 0)

    if token_est > remaining:
        if file_info.get("action") == "load_file":
            file_info["action"] = "load_symbols"
            token_est = 250
            if token_est > remaining:
                if verbose:
                    print(f"  -> {filepath} sembolleri bile bütçeye sığmıyor, atlandı", file=sys.stderr)
                return {"status": "skipped", "reason": "budget", "file": filepath}
        else:
            if verbose:
                print(f"  -> {filepath} bütçeye sığmıyor, atlandı", file=sys.stderr)
            return {"status": "skipped", "reason": "budget", "file": filepath}

    call_component("dedup", "add_to_context", dedup_key, dedup_key,
                   filepath, int(token_est))
    call_component("budget", "spend", int(token_est), f"file: {filepath}")
    context_item = build_context_item(file_info)
    context_item["dedup_key"] = dedup_key
    context_item["freshness"] = freshness

    loaded_symbols.append({
        "file": filepath,
        "summary": file_info.get("summary", ""),
        "tokens": context_item.get("token_estimate") or token_est,
        "priority": file_info.get("priority", "primary"),
        "action": file_info.get("action", "load_file"),
        "why": file_info.get("why", []),
    })
    context_items.append(context_item)
    return {"status": "loaded", "file": filepath, "tokens": context_item.get("token_estimate") or token_est}

def context_quality_score(route_result, context_items):
    confidence = route_result.get("confidence", {})
    level = confidence.get("level", "low")
    score = {"high": 40, "medium": 25, "low": 10}.get(level, 10)
    formula = {
        "confidence": {"high": 40, "medium": 25, "low": 10}.get(level, 10),
        "has_tests": 15,
        "has_dependencies": 15,
        "has_focus_symbols": 10,
        "all_fresh": 10,
        "has_content": 10,
    }

    has_tests = any(is_meaningful_test_path(item.get("file") or "") for item in context_items)
    has_dependencies_from_route = any(
        rel
        for file_info in route_result.get("relevant_files", [])[:3]
        for rel in file_info.get("related_files", [])
    )
    has_dependencies_from_items = any(
        item.get("priority") in ("related", "strict-related")
        or any(
            marker in (reason or "").lower()
            for reason in item.get("why", [])
            for marker in ("imports", "imported_by", "dependency", "related")
        )
        for item in context_items
    )
    has_dependencies = has_dependencies_from_route or has_dependencies_from_items
    has_focus_symbols = any(item.get("symbols") for item in context_items)
    all_fresh = all(item.get("freshness", {}).get("fresh") for item in context_items) if context_items else False
    has_content = all(bool(item.get("content") or item.get("symbols")) for item in context_items) if context_items else False

    if has_tests:
        score += 15
    if has_dependencies:
        score += 15
    if has_focus_symbols:
        score += 10
    if all_fresh:
        score += 10
    if has_content:
        score += 10

    score = min(score, 100)
    risk = "low" if score >= 75 else "medium" if score >= 45 else "high"
    return {
        "score": score,
        "risk": risk,
        "confidence": level,
        "has_tests": has_tests,
        "has_dependencies": has_dependencies,
        "has_focus_symbols": has_focus_symbols,
        "all_fresh": all_fresh,
        "has_content": has_content,
        "formula": formula,
        "explanation": "score = confidence base + present signal weights, capped at 100",
    }

def discover_nearby_test_files(base_files, limit=5):
    root = find_root()
    db_path = root / ".context" / "symbols.db"
    if not db_path.exists():
        return []

    candidates = []
    seen = set()
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        for file_path in base_files:
            stem = Path(file_path).stem.lower()
            if not stem:
                continue
            rows = conn.execute(
                """
                SELECT path, summary, token_estimate
                FROM files
                WHERE (lower(path) LIKE '%test%' OR lower(path) LIKE '%spec%')
                  AND (lower(path) LIKE ? OR lower(summary) LIKE ?)
                ORDER BY COALESCE(token_estimate, 0) ASC
                LIMIT 6
                """,
                (f"%{stem}%", f"%{stem}%"),
            ).fetchall()
            for path, summary, token_est in rows:
                if not is_meaningful_test_path(path):
                    continue
                if path in seen:
                    continue
                seen.add(path)
                candidates.append({
                    "file": path,
                    "summary": summary or "",
                    "token_estimate": int(token_est or 250),
                    "priority": "strict-test",
                    "action": "load_file" if int(token_est or 250) <= 1200 else "load_symbols",
                    "why": [f"strict quality: nearby test for {file_path}"],
                    "key_symbols": [],
                })
                if len(candidates) >= limit:
                    return candidates
    except Exception:
        return candidates
    return candidates

def discover_companion_files(base_files, keywords, limit=4):
    root = find_root()
    db_path = root / ".context" / "symbols.db"
    if not db_path.exists():
        return []

    candidates = []
    seen = set()
    words = [w.lower() for w in (keywords or []) if isinstance(w, str)]
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        for file_path in base_files:
            normalized = file_path.replace("\\", "/")
            folder = Path(normalized).parent.as_posix()
            if folder in (".", ""):
                path_like_posix = "%"
                path_like_win = "%"
            else:
                path_like_posix = folder.rstrip("/") + "/%"
                path_like_win = folder.rstrip("/").replace("/", "\\") + "\\%"
            rows = conn.execute(
                """
                SELECT path, summary, token_estimate
                FROM files
                WHERE (path LIKE ? OR path LIKE ?) AND path != ?
                LIMIT 40
                """,
                (path_like_posix, path_like_win, file_path),
            ).fetchall()
            ranked = []
            for path, summary, token_est in rows:
                if path in seen or is_test_path(path):
                    continue
                text = f"{path} {summary or ''}".lower()
                keyword_hits = sum(1 for w in words[:8] if w in text)
                ranked.append((keyword_hits, int(token_est or 260), path, summary or ""))
            ranked.sort(key=lambda row: (-row[0], row[1], row[2]))
            for _, token_est, path, summary in ranked[:3]:
                if path in seen:
                    continue
                seen.add(path)
                candidates.append({
                    "file": path,
                    "summary": summary,
                    "token_estimate": token_est,
                    "priority": "strict-related",
                    "action": "load_file" if token_est <= 1200 else "load_symbols",
                    "why": [f"strict quality: companion file near {file_path}"],
                    "key_symbols": [],
                })
                if len(candidates) >= limit:
                    return candidates
    except Exception:
        return candidates
    return candidates

def strict_quality_candidates(route_result, context_items):
    loaded = {item.get("file") for item in context_items if item.get("file")}
    candidates = []
    seen = set(loaded)
    relevant = route_result.get("relevant_files", []) if isinstance(route_result, dict) else []
    primary_files = [f.get("file") for f in relevant[:4] if f.get("file")]

    for file_info in relevant[:4]:
        for rel in file_info.get("related_files", [])[:4]:
            rel_file = rel.get("file")
            if not rel_file or rel_file in seen:
                continue
            seen.add(rel_file)
            candidates.append({
                "file": rel_file,
                "summary": rel.get("summary", ""),
                "token_estimate": int(rel.get("token_estimate", 220) or 220),
                "priority": "strict-related",
                "action": "load_file" if int(rel.get("token_estimate", 220) or 220) <= 1200 else "load_symbols",
                "why": [f"strict quality: {rel.get('relation', 'related')} of {file_info.get('file', '')}"],
                "key_symbols": [],
            })

    for test_item in discover_nearby_test_files(primary_files, limit=6):
        if test_item.get("file") in seen:
            continue
        seen.add(test_item["file"])
        candidates.append(test_item)

    for item in discover_companion_files(primary_files, route_result.get("analysis", {}).get("keywords", []), limit=6):
        if item.get("file") in seen:
            continue
        seen.add(item["file"])
        candidates.append(item)

    candidates.sort(
        key=lambda item: (
            0 if is_meaningful_test_path(item.get("file", "")) else 1,
            int(item.get("token_estimate", 0) or 0),
        )
    )
    return candidates

def apply_strict_quality(route_result, loaded_symbols, context_items, min_quality=80, max_extra_items=4, verbose=False, raw=False):
    info = {
        "enabled": True,
        "target": int(min_quality),
        "max_extra_items": int(max_extra_items),
        "attempted": 0,
        "loaded": 0,
        "initial_score": 0,
        "final_score": 0,
        "reached": False,
        "actions": [],
    }
    quality = context_quality_score(route_result, context_items)
    info["initial_score"] = quality.get("score", 0)

    if quality.get("score", 0) >= min_quality:
        info["final_score"] = quality.get("score", 0)
        info["reached"] = True
        info["note"] = "already_above_target"
        return quality, info

    candidates = strict_quality_candidates(route_result, context_items)
    if not candidates:
        info["final_score"] = quality.get("score", 0)
        info["reached"] = False
        info["note"] = "no_additional_candidates"
        return quality, info

    if verbose:
        print(f"  -> strict quality mode: hedef {min_quality}, aday={len(candidates)}", file=sys.stderr)

    for file_info in candidates:
        if info["loaded"] >= max_extra_items:
            break
        info["attempted"] += 1
        result = load_file_into_context(file_info, loaded_symbols, context_items, verbose=verbose, raw=raw)
        action = {
            "file": file_info.get("file"),
            "status": result.get("status"),
            "reason": result.get("reason", ""),
            "priority": file_info.get("priority"),
        }
        if result.get("status") == "loaded":
            info["loaded"] += 1
            action["tokens"] = result.get("tokens", 0)
        info["actions"].append(action)

        quality = context_quality_score(route_result, context_items)
        if quality.get("score", 0) >= min_quality:
            break

    info["final_score"] = quality.get("score", 0)
    info["reached"] = quality.get("score", 0) >= min_quality
    return quality, info

def _fallback_search_items(task, budget_limit=8000):
    """route.py çalışmadığında sembol DB'den doğrudan arama yaparak fallback context üretir."""
    root = find_root()
    db_path = root / ".context" / "symbols.db"
    if not db_path.exists():
        return []

    import re as _re

    stopwords = {
        "the", "and", "for", "with", "from", "that", "this", "into",
        "bir", "ile", "icin", "için", "gibi", "olan",
        "add", "fix", "update", "change", "create", "implement", "refactor",
    }
    words = [
        w for w in _re.findall(r"[A-Za-z_][A-Za-z0-9_]+", task.lower())
        if len(w) >= 3 and w not in stopwords
    ][:12]
    if not words:
        return []

    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        scores: dict = {}
        for kw in words:
            rows = conn.execute(
                "SELECT path FROM files WHERE lower(path) LIKE ? OR lower(summary) LIKE ? LIMIT 20",
                (f"%{kw}%", f"%{kw}%"),
            ).fetchall()
            for (path,) in rows:
                scores[path] = scores.get(path, 0) + 6
            sym_rows = conn.execute(
                "SELECT file FROM symbols WHERE lower(name) LIKE ? LIMIT 20",
                (f"%{kw}%",),
            ).fetchall()
            for (path,) in sym_rows:
                scores[path] = scores.get(path, 0) + 8

        ranked = sorted(scores.items(), key=lambda x: -x[1])[:8]
        items = []
        used = 0
        for path, _ in ranked:
            row = conn.execute(
                "SELECT summary, token_estimate FROM files WHERE path=?", (path,)
            ).fetchone()
            if not row:
                continue
            summary, tok = row
            tok = int(tok or 400)
            if used + tok > budget_limit:
                continue
            used += tok
            items.append({
                "file": path,
                "summary": summary or "",
                "token_estimate": tok,
                "priority": "primary",
                "action": "load_file" if tok <= 1200 else "load_symbols",
                "why": [f"fallback keyword match in task: {task[:60]}"],
                "key_symbols": [],
            })
        return items
    except Exception:
        return []


def select_task_directives(route_result, context_items, budget_tokens=1200, raw=False):
    """Routed analizi ve yüklü dosyaları kullanarak göreve uygun direktifleri seç.

    directives.py varsa (yeni kurulum) onu kullanır; yoksa sessizce boş döner
    (eski kurulumlarla geriye dönük uyumluluk)."""
    try:
        import directives as directives_mod
    except Exception:
        return None

    analysis = route_result.get("analysis", {}) if isinstance(route_result, dict) else {}
    files = [item.get("file") for item in context_items if item.get("file")]
    # context_items azsa routed adayları da glob eşleşmesine kat.
    for f in route_result.get("relevant_files", []) if isinstance(route_result, dict) else []:
        if f.get("file") and f["file"] not in files:
            files.append(f["file"])

    try:
        selection = directives_mod.select_directives(
            find_root(),
            op_type=analysis.get("op_type"),
            domains=analysis.get("domains", []),
            keywords=analysis.get("keywords", []),
            files=files,
            budget_tokens=budget_tokens,
        )
    except Exception as exc:
        return {"items": [], "error": str(exc)}

    # raw modda sabit-dedup atlanır → tüm direktifler tam gelir (sadakat).
    if raw:
        return selection
    return _dedup_stable_directives(selection)


def _stable_marker_path(scope):
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(scope or "default")).strip("-")[:80] or "default"
    return find_root() / ".context" / "sessions" / f"stable_{safe}.json"


def _dedup_stable_directives(selection):
    """Always (proje-global) direktifleri scope (oturum) içinde bir kez teslim et.

    İlk çağrıda gönderilir (kalite garantisi); sonraki çağrılarda sadece id ile
    referans verilir → tekrar token maliyeti kesilir. Dosya dedup'ıyla aynı felsefe.
    Cache'lenebilir `context-agent://rules` resource'u zaten kalıcı kopyayı taşır."""
    if os.environ.get("CONTEXT_AGENT_STABLE_DEDUP", "1") in ("0", "false", "False"):
        return selection
    items = selection.get("items", [])
    if not items:
        return selection

    scope = current_scope()
    marker_path = _stable_marker_path(scope)
    delivered = set()
    try:
        if marker_path.exists():
            delivered = set(json.loads(marker_path.read_text(encoding="utf-8")).get("delivered", []))
    except Exception:
        delivered = set()

    kept, referenced, newly = [], [], []
    for item in items:
        is_always = bool((item.get("matched") or {}).get("always"))
        if is_always and item.get("id") in delivered:
            referenced.append(item.get("id"))
            continue
        if is_always:
            newly.append(item.get("id"))
        kept.append(item)

    if newly:
        delivered.update(newly)
        try:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(
                json.dumps({"scope": scope, "delivered": sorted(delivered)}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    selection["items"] = kept
    selection["token_estimate"] = sum(i.get("token_estimate", 0) for i in kept)
    if referenced:
        selection["referenced_stable"] = referenced
        selection["referenced_note"] = (
            "Proje-global direktifler bu oturumda zaten teslim edildi; "
            "tam metin için context-agent://rules resource'una bak."
        )
    return selection


def _run_sufficiency_engine(task_plan, task, root, route_result, output,
                            quality_items, context_items, loaded_symbols,
                            known_items, ledger, scope, session_id, raw,
                            verbose, max_passes=2):
    """Sufficiency 2.0 (spec PHASE 10): açık coverage muhakemesi + OTOMATİK
    expansion. Yetersizlikte motorun kendisi ek retrieval turu yapar; sadece
    "dosya oku" tavsiyesi vermez.

    Durma koşulları: coverage hedefi / bütçe tükenmesi / yararlı aday kalmaması.
    quality_items/context_items/loaded_symbols/known_items YERİNDE güncellenir.
    Dönüş: machine-readable sufficiency bölümü.
    """
    try:
        import sufficiency as sufficiency_mod
    except ImportError:
        from scripts import sufficiency as sufficiency_mod

    db_path = _symbols_db()
    conn = None
    if db_path is not None and Path(str(db_path)).exists():
        try:
            conn = sqlite3.connect(str(db_path), timeout=10)
            conn.execute("PRAGMA busy_timeout=10000")
        except Exception:
            conn = None

    flags = {
        "has_project_map": bool(output.get("sections", {}).get("project_map")),
        "git_available": bool(root) and (root / ".git").exists(),
    }
    confidence_level = (route_result.get("confidence", {}) or {}).get("level", "low")
    expansion_history = []
    coverage = None

    def _evaluate():
        evidence = sufficiency_mod.detect_evidence(
            conn, task_plan["required_evidence"], quality_items,
            known_items, task_text=task, flags=flags)
        return sufficiency_mod.build_coverage(
            task_plan, evidence,
            quality=output["sections"].get("context_quality", {}),
            confidence_level=confidence_level)

    try:
        for pass_no in range(1, max_passes + 1):
            coverage = _evaluate()
            if coverage["sufficient"]:
                expansion_history.append({
                    "pass": pass_no, "action": "none_needed",
                    "coverage": coverage["coverage"],
                })
                break

            file_cands = [c for c in coverage["recommended_expansion"]
                          if "file" in c]
            already = ({it.get("file") for it in quality_items}
                       | {k["file"] for k in known_items})
            loaded_this_pass = 0
            for cand in file_cands:
                if cand["file"] in already:
                    continue
                file_info = {
                    "file": cand["file"],
                    "priority": "secondary",
                    "action": "load_file",
                    "token_estimate": min(int(cand.get("est_tokens", 220) or 220), 220),
                    "summary": "",
                    "why": [f"auto-expansion: {cand.get('for', '')}"],
                }
                kind, item, known_entry, symbol_entry = _load_file_item(
                    file_info, raw=raw, ledger=ledger, scope=scope,
                    session_id=session_id, task=task,
                    client=output.get("client", ""),
                    model=output.get("model", ""), verbose=verbose)
                already.add(cand["file"])
                if kind == "loaded":
                    item["expansion_pass"] = pass_no
                    context_items.append(item)
                    quality_items.append(item)
                    loaded_symbols.append(symbol_entry)
                    loaded_this_pass += 1
                elif kind == "known":
                    known_items.append(known_entry)
                    quality_items.append(_known_pseudo_item(known_entry))
                    loaded_this_pass += 1

            expansion_history.append({
                "pass": pass_no,
                "coverage_before": coverage["coverage"],
                "missing": list(coverage["missing_evidence"]),
                "loaded": loaded_this_pass,
            })
            if loaded_this_pass == 0:
                break  # yararlı aday kalmadı
            # Yeni efektif bağlamla kaliteyi tazele (sonraki turun girdisi).
            output["sections"]["context_quality"] = context_quality_score(
                route_result, quality_items)

        # Final değerlendirme (her zaman taze).
        coverage = _evaluate()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    section = build_context_sufficiency(
        output["sections"].get("context_quality", {}), quality_items, route_result)
    if coverage is not None:
        section.update(coverage)
        section["expansion_history"] = expansion_history
    if known_items:
        section["known_to_model_files"] = [k["file"] for k in known_items]
    return section


def build_context_sufficiency(quality, context_items, route_result):
    """Motorun kendi "bana güveneyim mi" sinyali. Düşükse ajana doğrudan
    okumayı öneren bir kaçış tavsiyesi üretir (kalite kaybına karşı emniyet)."""
    score = int(quality.get("score", 0) or 0)
    level = (route_result.get("confidence", {}) or {}).get("level", "low")
    enough = score >= 45 and level != "low" and len(context_items) > 0
    suggested = [
        f.get("file")
        for f in (route_result.get("relevant_files", []) or [])[:5]
        if f.get("file")
    ]
    info = {
        "sufficient": enough,
        "quality_score": score,
        "confidence": level,
        "item_count": len(context_items),
    }
    if not enough:
        info["recommendation"] = (
            "Context yetersiz olabilir (düşük güven/skor). Gerekirse bu dosyaları "
            "doğrudan oku — motora takılma."
        )
        info["read_directly"] = suggested
    return info


def build_context_package(task, budget_limit=None, model="claude-haiku", verbose=False, record_usage=True, strict_quality=0, directive_budget=1200, raw=False, diagnostics=False):
    """
    Task için hazır context paketi üret.
    LLM'e tek seferde gönderilecek JSON döner.
    diagnostics=True ise retrieval kararlarının neden/niçin dökümü eklenir
    (spec PHASE 16 — OPSİYONEL tanı modu; normal çıktıya yük bindirmez).
    """
    root = find_root()
    raw = bool(raw) or os.environ.get("CONTEXT_AGENT_RAW", "0") in ("1", "true", "True")
    output = {
        "task": task,
        "model": model,
        "timestamp": datetime.now().isoformat(),
        "client": os.environ.get("CONTEXT_AGENT_CLIENT", "direct"),
        "scope": current_scope(),
        "runtime_scope": current_runtime_scope(),
        "raw": raw,
        "sections": {}
    }
    capsule_start = call_component("capsule", "start", task)
    output["sections"]["capsule"] = capsule_start.get("capsule", {})

    print("[1/6] Task analiz ediliyor...", file=sys.stderr)

    # ── 1. Model-aware bütçe (spec PHASE 6) ─────────────────────────────────
    # budget_limit verilmediyse ModelContextProfile + görev tipi oranından
    # OTOMATİK türetilir; açık --budget her zaman kazanır (backward compat).
    profile, model_window, budget_source = None, None, "explicit"
    try:
        try:
            import model_profile as model_profile_mod
            import planner as planner_mod
        except ImportError:
            from scripts import model_profile as model_profile_mod
            from scripts import planner as planner_mod
        profile = model_profile_mod.profile_for(model)
        early_plan = planner_mod.plan_task(task)
        frac = early_plan["budget_fraction"]
        frac_mid = (frac["min"] + frac["max"]) / 2
        model_window = model_profile_mod.retrieval_budget(profile)
        if not budget_limit:
            budget_limit = model_profile_mod.retrieval_budget(
                profile, task_fraction=frac_mid)
            budget_source = "model_profile"
    except Exception:
        if not budget_limit:
            budget_limit = 8000

    call_component("budget", "init_budget", int(budget_limit), model)
    if profile is not None:
        output["budget_profile"] = {
            "model": profile.get("model", model),
            "profile_source": profile.get("source", ""),
            "context_window": profile.get("context_window"),
            "retrieval_budget": budget_limit,
            "budget_source": budget_source,
            "decay_window_tokens": model_window,
        }
    # Build-içi çift yüklemeyi önle (cross-turn hafıza artık ledger'da).
    call_component("dedup", "clear_context")

    # ── 1b. Cross-turn ledger: model neyi zaten biliyor? ────────────────────
    ledger = _open_ledger()
    scope = branch_scope()  # branch-isolated ledger (GAP 3)
    session_id = ledger_session_id()
    ledger_turn = 0
    if ledger is not None:
        try:
            # Model profili decay penceresini belirler (model-aware hafıza).
            prev = ledger.session_state(
                scope, session_id,
                window_tokens=model_window or None)
            if prev.get("last_package_tokens") or prev["turn"] > 0 or prev["total_tokens"] > 0:
                # Önceki paket model tarafından tüketildi → decay saatini ilerlet.
                advanced = ledger.advance_turn(
                    scope, session_id,
                    tokens_generated=prev.get("last_package_tokens") or 0,
                )
                ledger_turn = advanced["turn"]
            # Bellek sınırlı büyüme: çok eski kayıtları düşür.
            ledger.purge_old(days=7)
        except Exception:
            ledger = None

    # ── 2. map.json oku (her zaman) ──────────────────────────────────────────
    map_path = root / ".context" / "map.json"
    if map_path.exists():
        import json as j
        map_data = j.loads(map_path.read_text(encoding="utf-8"))
        # Sadece LLM'in ihtiyacı olan kısmı al (scripts + stats + module özetleri)
        map_summary = {
            "stats": map_data.get("stats", {}),
            "scripts": map_data.get("scripts", {}),
            "modules": {
                k: {"summary": list(v.get("files", {}).values())[0].get("summary","") if v.get("files") else "",
                    "total_tokens": v.get("total_tokens", 0)}
                for k, v in map_data.get("modules", {}).items()
            }
        }
        map_tokens = int(len(json.dumps(map_summary).split()) * 1.3)
        call_component("budget", "spend", int(map_tokens), "map.json")
        output["sections"]["project_map"] = map_summary

    print("[2/6] Task routing...", file=sys.stderr)

    # ── 3. Route: ilgili dosyaları bul ───────────────────────────────────────
    route_result = call_component(
        "route", "route", task, budget_limit=budget_limit)
    if "error" not in route_result:
        output["sections"]["routing"] = {
            "analysis": route_result.get("analysis", {}),
            "confidence": route_result.get("confidence", {}),
            "relevant_files": route_result.get("relevant_files", []),
            "context_plan": route_result.get("context_plan", {}),
            "execution_plan": route_result.get("execution_plan", {}),
            "project_commands": route_result.get("project_commands", {}),
            "suggested_commands": route_result.get("suggested_first_commands", []),
            "model_recommendation": route_result.get("model_recommendation", {}),
            # Additive bridge for the optional Router layer (autoroute §25):
            # the router consumes this analysis and never re-classifies.
            "machine_readable": route_result.get("machine_readable", {}),
        }
        # Model önerisi güncelle
        rec = route_result.get("model_recommendation", {})
        # The recommendation is advisory. The configured IDE model remains
        # the actual consumer of this package and must not be relabelled.

    print("[3/6] Session bağlamı ekleniyor...", file=sys.stderr)

    # ── 4. Session bağlamı ───────────────────────────────────────────────────
    session_ctx = call_component("session", "get_context", task)
    if "context" in session_ctx and session_ctx["context"] != "Aktif session yok.":
        ctx_tokens = session_ctx.get("token_estimate", 50)
        call_component("budget", "spend", int(ctx_tokens), "session context")
        output["sections"]["session_context"] = session_ctx["context"]

    capsule_ctx = call_component("capsule", "context_text", task)
    if "context" in capsule_ctx and capsule_ctx["context"] != "No active capsule.":
        output["sections"]["capsule_context"] = capsule_ctx["context"]

    print("[4/6] Semboller yükleniyor...", file=sys.stderr)

    # ── 5. İlgili sembolleri yükle (dedup kontrolüyle) ───────────────────────
    loaded_symbols = []
    context_items = []
    known_items = []       # modelin zaten bildiği (yeniden gönderilmeyen) dosyalar
    tokens_saved = 0

    if "error" in route_result:
        # route başarısız oldu; doğrudan sembol aramasıyla kurtarma yap
        fallback_items = _fallback_search_items(task, budget_limit)
        relevant_files = fallback_items
        route_result = {
            "relevant_files": fallback_items,
            "context_plan": {"steps": []},
            "confidence": {"level": "low", "score": 0, "reason": "route_failed_fallback"},
            "analysis": {"keywords": [], "domains": [], "complexity": "medium", "op_type": "unknown"},
        }
        output["sections"]["routing"] = {
            "analysis": route_result["analysis"],
            "confidence": route_result["confidence"],
            "relevant_files": fallback_items,
            "context_plan": route_result["context_plan"],
            "execution_plan": {},
            "project_commands": {},
            "suggested_commands": [],
            "model_recommendation": {},
            "machine_readable": {},
            "fallback": True,
            "fallback_reason": "route_script_failed",
        }
    else:
        relevant_files = planned_load_items(route_result)

    # ── Git-aware retrieval (spec PHASE 13): dirty/recent dosyalar sinyaldir ──
    # Repo dışı aramadan önce, üzerinde aktif çalışılan dosyalar boost edilir.
    try:
        try:
            import git_meta as git_meta_mod
        except ImportError:
            from scripts import git_meta as git_meta_mod
        ginfo = git_meta_mod.git_info(root)
        if ginfo.get("available"):
            git_changed = git_meta_mod.changed_files(root)
            output["sections"]["git_context"] = {
                "available": True,
                "branch": ginfo.get("branch"),
                "head": ginfo.get("head"),
                "dirty": ginfo.get("dirty"),
                "changed_files": git_changed,
                "recently_changed": git_meta_mod.recent_files(root),
            }
            relevant_files = _boost_git_changed(
                relevant_files, git_changed,
                (route_result.get("analysis", {}) or {}).get(
                    "raw_keywords", []))
    except Exception:
        pass

    # Task-aware planner: görev tipi, risk, strateji, bütçe oranı, evidence.
    # Sufficiency engine ve budget bu planı tüketir (spec PHASE 5 + 11).
    try:
        try:
            import planner as planner_mod
        except ImportError:
            from scripts import planner as planner_mod
        task_plan = planner_mod.plan_task(task, route_result.get("analysis"))
        output["sections"]["task_plan"] = task_plan
    except Exception:
        task_plan = None

    omitted_items = []  # explainability (PHASE 16): atlananlar + nedenleri
    for file_info in relevant_files[:8]:
        kind, context_item, known_entry, symbol_entry = _load_file_item(
            file_info, raw=raw, ledger=ledger, scope=scope,
            session_id=session_id, task=task,
            client=output.get("client", ""), model=output.get("model", ""),
            verbose=verbose)
        if kind == "known":
            known_items.append(known_entry)
            tokens_saved += int(known_entry.get("token_saved", 0) or 0)
        elif kind == "loaded":
            loaded_symbols.append(symbol_entry)
            context_items.append(context_item)
        elif kind == "skip" and isinstance(context_item, dict):
            omitted_items.append(context_item)

    output["sections"]["loaded_files"] = loaded_symbols
    output["sections"]["context_items"] = context_items
    if known_items:
        output["sections"]["known_to_model"] = known_items
    output["sections"]["context_reuse"] = {
        "enabled": ledger is not None,
        "session_id": session_id,
        "turn": ledger_turn,
        "known_count": len(known_items),
        "tokens_saved": tokens_saved,
        "note": "Değişmemiş ve modelin zaten bildiği içerik yeniden gönderilmez; "
                "decay ile pencereden düşenler otomatik yenilenir.",
    }
    # ── Explainability (spec PHASE 16): OPSİYONEL tanı modu ─────────────────
    # Her item neden DAHİL edildi, her önemli atlama neden hariç kaldı.
    # Normal model context'e yük bindirmez; sadece diagnostics=True iken üretilir.
    if diagnostics:
        output["sections"]["retrieval_diagnostics"] = {
            "included": [
                {
                    "file": it.get("file"),
                    "action": it.get("action"),
                    "why": it.get("why") or it.get("summary") or [],
                }
                for it in context_items
            ],
            "known_to_model": [k.get("file") for k in known_items],
            "omitted": omitted_items,
        }

    # Kalite/yeterlilik, modelin EFEKTİF bağlamını görmeli:
    # gönderilenler + modelin zaten bildikleri (known_to_model).
    quality_items = context_items + [_known_pseudo_item(k) for k in known_items]
    output["sections"]["context_quality"] = context_quality_score(route_result, quality_items)
    if strict_quality and int(strict_quality) > 0:
        quality, strict_info = apply_strict_quality(
            route_result,
            loaded_symbols,
            quality_items,
            min_quality=int(strict_quality),
            max_extra_items=4,
            verbose=verbose,
            raw=raw,
        )
        # Strict modun yüklediği gerçek itemleri çıktı listesine senkronize et.
        context_items[:] = [
            it for it in quality_items if it.get("action") != "known_to_model"
        ]
        output["sections"]["context_quality"] = quality
        output["sections"]["strict_quality"] = strict_info
        output["sections"]["loaded_files"] = loaded_symbols
        output["sections"]["context_items"] = context_items
    else:
        output["sections"]["strict_quality"] = {"enabled": False, "target": 0}
    # ── Project Memory 2.0 (spec PHASE 8): tipli, yaşam döngülü proje belleği ──
    try:
        try:
            import memory as memory_mod
        except ImportError:
            from scripts import memory as memory_mod
        mem = memory_mod.ProjectMemory(_symbols_db())
        try:
            # Branch isolation: merge project-global + branch-specific items.
            pg_scope = project_global_scope()
            br_scope = branch_scope()
            if pg_scope == br_scope:
                active = mem.list(scope, statuses=("ACTIVE",))
            else:
                active = mem.list(pg_scope, statuses=("ACTIVE",))
                seen_ids = {r.get("id") for r in active}
                for r in mem.list(br_scope, statuses=("ACTIVE",)):
                    if r.get("id") not in seen_ids:
                        active.append(r)
            # state_sheet: combine both scopes
            if pg_scope == br_scope:
                sheet = mem.state_sheet(scope)
            else:
                sheet = mem.state_sheet(pg_scope)
                br_sheet = mem.state_sheet(br_scope)
                if br_sheet:
                    sheet = (sheet + "\n\n" + br_sheet).strip()
            output["sections"]["project_memory"] = {
                "enabled": True,
                "active_count": len(active),
                "state_sheet": sheet,
            }
        finally:
            mem.close()
    except Exception:
        output["sections"]["project_memory"] = {"enabled": False}

    # ── 5b. Direktif/playbook katmanı (kod yanında "yöntem") ─────────────────
    directives_selection = select_task_directives(route_result, context_items, budget_tokens=directive_budget, raw=raw)
    if directives_selection is not None:
        output["sections"]["directives"] = directives_selection

    # ── 5c. Sufficiency 2.0: coverage muhakemesi + OTOMATİK expansion ───────
    # (spec PHASE 10) Motor yetersizlikte kendisi ek retrieval turu yapar.
    sufficiency_section = build_context_sufficiency(
        output["sections"].get("context_quality", {}), quality_items, route_result
    )
    if task_plan is not None and not raw:
        try:
            sufficiency_section = _run_sufficiency_engine(
                task_plan, task, root, route_result, output, quality_items,
                context_items, loaded_symbols, known_items, ledger, scope,
                session_id, raw, verbose)
            # Expansion listeleri büyütmüş olabilir → bölümleri senkronize et.
            output["sections"]["loaded_files"] = loaded_symbols
            output["sections"]["context_items"] = context_items
            if known_items:
                output["sections"]["known_to_model"] = known_items
            reuse = output["sections"].get("context_reuse")
            if isinstance(reuse, dict):
                reuse["known_count"] = len(known_items)
                reuse["tokens_saved"] = sum(
                    int(k.get("token_saved", 0) or 0) for k in known_items)
        except Exception:
            pass
    if known_items and "known_to_model_files" not in sufficiency_section:
        sufficiency_section["known_to_model_files"] = [
            k["file"] for k in known_items
        ]
    output["sections"]["context_sufficiency"] = sufficiency_section

    # Persist the final package once, after sufficiency expansion. The old
    # per-item path repeatedly read Git metadata and rewrote the capsule.
    capsule_items = [
        {
            "file": item.get("file"),
            "reason": item.get("reason", ""),
            "tokens": int(item.get("token_estimate", 0) or 0),
            "action": item.get("action", "load_file"),
        }
        for item in context_items
        if item.get("file")
    ]
    capsule_batch = call_component("capsule", "add_contexts", capsule_items)
    if capsule_batch.get("capsule"):
        output["sections"]["capsule"] = capsule_batch["capsule"]
    output["sections"]["capsule_context"] = call_component(
        "capsule", "context_text", task).get(
            "context", output["sections"].get("capsule_context", ""))

    print("[5/6] Bütçe hesaplanıyor...", file=sys.stderr)

    # ── 6. Final bütçe durumu ────────────────────────────────────────────────
    budget_status = call_component("budget", "status")
    output["budget"] = {
        "used": budget_status.get("used", 0),
        "max": budget_status.get("max", budget_limit),
        "remaining": budget_status.get("remaining", 0),
        "percent": budget_status.get("percent_used", 0),
        "bar": budget_status.get("bar", ""),
        "estimated_cost_usd": budget_status.get("estimated_cost_usd", 0)
    }
    repo_tokens = output.get("sections", {}).get("project_map", {}).get("stats", {}).get("total_tokens", 0)
    context_tokens = sum(item.get("token_estimate", 0) for item in context_items)
    quality_score = output.get("sections", {}).get("context_quality", {}).get("score", 0)
    if record_usage:
        usage = call_component(
            "usage", "record", output["client"], task, repo_tokens,
            context_tokens, quality_score, len(context_items))
        output["sections"]["usage"] = usage.get("event", usage)
    else:
        output["sections"]["usage"] = {
            "status": "skipped",
            "reason": "preview_mode",
            "repo_tokens": repo_tokens,
            "context_tokens": context_tokens,
            "quality": quality_score,
            "item_count": len(context_items),
        }

    print("[6/6] Paket hazır.", file=sys.stderr)

    # ── System prompt ────────────────────────────────────────────────────────
    output["system_prompt"] = generate_system_prompt(output)

    # ── Ledger kapanışı: paket boyutunu decay yakıtı olarak sakla ───────────
    if ledger is not None:
        try:
            pkg_tokens = int(len(json.dumps(output).split()) * 1.3)
            ledger.set_last_package_tokens(scope, session_id, pkg_tokens)
        except Exception:
            pass
        finally:
            ledger.close()

    return output

def generate_system_prompt(ctx):
    """Context paketi için system prompt üret"""
    budget = ctx.get("budget", {})
    scripts = ctx.get("sections", {}).get("project_map", {}).get("scripts", {})

    script_lines = "\n".join(f"  {v}" for v in scripts.values())

    modules = ctx.get("sections", {}).get("project_map", {}).get("modules", {})
    module_lines = "\n".join(
        f"  {k}: {v.get('summary','')[:80]}"
        for k, v in modules.items()
    )

    loaded = ctx.get("sections", {}).get("loaded_files", [])
    loaded_lines = "\n".join(
        f"  {f['file']} [{f.get('priority','primary')}]: {f['summary'][:60]}"
        for f in loaded
    ) if loaded else "  (henüz yüklenmedi — önce search veya get_symbol kullan)"

    known = ctx.get("sections", {}).get("known_to_model", [])
    known_lines = "\n".join(
        f"  {k['file']} [{k.get('representation','?')}, turn {k.get('turn_sent')}]"
        for k in known
    ) if known else "  (yok)"

    session_ctx = ctx.get("sections", {}).get("session_context", "")
    capsule_ctx = ctx.get("sections", {}).get("capsule_context", "")
    directives_md = ""
    directives_sel = ctx.get("sections", {}).get("directives")
    if directives_sel and directives_sel.get("items"):
        try:
            import directives as directives_mod
            directives_md = directives_mod.render_directives_markdown(directives_sel)
        except Exception:
            directives_md = ""
    return f"""Sen bir coding assistant'sın. Local context store kullanarak çalışıyorsun.

## Token Bütçesi
Kullanılan: {budget.get('used',0)} / {budget.get('max',8000)} token
Kalan: {budget.get('remaining',0)} token  {budget.get('bar','')}
Tahmini maliyet: ${budget.get('estimated_cost_usd',0):.5f}

## Proje Modülleri
{module_lines}

## Yüklü Dosyalar (Bu Session)
{loaded_lines}

## Zaten Bildiğin Dosyalar (değişmedi, yeniden gönderilmedi)
{known_lines}
Gereken yerde bu dosyaların güncel içeriğini get_range/get_symbol ile iste.

## Kullanabileceğin Script'ler
{script_lines}

## Kurallar
1. Kod yazmadan önce MUTLAKA önce oku: get_symbol veya get_range çalıştır
2. Ne aradığını bilmiyorsan: search kullan
3. Bağımlılık anlamak için: get_related
4. Hata logu varsa önce: compress_log
5. Aynı şeyi iki kez isteme — dedup otomatik takip ediyor
6. Dosya değiştirdikten sonra: index.py --file <dosya>

## Geçmiş Session Bağlamı
{session_ctx if session_ctx else "(yok)"}

## Context Capsule
{capsule_ctx if capsule_ctx else "(yok)"}

{directives_md if directives_md else ""}

## Görev
{ctx.get('task','')}
""".strip()

def status():
    """Sistemin genel durumunu göster"""
    root = find_root()

    # Index durumu
    ctx_dir = root / ".context"
    has_index = (ctx_dir / "symbols.db").exists()
    has_map   = (ctx_dir / "map.json").exists()

    result = {
        "root": str(root),
        "index_exists": has_index,
        "map_exists": has_map,
        "client": os.environ.get("CONTEXT_AGENT_CLIENT", "direct"),
        "ide": os.environ.get("CONTEXT_AGENT_IDE", ""),
        "scope": current_scope(),
        "runtime_scope": current_runtime_scope(),
        "model_session_id": (
            os.environ.get("CONTEXT_AGENT_MODEL_SESSION_ID")
            or os.environ.get("CONTEXT_AGENT_SESSION", "")
        ),
        "conversation_id": os.environ.get("CONTEXT_AGENT_CONVERSATION_ID", ""),
    }

    if has_index:
        import sqlite3 as _sq
        conn = _sq.connect(str(ctx_dir / "symbols.db"), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        result["files"]   = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        result["symbols"] = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]

    if has_map:
        import json as _j
        m = _j.loads((ctx_dir / "map.json").read_text(encoding="utf-8"))
        result["last_indexed"] = m.get("generated_at","")[:19]
        result["total_tokens"] = m.get("stats",{}).get("total_tokens",0)

    budget = run_script("budget", "--status")
    result["budget"] = {
        "used": budget.get("used", 0),
        "max": budget.get("max", 0),
        "remaining": budget.get("remaining", 0),
        "bar": budget.get("bar", ""),
    }

    # Session
    session = run_script("session", "--context")
    result["session"] = session.get("context","Yok")[:100]
    result["capsule"] = run_script("capsule", "--current").get("capsule")
    result["usage"] = run_script("usage", "--summary")

    print(json.dumps(result, indent=2, ensure_ascii=False))

def main():
    parser = argparse.ArgumentParser(description="Context Agent Orkestratörü")
    parser.add_argument("task", nargs="?", help="Yapılacak görev")
    parser.add_argument("--budget", type=int, default=None,
                        help="Token bütçesi (verilmezse model profiline göre otomatik)")
    parser.add_argument("--model", default="claude-haiku",
                        help="IDE/model identifier; unknown models use the conservative profile")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--new-session", metavar="NAME")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--skip-usage", action="store_true", help="Do not record usage event (preview mode).")
    parser.add_argument(
        "--strict-quality",
        type=int,
        default=0,
        help="Target minimum context quality score (0 disables strict mode).",
    )
    parser.add_argument(
        "--directive-budget",
        type=int,
        default=1200,
        help="Token budget for the directive/playbook layer.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Fidelity mode: skip skeleton/window/stable-dedup; full file bodies.",
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Explainability: include/omit kararlarının neden dökümü (spec PHASE 16).",
    )
    args = parser.parse_args()

    if args.status:
        status()
        return

    if args.new_session:
        result = run_script("session", "--new", args.new_session)
        print(json.dumps(result, indent=2))
        return

    if not args.task:
        parser.print_help()
        return

    package = build_context_package(
        args.task,
        budget_limit=args.budget,
        model=args.model,
        verbose=args.verbose,
        record_usage=not args.skip_usage,
        strict_quality=max(0, min(100, int(args.strict_quality or 0))),
        directive_budget=max(0, int(args.directive_budget or 0)),
        raw=args.raw,
        diagnostics=args.diagnostics,
    )

    print(json.dumps(package, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
