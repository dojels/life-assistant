"""渠道(微信 / 终端)的「提问回环」回归测试 —— 2026-10-13 真机翻车之后补的。

**真机现象**:在微信里让助手做一件事 → 它反问一句 → 用户**在微信里回答了** →
毫无反应;网页上那条问题一直显示「在等你拍板」,一直挂到五分钟超时。

**两个真因**(都在这里钉住):

  1. **适配器把入站消息串行处理**。模型反问时那一轮挂在 thread.handle 里等下一句,
     而主循环 await self._handle_raw(raw) 让那句回答排在它后面 —— 它要等的那条消息
     正卡在它自己后面,是**自锁**。修:一条消息一个任务(_dispatch / _spawn)。
  2. **回答可能来自别的进程**:用户在网页上点按钮时,那条路只改共享的 ops_questions,
     碰不到桥接进程里的 future。修:两个等待方都每隔一秒看库并**认领**(QuestionBroker.adopt)。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_bridge_ask -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from la import config
from la.agent.feedback import QuestionBroker, QuestionLog
from la.bridge import Bridge, Inbound, SendAction
from la.bridge.adapters.console import ConsoleAdapter
from la.bridge.adapters.weixin_ilink import ILinkAdapter
from la.bridge.bridge import render_for_chat
from la.llm.client import ToolCall
from la.llm.fake import FakeClient
from la.storage import db


class Collector:
    """测试用传输层:把出站动作记下来。"""

    name = "collector"

    def __init__(self) -> None:
        self.actions: list[SendAction] = []
        self.typing_calls = 0

    async def send(self, action: SendAction) -> None:
        self.actions.append(action)

    async def typing(self, thread_id: str) -> None:
        self.typing_calls += 1

    @property
    def asked(self) -> list[str]:
        return [item.text for item in self.actions if "需要你拍板" in item.text]


class AskFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "ARTIFACTS_DIR", "ASSISTANT_DB",
                        "MEMORY_DB", "LOGS_DIR")
        }
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config.LOGS_DIR = self.tmp / "logs"
        config.ensure_runtime_dirs()
        self.assistant = db.connect_assistant()
        self.bridges: list[Bridge] = []

    def tearDown(self) -> None:
        for bridge in self.bridges:
            try:
                bridge.close()
            except Exception:
                pass
        try:
            self.assistant.close()
        except Exception:
            pass
        for key, value in self._patched.items():
            setattr(config, key, value)
        self._tmp.cleanup()

    # -- 工具 --------------------------------------------------------------
    def scripted_bridge(self, transport: Collector) -> Bridge:
        """一个把模型脚本固定成"先反问、拿到回答再收尾"的桥。"""
        client = FakeClient(
            tool_script=[[
                ToolCall(
                    id="q1",
                    name="ask_user",
                    arguments=json.dumps(
                        {"prompt": "这笔算餐饮还是交通?",
                         "options": [{"label": "餐饮"}, {"label": "交通"}]},
                        ensure_ascii=False,
                    ),
                )
            ]],
            scripted=["好,按你选的记。"],
        )
        bridge = _ScriptedBridge(client, transport=transport)
        self.bridges.append(bridge)
        return bridge

    async def _wait_pending(self, timeout: float = 5.0) -> str:
        """等提问真的落库,返回 question_id。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            row = self.assistant.execute(
                "SELECT id FROM ops_questions WHERE status = 'pending' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                return str(row[0])
            await asyncio.sleep(0.02)
        self.fail("提问没有落库")

class ChannelAndCrossProcessCase(AskFixture):
    """真因 ②:回答可能来自本渠道,也可能来自**另一个进程**。"""

    # -- ① 渠道里答一句 -----------------------------------------------------
    async def test_answer_in_the_channel_resumes_the_turn(self) -> None:
        transport = Collector()
        thread = self.scripted_bridge(transport).thread("wxid_me")
        turn = asyncio.create_task(_drain(thread, _msg("刚花了一笔", "wx:1")))

        for _ in range(250):
            if transport.asked:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(transport.asked, "问题必须发到渠道里")

        answered = await _drain(thread, _msg("2", "wx:2"))
        self.assertTrue(
            any(event["event"] == "answered" for event in answered),
            "这一条必须被识别成'回答问题',而不是新起一轮",
        )
        events = await asyncio.wait_for(turn, timeout=5.0)
        self.assertIn("好,按你选的记。", render_for_chat(events))
        asked = _last_asked_event(thread)
        self.assertEqual(asked["status"], "answered")
        self.assertEqual(asked["selected"], ["交通"], "选第 2 项要落到选项的 value 上")

    # -- ② 别的进程(网页)答的 ----------------------------------------------
    async def test_answer_from_another_process_is_adopted(self) -> None:
        transport = Collector()
        thread = self.scripted_bridge(transport).thread("wxid_me")
        turn = asyncio.create_task(_drain(thread, _msg("刚花了一笔", "wx:1")))
        question_id = await self._wait_pending()

        # 模拟**另一个进程**的网页回答:它有自己的一份台账与登记处,
        # 对共享状态的唯一影响就是 ops_questions 里那一行变成终态。
        web_log = QuestionLog(self.assistant)
        web_broker = QuestionBroker()
        web_broker.answer(question_id, selected=["餐饮"], by="user", log=web_log)

        events = await asyncio.wait_for(turn, timeout=8.0)
        self.assertIn("好,按你选的记。", render_for_chat(events))
        asked = _last_asked_event(thread)
        self.assertEqual(asked["selected"], ["餐饮"], "桥接层必须认领别的进程答的那个答案")
        self.assertNotEqual(asked["by"], "duplicate", "认领不是'重复回答'")

    async def test_unbound_weixin_contact_is_gated(self) -> None:
        transport = Collector()
        bridge = self.scripted_bridge(transport)
        thread = bridge.thread("wxid_stranger", require_binding=True)
        self.assertTrue(thread.unbound)
        events = await _drain(thread, _msg("你好", "wx:stranger"))
        self.assertIn("还没有绑定", render_for_chat(events))
        self.assertEqual(bridge._scripted.calls, [], "未绑定不能进入模型")
        users = [
            event for event in thread.session.store.events(thread.session_id)
            if event.type == "user/message"
        ]
        self.assertEqual(users, [], "未绑定的消息不能落进记忆")
        marks = [
            event for event in thread.session.store.events(thread.session_id)
            if event.type == "bridge/unbound"
        ]
        self.assertTrue(marks, "要留下联系人标识,设置页才能列出待绑定")
        self.assertEqual(marks[-1].data["external_id"], "wxid_stranger")

    async def test_bound_weixin_contact_gets_member_session(self) -> None:
        from la.family import identity

        identity.bind_member(self.assistant, "weixin", "wxid_mom", 1)
        transport = Collector()
        thread = self.scripted_bridge(transport).thread(
            "wxid_mom", require_binding=True
        )
        self.assertFalse(thread.unbound)
        self.assertEqual(thread.session_id, "member:1")

    async def test_ask_user_routes_to_the_sender(self) -> None:
        """家庭版一个 bot 多个联系人:追问必须回到发消息的那个人。"""
        transport = Collector()
        thread = self.scripted_bridge(transport).thread("wxid_mom")
        turn = asyncio.create_task(_drain(thread, _msg("刚花了一笔", "wx:ask")))
        for _ in range(250):
            if transport.asked:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(transport.asked, "问题必须发到渠道里")
        asked = [item for item in transport.actions if "需要你拍板" in item.text]
        self.assertEqual(asked[-1].reply_to, "wxid_mom")

        await _drain(thread, _msg("1", "wx:answer"))
        await asyncio.wait_for(turn, timeout=8.0)

    async def test_an_already_terminal_question_reads_back_instead_of_waiting(self) -> None:
        """库里已经是终态的问题:等待方能**读到**它,而不是傻等到超时。"""
        log = QuestionLog(self.assistant)
        log.conn.execute(
            "INSERT INTO ops_questions(id, ts, expires_at, session_id, domain, source,"
            " prompt, options, context, allow_text, multi, status, answer, answered_at,"
            " answered_by) VALUES ('q_dead', '2026-10-13T00:00:00+00:00', '', 's', 'system',"
            " 'agent', '老问题', '[]', '{}', 1, 0, 'answered', ?, '2026-10-13T00:01:00+00:00',"
            " 'user')",
            (json.dumps({"question_id": "q_dead", "status": "answered", "selected": ["好"],
                         "text": "", "by": "user"}, ensure_ascii=False),),
        )
        log.conn.commit()
        broker = QuestionBroker()
        answer = await broker.wait("q_dead", timeout_s=2.0, log=log)
        self.assertEqual(answer.status, "answered")
        self.assertEqual(answer.selected, ["好"])


class StaleQuestionCase(AskFixture):
    """进程重启后,那些"没人等"的问题必须被标掉 —— 否则网页永远显示"在等你拍板"。"""

    async def test_restart_expires_questions_nobody_is_waiting_for(self) -> None:
        self.assistant.execute(
            "INSERT INTO ops_questions(id, ts, expires_at, session_id, domain, source,"
            " prompt, options, context, allow_text, multi, status)"
            " VALUES ('q_old', '2026-10-13T00:00:00+00:00', '', 'bridge:wxid_me', 'system',"
            " 'agent', '上一轮的问题', '[]', '{}', 1, 0, 'pending')"
        )
        self.assistant.commit()

        bridge = self.scripted_bridge(Collector())
        bridge.thread("wxid_me")          # 新进程起线程 = 收拾上一轮的旧账

        row = self.assistant.execute(
            "SELECT status FROM ops_questions WHERE id = 'q_old'"
        ).fetchone()
        self.assertEqual(str(row[0]), "expired")
        self.assertIn("重启", json.loads(self.assistant.execute(
            "SELECT answer FROM ops_questions WHERE id = 'q_old'"
        ).fetchone()[0])["note"])


class AdapterDispatchCase(AskFixture):
    """真因 ①:适配器的入站处理**不能串行 await**。"""

    async def test_wechat_adapter_dispatch_does_not_wait(self) -> None:
        adapter = ILinkAdapter(bridge=None, client=_StubClient(), log=lambda line: None)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(raw):
            started.set()
            await release.wait()

        adapter._handle_raw = slow  # type: ignore[assignment]
        adapter._dispatch({"probe": True})
        await asyncio.wait_for(started.wait(), timeout=2.0)
        self.assertTrue(adapter._tasks, "消息应该在独立任务里跑着,而不是被 await 掉")
        release.set()
        await asyncio.gather(*list(adapter._tasks), return_exceptions=True)
        self.assertFalse(adapter._tasks)

    async def test_console_adapter_dispatch_does_not_block_the_next_line(self) -> None:
        adapter = ConsoleAdapter()
        self.bridges.append(adapter.bridge)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(text: str, seq: int) -> None:
            started.set()
            await release.wait()

        adapter._on_message = slow  # type: ignore[assignment]
        adapter._spawn(adapter._on_message("第一句", 1))
        await asyncio.wait_for(started.wait(), timeout=2.0)
        self.assertTrue(adapter._tasks)
        release.set()
        await asyncio.gather(*list(adapter._tasks), return_exceptions=True)


# -- 小工具 ----------------------------------------------------------------
class _ScriptedBridge(Bridge):
    """把 FakeClient 塞进每个线程(与 scripts/verify_bridge.py 同一手法)。"""

    def __init__(self, client: FakeClient, **kwargs) -> None:
        super().__init__(**kwargs)
        self._scripted = client

    def thread(self, thread_id: str, **kwargs):
        found = self._threads.get(thread_id)
        if found is None:
            import la.bridge.bridge as core

            original = core.build_client
            core.build_client = lambda settings: self._scripted
            try:
                found = super().thread(thread_id, **kwargs)
            finally:
                core.build_client = original
        return found


class _StubClient:
    """只占位:ILinkAdapter.__init__ 不会用它发任何东西。"""

    async def aclose(self) -> None:
        return None


def _msg(text: str, external_id: str) -> Inbound:
    return Inbound(channel="wechat", user_id="wxid_me", thread_id="wxid_me",
                   text=text, external_id=external_id)


async def _drain(thread, inbound: Inbound) -> list[dict]:
    return [event async for event in thread.handle(inbound)]


def _last_asked_event(thread) -> dict:
    events = [
        item for item in thread.session.store.events(thread.session_id)
        if item.type == "feedback/asked"
    ]
    assert events, "没有 feedback/asked 事件"
    return events[-1].data


if __name__ == "__main__":
    unittest.main()
