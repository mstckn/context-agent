#!/usr/bin/env python3
"""
safe_io.py - Small cross-process helpers for Context Agent JSON writes.
"""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


# Lock files older than this are considered abandoned (their owning
# process presumably crashed) and are silently reclaimed.
STALE_LOCK_SECONDS = 30.0


def safe_decode(blob):
    """Decode bytes into text, trying the most common encodings in turn.

    Used to handle subprocess output on Windows where the child Python
    may have printed UTF-8, UTF-8 with BOM, UTF-16-LE, or CP-encodings
    depending on how the parent spawned it. We try them in order of
    plausibility and fall back to ``utf-8`` with ``errors=replace``.
    """
    for enc in ("utf-8-sig", "utf-8", "utf-16-le", "utf-16", "cp1254", "latin-1"):
        try:
            return blob.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return blob.decode("utf-8", errors="replace")


@contextmanager
def file_lock(lock_path, timeout=10.0, poll=0.05):
    """Cross-process advisory lock.

    Uses ``O_CREAT|O_EXCL`` on a sibling file. Tolerates ``PermissionError``
    (Windows file-handle races) and reclaims stale locks older than
    :data:`STALE_LOCK_SECONDS`.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii", errors="replace"))
            break
        except FileExistsError:
            if time.time() - start > timeout:
                # Final attempt: reclaim if stale.
                try:
                    if time.time() - lock_path.stat().st_mtime > STALE_LOCK_SECONDS:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                raise TimeoutError(f"lock timeout: {lock_path}")
            time.sleep(poll)
        except PermissionError:
            # Another process on Windows may be holding the lock; treat
            # the same as FileExistsError and wait.
            if time.time() - start > timeout:
                raise TimeoutError(f"lock timeout (permission): {lock_path}")
            time.sleep(poll)

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except PermissionError:
            # Best effort — the OS will clean up the lock file on
            # process exit.
            pass


def atomic_write_text(path, text, encoding="utf-8"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def atomic_write_json(path, data):
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
