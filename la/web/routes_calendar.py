"""日历 / 日程 / 提醒 / 待办路由(纯 SSR)。

从 create_app() 搬出来的第三块业务路由(2026-10-18 架构收口)。
四个页签共用同一次展开;写入口全部是普通表单 POST + 303 + 回执。
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..family import members as member_store
from ..tasks.schedules import ScheduleError
from ..tasks.todos import SOURCE_USER, STATUS_COMPLETED, STATUS_PENDING
from . import calendar_subscribe, calendar_view, task_view
from .calendar_view import (
    CAL_TAB_AGENDA,
    CAL_TAB_MONTH,
    CAL_TAB_REMINDERS,
    CAL_TAB_TODOS,
    EVENT_STATUS_LABELS,
    normalize_calendar_tab,
)
from .deps import Services, build_services, get_services
from .idempotency import run_once
from .util import flash, int_or


def _group_occurrences_by_day(occurrences: Any) -> list[dict[str, Any]]:
    grouped: list[dict[str, Any]] = []
    for item in occurrences:
        key = item.start.strftime("%Y-%m-%d")
        if not grouped or grouped[-1]["key"] != key:
            grouped.append({"key": key, "date": item.start, "items": []})
        grouped[-1]["items"].append(item)
    return grouped


def _reminder_todo_text(prompt: str) -> str:
    text = str(prompt or "").strip()
    for prefix in ("该做:", "该做：", "提醒:", "提醒：", "提醒一下:", "提醒一下："):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text or "这件事"


def _today_items(
    services: Services, occurrences: Any, todos: Any, *, now: Any, days: int,
) -> list[dict[str, Any]]:
    """右栏「今天」卡片:日程 + 到今天为止(含逾期)的待办,按时间排。

    ⚠️ 只看**今天这一天占不占位**:跨天日程在中间那天也要出现(与月历同一口径),
       否则"今天还在出差"这件事在右栏会消失。
    """
    tz = services.settings.tz
    today = now.date()
    items: list[dict[str, Any]] = []
    for occurrence in occurrences:
        if not calendar_view._spans(occurrence, today, tz):
            continue
        cell = calendar_view._cell_item(occurrence, tz, today)
        meta_parts = []
        if cell.get("end"):
            meta_parts.append("到 " + str(cell["end"]))
        if cell.get("location"):
            meta_parts.append("@" + str(cell["location"]))
        items.append({
            "kind": "event",
            "when": cell["what"],
            "title": cell["title"],
            "meta": " · ".join(meta_parts),
            "href": "/calendar?tab=agenda&days=" + str(days) + "#d-" + today.isoformat(),
            "overdue": False,
        })
    for todo in todos:
        due = task_view.parse_due(getattr(todo, "due_date_local", None))
        moment = task_view.local_moment(getattr(todo, "due_at_local", None), tz)
        if due is None:
            due = moment.date() if moment is not None else None
        if due is None or due > today:
            continue
        row = task_view.todo_row(todo, today, tz)
        items.append({
            "kind": "todo",
            "when": row["at_text"] or "待办",
            "title": row["content"],
            "meta": row["due_label"] or "待办",
            "href": "/calendar?tab=todos#todo-" + str(row["id"]),
            "overdue": bool(row["overdue"]),
        })
    def _key(item: dict[str, Any]) -> tuple[str, int]:
        when = str(item.get("when") or "")
        if when in ("全天", "待办", "续"):
            return ("00:00", 0)
        return (when, 1)
    items.sort(key=_key)
    return items


def register_calendar_routes(app, *, templates) -> None:
    # ---------------------------------------------------------------- 日历
    #
    # ⭐ 四个页签(见文件头 CAL_TABS)。做法与错题页(D111)同一套:
    #    **服务端只渲染当前那一页**、页签带计数、不引 JS —— 刷新/分享/手机后退都能用。
    #
    # ⚠️ 四页共用**同一次展开**:月历格子与日程明细本来就是同一批实例(见 D92),
    #    各查一遍等于把"格子里有、列表里没有"这个隐患复制四份。
    def _calendar_window(services: Services, days: str, month: str):
        """算出 (现在的时刻, 窗口起点, 窗口终点, 明细天数, 锚定月)。"""
        from datetime import timedelta

        from .calendar_view import month_key, parse_month

        now = services.settings.now()
        lookahead = int_or(days, 14, low=1, high=400)
        # ⚠️ 窗口从**今天 0 点**开始,不是"现在"。从"现在"开始的话,今天 15:00 那件事
        #    在 19:53 打开页面时就消失了(实测踩过,见 D92)。
        window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = window_start + timedelta(days=lookahead)
        wanted = parse_month(month)
        if wanted is not None and wanted < (now.year, now.month):
            wanted = None       # 不往前翻:过去的事在档案里,不在日历上
        anchor = wanted or (now.year, now.month)
        # ⚠️ 窗口至少覆盖**锚定月整月 + 7 天**。月历末尾那几格画的是下月初(补位格),
        #    而格子里的每一条都指向日程页那一天的锚点 —— 窗口算短了就会出现
        #    "格子里明明有、点过去什么都没有"。
        after = (anchor[0] + 1, 1) if anchor[1] == 12 else (anchor[0], anchor[1] + 1)
        month_end = window_start.replace(year=after[0], month=after[1], day=1)
        window_end = max(window_end, month_end + timedelta(days=7))
        return now, window_start, window_end, lookahead, month_key(*anchor)

    def _reminder_rows(services: Services, now) -> list[dict[str, Any]]:
        """定时提醒的行 —— 页签计数与「提醒」页**共用这一批**(D115:别在循环里查库)。

        ⭐ 为什么要抽出来(D117):页签上的数字和页面上画的东西必须出自同一份口径。
           以前角标数的是"开着的提醒",页面上分的是"会不会响" —— 一条今天已经响过的
           一次性提醒能让角标显示 ①,而那一页写着"0 条待响"。两个数字打架,用户只能都不信。
        """
        # 提醒属于哪条待办(D115):一次查好,别在循环里查库
        cache: dict[str, Any] = {}

        def _todo_of(todo_id: str) -> Any:
            if todo_id not in cache:
                cache[todo_id] = services.todos.get(todo_id)
            return cache[todo_id]

        names = {
            person.id: person.name
            for person in member_store.list_members(services.assistant, active_only=False)
        }
        rows = []
        for item in services.schedules.list(enabled_only=False, limit=200):
            row = task_view.schedule_row(
                item, now, services.settings.tz,
                _todo_of(str(item.todo_id)) if item.todo_id else None,
            )
            target = row.get("target_member_id")
            row["target_label"] = (
                "全部" if target is None else names.get(int(target), "已删除成员")
            )
            rows.append(row)
        return rows

    def _reminder_context(
        services: Services, occurrences, *, rows, now, start, tz
    ) -> dict[str, Any]:
        """「提醒」页的数据。两个来源在用户眼里是**同一件事**(见 la/bridge/reminder_push.py):

          ① 定时提醒(memory.db 的 schedules)—— 它答应过要提醒你的事;
          ② 日程自带的提醒(事件提前 N 分钟)—— 排期时顺带定的。

        rows 由 _reminder_rows 查好传进来(页签计数用的是同一批,不查第二遍)。
        """
        from datetime import timedelta, timezone

        rows = list(rows)
        # 「该响没响」排最前(推送环可能没跑),其次待响;已响过/已关闭沉底 ——
        # 用户打开这一页想知道的是"接下来会响什么"。
        order = {"late": 0, "wait": 1, "fired": 2, "off": 3}
        far = now + timedelta(days=3650)
        rows.sort(key=lambda row: (order.get(row["state"], 9), row["due"] or far))
        minutes_by_uid = {
            str(row["uid"]): int(row["reminder_minutes"] or 0)
            for row in services.assistant.execute(
                "SELECT uid, reminder_minutes FROM app_events"
            ).fetchall()
        }
        # ⚠️ 实例表只回答"响没响":物化只覆盖未来 14 天,而这一页的窗口可能到 400 天 ——
        #    所以"这条日程会不会提醒我"由事件本身算(与 materialize 同一算法)。
        instances: dict[tuple[str, str], dict[str, Any]] = {}
        for row in services.assistant.execute(
            "SELECT event_uid, recurrence_id, fired_at, snooze_until_utc"
            " FROM app_reminder_instances WHERE fire_at_utc >= ?"
            " ORDER BY fire_at_utc LIMIT 800",
            (start.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        ).fetchall():
            instances[(str(row["event_uid"]), str(row["recurrence_id"]))] = dict(row)
        # ⭐ D117:第一屏只放**该响的**(待响 + 已错过),已了结的收进折叠。
        #    分组口径在 task_view.split_reminder_rows 里,一行不丢、只换位置。
        split = task_view.split_reminder_rows(rows)
        return {
            "reminders_live": split["live"],
            "reminders_wait": split["wait"],
            "reminders_late": split["late"],
            "reminders_quiet": split["quiet"],
            "reminders_quiet_total": split["quiet_total"],
            "agenda_reminders": task_view.calendar_reminder_rows(
                occurrences, minutes_by_uid, instances, now=now, tz=tz
            ),
            "tzid": str(services.settings.timezone),
            # 「新建提醒」表单的默认时刻:一小时之后(用户多半是想定个近的)
            "default_at": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            "members": [
                {"id": person.id, "name": person.name}
                for person in member_store.list_members(
                    services.assistant, active_only=True
                )
            ],
        }

    def _todo_context(services: Services, *, now) -> dict[str, Any]:
        """「待办」页的数据。未完成的按到期分桶;已完成单独收着(默认折起来)。"""
        open_items = services.todos.list(status="pending", limit=200)
        done_items = sorted(
            services.todos.list(status="completed", limit=50),
            key=lambda item: str(item.updated_at or ""), reverse=True,
        )[:20]
        tz = services.settings.tz
        # 挂在待办上的提醒时刻(历史里"转成待办"的那些走这条)—— 行上要看得见它的闹钟
        linked: dict[str, str] = {}
        for row in services.memory.execute(
            "SELECT todo_id, at FROM schedules WHERE todo_id IS NOT NULL AND at IS NOT NULL"
        ).fetchall():
            linked[str(row["todo_id"])] = str(row["at"])
        return {
            "todo_groups": task_view.group_todos(open_items, now.date(), tz, linked),
            "todos_open": len(open_items),
            "todos_done": [
                task_view.todo_row(item, now.date(), tz, linked.get(str(item.id)))
                for item in done_items
            ],
            "todo_counts": services.todos.counts(),
        }

    @app.get("/calendar", response_class=HTMLResponse)
    def calendar_page(
        request: Request,
        tab: str = "",
        days: str = "",
        month: str = "",
        new: str = "",
        ok: str = "",
        err: str = "",
        services: Services = Depends(get_services),
    ):
        """日历 / 日程 / 提醒 / 待办 —— 四个页签,一份数据。**读取路径里没有模型。**"""
        from datetime import timedelta

        from .calendar_view import build_month_view

        chosen = normalize_calendar_tab(tab, month=month, days=days)
        now, window_start, window_end, lookahead, anchor_key = _calendar_window(
            services, days, month
        )
        occurrences, truncated = services.calendar.expand(
            start=window_start.isoformat(), end=window_end.isoformat()
        )
        # ⭐ 未完成待办也进月历(2026-10-22 用户:"待办事项也应该要显示在日历里")
        open_todos = services.todos.list(status="pending", limit=500)
        # 月历按**锚定月**画(不是"当月"):往后翻时它得跟着翻,往前翻已经被上面挡掉
        month_view = build_month_view(
            occurrences, start=window_start, end=window_end,
            today=now.date(), month=anchor_key, todos=open_todos,
        )
        today_items = _today_items(
            services, occurrences, open_todos, now=now, days=lookahead,
        )
        # 手机端:月历格子只画色条,下面用「本月日程 / 本月待办」把字读全。
        # 桌面端有右栏,不需要这两份列表(模板用 media query 只在小屏显示)。
        from datetime import date
        year, month_number = (int(part) for part in month_view.key.split("-"))
        month_start_date = date(year, month_number, 1)
        next_month = date(year + (1 if month_number == 12 else 0), 1 if month_number == 12 else month_number + 1, 1)
        month_end_date = next_month - timedelta(days=1)
        month_event_rows: list[dict[str, Any]] = []
        for item in occurrences:
            start_day = item.start.date()
            end_day = (item.end or item.start).date()
            if end_day < month_start_date or start_day > month_end_date:
                continue
            month_event_rows.append({
                "key": start_day.isoformat(),
                "date_label": str(start_day.month) + "月" + str(start_day.day) + "日",
                "time": "全天" if item.all_day else item.start.strftime("%H:%M"),
                "title": item.title or "(无标题)",
                "location": item.location,
            })
        month_event_rows.sort(key=lambda row: (row["key"], row["time"]))
        month_todo_rows: list[dict[str, Any]] = []
        for todo in open_todos:
            due = task_view.parse_due(getattr(todo, "due_date_local", None))
            if due is None or due < month_start_date or due > month_end_date:
                continue
            row = task_view.todo_row(todo, now.date(), services.settings.tz)
            month_todo_rows.append({
                **row,
                "date_label": str(due.month) + "月" + str(due.day) + "日",
            })
        month_todo_rows.sort(key=lambda row: (row["due_text"], row["at_text"], row["content"]))
        last_day = (window_end - timedelta(seconds=1)).date()
        # 提醒行**只算一次**(D117):页签上的角标与「提醒」页用的是同一批行
        reminder_rows = _reminder_rows(services, now)
        # iPhone 订阅:链接 + "手机上次来取"的那一笔(2026-10-31 用户报"好几天没更新")
        sub_member_id = getattr(services.member, "id", None)
        subscription = calendar_subscribe.link_info(
            request,
            calendar_subscribe.get_token(services.assistant, sub_member_id),
            # 手机不装 Tailscale 也能订阅:公网只放行 /calendar/ics 这一条(2026-10-31)
            phone_base=str(getattr(services.settings, "calendar_public_base", "") or ""),
        )
        subscription_last = calendar_subscribe.last_hit(
            services.assistant, sub_member_id,
            tzid=str(getattr(services.settings, "timezone", "") or ""),
        )
        if subscription_last is not None:
            # 手机该用的是**公网地址**(有配就比公网,否则退回当前地址)
            current_host = (
                subscription.get("primary_host")
                or subscription["base"].split("://", 1)[-1]
            )
            subscription_last["host_changed"] = bool(
                subscription_last.get("host")
                and subscription_last["host"] != current_host
            )
            subscription_last["now_host"] = current_host
        context: dict[str, Any] = {
            "page": "calendar",
            "tab": chosen,
            "days": lookahead,
            "month": month_view.key,
            "cal": month_view,
            "truncated": truncated,
            "today": now.strftime("%Y-%m-%d"),
            "today_items": today_items[:8],
            "today_more": max(len(today_items) - 8, 0),
            "today_count": len(today_items),
            "today_day": now.day,
            "today_month": str(now.month) + "月",
            "today_weekday": "周" + "一二三四五六日"[now.weekday()],
            "month_event_rows": month_event_rows[:40],
            "month_event_more": max(len(month_event_rows) - 40, 0),
            "month_todo_rows": month_todo_rows[:40],
            "month_todo_more": max(len(month_todo_rows) - 40, 0),
            "subscription": subscription,
            "subscription_last": subscription_last,
            "until_text": str(last_day.month) + "月" + str(last_day.day) + "日",
            "ok": ok,
            "err": err,
            # 页签上的计数每页都看得见 —— 不点进去也知道那边有没有活(D111 同款)
            "counts": {
                # 页签角标 = 这个月所有能看见的条目(日程 + 待办);subtitle 仍分开写
                "month": month_view.month_items + month_view.todo_items,
                "agenda": len(occurrences),
                # 角标 = **该响的**条数(与页面上那一列同一份口径,见 _reminder_rows)
                "reminders": len(task_view.split_reminder_rows(reminder_rows)["live"]),
                "todos": int(services.todos.counts()["open"]),
            },
        }
        # ⭐ D117:「新建」收成一颗按钮之后,出错的 POST 要能把**用户填过的东西**带回来 ——
        #    报错时还要人重填一遍,是比"表单占着第一屏"更烦人的那种不友好。
        #    ?new=1 或带 err 时表单**开着**:否则错误提示指着一个看不见的表单。
        context["form"] = {
            key: str(request.query_params.get(key) or "")[:200]
            for key in ("prompt", "at_local", "time_local", "minutes", "content", "due",
                        "at", "target_member_id")
        }
        context["form_open"] = bool(new) or bool(err)
        if chosen == CAL_TAB_AGENDA:
            context["days_grouped"] = _group_occurrences_by_day(occurrences)
        elif chosen == CAL_TAB_MONTH:
            # ⚠️ 与月历格子同一份 owner 过滤(2026-10-18 审计):裸 SQL
            #    会把别人“各自可见”的日程标题也画进这一页。
            context["events"] = [
                {
                    "id": event.id,
                    "uid": event.uid,
                    "title": event.title,
                    "dtstart": event.dtstart,
                    "rrule": event.rrule,
                    "status": event.status,
                    "status_label": EVENT_STATUS_LABELS.get(
                        str(event.status), str(event.status)
                    ),
                }
                for event in services.calendar.list_events(limit=2000)
            ]
        elif chosen == CAL_TAB_REMINDERS:
            context.update(_reminder_context(
                services, occurrences, rows=reminder_rows, now=now, start=window_start,
                tz=services.settings.tz,
            ))
        else:
            context.update(_todo_context(services, now=now))
        return templates.TemplateResponse(request, "calendar.html", context)

    # -- 「提醒」「待办」两个页签上的写入口 -------------------------------------
    #
    # ⚠️ 一律是**普通表单 POST + 303 重定向**(不是 fetch):禁 JS 也能用,与其它页同一套。
    #    每次写入都带一次性令牌 + 留一条回执 —— 双击、断网重试不会变成两条,
    #    写错了还能在聊天页的回执托盘里撤销(D65)。
    def _keep_values(back: str, **values: str) -> str:
        """出错跳回来时,把用户刚填过的东西一起带上(D117)。

        ⭐ 「新建」收成一颗按钮之后,跳回来时表单是**关着**的 —— 要是连填的内容也没了,
           用户就得从头再打一遍(实测最容易踩的是"这个时刻已经过去了,换个将来的时间")。
           所以这条链接**总是**带 new=1:表单必须开着,否则错误提示指着一个看不见的表单。
        """
        from urllib.parse import urlencode

        kept = {
            key: str(value)[:200]
            for key, value in values.items()
            if str(value or "").strip()
        }
        kept["new"] = "1"
        joiner = "&" if "?" in back else "?"
        return back + joiner + urlencode(kept)

    def _reminder_plan(
        services: Services, *, kind: str, at_local: str, time_local: str, minutes: str
    ) -> dict[str, Any]:
        """表单 -> 建提醒要的三个字段(kind / at / every_seconds)。

        ⚠️ **只认三种用户看得懂的形态**:某个时刻响一次 / 每天同一时间 / 每隔一段。
           这里不解析自然语言("明天早上七点")—— 那是聊天页的活(模型把话说成
           ISO8601 再调 schedule)。表单给确定的三选一,免得"填了看不懂"。
        """
        from datetime import timedelta

        now = services.settings.now()
        tz = services.settings.tz
        if kind == "daily":
            raw = str(time_local or "").strip()
            try:
                hour, minute = (int(part) for part in raw.split(":")[:2])
            except (TypeError, ValueError):
                raise ValueError("每天提醒要选一个时间") from None
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("时间填得不对(应在 00:00 – 23:59)")
            first = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if first <= now:
                first = first + timedelta(days=1)     # 今天这个点已经过了,从明天开始
            return {
                "kind": "every",
                "at": first.isoformat(timespec="seconds"),
                "every_seconds": 86400,
            }
        if kind == "interval":
            try:
                value = int(str(minutes or "").strip())
            except (TypeError, ValueError):
                raise ValueError("间隔要填分钟数(至少 5 分钟)") from None
            if value < 5:
                # 与领域层的硬约束同一口径(MIN_EVERY_SECONDS = 300,防消息轰炸)
                raise ValueError("间隔不得小于 5 分钟")
            return {
                "kind": "every",
                "at": (now + timedelta(minutes=value)).isoformat(timespec="seconds"),
                "every_seconds": value * 60,
            }
        moment = task_view.local_moment(at_local, tz)
        if moment is None:
            raise ValueError("请选择提醒的日期和时间")
        if moment <= now:
            raise ValueError("这个时刻已经过去了,换个将来的时间")
        return {"kind": "at", "at": moment.isoformat(timespec="seconds"), "every_seconds": None}

    @app.post("/calendar/todos/add")
    def calendar_todo_add(
        content: str = Form(""),
        due: str = Form(""),
        at: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """记一条待办。用户自己敲的 —— 直写(source=user,来源永远查得出来)。

        ⭐ 填了时刻就**自带一次到点提醒**(D115:提醒是待办的闹钟,不是另起一件事)。
        """
        back = "/calendar?tab=" + CAL_TAB_TODOS
        text = (content or "").strip()
        due_value = (due or "").strip()
        at_value = (at or "").strip()

        def keep() -> str:
            """出错跳回去的那条链接 —— 把刚填的带回去(D117),不然要人重打一遍。"""
            return _keep_values(back, content=text, due=due_value, at=at_value)

        if not text:
            return flash(keep(), "待办内容不能为空", error=True)
        if at_value and not due_value:
            return flash(keep(), "要定时刻就得先选个日期", error=True)
        if due_value:
            parsed = task_view.parse_due(due_value)
            if parsed is None:
                return flash(keep(), "到期日要填 2026-10-15 这样的日期", error=True)
            due_value = parsed.isoformat()
        due_at = ""
        if at_value:
            # 用户敲的是本地钟点 —— 库里存**带时区**的时刻(与日程/提醒同一口径)
            moment = task_view.local_moment(due_value + "T" + at_value, services.settings.tz)
            if moment is None:
                return flash(keep(), "时刻要填 09:30 这样的", error=True)
            due_at = moment.isoformat(timespec="seconds")
        try:
            run_once(
                services, idem_token, {"todo": text, "due": due_value, "at": due_at},
                lambda: services.todos.add(
                    text, source=SOURCE_USER, due_date_local=due_value or None,
                    due_at_local=due_at or None, actor="user",
                ),
            )
        except ValueError as exc:
            return flash(keep(), str(exc), error=True)
        return flash(back, "记下了:" + text[:40] + ("(到点会提醒你)" if due_at else ""))

    @app.post("/calendar/todos/{todo_id}/done")
    def calendar_todo_done(
        todo_id: str,
        back_to: str = Form("", alias="back"),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        # 「提醒」页签上的"完成"按钮要回到提醒页,不能把人甩到待办页(表单里带着 back)
        back = back_to if str(back_to).startswith("/calendar") else "/calendar?tab=" + CAL_TAB_TODOS
        try:
            result = run_once(
                services, idem_token, {"todo": todo_id, "to": STATUS_COMPLETED},
                lambda: services.todos.set_status(todo_id, STATUS_COMPLETED, actor="user"),
            )
        except ValueError as exc:
            return flash(back, str(exc), error=True)
        if result is None:
            return flash(back, "这一条刚刚已经勾过了。")
        return flash(back, "完成了一条待办")

    @app.post("/calendar/todos/{todo_id}/reopen")
    def calendar_todo_reopen(
        todo_id: str,
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """勾错了能改回来 —— 与"能撤销"是同一条纪律。"""
        back = "/calendar?tab=" + CAL_TAB_TODOS
        try:
            result = run_once(
                services, idem_token, {"todo": todo_id, "to": STATUS_PENDING},
                lambda: services.todos.set_status(todo_id, STATUS_PENDING, actor="user"),
            )
        except ValueError as exc:
            return flash(back, str(exc), error=True)
        if result is None:
            return flash(back, "这一条刚刚已经放回去了。")
        return flash(back, "放回未完成")

    @app.post("/calendar/todos/{todo_id}/delete")
    def calendar_todo_delete(
        todo_id: str,
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        back = "/calendar?tab=" + CAL_TAB_TODOS
        result = run_once(
            services, idem_token, {"todo": todo_id, "op": "delete"},
            lambda: services.todos.delete(todo_id, record_receipt=True, actor="user"),
        )
        if result is None:
            return flash(back, "这一条刚刚已经删过了。")
        if not result:
            return flash(back, "没有这条待办。", error=True)
        return flash(back, "删了;聊天页的回执托盘里能撤销")

    @app.post("/calendar/reminders/add")
    def calendar_reminder_add(
        prompt: str = Form(""),
        when: str = Form("at"),
        at_local: str = Form(""),
        time_local: str = Form(""),
        minutes: str = Form(""),
        target_member_id: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """定一条提醒。写的是 memory.db 的 schedules —— **与聊天里定的是同一张表**。"""
        back = "/calendar?tab=" + CAL_TAB_REMINDERS
        text = (prompt or "").strip()
        target_text = (target_member_id or "").strip()

        def keep() -> str:
            """出错跳回去的那条链接 —— 把刚填的带回去(D117),不然要人重打一遍。"""
            return _keep_values(
                back, prompt=text, at_local=at_local, time_local=time_local, minutes=minutes,
                target_member_id=target_text,
            )

        if not text:
            return flash(keep(), "提醒内容不能为空", error=True)
        target: int | None = None
        if target_text:
            try:
                target = int(target_text)
            except ValueError:
                return flash(keep(), "提醒对象不对", error=True)
            allowed = {
                person.id for person in member_store.list_members(
                    services.assistant, active_only=True
                )
            }
            if target not in allowed:
                return flash(keep(), "提醒对象不存在或已停用", error=True)
        try:
            plan = _reminder_plan(
                services, kind=(when or "at").strip(), at_local=at_local,
                time_local=time_local, minutes=minutes,
            )
        except ValueError as exc:
            return flash(keep(), str(exc), error=True)
        try:
            result = run_once(
                services, idem_token,
                {"reminder": text, "target_member_id": target, **plan},
                lambda: services.schedules.create(
                    text, actor="user", target_member_id=target, **plan
                ),
            )
        except ScheduleError as exc:
            return flash(keep(), str(exc), error=True)
        if result is None:
            return flash(back, "这一条刚刚已经定过了。")
        return flash(back, "定好了:" + text[:40])

    @app.post("/calendar/reminders/{schedule_id}/adopt")
    def calendar_reminder_adopt(
        schedule_id: str,
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """把一条**独立的**一次性提醒补成待办(提醒与待办是一件事,见 D115)。

        ⚠️ 为什么要用户点这一下:历史里那些"其实是工作"的提醒(更新云朵、生成课件)
           **不能自动改** —— 它是不是一件要做的事、做没做,只有他知道。这一步只做两件事:
           把内容搬进待办、把这条提醒挂到它下面;以后勾掉待办,闹钟跟着停。
        """
        back = "/calendar?tab=" + CAL_TAB_REMINDERS
        schedule = services.schedules.get(schedule_id)
        if schedule is None:
            return flash(back, "没有这条提醒。", error=True)
        if schedule.todo_id:
            return flash(back, "这条提醒已经挂在一条待办上了。", error=True)
        now = services.settings.now()
        content = _reminder_todo_text(schedule.prompt)
        # 只有"还没到点的一次性提醒"才知道这件事该在什么时候做;
        # 已经响过的、或重复提醒(every),**不造一个假的到期时间**(那只会变成一条假逾期)。
        due_at = ""
        if schedule.kind == "at":
            moment = task_view.local_moment(schedule.at, services.settings.tz)
            if moment is not None and moment > now:
                due_at = moment.isoformat(timespec="seconds")
        try:
            result = run_once(
                services, idem_token, {"reminder": schedule_id, "op": "adopt"},
                lambda: services.todos.add(
                    content, source=SOURCE_USER,
                    due_date_local=due_at[:10] or None, due_at_local=due_at or None,
                    # -1 = 不要再建一条闹钟:这条提醒**本来就是**它的闹钟,下面把它挂过去
                    remind_minutes_before=-1, actor="user",
                ),
            )
        except ValueError as exc:
            return flash(back, str(exc), error=True)
        if result is None:
            return flash(back, "刚刚已经补过了。")
        if not services.schedules.attach_todo(schedule_id, result.id, receipt=False, actor="user"):
            return flash(back, "待办建好了,但这条提醒没能挂上去 —— 到提醒页重试一次。", error=True)
        return flash(back, "已补成待办:" + content[:30] + "。以后勾掉它,这条提醒就跟着停。")

    @app.post("/calendar/reminders/{schedule_id}/delete")
    def calendar_reminder_delete(
        schedule_id: str,
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        back = "/calendar?tab=" + CAL_TAB_REMINDERS
        result = run_once(
            services, idem_token, {"reminder": schedule_id, "op": "delete"},
            lambda: services.schedules.delete(schedule_id, record_receipt=True, actor="user"),
        )
        if result is None:
            return flash(back, "这一条刚刚已经删过了。")
        if not result:
            return flash(back, "没有这条提醒。", error=True)
        return flash(back, "删了;聊天页的回执托盘里能撤销")

    # -- iPhone 订阅:只读 ICS + 一次性 token -------------------------------
    #
    # ⚠️ 这个 GET 是**公开**的(不受登录闸门保护),安全性完全靠 URL 里的随机 token。
    #    所以:只在显式点「生成订阅链接」时写 token;重置即让旧链接失效;
    #    无效 token 一律 403,不区分"没生成过"和"被重置了"。
    @app.get("/calendar/ics")
    def calendar_ics(request: Request, token: str = "", download: int = 0):
        from ..family import members as member_store
        from ..storage import db

        conn = db.connect_assistant()
        try:
            valid, member_id = calendar_subscribe.resolve_token(conn, token)
            member = (
                member_store.get_member(conn, member_id)
                if member_id is not None
                else None
            )
            # ⭐ 记一笔"手机来取过"(只用于观测):2026-10-24 公网端口关掉之后,
            #    iPhone 每小时都在失败,但页面上**什么都看不出来**,用户只能在
            #    "怎么好几天没更新"里自己猜。有了这一笔,日历页能直接说
            #    "上次手机来取是 X(7 天前),而且用的还是旧地址"。
            if valid:
                calendar_subscribe.record_hit(conn, member_id, request=request)
        finally:
            conn.close()
        if not valid or (
            member_id is not None
            and (member is None or not bool(getattr(member, "active", False)))
        ):
            return Response(
                "订阅链接无效或已被重置。请回到「日历 → 订阅到 iPhone」重新生成。",
                status_code=403, media_type="text/plain; charset=utf-8",
            )
        services = build_services(member=member)
        try:
            body = calendar_subscribe.build_ics(services)
        finally:
            services.close()
        # download=1 给"一次性导入";默认 inline 给 iPhone 的订阅客户端刷新
        disposition = "attachment" if int(download or 0) else "inline"
        return Response(
            content=body,
            media_type="text/calendar; charset=utf-8",
            headers={
                "Content-Disposition": disposition + '; filename="life-assistant.ics"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/calendar/subscribe/rotate")
    def calendar_subscribe_rotate(
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """生成/重置订阅链接。旧链接立即失效 —— 这是唯一能"收回"它的方式。"""
        member_id = getattr(services.member, "id", None)
        try:
            run_once(
                services, idem_token,
                {"op": "calendar_ics_rotate", "member": member_id},
                lambda: calendar_subscribe.rotate_token(services.assistant, member_id),
            )
        except Exception as exc:  # noqa: BLE001 - 表没迁移等,给出可读原因而不是 500
            return flash(
                "/calendar?tab=month",
                "订阅链接没生成:" + type(exc).__name__ + " " + str(exc)[:120],
                error=True,
            )
        return flash(
            "/calendar?tab=month",
            "订阅链接已生成;如果之前有一条,旧链接现在已失效。",
        )

