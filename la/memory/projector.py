"""M1 工作区:投影装配(固定顺序 + 每段独立预算)。

段与预算(具体数字一律读 config,这里不抄第二份 —— 抄一遍的下场是"注释说得对、代码做得不对"):

| 段 | 预算 | 裁剪规则 | 在消息列表里的位置 |
| :--- | :--- | :--- | :--- |
| 系统提示 | config.BUDGET_SYSTEM | 超出即截尾 | **第 1 条**(稳定段) |
| 压缩检查点 | config.BUDGET_CHECKPOINT | 只留最近 N 段,再早的折成一句 | **第 2 条**(历史之前) |
| 运行时上下文 | config.BUDGET_RUNTIME | 超出即截尾 | 末尾(历史之后) |
| 记忆热摘要块 | config.BUDGET_HOT_SUMMARY | 从新到旧淘汰,最多 20 条 | 末尾(历史之后) |
| 对话投影 | config.BUDGET_CONVERSATION | 最旧的成对丢掉,并交给压缩变摘要 | **中间**(只追加) |
| 考古证据块 | config.BUDGET_EVIDENCE | 只有回忆性提问才有内容 | 末尾(历史之后) |
| 工具 schema | config.BUDGET_TOOLS(预留额) | **不裁**(定长 JSON) | 另走 `tools` 字段,**紧跟系统提示之后** |

⚠️ 位置与段号**不是一回事**,这是 D122 故意的:消息列表长这样 ——

    [system:身份+人格+技能目录] [system:压缩检查点] [历史…只追加]
    [system:运行时] [system:记忆] [system:证据] [user:这一句]

为什么:前缀缓存命中的是"两次请求从头开始逐字节相同的那一段"。易变段只要排在历史**前面**,
后面所有历史都跟着变成未命中(实测:易变段在系统提示里 → 跨轮命中 22.5%;挪到末尾 → 93.1%,
见 `scripts/verify_prefix_cache.py`)。与 DSH 同形:它把动态上下文作为"历史里的一条快照"追加,
头部系统节点保持逐字节稳定。

⭐ 压缩检查点(D142)为什么排在**历史之前**。
   前缀缓存命中的是"连续两轮**从头逐字节相同**的那一段"。检查点只在压缩发生时变一次,
   而历史每轮都在长 —— 排在历史之前,它就整段落在缓存里,顺带把能命中的前缀往后推;
   排在历史之后则相反:它的位置跟着历史后移,永远不在"从头开始"的那一段里,
   等于每轮都付一次全价。代价:每压一次,下一条请求的前缀作废一次(约每 1400 轮一次)。

压缩触发线是写死的 120,000(D134);硬上限在**装配时**收紧对话段(不是当场压缩)。
⚠️ 设计稿里那条「超出硬上限 → 强制压缩 → 重投影」今天**没有实现**,
说得到这一点的页面上要如实写(见 docs/记忆模块一致性审核.md §2.3)。

⭐ 这组数 2026-10-14(D121/D122)与 2026-10-16(D130:工具预留额 2500 → 实测 7841)
两次重定标;每一格的来历与代价都写在 config.py 那一段注释里。
理由与实测见 config.py 的注释、docs/历史窗口重定标.md、scripts/analyze_conversation_budget.py。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import config
from ..config import Settings
from . import topics
from .anchors import extract_anchors

SEGMENT_SYSTEM = "system"
#: 压缩检查点:被压掉的那几段在历史之前留的一份索引(见上面那段"为什么排在历史之前")。
SEGMENT_CHECKPOINT = "checkpoint"
SEGMENT_RUNTIME = "runtime"
SEGMENT_MEMORY = "memory"
SEGMENT_CONVERSATION = "conversation"
SEGMENT_EVIDENCE = "evidence"
#: 工具 schema。设计深化 §1.6 把它算在段 1 里,但它由 API 的 tools 参数单独发出、
#: 每轮固定、压缩动不了 —— 所以实现上单列上报,不参与压缩触发(见 docs/决策记录.md D95)。
SEGMENT_TOOLS = "tools"

#: 摘要段至少预留多少 token(给事实段定上限时用)。
#: ⚠️ 这是"给摘要留位置"的软预留,不是硬指标:摘要本身放不下时仍按实际裁。
MIN_SUMMARY_TOKENS = 300


def _coverage_order(rows: list) -> list:
    """把每个领域**第一次出现**的那条提到前面,再按原顺序排其余。

    ⭐ 2026-11-07:块按分数裁预算时,如果高分全来自同一个领域,后面的领域会
       整段消失。先把各领域的"代表"提到前面,至少保证每个领域露一次脸;
       只有一个领域(或全都没标签)时顺序原样不动。
    """
    if not rows:
        return rows
    domains = {
        topics.primary_domain(getattr(row, "tags", None) or []) for row in rows
    }
    domains.discard("")
    if len(domains) < 2:
        return rows
    head: list = []
    tail: list = []
    seen: set[str] = set()
    for row in rows:
        domain = topics.primary_domain(getattr(row, "tags", None) or [])
        if domain and domain not in seen:
            seen.add(domain)
            head.append(row)
        else:
            tail.append(row)
    return head + tail


def clip_to_budget(text: str, budget_tokens: int) -> str:
    """按 4 字符/token 的估算裁剪文本。"""
    if estimate_le(text, budget_tokens):
        return text
    max_chars = max((budget_tokens - 4) * 4, 0)
    return text[:max_chars]


def estimate_le(text: str, budget_tokens: int) -> bool:
    return config.estimate_tokens(text) <= budget_tokens


@dataclass
class Projection:
    messages: list[dict[str, str]] = field(default_factory=list)
    #: 每条消息属于哪一段,与 messages **一一对应**。⭐ 轨迹页(D118)靠它把"第 2 条"
    #: 标成"记忆段",而不是在页面上猜顺序 —— 页面说的必须就是装配器做的。
    message_segments: list[str] = field(default_factory=list)
    segments: dict[str, int] = field(default_factory=dict)
    #: 投影文本合计(不含工具 schema)—— 硬上限说的就是它(budget_sum)
    total_tokens: int = 0
    #: 工具 schema 的估算值(单列上报,压缩动不了;见 D97)
    tools_tokens: int = 0
    #: 这一轮**对话段实际拿到的预算**(被硬上限收紧过就是收紧后的值)。
    #: ⚠️ 与 BUDGET_CONVERSATION 不是一回事:轨迹页(D118)要说"被压到多少",
    #:    拿默认值 4000 去比会说假话。
    conversation_budget: int = 0
    #: ⭐ D134:这一轮给模型的**历史上下文**(不含固定前缀、不含最末那句 user)。
    #:    压缩触发与"保留 20%"都按它算 —— 用户口径就是这一层。
    retained_tokens: int = 0
    dropped_turns: int = 0
    needs_compaction: bool = False
    #: 本轮是否因为硬上限又砍过对话段
    capped: bool = False
    #: ⭐ D122 的记分牌:这一轮里**下一轮还能原样重放**的那一段(估算)= 稳定系统提示 +
    #: 历史里除"最后那句 user 与末尾易变段"之外的部分。它越大,下一轮命中的前缀越长。
    #: (下一轮的第一条 user 就是这一轮的 u_N,而它在这边排在第 4 段之后 —— 位置不再对应,
    #:  所以从 u_N 起就分叉了。这不是缺陷:分叉点已经推到历史的**最末尾**。)
    cache_prefix_tokens: int = 0

    @property
    def variable_tokens(self) -> int:
        return self.total_tokens

    @property
    def grand_total(self) -> int:
        """真实每轮输入的量级(投影文本 + 工具 schema)。"""
        return self.total_tokens + self.tools_tokens

    def as_report(self) -> str:
        parts = [k + "=" + str(v) for k, v in self.segments.items()]
        return (
            " / ".join(parts)
            + " -> 投影 " + str(self.total_tokens)
            + " + 工具 schema " + str(self.tools_tokens)
            + " = " + str(self.grand_total)
        )


class Projector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build(
        self,
        *,
        system_text: str,
        runtime_text: str,
        memory_text: str,
        history: list[dict[str, str]],
        evidence_text: str = "",
        tools_tokens: int = 0,
        checkpoint_text: str = "",
    ) -> Projection:
        segments: dict[str, int] = {}

        system_clipped = clip_to_budget(system_text, config.BUDGET_SYSTEM)
        segments[SEGMENT_SYSTEM] = config.estimate_tokens(system_clipped)

        # ⭐ 压缩检查点(D142):排在系统提示**之后、历史之前** —— 位置是算过的,
        #    不是排版偏好(见模块注释"为什么排在历史之前")。
        checkpoint_clipped = (
            clip_to_budget(checkpoint_text, config.BUDGET_CHECKPOINT) if checkpoint_text else ""
        )
        segments[SEGMENT_CHECKPOINT] = (
            config.estimate_tokens(checkpoint_clipped) if checkpoint_clipped else 0
        )

        runtime_clipped = clip_to_budget(runtime_text, config.BUDGET_RUNTIME)
        segments[SEGMENT_RUNTIME] = config.estimate_tokens(runtime_clipped)

        memory_clipped = clip_to_budget(memory_text, config.BUDGET_HOT_SUMMARY)
        segments[SEGMENT_MEMORY] = config.estimate_tokens(memory_clipped)

        evidence_clipped = clip_to_budget(evidence_text, config.BUDGET_EVIDENCE) if evidence_text else ""
        segments[SEGMENT_EVIDENCE] = config.estimate_tokens(evidence_clipped) if evidence_clipped else 0

        segments[SEGMENT_TOOLS] = max(int(tools_tokens), 0)

        # ⚠️ 硬上限:固定四段先占位置,剩下的才给对话段 —— 只砍对话段(唯一可砍的),
        #    工具 schema 单列、不参与。
        #
        #    ⭐ 2026-10-16(D133)补了一刀:这段的预算**必须再扣掉工具预留额**。
        #    原因:BUDGET_HARD_MAX 的定义是「单轮上限 − 工具预留 − 轮内留量」——
        #    它描述的是**整个投影**能用多少,而不是"投影 + 工具"还能加多少。
        #    原来只按 BUDGET_CONVERSATION 比,于是投影自己就能长到 128,000,
        #    加上工具预留 7,841 与固定段,实际单轮输入会冲到 ~14.3 万,**把硬上限
        #    撑破 1/3**(工具那部分不在这本账里,所以账面看不出来)。
        #    修完:投影总量恒 <= BUDGET_HARD_MAX(由构造保证,不是靠运气)。
        fixed = (
            segments[SEGMENT_SYSTEM] + segments[SEGMENT_CHECKPOINT]
            + segments[SEGMENT_RUNTIME] + segments[SEGMENT_MEMORY]
            + segments[SEGMENT_EVIDENCE]
        )
        # ⭐ D134(用户 2026-10-16 拍板):历史上下文的**硬闸**改成
        #      触发线(120,000)+ 轮内留量(60,000)。
        #    为什么要有这 6 万的留量:用户要求"轮内多次调工具时超了也不压,
        #    等这一轮做完再压" —— 而 working 列表只在末尾追加。对话段若正好卡在
        #    12 万,历史一到 12 万就会被 _fit_conversation **静默丢掉最旧的消息**
        #    (既不产摘要、也不进记忆)—— 那不是"等做完再压",那是**当场丢数据**。
        #    实际算式是两者取更紧的那个:
        #      ① 硬上限 − 工具预留 − 固定段            = 141,118(整套单轮预算的边界)
        #      ② (触发线 + 轮内留量) − 固定段           = 173,300(只保对话段自己不越界)
        #    ⇒ 真正生效的是 ①,也就是说:**从触发线到硬上限,留给轮内的生长空间约 2.1 万**
        #      (实测一轮 20 次工具涨约 1,800;40 次 + 思维链量级在 4~6 万 —— 极端轮次仍可能
        #       被丢最旧的消息,那时该抬的是 config.BUDGET_TURN_WORKING,见 D134)。
        headroom_in_turn = config.BUDGET_COMPACT_TRIGGER + config.BUDGET_IN_TURN_HEADROOM
        conversation_budget = min(
            config.BUDGET_CONVERSATION,
            max(config.BUDGET_HARD_MAX - config.BUDGET_TOOLS - fixed, 0),
            max(headroom_in_turn - fixed, 0),
        )
        capped = conversation_budget < config.BUDGET_CONVERSATION
        kept, dropped = self._fit_conversation(history, conversation_budget)
        segments[SEGMENT_CONVERSATION] = config.estimate_messages_tokens(kept)

        # ⭐ D122:系统提示里**只留稳定段**,每轮都变的三段(运行时 / 记忆 / 证据)推到
        #    消息列表**末尾** —— 排在历史之后、这一句 user 之前。
        #    为什么:前缀缓存命中的是"两次请求从头逐字节相同的那一段";易变段只要排在
        #    历史**前面**,后面几千 token 的历史就一起变成未命中(实测 22.5% → 93.1%,
        #    见 scripts/verify_prefix_cache.py)。与 DSH 同形:动态上下文作为历史里的快照
        #    追加,头部的系统节点保持逐字节稳定。
        #    ⚠️ 这一句 user 必须留在**最后** —— 模型才知道现在该回谁。
        head, last_user = self._split_close(kept)
        messages: list[dict[str, str]] = [{"role": "system", "content": system_clipped}]
        labels: list[str] = [SEGMENT_SYSTEM]
        if checkpoint_clipped:
            messages.append({"role": "system", "content": checkpoint_clipped})
            labels.append(SEGMENT_CHECKPOINT)
        messages.extend(head)
        labels.extend([SEGMENT_CONVERSATION] * len(head))
        for key, text in (
            (SEGMENT_RUNTIME, runtime_clipped),
            (SEGMENT_MEMORY, memory_clipped),
            (SEGMENT_EVIDENCE, evidence_clipped),
        ):
            if text:
                messages.append({"role": "system", "content": text})
                labels.append(key)
        if last_user is not None:
            messages.append(last_user)
            labels.append(SEGMENT_CONVERSATION)
        #: 这一轮给模型的**历史上下文**(不含固定前缀、不含最末那句 user —— 用户说的
        #: "历史上下文"就是这个口径)。压缩后要保留的比例就按它算。
        retained_tokens = config.estimate_messages_tokens(
            kept[:-1] if last_user is not None else kept
        )

        total = sum(value for key, value in segments.items() if key != SEGMENT_TOOLS)
        cache_prefix = (
            segments[SEGMENT_SYSTEM] + segments[SEGMENT_CHECKPOINT]
            + config.estimate_messages_tokens(head)
        )
        return Projection(
            messages=messages,
            message_segments=labels,
            segments=segments,
            total_tokens=total,
            tools_tokens=segments[SEGMENT_TOOLS],
            conversation_budget=conversation_budget,
            retained_tokens=retained_tokens,
            dropped_turns=dropped,
            # 压缩触发只看**可压缩的那部分**:工具 schema 每轮都一样,压不动它。
            # ⭐ D134:阈值是**写死的 120,000**(不再从总锁推),见 config 那一段注释。
            needs_compaction=retained_tokens >= config.BUDGET_COMPACT_TRIGGER,
            capped=capped,
            cache_prefix_tokens=cache_prefix,
        )

    @staticmethod
    def _split_close(
        history: list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], dict[str, str] | None]:
        """把"这一轮最新那句 user"从历史里摘出来 —— 它要排在易变段**之后**。

        ⚠️ 只在这最后一条真的是 user 时才摘(respond() 先落 user/message 再装配,真实
           路径上总是它);拿不准就原样不动 —— 宁可顺序照旧,也不能把消息弄丢。
        """
        if history and str(history[-1].get("role") or "") == "user":
            return list(history[:-1]), dict(history[-1])
        return list(history), None

    def _fit_conversation(
        self, history: list[dict[str, str]], budget: int | None = None
    ) -> tuple[list[dict[str, str]], int]:
        limit = config.BUDGET_CONVERSATION if budget is None else int(budget)
        kept = list(history)
        dropped = 0
        while kept and config.estimate_messages_tokens(kept) > limit:
            if len(kept) >= 2:
                del kept[:2]
                dropped += 2
            else:
                kept.clear()
                dropped += 1
        return kept, dropped

    @staticmethod
    def _fit_retained(history: list[dict[str, str]], budget: int) -> list[dict[str, str]]:
        """按**保留量**裁历史(成对丢最旧的)—— D134 的"压缩后保留 20%"。

        ⚠️ 与 _fit_conversation 的区别:那个的 limit 是"上限",这个是**目标保留量**。
        两者都成对丢 —— 留下孤立的 assistant 消息会让模型看到"它自己说过一句没人问的话"。
        """
        limit = max(int(budget), 0)
        kept = list(history)
        while kept and config.estimate_messages_tokens(kept) > limit:
            if len(kept) >= 2:
                del kept[:2]
            else:
                kept.clear()
        return kept


def build_memory_block(
    *,
    facts: list,
    summaries: list,
    status: str,
    now_text: str,
    budget_tokens: int | None = None,
    briefing: str = "",
    goals: list | None = None,
    pending_review: int = 0,
) -> str:
    """把检索结果渲染成注入用的记忆块(分段预算、超预算先丢低分/多出来的领域)。

    briefing 与 goals 是**不可淘汰**的:它们要么是今天到点的推送,要么是长期目标状态,
    丢了就等于"助手忘了自己在跟什么事"。

    ⭐ 2026-11-07:块内分三段记账 —— 固定段(status/briefing/goals/pending)先量,
       事实最多 FACT_BUDGET_TOKENS,摘要用剩下的;摘要按分数放,先保证每个有命中的
       领域露一次脸(见 _coverage_order),再按分数补。以前只有一个总预算,事实
       先来先占,摘要会被整段挤掉。

    ⭐ 段序按「**变得慢的放前面**」(2026-10-16 用户点名"事实不会变动、记忆可能会变"):

        <memory> → <facts>(永久事实,一轮里完全不动)
                 → <goals>(长期目标,推进时才动)
                 → <memory-status> + briefing(这两样**每轮都可能变**)
                 → <hot-memories>(检索命中的摘要,每轮不同)
                 → <memory-guidance>(只在 miss 时出现)
                 → <pending-review>(有候选在等人放行时才出现,见 human_release)

    ⚠️ 为什么值得这么排(而不是"反正每轮都要重发"):同一次应答里模型可能被请求好几次
        (调工具后把结果追加进 messages 再发一次),而那几次请求的**记忆块是同一份** ——
        块本身从头逐字节相同,后面的请求就能把它整段命中;可变的排前面,等于第 2 个 token
        就把整段作废(前缀缓存是从头开始匹配的)。
    """
    budget = budget_tokens or config.BUDGET_HOT_SUMMARY

    def render(fact_items: list, summary_items: list) -> str:
        lines: list[str] = ["<memory>"]
        if fact_items:
            lines.append("<facts>")
            for fact in fact_items:
                # ⚠️ 标了敏感的事实**不进对话**(设计深化 §3.5/§3.8/§4.2):
                #    只留一个占位与编号 —— 编号是给 memory_correct 用的,内容不是。
                #    要看内容请打开记忆页(/memory 的 L3)或数据文件。
                if getattr(fact, "confidential", False):
                    lines.append(
                        "- " + config.SECRET_PLACEHOLDER
                        + "(#" + str(getattr(fact, "id", "?")) + " 已标记为敏感)"
                    )
                else:
                    lines.append("- " + str(fact.content))
            lines.append("</facts>")
        if goals:
            lines.append("<goals>")
            for goal in goals[: config.HOT_SUMMARY_MAX_ITEMS]:
                lines.append("- " + goal.render())
            lines.append("</goals>")
        # -- 从这里往下,每一轮都可能不一样(状态 / 今日推送 / 命中的摘要)---------
        lines.append("<memory-status>" + status + "</memory-status>")
        if briefing:
            lines.append(briefing)
        if summary_items:
            lines.append("<hot-memories>")
            for row in summary_items:
                text = " ".join(str(row.text).split())
                # ⭐ 2026-11-07:把领域标签一起渲染出来 —— 模型才能回答"这条是健康的还是账目的",
                #    也才知道 query_summary_pool 的 tags 该传什么。
                domains = [
                    topics.normalize_tag(tag)
                    for tag in (getattr(row, "tags", None) or [])
                    if topics.normalize_tag(tag) in topics.DOMAIN_TAGS
                ]
                prefix = ("[" + ",".join(domains) + "] ") if domains else ""
                lines.append(
                    "- " + prefix + "["
                    + str(row.start_time)[:16]
                    + " — "
                    + str(row.end_time)[:16]
                    + "] "
                    + text[: config.HOT_SUMMARY_HEAD_CHARS]
                )
            lines.append("</hot-memories>")
        if status == "miss":
            lines.append(
                "<memory-guidance>本次检索未命中任何长期记忆。"
                "如果用户问的是过去发生过的事,直接说记忆里没有,不要用听起来合理的猜测填空。"
                "</memory-guidance>"
            )
        # ⭐ 2026-10-25:有人在等放行的候选,模型必须**知道** —— 否则微信里用户说"通过吧",
        #    它只会再问一遍"要记住这条吗"(用户实际踩到的那一段)。
        #    ⚠️ 放在块的最末尾:这一段每轮都可能变,排在前面会把整块的前缀缓存作废。
        if pending_review:
            lines.append(
                "<pending-review>还有 " + str(pending_review) + " 条记忆候选在等用户放行"
                "(准入闸判不准,现在还没生效)。他提到「通过 / 放行 / 记好了吗 / 刚才那条」时,"
                "调用 memory_review(action=\"approve\") 放行(先 list 也行);"
                "他没有提就不要主动刷这件事,也不要再问一遍「要记住这条吗」——"
                "更不要自行放行。</pending-review>"
            )
        lines.append("</memory>")
        return "\n".join(lines)

    # ⭐ 2026-11-07:块内**分段记账** —— 固定段、事实、摘要各有自己的额度。
    #    以前只有一个 1500 的总预算,facts 先来先占(最多 20 条、每轮无条件注入),
    #    摘要只能捡剩下的;事实一多,摘要就被整段挤掉。三层现在分开算:
    #      ① 固定段(status/briefing/goals/pending)不可淘汰,先量出来;
    #      ② 事实最多 FACT_BUDGET_TOKENS(默认 600),且给摘要留 MIN_SUMMARY_TOKENS;
    #      ③ 摘要用剩下的额度,按分数从高到低放;放不下就停,不硬塞。
    base_tokens = config.estimate_tokens(render([], []))
    available = max(budget - base_tokens, 0)
    summary_reserve = MIN_SUMMARY_TOKENS if summaries else 0
    fact_budget = min(config.FACT_BUDGET_TOKENS, max(available - summary_reserve, 0))

    kept_facts: list = []
    fact_tokens = 0
    if fact_budget > 0:
        for fact in list(facts)[: config.HOT_SUMMARY_MAX_ITEMS]:
            cost = config.estimate_tokens(render([fact], [])) - base_tokens
            if fact_tokens + cost > fact_budget:
                break
            kept_facts.append(fact)
            fact_tokens += cost
            if fact_tokens >= fact_budget:
                break

    # 摘要:先让每个领域"露一次脸",再按分数补 —— 裁预算时不会整段丢掉某个领域。
    kept_summaries: list = []
    for row in _coverage_order(list(summaries))[: config.HOT_SUMMARY_MAX_ITEMS]:
        candidate = kept_summaries + [row]
        if config.estimate_tokens(render(kept_facts, candidate)) > budget:
            break
        kept_summaries = candidate

    return render(kept_facts, kept_summaries)


# -- 压缩检查点(D142)--------------------------------------------------------
_SECTION_ITEM_RE = re.compile(r"^-\s*事项\s*[:：]\s*(.*)$", re.M)
_SECTION_HEAD_RE = re.compile(r"^-\s*(时段|事项|事实|偏好|待办|锚点|其他)\s*[:：]\s*", re.M)


def _summary_preview(text: str, limit: int = 34) -> str:
    """摘要里"讲了什么"的一行预览:优先取「事项」那一节,取不到就退回正文开头。"""
    body = ""
    match = _SECTION_ITEM_RE.search(str(text or ""))
    if match:
        body = match.group(1)
    if not body.strip() or body.strip() in ("(none)", "[]"):
        body = _SECTION_HEAD_RE.sub("", re.sub(r"</?compacted-summary>", " ", str(text or "")))
    flat = " ".join(body.replace(";", "、").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _span(start: str, end: str) -> str:
    """'2026-10-14 09:12→11:40';跨天就把日期也写全(别让人以为同一天)。"""
    start, end = str(start or ""), str(end or "")
    if not start and not end:
        return "(时间未知)"
    head = start[:16].replace("T", " ")
    if start[:10] == end[:10]:
        return head + "→" + end[11:16]
    return head + " → " + end[:16].replace("T", " ")


def _render_checkpoint(rows: list, hidden: int) -> str:
    lines = [
        "<compacted-context>",
        "下面这几段对话已经压成摘要(原文仍在档案里;要细节就取回来,不要凭印象补):",
    ]
    for row in rows:
        lines.append(
            "- " + _span(row.start_time, row.end_time)
            + " [#" + str(row.id) + "] " + _summary_preview(row.text)
        )
    if hidden > 0:
        lines.append(
            "(更早还有 " + str(hidden) + " 段已折叠;"
            "按时间取原文用 query_timestamp_range,查概要先用 query_summary_pool)"
        )
    lines.append("</compacted-context>")
    return "\n".join(lines)


def build_checkpoint_block(summaries: list, *, budget_tokens: int | None = None) -> str:
    """渲染压缩检查点(D142)。没有摘要就返回空串 —— 空段不进消息列表,一个字节都不占。

    ⚠️ 只写"压过哪几段、摘要编号、各讲了什么",**不复制摘要正文**:
       正文要按需检索(D10),这里放一份就等于把摘要池整个搬进每轮请求。
    ⚠️ 顺序是**时间升序**、超预算时从**最早**的那一段开始折叠:
       最近压的那几段最可能被问到,先保住它们。
    """
    items = list(summaries)
    if not items:
        return ""
    budget = budget_tokens or config.BUDGET_CHECKPOINT
    rows = items[-config.CHECKPOINT_MAX_ITEMS:]
    while True:
        text = _render_checkpoint(rows, len(items) - len(rows))
        if not rows or config.estimate_tokens(text) <= budget:
            return text
        rows = rows[1:]


def block_anchors(text: str) -> list[str]:
    return extract_anchors(text)
