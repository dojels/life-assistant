"""网页扫码登录的会话管理(单进程、**按账号**)。

为什么单独一个模块:扫码要跨请求保持状态(生成一次、轮询很多次),
而 8788 那个独立登录页与 Web 页面用的是同一套东西 —— 两份实现会慢慢长歪。

⚠️ 三个边界:
  * **一个进程一个 manager**:状态在内存里,所以 Web 只能单 worker(见 deploy/README);
  * **按账号隔离**:Web 是多线程的,不能靠 use_account() 切全局(会串号),
    所以每个方法都显式收 account;
  * **单飞轮询**:同一个账号同一时刻只允许一个状态请求在飞,其余直接返回快照 ——
    否则一个 40 秒的长轮询就能把线程池占满。
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

from .weixin_login import QRSession, new_qr_session, poll_once
from .weixin_protocol import ILinkClient

#: 网页轮询的超时。网关是长轮询:变化会**立刻**返回,没变化才等到超时。
#: 取 10 秒而不是 2 秒 —— 浏览器每 2 秒问一次,但同一时刻只有一个请求在飞,
#: 其余直接拿快照。这样网关那边大约 6 次/分钟,而不是 30 次。
POLL_TIMEOUT_S = 10.0


class LoginSessionManager:
    def __init__(self, client_factory: Callable[[], ILinkClient] | None = None) -> None:
        self._factory = client_factory or (lambda: ILinkClient(timeout_s=20.0))
        self._sessions: dict[str, QRSession] = {}
        self._clients: dict[str, ILinkClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._polling: set[str] = set()

    @staticmethod
    def _key(account: str | None) -> str:
        return str(account or "")

    def _lock(self, key: str) -> asyncio.Lock:
        found = self._locks.get(key)
        if found is None:
            found = asyncio.Lock()
            self._locks[key] = found
        return found

    async def start(self, account: str = "") -> QRSession:
        """要一张新二维码(旧会话连同它的连接一起丢掉)。"""
        key = self._key(account)
        async with self._lock(key):
            await self._drop(key)
            client = self._factory()
            self._clients[key] = client
            session = await new_qr_session(client)
            self._sessions[key] = session
            return session

    def current(self, account: str = "") -> QRSession | None:
        return self._sessions.get(self._key(account))

    async def poll(self, account: str = "") -> QRSession | None:
        """查一次;没有会话返回 None。不会因为并发而多打一次网关。"""
        key = self._key(account)
        session = self._sessions.get(key)
        if session is None:
            return None
        if session.status in ("confirmed", "expired") or session.expired:
            return session
        if key in self._polling:
            return session  # 单飞:另一个请求正在问,直接给快照
        client = self._clients.get(key)
        if client is None:
            return session
        self._polling.add(key)
        try:
            session = await poll_once(
                client, session, account=key, timeout_s=POLL_TIMEOUT_S
            )
            self._sessions[key] = session
            return session
        except Exception as exc:  # noqa: BLE001 - 网关抖动不该让页面 500
            session.error = type(exc).__name__ + ": " + str(exc)[:120]
            return session
        finally:
            self._polling.discard(key)
            if session.status in ("confirmed", "expired") or session.expired:
                await self._close_client(key)

    async def _close_client(self, key: str) -> None:
        client = self._clients.pop(key, None)
        if client is None:
            return
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass

    async def _drop(self, key: str) -> None:
        self._sessions.pop(key, None)
        await self._close_client(key)

    async def forget(self, account: str = "") -> None:
        async with self._lock(self._key(account)):
            await self._drop(self._key(account))

    def expires_in(self, account: str = "") -> int:
        session = self.current(account)
        if session is None:
            return 0
        return max(0, int(session.ttl - (time.time() - session.created_at)))

