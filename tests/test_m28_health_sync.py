"""M28:从 iPhone 健康同步(2026-11-11)。

覆盖四件事:
  ① 每天的多个样本怎么合成一个数(步数相加、体重平均、VO2max 取最后一条);
  ② 快捷指令推上来的报文(鉴权 / 幂等 / 合理性 / 同日覆盖);
  ③ 导出 zip(单位换算 / 睡眠折成一夜 / 运动 / 撤销);
  ④ 页面:今天 / 趋势 / 同步三个页签读得出来,空状态说得清。
"""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from la import config
from la.domain.receipts import ReceiptLog
from la.domain.services.health_service import HealthError, HealthService
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY
from la.web import health_sync
from la.web.app import create_app

TZ = ZoneInfo("Asia/Shanghai")
TODAY = datetime.now(TZ).date()


def _xml(day: str, *, steps: int = 1000) -> str:
    """一份最小的 export.xml:两条步数 + 一夜睡眠 + 一次跑步。"""
    night_before = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="zh_CN">
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count"
   startDate="{day} 08:00:00 +0800" endDate="{day} 08:15:00 +0800" value="{steps // 2}"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count"
   startDate="{day} 09:00:00 +0800" endDate="{day} 09:30:00 +0800" value="{steps - steps // 2}"/>
 <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="iPhone" unit="lb"
   startDate="{day} 07:30:00 +0800" endDate="{day} 07:30:00 +0800" value="165.3"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="iPhone"
   value="HKCategoryValueSleepAnalysisAsleepCore"
   startDate="{night_before} 23:20:00 +0800" endDate="{day} 03:00:00 +0800"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="iPhone"
   value="HKCategoryValueSleepAnalysisAsleepDeep"
   startDate="{day} 03:00:00 +0800" endDate="{day} 07:00:00 +0800"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="32.5" durationUnit="min"
   totalDistance="5.2" totalDistanceUnit="km" startDate="{day} 18:30:00 +0800"
   endDate="{day} 19:02:30 +0800">
   <WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned"
     sum="310" unit="kcal" startDate="{day} 18:30:00 +0800" endDate="{day} 19:02:30 +0800"/>
 </Workout>
</HealthData>
"""


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "ARTIFACTS_DIR", "MEDIA_DIR",
                        "ASSISTANT_DB", "MEMORY_DB", "LOGS_DIR")
        }
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.MEDIA_DIR = self.tmp / "media"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config.LOGS_DIR = self.tmp / "logs"
        config.ensure_runtime_dirs()
        self.conn = db.connect_assistant()
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.health = HealthService(self.conn, receipts=ReceiptLog(self.conn))
        self.client = TestClient(create_app(), follow_redirects=False)
        self.client.post("/setup", data={"pin": "123456", "pin2": "123456"})
        row = self.conn.execute("SELECT id FROM app_members ORDER BY id LIMIT 1").fetchone()
        self.member_id = int(row["id"]) if row else None
        self.token = health_sync.rotate_token(self.conn, self.member_id)

    def tearDown(self) -> None:
        self.client.close()
        self.conn.close()
        for key, value in self._patched.items():
            setattr(config, key, value)
        self._tmp.cleanup()

    def push(self, payload: dict, *, token: str | None = None):
        return self.client.post(
            "/api/health/sync",
            json=payload,
            headers={"Authorization": "Bearer " + (token or self.token)},
        )


class AggregationTest(_Base):
    def test_rules_come_from_the_dictionary(self) -> None:
        rows = {
            str(row["code"]): str(row["aggregate"])
            for row in self.conn.execute("SELECT code, aggregate FROM app_metric_types")
        }
        self.assertEqual(rows["steps"], "sum", "步数一天几十条,必须相加")
        self.assertEqual(rows["weight"], "avg", "一天称两次取平均")
        self.assertEqual(rows["vo2max"], "last", "最大摄氧量取最后一次")
        self.assertEqual(rows["sleep_hours"], "sum")

    def test_blank_metric_strings_are_ignored(self) -> None:
        """⚠️ 真机:没有锻炼数据时变量为空,快捷指令发来 exercise_min=""。"""
        out = self.push({"steps": "1,234", "exercise_min": "", "active_kcal": " "})
        self.assertEqual(out.status_code, 200, out.text[:200])
        self.assertEqual(out.json()["samples"], 1)
        self.assertEqual(self.health.series("steps")["latest"], 1234)

    def test_flat_payload_is_accepted_without_a_date(self) -> None:
        """快捷指令只用填 steps/active_kcal/...;日期省略 = 今天。"""
        day = TODAY.isoformat()
        out = self.push({"steps": 4321, "active_kcal": 210, "exercise_min": 18})
        self.assertEqual(out.status_code, 200)
        self.assertEqual(out.json()["samples"], 3)
        self.assertEqual(self.health.series("steps")["latest"], 4321)
        self.assertEqual(self.health.series("active_kcal")["latest"], 210)
        row = self.conn.execute(
            "SELECT local_date FROM app_measurements WHERE metric_code='steps'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["local_date"], day)

    def test_same_day_push_overwrites_instead_of_accumulating(self) -> None:
        day = TODAY.isoformat()
        self.assertEqual(self.push({"days": [{"date": day, "steps": 8000}]}).status_code, 200)
        self.assertEqual(self.health.series("steps")["latest"], 8000)
        self.push({"days": [{"date": day, "steps": 9000}]})
        self.assertEqual(self.health.series("steps")["latest"], 9000, "同一天应当是覆盖")

    def test_push_needs_a_token(self) -> None:
        self.assertEqual(self.push({"days": []}, token="wrong").status_code, 403)
        self.assertEqual(
            self.client.post("/api/health/sync", json={"days": []}).status_code, 403
        )

    def test_absurd_values_are_skipped_not_written(self) -> None:
        day = TODAY.isoformat()
        out = self.push({"days": [{"date": day, "steps": 900000, "sleep_hours": 30}]})
        self.assertEqual(out.status_code, 200)
        self.assertEqual(out.json()["samples"], 0)
        self.assertIsNone(self.health.series("steps")["latest"])

    def test_unknown_metric_names_are_skipped(self) -> None:
        out = self.push({"samples": [{"metric": "blood_moon", "value": 1}]})
        self.assertEqual(out.json()["skipped"], 1)
        self.assertEqual(out.json()["samples"], 0)

    def test_device_kcal_wins_over_met_formula(self) -> None:
        day = TODAY.isoformat()
        self.health.update_profile(sex="男", birth_year=1990, height_cm=175,
                                   activity_level="moderate")
        self.health.log_measurement({"metric_code": "weight", "value": 70.0}, receipt=False)
        self.push({"days": [{"date": day, "steps": 1}],
                   "workouts": [{"activity": "跑步", "duration_min": 30, "kcal": 250,
                                 "start": day + "T18:00:00+08:00"}]})
        burn = self.health.energy_summary(days=7)["burn_kcal"]
        self.assertAlmostEqual(burn, 250, delta=1, msg="有设备消耗就不该再按 MET 估一遍")

    def test_stand_time_minutes_are_converted_to_hours(self) -> None:
        """⚠️ 回归:AppleStandTime 的单位是分钟,直接当小时会大 60 倍。"""
        day = TODAY.isoformat()
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="zh_CN">
 <Record type="HKQuantityTypeIdentifierAppleStandTime" sourceName="iPhone" unit="min"
   startDate="{day} 10:00:00 +0800" endDate="{day} 11:00:00 +0800" value="60"/>
 <Record type="HKQuantityTypeIdentifierAppleExerciseTime" sourceName="iPhone" unit="min"
   startDate="{day} 18:00:00 +0800" endDate="{day} 18:30:00 +0800" value="30"/>
</HealthData>
"""
        path = self.tmp / "stand.xml"
        path.write_text(xml, encoding="utf-8")
        self.health.import_apple_health(str(path))
        self.assertAlmostEqual(self.health.series("stand_hours")["latest"], 1.0, places=2)
        self.assertAlmostEqual(self.health.series("exercise_min")["latest"], 30.0, places=2)

    def test_every_apple_metric_is_in_the_dictionary(self) -> None:
        """⚠️ 2026-11-11 真机:白名单认 Height,字典却漏了 height —— 外键直接爆。"""
        from la.domain.health.apple_health import KNOWN_QUANTITY_TYPES

        mapped = {code for code, _unit in KNOWN_QUANTITY_TYPES.values()}
        have = {
            str(row["code"])
            for row in self.conn.execute("SELECT code FROM app_metric_types")
        }
        self.assertEqual(mapped - have, set(), "白名单里的指标必须在字典里有行")

    def test_high_frequency_records_are_summed_per_day(self) -> None:
        """71.5 万条导出不能一条一行:步数/距离/能量导入时就按天合计。"""
        day = TODAY.isoformat()
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="zh_CN">
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count"
   startDate="{day} 08:00:00 +0800" endDate="{day} 08:10:00 +0800" value="100"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count"
   startDate="{day} 09:00:00 +0800" endDate="{day} 09:10:00 +0800" value="200"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count"
   startDate="{day} 10:00:00 +0800" endDate="{day} 10:10:00 +0800" value="300"/>
 <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="iPhone" unit="kg"
   startDate="{day} 07:30:00 +0800" endDate="{day} 07:30:00 +0800" value="72.4"/>
 <Record type="HKQuantityTypeIdentifierHeight" sourceName="iPhone" unit="cm"
   startDate="{day} 07:30:00 +0800" endDate="{day} 07:30:00 +0800" value="178"/>
</HealthData>
"""
        path = self.tmp / "agg.xml"
        path.write_text(xml, encoding="utf-8")
        report = self.health.import_apple_health(str(path))
        self.assertEqual(report["created_measurements"], 3, "步数合成一行 + 体重 + 身高")
        self.assertEqual(self.health.series("steps")["latest"], 600)
        self.assertEqual(self.health.series("steps")["count"], 1, "一天只留一行")
        self.assertEqual(self.health.latest("height")["value"], 178)

    def test_raw_sample_never_wipes_other_records_of_the_day(self) -> None:
        """⚠️ 回归:补一条体重样本,不能把同一天已同步的步数/睡眠一起删掉。"""
        day = TODAY.isoformat()
        self.push({"days": [{"date": day, "steps": 8000, "sleep_hours": 7.0}]})
        out = self.push({"samples": [{"metric": "weight", "value": 70.5,
                                      "start": day + "T07:00:00+08:00"}]})
        self.assertEqual(out.json()["samples"], 1)
        self.assertEqual(self.health.series("steps")["latest"], 8000)
        self.assertAlmostEqual(self.health.latest("weight")["value"], 70.5)

    def test_sample_for_a_day_with_daily_total_is_skipped(self) -> None:
        day = TODAY.isoformat()
        self.push({"days": [{"date": day, "steps": 8000}]})
        out = self.push({"samples": [{"metric": "steps", "value": 1000,
                                      "start": day + "T09:00:00+08:00"}]})
        self.assertEqual(out.json()["skipped"], 1)
        self.assertEqual(self.health.series("steps")["latest"], 8000, "两边都算会翻倍")

    def test_daily_push_supersedes_export_for_that_day(self) -> None:
        """⚠️ 回归:同一天"导出 1000 步 + 快捷指令推 8000 步"不能变成 9000。"""
        day = TODAY.isoformat()
        self.health.import_apple_health(self._write_xml(day, steps=1000))
        self.assertEqual(self.health.series("steps")["latest"], 1000)
        self.push({"days": [{"date": day, "steps": 8000}]})
        self.assertEqual(self.health.series("steps")["latest"], 8000)

    def _write_xml(self, day: str, *, steps: int = 1000) -> str:
        path = self.tmp / (day + ".xml")
        path.write_text(_xml(day, steps=steps), encoding="utf-8")
        return str(path)


class ProfileTest(_Base):
    def test_profile_action_makes_energy_calculable(self) -> None:
        """真机:用户说"出生年份 1985、身高一米七二",以前没有工具能写档案。"""
        self.health.log_measurement({"metric_code": "weight", "value": 60}, receipt=False)
        self.health.update_profile(sex="男", birth_year=1985, height_cm=172)
        summary = self.health.energy_summary(days=7)
        self.assertNotIn("missing", summary)
        self.assertEqual(summary["height_source"], "profile")
        self.assertGreater(summary["bmr"], 1000)

    def test_energy_falls_back_to_a_height_measurement(self) -> None:
        """身高只记成 checkin 测量、档案没填时,也要能用上 —— 否则还是"缺身高"。"""
        self.health.log_measurement({"metric_code": "weight", "value": 60}, receipt=False)
        self.health.log_measurement({"metric_code": "height", "value": 172}, receipt=False)
        summary = self.health.energy_summary(days=7)
        self.assertEqual(summary["missing_fields"], ["出生年份"])
        self.assertNotIn("身高", summary["missing"])
        self.health.update_profile(birth_year=1985)
        summary = self.health.energy_summary(days=7)
        self.assertEqual(summary["height_source"], "measurement")
        self.assertNotIn("missing", summary)

    def test_tool_profile_action_writes_the_profile(self) -> None:
        import asyncio

        from la.domain.tools import build_health_tools

        spec = next(
            item for item in build_health_tools(self.health) if item.name == "health_log"
        )
        saved = asyncio.run(
            spec.handler({"action": "profile", "birth_year": 1985, "height_cm": 172})
        )
        self.assertEqual(saved["birth_year"], 1985)
        self.assertEqual(saved["height_cm"], 172)
        self.assertEqual(saved["age"], datetime.now(TZ).year - 1985)


class ExportTest(_Base):
    def test_zip_import_units_sleep_and_workouts(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("apple_health_export/export.xml", _xml(TODAY.isoformat()))
        payload.seek(0)
        response = self.client.post(
            "/health/import",
            files={"file": ("export.zip", payload, "application/zip")},
            data={"_idem": "import-1"},
        )
        self.assertEqual(response.status_code, 303, response.text[:200])

        weight = self.health.latest("weight")
        self.assertAlmostEqual(weight["value"], 74.98, places=2, msg="lb 必须换算成 kg")
        sleep = self.health.sleep_summary()
        self.assertTrue(sleep["has_data"])
        self.assertAlmostEqual(sleep["last_hours"], 7.7, places=1)
        self.assertEqual(sleep["bedtime"][11:16], "23:20", "入睡时间来自睡眠分期,不是编的")
        workouts = self.health.recent_workouts(limit=5)
        self.assertEqual(workouts[0]["kind"], "跑步")
        self.assertEqual(workouts[0]["kcal"], 310.0)
        self.assertEqual(workouts[0]["source"], "device:apple")

    def test_zip_with_a_renamed_xml_is_accepted(self) -> None:
        """真机反馈:把「健康」导出的文件夹重新压一次,文件名不再是 export.xml。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("apple_health_export/健康数据.xml", _xml(TODAY.isoformat(), steps=1200))
        path = self.tmp / "renamed.zip"
        path.write_bytes(buffer.getvalue())
        report = self.health.import_apple_health(str(path))
        self.assertGreater(report["written"], 0)
        self.assertEqual(self.health.series("steps")["latest"], 1200)

    def test_double_zip_is_unwrapped(self) -> None:
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("apple_health_export/export.xml", _xml(TODAY.isoformat(), steps=1500))
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr("导出.zip", inner.getvalue())
        path = self.tmp / "double.zip"
        path.write_bytes(outer.getvalue())
        report = self.health.import_apple_health(str(path))
        self.assertGreater(report["written"], 0)
        self.assertEqual(self.health.series("steps")["latest"], 1500)

    def test_zip_without_xml_names_what_is_inside(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "not a health export")
            archive.writestr("photo.png", b"\\x89PNG\\r\\n")
        path = self.tmp / "wrong.zip"
        path.write_bytes(buffer.getvalue())
        with self.assertRaises(Exception) as ctx:
            self.health.import_apple_health(str(path))
        message = str(ctx.exception)
        self.assertIn("readme.txt", message, "错误里要列出包里到底有什么")
        self.assertIn("photo.png", message)

    def test_empty_export_is_an_error_not_zero_success(self) -> None:
        path = self.tmp / "empty.xml"
        path.write_text('<?xml version="1.0"?><HealthData locale="zh_CN"></HealthData>', encoding="utf-8")
        with self.assertRaises(HealthError):
            self.health.import_apple_health(str(path))

    def test_reimport_is_idempotent(self) -> None:
        day = TODAY.isoformat()
        path = self.tmp / "e.xml"
        path.write_text(_xml(day, steps=1000), encoding="utf-8")
        first = self.health.import_apple_health(str(path))
        second = self.health.import_apple_health(str(path))
        self.assertGreater(first["created_measurements"], 0)
        self.assertEqual(second["created_measurements"], 0, "同样的数据第二次只更新,不新增")
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM app_workouts WHERE source = 'device:apple'"
        ).fetchone()
        self.assertEqual(int(rows["n"]), 1, "同一次运动重复导入不能变成两次")
        self.assertEqual(self.health.series("steps")["latest"], 1000, "重复导入也不能累加")

    def test_undo_import_removes_workouts_too(self) -> None:
        day = TODAY.isoformat()
        path = self.tmp / "u.xml"
        path.write_text(_xml(day), encoding="utf-8")
        self.health.import_apple_health(str(path))
        receipt = self.conn.execute(
            "SELECT id FROM ops_receipts WHERE kind = 'health.import' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(receipt)
        message = self.health.revert_import(ReceiptLog(self.conn).get(int(receipt["id"])))
        self.assertIn("运动", message)
        self.assertEqual(self.health.recent_workouts(limit=5), [])
        self.assertIsNone(self.health.series("steps")["latest"])


class PageTest(_Base):
    def test_today_page_explains_what_an_iphone_can_give(self) -> None:
        html = self.client.get("/health").text
        self.assertIn("还没有从手机同步过", html)
        self.assertIn("iPhone 自己就记", html)
        self.assertIn("href=\"/health?tab=sync\"", html)
        self.assertIn("hl-ring", html)

    def test_trend_tab_renders_a_series(self) -> None:
        day = TODAY.isoformat()
        self.push({"days": [{"date": day, "steps": 8123},
                            {"date": (TODAY - timedelta(days=1)).isoformat(), "steps": 6500}]})
        html = self.client.get("/health?tab=trend&metric=steps&days=30").text
        self.assertIn("8123", html)
        self.assertIn("hl-chart-svg", html)
        self.assertIn("7 日平均", html)

    def test_sync_tab_hands_out_a_token_and_endpoint(self) -> None:
        html = self.client.get("/health?tab=sync").text
        self.assertIn("/api/health/sync", html)
        self.assertIn(self.token, html)
        self.assertIn("Authorization", html)
        self.assertIn("导出所有健康数据", html)

    def test_rotating_the_token_kills_the_old_one(self) -> None:
        response = self.client.post("/health/sync/token", data={"_idem": "rot-1"})
        self.assertEqual(response.status_code, 303)
        new_token = health_sync.get_token(self.conn, self.member_id)
        self.assertNotEqual(new_token, self.token)
        self.assertEqual(self.push({"days": []}, token=self.token).status_code, 403)
        self.assertEqual(self.push({"days": []}, token=new_token).status_code, 200)

    def test_bad_zip_and_bad_json_say_what_is_wrong(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("hello.txt", "not a health export")
        payload.seek(0)
        response = self.client.post(
            "/health/import",
            files={"file": ("export.zip", payload, "application/zip")},
            data={"_idem": "bad-1"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers.get("location", ""), "坏文件要回一句人话")
        bad = self.client.post(
            "/api/health/sync", content=b"{not json",
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"},
        )
        self.assertEqual(bad.status_code, 400)

    def test_bad_tab_and_metric_fall_back_without_error(self) -> None:
        response = self.client.get("/health?tab=nope&metric=nope&days=99999")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
