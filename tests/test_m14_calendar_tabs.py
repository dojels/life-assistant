"""日历页的四个页签(D112):月历 / 日程 / 提醒 / 待办。

为什么要单独一个文件:
  * 用户 2026-10-13 的原话是"日历页要优化显示,另外提醒和待办都应该放在这里,
    可以用页标签切换的形式" —— 于是有三条**页面级**的口径必须钉住:
      ① 每个页签只渲染自己那一页(不然"页签"只是把长页面又拼了一遍);
      ② 提醒与待办真的在这一页里看得见、点得动(写入仍是普通表单 POST);
      ③ 老链接(/calendar?days=90、/calendar?month=…)既不报错,也不落到空页。
  * 「到期分桶」「提醒规则文案」「日程提醒按什么算」全是纯计算,
    单独断言比截图可靠得多(日历页最容易错的从来不是样式,是日期与口径)。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_m14_calendar_tabs -v
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from la.domain.calendars.ical import Occurrence
from la.web import task_view

TZ = ZoneInfo("Asia/Shanghai")


def occ(title: str, start: datetime, end: datetime | None = None, *, uid: str = "",
        all_day: bool = False) -> Occurrence:
    return Occurrence(
        uid=uid or title, start=start, end=end, title=title, all_day=all_day,
    )


class FakeTodo:
    """只带 todo_row / group_todos 真正读的那几个字段。"""

    def __init__(self, content: str, *, due: str | None = None, status: str = "pending",
                 source: str = "user", todo_id: str = "t-1") -> None:
        self.id = todo_id
        self.content = content
        self.status = status
        self.source = source
        self.due_date_local = due
        self.created_at = "2026-10-01T00:00:00+00:00"
        self.updated_at = self.created_at


class FakeSchedule:
    def __init__(self, prompt: str, *, kind: str = "every", at: str | None = None,
                 every_seconds: int | None = None, last_fired_at: str | None = None,
                 enabled: bool = True) -> None:
        self.id = "s-1"
        self.prompt = prompt
        self.kind = kind
        self.at = at
        self.every_seconds = every_seconds
        self.last_fired_at = last_fired_at
        self.enabled = enabled
        self.created_at = "2026-10-01T00:00:00+00:00"
        self.updated_at = self.created_at

    # 与 la/tasks/schedules.py 的 Schedule.due_at() 同一份口径
    def due_at(self) -> datetime | None:
        from la.tasks.schedules import Schedule

        return Schedule.due_at(self)  # type: ignore[arg-type]


class TodoBucketCase(unittest.TestCase):
    TODAY = date(2026, 10, 12)

    def test_each_gap_lands_in_its_own_bucket(self) -> None:
        cases = {
            "2026-10-11": "overdue",
            "2026-10-12": "today",
            "2026-10-13": "soon",
            "2026-10-19": "soon",
            "2026-10-20": "later",
            None: "none",
            "": "none",
        }
        for value, expected in cases.items():
            self.assertEqual(task_view.due_bucket(value, self.TODAY), expected, str(value))

    def test_labels_are_human(self) -> None:
        self.assertEqual(task_view.due_label("2026-10-09", self.TODAY), "逾期 3 天")
        self.assertEqual(task_view.due_label("2026-10-12", self.TODAY), "今天")
        self.assertEqual(task_view.due_label("2026-10-13", self.TODAY), "明天")
        self.assertEqual(task_view.due_label("2026-10-14", self.TODAY), "周三")
        self.assertEqual(task_view.due_label("2026-11-02", self.TODAY), "11月2日")
        self.assertEqual(task_view.due_label(None, self.TODAY), "")

    def test_a_datetime_shaped_due_date_still_counts(self) -> None:
        """模型可能把到期日写成 "2026-10-13T09:00" —— 只取日期,不能当成"没设日期"。"""
        self.assertEqual(task_view.due_bucket("2026-10-13T09:00", self.TODAY), "soon")
        self.assertEqual(task_view.due_bucket("2026-10-13 09:00", self.TODAY), "soon")

    def test_garbage_due_date_does_not_crash_the_page(self) -> None:
        self.assertEqual(task_view.due_bucket("下周三", self.TODAY), "none")
        self.assertEqual(task_view.due_label("下周三", self.TODAY), "下周三",
                         "认不出来的就照原文显示,不假装读懂")

    def test_groups_keep_the_fixed_order_and_drop_empty_ones(self) -> None:
        groups = task_view.group_todos(
            [
                FakeTodo("没日期的"),
                FakeTodo("以后再说", due="2026-10-25"),
                FakeTodo("过期的", due="2026-10-10"),
                FakeTodo("今天的", due="2026-10-12"),
            ],
            self.TODAY,
        )
        self.assertEqual([g["key"] for g in groups], ["overdue", "today", "later", "none"])
        self.assertEqual([g["label"] for g in groups], ["已过期", "今天", "以后", "没设日期"])
        self.assertEqual(groups[0]["items"][0]["content"], "过期的")

    def test_agent_created_todos_are_marked_as_such(self) -> None:
        row = task_view.todo_row(FakeTodo("它加的", source="agent"), self.TODAY)
        self.assertTrue(row["is_agent"])
        self.assertEqual(row["source_label"], "它记的")
        self.assertFalse(task_view.todo_row(FakeTodo("我加的"), self.TODAY)["is_agent"])


class ReminderTextCase(unittest.TestCase):
    NOW = datetime(2026, 10, 12, 8, 0, tzinfo=TZ)

    def test_rule_text_says_when_it_rings(self) -> None:
        daily = FakeSchedule("吃药", at="2026-10-12T09:00:00+08:00", every_seconds=86400)
        self.assertEqual(task_view.schedule_rule(daily, TZ), "每天 09:00")
        hourly = FakeSchedule("喝水", every_seconds=3600)
        self.assertEqual(task_view.schedule_rule(hourly, TZ), "每 1 小时")
        every30 = FakeSchedule("起来走走", every_seconds=1800)
        self.assertEqual(task_view.schedule_rule(every30, TZ), "每 30 分钟")
        once = FakeSchedule("看牙医", kind="at", at="2026-10-15T15:00:00+08:00")
        self.assertEqual(task_view.schedule_rule(once, TZ, self.NOW), "一次性 10月15日 15:00",
                         "同一年就不写年份 —— 每行都拖个 2026年 会把列表撑成两行")
        next_year = FakeSchedule("明年的事", kind="at", at="2027-01-05T15:00:00+08:00")
        self.assertEqual(task_view.schedule_rule(next_year, TZ, self.NOW),
                         "一次性 2027年1月5日 15:00", "跨年必须写年份,否则看不出是哪年")
        after = FakeSchedule("十分钟后", kind="after", every_seconds=600)
        self.assertEqual(task_view.schedule_rule(after, TZ), "建好后 10 分钟 响一次")

    def test_naked_local_time_is_read_as_local_not_crashing(self) -> None:
        """模型手写的 at 可能没有时区 —— 不能因为比大小就抛 TypeError(整页 500)。"""
        naked = FakeSchedule("吃药", at="2026-10-12T09:00:00", every_seconds=86400)
        row = task_view.schedule_row(naked, self.NOW, TZ)
        self.assertEqual(row["state"], "wait")
        self.assertIn("下次", row["when_text"])

    def test_states_distinguish_wait_late_and_fired(self) -> None:
        waiting = FakeSchedule("待响", kind="at", at="2026-10-12T09:00:00+08:00")
        self.assertEqual(task_view.schedule_row(waiting, self.NOW, TZ)["state"], "wait")
        late = FakeSchedule("该响没响", kind="at", at="2026-10-12T07:00:00+08:00")
        row = task_view.schedule_row(late, self.NOW, TZ)
        self.assertEqual(row["state"], "late")
        self.assertEqual(row["when_text"], "", "时刻在 rule 里已经写着了,别再说第二遍")
        self.assertIn("10月12日 07:00", row["rule"])
        fired = FakeSchedule("响过了", kind="at", at="2026-10-12T07:00:00+08:00",
                             last_fired_at="2026-10-12T07:00:05+00:00")
        self.assertEqual(task_view.schedule_row(fired, self.NOW, TZ)["state"], "fired")
        off = FakeSchedule("关着的", kind="at", at="2026-10-12T09:00:00+08:00", enabled=False)
        self.assertEqual(task_view.schedule_row(off, self.NOW, TZ)["state"], "off")

    def test_a_daily_reminder_that_already_rang_is_still_live(self) -> None:
        """⚠️ 实测踩到:"每天 9 点"响过一次就被折进"已了结" —— 等于把明天还会响的藏起来。"""
        daily = FakeSchedule("吃药", kind="every", every_seconds=86400,
                             at="2026-10-12T09:00:00+08:00",
                             last_fired_at="2026-10-12T01:00:05+00:00")
        row = task_view.schedule_row(daily, self.NOW, TZ)
        self.assertEqual(row["state"], "wait", "重复提醒响过之后还是待响")
        self.assertIn("下次", row["when_text"])
        self.assertIn("上次", row["when_text"], "响过的事实要留着 —— 它是推送环活着的证据")
        self.assertEqual(len(task_view.split_reminder_rows([row])["live"]), 1)

    def test_a_row_never_says_the_same_moment_twice(self) -> None:
        """⚠️ 实测踩到「一次性 10月15日 20:15 · 该响于 10月15日 20:15」:一行里同一句话说两遍。

        一次性提醒的时刻**规则那一栏就是它** —— 补充那栏只留规则里没有的信息。
        """
        for at, state in (("2026-10-12T09:00:00+08:00", "wait"),
                          ("2026-10-12T07:00:00+08:00", "late")):
            row = task_view.schedule_row(FakeSchedule("事", kind="at", at=at), self.NOW, TZ)
            self.assertEqual(row["state"], state)
            self.assertNotIn("10月12日", row["when_text"], row["when_text"])
            self.assertIn("10月12日", row["rule"])

    def test_human_seconds_never_rounds_silently(self) -> None:
        self.assertEqual(task_view.human_seconds(1800), "30 分钟")
        self.assertEqual(task_view.human_seconds(86400), "1 天")
        self.assertEqual(task_view.human_seconds(90), "90 秒")
        self.assertEqual(task_view.human_seconds(None), "—")


class AgendaReminderCase(unittest.TestCase):
    NOW = datetime(2026, 10, 12, 8, 0, tzinfo=TZ)

    def test_fire_time_is_start_minus_lead_and_sorted(self) -> None:
        rows = task_view.calendar_reminder_rows(
            [
                occ("晚一点的", datetime(2026, 10, 12, 16, 0, tzinfo=TZ)),
                occ("早一点的", datetime(2026, 10, 12, 10, 0, tzinfo=TZ)),
            ],
            {"晚一点的": 30, "早一点的": 60},
            {},
            now=self.NOW,
            tz=TZ,
        )
        self.assertEqual([r["title"] for r in rows["upcoming"]], ["早一点的", "晚一点的"])
        self.assertEqual(rows["upcoming"][0]["fire_text"], "10月12日 09:00")
        self.assertEqual(rows["upcoming"][0]["lead_text"], "提前 1 小时")
        self.assertEqual(rows["upcoming"][0]["state"], "wait")

    def test_events_without_a_reminder_are_left_out(self) -> None:
        rows = task_view.calendar_reminder_rows(
            [occ("不提醒的", datetime(2026, 10, 12, 16, 0, tzinfo=TZ))],
            {"不提醒的": 0},
            {},
            now=self.NOW,
            tz=TZ,
        )
        self.assertEqual(rows["total"], 0)

    def test_instances_tell_whether_it_really_rang(self) -> None:
        item = occ("开会", datetime(2026, 10, 12, 10, 0, tzinfo=TZ))
        instances = {
            (item.uid, item.instance_id): {"fired_at": "2026-10-12T01:30:05+00:00"},
        }
        rows = task_view.calendar_reminder_rows(
            [item], {"开会": 30}, instances, now=self.NOW, tz=TZ
        )
        self.assertEqual(rows["upcoming"], [])
        self.assertEqual(rows["past"][0]["state"], "fired")
        self.assertIn("已响过", rows["past"][0]["state_label"])

    def test_a_due_but_unfired_one_is_reported_as_late_not_hidden(self) -> None:
        item = occ("该响的", datetime(2026, 10, 12, 8, 10, tzinfo=TZ))
        rows = task_view.calendar_reminder_rows(
            [item], {"该响的": 30}, {}, now=self.NOW, tz=TZ
        )
        self.assertEqual(rows["past"][0]["state"], "late")
        self.assertEqual(rows["past"][0]["state_label"], "该响没响")

    def test_overflow_is_reported_as_a_number(self) -> None:
        items = [
            occ("第 " + str(index) + " 件", datetime(2026, 10, 12, 9, tzinfo=TZ) + timedelta(days=index))
            for index in range(15)
        ]
        rows = task_view.calendar_reminder_rows(
            items, {item.uid: 30 for item in items}, {}, now=self.NOW, tz=TZ, limit=12
        )
        self.assertEqual(len(rows["upcoming"]), 12)
        self.assertEqual(rows["upcoming_more"], 3)
        self.assertEqual(rows["upcoming_total"], 15)


class ReminderSplitCase(unittest.TestCase):
    """D117:提醒分成"该响的"与"已经了结的" —— **只分组,不丢行**。

    用户 2026-10-14 的原话是"像已完成的你应该收缩起来"。收起来的代价必须是零:
    折叠里画不下时如实报总数,坏行也不能把整页带崩。
    """

    def test_wait_and_late_stay_live_and_the_rest_fold(self) -> None:
        rows = [
            {"id": "a", "state": "late"}, {"id": "b", "state": "wait"},
            {"id": "c", "state": "fired"}, {"id": "d", "state": "off"},
        ]
        split = task_view.split_reminder_rows(rows)
        self.assertEqual([row["id"] for row in split["live"]], ["a", "b"])
        self.assertEqual([row["id"] for row in split["quiet"]], ["c", "d"])
        self.assertEqual((split["wait"], split["late"], split["quiet_total"]), (1, 1, 2))

    def test_a_long_quiet_list_reports_its_real_total(self) -> None:
        rows = [{"id": str(n), "state": "off"} for n in range(40)]
        split = task_view.split_reminder_rows(rows, quiet_max=5)
        self.assertEqual(len(split["quiet"]), 5)
        self.assertEqual(split["quiet_total"], 40, "折叠里画不下,也要如实报总数")

    def test_a_broken_row_does_not_crash_the_page(self) -> None:
        split = task_view.split_reminder_rows([{}, {"state": None}, {"state": "wait"}])
        self.assertEqual(len(split["live"]), 1)
        self.assertEqual(split["quiet_total"], 2)


class CalendarTabsPageCase(unittest.TestCase):
    """页面层:四个页签各自渲染自己的东西,写入口真的能写。"""

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

        settings = config.get_settings()
        self.now = settings.now()
        self.today = self.now.date()
        self.month = self.today.strftime("%Y-%m")
        self.soon_key = (self.today + timedelta(days=2)).isoformat()

        conn = db.connect_assistant()
        calendar = CalendarService(conn, audit=AuditSink(conn))
        calendar.create_event({
            "title": "和医生复诊",
            "start": self.soon_key + "T15:00:00+08:00",
        })
        conn.close()

        memory = db.connect_memory()
        from la.tasks.schedules import ScheduleStore
        from la.tasks.todos import TodoStore

        TodoStore(memory).add("交物业费", due_date_local=(self.today + timedelta(days=3)).isoformat())
        TodoStore(memory).add("过期的杂事", due_date_local=(self.today - timedelta(days=2)).isoformat())
        first_nine = self.now.replace(hour=9, minute=0, second=0, microsecond=0)
        if first_nine <= self.now:
            first_nine = first_nine + timedelta(days=1)
        ScheduleStore(memory).create(
            "吃降压药", kind="every", every_seconds=86400,
            at=first_nine.isoformat(timespec="seconds"), receipt=False,
        )
        memory.close()

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

    # -- 页签 ---------------------------------------------------------------
    def test_default_tab_is_the_month_grid(self) -> None:
        page = self.client.get("/calendar")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn("cal-grid", html)
        self.assertIn("本月 1 条", html)
        self.assertNotIn("接下来", html, "月历页不该再扛着那串日程明细")
        self.assertNotIn("定一个新提醒", html)
        self.assertNotIn("记一条待办", html)

    def test_every_tab_shows_the_tab_strip_with_counts(self) -> None:
        for tab in ("month", "agenda", "reminders", "todos"):
            html = self.client.get("/calendar?tab=" + tab).text
            for other in ("month", "agenda", "reminders", "todos"):
                self.assertIn("/calendar?tab=" + other, html, tab + " 少了 " + other + " 的页签")
            self.assertIn('class="segmented tabs"', html)

    def test_agenda_tab_carries_the_day_detail(self) -> None:
        html = self.client.get("/calendar?tab=agenda&days=400").text
        self.assertIn("接下来 400 天", html)
        self.assertIn("和医生复诊", html)
        self.assertIn('id="d-' + self.soon_key + '"', html)
        self.assertNotIn("cal-grid", html, "日程页不该再把月历画一遍")

    def test_month_grid_links_every_chip_to_the_agenda_tab(self) -> None:
        # ⚠️ 事件排在"今天 +2 天",**月底会跨到下个月** —— 所以要看事件所在的月份,
        #    而不是"当前月份"(2026-10-31 实测:10-31 +2 天 = 11-02,十月网格里没有它)。
        month_key = self.soon_key[:7]
        html = self.client.get("/calendar?month=" + month_key).text
        self.assertIn('id="d-' + self.soon_key + '"', html)
        # 格子里的条目必须能点去「日程」页里**那一天**的锚点
        self.assertIn("/calendar?tab=agenda&amp;month=" + month_key, html)
        self.assertIn("#d-" + self.soon_key, html)

    def test_reminders_tab_shows_both_sources(self) -> None:
        html = self.client.get("/calendar?tab=reminders").text
        self.assertIn("定一个新提醒", html)
        self.assertIn("吃降压药", html)
        self.assertIn("每天 09:00", html)
        # 日程自带的提醒:事件提前 30 分钟(默认值),由事件本身算出来
        self.assertIn("日程提醒", html)
        self.assertIn("提前 30 分钟", html)
        self.assertNotIn("cal-grid", html)

    def test_todos_tab_groups_by_due_date(self) -> None:
        html = self.client.get("/calendar?tab=todos").text
        self.assertIn("交物业费", html)
        self.assertIn("过期的杂事", html)
        self.assertIn("已过期", html)
        self.assertIn("接下来 7 天", html)
        self.assertIn("记一条待办", html)
        self.assertNotIn("cal-grid", html)

    # -- 老链接 -------------------------------------------------------------
    def test_old_links_land_somewhere_useful(self) -> None:
        legacy = self.client.get("/calendar?days=90").text
        self.assertIn("接下来 90 天", legacy, "带 days 的老书签要落到「日程」页")
        # ⚠️ 别拿"下个月"当空月份:事件是"今天 +2 天",月末正好会落进去。
        month = self.client.get("/calendar?month=2027-06").text
        self.assertIn("cal-grid", month)
        self.assertIn("本月 0 条", month)
        junk = self.client.get("/calendar?tab=乱写")
        self.assertEqual(junk.status_code, 200)
        self.assertIn("cal-grid", junk.text)

    def test_a_hand_typed_days_value_does_not_500(self) -> None:
        for value in ("abc", "-5", "99999", ""):
            page = self.client.get("/calendar?tab=agenda&days=" + value)
            self.assertEqual(page.status_code, 200, value)

    # -- 写入口 -------------------------------------------------------------
    def test_adding_a_todo_through_the_form(self) -> None:
        response = self.client.post(
            "/calendar/todos/add",
            data={"content": "给车做保养", "due": (self.today + timedelta(days=1)).isoformat(),
                  "_idem": "tok-todo-1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("tab=todos", response.headers["location"])
        follow = self.client.get(response.headers["location"]).text
        self.assertIn("给车做保养", follow)
        self.assertIn("明天", follow)

    def test_an_empty_todo_is_refused_with_a_readable_reason(self) -> None:
        response = self.client.post(
            "/calendar/todos/add", data={"content": "  ", "_idem": "tok-todo-2"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers["location"])

    def _todo_id(self, content: str) -> str:
        from la import config
        from la.storage import db

        conn = db.connect_memory()
        row = conn.execute("SELECT id FROM todos WHERE content = ?", (content,)).fetchone()
        conn.close()
        assert row is not None
        return str(row["id"])

    def test_completing_and_reopening_a_todo(self) -> None:
        todo_id = self._todo_id("交物业费")
        done = self.client.post(f"/calendar/todos/{todo_id}/done", data={"_idem": "tok-done-1"},
                                follow_redirects=False)
        self.assertEqual(done.status_code, 303)
        html = self.client.get("/calendar?tab=todos").text
        self.assertIn("已完成 1 条", html)
        self.assertIn("放回", html)

        # 同一个令牌再点一次 = 重复提交,不该变成两次操作,也不该报错
        again = self.client.post(f"/calendar/todos/{todo_id}/done", data={"_idem": "tok-done-1"},
                                 follow_redirects=False)
        self.assertEqual(again.status_code, 303)
        self.assertIn("ok=", again.headers["location"])

        reopened = self.client.post(f"/calendar/todos/{todo_id}/reopen", data={"_idem": "tok-open-1"},
                                    follow_redirects=False)
        self.assertEqual(reopened.status_code, 303)
        self.assertIn("交物业费", self.client.get("/calendar?tab=todos").text)

    def test_deleting_a_todo_records_a_undoable_receipt(self) -> None:
        todo_id = self._todo_id("过期的杂事")
        response = self.client.post(f"/calendar/todos/{todo_id}/delete", data={"_idem": "tok-del-1"},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("过期的杂事", self.client.get("/calendar?tab=todos").text)

        from la.storage import db

        conn = db.connect_memory()
        row = conn.execute(
            "SELECT label, actor, domain FROM ops_receipts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["actor"], "user", "界面上删的就该记成用户删的")
        self.assertEqual(row["domain"], "task")

    def test_adding_a_daily_reminder(self) -> None:
        response = self.client.post(
            "/calendar/reminders/add",
            data={"prompt": "接孩子", "when": "daily", "time_local": "17:30", "_idem": "tok-rm-1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("tab=reminders", response.headers["location"])
        html = self.client.get("/calendar?tab=reminders").text
        self.assertIn("接孩子", html)
        self.assertIn("每天 17:30", html)

    def test_reminder_target_can_be_selected(self) -> None:
        page = self.client.get("/calendar?tab=reminders")
        self.assertIn('name="target_member_id"', page.text)
        response = self.client.post(
            "/calendar/reminders/add",
            data={"prompt": "只提醒管理员", "when": "daily", "time_local": "18:00",
                  "target_member_id": "1", "_idem": "tok-rm-target-1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        from la.storage import db

        conn = db.connect_memory()
        row = conn.execute(
            "SELECT target_member_id FROM schedules WHERE prompt = ?", ("只提醒管理员",)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(int(row["target_member_id"]), 1)
        self.assertIn("只提醒 管理员", self.client.get("/calendar?tab=reminders").text)

    def test_a_reminder_in_the_past_is_refused(self) -> None:
        before = self.client.get("/calendar?tab=reminders").text.count("/delete")
        response = self.client.post(
            "/calendar/reminders/add",
            data={"prompt": "补一个过去", "when": "at", "at_local": "2020-01-01T09:00",
                  "_idem": "tok-rm-2"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers["location"])
        after = self.client.get("/calendar?tab=reminders").text.count("/delete")
        self.assertEqual(before, after, "被拒的提醒不该留下半条记录")

    def test_a_too_short_interval_is_refused_at_the_door(self) -> None:
        response = self.client.post(
            "/calendar/reminders/add",
            data={"prompt": "每分钟烦一次", "when": "interval", "minutes": "1",
                  "_idem": "tok-rm-3"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers["location"])

    def test_deleting_a_reminder(self) -> None:
        from la.storage import db

        conn = db.connect_memory()
        row = conn.execute("SELECT id FROM schedules WHERE prompt = ?", ("吃降压药",)).fetchone()
        conn.close()
        assert row is not None
        response = self.client.post(
            f"/calendar/reminders/{row['id']}/delete", data={"_idem": "tok-rm-del-1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("还没有该响的提醒", self.client.get("/calendar?tab=reminders").text)
        conn = db.connect_memory()
        left = conn.execute("SELECT COUNT(*) AS n FROM schedules").fetchone()
        conn.close()
        self.assertEqual(int(left["n"]), 0, "删除要真的删掉那一行,不能只是不画")

    # -- D117:内容优先(用户 2026-10-14) ------------------------------------
    def _add_disabled_reminder(self, prompt: str) -> None:
        """造一条已经了结的提醒(关着的)—— 用来验"它有没有从第一屏消失"。"""
        from la.storage import db
        from la.tasks.schedules import ScheduleStore

        conn = db.connect_memory()
        store = ScheduleStore(conn)
        item = store.create(
            prompt, kind="every", every_seconds=86400,
            at=(self.now + timedelta(hours=2)).isoformat(timespec="seconds"), receipt=False,
        )
        store.set_enabled(item.id, False, receipt=False)
        conn.close()

    def test_the_new_form_is_a_collapsed_button_not_a_wall_of_fields(self) -> None:
        """⭐ 用户原话:"新建你应该用一个按钮,而不是展开"。"""
        for tab in ("reminders", "todos"):
            html = self.client.get("/calendar?tab=" + tab).text
            self.assertIn('<details class="new-fold">', html, tab + " 的新建入口该默认收着")
            self.assertIn("＋", html, tab + " 该有一颗看得出来是按钮的入口")
            opened = self.client.get("/calendar?tab=" + tab + "&new=1").text
            self.assertIn('<details class="new-fold" open>', opened, tab + " ?new=1 要能展开")

    @staticmethod
    def _new_fold_span(html: str) -> tuple[int, int, str]:
        """第一个「新建」折叠的范围:(开标签起点, 闭合点, 开标签原文)。"""
        start = html.index('<details class="new-fold"')
        tag_end = html.index(">", start)
        return start, html.index("</details>", tag_end), html[start:tag_end + 1]

    def test_the_form_hides_behind_the_closed_button(self) -> None:
        """⭐ "用户点到界面应该是看到想看的东西"。

        ⚠️ 别用"谁在 DOM 里靠前"来断言这件事:按钮本来就排在最前面(那才顺手)。
           真正决定第一屏的是什么,是**输入框在不在那个闭合的 <details> 里** ——
           闭合的 details 在 HTML 层面就是不占位置。这条比截图可靠:截图会过期,语义不会。
        """
        for tab, field, head in (
            ("reminders", 'id="rm-prompt"', "该响的提醒"),
            ("todos", 'id="td-content"', "未完成"),
        ):
            html = self.client.get("/calendar?tab=" + tab).text
            start, close, tag = self._new_fold_span(html)
            self.assertNotIn(" open", tag, tab + ":新建入口默认必须是收着的")
            self.assertLess(start, html.index(field), tab + ":输入框该在这个按钮里面")
            self.assertLess(html.index(field), close, tab + ":输入框该在这个按钮里面")
            self.assertLess(close, html.index(head), tab + ":清单在它后面,点开之前不占地方")
            self.assertNotIn(head, html[start:close], tab + ":清单不该被收进新建按钮里")
        self.assertIn("吃降压药", self.client.get("/calendar?tab=reminders").text)
        self.assertIn("交物业费", self.client.get("/calendar?tab=todos").text)

    def test_finished_reminders_are_folded_and_still_there(self) -> None:
        """⭐ "像已完成的你应该收缩起来" —— 收起来,但不能丢。"""
        self._add_disabled_reminder("早就关掉的提醒")
        html = self.client.get("/calendar?tab=reminders").text
        parts = html.split("已响过 / 已关闭的", 1)
        self.assertEqual(len(parts), 2, "了结的提醒该有一个折叠入口")
        live, quiet = parts
        self.assertNotIn("早就关掉的提醒", live, "了结的提醒不该还占着第一屏")
        self.assertIn("早就关掉的提醒", quiet, "折叠里必须还找得到它")
        self.assertIn("1 条", quiet.split("</summary>")[0])

    def test_a_refused_todo_write_keeps_what_you_typed(self) -> None:
        """出错的 POST 要带着填过的内容跳回来,**并且把表单开着** —— 重打一遍最烦人。"""
        response = self.client.post(
            "/calendar/todos/add",
            data={"content": "给车做保养", "at": "09:30", "_idem": "tok-keep-1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        location = response.headers["location"]
        self.assertIn("err=", location)
        self.assertIn("new=1", location, "错误跳回去时表单必须是开着的")
        page = self.client.get(location).text
        self.assertIn('<details class="new-fold" open>', page)
        self.assertIn('value="给车做保养"', page)
        self.assertIn('value="09:30"', page)
        self.assertIn("要定时刻就得先选个日期", page)

    def test_a_refused_reminder_keeps_the_time_you_picked(self) -> None:
        response = self.client.post(
            "/calendar/reminders/add",
            data={"prompt": "补一个过去", "when": "at", "at_local": "2020-01-01T09:00",
                  "_idem": "tok-keep-2"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        page = self.client.get(response.headers["location"]).text
        self.assertIn('value="补一个过去"', page)
        self.assertIn('value="2020-01-01T09:00"', page, "选过的时刻不该让人重选")

    def test_new_on_another_tab_is_harmless(self) -> None:
        for tab in ("month", "agenda"):
            page = self.client.get("/calendar?tab=" + tab + "&new=1")
            self.assertEqual(page.status_code, 200, tab)

    def test_every_page_still_renders_without_javascript(self) -> None:
        """四页都必须是**服务端画出来的** —— 页签只是链接,不是前端路由。"""
        for tab in ("month", "agenda", "reminders", "todos"):
            html = self.client.get("/calendar?tab=" + tab).text
            self.assertIn('class="shell"', html)
            self.assertNotIn("onclick=", html)
            self.assertEqual(len(re.findall(r"<script", html)), 1, "只该有 app.js 那一个脚本标签")


if __name__ == "__main__":
    unittest.main()
