"""使用报表(D136):把事件日志摊成一张能对照的账,而且**口径不能混**。

用户 2026-10-16 的原话:
  *"在轨迹页多加一个子页面,可以看使用报表,按天、周、月、年、或者自定义时间区段,"
  *查看产生的对话轮次,输入的 token,多少命中,多少未命中,缓存命中率。"
  *"还有工具和 skill 的调用情况,都调用了多少次,成功率是多少"*

⚠️ 这一组测试守三件事,每一件都对着一种真实的会算错的账:

  1. **分桶不许串**:按周必须落在同一个周一,按月不许把两天的账混一格,
     "没有对话的那天"必须仍然占一行(0 与"没有这一格"是两回事);
  2. **口径不许混**:命中率 = 命中 ÷(命中 + 未命中) —— 分母是**输入**那一段,
     不是 total;工具成功率只算 `via=model` 的调用,框架内部调用单独一栏;
  3. **没有账就说没有账**:旧库里一条 llm/usage 都没有时,token 栏是 0 并且**页面明说原因**,
     而轮次/工具那几栏必须仍然是真数 —— 拿 0 冒充"没发生过"是最坏的一种谎。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_usage_report -v
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path

from la import config
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY
from la.web import report_view
from la.web.report_view import (
    PERIOD_CUSTOM,
    PERIOD_DAY,
    PERIOD_MONTH,
    PERIOD_WEEK,
    PERIOD_YEAR,
    build_usage_report,
)

#: 报表页要看的那个"现在" —— 固定住,否则每周一/每月 1 号跑测试会翻车。
NOW = datetime(2026, 10, 16, 21, 0, 0)


class TempDataCase(unittest.TestCase):
    """把数据目录搬到临时目录(与 test_context_trace 同一套做法)。"""

    def setUp(self) -> None:
        import tempfile

        self._orig = {}
        for name in ("DATA_DIR", "ARCHIVE_DIR", "MEMORY_DB", "ASSISTANT_DB"):
            self._orig[name] = getattr(config, name)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "archive").mkdir(parents=True, exist_ok=True)
        config.DATA_DIR = self.root
        config.ARCHIVE_DIR = self.root / "archive"
        config.MEMORY_DB = self.root / "memory.db"
        config.ASSISTANT_DB = self.root / "assistant.db"
        self.conn: sqlite3.Connection = db.connect(self.root / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        for key, value in self._orig.items():
            setattr(config, key, value)
        shutil.rmtree(self.root, ignore_errors=True)

    def next_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events").fetchone()
        return int(row[0])

    def add(self, kind: str, time: str, data: dict, *, session: str = "web") -> None:
        self.conn.execute(
            "INSERT INTO events(session_id, seq, type, time, surface, data)"
            " VALUES (?, ?, ?, ?, 0, ?)",
            (session, self.next_seq(), kind, time, json.dumps(data, ensure_ascii=False)),
        )
        self.conn.commit()

    def add_raw(self, kind: str, time: str, payload: str) -> None:
        self.conn.execute(
            "INSERT INTO events(session_id, seq, type, time, surface, data)"
            " VALUES ('web', ?, ?, ?, 0, ?)",
            (self.next_seq(), kind, time, payload),
        )
        self.conn.commit()

    def usage(self, day: str, prompt: int, hit: int, miss: int, out: int = 0,
              hour: int = 10, step: int = 0) -> None:
        data = {"step": step, "tools_sent": True, "prompt_tokens": prompt,
                "completion_tokens": out, "total_tokens": prompt + out,
                "cache_hit_tokens": hit, "cache_miss_tokens": miss}
        self.add("llm/usage", "%sT%02d:00:00+08:00" % (day, hour), data)

    def report(self, period: str = PERIOD_DAY, **kwargs):
        return build_usage_report(
            self.conn, period=period, now=NOW, now_text="2026-10-16 21:00", **kwargs
        )

    def rows_by_key(self, period: str = PERIOD_DAY):
        return {row.key: row for row in self.report(period).rows}


class BucketCase(TempDataCase):
    """一、分桶:哪一天的账就该落在哪一格。"""

    def test_day_buckets_keep_each_day_separate(self) -> None:
        self.add("user/message", "2026-10-15T09:00:00+08:00", {"text": "a"})
        self.add("user/message", "2026-10-15T09:05:00+08:00", {"text": "b"})
        self.add("user/message", "2026-10-16T09:00:00+08:00", {"text": "c"})
        rows = self.rows_by_key(PERIOD_DAY)
        self.assertEqual(rows["2026-10-15"].asks, 2)
        self.assertEqual(rows["2026-10-16"].asks, 1)
        self.assertEqual(self.report(PERIOD_DAY).totals.asks, 3)

    def test_days_without_any_event_still_get_a_row(self) -> None:
        """⭐ 0 与「没有这一格」是两回事:空白的那天必须仍然列出来。"""
        self.add("user/message", "2026-10-16T09:00:00+08:00", {"text": "a"})
        rows = self.report(PERIOD_DAY).rows
        self.assertEqual([row.key for row in rows], [
            "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14",
            "2026-10-15", "2026-10-16",
        ])
        self.assertEqual(rows[0].asks, 0)

    def test_week_buckets_start_on_monday(self) -> None:
        """2026-10-11 是周日 -> 归到 10-05 那一周;10-12 是周一 -> 新的一周。"""
        self.add("user/message", "2026-10-11T09:00:00+08:00", {"text": "sunday"})
        self.add("user/message", "2026-10-12T09:00:00+08:00", {"text": "monday"})
        rows = self.rows_by_key(PERIOD_WEEK)
        self.assertEqual(rows["2026-10-05"].asks, 1)
        self.assertEqual(rows["2026-10-12"].asks, 1)
        labels = [row.label for row in self.report(PERIOD_WEEK).rows]
        self.assertEqual(labels[-1], "2026-10-12 那周")

    def test_month_and_year_buckets(self) -> None:
        self.add("user/message", "2026-10-15T09:00:00+08:00", {"text": "a"})
        self.add("user/message", "2026-10-16T09:00:00+08:00", {"text": "b"})
        month = self.rows_by_key(PERIOD_MONTH)
        self.assertEqual(list(month), ["2026-10"])
        self.assertEqual(month["2026-10"].asks, 2)
        year = self.rows_by_key(PERIOD_YEAR)
        self.assertEqual(list(year), ["2026"])
        self.assertEqual([row.label for row in self.report(PERIOD_YEAR).rows], ["2026 年"])

    def test_custom_range_is_inclusive_on_both_ends(self) -> None:
        self.add("user/message", "2026-10-11T23:59:00+08:00", {"text": "first day"})
        self.add("user/message", "2026-10-16T00:01:00+08:00", {"text": "last day"})
        self.add("user/message", "2026-10-17T09:00:00+08:00", {"text": "outside"})
        report = self.report(PERIOD_CUSTOM, start="2026-10-11", end="2026-10-16")
        self.assertEqual(report.range_start, "2026-10-11")
        self.assertEqual(report.range_end, "2026-10-16")
        self.assertEqual(report.totals.asks, 2, "首尾两天都要含进去")
        self.assertEqual(len(report.rows), 6)

    def test_custom_range_with_garbage_falls_back_instead_of_failing(self) -> None:
        """⭐ 手打的链接不该把页面打成 422 —— 认不出来就用最近 30 天(与轨迹页同一条)。"""
        report = self.report(PERIOD_CUSTOM, start="昨天", end="")
        self.assertEqual(report.range_end, "2026-10-16")
        self.assertEqual(report.range_start, "2026-09-17")
        self.assertEqual(len(report.rows), 30)

    def test_custom_range_swaps_reversed_dates(self) -> None:
        report = self.report(PERIOD_CUSTOM, start="2026-10-16", end="2026-10-11")
        self.assertEqual(report.range_start, "2026-10-11")
        self.assertEqual(report.range_end, "2026-10-16")

    def test_unknown_period_falls_back_to_day(self) -> None:
        report = build_usage_report(self.conn, period="季度", now=NOW)
        self.assertEqual(report.period, PERIOD_DAY)

    def test_events_outside_the_range_are_not_counted(self) -> None:
        self.add("user/message", "2026-09-01T09:00:00+08:00", {"text": "old"})
        self.assertEqual(self.report(PERIOD_DAY).totals.asks, 0)


class TokenCase(TempDataCase):
    """二、token 与命中率:分母必须是输入那一段,而且逐格要对得上。"""

    def test_tokens_are_summed_per_bucket_and_in_total(self) -> None:
        self.usage("2026-10-16", prompt=1000, hit=900, miss=100, out=50)
        self.usage("2026-10-16", prompt=2000, hit=1800, miss=200, out=60, hour=11, step=1)
        self.usage("2026-10-15", prompt=500, hit=400, miss=100, out=10)
        rows = {row.key: row for row in self.report(PERIOD_DAY).rows}
        self.assertEqual(rows["2026-10-16"].calls, 2)
        self.assertEqual(rows["2026-10-16"].prompt_tokens, 3000)
        self.assertEqual(rows["2026-10-16"].cache_hit_tokens, 2700)
        self.assertEqual(rows["2026-10-16"].cache_miss_tokens, 300)
        self.assertEqual(rows["2026-10-16"].completion_tokens, 110)
        self.assertAlmostEqual(rows["2026-10-16"].cache_rate, 0.9, places=6)
        report = self.report(PERIOD_DAY)
        self.assertEqual(report.totals.calls, 3)
        self.assertEqual(report.totals.prompt_tokens, 3500)
        self.assertAlmostEqual(report.totals.cache_rate, 3100 / 3500, places=6)

    def test_hit_rate_uses_input_tokens_not_the_grand_total(self) -> None:
        """⭐ 命中率的分母是**输入**(命中 + 未命中),不是 total。

        拿 total(还含输出)当分母会把命中率算低 —— 这正是"多少命中"最容易算错的地方。
        """
        self.usage("2026-10-16", prompt=1000, hit=800, miss=200, out=9000)
        row = self.rows_by_key(PERIOD_DAY)["2026-10-16"]
        self.assertAlmostEqual(row.cache_rate, 0.8, places=6)

    def test_bucket_without_usage_probe_has_no_rate(self) -> None:
        """没有 token 账的那一格:率是 0,而不是编出来的值。"""
        self.add("user/message", "2026-10-15T09:00:00+08:00", {"text": "old"})
        row = self.rows_by_key(PERIOD_DAY)["2026-10-15"]
        self.assertEqual(row.calls, 0)
        self.assertEqual(row.cache_rate, 0.0)

    def test_corrupt_usage_payload_is_counted_as_a_call_without_tokens(self) -> None:
        """坏 JSON 当没有 —— 但"调用过一次"这件事是真的,不能连它一起吞掉。"""
        self.add_raw("llm/usage", "2026-10-16T09:00:00+08:00", "不是 json")
        row = self.rows_by_key(PERIOD_DAY)["2026-10-16"]
        self.assertEqual(row.calls, 1)
        self.assertEqual(row.prompt_tokens, 0)


class ToolCase(TempDataCase):
    """三、工具账:成功率只算模型发起的调用,内部调用单独一栏。"""

    def test_tool_success_rate_is_counted_per_tool(self) -> None:
        for ok in (True, True, False):
            self.add("tool/result", "2026-10-16T09:00:00+08:00",
                     {"tool": "expense_query", "ok": ok, "error_code": "", "via": "model"})
        self.add("tool/result", "2026-10-16T09:01:00+08:00",
                 {"tool": "todo_list", "ok": True, "via": "model"})
        tools = {item.name: item for item in self.report().tools}
        self.assertEqual(tools["expense_query"].calls, 3)
        self.assertEqual(tools["expense_query"].ok, 2)
        self.assertEqual(tools["expense_query"].failed, 1)
        self.assertAlmostEqual(tools["expense_query"].rate, 2 / 3, places=6)
        self.assertEqual(tools["todo_list"].rate, 1.0)

    def test_tools_are_sorted_by_calls_then_name(self) -> None:
        for name, count in (("z_tool", 2), ("a_tool", 2), ("big", 5)):
            for _ in range(count):
                self.add("tool/result", "2026-10-16T09:00:00+08:00",
                         {"tool": name, "ok": True, "via": "model"})
        self.assertEqual([item.name for item in self.report().tools], ["big", "a_tool", "z_tool"])

    def test_internal_calls_are_kept_out_of_the_success_rate(self) -> None:
        self.add("tool/result", "2026-10-16T09:00:00+08:00",
                 {"tool": "query_summary_pool", "ok": True, "phase": "qa"})
        self.add("tool/result", "2026-10-16T09:00:01+08:00",
                 {"tool": "web", "ok": False, "via": "model"})
        report = self.report()
        self.assertEqual([item.name for item in report.tools], ["web"])
        self.assertEqual(report.tools[0].calls, 1, "内部调用不许混进工具成功率")
        self.assertEqual(report.internal_tools[0].name, "query_summary_pool")
        self.assertEqual(report.internal_tools[0].calls, 1)

    def test_skill_calls_are_grouped_by_skill_name(self) -> None:
        self.add("tool/result", "2026-10-16T09:00:00+08:00",
                 {"tool": "skill", "ok": True, "via": "model",
                  "skill_action": "load", "skill": "ppt-master"})
        self.add("tool/result", "2026-10-16T09:01:00+08:00",
                 {"tool": "skill", "ok": False, "via": "model",
                  "skill_action": "load", "skill": "ppt-master"})
        self.add("tool/result", "2026-10-16T09:02:00+08:00",
                 {"tool": "skill", "ok": True, "via": "model", "skill_action": "list"})
        self.add("tool/result", "2026-10-16T09:03:00+08:00",
                 {"tool": "web", "ok": True, "via": "model"})
        report = self.report()
        skills = {item.name: item for item in report.skills}
        self.assertEqual(skills["ppt-master"].calls, 2)
        self.assertEqual(skills["ppt-master"].failed, 1)
        self.assertAlmostEqual(skills["ppt-master"].rate, 0.5, places=6)
        self.assertIn("(没给名字)", skills, "list 那一次没有技能名,要如实归到一格")
        self.assertEqual([item.name for item in report.tools], ["skill", "web"],
                         "skill 工具本身也要算进工具账")

    def test_full_catalog_lists_tools_that_were_never_called(self) -> None:
        """⭐ 用户 2026-10-16:*"工具要求全列,我可以通过成功率进行工具的优化"* ——

        只列"这段时间调过的"会把两类工具一起藏起来:**从没调过的**与**调得少的**。
        给了注册表名单就要全列,而且"从没调用"要能一眼看出来(calls=0 -> 页面显示「未调用」)。
        """
        self.add("tool/result", "2026-10-16T09:00:00+08:00",
                 {"tool": "expense_query", "ok": True, "via": "model"})
        catalog = ("expense_query", "never_used_tool", "also_unused")
        report = build_usage_report(
            self.conn, period=PERIOD_DAY, now=NOW, now_text="x", catalog=catalog
        )
        names = [item.name for item in report.tools]
        self.assertEqual(names, ["expense_query", "also_unused", "never_used_tool"])
        self.assertEqual(report.catalog_size, 3)
        self.assertEqual(report.never_called, ["also_unused", "never_used_tool"])
        unused = next(item for item in report.tools if item.name == "never_used_tool")
        self.assertEqual(unused.calls, 0)
        self.assertEqual(unused.rate, 0.0)

    def test_retired_tool_names_are_kept_apart_from_the_catalog(self) -> None:
        """⭐ 改过名的老工具(历史事件里有、现在的注册表里没有)要**单独一栏**。

        ⚠️ 实测过为什么必须拆:工具合并(批次 A/B)之后全表有 53 行,而注册表只有 35 个;
           切到"按失败次数"排时,那些**已经删掉**的旧名字会浮上来,看起来像"当前工具在失败"。
           所以:注册表里的进 `tools`,只在历史里出现过的进 `tools_retired`。
        """
        self.add("tool/result", "2026-10-16T09:00:00+08:00",
                 {"tool": "skill_load", "ok": True, "via": "model"})
        self.add("tool/result", "2026-10-16T09:01:00+08:00",
                 {"tool": "expense_query", "ok": False, "via": "model"})
        report = build_usage_report(
            self.conn, period=PERIOD_DAY, now=NOW, now_text="x",
            catalog=("never_used_tool", "expense_query"),
        )
        self.assertEqual([item.name for item in report.tools], ["expense_query", "never_used_tool"])
        self.assertEqual([item.name for item in report.tools_retired], ["skill_load"])
        self.assertEqual(report.never_called, ["never_used_tool"])

    def test_without_a_catalog_the_report_says_so_instead_of_pretending(self) -> None:
        """读不到注册表时:只列事件里出现过的,并且**明说**没能读到注册表。"""
        self.add("tool/result", "2026-10-16T09:00:00+08:00",
                 {"tool": "web", "ok": True, "via": "model"})
        report = self.report()
        self.assertEqual(report.catalog_size, 0)
        self.assertEqual([item.name for item in report.tools], ["web"])
        said = any("没能读到当前注册表" in note for note in report.notes)
        self.assertTrue(said, "读不到注册表就要如实说,不能假装这就是全部")

    def test_old_skill_tool_names_still_show_up(self) -> None:
        """历史数据里还有 skill_load / skill_list 这两个工具名(合并之前) —— 不许漏掉。"""
        self.add("tool/result", "2026-10-11T09:00:00+08:00",
                 {"tool": "skill_load", "ok": True, "via": "model"})
        tools = self.report(PERIOD_CUSTOM, start="2026-10-11", end="2026-10-16").tools
        rows = {item.name: item for item in tools}
        self.assertEqual(rows["skill_load"].calls, 1)


class CoverageCase(TempDataCase):
    """四、没有账就说没有账 —— 不拿 0 冒充「没发生过」。"""

    def test_empty_database_says_it_has_no_token_account(self) -> None:
        report = self.report()
        self.assertFalse(report.usage_available)
        self.assertEqual(report.totals.prompt_tokens, 0)
        said = any("一条 token 账都没有" in note for note in report.notes)
        self.assertTrue(said, "空库必须明说原因,而不是显示 0 就完事")

    def test_token_account_reports_when_it_started(self) -> None:
        self.usage("2026-10-16", prompt=10, hit=5, miss=5)
        report = self.report()
        self.assertTrue(report.usage_available)
        self.assertEqual(report.usage_first_at, "2026-10-16T10:00:00+08:00")
        self.assertEqual(report.usage_months, ["2026-10"])
        self.assertTrue(any("token 账从" in note for note in report.notes))

    def test_rounds_and_tools_are_still_real_without_any_token_account(self) -> None:
        """⭐ 这一条是这一页最要紧的诚实:旧库里 token 是 0,但轮次与工具调用是真的。"""
        self.add("user/message", "2026-10-16T09:00:00+08:00", {"text": "a"})
        self.add("tool/result", "2026-10-16T09:00:01+08:00",
                 {"tool": "expense_query", "ok": True, "via": "model"})
        report = self.report()
        self.assertEqual(report.totals.asks, 1)
        self.assertEqual(report.tools[0].calls, 1)
        self.assertFalse(report.usage_available)


class MetricCase(TempDataCase):
    """五、总览那几张卡片:数字与口径都要能对上。"""

    def test_metrics_carry_the_numbers_that_were_computed(self) -> None:
        self.add("user/message", "2026-10-16T09:00:00+08:00", {"text": "a"})
        self.usage("2026-10-16", prompt=1000, hit=900, miss=100, out=0)
        metrics = {item.key: item for item in self.report().metric_rows}
        self.assertEqual(metrics["对话轮次"].value, "1")
        self.assertEqual(metrics["输入 token"].value, "1000")
        self.assertEqual(metrics["缓存命中率"].value, "90.0%")
        self.assertIn("命中 900 / 未命中 100", metrics["缓存命中率"].note)

    def test_cache_saving_matches_the_50x_price_ratio(self) -> None:
        """命中单价是未命中的 1/50(D131 实测)—— 省下的比例按这个算。

        1000 输入全命中:花 1000×0.04/1e6,不做缓存要 1000×2.00/1e6
        => 省下 1 - 0.04/2.00 = 98%。
        """
        self.usage("2026-10-16", prompt=1000, hit=1000, miss=0, out=0)
        metrics = {item.key: item for item in self.report().metric_rows}
        self.assertEqual(metrics["缓存省下"].value, "98.0%")
        self.assertAlmostEqual(report_view.CACHE_PRICE_RATIO, 0.02, places=6)

    def test_no_cache_payload_keeps_the_rate_at_zero(self) -> None:
        self.usage("2026-10-16", prompt=0, hit=0, miss=0, out=0)
        metrics = {item.key: item for item in self.report().metric_rows}
        self.assertEqual(metrics["缓存命中率"].value, "0.0%")
        self.assertEqual(metrics["缓存省下"].value, "0.0%")


class PriceCase(TempDataCase):
    """八、钱:高峰 / 空闲两档 + 联网请求费(D148)。

    ⚠️ 这一组守的是"钱按每条事件自己的时刻算" —— 拿总量乘一个单价,
       会把晚上的调用也算成高峰价,一天能差近一倍。
    """

    def test_price_tier_boundaries(self) -> None:
        from datetime import datetime as dt

        peak = ("2026-10-19T08:59", "2026-10-19T09:00", "2026-10-19T11:59",
                "2026-10-19T14:00", "2026-10-19T17:59")
        idle = ("2026-10-19T07:00", "2026-10-19T12:00", "2026-10-19T13:59",
                "2026-10-19T18:00", "2026-10-17T10:00", "2026-10-18T10:00")
        for text in peak[1:3] + peak[3:]:
            with self.subTest(text=text):
                self.assertEqual(
                    report_view.price_tier(dt.fromisoformat(text + ":00+08:00")),
                    report_view.TIER_PEAK,
                )
        for text in idle:
            with self.subTest(text=text):
                self.assertEqual(
                    report_view.price_tier(dt.fromisoformat(text + ":00+08:00")),
                    report_view.TIER_IDLE,
                )

    def test_each_event_is_priced_at_its_own_hour(self) -> None:
        payload = dict(prompt=1_000_000, hit=0, miss=1_000_000, out=0)
        self.usage("2026-10-16", hour=10, **payload)     # 周五上午 = 高峰
        peak_only = self.report().totals.cost_tokens
        self.usage("2026-10-16", hour=20, step=1, **payload)   # 同一晚 = 空闲
        both = self.report().totals.cost_tokens
        idle_part = both - peak_only
        self.assertAlmostEqual(peak_only, 2.00, places=6)
        self.assertAlmostEqual(idle_part, 1.00, places=6)
        self.assertAlmostEqual(both, 3.00, places=6)

    def test_web_requests_are_counted_only_when_billable(self) -> None:
        self.add("web/usage", "2026-10-16T10:00:00+08:00",
                 {"kind": "search", "ok": True, "billable": True, "requests": 1,
                  "results": 3, "provider": "deepseek"})
        self.add("web/usage", "2026-10-16T10:00:01+08:00",
                 {"kind": "search", "ok": True, "billable": False, "requests": 0,
                  "results": 0, "provider": "deepseek"})
        self.add("web/usage", "2026-10-16T10:00:02+08:00",
                 {"kind": "fetch", "ok": True, "billable": True, "requests": 1,
                  "provider": "deepseek", "host": "example.com"})
        report = self.report()
        self.assertEqual(report.totals.search_calls, 1)
        self.assertEqual(report.totals.fetch_calls, 1)
        self.assertAlmostEqual(report.totals.web_cost, 0.01, places=6)
        metrics = {item.key: item for item in report.metric_rows}
        self.assertEqual(metrics["联网花费"].value, "￥0.0100")
        self.assertIn("搜索 1 次 · 抓取 1 次", metrics["联网花费"].note)

    def test_price_note_names_both_tiers_and_the_web_fee(self) -> None:
        note = self.report().price_note
        self.assertIn("高峰", note)
        self.assertIn("空闲", note)
        self.assertIn("0.005", note)
        self.assertIn("节假日", note, "节假日没单独扣这件事要写在脸上")


class TruncationCase(TempDataCase):
    """六、时段太多时砍最旧的,而且**如实说砍了**。"""

    def test_too_many_buckets_keeps_the_newest_and_says_so(self) -> None:
        report = build_usage_report(
            self.conn, period=PERIOD_CUSTOM, start="2024-01-01", end="2026-10-16", now=NOW
        )
        self.assertEqual(len(report.rows), report_view.MAX_ROWS)
        self.assertGreater(report.total_rows, report_view.MAX_ROWS)
        self.assertTrue(report.truncated)
        self.assertEqual(report.rows[-1].key, "2026-10-16")
        self.assertTrue(any("只列最近" in note for note in report.notes))


class EventTypeCase(TempDataCase):
    """七、事件类型进了白名单,而且**不进对话投影**。"""

    def test_llm_usage_is_a_known_event_type_and_never_surfaces(self) -> None:
        from la.memory.archive import EVENT_TYPES, SURFACE_DEFAULT

        self.assertIn("llm/usage", EVENT_TYPES)
        self.assertFalse(SURFACE_DEFAULT["llm/usage"], "token 账不该进对话投影")

    def test_web_usage_is_a_known_event_type_and_never_surfaces(self) -> None:
        """⭐ D148:联网请求账是新的一类事件 —— 漏登记就会被 store.append 拒掉。"""
        from la.memory.archive import EVENT_TYPES, SURFACE_DEFAULT

        self.assertIn("web/usage", EVENT_TYPES)
        self.assertFalse(SURFACE_DEFAULT["web/usage"], "联网请求账不该进对话投影")


class ReportPageCase(TempDataCase):
    """八、页面本身:能开、时段能切、口径写在脸上。

    ⚠️ 2026-10-25:它以前直接 `create_app()` 打**真实数据目录** —— 开发机上没配成员
       与 PIN,所以门是开的、断言都过;云端配了成员,页面回的是登录页,
       于是"页面能开"这几条全红(T0 因此在云端永远绿不了)。
       报表页是纯读页面,把它关进临时数据目录就够了 —— 与其它网页测试同一套做法。
    """

    def client(self):
        import os

        from fastapi.testclient import TestClient

        from la.web.app import create_app

        previous = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = ""
        self.addCleanup(self._restore_key, previous)
        return TestClient(create_app())

    @staticmethod
    def _restore_key(previous) -> None:
        import os

        if previous is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = previous

    def test_page_opens_without_an_api_key(self) -> None:
        body = self.client().get("/report").text
        for needle in ("使用报表", "对话轮次", "输入 token", "缓存命中率", "工具调用", "技能调用"):
            self.assertIn(needle, body)
        for label in ("按天", "按周", "按月", "按年", "自定义"):
            self.assertIn(label, body)

    def test_every_period_renders(self) -> None:
        client = self.client()
        for period in ("day", "week", "month", "year"):
            response = client.get("/report?period=" + period)
            self.assertEqual(response.status_code, 200, period)
        custom = "/report?period=custom&start=2026-10-11&end=2026-10-16"
        self.assertEqual(client.get(custom).status_code, 200)
        self.assertEqual(client.get("/report?period=nope").status_code, 200)

    def test_trace_tabs_carry_the_report_entry(self) -> None:
        body = self.client().get("/trace").text
        self.assertIn("使用报表", body)
        self.assertIn('href="/report"', body)
        report = self.client().get("/report").text
        self.assertIn('href="/trace"', report, "从报表要能回到轨迹")

    def test_page_says_where_the_numbers_come_from(self) -> None:
        body = self.client().get("/report").text
        self.assertIn("llm/usage", body)
        self.assertIn("tool/result", body)
        self.assertIn("只读", body, "纯读这条纪律要写在脸上")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()