#!/usr/bin/env python3
"""
identity.py — Cross-IDE identity & continuity (autoroute spec §28-34).

Layered identity model:

  PROJECT_ID        stable across paths/moves/clones (`.context/project_id.json`)
  REPOSITORY_ID     stable across checkouts of the same repo (git remote based)
  CHECKOUT_ID       unique per working copy / worktree / clone directory
  BRANCH/REVISION   live git state (git_meta)
  IDE_INSTANCE_ID   per IDE installation/machine (registry in identity.json)
  CONVERSATION_ID   per chat/thread; bridges to session.py current session
  MODEL_SESSION_ID  per model session (ledger session key; see ledger.py)
  TASK_ID           per task invocation (env or freshly generated)

Rules implemented here:

* Path is never the sole identity. Project identity travels with `.context/`.
* Project Memory != Model Knowledge: the handoff never claims ledger
  `known_to_model` state for a different model session.
* Writes to identity.json use atomic replace + optimistic version check
  (no silent overwrites between concurrent IDEs).
* Read-only everywhere: this module never mutates ledger/memory/session state.

Usage:
  python identity.py --show
  python identity.py --continuity
  python identity.py --handoff [--session NAME]
  python identity.py --scope-for BRANCH
  python identity.py --new-conversation
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

try:
    from safe_io import file_lock
except ImportError:  # pragma: no cover
    from scripts.safe_io import file_lock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from paths import (
        ensure_project_id,
        find_project_root,
        normalize_scope,
        project_scope_key,
        read_project_identity,
    )
    from git_meta import git_info
except ImportError:  # pragma: no cover - repo layout fallback
    from scripts.paths import (
        ensure_project_id,
        find_project_root,
        normalize_scope,
        project_scope_key,
        read_project_identity,
    )
    from scripts.git_meta import git_info

SCOPE_LEVELS = (
    "PROJECT_GLOBAL",
    "REPOSITORY",
    "CHECKOUT",
    "BRANCH",
    "TASK",
    "CONVERSATION",
    "MODEL_SESSION",
)


# ── storage ────────────────────────────────────────────────────────────


def identity_path(root: Path) -> Path:
    return Path(root) / ".context" / "identity.json"


def load_identity(root: Path) -> dict:
    path = identity_path(root)
    if not path.exists():
        return {"version": 0}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
        if isinstance(data, dict):
            data.setdefault("version", 1)
            return data
    except Exception:
        pass
    return {"version": 0}


def _write_identity(root: Path, data: dict, expected_version: int) -> bool:
    """Atomic write with optimistic concurrency check.

    Returns False when another writer changed the file meanwhile; callers
    retry with fresh state instead of silently overwriting.
    """
    path = identity_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_identity(root)
    if int(current.get("version", 0)) != int(expected_version):
        return False
    data["version"] = int(expected_version) + 1
    data["updated_at"] = datetime.now().isoformat()
    tmp = path.with_suffix(".tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return True


def _update_identity(root: Path, patch: dict, retries: int = 3) -> dict:
    root = Path(root)
    # The previous optimistic check still had a read/check/write race between
    # independent IDE processes. Serialize the merge so two simultaneous IDE
    # registrations cannot drop one another's identity fields.
    with file_lock(root / ".context" / "identity.lock"):
        for _ in range(max(1, retries)):
            data = load_identity(root)
            version = int(data.get("version", 0))
            merged = {**data, **patch}
            if isinstance(patch.get("ide_instances"), dict):
                merged["ide_instances"] = {
                    **dict(data.get("ide_instances", {})),
                    **patch["ide_instances"],
                }
            if _write_identity(root, merged, version):
                return merged
        return load_identity(root)


# ── identity elements ──────────────────────────────────────────────────


def _normalize_remote(url: str) -> str:
    text = str(url or "").strip().lower()
    for prefix in ("https://", "http://", "ssh://", "git://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if "@" in text.split("/")[0]:
        text = text.split("@", 1)[1]
    text = text.replace(":", "/", 1) if "/" not in text.split(":")[0] else text
    if text.endswith(".git"):
        text = text[:-4]
    return text.strip("/")


def repository_id(root: Path) -> str:
    """Repo-level identity stable across checkouts/clones/paths.

    Derived from the git remote when present; otherwise falls back to the
    persisted PROJECT_ID (documented fallback: non-git projects have no
    stronger repository anchor).
    """
    root = Path(root)
    identity = load_identity(root)
    cached = str(identity.get("repository_id", "")).strip()
    remote = ""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(root), capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            remote = result.stdout.strip()
    except Exception:
        remote = ""
    if remote:
        derived = "repo-" + hashlib.sha1(_normalize_remote(remote).encode("utf-8")).hexdigest()[:20]
        if cached != derived:
            _update_identity(root, {"repository_id": derived, "repository_source": "git-remote"})
        return derived
    if cached:
        return cached
    fallback = "repo-" + ensure_project_id(root)[:20]
    _update_identity(root, {"repository_id": fallback, "repository_source": "project-id-fallback"})
    return fallback


def checkout_id(root: Path) -> str:
    """Unique per working copy. Two clones of the same repo -> same
    repository_id but different checkout_id."""
    root = Path(root)
    identity = load_identity(root)
    cached = str(identity.get("checkout_id", "")).strip()
    if cached:
        return cached
    new_id = "chk-" + uuid.uuid4().hex[:20]
    _update_identity(root, {"checkout_id": new_id, "checkout_root_hint": str(root)})
    return new_id


def _ide_key() -> str:
    explicit = os.environ.get("CONTEXT_AGENT_IDE", "").strip().lower()
    ide_type = explicit or "unknown"
    if not explicit:
        try:
            from ide_detector import detect_ide

            detected = detect_ide()
            if getattr(detected, "ide_type", None) is not None:
                value = getattr(detected.ide_type, "value", "")
                if value and value != "unknown":
                    ide_type = value
        except Exception:
            pass
    return f"{ide_type}@{platform.node().lower() or 'host'}"


def ide_instance_id(root: Path) -> str:
    """Stable per IDE installation on this machine.

    Explicit CONTEXT_AGENT_IDE_INSTANCE_ID always wins (lets callers pin an
    instance). Otherwise one instance id is registered per (ide, host) pair
    so two different IDEs on the same machine stay distinct, while restarts
    of the same IDE keep the same identity.
    """
    explicit = os.environ.get("CONTEXT_AGENT_IDE_INSTANCE_ID", "").strip()
    if explicit:
        return normalize_scope(explicit, fallback="ide-instance")
    root = Path(root)
    key = _ide_key()
    identity = load_identity(root)
    registry = dict(identity.get("ide_instances", {}))
    entry = registry.get(key)
    if not isinstance(entry, dict) or not entry.get("instance_id"):
        registry[key] = {
            "instance_id": "ide-" + uuid.uuid4().hex[:16],
            "first_seen": datetime.now().isoformat(),
            "last_seen": datetime.now().isoformat(),
        }
        _update_identity(root, {"ide_instances": registry})
        return registry[key]["instance_id"]
    if entry.get("last_seen", "")[:10] != datetime.now().isoformat()[:10]:
        entry["last_seen"] = datetime.now().isoformat()
        registry[key] = entry
        _update_identity(root, {"ide_instances": registry})
    return entry["instance_id"]


def conversation_id(root: Path) -> str:
    """Conversation identity. Env wins; else bridge to the active session.py
    session (same conversation as long as the same session is current); else
    a persisted id in identity.json."""
    explicit = os.environ.get("CONTEXT_AGENT_CONVERSATION_ID", "").strip()
    if explicit:
        return normalize_scope(explicit, fallback="conversation")
    root = Path(root)
    try:
        from session import load_current

        current = load_current()
        started = (current or {}).get("started_at", "")
        if started:
            return "conv-" + hashlib.sha1(started.encode("utf-8")).hexdigest()[:16]
    except Exception:
        pass
    identity = load_identity(root)
    cached = str(identity.get("conversation_id", "")).strip()
    if cached:
        return cached
    new_id = "conv-" + uuid.uuid4().hex[:16]
    _update_identity(root, {"conversation_id": new_id})
    return new_id


def new_conversation(root: Path) -> str:
    new_id = "conv-" + uuid.uuid4().hex[:16]
    _update_identity(root, {"conversation_id": new_id})
    return new_id


def task_id() -> str:
    explicit = os.environ.get("CONTEXT_AGENT_TASK_ID", "").strip()
    if explicit:
        return normalize_scope(explicit, fallback="task")
    return "task-" + uuid.uuid4().hex[:12]


def full_identity(root: Path | None = None) -> dict:
    root = Path(root or find_project_root())
    git = git_info(root)
    return {
        "project_id": ensure_project_id(root),
        "scope": project_scope_key(root),
        "repository_id": repository_id(root),
        "checkout_id": checkout_id(root),
        "branch": git.get("branch") if git.get("available") else None,
        "revision": git.get("head") if git.get("available") else None,
        "git_available": bool(git.get("available")),
        "ide_instance_id": ide_instance_id(root),
        "conversation_id": conversation_id(root),
        "task_id": task_id(),
    }


# ── scope mapping (§31) ────────────────────────────────────────────────


def scope_key_for(level: str, root: Path | None = None, model_session: str = "") -> str:
    """Map an isolation level to a concrete scope key.

    PROJECT_GLOBAL is the project-stable default scope (memory, config).
    Narrower levels compose with it so branch/task state never leaks upward.
    """
    level = str(level or "").strip().upper()
    if level not in SCOPE_LEVELS:
        raise ValueError(f"unknown scope level: {level!r}; expected one of {SCOPE_LEVELS}")
    root = Path(root or find_project_root())
    base = project_scope_key(root)
    if level == "PROJECT_GLOBAL":
        return base
    if level == "REPOSITORY":
        return normalize_scope(f"{base}-{repository_id(root)[:24]}")
    if level == "CHECKOUT":
        return normalize_scope(f"{base}-{checkout_id(root)[:24]}")
    if level == "BRANCH":
        git = git_info(root)
        branch = git.get("branch") if git.get("available") else None
        if not branch:
            return normalize_scope(f"{base}-nobranch")
        return normalize_scope(f"{base}-branch-{branch}")
    if level == "TASK":
        return normalize_scope(f"{base}-{task_id()}")
    if level == "CONVERSATION":
        return normalize_scope(f"{base}-{conversation_id(root)}")
    if level == "MODEL_SESSION":
        session = normalize_scope(model_session or os.environ.get("CONTEXT_AGENT_SESSION", "") or "default")
        return normalize_scope(f"{base}-session-{session}")


# ── read-only digests ──────────────────────────────────────────────────


def _ledger_digest(root: Path, scope: str) -> dict:
    """Read-only summary of model sessions. Never claims another session's
    known-to-model state is transferable."""
    db_path = Path(root) / ".context" / "symbols.db"
    if not db_path.exists():
        return {"available": False}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            # Include sessions from project-global AND branch-scoped entries.
            base = project_scope_key(root)
            rows = conn.execute(
                "SELECT session_id, turn, total_tokens, updated_at "
                "FROM ledger_sessions WHERE scope=? OR scope LIKE ? "
                "ORDER BY updated_at DESC LIMIT 10",
                (scope, f"{base}-%"),
            ).fetchall()
            sessions = [
                {"session_id": r[0], "turn": r[1], "total_tokens": r[2], "updated_at": r[3]}
                for r in rows
            ]
            return {"available": True, "sessions": sessions}
        finally:
            conn.close()
    except Exception:
        return {"available": False}


def _session_digest(root: Path) -> dict:
    try:
        from session import load_current

        current = load_current()
        if not current:
            return {"active": False}
        return {
            "active": True,
            "name": current.get("name"),
            "started_at": current.get("started_at"),
            "decisions": [d.get("decision") for d in (current.get("decisions") or [])[-5:]],
            "notes": [n.get("text") for n in (current.get("notes") or [])[-5:]],
            "files_touched": [f.get("file") for f in (current.get("files_touched") or [])[-8:]],
        }
    except Exception:
        return {"active": False}


def _memory_digest(root: Path, scope: str) -> dict:
    db_path = Path(root) / ".context" / "symbols.db"
    if not db_path.exists():
        return {"available": False}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='project_memory'"
            ).fetchone()
            if not exists:
                return {"available": False}
            rows = conn.execute(
                "SELECT memory_type, COUNT(*) FROM project_memory "
                "WHERE scope=? AND status='ACTIVE' GROUP BY memory_type",
                (scope,),
            ).fetchall()
            return {"available": True, "active_counts": {r[0]: r[1] for r in rows}}
        finally:
            conn.close()
    except Exception:
        return {"available": False}


def session_handoff(root: Path | None = None, model_session: str = "") -> dict:
    """SessionHandoff (§29): everything another IDE/session needs to continue
    WITHOUT re-deriving project knowledge. Explicitly excludes any claim that
    model-session knowledge transfers."""
    root = Path(root or find_project_root())
    scope = project_scope_key(root)
    return {
        "handoff_version": 1,
        "generated_at": datetime.now().isoformat(),
        "identity": full_identity(root),
        "session": _session_digest(root),
        "model_sessions": _ledger_digest(root, scope),
        "project_memory": _memory_digest(root, scope),
        "semantics": {
            "project_memory_is_model_knowledge": False,
            "known_to_model_transferable": False,
            "note": (
                "Project memory travels with the project. known_to_model state "
                "belongs to one model session only; a new session starts with an "
                "empty ledger and must not be told it already saw anything."
            ),
        },
    }


# ── continuity report (§30) ────────────────────────────────────────────


def cross_ide_continuity(root: Path | None = None) -> dict:
    """Report CROSS_IDE_CONTINUITY as VERIFIED / PARTIAL / MISSING with proof."""
    root = Path(root or find_project_root())
    checks: dict[str, dict] = {}

    project_record = read_project_identity(root)
    project_ok = bool(project_record.get("project_id"))
    checks["project_id"] = {
        "status": "present" if project_ok else "missing",
        "source": ".context/project_id.json",
        "value": project_record.get("project_id"),
    }

    identity = load_identity(root)
    repo_ok = bool(identity.get("repository_id")) or project_ok  # derivable on demand
    checks["repository_id"] = {
        "status": "present" if repo_ok else "missing",
        "source": identity.get("repository_source", "derivable"),
        "value": identity.get("repository_id"),
    }
    checkout_ok = bool(identity.get("checkout_id"))
    checks["checkout_id"] = {
        "status": "present" if checkout_ok else "derivable",
        "source": "identity.json",
        "value": identity.get("checkout_id"),
    }

    git = git_info(root)
    checks["branch"] = {
        "status": "present" if git.get("available") else "not_applicable",
        "source": "git",
        "value": git.get("branch"),
    }
    checks["ide_instance"] = {
        "status": "present" if identity.get("ide_instances") else "derivable",
        "source": "identity.json registry",
        "value": sorted(identity.get("ide_instances", {}).keys()),
    }

    sessions_dir = root / ".context" / "sessions" / project_scope_key(root)
    sessions_ok = sessions_dir.exists()
    checks["session_store"] = {
        "status": "present" if sessions_ok else "empty",
        "source": str(sessions_dir.relative_to(root)),
        "value": None,
    }

    scope = project_scope_key(root)
    scope_ok = scope and scope != "default"
    checks["scope_stability"] = {
        "status": "present" if scope_ok else "missing",
        "source": "project_id-derived scope",
        "value": scope,
    }

    required = ("project_id", "repository_id", "scope_stability")
    present_required = all(checks[key]["status"] == "present" for key in required)
    missing_required = [key for key in required if checks[key]["status"] != "present"]
    optional_missing = [
        key for key, value in checks.items()
        if key not in required and value["status"] in ("missing",)
    ]

    if present_required and not optional_missing:
        status = "VERIFIED"
    elif present_required:
        status = "PARTIAL"
    elif missing_required and len(missing_required) < len(required):
        status = "PARTIAL"
    else:
        status = "MISSING"

    return {
        "cross_ide_continuity": status,
        "checks": checks,
        "missing_required": missing_required,
        "advice": (
            "Identity anchors are persisted under .context/ and travel with the "
            "project. Open the same project folder from another IDE: project "
            "memory and configuration follow the project_id, not the path."
            if status != "MISSING"
            else "Run any Context Agent command in this project to create identity anchors."
        ),
    }


# ── model session binding (transport reconnect survival) ───────────────


def _session_bindings_path(root: Path) -> Path:
    return Path(root) / ".context" / "model_sessions.json"


def _load_session_bindings(root: Path) -> list:
    path = _session_bindings_path(root)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_session_bindings(root: Path, bindings: list) -> None:
    path = _session_bindings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = json.dumps(bindings, indent=2, ensure_ascii=False)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _session_bindings_lock_path(root: Path) -> Path:
    return Path(root) / ".context" / "model_sessions.lock"


def _upsert_session_binding(bindings: list, project_id: str, ide_id: str,
                            conv_id: str, session_id: str) -> list:
    now = datetime.now().isoformat()
    for entry in bindings:
        if (entry.get("project_id") == project_id
                and entry.get("ide_instance_id") == ide_id
                and entry.get("conversation_id") == conv_id):
            entry["model_session_id"] = session_id
            entry["last_seen_at"] = now
            return bindings
    bindings.append({
        "project_id": project_id,
        "ide_instance_id": ide_id,
        "conversation_id": conv_id,
        "model_session_id": session_id,
        "created_at": now,
        "last_seen_at": now,
    })
    return bindings[-50:]


def resolve_model_session(root: Path | None = None) -> str:
    """Resolve MODEL_SESSION_ID with transport-reconnect awareness.

    Priority:
    1. Explicit CONTEXT_AGENT_MODEL_SESSION_ID env var (host-provided).
    2. Existing persisted binding for (project_id, ide_instance_id, conversation_id).
    3. Generate a new session id.
    """
    root = Path(root or find_project_root())

    # 1. Explicit host-provided
    explicit = os.environ.get("CONTEXT_AGENT_MODEL_SESSION_ID", "").strip()
    if explicit:
        os.environ["CONTEXT_AGENT_SESSION"] = explicit
        _record_binding(root, explicit)
        return explicit

    # 2. Look up existing binding
    project_id = ensure_project_id(root)
    ide_id = ide_instance_id(root)
    conv_id = conversation_id(root)
    with file_lock(_session_bindings_lock_path(root)):
        bindings = _load_session_bindings(root)
        for entry in bindings:
            if (entry.get("project_id") == project_id
                    and entry.get("ide_instance_id") == ide_id
                    and entry.get("conversation_id") == conv_id):
                session_id = entry["model_session_id"]
                _upsert_session_binding(
                    bindings, project_id, ide_id, conv_id, session_id)
                _save_session_bindings(root, bindings)
                os.environ["CONTEXT_AGENT_SESSION"] = session_id
                return session_id

        # 3. Generate new while holding the cross-process binding lock.
        new_id = f"session-{uuid.uuid4().hex[:16]}"
        bindings = _upsert_session_binding(
            bindings, project_id, ide_id, conv_id, new_id)
        _save_session_bindings(root, bindings)
        os.environ["CONTEXT_AGENT_SESSION"] = new_id
        return new_id


def _record_binding(root: Path, session_id: str, bindings: list | None = None) -> None:
    """Persist a ModelSessionBinding entry."""
    project_id = ensure_project_id(root)
    ide_id = ide_instance_id(root)
    conv_id = conversation_id(root)
    with file_lock(_session_bindings_lock_path(root)):
        current = _load_session_bindings(root)
        current = _upsert_session_binding(
            current, project_id, ide_id, conv_id, session_id)
        _save_session_bindings(root, current)


def invalidate_model_session(root: Path | None = None) -> str:
    """Explicit reset: generate a fresh MODEL_SESSION_ID and record it.

    Called when the host signals the actual LLM context was reset.
    """
    root = Path(root or find_project_root())
    new_id = f"session-{uuid.uuid4().hex[:16]}"
    os.environ["CONTEXT_AGENT_SESSION"] = new_id
    _record_binding(root, new_id)
    return new_id


# ── CLI ────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Cross-IDE identity & continuity")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--show", action="store_true", help="Full identity block")
    group.add_argument("--continuity", action="store_true", help="CROSS_IDE_CONTINUITY report")
    group.add_argument("--handoff", action="store_true", help="SessionHandoff payload")
    group.add_argument("--scope-for", metavar="LEVEL", help=f"One of {SCOPE_LEVELS}")
    group.add_argument("--new-conversation", action="store_true")
    parser.add_argument("--session", default="", help="Model session id (handoff/scope)")
    args = parser.parse_args()

    root = find_project_root()
    if args.show:
        print(json.dumps(full_identity(root), indent=2, ensure_ascii=False))
    elif args.continuity:
        print(json.dumps(cross_ide_continuity(root), indent=2, ensure_ascii=False))
    elif args.handoff:
        print(json.dumps(session_handoff(root, args.session), indent=2, ensure_ascii=False))
    elif args.scope_for:
        print(json.dumps({"level": args.scope_for.upper(),
                          "scope": scope_key_for(args.scope_for, root, args.session)}, indent=2))
    elif args.new_conversation:
        print(json.dumps({"conversation_id": new_conversation(root)}, indent=2))


if __name__ == "__main__":
    main()
