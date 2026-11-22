"""考古证据暂存器(Scratchpad)。

结构照抄设计深化 §2.3:
    { "evidence": [ {source, timestamp, content, anchors, seq, session_id} ],
      "conflicts": [], "final_basis": "..." }

不变量:证据**先按 timestamp 升序保存**,再做冲突裁决 —— 顺序错了裁决就会采信错的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import estimate_tokens
from .anchors import extract_anchors

SOURCE_SUMMARY = "summary_pool"
SOURCE_TIMESTAMP_RANGE = "timestamp_range"
SOURCE_ARCHIVE_SEARCH = "archive_search"
SOURCE_SUB_AGENT = "sub_agent"


@dataclass
class Evidence:
    source: str
    timestamp: str
    content: str
    anchors: list[str] = field(default_factory=list)
    seq: int | None = None
    session_id: str | None = None
    salience: float = 0.5
    locator: str = ""

    def __post_init__(self) -> None:
        if not self.anchors:
            self.anchors = extract_anchors(self.content)

    @property
    def key(self) -> tuple:
        return (self.session_id, self.seq, self.content[:60])

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "timestamp": self.timestamp,
            "content": self.content,
            "anchors": self.anchors,
            "seq": self.seq,
            "session_id": self.session_id,
            "salience": self.salience,
            "locator": self.locator,
        }


@dataclass
class Scratchpad:
    evidence: list[Evidence] = field(default_factory=list)
    conflicts: list[Any] = field(default_factory=list)
    final_basis: str = ""
    rejected: list[dict[str, Any]] = field(default_factory=list)

    def add(self, item: Evidence) -> bool:
        """去重后追加。返回是否真的新增。"""
        for existing in self.evidence:
            if existing.key == item.key:
                return False
        self.evidence.append(item)
        return True

    def extend(self, items: Iterable[Evidence]) -> int:
        return sum(1 for item in items if self.add(item))

    def add_summary_hits(self, summaries: Iterable[Any]) -> int:
        added = 0
        for row in summaries:
            added += self.add(
                Evidence(
                    source=SOURCE_SUMMARY,
                    timestamp=row.start_time,
                    content=" ".join(str(row.text).split()),
                    anchors=list(row.anchors),
                    session_id=row.session_id,
                    salience=0.6,
                    locator="summary_pool#id=" + str(row.id),
                )
            )
        return added

    def sorted_evidence(self) -> list[Evidence]:
        return sorted(self.evidence, key=lambda item: (item.timestamp, item.seq or 0))

    def tokens(self) -> int:
        return estimate_tokens("\n".join(item.content for item in self.evidence))

    def clip(self, budget_tokens: int) -> list[Evidence]:
        """超预算时丢最旧的,保留最新证据(裁决要用最新)。"""
        ordered = self.sorted_evidence()
        while ordered and estimate_tokens("\n".join(i.content for i in ordered)) > budget_tokens:
            ordered.pop(0)
        return ordered

    def __len__(self) -> int:
        return len(self.evidence)

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence": [item.as_dict() for item in self.sorted_evidence()],
            "conflicts": list(self.conflicts),
            "final_basis": self.final_basis,
            "rejected": list(self.rejected),
        }
