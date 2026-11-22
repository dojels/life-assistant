"""问答转录(D184):用户看到的提问与回答,必须进入模型下一轮的历史。

真机事故:工具把"目标保留约 8,351"发到微信,模型下一轮却说"我查了档案,
没找到我说过 8000" —— 因为提问只落在 ops_questions / feedback/asked(不 surface),
不在 conversation_history 里。
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from la import config
from la.agent.feedback import Answer, Option, Question, STATUS_ANSWERED, STATUS_EXPIRED
from la.agent.session import MemorySession
from la.llm.fake import FakeClient
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY


class _Asker:
    def __init__(self, choice: str = "keep", *, status: str = STATUS_ANSWERED,
                 text: str = "") -> None:
        self.choice = choice
        self.status = status
        self.text = text
        self.questions: list[Question] = []

    async def __call__(self, question: Question) -> Answer:
        question.prepare()
        self.questions.append(question)
        if self.status != STATUS_ANSWERED:
            return Answer(question_id=question.id, status=self.status, by="timeout")
        return Answer(
            question_id=question.id,
            status=STATUS_ANSWERED,
            selected=[self.choice] if self.choice else [],
            text=self.text,
            by="user",
        )


class AskTranscriptCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.tmp / "archive")

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def session(self, asker=None) -> MemorySession:
        settings = dataclasses.replace(
            config.get_settings(reload=True), provider="fake", session_id="s1"
        )
        return MemorySession(
            FakeClient(), settings, store=self.store, session_id="s1", asker=asker,
        )

    def asked_events(self) -> list:
        return [e for e in self.store.events("s1") if e.type == "feedback/asked"]

    async def test_answered_ask_is_replayed_as_assistant_then_user(self) -> None:
        asker = _Asker("keep")
        session = self.session(asker)

        result = await session._handle_ask({
            "prompt": "要记住这条吗?",
            "options": [
                {"label": "记住", "value": "keep"},
                {"label": "不记", "value": "skip"},
            ],
        })

        self.assertEqual(result["status"], STATUS_ANSWERED)
        self.assertEqual(result["question"], "要记住这条吗?")
        history = session.conversation_history()
        self.assertEqual(history[-2]["role"], "assistant")
        self.assertEqual(history[-2]["content"], "要记住这条吗?")
        self.assertEqual(history[-1]["role"], "user")
        self.assertEqual(history[-1]["content"], "记住", "回传的应是用户看到的 label,不是内部 value")
        self.assertEqual(len(self.asked_events()), 1, "一次提问只该有一条 feedback/asked")

    async def test_unanswered_ask_does_not_invent_a_user_message(self) -> None:
        asker = _Asker("", status=STATUS_EXPIRED)
        session = self.session(asker)

        await session._handle_ask({"prompt": "要记住这条吗?"})

        history = session.conversation_history()
        self.assertFalse(any("要记住这条吗" in item["content"] for item in history))
        self.assertEqual(len(self.asked_events()), 1)
        self.assertEqual(self.asked_events()[0].data["status"], STATUS_EXPIRED)

    async def test_domain_ask_goes_through_the_same_transcript(self) -> None:
        """category_create 这类领域代码的提问也走 self.ask —— 必须同样留痕。"""
        asker = _Asker("create")
        session = self.session(asker)
        question = Question(
            prompt="账本里还没有「宠物」这个支出分类,新建吗?",
            options=[Option(label="新建「宠物」", value="create")],
            domain="ledger",
            source="domain",
            session_id="s1",
        )

        answer = await session.ask(question)

        self.assertTrue(answer.answered)
        history = session.conversation_history()
        self.assertEqual(history[-2]["content"], question.prompt)
        self.assertEqual(history[-1]["content"], "新建「宠物」")
        self.assertEqual(len(self.asked_events()), 1)

    async def test_ask_without_channel_writes_nothing(self) -> None:
        session = self.session(asker=None)
        answer = await session.ask(Question(prompt="有人吗?"))
        self.assertFalse(answer.answered)
        self.assertEqual(session.conversation_history(), [])
        self.assertEqual(self.asked_events(), [])

    async def test_transcript_weights_match_the_history_estimator(self) -> None:
        """问答转录也要被选区权重看见,否则下一次压缩又会算错账。"""
        session = self.session(_Asker("keep"))
        await session._handle_ask({
            "prompt": "要记住这条吗?",
            "options": [{"label": "记住", "value": "keep"}],
        })
        events = session._uncompacted_events()
        self.assertEqual(
            sum(session._conversation_tokens(event) for event in events),
            config.estimate_messages_tokens(session.conversation_history()),
        )


if __name__ == "__main__":
    unittest.main()
