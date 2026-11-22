# -*- coding: utf-8 -*-
"""批次 B 合并的体检(2026-10-15):健康 / 错题 / 日程三域,14 个工具 → 5 个。

合并的收益是 token 与"少几个工具名",代价是**多了 action 这层分发**。
所以这个文件只钉四件事:
  1. 旧名字真的不再注册;
  2. 合并**只在门禁字段一致的工具之间**发生(一个工具只有一份 risk/phase/writes);
  3. 分发诚实:未知 action 报错、缺参数报错、合法 action 能跑通;
  4. **字段约束没有在合并里失守** —— 合并模型的字段仍来自各自的原模型
     (max_length / ge / le / Literal 都还在),没有第二份 schema。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_m15_tool_merge_b -v
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from la import config
from la.base.phases import PHASE_ANSWER, CircuitBreaker
from la.agent.session import MemorySession
from la.llm.fake import FakeClient
from la.storage import db


RETIRED = (
    "health_log_meal", "health_log_workout", "health_log_checkin",
    "health_get_trend", "health_energy", "health_goal",
    "quiz_add_wrong_answer", "quiz_list", "quiz_weak_points", "quiz_me",
    "quiz_record_result", "quiz_make_sheet", "quiz_set_push", "quiz_add_knowledge_point",
    "calendar_cancel", "calendar_reschedule",
)


class BatchBMergeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        for name in ("archive", "media", "artifacts"):
            (self.tmp / name).mkdir(exist_ok=True)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
                        "ASSISTANT_DB", "MEMORY_DB")
        }
        self._env = os.environ.get("LA_LLM_PROVIDER")
        os.environ["LA_LLM_PROVIDER"] = "fake"
        os.environ["DEEPSEEK_API_KEY"] = ""
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config._settings = None
        self.session = MemorySession(
            FakeClient(), config.get_settings(), session_id="batch-b-probe",
            assistant_conn=db.connect_assistant(),
        )
        self.specs = {spec.name: spec for spec in self.session.registry.specs()}

    def tearDown(self) -> None:
        try:
            self.session.assistant_conn.close()
        except Exception:
            pass
        for key, value in self._patched.items():
            setattr(config, key, value)
        if self._env is None:
            os.environ.pop("LA_LLM_PROVIDER", None)
        else:
            os.environ["LA_LLM_PROVIDER"] = self._env
        config._settings = None
        self._tmp.cleanup()

    def _call(self, name: str, params: dict):
        return asyncio.run(self.session.registry.execute(
            name, params, breaker=CircuitBreaker(phase=PHASE_ANSWER),
            session_id="batch-b-probe",
        ))

    def test_retired_names_are_gone(self) -> None:
        still = [name for name in RETIRED if name in self.specs]
        self.assertEqual(still, [], "这些名字还挂在注册表里:" + ", ".join(still))
        for name in ("health_log", "health_plan", "quiz_find", "quiz_write", "calendar_change"):
            self.assertIn(name, self.specs, name + " 没注册")

    def test_merged_groups_keep_one_gate_attestation(self) -> None:
        expected = {
            "health_log": ("low", "answer", True),
            "health_plan": ("low", "answer", False),
            "quiz_find": ("low", "answer", False),
            "quiz_write": ("low", "answer", True),
            "calendar_change": ("confirm", "answer", True),
        }
        for name, (risk, phase, writes) in expected.items():
            spec = self.specs[name]
            self.assertEqual((spec.risk, spec.phase, spec.writes), (risk, phase, writes),
                             name + " 的门禁声明变了(合并等于放宽闸,要重新论证)")
            self.assertEqual(spec.category, "task", name + " 的配额类目变了")

    def test_only_read_only_merges_enter_the_read_only_whitelist(self) -> None:
        from la.tools.registry import READ_ONLY_TOOLS

        for name in ("health_plan", "quiz_find"):
            self.assertIn(name, READ_ONLY_TOOLS, name + " 是只读工具,应该进白名单")
            self.assertFalse(self.specs[name].writes, name + " 标成会写却进了白名单")
        for name in ("health_log", "quiz_write", "calendar_change"):
            self.assertNotIn(name, READ_ONLY_TOOLS, name + " 会写,不能进只读白名单")
            self.assertTrue(self.specs[name].writes, name + " 是写入工具,却标成只读")

    def test_merged_schema_keeps_the_original_constraints(self) -> None:
        """合并模型的字段仍来自各自的原模型:约束一个都不许丢。"""
        schema = self.specs["health_log"].parameters_json_schema()
        props = schema["properties"]
        self.assertEqual(props["amount"]["maximum"], 100)
        # ⭐ 2026-11-21(D188):duration_min 从"必填"改成"两个维度里的一个" ——
        #    Optional 在 pydantic v2 里走 anyOf[int, null],约束(minimum)跟着进分支。
        self.assertEqual(props["duration_min"]["anyOf"][0]["minimum"], 1)
        self.assertEqual(props["reps"]["anyOf"][0]["minimum"], 1)
        self.assertEqual(props["distance_km"]["anyOf"][0]["maximum"], 1000)
        # Literal 在 pydantic v2 里是 anyOf[enum, null] —— 原样搬过来(不是手写第二份)。
        self.assertEqual(
            props["meal_type"]["anyOf"][0]["enum"], ["早餐", "午餐", "晚餐", "加餐"]
        )
        self.assertIn("action", props)
        quiz = self.specs["quiz_write"].parameters_json_schema()["properties"]
        # daily_limit 是 Optional[int] with ge/le —— pydantic v2 放在 anyOf 分支里,
        # 合并照搬(约束没有在合并里失守)。
        self.assertEqual(quiz["daily_limit"]["anyOf"][0]["minimum"], 1)
        self.assertEqual(quiz["daily_limit"]["anyOf"][0]["maximum"], 50)
        plan = self.specs["health_plan"].parameters_json_schema()["properties"]
        self.assertEqual(plan["days"]["minimum"], 7)

    def test_unknown_action_is_rejected_with_the_allowed_set(self) -> None:
        # calendar_change 的 schema 额外要求 uid(它不像别的四个可以空着问),
        # 所以这一组要带上各自的必填,才能走到分发器那一步。
        cases = [
            ("health_log", {}),
            ("health_plan", {}),
            ("quiz_find", {}),
            ("quiz_write", {}),
            ("calendar_change", {"uid": "u1"}),
        ]
        for name, extra in cases:
            result = self._call(name, {"action": "没有这个动作", **extra})
            self.assertFalse(result.ok, name + " 对未知 action 没有报错")
            self.assertIn("action 只能是", result.message,
                          name + " 的报错没说清 action 该给什么:" + result.message[:80])

    def test_workout_accepts_one_dimension_and_refuses_none(self) -> None:
        """⭐ 2026-11-21(D188):运动的两个维度。

        只给一个就只记一个(不估算);一个都不给 = **参数错误** + **不落库** ——
        这是"追问用户"的结构性保证:模型没有可填补的空子,只能回去问。
        """
        from la.tools.registry import CODE_BAD_PARAMS

        only_reps = self._call("health_log", {
            "action": "workout", "activity": "仰卧起坐", "reps": 10})
        self.assertTrue(only_reps.ok, only_reps.message)
        self.assertEqual(only_reps.data["reps"], 10)
        self.assertIsNone(only_reps.data["duration_min"])
        self.assertEqual(only_reps.data["amount_text"], "10 个")
        self.assertIsNone(only_reps.data["met_minutes"], "没有时长就不该有 MET-分钟")
        self.assertNotIn("kcal_estimate", only_reps.data, "没有时长就不该估算消耗")
        self.assertIn("没记时长", str(only_reps.data.get("calorie_note") or ""))

        none_given = self._call("health_log", {
            "action": "workout", "activity": "仰卧起坐"})
        self.assertFalse(none_given.ok)
        self.assertEqual(
            none_given.error_code, CODE_BAD_PARAMS,
            "缺维度应报 BAD_PARAMS —— 报成 HANDLER_ERROR 会让自省报表读成『工具坏了』",
        )
        self.assertIn("先问用户", none_given.message)

        # 连"做了什么"都不知道时也要先问,不要拿默认运动名去凑一行。
        no_activity = self._call("health_log", {"action": "workout", "reps": 10})
        self.assertFalse(no_activity.ok)
        self.assertEqual(no_activity.error_code, CODE_BAD_PARAMS)
        self.assertIn("先问用户", no_activity.message)

        rows = self.session.assistant_conn.execute(
            "SELECT count(*) FROM app_workouts").fetchone()[0]
        self.assertEqual(int(rows), 1, "一个维度都没给的那次不该写进库")

    def test_each_action_still_reaches_its_own_validation(self) -> None:
        cases = [
            ("health_log", {"action": "checkin", "value": 70}, "metric_code"),
            ("health_plan", {"action": "goal"}, "target_value"),
            ("quiz_write", {"action": "add"}, "subject"),
            ("quiz_write", {"action": "result"}, "rating"),
            # ⚠️ reschedule 的 new_start 在 CalendarReschedule 里是**可选**的
            #    (服务层自己判断"至少要给一个要改的字段"),所以这里断 uid。
            ("calendar_change", {"action": "reschedule"}, "uid"),
            ("calendar_change", {"action": "cancel"}, "uid"),
        ]
        for name, params, hint in cases:
            result = self._call(name, params)
            self.assertFalse(result.ok, name + " 缺参数却没报错")
            self.assertIn(hint, result.message,
                          name + " 的报错里没提 " + hint + ":" + result.message[:90])

    def test_every_declared_action_can_actually_run(self) -> None:
        """每个 action 至少要在**不报 UNKNOWN_ACTION** 的前提下跑通一遍。"""
        runs = [
            ("health_log", {"action": "meal", "food_name": "燕麦粥"}),
            ("health_log", {"action": "workout", "activity": "跑步", "duration_min": 30}),
            # D188:只给个数也必须是合法的一次调用(以前会报 duration_min 缺失)。
            ("health_log", {"action": "workout", "activity": "仰卧起坐", "reps": 10}),
            ("health_log", {"action": "checkin", "metric_code": "weight", "value": 70.5}),
            ("health_plan", {"action": "goal", "metric": "weight", "target_value": 65}),
            ("health_plan", {"action": "trend"}),
            ("health_plan", {"action": "energy"}),
            ("quiz_find", {"action": "list"}),
            ("quiz_find", {"action": "weak"}),
            ("quiz_find", {"action": "me", "count": 3}),
            ("quiz_write", {"action": "add", "subject": "数学"}),
            ("quiz_write", {"action": "know", "name": "加法"}),
            ("quiz_write", {"action": "push", "enabled": True}),
        ]
        for name, params in runs:
            result = self._call(name, params)
            self.assertTrue(result.ok, name + " " + str(params) + " 失败:" + str(result.message)[:90])

    def test_catalog_shrank_and_fits_the_budget_claim(self) -> None:
        from la.llm.tools import build_tool_catalog

        blob = json.dumps(build_tool_catalog(self.session.registry.specs()), ensure_ascii=False)
        # 批 A 之后是 35,082 字符 / 46 个工具;批 B 更小、工具数更少。
        # ⚠️ 2026-11-08(M33 笔记域):基线随"新增域"上调到 38,000 / 45 ——
        #    与 test_m15_tool_merge 同一个口径(见那里的说明)。
        # ⚠️ 2026-11-11(D181):health_log 增加 profile 动作(出生年份/身高等四个字段),
        #    catalog 38,150 → 38,556;基线随之上调到 38,600(仍是**紧贴实测**的守卫)。
        # ⚠️ 2026-11-21:新增 compact_context(用户主动压缩上下文),实测 38,899;
        #    基线随拍板上调到 39,000,与 test_m15_tool_merge 同一口径。
        # ⚠️ 2026-11-21(D188):health_log 的 workout 说明补上"两个维度",实测 39,175;
        #    基线随之上调到 39,250(同一条纪律:BUDGET_TOOLS 同步 9,780 → 9,850)。
        self.assertLess(len(blob), 39250, "catalog 超出基线(39,250):" + str(len(blob)))
        self.assertLessEqual(len(self.session.registry.specs()), 45, "工具数超出基线(45)")


if __name__ == "__main__":
    unittest.main()
