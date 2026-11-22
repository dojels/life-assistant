"""事项页签的显示口径:待办的到期分桶、提醒的规则文案、「日程提醒」那一列。

为什么单独一个模块(与 calendar_view.py 同一个理由):
  * 「逾期几天」「每天 9 点」这类文案是**纯计算**,不该被数据库与 Jinja 绑住 ——
    放在这里可以直接断言,不需要起服务;
  * 到期分桶(已过期 / 今天 / 接下来七天 / 以后 / 没设日期)是这一页最容易算错的
    地方,而它算错的代价是**用户记错日子**,比月历画错一格还严重。

⚠️ 时区:库里存的时刻可能带时区,也可能是**裸字符串**(模型手写的 at 就是这样)。
   一律按本地时区解释裸时刻 —— 与 la/bridge/reminder_push.py 的 _as_local 同一口径;
   不这么做的话,拿它和 now 比大小会直接抛 TypeError,整页打不开。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Sequence

#: 星期几的中文(不依赖系统 locale —— Windows 上 %a 会出中文,格式没法统一)
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

TODO_STATUS_LABELS = {"pending": "待办", "in_progress": "在做", "completed": "已完成"}
TODO_SOURCE_LABELS = {"user": "你记的", "agent": "它记的", "dispatcher": "自动排的"}

#: 到期分桶的**固定顺序**:过期的最要紧,没日期的最后。
TODO_BUCKETS: tuple[tuple[str, str], ...] = (
    ("overdue", "已过期"),
    ("today", "今天"),
    ("soon", "接下来 7 天"),
    ("later", "以后"),
    ("none", "没设日期"),
)

#: 提醒的状态 -> 人话。⭐ 四种状态必须分得开:
#: 「待响」是还没到点,「已响过」是真响过,「已错过」是到点了却没响(推送环没跑),
#: 「已关闭」是这条提醒本身关着。把它们混成一句"已处理"就等于不说。
REMINDER_STATE_LABELS = {
    "wait": "待响",
    "fired": "已响过",
    "late": "已错过",
    "off": "已关闭",
    "snoozed": "已贪睡",
}

#: 「日程提醒」那一列最多画几条(超出的如实报数,不假装就这几条)。
REMINDER_ROW_MAX = 12
#: 已经响过的那一列最多回顾几条(最近的在最前)。
REMINDER_PAST_MAX = 5

#: ⭐「该响的」= 待响 + 已错过(D117)。已错过**留在明面上**:那是"到点了却没响",
#: 多半是推送环没跑起来 —— 压进折叠里就等于把故障藏起来。
REMINDER_LIVE_STATES = ("wait", "late")
#: 折叠起来的"已了结"最多画几条(超出的如实报总数,不假装就这几条)。
REMINDER_QUIET_MAX = 30


def local_moment(value: Any, tz: Any = None) -> datetime | None:
    """ISO8601 字符串 / datetime -> 带时区的时刻;认不出来返回 None(**不猜**)。"""
    if isinstance(value, datetime):
        moment: datetime = value
    elif value:
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None and tz is not None:
        return moment.replace(tzinfo=tz)
    return moment


def stamp(moment: datetime | None, *, with_year: bool = False) -> str:
    """"10月15日 09:00" —— 列表里大多是同一年,默认不写年份。"""
    if moment is None:
        return ""
    head = (
        str(moment.year) + "年" + str(moment.month) + "月" + str(moment.day) + "日"
        if with_year
        else str(moment.month) + "月" + str(moment.day) + "日"
    )
    return head + " " + moment.strftime("%H:%M")


def clock(moment: datetime | None) -> str:
    return moment.strftime("%H:%M") if moment is not None else ""


def parse_due(value: Any) -> date | None:
    """待办的 due_date_local -> date。

    ⚠️ 列名里写着 local,但模型可能写成 "2026-10-15T09:00" 甚至 "2026-10-15 09:00" ——
       只取前 10 个字符,认不出来就当没设日期(**原文照样显示,只是不参与分桶**)。
    """
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def due_bucket(value: Any, today: date) -> str:
    """待办落在哪个桶里。与 due_label 共用 parse_due 一份实现。"""
    due = parse_due(value)
    if due is None:
        return "none"
    gap = (due - today).days
    if gap < 0:
        return "overdue"
    if gap == 0:
        return "today"
    if gap <= 7:
        return "soon"
    return "later"


def due_label(value: Any, today: date) -> str:
    """到期日的人话:「逾期 3 天」/「今天」/「明天」/「周三」/「10月20日」。"""
    due = parse_due(value)
    if due is None:
        return str(value or "").strip()
    gap = (due - today).days
    if gap < 0:
        return "逾期 " + str(-gap) + " 天"
    if gap == 0:
        return "今天"
    if gap == 1:
        return "明天"
    if gap <= 7:
        return WEEKDAYS[due.weekday()]
    return str(due.month) + "月" + str(due.day) + "日"


def todo_row(
    todo: Any, today: date, tz: Any = None, linked_alarm_at: Any = None
) -> dict[str, Any]:
    """一条待办在页面上的样子。**模板不做任何判断**,全部在这里算好。

    ⭐ 有时刻的待办要看得见它的闹钟(D115):时刻来自 due_at_local,
       提前量来自 remind_minutes_before(NULL = 不提醒)。
    """
    status = str(getattr(todo, "status", "") or "")
    source = str(getattr(todo, "source", "") or "")
    due = getattr(todo, "due_date_local", None)
    bucket = due_bucket(due, today)
    moment = local_moment(getattr(todo, "due_at_local", None), tz)
    remind = getattr(todo, "remind_minutes_before", None)
    at_text = clock(moment)
    # 闹钟可能来自两处:待办自带的(有时刻就有),或者**挂上来的那条提醒**
    # (历史里"转成待办"的走这条 —— 见 D115;两条都要在行上看得见)
    linked = local_moment(linked_alarm_at, tz)
    if at_text and remind is not None and int(remind) > 0:
        alarm_text = "提前 " + human_seconds(int(remind) * 60) + "提醒"
    elif at_text and remind is not None:
        alarm_text = at_text + " 提醒"
    elif linked is not None:
        alarm_text = clock(linked) + " 提醒"
    else:
        alarm_text = ""
    label = due_label(due, today)
    if at_text and moment is not None and not label.startswith(str(moment.year)):
        label = (label + " " + at_text).strip()
    return {
        "id": str(getattr(todo, "id", "") or ""),
        "content": str(getattr(todo, "content", "") or ""),
        "status": status,
        "status_label": TODO_STATUS_LABELS.get(status, status),
        "source": source,
        "source_label": TODO_SOURCE_LABELS.get(source, source),
        "due_text": str(due or ""),
        "due_label": label,
        "due_at_local": str(getattr(todo, "due_at_local", "") or ""),
        "at_text": at_text,
        "alarm_text": alarm_text,
        "has_alarm": bool(alarm_text),
        "bucket": bucket,
        "overdue": bucket == "overdue",
        "done": status == "completed",
        "is_agent": source != "user",
    }


def group_todos(
    todos: Sequence[Any],
    today: date,
    tz: Any = None,
    linked_alarms: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """把**未完成**的待办分到五个桶里;空桶不画(不留空标题占地方)。

    linked_alarms = {待办 id: 挂在它上面的提醒时刻}(调用方查好传进来)。
    """
    alarms = linked_alarms or {}
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in TODO_BUCKETS}
    for todo in todos:
        row = todo_row(todo, today, tz, alarms.get(str(getattr(todo, "id", ""))))
        buckets.setdefault(row["bucket"], []).append(row)
    groups: list[dict[str, Any]] = []
    for key, label in TODO_BUCKETS:
        items = buckets.get(key) or []
        if items:
            groups.append({"key": key, "label": label, "items": items, "count": len(items)})
    return groups


# ---------------------------------------------------------------- 提醒
def human_seconds(seconds: int | None) -> str:
    """秒 -> 「30 分钟」/「2 小时」/「1 天」。除不尽就退回秒(不四舍五入骗人)。"""
    value = int(seconds or 0)
    if value <= 0:
        return "—"
    for unit, size in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if value >= size and value % size == 0:
            return str(value // size) + " " + unit
    return str(value) + " 秒"


def schedule_due(schedule: Any, tz: Any = None) -> datetime | None:
    """这条提醒**该响的时刻**。

    ⚠️ 尽量走领域对象自己的 due_at()(口径只有一处),它坏了才返回 None ——
       展示层不该因为一条坏数据把整页打不开。
    """
    getter = getattr(schedule, "due_at", None)
    if not callable(getter):
        return None
    try:
        return local_moment(getter(), tz)
    except Exception:  # noqa: BLE001 - 展示层兜底
        return None


def schedule_rule(schedule: Any, tz: Any = None, now: datetime | None = None) -> str:
    """这条提醒是"什么时候响"的人话(每天 09:00 / 每 30 分钟 / 一次性 …)。

    ⚠️ 年份只在**跨年**时才写:这一列里大多是今年的提醒,每行都拖着 "2026年"
       会把行撑成两行(用户 2026-10-13 的诉求就是"优化显示")。
    """
    kind = str(getattr(schedule, "kind", "") or "")
    at = local_moment(getattr(schedule, "at", None), tz)
    every = getattr(schedule, "every_seconds", None)
    every = int(every) if every else None
    if kind == "at":
        if at is None:
            return "一次性(时刻缺失)"
        same_year = now is not None and at.year == now.year
        return "一次性 " + stamp(at, with_year=not same_year)
    if kind == "after":
        return "建好后 " + human_seconds(every) + " 响一次"
    if kind == "every":
        if every and every >= 86400 and every % 86400 == 0:
            days = every // 86400
            head = "每天" if days == 1 else "每 " + str(days) + " 天"
            return head + (" " + clock(at) if at else "(从建立时刻算起)")
        return "每 " + human_seconds(every)
    return kind


def schedule_row(
    schedule: Any, now: datetime, tz: Any = None, todo: Any = None
) -> dict[str, Any]:
    """一条提醒在页面上的样子(规则 + 状态 + 下一次/上一次 + **它属于哪条待办**)。

    todo 由调用方按 schedule.todo_id 查好传进来(这一层不碰库)。带上它以后,
    页面上就能写"属于待办:更新云朵智能体",并且给一个就地勾掉的按钮 ——
    提醒与待办本来就是同一件事的两个面(D115)。
    """
    enabled = bool(getattr(schedule, "enabled", True))
    kind = str(getattr(schedule, "kind", "") or "")
    due = schedule_due(schedule, tz)
    fired = local_moment(getattr(schedule, "last_fired_at", None), tz)
    if not enabled:
        state = "off"
    elif kind in ("at", "after"):
        if fired is not None:
            state = "fired"
        elif due is not None and due <= now:
            # 到点了却没响过:这不是"没事",是推送环没跑起来 —— 如实说
            state = "late"
        else:
            state = "wait"
    else:
        # ⭐ D117 实测踩到:重复提醒(every)的"响过"**不是终点** —— 每天 9 点那条响过
        #    一次就被折进"已了结",等于把"明天还会响"藏起来。它永远算"待响",
        #    上次响的时刻挂在后面当证据(见 when_text)。
        state = "wait"
    if state == "fired":
        when_text = "上次 " + stamp(fired)
    elif state == "late":
        # ⚠️ 实测踩到:「一次性 10月15日 20:15 · 该响于 10月15日 20:15」—— 同一句话说两遍。
        #    属于一次性提醒的时刻,**规则那一栏就是它**(见 schedule_rule),状态标签说
        #    "已错过"就够了。这一栏只负责补规则里没有的信息。
        when_text = "" if kind == "at" else "该响于 " + stamp(due)
    elif state == "off":
        when_text = "关着,不会响"
    elif due is None:
        when_text = "下次待定"
    elif kind == "after":
        when_text = "将在 " + stamp(due)
    elif kind == "at":
        when_text = ""          # 规则里已经写着"一次性 10月15日 23:00",不必再说一遍
    else:
        when_text = "下次 " + stamp(due)
        if fired is not None and kind not in ("at", "after"):
            # 今天响的就写"今天 09:00":一眼看得出这是刚响过、明天还会响的那条
            when_text += " · 上次 " + (
                "今天 " + clock(fired) if fired.date() == now.date() else stamp(fired)
            )
    todo_id = str(getattr(schedule, "todo_id", "") or "")
    return {
        "id": str(getattr(schedule, "id", "") or ""),
        "prompt": str(getattr(schedule, "prompt", "") or ""),
        "kind": kind,
        "enabled": enabled,
        "rule": schedule_rule(schedule, tz, now),
        "state": state,
        "state_label": REMINDER_STATE_LABELS.get(state, state),
        "when_text": when_text,
        "due": due,
        # -- 它属于哪条待办(独立提醒三项都为空)
        "todo_id": todo_id,
        "is_alarm": bool(todo_id),
        "todo_content": str(getattr(todo, "content", "") or "") if todo is not None else "",
        "todo_missing": bool(todo_id) and todo is None,
        "todo_done": bool(
            todo is not None and str(getattr(todo, "status", "") or "") == "completed"
        ),
        "target_member_id": getattr(schedule, "target_member_id", None),
    }


def split_reminder_rows(
    rows: Sequence[dict[str, Any]], *, quiet_max: int = REMINDER_QUIET_MAX
) -> dict[str, Any]:
    """提醒行分成**该响的**与**已经了结的**两组(D117)。

    ⭐ 用户 2026-10-14:"用户点到界面应该是看到想看的东西……像已完成的你应该收缩起来"。
       已经响过/已经关掉的提醒不是不重要,而是**不需要再看第二眼** —— 它们和"该响的"
       混在一列里,就把"接下来会不会响"挤到第一屏之外去了(实测第一屏全被表单和
       已了结的行占满)。所以这里只分组、不丢行:折叠里画不下时如实报总数。

    ⚠️ 顺序**沿用调用方排好的顺序**(app.py 按 late → wait → fired → off 排),
       这里不再排一次 —— 两处各排一遍,迟早会不一致。
    """
    live = [row for row in rows if str(row.get("state") or "") in REMINDER_LIVE_STATES]
    quiet = [row for row in rows if str(row.get("state") or "") not in REMINDER_LIVE_STATES]
    return {
        "live": live,
        "wait": sum(1 for row in live if row.get("state") == "wait"),
        "late": sum(1 for row in live if row.get("state") == "late"),
        "quiet": quiet[:quiet_max],
        "quiet_total": len(quiet),
    }


def calendar_reminder_rows(
    occurrences: Sequence[Any],
    minutes_by_uid: dict[str, int],
    instances: dict[tuple[str, str], dict[str, Any]],
    *,
    now: datetime,
    tz: Any = None,
    limit: int = REMINDER_ROW_MAX,
    past_max: int = REMINDER_PAST_MAX,
) -> dict[str, Any]:
    """日程自带的提醒(事件提前 N 分钟响)按**该响的时刻**排好。

    为什么直接由实例算、而不是只读 app_reminder_instances:
    物化只覆盖未来 14 天(见 reminders.DEFAULT_HORIZON_DAYS),而这一页的窗口可能到
    400 天 —— 只读那张表的话,"下个月的日程会不会提醒我"永远答不上来。所以这里用
    事件本身算(与物化同一算法),再拿实例表**补上"响没响"**。

    ⚠️ reminder_minutes <= 0 = 这条日程不提醒(与 materialize 的过滤同一口径)。
    """
    rows: list[dict[str, Any]] = []
    for item in occurrences:
        uid = str(getattr(item, "uid", "") or "")
        minutes = int(minutes_by_uid.get(uid) or 0)
        if minutes <= 0:
            continue
        start = local_moment(getattr(item, "start", None), tz)
        if start is None:
            continue
        fire = start - timedelta(minutes=minutes)
        info = instances.get((uid, str(getattr(item, "instance_id", "") or ""))) or {}
        fired = local_moment(info.get("fired_at"), tz)
        snooze = local_moment(info.get("snooze_until_utc"), tz)
        if snooze is not None and snooze > now:
            state = "snoozed"
            label = "贪睡到 " + stamp(snooze)
        elif fired is not None:
            state, label = "fired", "已响过 " + stamp(fired)
        elif fire <= now:
            # 到点了、表里却还没有"已响"的记录 —— 多半是窗口太短/推送环没跑到
            state, label = "late", "该响没响"
        else:
            state, label = "wait", "提前 " + human_seconds(minutes * 60)
        rows.append(
            {
                "key": uid + "|" + str(getattr(item, "instance_id", "") or ""),
                "title": str(getattr(item, "title", "") or "(无标题)"),
                "fire": fire,
                "fire_text": stamp(fire),
                "start_text": stamp(start),
                "lead_text": "提前 " + human_seconds(minutes * 60),
                "state": state,
                "state_label": label,
                "fired": fired is not None,
                "all_day": bool(getattr(item, "all_day", False)),
                "location": str(getattr(item, "location", "") or ""),
            }
        )
    rows.sort(key=lambda row: (row["fire"], row["title"]))
    upcoming = [row for row in rows if not row["fired"] and row["fire"] > now]
    past = [row for row in rows if row["fired"] or row["fire"] <= now]
    return {
        "upcoming": upcoming[:limit],
        "upcoming_more": max(len(upcoming) - limit, 0),
        "upcoming_total": len(upcoming),
        "past": list(reversed(past))[:past_max],
        "past_total": len(past),
        "total": len(rows),
    }


__all__ = [
    "REMINDER_LIVE_STATES",
    "REMINDER_PAST_MAX",
    "REMINDER_QUIET_MAX",
    "REMINDER_ROW_MAX",
    "REMINDER_STATE_LABELS",
    "TODO_BUCKETS",
    "TODO_SOURCE_LABELS",
    "TODO_STATUS_LABELS",
    "WEEKDAYS",
    "calendar_reminder_rows",
    "clock",
    "due_bucket",
    "due_label",
    "group_todos",
    "human_seconds",
    "local_moment",
    "parse_due",
    "schedule_due",
    "schedule_rule",
    "schedule_row",
    "split_reminder_rows",
    "stamp",
    "todo_row",
]
