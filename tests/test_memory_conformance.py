"""2026-10-12 记忆模块一致性审核的回归测试。

审核报告见 docs/记忆模块一致性审核.md,拍板见 docs/决策记录.md D93–D97。
这份文件只钉**审核中判定为差异**的那几条 —— 每一条都能对应回报告编号,
免得以后又被"简化"回去:

  P0   摘要池不可重建 -> 记忆页三层标注与事实一致(tests/test_memory_view.py 覆盖)
  P1-1 考古成功 -> base_strength + 1(设计深化 §1.4)
  P1-2 confidential -> 注入块与工具返回都要脱敏(§3.5/§3.8/§4.2)
  P1-3 硬上限 10000 真的会被强制 + 工具 schema 单列上报(§1.6)
  P2   三档衰减真的有三档(§1.4);子代理 ambiguous 给出"该问用户"的判据(§2.2/§4.3);
       巩固失败落 consolidate/failed 事件(§1.5);压缩区间不稳定就放弃(§1.3-③)
  口径 检索按加权公式排序并给出 is_exact_anchor_hit(§3.1);锚点行覆盖助手侧(§1.3)

运行:
    .venv/Scripts/python.exe -m unittest tests.test_memory_conformance -v
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from la import config
from la.base.phases import PHASE_ADJUDICATE, CircuitBreaker
from la.agent.session import MemorySession, PhaseOutcome
from la.llm.fake import FakeClient
from la.memory.adjudicator import Verdict, adjudicate, render_evidence_block
from la.memory.anchors import anchor_kind
from la.memory.archive import Event
from la.memory.consolidate import consolidate
from la.memory.decay import classify_bucket
from la.memory.projector import Projector, build_memory_block
from la.memory.retrieval import RetrievalResult, retrieve
from la.memory.scratchpad import Evidence, Scratchpad
from la.memory.store import MemoryStore
from la.memory.summarize import extractive_summary
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY
from la.tools.task_tools import build_task_tools


def make_settings(session_id: str = "s1") -> config.Settings:
    return dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id=session_id
    )


def event(kind: str, **data) -> Event:
    return Event(seq=1, type=kind, time="2026-10-08T10:00:00+08:00", data=data)


class StoreFixture(unittest.IsolatedAsyncioTestCase):
    """一个隔离的记忆库 + 归档目录。

    ⚠️ 必须是 IsolatedAsyncioTestCase:继承 `unittest.TestCase` 时 async 测试**不会被执行**,
    只会留下一条"coroutine was never awaited"的告警然后显示 ok —— 那是假绿。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = self.tmp / "archive"
        self.conn = db.connect(self.tmp / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.archive)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def new_session(self, *, session_id: str = "s1") -> MemorySession:
        session = MemorySession(
            FakeClient(), make_settings(session_id), store=self.store,
            session_id=session_id, auto_briefing=False,
        )
        session.start()
        return session

    def seed_turns(self, session_id: str, count: int) -> None:
        for index in range(count):
            self.store.append(session_id, "user/message", {"text": "第" + str(index) + "轮提问"})
            self.store.append(
                session_id, "assistant/message", {"text": "第" + str(index) + "轮回答"}
            )


# ==========================================================================
# P1-1 考古成功 -> 残余基底强度 +1(设计深化 §1.4)
# ==========================================================================
class BaseStrengthCase(StoreFixture):
    def test_bump_base_strength_is_permanent_and_clears_the_cache(self) -> None:
        session_id = "s1"
        self.store.ensure_session(session_id)
        summary_id = self.store.upsert_summary(
            session_id=session_id, start_seq=1, end_seq=2,
            start_time="2026-05-10T09:00:00+08:00", end_time="2026-05-10T11:30:00+08:00",
            text="<compacted-summary>\n- 事实:5 万\n</compacted-summary>", anchors=["5万"],
        )
        self.store.refresh_heat_cache()
        self.assertEqual(self.store.bump_base_strength([summary_id], 1.0), 1)
        row = self.store.summaries([summary_id])[summary_id]
        self.assertEqual(row.base_strength, 2.0)
        self.assertIsNone(row.heat_cache, "基底强度变了,热度缓存必须失效")

    async def test_archaeology_success_raises_base_strength(self) -> None:
        session_id = "s1"
        self.store.ensure_session(session_id)
        for index in range(6):
            self.store.append(
                session_id, "user/message", {"text": "第" + str(index) + "句:装修预算 5 万"}
            )
        summary_id = self.store.upsert_summary(
            session_id=session_id, start_seq=1, end_seq=6,
            start_time="2026-05-10T09:00:00+08:00", end_time="2026-05-10T11:30:00+08:00",
            text="<compacted-summary>\n- 事实:装修预算 5 万\n</compacted-summary>",
            anchors=["5万"], category="periodic",
        )
        session = self.new_session(session_id=session_id)
        pad = Scratchpad()
        pad.add(
            Evidence(
                source="timestamp_range", timestamp="2026-05-10T11:30:00+08:00",
                content="装修预算改成 6 万", seq=5, session_id=session_id,
            )
        )
        outcome = PhaseOutcome(
            breaker=CircuitBreaker(phase=PHASE_ADJUDICATE),
            pad=pad,
            qa=RetrievalResult(status="miss"),
            verdict=Verdict(basis="archive"),
            archaeology_ran=True,
        )
        await session._after_reply(outcome)

        row = self.store.summaries([summary_id])[summary_id]
        self.assertEqual(row.base_strength, 2.0, "考古成功必须让基底强度 +1(设计深化 §1.4)")
        self.assertIn("6 万", row.text, "证据要并进桶里")

        # 幂等:同一轮重复回填不再加
        await session._after_reply(outcome)
        self.assertEqual(self.store.summaries([summary_id])[summary_id].base_strength, 2.0)


# ==========================================================================
# P1-2 敏感事实不进对话(设计深化 §3.5 / §3.8 / §4.2)
# ==========================================================================
class ConfidentialCase(StoreFixture):
    SECRET = "用户的家庭住址是示例市示例区示例路100号"

    def test_confidential_fact_never_reaches_the_memory_block(self) -> None:
        self.store.add_fact(self.SECRET, fact_type="identity", confidential=True)
        block = build_memory_block(
            facts=self.store.active_facts(), summaries=[], status="hit", now_text=""
        )
        self.assertNotIn("示例路", block, "标了敏感的内容不许进提示词")
        self.assertIn(config.SECRET_PLACEHOLDER, block)
        self.assertIn("#1", block, "编号要留着 —— memory_correct 还得用它")

    def test_normal_fact_still_shows_in_full(self) -> None:
        self.store.add_fact("用户的英文名是 Kevin", fact_type="identity")
        block = build_memory_block(
            facts=self.store.active_facts(), summaries=[], status="hit", now_text=""
        )
        self.assertIn("Kevin", block)

    async def test_summary_pool_payload_masks_confidential_facts(self) -> None:
        self.store.add_fact(self.SECRET, fact_type="identity", confidential=True)
        session = self.new_session()
        payload = await session._handle_summary_pool({"query": "住址"})
        self.assertTrue(payload["facts"])
        self.assertTrue(all("示例路" not in item for item in payload["facts"]))
        self.assertIn(config.SECRET_PLACEHOLDER, payload["facts"][0])

    async def test_memory_tools_mask_confidential_content(self) -> None:
        tools = build_task_tools(
            todos=None, goals=None, schedules=None, library=None,
            add_fact=self.store.add_fact, memory=self.store,
        )
        by_name = {tool.name: tool for tool in tools}

        written = await by_name["memory_note"].handler(
            {"content": self.SECRET, "fact_type": "identity", "confidential": True}
        )
        self.assertEqual(written["content"], config.SECRET_PLACEHOLDER)

        recalled = await by_name["memory_recall"].handler({"query": ""})
        self.assertEqual(len(recalled["facts"]), 1)
        self.assertEqual(recalled["facts"][0]["content"], config.SECRET_PLACEHOLDER)
        self.assertTrue(recalled["facts"][0]["confidential"])


# ==========================================================================
# P1-3 硬上限与工具 schema(设计深化 §1.6)
# ==========================================================================
class ProjectionBudgetCase(StoreFixture):
    def test_the_normal_assembly_is_not_squeezed(self) -> None:
        """⭐ D121:五段上限之和(30700)装得进硬上限(40000)—— 正常装配**不该**被收。

        这是重定标的直接结果:旧数字下五段之和 10700 顶破硬上限 10000,所以每一轮都要
        "少给对话段",而对话段本来就只有 4000 —— 两头一起把历史窗口掐死了。
        """
        projection = Projector(make_settings()).build(
            system_text="记忆条目" * 3000, runtime_text="运行上下文",
            memory_text="记忆条目" * 3000, evidence_text="记忆条目" * 3000,
            history=[{"role": "user", "content": "问" * 400}] * 4,
        )
        self.assertFalse(projection.capped, "正常装配不该动对话段")
        self.assertEqual(projection.conversation_budget, config.BUDGET_CONVERSATION)

    def test_hard_cap_is_enforced_and_tools_are_reported_separately(self) -> None:
        """硬上限真被顶到时,仍然只收对话段、而且如实标记。

        ⚠️ 正常配置下顶不到(见上一条),所以这里把硬上限临时压小来验**机制**本身。
        """
        projector = Projector(make_settings())
        long_text = "记忆条目" * 3000
        history = [
            {"role": "user", "content": "问" * 400},
            {"role": "assistant", "content": "答" * 400},
        ] * 40
        original = config.BUDGET_HARD_MAX
        config.BUDGET_HARD_MAX = 8000
        try:
            projection = projector.build(
                system_text=long_text, runtime_text="运行上下文", memory_text=long_text,
                history=history, evidence_text=long_text, tools_tokens=9000,
            )
        finally:
            config.BUDGET_HARD_MAX = original
        self.assertLessEqual(
            projection.total_tokens, 8000,
            "硬上限是天花板:固定四段先占,剩下的才给对话段",
        )
        self.assertTrue(projection.capped, "收过就要如实标记")
        self.assertEqual(projection.segments["tools"], 9000)
        self.assertEqual(projection.grand_total, projection.total_tokens + 9000)

    def test_session_reports_the_real_tool_schema_weight(self) -> None:
        session = self.new_session()
        tools_tokens = session.tools_tokens()
        self.assertGreater(tools_tokens, 1000, "几十个工具的 schema 不可能只有几百 token")
        projection = session._build_projection([], "", "")
        self.assertEqual(projection.segments["tools"], tools_tokens)


# ==========================================================================
# P2 三档衰减 / ambiguous / 失败留痕 / 压缩稳定性
# ==========================================================================
class DecayCategoryCase(unittest.TestCase):
    def test_classify_bucket_covers_all_three_tiers(self) -> None:
        permanent = classify_bucket(
            [event("memory/note", content="用户的生日是 3 月 2 日", fact_type="identity")]
        )
        temporary = classify_bucket([event("user/message", text="晚上吃了碗面")])
        periodic = classify_bucket([event("user/message", text="装修预算 6 万元")])
        self.assertEqual(
            (permanent, temporary, periodic), ("permanent", "temporary", "periodic")
        )


class CompactionAndConsolidationCase(StoreFixture):
    async def test_compaction_is_skipped_when_the_range_grows_during_summary(self) -> None:
        session_id = "s1"
        session = self.new_session(session_id=session_id)
        self.seed_turns(session_id, 8)

        class GrowingSummarizer:
            async def summarize(self, events, *, start_time, end_time):
                session.store.append(
                    session_id, "user/message", {"text": "摘要生成期间又插进来一条"}
                )
                return extractive_summary(events, start_time=start_time, end_time=end_time)

        session.summarizer = GrowingSummarizer()
        self.assertIsNone(await session.compact_once(), "区间在长大时必须放弃这一轮")
        skipped = [
            item for item in self.store.events(session_id) if item.type == "compaction/skipped"
        ]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].data.get("reason"), "unstable")
        self.assertFalse(self.store.all_summaries(session_id), "放弃就不该留下半截摘要")

    async def test_consolidation_failure_writes_an_event_and_gives_up(self) -> None:
        session_id = "s1"
        self.store.ensure_session(session_id)
        for index in range(3):
            self.store.append(session_id, "user/message", {"text": "第" + str(index) + "句"})

        def boom(**kwargs):
            raise RuntimeError("磁盘满了")

        self.store.upsert_summary = boom  # type: ignore[assignment]
        outcome = await consolidate(
            self.store, session_id=session_id, start_seq=1, end_seq=3,
            rounds_after_reply=1,
            evidence=[
                Evidence(
                    source="timestamp_range", timestamp="2026-05-10T11:30:00+08:00",
                    content="装修预算 6 万", seq=2, session_id=session_id,
                )
            ],
            start_time="2026-05-10T09:00:00+08:00", end_time="2026-05-10T11:30:00+08:00",
        )
        self.assertFalse(outcome.applied)
        self.assertIn("FAILED", outcome.reason)
        failures = [
            item for item in self.store.events(session_id) if item.type == "consolidate/failed"
        ]
        self.assertEqual(len(failures), 1, "失败必须留痕(设计深化 §1.5)")


class AmbiguityCase(unittest.TestCase):
    def test_ambiguous_verdict_asks_the_user_before_deciding(self) -> None:
        pad = Scratchpad()
        pad.add(
            Evidence(
                source="sub_agent", timestamp="2026-10-08T10:00:00+08:00",
                content="方案叫星火计划",
            )
        )
        verdict = adjudicate(pad, terms=["星火"], budget_tokens=2500, ambiguous=True)
        self.assertTrue(verdict.ambiguous)
        block = render_evidence_block(verdict, budget_tokens=2500)
        self.assertIn("<evidence-ambiguity>", block)
        self.assertIn("ask_user", block)
        # 命中本身仍然是 found,只是多了一条"该问"的判据
        self.assertIn("<evidence-status>found</evidence-status>", block)


# ==========================================================================
# 口径:检索加权公式 / 锚点类别 / 锚点覆盖面
# ==========================================================================
class RetrievalScoringCase(StoreFixture):
    def test_score_is_the_weighted_formula_and_exposes_the_components(self) -> None:
        session_id = "s1"
        self.store.ensure_session(session_id)
        self.store.append(session_id, "user/message", {"text": "装修预算敲定 6 万"})
        summary_id = self.store.upsert_summary(
            session_id=session_id, start_seq=1, end_seq=1,
            start_time="2026-05-10T09:00:00+08:00", end_time="2026-05-10T11:30:00+08:00",
            text="<compacted-summary>\n- 事实:装修预算敲定 6 万\n</compacted-summary>",
            anchors=["6万", "2026-05-10"],
        )
        self.store.refresh_heat_cache()
        result = retrieve(self.store, "装修预算 6万", now=make_settings().now())
        self.assertEqual(result.status, "hit")
        detail = result.details[summary_id]
        expected = (
            0.5 * detail["anchor_hit"] + 0.3 * detail["fts_norm"] + 0.2 * detail["heat_norm"]
        )
        self.assertAlmostEqual(detail["score"], expected, places=5)
        self.assertTrue(detail["is_exact_anchor_hit"])

    def test_anchor_kind_covers_the_three_categories(self) -> None:
        self.assertEqual(anchor_kind("2026-10-08"), "date")
        self.assertEqual(anchor_kind("6万"), "number")
        self.assertEqual(anchor_kind("老张牛肉面"), "name")


class SummaryAnchorCase(unittest.TestCase):
    def test_extractive_anchors_cover_the_assistant_side_too(self) -> None:
        events = [
            Event(seq=1, type="user/message", time="2026-05-10T09:00:00+08:00",
                  data={"text": "就按这个来吧"}),
            Event(seq=2, type="assistant/message", time="2026-05-10T09:01:00+08:00",
                  data={"text": "好,预算 6 万,6月15日截止"}),
        ]
        draft = extractive_summary(events, start_time="t0", end_time="t1")
        self.assertIn("6月15日", draft.anchors, "锚点行必须覆盖区间内出现的锚点(含助手说的)")


if __name__ == "__main__":
    unittest.main()
