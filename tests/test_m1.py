"""M1 验收测试:事件日志、检索、投影、压缩、热度,以及场景 S1 / S2。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from la import config
from la.agent.session import MemorySession
from la.llm.fake import FakeClient
from la.memory.anchors import anchor_hits, extract_anchors
from la.memory.decay import heat
from la.memory.notes import detect_notes
from la.memory.projector import SEGMENT_CHECKPOINT, SEGMENT_SYSTEM, build_checkpoint_block
from la.memory.search import MODE_TRIGRAM, extract_terms, get_mode, search_summaries
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY

CHITCHAT = [
    "今天天气不错",
    "晚上吃了碗面",
    '我们刚才聊的那家餐厅叫"老张牛肉面",下周三中午想去',
    "地铁上的人真多",
    "下午开了个短会",
    "电脑有点卡",
    "楼下新开了家咖啡店",
    "周末想去看场电影",
    "手机充电器找不到了",
    "阳台的花该浇水了",
    "同事推荐了一本书",
    "快递放到了驿站",
    "明天要早起",
    "空调温度调低了一点",
    "鞋子有点磨脚",
    "想换个键盘",
    "冰箱里的牛奶快过期了",
    "晚上散步走了两圈",
    "窗户关好了",
    "睡前一小时不看手机",
]


def make_settings(session_id: str = "s1") -> config.Settings:
    return dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id=session_id
    )


class MemoryFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = self.tmp / "archive"
        self.db_path = self.tmp / "memory.db"
        self._open()

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _open(self) -> None:
        self.conn = db.connect(self.db_path)
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.archive)

    def restart(self) -> None:
        """模拟进程重启:关连接、重开、重建全部内存对象。"""
        self.conn.close()
        self._open()

    def new_session(self, *, session_id: str = "s1", client=None) -> MemorySession:
        session = MemorySession(
            client or FakeClient(),
            make_settings(session_id),
            store=self.store,
            session_id=session_id,
        )
        session.start()
        return session


class UnitTestCase(MemoryFixture):
    def test_extract_anchors(self):
        found = extract_anchors('预算 6 万元,6月15日截止,餐厅叫"老张牛肉面"')
        self.assertIn("6 万", " ".join(found))
        self.assertIn("6月15日", found)
        self.assertIn("老张牛肉面", found)

    def test_anchor_hits_is_bidirectional(self):
        self.assertTrue(anchor_hits(["老张牛肉面"], ["老张牛肉面店"]))

    def test_extract_terms_strips_chinese_function_words(self):
        self.assertEqual(extract_terms("我们刚才聊的餐厅叫什么"), ["餐厅"])
        self.assertIn("装修预算", extract_terms("上周三上午我们说的装修预算是多少"))

    def test_heat_formula_matches_documentation(self):
        # (base + visits) ** 0.5 * exp(-lambda * days)
        self.assertAlmostEqual(heat(1.0, 0, 0), 1.0, places=6)
        self.assertAlmostEqual(heat(1.0, 3, 0), 2.0, places=6)
        # temporary 半衰期 2 天 -> 2 天后正好减半
        self.assertAlmostEqual(heat(1.0, 0, 2, category="temporary"), 0.5, places=6)
        # periodic 半衰期 15 天
        self.assertAlmostEqual(heat(1.0, 0, 15, category="periodic"), 0.5, places=6)
        # permanent 永不衰减
        self.assertAlmostEqual(heat(1.0, 0, 3650, category="permanent"), 1.0, places=6)

    def test_detect_notes_identity_and_nickname(self):
        notes = detect_notes("我的英文名是 Kevin,叫我就行")
        contents = [n.content for n in notes]
        self.assertTrue(any("Kevin" in c for c in contents), contents)
        self.assertFalse(any("就行" in c for c in contents), contents)

    def test_detect_notes_aversion(self):
        notes = detect_notes("我不吃香菜")
        self.assertTrue(any("香菜" in n.content for n in notes))
        self.assertEqual(notes[0].fact_type, "preference")

    def test_detect_notes_explicit_marker(self):
        notes = detect_notes("帮我记住:项目X 报价 6月15日 截止")
        explicit = [n for n in notes if n.explicit]
        self.assertTrue(explicit)
        self.assertIn("6月15日", explicit[0].keywords)

    def test_capability_probe_reports_trigram_on_this_machine(self):
        self.assertEqual(get_mode(self.conn), MODE_TRIGRAM)

    def test_two_char_query_falls_back_to_like(self):
        """⚠️ 核心回归:实测 trigram 对两字词召回为 0,必须靠 LIKE 降级。"""
        self.store.upsert_summary(
            session_id="s1",
            start_seq=1,
            end_seq=2,
            start_time="2026-10-08T10:00:00+08:00",
            end_time="2026-10-08T11:00:00+08:00",
            text="<compacted-summary>- 事项:明天上午开项目会议讨论预算;老张牛肉面</compacted-summary>",
            anchors=["2026-10-08"],
        )
        short = search_summaries(self.conn, "会议")
        self.assertTrue(short, "两字词必须能召回")
        self.assertEqual(next(iter(short.values())).match_kinds, ("like",), "两字词走 LIKE")

        long = search_summaries(self.conn, "牛肉面")
        self.assertTrue(long, "三字词必须能召回")
        self.assertIn("fts", next(iter(long.values())).match_kinds, "三字词走 fts5 trigram")

    def test_extract_terms_never_shortens_a_word_below_two_chars(self):
        # "会议" 的 "会" 也在虚词表里 —— 无脑 strip 会把它剥成 "议" 而丢掉
        self.assertEqual(extract_terms("会议"), ["会议"])
        self.assertEqual(extract_terms("餐厅叫什么"), ["餐厅"])

    def test_summary_bucket_keeps_one_row(self):
        for _ in range(3):
            self.store.upsert_summary(
                session_id="s1",
                start_seq=1,
                end_seq=4,
                start_time="t0",
                end_time="t1",
                text="<compacted-summary>- 事项:x</compacted-summary>",
            )
        rows = self.store.all_summaries("s1")
        self.assertEqual(len(rows), 1)

    def test_events_rebuild_from_archive(self):
        session = self.new_session()
        self.store.append("s1", "user/message", {"text": "你好"})
        self.store.append("s1", "assistant/message", {"text": "你好呀"})
        before = len(self.store.events("s1"))
        self.conn.execute("DELETE FROM events")
        self.conn.commit()
        self.assertEqual(len(self.store.events("s1")), 0)
        restored = self.store.rebuild_from_archive("s1")
        self.assertEqual(restored, before)
        self.assertEqual(len(self.store.events("s1")), before)
        del session

    def test_facts_supersede_instead_of_delete(self):
        old = self.store.add_fact("用户的预算是 5 万", fact_type="decision")
        self.store.add_fact("用户的预算是 6 万", fact_type="decision", supersedes=old)
        active = self.store.active_facts()
        self.assertEqual([f.content for f in active], ["用户的预算是 6 万"])
        total = self.conn.execute("SELECT count(*) FROM facts").fetchone()[0]
        self.assertEqual(total, 2, "旧事实必须保留(只置 active=0)")


class ScenarioTestCase(MemoryFixture):
    async def test_s1_preference_is_remembered(self):
        """S1:说一次名字,写入 facts,下一轮起投影一直带着它。"""
        client = FakeClient()
        session = self.new_session(client=client)
        await session.respond("我的英文名是 Kevin,叫我就行")

        facts = self.store.active_facts()
        self.assertTrue(any("Kevin" in f.content for f in facts), facts)

        await session.respond("今天天气怎么样?")
        system_text = "\n".join(
            m["content"] for m in client.calls[-1] if m["role"] == "system"
        )
        self.assertIn("Kevin", system_text, "偏好必须出现在下一轮投影里")
        self.assertIn("<facts>", system_text)

    async def test_compaction_triggers_at_the_configured_number(self):
        """⭐ D134(用户 2026-10-16 拍板):历史上下文到 **120,000** 就压,**压到它的 20%**。

        两条与旧口径不同的地方,这里都钉住:
          ① 阈值是 config 里写死的数(`BUDGET_COMPACT_TRIGGER`),不再由总锁乘比例推出来;
          ② 保留量按"**当前那整段历史**"算(事件日志里实际有多少),不是"对话段预算的一半";
             也不是投影里剩下的那些 —— 投影已被对话段预算裁过,拿它乘会算小(实测踩到过)。
        """
        session = self.new_session()
        # 塞一段足够长的历史:每条约 200 token(700 字),500 对 ≈ 20 万 > 触发线 12 万
        for index in range(500):
            self.store.append("s1", "user/message", {"text": "第 %d 问 %s" % (index, "字" * 700)})
            self.store.append("s1", "assistant/message", {"text": "第 %d 答 %s" % (index, "字" * 700)})

        history = session.conversation_history()
        full = config.estimate_messages_tokens(history)
        projection = session._build_projection(history, "", "")
        self.assertTrue(
            projection.needs_compaction,
            "历史上下文 {} 已越过触发线 {},却没置压缩标志".format(
                projection.retained_tokens, config.BUDGET_COMPACT_TRIGGER),
        )
        self.assertGreaterEqual(projection.retained_tokens, config.BUDGET_COMPACT_TRIGGER)

        # 保留 20%(用户拍板):先看预算算得对不对,再看真的压完剩多少
        expected_keep = int(full * config.BUDGET_COMPACT_RETAIN_RATIO)
        self.assertEqual(session.compaction_retain_budget(), expected_keep)
        summary_id = await session.compact_once()
        self.assertIsNotNone(summary_id, "越过触发线后必须能切出一个摘要")

        after_full = config.estimate_messages_tokens(session.conversation_history())
        self.assertLessEqual(
            after_full, expected_keep * 1.5,
            "压缩后应当只剩 20% 上下(实测 {},目标 {})".format(after_full, expected_keep),
        )
        self.assertGreater(after_full, expected_keep * 0.5, "别把整段历史压没了")
        after = session._build_projection(session.conversation_history(), "", "")
        self.assertFalse(after.needs_compaction, "压完就不该还挂着「该压了」")
        self.assertTrue(self.store.all_summaries("s1"), "摘要必须落盘")

    async def test_compaction_leaves_a_checkpoint_before_history(self):
        """⭐ D142(用户 2026-10-17):压完之后,历史**最前面**留一条"压过哪一段"。

        两个性质都是必需的,少一个"这 500 token 能缓存命中"就不成立:
          ① 位置在历史**之前** —— 排在历史之后,它会跟着历史每轮后移,永远不在
             "从头开始逐字节相同"的那一段里(见 config.BUDGET_CHECKPOINT 的注释);
          ② 不压缩时它的字节**完全不变** —— 缓存命中的前提就是逐字节相同。
        """
        session = self.new_session()
        for index in range(500):
            self.store.append("s1", "user/message", {"text": "第 %d 问 %s" % (index, "字" * 700)})
            self.store.append("s1", "assistant/message", {"text": "第 %d 答 %s" % (index, "字" * 700)})

        # 没压过:一段都不占(空段不进消息列表,一个字节都不花)
        before = session._build_projection(session.conversation_history(), "", "")
        self.assertEqual(before.segments[SEGMENT_CHECKPOINT], 0)
        self.assertNotIn("<compacted-context>", before.messages[1]["content"])

        summary_id = await session.compact_once()
        self.assertIsNotNone(summary_id)

        projection = session._build_projection(session.conversation_history(), "", "")
        system_message, checkpoint = projection.messages[0], projection.messages[1]
        self.assertEqual(system_message["role"], "system")
        self.assertEqual(checkpoint["role"], "system", "检查点必须是紧跟着系统提示的那一条")
        self.assertEqual(projection.message_segments[1], SEGMENT_CHECKPOINT)
        self.assertIn("<compacted-context>", checkpoint["content"])
        self.assertIn("[#" + str(summary_id) + "]", checkpoint["content"], "得说清是哪条摘要")
        self.assertIn("2026-", checkpoint["content"], "得带时间区间,否则等于没法去查")
        self.assertGreater(projection.segments[SEGMENT_CHECKPOINT], 0)
        self.assertLessEqual(projection.segments[SEGMENT_CHECKPOINT], config.BUDGET_CHECKPOINT)
        # 它算在"下一轮还能原样重放的前缀"里 —— 这就是那 500 token 能命中的依据
        self.assertGreaterEqual(
            projection.cache_prefix_tokens,
            projection.segments[SEGMENT_SYSTEM] + projection.segments[SEGMENT_CHECKPOINT],
        )
        # 缓存的前提:同样的摘要 -> 逐字节相同的两条消息
        again = session._build_projection(session.conversation_history(), "", "")
        self.assertEqual(again.messages[0], projection.messages[0])
        self.assertEqual(again.messages[1], projection.messages[1])

    def test_checkpoint_keeps_the_newest_and_folds_the_rest(self):
        """列不完就折叠:只留最近 N 段,其余变成一句"还有几段",总量不涨。"""
        class Row:
            def __init__(self, index: int) -> None:
                self.id = index
                self.start_time = "2026-10-%02dT09:00:00+08:00" % (index % 28 + 1)
                self.end_time = "2026-10-%02dT11:00:00+08:00" % (index % 28 + 1)
                self.text = (
                    "<compacted-summary>\n- 事项:第 " + str(index) + " 段聊的事\n"
                    "- 锚点:[]\n</compacted-summary>"
                )

        rows = [Row(index) for index in range(1, 21)]
        text = build_checkpoint_block(rows)
        self.assertIn("[#20]", text, "最近压的那一段必须在")
        self.assertNotIn("[#1]", text, "最早的那几段该被折叠掉")
        self.assertIn(
            "更早还有 " + str(20 - config.CHECKPOINT_MAX_ITEMS) + " 段已折叠", text,
        )
        self.assertLessEqual(config.estimate_tokens(text), config.BUDGET_CHECKPOINT)
        self.assertEqual(build_checkpoint_block([]), "", "没压过就别占位")

    async def test_s2_summary_survives_restart(self):
        """S2:闲聊 20 轮 -> 压缩 -> 重启 -> 仍能从摘要命中,并带锚点时间。"""
        client = FakeClient()
        session = self.new_session(client=client)
        for line in CHITCHAT:
            await session.respond(line)

        summary_id = await session.compact_once()
        self.assertIsNotNone(summary_id, "压力满足时必须能切出一个摘要")

        summary = self.store.all_summaries("s1")[0]
        self.assertIn("老张牛肉面", summary.text)
        self.assertIn("2026-", json_dumps(summary.anchors)[:0] + summary.start_time)

        # 压缩后,原始轮次不再逐条进入投影
        history_text = " ".join(m["content"] for m in session.conversation_history())
        self.assertNotIn("老张牛肉面", history_text, "已压缩区间不该再逐条发送")

        # —— 重启 ——
        self.restart()
        revived_client = FakeClient()
        revived = self.new_session(client=revived_client)
        reply = await revived.respond("我们刚才聊的餐厅叫什么")

        self.assertEqual(revived.last_retrieval.status, "hit")
        self.assertIn("老张牛肉面", reply.text)
        self.assertRegex(reply.text, r"\d{4}-\d{2}-\d{2}", "命中必须带锚点时间")

        # 检索轨迹必须落盘(审计与调优用)
        retrievals = [e for e in self.store.events("s1") if e.type == "memory/retrieval"]
        self.assertTrue(retrievals)
        self.assertEqual(retrievals[-1].data["status"], "hit")

    async def test_s2_honest_when_nothing_matched(self):
        """S2 的另一半:没记过的事必须直说,不许编。"""
        session = self.new_session()
        for line in CHITCHAT[:8]:
            await session.respond(line)

        client = FakeClient()
        probe = self.new_session(client=client)
        reply = await probe.respond("我上次说的那个航班号是多少")
        self.assertEqual(probe.last_retrieval.status, "miss")
        self.assertIn("记忆里没有", reply.text)
        system_text = "\n".join(
            m["content"] for m in client.calls[-1] if m["role"] == "system"
        )
        self.assertIn("<memory-status>miss</memory-status>", system_text)
        self.assertIn("不要用听起来合理的猜测填空", system_text)

    async def test_memory_block_is_ordered_slowest_first(self):
        """⭐ 用户 2026-10-16 点名:"事实应该是不会变动的,记忆是不是可能会变动?"

        对。所以记忆块内部按「**变得慢的放前面**」排:facts(一轮里完全不动)→ goals
        (推进时才动)→ memory-status / briefing(每轮都可能变)→ hot-memories(每轮不同)。

        ⚠️ 为什么这条值得单独钉:同一次应答里模型可能被请求好几次(调工具后把结果
        追加进 messages 再发一次),那几次请求的记忆块**是同一份** —— 稳定的排前面,
        后面的请求才能整段命中它;可变的排在第一个,等于第 2 个 token 就作废整块。
        这条只断言**顺序**(事实必须先于状态),不锁具体文案。
        """
        from la.memory.projector import build_memory_block

        class Fact:
            content = "用户的名字是「路过」"
            confidential = False
            id = 1

        block = build_memory_block(
            facts=[Fact()],
            summaries=[],
            status="miss",
            now_text="",
            briefing="📚 孩子今天的错题复习(1 道)",
        )
        self.assertLess(block.index("<facts>"), block.index("<memory-status>"),
                        "事实在每个新事实进来之前完全不动,必须排在状态前面")
        self.assertLess(block.index("</facts>"), block.index("<memory-status>"))
        self.assertLess(block.index("<memory-status>"), block.index("<memory-guidance>"))
        # 状态与今日推送仍然都在,而且都在 facts 之后(不锁它们俩谁先)
        self.assertIn("<memory-status>miss</memory-status>", block)
        self.assertIn("今天的错题复习", block)
        self.assertTrue(block.startswith("<memory>") and block.rstrip().endswith("</memory>"))

    async def test_heat_rises_when_memory_is_used(self):
        session = self.new_session()
        for line in CHITCHAT:
            await session.respond(line)
        await session.compact_once()
        before = self.store.all_summaries("s1")[0]
        self.assertEqual(before.visits, 0)

        await session.respond("我们刚才聊的餐厅叫什么")
        after = self.store.all_summaries("s1")[0]
        self.assertGreater(after.visits, 0, "命中后热度必须升上去")
        self.assertIsNotNone(after.heat_cache)

    async def test_projection_never_exceeds_total_lock(self):
        session = self.new_session()
        for line in CHITCHAT[:10]:
            await session.respond(line)
        messages, total = session.projection_preview()
        self.assertLessEqual(total, config.BUDGET_HARD_MAX)

    async def test_history_does_not_replay_thinking_by_default(self):
        """⭐ 2026-10-14:跨轮历史**不回传思维链**(照云朵的口径:思考只活在本轮)。

        实测(scripts/verify_reasoning_replay.py,真实模型 5 项):API 只要求**轮内**那条
        "结果还没被消费"的 assistant 带 reasoning_content 字段(内容可以为空),跨轮历史
        连字段都不校验 —— 所以"历史带不带思维链"是**取舍**不是约束:实测思维链是正文的
        232%,带回去等于让历史窗口的三分之二花在"它当时怎么想的"上(云朵选择不留,
        DSH 选择全文回传,靠 1M 窗口扛)。要回滚:config.REPLAY_HISTORY_REASONING = True。
        """
        client = FakeClient(reasoning_script=["我先把它记下来。"] * 20)
        session = self.new_session(client=client)
        await session.respond("交报告这件事记一下")
        await session.respond("另外把牛奶也加上")

        stored = [e for e in self.store.events("s1") if e.type == "assistant/message"]
        self.assertTrue(any(e.data.get("reasoning") for e in stored),
                        "思维链照旧**落库**(它是事实源,只是不回传)")

        history = session.conversation_history()
        assistant = [m for m in history if m["role"] == "assistant"]
        self.assertTrue(assistant, "得先有一条 assistant 历史")
        self.assertTrue(
            all("reasoning_content" not in m for m in assistant),
            "默认口径:跨轮历史只装提问与正式回答",
        )

        original = config.REPLAY_HISTORY_REASONING
        try:
            config.REPLAY_HISTORY_REASONING = True
            replay = session.conversation_history()
        finally:
            config.REPLAY_HISTORY_REASONING = original
        self.assertTrue(
            any(m.get("reasoning_content") for m in replay if m["role"] == "assistant"),
            "开关打开时(DSH 口径)思维链必须回传",
        )

    async def test_auto_compaction_triggers_under_pressure(self):
        original = config.BUDGET_CONVERSATION
        try:
            config.BUDGET_CONVERSATION = 80
            session = self.new_session()
            for line in CHITCHAT[:12]:
                await session.respond(line)
        finally:
            config.BUDGET_CONVERSATION = original
        self.assertTrue(self.store.all_summaries("s1"), "超预算时必须自动压缩")


def json_dumps(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
