#!/usr/bin/env python3
"""
paths.py — Shared path helpers used by every LocalContextAgent2LLM script.

Centralises the "where is the project root?" logic that used to be duplicated
across mcp_server.py, mcp_launcher.py, watch.py, agent.py, eval.py, etc.

Kept intentionally small and dependency-free (stdlib only) so it can be
imported from any other script without side effects.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

# Marker files that strongly suggest "this directory is a project root".
# If any of these exist in a directory, we treat that directory as the root.
_PROJECT_MARKERS: tuple[str, ...] = (
    ".git",
    "CLAUDE.md",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    ".context",
)

_DESCEND_EXCLUDES: tuple[str, ...] = (
    ".git",
    ".context",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
)


class ProjectRootSelectionRequired(RuntimeError):
    """Raised when a workspace contains several plausible project roots."""

    def __init__(self, start: Path, candidates: list[Path]):
        self.start = start.resolve()
        self.candidates = [candidate.resolve() for candidate in candidates]
        super().__init__(self._message())

    def _message(self) -> str:
        lines = [
            "Birden fazla proje kökü bulundu. Hangisinde çalışıyoruz?",
            "CONTEXT_AGENT_PROJECT_ROOT değerini aşağıdaki adaylardan biri yapın:",
        ]
        for index, candidate in enumerate(self.candidates, 1):
            lines.append(f"{index}. {candidate}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "error": "project_root_selection_required",
            "message": "Birden fazla proje kökü bulundu. Hangisinde çalışıyoruz?",
            "start": str(self.start),
            "candidates": [str(candidate) for candidate in self.candidates],
            "set_env": "CONTEXT_AGENT_PROJECT_ROOT",
        }


def marker_names(path: Path) -> list[str]:
    return [marker for marker in _PROJECT_MARKERS if (path / marker).exists()]


def is_project_root(path: Path) -> bool:
    return bool(marker_names(path))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def discover_project_roots(start: Path | None = None, max_depth: int | None = None) -> list[Path]:
    """Return plausible project roots under ``start``.

    This is intentionally shallow and bounded. It is for IDE workspace folders
    that contain several sibling repositories, not for recursively indexing a
    whole home directory.
    """
    base = (start or Path.cwd()).resolve()
    depth_limit = max_depth if max_depth is not None else _env_int("CONTEXT_AGENT_PROJECT_SCAN_DEPTH", 2)
    max_candidates = _env_int("CONTEXT_AGENT_PROJECT_SCAN_MAX", 25)
    max_dirs = _env_int("CONTEXT_AGENT_PROJECT_SCAN_DIRS_MAX", 300)
    candidates: list[Path] = []
    seen: set[Path] = set()
    scanned = 0

    def visit(path: Path, depth: int) -> None:
        nonlocal scanned
        if scanned >= max_dirs or len(candidates) >= max_candidates:
            return
        try:
            resolved = path.resolve()
        except Exception:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        scanned += 1

        markers = marker_names(resolved)
        if markers and not (depth == 0 and markers == [".context"]):
            candidates.append(resolved)
            return
        if depth >= depth_limit:
            return
        try:
            children = sorted(
                (child for child in resolved.iterdir() if child.is_dir() and child.name not in _DESCEND_EXCLUDES),
                key=lambda child: child.name.lower(),
            )
        except Exception:
            return
        for child in children:
            if child.name.startswith(".") and child.name not in {".config"}:
                continue
            visit(child, depth + 1)

    visit(base, 0)
    return candidates


def _resolve_ambiguous_workspace(start: Path) -> Path | None:
    markers = marker_names(start)
    # A real project root should win over nested package roots. If the only
    # marker is a stray `.context/`, still inspect shallow children because a
    # broad IDE workspace can accidentally acquire `.context/` during probing.
    if markers and markers != [".context"]:
        return start

    candidates = [candidate for candidate in discover_project_roots(start) if candidate != start]
    if not candidates:
        return start if markers else None
    if len(candidates) == 1 and not markers:
        return candidates[0]
    raise ProjectRootSelectionRequired(start, candidates)


def find_project_root(start: Path | None = None) -> Path:
    """Return the project root for the current invocation.

    **Multi-project safe**: Walks up directory tree from ``start`` until finding
    a known project marker (.git, CLAUDE.md, package.json, pyproject.toml, etc).
    Prevents Route tool and other scripts from accidentally using the wrong
    project's `.context/` when multiple projects are open simultaneously.

    Resolution order:

    1. ``CONTEXT_AGENT_PROJECT_ROOT`` environment variable (absolute path).
    2. ``MCP_PROJECT_ROOT`` environment variable (alias, kept for backwards
       compatibility with the very first MCP launcher).
    3. Walk upward from ``start`` (default: ``Path.cwd()``) until a known
       project marker is found.
    4. If the workspace contains several sibling project roots, raise
       ``ProjectRootSelectionRequired`` so the MCP client can ask the user.
    5. If nothing matches, fall back to the original ``start`` / cwd so the
       caller still has a usable absolute path.

    Except for ambiguous multi-root workspaces, even an empty / non-existent
    path is wrapped with :meth:`Path.resolve` to give the caller a canonical
    absolute path.
    """
    env_root = (
        os.environ.get("CONTEXT_AGENT_PROJECT_ROOT")
        or os.environ.get("MCP_PROJECT_ROOT")
    )
    if env_root:
        return Path(env_root).resolve()

    current = (start or Path.cwd()).resolve()
    home = Path.home().resolve()

    current_markers = marker_names(current)
    if current_markers and current_markers != [".context"]:
        return current

    # Walk up at most MAX_DEPTH levels so we never spin out of control on
    # very deep directory trees (e.g. nested mount points or runaway
    # symlink loops). 10 covers the vast majority of real projects
    # (``~/projects/foo/subdir/deeper/even-deeper/``) while bounding the
    # search.
    #
    # Stop *before* crossing the home directory: the home dir itself often
    # holds stray markers (.git, CLAUDE.md, .context, dotfiles), and treating
    # home as a project root pulls in everything under it. This boundary is
    # what keeps Route from escaping to the wrong "project" when several real
    # projects live side by side under home.
    MAX_DEPTH = 10
    if not current_markers:
        for depth, parent in enumerate(current.parents, 1):
            if depth > MAX_DEPTH:
                break
            if parent == home:
                break
            if any((parent / marker).exists() for marker in _PROJECT_MARKERS):
                return parent

    selected = _resolve_ambiguous_workspace(current)
    if selected:
        return selected

    for depth, parent in enumerate([current, *current.parents]):
        if depth > MAX_DEPTH:
            break
        if parent == home:
            break
        if any((parent / marker).exists() for marker in _PROJECT_MARKERS):
            return parent
    return current


def find_context_dir(start: Path | None = None) -> tuple[Path | None, Path | None]:
    """Return ``(.context_dir, project_root)`` or ``(None, None)``.

    Convenience helper for scripts that need both the project root and the
    ``.context/`` subdirectory next to it.
    """
    root = find_project_root(start)
    ctx = root / ".context"
    if ctx.exists() and (ctx / "symbols.db").exists():
        return ctx, root
    return None, root


def normalize_scope(value: str | None, fallback: str = "default") -> str:
    """Return a filesystem-safe logical scope name.

    Scope names are used in filenames and SQLite keys, so keep them short,
    portable, and deterministic. The helper is shared so session, capsule,
    budget, dedup, and usage state do not silently drift apart.
    """
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip().lower()).strip("-")
    return text[:80] or fallback


def project_id_path(root: Path | None = None) -> Path:
    return (root or find_project_root()).resolve() / ".context" / "project_id.json"


def _valid_project_id(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text).strip("-")
    return text[:80]


def read_project_id(root: Path | None = None) -> str:
    """Read the persisted project identity if present."""
    return _valid_project_id(read_project_identity(root).get("project_id"))


def read_project_identity(root: Path | None = None) -> dict:
    path = project_id_path(root)
    if not path.exists():
        return {}
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def ensure_project_id(root: Path | None = None) -> str:
    """Return a path-independent project id, creating it when possible.

    This is the identity anchor for long-lived memory. Path hashes are a useful
    fallback, but they break when the same project is mounted elsewhere. A small
    persisted id in ``.context/project_id.json`` keeps scope stable across IDEs,
    containers, and mount-point changes as long as ``.context`` travels with the
    project.
    """
    env_id = _valid_project_id(os.environ.get("CONTEXT_AGENT_PROJECT_ID"))
    if env_id:
        return env_id

    project = (root or find_project_root()).resolve()
    path = project_id_path(project)
    try:
        try:
            from safe_io import atomic_write_json, file_lock
        except ImportError:
            from scripts.safe_io import atomic_write_json, file_lock
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path.parent / "project_id.lock"):
            # Re-read under the cross-process lock: several IDEs can attach to
            # a fresh project at exactly the same time.
            existing = read_project_id(project)
            if existing:
                record = read_project_identity(project)
                if not _valid_project_id(record.get("scope_name")):
                    record["scope_name"] = normalize_scope(
                        f"{project.name or 'project'}-{existing[:10]}",
                        fallback=f"project-{existing[:10]}",
                    )
                    atomic_write_json(path, record)
                return existing

            new_id = uuid.uuid4().hex
            scope_name = normalize_scope(
                f"{project.name or 'project'}-{new_id[:10]}",
                fallback=f"project-{new_id[:10]}")
            atomic_write_json(path, {
                "project_id": new_id,
                "scope_name": scope_name,
                "root_hint": str(project),
                "version": 1,
            })
            return new_id
    except Exception:
        existing = read_project_id(project)
        if existing:
            return existing
        return hashlib.sha1(str(project).encode("utf-8", errors="replace")).hexdigest()


def project_scope_key(root: Path | None = None) -> str:
    """Stable default scope for a project.

    Older launchers defaulted to ``<client>-<pid>``, which split memory every
    time an IDE restarted or a different MCP client connected. The default
    should instead follow the project, while still allowing explicit
    ``CONTEXT_AGENT_SCOPE`` overrides for isolated experiments.
    """
    project = (root or find_project_root()).resolve()
    project_id = ensure_project_id(project)
    record = read_project_identity(project)
    scope_name = _valid_project_id(record.get("scope_name"))
    if scope_name:
        return scope_name
    digest = project_id[:10] or hashlib.sha1(str(project).encode("utf-8", errors="replace")).hexdigest()[:10]
    return normalize_scope(f"project-{digest}", fallback=f"project-{digest}")


def resolve_scope(value: str | None = None, root: Path | None = None) -> str:
    """Resolve the active logical scope.

    Explicit env/config wins. Without it, use a project-stable key so context
    memory survives IDE, model, and MCP process changes.
    """
    raw = value if value is not None else os.environ.get("CONTEXT_AGENT_SCOPE")
    if raw:
        return normalize_scope(raw, fallback=project_scope_key(root))
    return project_scope_key(root)


def resolve_runtime_scope(value: str | None = None,
                          root: Path | None = None) -> str:
    """Return the isolation key for mutable per-model working state.

    Project memory/config intentionally use :func:`resolve_scope` so multiple
    IDEs can share durable project knowledge. Budgets, dedup state, capsules
    and active work sessions must be narrower: two simultaneous model
    conversations in the same checkout must never overwrite one another.

    ``CONTEXT_AGENT_RUNTIME_SCOPE`` is an explicit escape hatch used by the
    dashboard preview. Otherwise the stable project scope is composed with
    MODEL_SESSION_ID (preferred), the resolved session, or finally the MCP
    transport connection. Direct CLI use without any lifecycle identity keeps
    the historical project scope.
    """
    explicit = (
        value if value is not None
        else os.environ.get("CONTEXT_AGENT_RUNTIME_SCOPE")
    )
    base = resolve_scope(root=root)
    if explicit:
        return normalize_scope(explicit, fallback=base)
    session = (
        os.environ.get("CONTEXT_AGENT_MODEL_SESSION_ID", "").strip()
        or os.environ.get("CONTEXT_AGENT_SESSION", "").strip()
    )
    if session:
        return normalize_scope(f"{base}-session-{session}", fallback=base)
    connection = os.environ.get("CONTEXT_AGENT_MCP_CONNECTION", "").strip()
    if connection:
        return normalize_scope(f"{base}-connection-{connection}", fallback=base)
    return base
