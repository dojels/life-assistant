"""热记忆头部 300 → 500 字(2026-11-21)。"""

from __future__ import annotations

import unittest

from la import config
from la.memory.projector import build_memory_block
from la.memory.store import SummaryRow


def summary(index: int, text: str) -> SummaryRow:
    return SummaryRow(
        id=index,
        session_id="s1",
        start_seq=index,
        end_seq=index,
        start_time="2026-11-20T10:00:00+08:00",
        end_time="2026-11-20T11:00:00+08:00",
        text=text,
        anchors=[],
        tags=["memory"],
        category="periodic",
        base_strength=1.0,
        visits=0,
        last_access=None,
        heat_cache=None,
        created_at="2026-11-20T11:00:00+08:00",
    )


class HotSummaryHeadCase(unittest.TestCase):
    def test_head_is_500_chars(self) -> None:
        self.assertEqual(config.HOT_SUMMARY_HEAD_CHARS, 500)
        block = build_memory_block(
            facts=[],
            summaries=[summary(1, "甲" * 900)],
            status="hit",
            now_text="",
        )
        self.assertEqual(block.count("甲"), 500, "热记忆应取到 500 字,而不是旧的 300")

    def test_more_head_chars_means_fewer_summaries_fit(self) -> None:
        rows = [summary(index, ("乙%d" % index) * 500) for index in range(1, 21)]
        block = build_memory_block(
            facts=[], summaries=rows, status="hit", now_text=""
        )
        self.assertLessEqual(
            config.estimate_tokens(block), config.BUDGET_HOT_SUMMARY,
            "500 字头不能突破 1500 token 的记忆预算",
        )
        self.assertLess(block.count("乙"), 20 * 500, "预算下不可能 20 条都放满 500 字")


if __name__ == "__main__":
    unittest.main()
