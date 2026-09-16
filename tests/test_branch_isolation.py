"""GAP 3 — Branch isolation acceptance tests.

Verifies that memory, ledger, and config remain isolated across git branches.
Project-global items (ARCHITECTURAL, CONSTRAINT) remain visible across branches.
Branch-specific items (DECISION, TASK) do not leak across branches.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _make_git_repo(tmp: Path) -> str:
    """Create a minimal git repo with an initial commit. Returns default branch name."""
    subprocess.run(["git", "init"], cwd=str(tmp), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(tmp), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp), capture_output=True)
    (tmp / "README.md").write_text("test", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp), capture_output=True)
    r = subprocess.run(["git", "branch", "--show-current"], cwd=str(tmp),
                       capture_output=True, text=True)
    return r.stdout.strip() or "master"


def _create_branch(tmp: Path, name: str):
    subprocess.run(["git", "checkout", "-b", name], cwd=str(tmp), capture_output=True)


def _switch_branch(tmp: Path, name: str):
    subprocess.run(["git", "checkout", name], cwd=str(tmp), capture_output=True)


class BranchIsolationHarness:
    """Provides isolated memory and ledger instances for testing."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.ctx = tmp / ".context"
        self.ctx.mkdir(exist_ok=True)
        self.db_path = self.ctx / "symbols.db"

    def memory(self):
        from memory import ProjectMemory
        return ProjectMemory(self.db_path)

    def ledger(self):
        from ledger import ContextLedger
        return ContextLedger(self.db_path)

    def branch_scope(self):
        from identity import scope_key_for
        return scope_key_for("BRANCH", self.tmp)

    def project_scope(self):
        from identity import scope_key_for
        return scope_key_for("PROJECT_GLOBAL", self.tmp)


class TestBranchMemoryIsolation:
    """Test 1: branch-specific memory isolation."""

    def test_branch_memory_does_not_cross_branches(self, tmp_path):
        default_branch = _make_git_repo(tmp_path)
        harness = BranchIsolationHarness(tmp_path)

        # On default branch: add a DECISION
        mem = harness.memory()
        main_scope = harness.branch_scope()
        result = mem.add(main_scope, "DECISION", "Use JWT for auth on main",
                         source="test")
        assert result.get("id"), f"add failed: {result}"
        mem.close()

        # Switch to feature-auth
        _create_branch(tmp_path, "feature-auth")
        feat_scope = harness.branch_scope()
        assert feat_scope != main_scope, "branch scopes should differ"

        mem2 = harness.memory()
        # feature-auth should NOT see main's DECISION
        items = mem2.list(feat_scope, memory_type="DECISION")
        contents = [r["content"] for r in items]
        assert "Use JWT for auth on main" not in contents, \
            f"main DECISION leaked to feature-auth: {contents}"

        # Add a different DECISION on feature-auth
        result2 = mem2.add(feat_scope, "DECISION", "Replace JWT with sessions on feature",
                           source="test")
        assert result2.get("id")
        mem2.close()

        # Switch back to default branch
        _switch_branch(tmp_path, default_branch)
        main_scope2 = harness.branch_scope()
        assert main_scope2 == main_scope, f"scope mismatch: {main_scope2} != {main_scope}"

        mem3 = harness.memory()
        items3 = mem3.list(main_scope2, memory_type="DECISION")
        contents3 = [r["content"] for r in items3]
        assert "Use JWT for auth on main" in contents3
        assert "Replace JWT with sessions on feature" not in contents3, \
            f"feature DECISION leaked to main: {contents3}"
        mem3.close()

    def test_project_global_visible_across_branches(self, tmp_path):
        default_branch = _make_git_repo(tmp_path)
        harness = BranchIsolationHarness(tmp_path)

        # On main: add a CONSTRAINT (project-global)
        mem = harness.memory()
        pg_scope = harness.project_scope()
        result = mem.add(pg_scope, "CONSTRAINT", "Must support Python 3.10+",
                         source="test")
        assert result.get("id")
        mem.close()

        # Switch to feature branch
        _create_branch(tmp_path, "feature-x")
        mem2 = harness.memory()
        # Project-global items should be visible from any branch
        items = mem2.list(pg_scope, memory_type="CONSTRAINT")
        contents = [r["content"] for r in items]
        assert "Must support Python 3.10+" in contents, \
            f"project-global CONSTRAINT not visible on feature branch: {contents}"
        mem2.close()


class TestBranchTaskIsolation:
    """Test 2: branch-specific active task isolation."""

    def test_task_memory_scoped_to_branch(self, tmp_path):
        default_branch = _make_git_repo(tmp_path)
        harness = BranchIsolationHarness(tmp_path)

        mem = harness.memory()
        main_scope = harness.branch_scope()
        mem.add(main_scope, "TASK", "Implement JWT auth", source="main-task")
        mem.close()

        _create_branch(tmp_path, "feature-db")
        mem2 = harness.memory()
        feat_scope = harness.branch_scope()
        mem2.add(feat_scope, "TASK", "Add database migrations", source="feat-task")

        # feature-db should only see its own task
        items = mem2.list(feat_scope, memory_type="TASK")
        contents = [r["content"] for r in items]
        assert "Add database migrations" in contents
        assert "Implement JWT auth" not in contents
        mem2.close()

        # main should only see its own task
        _switch_branch(tmp_path, default_branch)
        mem3 = harness.memory()
        items3 = mem3.list(harness.branch_scope(), memory_type="TASK")
        contents3 = [r["content"] for r in items3]
        assert "Implement JWT auth" in contents3
        assert "Add database migrations" not in contents3
        mem3.close()


class TestBranchLedgerIsolation:
    """Test 3: branch-specific Context Ledger behavior."""

    def test_ledger_entries_scoped_to_branch(self, tmp_path):
        default_branch = _make_git_repo(tmp_path)
        harness = BranchIsolationHarness(tmp_path)

        led = harness.ledger()
        main_scope = harness.branch_scope()
        led.record(main_scope, "session-main", "auth/tokens.py", "full",
                   content_hash="abc123", token_cost=100)
        led.close()

        _create_branch(tmp_path, "feature-new")
        led2 = harness.ledger()
        feat_scope = harness.branch_scope()

        # feature branch should not see main's ledger entries
        known = led2.known_files(feat_scope, "session-main")
        paths = [k["path"] for k in known]
        assert "auth/tokens.py" not in paths, \
            f"main ledger leaked to feature branch: {paths}"

        # Record different entry on feature
        led2.record(feat_scope, "session-feat", "db/migrations.py", "full",
                    content_hash="def456", token_cost=80)
        led2.close()

        # Switch back to default branch
        _switch_branch(tmp_path, default_branch)
        led3 = harness.ledger()
        main_scope2 = harness.branch_scope()
        known3 = led3.known_files(main_scope2, "session-main")
        paths3 = [k["path"] for k in known3]
        assert "auth/tokens.py" in paths3
        assert "db/migrations.py" not in paths3, \
            f"feature ledger leaked to main: {paths3}"
        led3.close()


class TestBranchHandoffIsolation:
    """Test 4: branch-specific handoff/resume."""

    def test_handoff_reflects_current_branch(self, tmp_path):
        default_branch = _make_git_repo(tmp_path)
        harness = BranchIsolationHarness(tmp_path)

        # Add memory on default branch
        mem = harness.memory()
        main_scope = harness.branch_scope()
        mem.add(main_scope, "DECISION", "Main branch decision", source="test")
        mem.close()

        # Get handoff on default branch
        from identity import session_handoff
        main_handoff = session_handoff(tmp_path)
        assert main_handoff["identity"]["branch"] == default_branch

        # Switch to feature
        _create_branch(tmp_path, "feature-y")
        mem2 = harness.memory()
        feat_scope = harness.branch_scope()
        mem2.add(feat_scope, "DECISION", "Feature branch decision", source="test")
        mem2.close()

        feat_handoff = session_handoff(tmp_path)
        assert feat_handoff["identity"]["branch"] == "feature-y"

        # Handoffs should have different branch info
        assert main_handoff["identity"]["branch"] != feat_handoff["identity"]["branch"]


class TestGitRevisionInvalidation:
    """Test 5: Git HEAD/revision change detection."""

    def test_revision_changes_between_branches(self, tmp_path):
        default_branch = _make_git_repo(tmp_path)

        from identity import git_info
        main_info = git_info(tmp_path)
        main_rev = main_info.get("head")
        assert main_rev, "should have a git revision on main"

        _create_branch(tmp_path, "feature-z")
        (tmp_path / "new_file.txt").write_text("new", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add new file"],
                       cwd=str(tmp_path), capture_output=True)

        feat_info = git_info(tmp_path)
        feat_rev = feat_info.get("head")
        assert feat_rev != main_rev, "revision should change on feature branch"


class TestProjectGlobalAcrossBranches:
    """Test 6: project-global constraint remains available across branches."""

    def test_constraint_visible_on_all_branches(self, tmp_path):
        default_branch = _make_git_repo(tmp_path)
        harness = BranchIsolationHarness(tmp_path)

        # Add CONSTRAINT on default branch
        mem = harness.memory()
        pg_scope = harness.project_scope()
        mem.add(pg_scope, "CONSTRAINT", "No ORM allowed", source="arch")
        mem.close()

        # Create two feature branches
        for branch in ("feature-a", "feature-b"):
            _create_branch(tmp_path, branch)
            mem2 = harness.memory()
            items = mem2.list(pg_scope, memory_type="CONSTRAINT")
            contents = [r["content"] for r in items]
            assert "No ORM allowed" in contents, \
                f"CONSTRAINT not visible on {branch}: {contents}"
            mem2.close()
            _switch_branch(tmp_path, default_branch)


class TestSupersedeAcrossBranches:
    """Test 7: superseded decision on feature branch does not supersede main."""

    def test_supersede_scoped_to_branch(self, tmp_path):
        default_branch = _make_git_repo(tmp_path)
        harness = BranchIsolationHarness(tmp_path)

        # Add decision on default branch
        mem = harness.memory()
        main_scope = harness.branch_scope()
        r1 = mem.add(main_scope, "DECISION", "Use REST API", source="main")
        main_id = r1["id"]
        mem.close()

        # On feature branch, add a new decision that supersedes a local copy
        _create_branch(tmp_path, "feature-api")
        mem2 = harness.memory()
        feat_scope = harness.branch_scope()
        # First add the old decision on feature (local copy)
        r2 = mem2.add(feat_scope, "DECISION", "Use REST API", source="feature-copy")
        feat_old_id = r2["id"]
        # Now supersede it with a new one
        r3 = mem2.add(feat_scope, "DECISION", "Use GraphQL API", source="feature",
                       supersedes=feat_old_id)
        assert r3.get("superseded", {}).get("ok")
        mem2.close()

        # Default branch's original decision should still be ACTIVE
        _switch_branch(tmp_path, default_branch)
        mem3 = harness.memory()
        main_item = mem3.get(main_id)
        assert main_item["status"] == "ACTIVE", \
            f"main decision was incorrectly superseded: {main_item['status']}"
        mem3.close()
