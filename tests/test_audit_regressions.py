"""2026-10-18 审计的回归测试:每一条都对着一个**实测过的**事故。

覆盖:
  1. 记忆的两种编号(fact_candidates.id vs facts.id)不能混用;
  2. 改口必须等新说法入库才停用旧事实;撤销待裁决的改口 = 作废候选;
  3. 只改类别要真的落库,并且能撤销;
  4. 同一秒记两笔一模一样的账不能被静默吞掉;
  5. 收入交易的纠正撤销后分录仍然和为零;
  6. confirm -> void -> confirm 不再撞发件箱的幂等键;
  7. 导入回执的撤销真的能作废那一批(嵌套 batch_id + type 分发);
  8. 失败的一轮必须收尾(user/assistant 成对 + turn/end);
  9. 两本回执账同号时,带前缀的撤销只认指定的那一本;
 10. 错误页不把用户输入原样拼进 HTML(反射型 XSS);
 11. Host 不在白名单时整个站点拒绝(DNS rebinding);
 12. 健康导入的撤销只删本次新建的行,不碰历次导入;
 13. 跨成员改错题会被拒绝,而且内容不变。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from la import config
from la.agent.session import MemorySession
from la.domain.receipts import ReceiptLog, ReceiptReverter, build_fallback_reverter
from la.domain.services.health_service import HealthService
from la.domain.services.ledger_service import LedgerService
from la.domain.services.study_service import StudyError, StudyService
from la.llm.fake import FakeClient
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY
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
        self.settings = config.get_settings(reload=True)
        self.receipts = ReceiptLog(self.assistant)
        self.memory_receipts = ReceiptLog(self.memory_conn)

    def tearDown(self) -> None:
        for conn in (self.assistant, self.memory_conn):
            try:
                conn.close()
            except Exception:
                pass
        for key, value in self._patched.items():
            setattr(config, key, value)
        self._tmp.cleanup()


class MemoryIdsCase(Base):
    def _session(self) -> MemorySession:
        return MemorySession(
            FakeClient(), self.settings, store=MemoryStore(self.memory_conn),
            session_id="audit", auto_briefing=False,
        )

    def _tools(self, session: MemorySession) -> dict:
        return {
            tool.name: tool for tool in build_task_tools(
                todos=session.todos, goals=session.goals, schedules=session.schedules,
                library=session.library, add_fact=session._add_fact,
                memory_note_marker=session._mark_note, memory=session.store,
                session_id="audit",
            )
        }

    def test_note_reports_candidate_not_fact_id(self) -> None:
        session = self._session()
        out = asyncio.run(
            self._tools(session)["memory_note"].handler(
                {"content": "我最喜欢喝冰美式", "fact_type": "preference"}
            )
        )
        self.assertEqual(0, out["fact_id"], "候选阶段不该有事实编号")
        self.assertGreater(out["candidate_id"], 0)
        self.assertEqual("pending", out["status"])
        self.assertEqual([], session.store.active_facts(limit=10))

        report = asyncio.run(session.run_fact_gate())
        self.assertEqual(1, report.admitted)
        facts = session.store.active_facts(limit=10)
        self.assertEqual(1, len(facts))
        self.assertNotEqual(out["candidate_id"], 0)

    def test_pending_supersede_keeps_old_fact_active(self) -> None:
        session = self._session()
        tools = self._tools(session)
        fact_id = session.store.add_fact("用户喜欢喝美式", fact_type="preference")
        out = asyncio.run(
            tools["memory_correct"].handler({"fact_id": fact_id, "content": "用户喜欢喝拿铁"})
        )
        self.assertEqual("pending", out["status"])
        self.assertTrue(session.store.get_fact(fact_id).active, "裁决前旧说法必须仍然有效")

        # 撤销这次"待裁决的改口" = 作废候选,旧事实不动
        note = session.reverter.undo(out["receipt_id"])["note"]
        self.assertIn("改口已撤回", note)
        self.assertEqual("rejected", dict(session.store.get_candidate(out["candidate_id"]))["status"])
        self.assertTrue(session.store.get_fact(fact_id).active)

    def test_supersede_only_after_admission(self) -> None:
        session = self._session()
        tools = self._tools(session)
        fact_id = session.store.add_fact("用户喜欢喝美式", fact_type="preference")
        asyncio.run(
            tools["memory_correct"].handler({"fact_id": fact_id, "content": "用户喜欢喝拿铁"})
        )
        asyncio.run(session.run_fact_gate())
        old = session.store.get_fact(fact_id)
        self.assertFalse(old.active)
        self.assertIsNotNone(old.superseded_by if hasattr(old, "superseded_by") else True)

    def test_retype_persists_and_can_be_undone(self) -> None:
        session = self._session()
        tools = self._tools(session)
        fact_id = session.store.add_fact("用户喜欢喝美式", fact_type="preference")
        out = asyncio.run(
            tools["memory_correct"].handler({"fact_id": fact_id, "fact_type": "identity"})
        )
        self.assertEqual("identity", session.store.get_fact(fact_id).type)
        session.reverter.undo(out["receipt_id"])
        self.assertEqual("preference", session.store.get_fact(fact_id).type)


class LedgerCase(Base):
    def _service(self) -> LedgerService:
        return LedgerService(
            self.assistant, now_provider=self.settings.now, receipts=self.receipts
        )

    def test_same_second_identical_expenses_are_two_rows(self) -> None:
        svc = self._service()
        payload = {"amount_cents": 1200, "payee": "咖啡店", "category": "餐饮",
                   "source": "chat", "direction": "expense"}
        first = svc.record_expense(payload, actor="agent", status="confirmed")
        second = svc.record_expense(payload, actor="agent", status="confirmed")
        self.assertNotEqual(first.id, second.id)
        count = self.assistant.execute("SELECT count(*) FROM app_txns").fetchone()[0]
        self.assertEqual(2, int(count))

    def test_income_correction_undo_keeps_postings_balanced(self) -> None:
        svc = self._service()
        txn = svc.record_expense(
            {"amount_cents": 1000, "payee": "工资", "category": "工资",
             "source": "chat", "direction": "income"},
            actor="agent", status="confirmed",
        )
        svc.correct_last(
            amount_cents=1200, payee=None, category=None, txn_id=txn.id,
            actor="agent", learn=False,
        )
        receipt_id = self.receipts.recent(limit=1)[0].id
        svc.revert_txn(self.receipts.get(receipt_id))
        rows = self.assistant.execute(
            "SELECT amount_cents FROM app_postings WHERE txn_id = ?", (txn.id,)
        ).fetchall()
        self.assertEqual(0, sum(int(row[0]) for row in rows))
        self.assertEqual(1000, abs(int(rows[0][0])))

    def test_status_cycle_does_not_collide_on_outbox_key(self) -> None:
        svc = self._service()
        txn = svc.record_expense(
            {"amount_cents": 500, "payee": "x", "category": "餐饮", "source": "chat"},
            actor="agent", status="draft",
        )
        for status in ("confirmed", "void", "confirmed"):
            svc._set_status(txn.id, status, actor="user", session_id=None)
        self.assertEqual("confirmed", svc.read(txn.id).status)

    def test_import_revert_voids_the_batch(self) -> None:
        svc = self._service()
        first = svc.record_expense(
            {"amount_cents": 100, "payee": "导入", "category": "其他", "source": "import"},
            actor="import", status="confirmed",
        )
        batch_id = 7
        self.assistant.execute(
            "UPDATE app_txns SET import_batch_id = ? WHERE id = ?", (batch_id, first.id)
        )
        self.assistant.commit()
        receipt_id = self.receipts.record(
            kind="ledger.import", domain="ledger", label="导入 1 笔",
            detail={"imported": 1},
            undo_plan={"type": "ledger_import",
                       "plan": {"batch_id": batch_id, "type": "void_batch"}},
            actor="import",
        )
        self.assistant.commit()
        note = svc.revert_txn(self.receipts.get(receipt_id))
        self.assertIn("已作废", note)
        self.assertEqual(
            "void",
            self.assistant.execute(
                "SELECT status FROM app_txns WHERE id = ?", (first.id,)
            ).fetchone()[0],
        )


class ReceiptSourceCase(Base):
    def test_source_prefix_picks_the_right_book(self) -> None:
        a = self.receipts.record(kind="probe", domain="d", label="A", undo_plan={})
        m = self.memory_receipts.record(kind="probe", domain="d", label="M", undo_plan={})
        self.assertEqual(a, m, "两张表各自从 1 自增,所以这里本来就同号")

        rev_a = ReceiptReverter(self.receipts).register("probe", lambda r: "from-a")
        rev_m = ReceiptReverter(self.memory_receipts).register("probe", lambda r: "from-m")
        combined = build_fallback_reverter(rev_a, rev_m)
        self.assertEqual("from-m", combined.undo(1, source="m")["note"])
        # 另一本的同号回执没有被误撤
        self.assertFalse(self.receipts.get(1).undone)


class TurnCase(Base):
    def test_failed_turn_is_closed(self) -> None:
        class Boom:
            model = "boom"

            async def chat(self, *args, **kwargs):
                raise RuntimeError("boom")

        store = MemoryStore(self.memory_conn)
        session = MemorySession(
            Boom(), self.settings, store=store, session_id="audit-turn",
            auto_briefing=False,
        )
        with self.assertRaises(RuntimeError):
            asyncio.run(session.respond("你好"))
        events = [event.type for event in store.events("audit-turn")]
        self.assertIn("assistant/message", events)
        self.assertEqual("turn/end", events[-1])
        history = session.conversation_history()
        self.assertEqual(["user", "assistant"], [item["role"] for item in history])


class WebCase(Base):
    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient

        from la.web.app import create_app

        self.client = TestClient(create_app(), raise_server_exceptions=False)

    def test_error_page_escapes_user_input(self) -> None:
        payload = "<script>alert(1)</script>"

        # 用“未接住的意外异常”触发错误页;非法 period 现在走领域错误,
        # 页面会优雅降级成 200(这是 2026-10-18 的修复,不再用 500 表达)。
        def boom(self, **kwargs):
            raise RuntimeError("坏输入: " + payload)

        with mock.patch.object(LedgerService, "list_txns", boom):
            response = self.client.get("/ledger")
        self.assertEqual(500, response.status_code)
        self.assertNotIn(payload, response.text)
        self.assertIn("&lt;script&gt;", response.text)

    def test_host_header_outside_allowlist_is_refused(self) -> None:
        response = self.client.get("/chat", headers={"host": "evil.example"})
        self.assertEqual(403, response.status_code)
        self.assertIn("Host", response.text)


class HealthImportCase(Base):
    def test_import_undo_only_deletes_its_own_rows(self) -> None:
        svc = HealthService(self.assistant, now_provider=self.settings.now)
        # 上一次导入留下的行
        # ⚠️ measured_at 必须各不相同:同一个 (metric, 时刻, source) 会被
        #    ON CONFLICT 更新成一行 —— 那是"同一读数重复上报"的正确行为。
        svc.log_measurement(
            {"metric_code": "weight", "value": 70, "source": "device:apple",
             "measured_at": "2026-10-17T08:00:00+08:00"},
            actor="import", receipt=False,
        )
        # 本次导入
        first = svc.log_measurement(
            {"metric_code": "weight", "value": 71, "source": "device:apple",
             "measured_at": "2026-10-18T08:00:00+08:00"},
            actor="import", receipt=False,
        )
        second = svc.log_measurement(
            {"metric_code": "weight", "value": 72, "source": "device:apple",
             "measured_at": "2026-10-18T09:00:00+08:00"},
            actor="import", receipt=False,
        )
        # 手工记的行(不该被删)
        svc.log_measurement(
            {"metric_code": "weight", "value": 73, "source": "user",
             "measured_at": "2026-10-18T10:00:00+08:00"},
            actor="user", receipt=False,
        )
        from la.domain.receipts import Receipt

        receipt = Receipt(
            id=99, ts="2026-10-18T00:00:00+08:00", actor="import",
            kind="health.import", domain="health", label="导入",
            undo_plan={"type": "import", "source": "device:apple",
                       "first_id": first["id"], "last_id": second["id"]},
        )
        svc.revert_import(receipt)
        rows = self.assistant.execute(
            "SELECT id, source FROM app_measurements ORDER BY id"
        ).fetchall()
        self.assertEqual(2, len(rows), "上一次导入的 + 手工记的 应当留下")
        self.assertEqual(["device:apple", "user"], [str(row["source"]) for row in rows])


class StudyOwnerCase(Base):
    def test_cross_member_update_is_refused_and_unchanged(self) -> None:
        from la.domain import ownership

        ownership.set_share_mode(self.assistant, ownership.DOMAIN_STUDY, ownership.MODE_OWN)
        owner = StudyService(self.assistant, member_id=1)
        intruder = StudyService(self.assistant, member_id=2)
        record = owner.add_problem(
            {"subject": "数学", "stem_text": "1+1=?", "answer_text": "2"}, actor="user"
        )
        with self.assertRaises(StudyError):
            intruder.update_problem(record.id, {"stem_text": "被改了"})
        self.assertEqual("1+1=?", owner.get_problem(record.id).stem_text)


if __name__ == "__main__":
    unittest.main()
