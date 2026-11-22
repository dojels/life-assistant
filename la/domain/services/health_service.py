"""健康服务:记录 + 趋势 + 能量收支。

⭐ 两条设计原则(能力域 §5.3):
  1. **存原始观测,算派生指标** —— EWMA、MET-分钟、BMR/TDEE 全部查询时算,不落库;
  2. **指标用泛化测量表 + 字典** —— 加新指标零成本,不新建表。

⚠️ 一条红线:**不给医疗建议**。这里只做记录与统计,越界的表述由提示层另行约束。
⚠️ 一条诚实:**计数误差是固有的**(条目 ±10-25%、份量 ±20-50%),所以对外一律"约/区间"。
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ... import config
from ...memory.archive import now_iso
from .. import ownership
from ..receipts import DOMAIN_HEALTH, Receipt, ReceiptError, ReceiptLog
from ..health.apple_health import (
    WORKOUT_FALLBACK,
    WORKOUT_TYPES,
    convert_value,
    parse_apple_export,
)
from ..health.metrics import (
    ACTIVITY_LABELS,
    DEFAULT_ALPHA,
    EWMA_LAG_DAYS,
    FORMULA_ERROR_KCAL,
    KCAL_PER_KG_FAT,
    MIN_DAYS_FOR_ESTIMATE,
    EnergyBalance,
    energy_balance,
    ewma_trend,
    intensity_label,
    kcal_from_met,
    met_minutes,
    mifflin_st_jeor,
    tdee,
    tdee_from_history,
    weekly_rate,
)
from ..schemas.health import (
    DAY_METRIC_FIELDS,
    FoodLogCreate,
    HealthGoalCreate,
    HealthSyncPayload,
    MeasurementCreate,
    TrendQuery,
    TrendResult,
    WorkoutCreate,
)

# ⚠️ 单一来源(2026-10-17 审计):这个串原先在 8 个模块各写一份。
#    改 .env 的 LA_TIMEZONE 而这里不动,就会出现"提示词说伦敦、提醒按上海响"。
#    真正决定每个服务用哪个时区的是**构造参数 tzid**(由 build_services 注入 settings.timezone),
#    这里只是"没注入时的兜底"。
DEFAULT_TZID = config.DEFAULT_TIMEZONE

# ⚠️ 红线:不越过"记录与统计"
MEDICAL_DISCLAIMER = (
    "以上只是记录与统计,不构成医疗建议。身体不适或有疾病请咨询医生。"
)
#: 设备同步的来源标记。手动记的是 "user"/"chat",设备来的一律带前缀 ——
#: 撤销一次导入时,靶子就是"这次新建的 device:apple 行"。
APPLE_SOURCE = "device:apple"

#: 「这一天的一个汇总数」的标记。⭐ 它解决一个会静默翻倍的坑:
#: 同一天可能既有导出的逐条记录、又有快捷指令推的当日总数 —— 两个都算,
#: 步数会变成两倍。所以:推当日总数时先删掉同一天的旧记录;**导出的精确记录
#: 反过来会把当日汇总标记删掉**。同一天永远只有一个来源在说话。
DAILY_MARKER = "每日汇总"

#: 「今天」那三个环的参考目标(不是医嘱):步数取 WHO 推荐量级的常见手环值,
#: 活动能量与锻炼分钟取 Apple 活动环的默认值。页面上写明"参考",不当成任务。
RING_TARGETS: tuple[tuple[str, float, str], ...] = (
    ("steps", 8000, "步"),
    ("active_kcal", 400, "千卡"),
    ("exercise_min", 30, "分钟"),
)

#: 同步报文里的指标别名 —— iPhone 快捷指令的写法、苹果的 identifier、中文都认。
#: ⚠️ 认不出来就**跳过并报数**,不猜:错一个指标比少一个指标更难查。
METRIC_ALIASES: dict[str, str] = {
    "stepcount": "steps", "step_count": "steps", "steps": "steps", "步数": "steps",
    "hkquantitytypeidentifierstepcount": "steps",
    "distancewalkingrunning": "distance_km", "distance_km": "distance_km",
    "distance": "distance_km", "步行距离": "distance_km", "距离": "distance_km",
    "hkquantitytypeidentifierdistancewalkingrunning": "distance_km",
    "activeenergyburend": "active_kcal", "activeenergyburned": "active_kcal",
    "active_kcal": "active_kcal", "活动能量": "active_kcal",
    "hkquantitytypeidentifieractiveenergyburend": "active_kcal",
    "hkquantitytypeidentifieractiveenergyburned": "active_kcal",
    "basalenergyburned": "basal_kcal", "basal_kcal": "basal_kcal", "基础能量": "basal_kcal",
    "hkquantitytypeidentifierbasalenergyburned": "basal_kcal",
    "flightsclimbed": "flights", "flights": "flights", "爬楼层": "flights",
    "hkquantitytypeidentifierflightsclimbed": "flights",
    "appleexercisetime": "exercise_min", "exercisetime": "exercise_min",
    "exercise_min": "exercise_min", "锻炼分钟": "exercise_min",
    "hkquantitytypeidentifierappleexercisetime": "exercise_min",
    "applestandtime": "stand_hours", "standtime": "stand_hours",
    "stand_hours": "stand_hours", "站立小时": "stand_hours",
    "hkquantitytypeidentifierapplestandtime": "stand_hours",
    "sleepanalysis": "sleep_hours", "sleep": "sleep_hours", "sleep_hours": "sleep_hours",
    "睡眠": "sleep_hours", "睡眠时长": "sleep_hours",
    "hkcategorytypeidentifiersleepanalysis": "sleep_hours",
    "restingheartrate": "resting_hr", "resting_hr": "resting_hr", "静息心率": "resting_hr",
    "hkquantitytypeidentifierrestingheartrate": "resting_hr",
    "heartratevariabilitysdnn": "hrv", "hrv": "hrv", "心率变异性": "hrv",
    "hkquantitytypeidentifierheartratevariabilitysdnn": "hrv",
    "vo2max": "vo2max", "vo2_max": "vo2max", "最大摄氧量": "vo2max",
    "hkquantitytypeidentifiervo2max": "vo2max",
    "respiratoryrate": "respiratory_rate", "respiratory_rate": "respiratory_rate",
    "呼吸频率": "respiratory_rate",
    "hkquantitytypeidentifierrespiratoryrate": "respiratory_rate",
    "oxygensaturation": "oxygen_saturation", "oxygen_saturation": "oxygen_saturation",
    "血氧": "oxygen_saturation", "hkquantitytypeidentifieroxygensaturation": "oxygen_saturation",
    "heartrate": "heart_rate", "heart_rate": "heart_rate", "心率": "heart_rate",
    "hkquantitytypeidentifierheartrate": "heart_rate",
    "bodymass": "weight", "weight": "weight", "体重": "weight",
    "hkquantitytypeidentifierbodymass": "weight",
    "bodyfatpercentage": "body_fat", "body_fat": "body_fat", "体脂": "body_fat",
    "hkquantitytypeidentifierbodyfatpercentage": "body_fat",
    "waistcircumference": "waist", "waist": "waist", "腰围": "waist",
    "hkquantitytypeidentifierwaistcircumference": "waist",
    "height": "height", "身高": "height", "hkquantitytypeidentifierheight": "height",
    "bloodpressuresystolic": "bp_sys", "bp_sys": "bp_sys", "收缩压": "bp_sys",
    "hkquantitytypeidentifierbloodpressuresystolic": "bp_sys",
    "bloodpressurediastolic": "bp_dia", "bp_dia": "bp_dia", "舒张压": "bp_dia",
    "hkquantitytypeidentifierbloodpressurediastolic": "bp_dia",
}

#: 运动名别名:快捷指令可能直接给中文名,也可能给苹果的类型。
ACTIVITY_ALIASES: dict[str, tuple[str, float]] = {
    key.lower(): value for key, value in WORKOUT_TYPES.items()
}
ACTIVITY_ALIASES.update({
    "running": ("跑步", 8.3), "跑步": ("跑步", 8.3),
    "walking": ("走路", 3.5), "走路": ("走路", 3.5), "步行": ("走路", 3.5),
    "cycling": ("骑车", 6.8), "骑车": ("骑车", 6.8), "骑行": ("骑车", 6.8),
    "swimming": ("游泳", 7.0), "游泳": ("游泳", 7.0),
    "yoga": ("瑜伽", 2.5), "瑜伽": ("瑜伽", 2.5),
    "strength": ("力量训练", 5.0), "力量训练": ("力量训练", 5.0),
    "hiit": ("高强度间歇", 8.0), "高强度间歇": ("高强度间歇", 8.0),
    "hiking": ("爬山", 6.0), "爬山": ("爬山", 6.0),
    # ⭐ 2026-11-21(D188 补记):自重动作。中文名与字典种子对齐(M33),
    #    英文/变体放在这里 —— 第三方 App 推 "PushUp" 时也能落到同一行,
    #    而不是带着一个字典里没有的名字进来(那会退回 4.0)。
    "situps": ("仰卧起坐", 3.8), "sit-ups": ("仰卧起坐", 3.8),
    "situp": ("仰卧起坐", 3.8), "仰卧起坐": ("仰卧起坐", 3.8), "卷腹": ("仰卧起坐", 3.8),
    "pushups": ("俯卧撑", 3.8), "push-ups": ("俯卧撑", 3.8),
    "pushup": ("俯卧撑", 3.8), "俯卧撑": ("俯卧撑", 3.8),
    "squats": ("深蹲", 5.0), "squat": ("深蹲", 5.0), "深蹲": ("深蹲", 5.0),
    "pullups": ("引体向上", 7.5), "pull-ups": ("引体向上", 7.5),
    "引体向上": ("引体向上", 7.5),
    "plank": ("平板支撑", 2.8), "平板支撑": ("平板支撑", 2.8),
})


def workout_amount_text(
    duration_min: float | None, distance_km: float | None, reps: int | None
) -> str:
    """一次运动的「量」写成人话:「30 分钟 · 5.2 公里」/「10 个」。

    ⭐ 2026-11-21(D188):运动现在有两个维度,文案不能只认时长 —— 以前回执写死
    「记运动 X N 分钟」,只记个数的记录会变成「记运动 仰卧起坐 None 分钟」。
    只写**真的记了**的那几项,缺的那项不补(不估算)。
    """
    parts: list[str] = []
    if duration_min is not None:
        parts.append(str(int(round(float(duration_min)))) + " 分钟")
    if distance_km is not None:
        parts.append(("{:.2f}".format(float(distance_km))).rstrip("0").rstrip(".") + " 公里")
    if reps is not None:
        parts.append(str(int(reps)) + " 个")
    return " · ".join(parts)


# ⚠️ 误差必须诚实呈现
MEASUREMENT_CAVEAT = (
    "食物热量与消耗都是估计值:条目本身有 ±10-25% 误差,份量估算 ±20-50%,"
    "累计常超 ±30%。**按周看,不按日看**;最终以体重趋势为准。"
)


#: 合理性区间。⚠️ 设备(以及手滑的快捷指令)会把"昨天"推成 80000 步、
#: 把"睡眠"推成 30 小时 —— 那不是数据,是脏数据。越界的**不写**,并计入 skipped。
PLAUSIBLE_RANGES: dict[str, tuple[float, float]] = {
    "steps": (0, 200_000), "distance_km": (0, 1000),
    "active_kcal": (0, 20_000), "basal_kcal": (0, 20_000),
    "flights": (0, 2000), "exercise_min": (0, 1440), "stand_hours": (0, 24),
    "sleep_hours": (0, 24), "sleep_inbed": (0, 24), "sleep_midpoint": (-24, 48),
    "resting_hr": (20, 250), "heart_rate": (20, 250), "hrv": (1, 500),
    "vo2max": (5, 100), "respiratory_rate": (4, 80), "oxygen_saturation": (50, 100),
    "weight": (1, 500), "body_fat": (1, 80), "waist": (20, 300), "height": (50, 260),
    "bp_sys": (30, 300), "bp_dia": (20, 200),
}


def _sync_public(row: dict[str, Any]) -> dict[str, Any]:
    """同步记录给页面/模型看的那一份。

    ⚠️ ip / user-agent 只用于排障,不该进模型上下文 —— 健康数据本身已经够敏感,
    再顺手把内网 IP 与设备串带出去没有道理。
    """
    return {key: value for key, value in row.items() if key not in ("ip", "ua")}


def _plausible(metric_code: str, value: float) -> bool:
    bounds = PLAUSIBLE_RANGES.get(metric_code)
    if bounds is None:
        return True
    return bounds[0] <= float(value) <= bounds[1]


def normalize_metric(name: str) -> str | None:
    """把快捷指令给的指标名收敛到我们的 code。认不出来返回 None(不猜)。"""
    text = str(name or "").strip()
    if not text:
        return None
    return METRIC_ALIASES.get(text.lower())


def resolve_activity(name: str) -> tuple[str, float]:
    """运动名 -> (中文名, MET)。苹果的 HKWorkoutActivityType 与常见英文名都认。"""
    text = str(name or "").strip()
    hit = WORKOUT_TYPES.get(text) or ACTIVITY_ALIASES.get(text.lower())
    if hit is not None:
        return hit
    return (text or WORKOUT_FALLBACK[0], WORKOUT_FALLBACK[1])


def _circular_spread_hours(values: list[float]) -> float:
    """一天 24 小时是环:23:50 与 00:10 只差 20 分钟,直接求标准差会得到 24 小时。

    用圆统计:先把每个时刻转成角度,算合成长度 R,离散度 = sqrt(-2 ln R) × 24/2π。
    R→1 表示全都落在同一时刻(规律),R→0 表示散在一天各处。
    """
    if not values:
        return 0.0
    sines = sum(math.sin(value / 24.0 * 2 * math.pi) for value in values)
    cosines = sum(math.cos(value / 24.0 * 2 * math.pi) for value in values)
    count = float(len(values))
    length = math.hypot(sines, cosines) / count
    if length <= 1e-9:
        return 12.0
    return math.sqrt(-2.0 * math.log(length)) * 24.0 / (2.0 * math.pi)


class HealthError(RuntimeError):
    """健康域的领域错误。"""


@dataclass
class Profile:
    sex: str
    birth_year: int | None
    height_cm: float | None
    activity_level: str

    def age(self, now: datetime) -> int | None:
        if not self.birth_year:
            return None
        return max(now.year - int(self.birth_year), 1)

    def as_dict(self, now: datetime | None = None) -> dict:
        moment = now or datetime.now(ZoneInfo(DEFAULT_TZID))
        return {
            "sex": self.sex,
            "birth_year": self.birth_year,
            "age": self.age(moment),
            "height_cm": self.height_cm,
            "activity_level": self.activity_level,
            "activity_label": ACTIVITY_LABELS.get(self.activity_level, self.activity_level),
        }


class HealthService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        audit: Callable[..., None] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        tzid: str = DEFAULT_TZID,
        receipts: ReceiptLog | None = None,
        member_id: int | None = None,
    ) -> None:
        self.conn = conn
        self.member_id = member_id
        self._audit = audit
        self._now = now_provider
        self.tzid = tzid
        # 统一写入回执(M7)
        self.receipts = receipts

    def _current_time(self) -> datetime:
        if self._now is not None:
            return self._now()
        return datetime.now(ZoneInfo(self.tzid))

    def _owner_clause(self, column: str = "owner_id") -> tuple[str, list[object]]:
        return ownership.owner_filter(getattr(self, "member_id", None), column)

    def _owner_for_new(self) -> int | None:
        return ownership.owner_for_new(
            self.conn, ownership.DOMAIN_HEALTH, getattr(self, "member_id", None)
        )

    def _stamp_owner(self, table: str, row_id: int, owner: int | None) -> None:
        if owner is not None:
            self.conn.execute(
                "UPDATE " + table + " SET owner_id = ? WHERE id = ?",
                (owner, int(row_id)),
            )

    def _local_date(self, moment: datetime) -> str:
        return moment.astimezone(ZoneInfo(self.tzid)).date().isoformat()

    def _parse(self, text: str | None) -> datetime:
        if not text:
            return self._current_time()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise HealthError("时间格式必须是 ISO8601: " + str(text)) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(self.tzid))
        return parsed

    # -- 档案 -------------------------------------------------------------
    def profile(self) -> Profile:
        row = self.conn.execute("SELECT * FROM app_profiles ORDER BY id LIMIT 1").fetchone()
        return Profile(
            sex=row["sex"], birth_year=row["birth_year"],
            height_cm=row["height_cm"], activity_level=row["activity_level"],
        )

    def update_profile(self, **fields: Any) -> Profile:
        allowed = {"sex", "birth_year", "height_cm", "activity_level"}
        updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
        if updates:
            assignments = ", ".join(key + " = ?" for key in updates)
            self.conn.execute(
                "UPDATE app_profiles SET " + assignments + ", updated_at = ?",
                [*updates.values(), now_iso()],
            )
            self.conn.commit()
        return self.profile()

    def metric_type(self, code: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM app_metric_types WHERE code = ?", (code,)
        ).fetchone()
        if row is None:
            known = [item[0] for item in self.conn.execute(
                "SELECT code FROM app_metric_types ORDER BY id")]
            raise HealthError("未知指标: " + code + ";可用指标:" + "/".join(known))
        return row

    def list_metric_types(self) -> list[dict[str, Any]]:
        return [
            {"code": row["code"], "name": row["name"], "unit": row["unit"],
             "decimals": int(row["decimals"])}
            for row in self.conn.execute("SELECT * FROM app_metric_types ORDER BY id")
        ]

    # -- 测量 -------------------------------------------------------------
    def log_measurement(
        self, payload: MeasurementCreate | dict[str, Any], *, actor: str = "agent",
        session_id: str | None = None, receipt: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(payload, MeasurementCreate):
            payload = MeasurementCreate.model_validate(payload)
        metric = self.metric_type(payload.metric_code)
        moment = self._parse(payload.measured_at)
        # ⚠️ 这是 upsert:同一指标 + 同一时刻 + 同一来源会**覆盖**。所以撤销要能区分
        #    "新建"与"覆盖旧值"——否则一撤销就把用户原来那条记录也删了。
        measured_at = moment.isoformat(timespec="seconds")
        owner = self._owner_for_new()
        existing = self.conn.execute(
            "SELECT id, value, unit, note FROM app_measurements"
            " WHERE metric_code = ? AND measured_at = ?"
            " AND source = ? AND owner_id IS ?",
            (payload.metric_code, measured_at, payload.source, owner),
        ).fetchone()
        self.conn.execute(
            "INSERT INTO app_measurements(metric_code, value, unit, measured_at, local_date,"
            " source, note, created_at, owner_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT DO UPDATE SET"
            " value = excluded.value, note = excluded.note",
            (
                payload.metric_code, float(payload.value), metric["unit"],
                measured_at, self._local_date(moment),
                payload.source, payload.note, now_iso(), owner,
            ),
        )
        row = self.conn.execute(
            "SELECT id FROM app_measurements WHERE metric_code = ? AND measured_at = ?"
            " AND source = ? AND owner_id IS ?",
            (payload.metric_code, measured_at, payload.source, owner),
        ).fetchone()
        if receipt and self.receipts is not None and row is not None:
            amount = round(float(payload.value), int(metric["decimals"]))
            self.receipts.record(
                kind="health.measurement",
                domain=DOMAIN_HEALTH,
                label="记 " + str(metric["name"]) + " " + str(amount) + str(metric["unit"]),
                detail={
                    "id": int(row["id"]),
                    "metric_code": payload.metric_code,
                    "metric_name": metric["name"],
                    "value": float(payload.value),
                    "unit": metric["unit"],
                    "measured_at": measured_at,
                    "op": "overwrite" if existing is not None else "create",
                },
                undo_plan={
                    "type": "row",
                    "table": "app_measurements",
                    "row_id": int(row["id"]),
                    "before": (
                        # value 之外还要还原 unit/note:覆盖一条带备注的测量后撤销,
                        # 旧实现只写回 value,备注永久丢了(2026-10-18 审计)。
                        {"value": float(existing["value"]),
                         "unit": existing["unit"], "note": existing["note"]}
                        if existing is not None else None
                    ),
                },
                actor=actor,
                session_id=session_id,
            )
        self.conn.commit()
        self._emit(
            "health.measurement.logged",
            {"metric": payload.metric_code, "value": float(payload.value)},
            key="health.measurement:" + payload.metric_code + ":"
            + moment.isoformat(timespec="seconds"),
        )
        if self._audit is not None:
            self._audit(
                actor=actor, action="health.measurement.log",
                target="metric:" + payload.metric_code,
                detail={"value": float(payload.value), "source": payload.source},
                session_id=session_id,
            )
            self.conn.commit()
        return {
            "metric_code": payload.metric_code,
            "value": float(payload.value),
            "unit": metric["unit"],
            "measured_at": moment.isoformat(timespec="seconds"),
            "local_date": self._local_date(moment),
            # ⭐ 导入撤销要靠这两项:哪一行、是不是这次**新建**的。
            #    (只按 source='device:apple' 删会把历次导入全删掉,见 revert_import。)
            "id": int(row["id"]) if row is not None else 0,
            "created": existing is None,
        }

    def latest(self, metric_code: str) -> dict[str, Any] | None:
        clause, params = self._owner_clause()
        row = self.conn.execute(
            "SELECT * FROM app_measurements WHERE metric_code = ? AND " + clause
            + " ORDER BY measured_at DESC LIMIT 1",
            (metric_code, *params),
        ).fetchone()
        if row is None:
            return None
        return {"metric_code": row["metric_code"], "value": float(row["value"]),
                "unit": row["unit"], "measured_at": row["measured_at"],
                "local_date": row["local_date"]}

    def _aggregate_rule(self, metric_code: str) -> str:
        """这个指标一天里的多条记录怎么合成一个数。

        ⭐ 规则住在字典(app_metric_types.aggregate),不在代码里 —— 加一个指标
        不需要改这里。默认 avg 是为了让 M6 的老指标(体重/体脂)行为不变。
        """
        row = self.conn.execute(
            "SELECT aggregate FROM app_metric_types WHERE code = ?", (metric_code,)
        ).fetchone()
        if row is None:
            return "avg"
        return str(row["aggregate"] or "avg")

    def daily_values(self, metric_code: str, *, days: int = 90) -> list[tuple[str, float]]:
        """按天聚合。**规则来自字典**:步数是加总,体重是平均,最大摄氧量取最后一条。

        ⚠️ 2026-11-11:以前写死 AVG。iPhone 的步数一天有几十条记录,
        取平均会把「今天 8000 步」显示成「平均每段 60 步」—— 静默错一个量级。
        """
        since = (self._current_time() - timedelta(days=days)).date().isoformat()
        clause, params = self._owner_clause()
        rule = self._aggregate_rule(metric_code)
        if rule == "last":
            rows = self.conn.execute(
                "SELECT local_date, value AS v FROM app_measurements m"
                " WHERE metric_code = ? AND local_date >= ? AND " + clause
                + " AND measured_at = (SELECT MAX(measured_at) FROM app_measurements x"
                "   WHERE x.metric_code = m.metric_code AND x.local_date = m.local_date"
                "   AND x.owner_id IS m.owner_id)"
                " GROUP BY local_date ORDER BY local_date",
                (metric_code, since, *params),
            ).fetchall()
        else:
            expression = {
                "sum": "SUM(value)", "avg": "AVG(value)",
                "min": "MIN(value)", "max": "MAX(value)",
            }.get(rule, "AVG(value)")
            rows = self.conn.execute(
                "SELECT local_date, " + expression + " AS v FROM app_measurements"
                " WHERE metric_code = ? AND local_date >= ? AND " + clause
                + " GROUP BY local_date ORDER BY local_date",
                (metric_code, since, *params),
            ).fetchall()
        return [(row["local_date"], float(row["v"])) for row in rows]

    def recent_workouts(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """最近的运动记录。**带 owner 过滤** —— 页面以前直接查表,家庭版会串数据。"""
        clause, params = self._owner_clause()
        rows = self.conn.execute(
            "SELECT ts, local_date, kind, duration_min, distance_km, reps, kcal, source"
            " FROM app_workouts WHERE " + clause + " ORDER BY ts DESC LIMIT ?",
            (*params, max(1, int(limit))),
        ).fetchall()
        #: 每条都带一句「量」的文案与拆开的数字 —— 页面与模型都用它,
        #: 免得两边各写一份格式化(小数位、单位最容易打架)。
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["amount_text"] = workout_amount_text(
                item["duration_min"], item["distance_km"], item["reps"]
            )
            item["distance_text"] = (
                "" if item["distance_km"] is None
                else "{:.2f}".format(float(item["distance_km"])).rstrip("0").rstrip(".")
            )
            result.append(item)
        return result

    def trend(self, query: TrendQuery | dict[str, Any] | None = None) -> TrendResult:
        if query is None:
            query = TrendQuery()
        if not isinstance(query, TrendQuery):
            query = TrendQuery.model_validate(query)
        metric = self.metric_type(query.metric_code)
        samples = self.daily_values(query.metric_code, days=query.days)
        points = ewma_trend(samples, alpha=DEFAULT_ALPHA)
        rate = weekly_rate(points)
        summary = ""
        if points:
            latest = points[-1]
            summary = (
                "最近一次 " + str(round(latest.value, int(metric["decimals"])))
                + metric["unit"] + ",趋势值 "
                + str(round(latest.trend, int(metric["decimals"]))) + metric["unit"]
            )
            if rate is not None:
                direction = "上升" if rate > 0 else "下降"
                summary += ";" + str(query.days) + " 天里趋势" + direction + "约 "
                summary += str(round(abs(rate), 2)) + metric["unit"] + "/周"
        else:
            summary = "这段时间没有 " + metric["name"] + " 的记录。"
        return TrendResult(
            metric_code=query.metric_code,
            unit=metric["unit"],
            days=query.days,
            points=[point.as_dict() for point in points],
            weekly_rate=round(rate, 3) if rate is not None else None,
            trend_lag_days=EWMA_LAG_DAYS,
            summary=summary + "(趋势线有约 " + str(EWMA_LAG_DAYS) + " 天滞后,属正常)",
        )

    # -- 运动与进食 -------------------------------------------------------
    def log_workout(
        self, payload: WorkoutCreate | dict[str, Any], *, actor: str = "agent",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, WorkoutCreate):
            payload = WorkoutCreate.model_validate(payload)
        row = self.conn.execute(
            "SELECT * FROM app_activity_types WHERE name = ?", (payload.activity,)
        ).fetchone()
        met = float(row["met"]) if row is not None else 4.0
        activity_id = int(row["id"]) if row is not None else None
        moment = self._parse(payload.ts)
        owner = self._owner_for_new()
        # ⭐ 两个维度原样落库:给了什么写什么,缺的那项留 NULL(不估算、不补 0)。
        amount_text = workout_amount_text(
            payload.duration_min, payload.distance_km, payload.reps
        )
        cursor = self.conn.execute(
            "INSERT INTO app_workouts(ts, local_date, kind, activity_id, duration_min,"
            " distance_km, reps, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                moment.isoformat(timespec="seconds"), self._local_date(moment),
                payload.activity, activity_id, payload.duration_min,
                payload.distance_km, payload.reps, payload.note, now_iso(),
            ),
        )
        self._stamp_owner("app_workouts", int(cursor.lastrowid or 0), owner)
        if self.receipts is not None:
            self.receipts.record(
                kind="health.workout",
                domain=DOMAIN_HEALTH,
                label="记运动 " + str(payload.activity) + " " + amount_text,
                detail={"id": int(cursor.lastrowid or 0), "activity": payload.activity,
                        "duration_min": payload.duration_min,
                        "distance_km": payload.distance_km, "reps": payload.reps,
                        "amount_text": amount_text,
                        "local_date": self._local_date(moment)},
                undo_plan={"type": "row", "table": "app_workouts",
                           "row_id": int(cursor.lastrowid or 0), "before": None},
                actor=actor, session_id=session_id,
            )
        self.conn.commit()
        self._emit(
            "health.workout.logged",
            {"activity": payload.activity, "minutes": payload.duration_min,
             "distance_km": payload.distance_km, "reps": payload.reps},
            key="health.workout:" + uuid.uuid4().hex[:12],
        )
        result = {
            "activity": payload.activity,
            "duration_min": payload.duration_min,
            "distance_km": payload.distance_km,
            "reps": payload.reps,
            "amount_text": amount_text,
            "met": met,
            "intensity": intensity_label(met),
            "local_date": self._local_date(moment),
            # ⭐ 没有时长就没有 MET-分钟(它是 MET × 分钟)。给 None 而不是 0 ——
            #    0 会被跨类型累加当成"做了但没消耗",两者不是一回事。
            "met_minutes": (
                round(met_minutes(met, payload.duration_min), 1)
                if payload.duration_min is not None else None
            ),
        }
        if payload.duration_min is not None:
            weight = self.latest("weight")
            if weight is not None:
                result["kcal_estimate"] = round(
                    kcal_from_met(met, payload.duration_min, float(weight["value"]))
                )
                result["kcal_note"] = "约值,按最近一次体重估算"
        else:
            # ⚠️ 不按个数/距离反推时长 —— 那是编一个数出来(用户明确说过不要估算)。
            result["calorie_note"] = (
                "这次没记时长,所以没算消耗 —— 不拿个数/距离反推时长。"
                "想让它进能量收支,下次顺手说一句做了多久就行。"
            )
        return result

    # -- 撤销(由统一回执调用)--------------------------------------------
    def revert_row(self, receipt: Receipt) -> str:
        """删掉刚写的那一行;若是"覆盖了旧值",则把旧值写回去而不是删行。"""
        plan = receipt.undo_plan or {}
        table = str(plan.get("table") or "")
        row_id = plan.get("row_id")
        if table not in ("app_measurements", "app_workouts", "app_food_logs") or not row_id:
            raise ReceiptError("这条回执的撤销目标不明确,撤不了")
        before = plan.get("before")
        if before:
            sets = ", ".join(str(key) + " = ?" for key in before)
            self.conn.execute(
                "UPDATE " + table + " SET " + sets + " WHERE id = ?",
                [*before.values(), int(row_id)],
            )
            self.conn.commit()
            return "已把 #" + str(row_id) + " 改回原来的值"
        self.conn.execute("DELETE FROM " + table + " WHERE id = ?", (int(row_id),))
        self.conn.commit()
        return "已删掉刚记的这条(#" + str(row_id) + ")"

    def revert_import(self, receipt: Receipt) -> str:
        """把一次批量导入进来的行删掉。靶子是 source 前缀,不做任何猜测。"""
        plan = receipt.undo_plan or {}
        source = str(plan.get("source") or "")
        if not source:
            raise ReceiptError("这条导入回执没有来源标记,撤不了")
        first_id = int(plan.get("first_id") or 0)
        last_id = int(plan.get("last_id") or 0)
        workout_first = int(plan.get("workout_first_id") or 0)
        workout_last = int(plan.get("workout_last_id") or 0)
        if not (first_id and last_id) and not (workout_first and workout_last):
            # 老回执(以及"本次只更新、没有新增"的导入)没有区间可删。
            # ⚠️ 不能退回"按 source 全删" —— 那正是把历次导入一起删掉的 bug。
            raise ReceiptError(
                "这条导入回执没有批次范围(可能是旧版本写的,或本次只更新了已有数据);"
                "为了避免误删历次导入,请到健康页手动删除要删的那几条"
            )
        removed = 0
        if first_id and last_id:
            cursor = self.conn.execute(
                "DELETE FROM app_measurements WHERE source = ? AND id BETWEEN ? AND ?",
                (source, first_id, last_id),
            )
            removed = int(cursor.rowcount or 0)
        # 2026-11-11:一次导入现在也带运动(手机/手表记的),撤销必须连它一起撤,
        # 否则"撤销了"页面上还剩几条运动 —— 那是会骗人的。
        removed_workouts = 0
        if workout_first and workout_last:
            workout_cursor = self.conn.execute(
                "DELETE FROM app_workouts WHERE source = ? AND id BETWEEN ? AND ?",
                (source, workout_first, workout_last),
            )
            removed_workouts = int(workout_cursor.rowcount or 0)
        self.conn.commit()
        message = "已删掉这次导入新建的 " + str(removed) + " 条"
        if removed_workouts:
            message += "、" + str(removed_workouts) + " 次运动"
        return message + "(手工记的、之前导入的都不受影响)"

    def revert_goal(self, receipt: Receipt) -> str:
        plan = receipt.undo_plan or {}
        goal_id = plan.get("goal_id")
        if not goal_id:
            raise ReceiptError("这条回执没有目标号,撤不了")
        self.conn.execute(
            "UPDATE app_health_goals SET status = 'abandoned' WHERE id = ?", (int(goal_id),)
        )
        self.conn.commit()
        return "已把目标 #" + str(goal_id) + " 置为放弃(历史还在,不再算进度)"

    def ensure_food(self, name: str, unit: str = "份", kcal_per_unit: float | None = None) -> int:
        row = self.conn.execute(
            "SELECT id, kcal_per_unit FROM app_foods WHERE name = ? AND unit = ?", (name, unit)
        ).fetchone()
        if row is not None:
            if kcal_per_unit is not None and row["kcal_per_unit"] is None:
                self.conn.execute(
                    "UPDATE app_foods SET kcal_per_unit = ?, cached_at = ? WHERE id = ?",
                    (float(kcal_per_unit), now_iso(), row["id"]),
                )
                self.conn.commit()
            return int(row["id"])
        cursor = self.conn.execute(
            "INSERT INTO app_foods(name, unit, kcal_per_unit, source, cached_at)"
            " VALUES (?, ?, ?, 'manual', ?)",
            (name, unit, float(kcal_per_unit) if kcal_per_unit is not None else None, now_iso()),
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    def log_food(
        self, payload: FoodLogCreate | dict[str, Any], *, actor: str = "agent",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, FoodLogCreate):
            payload = FoodLogCreate.model_validate(payload)
        food_id = self.ensure_food(payload.food_name, payload.unit, payload.kcal_per_unit)
        row = self.conn.execute("SELECT * FROM app_foods WHERE id = ?", (food_id,)).fetchone()
        per_unit = row["kcal_per_unit"]
        kcal = float(per_unit) * float(payload.amount) if per_unit is not None else None
        moment = self._parse(payload.ts)
        owner = self._owner_for_new()
        cursor = self.conn.execute(
            "INSERT INTO app_food_logs(ts, local_date, meal_type, food_id, amount, kcal, note,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                moment.isoformat(timespec="seconds"), self._local_date(moment),
                payload.meal_type, food_id, float(payload.amount), kcal, None, now_iso(),
            ),
        )
        self._stamp_owner("app_food_logs", int(cursor.lastrowid or 0), owner)
        if self.receipts is not None:
            kcal_text = (" 约 " + str(round(kcal)) + " 千卡") if kcal is not None else ""
            self.receipts.record(
                kind="health.food",
                domain=DOMAIN_HEALTH,
                label="记进食 " + str(payload.food_name)[:20] + kcal_text,
                detail={"id": int(cursor.lastrowid or 0), "food": payload.food_name,
                        "amount": float(payload.amount), "unit": payload.unit,
                        "kcal": round(kcal) if kcal is not None else None,
                        "local_date": self._local_date(moment)},
                undo_plan={"type": "row", "table": "app_food_logs",
                           "row_id": int(cursor.lastrowid or 0), "before": None},
                actor=actor, session_id=session_id,
            )
        self.conn.commit()
        self._emit(
            "health.food.logged",
            {"food": payload.food_name, "amount": float(payload.amount), "kcal": kcal},
            key="health.food:" + uuid.uuid4().hex[:12],
        )
        return {
            "food": payload.food_name,
            "amount": float(payload.amount),
            "unit": payload.unit,
            "kcal_estimate": round(kcal) if kcal is not None else None,
            "caveat": MEASUREMENT_CAVEAT if kcal is not None else "这个食物库里还没有热量,记下份量即可",
            "local_date": self._local_date(moment),
        }

    # -- 能量收支 ---------------------------------------------------------
    def _intake_kcal(self, start: str, end: str) -> float:
        clause, params = self._owner_clause()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(kcal), 0) AS total FROM app_food_logs"
            " WHERE local_date >= ? AND local_date < ? AND " + clause,
            (start, end, *params),
        ).fetchone()
        return float(row["total"])

    def _burn_kcal(self, start: str, end: str, weight_kg: float | None) -> float:
        clause, params = self._owner_clause("w.owner_id")
        rows = self.conn.execute(
            "SELECT w.duration_min, w.kcal, COALESCE(a.met, 4.0) AS met FROM app_workouts w"
            " LEFT JOIN app_activity_types a ON a.id = w.activity_id"
            " WHERE w.local_date >= ? AND w.local_date < ? AND " + clause,
            (start, end, *params),
        ).fetchall()
        total = 0.0
        for row in rows:
            # ⭐ 手机/手表自己算的消耗优先:同一个动作不该被我们的公式再估一遍。
            #    没有它(手工记的运动)才退回 MET × 体重。
            if row["kcal"] is not None:
                total += float(row["kcal"])
            elif weight_kg is not None:
                total += kcal_from_met(
                    float(row["met"]), int(row["duration_min"] or 0), weight_kg
                )
        return total

    def energy_summary(self, *, days: int = 7) -> dict[str, Any]:
        now = self._current_time()
        end = (now + timedelta(days=1)).date().isoformat()
        start = (now - timedelta(days=days - 1)).date().isoformat()
        profile = self.profile()
        weight = self.latest("weight")
        weight_kg = float(weight["value"]) if weight is not None else None

        intake = self._intake_kcal(start, end)
        burn = self._burn_kcal(start, end, weight_kg)
        intake_logs = int(self.conn.execute(
            "SELECT COUNT(*) FROM app_food_logs WHERE local_date >= ? AND local_date < ? AND "
            + self._owner_clause()[0],
            (start, end, *self._owner_clause()[1]),
        ).fetchone()[0] or 0)

        # ⭐ 身高以档案为准;档案没填时退到"最新一条身高测量"。
        #    2026-11-11 真机:用户对助手说身高,助手记了一条 checkin 测量 ——
        #    档案仍是空的,能量收支继续报"缺身高"。兜底让这条测量也算数。
        height_cm = profile.height_cm
        height_source = "profile"
        if height_cm is None:
            height_row = self.latest("height")
            if height_row is not None:
                height_cm = float(height_row["value"])
                height_source = "measurement"

        age = profile.age(now)
        missing: list[str] = []
        if weight_kg is None:
            missing.append("体重")
        if height_cm is None:
            missing.append("身高")
        if age is None:
            missing.append("出生年份")
        if missing:
            return {
                "period_days": days, "start": start, "end": end,
                "missing": "还缺" + "、".join(missing) + ",补齐后才能算能量收支",
                "missing_fields": missing,
                "profile": profile.as_dict(now),
                "height_cm": height_cm,
                "height_source": height_source,
                "disclaimer": MEDICAL_DISCLAIMER, "source": "health",
            }
        bmr = mifflin_st_jeor(weight_kg, height_cm, age, profile.sex)
        balance = energy_balance(
            bmr=bmr, activity=profile.activity_level, intake_kcal=intake, burn_kcal=burn,
            days=days, basis="formula",
            note="公式估算,日误差常达 ±" + str(FORMULA_ERROR_KCAL) + " 千卡;" + MEASUREMENT_CAVEAT,
        )
        payload = balance.as_dict()
        # ⚠️ 没有进食记录时"缺口"等于全部消耗 —— 数字算得出来,但没有意义。
        #    单独给一条**结构化**的提示:页面用它画醒目提示,模型拿它说话 ——
        #    不要塞进 note(页面会把 note 与那条提示各显示一次,读起来像复读)。
        payload.update({"period_days": days, "start": start, "end": end,
                        "height_cm": height_cm, "height_source": height_source,
                        "profile": profile.as_dict(now),
                        "intake_logs": intake_logs,
                        "intake_note": (
                            "" if intake_logs else
                            "这段窗口里没有进食记录,缺口等于全部消耗,不具参考意义"
                        ),
                        "disclaimer": MEDICAL_DISCLAIMER, "source": "health"})
        return payload

    def estimate_tdee_from_history(self, *, days: int = 21) -> dict[str, Any]:
        """用你自己的历史反推 TDEE —— 攒够 21 天之后,这比任何公式都准。"""
        now = self._current_time()
        end = (now + timedelta(days=1)).date().isoformat()
        start = (now - timedelta(days=days - 1)).date().isoformat()
        samples = [item for item in self.daily_values("weight", days=days) if item[0] >= start]
        points = ewma_trend(samples)
        span = 0
        change = 0.0
        if len(points) >= 2:
            from ..health.metrics import days_between

            span = days_between(points[0].day, points[-1].day)
            change = points[-1].trend - points[0].trend

        # ⚠️ 没有体重数据时**必须**说数据不够 —— 否则 0 摄入 × 0 变化会算出个 0,
        #    再被当成"有效估计"报出去。实测踩过。
        if span < MIN_DAYS_FOR_ESTIMATE:
            return {
                "available": False,
                "reason": "还需要更多数据(至少 " + str(MIN_DAYS_FOR_ESTIMATE) + " 天)",
                "days_available": span,
                "needed": MIN_DAYS_FOR_ESTIMATE,
                "source": "health",
            }
        # ⚠️ 分子和分母必须是**同一个时间窗**(2026-10-18 审计):以前摄入按
        #    `days` 天算、却除以 body trend 的 `span` 天;体重只覆盖窗口后 10 天时,
        #    平均摄入会被高估 days/span 倍,TDEE 跟着偏大。
        intake_start = str(points[0].day)
        intake_end = (datetime.fromisoformat(str(points[-1].day)).date()
                      + timedelta(days=1)).isoformat()
        intake = self._intake_kcal(intake_start, intake_end)
        average = intake / span
        estimated = tdee_from_history(
            average_intake_kcal=average, weight_change_kg=change, days=span
        )
        if estimated is None:
            return {
                "available": False,
                "reason": "还需要更多数据(至少 " + str(MIN_DAYS_FOR_ESTIMATE) + " 天)",
                "days_available": span,
                "needed": MIN_DAYS_FOR_ESTIMATE,
                "source": "health",
            }
        return {
            "available": True,
            "estimated_tdee": round(estimated),
            "average_intake_kcal": round(average),
            "weight_change_kg": round(change, 2),
            "days": span,
            "basis": "history",
            "note": "用你自己的摄入与体重变化反推,比公式准;" + MEASUREMENT_CAVEAT,
            "disclaimer": MEDICAL_DISCLAIMER,
            "source": "health",
        }

    # -- 从 iPhone 来(2026-11-11)------------------------------------------
    def _units(self) -> dict[str, str]:
        return {
            str(row["code"]): str(row["unit"])
            for row in self.conn.execute("SELECT code, unit FROM app_metric_types")
        }

    def _day_value(self, metric_code: str, day: str) -> float | None:
        """某一天的聚合值(按字典里的规则)。没有记录返回 None —— 不是 0。"""
        clause, params = self._owner_clause()
        rule = self._aggregate_rule(metric_code)
        if rule == "last":
            row = self.conn.execute(
                "SELECT value FROM app_measurements WHERE metric_code = ? AND local_date = ?"
                " AND " + clause + " ORDER BY measured_at DESC LIMIT 1",
                (metric_code, day, *params),
            ).fetchone()
            return None if row is None else float(row["value"])
        expression = {
            "sum": "SUM(value)", "avg": "AVG(value)",
            "min": "MIN(value)", "max": "MAX(value)",
        }.get(rule, "AVG(value)")
        row = self.conn.execute(
            "SELECT " + expression + " AS v, COUNT(*) AS n FROM app_measurements"
            " WHERE metric_code = ? AND local_date = ? AND " + clause,
            (metric_code, day, *params),
        ).fetchone()
        if row is None or not int(row["n"]):
            return None
        return float(row["v"])

    def series(self, metric_code: str, *, days: int = 30) -> dict[str, Any]:
        """趋势页要的一条序列:原始日值 + 7 日滑动平均 + 四个数。

        ⚠️ 只有体重用 EWMA(D43:体重噪声必须压)。步数/睡眠用**滑动平均**:
        它们不是"缓慢漂移的真值 + 噪声",而是"每天本来就不同",EWMA 会把它抹平。
        """
        try:
            metric = self.metric_type(metric_code)
        except HealthError:
            return {"metric_code": metric_code, "has_data": False, "error": "未知指标"}
        decimals = int(metric["decimals"])
        values = self.daily_values(metric_code, days=days)
        points = [{"day": day, "value": round(value, decimals)} for day, value in values]

        moving: list[dict[str, Any]] = []
        window: list[float] = []
        for index, (day, value) in enumerate(values):
            window.append(value)
            if len(window) > 7:
                window.pop(0)
            if len(window) == 7 or (index == len(values) - 1 and len(window) >= 3):
                moving.append({"day": day, "value": round(sum(window) / len(window), decimals)})

        latest = points[-1]["value"] if points else None
        numbers = [float(value) for _day, value in values]
        change = None
        if len(numbers) >= 2:
            change = round(numbers[-1] - numbers[0], decimals)

        def _text(value: float | None) -> str:
            """页面上的数字:0 位小数就写成整数(7320),别写成 7320.0。"""
            if value is None:
                return ""
            return str(int(round(value))) if decimals == 0 else f"{value:.{decimals}f}"

        average = round(sum(numbers) / len(numbers), decimals) if numbers else None
        minimum = round(min(numbers), decimals) if numbers else None
        maximum = round(max(numbers), decimals) if numbers else None
        return {
            "metric_code": metric_code,
            "name": str(metric["name"]),
            "unit": str(metric["unit"]),
            "decimals": decimals,
            "days": days,
            "has_data": bool(points),
            "points": points,
            "moving": moving,
            "latest": latest,
            "average": average,
            "minimum": minimum,
            "maximum": maximum,
            "change": change,
            #: 给页面直接用的文本:小数位由字典决定,模板不做格式化。
            "latest_text": _text(latest),
            "average_text": _text(average),
            "minimum_text": _text(minimum),
            "maximum_text": _text(maximum),
            "change_text": _text(abs(change) if change is not None else None),
            "count": len(numbers),
            "source": "health",
        }

    def today_snapshot(self) -> dict[str, Any]:
        """「今天」那一页要的全部事实:三个环、昨夜睡眠、今天的运动、最新指标。

        纯读、不调模型;每一格都允许 "None"(没有数据 ≠ 0)。
        """
        now = self._current_time()
        day = self._local_date(now)
        rings: list[dict[str, Any]] = []
        for code, target, unit in RING_TARGETS:
            value = self._day_value(code, day)
            percent = None
            if value is not None and target:
                percent = round(min(100.0, max(0.0, value / float(target) * 100.0)), 1)
            rings.append({
                "metric": code, "name": str(self.metric_type(code)["name"]),
                "unit": unit, "target": target,
                "value": round(value, 0) if value is not None else None,
                "percent": percent,
                "done": value is not None and value >= target,
            })

        highlight = ("weight", "body_fat", "resting_hr", "hrv", "vo2max", "bp_sys")
        latest: list[dict[str, Any]] = []
        for code in highlight:
            row = self.latest(code)
            if row is None:
                continue
            history = self.daily_values(code, days=8)
            previous = next(
                (value for when, value in reversed(history) if when < row["local_date"]),
                None,
            )
            decimals = int(self.metric_type(code)["decimals"])
            value = round(float(row["value"]), decimals)
            latest.append({
                "metric": code,
                "name": str(self.metric_type(code)["name"]),
                "unit": row["unit"],
                "value": value,
                "text": str(int(round(value))) if decimals == 0 else f"{value:.{decimals}f}",
                "measured_at": row["measured_at"],
                "delta": (
                    round(float(row["value"]) - previous, decimals)
                    if previous is not None else None
                ),
            })

        workouts = [
            item for item in self.recent_workouts(limit=80)
            if str(item.get("ts") or "")[:10] == day
        ]
        device_row = self.conn.execute(
            "SELECT MAX(measured_at) AS last_at, COUNT(*) AS n FROM app_measurements"
            " WHERE source = ? AND " + self._owner_clause()[0],
            (APPLE_SOURCE, *self._owner_clause()[1]),
        ).fetchone()

        return {
            "date": day,
            "rings": rings,
            "sleep": self.sleep_summary(days=14),
            "workouts": workouts,
            "latest": latest,
            "last_sync": self.last_sync(),
            "device": {
                "rows": int(device_row["n"] or 0),
                "last_at": device_row["last_at"],
                "has_data": bool(int(device_row["n"] or 0)),
            },
            "source": "health",
        }

    def sleep_summary(self, *, days: int = 14) -> dict[str, Any]:
        """睡眠:昨夜、近 7 夜平均、作息规律性。

        ⭐ 规律性用**睡眠中点的离散度**(报告 §2.7.5:它比时长更敏感),
        但中点是个"环"(23:50 与 00:10 只差 20 分钟,直接算标准差会得到 24 小时),
        所以先转成角度算圆统计,再折回小时。
        """
        values = self.daily_values("sleep_hours", days=days)
        midpoints = [value for _day, value in self.daily_values("sleep_midpoint", days=days)]
        if not values:
            return {"has_data": False, "nights": 0, "source": "health"}

        last_day, last_hours = values[-1]
        recent = [value for _day, value in values[-7:]]
        average = sum(recent) / len(recent)
        clause, params = self._owner_clause()
        row = self.conn.execute(
            "SELECT measured_at, note FROM app_measurements WHERE metric_code = 'sleep_hours'"
            " AND " + clause + " ORDER BY measured_at DESC LIMIT 1",
            (*params,),
        ).fetchone()
        bedtime = wake = None
        if row is not None:
            wake_moment = self._parse(str(row["measured_at"]))
            # ⭐ 只有"起床时刻是真的"(导出里有具体时间)才反推入睡。
            #    快捷指令只推时长时,measured_at 是当天 00:00 —— 拿它减 7 小时
            #    会显示"下午 4 点入睡",那是编的。没有就如实不给。
            if wake_moment.hour or wake_moment.minute:
                wake = wake_moment.isoformat(timespec="seconds")
                # 导出时把真实入睡时刻写进了 note("23:20→07:00")—— 用它,
                # 免得用"起床 − 7.67 小时"反推出 23:19 这种差一分钟的假精确。
                note = str(row["note"] or "")
                if "→" in note and ":" in note.split("→", 1)[0]:
                    bedtime = wake_moment.date().isoformat() + "T" + note.split("→", 1)[0] + ":00"
                    if note.split("→", 1)[0] > wake[11:16]:
                        # 入睡在起床日之前(跨午夜):日期减一天
                        bedtime = (
                            wake_moment.date() - timedelta(days=1)
                        ).isoformat() + "T" + note.split("→", 1)[0] + ":00"
                else:
                    bedtime_moment = wake_moment - timedelta(hours=float(last_hours))
                    bedtime = bedtime_moment.isoformat(timespec="seconds")
        return {
            "has_data": True,
            "nights": len(values),
            "last_day": last_day,
            "last_hours": round(float(last_hours), 1),
            "average_hours": round(average, 1),
            "bedtime": bedtime,
            "wake": wake,
            "midpoint_hour": round(midpoints[-1], 2) if midpoints else None,
            "regularity_minutes": (
                round(_circular_spread_hours(midpoints) * 60)
                if len(midpoints) >= 3 else None
            ),
            "points": [{"day": day, "hours": round(float(value), 1)} for day, value in values[-14:]],
            "source": "health",
        }

    def coverage(self) -> dict[str, Any]:
        """数据覆盖域:每个指标有多少条、最早最晚到哪天;设备同步到哪一刻。

        ⭐ 页面与工具都该先看这一格 —— 曲线为空时,先要能回答"到底是没数据,
        还是同步没跑通"。
        """
        clause, params = self._owner_clause()
        names = {
            str(row["code"]): (str(row["name"]), str(row["unit"]), int(row["decimals"]))
            for row in self.conn.execute("SELECT code, name, unit, decimals FROM app_metric_types")
        }
        metrics: list[dict[str, Any]] = []
        for row in self.conn.execute(
            "SELECT metric_code, COUNT(*) AS n, MIN(local_date) AS first, MAX(local_date) AS last,"
            " COUNT(DISTINCT source) AS sources FROM app_measurements"
            " WHERE " + clause + " GROUP BY metric_code ORDER BY metric_code",
            (*params,),
        ):
            code = str(row["metric_code"])
            label, unit, decimals = names.get(code, (code, "", 1))
            metrics.append({
                "metric": code, "name": label, "unit": unit, "decimals": decimals,
                "count": int(row["n"]), "first": row["first"], "last": row["last"],
                "sources": int(row["sources"]),
            })
        workout_row = self.conn.execute(
            "SELECT COUNT(*) AS n, MAX(local_date) AS last FROM app_workouts WHERE "
            + self._owner_clause()[0], (*self._owner_clause()[1],),
        ).fetchone()
        total = sum(item["count"] for item in metrics)
        return {
            "metrics": metrics,
            "total": total,
            "workouts": int(workout_row["n"] or 0),
            "workouts_last": workout_row["last"],
            "last_sync": self.last_sync(),
            "synced_metrics": [item["metric"] for item in metrics if item["count"]],
            "source": "health",
        }

    def last_sync(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM app_health_syncs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _sync_public(dict(row)) if row is not None else None

    def recent_syncs(self, *, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM app_health_syncs ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        return [_sync_public(dict(row)) for row in rows]

    def record_sync(
        self, *, source: str, items: int, days: int, note: str = "",
        ip: str = "", ua: str = "",
    ) -> None:
        """记一笔同步。⚠️ 只记**数量与时间**,不记原始报文、不记 token。"""
        self.conn.execute(
            "INSERT INTO app_health_syncs(ts, source, items, days, note, ip, ua)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), str(source)[:40], int(items), int(days), str(note)[:200],
             str(ip)[:60], str(ua)[:200]),
        )
        self.conn.commit()

    def sync_push(
        self, payload: HealthSyncPayload | dict[str, Any], *,
        source: str = "shortcut", ip: str = "", ua: str = "",
    ) -> dict[str, Any]:
        """接收一次设备推送(快捷指令 / 第三方 App)。

        三个桶:daily(一天一个汇总)、samples(原始观测)、workouts(运动)。
        ⭐ 全部是 **upsert**:同一天重复推 = 覆盖,不会越积越多。
        """
        data = payload if isinstance(payload, HealthSyncPayload) else HealthSyncPayload.model_validate(payload)
        owner = self._owner_for_new()
        units = self._units()
        stamp = now_iso()
        today = self._current_time().date()
        # ⚠️ 两个桶要分开:只有"当日汇总"(days)会**替换**同一天的旧记录;
        #    "原始观测"(samples)只做 upsert,绝不能顺带删掉那天的其他记录 ——
        #    否则补一条昨天的体重,会把昨天已导入的步数/睡眠一起抹掉(实测踩过)。
        day_rows: list[tuple[Any, ...]] = []
        sample_rows: list[tuple[Any, ...]] = []
        skipped = 0

        # ⭐ 2026-11-14:扁平写法(顶层直接给 steps/active_kcal/...)也收 ——
        #    快捷指令里"请求体 JSON"只要填 3 个字段,不用手写嵌套数组。
        day_payloads = list(data.days)
        if data.date is not None or any(
            getattr(data, field, None) is not None for field in DAY_METRIC_FIELDS
        ):
            day_payloads.append(data)

        for day in day_payloads:
            raw_date = str(day.date) if day.date else today.isoformat()
            try:
                day_date = date.fromisoformat(raw_date[:10])
            except ValueError:
                skipped += 1
                continue
            # 未来日期一律不收(设备的时区错位会把明天推过来,那会把趋势拉歪)。
            if day_date > today + timedelta(days=1):
                skipped += 1
                continue
            local_date = day_date.isoformat()
            measured_at = datetime.combine(
                day_date, time.min, tzinfo=ZoneInfo(self.tzid)
            ).isoformat(timespec="seconds")
            for field in DAY_METRIC_FIELDS:
                value = getattr(day, field, None)
                if value is None or not _plausible(field, float(value)):
                    continue
                day_rows.append((
                    field, float(value), units.get(field, ""), measured_at, local_date,
                    APPLE_SOURCE, DAILY_MARKER, stamp, owner,
                ))

        for sample in data.samples:
            metric_code = normalize_metric(sample.metric)
            if metric_code is None:
                skipped += 1
                continue
            number = float(sample.value)
            if sample.unit:
                converted = convert_value(metric_code, number, sample.unit)
                if converted is None:
                    skipped += 1
                    continue
                number = converted
            if not _plausible(metric_code, number):
                skipped += 1
                continue
            moment = self._parse(sample.start) if sample.start else self._current_time()
            sample_rows.append((
                metric_code, number, units.get(metric_code, ""),
                moment.isoformat(timespec="seconds"), self._local_date(moment),
                APPLE_SOURCE, None, stamp, owner,
            ))

        # 同一天已经有"当日汇总"的指标,不再接受零散样本 —— 两边都算会翻倍。
        # ⚠️ 这个判断要**查库**:上一次推送留下的"每日汇总"不在这一次的 day_rows 里。
        day_cells = {(row[0], row[4]) for row in day_rows}
        marker_cells: set[tuple[str, str]] = set()
        sample_days = sorted({str(row[4]) for row in sample_rows})
        for offset in range(0, len(sample_days), 300):
            chunk = sample_days[offset:offset + 300]
            for marker in self.conn.execute(
                "SELECT metric_code, local_date FROM app_measurements WHERE source = ?"
                " AND note = ? AND local_date IN (" + ",".join("?" * len(chunk)) + ")",
                (APPLE_SOURCE, DAILY_MARKER, *chunk),
            ):
                marker_cells.add((str(marker["metric_code"]), str(marker["local_date"])))
        kept_samples = []
        for row in sample_rows:
            if (row[0], row[4]) in day_cells or (row[0], row[4]) in marker_cells:
                skipped += 1
                continue
            kept_samples.append(row)
        rows = day_rows + kept_samples

        if rows:
            # 只有"当日汇总"会替换同一天:否则导出 8000 步 + 快捷指令又推 8000 步
            # 会在聚合时变成 16000。
            for metric_code, local_date in day_cells:
                self.conn.execute(
                    "DELETE FROM app_measurements WHERE metric_code = ? AND local_date = ?"
                    " AND source = ?",
                    (metric_code, local_date, APPLE_SOURCE),
                )
            self.conn.executemany(
                "INSERT INTO app_measurements(metric_code, value, unit, measured_at,"
                " local_date, source, note, created_at, owner_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT DO UPDATE SET value = excluded.value",
                rows,
            )

        workouts = 0
        for item in data.workouts:
            kind, met = resolve_activity(item.activity)
            moment = self._parse(item.start) if item.start else self._current_time()
            external = item.external_id or (
                "sync|" + kind + "|" + moment.isoformat(timespec="seconds")
            )
            self.conn.execute(
                "INSERT INTO app_workouts(ts, local_date, kind, activity_id, duration_min,"
                " distance_km, note, created_at, owner_id, source, external_id, kcal)"
                " VALUES (?, ?, ?, (SELECT id FROM app_activity_types WHERE name = ?), ?, ?,"
                " NULL, ?, ?, ?, ?, ?)"
                " ON CONFLICT(source, external_id) WHERE external_id IS NOT NULL"
                " DO UPDATE SET duration_min = excluded.duration_min,"
                " distance_km = excluded.distance_km, kcal = excluded.kcal",
                (
                    moment.isoformat(timespec="seconds"), self._local_date(moment), kind,
                    kind, max(1, int(round(item.duration_min))), item.distance_km,
                    stamp, owner, APPLE_SOURCE, external, item.kcal,
                ),
            )
            workouts += 1

        self.conn.commit()
        day_count = len({row[4] for row in rows})
        self.record_sync(
            source=source or data.source, items=len(rows) + workouts,
            days=day_count, note=data.device, ip=ip, ua=ua,
        )
        return {
            "ok": True,
            "days": day_count,
            "samples": len(rows),
            "workouts": workouts,
            "skipped": skipped,
            "source": source or data.source,
        }

    # -- 导入 -------------------------------------------------------------
    def import_apple_health(self, path: str, *, limit: int | None = None) -> dict[str, Any]:
        """批量导入 Apple 健康导出(export.xml 或 export.zip)。

        ⚠️ **批量写,不逐条提交**:一次导入可能几万条,原来逐条
        log_measurement 会一行一个 commit(实测几分钟)。现在一个事务里
        executemany,并把撤销靶子记成两个 id 区间(测量 / 运动)。

        ⚠️ 逐条回执仍然不出(回执卡会被刷屏),只在最后出**一条**汇总回执。
        """
        report = parse_apple_export(path, tzid=self.tzid, limit=limit)
        # ⚠️ 解析成功不等于导对了文件:空 XML、或只有 CD A 记录时 report 是全空的。
        #    以前会回"已导入 0 条",用户以为成功 —— 现在明说选错了。
        if not (report.samples or report.nights or report.workouts):
            raise HealthError(
                "这个文件里没有可识别的健康记录(可能选错了文件,或者「健康」的导出还没完成)"
            )
        owner = self._owner_for_new()
        units = self._units()
        stamp = now_iso()
        activity_ids = {
            str(row["name"]): int(row["id"])
            for row in self.conn.execute("SELECT id, name FROM app_activity_types")
        }

        before_measure = int(
            self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM app_measurements").fetchone()[0]
        )
        before_workout = int(
            self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM app_workouts").fetchone()[0]
        )

        rows: list[tuple[Any, ...]] = []
        skipped_metric = 0
        for sample in report.samples:
            # ⚠️ 外键的防线放在这里:字典里没有的指标**跳过并报数**,不让
            #    "FOREIGN KEY constraint failed" 把整次导入回滚(2026-11-11 真机:
            #    导出里有一条 Height,而字典少 seed 了 height)。
            if sample.metric_code not in units:
                skipped_metric += 1
                continue
            rows.append((
                sample.metric_code, float(sample.value), units.get(sample.metric_code, ""),
                sample.measured_at, sample.local_date, APPLE_SOURCE,
                DAILY_MARKER if sample.daily else None, stamp, owner,
            ))
        for night in report.nights:
            # ⚠️ 睡眠是"区间",不是一条数字;折成三条后才和测量表同形:
            #   睡眠时长 / 卧床时长 / 睡眠中点(都用**起床时刻**当 measured_at)。
            note = night.bedtime[11:16] + "→" + night.wake[11:16]
            rows.append((
                "sleep_hours", float(night.asleep_hours), units.get("sleep_hours", "h"),
                night.wake, night.local_date, APPLE_SOURCE, note, stamp, owner,
            ))
            if night.in_bed_hours is not None:
                rows.append((
                    "sleep_inbed", float(night.in_bed_hours), units.get("sleep_inbed", "h"),
                    night.wake, night.local_date, APPLE_SOURCE, note, stamp, owner,
                ))
            rows.append((
                "sleep_midpoint", float(night.midpoint_hour), units.get("sleep_midpoint", "时"),
                night.wake, night.local_date, APPLE_SOURCE, note, stamp, owner,
            ))

        workout_rows: list[tuple[Any, ...]] = []
        for item in report.workouts:
            workout_rows.append((
                item.start, item.local_date, item.kind,
                activity_ids.get(item.kind), int(item.duration_min),
                item.distance_km, "来自 iPhone 健康" + (
                    " · 苹果估算 " + str(round(item.kcal)) + " 千卡" if item.kcal else ""
                ),
                stamp, owner, APPLE_SOURCE, item.external_id, item.kcal,
            ))

        if rows:
            # ⚠️ 2026-11-11 改:高频指标在**解析时**已经按天合计,导出的每日行与
            #    快捷指令推的每日行落在同一个键上(metric + 当天 00:00 + source),
            #    所以靠 upsert 就能互相替换,**不再整片删除** —— 删除会每次重新分配
            #    id,让"重复导入不新增"这条性质失效。
            #
            #    只有睡眠例外:导出的睡眠以**起床时刻**为时间戳(07:05),
            #    而快捷指令只推时长时用当天 00:00 —— 两者键不同,会在同一天算两遍。
            #    所以导入时只清掉那几天的"每日汇总"睡眠行。
            sleep_days = sorted({
                str(row[4]) for row in rows
                if row[0] in ("sleep_hours", "sleep_inbed", "sleep_midpoint")
            })
            for offset in range(0, len(sleep_days), 300):
                chunk = sleep_days[offset:offset + 300]
                self.conn.execute(
                    "DELETE FROM app_measurements WHERE source = ? AND note = ?"
                    " AND metric_code IN ('sleep_hours','sleep_inbed','sleep_midpoint')"
                    " AND local_date IN (" + ",".join("?" * len(chunk)) + ")",
                    (APPLE_SOURCE, DAILY_MARKER, *chunk),
                )
            self.conn.executemany(
                "INSERT INTO app_measurements(metric_code, value, unit, measured_at,"
                " local_date, source, note, created_at, owner_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT DO UPDATE SET value = excluded.value, note = excluded.note",
                rows,
            )
        if workout_rows:
            self.conn.executemany(
                "INSERT INTO app_workouts(ts, local_date, kind, activity_id, duration_min,"
                " distance_km, note, created_at, owner_id, source, external_id, kcal)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(source, external_id) WHERE external_id IS NOT NULL"
                " DO UPDATE SET duration_min = excluded.duration_min,"
                " distance_km = excluded.distance_km, kcal = excluded.kcal",
                workout_rows,
            )
        self.conn.commit()

        after_measure = int(
            self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM app_measurements").fetchone()[0]
        )
        after_workout = int(
            self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM app_workouts").fetchone()[0]
        )
        created_measurements = after_measure - before_measure
        created_workouts = after_workout - before_workout

        payload = report.as_dict()
        payload.update({
            "written": len(rows),
            "created_measurements": created_measurements,
            "created_workouts": created_workouts,
            "sleep_nights": len(report.nights),
            "workout_count": len(report.workouts),
            "skipped_unknown_metric": skipped_metric,
            "note": "只认白名单指标;未知类型直接跳过(苹果的 schema 没有官方文档)",
        })

        if self.receipts is not None and (created_measurements or created_workouts):
            self.receipts.record(
                kind="health.import",
                domain=DOMAIN_HEALTH,
                label=(
                    "导入 iPhone 健康 " + str(len(rows)) + " 条"
                    + ("· 睡眠 " + str(len(report.nights)) + " 夜" if report.nights else "")
                    + ("· 运动 " + str(len(report.workouts)) + " 次" if report.workouts else "")
                ),
                detail={
                    "written": len(rows), "sleep_nights": len(report.nights),
                    "workouts": len(report.workouts),
                    "skipped_bad": report.skipped_bad,
                    "skipped_unknown_metric": skipped_metric,
                    "file": str(path)[-40:],
                },
                undo_plan={
                    "type": "import",
                    "source": APPLE_SOURCE,
                    # ⚠️ 靶子是**这一次新插入的行**。用 max(id) 前后差即可 ——
                    #   ON CONFLICT 走更新时不产生新 id,不会被算进来。
                    #   没有新增的那一侧记 0/0(而不是 before+1/before),否则
                    #   只有运动、没有测量的那次导入,撤销会被"没有批次范围"挡掉。
                    "first_id": before_measure + 1 if created_measurements else 0,
                    "last_id": after_measure if created_measurements else 0,
                    "count": created_measurements,
                    "workout_first_id": before_workout + 1 if created_workouts else 0,
                    "workout_last_id": after_workout if created_workouts else 0,
                    "workout_count": created_workouts,
                    "note": "只删这次导入新建的行,不动手工记的、也不动之前导入的",
                },
                actor="import",
            )
            self.conn.commit()

        if rows or workout_rows:
            self.record_sync(
                source="export", items=len(rows) + len(workout_rows),
                days=len({row[4] for row in rows}),
                note="导入 " + Path(path).name[:60],
            )
        return payload

    # -- 目标 -------------------------------------------------------------
    def create_goal(self, payload: HealthGoalCreate | dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, HealthGoalCreate):
            payload = HealthGoalCreate.model_validate(payload)
        cursor = self.conn.execute(
            "INSERT INTO app_health_goals(metric, target_value, start_value, target_date,"
            " status, created_at) VALUES (?, ?, ?, ?, 'active', ?)",
            (payload.metric, float(payload.target_value), payload.start_value,
             payload.target_date, now_iso()),
        )
        goal_id = int(cursor.lastrowid or 0)
        if self.receipts is not None:
            self.receipts.record(
                kind="health.goal",
                domain=DOMAIN_HEALTH,
                label="设健康目标 " + str(payload.metric) + " 到 " + str(payload.target_value),
                detail={"id": goal_id, "metric": payload.metric,
                        "target_value": float(payload.target_value)},
                # 目标不删行,撤销 = 置为 abandoned(历史与进度还在,只是不再算数)
                undo_plan={"type": "health_goal", "goal_id": goal_id},
                actor="agent",
            )
        self.conn.commit()
        return self.goal_progress(goal_id)

    def goal_progress(self, goal_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM app_health_goals WHERE id = ?", (goal_id,)
        ).fetchone()
        if row is None:
            raise HealthError("目标不存在: " + str(goal_id))
        latest = self.latest(row["metric"])
        current = float(latest["value"]) if latest else None
        start = row["start_value"]
        target = float(row["target_value"])
        progress = None
        if current is not None and start is not None and abs(target - float(start)) > 1e-9:
            progress = (current - float(start)) / (target - float(start))
        return {
            "id": int(row["id"]),
            "metric": row["metric"],
            "target_value": target,
            "start_value": start,
            "current_value": current,
            "progress": round(progress, 3) if progress is not None else None,
            "status": row["status"],
            "note": "进度由领域数据算出,不来自记忆",
            "source": "health",
        }

    def daily_summary(self, local_date: str | None = None) -> dict[str, Any]:
        day = local_date or self._local_date(self._current_time())
        w_clause, w_params = self._owner_clause("w.owner_id")
        workouts = self.conn.execute(
            "SELECT w.duration_min, COALESCE(a.met, 4.0) AS met, w.kind FROM app_workouts w"
            " LEFT JOIN app_activity_types a ON a.id = w.activity_id"
            " WHERE w.local_date = ? AND " + w_clause,
            (day, *w_params),
        ).fetchall()
        total_met_minutes = sum(
            met_minutes(float(row["met"]), int(row["duration_min"] or 0)) for row in workouts
        )
        f_clause, f_params = self._owner_clause()
        meals = self.conn.execute(
            "SELECT COALESCE(SUM(kcal), 0) AS total, COUNT(*) AS n FROM app_food_logs"
            " WHERE local_date = ? AND " + f_clause,
            (day, *f_params),
        ).fetchone()
        m_clause, m_params = self._owner_clause()
        measurements = [
            {"metric": row["metric_code"], "value": float(row["value"]), "unit": row["unit"]}
            for row in self.conn.execute(
                "SELECT metric_code, value, unit FROM app_measurements WHERE local_date = ?"
                " AND " + m_clause + " ORDER BY metric_code",
                (day, *m_params),
            ).fetchall()
        ]
        # ⭐ 手机上那几个"今天"的数(步数/活动能量/锻炼/距离/睡眠/静息心率)。
        #   没有就是没有 —— 不塞 0,否则模型会把"还没同步"说成"今天没动"。
        device: dict[str, float] = {}
        for code in ("steps", "active_kcal", "exercise_min", "distance_km",
                     "sleep_hours", "resting_hr"):
            value = self._day_value(code, day)
            if value is None:
                continue
            device[code] = round(value, int(self.metric_type(code)["decimals"]))
        return {
            "date": day,
            "workouts": [row["kind"] for row in workouts],
            "met_minutes": round(total_met_minutes, 1),
            "intake_kcal": round(float(meals["total"])),
            "meal_count": int(meals["n"]),
            "measurements": measurements,
            "device": device,
            "last_sync": self.last_sync(),
            "source": "health",
        }

    # -- 内部 -------------------------------------------------------------
    def _emit(self, event_type: str, payload: dict[str, Any], key: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?)",
            (event_type, json.dumps(payload, ensure_ascii=False, default=str),
             "outbox:" + key, now_iso()),
        )
        self.conn.commit()
