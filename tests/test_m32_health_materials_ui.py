"""D166:健康页与素材页改版。

健康:大数字 + 平滑趋势 + 每日热量收支(以前把 7 天合计当"每日",数字大了 7 倍)。
素材:缩略图墙 + 状态标签 + 上传弹窗。
"""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from la import config
from la.domain.services.health_service import HealthService
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY
from la.web.app import create_app


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
        self.client = TestClient(create_app(), follow_redirects=False)
        self.client.post("/setup", data={"pin": "123456", "pin2": "123456"})

    def tearDown(self) -> None:
        self.client.close()
        self.conn.close()
        for key, value in self._patched.items():
            setattr(config, key, value)
        self._tmp.cleanup()


class HealthPageTest(_Base):
    def _seed_weight(self, days: int = 10) -> None:
        health = HealthService(self.conn)
        today = date.today()
        for offset in range(days, -1, -1):
            day = today - timedelta(days=offset)
            health.log_measurement({
                "metric_code": "weight",
                "value": round(74.0 - (days - offset) * 0.1, 1),
                "measured_at": day.isoformat() + "T07:30:00+08:00",
            }, actor="user", receipt=False)

    def test_page_has_two_dialogs_and_a_big_number(self) -> None:
        self._seed_weight()
        html = self.client.get("/health").text
        self.assertIn('id="hl-weight"', html)
        self.assertIn('id="hl-workout"', html)
        self.assertIn("当前体重", html)
        self.assertIn("hl-spark", html, "至少两天数据要画出曲线")

    def test_dialogs_are_closed_until_asked(self) -> None:
        html = self.client.get("/health").text
        self.assertNotIn('data-open="1"', html)
        opened = self.client.get("/health?compose=weight").text
        self.assertIn('data-open="1"', opened)
        self.assertIn('id="hl-weight"', opened)

    def test_energy_numbers_are_per_day(self) -> None:
        """⚠️ 回归:以前把 7 天合计当"每日缺口",数字整整大 7 倍。"""
        self._seed_weight(3)          # 没有体重就算不出能量收支
        health = HealthService(self.conn)
        health.update_profile(sex="男", birth_year=1990, height_cm=175,
                              activity_level="moderate")
        summary = health.energy_summary(days=7)
        self.assertNotIn("missing", summary)
        daily = round(summary["deficit_kcal"] / summary["days"])
        # ⭐ D181(2026-11-11):健康页拆成「今天 / 趋势 / 能量 / 同步」四个页签,
        #    能量收支搬到了「能量」页 —— 这条回归测试盯的仍是"那个大数字是**每天**的"。
        html = self.client.get("/health?tab=energy").text
        self.assertIn("每天缺口", html)
        self.assertIn(">" + str(daily) + "<", html.replace("\n", ""),
                      "页面上那个大数字应该是每天的缺口:" + str(daily))

    def test_workouts_are_listed(self) -> None:
        HealthService(self.conn).log_workout(
            {"activity": "跑步", "duration_min": 30}, actor="user")
        html = self.client.get("/health").text
        self.assertIn("跑步", html)
        self.assertIn("30", html)

    def test_untimed_workout_shows_reps_not_none_minutes(self) -> None:
        """⭐ 2026-11-21(D188):只记个数的运动,卡片显示「10 个」——

        以前模板写死「{{ duration_min }}分钟」,没有时长的记录会渲染成
        「None分钟」,或者更糟:列表里有一列 NULL 时,max 直接抛 TypeError。
        """
        HealthService(self.conn).log_workout(
            {"activity": "仰卧起坐", "reps": 10}, actor="user")
        html = self.client.get("/health").text
        self.assertIn("仰卧起坐", html)
        self.assertIn("10<i>个</i>", html)
        self.assertNotIn("None<i>分钟</i>", html)
        self.assertNotIn(">None<", html)

    def test_form_records_a_reps_only_workout(self) -> None:
        response = self.client.post(
            "/health/workout",
            data={"activity": "仰卧起坐", "reps": "10", "_idem": "t-reps"},
        )
        self.assertEqual(response.status_code, 303, response.text[:200])
        row = self.conn.execute(
            "SELECT duration_min, reps FROM app_workouts").fetchone()
        self.assertIsNone(row["duration_min"], "没填时长就必须是 NULL,不能是 0")
        self.assertEqual(row["reps"], 10)

    def test_form_rejects_a_workout_with_no_dimension(self) -> None:
        response = self.client.post(
            "/health/workout",
            data={"activity": "仰卧起坐", "_idem": "t-empty"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers["location"])
        count = self.conn.execute("SELECT count(*) FROM app_workouts").fetchone()[0]
        self.assertEqual(count, 0, "一个维度都没填就不该写库")


class MaterialsPageTest(_Base):
    def _upload(self, name: str = "数学.png", payload: bytes = b"\x89PNG\r\n\x1a\n fake") -> None:
        response = self.client.post(
            "/materials/upload",
            files=[("files", (name, io.BytesIO(payload), "image/png"))],
            data={"_idem": "t-" + name},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text[:200])

    def test_upload_shows_up_as_a_card(self) -> None:
        self._upload()
        html = self.client.get("/materials").text
        self.assertIn("mt-card", html)
        self.assertIn("数学.png", html)
        self.assertIn("/materials/", html)

    def test_upload_dialog_opens_with_flag(self) -> None:
        html = self.client.get("/materials").text
        self.assertIn('id="mt-upload"', html)
        self.assertNotIn('data-open="1"', html)
        opened = self.client.get("/materials?upload=1").text
        self.assertIn('data-open="1"', opened)

    def test_filter_tabs_show_counts(self) -> None:
        self._upload("a.png", b"one")
        self._upload("b.png", b"two")
        html = self.client.get("/materials").text
        self.assertIn("mt-tabs", html)
        # 两份素材 → 标签上的"全部"后面挂着 2
        self.assertIn(">全部", html)
        self.assertIn("<span>2</span>", html, "标签上要带数量")

    def test_empty_state_is_friendly(self) -> None:
        html = self.client.get("/materials").text
        self.assertIn("还没有素材", html)


class PaginationTest(_Base):
    """2026-10-24 用户:"到 1000 个也一直下拉吗?" —— 两段都要有边界。"""

    def _seed(self, count: int) -> None:
        from la.domain.media import MediaStore
        from la.domain.materials import MaterialService

        service = MaterialService(self.conn, media=MediaStore(self.conn, root=config.MEDIA_DIR))
        for index in range(count):
            service.ingest_bytes(
                b"material-%03d" % index, channel="web",
                original_name="m%03d.png" % index,
            )

    def test_materials_are_paginated(self) -> None:
        self._seed(30)
        first = self.client.get("/materials").text
        self.assertIn("共 30 条", first)
        self.assertIn("第 1 / 2 页", first)
        self.assertIn("pg-pager", first)
        self.assertIn("m029.png", first, "最新的在第一页")
        self.assertNotIn("m000.png", first, "最老的翻到第二页才出现")
        second = self.client.get("/materials?page=2").text
        self.assertIn("m000.png", second)
        self.assertIn("第 2 / 2 页", second)

    def test_page_out_of_range_is_clamped(self) -> None:
        self._seed(5)
        response = self.client.get("/materials?page=999")
        self.assertEqual(response.status_code, 200)
        self.assertIn("m004.png", response.text)

    def test_filter_and_page_are_kept_in_pager_links(self) -> None:
        self._seed(30)
        html = self.client.get("/materials?handled=pending").text
        self.assertIn("handled=pending", html)
        self.assertIn("page=2", html, "翻页链接要带着当前筛选")

    def test_artifacts_are_paginated_too(self) -> None:
        from la.domain.artifacts import ArtifactStore

        store = ArtifactStore(self.conn, root=config.ARTIFACTS_DIR)
        for index in range(10):
            store.register_text("row %d" % index, filename="f%02d.txt" % index,
                                label="产物 %02d" % index)
        first = self.client.get("/materials").text
        self.assertIn("共 10 个", first)
        self.assertIn("第 1 / 2 页", first)
        self.assertNotIn("产物 00", first, "最老的产物在第二页")
        second = self.client.get("/materials?apage=2").text
        self.assertIn("产物 00", second)

    def test_page_window_keeps_head_and_tail(self) -> None:
        from la.web.routes_materials import _page_window

        self.assertEqual(_page_window(5, 42), [1, None, 4, 5, 6, None, 42])
        self.assertEqual(_page_window(1, 3), [1, 2, 3])
        self.assertEqual(_page_window(2, 42), [1, 2, 3, 4, None, 42])


if __name__ == "__main__":
    unittest.main()
