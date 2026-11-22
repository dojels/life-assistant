"""手动压缩上下文:用户说一句 / 点一下就能压;确认在前,结果如实。

三条 DoD:
  1. **必须先问**:compact_context 工具自己问确认;没回答/答得含糊一律不压;
  2. **网页按钮**:聊天页与记忆页 L5 各一个按钮,普通 POST + 303,不依赖 JS;
  3. **结果如实**:成功给前后 token 与摘要号;没压成就说没压成,绝不冒充。
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import unquote

from fastapi.testclient import TestClient

from la import config
from la.agent.feedback import Answer, STATUS_ANSWERED, STATUS_EXPIRED
from la.agent.session import MemorySession
from la.base.phases import PHASE_ANSWER, CircuitBreaker
from la.llm.client import ToolCall
from la.llm.fake import FakeClient
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY
from la.tools.registry import CODE_READ_ONLY
from la.web import app as web_app


def make_settings(session_id: str = "s1", **overrides) -> config.Settings:
    return dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id=session_id, **overrides
    )


def seed_history(store: MemoryStore, session_id: str = "s1", *, pairs: int = 12,
                 chars: int = 200) -> None:
    """塞足够多的历史,让 pick_compaction_range 真的有区间可切。"""
    for index in range(pairs):
        store.append(session_id, "user/message", {"text": "第%d问 " % index + "字" * chars})
        store.append(session_id, "assistant/message", {"text": "第%d答 " % index + "字" * chars})


class _Asker:
    """记录被问了什么,并给出一个确定的回答(替代 Web/微信通道)。"""

    def __init__(self, choice: str = "压缩", *, status: str = STATUS_ANSWERED,
                 text: str = "") -> None:
        self.choice = choice
        self.status = status
        self.text = text
        self.questions: list[object] = []

    async def __call__(self, question):
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


class SessionCase(unittest.IsolatedAsyncioTestCase):
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

    def new_session(self, asker=None, **settings_overrides) -> MemorySession:
        return MemorySession(
            FakeClient(),
            make_settings(**settings_overrides),
            store=self.store,
            session_id="s1",
            asker=asker,
        )

    def summaries(self) -> int:
        return len(self.store.all_summaries("s1"))

    # -- 1. 手动压缩本身 -----------------------------------------------------
    async def test_manual_compaction_reports_before_after_and_summary(self) -> None:
        seed_history(self.store)
        session = self.new_session()
        self.assertTrue(session.compaction_status()["compactable"])

        result = await session.compact_manual()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "compacted")
        self.assertGreater(result["before_tokens"], result["after_tokens"])
        self.assertGreater(int(result["summary_id"]), 0)
        self.assertEqual(self.summaries(), 1)
        events = [e for e in self.store.events("s1") if e.type == "compaction/summary"]
        self.assertEqual(events[-1].data.get("via"), "manual", "手动压缩要留痕")
        self.assertEqual(events[-1].data.get("summary_id"), result["summary_id"])

    async def test_manual_compaction_without_a_range_is_a_clear_noop(self) -> None:
        seed_history(self.store, pairs=2)  # 只有 4 条,低于"保留最近 6 条"
        session = self.new_session()
        result = await session.compact_manual()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "nothing_to_compact")
        self.assertEqual(self.summaries(), 0)

    # -- 2. 工具:先问、后压 -------------------------------------------------
    async def test_tool_asks_then_compacts_on_confirmation(self) -> None:
        seed_history(self.store)
        asker = _Asker("压缩")
        session = self.new_session(asker)

        result = await session._handle_compact_context({})

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(asker.questions), 1, "必须只问一次")
        prompt = str(asker.questions[0].prompt)
        self.assertIn("压缩上下文", prompt)
        self.assertIn("当前历史", prompt)
        labels = [item.label for item in asker.questions[0].options]
        self.assertEqual(labels, ["压缩", "先不压"])
        # 问出去的那句话必须回到工具结果里 —— 否则模型下一轮看不到自己承诺过什么。
        self.assertIn("压缩上下文", result.get("question", ""))
        self.assertIn("目标保留", result.get("question", ""))
        self.assertGreater(int(result.get("target_tokens", 0)), 0)
        self.assertEqual(self.summaries(), 1)

    async def test_conversation_weights_match_the_real_estimator(self) -> None:
        """选区用的权重必须与 conversation_history 的估算**逐 token 相等**。

        ⚠️ estimate_messages_tokens 对每条消息都加一次
        estimate_tokens(reasoning_content or "") —— 空串也算 4。少算这 4,
        811 条消息就差 3,244,真机上会多压一截。
        """
        seed_history(self.store)
        session = self.new_session()
        events = session._uncompacted_events()
        self.assertEqual(
            sum(session._conversation_tokens(event) for event in events),
            config.estimate_messages_tokens(session.conversation_history()),
        )

    async def test_tool_result_events_do_not_make_compaction_stop_early(self) -> None:
        """⭐ 2026-11-21 真机:41,759 只压到 31,225。

        原因不是"最近 6 条太多"(那 6 条只有 47 token),而是选区累计的是
        **事件 JSON**:tool/result 的 JSON 很大却不进对话历史,累计很快到线,
        区间提前结束。这里用"大工具结果夹在对话里"把旧病复现:
        按文字计权应当压掉一半以上,按 JSON 计权只会压掉一两轮。
        """
        for index in range(8):
            self.store.append("s1", "user/message", {"text": "第%d问 " % index + "字" * 200})
            self.store.append("s1", "assistant/message", {"text": "第%d答 " % index + "字" * 200})
            self.store.append(
                "s1", "tool/result",
                {"tool": "expense_query", "ok": True, "payload": "x" * 4000},
            )
        session = self.new_session()

        result = await session.compact_manual()

        self.assertTrue(result["ok"], result)
        before = int(result["before_tokens"])
        after = int(result["after_tokens"])
        self.assertLessEqual(
            after, int(before * 0.5),
            "工具结果把选区提前截断了:{} → {},按文字计权不该剩这么多".format(before, after),
        )
        self.assertGreater(after, 0)
        self.assertEqual(result["target_tokens"], int(before * 0.2))

    async def test_tool_declines_without_writing(self) -> None:
        seed_history(self.store)
        session = self.new_session(_Asker("先不压"))

        result = await session._handle_compact_context({})

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "declined")
        self.assertEqual(self.summaries(), 0)
        self.assertFalse(
            [e for e in self.store.events("s1") if e.type == "compaction/start"],
            "用户没同意就不该留下任何压缩开始事件",
        )

    async def test_tool_timeout_is_not_consent(self) -> None:
        seed_history(self.store)
        session = self.new_session(_Asker("", status=STATUS_EXPIRED))

        result = await session._handle_compact_context({})

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(self.summaries(), 0)

    async def test_tool_without_question_channel_does_not_compact(self) -> None:
        seed_history(self.store)
        session = self.new_session(asker=None)

        result = await session._handle_compact_context({})

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(self.summaries(), 0)

    async def test_short_history_does_not_ask(self) -> None:
        seed_history(self.store, pairs=2)
        asker = _Asker("压缩")
        session = self.new_session(asker)

        result = await session._handle_compact_context({})

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "nothing_to_compact")
        self.assertEqual(asker.questions, [], "没有可压区间就不该打扰用户")
        self.assertEqual(self.summaries(), 0)

    # -- 3. 工具层:注册 / 只读闸 --------------------------------------------
    async def test_tool_is_registered_and_read_only_blocks_it(self) -> None:
        seed_history(self.store)
        spec = self.new_session().registry.get("compact_context")
        self.assertIsNotNone(spec, "工具没注册")
        self.assertTrue(spec.writes)
        self.assertEqual(spec.title, "压缩上下文")

        locked = self.new_session(read_only=True)
        breaker = CircuitBreaker(phase=PHASE_ANSWER)
        result = await locked.registry.execute("compact_context", {}, breaker=breaker)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, CODE_READ_ONLY)
        self.assertEqual(self.summaries(), 0)


class _WebFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._saved = {
            key: getattr(config, key)
            for key in (
                "DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
                "ASSISTANT_DB", "MEMORY_DB",
            )
        }
        self._env = {
            key: os.environ.get(key)
            for key in ("DEEPSEEK_API_KEY", "LA_LLM_PROVIDER", "LA_READ_ONLY")
        }
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config._settings = None
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["LA_LLM_PROVIDER"] = "fake"
        os.environ.pop("LA_READ_ONLY", None)
        for name in ("archive", "media", "artifacts"):
            (self.tmp / name).mkdir(exist_ok=True)
        assistant = db.connect_assistant()
        db.migrate(assistant, MIGRATIONS_ASSISTANT)
        assistant.close()
        self.mem = db.connect(config.MEMORY_DB)
        db.migrate(self.mem, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.mem, archive_dir=config.ARCHIVE_DIR)
        seed_history(self.store, "member:1")
        self.client = TestClient(web_app.create_app(), follow_redirects=False)

    def tearDown(self) -> None:
        web_app._build_client = getattr(self, "_real_build", web_app._build_client)
        try:
            self.mem.close()
        except Exception:
            pass
        for key, value in self._saved.items():
            setattr(config, key, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._settings = None
        self._tmp.cleanup()

    def summaries(self) -> int:
        return len(self.store.all_summaries("member:1"))

class WebCase(_WebFixture):
    # -- 4. 网页按钮 ---------------------------------------------------------
    def test_chat_page_has_the_button(self) -> None:
        page = self.client.get("/chat")
        self.assertEqual(page.status_code, 200)
        self.assertIn('action="/context/compact"', page.text)
        self.assertIn(">压缩</button>", page.text)

    def test_memory_l5_has_the_button(self) -> None:
        page = self.client.get("/memory")
        self.assertEqual(page.status_code, 200)
        self.assertIn('action="/context/compact"', page.text)
        self.assertIn("立即压缩上下文", page.text)

    def test_button_compacts_and_flashes_the_result(self) -> None:
        response = self.client.post(
            "/context/compact", data={"_idem": "compact-1", "next": "/chat"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("ok=", response.headers["location"])
        self.assertIn("已压缩", unquote(response.headers["location"]))
        # 回跳到聊天页之后,提示要真的渲染出来(不是只写在 Location 里)。
        page = self.client.get(response.headers["location"])
        self.assertEqual(page.status_code, 200)
        self.assertIn("已压缩", page.text)
        self.assertEqual(self.summaries(), 1)

    def test_same_token_replays_without_compacting_twice(self) -> None:
        first = self.client.post(
            "/context/compact", data={"_idem": "compact-2", "next": "/chat"},
        )
        self.assertEqual(first.status_code, 303)
        second = self.client.post(
            "/context/compact", data={"_idem": "compact-2", "next": "/chat"},
        )
        self.assertEqual(second.status_code, 303)
        self.assertIn("已经处理过", unquote(second.headers["location"]))
        self.assertEqual(self.summaries(), 1, "同一个令牌不该压第二次")

    def test_read_only_mode_refuses_the_button(self) -> None:
        os.environ["LA_READ_ONLY"] = "1"
        config._settings = None
        try:
            response = self.client.post(
                "/context/compact", data={"_idem": "compact-ro", "next": "/chat"},
            )
        finally:
            os.environ.pop("LA_READ_ONLY", None)
            config._settings = None
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers["location"])
        self.assertIn("只读", unquote(response.headers["location"]))
        self.assertEqual(self.summaries(), 0)

    def test_no_history_is_reported_as_noop(self) -> None:
        # 把这个会话的事件清空 —— 模拟"刚开聊天、历史还短"。
        self.store.conn.execute("DELETE FROM events WHERE session_id = ?", ("member:1",))
        self.store.conn.commit()
        response = self.client.post(
            "/context/compact", data={"_idem": "compact-3", "next": "/chat"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers["location"])
        self.assertIn("没有可压", unquote(response.headers["location"]))
        self.assertEqual(self.summaries(), 0)


class SseCase(_WebFixture):
    def chat_in_thread(self, text: str) -> dict:
        box: dict = {}

        def worker():
            try:
                with self.client.stream("POST", "/chat/stream", data={"text": text}) as response:
                    box["body"] = "".join(response.iter_text())
                    box["status"] = response.status_code
            except Exception as exc:  # noqa: BLE001 - 测试里要看到真实异常
                box["error"] = repr(exc)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        box["thread"] = thread
        return box

    def wait_for_question(self, box: dict, timeout: float = 20.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = self.client.get("/api/questions?pending=1").json()
            if payload["questions"]:
                return payload["questions"][0]
            time.sleep(0.05)
        raise AssertionError("等不到提问 | body=" + str(box.get("body"))[:600])

    def test_model_call_compact_context_asks_then_compacts(self) -> None:
        self._real_build = web_app._build_client
        web_app._build_client = lambda settings: FakeClient(
            tool_script=[[ToolCall(id="c1", name="compact_context", arguments="{}")]],
            scripted=["已压缩。"],
        )
        box = self.chat_in_thread("压缩上下文")
        question = self.wait_for_question(box)
        self.assertEqual([item["label"] for item in question["options"]], ["压缩", "先不压"])

        answered = self.client.post(
            "/api/questions/answer",
            json={"question_id": question["id"], "selected": ["压缩"]},
        ).json()
        self.assertTrue(answered["ok"], answered)
        self.assertTrue(answered["resumed"], "那一轮还在等,必须被唤醒")

        box["thread"].join(timeout=30)
        self.assertNotIn("error", box, box.get("error"))
        self.assertIn("event: ask", box["body"])
        self.assertIn("event: done", box["body"])
        self.assertEqual(self.summaries(), 1, "确认之后必须真的压出摘要")


if __name__ == "__main__":
    unittest.main()
