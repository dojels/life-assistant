"""自我观察 v0(D138):它看自己的那份账,必须**只报事实、分清灰白、带分母**。

用户 2026-10-16:*「先做行为吧」*。设计评审见 docs/决策记录.md D138。

⚠️ 这一组守三件事,每一件都对着一种会**误诊自己**的翻车方式:

  1. **"被挡住" ≠ "坏了"**:用户拒绝确认(``NEEDS_CONFIRMATION``)与真报错
     (``HANDLER_ERROR``)在事件里都是 ok=false。若不分开,``expense_confirm``
     会被报成"2 次 2 失败、成功率 0%",模型就可能得出"这个工具坏了"的错误结论;
  2. **每个计数都要带分母**:「失败 4 次」与「22 次里失败 4 次」是两件事;
  3. **不许自我暗示进记忆**:digest 末尾必须写死"这是事实、不是结论"。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_self_view -v
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from la import config
from la.agent.self_view import MAX_DIGEST_CHARS, build_behavior_digest
from la.tools.insight_tools import build_insight_tools
from la.tools.registry import READ_ONLY_TOOLS
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY

NOW = datetime(2026, 10, 16, 22, 0, 0)


class DigestCase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._orig = {
            name: getattr(config, name)
            for name in ("DATA_DIR", "ARCHIVE_DIR", "MEMORY_DB", "ASSISTANT_DB")
        }
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "archive").mkdir(parents=True, exist_ok=True)
        config.DATA_DIR = self.root
        config.ARCHIVE_DIR = self.root / "archive"
        config.MEMORY_DB = self.root / "memory.db"
        config.ASSISTANT_DB = self.root / "assistant.db"
        self.conn: sqlite3.Connection = db.connect(self.root / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        for key, value in self._orig.items():
            setattr(config, key, value)
        shutil.rmtree(self.root, ignore_errors=True)

    def add(self, kind: str, data: dict, *, day: str = "2026-10-16", hour: int = 10) -> None:
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events").fetchone()
        self.conn.execute(
            "INSERT INTO events(session_id, seq, type, time, surface, data)"
            " VALUES ('web', ?, ?, ?, 0, ?)",
            (int(row[0]), kind, "%sT%02d:00:00+08:00" % (day, hour),
             json.dumps(data, ensure_ascii=False)),
        )
        self.conn.commit()

    def tool(self, name: str, ok: bool, code: str = "", *, via: str = "model") -> None:
        payload = {"tool": name, "ok": ok, "error_code": code}
        if via:
            payload["via"] = via
        self.add("tool/result", payload)

    def digest(self, **kwargs) -> str:
        kwargs.setdefault("now", NOW)
        return build_behavior_digest(self.conn, **kwargs)

    # -- 一、灰白要分清 ---------------------------------------------------
    def test_waiting_for_confirmation_is_not_a_fault(self) -> None:
        """⭐ 用户拒绝确认 ≠ 工具坏了 —— 这条不成立,它就会误诊自己。"""
        self.tool("expense_confirm", False, "NEEDS_CONFIRMATION")
        self.tool("expense_confirm", False, "NEEDS_CONFIRMATION")
        self.tool("expense_query", True)
        text = self.digest()
        self.assertIn("等确认 2", text)
        self.assertIn("真故障 0", text)
        self.assertNotIn("expense_confirm 2/2", text)

    def test_policy_and_budget_codes_are_their_own_buckets(self) -> None:
        self.tool("expense_record", False, "READ_ONLY")
        self.tool("file", False, "DUPLICATE_CALL")
        self.tool("web", False, "TIMEOUT")
        text = self.digest()
        self.assertIn("被策略挡 1", text)
        self.assertIn("预算拒 1", text)
        self.assertIn("真故障 1", text)

    def test_bad_params_is_its_own_bucket_not_a_fault(self) -> None:
        """⭐ 参数写错 ≠ 工具坏了(2026-11-17:每天 22:00 日报误报 5 次真故障)。"""
        self.tool("expense_query", False, "BAD_PARAMS")
        self.tool("expense_query", True)
        text = self.digest()
        self.assertIn("参数错 1", text)
        self.assertIn("真故障 0", text)
        self.assertNotIn("expense_query 1/2", text)

    def test_unknown_code_counts_as_fault_not_as_success(self) -> None:
        """认不出来的码宁可当故障 —— 漏报会让它以为一切正常。"""
        self.tool("html_page", False, "SOMETHING_NEW")
        text = self.digest()
        self.assertIn("真故障 1", text)
        self.assertIn("html_page 1/1", text)

    # -- 二、分母 ---------------------------------------------------------
    def test_fault_list_carries_denominators(self) -> None:
        for _ in range(22):
            self.tool("expense_query", False, "HANDLER_ERROR")
        text = self.digest()
        self.assertIn("expense_query 22/22", text)

    def test_internal_calls_are_not_counted(self) -> None:
        self.tool("query_summary_pool", True, "", via="")
        text = self.digest()
        self.assertIn("共 0 次", text)

    # -- 三、覆盖期与约束 -------------------------------------------------
    def test_missing_token_account_is_stated(self) -> None:
        self.add("user/message", {"text": "hi"})
        text = self.digest()
        self.assertIn("轮次 1", text)
        self.assertIn("没有 token 账", text)

    def test_token_account_start_is_stated(self) -> None:
        self.add("llm/usage", {
            "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
            "cache_hit_tokens": 900, "cache_miss_tokens": 100,
        })
        text = self.digest()
        self.assertIn("命中 90.0%", text)

    def test_digest_fits_the_char_budget(self) -> None:
        for index in range(40):
            self.tool("tool_with_a_really_long_name_%02d" % index, True)
        catalog = ["tool_with_a_really_long_name_%02d" % i for i in range(40)]
        text = self.digest(catalog=catalog)
        self.assertLessEqual(len(text), MAX_DIGEST_CHARS)

    def test_digest_is_idempotent(self) -> None:
        """同一个 now 调两次必须字节相同 —— 否则它会以为自己变了。"""
        self.tool("web", True)
        self.add("user/message", {"text": "hi"})
        self.assertEqual(self.digest(), self.digest())

    def test_footer_forbids_writing_self_assessment_into_memory(self) -> None:
        text = self.digest()
        self.assertIn("不是结论", text)
        self.assertIn("不要把对自己的评价写进", text)

    # -- 四、轨迹里丢了消息也要说 -----------------------------------------
    def test_dropped_messages_from_trace_are_reported(self) -> None:
        text = self.digest(trace_records=[{"dropped_turns": 4, "capped": True}])
        self.assertIn("丢消息 4 条", text)
        self.assertIn("硬上限收紧 1 轮", text)


class ToolCase(unittest.TestCase):
    """工具层:注册、只读、参数收敛、返回形状。"""

    def test_tool_is_read_only_and_registered(self) -> None:
        self.assertIn("self_inspect", READ_ONLY_TOOLS)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        store = SimpleNamespace(conn=conn)
        specs = build_insight_tools(store)
        names = [spec.name for spec in specs]
        self.assertEqual(names, ["self_inspect"])
        # ⚠️ 2026-10-17 真机踩过:description 里多写一个逗号,括号里就成了**元组**,
        #    JSON 序列化出来是数组,API 直接 422(invalid type: sequence, expected a string)。
        #    所以这里必须**钉住类型**,不能只断言长度(元组也有长度)。
        self.assertIsInstance(specs[0].description, str, "description 必须是字符串,不能是序列")
        self.assertLessEqual(len(specs[0].description), 400, "工具描述会进每一轮 tools 字段")
        # ⚠️ 只读是**注册时**按白名单归一化的(registry.register 会把 writes 改成 False),
        #    所以断言要看注册表里的那一份,而不是原始 spec —— 与 G18 的实现一致。
        from la.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register_all(specs)
        registered = registry.get("self_inspect")
        self.assertIsNotNone(registered)
        self.assertFalse(registered.writes, "self_inspect 必须是只读工具")

    def test_handler_returns_digest_and_rejects_unknown_scope(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE events (session_id TEXT, seq INTEGER, type TEXT, time TEXT,"
            " surface INTEGER, data TEXT)"
        )
        conn.execute(
            "INSERT INTO events VALUES ('web', 1, 'user/message',"
            " '2026-10-16T10:00:00+08:00', 0, '{\"text\": \"hi\"}')"
        )
        conn.commit()
        store = SimpleNamespace(conn=conn)
        spec = build_insight_tools(
            store, catalog=lambda: ("self_inspect",), now=lambda: NOW,
        )[0]
        payload = asyncio.run(spec.handler({"scope": "behavior", "days": 7}))
        self.assertEqual(payload["scope"], "behavior")
        self.assertEqual(payload["chars"], len(payload["digest"]))
        self.assertIn("轮次 1", payload["digest"])
        bad = asyncio.run(spec.handler({"scope": "structure"}))
        self.assertIn("error", bad)
        self.assertEqual(bad["supported"], ["behavior"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
