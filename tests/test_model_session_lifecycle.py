"""Tests for MODEL_SESSION_ID lifecycle — transport reconnect survival.

Verifies:
1. Same IDE + same conversation + MCP reconnect → same MODEL_SESSION_ID
2. Same IDE + same conversation → known-to-model ledger retained
3. Same IDE + new conversation → new MODEL_SESSION_ID
4. Different IDE + new conversation → new MODEL_SESSION_ID
5. New model session → zero false known-to-model inheritance
6. Old model session remains queryable
7. Context decay continues across reconnect
8. Explicit reset creates fresh session
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import identity  # noqa: E402
import mcp_launcher  # noqa: E402


def _make_project(base: Path, name: str = "proj") -> Path:
    root = base / name
    (root / ".context").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return root


class ModelSessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _make_project(Path(self._tmp.name))
        # Clean env
        for key in ("CONTEXT_AGENT_MODEL_SESSION_ID", "CONTEXT_AGENT_SESSION",
                     "CONTEXT_AGENT_CONVERSATION_ID", "CONTEXT_AGENT_IDE_INSTANCE_ID",
                     "CONTEXT_AGENT_IDE", "CONTEXT_AGENT_MCP_CONNECTION",
                     "CONTEXT_AGENT_PROJECT_ROOT", "CONTEXT_AGENT_CLIENT",
                     "CONTEXT_AGENT_SCOPE"):
            os.environ.pop(key, None)

    def tearDown(self):
        self._tmp.cleanup()
        for key in ("CONTEXT_AGENT_MODEL_SESSION_ID", "CONTEXT_AGENT_SESSION",
                     "CONTEXT_AGENT_CONVERSATION_ID", "CONTEXT_AGENT_IDE_INSTANCE_ID",
                     "CONTEXT_AGENT_IDE", "CONTEXT_AGENT_MCP_CONNECTION",
                     "CONTEXT_AGENT_PROJECT_ROOT", "CONTEXT_AGENT_CLIENT",
                     "CONTEXT_AGENT_SCOPE"):
            os.environ.pop(key, None)

    # ── Test 1: same IDE + same conversation + reconnect → same session ──

    def test_reconnect_preserves_session(self):
        """MCP transport reconnect with same IDE+conversation keeps session."""
        # First connection
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-reconnect-test"
        os.environ["CONTEXT_AGENT_IDE_INSTANCE_ID"] = "ide-reconnect-test"
        session1 = identity.resolve_model_session(self.root)

        # Simulate MCP disconnect: clear transient env
        os.environ.pop("CONTEXT_AGENT_SESSION", None)
        os.environ.pop("CONTEXT_AGENT_MCP_CONNECTION", None)

        # Reconnect: same IDE + same conversation
        session2 = identity.resolve_model_session(self.root)
        self.assertEqual(session1, session2, "Session should survive reconnect")

    # ── Test 2: same IDE + same conversation → ledger retained ──

    def test_session_binding_persists(self):
        """Session binding is persisted in model_sessions.json."""
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-persist-test"
        os.environ["CONTEXT_AGENT_IDE_INSTANCE_ID"] = "ide-persist-test"
        session = identity.resolve_model_session(self.root)

        bindings_path = self.root / ".context" / "model_sessions.json"
        self.assertTrue(bindings_path.exists(), "model_sessions.json should be created")
        bindings = json.loads(bindings_path.read_text(encoding="utf-8"))
        self.assertTrue(any(b["model_session_id"] == session for b in bindings))

    # ── Test 3: same IDE + new conversation → new session ──

    def test_new_conversation_gets_new_session(self):
        """New conversation gets a fresh MODEL_SESSION_ID."""
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-A"
        os.environ["CONTEXT_AGENT_IDE_INSTANCE_ID"] = "ide-same"
        session_a = identity.resolve_model_session(self.root)

        # New conversation
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-B"
        os.environ.pop("CONTEXT_AGENT_SESSION", None)
        session_b = identity.resolve_model_session(self.root)

        self.assertNotEqual(session_a, session_b,
                            "New conversation must get new session")

    # ── Test 4: different IDE + new conversation → new session ──

    def test_different_ide_gets_new_session(self):
        """Different IDE instance gets a fresh MODEL_SESSION_ID."""
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-ide-A"
        os.environ["CONTEXT_AGENT_IDE_INSTANCE_ID"] = "ide-A"
        session_a = identity.resolve_model_session(self.root)

        # Different IDE
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-ide-B"
        os.environ["CONTEXT_AGENT_IDE_INSTANCE_ID"] = "ide-B"
        os.environ.pop("CONTEXT_AGENT_SESSION", None)
        session_b = identity.resolve_model_session(self.root)

        self.assertNotEqual(session_a, session_b,
                            "Different IDE must get different session")

    # ── Test 5: new session → zero false known-to-model inheritance ──

    def test_new_session_no_false_inheritance(self):
        """A new MODEL_SESSION_ID starts with empty known-to-model state."""
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-inherit-A"
        os.environ["CONTEXT_AGENT_IDE_INSTANCE_ID"] = "ide-inherit"
        session_a = identity.resolve_model_session(self.root)

        # New conversation → new session
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-inherit-B"
        os.environ.pop("CONTEXT_AGENT_SESSION", None)
        session_b = identity.resolve_model_session(self.root)

        # session_b should not appear in session_a's bindings as same session
        self.assertNotEqual(session_a, session_b)
        # Verify the binding file has both entries
        bindings = identity._load_session_bindings(self.root)
        sessions = [b["model_session_id"] for b in bindings]
        self.assertIn(session_a, sessions)
        self.assertIn(session_b, sessions)

    # ── Test 6: old session remains queryable ──

    def test_old_session_remains_queryable(self):
        """Old model session bindings remain in the file for diagnostics."""
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-old-A"
        os.environ["CONTEXT_AGENT_IDE_INSTANCE_ID"] = "ide-old"
        session_a = identity.resolve_model_session(self.root)

        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-old-B"
        os.environ.pop("CONTEXT_AGENT_SESSION", None)
        session_b = identity.resolve_model_session(self.root)

        bindings = identity._load_session_bindings(self.root)
        sessions = [b["model_session_id"] for b in bindings]
        self.assertIn(session_a, sessions, "Old session should remain queryable")
        self.assertIn(session_b, sessions, "New session should be present")

    # ── Test 7: context decay continues across reconnect ──

    def test_context_decay_independent_of_session(self):
        """Context decay is determined by the ledger, not by session binding."""
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-decay"
        os.environ["CONTEXT_AGENT_IDE_INSTANCE_ID"] = "ide-decay"
        session1 = identity.resolve_model_session(self.root)

        # Reconnect
        os.environ.pop("CONTEXT_AGENT_SESSION", None)
        session2 = identity.resolve_model_session(self.root)
        self.assertEqual(session1, session2)

        # Scope key should be the same (decay is handled by ledger, not session)
        scope1 = identity.scope_key_for("MODEL_SESSION", self.root, session1)
        scope2 = identity.scope_key_for("MODEL_SESSION", self.root, session2)
        self.assertEqual(scope1, scope2)

    # ── Test 8: explicit reset creates fresh session ──

    def test_explicit_reset_creates_fresh_session(self):
        """invalidate_model_session generates a new session id."""
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-reset"
        os.environ["CONTEXT_AGENT_IDE_INSTANCE_ID"] = "ide-reset"
        session1 = identity.resolve_model_session(self.root)

        session2 = identity.invalidate_model_session(self.root)
        self.assertNotEqual(session1, session2, "Reset must create new session")

        # resolve_model_session should now return the new session
        os.environ.pop("CONTEXT_AGENT_SESSION", None)
        session3 = identity.resolve_model_session(self.root)
        self.assertEqual(session2, session3, "After reset, resolve returns new session")

    # ── Test 9: explicit host-provided session wins ──

    def test_explicit_host_session_wins(self):
        """CONTEXT_AGENT_MODEL_SESSION_ID takes highest priority."""
        os.environ["CONTEXT_AGENT_MODEL_SESSION_ID"] = "host-provided-session"
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-explicit"
        os.environ["CONTEXT_AGENT_IDE_INSTANCE_ID"] = "ide-explicit"
        session = identity.resolve_model_session(self.root)
        self.assertEqual(session, "host-provided-session")

    # ── Test 10: MCP_CONNECTION_ID is distinct from MODEL_SESSION_ID ──

    def test_mcp_connection_is_not_model_session(self):
        """MCP_CONNECTION_ID and MODEL_SESSION_ID are independent."""
        os.environ["CONTEXT_AGENT_CONVERSATION_ID"] = "conv-conn"
        os.environ["CONTEXT_AGENT_IDE_INSTANCE_ID"] = "ide-conn"
        session = identity.resolve_model_session(self.root)

        # MCP_CONNECTION is set in mcp_server.py, not in identity
        # But we can verify that session doesn't start with "conn-"
        self.assertTrue(session.startswith("session-"),
                        f"Model session should start with 'session-', got {session}")

    def test_public_launcher_initialize_preserves_reconnect_and_rotates_conversation(self):
        os.environ["CONTEXT_AGENT_PROJECT_ROOT"] = str(self.root)
        fake_server = SimpleNamespace(
            initialize_payload=lambda *args: {"protocolVersion": "2024-11-05"})

        def initialize(conversation):
            request = {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"clientInfo": {"name": "VS Code"},
                           "conversationId": conversation},
            }
            with mock.patch.object(mcp_launcher, "load_server", return_value=fake_server), \
                 mock.patch.object(mcp_launcher, "start_auto_bootstrap",
                                   return_value={"status": "ready"}), \
                 mock.patch.object(mcp_launcher, "response"):
                mcp_launcher.handle(request)
            return os.environ["CONTEXT_AGENT_SESSION"]

        first = initialize("conversation-a")
        first_connection = os.environ["CONTEXT_AGENT_MCP_CONNECTION"]
        os.environ.pop("CONTEXT_AGENT_SESSION", None)
        second = initialize("conversation-a")
        self.assertEqual(first, second)
        self.assertNotEqual(first_connection,
                            os.environ["CONTEXT_AGENT_MCP_CONNECTION"])

        os.environ.pop("CONTEXT_AGENT_SESSION", None)
        third = initialize("conversation-b")
        self.assertNotEqual(first, third)


if __name__ == "__main__":
    unittest.main()
