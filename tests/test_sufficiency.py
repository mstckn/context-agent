#!/usr/bin/env python3
"""test_sufficiency.py - Sufficiency/coverage engine 2.0 testleri (Phase 1e).

Kabul kriterleri:
  * required_evidence kategorileri satisfied/available/waived olarak ayrılır.
  * coverage = satisfied / (required - waived).
  * available kategoriler SOMUT expansion adayı üretir.
  * Otomatik genişleme durma koşulları machine-readable çıktıda görünür.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import sufficiency  # noqa: E402
import planner  # noqa: E402

BUG_FIX_EVIDENCE = list(planner.TYPE_PROFILES["BUG_FIX"]["evidence"])


def _item(path, content="def x():\n    return 1\n", action="load_file"):
    return {"file": path, "action": action, "content": content, "symbols": []}


class SufficiencyTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "symbols.db"
        self.conn = sqlite3.connect(str(self.db))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, hash TEXT, "
            "lang TEXT, line_count INTEGER, summary TEXT, indexed_at TEXT, "
            "token_estimate INTEGER)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS edges (id INTEGER PRIMARY KEY, "
            "src_file TEXT, src_symbol TEXT DEFAULT '', kind TEXT, "
            "dst_file TEXT, dst_module TEXT DEFAULT '', dst_symbol TEXT DEFAULT '')")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.conn.close)

    def seed_files(self, *paths):
        for p in paths:
            self.conn.execute(
                "INSERT INTO files (path, hash, lang, line_count, summary, "
                "indexed_at, token_estimate) VALUES (?,?,?,?,?,?,?)",
                (p, "h", "python", 10, "", "", 100))
        self.conn.commit()

    def plan(self, evidence=None):
        return {"task_type": "BUG_FIX",
                "required_evidence": list(evidence or BUG_FIX_EVIDENCE)}

    def detect(self, plan, loaded, known=(), flags=None, task="fix the bug"):
        return sufficiency.detect_evidence(
            self.conn, plan["required_evidence"], loaded, list(known),
            task_text=task, flags=flags or {})

    def test_full_coverage_sufficient(self):
        self.seed_files("tests/test_service.py")
        self.conn.execute(
            "INSERT INTO edges (src_file, kind, dst_file) VALUES "
            "('api/h.py', 'calls', 'service.py')")
        self.conn.commit()
        plan = self.plan()
        ev = self.detect(plan, [_item("service.py")], known=["tests/test_service.py"])
        cov = sufficiency.build_coverage(plan, ev, quality={"score": 70})
        self.assertEqual(cov["missing_evidence"], [])
        self.assertTrue(cov["sufficient"])
        self.assertEqual(cov["coverage"], 1.0)

    def test_missing_tests_produces_expansion_candidate(self):
        self.seed_files("tests/test_service.py")
        plan = self.plan()
        ev = self.detect(plan, [_item("service.py")])
        cov = sufficiency.build_coverage(plan, ev, quality={"score": 70})
        self.assertIn("tests", cov["missing_evidence"])
        self.assertFalse(cov["sufficient"])
        files = [c["file"] for c in cov["recommended_expansion"] if "file" in c]
        self.assertIn("tests/test_service.py", files)

    def test_recent_modifications_waived_without_git(self):
        plan = self.plan(evidence=["symbol_body", "recent_modifications"])
        ev = self.detect(plan, [_item("service.py")], flags={"git_available": False})
        self.assertEqual(ev["recent_modifications"]["status"], "waived")
        cov = sufficiency.build_coverage(plan, ev, quality={"score": 70})
        # waived paydadan düşer → coverage 1.0 kalır.
        self.assertEqual(cov["coverage"], 1.0)
        self.assertIn("recent_modifications", cov["waived_evidence"])

    def test_recent_modifications_available_with_git(self):
        plan = self.plan(evidence=["symbol_body", "recent_modifications"])
        ev = self.detect(plan, [_item("service.py")], flags={"git_available": True})
        entry = ev["recent_modifications"]
        self.assertEqual(entry["status"], "available")
        self.assertTrue(any("git log" in c.get("cmd", "")
                            for c in entry["candidates"]))

    def test_graph_relations_satisfied_by_edges(self):
        self.conn.execute(
            "INSERT INTO edges (src_file, kind, dst_file) VALUES "
            "('api/h.py', 'calls', 'service.py')")
        self.conn.commit()
        plan = self.plan(evidence=["callers_callees"])
        ev = self.detect(plan, [_item("service.py")])
        self.assertEqual(ev["callers_callees"]["status"], "satisfied")

    def test_graph_relations_empty_recommends_get_related(self):
        plan = self.plan(evidence=["callers_callees"])
        ev = self.detect(plan, [_item("service.py")])
        # Graf tablosu var ama bu dosya için kenar yok → genişleme adayı üret.
        self.assertEqual(ev["callers_callees"]["status"], "available")
        cov = sufficiency.build_coverage(plan, ev, quality={"score": 70})
        cmds = [c.get("cmd", "") for c in cov["recommended_expansion"]]
        self.assertTrue(any("get_related.py service.py" in c for c in cmds))

    def test_runtime_evidence_from_task_text(self):
        plan = self.plan(evidence=["error_output"])
        ev_none = self.detect(plan, [_item("a.py")], task="fix the bug")
        self.assertEqual(ev_none["error_output"]["status"], "waived")
        ev_trace = self.detect(
            plan, [_item("a.py")],
            task="fix: Traceback (most recent call last): ...")
        self.assertEqual(ev_trace["error_output"]["status"], "satisfied")

    def test_known_representation_awareness(self):
        """Model gövde (full/range) gördüyse satisfied; sadece iskelet
        gördüyse genişleme önerilir."""
        plan = self.plan(evidence=["symbol_body"])
        ev_full = self.detect(
            plan, [], known=[{"file": "service.py", "representation": "full"}])
        self.assertEqual(ev_full["symbol_body"]["status"], "satisfied")
        ev_outline = self.detect(
            plan, [], known=[{"file": "service.py", "representation": "outline"}])
        self.assertEqual(ev_outline["symbol_body"]["status"], "available")

    def test_nearby_test_does_not_select_unrelated_repository_test(self):
        self.seed_files("tests/test_auth.py", "frontend/components/Button.tsx")
        plan = self.plan(evidence=["primary_symbol", "nearby_test"])
        ev = self.detect(plan, [_item("frontend/components/Button.tsx")])
        self.assertEqual(ev["nearby_test"]["status"], "waived")
        self.assertEqual(ev["nearby_test"]["candidates"], [])

    def test_local_leaf_dependency_is_waived_not_repo_expansion(self):
        plan = self.plan(evidence=["primary_symbol", "local_dependencies"])
        ev = self.detect(plan, [_item("frontend/components/Button.tsx")])
        self.assertEqual(ev["local_dependencies"]["status"], "waived")

    def test_non_api_feature_does_not_expand_to_unrelated_contract(self):
        self.seed_files("api/handlers.py", "jobs/queue.py")
        plan = self.plan(evidence=["implementation", "contracts"])
        ev = self.detect(plan, [_item("jobs/queue.py")],
                         task="prevent jobs from being executed twice")
        self.assertEqual(ev["contracts"]["status"], "waived")
        self.assertEqual(ev["contracts"]["candidates"], [])


if __name__ == "__main__":
    unittest.main()
