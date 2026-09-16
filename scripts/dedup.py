#!/usr/bin/env python3
"""
dedup.py — Context deduplication.
Aynı sembol/dosya parçasını session içinde tekrar göndermeyi önler.

Kullanım:
  python dedup.py --add "AuthService" "src/auth/service.py" 149
  python dedup.py --check "AuthService"
  python dedup.py --list
  python dedup.py --clear
  python dedup.py --stats
"""

import sys, json, sqlite3, hashlib, argparse
from pathlib import Path
from datetime import datetime

try:  # run directly: `python dedup.py` (script dir on sys.path)
    from paths import find_context_dir
except ImportError:  # imported as a package: `from scripts import dedup`
    from scripts.paths import find_context_dir

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def scope_key():
    try:
        from paths import resolve_runtime_scope
    except ImportError:
        from scripts.paths import resolve_runtime_scope
    return resolve_runtime_scope()

def scoped_key(key):
    return f"{scope_key()}::{key}"

def init_dedup_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_context (
            key TEXT PRIMARY KEY,
            scope TEXT DEFAULT 'default',
            label TEXT,
            file TEXT,
            token_cost INTEGER,
            added_at TEXT,
            hit_count INTEGER DEFAULT 0
        )
    """)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(session_context)").fetchall()]
    if "scope" not in cols:
        conn.execute("ALTER TABLE session_context ADD COLUMN scope TEXT DEFAULT 'default'")
    if "file_hash" not in cols:
        conn.execute("ALTER TABLE session_context ADD COLUMN file_hash TEXT DEFAULT ''")
        
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_context_scope ON session_context(scope)")
    conn.commit()

def get_file_hash(file_path):
    try:
        if not file_path:
            return ""
        path_obj = Path(file_path)
        # Handle relative/absolute properly by trying to resolve from current or root
        if not path_obj.is_absolute():
            _, root = find_context_dir()
            if root:
                path_obj = root / file_path
        
        if path_obj.exists() and path_obj.is_file():
            return hashlib.md5(path_obj.read_bytes()).hexdigest()
    except Exception:
        pass
    return ""

def add_to_context(key, label, file_path, token_cost):
    context_dir, _ = find_context_dir()
    if not context_dir:
        print(json.dumps({"error": "Index bulunamadı"})); return

    conn = sqlite3.connect(str(context_dir / "symbols.db"), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    init_dedup_table(conn)

    key = scoped_key(key)
    scope = scope_key()
    current_hash = get_file_hash(file_path)

    # Zaten var mı?
    existing = conn.execute(
        "SELECT hit_count, token_cost, file_hash FROM session_context WHERE key=?", (key,)
    ).fetchone()

    if existing:
        existing_hash = existing[2] or ""
        # Hash kontrolü
        if existing_hash and current_hash and existing_hash != current_hash:
            # Hash değişmişse duplicate sayma, veriyi güncelle ve 'updated' dön
            conn.execute(
                "UPDATE session_context SET hit_count=hit_count+1, file_hash=?, token_cost=?, added_at=? WHERE key=?",
                (current_hash, token_cost, datetime.now().isoformat(), key)
            )
            conn.commit()
            print(json.dumps({
                "status": "updated",
                "key": key,
                "label": label,
                "token_cost": token_cost,
                "message": f"'{label}' değiştiği için bağlama yeniden ekleniyor."
            }, indent=2))
        else:
            # Hash aynı veya hash bulamadıysa klasik duplicate işlemi
            conn.execute(
                "UPDATE session_context SET hit_count=hit_count+1 WHERE key=?", (key,)
            )
            conn.commit()
            print(json.dumps({
                "status": "duplicate",
                "key": key,
                "tokens_saved": existing[1],
                "hit_count": existing[0] + 1,
                "message": f"'{label}' zaten bu session'da gönderildi. {existing[1]} token tasarruf edildi."
            }, indent=2))
    else:
        conn.execute("""
            INSERT INTO session_context (key, scope, label, file, token_cost, added_at, file_hash)
            VALUES (?,?,?,?,?,?,?)
        """, (key, scope, label, file_path, token_cost, datetime.now().isoformat(), current_hash))
        conn.commit()
        print(json.dumps({
            "status": "added",
            "key": key,
            "label": label,
            "token_cost": token_cost
        }, indent=2))
    conn.close()

def check_context(key):
    context_dir, _ = find_context_dir()
    if not context_dir:
        print(json.dumps({"exists": False})); return

    conn = sqlite3.connect(str(context_dir / "symbols.db"), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    init_dedup_table(conn)

    key = scoped_key(key)
    row = conn.execute(
        "SELECT label, file, token_cost, added_at, hit_count FROM session_context WHERE key=?",
        (key,)
    ).fetchone()

    if row:
        print(json.dumps({
            "exists": True,
            "label": row[0],
            "file": row[1],
            "token_cost": row[2],
            "added_at": row[3],
            "hit_count": row[4]
        }, indent=2))
    else:
        print(json.dumps({"exists": False, "key": key}))
    conn.close()

def list_context():
    context_dir, _ = find_context_dir()
    if not context_dir:
        print(json.dumps({"items": []})); return

    conn = sqlite3.connect(str(context_dir / "symbols.db"), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    init_dedup_table(conn)

    scope = scope_key()
    rows = conn.execute("""
        SELECT key, label, file, token_cost, hit_count
        FROM session_context
        WHERE scope=?
        ORDER BY added_at
    """, (scope,)).fetchall()

    items = [{"key": r[0], "label": r[1], "file": r[2], "tokens": r[3], "hits": r[4]} for r in rows]
    total = sum(r[3] for r in rows)
    saved = sum(r[3] * r[4] for r in rows if r[4] > 0)

    print(json.dumps({
        "items": items,
        "total_items": len(items),
        "total_tokens_in_context": total,
        "tokens_saved_by_dedup": saved
    }, indent=2))
    conn.close()

def clear_context():
    context_dir, _ = find_context_dir()
    if not context_dir:
        return

    scope = scope_key()
    conn = sqlite3.connect(str(context_dir / "symbols.db"), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    init_dedup_table(conn)
    count = conn.execute("SELECT COUNT(*) FROM session_context WHERE scope=?", (scope,)).fetchone()[0]
    conn.execute("DELETE FROM session_context WHERE scope=?", (scope,))
    conn.commit()
    print(json.dumps({"cleared": count, "status": "ok"}))
    conn.close()

def stats():
    context_dir, _ = find_context_dir()
    if not context_dir:
        print(json.dumps({"error": "Index bulunamadı"})); return

    scope = scope_key()
    conn = sqlite3.connect(str(context_dir / "symbols.db"), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    init_dedup_table(conn)

    rows = conn.execute("""
        SELECT token_cost, hit_count FROM session_context
        WHERE scope=?
    """, (scope,)).fetchall()

    total_tokens = sum(r[0] for r in rows)
    total_saved  = sum(r[0] * r[1] for r in rows)
    duplicates   = sum(1 for r in rows if r[1] > 0)

    print(json.dumps({
        "items_in_context": len(rows),
        "total_tokens_sent": total_tokens,
        "tokens_saved": total_saved,
        "duplicate_hits": duplicates,
        "efficiency_pct": round(total_saved / max(total_tokens + total_saved, 1) * 100, 1)
    }, indent=2))
    conn.close()

def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", nargs=3, metavar=("KEY","FILE","TOKENS"),
                       help="Context'e ekle")
    group.add_argument("--check", metavar="KEY", help="Var mı kontrol et")
    group.add_argument("--list", action="store_true")
    group.add_argument("--clear", action="store_true")
    group.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    if args.add:
        key, file_path, tokens = args.add
        add_to_context(key, key, file_path, int(tokens))
    elif args.check:
        check_context(args.check)
    elif args.list:
        list_context()
    elif args.clear:
        clear_context()
    elif args.stats:
        stats()

if __name__ == "__main__":
    main()
