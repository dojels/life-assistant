"""M4.1 验收测试:三级去重、账单导入、规则引擎、纠正回写、退款关联、Beancount 导出。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from la.domain.audit import AuditSink
from la.domain.beancount import account_full_name, export_beancount, format_amount
from la.domain.dedup import find_duplicate, level1_key, level2_key
from la.domain.importers.statement import (
    SOURCE_ALIPAY,
    parse_amount,
    parse_statement,
    parse_time,
    read_rows,
)
from la.domain.rules import ORIGIN_LEARNED, RuleEngine, Subject, rule_matches
from la.domain.schemas.ledger import ExpenseCreate, ExpenseQuery
from la.domain.services.ledger_service import LedgerError, LedgerService
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT

ALIPAY_CSV = """支付宝交易记录明细查询
账号:[test@example.com]
起始日期:[2026-10-01 00:00:00]    终止日期:[2026-10-31 23:59:59]
---------------------------------交易记录明细列表------------------------------------
交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注
2026-10-08 12:30:00,餐饮美食,老张牛肉面,zhang***,午饭,支出,38.00,余额宝,交易成功,20261008001,MB001,
2026-10-08 18:10:00,交通出行,滴滴出行,didi***,打车回家,支出,26.50,花呗,交易成功,20261008002,MB002,
2026-10-09 09:00:00,其他,某公司,,退款,收入,38.00,余额,交易成功,20261009001,MB003,
------------------------------------------------------------------------------------
共 2 笔记录
已全部展示
"""

WECHAT_ROWS = [
    ["微信支付账单明细"],
    ["交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)", "支付方式", "当前状态", "交易单号", "商户单号", "备注"],
    ["2026-10-10 08:15:00", "商户消费", "肯德基", "早餐", "支出", "¥21.00", "零钱", "支付成功", "4200001", "M1", ""],
    ["2026-10-10 12:00:00", "转账", "朋友", "还款", "收入", "¥100.00", "零钱", "已收钱", "4200002", "M2", ""],
]


def column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def build_xlsx(rows: list[list[str]]) -> bytes:
    """手工拼一个最小 xlsx —— 只为验证读取器,不引第三方依赖。"""
    shared: list[str] = []
    index: dict[str, int] = {}
    row_xml: list[str] = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            if value not in index:
                index[value] = len(shared)
                shared.append(value)
            cells.append('<c r="' + column_letter(c) + str(r) + '" t="s"><v>' + str(index[value]) + "</v></c>")
        row_xml.append('<row r="' + str(r) + '">' + "".join(cells) + "</row>")
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet = '<?xml version="1.0"?><worksheet xmlns="' + ns + '"><sheetData>' + "".join(row_xml) + "</sheetData></worksheet>"
    sst = '<?xml version="1.0"?><sst xmlns="' + ns + '">' + "".join("<si><t>" + v + "</t></si>" for v in shared) + "</sst>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", sst)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


class Fixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.service = LedgerService(self.conn, audit=AuditSink(self.conn))

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path


# ==========================================================================
# 解析
# ==========================================================================
class ParsingTestCase(unittest.TestCase):
    def test_amount_and_time_parsing(self):
        self.assertEqual(parse_amount("¥38.00"), 3800)
        self.assertEqual(parse_amount("1,234.56"), 123456)
        self.assertEqual(parse_amount("-26.5"), -2650)
        self.assertIsNone(parse_amount(""))
        self.assertEqual(parse_time("2026-10-08 12:30:00")[:16], "2026-10-08T12:30")
        self.assertEqual(parse_time("2026/10/08 12:30")[:16], "2026-10-08T12:30")
        self.assertIsNone(parse_time("不是时间"))

    def test_header_is_found_behind_junk_lines(self):
        path = Path(tempfile.mkdtemp()) / "alipay.csv"
        path.write_text(ALIPAY_CSV, encoding="utf-8")
        result = parse_statement(path)
        self.assertEqual(result.source, SOURCE_ALIPAY)
        self.assertEqual(result.header_line, 5)
        self.assertEqual(len(result.rows), 3)
        self.assertEqual(len(result.expenses), 2, "收入行不算支出")
        first = result.rows[0]
        self.assertEqual(first.amount_cents, 3800)
        self.assertEqual(first.payee, "老张牛肉面")
        self.assertEqual(first.order_id, "20261008001")
        self.assertEqual(first.direction, "支出")

    def test_wechat_xlsx_is_read_with_stdlib_only(self):
        path = Path(tempfile.mkdtemp()) / "wechat.xlsx"
        path.write_bytes(build_xlsx(WECHAT_ROWS))
        rows = read_rows(path)
        self.assertEqual(len(rows), 4)
        result = parse_statement(path)
        self.assertEqual(result.source, "import:wechat")
        self.assertEqual(len(result.expenses), 1)
        self.assertEqual(result.expenses[0].amount_cents, 2100)
        self.assertEqual(result.expenses[0].payee, "肯德基")


# ==========================================================================
# 三级去重
# ==========================================================================
class DedupTestCase(Fixture):
    def _record(self, **overrides):
        payload = {
            "amount_cents": 3800,
            "payee": "老张牛肉面",
            "category": "餐饮",
            "ts": "2026-10-08T12:30:00+08:00",
        }
        payload.update(overrides)
        return self.service.record_expense(ExpenseCreate(**payload), status="confirmed")

    def test_level1_order_id(self):
        self._record()
        self.conn.execute("UPDATE app_txns SET dedup_key = ?", (level1_key("import:alipay", "A1"),))
        self.conn.commit()
        decision = find_duplicate(
            self.conn, source="import:alipay", ts="2026-10-08T12:31:00+08:00",
            direction="支出", amount_cents=3800, peer="老张牛肉面", item=None,
            method=None, order_id="A1",
        )
        self.assertEqual(decision.level, "L1")
        self.assertIsNotNone(decision.duplicate_txn_id)

    def test_level2_hash_catches_seconds_difference(self):
        self._record()
        self.conn.execute(
            "UPDATE app_txns SET dedup_key = ?",
            (level2_key(source="import:alipay", ts="2026-10-08T12:30:00+08:00", direction="支出",
                        amount_cents=3800, peer="老张牛肉面", item="午饭", method="余额宝"),),
        )
        self.conn.commit()
        # 同一笔账,导出时间差了几秒 —— 二级键只取到"日",照样判重
        decision = find_duplicate(
            self.conn, source="import:alipay", ts="2026-10-08T12:30:07+08:00",
            direction="支出", amount_cents=3800, peer="老张牛肉面", item="午饭",
            method="余额宝", order_id=None,
        )
        self.assertEqual(decision.level, "L2")

    def test_level3_only_reports_candidates_and_asks_for_confirmation(self):
        self._record()
        decision = find_duplicate(
            self.conn, source="import:wechat", ts="2026-10-09T12:30:00+08:00",
            direction="支出", amount_cents=3800, peer="老张牛肉面", item="午饭",
            method="零钱", order_id="W1",
        )
        self.assertEqual(decision.level, "L3")
        self.assertTrue(decision.needs_confirmation)
        self.assertIsNone(decision.duplicate_txn_id, "模糊命中绝不自动合并")
        self.assertEqual(len(decision.fuzzy_candidates), 1)

    def test_different_amount_is_new(self):
        self._record()
        decision = find_duplicate(
            self.conn, source="import:wechat", ts="2026-10-08T12:30:00+08:00",
            direction="支出", amount_cents=9900, peer="老张牛肉面", item="午饭",
            method=None, order_id=None,
        )
        self.assertEqual(decision.level, "new")


# ==========================================================================
# 导入
# ==========================================================================
class ImportTestCase(Fixture):
    def test_import_creates_confirmed_transactions(self):
        path = self.write("alipay.csv", ALIPAY_CSV)
        report = self.service.import_statement(path)
        # M9 起收入行也会入账(以前被静默丢掉) —— 所以是 3 笔:2 支出 + 1 收入(退款 38)
        self.assertEqual(report.imported, 3, report.summary())
        self.assertEqual(report.imported_income, 1)
        self.assertEqual(report.source, SOURCE_ALIPAY)
        rows = self.conn.execute(
            "SELECT status, payee, source FROM app_txns ORDER BY id"
        ).fetchall()
        self.assertEqual([row["status"] for row in rows], ["confirmed"] * 3)
        self.assertTrue(all(row["source"] == SOURCE_ALIPAY for row in rows))

    def test_reimport_after_undo_restores_the_batch(self):
        """撤销导入后再导同一个文件:恢复那批行,不能被 sha256 永久短路。"""
        from la.domain.receipts import ReceiptLog

        self.service.receipts = ReceiptLog(self.conn)
        path = self.write("alipay.csv", ALIPAY_CSV)
        first = self.service.import_statement(path)
        receipt = self.service.receipts.recent(limit=1)[0]
        self.service.revert_txn(receipt)
        voided = self.conn.execute(
            "SELECT count(*) FROM app_txns WHERE status = 'void'"
        ).fetchone()[0]
        self.assertEqual(int(voided), first.imported)

        again = self.service.import_statement(path)
        self.assertFalse(again.already_imported)
        self.assertEqual(again.imported, first.imported)
        live = self.conn.execute(
            "SELECT count(*) FROM app_txns WHERE status = 'confirmed'"
        ).fetchone()[0]
        self.assertEqual(int(live), first.imported)

    def test_same_file_twice_is_short_circuited_by_sha256(self):
        path = self.write("alipay.csv", ALIPAY_CSV)
        first = self.service.import_statement(path)
        second = self.service.import_statement(path)
        self.assertFalse(first.already_imported)
        self.assertTrue(second.already_imported)
        self.assertIn("已导入过", second.summary())
        count = self.conn.execute("SELECT count(*) FROM app_txns").fetchone()[0]
        self.assertEqual(count, 3, "重复上传不得重复入账")

    def test_renamed_copy_of_the_same_file_is_still_caught(self):
        first = self.write("a.csv", ALIPAY_CSV)
        second = self.write("b.csv", ALIPAY_CSV)
        self.service.import_statement(first)
        report = self.service.import_statement(second)
        self.assertTrue(report.already_imported, "改名不算换文件 —— 认的是内容")

    def test_row_level_dedup_when_only_one_row_is_new(self):
        path = self.write("alipay.csv", ALIPAY_CSV)
        self.service.import_statement(path)
        extra = ALIPAY_CSV.replace(
            "共 2 笔记录",
            "2026-10-11 20:00:00,餐饮美食,沙县小吃,,晚饭,支出,15.00,余额宝,交易成功,20261011001,MB009,\n共 3 笔记录",
        )
        report = self.service.import_statement(self.write("alipay2.csv", extra))
        self.assertEqual(report.imported, 1)
        self.assertEqual(report.skipped_duplicate, 3, "老的三行(2 支出 + 1 收入)要按行判重")

    def test_fuzzy_rows_are_reported_not_merged(self):
        self.service.record_expense(
            ExpenseCreate(amount_cents=3800, payee="老张牛肉面", category="餐饮",
                          ts="2026-10-08T12:00:00+08:00"),
            status="confirmed",
        )
        path = self.write("alipay.csv", ALIPAY_CSV)
        report = self.service.import_statement(path)
        self.assertEqual(report.skipped_fuzzy, 1)
        self.assertEqual(report.fuzzy_candidates[0]["payee"], "老张牛肉面")
        count = self.conn.execute("SELECT count(*) FROM app_txns").fetchone()[0]
        self.assertEqual(count, 3, "存疑的行不入库,等用户确认")

    def test_dry_run_writes_nothing(self):
        path = self.write("alipay.csv", ALIPAY_CSV)
        report = self.service.import_statement(path, dry_run=True)
        self.assertEqual(report.imported, 3)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM app_txns").fetchone()[0], 0)
        batches = self.conn.execute("SELECT count(*) FROM app_import_batches").fetchone()[0]
        self.assertEqual(batches, 0, "试运行不该占用文件指纹")

    def test_import_writes_audit_and_outbox(self):
        path = self.write("alipay.csv", ALIPAY_CSV)
        self.service.import_statement(path)
        audit = [row["action"] for row in self.conn.execute("SELECT action FROM ops_audit")]
        self.assertIn("ledger.import", audit)
        pending = self.conn.execute(
            "SELECT count(*) FROM ops_outbox WHERE processed_at IS NULL"
        ).fetchone()[0]
        self.assertEqual(pending, 4, "3 笔交易(2 支出 + 1 收入)+ 1 次导入")


# ==========================================================================
# 规则引擎
# ==========================================================================
class RulesTestCase(Fixture):
    def test_rule_matching_operators(self):
        engine = RuleEngine(self.conn)
        rule = engine.add(match_field="item", match_value="午餐", category="餐饮")
        self.assertTrue(rule_matches(rule, Subject(item="公司午餐")))
        self.assertFalse(rule_matches(rule, Subject(item="晚餐")))

        exact = engine.add(match_field="peer", match_op="equals", match_value="滴滴", category="交通")
        self.assertTrue(rule_matches(exact, Subject(peer="滴滴")))
        self.assertFalse(rule_matches(exact, Subject(peer="滴滴出行")))

        window = engine.add(match_field="time_window", match_op="in_range",
                            match_value="11:00-14:00", category="餐饮")
        self.assertTrue(rule_matches(window, Subject(ts="2026-10-08T12:30:00+08:00")))
        self.assertFalse(rule_matches(window, Subject(ts="2026-10-08T19:30:00+08:00")))

    def test_same_rule_twice_updates_instead_of_duplicating(self):
        engine = RuleEngine(self.conn)
        first = engine.add(match_field="peer", match_op="equals", match_value="星巴克",
                           category="餐饮")
        second = engine.add(match_field="peer", match_op="equals", match_value="星巴克",
                            category="购物")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(engine.list()), 1)

    def test_import_applies_rules_and_counts_hits(self):
        engine = RuleEngine(self.conn)
        engine.add(match_field="peer", match_op="equals", match_value="老张牛肉面",
                   category="餐饮")
        path = self.write("alipay.csv", ALIPAY_CSV)
        report = self.service.import_statement(path)
        self.assertEqual(report.rule_hits, 1)
        rules = engine.list()
        self.assertEqual(rules[0].hit_count, 1)
        row = self.conn.execute(
            "SELECT c.name FROM app_postings p JOIN app_categories c ON c.id = p.category_id"
            " JOIN app_txns t ON t.id = p.txn_id WHERE t.payee = '老张牛肉面'"
        ).fetchone()
        self.assertEqual(row["name"], "餐饮")

    def test_correction_is_learned_as_a_rule(self):
        record = self.service.record_expense(
            ExpenseCreate(amount_cents=2800, payee="星巴克", category="其他")
        )
        self.service.correct_last(txn_id=record.id, category="餐饮", actor="user")
        rules = RuleEngine(self.conn).list()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].origin, ORIGIN_LEARNED)
        self.assertEqual(rules[0].match_value, "星巴克")

    def test_rule_applies_to_conversation_records_too(self):
        """"先规则、规则未命中再靠模型" —— 对话记账也必须吃到规则。"""
        RuleEngine(self.conn).add(
            match_field="peer", match_op="equals", match_value="星巴克", category="餐饮"
        )
        record = self.service.record_expense(
            ExpenseCreate(amount_cents=2800, payee="星巴克", source="chat")
        )
        self.assertEqual(record.category, "餐饮", "规则命中就该直接分好类")
        rules = RuleEngine(self.conn).list()
        self.assertEqual(rules[0].hit_count, 1)

    def test_no_rule_means_no_category(self):
        record = self.service.record_expense(
            ExpenseCreate(amount_cents=2800, payee="没见过的店", source="chat")
        )
        self.assertIsNone(record.category, "规则未命中就留空,交给模型或用户")

    def test_correct_last_without_id_targets_the_latest(self):
        self.service.record_expense(ExpenseCreate(amount_cents=100, payee="A", category="餐饮"))
        latest = self.service.record_expense(
            ExpenseCreate(amount_cents=200, payee="B", category="餐饮")
        )
        fixed = self.service.correct_last(category="交通", actor="user")
        self.assertEqual(fixed.id, latest.id)
        self.assertEqual(fixed.category, "交通")

    def test_correcting_amount_keeps_the_balance(self):
        record = self.service.record_expense(
            ExpenseCreate(amount_cents=3800, payee="老张牛肉面", category="餐饮")
        )
        fixed = self.service.correct_last(txn_id=record.id, amount_cents=4200, actor="user")
        self.assertEqual(fixed.amount_cents, 4200)
        self.assertEqual(sum(amount for _a, amount in fixed.postings), 0, "改金额后仍需配平")


# ==========================================================================
# 退款与导出
# ==========================================================================
class RefundAndExportTestCase(Fixture):
    def test_link_refund_can_void_the_original(self):
        original = self.service.record_expense(
            ExpenseCreate(amount_cents=3800, payee="老张牛肉面", category="餐饮"),
            status="confirmed",
        )
        refund = self.service.record_expense(
            ExpenseCreate(amount_cents=3800, payee="老张牛肉面", category="餐饮",
                          ts="2026-10-09T10:00:00+08:00"),
            status="confirmed",
        )
        linked = self.service.link_refund(refund.id, original.id, void_original=True)
        self.assertEqual(linked.id, refund.id)
        row = self.conn.execute(
            "SELECT refund_of_txn_id FROM app_txns WHERE id = ?", (refund.id,)
        ).fetchone()
        self.assertEqual(row["refund_of_txn_id"], original.id)
        status = self.conn.execute(
            "SELECT status FROM app_txns WHERE id = ?", (original.id,)
        ).fetchone()
        self.assertEqual(status["status"], "void", "全额退款应当冲掉原单")

        with self.assertRaises(LedgerError):
            self.service.link_refund(original.id, original.id)

    def test_beancount_export_is_wellformed(self):
        self.service.record_expense(
            ExpenseCreate(amount_cents=3800, payee="老张牛肉面", narration="午饭",
                          category="餐饮", ts="2026-10-08T12:30:00+08:00"),
            status="confirmed",
        )
        self.service.record_expense(
            ExpenseCreate(amount_cents=100, payee="未确认的", category="餐饮"),
            status="draft",
        )
        text = self.service.export_beancount()
        self.assertIn("Expenses:餐饮", text)
        self.assertIn("Assets:支付宝", text)
        self.assertIn("38.00 CNY", text)
        self.assertIn("-38.00 CNY", text)
        self.assertIn('"老张牛肉面" "午饭"', text)
        self.assertIn(' * "老张牛肉面"', text)
        self.assertNotIn("未确认的", text, "默认只导已确认的")
        self.assertEqual(account_full_name("expense", "餐饮"), "Expenses:餐饮")
        self.assertEqual(format_amount(3800), "38.00")

    def test_beancount_export_legs_sum_to_zero_per_transaction(self):
        self.service.record_expense(
            ExpenseCreate(amount_cents=2650, payee="滴滴", category="交通"),
            status="confirmed",
        )
        text = self.service.export_beancount()
        amounts = [line.strip().split()[-2] for line in text.splitlines()
                   if line.startswith("  ") and "CNY" in line]
        total = sum(float(value) for value in amounts)
        self.assertAlmostEqual(total, 0.0, places=6)


# ==========================================================================
# DoD:导入 + 聚合 的端到端
# ==========================================================================
class DoDTestCase(Fixture):
    def test_import_then_aggregate_by_category(self):
        engine = RuleEngine(self.conn)
        engine.add(match_field="peer", match_op="equals", match_value="滴滴出行",
                   category="交通")
        path = self.write("alipay.csv", ALIPAY_CSV)
        self.service.import_statement(path)
        # 账单自带的"餐饮美食"会被解析成字典里的"餐饮"(别名/子串,确定性映射)

        result = self.service.query_expenses(ExpenseQuery(period="2026-10", dimension="category"))
        groups = {row.key: row.total_cents for row in result.groups}
        self.assertEqual(result.source, "ledger")
        self.assertEqual(result.txn_count, 2)
        self.assertEqual(groups.get("餐饮"), 3800)
        self.assertEqual(groups.get("交通"), 2650, "规则把滴滴归到了交通")
        self.assertEqual(result.total_cents, 6450)

    def test_conversation_then_confirm_then_aggregate(self):
        record = self.service.record_expense(
            ExpenseCreate(amount_cents=3800, payee="老张牛肉面", category="餐饮",
                          source="chat", ts="2026-10-08T12:30:00+08:00")
        )
        before = self.service.query_expenses(ExpenseQuery(period="2026-10"))
        self.assertEqual(before.total_cents, 0, "草稿不计入")

        self.service.confirm(record.id)
        after = self.service.query_expenses(ExpenseQuery(period="2026-10"))
        self.assertEqual(after.total_cents, 3800)
        self.assertEqual(after.txn_count, 1)


if __name__ == "__main__":
    unittest.main()
