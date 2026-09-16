#!/usr/bin/env python3
"""
get_symbol.py — Sembol kaynak kodunu getirir.

Kullanım:
  python get_symbol.py AuthService
  python get_symbol.py AuthService.login
  python get_symbol.py login --type function
  python get_symbol.py Auth --fuzzy
"""

import sys, json, sqlite3, argparse
from pathlib import Path

try:  # run directly: `python get_symbol.py` (script dir on sys.path)
    from paths import find_context_dir
except ImportError:  # imported as a package: `from scripts import get_symbol`
    from scripts.paths import find_context_dir

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def _normalized_file_filter(file_path):
    if not file_path:
        return None
    return str(file_path).replace("\\", "/").strip()


def _add_optional_filters(query, params, sym_type=None, file_path=None):
    if sym_type:
        query += " AND type = ?"
        params.append(sym_type)

    normalized_file = _normalized_file_filter(file_path)
    if normalized_file:
        query += " AND replace(file, '\\', '/') = ?"
        params.append(normalized_file)

    return query, params


def get_symbol(name, fuzzy=False, sym_type=None, file_path=None):
    context_dir, root = find_context_dir()
    if not context_dir:
        print(json.dumps({"error": "Index bulunamadı. Önce: python .context/scripts/index.py"}))
        sys.exit(1)

    conn = sqlite3.connect(str(context_dir / "symbols.db"), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")

    # Tam eşleşme dene
    query = "SELECT name, type, file, start_line, end_line, params, docstring, signature FROM symbols WHERE name = ?"
    params = [name]

    # ClassName.method_name formatı
    if "." in name:
        class_name, method_name = name.rsplit(".", 1)
        query = """
            SELECT name, type, file, start_line, end_line, params, docstring, signature
            FROM symbols WHERE name = ? AND file IN (
                SELECT file FROM symbols WHERE name = ?
            )
        """
        params = [method_name, class_name]

    query, params = _add_optional_filters(query, params, sym_type, file_path)

    rows = conn.execute(query, params).fetchall()

    # Bulunamadıysa fuzzy dene
    if not rows:
        search_term = name.split(".")[-1]
        fuzzy_query = """
            SELECT name, type, file, start_line, end_line, params, docstring, signature
            FROM symbols WHERE name LIKE ?
        """
        fuzzy_params = [f"%{search_term}%"]
        fuzzy_query, fuzzy_params = _add_optional_filters(
            fuzzy_query, fuzzy_params, sym_type, file_path
        )
        fuzzy_query += " LIMIT 10"
        rows = conn.execute(fuzzy_query, fuzzy_params).fetchall()

        if not rows:
            conn.close()
            print(json.dumps({
                "error": "not_found",
                "searched_for": name,
                "suggestion": "python .context/scripts/search.py \"" + name + "\""
            }))
            return

        if not fuzzy and len(rows) > 1:
            suggestions = [{"name": r[0], "type": r[1], "file": r[2]} for r in rows]
            conn.close()
            print(json.dumps({
                "error": "ambiguous",
                "matches": suggestions,
                "hint": "Daha spesifik ol veya --fuzzy kullan"
            }))
            return

    if file_path and len(rows) > 1:
        exact_file = str(file_path).strip()
        normalized_file = _normalized_file_filter(file_path)
        rows = sorted(
            rows,
            key=lambda row: (
                0
                if row[2] == exact_file
                else 1
                if str(row[2]).replace("\\", "/") == normalized_file
                else 2,
                row[3] or 0,
            ),
        )[:1]

    results = []
    for row in rows[:3]:  # Max 3 sonuç
        name_r, type_r, file_r, start, end, params_r, docstring, signature = row

        # Kaynak kodu oku
        content = ""
        token_estimate = 0
        file_path = root / file_r
        if file_path.exists():
            try:
                lines = file_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                # Bitiş satırını bul (basit heuristic)
                actual_end = min(end or start + 50, len(lines))
                # Fonksiyon bitişini daha doğru bul
                if type_r == "function":
                    base_indent = len(lines[start-1]) - len(lines[start-1].lstrip())
                    for i in range(start, min(start + 200, len(lines))):
                        line = lines[i]
                        if line.strip() and len(line) - len(line.lstrip()) <= base_indent and i > start:
                            actual_end = i
                            break

                content = "\n".join(lines[start-1:actual_end])
                token_estimate = int(len(content.split()) * 1.3)
            except Exception as e:
                content = f"[Dosya okunamadı: {e}]"

        results.append({
            "name": name_r,
            "type": type_r,
            "file": file_r,
            "lines": f"{start}-{end}",
            "signature": signature,
            "params": params_r,
            "docstring": docstring,
            "content": content,
            "token_estimate": token_estimate
        })

    conn.close()

    if len(results) == 1:
        print(json.dumps(results[0], indent=2, ensure_ascii=False))
    else:
        print(json.dumps({"results": results, "count": len(results)}, indent=2, ensure_ascii=False))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="Sembol adı (ör: AuthService veya AuthService.login)")
    parser.add_argument("--fuzzy", action="store_true", help="Benzer isimleri de bul")
    parser.add_argument("--type", dest="sym_type", help="Filtrele: function, class, interface")
    parser.add_argument("--file", dest="file_path", help="Sembolü belirli bir dosya ile sınırla")
    args = parser.parse_args()
    get_symbol(args.name, args.fuzzy, args.sym_type, args.file_path)

if __name__ == "__main__":
    main()
