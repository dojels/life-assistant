"""桥接层端到端验收(确定性、离线、可反复跑):

    .venv/Scripts/python.exe scripts/verify_bridge.py

它验证的是**"微信能不能接"这个问题里唯一有技术风险的部分**:
一条渠道消息进来,能不能真的把 Agent 跑完、把写入回执一起带出去、
把模型的追问接回来 —— 以及重复投递会不会记两遍账。

这些都与微信无关。微信适配器要做的只是 Inbound/SendAction 的搬运。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = tempfile.mkdtemp(prefix="la-verify-bridge-")

os.environ["LA_DATA_DIR"] = DATA_DIR
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["LA_LLM_PROVIDER"] = "fake"
sys.path.insert(0, str(BASE_DIR))

from la import config  # noqa: E402
from la.bridge import Bridge, Inbound, SendAction, render_for_chat  # noqa: E402
from la.llm.client import ToolCall  # noqa: E402
from la.llm.fake import FakeClient  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


class Collector:
    """测试用适配器:只把出站动作记下来。"""

    name = "collector"

    def __init__(self) -> None:
        self.actions: list[SendAction] = []
        self.typing_calls = 0

    async def send(self, action: SendAction) -> None:
        self.actions.append(action)

    async def typing(self, thread_id: str) -> None:
        self.typing_calls += 1


class ScriptedBridge(Bridge):
    """把 ScriptedBridge 造的 FakeClient 塞进每个线程。

    真实的 Bridge 用 settings.provider 决定客户端;验收要的是确定性脚本,
    所以这里覆盖掉 Thread 的客户端构造。**这层覆盖只存在于验收脚本里**,
    生产代码走 la/bridge/client.py。
    """

    def __init__(self, client: FakeClient, **kwargs) -> None:
        super().__init__(**kwargs)
        self._scripted = client

    def thread(self, thread_id: str):
        found = self._threads.get(thread_id)
        if found is None:
            import la.bridge.bridge as core

            original = core.build_client
            core.build_client = lambda settings: self._scripted
            try:
                found = super().thread(thread_id)
            finally:
                core.build_client = original
        return found


async def drain(thread, inbound: Inbound) -> list[dict]:
    return [event async for event in thread.handle(inbound)]


def msg(text: str, external_id: str) -> Inbound:
    return Inbound(channel="wechat", user_id="wxid_me", thread_id="wxid_me",
                   text=text, external_id=external_id)


async def main() -> int:
    print("验收数据目录: " + DATA_DIR)
    settings = config.get_settings(reload=True)
    print("provider = " + settings.provider + "(离线)")

    # ---------------------------------------------------------------- 1/5
    print("\n[1/6] 一条渠道消息 -> 一轮 Agent -> 回复回到渠道")
    client = FakeClient(scripted=["收到,今天午饭记好了。"])
    transport = Collector()
    bridge = ScriptedBridge(client, settings=settings, transport=transport)
    thread = bridge.thread("wxid_me")
    events = await drain(thread, msg("午饭 30 元", "wx:1"))
    check("产出了正文事件", any(e["event"] == "text" for e in events), str([e["event"] for e in events]))
    check("反问通道已接线(不是 unavailable_asker)", thread.session.asker is not None)
    check("调用过'正在输入'", transport.typing_calls >= 1, "次数=" + str(transport.typing_calls))
    check("用户原话进了事件日志",
          any(e.type == "user/message" and "午饭" in json.dumps(e.data, ensure_ascii=False)
              for e in thread.session.store.events("bridge:wxid_me")))

    # ---------------------------------------------------------------- 2/5
    print("\n[2/6] 微信回调重试:同一个 external_id 不能记两遍")
    again = await drain(thread, msg("午饭 30 元", "wx:1"))
    check("重复投递被识别为 duplicate",
          any(e["event"] == "duplicate" for e in again), str([e["event"] for e in again]))
    turns = [e for e in thread.session.store.events("bridge:wxid_me") if e.type == "turn/start"]
    check("只跑了一轮(没有第二次 turn/start)", len(turns) == 1, "轮数=" + str(len(turns)))

    # ---------------------------------------------------------------- 3/5
    print("\n[3/6] 模型真的调工具写账 -> 回执随回复一起发出去")
    client2 = FakeClient(
        tool_script=[[ToolCall(id="c1", name="expense_record",
                               arguments=json.dumps({"amount_cents": 2600, "payee": "出租车",
                                                     "category": "交通"}))]],
        scripted=["打车 26 元已经记上。"],
    )
    transport2 = Collector()
    bridge2 = ScriptedBridge(client2, settings=settings, transport=transport2)
    thread2 = bridge2.thread("wxid_me")
    events2 = await drain(thread2, msg("打车花了 26 元", "wx:2"))
    receipts = [e["data"] for e in events2 if e["event"] == "receipt"]
    check("有写入回执事件", bool(receipts), str(receipts))
    # 金额在分录腿(app_postings)上,交易本身只存"发生了什么"
    rows = thread2.session.ledger.conn.execute(
        "SELECT t.payee, p.amount_cents FROM app_txns t "
        "JOIN app_postings p ON p.txn_id = t.id").fetchall()
    check("账真的写进库了", any(abs(r[1]) == 2600 for r in rows), str(rows))
    body = render_for_chat(events2)
    check("渲染成一条聊天消息(正文 + 本轮写入)",
          "打车" in body and "本轮写入" in body, body.replace("\n", " / ")[:120])
    check("聊天消息里带了可撤销提示", "撤销" in body)

    # ---------------------------------------------------------------- 4/5
    print("\n[4/6] 模型反问 -> 用户在微信里回一条 -> 回答接回那个提问")
    asked: list[str] = []

    class AskingCollector(Collector):
        async def send(self, action: SendAction) -> None:
            asked.append(action.text)
            await super().send(action)

    client3 = FakeClient(
        tool_script=[[ToolCall(id="q1", name="ask_user",
                               arguments=json.dumps(
                                   {"prompt": "这笔算餐饮还是交通?",
                                    "options": [{"label": "餐饮"}, {"label": "交通"}]},
                                   ensure_ascii=False))]],
        scripted=["好,按你选的记。"],
    )
    transport3 = AskingCollector()
    bridge3 = ScriptedBridge(client3, settings=settings, transport=transport3)
    thread3 = bridge3.thread("wxid_me")

    task = asyncio.create_task(drain(thread3, msg("刚花了一笔", "wx:3")))
    for _ in range(200):  # 等提问真的发出去
        if asked:
            break
        await asyncio.sleep(0.02)
    check("问题被发到了渠道里", bool(asked) and "餐饮" in asked[0],
          (asked[0] if asked else "")[:80].replace("\n", " / "))
    answered = await drain(thread3, msg("2", "wx:4"))
    check("回答被识别为'回答问题',没有再起一轮对话",
          any(e["event"] == "answered" for e in answered), str([e["event"] for e in answered]))
    events3 = await task
    check("那一轮正常收尾", any(e["event"] for e in events3), str([e["event"] for e in events3]))
    answered_rows = thread3.session.ledger.conn.execute(
        "SELECT prompt, status, answer FROM ops_questions WHERE status = 'answered'").fetchall()
    check("回答落了库(与 Web/CLI 同一套)", len(answered_rows) >= 1,
          str(answered_rows)[:160])

    # ---------------------------------------------------------------- 5/5
    print("\n[5/6] 会话隔离 + 长消息截断")
    other = bridge3.thread("wxid_other")
    check("不同联系人 = 不同事件分片", other.session_id != thread3.session_id,
          thread3.session_id + " vs " + other.session_id)
    same = bridge3.thread("wxid_me")
    check("同一联系人复用同一个线程对象", same is thread3)
    check("投影里能看见上一轮(记忆没断)",
          len(thread3.session.conversation_history()) >= 2,
          "历史条数=" + str(len(thread3.session.conversation_history())))

    long_events = await drain(bridge3.thread("wxid_long"),
                              msg("x" * 5000, "wx:long"))
    check("超长消息被截断并提示",
          any(e["event"] == "notice" for e in long_events),
          str([e["event"] for e in long_events]))

    # ---------------------------------------------------------------- 6/6
    print("\n[6/6] 适配器派发:提问挂起时,回答照样能被处理(2026-10-13 真机那个自锁)")
    # ⚠️ 4/6 是**直接调 thread.handle**;真机上那句回答要先经过适配器的主循环。
    #    主循环若写成 `await self._handle_raw(raw)`,回答那条消息就永远排不到 ——
    #    "微信里确认了,毫无反应",一直挂到超时。这一条专门钉住那条路径。
    from la.agent.feedback import QuestionBroker, QuestionLog
    from la.bridge.adapters.weixin_ilink import ILinkAdapter
    from la.storage import db as _db

    asked6: list[str] = []

    class AskingCollector6(Collector):
        async def send(self, action: SendAction) -> None:
            asked6.append(action.text)
            await super().send(action)

    class NullClient:
        async def aclose(self) -> None:
            return None

    client6 = FakeClient(
        tool_script=[[ToolCall(id="q6", name="ask_user",
                               arguments=json.dumps(
                                   {"prompt": "这笔算餐饮还是交通?",
                                    "options": [{"label": "餐饮"}, {"label": "交通"}]},
                                   ensure_ascii=False))]],
        scripted=["按你选的记好了。"],
    )
    transport6 = AskingCollector6()
    bridge6 = ScriptedBridge(client6, settings=settings, transport=transport6)
    thread6 = bridge6.thread("wxid_me")

    adapter = ILinkAdapter(bridge=bridge6, client=NullClient(), log=lambda line: None)
    produced: dict[str, list] = {}

    async def fake_handle(raw: dict) -> list:
        key = str(raw.get("id"))
        produced[key] = await drain(thread6, msg(str(raw.get("text")), "wx:" + key))
        return produced[key]

    adapter._handle_raw = fake_handle          # type: ignore[assignment]
    adapter._dispatch({"text": "刚花了一笔", "id": 61})
    for _ in range(250):
        if asked6:
            break
        await asyncio.sleep(0.02)
    check("问题被发到了渠道里", bool(asked6),
          (asked6[0] if asked6 else "")[:60].replace("\n", " / "))
    check("派发没有卡在主循环里(任务真的在跑)", bool(adapter._tasks), "在跑 " + str(len(adapter._tasks)))
    # 关键:提问还挂着,回答那条消息也要能被派发出去(串行 await 时它永远轮不到)
    adapter._dispatch({"text": "2", "id": 62})
    await asyncio.wait_for(
        asyncio.gather(*list(adapter._tasks), return_exceptions=True), timeout=10.0
    )
    check("回答那条消息被派发并识别为'回答问题'",
          any(e["event"] == "answered" for e in produced.get("62", [])),
          str([e["event"] for e in produced.get("62", [])]))
    check("那一轮在回答到达后正常收尾",
          "按你选的记好了。" in render_for_chat(produced.get("61", [])),
          render_for_chat(produced.get("61", []))[:80].replace("\n", " / "))

    # 跨进程:用户在**网页**上答的(只改共享的 ops_questions)也要被认领
    client7 = FakeClient(
        tool_script=[[ToolCall(id="q7", name="ask_user",
                               arguments=json.dumps({"prompt": "算餐饮还是交通?",
                                                     "options": [{"label": "餐饮"},
                                                                 {"label": "交通"}]},
                                                    ensure_ascii=False))]],
        scripted=["好,按网页上选的记。"],
    )
    bridge7 = ScriptedBridge(client7, settings=settings, transport=Collector())
    thread7 = bridge7.thread("wxid_me")
    turn7 = asyncio.create_task(drain(thread7, msg("刚花了一笔", "wx:71")))
    pending_id = ""
    for _ in range(250):
        row = thread7.question_log.conn.execute(
            "SELECT id FROM ops_questions WHERE status = 'pending' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            pending_id = str(row[0])
            break
        await asyncio.sleep(0.02)
    check("提问落库了(网页才看得见)", bool(pending_id), pending_id)
    web_conn = _db.connect_assistant()
    QuestionBroker().answer(pending_id, selected=["餐饮"], by="user",
                            log=QuestionLog(web_conn))
    events7 = await asyncio.wait_for(turn7, timeout=8.0)
    check("网页上答的,桥接那一轮也接着做了",
          "好,按网页上选的记。" in render_for_chat(events7),
          render_for_chat(events7)[:80].replace("\n", " / "))
    web_conn.close()

    for item in (bridge, bridge2, bridge3, bridge6, bridge7):
        item.close()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
