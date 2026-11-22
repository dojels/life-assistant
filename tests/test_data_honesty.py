"""2026-10-17 审计后的第二道闸:让"用户看到的状态"等于"库里的状态"。

四条不变量,每条都对着一次可复现的坏状态:
  1. 审计写失败不许把"已写成功"变成一次异常(否则模型说"没记上",而钱已经记了);
  2. 回执表不可用只许**降级并重探**,不许一次失败就永久失能;
  3. 撤销必须**先认领、再动数据**(并发点两次不许撤两次);失败要把回执还回去;
  4. 录一道错题是一个事务 —— 中途失败不许留下"题在、回执没了"的半截状态。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from la import config
from la.domain import audit as audit_mod
from la.domain.receipts import CLAIM_NOTE, ReceiptError, ReceiptLog, ReceiptReverter
from la.domain.services.ledger_service import LedgerService
from la.domain.services.study_service import StudyService
from la.domain.schemas.study import ProblemCreate
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATION_17_ASSISTANT_UNDO_CLAIM


class _BrokenConn:
    """一执行就报错的连接:模拟"审计表被锁/不存在"这类瞬时故障。"""

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")


class AuditDegradesTestCase(unittest.TestCase):
    def test_audit_failure_does_not_raise(self) -> None:
        sink = audit_mod.AuditSink(_BrokenConn())
        sink(actor="user", action="test.write")     # 不许抛
        self.assertEqual(1, sink.failures)

    def test_a_committed_write_survives_a_broken_audit(self) -> None:
        """这条是 2026-10-17 审计实测的那个坑:异常让模型以为"没记上"。"""
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "assistant.db")
            try:
                db.migrate(conn, MIGRATIONS_ASSISTANT)
                ledger = LedgerService(conn, audit=audit_mod.AuditSink(_BrokenConn()))
                record = ledger.record_expense(
                    {"amount_cents": 3800, "payee": "老马牛肉面", "category": "餐饮",
                     "ts": "2026-10-17T12:30:00+08:00"}, actor="agent")
                self.assertIsNotNone(record)
                count = conn.execute("SELECT count(*) FROM app_txns_live").fetchone()[0]
                self.assertEqual(1, count, "写成功就是成功,审计写不进去不该翻案")
            finally:
                conn.close()


class ReceiptServiceabilityTestCase(unittest.TestCase):
    def test_a_failed_probe_is_retried(self) -> None:
        """回执表"探一次就永久不可用"会让整个渠道失去撤销按钮,且完全无声。"""
        log = ReceiptLog(_BrokenConn())
        self.assertFalse(log._table_ready())
        self.assertFalse(log._table_ready(), "退避期内不重复探")
        log._next_probe = 0.0            # 假装 5 秒过去了
        self.assertFalse(log._table_ready())

        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "assistant.db")
            try:
                db.migrate(conn, MIGRATIONS_ASSISTANT)
                fresh = ReceiptLog(conn)
                self.assertTrue(fresh._table_ready(), "表在的时候必须能探到")
            finally:
                conn.close()

    def test_the_claim_marker_matches_the_trigger(self) -> None:
        """认领标记是 SQL 里的字面量 —— 两边必须一致,否则收回认领会被触发器拒绝。"""
        self.assertIn("'撤销中'", "\n".join(MIGRATION_17_ASSISTANT_UNDO_CLAIM))
        self.assertEqual("撤销中", CLAIM_NOTE)


class UndoClaimTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.log = ReceiptLog(self.conn)
        self.calls: list[int] = []

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _reverter(self, *, boom: bool = False) -> ReceiptReverter:
        def handler(receipt):
            self.calls.append(receipt.id)
            if boom:
                raise RuntimeError("撤到一半炸了")
            return "已退回"

        return ReceiptReverter(self.log).register("test.kind", handler)

    def _record(self) -> int:
        rid = self.log.record(kind="test.kind", domain="system", label="测试写入",
                              undo_plan={"type": "noop"})
        self.conn.commit()
        return rid

    def test_undo_twice_runs_the_handler_once(self) -> None:
        rid = self._record()
        reverter = self._reverter()
        self.assertEqual(1, len([reverter.undo(rid)["note"]]))
        with self.assertRaises(ReceiptError):
            reverter.undo(rid)
        self.assertEqual(1, len(self.calls), "第二次不许再动一次数据")

    def test_failure_gives_the_receipt_back(self) -> None:
        rid = self._record()
        reverter = self._reverter(boom=True)
        with self.assertRaises(RuntimeError):
            reverter.undo(rid)
        self.assertFalse(self.log.get(rid).undone, "撤到一半失败:回执要还回去")
        retry = self._reverter()
        self.assertEqual("已退回", retry.undo(rid)["note"], "还回去之后要能重试")

    def test_the_note_lands_after_the_claim(self) -> None:
        rid = self._record()
        self._reverter().undo(rid)
        receipt = self.log.get(rid)
        self.assertTrue(receipt.undone)
        self.assertEqual("已退回", receipt.undone_note)


class StudyAtomicityTestCase(unittest.TestCase):
    def test_a_failed_problem_leaves_nothing_behind(self) -> None:
        """旧写法分 5 次提交:第 4 步失败 → 题在库里、没回执、没 outbox。"""
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "assistant.db")
            try:
                db.migrate(conn, MIGRATIONS_ASSISTANT)
                service = StudyService(conn)

                def boom(problem_id: int):
                    raise RuntimeError("建复习单元失败")

                service.ensure_review_units = boom  # type: ignore[assignment]
                with self.assertRaises(RuntimeError):
                    service.add_problem(ProblemCreate(subject="数学", stem_text="1+1=?"))
                # 用**另一个连接**看:本连接看不到未提交的行,只有真提交了才会看见。
                other = db.connect(Path(tmp) / "assistant.db")
                try:
                    problems = other.execute("SELECT count(*) FROM app_problems").fetchone()[0]
                    receipts = other.execute("SELECT count(*) FROM ops_receipts").fetchone()[0]
                    outbox = other.execute("SELECT count(*) FROM ops_outbox").fetchone()[0]
                finally:
                    other.close()
                self.assertEqual(0, problems, "题不该留在库里")
                self.assertEqual(0, receipts, "不该留下没有对应数据的回执")
                self.assertEqual(0, outbox, "记忆不该收到一条不存在的事件")
            finally:
                conn.close()


class OneClockTestCase(unittest.TestCase):
    def test_services_share_the_single_clock(self) -> None:
        """三个"现在"曾经各自为政:ledger 用 OS 时区、其余写死上海、settings 用 LA_TIMEZONE。"""
        import os

        previous = os.environ.get("LA_TIMEZONE")
        os.environ["LA_TIMEZONE"] = "Europe/London"
        # ⚠️ 必须把三个路径常量都指到临时目录(2026-10-17 的教训):
        #    只改 DATA_DIR 的话,ASSISTANT_DB 仍指真实库 —— 测试就会往用户的数据里写。
        saved = (config.DATA_DIR, config.ARCHIVE_DIR, config.ARTIFACTS_DIR,
                 config.ASSISTANT_DB, config.MEMORY_DB)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config.DATA_DIR = root
            config.ARCHIVE_DIR = root / "archive"
            config.ARTIFACTS_DIR = root / "artifacts"
            config.ASSISTANT_DB = root / "assistant.db"
            config.MEMORY_DB = root / "memory.db"
            try:
                from la.web.deps import build_services

                config.get_settings(reload=True)
                services = build_services()
                try:
                    expected = services.settings.now().utcoffset()
                    for name, service in (("calendar", services.calendar),
                                          ("ledger", services.ledger),
                                          ("health", services.health),
                                          ("study", services.study)):
                        self.assertEqual(expected, service._current_time().utcoffset(),
                                         name + " 的时钟没有跟着 settings 走")
                finally:
                    services.close()
            finally:
                (config.DATA_DIR, config.ARCHIVE_DIR, config.ARTIFACTS_DIR,
                 config.ASSISTANT_DB, config.MEMORY_DB) = saved
                if previous is None:
                    os.environ.pop("LA_TIMEZONE", None)
                else:
                    os.environ["LA_TIMEZONE"] = previous
                config.get_settings(reload=True)


if __name__ == "__main__":
    unittest.main()
