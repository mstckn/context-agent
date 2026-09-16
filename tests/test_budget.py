#!/usr/bin/env python3
"""test_budget.py — Token bütçe defteri davranış testleri.

Kabul kriterleri:
  * init → durum sıfırlanır, max/used doğru raporlanır.
  * Harcama kalanı azaltır; bütçe aşımı REDDEDİLİR ve durumu bozmaz.
  * %50/%75/%90 eşiklerinde uyarı seviyeleri yükselir.
  * reset kullanımı sıfırlar ama max'ı korur.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import budget  # noqa: E402


def call(fn, *args):
    """Bütçe fonksiyonlarını çalıştır, JSON çıktısını yakala."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return json.loads(buf.getvalue())


class BudgetTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        # LIFO: önce cwd eski yerine döner, SONRA dizin silinir
        # (Windows cwd içindeyken rmtree'ya izin vermez).
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._old_cwd)

    def test_init_and_status(self):
        out = call(budget.init_budget, 10_000, "claude-haiku")
        self.assertEqual(out["status"], "initialized")
        self.assertEqual(out["max_tokens"], 10_000)

        st = call(budget.status)
        self.assertEqual(st["used"], 0)
        self.assertEqual(st["max"], 10_000)
        self.assertEqual(st["remaining"], 10_000)
        self.assertEqual(st["status"], "ok")

    def test_spend_reduces_remaining(self):
        call(budget.init_budget, 10_000)
        out = call(budget.spend, 3_000, "test-load")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["used"], 3_000)
        self.assertEqual(out["remaining"], 7_000)

        st = call(budget.status)
        self.assertEqual(st["used"], 3_000)
        self.assertEqual(len(st["last_entries"]), 1)

    def test_overspend_is_rejected_without_state_change(self):
        call(budget.init_budget, 1_000)
        out = call(budget.spend, 1_500, "too-big")
        self.assertEqual(out["status"], "budget_exceeded")
        self.assertEqual(out["remaining"], 1_000)

        st = call(budget.status)
        self.assertEqual(st["used"], 0)

    def test_warning_thresholds(self):
        call(budget.init_budget, 1_000)

        out = call(budget.spend, 510, "half")
        self.assertIn("yarılandı", out.get("warning", ""))

        out = call(budget.spend, 250, "three-quarters")
        self.assertIn("%75", out.get("warning", ""))

        out = call(budget.spend, 150, "critical")
        self.assertIn("%90", out.get("warning", ""))
        st = call(budget.status)
        self.assertEqual(st["status"], "critical")

    def test_reset_keeps_max_and_clears_usage(self):
        call(budget.init_budget, 5_000)
        call(budget.spend, 4_000, "load")
        out = call(budget.reset)
        self.assertEqual(out["status"], "reset")
        self.assertEqual(out["max_tokens"], 5_000)

        st = call(budget.status)
        self.assertEqual(st["used"], 0)
        self.assertEqual(st["remaining"], 5_000)

    def test_cost_estimate_uses_model_rate(self):
        call(budget.init_budget, 10_000, "claude-sonnet")
        call(budget.spend, 2_000, "load")
        st = call(budget.status)
        self.assertAlmostEqual(st["estimated_cost_usd"], 2_000 / 1000 * 0.003)
        self.assertEqual(st["cost_per_1k_tokens"], 0.003)


if __name__ == "__main__":
    unittest.main()
