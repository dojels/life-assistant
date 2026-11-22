"""领域标签与热度分母的修复回归(2026-11-07)。

覆盖三件事:
  1. `pool_max_heat` 按会话取 —— A 成员的高频主题不能稀释 B 成员的 heat_norm;
  2. tags 由工具/领域事件真实生成、可过滤(含中文别名),回填保留已有标签;
  3. "健康/账目/错题"共用一个 top-k 时,每个有命中的领域至少留一条。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from la import config
from la.memory import topics
from la.memory.consolidate import consolidate
from la.memory.retrieval import retrieve
from la.memory.scratchpad import Evidence
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY, _memory_backfill_summary_tags


def _event(etype: str, **data):
    return SimpleNamespace(type=etype, data=data)


class TagDerivationTest(unittest.TestCase):
    def test_tool_and_domain_events_become_tags(self) -> None:
        tags = topics.tags_from_events([
            _event("user/message", text="今天血压多少"),
            _event("tool/result", tool="health_log", ok=True),
            _event("ledger.txn.created", amount_cents=100),
            _event("study.review.recorded", result="wrong"),
            _event("calendar.event.created", uid="x"),
            _event("todo/change", action="add"),
            _event("memory/note", content="记住:不吃香菜"),
        ])
        self.assertEqual(tags, ["ledger", "calendar", "health", "study", "task", "memory"])

    def test_internal_retrieval_tools_are_not_topic_evidence(self) -> None:
        tags = topics.tags_from_events([
            _event("tool/result", tool="query_summary_pool", ok=True),
            _event("tool/result", tool="query_timestamp_range", ok=True),
            _event("tool/result", tool="spawn_sub_agents", ok=True),
        ])
        self.assertEqual(tags, [], "框架每轮都调的检索工具不能把每个桶都标成 memory")

    def test_failed_write_is_not_evidence_but_failed_read_is(self) -> None:
        failed_write = topics.tags_from_events([
            _event("tool/result", tool="health_log", ok=False),
        ])
        self.assertEqual(failed_write, [])
        failed_read = topics.tags_from_events([
            _event("tool/result", tool="health_plan", ok=False),
        ])
        self.assertEqual(failed_read, ["health"], "只读工具问了就算这一桶聊过健康")

    def test_chinese_aliases_and_merge(self) -> None:
        self.assertEqual(topics.normalize_tag("健康"), "health")
        self.assertEqual(topics.normalize_tag(" Health "), "health")
        self.assertEqual(
            topics.merge_tags(["consolidated"], ["health"]),
            ["health", "consolidated"],
            "领域标签按固定顺序在前,内部标记留在后面",
        )


class StoreFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.tmp / "archive")
        self.now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=config.get_settings().tz)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def add_summary(
        self, session_id: str, seq: int, text: str, *, tags: list[str] | None = None,
        visits: int = 0,
    ) -> int:
        return self.store.upsert_summary(
            session_id=session_id,
            start_seq=seq,
            end_seq=seq,
            start_time="2026-11-01T10:00:00+08:00",
            end_time="2026-11-01T10:05:00+08:00",
            text=text,
            tags=tags or [],
        ) if not visits else self._add_with_visits(session_id, seq, text, tags or [], visits)

    def _add_with_visits(self, session_id: str, seq: int, text: str, tags: list[str], visits: int) -> int:
        summary_id = self.store.upsert_summary(
            session_id=session_id, start_seq=seq, end_seq=seq,
            start_time="2026-11-01T10:00:00+08:00",
            end_time="2026-11-01T10:05:00+08:00",
            text=text, tags=tags,
        )
        self.conn.execute(
            "UPDATE summary_pool SET visits = ?, last_access = '2026-11-07T00:00:00+08:00'"
            " WHERE id = ?",
            (visits, summary_id),
        )
        self.conn.commit()
        return summary_id


class HeatDenominatorTest(StoreFixture):
    def test_member_a_hot_topic_does_not_dilute_member_b(self) -> None:
        self.add_summary("member:1", 1, "A 的装修预算与建材清单", visits=400)
        self.add_summary("member:2", 1, "B 的健康:每天走 8000 步", tags=["health"], visits=1)
        self.store.refresh_heat_cache(self.now, session_id="member:1")
        self.store.refresh_heat_cache(self.now, session_id="member:2")

        self.assertGreater(
            self.store.pool_max_heat("member:1"),
            self.store.pool_max_heat("member:2"),
            "按会话取时,A 的最大热度不该算进 B 的分母",
        )
        result = retrieve(self.store, "健康 步数", session_id="member:2", now=self.now)
        self.assertTrue(result.summaries)
        detail = result.details[result.summaries[0].id]
        self.assertAlmostEqual(
            detail["heat_norm"], 1.0, places=4,
            msg="B 会话里最热的桶应当顶到 1.0;被 A 抬高分母时会远小于 1",
        )

    def test_global_denominator_still_available_for_diagnostics(self) -> None:
        self.add_summary("member:1", 1, "A 的装修", visits=400)
        self.add_summary("member:2", 1, "B 的健康", visits=1)
        self.store.refresh_heat_cache(self.now)
        self.assertGreaterEqual(
            self.store.pool_max_heat(), self.store.pool_max_heat("member:2")
        )


class TagFilterTest(StoreFixture):
    def test_only_tagged_rows_match_and_chinese_alias_works(self) -> None:
        health = self.add_summary("s1", 1, "健康:血压 130/85", tags=["health"])
        ledger = self.add_summary("s1", 2, "账目:餐饮 2350 元", tags=["ledger"])
        self.store.refresh_heat_cache(self.now, session_id="s1")

        got = retrieve(self.store, "健康", session_id="s1", now=self.now, tags=["健康"])
        self.assertEqual([row.id for row in got.summaries], [health])
        got = retrieve(self.store, "账目", session_id="s1", now=self.now, tags=["ledger"])
        self.assertEqual([row.id for row in got.summaries], [ledger])
        got = retrieve(self.store, "健康", session_id="s1", now=self.now, tags=["ledger"])
        self.assertEqual(got.summaries, [], "标签不符就是空,不再退化成不过滤")

    def test_untagged_legacy_row_does_not_match_a_tag_filter(self) -> None:
        self.add_summary("s1", 1, "健康:血压 130/85")
        self.store.refresh_heat_cache(self.now, session_id="s1")
        got = retrieve(self.store, "健康", session_id="s1", now=self.now, tags=["health"])
        self.assertEqual(got.summaries, [])


class CoverageTest(StoreFixture):
    def test_each_matching_domain_keeps_at_least_one_slot(self) -> None:
        for seq in range(1, 9):
            self.add_summary("s1", seq, "记录 健康 血压 第%d条" % seq, tags=["health"], visits=100 - seq)
        self.add_summary("s1", 20, "记录 账目 外卖", tags=["ledger"], visits=1)
        self.add_summary("s1", 21, "记录 错题 数学", tags=["study"], visits=1)
        self.store.refresh_heat_cache(self.now, session_id="s1")

        result = retrieve(self.store, "记录", session_id="s1", now=self.now, limit=5)
        domains = {topics.primary_domain(row.tags) for row in result.summaries}
        self.assertEqual(
            domains, {"health", "ledger", "study"},
            "高分领域不能把 5 条全占满 —— 每个有命中的领域至少一条",
        )

    def test_single_domain_is_not_diluted(self) -> None:
        for seq in range(1, 6):
            self.add_summary("s1", seq, "记录 健康 第%d条" % seq, tags=["health"], visits=10)
        self.store.refresh_heat_cache(self.now, session_id="s1")
        result = retrieve(self.store, "记录", session_id="s1", now=self.now, limit=5)
        self.assertEqual(len(result.summaries), 5)
        self.assertTrue(all(topics.primary_domain(row.tags) == "health" for row in result.summaries))


class ConsolidateTagTest(StoreFixture):
    def test_backfill_keeps_the_buckets_domain_tags(self) -> None:
        summary_id = self.add_summary("s1", 1, "健康:血压 130/85", tags=["health"])
        outcome = asyncio.run(consolidate(
            self.store,
            session_id="s1",
            start_seq=1,
            end_seq=1,
            rounds_after_reply=1,
            evidence=[Evidence(
                source="timestamp_range", timestamp="2026-11-01T10:00:00+08:00",
                content="那天血压是 130/85", seq=1, session_id="s1",
            )],
            start_time="2026-11-01T10:00:00+08:00",
            end_time="2026-11-01T10:05:00+08:00",
        ))
        self.assertTrue(outcome.applied, outcome.reason)
        row = self.store.summaries([summary_id])[summary_id]
        self.assertIn("health", row.tags, "回填不能把领域标签冲掉")
        self.assertIn("consolidated", row.tags, "内部标记仍要保留")


class BackfillMigrationTest(StoreFixture):
    def test_old_empty_tags_are_backfilled_from_events(self) -> None:
        self.store.append("s1", "user/message", {"text": "记一下血压"})
        self.store.append("s1", "health.measurement.logged", {"metric": "bp"})
        summary_id = self.store.upsert_summary(
            session_id="s1", start_seq=1, end_seq=2,
            start_time="2026-11-01T10:00:00+08:00",
            end_time="2026-11-01T10:05:00+08:00",
            text="健康:血压 130/85",
        )

        _memory_backfill_summary_tags(self.conn)

        row = self.store.summaries([summary_id])[summary_id]
        self.assertEqual(row.tags, ["health"], "历史桶要按同一个 tags_from_rows 口径补标")

    def test_non_empty_tags_are_left_alone(self) -> None:
        summary_id = self.add_summary("s1", 1, "账目:餐饮", tags=["ledger"])
        self.store.append("s1", "health.measurement.logged", {"metric": "bp"})
        _memory_backfill_summary_tags(self.conn)
        row = self.store.summaries([summary_id])[summary_id]
        self.assertEqual(row.tags, ["ledger"])




class _StubSummarizer:
    async def summarize(self, events, *, start_time, end_time):
        from la.memory.summarize import SummaryDraft
        return SummaryDraft(
            text="<compacted-summary>\n- 事项:记血压\n</compacted-summary>",
            anchors=[],
            source="extractive",
        )


class CompactionWritesTagsTest(StoreFixture):
    def test_compact_once_stores_domain_tags_on_the_bucket(self) -> None:
        import dataclasses
        from la.agent.session import MemorySession
        from la.llm.fake import FakeClient

        settings = dataclasses.replace(
            config.get_settings(reload=True), provider="fake", session_id="s1"
        )
        session = MemorySession(FakeClient(), settings, store=self.store, session_id="s1")
        self.store.append("s1", "user/message", {"text": "记一下血压"})
        self.store.append("s1", "tool/result", {"tool": "health_log", "ok": True})
        self.store.append("s1", "health.measurement.logged", {"metric": "bp"})
        session.summarizer = _StubSummarizer()
        session.pick_compaction_range = lambda: (1, 3)

        summary_id = asyncio.run(session.compact_once())

        self.assertIsNotNone(summary_id)
        row = self.store.summaries([summary_id])[summary_id]
        self.assertEqual(row.tags, ["health"], "压缩必须把工具/领域事件推成标签")
        events = [e for e in self.store.events("s1") if e.type == "compaction/summary"]
        self.assertEqual(events[-1].data["tags"], ["health"], "审计事件里也要能看见标签")


if __name__ == "__main__":
    unittest.main()
