#!/usr/bin/env python3
"""test_model_profile.py - Model-aware adaptive budget testleri (Phase 2a).

Kabul kriterleri:
  * Bilinen model aileleri yerleşik profilden çözülür (prefix eşleşmesi).
  * Bilinmeyen model FALLBACK'e düşer (asla hata yok).
  * retrieval_budget = window - rezervler, safe_input_budget ile sınırlı,
    taban/tavan kelepçeli.
  * Config (.context/config/model_profiles.json) override eder; çekirdeğe
    model sabitlemek gerekmez.
"""

import json
import os
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import model_profile  # noqa: E402


class ModelProfileTestCase(unittest.TestCase):
    def test_known_family_builtin(self):
        p = model_profile.profile_for("claude-haiku")
        self.assertEqual(p["source"], "builtin")
        self.assertEqual(p["context_window"], 200_000)

    def test_prefix_family_match(self):
        p = model_profile.profile_for("claude-haiku-4-5-20251001")
        self.assertEqual(p["source"], "builtin")
        self.assertEqual(p["context_window"], 200_000)

    def test_unknown_model_fallback(self):
        p = model_profile.profile_for("totally-new-model-9000")
        self.assertEqual(p["source"], "fallback")
        self.assertGreater(p["context_window"], 0)

    def test_empty_model_fallback(self):
        p = model_profile.profile_for("")
        self.assertEqual(p["source"], "fallback")

    def test_retrieval_budget_math(self):
        p = {
            "context_window": 100_000, "safe_input_budget": 30_000,
            "reserved_output": 10_000, "system_overhead": 2_000,
            "tool_overhead": 3_000, "reasoning_reserve": 5_000,
        }
        # 100k - 10k - 2k - 3k - 5k = 80k → safe_input 30k ile sınırlanır.
        self.assertEqual(model_profile.retrieval_budget(p), 30_000)

    def test_retrieval_budget_task_tokens_and_floor(self):
        p = {
            "context_window": 8_000, "safe_input_budget": 8_000,
            "reserved_output": 4_000, "system_overhead": 2_000,
            "tool_overhead": 2_000, "reasoning_reserve": 2_000,
        }
        # Negatife düşse bile taban korunur.
        self.assertEqual(model_profile.retrieval_budget(p),
                         model_profile.MIN_RETRIEVAL_BUDGET)

    def test_fraction_scaling(self):
        p = model_profile.profile_for("claude-haiku")
        full = model_profile.retrieval_budget(p)
        small = model_profile.retrieval_budget(p, task_fraction=0.10)
        self.assertLess(small, full)
        self.assertGreaterEqual(small, model_profile.MIN_RETRIEVAL_BUDGET)

    def test_ceiling(self):
        p = model_profile.profile_for("gemini-flash")
        self.assertLessEqual(model_profile.retrieval_budget(p),
                             model_profile.MAX_RETRIEVAL_BUDGET)

    def test_config_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_dir = root / ".context" / "config"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "model_profiles.json").write_text(json.dumps({
                "claude-haiku": {"safe_input_budget": 1234},
                "custom-model": {"context_window": 50_000,
                                 "safe_input_budget": 9_000},
            }), encoding="utf-8")
            old_cwd = os.getcwd()
            os.chdir(root)
            try:
                p = model_profile.profile_for("claude-haiku")
                self.assertEqual(p["source"], "config")
                self.assertEqual(p["safe_input_budget"], 1234)
                c = model_profile.profile_for("custom-model")
                self.assertEqual(c["source"], "config")
                self.assertEqual(c["context_window"], 50_000)
            finally:
                os.chdir(old_cwd)

    def test_public_agent_cli_accepts_unknown_model(self):
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "agent.py"), "--help"],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("{claude-sonnet,claude-haiku", result.stdout)

    def test_mcp_schema_does_not_reject_unknown_model(self):
        import mcp_server
        tool = next(t for t in mcp_server.tool_schema()
                    if t["name"] == "context_agent_build_context")
        model_schema = tool["inputSchema"]["properties"]["model"]
        self.assertNotIn("enum", model_schema)


if __name__ == "__main__":
    unittest.main()
