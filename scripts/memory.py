#!/usr/bin/env python3
"""
memory.py - Project Memory 2.0 (spec PHASE 8).

Kapsül/session modelinin üstünde YAPILANDIRILMIŞ proje belleği:

  * Tipli bellek: ARCHITECTURAL, DECISION, TASK, CONSTRAINT, KNOWN_ISSUE,
    USER_CORRECTION, RUNTIME, EPHEMERAL.
  * Yaşam döngüsü: ACTIVE → SUPERSEDED / STALE / INVALIDATED.
    Geçerse (supersession) eski kayıt silinmez, işaretlenir → denetlenebilirlik.
  * Staleness doğrulaması: related_files hash/varlık kontrolü
    (deterministik; LLM yok).
  * Compaction: kısa ömürlü sınıflar (EPHEMERAL/RUNTIME) sınırlanır.
  * Project State Sheet: bağlama giren kompakt, bütçeli özet.

Kullanım:
  python memory.py --add --type DECISION --content "JWT kullanma" --source user
  python memory.py --list [--type DECISION] [--status ACTIVE]
  python memory.py --supersede OLD_ID NEW_ID
  python memory.py --status ID INVALIDATED
  python memory.py --verify
  python memory.py --compact [--max-per-type 8]
  python memory.py --sheet [--max-chars 4800]
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MEMORY_TYPES = (
    "ARCHITECTURAL", "DECISION", "TASK", "CONSTRAINT",
    "KNOWN_ISSUE", "USER_CORRECTION", "RUNTIME", "EPHEMERAL",
)

STATUSES = ("ACTIVE", "SUPERSEDED", "STALE", "INVALIDATED")

# State sheet sıralaması: bağlam için en değerli sınıflar önde.
SHEET_ORDER = (
    "CONSTRAINT", "DECISION", "ARCHITECTURAL", "USER_CORRECTION",
    "KNOWN_ISSUE", "TASK", "RUNTIME", "EPHEMERAL",
)

# Compaction yalnızca kısa ömürlü sınıfları budar.
COMPACTABLE_TYPES = ("EPHEMERAL", "RUNTIME")
DEFAULT_MAX_PER_TYPE = 8
DEFAULT_SHEET_MAX_CHARS = 4800
MAX_ITEMS_PER_TYPE_IN_SHEET = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source TEXT DEFAULT '',
    confidence REAL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    supersedes INTEGER,
    superseded_by INTEGER,
    related_files TEXT DEFAULT '',
    related_symbols TEXT DEFAULT '',
    file_hashes TEXT DEFAULT '',
    git_revision TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_verified_at TEXT DEFAULT '',
    UNIQUE(scope, memory_type, content_hash)
)
"""


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _hash_content(text):
    return hashlib.sha1(" ".join(str(text or "").split()).encode("utf-8")).hexdigest()


def _file_hash(path):
    try:
        p = Path(path)
        if p.exists() and p.is_file():
            return hashlib.md5(p.read_bytes()).hexdigest()
    except OSError:
        pass
    return ""


def _loads(text):
    try:
        data = json.loads(text or "[]")
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


class ProjectMemory:
    """SQLite destekli, deterministik proje belleği."""

    def __init__(self, db_path):
        self.conn = sqlite3.connect(str(db_path), timeout=10)
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute(_SCHEMA)
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ── yazma ────────────────────────────────────────────────────────

    def add(self, scope, memory_type, content, *, source="", confidence=0.5,
            related_files=None, related_symbols=None, git_revision="",
            supersedes=None):
        memory_type = str(memory_type or "").upper()
        if memory_type not in MEMORY_TYPES:
            return {"error": f"unknown_memory_type: {memory_type}"}
        content = " ".join(str(content or "").split())
        if not content:
            return {"error": "empty_content"}

        chash = _hash_content(content)
        files = [str(f) for f in (related_files or [])]
        symbols = [str(s) for s in (related_symbols or [])]
        file_hashes = {str(f): _file_hash(f) for f in files} if files else {}

        cur = self.conn.execute("""
            INSERT OR IGNORE INTO project_memory
            (scope, memory_type, content, content_hash, source, confidence, status,
             related_files, related_symbols, file_hashes, git_revision,
             created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (scope, memory_type, content, chash, source,
              max(0.0, min(1.0, float(confidence))), "ACTIVE",
              json.dumps(files), json.dumps(symbols), json.dumps(file_hashes),
              git_revision, _now(), _now()))
        if cur.rowcount == 0:
            row = self.conn.execute(
                "SELECT id, status FROM project_memory WHERE scope=? AND memory_type=? AND content_hash=?",
                (scope, memory_type, chash)).fetchone()
            self.conn.execute(
                "UPDATE project_memory SET updated_at=? WHERE id=?", (_now(), row[0]))
            self.conn.commit()
            return {"id": row[0], "status": "existing", "memory_status": row[1]}
        new_id = cur.lastrowid
        self.conn.commit()

        result = {"id": new_id, "status": "added"}
        if supersedes:
            link = self.supersede(new_id, int(supersedes))
            result["superseded"] = link
        return result

    def supersede(self, new_id, old_id):
        """Yeni kayıt eski kaydın yerini alır; eski kayıt korunur ama pasifleşir."""
        old = self._get_row(old_id)
        new = self._get_row(new_id)
        if not old or not new:
            return {"error": "memory_not_found"}
        if old["scope"] != new["scope"]:
            return {"error": "scope_mismatch"}
        self.conn.execute(
            "UPDATE project_memory SET status='SUPERSEDED', superseded_by=?, updated_at=? WHERE id=?",
            (new_id, _now(), old_id))
        self.conn.execute(
            "UPDATE project_memory SET supersedes=?, updated_at=? WHERE id=?",
            (old_id, _now(), new_id))
        self.conn.commit()
        return {"ok": True, "old_id": old_id, "new_id": new_id}

    def set_status(self, memory_id, status):
        status = str(status or "").upper()
        if status not in STATUSES:
            return {"error": f"unknown_status: {status}"}
        row = self._get_row(memory_id)
        if not row:
            return {"error": "memory_not_found"}
        self.conn.execute(
            "UPDATE project_memory SET status=?, updated_at=? WHERE id=?",
            (status, _now(), memory_id))
        self.conn.commit()
        return {"ok": True, "id": memory_id, "status": status}

    # ── okuma ────────────────────────────────────────────────────────

    def _get_row(self, memory_id):
        cur = self.conn.execute(
            "SELECT * FROM project_memory WHERE id=?", (int(memory_id),))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def get(self, memory_id):
        return self._get_row(memory_id) or {"error": "memory_not_found"}

    def list(self, scope, memory_type=None, statuses=("ACTIVE",)):
        query = "SELECT * FROM project_memory WHERE scope=?"
        params = [scope]
        if memory_type:
            query += " AND memory_type=?"
            params.append(str(memory_type).upper())
        if statuses:
            marks = ",".join("?" for _ in statuses)
            query += f" AND status IN ({marks})"
            params.extend(str(s).upper() for s in statuses)
        query += " ORDER BY updated_at DESC, id DESC"
        cur = self.conn.execute(query, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ── doğrulama (staleness) ───────────────────────────────────────

    def verify(self, scope, root):
        """ACTIVE bellekleri mevcut koda karşı doğrula (deterministik).

        related_files'tan biri yoksa veya kayıtlı hash'ten sapmışsa
        bellek STALE işaretlenir; tarihçe silinmez.
        """
        root = Path(root)
        report = {"checked": 0, "still_valid": 0, "stale": [], "no_files": 0}
        for row in self.list(scope, statuses=("ACTIVE",)):
            files = _loads(row.get("related_files"))
            if not files:
                report["no_files"] += 1
                continue
            report["checked"] += 1
            saved = {}
            try:
                saved = json.loads(row.get("file_hashes") or "{}")
            except ValueError:
                saved = {}

            reasons = []
            new_hashes = {}
            for rel in files:
                full = root / rel
                h = _file_hash(full)
                if not h:
                    reasons.append(f"file_missing:{rel}")
                    continue
                new_hashes[rel] = h
                prev = saved.get(rel)
                if prev and prev != h:
                    reasons.append(f"file_changed:{rel}")

            if reasons:
                self.conn.execute(
                    "UPDATE project_memory SET status='STALE', updated_at=? WHERE id=?",
                    (_now(), row["id"]))
                report["stale"].append({"id": row["id"], "reasons": reasons})
            else:
                self.conn.execute(
                    "UPDATE project_memory SET last_verified_at=?, file_hashes=?, updated_at=? WHERE id=?",
                    (_now(), json.dumps(new_hashes), _now(), row["id"]))
                report["still_valid"] += 1
        self.conn.commit()
        return report

    # ── compaction ──────────────────────────────────────────────────

    def compact(self, scope, max_per_type=DEFAULT_MAX_PER_TYPE,
                types=COMPACTABLE_TYPES):
        """Kısa ömürlü sınıflarda en yeni N ACTIVE kalır; eskiler STALE olur."""
        compacted = []
        for mtype in types:
            rows = self.list(scope, memory_type=mtype, statuses=("ACTIVE",))
            overflow = rows[max_per_type:]
            for row in overflow:
                self.conn.execute(
                    "UPDATE project_memory SET status='STALE', updated_at=? WHERE id=?",
                    (_now(), row["id"]))
                compacted.append(row["id"])
        self.conn.commit()
        return {"compacted": compacted, "count": len(compacted)}

    # ── state sheet ─────────────────────────────────────────────────

    def state_sheet(self, scope, max_chars=DEFAULT_SHEET_MAX_CHARS,
                    per_type=MAX_ITEMS_PER_TYPE_IN_SHEET):
        """Bağlama giren kompakt proje durum sayfası (bütçeli)."""
        lines = []
        used = 0
        for mtype in SHEET_ORDER:
            rows = self.list(scope, memory_type=mtype, statuses=("ACTIVE",))[:per_type]
            if not rows:
                continue
            header = f"[{mtype}]"
            if used + len(header) + 1 > max_chars:
                break
            lines.append(header)
            used += len(header) + 1
            for row in rows:
                conf = float(row.get("confidence") or 0)
                line = f"- {row['content']} (conf {conf:.2f}"
                if row.get("source"):
                    line += f", src {row['source']}"
                line += ")"
                if used + len(line) + 1 > max_chars:
                    break
                lines.append(line)
                used += len(line) + 1
        return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────

def _default_db():
    try:
        try:
            from paths import find_context_dir, find_project_root
        except ImportError:
            from scripts.paths import find_context_dir, find_project_root
        ctx, root = find_context_dir()
        if ctx:
            return Path(ctx) / "symbols.db"
        return find_project_root() / ".context" / "symbols.db"
    except Exception:
        return Path(".context") / "symbols.db"


def _default_scope():
    try:
        try:
            from paths import resolve_scope
        except ImportError:
            from scripts.paths import resolve_scope
        return resolve_scope()
    except Exception:
        return "default"


def main():
    parser = argparse.ArgumentParser(description="Project Memory 2.0")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", action="store_true")
    group.add_argument("--list", action="store_true")
    group.add_argument("--get", type=int, metavar="ID")
    group.add_argument("--status", nargs=2, metavar=("ID", "STATUS"))
    group.add_argument("--supersede", nargs=2, type=int, metavar=("OLD_ID", "NEW_ID"))
    group.add_argument("--verify", action="store_true")
    group.add_argument("--compact", action="store_true")
    group.add_argument("--sheet", action="store_true")

    parser.add_argument("--type", default="", choices=["", *MEMORY_TYPES])
    parser.add_argument("--content", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--confidence", type=float, default=0.5)
    parser.add_argument("--file", action="append", default=[])
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--revision", default="")
    parser.add_argument("--supersedes", type=int, default=None)
    parser.add_argument("--all-statuses", action="store_true")
    parser.add_argument("--max-per-type", type=int, default=DEFAULT_MAX_PER_TYPE)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_SHEET_MAX_CHARS)
    parser.add_argument("--scope", default="")
    parser.add_argument("--branch", action="store_true",
                        help="Use branch-specific scope (auto-derived from git)")
    parser.add_argument("--include-global", action="store_true",
                        help="When used with --branch --list, also include project-global items")
    args = parser.parse_args()

    if args.branch:
        try:
            try:
                from identity import scope_key_for
                from paths import find_project_root
            except ImportError:
                from scripts.identity import scope_key_for
                from scripts.paths import find_project_root
            scope = scope_key_for("BRANCH", find_project_root())
        except Exception:
            scope = args.scope or _default_scope()
    else:
        scope = args.scope or _default_scope()
    mem = ProjectMemory(_default_db())
    try:
        if args.add:
            out = mem.add(scope, args.type, args.content, source=args.source,
                          confidence=args.confidence, related_files=args.file,
                          related_symbols=args.symbol, git_revision=args.revision,
                          supersedes=args.supersedes)
        elif args.list:
            statuses = () if args.all_statuses else ("ACTIVE",)
            rows = mem.list(scope, memory_type=args.type or None, statuses=statuses)
            if args.include_global and args.branch:
                try:
                    try:
                        from identity import scope_key_for as _skf
                        from paths import find_project_root as _fpr
                    except ImportError:
                        from scripts.identity import scope_key_for as _skf
                        from scripts.paths import find_project_root as _fpr
                    pg_scope = _skf("PROJECT_GLOBAL", _fpr())
                    if pg_scope != scope:
                        seen = {r.get("id") for r in rows}
                        for r in mem.list(pg_scope, memory_type=args.type or None, statuses=statuses):
                            if r.get("id") not in seen:
                                rows.append(r)
                except Exception:
                    pass
            out = {"count": len(rows), "memories": rows}
        elif args.get is not None:
            out = mem.get(args.get)
        elif args.status:
            out = mem.set_status(int(args.status[0]), args.status[1])
        elif args.supersede:
            out = mem.supersede(int(args.supersede[1]), int(args.supersede[0]))
        elif args.verify:
            try:
                try:
                    from paths import find_project_root
                except ImportError:
                    from scripts.paths import find_project_root
                root = find_project_root()
            except Exception:
                root = Path.cwd()
            out = mem.verify(scope, root)
        elif args.compact:
            out = mem.compact(scope, max_per_type=args.max_per_type)
        else:  # --sheet
            out = {"sheet": mem.state_sheet(scope, max_chars=args.max_chars)}
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    finally:
        mem.close()


if __name__ == "__main__":
    main()
