"""M0 验收测试:骨架、三张基础设施表、运行时上下文注入。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from la import config
from la.agent.loop import AgentLoop
from la.llm.fake import FakeClient
from la.storage import db

BASE_DIR = Path(__file__).resolve().parent.parent
INFRA_TABLES = ("ops_outbox", "ops_audit", "sys_idempotency", "ops_failure_samples")


def offline_env(data_dir: str) -> dict[str, str]:
    """离线且**数据目录隔离** —— 测试绝不能往日常使用的 data/ 里灌数据。"""
    env = dict(os.environ)
    env["LA_LLM_PROVIDER"] = "fake"
    env["LA_DATA_DIR"] = data_dir
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class ConfigTestCase(unittest.TestCase):
    def test_token_estimator_matches_documented_formula(self):
        # ceil(len/4) + 4,照抄 DSH token-meter 的量级
        self.assertEqual(config.estimate_tokens(""), 4)
        self.assertEqual(config.estimate_tokens("abcd"), 5)
        self.assertEqual(config.estimate_tokens("a" * 100), 29)

    def test_runtime_dirs_exist(self):
        config.ensure_runtime_dirs()
        for directory in config.RUNTIME_DIRS:
            self.assertTrue(directory.exists(), "缺少目录: " + str(directory))

    def test_budget_constants_are_consistent(self):
        """⭐ D121:三条线必须**自洽**,否则"总锁/硬上限"就只是句口号。

        旧的一组数(对话 4000 / 总锁 8500 / 硬上限 10000)是反的:五段上限之和 10700
        既越过总锁、也越过硬上限 —— 于是硬上限在正常装配下必然被撑破,对话段每一轮都被
        收一刀,模型能直接看到的原文只剩二十几条消息。重定标的经过见 docs/历史窗口重定标.md。
        """
        segments = (
            config.BUDGET_SYSTEM
            + config.BUDGET_CHECKPOINT
            + config.BUDGET_RUNTIME
            + config.BUDGET_HOT_SUMMARY
            + config.BUDGET_CONVERSATION
            + config.BUDGET_EVIDENCE
        )
        # ⚠️ 2026-10-16(D130):BUDGET_TOOLS 从 2500 的旧估算改成**实测值**之后,
        #    硬上限跟着收紧。五段是**理论上限**,实际固定四段只用几百 token;
        #    真会紧的只有对话段,而它用的是动态预算 min(它, 硬上限 − 其余固定段)。
        # ⚠️ 2026-10-17:总锁常量已删除(它不参与任何判定,只会在页面上被误读成"到它就压"),
        #    所以这里只钉真正有意义的那一格:
        self.assertLess(segments, config.BUDGET_HARD_MAX, "五段之和必须装得进硬上限")
        self.assertLess(
            config.BUDGET_HARD_MAX, config.CONTEXT_WINDOW_TOKENS,
            "硬上限远低于模型窗口 —— 定它的是钱和延迟,不是装不下",
        )
        # ⭐ D122:单轮上限是**从窗口推出来的**,而且三块加起来正好是那 30% ——
        #    别让人手写一个数把这条推导链打断(那正是"4000"当初怎么来的)。
        self.assertEqual(
            config.BUDGET_REQUEST_REAL,
            int(config.CONTEXT_WINDOW_TOKENS * config.SINGLE_TURN_INPUT_SHARE),
        )
        self.assertEqual(
            config.BUDGET_HARD_MAX + config.BUDGET_TOOLS + config.BUDGET_TURN_WORKING,
            config.BUDGET_REQUEST,
            "投影 + 工具 schema + 轮内留量 = 单轮上限(估算口径)",
        )
        self.assertEqual(
            int(config.BUDGET_REQUEST * config.TOKEN_ESTIMATE_RATIO),
            config.BUDGET_REQUEST_REAL,
            "估算口径与真实口径之间只差一个实测比值",
        )
        # ⭐ 对话段是"模型能直接看到多少历史"的那一段。4000 那种量级(实测只装下二十几条
        #    消息 / 2.9 天)在 1M 窗口下没有道理 —— 这条不许悄悄退回去。
        self.assertGreaterEqual(config.BUDGET_CONVERSATION, 16000)

    def test_tool_budget_stays_close_to_the_real_catalog(self):
        """⭐ D130:工具预留额必须**贴着实测值**,不是设计稿里那个 2500。

        用户 2026-10-15 的截图就是反例:轨迹页写着「工具 schema 7841 · 预算量级 2500」,
        当场判它"超预算" —— 可 tools 字段是一份定长 JSON,根本没有可截的地方。
        更要紧的是这个数**参与算硬上限**:留 2500 等于把工具那 7,841 里的 5,341 漏在账外。

        所以这条测试读**真实工具清单**来比(不是抄一个数),并留一条纪律:
        谁加工具/写长说明把 catalog 撑过预留额,这条就红 —— 那时该改的是 BUDGET_TOOLS
        (跟着涨),而不是当作没看见。
        """
        import json

        from la.agent.session import MemorySession
        from la.llm.tools import build_tool_catalog

        holder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp = Path(holder.name)
        previous = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
                        "ASSISTANT_DB", "MEMORY_DB")
        }
        previous_provider = os.environ.get("LA_LLM_PROVIDER")
        try:
            config.DATA_DIR = tmp
            config.ARCHIVE_DIR = tmp / "archive"
            config.MEDIA_DIR = tmp / "media"
            config.ARTIFACTS_DIR = tmp / "artifacts"
            config.ASSISTANT_DB = tmp / "assistant.db"
            config.MEMORY_DB = tmp / "memory.db"
            config._settings = None
            for name in ("archive", "media", "artifacts"):
                (tmp / name).mkdir(exist_ok=True)
            os.environ["LA_LLM_PROVIDER"] = "fake"
            session = MemorySession(
                FakeClient(), config.get_settings(), session_id="tool-budget-probe",
                assistant_conn=db.connect_assistant(),
            )
            catalog = build_tool_catalog(session.registry.specs())
            estimate = config.estimate_tokens(
                json.dumps(catalog, ensure_ascii=False)
            )
            self.assertGreaterEqual(
                config.BUDGET_TOOLS, estimate,
                "工具预留额比真实清单还小 —— 加工具/写长说明之后要把它一起调上去"
                "(实测估 " + str(estimate) + " token)",
            )
            self.assertLessEqual(
                config.BUDGET_TOOLS, int(estimate * 1.5),
                "预留额比真实清单大太多 —— 那是凭空从投影那边拿走预算"
                "(实测估 " + str(estimate) + " token)",
            )
            # ⭐ D136(用户 2026-10-16:*"工具要求全列,我可以通过成功率进行工具的优化"*):
            #    使用报表的工具表是**注册表全集**。所以这里顺手把两件事钉住 ——
            #    加了工具忘记让报表看得见(比如没把名单传进去),这条就会红。
            from la.web.report_view import build_usage_report

            names = list(session.registry.names())
            report = build_usage_report(
                session.store.conn, period="day", catalog=tuple(names)
            )
            self.assertEqual(report.catalog_size, len(names), "注册表个数要如实报出来")
            # ⚠️ 工具表 = 注册表 **减去**"框架自己发起"的那几个(query_summary_pool 等):
            #    它们不是模型选的,混进去会永远显示「未调用」而其实一直在跑。
            from la.agent.session import INTERNAL_TOOLS

            self.assertEqual(
                [item.name for item in report.tools],
                sorted(name for name in names if name not in INTERNAL_TOOLS),
                "工具表必须是注册表全集(含一次都没调过的),但不含框架内部工具",
            )
            self.assertEqual(
                report.internal_catalog, sorted(INTERNAL_TOOLS),
                "框架内那 6 个要在页面上单独列出来(它们照样占 schema 的钱)",
            )
            from la.web.report_view import INTERNAL_TOOL_NAMES

            self.assertEqual(
                set(INTERNAL_TOOLS), set(INTERNAL_TOOL_NAMES),
                "两份名单必须一致 —— 只在 report_view 里另抄一份,迟早会漂",
            )
            self.assertFalse(
                report.tools_retired, "空库里不该有已下线的旧名"
            )
        finally:
            try:
                session.assistant_conn.close()
            except Exception:  # noqa: BLE001
                pass
            for key, value in previous.items():
                setattr(config, key, value)
            if previous_provider is None:
                os.environ.pop("LA_LLM_PROVIDER", None)
            else:
                os.environ["LA_LLM_PROVIDER"] = previous_provider
            config._settings = None
            holder.cleanup()

    def test_projection_never_exceeds_the_hard_cap(self):
        """⭐ D133:硬上限是 BUDGET_HARD_MAX(已扣掉工具预留)—— 投影本身不许越过它。

        BUDGET_HARD_MAX = 单轮上限 − 工具预留 − 轮内留量,描述的是**整个投影**能用多少。
        装配时若只拿 BUDGET_CONVERSATION 比,投影能长到 128,000,再加工具那 7,841,
        实际单轮输入就撑破了它 1/3(而且账面看不出来,因为工具不在这本账里)。
        """
        from la.memory.projector import Projector

        long_history: list[dict[str, str]] = []
        for index in range(700):   # 700 对 ≈ 23 万 token,稳稳超过对话段预算
            long_history.append({"role": "user", "content": "问 " + str(index) + " " + "字" * 500})
            long_history.append({"role": "assistant", "content": "答 " + str(index) + " " + "字" * 500})
        projection = Projector(config.get_settings()).build(
            system_text="身份", runtime_text="运行时", memory_text="<memory></memory>",
            history=long_history, evidence_text="", tools_tokens=config.BUDGET_TOOLS,
        )
        self.assertLessEqual(
            projection.total_tokens, config.BUDGET_HARD_MAX,
            "投影总量越过硬上限 —— 硬上限成了一句口号",
        )
        self.assertGreater(projection.dropped_turns, 0, "前提:历史确实被砍过")
        # ⭐ 再验"收紧必须如实报":把硬上限临时压到 6 万,对话段预算必须跟着降。
        #    (常态下这个上限够大,收不到它 —— 所以机制要单独验,别指望日常能碰上。)
        from la.memory.projector import SEGMENT_CONVERSATION

        original = config.BUDGET_HARD_MAX
        config.BUDGET_HARD_MAX = config.BUDGET_TOOLS + 60_000
        try:
            tight = Projector(config.get_settings()).build(
                system_text="身份", runtime_text="运行时", memory_text="<memory></memory>",
                history=long_history, evidence_text="", tools_tokens=config.BUDGET_TOOLS,
            )
        finally:
            config.BUDGET_HARD_MAX = original
        self.assertTrue(tight.capped, "压小了硬上限还不算被收,这条机制就是假的")
        self.assertLess(tight.conversation_budget, config.BUDGET_CONVERSATION)
        # ⚠️ 固定段是按**裁剪后的实际大小**算的,不是按它们的预算上限 —— 所以这里反推固定段,
        #    再验那条公式;写死 2,500+200+1,500+2,500 会假红(实测过)。
        fixed_actual = (
            config.BUDGET_HARD_MAX - config.BUDGET_TOOLS - tight.conversation_budget
        )
        self.assertGreater(fixed_actual, 0)
        self.assertLessEqual(
            fixed_actual + config.BUDGET_TOOLS + tight.conversation_budget,
            config.BUDGET_HARD_MAX,
        )
        self.assertLessEqual(
            tight.total_tokens, config.BUDGET_HARD_MAX,
            "对话段预算 = 硬上限 − 工具预留 − 固定四段,所以投影总量必须呆在硬上限之内",
        )

    def test_compaction_trigger_is_not_crossed_by_a_short_history(self):
        """⭐ 触发线要落在"历史快攒满"的位置,而不是被固定段撑破。

        旧数字下把对话段稍微调大一点,固定四段 + 对话段就必然越过 5950 —— 结果是**每一轮**
        都压缩一次(每轮多一次模型调用、多一个摘要桶)。这条钉住"短对话不越线"。
        """
        short_projection = (
            config.BUDGET_SYSTEM + config.BUDGET_RUNTIME + config.BUDGET_HOT_SUMMARY
            + config.BUDGET_EVIDENCE + 1000
        )
        self.assertLess(
            short_projection,
            config.BUDGET_COMPACT_TRIGGER,
            "固定段 + 很短的历史就压线 = 每轮都会触发压缩",
        )


class SchemaTestCase(unittest.TestCase):
    """基础设施表的形状与约束 —— 三张表是四个领域共用的地基。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "assistant.db")
        db.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _insert_outbox(self) -> None:
        self.conn.execute(
            "INSERT INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("ledger.txn.created", '{"cents": 3800}', "k1", "2026-10-08T10:00:00+08:00"),
        )
        self.conn.commit()

    def test_pragmas_are_applied(self):
        snapshot = db.pragma_snapshot(self.conn)
        self.assertEqual(str(snapshot["journal_mode"]).lower(), "wal")
        self.assertEqual(snapshot["busy_timeout"], 5000)
        self.assertEqual(snapshot["foreign_keys"], 1)
        self.assertEqual(snapshot["synchronous"], 1)  # NORMAL

    def test_infrastructure_tables_exist(self):
        names = set(db.table_names(self.conn))
        for table in INFRA_TABLES:
            self.assertIn(table, names)

    def test_tables_are_strict(self):
        for table in INFRA_TABLES + ("sys_schema_version",):
            sql = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            self.assertIsNotNone(sql, table)
            self.assertIn("STRICT", sql[0], table + " 不是 STRICT 表")

    def test_migration_is_idempotent(self):
        self.assertEqual(db.migrate(self.conn), [])  # 已应用过,再跑应为空

    def test_outbox_is_append_only(self):
        self._insert_outbox()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE ops_outbox SET payload='{}' WHERE id=1")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM ops_outbox WHERE id=1")
        self.conn.rollback()
        # 投递状态允许更新(worker 需要标记已处理)
        self.conn.execute(
            "UPDATE ops_outbox SET processed_at=?, attempts=attempts+1 WHERE id=1",
            ("2026-10-08T11:00:00+08:00",),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT payload, processed_at, attempts FROM ops_outbox WHERE id=1"
        ).fetchone()
        self.assertEqual(row["payload"], '{"cents": 3800}')
        self.assertEqual(row["attempts"], 1)

    def test_audit_is_append_only(self):
        self.conn.execute(
            "INSERT INTO ops_audit(ts, actor, action, target) VALUES (?,?,?,?)",
            ("2026-10-08T10:00:00+08:00", "agent", "ledger.txn.create", "txn:1"),
        )
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE ops_audit SET action='x' WHERE id=1")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM ops_audit WHERE id=1")
        self.conn.rollback()

    def test_audit_actor_is_constrained(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO ops_audit(ts, actor, action) VALUES (?,?,?)",
                ("2026-10-08T10:00:00+08:00", "stranger", "peek"),
            )
        self.conn.rollback()

    def test_idempotency_key_is_unique(self):
        row = ("key-1", "fp-1", "completed", "{}", "t0", "t0")
        self.conn.execute(
            "INSERT INTO sys_idempotency(key, fingerprint, status, response, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            row,
        )
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO sys_idempotency(key, fingerprint, status, response, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                ("key-1", "fp-2", "completed", "{}", "t0", "t0"),
            )
        self.conn.rollback()

    def test_failure_sample_kinds_are_constrained(self):
        self.conn.execute(
            "INSERT INTO ops_failure_samples(ts, kind, input) VALUES (?,?,?)",
            ("2026-10-08T10:00:00+08:00", "user_correction", "上月的餐饮是 860"),
        )
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO ops_failure_samples(ts, kind) VALUES (?,?)",
                ("2026-10-08T10:00:00+08:00", "nonsense"),
            )
        self.conn.rollback()


class RuntimeContextTestCase(unittest.IsolatedAsyncioTestCase):
    """M0 的核心 DoD:时间上下文每轮注入,且来自代码而非模型。"""

    def _settings(self) -> config.Settings:
        return dataclasses.replace(
            config.get_settings(reload=True), provider="fake", session_id="test-session"
        )

    async def test_context_injected_every_turn(self):
        settings = self._settings()
        client = FakeClient()
        loop = AgentLoop(client, settings)

        await loop.respond("第一轮随便聊聊")
        await loop.respond("现在几点?")

        self.assertEqual(len(client.calls), 2)
        first_system = client.calls[0][0]
        second_system = client.calls[1][0]
        self.assertEqual(first_system["role"], "system")
        self.assertIn("<runtime-context>", first_system["content"])
        self.assertIn("本轮序号: 1", first_system["content"])
        self.assertIn("本轮序号: 2", second_system["content"])

        today = settings.now().strftime("%Y-%m-%d")
        self.assertIn("当前时间: " + today, second_system["content"])
        self.assertIn(settings.timezone, second_system["content"])
        # 模型是照着注入的时间回答的 —— 证明链路通了
        self.assertIn(today, loop.turns[-1].assistant_text)

    async def test_history_is_projected_in_order(self):
        settings = self._settings()
        client = FakeClient()
        loop = AgentLoop(client, settings)
        await loop.respond("我叫 Kevin")
        await loop.respond("我叫什么?")

        third_call = client.calls[-1]
        roles = [m["role"] for m in third_call]
        self.assertEqual(roles[0], "system")
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)
        self.assertIn("我叫 Kevin", third_call[1]["content"])

    async def test_conversation_budget_is_enforced(self):
        loop = AgentLoop(FakeClient(), self._settings())
        loop.history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": "字" * 2000}
            for i in range(20)
        ]
        messages = loop.build_messages()
        projected = config.estimate_messages_tokens(messages[1:])
        self.assertLessEqual(projected, config.BUDGET_CONVERSATION)

    async def test_usage_accumulates(self):
        loop = AgentLoop(FakeClient(), self._settings())
        await loop.respond("一")
        await loop.respond("二")
        self.assertEqual(len(loop.turns), 2)
        self.assertGreater(loop.total_usage.prompt_tokens, 0)


class CLITestCase(unittest.TestCase):
    """端到端:进程真的能起来、能回答、能自检。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "la", *args],
            cwd=str(BASE_DIR),
            env=offline_env(self.data_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )

    def test_check_reports_infrastructure(self):
        proc = self._run("--check")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for table in INFRA_TABLES:
            self.assertIn(table, proc.stdout)
        self.assertIn("1:m0_infrastructure", proc.stdout)

    def test_once_answers_with_injected_time(self):
        proc = self._run("--once", "现在几点?")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("现在是", proc.stdout)
        self.assertRegex(proc.stdout, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


if __name__ == "__main__":
    unittest.main()
