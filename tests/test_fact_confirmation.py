"""事实写入必须先经用户确认(2026-10-22 用户点名)。

真机:用户说「你使用的模型是 DeepSeek 4.1 flash,然后你记住这件事」,
助手回「已提交记住,状态是待确认」,但同一轮回复之后 fact_gate 自动裁决把它
写成了 facts #12 —— 用户没有点过任何确认,记忆却已经生效。

这里钉住三件事:
  ① memory_note 在写之前把问题送到提问通道(微信/网页/终端),用户答"记住"才写;
  ② 用户答"不记"/没回答 -> 一个字节都不写,并把"没写"如实告诉模型;
  ③ 确认过的候选也不会在队列里"等下一轮自动放过":先挪出 pending,再当场裁决。
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from la import config
from la.agent.feedback import STATUS_ANSWERED, Answer
from la.agent.session import MemorySession
from la.llm.fake import FakeClient
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY


def make_settings(session_id: str) -> config.Settings:
    return dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id=session_id
    )


class FactConfirmationCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.tmp / "archive")
        self.asked: list[str] = []

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _session(self, *, answer: str | None) -> MemorySession:
        async def asker(question):
            self.asked.append(question.prompt)
            if answer is None:
                return Answer(
                    question_id=question.id, status="expired", by="timeout",
                    note="没回答",
                )
            return Answer(
                question_id=question.id, status=STATUS_ANSWERED,
                selected=[answer], by="user",
            )

        session = MemorySession(
            FakeClient(), make_settings("fact"), store=self.store,
            session_id="fact", asker=asker,
        )
        session.start()
        return session

    async def _note(self, session: MemorySession, content: str) -> dict:
        spec = [item for item in session.registry.specs() if item.name == "memory_note"][0]
        return await spec.handler({"content": content, "fact_type": "other"})

    async def test_confirmed_fact_is_written_immediately(self) -> None:
        session = self._session(answer="记住")
        out = await self._note(session, "助手使用的模型是 DeepSeek 4.1 flash")
        self.assertTrue(self.asked, "写事实之前必须先问用户")
        self.assertIn("要记住这条吗", self.asked[0])
        self.assertEqual(out["status"], "admitted")
        self.assertGreater(out["fact_id"], 0)
        active = {row.content for row in self.store.active_facts()}
        self.assertIn("助手使用的模型是 DeepSeek 4.1 flash", active)

    async def test_declined_fact_is_not_written(self) -> None:
        session = self._session(answer="不记")
        out = await self._note(session, "用户讨厌下雨天")
        self.assertEqual(out["status"], "declined")
        self.assertEqual(out["fact_id"], 0)
        self.assertFalse(out["written"])
        self.assertEqual(self.store.active_facts(), [])
        self.assertEqual(self.store.candidate_counts().get("admitted", 0), 0)

    async def test_no_answer_is_not_consent(self) -> None:
        session = self._session(answer=None)
        out = await self._note(session, "用户养了一只猫")
        self.assertEqual(out["status"], "unconfirmed")
        self.assertEqual(out["fact_id"], 0)
        self.assertFalse(out["written"])
        self.assertEqual(self.store.active_facts(), [])
        self.assertIn("不是同意", out["note"])

    def test_confirmation_tool_waits_longer_than_the_user_thinks(self) -> None:
        """写事实会等用户确认:超时不能还是默认 3 秒,也不能算进本轮墙钟。"""
        from la.tools.registry import USER_WAIT_TOOLS

        session = self._session(answer="记住")
        spec = [item for item in session.registry.specs() if item.name == "memory_note"][0]
        self.assertGreaterEqual(spec.timeout_ms, 300000)
        self.assertIn("memory_note", USER_WAIT_TOOLS)
        self.assertIn("memory_correct", USER_WAIT_TOOLS)

    async def test_confirmed_candidate_never_waits_in_the_queue(self) -> None:
        """确认过的候选直接裁决,不会留在 pending 里被下一轮自动放过。"""
        session = self._session(answer="记住")
        out = await self._note(session, "助手使用的模型是 DeepSeek 4.1 flash")
        self.assertEqual(out["status"], "admitted")
        self.assertEqual(self.store.candidate_counts().get("pending", 0), 0)
        row = self.store.get_candidate(out["candidate_id"])
        self.assertEqual(row["status"], "admitted")


if __name__ == "__main__":
    unittest.main()
