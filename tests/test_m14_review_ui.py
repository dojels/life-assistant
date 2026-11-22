"""界面上的素材与复习卡(D107 / D108 的界面那一半)。

用户 2026-10-13 的两句话就是这两个用例:
  ① "今日复习里,怎么有重复的" —— 一道题一张卡,一次作答记到当天到期的所有单元;
  ② "试题如果入库时生成了语音或者图片,在界面里要能看到,方便用户确认" ——
     孩子端的卡片与家长端的错题页都要看得见、听得到那两样**入库时生成的**素材。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from la import config
from la.domain.artifacts import ArtifactStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT

BASE_DIR = Path(__file__).resolve().parent.parent


class StudyPageMaterialsTest(unittest.TestCase):
    """⚠️ 路径常量必须**逐个**打补丁:config.ASSISTANT_DB 是 import 期算好的。"""

    PATHS = ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
             "LOGS_DIR", "ASSISTANT_DB", "MEMORY_DB", "RUNTIME_DIRS")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._patched = {key: getattr(config, key) for key in self.PATHS}
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.LOGS_DIR = self.tmp / "logs"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config.RUNTIME_DIRS = (self.tmp, config.ARCHIVE_DIR, config.MEDIA_DIR,
                               config.ARTIFACTS_DIR, config.LOGS_DIR)
        self._settings_cache = config._settings  # noqa: SLF001
        config._settings = None  # noqa: SLF001

        conn = db.connect(config.ASSISTANT_DB)
        db.migrate(conn, MIGRATIONS_ASSISTANT)
        self.problem_id, self.artifact_id = self._seed(conn)
        conn.close()

        from fastapi.testclient import TestClient

        from la.web import deps as web_deps
        from la.web.app import create_app

        self._real_factory = web_deps.build_model_client
        web_deps.build_model_client = lambda settings: None
        try:
            self.client = TestClient(create_app(), follow_redirects=False)
        finally:
            web_deps.build_model_client = self._real_factory

    def tearDown(self) -> None:
        for key, value in self._patched.items():
            setattr(config, key, value)
        config._settings = self._settings_cache  # noqa: SLF001
        self._tmp.cleanup()

    def _seed(self, conn) -> tuple[int, str]:
        """一道**听力题**:入库时就绑好了图形与声音(这里直接写库,省掉模型那一趟)。"""
        from la.domain.media import MediaStore
        from la.domain.schemas.study import ProblemCreate
        from la.domain.services.study_service import StudyService

        media = MediaStore(conn, root=config.MEDIA_DIR)
        artifacts = ArtifactStore(conn, root=config.ARTIFACTS_DIR)
        service = StudyService(conn, media=media, artifacts=artifacts)
        record = service.add_problem(
            ProblemCreate(subject="英语", stem_text="听写单词:life、slow、control",
                          knowledge_points=["单词拼写", "听力"], asset_ids=[]),
            actor="user",
        )
        audio = artifacts.register_bytes(
            b"ID3\x03\x00\x00\x00\x00\x00\x00fakemp3",
            filename="听写音频-1.mp3", label="听写音频 错题 #1", kind="file",
        )
        conn.execute(
            "UPDATE app_problems SET audio_artifact_id = ?, audio_spec = ?,"
            " figure_source = 'svg', figure_spec = ? WHERE id = ?",
            (
                audio.id,
                '{"kind": "dictation", "label": "听写音频", "text": "life、slow、control",'
                ' "seconds": 35.6, "voice": "Cherry", "provider": "bailian"}',
                '{"kind": "unit_cubes_front", "description": "示意",'
                ' "svg": "<svg xmlns=\'http://www.w3.org/2000/svg\'><rect width=\'10\' height=\'10\'/></svg>"}',
                record.id,
            ),
        )
        # ⚠️ 新建的单元首次到期是"明天",而这一页看的是"今天" —— 把它挪到今天
        conn.execute(
            "UPDATE app_review_units SET due_date_local = date('now', 'localtime')"
            " WHERE problem_id = ?", (record.id,),
        )
        conn.commit()
        return record.id, audio.id

    # -- 页签(D111):一页一件事,不再是一条长长的下拉 ----------------------
    def test_each_tab_shows_only_its_own_content(self) -> None:
        """⭐ 用户 2026-10-13:"下拉太长了…采用切换页面的设计…每页都显示适当的内容"。

        判据不只看"有没有",还要看"**不该有的别混进来**" —— 否则页签只是把长页面
        又拼了一遍(那正是要修的病)。
        """
        review = self.client.get("/study")
        self.assertEqual(review.status_code, 200)
        self.assertIn('class="review"', review.text)              # 复习卡在这一页
        self.assertNotIn('action="/study/upload"', review.text)   # 但录入不在
        self.assertNotIn('href="/study/problem/' + str(self.problem_id) + '"',
                         review.text)                             # 错题本列表也不在

        problems = self.client.get("/study?tab=problems")
        self.assertIn('href="/study/problem/' + str(self.problem_id) + '"', problems.text)
        self.assertIn("错题本", problems.text)
        self.assertNotIn('class="review"', problems.text)
        self.assertNotIn('action="/study/upload"', problems.text)

        intake = self.client.get("/study?tab=intake")
        self.assertIn('action="/study/upload"', intake.text)
        self.assertNotIn('class="review"', intake.text)

        weak = self.client.get("/study?tab=weak")
        self.assertIn("薄弱知识点", weak.text)
        self.assertNotIn('class="review"', weak.text)

    def test_tab_bar_is_on_every_tab_with_counts(self) -> None:
        for tab in ("review", "problems", "weak", "intake"):
            html = self.client.get("/study?tab=" + tab).text
            self.assertIn('class="segmented tabs"', html, tab)
            for name in ("今日复习", "错题本", "薄弱点", "录入"):
                self.assertIn(name, html, tab + " 的页签里少了 " + name)

    def test_unknown_tab_and_old_filter_links_still_land_somewhere_useful(self) -> None:
        """⚠️ 收敛而不是报错:老链接/书签、聊天里的旧参数都要还能打开他要看的东西。"""
        self.assertEqual(self.client.get("/study?tab=胡说").status_code, 200)
        self.assertIn('class="review"', self.client.get("/study?tab=胡说").text)
        # 带筛选的旧链接 → 错题本那一页(筛选只在那一页有意义)
        filtered = self.client.get("/study?subject=英语")
        self.assertEqual(filtered.status_code, 200)
        self.assertIn('href="/study/problem/' + str(self.problem_id) + '"', filtered.text)
        self.assertNotIn('class="review"', filtered.text)

    def test_review_post_lands_back_on_the_review_tab(self) -> None:
        page = self.client.post(
            "/study/review/problem/" + str(self.problem_id),
            data={"rating": 3, "_idem": "idem-tab"},
        )
        self.assertEqual(page.status_code, 303)
        self.assertIn("tab=review", page.headers.get("location", ""))

    def test_review_card_shows_the_bound_audio_and_figure_once(self) -> None:
        page = self.client.get("/study")
        self.assertEqual(page.status_code, 200)
        html = page.text
        # ⭐ 一道题一张卡:两个知识点不能变成两张卡(用户:"怎么有重复的")
        self.assertEqual(html.count('class="review"'), 1, html[:2000])
        # 卡片直接指向"按题记一次"的入口
        self.assertIn('action="/study/review/problem/' + str(self.problem_id) + '"', html)
        # 🔊 入库时绑好的音频:孩子端点开就能听
        self.assertIn('src="/artifacts/' + self.artifact_id + '"', html)
        self.assertIn("听写音频", html)
        # 图形(矢量重绘)也在卡上
        self.assertIn("<svg", html)

    def test_problem_page_shows_materials_for_the_parent(self) -> None:
        """界面上的"题是什么、有没有声音"要一眼看得到、听得到(2026-10-13 的原话)。

        ⚠️ 2026-11-09 重排了版面:原来的「题目素材」块并进了题干卡与声音卡 ——
        判据仍然是"看得见、听得到、能核对",只是不再靠那几个标题。
        """
        page = self.client.get("/study/problem/" + str(self.problem_id))
        self.assertEqual(page.status_code, 200)
        self.assertIn("题干", page.text)
        self.assertIn("答案", page.text)
        self.assertIn("声音", page.text)                     # 声音那一块(有才显示)
        self.assertIn('src="/artifacts/' + self.artifact_id + '"', page.text)
        self.assertIn("life、slow、control", page.text)     # 要念的原文,方便核对
        self.assertIn("Cherry", page.text)
        self.assertIn("35.6 秒", page.text)
        # 改了词表要能重做(否则孩子听到的还是旧的那几个词)
        self.assertIn('action="/study/problem/' + str(self.problem_id) + '/audio"', page.text)
        self.assertIn("按当前题干重做", page.text)

    def test_figure_buttons_rework_the_crop_without_a_model(self) -> None:
        """⭐ 2026-11-21:图形歪了 / 切了一边 / 带进一句题干 —— 人点一下重裁。

        判据有三条:① 页面上有这四个按钮;② POST 一次真的换了图;③ **一次模型都没调**
        (这一页在测试里本来就没有模型客户端,能跑通就说明不依赖模型)。
        """
        import io
        import json

        from PIL import Image

        from la.domain.media import MediaStore

        conn = db.connect(config.ASSISTANT_DB)
        media = MediaStore(conn, root=config.MEDIA_DIR)
        buffer = io.BytesIO()
        Image.new("RGB", (400, 300), "white").save(buffer, format="PNG")
        photo = media.store_bytes(buffer.getvalue(), media_type="image/png", subdir="problems")
        asset = media.store_bytes(buffer.getvalue(), media_type="image/png", subdir="figures")
        conn.execute(
            "UPDATE app_problems SET figure_asset_id = ?, figure_bbox = ?,"
            " figure_source = 'crop', figure_spec = ?, asset_ids = ? WHERE id = ?",
            (asset.id, json.dumps([0.2, 0.2, 0.8, 0.8]),
             json.dumps({"kind": "长方形示意图", "rotation_applied": 0}, ensure_ascii=False),
             json.dumps([photo.id]), self.problem_id),
        )
        conn.commit()
        conn.close()

        page = self.client.get("/study/problem/" + str(self.problem_id))
        self.assertIn('action="/study/problem/' + str(self.problem_id) + '/figure"', page.text)
        self.assertIn("逆时针", page.text)
        self.assertIn("收窄一点", page.text)

        resp = self.client.post(
            "/study/problem/" + str(self.problem_id) + "/figure",
            data={"action": "rotate_right"},
        )
        self.assertEqual(resp.status_code, 303)

        conn = db.connect(config.ASSISTANT_DB)
        row = conn.execute(
            "SELECT figure_spec, figure_asset_id FROM app_problems WHERE id = ?",
            (self.problem_id,),
        ).fetchone()
        conn.close()
        spec = json.loads(row["figure_spec"])
        self.assertEqual(spec["rotation_applied"], 90)
        self.assertNotEqual(row["figure_asset_id"], asset.id)

    def test_a_text_problem_does_not_show_an_empty_photo_card(self) -> None:
        """2026-11-09 重做错题页:旧版最糟的一屏,是左边一张空白的「题目照片」卡占掉半屏。"""
        page = self.client.get("/study/problem/" + str(self.problem_id))
        self.assertNotIn("题目照片", page.text)
        self.assertNotIn("这道题没有配图", page.text)

    def test_due_date_is_said_in_plain_words(self) -> None:
        """家长要的是"明天",不是一串 ISO 日期(2026-11-09 重做)。"""
        from la.web.routes_study import due_hint

        self.assertEqual(due_hint("2026-11-10", today="2026-11-09"), "明天")
        self.assertEqual(due_hint("2026-11-09", today="2026-11-09"), "今天")
        self.assertEqual(due_hint("2026-11-11", today="2026-11-09"), "后天")
        self.assertEqual(due_hint("2026-11-14", today="2026-11-09"), "5 天后")
        self.assertEqual(due_hint("2026-11-07", today="2026-11-09"), "已过期 2 天")
        self.assertEqual(due_hint("坏数据", today="2026-11-09"), "")
        # 种子题的到期日被排到了今天(复习卡要用),页面要按"今天"说话并给出「今天要做」
        page = self.client.get("/study/problem/" + str(self.problem_id))
        self.assertIn("今天要做", page.text)
        self.assertIn("下次复习", page.text)

    # -- 待确认 / 补做声音(2026-10-26 用户问的两件事)---------------------
    def test_pending_problem_can_be_confirmed_from_the_page(self) -> None:
        """「待确认」= 助手/识别写进来的题,还没经你看过。

        ⚠️ 这条以前是**死标签**:服务层有 confirm_problem,页面上却没有按钮,
        只能归档再恢复绕一圈才变成「在学」。
        """
        page = self.client.get("/study/problem/" + str(self.problem_id))
        self.assertIn("待确认", page.text)
        self.assertIn('action="/study/problem/' + str(self.problem_id) + '/confirm"', page.text)

        done = self.client.post("/study/problem/" + str(self.problem_id) + "/confirm")
        self.assertEqual(done.status_code, 303)
        conn = db.connect(config.ASSISTANT_DB)
        status = conn.execute(
            "SELECT status FROM app_problems WHERE id = ?", (self.problem_id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, "confirmed")

        after = self.client.get("/study/problem/" + str(self.problem_id))
        self.assertIn("在学", after.text)
        self.assertNotIn('/confirm"', after.text, "确认过了就不该再让点一次")

    # -- 录错的题要能删掉(2026-11-09 用户:"录错的,智能体自己也删除不掉")----------
    def test_the_page_can_delete_a_problem_that_was_never_reviewed(self) -> None:
        page = self.client.get("/study/problem/" + str(self.problem_id))
        self.assertIn("彻底删除", page.text)
        self.assertIn('action="/study/problem/' + str(self.problem_id) + '/delete"', page.text)

        done = self.client.post("/study/problem/" + str(self.problem_id) + "/delete")
        self.assertEqual(done.status_code, 303)
        self.assertTrue(done.headers["location"].startswith("/study?tab=problems"),
                        done.headers["location"])
        conn = db.connect(config.ASSISTANT_DB)
        row = conn.execute(
            "SELECT 1 FROM app_problems WHERE id = ?", (self.problem_id,)).fetchone()
        conn.close()
        self.assertIsNone(row, "没复习过的题应该真的删掉")

    def test_a_reviewed_problem_only_offers_archiving(self) -> None:
        """复习过的题:页面上不给删除按钮,路由也只会如实说"改成归档了"。"""
        conn = db.connect(config.ASSISTANT_DB)
        conn.execute("UPDATE app_review_units SET reps = 3 WHERE problem_id = ?",
                     (self.problem_id,))
        conn.commit()
        conn.close()

        page = self.client.get("/study/problem/" + str(self.problem_id))
        self.assertNotIn('action="/study/problem/' + str(self.problem_id) + '/delete"', page.text)
        self.assertIn("只有归档", page.text)

        done = self.client.post("/study/problem/" + str(self.problem_id) + "/delete")
        self.assertEqual(done.status_code, 303)
        self.assertIn("err=", done.headers.get("location", ""), "删不掉要如实说,不能假装删了")
        conn = db.connect(config.ASSISTANT_DB)
        row = conn.execute(
            "SELECT status FROM app_problems WHERE id = ?", (self.problem_id,)).fetchone()
        conn.close()
        self.assertIsNotNone(row, "复习过的题不许删")
        self.assertEqual(row["status"], "archived")

    # -- 错题本列表(2026-11-09 重做)--------------------------------------
    def _add(self, *, subject: str = "数学", stem: str = "3/4 + 1/6 = ?",
             due_offset: int = 1, archived: bool = False) -> int:
        from datetime import date, timedelta

        from la.domain.media import MediaStore
        from la.domain.schemas.study import ProblemCreate
        from la.domain.services.study_service import StudyService

        conn = db.connect(config.ASSISTANT_DB)
        try:
            service = StudyService(conn, media=MediaStore(conn, root=config.MEDIA_DIR))
            record = service.add_problem(
                ProblemCreate(subject=subject, stem_text=stem), actor="user",
            )
            due = (date.today() + timedelta(days=due_offset)).isoformat()
            conn.execute("UPDATE app_review_units SET due_date_local = ? WHERE problem_id = ?",
                         (due, record.id))
            conn.commit()
            if archived:
                service.archive_problem(record.id)
            return record.id
        finally:
            conn.close()

    def test_problem_list_sorts_by_when_it_is_due(self) -> None:
        """⭐ 旧列表按 id 倒序(最新录的排最前),家长要的却是"哪些今天要复习"。"""
        old = self._add(stem="五天后才复习的题", due_offset=5)
        archived = self._add(stem="归档过的题", due_offset=3, archived=True)

        page = self.client.get("/study?tab=problems")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn("今天", html)                     # 种子题:今天到期
        self.assertIn("5 天后", html)                   # 种子之外那道
        # 归档的不进默认列表
        self.assertNotIn('href="/study/problem/' + str(archived) + '"', html)
        # 今天到期的那道排在"5 天后"那道前面
        today_at = html.index('href="/study/problem/' + str(self.problem_id) + '"')
        later_at = html.index('href="/study/problem/' + str(old) + '"')
        self.assertLess(today_at, later_at, "今天该复习的题要排在后面到期的前面")

    def test_due_labels_and_order_are_plain_words(self) -> None:
        """列表左边那一列:逾期/今天/明天/日期 —— 并且按它排序。"""
        import types

        from la.web.routes_study import problem_cards

        def card(pid: int, due: str, status: str = "confirmed"):
            return types.SimpleNamespace(
                id=pid, subject="英语", stem_text="题 " + str(pid), student_name=None,
                knowledge_points=[], status=status, reps=0, lapses=0,
                next_due_local=due, due_count=0, audio_handle=None, photo_handles=[],
            )

        cards = problem_cards(
            [card(1, "2026-11-09"), card(2, "2026-11-07"), card(3, "2026-11-10"),
             card(4, "2026-11-20"), card(5, "")],
            today="2026-11-09",
        )
        self.assertEqual([c["due_label"] for c in cards],
                         ["逾期 2 天", "今天", "明天", "11-20", "—"])
        self.assertEqual([c["id"] for c in cards], [2, 1, 3, 4, 5])

    def test_archived_filter_shows_only_archived_problems(self) -> None:
        """⚠️ 旧写法只传 include_archived=True,「已归档」一栏把没归档的也列了出来。"""
        archived = self._add(stem="归档过的题", due_offset=2, archived=True)
        page = self.client.get("/study?tab=problems&archived=1")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/study/problem/' + str(archived) + '"', page.text)
        self.assertNotIn('href="/study/problem/' + str(self.problem_id) + '"',
                         page.text, "没归档的不该出现在已归档里")

    def test_counts_exclude_archived_so_tab_and_list_agree(self) -> None:
        """页签写 23、列表只有 17 道 —— 就是计数口径不一致闹的(2026-11-09 实测)。"""
        from la.domain.media import MediaStore
        from la.domain.services.study_service import StudyService

        self._add(stem="归档过的题", archived=True)
        conn = db.connect(config.ASSISTANT_DB)
        try:
            counts = StudyService(conn, media=MediaStore(conn, root=config.MEDIA_DIR)).problem_counts()
        finally:
            conn.close()
        self.assertEqual(counts["archived"], 1)
        self.assertEqual(counts["total"], 1)                      # 只有 setUp 里那一题
        self.assertEqual(sum(counts["by_subject"].values()), counts["total"])

    # -- 一天多道跟读并成一段(2026-11-09)----------------------------------
    def test_the_page_merges_todays_follow_problems_into_one_audio(self) -> None:
        """用户问:"如果一天同时有多个跟读,能合并吗" —— 页面上就是一段(与合并听写同一套)。"""
        import json
        from datetime import datetime
        from unittest import mock
        from zoneinfo import ZoneInfo

        from la.domain.services.study_service import StudyService

        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        spec = {"kind": "follow", "text": "take care of", "label": "跟读音频",
                "purpose": "follow", "repeat": 2, "gap_s": 3.0, "seconds": 8.2}
        conn = db.connect(config.ASSISTANT_DB)
        conn.execute("UPDATE app_problems SET audio_spec = ? WHERE id = ?",
                     (json.dumps(spec, ensure_ascii=False), self.problem_id))
        conn.execute("UPDATE app_review_units SET due_date_local = ? WHERE problem_id = ?",
                     (today, self.problem_id))
        conn.commit()
        conn.close()

        with mock.patch.object(
            StudyService, "_merged_audio",
            return_value={"audio_url": "/artifacts/merged-follow", "artifact_id": "m1",
                          "seconds": 8.2, "error": ""},
        ):
            page = self.client.get("/study")
        self.assertEqual(page.status_code, 200)
        self.assertIn("今天的合并听写 / 跟读", page.text)
        self.assertIn("英语跟读", page.text)
        self.assertIn("1 条", page.text)
        self.assertIn("/artifacts/merged-follow", page.text)

    def test_silent_dictation_page_says_it_out_loud_and_offers_a_repair(self) -> None:
        """手输/老数据里没有 audio_spec 的听写题:以前写「这题不需要声音」—— 那句话是错的。"""
        conn = db.connect(config.ASSISTANT_DB)
        conn.execute(
            "UPDATE app_problems SET audio_artifact_id = NULL, audio_spec = NULL WHERE id = ?",
            (self.problem_id,),
        )
        conn.commit()
        conn.close()
        page = self.client.get("/study/problem/" + str(self.problem_id))
        self.assertIn("这题要出声,但音频还没做过", page.text)
        self.assertIn('action="/study/problem/' + str(self.problem_id) + '/audio"', page.text)
        self.assertNotIn("这道题不需要声音", page.text)

    def test_repair_audio_route_reports_what_happened(self) -> None:
        """补做声音那条路:做成说做成,做不成说原因(不许静默)。"""
        from unittest import mock

        from la.domain.services.study_service import StudyService

        with mock.patch.object(
            StudyService, "bind_audio",
            return_value={"needed": True, "audio_artifact_id": "f1",
                          "audio_note": "听写音频已生成并绑定:13.0 秒"},
        ):
            page = self.client.post("/study/problem/" + str(self.problem_id) + "/audio")
        self.assertEqual(page.status_code, 303)
        self.assertIn("%E5%90%AC%E5%86%99", page.headers.get("location", ""), "回执里要带那句话")

        with mock.patch.object(
            StudyService, "bind_audio",
            return_value={"needed": True, "audio_artifact_id": None,
                          "audio_error": "没拿到要念的原文"},
        ):
            failed = self.client.post("/study/problem/" + str(self.problem_id) + "/audio")
        self.assertEqual(failed.status_code, 303)
        self.assertIn("err=", failed.headers.get("location", ""))

    def test_one_tap_records_every_due_unit(self) -> None:
        page = self.client.post(
            "/study/review/problem/" + str(self.problem_id),
            data={"rating": 3, "_idem": "idem-1"},
        )
        self.assertEqual(page.status_code, 303)
        conn = db.connect(config.ASSISTANT_DB)
        logs = conn.execute(
            "SELECT unit_id FROM app_review_logs ORDER BY id"
        ).fetchall()
        due_left = conn.execute(
            "SELECT count(*) FROM app_review_units WHERE problem_id = ?"
            " AND due_date_local <= date('now', 'localtime')", (self.problem_id,),
        ).fetchone()[0]
        kinds = [row[0] for row in conn.execute(
            "SELECT kind FROM ops_receipts ORDER BY id").fetchall()]
        conn.close()
        # 原题单元 + 2 个知识点单元 = 3 个都到期,一次作答全记上
        self.assertEqual(len(logs), 3, "三个单元各留一条日志")
        self.assertEqual(due_left, 0, "一次作答把当天到期的单元全记上")
        self.assertEqual(kinds.count("study.review"), 1, "回执只有一条")

    def test_review_history_shows_one_row_per_tap_not_per_unit(self) -> None:
        """⭐ 用户 2026-10-27 问:"复习历史每天为什么有两条?"

        一题多单元(整题 + 每个知识点各一个),一次作答会记到**所有到期单元**上 ——
        以前按单元平铺,看着像每天复习了两遍。现在合并成一条,并写清记到了几个单元。
        """
        page = self.client.post(
            "/study/review/problem/" + str(self.problem_id),
            data={"rating": 3, "_idem": "idem-hist"},
            follow_redirects=False,
        )
        self.assertEqual(page.status_code, 303)
        detail = self.client.get("/study/problem/" + str(self.problem_id))
        self.assertIn("共 1 次", detail.text)          # 3 条日志 = 1 次复习
        self.assertIn("整题", detail.text)
        self.assertIn("单词拼写", detail.text)
        self.assertIn("听力", detail.text)
        self.assertIn("这一次记到了 3 个单元", detail.text)

    def test_history_time_is_shown_in_local_time(self) -> None:
        """库里存 UTC;家长看到 "11:26" 会以为系统坏了(实际是 19:26)。"""
        from la.domain.services.study_service import StudyService

        conn = db.connect(config.ASSISTANT_DB)
        service = StudyService(conn, tzid="Asia/Shanghai")
        try:
            self.assertEqual(
                service._local_stamp("2026-10-27T11:26:49+00:00"), "2026-10-27 19:26"
            )
            self.assertEqual(service._local_stamp("坏数据"), "坏数据")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
