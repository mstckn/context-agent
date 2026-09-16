#!/usr/bin/env python3
"""Regressions for context delivered through the public MCP compact path."""

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import index as index_mod  # noqa: E402
import mcp_server  # noqa: E402
import route  # noqa: E402


def test_compact_mcp_never_drops_items_used_by_sufficiency():
    items = [
        {
            "file": f"pkg/file_{i}.py", "content": f"value_{i} = {i}",
            "priority": "primary" if i < 3 else "secondary",
            "action": "load_file", "token_estimate": 4,
        }
        for i in range(12)
    ]
    data = {
        "task": "cross-file contract change",
        "sections": {
            "routing": {"analysis": {"keywords": ["contract"]}},
            "context_items": items,
            "context_sufficiency": {"sufficient": True},
        },
    }

    compact = mcp_server.compact_context_package(data)

    assert [item["file"] for item in compact["context_items"]] == [
        item["file"] for item in items
    ]
    assert compact["response_meta"]["truncated_items"] == 0
    assert compact["sufficiency"]["sufficient"] is True


def test_generic_layer_words_do_not_rank_every_layer_file():
    info = route.classify_task(
        "Add account_id to the user model, frontend consumer and tests"
    )
    assert "account_id" in info["keywords"]
    assert "user" in info["keywords"]
    for generic in ("model", "frontend", "consumer", "tests"):
        assert generic not in info["keywords"]
    # Domain/evidence classification remains available for expansion.
    assert "user" in info["domains"]
    assert "test" in info["domains"]


def test_graph_neighbors_prefer_real_import_dependencies():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE files (
            path TEXT PRIMARY KEY, summary TEXT, line_count INTEGER,
            token_estimate INTEGER
        );
        CREATE TABLE imports (file TEXT, imports_from TEXT);
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, src_file TEXT, kind TEXT, dst_file TEXT
        );
        """
    )
    files = [
        "jobs/queue.py", "db/connection.py", "analytics/events.py",
        "utils/logging.py", "tests/test_jobs.py",
    ]
    conn.executemany(
        "INSERT INTO files VALUES (?, '', 10, 20)", [(path,) for path in files]
    )
    conn.executemany(
        "INSERT INTO edges (src_file, kind, dst_file) VALUES (?, 'imports', ?)",
        [
            ("jobs/queue.py", "db/connection.py"),
            ("jobs/queue.py", "analytics/events.py"),
            ("jobs/queue.py", "utils/logging.py"),
            ("tests/test_jobs.py", "jobs/queue.py"),
        ],
    )

    neighbors = route.find_neighbor_files(conn, "jobs/queue.py")

    assert [item["file"] for item in neighbors] == [
        "db/connection.py", "analytics/events.py", "utils/logging.py",
        "tests/test_jobs.py",
    ]


def test_python_import_extraction_is_line_bounded_and_complete():
    source = """\
import threading
import time
from db.connection import transaction
from analytics.events import track_event
"""
    assert index_mod.extract_imports(source, "python") == [
        "threading", "time", "db.connection", "analytics.events"
    ]
