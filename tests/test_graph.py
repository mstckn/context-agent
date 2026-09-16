#!/usr/bin/env python3
"""test_graph.py — Yapısal bağımlılık grafı testleri (Phase 1c).

Kabul kriterleri:
  * Python import'ları GERÇEK dosyalara çözülür (LIKE yok).
  * Kalıtım (inherits) ve çağrı (calls) kenarları üretilir.
  * impact_set transitif etki kümesini BFS ile bulur.
  * Harici modüller (os, third-party) proje dosyasına bağlanmaz.
  * JS/TS relative import'lar çözülür.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import graph  # noqa: E402

BASE_PY = "class Animal:\n    pass\n\n\ndef feed():\n    return 1\n"
SERVICE_PY = (
    "from base import Animal, feed\n\n\n"
    "class Dog(Animal):\n    pass\n\n\n"
    "def walk():\n    return feed()\n"
)
ENTRY_PY = "from service import Dog\n\n\ndef main():\n    return Dog()\n"
UTIL_TS = "export const helper = 1;\n"
APP_TS = "import { helper } from './util';\nexport const v = helper;\n"


class GraphTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "symbols.db"
        self.conn = sqlite3.connect(str(self.db))
        graph.ensure_schema(self.conn)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, hash TEXT, "
            "lang TEXT, line_count INTEGER, summary TEXT, indexed_at TEXT, "
            "token_estimate INTEGER)")
        # LIFO: önce conn kapanır, sonra geçici dizin silinir.
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.conn.close)

    def seed(self, files):
        """files: {rel_path: (content, lang)} — dosyaları yaz + files tablosuna ekle."""
        for rel, (content, lang) in files.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(content, encoding="utf-8")
            self.conn.execute(
                "INSERT INTO files (path, hash, lang, line_count, summary, "
                "indexed_at, token_estimate) VALUES (?,?,?,?,?,?,?)",
                (rel, f"hash-{rel}", lang, content.count("\n") + 1, "", "", 10))
        self.conn.commit()
        return graph.build_all(self.root, self.conn)

    def test_python_import_resolution(self):
        self.seed({
            "base.py": (BASE_PY, "python"),
            "service.py": (SERVICE_PY, "python"),
            "entry.py": (ENTRY_PY, "python"),
        })
        deps = {d["file"] for d in graph.dependencies(self.conn, "service.py")}
        self.assertIn("base.py", deps)
        dependents = {d["file"] for d in graph.dependents(self.conn, "base.py")}
        self.assertIn("service.py", dependents)

    def test_inherits_and_calls_edges(self):
        self.seed({
            "base.py": (BASE_PY, "python"),
            "service.py": (SERVICE_PY, "python"),
            "entry.py": (ENTRY_PY, "python"),
        })
        edges = graph.outgoing_edges(self.conn, "service.py")
        inh = [e for e in edges if e["kind"] == "inherits"]
        self.assertTrue(any(e["dst_file"] == "base.py" and e["dst_symbol"] == "Animal"
                            for e in inh), f"inherits kenarı yok: {edges}")
        entry_edges = graph.outgoing_edges(self.conn, "entry.py")
        calls = [e for e in entry_edges if e["kind"] == "calls"]
        self.assertTrue(any(e["dst_file"] == "service.py" and e["dst_symbol"] == "Dog"
                            for e in calls), f"calls kenarı yok: {entry_edges}")

    def test_external_module_not_linked(self):
        self.seed({"svc.py": ("import os\nimport json\n\ndef f():\n    return os.getcwd()\n",
                              "python")})
        deps = graph.dependencies(self.conn, "svc.py")
        self.assertEqual(deps, [], f"harici modül dosyaya bağlandı: {deps}")

    def test_impact_set_transitive(self):
        self.seed({
            "base.py": (BASE_PY, "python"),
            "service.py": (SERVICE_PY, "python"),
            "entry.py": (ENTRY_PY, "python"),
        })
        impacted = graph.impact_set(self.conn, "base.py", depth=2)
        by_file = {i["file"]: i["distance"] for i in impacted}
        self.assertEqual(by_file.get("service.py"), 1)
        self.assertEqual(by_file.get("entry.py"), 2)
        # depth=1 girişimi entry.py'yi kapsamaz
        impacted1 = {i["file"] for i in graph.impact_set(self.conn, "base.py", depth=1)}
        self.assertNotIn("entry.py", impacted1)

    def test_js_relative_import_resolution(self):
        self.seed({
            "frontend/util.ts": (UTIL_TS, "typescript"),
            "frontend/app.ts": (APP_TS, "typescript"),
        })
        deps = {d["file"] for d in graph.dependencies(self.conn, "frontend/app.ts")}
        self.assertIn("frontend/util.ts", deps)
        dependents = {d["file"] for d in graph.dependents(self.conn, "frontend/util.ts")}
        self.assertIn("frontend/app.ts", dependents)

    def test_relations_dedup_and_no_self(self):
        """Aynı dosyaya imports+inherits+calls kenarları → tek ve en güçlü ilişki;
        dosyanın kendisi ilişkilerde görünmez."""
        self.seed({
            "base.py": (BASE_PY, "python"),
            "service.py": (SERVICE_PY, "python"),
        })
        deps = graph.dependencies(self.conn, "service.py")
        by_file = {d["file"]: d["relation"] for d in deps}
        self.assertEqual(list(by_file), ["base.py"], f"duplike ilişki: {deps}")
        self.assertEqual(by_file["base.py"], "imports")
        # Dosya-içi çağrılar (self-kenar) ilişkilerde görünmemeli
        self.seed({"selfcall.py": ("def a():\n    return b()\n\ndef b():\n    return 2\n",
                                   "python")})
        deps_self = graph.dependencies(self.conn, "selfcall.py")
        self.assertNotIn("selfcall.py", {d["file"] for d in deps_self})
        dependents_self = graph.dependents(self.conn, "selfcall.py")
        self.assertNotIn("selfcall.py", {d["file"] for d in dependents_self})
        # Ama self-kenar DB'de mevcut (sembol düzeyi kullanım için)
        edges = graph.outgoing_edges(self.conn, "selfcall.py")
        self.assertTrue(any(e["kind"] == "calls" and e["dst_symbol"] == "b"
                            for e in edges))

    def test_rebuild_skips_unchanged(self):
        self.seed({"base.py": (BASE_PY, "python")})
        r2 = graph.build_all(self.root, self.conn)
        self.assertEqual(r2["built"], 0)
        self.assertEqual(r2["skipped_unchanged"], 1)

    def test_deleted_file_edges_purged(self):
        self.seed({
            "base.py": (BASE_PY, "python"),
            "entry.py": (ENTRY_PY, "python"),
            "service.py": (SERVICE_PY, "python"),
        })
        # service.py silinmiş gibi files tablosundan düşür
        self.conn.execute("DELETE FROM files WHERE path='service.py'")
        self.conn.commit()
        graph.build_all(self.root, self.conn)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM edges WHERE src_file='service.py'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
