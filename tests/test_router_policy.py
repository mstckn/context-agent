"""Tests for scripts/router.py + secret_store.py (PHASE D, G).

Acceptance coverage:
  * deterministic selection, risk floor, quality guardrail (never pretend
    a weaker model is enough), cost guardrails, escalation chain
  * zero-provider core-only operation (IDE MODEL default, no keys required)
  * secret boundary: masks only in output, replace/delete, no raw leakage
  * hybrid delegation honesty (executed_by=ide_model)
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import router  # noqa: E402
import secret_store  # noqa: E402


def make_project(base: Path, name: str = "proj") -> Path:
    root = base / name
    (root / ".context").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return root


class RouterIsolationHarness(unittest.TestCase):
    """Base: redirects every router/secret persistence path into a temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.agent_dir = self.home / ".context-agent"
        self.root = make_project(base)

        self._patches = [
            mock.patch.object(Path, "home", return_value=self.home),
            mock.patch.object(router, "AGENT_DIR", self.agent_dir),
            mock.patch.object(router, "PROVIDERS_PATH", self.agent_dir / "providers.json"),
            mock.patch.object(router, "MODELS_PATH", self.agent_dir / "models.json"),
            mock.patch.object(router, "HEALTH_PATH", self.agent_dir / "provider_health.json"),
            mock.patch.object(router, "GATEWAY_PATH", self.agent_dir / "gateway.json"),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    # helpers ----------------------------------------------------------

    def enable_provider(self, provider_id="openai"):
        providers = router.list_providers()
        for provider in providers:
            if provider["id"] == provider_id:
                provider["enabled"] = True
        router.save_providers(providers)

    def restrict_models(self, keep_tiers):
        models = []
        for model in router.list_models():
            if model["provider_id"] == "openai" and model["quality_tier"] in keep_tiers:
                models.append(model)
        router.save_models(models)


class ZeroConfigurationTests(RouterIsolationHarness):
    def test_status_is_ide_model_without_any_provider_or_key(self):
        status = router.router_status(self.root)
        self.assertEqual(status["execution_mode"], "IDE_MODEL")
        self.assertEqual(status["effective_mode"], "IDE_MODEL")
        self.assertTrue(status["mode_available"]["IDE_MODEL"])
        self.assertFalse(status["mode_available"]["HYBRID"])
        self.assertFalse(status["mode_available"]["AUTO_ROUTER"])
        self.assertEqual(status["router_component"], "NOT ENABLED")

    def test_route_without_providers_is_blocked_not_faked(self):
        decision = router.route_task(
            self.root, {"task_type": "QUICK_EDIT", "risk": "LOW"}, record=False)
        self.assertEqual(decision["decision"], "ROUTING_BLOCKED")
        self.assertTrue(decision["blocked"])
        self.assertIsNone(decision["final_model"])

    def test_auto_router_requires_gateway_honest_message(self):
        try:
            from config_store import set_value
            set_value("PROJECT", "execution.mode", "AUTO_ROUTER", self.root)
        except Exception:
            router_path = self.root / ".context" / "config.json"
            router_path.write_text(json.dumps({"execution.mode": "AUTO_ROUTER"}), encoding="utf-8")
        status = router.router_status(self.root)
        self.assertEqual(status["effective_mode"], "IDE_MODEL")
        self.assertIn("AUTO ROUTER NOT AVAILABLE", status["mode_note"])


class DeterministicRoutingTests(RouterIsolationHarness):
    def setUp(self):
        super().setUp()
        self.enable_provider("openai")
        secret_store.set_secret("openai", "sk-test-1234567890")
        self.restrict_models(("ECONOMY", "BALANCED", "STRONG", "FRONTIER"))

    def test_selection_is_deterministic_and_cheapest_sufficient(self):
        analysis = {"task_type": "QUICK_EDIT", "risk": "LOW"}
        first = router.route_task(self.root, analysis, record=False)
        second = router.route_task(self.root, analysis, record=False)
        self.assertEqual(first["final_model"], second["final_model"])
        self.assertEqual(first["decision"], "ROUTED")
        # cheapest sufficient tier must not be FRONTIER for a LOW-risk edit
        self.assertIn(first["model"]["quality_tier"], ("ECONOMY", "BALANCED"))

    def test_risk_floor_raises_tier(self):
        decision = router.route_task(
            self.root, {"task_type": "QUICK_EDIT", "risk": "CRITICAL"}, record=False)
        self.assertEqual(decision["decision"], "ROUTED")
        self.assertIn(decision["model"]["quality_tier"], ("STRONG", "FRONTIER"))

    def test_quality_guardrail_blocks_instead_of_downgrading(self):
        # keep only ECONOMY models; a SECURITY task requires STRONG+
        self.restrict_models(("ECONOMY",))
        decision = router.route_task(
            self.root, {"task_type": "SECURITY", "risk": "HIGH"}, record=False)
        self.assertEqual(decision["decision"], "ROUTING_BLOCKED")
        self.assertEqual(decision["reason"], "quality_guardrail")
        self.assertIsNone(decision["final_model"])

    def test_reasoning_requirement_raises_to_strong(self):
        self.restrict_models(("ECONOMY", "BALANCED", "STRONG"))
        decision = router.route_task(
            self.root,
            {"task_type": "REFACTOR", "risk": "LOW", "reasoning_requirement": "high"},
            record=False)
        self.assertEqual(decision["required_tier"], "STRONG")
        self.assertEqual(decision["model"]["quality_tier"], "STRONG")

    def test_critical_risk_and_extreme_complexity_require_frontier(self):
        critical = router.route_task(
            self.root, {"task_type": "QUICK_EDIT", "risk": "CRITICAL"}, record=False)
        extreme = router.route_task(
            self.root, {"task_type": "FIX", "risk": "LOW", "complexity": "EXTREME"},
            record=False)
        self.assertEqual(critical["required_tier"], "FRONTIER")
        self.assertEqual(extreme["required_tier"], "FRONTIER")
        self.assertEqual(critical["model"]["quality_tier"], "FRONTIER")
        self.assertEqual(extreme["model"]["quality_tier"], "FRONTIER")

    def test_required_tools_never_routes_to_incapable_model(self):
        models = router.list_models()
        for model in models:
            if model.get("provider_id") == "openai":
                model["capabilities"] = ["chat"]
        router.save_models(models)
        decision = router.route_task(
            self.root,
            {"task_type": "QUICK_EDIT", "risk": "LOW", "requires_tools": True},
            record=False)
        self.assertEqual(decision["decision"], "ROUTING_BLOCKED")
        self.assertEqual(decision["reason"], "capability_guardrail")

    def test_escalation_chain_goes_upward_only(self):
        self.restrict_models(("ECONOMY", "BALANCED", "STRONG", "FRONTIER"))
        decision = router.route_task(
            self.root, {"task_type": "QUICK_EDIT", "risk": "LOW"}, record=False)
        chosen_tier = decision["model"]["quality_tier"]
        from router import TIER_ORDER
        for tier in decision["escalation_chain"]:
            self.assertGreater(TIER_ORDER[tier], TIER_ORDER[chosen_tier])

    def test_invalid_or_cyclic_escalation_chain_is_bounded(self):
        policy = router.load_policy(self.root)
        policy["escalation_chain"] = ["STRONG", "BALANCED", "STRONG",
                                      "BOGUS", "ECONOMY"]
        router.save_policy(self.root, policy)
        decision = router.route_task(
            self.root, {"task_type": "QUICK_EDIT", "risk": "LOW"},
            record=False)
        self.assertEqual(decision["decision"], "ROUTED")
        self.assertEqual(len(decision["escalation_chain"]),
                         len(set(decision["escalation_chain"])))
        chosen = router.TIER_ORDER[decision["model"]["quality_tier"]]
        self.assertTrue(all(router.TIER_ORDER[tier] > chosen
                            for tier in decision["escalation_chain"]))

    def test_cost_guardrail_block(self):
        policy = router.load_policy(self.root)
        policy["guardrails"]["cost"] = {"action": "BLOCK", "task_limit_usd": 0.00001,
                                        "daily_limit_usd": 5.0, "monthly_limit_usd": 50.0}
        router.save_policy(self.root, policy)
        decision = router.route_task(
            self.root, {"task_type": "ARCHITECTURE", "risk": "HIGH"},
            record=False, context_tokens=50000)
        self.assertEqual(decision["decision"], "ROUTING_BLOCKED")
        self.assertEqual(decision["reason"], "cost_guardrail")

    def test_history_and_explain_and_usage(self):
        decision = router.route_task(
            self.root, {"task_type": "REFACTOR", "risk": "MEDIUM"},
            record=True, task_text="refactor payments")
        route_id = router.history(self.root, limit=1)[0]["id"]
        explained = router.explain_route(self.root, route_id)
        self.assertEqual(explained["final_model"], decision["final_model"])
        self.assertEqual(explained["policy_version"], decision["policy_version"])
        usage = router.usage_summary(self.root)
        self.assertGreaterEqual(usage["today"]["routes"], 1)

    def test_policy_version_increments_on_save(self):
        policy = router.load_policy(self.root)
        version = policy["policy_version"]
        saved = router.save_policy(self.root, policy)
        self.assertEqual(saved["policy_version"], version + 1)


class HybridDelegationTests(RouterIsolationHarness):
    def test_delegation_executes_and_records_router_hybrid(self):
        """GAP 1: delegate_task actually executes against the selected provider."""
        self.enable_provider("openai")
        secret_store.set_secret("openai", "sk-test-1234567890")
        self.restrict_models(("BALANCED",))
        # Mock execute_with_model to avoid real HTTP calls.
        mock_result = {
            "ok": True, "model_id": "openai/gpt-4o-mini",
            "executed_by": "openai/gpt-4o-mini",
            "text": "Here is the feature implementation...",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "estimated_cost_usd": 0.0001, "duration_ms": 1200,
        }
        with mock.patch.object(router, "execute_with_model", return_value=mock_result):
            decision = router.delegate_task(
                self.root, {"task_type": "NEW_FEATURE", "risk": "MEDIUM"},
                task_text="add feature", prompt="Implement the feature")
        self.assertEqual(decision["executed_by"], "router_hybrid")
        self.assertEqual(decision["delegation"]["primary_executor"], "ide_model")
        self.assertEqual(decision["delegation"]["delegated_executor"], "openai/gpt-4o-mini")
        self.assertTrue(decision["delegation"]["executed"])
        self.assertEqual(decision["delegation"]["result"], "Here is the feature implementation...")
        self.assertEqual(decision["outcome"], "delegated")
        record = router.history(self.root, limit=1)[0]
        self.assertEqual(record["executed_by"], "router_hybrid")

    def test_delegation_failure_classifies_no_api_key(self):
        """GAP 1: NO_API_KEY failure classification."""
        self.enable_provider("openai")
        # No secret set → execute_with_model returns no_api_key.
        self.restrict_models(("BALANCED",))
        # Mock route_task to return a successful routing decision (bypass guardrail).
        routed = {"decision": "ROUTED", "final_model": "openai/gpt-4o-mini",
                  "provider_id": "openai", "initial_model": "openai/gpt-4o-mini",
                  "required_tier": "BALANCED", "task_type": "NEW_FEATURE",
                  "risk": "MEDIUM", "complexity": "MEDIUM",
                  "reasons": [], "policy_version": 1, "executed_by": "none"}
        with mock.patch.object(router, "route_task", return_value=routed):
            decision = router.delegate_task(
                self.root, {"task_type": "NEW_FEATURE", "risk": "MEDIUM"},
                task_text="add feature", prompt="Implement the feature")
        self.assertEqual(decision["executed_by"], "ide_model")
        self.assertFalse(decision["delegation"]["executed"])
        self.assertEqual(decision["delegation"]["failure_class"], "NO_API_KEY")
        self.assertEqual(decision["outcome"], "delegation_failed")

    def test_delegation_failure_classifies_network(self):
        """GAP 1: NETWORK failure classification."""
        self.enable_provider("openai")
        secret_store.set_secret("openai", "sk-test-1234567890")
        self.restrict_models(("BALANCED",))
        mock_result = {"error": "execution_failed", "detail": "connection timeout",
                       "model_id": "openai/gpt-4o-mini"}
        with mock.patch.object(router, "execute_with_model", return_value=mock_result):
            decision = router.delegate_task(
                self.root, {"task_type": "NEW_FEATURE", "risk": "MEDIUM"},
                task_text="add feature", prompt="Implement the feature")
        self.assertEqual(decision["delegation"]["failure_class"], "NETWORK")

    def test_delegation_blocked_returns_ide_model(self):
        """GAP 1: routing blocked → executed_by=ide_model, no execution."""
        decision = router.delegate_task(
            self.root, {"task_type": "QUICK_EDIT", "risk": "LOW"},
            task_text="fix typo")
        self.assertEqual(decision["executed_by"], "ide_model")
        self.assertFalse(decision["delegation"]["executed"])

    def test_delegation_no_prompt_skips_execution(self):
        """GAP 1: no prompt → no execution."""
        self.enable_provider("openai")
        secret_store.set_secret("openai", "sk-test-1234567890")
        self.restrict_models(("BALANCED",))
        decision = router.delegate_task(
            self.root, {"task_type": "NEW_FEATURE", "risk": "MEDIUM"},
            task_text="", prompt="")
        self.assertEqual(decision["executed_by"], "ide_model")
        self.assertFalse(decision["delegation"]["executed"])


class SecretStoreTests(RouterIsolationHarness):
    def test_mask_only_output_never_contains_raw_key(self):
        raw = "sk-supersecretkey-abcdef-987654"
        result = secret_store.set_secret("openai", raw)
        self.assertNotIn(raw, json.dumps(result))
        listing = json.dumps(secret_store.list_secrets())
        self.assertNotIn(raw, listing)
        self.assertIn("sk-...7654", listing)

    def test_get_secret_returns_value_internally(self):
        raw = "sk-internal-check-1111"
        secret_store.set_secret("anthropic", raw)
        self.assertEqual(secret_store.get_secret("anthropic"), raw)
        self.assertIsNone(secret_store.get_secret("missing"))

    def test_replace_and_delete(self):
        secret_store.set_secret("openai", "sk-first-key-111")
        secret_store.set_secret("openai", "sk-second-key-222")
        self.assertEqual(secret_store.get_secret("openai"), "sk-second-key-222")
        result = secret_store.delete_secret("openai")
        self.assertTrue(result["ok"])
        self.assertFalse(secret_store.has_secret("openai"))
        self.assertIsNone(secret_store.get_secret("openai"))

    def test_secrets_file_is_outside_project_and_not_in_context(self):
        secret_store.set_secret("openai", "sk-location-check-333")
        self.assertTrue(secret_store.secrets_path().exists())
        self.assertTrue(str(secret_store.secrets_path()).startswith(str(self.home)))
        # nothing secret-shaped inside the project's .context directory
        for path in (self.root / ".context").rglob("*"):
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="ignore")
                self.assertNotIn("sk-location-check-333", content)


class FailureIsolationTests(RouterIsolationHarness):
    def test_context_core_imports_do_not_require_router(self):
        """PROTECTED CORE: context modules import cleanly with router absent."""
        import importlib

        for name in ("paths", "route", "ledger", "memory", "budget"):
            module = importlib.import_module(name)
            self.assertTrue(module)

    def test_execute_without_key_fails_cleanly(self):
        self.enable_provider("openai")
        result = router.execute_with_model(self.root, "openai/gpt-4o-mini", "hi")
        self.assertEqual(result["error"], "no_api_key")

    def test_execution_exception_redacts_provider_key(self):
        self.enable_provider("openai")
        raw = "sk-redact-me-123456"
        secret_store.set_secret("openai", raw)
        with mock.patch.object(router.urllib.request, "urlopen",
                               side_effect=RuntimeError(f"provider echoed {raw}")):
            result = router.execute_with_model(
                self.root, "openai/gpt-4o-mini", "hi")
        self.assertNotIn(raw, json.dumps(result))
        self.assertIn("[REDACTED]", result["detail"])


if __name__ == "__main__":
    unittest.main()
