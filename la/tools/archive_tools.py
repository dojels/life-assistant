"""档案工具:按时间范围 / 全文回查原始事件(Phase 2 的慢路径)。

与摘要池检索的区别(设计深化 §3.3):这两个工具搜的是**原文**,摘要池只搜**摘要**。
两者是快/慢路径,不是替代关系。

⚠️ 已知取舍:v1 的全文检索走 SQL 的 LIKE + 关键词切分,**没有为档案建 FTS5 索引**。
档案量级到十万条事件时应升级为独立 FTS 索引(设计里提到的 dsh-session-query-sqlite 思路)。
"""

from __future__ import annotations

from typing import Any

from ..base.phases import PHASE_ARCH
from ..memory.archive import archive_filename
from ..memory.search import extract_terms
from ..memory.timerange import TimeRange, parse_range
from .registry import RISK_LOW, ToolSpec

DEFAULT_MAX_CHARS = 2000
DEFAULT_SEARCH_LIMIT = 20


def _render_events(events: list[Any], *, max_chars: int) -> tuple[list[dict[str, Any]], bool, str]:
    """把事件渲染成列表,**按总字符预算真正截断**。

    ⚠️ 旧实现只把拼好的文本喂给 clip_head_tail 取一个 bool(2026-10-18 审计),
    返回的 rendered 一条不少 —— `output_budget_tokens` 对 list 不生效,
    500 条 × 500 字能把上下文打爆,而工具还自报 truncated=True。
    """
    rendered: list[dict[str, Any]] = []
    used = 0
    budget = max(int(max_chars), 0)
    truncated = False
    for event in events:
        text = str(event.data.get("text") or event.data.get("content") or "")
        if not text:
            continue
        piece = text[:500]
        # 估算:时间 + 角色 + 文本 + locator 的大致开销
        cost = len(piece) + 80
        if budget and used + cost > budget:
            truncated = True
            break
        rendered.append(
            {
                "time": event.time,
                "seq": event.seq,
                "type": event.type,
                "role": "user" if event.type == "user/message" else "assistant",
                "text": piece,
                # 设计深化 §3.2:data/archive/<sid>.jsonl#seq=123 —— 少了文件名就没法照着去翻
                # 文件名过 archive_filename:会话 id 里的 `:` 不能直接进 Windows 路径(ADS)。
                "locator": "data/archive/" + archive_filename(event.session_id or "?") + "#seq=" + str(event.seq),
            }
        )
        used += cost
    locator = rendered[-1]["locator"] if rendered else ""
    return rendered, truncated, locator


def build_archive_tools(store, *, session_id: str = "") -> list[ToolSpec]:
    """把档案查询绑到具体 store 上,生成工具规格。"""

    async def query_timestamp_range(params: dict[str, Any]) -> dict[str, Any]:
        start = str(params["start"])
        end = str(params["end"])
        keywords = params.get("keywords") or ""
        max_chars = int(params.get("max_chars") or DEFAULT_MAX_CHARS)

        events = store.query_range(
            start, end, limit=500, session_id=session_id or None
        )
        if keywords:
            terms = extract_terms(str(keywords))
            if terms:
                events = [
                    event
                    for event in events
                    if any(
                        term in str(event.data.get("text") or event.data.get("content") or "")
                        for term in terms
                    )
                ]
        rendered, truncated, locator = _render_events(events, max_chars=max_chars)
        return {
            "events": rendered,
            "count": len(rendered),
            "truncated": truncated,
            "full_locator": locator,
        }

    async def query_archive_search(params: dict[str, Any]) -> dict[str, Any]:
        query = str(params["query"])
        limit = int(params.get("limit") or DEFAULT_SEARCH_LIMIT)
        events = store.search_events(
            query, limit=limit, session_id=session_id or None
        )
        rendered, truncated, locator = _render_events(events, max_chars=DEFAULT_MAX_CHARS)
        return {
            "events": rendered,
            "count": len(rendered),
            "truncated": truncated,
            "full_locator": locator,
        }

    return [
        ToolSpec(
            # ⚠️ required= 与 params= 必须成对:只写 required 的话,模型看到的
            #    是一个没有任何属性的对象,只能从描述里猜参数名(见 D90)。
            name="query_timestamp_range",
            description=(
                "按时间范围回查原始对话事件;已知大致时间时优先用它。"
                "⭐ 先给宽窗口再收窄(一次调用只覆盖一个区间);"
                "有明确关键词就带上 —— 加上 keywords 能把「那段时间说过的话」缩到相关的那几句。"
            ),
            phase=PHASE_ARCH,
            risk=RISK_LOW,
            timeout_ms=5000,
            handler=query_timestamp_range,
            output_budget_tokens=800,
            required=("start", "end"),
            params={
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": "区间开始,ISO8601 带时区,例如 2026-10-01T00:00:00+08:00",
                    },
                    "end": {
                        "type": "string",
                        "description": "区间结束,ISO8601 带时区(不含)",
                    },
                    "keywords": {
                        "type": "string",
                        "description": "可选:只留下含这些词的原文(空格或逗号分隔),例如 装修 预算",
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 200,
                        "maximum": 8000,
                        "description": "返回正文的字符上限,默认 2000;条目多时先要个总数再决定要不要放大",
                    },
                },
                "required": ["start", "end"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="query_archive_search",
            description=(
                "在全部档案原文里做关键词检索,不限定时间范围。"
                "⭐ 和 query_summary_pool 的分工:那个搜**摘要**(快),这个搜**原文**(准)。"
                "摘要没命中、或者需要原话/具体数字时用它。"
            ),
            phase=PHASE_ARCH,
            risk=RISK_LOW,
            timeout_ms=5000,
            handler=query_archive_search,
            output_budget_tokens=800,
            required=("query",),
            params={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "关键词(用户原话里的词就行);中文两字词走 LIKE 降级,所以别写太长的句子",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "最多带回几条,默认 20",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
    ]


def range_from_query(text: str, now) -> TimeRange | None:
    return parse_range(text, now)
