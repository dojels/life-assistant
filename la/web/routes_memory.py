"""记忆页与人工动记忆(停用 / 恢复 / 改口)。

从 create_app() 搬出来的第五块业务路由(2026-10-18 架构收口)。
纯读页 + 三种人工动作;改口仍然走准入闸,不另造写库后门。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config
from ..agent.session import MemorySession
from ..memory.fact_gate import FactGate
from .deps import Services, build_model_client, get_services
from .idempotency import run_once, run_once_async
from .visibility import web_session


def _fact_receipt(services: Services, *, label: str, detail: dict[str, Any],
                  plan: dict[str, Any], session_id: str) -> int:
    log = services.memory_receipts
    if log is None:
        return 0
    receipt_id = log.record(
        kind="memory.fact", domain="memory", label=label, detail=detail,
        undo_plan=plan, actor="user", session_id=session_id,
    )
    if receipt_id:
        log.conn.commit()
    return receipt_id


def _memory_admin_guard(request: Request):
    member = getattr(request.state, "member", None)
    if member is not None and str(getattr(member, "role", "")) != "admin":
        return HTMLResponse("<h1>记忆页目前是管理员诊断页</h1>", status_code=403)
    return None


def register_memory_routes(app, *, templates) -> None:
    @app.get("/memory", response_class=HTMLResponse)
    def memory_page(request: Request, services: Services = Depends(get_services)):
        """把每一层记忆摊开 —— 纯读,不经过模型,也不需要密钥。

        ⚠️ 家庭版先只给管理员(2026-10-18 审计):这一页按设计摊开的是
        **全库** facts / summaries / events,没有按成员分片的视图。与其在页面上
        泄露别人的记忆,不如先关掉,等做到“按会话分片的记忆页”再放开。
        """
        from ..web.memory_view import build_memory_view

        member = getattr(services, "member", None)
        if member is not None and str(getattr(member, "role", "")) != "admin":
            return HTMLResponse(
                "<h1>记忆页目前是管理员诊断页</h1>"
                "<p>你的记忆仍然只在自己的会话里使用;要查看请联系管理员。</p>",
                status_code=403,
            )
        view = build_memory_view(
            services.memory, now_text=config.format_local(services.settings.now()),
        )
        return templates.TemplateResponse(
            request,
            "memory.html",
            {
                "page": "memory",
                "view": view,
                "flash": request.query_params.get("flash") or "",
                # ⭐ 2026-11-21:L5 的「立即压缩上下文」按钮回跳提示(与聊天页同一套)。
                "ok": request.query_params.get("ok") or "",
                "err": request.query_params.get("err") or "",
            },
        )

    # ---- 人工动这一层:停用 / 恢复 / 改口(2026-10-13) -------------------------
    # ⭐ 为什么必须有这三个入口:
    #    facts 是唯一**每轮无条件进上下文**的一层,而在此之前页面上只有"看"没有"动" ——
    #    要删一条只能去聊天里跟模型说「忘掉那条关于 X 的事实」,靠模型去调 memory_correct。
    #    实测下来的结果是:11 条生效事实里 5 条有问题(极性写反/疑问句/半句/未来动作),
    #    而用户没有任何直接动作可以处理它们。
    #
    # ⚠️ 三种动作都**不是 DELETE**:
    #    * 停用 = active=0(不再注入、不再被检索命中),但行还在 —— 排查"它当时为什么这么答"
    #      还得靠它(这正是 D64 立这一层时的理由);
    #    * 每一步都写 memory/note 事件 + 一条可撤销回执(undo_plan={"type":"fact"}),
    #      撤销走的是既有的 /api/receipts/undo,不另造一套;
    #    * 改口走**同一条准入闸**(guard + 模型裁决),不是改库后门 ——
    #      因为"规则直接写库"造成的问题,靠"人工直接写库"是修不掉的。
    def _fact_receipt(services: Services, *, label: str, detail: dict[str, Any],
                      plan: dict[str, Any], session_id: str) -> int:
        log = services.memory_receipts
        if log is None:
            return 0
        receipt_id = log.record(
            kind="memory.fact", domain="memory", label=label, detail=detail,
            undo_plan=plan, actor="user", session_id=session_id,
        )
        if receipt_id:
            log.conn.commit()
        return receipt_id

    def _memory_admin_guard(request: Request):
        member = getattr(request.state, "member", None)
        if member is not None and str(getattr(member, "role", "")) != "admin":
            return HTMLResponse(
                "<h1>记忆页目前是管理员诊断页</h1>", status_code=403
            )
        return None

    @app.post("/memory/facts/{fact_id}/deactivate")
    def memory_fact_deactivate(
        request: Request,
        fact_id: int,
        reason: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ):
        """停用一条事实(软删除):不再进上下文,但行留在库里可查、可撤销。"""
        denied = _memory_admin_guard(request)
        if denied is not None:
            return denied
        store = services.store

        def _do():
            fact = store.get_fact(fact_id)
            if fact is None:
                return RedirectResponse(
                    "/memory?flash=没有编号 " + str(fact_id) + " 这条事实", 303
                )
            if not fact.active:
                return RedirectResponse("/memory?flash=#%d 已经是停用状态" % fact_id, 303)
            store.set_fact_active(fact_id, False)
            store.append(
                web_session(services), "memory/note",
                {"content": "人工停用:#" + str(fact_id) + " " + fact.content,
                 "fact_type": fact.type, "explicit": True, "via": "memory_page",
                 "fact_id": fact_id, "reason": reason[:200]},
            )
            receipt_id = _fact_receipt(
                services, label="停用记忆 #" + str(fact_id) + " " + fact.content[:24],
                detail={"fact_id": fact_id, "content": fact.content, "op": "deactivate",
                        "reason": reason[:200]},
                plan={"type": "fact", "fact_id": fact_id, "new_fact_id": 0},
                session_id=web_session(services),
            )
            note = "已停用 #%d(回执 %s,可撤销)" % (fact_id, receipt_id or "—")
            return RedirectResponse("/memory?flash=" + quote(note), 303)

        return run_once(
            services, idem_token,
            {"fact_id": fact_id, "op": "deactivate", "reason": reason[:200]},
            _do,
            replay=RedirectResponse("/memory?flash=这一次已经处理过了", 303),
        )

    @app.post("/memory/facts/{fact_id}/restore")
    def memory_fact_restore(
        request: Request,
        fact_id: int,
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ):
        """把一条被停用/被取代的事实放回生效状态(人工纠正误停)。"""
        denied = _memory_admin_guard(request)
        if denied is not None:
            return denied
        store = services.store

        def _do():
            fact = store.get_fact(fact_id)
            if fact is None:
                return RedirectResponse(
                    "/memory?flash=没有编号 " + str(fact_id) + " 这条事实", 303
                )
            if fact.active:
                return RedirectResponse("/memory?flash=#%d 本来就是生效的" % fact_id, 303)
            store.set_fact_active(fact_id, True)
            store.append(
                web_session(services), "memory/note",
                {"content": "人工恢复:#" + str(fact_id) + " " + fact.content,
                 "fact_type": fact.type, "explicit": True, "via": "memory_page",
                 "fact_id": fact_id},
            )
            # 恢复也要有回执:三种人工动作走同一条口径(否则"谁在什么时候把它放回来了"
            # 只有事件里有、回执托盘里没有 —— 两份记录不该不一样)
            receipt_id = _fact_receipt(
                services, label="恢复记忆 #" + str(fact_id) + " " + fact.content[:24],
                detail={"fact_id": fact_id, "content": fact.content, "op": "restore"},
                plan={"type": "fact", "fact_id": fact_id, "new_fact_id": 0},
                session_id=web_session(services),
            )
            return RedirectResponse(
                "/memory?flash=" + quote(
                    "已恢复 #%d(回执 %s,它会重新每轮进上下文)" % (fact_id, receipt_id or "—")
                ),
                303,
            )

        return run_once(
            services, idem_token, {"fact_id": fact_id, "op": "restore"}, _do,
            replay=RedirectResponse("/memory?flash=这一次已经处理过了", 303),
        )

    @app.post("/memory/facts/{fact_id}/correct")
    async def memory_fact_correct(
        request: Request,
        fact_id: int,
        content: str = Form(...),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ):
        """改口:换一个说法。**过准入闸**(结构检查 + 一次模型裁决),不是直接改库。"""
        denied = _memory_admin_guard(request)
        if denied is not None:
            return denied
        store = services.store
        new_text = str(content or "").strip()

        async def _do():
            fact = store.get_fact(fact_id)
            if fact is None:
                return RedirectResponse(
                    "/memory?flash=没有编号 " + str(fact_id) + " 这条事实", 303
                )
            if not new_text or new_text == fact.content:
                return RedirectResponse("/memory?flash=内容没变", 303)

            # 与 /chat 用**同一套**会话装配(同一个 store、同一个 client 工厂),
            # 这样人工改口走的闸与对话里走的闸是同一个 —— 不另造一套判据。
            session = MemorySession(
                build_model_client(services.settings), services.settings,
                store=store, session_id=web_session(services), auto_briefing=False,
            )
            admission = await session.fact_gate.human_correct(fact_id, new_text)
            if admission.status != "admitted":
                return RedirectResponse(
                    "/memory?flash=" + quote("没改成:" + (admission.reason or admission.status)),
                    303,
                )
            # ⚠️ 回执里必须带**新的事实号**(2026-10-18 审计修):human_correct 会写一条
            #    新事实并把旧的置为失效;只记 fact_id、new_fact_id=0 的话,撤销会把旧
            #    的放回来,而新的那条仍然生效 —— 同一条记忆变成两条都在。
            receipt_id = _fact_receipt(
                services,
                label="改口 #" + str(fact_id) + " " + fact.content[:16]
                      + " → " + new_text[:16],
                detail={"fact_id": fact_id, "new_fact_id": int(admission.fact_id or 0),
                        "content": new_text, "op": "supersede",
                        "from": fact.content, "to": new_text},
                plan={"type": "fact", "op": "supersede", "fact_id": fact_id,
                      "new_fact_id": int(admission.fact_id or 0), "candidate_id": 0},
                session_id=web_session(services),
            )
            note = "已改口 #%d(回执 %s,可撤销)" % (fact_id, receipt_id or "—")
            return RedirectResponse("/memory?flash=" + quote(note), 303)

        return await run_once_async(
            services, idem_token,
            {"fact_id": fact_id, "op": "correct", "to": new_text[:200]},
            _do,
            replay=RedirectResponse("/memory?flash=这一次已经处理过了", 303),
        )

    # ---- 待放行的候选:放行 / 拒收(2026-10-25) -------------------------------
    # ⭐ 用户原话:"图2是网页端的显示,用户也没有审核放行的按钮。"
    #    这一层以前在页面上只有**读**:review 只是一个标签,点不了、也退不掉。
    #    现在两个按钮都走 FactGate.human_release —— 与对话里 memory_review
    #    完全同一条路(同一份判据、同一本回执),不另造一套写库后门。
    @app.post("/memory/candidates/{candidate_id}/approve")
    def memory_candidate_approve(
        request: Request,
        candidate_id: int,
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ):
        """放行:直接写进 facts;**不做语义合并** —— 合并/改写要人自己在 L3 上改口。"""
        denied = _memory_admin_guard(request)
        if denied is not None:
            return denied
        store = services.store
        session_id = web_session(services)

        def _do():
            gate = FactGate(store, client=None, session_id=session_id)
            admission = gate.human_release(
                candidate_id, approve=True, reason="人工放行(记忆页)", via="memory_page",
            )
            if admission.status == "admitted":
                note = "已放行候选 #%d → 事实 #%s" % (candidate_id, admission.fact_id)
                if admission.receipt_id:
                    note += "(回执 %d,可撤销)" % admission.receipt_id
                return RedirectResponse("/memory?flash=" + quote(note), 303)
            if admission.status == "duplicate":
                return RedirectResponse(
                    "/memory?flash=" + quote(
                        "这条和 #%s 是同一条(说法完全一致),没有重复写"
                        % admission.target_fact_id
                    ),
                    303,
                )
            return RedirectResponse(
                "/memory?flash=" + quote("没放行:" + (admission.reason or admission.status)), 303
            )

        return run_once(
            services, idem_token, {"candidate_id": candidate_id, "op": "approve"}, _do,
            replay=RedirectResponse("/memory?flash=这一次已经处理过了", 303),
        )

    @app.post("/memory/candidates/{candidate_id}/reject")
    def memory_candidate_reject(
        request: Request,
        candidate_id: int,
        reason: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ):
        """拒收:候选不再入库(只改队列状态,没有写任何事实,所以没有回执)。"""
        denied = _memory_admin_guard(request)
        if denied is not None:
            return denied
        store = services.store
        session_id = web_session(services)
        note = reason.strip() or "人工拒收(记忆页)"

        def _do():
            gate = FactGate(store, client=None, session_id=session_id)
            admission = gate.human_release(
                candidate_id, approve=False, reason=note, via="memory_page",
            )
            if admission.status == "rejected":
                return RedirectResponse(
                    "/memory?flash=" + quote(
                        "已拒收候选 #%d(不会写进记忆;理由:%s)" % (candidate_id, note[:40])
                    ),
                    303,
                )
            return RedirectResponse(
                "/memory?flash=" + quote("没动:" + (admission.reason or admission.status)), 303
            )

        return run_once(
            services, idem_token,
            {"candidate_id": candidate_id, "op": "reject", "reason": note[:200]}, _do,
            replay=RedirectResponse("/memory?flash=这一次已经处理过了", 303),
        )


