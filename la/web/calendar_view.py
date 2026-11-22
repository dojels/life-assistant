"""月历网格:把「某个月有哪些日程」画成**看得懂的月视图**。

为什么单独一个模块、而且写成纯函数(不碰数据库、不碰 Jinja):
  * 网格的**填充与跨月边界**是最容易出错的地方(4 月 1 日是星期几、上个月要补几天、
    下个月要补几天),把它从"渲染"里剥出来才好写测试;
  * 页面**不依赖 JS** 也能看 —— 月历是排期用的,断网、欠费、关掉模型时它必须还在。

三条口径(与前端的 agenda 列表**共用同一份展开结果**,不重算):
  1. 跨天事件落在它覆盖的**每一天**(10-12 20:00 → 10-13 01:00 两天都显示),
     否则"这两天都占着"这件事在月历上会消失;
  2. **全天事件只落在开始那天**(连开三天的假,画三格反而看不清是同一件事);
  3. 每天最多列 `per_day_max` 条,**多出来的如实报数**(`+N`)——
     绝不假装那天就这几件事。

⚠️ 时区:所有时间都按事件自己的 tzid 显示(库里存的是带时区的 ISO8601),
   这里不做任何 UTC 换算 —— 「20:00 的事」就该显示 20:00。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

#: 四个页签(见 app 文件头 CAL_TABS 的同一套值)。
CAL_TAB_MONTH = "month"
CAL_TAB_AGENDA = "agenda"
CAL_TAB_REMINDERS = "reminders"
CAL_TAB_TODOS = "todos"
CAL_TABS = (CAL_TAB_MONTH, CAL_TAB_AGENDA, CAL_TAB_REMINDERS, CAL_TAB_TODOS)

#: 日程状态 -> 人话。⚠️ 不能借账本的 STATUS_LABELS(draft/confirmed/void)。
EVENT_STATUS_LABELS = {"confirmed": "有效", "cancelled": "已取消", "tentative": "待定"}

#: 周一开头(中文习惯)。
WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def normalize_calendar_tab(value: str, *, month: str = "", days: str = "") -> str:
    """把 /calendar 的 tab 收敛到白名单。认不出来就按 has month/days 猜。"""
    wanted = str(value or "").strip().lower()
    if wanted in CAL_TABS:
        return wanted
    if str(month or "").strip():
        return CAL_TAB_MONTH
    if str(days or "").strip():
        return CAL_TAB_AGENDA
    return CAL_TAB_MONTH
#: 每天在格子里最多列几条;多出来的给 "+N"。手机端由 CSS 折叠到更少。
PER_DAY_MAX = 3
#: 月份参数(YYYY-MM)的合法范围,超出就当作"没给"。
_MONTH_MIN = "2000-01"
_MONTH_MAX = "2099-12"


@dataclass
class DayCell:
    """月历里的一格。`in_month=False` 的是补位的上/下月日期。"""

    key: str                                  # 2026-10-12
    day: int
    in_month: bool
    is_today: bool
    is_weekend: bool
    items: list[dict[str, Any]] = field(default_factory=list)
    more: int = 0
    #: 全天/跨天/当天条目**全量**计数(不受 per_day_max 截断影响)。
    #: monthly 统计要用它,不能拿 len(items) —— 截断后会把"多出来的"算丢。
    event_count: int = 0
    todo_count: int = 0

    @property
    def count(self) -> int:
        return len(self.items) + self.more


@dataclass
class MonthView:
    key: str                                  # 2026-10
    title: str                                # 2026 年 10 月
    subtitle: str                             # 今年就不再重复写年份
    prev_key: str
    next_key: str
    weekday_labels: tuple[str, ...]
    rows: list[list[DayCell]]
    today_key: str
    month_items: int                          # 这个月一共几个**日程**(保留旧口径)
    month_days: int                           # 这个月有几天是有事的(日程或待办)
    grid_items: int                           # 网格范围内(含补位几天)一共几件事
    todo_items: int = 0                       # 这个月一共有几条**待办**落在格子上


def parse_month(text: Any) -> tuple[int, int] | None:
    """`YYYY-MM` -> (年, 月)。认不出来就 None(**不猜**,交给调用方回落到本月)。"""
    value = str(text or "").strip()
    if len(value) != 7 or value[4] != "-":
        return None
    head, tail = value[:4], value[5:]
    if not (head.isdigit() and tail.isdigit()):
        return None
    year, month = int(head), int(tail)
    if not (1 <= month <= 12):
        return None
    if not (_MONTH_MIN <= value <= _MONTH_MAX):
        return None
    return year, month


def month_key(year: int, month: int) -> str:
    return str(year).zfill(4) + "-" + str(month).zfill(2)


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """按月平移,**不碰日期算术**(12 月 +1 = 次年 1 月)。"""
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def month_days(year: int, month: int) -> list[date]:
    first = date(year, month, 1)
    last = (date(*shift_month(year, month, 1), 1) - timedelta(days=1))
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _tzinfo_of(occurrences: Sequence[Any]) -> Any:
    for item in occurrences:
        tzinfo = getattr(getattr(item, "start", None), "tzinfo", None)
        if tzinfo is not None:
            return tzinfo
    return None


def _to_local_date(value: datetime, tzinfo: Any) -> date:
    if tzinfo is not None:
        try:
            return value.astimezone(tzinfo).date()
        except (ValueError, OSError):
            pass
    return value.date()


def _ends_at_midnight(item: Any, tzinfo: Any) -> bool:
    """结束时刻是不是**刚好落在 00:00**(那就代表"到那天开始为止",不含那天)。"""
    if item.end is None:
        return False
    return _to_local_time(item.end, tzinfo) == _to_local_time(
        item.start.replace(hour=0, minute=0, second=0, microsecond=0), tzinfo
    )


def _tail_day(item: Any, tzinfo: Any) -> date:
    """这条实例**最后占位的那一天**(闭区间)。

    ⚠️ 三条容易搞反的:
      * 12:00–13:00 → 只占 12 号(结束在 13:00,不是 13 号);
      * 12 号 20:00 → 13 号 01:00 → **两天都占**(凌晨 1 点还在);
      * 全天事件 + 结束时刻 00:00 → 只占开始那天(结束时刻是"不含")。
    """
    start_day = _to_local_date(item.start, tzinfo)
    if item.end is None:
        return start_day
    stop_day = _to_local_date(item.end, tzinfo)
    if stop_day <= start_day:
        return start_day
    # 结束时刻刚好 00:00 = "到那天开始为止",那一天不算
    return stop_day - timedelta(days=1) if _ends_at_midnight(item, tzinfo) else stop_day


def _spans(item: Any, day: date, tzinfo: Any) -> bool:
    """这条实例在 `day` 这天占不占位置。判据与分桶完全同一份实现。"""
    start_day = _to_local_date(item.start, tzinfo)
    if start_day == day:
        return True
    if bool(getattr(item, "all_day", False)) or item.end is None:
        return False                      # 全天事件只落开始那天
    return start_day < day <= _tail_day(item, tzinfo)


def _local(value: datetime, tzinfo: Any) -> datetime:
    if tzinfo is None:
        return value
    try:
        return value.astimezone(tzinfo)
    except (ValueError, OSError):
        return value


def _clock(value: datetime, tzinfo: Any) -> str:
    return _local(value, tzinfo).strftime("%H:%M")


def _to_local_time(value: datetime, tzinfo: Any) -> Any:
    """本地钟面上的时刻(用来判断"是不是刚好跨了一整天")。"""
    return _local(value, tzinfo).time()


def _cell_item(item: Any, tzinfo: Any, day: date) -> dict[str, Any]:
    """一条实例在**某一天**的那个条目。全部在这里算好,模板不做判断。"""
    start_day = _to_local_date(item.start, tzinfo)
    all_day = bool(getattr(item, "all_day", False))
    continuing = start_day != day            # 跨天事件的第二天(不是"开始那格")
    if all_day:
        what = "全天"
    elif continuing:
        what = "续"
    else:
        what = _clock(item.start, tzinfo)
    end_text = ""
    if not all_day and item.end is not None and _to_local_date(item.end, tzinfo) == day:
        end_text = _clock(item.end, tzinfo)
    mark = "✎" if getattr(item, "is_override", False) else (
        "↻" if getattr(item, "is_recurring", False) else ""
    )
    return {
        "kind": "event",
        "key": start_day.isoformat() + "|" + str(getattr(item, "title", "")),
        "what": what,
        "end": end_text,
        "title": str(getattr(item, "title", "") or "(无标题)"),
        "location": str(getattr(item, "location", "") or ""),
        "mark": mark,
        "all_day": all_day,
        "continuing": continuing,
    }


def _todo_cell_item(todo: Any, day: date, tzinfo: Any, today: date) -> dict[str, Any]:
    """一条待办在月历格子里的样子。

    ⚠️ 待办不是"缩小的日程":它没有结束时刻,只有到期日/到点时刻;
       但有到期时刻的待办在日历里占一个时间点(和它的闹钟一致)。
    """
    from . import task_view

    row = task_view.todo_row(todo, today, tzinfo)
    moment = task_view.local_moment(getattr(todo, "due_at_local", None), tzinfo)
    timed = moment is not None and moment.date() == day
    return {
        "kind": "todo",
        "key": "todo:" + str(getattr(todo, "id", "") or "") + "|" + day.isoformat(),
        # 有时刻就显示时刻;只有日期就显示"待办"(模板用它当小标签)
        "what": moment.strftime("%H:%M") if timed else "待办",
        "end": "",
        "title": row["content"] or "(无内容)",
        "location": "",
        "mark": "",
        "all_day": not timed,
        "continuing": False,
        "todo_id": row["id"],
        "overdue": row["overdue"],
        "has_alarm": row["has_alarm"],
        "due_label": row["due_label"],
    }


def _sort_key(entry: dict[str, Any]) -> tuple:
    """全天日程 -> 待办 -> 续排 -> 有时刻的日程,同类按时刻/标题。

    待办排在"续排"之前,是因为它是**还没做完的事**,月历上应该先被看到;
    全天日程仍压在最前(它占一整天,先给读者一个"这天有事"的信号)。
    """
    if entry.get("kind") == "todo":
        bucket, moment = 1, ("00:00" if entry.get("all_day") else str(entry.get("what") or "00:00"))
    elif entry.get("all_day"):
        bucket, moment = 0, "00:00"
    elif entry.get("continuing"):
        bucket, moment = 2, "00:00"
    else:
        bucket, moment = 3, str(entry.get("what") or "00:00")
    return (bucket, moment, entry.get("title") or "")


def build_month_view(
    occurrences: Sequence[Any],
    *,
    start: datetime,
    end: datetime,
    today: date,
    month: Any = "",
    per_day_max: int = PER_DAY_MAX,
    monday_first: bool = True,
    todos: Sequence[Any] = (),
) -> MonthView:
    """把展开好的实例装进月历网格。

    `start` / `end` 是**这批实例的覆盖范围**(调用方展开时用的窗口):
    请求的月份不在这个范围里就回落 —— 宁可显示本月并说清,也不要画一张空白的月历
    让用户以为"这个月没事"。
    """
    tzinfo = _tzinfo_of(occurrences) or getattr(start, "tzinfo", None)
    wanted = parse_month(month)
    if wanted is None:
        anchor = min(max(today, start.date()), _last_day(end))
        wanted = (anchor.year, anchor.month)
    year, month_number = wanted

    days = month_days(year, month_number)
    first = days[0]
    last = days[-1]

    # ⚠️ 分桶扫的是**整个网格**(含上/下月的补位格,整周填满后最多 42 天),
    #    而不是"只扫当月 1..31"。用当月做边界的话,补位格里那些日期
    #    (例如 11 月 1 日)永远分不到东西 —— 而月历上明明画着那一格(实测踩过)。
    grid_first = first - timedelta(days=first.weekday())
    grid_last = grid_first + timedelta(days=34)        # 6 周以内足够容纳任何月份
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in occurrences:
        start_day = _to_local_date(item.start, tzinfo)
        # 全天事件只落开始那天(见模块头第 2 条)
        if bool(getattr(item, "all_day", False)) or item.end is None:
            tail = start_day
        else:
            tail = _tail_day(item, tzinfo)
        cursor = max(start_day, grid_first)
        stop = min(tail, grid_last)
        while cursor <= stop:
            buckets.setdefault(cursor.isoformat(), []).append(
                _cell_item(item, tzinfo, cursor)
            )
            cursor += timedelta(days=1)
    # ⭐ 待办直接进同一批 bucket(2026-10-22 用户:"待办事项也应该要显示在日历里")。
    #    只放未完成的;已完成的不进月历,否则历史会越堆越满 —— 它们仍在「待办」页的折叠里。
    if todos:
        from . import task_view

        for todo in todos:
            if str(getattr(todo, "status", "") or "") == "completed":
                continue
            due = task_view.parse_due(getattr(todo, "due_date_local", None))
            if due is None:
                moment = task_view.local_moment(getattr(todo, "due_at_local", None), tzinfo)
                due = moment.date() if moment is not None else None
            if due is None or due < grid_first or due > grid_last:
                continue
            buckets.setdefault(due.isoformat(), []).append(
                _todo_cell_item(todo, due, tzinfo, today)
            )

    for entries in buckets.values():
        entries.sort(key=_sort_key)
    # 周一开头:周一=0;周日开头:周一=1…… 位置算错会让整月错位一格
    lead = first.weekday() if monday_first else (first.weekday() + 1) % 7
    grid_start = first - timedelta(days=lead)
    total_cells = lead + len(days)
    rows_count = (total_cells + 6) // 7

    rows: list[list[DayCell]] = []
    for row_index in range(rows_count):
        row: list[DayCell] = []
        for column in range(7):
            day = grid_start + timedelta(days=row_index * 7 + column)
            key = day.isoformat()
            entries = buckets.get(key, [])
            cell = DayCell(
                key=key,
                day=day.day,
                in_month=day.month == month_number and day.year == year,
                is_today=day == today,
                is_weekend=day.weekday() >= 5,
                items=entries[:per_day_max],
                more=max(len(entries) - per_day_max, 0),
                event_count=sum(1 for entry in entries if entry.get("kind") != "todo"),
                todo_count=sum(1 for entry in entries if entry.get("kind") == "todo"),
            )
            row.append(cell)
        rows.append(row)

    month_cells = [cell for row in rows for cell in row if cell.in_month]
    prev_year, prev_month = shift_month(year, month_number, -1)
    next_year, next_month = shift_month(year, month_number, 1)
    return MonthView(
        key=month_key(year, month_number),
        title=str(year) + " 年 " + str(month_number) + " 月",
        subtitle=(str(month_number) + " 月") if year == today.year else (
            str(year) + " 年 " + str(month_number) + " 月"
        ),
        prev_key=month_key(prev_year, prev_month),
        next_key=month_key(next_year, next_month),
        weekday_labels=WEEKDAYS if monday_first else (WEEKDAYS[6], *WEEKDAYS[:6]),
        rows=rows,
        today_key=today.isoformat(),
        # ⚠️ month_items 保留"日程数"的旧口径(页面上仍写「本月 N 条」);
        #    待办单独计 todo_items,两种条目在格子里一起画。
        month_items=sum(cell.event_count for cell in month_cells),
        todo_items=sum(cell.todo_count for cell in month_cells),
        month_days=sum(1 for cell in month_cells if cell.count),
        grid_items=sum(cell.count for row in rows for cell in row),
    )


def _last_day(moment: datetime) -> date:
    """窗口的最后一刻落在哪一天(end 是**开区间**,所以退一秒)。"""
    return (moment - timedelta(seconds=1)).date()


__all__ = [
    "DayCell",
    "MonthView",
    "PER_DAY_MAX",
    "WEEKDAYS",
    "build_month_view",
    "month_days",
    "month_key",
    "parse_month",
    "shift_month",
]

# 让测试能直接验证"哪一天占位"这条口径(它是月历最容易错的地方)
_spans_for_tests = _spans
