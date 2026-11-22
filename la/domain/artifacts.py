"""产物层:助手**做出来**、要交到用户手上的东西(错题卷 / 统计 / 清单)。

⭐ 与素材层的方向正好相反,所以两张表、两个目录、两套生命周期:

    素材层 data/media/materials/   用户 → 助手   原件,**不删**
    产物层 data/artifacts/          助手 → 用户   结果,可按保留期清理

⚠️ 三条从云朵项目(who 真机上踩过)照搬的纪律:

  1. **落地即登记,登记即归属**。文件写完**先**写库,再返回任何链接/句柄 ——
     反过来的话,用户拿到链接那一刻文件还没登记,下载会被归属闸按 404 拒掉。
  2. **句柄 = "f" + sha1(相对路径)[:12]**,登记 / 查询 / 下载**共用同一个算法**。
     各写一遍,就会出现"登记了却查不到归属"。
  3. **文件不在了就把登记删掉,只按存在性判、绝不按时间判** ——
     这张表同时是下载的判据,按时间删早了用户连自己的产物都打不开。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .. import config
from ..memory.archive import now_iso

# ⚠️ 单一来源(2026-10-17 审计):这个串原先在 8 个模块各写一份。
#    改 .env 的 LA_TIMEZONE 而这里不动,就会出现"提示词说伦敦、提醒按上海响"。
#    真正决定每个服务用哪个时区的是**构造参数 tzid**(由 build_services 注入 settings.timezone),
#    这里只是"没注入时的兜底"。
DEFAULT_TZID = config.DEFAULT_TIMEZONE

KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_SHEET = "sheet"      # 错题卷 / 复习清单这类"可打印"的东西
KIND_DATA = "data"        # json/csv
KIND_FILE = "file"
#: 生成网页(M13)。单独一个 kind 的理由:它不是"一段文本",而是一个**能打开的页面** ——
#: 「素材」页与聊天回执按它给『预览』入口(沙箱路由见 web/app.py 的 /generated/<id>)。
#: ⚠️ 基础层的 html_page.py 里有一个同名常量 PAGE_KIND(基础层不 import 域),两处要一起改。
KIND_HTML = "html"

MEDIA_TYPES = {
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".ics": "text/calendar; charset=utf-8",
    # M14d:音频(听写/听力题的产物)。⭐ 少了这几行,register_bytes 会按"认不出"
    # 落成 application/octet-stream —— 浏览器不当音频、页面内联器也认不出它。
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    # M12:助手能直接产出 Word / Excel(正式文档观感)。MIME 要写对,
    # 不然浏览器会把 .docx 当纯文本打开 —— 那是"下载了但打不开"的另一种表现。
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

#: 文件名里只允许这些字符 —— 产物名会进 URL,不能带路径分隔符或控制字符
_SAFE_NAME = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


class ArtifactError(RuntimeError):
    """产物层的领域错误。"""


def artifact_id_for(rel_path: str) -> str:
    """句柄算法:**唯一实现**。登记、查询、下载三处都必须调它。"""
    digest = hashlib.sha1(str(rel_path).encode("utf-8")).hexdigest()
    return "f" + digest[:12]


def safe_filename(name: str, *, fallback: str = "artifact") -> str:
    """把任意标题收敛成安全文件名(保留中文,去掉路径与怪字符)。"""
    text = _SAFE_NAME.sub("_", str(name or "").strip())
    text = text.strip("._-")
    if not text:
        text = fallback
    return text[:60]


def media_type_for(path: str | Path) -> str:
    return MEDIA_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def kind_for(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".webp"):
        return KIND_IMAGE
    if suffix in (".csv", ".json"):
        return KIND_DATA
    if suffix in (".html", ".htm"):
        return KIND_HTML
    if suffix in (".md", ".txt", ".ics"):
        return KIND_TEXT
    if suffix == ".pdf":
        return KIND_SHEET
    return KIND_FILE


@dataclass
class Artifact:
    id: str
    kind: str
    rel_path: str
    label: str
    media_type: str
    byte_size: int
    meta: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    created_at: str = ""

    @property
    def name(self) -> str:
        return Path(self.rel_path).name

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "rel_path": self.rel_path,
            "label": self.label,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "meta": dict(self.meta),
            "session_id": self.session_id,
            "created_at": self.created_at,
        }


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    try:
        meta = json.loads(row["meta"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    return Artifact(
        id=row["id"],
        kind=row["kind"],
        rel_path=row["rel_path"],
        label=row["label"],
        media_type=row["media_type"],
        byte_size=int(row["byte_size"] or 0),
        meta=meta if isinstance(meta, dict) else {},
        session_id=row["session_id"],
        created_at=row["created_at"],
    )


class ArtifactStore:
    """产物的落盘与登记。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        root: Path | None = None,
        now_provider: Callable[[], datetime] | None = None,
        tzid: str = DEFAULT_TZID,
    ) -> None:
        self.conn = conn
        self.root = Path(root) if root is not None else config.ARTIFACTS_DIR
        self._now = now_provider
        self.tzid = tzid

    def _moment(self) -> datetime:
        if self._now is not None:
            return self._now()
        return datetime.now(ZoneInfo(self.tzid))

    # -- 写 ---------------------------------------------------------------
    def register_bytes(
        self,
        data: bytes,
        *,
        filename: str,
        label: str = "",
        kind: str | None = None,
        media_type: str | None = None,
        session_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Artifact:
        """落盘 + 登记。⭐ 顺序不能反(见模块开头第 1 条纪律)。"""
        if not data:
            raise ArtifactError("产物内容是空的")
        stamp = self._moment().strftime("%Y%m%d-%H%M%S")
        name = safe_filename(filename)
        rel_path = str(Path(stamp[:8]) / (stamp + "-" + name))
        target = self.root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        # 原子写:先写 .tmp 再 replace —— 半截文件被登记出去比没有更糟
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        return self.register_path(
            target, rel_path=rel_path, label=label or name,
            kind=kind, media_type=media_type, session_id=session_id, meta=meta,
        )

    def register_text(
        self, text: str, *, filename: str, label: str = "", **kwargs: Any
    ) -> Artifact:
        payload = text.encode("utf-8")
        # ⚠️ 不在这里写 media_type:它由**文件名后缀**决定(.md 与 .txt 不一样)。
        #    硬塞 text/plain 会让 markdown 在浏览器里变成一堆纯文本。
        kwargs.setdefault("kind", KIND_TEXT)
        return self.register_bytes(payload, filename=filename, label=label, **kwargs)

    def register_path(
        self,
        path: str | Path,
        *,
        rel_path: str | None = None,
        label: str = "",
        kind: str | None = None,
        media_type: str | None = None,
        session_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Artifact:
        """登记一个**已经在产物目录里**的文件。

        ⚠️ 落点铁律:必须在 self.root 里面。外面的文件不登记 ——
        宁可不给句柄,也不给一个下载会 404 的链接。
        """
        target = Path(path).resolve()
        root = self.root.resolve()
        try:
            relative = target.relative_to(root)
        except ValueError as exc:
            raise ArtifactError(
                "产物必须落在产物目录里(现在是 " + str(target) + ",要求 " + str(root) + ")"
            ) from exc
        if rel_path is not None:
            relative = Path(rel_path)
        if not target.exists():
            raise ArtifactError("产物文件不存在: " + str(target))
        artifact_id = artifact_id_for(str(relative))
        resolved_meta = dict(meta or {})
        existing = self.conn.execute(
            "SELECT meta FROM app_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if existing is not None:
            # ⚠️ meta **合并**而不是覆盖:同一路径重新导出时,别把上一次写进去的信息抹掉
            try:
                previous = json.loads(existing["meta"] or "{}")
            except json.JSONDecodeError:
                previous = {}
            if isinstance(previous, dict):
                previous.update(resolved_meta)
                resolved_meta = previous
        self.conn.execute(
            "INSERT INTO app_artifacts(id, kind, rel_path, label, media_type, byte_size,"
            " meta, session_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET label = excluded.label,"
            " media_type = excluded.media_type, byte_size = excluded.byte_size,"
            " meta = excluded.meta, session_id = COALESCE(excluded.session_id, session_id)",
            (
                artifact_id,
                kind or kind_for(relative),
                str(relative),
                label or Path(relative).name,
                media_type or media_type_for(relative),
                target.stat().st_size,
                json.dumps(resolved_meta, ensure_ascii=False, default=str),
                session_id,
                now_iso(),
            ),
        )
        self.conn.commit()
        return self.get(artifact_id)

    # -- 读 ---------------------------------------------------------------
    def get(self, artifact_id: str) -> Artifact:
        row = self.conn.execute(
            "SELECT * FROM app_artifacts WHERE id = ?", (str(artifact_id),)
        ).fetchone()
        if row is None:
            raise ArtifactError("产物不存在: " + str(artifact_id))
        return _row_to_artifact(row)

    def list(
        self, *, session_id: str | None = None, kind: str | None = None,
        limit: int = 30, offset: int = 0,
    ) -> list[Artifact]:
        """按时间倒序取一页产物。⭐ offset 是给翻页用的(2026-10-24:产物到 1000 个
        也不能靠无限下拉,页面得有边界)。"""
        sql = "SELECT * FROM app_artifacts"
        where: list[str] = []
        params: list[Any] = []
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
        params.extend((max(1, int(limit)), max(0, int(offset))))
        return [_row_to_artifact(row) for row in self.conn.execute(sql, params).fetchall()]

    def count(self, *, session_id: str | None = None, kind: str | None = None) -> int:
        """同筛选条件下的总数(翻页要它算页数)。"""
        sql = "SELECT COUNT(*) AS n FROM app_artifacts"
        where: list[str] = []
        params: list[Any] = []
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        row = self.conn.execute(sql, params).fetchone()
        return int(row["n"] or 0) if row is not None else 0

    def path_of(self, artifact: Artifact | str) -> Path:
        item = self.get(artifact) if isinstance(artifact, str) else artifact
        return self.root / item.rel_path

    def exists(self, artifact: Artifact | str) -> bool:
        try:
            return self.path_of(artifact).exists()
        except (ArtifactError, OSError):
            return False

    def read_bytes(self, artifact: Artifact | str) -> bytes:
        return self.path_of(artifact).read_bytes()

    def delete(self, artifact: Artifact | str) -> bool:
        """删掉一个产物(文件 + 登记)。**撤销**走这里。

        ⚠️ 与素材层刻意不对称:产物是可再生的(清单/报表随时能重出),所以真删;
        而用户给我们的原件在 materials 那一侧**没有**这个方法。
        """
        try:
            item = self.get(artifact) if isinstance(artifact, str) else artifact
        except ArtifactError:
            return False
        path = self.root / item.rel_path
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            raise ArtifactError("删不掉这个产物文件:" + str(exc)) from exc
        self.conn.execute("DELETE FROM app_artifacts WHERE id = ?", (item.id,))
        self.conn.commit()
        return True

    # -- 维护 -------------------------------------------------------------
    def forget_missing(self) -> int:
        """把"文件已经不在了"的登记删掉。

        ⚠️ 只按**存在性**判,绝不按时间判 —— 这张表同时是下载的判据。
        """
        removed = 0
        for artifact in self.list(limit=500):
            if not self.exists(artifact):
                self.conn.execute("DELETE FROM app_artifacts WHERE id = ?", (artifact.id,))
                removed += 1
        if removed:
            self.conn.commit()
        return removed

    def prune(self, *, days: int = 30) -> int:
        """按保留期清产物(默认 30 天,**只动产物层**)。

        ⚠️ 与素材层完全不同:用户给我们的原件**一份都不删**(materials 表没有 prune)。
        这里的取舍是"助手生成的清单类文件会越积越多",而它们随时可以重新生成。
        """
        if days <= 0:
            return 0
        seconds = int(days) * 86400
        now_stamp = self._moment().timestamp()
        removed = 0
        for artifact in self.list(limit=1000):
            path = self.path_of(artifact)
            try:
                # 按 mtime 判龄:Windows 的 ctime 是创建时间,拿它判会把"刚重写过"的当旧的删
                if path.exists() and (now_stamp - path.stat().st_mtime) < seconds:
                    continue
                if path.exists():
                    path.unlink()
            except OSError:
                continue
            self.conn.execute("DELETE FROM app_artifacts WHERE id = ?", (artifact.id,))
            removed += 1
        if removed:
            self.conn.commit()
        return removed

    def stats(self) -> dict[str, Any]:
        total = int(self.conn.execute("SELECT count(*) FROM app_artifacts").fetchone()[0])
        size = int(
            self.conn.execute("SELECT COALESCE(SUM(byte_size), 0) FROM app_artifacts").fetchone()[0]
        )
        return {"total": total, "bytes": size}
