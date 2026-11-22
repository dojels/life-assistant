"""轨迹(D118):**落下来的就是你发出去的**,而且页面说的与装配器做的一致。

用户 2026-10-13 的原话:*"我希望在个人生活助手界面上加一个【轨迹】的菜单,在这里面,
可以看到当前上下文的装配规则,并能显示上一次对话的完整上下文。"*

⚠️ 这一组测试守四件事,每一件都对着一种真实的翻车方式:

  1. **落下来的 == 发出去的** —— 记录里的 messages 与 projection.messages **逐字相同**。
     只要中间有一道"加工"(截断、重排、重算),这一页就从证据变成宣传;
  2. **页面说的 == 代码做的** —— 规则表里的段必须与 Projector 实际产出的段完全一致、
     预算必须等于 config 里的值。一张抄错的规则表比没有规则表更坏:它会让人以为核对过了;
  3. **写不进去也不影响对话** —— 轨迹是观测,不是主流程(磁盘满、目录被占都不该让它答不出来);
  4. **脱敏不退化** —— 这一页显示的是真实上下文,所以敏感事实在这里**仍然只是占位符**。
     "完整"指的是没有第二个版本,不是把省略掉的信息补回来。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_context_trace -v
"""

from __future__ import annotations

import asyncio
import html
import json
import shutil
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from la import config
from la.agent import prompts
from la.agent.context_trace import (
    MAX_RECORDS,
    read_records,
    record_context,
    trace_path,
)
from la.agent.session import MemorySession
from la.llm.fake import FakeClient
from la.memory.projector import (
    SEGMENT_CHECKPOINT,
    SEGMENT_CONVERSATION,
    SEGMENT_EVIDENCE,
    SEGMENT_MEMORY,
    SEGMENT_RUNTIME,
    SEGMENT_SYSTEM,
    SEGMENT_TOOLS,
    Projector,
    build_memory_block,
)
from la.memory.store import MemoryStore
from la.storage import db
from la.web.trace_view import (
    CONVERSATION_RULES,
    MEMORY_BLOCK_STEPS,
    ONLY_ALL,
    ONLY_SYSTEM,
    ONLY_TALK,
    SEGMENT_BUDGET_SUM,
    SEGMENT_ORDER,
    SEGMENT_RULES,
    TRACE_TABS,
    TRACE_TAB_CONTEXT,
    TRACE_TAB_HISTORY,
    TRACE_TAB_REPORT,
    TRACE_TAB_RULES,
    build_trace_view,
    build_turn,
)

BASE_DIR = Path(__file__).resolve().parent.parent

#: 临时根目录。⚠️ 刻意不用 tempfile.TemporaryDirectory():它以 0o700 建目录,
#: 受限文件系统(带文件沙箱的环境)会因此拒绝后续写入 —— 与 test_persona_edit 同一个理由。
TMP_ROOT = BASE_DIR / ".tmp-trace-tests"

DATA_PATHS = (
    "DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "BACKUPS_DIR",
    "ASSISTANT_DB", "MEMORY_DB",
)


def temp_dir(name: str) -> Path:
    path = TMP_ROOT / (name + "-" + uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    return path


class TempDataCase:
    """把数据目录整体搬到临时目录 —— 真实的 data/ 一个字都不许动。

    ⚠️ 刻意做成 **mixin**(不继承 TestCase):同步与异步两组测试都要用它,
    而 `(TempDataCase, IsolatedAsyncioTestCase)` 这种多继承的 MRO 是不合法的。
    """

    def setUp(self) -> None:
        self.root = temp_dir(self.__class__.__name__)
        self._orig = {key: getattr(config, key) for key in DATA_PATHS}
        config.DATA_DIR = self.root
        config.ARCHIVE_DIR = self.root / "archive"
        config.MEDIA_DIR = self.root / "media"
        config.BACKUPS_DIR = self.root / "backups"
        config.ASSISTANT_DB = self.root / "assistant.db"
        config.MEMORY_DB = self.root / "memory.db"

    def tearDown(self) -> None:
        for key, value in self._orig.items():
            setattr(config, key, value)
        shutil.rmtree(self.root, ignore_errors=True)


class SyncCase(TempDataCase, unittest.TestCase):
    """同步用例的基类(继承顺序:先把数据目录搬走,再进 TestCase)。"""


class AsyncCase(TempDataCase, unittest.IsolatedAsyncioTestCase):
    """异步用例的基类。"""


def make_settings(session_id: str = "web") -> config.Settings:
    """离线(fake)设置:轨迹页与落盘层都不该依赖真模型。"""
    import dataclasses

    return dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id=session_id
    )


# ============================================================ 落盘层
class TraceStoreCase(SyncCase):
    """一条记录本身:逐字、自描述、坏行不致命、写不进去也不抛。"""

    def setUp(self) -> None:
        super().setUp()
        self.projector = Projector(make_settings())
        self.path = self.root / "trace" / "context.jsonl"

    def projection(self, **kwargs):
        params = dict(
            system_text="你是「家庭助手」的「小默」。",
            runtime_text="<runtime-context>当前时间: 2026-10-13</runtime-context>",
            memory_text="<memory><facts>- 用户喝拿铁</facts></memory>",
            history=[
                {"role": "user", "content": "今天做了什么"},
                {"role": "assistant", "content": "没做什么。"},
            ],
            evidence_text="",
            tools_tokens=2600,
        )
        params.update(kwargs)
        return self.projector.build(**params)

    def record(self, projection=None, **kwargs):
        params = dict(session_id="web", turn=1, surface="web",
                      provider="fake", model="deepseek-flash")
        params.update(kwargs)
        return record_context(projection or self.projection(), path=self.path, **params)

    # -- 逐字 ------------------------------------------------------------
    def test_recorded_messages_are_verbatim(self) -> None:
        """⭐ 这一条是整页的地基:记录里的正文必须与 projection.messages **一字不差**。"""
        projection = self.projection()
        record = self.record(projection)
        self.assertIsNotNone(record)
        self.assertEqual(
            [item["content"] for item in record["messages"]],
            [str(message["content"]) for message in projection.messages],
        )
        self.assertEqual(
            [item["role"] for item in record["messages"]],
            [str(message["role"]) for message in projection.messages],
        )

    def test_message_segments_come_from_the_projector(self) -> None:
        """段名由装配器给(Projection.message_segments)—— 页面不猜顺序。"""
        projection = self.projection()
        record = self.record(projection)
        self.assertEqual(
            [item["segment"] for item in record["messages"]],
            list(projection.message_segments),
        )
        self.assertEqual(record["messages"][0]["segment"], SEGMENT_SYSTEM)
        # ⭐ D122:每轮都变的那三段(runtime / memory / evidence)一律排在**全部历史之后** ——
        #    这就是前缀缓存能吃到整段历史的全部理由(见 scripts/verify_prefix_cache.py)。
        volatile = (SEGMENT_RUNTIME, SEGMENT_MEMORY, SEGMENT_EVIDENCE)
        segments = [item["segment"] for item in record["messages"]]
        volatile_at = [i for i, key in enumerate(segments) if key in volatile]
        conversation_at = [i for i, key in enumerate(segments) if key == SEGMENT_CONVERSATION]
        self.assertTrue(volatile_at, "这三段该在(运行时 / 记忆 / 证据)")
        self.assertGreater(
            min(volatile_at), max(conversation_at), "易变段必须排在历史之后"
        )

    def test_the_current_user_message_stays_last(self) -> None:
        """⭐ D122:易变段夹在历史与**这一句 user**之间 —— 模型最后看到的仍然是"该回谁"。"""
        projection = self.projection(history=[
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二句"},
        ])
        messages = projection.messages
        self.assertEqual(projection.message_segments[0], SEGMENT_SYSTEM)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user", "最后一条必须是要回的那一句")
        self.assertEqual(messages[-1]["content"], "第二句")
        self.assertEqual([m["role"] for m in messages],
                         ["system", "user", "assistant", "system", "system", "user"])
        # 下一轮能命中的前缀 = 稳定系统提示 + 除最后一句 user 之外的历史
        self.assertEqual(
            projection.cache_prefix_tokens,
            config.estimate_tokens(messages[0]["content"])
            + config.estimate_messages_tokens(messages[1:3]),
        )

    def test_record_is_self_describing(self) -> None:
        record = self.record(turn=7)
        for key in ("at", "session", "turn", "surface", "provider", "model",
                    "segments", "total", "tools", "grand_total", "messages"):
            self.assertIn(key, record, "记录缺字段: " + key)
        self.assertEqual(record["turn"], 7)
        self.assertEqual(record["surface"], "web")
        self.assertEqual(record["tools"], 2600)
        self.assertEqual(record["grand_total"], record["total"] + 2600)
        # 段账是装配器报的那一份,页面不重算
        self.assertIn(SEGMENT_SYSTEM, record["segments"])

    def test_pre_clip_and_parts_are_recorded(self) -> None:
        """裁剪前的段大小要留下来 —— 否则"这一段被截尾了"在页面上看不出来。"""
        detail = {
            "parts": [{"key": "identity", "name": "身份与边界", "chars": 10, "tokens": 7}],
            "pre_clip": {"system": 9, "runtime": 12, "memory": 40, "evidence": 0},
            "persona_custom": True,
            "persona_chars": 128,
        }
        record = self.record(detail=detail)
        self.assertEqual(record["pre_clip"]["memory"], 40)
        self.assertEqual(record["parts"][0]["name"], "身份与边界")
        self.assertTrue(record["persona_custom"])
        self.assertEqual(record["persona_chars"], 128)

    def test_confidential_facts_stay_placeholders(self) -> None:
        """⭐ 轨迹页显示的是真实上下文 —— 所以它**继承** L4 的脱敏口径,不是绕开它。"""
        secret = "用户的身份证号是 110101199001011234"
        block = build_memory_block(
            facts=[SimpleNamespace(id=9, content=secret, confidential=True)],
            summaries=[], status="hit", now_text="",
        )
        self.assertNotIn(secret, block, "记忆块本身就该只留占位符")
        record = self.record(self.projection(memory_text=block))
        joined = " ".join(item["content"] for item in record["messages"])
        self.assertNotIn(secret, joined, "轨迹里不许出现敏感事实原文")
        self.assertIn(config.SECRET_PLACEHOLDER, joined)

    # -- 读 --------------------------------------------------------------
    def test_corrupt_lines_are_skipped(self) -> None:
        self.record(turn=1)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("这不是 json\n")
            handle.write("\n")
        self.record(turn=2)
        records = read_records(path=self.path)
        self.assertEqual([item["turn"] for item in records], [2, 1], "新的要在前")

    def test_missing_file_reads_as_empty(self) -> None:
        self.assertEqual(read_records(path=self.root / "trace" / "nope.jsonl"), [])

    def test_prune_keeps_the_newest(self) -> None:
        for index in range(6):
            self.record(turn=index + 1, max_records=3)
        records = read_records(path=self.path)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["turn"], 6)
        self.assertEqual(records[-1]["turn"], 4)

    def test_write_failure_returns_none_instead_of_raising(self) -> None:
        blocked = self.root / "a-file"
        blocked.write_text("我不是目录", encoding="utf-8")
        result = record_context(
            self.projection(), session_id="web", turn=1,
            path=blocked / "context.jsonl",
        )
        self.assertIsNone(result, "写不进去要如实返回 None,而不是抛异常")

    def test_trace_path_follows_the_data_dir(self) -> None:
        """⭐ 目录**现算**:测试与验收脚本靠改 config.DATA_DIR 隔离,常量会漏。"""
        self.assertTrue(str(trace_path()).startswith(str(self.root)))


# ============================================================ 真链路:一轮对话
class TraceSessionCase(AsyncCase):
    """一轮 respond 之后,轨迹里就是那一轮**真正发出去**的东西。"""

    def setUp(self) -> None:
        super().setUp()
        self.assistant_conn = db.connect_assistant()
        self.memory_conn = db.connect_memory()
        self.session = MemorySession(
            FakeClient(), make_settings(),
            store=MemoryStore(self.memory_conn, archive_dir=config.ARCHIVE_DIR),
            session_id="web", assistant_conn=self.assistant_conn,
            auto_approve=True, auto_briefing=False, surface="web",
        )
        self.session.start()

    def tearDown(self) -> None:
        for conn in (self.assistant_conn, self.memory_conn):
            try:
                conn.close()
            except Exception:
                pass
        super().tearDown()

    def records(self):
        return read_records(path=trace_path())

    async def test_one_turn_records_exactly_what_was_sent(self) -> None:
        await self.session.respond("今天天气怎么样")
        records = self.records()
        self.assertEqual(len(records), 1, "一轮只该落一条")
        projection = self.session.last_projection
        self.assertIsNotNone(projection)
        self.assertEqual(
            [item["content"] for item in records[0]["messages"]],
            [str(message["content"]) for message in projection.messages],
        )
        self.assertIn("今天天气怎么样", records[0]["messages"][-1]["content"])

    async def test_surface_turn_and_segments_are_recorded(self) -> None:
        await self.session.respond("你好")
        record = self.records()[0]
        self.assertEqual(record["surface"], "web")
        self.assertEqual(record["turn"], 1)
        self.assertEqual(record["provider"], "fake")
        self.assertIn(SEGMENT_SYSTEM, record["segments"])
        self.assertIn(SEGMENT_CONVERSATION, record["segments"])
        self.assertIn(SEGMENT_TOOLS, record["segments"])
        # 系统提示由哪几段拼成 —— 明细也要落下来(页面拿它标截尾)
        keys = [part["key"] for part in record.get("parts") or []]
        self.assertEqual(keys, ["identity", "persona", "catalog", "runtime"])

    async def test_second_turn_appends_and_the_page_defaults_to_the_newest(self) -> None:
        await self.session.respond("第一句")
        await self.session.respond("第二句")
        records = self.records()
        self.assertEqual(len(records), 2)
        view = build_trace_view(
            make_settings(), assistant_conn=self.assistant_conn,
            memory_conn=self.memory_conn, path=trace_path(),
        )
        self.assertEqual(view.index, 0)
        self.assertEqual(view.turn.turn, 2, "默认看最新那一轮")
        self.assertIn("第二句", view.turn.messages[-1].content)
        # 往前翻一轮:索引 1 是上一轮
        older = build_trace_view(
            make_settings(), assistant_conn=self.assistant_conn,
            memory_conn=self.memory_conn, index=1, path=trace_path(),
        )
        self.assertEqual(older.turn.turn, 1)
        self.assertIn("第一句", older.turn.messages[-1].content)

    async def test_the_page_shows_the_system_prompt_that_was_sent(self) -> None:
        await self.session.respond("你好")
        view = build_trace_view(
            make_settings(), assistant_conn=self.assistant_conn,
            memory_conn=self.memory_conn, path=trace_path(),
        )
        system = view.turn.messages[0].content
        self.assertIn(prompts.IDENTITY[:12], system)
        self.assertIn(prompts.PERSONA_TAIL, system, "固定尾巴必须真的在里面")
        # ⭐ D122:时间戳这类每轮都变的东西**不再**拼在系统提示里 —— 它是历史之后的一条
        #    system 快照。稳定段保持逐字节不变,跨轮前缀缓存才吃得长。
        self.assertNotIn("<runtime-context>", system, "稳定段里不该再掺易变内容")
        snapshots = [
            item for item in view.turn.messages
            if item.role == "system" and item.segment == SEGMENT_RUNTIME
        ]
        self.assertEqual(len(snapshots), 1, "运行时上下文该是历史里的一条 system 快照")
        self.assertIn("<runtime-context>", snapshots[0].content)
        self.assertEqual(view.turn.messages[-1].role, "user", "最后一条永远是这一句 user")

    async def test_a_failed_trace_write_does_not_break_the_turn(self) -> None:
        """⭐ 轨迹是观测,不是主流程:写不进去也得照样答出来。"""
        blocked = self.root / "not-a-dir"
        blocked.write_text("x", encoding="utf-8")
        target = blocked / "context.jsonl"
        # ⚠️ 补在 **session** 这一侧:它 import 的是函数本身(from … import trace_path_for),
        #    补在 context_trace 模块上对它没有影响。
        with mock.patch("la.agent.session.trace_path_for", return_value=target):
            result = await self.session.respond("你好")
        self.assertIsNotNone(result, "轨迹写不进去,这一轮也必须答出来")
        self.assertFalse(target.exists())
        self.assertEqual(self.records(), [], "没有落成任何东西")

    async def test_the_trace_follows_the_store_not_config_data_dir(self) -> None:
        """⭐ 实测踩过的坑:tests/test_m1 用**临时库 + 临时归档**跑二十轮对话,而 config.DATA_DIR
        还指着真实目录 —— 于是真实的 `data/trace` 里长出了 40 条测试对话(agent 里那句注释就是它)。
        轨迹必须**跟着库走**:库在临时目录,轨迹就在临时目录,与 config.DATA_DIR 无关。"""
        elsewhere = self.root / "elsewhere"
        session = MemorySession(
            FakeClient(), make_settings(),
            store=MemoryStore(self.memory_conn, archive_dir=elsewhere / "archive"),
            session_id="web", assistant_conn=self.assistant_conn,
            auto_approve=True, auto_briefing=False, surface="web",
        )
        session.start()
        with mock.patch.object(config, "DATA_DIR", self.root / "not-here"):
            await session.respond("你好")
        self.assertTrue(
            (elsewhere / "trace" / "context.jsonl").exists(),
            "轨迹要落在**库所在的那个数据目录**里",
        )
        self.assertFalse(
            (self.root / "not-here" / "trace").exists(),
            "不能因为 config.DATA_DIR 指到别处,就把轨迹写到那儿去",
        )
        self.assertEqual(read_records(path=elsewhere / "trace" / "context.jsonl").__len__(), 1)

    async def test_a_turn_writes_into_the_isolated_data_dir(self) -> None:
        await self.session.respond("你好")
        self.assertTrue(str(trace_path()).startswith(str(self.root)))
        self.assertEqual(len(self.records()), 1)


# ============================================================ 视图:规则与截尾
class TraceViewCase(SyncCase):
    """页面模型:截尾看得见、动态预算如实报、缺字段不炸。"""

    def setUp(self) -> None:
        super().setUp()
        self.projector = Projector(make_settings())
        self.path = self.root / "trace" / "context.jsonl"

    def test_clipping_is_visible(self) -> None:
        system_text = "边" * 40000              # 远超 BUDGET_SYSTEM(2500)
        runtime_text = "<runtime-context>ok</runtime-context>"
        projection = self.projector.build(
            system_text=system_text,
            runtime_text=runtime_text,
            memory_text="<memory></memory>",
            history=[],
            tools_tokens=100,
        )
        # 裁剪发生在 projector 里 —— 所以"裁剪前多大"只有装配方(session)知道,
        # 由它随记录一起报上来。这里就按那条真链路走。
        record_context(
            projection, session_id="web", turn=1, path=self.path,
            detail={"pre_clip": {
                "system": config.estimate_tokens(system_text),
                "runtime": config.estimate_tokens(runtime_text),
                "memory": 4, "evidence": 0,
            }},
        )
        turn = build_turn(read_records(path=self.path)[0])
        system = next(item for item in turn.segments if item.key == SEGMENT_SYSTEM)
        self.assertTrue(system.clipped, "被截尾的段必须标出来")
        self.assertGreater(system.pre_clip, system.actual)
        self.assertTrue(any("截尾" in note for note in turn.notes), turn.notes)

    def test_conversation_budget_reports_the_tightened_value(self) -> None:
        """⭐ 硬上限把对话段压到多少,页面上就得说多少 —— 拿默认值去比是说假话。

        ⚠️ D121 重定标之后,五段上限之和(30700)已经装得进硬上限(40000),正常装配**不会**
           被收 —— 所以这里先把硬上限临时压小,专门验"收过了就必须如实报"这条机制。
           (旧数字下正好相反:硬上限 10000 被五段之和 10700 顶破,每一轮都收。)
        """
        # ⚠️ D133:硬上限现在要**先扣掉工具预留额**(BUDGET_TOOLS)才轮到对话段,
        #    所以这里把假硬上限设成"工具预留 + 8,000",让固定段仍然挤得满。
        original = config.BUDGET_HARD_MAX
        config.BUDGET_HARD_MAX = config.BUDGET_TOOLS + 8000
        try:
            projection = self.projector.build(
                system_text="身" * 40000,
                runtime_text="运" * 4000,
                memory_text="记" * 40000,
                history=[{"role": "user", "content": "问题" * 2000}],
                evidence_text="证据" * 20000,
                tools_tokens=100,
            )
        finally:
            config.BUDGET_HARD_MAX = original
        self.assertTrue(projection.capped, "这一段测试前提:硬上限应该生效")
        record_context(projection, session_id="web", turn=1, path=self.path)
        turn = build_turn(read_records(path=self.path)[0])
        conversation = next(
            item for item in turn.segments if item.key == SEGMENT_CONVERSATION
        )
        self.assertLess(conversation.budget, config.BUDGET_CONVERSATION)
        self.assertEqual(conversation.budget, projection.conversation_budget)

    def test_view_is_read_only(self) -> None:
        projection = self.projector.build(
            system_text="a", runtime_text="b", memory_text="c", history=[],
        )
        record_context(projection, session_id="web", turn=1, path=self.path)
        before = self.path.read_bytes()
        view = build_trace_view(
            make_settings(), index=0, now_text="2026-10-13 21:00", path=self.path
        )
        self.assertIsNotNone(view.turn)
        self.assertEqual(self.path.read_bytes(), before, "看一眼不该改变任何东西")
        self.assertEqual(sorted(item.name for item in self.path.parent.iterdir()),
                         ["context.jsonl"])

    def test_a_record_missing_optional_fields_still_renders(self) -> None:
        line = json.dumps(
            {"session": "web", "turn": 3, "messages": [
                {"role": "user", "content": "只有最少字段", "tokens": 9}
            ]},
            ensure_ascii=False,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(line + "\n", encoding="utf-8")
        view = build_trace_view(make_settings(), path=self.path)
        self.assertEqual(view.turn.turn, 3)
        self.assertEqual(view.turn.segments[0].actual, 0)
        self.assertEqual(view.turn.messages[0].preview, "只有最少字段")

    def test_the_persona_line_on_the_rules_tab_shows_the_name(self) -> None:
        """D145:「现在跑在什么上面」那一栏要写清**现在她叫什么**,不只写"你自己写的"。"""
        from la.agent.prompts import PERSONA_NAME

        view = build_trace_view(make_settings(), path=self.path)
        self.assertIn(PERSONA_NAME, view.current["persona"])
        self.assertIn("出厂默认", view.current["persona"])

    def test_assistant_role_label_follows_the_stored_name(self) -> None:
        """D144:名字成了数据 —— 轨迹页那一栏显示的是**现在**她叫什么,不是代码里的常量。"""
        from la.agent.prompts import PERSONA_NAME

        record = {"messages": [{"role": "assistant", "content": "在呢", "tokens": 4}]}
        self.assertEqual(build_turn(record).messages[0].role_label, PERSONA_NAME)
        named = build_turn(record, assistant_name="阿豆")
        self.assertEqual(named.messages[0].role_label, "阿豆")
        self.assertEqual(named.messages[0].role, "assistant")

    def test_an_unknown_segment_is_still_shown(self) -> None:
        """⭐ 记录里多出一段规则表不认识的段(新版本写的)时**照原样列出来** ——
        静默丢数据是这一页最坏的失败:它会让人以为"上下文就这些"。"""
        line = json.dumps(
            {
                "session": "web", "turn": 1, "messages": [{"role": "user", "content": "x"}],
                "segments": {"system": 10, "brand-new": 42},
            },
            ensure_ascii=False,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(line + "\n", encoding="utf-8")
        turn = build_turn(read_records(path=self.path)[0])
        extra = next(item for item in turn.segments if item.key == "brand-new")
        self.assertEqual(extra.actual, 42)
        self.assertEqual(extra.budget, 0, "没有预算可比时要如实说,不编一个")

    def test_bad_index_never_breaks_the_page(self) -> None:
        """认不出来的看最新那条,越界的夹到边界 —— 手打链接、旧书签都不该打不开。"""
        for turn in (1, 2, 3):
            projection = self.projector.build(
                system_text="a", runtime_text="b", memory_text="", history=[],
            )
            record_context(
                projection, session_id="web", turn=turn, path=self.path
            )
        for value in ("abc", "", "-5", None, "0"):
            view = build_trace_view(make_settings(), index=value, path=self.path)
            self.assertEqual(view.index, 0, "认不出来的索引要看最新那条: " + str(value))
        # 越界(比记录还多)夹到最旧的那一条,而不是空白
        beyond = build_trace_view(make_settings(), index="99", path=self.path)
        self.assertEqual(beyond.index, 2)
        self.assertEqual(beyond.turn.turn, 1)

    def test_empty_store_says_how_to_fill_it(self) -> None:
        view = build_trace_view(make_settings(), path=self.path)
        self.assertIsNone(view.turn)
        self.assertFalse(view.trace_exists)
        self.assertIn("trace", view.trace_file)
        # ⭐ 一轮都没有的时候,页签照样要在:规则那一页本来就与有没有聊过无关
        # ⭐ D136 起多一个入口「使用报表」—— 它排在最后,而且**不带计数**
        #    (报表是时间的账,不是某一轮的条数)。
        self.assertEqual(
            [item["key"] for item in view.tabs], [*TRACE_TABS, TRACE_TAB_REPORT]
        )
        self.assertEqual([item["count"] for item in view.tabs][-1], 0)
        self.assertEqual(view.tab, TRACE_TAB_CONTEXT)

    # -- 占用图(D119)----------------------------------------------------
    def _one_turn(self, **kwargs):
        params = dict(
            system_text="身份" * 200, runtime_text="<runtime-context>ok</runtime-context>",
            memory_text="<memory><facts>- 一条</facts></memory>",
            history=[{"role": "user", "content": "你好"}],
            evidence_text="", tools_tokens=config.BUDGET_TOOLS,
        )
        params.update(kwargs)
        record_context(self.projector.build(**params), session_id="web", turn=1, path=self.path)
        return build_turn(read_records(path=self.path)[0])

    def test_stack_scales_to_this_turn_not_to_the_ceiling(self) -> None:
        """⭐ 一张图要能回答"谁占了大头":刻度跟着**这一轮实际大小**走。

        ⚠️ D121 起不再拿硬上限当分母 —— 一条两百来 token 的投影放到 40000 的刻度上
           只剩一根细线,"谁占了大头"这件事反而看不见了(而那正是这张图存在的理由)。
        ⚠️ 三条线**落在刻度内才画**:远没到线的时候,把线画在右边缘会被读成"快满了"。
        """
        turn = self._one_turn()
        stack = turn.stack
        self.assertGreaterEqual(stack.limit, turn.total, "刻度得装得下这一轮")
        self.assertLessEqual(stack.limit, max(int(turn.total * 1.1), 1), "右边不该空一大片")
        # 每一块之和 == 总的已用比例(图不撒谎:拼起来就是那个数)
        self.assertAlmostEqual(
            sum(item.percent for item in stack.slices), stack.used_percent, places=3
        )
        trigger = config.BUDGET_COMPACT_TRIGGER
        self.assertLess(turn.total, trigger, "前提:这一轮远没到触发线")
        self.assertEqual(stack.marks, [], "没到线的时候不该画线")
        self.assertIn("在刻度之外", stack.note, "刻度外也要说清离触发线还有多远")
        self.assertFalse(stack.over)
        # 工具 schema 不进这张图(它由 API 的 tools 参数单独发出)
        self.assertNotIn(SEGMENT_TOOLS, [item.key for item in stack.slices])

    def test_stack_draws_the_trigger_when_it_is_really_crossed(self) -> None:
        """越过触发线时,线必须画出来,而且位置与数值一致 —— 图是给人判"该压缩了"看的。"""
        trigger = config.BUDGET_COMPACT_TRIGGER
        turn = self._one_turn(
            history=[{"role": "user", "content": "问" * (trigger * 4)}]
        )
        stack = turn.stack
        self.assertGreaterEqual(turn.total, trigger, "前提:这一轮越过了触发线")
        marks = {item["key"]: item for item in stack.marks}
        self.assertIn("trigger", marks)
        self.assertEqual(marks["trigger"]["at"], trigger)
        # 刻度线的位置 = 它的值 / 同一把刻度 —— 与图上那根线对得上
        self.assertAlmostEqual(
            marks["trigger"]["percent"], trigger / stack.limit * 100, places=3,
        )
        self.assertIn("已越过压缩触发线", stack.note)

    def test_tool_scale_has_its_own_scale_and_flags_over_budget(self) -> None:
        """⭐ 两条刻度是**分开**的:共用一条的话,投影那一小截会细成一根线,
        "工具比上下文还大"这件事反而看不见了。"""
        turn = self._one_turn(tools_tokens=config.BUDGET_TOOLS * 3)
        self.assertTrue(turn.tool_scale.over)
        self.assertEqual(turn.tool_scale.limit, config.BUDGET_TOOLS * 3)
        self.assertEqual(turn.tool_scale.marks[0]["at"], config.BUDGET_TOOLS)
        self.assertEqual([item.key for item in turn.tool_scale.slices], [SEGMENT_TOOLS])

    def test_segments_with_nothing_in_them_are_not_drawn(self) -> None:
        """占 0 的段不进图 —— 图是给"看得见"用的,空块只会添噪。"""
        turn = self._one_turn(evidence_text="")
        self.assertNotIn(SEGMENT_EVIDENCE, [item.key for item in turn.stack.slices])

    # -- 过滤器与服务端页签 -----------------------------------------------
    def test_filters_split_the_messages_by_role(self) -> None:
        self._one_turn()
        everything = build_trace_view(make_settings(), path=self.path)
        talk = build_trace_view(make_settings(), only=ONLY_TALK, path=self.path)
        system = build_trace_view(make_settings(), only=ONLY_SYSTEM, path=self.path)
        self.assertEqual(len(everything.messages), len(everything.turn.messages))
        self.assertTrue(all(m.role in ("user", "assistant") for m in talk.messages))
        self.assertTrue(all(m.role == "system" for m in system.messages))
        # 两条加起来 = 全部(没有消息会从过滤器里漏掉)
        self.assertEqual(len(talk.messages) + len(system.messages), len(everything.messages))

    def test_unknown_tab_and_filter_fall_back_instead_of_failing(self) -> None:
        self._one_turn()
        view = build_trace_view(make_settings(), tab="nope", only="nope", path=self.path)
        self.assertEqual(view.tab, TRACE_TAB_CONTEXT)
        self.assertEqual(view.only, ONLY_ALL)

    def test_tabs_carry_counts_and_keep_the_current_turn(self) -> None:
        """⭐ 切页签要**保留正在看的那一轮与过滤器** —— 否则"翻到上一轮 → 看规则 →
        切回上下文"会莫名其妙跳回最新那一轮。"""
        for turn in (1, 2):
            record_context(
                self.projector.build(
                    system_text="a", runtime_text="b", memory_text="", history=[],
                ),
                session_id="web", turn=turn, path=self.path,
            )
        view = build_trace_view(
            make_settings(), index=1, only=ONLY_TALK, path=self.path
        )
        tabs = {item["key"]: item for item in view.tabs}
        self.assertEqual(tabs[TRACE_TAB_RULES]["count"], len(SEGMENT_RULES))
        self.assertEqual(tabs[TRACE_TAB_HISTORY]["count"], 2)
        self.assertEqual(tabs[TRACE_TAB_CONTEXT]["count"], len(view.turn.messages))
        self.assertIn("i=1", tabs[TRACE_TAB_CONTEXT]["href"])
        self.assertIn("only=talk", tabs[TRACE_TAB_CONTEXT]["href"])
        self.assertTrue(tabs[TRACE_TAB_CONTEXT]["on"])
        # 规则 / 历史两页不背这些参数(它们与"看哪一轮"无关)
        self.assertEqual(tabs[TRACE_TAB_RULES]["href"], "/trace?tab=rules")
        self.assertEqual(tabs[TRACE_TAB_HISTORY]["href"], "/trace?tab=history")


# ============================================================ 规则表:页面 == 代码
class TraceRulesCase(unittest.TestCase):
    """⭐ 一张抄错的规则表比没有规则表更坏 —— 所以逐条与实现对照。"""

    def setUp(self) -> None:
        self.projector = Projector(make_settings())

    def test_rule_table_covers_exactly_the_segments_the_projector_emits(self) -> None:
        projection = self.projector.build(
            system_text="身份",
            runtime_text="运行时",
            memory_text="<memory><facts>- 一条</facts></memory>",
            history=[{"role": "user", "content": "你好"}],
            evidence_text="证据块",
            tools_tokens=2600,
        )
        self.assertEqual(
            {rule.key for rule in SEGMENT_RULES},
            set(projection.segments),
            "页面列出的段与装配器实际产出的段对不上",
        )
        self.assertEqual(
            tuple(rule.key for rule in SEGMENT_RULES), SEGMENT_ORDER,
            "顺序也要一致(它就是页面上的显示顺序)",
        )

    def test_display_order_matches_the_wire_order(self) -> None:
        """⭐ 用户 2026-10-16 点名:这几段的先后要按**真实的一次请求**排。

        实测出来的形状是:system 消息 → **tools 字段** → 历史(对话)→
        runtime / memory / evidence 三条快照 → 最后那句 user。
        所以「工具 schema」排在**第二位**——它比整段历史都先给到模型,
        原来的写法把它排在最后(`order="—"`),读起来像"某种收尾的附加物"。

        ⚠️ 这条与"消息列表里的装配顺序"(D122:易变段推到历史之后)是**两件事**:
           前者是模型遇到的先后,后者是投影内部的排布,别拿一个去否定另一个。
        """
        order = [rule.key for rule in SEGMENT_RULES]
        self.assertEqual(order[0], SEGMENT_SYSTEM, "第一条永远是系统提示")
        self.assertEqual(order[1], SEGMENT_TOOLS, "工具 schema 紧跟系统提示")
        self.assertLess(
            order.index(SEGMENT_TOOLS), order.index(SEGMENT_CONVERSATION),
            "工具在历史之前 —— 这是实测出来的 wire 顺序,不是排版偏好",
        )
        self.assertEqual(
            [rule.order for rule in SEGMENT_RULES],
            ["①", "②", "③", "④", "⑤", "⑥", "⑦"],
            "序号即先后,不许留一个没有序号的段",
        )

    def test_budgets_come_from_config(self) -> None:
        budgets = {rule.key: rule.budget for rule in SEGMENT_RULES}
        self.assertEqual(budgets[SEGMENT_SYSTEM], config.BUDGET_SYSTEM)
        self.assertEqual(budgets[SEGMENT_RUNTIME], config.BUDGET_RUNTIME)
        self.assertEqual(budgets[SEGMENT_MEMORY], config.BUDGET_HOT_SUMMARY)
        self.assertEqual(budgets[SEGMENT_CONVERSATION], config.BUDGET_CONVERSATION)
        self.assertEqual(budgets[SEGMENT_EVIDENCE], config.BUDGET_EVIDENCE)
        self.assertEqual(budgets[SEGMENT_TOOLS], config.BUDGET_TOOLS)
        self.assertEqual(budgets[SEGMENT_CHECKPOINT], config.BUDGET_CHECKPOINT)

    def test_every_rule_explains_itself(self) -> None:
        for rule in SEGMENT_RULES:
            self.assertGreater(len(rule.note), 10, rule.key + " 没有说明")
            self.assertTrue(rule.source, rule.key + " 没写谁来填")
            self.assertTrue(rule.clipping, rule.key + " 没写怎么裁")

    def test_memory_and_conversation_details_are_documented(self) -> None:
        keys = [key for key, _ in MEMORY_BLOCK_STEPS]
        for expected in ("<facts>", "<hot-memories>", "<goals>"):
            self.assertIn(expected, keys)
        self.assertGreaterEqual(len(CONVERSATION_RULES), 3)

    def test_totals_match_config(self) -> None:
        view = build_trace_view(make_settings(), path=Path("/nonexistent/context.jsonl"))
        self.assertEqual(view.totals["hard_max"], config.BUDGET_HARD_MAX)
        self.assertEqual(
            view.totals["trigger"],
            config.BUDGET_COMPACT_TRIGGER,
        )
        # ⚠️ 两个口径别混(页面上原先把六段之和写成了"五段之和",已修):
        #    进 messages 的是**五段**;工具 schema 是第六段、不进 messages。
        self.assertEqual(view.totals["budget_sum"], SEGMENT_BUDGET_SUM)
        self.assertEqual(
            view.totals["budget_sum_with_tools"],
            sum(rule.budget for rule in SEGMENT_RULES),
        )
        self.assertEqual(
            view.totals["budget_sum_with_tools"] - view.totals["budget_sum"],
            config.BUDGET_TOOLS,
        )

    def test_the_budget_lines_say_who_actually_triggers(self) -> None:
        """⭐ 用户 2026-10-14 的追问:*"到多少 token 就会压缩?"*

        答案必须直接:触发压缩的是写死的 120,000(装配时只置标志,回合结束才压),
        而装配时就生效的是硬上限(它做的是收紧对话段,不是当场压缩)。
        这些数被混着看是很容易发生的,所以逐条钉住。

        ⚠️ 2026-10-17:原先并列的「总锁」一条已删除(它不参与任何判定,只会让人再问一遍
        "到它就压缩吗")—— 现在只剩三条线。

        ⭐ D121 起多一条**模型窗口**(deepseek-flash = 1M):它回答的是另一半疑惑 ——
        "怎么这么快就压缩"(触发线只占窗口 2.2%;定这条线的是成本与延迟,不是模型装不下)。
        """
        view = build_trace_view(make_settings(), path=Path("/nonexistent/context.jsonl"))
        lines = {item["key"]: item for item in view.budget_lines}
        self.assertEqual(set(lines), {"trigger", "hard", "window"})
        self.assertEqual(
            lines["trigger"]["value"],
            config.BUDGET_COMPACT_TRIGGER,
        )
        self.assertEqual(lines["hard"]["value"], config.BUDGET_HARD_MAX)
        self.assertIn("压缩", lines["trigger"]["role"])
        self.assertIn("对话段", lines["hard"]["role"])
        # 压缩不在装配时做,而在回复之后
        self.assertIn("回复发出去之后", lines["trigger"]["when"])
        self.assertIn("装配", lines["hard"]["when"])
        # ⭐ 模型窗口那一条:既不触发动作,也要把"触发线占窗口百分之几"算给读者看
        self.assertEqual(lines["window"]["value"], config.CONTEXT_WINDOW_TOKENS)
        self.assertIn("不是我们的预算", lines["window"]["role"])
        share = 100.0 * int(
            config.BUDGET_COMPACT_TRIGGER
        ) / config.CONTEXT_WINDOW_TOKENS
        self.assertIn("%.1f%%" % share, lines["window"]["detail"], "别让读者自己去除")


# ============================================================ 页面
class TracePageCase(SyncCase):
    """页面本身:无密钥能开、导航里处处有它、真聊一轮之后页面上就是那一轮。"""

    def client(self):
        import os

        from fastapi.testclient import TestClient

        from la.web.app import create_app

        previous = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = ""
        self.addCleanup(self._restore_key, previous)
        return TestClient(create_app())

    @staticmethod
    def _restore_key(previous) -> None:
        import os

        if previous is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = previous

    def chat_once(self, text: str) -> None:
        """按 /chat 那套装配真跑一轮(离线 fake,不联网、不花钱)。"""
        conn = db.connect_assistant()
        memory = db.connect_memory()
        try:
            session = MemorySession(
                FakeClient(), make_settings(), store=MemoryStore(memory),
                session_id="web", assistant_conn=conn,
                auto_approve=True, auto_briefing=False, surface="web",
            )
            session.start()
            asyncio.run(session.respond(text))
        finally:
            conn.close()
            memory.close()

    def test_page_opens_without_an_api_key(self) -> None:
        client = self.client()
        response = client.get("/trace")
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("模型这一轮到底看到了什么", body)
        self.assertIn("还没有任何一轮对话", body, "空着的时候要说清怎么让它有内容")
        # ⭐ 一轮都没聊过时,三个页签照样在(规则那一页本来就与有没有聊过无关)
        for label in ("上下文", "规则", "历史"):
            self.assertIn(label, body)
        rules = client.get("/trace?tab=rules").text
        for rule in SEGMENT_RULES:
            self.assertIn(str(rule.budget), rules, "预算没渲染出来: " + rule.key)
        self.assertIn("三条线各管什么", rules)
        self.assertNotIn("总锁", rules, "总锁已经不参与任何判定,不该再出现在页面上")
        self.assertIn("现在这套规则跑在什么上面", rules)

    def test_the_rules_tab_explains_where_the_tool_schema_lives(self) -> None:
        """⭐ 用户 2026-10-14 的追问:"【工具 schema 不在这张表里参与压缩】这句我没理解,
        是这部分内容不在提示词里?那 LLM 怎么拿到这些内容?还是分两次调用 LLM?"

        这三个问题页面必须**直接回答**,而不是留给读者猜 —— 所以要钉住:
        ① 它和 messages 在同一次请求里(不是"模型看不到");
        ② 模型两样都收得到;
        ③ "一轮里可能多次请求"要说清,否则"是不是分两次调用"仍然悬着。
        """
        rules = self.client().get("/trace?tab=rules").text
        self.assertIn("一次请求长什么样", rules)
        self.assertIn("同一次请求", rules)
        self.assertIn("模型两样都收得到", rules)
        self.assertIn("再发一次", rules, "没回答“是不是分两次调用”")
        self.assertNotIn("单独发出", rules, "这句会被读成'另一次调用',已经从页面上撤掉")

    def test_the_rules_tab_separates_the_budget_lines(self) -> None:
        """⭐ 页面上要能直接回答"到 8500 就压缩吗":几条线各管什么 + 修正过的口径数字。"""
        rules = self.client().get("/trace?tab=rules").text
        self.assertIn("三条线各管什么", rules)
        self.assertIn("只有前两条会真的触发动作", rules)
        self.assertIn("回复发出去之后", rules)
        # 设计稿里那条"超硬上限 → 强制压缩 → 重投影"今天没实现,页面上要如实说
        self.assertIn("强制压缩", rules)
        self.assertIn("没有实现", rules)
        # 五段之和(30700)与含工具 schema 的六段之和(33200)都要在、且标清口径
        self.assertIn(str(SEGMENT_BUDGET_SUM), rules)
        self.assertIn(str(SEGMENT_BUDGET_SUM + config.BUDGET_TOOLS), rules)
        # ⭐ D121:模型窗口也要摆在那条线上 —— 它一眼回答"1M 的窗口,为什么 2.2% 就压缩"
        self.assertIn(str(config.CONTEXT_WINDOW_TOKENS), rules)

    def test_three_tabs_are_links_and_the_current_one_is_marked(self) -> None:
        """页签是**纯链接**(服务端只渲染当前那一页)—— 不引 JS,刷新/分享/后退都正常。"""
        client = self.client()
        for tab, needle in (
            (TRACE_TAB_CONTEXT, "还没有任何一轮对话"),
            (TRACE_TAB_RULES, "谁来填"),
            (TRACE_TAB_HISTORY, "还没有任何一轮对话"),
        ):
            body = client.get("/trace?tab=" + tab).text
            self.assertIn('href="/trace?tab=' + tab, body)
            self.assertIn('class="on"', body, tab + " 的页签没标出当前项")
            self.assertIn(needle, body, tab + " 的内容不对")

    def test_unknown_tab_falls_back_to_context(self) -> None:
        """⚠️ 认不出来的 tab 一律回默认页 —— 手打链接、旧书签都不该打成空白。"""
        client = self.client()
        for query in ("?tab=nope", "?tab=", "?tab=../etc", "?tab=CONTEXT"):
            body = client.get("/trace" + query).text
            self.assertIn('href="/trace?tab=context" class="on"', body, query)

    def test_old_index_links_still_land_on_the_context_tab(self) -> None:
        """⭐ 向后兼容:改版前"往前翻一轮"靠 ?i=,那些老链接必须仍然落在「上下文」。"""
        self.chat_once("第一句")
        self.chat_once("第二句")
        body = self.client().get("/trace?i=1").text
        # ⚠️ href 里带 &i=1,Jinja 会把 & 转义成 &amp; —— 只比前缀
        self.assertIn('href="/trace?tab=context', body)
        self.assertIn("第一句", body, "?i=1 要能翻到上一轮")

    def test_message_filter_is_server_side_and_role_aware(self) -> None:
        self.chat_once("这句话是用户说的")
        client = self.client()
        everything = client.get("/trace").text
        talk = client.get("/trace?only=talk").text
        system = client.get("/trace?only=system").text
        self.assertIn("这句话是用户说的", everything)
        self.assertIn("这句话是用户说的", talk, "只看对话时,用户那句必须在")
        self.assertIn("五条不可违反的边界", system, "只看系统时,系统提示必须在")
        self.assertNotIn("这句话是用户说的", system, "只看系统时不该出现用户消息")
        self.assertIn("只看对话", talk)
        # 过滤器也只认白名单
        self.assertIn("这句话是用户说的", client.get("/trace?only=nope").text)

    def test_every_page_links_to_the_trace_page(self) -> None:
        client = self.client()
        for path in ("/chat", "/ledger", "/calendar", "/health", "/study",
                     "/materials", "/memory", "/persona"):
            body = client.get(path, follow_redirects=True).text
            self.assertIn('href="/trace"', body, path + " 的导航缺「轨迹」")

    def test_the_page_shows_the_last_turn_after_a_real_chat(self) -> None:
        self.chat_once("我想看看这一轮到底发了什么")
        body = self.client().get("/trace").text
        self.assertIn("我想看看这一轮到底发了什么", body, "用户那句话必须在里面")
        self.assertIn("逐条消息", body)
        # ⚠️ 尾巴里有 ">",进 HTML 会被转义 —— 按转义后的样子比
        self.assertIn(html.escape(prompts.PERSONA_TAIL), body, "系统提示要能看到")
        self.assertIn(config.get_settings().model, body, "当前参数要说人话")

    def test_page_escapes_whatever_the_user_said(self) -> None:
        self.chat_once("<b>粗体</b> 会被当成文字")
        body = self.client().get("/trace").text
        self.assertNotIn("<b>粗体</b>", body, "用户内容必须转义后进页面")
        self.assertIn("&lt;b&gt;", body)

    def test_bad_index_does_not_break_the_page(self) -> None:
        self.chat_once("你好")
        client = self.client()
        for query in ("?i=abc", "?i=99", "?i=-3", "", "?i=1&tab=rules",
                      "?i=0&only=talk", "?tab=history&i=99"):
            response = client.get("/trace" + query)
            self.assertEqual(response.status_code, 200, "/trace" + query)

    def test_the_history_tab_links_into_the_context_tab(self) -> None:
        self.chat_once("第一句")
        self.chat_once("第二句")
        body = self.client().get("/trace?tab=history").text
        self.assertIn("看这一轮", body)
        self.assertIn('href="/trace?tab=context&i=1"', body, "历史那一行要能点进那一轮")
        self.assertIn("正在看", body, "当前那一轮要标出来")


if __name__ == "__main__":
    unittest.main()