"""月历网格:纯函数口径 + 页面真的画出来了。

为什么要单独立一个文件:
  * 月历最容易错的三件事("{月}1 号是星期几"、上/下月补位、"哪些天要补")都在
    纯函数里,可以直接断言,不需要起浏览器;
  * 另外三条**业务口径**必须钉住:跨天事件落在覆盖的每一天、全天事件只落开始那天、
    每天放不下的用 +N **如实报数**(不能假装那天只有这几件);
  * 还有一条页面级的:月历**不依赖 JS**(断网/欠费/关掉模型时排期还得看得见),
    所以断言的是**服务端渲染出来的 HTML**,不是"某个 JS 组件挂载成功"。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_m13_calendar_view -v
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo

from la.domain.calendars.ical import Occurrence
from la.web.calendar_view import (
    _spans_for_tests as spans,
    build_month_view,
    month_days,
    parse_month,
    shift_month,
)

TZ = ZoneInfo("Asia/Shanghai")


def occ(title: str, start: datetime, end: datetime | None = None, *, all_day: bool = False,
        recurring: bool = False, override: bool = False) -> Occurrence:
    return Occurrence(
        uid=title, start=start, end=end, title=title,
        all_day=all_day, is_recurring=recurring, is_override=override,
    )


class MonthMathCase(unittest.TestCase):
    def test_parse_month_accepts_only_yyyy_mm(self) -> None:
        self.assertEqual(parse_month("2026-10"), (2026, 10))
        self.assertEqual(parse_month("2026-1"), None)
        self.assertEqual(parse_month("2026-13"), None)
        self.assertEqual(parse_month("26-10"), None)
        self.assertEqual(parse_month("abcd-ef"), None)
        self.assertEqual(parse_month(""), None)
        self.assertEqual(parse_month(None), None)
        self.assertEqual(parse_month("1990-05"), None, "超出可读范围就当作没给,不猜")

    def test_shift_month_crosses_the_year(self) -> None:
        self.assertEqual(shift_month(2026, 12, 1), (2027, 1))
        self.assertEqual(shift_month(2026, 1, -1), (2025, 12))
        self.assertEqual(shift_month(2026, 10, 3), (2027, 1))

    def test_month_days_handles_leap_february(self) -> None:
        self.assertEqual(len(month_days(2026, 2)), 28)
        self.assertEqual(len(month_days(2024, 2)), 29)
        self.assertEqual(month_days(2026, 10)[0], date(2026, 10, 1))
        self.assertEqual(month_days(2026, 10)[-1], date(2026, 10, 31))


class SpanRuleCase(unittest.TestCase):
    """哪一天要占位 —— 月历上最容易反的三条。"""

    def test_same_day_event_only_occupies_that_day(self) -> None:
        item = occ("复诊", datetime(2026, 10, 12, 12, tzinfo=TZ),
                   datetime(2026, 10, 12, 13, tzinfo=TZ))
        self.assertTrue(spans(item, date(2026, 10, 12), TZ))
        self.assertFalse(spans(item, date(2026, 10, 13), TZ), "13:00 结束不该占 13 号")

    def test_overnight_event_occupies_both_days(self) -> None:
        item = occ("跨夜", datetime(2026, 10, 12, 20, tzinfo=TZ),
                   datetime(2026, 10, 13, 1, tzinfo=TZ))
        self.assertTrue(spans(item, date(2026, 10, 12), TZ))
        self.assertTrue(spans(item, date(2026, 10, 13), TZ), "凌晨 1 点还在,13 号得显示")
        self.assertFalse(spans(item, date(2026, 10, 14), TZ))

    def test_all_day_event_only_occupies_its_first_day(self) -> None:
        item = occ("团建", datetime(2026, 10, 17, tzinfo=TZ),
                   datetime(2026, 10, 18, tzinfo=TZ), all_day=True)
        self.assertTrue(spans(item, date(2026, 10, 17), TZ))
        self.assertFalse(spans(item, date(2026, 10, 18), TZ),
                         "结束时刻 00:00 是'不含',连开三天画三格反而看不清是同一件事")

    def test_multi_day_event_spans_the_middle_days(self) -> None:
        item = occ("出差", datetime(2026, 10, 17, 10, tzinfo=TZ),
                   datetime(2026, 10, 19, 18, tzinfo=TZ))
        for day in (17, 18, 19):
            self.assertTrue(spans(item, date(2026, 10, day), TZ), day)

    def test_event_without_end_does_not_leak_to_the_next_day(self) -> None:
        item = occ("没有结束时间", datetime(2026, 10, 12, 9, tzinfo=TZ))
        self.assertFalse(spans(item, date(2026, 10, 13), TZ))


class GridCase(unittest.TestCase):
    #: 与路由同口径:看 10 月时窗口会**覆盖到 11 月 1 日之后**(否则翻到下个月是空的),
    #: 所以补位格里能看到下月初的事 —— 这里就用这个更宽的窗口。
    def build(self, occurrences, *, today=date(2026, 10, 12), month="2026-10"):
        return build_month_view(
            occurrences,
            start=datetime(2026, 10, 1, 0, 0, tzinfo=TZ),
            end=datetime(2026, 11, 2, 0, 0, tzinfo=TZ),
            today=today, month=month,
        )

    def test_grid_starts_on_monday_and_fills_whole_weeks(self) -> None:
        view = self.build([])
        self.assertEqual(len(view.weekday_labels), 7)
        self.assertEqual(view.weekday_labels[0], "一")
        for row in view.rows:
            self.assertEqual(len(row), 7)
        self.assertEqual(view.rows[0][0].key, "2026-09-28", "2026-10-01 是周四,前面补 3 天")
        self.assertEqual(view.rows[0][3].key, "2026-10-01")
        self.assertEqual(view.rows[-1][-1].key, "2026-11-01", "末尾补齐到整周")
        self.assertEqual(len(view.rows), 5)

    def test_today_is_marked_and_link_keys_are_real_dates(self) -> None:
        view = self.build([])
        today_cells = [cell for row in view.rows for cell in row if cell.is_today]
        self.assertEqual(len(today_cells), 1)
        self.assertEqual(today_cells[0].key, "2026-10-12")
        self.assertEqual(view.today_key, "2026-10-12")
        for row in view.rows:
            for cell in row:
                date.fromisoformat(cell.key)      # 键必须是真日期,否则锚点会指向空气

    def test_month_counts_ignore_the_filler_days_of_neighbours(self) -> None:
        view = self.build([
            # 11-01 是网格最后那个补位格:它在窗口里、画得出来,但不进"本月统计"
            occ("下月初的事", datetime(2026, 11, 1, 10, tzinfo=TZ),
                datetime(2026, 11, 1, 11, tzinfo=TZ)),
            occ("本月的事", datetime(2026, 10, 14, 10, tzinfo=TZ),
                datetime(2026, 10, 14, 11, tzinfo=TZ)),
        ])
        self.assertEqual(view.month_items, 1, "11-01 是补位格,不算本月")
        self.assertEqual(view.month_days, 1)
        self.assertGreater(view.grid_items, view.month_items,
                           "补位那几天的条目要算进网格总数,但不进本月统计")

    def test_overflow_is_reported_as_a_number_not_hidden(self) -> None:
        occurrences = [
            occ("第 " + str(index) + " 件事", datetime(2026, 10, 12, 9 + index, tzinfo=TZ),
                datetime(2026, 10, 12, 9 + index, 30, tzinfo=TZ))
            for index in range(5)
        ]
        view = self.build(occurrences)
        cell = [c for row in view.rows for c in row if c.key == "2026-10-12"][0]
        self.assertEqual(len(cell.items), 3, "每天最多画 3 条")
        self.assertEqual(cell.more, 2)
        self.assertEqual(cell.count, 5, "但条数必须如实报 5")

    def test_entries_are_sorted_all_day_first_then_by_clock(self) -> None:
        view = self.build([
            occ("下午", datetime(2026, 10, 12, 15, tzinfo=TZ),
                datetime(2026, 10, 12, 16, tzinfo=TZ)),
            occ("全天", datetime(2026, 10, 12, tzinfo=TZ),
                datetime(2026, 10, 13, tzinfo=TZ), all_day=True),
            occ("早上", datetime(2026, 10, 12, 8, tzinfo=TZ),
                datetime(2026, 10, 12, 9, tzinfo=TZ)),
        ])
        cell = [c for row in view.rows for c in row if c.key == "2026-10-12"][0]
        self.assertEqual([item["title"] for item in cell.items], ["全天", "早上", "下午"])

    def test_recurring_and_override_markers_survive_into_the_grid(self) -> None:
        view = self.build([
            occ("每周例会", datetime(2026, 10, 12, 10, tzinfo=TZ),
                datetime(2026, 10, 12, 11, tzinfo=TZ), recurring=True),
            occ("这次改过", datetime(2026, 10, 13, 10, tzinfo=TZ),
                datetime(2026, 10, 13, 11, tzinfo=TZ), override=True),
        ])
        cells = {cell.key: cell for row in view.rows for cell in row}
        self.assertEqual(cells["2026-10-12"].items[0]["mark"], "↻")
        self.assertEqual(cells["2026-10-13"].items[0]["mark"], "✎")

    def test_unknown_month_falls_back_to_the_window_start(self) -> None:
        view = self.build([], month="不是月份")
        self.assertEqual(view.key, "2026-10")
        self.assertEqual(view.prev_key, "2026-09")
        self.assertEqual(view.next_key, "2026-11")


class CalendarPageCase(unittest.TestCase):
    """页面层:月历必须**服务端渲染出来**(不依赖 JS)。"""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from la import config
        from la.domain.audit import AuditSink
        from la.domain.services.calendar_service import CalendarService
        from la.storage import db
        from la.web import app as web_app

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ASSISTANT_DB", "MEMORY_DB")
        }
        self._env = {
            key: os.environ.get(key)
            for key in ("DEEPSEEK_API_KEY", "LA_LLM_PROVIDER", "LA_TZ")
        }
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["LA_LLM_PROVIDER"] = "fake"
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config._settings = None

        conn = db.connect_assistant()
        service = CalendarService(conn, audit=AuditSink(conn))
        self.today = config.get_settings().now().date()
        self.month = self.today.strftime("%Y-%m")
        # ⚠️ 日程必须落在**今天之后**:默认窗口是"从现在起 14 天",
        #    放一个已经过去的日期,页面当然看不到它(测试自己造出来的坑)。
        # ⚠️ 两件事都放在**今天**,而且第一件特意放在"今天早些时候" ——
        #    窗口如果从"现在"开始,它就看不见了(实测踩过,见 calendar_page 的注释)。
        soon = self.today
        later = self.soon_day(2)
        self.soon_key = soon.isoformat()
        self.later_key = later.isoformat()
        service.create_event({
            "title": "和医生复诊",
            "start": self.soon_key + "T00:05:00+08:00",
        })
        service.create_event({
            "title": "夜班值守",
            "start": self.soon_key + "T20:00:00+08:00",
            "end": self.soon_key + "T23:00:00+08:00",
        })
        conn.close()
        self.client = TestClient(web_app.create_app())

    def tearDown(self) -> None:
        from la import config

        for key, value in self._patched.items():
            setattr(config, key, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._settings = None
        self._tmp.cleanup()

    def soon_day(self, offset: int):
        from datetime import timedelta

        return self.today + timedelta(days=offset)

    def test_page_renders_a_real_month_grid_without_javascript(self) -> None:
        page = self.client.get("/calendar")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn("cal-grid", html, "月历网格必须由服务端画出来")
        cells = len(re.findall(r'class="cal-day', html))
        self.assertEqual(cells % 7, 0, "网格必须是整周")
        self.assertGreaterEqual(cells, 28)
        self.assertEqual(len(re.findall(r'class="cal-weekdays"', html)), 1)
        self.assertIn('<span class="cc-title">和医生复诊</span>', html)
        self.assertIn("本月 2 条", html)

    def test_grid_cells_link_to_the_agenda_day(self) -> None:
        """D112 起月历与日程是两个页签 —— 格子里的条目要**跨页**指向那一天的锚点。

        ⚠️ 光带 #d-… 不够:锚点得真的存在于「日程」页里,所以窗口必须覆盖到那一天
           (页签那侧由 test_m14_calendar_tabs 钉住)。
        """
        html = self.client.get("/calendar").text
        self.assertIn('id="d-' + self.soon_key + '"', html, "有日程那天的格子要认得出来")
        self.assertIn("/calendar?tab=agenda&amp;month=" + self.month, html)
        self.assertIn("#d-" + self.soon_key, html, "格子里的条目要指向日程页那一天的明细")
        # ⚠️ 别拿"今天 +2 天"当月份内的格子:月底会跨月(2026-10-31 +2 天 = 11-02,
        #    十月的网格里没有它)。要看"整月每一天"就用本月的最后一天。
        import calendar as _calendar

        month_end = self.today.replace(
            day=_calendar.monthrange(self.today.year, self.today.month)[1]
        )
        self.assertIn('id="d-' + month_end.isoformat() + '"', html, "整月每一天都画得出格子")
        agenda = self.client.get("/calendar?tab=agenda&month=" + self.month).text
        self.assertIn('id="d-' + self.soon_key + '"', agenda, "锚点得真的在日程页里")

    def test_next_month_link_keeps_the_page_alive(self) -> None:
        page = self.client.get("/calendar?month=2099-01")
        self.assertEqual(page.status_code, 200)
        follow = self.client.get("/calendar?month=" + self._next_month())
        self.assertEqual(follow.status_code, 200)

    def _next_month(self) -> str:
        year = self.today.year + (1 if self.today.month == 12 else 0)
        month = 1 if self.today.month == 12 else self.today.month + 1
        return str(year).zfill(4) + "-" + str(month).zfill(2)

    def test_navigation_links_point_at_real_months(self) -> None:
        html = self.client.get("/calendar").text
        for key in re.findall(r"/calendar\?month=(\d{4}-\d{2})", html):
            parse_month(key)          # 链接里的月份必须可解析
            self.assertIsNotNone(parse_month(key))

    def test_agenda_window_is_still_configurable(self) -> None:
        page = self.client.get("/calendar?days=400")
        self.assertEqual(page.status_code, 200)
        self.assertIn("接下来 400 天", page.text)



class SubscribeCardWrapCase(unittest.TestCase):
    """订阅卡片里的长链接必须能断行(2026-11-08 用户截图:URL 顶出灰框)。

    为什么用"读 CSS"而不是起浏览器量:这条规则是**纯样式**的回归保险,
    真正的版式由人眼在截图里看(见 docs/笔记域设计.md 的做法);
    这里只钉"那条规则还在",防止以后有人顺手把它删掉。
    """

    def test_long_subscription_url_is_allowed_to_wrap(self) -> None:
        from la import config

        css = (Path(config.BASE_DIR) / "la" / "web" / "static" / "calendar.css").read_text(
            encoding="utf-8"
        )
        at = css.find(".sc-note code")
        self.assertGreater(at, 0, "订阅卡片的 code 断行规则不见了")
        block = css[at:at + 260]
        self.assertIn("white-space: normal", block,
                      "app.css 的 code 是 nowrap,这里必须显式放开")
        self.assertIn("overflow-wrap: anywhere", block,
                      "长 URL 要在必要时断行,否则会顶出卡片")


if __name__ == "__main__":
    unittest.main()