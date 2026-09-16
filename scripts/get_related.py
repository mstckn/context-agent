#!/usr/bin/env python3
"""
get_related.py - Dosyanın bağımlılıklarını getirir.

Kullanım:
  python get_related.py src/auth/service.py
  python get_related.py src/auth/service.py --depth 2
  python get_related.py src/auth/service.py --reverse
"""

import sys, json, sqlite3, argparse
from pathlib import Path

try:  # run directly: `python get_related.py` (script dir on sys.path)
    from paths import find_context_dir
except ImportError:  # imported as a package: `from scripts import get_related`
    from scripts.paths import find_context_dir

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def import_like_patterns(module_name):
    normalized = module_name.strip()
    if not normalized:
        return []
    slash = normalized.replace(".", "/")
    backslash = normalized.replace(".", "\\")
    return list(dict.fromkeys([f"%{slash}%", f"%{backslash}%", f"%{normalized}%"]))

def find_imported_file(conn, import_name):
    for pattern in import_like_patterns(import_name):
        matched = conn.execute(
            "SELECT path, summary, line_count FROM files WHERE path LIKE ? LIMIT 1",
            (pattern,)
        ).fetchone()
        if matched:
            return matched
    return None

def get_importers(conn, rel_path, max_files):
    importer_rows = {}
    for pattern in import_like_patterns(Path(rel_path).stem):
        rows = conn.execute("""
            SELECT i.file, f.summary, f.line_count
            FROM imports i
            LEFT JOIN files f ON i.file = f.path
            WHERE i.imports_from LIKE ?
            LIMIT ?
        """, (pattern, max_files)).fetchall()
        for row in rows:
            importer_rows[row[0]] = row
    return list(importer_rows.values())[:max_files]

def get_related(filepath, depth=1, reverse=False, max_files=15):
    context_dir, root = find_context_dir()
    if not context_dir:
        print(json.dumps({"error": "Index bulunamadı"}))
        sys.exit(1)

    conn = sqlite3.connect(str(context_dir / "symbols.db"), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")

    rel_path = filepath
    if filepath.startswith(str(root)):
        rel_path = str(Path(filepath).relative_to(root))

    file_info = conn.execute(
        "SELECT path, lang, line_count, summary FROM files WHERE path = ?",
        (rel_path,)
    ).fetchone()

    if not file_info:
        similar = conn.execute(
            "SELECT path FROM files WHERE path LIKE ? LIMIT 5",
            (f"%{Path(filepath).name}%",)
        ).fetchall()
        print(json.dumps({
            "error": f"Dosya indexte bulunamadı: {rel_path}",
            "similar": [r[0] for r in similar]
        }, ensure_ascii=False))
        return

    result = {
        "file": rel_path,
        "summary": file_info[3],
        "imports": [],
        "imported_by": [],
        "key_symbols": [],
        "stats": {}
    }

    # Önce yapısal grafı dene (gerçek import çözümlemesi); yoksa LIKE fallback.
    graph_used = False
    try:
        import graph as graph_mod
        graph_mod.ensure_schema(conn)
        has_edges = conn.execute(
            "SELECT COUNT(*) FROM graph_meta WHERE file=?", (rel_path,)
        ).fetchone()[0]
        if has_edges:
            result["imports"] = [
                {"file": d["file"], "summary": d["summary"], "lines": d["lines"],
                 "relation": d["relation"]}
                for d in graph_mod.dependencies(conn, rel_path)
            ]
            result["imported_by"] = [
                {"file": d["file"], "summary": d["summary"], "lines": d["lines"],
                 "relation": d["relation"]}
                for d in graph_mod.dependents(conn, rel_path)
            ][:max_files]
            result["impact_set"] = graph_mod.impact_set(conn, rel_path, depth=depth)
            graph_used = True
    except Exception:
        graph_used = False

    if not reverse:
        raw_imports = conn.execute(
            "SELECT imports_from FROM imports WHERE file = ?",
            (rel_path,)
        ).fetchall()

        if not graph_used:
            for (imp,) in raw_imports:
                matched = find_imported_file(conn, imp)
                if matched:
                    result["imports"].append({
                        "file": matched[0],
                        "summary": matched[1] or "",
                        "lines": matched[2]
                    })
                else:
                    result["imports"].append({
                        "module": imp,
                        "external": True
                    })

        symbols = conn.execute(
            "SELECT name, type, start_line, signature FROM symbols WHERE file = ? LIMIT 20",
            (rel_path,)
        ).fetchall()
        result["key_symbols"] = [
            {"name": n, "type": t, "line": l, "signature": s}
            for n, t, l, s in symbols
        ]

    if not graph_used:
        for file_r, summary, lines in get_importers(conn, rel_path, max_files):
            result["imported_by"].append({
                "file": file_r,
                "summary": summary or "",
                "lines": lines
            })

    result["stats"] = {
        "imports_count": len(result["imports"]),
        "imported_by_count": len(result["imported_by"]),
        "symbols_count": len(result["key_symbols"]),
        "depth": depth,
        "reverse_only": reverse,
        "graph_used": graph_used
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="Dosya yolu")
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--reverse", action="store_true", help="Sadece kim import ediyor")
    parser.add_argument("--max", type=int, default=15)
    args = parser.parse_args()
    get_related(args.file, args.depth, args.reverse, args.max)

if __name__ == "__main__":
    main()
