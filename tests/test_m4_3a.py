"""M4.3a 验收测试:媒体资产、知识点字典、手工录入、复习单元、双视角、统计。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from la.domain.media import MediaStore, sniff_media_type
from la.domain.schemas.study import KnowledgePointCreate, ProblemCreate, ProblemQuery
from la.domain.services.study_service import StudyError, StudyService
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 8, 20, 0, tzinfo=TZ)

# 最小合法 PNG(1x1),用来当"拍到的题图"
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001'0d0a2db40000"
    "000049454e44ae426082".replace("'", "")
)
JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.media = MediaStore(self.conn, root=self.tmp / "media")
        self.service = StudyService(
            self.conn, media=self.media, now_provider=lambda: NOW
        )

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()


# ==========================================================================
# 媒体资产
# ==========================================================================
class MediaTestCase(Fixture):
    def test_content_addressed_storage(self):
        asset = self.media.store_bytes(PNG_1PX, original_name="q1.png")
        self.assertEqual(asset.media_type, "image/png", "按魔数认类型,不看后缀")
        self.assertTrue(asset.path(self.media.root).exists())
        self.assertIn(asset.sha256[:2], asset.rel_path)
        self.assertEqual(self.media.read_bytes(asset.id), PNG_1PX)

    def test_same_content_is_deduplicated(self):
        first = self.media.store_bytes(PNG_1PX)
        second = self.media.store_bytes(PNG_1PX, original_name="另一份.png")
        self.assertEqual(first.id, second.id, "相同内容只存一份")
        rows = self.conn.execute("SELECT count(*) FROM app_assets").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_sniff_media_type(self):
        self.assertEqual(sniff_media_type(JPEG_HEADER), "image/jpeg")
        self.assertEqual(sniff_media_type(PNG_1PX), "image/png")
        self.assertEqual(sniff_media_type(b"not an image"), "application/octet-stream")

    def test_store_file_from_path(self):
        path = self.tmp / "photo.jpg"
        path.write_bytes(JPEG_HEADER)
        asset = self.media.store_file(path)
        self.assertEqual(asset.original_name, "photo.jpg")
        self.assertEqual(asset.byte_size, len(JPEG_HEADER))

    def test_empty_data_rejected(self):
        with self.assertRaises(ValueError):
            self.media.store_bytes(b"")

    def test_missing_asset_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.media.get(999)


# ==========================================================================
# 知识点字典
# ==========================================================================
class KnowledgePointTestCase(Fixture):
    def test_resolve_creates_then_reuses(self):
        first = self.service.resolve_knowledge_point("异分母分数加减", subject="数学")
        second = self.service.resolve_knowledge_point("异分母分数加减", subject="数学")
        self.assertEqual(first.id, second.id)

    def test_alias_maps_to_the_same_point(self):
        point = self.service.upsert_knowledge_point(
            KnowledgePointCreate(name="异分母分数加减", subject="数学",
                                 aliases=["分数加减", "通分"])
        )
        via_alias = self.service.resolve_knowledge_point("通分", subject="数学")
        self.assertEqual(via_alias.id, point.id, "别名必须归到同一个知识点,否则统计会分裂")
        self.assertIn("分数加减", self.service.list_knowledge_points()[0].aliases)

    def test_empty_name_rejected(self):
        with self.assertRaises(StudyError):
            self.service.resolve_knowledge_point("  ")


# ==========================================================================
# 录入与复习单元
# ==========================================================================
class ProblemTestCase(Fixture):
    def _add(self, **overrides):
        payload = {
            "subject": "数学",
            "stem_text": "3/4 + 1/6 = ?",
            "answer_text": "11/12",
            "knowledge_points": ["异分母分数加减", "通分"],
        }
        payload.update(overrides)
        return self.service.add_problem(ProblemCreate(**payload))

    def test_one_unit_per_knowledge_point_plus_the_original(self):
        """⭐ 一题多单元 —— 这是"能不能归因"的前提。"""
        record = self._add()
        self.assertEqual(len(record.knowledge_points), 2)
        self.assertEqual(len(record.review_units), 3, "原题 1 个 + 知识点 2 个")
        ords = [unit.ord for unit in record.review_units]
        self.assertEqual(ords, [0, 1, 2])
        self.assertIsNone(record.review_units[0].knowledge_point)
        self.assertEqual(
            sorted(u.knowledge_point for u in record.review_units[1:]),
            ["异分母分数加减", "通分"],
        )

    def test_first_due_is_tomorrow_not_now(self):
        """⚠️ FSRS 落地五个坑之三:新建单元立刻到期 -> 一次传 20 道题就被 20 道题轰炸。"""
        record = self._add()
        expected = (NOW + timedelta(days=1)).date().isoformat()
        for unit in record.review_units:
            self.assertEqual(unit.due_date_local, expected)

    def test_both_utc_and_local_due_are_stored(self):
        """⚠️ 坑之一:py-fsrs 只认 UTC,而"今日复习"要按本地自然日走索引。"""
        self._add()
        row = self.conn.execute(
            "SELECT due_utc, due_date_local FROM app_review_units LIMIT 1"
        ).fetchone()
        self.assertIn("+00:00", row["due_utc"])
        self.assertEqual(len(row["due_date_local"]), 10)

    def test_new_problem_is_pending_until_confirmed(self):
        record = self._add()
        self.assertEqual(record.status, "pending")
        confirmed = self.service.confirm_problem(record.id)
        self.assertEqual(confirmed.status, "confirmed")

    def test_image_path_becomes_an_asset(self):
        path = self.tmp / "q1.png"
        path.write_bytes(PNG_1PX)
        record = self._add(image_paths=[str(path)])
        self.assertEqual(len(record.asset_ids), 1)
        self.assertTrue(self.media.exists(record.asset_ids[0]))

    def test_same_image_twice_is_one_asset(self):
        path = self.tmp / "q1.png"
        path.write_bytes(PNG_1PX)
        record = self._add(image_paths=[str(path)])
        second = self.service.add_problem(
            ProblemCreate(subject="数学", stem_text="另一题", image_paths=[str(path)])
        )
        self.assertEqual(record.asset_ids, second.asset_ids, "内容寻址会去重")

    def test_editing_content_does_not_reset_scheduling(self):
        """⭐ 内容与调度分离:改个错别字不该让孩子重新学一遍。"""
        record = self._add()
        unit_ids = [unit.id for unit in record.review_units]
        self.service.update_problem(record.id, stem_text="3/4 + 1/6 = ?(订正版)")
        after = self.service.get_problem(record.id)
        self.assertEqual([u.id for u in after.review_units], unit_ids)
        self.assertEqual(after.knowledge_points, record.knowledge_points)

    def test_missing_subject_rejected(self):
        with self.assertRaises(Exception):
            self.service.add_problem(ProblemCreate(subject="", stem_text="x"))

    def test_unknown_problem_raises(self):
        with self.assertRaises(StudyError):
            self.service.get_problem(4242)


# ==========================================================================
# 列表与统计
# ==========================================================================
class QueryTestCase(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.service.add_problem(ProblemCreate(
            subject="数学", stem_text="分数题", knowledge_points=["分数"]
        ))
        self.service.add_problem(ProblemCreate(
            subject="数学", stem_text="几何题", knowledge_points=["三角形面积"]
        ))
        self.service.add_problem(ProblemCreate(
            subject="英语", stem_text="时态题", knowledge_points=["一般过去时"]
        ))

    def test_filter_by_subject(self):
        items = self.service.list_problems(ProblemQuery(subject="数学"))
        self.assertEqual(len(items), 2)

    def test_filter_by_knowledge_point(self):
        items = self.service.list_problems(ProblemQuery(knowledge_point="三角形面积"))
        self.assertEqual(len(items), 1)
        self.assertIn("三角形面积", items[0].knowledge_points)

    def test_stats_by_subject_and_due_today(self):
        stats = self.service.stats()
        self.assertEqual(stats.total_problems, 3)
        self.assertEqual(stats.by_subject, {"数学": 2, "英语": 1})
        self.assertEqual(stats.due_today, 0, "首次到期在明天,今天不该有到期的")

    def test_due_units_appear_tomorrow(self):
        tomorrow = (NOW + timedelta(days=1)).date().isoformat()
        due = self.service.due_units(today=tomorrow)
        # 每道题 1 个知识点 -> 原题单元 1 个 + 知识点单元 1 个 = 2 个,三道题共 6 个
        self.assertEqual(len(due), 6, "3 道题 × (1 原题 + 1 知识点) = 6 个单元")

    def test_weak_points_ordered_by_lapses(self):
        math = self.service.list_problems(ProblemQuery(subject="数学"))
        target = next(item for item in math if "三角形面积" in item.knowledge_points)
        unit = target.review_units[1]
        self.conn.execute("UPDATE app_review_units SET lapses = 5 WHERE id = ?", (unit.id,))
        self.conn.commit()
        stats = self.service.stats()
        self.assertEqual(stats.weak_points[0].knowledge_point, "三角形面积")
        self.assertEqual(stats.weak_points[0].lapses, 5)


# ==========================================================================
# 双视角
# ==========================================================================
class PerspectiveTestCase(Fixture):
    def _unit(self):
        record = self.service.add_problem(ProblemCreate(
            subject="数学", stem_text="3/4 + 1/6 = ?", answer_text="11/12",
            explanation="先通分再相加", knowledge_points=["通分"],
        ))
        return record.review_units[1].id

    def test_child_card_hides_parent_side_evaluation(self):
        card = self.service.render_card(self._unit(), perspective="child")
        self.assertEqual(card["stem_text"], "3/4 + 1/6 = ?")
        self.assertNotIn("answer_text", card)
        self.assertNotIn("explanation", card)
        self.assertNotIn("knowledge_point", card)
        self.assertNotIn("lapses", card)
        self.assertEqual([b["label"] for b in card["buttons"]], ["忘了", "会了"])
        self.assertEqual([b["rating"] for b in card["buttons"]], [1, 3])
        self.assertIn("没有关系", card["guidance"])

    def test_parent_card_has_everything(self):
        card = self.service.render_card(self._unit(), perspective="parent")
        self.assertEqual(card["answer_text"], "11/12")
        self.assertEqual(card["knowledge_point"], "通分")
        self.assertIn("fsrs_state", card)
        self.assertNotIn("buttons", card)

    def test_bad_perspective_rejected(self):
        with self.assertRaises(StudyError):
            self.service.render_card(self._unit(), perspective="teacher")


if __name__ == "__main__":
    unittest.main()
