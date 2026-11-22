"""聊天页 / SSE 流式问答 / 联网开关。

从 create_app() 搬出来的第八块业务路由(2026-10-18 架构收口)。
流式接口不能在 yield 依赖里建 services,所以生成器内部自己建、自己关。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from .. import config
from ..agent.feedback import build_asker
from ..agent.persona import PersonaStore
from ..agent.prompts import SURFACE_WEB
from ..agent.session import MemorySession
from ..agent.step_ledger import ExecutionLedger
from ..memory.turn_lock import turn_lock
from ..web.idempotency import run_once_async
from ..web.markdown import StreamingMarkdown, render_markdown
from .chat_support import (
    HISTORY_LIMIT,
    _now_ms,
    _receipt_dict,
    _receipt_marks,
    _receipts_since,
    _sse,
    _TraceStep,
)
from .deps import Services, build_services, get_services
from .util import flash
from .visibility import question_visible, receipt_visible, web_session


def _client(settings):
    # ⚠️ 必须**运行时**取 app._build_client:测试会 monkeypatch 它来注入 FakeClient;
    #    如果在 import 时绑死 build_model_client,补丁就失效(2026-10-18 拆分踩过)。
    from . import app as app_module

    return app_module._build_client(settings)


def register_chat_routes(app, *, templates) -> None:
    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request, services: Services = Depends(get_services)):
        """聊天页:把**已经落盘的历史**渲染出来。纯读、零迁移、不经模型。"""
        if services.receipts is None:
            open_receipts = 0
        else:
            open_receipts = services.receipts.count_open()

        history: list[dict[str, str]] = []
        history_more = 0
        try:
            from ..agent.session import MemorySession

            session = MemorySession(
                _client(services.settings), services.settings,
                store=services.store, session_id=web_session(services),
                assistant_conn=services.assistant, auto_briefing=False,
            )
            # 唤醒那一轮(带 via=wake)不进普通聊天时间线;模型自己读历史时仍看得见。
            full_history = session.conversation_history(include_wake=False)
            if len(full_history) > HISTORY_LIMIT:
                history_more = len(full_history) - HISTORY_LIMIT
                full_history = full_history[-HISTORY_LIMIT:]
            history = full_history
        except Exception:  # noqa: BLE001 - 历史渲染失败不该让整页打不开
            history = []

        pending = (
            [item for item in services.questions.recent(limit=10, only_pending=True)
             if question_visible(services, item[0])]
            if services.questions is not None else []
        )
        try:
            assistant_name = PersonaStore(services.assistant).state().name
        except Exception:  # noqa: BLE001 - 名字只是装饰,读不到不该让聊天页挂掉
            assistant_name = "助手"
        return templates.TemplateResponse(
            request,
            "chat.html",
            {"page": "chat", "provider": services.settings.provider,
             "assistant_name": assistant_name,
             # 没有密钥也不该是一片空白:页面顶部给一条可点的提示(见 chat.html)。
             "model_ready": bool(services.settings.api_key)
             or services.settings.provider == "fake",
             "pending_questions": pending,
             "model": services.settings.model if services.settings.provider != "fake" else "离线(fake)",
             "web": _web_state(services),
             "direct_write": services.settings.direct_write,
             "read_only": services.settings.read_only,
             "open_receipts": open_receipts,
             # ⭐ 2026-11-21:「压缩上下文」按钮的回跳提示。普通表单 POST + 303,
             #    不依赖 JS;别的页面也走同一套 ok/err,这里只是把它接到聊天页。
             "ok": request.query_params.get("ok") or "",
             "err": request.query_params.get("err") or "",
             "history": history, "history_more": history_more,
             "recent": (
                 [{**item.as_dict(), "source": "a"}
                  for item in services.receipts.recent(limit=8)
                  if receipt_visible(services, item)]
                 if services.receipts is not None else []
             )},
        )

    @app.post("/context/compact")
    async def context_compact(
        next_url: str = Form("/chat", alias="next"),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ):
        """网页上的「压缩上下文」按钮:点击本身就是确认,不再问一遍。

        ⚠️ 与聊天入口的区别只有一条:这里没有模型参与,直接把当前会话的旧历史压掉。
           所以必须自己拿**跨进程回合锁** —— 否则用户刚点完,微信那边正在跑的一轮
           会与它同时改同一份事件。
        """
        settings = services.settings
        if settings.read_only:
            return flash(
                next_url,
                "只读模式:压缩会改写记忆的投影,先关掉 LA_READ_ONLY。",
                error=True,
            )
        session_id = web_session(services)

        async def _do():
            try:
                session = MemorySession(
                    _client(settings), settings,
                    store=services.store, session_id=session_id,
                    assistant_conn=services.assistant, auto_briefing=False,
                )
                archive_dir = Path(
                    getattr(services.store, "archive_dir", None) or config.ARCHIVE_DIR
                )
                async with turn_lock(
                    session_id, timeout=5.0, directory=archive_dir / ".locks"
                ):
                    result = await session.compact_manual()
            except TimeoutError:
                return flash(
                    next_url, "会话正忙(另一个入口正在处理),稍后再点。", error=True
                )
            except Exception as exc:  # noqa: BLE001 - 按钮失败要给看得懂的回跳,而不是 500
                return flash(
                    next_url,
                    "压缩失败:" + type(exc).__name__ + ": " + str(exc)[:80],
                    error=True,
                )
            if result.get("ok"):
                message = (
                    "已压缩:历史 " + format(int(result["before_tokens"]), ",")
                    + " → " + format(int(result["after_tokens"]), ",")
                    + " token,摘要 #" + str(result["summary_id"])
                    + "(原始事件仍在归档,摘要可检索)。"
                )
                return flash(next_url, message)
            if result.get("status") == "nothing_to_compact":
                return flash(
                    next_url,
                    "现在没有可压的区间:历史还短,或者已经压到保留线。",
                    error=True,
                )
            return flash(
                next_url,
                "这次没有压成:"
                + str(result.get("reason") or result.get("status")) + "。",
                error=True,
            )

        return await run_once_async(
            services,
            idem_token,
            {"op": "compact", "session": session_id},
            _do,
            replay=flash(next_url, "这一次已经处理过了"),
        )

    def _web_state(services: Services) -> dict[str, Any]:
        """联网当前状态。库里的按钮状态优先,.env 的 LA_WEB_ENABLED 只是默认值。"""
        from ..base.web import build_web_backend
        from ..ops import settings_store

        backend = build_web_backend(services.settings)
        return {
            "enabled": settings_store.web_enabled(
                services.assistant, default=services.settings.web_enabled,
            ),
            "provider": services.settings.web_search_provider,
            "fetch_provider": services.settings.web_fetch_provider,
            "label": backend.label,
            "provider_ready": backend.search is not None,
        }

    @app.get("/api/web/status")
    def web_status(services: Services = Depends(get_services)) -> dict[str, Any]:
        return _web_state(services)

    @app.post("/web/toggle")
    def web_toggle(
        next_url: str = Form("/chat", alias="next"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """点一下切换联网。**不改 .env、不重启进程**。"""
        from ..ops import settings_store

        state = _web_state(services)
        if services.assistant is None:
            return flash(next_url, "没有领域库连接,这个开关存不下来", error=True)
        settings_store.set_flag(
            services.assistant, settings_store.WEB_ENABLED_KEY, not state["enabled"],
        )
        if state["enabled"]:
            return flash(next_url, "联网已关:助手不再搜索、不再打开网页。")
        if not state["provider_ready"]:
            return flash(next_url, "联网已开,但没配搜索后端:能打开已知链接,搜索会明确报错。",
                         error=True)
        return flash(next_url, "联网已开:需要查东西时它才会用(检索词会发给搜索服务)。")

    @app.post("/chat/stream")
    async def chat_stream(request: Request, text: str = Form(...)) -> StreamingResponse:
        """SSE 流式问答(执行反馈的主战场)。

        ⚠️ 这里**不能用 Depends(get_services)**:FastAPI 会在响应体发送**之前**
        就退出 yield 依赖,连接会被提前关掉。所以在生成器内部自己建、自己关。
        """

        async def event_stream():
            services = build_services(member=getattr(request.state, "member", None))
            denied: list[str] = []
            task: asyncio.Task | None = None
            queue: asyncio.Queue = asyncio.Queue()
            ledger = ExecutionLedger(lambda frame: queue.put_nowait(("frame", frame)))
            try:
                settings = services.settings
                assert services.receipts is not None
                chat_session = web_session(services)
                receipt_marks = _receipt_marks(services, chat_session)

                def _notify_question(question: Any) -> None:
                    queue.put_nowait(
                        ("frame", {"event": "ask", "data": question.as_dict()})
                    )

                asker = build_asker(
                    log=services.questions, notify=_notify_question,
                    session_id=web_session(services),
                )
                session = MemorySession(
                    _client(settings),
                    settings,
                    store=services.store,
                    session_id=web_session(services),
                    assistant_conn=services.assistant,
                    auto_briefing=False,
                    asker=asker,
                    surface=SURFACE_WEB,
                    auto_approve=settings.direct_write,
                    direct_write=settings.direct_write,
                    approver=lambda spec, params: (denied.append(spec.name), False)[1],
                    on_step=_TraceStep(ledger, queue),
                )
                session.start()
                renderer = StreamingMarkdown()

                yield _sse("status", {"msg": "正在连接模型…", "since": _now_ms()})

                def on_delta(piece: str) -> None:
                    queue.put_nowait(("delta", piece))

                def on_reasoning(piece: str) -> None:
                    queue.put_nowait(("thinking", piece))

                task = asyncio.create_task(
                    session.respond(text, on_delta=on_delta, on_reasoning=on_reasoning)
                )
                last_push = 0.0
                last_think = 0.0
                think_buffer: list[str] = []
                think_chars = 0
                thinking_open = True

                def flush_thinking() -> str:
                    nonlocal think_chars, last_think
                    if not think_buffer:
                        return ""
                    text = "".join(think_buffer)
                    think_buffer.clear()
                    think_chars = 0
                    last_think = asyncio.get_running_loop().time()
                    return _sse("thinking", text)

                while True:
                    try:
                        kind, payload = await asyncio.wait_for(queue.get(), timeout=0.06)
                    except asyncio.TimeoutError:
                        if think_buffer and (
                            asyncio.get_running_loop().time() - last_think >= 0.06
                        ):
                            yield flush_thinking()
                        if task.done() and queue.empty():
                            break
                        yield ": ping\n\n"
                        continue
                    if kind == "delta":
                        if thinking_open:
                            thinking_open = False
                            pending = flush_thinking()
                            if pending:
                                yield pending
                            yield _sse("thinking_end", {})
                        html = renderer.feed(payload)
                        now = asyncio.get_running_loop().time()
                        if now - last_push < 0.08:
                            continue
                        last_push = now
                        yield _sse("html", html)
                    elif kind == "thinking":
                        if not thinking_open:
                            continue
                        think_buffer.append(payload)
                        think_chars += len(payload)
                        if think_chars >= 24:
                            yield flush_thinking()
                    elif kind == "step":
                        yield _sse("step", payload)
                        receipt_id = payload.get("receipt_id")
                        if receipt_id:
                            yield _sse("receipt", {"receipt": _receipt_dict(services, receipt_id)})
                    elif kind == "frame":
                        yield _sse(payload["event"], payload["data"])

                result = await task
                if thinking_open:
                    pending = flush_thinking()
                    if pending:
                        yield pending
                    yield _sse("thinking_end", {})
                final = render_markdown(getattr(result, "text", "") or renderer.text).html
                yield _sse("html", final)

                for receipt in _receipts_since(services, receipt_marks, chat_session):
                    yield _sse("receipt", {"receipt": receipt.as_dict()})

                payload = ledger.finish()
                payload["label"] = ledger.label()
                yield _sse(
                    "done",
                    {
                        "steps": payload["steps"],
                        "steps_meta": payload["steps_meta"],
                        "label": payload["label"],
                        "receipts": [
                            item.as_dict()
                            for item in _receipts_since(services, receipt_marks, chat_session)
                        ],
                    },
                )
                if denied:
                    note = (
                        "以下操作停在待确认状态(请到对应页面手动完成):"
                        + "、".join(sorted(set(denied)))
                    )
                    yield _sse("notice", note)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 聊天失败不该白屏
                message = type(exc).__name__ + ": " + str(exc)[:200]
                yield _sse("error", message)
                yield _sse("done", {"steps": ledger.finish("failed")["steps"],
                                    "steps_meta": ledger.summary()})
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    task.add_done_callback(lambda _t: services.close())
                else:
                    services.close()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
