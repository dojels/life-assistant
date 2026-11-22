"""M14b 验收测试:模型独立解题 + 三方对照 + 图形裁剪落库。

这一组测试要守住的核心不变量只有一条:
**孩子的作答永远不能变成标准答案** —— 它只能进 student_answer,只能作为对照的一票。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from la.domain.media import MediaStore
from la.domain.services.study_service import StudyService
from la.domain.study.answers import (
    SOURCE_MODEL,
    STATUS_AGREED,
    STATUS_CONFLICT,
    STATUS_INCOMPLETE,
    STATUS_UNVERIFIED,
    compare_three_way,
    fingerprint,
    numbers,
    parse_solution,
    same_answer,
)
from la.domain.study.intake import IntakeService
from la.llm.vision import normalize_figure, normalize_rotation, parse_answer_probe
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT

TZ = ZoneInfo("Asia/Shanghai")
NOW = __import__("datetime").datetime(2026, 10, 13, 8, 0, tzinfo=TZ)


def make_png_blank(width: int = 400, height: int = 300) -> bytes:
    """一张"白纸"(实测里模型有一次把框落在这上面)。"""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def make_png(width: int = 400, height: int = 300) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), "white")
    ImageDraw.Draw(image).rectangle((100, 80, 300, 220), fill="blue")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class ScriptedClient:
    """按顺序把准备好的回复发出去,并记下每一次请求的 messages(用于断言"没喂错东西")。"""

    model = "stub"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        from la.llm.client import ChatResult

        self.calls.append(list(messages))
        reply = self.replies.pop(0) if self.replies else "{}"
        return ChatResult(text=reply, model=self.model)

    def transcript(self) -> str:
        parts: list[str] = []
        for messages in self.calls:
            for message in messages:
                content = message.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(str(block.get("text") or ""))
        return "\n".join(parts)


PROBE_REPLY = json.dumps({
    "is_problem": True,
    "student_answer": "7(红笔圈出)",
    "teacher_answer": "订:8",
    "figure": {"present": True, "kind": "小正方体组合体的正面视图",
               "description": "下面一排 3 个,中间上方 1 个", "bbox": [200, 100, 600, 700]},
    "rotation_degrees": 0,
}, ensure_ascii=False)

RECOGNITION_REPLY = json.dumps({
    "is_problem": True, "reason": "",
    "problems": [{
        "subject": "数学", "stem_text": "(3)有( )种不同的摆法。",
        "student_answer": "7", "teacher_answer": "订:8",
        "figure": {"present": True, "kind": "组合体正面视图", "bbox": [200, 100, 600, 700]},
        "knowledge_points": ["观察物体"], "confidence": 0.9,
    }],
}, ensure_ascii=False)

REFINE_REPLY = json.dumps({
    "complete": True, "bbox": [200, 100, 600, 700], "why": "图形完整",
}, ensure_ascii=False)

SOLVE_REPLY = json.dumps({
    "answer": "4 种", "steps": ["正面看到 4 个小正方形", "新方块必须藏在后面"],
    "answer_kind": "unique", "missing_info": [], "confidence": 0.9,
}, ensure_ascii=False)


class ParseTest(unittest.TestCase):
    def test_parse_solution_variants(self) -> None:
        good = parse_solution(SOLVE_REPLY)
        self.assertTrue(good.ok)
        self.assertEqual(good.answer, "4 种")
        self.assertEqual(good.steps[0], "正面看到 4 个小正方形")
        self.assertEqual(good.answer_kind, "unique")
        self.assertFalse(parse_solution("随便说点什么").ok)
        # 有答案但没写 kind:当"参考答案"处理,不假装唯一解
        self.assertEqual(parse_solution('{"answer":"大概 5 种"}').answer_kind, "reference")
        # 没答案也没 kind:判成"信息不足"
        self.assertEqual(parse_solution('{"steps":["看不懂"]}').answer_kind, "incomplete")

    def test_probe_parsing_keeps_roles_apart(self) -> None:
        got = parse_answer_probe(PROBE_REPLY)
        self.assertEqual(got["student_answer"], "7(红笔圈出)")
        self.assertEqual(got["teacher_answer"], "订:8")
        self.assertTrue(got["figure"]["present"])
        self.assertEqual(got["figure"]["bbox"], [200, 100, 600, 700])
        self.assertTrue(parse_answer_probe("不是 JSON").get("error"))

    def test_verify_parsing_is_robust(self) -> None:
        """模型爱写「第 4 张 = 270°」—— 别把「它答了」当成「它没答」,更别当成 0。"""
        from la.llm.vision import parse_verify

        got = parse_verify(
            json.dumps({"upright": "第 4 张 = 270°", "complete": "完整", "has_text": "没有"},
                       ensure_ascii=False)
        )
        self.assertEqual(got["upright"], 270)
        self.assertTrue(got["complete"])
        self.assertFalse(got["has_text"])
        # 没答 ≠ 答了 0;"第 4 张"(只有序号没有角度)也不许猜成 4
        self.assertIsNone(parse_verify('{"complete":true}')["upright"])
        self.assertIsNone(parse_verify('{"upright":4}')["upright"])

    def test_figure_and_rotation_normalisation(self) -> None:
        self.assertEqual(normalize_figure({"present": "是", "bbox": [1, 2, 3, 4]})["bbox"],
                         [1, 2, 3, 4])
        self.assertEqual(normalize_figure({"present": False, "bbox": [1, 2, 3, 4]}), {})
        self.assertEqual(normalize_figure(None), {})
        self.assertEqual(normalize_rotation(-90), 270)
        self.assertEqual(normalize_rotation(360), 0)
        self.assertEqual(normalize_rotation("乱写"), 0)


class CompareTest(unittest.TestCase):
    def test_agreed_when_teacher_and_model_match(self) -> None:
        status, note = compare_three_way(
            model_answer="54 分钟", teacher_answer="8.2×4+5.3×4=54(分)", student_answer="65")
        self.assertEqual(status, STATUS_AGREED)
        self.assertIn("一致", note)

    def test_conflict_is_not_auto_trusted(self) -> None:
        status, note = compare_three_way(
            model_answer="4 种", teacher_answer="8", student_answer="7")
        self.assertEqual(status, STATUS_CONFLICT)
        self.assertIn("老师批改", note)
        self.assertIn("孩子写的是", note)

    def test_unverified_when_no_teacher_mark(self) -> None:
        status, note = compare_three_way(model_answer="180", student_answer="176")
        self.assertEqual(status, STATUS_UNVERIFIED)
        self.assertIn("未经确认", note)

    def test_incomplete_names_what_is_missing(self) -> None:
        status, note = compare_three_way(
            model_answer="", answer_kind="incomplete", missing_info=["②号组合体的图形"])
        self.assertEqual(status, STATUS_INCOMPLETE)
        self.assertIn("②号组合体的图形", note)

    def test_mark_only_teacher_note_is_not_an_answer(self) -> None:
        """实测:老师只打了个"对号",却被当成老师给的答案抄进来 —— 那没法比。"""
        status, note = compare_three_way(model_answer="91", teacher_answer="对号")
        self.assertEqual(status, STATUS_UNVERIFIED)
        self.assertIn("对号", note)
        # 打对号 = 孩子写的那个是对的 → 孩子那一票可以当参考用
        status, _note = compare_three_way(
            model_answer="180", teacher_answer="对号", student_answer="180")
        self.assertEqual(status, STATUS_AGREED)

    def test_local_arithmetic_verification(self) -> None:
        """⭐ 纯算式一律本地验算:实测模型把 "26×7-(15-13)=?" 算成了 91。"""
        from la.domain.study.answers import contains_number, local_arithmetic

        self.assertEqual(local_arithmetic("26×7-(15-13)=?"), ("26×7-(15-13)", 180.0))
        self.assertEqual(local_arithmetic("3+4×2"), ("3+4×2", 11.0))
        # ⚠️ 分数题**不许**本地算:实测 "3/4 + 1/6" 会被算成 0.9167,而正确答案是 11/12
        self.assertIsNone(local_arithmetic("3/4 + 1/6 = ?"))
        self.assertIsNone(local_arithmetic("7÷3"))
        self.assertIsNone(local_arithmetic("估 75.9+14.6×10+32.5×2<300(元),你是怎么估的?"))
        self.assertIsNone(local_arithmetic("一个苹果 3 元,买 4 个多少钱?"))
        self.assertIsNone(local_arithmetic("5÷0"))
        self.assertTrue(contains_number("180", 180.0))
        self.assertTrue(contains_number("答:180。", 180.0))
        self.assertFalse(contains_number("91", 180.0))

    def test_wording_difference_is_not_a_conflict(self) -> None:
        """⭐ 实测:老师写"…54>50 所以50分不能,60分能",模型写"50分钟不能锯完;60分钟能锯完" ——
        数字对得上、结论一致,只是话不一样。判成 conflict 会把**答对了的题**拦下来。"""
        status, note = compare_three_way(
            model_answer="50 分钟不能锯完;60 分钟能锯完。",
            teacher_answer="8.2×4+5.3×4=54(分) 54>50 所以 50 分不能,60 分能",
        )
        self.assertEqual(status, STATUS_AGREED)
        self.assertIn("措辞不同", note)

    def test_opposite_conclusions_still_conflict(self) -> None:
        status, _note = compare_three_way(
            model_answer="50 分钟能锯完", teacher_answer="50 分钟不能锯完")
        self.assertEqual(status, STATUS_CONFLICT)

    def test_same_answer_is_conservative(self) -> None:
        self.assertTrue(same_answer("54 分钟", "54"))
        self.assertTrue(same_answer("一亩地", "一亩地"))
        self.assertFalse(same_answer("7", "8"))
        self.assertFalse(same_answer("", "8"))
        # 数字一样但结论相反 —— 不许判成一致(实测踩过的坑)
        self.assertFalse(same_answer("50 分钟能锯完", "50 分钟不能锯完"))
        self.assertEqual(numbers("8.2×4=32.8 分钟"), ["8.2", "4", "32.8"])
        self.assertEqual(fingerprint("答:54 分钟。"), "54")


class ResolveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.media = MediaStore(self.conn, root=self.tmp / "media")
        self.study = StudyService(self.conn, media=self.media, now_provider=lambda: NOW)
        # 三次调用:① 看照片(抄角色 + 找图形)② 复核裁剪框 ③ 独立解题
        self.client = ScriptedClient([PROBE_REPLY, REFINE_REPLY, SOLVE_REPLY])
        self.intake = IntakeService(
            self.conn, study=self.study, media=self.media,
            client_factory=lambda: self.client, now_provider=lambda: NOW,
        )

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _seed(self, *, with_photo: bool = True) -> int:
        from la.domain.schemas.study import ProblemCreate

        asset_ids = []
        if with_photo:
            asset = self.media.store_bytes(make_png(), media_type="image/png", subdir="problems")
            asset_ids = [asset.id]
        record = self.study.add_problem(
            ProblemCreate(subject="数学", stem_text="(3)有( )种不同的摆法。",
                          knowledge_points=["观察物体"], asset_ids=asset_ids),
            actor="user",
        )
        # 老数据的模样:照片上的答案被抄进 answer_text,migration 13 标成 legacy
        self.conn.execute(
            "UPDATE app_problems SET answer_text = '7；订：8', answer_source = 'legacy',"
            " answer_status = 'unverified' WHERE id = ?", (record.id,),
        )
        self.conn.commit()
        return record.id

    def test_resolve_separates_roles_and_flags_conflict(self) -> None:
        problem_id = self._seed()
        result = self.intake.resolve_problem(problem_id)
        self.assertFalse(result.get("error"), result)
        self.assertEqual(result["answer"], "4 种")
        self.assertEqual(result["answer_status"], STATUS_CONFLICT)
        row = self.conn.execute("SELECT * FROM app_problems WHERE id = ?", (problem_id,)).fetchone()
        self.assertEqual(row["answer_text"], "4 种")          # 标准答案 = 模型独立解出的
        self.assertEqual(row["answer_source"], SOURCE_MODEL)
        self.assertEqual(row["student_answer"], "7(红笔圈出)")  # 孩子的作答单独一列
        self.assertEqual(row["teacher_answer"], "订:8")         # 老师那一票单独一列
        self.assertEqual(json.loads(row["solution_steps"])[0], "正面看到 4 个小正方形")
        self.assertIsNotNone(row["figure_asset_id"])            # 图形裁出来并落了库
        self.assertEqual(row["figure_source"], "crop")
        figure = self.media.get(int(row["figure_asset_id"]))
        self.assertGreater(figure.byte_size, 0)

    def test_solver_never_sees_the_child_answer(self) -> None:
        """⭐ 这条是整套设计的命门:解题调用里不许出现孩子的作答。"""
        problem_id = self._seed()
        self.intake.resolve_problem(problem_id)
        # 三次:看照片 → 复核裁剪框 → 独立解题
        self.assertEqual(len(self.client.calls), 3)
        transcript = self.client.transcript()
        # 模型在第一次回复里"看到"了孩子的 7,但**请求**里从头到尾不许带它
        self.assertNotIn("7(红笔圈出)", transcript)
        self.assertNotIn('"student_answer": "7', transcript)
        self.assertIn("(3)有( )种不同的摆法。", transcript)
        # 解题那一次的 messages 只有 user 一条(题干 + 图形),没有任何历史
        solve_messages = self.client.calls[-1]
        self.assertEqual([message["role"] for message in solve_messages], ["user"])

    def test_accept_auto_solves(self) -> None:
        """⭐ M14c:录进来就自动有标准答案与图形 —— 不必再手动跑一次 --resolve-answers。"""
        self.client.replies = [RECOGNITION_REPLY, PROBE_REPLY, REFINE_REPLY, SOLVE_REPLY]
        session_id = self.intake.start_session()
        candidate = self.intake.add_image(session_id, make_png(), original_name="题.png")
        self.intake.recognize_candidate(candidate.id)
        result = self.intake.accept(candidate.id)
        self.assertEqual(result["answer"], "4 种")
        self.assertEqual(result["answer_status"], STATUS_CONFLICT)
        self.assertEqual(result["figure_source"], "crop")
        row = self.conn.execute("SELECT * FROM app_problems WHERE id = ?",
                                (result["problem_id"],)).fetchone()
        self.assertEqual(row["answer_source"], SOURCE_MODEL)
        self.assertIsNotNone(row["figure_asset_id"])

    def test_degenerate_probe_bbox_falls_back_to_recognition(self) -> None:
        """⭐ 真机踩过(2026-11-21 错题 #30):补拍这一趟"说是有图、框却退化了",
        而识别那一趟其实给过能用的框。没有这条兜底,卷面上就只剩一句
        "识别说这题有图形,但这次没取到"。"""
        degraded = json.dumps({
            "is_problem": True, "student_answer": "", "teacher_answer": "",
            "figure": {"present": True, "kind": "长方形示意图", "bbox": [0, 0, 0, 0]},
            "rotation_degrees": 0,
        }, ensure_ascii=False)
        self.client.replies = [RECOGNITION_REPLY, degraded, REFINE_REPLY, SOLVE_REPLY]
        session_id = self.intake.start_session()
        candidate = self.intake.add_image(session_id, make_png(), original_name="题.png")
        self.intake.recognize_candidate(candidate.id)
        result = self.intake.accept(candidate.id)
        self.assertIsNotNone(result["figure_asset_id"])
        row = self.conn.execute(
            "SELECT figure_spec FROM app_problems WHERE id = ?", (result["problem_id"],)
        ).fetchone()
        spec = json.loads(row["figure_spec"])
        self.assertEqual(spec["probe"]["bbox_source"], "recognize")
        self.assertTrue(any("改用识别那一趟的框" in note for note in result["resolve_notes"]))

    def test_accept_never_overwrites_a_human_answer(self) -> None:
        """人手填的答案优先:标 manual,而且模型**不许**覆盖它。"""
        self.client.replies = [RECOGNITION_REPLY, PROBE_REPLY, REFINE_REPLY, SOLVE_REPLY]
        session_id = self.intake.start_session()
        candidate = self.intake.add_image(session_id, make_png(), original_name="题.png")
        self.intake.recognize_candidate(candidate.id)
        result = self.intake.accept(candidate.id, overrides={"answer_text": "我自己填的 9 种"})
        row = self.conn.execute("SELECT * FROM app_problems WHERE id = ?",
                                (result["problem_id"],)).fetchone()
        self.assertEqual(row["answer_text"], "我自己填的 9 种")
        self.assertEqual(row["answer_source"], "manual")

    def test_resolve_without_photo_still_solves_from_stem(self) -> None:
        problem_id = self._seed(with_photo=False)
        self.client.replies = [SOLVE_REPLY]                     # 没有照片 → 只有解题那一次
        result = self.intake.resolve_problem(problem_id)
        self.assertEqual(result["answer"], "4 种")
        self.assertEqual(result["answer_status"], STATUS_UNVERIFIED)
        self.assertEqual(len(self.client.calls), 1)

    def test_incomplete_clears_the_old_photo_answer(self) -> None:
        problem_id = self._seed()
        self.client.replies = [
            PROBE_REPLY,
            REFINE_REPLY,
            json.dumps({"answer": "", "steps": [], "answer_kind": "incomplete",
                        "missing_info": ["②号组合体的图形"], "confidence": 0.2},
                       ensure_ascii=False),
        ]
        result = self.intake.resolve_problem(problem_id)
        row = self.conn.execute("SELECT * FROM app_problems WHERE id = ?", (problem_id,)).fetchone()
        self.assertEqual(result["answer_status"], STATUS_INCOMPLETE)
        self.assertIsNone(row["answer_text"])                   # 老答案被清空
        self.assertIn("已清空", row["answer_note"])

    def test_resolve_overrides_wrong_model_arithmetic(self) -> None:
        """⭐ 模型算错了(实测把 26×7-(15-13) 算成 91),本地验算改回来并写进备注。"""
        from la.domain.schemas.study import ProblemCreate

        record = self.study.add_problem(
            ProblemCreate(subject="数学", stem_text="26×7-(15-13)=?",
                          knowledge_points=["四则混合运算"]),
            actor="user",
        )
        self.client.replies = [
            json.dumps({"answer": "91", "steps": ["26×7=182", "182÷2=91"],
                        "answer_kind": "unique", "missing_info": [], "confidence": 0.9},
                       ensure_ascii=False),
        ]
        result = self.intake.resolve_problem(record.id)
        self.assertEqual(result["answer"], "180")
        self.assertTrue(any("本地验算" in note for note in result["notes"]))
        row = self.conn.execute("SELECT solution_steps FROM app_problems WHERE id = ?",
                                (record.id,)).fetchone()
        self.assertIn("26×7-(15-13) = 180", row["solution_steps"])

    def test_blank_crop_is_not_a_figure(self) -> None:
        """⭐ 实测:模型有一次把框落在**空白纸面**上,裁出来是纯纸。宁可说"没取到",
        也不能把一张白纸当成"题目图形"印在卷子上。"""
        from la.domain.study.figure import bbox_to_thousand, ink_ratio, looks_blank

        self.assertEqual(bbox_to_thousand([0.7356, 0.3368, 0.8444, 0.6632]),
                         [736, 337, 844, 663])
        blank = make_png_blank()
        self.assertTrue(looks_blank(blank))
        self.assertLess(ink_ratio(blank), 0.012)
        self.assertFalse(looks_blank(make_png()))

    def test_verify_can_override_the_model_rotation(self) -> None:
        """⭐ 真机踩过(#31):模型报"顺时针 90°",复核（四个方向各发一张）说是 270 ——
        以前这种错只能等家长看见"图怎么是反的",现在复核当场改掉并重裁。"""
        problem_id = self._seed()
        verify = json.dumps({"upright": 270, "complete": True, "has_text": False,
                             "why": "第 4 张才是正的"}, ensure_ascii=False)
        self.client.replies = [PROBE_REPLY, verify, SOLVE_REPLY]
        result = self.intake.resolve_problem(problem_id)
        row = self.conn.execute(
            "SELECT figure_spec FROM app_problems WHERE id = ?", (problem_id,)
        ).fetchone()
        spec = json.loads(row["figure_spec"])
        self.assertEqual(spec["rotation_applied"], 270)
        self.assertEqual(spec["probe"]["rotation_guess"], 0)
        self.assertEqual(spec["verify"]["upright"], 270)
        self.assertTrue(any("方向按复核改了" in note for note in result["notes"]), result["notes"])

    def test_verify_sees_text_and_flags_needs_check(self) -> None:
        """复核说"框里混进了题干文字" —— 不瞎改框,但要在页面上如实标出来。"""
        problem_id = self._seed()
        verify = json.dumps({"upright": 0, "complete": True, "has_text": True,
                             "why": "带了一句题干"}, ensure_ascii=False)
        self.client.replies = [PROBE_REPLY, verify, SOLVE_REPLY]
        result = self.intake.resolve_problem(problem_id)
        row = self.conn.execute(
            "SELECT figure_spec, figure_asset_id FROM app_problems WHERE id = ?", (problem_id,)
        ).fetchone()
        spec = json.loads(row["figure_spec"])
        self.assertTrue(spec["needs_check"])
        self.assertIsNotNone(row["figure_asset_id"])      # 图还是留着,只是标了"要核对"
        self.assertTrue(any("题干文字" in note for note in result["notes"]), result["notes"])

    def test_manual_recrop_rotates_without_any_model(self) -> None:
        """⭐ 人工返工:按**已存的框**重裁,一次模型都不调(这是最便宜的兜底)。"""
        problem_id = self._seed()
        self.intake.resolve_problem(problem_id)
        before = self.conn.execute(
            "SELECT figure_asset_id, figure_spec FROM app_problems WHERE id = ?", (problem_id,)
        ).fetchone()
        calls = len(self.client.calls)
        got = self.study.recrop_figure(problem_id, rotate_delta=90)
        self.assertEqual(len(self.client.calls), calls)   # 没有模型调用
        self.assertNotEqual(got["figure_asset_id"], before["figure_asset_id"])
        after = self.conn.execute(
            "SELECT figure_spec FROM app_problems WHERE id = ?", (problem_id,)
        ).fetchone()
        spec = json.loads(after["figure_spec"])
        self.assertEqual(spec["rotation_applied"], 90)
        self.assertFalse(spec["needs_check"])
        # 再收窄一点:框必须变小(而不是原样返回)
        tight = self.study.recrop_figure(problem_id, zoom=-0.2)
        self.assertLess(tight["width"] * tight["height"], got["width"] * got["height"])

    def test_already_resolved_is_skipped_unless_forced(self) -> None:
        problem_id = self._seed()
        self.intake.resolve_problem(problem_id)
        again = self.intake.resolve_problem(problem_id)
        self.assertTrue(again.get("skipped"))
        forced = self.intake.resolve_problem(problem_id, force=True)
        self.assertFalse(forced.get("skipped"))

    def test_batch_does_not_skip_legacy_rows(self) -> None:
        """⭐ legacy 恰恰是"抄了孩子答案"的老数据,补答案时必须重跑它。"""
        problem_id = self._seed()
        self.client.replies = [PROBE_REPLY, REFINE_REPLY, SOLVE_REPLY]
        report = self.intake.resolve_answers(limit=10)
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["items"][0]["problem_id"], problem_id)


if __name__ == "__main__":
    unittest.main()
