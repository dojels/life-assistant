"""M4.0 验收测试:服务层地基、schema 单真源、发件箱、审计、function calling。

核心可验收标准(能力域 §6.3 / 执行计划 G3):
    **同一份 Pydantic 模型,同时生成 REST 请求体与 LLM 工具的 JSON Schema;
      两者调用同一个 service 方法。**
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from la import config
from la.base import phases
from la.base.phases import (
    LIMIT_ANSWER_ROUNDS,
    LIMIT_ANSWER_TOOL_CALLS,
    PHASE_ADJUDICATE,
    PHASE_ANSWER,
    CircuitBreaker,
)
from la.agent.session import MemorySession
from la.domain.audit import AuditSink
from la.domain.outbox import OutboxRelay
from la.domain.routers.ledger import LedgerRouter
from la.domain.schemas.ledger import ExpenseCreate, ExpenseQuery
from la.domain.services.ledger_service import (
    LedgerError,
    LedgerService,
    UnknownCategoryError,
    period_bounds,
)
from la.domain.tools import build_ledger_tools
from la.llm.client import ToolCall
from la.llm.fake import FakeClient
from la.llm.tools import build_tool_catalog, catalog_fingerprint, to_openai_tool
from la.tools.registry import ToolInputError
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=TZ)


def make_settings(session_id: str = "s1") -> config.Settings:
    return dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id=session_id
    )


class Fixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.assistant = db.connect(self.tmp / "assistant.db")
        db.migrate(self.assistant, MIGRATIONS_ASSISTANT)
        self.memory = db.connect(self.tmp / "memory.db")
        db.migrate(self.memory, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.memory, archive_dir=self.tmp / "archive")
        self.service = LedgerService(
            self.assistant, audit=AuditSink(self.assistant), now_provider=lambda: NOW
        )

    def tearDown(self) -> None:
        for conn in (self.assistant, self.memory):
            try:
                conn.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def new_session(self, client=None, **kwargs) -> MemorySession:
        session = MemorySession(
            client or FakeClient(),
            make_settings(),
            store=self.store,
            session_id="s1",
            assistant_conn=self.assistant,
            auto_briefing=False,
            **kwargs,
        )
        session.start()
        return session


# ==========================================================================
# 地基验收:schema 单真源
# ==========================================================================
class SchemaSingleSourceTestCase(Fixture):
    def test_tool_parameters_come_from_the_same_pydantic_model(self):
        specs = {spec.name: spec for spec in build_ledger_tools(self.service)}
        record_spec = specs["expense_record"]
        self.assertEqual(
            record_spec.parameters_json_schema(),
            ExpenseCreate.model_json_schema(),
            "工具参数必须直接来自 ExpenseCreate,不能手写第二份",
        )

    def test_openai_tool_shape_uses_the_same_schema(self):
        specs = {spec.name: spec for spec in build_ledger_tools(self.service)}
        payload = to_openai_tool(specs["expense_record"])
        self.assertEqual(payload["type"], "function")
        self.assertEqual(payload["function"]["name"], "expense_record")
        self.assertEqual(
            payload["function"]["parameters"]["properties"]["amount_cents"]["type"], "integer"
        )
        self.assertIn("分", payload["function"]["parameters"]["properties"]["amount_cents"]["description"])

    def test_router_and_tool_share_one_service(self):
        router = LedgerRouter(self.service)
        status, body = router.create_expense(
            {"amount_cents": 3800, "payee": "老张牛肉面", "category": "餐饮"}
        ).as_tuple()
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "draft")

        record = self.service.read(body["id"])
        self.assertEqual(record.amount_cents, 3800)
        self.assertEqual(sum(amount for _account, amount in record.postings), 0, "分录之和必须为零")

    def test_router_rejects_bad_body(self):
        router = LedgerRouter(self.service)
        status, body = router.create_expense({"amount_cents": -5}).as_tuple()
        self.assertEqual(status, 400)
        status, body = router.create_expense(
            {"amount_cents": 100, "category": "不存在的分类"}
        ).as_tuple()
        self.assertEqual(status, 422)
        self.assertIn("可用分类", body["message"], "错误信息必须可操作")


# ==========================================================================
# 服务层
# ==========================================================================
class LedgerServiceTestCase(Fixture):
    def test_expense_is_written_as_draft_with_two_balanced_legs(self):
        record = self.service.record_expense(
            ExpenseCreate(amount_cents=3800, payee="老张牛肉面", category="餐饮")
        )
        self.assertEqual(record.status, "draft")
        self.assertEqual(len(record.postings), 2)
        self.assertEqual(sum(amount for _account, amount in record.postings), 0)

    def test_idempotency_key_prevents_double_recording(self):
        first = self.service.record_expense(
            ExpenseCreate(amount_cents=500, category="交通"), idempotency_key="k1"
        )
        second = self.service.record_expense(
            ExpenseCreate(amount_cents=500, category="交通"), idempotency_key="k1"
        )
        self.assertEqual(first.id, second.id)
        count = self.assistant.execute("SELECT count(*) FROM app_txns").fetchone()[0]
        self.assertEqual(count, 1)

    def test_outbox_row_is_written_in_the_same_transaction(self):
        self.service.record_expense(ExpenseCreate(amount_cents=100, category="餐饮"))
        rows = self.assistant.execute(
            "SELECT event_type, payload, idempotency_key FROM ops_outbox"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "ledger.txn.created")
        payload = json.loads(rows[0]["payload"])
        self.assertEqual(payload["amount_cents"], 100)

    def test_audit_records_who_wrote_it(self):
        self.service.record_expense(
            ExpenseCreate(amount_cents=100, category="餐饮"), actor="agent", session_id="s1"
        )
        rows = self.assistant.execute("SELECT actor, action FROM ops_audit").fetchall()
        self.assertEqual([(row["actor"], row["action"]) for row in rows],
                         [("agent", "ledger.txn.create")])

    def test_unknown_account_gives_actionable_message(self):
        with self.assertRaises(LedgerError):
            self.service.record_expense(
                ExpenseCreate(amount_cents=100, account="不存在的卡")
            )

    def test_query_only_counts_confirmed_by_default(self):
        first = self.service.record_expense(ExpenseCreate(amount_cents=1000, category="餐饮"))
        self.service.record_expense(ExpenseCreate(amount_cents=2000, category="交通"))
        self.service.confirm(first.id)

        default = self.service.query_expenses(ExpenseQuery(period="2026-10"))
        self.assertEqual(default.total_cents, 1000)
        self.assertEqual(default.txn_count, 1)

        with_drafts = self.service.query_expenses(
            ExpenseQuery(period="2026-10", include_drafts=True)
        )
        self.assertEqual(with_drafts.total_cents, 3000)
        self.assertEqual(with_drafts.txn_count, 2)

    def test_query_groups_by_category(self):
        self.service.record_expense(ExpenseCreate(amount_cents=1000, category="餐饮"))
        self.service.record_expense(ExpenseCreate(amount_cents=2000, category="餐饮"))
        self.service.record_expense(ExpenseCreate(amount_cents=500, category="交通"))
        result = self.service.query_expenses(
            ExpenseQuery(period="2026-10", include_drafts=True)
        )
        groups = {row.key: row.total_cents for row in result.groups}
        self.assertEqual(groups, {"餐饮": 3000, "交通": 500})
        self.assertEqual(result.source, "ledger", "回答必须能标注来源")

    def test_period_bounds(self):
        self.assertEqual(period_bounds("2026-10", NOW)[0][:7], "2026-10")
        self.assertEqual(period_bounds("2026-Q4", NOW)[0][:7], "2026-10")
        self.assertEqual(period_bounds("2026", NOW)[0][:4], "2026")
        self.assertEqual(period_bounds("本月", NOW)[0][:7], "2026-10")
        self.assertEqual(period_bounds("上月", NOW)[0][:7], "2026-09")
        # ⚠️ 2026-11-17:模型对「今天」最自然的写法是单个日期;它必须被收下,
        #    否则会抛 LedgerError → HANDLER_ERROR → 自省报表误记成工具故障。
        self.assertEqual(period_bounds("2026-10-08", NOW), (
            "2026-10-08T00:00:00+08:00", "2026-10-09T00:00:00+08:00",
        ))
        self.assertEqual(period_bounds("今天", NOW)[0][:10], "2026-10-08")
        with self.assertRaises(LedgerError):
            period_bounds("上个季度", NOW)
        with self.assertRaises(LedgerError):
            period_bounds("2026-13-40", NOW)


# ==========================================================================
# 发件箱中继
# ==========================================================================
class OutboxTestCase(Fixture):
    def _relay(self) -> OutboxRelay:
        return OutboxRelay(
            self.assistant,
            append_event=lambda kind, data: self.store.append("domain", kind, data),
            seen=self._memory_has_key,
        )

    def _memory_has_key(self, key: str) -> bool:
        row = self.memory.execute(
            "SELECT 1 FROM events WHERE data LIKE ? AND data LIKE ? LIMIT 1",
            ("%outbox_key%", "%" + key + "%"),
        ).fetchone()
        return row is not None

    def test_relay_moves_events_into_the_archive(self):
        self.service.record_expense(ExpenseCreate(amount_cents=100, category="餐饮"))
        report = self._relay().drain()
        self.assertEqual(report.relayed, 1)
        events = [e for e in self.store.events("domain") if e.type == "ledger.txn.created"]
        self.assertEqual(len(events), 1)
        self.assertIn("outbox_key", events[0].data)

        pending = self.assistant.execute(
            "SELECT count(*) FROM ops_outbox WHERE processed_at IS NULL"
        ).fetchone()[0]
        self.assertEqual(pending, 0)

    def test_relay_is_idempotent_even_if_marker_is_lost(self):
        self.service.record_expense(ExpenseCreate(amount_cents=100, category="餐饮"))
        self._relay().drain()
        # 模拟"搬完了但标记没落盘":清掉 processed_at 再搬一次
        self.assistant.execute("UPDATE ops_outbox SET processed_at = NULL")
        self.assistant.commit()
        report = self._relay().drain()
        self.assertEqual(report.relayed, 0)
        self.assertEqual(report.skipped, 1, "靠 outbox_key 认出已经搬过")
        events = [e for e in self.store.events("domain") if e.type == "ledger.txn.created"]
        self.assertEqual(len(events), 1, "归档里不能出现重复事件")


# ==========================================================================
# function calling
# ==========================================================================
class ToolLoopTestCase(Fixture):
    async def test_session_executes_model_requested_tool(self):
        client = FakeClient(
            tool_script=[
                [ToolCall(id="c1", name="expense_record",
                          arguments=json.dumps({"amount_cents": 3800, "payee": "老张牛肉面",
                                                "category": "餐饮"}))]
            ]
        )
        session = self.new_session(client=client)
        reply = await session.respond("午饭花了 38 块")

        count = self.assistant.execute("SELECT count(*) FROM app_txns").fetchone()[0]
        self.assertEqual(count, 1, "模型请求的工具必须真的被执行")
        self.assertIn("工具返回", reply.text)
        calls = self.assistant.execute("SELECT actor, action FROM ops_audit").fetchall()
        self.assertEqual(calls[0]["actor"], "agent")

    async def test_tool_catalog_is_stable_across_turns(self):
        client = FakeClient()
        session = self.new_session(client=client)
        await session.respond("你好")
        await session.respond("再见")
        first, second = client.tool_catalogs[0], client.tool_catalogs[1]
        self.assertTrue(first, "工具清单不能为空")
        self.assertEqual(catalog_fingerprint(first), catalog_fingerprint(second),
                         "工具清单每轮必须一致,否则 KV 前缀缓存全废")
        names = [item["function"]["name"] for item in first]
        self.assertEqual(names, sorted(names), "必须按名字排序")
        self.assertIn("expense_record", names)
        self.assertIn("memory_note", names)

    async def test_expense_query_accepts_a_single_day_and_bad_period_is_bad_params(self):
        specs = {spec.name: spec for spec in build_ledger_tools(self.service)}
        handler = specs["expense_query"].handler
        result = await handler({"period": "2026-10-08"})
        self.assertEqual(result["start"][:10], "2026-10-08")
        self.assertEqual(result["end"][:10], "2026-10-09")
        # 参数写错要翻译成 ToolInputError(注册表据此记 BAD_PARAMS,不再算真故障)。
        with self.assertRaises(ToolInputError):
            await handler({"period": "2026-13-40"})

    async def test_tool_error_is_fed_back_not_raised(self):
        client = FakeClient(
            tool_script=[[ToolCall(id="c1", name="expense_record",
                                   arguments=json.dumps({"amount_cents": 100,
                                                         "category": "不存在的分类"}))]]
        )
        session = self.new_session(client=client)
        reply = await session.respond("记一笔")
        tool_messages = [m for m in client.calls[-1] if m.get("role") == "tool"]
        self.assertTrue(tool_messages)
        self.assertIn("error", tool_messages[-1]["content"])
        self.assertIn("可用分类", tool_messages[-1]["content"])
        self.assertTrue(reply.text, "工具失败也必须给出答复,不能崩")

    def test_answer_phase_allows_tools_but_adjudicate_does_not(self):
        breaker = CircuitBreaker(phase=PHASE_ADJUDICATE)
        self.assertFalse(breaker.admit("expense_record", {}).allowed)
        breaker.enter(PHASE_ANSWER)
        allowed = sum(1 for i in range(LIMIT_ANSWER_TOOL_CALLS + 2)
                      if breaker.admit("expense_record", {"i": i}).allowed)
        self.assertEqual(allowed, LIMIT_ANSWER_TOOL_CALLS, "回答阶段有自己的配额")

    def test_answer_phase_reads_and_writes_share_one_pool(self):
        """D101:读/写**共用一池**(照云朵的 AGENT_MAX_TOOL_CALLS=40)。

        D99 那条真机场景(查完资料还要交付)仍然必须过 —— 只是它现在靠
        **池子够大**,而不是靠分账。分账治的是症状("查资料把交付挤没"),
        单池 40 治的是病根(池子太小):真机那轮(2 搜索 + 4 抓网页)在 40 格里只是热身。
        """
        breaker = CircuitBreaker(phase=PHASE_ANSWER)
        for index in range(6):
            self.assertTrue(
                breaker.admit("web_fetch", {"url": "https://x/" + str(index)}).allowed
            )
        self.assertTrue(
            breaker.admit("artifact_write", {"name": "龙翔桥攻略.md"}, writes=True).allowed,
            "查了 6 次之后,交付必须还做得出来(D99 的真机场景)",
        )
        self.assertEqual(breaker.answer_tool_calls, 7, "读和写走同一个计数器")
        self.assertEqual(breaker.answer_write_calls, 1, "写入次数只统计,不再单独设闸")

        for index in range(LIMIT_ANSWER_TOOL_CALLS - 7):
            self.assertTrue(breaker.admit("todo_list", {"n": index}).allowed)
        denied = breaker.admit("todo_list", {"n": "over"})
        self.assertFalse(denied.allowed, "到顶必须挡住")
        self.assertIn("按已经拿到的结果", denied.detail, "要指路,不能只说'不行'")
        self.assertNotIn("再发一条", denied.detail, "别把交付推回给用户(D99 的教训)")

    def test_repeated_call_in_the_answer_phase_is_reminded_not_blocked(self):
        """回答阶段的重复调用**只提醒不拦** —— 照云朵与 DSH 的定位:提醒是建议。

        理由:模型自己发起的调用,瞬时失败后用同样参数重试是**正当**的;
        拦死它会把"重试一次就好"变成"这轮做不完"。防打转交给总池那 40 格。
        (QA/ARCH 的调用是代码发起的,重复仍按 DUPLICATE_CALL 硬拒 —— 见 test_m2。)
        """
        breaker = CircuitBreaker(phase=PHASE_ANSWER)
        self.assertTrue(breaker.admit("web_fetch", {"url": "https://x"}).allowed)
        again = breaker.admit("web_fetch", {"url": "https://x"})
        self.assertTrue(again.allowed, "同参重试必须放行")
        self.assertEqual(again.code, "OK")
        self.assertIn("已经用完全相同的参数调用过", again.note)
        self.assertEqual(again.detail, "回答阶段调用 2")

    async def test_a_report_still_gets_written_after_many_research_calls(self):
        """端到端:连着查了 6 次之后,第 7 步的 artifact_write 仍然写得出来。"""
        with mock.patch.object(config, "ARTIFACTS_DIR", self.tmp / "artifacts"):
            rounds = [
                [ToolCall(id="r" + str(index), name="todo_list",
                          arguments=json.dumps({"filter": "pending", "n": index}))]
                for index in range(6)
            ]
            rounds.append([
                ToolCall(id="w1", name="artifact_write",
                         arguments=json.dumps({"name": "龙翔桥攻略.md",
                                               "content": "# 攻略\n- 一"}))
            ])
            client = FakeClient(tool_script=rounds, scripted=["写好了,发给你。"])
            session = self.new_session(client=client)
            reply = await session.respond("我明天要去龙翔桥玩,给我做份攻略")

            results = [
                event for event in session.store.events("s1")
                if event.type == "tool/result" and event.data.get("tool") == "artifact_write"
            ]
            self.assertTrue(results, "第 7 步必须真的调到 artifact_write")
            self.assertTrue(results[-1].data.get("ok"), results[-1].data)
            self.assertEqual(reply.text, "写好了,发给你。")
            written = [
                item for item in (self.tmp / "artifacts").rglob("*")
                if item.is_file()
            ]
            self.assertTrue(
                any(item.suffix == ".md" for item in written),
                "文件必须真的落盘:" + str(written),
            )

    async def test_tools_are_taken_away_once_the_budget_is_gone(self):
        """到顶后**每一轮都不再带工具**(云朵语义),并在账本里写明原因。

        真机教训(2026-10-12 11:55):额度用尽后模型还在发工具调用,每次都拿到一个
        "被拒"的结果,它就把这句话照抄给用户("你再发一条消息我就写出来")。
        云朵的做法是**直接不再给工具**:模型只能按现有结果成文。
        """
        steps: list = []
        rounds = [
            [ToolCall(id="t" + str(index), name="todo_list",
                      arguments=json.dumps({"filter": "pending", "n": index}))]
            for index in range(3)
        ]
        client = FakeClient(tool_script=rounds, scripted=["收尾了。"])
        session = self.new_session(client=client, on_step=lambda kind, data: steps.append(data))
        with mock.patch.object(phases, "LIMIT_ANSWER_TOOL_CALLS", 3), \
                mock.patch.object(phases, "LIMIT_ANSWER_ROUNDS", 6):
            reply = await session.respond("看看我的待办")

        self.assertEqual(reply.text, "收尾了。")
        self.assertTrue(client.tool_catalogs[0], "第一轮必须带工具")
        self.assertEqual(client.tool_catalogs[3], [], "额度用尽后必须收回工具")
        self.assertTrue(
            any("按现有结果收尾" in str(row.get("detail") or "") for row in steps),
            "账本里要写明原因 —— 用户得能分清'做完了'与'被叫停了':" + str(steps[-3:]),
        )

    def test_tool_call_arguments_are_parsed_safely(self):
        self.assertEqual(ToolCall("c", "t", "{bad json").parsed_arguments(), {})
        self.assertEqual(ToolCall("c", "t", "[1,2]").parsed_arguments(), {})
        self.assertEqual(ToolCall("c", "t", '{"a":1}').parsed_arguments(), {"a": 1})


class ValidationShapeTestCase(unittest.TestCase):
    def test_expense_create_rejects_non_positive_amount(self):
        with self.assertRaises(ValidationError):
            ExpenseCreate(amount_cents=0)
        with self.assertRaises(ValidationError):
            ExpenseCreate(amount_cents=200_000_000)

    def test_build_tool_catalog_orders_by_name(self):
        specs = build_ledger_tools(LedgerService.__new__(LedgerService))
        catalog = build_tool_catalog(specs)
        names = [item["function"]["name"] for item in catalog]
        self.assertEqual(names, sorted(names))


if __name__ == "__main__":
    unittest.main()
