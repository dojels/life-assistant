"""热度与遗忘(设计深化 §1.4 公式终版)。

纯函数,读取时计算,无副作用 —— 便于单元测试与"改算法不用迁移数据"。
"""

from __future__ import annotations
from ..base.clock import parse_iso as _clock_parse_iso

import math
from datetime import datetime

# 半衰期:闲聊 2 天、工作项目/健身 15 天、永久 0(不衰减)
LAMBDA_BY_CATEGORY: dict[str, float] = {
    "temporary": math.log(2) / 2.0,
    "periodic": math.log(2) / 15.0,
    "permanent": 0.0,
}

DEFAULT_CATEGORY = "periodic"

# 升温规则(设计深化 §1.4):被动提及 +1、用户主动检索 +5、考古成功 base_strength +1
VISITS_PASSIVE = 1
VISITS_USER_INITIATED = 5
BASE_STRENGTH_ON_ARCHAEOLOGY = 1.0


def decay_lambda(category: str | None) -> float:
    return LAMBDA_BY_CATEGORY.get(category or DEFAULT_CATEGORY, LAMBDA_BY_CATEGORY[DEFAULT_CATEGORY])


def heat(
    base_strength: float,
    visits: int,
    days_since_last_access: float,
    *,
    category: str | None = DEFAULT_CATEGORY,
    lam: float | None = None,
) -> float:
    """(base_strength + visits) ** 0.5 * exp(-lambda * days)。"""
    strength = max(float(base_strength), 0.0) + max(int(visits), 0)
    rate = decay_lambda(category) if lam is None else lam
    days = max(float(days_since_last_access), 0.0)
    return math.sqrt(strength) * math.exp(-rate * days)


#: 用户显式要求长期保留的事实类型 —— 它们所在的桶按 permanent 处理
PERMANENT_FACT_TYPES: tuple[str, ...] = ("identity", "decision")


def classify_bucket(events) -> str:
    """给一个压缩桶定衰减档(设计深化 §1.4 的三档,规则确定、不调模型)。

    * 桶里出现过 identity / decision 的 memory/note -> `permanent`(永不衰减);
    * 既没有 memory/note、又没有任何锚点 -> `temporary`(闲聊/娱乐,半衰期 2 天);
    * 其余 -> `periodic`(工作项目/健身,15 天)。

    ⚠️ 早先所有桶都硬编码成 periodic,三档 λ 只有一档在用 —— 等于公式白写了。
    """
    from .anchors import extract_anchors

    notes = [event for event in events if getattr(event, "type", "") == "memory/note"]
    if any(
        str(event.data.get("fact_type") or "") in PERMANENT_FACT_TYPES for event in notes
    ):
        return "permanent"
    texts = [
        str(event.data.get("text") or event.data.get("content") or "")
        for event in events
        if getattr(event, "type", "") in ("user/message", "assistant/message", "memory/note")
    ]
    if not notes and not extract_anchors("\n".join(texts)):
        return "temporary"
    return "periodic"


def parse_iso(text: str | None) -> datetime | None:
    return _clock_parse_iso(text)


def days_since(text: str | None, reference: datetime) -> float:
    """距今天数;时间戳缺失或无法解析时按"很久没碰"处理(返回 365)。"""
    moment = parse_iso(text)
    if moment is None:
        return 365.0
    if moment.tzinfo is None and reference.tzinfo is not None:
        moment = moment.replace(tzinfo=reference.tzinfo)
    delta = reference - moment
    return max(delta.total_seconds() / 86400.0, 0.0)
