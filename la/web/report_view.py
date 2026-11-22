"""使用报表(/report):把事件日志里那些事实,按**时段**摊成一张能对照的账。

⭐ 用户 2026-10-16 的原话:
   *"在轨迹页多加一个子页面,可以看使用报表,按天、周、月、年、或者自定义时间区段,"
   *查看产生的对话轮次,输入的 token,多少命中,多少未命中,缓存命中率。"
   *"还有工具和 skill 的调用情况,都调用了多少次,成功率是多少"*

两个数据来源(都是**已经落下的事实**,这一页不写任何东西):

  1. `events` 表的 `llm/usage` —— 每次模型调用的 token 账(token / 命中 / 未命中);
     它是 D136 新加的:在这之前 usage 只在内存里(`session.total_usage`)与实时事件里,
     **从不落盘**,所以旧数据没有这一行 —— 页面会如实说这一段时间里有多少次调用有账,
     不拿 0 冒充「没有」(与轨迹页「旧记录」同一条口径)。
  2. `events` 表的 `tool/result` —— 每次工具调用的名字、成功与否、错误码;
     `skill` 工具额外带 `skill`(技能名)与 `skill_action`(list/load)。
  3. `events` 表的 `web/usage` —— **联网的请求账**(D148):Search / Fetch 按成功请求次数
     计费(5 元 / 1000 次 = 0.005 元/次),它**不在 token 账里**,所以必须单独记、单独报。

价格口径(2026-10-18 对齐官方价目表):
  * token 分**高峰 / 空闲**两档 —— 北京时间周一至周五 9:00-12:00、14:00-18:00 是高峰,
    其余(含周末)是空闲,空闲**半价**。所以钱要按**每条事件自己的时刻**算,不能拿总量乘一个价;
  * ⚠️ **法定节假日没单独扣**:那天我们按工作日高峰计价 → 会略微**高估**。
    这条写在页面的价格说明里,不藏着。

⚠️ 三条口径,别混(它们与轨迹页/记忆页同一条纪律):
  * **轮次**数的是 `user/message`(用户问了才算一轮),不是 turn/end(那一轮被中止就没有);
  * **缓存命中率**只在 `llm/usage` 里有账的调用上算:命中 ÷ 输入 token ——
    分母用 prompt_tokens(不是 total),因为命中与未命中都属于输入;
  * **工具成功率**只算模型自己发起的调用(`via=model`);
    框架内部调用的(query_summary_pool / spawn_sub_agents / todo_write …)单独一栏列出来,
    混进成功率里会让「这个工具好不好用」这件事失真。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import config

#: 时段粒度(白名单。认不出来的一律回「按天」—— 与轨迹页的 tab 收敛同一条纪律)
PERIOD_DAY = "day"
PERIOD_WEEK = "week"
PERIOD_MONTH = "month"
PERIOD_YEAR = "year"
PERIOD_CUSTOM = "custom"
PERIODS: tuple[str, ...] = (PERIOD_DAY, PERIOD_WEEK, PERIOD_MONTH, PERIOD_YEAR, PERIOD_CUSTOM)
PERIOD_LABELS: dict[str, str] = {
    PERIOD_DAY: "按天",
    PERIOD_WEEK: "按周",
    PERIOD_MONTH: "按月",
    PERIOD_YEAR: "按年",
    PERIOD_CUSTOM: "自定义",
}

#: 不选自定义时,往前看多少天(默认 7 天:与「按天」这张表最常见的用法一致)。
DEFAULT_RANGE_DAYS = 7
#: 自定义区间缺一头时,另一头往前/往后补多少天。
DEFAULT_DAYS_PERIOD = 30
#: 表里最多列多少个时段(天粒度一年 365 行) —— 超了砍最旧的。
MAX_ROWS = 400
#: ⚠️ **框架自己发起的工具**:它们的 `tool/result` 没有 `via` 字段(不是模型调的),
#: 但**老记录里也缺 `via`**(那几处 append 是后来才补的)。所以判"内部"要按名字兜一道 ——
#: 否则 `spawn_sub_agents` / `query_summary_pool` 会在工具表里被列成「未调用」,
#: 而它们其实一直在跑(只是不是模型选的)。名单的唯一口径在 `session.INTERNAL_TOOLS`。
#: 复现:`_research/probe_report.py`(真实库里这三条都只有 phase/count,没有 via)。
INTERNAL_TOOL_NAMES: frozenset[str] = frozenset({
    "query_summary_pool",
    "query_timestamp_range",
    "spawn_sub_agents",
    "todo_write",
    "goal",
    "schedule",
})

#: 工具表的两种排序(用户 2026-10-16:*"我可以通过成功率进行工具的优化"*):
#: 按调用次数(默认,看谁在干活)或按**失败次数**(挑该修的工具)。
SORT_CALLS = "calls"
SORT_FAILED = "failed"
SORTS: tuple[str, ...] = (SORT_CALLS, SORT_FAILED)

#: 明细表列多少行。⚠️ **工具表不再用它** —— 用户 2026-10-16:
#:   *"工具要求全列,我可以通过成功率进行工具的优化"*:要拿成功率挑工具,就不能只给前 20 个。
#: 现在工具表 = **注册表全集**(一次都没调过的也在,成功率显示「未调用」),
#: 只保留这个上限给「框架内部调用」那一栏(它只是背景信息,不是优化的对象)。
MAX_ITEMS = 20

#: 价格(元 / 百万 token;DeepSeek V4.1-Flash,口径见 docs/压缩档位与成本对照.md)。
#: ⚠️ 它是**估算**:真实账单以服务端为准,这里回答的是「这一段时间大概花了多少、缓存省了多少」。
#: ⭐ 2026-10-18(D148)起按**官方两档价**算:高峰 = 下面这组,空闲**半价**。
#:    这不是把数字拍小 —— 之前只按高峰算,而高峰只占工作日的 6 小时,等于多数时候高估近一倍。
PRICE_INPUT_MISS_PER_M = 2.00
PRICE_INPUT_HIT_PER_M = 0.04
PRICE_OUTPUT_PER_M = 8.00
#: 空闲时段(北京时间):命中 0.02 / 未命中 1 / 输出 4 元每百万 token。
PRICE_IDLE_INPUT_MISS_PER_M = 1.00
PRICE_IDLE_INPUT_HIT_PER_M = 0.02
PRICE_IDLE_OUTPUT_PER_M = 4.00
#: 高峰时段(北京时间,仅工作日):[起, 止) 按小时计。
PEAK_WINDOWS: tuple[tuple[int, int], ...] = ((9, 12), (14, 18))
TIER_PEAK = "peak"
TIER_IDLE = "idle"
#: 时区:事件里带 +08:00,但**判高峰必须换成北京时间**再比(不靠本机时区)。
PRICE_TZ = ZoneInfo("Asia/Shanghai")
#: 联网:DeepSeek 官方 Search / Fetch,每 1,000 次**成功**请求 5 元。
WEB_FEE_PER_REQUEST = 0.005
#: 命中单价与未命中单价之比(两档都是 1/50)—— 用它算「不做缓存要多少钱」做对照。
CACHE_PRICE_RATIO = PRICE_INPUT_HIT_PER_M / PRICE_INPUT_MISS_PER_M


def price_tier(moment: datetime) -> str:
    """这一刻按**高峰**还是**空闲**计价。

    ⚠️ 法定节假日没有单独扣:那天按工作日算(高峰),方向是**高估**。
       要精确就得维护一张节假日表 —— 那是一条会烂的数据,不如把话说在页面上。
    """
    local = moment.astimezone(PRICE_TZ) if moment.tzinfo is not None else moment
    if local.weekday() >= 5:            # 周六日 = 空闲
        return TIER_IDLE
    hour = local.hour + local.minute / 60.0
    for start, end in PEAK_WINDOWS:
        if start <= hour < end:
            return TIER_PEAK
    return TIER_IDLE


def usage_cost(
    *, prompt_tokens: int, completion_tokens: int, hit_tokens: int, miss_tokens: int,
    tier: str = TIER_PEAK,
) -> tuple[float, float]:
    """这一次调用花了多少、(不做缓存)本该花多少 —— 单位元。

    ⚠️ 命中/未命中的 token 由调用方给(它们与 prompt_tokens 的关系是
       hit + miss = prompt;**别在这里重新推算**,否则两处口径会漂)。
    """
    if tier == TIER_IDLE:
        miss_price, hit_price, out_price = (
            PRICE_IDLE_INPUT_MISS_PER_M, PRICE_IDLE_INPUT_HIT_PER_M, PRICE_IDLE_OUTPUT_PER_M,
        )
    else:
        miss_price, hit_price, out_price = (
            PRICE_INPUT_MISS_PER_M, PRICE_INPUT_HIT_PER_M, PRICE_OUTPUT_PER_M,
        )
    counted = int(hit_tokens) + int(miss_tokens)
    spend = (int(hit_tokens) * hit_price + int(miss_tokens) * miss_price) / 1_000_000.0
    plain = (counted * miss_price) / 1_000_000.0
    spend += int(completion_tokens) * out_price / 1_000_000.0
    plain += int(completion_tokens) * out_price / 1_000_000.0
    return spend, plain


@dataclass
class MetricRow:
    """顶部一张卡片:值 + 一句**算出来的口径**。"""

    key: str
    value: str
    note: str = ""


@dataclass
class PeriodRow:
    """一个时段(一天 / 一周 / 一月 / 一年)的一行账。"""

    key: str
    label: str
    start: str
    end: str = ""
    asks: int = 0
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    usage_calls: int = 0
    #: token 花掉的钱(按每条事件自己的**高峰/空闲**档算,见 price_tier)
    cost_tokens: float = 0.0
    #: 同一批调用"不做缓存"本该花多少(算「缓存省下」用)
    cost_plain: float = 0.0
    #: 联网:成功计费的 Search / Fetch 请求数(D148 起有账)
    search_calls: int = 0
    fetch_calls: int = 0

    @property
    def cache_rate(self) -> float:
        total = self.cache_hit_tokens + self.cache_miss_tokens
        return (self.cache_hit_tokens / total) if total else 0.0

    @property
    def web_calls(self) -> int:
        return self.search_calls + self.fetch_calls

    @property
    def web_cost(self) -> float:
        return self.web_calls * WEB_FEE_PER_REQUEST

    @property
    def cost_total(self) -> float:
        return self.cost_tokens + self.web_cost


@dataclass
class ItemRow:
    """工具 / 技能的一行:调了多少次、成功多少次、成功率。"""

    name: str
    calls: int = 0
    ok: int = 0
    failed: int = 0
    note: str = ""

    @property
    def rate(self) -> float:
        return (self.ok / self.calls) if self.calls else 0.0


@dataclass
class UsageReport:
    """整页的形状。**纯数据**:模板不在这里,免得两边各写一遍口径。"""

    generated_at: str = ""
    period: str = PERIOD_DAY
    range_start: str = ""
    range_end: str = ""
    range_label: str = ""
    custom_start: str = ""
    custom_end: str = ""
    rows: list[PeriodRow] = field(default_factory=list)
    totals: PeriodRow = field(
        default_factory=lambda: PeriodRow(key="total", label="合计", start="")
    )
    total_rows: int = 0
    truncated: bool = False
    tools: list[ItemRow] = field(default_factory=list)
    skills: list[ItemRow] = field(default_factory=list)
    internal_tools: list[ItemRow] = field(default_factory=list)
    metric_rows: list[MetricRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    usage_available: bool = False
    #: 当前注册表里有多少个工具(0 = 这一次没读到注册表,页面要如实说)
    catalog_size: int = 0
    #: 注册了但这一段时间**一次都没调过**的工具名(按名字排)—— 优化时先看这一串
    never_called: list[str] = field(default_factory=list)
    #: 只在历史事件里出现过的旧工具名(现在的注册表里没有)—— 单独一栏,别混进成功率
    tools_retired: list[ItemRow] = field(default_factory=list)
    #: 注册表里"由框架自己发起"的那几个(不在工具表里,但在 schema 开销里)—— 页面要列出来
    internal_catalog: list[str] = field(default_factory=list)
    #: 工具表的排序口径(calls / failed)
    sort: str = SORT_CALLS
    usage_first_at: str = ""
    usage_months: list[str] = field(default_factory=list)
    events_total: int = 0
    events_first_at: str = ""
    price_note: str = ""


# ---------------------------------------------------------------- 时段


def _parse_day(value: Any) -> date | None:
    """`YYYY-MM-DD` 或 `YYYY-MM-DDTHH:MM(:SS)` -> date。认不出来返回 None(**不猜**)。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_moment(value: Any) -> datetime | None:
    """事件里的 `time`(ISO8601,带 +08:00)→ datetime。认不出来返回 None(**不猜**)。

    ⚠️ 判高峰用得到**时刻**(不只是日期),所以不能退回 _parse_day。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _fmt(value: date) -> str:
    return value.isoformat()


def _week_start(value: date) -> date:
    """这一周的周一(ISO 周:周一是一周的第一天)。"""
    return value - timedelta(days=value.weekday())


def _period_range(
    period: str,
    *,
    now: datetime,
    start: str = "",
    end: str = "",
) -> tuple[date, date]:
    """把时段翻译成**左右都含**的日期区间(页面按 [start, end+1) 去查)。

    ⚠️ 自定义区间的写法与轨迹页的表单同一条口径:**认不出来就用默认**,
       不报错、也不把页面打成空白(手打的链接、书签都要能开)。
    """
    today = now.date()
    if period == PERIOD_CUSTOM:
        first = _parse_day(start)
        last = _parse_day(end)
        if first is None and last is None:
            return today - timedelta(days=DEFAULT_DAYS_PERIOD - 1), today
        if first is None:
            first = (last or today) - timedelta(days=DEFAULT_DAYS_PERIOD - 1)
        if last is None:
            last = today
        if last < first:
            first, last = last, first
        return first, last
    return today - timedelta(days=DEFAULT_RANGE_DAYS - 1), today


def _bucket_of(day: date, period: str) -> tuple[str, str, str]:
    """一条事件的时间 -> (key, label, 这一格的起始日)。"""
    if period == PERIOD_WEEK:
        first = _week_start(day)
        return _fmt(first), _fmt(first) + " 那周", _fmt(first)
    if period == PERIOD_MONTH:
        key = day.strftime("%Y-%m")
        return key, key, key + "-01"
    if period == PERIOD_YEAR:
        key = day.strftime("%Y")
        return key, key + " 年", key + "-01-01"
    key = _fmt(day)
    return key, key, key


def _empty_buckets(period: str, first: date, last: date) -> list[PeriodRow]:
    """把区间里**每一格**都先摆上 —— 没有事件的时段也占一行(0 与「没有这一格」是两回事)。"""
    rows: list[PeriodRow] = []
    seen: set[str] = set()
    day = first
    guard = 0
    while day <= last and guard < 4000:
        guard += 1
        key, label, start_day = _bucket_of(day, period)
        if key not in seen:
            seen.add(key)
            rows.append(PeriodRow(key=key, label=label, start=start_day))
        if period == PERIOD_WEEK:
            day = _week_start(day) + timedelta(days=7)
        elif period == PERIOD_MONTH:
            day = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
        elif period == PERIOD_YEAR:
            day = day.replace(year=day.year + 1, month=1, day=1)
        else:
            day = day + timedelta(days=1)
    return rows


# ---------------------------------------------------------------- 聚合


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _rate_text(value: float) -> str:
    return "%.1f%%" % (100.0 * value)


def _money(value: float) -> str:
    return "￥%.4f" % value


def _json(value: Any) -> dict[str, Any]:
    """事件 data 是 TEXT。坏 JSON 当没有(与轨迹页读坏行同一条口径)。"""
    try:
        payload = json.loads(str(value or "{}"))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _sort_key(sort: str):
    """排序口径:**按调用次数**(默认)或**按失败次数**。

    ⭐ 为什么要两种:挑"值得优化"的工具时,看的是**失败次数**而不是调用次数 ——
       一个调了 3 次失败 3 次的工具,比一个调了 20 次全成功的更该被看见。
    两种都按名字收尾,保证顺序**稳定**(同样的数据每次渲染都一样)。
    """
    if sort == SORT_FAILED:
        return lambda item: (-item.failed, -item.calls, item.name)
    return lambda item: (-item.calls, item.name)


def _item_rows(
    counter: Counter,
    *,
    limit: int | None = MAX_ITEMS,
    catalog: tuple[str, ...] | list[str] | None = None,
    sort: str = SORT_CALLS,
) -> list[ItemRow]:
    """Counter[(名字, ok)] -> 排好序的行。调用次数多的在前,同数按名字。

    `catalog` 给了就**并进一张全集表**:名单里有、这段时间没调过的名字也占一行(calls=0)。
    传 `limit=None` 表示全列(工具表就是这么用的)。
    """
    grouped: dict[str, ItemRow] = {}
    for name in catalog or ():
        grouped.setdefault(str(name), ItemRow(name=str(name)))
    for (name, ok), count in counter.items():
        item = grouped.get(name)
        if item is None:
            item = ItemRow(name=name)
            grouped[name] = item
        item.calls += count
        if ok:
            item.ok += count
        else:
            item.failed += count
    rows = sorted(grouped.values(), key=_sort_key(sort))
    return rows[:limit]


def _load_events(
    conn: sqlite3.Connection, start_text: str, end_text: str
) -> list[sqlite3.Row]:
    """按时间范围读事件(左闭右开,字符串比较 —— 与 store.query_range 同一条不变量)。"""
    sql = (
        "SELECT session_id, seq, type, time, data FROM events"
        " WHERE time >= ? AND time < ? ORDER BY time, seq"
    )
    return list(conn.execute(sql, (start_text, end_text)).fetchall())


def _coverage(conn: sqlite3.Connection) -> tuple[bool, str, list[str]]:
    """这一份库里有没有 token 账?有账的最早时间?哪几个年月有账?(如实说,不猜)"""
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(time) AS t0 FROM events WHERE type = 'llm/usage'"
    ).fetchone()
    count = int(row["n"] or 0) if row is not None else 0
    first = str(row["t0"] or "") if row is not None else ""
    months: list[str] = []
    if count:
        for item in conn.execute(
            "SELECT DISTINCT substr(time, 1, 7) AS m FROM events"
            " WHERE type = 'llm/usage' ORDER BY m"
        ).fetchall():
            months.append(str(item["m"]))
    return bool(count), first, months


def build_usage_report(
    conn: sqlite3.Connection,
    *,
    period: str = PERIOD_DAY,
    start: str = "",
    end: str = "",
    now: datetime | None = None,
    now_text: str = "",
    item_limit: int = MAX_ITEMS,
    catalog: tuple[str, ...] | list[str] | None = None,
    sort: str = SORT_CALLS,
) -> UsageReport:
    """把这一页要的一切算出来。**纯读** —— 不写库、不调模型。"""
    # 与全项目同一口径(配置时区的现在),不要退回 OS 本地时区。
    moment = now or config.get_settings().now()
    wanted = period if period in PERIODS else PERIOD_DAY
    report = UsageReport(
        generated_at=now_text or moment.strftime("%Y-%m-%d %H:%M"),
        period=wanted,
        price_note=(
            "token 价按**高峰 / 空闲**两档算:高峰(工作日 9:00-12:00、14:00-18:00,北京时间)"
            "命中 %.2f / 未命中 %.2f / 输出 %.2f 元每百万,空闲**减半**;"
            "联网 Search / Fetch 每次成功请求 %.3f 元(与 token 分开记)。"
            "⚠️ 法定节假日未单独扣(按工作日高峰算,略高估)。"
            % (PRICE_INPUT_HIT_PER_M, PRICE_INPUT_MISS_PER_M, PRICE_OUTPUT_PER_M,
               WEB_FEE_PER_REQUEST)
        ),
    )
    first_day, last_day = _period_range(wanted, now=moment, start=start, end=end)
    report.custom_start = start
    report.custom_end = end
    all_buckets = _empty_buckets(wanted, first_day, last_day)
    report.range_start = _fmt(first_day)
    report.range_end = _fmt(last_day)
    report.range_label = _fmt(first_day) + " → " + _fmt(last_day)
    by_key = {row.key: row for row in all_buckets}
    totals = PeriodRow(
        key="total", label="合计", start=_fmt(first_day), end=_fmt(last_day)
    )
    tool_calls: Counter = Counter()
    internal_calls: Counter = Counter()
    skill_calls: Counter = Counter()

    end_text = _fmt(last_day + timedelta(days=1))
    for event in _load_events(conn, _fmt(first_day), end_text):
        day = _parse_day(event["time"])
        if day is None:
            continue
        key, _label, _start = _bucket_of(day, wanted)
        row = by_key.get(key)
        kind = str(event["type"])
        data = _json(event["data"])
        if kind == "user/message":
            if row is not None:
                row.asks += 1
            totals.asks += 1
        elif kind == "llm/usage":
            targets = [totals] + ([row] if row is not None else [])
            hit = _int(data.get("cache_hit_tokens"))
            miss = _int(data.get("cache_miss_tokens"))
            completion = _int(data.get("completion_tokens"))
            # ⭐ D148:钱按**这条事件自己的时刻**所在的档位算(高峰/空闲半价)。
            moment = _parse_moment(event["time"])
            tier = price_tier(moment) if moment is not None else TIER_PEAK
            spend, plain = usage_cost(
                prompt_tokens=_int(data.get("prompt_tokens")),
                completion_tokens=completion,
                hit_tokens=hit, miss_tokens=miss, tier=tier,
            )
            for item in targets:
                item.calls += 1
                item.usage_calls += 1
                item.prompt_tokens += _int(data.get("prompt_tokens"))
                item.completion_tokens += completion
                item.cache_hit_tokens += hit
                item.cache_miss_tokens += miss
                item.cost_tokens += spend
                item.cost_plain += plain
        elif kind == "web/usage":
            # 官方口径:只有"成功且产生结果"的请求才计费 —— 记账那一层已经把
            # billable 判好了,这里**只认它**,不自己再推一遍。
            if not bool(data.get("billable")):
                continue
            requests = max(1, _int(data.get("requests")) or 1)
            kind_name = str(data.get("kind") or "")
            targets = [totals] + ([row] if row is not None else [])
            for item in targets:
                if kind_name == "fetch":
                    item.fetch_calls += requests
                else:
                    item.search_calls += requests
        elif kind == "tool/result":
            name = str(data.get("tool") or "?")
            ok = bool(data.get("ok"))
            if str(data.get("via") or "") == "model" and name not in INTERNAL_TOOL_NAMES:
                tool_calls[(name, ok)] += 1
                if name == "skill":
                    skill_calls[(str(data.get("skill") or "(没给名字)"), ok)] += 1
            else:
                internal_calls[(name, ok)] += 1

    kept = all_buckets[-MAX_ROWS:] if len(all_buckets) > MAX_ROWS else all_buckets
    report.rows = kept
    report.total_rows = len(all_buckets)
    report.truncated = len(kept) < len(all_buckets)
    report.totals = totals
    report.events_total = totals.asks
    # ⭐ 用户 2026-10-16:*"工具要求全列,我可以通过成功率进行工具的优化"* ——
    #    所以工具表 = **注册表全集 ∪ 这段时间调过的工具**,一次都没调过的排在最前(0 次,按名字)。
    #    ⚠️ 为什么"没调过"也算一条:看不到它才会一直以为它在干活;
    #       而"某工具从没被调用"本身就是最该知道的一条优化信息。
    report.sort = sort if sort in SORTS else SORT_CALLS
    names = [str(name) for name in (catalog or ())]
    report.catalog_size = len(names)
    report.internal_catalog = sorted(name for name in names if name in INTERNAL_TOOL_NAMES)
    # ⚠️ 注册表里有几个工具是**框架自己发起**的(query_summary_pool / spawn_sub_agents …):
    #    它们不在这张"模型选的工具"表里(否则永远显示「未调用」,而其实一直在跑)。
    #    这一栏**不删**,只在页面那一栏里单独说明 —— "工具 schema 每一轮都要付钱"这件事
    #    对它们同样成立,藏起来反而会让人以为工具没那么多。
    model_facing = [name for name in names if name not in INTERNAL_TOOL_NAMES]
    catalog_set = set(model_facing)
    report.tools = _item_rows(tool_calls, limit=None, catalog=model_facing, sort=report.sort)
    # ⚠️ **两个名单分开**(2026-10-16 工具合并之后实测过:全表 53 行,而注册表只有 35 个):
    #    * `tools` = **现在还在注册表里**的工具(用户要按它挑工具,一个都不能少);
    #    * `tools_retired` = 只在历史事件里出现过的旧名字(批次 A/B 合并前的 skill_load 等),切到
    #      "按失败次数"排时它们会浮到最上面,看起来像"当前工具在失败" —— 而它们其实已经不存在了。
    #    名单为空(没读到注册表)时就**不拆**:那时唯一的名单就是事件里那些。
    if catalog_set:
        report.tools_retired = [item for item in report.tools if item.name not in catalog_set]
        report.tools = [item for item in report.tools if item.name in catalog_set]
    report.never_called = sorted(item.name for item in report.tools if not item.calls)
    report.skills = _item_rows(skill_calls, limit=item_limit)
    report.internal_tools = _item_rows(internal_calls, limit=item_limit)
    report.usage_available, report.usage_first_at, report.usage_months = _coverage(conn)
    report.metric_rows = _metric_rows(totals, first_day, last_day)
    report.notes = _notes(report, wanted, item_limit)
    return report


def _metric_rows(
    totals: PeriodRow, first: date, last: date
) -> list[MetricRow]:
    """顶部五张卡片。**每个数都带口径说明** —— 这几个数最容易被混着看。"""
    # ⭐ D148:钱是**逐条事件**按高峰/空闲算好再加起来的(见 usage_cost),
    #    这里不再拿总量乘单价 —— 那样会把空闲时说成高峰价,差近一倍。
    spent = totals.cost_tokens
    plain = totals.cost_plain or spent
    saved = ((plain - spent) / plain) if plain > 0 else 0.0
    span = (last - first).days + 1
    per_day = (totals.asks / float(span)) if span > 0 else 0.0
    cache_note = (
        "命中 %d / 未命中 %d —— 只统计有账的 %d 次调用"
        % (totals.cache_hit_tokens, totals.cache_miss_tokens, totals.usage_calls)
    )
    return [
        MetricRow(
            key="对话轮次",
            value=str(totals.asks),
            note=("用户发来的消息条数(user/message)—— 平均每天 %.1f 轮" % per_day),
        ),
        MetricRow(
            key="输入 token",
            value=str(totals.prompt_tokens),
            note="这一段里**每一次模型调用**的输入之和(含工具往返,不是一轮一次)",
        ),
        MetricRow(key="缓存命中率", value=_rate_text(totals.cache_rate), note=cache_note),
        MetricRow(
            key="缓存省下",
            value=_rate_text(saved),
            note=("估算 %s;不做缓存要 %s(按高峰/空闲分档算)"
                  % (_money(spent), _money(plain))),
        ),
        MetricRow(
            key="联网花费",
            value=_money(totals.web_cost),
            note=("搜索 %d 次 · 抓取 %d 次 —— 每次成功请求 0.005 元,"
                  "这笔钱不在 token 账里(D148 起才有账)"
                  % (totals.search_calls, totals.fetch_calls)),
        ),
    ]


def _notes(report: UsageReport, period: str, item_limit: int) -> list[str]:
    notes: list[str] = []
    if not report.usage_available:
        notes.append(
            "⚠️ 这一份库里**一条 token 账都没有**(llm/usage 事件为零)—— 报表里的 token"
            " 与命中率会是 0,而**轮次与工具调用是真实存在的**。原因是这一层直到"
            " 2026-10-16(D136)才落盘:在那之前的调用没留下这一行。"
        )
    else:
        notes.append(
            "token 账从 " + report.usage_first_at + " 起才有(D136 之前不落盘);"
            "更早的对话只数得到轮次与工具调用。"
        )
    if report.totals.web_calls:
        notes.append(
            "联网花费 = **成功请求次数 × 0.005 元**(Search / Fetch 各 5 元 / 1000 次)。"
            "失败、无结果、超时的请求**不计费**,所以它比「联网工具被调了几次」小 —— "
            "两者看的是不同的事。"
        )
    if period == PERIOD_CUSTOM:
        notes.append(
            "自定义区间看的是 " + report.range_start + " 到 " + report.range_end
            + "(含首尾两天);区间里每一格都会列出来,没有对话的那格是 0。"
        )
    if report.truncated:
        notes.append(
            "时段太多,表里只列最近的 " + str(len(report.rows)) + " 格(共 "
            + str(report.total_rows) + " 格);把时间范围收窄一点。"
        )
    # ⭐ 工具表的口径(D136 补):**全列** —— 用户要拿成功率挑工具,
    #    只给"前 20 个"等于把"从没调过"和"调得少"的工具一起藏起来了。
    #    名单来自会话的注册表(catalog),所以"注册了但一次都没调"也看得见。
    catalog_note = (
        "工具表列出**全部工具**(含一次都没调过的) —— 成功率按这个挑工具;"
        "已下线的旧工具名单列在最下面,不混进来。"
    )
    if not report.catalog_size:
        catalog_note = (
            "⚠️ 这一次没能读到当前注册表,工具表只列出**事件日志里出现过的**那些;"
            "「注册了但从没调过」的工具要等一次带注册表的读取才能显示。"
        )
    notes.append(catalog_note)
    notes.append(
        "框架内部调用"
        " (query_summary_pool / spawn_sub_agents 等)单列,不计入成功率。"
    )
    return notes


__all__ = [
    "CACHE_PRICE_RATIO",
    "TIER_IDLE",
    "TIER_PEAK",
    "WEB_FEE_PER_REQUEST",
    "price_tier",
    "usage_cost",
    "DEFAULT_DAYS_PERIOD",
    "DEFAULT_RANGE_DAYS",
    "ItemRow",
    "MAX_ITEMS",
    "MAX_ROWS",
    "MetricRow",
    "PERIODS",
    "PERIOD_CUSTOM",
    "PERIOD_DAY",
    "PERIOD_LABELS",
    "PERIOD_MONTH",
    "PERIOD_WEEK",
    "PERIOD_YEAR",
    "PeriodRow",
    "UsageReport",
    "build_usage_report",
]
