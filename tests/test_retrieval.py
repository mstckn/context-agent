#!/usr/bin/env python3
"""test_retrieval.py — Retrieval çekirdeği: dedup defteri + arama skoru.

Kabul kriterleri:
  * Aynı içerik ikinci kez BAĞLANMAZ (duplicate, token tasarrufu raporlanır).
  * Dosya hash'i değiştiyse kayıt yenilenir (updated) — eski içerik bilinmez.
  * check_context mevcut/kayıp ayrımını doğru yapar.
  * Arama skoru: sembol adı > dosya adı > gövde/docstring.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import dedup  # noqa: E402
from search import score_result  # noqa: E402


def call(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return json.loads(buf.getvalue())


class DedupTestCase(unittest.TestCase):
    def setUp(self):
        # dedup fonksiyonları DB bağlantısını AÇIK bırakır; Windows'ta
        # kilitli dosya TemporaryDirectory.cleanup'ı patlatır. Bu yüzden
        # elle ve hata toleranslı temizlik yapılır.
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        ctx = Path(self._tmp.name) / ".context"
        ctx.mkdir()
        # find_context_dir symbols.db varlığı ister; boş DB yeterli.
        sqlite3.connect(str(ctx / "symbols.db")).close()
        self.src = Path(self._tmp.name) / "service.py"
        self.src.write_text("VALUE = 1\n", encoding="utf-8")
        self.addCleanup(shutil.rmtree, self._tmp.name, True)
        self.addCleanup(os.chdir, self._old_cwd)

    def test_add_then_duplicate_saves_tokens(self):
        first = call(dedup.add_to_context, "k1", "service.py", "service.py", 420)
        self.assertEqual(first["status"], "added")

        second = call(dedup.add_to_context, "k1", "service.py", "service.py", 420)
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["tokens_saved"], 420)
        # INSERT hit_count kolonu yazmaz (varsayılan 0); ilk tekrar → 1.
        self.assertEqual(second["hit_count"], 1)

    def test_hash_change_forces_refresh(self):
        call(dedup.add_to_context, "k1", "service.py", "service.py", 420)
        self.src.write_text("VALUE = 2\n", encoding="utf-8")

        out = call(dedup.add_to_context, "k1", "service.py", "service.py", 430)
        self.assertEqual(out["status"], "updated")
        self.assertEqual(out["token_cost"], 430)

    def test_check_context_exists_and_missing(self):
        missing = call(dedup.check_context, "nope")
        self.assertFalse(missing["exists"])

        call(dedup.add_to_context, "k1", "service.py", "service.py", 420)
        found = call(dedup.check_context, "k1")
        self.assertTrue(found["exists"])
        self.assertEqual(found["label"], "service.py")

    def test_distinct_keys_are_independent(self):
        call(dedup.add_to_context, "k1", "service.py", "service.py", 100)
        out = call(dedup.add_to_context, "k2", "other", "service.py", 100)
        self.assertEqual(out["status"], "added")


class SearchScoringTestCase(unittest.TestCase):
    def row(self, name="x", doc="", sig="", file="src/x.py"):
        return (name, "function", file, 1, doc, sig)

    def test_name_hit_outranks_docstring_hit(self):
        words = ["token"]
        name_hit = score_result(self.row(name="validate_token"), words)
        doc_hit = score_result(self.row(name="validate", doc="checks token"), words)
        self.assertGreater(name_hit, doc_hit)

    def test_file_name_hit_adds_weight(self):
        words = ["auth"]
        in_file = score_result(self.row(name="check", file="src/auth.py"), words)
        not_in_file = score_result(self.row(name="check", file="src/util.py"), words)
        self.assertGreater(in_file, not_in_file)

    def test_no_match_scores_zero(self):
        self.assertEqual(score_result(self.row(name="check"), ["zebra"]), 0)


if __name__ == "__main__":
    unittest.main()
