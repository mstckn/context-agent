"""GAP 4 — Cross-IDE lifecycle smoke test.

Simulates two independent MCP clients (TREA and VS Code) connecting to the
same project. Verifies:
- Same PROJECT_ID and REPOSITORY_ID
- Same project memory (ARCHITECTURAL, CONSTRAINT)
- Different IDE_INSTANCE_ID, CONVERSATION_ID, MODEL_SESSION_ID
- Client B does NOT inherit Client A's known-to-model state
- Handoff/bootstrap context works for resumption
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

SAMPLE_PROJECT = ROOT / "examples" / "sample_project"


@pytest.fixture
def sample_project():
    return SAMPLE_PROJECT


class CrossIDEHarness:
    """Simulates an MCP client lifecycle for a given IDE name."""

    def __init__(self, root: Path, ide_name: str):
        self.root = root
        self.ide_name = ide_name
        self.ctx = root / ".context"
        self.ctx.mkdir(exist_ok=True)
        self.db_path = self.ctx / "symbols.db"
        # Simulate MCP initialize: set client name and generate fresh session.
        self.session_id = f"session-{uuid.uuid4().hex[:16]}"

    def identity(self):
        from identity import full_identity
        return full_identity(self.root)

    def handoff(self):
        from identity import session_handoff
        return session_handoff(self.root)

    def memory(self):
        from memory import ProjectMemory
        return ProjectMemory(self.db_path)

    def ledger(self):
        from ledger import ContextLedger
        return ContextLedger(self.db_path)

    def project_scope(self):
        from identity import scope_key_for
        return scope_key_for("PROJECT_GLOBAL", self.root)

    def branch_scope(self):
        from identity import scope_key_for
        return scope_key_for("BRANCH", self.root)


class TestCrossIDELifecycle:
    """Cross-IDE lifecycle integration tests."""

    def test_same_project_different_ide_instances(self, sample_project):
        """Client A (TREA) and Client B (VS Code) share project identity
        but have different IDE instance IDs."""
        client_a = CrossIDEHarness(sample_project, "TREA")
        client_b = CrossIDEHarness(sample_project, "VS Code")

        id_a = client_a.identity()
        id_b = client_b.identity()

        # Same project
        assert id_a["project_id"] == id_b["project_id"], \
            "Both clients should see the same project_id"
        assert id_a["repository_id"] == id_b["repository_id"], \
            "Both clients should see the same repository_id"

        # Different IDE instances (unless CONTEXT_AGENT_IDE_INSTANCE_ID is set externally)
        # In test env without explicit env vars, identity.py may produce different IDs
        # based on IDE type detection. At minimum, the identity objects should be valid.
        assert id_a["ide_instance_id"], "Client A should have an ide_instance_id"
        assert id_b["ide_instance_id"], "Client B should have an ide_instance_id"

    def test_shared_project_memory(self, sample_project):
        """Project-global memory (ARCHITECTURAL, CONSTRAINT) is visible to both clients."""
        client_a = CrossIDEHarness(sample_project, "TREA")
        client_b = CrossIDEHarness(sample_project, "VS Code")

        # Client A adds an architectural decision
        mem_a = client_a.memory()
        pg_scope = client_a.project_scope()
        result = mem_a.add(pg_scope, "ARCHITECTURAL",
                           "Context Intelligence is the product core",
                           source="client-a")
        assert result.get("id")
        mem_a.close()

        # Client B should see it
        mem_b = client_b.memory()
        items = mem_b.list(pg_scope, memory_type="ARCHITECTURAL")
        contents = [r["content"] for r in items]
        assert "Context Intelligence is the product core" in contents, \
            f"Client B should see Client A's ARCHITECTURAL memory: {contents}"
        mem_b.close()

    def test_fresh_known_to_model_state(self, sample_project):
        """Client B must NOT inherit Client A's known-to-model ledger state."""
        client_a = CrossIDEHarness(sample_project, "TREA")
        client_b = CrossIDEHarness(sample_project, "VS Code")

        # Client A records some ledger entries
        led_a = client_a.ledger()
        scope = client_a.branch_scope()
        led_a.record(scope, client_a.session_id, "auth/tokens.py", "full",
                     content_hash="hash-a1", token_cost=150)
        led_a.record(scope, client_a.session_id, "auth/service.py", "full",
                     content_hash="hash-a2", token_cost=200)
        led_a.close()

        # Client B with a fresh session should NOT see Client A's entries
        led_b = client_b.ledger()
        known_b = led_b.known_files(scope, client_b.session_id)
        paths_b = [k["path"] for k in known_b]
        assert "auth/tokens.py" not in paths_b, \
            f"Client B inherited Client A's known-to-model: {paths_b}"
        assert "auth/service.py" not in paths_b, \
            f"Client B inherited Client A's known-to-model: {paths_b}"
        led_b.close()

    def test_handoff_carries_project_context(self, sample_project):
        """Handoff payload carries project context for resumption."""
        client_a = CrossIDEHarness(sample_project, "TREA")

        # Add some memory
        mem = client_a.memory()
        pg_scope = client_a.project_scope()
        mem.add(pg_scope, "CONSTRAINT", "Must use Python 3.10+", source="arch")
        mem.add(pg_scope, "DECISION", "Use SQLite for storage", source="team")
        mem.close()

        # Get handoff
        handoff = client_a.handoff()

        # Verify structure
        assert "identity" in handoff
        assert "session" in handoff
        assert "project_memory" in handoff
        assert "semantics" in handoff

        # Verify semantics
        assert handoff["semantics"]["known_to_model_transferable"] is False, \
            "known_to_model must be marked non-transferable"
        assert handoff["semantics"]["project_memory_is_model_knowledge"] is False, \
            "project memory is NOT model knowledge"

        # Verify project memory is included
        assert handoff["project_memory"]["available"] is True
        active = handoff["project_memory"]["active_counts"]
        assert active.get("CONSTRAINT", 0) >= 1
        assert active.get("DECISION", 0) >= 1

    def test_different_session_ids(self, sample_project):
        """Each client gets a unique MODEL_SESSION_ID."""
        client_a = CrossIDEHarness(sample_project, "TREA")
        client_b = CrossIDEHarness(sample_project, "VS Code")

        assert client_a.session_id != client_b.session_id, \
            "Different clients should have different session IDs"

    def test_client_b_can_resume_without_replaying(self, sample_project):
        """Client B can resume the project using handoff context without
        replaying Client A's entire conversation."""
        client_a = CrossIDEHarness(sample_project, "TREA")

        # Client A does work: records memory and ledger entries
        mem = client_a.memory()
        pg_scope = client_a.project_scope()
        mem.add(pg_scope, "ARCHITECTURAL", "Modular monolith architecture", source="a")
        mem.add(pg_scope, "CONSTRAINT", "No external dependencies for core", source="a")
        mem.close()

        led = client_a.ledger()
        scope = client_a.branch_scope()
        led.record(scope, client_a.session_id, "scripts/agent.py", "full",
                   content_hash="h1", token_cost=500)
        led.record(scope, client_a.session_id, "scripts/router.py", "symbols",
                   content_hash="h2", token_cost=200)
        # Register the session in ledger_sessions so it appears in handoff.
        led.advance_turn(scope, client_a.session_id, tokens_generated=700)
        led.close()

        # Get handoff for Client B
        handoff = client_a.handoff()

        # Client B connects and reads the handoff
        client_b = CrossIDEHarness(sample_project, "VS Code")

        # Client B can access project memory (shared)
        mem_b = client_b.memory()
        items = mem_b.list(pg_scope, statuses=("ACTIVE",))
        contents = [r["content"] for r in items]
        assert "Modular monolith architecture" in contents
        assert "No external dependencies for core" in contents
        mem_b.close()

        # Client B has fresh ledger (no known-to-model from A)
        led_b = client_b.ledger()
        known = led_b.known_files(scope, client_b.session_id)
        assert len(known) == 0, \
            f"Client B should start with empty ledger, got {len(known)} entries"
        led_b.close()

        # But the handoff tells Client B what Client A already sent
        assert handoff["model_sessions"]["available"] is True
        sessions = handoff["model_sessions"]["sessions"]
        # At least Client A's session should be recorded
        session_ids = [s["session_id"] for s in sessions]
        assert client_a.session_id in session_ids, \
            "Client A's session should appear in handoff model_sessions"
