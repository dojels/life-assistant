"""中文时间表达 -> 时间区间。

只覆盖生活场景里的高频写法,不追求完整的自然语言时间解析。
**解析不出来就返回 None** —— 宁可让助手改问用户,也不要猜错时间范围:
猜错 = 翻错档案 = 比"没找到"更坏的回答。

支持:
    今天 / 昨天 / 前天 / 明天 / 后天
    上周三 / 这周三 / 下周三 / 周日 ...
    3 天前 / 2 周前
    2026-05-10 / 5月10日 / 5/10
    上午 / 下午 / 晚上 / 中午 / 凌晨(时间窗,可与其他表达组合)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

PERIODS: dict[str, tuple[int, int]] = {
    "凌晨": (0, 6),
    "早上": (6, 9),
    "早晨": (6, 9),
    "上午": (6, 12),
    "中午": (11, 14),
    "下午": (12, 18),
    "傍晚": (17, 20),
    "晚上": (18, 24),
    "夜里": (20, 24),
    "半夜": (22, 24),
}

_ABS_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_CN_DATE = re.compile(r"(\d{1,2})月(\d{1,2})日")
_SLASH_DATE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)")
_WEEK = re.compile(r"(上个|这个|下个|上|这|本|下)(?:周|星期|礼拜)([一二三四五六日天])")
_DAYS_AGO = re.compile(r"(\d+)\s*天前")
_WEEKS_AGO = re.compile(r"(\d+)\s*(?:周|个?星期|礼拜)前")
_REL_DAY = re.compile(r"(今天|今日|昨天|昨日|前天|明天|明日|后天)")

_REL_DAY_OFFSET = {
    "前天": -2,
    "昨天": -1,
    "昨日": -1,
    "今天": 0,
    "今日": 0,
    "明天": 1,
    "明日": 1,
    "后天": 2,
}

_WEEK_OFFSET = {"上": -1, "上个": -1, "这": 0, "这个": 0, "本": 0, "下": 1, "下个": 1}


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime
    label: str

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "label": self.label}


def midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _safe_day(year: int, month: int, day: int, tz: Any = None) -> datetime | None:
    """构造某一天;日期不合法就返回 None。

    ⚠️ 2026-10-17 审计实测:直接 datetime(...) 会因为"2月29日(平年)/ 13月 /
    2026-02-31"抛 ValueError,而它在 QA 阶段没有任何 try —— **一句话就能打死整轮对话**
    (用户得到一个 error 帧,库里还留下一条没有回复的 user 事件)。
    这个函数的文档一直写的是"解析不出来返回 None",现在代码也是了。
    """
    try:
        return datetime(year, month, day, tzinfo=tz)
    except ValueError:
        return None


def parse_range(text: str, now: datetime) -> TimeRange | None:
    """把一句话里的时间表达解析成一个左闭右开区间;解析不出来返回 None。"""
    if not text:
        return None
    day: datetime | None = None
    label = ""

    match = _ABS_DATE.search(text)
    if match:
        year, month, day_of_month = (int(part) for part in match.groups())
        day = _safe_day(year, month, day_of_month, now.tzinfo)
        label = match.group(0)

    if day is None:
        match = _CN_DATE.search(text)
        if match:
            month, day_of_month = int(match.group(1)), int(match.group(2))
            day = _safe_day(now.year, month, day_of_month, now.tzinfo)
            label = match.group(0)

    if day is None:
        match = _SLASH_DATE.search(text)
        if match:
            month, day_of_month = int(match.group(1)), int(match.group(2))
            day = _safe_day(now.year, month, day_of_month, now.tzinfo)
            label = match.group(0)

    if day is None:
        match = _REL_DAY.search(text)
        if match:
            word = match.group(1)
            day = midnight(now) + timedelta(days=_REL_DAY_OFFSET[word])
            label = word

    if day is None:
        match = _WEEK.search(text)
        if match:
            prefix, weekday_word = match.group(1), match.group(2)
            monday = midnight(now) - timedelta(days=now.weekday())
            day = monday + timedelta(days=_WEEK_OFFSET[prefix] * 7 + WEEKDAY_INDEX[weekday_word])
            label = match.group(0)

    if day is None:
        match = _DAYS_AGO.search(text)
        if match:
            day = midnight(now) - timedelta(days=int(match.group(1)))
            label = match.group(0)

    if day is None:
        match = _WEEKS_AGO.search(text)
        if match:
            day = midnight(now) - timedelta(weeks=int(match.group(1)))
            label = match.group(0)

    period_word: str | None = None
    period_hours: tuple[int, int] | None = None
    for word, hours in PERIODS.items():
        if word in text:
            period_word, period_hours = word, hours
            break

    if day is None and period_hours is not None:
        day = midnight(now)
        label = period_word or ""

    if day is None:
        return None

    day = midnight(day)
    if period_hours is not None:
        start = day + timedelta(hours=period_hours[0])
        end = day + timedelta(hours=period_hours[1])
        full_label = (label + " " + (period_word or "")).strip()
    else:
        start = day
        end = day + timedelta(days=1)
        full_label = label or day.strftime("%Y-%m-%d")
    return TimeRange(start=start, end=end, label=full_label)


def describe(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")
