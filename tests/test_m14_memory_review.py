"""记忆候选的**人工放行**与**助手身份归人格**的回归测试(2026-10-25)。

用户当天报的三件事,这一份逐条钉住:
  ① "看这段对话,用户在微信端,无法让智能体写入事实"
       -> 裁决调用不再被思考吃光额度(它被判成"待人工放行"的真因);
          判不准时**当场**问用户放行;对话工具 memory_review 也能放行。
  ② "图2是网页端的显示,用户也没有审核放行的按钮"
       -> 记忆页 L3c 的 review 行有「放行 / 拒收」,且放行留回执、可撤销。
  ③ "智能体的英文名应该是人格部分的内容,要更新到自己的人格界面"
       -> 关于她自己的名字类陈述走 persona(人格页),不进事实层。

跑法:.venv/Scripts/python.exe -m unittest tests.test_m14_memory_review -v
"""

from __future__ import annotations

import asyncio
import dataclasses
import tempfile
import unittest
from pathlib import Path

from la import config
from la.agent.feedback import STATUS_ANSWERED, Answer
from la.agent.persona import PersonaStore, is_self_identity
from la.agent.session import MemorySession
from la.llm.client import ChatResult
from la.llm.fake import FakeClient
from la.memory import fact_adjudicator
from la.memory.fact_adjudicator import (
    REL_NEW,
    REL_REVIEW,
    VERDICT_MAX_TOKENS,
    arbitrate,
)
from la.memory.fact_gate import FactGate
from la.memory.projector import build_memory_block
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY


def make_settings(session_id: str = "s1") -> config.Settings:
    return dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id=session_id
    )


class JudgeBudgetCase(unittest.IsolatedAsyncioTestCase):
    """裁决调用:**不要思考**,而且额度够放一条完整判决。

    ⚠️ 这里只断言"调用方传了空串";空串在 wire 上是否真的变成 `none`
       (而不是"不发字段"、服务端仍按 high 跑),由
       `tests/test_llm_reasoning_budget.py` 钉住 —— 见 D170。
    """

    async def test_judge_turns_thinking_off_and_leaves_room(self):
        # 真机现场:max_tokens=200 + high 思考 -> 判决被截在半个字符串上
        # ("...","text":"助手的名字是"),parse_verdict 判"不是 JSON" -> 待人工放行。
        class Spy:
            model = "spy"

            def __init__(self):
                self.kwargs: dict = {}

            async def chat(self, messages, **kwargs):
                self.kwargs = kwargs
                return ChatResult(text='{"relationship":"new"}', model="spy")

        spy = Spy()
        verdict = await arbitrate(spy, "用户养了一只猫", [])
        self.assertEqual(verdict.relationship, REL_NEW)
        self.assertEqual(spy.kwargs.get("reasoning_effort"), "", "裁决不该开着思考跑")
        self.assertGreaterEqual(int(spy.kwargs.get("max_tokens") or 0), VERDICT_MAX_TOKENS,
                                "额度不够时判决会被截断成非 JSON")


class GateReleaseCase(unittest.IsolatedAsyncioTestCase):
    """FactGate.human_release:放行 / 拒收 / 判重 / 留痕 / 回执。"""

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

    def gate(self) -> FactGate:
        return FactGate(self.store, client=None, session_id="s1")

    def review_candidate(self, content: str = "用户养了一只猫", **extra) -> int:
        candidate_id = self.store.add_candidate(content, source="memory_note", session_id="s1", **extra)
        # 模拟"裁判判不准":真实路径里 adjudicate_row 会把它挪进 review
        self.store.set_candidate_status(candidate_id, "review", reason="裁判判不准")
        return candidate_id

    def facts(self) -> list[str]:
        return [
            str(row["content"])
            for row in self.conn.execute("SELECT content FROM facts WHERE active = 1 ORDER BY id")
        ]

    async def test_approve_writes_the_fact_and_leaves_a_receipt(self):
        candidate_id = self.review_candidate()
        admission = self.gate().human_release(candidate_id, approve=True, via="memory_page")
        self.assertEqual(admission.status, "admitted", admission.reason)
        self.assertGreater(admission.fact_id, 0)
        self.assertEqual(self.facts(), ["用户养了一只猫"])
        row = self.store.get_candidate(candidate_id)
        self.assertEqual(str(row["status"]), "admitted")
        self.assertEqual(int(row["fact_id"]), admission.fact_id)
        # 回执:撤销它是"那条新事实置失效 + 候选退回待放行"
        self.assertGreater(admission.receipt_id, 0, "放行没有回执,撤不回去")
        receipt = self.conn.execute(
            "SELECT undo_plan, label FROM ops_receipts WHERE id = ?", (admission.receipt_id,)
        ).fetchone()
        self.assertIn("release", str(receipt["undo_plan"]))
        self.assertIn("放行", str(receipt["label"]))
        # 事件也留了(排障时能回答"谁在什么时候放行的")
        events = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE data LIKE '%人工放行%'"
        ).fetchone()[0]
        self.assertEqual(int(events), 1)

    async def test_reject_writes_nothing(self):
        candidate_id = self.review_candidate()
        admission = self.gate().human_release(
            candidate_id, approve=False, reason="这条不对", via="memory_page",
        )
        self.assertEqual(admission.status, "rejected")
        self.assertEqual(self.facts(), [])
        row = self.store.get_candidate(candidate_id)
        self.assertEqual(str(row["status"]), "rejected")
        self.assertIn("这条不对", str(row["reason"]))

    async def test_approving_the_same_sentence_twice_does_not_duplicate_it(self):
        self.store.add_fact("用户养了一只猫", session_id="s1")
        candidate_id = self.review_candidate("用户养了一只猫。")
        admission = self.gate().human_release(candidate_id, approve=True)
        # 归一化后同一条(标点不算) -> duplicate,不重复写
        self.assertEqual(admission.status, "duplicate", admission.reason)
        self.assertEqual(self.facts(), ["用户养了一只猫"])

    async def test_already_resolved_candidate_is_not_released_again(self):
        candidate_id = self.review_candidate()
        self.gate().human_release(candidate_id, approve=True)
        again = self.gate().human_release(candidate_id, approve=True)
        self.assertEqual(again.status, "stale")
        self.assertEqual(self.facts(), ["用户养了一只猫"])

    async def test_release_with_supersedes_retires_the_old_fact(self):
        old_id = self.store.add_fact("用户喜欢吃香菜", fact_type="preference", session_id="s1")
        candidate_id = self.review_candidate("用户不喜欢吃香菜", fact_type="preference",
                                             supersedes=old_id)
        admission = self.gate().human_release(candidate_id, approve=True)
        self.assertEqual(admission.status, "admitted", admission.reason)
        row = self.conn.execute(
            "SELECT active, superseded_by FROM facts WHERE id = ?", (old_id,)
        ).fetchone()
        self.assertEqual(int(row["active"]), 0)
        self.assertEqual(int(row["superseded_by"]), admission.fact_id)

    async def test_review_count_is_scoped_to_the_session(self):
        self.review_candidate("用户养了一只猫")
        self.store.add_candidate("用户想学吉他", session_id="s2")
        self.store.set_candidate_status(
            int(self.store.candidates(status="pending")[0]["id"]), "review"
        )
        self.assertEqual(self.store.review_count(session_id="s1"), 1)

        self.assertEqual(self.store.review_count(session_id="s2"), 1)
        self.assertEqual(self.store.review_count(), 2)

    async def test_pending_review_is_announced_in_the_memory_block(self):
        # 模型要**知道**有人在等放行,否则微信里"通过吧"它接不住(用户实际那段对话)
        block = build_memory_block(
            facts=[], summaries=[], status="hit", now_text="", pending_review=2,
        )
        self.assertIn("<pending-review>", block)
        self.assertIn("memory_review", block)
        # 没有待放行时这一段不出现(块逐字节不变 = 前缀缓存不被这段改动打散)
        plain = build_memory_block(facts=[], summaries=[], status="hit", now_text="")
        self.assertNotIn("pending-review", plain)


class ChatReleaseCase(unittest.IsolatedAsyncioTestCase):
    """微信/网页对话里:判不准 -> 当场问 -> 他说放行才写。"""

    class ReviewJudgeClient:
        """裁决一律"判不准",正文正常 —— 模拟真机上裁判拿不准的那一类。"""

        model = "stub"

        async def chat(self, messages, **kwargs):
            user = str(messages[-1].get("content") or "")
            if fact_adjudicator.PROMPT_MARKER in user:
                return ChatResult(text='{"relationship":"review"}', model=self.model)
            return ChatResult(text="好的。", model=self.model)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.tmp / "archive")
        self.answers: list[str] = []

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def session(self, answers: list[str]) -> MemorySession:
        async def asker(question):
            value = answers.pop(0) if answers else ""
            return Answer(
                question_id=question.id, status=STATUS_ANSWERED,
                selected=[value] if value else [], text="" if value else "算了",
                by="user",
            )

        return MemorySession(
            self.ReviewJudgeClient(), make_settings(), store=self.store,
            session_id="s1", auto_briefing=False, asker=asker,
            auto_approve=True, direct_write=True,
        )

    def facts(self) -> list[str]:
        return [
            str(row["content"])
            for row in self.conn.execute("SELECT content FROM facts WHERE active = 1")
        ]

    async def test_review_then_user_says_release_immediately_writes_the_fact(self):
        session = self.session(["记住", "放行"])
        proposal = await session._confirm_and_add_fact("用户养了一只猫")
        self.assertEqual(proposal.status, "admitted", proposal.status)
        self.assertGreater(proposal.fact_id, 0)
        self.assertEqual(self.facts(), ["用户养了一只猫"])
        # 两次提问都留了痕:要记住吗 / 要放行吗
        asked = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='feedback/asked'"
        ).fetchone()[0]
        self.assertEqual(int(asked), 2)

    async def test_declining_the_release_keeps_the_candidate_waiting(self):
        session = self.session(["记住", "先不放"])
        proposal = await session._confirm_and_add_fact("用户养了一只猫")
        self.assertEqual(proposal.status, "review", "说了先不放还能写进去?")
        self.assertEqual(self.facts(), [])
        self.assertEqual(self.store.review_count(session_id="s1"), 1)



    async def test_memory_review_tool_is_the_wechat_backdoor(self):
        """微信端唯一的放行入口:先 list 看,再 approve / reject。"""
        from la.base.phases import CircuitBreaker, PHASE_ANSWER

        candidate_id = self.store.add_candidate(
            "用户养了一只猫", source="memory_note", session_id="s1",
        )
        self.store.set_candidate_status(candidate_id, "review", reason="裁判判不准")
        session = self.session([])

        listed = await session.registry.execute(
            "memory_review", {"action": "list"},
            breaker=CircuitBreaker(phase=PHASE_ANSWER), session_id="s1",
        )
        self.assertTrue(listed.ok, listed.message)
        self.assertEqual(listed.data["count"], 1, listed.data)
        self.assertIn("用户养了一只猫", str(listed.data["items"]))

        approved = await session.registry.execute(
            "memory_review", {"action": "approve", "candidate_id": candidate_id},
            breaker=CircuitBreaker(phase=PHASE_ANSWER), session_id="s1",
        )
        self.assertTrue(approved.ok, approved.message)
        self.assertEqual(approved.data["status"], "admitted", approved.data)
        self.assertEqual(self.facts(), ["用户养了一只猫"])

    async def test_memory_review_reject_keeps_the_facts_empty(self):
        from la.base.phases import CircuitBreaker, PHASE_ANSWER

        candidate_id = self.store.add_candidate(
            "用户不喜欢吃香菜", source="memory_note", session_id="s1",
        )
        self.store.set_candidate_status(candidate_id, "review", reason="裁判判不准")
        session = self.session([])
        rejected = await session.registry.execute(
            "memory_review", {"action": "reject", "reason": "这句不对"},
            breaker=CircuitBreaker(phase=PHASE_ANSWER), session_id="s1",
        )
        self.assertTrue(rejected.ok, rejected.message)
        self.assertEqual(rejected.data["status"], "rejected", rejected.data)


class PersonaPlacementCase(unittest.IsolatedAsyncioTestCase):
    """关于她自己的名字类陈述:认得出、写进人格、不进事实层。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "ASSISTANT_DB", "MEMORY_DB")
        }
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config._settings = None
        self.assistant = db.connect_assistant()
        self.memory_conn = db.connect(config.MEMORY_DB)
        db.migrate(self.memory_conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.memory_conn, archive_dir=self.tmp / "archive")

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            setattr(config, key, value)
        config._settings = None
        for conn in (self.assistant, self.memory_conn):
            try:
                conn.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def test_identifies_statements_about_herself_only(self):
        for text in (
            "助手的英文名是 Mo(对应中文名「小默」)",
            "你的英文名叫 Mo",
            "以后你叫小夏",
            "小默的名字是「小默」",
        ):
            self.assertTrue(is_self_identity(text), text)
        for text in ("用户叫小默", "你明天提醒我", "助手很聪明", "用户养了一只猫"):
            self.assertFalse(is_self_identity(text), text)

    async def test_persona_append_writes_to_the_persona_page(self):
        store = PersonaStore(self.assistant)
        before = store.state()
        previous, notes = store.append("英文名是 Mo(对应中文名「小默」)")
        after = store.state()
        self.assertIn("英文名是 Mo", after.body)
        self.assertTrue(after.custom)
        self.assertIn(before.body[:8], after.body, "追加不是重写:原来那段必须还在")
        # 撤销要能退回去:回执里带的就是这串(空串 = 当时还是出厂默认)
        self.assertEqual(previous, "" if not before.custom else before.body)
        self.assertEqual([], [item for item in notes if "太" in item])

    async def test_memory_note_routes_her_own_name_to_the_persona(self):
        async def asker(question):
            # 第一问"要记住这条吗" -> 记住;第二问"写进人格吗" -> 写进人格
            value = "写进人格" if "人格" in question.prompt else "记住"
            return Answer(question_id=question.id, status=STATUS_ANSWERED,
                          selected=[value], by="user")

        session = MemorySession(
            FakeClient(), make_settings(), store=self.store, session_id="s1",
            assistant_conn=self.assistant, auto_briefing=False, asker=asker,
            auto_approve=True, direct_write=True,
        )
        from la.base.phases import CircuitBreaker, PHASE_ANSWER

        result = await session.registry.execute(
            "memory_note",
            {"content": "助手的英文名是 Mo", "fact_type": "identity"},
            breaker=CircuitBreaker(phase=PHASE_ANSWER), session_id="s1",
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.data["status"], "persona", result.data)
        self.assertTrue(result.data["written"], result.data)
        # 人格页里有了;事实层**一条都没写**
        self.assertIn("英文名是 Mo", PersonaStore(self.assistant).state().body)
        count = self.memory_conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(int(count), 0, "助手的事不该写进事实层")

    async def test_persona_note_tool_takes_the_same_route(self):
        async def asker(question):
            return Answer(question_id=question.id, status=STATUS_ANSWERED,
                          selected=["写进人格"], by="user")

        session = MemorySession(
            FakeClient(), make_settings(), store=self.store, session_id="s1",
            assistant_conn=self.assistant, auto_briefing=False, asker=asker,
            auto_approve=True, direct_write=True,
        )
        from la.base.phases import CircuitBreaker, PHASE_ANSWER

        result = await session.registry.execute(
            "persona_note",
            {"text": "说话再短一点,能一句说完就别写第二句"},
            breaker=CircuitBreaker(phase=PHASE_ANSWER), session_id="s1",
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.data["status"], "persona", result.data)
        self.assertIn("说话再短一点", PersonaStore(self.assistant).state().body)




class ReviewPageCase(unittest.TestCase):
    """记忆页:待放行的那几行**有按钮**,点下去真的写/真的拒(2026-10-25)。"""

    PATHS = ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR", "LOGS_DIR",
             "ASSISTANT_DB", "MEMORY_DB", "RUNTIME_DIRS")

    def setUp(self) -> None:
        import os

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved = {name: getattr(config, name) for name in self.PATHS}
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.LOGS_DIR = self.tmp / "logs"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config.RUNTIME_DIRS = (self.tmp, config.ARCHIVE_DIR, config.MEDIA_DIR,
                               config.ARTIFACTS_DIR, config.LOGS_DIR)
        self._settings = config._settings
        config._settings = None
        self._env = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = ""

        conn = db.connect(config.MEMORY_DB)
        db.migrate(conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(conn, archive_dir=config.ARCHIVE_DIR)
        self.candidate_id = self.store.add_candidate(
            "用户养了一只猫", source="memory_note", session_id="web",
        )
        self.store.set_candidate_status(
            self.candidate_id, "review", reason="输出不是 JSON:(裁判判不准)",
        )
        conn.close()

        from fastapi.testclient import TestClient

        from la.web.app import create_app

        self.client = TestClient(create_app(), follow_redirects=False)

    def tearDown(self) -> None:
        import os

        for name, value in self._saved.items():
            setattr(config, name, value)
        config._settings = self._settings
        if self._env is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = self._env
        self._tmp.cleanup()

    def _facts(self) -> list[str]:
        conn = db.connect(config.MEMORY_DB)
        try:
            return [str(row[0]) for row in conn.execute(
                "SELECT content FROM facts WHERE active = 1 ORDER BY id"
            )]
        finally:
            conn.close()

    def _candidate(self) -> dict:
        conn = db.connect(config.MEMORY_DB)
        conn.row_factory = lambda cursor, row: {
            column[0]: row[index] for index, column in enumerate(cursor.description)
        }
        try:
            return dict(conn.execute(
                "SELECT * FROM fact_candidates WHERE id = ?", (self.candidate_id,)
            ).fetchone())
        finally:
            conn.close()

    def test_page_shows_the_two_buttons_only_for_review_rows(self):
        page = self.client.get("/memory")
        self.assertEqual(page.status_code, 200)
        body = page.text
        self.assertIn("待人工放行", body)
        self.assertIn(
            'action="/memory/candidates/%d/approve"' % self.candidate_id, body,
            "网页端没有放行按钮(用户报的第二件事)",
        )
        self.assertIn('action="/memory/candidates/%d/reject"' % self.candidate_id, body)
        self.assertIn(">放行</button>", body)
        self.assertIn(">拒收</button>", body)

    def test_approve_button_writes_the_fact_and_a_receipt(self):
        response = self.client.post(
            "/memory/candidates/%d/approve" % self.candidate_id,
            data={"_idem": "approve-1"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("flash=", response.headers["location"])
        self.assertEqual(self._facts(), ["用户养了一只猫"])
        self.assertEqual(self._candidate()["status"], "admitted")
        conn = db.connect(config.MEMORY_DB)
        try:
            kinds = [str(row[0]) for row in conn.execute(
                "SELECT kind FROM ops_receipts ORDER BY id"
            )]
        finally:
            conn.close()
        self.assertIn("memory.fact", kinds, "放行没有落回执")
        # 按钮点完之后页面就不该再有这一行了(已经处理过)
        self.assertNotIn(
            'action="/memory/candidates/%d/approve"' % self.candidate_id,
            self.client.get("/memory").text,
        )

    def test_reject_button_writes_nothing(self):
        response = self.client.post(
            "/memory/candidates/%d/reject" % self.candidate_id,
            data={"_idem": "reject-1", "reason": "这句不对"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self._facts(), [])
        self.assertEqual(self._candidate()["status"], "rejected")


if __name__ == "__main__":
    unittest.main()