"""M33 笔记的**微信端到端**用例(假 transport + 脚本化模型)。

为什么单独立一个文件:笔记的服务层测试(tests/test_m33_notes.py)证明"它会写";
这个文件证明的是**用户在微信里说一句话,最终看到什么** —— 中间隔着桥接层、
工具门禁、回执托盘、以及 /撤销 命令。这条链任何一环断了,服务层测试都不会红。

四条:
  1. 说"记一下" -> 工具被真的调用 -> 回执发到渠道 -> 库里有这一条;
  2. 回一句 /撤销 -> 笔记进回收站,且**这一轮没有再调用模型**;
  3. 夹不存在 -> 不写,并把 needs_folder 交回模型(而不是悄悄记错地方);
  4. 问起旧事 -> <notes-hint> 真的出现在模型这一轮看到的内容里。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_bridge_notes -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from la import config
from la.bridge import Bridge, Inbound, SendAction
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


class _ScriptedBridge(Bridge):
    """在**建线程那一刻**把模型换成脚本化的假客户端。

    ⚠️ 必须在这一刻替换:Thread 构造时会 build_client(settings) 并把客户端
       交给 MemorySession,之后再换就只换了壳、换不掉已经在用的那份。
    """

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


def _msg(text: str, external_id: str) -> Inbound:
    return Inbound(channel="wechat", user_id="wxid_me", thread_id="wxid_me",
                   text=text, external_id=external_id)


async def _drain(thread, inbound: Inbound) -> list[dict]:
    return [event async for event in thread.handle(inbound)]


def _note_add_call(body: str, **extra) -> ToolCall:
    payload = {"action": "add", "body": body}
    payload.update(extra)
    return ToolCall(id="n1", name="note_add",
                    arguments=json.dumps(payload, ensure_ascii=False))


class NotesBridgeCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
                        "ASSISTANT_DB", "MEMORY_DB", "LOGS_DIR")
        }
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
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
            except Exception:  # noqa: BLE001
                pass
        try:
            self.assistant.close()
        except Exception:  # noqa: BLE001
            pass
        for key, value in self._patched.items():
            setattr(config, key, value)
        config._settings = None
        self._tmp.cleanup()

    # -- 装配 -------------------------------------------------------------
    def bridge(self, transport: Collector, **client_kwargs) -> Bridge:
        client = FakeClient(**client_kwargs)
        bridge = _ScriptedBridge(client, transport=transport)
        bridge._client = client  # type: ignore[attr-defined]
        self.bridges.append(bridge)
        return bridge

    def _notes(self) -> list[dict]:
        rows = self.assistant.execute(
            "SELECT n.id, n.title, n.body, n.deleted_at, f.path AS folder"
            " FROM app_notes n LEFT JOIN app_note_folders f ON f.id = n.folder_id"
            " ORDER BY n.id"
        ).fetchall()
        return [dict(row) for row in rows]

    # -- ① 写 -------------------------------------------------------------
    async def test_a_note_dictated_in_wechat_lands_in_the_inbox_with_a_receipt(self) -> None:
        transport = Collector()
        bridge = self.bridge(
            transport,
            tool_script=[[_note_add_call("今天反手还是拉不起来,球老往左飘")]],
            scripted=["记好了。"],
        )
        events = await _drain(bridge.thread("wxid_me"), _msg(
            "记一下:今天反手还是拉不起来,球老往左飘", "wx:1"))

        notes = self._notes()
        self.assertEqual(len(notes), 1, "说了记一下,就必须真的有一条")
        self.assertEqual(notes[0]["folder"], "收件箱", "没指定夹就该进收件箱")
        self.assertIn("反手", notes[0]["body"])

        receipts = [event["data"] for event in events if event["event"] == "receipt"]
        self.assertTrue(receipts, "微信端靠回执那一行告诉用户「写了什么」")
        self.assertIn("记笔记", receipts[-1])

        text = render_for_chat(events)
        self.assertIn("记好了", text)
        # 模型这一轮确实拿到了工具结果(否则它是在"盲说")
        tool_messages = [
            message for message in bridge._client.calls[-1]  # type: ignore[attr-defined]
            if message.get("role") == "tool"
        ]
        self.assertTrue(tool_messages)
        self.assertIn("note_id", str(tool_messages[-1]["content"]))

    # -- ② 撤销 -----------------------------------------------------------
    async def test_undo_sends_the_note_to_trash_without_calling_the_model(self) -> None:
        transport = Collector()
        bridge = self.bridge(
            transport,
            tool_script=[[_note_add_call("要撤销的一条")]],
            scripted=["记好了。"],
        )
        thread = bridge.thread("wxid_me")
        await _drain(thread, _msg("记一下:要撤销的一条", "wx:1"))
        self.assertEqual(len(self._notes()), 1)
        calls_before = len(bridge._client.calls)  # type: ignore[attr-defined]

        events = await _drain(thread, _msg("/撤销", "wx:2"))

        notes = self._notes()
        self.assertEqual(len(notes), 1, "撤销是软删,不是物理删除")
        self.assertIsNotNone(notes[0]["deleted_at"], "撤销之后这条必须进回收站")
        self.assertIn("已撤销", render_for_chat(events))
        self.assertEqual(
            len(bridge._client.calls), calls_before,  # type: ignore[attr-defined]
            "/撤销 是本渠道命令,不该花一轮模型调用",
        )

    async def test_undo_with_an_id_also_works(self) -> None:
        transport = Collector()
        bridge = self.bridge(
            transport, tool_script=[[_note_add_call("按编号撤销")]], scripted=["记好了。"])
        thread = bridge.thread("wxid_me")
        await _drain(thread, _msg("记一下:按编号撤销", "wx:1"))
        receipt_id = self.assistant.execute(
            "SELECT MAX(id) FROM ops_receipts WHERE kind = 'journal.note'"
        ).fetchone()[0]
        events = await _drain(thread, _msg("/撤销 " + str(receipt_id), "wx:2"))
        self.assertIn("已撤销", render_for_chat(events))
        self.assertIsNotNone(self._notes()[0]["deleted_at"])

    # -- ③ 夹不存在 -------------------------------------------------------
    async def test_unknown_folder_is_reported_back_instead_of_guessed(self) -> None:
        transport = Collector()
        bridge = self.bridge(
            transport,
            tool_script=[[_note_add_call("第三章从雨夜写起", folder="写作/《归途》")]],
            scripted=["那个夹还没有,要不要新建?"],
        )
        await _drain(bridge.thread("wxid_me"), _msg(
            "记到写作/《归途》里:第三章从雨夜写起", "wx:1"))

        self.assertEqual(self._notes(), [], "夹不存在时**一条都不该写**")
        tool_messages = [
            message for message in bridge._client.calls[-1]  # type: ignore[attr-defined]
            if message.get("role") == "tool"
        ]
        self.assertTrue(tool_messages)
        payload = str(tool_messages[-1]["content"])
        self.assertIn("needs_folder", payload, "要把「该选哪个夹」交回模型,而不是替它猜")

    # -- ④ 自动召回 -------------------------------------------------------
    async def test_asking_about_an_old_note_puts_a_hint_in_front_of_the_model(self) -> None:
        transport = Collector()
        bridge = self.bridge(transport, scripted=["你 11-05 记的那条是这么说的……"])
        thread = bridge.thread("wxid_me")
        thread.session.notes.add(
            {"body": "今天反手还是拉不起来,球老往左飘。下次先练脚步。"}
        )

        await _drain(thread, _msg("我上次说反手拉不起来是怎么说的", "wx:1"))

        saw = "\n".join(
            str(message.get("content") or "")
            for message in bridge._client.calls[-1]  # type: ignore[attr-defined]
        )
        self.assertIn("<notes-hint>", saw, "每轮自动召回没生效")
        self.assertIn("反手还是拉不起来", saw)
        self.assertIn("#1", saw, "线索里必须带编号,模型才能接着 note_find(show)")


if __name__ == "__main__":
    unittest.main()
