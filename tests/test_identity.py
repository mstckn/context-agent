"""Tests for scripts/identity.py — cross-IDE identity & continuity (PHASE B)."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import identity  # noqa: E402


def make_project(base: Path, name: str = "proj") -> Path:
    root = base / name
    (root / ".context").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return root


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_full_identity_persists_anchors(self):
        block = identity.full_identity(self.root)
        for key in ("project_id", "repository_id", "checkout_id",
                    "ide_instance_id", "conversation_id", "task_id", "scope"):
            self.assertTrue(block.get(key), f"missing identity element {key}")
        self.assertTrue(identity.identity_path(self.root).exists())
        # second call: stable ids, no churn
        block2 = identity.full_identity(self.root)
        for key in ("project_id", "repository_id", "checkout_id", "ide_instance_id"):
            self.assertEqual(block[key], block2[key], key)

    def test_path_is_not_identity_same_project_id_after_move(self):
        pid = identity.full_identity(self.root)["project_id"]
        moved = Path(self._tmp.name) / "renamed-elsewhere"
        # Use shutil.move instead of os.replace: on Windows, os.replace fails
        # when the OS still holds file handles inside the directory (e.g. from
        # recent write_text/read_text calls). shutil.move handles this properly
        # by falling back to copy+delete when rename is blocked.
        shutil.move(str(self.root), str(moved))
        record = json.loads((moved / ".context" / "project_id.json").read_text(encoding="utf-8"))
        self.assertEqual(record["project_id"], pid)

    def test_two_checkouts_same_repo_get_distinct_checkout_ids(self):
        a = make_project(Path(self._tmp.name), "clone-a")
        b = make_project(Path(self._tmp.name), "clone-b")
        self.assertNotEqual(identity.checkout_id(a), identity.checkout_id(b))

    def test_repository_id_falls_back_to_project_id_without_git(self):
        repo = identity.repository_id(self.root)
        self.assertTrue(repo.startswith("repo-"))
        # deterministic on repeat
        self.assertEqual(repo, identity.repository_id(self.root))

    def test_scope_levels_are_distinct(self):
        scopes = {
            level: identity.scope_key_for(level, self.root, model_session="sess-1")
            for level in identity.SCOPE_LEVELS
        }
        # every level resolves to a non-empty normalized scope
        for level, scope in scopes.items():
            self.assertTrue(scope, level)
        # narrower levels must not collapse onto PROJECT_GLOBAL
        self.assertNotEqual(scopes["PROJECT_GLOBAL"], scopes["MODEL_SESSION"])
        self.assertNotEqual(scopes["PROJECT_GLOBAL"], scopes["CONVERSATION"])
        self.assertNotEqual(scopes["CHECKOUT"], scopes["REPOSITORY"])

    def test_scope_level_validation(self):
        with self.assertRaises(ValueError):
            identity.scope_key_for("GALAXY", self.root)

    def test_handoff_semantics_project_memory_is_not_model_knowledge(self):
        handoff = identity.session_handoff(self.root, model_session="s1")
        sem = handoff["semantics"]
        self.assertFalse(sem["project_memory_is_model_knowledge"])
        self.assertFalse(sem["known_to_model_transferable"])
        self.assertIn("identity", handoff)
        self.assertIn("model_sessions", handoff)
        # a project without a ledger must not fake known state
        self.assertFalse(handoff["model_sessions"].get("sessions"))

    def test_continuity_missing_then_verified(self):
        fresh = make_project(Path(self._tmp.name), "fresh")
        report = identity.cross_ide_continuity(fresh)
        self.assertIn(report["cross_ide_continuity"], ("MISSING", "PARTIAL"))
        # materialize anchors
        identity.full_identity(fresh)
        report2 = identity.cross_ide_continuity(fresh)
        self.assertIn(report2["cross_ide_continuity"], ("VERIFIED", "PARTIAL"))
        self.assertEqual(report2["checks"]["project_id"]["status"], "present")
        self.assertEqual(report2["checks"]["scope_stability"]["status"], "present")

    def test_optimistic_concurrency_rejects_stale_version(self):
        data = identity.load_identity(self.root)
        version = int(data.get("version", 0))
        self.assertTrue(identity._write_identity(self.root, {"checkout_id": "chk-a"}, version))
        # same expected version again -> another writer moved on -> reject
        self.assertFalse(identity._write_identity(self.root, {"checkout_id": "chk-b"}, version))
        self.assertEqual(identity.load_identity(self.root)["checkout_id"], "chk-a")

    def test_env_overrides_win(self):
        os.environ["CONTEXT_AGENT_TASK_ID"] = "T-42"
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "C-9"
        try:
            self.assertEqual(identity.task_id(), "t-42")
            self.assertEqual(identity.conversation_id(self.root), "c-9")
        finally:
            del os.environ["CONTEXT_AGENT_TASK_ID"]
            del os.environ["CONTEXT_AGENT_CONVERSATION_ID"]

    def test_new_conversation_rotates(self):
        first = identity.conversation_id(self.root)
        second = identity.new_conversation(self.root)
        self.assertNotEqual(first, second)
        self.assertEqual(identity.conversation_id(self.root), second)


class LedgerKnowledgeIsolationTests(unittest.TestCase):
    """Project Memory != Model Knowledge: a NEW model session must start with
    zero known_to_model state (protected core semantics, asserted here as a
    cross-IDE acceptance gate)."""

    def test_new_session_has_no_known_files(self):
        sys.path.insert(0, str(SCRIPTS))
        from ledger import ContextLedger

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "symbols.db"
            ledger = ContextLedger(db)
            ledger.record("scope-x", "session-old", "a.py", "full",
                          content_hash="h1", token_cost=100)
            self.assertEqual(
                [item["path"] for item in ledger.known_files("scope-x", "session-old")],
                ["a.py"],
            )
            # different model session, same project scope: nothing known
            self.assertEqual(ledger.known_files("scope-x", "session-new"), [])
            status = ledger.status("scope-x", "session-new", "a.py", new_hash="h1")
            self.assertEqual(status["decision"], "send")
            ledger.close()


if __name__ == "__main__":
    unittest.main()
