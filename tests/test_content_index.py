#!/usr/bin/env python3
"""test_content_index.py — Chunk tabanlı içerik indeksi testleri (Phase 1b).

Kabul kriterleri:
  * Python dosyalar AST ile fonksiyon/sınıf/modül chunk'larına bölünür.
  * Sembol aramasının göremediği GÖVDE içeriği chunk aramasında bulunur.
  * Sonuçlar genişletme komutu (get_range start end) taşır.
  * Yeniden indeksleme idempotenttir (duplikasyon yok).
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import content_index  # noqa: E402
import index as index_mod  # noqa: E402

PY_SOURCE = '''"""Modül docstring."""

import os

CONSTANT = 42


def small_helper(x):
    """Helper docstring."""
    return x + CONSTANT


class Worker:
    """Worker docstring."""

    def run(self):
        # magic token only present in body
        marker = "zebra-lantern-42"
        return marker
'''


class ContentIndexTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "test.db"
        self.conn = sqlite3.connect(str(self.db))
        content_index.ensure_schema(self.conn)
        # build_all testinin ihtiyaç duyduğu minimal files tablosu
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, hash TEXT, "
            "lang TEXT, line_count INTEGER, summary TEXT, indexed_at TEXT, "
            "token_estimate INTEGER)")
        self.conn.commit()
        # LIFO: addCleanup sırası önemli — önce conn kapanmalı, sonra dizin silinmeli.
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.conn.close)

    def build(self, rel="pkg/worker.py", source=PY_SOURCE, lang="python"):
        return content_index.replace_file_chunks(
            self.conn, rel, source, lang, file_hash="h1")

    def test_python_chunks_kinds_and_ranges(self):
        self.build()
        rows = self.conn.execute(
            "SELECT kind, name, start_line, end_line FROM content_chunks "
            "WHERE file=? ORDER BY start_line", ("pkg/worker.py",)
        ).fetchall()
        kinds = {r[0] for r in rows}
        names = {r[1] for r in rows}
        self.assertIn("module", kinds)
        self.assertIn("function", kinds)
        self.assertIn("class", kinds)
        self.assertIn("small_helper", names)
        self.assertIn("Worker", names)
        # range tutarlılığı: start <= end, modül başı 1'den başlar
        for kind, name, start, end in rows:
            self.assertGreaterEqual(end, start)
        mod = [r for r in rows if r[0] == "module"][0]
        self.assertEqual(mod[2], 1)

    def test_body_only_phrase_is_findable(self):
        self.build()
        results = content_index.search_chunks(self.conn, "zebra lantern", limit=5)
        self.assertTrue(results, "gövde içeriği bulunamadı")
        hit = [r for r in results if r["name"] in ("run", "Worker")]
        self.assertTrue(hit)
        r = hit[0]
        self.assertEqual(r["file"], "pkg/worker.py")
        self.assertIn("get_range.py pkg/worker.py", r["get_cmd"])
        self.assertIn(str(r["start_line"]), r["get_cmd"])

    def test_rebuild_is_idempotent(self):
        self.build()
        first = self.conn.execute(
            "SELECT COUNT(*) FROM content_chunks").fetchone()[0]
        self.build()  # aynı içerikle tekrar
        second = self.conn.execute(
            "SELECT COUNT(*) FROM content_chunks").fetchone()[0]
        self.assertEqual(first, second)
        fts = self.conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        self.assertEqual(first, fts)

    def test_changed_content_replaces_old_chunks(self):
        self.build()
        new_source = PY_SOURCE.replace("zebra-lantern-42", "quartz-compass-7")
        content_index.replace_file_chunks(
            self.conn, "pkg/worker.py", new_source, "python", file_hash="h2")
        self.assertEqual(
            content_index.search_chunks(self.conn, "zebra lantern", limit=3), [])
        self.assertTrue(
            content_index.search_chunks(self.conn, "quartz compass", limit=3))

    def test_window_fallback_for_unknown_lang(self):
        long_text = "\n".join(f"line {i} with data" for i in range(1, 101))
        chunks = content_index.chunk_file(long_text, "notes.txt", "plaintext")
        self.assertTrue(all(c["kind"] == "window" for c in chunks))
        self.assertEqual(chunks[0]["start_line"], 1)
        # pencere adımları örtüşmeli ve dosya sonunu kapsamalı
        self.assertGreaterEqual(chunks[-1]["end_line"], 100)

    def test_build_all_skips_unchanged(self):
        root = Path(self._tmp.name)
        (root / "a.py").write_text(PY_SOURCE, encoding="utf-8")
        self.conn.execute(
            "INSERT INTO files (path, hash, lang, line_count, summary, indexed_at, "
            "token_estimate) VALUES (?,?,?,?,?,?,?)",
            ("a.py", "h1", "python", 10, "", "", 100))
        self.conn.commit()
        r1 = content_index.build_all(root, self.conn)
        self.assertEqual(r1["built"], 1)
        r2 = content_index.build_all(root, self.conn)
        self.assertEqual(r2["built"], 0)
        self.assertEqual(r2["skipped_unchanged"], 1)

    def test_docs_config_and_sql_migrations_are_indexed_and_searchable(self):
        root = Path(self._tmp.name) / "project"
        root.mkdir()
        files = {
            "README.md": "deployment uses a cobalt canary procedure",
            "pyproject.toml": "[tool.service]\nmode = 'strict'\n",
            "config.yaml": "feature_gate: enabled\n",
            "migrations/0002_accounts.sql":
                "ALTER TABLE users ADD COLUMN account_id TEXT;\n",
        }
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        db_dir = root / ".context"
        db_dir.mkdir()
        conn = index_mod.init_db(db_dir)
        try:
            for rel in files:
                result = index_mod.index_file(root / rel, root, conn, force=True)
                self.assertEqual(result["status"], "indexed")
            content_index.build_all(root, conn, force=True)
            indexed = {row[0] for row in conn.execute("SELECT path FROM files")}
            self.assertTrue(set(files).issubset(indexed))
            hits = content_index.search_chunks(conn, "cobalt canary", limit=5)
            self.assertEqual(hits[0]["file"], "README.md")
            migration_hits = content_index.search_chunks(conn, "account_id", limit=5)
            self.assertEqual(migration_hits[0]["file"],
                             "migrations/0002_accounts.sql")
        finally:
            conn.close()


    def test_full_scan_can_defer_auxiliary_indexes(self):
        root = Path(self._tmp.name) / "bulk"
        root.mkdir()
        source = root / "worker.py"
        source.write_text("def work():\n    return 1\n", encoding="utf-8")
        context_dir = root / ".context"
        context_dir.mkdir()
        conn = index_mod.init_db(context_dir)
        try:
            with mock.patch("content_index.replace_file_chunks") as chunks, \
                    mock.patch("graph.build_file_edges") as edges:
                result = index_mod.index_file(
                    source, root, conn, force=True, update_auxiliary=False
                )
            self.assertEqual(result["status"], "indexed")
            chunks.assert_not_called()
            edges.assert_not_called()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
