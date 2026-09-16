#!/usr/bin/env python3
"""test_memory.py — Project Memory 2.0 davranış testleri (promt.txt PHASE 8).

Kabul kriterleri:
  * Tipli bellek ekleme/listeleme; aynı içerik ikinci kez EKLENMEZ.
  * Supersession: yeni karar eskinin yerini alır; ikisi birden ACTIVE dönmez.
  * Durum geçişleri (ACTIVE/STALE/INVALIDATED) korunur, tarihçe silinmez.
  * Staleness doğrulaması: ilişkili dosya değiştiyse/yoksa bellek STALE.
  * Compaction kısa ömürlü sınıfları sınırlar.
  * State sheet bütçeye uyar ve CONSTRAINT gibi kritik sınıflarla başlar.
  * Scope'lar birbirinden izoledir.
"""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from memory import ProjectMemory  # noqa: E402

SCOPE = "test-scope"


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.root = Path(self._tmp.name)
        self.mem = ProjectMemory(self.root / "memory.db")
        # LIFO: önce bağlantı kapanır, sonra cwd döner, en son dizin silinir.
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._old_cwd)
        self.addCleanup(self.mem.close)

    # ── temel yaşam döngüsü ─────────────────────────────────────────

    def test_add_and_list_active(self):
        out = self.mem.add(SCOPE, "DECISION", "Use JWT for auth tokens",
                           source="user", confidence=0.9)
        self.assertEqual(out["status"], "added")

        rows = self.mem.list(SCOPE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["memory_type"], "DECISION")
        self.assertEqual(rows[0]["status"], "ACTIVE")
        self.assertAlmostEqual(rows[0]["confidence"], 0.9)

    def test_unknown_type_is_rejected(self):
        out = self.mem.add(SCOPE, "FEELINGS", "something")
        self.assertIn("error", out)

    def test_duplicate_content_is_not_readded(self):
        first = self.mem.add(SCOPE, "CONSTRAINT", "Must stay on Python 3.11")
        again = self.mem.add(SCOPE, "CONSTRAINT",
                             "Must stay on   Python 3.11")  # boşluk farkı
        self.assertEqual(first["status"], "added")
        self.assertEqual(again["status"], "existing")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.mem.list(SCOPE)), 1)

    def test_scope_isolation(self):
        self.mem.add(SCOPE, "DECISION", "A")
        self.mem.add("other-scope", "DECISION", "B")
        self.assertEqual(len(self.mem.list(SCOPE)), 1)
        self.assertEqual(len(self.mem.list("other-scope")), 1)

    # ── supersession ────────────────────────────────────────────────

    def test_supersession_hides_old_decision(self):
        a = self.mem.add(SCOPE, "DECISION", "Use JWT.")["id"]
        b = self.mem.add(SCOPE, "DECISION", "Replace JWT with sessions.",
                         supersedes=a)["id"]

        active = self.mem.list(SCOPE, memory_type="DECISION")
        self.assertEqual([r["id"] for r in active], [b])

        old = self.mem.get(a)
        self.assertEqual(old["status"], "SUPERSEDED")
        self.assertEqual(old["superseded_by"], b)
        # Tarihçe korunur: eski kayıt hâlâ okunabilir.
        all_rows = self.mem.list(SCOPE, memory_type="DECISION",
                                 statuses=("ACTIVE", "SUPERSEDED"))
        self.assertEqual(len(all_rows), 2)

    def test_explicit_supersede_links(self):
        a = self.mem.add(SCOPE, "ARCHITECTURAL", "Queue uses Redis.")["id"]
        b = self.mem.add(SCOPE, "ARCHITECTURAL", "Queue uses in-process worker.")["id"]
        out = self.mem.supersede(b, a)
        self.assertTrue(out["ok"])
        self.assertEqual(self.mem.get(a)["status"], "SUPERSEDED")
        self.assertEqual(self.mem.get(b)["supersedes"], a)

    def test_set_status_invalidated(self):
        mid = self.mem.add(SCOPE, "KNOWN_ISSUE", "Windows paths fail in X")["id"]
        out = self.mem.set_status(mid, "INVALIDATED")
        self.assertTrue(out["ok"])
        self.assertEqual(self.mem.get(mid)["status"], "INVALIDATED")
        self.assertEqual(self.mem.list(SCOPE), [])

    # ── doğrulama / staleness ───────────────────────────────────────

    def test_verify_marks_stale_when_file_changes(self):
        f = self.root / "auth.py"
        f.write_text("MODE = 'jwt'\n", encoding="utf-8")
        mid = self.mem.add(SCOPE, "ARCHITECTURAL", "Authentication uses JWT",
                           related_files=["auth.py"])["id"]

        report = self.mem.verify(SCOPE, self.root)
        self.assertEqual(report["still_valid"], 1)
        self.assertEqual(self.mem.get(mid)["status"], "ACTIVE")

        f.write_text("MODE = 'sessions'\n", encoding="utf-8")
        report = self.mem.verify(SCOPE, self.root)
        self.assertEqual(len(report["stale"]), 1)
        self.assertTrue(any("file_changed:auth.py" in r for r in report["stale"][0]["reasons"]))
        self.assertEqual(self.mem.get(mid)["status"], "STALE")
        self.assertEqual(self.mem.list(SCOPE), [])

    def test_verify_marks_stale_when_file_missing(self):
        f = self.root / "gone.py"
        f.write_text("X = 1\n", encoding="utf-8")
        mid = self.mem.add(SCOPE, "KNOWN_ISSUE", "Bug in gone.py",
                           related_files=["gone.py"])["id"]
        f.unlink()

        report = self.mem.verify(SCOPE, self.root)
        self.assertTrue(any("file_missing:gone.py" in r for r in report["stale"][0]["reasons"]))
        self.assertEqual(self.mem.get(mid)["status"], "STALE")

    # ── compaction + state sheet ────────────────────────────────────

    def test_compact_caps_ephemeral_memory(self):
        for i in range(12):
            self.mem.add(SCOPE, "EPHEMERAL", f"note {i}")
        out = self.mem.compact(SCOPE, max_per_type=8)
        self.assertEqual(out["count"], 4)
        active = self.mem.list(SCOPE, memory_type="EPHEMERAL")
        self.assertEqual(len(active), 8)
        # En yeni kayıtlar hayatta kalır.
        self.assertEqual(active[0]["content"], "note 11")

    def test_compact_does_not_touch_decisions(self):
        for i in range(12):
            self.mem.add(SCOPE, "DECISION", f"decision {i}")
        out = self.mem.compact(SCOPE, max_per_type=8)
        self.assertEqual(out["count"], 0)
        self.assertEqual(len(self.mem.list(SCOPE, memory_type="DECISION")), 12)

    def test_state_sheet_priority_and_budget(self):
        self.mem.add(SCOPE, "CONSTRAINT", "Must remain compatible with Python 3.11")
        self.mem.add(SCOPE, "EPHEMERAL", "temporary noise")
        sheet = self.mem.state_sheet(SCOPE)
        self.assertTrue(sheet.startswith("[CONSTRAINT]"))
        self.assertIn("Python 3.11", sheet)
        self.assertIn("[EPHEMERAL]", sheet)

        tiny = self.mem.state_sheet(SCOPE, max_chars=30)
        self.assertLessEqual(len(tiny), 30)

    def test_concurrent_duplicate_add_is_idempotent(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def writer():
            mem = ProjectMemory(self.root / "memory.db")
            try:
                barrier.wait()
                results.append(mem.add(SCOPE, "DECISION", "Use SQLite"))
            except Exception as exc:
                errors.append(exc)
            finally:
                mem.close()

        threads = [threading.Thread(target=writer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len({result["id"] for result in results}), 1)
        self.assertEqual(len(self.mem.list(SCOPE, memory_type="DECISION")), 1)


if __name__ == "__main__":
    unittest.main()
