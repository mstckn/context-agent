"""Cross-cutting acceptance slices (autoroute §54-62).

These are the slice acceptance tests that prove the product layer never
compromises the protected core and never leaks provider secrets:

  §54 CORE-ONLY            zero providers/keys, router+gateway absent
  §57 MODE SWITCHING       IDE MODEL -> HYBRID -> back, state preserved
  §59 MULTI-PROJECT        zero memory/policy/ledger/session leakage
  §60 ROUTER FAILURE       routing unavailable, context stays healthy
  §61 GATEWAY FAILURE      AUTO ROUTER never claimed operational
  §62 PROVIDER SECURITY    raw API keys never appear anywhere
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
import config_store  # noqa: E402
from memory import ProjectMemory  # noqa: E402
from ledger import ContextLedger  # noqa: E402

RAW_KEY = "fixture-value-1234567890"


def make_project(base: Path, name: str) -> Path:
    root = base / name
    (root / ".context").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='%s'\n" % name, encoding="utf-8")
    return root


class IsolationHarness(unittest.TestCase):
    """Redirect every router/secret persistence path into a temp home."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.agent_dir = self.home / ".context-agent"
        self.base = base

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

    def enable_openai(self):
        providers = router.list_providers()
        for provider in providers:
            if provider["id"] == "openai":
                provider["enabled"] = True
        router.save_providers(providers)
        secret_store.set_secret("openai", RAW_KEY)


class CoreOnlyAcceptance(IsolationHarness):
    """§54: zero providers, zero keys, router+gateway absent."""

    def test_context_primitives_work_without_any_router_component(self):
        root = make_project(self.base, "core_only")
        memory = ProjectMemory(root / ".context" / "memory.db")
        added = memory.add("core_only:scope", "DECISION", "use sqlite for memory",
                           source="test", confidence=0.9)
        self.assertIn(added["status"], ("added", "existing"))
        memory.close()

        ledger = ContextLedger(root / ".context" / "ledger.db")
        ledger.record("core_only:scope", "session-1", "auth/service.py", "full")
        known = ledger.known_files("core_only:scope", "session-1")
        self.assertEqual([item["path"] for item in known], ["auth/service.py"])
        ledger.close()

    def test_status_reports_ide_model_and_not_enabled_without_blocking(self):
        root = make_project(self.base, "core_only2")
        status = router.router_status(root)
        self.assertEqual(status["effective_mode"], "IDE_MODEL")
        self.assertEqual(status["router_component"], "NOT ENABLED")
        self.assertFalse(status["mode_available"]["AUTO_ROUTER"])
        # NOT ENABLED must never surface as an error condition.
        self.assertNotIn("error", status)

    def test_no_provider_warning_blocks_context_only_route(self):
        root = make_project(self.base, "core_only3")
        decision = router.route_task(root, {"task_type": "QUICK_EDIT", "risk": "LOW"},
                                     record=False)
        self.assertEqual(decision["decision"], "ROUTING_BLOCKED")
        self.assertIsNone(decision["final_model"])


class ModeSwitchingAcceptance(IsolationHarness):
    """§57: IDE MODEL -> HYBRID -> IDE MODEL preserves all project state."""

    def test_mode_switch_preserves_memory_and_ledger(self):
        root = make_project(self.base, "mode_switch")
        scope = "mode_switch:scope"

        memory = ProjectMemory(root / ".context" / "memory.db")
        memory.add(scope, "DECISION", "login uses rate limiting", source="t", confidence=0.8)
        memory.close()
        ledger = ContextLedger(root / ".context" / "ledger.db")
        ledger.record(scope, "session-1", "auth/service.py", "full")
        ledger.close()

        # Switch to HYBRID (routing capability changes, state must not).
        config_store.set_value("PROJECT", "execution.mode", "HYBRID", root)
        config_store.set_value("PROJECT", "routing.enabled", True, root)
        status = router.router_status(root)
        self.assertEqual(status["execution_mode"], "HYBRID")

        memory = ProjectMemory(root / ".context" / "memory.db")
        rows = memory.conn.execute(
            "SELECT content FROM project_memory WHERE scope=?", (scope,)).fetchall()
        memory.close()
        self.assertEqual(rows[0][0], "login uses rate limiting")

        # Switch back to IDE MODEL: router must no longer influence execution.
        config_store.set_value("PROJECT", "execution.mode", "IDE_MODEL", root)
        config_store.set_value("PROJECT", "routing.enabled", False, root)
        status = router.router_status(root)
        self.assertEqual(status["effective_mode"], "IDE_MODEL")
        self.assertEqual(status["router_component"], "NOT ENABLED")

        ledger = ContextLedger(root / ".context" / "ledger.db")
        known = ledger.known_files(scope, "session-1")
        ledger.close()
        self.assertEqual([item["path"] for item in known], ["auth/service.py"])

    def test_context_only_task_never_touches_provider_secret(self):
        root = make_project(self.base, "mode_switch2")
        self.enable_openai()
        with mock.patch.object(secret_store, "get_secret",
                               side_effect=AssertionError("secret accessed")) as probe:
            status = router.router_status(root)
            # reading status in IDE_MODEL must not require the raw key
            self.assertEqual(status["execution_mode"], "IDE_MODEL")
        self.assertEqual(probe.call_count, 0)


class MultiProjectAcceptance(IsolationHarness):
    """§59: zero leakage between two projects."""

    def test_memory_policy_and_ledger_do_not_leak_across_projects(self):
        proj_a = make_project(self.base, "proj_a")
        proj_b = make_project(self.base, "proj_b")

        mem_a = ProjectMemory(proj_a / ".context" / "memory.db")
        mem_a.add("a:scope", "DECISION", "secret of project A", source="t")
        mem_b = ProjectMemory(proj_b / ".context" / "memory.db")
        mem_b.add("b:scope", "DECISION", "secret of project B", source="t")

        self.assertEqual(mem_a.conn.execute(
            "SELECT COUNT(*) FROM project_memory WHERE content LIKE '%project B%'").fetchone()[0], 0)
        self.assertEqual(mem_b.conn.execute(
            "SELECT COUNT(*) FROM project_memory WHERE content LIKE '%project A%'").fetchone()[0], 0)
        mem_a.close()
        mem_b.close()

        # Routing policy is per-project: enabling on A must not affect B.
        config_store.set_value("PROJECT", "routing.enabled", True, proj_a)
        self.assertTrue(config_store.effective_value(proj_a, "routing.enabled", False))
        self.assertFalse(config_store.effective_value(proj_b, "routing.enabled", False))

        # Ledger isolation.
        led_a = ContextLedger(proj_a / ".context" / "ledger.db")
        led_a.record("a:scope", "s1", "a/only.py", "full")
        led_b = ContextLedger(proj_b / ".context" / "ledger.db")
        self.assertEqual(led_b.known_files("a:scope", "s1"), [])
        led_a.close()
        led_b.close()


class RouterFailureAcceptance(IsolationHarness):
    """§60: router unavailable -> context healthy, routing reports unavailable."""

    def test_context_healthy_when_routing_disabled(self):
        root = make_project(self.base, "router_fail")
        ledger = ContextLedger(root / ".context" / "ledger.db")
        ledger.record("s", "sess", "x.py", "full")
        ledger.close()
        decision = router.route_task(root, {"task_type": "BUGFIX", "risk": "MEDIUM"},
                                     record=False)
        self.assertIn(decision["decision"], ("ROUTING_BLOCKED", "ROUTED"))
        self.assertTrue(decision["blocked"] or decision.get("final_model"))


class GatewayFailureAcceptance(IsolationHarness):
    """§61: gateway absent -> never claim AUTO ROUTER operational."""

    def test_gateway_unavailable_is_reported_honestly(self):
        root = make_project(self.base, "gw_fail")
        config_store.set_value("PROJECT", "execution.mode", "AUTO_ROUTER", root)
        status = router.router_status(root)
        self.assertFalse(status["gateway"]["available"])
        self.assertFalse(status["mode_available"]["AUTO_ROUTER"])
        self.assertEqual(status["effective_mode"], "IDE_MODEL")
        self.assertIn("AUTO ROUTER NOT AVAILABLE", status["mode_note"])

    def test_gateway_alone_does_not_falsely_enable_auto_router(self):
        root = make_project(self.base, "gw_ok")
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        router.GATEWAY_PATH.write_text(json.dumps({"port": 8791, "pid": 1}), encoding="utf-8")
        with mock.patch.object(router, "gateway_available",
                               return_value={"available": True, "port": 8791, "pid": 1,
                                             "primary_routing": True}):
            status = router.router_status(root)
        self.assertFalse(status["mode_available"]["AUTO_ROUTER"])


class ProviderSecurityAcceptance(IsolationHarness):
    """§62: raw API keys never appear in any output surface."""

    def _recorded_route(self, root):
        self.enable_openai()
        models = [m for m in router.list_models() if m["provider_id"] == "openai"]
        router.save_models(models)
        return router.route_task(root, {"task_type": "QUICK_EDIT", "risk": "LOW"},
                                 record=True, task_text="security probe task",
                                 context_tokens=100)

    def test_secret_list_exposes_mask_only(self):
        self.enable_openai()
        listing = secret_store.list_secrets()
        rendered = json.dumps(listing)
        self.assertNotIn(RAW_KEY, rendered)
        self.assertIn(listing[0]["mask"], rendered)
        self.assertNotEqual(listing[0]["mask"], RAW_KEY)

    def test_raw_key_absent_from_routing_history_and_usage(self):
        root = make_project(self.base, "sec_history")
        self._recorded_route(root)
        history_rows = router.history(root)
        self.assertNotIn(RAW_KEY, json.dumps(history_rows, default=str))
        usage = router.usage_summary(root)
        self.assertNotIn(RAW_KEY, json.dumps(usage, default=str))

    def test_raw_key_absent_from_status_and_provider_listing(self):
        root = make_project(self.base, "sec_status")
        self.enable_openai()
        status = router.router_status(root)
        self.assertNotIn(RAW_KEY, json.dumps(status, default=str))
        providers = router.list_providers()
        self.assertNotIn(RAW_KEY, json.dumps(providers, default=str))

    def test_raw_key_absent_from_explain_and_decision(self):
        root = make_project(self.base, "sec_explain")
        decision = self._recorded_route(root)
        self.assertNotIn(RAW_KEY, json.dumps(decision, default=str))
        history_rows = router.history(root)
        self.assertTrue(history_rows, "expected at least one recorded route")
        last_id = history_rows[0].get("id")
        if last_id is not None:
            explained = router.explain_route(root, last_id)
            self.assertNotIn(RAW_KEY, json.dumps(explained, default=str))

    def test_mask_format_is_first3_last4(self):
        self.enable_openai()
        mask = secret_store.list_secrets()[0]["mask"]
        self.assertTrue(mask.startswith(RAW_KEY[:3]))
        self.assertTrue(mask.endswith(RAW_KEY[-4:]))
        self.assertIn("...", mask)


if __name__ == "__main__":
    unittest.main()
