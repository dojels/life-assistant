"""扫码登录的**共用流程**。

终端服务(weixin_ilink_main)和网页登录页(weixin_login_server)都调这里的函数,
免得"怎么扫码"这件事有两份实现、然后慢慢长歪。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import weixin_store
from .weixin_protocol import BotCredentials, ILinkError, ILinkClient


@dataclass
class QRSession:
    """一次扫码会话。"""

    qrcode: str = ""
    url: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "waiting"      # waiting | scaned | confirmed | expired
    credentials: BotCredentials | None = None
    error: str = ""
    #: 二维码有效期(秒)。官方没给确切数字,按常见的 5 分钟处理。
    ttl: float = 300.0

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "expired" if (self.expired and self.status == "waiting") else self.status,
            "url": self.url,
            "age": int(time.time() - self.created_at),
            "expired": self.expired,
            "error": self.error,
            "bot_id": self.credentials.bot_id if self.credentials else "",
        }


async def new_qr_session(client: ILinkClient) -> QRSession:
    """向网关要一张新二维码。"""
    resp = await client.request_qrcode()
    qrcode = str(resp.get("qrcode") or "")
    if not qrcode:
        raise ILinkError(errmsg="登录网关没有返回二维码")
    return QRSession(qrcode=qrcode, url=str(resp.get("qrcode_img_content") or ""))


def _credentials_from(status: dict[str, Any]) -> BotCredentials:
    return BotCredentials(
        bot_id=str(status.get("ilink_bot_id") or ""),
        token=str(status.get("bot_token") or ""),
        base_url=str(status.get("baseurl") or "https://ilinkai.weixin.qq.com"),
        user_id=str(status.get("ilink_user_id") or ""),
    )


async def poll_once(
    client: ILinkClient,
    session: QRSession,
    *,
    account: str | None = None,
    timeout_s: float = 40.0,
) -> QRSession:
    """查一次扫码状态。返回同一个 session(原地更新)。

    `account` 只在"网页给某个微信号扫码"时传:凭据要落到那个账号的目录里,
    而不是本进程 use_account() 指的那一个(Web 是多线程的,全局会串号)。
    """
    try:
        status = await client.poll_qrcode(session.qrcode, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 - 轮询抖动不该中断登录
        # 网页轮询用的是短超时:网关本来就是长轮询,超时 = "还没有变化",不是故障。
        # 把它记成 error 会让页面上每两秒闪一次红字,纯噪音。
        if "Timeout" not in type(exc).__name__:
            session.error = type(exc).__name__ + ": " + str(exc)[:160]
        return session
    state = str(status.get("status") or "")
    if state == "confirmed":
        creds = _credentials_from(status)
        if not creds.token:
            session.status = "expired"
            session.error = "确认回调里没有 bot_token"
            return session
        weixin_store.save(creds, account=account)
        weixin_store.save_cursor("", account=account)   # 换了 bot,游标必须清掉
        # 通知常驻进程"凭据换了":它会在下一轮循环边界重连(不需要 systemctl 权限)。
        weixin_store.request_relogin(account)
        session.credentials = creds
        session.status = "confirmed"
    elif state == "scaned":
        session.status = "scaned"
    elif state == "expired":
        session.status = "expired"
    else:
        session.status = "waiting"
    return session


async def wait_for_scan(
    client: ILinkClient,
    *,
    on_qr: Callable[[QRSession], None],
    timeout_s: float = 300.0,
    poll_interval: float = 3.0,
) -> BotCredentials:
    """终端用:循环等扫码,过期就换一张。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        session = await new_qr_session(client)
        on_qr(session)
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            await poll_once(client, session)
            if session.status == "confirmed" and session.credentials is not None:
                return session.credentials
            if session.status == "expired" or session.expired:
                on_qr(session)   # 通知调用方"这张废了"
                break
    raise ILinkError(errmsg="扫码等待超时")
