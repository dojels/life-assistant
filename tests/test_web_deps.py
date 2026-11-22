"""Web 层连接与 FastAPI 线程池的边界测试。

⭐ 这一组测的是一个**真出现过**的 bug:

    sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in
    that same thread. The object was created in thread id 18052 and this is thread id 16644.

页面**偶尔** 500,重试又好了。原因不是 SQLite 有问题,而是:
FastAPI 把**同步的生成器依赖**丢进 `run_in_threadpool`,端点也丢进 `run_in_threadpool`,
两者**不保证落在同一个工作线程上**。所以「线程 A 建连接、线程 B 用连接」是常态。

这类 bug 最难的地方在于**它是概率性的**:顺序快速请求时线程池常常复用同一个空闲线程,
于是本地怎么点都是好的,部署上去偶发 500。所以这里的测试**必须显式跨线程**,
不能靠"多请求几次试试看"。
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from la import config


class ServicesThreadingCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig = (config.DATA_DIR, config.ARCHIVE_DIR, config.MEDIA_DIR,
                      config.BACKUPS_DIR, config.ASSISTANT_DB, config.MEMORY_DB)
        config.DATA_DIR = root
        config.ARCHIVE_DIR = root / "archive"
        config.MEDIA_DIR = root / "media"
        config.BACKUPS_DIR = root / "backups"
        config.ASSISTANT_DB = root / "assistant.db"
        config.MEMORY_DB = root / "memory.db"

    def tearDown(self) -> None:
        (config.DATA_DIR, config.ARCHIVE_DIR, config.MEDIA_DIR,
         config.BACKUPS_DIR, config.ASSISTANT_DB, config.MEMORY_DB) = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass  # Windows:连接刚关掉时文件可能还被占着

    @staticmethod
    def run_in_other_thread(fn):
        """在**另一个**线程里跑 fn,把结果或异常带回来。"""
        box: list = []

        def worker() -> None:
            try:
                box.append(("ok", fn()))
            except BaseException as exc:  # noqa: BLE001 —— 要把异常原样带回来
                box.append(("error", type(exc).__name__ + ": " + str(exc)))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=30)
        self_kind, payload = box[0]
        return self_kind, payload

    def test_services_built_in_one_thread_are_usable_from_another(self) -> None:
        """⭐ 这就是那个 bug 的最小复现:建连接和用连接不在同一个线程。"""
        from la.web.deps import build_services

        services = build_services()  # 主线程里建
        try:
            kind, payload = self.run_in_other_thread(
                lambda: services.calendar.expand(
                    start="2026-10-11T00:00:00+08:00", end="2026-10-18T00:00:00+08:00"
                )
            )
        finally:
            services.close()
        self.assertEqual(kind, "ok", payload)

    def test_memory_view_is_usable_from_another_thread(self) -> None:
        """记忆页走的是裸连接,同样必须能跨线程用。"""
        from la.web.deps import build_services
        from la.web.memory_view import build_memory_view

        services = build_services()
        try:
            kind, payload = self.run_in_other_thread(
                lambda: build_memory_view(services.memory, now_text="2026-10-11 13:00")
            )
        finally:
            services.close()
        self.assertEqual(kind, "ok", payload)
        self.assertEqual(len(payload.layers), 8)  # L3c 候选层(2026-10-13)

    def test_web_connections_really_opt_out_of_the_thread_check(self) -> None:
        """⭐ 断言"关掉检查"这件事本身,而不是碰运气。"""
        from la.web.deps import build_services

        services = build_services()
        try:
            for conn in (services.assistant, services.memory):
                kind, payload = self.run_in_other_thread(
                    lambda c=conn: c.execute("SELECT 1").fetchone()[0]
                )
                self.assertEqual(kind, "ok", payload)
        finally:
            services.close()

    def test_cli_connections_keep_the_strict_check(self) -> None:
        """⚠️ 默认必须仍然是从严的 —— 只有 Web 层显式关掉。

        如果哪天有人图省事把默认值改成 False,这条会红。
        """
        from la.storage import db

        conn = db.connect_assistant()
        try:
            kind, payload = self.run_in_other_thread(lambda: conn.execute("SELECT 1").fetchone())
        finally:
            conn.close()
        self.assertEqual(kind, "error", "默认连接竟然允许跨线程 —— 防护被拆掉了")
        self.assertIn("thread", str(payload).lower())


class WebPagesCase(unittest.TestCase):
    """九个页面走一遍(含 D117 的人格页、D118 的轨迹页)—— 用 TestClient,不起服务器。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig = (config.DATA_DIR, config.ARCHIVE_DIR, config.MEDIA_DIR,
                      config.BACKUPS_DIR, config.ASSISTANT_DB, config.MEMORY_DB)
        config.DATA_DIR = root
        config.ARCHIVE_DIR = root / "archive"
        config.MEDIA_DIR = root / "media"
        config.BACKUPS_DIR = root / "backups"
        config.ASSISTANT_DB = root / "assistant.db"
        config.MEMORY_DB = root / "memory.db"

    def tearDown(self) -> None:
        (config.DATA_DIR, config.ARCHIVE_DIR, config.MEDIA_DIR,
         config.BACKUPS_DIR, config.ASSISTANT_DB, config.MEMORY_DB) = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_all_pages_render_without_an_api_key(self) -> None:
        """⭐ 读取路径里没有 LLM:不配密钥,九个页面全部 200。"""
        import os

        from fastapi.testclient import TestClient

        from la.web.app import create_app

        previous = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = ""
        try:
            client = TestClient(create_app())
            for path in ("/", "/ledger", "/calendar", "/health", "/study", "/memory",
                         "/persona", "/trace"):
                response = client.get(path, follow_redirects=True)
                self.assertEqual(response.status_code, 200, path + " 打不开")
            # 连打两轮:第一轮会建库,第二轮会命中缓存/已存在的连接
            for path in ("/ledger", "/calendar", "/memory"):
                self.assertEqual(client.get(path).status_code, 200, path + " 第二轮失败")
        finally:
            if previous is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = previous

    def test_every_page_links_to_the_memory_page(self) -> None:
        from fastapi.testclient import TestClient

        from la.web.app import create_app

        client = TestClient(create_app())
        for path in ("/ledger", "/calendar", "/health", "/study", "/chat", "/persona"):
            body = client.get(path, follow_redirects=True).text
            self.assertIn('href="/memory"', body, path + " 的导航缺「记忆」")

    def test_every_page_links_to_the_persona_page(self) -> None:
        """人格页(D117)同样是导航里常驻的一项 —— 藏起来的设置页等于没有。"""
        from fastapi.testclient import TestClient

        from la.web.app import create_app

        client = TestClient(create_app())
        for path in ("/ledger", "/calendar", "/health", "/study", "/chat", "/memory"):
            body = client.get(path, follow_redirects=True).text
            self.assertIn('href="/persona"', body, path + " 的导航缺「人格」")

    def test_every_page_links_to_the_trace_page(self) -> None:
        """轨迹页(D118)同理:要核对上下文,入口必须随手可及(手机标签栏也有)。"""
        from fastapi.testclient import TestClient

        from la.web.app import create_app

        client = TestClient(create_app())
        for path in ("/", "/ledger", "/calendar", "/health", "/study", "/materials",
                     "/memory", "/persona"):
            body = client.get(path, follow_redirects=True).text
            self.assertIn('href="/trace"', body, path + " 的导航缺「轨迹」")


if __name__ == "__main__":
    unittest.main()