#!/usr/bin/env python3
"""test_planner.py - Task-aware planner testleri (Phase 1d).

Kabul kriterleri:
  * Görev metni doğru tipe sınıflanır (deterministik).
  * Risk sinyalleri seviyeyi yükseltir ve evidence gereksinimini artırır.
  * Bütçe aralıkları tip profiline uygundur; LOCAL_EDIT küçük,
    ARCHITECTURE/SECURITY büyüktür.
  * Boş görev REPOSITORY_EXPLORATION olur.
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import planner  # noqa: E402


class PlannerTestCase(unittest.TestCase):
    def test_bug_fix_classification(self):
        plan = planner.plan_task("Fix the payment processing bug")
        self.assertEqual(plan["task_type"], "BUG_FIX")
        for ev in ("symbol_body", "callers_callees", "tests"):
            self.assertIn(ev, plan["required_evidence"])
        bf = plan["budget_fraction"]
        self.assertGreaterEqual(bf["min"], 0.10)
        self.assertLessEqual(bf["max"], 0.40)

    def test_security_wins_over_bugfix_and_raises_risk(self):
        plan = planner.plan_task("Fix authentication vulnerability in login")
        self.assertEqual(plan["task_type"], "SECURITY")
        self.assertIn(plan["risk"], ("HIGH", "CRITICAL"))
        # Kritik görevde agresif optimizasyon yok: taban yükselir.
        bf = plan["budget_fraction"]
        self.assertGreaterEqual(bf["min"], 0.40)
        # Risk ek evidence dayatır.
        self.assertIn("tests", plan["required_evidence"])
        self.assertIn("contracts", plan["required_evidence"])

    def test_local_edit_small_budget(self):
        plan = planner.plan_task("fix a typo in the comment")
        self.assertEqual(plan["task_type"], "LOCAL_EDIT")
        self.assertLessEqual(plan["budget_fraction"]["max"], 0.20)

    def test_architecture_large_budget(self):
        plan = planner.plan_task("Redesign the entire system architecture")
        self.assertEqual(plan["task_type"], "ARCHITECTURE")
        self.assertGreaterEqual(plan["budget_fraction"]["min"], 0.30)
        self.assertTrue(plan["strategy"]["use_dependency_graph"])

    def test_empty_task_is_exploration(self):
        plan = planner.plan_task("")
        self.assertEqual(plan["task_type"], "REPOSITORY_EXPLORATION")

    def test_database_change_turkish(self):
        plan = planner.plan_task("veritabanına kolon ekle ve migration yaz")
        self.assertEqual(plan["task_type"], "DATABASE_CHANGE")
        self.assertIn("migrations", plan["required_evidence"])
        # migration risk sinyalidir → risk en az MEDIUM.
        self.assertIn(plan["risk"], ("MEDIUM", "HIGH", "CRITICAL"))

    def test_api_change_evidence(self):
        plan = planner.plan_task(
            "Change the response format of the /login endpoint")
        self.assertEqual(plan["task_type"], "API_CHANGE")
        for ev in ("input_schema", "output_schema", "consumers"):
            self.assertIn(ev, plan["required_evidence"])

    def test_plan_is_machine_readable_and_stable(self):
        p1 = planner.plan_task("Add rate limiting to the login endpoint")
        p2 = planner.plan_task("Add rate limiting to the login endpoint")
        self.assertEqual(p1, p2)  # deterministik
        for key in ("task_type", "risk", "strategy", "budget_fraction",
                    "required_evidence"):
            self.assertIn(key, p1)

    def test_route_analysis_low_complexity_shrinks_feature_budget(self):
        plan = planner.plan_task("add something", route_analysis={
            "domains": [], "complexity": "low"})
        self.assertEqual(plan["budget_fraction"]["max"], 0.20)

    def test_css_value_change_is_local_edit(self):
        plan = planner.plan_task("Change button border radius from 8px to 12px")
        self.assertEqual(plan["task_type"], "LOCAL_EDIT")
        self.assertLessEqual(plan["budget_fraction"]["max"], 0.15)

    def test_profile_word_is_not_performance_stem(self):
        plan = planner.plan_task("add an account profile API")
        self.assertEqual(plan["task_type"], "FEATURE")

    def test_authorization_change_uses_security_evidence(self):
        plan = planner.plan_task("change one authorization check for admins")
        self.assertEqual(plan["task_type"], "SECURITY")
        self.assertIn("auth_boundaries", plan["required_evidence"])


if __name__ == "__main__":
    unittest.main()
