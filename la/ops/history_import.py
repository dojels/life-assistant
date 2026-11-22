"""历史导入(M6.9):把既有聊天记录灌进来当初始记忆。

设计取舍:
  * **只接受结构化或半结构化的文本** —— 不做 PDF/截图/微信数据库解析,
    那些要么引重依赖,要么要逆向,收益不抵风险。
  * **同一个文件只导一次** —— 按 sha256 记账。重复导入是"没反应",
    而不是"记忆里多了一倍"。
  * **导入的是事件,不是事实** —— 事实要靠模型在真实对话里提炼。
    直接往 facts 里灌原文,只会让检索结果变成一团浆糊。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..memory.archive import now_iso
from ..memory.store import MemoryStore
from .paths import DEFAULT_POLICY

HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS sys_history_import (
    id          INTEGER PRIMARY KEY,
    source      TEXT    NOT NULL,
    sha256      TEXT    NOT NULL UNIQUE,
    session_id  TEXT    NOT NULL,
    lines       INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT    NOT NULL
) STRICT
"""

# 半结构化行:"用户: 内容" / "assistant: 内容" / "2026-01-01 10:00 用户: 内容"
ROLE_ALIASES = {
    "user": "user", "用户": "user", "我": "user", "human": "user", "me": "user",
    "assistant": "assistant", "助手": "assistant", "ai": "assistant", "bot": "assistant",
    "system": "system", "系统": "system",
}
SPEAKER = re.compile(
    r"^(?:(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\s+)?"
    r"(?P<role>[A-Za-z\u4e00-\u9fff]{1,10})\s*[:：]\s*(?P<text>.*)$"
)


class HistoryImportError(Exception):
    pass


@dataclass
class ImportReport:
    source: str
    session_id: str
    parsed: int = 0
    imported: int = 0
    skipped: int = 0
    already_done: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "session_id": self.session_id,
            "parsed": self.parsed, "imported": self.imported,
            "skipped": self.skipped, "already_done": self.already_done,
        }

    def summary(self) -> str:
        if self.already_done:
            return "这个文件之前已经导过了(sha256 相同),没有重复导入。"
        return ("导入完成:解析 " + str(self.parsed) + " 条,写入 " + str(self.imported)
                + " 条,跳过 " + str(self.skipped) + " 行(空行/无法识别)。")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_history(text: str) -> list[dict[str, Any]]:
    """把一段文本解析成 [{role, text, ts}]。解析不了的行直接丢,不猜。"""
    messages: list[dict[str, Any]] = []
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        payload: Any = None
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                role = ROLE_ALIASES.get(str(item.get("role") or "").lower())
                body = str(item.get("content") or item.get("text") or "").strip()
                if role and body:
                    messages.append({"role": role, "text": body,
                                     "ts": item.get("ts") or item.get("time")})
            return messages
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = SPEAKER.match(line)
        if match is None:
            continue
        role = ROLE_ALIASES.get(match.group("role").lower())
        body = match.group("text").strip()
        if role and body:
            messages.append({"role": role, "text": body, "ts": match.group("ts")})
    return messages


def import_history(
    store: "MemoryStore", path: str | Path, *, session_id: str = "imported",
) -> ImportReport:
    """导入一个历史记录文件。同一个文件第二次调用直接返回"已导过"。

    ⚠️ 必须走 MemoryStore.append —— 它先把事件写进归档 JSONL(事实源),
    再更新镜像表。直接 INSERT 进 events 表会让"记忆可重建"这个不变量当场失效:
    重建时这些事件会全部消失。
    """
    resolved = DEFAULT_POLICY.check(path, must_exist=True)
    conn = store.conn
    conn.execute(HISTORY_TABLE)

    digest = _file_sha256(resolved)
    existing = conn.execute(
        "SELECT lines FROM sys_history_import WHERE sha256 = ?", (digest,)
    ).fetchone()
    if existing is not None:
        return ImportReport(source=str(resolved), session_id=session_id,
                            imported=int(existing[0]), already_done=True)

    text = resolved.read_text(encoding="utf-8", errors="replace")
    messages = parse_history(text)
    if not messages:
        raise HistoryImportError(
            "没能从 " + resolved.name + " 里解析出任何对话。"
            "支持两种格式:JSON(array of {role, content})或每行「用户: 内容」。"
        )

    stamp = now_iso()
    for message in messages:
        # ⚠️ 用**已有的事件类型**,不新造一种。归档是事实源,
        # 加一种类型意味着所有回放/投影路径都要跟着改(而且很难想起来改)。
        event_type = {
            "user": "user/message", "assistant": "assistant/message",
            "system": "memory/note",
        }[message["role"]]
        store.append(
            session_id,
            event_type,
            {"role": message["role"], "content": message["text"],
             "imported": True, "source": resolved.name},
            surface=False,  # 历史不进热摘要 —— 否则一导入就把上下文塞满
            time=message.get("ts"),
        )

    conn.execute(
        "INSERT INTO sys_history_import(source, sha256, session_id, lines, imported_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (resolved.name, digest, session_id, len(messages), stamp),
    )
    conn.commit()
    return ImportReport(source=str(resolved), session_id=session_id,
                        parsed=len(messages), imported=len(messages),
                        skipped=max(len(text.splitlines()) - len(messages), 0))


__all__ = ["HistoryImportError", "ImportReport", "import_history", "parse_history"]
