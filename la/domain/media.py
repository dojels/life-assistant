"""媒体资产:内容寻址(sha256),数据库只存路径不存 BLOB。

理由(能力域 §2):SQLite 存大 BLOB 会让备份和查询都变慢,而文件系统天然支持去重
(同一张照片只存一份)与增量备份。所以库里只有 sha256 + 相对路径。

⚠️ 隐私:题目照片**永不出本机**(OCR 也在本地跑,见 M4.3b)。
"""

from __future__ import annotations
from ..base.clock import utc_now_iso as _utc_now

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..base.sniff import sniff_media_type  # noqa: F401  (对外仍从本模块导出)

DEFAULT_MEDIA_TYPE = "image/jpeg"
SUBDIR_PROBLEMS = "problems"
SUBDIR_RECEIPTS = "receipts"

_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    # 音频:听力题要 mp3,微信语音原件是 amr/silk —— 落盘时给对的扩展名,
    # 否则一律 .bin,以后想拿原件出来放都没法直接播(D139)。
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/amr": ".amr",
    "audio/silk": ".silk",
}




# ⚠️ sniff_media_type 的**实现**已经搬到基础层(la/base/sniff.py,M12):
#    图片、文件读取、账单导入都要认类型,而基础层不许 import 域 —— 所以实现放基础层,
#    这里只 import 回来。对外名字与行为不变,老调用点一行都不用改。


@dataclass
class Asset:
    id: int
    sha256: str
    rel_path: str
    media_type: str
    byte_size: int
    original_name: str | None = None

    def path(self, root: Path | None = None) -> Path:
        base = Path(root) if root is not None else config.MEDIA_DIR
        return base / self.rel_path

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "sha256": self.sha256,
            "rel_path": self.rel_path,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "original_name": self.original_name,
        }


def _row_to_asset(row: sqlite3.Row) -> Asset:
    return Asset(
        id=int(row["id"]),
        sha256=row["sha256"],
        rel_path=row["rel_path"],
        media_type=row["media_type"],
        byte_size=int(row["byte_size"]),
        original_name=row["original_name"],
    )


class MediaStore:
    def __init__(self, conn: sqlite3.Connection, *, root: Path | None = None) -> None:
        self.conn = conn
        self.root = Path(root) if root is not None else config.MEDIA_DIR

    # -- 写 ---------------------------------------------------------------
    def store_bytes(
        self,
        data: bytes,
        *,
        original_name: str | None = None,
        subdir: str = SUBDIR_PROBLEMS,
        media_type: str | None = None,
    ) -> Asset:
        """存一份二进制。相同内容的第二次上传直接复用已有记录(内容寻址去重)。"""
        if not data:
            raise ValueError("空文件")
        digest = hashlib.sha256(data).hexdigest()
        existing = self.get_by_sha256(digest)
        if existing is not None:
            return existing

        resolved_type = media_type or sniff_media_type(data)
        suffix = _EXTENSIONS.get(resolved_type, ".bin")
        # 分片目录,避免单目录几十万个文件
        rel_path = str(Path(subdir) / digest[:2] / (digest + suffix))
        target = self.root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)

        cursor = self.conn.execute(
            "INSERT INTO app_assets(sha256, rel_path, media_type, byte_size, original_name,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (digest, rel_path, resolved_type, len(data), original_name, _utc_now()),
        )
        self.conn.commit()
        return self.get(int(cursor.lastrowid or 0))

    def store_file(self, path: str | Path, *, subdir: str = SUBDIR_PROBLEMS) -> Asset:
        source = Path(path)
        if not source.exists():
            raise FileNotFoundError("文件不存在: " + str(source))
        return self.store_bytes(
            source.read_bytes(), original_name=source.name, subdir=subdir
        )

    # -- 读 ---------------------------------------------------------------
    def get(self, asset_id: int) -> Asset:
        row = self.conn.execute("SELECT * FROM app_assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            raise FileNotFoundError("资产不存在: " + str(asset_id))
        return _row_to_asset(row)

    def get_by_sha256(self, digest: str) -> Asset | None:
        row = self.conn.execute(
            "SELECT * FROM app_assets WHERE sha256 = ?", (digest,)
        ).fetchone()
        return _row_to_asset(row) if row else None

    def read_bytes(self, asset_id: int) -> bytes:
        asset = self.get(asset_id)
        return asset.path(self.root).read_bytes()

    def exists(self, asset_id: int) -> bool:
        try:
            return self.get(asset_id).path(self.root).exists()
        except FileNotFoundError:
            return False

    def total_bytes(self) -> int:
        row = self.conn.execute("SELECT COALESCE(SUM(byte_size), 0) FROM app_assets").fetchone()
        return int(row[0])
