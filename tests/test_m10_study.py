"""M10 验收测试:拍照录入、学生档案、复习周期展示、每日推送。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from la.domain.media import MediaStore
from la.domain.schemas.study import ProblemQuery, StudyPushSettings
from la.domain.services.study_service import (
    StudyError,
    StudyService,
    parse_push_time,
)
from la.domain.study.intake import (
    DECISION_ACCEPTED,
    DECISION_REJECTED,
    IntakeError,
    IntakeService,
    merge_overrides,
)
from la.llm.client import ChatResult
from la.llm.vision import (
    MAX_KP_COUNT,
    normalize_knowledge_points,
    normalize_subject,
    parse_response,
    strip_code_fence,
)
from la.storage import db
from la.storage.migrations import (
    MIGRATIONS_ASSISTANT,
    MIGRATIONS_MEMORY,
    add_column_if_missing,
)
from la.tasks.dispatcher import KIND_STUDY, Dispatcher

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 8, 20, 0, tzinfo=TZ)          # 晚上八点:已过默认推送时间
EARLY = datetime(2026, 10, 8, 7, 0, tzinfo=TZ)         # 早上七点:还没到推送时间
FENCE = chr(96) * 3                                    # 代码围栏(不直接写反引号,免得割裂文件)

# 最小合法 PNG(1x1),用来当"拍到的题图"
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)

VISION_REPLY = json.dumps(
    {
        "problems": [
            {
                "subject": "初中数学",
                "grade": "五年级",
                "stem_text": "3/4 + 1/6 = ?",
                "answer_text": "11/12",
                "explanation": "",
                "knowledge_points": ["数学 异分母分数加减", "通分", "通分"],
                "confidence": 0.91,
                "source": "单元测",
            },
            {
                "subject": "数学",
                "stem_text": "2/3 - 1/4 = ?",
                "knowledge_points": ["异分母分数加减"],
                "confidence": 0.8,
            },
        ]
    },
    ensure_ascii=False,
)


class StubVisionClient:
    """假的视觉客户端:只回一段固定文本,并记下它收到的消息。"""

    model = "stub-vision"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append([dict(item) for item in messages])
        return ChatResult(text=self.reply, model=self.model)


class ExplodingClient:
    model = "boom"

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("网络炸了")


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
        self.client = StubVisionClient(VISION_REPLY)
        self.intake = IntakeService(
            self.conn,
            study=self.service,
            media=self.media,
            client_factory=lambda: self.client,
            now_provider=lambda: NOW,
        )

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _session_with_image(self) -> tuple[int, int]:
        session_id = self.intake.start_session()
        candidate = self.intake.add_image(session_id, PNG_1PX, original_name="题.png")
        return session_id, candidate.id


# ==========================================================================
# 迁移
# ==========================================================================
class MigrationTestCase(Fixture):
    def test_v10_tables_and_columns(self):
        tables = {
            row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertIn("app_students", tables)
        self.assertIn("app_study_settings", tables)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(app_problems)")}
        self.assertIn("student_id", columns)
        candidate_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(app_intake_candidates)")
        }
        self.assertLessEqual({"model", "raw_text", "problem_id"}, candidate_columns)

    def test_add_column_if_missing_is_idempotent(self):
        first = add_column_if_missing(self.conn, "app_problems", "demo_col", "demo_col TEXT")
        second = add_column_if_missing(self.conn, "app_problems", "demo_col", "demo_col TEXT")
        self.assertTrue(first)
        self.assertFalse(second, "第二次加同一列必须是无操作,而不是报 duplicate column")

    def test_migrate_twice_is_noop(self):
        self.assertEqual(db.migrate(self.conn, MIGRATIONS_ASSISTANT), [])


# ==========================================================================
# 学生档案
# ==========================================================================
class StudentTestCase(Fixture):
    def test_default_student_is_created_on_demand(self):
        student = self.service.default_student()
        self.assertEqual(student.name, "孩子")
        self.assertTrue(student.is_default)

    def test_ensure_student_reuses_by_name(self):
        first = self.service.ensure_student("小明", grade="五年级")
        second = self.service.ensure_student("小明")
        self.assertEqual(first.id, second.id, "同名必须是同一个学生,否则统计会分裂")
        self.assertEqual(second.grade, "五年级")

    def test_set_default_switches_and_persists(self):
        self.service.default_student()
        xiaoming = self.service.ensure_student("小明")
        self.service.set_default_student(xiaoming.id)
        self.assertEqual(self.service.settings().student_id, xiaoming.id)
        self.assertEqual(self.service.default_student().name, "小明")

    def test_student_for_priority(self):
        self.service.default_student()
        xiaoming = self.service.ensure_student("小明")
        self.assertEqual(self.service.student_for(student_id=xiaoming.id).name, "小明")
        self.assertEqual(self.service.student_for(student_name="小红").name, "小红")
        self.assertEqual(self.service.student_for().name, "孩子")

    def test_problem_carries_student(self):
        xiaoming = self.service.ensure_student("小明")
        record = self.service.add_problem(
            {"subject": "数学", "stem_text": "1+1", "student_id": xiaoming.id}
        )
        self.assertEqual(record.student_id, xiaoming.id)
        self.assertEqual(record.student_name, "小明")
        filtered = self.service.list_problems(ProblemQuery(student_id=xiaoming.id))
        self.assertEqual([item.id for item in filtered], [record.id])


# ==========================================================================
# 推送时间解析
# ==========================================================================
class PushTimeTestCase(unittest.TestCase):
    def test_accepts_common_shapes(self):
        cases = {
            "19:30": "19:30",
            "07:05": "07:05",
            "7:30": "07:30",
            "晚上7点半": "19:30",
            "下午3点": "15:00",
            "19点30分": "19:30",
            "19时30": "19:30",
            "19点整": "19:00",
            "7:30pm": "19:30",
        }
        for raw, expected in cases.items():
            self.assertEqual(parse_push_time(raw), expected, raw)

    def test_rejects_nonsense(self):
        for raw in ("", "下午", "25:00", "19:99", "abc"):
            with self.assertRaises(StudyError, msg=raw):
                parse_push_time(raw)


# ==========================================================================
# 设置
# ==========================================================================
class SettingsTestCase(Fixture):
    def test_defaults_without_row(self):
        settings = self.service.settings()
        self.assertTrue(settings.push_enabled)
        self.assertEqual(settings.push_time_local, "19:30")
        self.assertEqual(settings.push_daily_limit, 10)

    def test_update_and_read_back(self):
        updated = self.service.update_settings(
            StudyPushSettings(push_time="晚上7点半", daily_limit=5, enabled=False)
        )
        self.assertEqual(updated.push_time_local, "19:30")
        self.assertEqual(updated.push_daily_limit, 5)
        self.assertFalse(updated.push_enabled)
        again = self.service.settings()
        self.assertEqual(again.push_time_local, "19:30", "设置必须落库,不能只活在内存里")

    def test_daily_limit_is_bounded(self):
        with self.assertRaises(StudyError):
            self.service.update_settings({"daily_limit": 0})
        with self.assertRaises(StudyError):
            self.service.update_settings({"daily_limit": 99})

    def test_student_name_creates_and_sets_default(self):
        updated = self.service.update_settings({"student_name": "小明"})
        self.assertEqual(updated.student_name, "小明")
        self.assertEqual(self.service.default_student().name, "小明")

    def test_single_row_only(self):
        self.service.update_settings({"push_time": "08:00"})
        self.service.update_settings({"push_time": "09:00"})
        count = self.conn.execute("SELECT count(*) FROM app_study_settings").fetchone()[0]
        self.assertEqual(count, 1, "设置是单行表:CHECK (id = 1) 兜住这条不变量")


# ==========================================================================
# 视觉识别:解析层(不联网)
# ==========================================================================
class VisionParseTestCase(unittest.TestCase):
    def test_strip_code_fence(self):
        text = FENCE + 'json\n{"a":1}\n' + FENCE
        self.assertEqual(strip_code_fence(text), '{"a":1}')

    def test_parses_multiple_problems(self):
        problems = parse_response(VISION_REPLY)
        self.assertEqual(len(problems), 2)
        first = problems[0]
        self.assertEqual(first.subject, "数学", "「初中数学」要归到闭集里的「数学」")
        self.assertEqual(first.stem_text, "3/4 + 1/6 = ?")
        self.assertEqual(first.knowledge_points, ["异分母分数加减", "通分"])
        self.assertAlmostEqual(first.confidence, 0.91)

    def test_tolerates_prose_and_single_object(self):
        self.assertEqual(parse_response("先说一句废话"), [])
        single = parse_response('{"subject":"英语","stem_text":"Translate this"}')
        self.assertEqual(len(single), 1)
        self.assertEqual(single[0].subject, "英语")

    def test_caps_and_cleans(self):
        points = normalize_knowledge_points(
            ["数学 " + "很长的知识点" * 20, "", "通分", "通分", "a", "b", "c"], subject="数学"
        )
        self.assertLessEqual(len(points), MAX_KP_COUNT)
        self.assertNotIn("", points)
        self.assertEqual(len(set(points)), len(points))

    def test_subject_falls_back_to_other(self):
        self.assertEqual(normalize_subject("体育"), "其他")
        self.assertEqual(normalize_subject("数学(人教版)"), "数学")


# ==========================================================================
# 拍照录入流水线
# ==========================================================================
class IntakeTestCase(Fixture):
    def test_add_image_stores_asset_and_candidate(self):
        session_id, candidate_id = self._session_with_image()
        candidate = self.intake.get_candidate(candidate_id)
        self.assertIsNotNone(candidate.asset_id)
        self.assertEqual(candidate.decision, "pending")
        self.assertEqual(self.intake.session(session_id).status, "reviewing")

    def test_sessions_and_pending_are_scoped_to_owner(self):
        """录入会话按成员归属:别人看不到、也打不开我的候选。"""
        mine = IntakeService(
            self.conn, study=self.service, media=self.media,
            owner_session="member:1", now_provider=lambda: NOW,
        )
        other = IntakeService(
            self.conn, study=self.service, media=self.media,
            owner_session="member:2", now_provider=lambda: NOW,
        )
        session_id = mine.start_session()
        mine.add_image(session_id, PNG_1PX, original_name="我的题.png")
        self.assertEqual(mine.pending_count(), 1)
        self.assertEqual(other.pending_count(), 0, "不能数到别人的待确认")
        self.assertIsNone(other.latest_session(), "不能拿到别人的录入会话")
        with self.assertRaises(IntakeError):
            other.session(session_id)

    def test_rejects_non_image(self):
        session_id = self.intake.start_session()
        with self.assertRaises(IntakeError):
            self.intake.add_image(session_id, b"%PDF-1.4 not an image", original_name="a.pdf")

    def test_recognize_fills_candidate_and_splits_extra_problems(self):
        session_id, _ = self._session_with_image()
        report = self.intake.recognize_session(session_id)
        self.assertEqual(report["recognized"], 1)
        self.assertEqual(report["problems"], 2, "一张照片里两道题 -> 两条候选")
        candidates = self.intake.candidates(session_id)
        self.assertEqual(len(candidates), 2, "多出来的那道题另开一条候选,而不是被丢掉")
        self.assertEqual(candidates[0].payload["stem_text"], "3/4 + 1/6 = ?")
        self.assertEqual(candidates[1].payload["stem_text"], "2/3 - 1/4 = ?")
        self.assertEqual(candidates[0].model, "stub-vision")
        self.assertTrue(candidates[0].raw_text, "模型原话要留档 —— 排错时最想知道它说了什么")

    def test_recognize_sends_image_as_user_message(self):
        session_id, _ = self._session_with_image()
        self.intake.recognize_session(session_id)
        message = self.client.calls[0][0]
        self.assertEqual(message["role"], "user", "图片只能出现在 user 消息里,否则 API 400")
        kinds = [block["type"] for block in message["content"]]
        self.assertEqual(kinds, ["text", "image_url"])
        self.assertTrue(
            message["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        )

    def test_recognize_is_skipped_when_already_recognized(self):
        session_id, _ = self._session_with_image()
        self.intake.recognize_session(session_id)
        before = len(self.client.calls)
        report = self.intake.recognize_session(session_id)
        self.assertEqual(report["images"], 0, "已经认过的图不要重复调模型(费钱)")
        self.assertEqual(len(self.client.calls), before)
        self.intake.recognize_session(session_id, force=True)
        self.assertGreater(len(self.client.calls), before, "force 会真的重新调模型")
        # ⚠️ 重跑不能把"一张图两道题"变成四条候选 —— 那道拆分出来的题也要认得出是自己
        self.assertEqual(len(self.intake.candidates(session_id)), 2)

    def test_failure_keeps_candidate_usable(self):
        self.intake._client_factory = lambda: ExplodingClient()  # noqa: SLF001
        session_id, candidate_id = self._session_with_image()
        report = self.intake.recognize_session(session_id)
        self.assertEqual(report["failed"], 1)
        candidate = self.intake.get_candidate(candidate_id)
        self.assertTrue(candidate.error)
        self.assertFalse(candidate.settled)
        outcome = self.intake.accept(
            candidate_id, overrides={"subject": "数学", "stem_text": "手抄的题"}
        )
        self.assertEqual(self.service.get_problem(outcome["problem_id"]).stem_text, "手抄的题")

    def test_offline_mode_degrades_gracefully(self):
        service = IntakeService(
            self.conn, study=self.service, media=self.media, client_factory=None
        )
        session_id = service.start_session()
        candidate = service.add_image(session_id, PNG_1PX)
        report = service.recognize_session(session_id)
        self.assertTrue(report["offline"])
        self.assertFalse(candidate.settled)

    def test_accept_creates_problem_with_units(self):
        session_id, candidate_id = self._session_with_image()
        self.intake.recognize_session(session_id)
        outcome = self.intake.accept(candidate_id, actor="user")
        problem = self.service.get_problem(outcome["problem_id"])
        self.assertEqual(problem.subject, "数学")
        self.assertEqual(problem.knowledge_points, ["异分母分数加减", "通分"])
        self.assertEqual(len(problem.review_units), 3, "原题 1 个 + 两个知识点各 1 个")
        self.assertEqual(len(problem.asset_ids), 1)
        self.assertEqual(problem.status, "pending")
        self.assertEqual(outcome["decision"], DECISION_ACCEPTED)

    def test_accept_twice_is_refused(self):
        session_id, candidate_id = self._session_with_image()
        self.intake.recognize_session(session_id)
        self.intake.accept(candidate_id)
        with self.assertRaises(IntakeError):
            self.intake.accept(candidate_id)

    def test_accept_requires_subject(self):
        session_id, candidate_id = self._session_with_image()
        with self.assertRaises(IntakeError):
            self.intake.accept(candidate_id, overrides={"subject": ""})

    def test_overrides_clear_fields_on_purpose(self):
        session_id, candidate_id = self._session_with_image()
        self.intake.recognize_session(session_id)
        self.intake.accept(candidate_id, overrides={"answer_text": ""})
        problem = self.service.get_problem(self.intake.get_candidate(candidate_id).problem_id)
        self.assertIsNone(problem.answer_text, "表单里的空串是「我要清空」,不是「没填」")

    def test_reject_settles_session(self):
        session_id, candidate_id = self._session_with_image()
        self.intake.reject(candidate_id, note="不是错题")
        self.assertEqual(self.intake.get_candidate(candidate_id).decision, DECISION_REJECTED)
        self.assertEqual(self.intake.session(session_id).status, "done")

    def test_merge_overrides_ignores_meta(self):
        merged = merge_overrides(
            {"_meta": {"error": "x"}, "subject": "数学"}, {"stem_text": "新题干"}
        )
        self.assertNotIn("_meta", merged)
        self.assertEqual(merged["stem_text"], "新题干")
        self.assertEqual(merged["subject"], "数学")

    def test_session_student_is_inherited_on_accept(self):
        """会话里选了谁,录进去就是谁的 —— CLI 的 --student 靠这条生效。"""
        xiaoming = self.service.ensure_student("小明")
        session_id = self.intake.start_session(student_id=xiaoming.id)
        candidate = self.intake.add_image(session_id, PNG_1PX)
        self.intake.recognize_session(session_id)
        outcome = self.intake.accept(candidate.id)
        problem = self.service.get_problem(outcome["problem_id"])
        self.assertEqual(problem.student_name, "小明")

    def test_pending_count_and_latest_session(self):
        session_id, _ = self._session_with_image()
        self.assertEqual(self.intake.pending_count(), 1)
        latest = self.intake.latest_session()
        self.assertIsNotNone(latest)
        self.assertEqual(latest.id, session_id)


# ==========================================================================
# 复习单元的身份:kp_id,不是 ord 序号
# ==========================================================================
class ReviewUnitIdentityTestCase(Fixture):
    def test_existing_units_do_not_shift_when_a_smaller_kp_is_added(self):
        record = self.service.add_problem(
            {"subject": "数学", "stem_text": "题", "knowledge_points": ["通分"]}
        )
        before = {unit.kp_id: unit.id for unit in self.service.list_units(record.id) if unit.kp_id}
        self.service.resolve_knowledge_point("约分", subject="数学")
        self.service.set_knowledge_points(record.id, ["约分", "通分"])
        after = {unit.kp_id: unit.id for unit in self.service.list_units(record.id) if unit.kp_id}
        for kp_id, unit_id in before.items():
            self.assertEqual(after.get(kp_id), unit_id, "已有单元必须还是原来那个单元")

    def test_removing_knowledge_point_suspends_instead_of_deleting(self):
        record = self.service.add_problem(
            {"subject": "数学", "stem_text": "题", "knowledge_points": ["通分", "约分"]}
        )
        dropped = [
            unit for unit in self.service.list_units(record.id) if unit.knowledge_point == "约分"
        ]
        self.assertEqual(len(dropped), 1)
        self.service.set_knowledge_points(record.id, ["通分"])
        units = {unit.id: unit for unit in self.service.list_units(record.id)}
        self.assertIn(dropped[0].id, units, "单元不能被删 —— 进度是资产")
        self.assertEqual(units[dropped[0].id].suspended, 1)
        self.service.set_knowledge_points(record.id, ["通分", "约分"])
        units = {unit.id: unit for unit in self.service.list_units(record.id)}
        self.assertEqual(units[dropped[0].id].suspended, 0)

    def test_archived_problem_leaves_the_queue(self):
        record = self.service.add_problem({"subject": "数学", "stem_text": "题"})
        tomorrow = (NOW + timedelta(days=2)).date().isoformat()
        self.assertTrue(self.service.due_items(today=tomorrow))
        self.service.archive_problem(record.id)
        self.assertEqual(self.service.due_items(today=tomorrow), [])
        self.assertEqual(self.service.list_problems(ProblemQuery()), [])
        self.assertEqual(len(self.service.list_problems(ProblemQuery(include_archived=True))), 1)
        self.service.restore_problem(record.id)
        self.assertTrue(self.service.due_items(today=tomorrow))


# ==========================================================================
# 每日推送
# ==========================================================================
class PushTestCase(Fixture):
    def _problem(self) -> int:
        return self.service.add_problem(
            {"subject": "数学", "stem_text": "3/4 + 1/6 = ?", "knowledge_points": ["通分"]}
        ).id

    def test_no_digest_before_push_time(self):
        self._problem()
        self.assertIsNone(self.service.push_digest(now=EARLY))

    def test_no_digest_when_disabled(self):
        self._problem()
        self.service.update_settings({"enabled": False})
        self.assertIsNone(self.service.push_digest(now=NOW))

    def test_digest_lists_due_items(self):
        self._problem()
        tomorrow = NOW + timedelta(days=1)
        text = self.service.push_digest(now=tomorrow)
        self.assertIsNotNone(text)
        self.assertIn("错题复习", text)
        self.assertIn("【数学】", text)
        self.assertIn("通分", text)

    def test_digest_respects_daily_limit(self):
        for index in range(5):
            self.service.add_problem({"subject": "数学", "stem_text": "第 " + str(index) + " 题"})
        self.service.update_settings({"daily_limit": 2})
        text = self.service.push_digest(now=NOW + timedelta(days=1))
        self.assertIn("(2 道", text)

    def test_digest_says_so_when_nothing_due(self):
        text = self.service.push_digest(now=NOW + timedelta(days=30))
        self.assertIn("没有到期的题", text)

    def test_digest_lists_each_problem_once(self):
        """⭐ 推送正文也**按题归并**(D108)。

        用户 2026-10-13 报的"今日复习里怎么有重复的"在界面上是三张一样的卡,
        在晚上的微信推送里就是连着三条一模一样的题 —— 同一件事,两处都要治。
        """
        record = self.service.add_problem(
            {"subject": "数学", "stem_text": "3/4 + 1/6 = ?",
             "knowledge_points": ["通分", "异分母分数加减"]}
        )
        tomorrow = NOW + timedelta(days=1)
        text = self.service.push_digest(now=tomorrow)
        self.assertIsNotNone(text)
        self.assertIn("(1 道", text, "三个单元 = 一道题")
        self.assertEqual(text.count("3/4 + 1/6"), 1, "同一道题不许在推送里出现两次")
        self.assertIn("通分", text)
        self.assertIn("异分母分数加减", text, "两个知识点都要写在同一行里")
        state = self.service.push_state(now=tomorrow)
        self.assertEqual(state["due_count"], 1)
        self.assertEqual(state["due_units"], 3)
        _ = record

    def test_dispatch_claims_once_per_day(self):
        # ⚠️ dispatches 表在 **memory.db**(调度器住那边),错题在 assistant.db ——
        #    这个测试必须真的开两个库,否则测的不是真实装配(第一次写就踩过这个坑)。
        memory = db.connect(self.tmp / "memory.db")
        db.migrate(memory, MIGRATIONS_MEMORY)
        try:
            self._problem()
            dispatcher = Dispatcher(
                memory, study_digest=lambda moment: self.service.push_digest(now=moment)
            )
            tomorrow = NOW + timedelta(days=1)
            first = dispatcher.deliver_study_push(tomorrow)
            self.assertIsNotNone(first)
            self.assertEqual(first.kind, KIND_STUDY)
            self.assertIsNone(dispatcher.deliver_study_push(tomorrow), "同一天只推一次")
            again = dispatcher.study_push_today(tomorrow)
            self.assertIsNotNone(again)
            self.assertEqual(again.text, first.text)
            self.assertIsNotNone(dispatcher.deliver_study_push(tomorrow + timedelta(days=1)))
        finally:
            memory.close()

    def test_push_state_reports_readiness(self):
        self._problem()
        state = self.service.push_state(now=NOW)
        self.assertTrue(state["ready"], "晚上八点已过默认 19:30")
        self.assertEqual(state["push_time_local"], "19:30")
        early = self.service.push_state(now=EARLY)
        self.assertFalse(early["ready"])

    def test_scheduler_note_mentions_engine(self):
        self.assertIn("固定间隔", self.service.schedule_note())


# ==========================================================================
# Web 端(读取路径不需要模型)
# ==========================================================================
class WebTestCase(Fixture):
    """Web 层测试。

    ⚠️ app 用的是 config.ASSISTANT_DB / MEDIA_DIR 这些**模块级路径**,不是 fixture 里
    那个连接。所以必须像 test_m5 那样把路径临时搬到 temp 目录 ——
    否则这些测试会往真实 data/ 里写数据(实测踩过:设置被写进了日常库)。
    """

    def setUp(self) -> None:
        super().setUp()
        from la import config

        self._config = config
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "LOGS_DIR",
                        "ASSISTANT_DB", "MEMORY_DB", "RUNTIME_DIRS")
        }
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.LOGS_DIR = self.tmp / "logs"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config.RUNTIME_DIRS = (self.tmp, config.ARCHIVE_DIR, config.MEDIA_DIR, config.LOGS_DIR)

    def tearDown(self) -> None:
        for key, value in self._patched.items():
            setattr(self._config, key, value)
        super().tearDown()

    def _client(self):
        from fastapi.testclient import TestClient

        from la.web.app import create_app

        return TestClient(create_app())

    def test_pages_render_without_model(self):
        asset = self.media.store_bytes(PNG_1PX)
        record = self.service.add_problem(
            {"subject": "数学", "stem_text": "3/4 + 1/6 = ?", "knowledge_points": ["通分"],
             "asset_ids": [asset.id]}
        )
        client = self._client()
        for path in ("/study", "/study/problem/" + str(record.id)):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)
        self.assertEqual(client.get("/assets/" + str(asset.id)).status_code, 200)
        self.assertEqual(client.get("/assets/999999").status_code, 404)

    def test_intake_accept_is_idempotent(self):
        """双击「确认录入」不该 500,也不该录进去两道。"""
        session_id = self.intake.start_session()
        candidate = self.intake.add_image(session_id, PNG_1PX)
        self.intake.recognize_session(session_id)
        client = self._client()
        payload = {
            "subject": "数学", "stem": "3/4 + 1/6 = ?", "answer": "11/12",
            "kp": "通分", "grade": "", "student_id": "", "_idem": "same-token",
        }
        first = client.post("/study/intake/candidate/" + str(candidate.id) + "/accept",
                            data=payload, follow_redirects=False)
        second = client.post("/study/intake/candidate/" + str(candidate.id) + "/accept",
                             data=payload, follow_redirects=False)
        self.assertEqual(first.status_code, 303)
        self.assertEqual(second.status_code, 303, "重复提交必须是 303 跳转,不是 500")
        self.assertEqual(len(self.service.list_problems(ProblemQuery())), 1,
                         "同一次确认只该录进去一道题")

    def test_settings_post_round_trip(self):
        client = self._client()
        response = client.post(
            "/study/settings",
            data={"push_time": "06:45", "enabled": "1", "daily_limit": "4",
                  "student_name": "小明", "_idem": "t1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        settings = self.service.settings()
        self.assertEqual(settings.push_time_local, "06:45")
        self.assertEqual(settings.push_daily_limit, 4)
        self.assertEqual(settings.student_name, "小明")


class TickerTestCase(WebTestCase):
    """后台心跳:到点了真的会推一条出来(不经过模型)。

    ⚠️ 心跳走的是 build_services() -> config 里的真实路径,所以这里必须和 WebTestCase
    一样把路径搬到 temp 目录,否则它会往日常库里推。推送时间设成 00:01 ——
    这样无论测试在几点跑,都算"已经过点"。
    """

    def test_tick_pushes_once_and_only_once(self):
        from la.web import ticker

        settings = self.service.update_settings({"push_time": "00:01"})
        self.assertEqual(settings.push_time_local, "00:01")
        record = self.service.add_problem(
            {"subject": "数学", "stem_text": "3/4 + 1/6 = ?", "knowledge_points": ["通分"]}
        )
        # 把首次到期提前到今天,制造"今天有题该复习"的局面
        today = self.service._current_time().date().isoformat()  # noqa: SLF001
        self.conn.execute(
            "UPDATE app_review_units SET due_date_local = ? WHERE problem_id = ?",
            (today, record.id),
        )
        self.conn.commit()

        first = ticker.tick_once()
        self.assertEqual(first["error"], "", first["error"])
        self.assertIsNotNone(first["study"], "到点了就该推一条出来")
        self.assertTrue(first["study"].startswith("study:"))
        second = ticker.tick_once()
        self.assertIsNone(second["study"], "同一天不会推第二次")

        memory = db.connect(self.tmp / "memory.db")
        try:
            rows = memory.execute(
                "SELECT id, payload FROM dispatches WHERE kind = 'study'"
            ).fetchall()
        finally:
            memory.close()
        self.assertEqual(len(rows), 1)
        self.assertIn("错题复习", rows[0]["payload"])

    def test_ticker_can_be_switched_off(self):
        import os

        from la.web import ticker

        original = os.environ.get("LA_TICKER")
        os.environ["LA_TICKER"] = "0"
        try:
            self.assertFalse(ticker.ticker_enabled())
        finally:
            if original is None:
                os.environ.pop("LA_TICKER", None)
            else:
                os.environ["LA_TICKER"] = original


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
