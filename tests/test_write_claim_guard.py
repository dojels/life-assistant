"""说了"已经写好"、却一次都没写 —— 护栏必须**当场更正**(D113)。

真机取证(2026-10-13 22:08,微信):
    用户:「明天中午的时候更新云朵智能体」
    它  :「记好了。**新建提醒(收据 45):** 明天 2026-10-14 12:00,提醒你「更新云朵智能体」。」
事实:那一轮 `assistant/message` 的 tool_calls 为空、`turn/end` 里 task_calls=0 /
answer_write_calls=0,memory.db 的 schedules 表没有那一行,回执表也没涨 ——
**它说做了,但没做**,用户照着去日历页找,什么都没有。编号 45 还是它从十分钟前
那批复习回执里抄来的。

三条纪律钉在这里:
  ① 判据是**事实**(这一轮有没有回执 / 有没有调写入工具),不是文风 ——
     所以"我记得你说过""要是删了会怎样"不该被判成报账;
  ② 更正接在**正文**上(网页最后一帧、微信那条正文、事件日志读的都是 result.text),
     而且不拦它说话(与 D101 "到顶也让它把话说完"一致);2026-10-22 起先补调一次工具,
     补调也没写才更正(见 D155);
  ③ 真的写了就一个字都不改 —— 护栏不能变成噪音。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_write_claim_guard -v
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from la import config
from la.base.phases import CircuitBreaker
from la.agent.session import MemorySession, claims_write
from la.llm.client import ChatResult, ToolCall, Usage
from la.llm.fake import FakeClient
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY

#: 真机上那句原话(测试里照抄,免得将来"优化文案"时把这条回归弄丢)
REAL_REPLY = (
    "记好了。\n\n**新建提醒(收据 45):** 明天 **2026-10-14 12:00**,"
    "提醒你「更新云朵智能体」。\n\n说明:这是**定时提醒**,不在日历里。"
)


def make_settings(session_id: str = "guard") -> config.Settings:
    return dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id=session_id
    )


class ClaimDetectorCase(unittest.TestCase):
    """纯筛查:哪些话算"在报账"。"""

    def test_completion_claims_are_caught(self) -> None:
        for text in (
            REAL_REPLY,
            "已经记好了,待办里能看到。",
            "已建好提醒:明天 12:00。",
            "那条我删掉了。",
            "已取消明天下午的会议。",
            "回执 #12 就是这一次。",
            "收据45",
        ):
            self.assertTrue(claims_write(text), text)

    def test_ordinary_speech_is_not_caught(self) -> None:
        for text in (
            "",
            "我记得你说过要更新云朵智能体。",
            "你要是把那条删了,我就不提醒了。",
            "明天中午要不要我提醒你?",
            "这条提醒现在是开着的。",
            "查不到相关记录。",
            # 2026-10-22 两次真机误报:引号里的"记好了"是引用,不是承诺
            "我说“记好了”却没生成文件、报了不存在的产物 id。",
            "你不太接受我含糊。我说「记好了」却没生成文件。",
            # "已取消的那条会议"是历史状态,不是这一轮取消了什么
            "另一条「修推送功能 + 看新建提醒机制」是 10-12 的,已经过去了"
            "(已取消的那条会议不计入在内)。",
            "已建立的那条不用再建。",
        ):
            self.assertFalse(claims_write(text), text)


class WriteClaimGuardCase(unittest.IsolatedAsyncioTestCase):
    """会话级:一句话说得再像,也得以回执为准。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.tmp / "archive")

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._tmp.cleanup()

    def new_session(self, *, scripted: list[str]) -> MemorySession:
        session = MemorySession(
            FakeClient(scripted=scripted),
            make_settings("guard"),
            store=self.store,
            session_id="guard",
        )
        session.start()
        return session

    def _with_memory_receipts(self, session: MemorySession) -> MemorySession:
        """把记忆库那本回执账挂上(生产装配里 memory_receipts 就在 memory.db 上)。"""
        from la.domain.receipts import ReceiptLog

        session.memory_receipts = ReceiptLog(self.conn)
        return session

    async def test_a_claim_with_no_write_at_all_is_corrected(self) -> None:
        session = self._with_memory_receipts(self.new_session(scripted=[REAL_REPLY]))
        result = await session.respond("明天中午的时候更新云朵智能体")
        self.assertIn("⚠️ 更正", result.text, "说了已经写好、却什么都没写,必须当场挑明")
        self.assertIn("一次写入工具都没调用", result.text)
        types = [event.type for event in self.store.events(session.session_id)]
        self.assertIn("turn/write-claim-without-write", types, "这件事要留痕,不能只改一句话")

    async def test_a_plain_answer_is_left_alone(self) -> None:
        session = self._with_memory_receipts(self.new_session(scripted=["今天 24 度,不用带伞。"]))
        result = await session.respond("今天天气怎么样")
        self.assertNotIn("更正", result.text)

    async def test_a_real_receipt_silences_the_guard(self) -> None:
        """真的写了(有回执)就一个字都不许改 —— 护栏不能变成噪音。"""
        session = self._with_memory_receipts(self.new_session(scripted=[REAL_REPLY]))
        marks = session._receipt_marks()          # noqa: SLF001 - 与 respond() 同一手法
        from la.tasks.todos import TodoStore

        TodoStore(self.conn, receipts=session.memory_receipts).add("交物业费")
        result = session._guard_write_claims(  # noqa: SLF001
            ChatResult(text=REAL_REPLY), CircuitBreaker(), marks
        )
        self.assertEqual(result.text, REAL_REPLY)

    async def test_a_write_tool_call_alone_silences_the_guard(self) -> None:
        """有些写入不落回执(例如记忆事实)—— 那也有工具调用计数器兜着。"""
        session = self._with_memory_receipts(self.new_session(scripted=[REAL_REPLY]))
        marks = session._receipt_marks()          # noqa: SLF001
        result = session._guard_write_claims(  # noqa: SLF001
            ChatResult(text="已经记好了。"), CircuitBreaker(task_calls=1), marks
        )
        self.assertNotIn("更正", result.text)
        result = session._guard_write_claims(  # noqa: SLF001
            ChatResult(text="已经记好了。"), CircuitBreaker(answer_write_calls=1), marks
        )
        self.assertNotIn("更正", result.text)

    async def test_the_correction_lands_in_the_persisted_message(self) -> None:
        """历史读的是事件里那条 assistant/message —— 更正必须写进去,否则刷新就没了。"""
        session = self._with_memory_receipts(self.new_session(scripted=[REAL_REPLY]))
        await session.respond("明天中午的时候更新云朵智能体")
        events = self.store.events(session.session_id)
        said = [e for e in events if e.type == "assistant/message"][-1]
        self.assertIn("⚠️ 更正", str(said.data.get("text") or ""))


class RepairClient(FakeClient):
    """第一次只报账、第二次才发工具调用 —— 复现 2026-10-22 的真机行为。"""

    def __init__(self) -> None:
        super().__init__()
        self.responses = [
            ChatResult(text=REAL_REPLY),
            ChatResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="repair-1",
                        name="todo_write",
                        arguments='{"content":"交物业费","due_date_local":"2026-10-23"}',
                    )
                ],
            ),
            ChatResult(text="记好了:交物业费,10-23 到期。"),
        ]

    async def chat(self, messages, **kwargs):  # type: ignore[override]
        self.calls.append([dict(message) for message in messages])
        result = self.responses.pop(0)
        result.usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        return result


class WriteRepairCase(unittest.IsolatedAsyncioTestCase):
    """说了"已办好"但没调用 —— 先补调工具,而不是只贴一句更正。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.tmp / "archive")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    async def test_a_missed_tool_call_is_repaired_instead_of_only_corrected(self) -> None:
        session = MemorySession(
            RepairClient(), make_settings("repair"), store=self.store, session_id="repair",
        )
        session.start()
        result = await session.respond("明天交物业费")
        self.assertNotIn("⚠️ 更正", result.text)
        self.assertIn("交物业费", result.text)
        pending = session.todos.list(status="pending")
        self.assertEqual([item.content for item in pending], ["交物业费"])
        self.assertEqual(pending[0].due_date_local, "2026-10-23")


if __name__ == "__main__":
    unittest.main()
