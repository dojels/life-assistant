"""记忆巩固 / 回填(设计深化 §1.5)。

触发:考古成功且回复已发送 -> 后台任务 consolidate()。
动作:把本次考古命中的证据按模板生成新摘要骨架,与目标桶已有摘要**合并**
      —— 保留仍为真的事实、丢弃过时事实、合并新信息、锚点合并去重 —— 写回同一行。
幂等键:(session_id, start_seq, end_seq, rounds_after_reply)。回复轮次作为版本,
        防止同一段证据被反复回填。
失败处理:重试 2 次后写 consolidate_failed 标记并丢弃,**不阻塞主流程**。

合并是纯函数,不调模型:回填发生在回复之后,它出错的代价必须被限制在"摘要差一点",
而不是"整轮对话失败"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .anchors import extract_anchors
from .decay import DEFAULT_CATEGORY
from .scratchpad import Evidence
from .topics import merge_tags

SECTIONS = ("时段", "事项", "事实", "偏好", "待办", "锚点", "其他")
SECTION_RE = re.compile(r"^-\s*(时段|事项|事实|偏好|待办|锚点|其他)\s*[:：]\s*(.*)$", re.M)
MAX_FACT_ITEMS = 6
SNIPPET_CHARS = 60
MAX_RETRIES = 2


@dataclass
class ConsolidationOutcome:
    applied: bool
    summary_id: int | None = None
    reason: str = ""
    evidence_count: int = 0


def parse_sections(text: str) -> dict[str, str]:
    """把摘要文本拆回分节字典;解析不出来就返回空字典。"""
    found: dict[str, str] = {}
    for match in SECTION_RE.finditer(text or ""):
        found[match.group(1)] = match.group(2).strip()
    return found


def render_sections(sections: dict[str, str]) -> str:
    lines = ["<compacted-summary>"]
    for name in SECTIONS:
        value = (sections.get(name) or "").strip() or "(none)"
        lines.append("- " + name + ":" + value)
    lines.append("</compacted-summary>")
    return "\n".join(lines)


def _items(value: str) -> list[str]:
    if not value or value.strip() in ("(none)", "[]"):
        return []
    if value.strip().startswith("[") and value.strip().endswith("]"):
        return [part.strip() for part in value.strip()[1:-1].split(",") if part.strip()]
    return [part.strip() for part in value.split(";") if part.strip()]


def _join_items(items: Sequence[str]) -> str:
    return ";".join(items) if items else "(none)"


def _join_anchors(items: Sequence[str]) -> str:
    return "[" + ", ".join(items) + "]" if items else "(none)"


def merge_summary_text(
    existing: str | None,
    evidence: Iterable[Evidence],
    *,
    start_time: str,
    end_time: str,
) -> tuple[str, int]:
    """把考古证据并进已有摘要。返回 (新摘要文本, 新增事实条数)。"""
    sections = parse_sections(existing or "") or {name: "(none)" for name in SECTIONS}

    facts = _items(sections.get("事实", ""))
    added = 0
    for item in evidence:
        snippet = " ".join(item.content.split())[:SNIPPET_CHARS]
        if not snippet:
            continue
        if any(snippet[:20] in existing_fact for existing_fact in facts):
            continue
        if len(facts) >= MAX_FACT_ITEMS:
            break
        facts.append(snippet)
        added += 1

    anchors = _items(sections.get("锚点", ""))
    for item in evidence:
        for anchor in extract_anchors(item.content):
            if anchor not in anchors:
                anchors.append(anchor)

    sections["时段"] = start_time + " — " + end_time
    sections["事实"] = _join_items(facts)
    sections["锚点"] = _join_anchors(anchors)
    return render_sections(sections), added


async def consolidate(
    store,
    *,
    session_id: str,
    start_seq: int,
    end_seq: int,
    rounds_after_reply: int,
    evidence: Sequence[Evidence],
    start_time: str,
    end_time: str,
    retries: int = MAX_RETRIES,
) -> ConsolidationOutcome:
    """把证据回填进摘要池。幂等、可失败、不阻塞主流程。"""
    if not evidence:
        return ConsolidationOutcome(False, reason="NO_EVIDENCE")

    if store.consolidation_seen(session_id, start_seq, end_seq, rounds_after_reply):
        return ConsolidationOutcome(False, reason="ALREADY_CONSOLIDATED")

    bucket = store.find_summary_for_seq(session_id, start_seq, end_seq)
    existing = bucket.text if bucket is not None else None
    # ⚠️ 合并**不能**把原有的衰减档冲掉:回填是"想起来更多细节",
    #    不是"这件事变成另一类事"。桶不在时才用默认档。
    category = bucket.category if bucket is not None else DEFAULT_CATEGORY
    # ⚠️ 回填不能把桶原有的领域标签冲掉(2026-11-07):以前这里写死 tags=["consolidated"],
    #    于是"回填过一次"的桶反而丢掉 health/ledger 标签 —— 过滤又变成查不到。
    tags = merge_tags(bucket.tags if bucket is not None else (), ["consolidated"])
    merged, added = merge_summary_text(
        existing, evidence, start_time=start_time, end_time=end_time
    )

    last_error = ""
    for _ in range(max(retries, 1)):
        try:
            summary_id = store.upsert_summary(
                session_id=session_id,
                start_seq=start_seq,
                end_seq=end_seq,
                start_time=start_time,
                end_time=end_time,
                text=merged,
                anchors=extract_anchors(merged),
                tags=tags,
                category=category,
            )
        except Exception as exc:  # 回填失败不能影响主流程
            last_error = type(exc).__name__ + ": " + str(exc)[:200]
            continue
        first = store.record_consolidation(
            session_id=session_id,
            start_seq=start_seq,
            end_seq=end_seq,
            rounds_after_reply=rounds_after_reply,
            summary_id=summary_id,
            evidence_count=len(evidence),
        )
        return ConsolidationOutcome(
            applied=first,
            summary_id=summary_id,
            reason="OK" if first else "DUPLICATE",
            evidence_count=len(evidence),
        )
    # 设计深化 §1.5:重试 2 次仍失败 -> 写 consolidate/failed 事件后丢弃(不阻塞主流程)。
    # 没有这条事件的话,"回填悄悄没成功"在审计里完全看不见。
    _mark_failed(
        store,
        session_id=session_id,
        start_seq=start_seq,
        end_seq=end_seq,
        rounds_after_reply=rounds_after_reply,
        reason=last_error,
    )
    return ConsolidationOutcome(False, reason="FAILED: " + last_error)


def _mark_failed(
    store,
    *,
    session_id: str,
    start_seq: int,
    end_seq: int,
    rounds_after_reply: int,
    reason: str,
) -> None:
    """失败留痕。store 没有 append(测试替身)时安静跳过,绝不让它反过来影响主流程。"""
    append = getattr(store, "append", None)
    if append is None:
        return
    try:
        append(
            session_id,
            "consolidate/failed",
            {
                "bucket": {"start_seq": start_seq, "end_seq": end_seq},
                "rounds_after_reply": rounds_after_reply,
                "error": str(reason)[:200],
            },
        )
    except Exception:
        pass
