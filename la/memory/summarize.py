"""摘要生成:LLM 优先,抽取式兜底。

摘要模板固定分节(设计深化 §1.3),机器可解析;每个分节必须存在,空节写 (none)。
代码侧校验(不通过就回落到抽取式):
    ① 必须被 <compacted-summary> 包裹;
    ② 必须含全部必填分节;
    ③ **摘要必须比被遮蔽的区间更短** —— 否则压缩毫无意义;
    ④ 摘要不能超过 MAX_SUMMARY_CHARS —— 超过后在热记忆/检索里也只看得见头部。

⚠️ 2026-11-21 真机事故:模型输出的是
      时段: ...
      事项: ...
   没有 "- " 前缀;而校验器只认 "- 时段:",于是**每一条** LLM 摘要都被判
   "缺少分节",全部回落到抽取式。抽取式又把所有锚点塞进去,产生 4,497 字的
   摘要 —— 长摘要不是模型写的,是兜底拼的。现在校验前先 normalize_summary。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from ..config import Settings, estimate_tokens
from .anchors import extract_anchors
from .archive import Event

SECTIONS: tuple[str, ...] = ("时段", "事项", "事实", "偏好", "待办", "锚点", "其他")
SECTION_HEAD = "- "
MAX_ITEMS_LINE = 4
SNIPPET_CHARS = 32
#: 摘要硬上限。摘要会被热记忆/检索消费;超过这个量级后,后面的正文在
#: 热记忆头部上限(当前 500 字)与 tool 4,000 字符截断下基本看不到。
#: 2026-11-21 真机 #23 是 4,497 字符(抽取式兜底把 447 个锚点全塞进去),
#: 一次查询就超过 4,000 字符上限 —— 这里给兜底与 LLM 输出同一条硬线。
MAX_SUMMARY_CHARS = 1_200
#: 锚点最多保留多少个。抽取式兜底以前是“全量”,是长摘要的第一大来源。
MAX_ANCHORS = 40

SUMMARY_SYSTEM_PROMPT = """你是记忆压缩器。把一段对话事件压缩成结构化摘要。

必须严格遵守:
1. 用 <compacted-summary> 与 </compacted-summary> 包裹全部输出;
2. 必须逐行写成下面 7 个分节,**每行以 "- " 开头**,不要用标题、编号或加粗:
   - 时段: 开始 — 结束
   - 事项: ...
   - 事实: ...
   - 偏好: ...
   - 待办: ...
   - 锚点: [日期, 金额, 人名, 项目名]
   - 其他: ...
3. 空的分节写 (none),不要省略分节;
4. 锚点只列最关键的日期/金额/人名/项目名,最多 20 个,不要罗列所有候选;
5. 总长度控制在 1200 字以内;压缩后的内容必须**明显短于**原文;
6. 只写出现过的信息,不要推测、不要补充背景。"""


@dataclass
class SummaryDraft:
    text: str
    anchors: list[str]
    source: str  # 'llm' | 'extractive'
    valid: bool = True
    problems: tuple[str, ...] = ()


def strip_summary(text: str) -> str:
    match = re.search(r"<compacted-summary>(.*?)</compacted-summary>", text, re.S)
    return match.group(1).strip() if match else text.strip()


#: 模型常见的分节写法:时段:、- 时段:、**时段**:、## 时段、1. 时段:。
#: ⚠️ 2026-11-21 真机:模型一直输出 “时段:”(没有 “- ”),而校验器只认
#:    “- 时段:”,于是每一条 LLM 摘要都被判“缺少分节”,全部回落到抽取式兜底。
#:    这是摘要变成 4,497 字锚点清单的根因。校验前先归一化,不要靠模型自觉。
_SECTION_LINE_RE = re.compile(
    r"(?m)^[ \t]*(?:[-*•][ \t]*|#{1,6}[ \t]*|[0-9]+[.、)][ \t]*)?"
    r"(?:\*\*)?[ \t]*(?P<name>" + "|".join(SECTIONS) + r")[ \t]*(?:\*\*)?"
    r"[ \t]*[:：][ \t]*"
)


def normalize_summary(text: str) -> str:
    """把模型输出归一化成可校验的固定格式。

    * 缺 <compacted-summary> 外壳就补上(只要正文里有分节,就不该整条丢掉);
    * 分节行统一成 “- 分节:”(允许无项目符号、加粗、标题、编号)。
    """
    raw = str(text or "").strip()
    if not raw:
        return raw
    if "<compacted-summary>" not in raw:
        raw = "<compacted-summary>\n" + strip_summary(raw) + "\n</compacted-summary>"
    elif "</compacted-summary>" not in raw:
        raw = raw + "\n</compacted-summary>"
    lines: list[str] = []
    for line in raw.splitlines():
        lines.append(_SECTION_LINE_RE.sub(lambda m: "- " + m.group("name") + ":", line))
    return "\n".join(lines).strip()


def validate_summary(text: str, source_text: str) -> tuple[bool, tuple[str, ...]]:
    problems: list[str] = []
    normalized = normalize_summary(text)
    if "<compacted-summary>" not in normalized or "</compacted-summary>" not in normalized:
        problems.append("缺少 <compacted-summary> 包裹")
    for section in SECTIONS:
        if not re.search(
            r"(?m)^[ \t]*-[ \t]*" + re.escape(section) + r"[ \t]*[:：]", normalized
        ):
            problems.append("缺少分节: " + section)
    if len(normalized) > MAX_SUMMARY_CHARS:
        problems.append("摘要超过 " + str(MAX_SUMMARY_CHARS) + " 字")
    if estimate_tokens(normalized) >= estimate_tokens(source_text):
        problems.append("摘要不比原文短")
    return (not problems), tuple(problems)


def render_events(events: Sequence[Event]) -> str:
    """把事件渲染成给压缩器看的文本。"""
    lines: list[str] = []
    for event in events:
        if event.type == "user/message":
            lines.append("[用户] " + str(event.data.get("text") or ""))
        elif event.type == "assistant/message":
            lines.append("[助手] " + str(event.data.get("text") or ""))
        elif event.type == "memory/note":
            lines.append("[记忆标记] " + str(event.data.get("content") or ""))
        else:
            continue
    return "\n".join(lines) if lines else "(无对话内容)"


def _snippets(
    texts: Sequence[str],
    *,
    keywords: Sequence[str] = (),
    limit: int = MAX_ITEMS_LINE,
    width: int = SNIPPET_CHARS,
) -> list[str]:
    picked: list[str] = []
    for text in texts:
        cleaned = " ".join(str(text).split())
        if keywords and not any(keyword in cleaned for keyword in keywords):
            continue
        if not cleaned:
            continue
        picked.append(cleaned[:width] + ("…" if len(cleaned) > width else ""))
        if len(picked) >= limit:
            break
    return picked


def _section(name: str, items: Sequence[str]) -> str:
    if not items:
        return SECTION_HEAD + name + ":(none)"
    if name == "锚点":
        return SECTION_HEAD + name + ":[" + ", ".join(items) + "]"
    return SECTION_HEAD + name + ":" + ";".join(items)


def _extractive_text(
    *,
    user_texts: Sequence[str],
    assistant_texts: Sequence[str],
    note_texts: Sequence[str],
    anchors: Sequence[str],
    start_time: str,
    end_time: str,
    items_limit: int,
    snippet_width: int,
    anchor_limit: int,
) -> str:
    lines = [
        "<compacted-summary>",
        _section("时段", [start_time + " — " + end_time]),
        _section("事项", _snippets(user_texts, limit=items_limit, width=snippet_width) or ["(none)"]),
        _section("事实", _snippets(note_texts, limit=items_limit, width=snippet_width) or ["(none)"]),
        _section(
            "偏好",
            _snippets(
                user_texts,
                keywords=("喜欢", "不喜欢", "习惯", "不要", "不吃"),
                limit=items_limit,
                width=snippet_width,
            ) or ["(none)"],
        ),
        _section(
            "待办",
            _snippets(
                user_texts,
                keywords=("要", "需要", "记得", "别忘", "截止", "之前"),
                limit=items_limit,
                width=snippet_width,
            ) or ["(none)"],
        ),
        _section("锚点", list(anchors)[:anchor_limit]),
        _section("其他", ["(none)"]),
        "</compacted-summary>",
    ]
    return "\n".join(lines)


def extractive_summary(
    events: Sequence[Event], *, start_time: str, end_time: str
) -> SummaryDraft:
    """确定性抽取式摘要 —— 无模型也能压缩,同时是 LLM 失败时的兜底。

    ⚠️ 兜底必须有上限:以前锚点是“全量”,一个长区间能产生 447 个锚点 /
       4,497 字,热记忆只取头部(当时 300 字)、检索一次就超过 4,000 字符工具上限。
       现在先按正常宽度生成;超过 MAX_SUMMARY_CHARS 就逐级缩短。
    """
    user_texts = [
        str(e.data.get("text") or "") for e in events if e.type == "user/message"
    ]
    note_texts = [
        str(e.data.get("content") or "") for e in events if e.type == "memory/note"
    ]
    # ⚠️ 锚点必须覆盖**区间内**出现的日期/金额/人名/项目名(设计深化 §1.3),
    #    所以助手说过的话也要算进来 —— 早先只抽用户侧,助手复述过的数字会漏。
    assistant_texts = [
        str(e.data.get("text") or "") for e in events if e.type == "assistant/message"
    ]
    everything = "\n".join([*user_texts, *assistant_texts, *note_texts])
    anchors_all = extract_anchors(everything)

    text = ""
    anchors: list[str] = []
    # (每条上限, 每条宽度, 锚点上限):超长时逐级收紧,先保结构,再谈细节。
    for items_limit, snippet_width, anchor_limit in (
        (MAX_ITEMS_LINE, SNIPPET_CHARS, MAX_ANCHORS),
        (2, 24, 20),
        (1, 20, 10),
    ):
        anchors = anchors_all[:anchor_limit]
        text = _extractive_text(
            user_texts=user_texts,
            assistant_texts=assistant_texts,
            note_texts=note_texts,
            anchors=anchors,
            start_time=start_time,
            end_time=end_time,
            items_limit=items_limit,
            snippet_width=snippet_width,
            anchor_limit=anchor_limit,
        )
        if len(text) <= MAX_SUMMARY_CHARS:
            break

    valid = len(text) <= MAX_SUMMARY_CHARS
    if not valid:
        # 极端情况(单条 snippet 本身就很长):保留外壳,截正文并标记不完整。
        body = strip_summary(text)
        room = max(0, MAX_SUMMARY_CHARS - len("<compacted-summary>\n\n</compacted-summary>") - 1)
        text = (
            "<compacted-summary>\n"
            + body[:room].rstrip()
            + "…\n</compacted-summary>"
        )
    return SummaryDraft(
        text=text,
        anchors=anchors,
        source="extractive",
        valid=valid,
        problems=() if valid else ("抽取式摘要超长,已截断",),
    )


class ExtractiveSummarizer:
    async def summarize(
        self, events: Sequence[Event], *, start_time: str, end_time: str
    ) -> SummaryDraft:
        return extractive_summary(events, start_time=start_time, end_time=end_time)


class LLMSummarizer:
    """用模型生成摘要;任何一步不达标就回落到抽取式(绝不因为压缩失败而丢记忆)。"""

    def __init__(self, client, settings: Settings, fallback: ExtractiveSummarizer | None = None) -> None:
        self.client = client
        self.settings = settings
        self.fallback = fallback or ExtractiveSummarizer()

    async def summarize(
        self, events: Sequence[Event], *, start_time: str, end_time: str
    ) -> SummaryDraft:
        source_text = render_events(events)
        prompt = (
            "把下面这段对话压缩成结构化摘要。\n"
            "时段固定写作:" + start_time + " — " + end_time + "\n\n"
            "<events>\n" + source_text + "\n</events>"
        )
        problems: tuple[str, ...] = ()
        try:
            result = await self.client.chat(
                [
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                # 摘要只要一段结构化文本,不需要思考;显式关掉,避免思考吃掉输出额度
                # (与事实准入裁决同一条教训,D170)。
                reasoning_effort="",
                max_tokens=800,
            )
            text = normalize_summary(result.text)
            if getattr(result, "truncated", False):
                problems = ("模型输出被截断",)
            else:
                ok, found = validate_summary(text, source_text)
                if ok:
                    return SummaryDraft(
                        text=text,
                        anchors=extract_anchors(text)[:MAX_ANCHORS],
                        source="llm",
                    )
                problems = found
        except Exception:
            return await self.fallback.summarize(
                events, start_time=start_time, end_time=end_time
            )
        fallback = await self.fallback.summarize(
            events, start_time=start_time, end_time=end_time
        )
        fallback.valid = False
        fallback.problems = problems
        return fallback


def build_summarizer(client, settings: Settings):
    """离线假客户端不做压缩(它产生的是无意义回声),直接用抽取式。"""
    if settings.provider == "fake":
        return ExtractiveSummarizer()
    return LLMSummarizer(client, settings)
