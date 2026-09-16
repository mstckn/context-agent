#!/usr/bin/env python3
"""
index.py — Projeyi tarar, .context/ dizinini oluşturur/günceller.

Kullanım:
  python index.py                    # mevcut dizini indexle
  python index.py /path/to/project   # belirtilen dizini indexle
  python index.py --file src/auth.py # tek dosyayı güncelle
  python index.py --stats            # index istatistiklerini göster
"""

import sys, os, json, sqlite3, hashlib, re, ast, time, argparse, fnmatch
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime

# ─── Konfigürasyon ──────────────────────────────────────────────────────────

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_IGNORE = {
    # NOT: "migrations" ve "fixtures" bilinçli olarak ENGELLENMİYOR;
    # şema/contract bilgisi DATABASE_CHANGE görevleri için kritiktir.
    "dirs":  {"node_modules",".git","__pycache__",".venv","venv","dist",
               "build",".next","out","target","vendor",".cache",".idea",
               ".vscode",".context",".pytest_cache","coverage","__snapshots__"},
    "exts":  {".pyc",".pyo",".class",".o",".so",".dylib",".dll",".exe",
               ".png",".jpg",".jpeg",".gif",".ico",".svg",".woff",".woff2",
               ".ttf",".eot",".pdf",".zip",".tar",".gz",".lock",".min.js",
               ".min.css",".map"},
    "files": {"package-lock.json","yarn.lock","Cargo.lock","poetry.lock",
               ".DS_Store","Thumbs.db"}
}

SUPPORTED_EXTS = {
    ".py":   "python",
    ".js":   "javascript",
    ".ts":   "typescript",
    ".jsx":  "javascript",
    ".tsx":  "typescript",
    ".go":   "go",
    ".rs":   "rust",
    ".java": "java",
    ".rb":   "ruby",
    ".php":  "php",
    ".cs":   "csharp",
    ".cpp":  "cpp",
    ".c":    "c",
    ".swift":"swift",
    ".kt":   "kotlin",
    ".md":   "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml":  "yaml",
    ".toml": "toml",
    ".ini":  "config",
    ".cfg":  "config",
    ".sql":  "sql",
    ".xml":  "xml",
    ".properties": "config",
}

# ─── Helpers ────────────────────────────────────────────────────────────────

def file_hash(path):
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except:
        return ""

def count_tokens(text):
    """Approximate token count.

    Delegates to :mod:`scripts.token_count` which prefers ``tiktoken`` if
    it is installed and falls back to a language-aware heuristic
    otherwise. The function never raises.
    """
    try:
        from scripts.token_count import count_tokens as _impl
    except ImportError:
        from token_count import count_tokens as _impl  # type: ignore[no-redef]
    return _impl(text)

def read_aiignore(root):
    """`.aiignore` dosyasını oku, pattern listesi döndür"""
    ignore_file = Path(root) / ".aiignore"
    patterns = set()
    if ignore_file.exists():
        for line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.add(line.rstrip("/"))
    return patterns

def should_ignore(path, root, extra_ignores):
    rel = Path(path).relative_to(root)
    parts = rel.parts
    rel_posix = rel.as_posix()

    for part in parts:
        if part in DEFAULT_IGNORE["dirs"]:
            return True
        if part in extra_ignores:
            return True

    for pattern in extra_ignores:
        if not pattern:
            continue
        normalized = pattern.replace("\\", "/").rstrip("/")
        if rel_posix == normalized or rel_posix.startswith(normalized + "/"):
            return True
        if fnmatch.fnmatch(rel_posix, normalized) or fnmatch.fnmatch(rel.name, normalized):
            return True
        if "/" not in normalized and any(fnmatch.fnmatch(part, normalized) for part in parts):
            return True

    if rel.name in DEFAULT_IGNORE["files"]:
        return True

    suffix = Path(path).suffix.lower()
    if suffix in DEFAULT_IGNORE["exts"]:
        return True

    # Çok büyük dosyaları atla (>500KB)
    try:
        if Path(path).stat().st_size > 500_000:
            return True
    except:
        pass

    return False

# ─── Sembol Çıkarıcılar ─────────────────────────────────────────────────────

def extract_python_symbols(content, filepath):
    symbols = []
    try:
        tree = ast.parse(content)
        lines = content.splitlines()

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Parametreleri al
                params = []
                for arg in node.args.args:
                    params.append(arg.arg)

                # Docstring
                docstring = ast.get_docstring(node) or ""

                # Dekoratörler
                decorators = []
                for d in node.decorator_list:
                    if isinstance(d, ast.Name):
                        decorators.append(d.id)
                    elif isinstance(d, ast.Attribute):
                        decorators.append(f"{d.value.id}.{d.attr}" if hasattr(d.value, 'id') else d.attr)

                end_line = getattr(node, 'end_lineno', node.lineno + 10)

                symbols.append({
                    "name": node.name,
                    "type": "function",
                    "file": filepath,
                    "start_line": node.lineno,
                    "end_line": end_line,
                    "params": ", ".join(params),
                    "docstring": docstring[:200],
                    "decorators": decorators,
                    "signature": f"def {node.name}({', '.join(params)})"
                })

            elif isinstance(node, ast.ClassDef):
                end_line = getattr(node, 'end_lineno', node.lineno + 50)
                methods = []
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.append(item.name)

                symbols.append({
                    "name": node.name,
                    "type": "class",
                    "file": filepath,
                    "start_line": node.lineno,
                    "end_line": end_line,
                    "params": "",
                    "docstring": (ast.get_docstring(node) or "")[:200],
                    "decorators": [],
                    "signature": f"class {node.name}",
                    "methods": methods
                })
    except SyntaxError:
        pass
    return symbols

def _extract_js_ts_symbols_legacy(content, filepath):
    symbols = []
    lines = content.splitlines()

    # function declarations
    fn_pattern = re.compile(
        r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)',
        re.MULTILINE
    )
    # arrow functions / const
    arrow_pattern = re.compile(
        r'^(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\(?([^)=]*)\)?\s*=>',
        re.MULTILINE
    )
    # class declarations
    class_pattern = re.compile(
        r'^(?:export\s+)?class\s+(\w+)',
        re.MULTILINE
    )
    # interface/type
    interface_pattern = re.compile(
        r'^(?:export\s+)?(?:interface|type)\s+(\w+)',
        re.MULTILINE
    )

    for pattern, sym_type in [
        (fn_pattern, "function"),
        (arrow_pattern, "function"),
        (class_pattern, "class"),
        (interface_pattern, "interface"),
    ]:
        for m in pattern.finditer(content):
            line_num = content[:m.start()].count('\n') + 1
            name = m.group(1)
            params = m.group(2).strip() if m.lastindex >= 2 else ""
            symbols.append({
                "name": name,
                "type": sym_type,
                "file": filepath,
                "start_line": line_num,
                "end_line": line_num + 20,  # JS için basit tahmin
                "params": params,
                "docstring": "",
                "decorators": [],
                "signature": f"{sym_type} {name}({params})"
            })

    return symbols

def estimate_js_block_end(lines, start_line, max_scan=260):
    """Best-effort JS/TS block end finder using brace balance."""
    idx = max(0, int(start_line) - 1)
    limit = min(len(lines), idx + max_scan)
    depth = 0
    started = False
    in_multiline_comment = False

    for i in range(idx, limit):
        line = lines[i]
        
        # Handle multiline comments
        if in_multiline_comment:
            if "*/" in line:
                line = line.split("*/", 1)[1]
                in_multiline_comment = False
            else:
                continue
                
        while "/*" in line:
            if "*/" in line:
                # Remove inline multiline comment entirely
                line = re.sub(r'/\*.*?\*/', '', line, count=1)
            else:
                line = line.split("/*", 1)[0]
                in_multiline_comment = True
                break
                
        if "//" in line:
            line = line.split("//", 1)[0]
            
        line = re.sub(r'(\"([^\"\\\\]|\\\\.)*\"|\'([^\'\\\\]|\\\\.)*\'|`([^`\\\\]|\\\\.)*`)', "", line)

        open_count = line.count("{")
        close_count = line.count("}")
        if open_count:
            started = True
            depth += open_count
        if close_count and started:
            depth -= close_count
            if depth <= 0:
                return i + 1

    if started:
        return limit
    return min(len(lines), idx + 24)


def extract_js_ts_symbols(content, filepath):
    """Enhanced JS/TS symbol extraction with broader syntax coverage."""
    symbols = []
    lines = content.splitlines()
    seen = set()

    fn_pattern = re.compile(
        r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)',
        re.MULTILINE
    )
    arrow_pattern = re.compile(
        r'^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:<[^>]+>\s*)?\(?([^)=]*)\)?\s*=>',
        re.MULTILINE
    )
    class_pattern = re.compile(
        r'^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)',
        re.MULTILINE
    )
    interface_pattern = re.compile(
        r'^\s*(?:export\s+)?(?:default\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)',
        re.MULTILINE
    )
    method_pattern = re.compile(
        r'^\s{2,}(?:public\s+|private\s+|protected\s+|static\s+|readonly\s+|abstract\s+)*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{',
        re.MULTILINE
    )
    method_skip = {"if", "for", "while", "switch", "catch", "with", "return", "else", "try", "do", "function", "class"}

    for pattern, sym_type in [
        (fn_pattern, "function"),
        (arrow_pattern, "function"),
        (class_pattern, "class"),
        (interface_pattern, "interface"),
        (method_pattern, "method"),
    ]:
        for m in pattern.finditer(content):
            line_num = content[:m.start()].count("\n") + 1
            name = m.group(1)
            if sym_type == "method" and name in method_skip:
                continue
            params = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else ""
            key = (name, line_num, sym_type)
            if key in seen:
                continue
            seen.add(key)
            end_line = estimate_js_block_end(lines, line_num) if sym_type in {"function", "class", "method"} else min(len(lines), line_num + 12)
            symbols.append({
                "name": name,
                "type": sym_type,
                "file": filepath,
                "start_line": line_num,
                "end_line": end_line,
                "params": params,
                "docstring": "",
                "decorators": [],
                "signature": f"{sym_type} {name}({params})"
            })

    return symbols


def extract_imports(content, lang):
    imports = []
    if lang == "python":
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
        except SyntaxError:
            # Line-bounded fallback. ``\s`` is intentionally avoided because
            # it consumed subsequent import statements into one corrupt row.
            for m in re.finditer(
                    r'^(?:from\s+([\w.]+)\s+import|import\s+([^\r\n#]+))',
                    content, re.MULTILINE):
                imp = m.group(1) or m.group(2)
                if imp:
                    imports.extend(
                        part.strip().split(" as ", 1)[0]
                        for part in imp.split(",") if part.strip()
                    )
    elif lang in ("javascript", "typescript"):
        for m in re.finditer(r"(?:import|require)\s*[({'\"]?\s*(?:.*?from\s+)?['\"]([^'\"]+)['\"]", content):
            imports.append(m.group(1))
    return imports[:20]

def extract_symbols(content, filepath, lang):
    if lang == "python":
        return extract_python_symbols(content, filepath)
    elif lang in ("javascript", "typescript"):
        return extract_js_ts_symbols(content, filepath)
    else:
        return []

# ─── Özet Üretici ───────────────────────────────────────────────────────────

def generate_summary(content, filepath, symbols, lang):
    """Basit kural tabanlı özet — LLM gerekmez"""
    lines = content.splitlines()
    line_count = len(lines)
    ext = Path(filepath).suffix

    # İlk anlamlı yorum/docstring'i bul
    top_comment = ""
    for line in lines[:20]:
        stripped = line.strip()
        if stripped.startswith(("#", "//", "/*", '"""', "'''")):
            top_comment = stripped.lstrip("#/ *\"'")[:100]
            if top_comment:
                break

    sym_names = [s["name"] for s in symbols[:8]]
    sym_types = {}
    for s in symbols:
        sym_types[s["type"]] = sym_types.get(s["type"], 0) + 1

    type_str = ", ".join(f"{v} {k}" for k, v in sym_types.items())
    exports_str = ", ".join(sym_names[:5])
    if len(sym_names) > 5:
        exports_str += f" +{len(sym_names)-5} more"

    summary_parts = []
    if top_comment:
        summary_parts.append(top_comment)
    if type_str:
        summary_parts.append(f"Contains: {type_str}")
    if exports_str:
        summary_parts.append(f"Key symbols: {exports_str}")
    summary_parts.append(f"{line_count} lines, {lang}")

    return " | ".join(summary_parts)

def detect_project_commands(root):
    """Projedeki yaygın test/lint/dev komutlarını keşfet."""
    commands = {}
    root = Path(root)

    package_json = root / "package.json"
    if package_json.exists():
        try:
            pkg = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
            scripts = pkg.get("scripts", {})
            for name in ("test", "lint", "typecheck", "build", "dev"):
                if name in scripts:
                    commands[name] = f"npm run {name}"
        except Exception:
            pass

    pyproject = root / "pyproject.toml"
    if (root / "pytest.ini").exists() or pyproject.exists() or (root / "tests").exists():
        commands.setdefault("test", "python -m pytest")
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        if "[tool.ruff" in text:
            commands.setdefault("lint", "python -m ruff check .")
        if "[tool.mypy" in text:
            commands.setdefault("typecheck", "python -m mypy .")

    if (root / "go.mod").exists():
        commands.setdefault("test", "go test ./...")
    if (root / "Cargo.toml").exists():
        commands.setdefault("test", "cargo test")
        commands.setdefault("build", "cargo build")

    return commands

# ─── Veritabanı ─────────────────────────────────────────────────────────────

def init_db(context_dir):
    db_path = context_dir / "symbols.db"
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            hash TEXT,
            lang TEXT,
            line_count INTEGER,
            summary TEXT,
            indexed_at TEXT,
            token_estimate INTEGER
        );

        CREATE TABLE IF NOT EXISTS symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT,
            file TEXT,
            start_line INTEGER,
            end_line INTEGER,
            params TEXT,
            docstring TEXT,
            signature TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
        CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);
        CREATE INDEX IF NOT EXISTS idx_symbols_type ON symbols(type);

        CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
            name, file, docstring, signature,
            content='symbols', content_rowid='id'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
            path, summary,
            content='files', content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
            INSERT INTO symbols_fts(rowid, name, file, docstring, signature) 
            VALUES (new.id, new.name, new.file, new.docstring, new.signature);
        END;
        CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
            INSERT INTO symbols_fts(symbols_fts, rowid, name, file, docstring, signature) 
            VALUES ('delete', old.id, old.name, old.file, old.docstring, old.signature);
        END;
        CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols BEGIN
            INSERT INTO symbols_fts(symbols_fts, rowid, name, file, docstring, signature) 
            VALUES ('delete', old.id, old.name, old.file, old.docstring, old.signature);
            INSERT INTO symbols_fts(rowid, name, file, docstring, signature) 
            VALUES (new.id, new.name, new.file, new.docstring, new.signature);
        END;

        CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
            INSERT INTO files_fts(rowid, path, summary) 
            VALUES (new.rowid, new.path, new.summary);
        END;
        CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
            INSERT INTO files_fts(files_fts, rowid, path, summary) 
            VALUES ('delete', old.rowid, old.path, old.summary);
        END;
        CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
            INSERT INTO files_fts(files_fts, rowid, path, summary) 
            VALUES ('delete', old.rowid, old.path, old.summary);
            INSERT INTO files_fts(rowid, path, summary) 
            VALUES (new.rowid, new.path, new.summary);
        END;

        CREATE TABLE IF NOT EXISTS imports (
            file TEXT,
            imports_from TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file);
        CREATE INDEX IF NOT EXISTS idx_imports_from ON imports(imports_from);
    """)
    conn.commit()
    return conn

def rebuild_fts_indexes(conn):
    """Rebuild FTS tables from canonical content tables."""
    conn.execute("INSERT INTO symbols_fts(symbols_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")

# ─── Ana Indexleme ───────────────────────────────────────────────────────────

def index_file(filepath, root, conn, force=False, update_auxiliary=True,
               bulk_auxiliary=False, known_files=None, preloaded_content=None):
    rel_path = str(Path(filepath).relative_to(root)).replace("\\", "/")
    lang = SUPPORTED_EXTS.get(Path(filepath).suffix.lower(), "unknown")

    if lang == "unknown":
        return None

    try:
        raw_content = (Path(filepath).read_bytes()
                       if preloaded_content is None else preloaded_content)
        content = raw_content.decode("utf-8-sig", errors="replace")
    except Exception as e:
        return None

    new_hash = hashlib.md5(raw_content).hexdigest()

    # Hash kontrolü — değişmediyse atla
    if not force:
        row = conn.execute("SELECT hash FROM files WHERE path=?", (rel_path,)).fetchone()
        if row and row[0] == new_hash:
            return {"status": "skipped", "file": rel_path}

    # Sembolleri çıkar
    symbols = extract_symbols(content, rel_path, lang)
    imports = extract_imports(content, lang)
    summary = generate_summary(content, rel_path, symbols, lang)
    line_count = len(content.splitlines())
    token_est = count_tokens(content)

    # Eski kayıtları sil
    conn.execute("DELETE FROM symbols WHERE file=?", (rel_path,))
    conn.execute("DELETE FROM imports WHERE file=?", (rel_path,))

    # Yeni sembolleri ekle
    for sym in symbols:
        conn.execute(
            "INSERT INTO symbols (name,type,file,start_line,end_line,params,docstring,signature) VALUES (?,?,?,?,?,?,?,?)",
            (sym["name"], sym["type"], rel_path,
             sym["start_line"], sym["end_line"],
             sym.get("params",""), sym.get("docstring",""), sym.get("signature",""))
        )

    # Import ilişkileri
    for imp in imports:
        conn.execute("INSERT INTO imports VALUES (?,?)", (rel_path, imp))

    # Dosya kaydı
    conn.execute("""
        INSERT INTO files (path,hash,lang,line_count,summary,indexed_at,token_estimate)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET
            hash=excluded.hash,
            lang=excluded.lang,
            line_count=excluded.line_count,
            summary=excluded.summary,
            indexed_at=excluded.indexed_at,
            token_estimate=excluded.token_estimate
    """, (rel_path, new_hash, lang, line_count, summary,
          datetime.now().isoformat(), token_est))

    # İçerik chunk'ları (içerik düzeyi arama için)
    if update_auxiliary or bulk_auxiliary:
        try:
            import content_index
            content_index.replace_file_chunks(
                conn, rel_path, content, lang, new_hash,
                ensure=not bulk_auxiliary, commit=not bulk_auxiliary,
            )
        except Exception:
            pass

    # Yapısal graf kenarları (gerçek import çözümlemesi)
    if update_auxiliary or bulk_auxiliary:
        try:
            import graph as graph_mod
            known = known_files
            if known is None:
                known = {r[0] for r in conn.execute("SELECT path FROM files").fetchall()}
            graph_mod.build_file_edges(
                conn, rel_path, content, lang, new_hash, known,
                ensure=not bulk_auxiliary, commit=not bulk_auxiliary,
            )
        except Exception:
            pass

    return {
        "status": "indexed",
        "file": rel_path,
        "lang": lang,
        "symbols": len(symbols),
        "lines": line_count,
        "tokens": token_est
    }


def prefetch_file_bytes(paths, workers=8, window=32):
    """Read files concurrently with bounded memory and deterministic ordering."""
    paths = iter(paths)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pending = deque()

        def submit_one():
            try:
                path = next(paths)
            except StopIteration:
                return False
            pending.append((path, pool.submit(Path(path).read_bytes)))
            return True

        for _ in range(max(1, window)):
            if not submit_one():
                break
        while pending:
            path, future = pending.popleft()
            try:
                raw = future.result()
            except Exception:
                raw = None
            yield path, raw
            submit_one()


def purge_stale_records(root, conn, valid_paths):
    """Remove ignored, deleted, or non-canonical records after a full scan."""
    deleted = 0
    rows = conn.execute("SELECT path FROM files").fetchall()
    for (path,) in rows:
        normalized = str(path or "").replace("\\", "/")
        exists = (root / Path(normalized)).exists()
        if path != normalized or normalized not in valid_paths or not exists:
            conn.execute("DELETE FROM symbols WHERE file=?", (path,))
            conn.execute("DELETE FROM imports WHERE file=?", (path,))
            conn.execute("DELETE FROM files WHERE path=?", (path,))
            try:
                import content_index
                content_index.ensure_schema(conn)
                content_index._delete_file_chunks(conn, path)
            except Exception:
                pass
            try:
                import graph as graph_mod
                graph_mod.ensure_schema(conn)
                conn.execute("DELETE FROM edges WHERE src_file=?", (path,))
                conn.execute("DELETE FROM graph_meta WHERE file=?", (path,))
            except Exception:
                pass
            deleted += 1
    return deleted

def build_map(root, conn, context_dir):
    """map.json oluştur — LLM'in ana haritası"""
    files = conn.execute(
        "SELECT path,lang,line_count,summary,token_estimate FROM files ORDER BY path"
    ).fetchall()

    # Modüllere grupla
    modules = {}
    total_tokens = 0

    for path, lang, lines, summary, tokens in files:
        parts = Path(path).parts
        module = parts[0] if len(parts) > 1 else "root"

        if module not in modules:
            modules[module] = {"files": {}, "total_lines": 0, "total_tokens": 0}

        modules[module]["files"][path] = {
            "lang": lang,
            "lines": lines,
            "summary": summary,
            "tokens": tokens or 0
        }
        modules[module]["total_lines"] += (lines or 0)
        modules[module]["total_tokens"] += (tokens or 0)
        total_tokens += (tokens or 0)

    # Sembol sayıları
    sym_counts = conn.execute(
        "SELECT type, COUNT(*) FROM symbols GROUP BY type"
    ).fetchall()

    # Top semboller (en çok referans alınanlar)
    top_symbols = conn.execute("""
        SELECT s.name, s.type, s.file, s.start_line, s.params
        FROM symbols s
        ORDER BY s.name
        LIMIT 100
    """).fetchall()

    symbol_index = {}
    for name, stype, file, start, params in top_symbols:
        symbol_index[name] = {
            "type": stype,
            "file": file,
            "line": start,
            "params": params
        }

    map_data = {
        "generated_at": datetime.now().isoformat(),
        "root": str(root),
        "stats": {
            "total_files": len(files),
            "total_tokens": total_tokens,
            "symbol_counts": {t: c for t, c in sym_counts}
        },
        "scripts": {
            "get_symbol":    "python .context/scripts/get_symbol.py <name>",
            "search":        "python .context/scripts/search.py <query>",
            "get_range":     "python .context/scripts/get_range.py <file> <start> <end>",
            "get_related":   "python .context/scripts/get_related.py <file>",
            "compress_log":  "echo '<log>' | python .context/scripts/compress_log.py",
            "update_index":  "python .context/scripts/index.py --file <file>",
            "route":         "python .context/scripts/route.py <task>",
            "agent":         "python .context/scripts/agent.py <task>",
            "mcp_server":    "python .context/scripts/mcp_server.py"
        },
        "commands": detect_project_commands(root),
        "modules": modules,
        "symbols": symbol_index
    }

    map_path = context_dir / "map.json"
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump(map_data, f, indent=2, ensure_ascii=False)

    return map_data

# ─── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Context Indexer")
    parser.add_argument("root", nargs="?", default=".", help="Proje kök dizini")
    parser.add_argument("--file", nargs="+", help="Tek veya çoklu dosyayı indexle")
    parser.add_argument("--force", action="store_true", help="Hash kontrolünü atla")
    parser.add_argument("--stats", action="store_true", help="İstatistik göster")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    context_dir = root / ".context"
    context_dir.mkdir(exist_ok=True)

    # Alt dizinler
    (context_dir / "scripts").mkdir(exist_ok=True)
    (context_dir / "summaries").mkdir(exist_ok=True)
    (context_dir / "sessions").mkdir(exist_ok=True)

    conn = init_db(context_dir)
    extra_ignores = read_aiignore(root)

    if args.stats:
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        tokens = conn.execute("SELECT SUM(token_estimate) FROM files").fetchone()[0] or 0
        print(json.dumps({
            "files": files,
            "symbols": symbols,
            "total_tokens": tokens,
            "db_size_kb": int(Path(context_dir/"symbols.db").stat().st_size / 1024)
        }, indent=2))
        return

    if args.file:
        # Çoklu dosya güncelleme
        results = []
        for f in args.file:
            res = index_file(
                root / f, root, conn, force=args.force
            )
            results.append(res)
        
        conn.commit()
        rebuild_fts_indexes(conn)
        conn.commit()
        build_map(root, conn, context_dir)
        print(json.dumps(results if len(results) > 1 else results[0], indent=2))
        return

    # Tüm projeyi indexle
    start_time = time.time()
    results = {"indexed": 0, "skipped": 0, "errors": 0, "files": []}

    all_files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    )
    all_files = [
        filepath for filepath in all_files
        if not should_ignore(filepath, root, extra_ignores)
    ]
    valid_paths = {
        str(filepath.relative_to(root)).replace("\\", "/")
        for filepath in all_files
    }

    # Tam taramada ikincil indeks ÅŸemalarÄ±nÄ± bir kez hazÄ±rla. TÃ¼m dosya
    # yollarÄ± Ã¶nceden bilindiÄŸi iÃ§in graph da aynÄ± dosya okumasÄ±nda kurulabilir.
    import content_index
    import graph as graph_mod
    content_index.ensure_schema(conn)
    graph_mod.ensure_schema(conn)

    print(f"Taranıyor: {len(all_files)} dosya bulundu...", file=sys.stderr)

    for i, (filepath, raw_content) in enumerate(prefetch_file_bytes(all_files)):
        if raw_content is None:
            results["errors"] += 1
            continue
        result = index_file(
            filepath, root, conn, force=args.force, update_auxiliary=False,
            bulk_auxiliary=True, known_files=valid_paths,
            preloaded_content=raw_content,
        )
        if result is None:
            results["errors"] += 1
        elif result["status"] == "skipped":
            results["skipped"] += 1
        else:
            results["indexed"] += 1
            results["files"].append(result)

        # Progress
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(all_files)} işlendi...", file=sys.stderr)

    results["purged"] = purge_stale_records(root, conn, valid_paths)
    conn.commit()

    # FTS index'leri kanonik tablolardan yenile
    rebuild_fts_indexes(conn)
    conn.commit()

    # İçerik chunk indeksi (yalnızca değişen/yeni dosyalar)
    try:
        import content_index
        results["content_chunks"] = content_index.build_all(root, conn)
        conn.commit()
    except Exception:
        pass

    # Yapısal bağımlılık grafı (yalnızca değişen/yeni dosyalar)
    try:
        import graph as graph_mod
        results["graph"] = graph_mod.build_all(root, conn)
        conn.commit()
    except Exception:
        pass

    # map.json oluştur
    map_data = build_map(root, conn, context_dir)

    elapsed = time.time() - start_time
    results["elapsed_seconds"] = round(elapsed, 2)
    results["map_generated"] = True
    results["total_tokens"] = map_data["stats"]["total_tokens"]

    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
