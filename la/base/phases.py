"""三阶段门禁与熔断矩阵 —— 全部在代码层,不是提示词。

⚠️ 2026-10-17 从 la/agent/ 搬到 la/base/:**它被 domain / base / ops 反过来依赖**,
   住在 agent 里会让这些层"向上"引用 agent(4 条包级循环依赖之一)。
   它本身只依赖 config,是典型的叶子。

状态机(设计深化 §2.1):

    IDLE --用户消息--> RUNNING{phase=QA}
      QA 命中且达标        --> ADJUDICATE
      QA 预算(2)耗尽或无望 --> ARCHAEOLOGY
      ARCH 预算(3)耗尽或证据充分 --> ADJUDICATE
      ADJUDICATE:拒绝一切工具调用,只读 Scratchpad --> 回复 --> IDLE

熔断矩阵(每一条都是代码):

| 规则 | 值 | 拒绝码 |
| 总步数 | <=6 | TOTAL_STEPS_EXCEEDED |
| QA 调用 | <=2 | PHASE_BUDGET_EXCEEDED |
| ARCH 调用 | <=3 | PHASE_BUDGET_EXCEEDED |
| ADJUDICATE | 0 工具 | PHASE_FORBIDS_TOOLS |
| ANSWER 工具调用(读+写共用一池) | <=40(env 可调) | PHASE_BUDGET_EXCEEDED |
| ANSWER 模型往返 | <=30(env 可调) | 循环退出,收回工具强制作答 |
| TASK 类目(非回答阶段) | <=8 | PHASE_BUDGET_EXCEEDED |
| 重复调用(QA/ARCH,代码发起) | 第 2 次拒绝 | DUPLICATE_CALL |
| 重复调用(ANSWER,模型发起) | **只提醒,不拦** | — |
| 子代理 | <=4 个 / 每个 <=500 token / 深度 <=2 | SUBAGENT_LIMIT_EXCEEDED |
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# 额度来自 config(可用 LA_AGENT_MAX_ITERS / LA_AGENT_MAX_TOOL_CALLS 覆盖,对齐云朵的 .env 口径)。
from ..config import AGENT_MAX_ITERS, AGENT_MAX_TOOL_CALLS

PHASE_QA = "qa"
PHASE_ARCH = "archaeology"
PHASE_ADJUDICATE = "adjudicate"
# 回答阶段:自动检索跑完之后,模型在这一阶段可以**主动**调工具(见 docs/决策记录.md D25)。
# 它独立于自动机,配额也独立 —— 否则模型一调工具就会撞上 ADJUDICATE 的"零工具"铁律。
PHASE_ANSWER = "answer"

PHASE_ORDER = (PHASE_QA, PHASE_ARCH, PHASE_ADJUDICATE, PHASE_ANSWER)

LIMIT_TOTAL_STEPS = 6
LIMIT_QA_CALLS = 2
LIMIT_ARCH_CALLS = 3
LIMIT_ADJUDICATE_TOOLS = 0
# 回答阶段的工具调用上限:读+写**共用一池**(D101)。
#
# ⚠️ 沿革,别改回去:
#   M9 从 4 提到 6(分类决策链放不下);
#   D99(2026-10-12)把"写入/交付"拆出来单列 3 格 —— 真机:2 次搜索 + 4 次抓网页
#     正好用满 6 格,紧接着的 artifact_write 被挡下,模型只能回"你再发一条我就写";
#   M9 把它从 4 提到 6,原因是账本的"分类决策链"放不下
#     (expense_record → category_create(弹确认框) → expense_record);
#   D101(2026-10-13)照**云朵辅助培训智能体**的实测口径改回**单池**,额度放大一个量级。
#
# 云朵(用户另一个已上线的项目)的口径:.env 里 AGENT_MAX_ITERS=30 / AGENT_MAX_TOOL_CALLS=40,
#   到顶**撤掉工具、按现有结果成文**,原因写进用户可见的步骤账本;它的文档里记着这次上调的理由
#   ——"2026-09-27 由 15/18 上调(用户定调:重任务在旧预算下不够用)"。
#   同一个坑:额度太小 → 重任务被半路掐断。所以这里**直接对齐**,不再自己调参。
#
# 为什么分账退役:分账治的是"研究把交付挤没"这个**症状**,单池 40 治的是**病根**(池子太小)。
#   ⚠️ 残余风险仍在:理论上仍可能"查了几十次还没动手写"。真要再收紧,
#   优先做"到顶时保留交付类工具"的收尾通道,而不是回到分账(见 docs/决策记录.md D101)。
LIMIT_ANSWER_TOOL_CALLS = AGENT_MAX_TOOL_CALLS
#: 回答阶段最多几轮模型往返(＝云朵的 AGENT_MAX_ITERS)。
#: 一轮里模型可以并行发多个调用,所以轮数与额度分开计。
LIMIT_ANSWER_ROUNDS = AGENT_MAX_ITERS
#: QA/ARCH/TASK 的重复调用:允许 1 次,第 2 次同参即拒(代码发起的调用,重复=代码有病)。
#: ⚠️ 回答阶段**不用**这个数 —— 那里只提醒不拦(模型发起的调用,同参重试是正当的)。
LIMIT_DUPLICATE_CALLS = 1
LIMIT_SUBAGENTS = 4
LIMIT_SUBAGENT_TOKENS = 500
LIMIT_SUBAGENT_DEPTH = 2

# 工具分两类,配额互不侵占:
#   research(研究类)—— 检索、考古、子代理,受阶段预算与总步数约束;
#   task(事项类)   —— 记待办、建提醒、读技能,不该吃掉研究的步数预算(D21)。
#
# ⚠️ 现实提醒(2026-10-12 核实):M4.0 起模型自己发起的调用**全部**落在回答阶段,
#    而 admit() 里"回答阶段"那个分支排在类别判断**之前** —— 也就是说
#    模型在回答阶段调 task 类工具时,走的是回答阶段那一池(读/写共用),不碰这 8 格。
#    这 8 格因此只在 QA/ARCH 阶段生效,而那两个阶段是代码自动跑的(只调检索类),
#    实际是一次**安全网**而不是日常约束。真要动它,先看 D21 与 D99。
CATEGORY_RESEARCH = "research"
CATEGORY_TASK = "task"
LIMIT_TASK_CALLS = 8

CODE_TOTAL_STEPS_EXCEEDED = "TOTAL_STEPS_EXCEEDED"
CODE_PHASE_BUDGET_EXCEEDED = "PHASE_BUDGET_EXCEEDED"
CODE_PHASE_FORBIDS_TOOLS = "PHASE_FORBIDS_TOOLS"
CODE_DUPLICATE_CALL = "DUPLICATE_CALL"
CODE_SUBAGENT_LIMIT_EXCEEDED = "SUBAGENT_LIMIT_EXCEEDED"
CODE_OK = "OK"


def canonical_params(params: dict[str, Any] | None) -> str:
    """参数的规范形式:键排序 + 紧凑分隔,用于"同工具同参数"判定。"""
    return json.dumps(params or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Decision:
    allowed: bool
    code: str = CODE_OK
    detail: str = ""
    #: 放行但要说给模型听的一句话(回答阶段的"重复调用提醒"走这里)。
    #: 与 detail 的区别:**detail 是拒绝理由,note 是随放行一起送达的提醒** ——
    #: 两者混用会让模型把"提醒"当成"被拒"而放弃这一步(云朵/DSH 都只提醒不拦)。
    note: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class CircuitBreaker:
    """每一步工具调用都要先过这里。超限就强制进下一阶段,而不是抛异常。"""

    phase: str = PHASE_QA
    total_steps: int = 0
    calls_by_phase: dict[str, int] = field(default_factory=dict)
    task_calls: int = 0
    #: 回答阶段的工具调用总数 —— 读+写**共用**这一格(D101)。
    answer_tool_calls: int = 0
    #: 其中写入/交付类的次数。**只统计,不再单独设闸**(分账已退役,见 LIMIT_ANSWER_TOOL_CALLS)。
    answer_write_calls: int = 0
    seen: dict[str, int] = field(default_factory=dict)
    subagents_spawned: int = 0
    forced_transitions: list[str] = field(default_factory=list)

    # -- 配额 -------------------------------------------------------------
    def budget_for(self, phase: str) -> int:
        if phase == PHASE_QA:
            return LIMIT_QA_CALLS
        if phase == PHASE_ARCH:
            return LIMIT_ARCH_CALLS
        return LIMIT_ADJUDICATE_TOOLS

    def admit(
        self,
        tool: str,
        params: dict[str, Any] | None = None,
        *,
        phase: str | None = None,
        category: str = CATEGORY_RESEARCH,
        writes: bool = False,
    ) -> Decision:
        """检查并占用一次调用配额。允许时立即计数(调用方无需再 record)。

        `writes`:这个工具会不会改数据(来自 ToolSpec.writes)。回答阶段读/写**共用一池**,
        它只用来**统计**写入次数;QA/ARCH/TASK 那几个分支按 category 分账(见 D21)。
        """
        current = phase or self.phase

        if current == PHASE_ADJUDICATE:
            return Decision(False, CODE_PHASE_FORBIDS_TOOLS, "裁决阶段只读 Scratchpad,不允许调用工具")

        if current == PHASE_ANSWER:
            # 回答阶段:模型自己决定调什么,配额独立,也不计入考古总步数。
            # 读/写**共用一池**(D101,对齐云朵);到顶由调用方收回工具、强制作答。
            if self.answer_tool_calls >= LIMIT_ANSWER_TOOL_CALLS:
                # ⚠️ 这句话是**给模型看的行动指令**,不是给用户的道歉:
                #    云朵到顶的文案就是"已停止调用工具,按现有结果收尾"。
                #    别写"你再发一条消息" —— 真机模型照抄了它,把交付推回给用户(D99 的教训)。
                return Decision(
                    False, CODE_PHASE_BUDGET_EXCEEDED,
                    "本轮工具调用已达上限 " + str(LIMIT_ANSWER_TOOL_CALLS)
                    + "(读/写共用);现在按已经拿到的结果把话说完、把该交的交付掉",
                )
            key = PHASE_ANSWER + "|" + tool + "|" + canonical_params(params)
            count = self.seen.get(key, 0)
            # ⚠️ 重复调用在回答阶段**只提醒不拦**(云朵与 DSH 的定位:提醒是建议,绝不阻止调用)。
            #    理由:模型自己发起的调用,瞬时失败后用同样参数重试是**正当**的;
            #    拦死它会把"重试一次就好"变成"这轮做不完"。防打转交给上面那格总池。
            note = ""
            if count >= LIMIT_DUPLICATE_CALLS:
                note = (
                    "你已经用完全相同的参数调用过 " + tool + " " + str(count)
                    + " 次;没有新信息就别再重复,换参数、换工具或直接作答"
                )
            self.seen[key] = count + 1
            self.answer_tool_calls += 1
            if writes:
                self.answer_write_calls += 1
            return Decision(
                True, CODE_OK, "回答阶段调用 " + str(self.answer_tool_calls), note=note
            )

        if category == CATEGORY_TASK:
            if self.task_calls >= LIMIT_TASK_CALLS:
                return Decision(
                    False, CODE_PHASE_BUDGET_EXCEEDED,
                    "事项类工具调用已达上限 " + str(LIMIT_TASK_CALLS),
                )
            key = CATEGORY_TASK + "|" + tool + "|" + canonical_params(params)
            count = self.seen.get(key, 0)
            if count >= LIMIT_DUPLICATE_CALLS:
                return Decision(False, CODE_DUPLICATE_CALL, "重复调用: " + tool + " 参数完全相同")
            self.seen[key] = count + 1
            self.task_calls += 1
            return Decision(True, CODE_OK, "事项调用 " + str(self.task_calls))

        if self.total_steps >= LIMIT_TOTAL_STEPS:
            self._force(PHASE_ADJUDICATE, CODE_TOTAL_STEPS_EXCEEDED)
            return Decision(False, CODE_TOTAL_STEPS_EXCEEDED, "总步数已达上限 " + str(LIMIT_TOTAL_STEPS))

        if self.calls_by_phase.get(current, 0) >= self.budget_for(current):
            self._force(PHASE_ADJUDICATE, CODE_PHASE_BUDGET_EXCEEDED)
            return Decision(
                False,
                CODE_PHASE_BUDGET_EXCEEDED,
                current + " 阶段预算已达上限 " + str(self.budget_for(current)),
            )

        key = current + "|" + tool + "|" + canonical_params(params)
        count = self.seen.get(key, 0)
        if count >= LIMIT_DUPLICATE_CALLS:
            self._force(PHASE_ADJUDICATE, CODE_DUPLICATE_CALL)
            return Decision(False, CODE_DUPLICATE_CALL, "重复调用: " + tool + " 参数完全相同")

        self.seen[key] = count + 1
        self.total_steps += 1
        self.calls_by_phase[current] = self.calls_by_phase.get(current, 0) + 1
        return Decision(True, CODE_OK, "第 " + str(self.total_steps) + " 步")

    # -- 阶段流转 ---------------------------------------------------------
    def _force(self, phase: str, reason: str) -> None:
        if self.phase != phase:
            self.forced_transitions.append(self.phase + "->" + phase + ":" + reason)
            self.phase = phase

    def enter(self, phase: str) -> None:
        self.phase = phase

    def allow_subagents(self, count: int, *, depth: int = 1) -> Decision:
        if depth > LIMIT_SUBAGENT_DEPTH:
            return Decision(False, CODE_SUBAGENT_LIMIT_EXCEEDED, "子代理深度超过 " + str(LIMIT_SUBAGENT_DEPTH))
        if count > LIMIT_SUBAGENTS:
            return Decision(
                False, CODE_SUBAGENT_LIMIT_EXCEEDED, "子代理数量超过 " + str(LIMIT_SUBAGENTS)
            )
        if self.subagents_spawned + count > LIMIT_SUBAGENTS:
            return Decision(
                False, CODE_SUBAGENT_LIMIT_EXCEEDED, "子代理总量超过 " + str(LIMIT_SUBAGENTS)
            )
        self.subagents_spawned += count
        return Decision(True, CODE_OK, "派发 " + str(count) + " 个子代理")

    # -- 报告 -------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "total_steps": self.total_steps,
            "calls_by_phase": dict(self.calls_by_phase),
            "task_calls": self.task_calls,
            "answer_tool_calls": self.answer_tool_calls,
            "answer_write_calls": self.answer_write_calls,
            "subagents_spawned": self.subagents_spawned,
            "forced_transitions": list(self.forced_transitions),
        }


def params_fingerprint(params: dict[str, Any] | None) -> str:
    return hashlib.sha256(canonical_params(params).encode("utf-8")).hexdigest()[:16]
