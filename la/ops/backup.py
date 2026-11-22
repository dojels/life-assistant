"""备份:快照 + 清单 + **可复跑的恢复演练**。

能力域 §9.5 的原话是"备份要两手,不是一手":`VACUUM INTO`(一致性快照)与持续复制
解决的是不同问题。而计划里还有更狠的一句:

> **每季度实际恢复一次,不能只看备份文件在不在。**

本模块把后半句做成了代码 —— `restore_drill()` 真的把备份还原到临时目录并逐项校验。
"备份文件存在"和"备份能用"是两件事。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..storage import db

MANIFEST_NAME = "manifest.json"
DEFAULT_KEEP = 14
DEFAULT_QUOTA_MB = 2048


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class BackupResult:
    directory: Path
    created_at: str
    databases: dict[str, int] = field(default_factory=dict)
    archive_files: int = 0
    archive_bytes: int = 0
    total_bytes: int = 0

    def as_dict(self) -> dict:
        return {
            "directory": str(self.directory),
            "created_at": self.created_at,
            "databases": dict(self.databases),
            "archive_files": self.archive_files,
            "archive_bytes": self.archive_bytes,
            "total_bytes": self.total_bytes,
        }

    def summary(self) -> str:
        lines = ["备份完成:" + str(self.directory)]
        for name, size in self.databases.items():
            lines.append("  " + name + "  " + str(round(size / 1024, 1)) + " KB")
        lines.append("  归档 " + str(self.archive_files) + " 个文件 / "
                     + str(round(self.archive_bytes / 1024, 1)) + " KB")
        lines.append("  合计 " + str(round(self.total_bytes / 1024 / 1024, 2)) + " MB")
        return "\n".join(lines)


def _vacuum_into(source: Path, target: Path) -> int:
    """一致性快照。VACUUM INTO 会在写入过程中持锁,所以拿到的一定是完整可用的库。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    conn = sqlite3.connect(str(source))
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()
    return target.stat().st_size


def backup_now(*, root: Path | None = None) -> BackupResult:
    base = Path(root) if root is not None else config.BACKUPS_DIR
    stamp = _stamp()
    target = base / stamp
    target.mkdir(parents=True, exist_ok=True)

    result = BackupResult(directory=target, created_at=stamp)
    for name, source in (("assistant.db", config.ASSISTANT_DB), ("memory.db", config.MEMORY_DB)):
        if Path(source).exists():
            result.databases[name] = _vacuum_into(Path(source), target / name)

    archive_target = target / "archive"
    archive_target.mkdir(exist_ok=True)
    for item in sorted(Path(config.ARCHIVE_DIR).glob("*.jsonl")):
        shutil.copy2(item, archive_target / item.name)
        result.archive_files += 1
        result.archive_bytes += item.stat().st_size

    result.total_bytes = sum(
        item.stat().st_size for item in target.rglob("*") if item.is_file()
    )
    manifest = {
        "created_at": stamp,
        "databases": result.databases,
        "archive_files": result.archive_files,
        "archive_bytes": result.archive_bytes,
        "total_bytes": result.total_bytes,
        "note": "VACUUM INTO 快照 + 归档 JSONL 副本;恢复演练见 restore_drill()",
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def list_backups(*, root: Path | None = None) -> list[dict]:
    base = Path(root) if root is not None else config.BACKUPS_DIR
    if not base.exists():
        return []
    found: list[dict] = []
    for item in sorted(base.iterdir(), reverse=True):
        if not item.is_dir():
            continue
        manifest_path = item / MANIFEST_NAME
        payload: dict = {"directory": str(item), "name": item.name}
        if manifest_path.exists():
            try:
                payload.update(json.loads(manifest_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                payload["manifest"] = "损坏"
        # 人看的字段在这里算,免得每个调用方各算一遍(算法还不一样)
        payload["size_mb"] = round(int(payload.get("total_bytes") or 0) / 1024 / 1024, 2)
        payload["created_at"] = payload.get("created_at") or item.name
        found.append(payload)
    return found


def prune(*, keep: int = DEFAULT_KEEP, root: Path | None = None) -> int:
    base = Path(root) if root is not None else config.BACKUPS_DIR
    if not base.exists():
        return 0
    directories = [item for item in sorted(base.iterdir(), reverse=True) if item.is_dir()]
    removed = 0
    for item in directories[keep:]:
        shutil.rmtree(item, ignore_errors=True)
        removed += 1
    return removed


@dataclass
class DrillReport:
    ok: bool = False
    backup: str = ""
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    restored_dir: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "backup": self.backup,
            "restored_dir": self.restored_dir,
            "checks": [{"name": n, "passed": p, "detail": d} for n, p, d in self.checks],
        }

    def render(self) -> str:
        lines = ["恢复演练(真的恢复了一份,不是看文件在不在)"]
        lines.append("  备份:" + (self.backup or "(没有)"))
        for name, passed, detail in self.checks:
            lines.append(("  ✅ " if passed else "  ❌ ") + name + " —— " + detail)
        lines.append("  结论:" + ("可用,这份备份能救命" if self.ok else "不可用,需要处理"))
        return "\n".join(lines)


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " AND name NOT LIKE '%_fts%'"
        ).fetchall()
    ]
    counts: dict[str, int] = {}
    for name in names:
        try:
            counts[name] = int(conn.execute("SELECT count(*) FROM " + name).fetchone()[0])
        except sqlite3.Error:
            continue
    return counts


def restore_drill(*, root: Path | None = None, live_assistant: Path | None = None) -> DrillReport:
    """⭐ 真的把最新备份还原一遍,并逐项校验它能不能用。

    只检查"备份文件在不在"是不够的 —— 一个损坏的快照同样"在"。
    """
    report = DrillReport()
    backups = list_backups(root=root)
    if not backups:
        report.checks.append(("存在备份", False, "一个备份都没有,先跑一次 backup_now()"))
        return report
    latest = Path(backups[0]["directory"])
    report.backup = str(latest)

    workdir = Path(tempfile.mkdtemp(prefix="la-restore-"))
    report.restored_dir = str(workdir)
    try:
        for name in ("assistant.db", "memory.db"):
            source = latest / name
            if not source.exists():
                report.checks.append((name + " 在备份里", False, "缺失"))
                continue
            shutil.copy2(source, workdir / name)
            report.checks.append((name + " 在备份里", True, str(source.stat().st_size) + " 字节"))

        for name in ("assistant.db", "memory.db"):
            target = workdir / name
            if not target.exists():
                continue
            conn = sqlite3.connect(str(target))
            try:
                verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
                report.checks.append((name + " 完整性", verdict == "ok", str(verdict)))
            finally:
                conn.close()

        # 行数比对:还原出来的库必须和线上库对得上
        live = Path(live_assistant) if live_assistant is not None else Path(config.ASSISTANT_DB)
        restored_path = workdir / "assistant.db"
        if live.exists() and restored_path.exists():
            live_conn = sqlite3.connect(str(live))
            restored_conn = sqlite3.connect(str(restored_path))
            try:
                live_counts = _table_counts(live_conn)
                restored_counts = _table_counts(restored_conn)
            finally:
                live_conn.close()
                restored_conn.close()
            mismatched = [
                name for name, count in live_counts.items()
                if restored_counts.get(name) != count
            ]
            report.checks.append(
                ("行数与线上一致", not mismatched,
                 "不一致的表:" + ",".join(mismatched[:5]) if mismatched else
                 str(len(live_counts)) + " 张表逐一对上")
            )

        # 还原出来的库必须能被**当前代码**读 —— 只看行数不查 schema,
        # 一次失败的迁移也能蒙混过关。
        if restored_path.exists():
            restored_conn = sqlite3.connect(str(restored_path))
            try:
                restored_conn.execute("SELECT count(*) FROM app_txns_live").fetchone()
                tables = restored_conn.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
                ).fetchone()[0]
                report.checks.append(
                    ("当前代码能读还原库", True,
                     "视图 app_txns_live 可用," + str(tables) + " 张表")
                )
            except sqlite3.Error as exc:
                report.checks.append(
                    ("当前代码能读还原库", False, type(exc).__name__ + ": " + str(exc)[:120])
                )
            finally:
                restored_conn.close()

        archive_dir = latest / "archive"
        files = sorted(archive_dir.glob("*.jsonl")) if archive_dir.exists() else []
        total_events = 0
        bad_lines = 0
        for item in files:
            with item.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        json.loads(line)
                        total_events += 1
                    except json.JSONDecodeError:
                        bad_lines += 1
        report.checks.append(
            ("归档可逐行解析", bad_lines == 0,
             str(len(files)) + " 个文件 / " + str(total_events) + " 条事件 / 坏行 "
             + str(bad_lines))
        )

        report.ok = all(passed for _name, passed, _detail in report.checks)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return report


def directory_size_mb(path: Path | None = None) -> float:
    base = Path(path) if path is not None else config.DATA_DIR
    if not base.exists():
        return 0.0
    total = sum(item.stat().st_size for item in base.rglob("*") if item.is_file())
    return round(total / 1024 / 1024, 2)
