"""微信官方 iLink(ClawBot)渠道适配器。

把"微信里的一条消息"接到项目已有的桥接层上。整条链路:

    手机微信 ──► 腾讯 iLink 服务器(ilinkai.weixin.qq.com)
                        ▲ │ 长轮询 getupdates / 发消息 sendmessage
                        │ ▼
                  本文件(出站 HTTP,无需公网 IP、无需微信客户端常驻)
                        │
                        ▼
              la/bridge/bridge.py(会话串行 / 幂等去重 / 提问回环)
                        │
                        ▼
              MemorySession(记忆 / 工具 / 账本 / 回执 / 护栏)

⚠️ 三条必须知道的运行约束(都不是本项目的选择 —— 前两条的证据级别见各自注):

1. **24 小时会话窗口**:必须先有用户消息拿到 `context_token`,bot 才能回。
   主动提醒(日程到点推送)**走不了这条路**。
2. **一个 context_token 只能发 10 条**(CONTEXT_TOKEN_QUOTA)。窗口内额度用完,
   同样要等用户再说一句。超出时服务端回 `ret=-2`,本地额度立即归零(不白试)。
   ⚠️ **证据级别**:24 小时窗口有官方 FAQ 原文;这个"10 条"**没有**在腾讯官方
   protocol.md 里核到 —— 依据是多方独立印证(第三方生产服务的帮助页写明"受微信官方
   限制…最多 10 条消息或 10 个文件";多个开源实现的同类限制处理)。见 D98 补充。
   ⚠️ 响应里的 `remaining` 字段同样**未核实**:取得到就用它校准,取不到退回本地自减。
3. **扫码凭据等同于你的微信身份**,存 `data/wechat/credentials.json`,不要外传。

登录只需要做一次;之后启动直接复用凭据,除非收到 -14(会话过期)。
"""

from __future__ import annotations

import asyncio
import time
import types
from dataclasses import dataclass, field
from typing import Any

from ...storage import db
from ..bridge import Inbound, SendAction
from ..types import Attachment, OutboundFile
from . import weixin_store
from .weixin_protocol import (
    CONTEXT_TOKEN_QUOTA,
    LONGPOLL_TIMEOUT,
    WINDOW_S,
    SESSION_EXPIRED,
    BotCredentials,
    ILinkError,
    ILinkClient,
    IncomingMessage,
    RateLimited,
    SessionExpired,
    parse_incoming,
    split_text,
)
from .weixin_qr import render_terminal

#: 会话过期时的重试节奏(秒):不要把扫码窗口刷爆
RELOGIN_BACKOFF = 300.0
#: 网络错误的退避上限
MAX_BACKOFF = 60.0
#: 补发的有效期:超过这么久就不再补了 —— 隔了半小时突然收到一条旧回复,只会让人困惑
OUTBOX_TTL_S = 30 * 60.0

def human_gap(age_s: float) -> str:
    """把"多久以前"说成人话(日志和页面都用它)。"""
    seconds = max(0.0, float(age_s))
    if seconds < 3600:
        return str(int(seconds // 60)) + " 分钟"
    hours = seconds / 3600.0
    if hours < 48:
        return str(int(hours)) + " 小时"
    return str(int(hours // 24)) + " 天"


def split_media_refs(refs: Any) -> tuple[list[tuple[int, Any]], int]:
    """把入站媒体分成"要下载的"和"语音但已有转写、不必下载的"。

    ⭐ D139(2026-10-17 真机):语音的转写文字已经以正文形式进了这一轮,原声再下载一份
    会做三件坏事 —— 白花一次下载、素材区多一条读不了的音频、回话里多一句
    「已存成素材,你可以直接让我读它的内容」。最后这条就是用户看到的
    「有个我不认识的附件」:模型真去读了,读不出来,只能那样绕着说。

    ⚠️ 返回的是 **(原始下标, ref)**:下标要原样交给取媒体那一步,
       因为它是幂等键的一部分(<message_id>:<index>)。过滤后重新编号会让
       同一条媒体在重发时换一个 external_id,去重就失效了。
    """
    pending: list[tuple[int, Any]] = []
    transcribed = 0
    for index, ref in enumerate(refs or []):
        if getattr(ref, "transcribed", False):
            transcribed += 1
            continue
        pending.append((index, ref))
    return pending, transcribed


@dataclass
class Conversation:
    """一个微信联系人的会话状态。"""

    user_id: str
    context_token: str = ""
    typing_ticket: str = ""
    last_seen_ms: int = 0
    remaining_quota: int = CONTEXT_TOKEN_QUOTA
    messages: int = 0

    def window_age_s(self, *, now_ms: int | None = None) -> float | None:
        """距用户最后一次说话多少秒。没记时间戳(老 session.json)→ None。"""
        if not self.last_seen_ms:
            return None
        moment = int(time.time() * 1000) if now_ms is None else int(now_ms)
        return max(0.0, (moment - self.last_seen_ms) / 1000.0)

    def window_open(self, *, now_ms: int | None = None) -> bool:
        """主动消息还能不能发。没时间戳时**从宽**(宁可信其开,别把能发的判死)。"""
        age = self.window_age_s(now_ms=now_ms)
        return True if age is None else age < WINDOW_S

    def touch(self, message: IncomingMessage) -> None:
        self.user_id = message.from_user
        self.context_token = message.context_token or self.context_token
        self.last_seen_ms = message.create_time_ms or self.last_seen_ms
        # 新 token = 新额度
        self.remaining_quota = CONTEXT_TOKEN_QUOTA

    def as_dict(self) -> dict[str, Any]:
        """落盘用。⚠️ 这份东西等同于"能替你发消息的通行证",别写进日志。"""
        return {
            "user_id": self.user_id,
            "context_token": self.context_token,
            "typing_ticket": self.typing_ticket,
            "last_seen_ms": self.last_seen_ms,
            "remaining_quota": self.remaining_quota,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Conversation":
        return cls(
            user_id=str(raw.get("user_id") or ""),
            context_token=str(raw.get("context_token") or ""),
            typing_ticket=str(raw.get("typing_ticket") or ""),
            last_seen_ms=int(raw.get("last_seen_ms") or 0),
            remaining_quota=int(raw.get("remaining_quota") or CONTEXT_TOKEN_QUOTA),
        )


@dataclass
class ILinkTransport:
    """桥接层的 Transport 实现:把 SendAction 真正发到微信。

    桥接层只管问"能不能发",这里负责"怎么发"和"发不出去怎么办"。
    """

    client: ILinkClient
    conversations: dict[str, Conversation] = field(default_factory=dict)
    name: str = "weixin-ilink"
    #: 发失败时记在案,便于排障(不抛给桥接层,免得一轮对话整个挂掉)
    last_error: str = ""
    #: 本次 send() 里**没发出去**的文件名。D87 之前这类失败是完全静默的:
    #: 正文照发、文件没了、日志里一个字都没有,用户只能干等。
    failed_files: list[str] = field(default_factory=list)

    def conversation(self, user_id: str) -> Conversation:
        found = self.conversations.get(user_id)
        if found is None:
            found = Conversation(user_id=user_id)
            self.conversations[user_id] = found
        return found

    #: 待发队列:发失败(断网 / 额度用完)的**回复正文**,等能发了再补上。
    #: ⭐ 以前发失败就是"丢掉 + 写一行日志":2026-10-13 21:32–21:53 本机到微信的网络断掉,
    #:    那条"照片录好了"的回复**永远没到用户手里**,他那边只看到连续无响应。
    outbox: list[dict[str, Any]] = field(default_factory=list)
    #: 补发时贴上的一句话。⚠️ 不要省:隔了几分钟的回复突然冒出来,用户会以为串台了。
    LATE_PREFIX = "(刚才网络不通,这条是补发的)"

    def hold(self, user_id: str, text: str, *, reason: str = "") -> None:
        """把一条发不出去的回复存进待发队列(顺带落盘,重启也不丢)。"""
        body = str(text or "").strip()
        if not body:
            return
        self.outbox.append({
            "user_id": str(user_id or ""), "text": body,
            "at": time.time(), "reason": str(reason or "")[:120],
        })
        # 只留最近 20 条:队列是"补救",不是归档 —— 无上限的话一次长断网能攒出几百条
        if len(self.outbox) > 20:
            self.outbox = self.outbox[-20:]
        weixin_store.save_outbox(self.outbox)

    def take_expired(self, *, ttl_s: float = OUTBOX_TTL_S, now: float | None = None) -> int:
        """丢掉太旧的待发(超过 TTL 的补发只会更让人困惑),返回丢了几条。"""
        moment = time.time() if now is None else now
        keep = [item for item in self.outbox
                if moment - float(item.get("at") or 0) <= ttl_s]
        dropped = len(self.outbox) - len(keep)
        if dropped:
            self.outbox = keep
            weixin_store.save_outbox(self.outbox)
        return dropped

    async def flush_outbox(self) -> int:
        """把待发队列里还能发的补发掉,返回补发成功几条。

        ⚠️ 只有"能发"的时候才试:没有 context_token、额度见底,试也是白试
        (额度要等用户下一条消息带来新的 token 才会恢复)。
        """
        self.take_expired()
        if not self.outbox:
            return 0
        sent = 0
        remaining: list[dict[str, Any]] = []
        for index, item in enumerate(self.outbox):
            user_id = str(item.get("user_id") or "")
            conv = self.conversations.get(user_id)
            if conv is None or not conv.context_token or conv.remaining_quota <= 0:
                remaining.extend(self.outbox[index:])
                break
            ok = await self._send_one(conv, self.LATE_PREFIX + "\n" + str(item["text"]))
            if not ok:
                remaining.extend(self.outbox[index:])
                break
            sent += 1
        if len(remaining) != len(self.outbox):
            self.outbox = remaining
            weixin_store.save_outbox(self.outbox)
        return sent

    async def _send_one(self, conv: "Conversation", text: str) -> bool:
        """发一条(失败只记 last_error,不抛)。send() 与补发共用这一条路。"""
        try:
            resp = await self.client.send_text(conv.user_id, text, conv.context_token)
        except SessionExpired:
            self.last_error = "会话过期(-14),需要重新扫码"
            raise
        except RateLimited as exc:
            conv.remaining_quota = 0
            self.last_error = (
                "本窗口额度已用完或会话已作废(ret=-2):" + str(exc)[:120]
                + " —— 需要用户再发一条消息才能继续回复"
            )
            return False
        except Exception as exc:  # noqa: BLE001 - 发失败不该打死常驻进程
            self.last_error = type(exc).__name__ + ": " + str(exc)[:200]
            return False
        conv.remaining_quota -= 1
        quota = resp.get("remaining") if isinstance(resp, dict) else None
        if isinstance(quota, int):
            conv.remaining_quota = quota
        return True

    async def send(self, action: SendAction) -> None:
        conv = self.conversation(action.reply_to or self._only_user())
        # ⭐ 产物(图片/文件)先发:**上传是最容易失败的一步**,失败了正文照样要送到。
        #    反过来的话,上传重试会让用户收到两条一样的正文却看不到文件。
        #
        # ⚠️ 但**必须给正文留一格**:一个 context_token 只有 10 条额度。文件把额度吃光的
        #    话,用户会收到一堆文件、却没有任何一句话说明这些是什么 —— 比不发更糟。
        pending = list(action.files)
        skipped = 0
        if action.text.strip() and pending:
            room = max(conv.remaining_quota - 1, 0)
            if len(pending) > room:
                skipped = len(pending) - room
                pending = pending[:room]
        self.failed_files = []
        for item in pending:
            if not await self.send_file(item, conv):
                self.failed_files.append(
                    str(getattr(item, "name", "") or getattr(item, "path", "") or "?")
                )
        # ⚠️ send_file 成功时会把 last_error 清空,所以这句必须**在文件发完之后**再落,
        #    否则刚写上的"少发了几个"会被下一条成功记录抹掉。
        if skipped and not self.last_error:
            self.last_error = (
                "本轮产物有 " + str(len(action.files)) + " 个,额度只够先发 "
                + str(len(pending)) + " 个(给正文留一格),少发了 " + str(skipped) + " 个"
            )
        if not action.text.strip():
            return
        if not conv.context_token:
            self.last_error = "没有 context_token(用户还没说过话),这条发不出去"
            return
        chunks = split_text(action.text)
        for index, chunk in enumerate(chunks):
            if conv.remaining_quota <= 0:
                self.last_error = (
                    "本窗口额度已用完(" + str(CONTEXT_TOKEN_QUOTA) + " 条),"
                    "需要用户再发一条消息才能继续回复"
                )
                # ⭐ 剩下的**存起来**,等用户下一条消息把 token 刷新后再补发 ——
                #    以前是直接丢掉:用户看到"回复说了一半就没了"(D112)
                for rest in chunks[index:]:
                    self.hold(conv.user_id, rest, reason="额度用完")
                return
            # ⚠️ 一次网络抖动不该吃掉整条回复:失败的那一条(以及它后面的)全部进待发队列。
            #    SessionExpired 例外:那是要重新扫码,补发也没用。
            ok = await self._send_one(conv, chunk)
            if not ok:
                for rest in chunks[index:]:
                    self.hold(conv.user_id, rest, reason=self.last_error)
                return

    async def send_file(self, item: Any, conv: "Conversation") -> bool:
        """把一个文件/图片发到微信。返回是否发成功(失败只记 last_error,不抛)。"""
        import pathlib

        from .weixin_media import (
            ITEM_FILE,
            ITEM_IMAGE,
            MediaError,
            build_media_item,
            upload_media,
        )

        path = pathlib.Path(str(getattr(item, "path", "")))
        if not path.exists():
            self.last_error = "产物文件不存在: " + str(path)
            return False
        if not conv.context_token:
            self.last_error = "没有 context_token,媒体发不出去"
            return False
        if conv.remaining_quota <= 0:
            self.last_error = "本窗口额度已用完,媒体要等用户再说一句"
            return False
        kind = str(getattr(item, "kind", "file") or "file")
        item_type = ITEM_IMAGE if kind == "image" else ITEM_FILE
        try:
            media = await upload_media(
                self.client.get_upload_url, self.client.http,
                data=path.read_bytes(), item_type=item_type, to_user_id=conv.user_id,
            )
            media["file_name"] = str(getattr(item, "name", "") or path.name)
            await self.client.send_items(
                conv.user_id, [build_media_item(item_type, media)], conv.context_token
            )
            conv.remaining_quota -= 1
            self.last_error = ""
            return True
        except MediaError as exc:
            self.last_error = str(exc)[:200]
            return False
        except Exception as exc:  # noqa: BLE001 - 发文件失败不该打死常驻进程
            self.last_error = type(exc).__name__ + ": " + str(exc)[:160]
            return False

    async def typing(self, thread_id: str) -> None:
        conv = self.conversation(thread_id)
        if not conv.typing_ticket:
            try:
                cfg = await self.client.get_config(conv.user_id, conv.context_token or None)
                ticket = cfg.get("typing_ticket") if isinstance(cfg, dict) else None
                if ticket:
                    conv.typing_ticket = str(ticket)
            except Exception:  # noqa: BLE001 - "正在输入"是锦上添花,失败就算了
                return
        if not conv.typing_ticket:
            return
        try:
            await self.client.send_typing(conv.user_id, conv.typing_ticket, 1)
        except Exception:  # noqa: BLE001
            pass

    def _only_user(self) -> str:
        """只有一个联系人时用它(自用场景就是这么简单)。"""
        if len(self.conversations) == 1:
            return next(iter(self.conversations))
        return ""


class ILinkAdapter:
    """常驻服务:登录 → 长轮询 → 交给 Bridge → 发回微信。"""

    #: 常驻服务的模块名(见 weixin_ilink_main.py)。放这里是为了让
    #: "怎么启动"这件事和适配器在一起,而不是散在 README 里。
    RUN_HINT = "python -m la.bridge.adapters.weixin_ilink_main"

    def __init__(
        self,
        *,
        bridge: Any,
        client: ILinkClient | None = None,
        log: Any = None,
        account: str | None = None,
    ) -> None:
        # ⚠️ 先切账号目录,再读 outbox/cursor/session —— 否则两个微信号会共用
        #    同一份凭据/游标,互相覆盖(2026-10-18 多号修复)。
        weixin_store.use_account(account)
        self.account = weixin_store.current_account()
        self.bridge = bridge
        self.client = client or ILinkClient()
        self.transport = ILinkTransport(client=self.client)
        # 上次进程没发出去的回复:接回来(不然重启一次就真的没了)
        self.transport.outbox = weixin_store.load_outbox()
        if self.transport.outbox:
            self.transport.take_expired()
        self.cursor = weixin_store.load_cursor()
        self.log = log
        self._stop = False
        #: 正在处理的入站消息(一条一个任务)。见 _dispatch 的注释:这里**不能串行**。
        self._tasks: set[asyncio.Task] = set()
        self.stats: dict[str, int] = {
            "polls": 0, "messages": 0, "errors": 0, "relogins": 0,
            #: 语音条数(带转写 = 内容已在正文里,原声不下载)
            "voice": 0,
        }
        #: 已经留过原始报文样例的"不认识的内容类型"(每种一份就够)
        self._unknown_logged: set[int] = set()
        self._restore_session()

    def _restore_session(self) -> None:
        """把上次进程留下的会话窗口接回来。

        ⚠️ 不接回来的话,每次重启(计划任务每天都会重起服务)主动推送都是哑的,
        直到用户再说一句话 —— 而"不用先开口就能收到提醒"正是这个功能的意义。
        """
        raw = weixin_store.load_session()
        if not raw:
            return
        try:
            conv = Conversation.from_dict(raw)
        except (TypeError, ValueError):
            return
        if not conv.user_id or not conv.context_token:
            return
        self.transport.conversations[conv.user_id] = conv
        # ⚠️ 只报"接回来了",**绝不打印 token 本身**(它等同于能替你发消息的通行证)
        self._say(
            "已接回上次的会话窗口(剩余额度 " + str(conv.remaining_quota)
            + ";窗口是 24 小时的,过期后要等用户先说一句)"
        )

    # -- 日志 -------------------------------------------------------------
    def _say(self, text: str) -> None:
        line = time.strftime("%H:%M:%S ") + text
        if self.log is not None:
            self.log(line)
        else:
            print(line, flush=True)

    # -- 登录 -------------------------------------------------------------
    #
    # ⚠️ 这里有一份"终端里的扫码循环",weixin_login.py 里有一份"可复用的扫码流程"。
    #    两处都指向同一套协议调用与凭据落盘(weixin_store),所以不会分叉出错;
    #    但也确实是重复代码。**网页登录(weixin_login_server)用后者**。
    #    收敛条件:等哪天真要在终端和网页之间切着扫码,再把这里改成调 weixin_login。
    async def login(self, *, force: bool = False) -> BotCredentials:
        """扫码登录(终端版)。已有凭据且不强制时直接复用。"""
        cached = None if force else weixin_store.load(self.account)
        if cached is not None:
            self._say("复用已有凭据(bot " + cached.bot_id + ")")
            self._apply(cached)
            return cached

        while True:
            resp = await self.client.request_qrcode()
            qrcode = str(resp.get("qrcode") or "")
            url = str(resp.get("qrcode_img_content") or "")
            if not qrcode:
                raise ILinkError(errmsg="登录网关没有返回二维码")
            self._show_qr(url)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                await asyncio.sleep(3.0)
                status = await self.client.poll_qrcode(qrcode)
                state = str(status.get("status") or "")
                if state == "confirmed":
                    creds = BotCredentials(
                        bot_id=str(status.get("ilink_bot_id") or ""),
                        token=str(status.get("bot_token") or ""),
                        base_url=str(status.get("baseurl") or "https://ilinkai.weixin.qq.com"),
                        user_id=str(status.get("ilink_user_id") or ""),
                    )
                    if not creds.token:
                        raise ILinkError(errmsg="确认回调里没有 bot_token")
                    path = weixin_store.save(creds, self.account)
                    self._say("登录成功:bot " + creds.bot_id + " / 凭据已存 " + str(path))
                    self.cursor = ""
                    weixin_store.save_cursor("", self.account)
                    self._apply(creds)
                    return creds
                if state == "scaned":
                    self._say("已扫码,请在手机上确认…")
                elif state == "expired":
                    self._say("二维码过期,重新生成")
                    break
            else:
                self._say("等待超时(5 分钟),重新生成二维码")

    async def reload_credentials(self) -> bool:
        """把磁盘上的新凭据接到当前连接上(网页扫码后用,不重启进程)。

        ⚠️ 只换 token 与 base_url,并清掉游标与内存里的会话窗口:
           换 bot 之后旧的 context_token 一律作废,留着会让第一条回复发不出去。
        """
        creds = weixin_store.load(self.account)
        if creds is None or not creds.token:
            self._say("收到重连通知,但磁盘上没有可用凭据")
            return False
        if creds.token == self.client.token:
            return True
        self._apply(creds)
        self.cursor = weixin_store.load_cursor(self.account) or ""
        self.transport.conversations.clear()
        self._say("凭据已更新:重新连接 bot " + creds.bot_id)
        return True

    def _apply(self, creds: BotCredentials) -> None:
        self.client.token = creds.token
        self.client.base_url = (creds.base_url or self.client.base_url).rstrip("/")

    def _show_qr(self, url: str) -> None:
        self._say("")
        self._say("=" * 60)
        self._say("请用**手机微信**扫描下面的二维码(我 → 设置 → 插件 → ClawBot)")
        self._say("=" * 60)
        try:
            self._say(render_terminal(url))
        except ValueError as exc:
            # 兜底:二维码画不出来时,至少把链接给出来,别让人卡死
            self._say("(二维码渲染失败:" + str(exc) + ")")
        self._say("链接(在手机微信里打开也可以):")
        self._say(url)
        self._say("=" * 60)

    # -- 主动推送 ---------------------------------------------------------
    async def push_text(self, text: str, *, to_user_id: str = "") -> tuple[bool, str]:
        """主动推一条文本(窗口内)。返回 (是否发出, 原因)。

        ⚠️ 与 `send()` 的区别只有一个,但很关键:**调用方要能知道成没成**。
        `send()` 把失败吞进 `last_error`(那一轮对话照常往下走,是对的);
        而"到点提醒"必须先确认送达成功才敢认领 —— 认领 = 已通知,
        没送到却认领掉,这条提醒就再也不会响了(见 la/bridge/reminder_push.py)。

        ⚠️ 额度要**留一格**:一个 context_token 只有 10 条,主动推送把它吃光的话,
        用户再说话时助手就哑了 —— 那比不推更糟。所以只剩最后一条时返回 False,
        让提醒在下一轮重试(窗口刷新后自然就发得出去)。
        """
        body = (text or "").strip()
        if not body:
            return False, "空消息"
        # ⚠️ 先确认"发给谁"再取会话:conversation("") 会往表里建出一个 user_id 为空的
        #    假联系人,把 _only_user() 从此污染掉(它只在恰好一个联系人时才给得出答案)。
        user_id = to_user_id or self.transport._only_user()
        if not user_id:
            return False, "还没有联系人(用户从没发过消息)"
        conv = self.transport.conversation(user_id)
        if not conv.context_token:
            return False, "没有 context_token(用户还没说过话)"
        if not conv.window_open():
            # ⚠️ 别发:token 过期后服务端只会回 ret=-2,一个晚上白打几百次
            return False, ("微信窗口已关(上次说话 "
                           + human_gap(conv.window_age_s() or 0.0) + "前),"
                           "让 TA 给机器人发一句话")
        if conv.remaining_quota <= 1:
            return False, "本窗口额度只剩 " + str(conv.remaining_quota) + " 条,留给回复"
        sent = 0
        chunks = list(split_text(body))
        try:
            for chunk in chunks:
                if conv.remaining_quota <= 1:
                    break
                resp = await self.client.send_text(conv.user_id, chunk, conv.context_token)
                conv.remaining_quota -= 1
                quota = resp.get("remaining") if isinstance(resp, dict) else None
                if isinstance(quota, int):
                    conv.remaining_quota = quota
                sent += 1
        except SessionExpired:
            return False, "会话过期(-14),需要重新扫码"
        except Exception as exc:  # noqa: BLE001 - 推不出去不该打死常驻进程
            if sent == 0:
                return False, type(exc).__name__ + ": " + str(exc)[:160]
            # ⚠️ 部分发送**不能返回 True**(2026-10-18 审计):调用方(提醒推送)
            #    拿到 True 会把整批 item 都 claim 掉,用户只收到前半段,后面永远不补。
            weixin_store.save_session(conv.as_dict())
            return False, ("只发出 " + str(sent) + "/" + str(len(chunks))
                           + " 段后中断:" + type(exc).__name__)
        if sent == 0:
            return False, "一条都没发出去"
        # 额度变了也落一次盘,免得重启后把已经用掉的额度当成还没用
        weixin_store.save_session(conv.as_dict())
        if sent < len(chunks):
            return False, ("只发出 " + str(sent) + "/" + str(len(chunks))
                           + " 段(额度不足),下一轮再补")
        return True, "已推送 " + str(sent) + " 条"

    def _why_no_window(self, wanted: set[str]) -> str:
        """这个成员为什么推不出去:窗口关了(说清多久)还是根本没说过话。"""
        stale = [
            conv for uid, conv in list(self.transport.conversations.items())
            if uid in wanted and conv.context_token
        ]
        if stale:
            age = min((c.window_age_s() or 0.0) for c in stale)
            return ("微信窗口已关(上次说话 " + human_gap(age) + "前),"
                    "让 TA 给机器人发一句话")
        return "该成员还没有可推送的微信窗口(让他先发一句话)"

    async def push_text_to_member(
        self, text: str, member_id: int | None = None
    ) -> tuple[bool, str]:
        """只推给某个成员绑定的微信联系人;member_id=None 时等同 push_text_all。"""
        if member_id is None:
            return await self.push_text_all(text)
        conn = db.connect_assistant()
        try:
            wanted = {
                str(row["external_id"])
                for row in conn.execute(
                    "SELECT external_id FROM app_member_bindings"
                    " WHERE channel = 'weixin' AND member_id = ?",
                    (int(member_id),),
                ).fetchall()
            }
        finally:
            conn.close()
        if not wanted:
            return False, "这个成员还没有绑定微信"
        users = [
            uid
            for uid, conv in list(self.transport.conversations.items())
            if uid in wanted and conv.context_token and conv.remaining_quota > 1
            and conv.window_open()
        ]
        if not users:
            return False, self._why_no_window(wanted)
        sent = 0
        notes: list[str] = []
        for uid in users:
            ok, reason = await self.push_text(text, to_user_id=uid)
            if ok:
                sent += 1
            else:
                notes.append(uid[:8] + ":" + str(reason))
        if sent:
            return True, "推给成员 #" + str(int(member_id)) + " 的 " + str(sent) + " 个联系人"
        return False, ("; ".join(notes)[:200] or "一条都没发出去")

    async def push_file_to_member(
        self, path: Any, name: str, member_id: int | None = None, *, kind: str = "file"
    ) -> tuple[bool, str]:
        """给某个成员绑定的联系人发一个文件(D162)。member_id=None = 所有窗口。"""
        if member_id is None:
            users = [
                uid for uid, conv in list(self.transport.conversations.items())
                if uid and conv.context_token and conv.remaining_quota > 0
                and conv.window_open()
            ]
            target_note = "所有联系人"
        else:
            conn = db.connect_assistant()
            try:
                wanted = {
                    str(row["external_id"])
                    for row in conn.execute(
                        "SELECT external_id FROM app_member_bindings"
                        " WHERE channel = 'weixin' AND member_id = ?",
                        (int(member_id),),
                    ).fetchall()
                }
            finally:
                conn.close()
            if not wanted:
                return False, "这个成员还没有绑定微信"
            users = [
                uid for uid, conv in list(self.transport.conversations.items())
                if uid in wanted and conv.context_token and conv.remaining_quota > 0
                and conv.window_open()
            ]
            target_note = "成员 #" + str(int(member_id))
        if not users:
            if member_id is None:
                return False, "没有可推送的微信窗口(先让用户说一句话)"
            return False, self._why_no_window(wanted)
        item = types.SimpleNamespace(path=str(path), name=str(name), kind=str(kind))
        sent = 0
        notes: list[str] = []
        for uid in users:
            conv = self.transport.conversation(uid)
            if await self.send_file(item, conv):
                sent += 1
            else:
                notes.append(uid[:8] + ":" + str(self.last_error))
        if sent:
            return True, "发给" + target_note + "的 " + str(sent) + " 个联系人"
        return False, ("; ".join(notes)[:200] or "一个都没发出去")

    async def push_text_all(self, text: str) -> tuple[bool, str]:
        """把一条提醒推给**所有**还有窗口的联系人。

        为什么不是 `_only_user()`:家庭版一个 bot 账号会有爸妈两个联系人,
        它在联系人 ≠1 时返回空串,提醒就永远发不出去。日程/提醒默认是全家,
        推给所有绑定联系人是有意为之;个人提醒等 P5 有 owner 后再收窄。
        """
        users = [
            uid
            for uid, conv in list(self.transport.conversations.items())
            if uid and conv.context_token and conv.remaining_quota > 1
            and conv.window_open()
        ]
        if not users:
            return False, "没有可推送的联系人窗口(先让用户在微信里说一句话)"
        sent = 0
        notes: list[str] = []
        for uid in users:
            ok, reason = await self.push_text(text, to_user_id=uid)
            if ok:
                sent += 1
            else:
                notes.append(uid[:8] + ":" + str(reason))
        if sent:
            return True, "推给 " + str(sent) + " 个联系人"
        return False, ("; ".join(notes)[:200] or "一条都没发出去")

    async def _flush_pending(self) -> None:
        """补发待发队列(失败只记日志,不打断主循环)。"""
        try:
            sent = await self.transport.flush_outbox()
        except Exception as exc:  # noqa: BLE001 - 补发失败不该打死常驻进程
            self._say("补发失败:" + type(exc).__name__ + " " + str(exc)[:160])
            return
        if sent:
            left = len(self.transport.outbox)
            self._say("补发 " + str(sent) + " 条(当时网络不通)"
                      + (";还剩 " + str(left) + " 条" if left else ""))

    # -- 主循环 -----------------------------------------------------------
    def stop(self) -> None:
        self._stop = True

    async def run_forever(self, *, max_polls: int | None = None) -> None:
        """常驻:拉消息 → 交给桥接层 → 发回。"""
        backoff = 1.0
        while not self._stop:
            # 网页扫码成功后会留一个 .relogin 标志:在这里(循环边界)换凭据,
            # 不在半路重启进程,也不需要 systemctl 权限。
            if weixin_store.consume_relogin(self.account):
                await self.reload_credentials()
            if max_polls is not None and self.stats["polls"] >= max_polls:
                self._say("已达轮询上限(" + str(max_polls) + "),正常退出")
                return
            try:
                resp = await self.client.get_updates(self.cursor)
                self.stats["polls"] += 1
                backoff = 1.0
                new_cursor = str(resp.get("get_updates_buf") or "")
                if new_cursor and new_cursor != self.cursor:
                    self.cursor = new_cursor
                    weixin_store.save_cursor(new_cursor)
                for raw in resp.get("msgs") or []:
                    self._dispatch(raw)
                # ⭐ 网络通了就顺手把"当时没发出去"的回复补上(2026-10-13 那次断网实测):
                #    轮询成功 = 路通了;没有待发时这一步什么也不做。
                await self._flush_pending()
            except SessionExpired:
                self.stats["relogins"] += 1
                self._say("会话过期(-14):需要重新扫码。先等 "
                          + str(int(RELOGIN_BACKOFF)) + " 秒,避免刷爆扫码窗口")
                # ⚠️ 竞态(2026-10-23 修):网页刚扫完的新凭据就在磁盘上,
                #    无条件 clear() 会把它擦掉,然后这里又去终端重新扫码 ——
                #    用户看到的是"扫了没反应"。只有"磁盘上还是我正在用的那份"才清。
                disk = weixin_store.load(self.account)
                if disk is not None and disk.token and disk.token != self.client.token:
                    self._say("磁盘上的凭据已经换成新的(网页扫码),直接重连")
                    if await self.reload_credentials():
                        continue
                weixin_store.clear(self.account)
                await asyncio.sleep(RELOGIN_BACKOFF)
                try:
                    await self.login(force=True)
                except Exception as exc:  # noqa: BLE001
                    self._say("重新登录失败:" + type(exc).__name__ + " " + str(exc)[:160])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络抖动不该打死常驻进程
                self.stats["errors"] += 1
                self._say("轮询出错:" + type(exc).__name__ + " " + str(exc)[:160]
                          + " → " + str(int(backoff)) + " 秒后重试")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    async def _fetch_media(self, ref: Any, message: Any, index: int) -> Attachment:
        """把一条媒体取回来(下载 + 解密)。**失败也返回一条 Attachment**,带上原因。"""
        from .weixin_media import MediaError, download_media

        external = message.dedup_key() + ":" + str(index)
        try:
            data = await download_media(ref, client=self.client.http)
        except MediaError as exc:
            self._say("媒体取不回来(" + ref.kind + "):" + str(exc)[:160])
            return Attachment(kind=ref.kind, error=str(exc)[:200],
                              external_id=external, raw=ref.as_dict())
        except Exception as exc:  # noqa: BLE001 - 网络层什么都可能抛
            self._say("媒体下载异常:" + type(exc).__name__ + " " + str(exc)[:160])
            return Attachment(kind=ref.kind, error=type(exc).__name__ + ": " + str(exc)[:160],
                              external_id=external, raw=ref.as_dict())
        from ...domain.media import sniff_media_type

        media_type = sniff_media_type(data)
        name = ref.file_name or None
        self._say(
            "取回媒体:" + ref.kind + " " + str(len(data)) + " 字节 (" + media_type + ")"
        )
        return Attachment(
            kind=ref.kind,
            data=data,
            name=name,
            media_type=media_type,
            external_id=external,
            raw=ref.as_dict(),
        )

    def _dump_raw(self, raw: dict[str, Any], reason: str) -> None:
        """把一条没处理成功的原始报文留档(截断到 200KB),便于事后定位字段名。"""
        import json as _json

        from ... import config as _config

        try:
            _config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
            target = _config.LOGS_DIR / (
                "wechat-raw-" + time.strftime("%Y%m%d-%H%M%S") + ".json"
            )
            target.write_text(
                _json.dumps({"reason": reason, "raw": raw}, ensure_ascii=False, default=str)[:200000],
                encoding="utf-8",
            )
            self._say("原始报文已留档:" + str(target))
        except Exception as exc:  # noqa: BLE001 - 留档失败不该影响主流程
            self._say("留档失败:" + type(exc).__name__)

    def _dispatch(self, raw: dict[str, Any]) -> None:
        """把一条入站消息派成一个**独立任务** —— 绝不能在这里 await 到它跑完。

        ⚠️ 2026-10-13 真机踩到的坑,值得写清楚:模型在微信里反问用户时,
        那一轮会**挂在 thread.handle 里等下一句话**(桥接层的提问回环)。
        主循环如果写 `await self._handle_raw(raw)`,那句回答就永远排不到 ——
        它要等的那条消息,正卡在它自己后面。表现是"微信里确认了,毫无反应",
        一直挂到 5 分钟超时;而同一时刻网页上那条问题一直显示"在等你拍板"。
        **串行处理在这里不是保守,是自锁。**

        并发是安全的:bridge 的 Thread 自带走 asyncio.Lock 串行化"轮次",
        而 answer_pending 刻意放在**拿锁之前**(见 bridge.py),所以"回答"那条
        消息能插进去把等待方叫醒;两条普通消息仍然按到达顺序排队。
        """
        task = asyncio.create_task(self._handle_raw(raw))
        self._tasks.add(task)

        def _done(item: asyncio.Task) -> None:
            self._tasks.discard(item)
            if item.cancelled():
                return
            error = item.exception()
            if error is not None:   # 一条消息失败不该打死常驻进程,但**必须响**
                self.stats["errors"] += 1
                self._say(
                    "这条消息处理失败:" + type(error).__name__ + " " + str(error)[:160]
                )

        task.add_done_callback(_done)

    async def _handle_raw(self, raw: dict[str, Any]) -> None:
        message = parse_incoming(raw)
        if message is None:
            return
        # ⭐ 图片/文件:先取回来,再交给桥接层 —— 手机拍错题就是走这条路进来的
        pending, transcribed = split_media_refs(getattr(message, "media", []) or [])
        attachments: list[Attachment] = []
        for index, ref in pending:
            attachments.append(await self._fetch_media(ref, message, index))
        if transcribed:
            self.stats["voice"] = self.stats.get("voice", 0) + transcribed
            self._say(
                "语音已用转写文字(" + str(transcribed) + " 条," + str(len(message.text))
                + " 字),原声不下载"
            )
        unknown = list(getattr(message, "unknown_item_types", []) or [])
        if unknown:
            # 认不出类型的内容**当没发**,只留一行日志 + 原始报文,别拿它去打扰用户
            self._say("消息里有不认识的内容类型 " + str(unknown) + ",已忽略(不当附件)")
            # 每种类型只留一份样例:表情/位置发得勤,照单全留会把 data/logs 撑爆
            fresh = [kind for kind in unknown if kind not in self._unknown_logged]
            if fresh:
                self._unknown_logged.update(fresh)
                self._dump_raw(raw, "不认识的内容类型 " + str(fresh))
        if any(item.error for item in attachments):
            # ⭐ 协议字段名与我们的猜测不一致时,**原始报文是唯一的线索**。
            #    留一份到 data/logs/,真机上出问题才有得查(而不是"没反应")。
            self._dump_raw(raw, "媒体取不回来")
        if not message.text.strip() and not attachments:
            self._say("收到一条既没有文字也没有媒体的消息,忽略")
            return
        self.stats["messages"] += 1
        self.stats["attachments"] = self.stats.get("attachments", 0) + len(attachments)
        conv = self.transport.conversation(message.from_user)
        conv.touch(message)
        # 每收到一条消息就把窗口刷新落盘 —— 主动推送靠它活过下一次重启
        weixin_store.save_session(conv.as_dict())
        inbound = Inbound(
            channel="weixin",
            user_id=message.from_user,
            thread_id=message.from_user,
            text=message.text,
            # ⚠️ 用 message_id 去重,不能用 context_token:
            #    同一个 token 伴随用户的多条消息,拿它去重会吞掉后续真消息。
            external_id=message.dedup_key(),
            attachments=attachments,
            raw=raw,
        )
        thread = self.bridge.thread(message.from_user, require_binding=True)
        from ..bridge import render_for_chat

        events: list[dict[str, Any]] = []
        async for event in thread.handle(inbound):
            events.append(event)
        # 用户刚说话 = token 是新的、额度也回来了:这正是补发的最佳时机(D112)
        await self._flush_pending()
        body = render_for_chat(events)
        # ⭐ 这一轮写出来的文件:桥接层已经按回执认领过了,这里只管发。
        files = [
            OutboundFile(**event["data"])
            for event in events
            if event.get("event") == "file" and event.get("data")
        ]
        if body or files:
            self.transport.last_error = ""
            await self.transport.send(
                SendAction(text=body, files=files, reply_to=message.from_user)
            )
            # ⚠️ **出站失败必须响**。2026-10-11 那次真实的翻车:文件被服务端拒掉,整条链路
            #    一声不吭 —— 正文里还写着"系统会直接发给你",用户在微信里干等(见 D87)。
            #    只记日志不够:用户看不到日志。所以这里**两件事都做**。
            if self.transport.failed_files:
                reason = self.transport.last_error or "原因不明"
                self._say(
                    "出站失败:这些文件没发出去 → "
                    + "、".join(self.transport.failed_files) + ";原因:" + reason
                )
                try:
                    await self.client.send_text(
                        message.from_user,
                        "⚠️ 文件没发出去:" + reason + "(正文照旧,文件可以到电脑上的产物页下载)",
                        message.context_token,
                    )
                except Exception as exc:  # noqa: BLE001 - 补一句失败不该再抛
                    self._say("补发失败说明也失败了:" + type(exc).__name__)
            elif self.transport.last_error:
                # ⚠️ 正文也没发出去(限流 / 额度用完 / 网络):用户那边**什么都收不到**,
                #    而我们连"发一条说明"的额度都没有 —— 那就必须**在日志里响**
                #    (launcher.cmd logs)。静默失败是本项目反复踩过的坑(D87)。
                # ⭐ D112 起这条还会**进待发队列**:路一通就补上,所以这里要报清楚"还剩几条待发"。
                waiting = len(self.transport.outbox)
                self._say(
                    "出站失败:正文没发出去 → " + self.transport.last_error
                    + ("(已存入待发队列,共 " + str(waiting) + " 条,网络恢复后补发)"
                       if waiting else "")
                )

    async def aclose(self) -> None:
        await self.client.aclose()
