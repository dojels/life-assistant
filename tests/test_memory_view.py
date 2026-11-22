"""记忆页测试:每一层都要能被看见,而且看得对。

这一组测试的重点不是"页面能打开",而是**数字和内容对不对** ——
一个显示错误记忆内容的页面比没有页面更糟糕:它会让人以为系统记对了。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from la import config
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY
from la.web.memory_view import build_memory_view


class MemoryViewCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.archive = self.root / "archive"
        self.archive.mkdir(parents=True, exist_ok=True)
        self.conn: sqlite3.Connection = db.connect(self.root / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def view(self):
        return build_memory_view(
            self.conn, now_text="2026-10-08 22:00", archive_dir=self.archive
        )

    def layer(self, code: str):
        for item in self.view().layers:
            if item.code == code:
                return item
        raise AssertionError("没有这一层: " + code)

    def write_archive(self, name: str, events: list[dict]) -> Path:
        path = self.archive / name
        with path.open("w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + chr(10))
        return path

    def add_event(self, session: str, seq: int, kind: str, data: dict, *,
                  surface: int = 1, time: str = "2026-10-08T10:00:00+08:00") -> None:
        self.conn.execute(
            "INSERT INTO events(session_id, seq, type, time, surface, data)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session, seq, kind, time, surface, json.dumps(data, ensure_ascii=False)),
        )
        self.conn.commit()

    def add_fact(self, content: str, *, kind: str = "preference", active: int = 1,
                 superseded_by: int | None = None, confidential: int = 0) -> int:
        cursor = self.conn.execute(
            "INSERT INTO facts(session_id, type, category, content, keywords, confidential,"
            " active, superseded_by, created_at, updated_at)"
            " VALUES ('default', ?, 'permanent', ?, '[]', ?, ?, ?, '2026-10-08T10:00:00Z',"
            " '2026-10-08T10:00:00Z')",
            (kind, content, confidential, active, superseded_by),
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    # -- 层次齐全 -------------------------------------------------------
    def test_all_layers_are_present_in_data_flow_order(self) -> None:
        codes = [item.code for item in self.view().layers]
        # L3c = 事实候选(2026-10-13 加的准入闸)。它排在 L3 与 L4 之间:
        # 候选是"还没进事实层"的东西,天然住在 L3 的入口上。
        self.assertEqual(codes, ["L0", "L1", "L1b", "L2", "L3", "L3c", "L4", "L5"])

    def test_rebuildable_flags_match_reality(self) -> None:
        """⭐ 分层的意义就在这个标记上 —— 而**标记必须与事实一致**(2026-10-12 审核):

        * L0 归档:删了就真没了;
        * L2 摘要池:归档里的 compaction/summary 事件只有桶与 id,没有摘要正文 -> 重放不出来;
        * L3 事实:事件里只有 content,类别/敏感标记/失效状态会丢 -> 只能近似重建。
        所以这三层都标"不可重建",而不是让页面承诺一个做不到的能力。
        """
        layers = self.view().layers
        not_rebuildable = [item.code for item in layers if not item.rebuildable]
        # L3c 候选同样不可重建:事件里只有裁决轨迹,没有候选正文的完整快照。
        self.assertEqual(not_rebuildable, ["L0", "L2", "L3", "L3c"])
        for code in ("L2", "L3"):
            note = next(item.note for item in layers if item.code == code)
            self.assertIn("重", note, code + " 的说明要讲清为什么不可重建")

    def test_every_layer_explains_itself(self) -> None:
        for item in self.view().layers:
            self.assertGreater(len(item.note), 10, item.code + " 没有说明")
            self.assertTrue(item.source, item.code + " 没写位置")

    # -- L0 归档 ---------------------------------------------------------
    def test_archive_layer_counts_events_and_reports_last_seq(self) -> None:
        self.write_archive("default.jsonl", [
            {"seq": 1, "type": "user/message", "time": "2026-10-08T10:00:00+08:00", "data": {}},
            {"seq": 2, "type": "assistant/message", "time": "2026-10-08T10:00:05+08:00", "data": {}},
        ])
        layer = self.layer("L0")
        self.assertEqual(layer.rows[0][0], "default.jsonl")
        self.assertEqual(layer.rows[0][1], "2")      # 事件数
        self.assertEqual(layer.rows[0][2], "2")      # 最大序号
        self.assertIn("事件总数", [s[0] for s in layer.stats])

    def test_archive_layer_skips_corrupt_lines_instead_of_crashing(self) -> None:
        path = self.archive / "broken.jsonl"
        path.write_text(
            '{"seq": 1, "type": "user/message", "time": "t", "data": {}}' + chr(10)
            + "这不是 json" + chr(10)
            + '{"seq": 2, "type": "user/message", "time": "t", "data": {}}' + chr(10),
            encoding="utf-8",
        )
        layer = self.layer("L0")
        self.assertEqual(layer.rows[0][1], "2")  # 坏行不计入,其余照常

    def test_empty_archive_says_how_to_fill_it(self) -> None:
        layer = self.layer("L0")
        self.assertTrue(layer.empty)
        self.assertIn("--once", layer.empty_hint)

    # -- L1 事件镜像 -----------------------------------------------------
    def test_event_layer_splits_surfaced_from_hidden(self) -> None:
        self.add_event("default", 1, "user/message", {"c": "a"})
        self.add_event("default", 2, "tool/result", {"c": "b"}, surface=0)
        layer = self.layer("L1")
        self.assertEqual(layer.rows[0][1], "2")   # 总数
        self.assertEqual(layer.rows[0][2], "1")   # 进上下文
        self.assertEqual(layer.rows[0][3], "1")   # 不进上下文

    def test_event_type_layer_groups_by_type(self) -> None:
        self.add_event("default", 1, "user/message", {"c": "a"})
        self.add_event("default", 2, "user/message", {"c": "b"})
        self.add_event("default", 3, "ledger.txn.created", {"c": "c"})
        rows = {row[0]: row[1] for row in self.layer("L1b").rows}
        self.assertEqual(rows["user/message"], "2")
        self.assertEqual(rows["ledger.txn.created"], "1")

    # -- L3 永久事实 -----------------------------------------------------
    def test_fact_layer_marks_superseded_ones(self) -> None:
        """⭐ 被取代的事实不能消失 —— 否则没法追查"它当时为什么这么答"。"""
        old_id = self.add_fact("用户喝美式", active=0)
        new_id = self.add_fact("用户喝拿铁")
        self.conn.execute("UPDATE facts SET superseded_by = ? WHERE id = ?", (new_id, old_id))
        self.conn.commit()
        rows = {row[0]: row for row in self.layer("L3").rows}
        self.assertEqual(rows["#" + str(old_id)][2], "已被 #" + str(new_id) + " 取代")
        self.assertEqual(rows["#" + str(new_id)][2], "生效中")

    def test_fact_layer_counts_active_and_confidential(self) -> None:
        self.add_fact("生效的")
        self.add_fact("失效的", active=0)
        self.add_fact("敏感的身份信息", kind="identity", confidential=1)
        stats = dict(self.layer("L3").stats)
        self.assertEqual(stats["事实总数"], "3")
        self.assertEqual(stats["生效中"], "2")
        self.assertEqual(stats["已取代/失效"], "1")
        self.assertEqual(stats["标记为敏感"], "1")

    def test_fact_layer_content_is_actually_shown(self) -> None:
        self.add_fact("用户的英文名是 Kevin")
        cells = [cell for row in self.layer("L3").rows for cell in row]
        self.assertTrue(any("Kevin" in cell for cell in cells))

    # -- L4 热摘要块 -----------------------------------------------------
    def test_hot_block_is_a_real_memory_block_containing_active_facts(self) -> None:
        self.add_fact("用户的英文名是 Kevin")
        layer = self.layer("L4")
        self.assertIn("<memory>", layer.raw)
        self.assertIn("</memory>", layer.raw)
        self.assertIn("<facts>", layer.raw)
        self.assertIn("Kevin", layer.raw)
        self.assertEqual(dict(layer.stats)["状态"], "命中")

    def test_hot_block_excludes_superseded_facts(self) -> None:
        """⭐ 已取代的事实绝不能进上下文 —— 否则旧答案会和新答案打架。"""
        old_id = self.add_fact("用户喝美式", active=0)
        new_id = self.add_fact("用户喝拿铁")
        self.conn.execute("UPDATE facts SET superseded_by = ? WHERE id = ?", (new_id, old_id))
        self.conn.commit()
        raw = self.layer("L4").raw
        self.assertIn("拿铁", raw)
        self.assertNotIn("美式", raw)

    def test_hot_block_says_miss_when_there_is_nothing(self) -> None:
        raw = self.layer("L4").raw
        self.assertIn("miss", raw)
        self.assertIn("不要用听起来合理的猜测填空", raw)

    def test_hot_block_stays_within_budget(self) -> None:
        for index in range(60):
            self.add_fact("第 " + str(index) + " 条事实," + "内容" * 40)
        layer = self.layer("L4")
        self.assertLessEqual(
            config.estimate_tokens(layer.raw), config.BUDGET_HOT_SUMMARY,
            "热摘要块超出预算了 —— 它会挤掉对话段",
        )

    # -- L5 投影预算 -----------------------------------------------------
    def test_projection_layer_lists_all_five_segments(self) -> None:
        names = [row[0] for row in self.layer("L5").rows]
        for segment in ("system", "checkpoint", "runtime", "memory",
                        "evidence", "conversation"):
            self.assertIn(segment, names)
        # ⚠️ 2026-10-17:总锁 / 单轮上限 / 硬上限**不再搬到这一页** ——
        #    用户原话:"我只要控什么时候压缩就行了……没有作用就删除,不要污染页面"。
        self.assertNotIn("总锁(量级参照)", names)
        self.assertNotIn("硬上限", names)
        self.assertNotIn("单轮真实上限", names)

    def test_projection_budget_matches_config(self) -> None:
        rows = {row[0]: row[1] for row in self.layer("L5").rows}
        self.assertEqual(rows["memory"], str(config.BUDGET_HOT_SUMMARY))
        self.assertEqual(rows["conversation"], str(config.BUDGET_CONVERSATION))
        self.assertEqual(rows["tools(工具 schema)"], str(config.BUDGET_TOOLS))

    def test_projection_layer_says_when_compaction_happens(self) -> None:
        """⭐ 这一页只控一件事:什么时候压。触发线与保留比例必须在最显眼的那一句里。"""
        layer = self.layer("L5")
        # 页面上写成千分位(120,000),更好扫读
        self.assertIn(format(config.BUDGET_COMPACT_TRIGGER, ","), layer.headline)
        self.assertIn(
            str(int(config.BUDGET_COMPACT_RETAIN_RATIO * 100)) + "%", layer.headline,
        )

    # -- 只读与总览 ------------------------------------------------------
    def test_view_does_not_write_anything(self) -> None:
        """⭐ 看一眼不该改变任何东西。"""
        self.add_event("default", 1, "user/message", {"c": "a"})
        self.add_fact("用户的英文名是 Kevin")

        def snapshot() -> list[tuple[str, int]]:
            tables = [
                row[0] for row in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                    " AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            return [(name, int(self.conn.execute(
                "SELECT count(*) FROM " + name).fetchone()[0])) for name in sorted(tables)]

        before = snapshot()
        self.view()
        self.view()
        self.assertEqual(snapshot(), before)

    def test_totals_match_the_tables(self) -> None:
        self.add_event("default", 1, "user/message", {"c": "a"})
        self.add_event("default", 2, "user/message", {"c": "b"})
        self.add_fact("生效的")
        self.add_fact("失效的", active=0)
        view = self.view()
        self.assertEqual(view.total_events, 2)
        self.assertEqual(view.total_active_facts, 1)
        self.assertEqual(view.total_summaries, 0)

    def test_empty_database_renders_without_crashing(self) -> None:
        view = self.view()
        self.assertEqual(len(view.layers), 8)  # L3c 候选层(2026-10-13)
        self.assertEqual(view.total_events, 0)


if __name__ == "__main__":
    unittest.main()