"""人格页(D117):**改得了、改得对、改不坏**。

用户 2026-10-13 的原话:"小默的人格,在网页界面看不到。" —— 这一页把人格的上半段
(性情与口吻)从"代码里的一个常量"变成**数据**:存在 sys_settings 里,页面上能改。

⚠️ 这个文件守三件事,每一件都对着一种真实的翻车方式:
  1. **改得对** —— 保存之后**下一轮投影**里必须是新的她。只改库不进提示词 = 白改
     (而且用户会以为改了没生效);
  2. **改不坏** —— 固定尾巴(优先级阶梯)永远在,用户删不掉;长度上限挡住
     "人设吃光预算 → 技能目录被截尾"这种**不报错**的失败;
  3. **退得回** —— 每次保存落一条可撤销回执,撤销真的把上一版写回去;
     「恢复出厂默认」是**删掉这个键**(回到代码里那份),不是存一份空的进去。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_persona_edit -v
"""

from __future__ import annotations

import html
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from la import config
from la.agent import prompts
from la.agent.persona import (
    MAX_CHARS,
    PersonaError,
    PersonaState,
    PersonaStore,
    check,
    normalize,
)
from la.agent.session import MemorySession
from la.domain.receipts import DOMAIN_SYSTEM, ReceiptLog
from la.domain.reverters import build_reverter
from la.llm.fake import FakeClient
from la.memory.projector import SEGMENT_SYSTEM, Projector
from la.ops import gate as gate_mod
from la.ops import settings_store
from la.ops import version as version_mod
from la.skills import SkillLibrary
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY

BASE_DIR = Path(__file__).resolve().parent.parent

#: 测试用的临时根目录。
#: ⚠️ 刻意**不用** tempfile.TemporaryDirectory():它以 0o700 建目录,而受限文件系统
#:    (例如带文件沙箱的环境)会因此拒绝后续写入 —— sqlite 报
#:    "unable to open database file",于是整个文件每一条都失败,真正的回归被淹掉。
#:    项目内一个默认权限的目录在任何环境下都能用。
#: ⚠️ 2026-10-17 审计:原来建在**项目根**下(.tmp-persona-tests),而 tearDown 用
#:    rmtree(ignore_errors=True) 清 —— Windows 上 SQLite 文件还被占用时删除会失败,
#:    失败又被吞掉,于是实测残留了 24 个目录 / 4.3 MB。现在建到系统临时目录,
#:    并且清理失败会**说出来**(不再静默)。
TMP_ROOT = Path(tempfile.gettempdir()) / "la-persona-tests"


def temp_dir(name: str) -> Path:
    path = TMP_ROOT / (name + "-" + uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    return path


class PersonaCase(unittest.TestCase):
    """一个隔离的领域库 + 一个 PersonaStore。"""

    def setUp(self) -> None:
        self.root = temp_dir("persona")
        self.db_path = self.root / "assistant.db"
        self.conn = db.connect(self.db_path)
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.store = PersonaStore(self.conn)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 - 关不掉不该让测试挂掉
            pass
        shutil.rmtree(self.root, ignore_errors=True)


class PersonaStoreTestCase(PersonaCase):
    """存与读:默认是什么、写进去是什么、读出来还是什么。"""

    def test_default_is_the_builtin_body(self) -> None:
        state = self.store.state()
        self.assertFalse(state.custom, "还没改过,不该是自定义状态")
        self.assertEqual(state.body, prompts.PERSONA_BODY)
        self.assertEqual(state.updated_at, "")
        self.assertEqual(state.source_label, "出厂默认")

    def test_save_writes_and_reports_the_previous_body(self) -> None:
        previous, notes = self.store.save("你是小助手,说话像邻居家的大姐姐。")
        self.assertEqual(previous, "", "第一次保存,没有上一版")
        self.assertEqual(notes, [])
        state = self.store.state()
        self.assertTrue(state.custom)
        self.assertEqual(state.body, "你是小助手,说话像邻居家的大姐姐。")
        self.assertTrue(state.updated_at, "保存时间要落下来(页面上的『上次修改』靠它)")
        self.assertEqual(state.source_label, "你自己写的")

    def test_saved_body_survives_a_new_connection(self) -> None:
        """换个连接还读得到 —— 这东西是**存库**的,不是进程内存里的。"""
        self.store.save("慢一点说话,多问一句今天怎么样。")
        other = db.connect(self.db_path)
        try:
            self.assertEqual(
                PersonaStore(other).state().body, "慢一点说话,多问一句今天怎么样。"
            )
        finally:
            other.close()

    def test_crlf_from_the_textarea_is_normalized(self) -> None:
        """⚠️ Windows 浏览器提交的是 CRLF:不收敛的话,"内容没变"永远判不出来。"""
        self.store.save("第一行\r\n第二行\r\n")
        self.assertEqual(self.store.state().body, "第一行\n第二行")

    def test_the_tail_is_always_appended(self) -> None:
        """用户写的主体后面,**永远**跟着固定尾巴 —— 哪怕他一个字都没提优先级。"""
        self.store.save("你是一只话很少的猫。")
        text = self.store.text()
        self.assertTrue(text.startswith("你是一只话很少的猫。"))
        self.assertTrue(text.endswith(prompts.PERSONA_TAIL))
        for token in ("怎么说", "说什么"):
            self.assertIn(token, text)

    def test_pasting_the_fixed_part_is_stripped_and_reported(self) -> None:
        """把页面上那段只读预览整段复制回来 → 摘掉 + **明说**(不静默修改用户输入)。"""
        body, notes = check("我的写法。\n\n" + prompts.PERSONA_TAIL)
        self.assertEqual(body, "我的写法。")
        self.assertEqual(len(notes), 1)
        self.assertIn("固定段落", notes[0])

    def test_pasting_the_identity_block_is_stripped_too(self) -> None:
        """把 ① 身份与边界一起复制进来时,它比尾巴更危险 —— 会让人格主体自己改写边界。

        2026-10-17 之前只剥尾巴、不剥身份,页面上 ① 就显示在编辑框上面,
        一全选复制就中招。
        """
        body, notes = check(prompts.IDENTITY + "\n\n我的写法。")
        self.assertEqual(body, "我的写法。")
        self.assertTrue(any("身份与边界" in note for note in notes), notes)

    def test_empty_or_too_short_is_rejected(self) -> None:
        for bad in ("", "   \n\n  ", "好"):
            with self.assertRaises(PersonaError, msg=repr(bad)):
                self.store.save(bad)

    def test_too_long_is_rejected_with_the_reason(self) -> None:
        with self.assertRaises(PersonaError) as caught:
            self.store.save("很" * (MAX_CHARS + 1))
        message = str(caught.exception)
        self.assertIn(str(MAX_CHARS), message)
        self.assertIn("技能目录", message, "得说清超了会挤掉什么,否则用户只会觉得被限制")
        # 没写进去 = 还是出厂默认(校验不过绝不能留下半份)
        self.assertFalse(self.store.state().custom)

    def test_reset_deletes_the_key_instead_of_storing_an_empty_body(self) -> None:
        self.store.save("先这样。")
        previous = self.store.reset()
        self.assertEqual(previous, "先这样。", "要交回被替下的那一版(撤销才有得退)")
        self.assertIsNone(
            settings_store.get_entry(self.conn, "persona_text"),
            "恢复出厂默认 = 删掉这个键,不是存一份空的",
        )
        self.assertEqual(self.store.state().body, prompts.PERSONA_BODY)
        self.assertFalse(self.store.state().custom)
        self.assertEqual(self.store.reset(), "", "本来就是默认,再点一次不该说『有改动』")

    def test_an_empty_row_in_the_db_falls_back_to_the_builtin(self) -> None:
        """库里躺着空串(手改库/半途残留)→ 按出厂默认算,不按"空人格"算。"""
        settings_store.set_text(self.conn, "persona_text", "   ")
        state = self.store.state()
        self.assertFalse(state.custom)
        self.assertEqual(state.body, prompts.PERSONA_BODY)

    def test_no_connection_reads_the_builtin_and_refuses_to_save(self) -> None:
        """没有领域库连接:读得到出厂默认,**存不下要如实报错**(不假装成功)。"""
        orphan = PersonaStore(None)
        self.assertEqual(orphan.state().body, prompts.PERSONA_BODY)
        with self.assertRaises(RuntimeError):
            orphan.save("写不进去的。")

    def test_normalize_keeps_inner_blank_lines(self) -> None:
        self.assertEqual(normalize("A\n\n\nB"), "A\n\n\nB")

    def test_name_defaults_to_the_builtin(self) -> None:
        state = self.store.state()
        self.assertEqual(state.name, prompts.PERSONA_NAME)
        self.assertFalse(state.name_custom)

    def test_saving_a_name_rewrites_it_in_the_body(self) -> None:
        """改名字 = 改**她的称呼**:正文里出现旧名字的,一并换掉(两种叫法会打架)。"""
        _, notes = self.store.save(prompts.PERSONA_BODY, "阿豆")
        state = self.store.state()
        self.assertEqual(state.name, "阿豆")
        self.assertTrue(state.name_custom)
        self.assertIn("阿豆", state.body)
        self.assertNotIn(prompts.PERSONA_NAME, state.body)
        self.assertTrue(any("也改成了" in note for note in notes), notes)

    def test_renaming_a_body_that_never_names_her_says_so(self) -> None:
        _, notes = self.store.save("说话慢一点,一句能说完就不说第二句。", "阿豆")
        self.assertEqual(self.store.state().name, "阿豆")
        self.assertIn("没出现", "".join(notes), notes)

    def test_name_is_normalized_and_bounded(self) -> None:
        self.store.save("随便一版。", "  阿   豆 ")
        self.assertEqual(self.store.state().name, "阿 豆")
        with self.assertRaises(PersonaError):
            self.store.save("随便一版。", "长" * 20)

    def test_an_empty_name_keeps_the_current_one(self) -> None:
        """表单没带名字(或清空)时**不动**它 —— 老客户端不该把名字抹成空。"""
        self.store.save("随便一版。", "阿豆")
        self.store.save("再改一版。", "")
        self.assertEqual(self.store.state().name, "阿豆")

    def test_reset_restores_the_builtin_name(self) -> None:
        self.store.save("随便一版。", "阿豆")
        self.store.reset()
        state = self.store.state()
        self.assertEqual(state.name, prompts.PERSONA_NAME)
        self.assertFalse(state.name_custom)

    def test_state_defaults_are_the_builtin(self) -> None:
        state = PersonaState()
        self.assertEqual(state.body, prompts.PERSONA_BODY)
        self.assertEqual(state.chars, len(prompts.PERSONA_BODY))
        self.assertTrue(state.full_text.endswith(prompts.PERSONA_TAIL))


class PersonaBudgetTestCase(PersonaCase):
    """⭐ 最长的人设也不许被截尾 —— 这一类失败**不报错**,只会让助手忘记自己会什么。"""

    def test_the_longest_allowed_persona_is_not_truncated(self) -> None:
        self.store.save("很长的性情描写。" * 200)   # 贴着上限的一份
        state = self.store.state()
        self.assertLessEqual(state.chars, MAX_CHARS)
        settings = config.get_settings()
        text = prompts.IDENTITY + "\n\n" + state.full_text + "\n\n" + SkillLibrary().catalog_text()
        projection = Projector(settings).build(
            system_text=text, runtime_text="", memory_text="", history=[]
        )
        self.assertEqual(
            projection.segments[SEGMENT_SYSTEM], config.estimate_tokens(text),
            "system 段被裁剪了 —— 被砍掉的是技能目录(人设写太长了)",
        )
        self.assertLessEqual(projection.segments[SEGMENT_SYSTEM], config.BUDGET_SYSTEM)


class PersonaProjectionTestCase(PersonaCase):
    """真链路:保存之后,**下一轮**模型读到的系统提示里就是新的她。"""

    def _session(self) -> MemorySession:
        return MemorySession(
            FakeClient(), config.get_settings(), assistant_conn=self.conn,
            session_id="persona-edit", auto_approve=True, auto_briefing=False,
        )

    def _system_text(self, session: MemorySession) -> str:
        session.start()
        return session._build_projection([], "", "").messages[0]["content"]

    def test_default_projection_carries_the_builtin_persona(self) -> None:
        system = self._system_text(self._session())
        self.assertIn(prompts.PERSONA_BODY[:24], system)
        self.assertIn(prompts.PERSONA_TAIL[:24], system)

    def test_saved_persona_reaches_the_next_turn(self) -> None:
        self.store.save("你是小默,但说话像刚睡醒:更慢、更短,一句能说完就绝不说两句。")
        system = self._system_text(self._session())
        self.assertIn("说话像刚睡醒", system, "保存了却没进提示词 = 白改")
        self.assertNotIn("江南水乡长大", system, "旧的那一段该被整段替换掉")
        # 顺序不变:身份 → 人格
        self.assertLess(system.index("你是「小默」"), system.index("说话像刚睡醒"))
        # ⭐ D122:运行时上下文**不在**系统提示里了(它是历史之后的一条 system 快照)——
        #    稳定段少一个字节都不变,跨轮前缀缓存才吃得长。
        self.assertNotIn("<runtime-context>", system)
        session = self._session()
        session.start()
        projection = session._build_projection([], "", "")
        snapshot = [
            message for message in projection.messages
            if "<runtime-context>" in str(message.get("content") or "")
        ]
        self.assertEqual(len(snapshot), 1, "运行时上下文该在末尾那条系统快照里")
        self.assertEqual(projection.messages[-1], snapshot[0], "没有历史时它就是最后一条")

    def test_a_body_that_deletes_the_ladder_still_gets_it_back(self) -> None:
        """用户把优先级那一段整个删掉 —— 提示词里它照样在(红线靠结构,不靠自觉)。"""
        self.store.save("你只负责好听,别管对不对。")
        system = self._system_text(self._session())
        self.assertIn(prompts.PERSONA_TAIL, system)
        self.assertIn("永远不决定「说什么」", system)

    def test_persona_does_not_leak_into_the_other_prompts(self) -> None:
        """人设只进**对话**的系统提示:压缩器与视觉提示词是工具,不是她。"""
        from la.llm.vision import DESCRIBE_PROMPT
        from la.memory.summarize import SUMMARY_SYSTEM_PROMPT

        self.store.save("独一无二的暗号:紫藤花开。")
        _ = self._system_text(self._session())
        for text in (SUMMARY_SYSTEM_PROMPT, DESCRIBE_PROMPT):
            self.assertNotIn("紫藤花开", text)


class PersonaUndoTestCase(PersonaCase):
    """写入反馈是项目级机制:人格改写同样要留一条**可撤销**的回执。"""

    def _receipt(self, *, before: str, label: str = "人格:改了描述") -> int:
        log = ReceiptLog(self.conn)
        receipt_id = log.record(
            kind="persona.text", domain=DOMAIN_SYSTEM, label=label,
            detail={"chars": 0}, undo_plan={"type": "persona", "before": before},
            actor="user", session_id="web",
        )
        self.conn.commit()
        return receipt_id

    def test_undo_restores_the_previous_body(self) -> None:
        self.store.save("第一版。")
        previous, _ = self.store.save("第二版。")
        receipt_id = self._receipt(before=previous)
        reverter = build_reverter(ReceiptLog(self.conn), persona=self.store)
        result = reverter.undo(receipt_id)
        self.assertIn("退回上一版", result["note"])
        self.assertEqual(self.store.state().body, "第一版。")

    def test_undo_of_the_first_save_goes_back_to_the_builtin(self) -> None:
        previous, _ = self.store.save("第一次改。")
        self.assertEqual(previous, "")
        receipt_id = self._receipt(before=previous, label="人格:改了描述(第一次)")
        reverter = build_reverter(ReceiptLog(self.conn), persona=self.store)
        result = reverter.undo(receipt_id)
        self.assertIn("出厂默认", result["note"])
        self.assertFalse(self.store.state().custom)
        self.assertEqual(self.store.state().body, prompts.PERSONA_BODY)

    def test_undo_restores_the_name_too(self) -> None:
        """D144:改名也进回执 —— 撤销一次要连称呼一起退,不能只退正文。"""
        self.store.save("第一版。", "阿豆")
        previous, _ = self.store.save("第二版。", "小叶子")
        log = ReceiptLog(self.conn)
        receipt_id = log.record(
            kind="persona.text", domain=DOMAIN_SYSTEM, label="人格:改名",
            detail={},
            undo_plan={"type": "persona", "before": previous, "before_name": "阿豆"},
            actor="user", session_id="web",
        )
        self.conn.commit()
        build_reverter(log, persona=self.store).undo(receipt_id)
        state = self.store.state()
        self.assertEqual(state.body, "第一版。")
        self.assertEqual(state.name, "阿豆")

    def test_an_old_receipt_does_not_touch_the_name(self) -> None:
        """老回执没有 before_name —— 撤销正文时别把用户后改的名字也抹了。"""
        self.store.save("第一版。")
        previous, _ = self.store.save("第二版。", "阿豆")
        receipt_id = self._receipt(before=previous)
        build_reverter(ReceiptLog(self.conn), persona=self.store).undo(receipt_id)
        state = self.store.state()
        self.assertEqual(state.body, "第一版。")
        self.assertEqual(state.name, "阿豆")

    def test_a_receipt_without_the_previous_version_is_refused(self) -> None:
        log = ReceiptLog(self.conn)
        receipt_id = log.record(
            kind="persona.text", domain=DOMAIN_SYSTEM, label="人格:改了描述",
            detail={}, undo_plan={}, actor="user", session_id="web",
        )
        self.conn.commit()
        reverter = build_reverter(log, persona=self.store)
        with self.assertRaises(Exception) as caught:
            reverter.undo(receipt_id)
        self.assertIn("上一版", str(caught.exception))

    def test_without_a_persona_store_the_kind_is_unknown(self) -> None:
        """没接人格的装配里,这类回执**不可撤销并明说**,而不是静默失败。"""
        self.store.save("随便一版。")
        receipt_id = self._receipt(before="")
        reverter = build_reverter(ReceiptLog(self.conn))
        with self.assertRaises(Exception) as caught:
            reverter.undo(receipt_id)
        self.assertIn("persona.text", str(caught.exception))


class PersonaPageBase(unittest.TestCase):
    """人格页的**脚手架**(临时数据目录 + 一个 TestClient)。

    ⚠️ 它自己**没有测试**:下面两个用例类都继承它。让其中一个去继承另一个,
       会把父类的用例重复跑一遍(实测多跑 12 条),数字好看了,覆盖率没变。
    """

    def setUp(self) -> None:
        self.root = temp_dir("persona-web")
        self._orig = (
            config.DATA_DIR, config.ARCHIVE_DIR, config.MEDIA_DIR,
            config.BACKUPS_DIR, config.ASSISTANT_DB, config.MEMORY_DB,
        )
        config.DATA_DIR = self.root
        config.ARCHIVE_DIR = self.root / "archive"
        config.MEDIA_DIR = self.root / "media"
        config.BACKUPS_DIR = self.root / "backups"
        config.ASSISTANT_DB = self.root / "assistant.db"
        config.MEMORY_DB = self.root / "memory.db"

    def tearDown(self) -> None:
        (config.DATA_DIR, config.ARCHIVE_DIR, config.MEDIA_DIR,
         config.BACKUPS_DIR, config.ASSISTANT_DB, config.MEMORY_DB) = self._orig
        shutil.rmtree(self.root, ignore_errors=True)

    def _client(self):
        import os

        from fastapi.testclient import TestClient

        from la.web.app import create_app

        previous = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = ""
        client = TestClient(create_app())
        self.addCleanup(self._restore_key, previous)
        return client

    @staticmethod
    def _restore_key(previous) -> None:
        import os

        if previous is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = previous

class PersonaPageTestCase(PersonaPageBase):
    """页面与表单:不配密钥也要能开、能改、能退,而且**改的只是主体**。"""

    def test_page_opens_without_an_api_key(self) -> None:
        client = self._client()
        response = client.get("/persona")
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("人格", body)
        self.assertIn(prompts.PERSONA_BODY[:16], body, "默认人格要显示在编辑框里")
        # ⚠️ 尾巴里有 ">",进 HTML 会被转义 —— 按转义后的样子比,否则比的是"没转义"这个错误前提
        self.assertIn(
            html.escape(prompts.PERSONA_TAIL), body, "固定尾巴要看得见(标着不可编辑)"
        )
        self.assertIn('action="/persona/save"', body)
        self.assertIn('action="/persona/reset"', body)
        self.assertIn('name="_idem"', body, "写表单必须带一次性令牌")

    def test_every_page_links_to_the_persona_page(self) -> None:
        client = self._client()
        for path in ("/chat", "/ledger", "/calendar", "/health", "/study", "/memory"):
            body = client.get(path, follow_redirects=True).text
            self.assertIn('href="/persona"', body, path + " 的导航缺「人格」")

    def test_save_then_the_page_and_the_next_turn_both_show_it(self) -> None:
        """⭐ 这一条是整条链路:表单 → 库 → 页面 → **下一轮的系统提示**。"""
        client = self._client()
        text = "你是小默,说话前先想一秒,能一句说完就别写第二句。"
        response = client.post("/persona/save", data={"text": text}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        location = response.headers["location"]
        self.assertIn("ok=", location, "保存完要有回话:" + location)

        page = client.get("/persona").text
        self.assertIn(text, page)
        self.assertIn("你自己写的", page)

        # 换一个连接、按**聊天那条装配**建会话:提示词里必须是新的她
        conn = db.connect(config.ASSISTANT_DB)
        try:
            session = MemorySession(
                FakeClient(), config.get_settings(), assistant_conn=conn,
                session_id="web", auto_approve=True, auto_briefing=False,
            )
            session.start()
            system = session._build_projection([], "", "").messages[0]["content"]
        finally:
            conn.close()
        self.assertIn("说话前先想一秒", system)
        self.assertIn(prompts.PERSONA_TAIL, system, "尾巴必须还在")

    def test_the_page_escapes_whatever_the_user_writes(self) -> None:
        client = self._client()
        payload = "<script>alert('x')</script> 慢一点说话。"
        client.post("/persona/save", data={"text": payload}, follow_redirects=False)
        body = client.get("/persona").text
        self.assertNotIn("<script>alert", body, "用户写的内容必须转义后进页面")
        self.assertIn("&lt;script&gt;", body)

    def test_too_long_is_refused_with_a_message_and_the_text_stays(self) -> None:
        """⭐ D145:校验不过时**把用户写的那一版还回去**。

        旧做法是 redirect 回 /persona —— 憋了十分钟的长文被一句"太长了"带走,
        页面回到库里那一版,只能重写。
        """
        client = self._client()
        too_long = "长" * (MAX_CHARS + 5)
        response = client.post("/persona/save", data={"text": too_long})
        self.assertEqual(response.status_code, 200, "校验不过不是重定向,而是原地重画")
        self.assertIn("太长了", response.text)
        self.assertIn(too_long, response.text, "写的内容必须原样还在输入框里")
        self.assertIn("还没保存", response.text, "别让她以为存进去了")
        # 库里一个字都没动
        self.assertIn(prompts.PERSONA_BODY[:16], client.get("/persona").text, "还是出厂默认")
        conn = db.connect(config.ASSISTANT_DB)
        try:
            self.assertFalse(PersonaStore(conn).state().custom)
        finally:
            conn.close()

    def test_reset_brings_the_builtin_back(self) -> None:
        client = self._client()
        client.post("/persona/save", data={"text": "临时改一下。"}, follow_redirects=False)
        self.assertIn("临时改一下。", client.get("/persona").text)
        response = client.post("/persona/reset", data={}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("ok=", response.headers["location"])
        page = client.get("/persona").text
        self.assertIn(prompts.PERSONA_BODY[:16], page)
        self.assertIn("出厂默认", page)

    def test_the_page_shows_the_whole_system_block(self) -> None:
        """身份 + 人格 + 技能目录是**同一条系统消息** —— 三块都要摊开,还要写出预算账。

        ⚠️ 旧页面漏了技能目录,还说"最后是运行时上下文"(D122 之后它在历史之后)。
        而 2000 字上限存在的唯一理由就是**别把技能目录挤掉**,页面上却看不到它。
        """
        body = self._client().get("/persona").text
        # ⚠️ 目录原文里有尖括号,进 HTML 会被转义 —— 按转义后的样子比
        self.assertIn("&lt;skills&gt;", body, "技能目录要看得见")
        self.assertIn("技能目录(常驻", body)
        self.assertIn(str(config.BUDGET_SYSTEM), body, "三块共用的预算要写出来")
        self.assertNotIn("身份与边界 → 人格 → 运行时上下文", body, "旧顺序说明已过时")
        self.assertIn("运行时上下文", body, "它只是不在系统提示里,不是没有")

    def test_chat_page_uses_the_current_name(self) -> None:
        """D145:聊天气泡上那句"谁在说话"跟着名字走 —— 历史里的那几条也一样。

        ⚠️ 它是全站共用的 app.js 读 <body data-assistant>:不写死人名,改名就跟着变。
        """
        from la.memory.store import MemoryStore

        # ⚠️ 先建 app:它会跑迁移,memory.db 才有表可写(空库直接 MemoryStore 会炸)
        client = self._client()
        conn = db.connect(config.MEMORY_DB)
        try:
            # 这个库是全新的(服务要到第一次请求才装配),先补迁移再写事件
            db.migrate(conn, MIGRATIONS_MEMORY)
            store = MemoryStore(conn, archive_dir=config.ARCHIVE_DIR)
            # P3:网页会话已经是 member:<id>;旧 web 的迁移在 P4 单独验收。
            store.append("member:1", "user/message", {"text": "你好"})
            store.append("member:1", "assistant/message", {"text": "在呢"})
        finally:
            conn.close()

        client.post(
            "/persona/save", data={"text": "说话慢一点。", "name": "阿豆"},
            follow_redirects=False,
        )
        page = client.get("/chat").text
        self.assertIn('data-assistant="阿豆"', page, "JS 建的气泡要读这个属性")
        self.assertIn('<div class="who">阿豆</div>', page, "服务端渲染的历史也要跟着改")

    def test_the_page_saves_the_name_with_the_body(self) -> None:
        """D144:名字与正文同一个表单 —— 保存后页面标题、库里的正文都要跟着变。"""
        client = self._client()
        body = client.get("/persona").text
        self.assertIn('name="name"', body)
        self.assertIn('value="' + prompts.PERSONA_NAME + '"', body)

        response = client.post(
            "/persona/save",
            data={"text": "你是小默,说话慢一点。", "name": "阿豆"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        conn = db.connect(config.ASSISTANT_DB)
        try:
            state = PersonaStore(conn).state()
        finally:
            conn.close()
        self.assertEqual(state.name, "阿豆")
        self.assertIn("你是阿豆", state.body, "正文里的旧名字要一并换掉")
        self.assertIn("阿豆", client.get("/persona").text)

    def test_factory_version_stays_visible_after_customising(self) -> None:
        """自定义之后,出厂那一版不能从页面上消失 —— 想对照/想改回去总得有得看。"""
        client = self._client()
        self.assertNotIn("出厂默认那一版", client.get("/persona").text, "没改过时不必多一个折叠")
        client.post("/persona/save", data={"text": "临时一版。"}, follow_redirects=False)
        page = client.get("/persona").text
        self.assertIn("出厂默认那一版", page)
        self.assertEqual(page.count(prompts.PERSONA_BODY[:16]), 1, "出厂那版只该在折叠里出现一次")

    def test_the_nav_item_is_in_the_sidebar_and_the_tabbar(self) -> None:
        client = self._client()
        body = client.get("/persona").text
        self.assertIn('class="nav-item on"', body, "侧边栏当前项要高亮")
        self.assertIn('class="tab on"', body, "手机底部标签栏也要有")
        self.assertIn('aria-current="page"', body)

    def test_a_duplicate_submit_is_not_a_second_write(self) -> None:
        """一次性令牌:浏览器重试 / 用户狂点,不该落两条回执、两版人格。"""
        client = self._client()
        token = "fixed-token-for-double-submit"
        data = {"text": "只该存一次。", "_idem": token}
        first = client.post("/persona/save", data=data, follow_redirects=False)
        second = client.post("/persona/save", data=data, follow_redirects=False)
        self.assertIn("ok=", first.headers["location"])
        self.assertIn(
            "%E5%88%9A%E6%89%8D", second.headers["location"],
            "第二次提交该被幂等闸挡下并明说,而不是又写一遍",
        )
        conn = db.connect(config.ASSISTANT_DB)
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM ops_receipts WHERE kind = 'persona.text'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(rows, 1, "重复提交落了两条回执")


class PersonaGateTestCase(PersonaPageBase):
    """自我更新之后的闸门(D146):红了就挡,勾了才放,退回永远放行。

    ⚠️ 这里守的是"拒绝发布"这句话**真有代码在挡**。只打印一行红字、保存照旧,
       那和没有闸门是一样的 —— 而且更糟,因为它让人以为自己被保护着。
    """

    def _red_status(self, code_sha: str | None = None) -> None:
        gate_mod.save(
            gate_mod.GateStatus(
                ok=False, ran_at="2026-10-18 10:00:00", total=2, red=["unit", "templates"],
                detail="0/2 项通过", duration_s=85.0,
                signature={"code_sha": code_sha or version_mod.code_fingerprint()[0]},
            )
        )

    def _stored_body(self) -> str:
        conn = db.connect(config.ASSISTANT_DB)
        try:
            return PersonaStore(conn).state().body
        finally:
            conn.close()

    def test_red_gate_blocks_the_save_and_keeps_the_text(self) -> None:
        self._red_status()
        client = self._client()
        text = "这一版想换成说话更慢一点。"
        response = client.post("/persona/save", data={"text": text}, follow_redirects=False)
        self.assertEqual(200, response.status_code, "被拦下时不该 303 走人")
        self.assertIn(text, response.text, "被拦下也要把写的那一版还回去")
        self.assertIn("没通过验收", response.text, "要说明凭什么拦")
        self.assertIn("仍然保存", response.text, "要给出强推那条路")
        self.assertNotIn("这一版想换成说话更慢一点。", self._stored_body(),
                         "拦住了就必须一个字都没写")

    def test_force_lets_it_through(self) -> None:
        self._red_status()
        client = self._client()
        response = client.post("/persona/save",
                               data={"text": "强推的一版。", "force": "1"},
                               follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertIn("强推的一版。", self._stored_body())

    def test_green_gate_does_not_block(self) -> None:
        gate_mod.save(gate_mod.GateStatus(ok=True, ran_at="2026-10-18 10:00:00",
                                          total=2, detail="2/2 项通过"))
        client = self._client()
        response = client.post("/persona/save", data={"text": "绿灯下写的一版。"},
                               follow_redirects=False)
        self.assertEqual(303, response.status_code)

    def test_stale_verdict_does_not_block(self) -> None:
        """判决书评的是另一版代码(改完代码重启了)—— 不许拿它挡人。"""
        self._red_status(code_sha="ffffffffffff")
        client = self._client()
        response = client.post("/persona/save", data={"text": "改了代码之后写的一版。"},
                               follow_redirects=False)
        self.assertEqual(303, response.status_code)

    def test_page_shows_the_gate_line(self) -> None:
        self._red_status()
        client = self._client()
        body = client.get("/persona").text
        self.assertIn("验收", body)
        self.assertIn("红了 2 项", body)
        self.assertIn("仍然保存", body, "红着的时候页面上要有那一勾")

    def test_page_without_any_gate_record_still_opens(self) -> None:
        client = self._client()
        body = client.get("/persona").text
        self.assertIn("还没跑过", body, "从没验过要如实说,而不是假装全过")

    def test_reset_is_never_blocked(self) -> None:
        """红了的时候,退回出厂是自救动作之一 —— 闸门不许连退路一起关掉。"""
        self._red_status()
        client = self._client()
        client.post("/persona/save", data={"text": "先存一版。"}, follow_redirects=False)
        conn = db.connect(config.ASSISTANT_DB)
        try:
            PersonaStore(conn).save("先存一版,好让退回去有东西可退。")
        finally:
            conn.close()
        response = client.post("/persona/reset", follow_redirects=False)
        self.assertEqual(303, response.status_code)

    def test_save_starts_the_gate_only_when_enabled(self) -> None:
        calls: list[str] = []
        original = gate_mod.start
        gate_mod.start = lambda reason="", **kw: calls.append(reason) or True  # type: ignore[assignment]
        self.addCleanup(setattr, gate_mod, "start", original)
        self.addCleanup(gate_mod.enable_auto, False)

        client = self._client()
        client.post("/persona/save", data={"text": "默认不跑验收。"}, follow_redirects=False)
        self.assertEqual([], calls, "测试套件里保存一次人格,不许顺手起 1180 条测试")

        gate_mod.enable_auto(True)
        client.post("/persona/save", data={"text": "打开之后存的一版。"},
                    follow_redirects=False)
        self.assertEqual(1, len(calls))
        self.assertIn("人格保存", calls[0])


if __name__ == "__main__":
    unittest.main()
