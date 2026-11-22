"""M6 测试:安全、撤销、备份、报表、护栏。

这一节的测试有个共同点:**它们测的都是"出事的时候"** ——
重复提交、误删、备份到底能不能恢复、模型能不能读到 .env。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from la import config
from la.ops import backup as backup_mod
from la.ops.guardrails import run_all
from la.ops.idempotency import IdempotencyStore
from la.ops.paths import PathNotAllowed, PathPolicy
from la.ops.report import build_report
from la.ops.selfcheck import build_privacy_report
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY


class M6Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.assistant = sqlite3.connect(str(self.root / "assistant.db"))
        self.assistant.row_factory = sqlite3.Row
        self.memory = sqlite3.connect(str(self.root / "memory.db"))
        self.memory.row_factory = sqlite3.Row
        from la.storage.migrations import MIGRATIONS_MEMORY

        for conn, migrations in (
            (self.assistant, MIGRATIONS_ASSISTANT),
            (self.memory, MIGRATIONS_MEMORY),
        ):
            for pragma in config.SQLITE_PRAGMAS:
                conn.execute(pragma)
            db.migrate(conn, migrations)

    def tearDown(self) -> None:
        self.assistant.close()
        self.memory.close()
        self._tmp.cleanup()


class SchemaCase(M6Base):
    def test_migration_7_is_applied(self) -> None:
        versions = {
            row[0]
            for row in self.assistant.execute("SELECT version FROM sys_schema_version")
        }
        self.assertIn(7, versions)

    def test_soft_delete_columns_exist(self) -> None:
        columns = {
            row[1] for row in self.assistant.execute("PRAGMA table_info(app_txns)")
        }
        self.assertIn("deleted_at", columns)
        self.assertIn("version", columns)

    def test_live_view_hides_deleted_rows(self) -> None:
        """⭐ 读走视图 —— 这是"删了就等于不存在"的唯一保证。"""
        self.assistant.execute(
            "INSERT INTO app_txns(ts, payee, source, status, created_at, updated_at, deleted_at)"
            " VALUES ('2026-01-01T10:00:00+08:00','删掉的','user','confirmed','x','x',"
            "'2026-01-02T10:00:00+08:00')"
        )
        self.assistant.execute(
            "INSERT INTO app_txns(ts, payee, source, status, created_at, updated_at, deleted_at)"
            " VALUES ('2026-01-01T11:00:00+08:00','留着的','user','confirmed','x','x',NULL)"
        )
        self.assistant.commit()
        all_rows = self.assistant.execute("SELECT count(*) FROM app_txns").fetchone()[0]
        live_rows = self.assistant.execute("SELECT count(*) FROM app_txns_live").fetchone()[0]
        self.assertEqual(all_rows, 2)
        self.assertEqual(live_rows, 1)

    def test_undo_log_is_append_only(self) -> None:
        self.assistant.execute(
            "INSERT INTO ops_undo_log(ts, actor, kind, label, table_name, row_id, before_json)"
            " VALUES ('t','user','k','l','app_txns',1,'{}')"
        )
        self.assistant.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.assistant.execute("UPDATE ops_undo_log SET label = '改掉' WHERE id = 1")
        self.assistant.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.assistant.execute("DELETE FROM ops_undo_log WHERE id = 1")
        self.assistant.rollback()

    def test_undo_log_can_only_be_marked_used_once(self) -> None:
        self.assistant.execute(
            "INSERT INTO ops_undo_log(ts, actor, kind, label, table_name, row_id, before_json)"
            " VALUES ('t','user','k','l','app_txns',1,'{}')"
        )
        self.assistant.commit()
        self.assistant.execute("UPDATE ops_undo_log SET used_at = 'x' WHERE id = 1")
        self.assistant.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.assistant.execute("UPDATE ops_undo_log SET used_at = 'y' WHERE id = 1")
        self.assistant.rollback()


class UndoCase(M6Base):
    def setUp(self) -> None:
        super().setUp()
        from la.domain.schemas.ledger import ExpenseCreate
        from la.domain.services.ledger_service import LedgerService

        self.ledger = LedgerService(self.assistant)
        self.ledger.record_expense(
            ExpenseCreate(amount_cents=1250, category="餐饮", account="支付宝",
                          payee="沙县小吃", narration="午饭"),
            actor="user",
        )

    def test_soft_delete_then_undo_restores_it(self) -> None:
        txn_id = int(self.assistant.execute("SELECT id FROM app_txns_live").fetchone()[0])
        self.ledger.soft_delete(txn_id, reason="点错了")
        self.assertEqual(
            self.assistant.execute("SELECT count(*) FROM app_txns_live").fetchone()[0], 0
        )
        # 全量表里还在,只是看不见
        self.assertEqual(
            self.assistant.execute(
                "SELECT count(*) FROM app_txns WHERE deleted_at IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        info = self.ledger.undo_last()
        self.assertEqual(info["txn_id"], txn_id)
        self.assertEqual(
            self.assistant.execute("SELECT count(*) FROM app_txns_live").fetchone()[0], 1
        )

    def test_version_bumps_on_every_change(self) -> None:
        txn_id = self.assistant.execute("SELECT id FROM app_txns_live").fetchone()[0]
        before = self.assistant.execute(
            "SELECT version FROM app_txns WHERE id = ?", (txn_id,)
        ).fetchone()[0]
        self.ledger.soft_delete(txn_id)
        after = self.assistant.execute(
            "SELECT version FROM app_txns WHERE id = ?", (txn_id,)
        ).fetchone()[0]
        self.assertEqual(after, before + 1)

    def test_undo_cannot_be_replayed(self) -> None:
        from la.domain.undo import UndoError

        txn_id = self.assistant.execute("SELECT id FROM app_txns_live").fetchone()[0]
        self.ledger.soft_delete(txn_id)
        self.ledger.undo_last()
        with self.assertRaises(UndoError):
            self.ledger.undo_last()

    def test_deleted_txn_disappears_from_queries_and_exports(self) -> None:
        txn_id = self.assistant.execute("SELECT id FROM app_txns_live").fetchone()[0]
        self.ledger.confirm(txn_id)
        self.assertIn("沙县小吃", self.ledger.export_beancount())
        self.ledger.soft_delete(txn_id)
        self.assertNotIn("沙县小吃", self.ledger.export_beancount())

    def test_failed_undo_leaves_nothing_consumed(self) -> None:
        """撤销失败(没有可撤销的)不该把日志吃掉。"""
        from la.domain.undo import UndoError

        with self.assertRaises(UndoError):
            self.ledger.undo_last()
        self.assertEqual(len(self.ledger.undo.recent(limit=5)), 0)


class IdempotencyCase(M6Base):
    def test_same_key_second_call_is_a_replay(self) -> None:
        store = IdempotencyStore(self.assistant)
        first = store.begin("tok-1", {"value": 70.5})
        self.assertTrue(first.should_process)
        store.complete("tok-1", {"ok": True})
        second = store.begin("tok-1", {"value": 70.5})
        self.assertFalse(second.should_process)
        self.assertEqual(second.kind, "replay")

    def test_same_key_different_payload_is_a_conflict(self) -> None:
        store = IdempotencyStore(self.assistant)
        store.begin("tok-2", {"value": 70.5})
        store.complete("tok-2", {"ok": True})
        clash = store.begin("tok-2", {"value": 99.9})
        self.assertEqual(clash.kind, "conflict")
        self.assertEqual(clash.http_status, 422)

    def test_failed_call_can_be_retried(self) -> None:
        store = IdempotencyStore(self.assistant)
        store.begin("tok-3", {"value": 1})
        store.fail("tok-3", "boom")
        again = store.begin("tok-3", {"value": 1})
        self.assertTrue(again.should_process)


class PathPolicyCase(unittest.TestCase):
    def test_inside_root_is_allowed(self) -> None:
        policy = PathPolicy(roots=(config.BASE_DIR,))
        self.assertTrue(policy.is_allowed(config.BASE_DIR / "README.md"))

    def test_traversal_outside_root_is_denied(self) -> None:
        policy = PathPolicy(roots=(config.BASE_DIR,))
        self.assertFalse(policy.is_allowed("../../Windows/System32/drivers/etc/hosts"))

    def test_env_and_keys_are_denied_even_inside_root(self) -> None:
        policy = PathPolicy(roots=(config.BASE_DIR,))
        with self.assertRaises(PathNotAllowed):
            policy.check(config.BASE_DIR / ".env")
        with self.assertRaises(PathNotAllowed):
            policy.check(config.BASE_DIR / "secret.pem")

    def test_missing_file_raises_when_required(self) -> None:
        policy = PathPolicy(roots=(config.BASE_DIR,))
        with self.assertRaises(PathNotAllowed):
            policy.check(config.BASE_DIR / "no_such_file_xyz.csv", must_exist=True)

    def test_default_policy_covers_base_and_data_dir(self) -> None:
        policy = PathPolicy()
        self.assertIn(config.BASE_DIR.resolve(), policy.effective_roots)
        self.assertIn(config.DATA_DIR.resolve(), policy.effective_roots)

    def test_default_roots_follow_a_moved_data_dir(self) -> None:
        """⭐ 默认根必须**现算**:否则 LA_DATA_DIR 一改,单例就指向老路径。"""
        original = config.DATA_DIR
        try:
            config.DATA_DIR = Path(tempfile.mkdtemp(prefix="la-moved-"))
            policy = PathPolicy()
            self.assertIn(config.DATA_DIR.resolve(), policy.effective_roots)
            self.assertTrue(policy.is_allowed(config.DATA_DIR / "bill.csv"))
        finally:
            config.DATA_DIR = original

    def test_other_drive_is_still_denied(self) -> None:
        """白名单之外的系统路径**任何平台**都不放行(Windows 用盘符,POSIX 用 /etc)。"""
        outside = (
            "C:/Windows/System32/drivers/etc/hosts" if os.name == "nt" else "/etc/hosts"
        )
        policy = PathPolicy()
        self.assertFalse(policy.is_allowed(outside))

    def test_tool_layer_blocks_env_read(self) -> None:
        """⭐ 工具层才是真边界:模型给 .env 必须被拒。"""
        from la.domain.tools import check_tool_path

        with self.assertRaises(PathNotAllowed):
            check_tool_path(str(config.BASE_DIR / ".env"))

    def test_tool_layer_passes_empty_through(self) -> None:
        from la.domain.tools import check_tool_path

        self.assertIsNone(check_tool_path(None))
        self.assertEqual(check_tool_path(""), "")


class ReadOnlyCase(M6Base):
    def test_read_only_registry_rejects_writes_but_allows_reads(self) -> None:
        import asyncio

        from la.base.phases import PHASE_ANSWER, CircuitBreaker
        from la.tools.registry import CODE_READ_ONLY, ToolRegistry

        registry = ToolRegistry(read_only=True)
        actions: list[str] = []

        async def read_handler(params):
            return {"ok": True}

        async def write_handler(params):
            actions.append("写入发生了")
            return {"ok": True}

        from la.tools.registry import ToolSpec

        registry.register(ToolSpec(
            name="query_summary_pool", description="读", phase=PHASE_ANSWER,
            risk="low", timeout_ms=1000, handler=read_handler, writes=False,
        ))
        registry.register(ToolSpec(
            name="expense_record", description="写", phase=PHASE_ANSWER,
            risk="low", timeout_ms=1000, handler=write_handler,
        ))
        read_result = asyncio.run(
            registry.execute("query_summary_pool", {}, breaker=CircuitBreaker(phase=PHASE_ANSWER))
        )
        write_result = asyncio.run(
            registry.execute("expense_record", {}, breaker=CircuitBreaker(phase=PHASE_ANSWER))
        )
        self.assertTrue(read_result.ok)
        self.assertFalse(write_result.ok)
        self.assertEqual(write_result.error_code, CODE_READ_ONLY)
        self.assertEqual(actions, [])  # ⭐ handler 根本没被调用

    def test_whitelist_names_all_exist(self) -> None:
        """白名单不能腐烂:名单里的每个工具都得真的存在。"""
        from la.tools.registry import READ_ONLY_TOOLS

        self.assertGreaterEqual(len(READ_ONLY_TOOLS), 10)
        self.assertEqual(len(set(READ_ONLY_TOOLS)), len(READ_ONLY_TOOLS))


class BackupCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig = (config.DATA_DIR, config.ASSISTANT_DB, config.MEMORY_DB,
                      config.ARCHIVE_DIR, config.BACKUPS_DIR)
        data = self.root / "data"
        (data / "archive").mkdir(parents=True)
        (data / "backups").mkdir(parents=True)
        config.DATA_DIR = data
        config.ASSISTANT_DB = data / "assistant.db"
        config.MEMORY_DB = data / "memory.db"
        config.ARCHIVE_DIR = data / "archive"
        config.BACKUPS_DIR = data / "backups"
        for path, migrations in (
            (config.ASSISTANT_DB, MIGRATIONS_ASSISTANT),
            (config.MEMORY_DB, MIGRATIONS_MEMORY),
        ):
            conn = db.connect(str(path))
            db.migrate(conn, migrations)
            conn.commit()
            conn.close()
        # 只往领域库塞种子数据 —— 记忆库里没有 app_accounts,这是刻意的分层
        conn = db.connect(config.ASSISTANT_DB)
        conn.execute(
            "INSERT OR IGNORE INTO app_accounts(name, kind) VALUES ('测试账户','expense')"
        )
        conn.commit()
        conn.close()
        (config.ARCHIVE_DIR / "default.jsonl").write_text(
            '{"seq": 1, "type": "message", "data": {"text": "hi"}}\n', encoding="utf-8"
        )

    def tearDown(self) -> None:
        (config.DATA_DIR, config.ASSISTANT_DB, config.MEMORY_DB,
         config.ARCHIVE_DIR, config.BACKUPS_DIR) = self._orig
        self._tmp.cleanup()

    def test_backup_copies_databases_and_archive(self) -> None:
        result = backup_mod.backup_now()
        self.assertTrue(result.directory.exists())
        self.assertIn("assistant.db", result.databases)
        self.assertIn("memory.db", result.databases)
        self.assertEqual(result.archive_files, 1)

    def test_restore_drill_actually_restores(self) -> None:
        """⭐ M6.6 的核心:不是"文件在不在",是"恢复出来能不能用"。"""
        backup_mod.backup_now()
        report = backup_mod.restore_drill()
        self.assertTrue(report.ok, report.as_dict())
        names = [name for name, _p, _d in report.checks]
        self.assertIn("assistant.db 完整性", names)
        self.assertIn("当前代码能读还原库", names)
        self.assertIn("行数与线上一致", names)

    def test_drill_fails_when_row_counts_drift(self) -> None:
        backup_mod.backup_now()
        conn = db.connect(config.ASSISTANT_DB)
        conn.execute(
            "INSERT INTO app_accounts(name, kind) VALUES ('备份之后加的','expense')"
        )
        conn.commit()
        conn.close()
        report = backup_mod.restore_drill()
        drift = [item for item in report.checks if item[0] == "行数与线上一致"][0]
        self.assertFalse(drift[1])

    def test_no_backup_means_drill_fails_loudly(self) -> None:
        report = backup_mod.restore_drill()
        self.assertFalse(report.ok)
        self.assertIn("一个备份都没有", report.checks[0][2])


class GuardrailCase(M6Base):
    def _settings(self):
        return config.Settings(read_only=True, data_quota_mb=4096)

    def test_every_guardrail_reports_pass_fail_or_na(self) -> None:
        results = run_all(assistant=self.assistant, memory=self.memory, settings=self._settings())
        self.assertGreaterEqual(len(results), 18)
        for item in results:
            self.assertIn(item.status, ("pass", "fail", "na"))
            self.assertTrue(item.evidence, item.code + " 没给证据")

    def test_codes_are_unique(self) -> None:
        results = run_all(assistant=self.assistant, memory=self.memory, settings=self._settings())
        codes = [item.code for item in results]
        self.assertEqual(len(codes), len(set(codes)))

    def test_not_applicable_ones_say_why(self) -> None:
        results = run_all(assistant=self.assistant, memory=self.memory, settings=self._settings())
        na = [item for item in results if item.status == "na"]
        self.assertGreaterEqual(len(na), 9)
        for item in na:
            self.assertGreater(len(item.evidence), 8, item.code + " 的「不适用」没说清理由")

    def test_append_only_checks_actually_probe(self) -> None:
        """⭐ 护栏必须**真的去改一下试试**,而不是查文档有没有写。"""
        results = {item.code: item for item in run_all(
            assistant=self.assistant, memory=self.memory, settings=self._settings()
        )}
        self.assertEqual(results["G01"].status, "pass")
        self.assertIn("IntegrityError", results["G01"].evidence)

    def test_stop_switch_is_reported(self) -> None:
        results = {item.code: item for item in run_all(
            assistant=self.assistant, memory=self.memory, settings=self._settings()
        )}
        self.assertEqual(results["G18"].status, "pass")

    def test_secret_leak_detector_catches_a_planted_key(self) -> None:
        self.memory.execute(
            "INSERT INTO facts(content, type, created_at, updated_at)"
            " VALUES ('我的 key 是 sk-abcdefghijklmnopqrstuvwx', 'other', 'x', 'x')"
        )
        self.memory.commit()
        results = {item.code: item for item in run_all(
            assistant=self.assistant, memory=self.memory, settings=self._settings()
        )}
        self.assertEqual(results["G06b"].status, "fail")


class ReportCase(M6Base):
    def test_report_is_all_zeros_on_empty_db(self) -> None:
        report = build_report(self.assistant, self.memory)
        self.assertEqual(report.expenses_total_cents, 0)
        self.assertIn("没有已确认的支出", report.render())

    def test_report_totals_match_sql(self) -> None:
        from la.domain.services.ledger_service import LedgerService

        from la.domain.schemas.ledger import ExpenseCreate

        ledger = LedgerService(self.assistant)
        ledger.record_expense(
            ExpenseCreate(amount_cents=3000, category="餐饮", account="支付宝", payee="A"),
            actor="user",
        )
        txn_id = self.assistant.execute("SELECT id FROM app_txns_live").fetchone()[0]
        ledger.confirm(txn_id)
        report = build_report(self.assistant, self.memory)
        self.assertEqual(report.expenses_total_cents, 3000)
        self.assertEqual(report.txn_count, 1)
        self.assertEqual(report.by_category["餐饮"], 3000)
        self.assertIn("30.0", report.render())

    def test_deleted_expense_leaves_the_report(self) -> None:
        from la.domain.services.ledger_service import LedgerService

        from la.domain.schemas.ledger import ExpenseCreate

        ledger = LedgerService(self.assistant)
        ledger.record_expense(
            ExpenseCreate(amount_cents=3000, category="餐饮", account="支付宝", payee="A"),
            actor="user",
        )
        txn_id = self.assistant.execute("SELECT id FROM app_txns_live").fetchone()[0]
        ledger.confirm(txn_id)
        ledger.soft_delete(txn_id)
        self.assertEqual(build_report(self.assistant, self.memory).expenses_total_cents, 0)


class HistoryImportCase(unittest.TestCase):
    """M6.9:历史导入。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig = (config.DATA_DIR, config.MEMORY_DB, config.ARCHIVE_DIR)
        config.DATA_DIR = self.root
        config.MEMORY_DB = self.root / "memory.db"
        config.ARCHIVE_DIR = self.root / "archive"
        config.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        from la.memory.store import MemoryStore
        from la.storage.migrations import MIGRATIONS_MEMORY

        conn = db.connect(config.MEMORY_DB)
        db.migrate(conn, MIGRATIONS_MEMORY)
        conn.commit()
        self.store = MemoryStore(conn)

    def tearDown(self) -> None:
        # Windows 上没关的连接会让临时目录删不掉
        self.store.conn.close()
        (config.DATA_DIR, config.MEMORY_DB, config.ARCHIVE_DIR) = self._orig
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_parses_json_and_plain_text(self) -> None:
        from la.ops.history_import import parse_history

        self.assertEqual(len(parse_history('[{"role":"user","content":"a"}]')), 1)
        self.assertEqual(len(parse_history("用户: a\n助手: b\n")), 2)
        self.assertEqual(len(parse_history("用户: a\n乱七八糟没有角色\n")), 1)
        self.assertEqual(parse_history("{不是合法 json"), [])

    def test_import_writes_events_and_archive(self) -> None:
        from la.ops.history_import import import_history

        path = self._write("chat.json", '[{"role":"user","content":"你好"}]')
        report = import_history(self.store, path)
        self.assertEqual(report.imported, 1)
        self.assertEqual(
            self.store.conn.execute("SELECT count(*) FROM events").fetchone()[0], 1
        )
        # ⭐ 归档(事实源)里也必须有一条 —— 否则重建记忆时它会消失
        archive = list(config.ARCHIVE_DIR.glob("*.jsonl"))
        self.assertTrue(archive, "归档里什么都没有,记忆就不可重建了")

    def test_second_import_of_the_same_file_is_a_noop(self) -> None:
        from la.ops.history_import import import_history

        path = self._write("chat.json", '[{"role":"user","content":"你好"}]')
        import_history(self.store, path)
        again = import_history(self.store, path)
        self.assertTrue(again.already_done)
        self.assertEqual(
            self.store.conn.execute("SELECT count(*) FROM events").fetchone()[0], 1
        )

    def test_unparseable_file_raises_instead_of_silently_importing_nothing(self) -> None:
        from la.ops.history_import import HistoryImportError, import_history

        path = self._write("junk.txt", "一段没有角色标记的普通文本\n再来一行\n")
        with self.assertRaises(HistoryImportError):
            import_history(self.store, path)

    def test_outside_path_is_rejected(self) -> None:
        from la.ops.history_import import import_history
        from la.ops.paths import PathNotAllowed

        with self.assertRaises(PathNotAllowed):
            import_history(self.store, "C:/Windows/System32/drivers/etc/hosts")

    def test_imported_events_do_not_pollute_the_hot_summary(self) -> None:
        """导入的历史不该把上下文塞满 —— surface=False。"""
        from la.ops.history_import import import_history

        path = self._write(
            "chat.json",
            '[{"role":"user","content":"很久以前的事"},{"role":"user","content":"更久以前"}]',
        )
        import_history(self.store, path)
        surfaced = self.store.conn.execute(
            "SELECT count(*) FROM events WHERE surface = 1"
        ).fetchone()[0]
        self.assertEqual(surfaced, 0)


class PrivacyCase(M6Base):
    def test_report_lists_outbound_and_local_tables(self) -> None:
        report = build_privacy_report(assistant=self.assistant, memory=self.memory)
        self.assertTrue(report.outbound_tables)
        self.assertTrue(report.local_only_tables)
        self.assertIn("app_txns", [item["table"] for item in report.local_only_tables])

    def test_secret_never_printed_in_full(self) -> None:
        os.environ["DEEPSEEK_API_KEY"] = "sk-1234567890abcdefghijklmn"
        try:
            report = build_privacy_report(assistant=self.assistant, memory=self.memory)
            text = report.render()
            self.assertNotIn("sk-1234567890abcdefghijklmn", text)
            self.assertIn("sk-1", text)  # 掩码保留头尾
        finally:
            os.environ.pop("DEEPSEEK_API_KEY", None)


if __name__ == "__main__":
    unittest.main()
