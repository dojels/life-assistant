"""素材层:用户从任何渠道送来的原件,先原样落下来,再谈"它是什么"。

⭐ 这一层的存在理由只有一句:**别让"没认出来"等于"丢掉了"**。

    手机拍一张错题 ──► 微信 ──► 桥接层 ──► 素材层(字节 + 来源 + 去向)
                                              │
                                              ├─ 识别成功 ──► 错题本(handled=recorded)
                                              ├─ 不是题目 ──► 留档    (handled=stored)
                                              └─ 识别失败 ──► 留档    (handled=failed)

⚠️ 三条必须守住的:
  1. **渠道幂等**(channel + external_id 唯一)。微信会重发回调;同一条消息入两次素材,
     后面就是"同一张错题照片录了两道题"。
  2. **字节仓与素材表分工**:同一个文件只存一份(MediaStore 内容寻址),
     素材行记的是**来源与去向**(谁发的、哪条消息、最后变成了什么)。
  3. **不删用户的东西**。这里不是缓存,是原件。清理策略只管产物层(见 artifacts.py)。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .. import config
from ..memory.archive import now_iso
from .media import MediaStore, sniff_media_type

# ⚠️ 单一来源(2026-10-17 审计):这个串原先在 8 个模块各写一份。
#    改 .env 的 LA_TIMEZONE 而这里不动,就会出现"提示词说伦敦、提醒按上海响"。
#    真正决定每个服务用哪个时区的是**构造参数 tzid**(由 build_services 注入 settings.timezone),
#    这里只是"没注入时的兜底"。
DEFAULT_TZID = config.DEFAULT_TIMEZONE
SUBDIR_MATERIALS = "materials"

CHANNEL_WEIXIN = "weixin"
CHANNEL_WEB = "web"
CHANNEL_CLI = "cli"
CHANNEL_LOCAL = "local"

KIND_IMAGE = "image"
KIND_FILE = "file"
KIND_VOICE = "voice"
KIND_TEXT = "text"

#: 素材的三种归宿 + 两种失败态。界面与回话都按它措辞,不另造一套词。
HANDLED_PENDING = "pending"
HANDLED_RECORDED = "recorded"     # 变成了错题
HANDLED_STORED = "stored"         # 只是留档(认出来不是题目 / 不是图片)
HANDLED_IGNORED = "ignored"       # 用户明确说不要
HANDLED_FAILED = "failed"         # 识别失败,但原件还在

HANDLED_LABELS = {
    HANDLED_PENDING: "待处理",
    HANDLED_RECORDED: "已录成错题",
    HANDLED_STORED: "已留档",
    HANDLED_IGNORED: "已忽略",
    HANDLED_FAILED: "识别失败(原件还在)",
}


class MaterialError(RuntimeError):
    """素材层的领域错误。"""


def kind_of(media_type: str) -> str:
    """按 MIME 归类。认不出来的一律 file —— **不猜**。"""
    text = (media_type or "").lower()
    if text.startswith("image/"):
        return KIND_IMAGE
    if text.startswith("audio/") or text.startswith("voice/"):
        return KIND_VOICE
    if text.startswith("text/"):
        return KIND_TEXT
    return KIND_FILE


@dataclass
class Material:
    id: int
    channel: str
    external_id: str | None
    sender: str | None
    thread_id: str | None
    asset_id: int | None
    original_name: str | None
    media_type: str
    byte_size: int
    sha256: str
    kind: str
    note: str | None
    student_id: int | None
    handled: str
    result: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @property
    def is_image(self) -> bool:
        return self.kind == KIND_IMAGE

    @property
    def handled_label(self) -> str:
        return HANDLED_LABELS.get(self.handled, self.handled)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "external_id": self.external_id,
            "sender": self.sender,
            "thread_id": self.thread_id,
            "asset_id": self.asset_id,
            "original_name": self.original_name,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "kind": self.kind,
            "note": self.note,
            "student_id": self.student_id,
            "handled": self.handled,
            "handled_label": self.handled_label,
            "result": dict(self.result),
            "created_at": self.created_at,
        }


def _row_to_material(row: sqlite3.Row) -> Material:
    try:
        result = json.loads(row["result_json"] or "{}")
    except json.JSONDecodeError:
        result = {}
    return Material(
        id=int(row["id"]),
        channel=row["channel"],
        external_id=row["external_id"],
        sender=row["sender"],
        thread_id=row["thread_id"],
        asset_id=int(row["asset_id"]) if row["asset_id"] is not None else None,
        original_name=row["original_name"],
        media_type=row["media_type"],
        byte_size=int(row["byte_size"] or 0),
        sha256=row["sha256"] or "",
        kind=row["kind"],
        note=row["note"],
        student_id=int(row["student_id"]) if row["student_id"] is not None else None,
        handled=row["handled"],
        result=result if isinstance(result, dict) else {},
        created_at=row["created_at"],
    )


class MaterialService:
    """素材的入库、查询与"归宿"标记。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        media: MediaStore | None = None,
        now_provider: Callable[[], datetime] | None = None,
        tzid: str = DEFAULT_TZID,
    ) -> None:
        self.conn = conn
        self.media = media or MediaStore(conn)
        self._now = now_provider
        self.tzid = tzid

    def _moment(self) -> datetime:
        if self._now is not None:
            return self._now()
        return datetime.now(ZoneInfo(self.tzid))

    # -- 写 ---------------------------------------------------------------
    def ingest_bytes(
        self,
        data: bytes,
        *,
        channel: str = CHANNEL_LOCAL,
        external_id: str | None = None,
        sender: str | None = None,
        thread_id: str | None = None,
        original_name: str | None = None,
        media_type: str | None = None,
        kind: str | None = None,
        note: str | None = None,
        student_id: int | None = None,
    ) -> tuple[Material, bool]:
        """收下一份原件。返回 (素材, 是不是新收的)。

        ⭐ 幂等先查:`(channel, external_id)` 命中就直接把已有那条还回去 ——
        渠道重发不会变成两条素材,更不会变成两道重复的错题。
        """
        if not data:
            raise MaterialError("空文件")
        if external_id:
            existing = self.find(channel=channel, external_id=external_id)
            if existing is not None:
                return existing, False

        resolved_type = media_type or sniff_media_type(data)
        asset = self.media.store_bytes(
            data, original_name=original_name, subdir=SUBDIR_MATERIALS,
            media_type=resolved_type,
        )
        resolved_kind = kind or kind_of(resolved_type)
        cursor = self.conn.execute(
            "INSERT INTO app_materials(channel, external_id, sender, thread_id, asset_id,"
            " original_name, media_type, byte_size, sha256, kind, note, student_id, handled,"
            " result_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)",
            (
                channel, external_id, sender, thread_id, asset.id, original_name,
                resolved_type, len(data), asset.sha256, resolved_kind, note, student_id,
                HANDLED_PENDING, now_iso(),
            ),
        )
        self.conn.commit()
        return self.get(int(cursor.lastrowid or 0)), True

    def ingest_path(self, path: str | Path, **kwargs: Any) -> tuple[Material, bool]:
        source = Path(path)
        if not source.exists():
            raise MaterialError("文件不存在: " + str(source))
        kwargs.setdefault("original_name", source.name)
        return self.ingest_bytes(source.read_bytes(), **kwargs)

    def mark(
        self,
        material_id: int,
        *,
        handled: str,
        result: dict[str, Any] | None = None,
        note: str | None = None,
        student_id: int | None = None,
    ) -> Material:
        """记下这份素材最后变成了什么。**只改归宿,不动字节。**"""
        if handled not in HANDLED_LABELS:
            raise MaterialError("不认识的归宿: " + str(handled))
        fields = ["handled = ?"]
        params: list[Any] = [handled]
        if result is not None:
            fields.append("result_json = ?")
            params.append(json.dumps(result, ensure_ascii=False, default=str))
        if note is not None:
            fields.append("note = ?")
            params.append(note)
        if student_id is not None:
            fields.append("student_id = ?")
            params.append(int(student_id))
        params.append(int(material_id))
        self.conn.execute(
            "UPDATE app_materials SET " + ", ".join(fields) + " WHERE id = ?", params
        )
        self.conn.commit()
        return self.get(int(material_id))

    # -- 读 ---------------------------------------------------------------
    def get(self, material_id: int) -> Material:
        row = self.conn.execute(
            "SELECT * FROM app_materials WHERE id = ?", (int(material_id),)
        ).fetchone()
        if row is None:
            raise MaterialError("素材不存在: " + str(material_id))
        return _row_to_material(row)

    def find(self, *, channel: str, external_id: str) -> Material | None:
        row = self.conn.execute(
            "SELECT * FROM app_materials WHERE channel = ? AND external_id = ?",
            (channel, external_id),
        ).fetchone()
        return _row_to_material(row) if row is not None else None

    def list(
        self,
        *,
        channel: str | None = None,
        kind: str | None = None,
        handled: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Material]:
        sql = "SELECT * FROM app_materials"
        where: list[str] = []
        params: list[Any] = []
        if channel:
            where.append("channel = ?")
            params.append(channel)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if handled:
            where.append("handled = ?")
            params.append(handled)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, int(limit)), max(0, int(offset))])
        return [_row_to_material(row) for row in self.conn.execute(sql, params).fetchall()]

    def recent_for_thread(self, thread_id: str, *, limit: int = 5) -> list[Material]:
        rows = self.conn.execute(
            "SELECT * FROM app_materials WHERE thread_id = ? ORDER BY id DESC LIMIT ?",
            (thread_id, int(limit)),
        ).fetchall()
        return [_row_to_material(row) for row in rows]

    def latest(self) -> Material | None:
        """最近一份素材 —— 聊天里说"刚才那张"时,指的就是它。"""
        row = self.conn.execute(
            "SELECT * FROM app_materials ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _row_to_material(row) if row is not None else None

    def path_of(self, material: Material | int) -> Path:
        item = self.get(material) if isinstance(material, int) else material
        if item.asset_id is None:
            raise MaterialError("这份素材没有字节(asset_id 为空)")
        return self.media.get(item.asset_id).path(self.media.root)

    def read_bytes(self, material: Material | int) -> bytes:
        return self.path_of(material).read_bytes()

    def stats(self) -> dict[str, Any]:
        total = int(self.conn.execute("SELECT count(*) FROM app_materials").fetchone()[0])
        by_handled = {
            row["handled"]: int(row["n"])
            for row in self.conn.execute(
                "SELECT handled, count(*) AS n FROM app_materials GROUP BY handled"
            ).fetchall()
        }
        by_channel = {
            row["channel"]: int(row["n"])
            for row in self.conn.execute(
                "SELECT channel, count(*) AS n FROM app_materials GROUP BY channel"
            ).fetchall()
        }
        return {"total": total, "by_handled": by_handled, "by_channel": by_channel}
