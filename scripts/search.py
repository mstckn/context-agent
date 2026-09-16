#!/usr/bin/env python3
"""
search.py — Codebase'de arama yapar.

Kullanım:
  python search.py "rate limiting"
  python search.py "authentication" --type function
  python search.py "payment" --file-only
  python search.py "login" --limit 20
"""

import sys, json, sqlite3, re, argparse
from pathlib import Path

try:  # run directly: `python search.py` (script dir on sys.path)
    from paths import find_context_dir
except ImportError:  # imported as a package: `from scripts import search`
    from scripts.paths import find_context_dir

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def score_result(row, query_words):
    """Basit relevance skoru"""
    name, sym_type, file_r, start, docstring, signature = row
    score = 0
    text = f"{name} {docstring} {signature} {file_r}".lower()
    for word in query_words:
        word_lower = word.lower()
        if word_lower in name.lower():
            score += 10  # İsimde geçiyorsa yüksek skor
        if word_lower in text:
            score += 3
        if word_lower in file_r.lower():
            score += 5  # Dosya adında geçiyorsa
    return score

def _content_results(conn, query, limit):
    """Chunk (gövde içeriği) araması — sembol araması kör kaldığında devreye girer."""
    try:
        import content_index
    except ImportError:
        try:
            from scripts import content_index
        except ImportError:
            return []
    try:
        rows = content_index.search_chunks(conn, query, limit=limit)
    except Exception:
        return []
    out = []
    for r in rows:
        out.append({
            "type": "chunk",
            "kind": r.get("kind", ""),
            "name": r.get("name", ""),
            "file": r.get("file", ""),
            "line": r.get("start_line"),
            "end_line": r.get("end_line"),
            "snippet": (r.get("snippet") or "")[:200],
            "token_estimate": r.get("token_estimate", 0),
            "get_cmd": r.get("get_cmd", ""),
        })
    return out


def search(query, sym_type=None, file_only=False, limit=10, content_only=False):
    context_dir, root = find_context_dir()
    if not context_dir:
        print(json.dumps({"error": "Index bulunamadı"}))
        sys.exit(1)

    conn = sqlite3.connect(str(context_dir / "symbols.db"), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    query_words = query.lower().split()
    results = []

    if content_only:
        results = _content_results(conn, query, limit)
        print(json.dumps({"query": query, "count": len(results),
                          "results": results}, indent=2, ensure_ascii=False))
        return

    if file_only:
        # Sadece dosya arama
        try:
            rows = conn.execute("""
                SELECT path, lang, line_count, summary
                FROM files_fts
                JOIN files ON files.rowid = files_fts.rowid
                WHERE files_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (f"{query}*", limit)).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute("""
                SELECT path, lang, line_count, summary
                FROM files
                WHERE summary LIKE ? OR path LIKE ?
                LIMIT ?
            """, (f"%{query}%", f"%{query}%", limit)).fetchall()

        for path, lang, lines, summary in rows:
            results.append({
                "type": "file",
                "file": path,
                "lang": lang,
                "lines": lines,
                "summary": summary
            })
    else:
        # FTS araması dene
        fts_results = []
        try:
            fts_query = " OR ".join(query_words)
            fts_rows = conn.execute("""
                SELECT s.name, s.type, s.file, s.start_line, s.docstring, s.signature
                FROM symbols_fts f
                JOIN symbols s ON f.rowid = s.id
                WHERE symbols_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, limit)).fetchall()
            fts_results = fts_rows
        except Exception:
            pass

        # LIKE araması (FTS yoksa veya yetersizse)
        like_results = []
        conditions = []
        params = []
        for word in query_words:
            conditions.append("(name LIKE ? OR docstring LIKE ? OR signature LIKE ?)")
            params.extend([f"%{word}%", f"%{word}%", f"%{word}%"])

        where = " AND ".join(conditions) if conditions else "1=1"
        if sym_type:
            where += " AND type = ?"
            params.append(sym_type)

        like_rows = conn.execute(f"""
            SELECT name, type, file, start_line, docstring, signature
            FROM symbols WHERE {where} LIMIT ?
        """, params + [limit * 2]).fetchall()

        # Birleştir ve skorla
        seen = set()
        all_rows = list(fts_results) + like_rows
        scored = []
        for row in all_rows:
            key = (row[0], row[2])
            if key not in seen:
                seen.add(key)
                scored.append((score_result(row, query_words), row))

        scored.sort(key=lambda x: -x[0])

        for score, (name, stype, file_r, start, docstring, signature) in scored[:limit]:
            if score == 0:
                continue
            results.append({
                "name": name,
                "type": stype,
                "file": file_r,
                "line": start,
                "docstring": docstring[:150] if docstring else "",
                "signature": signature,
                "relevance": score,
                "get_cmd": f"python .context/scripts/get_symbol.py {name}"
            })

    output = {
        "query": query,
        "count": len(results),
        "results": results
    }

    if not results:
        # Sembol araması boş döndüyse gövde içeriğinde ara (hybrid fallback).
        fallback = _content_results(conn, query, limit)
        if fallback:
            output["results"] = fallback
            output["count"] = len(fallback)
            output["source"] = "content_chunks"
        else:
            output["hint"] = f"Bulunamadı. Dene: python .context/scripts/search.py \"{query}\" --file-only"

    print(json.dumps(output, indent=2, ensure_ascii=False))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="Arama sorgusu")
    parser.add_argument("--type", dest="sym_type", help="function, class, interface")
    parser.add_argument("--file-only", action="store_true")
    parser.add_argument("--content", action="store_true",
                        help="Sadece gövde içeriğinde (chunk) ara")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    search(args.query, args.sym_type, args.file_only, args.limit,
           content_only=args.content)

if __name__ == "__main__":
    main()
