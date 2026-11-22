"""M12 界面细节:产物那条回执给的是「下载」,不是「撤销」。

⭐ 用户的原话:"产物后面应该给的是下载按钮,而不是撤销按钮。只有领域内的各种写入操作,
   给的才是撤销按钮。" —— 产物是可再生的结果(随时能重出),拿到它第一件想做的事是下载;
   领域写入(记一笔账、录一道错题)才有"撤销"这个动作。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from la import config
from la.domain.receipts import DOMAIN_LEDGER, ReceiptLog
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT

BASE_DIR = Path(__file__).resolve().parent.parent


class TestReceiptCardButtons(unittest.TestCase):
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

        # 先在库里放两条回执:一条产物、一条领域写入
        conn = db.connect(config.ASSISTANT_DB)
        db.migrate(conn, MIGRATIONS_ASSISTANT)
        log = ReceiptLog(conn)
        log.record(kind="artifact.file", domain="artifact", label="产物:清单.md",
                   detail={"artifact_id": "abc123", "name": "清单.md", "bytes": 12},
                   undo_plan={"type": "artifact", "artifact_id": "abc123"})
        log.record(kind="ledger.txn", domain=DOMAIN_LEDGER, label="记一笔:午饭 38 元",
                   detail={"amount_text": "38.00 元"})
        conn.commit()
        conn.close()

        from fastapi.testclient import TestClient

        from la.web import deps as web_deps
        from la.web.app import create_app

        self._real_factory = web_deps.build_model_client
        web_deps.build_model_client = lambda settings: _StubClient()
        try:
            self.client = TestClient(create_app(), follow_redirects=False)
        finally:
            web_deps.build_model_client = self._real_factory

    def tearDown(self) -> None:
        for key, value in self._patched.items():
            setattr(config, key, value)
        config._settings = self._settings_cache  # noqa: SLF001
        self._tmp.cleanup()

    def test_artifact_receipt_offers_download(self) -> None:
        page = self.client.get("/chat")
        self.assertEqual(page.status_code, 200)
        self.assertIn('class="receipt-download"', page.text)
        self.assertIn('href="/artifacts/abc123"', page.text)

    def test_domain_receipt_still_offers_undo(self) -> None:
        self.assertIn("撤销这次写入", self.client.get("/chat").text)

    def test_js_uses_the_same_rule_for_streamed_cards(self) -> None:
        """流式那一路是 JS 现拼的卡片 —— 两条路必须同一条规则,否则刷新前后不一致。"""
        script = (BASE_DIR / "la" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("item.kind === 'artifact.file'", script)
        self.assertIn("link.className = 'receipt-download'", script)


class _StubClient:
    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        from la.llm.client import ChatResult

        return ChatResult(text="好的", model="stub")


if __name__ == "__main__":
    unittest.main()
