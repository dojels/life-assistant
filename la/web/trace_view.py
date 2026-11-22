"""轨迹页(/trace):**当前上下文的装配规则** + **上一次对话真正发出去的那份上下文**。

用户 2026-10-13 的原话:*"我希望在个人家庭助手界面上加一个【轨迹】的菜单,在这里面,
可以看到当前上下文的装配规则,并能显示上一次对话的完整上下文。"*

这一页回答的问题只有一个:**模型这一轮到底看到了什么**。它由两半拼成:

  1. **装配规则**(规则表)—— 顺序与预算来自 `la/memory/projector.py` 与 `la/config.py`。
     ⭐ 规则表**从代码里读**,不是页面上另抄一遍:抄一遍的下场是"页面说得对、代码做得不对",
     而这一页存在的全部理由就是**让人能核对**。`tests/test_context_trace.py` 钉住
     "页面列出的段 == 装配器实际产出的段"。
  2. **上一次对话的完整上下文** —— `la/agent/context_trace.py` 每轮落下的那份 JSONL,
     **逐字**回放(不是重算:重算会漂移,而漂移出来的东西和当时发出去的长得一模一样)。

⚠️ 三条口径:
  * **纯读**:不调模型、不写任何东西(与记忆页/人格页同一条纪律,关掉 API 照样打得开);
  * **脱敏继承**:这一页显示的是**真实上下文**,所以敏感事实仍然只是 L4 里那个占位符
    —— "完整"指的是"没有第二个版本",不是"把省略掉的东西补回来";
  * **认不出来不猜**:记录缺字段(旧版本、半截写入)就当没有,不编一个数出来。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from ..agent.context_trace import MAX_RECORDS, read_records
from ..agent.context_trace import trace_path as default_trace_path
from ..agent.persona import PersonaStore
#: ⚠️ 助手那一侧的角色名**从 prompts 取**,不在这里再写一遍:人设名只许住在一个模块里
#: (tests/test_persona.py::test_persona_string_is_defined_in_exactly_one_module 钉着这条)。
from ..agent.prompts import PERSONA_NAME
from ..memory.projector import (
    SEGMENT_CHECKPOINT,
    SEGMENT_CONVERSATION,
    SEGMENT_EVIDENCE,
    SEGMENT_MEMORY,
    SEGMENT_RUNTIME,
    SEGMENT_SYSTEM,
    SEGMENT_TOOLS,
)

#: 段的显示顺序 = **模型真正遇到它们的顺序**(2026-10-16 用户点名改的)。
#: ⚠️ 它与 projector 里的键**必须一致**,由
#: tests/test_context_trace.py::test_rule_table_covers_exactly_the_segments_the_projector_emits
#: (`{规则表的键} == set(projection.segments)`)钉住。
#:
#: ⭐ 为什么工具**排在第二位**(原来排在最后):
#:   真实的一次请求是这个形状 —— `system` 消息 → **`tools` 字段** → 历史("对话")→
#:   `runtime` / `memory` / `evidence` 三条快照 → 最后那句 user。
#:   也就是说:工具 schema 是**紧跟系统提示之后**给到模型的,它比整段历史都在前面。
#:   把它画在最后会让人以为"它是某种收尾的附加物",而它其实是**每轮都要重算、
#:   又最不容易被压缩影响**的那一块(见 D128/D129 之后它已经比投影的 58% 还大)。
#:   ⚠️ 消息**列表**里的装配顺序仍然是 D122 那一条(易变段全部推到历史之后),
#:   那是投影内部的事(`Projection.message_segments`);这里排的是"模型看到的先后"。
SEGMENT_ORDER = (
    SEGMENT_SYSTEM, SEGMENT_TOOLS, SEGMENT_CHECKPOINT, SEGMENT_RUNTIME,
    SEGMENT_MEMORY, SEGMENT_CONVERSATION, SEGMENT_EVIDENCE,
)

#: 段 -> 消息里的角色标签(一条 system 消息里其实装了 system + runtime 两段)。
SEGMENT_LABELS = {
    SEGMENT_SYSTEM: "系统提示",
    SEGMENT_CHECKPOINT: "压缩检查点",
    SEGMENT_RUNTIME: "运行时上下文",
    SEGMENT_MEMORY: "记忆块",
    SEGMENT_CONVERSATION: "对话",
    SEGMENT_EVIDENCE: "考古证据",
    # ⚠️ 这句话反复被人读成"模型看不到它"。事实是:**同一次请求、另一个字段** ——
    #    写成"单独发出"太容易被读成"另一次调用",所以改成直白的一行(见 D119 的补充说明)。
    SEGMENT_TOOLS: "工具 schema(与 messages 同一次请求,走 tools 字段)",
}

ROLE_LABELS = {"system": "系统", "user": "你", "assistant": PERSONA_NAME}

#: 入口 -> 人话。认不出来就原样报(与 prompts.surface_lines 同一条口径)。
SURFACE_LABELS = {"web": "电脑网页", "weixin": "手机微信", "cli": "电脑终端"}

#: 三个页签(D119,与 D111 错题页 / D112 日历页同一套做法:**一页一件事**)。
#: ⭐ 默认落在「上下文」:点开这一页的人,十次里有九次是想看"刚才那一轮它到底看到了什么";
#: 规则是参考材料,读一次就够。
TRACE_TAB_CONTEXT = "context"
TRACE_TAB_RULES = "rules"
TRACE_TAB_HISTORY = "history"
TRACE_TABS = (TRACE_TAB_CONTEXT, TRACE_TAB_RULES, TRACE_TAB_HISTORY)
TAB_LABELS = {
    TRACE_TAB_CONTEXT: "上下文",
    TRACE_TAB_RULES: "规则",
    TRACE_TAB_HISTORY: "历史",
}

#: ⭐ 第四个页签「使用报表」(D136,用户 2026-10-16:
#:   *"在轨迹页多加一个子页面,可以看使用报表…"*)。
#: ⚠️ 它与前三个**不是同一份数据的三种看法**:报表数的是**时间的账**(事件日志按天/周/月聚合),
#:   而轨迹看的是**某一轮发出去的上下文**。所以它是一个独立入口 `/report`,只是挂在同一个页签条上
#:   —— 这样"它属于轨迹这一支"这件事在界面上是看得见的,而两边的口径也仍然是各说各的。
TRACE_TAB_REPORT = "report"
REPORT_TAB_LABELS = dict(TAB_LABELS)
REPORT_TAB_LABELS[TRACE_TAB_REPORT] = "使用报表"

#: 消息过滤器(服务端过滤,纯链接 —— 不引 JS)。
#: 为什么要它:系统提示常有几 KB,会把"这一轮到底说了什么"淹掉;
#: 而"上一次对话"里的"对话"两个字,指的正是 user / assistant 那几条。
ONLY_ALL = "all"
ONLY_TALK = "talk"
ONLY_SYSTEM = "system"
ONLY_ALLOWED = (ONLY_ALL, ONLY_TALK, ONLY_SYSTEM)
ONLY_LABELS = {ONLY_ALL: "全部", ONLY_TALK: "只看对话", ONLY_SYSTEM: "只看系统"}

#: 段 -> 占用图上的颜色类(在 app.css 里定义,浅色/深色各一套)。
SLICE_CLASSES = {
    SEGMENT_SYSTEM: "s-system",
    SEGMENT_CHECKPOINT: "s-checkpoint",
    SEGMENT_RUNTIME: "s-runtime",
    SEGMENT_MEMORY: "s-memory",
    SEGMENT_CONVERSATION: "s-conversation",
    SEGMENT_EVIDENCE: "s-evidence",
    SEGMENT_TOOLS: "s-tools",
}


@dataclass(frozen=True)
class SegmentRule:
    """一条装配规则。**全部字段都来自代码**(config / projector),不是文案。"""

    key: str
    order: str
    name: str
    budget: int
    source: str
    clipping: str
    note: str = ""


#: 进 messages 的**六段**上限之和(工具 schema 是第七段:单列、不进 messages)。
#: ⚠️ 它和硬上限的关系决定"装配时那刀砍不砍得着",所以两个数字都要在页面上说清。
SEGMENT_BUDGET_SUM = (
    config.BUDGET_SYSTEM + config.BUDGET_CHECKPOINT + config.BUDGET_RUNTIME
    + config.BUDGET_HOT_SUMMARY + config.BUDGET_CONVERSATION + config.BUDGET_EVIDENCE
)

SEGMENT_RULES: tuple[SegmentRule, ...] = (
    SegmentRule(
        key=SEGMENT_SYSTEM,
        order="①",
        name="系统提示(身份 / 人格 / 技能目录)",
        budget=config.BUDGET_SYSTEM,
        source="代码:prompts.IDENTITY(五条边界)+ prompts.build_persona_text()(人格页那一版 + 代码固定的尾巴)"
               " + skills.SkillLibrary.catalog_text()(技能目录,只有名字与一句说明)",
        clipping="超出即从尾部截断",
        note="⚠️ 截尾是有代价的:排在最后的是技能目录,被砍掉时助手会突然不知道自己会什么,"
             "而且不报错 —— 人格页把主体限制在 2000 字,正是为了不让它挤掉这一段。",
    ),
    SegmentRule(
        key=SEGMENT_TOOLS,
        order="②",
        name="工具 schema",
        budget=config.BUDGET_TOOLS,
        source="代码:llm.tools.build_tool_catalog() —— 每个工具的 JSON schema"
               "(实测值上报;预算那一栏 = config.BUDGET_TOOLS,也就是「为它预留了多少」)",
        clipping="不参与裁剪(定长 JSON,没有可截的地方)",
        note="⭐ 它**不是**提示词的一部分,但模型照样看得到,而且**排在历史前面**:"
             "真实请求的形状是 system 消息 → **tools 字段** → 历史 → 那三条快照 → 这一句 user。"
             "所以这一页把它画在系统提示**之后、其余各段之前** —— 那才是模型遇到它的位置。"
             "(官方接口靠这个字段把「模型想调哪个工具、参数是什么」变成结构化的 tool_calls;"
             "写进 messages 反而拿不到 tool_calls。)"
             "⚠️ 每轮完全一样、压缩也动不了它 —— 所以单列记账,不并进 messages 那本账"
             "(并进去的话,每一轮都会越过触发线,页面上就会说成「该压缩了」,见 D95)。"
             "⚠️ 这一栏的「预算」是**预留额**不是上限:工具 schema 改大了它就该跟着改"
             "(2026-10-16 从 2500 的旧估算改成实测值,见 D130;"
             "此后每加一个工具都贴着实测改:加 self_inspect → 7969(D138),"
             "加 page_view → 8270(D140),换联网工具描述 → 8314(D148));"
             "真超了会如实标出来 —— 想省这笔钱只有一条路:砍工具(合并 / 按域裁撤)。"
    ),
    SegmentRule(
        key=SEGMENT_CHECKPOINT,
        order="③",
        name="压缩检查点(<compacted-context>)",
        budget=config.BUDGET_CHECKPOINT,
        source="代码:memory.projector.build_checkpoint_block() —— 摘要池里这一会话的桶:"
               "时间区间、摘要编号、一行概要(正文不复制,要细节按需检索)",
        clipping="只留最近 " + str(config.CHECKPOINT_MAX_ITEMS)
                 + " 段,再早的折成一句「还有 N 段」",
        note="⭐ 它回答的是「前面是不是还有过对话」。没有它,压缩过的历史对模型来说等于"
             "从没发生过 —— 只能靠关键词检索撞上,撞不上就真的成了「记不清」。"
             "⚠️ 排在历史**之前**是算过账的:前缀缓存命中「连续两轮从头逐字节相同的那一段」,"
             "它只在压缩发生时变一次,于是整段落在缓存里、还把可命中的前缀往后推;"
             "挪到历史**之后**就会每轮都付全价(见 D142)。"
             "代价:每压一次,下一条请求的整条前缀作废一次(约每 1400 轮压一次)。",
    ),
    SegmentRule(
        key=SEGMENT_RUNTIME,
        order="④",
        name="运行时上下文",
        budget=config.BUDGET_RUNTIME,
        source="代码:prompts.build_runtime_context() —— 当前时间、时区、会话、本轮序号、"
               "对话入口(手机微信 / 电脑网页 / 终端)、上次活动时间",
        clipping="超出即从尾部截断",
        note="⭐ D122 起它**不再拼在系统提示里**,而是排在历史之后的**一条 system 快照**"
             "(与 DSH 同形):每轮都变的东西放末尾,前面几千 token 的历史才能继续命中前缀缓存"
             "(实测跨轮命中 22.5% → 93.1%,见 scripts/verify_prefix_cache.py)。"
             "它单独记账、有自己的预算。时间靠注入,不靠模型猜 —— "
             "这是“不要凭印象编造”那条边界的地基。",
    ),
    SegmentRule(
        key=SEGMENT_MEMORY,
        order="⑤",
        name="记忆块(<memory>)",
        budget=config.BUDGET_HOT_SUMMARY,
        source="代码:memory.projector.build_memory_block() —— 这一轮检索命中的事实与摘要,"
               "外加今天到点的推送、进行中的长期目标",
        clipping="从最旧的记忆条目开始淘汰",
        note="不可淘汰的两样:briefing(今天到点的推送)与 goals(长期目标状态)——"
             "丢了就等于“助手忘了自己在跟什么事”。标了敏感的事实只留占位符与编号。",
    ),
    SegmentRule(
        key=SEGMENT_CONVERSATION,
        order="⑥",
        name="对话投影",
        budget=config.BUDGET_CONVERSATION,
        source="事件日志(events 表)里这一会话的 user / assistant 消息",
        clipping="从最旧的一对开始丢,而且成对丢(不会留下孤立的 assistant 消息)",
        note=(
            "六段上限之和是 " + str(SEGMENT_BUDGET_SUM) + ",装得进硬上限 "
            + str(config.BUDGET_HARD_MAX) + " —— 所以正常装配下这一刀是收着的;真要收,"
            "收的也只有它(min(它的预算, 硬上限 − 其余固定段)),其余固定段与工具 schema 都不动。"
            if SEGMENT_BUDGET_SUM <= config.BUDGET_HARD_MAX else
            "⚠️ 六段上限之和(" + str(SEGMENT_BUDGET_SUM) + ")本身就超过硬上限 "
            + str(config.BUDGET_HARD_MAX) + " —— 所以真正分给对话段的是"
            " min(它的预算, 硬上限 − 其余固定段),超了就先收紧它。"
        ),
    ),
    SegmentRule(
        key=SEGMENT_EVIDENCE,
        order="⑦",
        name="考古证据块",
        budget=config.BUDGET_EVIDENCE,
        source="代码:memory.adjudicator.render_evidence_block() —— 按时间回查原文捞出来的片段",
        clipping="超出即从尾部截断",
        note="平时是 0:只有“上次 / 之前 / 那次”这类回忆性提问才会走这条慢路径。",
    ),
)

#: 记忆块内部的顺序(与 build_memory_block 的拼装顺序一一对应)。
#: ⭐ 2026-10-16(D132)改成「**变得慢的放前面**」:facts 在一轮里完全不动、goals 推进时才动,
#:    而 memory-status / briefing / hot-memories 每轮都可能变。同一次应答里模型可能被请求
#:    好几次(工具往返),那几次的记忆块是同一份 —— 稳定的排前面,后面的请求才能整段命中它。
MEMORY_BLOCK_STEPS: tuple[tuple[str, str], ...] = (
    ("<facts>", "生效的永久事实,最多 " + str(config.HOT_SUMMARY_MAX_ITEMS) + " 条;"
                "标了敏感的只留占位符与编号"),
    ("<goals>", "进行中的长期目标 —— 不可淘汰;它排在 facts 之后,因为它会被推进/关闭"),
    ("<memory-status>", "这次检索命中了没有:命中就正常注入,没命中会多一句“记不清就直说,别拿猜测填空”"),
    ("briefing", "今天到点的推送(错题复习等)—— 不可淘汰,预算再紧也先保它"),
    ("<hot-memories>", "摘要池按热度取,最多 " + str(config.HOT_SUMMARY_MAX_ITEMS) + " 条,"
                       "每条只取头部 " + str(config.HOT_SUMMARY_HEAD_CHARS) + " 字"),
    # ⭐ 2026-10-25(D168):排在**块尾**是刻意的 —— 它每轮都可能变,放前面会把整块的前缀缓存作废。
    ("<pending-review>", "有记忆候选在等人放行时才出现:里面教模型用 memory_review 放行,"
                         "不让微信里那句「通过吧」落空"),
)

#: 预算线各管什么(用户 2026-10-14 的追问:"到多少 token 就会压缩?")。
#: ⭐ 这几个数字最容易被混着看,所以把"谁真的会触发动作"摆出来 —— **只有前两条会**:
#:   压缩触发线(D134 起写死 120000)与硬上限(装配时收紧对话段)。
#: ⚠️ 2026-10-17:原来的「总锁」一条已删除 —— D134 之后它不参与任何判定,
#:   留在页面上只会让人再问一遍"到它就压缩吗"(用户:"没有作用就删除,不要污染页面")。
#: ⭐ D121 起多列一条「模型窗口」:它才是真正的天花板,而我们离它极远 —— 这一条专门用来
#:   打消"怎么这么快就压缩"的疑惑(决定触发线的是**成本与延迟**,不是模型装不下)。
BUDGET_LINES: tuple[dict[str, Any], ...] = (
    {
        "key": "trigger",
        "name": "压缩触发线",
        "value": config.BUDGET_COMPACT_TRIGGER,
        "role": "会触发:去压缩最旧的未压缩区间",
        "when": "这一轮回复发出去之后(不是装配时)",
        "detail": "**历史上下文**(进 messages 的那一段,不含系统提示与工具 schema)一越过它,"
                  "回合结束时的 _maybe_compact() 就会压一次 —— 把最旧的一段事件压成摘要,"
                  "**留下来的历史变成原来的 " + str(int(config.BUDGET_COMPACT_RETAIN_RATIO * 100)) + "%**"
                  "(D134,用户 2026-10-16 拍板)。\n"
                  "⭐ 为什么**只在回合结束后**压(不在装配时、也不在工具往返中间):"
                  "一轮里模型可能调几十次工具,那些往返是**串行依赖**的 —— 中途把历史收走,"
                  "正在做的任务就会断掉。所以装配时只置一个标志,等这一轮回答发出去再压。\n"
                  "⚠️ 这条线是**写死的数**(不再由任何比例推出来):触发线的依据是成本与延迟,"
                  "不是模型装不下(1M 窗口离得很远),所以留一个能直接改的数。",
    },
    {
        "key": "hard",
        "name": "硬上限",
        "value": config.BUDGET_HARD_MAX,
        "role": "会触发:装配时当场收紧对话段",
        "when": "装配的时候立刻",
        "detail": "装配时先把固定四段(系统 / 运行时 / 记忆 / 证据)放进去,剩下的才给对话段 —— "
                  "对话预算**三取小**:min(对话段自己的预算 " + str(config.BUDGET_CONVERSATION)
                  + ", 硬上限 − 工具预留 " + str(config.BUDGET_TOOLS) + " − 固定四段, "
                  + "触发线 " + str(config.BUDGET_COMPACT_TRIGGER) + " + 轮内留量 "
                  + str(config.BUDGET_IN_TURN_HEADROOM) + " − 固定四段)。"
                  "⚠️ 实测下来**生效的通常是第一项**(对话段自己的预算),因为它最小;"
                  "第②项要等固定四段超过 " + str(config.BUDGET_HARD_MAX - config.BUDGET_TOOLS
                  - config.BUDGET_CONVERSATION) + " 才会咬住(实际才几百 token),"
                  "第③项比对话段预算大 " + str(config.BUDGET_COMPACT_TRIGGER
                  + config.BUDGET_IN_TURN_HEADROOM - config.BUDGET_CONVERSATION) + "。"
                  "所以它做的是「少给对话段」而不是「当场压缩」;"
                  "被砍掉的消息记在 dropped_turns 里(见「上下文」那一页)。"
                  "⭐ D133 起这一刀**要把工具预留额也扣掉**(它描述的是整个投影能用多少);"
                  "⭐ D134 起它还要装得下「从触发线到这一轮结束之间,工具往返涨出来的那一段」——"
                  "所以正常装配下它是收着的,它不是每天都会砍到你。"
                  "⭐ 而且这个数**是从窗口推出来的**(D122):窗口 × "
                  + str(int(config.SINGLE_TURN_INPUT_SHARE * 100)) + "% = 单轮真实上限 "
                  + str(config.BUDGET_REQUEST_REAL) + "(那 30% 是 2026-10-14 定的口径:"
                  "成本与延迟的取向,不是模型装不下),扣掉工具预留 "
                  + str(config.BUDGET_TOOLS) + " 与轮内留量 " + str(config.BUDGET_TURN_WORKING)
                  + " 估算之后,剩下的那块才是它。",
    },
    {
        "key": "window",
        "name": "模型的上下文窗口",
        "value": config.CONTEXT_WINDOW_TOKENS,
        "role": "不是我们的预算 —— 它只说明「还早得很」",
        "when": "从不触发(它是模型的能力,不是我们的闸)",
        "detail": "同一款 deepseek-flash 的窗口是 100 万 token(来源:DSH 的模型表)。"
                  "我们把**单轮真实输入**的上限定在它的 "
                  + str(int(config.SINGLE_TURN_INPUT_SHARE * 100)) + "%(即 "
                  + str(config.BUDGET_REQUEST_REAL) + " token;这个比例是 2026-10-14 定的"
                  "取向,依据是成本与延迟,不是模型装不下),压缩触发线 "
                  + str(config.BUDGET_COMPACT_TRIGGER)
                  + " 只占它的 %.1f%% —— 决定这两条线的是成本与延迟,不是模型装不下。"
                  % (100.0 * config.BUDGET_COMPACT_TRIGGER
                     / config.CONTEXT_WINDOW_TOKENS),
    },
)

#: 对话段的三条取舍(代码在 session.conversation_history / projector._fit_conversation)。
CONVERSATION_RULES: tuple[str, ...] = (
    "只取 user / assistant 两种消息 —— 工具结果是执行轨迹,不是聊过的内容,不进投影",
    "已经压缩过的那几段整段跳过 —— 它们变成摘要(检索命中才进记忆段)"
    "和历史最前面那条检查点,原文仍在档案里",
    "跨轮历史**不回传**思维链(照云朵的口径:思考只活在本轮)—— ⚠️ 这是取舍不是 API 要求:"
    "实测 API 只要求轮内那条「结果还没被消费」的 assistant 带 reasoning_content **字段**,"
    "内容可以是空;跨轮历史连字段都不校验(scripts/verify_reasoning_replay.py)。"
    "带回去的话,历史窗口的三分之二会花在「它当时怎么想的」上(实测思维链 69500 字符 vs "
    "正文 30000 字符)。要照 DSH 全文回传:LA_REPLAY_HISTORY_REASONING=1",
)


@dataclass
class TraceSegment:
    key: str
    order: str
    name: str
    actual: int
    budget: int
    pre_clip: int
    clipped: bool
    ratio: float
    label: str


@dataclass
class TraceSlice:
    """占用图上的一块。宽度按**同一把刻度**算好(模板里不做算术)。"""

    key: str
    label: str
    actual: int
    percent: float
    css: str = ""


@dataclass
class TraceScale:
    """一条"用掉多少 / 离刻度还有多远"的图,外加刻度线。

    ⭐ 为什么要一张图而不是一排数字:四个 token 数(投影 / 工具 / 合计 / 距触发线)分开看都对,
       但"谁占了大头"得自己在脑子里做除法。图把这件事直接摆出来 —— 实测这一页最该被抓住的
       是"工具 schema 10013"这个数,而不是 654。
    """

    limit: int
    used: int
    used_percent: float
    marks: list[dict[str, Any]] = field(default_factory=list)
    slices: list[TraceSlice] = field(default_factory=list)
    over: bool = False
    note: str = ""


@dataclass
class TraceMessage:
    index: int
    role: str
    role_label: str
    segment: str
    segment_label: str
    tokens: int
    preview: str
    content: str
    reasoning: str = ""


@dataclass
class TraceTurn:
    index: int
    at: str
    session: str
    turn: int
    surface: str
    surface_label: str
    provider: str
    model: str
    total: int
    tools: int
    grand_total: int
    dropped_turns: int
    capped: bool
    needs_compaction: bool
    #: ⭐ D122:这一轮里"下一轮还能原样重放"的那段(估算)—— 前缀缓存的记分牌
    cache_prefix: int = 0
    segments: list[TraceSegment] = field(default_factory=list)
    messages: list[TraceMessage] = field(default_factory=list)
    parts: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    persona_custom: bool | None = None
    persona_chars: int = 0
    #: 投影的占用图(不含工具 schema)
    stack: TraceScale | None = None
    #: 工具 schema 的占用图 —— 它不进 messages,所以**单独一把刻度**
    tool_scale: TraceScale | None = None

    @property
    def talk_count(self) -> int:
        return sum(1 for item in self.messages if item.role in ("user", "assistant"))

    @property
    def system_count(self) -> int:
        return sum(1 for item in self.messages if item.role == "system")


@dataclass
class TraceView:
    generated_at: str
    rules: tuple[SegmentRule, ...]
    totals: dict[str, int]
    current: dict[str, str]
    #: 三条预算线(触发线 / 硬上限 / 模型窗口)各管什么 —— 这几个数最容易被混着看
    budget_lines: tuple[dict[str, Any], ...] = BUDGET_LINES
    #: 工具 schema 那一条规则。⭐ 按 key **找**,不写 rules[-1] —— 显示顺序会变
    #: (2026-10-16 它就从头挪到了第二位),写死下标的话模板会静默显示成另一段。
    rules_tools: SegmentRule | None = None
    memory_steps: tuple[tuple[str, str], ...] = MEMORY_BLOCK_STEPS
    conversation_rules: tuple[str, ...] = CONVERSATION_RULES
    turn: TraceTurn | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    index: int = 0
    total_records: int = 0
    keep: int = MAX_RECORDS
    trace_file: str = ""
    trace_exists: bool = False
    #: 当前页签(context / rules / history / report)与它那一排链接
    tab: str = TRACE_TAB_CONTEXT
    tabs: list[dict[str, Any]] = field(default_factory=list)
    #: ⭐ 使用报表(独立页面 /report,但共用这一排页签)。这几个字段只在那一页填。
    period: str = "day"
    period_tabs: list[dict[str, Any]] = field(default_factory=list)
    period_label: str = "按天"
    custom_start: str = ""
    custom_end: str = ""
    default_days: int = 0
    metric_rows: list[Any] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)
    #: 只在历史事件里出现过的旧工具名(注册表里已经没有)
    tools_retired: list[Any] = field(default_factory=list)
    #: 注册表里"由框架自己发起"的那几个(不在工具表里,但算 schema 开销)
    internal_catalog: list[str] = field(default_factory=list)
    skills: list[Any] = field(default_factory=list)
    internal_tools: list[Any] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    usage_available: bool = False
    #: 当前注册表里有多少个工具(0 = 这一次没读到注册表)
    catalog_size: int = 0
    #: 注册了但这一段时间一次都没调过的工具名(优化时先看这一串)
    never_called: list[str] = field(default_factory=list)
    #: 工具表的排序口径(calls / failed)与那两个切换链接(带当前时段)
    sort: str = "calls"
    sort_link_calls: str = "/report?sort=calls"
    sort_link_failed: str = "/report?sort=failed"
    #: ⚠️ 报表的时段行**单独一格**,不复用 history/totals —— 那两格是"轨迹与预算"的口径
    report_rows: list[Any] = field(default_factory=list)
    report_totals: Any = None
    range_label: str = ""
    price_note: str = ""
    #: 消息过滤(全部 / 只看对话 / 只看系统)与**过滤后**要渲染的那些消息
    only: str = ONLY_ALL
    only_options: list[dict[str, Any]] = field(default_factory=list)
    messages: list[TraceMessage] = field(default_factory=list)


def _clip(text: Any, limit: int = 90) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _surface_label(surface: Any) -> str:
    key = str(surface or "").strip()
    if not key:
        return "未知入口(命令行/定时任务等)"
    return SURFACE_LABELS.get(key, key)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_turn(
    record: dict[str, Any], *, index: int = 0, assistant_name: str | None = None
) -> TraceTurn:
    """把一条落盘的记录翻成页面要的形状。**缺字段就当没有,不猜**。

    ⭐ D144:assistant_name 是**现在**的名字(人格页里可改)。角色标签显示"现在她叫什么",
       而记录本身是死的 —— 所以历史记录里那句话不会因为改名而变。
    """
    role_labels = dict(ROLE_LABELS)
    if assistant_name:
        role_labels["assistant"] = assistant_name
    segments_raw = record.get("segments") or {}
    pre_clip = record.get("pre_clip") or {}
    turn = TraceTurn(
        index=index,
        at=str(record.get("at") or ""),
        session=str(record.get("session") or ""),
        turn=_to_int(record.get("turn")),
        surface=str(record.get("surface") or ""),
        surface_label=_surface_label(record.get("surface")),
        provider=str(record.get("provider") or ""),
        model=str(record.get("model") or ""),
        total=_to_int(record.get("total")),
        tools=_to_int(record.get("tools")),
        grand_total=_to_int(record.get("grand_total")),
        dropped_turns=_to_int(record.get("dropped_turns")),
        capped=bool(record.get("capped")),
        cache_prefix=_to_int(record.get("cache_prefix")),
        needs_compaction=bool(record.get("needs_compaction")),
        persona_custom=(
            bool(record["persona_custom"]) if "persona_custom" in record else None
        ),
        persona_chars=_to_int(record.get("persona_chars")),
    )
    for key in SEGMENT_ORDER:
        rule = next(item for item in SEGMENT_RULES if item.key == key)
        actual = _to_int(segments_raw.get(key))
        before = _to_int(pre_clip.get(key))
        # ⚠️ 对话段的预算是**动态**的(硬上限 − 其余固定段):拿 4000 去比会说假话
        budget = rule.budget
        if key == SEGMENT_CONVERSATION:
            tightened = _to_int(record.get("conversation_budget"))
            if tightened > 0:
                budget = tightened
        turn.segments.append(
            TraceSegment(
                key=key,
                order=rule.order,
                name=rule.name,
                actual=actual,
                budget=budget,
                pre_clip=before,
                # 裁剪前比裁剪后大 = 这一段被截过(只有 system/runtime/memory/evidence 记了 pre_clip)
                clipped=before > actual,
                ratio=min(actual / budget, 1.0) if budget else 0.0,
                label=SEGMENT_LABELS.get(key, key),
            )
        )
    # ⚠️ 记录里出现了规则表**不认识**的段(比如更新版本的进程写的记录)时,**照原样列出来**:
    #    这一页宁可显示一个没有注解的段,也不能让人觉得"上下文就这些"(静默丢数据是最坏的失败)。
    for key, value in segments_raw.items():
        if key in SEGMENT_ORDER:
            continue
        turn.segments.append(
            TraceSegment(
                key=str(key), order="?", name=str(key), actual=_to_int(value),
                budget=0, pre_clip=_to_int(pre_clip.get(key)), clipped=False, ratio=0.0,
                label=str(key) + "(这一版不认识的一段)",
            )
        )
    for position, message in enumerate(record.get("messages") or []):
        if not isinstance(message, dict):
            continue
        content = str(message.get("content") or "")
        segment = str(message.get("segment") or "")
        # 一条 system 消息里装了两段(系统提示 + 拼在末尾的运行时上下文),页面上要说清
        # ⚠️ D122 之前,运行时上下文是拼在系统提示**同一条消息**里的,所以这里要合成一个
        #    "系统提示 + 运行时上下文"的标签;现在它是一个独立的段(排在历史之后),
        #    标签按 message_segments 原样取就行 —— 页面说的必须就是装配器做的。
        label = SEGMENT_LABELS.get(segment, segment or "—")
        turn.messages.append(
            TraceMessage(
                index=position,
                role=str(message.get("role") or ""),
                role_label=role_labels.get(str(message.get("role") or ""), str(message.get("role") or "")),
                segment=segment,
                segment_label=label,
                tokens=_to_int(message.get("tokens")),
                preview=_clip(content, 90),
                content=content,
                reasoning=str(message.get("reasoning") or ""),
            )
        )
    for part in record.get("parts") or []:
        if isinstance(part, dict):
            turn.parts.append(dict(part))
    turn.notes = _turn_notes(turn, record)
    turn.stack = build_stack(turn)
    turn.tool_scale = build_tool_scale(turn)
    return turn


def build_stack(turn: TraceTurn) -> TraceScale:
    """投影的占用图:每段一块 —— **刻度就是这一轮实际的大小**(谁的占比一眼可见)。

    ⚠️ D121 之后刻度不再拿硬上限当分母:一条 3k 的投影放到 40k 的刻度上只剩一根细线,
       "谁占了大头"这件事反而看不见了 —— 而那正是这张图存在的理由(见 D119)。
       所以:先按这一轮的大小定刻度,三条线**落在刻度内才画出来**;离触发线还有多远,
       上面那格指标(「距压缩触发线」)已经在说,不必挤在这一张图里。
    ⚠️ 工具 schema **不在这张图里** —— 它与 messages 在同一次请求里,但走 API 的 tools 字段,
       两笔钱分开算,所以分开画(见 build_tool_scale 与页面上那张"一次请求长什么样")。
    """
    trigger = config.BUDGET_COMPACT_TRIGGER
    limit = max(int(turn.total * 1.06), 1)
    slices = [
        TraceSlice(
            key=item.key,
            label=item.label,
            actual=item.actual,
            percent=min(item.actual / limit * 100.0, 100.0),
            css=SLICE_CLASSES.get(item.key, ""),
        )
        for item in turn.segments
        # 0 的段不进图:图是给"看得见"用的,占 0 的块只会添噪
        if item.key != SEGMENT_TOOLS and item.actual > 0
    ]
    marks: list[dict[str, Any]] = []
    if trigger <= limit:
        marks.append({"key": "trigger", "label": "压缩触发线", "at": trigger,
                      "percent": min(trigger / limit * 100.0, 100.0), "css": "trigger"})
    if turn.total >= config.BUDGET_HARD_MAX:
        note = ("投影 " + str(turn.total) + " · 已越过硬上限 "
                + str(config.BUDGET_HARD_MAX) + "(刻度按实际扩到 " + str(limit) + ")")
    elif turn.total >= trigger:
        note = ("投影 " + str(turn.total) + " · 已越过压缩触发线 " + str(trigger)
                + "(刻度按这一轮实际大小)")
    else:
        note = ("投影 " + str(turn.total) + " · 刻度按这一轮实际大小;压缩触发线 "
                + str(trigger) + " 在刻度之外(这一轮用了它的 "
                + str(round(100.0 * turn.total / max(trigger, 1))) + "%)")
    return TraceScale(
        limit=limit,
        used=turn.total,
        used_percent=min(turn.total / limit * 100.0, 100.0),
        marks=marks,
        slices=slices,
        over=turn.total > config.BUDGET_HARD_MAX,
        note=note,
    )


def build_tool_scale(turn: TraceTurn) -> TraceScale:
    """工具 schema 的占用图 —— **单独一把刻度**。

    ⚠️ 为什么不与投影共用刻度:共用的话,投影那一小截会细成一根线,
       "工具比上下文还大"这件事反而**看不见**了 —— 而那正是这一页最该说出来的事实。
    """
    limit = max(turn.tools, config.BUDGET_TOOLS, 1)
    return TraceScale(
        limit=limit,
        used=turn.tools,
        used_percent=min(turn.tools / limit * 100.0, 100.0),
        marks=[{
            "key": "budget", "label": "预算量级", "at": config.BUDGET_TOOLS,
            "percent": min(config.BUDGET_TOOLS / limit * 100.0, 100.0), "css": "trigger",
        }],
        slices=(
            [TraceSlice(
                key=SEGMENT_TOOLS, label="工具 schema", actual=turn.tools,
                percent=min(turn.tools / limit * 100.0, 100.0),
                css=SLICE_CLASSES.get(SEGMENT_TOOLS, ""),
            )]
            if turn.tools else []
        ),
        over=turn.tools > config.BUDGET_TOOLS,
        note="工具 schema " + str(turn.tools) + " · 预算量级 " + str(config.BUDGET_TOOLS),
    )


def _filter_messages(messages: list[TraceMessage], only: str) -> list[TraceMessage]:
    """按角色过滤消息。**服务端过滤** —— 纯链接,不引 JS(与页签同一条纪律)。"""
    if only == ONLY_TALK:
        return [item for item in messages if item.role in ("user", "assistant")]
    if only == ONLY_SYSTEM:
        return [item for item in messages if item.role == "system"]
    return list(messages)


def _turn_notes(turn: TraceTurn, record: dict[str, Any]) -> list[str]:
    """这一轮的**异常之处**。⭐ 只报事实,不解释、不猜测。"""
    notes: list[str] = []
    if turn.dropped_turns:
        notes.append(
            "对话段被砍过:最旧的 " + str(turn.dropped_turns) + " 条消息没进这一轮上下文"
            "(投影超预算时成对丢弃)。它们仍在事件日志里,只是这一轮没发出去。"
        )
    conversation = next(
        (item for item in turn.segments if item.key == SEGMENT_CONVERSATION), None
    )
    if turn.capped and conversation is not None:
        notes.append(
            "硬上限生效:对话段的预算被压到约 " + str(conversation.budget) + " token"
            "(不是默认的 " + str(config.BUDGET_CONVERSATION) + ")—— 其余固定段先占住了位置。"
        )
    for item in turn.segments:
        if item.clipped:
            notes.append(
                "「" + item.label + "」超预算被截尾:裁剪前约 " + str(item.pre_clip)
                + " token,发出去的是 " + str(item.actual) + "。"
            )
    if turn.needs_compaction:
        notes.append(
            "已经越过压缩触发线(" + str(config.BUDGET_COMPACT_TRIGGER)
            + " token):这一轮之后该把它压成摘要了。"
        )
    if not record.get("parts"):
        notes.append("这条记录没有留下“系统提示由哪几段拼成”的明细(旧版本记录)。")
    return notes


def _current_facts(memory_conn: sqlite3.Connection | None) -> dict[str, str]:
    """当前生效的事实与摘要各有多少条。**裸 SQL、只读** —— 不建索引、不写任何东西。"""
    out = {"facts": "—", "summaries": "—"}
    if memory_conn is None:
        return out
    for key, sql in (
        ("facts", "SELECT COUNT(*) FROM facts WHERE active = 1"),
        ("summaries", "SELECT COUNT(*) FROM summary_pool"),
    ):
        try:
            out[key] = str(int(memory_conn.execute(sql).fetchone()[0]))
        except Exception:  # noqa: BLE001 - 这一页坏一个数字不该整页打不开
            out[key] = "—"
    return out


def _build_tabs(view: TraceView) -> list[dict[str, Any]]:
    """三个页签 + 各自的计数。⭐ 切页签时**保留正在看的那一轮与过滤器** ——
    否则"翻到第 3 轮 → 去看规则 → 切回上下文"会莫名其妙跳回最新那一轮。"""
    counts = {
        TRACE_TAB_CONTEXT: len(view.turn.messages) if view.turn else 0,
        TRACE_TAB_RULES: len(SEGMENT_RULES),
        TRACE_TAB_HISTORY: len(view.history),
    }
    tabs: list[dict[str, Any]] = []
    for key in TRACE_TABS:
        href = "/trace?tab=" + key
        if key == TRACE_TAB_CONTEXT:
            if view.index > 0:
                href += "&i=" + str(view.index)
            if view.only != ONLY_ALL:
                href += "&only=" + view.only
        tabs.append({
            "key": key, "label": TAB_LABELS[key], "href": href,
            "count": counts[key], "on": view.tab == key,
        })
    # ⭐ D136:第四个入口「使用报表」。它是独立页面(/report),不带计数 ——
    #    "报"这个字本来就不该有一个数压在旁边(它是时间的账,不是某一轮的条数)。
    tabs.append({
        "key": TRACE_TAB_REPORT, "label": REPORT_TAB_LABELS[TRACE_TAB_REPORT],
        "href": "/report", "count": 0, "on": view.tab == TRACE_TAB_REPORT,
    })
    return tabs


def _build_only_options(view: TraceView) -> list[dict[str, Any]]:
    """消息过滤器那一排(全部 / 只看对话 / 只看系统),带条数。"""
    counts = {
        ONLY_ALL: len(view.turn.messages) if view.turn else 0,
        ONLY_TALK: view.turn.talk_count if view.turn else 0,
        ONLY_SYSTEM: view.turn.system_count if view.turn else 0,
    }
    options: list[dict[str, Any]] = []
    for key in ONLY_ALLOWED:
        href = "/trace?tab=" + TRACE_TAB_CONTEXT
        if view.index > 0:
            href += "&i=" + str(view.index)
        if key != ONLY_ALL:
            href += "&only=" + key
        options.append({
            "key": key, "label": ONLY_LABELS[key], "href": href,
            "count": counts[key], "on": view.only == key,
        })
    return options


def build_period_tabs(
    view: TraceView, *, periods: tuple[str, ...], labels: dict[str, str]
) -> list[dict[str, Any]]:
    """报表页那一排时段选择(按天 / 按周 / 按月 / 按年 / 自定义)。

    ⚠️ 切时段时**把自定义的起止带过去** —— 否则"填好区间 -> 想换个粒度看看 -> 区间没了"
       会让人以为表单坏了(与「切页签保留正在看的那一轮」同一条取舍)。
    """
    tabs: list[dict[str, Any]] = []
    for key in periods:
        href = "/report?period=" + key
        if key == "custom" and (view.custom_start or view.custom_end):
            if view.custom_start:
                href += "&start=" + str(view.custom_start)
            if view.custom_end:
                href += "&end=" + str(view.custom_end)
        tabs.append({
            "key": key, "label": labels[key], "href": href, "on": view.period == key,
        })
    return tabs


def build_trace_view(
    settings: config.Settings,
    *,
    assistant_conn: sqlite3.Connection | None = None,
    memory_conn: sqlite3.Connection | None = None,
    index: Any = 0,
    tab: str = TRACE_TAB_CONTEXT,
    only: str = ONLY_ALL,
    now_text: str = "",
    path: Path | str | None = None,
    sessions: set[str] | None = None,
) -> TraceView:
    """把这一页要的一切拼出来。**纯读** —— 不经过模型,也不写任何文件。

    `sessions` 给定时只显示这些会话的轨迹(家庭版按成员过滤);None = 全部。
    """
    records = read_records(limit=MAX_RECORDS, path=path)
    if sessions is not None:
        records = [item for item in records if str(item.get("session") or "") in sessions]
    target = Path(path) if path is not None else default_trace_path()
    view = TraceView(
        generated_at=now_text,
        rules=SEGMENT_RULES,
        rules_tools=next(
            (item for item in SEGMENT_RULES if item.key == SEGMENT_TOOLS), None
        ),
        memory_steps=MEMORY_BLOCK_STEPS,
        conversation_rules=CONVERSATION_RULES,
        totals={
            "hard_max": config.BUDGET_HARD_MAX,
            "trigger": config.BUDGET_COMPACT_TRIGGER,
            # ⭐ 模型窗口(1M)也摆上:与触发线一比,就能说清"这条线是钱和延迟定的,不是装不下"
            "window": config.CONTEXT_WINDOW_TOKENS,
            # ⚠️ 两个口径别混:进 messages 的是**六段**,工具 schema 是另一格(tools 字段)、不进 messages。
            #    页面上原先把六段之和(13200)写成了"五段之和" —— 一个数字对不上代码的错(已修)。
            "budget_sum": sum(
                item.budget for item in SEGMENT_RULES if item.key != SEGMENT_TOOLS
            ),
            "budget_sum_with_tools": sum(item.budget for item in SEGMENT_RULES),
            "keep": MAX_RECORDS,
        },
        current=_current_state(settings, assistant_conn, memory_conn),
        total_records=len(records),
        keep=MAX_RECORDS,
        trace_file=str(target),
        trace_exists=target.exists(),
        # 页签与过滤器都收敛到白名单:认不出来的一律回**默认**(上下文 / 全部),
        # 手打的链接与旧书签都不该把这一页打成空白或 422(与 _int_or 同一条口径)。
        tab=tab if tab in TRACE_TABS else TRACE_TAB_CONTEXT,
        only=only if only in ONLY_ALLOWED else ONLY_ALL,
    )
    if records:
        # 认不出来的(比如 ?i=abc)看最新那条;越界的**夹到边界**(?i=99 → 最旧那条)
        view.index = max(0, min(_to_int(index, 0), len(records) - 1))
        view.turn = build_turn(
            records[view.index], index=view.index,
            assistant_name=str(view.current.get("persona_name") or ""),
        )
    for position, record in enumerate(records):
        view.history.append({
            "index": position,
            "at": str(record.get("at") or ""),
            "session": str(record.get("session") or ""),
            "turn": _to_int(record.get("turn")),
            "surface_label": _surface_label(record.get("surface")),
            "model": str(record.get("model") or ""),
            "total": _to_int(record.get("total")),
            "tools": _to_int(record.get("tools")),
            "grand_total": _to_int(record.get("grand_total")),
            "messages": len(record.get("messages") or []),
        })
    view.tabs = _build_tabs(view)
    view.only_options = _build_only_options(view)
    if view.turn is not None:
        view.messages = _filter_messages(view.turn.messages, view.only)
    return view


def _current_state(
    settings: config.Settings,
    assistant_conn: sqlite3.Connection | None,
    memory_conn: sqlite3.Connection | None,
) -> dict[str, str]:
    """规则是稳定的,参数是**活的** —— 这一栏回答"现在这套规则跑在什么上面"。"""
    state = PersonaStore(assistant_conn).state()
    skills = "—"
    try:
        from ..skills import SkillLibrary

        skills = str(len(SkillLibrary().scan()))
    except Exception:  # noqa: BLE001 - 技能目录读不到就如实显示"—"
        skills = "—"
    counters = _current_facts(memory_conn)
    return {
        "model": settings.model if settings.provider != "fake" else "离线(fake)",
        "provider": settings.provider,
        # ⭐ D144/D145:名字是数据 —— 这里也摊开(页面那枚标签直接显示这一句)
        "persona": state.name + " · " + state.source_label + "(" + str(state.chars) + " 字)",
        "persona_name": state.name,
        "skills": skills,
        "facts": counters["facts"],
        "summaries": counters["summaries"],
        "fact_limit": str(config.FACT_MAX_ACTIVE),
    }


__all__ = [
    "CONVERSATION_RULES",
    "MEMORY_BLOCK_STEPS",
    "ONLY_ALL",
    "ONLY_ALLOWED",
    "ONLY_LABELS",
    "ONLY_SYSTEM",
    "ONLY_TALK",
    "ROLE_LABELS",
    "SEGMENT_LABELS",
    "SEGMENT_ORDER",
    "SEGMENT_RULES",
    "SLICE_CLASSES",
    "TAB_LABELS",
    "TRACE_TABS",
    "TRACE_TAB_CONTEXT",
    "TRACE_TAB_HISTORY",
    "TRACE_TAB_RULES",
    "SegmentRule",
    "TraceMessage",
    "TraceScale",
    "TraceSegment",
    "TraceSlice",
    "TraceTurn",
    "TraceView",
    "build_stack",
    "build_tool_scale",
    "build_trace_view",
    "build_turn",
]