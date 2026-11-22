"""会话线程:一条渠道对话 = 一个 MemorySession + 一条串行队列。

设计要点(为什么不是"直接调 session.respond"):

1. **串行**。同一个会话的两次对话不能并发 —— MemorySession 里有投影、账本、
   回执这些跨轮状态。渠道侧的消息是异步到达的,所以这里必须自己排队。
2. **幂等**。微信的回调会重试(没在 5 秒内应答就重发)。同一 external_id
   只处理一次,否则"记一笔 30 元"会被记两遍。
3. **提问回环**。项目已有的 M9 机制允许模型反问用户。终端里是 input(),
   Web 里是 SSE + POST。在微信里就是"再发一条消息作为回答" —— 所以这里
   要把提问挂起、把下一条入站消息接到那个 future 上。
4. **回复 + 回执一起给**。直写模式下,用户最需要看到的是"它到底改了什么",
   所以写入回执必须跟正文一起发出去,而不是只留在 Web 界面上。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any, AsyncIterator, Awaitable, Callable

from .. import config
from ..agent.feedback import (
    CROSS_PROCESS_POLL_S,
    Answer,
    QuestionLog,
    STATUS_ANSWERED,
    STATUS_CANCELLED,
    build_asker,
    format_question,
)
from ..agent.session import MemorySession
from ..domain.artifacts import KIND_IMAGE, safe_filename
from ..domain.materials import HANDLED_STORED, KIND_VOICE
from ..domain.receipts import DOMAIN_ARTIFACT, format_receipts
from ..family import identity
from ..memory.store import MemoryStore
from ..storage import db

from .client import build_client
from .types import Inbound, OutboundFile, SendAction, Transport

#: 单条消息上限。微信文本上限远大于此,限制是为了防"粘贴一整篇"把投影撑爆
MAX_INBOUND_CHARS = 4000
#: 已见过的 external_id 保留多少条(去重窗口)
DEDUP_SIZE = 512


def _stored_note(material: Any) -> str:
    """"已存成素材 #N"这半句必须**只承诺真的做得到的事**。

    ⚠️ 语音(以及任何音频)读不出文字 —— 微信语音是 SILK/AMR,要靠发消息那边转写。
    以前对语音也写"你可以直接让我读它的内容",模型于是真去读了、读不出来,
    回话里就一直挂着一句"有个我不认识的附件"(2026-10-17 真机,D139)。
    """
    name = str(getattr(material, "original_name", None) or "附件")
    head = "已存成素材 #" + str(getattr(material, "id", 0)) + "(" + name + ")"
    kind = str(getattr(material, "kind", "") or "")
    media_type = str(getattr(material, "media_type", "") or "")
    if kind == KIND_VOICE or media_type.startswith("audio/"):
        return head + ":这是音频,我读不出里面的文字;要我知道内容,发文字或截图给我最快。"
    return head + ",你可以直接让我读它的内容。"


def _receipt_line(receipt: Any) -> str:
    label = getattr(receipt, "label", None) or getattr(receipt, "kind", "写入")
    undoable = getattr(receipt, "undoable", False)
    return "· " + str(label) + ("(可撤销)" if undoable else "")


class Thread:
    """一个会话线程。别直接用,用 Bridge.thread() 拿。"""

    def __init__(
        self,
        thread_id: str,
        *,
        settings: config.Settings,
        transport: Transport,
        session_id: str | None = None,
        unbound: bool = False,
    ) -> None:
        self.thread_id = thread_id
        self.settings = settings
        self.transport = transport
        #: 事件日志的分片键。会话 = 分片,不是记忆边界(与 Web 的 WEB_SESSION_ID 同理)
        self.session_id = session_id or ("bridge:" + thread_id)
        #: 微信渠道要求先绑定成员;未绑定时只回一句固定提示,不进模型/不碰工具。
        self.unbound = bool(unbound)

        self._lock = asyncio.Lock()
        self._pending_question: dict[str, Any] | None = None
        self._pending_future: asyncio.Future | None = None
        self._seen: "OrderedDict[str, bool]" = OrderedDict()
        self._task: asyncio.Task | None = None

        # ⚠️ check_same_thread=False 与 Web 层同一条理由(D66),但这里多一层:
        #    识别是**阻塞**的(视觉模型几秒到十几秒),必须丢进工作线程跑,
        #    而那两个连接是在事件循环线程里建的。用法仍然是严格串行
        #    (handle 里 await 到它结束才继续),不存在两个线程同时用一个连接。
        memory_conn = db.connect_memory(check_same_thread=False)
        assistant_conn = db.connect_assistant(check_same_thread=False)
        store = MemoryStore(memory_conn)
        #: 问题台账 —— 与 Web / CLI 共用同一张 ops_questions。
        #: ⚠️ 它存在的意义不只是留痕:用户在**网页**上回答时,答案只落在这张表里,
        #:    不会碰到本进程的 future。跨进程认领靠它(见 _ask)。
        self.question_log = QuestionLog(assistant_conn)
        self.session = MemorySession(
            build_client(settings),
            settings,
            store=store,
            session_id=self.session_id,
            assistant_conn=assistant_conn,
            auto_approve=settings.direct_write,
            direct_write=settings.direct_write,
            auto_briefing=False,
            # 提问通道 = 这个渠道自己(见下面 _ask)。与 CLI/Web 走同一套
            # Question/Broker/落库路径,"超时不算同意"在这里同样成立。
            asker=build_asker(
                log=self.question_log,
                transport=self._ask,
                session_id=self.session_id,
            ),
        )
        # ⚠️ 进程重启 = 上一轮那个等待方已经不在了。把属于本会话、还标着 pending 的
        #    问题标成 expired —— 否则网页上会永远挂着一条「它在等你拍板」,而没人等。
        try:
            self.question_log.expire_pending(
                self.session_id, note="发起提问的那个进程重启了,这一轮接不上了"
            )
        except Exception:  # noqa: BLE001 - 收拾旧账失败不该影响开新会话
            pass
        self.session.start()

    # -- 提问回环 ---------------------------------------------------------
    async def _ask(self, question: Any):
        """模型/领域层要用户拍板时,把问题发到渠道里,等回答。

        ⚠️ 回答有**两个来源**,谁先到算谁:

          ① **本渠道**:用户直接回一句 —— 下一条入站消息经 answer_pending() 塞进 future;
          ② **别的入口**:他在网页上点了按钮(或终端里敲了一行)。那条路改的是
             共享的 ops_questions 表,**碰不到这个 future**,所以这里必须**看库**。

        2026-10-13 真机踩的坑正是"只有 ①":而当时的适配器还把入站消息串行处理,
        那句回答永远排不到 —— 表现就是"微信里确认了,毫无反应",一直挂到五分钟超时。
        """
        self._pending_question = question
        future = asyncio.get_running_loop().create_future()
        self._pending_future = future
        # ⭐ 必须带 reply_to:家庭版一个 bot 会有多个联系人。
        #    传 None 时适配器回退 _only_user(),而它在联系人 ≠1 时返回 "",
        #    追问就会静默发不出去(还会插入一个 user_id 为空的假会话)。
        await self.transport.send(
            SendAction(text=format_question(question), reply_to=self.thread_id)
        )
        timeout = float(getattr(question, "timeout_s", 300) or 300)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 1.0)
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return Answer(
                        question_id=question.id,
                        status=STATUS_CANCELLED,
                        by="system",
                        note="用户在时限内没有回答",
                    )
                done, _ = await asyncio.wait(
                    {future}, timeout=min(remaining, CROSS_PROCESS_POLL_S)
                )
                if future in done:
                    return future.result()
                # 这一秒渠道里没人回 —— 看看是不是**别的入口**已经答过了。
                # ⚠️ 读库失败绝不能把提问打断:看不到就接着等下一秒。
                try:
                    stored = self.question_log.answer_of(question.id)
                except Exception:
                    stored = None
                if stored is not None:
                    return stored
        finally:
            self._pending_question = None
            self._pending_future = None

    def answer_pending(self, text: str) -> bool:
        """把一条入站消息当成"对上一个提问的回答"。没有提问时返回 False。"""
        future = self._pending_future
        question = self._pending_question
        if future is None or future.done() or question is None:
            return False
        from ..agent.feedback import Answer, STATUS_ANSWERED

        raw = text.strip()
        selected: list[str] = []
        if raw.isdigit() and 1 <= int(raw) <= len(question.options):
            selected = [question.options[int(raw) - 1].key()]
        future.set_result(
            Answer(
                question_id=question.id,
                status=STATUS_ANSWERED,
                selected=selected,
                text=None if selected else raw,
                by="user",
            )
        )
        return True

    # -- 去重 -------------------------------------------------------------
    def seen(self, external_id: str | None) -> bool:
        if not external_id:
            return False
        if external_id in self._seen:
            return True
        self._seen[external_id] = True
        while len(self._seen) > DEDUP_SIZE:
            self._seen.popitem(last=False)
        return False

    # -- 一轮对话 ---------------------------------------------------------
    async def handle(self, inbound: Inbound) -> AsyncIterator[dict[str, Any]]:
        """处理一条入站消息,产出事件流。

        事件是给**适配器**看的(不是给终端用户看的):
            text    正文增量
            receipt 写入回执
            notice  提示(比如"没听懂")
            done    本轮结束(带本轮写了什么)
        """
        if self.seen(inbound.external_id):
            yield {"event": "duplicate", "data": {"external_id": inbound.external_id}}
            return

        if self.unbound:
            # ⭐ 未绑定 = 不进入模型、不落用户消息、不碰工具/领域数据。
            #    只回一句固定提示;管理员在「设置 → 微信绑定」里绑定后,
            #    下次发消息就会走 member:<id>。
            #
            # ⚠️ 但必须留下**联系人标识**(2026-10-18):设置页的“待绑定联系人”
            #    靠 events 里的 bridge:<id> 分片发现新联系人;不写这一条,
            #    用户永远拿不到 external_id,页面还会骗他说“下面会出现”。
            #    只记渠道与标识,不记消息正文。
            try:
                self.session.store.append(
                    self.session_id, "bridge/unbound",
                    {"channel": str(inbound.channel or ""),
                     "external_id": str(self.thread_id or "")},
                )
            except Exception:  # noqa: BLE001 - 记不上标识不该让渠道死掉
                pass
            yield {
                "event": "notice",
                "data": "这个微信还没有绑定家庭成员。请让管理员在「设置 → 微信绑定」里绑定后再说话。",
            }
            yield {"event": "done", "data": {"wrote": 0, "unbound": True}}
            return

        # ⭐ 每一轮都按**这条消息的来源**重设入口:同一个进程可能同时服务
        #    手机微信和终端,构造时定死会把"这一句从哪来"说错。
        self.session.surface = str(inbound.channel or "")

        if self.answer_pending(inbound.text):
            # 这条不该再起一轮对话
            yield {"event": "answered", "data": {"text": inbound.text}}
            return

        # -- 附件(手机那条主路径)----------------------------------------
        # ⭐ 顺序很重要:**先收素材,再决定这一轮说什么**。照片本身不需要模型在场,
        #    所以"只发了一张图"这种情况压根不该起一轮对话(省一次调用、也更快)。
        notes: list[str] = []
        if inbound.attachments:
            notes = await self._handle_attachments(inbound)

        raw_text = inbound.text.strip()
        if not raw_text:
            for note in notes:
                if note:
                    yield {"event": "text", "data": note}
            if notes:
                # ⭐ 把"这张照片最后变成了什么"写进对话事件。
                #    不写的话,用户下一句"刚才那道题呢?"会得到"记忆里没有相关记录" ——
                #    因为模型确实没见过这件事(它只看图,而图没进对话)。
                self._log_attachment_turn(inbound, notes)
                yield {"event": "done", "data": {"wrote": len(notes), "attachments": True}}
            return

        # -- /撤销:回执上的那句提示必须真的能用 ---------------------------
        undo_reply = self._maybe_undo(raw_text)
        if undo_reply is not None:
            yield {"event": "text", "data": undo_reply}
            yield {"event": "done", "data": {"wrote": 0, "undo": True}}
            return

        # 说明行随这一轮一起带给模型,而不是单独发一轮(云朵项目的做法):
        # 用户拍完照往往会顺手补一句"这道题是英语的",那句话必须和照片在同一轮里。
        text = raw_text
        if notes:
            note_line = "【已收到的素材】" + " ".join(item.strip() for item in notes if item)
            text = note_line + "\n" + raw_text
        if len(text) > MAX_INBOUND_CHARS:
            text = text[:MAX_INBOUND_CHARS]
            yield {
                "event": "notice",
                "data": "消息太长,只取前 " + str(MAX_INBOUND_CHARS) + " 个字。",
            }

        async with self._lock:  # 同一个会话严格串行
            await self.transport.typing(self.thread_id)
            # ⚠️ 两本回执账各记各的水位线(2026-10-18 审计修):以前拿 assistant.db 的
            #    last_id 去 memory.db 里 since(),事项层(待办/提醒)的回执永远是空的。
            receipt_marks = self.session.receipt_marks()
            try:
                result = await self.session.respond(text)
            except Exception as exc:  # noqa: BLE001 - 一轮失败不该打死常驻进程
                yield {
                    "event": "notice",
                    "data": "这一轮出错了:" + type(exc).__name__ + " " + str(exc)[:200],
                }
                return

            if result.text:
                yield {"event": "text", "data": result.text}

            # ⚠️ 这里要的是**回执对象**而不只是它的文案:产物回执里带着
            #    artifact_id,是"这一轮到底写出了哪个文件"的唯一可信出处。
            fresh_receipts = self.session.receipts_since(receipt_marks)
            fresh = [_receipt_line(item) for item in fresh_receipts]
            for line in fresh:
                yield {"event": "receipt", "data": line}
            # ⭐ 写出来的文件单独成事件:能不能发出去是**渠道**的事
            #    (微信发文件、终端打路径、网页给下载),桥接层只负责如实报出来。
            #    只发给"能发的渠道",不在这里发。
            files = self._outbound_files(fresh_receipts)
            for item in files:
                yield {"event": "file", "data": item.as_dict()}
            yield {"event": "done", "data": {"wrote": len(fresh), "files": len(files)}}

    # -- 产物文件 ---------------------------------------------------------
    def _outbound_files(self, receipts: list[Any]) -> list[OutboundFile]:
        """从本轮的产物回执里挑出"要真的交给用户"的文件。

        ⚠️ 判据是**回执**,不是让模型自己报文件名 —— 模型报的名字可能是编的,
        回执是写入那一刻留下的(带 artifact_id 与归属),这是唯一可信的出处。

        ⚠️ 只认**这一轮新写出来的**(传进来的就是 since(before) 的结果)。
        把历史产物一并重发的话,用户每说一句话都会再收到一堆旧文件。
        """
        store = getattr(self.session, "artifacts", None)
        if store is None:
            return []
        found: list[OutboundFile] = []
        seen: set[str] = set()
        for receipt in receipts:
            if getattr(receipt, "domain", "") != DOMAIN_ARTIFACT:
                continue
            # 已经撤销掉的产物:文件都被删了,不能再发给用户
            if getattr(receipt, "undone", False):
                continue
            detail = getattr(receipt, "detail", None) or {}
            artifact_id = str(detail.get("artifact_id") or "")
            if not artifact_id or artifact_id in seen:
                continue
            seen.add(artifact_id)
            try:
                artifact = store.get(artifact_id)
                path = store.path_of(artifact)
            except Exception:  # noqa: BLE001 - 登记没了/查不到,都不该让这一轮失败
                continue
            if not path.exists():
                continue
            # ⚠️ 用**回执里记的那个名字**,不是磁盘上的文件名:产物落盘时会加时间戳前缀
            #    (20261013-101500-周报.docx)。照搬 artifact.name 的话,用户收到的是一个
            #    带时间戳的长名字 —— 他要的是"周报.docx"。这个名字会进 URL/协议,所以照样过
            #    safe_filename,不因为是"用户写的"就免检。
            wanted = str(detail.get("name") or "").strip()
            found.append(
                OutboundFile(
                    path=str(path),
                    kind="image" if artifact.kind == KIND_IMAGE else "file",
                    name=safe_filename(wanted, fallback=artifact.name) if wanted else artifact.name,
                )
            )
        return found

    def _log_attachment_turn(self, inbound: Inbound, notes: list[str]) -> None:
        """把一次"只发附件"的往来记进事件日志(与 Web 聊天页同一个口径)。

        ⚠️ 记的是**结果**而不是图片本身:模型看不见图,但它必须知道
        "刚才那张照片被录成了什么",否则下一句追问就对不上。
        """
        try:
            names = [item.name for item in inbound.attachments if item.name]
            self.session.store.append(
                self.session.session_id, "user/message",
                {"text": "[发来 " + str(len(inbound.attachments)) + " 个附件"
                 + (":" + "、".join(str(name) for name in names) if names else "")
                 + "]", "attachments": [item.as_dict() for item in inbound.attachments]},
            )
            self.session.store.append(
                self.session.session_id, "assistant/message",
                {"text": "\n".join(item for item in notes if item), "auto": True},
            )
        except Exception:  # noqa: BLE001 - 记不上事件不该让这次上传失败
            pass

    # -- 附件:素材层 + 拍照录错题 ----------------------------------------
    async def _handle_attachments(self, inbound: Inbound) -> list[str]:
        """把这一条消息里的附件收进素材层,图片顺带走一遍错题流水线。

        ⭐ 全程**不需要聊天模型在场**:识别用的是视觉能力,录入用的是确定性代码。
        所以"只发一张照片"这种情况压根不该起一轮对话 —— 省一次调用,也更快。

        ⚠️ ingest_material 是**同步且阻塞**的(要等视觉模型十几秒),所以丢进工作线程跑。
        连接在建的时候已经声明了 check_same_thread=False,而这里 await 到它结束才继续,
        用法仍然严格串行(与 Web 层同一条纪律,见 D66)。
        """
        import asyncio

        notes: list[str] = []
        materials = getattr(self.session, "materials", None)
        intake = getattr(self.session, "intake", None)
        if materials is None:
            return ["⚠️ 这个会话没有接素材层,附件收不下来。"]

        for index, item in enumerate(inbound.attachments):
            if not item.ok:
                notes.append("⚠️ 有个附件没取回来:" + (item.error or "原因不明"))
                continue
            # 渠道幂等键:微信会重发回调,同一条消息不能入两次素材
            external = item.external_id or (
                (str(inbound.external_id) + ":" + str(index)) if inbound.external_id else None
            )
            try:
                material, fresh = materials.ingest_bytes(
                    item.data or b"",
                    channel=inbound.channel,
                    external_id=external,
                    sender=inbound.user_id,
                    # ⚠️ 用会话 id 而不是 wxid(2026-10-18 审计):绑定成员的会话是
                    #    member:<id>,网页的素材/资产可见性按它判断;写 wxid 会让
                    #    家长在自己网页上看不到微信里刚发的照片。
                    thread_id=getattr(self.session, "session_id", None) or inbound.key(),
                    original_name=item.name,
                    media_type=item.media_type,
                    kind=item.kind,
                )
            except Exception as exc:  # noqa: BLE001 - 一份附件失败不该毁掉整条消息
                notes.append("⚠️ 附件存不下来:" + type(exc).__name__ + " " + str(exc)[:80])
                continue
            if not fresh:
                notes.append(
                    "这张刚才已经收过了(素材 #" + str(material.id) + "),没有重复录入。"
                )
                continue
            if intake is None or material.kind != "image":
                notes.append(_stored_note(material))
                # 归宿如实记成"已留档"。⚠️ 以前一直留在"待处理",而这里**没有任何流水线**
                # 会再来处理它 —— 界面上写"待处理"只会让人(和模型)以为有东西卡住了:
                # 2026-10-17 真机上模型就是照这个词去猜"音频还没转写"的(D139)。
                try:
                    materials.mark(material.id, handled=HANDLED_STORED)
                except Exception:  # noqa: BLE001 - 记不上归宿不该毁掉这条消息
                    pass
                continue
            try:
                outcome = await asyncio.to_thread(
                    intake.ingest_material, material.id,
                    auto_accept=True, actor="user",
                )
            except Exception as exc:  # noqa: BLE001
                notes.append(
                    "照片存成素材 #" + str(material.id) + " 了,但这一步出错了:"
                    + type(exc).__name__ + " " + str(exc)[:80]
                )
                continue
            notes.append(str(outcome.get("message") or "").strip())
        return notes

    # -- /撤销 -------------------------------------------------------------
    def _maybe_undo(self, text: str) -> str | None:
        """认出"/撤销"这类命令并真的执行。不是命令就返回 None(照常走对话)。

        ⚠️ 为什么要有它:回执文案里一直写着"(回复 /撤销 <编号> 可撤销)",
        但**根本没有实现** —— 界面上承诺了、照做了没反应,比不承诺更糟。
        """
        stripped = text.strip()
        lowered = stripped.lower()
        candidates = ("/撤销", "/undo", "/revoke", "撤销", "undo")
        matched = next((item for item in candidates if lowered.startswith(item)), None)
        if matched is None:
            return None
        rest = stripped[len(matched):].strip().lstrip("#").strip()
        # 光说"撤销"两个字当命令;夹在句子里("帮我撤销那笔账")就交回给模型
        if not rest and len(stripped) > 6:
            return None
        reverter = getattr(self.session, "reverter", None)
        if reverter is None:
            return "这个渠道没有接撤销通道,撤销请到电脑上做。"
        try:
            if rest.isdigit():
                info = reverter.undo(int(rest), actor="user")
            else:
                info = reverter.undo_last(actor="user")
        except Exception as exc:  # noqa: BLE001 - 撤不了要说清楚为什么
            return "撤不了:" + str(exc)[:160]
        receipt = info.get("receipt") or {}
        note = str(info.get("note") or "")
        return (
            "已撤销 #" + str(receipt.get("id")) + " " + str(receipt.get("label") or "")
            + (("  → " + note) if note else "")
        )

    def close(self) -> None:
        try:
            self.session.store.conn.close()
        except Exception:
            pass
        conn = getattr(self.session, "assistant_conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


class Bridge:
    """渠道无关的桥。一个进程一个,内部按 thread_id 管若干 Thread。"""

    def __init__(
        self,
        *,
        settings: config.Settings | None = None,
        transport: Transport,
    ) -> None:
        self.settings = settings or config.get_settings()
        self.transport = transport
        self._threads: dict[str, Thread] = {}
        #: 这份 settings 是按哪个配置版本号算出来的。页面改过模型密钥就 +1。
        self._settings_rev: int = -1

    def thread(self, thread_id: str, *, require_binding: bool = False) -> Thread:
        # 页面改了模型密钥 / 接口地址之后,这个进程不该等重启 —— 版本号变了就重建线程,
        # 否则用户看到的是"保存了,微信那边还用旧 key"。
        self._sync_settings()
        found = self._threads.get(thread_id)
        if found is not None:
            # ⚠️ 网页端刚绑定/改绑后,桥接进程里的 Thread 还缓存着旧 session
            #    (它构造时就把 session_id 固定了)。每次取线程都对一次绑定表,
            #    变了就重建 —— 否则设置页说“下一条消息就进新时间线”是假的。
            current = self._member_session_id(thread_id) or ("bridge:" + thread_id)
            if found.session_id != current:
                self.forget(thread_id)
                found = None
        if found is None:
            session_id = self._member_session_id(thread_id)
            found = Thread(
                thread_id,
                settings=self.settings,
                transport=self.transport,
                session_id=session_id,
                unbound=bool(require_binding and session_id is None),
            )
            self._threads[thread_id] = found
        return found

    def _sync_settings(self) -> None:
        """把库里的配置版本号拉进来;变了就丢掉所有 Thread(下次按新 settings 重建)。

        ⚠️ 只清缓存,不动 transport:在途的请求照常走完,新消息才用新配置。
        """
        try:
            from ..ops import secrets as secret_store
            from ..storage import db

            conn = db.connect_assistant()
            try:
                rev = secret_store.sync(conn, force=True)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - 读不到配置不该打死渠道
            return
        if rev == self._settings_rev:
            return
        self._settings_rev = rev
        try:
            self.settings = config.get_settings()
        except Exception:  # noqa: BLE001
            return
        for item in list(self._threads.values()):
            try:
                item.close()
            except Exception:  # noqa: BLE001
                pass
        self._threads.clear()

    def _member_session_id(self, thread_id: str) -> str | None:
        """微信联系人 -> 成员会话。没绑定就返回 None(维持旧的 bridge: 分片)。

        ⚠️ 绑定变更后必须调用 forget(thread_id):Thread 在构造时就把
           session_id 固定了,不重建会继续写旧会话。
        """
        try:
            conn = db.connect_assistant()
            try:
                member_id = identity.member_for_binding(conn, "weixin", thread_id)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - 绑定表读不到不该打死渠道
            return None
        if member_id is None:
            return None
        return identity.session_id_for_member(member_id)

    def forget(self, thread_id: str) -> bool:
        """丢掉一个 Thread(改绑/解绑后用),下次 thread() 会按新绑定重建。"""
        found = self._threads.pop(thread_id, None)
        if found is None:
            return False
        try:
            found.close()
        except Exception:  # noqa: BLE001 - 关不掉也不该影响改绑
            pass
        return True

    def close(self) -> None:
        for item in self._threads.values():
            item.close()
        self._threads.clear()


def render_for_chat(events: list[dict[str, Any]]) -> str:
    """把事件流压成"一条聊天消息"。

    聊天软件不是终端:没有 ANSI、没有折叠、一屏就那么多字。
    所以正文和回执拼成一条,去掉 Markdown 里对聊天无意义的重装饰。
    """
    parts: list[str] = []
    receipts: list[str] = []
    for event in events:
        if event["event"] == "text":
            parts.append(str(event["data"]))
        elif event["event"] == "receipt":
            receipts.append(str(event["data"]))
        elif event["event"] == "notice":
            parts.append("⚠️ " + str(event["data"]))
    body = "\n".join(item for item in parts if item).strip()
    if receipts:
        block = "\n".join(receipts)
        body = (body + "\n\n" if body else "") + "本轮写入:\n" + block + "\n(回复 /撤销 <编号> 可撤销)"
    return body
