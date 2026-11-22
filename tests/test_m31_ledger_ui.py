"""D164:账本页改版 —— 收支双视图、自定义区间、分类图、弹窗记账、分类管理。

用户 2026-10-24 的四条要求,逐条钉在页面上:
  ① 记一笔是**按钮 + 弹窗**,不再占页面;
  ② 分类可以自己管(新建 / 改名 / 归档);
  ③ 自定义日期区间 + 按分类的图;
  ④ 收入与支出分成两个页面。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from la import config
from la.domain.schemas.ledger import TxnCreate
from la.domain.services.ledger_service import LedgerService
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT
from la.web.app import create_app


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "ARTIFACTS_DIR", "MEDIA_DIR",
                        "ASSISTANT_DB", "MEMORY_DB", "LOGS_DIR")
        }
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.MEDIA_DIR = self.tmp / "media"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config.LOGS_DIR = self.tmp / "logs"
        config.ensure_runtime_dirs()
        self.conn = db.connect_assistant()
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.ledger = LedgerService(self.conn)
        self.client = TestClient(create_app(), follow_redirects=False)
        self.client.post("/setup", data={"pin": "123456", "pin2": "123456"})

    def tearDown(self) -> None:
        self.client.close()
        self.conn.close()
        for key, value in self._patched.items():
            setattr(config, key, value)
        self._tmp.cleanup()


class LedgerPageTest(_Base):
    def _seed(self) -> None:
        self.ledger.record_expense(
            TxnCreate(amount_cents=3800, payee="老张牛肉面", category="餐饮",
                      ts="2026-10-08T12:00:00+08:00"),
            status="confirmed", actor="user",
        )
        self.ledger.record_expense(
            TxnCreate(direction="income", amount_cents=100000, payee="某公司",
                      category="工资", ts="2026-10-09T09:00:00+08:00"),
            status="confirmed", actor="user",
        )

    def test_page_has_tabs_donut_and_button(self) -> None:
        self._seed()
        html = self.client.get("/ledger?period=2026-10").text
        # ① 记一笔是按钮(链接到 compose),不是摊在页面上的表单
        self.assertIn('id="lg-add"', html)
        self.assertIn('id="lg-compose"', html)
        # ③ 自定义区间
        self.assertIn('name="start"', html)
        self.assertIn('name="end"', html)
        # 分类图
        self.assertIn("conic-gradient(", html)
        self.assertIn("lg-legend-name", html)

    def test_dialog_is_closed_until_asked(self) -> None:
        html = self.client.get("/ledger?period=2026-10").text
        self.assertNotIn('data-open="1"', html, "默认不该自己弹出来")
        opened = self.client.get("/ledger?period=2026-10&compose=1").text
        self.assertIn('data-open="1"', opened, "没有 JS 时也要能把表单打开")
        self.assertIn('name="amount"', opened)

    def test_income_view_shows_only_income_categories(self) -> None:
        self._seed()
        expense = self.client.get("/ledger?view=expense&period=2026-10").text
        income = self.client.get("/ledger?view=income&period=2026-10").text
        self.assertIn("lg-legend-name\">餐饮", expense)
        self.assertNotIn("lg-legend-name\">工资", expense)
        self.assertIn("lg-legend-name\">工资", income)
        self.assertNotIn("lg-legend-name\">餐饮", income)

    def test_custom_range_filters_the_list(self) -> None:
        self._seed()
        self.ledger.record_expense(
            TxnCreate(amount_cents=9900, payee="月底大采购", category="购物",
                      ts="2026-10-20T10:00:00+08:00"),
            status="confirmed", actor="user",
        )
        html = self.client.get("/ledger?view=expense&start=2026-10-01&end=2026-10-10").text
        self.assertIn("老张牛肉面", html)
        self.assertNotIn("月底大采购", html, "区间外的账不该出现在明细里")
        self.assertIn("2026.10.01 – 10.10", html, "要把区间说人话")

    def test_trend_is_hidden_until_two_months_have_data(self) -> None:
        """只有一个月有账时不画趋势 —— 六根柱子里五根是空气,看不出任何东西。"""
        self._seed()
        html = self.client.get("/ledger?period=2026-10").text
        self.assertNotIn("近半年", html)
        self.ledger.record_expense(
            TxnCreate(amount_cents=1000, payee="九月那笔", category="餐饮",
                      ts="2026-09-10T12:00:00+08:00"),
            status="confirmed", actor="user",
        )
        html = self.client.get("/ledger?period=2026-10").text
        self.assertIn("近半年", html, "有第二个月的数据了,趋势就该出现")

    def test_ranking_is_a_bounded_window(self) -> None:
        """分类排行是个固定窗口(2026-10-24 用户:"下面是空的,太影响美观")。"""
        self._seed()
        html = self.client.get("/ledger?period=2026-10").text
        self.assertIn('class="lg-ranks"', html)
        self.assertIn("lg-rank-foot", html)
        self.assertIn("共 1 个分类", html)

    def test_backwards_range_is_explained_not_crashed(self) -> None:
        response = self.client.get("/ledger?start=2026-10-20&end=2026-10-01")
        self.assertEqual(response.status_code, 200, "填反了也不能白屏")
        self.assertIn("结束必须晚于开始", response.text)

    def test_old_month_link_still_works(self) -> None:
        self._seed()
        html = self.client.get("/ledger?period=2026-10").text
        self.assertIn("2026 年 10 月", html)
        self.assertIn("1,000.00", html)

    def test_add_goes_back_to_where_you_came_from(self) -> None:
        response = self.client.post(
            "/ledger/add",
            data={"direction": "income", "amount": "500.50", "category": "奖金",
                  "account": "银行卡", "payee": "年终", "next": "/ledger?view=income&period=2026-10"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("view=income", response.headers["location"])

    def test_add_switches_to_the_view_it_belongs_to(self) -> None:
        """在「收入」页记了一笔支出 —— 要跳到支出页,不然看起来像没记上。"""
        response = self.client.post(
            "/ledger/add",
            data={"direction": "expense", "amount": "12",
                  "next": "/ledger?view=income&period=2026-10"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("view=expense", response.headers["location"])

    def test_bad_amount_is_explained(self) -> None:
        response = self.client.post(
            "/ledger/add", data={"amount": "abc", "next": "/ledger"}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["location"])

    def test_next_cannot_leave_the_ledger(self) -> None:
        response = self.client.post(
            "/ledger/add",
            data={"amount": "1", "next": "https://evil.example/x"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/ledger")


class CategoryAdminTest(_Base):
    def test_lists_categories_with_usage(self) -> None:
        self.ledger.record_expense(
            TxnCreate(amount_cents=3800, payee="老张牛肉面", category="餐饮"),
            status="confirmed", actor="user",
        )
        html = self.client.get("/ledger/categories?kind=expense").text
        self.assertIn("餐饮", html)
        self.assertIn("用了 1 笔", html)
        self.assertIn("新建一个支出分类", html)

    def test_create_category_from_the_page(self) -> None:
        response = self.client.post(
            "/ledger/categories/add", data={"name": "宠物", "kind": "expense"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("宠物", self.client.get("/ledger/categories?kind=expense").text)
        self.assertIn("宠物", self.ledger.list_categories("expense"))

    def test_rename_keeps_the_id(self) -> None:
        self.ledger.record_expense(
            TxnCreate(amount_cents=3800, payee="老张牛肉面", category="餐饮"),
            status="confirmed", actor="user",
        )
        before = {c["name"]: c for c in self.ledger.list_categories_detail("expense")}
        category_id = before["餐饮"]["id"]
        response = self.client.post(
            "/ledger/categories/" + str(category_id) + "/rename",
            data={"name": "吃饭", "kind": "expense"}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        after = {c["name"]: c for c in self.ledger.list_categories_detail("expense")}
        self.assertIn("吃饭", after)
        self.assertEqual(after["吃饭"]["id"], category_id, "改名不能换 id,老账还指着它")
        self.assertEqual(after["吃饭"]["txn_count"], 1)

    def test_rename_to_existing_name_is_refused(self) -> None:
        rows = {c["name"]: c for c in self.ledger.list_categories_detail("expense")}
        response = self.client.post(
            "/ledger/categories/" + str(rows["交通"]["id"]) + "/rename",
            data={"name": "餐饮", "kind": "expense"}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        location = response.headers["location"]
        self.assertIn("err=", location)
        self.assertIn("%E9%87%8D%E5%90%8D", location, "要说清楚是重名(URL 编码后的「重名」)")

    def test_archive_hides_from_picker_but_keeps_history(self) -> None:
        self.ledger.record_expense(
            TxnCreate(amount_cents=3800, payee="老张牛肉面", category="餐饮"),
            status="confirmed", actor="user",
        )
        rows = {c["name"]: c for c in self.ledger.list_categories_detail("expense")}
        response = self.client.post(
            "/ledger/categories/" + str(rows["餐饮"]["id"]) + "/archive",
            data={"kind": "expense", "archived": "1"}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        page = self.client.get("/ledger/categories?kind=expense").text
        self.assertIn("已归档", page)
        self.assertNotIn("餐饮", self.ledger.list_categories("expense"), "归档后不再出现在下拉里")
        self.assertIn("餐饮", [c["name"] for c in self.ledger.list_categories_detail("expense")],
                      "但管理页还要看得见,才能恢复")
        # 已记的那笔还在(⚠️ 按**当前月份**查:写死 "2026-10" 会在下个月变成空列表)
        current = self.ledger._current_time().strftime("%Y-%m")
        self.assertEqual(self.ledger.list_txns(period=current, limit=10)[0]["category"], "餐饮")

    def test_restore_brings_it_back(self) -> None:
        rows = {c["name"]: c for c in self.ledger.list_categories_detail("expense")}
        category_id = rows["教育"]["id"]
        self.client.post("/ledger/categories/" + str(category_id) + "/archive",
                         data={"kind": "expense", "archived": "1"}, follow_redirects=False)
        self.assertNotIn("教育", self.ledger.list_categories("expense"))
        self.client.post("/ledger/categories/" + str(category_id) + "/archive",
                         data={"kind": "expense", "archived": "0"}, follow_redirects=False)
        self.assertIn("教育", self.ledger.list_categories("expense"))


if __name__ == "__main__":
    unittest.main()
