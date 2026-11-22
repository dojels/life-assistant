"""隐私自查(M6.7):把"数据都去哪了"变成一张可以自己看的表。

设计原则里说"三层数据分离",但**分离本身不产生信任,可核对才产生信任**。
这个模块回答三个问题:
  1. 数据文件都在哪、多大?
  2. 哪些东西会被发到模型 API?哪些永远不会?
  3. 备份在哪、密钥在不在库里?
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from .backup import directory_size_mb, list_backups
from .credentials import audit as credential_audit

# 出网判定:这三张表的数据会被拼进请求体。
# ⚠️ 只是"可能" —— 投影预算会砍,但设计上必须假设**它们会被发出去**。
OUTBOUND_TABLES = ("events", "facts", "summaries")
# 这些表**永不**出网:完整账本、媒体原件、审计明细。
LOCAL_ONLY_TABLES = (
    "app_txns", "app_postings", "app_measurements", "app_workouts",
    "app_review_units", "app_review_logs", "ops_audit", "ops_undo_log",
)


@dataclass
class PrivacyReport:
    data_dir: str
    size_mb: float
    files: list[dict[str, Any]] = field(default_factory=list)
    outbound_tables: list[dict[str, Any]] = field(default_factory=list)
    local_only_tables: list[dict[str, Any]] = field(default_factory=list)
    backups: list[dict[str, Any]] = field(default_factory=list)
    secret_in_db: bool = False
    credential_source: str = ""
    credential_preview: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "data_dir": self.data_dir, "size_mb": self.size_mb, "files": self.files,
            "outbound_tables": self.outbound_tables, "local_only_tables": self.local_only_tables,
            "backups": self.backups, "secret_in_db": self.secret_in_db,
            "credential_source": self.credential_source, "notes": self.notes,
        }

    def render(self) -> str:
        lines = ["【隐私与数据自查】", "数据目录:" + self.data_dir
                 + "(" + str(self.size_mb) + " MB)"]
        lines.append("  文件:")
        for item in self.files:
            lines.append("    " + item["name"] + "  " + str(item["size_mb"]) + " MB"
                         + ("  ← 事实源,不可重建" if item.get("irreplaceable") else ""))
        lines.append("  会随请求发给模型 API 的表(可能被投影,但按最坏情况算):")
        for item in self.outbound_tables:
            lines.append("    " + item["table"] + "  " + str(item["rows"]) + " 行")
        lines.append("  永不出网的本地表:")
        lines.append("    " + "、".join(item["table"] + "(" + str(item["rows"]) + ")"
                                       for item in self.local_only_tables))
        lines.append("  密钥来源:" + (self.credential_source or "未配置"))
        lines.append("  密钥/身份证落库:" + ("⚠️ 有" if self.secret_in_db else "无"))
        lines.append("  备份:" + (str(len(self.backups)) + " 份,最近 "
                                  + (self.backups[0]["name"] if self.backups else "无")
                                  if self.backups else "还没有 —— 现在就跑一次 --backup"))
        for note in self.notes:
            lines.append("  ⚠️ " + note)
        return "\n".join(lines)


def _table_rows(conn: sqlite3.Connection, names: tuple[str, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in names:
        try:
            count = conn.execute("SELECT count(*) FROM " + name).fetchone()[0]
        except sqlite3.Error:
            continue
        out.append({"table": name, "rows": int(count)})
    return out


def build_privacy_report(
    *, assistant: sqlite3.Connection, memory: sqlite3.Connection
) -> PrivacyReport:
    report = PrivacyReport(data_dir=str(config.DATA_DIR),
                           size_mb=directory_size_mb(config.DATA_DIR))
    for path in sorted(config.DATA_DIR.glob("*")):
        if path.is_file():
            report.files.append({
                "name": path.name,
                "size_mb": round(path.stat().st_size / 1024 / 1024, 3),
                "irreplaceable": path.suffix == ".db" and path.name == "assistant.db",
            })
    for name in ("archive", "media", "backups"):
        folder = config.DATA_DIR / name
        if folder.is_dir():
            report.files.append({
                "name": name + "/",
                "size_mb": directory_size_mb(folder),
                "irreplaceable": name == "media",
            })

    report.outbound_tables = _table_rows(memory, OUTBOUND_TABLES)
    report.local_only_tables = _table_rows(assistant, LOCAL_ONLY_TABLES)

    report.backups = [item.as_dict() if hasattr(item, "as_dict") else dict(item)
                      for item in list_backups()]

    from .guardrails import check_secret_leak

    report.secret_in_db = check_secret_leak(assistant, memory).status == "fail"
    for item in credential_audit(assistant):
        if item.name == "DEEPSEEK_API_KEY":
            report.credential_source = item.source
            report.notes.append(
                "密钥全部以掩码示人:" + (item.preview or "(未配置)")
                + "(来源 " + item.source + ")"
            )
    if not report.backups:
        report.notes.append("还没有备份 —— 单点故障。跑 python -m la --backup")
    return report


__all__ = ["PrivacyReport", "build_privacy_report"]
