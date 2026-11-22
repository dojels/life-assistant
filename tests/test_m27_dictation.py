"""D160:认读发音 + 每日合并听写。

两条用户 2026-10-24 的要求:
  * 单词认读题要有标准发音(先自己读,再听对照);
  * 同一天多道听写要并成一段,语文和英语分开。
"""

from __future__ import annotations

import sqlite3
import tempfile
import types
import unittest
from pathlib import Path

from la.domain.study.audio import (
    KIND_DICTATION,
    KIND_FOLLOW,
    KIND_LISTENING,
    merge_audio_specs,
    merge_dictation_specs,
    plan_from_problem,
    spec_payload,
)


class ReadAloudTest(unittest.TestCase):
    def test_read_aloud_problem_gets_single_pass_audio(self) -> None:
        plan = plan_from_problem(
            stem="单词认读(星期二到星期五):Tuesday、Wednesday、Thursday、Friday",
            answer="",
            subject="英语",
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan.kind, KIND_LISTENING)
        self.assertEqual(plan.purpose, "read_aloud")
        self.assertEqual(plan.repeat, 1)
        self.assertIn("Tuesday", plan.text)
        self.assertEqual(plan.label, "认读音频")

    def test_follow_read_reads_each_item_once_with_a_gap(self) -> None:
        """跟读是"先听后跟":一条只念一遍,念完留白让他跟(不是报两遍的听写)。

        ⚠️ 2026-11-09 用户报"take care of 被拆成三个词分开读"之后,跟读从听写里拆了出来:
        词表按条念一遍 + 留白;整句(短语)则**整句连读两遍** —— 两条路都不拆短语。
        """
        plan = plan_from_problem(stem="跟读单词:Monday、Tuesday", subject="英语")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.kind, KIND_FOLLOW)
        self.assertEqual(plan.items, ["Monday", "Tuesday"])
        self.assertEqual(plan.repeat, 1)

    def test_follow_phrase_is_read_as_one_sentence(self) -> None:
        """短语跟读:整句连读两遍,中间留白 —— 绝不按空格拆成单词。"""
        plan = plan_from_problem(
            stem="英语跟读：take care of（照顾、照料）", answer="take care of", subject="英语",
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan.kind, KIND_FOLLOW)
        self.assertEqual(plan.text, "take care of")
        self.assertEqual(plan.items, [])
        self.assertEqual(plan.repeat, 2)
        self.assertNotIn("、", plan.text)

    def test_dictation_wins_when_both_hints_present(self) -> None:
        plan = plan_from_problem(stem="听写并认读:一亩地、庄稼", subject="语文")
        self.assertEqual(plan.kind, KIND_DICTATION)

    def test_purpose_goes_into_spec(self) -> None:
        plan = plan_from_problem(stem="读一读:apple、pear", subject="英语")
        payload = spec_payload(plan)
        self.assertEqual(payload.get("purpose"), "read_aloud")
        self.assertEqual(payload.get("label"), "认读音频")

    def test_knowledge_point_name_counts_as_hint(self) -> None:
        """题干只有词表、线索在知识点名里(截图 #13 就是这样)。"""
        plan = plan_from_problem(
            stem="Tuesday、Wednesday、Thursday", subject="英语",
            extra_hints="单词认读",
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan.purpose, "read_aloud")
        self.assertIn("Tuesday", plan.text)

    def test_read_aloud_without_material_is_reported(self) -> None:
        plan = plan_from_problem(stem="朗读课文第 3 段", subject="语文")
        # 「朗读」走听力线,但只有定位没有内容 → 必须如实说没材料,不能编
        self.assertIsNotNone(plan)
        self.assertFalse(plan.ok)


class MergeTest(unittest.TestCase):
    def test_groups_by_language_and_dedupes(self) -> None:
        entries = [
            {"problem_id": 1, "subject": "英语",
             "spec": {"kind": "dictation", "text": "apple、pear"}},
            {"problem_id": 2, "subject": "语文",
             "spec": {"kind": "dictation", "text": "一亩地、庄稼"}},
            {"problem_id": 3, "subject": "英语",
             "spec": {"kind": "dictation", "text": "pear、banana"}},
            {"problem_id": 4, "subject": "英语",
             "spec": {"kind": "listening", "text": "不是听写"}},
        ]
        groups = merge_dictation_specs(entries)
        self.assertEqual(len(groups), 2)
        by_lang = {g["language"]: g for g in groups}
        self.assertEqual(by_lang["en"]["words"], ["apple", "pear", "banana"])
        self.assertEqual(by_lang["en"]["problem_ids"], [1, 3])
        self.assertEqual(by_lang["zh"]["words"], ["一亩地", "庄稼"])

    def test_empty_when_no_dictation(self) -> None:
        self.assertEqual(merge_dictation_specs([{"spec": {"kind": "none"}}]), [])

    def test_follow_groups_by_language_and_dedupes(self) -> None:
        """2026-11-09 用户问:"如果一天同时有多个跟读,能合并吗" —— 能,按语言并成一段。"""
        entries = [
            {"problem_id": 1, "spec": {"kind": "follow", "text": "take care of"}},
            {"problem_id": 2, "spec": {"kind": "follow", "text": "think of"}},
            {"problem_id": 3, "spec": {"kind": "follow", "text": "take care of"}},
            {"problem_id": 4, "spec": {"kind": "follow", "text": "床前明月光"}},
        ]
        groups = merge_audio_specs(entries)
        self.assertEqual(len(groups), 2)
        by_lang = {g["language"]: g for g in groups}
        self.assertEqual(by_lang["en"]["kind"], KIND_FOLLOW)
        self.assertEqual(by_lang["en"]["label"], "英语跟读")
        self.assertEqual(by_lang["en"]["phrases"], ["take care of", "think of"])
        self.assertEqual(by_lang["en"]["problem_ids"], [1, 2, 3])
        self.assertEqual(by_lang["zh"]["phrases"], ["床前明月光"])

    def test_a_phrase_stays_one_item_when_merging(self) -> None:
        """⚠️ 合并时也**不许**把短语拆开:句内逗号是停顿,不是分隔符。"""
        entries = [{
            "problem_id": 1,
            "spec": {"kind": "follow",
                     "text": "What's he like?，What does he like?"},
        }]
        groups = merge_audio_specs(entries)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["phrases"], ["What's he like?，What does he like?"])

    def test_follow_list_mode_keeps_its_items(self) -> None:
        """列表模式的跟读(「跟读单词:Monday、Tuesday」)按条并,不并成一句。"""
        entries = [{
            "problem_id": 7,
            "spec": {"kind": "follow", "text": "Monday、Tuesday",
                     "items": ["Monday", "Tuesday"]},
        }]
        groups = merge_audio_specs(entries)
        self.assertEqual(groups[0]["phrases"], ["Monday", "Tuesday"])

    def test_dictation_and_follow_are_not_merged_together(self) -> None:
        """两种节奏不许混:听写是报词,跟读是念句。"""
        entries = [
            {"problem_id": 1, "spec": {"kind": "dictation", "text": "apple、pear"}},
            {"problem_id": 2, "spec": {"kind": "follow", "text": "take care of"}},
        ]
        groups = merge_audio_specs(entries)
        self.assertEqual({g["kind"] for g in groups}, {KIND_DICTATION, KIND_FOLLOW})
        self.assertEqual({g["label"] for g in groups}, {"英语听写", "英语跟读"})

    def test_limit_marks_truncated(self) -> None:
        entries = [{"problem_id": 1, "spec": {"kind": "dictation", "text": "a、b、c、d"}}]
        groups = merge_dictation_specs(entries, limit=2)
        self.assertEqual(groups[0]["words"], ["a", "b"])
        self.assertTrue(groups[0].get("truncated"))


class CachedAudioTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(str(Path(self._tmp.name) / "assistant.db"))
        self.conn.row_factory = sqlite3.Row
        from la.storage import db
        from la.storage.migrations import MIGRATIONS_ASSISTANT

        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.artifacts = FakeArtifacts()
        from la.domain.services.study_service import StudyService

        self.service = StudyService(self.conn, artifacts=self.artifacts)
        FakeEngine.calls = 0
        FakeEngine.follow_calls = 0

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_merged_follow_list_uses_the_follow_engine(self) -> None:
        """合并跟读走 build_follow(每条念两遍、留白),不是 build_dictation。"""
        import la.domain.study.tts as tts_module

        original = tts_module.TtsEngine
        tts_module.TtsEngine = FakeEngine  # type: ignore[assignment]
        try:
            got = self.service._merged_audio(
                "2026-11-09",
                {"kind": KIND_FOLLOW, "phrases": ["take care of", "think of"],
                 "language": "en", "label": "英语跟读"},
            )
        finally:
            tts_module.TtsEngine = original  # type: ignore[assignment]
        self.assertEqual(got["audio_url"], "/artifacts/1")
        self.assertEqual(FakeEngine.follow_calls, 1)

    def test_merged_audio_is_built_once_and_cached(self) -> None:
        import la.domain.study.tts as tts_module

        original = tts_module.TtsEngine
        FakeEngine.calls = 0
        tts_module.TtsEngine = FakeEngine  # type: ignore[assignment]
        try:
            group = {"words": ["apple", "pear"], "language": "en", "label": "英语听写"}
            first = self.service._merged_audio("2026-10-24", group)
            second = self.service._merged_audio("2026-10-24", group)
        finally:
            tts_module.TtsEngine = original  # type: ignore[assignment]
        self.assertEqual(first["audio_url"], "/artifacts/1")
        self.assertEqual(second["audio_url"], "/artifacts/1")
        self.assertEqual(FakeEngine.calls, 1)
        self.assertEqual(len(self.artifacts.items), 1)

    def test_changed_words_rebuild(self) -> None:
        import la.domain.study.tts as tts_module

        original = tts_module.TtsEngine
        FakeEngine.calls = 0
        tts_module.TtsEngine = FakeEngine  # type: ignore[assignment]
        try:
            self.service._merged_audio(
                "2026-10-24", {"words": ["apple"], "language": "en", "label": "英语听写"}
            )
            second = self.service._merged_audio(
                "2026-10-24", {"words": ["apple", "pear"], "language": "en", "label": "英语听写"}
            )
        finally:
            tts_module.TtsEngine = original  # type: ignore[assignment]
        self.assertEqual(FakeEngine.calls, 2)
        self.assertEqual(second["audio_url"], "/artifacts/2")


class FakeArtifacts:
    def __init__(self) -> None:
        self.items: dict[int, bytes] = {}
        self.next_id = 1

    def exists(self, artifact_id: int) -> bool:
        return int(artifact_id) in self.items

    def register_bytes(self, data, **kwargs):
        artifact_id = self.next_id
        self.next_id += 1
        self.items[artifact_id] = data
        return types.SimpleNamespace(id=artifact_id)


class FakeEngine:
    calls = 0
    follow_calls = 0

    def build_follow(self, items, **kwargs):
        FakeEngine.calls += 1
        FakeEngine.follow_calls += 1
        return types.SimpleNamespace(
            ok=True, data=b"fake-mp3", media_type="audio/mpeg", seconds=6.4,
            provider="fake", voice="v", spent_yuan=0.0, error="", notes=[],
        )

    def build_dictation(self, words, **kwargs):
        FakeEngine.calls += 1
        return types.SimpleNamespace(
            ok=True, data=b"fake-mp3", media_type="audio/mpeg", seconds=3.2,
            provider="fake", voice="v", spent_yuan=0.0, error="", notes=[],
        )


if __name__ == "__main__":
    unittest.main()
