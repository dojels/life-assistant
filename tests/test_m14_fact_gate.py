"""永久事实层准入闸的回归测试(2026-10-13,D98)。

这一层为什么值得单独一份回归:
    facts 是唯一**每轮无条件进上下文**的一层,而它以前是"规则匹配到就直接写库"。
    实测 11 条生效事实里 5 条有问题,全部能追到那一条路径上。这份文件把
    "哪些东西从此进不来"逐条钉死 —— 免得以后又被简化回去。

覆盖:
    1. guard        —— 去重指纹全角标点、疑问句、残句、超长、未来动作
    2. 裁决器        —— JSON 解析容错、极性反了不许判重复
    3. FactGate     —— 候选不直接入库、拒绝留痕、完全重复合并、状态机
    4. 会话集成      —— 说话 -> 候选 -> 回复之后裁决 -> facts 才出现
    5. 注入覆盖      —— 超上限的条目"被挤掉"是可观测的,不是静默的
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from la import config
from la.agent.session import MemorySession
from la.llm.fake import FakeClient
from la.memory import guard
from la.memory.fact_adjudicator import (
    REL_CONTRADICTS,
    REL_DUPLICATE,
    REL_NEW,
    REL_REFINES,
    REL_REVIEW,
    arbitrate,
    parse_verdict,
)
from la.memory.fact_gate import FactGate
from la.memory.store import MemoryStore, _fact_fingerprint
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY


def make_settings(session_id: str = "s1") -> config.Settings:
    return dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id=session_id
    )


class GateFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.tmp / "archive")

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def gate(self, client=None) -> FactGate:
        return FactGate(self.store, client=client or FakeClient(), session_id="s1")

    def facts(self) -> list[str]:
        return [
            str(row["content"])
            for row in self.conn.execute(
                "SELECT content FROM facts WHERE active = 1 ORDER BY id"
            )
        ]


class GuardCase(GateFixture):
    """确定性检查:结构垃圾不需要模型就能判死。"""

    def test_fingerprint_catches_fullwidth_punctuation(self):
        # 钉的是一个**汉字级**的漏洞:原字符类漏了全角逗号/分号/问号,
        # 导致同一条事实的两种说法指纹不相等,于是写了两行、两行都进上下文。
        self.assertFalse("，" in _fact_fingerprint("用户喜欢打游戏，也喜欢滑板"))
        self.assertFalse("；" in _fact_fingerprint("用户喜欢打游戏；也喜欢滑板"))
        self.assertFalse("？" in _fact_fingerprint("用户喜欢打游戏？"))
        self.assertEqual(
            _fact_fingerprint("用户喜欢打游戏，也喜欢滑板"),
            _fact_fingerprint("用户 喜欢打游戏、也喜欢滑板"),
        )
        # 但**极性不能靠指纹抹平**:它是矛盾,不是重复(见裁决器的极性兜底)
        self.assertNotEqual(
            _fact_fingerprint("用户喜欢吃香菜"), _fact_fingerprint("用户不喜欢吃香菜")
        )

    def test_questions_are_rejected(self):
        for text in ("用户喜欢 吃什么东西", "用户喜欢 什么", "用户喜欢打游戏吗"):
            self.assertEqual(guard.check_candidate(text).code, "question", text)

    def test_truncated_and_fragment_are_rejected(self):
        self.assertEqual(
            guard.check_candidate("用户喜欢 打游戏，以前喜欢画画，喜欢看网络小说，然").code,
            "truncated",
        )
        self.assertEqual(
            guard.check_candidate("，明天晚上7点钟发信息提醒我").code, "fragment"
        )

    def test_template_artifact_goes_to_review_not_admit(self):
        # "用户比较喜欢 吃香菜" 这种汉字中间带空格的是模板拼装痕迹 ——
        # 不拒收,但**不许自动入库**(它正是那条极性写反的事实的样子)
        verdict = guard.check_candidate("用户比较喜欢 吃香菜")
        self.assertTrue(verdict.needs_review)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.code, "template_artifact")

    def test_future_action_is_review_not_fact(self):
        verdict = guard.check_candidate("用户明天晚上7点要发信息提醒我")
        self.assertTrue(verdict.needs_review)
        self.assertEqual(verdict.code, "future_action")

    def test_too_long_is_rejected_not_truncated(self):
        verdict = guard.check_candidate("用户喜欢" + "很长" * 80)
        self.assertEqual(verdict.code, "too_long")
        # **拒绝**而不是截断:截断才会造出"…然"那种半句话
        self.assertFalse(verdict.ok)

    def test_normal_preferences_pass(self):
        for text in ("用户不喜欢吃香菜", "用户的英文名是 Kevin", "用户在地铁公司上班"):
            self.assertTrue(guard.check_candidate(text).ok, text)

    def test_rule_layer_no_longer_inverts_polarity(self):
        # 规则层自己的洞:aversion 与 preference 两条规则各自 add(),
        # 于是"我不吃香菜"同时产出"用户喜欢 香菜"(错)与"用户不接受 香菜"(对)。
        from la.memory.notes import detect_notes

        contents = [note.content for note in detect_notes("我不吃香菜")]
        self.assertTrue(any("不接受" in item for item in contents), contents)
        self.assertFalse(any("喜欢" in item for item in contents), contents)

    def test_rule_layer_ignores_questions(self):
        from la.memory.notes import detect_notes

        self.assertEqual(detect_notes("我喜欢吃什么东西?"), [])


class VerdictCase(GateFixture):
    """裁决输出解析:解析不出来一律 review,绝不猜。"""

    def test_parses_each_relationship(self):
        self.assertEqual(parse_verdict('{"relationship":"new"}').relationship, REL_NEW)
        verdict = parse_verdict('{"relationship":"duplicate","target_id":3}')
        self.assertEqual(verdict.relationship, REL_DUPLICATE)
        self.assertEqual(verdict.target_id, 3)
        verdict = parse_verdict('{"relationship":"refines","target_id":2,"text":"合并后的说法"}')
        self.assertEqual(verdict.relationship, REL_REFINES)
        self.assertEqual(verdict.text, "合并后的说法")

    def test_garbage_becomes_review(self):
        for raw in ("", "我不知道", '{"relationship":"maybe"}',
                    '{"relationship":"duplicate"}'):
            verdict = parse_verdict(raw)
            self.assertEqual(verdict.relationship, REL_REVIEW, raw)
            self.assertFalse(verdict.ok)

    def test_markdown_wrapped_json_still_parses(self):
        fence = chr(96) * 3
        raw = fence + "json" + chr(10) + '{"relationship":"new"}' + chr(10) + fence
        self.assertEqual(parse_verdict(raw).relationship, REL_NEW)

    async def test_polarity_police_turns_duplicate_into_contradiction(self):
        # 模型把"喜欢/不喜欢"判成 duplicate 时,代码改判为矛盾 ——
        # 这一条判错的代价是**永久丢掉一个方向的偏好**。
        class FixedClient:
            model = "fixed"

            async def chat(self, messages, **kwargs):
                class R:
                    text = '{"relationship":"duplicate","target_id":1}'
                return R()

        self.store.add_fact("用户喜欢吃香菜", fact_type="preference")
        existing = self.store.active_facts(limit=20)
        verdict = await arbitrate(FixedClient(), "用户不喜欢吃香菜", existing)
        self.assertEqual(verdict.relationship, REL_CONTRADICTS)
        self.assertEqual(verdict.target_id, 1)


class FactGateCase(GateFixture):
    """准入状态机:提议不落库、拒绝留痕、重复合并、改口互指。"""

    async def test_candidate_alone_does_not_become_a_fact(self):
        gate = self.gate()
        gate.propose("用户养了一只猫", source="rule")
        self.assertEqual(self.facts(), [])
        self.assertEqual(self.store.candidate_counts()["pending"], 1)

    async def test_rejected_candidate_keeps_a_reason(self):
        gate = self.gate()
        gate.propose("用户喜欢 吃什么东西")
        report = await gate.process_pending()
        self.assertEqual(report.rejected, 1)
        self.assertEqual(self.facts(), [])
        row = self.store.candidates(status="rejected")[0]
        self.assertIn("问句", str(row["reason"]))
        # 留痕:归档里有一条"候选被拒"的事件(否则"某条路径从没走通"看不见)
        events = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='memory/note' AND data LIKE '%候选被拒%'"
        ).fetchone()[0]
        self.assertEqual(int(events), 1)

    async def test_same_statement_twice_is_merged_not_added_twice(self):
        """同一件事说两遍(换个标点)只留一条 —— 这是用户点名要的"去重把控"。

        ⚠️ 它走的必须是**模型的语义裁决**,不是字面判等:字面判等判不了
           "喜欢 X"与"不喜欢 X"(那是矛盾,见下面那条用例)。
        """
        gate = self.gate()
        first = gate.propose("用户养了一只猫")
        await gate.process_pending()
        self.assertEqual(self.facts(), ["用户养了一只猫"])
        second = gate.propose("用户养了一只猫。")
        self.assertNotEqual(second, 0)
        report = await gate.process_pending()
        self.assertEqual(report.duplicate, 1, report.summary())
        self.assertEqual(self.facts(), ["用户养了一只猫"])

    async def test_contradiction_keeps_both_rows_and_marks_the_old_one(self):
        store = self.store
        old_id = store.add_fact("用户喜欢吃香菜", fact_type="preference")
        gate = self.gate()
        gate.propose("用户不喜欢吃香菜", fact_type="preference")
        report = await gate.process_pending()
        self.assertEqual(report.admitted, 1)
        row = store.conn.execute(
            "SELECT active, superseded_by FROM facts WHERE id = ?", (old_id,)
        ).fetchone()
        # 旧的那条**不删除**,只是不再生效(还能追查"它当时为什么这么答")
        self.assertEqual(int(row["active"]), 0)
        self.assertIsNotNone(row["superseded_by"])
        self.assertEqual(self.facts(), ["用户不喜欢吃香菜"])

    async def test_same_content_from_two_sources_is_admitted_once(self):
        # 规则抽一条、模型也记一条 —— 这是 D28 踩过的真实坑(同一句话写两遍)
        gate = self.gate()
        gate.propose("用户的英文名是 Kevin", source="rule")
        await gate.process_pending()
        gate.propose("用户的英文名是 Kevin", source="memory_note")
        await gate.process_pending()
        self.assertEqual(self.facts().count("用户的英文名是 Kevin"), 1)

    async def test_candidate_dedup_by_normalized_content(self):
        gate = self.gate()
        first = gate.propose("用户养了一只猫")
        second = gate.propose("用户养了一只猫")
        self.assertNotEqual(first, 0)
        self.assertEqual(second, 0)  # 同一句重复提议不刷屏
        self.assertEqual(self.store.candidate_counts()["pending"], 1)

    async def test_pending_survives_without_a_client(self):
        # 没有模型 / 模型挂了 -> 候选留在 review,**不会消失**
        gate = FactGate(self.store, client=None, session_id="s1")
        gate.propose("用户养了一只猫")
        report = await gate.process_pending()
        self.assertEqual(report.review, 1)
        self.assertEqual(self.facts(), [])
        self.assertEqual(self.store.candidate_counts()["review"], 1)

    async def test_human_correct_goes_through_the_gate(self):
        store = self.store
        fact_id = store.add_fact("用户比较喜欢 吃香菜", fact_type="preference")
        gate = self.gate()
        # 人工改成一个**问句**也要被拦 —— 人工不是改库后门
        bad = await gate.human_correct(fact_id, "用户喜欢 吃什么东西")
        self.assertEqual(bad.status, "rejected")
        self.assertEqual(store.get_fact(fact_id).content, "用户比较喜欢 吃香菜")
        good = await gate.human_correct(fact_id, "用户不喜欢吃香菜")
        self.assertEqual(good.status, "admitted")
        self.assertEqual(store.get_fact(fact_id).content, "用户不喜欢吃香菜")


class SessionIntegrationCase(GateFixture):
    """一条消息走完全程:规则提议 -> 候选 -> 回复之后裁决 -> facts 才出现。"""

    def new_session(self, client=None) -> MemorySession:
        session = MemorySession(
            client or FakeClient(), make_settings(), store=self.store,
            session_id="s1", auto_briefing=False,
        )
        session.start()
        return session

    async def test_preference_reaches_facts_only_after_the_turn(self):
        session = self.new_session()
        await session.respond("我不吃香菜")
        self.assertEqual(len(self.facts()), 1, self.facts())
        self.assertIn("香菜", self.facts()[0])
        # 而且**不是**"用户喜欢 香菜"那条说反的
        self.assertNotIn("喜欢", self.facts()[0])

    async def test_fact_layer_is_not_written_before_the_gate_runs(self):
        session = self.new_session()
        session.store.append("s1", "user/message", {"text": "我不吃香菜"})
        session._capture_notes("我不吃香菜")
        # 只提议,不入库 —— 这正是这次改动的全部要点
        self.assertEqual(self.facts(), [])
        self.assertEqual(self.store.candidate_counts()["pending"], 1)
        await session.run_fact_gate()
        self.assertEqual(len(self.facts()), 1, self.facts())

    async def test_fake_marker_matches_the_real_one(self):
        # fake 客户端靠暗号识别"这是裁决请求";两边常量必须一致,否则离线测的是另一套
        from la.llm import fake
        from la.memory import fact_adjudicator

        self.assertEqual(fake.PROMPT_MARKER, fact_adjudicator.PROMPT_MARKER)


class InjectionCoverageCase(GateFixture):
    """注入覆盖:上限之外的事实"被挤掉"必须是可观测的,不能静默。"""

    def test_evicted_facts_are_reported(self):
        store = self.store
        for index in range(3):
            store.add_fact("第 " + str(index) + " 条偏好:喜欢喝第 " + str(index) + " 种茶")
        original = config.FACT_MAX_ACTIVE
        try:
            config.FACT_MAX_ACTIVE = 2
            overview = store.injection_overview()
        finally:
            config.FACT_MAX_ACTIVE = original
        self.assertEqual(overview["total_active"], 3)
        self.assertEqual(overview["injected_count"], 2)
        self.assertEqual(overview["evicted_count"], 1)
        # 注入的是**最新**的两条:最旧那条没进上下文
        self.assertNotIn(1, overview["injected_ids"])


if __name__ == "__main__":
    unittest.main()
