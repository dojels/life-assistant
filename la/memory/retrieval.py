"""Phase 1 目录直觉检索(快速通道)。

排序是**确定性**的,按设计深化 §3.1 的加权公式(不用任何"置信度" ——
LLM 自评置信度不可靠,所以排序完全由代码做):

    score = 0.5 · anchor_hit + 0.3 · fts_norm + 0.2 · heat_norm

其中:
  * `anchor_hit` = 命中的锚点**类别数** / 3(日期 / 数字 / 人名实体);
  * `fts_norm`  = `1 / (1 + |bm25|)`;只走 LIKE 降级时没有 bm25,给固定值 0.3;
  * `heat_norm` = `min(1, heat / **本会话**池内最大 heat)`
    (家庭版共库:别让 A 成员的高频主题抬高 B 的分母,见 2026-11-07 修);
  * 平局按 `last_access DESC`(从没访问过的排最后)。

⚠️ 早先这里是**字典序**(锚点数 → 时间匹配 → 关键词命中计数 → 热度),
与设计稿的加权公式不同;2026-10-12 一致性审核后改为按公式实现。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .. import config
from . import topics
from .anchors import ANCHOR_KINDS, anchor_hits, anchor_kinds, extract_anchors
from .decay import days_since, heat
from .projector import build_memory_block
from .search import extract_terms, search_summaries
from .store import FactRow, MemoryStore, SummaryRow

STATUS_HIT = "hit"
STATUS_MISS = "miss"

#: 加权公式的三个权重(设计深化 §3.1)
WEIGHT_ANCHOR = 0.5
WEIGHT_FTS = 0.3
WEIGHT_HEAT = 0.2
#: LIKE 降级没有 bm25:给一个固定值,低于大多数真实 fts 命中
LIKE_FTS_NORM = 0.3


@dataclass
class RetrievalResult:
    status: str
    summaries: list[SummaryRow] = field(default_factory=list)
    facts: list[FactRow] = field(default_factory=list)
    query_anchors: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    scores: dict[int, float] = field(default_factory=dict)
    #: 每个命中的分项(anchor_hit / fts_norm / heat_norm / is_exact_anchor_hit)——
    #: 排障时"它为什么排第一"要能回答,不然公式只是黑箱
    details: dict[int, dict[str, Any]] = field(default_factory=dict)

    @property
    def ids(self) -> list[int]:
        return [row.id for row in self.summaries]


def _passes_filters(row: SummaryRow, since: str | None, tags: Iterable[str] | None) -> bool:
    if since and str(row.end_time) < str(since):
        return False
    # 中文别名("健康")与大小写("Health")都要能过滤 —— 标签存的是小写领域名。
    wanted = {topics.normalize_tag(tag) for tag in (tags or ()) if str(tag).strip()}
    if wanted and not wanted.intersection({topics.normalize_tag(tag) for tag in row.tags}):
        return False
    return True


def _summary_domain(row: SummaryRow) -> str:
    return topics.primary_domain(row.tags)


def _select_with_tag_coverage(
    scored: list[tuple[float, str, SummaryRow, dict[str, Any]]], limit: int
) -> list[tuple[float, str, SummaryRow, dict[str, Any]]]:
    """top-k 里给每个**有命中的领域**至少留一条(2026-11-07 修)。

    ⚠️ 修的是"健康/账目/错题/装修共用一个 top-k":分数最高的那个领域以前可以
       把 limit 条全占满,别的领域一条都进不来 —— 哪怕它只差 0.01 分。
       现在给"占满者"设上限 limit-(k-1)(k=命中里的领域数),保证剩下的领域
       至少各有一条;整体仍按分数排序,不是平均分配。
    ⚠️ 只有一个领域命中时上限=limit,行为与修改前完全一致。
    """
    if limit <= 0:
        return []
    top = scored[:limit]
    if len(top) < 3:
        return top
    # ⚠️ 领域集合要看**全部候选**,不能只看 top 里已有的:高分全来自一个领域时,
    #    top 里当然只有一个领域 —— 那正是要修的"占满"场景,拿它判断等于永远不触发。
    domains = {_summary_domain(item[2]) for item in scored if _summary_domain(item[2])}
    if len(domains) < 2:
        return top
    cap = max(1, limit - (len(domains) - 1))
    if cap >= limit:
        return top
    chosen: list[tuple[float, str, SummaryRow, dict[str, Any]]] = []
    counts: dict[str, int] = {}
    chosen_rows: set[int] = set()
    for item in scored:
        tag = _summary_domain(item[2])
        if tag and counts.get(tag, 0) >= cap:
            continue
        chosen.append(item)
        chosen_rows.add(item[2].id)
        if tag:
            counts[tag] = counts.get(tag, 0) + 1
        if len(chosen) >= limit:
            break
    if len(chosen) < limit:
        # 领域数大于 limit 时(极罕见)上面的循环凑不满,按分数把剩下的补回去。
        for item in scored:
            if item[2].id in chosen_rows:
                continue
            chosen.append(item)
            if len(chosen) >= limit:
                break
    chosen.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return chosen[:limit]


def retrieve(
    store: MemoryStore,
    query: str,
    *,
    limit: int = 5,
    now: datetime | None = None,
    since: str | None = None,
    tags: Iterable[str] | None = None,
    session_id: str | None = None,
) -> RetrievalResult:
    started = time.perf_counter()
    moment = now or config.get_settings().now()
    query_anchors = extract_anchors(query)
    terms = extract_terms(query)

    hits = search_summaries(
        store.conn, query, limit=limit * 4, mode=store.search_mode,
        session_id=session_id,
    )
    rows = store.summaries(hits.keys())
    # 永久事实(偏好/身份/决定)每轮都注入 —— 它们小、且与"这一句问什么"无关;
    # 而"命中与否"的状态只看关键词匹配到的事实,否则一旦存过偏好就永远是 hit。
    matched_facts = store.search_facts(query, limit=limit, session_id=session_id)
    # ⭐ 相关事实优先 + 600 token 子预算(2026-11-07):不再把"最近 20 条"整包塞进
    #    1500 token 的记忆块 —— 那会让事实挤掉摘要,也会让老而相关的偏好被新事实顶掉。
    facts = store.facts_for_context(
        query,
        limit=config.HOT_SUMMARY_MAX_ITEMS,
        session_id=session_id,
    )

    heats: dict[int, float] = {}
    for summary_id, row in rows.items():
        heats[summary_id] = heat(
            row.base_strength,
            row.visits,
            days_since(row.last_access or row.created_at, moment),
            category=row.category,
        )
    # ⚠️ 分母必须按会话取:全库最大值会被别的高频主题(家庭版里是别的成员)抬走。
    pool_max = store.pool_max_heat(session_id)
    max_heat = pool_max if pool_max > 0 else max(heats.values(), default=0.0)

    scored: list[tuple[float, str, SummaryRow, dict[str, Any]]] = []
    for summary_id, hit in hits.items():
        row = rows.get(summary_id)
        if row is None or not _passes_filters(row, since, tags):
            continue
        matched = anchor_hits(query_anchors, row.anchors)
        anchor_hit = len(anchor_kinds(matched) & set(ANCHOR_KINDS)) / 3.0
        if hit.bm25 is not None:
            fts_norm = 1.0 / (1.0 + abs(float(hit.bm25)))
        else:
            fts_norm = LIKE_FTS_NORM if "like" in hit.match_kinds else 0.0
        heat_norm = min(1.0, heats[summary_id] / max_heat) if max_heat > 0 else 0.0
        score = WEIGHT_ANCHOR * anchor_hit + WEIGHT_FTS * fts_norm + WEIGHT_HEAT * heat_norm
        detail = {
            "score": round(score, 6),
            "anchor_hit": round(anchor_hit, 6),
            "fts_norm": round(fts_norm, 6),
            "heat_norm": round(heat_norm, 6),
            "heat": round(heats[summary_id], 6),
            "matched_anchors": matched,
            "is_exact_anchor_hit": any(
                query_token == doc_token
                for query_token in query_anchors
                for doc_token in row.anchors
            ),
            "match_kinds": list(hit.match_kinds),
        }
        scored.append((score, str(row.last_access or ""), row, detail))

    # score DESC,平局 last_access DESC(从没访问过的 last_access 为空 -> 排最后)
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    top = _select_with_tag_coverage(scored, limit)

    status = STATUS_HIT if (top or matched_facts) else STATUS_MISS
    elapsed = (time.perf_counter() - started) * 1000.0
    return RetrievalResult(
        status=status,
        summaries=[row for _, _, row, _ in top],
        facts=facts,
        query_anchors=query_anchors,
        terms=terms,
        elapsed_ms=elapsed,
        scores={row.id: score for _, _, row, _ in top},
        details={row.id: detail for _, _, row, detail in top},
    )


def render_block(result: RetrievalResult, *, now_text: str = "") -> str:
    return build_memory_block(
        facts=result.facts,
        summaries=result.summaries,
        status=result.status,
        now_text=now_text,
    )
