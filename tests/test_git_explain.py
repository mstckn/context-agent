#!/usr/bin/env python3
"""test_git_explain.py — Git-aware retrieval + explainability + büyük dosya
haritası testleri (promt.txt PHASE 13 / PHASE 16 + büyük dosya düzeltmesi).

Kabul kriterleri:
  * Dirty dosyalar retrieval listesine boost edilir (en fazla 3, gürültü elenir).
  * porcelain/log parse'ı doğru çalışır (rename dahil).
  * Büyük dosya preview'unda chunk_map üretilir (sembol DB varsa sembol haritası,
    yoksa kaba satır haritası).
  * MCP compact katmanı chunk_map'i düşürmez.
"""

import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import agent  # noqa: E402
import get_range as get_range_mod  # noqa: E402
import git_meta  # noqa: E402
import mcp_server  # noqa: E402


class BoostGitChangedTestCase(unittest.TestCase):
    def test_dirty_files_boosted_with_reason(self):
        items = [{"file": "a.py", "summary": "", "token_estimate": 100,
                  "priority": "primary", "action": "load_file", "why": [],
                  "key_symbols": []}]
        changed = [{"path": "b.py", "status": "M"}]
        out = agent._boost_git_changed(items, changed)
        self.assertEqual(len(out), 2)
        boosted = out[1]
        self.assertEqual(boosted["file"], "b.py")
        self.assertEqual(boosted["priority"], "secondary")
        self.assertEqual(boosted["why"], ["git_dirty:M"])

    def test_existing_files_not_duplicated(self):
        items = [{"file": "a.py"}, {"file": "sub/b.py"}]
        changed = [
            {"path": "a.py", "status": "M"},       # zaten listede → eklenmez
            {"path": "sub\\b.py", "status": "M"},  # normalize çakışma → eklenmez
            {"path": "c.py", "status": "M"},
        ]
        out = agent._boost_git_changed(items, changed)
        files = [f["file"] for f in out]
        self.assertEqual(files.count("a.py"), 1)
        self.assertEqual(files.count("sub/b.py"), 1)
        self.assertIn("c.py", files)

    def test_max_three_boosted(self):
        changed = [{"path": f"f{i}.py", "status": "M"} for i in range(6)]
        out = agent._boost_git_changed([], changed)
        self.assertEqual(len(out), 3)

    def test_noise_filtered(self):
        changed = [
            {"path": "cache.pyc", "status": "?"},
            {"path": "app.log", "status": "M"},
            {"path": "pkg/node_modules/x.js", "status": "M"},
            {"path": "yarn.lock", "status": "M"},
            {"path": "auth/", "status": "??"},                    # dizin girdisi
            {"path": ".context/symbols.db", "status": "M"},       # motor durumu
            {"path": "real.py", "status": "M"},
        ]
        out = agent._boost_git_changed([], changed)
        self.assertEqual([f["file"] for f in out], ["real.py"])

    def test_inserted_after_primary_block(self):
        items = [
            {"file": "p1.py", "priority": "primary"},
            {"file": "p2.py", "priority": "primary"},
            {"file": "s1.py", "priority": "secondary"},
            {"file": "t1.py", "priority": "tertiary"},
        ]
        out = agent._boost_git_changed(items, [{"path": "work.py", "status": "M"}])
        self.assertEqual([f["file"] for f in out],
                         ["p1.py", "p2.py", "work.py", "s1.py", "t1.py"])

    def test_unrelated_dirty_files_not_boosted_for_targeted_task(self):
        items = [{"file": "frontend/components/Button.tsx", "priority": "primary"}]
        changed = [{"path": "auth/models.py", "status": "M"},
                   {"path": "frontend/theme.ts", "status": "M"}]
        out = agent._boost_git_changed(items, changed,
                                       task_keywords=["button", "border", "radius"])
        # A dirty file in the same broad directory is not task evidence by
        # itself; otherwise large worktrees inject arbitrary nearby changes.
        self.assertEqual([f["file"] for f in out],
                         ["frontend/components/Button.tsx"])


class GitParsingTestCase(unittest.TestCase):
    """git subprocess yerine _git'i sahte çıktıyla besle (ortam-bağımsız)."""

    def test_changed_files_parses_porcelain(self):
        porcelain = " M scripts/agent.py\n?? new_file.txt\nR  old.py -> new.py\n"
        with unittest.mock.patch.object(git_meta, "_git", return_value=porcelain):
            out = git_meta.changed_files(Path("."))
        self.assertEqual(out[0], {"path": "scripts/agent.py", "status": "M"})
        self.assertEqual(out[1], {"path": "new_file.txt", "status": "??"})
        # rename → yeni yol
        self.assertEqual(out[2], {"path": "new.py", "status": "R"})

    def test_changed_files_limit(self):
        porcelain = "\n".join(f" M f{i}.py" for i in range(20))
        with unittest.mock.patch.object(git_meta, "_git", return_value=porcelain):
            out = git_meta.changed_files(Path("."), limit=5)
        self.assertEqual(len(out), 5)

    def test_recent_files_dedup_and_order(self):
        log = "b.py\na.py\n\nb.py\nc.py\n"
        with unittest.mock.patch.object(git_meta, "_git", return_value=log):
            out = git_meta.recent_files(Path("."))
        self.assertEqual(out, ["b.py", "a.py", "c.py"])


class ChunkMapTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_coarse_map_without_symbols_db(self):
        entries = get_range_mod._chunk_map("big.py", self.root, 400)
        # 51..400 aralığı 150'lik adımlarla haritalanır.
        self.assertTrue(all(e["type"] == "range" for e in entries))
        self.assertEqual(entries[0]["lines"], "51-200")
        self.assertEqual(entries[-1]["lines"], "351-400")
        self.assertIn("get_range.py", entries[0]["get_cmd"])

    def test_large_file_uses_query_matched_symbol_body_not_header_only(self):
        file_info = {
            "file": "big.py", "priority": "primary", "action": "load_file",
            "focused_by_query": True,
            "key_symbols": [{"name": "prevent_duplicate", "type": "function",
                             "line": 2100}],
        }

        def fake_call(module, function, *args, **kwargs):
            if module == "get_range":
                return {"content": "# header only", "token_estimate": 4,
                        "mode": "preview", "chunk_map": [{"lines": "2051-2200"}],
                        "get_full": "get full"}
            if module == "get_symbol":
                return {"name": "prevent_duplicate", "type": "function",
                        "lines": "2100-2140", "content": "def prevent_duplicate():\n    pass\n",
                        "token_estimate": 8}
            return {}

        with unittest.mock.patch.object(agent, "call_component", side_effect=fake_call), \
             unittest.mock.patch.object(agent, "ensure_fresh_file",
                                        return_value={"fresh": True}):
            item = agent.build_context_item(file_info)
        self.assertEqual(item["mode"], "focused_symbol")
        self.assertIn("def prevent_duplicate", item["content"])
        self.assertEqual(item["focused_symbol"]["lines"], "2100-2140")
        self.assertTrue(item["chunk_map"])

    def test_symbol_map_from_db(self):
        db = self.root / ".context" / "symbols.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "CREATE TABLE symbols (name TEXT, type TEXT, file TEXT, "
                "start_line INTEGER, end_line INTEGER)")
            conn.execute(
                "INSERT INTO symbols VALUES (?,?,?,?,?)",
                ("helper", "function", "big.py", 60, 90))
            conn.execute(
                "INSERT INTO symbols VALUES (?,?,?,?,?)",
                ("Main", "class", "big.py", 100, 400))
            conn.commit()
        finally:
            conn.close()
        entries = get_range_mod._chunk_map("big.py", self.root, 400)
        self.assertEqual([e["name"] for e in entries], ["helper", "Main"])
        self.assertEqual(entries[0]["lines"], "60-90")
        self.assertIn("get_range.py big.py 100 400", entries[1]["get_cmd"])

    def test_backslash_path_normalized(self):
        db = self.root / ".context" / "symbols.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "CREATE TABLE symbols (name TEXT, type TEXT, file TEXT, "
                "start_line INTEGER, end_line INTEGER)")
            conn.execute(
                "INSERT INTO symbols VALUES (?,?,?,?,?)",
                ("fn", "function", "pkg/mod.py", 70, 80))
            conn.commit()
        finally:
            conn.close()
        entries = get_range_mod._chunk_map("pkg\\mod.py", self.root, 200)
        self.assertEqual([e["name"] for e in entries], ["fn"])


class CompactChunkMapTestCase(unittest.TestCase):
    def test_chunk_map_passes_through(self):
        item = {
            "file": "big.py",
            "priority": "primary",
            "action": "load_file",
            "why": ["test"],
            "mode": "preview",
            "chunk_map": [{"name": "helper", "type": "function",
                           "lines": "60-90", "get_cmd": "x"}],
        }
        compact = mcp_server.compact_context_item(item)
        self.assertEqual(compact["chunk_map"], item["chunk_map"])
        self.assertTrue(compact.get("partial"))

    def test_no_chunk_map_when_absent(self):
        compact = mcp_server.compact_context_item(
            {"file": "a.py", "action": "load_file", "why": []})
        self.assertNotIn("chunk_map", compact)


if __name__ == "__main__":
    unittest.main()
