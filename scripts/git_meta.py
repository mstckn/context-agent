#!/usr/bin/env python3
"""Small git metadata helpers for Context Agent memory records."""

from __future__ import annotations

import subprocess
import os
import threading
import time
from pathlib import Path


_GIT_INFO_CACHE = {}
_GIT_INFO_CACHE_LOCK = threading.Lock()


def _git_dir(root: Path) -> Path | None:
    marker = Path(root) / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        try:
            line = marker.read_text(encoding="utf-8", errors="replace").strip()
            if line.lower().startswith("gitdir:"):
                target = line.split(":", 1)[1].strip()
                return (marker.parent / target).resolve()
        except OSError:
            return None
    return None


def _git_signature(root: Path):
    """Cheap invalidation signature for branch/commit/index transitions."""
    git_dir = _git_dir(root)
    if not git_dir:
        return None
    head_path = git_dir / "HEAD"
    try:
        head_text = head_path.read_text(
            encoding="utf-8", errors="replace").strip()
        head_mtime = head_path.stat().st_mtime_ns
    except OSError:
        head_text, head_mtime = "", 0
    ref_mtime = 0
    if head_text.startswith("ref:"):
        ref_path = git_dir / head_text.split(":", 1)[1].strip()
        try:
            ref_mtime = ref_path.stat().st_mtime_ns
        except OSError:
            # Packed refs or unborn branch; HEAD text still distinguishes it.
            ref_mtime = 0
    try:
        index_mtime = (git_dir / "index").stat().st_mtime_ns
    except OSError:
        index_mtime = 0
    return (head_text, head_mtime, ref_mtime, index_mtime)


def clear_git_info_cache(root: Path | None = None) -> None:
    """Clear the short-lived metadata cache (mainly for tests/explicit refresh)."""
    with _GIT_INFO_CACHE_LOCK:
        if root is None:
            _GIT_INFO_CACHE.clear()
        else:
            _GIT_INFO_CACHE.pop(str(Path(root).resolve()), None)


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def git_info(root: Path, refresh: bool = False) -> dict:
    """Return compact git state for memory provenance.

    The data is intentionally tiny. It lets future context packages warn when a
    decision came from another branch without spending meaningful tokens.
    """
    root = Path(root).resolve()
    cache_key = str(root)
    signature = _git_signature(root)
    try:
        ttl = max(0.0, float(os.environ.get(
            "CONTEXT_AGENT_GIT_CACHE_TTL", "2.0")))
    except ValueError:
        ttl = 2.0
    if not refresh and ttl > 0:
        with _GIT_INFO_CACHE_LOCK:
            cached = _GIT_INFO_CACHE.get(cache_key)
            if (cached and time.monotonic() - cached[0] <= ttl
                    and cached[1] == signature):
                return dict(cached[2])
    # Avoid spawning git merely to rediscover that a plain directory is not a
    # repository. This is a hot path for capsule/memory metadata.
    if not (root / ".git").exists():
        result = {"available": False}
        with _GIT_INFO_CACHE_LOCK:
            _GIT_INFO_CACHE[cache_key] = (
                time.monotonic(), signature, result)
        return dict(result)
    inside = _git(root, "rev-parse", "--is-inside-work-tree") == "true"
    if not inside:
        result = {"available": False}
        with _GIT_INFO_CACHE_LOCK:
            _GIT_INFO_CACHE[cache_key] = (
                time.monotonic(), signature, result)
        return dict(result)

    branch = _git(root, "branch", "--show-current") or _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    head = _git(root, "rev-parse", "--short=12", "HEAD")
    status = _git(root, "status", "--porcelain")
    result = {
        "available": True,
        "branch": branch,
        "head": head,
        "dirty": bool(status),
    }
    with _GIT_INFO_CACHE_LOCK:
        _GIT_INFO_CACHE[cache_key] = (
            time.monotonic(), _git_signature(root), result)
    return dict(result)


def same_ref(left: dict | None, right: dict | None) -> bool:
    left = left or {}
    right = right or {}
    if not left.get("available") or not right.get("available"):
        return True
    return left.get("branch") == right.get("branch") and left.get("head") == right.get("head")


def changed_files(root: Path, limit: int = 12) -> list[dict]:
    """Dirty/changed dosyalar (git status --porcelain).

    Aktif kodlama görevlerinde diff, eski repo-arası aramadan daha değerli
    bir sinyaldir (spec PHASE 13). Dönüş: [{"path","status"}...].
    """
    root = Path(root)
    raw = _git(root, "status", "--porcelain")
    if not raw:
        return []
    out = []
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        code = line[:2].strip() or "?"
        path = line[3:].strip().strip('"')
        # rename: "orig -> new" → yeni yol
        if " -> " in path:
            path = path.split(" -> ")[-1]
        out.append({"path": path, "status": code})
        if len(out) >= limit:
            break
    return out


def recent_files(root: Path, commits: int = 5, limit: int = 12) -> list[str]:
    """Son N commit'te değişmiş dosyalar (yeni → eski sırasında, tekil)."""
    root = Path(root)
    raw = _git(root, "log", f"-n{int(commits)}", "--name-only", "--pretty=format:")
    if not raw:
        return []
    seen, out = set(), []
    for line in raw.splitlines():
        path = line.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
        if len(out) >= limit:
            break
    return out
