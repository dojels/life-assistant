"""事项层工具(设计深化 §3.7):goal / todo / schedule / skill / memory_note。

全部 risk=low、category=task —— 它们不消耗考古步数预算,但**同样不能在裁决阶段调用**
(ADJUDICATE 一律拒绝工具,这条对事项类也成立)。
"""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Any, Callable

from .. import config
from ..base.phases import CATEGORY_TASK
from ..tasks.goals import GoalError, GoalStore, RevisionConflict, SOURCE_AGENT, SOURCE_USER
from ..tasks.schedules import ScheduleError, ScheduleStore
from ..tasks.todos import STATUS_COMPLETED, STATUS_IN_PROGRESS, STATUS_PENDING
from ..tasks.todos import STATUSES as TODO_STATUSES
from ..tasks.todos import TodoStore
from .registry import RISK_CONFIRM, RISK_LOW, ToolSpec

ANY_PHASE = "any"

#: 待办的动作闭集。老调用不给 action -> 一律当新增(向后兼容)。
TODO_ACTIONS = ("add", "update", "set_due", "set_status", "complete", "reopen", "delete")
#: 批次 A 合并后的动作闭集(见 docs/工具集瘦身对照表.md)。
#: ⚠️ 合并的前提是**门禁字段一致**(risk / phase / writes):同组内不许有 confirm 或只读工具,
#:    否则合并会把确认框或只读闸一起放宽 —— tests/test_m15_tool_merge.py 钉着这条。
SCHEDULE_ACTIONS = ("create", "update", "delete", "list")
GOAL_ACTIONS = ("create", "update")
SKILL_ACTIONS = ("list", "load")
FACT_TYPES = ("identity", "preference", "decision", "other")


def _spec(name: str, description: str, handler, *, required: tuple[str, ...] = (),
          budget: int = 800, timeout_ms: int = 3000, risk: str = RISK_LOW,
          params: dict[str, Any] | None = None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        phase=ANY_PHASE,
        risk=risk,
        timeout_ms=timeout_ms,
        handler=handler,
        output_budget_tokens=budget,
        required=required,
        category=CATEGORY_TASK,
        params=params or {"type": "object", "properties": {}},
    )


def _object(**properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


def _string(desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **extra}


def _int(desc: str, *, minimum: int = 1, maximum: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "description": desc, "minimum": minimum}
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


def _as_proposal(value: Any) -> Any:
    """把 add_fact 的返回值收敛成 FactProposal。

    ⚠️ 两种实现都要认(2026-10-18 审计):
      * 生产: session._add_fact 返回 FactProposal —— 候选队列,**还没有 fact_id**;
      * 测试/同步实现: 直接返回 facts.id 的整数(已经写进库)。
    以前这里只认整数,于是生产环境把「候选编号」当「事实编号」报给了模型。
    """
    from ..memory.fact_gate import FactProposal

    if isinstance(value, FactProposal):
        return value
    try:
        fact_id = int(value or 0)
    except (TypeError, ValueError):
        fact_id = 0
    return FactProposal(
        candidate_id=0,
        status="admitted" if fact_id else "pending",
        fact_id=fact_id,
    )


def _redact(content: str, confidential: bool) -> str:
    """标了敏感的内容不进模型上下文(只给占位符)。与 memory_recall 同一口径。"""
    if not confidential:
        return str(content or "")
    return config.SECRET_PLACEHOLDER + "(敏感内容,不在聊天里展开)"


def _persona_payload(content: str, outcome: Any, *, redirected: bool = False) -> dict[str, Any]:
    """把一次"写进人格页"的结果翻成**模型看得懂的**工具结果。

    ⚠️ 与 memory_note 同一条铁律:没写进去就绝不说"已经记住"。
       declined / unconfirmed / failed 三种情况都必须让模型知道**什么都没发生**。
    """
    status = str(getattr(outcome, "status", "") or "")
    name = str(getattr(outcome, "name", "") or "")
    added = str(getattr(outcome, "added", "") or "")
    notes = [str(item) for item in (getattr(outcome, "notes", ()) or ())]
    receipt_id = int(getattr(outcome, "receipt_id", 0) or 0)
    payload: dict[str, Any] = {
        "content": added or content,
        "status": "persona" if status in ("appended", "unchanged") else status,
        "fact_id": 0,
        "candidate_id": 0,
        "persona_name": name,
        "receipt_id": receipt_id,
        "written": status in ("appended", "unchanged"),
    }
    if status == "appended":
        note = "已写进我的**人格页**(不是记忆页),下一轮对话就生效"
        if getattr(outcome, "renamed", False):
            note += ";我的称呼也改成了「" + name + "」"
        if notes:
            note += ";" + "、".join(notes)
        if receipt_id:
            note += "(回执 " + str(receipt_id) + ",可撤销)"
        payload["note"] = note + "。"
    elif status == "unchanged":
        payload["note"] = "我的**人格页**里本来就有这句,没有重复写。"
    elif status == "declined":
        payload["note"] = (
            "用户选了「先不写」:这句**没有**写进记忆,也没有动我的人格。"
            "不要说你已经记住。"
        )
    elif status == "unconfirmed":
        payload["note"] = (
            "没有拿到用户确认(**这不是同意**):这句**没有**写进记忆,也没有动我的人格。"
            "不要说你已经记住,也不要假定他同意。"
        )
    else:
        payload["note"] = (
            "没写进人格:" + (str(getattr(outcome, "reason", "") or "校验没通过")[:120])
            + "。人格一个字都没改,不要说你已经改好,也不要把它当事实写进记忆。"
        )
    if redirected:
        payload["redirected_from"] = "memory_note"
        payload["why"] = "这句说的是我自己(名字/称呼/性情),归人格页,不归事实层。"
    return payload


def build_task_tools(
    *,
    todos: TodoStore,
    goals: GoalStore,
    schedules: ScheduleStore,
    library: Any,
    add_fact: Callable[..., int],
    memory_note_marker: Callable[[str], None] | None = None,
    memory: Any = None,
    fact_gate: Any = None,
    persona: Any = None,
    append_persona: Callable[..., Any] | None = None,
    session_id: str = "",
) -> list[ToolSpec]:
    """事项层工具。

    ⚠️ **改与删**必须落在这里,而不是提示词里求模型"别乱记":
      * 待办的 set_status / delete 早就写好了(store 层),缺的只是工具入口;
      * 记忆**没有删除**,只有"置为失效 + 互指"(facts 表的不变量第 3 条);
      * 提醒以前只能删了重建,现在有 schedule_update —— 改时间不再换 id。
    """
    async def memory_note(params: dict[str, Any]) -> dict[str, Any]:
        content = str(params.get("content") or "").strip()
        if not content:
            raise ValueError("content 不能为空")
        fact_type = str(params.get("fact_type") or "other")
        if fact_type not in ("identity", "preference", "decision", "other"):
            fact_type = "other"
        confidential = bool(params.get("confidential"))
        # ⭐ 2026-10-25:"我自己是谁"归人格页,不进事实层。
        #
        #   用户原话:*"智能体的英文名应该是属于人格部分的内容,要更新到自己的人格界面"*。
        #   模型看不到这条边界时,会把"助手的英文名是 Mo"当**用户的事实**记下来 ——
        #   于是人格页有一套她的名字、事实层又有另一条,两处各说各的。
        #   这里做一道**确定性**兜底(见 persona.is_self_identity):主语明确是助手/她
        #   自己的名字类陈述,改走人格页;确认与留痕仍由 append_persona(session)负责。
        if append_persona is not None and persona is not None:
            from ..agent.persona import is_self_identity

            try:
                current_name = str(persona.state().name or "")
            except Exception:  # noqa: BLE001 - 读不到名字就按出厂名判
                current_name = ""
            looks_like_mine = (
                is_self_identity(content, current_name) if current_name
                else is_self_identity(content)
            )
            if looks_like_mine:
                outcome = append_persona(
                    content,
                    note="这句说的是我自己(名字/称呼),不是用户的事 —— 它该进人格页。",
                )
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                return _persona_payload(content, outcome, redirected=True)
        produced = add_fact(content, fact_type=fact_type, confidential=confidential)
        # ⭐ 2026-10-22:生产装配传进来的是 **async** 的"先确认再写"回调
        #    (session._confirm_and_add_fact);测试里的同步闭包仍然走原路。
        if inspect.isawaitable(produced):
            produced = await produced
        proposal = _as_proposal(produced)
        if proposal.status in ("declined", "unconfirmed"):
            # ⚠️ 这句话必须让模型看见"没写":否则它下一句又会说"记好了"。
            note = (
                "用户说了不记,这条**没有**写进记忆。不要说你已经记住;"
                "如果他还想记,再问一次。"
                if proposal.status == "declined"
                else "没有拿到用户的确认(**这不是同意**),这条**没有**写进记忆。"
                "不要说你已经记住,也不要假定他同意。"
            )
            return {
                "content": config.SECRET_PLACEHOLDER if confidential else content,
                "fact_id": 0,
                "status": proposal.status,
                "written": False,
                "note": note,
            }
        if memory_note_marker is not None:
            memory_note_marker(content)
        # 设计深化 §3.8:标了敏感的内容在**工具结果里**就要替换成占位符 ——
        # 工具结果是原样进模型上下文的,这里不脱敏等于"标了敏感"只是个摆设。
        echoed = config.SECRET_PLACEHOLDER if confidential else content
        payload: dict[str, Any] = {
            "content": echoed, "fact_type": fact_type, "confidential": confidential,
        }
        if proposal.fact_id:
            payload.update({
                "fact_id": proposal.fact_id, "candidate_id": proposal.candidate_id,
                "status": "admitted",
                "note": "已写进永久事实(编号 " + str(proposal.fact_id) + ")。",
            })
        elif proposal.status == "review":
            payload.update({
                "fact_id": 0, "candidate_id": proposal.candidate_id,
                "status": "review",
                "note": (
                    "用户已经确认,但准入闸判为**待人工复核**,现在还没有生效、"
                    "也没有事实编号。不要说你已经记住,如实说在等他复核。"
                ),
            })
        elif proposal.status == "duplicate":
            payload.update({
                "fact_id": 0, "candidate_id": proposal.candidate_id,
                "status": "duplicate",
                "note": "这句话已经是记忆里的一条(或已经在候选队列里排过),没有重复写。",
            })
        elif proposal.status == "rejected":
            payload.update({
                "fact_id": 0, "candidate_id": proposal.candidate_id,
                "status": "rejected",
                "note": (
                    "用户确认了,但这条没通过结构检查(空/残句/疑问句/超长/未来动作),"
                    "**没有写进记忆**。不要说你已经记住;可以问他要一句完整、确定的说法。"
                ),
            })
        else:
            payload.update({
                "fact_id": 0, "candidate_id": proposal.candidate_id,
                "status": "pending",
                "note": (
                    "已排进候选队列(第 " + str(proposal.candidate_id) + " 号),"
                    "裁决通过后才会生效。现在**还没有**事实编号 —— "
                    "别把这个候选号拿去 memory_correct。"
                ),
            })
        return payload

    def _need_todo(todo_id: str):
        todo = todos.get(todo_id) if todo_id else None
        if todo is None:
            raise ValueError(
                "没有这条待办: " + (todo_id or "(没给 id)")
                + "。先用 todo_list 拿到 id —— 不要凭内容猜。"
            )
        return todo

    def _todo_payload(todo: Any) -> dict[str, Any]:
        from ..tasks.todos import alarm_at

        return {
            "id": todo.id, "content": todo.content, "status": todo.status,
            "due_date_local": todo.due_date_local,
            "due_at_local": todo.due_at_local,
            "remind_minutes_before": todo.remind_minutes_before,
            # 闹钟什么时候响 —— 模型要能一句话把它说出来,而不是让用户自己去算
            "alarm_at": alarm_at(todo.due_at_local, todo.remind_minutes_before),
        }

    async def memory_correct(params: dict[str, Any]) -> dict[str, Any]:
        """改一条已经记住的事实;**没有删除**。

        ⭐ 记忆是"改口",不是"抹掉":旧说法置为失效(不再注入、不再被检索命中),
          但仍然留在库里可查 —— 这正是 facts 表 active / superseded_by 两列的用途。
        """
        if memory is None:
            raise ValueError("这个会话没有接记忆库,改不了事实")
        try:
            fact_id = int(params.get("fact_id") or 0)
        except (TypeError, ValueError):
            fact_id = 0
        if not fact_id:
            raise ValueError("要给 fact_id(先 memory_recall 拿到它)")
        old = memory.get_fact(fact_id)
        if old is None:
            raise ValueError("记忆里没有编号 " + str(fact_id) + " 这条事实")
        if not old.active:
            raise ValueError("编号 " + str(fact_id) + " 这条已经被改过/失效了,不用再改")

        content = str(params.get("content") or "").strip()
        fact_type = str(params.get("fact_type") or "").strip()
        if not content and fact_type:
            if fact_type not in FACT_TYPES:
                raise ValueError(
                    "fact_type 只能是 " + "/".join(FACT_TYPES) + "(收到的是 " + fact_type + ")"
                )
            return _retype_fact(old, fact_id, fact_type)
        if not content:
            if not memory.set_fact_active(fact_id, False):
                raise ValueError("这条事实已经不在了")
            _mark_memory("memory_correct:forget", old.content)
            receipt_id = _record_memory_change(
                label="忘掉:" + old.content[:36],
                detail={"fact_id": fact_id, "content": old.content, "op": "forget"},
                plan={"type": "fact", "op": "forget", "fact_id": fact_id, "new_fact_id": 0},
            )
            return {
                "fact_id": old.id, "content": old.content, "active": False,
                "receipt_id": receipt_id,
                "note": "这条不再进记忆(旧记录仍留在库里可查);想恢复就让用户撤销这一次写入。",
            }
        resolved = fact_type if fact_type in FACT_TYPES else (old.type or "other")
        produced = add_fact(content, fact_type=resolved, supersedes=fact_id)
        # ⭐ 与 memory_note 同一条口径:改口也要先确认(生产装配传的是 async 回调)。
        if inspect.isawaitable(produced):
            produced = await produced
        proposal = _as_proposal(produced)
        if proposal.status in ("declined", "unconfirmed"):
            return {
                "fact_id": fact_id, "content": old.content, "fact_type": old.type,
                "status": proposal.status, "written": False,
                "note": (
                    "用户没有确认这次改口,旧说法**仍然有效**,库里什么都没改。"
                    "不要说你已经改好。"
                ),
            }
        if proposal.status == "review":
            return {
                "fact_id": fact_id, "content": old.content, "fact_type": old.type,
                "status": "review", "candidate_id": proposal.candidate_id,
                "written": False,
                "note": (
                    "用户已经确认这次改口,但准入闸判为**待人工复核**;"
                    "新说法还没有生效,旧说法仍然有效。不要说你已经改好。"
                ),
            }
        if proposal.status == "rejected":
            return {
                "fact_id": fact_id, "content": old.content, "fact_type": old.type,
                "status": "rejected", "candidate_id": proposal.candidate_id,
                "written": False,
                "note": (
                    "用户确认了,但新说法没通过结构检查,**没有改成**;"
                    "旧说法仍然有效。不要说你已经改好。"
                ),
            }
        _mark_memory("memory_correct:supersede", content)

        # ---- 同步落库:注入的 add_fact 直接写了 facts(测试 / 同步实现) ----
        if proposal.fact_id:
            if proposal.fact_id == fact_id:
                return {
                    "fact_id": fact_id, "content": content, "fact_type": resolved,
                    "superseded": None,
                    "note": "这句话库里已经有了,没有重复写。",
                }
            receipt_id = _record_memory_change(
                label="改口:" + old.content[:24] + " → " + content[:24],
                detail={"fact_id": fact_id, "new_fact_id": proposal.fact_id,
                        "op": "supersede", "from": old.content, "to": content},
                plan={"type": "fact", "op": "supersede", "fact_id": fact_id,
                      "new_fact_id": proposal.fact_id, "candidate_id": 0},
            )
            return {
                "old_fact_id": fact_id, "fact_id": proposal.fact_id, "content": content,
                "fact_type": resolved, "superseded": old.content,
                "receipt_id": receipt_id,
                "note": "已经改口:旧说法置为失效(不再注入、不再被检索命中),新说法生效。",
            }

        # ---- 候选队列(生产路径):裁决通过后才生效,旧说法在此之前仍然有效 ----
        receipt_id = _record_memory_change(
            label="改口(待裁决):" + old.content[:20] + " → " + content[:20],
            detail={"fact_id": fact_id, "candidate_id": proposal.candidate_id,
                    "op": "supersede_pending", "from": old.content, "to": content},
            plan={"type": "fact", "op": "supersede_pending", "fact_id": fact_id,
                  "candidate_id": proposal.candidate_id, "new_fact_id": 0},
        )
        return {
            "old_fact_id": fact_id, "fact_id": 0,
            "candidate_id": proposal.candidate_id,
            "content": content, "fact_type": resolved,
            "superseded": None, "receipt_id": receipt_id,
            "status": "pending",
            "note": (
                "改口已排进候选队列(第 " + str(proposal.candidate_id) + " 号):"
                "裁决通过后新说法生效、旧说法置为失效;在那之前旧说法仍然有效。"
                "现在**还没有**新的事实编号。"
            ),
        }

    def _retype_fact(old: Any, fact_id: int, fact_type: str) -> dict[str, Any]:
        """只改类别(说法不动),并且留下可撤销的回执。"""
        before = str(old.type or "other")
        if fact_type == before:
            return {
                "fact_id": old.id, "content": old.content, "fact_type": fact_type,
                "active": True, "receipt_id": 0,
                "note": "类别本来就是 " + fact_type + ",没动。",
            }
        if not memory.set_fact_type(fact_id, fact_type):
            raise ValueError("这条事实已经不在了")
        _mark_memory("memory_correct:retype", old.content)
        receipt_id = _record_memory_change(
            label="改类别:" + old.content[:24] + "(" + before + " → " + fact_type + ")",
            detail={"fact_id": fact_id, "op": "retype", "from": before, "to": fact_type},
            plan={"type": "fact", "op": "retype", "fact_id": fact_id,
                  "before_type": before},
        )
        return {
            "fact_id": old.id, "content": old.content, "fact_type": fact_type,
            "active": True, "receipt_id": receipt_id,
            "note": "类别改成 " + fact_type + " 了(说法没动)。",
        }

    async def memory_recall(params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query") or "").strip()
        limit = int(params.get("limit") or 10)
        limit = min(max(limit, 1), 30)
        if memory is None:
            raise ValueError("这个会话没有接记忆库,查不了事实")
        scope = session_id or None
        if not query:
            rows = memory.active_facts(limit=limit, session_id=scope)
            mode = "active"
        elif hasattr(memory, "search_facts"):
            rows = memory.search_facts(query, limit=limit, session_id=scope)
            mode = "search"
        else:
            rows = memory.active_facts(limit=limit, session_id=scope)
            mode = "active"
        return {
            "mode": mode,
            "count": len(rows),
            # 敏感事实只给编号不给内容(设计深化 §4.2:已保存但不在对话中展示)
            "facts": [
                {
                    "fact_id": row.id,
                    "content": (
                        config.SECRET_PLACEHOLDER
                        if getattr(row, "confidential", False) else row.content
                    ),
                    "fact_type": row.type,
                    "confidential": bool(getattr(row, "confidential", False)),
                }
                for row in rows
            ],
            "coverage": memory.fact_coverage() if hasattr(memory, "fact_coverage") else None,
            "note": (
                "这只是**长期事实**(偏好/身份/决定)。"
                + ("按关键词筛的。" if mode == "search" else "最新的在前,不是全部。")
                + "要改其中一条:memory_correct(fact_id=…)。"
            ),
        }

    async def memory_review(params: dict[str, Any]) -> dict[str, Any]:
        """看 / 放行 / 拒收**待人工复核**的记忆候选(2026-10-25)。

        ⭐ 用户原话:*"看这段对话,用户在微信端,无法让智能体写入事实。"*
           微信端没有记忆页,唯一的入口就是这里:用户说"通过吧",模型调 approve。
        ⚠️ approve 只在用户**明确说**通过/放行/可以 时调用;拿不准就先 action=list
           把内容说给他听。"我自己放行"不算数。
        """
        if fact_gate is None:
            raise ValueError("这个会话没有接记忆库,看不了待放行的候选")
        action = str(params.get("action") or "list").strip().lower() or "list"
        if action in ("list", "queue"):
            items = [
                {**item, "content": _redact(item["content"], bool(item.get("confidential")))}
                for item in fact_gate.review_queue(limit=5)
            ]
            if not items:
                return {"count": 0, "items": [],
                        "note": "现在没有等我放行的记忆候选 —— 不用再问用户了。"}
            return {
                "count": len(items),
                "items": items,
                "note": (
                    "这些是准入闸判不准的候选,**还没有生效**。把它们是什么说给用户听;"
                    "他明确说「通过 / 放行」就 approve(给 candidate_id),"
                    "说「不要 / 算了」就 reject。不要自己替他决定。"
                ),
            }
        if action not in ("approve", "reject"):
            raise ValueError("action 只能是 list / approve / reject(收到 " + action + ")")
        candidate_id = int(params.get("candidate_id") or 0)
        if not candidate_id:
            # 没给编号 = 处理最近那条待放行的(微信里用户只会说"通过吧")
            items = fact_gate.review_queue(limit=1)
            if not items:
                return {"status": "empty",
                        "note": "没有等我放行的候选,这次什么都没改。"}
            candidate_id = int(items[0]["candidate_id"])
        admission = fact_gate.human_release(
            candidate_id,
            approve=(action == "approve"),
            reason=str(params.get("reason") or ("用户在对话里放行" if action == "approve" else "用户在对话里拒收")),
            via="chat",
        )
        # 标了敏感的候选:内容不进上下文,只给编号(与 memory_recall 同一口径)
        row = fact_gate.store.get_candidate(candidate_id) if fact_gate is not None else None
        confidential = bool(row["confidential"]) if row is not None else False
        payload: dict[str, Any] = {
            "candidate_id": candidate_id,
            "status": admission.status,
            "content": _redact(admission.content, confidential),
            "confidential": confidential,
        }
        if admission.status == "admitted":
            payload.update({
                "fact_id": int(admission.fact_id or 0),
                "note": "放行成功:它已经写进长期记忆(编号 "
                        + str(int(admission.fact_id or 0)) + "),这一轮就能用上。",
            })
        elif admission.status == "duplicate":
            payload.update({
                "fact_id": 0,
                "note": "放行时发现库里**已经有同一句**(编号 "
                        + str(int(admission.target_fact_id or 0)) + "),没有重复写。",
            })
        elif admission.status == "rejected":
            payload["note"] = "已拒收:" + (admission.reason or "") + "。它不会写进记忆。"
        else:
            payload["note"] = "没处理:" + (admission.reason or admission.status) + "。"
        return payload

    async def persona_note(params: dict[str, Any]) -> dict[str, Any]:
        """把一句关于**她自己**的话写进人格页(名字 / 英文名 / 称呼 / 性情)。

        ⭐ 与 memory_note 的分工:**用户的事**进记忆,**她自己的事**进人格。
           人格是数据(sys_settings),下一轮就生效,人格页上直接看得到。
        """
        if append_persona is None:
            raise ValueError("这个会话没有接人格库,改不了人格")
        text = str(params.get("text") or "").strip()
        name = str(params.get("name") or "").strip()
        if not text and not name:
            raise ValueError("text 与 name 至少要给一个")
        outcome = append_persona(text, name=name or None)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return _persona_payload(text or name, outcome)

    async def todo_write(params: dict[str, Any]) -> dict[str, Any]:
        """待办:加、改、勾、删。⚠️ 兼容老调用(不给 action = 新增)。"""
        action = str(params.get("action") or "add").strip() or "add"
        if action not in TODO_ACTIONS:
            raise ValueError(
                "不认识的 action: " + action + "(可选 " + "/".join(TODO_ACTIONS) + ")"
            )
        todo_id = str(params.get("todo_id") or "").strip()
        content = str(params.get("content") or "").strip()
        due = params.get("due_date_local")

        if action == "add":
            if not content:
                raise ValueError("新增待办要给 content")
            todo = todos.add(
                content,
                source=str(params.get("source") or SOURCE_AGENT),
                due_date_local=due,
                due_at_local=params.get("due_at_local"),
                remind_minutes_before=params.get("remind_minutes_before"),
            )
            payload = _todo_payload(todo)
            payload["action"] = "add"
            payload["note"] = (
                ("记下了;" + str(payload["alarm_at"]) + " 我会提醒你一次。")
                if payload["alarm_at"] else "记下了。"
            ) + "做完用 action=complete 勾掉它(闹钟也会跟着停)。"
            return payload

        if not todo_id:
            raise ValueError("action=" + action + " 要给 todo_id(先 todo_list 拿 id)")
        todo = _need_todo(todo_id)

        if action == "update":
            if not content:
                raise ValueError("action=update 要给新的 content")
            if content == todo.content:
                return {"action": "update", **_todo_payload(todo), "note": "内容和原来一样,没动。"}
            updated = todos.set_content(todo_id, content)
            return {
                "action": "update", **_todo_payload(updated), "before": todo.content,
                "note": "内容改了(想退回去就让用户撤销这一次写入)。",
            }

        if action == "set_due":
            # ⚠️ 只给日期 = 只挪日期(时刻与提前量留着);给了时刻就按新时刻重排闹钟
            if params.get("due_at_local") in (None, "") and "due_at_local" not in params:
                updated = todos.set_due_date(todo_id, due)
            else:
                updated = todos.set_due(
                    todo_id,
                    due,
                    due_at_local=params.get("due_at_local"),
                    remind_minutes_before=params.get("remind_minutes_before"),
                )
            return {
                "action": "set_due", **_todo_payload(updated),
                "before": {"due_date_local": todo.due_date_local,
                           "due_at_local": todo.due_at_local},
            }

        if action == "set_status":
            target = str(params.get("status") or "").strip()
            if target not in TODO_STATUSES:
                raise ValueError("status 只能是 " + "/".join(TODO_STATUSES))
            updated = todos.set_status(todo_id, target)
            return {"action": "set_status", **_todo_payload(updated)}

        if action == "complete":
            updated = todos.set_status(todo_id, STATUS_COMPLETED)
            return {
                "action": "complete", **_todo_payload(updated),
                "note": "勾掉了。" + ("它的提醒也停了。" if todo.due_at_local else ""),
            }

        if action == "reopen":
            updated = todos.set_status(todo_id, STATUS_PENDING)
            return {"action": "reopen", **_todo_payload(updated)}

        # action == "delete"
        if not todos.delete(todo_id, record_receipt=True):
            raise ValueError("这条待办已经不在了")
        return {
            "action": "delete", "id": todo_id, "content": todo.content,
            "note": "删了;这一步有回执,撤销能把原样放回来。",
        }

    async def todo_list(params: dict[str, Any]) -> dict[str, Any]:
        status_filter = str(params.get("filter") or "pending")
        items = todos.list(status=status_filter)
        counts = todos.counts()
        return {
            "count": len(items),
            "todos": [_todo_payload(item) for item in items],
            # ⭐ 覆盖域:列表只有前 N 条,这里如实给全量分布 ——
            #    否则模型会把"我看到的 8 条"说成"你一共有 8 条待办"。
            "coverage": {
                "rows": counts["all"],
                "open": counts["open"],
                "done": counts[STATUS_COMPLETED],
                "in_progress": counts[STATUS_IN_PROGRESS],
                "shown": len(items),
                "as_of": config.get_settings().now().isoformat(timespec="seconds"),
            },
        }

    def _mark_memory(event: str, content: str) -> None:
        """记忆的每次改口都留一条事件 —— 与 memory_note 同一口径,可回放。"""
        if memory_note_marker is not None:
            memory_note_marker(event + " | " + content)

    def _memory_receipts() -> Any:
        """记忆库上的回执本(事实住在 memory.db,回执必须同库同事务)。

        ⚠️ add_fact 自己**不记回执** —— 它被规则识别等多条路径共用。
        所以"改口/遗忘"这条明确的、用户看得见的写入,回执在这里记。
        """
        if memory is None or not hasattr(memory, "conn"):
            return None
        from ..domain.receipts import DOMAIN_MEMORY, ReceiptLog

        log = ReceiptLog(memory.conn)
        if log._table_ready():
            return (log, DOMAIN_MEMORY)
        return None

    def _record_memory_change(*, label: str, detail: dict[str, Any], plan: dict[str, Any]) -> int:
        book = _memory_receipts()
        if book is None:
            return 0
        log, domain = book
        receipt_id = log.record(
            kind="memory.fact", domain=domain, label=label, detail=detail, undo_plan=plan,
            actor="agent",
        )
        if receipt_id:
            log.conn.commit()
        return receipt_id

    async def schedule_update(params: dict[str, Any]) -> dict[str, Any]:
        """改一条已存在的提醒(改时刻 / 改间隔 / 改内容)。**id 不变**。"""
        schedule_id = str(params.get("id") or "").strip()
        if not schedule_id:
            raise ValueError("要给 id(先 schedule_list 拿到它)")
        before = schedules.get(schedule_id)
        if before is None:
            raise ValueError("没有这条提醒: " + schedule_id)
        every = params.get("every_seconds")
        next_at = params.get("at")
        prompt = params.get("prompt")
        if every in (None, "") and next_at in (None, "") and prompt in (None, ""):
            raise ValueError(
                "至少要给一个要改的字段:at(新时刻,ISO8601)/ every_seconds(新间隔,≥300)/ prompt"
            )
        updated = schedules.update(
            schedule_id,
            prompt=str(prompt) if prompt not in (None, "") else None,
            at=str(next_at) if next_at not in (None, "") else None,
            every_seconds=int(every) if every not in (None, "") else None,
        )
        return {
            "id": updated.id, "kind": updated.kind, "at": updated.at,
            "every_seconds": updated.every_seconds, "prompt": updated.prompt,
            "before": {"at": before.at, "every_seconds": before.every_seconds,
                       "prompt": before.prompt},
            "note": (
                "改好了(id 没变)。重复提醒的下一次触发按新时刻重新算 —— "
                "已经响过的不再算数,所以不会出现「改了时间反而不响」。"
            ),
        }

    async def schedule_create(params: dict[str, Any]) -> dict[str, Any]:
        every_seconds = params.get("every_seconds")
        kind = str(params.get("kind") or ("every" if every_seconds else "at"))
        schedule = schedules.create(
            str(params.get("prompt") or ""),
            kind=kind,
            at=params.get("at"),
            every_seconds=int(every_seconds) if every_seconds else None,
        )
        return {
            "id": schedule.id, "kind": schedule.kind, "at": schedule.at,
            "every_seconds": schedule.every_seconds, "prompt": schedule.prompt,
        }

    async def schedule_list(params: dict[str, Any]) -> dict[str, Any]:
        enabled_only = bool(params.get("enabled_only", True))
        items = schedules.list(enabled_only=enabled_only)
        everything = schedules.list(enabled_only=False, limit=200)
        return {
            "count": len(items),
            "schedules": [
                {"id": item.id, "kind": item.kind, "at": item.at,
                 "every_seconds": item.every_seconds, "prompt": item.prompt}
                for item in items
            ],
            "coverage": {
                "rows": len(everything),
                "enabled": sum(1 for item in everything if item.enabled),
                "shown": len(items),
                "as_of": config.get_settings().now().isoformat(timespec="seconds"),
            },
        }

    async def schedule_delete(params: dict[str, Any]) -> dict[str, Any]:
        schedule_id = str(params.get("id") or "")
        existed = schedules.get(schedule_id) is not None
        return {
            "id": schedule_id,
            "deleted": schedules.delete(schedule_id, record_receipt=True),
            "note": ("删了;撤销能把这条提醒原样放回来(编号不变)。" if existed else ""),
        }

    # -- 批次 A:合并后的三个 action 入口 -------------------------------------
    # ⚠️ 形式统一:分发参数一律叫 action,handler 名字就是 action 名。
    #    goal 的 update 是**状态变更**(edit/pause/resume/complete/blocked),
    #    "建一个新目标"固定用 action=create —— 与 GoalStore.update 的动作名不冲突。
    async def schedule(params: dict[str, Any]) -> dict[str, Any]:
        """提醒:create / update / delete / list 四合一。

        ⚠️ 默认值刻意选 create:创建是最常用、也是唯一"给了内容就能成事"的动作;
           list 只给 action 就够;update / delete 必须有 id。
        """
        action = str(params.get("action") or "create")
        if action == "create":
            return await schedule_create(params)
        if action == "update":
            return await schedule_update(params)
        if action == "delete":
            return await schedule_delete(params)
        if action == "list":
            return await schedule_list(params)
        raise ValueError("action 只能是 " + "/".join(SCHEDULE_ACTIONS) + ",收到的是 " + action)

    #: GoalStore.update 认的动作名 —— 与分发的 action 同名但含义不同,所以单独一份。
    GOAL_STATES = ("edit", "pause", "resume", "complete", "blocked")

    async def goal(params: dict[str, Any]) -> dict[str, Any]:
        """目标:create(新建)/ update(改状态)。

        ⚠️ 两层 action:分发的 action 取 create/update,状态变更走 status。
           兼容:老的写法(把状态直接塞在 action 里)照样认 —— 旧 transcript 里
           可能就是 action=complete。
        """
        raw = str(params.get("action") or "").strip()
        if raw in GOAL_STATES:
            forwarded = dict(params)
            forwarded["status"] = raw
            forwarded["action"] = "update"
            return await goal_update(forwarded)
        action = raw or "update"
        if action == "create":
            return await goal_create(params)
        if action == "update":
            forwarded = dict(params)
            forwarded["status"] = params.get("status") or raw or "edit"
            return await goal_update(forwarded)
        raise ValueError("action 只能是 " + "/".join(GOAL_ACTIONS) + ",收到的是 " + action)

    async def skill(params: dict[str, Any]) -> dict[str, Any]:
        """技能:list(看有哪些)/ load(读正文)。"""
        action = str(params.get("action") or "")
        if not action:
            action = "load" if params.get("name") else "list"
        if action == "list":
            return await skill_list(params)
        if action == "load":
            return await skill_load(params)
        raise ValueError("action 只能是 " + "/".join(SKILL_ACTIONS) + ",收到的是 " + action)

    async def goal_create(params: dict[str, Any]) -> dict[str, Any]:
        objective = str(params.get("objective") or "").strip()
        goal = goals.create(objective, max_rounds=params.get("max_rounds"))
        return {"id": goal.id, "objective": goal.objective, "phase": goal.phase,
                "revision": goal.revision, "max_rounds": goal.max_rounds}

    async def goal_get(params: dict[str, Any]) -> dict[str, Any]:
        goal_id = str(params.get("goal_id") or "")
        if goal_id:
            goal = goals.get(goal_id)
            if goal is None:
                raise GoalError("目标不存在: " + goal_id)
            return {"goal": {"id": goal.id, "objective": goal.objective, "phase": goal.phase,
                             "revision": goal.revision, "rounds_started": goal.rounds_started,
                             "max_rounds": goal.max_rounds,
                             "blocked_reason": goal.blocked_reason}}
        items = goals.list(open_only=bool(params.get("open_only", True)))
        all_goals = goals.list(open_only=False, limit=200)
        done = sum(1 for g in all_goals if g.phase == "complete")
        return {
            "count": len(items),
            "goals": [{"id": g.id, "objective": g.objective, "phase": g.phase,
                       "revision": g.revision, "rounds_started": g.rounds_started,
                       "max_rounds": g.max_rounds} for g in items],
            # ⭐ 覆盖域:open_only 默认只看进行中的,"一共有几个目标"得另算。
            "coverage": {
                "rows": len(all_goals),
                "open": sum(1 for g in all_goals if g.is_open),
                "complete": done,
                "shown": len(items) if not goal_id else 1,
                "as_of": config.get_settings().now().isoformat(timespec="seconds"),
            },
        }

    def _user_asked_resume() -> bool:
        """这一轮用户原话里有没有“继续/恢复”这类明确指令。

        ⚠️ 不再信任模型自填的 `source="user"`(2026-10-18 审计):它只是一段
        模型输出,模型为“帮用户恢复”可以随手填。门禁要看的**事实**是用户刚说了什么。
        """
        if memory is None or not session_id:
            return False
        try:
            events = memory.events(session_id)
        except Exception:  # noqa: BLE001 - 读不到就当没说
            return False
        for event in reversed(events):
            if getattr(event, "type", "") != "user/message":
                continue
            text = str((event.data or {}).get("text") or "")
            return any(cue in text for cue in
                       ("继续", "恢复", "接着", "重新开始", "重启", "启动", "resume", "continue"))
        return False

    async def goal_update(params: dict[str, Any]) -> dict[str, Any]:
        # ⚠️ 状态从 status 读(action 现在是**分发**参数,不再是状态名);
        #    没给 status 时才回退到 action —— 那是给旧 transcript 留的兼容口子。
        state = str(params.get("status") or params.get("action") or "")
        source = str(params.get("source") or SOURCE_AGENT)
        if state == "resume":
            # 模型说“用户要求的”不算数,要看这一轮用户原话。
            source = SOURCE_USER if (source == SOURCE_USER and _user_asked_resume()) else SOURCE_AGENT
        goal = goals.update(
            str(params.get("goal_id") or ""),
            state,
            expected_revision=params.get("revision"),
            objective=params.get("objective"),
            blocked_reason=params.get("blocked_reason"),
            source=source,
        )
        return {"id": goal.id, "objective": goal.objective, "phase": goal.phase,
                "revision": goal.revision, "blocked_reason": goal.blocked_reason}

    async def skill_list(params: dict[str, Any]) -> dict[str, Any]:
        metas = library.scan()
        return {"count": len(metas), "skills": [meta.as_dict() for meta in metas]}

    async def skill_load(params: dict[str, Any]) -> dict[str, Any]:
        skill = library.load(str(params.get("name") or ""))
        payload: dict[str, Any] = {
            "name": skill.meta.name, "description": skill.meta.description, "body": skill.body,
        }
        if skill.resources:
            # ⚠️ 只给路径,不给内容:让模型用 file(action=read) 按需去读(渐进披露)。
            payload["resources"] = skill.resources
            payload["resources_hint"] = (
                "这个技能还带着文件(脚本/参考资料),需要时用 file(action=list) 看目录、"
                "file(action=read) 读具体文件;不要凭文件名猜内容。"
            )
        return payload

    return [
        _spec(
            "memory_note",
            "把一条需要长期保留的事实写进记忆(**用户**的偏好/身份/决定);写错了用 memory_correct 改。"
            "⚠️ 记的是**用户**的事;关于**你自己**的名字/英文名/称呼/性情用 persona_note(人格页),"
            "别把它们当用户的事实记下来。"
            "⚠️ 有正文、有日期、以后要回看的**感想/灵感/摘录**不是事实 —— 那是笔记(note_add),别塞进这里。"
            "⭐ 调用后系统会自动先问用户「要记住这条吗」—— 他会收到微信/网页上的确认;"
            "他确认了才真正写入。你不要自己再问一遍,也不要在结果返回前说已经记住。",
            # 设计深化 §3.5:写记忆是 risk=confirm —— 它每轮都进上下文,写错一条会持续污染。
            # 默认直写模式下这道门不拦(见 D65),但 LA_DIRECT_WRITE=0 时要能拦住。
            # ⚠️ 2026-10-22:它现在会在工具里**等用户确认**(最多 5 分钟),
            #    所以超时必须按"等用户"算,不能沿用 3 秒默认值。
            memory_note, required=("content",), risk=RISK_CONFIRM, timeout_ms=600000,
            params=_object(
                content=_string("要记住的事实,用一句完整的话写,例如 用户的英文名是 Kevin"),
                fact_type={"type": "string",
                           "enum": ["identity", "preference", "decision", "other"],
                           "description": "事实类别"},
                confidential={"type": "boolean",
                              "description": "是否属于敏感信息(标了之后不再进对话,只在记忆页可见)"},
            ),
        ),
        _spec(
            "memory_correct",
            "改一条已经记住的事实,或者让它失效(**没有删除**)。"
            "⭐ 用户说「记错了 / 不是这样 / 那个说法不对 / 忘掉这个」时用它:"
            "给 fact_id 与新的 content;只给 fact_id(不给 content)表示这件事不再算数。"
            "旧说法会被置为失效 —— 不再注入对话、也不再被检索命中 —— 但仍留在库里可查。"
            "**不要**用 memory_note 再写一条去盖住错的:那会越堆越脏。",
            # ⚠️ 同 memory_note:改口要等用户确认,超时按"等用户"算。
            memory_correct, required=("fact_id",), timeout_ms=600000,
            params=_object(
                fact_id={"type": "integer", "minimum": 1, "description": "要改的那条事实编号"},
                content=_string("新的说法(一句完整的话);留空 = 这件事不再算数"),
                fact_type={"type": "string", "enum": list(FACT_TYPES),
                           "description": "新的类别;不改就留空"},
            ),
        ),
        _spec(
            "memory_review",
            "看 / 放行 / 拒收**等人工复核**的记忆候选。"
            "⭐ 用户说「通过吧 / 放行 / 就这么记」,而上一轮有一条候选被判为待审时用它:"
            "action=approve(不给 candidate_id 就是最近那条);"
            "他说「不要 / 算了 / 别记」用 action=reject;不清楚有没有待审的,先 action=list。"
            "⚠️ 只有用户**明确表态**才 approve —— 不要替他放行,也不要自己编一个候选编号。",
            memory_review,
            params=_object(
                action={"type": "string", "enum": ["list", "approve", "reject"],
                        "description": "要做什么;默认 list"},
                candidate_id=_int(
                    "候选编号(先 list 拿到);approve / reject 不填 = 最近那条", maximum=1000000
                ),
                reason=_string("拒收理由(可选)"),
            ),
        ),
        _spec(
            "persona_note",
            "把一句关于**你自己**的话写进人格页(名字 / 英文名 / 称呼 / 性情 / 说话方式),"
            "下一轮对话就生效,人格页上直接看得到。"
            "⭐ 用户说「你的英文名叫 Mo」「以后你叫小夏」时用它。"
            "**不要**用 memory_note 记这种事:记忆页记的是**用户**的事,你自己是谁归人格页。"
            "调用后系统会自动先问用户「写进人格吗」—— 他确认了才写,你不要自己再问一遍,"
            "也不要在结果返回前说已经改好。",
            persona_note, risk=RISK_CONFIRM, timeout_ms=600000,
            params=_object(
                text=_string("要写进人格的那一句,例如 英文名是 Mo"),
                name=_string("顺便改称呼(最多 12 字);不改就留空", maxLength=12),
            ),
        ),
        _spec(
            "memory_recall", "看长期记忆里的事实(给编号,不是全文)。",
            memory_recall,
            params=_object(
                query=_string("关键词;留空 = 列出最近生效的那批"),
                limit=_int("最多几条,默认 10", maximum=30),
            ),
        ),
        _spec(
            "todo_write",
            "待办:新增 / 改内容 / 改到期日 / 设状态 / 勾完成 / 重新打开 / 删除。"
            "⭐ 做完一件事就用 action=complete 勾掉(给 todo_id);"
            "用户改主意就用 action=update 改内容,或 action=delete 删掉 —— "
            "**不要**再记一条新的去盖住旧的。不给 action 就是 action=add。"
            "⭐ **有产出、做完了要勾掉的事都记这里** —— 用户说「明天中午更新云朵」"
            "这种话,记的是**待办**(content=更新云朵,due_at_local=明天中午),"
            "给了 due_at_local 就自带一次到点提醒;勾掉它,提醒跟着停。"
            "只有「到点说一声、没有可勾的产出」的事(吃饭、喝水)才用 schedule。",
            todo_write,
            params=_object(
                action={"type": "string", "enum": list(TODO_ACTIONS),
                        "description": "要做什么;默认 add"},
                todo_id=_string("待办 id(除 add 外都要;先 todo_list 拿)"),
                content=_string("待办内容:add 必填,update 填新内容"),
                due_date_local=_string("到期日 YYYY-MM-DD;action=set_due 时传空串 = 去掉日期"),
                due_at_local=_string(
                    "到期的**时刻** ISO8601 带时区(如 2026-10-14T12:00:00+08:00);"
                    "只给日期不给它 = 那天的事、不响;给了它 = 自动带一次到点提醒"
                ),
                remind_minutes_before=_int(
                    "提前多少分钟提醒:0(默认,到点)=到点响;30=提前半小时;-1=这次不要提醒",
                    minimum=-1,
                ),
                status={"type": "string", "enum": list(TODO_STATUSES),
                        "description": "action=set_status 时的目标状态"},
                source={"type": "string", "enum": ["agent", "user"],
                        "description": "这条是谁提的;默认 agent。**用户自己说「记个待办」时填 user**"},
            ),
        ),
        _spec(
            "todo_list", "列出待办;filter 取 pending 或 all。返回里带覆盖域(一共几条、几条没做完)。",
            todo_list,
            params=_object(filter={"type": "string", "enum": ["pending", "all"],
                                   "description": "pending 只看未完成,all 看全部"}),
        ),
        # ⭐ 批次 A 合并(2026-10-15):原 schedule_create / schedule_list /
        #    schedule_update / schedule_delete 四个合成一个(见 docs/工具集瘦身对照表.md)。
        #    四者门禁字段完全一致(risk=low / phase=any / writes=True / timeout 同值),
        #    所以合并**不放宽任何闸**;省下的是每个工具各自的说明与重复字段定义。
        # ⚠️ 说明里保留原来的行为纪律(尤其是"有产出的用 todo_write 而不是这里")——
        #    合并只许省结构,不许省叮嘱(那正是 BAD_PARAMS 的来源)。
        _spec(
            "schedule",
            "提醒(到点说一声,没有可勾的产出):create 新建 / update 改 / delete 删 / list 查看。"
            "⭐ 「某个时刻要做某件事」(有产出、做完要勾掉)请用 todo_write 带 due_at_local ——"
            "那是待办,提醒挂在它身上;这里建的提醒不会出现在待办里。"
            "update 改时刻/间隔/内容(**不要**删了重建,id 会变);delete 可撤销、编号不变。"
            "间隔最小 300 秒。",
            schedule,
            params=_object(
                action={"type": "string", "enum": list(SCHEDULE_ACTIONS),
                        "description": "默认 create;update 要 id(先 list 拿)"},
                prompt=_string("到点要提醒的事情(create 必填;update 时填新内容)"),
                kind={"type": "string", "enum": ["at", "every", "after"],
                      "description": "at=某个时刻一次;every=每隔一段时间"},
                at=_string("首次触发时刻,ISO8601(如 2026-10-12T20:00:00+08:00);kind=at 时必填"),
                every_seconds={"type": "integer", "minimum": 300,
                               "description": "重复间隔秒数,最小 300"},
                id=_string("提醒 id(update / delete 必填;先 action=list 拿到它)"),
                enabled_only={"type": "boolean",
                              "description": "action=list 时:只看还开着的(默认 true);问「一共定过多少提醒」时传 false"},
            ),
        ),
        # ⭐ 批次 A:id 与 goal 由 3 个合成 2 个(goal_get 单独留着 —— 它是只读工具,
        #    并进写入工具会击穿只读模式)。goal 的 update 分支仍按 GoalStore 的动作名走。
        _spec(
            "goal",
            "长期目标:create 新建(必须可判定完成);"
            "update 改状态(edit/pause/resume/complete/blocked,需带 revision;resume 只能用户直接要求)。",
            goal,
            params=_object(
                action={"type": "string", "enum": list(GOAL_ACTIONS),
                        "description": "create=新建;update=改状态(默认)"},
                goal_id=_string("目标 id(update 必填;先 goal_get 拿)"),
                objective=_string("action=create 时必填;action=update 且状态=edit 时填新目标内容"),
                max_rounds={"type": "integer", "minimum": 1,
                            "description": "轮次预算;耗尽会自动变为 blocked"},
                status={"type": "string",
                        "enum": ["edit", "pause", "resume", "complete", "blocked"],
                        "description": "action=update 时必填:要执行的状态变更;resume 只能由用户直接要求"},
                revision={"type": "integer", "description": "action=update 时读取到的 revision,用于并发校验"},
                blocked_reason=_string("状态=blocked 时的原因"),
                source={"type": "string", "enum": ["agent", "user"],
                        "description": "这次改动是谁发起的;默认 agent(resume 只能由用户直接要求)"},
            ),
        ),
        _spec(
            "goal_get", "读取目标状态;不传 goal_id 则列出全部。", goal_get,
            params=_object(
                goal_id=_string("目标 id;留空则列出全部"),
                open_only={"type": "boolean", "description": "是否只看进行中的目标"},
            ),
        ),
        # ⭐ 批次 A:skill_list / skill_load 合成一个(两个都是只读、超时同值)。
        _spec(
            "skill",
            "技能:list 看有哪些(只有名字与说明);load 加载一份技能的正文(name 必填)。",
            skill,
            params=_object(
                action={"type": "string", "enum": list(SKILL_ACTIONS),
                        "description": "list=看有哪些;load=读正文(给了 name 时默认 load)"},
                name=_string("技能名(load 必填;先用 action=list 查)"),
            ),
        ),
    ]


__all__ = ["build_task_tools", "SOURCE_USER", "RevisionConflict"]
