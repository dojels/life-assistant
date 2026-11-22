"""M4.3c 验收测试:FSRS/固定间隔排程、复习日志、孩子端两按钮。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from la.domain.media import MediaStore
from la.domain.schemas.study import ProblemCreate, QuizMe, QuizResult
from la.domain.services.study_service import StudyError, StudyService
from la.domain.study.scheduler import (
    CHILD_RATINGS,
    FIXED_INTERVALS_DAYS,
    MAX_INTERVAL_DAYS,
    RATING_AGAIN,
    RATING_EASY,
    RATING_GOOD,
    FixedIntervalPolicy,
    FsrsPolicy,
    UnitState,
    build_policy,
    local_due_date,
)
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 8, 20, 0, tzinfo=TZ)
UTC = ZoneInfo("UTC")


# ==========================================================================
# 排程策略本身
# ==========================================================================
class SchedulerTestCase(unittest.TestCase):
    def test_child_side_has_exactly_two_ratings(self):
        """⚠️ FSRS 落地的五个坑之四:孩子端绝不能给 4 个按钮。"""
        self.assertEqual(CHILD_RATINGS, (1, 3))

    def test_fixed_interval_walks_the_ladder(self):
        policy = FixedIntervalPolicy()
        state = UnitState()
        seen: list[float] = []
        now = NOW
        for _ in range(len(FIXED_INTERVALS_DAYS)):
            result = policy.review(state, RATING_GOOD, now)
            seen.append(result.interval_days)
            state = UnitState(reps=result.reps, lapses=result.lapses)
            now = now + timedelta(days=result.interval_days)
        self.assertEqual(seen, [1.0, 3.0, 7.0, 15.0, 30.0])
        # 到顶之后停在最后一档
        capped = policy.review(state, RATING_GOOD, now)
        self.assertEqual(capped.interval_days, 30.0)

    def test_fixed_interval_resets_on_forgetting(self):
        policy = FixedIntervalPolicy()
        state = UnitState(reps=3, lapses=0)
        result = policy.review(state, RATING_AGAIN, NOW)
        self.assertEqual(result.interval_days, 1.0)
        self.assertEqual(result.reps, 0, "忘了就回到第一档")
        self.assertEqual(result.lapses, 1)
        self.assertEqual(result.state, "relearning")

    def test_fsrs_uses_180_day_ceiling_not_36500(self):
        """⚠️ 坑之二:默认 36500 天是给成人终身记忆的;错题本有学年尺度。"""
        policy = FsrsPolicy()
        self.assertEqual(policy.scheduler.maximum_interval, MAX_INTERVAL_DAYS)
        self.assertEqual(MAX_INTERVAL_DAYS, 180)

    def test_fsrs_intervals_grow_and_stay_under_ceiling(self):
        policy = FsrsPolicy()
        state = UnitState()
        now = NOW
        intervals: list[float] = []
        for _ in range(8):
            result = policy.review(state, RATING_GOOD, now)
            intervals.append(result.interval_days)
            state = UnitState(
                stability=result.stability, difficulty=result.difficulty, state=result.state,
                reps=result.reps, lapses=result.lapses,
                last_review_utc=now.astimezone(UTC).isoformat(timespec="seconds"),
            )
            now = now + timedelta(days=result.interval_days)
        self.assertGreater(intervals[-1], 100, "连续答对间隔应当长起来")
        self.assertTrue(all(value <= MAX_INTERVAL_DAYS + 1 for value in intervals),
                        "任何一次都不该超过 180 天:" + str([round(i, 1) for i in intervals]))

    def test_fsrs_forgetting_goes_to_relearning(self):
        policy = FsrsPolicy()
        first = policy.review(UnitState(), RATING_GOOD, NOW)
        learned = UnitState(
            stability=first.stability, difficulty=first.difficulty, state=first.state,
            reps=first.reps, lapses=first.lapses,
            last_review_utc=NOW.astimezone(UTC).isoformat(timespec="seconds"),
        )
        # 先让它进入 Review 态(learning_steps=() 时第一次 Good 就进 Review)
        self.assertEqual(first.state, "review")
        later = NOW + timedelta(days=2)
        forgot = policy.review(learned, RATING_AGAIN, later)
        self.assertEqual(forgot.state, "relearning")
        self.assertEqual(forgot.lapses, 1)
        self.assertLess(forgot.interval_days, 1, "答错后应当很快再见")

    def test_sub_day_interval_collapses_to_tomorrow(self):
        """⚠️ 错题本是一天看一次的东西:10 分钟后再见等于当天被反复叫住。"""
        due = NOW.astimezone(UTC) + timedelta(minutes=10)
        self.assertEqual(local_due_date(due, NOW), "2026-10-09")

    def test_real_interval_keeps_its_date(self):
        due = (NOW + timedelta(days=7)).astimezone(UTC)
        self.assertEqual(local_due_date(due, NOW), "2026-10-15")

    def test_invalid_rating_rejected(self):
        with self.assertRaises(ValueError):
            FixedIntervalPolicy().review(UnitState(), 9, NOW)

    def test_build_policy_switch(self):
        self.assertEqual(build_policy("fixed").name, "fixed")
        self.assertEqual(build_policy("fsrs").name, "fsrs")
        self.assertEqual(build_policy("不认识").name, "fixed", "未知值退回安全默认")

    def test_parent_side_can_use_finer_ratings(self):
        policy = FsrsPolicy()
        easy = policy.review(UnitState(), RATING_EASY, NOW)
        good = policy.review(UnitState(), RATING_GOOD, NOW)
        self.assertGreaterEqual(easy.interval_days, good.interval_days,
                                "家长端的「太简单」应该比「会了」间隔更长")


# ==========================================================================
# 复习流程
# ==========================================================================
class ReviewFlowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.service = StudyService(
            self.conn,
            media=MediaStore(self.conn, root=self.tmp / "media"),
            now_provider=lambda: NOW,
        )

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _problem(self, **overrides):
        payload = {
            "subject": "数学",
            "stem_text": "3/4 + 1/6 = ?",
            "answer_text": "11/12",
            "knowledge_points": ["异分母分数加减", "通分"],
        }
        payload.update(overrides)
        return self.service.add_problem(ProblemCreate(**payload))

    def test_review_updates_unit_and_appends_log(self):
        record = self._problem()
        unit = record.review_units[0]
        result = self.service.record_review(unit.id, RATING_GOOD, elapsed_ms=4200)

        refreshed = self.service.list_units(record.id)[0]
        self.assertEqual(refreshed.reps, 1)

        logs = self.service.review_history(unit.id)
        self.assertEqual(len(logs), 1)
        entry = logs[0]
        self.assertEqual(entry["rating"], RATING_GOOD)
        self.assertEqual(entry["elapsed_ms"], 4200)
        self.assertEqual(entry["state_before"], "new")
        self.assertEqual(entry["source"], "review")
        self.assertIsNotNone(entry["interval_days"])

        # 首次到期本来就是"明天",而固定间隔第一档也是 1 天 —— 所以要看两次才看得出推进
        self.service.record_review(unit.id, RATING_GOOD)
        advanced = self.service.list_units(record.id)[0]
        self.assertGreater(advanced.due_date_local, refreshed.due_date_local,
                           "连续答对后到期日必须往后走")
        self.assertEqual(len(self.service.review_history(unit.id)), 2)

    def test_log_records_last_interval_for_progress_comparison(self):
        """⭐ last_interval_days 与 interval_days 的对比就是"这次进步了没"。"""
        record = self._problem()
        unit_id = record.review_units[0].id
        first = self.service.record_review(unit_id, RATING_GOOD)
        second = self.service.record_review(unit_id, RATING_GOOD)
        logs = self.service.review_history(unit_id)
        self.assertEqual(len(logs), 2)
        self.assertIsNone(logs[1]["last_interval_days"], "第一次没有上一间隔")
        self.assertAlmostEqual(logs[0]["last_interval_days"], first.interval_days, places=6)
        self.assertGreaterEqual(second.interval_days, first.interval_days)

    def test_logs_are_append_only(self):
        record = self._problem()
        unit_id = record.review_units[0].id
        for _ in range(3):
            self.service.record_review(unit_id, RATING_GOOD)
        total = self.conn.execute("SELECT count(*) FROM app_review_logs").fetchone()[0]
        self.assertEqual(total, 3, "每次复习各留一条,绝不覆盖")

    def test_lapses_only_count_forgetting(self):
        record = self._problem()
        unit_id = record.review_units[0].id
        self.service.record_review(unit_id, RATING_GOOD)
        self.service.record_review(unit_id, RATING_GOOD)
        self.assertEqual(self.service.list_units(record.id)[0].lapses, 0)
        self.service.record_review(unit_id, RATING_AGAIN)
        self.assertEqual(self.service.list_units(record.id)[0].lapses, 1)

    def test_review_queue_is_child_view(self):
        record = self._problem()
        tomorrow = (NOW + timedelta(days=1)).date().isoformat()
        queue = self.service.review_queue(today=tomorrow)
        # ⭐ D108:一道题**一张卡**。原题 + 2 个知识点是 3 个**单元**,但孩子只该看到 1 道题 ——
        #    单元是记账单位,不是"这道题要做几遍"的单位(用户 2026-10-13 报的重复卡)。
        self.assertEqual(len(queue), 1, "同一道题的 3 个单元合成 1 张卡")
        card = queue[0]
        self.assertEqual(card["problem_id"], record.id)
        self.assertEqual(len(card["unit_ids"]), 3, "3 个单元都挂在这张卡上")
        self.assertEqual(card["unit_id"], card["unit_ids"][0], "老的单数 unit_id 仍然在")
        self.assertEqual(card["perspective"], "child")
        self.assertNotIn("answer_text", card)
        self.assertNotIn("explanation", card)
        self.assertEqual([b["rating"] for b in card["buttons"]], [1, 3])

    def test_two_problems_still_make_two_cards(self):
        """⚠️ 归并的是**同一道题的单元**,不是不同题目 —— 别把两道路题合成一张。"""
        self._problem()
        self.service.add_problem(ProblemCreate(subject="英语", stem_text="He ___ yesterday."))
        tomorrow = (NOW + timedelta(days=1)).date().isoformat()
        queue = self.service.review_queue(today=tomorrow)
        self.assertEqual(len(queue), 2)

    def test_one_tap_records_every_due_unit_and_undoes_together(self):
        """⭐ 一次作答 = 一次证据:当天到期的几个单元一起记,回执也只有**一条**。"""
        from la.domain.receipts import ReceiptLog

        # 这个用例要看回执,所以给服务配上回执账(其余用例不需要)
        self.service = StudyService(
            self.conn,
            media=MediaStore(self.conn, root=self.tmp / "media"),
            now_provider=lambda: NOW,
            receipts=ReceiptLog(self.conn),
        )
        record = self._problem()
        today = NOW.date().isoformat()
        self.conn.execute(
            "UPDATE app_review_units SET due_date_local = ? WHERE problem_id = ?",
            (today, record.id),
        )
        self.conn.commit()
        outcome = self.service.record_problem_review(record.id, RATING_GOOD, today=today)
        self.assertEqual(outcome["units"], 3, "一次作答记到当天到期的 3 个单元")
        self.assertEqual(len(outcome["log_ids"]), 3)
        logs = self.conn.execute("SELECT count(*) FROM app_review_logs").fetchone()[0]
        self.assertEqual(logs, 3, "每个单元各留一条日志(它是训练 FSRS 的依据)")
        receipts = [item for item in self.service.receipts.recent(limit=20)
                    if item.kind == "study.review"]
        self.assertEqual(len(receipts), 1, "点一次不该在回执托盘里冒出三条")
        self.assertEqual(receipts[0].undo_plan.get("type"), "review_batch")
        # 撤销:三个单元一起退回,三条日志一起删
        note = self.service.revert_review(receipts[0])
        self.assertIn("3 个单元", note)
        self.assertEqual(self.service.review_queue(today=today) and len(self.service.review_queue(today=today)), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM app_review_logs").fetchone()[0], 0)

    def test_card_carries_the_bound_figure_and_audio(self):
        """⭐ 入库时生成的图形与声音,孩子端卡片上要看得见(D107 / D104)。"""
        record = self._problem()
        self.conn.execute(
            "UPDATE app_problems SET figure_source = 'svg', figure_asset_id = NULL,"
            " figure_spec = ?, audio_artifact_id = 'fabc123', audio_spec = ? WHERE id = ?",
            (
                '{"kind": "unit_cubes_front", "description": "3+1",'
                ' "svg": "<svg xmlns=\'http://www.w3.org/2000/svg\'></svg>"}',
                '{"kind": "dictation", "label": "听写音频", "text": "一亩地", "seconds": 13.0,'
                ' "voice": "Cherry", "provider": "bailian", "artifact_id": "fabc123"}',
                record.id,
            ),
        )
        self.conn.commit()
        tomorrow = (NOW + timedelta(days=1)).date().isoformat()
        card = self.service.review_queue(today=tomorrow)[0]
        self.assertIn("<svg", card["figure_svg"])
        self.assertEqual(card["audio_handle"], "/artifacts/fabc123")
        self.assertEqual(card["audio_label"], "听写音频")
        self.assertIn("13.0 秒", card["audio_note"])
        # 家长端的错题详情里也要有(用户:"在界面里要能看到,方便用户确认")
        materials = self.service.problem_detail(record.id)["materials"]
        self.assertIn("<svg", materials["figure_svg"])
        self.assertEqual(materials["audio_text"], "一亩地")
        self.assertEqual(materials["audio_voice"], "Cherry")

    def test_reviewed_unit_leaves_todays_queue(self):
        """⭐ 复习过的单元**当天不会再被要求一次** —— 这是孩子端最容易被烦到的地方。"""
        record = self._problem()
        today = NOW.date().isoformat()
        self.conn.execute(
            "UPDATE app_review_units SET due_date_local = ? WHERE problem_id = ?",
            (today, record.id),
        )
        self.conn.commit()
        queue = self.service.review_queue(today=today)
        self.assertEqual(len(queue), 1, "同一道题只有一张卡")

        # 一次作答把这道题当天到期的单元全记上 → 这道题当天不再出现
        self.service.record_problem_review(record.id, RATING_GOOD, today=today)
        after = self.service.review_queue(today=today)
        self.assertEqual(after, [], "复习过的题不该在同一天的清单里再出现")

    def test_queue_filters_by_subject(self):
        self._problem()
        self.service.add_problem(ProblemCreate(subject="英语", stem_text="He ___ yesterday."))
        tomorrow = (NOW + timedelta(days=1)).date().isoformat()
        english = self.service.review_queue(subject="英语", today=tomorrow)
        self.assertTrue(all(card["subject"] == "英语" for card in english))

    def test_unknown_unit_raises(self):
        with self.assertRaises(StudyError):
            self.service.record_review(9999, RATING_GOOD)

    def test_weak_points_reflect_lapses(self):
        record = self._problem()
        target = record.review_units[1]
        self.service.record_review(target.id, RATING_AGAIN)
        stats = self.service.stats()
        top = stats.weak_points[0]
        self.assertEqual(top.lapses, 1)
        self.assertIn(top.knowledge_point, ("异分母分数加减", "通分"))

    def test_both_engines_work_end_to_end(self):
        for engine in ("fixed", "fsrs"):
            service = StudyService(
                self.conn,
                media=MediaStore(self.conn, root=self.tmp / "media"),
                now_provider=lambda: NOW,
                srs_engine=engine,
            )
            record = service.add_problem(
                ProblemCreate(subject="数学", stem_text=engine + " 的题", knowledge_points=[engine])
            )
            unit = record.review_units[0]
            first = service.record_review(unit.id, RATING_GOOD)
            self.assertTrue(first.due_date_local)
            second = service.record_review(unit.id, RATING_GOOD)
            self.assertGreaterEqual(second.interval_days, first.interval_days,
                                    engine + " 引擎:连续答对间隔不应缩短")


# ==========================================================================
# 工具层
# ==========================================================================
class ToolSchemaTestCase(unittest.TestCase):
    def test_quiz_me_and_result_schemas(self):
        me = QuizMe(subject="数学", count=5)
        self.assertEqual(me.count, 5)
        result = QuizResult(unit_id=3, rating=1)
        self.assertEqual(result.rating, 1)
        self.assertIn("只有这两个", QuizResult.model_fields["rating"].description)


if __name__ == "__main__":
    unittest.main()
