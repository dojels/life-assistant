"""Apple Health 导出解析。

⚠️ 四条必须遵守的(能力域 §9.2):
  1. **苹果从未官方文档化 export.xml 的 schema**(社区逆向),所以**只认白名单**;
  2. 文件可达数百 MB -> **流式解析**,绝不 read() 全文;支持直接吃 export.zip;
  3. **未知类型跳过而不是崩溃** —— 苹果随时可能加新的记录类型;
  4. **单位必须换算** —— 同一种指标,美国区导出可能是 lb / mi / °F,照原数字入库
     就是静默错一个量级(2026-11-11 这次把单位表显式写出来)。

日期是苹果自有格式("2024-01-01 08:00:00 +0800"),带时区偏移;
夜间睡眠要**按起床日归属**(跨午夜归到第二天早上,这是最常见的归属 bug)。

三种元素都认:
  <Record>          —— 测量与睡眠分期(睡眠是 HKCategoryType,不是数字)
  <Workout>         —— 一次运动;消耗/距离可能在属性里,也可能在子元素里
  <ActivitySummary> —— 每日活动环汇总(⚠️ 与步数/活动能量的 Record 重复,不收)
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Iterator
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from ... import config

# --- 指标白名单 -----------------------------------------------------------
#: HK identifier -> (我们的 metric_code, 期望单位)。单位只用于换算;
#: 导出里写别的单位时查 UNIT_FACTORS,查不到就**跳过**(不猜)。
KNOWN_QUANTITY_TYPES: dict[str, tuple[str, str]] = {
    "HKQuantityTypeIdentifierBodyMass": ("weight", "kg"),
    "HKQuantityTypeIdentifierBodyFatPercentage": ("body_fat", "%"),
    "HKQuantityTypeIdentifierWaistCircumference": ("waist", "cm"),
    "HKQuantityTypeIdentifierHeight": ("height", "cm"),
    "HKQuantityTypeIdentifierRestingHeartRate": ("resting_hr", "count/min"),
    "HKQuantityTypeIdentifierHeartRate": ("heart_rate", "count/min"),
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": ("hrv", "ms"),
    "HKQuantityTypeIdentifierBloodPressureSystolic": ("bp_sys", "mmHg"),
    "HKQuantityTypeIdentifierBloodPressureDiastolic": ("bp_dia", "mmHg"),
    "HKQuantityTypeIdentifierOxygenSaturation": ("oxygen_saturation", "%"),
    "HKQuantityTypeIdentifierRespiratoryRate": ("respiratory_rate", "count/min"),
    "HKQuantityTypeIdentifierVO2Max": ("vo2max", "mL/kg/min"),
    "HKQuantityTypeIdentifierStepCount": ("steps", "count"),
    "HKQuantityTypeIdentifierDistanceWalkingRunning": ("distance_km", "km"),
    "HKQuantityTypeIdentifierActiveEnergyBurned": ("active_kcal", "kcal"),
    "HKQuantityTypeIdentifierBasalEnergyBurned": ("basal_kcal", "kcal"),
    "HKQuantityTypeIdentifierFlightsClimbed": ("flights", "count"),
    "HKQuantityTypeIdentifierAppleExerciseTime": ("exercise_min", "min"),
    "HKQuantityTypeIdentifierAppleStandTime": ("stand_hours", "h"),
}

#: 单位换算表。**每一种指标都要能在这张表里找到自己的原生单位**;
#: 找不到的单位不硬套(宁可少导一条,不要静默错一个量级)。
UNIT_FACTORS: dict[str, dict[str, float]] = {
    "weight": {"kg": 1.0, "lb": 0.45359237, "lbs": 0.45359237, "g": 0.001, "stone": 6.35029318},
    "height": {"cm": 1.0, "in": 2.54, "m": 100.0, "ft": 30.48},
    "waist": {"cm": 1.0, "in": 2.54, "m": 100.0},
    "distance_km": {"km": 1.0, "mi": 1.609344, "m": 0.001, "ft": 0.0003048, "yd": 0.0009144},
    "active_kcal": {"kcal": 1.0, "Cal": 1.0, "kJ": 0.239005736},
    "basal_kcal": {"kcal": 1.0, "Cal": 1.0, "kJ": 0.239005736},
    "bp_sys": {"mmHg": 1.0, "kPa": 7.50061683},
    "bp_dia": {"mmHg": 1.0, "kPa": 7.50061683},
    "steps": {"count": 1.0, "": 1.0},
    "flights": {"count": 1.0, "": 1.0},
    "exercise_min": {"min": 1.0, "h": 60.0, "s": 1.0 / 60.0},
    # ⚠️ 苹果的「站立时间」是**分钟**(每小时一条,值常常是 1),我们存小时。
    #    以前这张表里没有 stand_hours,换算时落回原值 —— 1 分钟被当成 1 小时。
    "stand_hours": {"h": 1.0, "min": 1.0 / 60.0, "s": 1.0 / 3600.0},
    "resting_hr": {"count/min": 1.0, "bpm": 1.0},
    "heart_rate": {"count/min": 1.0, "bpm": 1.0},
    "hrv": {"ms": 1.0, "s": 1000.0},
    "respiratory_rate": {"count/min": 1.0, "bpm": 1.0},
    "vo2max": {"mL/kg/min": 1.0, "ml/kg/min": 1.0, "mL/min·kg": 1.0},
}

#: 百分比类:苹果的存储口径在不同版本/来源间不一致(0.185 与 18.5 都见过,
#: 实测:同一个人的体脂,含 0.185 的导出与含 18.5 的不是同一种写法)。
#: 用**量级**判:<=1 当比例,>1 当百分数。1% 的体脂与 100% 的血氧在现实中都不存在,
#: 所以这条启发式不会把正常人判错。
PERCENT_METRICS = ("body_fat", "oxygen_saturation")

#: 睡眠分期里算"睡着了"的取值(iOS 16 起分 Core/Deep/REM,老版本是 Asleep)。
SLEEP_ASLEEP_VALUES = (
    "HKCategoryValueSleepAnalysisAsleep",
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepREM",
)
SLEEP_INBED_VALUES = ("HKCategoryValueSleepAnalysisInBed",)
#: ⭐ 这些指标在苹果导出里是"一天切几十上百段"的:一年几万条,十年几十万条。
#: 它们的语义是**一天合计**(步数相加、距离相加),所以导入时就按天合成一行 ——
#: 否则十年导出 = 70 万行,既慢又占空间,页面读数还一样。
#: (2026-11-11 真机:用户 12.5MB 的导出里有 71.5 万条记录,299MB XML。)
DAILY_SUM_METRICS = frozenset({
    "steps", "distance_km", "active_kcal", "basal_kcal",
    "flights", "exercise_min", "stand_hours",
})

#: 一段睡眠和下一段之间超过这个间隔,就当成两觉(午睡不会和夜间睡眠并成一段)。
SLEEP_GAP_SPLIT = timedelta(hours=3)
#: 短于这个的"一觉"不记(打盹十几分钟会把"昨夜睡眠"刷成 0.2 小时)。
MIN_SLEEP_HOURS = 0.5

#: 苹果的运动类型 -> (我们的运动名, MET)。MET 来自 Compendium(可商用)。
#: ⚠️ 名字必须与 app_activity_types 的种子对得上,否则会退回 4.0 的中等强度。
WORKOUT_TYPES: dict[str, tuple[str, float]] = {
    "HKWorkoutActivityTypeWalking": ("走路", 3.5),
    "HKWorkoutActivityTypeRunning": ("跑步", 8.3),
    "HKWorkoutActivityTypeHiking": ("爬山", 6.0),
    "HKWorkoutActivityTypeCycling": ("骑车", 6.8),
    "HKWorkoutActivityTypeSwimming": ("游泳", 7.0),
    "HKWorkoutActivityTypeRowing": ("划船", 7.0),
    "HKWorkoutActivityTypeElliptical": ("椭圆机", 5.0),
    "HKWorkoutActivityTypeStairClimbing": ("爬楼", 8.0),
    "HKWorkoutActivityTypeJumpRope": ("跳绳", 11.0),
    "HKWorkoutActivityTypeTraditionalStrengthTraining": ("力量训练", 5.0),
    "HKWorkoutActivityTypeFunctionalStrengthTraining": ("力量训练", 5.0),
    "HKWorkoutActivityTypeCoreTraining": ("核心训练", 4.0),
    "HKWorkoutActivityTypeHighIntensityIntervalTraining": ("高强度间歇", 8.0),
    "HKWorkoutActivityTypeYoga": ("瑜伽", 2.5),
    "HKWorkoutActivityTypeDance": ("跳舞", 5.0),
    "HKWorkoutActivityTypeCardioDance": ("跳舞", 5.0),
    "HKWorkoutActivityTypeMindAndBody": ("瑜伽", 2.5),
}
WORKOUT_FALLBACK = ("其他运动", 4.0)

APPLE_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%d %H:%M:%S",
)

#: 日期解析要认的本地时区:导出里带偏移就用它,不带的按应用时区补。
DEFAULT_TZID = config.DEFAULT_TIMEZONE


def parse_apple_time(text: str) -> datetime | None:
    """苹果自有时间格式 -> datetime;认不出来返回 None(不抛)。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    for pattern in APPLE_TIME_FORMATS:
        try:
            return datetime.strptime(raw, pattern)
        except ValueError:
            continue
    # iOS 新版本偶尔会写 ISO(带 T 或小数秒) —— 多认一种,少丢一条。
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _local_date(moment: datetime, tzid: str) -> str:
    return moment.astimezone(ZoneInfo(tzid)).date().isoformat()


def _as_local(moment: datetime, tzid: str) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=ZoneInfo(tzid))
    return moment.astimezone(ZoneInfo(tzid))


def convert_value(metric_code: str, value: float, unit: str) -> float | None:
    """按单位换算成我们存的原生单位。认不出单位 -> None(交给 skipped_bad)。"""
    unit = str(unit or "").strip()
    if metric_code in PERCENT_METRICS:
        number = float(value)
        # 比例写法(0.185)-> 百分数;已经是 18.5 就原样。
        return number * 100.0 if 0.0 < number <= 1.0 else number
    table = UNIT_FACTORS.get(metric_code)
    if table is None:
        return float(value)
    factor = table.get(unit)
    if factor is None:
        return None
    return float(value) * factor


# --- 结果对象 -------------------------------------------------------------
@dataclass
class ImportedSample:
    metric_code: str
    value: float
    measured_at: str
    local_date: str
    #: 这一条是"当天合计"(不是一次原始观测)。导入时打上"每日汇总"标记,
    #: 让快捷指令推的当日总数能替换它,而不是两份相加。
    daily: bool = False


@dataclass
class ImportedWorkout:
    kind: str
    met: float
    start: str
    local_date: str
    duration_min: int
    distance_km: float | None = None
    kcal: float | None = None
    external_id: str = ""


@dataclass
class SleepNight:
    """**一夜**睡眠(跨午夜归到起床那天)。"""

    local_date: str          # 起床日
    asleep_hours: float
    in_bed_hours: float | None
    bedtime: str             # ISO,入睡
    wake: str                # ISO,起床
    midpoint_hour: float     # 睡眠中点(相对起床日 00:00 的小时数,0~24)


@dataclass
class AppleHealthReport:
    total_records: int = 0          # <Record> 数(含睡眠分期)
    total_workouts: int = 0
    imported: int = 0               # 可入库的测量样本数(不含睡眠/运动)
    skipped_unknown: int = 0
    skipped_bad: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    samples: list[ImportedSample] = field(default_factory=list)
    workouts: list[ImportedWorkout] = field(default_factory=list)
    nights: list[SleepNight] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total_records": self.total_records,
            "total_workouts": self.total_workouts,
            "imported": self.imported,
            "skipped_unknown": self.skipped_unknown,
            "skipped_bad": self.skipped_bad,
            "by_type": dict(self.by_type),
            "sleep_nights": len(self.nights),
            "workouts": len(self.workouts),
        }


# --- 流式读取 -------------------------------------------------------------
#: 套娃 zip 只允许解到内存里,且不能超过这个大小(再大就请用户先解压)。
NESTED_ZIP_LIMIT = 300 * 1024 * 1024


def _basename(name: str) -> str:
    """zip 成员名归一化:Windows 压出来的可能用反斜杠。"""
    return str(name or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()


def _pick_xml(archive: zipfile.ZipFile) -> zipfile.ZipInfo | None:
    """在 zip 里找那份健康导出 xml。

    优先级:① 文件名正好是 export.xml;② 其它 .xml(排除 export_cda.xml)里最大的那个。
    ⭐ ② 是 2026-11-11 真机反馈加的:用户把「健康」导出的文件夹**重新压了一次**,
    或者中文系统里被改名,文件名不再是 export.xml —— 旧逻辑直接判"没有 export.xml"。
    """
    files = [info for info in archive.infolist() if not info.is_dir()]
    exact = [info for info in files if _basename(info.filename) == "export.xml"]
    if exact:
        return sorted(exact, key=lambda info: len(info.filename))[0]
    others = [
        info for info in files
        if _basename(info.filename).endswith(".xml") and "cda" not in _basename(info.filename)
    ]
    if others:
        return max(others, key=lambda info: info.file_size)
    return None


def _describe(archive: zipfile.ZipFile, limit: int = 8) -> str:
    names = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        size = info.file_size
        text = str(size) + "B" if size < 1024 else (str(round(size / 1024)) + "KB"
                                                    if size < 1024 * 1024 else str(round(size / 1024 / 1024)) + "MB")
        names.append(info.filename + "(" + text + ")")
        if len(names) >= limit:
            break
    return "、".join(names) if names else "(空压缩包)"


def _open_export(path: str | Path) -> tuple[object, str | None, Callable[[], None]]:
    """返回(可读的二进制流, zip 成员名, 关闭函数)。

    zip 走 ZipFile.open(成员),**不解压到磁盘**(几百 MB 的临时文件没必要,
    而且解压到 data/ 会把健康数据留在那里)。

    ⚠️ 关闭函数必须连 ZipFile 一起关:只关成员流,Windows 上这个 zip 文件
    仍然被占用,上传后的临时文件删不掉(WinError 32,实测踩过)。
    """
    target = Path(path)
    if not zipfile.is_zipfile(target):
        stream = target.open("rb")

        def close_plain() -> None:
            stream.close()

        return stream, None, close_plain

    archive = zipfile.ZipFile(target)
    info = _pick_xml(archive)
    if info is not None:
        stream = archive.open(info)

        def close() -> None:
            stream.close()
            archive.close()

        return stream, info.filename, close

    # 套娃:用户可能把「导出.zip」又压了一层(手机上“压缩”很容易这样)。
    nested = [
        item for item in archive.infolist()
        if not item.is_dir() and _basename(item.filename).endswith(".zip")
    ]
    if nested:
        inner_info = max(nested, key=lambda item: item.file_size)
        if inner_info.file_size > NESTED_ZIP_LIMIT:
            archive.close()
            raise ValueError(
                "压缩包里套了一个 " + str(round(inner_info.file_size / 1024 / 1024))
                + "MB 的 zip,太大了;请先解压,再把里面的 export.zip 单独传上来"
            )
        try:
            inner = zipfile.ZipFile(io.BytesIO(archive.read(inner_info)))
        except zipfile.BadZipFile:
            inner = None
        if inner is not None:
            inner_xml = _pick_xml(inner)
            if inner_xml is not None:
                stream = inner.open(inner_xml)

                def close_nested() -> None:
                    stream.close()
                    inner.close()
                    archive.close()

                return (
                    stream,
                    inner_info.filename + " → " + inner_xml.filename,
                    close_nested,
                )
            inner.close()

    described = _describe(archive)
    archive.close()
    raise ValueError(
        "这个压缩包里没有 export.xml。里面有:" + described
        + "。请把「健康」App 导出的整份 zip 直接传上来 —— 不要解压后再压一次,"
        "也不要选到别的东西"
    )


def iter_records(path: str | Path) -> Iterator[tuple[str, str, str]]:
    """流式吐出 (type, startDate, value)。内存占用与文件大小无关。

    ⚠️ 保留这个函数是因为它是 M4.4 的老接口(测试与外部脚本在用);
    真正需要单位/睡眠/运动的调用方用 parse_apple_export()。
    """
    stream, _member, close = _open_export(path)
    try:
        for _event, element in ElementTree.iterparse(stream, events=("end",)):
            if not element.tag.endswith("Record"):
                continue
            yield (
                element.attrib.get("type") or "",
                element.attrib.get("startDate") or "",
                element.attrib.get("value") or "",
            )
            element.clear()
    finally:
        close()


@dataclass
class _SleepSpan:
    start: datetime
    end: datetime
    kind: str


def _merge_hours(spans: Iterable[tuple[datetime, datetime]]) -> float:
    """合并重叠区间再求和 —— 同一段睡眠被两个 App 都写了,不该算两遍。"""
    ordered = sorted(spans)
    total = 0.0
    current_start: datetime | None = None
    current_end: datetime | None = None
    for start, end in ordered:
        if end <= start:
            continue
        if current_start is None or start > current_end:
            if current_start is not None and current_end is not None:
                total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_start is not None and current_end is not None:
        total += (current_end - current_start).total_seconds()
    return total / 3600.0


def _sleep_nights(spans: list[_SleepSpan], tzid: str) -> list[SleepNight]:
    """把分期区间折成"一夜"。

    三件事:① 按**起床日**归属;② 同一夜里重叠的分期先合并(不然深睡+核心睡会翻倍);
    ③ 一夜里可能还有午睡 —— 间隔超过 3 小时算另一觉,只取最长的那段当"昨夜睡眠"。
    """
    by_day: dict[str, list[_SleepSpan]] = {}
    for span in spans:
        day = _local_date(span.end, tzid)
        by_day.setdefault(day, []).append(span)

    nights: list[SleepNight] = []
    for day, items in sorted(by_day.items()):
        asleep = [s for s in items if s.kind == "asleep"]
        inbed = [s for s in items if s.kind == "inbed"]
        # 只有卧床、没有分期(用「睡眠定时」而没有任何设备写分期时就是这样):
        # 卧床时长本身就是唯一能拿到的事实,用它,并在页面上说明来源。
        if not asleep:
            if not inbed:
                continue
            hours = _merge_hours((s.start, s.end) for s in inbed)
            if hours < MIN_SLEEP_HOURS:
                continue
            start = min(s.start for s in inbed)
            end = max(s.end for s in inbed)
            nights.append(_night(day, hours, hours, start, end, tzid))
            continue

        # 按 3 小时间隔切成一觉一觉
        ordered = sorted(asleep, key=lambda s: s.start)
        clusters: list[list[_SleepSpan]] = [[ordered[0]]]
        for span in ordered[1:]:
            if span.start - clusters[-1][-1].end > SLEEP_GAP_SPLIT:
                clusters.append([span])
            else:
                clusters[-1].append(span)
        best = max(
            clusters,
            key=lambda group: _merge_hours((s.start, s.end) for s in group),
        )
        hours = _merge_hours((s.start, s.end) for s in best)
        if hours < MIN_SLEEP_HOURS:
            continue
        start = min(s.start for s in best)
        end = max(s.end for s in best)
        # 卧床区间只取与这一觉有重叠的那些(否则会把整晚的 InBed 全算进来)
        window = [s for s in inbed if s.end > start and s.start < end]
        inbed_hours = _merge_hours((s.start, s.end) for s in window) if window else None
        nights.append(_night(day, hours, inbed_hours, start, end, tzid))
    return nights


def _night(
    day: str, hours: float, inbed_hours: float | None,
    start: datetime, end: datetime, tzid: str,
) -> SleepNight:
    local_start = _as_local(start, tzid)
    local_end = _as_local(end, tzid)
    midnight = datetime.fromisoformat(day + "T00:00:00").replace(tzinfo=ZoneInfo(tzid))
    middle = local_start + (local_end - local_start) / 2
    return SleepNight(
        local_date=day,
        asleep_hours=round(hours, 2),
        in_bed_hours=round(inbed_hours, 2) if inbed_hours is not None else None,
        bedtime=local_start.isoformat(timespec="seconds"),
        wake=local_end.isoformat(timespec="seconds"),
        # 折到 0..24 的环上:23:00 睡、07:00 起,中点是 3.0(起床日);
        # 前一天 18:00 睡、02:00 起,中点是 22.0(仍是起床日 00:00 起算的 −2h)。
        midpoint_hour=round(((middle - midnight).total_seconds() / 3600.0) % 24.0, 2),
    )


def _workout_from_element(element: ElementTree.Element, tzid: str) -> ImportedWorkout | None:
    workout_type = element.attrib.get("workoutActivityType") or ""
    kind, met = WORKOUT_TYPES.get(workout_type, WORKOUT_FALLBACK)
    start_raw = element.attrib.get("startDate") or ""
    end_raw = element.attrib.get("endDate") or ""
    start = parse_apple_time(start_raw)
    end = parse_apple_time(end_raw)
    if start is None:
        return None

    def _stat(identifier: str) -> tuple[float | None, str]:
        """苹果新导出把总量放在 <WorkoutStatistics type=… sum=… unit=…> 里。

        ⚠️ 不用 findall("WorkoutStatistics"):带命名空间的导出会匹配不到
        (按本地名比较,与 Record/Workout 的判法一致)。
        """
        for child in element:
            if child.tag.rsplit("}", 1)[-1] != "WorkoutStatistics":
                continue
            if (child.attrib.get("type") or "") != identifier:
                continue
            try:
                return float(child.attrib.get("sum") or ""), child.attrib.get("unit") or ""
            except ValueError:
                return None, ""
        return None, ""

    duration_min: float | None = None
    raw_duration = element.attrib.get("duration")
    unit = (element.attrib.get("durationUnit") or "min").strip().lower()
    if raw_duration:
        try:
            duration_min = float(raw_duration) * {"min": 1.0, "s": 1 / 60.0, "h": 60.0}.get(unit, 1.0)
        except ValueError:
            duration_min = None
    if duration_min is None and end is not None:
        duration_min = (end - start).total_seconds() / 60.0
    if duration_min is None or duration_min <= 0:
        return None

    distance_km: float | None = None
    raw_distance = element.attrib.get("totalDistance")
    distance_unit = element.attrib.get("totalDistanceUnit") or "km"
    if raw_distance:
        try:
            distance_km = float(raw_distance) * UNIT_FACTORS["distance_km"].get(
                distance_unit.strip(), 1.0
            )
        except ValueError:
            distance_km = None
    if distance_km is None:
        sum_value, sum_unit = _stat("HKQuantityTypeIdentifierDistanceWalkingRunning")
        if sum_value is not None:
            distance_km = sum_value * UNIT_FACTORS["distance_km"].get(sum_unit.strip(), 1.0)

    kcal: float | None = None
    raw_kcal = element.attrib.get("totalEnergyBurned")
    kcal_unit = element.attrib.get("totalEnergyBurnedUnit") or "kcal"
    if raw_kcal:
        try:
            kcal = float(raw_kcal) * UNIT_FACTORS["active_kcal"].get(kcal_unit.strip(), 1.0)
        except ValueError:
            kcal = None
    if kcal is None:
        sum_value, sum_unit = _stat("HKQuantityTypeIdentifierActiveEnergyBurned")
        if sum_value is not None:
            kcal = sum_value * UNIT_FACTORS["active_kcal"].get(sum_unit.strip(), 1.0)

    local_start = _as_local(start, tzid)
    return ImportedWorkout(
        kind=kind,
        met=met,
        start=local_start.isoformat(timespec="seconds"),
        local_date=local_start.date().isoformat(),
        duration_min=max(1, int(round(duration_min))),
        distance_km=round(distance_km, 3) if distance_km is not None else None,
        kcal=round(kcal, 1) if kcal is not None else None,
        external_id=workout_type + "|" + local_start.isoformat(timespec="seconds"),
    )


def parse_apple_export(
    path: str | Path,
    *,
    tzid: str = DEFAULT_TZID,
    allow: dict[str, tuple[str, str]] | None = None,
    limit: int | None = None,
) -> AppleHealthReport:
    """一次流式扫描:测量 / 睡眠 / 运动各归各的。**不写数据库**。"""
    whitelist = allow or KNOWN_QUANTITY_TYPES
    report = AppleHealthReport()
    spans: list[_SleepSpan] = []
    #: (metric, local_date) -> 当天合计
    daily_totals: dict[tuple[str, str], float] = {}
    stream, _member, close = _open_export(path)
    try:
        for _event, element in ElementTree.iterparse(stream, events=("end",)):
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "Workout":
                report.total_workouts += 1
                workout = _workout_from_element(element, tzid)
                if workout is None:
                    report.skipped_bad += 1
                else:
                    report.workouts.append(workout)
                element.clear()
                continue
            if tag != "Record":
                continue
            report.total_records += 1
            record_type = element.attrib.get("type") or ""
            start_raw = element.attrib.get("startDate") or ""
            value_raw = element.attrib.get("value") or ""
            moment = parse_apple_time(start_raw)

            if record_type == "HKCategoryTypeIdentifierSleepAnalysis":
                # ⚠️ 睡眠不是数字:它是"从几点到几点、处于哪一期"的**区间**。
                end = parse_apple_time(element.attrib.get("endDate") or "")
                if moment is None or end is None:
                    report.skipped_bad += 1
                    element.clear()
                    continue
                if value_raw in SLEEP_ASLEEP_VALUES:
                    spans.append(_SleepSpan(moment, end, "asleep"))
                elif value_raw in SLEEP_INBED_VALUES:
                    spans.append(_SleepSpan(moment, end, "inbed"))
                else:
                    report.skipped_unknown += 1
                element.clear()
                continue

            mapping = whitelist.get(record_type)
            if mapping is None:
                report.skipped_unknown += 1
                element.clear()
                continue
            metric_code, _native_unit = mapping
            if moment is None:
                report.skipped_bad += 1
                element.clear()
                continue
            try:
                number = float(value_raw)
            except ValueError:
                report.skipped_bad += 1
                element.clear()
                continue
            converted = convert_value(metric_code, number, element.attrib.get("unit") or "")
            if converted is None:
                report.skipped_bad += 1
                element.clear()
                continue
            local = _as_local(moment, tzid)
            day = local.date().isoformat()
            if metric_code in DAILY_SUM_METRICS:
                key = (metric_code, day)
                daily_totals[key] = daily_totals.get(key, 0.0) + float(converted)
            else:
                report.samples.append(
                    ImportedSample(
                        metric_code=metric_code,
                        value=converted,
                        measured_at=local.isoformat(timespec="seconds"),
                        local_date=day,
                    )
                )
                report.imported += 1
            report.by_type[metric_code] = report.by_type.get(metric_code, 0) + 1
            element.clear()
            if limit is not None and report.imported >= limit:
                break
    finally:
        close()

    # 把"一天合计"折成每天一条样本(时间戳取当天本地 00:00,与快捷指令的当日总数同形)。
    midnight = ZoneInfo(tzid)
    for (metric_code, day), total in sorted(daily_totals.items()):
        report.samples.append(
            ImportedSample(
                metric_code=metric_code,
                value=total,
                measured_at=datetime.fromisoformat(day + "T00:00:00")
                .replace(tzinfo=midnight)
                .isoformat(timespec="seconds"),
                local_date=day,
                daily=True,
            )
        )
    report.imported = len(report.samples)
    report.nights = _sleep_nights(spans, tzid)
    return report


def import_apple_health(
    path: str | Path,
    *,
    allow: dict[str, tuple[str, str]] | None = None,
    limit: int | None = None,
) -> AppleHealthReport:
    """兼容 M4.4 的旧入口:解析出一批样本,**不做数据库写入**。"""
    return parse_apple_export(path, allow=allow, limit=limit)
