"""记忆隔离:成员 A 的检索不能看到成员 B 的事实/摘要/原文。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_memory_isolation -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from la.memory.retrieval import retrieve
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY
from la.tools.archive_tools import build_archive_tools


class MemoryIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.tmp / "archive")

        self.store.add_fact("妈妈喜欢喝茶", session_id="member:1")
        self.store.add_fact("爸爸喜欢咖啡", session_id="member:2")
        # 迁移前遗留的 NULL = 全局事实,兼容保留给所有人
        self.store.add_fact("全家住在杭州")

        for session, text in (("member:1", "妈妈聊装修预算"), ("member:2", "爸爸聊装修预算")):
            self.conn.execute(
                "INSERT INTO summary_pool(session_id, start_seq, end_seq, start_time,"
                " end_time, text, anchors, tags, category, created_at)"
                " VALUES (?, 1, 1, '2026-10-01T10:00:00+08:00',"
                " '2026-10-01T10:01:00+08:00', ?, '[]', '[]', 'periodic',"
                " '2026-10-01T10:00:00+08:00')",
                (session, text),
            )
        self.conn.commit()

        self.store.append("member:1", "user/message", {"text": "妈妈的原文"})
        self.store.append("member:2", "user/message", {"text": "爸爸的原文"})

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_active_facts_are_scoped_but_legacy_null_stays_global(self):
        mom = [row.content for row in self.store.active_facts(session_id="member:1")]
        dad = [row.content for row in self.store.active_facts(session_id="member:2")]
        self.assertIn("妈妈喜欢喝茶", mom)
        self.assertNotIn("爸爸喜欢咖啡", mom)
        self.assertIn("爸爸喜欢咖啡", dad)
        self.assertNotIn("妈妈喜欢喝茶", dad)
        self.assertIn("全家住在杭州", mom)
        self.assertIn("全家住在杭州", dad)

    def test_search_facts_is_scoped(self):
        mom = [row.content for row in self.store.search_facts("喜欢", session_id="member:1")]
        dad = [row.content for row in self.store.search_facts("喜欢", session_id="member:2")]
        self.assertIn("妈妈喜欢喝茶", mom)
        self.assertNotIn("爸爸喜欢咖啡", mom)
        self.assertIn("爸爸喜欢咖啡", dad)
        self.assertNotIn("妈妈喜欢喝茶", dad)

    def test_retrieve_returns_only_the_members_summaries_and_facts(self):
        mom = retrieve(self.store, "装修", session_id="member:1")
        dad = retrieve(self.store, "装修", session_id="member:2")
        self.assertEqual([row.text for row in mom.summaries], ["妈妈聊装修预算"])
        self.assertEqual([row.text for row in dad.summaries], ["爸爸聊装修预算"])
        self.assertIn("妈妈喜欢喝茶", [row.content for row in mom.facts])
        self.assertNotIn("爸爸喜欢咖啡", [row.content for row in mom.facts])

    def test_archive_search_is_scoped(self):
        tools = build_archive_tools(self.store, session_id="member:1")
        spec = next(item for item in tools if item.name == "query_archive_search")
        result = asyncio.run(spec.handler({"query": "原文", "limit": 10}))
        texts = " ".join(item["text"] for item in result["events"])
        self.assertIn("妈妈的原文", texts)
        self.assertNotIn("爸爸的原文", texts)


if __name__ == "__main__":
    unittest.main()
