"""记忆块的分段预算 / 事实相关性 / 领域覆盖(2026-11-07)。

修的是评审第三条:1500 token 的单块里,健康/账目/错题/装修共用一个 top-k,
事实先来先占、摘要只能捡剩下的。现在:
  * 事实有独立子预算(FACT_BUDGET_TOKENS),不再把摘要挤没;
  * 事实先相关、再补最近,不再"最近 20 条"整包注入;
  * 块内先让每个领域露一次脸,裁预算时不会整段丢掉某个领域。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from la import config
from la.memory.projector import build_memory_block
from la.memory.store import FactRow, MemoryStore, SummaryRow
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY

DOMAINS = ("health", "ledger", "study", "task")


def _summary(index: int, domain: str, text: str | None = None, tags: list[str] | None = None) -> SummaryRow:
    body = text or (domain + " 摘要 " + "细节" * 140)
    return SummaryRow(
        index, "s1", index, index, "2026-11-01T10:00", "2026-11-01T10:05",
        body[:300], [], tags if tags is not None else [domain], "periodic",
        1.0, 0, None, None, "2026-11-01T10:00:00+08:00",
    )


def _fact(index: int, text: str | None = None) -> FactRow:
    body = text or ("事实" + str(index) + " " + "说明" * 55)
    return FactRow(index, "other", "permanent", body[:120], [], True, None)


class BlockBudgetTest(unittest.TestCase):
    def test_block_fits_budget_and_keeps_every_domain(self) -> None:
        summaries = [_summary(i + 1, DOMAINS[i % 4]) for i in range(10)]
        facts = [_fact(i) for i in range(1, 41)]
        block = build_memory_block(facts=facts, summaries=summaries, status="hit", now_text="")

        self.assertLessEqual(
            config.estimate_tokens(block), config.BUDGET_HOT_SUMMARY,
            "整块必须装进 1500 token 的预算",
        )
        for domain in DOMAINS:
            self.assertIn("[" + domain + "]", block, domain + " 的摘要被挤没了")
        self.assertLess(
            block.count("- 事实"), len(facts),
            "事实要按 FACT_BUDGET_TOKENS 裁,不能 40 条全塞进来",
        )
        self.assertGreaterEqual(
            block.count("- ["), 8, "摘要仍要拿到自己的预算(不能被事实挤到个位数)"
        )

    def test_tight_budget_still_shows_more_than_one_domain(self) -> None:
        summaries = [_summary(i + 1, DOMAINS[i % 4]) for i in range(8)]
        block = build_memory_block(
            facts=[], summaries=summaries, status="hit", now_text="", budget_tokens=260,
        )
        self.assertLessEqual(config.estimate_tokens(block), 260)
        shown = [domain for domain in DOMAINS if "[" + domain + "]" in block]
        self.assertGreaterEqual(len(shown), 2, "预算很紧时也要让多个领域露脸")

    def test_facts_and_summaries_have_separate_budgets(self) -> None:
        summaries = [_summary(1, "health")]
        facts = [_fact(i) for i in range(1, 41)]
        block = build_memory_block(facts=facts, summaries=summaries, status="hit", now_text="")
        self.assertIn("health", block, "事实再多也不许把唯一一条摘要挤掉")

        fact_section = "\n".join(
            line for line in block.splitlines() if line.startswith("- 事实")
        )
        self.assertLessEqual(
            config.estimate_tokens(fact_section), config.FACT_BUDGET_TOKENS,
            "事实段最多吃 FACT_BUDGET_TOKENS,剩下的额度留给摘要",
        )


class RelevantFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.tmp / "archive")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_relevant_old_fact_comes_before_newer_unrelated_ones(self) -> None:
        old_id = self.store.add_fact("用户对装修预算是 6 万", fact_type="preference")
        for index in range(30):
            self.store.add_fact("杂项事实 " + str(index), fact_type="other")

        picked = self.store.facts_for_context("装修预算是多少", limit=20)
        self.assertEqual(picked[0].id, old_id, "相关事实必须排在纯按新旧补足的前面")
        self.assertIn("装修", picked[0].content)

    def test_context_facts_respect_the_token_budget(self) -> None:
        for index in range(30):
            self.store.add_fact(("事实" + str(index) + " " + "说明" * 55)[:120])
        picked = self.store.facts_for_context("随便问问", limit=20)
        used = sum(config.estimate_tokens(fact.content) + 8 for fact in picked)
        self.assertLessEqual(used, config.FACT_BUDGET_TOKENS)
        self.assertLess(len(picked), 20, "120 字一条的事实装不下 20 条")


if __name__ == "__main__":
    unittest.main()
