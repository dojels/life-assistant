"""M13 验收测试:**改与删** —— 待办 / 记忆 / 提醒 / 文件,以及"我到底看得到什么"。

为什么单独立一个文件(而不是塞进 M12 的测试里):
  * M12 那批测的是"能读能看能写新东西",这批测的是**已有数据的变动**;
  * 五处改动有一条共同的验收线:**每一步都要留痕、都能撤销,而且撤销之后
    回到的是"上一版",不是"东西没了"**。

覆盖:
  1. 待办:改内容 / 改到期日 / 勾完成 / 重新打开 / 删除,以及删除后的撤销(原样放回来);
  2. 记忆:改口(supersede)与遗忘都不删除,撤销能把旧说法恢复生效;
  3. 提醒:改时刻不换 id、重复提醒改期后会重新触发、最小间隔仍然拦住;
  4. 文件:写工作区 .html、覆盖已有文件(留备份 + 可撤销)、产物仍然落产物区;
  5. 覆盖域:**只读查询顺带告诉我"一共多少行、从哪天到哪天"**。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_m13_crud -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from la import config
from la.base.phases import PHASE_ANSWER, CircuitBreaker
from la.base.tools import build_base_tools
from la.domain.artifacts import ArtifactStore
from la.domain.receipts import ReceiptLog
from la.domain.reverters import build_reverter
from la.domain.schemas.ledger import ExpenseQuery, TxnCreate
from la.domain.services.ledger_service import LedgerService
from la.domain.tools import build_ledger_tools
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY
from la.tasks.goals import GoalStore
from la.tasks.schedules import ScheduleStore
from la.tasks.todos import STATUS_COMPLETED, STATUS_PENDING, TodoStore
from la.tools.registry import ToolRegistry
from la.tools.task_tools import build_task_tools


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "ARTIFACTS_DIR", "ASSISTANT_DB", "MEMORY_DB")
        }
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"

        self.assistant = db.connect_assistant()
        db.migrate(self.assistant, MIGRATIONS_ASSISTANT)
        self.memory_conn = db.connect_memory()
        db.migrate(self.memory_conn, MIGRATIONS_MEMORY)

        self.memory = MemoryStore(self.memory_conn)
        self.memory_receipts = ReceiptLog(self.memory_conn)
        self.receipts = ReceiptLog(self.assistant)
        self.todos = TodoStore(self.memory_conn, receipts=self.memory_receipts)
        self.goals = GoalStore(self.memory_conn, receipts=self.memory_receipts)
        self.schedules = ScheduleStore(self.memory_conn, receipts=self.memory_receipts)
        self.artifacts = ArtifactStore(self.assistant, root=config.ARTIFACTS_DIR)
        self.ledger = LedgerService(self.assistant, receipts=self.receipts)

        def add_fact(content: str, *, fact_type: str = "other", supersedes: int | None = None,
                     confidential: bool = False) -> int:
            return self.memory.add_fact(
                content, session_id="t", fact_type=fact_type, supersedes=supersedes,
                confidential=confidential,
            )

        self.add_fact = add_fact
        self.task_tools = build_task_tools(
            todos=self.todos, goals=self.goals, schedules=self.schedules,
            library=None, add_fact=add_fact, memory=self.memory,
        )
        self.base_tools = build_base_tools(
            materials=None, artifacts=self.artifacts, receipts=self.receipts,
            session_id="t",
        )
        # ⚠️ direct_write=True 与线上默认一致(D65):memory_note 现在是 risk=confirm
        #    (设计深化 §3.5),这一批测试测的是"改口/遗忘"的语义,不是门禁本身。
        self.registry = ToolRegistry(direct_write=True)
        self.registry.register_all(self.task_tools)
        self.registry.register_all(self.base_tools)

        self.reverter = build_reverter(
            self.receipts,
            todos=self.todos, goals=self.goals, schedules=self.schedules,
            artifacts=self.artifacts, memory=self.memory,
        )
        self.memory_reverter = build_reverter(
            self.memory_receipts,
            todos=self.todos, goals=self.goals, schedules=self.schedules,
            artifacts=self.artifacts, memory=self.memory,
        )

    def tearDown(self) -> None:
        for conn in (self.assistant, self.memory_conn):
            try:
                conn.close()
            except Exception:
                pass
        for key, value in self._patched.items():
            setattr(config, key, value)
        self._tmp.cleanup()

    def call(self, name: str, params: dict):
        return asyncio.run(
            self.registry.execute(
                name, params, breaker=CircuitBreaker(phase=PHASE_ANSWER), session_id="t",
            )
        )

    def spec(self, name: str):
        for item in self.task_tools + self.base_tools:
            if item.name == name:
                return item
        raise AssertionError("没有这个工具: " + name)


# ==========================================================================
# 1. 待办:加 -> 改 -> 勾 -> 删
# ==========================================================================
class TodoCrudCase(Base):
    def add(self, content: str, due: str | None = None) -> str:
        params = {"content": content}
        if due:
            params["due_date_local"] = due
        result = self.call("todo_write", params)
        self.assertTrue(result.ok, result.message)
        return result.data["id"]

    def test_add_then_update_content(self) -> None:
        todo_id = self.add("写周报", due="2026-10-12")
        updated = self.call("todo_write", {"action": "update", "todo_id": todo_id,
                                           "content": "写月报"})
        self.assertTrue(updated.ok, updated.message)
        self.assertEqual(updated.data["before"], "写周报")
        self.assertEqual(self.todos.get(todo_id).content, "写月报")

    def test_set_due_date_and_clear_it(self) -> None:
        todo_id = self.add("交房租")
        moved = self.call("todo_write", {"action": "set_due", "todo_id": todo_id,
                                         "due_date_local": "2026-11-01"})
        self.assertTrue(moved.ok, moved.message)
        self.assertEqual(self.todos.get(todo_id).due_date_local, "2026-11-01")
        cleared = self.call("todo_write", {"action": "set_due", "todo_id": todo_id,
                                           "due_date_local": ""})
        self.assertTrue(cleared.ok, cleared.message)
        self.assertIsNone(self.todos.get(todo_id).due_date_local)

    def test_complete_reopen_and_list_split(self) -> None:
        todo_id = self.add("买牛奶")
        done = self.call("todo_write", {"action": "complete", "todo_id": todo_id})
        self.assertTrue(done.ok, done.message)
        self.assertEqual(self.todos.get(todo_id).status, STATUS_COMPLETED)

        pending = self.call("todo_list", {"filter": "pending"})
        self.assertEqual(pending.data["count"], 0)
        self.assertEqual(pending.data["coverage"]["done"], 1, "勾掉的那条要出现在覆盖域里")

        reopened = self.call("todo_write", {"action": "reopen", "todo_id": todo_id})
        self.assertTrue(reopened.ok, reopened.message)
        self.assertEqual(self.todos.get(todo_id).status, STATUS_PENDING)

    def test_delete_creates_receipt_and_undo_puts_the_row_back(self) -> None:
        todo_id = self.add("退掉那个订阅", due="2026-10-20")
        deleted = self.call("todo_write", {"action": "delete", "todo_id": todo_id})
        self.assertTrue(deleted.ok, deleted.message)
        self.assertIsNone(self.todos.get(todo_id))

        receipt = self.memory_receipts.recent(limit=1)[0]
        self.assertEqual(receipt.kind, "task.todo")
        note = self.memory_reverter.undo(receipt.id)["note"]
        self.assertIn("放回来", note)
        restored = self.todos.get(todo_id)
        self.assertIsNotNone(restored, "撤销删除要把原样放回来,而且是同一个 id")
        self.assertEqual(restored.content, "退掉那个订阅")
        self.assertEqual(restored.due_date_local, "2026-10-20")

    def test_completion_is_undoable(self) -> None:
        todo_id = self.add("打疫苗")
        self.call("todo_write", {"action": "complete", "todo_id": todo_id})
        receipt_id = self.memory_receipts.recent(limit=1)[0].id
        self.memory_reverter.undo(receipt_id)
        self.assertEqual(self.todos.get(todo_id).status, STATUS_PENDING)

    def test_unknown_action_and_missing_id_are_errors_with_guidance(self) -> None:
        bad = self.call("todo_write", {"action": "finish", "todo_id": "t-x"})
        self.assertFalse(bad.ok)
        self.assertIn("不认识的 action", bad.message)
        missing = self.call("todo_write", {"action": "complete"})
        self.assertFalse(missing.ok)
        self.assertIn("todo_id", missing.message)

    def test_schema_exposes_the_actions(self) -> None:
        schema = self.spec("todo_write").parameters_json_schema()
        self.assertEqual(
            set(schema["properties"]["action"]["enum"]),
            {"add", "update", "set_due", "set_status", "complete", "reopen", "delete"},
        )


# ==========================================================================
# 2. 记忆:改口与遗忘,都不删除
# ==========================================================================
class MemoryCorrectCase(Base):
    def note(self, content: str, fact_type: str = "preference") -> int:
        result = self.call("memory_note", {"content": content, "fact_type": fact_type})
        self.assertTrue(result.ok, result.message)
        return int(result.data["fact_id"])

    def test_correct_supersedes_without_deleting(self) -> None:
        old_id = self.note("用户喜欢喝美式")
        fixed = self.call("memory_correct", {"fact_id": old_id, "content": "用户喜欢喝拿铁"})
        self.assertTrue(fixed.ok, fixed.message)
        new_id = int(fixed.data["fact_id"])
        self.assertNotEqual(old_id, new_id)

        old_row = self.memory.get_fact(old_id)
        self.assertIsNotNone(old_row, "旧说法必须还在库里(不删除)")
        self.assertFalse(old_row.active, "旧说法要置为失效")
        self.assertTrue(self.memory.get_fact(new_id).active)

        active = {row.id for row in self.memory.active_facts()}
        self.assertIn(new_id, active)
        self.assertNotIn(old_id, active, "失效的旧说法不再进注入块")
        superseded = self.memory_conn.execute(
            "SELECT superseded_by FROM facts WHERE id = ?", (old_id,)
        ).fetchone()[0]
        self.assertEqual(int(superseded), new_id, "失效的那条要指回新那条")

    def test_forget_marks_inactive_and_keeps_the_row(self) -> None:
        old_id = self.note("用户不喜欢香菜")
        forgotten = self.call("memory_correct", {"fact_id": old_id})
        self.assertTrue(forgotten.ok, forgotten.message)
        self.assertEqual(forgotten.data["active"], False)
        self.assertFalse(self.memory.get_fact(old_id).active)
        self.assertEqual(
            self.memory_conn.execute("SELECT COUNT(*) FROM facts WHERE id = ?", (old_id,)).fetchone()[0],
            1, "遗忘不是删除",
        )

    def test_forgotten_fact_disappears_from_recall(self) -> None:
        kept = self.note("用户在做错题本")
        gone = self.note("用户养了一只仓鼠")
        self.call("memory_correct", {"fact_id": gone})
        recalled = {row["fact_id"] for row in self.call("memory_recall", {}).data["facts"]}
        self.assertIn(kept, recalled)
        self.assertNotIn(gone, recalled)

    def test_recall_reports_coverage(self) -> None:
        self.note("用户喜欢喝美式")
        payload = self.call("memory_recall", {}).data
        self.assertEqual(payload["coverage"]["active"], 1)
        self.assertEqual(payload["coverage"]["total"], 1)

    def test_undo_restores_the_old_wording(self) -> None:
        old_id = self.note("用户的生日是 3 月 2 日")
        fixed = self.call("memory_correct", {"fact_id": old_id, "content": "用户的生日是 3 月 5 日"})
        new_id = int(fixed.data["fact_id"])
        receipt_id = self.memory_receipts.recent(limit=1)[0].id
        note = self.memory_reverter.undo(receipt_id)["note"]
        self.assertIn("改回", note)
        self.assertTrue(self.memory.get_fact(old_id).active, "旧说法要恢复生效")
        self.assertFalse(self.memory.get_fact(new_id).active, "新写的那条要置回失效")

    def test_correcting_an_unknown_or_stale_fact_is_refused(self) -> None:
        missing = self.call("memory_correct", {"fact_id": 999, "content": "x"})
        self.assertFalse(missing.ok)
        self.assertIn("没有编号", missing.message)

        old_id = self.note("用户喜欢跑步")
        self.call("memory_correct", {"fact_id": old_id, "content": "用户喜欢游泳"})
        again = self.call("memory_correct", {"fact_id": old_id, "content": "用户喜欢骑行"})
        self.assertFalse(again.ok)
        self.assertIn("失效", again.message)


# ==========================================================================
# 3. 提醒:改期不换 id
# ==========================================================================
class ScheduleUpdateCase(Base):
    def test_update_moves_the_time_and_keeps_the_id(self) -> None:
        created = self.call("schedule", {
            "action": "create",
            "prompt": "提醒我看错题", "kind": "at", "at": "2026-10-12T19:00:00+08:00",
        })
        self.assertTrue(created.ok, created.message)
        schedule_id = created.data["id"]

        moved = self.call("schedule", {
            "action": "update", "id": schedule_id, "at": "2026-10-12T20:00:00+08:00",
        })
        self.assertTrue(moved.ok, moved.message)
        self.assertEqual(moved.data["id"], schedule_id, "id 必须不变")
        self.assertEqual(moved.data["at"], "2026-10-12T20:00:00+08:00")
        self.assertEqual(moved.data["before"]["at"], "2026-10-12T19:00:00+08:00")
        self.assertEqual(self.schedules.get(schedule_id).at, "2026-10-12T20:00:00+08:00")

    def test_reschedule_makes_a_fired_repeating_reminder_due_again(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        created = self.schedules.create(
            "每天提醒喝水", kind="every",
            at="2026-10-12T19:00:00+08:00", every_seconds=86400,
        )
        # ⚠️ tzinfo 必须给关键字:Python 3.14 的 datetime() 第 6 个位置参数是 fold,
        #    位置传时区会得到 "ZoneInfo object cannot be interpreted as an integer"。
        tz = ZoneInfo("Asia/Shanghai")
        self.assertTrue(
            self.schedules.claim(
                schedule_id=created.id,
                fired_at=datetime(2026, 10, 12, 19, 0, tzinfo=tz),
            )
        )
        self.assertFalse(
            self.schedules.get(created.id).is_due(
                datetime(2026, 10, 12, 19, 30, tzinfo=tz)
            ),
            "刚响过的重复提醒不该再响",
        )
        self.schedules.update(created.id, at="2026-10-12T20:00:00+08:00")
        self.assertTrue(
            self.schedules.get(created.id).is_due(
                datetime(2026, 10, 12, 20, 1, tzinfo=tz)
            ),
            "改到 20:00 之后就必须在 20:00 响 —— 否则用户看到的是「改了时间反而不响」",
        )

    def test_interval_floor_is_still_enforced(self) -> None:
        created = self.schedules.create("喝水", kind="every", every_seconds=600)
        with self.assertRaises(Exception):
            self.schedules.update(created.id, every_seconds=60)

    def test_undo_brings_the_old_time_back(self) -> None:
        created = self.schedules.create("开会", kind="at", at="2026-10-12T09:00:00+08:00")
        self.call("schedule", {"action": "update", "id": created.id, "at": "2026-10-12T10:00:00+08:00"})
        receipt_id = self.memory_receipts.recent(limit=1)[0].id
        self.memory_reverter.undo(receipt_id)
        self.assertEqual(self.schedules.get(created.id).at, "2026-10-12T09:00:00+08:00")

    def test_update_needs_at_least_one_field(self) -> None:
        created = self.schedules.create("买菜", kind="at", at="2026-10-12T09:00:00+08:00")
        result = self.call("schedule", {"action": "update", "id": created.id})
        self.assertFalse(result.ok)
        self.assertIn("至少要给一个", result.message)


# ==========================================================================
# 4. 文件:写工作区、覆盖、备份、撤销
# ==========================================================================
class FileWriteCase(Base):
    def test_writes_html_to_an_explicit_path(self) -> None:
        target = self.tmp / "templates" / "index.html"
        result = self.call("artifact_write", {
            "path": str(target), "name": "index.html", "content": "<h1>你好</h1>",
        })
        self.assertTrue(result.ok, result.message)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "<h1>你好</h1>")
        self.assertFalse(result.data["overwrote"])

    def test_overwrite_backs_up_and_undo_restores_previous_bytes(self) -> None:
        target = self.tmp / "templates" / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("<h1>旧版</h1>", encoding="utf-8")

        result = self.call("artifact_write", {
            "path": str(target), "name": "index.html", "content": "<h1>新版</h1>",
        })
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.data["overwrote"])
        self.assertTrue(result.data["backed_up"], "覆盖必须留一份旧内容")
        self.assertEqual(target.read_text(encoding="utf-8"), "<h1>新版</h1>")

        note = self.reverter.undo(result.data["receipt_id"])["note"]
        self.assertIn("上一版", note)
        self.assertEqual(target.read_text(encoding="utf-8"), "<h1>旧版</h1>",
                         "撤销覆盖 = 回到上一版,不是把文件删掉")

    def test_new_workspace_file_is_deleted_on_undo(self) -> None:
        target = self.tmp / "notes" / "草稿.md"
        result = self.call("artifact_write", {
            "path": str(target), "name": "草稿.md", "content": "临时",
        })
        self.assertTrue(result.ok, result.message)
        self.reverter.undo(result.data["receipt_id"])
        self.assertFalse(target.exists())

    def test_receipt_reports_the_workspace_domain(self) -> None:
        target = self.tmp / "a.css"
        result = self.call("artifact_write", {"path": str(target), "content": "body{}"})
        self.assertTrue(result.ok, result.message)
        receipt = self.receipts.get(result.data["receipt_id"])
        self.assertEqual(receipt.kind, "file.workspace")
        self.assertEqual(receipt.domain, "task")

    def test_office_renderers_cannot_be_written_to_a_path(self) -> None:
        result = self.call("artifact_write", {
            "path": str(self.tmp / "周报.docx"), "name": "周报.docx", "content": "# 标题",
        })
        self.assertFalse(result.ok)
        self.assertIn("产物区", result.message)

    def test_sensitive_files_are_never_overwritten(self) -> None:
        env = self.tmp / ".env"
        env.write_text("SECRET=1", encoding="utf-8")
        result = self.call("artifact_write", {"path": str(env), "content": "SECRET=2"})
        self.assertFalse(result.ok)
        self.assertEqual(env.read_text(encoding="utf-8"), "SECRET=1")

    def test_without_path_it_still_lands_in_the_artifact_store(self) -> None:
        result = self.call("artifact_write", {"name": "清单.md", "content": "- 牛奶"})
        self.assertTrue(result.ok, result.message)
        self.assertTrue(self.artifacts.exists(result.data["artifact_id"]))

    def test_html_is_now_an_allowed_artifact_extension(self) -> None:
        result = self.call("artifact_write", {"name": "页面.html", "content": "<p>x</p>"})
        self.assertTrue(result.ok, result.message)
        self.assertEqual(
            self.artifacts.get(result.data["artifact_id"]).media_type,
            "text/html; charset=utf-8",
        )


# ==========================================================================
# 5. 覆盖域:只读查询顺带报"一共多少"
# ==========================================================================
class CoverageCase(Base):
    def test_expense_query_can_report_the_whole_ledger(self) -> None:
        self.ledger.record_expense(
            TxnCreate(amount_cents=3000, payee="午饭", category="餐饮",
                      ts="2026-10-08T12:00:00+08:00"),
            status="confirmed",
        )
        registry = ToolRegistry()
        registry.register_all(build_ledger_tools(self.ledger, session_id="t"))

        plain = asyncio.run(registry.execute(
            "expense_query", {"period": "2026-10"},
            breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))
        self.assertNotIn("coverage", plain.data, "没要就不给 —— 别白吃输出预算")

        asked = asyncio.run(registry.execute(
            "expense_query", {"period": "2026-10", "with_coverage": True},
            breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))
        self.assertTrue(asked.ok, asked.message)
        coverage = asked.data["coverage"]
        self.assertEqual(coverage["rows"], 1)
        self.assertEqual(coverage["earliest"], "2026-10-08T12:00:00+08:00")
        self.assertEqual(coverage["breakdown"]["expense_legs"], 1)

    def test_expense_query_can_list_every_transaction(self) -> None:
        """⭐ 用户 2026-10-16 的原话:"智能体无法读到每天的每一笔帐"。

        以前只有**按天/按分类的合计** —— 「10-12 花了 76 元、4 笔」,
        同一天那 4 笔各是什么、各多少,模型手里**根本没有**。
        现在 detail=true 给出逐笔明细,而且**明细逐笔相加必须等于聚合合计**
        (两个口径不一致比没有明细更糟)。
        """
        self.ledger.record_expense(
            TxnCreate(amount_cents=2000, narration="中饭", category="餐饮",
                      ts="2026-10-12T11:48:59+08:00"), status="confirmed")
        self.ledger.record_expense(
            TxnCreate(amount_cents=3500, narration="晚饭", category="餐饮",
                      ts="2026-10-12T18:00:00+08:00"), status="confirmed")
        self.ledger.record_expense(
            TxnCreate(amount_cents=1500, narration="中饭", category="餐饮",
                      ts="2026-10-15T12:00:00+08:00"), status="confirmed")
        registry = ToolRegistry()
        registry.register_all(build_ledger_tools(self.ledger, session_id="t"))

        plain = asyncio.run(registry.execute(
            "expense_query", {"period": "2026-10-12..2026-10-18", "dimension": "day"},
            breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))
        self.assertEqual(plain.data["txns"], [], "没要明细就不给 —— 别白吃输出预算")
        self.assertEqual(plain.data["txn_total"], 0)

        got = asyncio.run(registry.execute(
            "expense_query", {"period": "2026-10-12..2026-10-18", "dimension": "day",
                              "detail": True},
            breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))
        self.assertTrue(got.ok, got.message)
        rows = got.data["txns"]
        self.assertEqual(len(rows), 3, "每一天的每一笔都要在")
        self.assertEqual([row["day"] for row in rows],
                         ["2026-10-15", "2026-10-12", "2026-10-12"], "按时刻倒序")
        first = rows[-1]
        self.assertEqual(first["amount_cents"], 2000)
        self.assertEqual(first["narration"], "中饭")
        self.assertEqual(first["category"], "餐饮")
        self.assertEqual(first["account"], "支付宝")
        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(first["direction"], "expense")
        self.assertNotEqual(first["id"], 0)
        self.assertEqual(sum(row["amount_cents"] for row in rows), got.data["total_cents"],
                         "明细逐笔相加必须等于聚合合计(两个口径不能打架)")
        self.assertEqual(got.data["txn_total"], 3)
        self.assertIn("全在 txns 里", got.data["detail_note"])

    def test_expense_detail_direction_filters_out_income(self) -> None:
        """明细也要认 direction —— 「只花了多少」里不该混进收入那几笔。"""
        self.ledger.record_expense(
            TxnCreate(amount_cents=2000, narration="中饭", category="餐饮",
                      ts="2026-10-12T12:00:00+08:00"), status="confirmed")
        self.ledger.record_expense(
            TxnCreate(direction="income", amount_cents=50000, payee="摆摊",
                      category="工资", ts="2026-10-12T20:00:00+08:00"), status="confirmed")
        registry = ToolRegistry()
        registry.register_all(build_ledger_tools(self.ledger, session_id="t"))

        spent = asyncio.run(registry.execute(
            "expense_query", {"period": "2026-10-12..2026-10-18", "detail": True},
            breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))
        self.assertEqual([row["direction"] for row in spent.data["txns"]], ["expense"])
        self.assertEqual(spent.data["total_cents"], 2000)

        both = asyncio.run(registry.execute(
            "expense_query", {"period": "2026-10-12..2026-10-18", "direction": "all",
                              "detail": True},
            breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))
        self.assertEqual(sorted(row["direction"] for row in both.data["txns"]),
                         ["expense", "income"])
        self.assertEqual(both.data["income_cents"], 50000)
        self.assertEqual(both.data["expense_cents"], 2000)

    def test_expense_detail_says_out_loud_when_it_is_truncated(self) -> None:
        """⚠️ 截断了必须写清楚漏了多少 —— 不许让模型拿"看到的这些"当成全部。"""
        for index in range(4):
            self.ledger.record_expense(
                TxnCreate(amount_cents=100 * (index + 1), narration="第 " + str(index + 1) + " 笔",
                          category="餐饮", ts="2026-10-12T1" + str(index) + ":00:00+08:00"),
                status="confirmed",
            )
        registry = ToolRegistry()
        registry.register_all(build_ledger_tools(self.ledger, session_id="t"))
        got = asyncio.run(registry.execute(
            "expense_query", {"period": "2026-10-12..2026-10-18", "detail": True,
                              "detail_limit": 2},
            breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))
        self.assertEqual(len(got.data["txns"]), 2)
        self.assertEqual(got.data["txn_total"], 4, "总笔数只认聚合那条路,不被截断影响")
        self.assertIn("只带了最近 2 笔", got.data["detail_note"])
        self.assertIn("4", got.data["detail_note"])

    def test_todo_list_always_reports_distribution(self) -> None:
        self.call("todo_write", {"content": "一件事"})
        self.call("todo_write", {"content": "两件事"})
        payload = self.call("todo_list", {"filter": "pending"}).data
        self.assertEqual(payload["coverage"]["rows"], 2)
        self.assertEqual(payload["coverage"]["open"], 2)
        self.assertEqual(payload["coverage"]["done"], 0)

    def test_fallback_reverter_undoes_receipts_from_the_other_database(self) -> None:
        """⛔ 只认一本回执账的撤销按钮是**假的**。

        领域写入的回执在 assistant.db,事项层/记忆的在 memory.db。
        用户只会看到一个「撤销」按钮 —— 所以两本账必须拼成一条路。
        """
        from la.domain.receipts import ReceiptError, build_fallback_reverter

        combined = build_fallback_reverter(self.reverter, self.memory_reverter)
        created = self.call("todo_write", {"content": "把两本回执账拼起来"})
        receipt = self.memory_receipts.recent(limit=1)[0]
        # 领域库那本撤销器**不认识**这条:它连 id 都查不到
        with self.assertRaises(ReceiptError):
            self.reverter.undo(receipt.id)
        note = combined.undo(receipt.id)["note"]
        self.assertIn("删掉", note)  # 撤销"新建待办"= 删掉它
        self.assertIsNone(self.todos.get(created.data["id"]))
        with self.assertRaises(ReceiptError):
            combined.undo(999999)

    def test_fallback_reverter_still_refuses_unknown_kinds(self) -> None:
        from la.domain.receipts import ReceiptError, build_fallback_reverter

        combined = build_fallback_reverter(
            build_reverter(self.receipts), build_reverter(self.memory_receipts)
        )
        with self.assertRaises(ReceiptError):
            combined.undo(1)

    def test_schema_advertises_the_flag(self) -> None:
        self.assertIn("with_coverage", ExpenseQuery.model_json_schema()["properties"])


if __name__ == "__main__":
    unittest.main()
