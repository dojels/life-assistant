"""记忆检索输出预算:不再把 9,839 字符的 JSON 交给 4,000 字符截断器。

2026-11-21 真机:#23 摘要 4,497 字 + 447 锚点;"错题"命中 8 条 = 9,839 字符,
_converse 把工具结果转成 preview 字符串,后面的命中/锚点丢失。
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from la import config
from la.agent.session import (
    MAX_QUERY_ANCHORS,
    MAX_QUERY_OUTPUT_CHARS,
    MAX_QUERY_TEXT_CHARS,
    MemorySession,
    _summary_excerpt,
)
from la.llm.fake import FakeClient
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY


class SummaryPoolOutputCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.tmp / "archive")
        for index in range(10):
            self.store.upsert_summary(
                session_id="s1",
                start_seq=index + 1,
                end_seq=index + 1,
                start_time="2026-11-20T10:00:00+08:00",
                end_time="2026-11-20T11:00:00+08:00",
                text="错题本记录 " + ("很长的内容 " * 400) + "第 %d 条" % index,
                anchors=["锚点%d" % i for i in range(100)],
                tags=["study"],
            )
        self.session = MemorySession(
            FakeClient(),
            dataclasses.replace(
                config.get_settings(reload=True), provider="fake", session_id="s1"
            ),
            store=self.store,
            session_id="s1",
        )

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    async def test_payload_stays_under_the_tool_truncation_line(self) -> None:
        payload = await self.session._handle_summary_pool({"query": "错题", "limit": 10})
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertIsInstance(json.loads(blob), dict, "返回必须是合法 JSON")
        self.assertLessEqual(len(blob), MAX_QUERY_OUTPUT_CHARS)
        self.assertGreaterEqual(payload["total_hits"], payload["returned_hits"])
        self.assertGreater(payload["returned_hits"], 0)
        self.assertTrue(payload["truncated"], "10 条长摘要不可能全塞进预算")
        for hit in payload["hits"]:
            self.assertLessEqual(len(hit["text"]), MAX_QUERY_TEXT_CHARS)
            self.assertLessEqual(len(hit["anchors"]), MAX_QUERY_ANCHORS)
            self.assertIn("anchors_total", hit)
            self.assertIn("text_truncated", hit)

    async def test_excerpt_is_centered_on_the_query_term(self) -> None:
        text = "前文" * 300 + "目标词在这里" + "后文" * 300
        excerpt = _summary_excerpt(text, ["目标词"], 120)
        self.assertIn("目标词", excerpt)
        self.assertLessEqual(len(excerpt), 122)

    async def test_no_hits_still_returns_a_valid_shape(self) -> None:
        payload = await self.session._handle_summary_pool({"query": "完全不存在的词"})
        self.assertEqual(payload["status"], "miss")
        self.assertEqual(payload["hits"], [])
        self.assertEqual(payload["total_hits"], 0)


if __name__ == "__main__":
    unittest.main()
