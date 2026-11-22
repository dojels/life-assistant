"""健康域的请求/响应模型。

⚠️ 两条写在 schema 里的红线(能力域 §6.5):
  * **不给医疗建议** —— 工具只做记录与统计,越界的表述由提示词层另行约束;
  * **一律用"约/区间"** —— 计数误差是固有的(条目 ±10-25%,份量 ±20-50%),不许报精确值。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

MEASUREMENT_SOURCES = ("user", "chat", "device:apple", "device:google", "calculated")
ACTIVITY_LEVELS = ("sedentary", "light", "moderate", "active", "very_active")
MEAL_TYPES = ("早餐", "午餐", "晚餐", "加餐")


class MeasurementCreate(BaseModel):
    """记一条身体测量。指标从字典里选,不新建表。"""

    metric_code: str = Field(
        ..., max_length=30,
        description="指标代码:weight(体重kg)/body_fat(体脂%)/waist(腰围cm)/"
                    "resting_hr(静息心率)/bp_sys/bp_dia/sleep_hours/steps",
    )
    value: float = Field(..., description="数值")
    measured_at: str | None = Field(
        None, max_length=40, description="测量时间 ISO8601;不传就是现在"
    )
    source: Literal["user", "chat", "device:apple", "device:google", "calculated"] = "chat"
    note: str | None = Field(None, max_length=200, description="备注")


class ProfileUpdate(BaseModel):
    """身体档案(能量收支的输入)。

    ⚠️ 这是**档案**,不是测量:
      * 身高写进档案才会影响 BMR;只记一条 height 测量不会(页面/能量会做兜底,
        但档案才是正式来源);
      * 出生年份只在这里有落点 —— 以前没有工具能写,模型只能回"系统里没有这个字段"。
    """

    sex: Literal["男", "女"] | None = Field(None, description="性别:男 / 女")
    birth_year: int | None = Field(None, ge=1900, le=2100, description="出生年份,例如 1985")
    height_cm: float | None = Field(None, ge=50, le=260, description="身高(cm)")
    activity_level: Literal[
        "sedentary", "light", "moderate", "active", "very_active"
    ] | None = Field(
        None,
        description="日常活动水平:sedentary=久坐 / light=轻度 / moderate=中度 / "
                    "active=高度 / very_active=极高",
    )


class WorkoutCreate(BaseModel):
    """记一次运动。⭐ **两个维度:时长 + 这次运动的"量"(距离 / 个数)**。

    ⚠️ 2026-11-21(D188)用户原话:*「每项运动,应该能有两个不同维度的记录。
    比如跑步,一个是跑步的时间,还有一个就是跑的距离。仰卧起坐,一个是时间,
    另一个就个数。如果用户只提供了其中一个,你就只记一个就好,也不用估算。」*
    所以:
      * duration_min **不再是必填** —— 只记个数(或只记距离)也必须能落库;
      * 至少给一个维度(时长 / 距离 / 个数),一个都没有就**不写**,先追问用户;
      * **不估算**:缺时长就不算消耗,缺个数就不补个数 —— 有就有,没有就空着。
    """

    activity: str = Field(..., max_length=30, description="运动名称,例如 跑步、力量训练、瑜伽")
    duration_min: int | None = Field(None, ge=1, le=1440, description="时长(分钟),可选")
    ts: str | None = Field(None, max_length=40, description="发生时间 ISO8601;不传就是现在")
    distance_km: float | None = Field(
        None, ge=0, le=1000, description="距离(公里),可选;跑步/走路/骑车/游泳这类"
    )
    reps: int | None = Field(
        None, ge=1, le=5000, description="完成个数,可选;仰卧起坐/俯卧撑/深蹲/跳绳这类"
    )
    note: str | None = Field(None, max_length=200, description="备注")

    @model_validator(mode="after")
    def _at_least_one_dimension(self) -> "WorkoutCreate":
        """两个维度里**至少给一个**;一个都没有就不是一条运动记录,不落库。

        ⚠️ 这条校验是"不估算"的结构性保证(见 docs/决策记录.md D188):
        模型拿不到可填补的空子,只能回去问用户。文案写中文,是因为它会原样
        出现在工具错误里 —— 模型正是照着那句话去追问的。
        """
        if self.duration_min is None and self.distance_km is None and self.reps is None:
            raise ValueError(
                "这次运动一个维度都没有:时长(duration_min)/ 距离(distance_km)/ "
                "个数(reps)至少给一个。一个都没给时**先问用户**,不要估算、不要编。"
            )
        return self


class FoodLogCreate(BaseModel):
    """记一次进食。食物进你自己的库,不依赖外部表。"""

    food_name: str = Field(..., max_length=60, description="食物名称")
    amount: float = Field(1.0, gt=0, le=100, description="份数")
    unit: str = Field("份", max_length=10, description="单位,例如 份/克/毫升")
    kcal_per_unit: float | None = Field(
        None, ge=0, description="每份热量。第一次填了就进食物库,以后不用再填。"
    )
    meal_type: Literal["早餐", "午餐", "晚餐", "加餐"] | None = Field(
        None, description="哪一餐"
    )
    ts: str | None = Field(None, max_length=40, description="时间 ISO8601;不传就是现在")


class TrendQuery(BaseModel):
    metric_code: str = Field("weight", max_length=30, description="要看的指标,默认体重")
    days: int = Field(90, ge=7, le=730, description="回看多少天")
    with_coverage: bool = Field(
        False,
        description="额外给一份覆盖域:健康数据一共多少条记录、最早最晚到什么时候(曲线为空时尤其该看一眼)。",
    )


class TrendPointRead(BaseModel):
    day: str
    value: float
    trend: float


class TrendResult(BaseModel):
    metric_code: str
    unit: str
    days: int
    points: list[TrendPointRead] = Field(default_factory=list)
    weekly_rate: float | None = Field(
        None, description="按趋势线估的周变化(单位/周);正数是涨"
    )
    trend_lag_days: int = Field(10, description="⚠️ 平滑曲线有约 10 天滞后,必须告诉用户")
    summary: str = ""
    source: str = "health"


class EnergySummary(BaseModel):
    period_days: int
    bmr: float
    activity: str
    tdee: float
    intake_kcal: float
    burn_kcal: float
    deficit_kcal: float
    deficit_range: list[float] = Field(default_factory=list)
    basis: str = Field("formula", description="formula=公式估算;history=用你自己的历史反推")
    note: str = ""
    disclaimer: str = ""
    source: str = "health"


# --- M28:从 iPhone 健康同步进来的数据 -------------------------------------
#: 同步接口认的"每天一个数"的字段。⭐ 这是给 iPhone「快捷指令」看的契约:
#: 那边只要拼一个字典 {"date": "...", "steps": 8123, ...} 就行。
#: 没填的字段 = 今天没这个数据,**不是 0** —— 两者必须分开(0 会把平均值拉低)。
DAY_METRIC_FIELDS: tuple[str, ...] = (
    "steps", "distance_km", "active_kcal", "basal_kcal", "flights",
    "exercise_min", "stand_hours", "sleep_hours", "sleep_inbed", "sleep_midpoint",
    "resting_hr", "hrv", "vo2max", "respiratory_rate", "oxygen_saturation",
    "weight", "body_fat", "waist", "height", "bp_sys", "bp_dia", "heart_rate",
)


class SyncSample(BaseModel):
    """一条原始观测。字段用苹果的写法也行("type"/"startDate"),服务层会认。"""

    metric: str = Field(..., max_length=60, description="指标:steps / weight / sleep_hours …;也认 HK identifier")
    value: float = Field(..., description="数值(按我们存的原生单位:kg / km / kcal / h …)")
    start: str | None = Field(None, max_length=40, description="开始时间 ISO8601;不传就是收到的时间")
    end: str | None = Field(None, max_length=40, description="结束时间,可选")
    unit: str | None = Field(None, max_length=20, description="单位,可选;认不出就不换算")


class SyncDay(BaseModel):
    """一天一个汇总。这是快捷指令最省事的写法:一天一次,重复推同一个日期 = 覆盖。

    ⭐ date 可以为空 = **今天**(按服务端时区)。快捷指令里"格式化日期"那一步
    因此可以省掉 —— 早上 8 点推的就是今天。
    """

    date: str | None = Field(None, max_length=20, description="本地日期 YYYY-MM-DD;不传就是今天")
    steps: float | None = None
    distance_km: float | None = None
    active_kcal: float | None = None
    basal_kcal: float | None = None
    flights: float | None = None
    exercise_min: float | None = None
    stand_hours: float | None = None
    sleep_hours: float | None = None
    sleep_inbed: float | None = None
    sleep_midpoint: float | None = None
    resting_hr: float | None = None
    hrv: float | None = None
    vo2max: float | None = None
    respiratory_rate: float | None = None
    oxygen_saturation: float | None = None
    weight: float | None = None
    body_fat: float | None = None
    waist: float | None = None
    height: float | None = None
    bp_sys: float | None = None
    bp_dia: float | None = None
    heart_rate: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _blank_metrics_are_none(cls, data: Any) -> Any:
        """快捷指令把"没有数据"的变量发成空字符串 —— 当成"没有这一项",不当成错误。

        ⚠️ 2026-11-14 真机:没有锻炼数据时「锻炼」变量是空的,JSON 里变成
        `exercise_min: ""`,后端 float 解析直接 400「报文格式不对」。
        用户看不懂这句话,也不该被逼着手动删字段。
        """
        if not isinstance(data, dict):
            return data
        cleaned = dict(data)
        for key, value in list(cleaned.items()):
            if key == "date":
                if isinstance(value, str) and not value.strip():
                    cleaned[key] = None
                continue
            if key not in DAY_METRIC_FIELDS or not isinstance(value, str):
                continue
            text = value.strip().replace(",", "")
            cleaned[key] = None if not text else text
        return cleaned


class SyncWorkout(BaseModel):
    activity: str = Field(..., max_length=40, description="运动名,例如 跑步;也认 HK 类型")
    duration_min: float = Field(..., gt=0, le=1440)
    start: str | None = Field(None, max_length=40, description="开始时间 ISO8601")
    distance_km: float | None = Field(None, ge=0)
    kcal: float | None = Field(None, ge=0)
    external_id: str | None = Field(None, max_length=80, description="设备侧身份;不传就按 类型+开始时间 生成")


class HealthSyncPayload(SyncDay):
    """POST /api/health/sync 的请求体。

    ⭐ 既可**扁平**(直接给 date/steps/... 一个对象),也可**分组**(days/samples/workouts)。
    扁平是给快捷指令的:那 4 个字段直接填进"请求体 JSON",不用手写嵌套数组。

    ⚠️ 三条设计取舍:
      * days / samples / workouts 三选一都行,也可以同时给(补历史时用 samples);
      * 上限是**防滥用**的,不是业务限制 —— 一次推 400 天足够覆盖断网半年;
      * 不认识的多余字段直接忽略(pydantic 默认),方便第三方 App 附送元数据。
    """

    source: str = Field("shortcut", max_length=40, description="shortcut / export / app / manual")
    device: str = Field("iPhone", max_length=40)
    sent_at: str | None = Field(None, max_length=40)
    days: list[SyncDay] = Field(default_factory=list, max_length=400)
    samples: list[SyncSample] = Field(default_factory=list, max_length=5000)
    workouts: list[SyncWorkout] = Field(default_factory=list, max_length=400)


class HealthGoalCreate(BaseModel):
    metric: str = Field("weight", max_length=30, description="目标指标")
    target_value: float = Field(..., description="目标值")
    start_value: float | None = Field(None, description="起始值")
    target_date: str | None = Field(None, max_length=20, description="目标日期 YYYY-MM-DD")
