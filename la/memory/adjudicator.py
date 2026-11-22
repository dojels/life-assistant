"""证据裁决(设计深化 §2.3)。

算法:
  1. 全部证据按 timestamp 升序;
  2. 冲突检测:同一"实体+属性"的取值集合 >1 且时间不同 -> 记为冲突;
  3. 裁决:**采信最新时间戳**的条目,并附一句代码生成的修正说明;
  4. 零证据 -> 注入"诚实遗忘"模板并结束,不再搜索。

裁决是纯函数,不调用模型 —— 这是"时序题必须可复现"的前提。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from .scratchpad import Evidence, Scratchpad

BASIS_SUMMARY = "summary"
BASIS_ARCHIVE = "archive"
BASIS_NONE = "none"

# 能在数值锚点之间比较的形态:5 万 / 3800元 / ¥120 / 6.5万元
_VALUE_PATTERN = re.compile(r"^[¥￥]?\d+(?:\.\d+)?(?:万元|万|元|块)?$")

TEMPLATE_ZERO_EVIDENCE = (
    "档案中没有找到相关记录。可能当时未存入,或时间/表述有误。"
    "你可以告诉我大致时间范围,我再按时间翻一遍。"
)
TEMPLATE_BUDGET_EXHAUSTED = "我已翻遍你指定范围的档案,没有相关内容。"


def normalize_value(anchor: str) -> str | None:
    compact = re.sub(r"\s+", "", anchor or "")
    if not compact or not _VALUE_PATTERN.match(compact):
        return None
    return compact


def values_of(item: Evidence) -> list[str]:
    found = []
    for anchor in item.anchors:
        value = normalize_value(anchor)
        if value is not None:
            found.append(value)
    return found


@dataclass
class Conflict:
    entity: str
    entries: list[tuple[str, list[str]]] = field(default_factory=list)  # (timestamp, values)
    winner_timestamp: str = ""
    winner_values: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "entries": [{"timestamp": t, "values": v} for t, v in self.entries],
            "winner_timestamp": self.winner_timestamp,
            "winner_values": list(self.winner_values),
            "note": self.note,
        }


@dataclass
class Verdict:
    basis: str
    evidence: list[Evidence] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    note: str = ""
    reason_code: str = ""
    #: 子代理报告里出现过 ambiguous(多组候选同时命中)——
    #: 设计深化 §2.2/§4.3:这种情况该**问用户**,不该由模型自己挑一个。
    #: 判据由代码给(status 来自子代理协议),提示词只负责把理由写出来。
    ambiguous: bool = False

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence)

    def latest(self) -> Evidence | None:
        return self.evidence[-1] if self.evidence else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "basis": self.basis,
            "evidence_count": len(self.evidence),
            "conflicts": [c.as_dict() for c in self.conflicts],
            "note": self.note,
            "reason_code": self.reason_code,
            "ambiguous": self.ambiguous,
        }


def _entity_for(item: Evidence, terms: Sequence[str]) -> str | None:
    for term in terms:
        if term and term in item.content:
            return term
    return None


def detect_conflicts(evidence: Iterable[Evidence], terms: Sequence[str]) -> list[Conflict]:
    """同一实体在**不同时间戳**上出现不同数值 -> 冲突。"""
    groups: dict[str, list[Evidence]] = {}
    for item in evidence:
        entity = _entity_for(item, terms)
        if entity is None:
            continue
        if values_of(item):
            groups.setdefault(entity, []).append(item)

    conflicts: list[Conflict] = []
    for entity, items in groups.items():
        by_time: dict[str, list[str]] = {}
        for item in items:
            by_time.setdefault(item.timestamp, [])
            for value in values_of(item):
                if value not in by_time[item.timestamp]:
                    by_time[item.timestamp].append(value)
        distinct = {value for values in by_time.values() for value in values}
        if len(by_time) < 2 or len(distinct) < 2:
            continue
        entries = sorted(by_time.items())
        winner_timestamp, winner_values = entries[-1]
        detail = ";".join(
            " ".join(values) + "(" + timestamp + ")" for timestamp, values in entries
        )
        note = (
            "⚠️ 相关记录存在修正:" + detail + ",以最新为准(" + winner_timestamp + ")。"
        )
        conflicts.append(
            Conflict(
                entity=entity,
                entries=entries,
                winner_timestamp=winner_timestamp,
                winner_values=list(winner_values),
                note=note,
            )
        )
    return conflicts


def adjudicate(
    pad: Scratchpad,
    *,
    terms: Sequence[str] = (),
    budget_tokens: int | None = None,
    budget_exhausted: bool = False,
    ambiguous: bool = False,
    now: datetime | None = None,
) -> Verdict:
    """把 Scratchpad 变成可注入的裁决结果。"""
    del now  # 目前不依赖当前时间;保留参数是为了将来做"距今多久"的叙述
    if not pad.evidence:
        return Verdict(
            basis=BASIS_NONE,
            note=TEMPLATE_BUDGET_EXHAUSTED if budget_exhausted else TEMPLATE_ZERO_EVIDENCE,
            reason_code="ZERO_EVIDENCE",
        )

    evidence = pad.clip(budget_tokens) if budget_tokens else pad.sorted_evidence()
    conflicts = detect_conflicts(evidence, terms)
    pad.conflicts = [conflict.as_dict() for conflict in conflicts]

    has_archive = any(
        item.source in ("timestamp_range", "archive_search", "sub_agent") for item in evidence
    )
    basis = BASIS_ARCHIVE if has_archive else BASIS_SUMMARY
    earliest, latest = evidence[0].timestamp, evidence[-1].timestamp
    sources = len({item.source for item in evidence})
    note = (
        "找到了 " + str(len(evidence)) + " 条相关记录(最早 " + earliest
        + ",最新 " + latest + ",来源 " + str(sources) + " 类)。以下按时间顺序整理:"
    )
    if conflicts:
        note = note + "\n" + "\n".join(conflict.note for conflict in conflicts)
    pad.final_basis = basis
    return Verdict(
        basis=basis,
        evidence=evidence,
        conflicts=conflicts,
        note=note,
        reason_code="OK",
        ambiguous=bool(ambiguous),
    )


def render_evidence_block(verdict: Verdict, *, budget_tokens: int) -> str:
    """渲染成注入用的证据块。超预算时丢最旧的证据条目(裁决要用最新的)。"""
    if not verdict.has_evidence:
        return (
            "<evidence>\n<evidence-status>none</evidence-status>\n"
            "<evidence-guidance>" + verdict.note + "</evidence-guidance>\n</evidence>"
        )
    items = list(verdict.evidence)
    while len(items) > 1 and _block_tokens(items, verdict) > budget_tokens:
        items.pop(0)
    return _block_tokens_text(items, verdict)


def _block_tokens(items: Sequence[Evidence], verdict: Verdict) -> int:
    from ..config import estimate_tokens

    return estimate_tokens(_block_tokens_text(items, verdict))


def _block_tokens_text(items: Sequence[Evidence], verdict: Verdict) -> str:
    lines = ['<evidence count="' + str(len(items)) + '">']
    lines.append("<evidence-status>found</evidence-status>")
    if verdict.ambiguous:
        # 代码给的判据(不是让模型自己猜"要不要问"):
        # 子代理报过 ambiguous,说明有多组候选 —— 先问清是哪一件,再下结论。
        lines.append(
            "<evidence-ambiguity>有多组候选同时命中,先用 ask_user 向用户确认是哪一件,"
            "再给结论;不要自己替他挑一个。</evidence-ambiguity>"
        )
    if verdict.conflicts:
        for conflict in verdict.conflicts:
            lines.append("<conflict-note>" + conflict.note + "</conflict-note>")
    for item in items:
        content = " ".join(item.content.split())
        lines.append("- [" + item.timestamp + "] (" + item.source + ") " + content)
    lines.append("</evidence>")
    return "\n".join(lines)
