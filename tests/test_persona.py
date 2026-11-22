"""人格装配的离线护栏:提示词怎么拼、会不会溢预算、会不会渗到别处。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_persona -v

⚠️ 这里**不测人格本身**。"她像不像小默"离线测不出来 —— 那要用真实 API 跑
   `scripts/verify_persona.py`(20 条用例,先跑基线再对照)。
   这个文件只守那些**能确定的事**:装配顺序、预算没被吃光、以及
   "人设块别跑到压缩器 / 视觉 / 领域提示词里去"。

两条设计依据:
  * D47:红线写进返回值,不写进提示词 —— 所以人设块里**不许**出现硬约束的落点;
  * 预算:clip_to_budget 是**截尾**的,而 system_text = IDENTITY + PERSONA + 技能目录。
    人设块一旦吃光预算,先被砍掉的是技能目录(助手会突然不知道自己会什么)。
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from la import config
from la.agent import prompts
from la.agent.prompts import build_system_message
from la.agent.session import MemorySession
from la.llm.fake import FakeClient
from la.memory.projector import SEGMENT_SYSTEM, Projector
from la.memory.store import MemoryStore
from la.skills import SkillLibrary
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY

BASE_DIR = Path(__file__).resolve().parent.parent

#: 稳定前缀里"人设块"的位置:IDENTITY 之后、运行时上下文之前。
#: 顺序有讲究 —— runtime 每轮都变,把会变的东西往前挪就等于把 KV 缓存前缀打散。
RUNTIME_OPEN = "<runtime-context>"


def make_settings(session_id: str = "persona") -> config.Settings:
    return dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id=session_id
    )


class PersonaBlockTestCase(unittest.TestCase):
    """人设块本身:写了没、有多长、安全绳还在不在。"""

    def test_persona_exists(self):
        self.assertTrue(prompts.PERSONA.strip(), "PERSONA 不该是空的")

    def test_persona_stays_small(self):
        """人设块 < 2000 字。

        不是为了省 token(预算 2500,现在只用两百多),而是因为超预算时
        被截掉的是**尾巴**上的技能目录 —— 那种失败不会报错,只会让助手
        突然不知道自己有哪些技能。
        """
        self.assertLess(len(prompts.PERSONA), 2000, "人设块太长了,会挤掉技能目录")

    def test_persona_keeps_the_priority_ladder(self):
        """'人格只决定怎么说,不决定说什么' —— 这是整个人格实验的安全绳。

        改丢了它,人格就会开始影响**内容**(软化坏消息、回避该问的问题),
        而那正是这个改动最危险的失败模式。所以拿测试钉住。
        """
        for token in ("怎么说", "说什么"):
            self.assertIn(token, prompts.PERSONA)

    def test_the_boundary_count_matches_the_list(self):
        """IDENTITY 说"五条边界",下面就正好五条;尾巴那句阶梯也得说"上面五条"。

        这条是给一次**真实的漂移**钉的:第 5 条(审视自己时先看事实,D138)加进来之后,
        开头与尾巴都还写着"四条" —— 优先级阶梯于是指着一个小了一圈的集合,
        而第 5 条(别把对自己的评价写进记忆)恰好不在"不让步"的那一档里。
        """
        import re

        declared = {"四": 4, "五": 5, "六": 6}
        match = re.search(r"([四五六])条不可违反的边界", prompts.IDENTITY)
        self.assertIsNotNone(match, "IDENTITY 开头那句边界声明没了")
        wanted = declared[match.group(1)]
        listed = [int(item) for item in re.findall(r"^(\d+)\. ", prompts.IDENTITY, re.M)]
        self.assertEqual(listed, list(range(1, wanted + 1)), "边界条数与开头说的对不上")
        self.assertIn(
            "上面" + match.group(1) + "条边界", prompts.PERSONA_TAIL,
            "优先级阶梯引用的条数必须与 IDENTITY 一致",
        )

    def test_persona_keeps_the_identity_rules_ahead_of_it(self):
        """边界条款仍在 IDENTITY 里,而且人设块明说它优先级更低。"""
        self.assertIn("边界", prompts.PERSONA)
        for token in ("数字必须来自查询", "记忆里没有", "ask_user"):
            self.assertIn(token, prompts.IDENTITY)

    def test_persona_bans_affectation(self):
        """禁用清单必须在 —— 没有它,'16 岁少女'会被模型演绎成语气词堆砌。"""
        for token in ("颜文字", "卖萌", "撒娇"):
            self.assertIn(token, prompts.PERSONA)

    def test_persona_carries_no_hard_rules(self):
        """D47:硬约束的落点是**返回值**,不是提示词。

        所以人设块里不许出现免责声明这类"靠自觉"的东西 ——
        它只该管口吻。
        """
        for token in ("免责", "MEDICAL_DISCLAIMER", "误差说明"):
            self.assertNotIn(token, prompts.PERSONA)


class SystemMessageAssemblyTestCase(unittest.TestCase):
    """装配顺序:IDENTITY -> PERSONA -> 运行时上下文。"""

    def setUp(self) -> None:
        self.settings = make_settings()

    def test_order_is_identity_then_persona_then_runtime(self):
        content = build_system_message(self.settings, turn=1, surface="web")["content"]
        head = content.index("你是")
        persona_at = content.index(prompts.PERSONA[:24])
        runtime_at = content.index(RUNTIME_OPEN)
        self.assertLess(head, persona_at, "人设块该在身份之后")
        self.assertLess(persona_at, runtime_at, "人设块该在运行时上下文之前")
        self.assertEqual(content.index(RUNTIME_OPEN), content.rindex(RUNTIME_OPEN),
                         "运行时上下文只该出现一次")

    def test_system_segment_fits_budget_with_the_skill_catalog(self):
        """身份 + 人设 + 技能目录 必须整体在 BUDGET_SYSTEM 之内。

        断言的是**实测值等于原文长度**:不等就说明 clip_to_budget 动了刀,
        也就是有人被悄悄砍掉了。
        """
        text = (prompts.IDENTITY + "\n\n" + prompts.PERSONA + "\n\n"
                + SkillLibrary().catalog_text())
        projection = Projector(self.settings).build(
            system_text=text, runtime_text="", memory_text="", history=[]
        )
        self.assertEqual(projection.segments[SEGMENT_SYSTEM], config.estimate_tokens(text),
                         "system 段被裁剪了 —— 被砍掉的是技能目录")
        self.assertLessEqual(projection.segments[SEGMENT_SYSTEM], config.BUDGET_SYSTEM)


class PersonaPlacementTestCase(unittest.TestCase):
    """人设块只许在一个地方定义,而且必须真的进到投影里。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.conn = db.connect(tmp / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=tmp / "archive")

    def tearDown(self) -> None:
        # ⚠️ 必须先关连接再删临时目录:Windows 上被 sqlite 占着的文件删不掉
        #    (WinError 32),那样 tearDown 会以"无关的报错"炸掉真正的失败信息。
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 - 关不掉也不该让测试挂掉
            pass
        self._tmp.cleanup()

    def test_persona_string_is_defined_in_exactly_one_module(self):
        """扫全 la/:'小默'只该出现在 agent/prompts.py。

        这条拦的是"顺手在别的提示词里也写一份" —— D84 记录过一次同类翻车
        (视觉提示词里写死了领域角色),收拾起来很麻烦。
        """
        offenders = []
        for path in (BASE_DIR / "la").rglob("*.py"):
            if path.name == "prompts.py":
                continue
            if "小默" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(BASE_DIR)))
        self.assertEqual(offenders, [], "人设块被复制到了别处: " + str(offenders))

    def test_compressor_and_vision_prompts_are_not_personified(self):
        """压缩器与视觉提示词是**工具**,不是小默 —— 它们不该带人设。

        注入一次坏一次:压缩器带人设 → 结构化摘要开始跑偏;
        视觉带人设 → "只讲图上真实存在的信息"那条硬要求被稀释。
        """
        from la.llm.vision import DESCRIBE_PROMPT
        from la.memory.summarize import SUMMARY_SYSTEM_PROMPT

        for text in (SUMMARY_SYSTEM_PROMPT, DESCRIBE_PROMPT):
            self.assertNotIn("小默", text)
            self.assertNotIn("路过", text)

    def test_projection_carries_the_persona(self):
        """投影装配点(session.py)也得带上人设块 —— 只改 prompts.py 是不够的。"""
        session = MemorySession(
            FakeClient(), make_settings(), store=self.store, session_id="persona",
            auto_approve=True, auto_briefing=False,
        )
        session.start()
        projection = session._build_projection([], "", "")
        system = projection.messages[0]["content"]
        self.assertIn(prompts.PERSONA[:24], system, "投影里的 system 段没有人设块")
        self.assertIn("小默", system)


class DomainReplyShapeTestCase(unittest.TestCase):
    """领域工作的回复**一句说完**(用户 2026-10-13 点名:"不要给用户增加阅读负担")。

    ⚠️ 这条不是文风洁癖。它对应的失败模式是**阅读负担**:一屏明细里真正要看的那一行
       (记了什么、几点响)会被自己的复述和操作指引淹没 —— 回执、编号、时刻全白记。
       离线只能钉住"规则还在";到底缩没缩、缩了多少,由 scripts/verify_persona.py 的
       P01 / P21 / P22 / P23 用**真实模型**量字数(硬检查 brief,max_chars 写在用例上)。
    """

    def test_identity_pins_the_one_line_rule(self):
        for token in ("写完只说一句", "一行说完", "系统的回执", "不要自己再列一遍"):
            self.assertIn(token, prompts.IDENTITY, "IDENTITY 里的领域回复规矩被删了?")

    def test_persona_repeats_it_in_one_clause(self):
        self.assertIn("只回一句结果", prompts.PERSONA, "人设块里那句口径没了")

    def _eval_module(self, name: str):
        """按路径加载 scripts/verify_persona.py(它不是包,没法直接 import)。

        ⚠️ 必须先登记进 sys.modules:脚本里有 dataclass,而 dataclasses 会回头去
           sys.modules[cls.__module__] 找模块来解析注解 —— 不登记就是
           "'NoneType' object has no attribute '__dict__'"(实测踩过)。
        """
        import importlib.util
        import sys

        path = BASE_DIR / "scripts" / "verify_persona.py"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
        return module

    def test_the_eval_still_covers_domain_replies(self):
        """评测里必须留着**带字数上限**的领域用例 —— 提示词能被量,才谈得上对照。"""
        module = self._eval_module("_verify_persona_for_test")
        capped = {case.id: case.max_chars for case in module.CASES if case.max_chars}
        self.assertIn("P01", capped, "记账那条得有上限")
        self.assertIn("P21", capped, "用户原话那条(明天中午提醒我更新云朵)得在")
        for case_id, limit in capped.items():
            self.assertLessEqual(int(limit), 200, case_id + " 的上限太松了,量不出退化")

    def test_brief_is_a_hard_check(self):
        """brief 必须是**硬检查**(不过就非零退出),否则只能靠人眼看字数。"""
        module = self._eval_module("_verify_persona_for_test2")
        self.assertIn("brief", module.HARD)
        ok, detail = module.evaluate("brief", {"chars": 300, "max_chars": 100}, {})
        self.assertFalse(ok, detail)
        ok, _ = module.evaluate("brief", {"chars": 40, "max_chars": 100}, {})
        self.assertTrue(ok)
        ok, detail = module.evaluate("task_created", {}, {"todo_count": 0, "schedule_count": 0})
        self.assertFalse(ok, detail)
        ok, _ = module.evaluate("task_created", {}, {"todo_count": 1, "schedule_count": 1})
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
