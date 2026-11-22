"""每日错题推送:到点把清单 + 合并听写**真的发到微信**(D162)。

⚠️ 为什么必须住在微信常驻进程里:主动消息要一个**还活着的 context_token**,
   而它只存在于长轮询进程的内存里。网页心跳拿不到,所以过去只能"生成"不能"送达"
   —— 页面看得见、上下文里有,用户手机上一句话都没有。

去重**不用 dispatches**:那张表是"网页那份清单认领过了"的凭据,由 web 心跳写;
这里要的是"这条消息真的发出去了",所以另记一个 sys_settings 标记,而且**成功才写**:
发失败(没有窗口)不能标记,否则今天这条就永远不补了。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

from .. import config
from ..ops import secrets as secret_store
from ..ops import settings_store
from ..storage import db

#: 多久看一眼该不该推(与提醒推送同一节奏)
TICK_S = 30.0
#: 某个收件人失败后的冷却:窗口关着的时候,30 秒一次纯属白打(2026-10-24:
#: 妈妈那个号窗口停在 6 天前,一晚上打了 500 多次注定失败的请求)。
RETRY_S = 300.0


class StudyPusher:
    """到点把今天的错题清单与合并听写推给指定成员。"""

    def __init__(
        self,
        *,
        send: Callable[[str, int | None], Awaitable[tuple[bool, str]]],
        send_file: Callable[..., Awaitable[tuple[bool, str]]],
        log: Callable[[str], None] | None = None,
        service_factory: Callable[[], Any] | None = None,
        conn_factory: Callable[[], Any] | None = None,
        interval_s: float = TICK_S,
        retry_s: float = RETRY_S,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.send = send
        self.send_file = send_file
        self._log_line = log or (lambda text: None)
        self._service_factory = service_factory
        self._conn_factory = conn_factory or db.connect_assistant
        self.interval_s = max(5.0, float(interval_s))
        self.retry_s = max(0.0, float(retry_s))
        self._now = now
        self._stop = False
        #: 收件人 → 下次可以重试的时刻(失败后冷却)
        self._retry_at: dict[str, float] = {}

    def _log(self, text: str) -> None:
        self._log_line(time.strftime("%H:%M:%S ") + text)

    def stop(self) -> None:
        self._stop = True

    async def run_forever(self) -> None:
        while not self._stop:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - 推送失败不该打死常驻进程
                self._log("错题推送出错:" + type(exc).__name__ + " " + str(exc)[:160])
            await asyncio.sleep(self.interval_s)

    # -- 一轮 -------------------------------------------------------------
    async def tick(self, *, now: datetime | None = None) -> None:
        service = None
        conn = None
        try:
            service = self._build_service()
            moment = now or self._current(service)
            settings = service.settings()
            if not settings.push_enabled:
                return
            if moment.strftime("%H:%M") < settings.push_time_local:
                return
            day = moment.date().isoformat()
            targets = list(settings.push_targets) or [None]
            conn = self._conn_factory()
            pending = [
                member for member in targets
                if not self._sent(conn, day, member) and not self._cooling(day, member)
            ]
            if not pending:
                return
            text = service.push_text(now=moment)
            if not text:
                return
            files: list[tuple[str, str]] | None = None
            for member in pending:
                ok, reason = await self.send(text, member)
                if not ok:
                    # 没有窗口是常态(用户今天还没说话)—— 不标记,**冷却 5 分钟**再试,
                    # 并把原因留在库里,让设置页能说清"为什么这个收件人没收到"。
                    self._cool(day, member)
                    self._note_error(conn, day, member, str(reason))
                    self._log(
                        "错题推送没发出去(收件人 " + str(member) + "):" + str(reason)[:120]
                    )
                    continue
                # 音频只在**确实有窗口**时才合成:没有窗口的晚上不该白花一次语音费
                if files is None:
                    files = self._audio_files(service)
                for path, name in files:
                    filed, why = await self.send_file(path, name, member)
                    if not filed:
                        self._log("合并音频没发出去:" + str(why)[:120])
                self._mark(conn, day, member)
                self._clear_error(conn, day, member)
                self._log(
                    "错题推送已发出(收件人 " + str(member) + ","
                    + str(len(files or [])) + " 个音频)"
                )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            if service is not None:
                closer = getattr(service, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001
                        pass

    # -- 依赖装配 ---------------------------------------------------------
    def _build_service(self) -> Any:
        if self._service_factory is not None:
            return self._service_factory()
        from ..domain.assembly import build_domain_services

        conn = db.connect_assistant()
        try:
            # 页面里改过模型/语音的 key,要让这边也拿到(否则合成会用旧配置)
            secret_store.sync(conn, force=True)
            domain = build_domain_services(conn, settings=config.get_settings(), receipts=None)
        except Exception:
            conn.close()
            raise
        # 连接跟着 handle 一起关;study 服务本身用的就是这条连接
        return _ServiceHandle(domain.study, conn)

    def _current(self, service: Any) -> datetime:
        if self._now is not None:
            return self._now()
        try:
            return service.now()
        except Exception:  # noqa: BLE001
            return config.get_settings().now()

    # -- 音频 -------------------------------------------------------------
    def _audio_files(self, service: Any) -> list[tuple[str, str]]:
        files: list[tuple[str, str]] = []
        try:
            # 听写与跟读各成一段(D160 / D178):都在这份"今天的合并声音"里
            groups = service.merged_audio_today()
        except Exception as exc:  # noqa: BLE001
            self._log("合并声音没做成:" + type(exc).__name__)
            return files
        for group in groups or []:
            artifact_id = group.get("artifact_id")
            if not artifact_id or group.get("error"):
                continue
            try:
                artifact = service.artifacts.get(str(artifact_id))
                path = artifact.path(service.artifacts.root)
            except Exception:  # noqa: BLE001
                continue
            files.append((str(path), str(group.get("label") or "听写") + path.suffix))
        return files

    # -- 冷却与失败原因 ---------------------------------------------------
    def _cooling(self, day: str, member: int | None) -> bool:
        return time.time() < self._retry_at.get(_key(day, member), 0.0)

    def _cool(self, day: str, member: int | None) -> None:
        self._retry_at[_key(day, member)] = time.time() + self.retry_s

    def _note_error(self, conn: Any, day: str, member: int | None, reason: str) -> None:
        """把"为什么没发出去"写进 sys_settings,设置页直接读它。"""
        try:
            settings_store.set_text(
                conn, "study.push.err." + _key(day, member), str(reason)[:200]
            )
        except Exception:  # noqa: BLE001 - 记不下来也不能挡住推送
            pass

    def _clear_error(self, conn: Any, day: str, member: int | None) -> None:
        try:
            settings_store.clear(conn, "study.push.err." + _key(day, member))
        except Exception:  # noqa: BLE001
            pass

    # -- 去重标记 ---------------------------------------------------------
    def _sent(self, conn: Any, day: str, member: int | None) -> bool:
        key = "study.push." + _key(day, member)
        try:
            return bool(settings_store.get_raw(conn, key))
        except Exception:  # noqa: BLE001
            return False

    def _mark(self, conn: Any, day: str, member: int | None) -> None:
        key = "study.push." + _key(day, member)
        try:
            settings_store.set_text(conn, key, "1")
        except Exception:  # noqa: BLE001 - 标记写不进去最多重复发一次,不阻断
            pass


def _key(day: str, member: int | None) -> str:
    """标记/冷却/失败原因共用一把钥匙:同一天、同一个收件人。"""
    return day + "." + ("all" if member is None else str(member))


class _ServiceHandle:
    """把 study 服务和它的连接绑在一起,关闭时一起收掉。"""

    def __init__(self, study: Any, conn: Any) -> None:
        self._study = study
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._study, name)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
