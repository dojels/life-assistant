"""D157:页面可编辑的模型密钥(加密落库、版本号热加载、sudo、TLS 闸)。

每一条都对着一个真会出事的场景:
  * 密钥明文落库/落 HTML —— 备份泄露就等于 key 泄露;
  * 保存了但进程还用旧值 —— 用户会以为功能坏了;
  * 云端 HTTP 明文传 key —— 同一个 WiFi 就能抓走;
  * 三个进程同时迁移 —— 开机时互相撞版本号。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from la import config
from la.family import members as member_store
from la.ops import secrets as secret_store
from la.storage import db


#: 会被真实 .env 灌进 os.environ 的键(测试前要清干净,否则互相串味)。
ENV_KEYS = (
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "LA_BASE_URL",
    "LA_MODEL",
    "LA_REASONER_MODEL",
    "LA_REASONING_EFFORT",
    "LA_LLM_PROVIDER",
    "LA_TTS_PROVIDER",
    "LA_TTS_VOICE",
)


class SecretsCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ASSISTANT_DB", "MEMORY_DB")
        }
        config.DATA_DIR = self.root
        config.ASSISTANT_DB = self.root / "assistant.db"
        config.MEMORY_DB = self.root / "memory.db"
        # 不读真实 .env:那里面有真 key,测试不该依赖也不该把它带进环境变量。
        self._dotenv = config.load_dotenv
        config.load_dotenv = lambda *args, **kwargs: {}  # type: ignore[assignment]
        self._settings = config._settings
        self._settings_rev = config._settings_rev
        config._settings = None
        config._settings_rev = -1
        # 别的测试可能已经 load_dotenv 过(真 .env 里就有 LA_BASE_URL 等),
        # 那会把值灌进 os.environ 并记进 _DOTENV_INJECTED。不清掉的话,
        # 这里的"页面覆盖"会被真实 .env 值顶掉 —— 测试之间串味。
        self._env_backup = {
            key: os.environ.pop(key, None) for key in ENV_KEYS
        }
        self._injected_backup = set(config._DOTENV_INJECTED)
        config._DOTENV_INJECTED.difference_update(ENV_KEYS)
        secret_store.reset_cache()
        self.conn = db.connect_assistant()

    def tearDown(self) -> None:
        self.conn.close()
        for key, value in self._saved.items():
            setattr(config, key, value)
        config.load_dotenv = self._dotenv  # type: ignore[assignment]
        config._settings = self._settings
        config._settings_rev = self._settings_rev
        config._DOTENV_INJECTED.clear()
        config._DOTENV_INJECTED.update(self._injected_backup)
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        secret_store.reset_cache()
        self._tmp.cleanup()


class CryptoTest(SecretsCase):
    def test_roundtrip(self) -> None:
        secret_store.set_secret(self.conn, "llm.api_key", "sk-test-1234567890")
        self.assertEqual(
            secret_store.get_secret(self.conn, "llm.api_key"), "sk-test-1234567890"
        )

    def test_ciphertext_is_not_the_plaintext(self) -> None:
        secret_store.set_secret(self.conn, "llm.api_key", "sk-plain-value")
        row = self.conn.execute("SELECT ciphertext FROM app_secrets").fetchone()
        self.assertNotIn(b"sk-plain-value", bytes(row["ciphertext"]))

    def test_aad_binds_field_name(self) -> None:
        """把 llm 的密文凭到 tts 字段上必须解不开(AAD 绑字段名)。"""
        blob = secret_store.encrypt("llm.api_key", "sk-moved")
        self.assertIsNone(secret_store.decrypt("tts.api_key", blob))

    def test_garbage_ciphertext_is_missing_not_crash(self) -> None:
        self.conn.execute(
            "INSERT INTO app_secrets(name, ciphertext, updated_at) VALUES (?, ?, ?)",
            ("llm.api_key", b"junk-bytes-that-are-too-short", "2026-01-01T00:00:00+00:00"),
        )
        self.conn.commit()
        self.assertIsNone(secret_store.get_secret(self.conn, "llm.api_key"))

    def test_mask_never_reveals_middle(self) -> None:
        masked = secret_store.mask("sk-abcdefghijklmnop")
        self.assertTrue(masked.startswith("sk-a"))
        self.assertNotIn("defghijkl", masked)


class PriorityTest(SecretsCase):
    def test_page_beats_dotenv(self) -> None:
        """页面保存的 key 要压过文件默认(与联网开关同一条口径)。"""
        config.load_dotenv = lambda *a, **k: {"DEEPSEEK_API_KEY": "sk-from-dotenv"}  # type: ignore
        secret_store.set_secret(self.conn, "llm.api_key", "sk-from-page")
        secret_store.sync(self.conn, force=True)
        self.assertEqual(config.get_settings(reload=True).api_key, "sk-from-page")

    def test_env_beats_page(self) -> None:
        """环境变量是运维的紧急通道,故意留在最高层。"""
        secret_store.set_secret(self.conn, "llm.api_key", "sk-from-page")
        secret_store.sync(self.conn, force=True)
        os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
        try:
            self.assertEqual(config.get_settings(reload=True).api_key, "sk-from-env")
        finally:
            os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_describe_reports_source_without_plaintext(self) -> None:
        secret_store.set_secret(self.conn, "llm.api_key", "sk-secret-value-123")
        entries = {entry.key: entry for entry in secret_store.describe(self.conn)}
        entry = entries["llm.api_key"]
        self.assertEqual(entry.source, "page")
        self.assertNotIn("secret-value", entry.preview)


class HotReloadTest(SecretsCase):
    def test_rev_change_rebuilds_settings_without_restart(self) -> None:
        self.assertEqual(config.get_settings().api_key, "")
        secret_store.set_secret(self.conn, "llm.api_key", "sk-hot-reload")
        # 页面保存时 bump_rev 已经把新值推进本进程;这里再模拟另一个进程拉一次。
        secret_store.sync(self.conn, force=True)
        self.assertEqual(config.get_settings().api_key, "sk-hot-reload")

    def test_clear_falls_back_to_file(self) -> None:
        secret_store.set_secret(self.conn, "llm.api_key", "sk-temp")
        secret_store.sync(self.conn, force=True)
        self.assertEqual(config.get_settings().api_key, "sk-temp")
        secret_store.clear_secret(self.conn, "llm.api_key")
        secret_store.sync(self.conn, force=True)
        self.assertEqual(config.get_settings().api_key, "")

    def test_base_url_and_model_overrides(self) -> None:
        secret_store.set_setting(self.conn, "llm.base_url", "https://proxy.example.com")
        secret_store.set_setting(self.conn, "llm.model", "deepseek-v4-pro")
        secret_store.sync(self.conn, force=True)
        settings = config.get_settings()
        self.assertEqual(settings.base_url, "https://proxy.example.com")
        self.assertEqual(settings.model, "deepseek-v4-pro")


class MigrationTest(unittest.TestCase):
    def test_secrets_table_and_rev_exist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "assistant.db"
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            try:
                from la.storage.migrations import MIGRATIONS_ASSISTANT

                db.migrate(conn, MIGRATIONS_ASSISTANT)
                names = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                self.assertIn("app_secrets", names)
                self.assertIn("app_sudo_tokens", names)
                row = conn.execute(
                    "SELECT value FROM sys_settings WHERE key = 'config_rev'"
                ).fetchone()
                self.assertIsNotNone(row)
            finally:
                conn.close()

    def test_cross_process_lock_file_is_used(self) -> None:
        """迁移要在库旁边留一把文件锁 —— 云上三条服务同时开机时才不会互相撞。"""
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "assistant.db"
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            try:
                from la.storage.migrations import MIGRATIONS_ASSISTANT

                db.migrate(conn, MIGRATIONS_ASSISTANT)
                self.assertTrue((Path(str(path) + ".migrate.lock")).exists())
                # 再跑一次应该是空列表(版本号已记),而且不抛。
                self.assertEqual(db.migrate(conn, MIGRATIONS_ASSISTANT), [])
            finally:
                conn.close()


class TlsGateTest(unittest.TestCase):
    def _ok(self, **kwargs):
        from la.web.routes_model import secret_channel_ok

        return secret_channel_ok(**kwargs)

    def test_https_allowed(self) -> None:
        self.assertTrue(self._ok(scheme="https", forwarded_proto="", client_host="1.2.3.4", hostname="la.example.com"))

    def test_forwarded_https_allowed(self) -> None:
        """云上是 nginx 终止 TLS:到进程这一跳是 http,但 X-Forwarded-Proto 是 https。"""
        self.assertTrue(self._ok(scheme="http", forwarded_proto="https", client_host="10.0.0.9", hostname="la.example.com"))

    def test_plain_http_remote_rejected(self) -> None:
        self.assertFalse(self._ok(scheme="http", forwarded_proto="", client_host="203.0.113.5", hostname="203.0.113.10"))

    def test_loopback_allowed(self) -> None:
        self.assertTrue(self._ok(scheme="http", forwarded_proto="", client_host="127.0.0.1", hostname="127.0.0.1"))


class WebTest(SecretsCase):
    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient

        from la.web import auth as web_auth
        from la.web.app import create_app

        member_store.set_pin(self.conn, 1, "123456")
        token = web_auth.create_session(self.conn, 1)
        self.client = TestClient(create_app(), follow_redirects=False)
        self.client.cookies.set(web_auth.SESSION_COOKIE, token)

    def test_page_without_sudo_shows_pin_form(self) -> None:
        page = self.client.get("/settings/model")
        self.assertEqual(page.status_code, 200)
        self.assertIn("先确认是你", page.text)

    def test_save_without_sudo_is_refused(self) -> None:
        response = self.client.post("/settings/model", data={"api_key": "sk-nope"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("PIN", response.headers["location"])
        self.assertFalse(secret_store.has_secret(self.conn, "llm.api_key"))

    def test_sudo_then_save_then_effective(self) -> None:
        unlock = self.client.post("/settings/sudo", data={"pin": "123456"})
        self.assertEqual(unlock.status_code, 303)
        self.assertIn("la_sudo", unlock.cookies)
        saved = self.client.post(
            "/settings/model",
            data={"api_key": "sk-web-test-key", "model": "deepseek-flash"},
        )
        self.assertEqual(saved.status_code, 303)
        self.assertEqual(secret_store.get_secret(self.conn, "llm.api_key"), "sk-web-test-key")
        # 页面渲染:只出掩码,不出明文。
        page = self.client.get("/settings/model")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("sk-web-test-key", page.text)
        self.assertIn("sk-w…-key", page.text)

    def test_wrong_pin_is_rejected(self) -> None:
        response = self.client.post("/settings/sudo", data={"pin": "000000"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("PIN", response.headers["location"])

    def test_clear_button_removes_page_value(self) -> None:
        self.client.post("/settings/sudo", data={"pin": "123456"})
        self.client.post("/settings/model", data={"api_key": "sk-to-clear"})
        self.assertTrue(secret_store.has_secret(self.conn, "llm.api_key"))
        cleared = self.client.post("/settings/model", data={"clear": "llm.api_key"})
        self.assertEqual(cleared.status_code, 303)
        self.assertFalse(secret_store.has_secret(self.conn, "llm.api_key"))

    def test_non_admin_is_rejected(self) -> None:
        from fastapi.testclient import TestClient

        from la.web import auth as web_auth

        parent = member_store.create_member(self.conn, "家长", role="parent", pin="111111")
        token = web_auth.create_session(self.conn, parent.id)
        client = TestClient(self.client.app, follow_redirects=False)
        client.cookies.set(web_auth.SESSION_COOKIE, token)
        response = client.get("/settings/model")
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
