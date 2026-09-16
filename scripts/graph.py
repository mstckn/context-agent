#!/usr/bin/env python3
"""
graph.py — Yapısal bağımlılık/referans grafı (Phase 1c).

Eski LIKE-tabanlı komşu bulma hem yanlış pozitif üretiyordu hem gerçek
import çözümlemesi yapmıyordu. Bu modül deterministik bir kenar grafı kurar:

  imports    : dosya → dosya (gerçek modül çözümlemesi, paket/__init__ dahil)
  calls      : sembol → sembol (aynı dosya + import edilen isimler)
  inherits   : sınıf → sınıf (base class çözümlemesi)

Dil adaptörleri: Python = AST (tam), JS/TS = import cümlesi çözümlemesi
(çağrı grafı regex ile güvenilir olmadığı için bilinçli olarak yok).

Sorgular:
  dependencies(conn, f) : f'nin bağımlı OLDUĞU dosyalar (outgoing)
  dependents(conn, f)   : f'ye bağımlı olan dosyalar (incoming)
  impact_set(conn, f)   : f değişirse ETKİLENECEK dosyalar (transitif, BFS)

CLI:
  python graph.py --build [--file P]
  python graph.py --related PATH
  python graph.py --impact PATH [--depth 2]
"""

import argparse
import ast
import json
import re
import sqlite3
import sys
from collections import deque
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from paths import find_context_dir
except ImportError:
    from scripts.paths import find_context_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_file TEXT NOT NULL,
    src_symbol TEXT DEFAULT '',
    kind TEXT NOT NULL,
    dst_file TEXT,
    dst_module TEXT DEFAULT '',
    dst_symbol TEXT DEFAULT '',
    UNIQUE(src_file, src_symbol, kind, dst_file, dst_module, dst_symbol)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_file);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_file);
CREATE TABLE IF NOT EXISTS graph_meta (
    file TEXT PRIMARY KEY,
    hash TEXT,
    built_at TEXT
);
"""

_JS_IMPORT_RE = re.compile(
    r"""(?:import\s[^'";]*?from\s*|import\s*\(\s*|require\s*\(\s*|export\s[^'";]*?from\s*)['"]([^'"]+)['"]""",
)


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


# ── Python çözümleme ───────────────────────────────────────────────────

def _module_to_file(module, known_files):
    """Noktalı modül adı → proje içi dosya yolu (yoksa None)."""
    if not module:
        return None
    base = module.replace(".", "/")
    for cand in (base + ".py", base + "/__init__.py"):
        if cand in known_files:
            return cand
    return None


def _resolve_python_imports(tree, rel_path, known_files):
    """Import cümlelerini gerçek dosyalara çözer.

    Dönüş: (file_edges, imported_names)
      file_edges      : [(dst_file, dst_module, dst_symbol), ...]
      imported_names  : {yerel_isim: (dst_file, dst_symbol)}
    """
    src_dir = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    file_edges = []
    imported_names = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dst = _module_to_file(alias.name, known_files)
                file_edges.append((dst, alias.name, ""))
                local = alias.asname or alias.name.split(".")[0]
                if dst:
                    imported_names[local] = (dst, "")
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            parts = src_dir.split("/") if src_dir else []
            if level > 1:
                parts = parts[: -(level - 1)]
            base_pkg = ".".join(parts)
            if node.module:
                module = f"{base_pkg}.{node.module}" if base_pkg and level else node.module
            else:
                module = base_pkg
            dst = _module_to_file(module, known_files)
            for alias in node.names:
                if alias.name == "*":
                    file_edges.append((dst, module, "*"))
                    continue
                # `from pkg import submodule` → alt modül dosyası olabilir.
                sub = _module_to_file(f"{module}.{alias.name}" if module else alias.name,
                                      known_files)
                target = sub or dst
                file_edges.append((target, module, alias.name))
                local = alias.asname or alias.name
                if target:
                    imported_names[local] = (target, alias.name)
    return file_edges, imported_names


def build_python_edges(rel_path, content, known_files):
    """Python dosyası için tüm kenarları üret."""
    edges = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return edges

    file_edges, imported_names = _resolve_python_imports(tree, rel_path, known_files)
    for dst_file, dst_module, dst_symbol in file_edges:
        edges.append((rel_path, "", "imports", dst_file, dst_module, dst_symbol))

    # Aynı dosyadaki üst-seviye semboller.
    local_defs = {}
    classes = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local_defs[node.name] = node.lineno
        elif isinstance(node, ast.ClassDef):
            local_defs[node.name] = node.lineno
            classes[node.name] = node

    # Kalıtım kenarları.
    for name, node in classes.items():
        for base in node.bases:
            target = None
            if isinstance(base, ast.Name):
                if base.id in imported_names:
                    dst_file, dst_symbol = imported_names[base.id]
                    target = (dst_file, dst_symbol or base.id)
                elif base.id in local_defs:
                    target = (rel_path, base.id)
            elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
                if base.value.id in imported_names:
                    dst_file, _ = imported_names[base.value.id]
                    target = (dst_file, base.attr)
            if target:
                edges.append((rel_path, name, "inherits", target[0], "", target[1]))

    # Çağrı kenarları (import edilen isimler + aynı dosya tanımları).
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in imported_names:
                dst_file, dst_symbol = imported_names[func.id]
                edges.append((rel_path, "", "calls", dst_file, "", dst_symbol))
            elif func.id in local_defs:
                edges.append((rel_path, "", "calls", rel_path, "", func.id))
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            mod = imported_names.get(func.value.id)
            if mod:
                dst_file, dst_symbol = mod
                if dst_symbol and dst_symbol.endswith("__init__"):
                    dst_symbol = ""
                edges.append((rel_path, "", "calls", dst_file, "",
                              func.attr if dst_symbol in ("", "__init__") else dst_symbol))
    return edges


# ── JS/TS çözümleme ────────────────────────────────────────────────────

def build_js_edges(rel_path, content, known_files):
    """JS/TS import cümlelerini çözer (çağrı grafı yok — bilinçli karar)."""
    edges = []
    src_dir = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    for m in _JS_IMPORT_RE.finditer(content):
        spec = m.group(1)
        dst = None
        if spec.startswith("."):
            base = f"{src_dir}/{spec}" if src_dir else spec.lstrip("./")
            # ../ bileşenlerini sadeleştir
            parts = []
            for p in base.split("/"):
                if p in ("", "."):
                    continue
                if p == "..":
                    if parts:
                        parts.pop()
                else:
                    parts.append(p)
            stem = "/".join(parts)
            for ext in ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"):
                cand = stem + ext
                if cand in known_files:
                    dst = cand
                    break
        # Paket importları (bare specifier) proje dışı → dst None.
        edges.append((rel_path, "", "imports", dst, spec, ""))
    return edges


def build_file_edges(conn, rel_path, content, lang, file_hash, known_files, *,
                     ensure=True, commit=True):
    if ensure:
        ensure_schema(conn)
    conn.execute("DELETE FROM edges WHERE src_file=?", (rel_path,))
    if lang == "python":
        edges = build_python_edges(rel_path, content, known_files)
    elif lang in ("javascript", "typescript"):
        edges = build_js_edges(rel_path, content, known_files)
    else:
        edges = []
    for e in edges:
        conn.execute(
            "INSERT OR IGNORE INTO edges (src_file, src_symbol, kind, dst_file, "
            "dst_module, dst_symbol) VALUES (?,?,?,?,?,?)",
            e,
        )
    conn.execute(
        "INSERT INTO graph_meta (file, hash, built_at) VALUES (?,?,?) "
        "ON CONFLICT(file) DO UPDATE SET hash=excluded.hash, built_at=excluded.built_at",
        (rel_path, file_hash or "", datetime.now().isoformat()),
    )
    if commit:
        conn.commit()
    return len(edges)


def build_all(root, conn, only_file=None):
    """Değişen/yeni dosyalar için grafı güncelle."""
    ensure_schema(conn)
    known_files = {r[0] for r in conn.execute("SELECT path FROM files").fetchall()}
    query = "SELECT path, lang, hash FROM files"
    params = ()
    if only_file:
        query += " WHERE path=?"
        params = (only_file.replace("\\", "/"),)
    built, skipped = 0, 0
    for rel_path, lang, file_hash in conn.execute(query, params).fetchall():
        meta = conn.execute(
            "SELECT hash FROM graph_meta WHERE file=?", (rel_path,)
        ).fetchone()
        if meta and meta[0] == file_hash:
            skipped += 1
            continue
        try:
            content = (root / rel_path).read_text(encoding="utf-8-sig", errors="replace")
        except Exception:
            continue
        build_file_edges(
            conn, rel_path, content, lang or "unknown", file_hash or "",
            known_files, ensure=False, commit=False,
        )
        built += 1
    # Silinen dosyaların kenarlarını düşür.
    conn.execute(
        "DELETE FROM edges WHERE src_file NOT IN (SELECT path FROM files)")
    conn.commit()
    return {"built": built, "skipped_unchanged": skipped}


# ── sorgular ───────────────────────────────────────────────────────────

def _file_summary(conn, path):
    row = conn.execute(
        "SELECT summary, line_count FROM files WHERE path=?", (path,)
    ).fetchone()
    return {"summary": (row[0] or "") if row else "", "lines": row[1] if row else 0}


_KIND_RANK = {"imports": 3, "inherits": 2, "calls": 1}


def _strongest(pairs):
    """[(file, kind), ...] → dosya başına en güçlü tek ilişki."""
    best = {}
    for path, kind in pairs:
        if path not in best or _KIND_RANK.get(kind, 0) > _KIND_RANK.get(best[path], 0):
            best[path] = kind
    return best


def dependencies(conn, rel_path):
    """Bu dosyanın bağımlı olduğu DOSYALAR (çözülebilmiş kenarlar)."""
    rows = conn.execute(
        "SELECT DISTINCT dst_file, kind FROM edges "
        "WHERE src_file=? AND dst_file IS NOT NULL AND dst_file != '' AND dst_file != ?",
        (rel_path, rel_path),
    ).fetchall()
    out = []
    for dst, kind in sorted(_strongest(rows).items()):
        info = _file_summary(conn, dst)
        info["file"] = dst
        info["relation"] = kind
        out.append(info)
    return out


def dependents(conn, rel_path):
    """Bu dosyaya bağımlı olan DOSYALAR (kim bunu import/çağrı ediyor)."""
    rows = conn.execute(
        "SELECT DISTINCT src_file, kind FROM edges WHERE dst_file=? AND src_file != ?",
        (rel_path, rel_path),
    ).fetchall()
    out = []
    for src, kind in sorted(_strongest(rows).items()):
        info = _file_summary(conn, src)
        info["file"] = src
        info["relation"] = f"{kind}_this"
        out.append(info)
    return out


def impact_set(conn, rel_path, depth=2):
    """Transitif etki kümesi: rel_path değişirse kim etkilenir (BFS, gelen kenarlar)."""
    seen = {}
    queue = deque([(rel_path, 0)])
    while queue:
        current, dist = queue.popleft()
        if dist >= depth:
            continue
        for (src,) in conn.execute(
            "SELECT DISTINCT src_file FROM edges WHERE dst_file=?", (current,)
        ).fetchall():
            if src == rel_path or src in seen:
                continue
            seen[src] = dist + 1
            queue.append((src, dist + 1))
    out = []
    for path, dist in sorted(seen.items(), key=lambda kv: kv[1]):
        info = _file_summary(conn, path)
        info["file"] = path
        info["distance"] = dist
        out.append(info)
    return out


def outgoing_edges(conn, rel_path, limit=60):
    rows = conn.execute(
        "SELECT kind, dst_file, dst_module, dst_symbol, src_symbol FROM edges "
        "WHERE src_file=? LIMIT ?",
        (rel_path, limit),
    ).fetchall()
    return [
        {"kind": k, "dst_file": d, "dst_module": m, "dst_symbol": s, "src_symbol": ss}
        for k, d, m, s, ss in rows
    ]


# ── CLI ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Structural dependency graph")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build", action="store_true")
    group.add_argument("--related", metavar="PATH")
    group.add_argument("--impact", metavar="PATH")
    parser.add_argument("--file", help="build: tek dosya")
    parser.add_argument("--depth", type=int, default=2)
    args = parser.parse_args()

    context_dir, root = find_context_dir()
    if not context_dir:
        print(json.dumps({"error": "Index bulunamadı. Önce: python .context/scripts/index.py"}))
        sys.exit(1)

    conn = sqlite3.connect(str(context_dir / "symbols.db"), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        if args.build:
            print(json.dumps(build_all(root, conn, only_file=args.file)))
        elif args.related:
            rel = args.related.replace("\\", "/")
            print(json.dumps({
                "file": rel,
                "dependencies": dependencies(conn, rel),
                "dependents": dependents(conn, rel),
                "edges": outgoing_edges(conn, rel),
            }, indent=2, ensure_ascii=False))
        else:
            rel = args.impact.replace("\\", "/")
            print(json.dumps({
                "file": rel,
                "depth": args.depth,
                "impacted": impact_set(conn, rel, depth=args.depth),
            }, indent=2, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
