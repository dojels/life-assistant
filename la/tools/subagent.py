"""子代理考古:并行、上下文隔离、**由代码截断**。

代码保证(设计深化 §3.4):
  * asyncio.gather 并行;
  * 总数 <= 4、深度 <= 2(配额由 CircuitBreaker 统一管);
  * 每个代理的输出由**代码**截断到 max_tokens_per_agent(默认 500);
  * 任一代理失败只影响它自己(stop_reason=error),主循环不崩。

两种摘录策略(与摘要器同一套兜底思路,见 docs/决策记录.md D12):
  * DeterministicDigest —— 纯代码打分抽取,离线可跑,是默认与兜底;
  * LLMDigest —— 按设计深化 §2.2 的模板交给模型,需 API 密钥,失败自动回落。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..base.phases import LIMIT_SUBAGENT_TOKENS
from ..memory.anchors import extract_anchors
from ..memory.search import extract_terms
from .registry import clip_tokens

STATUS_FOUND = "found"
STATUS_EMPTY = "empty"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_FAILED = "failed"

STOP_COMPLETED = "completed"
STOP_ERROR = "error"
STOP_TIMEOUT = "timeout"
STOP_MAX_TOKENS = "max_tokens"

SUBAGENT_TEMPLATE = """你是记忆档案挖掘员。任务:在指定时间范围内查找与下列问题相关的原文。
规则:
1. 只读取,禁止调用任何其他工具。
2. 命中处仅截取该事件前后各 3 轮,严禁全文搬运。
3. 按 JSON 返回;无命中返回 {"status":"empty"}:
   {"status":"found"|"empty"|"ambiguous",
    "findings":[{"timestamp":"ISO8601","seq":123,"content":"原文片段(≤500 字符)","salience":0.9}],
    "note":"一句话说明"}
问题:{{query}}
时间范围:{{start}} — {{end}}"""

MAX_FINDINGS = 3
SNIPPET_CHARS = 500
#: 设计深化 §2.2:命中处仅截取该事件**前后各 3 轮**(记忆方案 §五 允许放宽到 5-10 轮)。
#: 早先是 1 轮 —— 三份文稿三个值,实现取了最小的那个,审核时判定为偏离。
CONTEXT_NEIGHBOURS = 3


@dataclass
class SubAgentTask:
    query: str
    start: str
    end: str
    session_id: str | None = None
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "start": self.start,
            "end": self.end,
            "session_id": self.session_id,
            "label": self.label,
        }


@dataclass
class Finding:
    timestamp: str
    content: str
    seq: int | None = None
    session_id: str | None = None
    salience: float = 0.5
    locator: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "content": self.content,
            "seq": self.seq,
            "session_id": self.session_id,
            "salience": self.salience,
            "locator": self.locator,
        }


@dataclass
class SubAgentReport:
    agent_id: str
    status: str
    stop_reason: str
    findings: list[Finding] = field(default_factory=list)
    note: str = ""
    elapsed_ms: float = 0.0
    raw_tokens: int = 0
    task: SubAgentTask | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "findings": [item.as_dict() for item in self.findings],
            "note": self.note,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "raw_tokens": self.raw_tokens,
            "task": self.task.as_dict() if self.task else None,
        }


class DeterministicDigest:
    """纯代码打分:命中关键词/锚点的事件 + 少量上下文,按命中数排序。"""

    def __init__(self, store) -> None:
        self.store = store

    def _score(self, text: str, terms: Sequence[str], anchors: Sequence[str]) -> float:
        hits = sum(1 for term in terms if term and term in text)
        anchor_hits = sum(1 for anchor in anchors if anchor and anchor in text)
        return float(hits) + 0.5 * float(anchor_hits)

    async def run(self, task: SubAgentTask) -> SubAgentReport:
        """⚠️ 必须是 async:SQLite 连接默认 check_same_thread=True,
        丢进 asyncio.to_thread 会直接抛 ProgrammingError —— 那样每个子代理都会"失败",
        而且失败得很安静(被隔离机制吞掉)。"""
        terms = extract_terms(task.query)
        anchors = extract_anchors(task.query)
        events = self.store.query_range(task.start, task.end, limit=500, session_id=task.session_id)
        scored: list[tuple[float, int, Any]] = []
        for index, event in enumerate(events):
            text = str(event.data.get("text") or event.data.get("content") or "")
            if not text:
                continue
            score = self._score(text, terms, anchors)
            if score > 0:
                scored.append((score, index, event))
        if not scored:
            return SubAgentReport(
                agent_id="", status=STATUS_EMPTY, stop_reason=STOP_COMPLETED, task=task
            )

        scored.sort(key=lambda item: (-item[0], item[1]))
        chosen = sorted(scored[:MAX_FINDINGS], key=lambda item: item[1])

        findings: list[Finding] = []
        for score, index, event in chosen:
            window = events[max(index - CONTEXT_NEIGHBOURS, 0): index + CONTEXT_NEIGHBOURS + 1]
            snippet = " / ".join(
                str(item.data.get("text") or item.data.get("content") or "") for item in window
            )
            findings.append(
                Finding(
                    timestamp=event.time,
                    content=snippet[:SNIPPET_CHARS],
                    seq=event.seq,
                    session_id=event.session_id if hasattr(event, "session_id") else task.session_id,
                    salience=min(1.0, score / 3.0),
                    locator="data/archive/#seq=" + str(event.seq),
                )
            )
        status = STATUS_AMBIGUOUS if len(scored) > MAX_FINDINGS else STATUS_FOUND
        return SubAgentReport(
            agent_id="",
            status=status,
            stop_reason=STOP_COMPLETED,
            findings=findings,
            note="命中 " + str(len(scored)) + " 处,返回前 " + str(len(findings)) + " 条",
            task=task,
        )


class LLMDigest:
    """按 §2.2 模板交给模型;任何解析失败都回落到 DeterministicDigest。"""

    def __init__(self, store, client, fallback: DeterministicDigest | None = None) -> None:
        self.store = store
        self.client = client
        self.fallback = fallback or DeterministicDigest(store)

    async def run(self, task: SubAgentTask) -> SubAgentReport:
        events = self.store.query_range(task.start, task.end, limit=200, session_id=task.session_id)
        rendered = "\n".join(
            event.time + " [" + event.type + "] "
            + str(event.data.get("text") or event.data.get("content") or "")
            for event in events
        )
        prompt = SUBAGENT_TEMPLATE.replace("{{query}}", task.query).replace(
            "{{start}}", task.start
        ).replace("{{end}}", task.end)
        try:
            result = await self.client.chat(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "<events>\n" + rendered[:20000] + "\n</events>"},
                ]
            )
            parsed = json.loads(_extract_json(result.text))
            status = str(parsed.get("status") or STATUS_EMPTY)
            findings = [
                Finding(
                    timestamp=str(item.get("timestamp") or ""),
                    content=str(item.get("content") or "")[:SNIPPET_CHARS],
                    seq=item.get("seq"),
                    session_id=task.session_id,
                    salience=float(item.get("salience") or 0.5),
                )
                for item in (parsed.get("findings") or [])
            ]
            return SubAgentReport(
                agent_id="",
                status=status,
                stop_reason=STOP_COMPLETED,
                findings=findings,
                note=str(parsed.get("note") or ""),
                task=task,
            )
        except Exception:
            return await self.fallback.run(task)


def _extract_json(text: str) -> str:
    match = re.search(r"\{.*\}", text, re.S)
    return match.group(0) if match else "{}"


class SubAgentRunner:
    """并行派发 + 超时 + 代码截断 + 失败隔离。"""

    def __init__(
        self,
        digest,
        *,
        timeout_s: float = 120.0,
        max_tokens_per_agent: int = LIMIT_SUBAGENT_TOKENS,
    ) -> None:
        self.digest = digest
        self.timeout_s = timeout_s
        self.max_tokens_per_agent = max_tokens_per_agent

    async def spawn(
        self,
        tasks: Sequence[SubAgentTask],
        *,
        max_agents: int = 4,
        max_tokens_per_agent: int | None = None,
    ) -> list[SubAgentReport]:
        selected = list(tasks)[:max_agents]
        budget = int(max_tokens_per_agent or self.max_tokens_per_agent)
        results = await asyncio.gather(
            *(self._run_one(task, index, budget) for index, task in enumerate(selected)),
            return_exceptions=True,
        )
        reports: list[SubAgentReport] = []
        for index, outcome in enumerate(results):
            if isinstance(outcome, BaseException):
                reports.append(
                    SubAgentReport(
                        agent_id="a" + str(index + 1),
                        status=STATUS_FAILED,
                        stop_reason=STOP_ERROR,
                        note=type(outcome).__name__ + ": " + str(outcome)[:120],
                        task=selected[index],
                    )
                )
            else:
                reports.append(outcome)
        return reports

    async def _run_one(
        self, task: SubAgentTask, index: int, max_tokens: int | None = None
    ) -> SubAgentReport:
        started = time.perf_counter()
        agent_id = "a" + str(index + 1)
        try:
            # 并行 = asyncio 并发。确定性摘录是本地索引扫描;LLM 摘录才是真正的并发等待。
            report = await asyncio.wait_for(self.digest.run(task), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            return SubAgentReport(
                agent_id=agent_id,
                status=STATUS_FAILED,
                stop_reason=STOP_TIMEOUT,
                note="子代理超过 " + str(self.timeout_s) + "s 未返回",
                task=task,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
        except Exception as exc:
            return SubAgentReport(
                agent_id=agent_id,
                status=STATUS_FAILED,
                stop_reason=STOP_ERROR,
                note=type(exc).__name__ + ": " + str(exc)[:120],
                task=task,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

        report.agent_id = agent_id
        report.elapsed_ms = (time.perf_counter() - started) * 1000.0
        report.raw_tokens = _report_tokens(report)
        report.findings, truncated = _clip_findings(
            report.findings, int(max_tokens or self.max_tokens_per_agent)
        )
        if truncated:
            report.stop_reason = STOP_MAX_TOKENS
        return report


def _report_tokens(report: SubAgentReport) -> int:
    from ..config import estimate_tokens

    return estimate_tokens("\n".join(item.content for item in report.findings))


def _clip_findings(findings: list[Finding], max_tokens: int) -> tuple[list[Finding], bool]:
    """代码截断:超出预算就按 head/tail 保留,并在中间标注省略。"""
    from ..config import estimate_tokens

    kept = list(findings)
    total = estimate_tokens("\n".join(item.content for item in kept))
    if total <= max_tokens:
        return kept, False
    clipped: list[Finding] = []
    budget = max_tokens
    for item in kept:
        cost = estimate_tokens(item.content)
        if cost > budget:
            text, _ = clip_tokens(item.content, max(budget, 40))
            item = Finding(
                timestamp=item.timestamp,
                content=text,
                seq=item.seq,
                session_id=item.session_id,
                salience=item.salience,
                locator=item.locator,
            )
            clipped.append(item)
            break
        clipped.append(item)
        budget -= cost
    return clipped, True


def build_runner(store, settings, client) -> SubAgentRunner:
    if settings.provider == "fake":
        digest: Any = DeterministicDigest(store)
    else:
        digest = LLMDigest(store, client, fallback=DeterministicDigest(store))
    return SubAgentRunner(digest, timeout_s=120.0, max_tokens_per_agent=LIMIT_SUBAGENT_TOKENS)
