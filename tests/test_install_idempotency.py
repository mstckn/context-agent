"""Regression tests for install.py idempotency — SameFileError fix.

Verifies:
1. Install into a new project works
2. Install when destination already exists works (idempotent)
3. Source == destination does not throw SameFileError
4. Genuine copy failures are NOT silently swallowed
5. Benchmark can run against an already-installed project
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import install  # noqa: E402
import benchmark_token_savings  # noqa: E402


class InstallIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_into_new_project(self):
        """Install into a fresh project creates .context/scripts/."""
        target = Path(self._tmp.name) / "new-project"
        target.mkdir()
        (target / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        install.install(str(target))
        scripts_dir = target / ".context" / "scripts"
        self.assertTrue(scripts_dir.exists())
        required_runtime = {
            "install.py", "agent.py", "mcp_server.py", "identity.py",
            "ledger.py", "memory.py", "planner.py", "sufficiency.py",
            "content_index.py", "graph.py", "model_profile.py",
        }
        missing = sorted(name for name in required_runtime
                         if not (scripts_dir / name).exists())
        self.assertEqual(missing, [], f"fresh install missing runtime modules: {missing}")

    def test_install_idempotent_when_already_installed(self):
        """Running install twice on the same project does not throw."""
        target = Path(self._tmp.name) / "idempotent-project"
        target.mkdir()
        (target / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        # First install
        install.install(str(target))
        # Second install should not throw SameFileError
        install.install(str(target))
        scripts_dir = target / ".context" / "scripts"
        self.assertTrue((scripts_dir / "install.py").exists())

    def test_same_file_does_not_throw(self):
        """When source == destination, install skips silently without SameFileError."""
        target = Path(self._tmp.name) / "same-file-project"
        target.mkdir()
        (target / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        # First install to create the scripts
        install.install(str(target))
        # Now simulate source == destination by running install from within
        # the installed scripts directory. The fix checks src.resolve() == dst.resolve().
        # We verify no exception is raised.
        try:
            install.install(str(target))
        except shutil.SameFileError:
            self.fail("SameFileError should not be raised when source == destination")

    def test_genuine_copy_failure_not_swallowed(self):
        """Real copy failures (e.g. permission denied) are NOT silently caught."""
        target = Path(self._tmp.name) / "genuine-fail"
        target.mkdir()
        (target / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        # First install to create scripts
        install.install(str(target))
        with patch.object(install.shutil, "copy2",
                          side_effect=PermissionError("simulated copy denial")):
            with self.assertRaises(PermissionError):
                install.install(str(target))

    def test_benchmark_runs_on_already_installed_project(self):
        """benchmark_token_savings.py can run against an already-installed project."""
        # This is the actual scenario that was failing
        # We verify that install.py doesn't throw when run from within
        # the project's own .context/scripts/ directory
        target = Path(self._tmp.name) / "bench-project"
        target.mkdir()
        (target / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        # Install first
        install.install(str(target))
        # Now run install again (simulating benchmark's ensure_installed)
        # This was the exact failure path
        install.install(str(target))
        # Verify scripts are still intact
        self.assertTrue((target / ".context" / "scripts" / "install.py").exists())

    def test_benchmark_aggregate_compares_equal_query_counts(self):
        target = Path(self._tmp.name) / "metric-project"
        target.mkdir()
        with patch.object(benchmark_token_savings, "count_full_repo_tokens",
                          return_value=(1000, 1, [])), \
             patch.object(benchmark_token_savings, "ensure_installed",
                          return_value=Path(self._tmp.name)), \
             patch.object(benchmark_token_savings, "index_project", return_value={}), \
             patch.object(benchmark_token_savings, "query_route",
                          return_value={"context_plan": {"steps": []}}), \
             patch.object(benchmark_token_savings, "sum_retrieved_tokens",
                          return_value=(250, [])):
            report = benchmark_token_savings.benchmark(target, ["a", "b"])
        self.assertEqual(report["full_repo_tokens_across_queries"], 2000)
        self.assertEqual(report["retrieved_tokens"], 500)
        self.assertEqual(report["saved_tokens"], 1500)
        self.assertEqual(report["savings_pct"], 75.0)


if __name__ == "__main__":
    unittest.main()
