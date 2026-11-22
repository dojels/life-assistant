"""跨进程的会话轮次锁:同一个会话同一时间只允许一轮。

为什么需要它
------------
Web 与微信是**两个进程**。家庭版把两端映射到同一个 `member:<id>` 之后,
同一个人可能一边在网页发消息、一边在微信发消息。两个 `MemorySession` 会同时:

* 从同一个 seq 投影历史、同时压缩;
* 同时写回执 / 待办 / 账本;
* 在 `ask_user` 上各问各的。

只在进程内用 `asyncio.Lock` 不够 —— 那只管得住同一个 Thread。
这里用**文件锁**:进程正常退出或崩溃时,操作系统都会释放,不会留下需要
超时清理的 DB 锁;代价是锁文件常驻(和 archive 的 .lock 同一个思路)。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

from .. import config
from .archive import archive_filename

DEFAULT_TIMEOUT_S = 180.0
DEFAULT_POLL_S = 0.1


def lock_path(session_id: str, directory: Path | None = None) -> Path:
    base = (
        Path(directory)
        if directory is not None
        else Path(config.DATA_DIR) / "run" / "locks"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base / (archive_filename(session_id) + ".turn")


def _ensure_byte(handle) -> None:
    # ⚠️ 必须看**文件真实大小**,不能看 handle.tell():以 "a+b" 打开时,
    #    即使文件已有 1 字节,tell() 仍可能是 0。第二个进程会再写一次,
    #    而第一个进程已经锁住第 0 字节 —— flush 在 Windows 上会偶发
    #    PermissionError(2026-10-18 全量测试抓到)。
    if os.fstat(handle.fileno()).st_size > 0:
        return
    try:
        handle.write(b"\0")
        handle.flush()
    except OSError:
        # 另一个进程刚好在同一毫秒写了这一字节:只要文件现在非空就算初始化完成。
        if os.fstat(handle.fileno()).st_size == 0:
            raise


def _try_lock(handle) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.asynccontextmanager
async def turn_lock(
    session_id: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    poll: float = DEFAULT_POLL_S,
    directory: Path | None = None,
):
    """异步地拿住某个会话的跨进程轮次锁;退出 `with` 时释放。

    等锁是异步的(`asyncio.sleep`),不会阻塞事件循环;Web 的 SSE 心跳
    在等锁期间仍然能往外发。
    """
    path = lock_path(session_id, directory)
    handle = path.open("a+b")
    try:
        _ensure_byte(handle)
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise TimeoutError("会话正忙(另一个入口还在处理): " + session_id)
            await asyncio.sleep(max(float(poll), 0.01))
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()
