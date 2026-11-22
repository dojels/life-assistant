"""SQLite 连接、PRAGMA 与事务。

要点(执行计划 §4.2 / 能力域 §9.5):
  * PRAGMA 写死在 config.SQLITE_PRAGMAS,不允许业务代码零散设置。
  * autocommit 显式写死为 LEGACY_TRANSACTION_CONTROL:Python 计划在未来版本
    把默认值改为 False,显式声明可避免升级解释器时事务行为突变。
  * 两个库:assistant.db(领域 + 运维,不可重建)、memory.db(记忆,可从归档重建)。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .. import config
from .migrations import (
    MIGRATION_LOCK,
    MIGRATIONS_ASSISTANT,
    MIGRATIONS_MEMORY,
    migrate,
)

__all__ = [
    "connect",
    "connect_assistant",
    "connect_memory",
    "migrate",
    "set_member",
    "transaction",
    "pragma_snapshot",
    "table_names",
]


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开一个连接并应用全部 PRAGMA。

    ⚠️ check_same_thread 默认 **True**(从严):有人不小心把连接丢给别的线程时,
    立刻炸出 ProgrammingError,而不是让两个线程悄悄共用一个连接。
    只有 Web 层需要关掉它 —— 原因见 la/web/deps.py 与决策记录 D66。
    """
    conn = sqlite3.connect(
        str(path),
        timeout=5.0,
        autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL,
        check_same_thread=check_same_thread,
    )
    conn.row_factory = sqlite3.Row
    #: 领域可见性视图靠这个函数取"当前成员";默认 None = 管理员视角(全看)。
    #: 每个连接独立注册,不会把成员身份写进共享库。
    conn.create_function("current_member", 0, lambda: None)
    _apply_pragmas(conn)
    return conn


def set_member(conn: sqlite3.Connection, member_id: int | None) -> None:
    """给这个连接设置当前成员(领域 owner 过滤用)。"""
    value = None if member_id is None else int(member_id)
    conn.create_function("current_member", 0, lambda: value)


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """应用 config 里那套 PRAGMA。

    ⚠️ **journal_mode 是唯一需要独占锁的 PRAGMA**(它要改数据库头),
    所以"刚建库"或者"两个连接同时开一个库"的那一瞬间会抛 database is locked。
    两道处理,都是必要的:
      1. 已经是 wal 就**别写它** —— 读当前模式不拿写锁,这一步省掉绝大多数冲突;
      2. 写失败也**不让连接开不出来** —— 退化成 delete 模式只是慢,不是错。
         (实测:测试里并发开库时,这一句曾经让 GET /api/questions 直接 500)
    另外:busy_timeout 要**排在 journal_mode 之前**,否则那次改模式连等都不等。
    """
    deferred: list[str] = []
    for pragma in config.SQLITE_PRAGMAS:
        if "journal_mode" in pragma.lower():
            deferred.append(pragma)      # 留到最后,等 busy_timeout 先生效
            continue
        conn.execute(pragma)
    for pragma in deferred:
        _ensure_journal_mode(conn, pragma)


def _ensure_journal_mode(conn: sqlite3.Connection, pragma: str) -> None:
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        if row is not None and str(row[0]).lower() == "wal":
            return
    except sqlite3.Error:
        pass
    # 与迁移共用一把锁:改库模式和建表一样,是"一次性"的事,不能让两个请求同时干。
    with MIGRATION_LOCK:
        for attempt in range(3):
            try:
                conn.execute(pragma)
                return
            except sqlite3.Error:
                time.sleep(0.05 * (attempt + 1))


def connect_assistant(*, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开领域库(assistant.db)并确保 schema 已迁移。"""
    config.ensure_runtime_dirs()
    conn = connect(config.ASSISTANT_DB, check_same_thread=check_same_thread)
    migrate(conn, MIGRATIONS_ASSISTANT)
    return conn


def connect_memory(*, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开记忆库(memory.db)并确保 schema 已迁移。

    注意:检索索引(fts5 虚表)由 la.memory.search.ensure_search_index() 按能力创建。
    """
    config.ensure_runtime_dirs()
    conn = connect(config.MEMORY_DB, check_same_thread=check_same_thread)
    migrate(conn, MIGRATIONS_MEMORY)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """一个写事务的**唯一**正确写法:成功 commit,失败 rollback 并原样抛出。

    ⚠️ 为什么要有它:全仓几十个写方法各自 commit、只有两处 rollback。SQLite 在
       legacy 事务模式下,失败的那条语句会回滚,但**之前已经执行的 DML 留在打开
       的事务里** —— 下一次无关的 commit 会把它们一起提交,变成"一次失败的写入
       悄悄成功了一半"。
    """
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def pragma_snapshot(conn: sqlite3.Connection) -> dict[str, object]:
    """读回当前连接的 PRAGMA,用于自检与测试断言。"""
    return {
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
        "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
        "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
    }


def table_names(conn: sqlite3.Connection, *, include_internal: bool = False) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    names = [row[0] for row in rows]
    if include_internal:
        return names
    return [n for n in names if not n.startswith("sqlite_")]
