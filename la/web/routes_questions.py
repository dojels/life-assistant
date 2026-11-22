"""统一反馈机制:提问列表 + 回答(JSON 与普通表单两个入口)。

从 create_app() 搬出来的第十二块业务路由(2026-10-18 架构收口)。
同一个入口管所有域;回答会唤醒等待方,没人在等也如实说明。
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Form
from fastapi.responses import RedirectResponse

from ..agent.feedback import get_broker
from .deps import Services, get_services
from .util import safe_next
from .visibility import question_visible


def register_question_routes(app) -> None:
    # ------------------------------------------------- 统一反馈机制:提问与回答
    def _answer_question(
        services: Services, question_id: str, selected: list[str], text: str
    ) -> dict[str, Any]:
        """回答一个问题。**同一个入口管所有域** —— 前端不需要知道是谁问的。"""
        from ..agent.feedback import get_broker

        broker = get_broker()
        # ⚠️ 先看有没有人在等,再回答 —— 回答会把等待方从登记处摘掉。
        question = services.questions.get(question_id) if services.questions is not None else None
        # 进程内 broker 里可能存在**尚未落库**的提问(测试/CLI 路径);
        # 只有库里查得到时才做成员校验,查不到就按旧路径交给 broker。
        if question is not None and not question_visible(services, question):
            return {"ok": False, "error": "这个问题不属于当前成员,不能在这里回答"}
        waiting = broker.is_waiting(question_id)
        answer = broker.answer(
            question_id, selected=selected, text=text, by="user", log=services.questions
        )
        if answer.status != "answered" or answer.by == "duplicate":
            return {
                "ok": False,
                "error": answer.note or "这个问题已经处理过了",
                "status": answer.status,
            }
        # 还在等的那一轮会被唤醒;没人在等(进程重启过 / 对话早结束了)就如实说明,不假装它收到了。
        return {
            "ok": True,
            "question_id": question_id,
            "selected": answer.selected,
            "text": answer.text,
            "resumed": waiting,
            # ⚠️ 不能再说"这一轮对话已经结束了":提问可能来自**另一个进程**(微信桥接 /
            #    提醒推送),那一轮会在 1 秒内从 ops_questions 里认领这个答案并接着做
            #    (2026-10-13 修:见 decision D98 与 tests/test_bridge_ask.py)。
            "note": (
                "已收到你的回答,它接着做"
                if waiting
                else "已收到你的回答(提问来自另一个进程时,它会在几秒内接手)"
            ),
        }

    @app.get("/api/questions")
    def api_questions(
        limit: int = 20, pending: int = 0, services: Services = Depends(get_services)
    ) -> dict[str, Any]:
        """问题列表(纯读,不经过模型)。pending=1 只看还没回答的。"""
        if services.questions is None:
            return {"questions": [], "pending": 0}
        items = [
            (question, answer)
            for question, answer in services.questions.recent(
                limit=max(1, min(limit, 100)), only_pending=bool(pending)
            )
            if question_visible(services, question)
        ]
        return {
            "questions": [
                {**question.as_dict(), "answer": answer.as_dict() if answer else None}
                for question, answer in items
            ],
            "pending": len([
                item for item in services.questions.recent(limit=100, only_pending=True)
                if question_visible(services, item[0])
            ]),
        }

    @app.post("/api/questions/answer")
    def api_questions_answer(
        payload: dict[str, Any], services: Services = Depends(get_services)
    ) -> dict[str, Any]:
        """回答一个问题(JSON,聊天页的按钮走这条)。"""
        selected = payload.get("selected")
        if isinstance(selected, str):
            selected = [selected]
        return _answer_question(
            services,
            str(payload.get("question_id") or payload.get("id") or ""),
            [str(item) for item in (selected or [])],
            str(payload.get("text") or ""),
        )

    @app.post("/questions/answer")
    def questions_answer_form(
        question_id: str = Form(...),
        choice: str = Form(""),
        text: str = Form(""),
        next_url: str = Form("/chat", alias="next"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """回答一个问题(普通表单,**禁用 JS 也能用**)。"""
        _answer_question(services, question_id, [choice] if choice else [], text)
        return RedirectResponse(safe_next(next_url), status_code=303)

