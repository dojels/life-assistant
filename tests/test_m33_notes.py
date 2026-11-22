"""M33 笔记域:夹树、笔记、版本、回执撤销、检索。

跑法:
    .venv/Scripts/python.exe -m unittest tests.test_m33_notes -v

这里钉的是**用户能感觉到**的那几条:
  * 说了"记一下"就真的写进去(收件箱兜底,不逼人先选夹);
  * 夹不存在时**不写**,而是给候选(宁可不记,也不能悄悄记错地方);
  * 删是软删、改留旧版 —— 手写的东西丢了最痛;
  * 每一条写入都能撤销,且撤一次就够(再撤要报错)。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from la import config
from la.base.phases import CircuitBreaker
from la.domain import ownership
from la.domain.receipts import ReceiptLog
from la.domain.reverters import build_reverter
from la.domain.services.notes_service import INBOX_PATH, NotesError, NotesService
from la.domain.tools import build_note_tools
from la.storage import db


class NotesCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
                        "ASSISTANT_DB", "MEMORY_DB")
        }
        self._env = {key: os.environ.get(key) for key in ("LA_LLM_PROVIDER", "DEEPSEEK_API_KEY")}
        os.environ["LA_LLM_PROVIDER"] = "fake"
        os.environ["DEEPSEEK_API_KEY"] = ""
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config._settings = None

        self.conn = db.connect_assistant()
        self.receipts = ReceiptLog(self.conn)
        self.service = NotesService(self.conn, receipts=self.receipts)
        self.reverter = build_reverter(self.receipts, notes=self.service)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        for key, value in self._patched.items():
            setattr(config, key, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._settings = None
        self._tmp.cleanup()

    # -- 小工具 -----------------------------------------------------------
    def _last_receipt_id(self) -> int:
        return self.receipts.recent(limit=1)[0].id

    def _undo_last(self) -> str:
        return str(self.reverter.undo(self._last_receipt_id())["note"])

    # -- 写入 -------------------------------------------------------------
    def test_add_goes_to_inbox_when_no_folder(self) -> None:
        result = self.service.add({"body": "今天反手还是拉不起来,球老往左飘。"})
        self.assertTrue(result["recorded"])
        self.assertEqual(result["folder"], INBOX_PATH)
        self.assertEqual(result["note_id"], 1)
        row = self.conn.execute("SELECT * FROM app_notes WHERE id = 1").fetchone()
        self.assertEqual(row["deleted_at"], None)
        # 回执必须与写入同事务落下来(否则用户看不到"写了什么")
        self.assertEqual(self.receipts.recent(limit=1)[0].kind, "journal.note")

    def test_add_with_unknown_folder_does_not_write(self) -> None:
        result = self.service.add({"body": "第三章开头想从雨夜写起", "folder": "写作/《归途》"})
        self.assertFalse(result.get("recorded"))
        self.assertTrue(result["needs_folder"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM app_notes").fetchone()[0], 0
        )
        # 候选里至少要有"收件箱",否则模型一个合法落点都拿不到
        paths = [item["path"] for item in result["candidates"]]
        self.assertIn(INBOX_PATH, paths)

    def test_duplicate_is_blocked_unless_forced(self) -> None:
        body = "发球练习:抛球要稳,别急着收拍。"
        first = self.service.add({"body": body})
        self.assertTrue(first["recorded"])
        second = self.service.add({"body": body})
        self.assertFalse(second["recorded"])
        self.assertEqual(second["duplicate_of"][0]["id"], first["note_id"])
        forced = self.service.add({"body": body, "force": True})
        self.assertTrue(forced["recorded"])

    def test_append_keeps_a_version(self) -> None:
        note = self.service.add({"body": "第一段"})
        result = self.service.append({"note_id": note["note_id"], "body": "第二段"})
        self.assertEqual(result["chars"], len("第一段\n\n第二段"))
        versions = self.service.versions(note["note_id"])
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["reason"], "append")

    def test_edit_then_undo_restores_previous_body(self) -> None:
        note = self.service.add({"body": "原来的话"})
        self.service.edit({"note_id": note["note_id"], "body": "改过的话"})
        self.assertEqual(
            self.conn.execute("SELECT body FROM app_notes WHERE id = ?",
                              (note["note_id"],)).fetchone()[0],
            "改过的话",
        )
        self._undo_last()
        self.assertEqual(
            self.conn.execute("SELECT body FROM app_notes WHERE id = ?",
                              (note["note_id"],)).fetchone()[0],
            "原来的话",
        )

    def test_delete_is_soft_and_undoable(self) -> None:
        note = self.service.add({"body": "要删的这条"})
        self.service.delete({"note_id": note["note_id"]})
        row = self.conn.execute("SELECT deleted_at FROM app_notes WHERE id = ?",
                                (note["note_id"],)).fetchone()
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(len(self.service.trash()), 1)
        self.assertEqual(self.service.recent({"limit": 10})["count"], 0)
        self._undo_last()
        self.assertEqual(self.service.recent({"limit": 10})["count"], 1)

    def test_restore_after_its_folder_was_deleted_lands_in_inbox(self) -> None:
        """删本子只看"活着的笔记";回收站里那条仍指着旧夹。
        恢复时若夹已经归档,必须落到收件箱 —— 否则"恢复了但哪儿都找不到"。"""
        self.service.mkdir({"name": "网球"})
        note = self.service.add({"body": "反手的问题", "folder": "网球"})
        self.service.delete({"note_id": note["note_id"]})
        self.service.delete_folder("网球")          # 活的笔记为 0,允许删

        restored = self.service.restore({"note_id": note["note_id"]})
        self.assertTrue(restored["restored"])
        self.assertTrue(restored["moved_to_inbox"])
        self.assertEqual(
            self.service.show({"note_id": note["note_id"]})["folder"], INBOX_PATH
        )

    # -- 夹 ---------------------------------------------------------------
    def test_mkdir_nested_and_tree(self) -> None:
        self.service.mkdir({"name": "写作"})
        self.service.mkdir({"name": "长篇", "parent": "写作"})
        self.service.mkdir({"name": "《归途》", "parent": "写作/长篇"})
        tree = self.service.tree()
        by_name = {node["name"]: node for node in tree}
        self.assertIn("写作", by_name)
        child = by_name["写作"]["children"][0]
        self.assertEqual(child["path"], "写作/长篇")
        self.assertEqual(child["children"][0]["path"], "写作/长篇/《归途》")

    def test_mkdir_rejects_duplicate_and_deep_paths(self) -> None:
        self.service.mkdir({"name": "网球"})
        with self.assertRaises(NotesError):
            self.service.mkdir({"name": "网球"})
        path = ""
        for index in range(6):
            name = "第" + str(index + 1) + "层"
            self.service.mkdir({"name": name, "parent": path or None})
            path = (path + "/" + name) if path else name
        with self.assertRaises(NotesError):
            self.service.mkdir({"name": "太深了", "parent": path})

    def test_rename_folder_moves_whole_subtree(self) -> None:
        self.service.mkdir({"name": "写作"})
        self.service.mkdir({"name": "长篇", "parent": "写作"})
        self.service.rename_folder({"folder": "写作", "name": "创作"})
        paths = [row[0] for row in self.conn.execute(
            "SELECT path FROM app_note_folders ORDER BY path")]
        self.assertIn("创作", paths)
        self.assertIn("创作/长篇", paths)
        self.assertNotIn("写作", paths)

    def test_delete_folder_refuses_when_not_empty(self) -> None:
        self.service.mkdir({"name": "网球"})
        self.service.add({"body": "一条笔记", "folder": "网球"})
        with self.assertRaises(NotesError):
            self.service.delete_folder("网球")
        self.service.delete({"note_id": 1})
        # 软删后夹里没有"活着的"笔记 —— 这时才允许归档
        self.service.delete_folder("网球")
        self.assertIsNone(self.service.folder_by_path("网球"))

    def test_inbox_cannot_be_deleted_or_renamed(self) -> None:
        with self.assertRaises(NotesError):
            self.service.delete_folder(INBOX_PATH)
        with self.assertRaises(NotesError):
            self.service.rename_folder({"folder": INBOX_PATH, "name": "别的"})

    def test_move_and_undo(self) -> None:
        self.service.mkdir({"name": "网球"})
        note = self.service.add({"body": "今天的球"})
        self.service.move({"note_id": note["note_id"], "new_folder": "网球"})
        self.assertEqual(self.service.show({"note_id": note["note_id"]})["folder"], "网球")
        self._undo_last()
        self.assertEqual(
            self.service.show({"note_id": note["note_id"]})["folder"], INBOX_PATH
        )

    # -- 检索 -------------------------------------------------------------
    def test_search_three_chars_uses_fts(self) -> None:
        self.service.add({"body": "今天反手还是拉不起来"})
        self.service.add({"body": "发球练习:抛球要稳"})
        # 3 字以上走 trigram 短语匹配:"拉不起来" 是原文里的连续子串
        result = self.service.search({"query": "拉不起来"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["title"], "今天反手还是拉不起来")

    def test_search_two_chars_falls_back_to_like(self) -> None:
        self.service.add({"body": "今天反手还是拉不起来"})
        result = self.service.search({"query": "反手"})
        self.assertEqual(result["count"], 1)

    def test_search_filters_by_folder_and_skips_deleted(self) -> None:
        self.service.mkdir({"name": "网球"})
        self.service.mkdir({"name": "写作"})
        self.service.add({"body": "反手的问题", "folder": "网球"})
        self.service.add({"body": "反手写进小说里", "folder": "写作"})
        result = self.service.search({"query": "反手", "folder": "网球"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["folder"], "网球")
        self.service.delete({"note_id": result["items"][0]["id"]})
        self.assertEqual(self.service.search({"query": "反手", "folder": "网球"})["count"], 0)

    def test_hint_returns_titles_only(self) -> None:
        self.service.add({"body": "今天反手还是拉不起来,球老往左飘。"})
        hits = self.service.hint("我上次说反手拉不起来是怎么说的")
        self.assertEqual(len(hits), 1)
        self.assertIn("title", hits[0])
        self.assertNotIn("body", hits[0])
        # 太短的输入不该触发检索(否则每句"好"都要跑一次 FTS)
        self.assertEqual(self.service.hint("好"), [])

    # -- 检索:2026-11-09 按云朵的 kb 检索升级后的行为 ---------------------
    def test_search_matches_a_word_in_the_middle_of_the_query(self) -> None:
        """滑窗的价值:查询里"不在开头"的词也能命中。

        旧实现只做前缀扩展("人物弧光" → 人物 / 人物弧),这条笔记一个都搜不到;
        新实现会切出 "弧光"。
        """
        self.service.add({"body": "这本小说先把弧光定下来,再写情节。"})
        result = self.service.search({"query": "人物弧光"})
        self.assertEqual(result["count"], 1, "中间的词(弧光)必须能命中")

    def test_search_matches_the_folder_name(self) -> None:
        """夹名也是检索内容 —— 正文里没写"网球"也要能搜到本子里的笔记。"""
        self.service.mkdir({"name": "网球"})
        self.service.add({"body": "反手还是拉不起来,球老往左飘。", "folder": "网球"})
        self.service.add({"body": "今天在读一本小说。"})
        result = self.service.search({"query": "网球"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["folder"], "网球")

    def test_exact_phrase_ranks_above_scattered_words(self) -> None:
        self.service.add({"body": "反手拉不起来,球老往左飘。"})
        self.service.add({"body": "反手还行,拉球也稳,就是起不来。"})
        result = self.service.search({"query": "反手拉不起来"})
        self.assertEqual(result["count"], 2)
        self.assertIn("球老往左飘", result["items"][0]["peek"],
                      "原话照抄的那条要排在前面")

    def test_hint_ignores_a_single_weak_term_outside_the_title(self) -> None:
        """只撞上一个 2 字词、还没进标题 = 噪音,不该每轮都提示。"""
        self.service.add({"title": "随手一记", "body": "今天去看了场电影。"})
        self.assertEqual(self.service.hint("电影"), [])

    def test_hint_shows_only_the_winner_when_it_is_clearly_ahead(self) -> None:
        self.service.add({"body": "反手还是拉不起来,球老往左飘。"})
        self.service.add({"body": "今天在读一本小说,挺好。"})
        hits = self.service.hint("我上次说反手拉不起来是怎么说的")
        self.assertEqual(len(hits), 1, "冠军明显领先时只提示一条")
        self.assertIn("反手", hits[0]["title"])

    # -- 归属 -------------------------------------------------------------
    def test_notes_are_private_per_member(self) -> None:
        mine = NotesService(self.conn, receipts=self.receipts, member_id=1)
        other = NotesService(self.conn, receipts=self.receipts, member_id=2)
        mine.add({"body": "只有我看得见"})
        self.assertEqual(mine.recent({"limit": 10})["count"], 1)
        self.assertEqual(other.recent({"limit": 10})["count"], 0)
        self.assertEqual(other.hint("只有我看得见"), [])
        self.assertEqual(ownership.get_share_mode(self.conn, ownership.DOMAIN_NOTES),
                         ownership.MODE_OWN)

    # -- 工具面 -----------------------------------------------------------
    def test_tool_specs_and_read_only(self) -> None:
        from la.tools.registry import ToolRegistry

        specs = {spec.name: spec for spec in build_note_tools(self.service)}
        self.assertEqual(set(specs), {"note_find", "note_add", "note_change"})
        self.assertEqual(specs["note_change"].risk, "confirm")

        # 只读判定在 register 那一刻按 metadata 白名单拍板 —— 必须走真的注册表,
        # 否则测的是 "ToolSpec 的默认值",不是"模型看到的门禁"。
        registry = ToolRegistry()
        registry.register_all(list(specs.values()))
        self.assertFalse(registry.get("note_find").writes)
        self.assertTrue(registry.get("note_add").writes)
        self.assertIn("note_find", registry.read_only_names())

        for spec in specs.values():
            schema = spec.parameters_json_schema()
            self.assertIn("action", schema["properties"])
            self.assertIn("action", schema["required"])

    def test_tool_dispatch_add_and_find(self) -> None:
        specs = {spec.name: spec for spec in build_note_tools(self.service)}
        breaker = CircuitBreaker(phase="answer")

        async def run() -> None:
            added = await specs["note_add"].handler(
                {"action": "add", "body": "微信里记的一条"}
            )
            self.assertTrue(added["recorded"])
            found = await specs["note_find"].handler({"action": "recent", "limit": 5})
            self.assertEqual(found["count"], 1)
            with self.assertRaises(ValueError):
                await specs["note_add"].handler({"action": "nope"})

        asyncio.run(run())
        # 说明:这里没走 registry.execute,所以不涉及门禁;门禁本身由
        # tests/test_m13_tool_schema.py 与只读白名单测试覆盖。

    def test_every_write_is_undoable_exactly_once(self) -> None:
        note = self.service.add({"body": "一次写入"})
        receipt_id = self._last_receipt_id()
        self.reverter.undo(receipt_id)
        from la.domain.receipts import ReceiptError
        with self.assertRaises(ReceiptError):
            self.reverter.undo(receipt_id)



class SessionWiringCase(unittest.TestCase):
    """会话层接线:工具注册进注册表 + 笔记线索真的进了这一轮的上下文。

    ⚠️ 这两件事都是"不报错但静默失效"的典型:工具没注册 → 模型永远不知道有笔记;
       hint 没拼进去 → 这条能力等于不存在。所以必须各钉一条。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
                        "ASSISTANT_DB", "MEMORY_DB")
        }
        self._env = {key: os.environ.get(key) for key in ("LA_LLM_PROVIDER", "DEEPSEEK_API_KEY")}
        os.environ["LA_LLM_PROVIDER"] = "fake"
        os.environ["DEEPSEEK_API_KEY"] = ""
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config._settings = None

        from la.agent.session import MemorySession
        from la.llm.fake import FakeClient

        self.session = MemorySession(
            FakeClient(), config.get_settings(), session_id="notes-wire",
            assistant_conn=db.connect_assistant(),
        )

    def tearDown(self) -> None:
        try:
            self.session.store.conn.close()
        except Exception:
            pass
        for key, value in self._patched.items():
            setattr(config, key, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._settings = None
        self._tmp.cleanup()

    def test_note_tools_are_registered(self) -> None:
        names = set(self.session.registry.names())
        self.assertTrue({"note_find", "note_add", "note_change"} <= names)
        self.assertIn("note_find", self.session.registry.read_only_names())

    def test_hint_is_injected_and_logged(self) -> None:
        # 两句:标题只会是第一句,第二句绝不能出现在线索行里
        self.session.notes.add(
            {"body": "今天反手还是拉不起来,球老往左飘。下次先练脚步,别急着发力。"}
        )
        text = self.session._notes_hint("我上次说反手拉不起来是怎么说的")
        self.assertIn("<notes-hint>", text)
        self.assertIn("反手还是拉不起来", text)
        # 只给标题,不给正文
        self.assertNotIn("下次先练脚步", text)
        # 绕过工具管线,但必须在事件里留痕
        events = [e for e in self.session.store.events(self.session.session_id)
                  if e.type == "journal/hint"]
        self.assertEqual(len(events), 1)

    def test_hint_is_empty_for_short_input(self) -> None:
        self.session.notes.add({"body": "反手还是拉不起来"})
        self.assertEqual(self.session._notes_hint("好"), "")


class JournalPageCase(unittest.TestCase):
    """页面本身:新建态、按天分组、空态 —— 这三处是这次重做动到的结构。

    ⚠️ 只钉"这一页该有的东西在不在",不钉像素:版式会继续改,
       但"点写一条要能看见一张空白纸"这种契约不该悄悄丢。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
                        "ASSISTANT_DB", "MEMORY_DB", "LOGS_DIR")
        }
        self._env = {key: os.environ.get(key) for key in ("LA_LLM_PROVIDER", "DEEPSEEK_API_KEY")}
        os.environ["LA_LLM_PROVIDER"] = "fake"
        os.environ["DEEPSEEK_API_KEY"] = ""
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config.LOGS_DIR = self.tmp / "logs"
        config._settings = None

        from fastapi.testclient import TestClient

        from la.web.app import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        for key, value in self._patched.items():
            setattr(config, key, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._settings = None
        self._tmp.cleanup()

    def _seed(self, folder: str = "", body: str = "一条笔记") -> None:
        from la.web.deps import build_services

        services = build_services()
        try:
            services.notes.add({"body": body, "folder": folder or None}, actor="user")
        finally:
            services.close()

    def test_page_renders_with_empty_state(self) -> None:
        page = self.client.get("/journal")
        self.assertEqual(page.status_code, 200)
        self.assertIn("这里还没有笔记", page.text)

    def test_new_note_opens_a_blank_sheet(self) -> None:
        page = self.client.get("/journal?new=1")
        self.assertEqual(page.status_code, 200)
        self.assertIn("记下来", page.text)
        self.assertIn("标题(可留空)", page.text)
        # 新建态不该顺手把"第一条"塞进编辑区
        self.assertNotIn("保存</button>", page.text)

    def test_list_is_grouped_by_day(self) -> None:
        self._seed(body="今天写的一条")
        page = self.client.get("/journal")
        self.assertIn("今天", page.text)
        self.assertIn("今天写的一条", page.text)

    def test_empty_folder_shows_its_own_empty_state(self) -> None:
        from la.web.deps import build_services

        services = build_services()
        try:
            services.notes.mkdir({"name": "网球"}, actor="user")
        finally:
            services.close()
        page = self.client.get("/journal?folder=%E7%BD%91%E7%90%83")
        self.assertEqual(page.status_code, 200)
        self.assertIn("这里还没有笔记", page.text)
        self.assertIn("网球", page.text)


    def test_new_folder_button_lives_outside_the_scrolling_chips(self) -> None:
        """「＋ 子本子」必须在滚动容器**外面**。

        真机 bug(2026-11-08):它原来放在 .nt-chips 里,而 .nt-chips 有 overflow-x:auto ——
        横向滚动会把纵向也变成 auto,于是弹出的表单被裁掉,点了像没反应,
        用户报"写作下面无法建文件夹"。
        """
        from la.web.deps import build_services

        services = build_services()
        try:
            services.notes.mkdir({"name": "写作"}, actor="user")
        finally:
            services.close()

        html = self.client.get("/journal?folder=%E5%86%99%E4%BD%9C").text
        chips_at = html.index('class="nt-chips"')
        chips_end = html.index("</nav>", chips_at)
        self.assertNotIn("nt-new", html[chips_at:chips_end],
                         "新建本子的表单不能放进 .nt-chips(会被 overflow 裁掉)")
        self.assertIn("nt-new", html[chips_end:], "它应当紧挨着筛选片、但在滚动区之外")
        # 表单必须带上当前本子作为父级,否则会建到最外层
        self.assertIn('name="parent" value="写作"', html)

    def test_creating_a_subfolder_lands_inside_it(self) -> None:
        from la.domain.services.notes_service import INBOX_PATH
        from la.web.deps import build_services

        services = build_services()
        try:
            services.notes.mkdir({"name": "写作"}, actor="user")
        finally:
            services.close()

        response = self.client.post(
            "/journal/folders",
            data={"name": "小说A", "parent": "写作", "_idem": "mk-1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("folder=", response.headers["location"])
        self.assertIn("%E5%B0%8F%E8%AF%B4A", response.headers["location"],
                      "建完应当直接进新本子")
        page = self.client.get("/journal?folder=%E5%86%99%E4%BD%9C%2F%E5%B0%8F%E8%AF%B4A")
        self.assertEqual(page.status_code, 200)
        self.assertIn("写作/小说A", page.text, "新本子要出现在筛选片里")

        # 同一个父级下可以建第二本(多本小说)
        self.client.post(
            "/journal/folders",
            data={"name": "小说B", "parent": "写作", "_idem": "mk-2"},
            follow_redirects=False,
        )
        page = self.client.get("/journal?folder=%E5%86%99%E4%BD%9C")
        self.assertIn("写作/小说A", page.text)
        self.assertIn("写作/小说B", page.text)
        self.assertIn(INBOX_PATH, page.text)



    def test_empty_folder_offers_a_delete_that_works(self) -> None:
        """空本子要能删 —— 这是用户报的第二个入口问题:入口原来只在"选中笔记"的右栏,
        而空本子恰恰没有笔记可选。"""
        from la.web.deps import build_services

        services = build_services()
        try:
            services.notes.mkdir({"name": "写作"}, actor="user")
            services.notes.mkdir({"name": "小说A", "parent": "写作"}, actor="user")
        finally:
            services.close()

        page = self.client.get("/journal?folder=%E5%86%99%E4%BD%9C%2F%E5%B0%8F%E8%AF%B4A")
        self.assertIn("删掉这个空本子", page.text, "空本子上必须看得见删除入口")
        self.assertIn('action="/journal/folders/delete"', page.text)

        response = self.client.post(
            "/journal/folders/delete",
            data={"folder": "写作/小说A", "_idem": "rm-1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        page = self.client.get("/journal")
        self.assertNotIn("写作/小说A", page.text, "删掉之后不该还挂在筛选片里")
        self.assertIn("写作", page.text, "父本子仍在")

    def test_folder_with_notes_refuses_delete_and_says_why(self) -> None:
        from la.web.deps import build_services

        services = build_services()
        try:
            services.notes.mkdir({"name": "网球"}, actor="user")
            services.notes.add({"body": "一条笔记", "folder": "网球"}, actor="user")
        finally:
            services.close()

        page = self.client.get("/journal?folder=%E7%BD%91%E7%90%83")
        self.assertIn("里面还有 1 条笔记", page.text)
        self.assertNotIn("删掉这个空本子</button>", page.text,
                         "有笔记时不该给一个能点的删除按钮")

        # 绕过界面直接 POST:服务端仍然要拒(界面只是"说清楚",不是安全边界)
        response = self.client.post(
            "/journal/folders/delete",
            data={"folder": "网球", "_idem": "rm-2"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers["location"])
        page = self.client.get("/journal")
        self.assertIn("网球", page.text, "被拒之后本子必须还在")


if __name__ == "__main__":
    unittest.main()