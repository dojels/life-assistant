"""自我观察工具(D138):模型可以**按需**看自己的行为账,而不是每轮都被喂。

⭐ 为什么是一个工具、而不是每轮注入:
  * 注入 = 每轮都花 token、且会把"自我评价"混进正常对话的上下文;
  * 工具 = **触发才读**,读到的是摘要,用完就留在本轮 working 列表里(跨轮自动消失)。

⭐ 为什么只暴露一个 scope(behavior):
  行为数据是**事实**(工具失败、压缩次数、回执条数),读它不会污染认知;
  而"读自己的代码/文档"得到的是主人写下的信念,复述出来是回声 —— 见 docs/决策记录.md D138。

⚠️ schema 必须小:它在**每一次请求**的 tools 字段里,而 tools 字段是前缀缓存的一部分。
   加一个工具会改 tools 字段 → 击穿一次缓存,并且要让 config.BUDGET_TOOLS 跟着涨
   (tests/test_m0.py 的"工具预留额必须贴着实测目录"会红)。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from .. import config
from ..base.phases import PHASE_ANSWER
from ..agent.self_view import build_behavior_digest
from .registry import RISK_LOW, ToolSpec

#: 默认窗口(天)。7 天够看出"最近哪里不顺",30 天能看趋势。
DEFAULT_DAYS = 7
MAX_DAYS = 30


def build_insight_tools(
    store: Any,
    *,
    assistant_conn: Any = None,
    catalog: Callable[[], Sequence[str]] | None = None,
    trace_loader: Callable[[], Sequence[dict[str, Any]]] | None = None,
    now: Callable[[], Any] | None = None,
) -> list[ToolSpec]:
    """装配"看自己"的工具。所有依赖都能注入 —— 测试里不碰真文件、不碰真时钟。"""

    def _moment() -> Any:
        return now() if now is not None else config.get_settings().now()

    async def self_inspect(params: dict[str, Any]) -> dict[str, Any]:
        scope = str(params.get("scope") or "behavior").strip().lower()
        if scope != "behavior":
            return {
                "error": "scope 目前只支持 behavior",
                "supported": ["behavior"],
            }
        days = max(1, min(int(params.get("days") or DEFAULT_DAYS), MAX_DAYS))
        moment = _moment()
        names = tuple(catalog() if catalog is not None else ())
        records = list(trace_loader() if trace_loader is not None else [])
        digest = build_behavior_digest(
            store.conn,
            now=moment,
            window_days=days,
            catalog=names,
            trace_records=records,
            assistant_conn=assistant_conn,
        )
        return {
            "scope": "behavior",
            "days": days,
            "as_of": config.format_local(moment),
            "digest": digest,
            "chars": len(digest),
        }

    return [
        ToolSpec(
            name="self_inspect",
            description=(
                "查看**你自己的近况账**:轮次、token、缓存命中、工具调用成败、"
                "压缩与写成条数。⭐ 主人要求你审视/复盘自己(「看下你自己」"
                "「你觉得自己的问题在哪」)时,**先调它拿事实**,再下判断;"
                "不要凭印象或凭记忆里的文档下结论。返回的是事实,判断写在回复里"
                "并说明依据;**不要把对自己的评价写进记忆**。"
            ),
            phase=PHASE_ANSWER,
            risk=RISK_LOW,
            timeout_ms=3000,
            handler=self_inspect,
            output_budget_tokens=500,
            required=(),
            params={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["behavior"],
                        "description": "看哪一面;目前只支持 behavior(行为账)",
                    },
                    "days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_DAYS,
                        "description": "回看多少天,默认 7;要趋势用 30",
                    },
                },
                "additionalProperties": False,
            },
        ),
    ]


__all__ = ["DEFAULT_DAYS", "MAX_DAYS", "build_insight_tools"]
