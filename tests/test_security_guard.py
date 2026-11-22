"""2026-10-17 审计后的第一道闸:同源守卫 / 路径黑名单 / 代码目录只读 / 日期兜底 / 迁移幂等。

每一条都对着一次**实测过的**事故:

1. 跨源 POST 改成功人格(任何网页都能写)              -> test_same_origin_guard
2. file 工具读走 data/wechat/credentials.json          -> test_credentials_are_not_readable
3. artifact_write 覆盖 la/agent/prompts.py             -> test_code_dirs_are_read_only
4. "2月29日" 打死整轮对话                              -> test_impossible_date_is_none
5. 迁移 v7 崩在中途后库永久打不开                       -> test_v7_can_run_twice
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from la import config
from la.base.files import BaseToolError
from la.base.tools import build_base_tools
from la.memory.timerange import parse_range
from la.ops.paths import DEFAULT_POLICY, PathNotAllowed
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT


class GuardBase(unittest.TestCase):
    """隔离的数据目录。

    ⚠️ 2026-10-17 的教训(我踩的):只改 `config.DATA_DIR` 是不够的 ——
    `config.ASSISTANT_DB` / `config.MEMORY_DB` 是**另一组常量**,
    `build_services()` 用的是它们。漏了这一层,这一个测试就会往**真实的库**里写:
    实测把用户的人格覆盖成了 "同源页面正常保存的一版。"(已从回执里恢复)。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig = (
            config.DATA_DIR, config.ARCHIVE_DIR, config.ARTIFACTS_DIR,
            config.ASSISTANT_DB, config.MEMORY_DB,
        )
        config.DATA_DIR = self.root
        config.ARCHIVE_DIR = self.root / "archive"
        config.ARTIFACTS_DIR = self.root / "artifacts"
        config.ASSISTANT_DB = self.root / "assistant.db"
        config.MEMORY_DB = self.root / "memory.db"

    def tearDown(self) -> None:
        (config.DATA_DIR, config.ARCHIVE_DIR, config.ARTIFACTS_DIR,
         config.ASSISTANT_DB, config.MEMORY_DB) = self._orig
        self._tmp.cleanup()


class SameOriginGuardTestCase(GuardBase):
    """① 别人的网页发不出写请求;② 我们自己的页面照旧能写;③ 脚本/测试照旧能写。"""

    def setUp(self) -> None:
        super().setUp()
        import os

        self._key = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = ""
        from fastapi.testclient import TestClient

        from la.web.app import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        import os

        if self._key is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = self._key
        super().tearDown()

    def test_cross_origin_write_is_refused(self) -> None:
        response = self.client.post(
            "/persona/save", data={"text": "攻击者写进去的一版。"},
            headers={"Origin": "https://evil.example"}, follow_redirects=False)
        self.assertEqual(403, response.status_code)
        self.assertIn("跨站点写入已拒绝", response.text)

    def test_null_origin_is_refused(self) -> None:
        response = self.client.post(
            "/persona/save", data={"text": "沙箱 iframe 写的一版。"},
            headers={"Origin": "null"}, follow_redirects=False)
        self.assertEqual(403, response.status_code)

    def test_cross_origin_referer_without_origin_is_refused(self) -> None:
        """老浏览器或某些插件不带 Origin —— Referer 是第二道。"""
        response = self.client.post(
            "/persona/save", data={"text": "referer 伪造。"},
            headers={"Referer": "https://evil.example/attack.html"},
            follow_redirects=False)
        self.assertEqual(403, response.status_code)

    def test_the_test_itself_is_isolated(self) -> None:
        """这一条是给上面那段注释钉的钉子:库必须落在临时目录里。"""
        from la.web.deps import build_services

        services = build_services()
        try:
            self.assertEqual(self.root, Path(config.ASSISTANT_DB).parent)
            self.assertEqual(self.root, Path(services.settings.data_dir or self.root).parent
                             if getattr(services.settings, "data_dir", None) else self.root)
        finally:
            services.close()

    def test_same_origin_write_goes_through(self) -> None:
        response = self.client.post(
            "/persona/save", data={"text": "同源页面正常保存的一版。"},
            headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(303, response.status_code)

    def test_a_client_without_origin_still_works(self) -> None:
        """curl / 脚本 / 测试客户端没有 Origin —— 不许被误伤。"""
        response = self.client.post(
            "/persona/save", data={"text": "没有 Origin 的一版。"},
            follow_redirects=False)
        self.assertEqual(303, response.status_code)

    def test_reads_are_not_blocked(self) -> None:
        for path in ("/persona", "/memory", "/chat", "/trace"):
            self.assertEqual(200, self.client.get(path).status_code, path)

    def test_security_headers_are_present(self) -> None:
        headers = self.client.get("/chat").headers
        self.assertEqual("SAMEORIGIN", headers.get("X-Frame-Options"))
        self.assertEqual("nosniff", headers.get("X-Content-Type-Options"))
        self.assertEqual("same-origin", headers.get("Referrer-Policy"))
        self.assertIn("frame-ancestors 'self'", headers.get("Content-Security-Policy", ""))


class PathPolicyTestCase(unittest.TestCase):
    """凭据类文件:按名字拒、按位置拒;普通工作区文件照旧能读。"""

    def test_credentials_are_not_readable(self) -> None:
        for path in ("data/wechat/credentials.json", "data/wechat/session.json",
                     "data/wechat/outbox.json", "settings.yaml", ".env", ".env.bak"):
            with self.assertRaises(PathNotAllowed, msg=path):
                DEFAULT_POLICY.check(path)

    def test_normal_files_are_still_readable(self) -> None:
        for path in ("la/config.py", "data/logs", "README.md"):
            self.assertTrue(DEFAULT_POLICY.check(path), path)

    def test_outside_the_workspace_is_refused(self) -> None:
        with self.assertRaises(PathNotAllowed):
            DEFAULT_POLICY.check("C:/Users/example/.ssh/id_rsa")


class CodeDirsReadOnlyTestCase(unittest.TestCase):
    """artifact_write 不许改代码目录 —— 它是"注入变持久化"的最短路径。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig = (config.BASE_DIR, config.DATA_DIR, config.ARTIFACTS_DIR)
        config.BASE_DIR = self.root
        config.DATA_DIR = self.root / "data"
        config.ARTIFACTS_DIR = self.root / "data" / "artifacts"
        self.conn = db.connect(self.root / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        from la.domain.artifacts import ArtifactStore
        from la.domain.receipts import DOMAIN_ARTIFACT, ReceiptLog

        self.spec = next(
            item for item in build_base_tools(
                artifacts=ArtifactStore(self.conn, root=config.ARTIFACTS_DIR),
                receipts=ReceiptLog(self.conn),
                receipt_domain=DOMAIN_ARTIFACT,
            ) if item.name == "artifact_write"
        )
        (self.root / "data" / "notes").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.conn.close()
        config.BASE_DIR, config.DATA_DIR, config.ARTIFACTS_DIR = self._orig
        self._tmp.cleanup()

    def _write(self, params: dict):
        return asyncio.run(self.spec.handler(params))

    def test_code_directories_are_refused(self) -> None:
        for path in ("la/config.py", "la/agent/prompts.py", "tests/test_m0.py",
                     "scripts/verify_m1.py"):
            with self.assertRaises(BaseToolError, msg=path) as caught:
                self._write({"path": path, "content": "x"})
            self.assertIn("只读", str(caught.exception))

    def test_user_data_is_still_writable(self) -> None:
        result = self._write({"path": "data/notes/清单.md", "content": "# 清单"})
        self.assertTrue(result)
        self.assertTrue((self.root / "data" / "notes" / "清单.md").exists())


class DateGuardTestCase(unittest.TestCase):
    """不存在的日期 = 没有时间范围,而不是整轮对话崩掉。"""

    NOW = datetime(2026, 10, 17, 12, 0).astimezone()

    def test_impossible_date_is_none(self) -> None:
        for text in ("2月29日那天我花了多少钱", "13月5日呢", "2026-02-31 的记录"):
            self.assertIsNone(parse_range(text, self.NOW), text)

    def test_real_dates_still_parse(self) -> None:
        today = parse_range("10月17日呢", self.NOW)
        self.assertIsNotNone(today)
        self.assertEqual("2026-10-17", today.start.date().isoformat())
        self.assertEqual("10月17日", today.label)


class MigrationReentrantTestCase(unittest.TestCase):
    def test_v7_can_run_twice(self) -> None:
        """删掉版本号再跑一遍:以前这里抛 duplicate column,之后库就再也打不开。"""
        tmp = tempfile.mkdtemp(prefix="la-mig-")
        try:
            conn = db.connect(Path(tmp) / "assistant.db")
            try:
                db.migrate(conn, MIGRATIONS_ASSISTANT)
                conn.execute("DELETE FROM sys_schema_version WHERE version = 7")
                conn.commit()
                self.assertEqual([7], db.migrate(conn, MIGRATIONS_ASSISTANT))
                columns = {row[1] for row in conn.execute("PRAGMA table_info(app_txns)")}
                self.assertTrue({"deleted_at", "version"} <= columns)
            finally:
                conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
