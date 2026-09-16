#!/usr/bin/env python3
"""
content_index.py — İçerik düzeyi (chunk tabanlı) arama indeksi (Phase 1b).

 Sembol indeksi sadece isim/imza/docstring bilir; görev "idempotency mantığı
 nerede?" gibi GÖVDE içeriği sorduğunda lexical arama kör kalır. Bu modül
 dosyaları deterministik parçalara (chunk) böler ve FTS5 ile aranabilir yapar:

  * Python: AST ile fonksiyon/sınıf/modül-başı chunk'ları
  * JS/TS : regex ile function/class/interface chunk'ları
  * Diğer : kayan pencere (30 satır, 20 adım) + başlık bölümleri

 Her chunk metadata taşır: file, kind, name, start/end line, token maliyeti.
 Arama sonucu chunk + get_range komutu döner → model sadece ilgili bölümü
 genişletir (progressive disclosure).

CLI:
  python content_index.py --build [--file P] [--force]
  python content_index.py --search "duplicate job protection" [--limit 8]

Importable API: ensure_schema / replace_file_chunks / build_all / search_chunks
"""

import argparse
import ast
import json
import re
import sqlite3
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from paths import find_context_dir
except ImportError:
    from scripts.paths import find_context_dir

WINDOW_LINES = 30
WINDOW_STRIDE = 20
CHUNK_MAX_CHARS = 6000          # FTS'ye giren metin üst sınırı
SNIPPET_CHARS = 240

CHUNKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS content_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT DEFAULT '',
    start_line INTEGER,
    end_line INTEGER,
    token_estimate INTEGER DEFAULT 0,
    text TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON content_chunks(file);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(name, text);
CREATE TABLE IF NOT EXISTS chunks_meta (
    file TEXT PRIMARY KEY,
    hash TEXT,
    chunked_at TEXT
);
"""

_JS_CHUNK_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?"
    r"(async\s+function|function|class|interface|enum|const)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)


def ensure_schema(conn):
    conn.executescript(CHUNKS_SCHEMA)
    conn.commit()


def _token_est(text):
    return int(len(text.split()) * 1.3)


def _cap(text):
    if len(text) <= CHUNK_MAX_CHARS:
        return text
    return text[:CHUNK_MAX_CHARS]


def _lines(content):
    return content.splitlines()


def _join(lines_list, start, end):
    return "\n".join(lines_list[start - 1:end])


# ── chunk üreticiler ───────────────────────────────────────────────────

def chunk_python(content, rel_path):
    """AST tabanlı chunk'lar: modül başı + her fonksiyon/sınıf."""
    chunks = []
    lines = _lines(content)
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return chunk_windows(content)

    defs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.append(node)
    defs.sort(key=lambda n: n.lineno)

    # Modül başı: ilk tanıma kadar (importlar + modül docstring'i).
    first_line = defs[0].lineno if defs else len(lines) + 1
    header_end = max(1, min(first_line - 1, len(lines)))
    header_text = _join(lines, 1, header_end).strip()
    if header_text:
        chunks.append({
            "kind": "module", "name": rel_path,
            "start_line": 1, "end_line": header_end,
            "text": _cap(header_text),
        })

    for node in defs:
        start = node.lineno
        end = getattr(node, "end_lineno", None) or min(start + 200, len(lines))
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        text = _join(lines, start, end)
        chunks.append({
            "kind": kind, "name": node.name,
            "start_line": start, "end_line": end,
            "text": _cap(text),
        })
    return chunks


def chunk_js(content, rel_path):
    """Regex tabanlı JS/TS chunk'ları; tutmazsa pencereye düş."""
    lines = _lines(content)
    matches = list(_JS_CHUNK_RE.finditer(content))
    if not matches:
        return chunk_windows(content)

    chunks = []
    line_starts = [0]
    for i, ln in enumerate(lines):
        line_starts.append(line_starts[-1] + len(ln) + 1)

    def offset_to_line(off):
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if line_starts[mid] <= off:
                lo = mid + 1
            else:
                hi = mid
        return max(1, lo)

    # Dosya başı (ilk tanıma kadar)
    first_off = matches[0].start()
    header_end = offset_to_line(first_off) - 1
    if header_end >= 1:
        header_text = _join(lines, 1, header_end).strip()
        if header_text:
            chunks.append({
                "kind": "module", "name": rel_path,
                "start_line": 1, "end_line": header_end,
                "text": _cap(header_text),
            })

    for i, m in enumerate(matches):
        start = offset_to_line(m.start())
        next_off = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        end = min(offset_to_line(next_off) - 1, len(lines)) if i + 1 < len(matches) else len(lines)
        if end < start:
            end = start
        kind = "class" if m.group(1) in ("class", "interface", "enum") else "function"
        text = _join(lines, start, end)
        chunks.append({
            "kind": kind, "name": m.group(2),
            "start_line": start, "end_line": end,
            "text": _cap(text),
        })
    return chunks


def chunk_windows(content):
    """Dil-bağımsız kayan pencere chunk'ları (md/config/fallback)."""
    lines = _lines(content)
    chunks = []
    if not lines:
        return chunks
    i = 0
    n = 0
    while i < len(lines):
        end = min(i + WINDOW_LINES, len(lines))
        text = "\n".join(lines[i:end]).strip()
        if text:
            n += 1
            chunks.append({
                "kind": "window", "name": f"lines {i + 1}-{end}",
                "start_line": i + 1, "end_line": end,
                "text": _cap(text),
            })
        if end >= len(lines):
            break
        i += WINDOW_STRIDE
    return chunks


def chunk_file(content, rel_path, lang):
    if lang == "python":
        return chunk_python(content, rel_path)
    if lang in ("javascript", "typescript"):
        return chunk_js(content, rel_path)
    return chunk_windows(content)


# ── indeks yazımı ──────────────────────────────────────────────────────

def replace_file_chunks(conn, rel_path, content, lang, file_hash="", *,
                        ensure=True, commit=True):
    """Bir dosyanın chunk'larını yeniden yaz (idempotent)."""
    if ensure:
        ensure_schema(conn)
    chunks = chunk_file(content, rel_path, lang)
    _delete_file_chunks(conn, rel_path)
    for ch in chunks:
        cur = conn.execute(
            "INSERT INTO content_chunks (file, kind, name, start_line, end_line, "
            "token_estimate, text) VALUES (?,?,?,?,?,?,?)",
            (rel_path, ch["kind"], ch["name"], ch["start_line"], ch["end_line"],
             _token_est(ch["text"]), ch["text"]),
        )
        conn.execute(
            "INSERT INTO chunks_fts (rowid, name, text) VALUES (?,?,?)",
            (cur.lastrowid, ch["name"], ch["text"]),
        )
    conn.execute(
        "INSERT INTO chunks_meta (file, hash, chunked_at) VALUES (?,?,?) "
        "ON CONFLICT(file) DO UPDATE SET hash=excluded.hash, chunked_at=excluded.chunked_at",
        (rel_path, file_hash or "", datetime.now().isoformat()),
    )
    if commit:
        conn.commit()
    return len(chunks)


def _delete_file_chunks(conn, rel_path):
    rows = conn.execute(
        "SELECT id FROM content_chunks WHERE file=?", (rel_path,)
    ).fetchall()
    for (cid,) in rows:
        conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
    conn.execute("DELETE FROM content_chunks WHERE file=?", (rel_path,))
    conn.execute("DELETE FROM chunks_meta WHERE file=?", (rel_path,))


def build_all(root, conn, force=False, only_file=None):
    """files tablosundaki tüm (veya tek) dosya için chunk indeksini güncelle."""
    ensure_schema(conn)
    query = "SELECT path, lang, hash FROM files"
    params = ()
    if only_file:
        query += " WHERE path=?"
        params = (only_file.replace("\\", "/"),)
    built, skipped = 0, 0
    for rel_path, lang, file_hash in conn.execute(query, params).fetchall():
        if not force:
            meta = conn.execute(
                "SELECT hash FROM chunks_meta WHERE file=?", (rel_path,)
            ).fetchone()
            if meta and meta[0] == file_hash:
                skipped += 1
                continue
        full = root / rel_path
        try:
            content = full.read_text(encoding="utf-8-sig", errors="replace")
        except Exception:
            continue
        replace_file_chunks(
            conn, rel_path, content, lang or "unknown", file_hash or "",
            ensure=False, commit=False,
        )
        built += 1
    conn.commit()
    return {"built": built, "skipped_unchanged": skipped}


# ── arama ──────────────────────────────────────────────────────────────

def _fts_match_expr(query):
    words = [w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query) if len(w) >= 2][:10]
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in words)


def search_chunks(conn, query, limit=8):
    """Chunk düzeyi içerik araması. FTS5 (BM25) + LIKE fallback."""
    ensure_schema(conn)
    results = []
    match_expr = _fts_match_expr(query)
    if match_expr:
        try:
            rows = conn.execute(
                """
                SELECT c.file, c.kind, c.name, c.start_line, c.end_line,
                       c.token_estimate,
                       snippet(chunks_fts, 1, '', '', ' … ', 16) AS snip,
                       bm25(chunks_fts) AS rank
                FROM chunks_fts f
                JOIN content_chunks c ON c.id = f.rowid
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match_expr, limit * 2),
            ).fetchall()
            results = [dict(zip(
                ("file", "kind", "name", "start_line", "end_line",
                 "token_estimate", "snippet", "rank"), r)) for r in rows]
        except sqlite3.OperationalError:
            results = []

    if len(results) < limit:
        like = f"%{query.strip()[:60]}%"
        seen = {(r["file"], r["start_line"]) for r in results}
        for row in conn.execute(
            """
            SELECT file, kind, name, start_line, end_line, token_estimate, text
            FROM content_chunks WHERE text LIKE ? OR name LIKE ? LIMIT ?
            """,
            (like, like, limit * 2),
        ).fetchall():
            key = (row[0], row[3])
            if key in seen:
                continue
            seen.add(key)
            text = row[6] or ""
            pos = text.lower().find(query.strip().lower()[:60])
            snip = text[max(0, pos - 60):pos + SNIPPET_CHARS] if pos >= 0 else text[:SNIPPET_CHARS]
            results.append({
                "file": row[0], "kind": row[1], "name": row[2],
                "start_line": row[3], "end_line": row[4],
                "token_estimate": row[5], "snippet": snip.strip(),
                "rank": 999.0,
            })

    out = []
    for r in results[:limit]:
        r = dict(r)
        r["get_cmd"] = (
            f"python .context/scripts/get_range.py {r['file']} "
            f"{r['start_line']} {r['end_line']}"
        )
        r.pop("rank", None)
        out.append(r)
    return out


# ── CLI ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Chunk-level content index")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build", action="store_true")
    group.add_argument("--search", metavar="QUERY")
    parser.add_argument("--file", help="build: tek dosya; search: dosya filtresi")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    context_dir, root = find_context_dir()
    if not context_dir:
        print(json.dumps({"error": "Index bulunamadı. Önce: python .context/scripts/index.py"}))
        sys.exit(1)

    conn = sqlite3.connect(str(context_dir / "symbols.db"), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        if args.build:
            result = build_all(root, conn, force=args.force, only_file=args.file)
            print(json.dumps(result))
        else:
            results = search_chunks(conn, args.search, limit=args.limit)
            if args.file:
                needle = args.file.replace("\\", "/")
                results = [r for r in results if r["file"] == needle]
            print(json.dumps({
                "query": args.search,
                "count": len(results),
                "results": results,
            }, indent=2, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
