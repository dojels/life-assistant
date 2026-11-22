# -*- coding: utf-8 -*-
"""批次 A 合并的体检(2026-10-15):工具数变少了,但**合同与门禁一样都不许丢**。

合并的收益是 token(见 docs/工具集瘦身对照表.md),代价是**多了 action 这层分发**。
所以这个文件只钉三件事:
  1. 旧名字真的不再注册(免得"合并了但老的还在"——那就是纯亏);
  2. 合并**只在门禁字段一致的工具之间**发生(risk / phase / writes / category 四同),
     否则一个工具只能有一份声明,合并就等于把确认框或只读闸一起放宽;
  3. 分发本身要诚实:未知 action 报错、缺参数报错、合法 action 能跑通。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_m15_tool_merge -v
"""

from __future__ import annotations

import asyncio
import os
import unittest

from la import config
from la.agent.session import MemorySession
from la.llm.fake import FakeClient
from la.storage import db


#: 批次 A 合并掉的旧名字。它们**不许**再出现在注册表里 —— 留着就是两头付钱:
#: 既付了 action 分发的复杂度,又没省下 token。
RETIRED = (
    "file_read", "file_list", "web_search", "web_fetch",
    "skill_list", "skill_load",
    "schedule_create", "schedule_list", "schedule_update", "schedule_delete",
    "goal_create", "goal_update",
)

#: 合并后的新名 -> 它并掉了哪些(用于"门禁一致"的体检)。
MERGED = {
    "file": ("file_read", "file_list"),
    "web": ("web_search", "web_fetch"),
    "skill": ("skill_list", "skill_load"),
    "schedule": ("schedule_create", "schedule_list", "schedule_update", "schedule_delete"),
    "goal": ("goal_create", "goal_update"),
}


class MergedToolsCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory(ignore_cleanup_errors=True)
        from pathlib import Path

        self.tmp = Path(self._tmp.name)
        # ⚠️ 2026-10-25:这些路径**必须**在 tearDown 里还原。以前没还原 + 临时目录被清掉,
        #    在 Windows 上碰巧不出事(文件还被占着,rmtree 静默失败,目录还在),而在 Linux 上
        #    目录真的被删了 —— 后面所有用默认路径的测试(persona / usage_report / 微信登录)
        #    全部报 "unable to open database file"。云端 T0 就是被这一条拖红的。
        self._saved_paths = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
                        "ASSISTANT_DB", "MEMORY_DB")
        }
        for name in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR"):
            setattr(config, name, self.tmp / name.lower())
        (self.tmp / "archive").mkdir(exist_ok=True)
        (self.tmp / "media").mkdir(exist_ok=True)
        (self.tmp / "artifacts").mkdir(exist_ok=True)
        self._env = os.environ.get("LA_LLM_PROVIDER")
        os.environ["LA_LLM_PROVIDER"] = "fake"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config._settings = None
        self.session = MemorySession(
            FakeClient(), config.get_settings(), session_id="merge-probe",
            assistant_conn=db.connect_assistant(),
        )
        self.specs = {spec.name: spec for spec in self.session.registry.specs()}

    def tearDown(self) -> None:
        try:
            self.session.assistant_conn.close()
        except Exception:
            pass
        try:
            self.session.store.conn.close()
        except Exception:
            pass
        if self._env is None:
            os.environ.pop("LA_LLM_PROVIDER", None)
        else:
            os.environ["LA_LLM_PROVIDER"] = self._env
        # ⚠️ 还原顺序:先还原路径,再清目录 —— 反过来会让"目录已经没了"的那一瞬间
        #    被别的清理逻辑读到(与上面那条 Linux 真机失败同源)。
        for key, value in getattr(self, "_saved_paths", {}).items():
            setattr(config, key, value)
        config._settings = None
        self._tmp.cleanup()

    def _call(self, name: str, params: dict):
        from la.base.phases import CircuitBreaker, PHASE_ANSWER

        return asyncio.run(self.session.registry.execute(
            name, params, breaker=CircuitBreaker(phase=PHASE_ANSWER),
            session_id="merge-probe",
        ))

    # -- 1. 旧名字真的没了 ---------------------------------------------------
    def test_retired_names_are_not_registered_any_more(self) -> None:
        still = [name for name in RETIRED if name in self.specs]
        self.assertEqual(still, [], "这些名字还挂在注册表里(合并没做干净):" + ", ".join(still))
        for new_name in MERGED:
            self.assertIn(new_name, self.specs, new_name + " 没注册")

    # -- 2. 合并只在门禁一致的工具之间 ---------------------------------------
    def test_merged_groups_share_one_gate_attestation(self) -> None:
        """一个工具只有一份 risk/phase/writes/category —— 合并的前提是这四项本来就相同。"""
        # ⚠️ 这里不重新构造旧 spec(它们已经不存在了),而是**钉住合并后的值** ——
        #    将来有人把 confirm 的工具并进来,门禁会变,这几条就会红。
        expected = {
            "file": ("low", "any", False),
            "web": ("low", "any", False),
            "skill": ("low", "any", False),
            "schedule": ("low", "any", True),
            "goal": ("low", "any", True),
        }
        for name, (risk, phase, writes) in expected.items():
            spec = self.specs[name]
            self.assertEqual((spec.risk, spec.phase, spec.writes), (risk, phase, writes),
                             name + " 的门禁声明变了(合并等于放宽闸,要重新论证)")
            # 五个都是 category=task(事项层工具不占考古步数,但裁决阶段照样被拒)
            self.assertEqual(spec.category, "task", name + " 的配额类目变了")

    def test_read_only_whitelist_covers_only_read_only_merges(self) -> None:
        """只有"合并后仍然只读"的工具才配进白名单;写工具混进去 = 只读模式被击穿。"""
        from la.tools.registry import READ_ONLY_TOOLS

        for name in ("file", "web", "skill"):
            self.assertIn(name, READ_ONLY_TOOLS, name + " 是只读工具,应该进白名单")
            self.assertFalse(self.specs[name].writes, name + " 标成会写,却进了只读白名单")
        for name in ("schedule", "goal"):
            self.assertNotIn(name, READ_ONLY_TOOLS, name + " 会写,不能进只读白名单")

    # -- 3. 分发要诚实 -------------------------------------------------------
    def test_unknown_action_is_rejected_with_the_allowed_set(self) -> None:
        for name in ("schedule", "goal", "file", "web", "skill"):
            result = self._call(name, {"action": "没有这个动作"})
            self.assertFalse(result.ok, name + " 对未知 action 没有报错")
            self.assertIn("action", result.message, name + " 的报错没说清 action 该给什么")

    def test_each_action_still_reaches_its_own_argument_checks(self) -> None:
        """分发到分支之后,原工具的参数校验必须照旧生效(不能被"合并"顺手吃掉)。"""
        # 注意 web(action=fetch) 不在此列:它先撞"联网是关着的"那道闸,
        # 那是更早也更正确的失败(联网默认关是刻意的)。
        cases = [
            ("schedule", {"action": "update"}, "要给 id"),
            ("skill", {"action": "load"}, "技能"),
        ]
        for name, params, expected in cases:
            result = self._call(name, params)
            self.assertFalse(result.ok, name + " 缺参数却没报错")
            self.assertIn(expected, result.message, name + " 的报错变了:" + result.message[:80])

    def test_file_action_defaults_by_arguments(self) -> None:
        """file 的 action 留空时按参数判断 —— pattern/recursive 走 list,其余走 read。"""
        from pathlib import Path

        # ⚠️ 探针文件必须落在**白名单里**:file 工具读任何路径都要过 G18 的路径策略
        #    (允许的根 = 项目根 + DATA_DIR),写到临时目录的**上一层**会被正确地拒掉。
        probe_dir = self.tmp / "data_dir"
        probe_dir.mkdir(exist_ok=True)
        (probe_dir / "probe.txt").write_text("甲\n乙", encoding="utf-8")
        read = self._call("file", {"path": str(probe_dir / "probe.txt")})
        self.assertTrue(read.ok, read.message)
        self.assertIn("1| 甲", read.data)
        listed = self._call("file", {"path": str(probe_dir), "pattern": "*.txt"})
        self.assertTrue(listed.ok, listed.message)
        self.assertIn("probe.txt", listed.data)
        del Path

    # -- 4. 省下的字节是真的 -------------------------------------------------
    def test_catalog_is_smaller_than_the_pre_merge_baseline(self) -> None:
        """合并的账要能算:53 个工具在"合并前 + wire 最小化"下的基线是 35,082 字符。

        ⚠️ 2026-11-08(M33 笔记域):**基线被重新定过** —— 35,082 → 38,000 字符、
        48 → 45 个工具。理由:笔记域是用户点名要的**新增**域(+3 工具),它不是
        "合并又把字节吃回去了",拿合并前的基线去卡它等于禁止加功能。

        ⚠️ 2026-11-09(D177):38,000 → **38,400**。quiz_write 补了 delete 这个动作与
        跟读的写法说明(短语写「英语跟读：take care of」,系统整句连读)—— 又只差 48 字符
        就顶到基线,那样下一次改说明就会红。工具数仍是 42。

        ⚠️ 2026-11-11(D181):38,400 → **38,600**。health_log 增加 profile 动作
        (性别/出生年份/身高/活动水平)—— 真机里用户报出生年份,模型只能说
        "系统里没有存年份的字段"。实测 38,556;工具数仍是 42。

        ⚠️ 2026-11-21:38,600 → **39,000**。新增 compact_context(用户主动压缩上下文,
        工具自己先问确认)。实测 **38,899** 字符、43 个工具;它是一次**拍板过的新增**,
        不是合并退化。BUDGET_TOOLS 同步 9700 → 9780(见 config.py 同一条记账)。

        ⚠️ 2026-11-21(D188):39,000 → **39,250**。health_log 的 workout 说明补上
        "两个维度"(时长 / 距离 / 个数)与"一个都没给先问、不算消耗"。实测
        **39,175** 字符、43 个工具;BUDGET_TOOLS 同步 9,780 → 9,850。

        这条测试仍然有用:它挡的是**没经过拍板的悄悄膨胀** —— 超过 39,250
        就说明又加了东西,那时该做的是复核 config.BUDGET_TOOLS 并记账。
        """
        import json

        from la.llm.tools import build_tool_catalog

        blob = json.dumps(build_tool_catalog(self.session.registry.specs()), ensure_ascii=False)
        self.assertLess(len(blob), 39250,
                        "工具集又变大了(合并+笔记域+删除动作之后的基线 39,250 字符):" + str(len(blob)))
        self.assertLessEqual(len(self.session.registry.specs()), 45,
                             "工具数超出基线(合并 + 笔记域 = 45):先看看是不是又加了域")


if __name__ == "__main__":
    unittest.main()
