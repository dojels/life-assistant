"""日程域的请求/响应模型(schema 单真源,与记账同一条规则)。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CalendarEventCreate(BaseModel):
    """建一个日程。时间必须给成 ISO8601,由调用方(模型或界面)负责把"下周三下午三点"解析成具体时刻。"""

    title: str = Field(..., max_length=80, description="事件标题,例如 和医生复诊")
    start: str = Field(
        ..., max_length=40,
        description="开始时间,ISO8601 带时区,例如 2026-10-14T15:00:00+08:00",
    )
    end: str | None = Field(
        None, max_length=40, description="结束时间;不给则默认 1 小时(全天事件则为一整天)"
    )
    rrule: str | None = Field(
        None, max_length=200,
        description="重复规则,RFC5545 原文,例如 FREQ=WEEKLY;BYDAY=MO;COUNT=6。不重复就留空。",
    )
    location: str | None = Field(None, max_length=120, description="地点")
    description: str | None = Field(None, max_length=500, description="备注")
    all_day: bool = Field(False, description="是否是全天事件")
    reminder_minutes: int = Field(
        30, ge=0, le=10080, description="提前多少分钟提醒;0 表示不提醒"
    )


class CalendarReschedule(BaseModel):
    """改期。这是"例外三种机制"的统一入口。"""

    uid: str = Field(..., max_length=80, description="事件 uid")
    recurrence_id: str | None = Field(
        None, max_length=40,
        description="要改的那一次的原开始时间;不传则改整个系列(或单次事件本身)",
    )
    new_start: str | None = Field(None, max_length=40, description="新的开始时间,ISO8601")
    new_end: str | None = Field(None, max_length=40, description="新的结束时间")
    new_title: str | None = Field(None, max_length=80, description="新的标题")
    this_and_future: bool = Field(
        False,
        description="true = 此次及以后都改(RFC 的 RANGE=THISANDFUTURE);false = 只改这一次",
    )


class CalendarCancel(BaseModel):
    """取消。只取消某一次 = EXDATE;取消整个系列 = 把主事件置为 cancelled。"""

    uid: str = Field(..., max_length=80, description="事件 uid")
    recurrence_id: str | None = Field(
        None, max_length=40, description="要取消的那一次的原开始时间;不传则取消整个系列"
    )


class CalendarList(BaseModel):
    """列出某个时间窗内的日程(重复事件会被展开成一个个实例)。"""

    with_coverage: bool = Field(
        False,
        description="额外给一份覆盖域:日历里一共存了多少条事件、最早最晚到什么时候 —— 用来回答「我排过多少事」。",
    )
    start: str = Field(..., max_length=40, description="窗口开始,ISO8601")
    end: str = Field(..., max_length=40, description="窗口结束,ISO8601")
    limit: int = Field(50, ge=1, le=200, description="最多返回多少条")


class CalendarEventRead(BaseModel):
    uid: str
    title: str
    start: str
    end: str | None = None
    rrule: str | None = None
    location: str | None = None
    status: str = "confirmed"
    overrides: int = Field(0, description="这个事件上有多少条例外(改期/取消)")


class OccurrenceRead(BaseModel):
    uid: str
    instance_id: str = Field(..., description="实例身份 = 它实际开始的时间")
    start: str
    end: str | None = None
    title: str
    location: str = ""
    all_day: bool = False
    original_start: str | None = Field(
        None, description="被改过的那一次的原开始时间;要再改/取消它时把值传给 recurrence_id"
    )
    is_override: bool = Field(False, description="这一次是否被单独改过")
    is_recurring: bool = False


class CalendarQueryResult(BaseModel):
    start: str
    end: str
    count: int
    truncated: bool = False
    occurrences: list[OccurrenceRead] = Field(default_factory=list)
    source: str = "calendar"
