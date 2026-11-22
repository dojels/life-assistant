"""M4.4 验收测试:EWMA 趋势、MET、能量收支、Apple Health 导入、两条红线。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from la.domain.health.apple_health import (
    KNOWN_QUANTITY_TYPES,
    import_apple_health,
    parse_apple_time,
)
from la.domain.health.metrics import (
    ACTIVITY_FACTORS,
    DEFAULT_ALPHA,
    EWMA_LAG_DAYS,
    KCAL_PER_KG_FAT,
    MIN_DAYS_FOR_ESTIMATE,
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
from la.domain.schemas.health import (
    FoodLogCreate,
    MeasurementCreate,
    TrendQuery,
    WorkoutCreate,
)
from la.domain.services.health_service import (
    MEASUREMENT_CAVEAT,
    MEDICAL_DISCLAIMER,
    HealthError,
    HealthService,
    resolve_activity,
)
from pydantic import ValidationError

from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 8, 8, 0, tzinfo=TZ)


# ==========================================================================
# 纯函数
# ==========================================================================
class MetricsTestCase(unittest.TestCase):
    def test_ewma_formula(self):
        points = ewma_trend([("2026-10-01", 80.0), ("2026-10-02", 70.0)], alpha=0.1)
        self.assertAlmostEqual(points[0].trend, 80.0, places=6)
        self.assertAlmostEqual(points[1].trend, 80.0 + 0.1 * (70.0 - 80.0), places=6)

    def test_ewma_smooths_noise_but_lags(self):
        """⭐ 原始日体重噪声 ±1~2kg,而真实变化只有 0.5~1kg/周 —— 直接看原图必然误判。"""
        raw = [("2026-09-%02d" % day, 80.0 + (2.0 if day % 2 else -2.0)) for day in range(1, 61)]
        points = ewma_trend(raw, alpha=DEFAULT_ALPHA)
        # 只看后半段:前几个点是冷启动瞬态,不该拿来说事
        tail_raw = [value for _d, value in raw[30:]]
        tail_trend = [point.trend for point in points[30:]]
        spread_raw = max(tail_raw) - min(tail_raw)
        spread_trend = max(tail_trend) - min(tail_trend)
        self.assertGreater(spread_raw, 3.0)
        self.assertLess(spread_trend, spread_raw / 3, "趋势线必须把噪声压下去")

    def test_ewma_lag_is_documented(self):
        """⚠️ 阶跃变化要 ~10 天才走完 63% —— UI 必须说明,否则像 bug。

        这里用时间常数口径(1 − 1/e ≈ 63%),而不是"跨过某个阈值":
        α=0.1 时 (1−α)^n = 0.37 -> n ≈ 9.5 天,与文档里的 9~10 天一致。
        """
        samples = [("2026-09-%02d" % day, 80.0) for day in range(1, 31)]
        samples += [("2026-10-%02d" % day, 75.0) for day in range(1, 31)]
        points = ewma_trend(samples, alpha=DEFAULT_ALPHA)
        step_index = 30
        target = 80.0 - 0.63 * 5.0
        moved = next(
            (index for index, point in enumerate(points) if point.trend <= target), None
        )
        self.assertIsNotNone(moved, "趋势线必须最终跟上")
        lag = moved - step_index
        self.assertGreaterEqual(lag, EWMA_LAG_DAYS - 2)
        self.assertLessEqual(lag, EWMA_LAG_DAYS + 3)

    def test_weekly_rate_uses_trend_not_raw(self):
        samples = [("2026-09-%02d" % day, 80.0 - 0.1 * day) for day in range(1, 29)]
        rate = weekly_rate(ewma_trend(samples))
        self.assertIsNotNone(rate)
        self.assertLess(rate, 0, "体重在降,速率应当为负")
        self.assertAlmostEqual(rate, -0.7, delta=0.3)

    def test_met_minutes_do_not_depend_on_weight(self):
        self.assertAlmostEqual(met_minutes(8.3, 30), 249.0, places=9)
        self.assertAlmostEqual(met_minutes(3.5, 60), 210.0, places=9)

    def test_kcal_scales_with_weight(self):
        light = kcal_from_met(8.3, 30, 60)
        heavy = kcal_from_met(8.3, 30, 90)
        self.assertGreater(heavy, light)
        self.assertAlmostEqual(light, 8.3 * 60 * 0.5, places=6)

    def test_intensity_bands(self):
        self.assertEqual(intensity_label(2.5), "轻")
        self.assertEqual(intensity_label(4.0), "中")
        self.assertEqual(intensity_label(8.3), "剧烈")

    def test_mifflin_st_jeor(self):
        male = mifflin_st_jeor(70, 175, 30, "男")
        female = mifflin_st_jeor(70, 175, 30, "女")
        self.assertAlmostEqual(male, 10 * 70 + 6.25 * 175 - 5 * 30 + 5, places=6)
        self.assertAlmostEqual(female - male, -166, places=6)

    def test_tdee_activity_factors(self):
        self.assertAlmostEqual(tdee(1600, "sedentary"), 1600 * ACTIVITY_FACTORS["sedentary"])
        self.assertAlmostEqual(tdee(1600, "very_active"), 1600 * 1.9)

    def test_energy_balance_has_honest_range(self):
        balance = energy_balance(bmr=1600, activity="light", intake_kcal=14000,
                                 burn_kcal=1500, days=7,
                                 note="公式估算,日误差常达 ±400 千卡")
        low, high = balance.deficit_range
        self.assertEqual((high - low) / 2, 400, "必须给出 ±400 的诚实区间")
        self.assertIn("±", balance.as_dict()["note"])

    def test_history_tdee_needs_21_days(self):
        self.assertIsNone(
            tdee_from_history(average_intake_kcal=2000, weight_change_kg=-1, days=10)
        )
        value = tdee_from_history(
            average_intake_kcal=2000, weight_change_kg=-1, days=28
        )
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value, 2000 + 1 * KCAL_PER_KG_FAT / 28, places=3)
        self.assertEqual(MIN_DAYS_FOR_ESTIMATE, 21)


# ==========================================================================
# 服务
# ==========================================================================
class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.service = HealthService(self.conn, now_provider=lambda: NOW)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_measurement_written_with_local_date(self):
        out = self.service.log_measurement(
            MeasurementCreate(metric_code="weight", value=72.5)
        )
        self.assertEqual(out["unit"], "kg")
        self.assertEqual(out["local_date"], "2026-10-08")

    def test_unknown_metric_is_rejected_with_hints(self):
        with self.assertRaises(HealthError) as ctx:
            self.service.log_measurement({"metric_code": "身高体重", "value": 1})
        self.assertIn("weight", str(ctx.exception))

    def test_duplicate_measurement_upserts(self):
        payload = {"metric_code": "weight", "value": 72.5, "measured_at": "2026-10-08T08:00:00+08:00"}
        self.service.log_measurement(payload)
        self.service.log_measurement({**payload, "value": 72.8})
        count = self.conn.execute("SELECT count(*) FROM app_measurements").fetchone()[0]
        self.assertEqual(count, 1, "同一时刻同一来源只保留一条")
        self.assertAlmostEqual(self.service.latest("weight")["value"], 72.8, places=6)

    def test_daily_values_average_multiple_readings(self):
        for hour, value in ((7, 72.0), (21, 73.0)):
            self.service.log_measurement({
                "metric_code": "weight", "value": value,
                "measured_at": "2026-10-08T%02d:00:00+08:00" % hour,
            })
        daily = self.service.daily_values("weight")
        self.assertEqual(len(daily), 1, "早晚各称一次不该算两个点")
        self.assertAlmostEqual(daily[0][1], 72.5, places=6)

    def test_trend_discloses_lag(self):
        for day in range(1, 15):
            self.service.log_measurement({
                "metric_code": "weight", "value": 80.0 - 0.05 * day,
                "measured_at": "2026-09-%02dT08:00:00+08:00" % day,
            })
        result = self.service.trend(TrendQuery(metric_code="weight", days=365))
        self.assertGreater(len(result.points), 5)
        self.assertIn("滞后", result.summary)
        self.assertEqual(result.trend_lag_days, EWMA_LAG_DAYS)

    def test_trend_on_empty_data_is_honest(self):
        result = self.service.trend(TrendQuery(metric_code="waist"))
        self.assertEqual(result.points, [])
        self.assertIn("没有", result.summary)

    def test_workout_computes_met_minutes_and_kcal(self):
        self.service.log_measurement({"metric_code": "weight", "value": 70.0})
        out = self.service.log_workout(WorkoutCreate(activity="跑步", duration_min=30))
        self.assertEqual(out["met"], 8.3)
        self.assertEqual(out["met_minutes"], 249.0)
        self.assertEqual(out["intensity"], "剧烈")
        self.assertIn("kcal_estimate", out)
        self.assertAlmostEqual(out["kcal_estimate"], round(8.3 * 70 * 0.5))

    # --- 2026-11-21(D188):运动的两个维度 -------------------------------------
    def test_workout_reps_only_is_recorded_without_estimation(self):
        """只记个数就只存个数:不补时长、不估消耗(用户原话:不用估算)。"""
        out = self.service.log_workout({"activity": "仰卧起坐", "reps": 10})
        self.assertEqual(out["reps"], 10)
        self.assertIsNone(out["duration_min"])
        self.assertIsNone(out["met_minutes"])
        self.assertEqual(out["amount_text"], "10 个")
        self.assertNotIn("kcal_estimate", out)
        self.assertIn("没记时长", str(out.get("calorie_note") or ""))
        row = self.conn.execute(
            "SELECT duration_min, reps FROM app_workouts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNone(row["duration_min"], "没给时长必须是 NULL —— 0 会被当成『做了但没时长』")
        self.assertEqual(row["reps"], 10)

    def test_workout_distance_only_is_recorded(self):
        out = self.service.log_workout({"activity": "跑步", "distance_km": 5.2})
        self.assertIsNone(out["duration_min"])
        self.assertEqual(out["distance_km"], 5.2)
        self.assertEqual(out["amount_text"], "5.2 公里")
        self.assertIsNone(out["met_minutes"], "MET-分钟 = MET × 分钟,没有分钟就没有它")

    def test_workout_keeps_both_dimensions(self):
        out = self.service.log_workout(
            {"activity": "跑步", "duration_min": 30, "distance_km": 5.2})
        self.assertEqual(out["amount_text"], "30 分钟 · 5.2 公里")
        self.assertEqual(out["met_minutes"], 249.0)
        row = self.conn.execute(
            "SELECT duration_min, distance_km FROM app_workouts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual((row["duration_min"], row["distance_km"]), (30, 5.2))

    def test_workout_without_any_dimension_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            self.service.log_workout({"activity": "仰卧起坐"})
        self.assertIn("至少给一个", str(ctx.exception))
        count = self.conn.execute("SELECT count(*) FROM app_workouts").fetchone()[0]
        self.assertEqual(count, 0, "一个维度都没有就不是一条运动记录 —— 先追问,不要写")

    def test_recent_workouts_carry_the_second_dimension(self):
        self.service.log_workout({"activity": "仰卧起坐", "reps": 10})
        self.service.log_workout({"activity": "跑步", "duration_min": 30, "distance_km": 5})
        rows = self.service.recent_workouts(limit=5)
        # 两条的 ts 相同(测试时钟是固定的),所以按项目名取,不赌排序。
        by_kind = {row["kind"]: row for row in rows}
        self.assertEqual(by_kind["跑步"]["amount_text"], "30 分钟 · 5 公里")
        self.assertEqual(by_kind["跑步"]["distance_text"], "5")
        self.assertEqual(by_kind["仰卧起坐"]["amount_text"], "10 个")

    def test_untimed_workout_does_not_invent_met_minutes(self):
        """没时长的那次对"运动量"的贡献是 0,但它是**一条记录**,不是没记录。"""
        self.service.log_workout({"activity": "仰卧起坐", "reps": 10})
        summary = self.service.daily_summary("2026-10-08")
        self.assertEqual(summary["met_minutes"], 0.0)
        self.assertEqual(summary["workouts"], ["仰卧起坐"])

    def test_bodyweight_activities_have_real_met(self):
        """⭐ 2026-11-21(D188 补记):自重动作要有真的强度系数。

        以前它们不在 app_activity_types 里,log_workout 会退回 4.0 —— 记录本身没错,
        但"强度"那一栏是编的;有分钟数时 kcal 也跟着偏。
        """
        for name, expected in (
            ("仰卧起坐", 3.8), ("俯卧撑", 3.8), ("深蹲", 5.0),
            ("引体向上", 7.5), ("平板支撑", 2.8),
        ):
            out = self.service.log_workout({"activity": name, "reps": 10})
            self.assertEqual(out["met"], expected, name + " 退回了兜底值 —— 字典里没有它")
        # 英文/变体也认(第三方 App 推的是 PushUp / sit-ups 这种写法)
        for alias, expected in (
            ("PushUp", ("俯卧撑", 3.8)),
            ("sit-ups", ("仰卧起坐", 3.8)),
            ("squats", ("深蹲", 5.0)),
            ("Plank", ("平板支撑", 2.8)),
        ):
            self.assertEqual(resolve_activity(alias), expected, alias + " 没收敛到字典里的那一行")

    def test_unknown_activity_falls_back_but_still_records(self):
        out = self.service.log_workout(WorkoutCreate(activity="没听过的运动", duration_min=20))
        self.assertGreater(out["met"], 0)
        self.assertTrue(self.service.daily_summary()["workouts"])

    def test_food_is_learned_on_first_use(self):
        first = self.service.log_food(FoodLogCreate(food_name="燕麦粥", kcal_per_unit=180))
        self.assertEqual(first["kcal_estimate"], 180)
        second = self.service.log_food(FoodLogCreate(food_name="燕麦粥", amount=2))
        self.assertEqual(second["kcal_estimate"], 360, "第二次不用再填热量")
        foods = self.conn.execute("SELECT count(*) FROM app_foods").fetchone()[0]
        self.assertEqual(foods, 1)

    def test_food_without_kcal_says_so(self):
        out = self.service.log_food(FoodLogCreate(food_name="没热量的东西"))
        self.assertIsNone(out["kcal_estimate"])
        self.assertIn("还没有热量", out["caveat"])

    def test_energy_summary_needs_profile(self):
        summary = self.service.energy_summary(days=7)
        self.assertIn("missing", summary)

    def test_energy_summary_full_path(self):
        self.service.update_profile(sex="男", birth_year=1990, height_cm=175,
                                    activity_level="light")
        self.service.log_measurement({"metric_code": "weight", "value": 70.0})
        self.service.log_food(FoodLogCreate(food_name="米饭", kcal_per_unit=200, amount=10))
        self.service.log_workout(WorkoutCreate(activity="跑步", duration_min=30))
        summary = self.service.energy_summary(days=7)
        self.assertGreater(summary["bmr"], 1000)
        self.assertGreater(summary["tdee"], summary["bmr"])
        self.assertAlmostEqual(summary["intake_kcal"], 2000, places=0)
        self.assertIn("deficit_range", summary)
        self.assertIn("不构成医疗建议", summary["disclaimer"])

    def test_history_tdee_says_not_enough_data(self):
        result = self.service.estimate_tdee_from_history(days=21)
        self.assertFalse(result["available"])
        self.assertIn("还需要更多数据", result["reason"])

    def test_daily_summary(self):
        self.service.log_measurement({"metric_code": "weight", "value": 71.0})
        self.service.log_workout(WorkoutCreate(activity="瑜伽", duration_min=40))
        summary = self.service.daily_summary("2026-10-08")
        self.assertEqual(summary["met_minutes"], 100.0)
        self.assertEqual(len(summary["measurements"]), 1)

    def test_goal_progress_from_domain_data(self):
        self.service.log_measurement({"metric_code": "weight", "value": 72.0})
        goal = self.service.create_goal(
            {"metric": "weight", "target_value": 68.0, "start_value": 75.0}
        )
        self.assertEqual(goal["current_value"], 72.0)
        self.assertAlmostEqual(goal["progress"], (72.0 - 75.0) / (68.0 - 75.0), places=3)
        self.assertIn("不来自记忆", goal["note"])


# ==========================================================================
# Apple Health 导入
# ==========================================================================
APPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="zh_CN">
 <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="iPhone" unit="kg"
   startDate="2026-09-01 07:30:00 +0800" endDate="2026-09-01 07:30:00 +0800" value="75.2"/>
 <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="iPhone" unit="kg"
   startDate="2026-09-02 07:31:00 +0800" endDate="2026-09-02 07:31:00 +0800" value="75.0"/>
 <Record type="HKQuantityTypeIdentifierRestingHeartRate" sourceName="Watch" unit="count/min"
   startDate="2026-09-01 08:00:00 +0800" endDate="2026-09-01 08:00:00 +0800" value="58"/>
 <Record type="HKQuantityTypeIdentifierBodyFatPercentage" sourceName="Watch" unit="%"
   startDate="2026-09-01 08:05:00 +0800" endDate="2026-09-01 08:05:00 +0800" value="0.185"/>
 <Record type="HKQuantityTypeIdentifierSomeFutureThing" sourceName="Watch" unit="x"
   startDate="2026-09-01 08:10:00 +0800" endDate="2026-09-01 08:10:00 +0800" value="1"/>
 <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="iPhone" unit="kg"
   startDate="坏时间" endDate="坏时间" value="70"/>
 <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="iPhone" unit="kg"
   startDate="2026-09-03 07:30:00 +0800" endDate="2026-09-03 07:30:00 +0800" value="不是数字"/>
</HealthData>
"""


class AppleHealthTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.xml = self.tmp / "export.xml"
        self.xml.write_text(APPLE_XML, encoding="utf-8")
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.service = HealthService(self.conn, now_provider=lambda: NOW)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_apple_time_format(self):
        parsed = parse_apple_time("2026-09-01 07:30:00 +0800")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.date().isoformat(), "2026-09-01")
        self.assertIsNone(parse_apple_time("坏时间"))

    def test_only_whitelist_is_imported_and_unknown_is_skipped(self):
        """⚠️ 苹果从未官方文档化这个 schema,所以**只认白名单,未知类型跳过而不是崩**。"""
        report = import_apple_health(self.xml)
        self.assertEqual(report.total_records, 7)
        self.assertEqual(report.imported, 4, "两条体重 + 静息心率 + 体脂")
        self.assertEqual(report.skipped_unknown, 1)
        self.assertEqual(report.skipped_bad, 2, "坏时间与坏数值各一条")
        self.assertEqual(set(report.as_dict()["by_type"]), {"weight", "resting_hr", "body_fat"},
                         "未知类型不计数,只记在 skipped_unknown 里")

    def test_unit_conversion_for_body_fat(self):
        report = import_apple_health(self.xml)
        fat = next(s for s in report.samples if s.metric_code == "body_fat")
        self.assertAlmostEqual(fat.value, 18.5, places=3, msg="苹果存 0..1,我们存百分比")

    def test_import_writes_into_measurements(self):
        out = self.service.import_apple_health(str(self.xml))
        self.assertEqual(out["written"], 4)
        self.assertEqual(self.service.latest("weight")["value"], 75.0)
        sources = {
            row[0] for row in self.conn.execute("SELECT DISTINCT source FROM app_measurements")
        }
        self.assertEqual(sources, {"device:apple"}, "来源必须可追溯")

    def test_whitelist_is_explicit(self):
        self.assertIn("HKQuantityTypeIdentifierBodyMass", KNOWN_QUANTITY_TYPES)
        self.assertNotIn("HKQuantityTypeIdentifierSleepAnalysis", KNOWN_QUANTITY_TYPES)


# ==========================================================================
# 两条红线
# ==========================================================================
class RedLineTestCase(unittest.TestCase):
    def test_disclaimer_text_exists(self):
        self.assertIn("不构成医疗建议", MEDICAL_DISCLAIMER)

    def test_measurement_caveat_is_honest(self):
        self.assertIn("±30%", MEASUREMENT_CAVEAT)
        self.assertIn("按周看", MEASUREMENT_CAVEAT)


if __name__ == "__main__":
    unittest.main()
