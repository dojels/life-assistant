"""人格页:查看 / 保存 / 恢复出厂。

从 create_app() 搬出来的第六块业务路由(2026-10-18 架构收口)。
纯读页 + 三种写入(保存 / 重置 / 回执撤销在别处);验收红了先挡“改”,不挡“退”。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config
from ..agent.persona import (
    MAX_CHARS, MAX_NAME_CHARS, MIN_CHARS, PersonaError, PersonaStore,
)
from ..agent.prompts import IDENTITY, PERSONA_BODY, PERSONA_NAME, PERSONA_TAIL
from ..domain.receipts import DOMAIN_SYSTEM
from .deps import Services, get_services
from .idempotency import run_once
from .util import flash
from .visibility import web_session


def register_persona_routes(app, *, templates) -> None:
    # ---------------------------------------------------------------- 人格
    #
    # ⭐ 用户 2026-10-13 的原话:"她的人格,在网页界面看不到。" —— 在此之前人设只活在
    #    la/agent/prompts.py 的一个常量里:要看它得读代码,要改它得改代码 + 重启进程。
    #    这一页把它**摊开**:你写的那半段给编辑框,代码固定的那半段明说不可改,
    #    下面还给出模型这一轮**真正读到**的那段原文(以及它上面压着的边界条款)。
    #
    # ⚠️ 三条口径(与记忆页那三个人工动作同源):
    #    * 改的是**性情与口吻**,不是边界 —— 优先级阶梯由代码无条件追加,页面上根本
    #      拿不到它的编辑框(红线靠结构,不靠自觉,见 D47 / D117);
    #    * 保存写 sys_settings(不是 .env / settings.yaml):**不重启进程**,下一轮就生效
    #      —— 与聊天页那个「联网」按钮同一条优先级;
    #    * 每次保存落一条**可撤销回执**(undo_plan 里带上一版全文),回执托盘里能退回去。
    def _persona_store(services: Services) -> PersonaStore:
        return PersonaStore(services.assistant)

    def _persona_receipt(
        services: Services, *, label: str, detail: dict[str, Any], plan: dict[str, Any]
    ) -> int:
        """人格写入的回执。与记忆页那三个动作**同一套**(写入反馈是项目级机制)。"""
        log = services.receipts
        if log is None:
            return 0
        receipt_id = log.record(
            kind="persona.text", domain=DOMAIN_SYSTEM, label=label, detail=detail,
            undo_plan=plan, actor="user", session_id=web_session(services),
        )
        if receipt_id:
            log.conn.commit()
        return receipt_id

    def _gate_block(services: Services) -> tuple[bool, str]:
        """当前这一版验收是不是红的 —— 红就挡下一次保存(D146)。

        ⚠️ 只挡"改",**不挡"退"**:/persona/reset 与回执撤销永远放行。一个坏了还
           退不回去的系统,比一个不让改的系统糟得多。
        """
        try:
            from ..ops import gate as gate_mod
            from ..ops import version as version_mod

            return gate_mod.should_block(gate_mod.latest(), version_mod.code_fingerprint()[0])
        except Exception:  # noqa: BLE001 - 状态读不到就当没红(不让提示链路阻塞写入)
            return False, ""

    def _start_gate(reason: str) -> None:
        """改完之后**自动**跑一遍 T0。后台线程 + 子进程,不拖慢这次保存。

        ⚠️ 只有 `gate.enable_auto()` 过才真跑(生产入口 `la --web` 会打开它):
           起一次就是整套测试的子进程 —— 测试套件里不能有这种副作用。
        """
        try:
            from ..ops import gate as gate_mod

            if gate_mod.auto_enabled():
                gate_mod.start(reason)
        except Exception:  # noqa: BLE001 - 起不来就只是少了一次自动验收
            pass

    def _persona_context(services: Services, state: Any) -> dict[str, Any]:
        """人格页要展示的一切。⚠️ **纯读**:没有模型、没有网络,模型欠费也改得了。"""
        from datetime import datetime, timezone

        from ..skills import SkillLibrary

        updated = ""
        if state.updated_at:
            try:
                moment = datetime.fromisoformat(str(state.updated_at))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                updated = moment.astimezone(services.settings.tz).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                updated = str(state.updated_at)
        # ⭐ 2026-10-17:系统提示那一块现在是「身份 + 人格 + 技能目录」——
        #    技能目录必须一起摊开:2000 字上限存在的理由就是别把它挤掉,
        #    页面上却一直看不到它(旧的说明还把运行时上下文当成系统提示的最后一段)。
        # ⭐ 验收状态(D146):这一版改完之后,全量测试跑过没有?
        #    ⚠️ 读一份小 JSON,不起进程、不调模型 —— 这一页永远是"纯读"的。
        #    读不到就整块不渲染(状态文件可能正在被原子替换)。
        gate_state: dict[str, Any] = {}
        try:
            from ..ops import gate as gate_mod
            from ..ops import version as version_mod

            status = gate_mod.latest()
            blocked, note = gate_mod.should_block(
                status, version_mod.code_fingerprint()[0])
            gate_state = {
                "line": status.render() if status is not None else "",
                "tone": ("bad" if (status is not None and not status.ok and not status.error)
                         else ("warn" if status is not None and status.error else "good")),
                "blocked": blocked,
                "note": note,
                "running": gate_mod.running() > 0,
                "red": list(status.red) if status is not None else [],
            }
        except Exception:  # noqa: BLE001 - 验收状态只是提示,读不到不该让这一页打不开
            gate_state = {}

        catalog = SkillLibrary().catalog_text()
        identity_tokens = config.estimate_tokens(IDENTITY)
        persona_tokens = config.estimate_tokens(state.full_text)
        catalog_tokens = config.estimate_tokens(catalog)
        return {
            "page": "persona",
            "state": state,
            # 页面上要**明说**哪一段不可改 —— 所以两半都交给模板,不在这里拼死
            "tail": PERSONA_TAIL,
            "tail_chars": len(PERSONA_TAIL),
            "identity": IDENTITY,
            "catalog": catalog,
            "model_text": state.full_text,
            # ⭐ D144:名字是数据 —— 页面标题、表单默认值都读库里的那个,不再写死代码常量
            "persona_name": state.name,
            "name_max": MAX_NAME_CHARS,
            "default_name": PERSONA_NAME,
            "max_chars": MAX_CHARS,
            "min_chars": MIN_CHARS,
            "updated_local": updated,
            # 自定义之后还能对照出厂那一版(否则它从页面上彻底消失)
            "default_body": PERSONA_BODY,
            "default_chars": len(PERSONA_BODY),
            # 三块共用 BUDGET_SYSTEM:把账摆出来,用户才知道"还剩多少"
            "identity_tokens": identity_tokens,
            "persona_tokens": persona_tokens,
            "catalog_tokens": catalog_tokens,
            # ⚠️ 三块**分别**估算再相加(而不是把拼好的串估一次):页面上那行
            #    "332 + 131 + 95" 必须自己加得起来 —— 拼串估一次会比三项之和少 8,
            #    用户一眼就能看出账不对。误差只有几 token,写的是约数。
            "system_tokens": identity_tokens + persona_tokens + catalog_tokens,
            "budget": config.BUDGET_SYSTEM,
            "gate": gate_state,
        }

    def _persona_error_page(
        request: Request, services: Services, text: str, name: str, message: str,
    ):
        """校验不过:一个字都没写,但**把用户写的那一版原样还给他**。

        ⚠️ D145 之前这里是 redirect 回 /persona:憋了十分钟的长文被一句"太长了"带走,
           页面回到库里那一版 —— 用户只能重写。现在直接渲染同一页,draft 状态让模板
           把"当前这一版"标成"还没保存"。
        """
        state = _persona_store(services).state()
        draft = dataclasses.replace(
            state,
            body=str(text or state.body),
            name=(" ".join(str(name or "").split()) or state.name),
        )
        context = _persona_context(services, draft)
        context["err"] = message
        context["draft"] = True
        return templates.TemplateResponse(request, "persona.html", context)

    @app.get("/persona", response_class=HTMLResponse)
    def persona_page(request: Request, services: Services = Depends(get_services)):
        state = _persona_store(services).state()
        context = _persona_context(services, state)
        context["ok"] = request.query_params.get("ok") or ""
        context["err"] = request.query_params.get("err") or ""
        return templates.TemplateResponse(request, "persona.html", context)

    @app.post("/persona/save")
    def persona_save(
        request: Request,
        text: str = Form(""),
        name: str = Form(""),
        force: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ):
        """保存人格的**上半段**(主体 + 名字)。固定尾巴不在这里,也不可能被它顶掉。

        ⭐ D144:名字与主体**一起**存 —— 分两次保存会出现"标题改了、正文没改"的半截状态。
        ⭐ D146:上一版验收是红的就先别改(勾 force 才放行)—— 改完自动跑一遍 T0。
        """
        store = _persona_store(services)

        def action():
            before = store.state()
            forced = str(force or "").strip().lower() in ("1", "on", "true", "yes")
            blocked, gate_note = _gate_block(services)
            if blocked and not forced:
                # 没写进去 —— 把用户写的那一版还回去,并说清楚凭什么拦(可以强推)
                return _persona_error_page(request, services, text, name, gate_note)
            try:
                previous, notes = store.save(text, name)
            except PersonaError as exc:
                # 校验不过 = 没写进去。**如实说是哪一条不过**,并且把写的那一版还回去
                return _persona_error_page(request, services, text, name, str(exc))
            except RuntimeError as exc:  # 没有领域库 → 没地方存
                return _persona_error_page(
                    request, services, text, name, "没存下来:" + str(exc)[:120]
                )
            state = store.state()
            renamed = state.name != before.name
            receipt_id = _persona_receipt(
                services,
                label=(
                    "人格:改名成「%s」并保存(%d 字)" % (state.name, state.chars)
                    if renamed else "人格:改了描述(%d 字)" % state.chars
                ),
                detail={
                    "chars": state.chars, "before_chars": len(previous), "op": "save",
                    "name": state.name, "before_name": before.name if before.name_custom else "",
                },
                # ⭐ 上一版**全文**进回执 —— 撤销才有的退(空串 = 当时还没自定义过)。
                #    D144 起上一版**名字**也一起进:撤销一次要把正文和称呼都退回去。
                plan={
                    "type": "persona", "before": previous,
                    "before_name": before.name if before.name_custom else "",
                },
            )
            message = "人格已保存(%d 字),下一轮对话就是新的她" % state.chars
            if renamed:
                message += ";她的名字改成「%s」了" % state.name
            if notes:
                message += ";" + "、".join(notes)
            if receipt_id:
                message += "(回执 %d,可撤销)" % receipt_id
            # ⭐ D146:改完**自动**验收。不阻塞这次保存 —— 页面顶部会显示"正在跑"。
            _start_gate("人格保存(%d 字)" % state.chars)
            return flash("/persona", message + ";验收在后台跑,约一分半")

        return run_once(
            services, idem_token,
            {"persona": text, "persona_name": name, "force": "1" if force else ""}, action,
            replay=flash("/persona", "这一版刚才已经保存过了。"),
        )

    @app.post("/persona/reset")
    def persona_reset(
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """恢复出厂默认。⭐ 不是"存一份空的" —— 而是**删掉这个键**(库里没有 = 用代码里那份)。

        ⚠️ D146:**永远不挡**。验收红了的时候,退回出厂是唯一的自救动作之一。
        """
        store = _persona_store(services)

        def action():
            before = store.state()
            try:
                previous = store.reset()
            except RuntimeError as exc:
                return flash("/persona", "没改成:" + str(exc)[:120], error=True)
            # ⚠️ 名字也可能单独改过(正文空着、只换了称呼):那也算"动过",不能报"没改动"
            if not previous and not before.name_custom:
                return flash("/persona", "本来就是出厂默认,没有改动。")
            receipt_id = _persona_receipt(
                services,
                label="人格:恢复出厂默认(替下 %d 字)" % len(previous),
                detail={
                    "chars": 0, "before_chars": len(previous), "op": "reset",
                    "name": PERSONA_NAME,
                    "before_name": before.name if before.name_custom else "",
                },
                plan={
                    "type": "persona", "before": previous,
                    "before_name": before.name if before.name_custom else "",
                },
            )
            message = "已恢复出厂默认"
            if before.name_custom:
                message += ",名字也改回「%s」" % PERSONA_NAME
            if receipt_id:
                message += "(你写的那一版在回执 %d 里,可撤销)" % receipt_id
            return flash("/persona", message)

        return run_once(
            services, idem_token, {"persona_reset": "1"}, action,
            replay=flash("/persona", "刚才已经恢复过出厂默认了。"),
        )

