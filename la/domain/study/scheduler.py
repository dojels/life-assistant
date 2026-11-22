"""复习排程:固定间隔 与 FSRS 两种策略,同一接口。

⭐ 为什么要有"固定间隔"这一档(能力域 §5.4.3 的明确建议):

    先用固定间隔(1/3/7/15/30)验证**孩子愿不愿意用**,再换 FSRS。
    调研的原话是:"唯一真正的风险是'孩子愿不愿意用',不是算法"。

所以默认引擎是 `fixed`,FSRS 已经实现好并测试过,想切换只改一个环境变量。

⚠️ FSRS 落地的五个坑,这里逐条对应:
  1. py-fsrs 只认 UTC -> `due_utc` 与 `due_date_local` 两个都存;
  2. `maximum_interval` 默认 36500 天(实测确认)-> 改成 180,保证学年内至少再见一次;
  3. 新建 Card() 立即到期 -> 在 M4.3a 就把首次到期设在明天;
  4. **孩子端只给"忘了/会了"两个按钮** -> 见 StudyService.render_card(child);
  5. 第一年不要用 optimizer -> 本模块不加载任何个性化参数。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from ... import config

ENGINE_FIXED = "fixed"
ENGINE_FSRS = "fsrs"
ENGINES = (ENGINE_FIXED, ENGINE_FSRS)

# 固定间隔档:先用它验证意愿
FIXED_INTERVALS_DAYS = (1, 3, 7, 15, 30)

# 孩子端只有两个按钮(坑之四):忘了 = Again(1),会了 = Good(3)
RATING_AGAIN = 1
RATING_HARD = 2
RATING_GOOD = 3
RATING_EASY = 4
CHILD_RATINGS = (RATING_AGAIN, RATING_GOOD)

RATING_LABELS = {1: "忘了", 2: "有点难", 3: "会了", 4: "太简单"}

DEFAULT_RETENTION = 0.9
MAX_INTERVAL_DAYS = 180
# ⚠️ 单一来源(2026-10-17 审计):这个串原先在 8 个模块各写一份。
#    改 .env 的 LA_TIMEZONE 而这里不动,就会出现"提示词说伦敦、提醒按上海响"。
#    真正决定每个服务用哪个时区的是**构造参数 tzid**(由 build_services 注入 settings.timezone),
#    这里只是"没注入时的兜底"。
DEFAULT_TZID = config.DEFAULT_TIMEZONE


@dataclass
class UnitState:
    """排程器眼里一个复习单元的全部状态。"""

    stability: float = 0.0
    difficulty: float = 0.0
    state: str = "new"
    reps: int = 0
    lapses: int = 0
    last_interval_days: float | None = None
    last_review_utc: str | None = None
    due_utc: str | None = None
    step: int | None = None       # FSRS 的学习步游标;当前用 learning_steps=() 所以恒为 None


@dataclass
class ReviewResult:
    due_utc: str
    due_date_local: str
    state: str
    stability: float
    difficulty: float
    interval_days: float
    reps: int
    lapses: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "due_utc": self.due_utc,
            "due_date_local": self.due_date_local,
            "state": self.state,
            "stability": round(self.stability, 4),
            "difficulty": round(self.difficulty, 4),
            "interval_days": round(self.interval_days, 3),
            "reps": self.reps,
            "lapses": self.lapses,
        }


def local_due_date(due_utc: datetime, now: datetime, tzid: str = DEFAULT_TZID) -> str:
    """把 UTC 到期时刻换算成"哪一天该复习"。

    ⚠️ 同一天内不重复出现:FSRS 的学习步可能是 10 分钟,而错题本是**一天看一次**的东西。
    真按 10 分钟算,孩子当天会被同一道题反复叫住。所以 `due_date_local` 至少是明天,
    而 `due_utc` 保留 FSRS 的真实值 —— 模型不失真,体验不失礼。
    """
    local = due_utc.astimezone(ZoneInfo(tzid))
    today = now.astimezone(ZoneInfo(tzid)).date()
    if local.date() <= today:
        return (today + timedelta(days=1)).isoformat()
    return local.date().isoformat()


class SchedulingPolicy(Protocol):
    name: str

    def review(self, state: UnitState, rating: int, now: datetime) -> ReviewResult: ...


class FixedIntervalPolicy:
    """固定间隔:1 / 3 / 7 / 15 / 30 天。答错回到第一档。"""

    name = ENGINE_FIXED

    def __init__(
        self, intervals: tuple[int, ...] = FIXED_INTERVALS_DAYS, tzid: str = DEFAULT_TZID
    ) -> None:
        self.intervals = intervals
        self.tzid = tzid

    def review(self, state: UnitState, rating: int, now: datetime) -> ReviewResult:
        if rating not in RATING_LABELS:
            raise ValueError("评分只能是 1..4")
        failed = rating == RATING_AGAIN
        # reps 当作"已经连续记住几步"
        step = 0 if failed else min(state.reps, len(self.intervals) - 1)
        interval = self.intervals[step]
        due = now + timedelta(days=interval)
        return ReviewResult(
            due_utc=due.astimezone(timezone.utc).isoformat(timespec="seconds"),
            due_date_local=local_due_date(due, now, self.tzid),
            state="relearning" if failed else ("new" if state.reps == 0 else "review"),
            stability=float(step),
            difficulty=0.0,
            interval_days=float(interval),
            reps=0 if failed else state.reps + 1,
            lapses=state.lapses + (1 if failed else 0),
        )


class FsrsPolicy:
    """FSRS-6(py-fsrs;PyPI 上的包名其实是 `fsrs`)。"""

    name = ENGINE_FSRS

    def __init__(
        self,
        *,
        desired_retention: float = DEFAULT_RETENTION,
        maximum_interval: int = MAX_INTERVAL_DAYS,
        tzid: str = DEFAULT_TZID,
    ) -> None:
        from fsrs import Scheduler

        self.tzid = tzid
        # ⚠️ 不传 parameters:第一年不碰 optimizer(坑之五)
        #
        # learning_steps=() 是**刻意的**:FSRS 默认的学习步是 1 分钟 / 10 分钟,
        # 而错题本是**一天看一次**的东西 —— 实测不关掉的话,卡片会一直卡在"10 分钟后再见"。
        # 关掉之后:Good -> 2 天 -> 12 天 -> 51 天;Again -> Relearning(10 分钟,
        # 再由 local_due_date 折叠成"明天")。这才是孩子端该有的节奏。
        self.scheduler = Scheduler(
            desired_retention=desired_retention,
            maximum_interval=maximum_interval,
            learning_steps=(),
        )

    def review(self, state: UnitState, rating: int, now: datetime) -> ReviewResult:
        from fsrs import Card, Rating, State

        if rating not in RATING_LABELS:
            raise ValueError("评分只能是 1..4")

        if state.state == "new" and state.reps == 0:
            card = Card()
        else:
            last_review = (
                datetime.fromisoformat(state.last_review_utc) if state.last_review_utc else None
            )
            card = Card(
                state=_to_fsrs_state(state.state, State),
                step=state.step,
                stability=state.stability or None,
                difficulty=state.difficulty or None,
                due=datetime.fromisoformat(state.due_utc) if state.due_utc else None,
                last_review=last_review,
            )
        reviewed, _log = self.scheduler.review_card(
            card, _to_fsrs_rating(rating, Rating), review_datetime=now.astimezone(timezone.utc)
        )
        # 保留 step:将来若要启用学习步,没有它卡片会永远卡在第一步(实测过)
        due = reviewed.due
        interval = (due - now.astimezone(timezone.utc)).total_seconds() / 86400.0
        return ReviewResult(
            due_utc=due.astimezone(timezone.utc).isoformat(timespec="seconds"),
            due_date_local=local_due_date(due, now, self.tzid),
            state=_from_fsrs_state(reviewed.state),
            stability=float(reviewed.stability or 0.0),
            difficulty=float(reviewed.difficulty or 0.0),
            interval_days=interval,
            reps=state.reps + 1,
            lapses=state.lapses + (1 if rating == RATING_AGAIN else 0),
        )


def _to_fsrs_rating(rating: int, enum):
    return {
        1: enum.Again,
        2: enum.Hard,
        3: enum.Good,
        4: enum.Easy,
    }[rating]


def _to_fsrs_state(state: str, enum):
    return {
        "new": enum.Learning,
        "learning": enum.Learning,
        "review": enum.Review,
        "relearning": enum.Relearning,
    }.get(state, enum.Learning)


def _from_fsrs_state(state) -> str:
    return {
        "Learning": "learning",
        "Review": "review",
        "Relearning": "relearning",
    }.get(getattr(state, "name", ""), "review")


def build_policy(engine: str = ENGINE_FIXED, *, tzid: str = DEFAULT_TZID) -> SchedulingPolicy:
    normalized = (engine or ENGINE_FIXED).strip().lower()
    if normalized == ENGINE_FSRS:
        return FsrsPolicy(tzid=tzid)
    return FixedIntervalPolicy(tzid=tzid)
