"""Tests for scripts/config_store.py — layered configuration (PHASE C)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import config_store  # noqa: E402


def make_project(base: Path, name: str = "proj") -> Path:
    root = base / name
    (root / ".context").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return root


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.root = make_project(Path(self._tmp.name))
        # isolate GLOBAL layer into the temp home
        self._home_patch = mock.patch.object(Path, "home", return_value=self.home)
        self._home_patch.start()

    def tearDown(self):
        self._home_patch.stop()
        self._tmp.cleanup()

    def test_system_defaults_are_ide_model_and_keyless(self):
        eff = config_store.effective(self.root)
        self.assertEqual(eff["execution.mode"]["value"], "IDE_MODEL")
        self.assertEqual(eff["execution.mode"]["source"], "SYSTEM")
        self.assertFalse(eff["routing.enabled"]["value"])

    def test_layer_precedence_system_global_project_session_task(self):
        config_store.set_value("GLOBAL", "budget.default_tokens", 4000, self.root)
        config_store.set_value("PROJECT", "budget.default_tokens", 6000, self.root)
        eff = config_store.effective(self.root, key="budget.default_tokens")
        self.assertEqual(eff["budget.default_tokens"]["value"], 6000)
        self.assertEqual(eff["budget.default_tokens"]["source"], "PROJECT")

        config_store.set_value("SESSION", "budget.default_tokens", 2000, self.root)
        eff = config_store.effective(self.root, key="budget.default_tokens")
        self.assertEqual(eff["budget.default_tokens"]["value"], 2000)
        self.assertEqual(eff["budget.default_tokens"]["source"], "SESSION")

        eff = config_store.effective(self.root, key="budget.default_tokens",
                                     task_overrides={"budget.default_tokens": 999})
        self.assertEqual(eff["budget.default_tokens"]["value"], 999)
        self.assertEqual(eff["budget.default_tokens"]["source"], "TASK")

    def test_reset_to_inherited(self):
        config_store.set_value("PROJECT", "budget.default_tokens", 6000, self.root)
        config_store.set_value("SESSION", "budget.default_tokens", 2000, self.root)
        result = config_store.reset_key("SESSION", "budget.default_tokens", self.root)
        self.assertEqual(result["removed"], ["budget.default_tokens"])
        eff = config_store.effective(self.root, key="budget.default_tokens")
        self.assertEqual(eff["budget.default_tokens"]["value"], 6000)
        self.assertEqual(eff["budget.default_tokens"]["source"], "PROJECT")

    def test_task_overrides_never_persisted(self):
        config_store.effective(self.root, task_overrides={"execution.mode": "HYBRID"})
        for layer in ("GLOBAL", "PROJECT", "SESSION"):
            self.assertNotIn("execution.mode", config_store.get_layer(layer, self.root))
        # SYSTEM/TASK layers are not writable through the store
        self.assertIn("error", config_store.set_value("TASK", "execution.mode", "HYBRID", self.root))
        self.assertIn("error", config_store.set_value("SYSTEM", "execution.mode", "HYBRID", self.root))

    def test_routing_preferences_live_at_project_layer(self):
        """Cross-IDE requirement (§17): routing config follows the project,
        not the IDE. It must be stored inside <root>/.context/."""
        config_store.set_value("PROJECT", "routing.enabled", True, self.root)
        path = self.root / ".context" / "config.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIs(data["routing.enabled"], True)
        # independent of any GLOBAL (machine) location
        self.assertFalse(config_store.global_config_path().exists())

    def test_type_coercion_follows_system_defaults(self):
        config_store.set_value("PROJECT", "routing.enabled", "true", self.root)
        config_store.set_value("PROJECT", "budget.default_tokens", "12000", self.root)
        eff = config_store.effective(self.root)
        self.assertIs(eff["routing.enabled"]["value"], True)
        self.assertEqual(eff["budget.default_tokens"]["value"], 12000)

    def test_unknown_keys_visible_but_flagged(self):
        config_store.set_value("PROJECT", "custom.flag", "x", self.root)
        eff = config_store.effective(self.root, key="custom.flag")
        self.assertEqual(eff["custom.flag"]["value"], "x")
        self.assertFalse(eff["custom.flag"]["known"])

    def test_effective_value_helper_and_defaults(self):
        self.assertEqual(
            config_store.effective_value(self.root, "budget.default_tokens"), 8000)
        self.assertEqual(
            config_store.effective_value(self.root, "no.such.key", default="dflt"), "dflt")


if __name__ == "__main__":
    unittest.main()
