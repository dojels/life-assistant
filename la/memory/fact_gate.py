"""事实准入的**编排**:候选 -> 裁决 -> 落库 + 留痕。

这一段被三条调用路径共用(这正是它必须独立成模块的理由):
    ① 规则识别(`session._capture_notes`)—— 用户说"我不吃香菜"
    ② 模型的 memory_note 工具    —— 模型自己觉得该记
    ③ 页面上的人工动作           —— 停用 / 改口

准入流程(每一步都要留下"为什么"):
    guard.check_candidate   结构问题(空/残句/疑问/超长/未来动作)-> 直接拒,不花钱
    fact_adjudicator.arbitrate  语义关系(新/重复/细化/矛盾)-> 一次模型调用
    落库                     new/refines/contradicts 才写 facts;duplicate 只留痕

⚠️ 裁决用的模型调用**只在有候选时发生**,每轮最多 `config.FACT_ADJUDICATE_PER_TURN` 条
   —— 它不在回答链路上(在回复发送之后跑),失败也不影响这一轮回答。
⚠️ 任何一条候选都不会"因为裁决失败而消失":它要么 admitted,要么留在 pending/review。
   事实库宁可少一条,也不能悄悄多一条错的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .. import config
from . import guard
from .fact_adjudicator import (
    REL_CONTRADICTS,
    REL_DUPLICATE,
    REL_NEW,
    REL_REFINES,
    REL_REVIEW,
    arbitrate,
)

#: 候选的来路(写进 fact_candidates.source,排障时一眼看出是谁提的)
SOURCE_RULE = "rule"
SOURCE_MODEL = "memory_note"
SOURCE_HUMAN = "human"


@dataclass(frozen=True)
class FactProposal:
    """一次「提议」的结果:此刻只有候选编号,**还没有 fact_id**。

    ⚠️ 为什么单独一个类型(2026-10-18 审计修):propose() 返回的整数是
    fact_candidates.id,而工具层一直把它当 facts.id 回给模型 —— 两张表各自从 1
    自增,于是 memory_note 报的编号要么查不到,要么指向另一条无关事实。
    类型分开之后,"还没有事实编号"这件事在签名上就看得见,想混也混不了。
    """

    candidate_id: int
    status: str = "pending"          # pending | duplicate | admitted(同步落库时)
    supersedes: int | None = None
    #: 只有「同步落库」的实现(测试里的 add_fact 闭包、未来可能的同步闸)才非 0。
    fact_id: int = 0

    @property
    def queued(self) -> bool:
        return bool(self.candidate_id)


@dataclass
class FactAdmission:
    """一条候选的处置结果。"""

    candidate_id: int
    content: str
    status: str                  # admitted | duplicate | rejected | review | pending
    reason: str = ""
    fact_id: int | None = None
    target_fact_id: int | None = None
    #: 人工动作落的那条回执(只有 human_release 会填;撤销入口靠它)
    receipt_id: int = 0


@dataclass
class FactGateReport:
    """一次准入批处理的汇总 —— 直接给执行反馈/日志用。"""

    considered: int = 0
    admitted: int = 0
    duplicate: int = 0
    rejected: int = 0
    review: int = 0
    items: list[FactAdmission] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.admitted or self.duplicate or self.rejected or self.review)

    def summary(self) -> str:
        parts = []
        if self.admitted:
            parts.append("入 " + str(self.admitted))
        if self.duplicate:
            parts.append("并 " + str(self.duplicate))
        if self.rejected:
            parts.append("拒 " + str(self.rejected))
        if self.review:
            parts.append("待审 " + str(self.review))
        return "、".join(parts) if parts else "无变化"


def capacity_note(store, *, incoming: int = 1, session_id: str | None = None) -> str:
    """算给裁判看的容量说明 —— 它据此决定"合并"还是"新增"。"""
    overview = store.injection_overview(session_id=session_id)
    room = int(overview["max_active"]) - int(overview["total_active"])
    note = (
        "生效 " + str(overview["total_active"]) + " 条 / 上限 "
        + str(overview["max_active"]) + " 条"
    )
    if room <= 0:
        note += "(已满:新增会让最旧的一条挤不出去上下文,请优先判 duplicate 或 refines)"
    elif room < incoming:
        note += "(快满,只剩 " + str(room) + " 个位置)"
    return note


def log_event(
    store, session_id: str, event: str, payload: dict[str, Any], *, via: str = "fact_gate"
) -> None:
    """准入轨迹写进归档(与 memory/note 同一口径,可回放)。

    ⚠️ 事件是**追加**的,不改老的 memory/note —— 归档只增不改。
    """
    try:
        store.append(
            session_id,
            "memory/note",
            {"content": event, "fact_type": "other", "explicit": False,
             "via": via, **payload},
        )
    except Exception:  # noqa: BLE001 - 留痕失败不该挡住准入本身
        pass


class FactGate:
    """候选准入闸。一个会话一个实例,复用会话自己的模型客户端。"""

    def __init__(self, store, *, client: Any = None, session_id: str = "") -> None:
        self.store = store
        self.client = client
        self.session_id = session_id

    # -- ① 提议 -----------------------------------------------------------
    def propose(
        self,
        content: str,
        *,
        source: str = SOURCE_RULE,
        fact_type: str = "other",
        keywords: list[str] | None = None,
        confidential: bool = False,
        supersedes: int | None = None,
    ) -> int:
        """把一条候选放进队列,返回候选编号(0 = 同一句已经在队列里)。

        ⚠️ 返回值是 **fact_candidates.id**,不是 facts.id。要把它交给模型/回执,
        必须用 propose_candidate() 拿 FactProposal —— 别再把两个编号混着用。
        """
        return self.propose_candidate(
            content, source=source, fact_type=fact_type, keywords=keywords,
            confidential=confidential, supersedes=supersedes,
        ).candidate_id

    def propose_candidate(
        self,
        content: str,
        *,
        source: str = SOURCE_RULE,
        fact_type: str = "other",
        keywords: list[str] | None = None,
        confidential: bool = False,
        supersedes: int | None = None,
    ) -> FactProposal:
        """同 propose(),但返回带类型的提案(候选编号 + 状态 + 要取代谁)。"""
        candidate_id = self.store.add_candidate(
            content, source=source, session_id=self.session_id or None,
            fact_type=fact_type, keywords=keywords or (), confidential=confidential,
            supersedes=supersedes,
        )
        return FactProposal(
            candidate_id=candidate_id,
            status="pending" if candidate_id else "duplicate",
            supersedes=supersedes,
        )

    # -- ② 裁决 + 落库 -----------------------------------------------------
    async def process_pending(self, *, limit: int | None = None) -> FactGateReport:
        """处理队列里的候选。**每轮调用一次**,在回复发送之后。"""
        report = FactGateReport()
        cap = int(limit or config.FACT_ADJUDICATE_PER_TURN)
        # ⚠️ 只处理本会话的候选(2026-10-18 审计):旧实现取全库队列,
        #    却用当前会话的事实裁决;A 可能改写 B 的 fact,或替 B 占用每分钟 6 个名额。
        pending = self.store.pending_candidates(limit=cap, session_id=self.session_id or None)
        if not pending:
            return report

        for row in pending:
            report.considered += 1
            admission = await self.adjudicate_row(row)
            report.items.append(admission)
            if admission.status == "admitted":
                report.admitted += 1
            elif admission.status == "duplicate":
                report.duplicate += 1
            elif admission.status == "rejected":
                report.rejected += 1
            elif admission.status == "review":
                report.review += 1
        return report

    async def adjudicate_row(self, row) -> FactAdmission:
        """一条候选的完整准入:确定性检查 -> 语义裁决 -> 落库。"""
        candidate_id = int(row["id"])
        content = str(row["content"])
        session_id = str(row["session_id"] or self.session_id or "")
        # 这条候选是「改口」来的吗?改口要等**新说法真的入库**才让旧事实失效。
        supersedes = (
            int(row["supersedes"])
            if "supersedes" in row.keys() and row["supersedes"] is not None
            else None
        )

        # --- 第一道:确定性检查(不花钱,判死结构垃圾) ---
        check = guard.check_candidate(content)
        if check.rejected:
            self.store.set_candidate_status(
                candidate_id, "rejected", reason=check.reason,
                verdict={"guard": check.code},
            )
            log_event(self.store, session_id, "记忆候选被拒:" + content[:60],
                      {"candidate_id": candidate_id, "code": check.code,
                       "reason": check.reason})
            return FactAdmission(candidate_id, content, "rejected", check.reason)
        if check.needs_review:
            self.store.set_candidate_status(
                candidate_id, "review", reason=check.reason,
                verdict={"guard": check.code},
            )
            log_event(self.store, session_id, "记忆候选待审:" + content[:60],
                      {"candidate_id": candidate_id, "code": check.code,
                       "reason": check.reason})
            return FactAdmission(candidate_id, content, "review", check.reason)

        # --- 第二道:语义裁决(一次模型调用) ---
        existing = self.store.active_facts(
            limit=config.FACT_MAX_ACTIVE, session_id=self.session_id or None
        )
        verdict = await arbitrate(
            self.client, content, existing,
            capacity_note=capacity_note(self.store, session_id=self.session_id or None),
        )
        payload = {
            "relationship": verdict.relationship,
            "target_id": verdict.target_id,
            "text": verdict.text,
            "reason": verdict.reason,
        }

        if verdict.relationship == REL_REVIEW or not verdict.ok:
            reason = verdict.reason or "裁判判为待审"
            self.store.set_candidate_status(
                candidate_id, "review", reason=reason, verdict=payload,
                target_fact_id=verdict.target_id,
            )
            log_event(self.store, session_id, "记忆候选待审:" + content[:60],
                      {"candidate_id": candidate_id, "code": "review", "reason": reason})
            return FactAdmission(
                candidate_id, content, "review", reason, target_fact_id=verdict.target_id,
            )

        if verdict.relationship == REL_DUPLICATE and verdict.target_id:
            if supersedes and int(verdict.target_id) != supersedes:
                # 改口的内容与库里另一条是同一条 -> 旧事实指向它,而不是凭空多一行
                self.store.supersede(supersedes, int(verdict.target_id))
            self.store.set_candidate_status(
                candidate_id, "duplicate",
                reason="与 #" + str(verdict.target_id) + " 是同一条,不重复入库",
                verdict=payload, target_fact_id=verdict.target_id,
            )
            log_event(self.store, session_id,
                      "记忆候选合并:" + content[:60] + " -> #" + str(verdict.target_id),
                      {"candidate_id": candidate_id, "code": "duplicate",
                       "target_fact_id": verdict.target_id})
            return FactAdmission(
                candidate_id, content, "duplicate",
                "与 #" + str(verdict.target_id) + " 是同一条",
                target_fact_id=verdict.target_id,
            )

        # --- new / refines / contradicts:这才写 facts ---
        fact_type = str(row["fact_type"] or "other")
        keywords = self._keywords(row)
        confidential = bool(row["confidential"])
        text = verdict.text or content
        if len(text) > config.FACT_MAX_CONTENT_CHARS:
            text = text[: config.FACT_MAX_CONTENT_CHARS]
        if verdict.relationship == REL_REFINES and verdict.target_id:
            # 细化:更新原行的内容(不是新增),变更有据可查
            self.store.update_fact_content(
                int(verdict.target_id), text, keywords=keywords,
            )
            fact_id = int(verdict.target_id)
        else:
            fact_id = self.store.add_fact(
                text, session_id=session_id or None, fact_type=fact_type,
                keywords=keywords, confidential=confidential,
                supersedes=int(verdict.target_id) if verdict.relationship == REL_CONTRADICTS
                and verdict.target_id else None,
            )
        self.store.set_candidate_status(
            candidate_id, "admitted",
            reason=verdict.relationship
            + ((" -> #" + str(fact_id)) if fact_id else ""),
            verdict=payload, target_fact_id=verdict.target_id, fact_id=fact_id,
        )
        # ⭐ 改口到这里才算完成:新说法真的进了 facts,旧的那条才失效并指回来。
        #    (以前是工具层"先停用、再排队",候选被拒时旧记忆就没了。)
        if supersedes and supersedes != fact_id:
            self.store.supersede(supersedes, fact_id)
        log_event(self.store, session_id,
                  "记忆入库:" + text[:60],
                  {"candidate_id": candidate_id, "code": verdict.relationship,
                   "fact_id": fact_id, "target_fact_id": verdict.target_id})
        return FactAdmission(
            candidate_id, content, "admitted", verdict.relationship,
            fact_id=fact_id, target_fact_id=verdict.target_id,
        )

    def review_queue(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """等人放行的候选 —— 给记忆页与对话工具(memory_review)同一份口径。

        ⚠️ 只给本会话的(review_candidates 的 session_id 过滤):别的成员的候选
           不该出现在这个人的对话里,也不该被这个人的一句话放行。
        """
        rows = self.store.review_candidates(
            limit=limit, session_id=self.session_id or None,
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            items.append({
                "candidate_id": int(row["id"]),
                "content": str(row["content"]),
                "reason": str(row["reason"] or "")[:200],
                "source": str(row["source"] or ""),
                "confidential": bool(row["confidential"]),
                "created_at": str(row["created_at"] or "")[:19],
            })
        return items

    def human_release(
        self,
        candidate_id: int,
        *,
        approve: bool,
        reason: str = "",
        via: str = "human",
    ) -> FactAdmission:
        """人工处理一条**待放行**的候选:放行进事实库,或者拒收。

        ⭐ 为什么必须有这个入口(2026-10-25,用户原话):
             "看这段对话,用户在微信端,无法让智能体写入事实。
              图2是网页端的显示,用户也没有审核放行的按钮。"
           在此之前 review 只是一张只读表格里的一行字:微信里说"通过吧"没有任何
           动作,网页上也点不了 —— 候选就永远停在那里。

        ⚠️ 放行**不是**重跑裁判,也不会静默合并/改写别的事实:
           人拍板的是"这条可以入库",所以这里只做三件事 ——
             ① 同一条(归一化后)已经在 facts 里 -> 记 duplicate,不重复写;
             ② 否则按候选原文新增一条;候选带着 supersedes(改口)时旧的那条置失效;
             ③ 候选状态、判决原文、事件三处都留痕,回执里带新事实号(可撤销)。
           "是不是更准的一条、要不要合并进 #N"仍然只能由人自己在 L3 上改口 ——
           那是不可逆的语义动作,不该由一次"放行"顺手替人做掉。
        """
        row = self.store.get_candidate(int(candidate_id))
        if row is None:
            return FactAdmission(int(candidate_id), "", "stale", "没有这条候选")
        content = str(row["content"])
        status = str(row["status"])
        if status in ("admitted", "duplicate", "rejected"):
            return FactAdmission(
                int(candidate_id), content, "stale",
                "这条候选已经处理过了(当前状态:" + status + "),不在等放行",
            )
        # 原判决要保留下来(排障时"它当初被裁成什么"比"人怎么改的"更早),只补一个 human 字段
        payload: dict[str, Any] = {}
        try:
            loaded = json.loads(row["verdict"] or "{}")
            if isinstance(loaded, dict):
                payload = loaded
        except (json.JSONDecodeError, TypeError):
            payload = {}
        payload["human"] = "approve" if approve else "reject"
        if reason:
            payload["human_reason"] = reason[:200]

        if not approve:
            note = reason or "人工拒收"
            self.store.set_candidate_status(
                int(candidate_id), "rejected", reason=note, verdict=payload,
            )
            log_event(
                self.store, str(row["session_id"] or self.session_id),
                "记忆候选被人工拒收:" + content[:60],
                {"candidate_id": int(candidate_id), "code": "human_reject",
                 "reason": note}, via=via,
            )
            return FactAdmission(int(candidate_id), content, "rejected", note)

        session_id = str(row["session_id"] or self.session_id or "") or None
        # --- 放行:先看同一句是不是已经在**这个会话**的库里 ---
        #     (字面完全一致才判重,不做语义猜测;不跨会话判 —— 别的成员的同一句话
        #      不该让这个人少一条属于他自己的事实)
        from .guard import normalize

        mine = normalize(content)
        for fact in self.store.active_facts(limit=200, session_id=session_id):
            if normalize(fact.content) == mine:
                note = "与 #" + str(fact.id) + " 是同一条,不重复入库"
                self.store.set_candidate_status(
                    int(candidate_id), "duplicate", reason=note, verdict=payload,
                    target_fact_id=int(fact.id),
                )
                log_event(
                    self.store, str(row["session_id"] or self.session_id),
                    "人工放行时判定重复:" + content[:60] + " -> #" + str(fact.id),
                    {"candidate_id": int(candidate_id), "code": "human_duplicate",
                     "target_fact_id": int(fact.id)}, via=via,
                )
                return FactAdmission(
                    int(candidate_id), content, "duplicate", note,
                    target_fact_id=int(fact.id),
                )

        supersedes = (
            int(row["supersedes"])
            if "supersedes" in row.keys() and row["supersedes"] is not None
            else None
        )
        # ⚠️ 候选自己写着要取代谁,就在这里一起做:改口要等新说法**真的入库**,
        #    而放行就是"真的入库"的那一刻。
        fact_id = self.store.add_fact(
            content,
            session_id=session_id,
            fact_type=str(row["fact_type"] or "other"),
            keywords=self._keywords(row),
            confidential=bool(row["confidential"]),
            supersedes=supersedes,
        )
        note = "人工放行" + (" -> #" + str(fact_id) if fact_id else "")
        if supersedes:
            note += "(取代 #" + str(supersedes) + ")"
        self.store.set_candidate_status(
            int(candidate_id), "admitted", reason=note, verdict=payload,
            target_fact_id=supersedes, fact_id=fact_id,
        )
        log_event(
            self.store, str(row["session_id"] or self.session_id),
            "记忆候选被人工放行:" + content[:60],
            {"candidate_id": int(candidate_id), "code": "human_release",
             "fact_id": fact_id, "target_fact_id": supersedes}, via=via,
        )
        receipt_id = self._release_receipt(
            candidate_id=int(candidate_id), fact_id=fact_id, content=content,
            supersedes=supersedes, via=via,
        )
        return FactAdmission(
            int(candidate_id), content, "admitted", note,
            fact_id=fact_id, target_fact_id=supersedes, receipt_id=receipt_id,
        )

    def _release_receipt(
        self, *, candidate_id: int, fact_id: int, content: str,
        supersedes: int | None, via: str,
    ) -> int:
        """放行也是一次**写入**,必须有回执(撤销 = 那条新事实置失效)。

        ⚠️ 回执与它记录的那次写入同库(memory.db,见 migrations.py 的说明),
           所以这里用 store.conn 建一本,而不是借用 Web 层那本领域回执。
        """
        from ..domain.receipts import DOMAIN_MEMORY, ReceiptLog

        try:
            log = ReceiptLog(self.store.conn)
            if not log._table_ready():
                return 0
            log.session_id = self.session_id or None
            plan: dict[str, Any] = {
                "type": "fact", "candidate_id": int(candidate_id),
                "new_fact_id": 0,
            }
            if supersedes:
                # 改口式放行:撤销 = 旧说法恢复生效 + 新写的那条置失效(与记忆页改口同一条路)
                plan.update({"op": "supersede", "fact_id": int(supersedes),
                             "new_fact_id": int(fact_id)})
            else:
                plan.update({"op": "release", "fact_id": int(fact_id)})
            receipt_id = log.record(
                kind="memory.fact", domain=DOMAIN_MEMORY,
                label="放行记忆候选 #" + str(candidate_id) + ":" + content[:24],
                detail={"candidate_id": int(candidate_id), "fact_id": int(fact_id),
                        "content": content[:200], "op": "release", "via": via},
                undo_plan=plan,
                actor="user" if via in ("memory_page", "chat") else "agent",
                session_id=self.session_id or None,
            )
            if receipt_id:
                log.conn.commit()
            return receipt_id
        except Exception:  # noqa: BLE001 - 记不上回执不该让放行失败
            return 0

    async def human_correct(self, fact_id: int, new_content: str) -> FactAdmission:
        """**人工**在记忆页上改一条事实 —— 走同一条准入闸,不是改库后门。

        ⚠️ 为什么人工改也要过闸:上次"规则直接写库"造成的问题,靠"人工直接写库"
           是修不掉的 —— 人也会写重复的说法。所以人工只享受两件事:
            ① 由人发起(来路记成 human,审计查得到);
            ② 判完之后**当场生效**(不像对话里那样等到回复之后)。
        """
        content = str(new_content or "").strip()
        check = guard.check_candidate(content)
        if check.rejected:
            log_event(self.store, self.session_id, "人工改记忆被拒:" + content[:60],
                      {"candidate_id": 0, "code": check.code, "reason": check.reason})
            return FactAdmission(0, content, "rejected", check.reason)

        existing = self.store.active_facts(
            limit=config.FACT_MAX_ACTIVE, session_id=self.session_id or None
        )
        verdict = await arbitrate(
            self.client, content, existing,
            capacity_note=capacity_note(self.store, session_id=self.session_id or None),
        )
        if not verdict.ok:
            return FactAdmission(0, content, "review", verdict.reason or "裁判判为待审")

        target = int(verdict.target_id) if verdict.target_id else None
        if target is not None and target != int(fact_id):
            # 改完之后会和**另一条**重复/冲突 —— 那种情况下让人先看清楚再决定,
            # 而不是替他合并掉一条(合并是不可逆的语义动作)。
            reason = (
                "改完之后会和 #" + str(target) + " 重复/冲突:"
                + verdict.relationship + "。请先处理那一条。"
            )
            log_event(self.store, self.session_id, "人工改记忆被推迟:" + content[:60],
                      {"candidate_id": 0, "code": "conflict_with_other",
                       "target_fact_id": target, "reason": reason})
            return FactAdmission(0, content, "review", reason, target_fact_id=target)

        if self.store.update_fact_content(
            int(fact_id), content, keywords=self.store.get_fact(fact_id).keywords
            if self.store.get_fact(fact_id) else (),
        ):
            log_event(self.store, self.session_id, "人工改记忆:#" + str(fact_id) + " -> " + content[:60],
                      {"candidate_id": 0, "code": "human_correct", "fact_id": int(fact_id)})
            return FactAdmission(0, content, "admitted", "人工改口", fact_id=int(fact_id))
        return FactAdmission(0, content, "rejected", "没有这条事实")

    @staticmethod
    def _keywords(row) -> list[str]:
        try:
            value = json.loads(row["keywords"] or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return [str(item) for item in value] if isinstance(value, list) else []


__all__ = [
    "FactAdmission",
    "FactGate",
    "FactGateReport",
    "SOURCE_HUMAN",
    "SOURCE_MODEL",
    "SOURCE_RULE",
    "capacity_note",
    "log_event",
]
