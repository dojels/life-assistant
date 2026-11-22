"""摘要格式与长度:LLM 输出不再因为缺 "- " 被整条丢掉。

真机事故:模型输出
    时段: ...
    事项: ...
校验器只认 "- 时段:",7 个分节全判缺失 -> 每条摘要都回落抽取式 ->
447 个锚点 / 4,497 字 -> 热记忆只取前 300 字、检索一次超过 4,000 字符上限。
"""

from __future__ import annotations

import dataclasses
import unittest

from la import config
from la.llm.fake import FakeClient
from la.memory.archive import Event
from la.memory.summarize import (
    MAX_ANCHORS,
    MAX_SUMMARY_CHARS,
    LLMSummarizer,
    extractive_summary,
    normalize_summary,
    validate_summary,
)

BODY = """<compacted-summary>
时段: 2026-11-20T10:00:00+08:00 — 2026-11-20T18:00:06+08:00
事项: 建明天 15:00 医院复查提醒
事实: 挂号费 50 元
偏好: (none)
待办: 明天去医院复查
锚点: [2026-11-20, 50元, 医院]
其他: (none)
</compacted-summary>"""


def settings():
    return dataclasses.replace(config.get_settings(reload=True), provider="fake")


def events():
    return [
        Event(seq=1, type="user/message", time="2026-11-20T10:00:00+08:00",
              data={"text": "明天下午三点提醒我去医院复查,挂号费 50 元。" + "背景说明" * 120}),
        Event(seq=2, type="assistant/message", time="2026-11-20T10:00:05+08:00",
              data={"text": "已建好提醒并记了一笔 50 元医疗支出"}),
    ]


class NormalizeCase(unittest.TestCase):
    def test_no_bullet_headings_are_normalized_and_validated(self) -> None:
        normalized = normalize_summary(BODY)
        for section in ("时段", "事项", "事实", "偏好", "待办", "锚点", "其他"):
            self.assertIn("- " + section + ":", normalized)
        ok, problems = validate_summary(BODY, "源文本" * 500)
        self.assertTrue(ok, problems)

    def test_missing_wrapper_is_added(self) -> None:
        normalized = normalize_summary(BODY.replace("<compacted-summary>", "").replace("</compacted-summary>", ""))
        self.assertIn("<compacted-summary>", normalized)
        self.assertIn("</compacted-summary>", normalized)

    def test_heading_variants_are_accepted(self) -> None:
        variant = BODY.replace("时段:", "## 时段:").replace("事项:", "**事项**:").replace("事实:", "1. 事实:")
        ok, problems = validate_summary(variant, "源文本" * 500)
        self.assertTrue(ok, problems)

    def test_too_long_summary_is_rejected(self) -> None:
        long_body = BODY.replace("事项: 建明天 15:00 医院复查提醒", "事项: " + "字" * (MAX_SUMMARY_CHARS + 100))
        ok, problems = validate_summary(long_body, "源文本" * 1000)
        self.assertFalse(ok)
        self.assertTrue(any("超过" in item for item in problems), problems)


class LLMSummarizerCase(unittest.IsolatedAsyncioTestCase):
    async def test_model_output_without_bullets_is_used_not_discarded(self) -> None:
        client = FakeClient(scripted=[BODY])
        draft = await LLMSummarizer(client, settings()).summarize(
            events(), start_time="2026-11-20T10:00:00+08:00", end_time="2026-11-20T18:00:06+08:00"
        )
        self.assertEqual(draft.source, "llm", draft.problems)
        self.assertTrue(draft.valid)
        self.assertIn("- 时段:", draft.text)

    async def test_oversized_model_output_falls_back_but_is_capped(self) -> None:
        huge = BODY.replace("事项: 建明天 15:00 医院复查提醒", "事项: " + "字" * (MAX_SUMMARY_CHARS + 100))
        client = FakeClient(scripted=[huge])
        draft = await LLMSummarizer(client, settings()).summarize(
            events(), start_time="2026-11-20T10:00:00+08:00", end_time="2026-11-20T18:00:06+08:00"
        )
        self.assertEqual(draft.source, "extractive")
        self.assertFalse(draft.valid)
        self.assertLessEqual(len(draft.text), MAX_SUMMARY_CHARS)


class ExtractiveCapCase(unittest.TestCase):
    def test_anchors_and_length_are_capped(self) -> None:
        events_list = []
        for index in range(80):
            events_list.append(
                Event(
                    seq=index + 1,
                    type="user/message",
                    time="2026-11-20T10:00:00+08:00",
                    data={"text": "2026-01-%02d 花了 %d 元,项目 Alpha%d" % (index % 28 + 1, index + 1, index)},
                )
            )
        draft = extractive_summary(
            events_list, start_time="2026-11-20T10:00:00+08:00", end_time="2026-11-20T18:00:00+08:00"
        )
        self.assertLessEqual(len(draft.anchors), MAX_ANCHORS)
        self.assertLessEqual(len(draft.text), MAX_SUMMARY_CHARS)
        for section in ("时段", "事项", "事实", "偏好", "待办", "锚点", "其他"):
            self.assertIn("- " + section + ":", draft.text)


if __name__ == "__main__":
    unittest.main()
