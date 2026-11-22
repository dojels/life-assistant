"""健康域的纯函数:平滑、MET、能量公式。

全部是**读取时计算**,不落库(能力域 §5.3 要点 6:存原始观测,算派生指标)。
好处:改算法不用迁移数据,也不会出现"冗余列和真实数据不一致"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

# --- 体重趋势 -------------------------------------------------------------
# ⚠️ 原始日体重噪声 ±1~2kg,而真实脂肪变化上限只有 0.5~1kg/周 —— 直接看原图必然误判。
DEFAULT_ALPHA = 0.1
# ⚠️ EWMA 有约 9~10 天滞后,**UI 必须说明**,否则用户以为"我减了但曲线没动"是 bug。
EWMA_LAG_DAYS = 10

# --- 运动 -----------------------------------------------------------------
MET_MINUTES_DIVISOR = 1.0  # MET-分钟 = MET × 分钟
INTENSITY_LIGHT = 3.0
INTENSITY_VIGOROUS = 6.0


def intensity_label(met: float) -> str:
    if met < INTENSITY_LIGHT:
        return "轻"
    if met < INTENSITY_VIGOROUS:
        return "中"
    return "剧烈"


def met_minutes(met: float, minutes: float) -> float:
    """MET-分钟。选它是因为**不依赖体重、可跨运动类型累加**。"""
    return float(met) * float(minutes)


def kcal_from_met(met: float, minutes: float, weight_kg: float) -> float:
    """千卡 = MET × 体重kg × 小时。"""
    return float(met) * float(weight_kg) * (float(minutes) / 60.0)


# --- 能量收支 -------------------------------------------------------------
ACTIVITY_FACTORS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}
ACTIVITY_LABELS = {
    "sedentary": "久坐",
    "light": "轻度活动",
    "moderate": "中度活动",
    "active": "高度活动",
    "very_active": "极高强度",
}
KCAL_PER_KG_FAT = 7700.0
# ⚠️ 公式法必然有误差(BMR 个体差异 ±10-15%、活动系数是离散值 ±20%、代谢适应…),
#    叠加常达 ±300~500 kcal/日。所以对外一律用"约/区间"表述。
FORMULA_ERROR_KCAL = 400
MIN_DAYS_FOR_ESTIMATE = 21


def mifflin_st_jeor(weight_kg: float, height_cm: float, age: int, sex: str) -> float:
    """基础代谢率(Mifflin-St Jeor, Am J Clin Nutr 1990)。

        REE = 10×kg + 6.25×cm − 5×age + (男 +5 / 女 −161)
    """
    base = 10.0 * float(weight_kg) + 6.25 * float(height_cm) - 5.0 * float(age)
    return base + (5.0 if (sex or "").strip() in ("男", "male", "m") else -161.0)


def tdee(bmr: float, activity: str = "sedentary") -> float:
    return float(bmr) * ACTIVITY_FACTORS.get(activity, 1.2)


@dataclass
class TrendPoint:
    day: str
    value: float
    trend: float

    def as_dict(self) -> dict:
        return {"day": self.day, "value": round(self.value, 3), "trend": round(self.trend, 3)}


def ewma_trend(
    samples: Sequence[tuple[str, float]], *, alpha: float = DEFAULT_ALPHA
) -> list[TrendPoint]:
    """指数加权移动平均。

    trend[t] = trend[t-1] + α(value[t] − trend[t-1])

    优于滑动平均:冷启动只要一个初值、对缺失日鲁棒、O(1) 增量。
    ⚠️ 代价是**约 10 天滞后**,这一点必须告诉用户。
    """
    ordered = sorted(samples, key=lambda item: item[0])
    if not ordered:
        return []
    trend = float(ordered[0][1])
    points: list[TrendPoint] = []
    for day, value in ordered:
        trend = trend + alpha * (float(value) - trend)
        points.append(TrendPoint(day=day, value=float(value), trend=trend))
    return points


def weekly_rate(points: Sequence[TrendPoint]) -> float | None:
    """按趋势线估周变化速率(kg/周)。用首尾趋势值,不用原始值。"""
    if len(points) < 2:
        return None
    first, last = points[0], points[-1]
    try:
        start = date.fromisoformat(first.day)
        end = date.fromisoformat(last.day)
    except ValueError:
        return None
    days = (end - start).days
    if days <= 0:
        return None
    return (last.trend - first.trend) / days * 7.0


@dataclass
class EnergyBalance:
    bmr: float
    activity: str
    tdee: float
    intake_kcal: float
    burn_kcal: float
    deficit_kcal: float
    days: int
    basis: str                      # formula | history
    note: str = ""

    @property
    def deficit_range(self) -> tuple[float, float]:
        return (self.deficit_kcal - FORMULA_ERROR_KCAL, self.deficit_kcal + FORMULA_ERROR_KCAL)

    def as_dict(self) -> dict:
        low, high = self.deficit_range
        return {
            "bmr": round(self.bmr),
            "activity": self.activity,
            "activity_label": ACTIVITY_LABELS.get(self.activity, self.activity),
            "tdee": round(self.tdee),
            "intake_kcal": round(self.intake_kcal),
            "burn_kcal": round(self.burn_kcal),
            "deficit_kcal": round(self.deficit_kcal),
            "deficit_range": [round(low), round(high)],
            "days": self.days,
            "basis": self.basis,
            "note": self.note,
        }


def energy_balance(
    *,
    bmr: float,
    activity: str,
    intake_kcal: float,
    burn_kcal: float,
    days: int,
    basis: str = "formula",
    note: str = "",
) -> EnergyBalance:
    total_tdee = tdee(bmr, activity) * max(days, 1)
    deficit = total_tdee + burn_kcal - intake_kcal
    return EnergyBalance(
        bmr=bmr, activity=activity, tdee=total_tdee, intake_kcal=intake_kcal,
        burn_kcal=burn_kcal, deficit_kcal=deficit, days=days, basis=basis, note=note,
    )


def tdee_from_history(
    *, average_intake_kcal: float, weight_change_kg: float, days: int
) -> float | None:
    """从你自己的历史数据反推 TDEE —— **攒够 21 天之后,这比任何公式都准**。

        实际 TDEE ≈ 平均日摄入 − (体重变化kg × 7700 / 天数)
    """
    if days < MIN_DAYS_FOR_ESTIMATE:
        return None
    return float(average_intake_kcal) - (float(weight_change_kg) * KCAL_PER_KG_FAT / float(days))


def week_bounds(moment: datetime) -> tuple[str, str]:
    start = (moment - timedelta(days=moment.weekday())).date()
    return start.isoformat(), (start + timedelta(days=7)).isoformat()


def days_between(first: str, last: str) -> int:
    try:
        return (date.fromisoformat(last) - date.fromisoformat(first)).days
    except ValueError:
        return 0
