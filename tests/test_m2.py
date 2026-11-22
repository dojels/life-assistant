"""M2 验收测试:时间解析、熔断矩阵、工具管线、裁决、子代理,以及场景 S3 / S4 / S7。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import asyncio
import dataclasses
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from la import config
from la.base.phases import (
    CODE_DUPLICATE_CALL,
    CODE_PHASE_BUDGET_EXCEEDED,
    CODE_PHASE_FORBIDS_TOOLS,
    CODE_TOTAL_STEPS_EXCEEDED,
    LIMIT_ARCH_CALLS,
    LIMIT_QA_CALLS,
    LIMIT_SUBAGENTS,
    LIMIT_TOTAL_STEPS,
    PHASE_ADJUDICATE,
    PHASE_ARCH,
    PHASE_QA,
    CircuitBreaker,
)
from la.agent.session import MemorySession
from la.llm.fake import FakeClient
from la.memory.adjudicator import (
    BASIS_ARCHIVE,
    BASIS_NONE,
    adjudicate,
    detect_conflicts,
    render_evidence_block,
)
from la.memory.consolidate import consolidate, merge_summary_text, parse_sections
from la.memory.scratchpad import Evidence, Scratchpad
from la.memory.store import MemoryStore
from la.memory.timerange import parse_range
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY
from la.tools.registry import (
    CODE_NEEDS_CONFIRMATION,
    CODE_TIMEOUT,
    CODE_UNKNOWN_TOOL,
    RISK_CONFIRM,
    RISK_LOW,
    ToolRegistry,
    ToolSpec,
    clip_head_tail,
    mask_secrets,
)
from la.tools.subagent import (
    STOP_TIMEOUT,
    SubAgentReport,
    SubAgentRunner,
    SubAgentTask,
)

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 8, 19, 0, tzinfo=TZ)


def make_settings(session_id: str = "s1") -> config.Settings:
    return dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id=session_id
    )


# ==========================================================================
# 纯函数与门禁
# ==========================================================================
class TimeRangeTestCase(unittest.TestCase):
    def test_last_wednesday_morning(self):
        rng = parse_range("上周三上午", NOW)
        self.assertIsNotNone(rng)
        self.assertEqual(rng.start.weekday(), 2, "必须是周三")
        self.assertEqual(rng.start.hour, 6)
        self.assertEqual(rng.end.hour, 12)
        self.assertLess(rng.start, NOW)
        this_week = parse_range("这周三上午", NOW).start
        self.assertEqual((this_week - rng.start).days, 7)

    def test_relative_days(self):
        self.assertEqual(parse_range("今天", NOW).start.date(), NOW.date())
        self.assertEqual(
            parse_range("昨天", NOW).start.date(), (NOW - timedelta(days=1)).date()
        )
        self.assertEqual(
            parse_range("3 天前", NOW).start.date(), (NOW - timedelta(days=3)).date()
        )

    def test_absolute_dates(self):
        self.assertEqual(parse_range("2026-05-10 的事", NOW).start.month, 5)
        self.assertEqual(parse_range("5月10日", NOW).start.month, 5)
        self.assertEqual(parse_range("5/10", NOW).start.day, 10)

    def test_period_only_means_today(self):
        rng = parse_range("下午的事", NOW)
        self.assertEqual(rng.start.hour, 12)
        self.assertEqual(rng.end.hour, 18)
        self.assertEqual(rng.start.date(), NOW.date())

    def test_unknown_returns_none_instead_of_guessing(self):
        self.assertIsNone(parse_range("随便聊聊", NOW))
        self.assertIsNone(parse_range("", NOW))


class CircuitBreakerTestCase(unittest.TestCase):
    def test_phase_budgets_are_enforced(self):
        breaker = CircuitBreaker(phase=PHASE_QA)
        for index in range(LIMIT_QA_CALLS):
            self.assertTrue(breaker.admit("query_summary_pool", {"q": index}).allowed)
        denied = breaker.admit("query_summary_pool", {"q": "third"})
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.code, CODE_PHASE_BUDGET_EXCEEDED)
        self.assertEqual(breaker.phase, PHASE_ADJUDICATE, "超预算必须强制进裁决阶段")

    def test_arch_budget(self):
        breaker = CircuitBreaker(phase=PHASE_ARCH)
        for index in range(LIMIT_ARCH_CALLS):
            self.assertTrue(breaker.admit("query_timestamp_range", {"i": index}).allowed)
        self.assertFalse(breaker.admit("query_timestamp_range", {"i": 99}).allowed)

    def test_total_steps_cap(self):
        breaker = CircuitBreaker(phase=PHASE_ARCH)
        breaker.total_steps = LIMIT_TOTAL_STEPS
        denied = breaker.admit("query_timestamp_range", {"start": "a", "end": "b"})
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.code, CODE_TOTAL_STEPS_EXCEEDED)
        self.assertEqual(breaker.phase, PHASE_ADJUDICATE)

    def test_duplicate_call_is_rejected(self):
        breaker = CircuitBreaker(phase=PHASE_ARCH)
        self.assertTrue(breaker.admit("query_timestamp_range", {"start": "a", "end": "b"}).allowed)
        denied = breaker.admit("query_timestamp_range", {"end": "b", "start": "a"})
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.code, CODE_DUPLICATE_CALL, "参数顺序不同也算同一次调用")

    def test_adjudicate_forbids_all_tools(self):
        breaker = CircuitBreaker(phase=PHASE_ADJUDICATE)
        denied = breaker.admit("query_summary_pool", {"q": "x"})
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.code, CODE_PHASE_FORBIDS_TOOLS)

    def test_subagent_quota(self):
        breaker = CircuitBreaker(phase=PHASE_ARCH)
        self.assertTrue(breaker.allow_subagents(4).allowed)
        self.assertFalse(breaker.allow_subagents(1).allowed)
        breaker2 = CircuitBreaker(phase=PHASE_ARCH)
        self.assertFalse(breaker2.allow_subagents(LIMIT_SUBAGENTS + 1).allowed)


class ToolRegistryTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_tool_and_risk_gate(self):
        registry = ToolRegistry()
        breaker = CircuitBreaker(phase=PHASE_ARCH)

        async def handler(params):
            return "ok"

        registry.register(
            ToolSpec("needs_confirm", "d", PHASE_ARCH, RISK_CONFIRM, 1000, handler)
        )
        unknown = await registry.execute("nope", {}, breaker=breaker)
        self.assertEqual(unknown.error_code, CODE_UNKNOWN_TOOL)

        denied = await registry.execute("needs_confirm", {}, breaker=breaker)
        self.assertEqual(denied.error_code, CODE_NEEDS_CONFIRMATION)

        approving = ToolRegistry(risk_approver=lambda spec, params: True)
        approving.register(
            ToolSpec("needs_confirm", "d", PHASE_ARCH, RISK_CONFIRM, 1000, handler)
        )
        ok = await approving.execute("needs_confirm", {}, breaker=breaker)
        self.assertTrue(ok.ok)

    async def test_timeout_is_reported_not_raised(self):
        registry = ToolRegistry()

        async def slow(params):
            await asyncio.sleep(1.0)
            return "late"

        registry.register(ToolSpec("slow", "d", PHASE_ARCH, RISK_LOW, 50, slow))
        result = await registry.execute("slow", {}, breaker=CircuitBreaker(phase=PHASE_ARCH))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, CODE_TIMEOUT)

    async def test_missing_required_params(self):
        registry = ToolRegistry()

        async def handler(params):
            return "ok"

        registry.register(
            ToolSpec("t", "d", PHASE_ARCH, RISK_LOW, 1000, handler, required=("start",))
        )
        result = await registry.execute("t", {}, breaker=CircuitBreaker(phase=PHASE_ARCH))
        self.assertFalse(result.ok)
        self.assertIn("start", result.message)

    def test_post_process_truncates_and_masks(self):
        text, truncated = clip_head_tail("x" * 5000, 1000)
        self.assertTrue(truncated)
        self.assertIn("中间省略", text)
        self.assertLess(len(text), 5000)
        masked = mask_secrets("身份证 110101199001011234 与卡号 6222021234567890123")
        self.assertNotIn("110101199001011234", masked)
        self.assertIn("[敏感性信息已省略]", masked)


class AdjudicatorTestCase(unittest.TestCase):
    def _pad(self) -> Scratchpad:
        pad = Scratchpad()
        pad.add(
            Evidence(
                source="timestamp_range",
                timestamp="2026-09-30T09:00:00+08:00",
                content="装修预算敲定为 5 万",
                seq=3,
            )
        )
        pad.add(
            Evidence(
                source="timestamp_range",
                timestamp="2026-09-30T11:30:00+08:00",
                content="装修预算改成 6 万了",
                seq=5,
            )
        )
        return pad

    def test_conflict_detected_and_latest_wins(self):
        pad = self._pad()
        conflicts = detect_conflicts(pad.sorted_evidence(), ["装修预算"])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].winner_values, ["6万"])
        self.assertIn("以最新为准", conflicts[0].note)

    def test_adjudicate_prefers_archive_basis(self):
        verdict = adjudicate(self._pad(), terms=["装修预算"])
        self.assertEqual(verdict.basis, BASIS_ARCHIVE)
        self.assertTrue(verdict.conflicts)
        self.assertIn("找到了 2 条相关记录", verdict.note)
        self.assertEqual(verdict.latest().timestamp, "2026-09-30T11:30:00+08:00")

    def test_zero_evidence_gives_honest_template(self):
        verdict = adjudicate(Scratchpad(), terms=["装修预算"])
        self.assertEqual(verdict.basis, BASIS_NONE)
        self.assertIn("没有找到相关记录", verdict.note)

    def test_evidence_block_respects_budget(self):
        pad = Scratchpad()
        for index in range(40):
            pad.add(
                Evidence(
                    source="sub_agent",
                    timestamp="2026-09-30T09:%02d:00+08:00" % index,
                    content="很长的证据内容" * 40,
                    seq=index,
                )
            )
        verdict = adjudicate(pad, terms=["证据"], budget_tokens=config.BUDGET_EVIDENCE)
        block = render_evidence_block(verdict, budget_tokens=config.BUDGET_EVIDENCE)
        self.assertLessEqual(config.estimate_tokens(block), config.BUDGET_EVIDENCE)
        self.assertIn("<evidence-status>found</evidence-status>", block)


class SubAgentTestCase(unittest.IsolatedAsyncioTestCase):
    class _Digest:
        def __init__(
            self,
            fail_on: int | None = None,
            sleep_on: int | None = None,
            body: str = "内容" * 200,
        ) -> None:
            self.fail_on = fail_on
            self.sleep_on = sleep_on
            self.body = body

        async def run(self, task: SubAgentTask) -> SubAgentReport:
            index = int(task.label.replace("w", "") or 0)
            if self.fail_on == index:
                raise RuntimeError("模拟子代理崩溃")
            if self.sleep_on == index:
                await asyncio.sleep(0.5)
            from la.tools.subagent import Finding, STATUS_FOUND

            return SubAgentReport(
                agent_id="",
                status=STATUS_FOUND,
                stop_reason="completed",
                findings=[
                    Finding(timestamp="2026-09-30T09:00:00+08:00", content=self.body, seq=index)
                ],
                task=task,
            )

    def _tasks(self) -> list[SubAgentTask]:
        return [
            SubAgentTask(query="q", start="2026-09-23T00:00:00+08:00",
                         end="2026-09-25T00:00:00+08:00", label="w" + str(i))
            for i in range(1, 5)
        ]

    async def test_one_failure_does_not_break_the_rest(self):
        runner = SubAgentRunner(self._Digest(fail_on=2), timeout_s=5)
        reports = await runner.spawn(self._tasks())
        self.assertEqual(len(reports), 4)
        statuses = [report.status for report in reports]
        self.assertEqual(statuses.count("failed"), 1)
        self.assertEqual(statuses.count("found"), 3)
        failed = [r for r in reports if r.status == "failed"][0]
        self.assertIn("模拟子代理崩溃", failed.note)

    async def test_timeout_is_isolated(self):
        runner = SubAgentRunner(self._Digest(sleep_on=3), timeout_s=0.1)
        reports = await runner.spawn(self._tasks())
        timed_out = [r for r in reports if r.stop_reason == STOP_TIMEOUT]
        self.assertEqual(len(timed_out), 1)
        self.assertEqual(timed_out[0].agent_id, "a3")

    async def test_output_is_truncated_to_token_budget(self):
        runner = SubAgentRunner(self._Digest(body="内容" * 600), timeout_s=5, max_tokens_per_agent=200)
        reports = await runner.spawn(self._tasks())
        for report in reports:
            text = "\n".join(item.content for item in report.findings)
            self.assertLessEqual(config.estimate_tokens(text), 200)
            self.assertEqual(report.stop_reason, "max_tokens")


# ==========================================================================
# 场景验收
# ==========================================================================
class ScenarioFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = self.tmp / "archive"
        self.db_path = self.tmp / "memory.db"
        self.assistant_conn = db.connect(Path(self._tmp.name) / "assistant.db")
        db.migrate(self.assistant_conn, MIGRATIONS_ASSISTANT)
        self._open()

    def tearDown(self) -> None:
        for conn in (getattr(self, "conn", None), self.assistant_conn):
            try:
                conn.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def _open(self) -> None:
        self.conn = db.connect(self.db_path)
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.archive)

    def restart(self) -> None:
        self.conn.close()
        self._open()

    def new_session(
        self,
        *,
        session_id: str = "s1",
        client=None,
        auto_approve: bool = False,
        direct_write: bool = False,
    ):
        session = MemorySession(
            client or FakeClient(),
            make_settings(session_id),
            store=self.store,
            session_id=session_id,
            assistant_conn=self.assistant_conn,
            auto_approve=auto_approve,
            # M7:这套用例守的是"确认门禁"本身的老行为,所以显式关掉直写。
            # 直写模式的守门测试在 tests/test_m7.py。
            direct_write=direct_write,
        )
        session.start()
        return session

    def seed(self, session_id: str, pairs, *, base: datetime) -> None:
        for offset, (role, text) in enumerate(pairs):
            self.store.append(
                session_id,
                "user/message" if role == "user" else "assistant/message",
                {"text": text},
                time=(base + timedelta(minutes=offset)).isoformat(timespec="seconds"),
            )


class ScenarioS3(ScenarioFixture):
    """S3:QA 未命中 -> ARCH 时间检索命中原文 -> 冲突按最新采信并附说明。"""

    async def test_recovers_from_archive_and_resolves_conflict(self):
        now = make_settings().now()
        target = parse_range("上周三上午", now)
        self.assertIsNotNone(target, "测试前置:中文时间必须能解析")

        self.seed(
            "s1",
            [
                ("user", "装修预算敲定为 5 万,先按这个准备"),
                ("assistant", "好,按 5 万记下了"),
                ("user", "装修预算改成 6 万了,材料涨了"),
                ("assistant", "收到,已更新为 6 万"),
            ],
            base=target.start + timedelta(hours=1),
        )

        client = FakeClient()
        session = self.new_session(client=client)
        reply = await session.respond("上周三上午我们说的装修预算是多少")

        outcome = session.last_outcome
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.qa.status, "miss", "摘要池里没有,必须靠考古")
        self.assertTrue(outcome.archaeology_ran)
        self.assertEqual(outcome.verdict.basis, BASIS_ARCHIVE)
        self.assertEqual(len(outcome.verdict.conflicts), 1)
        self.assertEqual(outcome.verdict.conflicts[0].winner_values, ["6万"])

        self.assertIn("6 万", reply.text)
        self.assertIn("以最新为准", reply.text)
        self.assertRegex(reply.text, r"\d{4}-\d{2}-\d{2}", "回复必须带时间")

        # 证据块确实进了投影,而且没有超预算
        system_text = "\n".join(
            m["content"] for m in client.calls[-1] if m["role"] == "system"
        )
        self.assertIn("<evidence-status>found</evidence-status>", system_text)
        self.assertIn("<conflict-note>", system_text)

    async def test_tool_calls_are_audited(self):
        now = make_settings().now()
        target = parse_range("上周三上午", now)
        self.seed(
            "s1",
            [("user", "装修预算敲定为 5 万")],
            base=target.start + timedelta(hours=1),
        )
        session = self.new_session()
        await session.respond("上周三上午我们说的装修预算是多少")
        rows = self.assistant_conn.execute(
            "SELECT action, actor FROM ops_audit ORDER BY id"
        ).fetchall()
        actions = [row["action"] for row in rows]
        self.assertIn("tool.query_summary_pool", actions)
        self.assertIn("tool.query_timestamp_range", actions)
        self.assertTrue(all(row["actor"] == "agent" for row in rows))


class ScenarioS4(ScenarioFixture):
    """S4:显式记住的事实,重启后仍能直接命中(热度还在高位)。"""

    async def test_memory_note_survives_restart(self):
        session = self.new_session()
        await session.respond("帮我记住:项目X 报价 6月15日截止")
        facts = self.store.active_facts()
        self.assertTrue(any("项目X" in f.content for f in facts), facts)
        self.assertTrue(any("6月15日" in f.content for f in facts), facts)

        self.restart()
        client = FakeClient()
        revived = self.new_session(client=client)
        reply = await revived.respond("项目X 的报价什么时候截止?")
        self.assertEqual(revived.last_retrieval.status, "hit")
        self.assertIn("6月15日", reply.text)


class ScenarioS7(ScenarioFixture):
    """S7:4 个子代理并行翻 4 个时段;合并证据 <=2500 token;失败互不影响。"""

    def _seed_scattered(self, now: datetime) -> None:
        target = parse_range("上周三上午", now)
        # 放在窄窗口之外、但落在 7 天扩查窗之内 —— 只有派子代理才找得到
        base = target.start - timedelta(days=3)
        self.seed(
            "s1",
            [
                ("user", "方案就叫星火计划吧"),
                ("assistant", "好,记录为星火计划"),
            ],
            base=base,
        )

    async def test_four_agents_cover_widened_windows(self):
        now = make_settings().now()
        self._seed_scattered(now)

        session = self.new_session(auto_approve=True)
        reply = await session.respond("上周三上午我们说的那个方案叫什么")

        outcome = session.last_outcome
        self.assertEqual(len(outcome.reports), 4, "必须派满 4 个子代理")
        self.assertTrue(all(report.stop_reason != STOP_TIMEOUT for report in outcome.reports))
        self.assertTrue(all(report.elapsed_ms < 120000 for report in outcome.reports))

        block = render_evidence_block(outcome.verdict, budget_tokens=config.BUDGET_EVIDENCE)
        self.assertLessEqual(config.estimate_tokens(block), config.BUDGET_EVIDENCE)
        self.assertIn("星火计划", reply.text)

    async def test_confirm_gate_blocks_subagents_without_approval(self):
        now = make_settings().now()
        self._seed_scattered(now)
        session = self.new_session(auto_approve=False)
        await session.respond("上周三上午我们说的那个方案叫什么")
        outcome = session.last_outcome
        self.assertEqual(outcome.reports, [], "没批准就不该派子代理")
        codes = [result.error_code for result in outcome.tool_results]
        self.assertIn(CODE_NEEDS_CONFIRMATION, codes)


class ConsolidationTestCase(ScenarioFixture):
    def test_merge_keeps_sections_and_dedupes_anchors(self):
        existing = (
            "<compacted-summary>\n- 时段:2026-09-30T09:00 — 09:01\n- 事项:聊装修\n"
            "- 事实:装修预算敲定为 5 万\n- 偏好:(none)\n- 待办:(none)\n"
            "- 锚点:[5 万]\n- 其他:(none)\n</compacted-summary>"
        )
        merged, added = merge_summary_text(
            existing,
            [
                Evidence(
                    source="timestamp_range",
                    timestamp="2026-09-30T11:30:00+08:00",
                    content="装修预算改成 6 万了",
                )
            ],
            start_time="2026-09-30T09:00:00+08:00",
            end_time="2026-09-30T11:30:00+08:00",
        )
        sections = parse_sections(merged)
        self.assertEqual(set(sections), {"时段", "事项", "事实", "偏好", "待办", "锚点", "其他"})
        self.assertIn("5 万", sections["事实"])
        self.assertIn("6 万", sections["事实"])
        self.assertIn("6 万", sections["锚点"])
        self.assertEqual(added, 1)

    async def test_consolidation_is_idempotent(self):
        session = self.new_session()
        self.store.append(session.session_id, "user/message", {"text": "装修预算 6 万"})
        evidence = [
            Evidence(
                source="timestamp_range",
                timestamp="2026-09-30T11:30:00+08:00",
                content="装修预算改成 6 万了",
                seq=1,
            )
        ]
        kwargs = dict(
            session_id=session.session_id,
            start_seq=1,
            end_seq=1,
            rounds_after_reply=1,
            evidence=evidence,
            start_time="2026-09-30T11:30:00+08:00",
            end_time="2026-09-30T11:30:00+08:00",
        )
        first = await consolidate(self.store, **kwargs)
        self.assertTrue(first.applied)
        second = await consolidate(self.store, **kwargs)
        self.assertFalse(second.applied)
        self.assertEqual(second.reason, "ALREADY_CONSOLIDATED")
        self.assertEqual(len(self.store.all_summaries()), 1, "同一桶只应有一行")

    async def test_archaeology_backfills_summary_so_next_time_qa_hits(self):
        now = make_settings().now()
        target = parse_range("上周三上午", now)
        self.seed(
            "s1",
            [("user", "装修预算敲定为 6 万")],
            base=target.start + timedelta(hours=1),
        )
        session = self.new_session()
        await session.respond("上周三上午我们说的装修预算是多少")
        self.assertTrue(self.store.all_summaries(), "考古成功后必须回填摘要")

        probe = self.new_session(client=FakeClient())
        await probe.respond("上周三上午我们说的装修预算是多少")
        self.assertEqual(probe.last_retrieval.status, "hit", "回填后 QA 应当直接命中")


if __name__ == "__main__":
    unittest.main()
