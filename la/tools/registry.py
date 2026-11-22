"""工具执行管线(设计深化 §3.8,照抄 DSH):

    parse/validate(schema 子集校验)
      -> phase 门禁(阶段/预算/重复调用)
      -> risk 门禁(low 直通 / confirm 需批准 / never 拒绝)
      -> execute(带超时)
      -> post-process(头尾剪裁 + 脱敏)
      -> 审计

三条不变量:
  * **工具是唯一的执行入口** —— 主循环不直接调 handler;
  * **超时与预算在代码层**,提示词里写"请简短"不算数;
  * **失败返回结构化错误码**,不抛异常打断主循环。

⚠️ **超时只对"真的会 await"的 handler 有效**(D105 实测,见 docs/决策记录.md):
   `asyncio.wait_for` 只能在协程让出控制权时抢占。本仓 52 个工具里有 **46 个**的 handler
   从头到尾没有一个 await(它们是对同步 service 的 async 包装),对它们:
     ① `timeout_ms` **到点也不会中止**;② 它们跑在事件循环里,**跑多久整个网页端/桥接就冻多久**
       (实测:写一份 218KB 的 docx 用了 1.97s,50ms 的限时没拦住,期间事件循环心跳 0 次)。
   认得这件事的字段是 `ToolSpec.blocking`;标了它的工具超出标称超时会记一条
   `tool_over_budget` 事件(先攒数据,再决定要不要花钱拆线程 —— 见 D105 的 P1-C)。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import logging

from .. import config
from ..base.phases import CircuitBreaker, Decision
from ..config import SECRET_PLACEHOLDER, estimate_tokens

logger = logging.getLogger(__name__)

RISK_LOW = "low"
RISK_CONFIRM = "confirm"
RISK_NEVER = "never"

#: "等用户"的工具(唯一口径,别在别处再写一份 —— session 的墙钟豁免也用这个):
#: 它们的耗时不是模型在做功,而是用户在思考。
USER_WAIT_TOOLS = frozenset({
    "ask_user", "category_create", "memory_note", "memory_correct", "persona_note",
    # ⭐ 2026-11-21:它先问"要现在压吗",等用户回答的时长同样不算模型做功。
    "compact_context",
})

#: 即使只读也**不自动重试**的工具:
#:   * 等用户的 —— 超时的含义是"用户没回答",重试等于把同一个问题再弹一次(还白等一轮);
#:   * spawn_sub_agents —— 一次要跑最多 4 个子代理,重试=白花一倍钱,而且它内部已经按代理做了失败隔离。
NO_AUTO_RETRY_TOOLS = USER_WAIT_TOOLS | {"spawn_sub_agents"}

CODE_UNKNOWN_TOOL = "UNKNOWN_TOOL"
CODE_BAD_PARAMS = "BAD_PARAMS"
CODE_NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
CODE_FORBIDDEN = "FORBIDDEN"
CODE_TIMEOUT = "TIMEOUT"
CODE_READ_ONLY = "READ_ONLY"
CODE_HANDLER_ERROR = "HANDLER_ERROR"
#: 唤醒会话的参数级守卫拒绝(见 tools/wake_policy.py)
CODE_WAKE_POLICY = "WAKE_POLICY"


class ToolInputError(ValueError):
    """**参数不合法** —— 不是工具坏了。

    ⚠️ 2026-11-17 修:领域层的 LedgerError(如"无法识别的周期格式")以前一路抛到
    except Exception,被记成 HANDLER_ERROR。自省报表把 HANDLER_ERROR 当真故障,
    于是一次参数写错长得像工具崩了(真机:每天 22:00 日报各一次「expense_query 失败」)。
    参数错误应当在执行时就被认出来,单独一个码,并且不触发自动重试。
    """

# 脱敏:身份证 / 银行卡这类长数字串不进模型上下文
SECRET_PATTERNS = (
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"\b\d{16,19}\b"),
)
#: 与记忆注入块共用同一个占位串(见 config.SECRET_PLACEHOLDER)
MASK = SECRET_PLACEHOLDER

DEFAULT_TAIL_CHARS = 400

# 只读工具白名单(M6 护栏 G18)。
# ⚠️ 这是**白名单**:没登记为只读的一律算"会写"。新加工具忘了标 -> 只读模式下会被多拦一次,
# 而不是偷偷放行。
# ⭐ 2026-10-18 起它**从 la/tools/metadata.py 派生**(唯一真源):同一个工具的名字以前
#    散在四处(标题/图标领域/只读/内部),漏改一处不会报错,只会静默少一层保护。
from .metadata import internal_names as _internal_names
from .metadata import read_only_names as _read_only_names

READ_ONLY_TOOLS: tuple[str, ...] = _read_only_names()


def clip_head_tail(text: str, max_chars: int, *, tail_chars: int = DEFAULT_TAIL_CHARS) -> tuple[str, bool]:
    """超长时保留头尾、中间省略(照抄 DSH pruner 的思路)。返回 (文本, 是否截断)。"""
    if len(text) <= max_chars:
        return text, False
    if max_chars <= 0:
        return "", True
    tail = min(tail_chars, max_chars // 3)
    head = max(max_chars - tail - 32, 0)  # 32 = 省略标记的保守余量
    marker = ""
    for _ in range(6):
        omitted = max(len(text) - head - tail, 0)
        marker = "\n(…中间省略 " + str(omitted) + " 字符…)\n"
        overflow = head + len(marker) + tail - max_chars
        if overflow <= 0:
            break
        head = max(head - overflow, 0)
    return text[:head] + marker + text[-tail:], True


def mask_secrets(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(MASK, text)
    return text


def clip_tokens(text: str, max_tokens: int, *, tail_chars: int = DEFAULT_TAIL_CHARS) -> tuple[str, bool]:
    """按 token 预算剪裁(4 字符/token 的估算,与投影器同一口径)。"""
    max_chars = max((max_tokens - 4) * 4, 0)
    return clip_head_tail(text, max_chars, tail_chars=tail_chars)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    phase: str
    risk: str
    timeout_ms: int
    handler: Callable[[dict[str, Any]], Awaitable[Any]]
    output_budget_tokens: int = 500
    required: tuple[str, ...] = ()
    category: str = "research"  # research | task(配额互不侵占,见 phases.py)
    # 这个工具会不会改数据。M6 的"一键停止写入"(护栏 G18)按它筛。
    # ⚠️ 默认 True —— 新工具忘了标只会"多拦",不会"漏放"。
    writes: bool = True
    #: handler 里**没有 await**(纯同步重活):`timeout_ms` 对它只是预算,到点抢不动它。
    #: 标了它 → 执行耗时超出标称超时会记一条 tool_over_budget(攒实测数据用)。
    #: 判定口径有测试钉着(tests/test_tool_timeout.py):标称超时 ≥8s 的工具,
    #: 要么 handler 里真有 await,要么必须显式标 blocking=True。
    blocking: bool = False
    #: 中文说法 / 领域 / 前端 kind。空值由 tools/metadata.py 补齐(单一真源);
    #: 仍留字段是为了少数工具在注册处就地覆盖。
    title: str = ""
    domain: str = ""
    kind: str = ""
    #: 框架自己发起(工具层不开步骤行,由 run_phases 开)。
    internal: bool = False
    # 参数 schema 的来源二选一:
    #   model  —— Pydantic 模型(领域工具走这条,"schema 单真源")
    #   params —— 手写的 JSON Schema(少数系统工具走这条)
    model: Any = None
    params: dict[str, Any] = field(default_factory=dict)

    def parameters_json_schema(self) -> dict[str, Any]:
        if self.model is not None:
            return self.model.model_json_schema()
        return dict(self.params or {"type": "object", "properties": {}})


@dataclass
class ToolResult:
    ok: bool
    tool: str
    data: Any = None
    error_code: str = ""
    message: str = ""
    duration_ms: float = 0.0
    truncated: bool = False
    gate: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "error_code": self.error_code,
            "message": self.message,
            "duration_ms": round(self.duration_ms, 2),
            "truncated": self.truncated,
            "gate": self.gate,
        }


class ToolRegistry:
    """工具注册表 + 执行管线。"""

    def __init__(
        self,
        *,
        audit_sink: Callable[[dict[str, Any]], None] | None = None,
        risk_approver: Callable[[ToolSpec, dict[str, Any]], bool] | None = None,
        read_only: bool = False,
        direct_write: bool = False,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        guard: Callable[[ToolSpec, dict[str, Any]], str | None] | None = None,
    ) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._audit_sink = audit_sink
        self._risk_approver = risk_approver
        self._read_only = read_only
        # M7 直写:True 时 risk=confirm 不再拦(安全网换成"回执 + 撤销")。
        # ⚠️ 默认 **False** —— 门禁的默认值必须是"关上",要开就得有人显式开。
        self._direct_write = direct_write
        # 执行反馈挂钩:工具开始/结束时各发一条,前端据此画步骤行(含每步用时)。
        self._on_event = on_event
        # 参数级守卫(唤醒会话用):在 read_only / risk 之前判,所以**读工具也能被拦**
        # (ask_user 就是读工具)。返回 None = 放行,返回字符串 = 拒绝理由。
        self._guard = guard

    # -- 注册 -------------------------------------------------------------
    def register(self, spec: ToolSpec) -> None:
        from dataclasses import replace

        from .metadata import meta_for

        meta = meta_for(spec.name)
        spec = replace(
            spec,
            title=spec.title or str(meta.get("title") or ""),
            domain=spec.domain or str(meta.get("domain") or ""),
            kind=spec.kind or str(meta.get("kind") or ""),
            internal=spec.internal or bool(meta.get("internal")),
        )
        # G18:只读白名单是**唯一**判据(它从 metadata 派生),不靠各工具自己记得标 writes。
        if spec.writes and spec.name in READ_ONLY_TOOLS:
            spec = replace(spec, writes=False)
        self._tools[spec.name] = spec

    def register_all(self, specs: list[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name] for name in self.names()]

    def read_only(self) -> bool:
        return self._read_only

    def direct_write(self) -> bool:
        return self._direct_write

    def _emit(self, kind: str, **data: Any) -> None:
        """执行反馈事件(工具开始/结束)。抛异常绝不打断工具本身。"""
        if self._on_event is None:
            return
        try:
            self._on_event(kind, data)
        except Exception:
            pass

    def read_only_names(self) -> list[str]:
        return sorted(name for name, spec in self._tools.items() if not spec.writes)

    # -- 执行 -------------------------------------------------------------
    async def execute(
        self,
        name: str,
        params: dict[str, Any] | None,
        *,
        breaker: CircuitBreaker,
        actor: str = "agent",
        session_id: str | None = None,
    ) -> ToolResult:
        started = time.perf_counter()
        spec = self.get(name)
        if spec is None:
            return self._finish(
                ToolResult(False, name, error_code=CODE_UNKNOWN_TOOL, message="未注册的工具"), started, actor, session_id, params
            )

        missing = [key for key in spec.required if not (params or {}).get(key)]
        if missing:
            return self._finish(
                ToolResult(
                    False, name, error_code=CODE_BAD_PARAMS, message="缺少必填参数: " + ", ".join(missing)
                ),
                started, actor, session_id, params,
            )

        # 参数级守卫:**先于** read_only 与 risk —— 它管的是"这一次调用能不能做",
        # 而不是"这个工具危不危险"。唤醒的 delete 动作就藏在这里被拦下。
        if self._guard is not None:
            reason = self._guard(spec, params or {})
            if reason:
                return self._finish(
                    ToolResult(False, name, error_code=CODE_WAKE_POLICY, message=reason),
                    started, actor, session_id, params,
                )

        # 护栏 G18:全局只读开关优先级最高 —— 它不需要理由,
        # 也不给"恰好这个工具风险低"留后门。
        if self._read_only and spec.writes:
            return self._finish(
                ToolResult(
                    False, name, error_code=CODE_READ_ONLY,
                    message="当前处于只读模式(LA_READ_ONLY=1),写操作已全部停止",
                ),
                started, actor, session_id, params,
            )

        # ⚠️ 顺序:风险门禁**先于**预算门禁 —— 被风险挡下的调用不该消耗阶段配额,
        # 否则一次被拒的确认请求会把后面的正常调用一起拖进裁决阶段。
        if spec.risk == RISK_NEVER:
            return self._finish(
                ToolResult(False, name, error_code=CODE_FORBIDDEN, message="该操作永不自动执行"),
                started, actor, session_id, params,
            )
        if spec.risk == RISK_CONFIRM and not self._direct_write:
            approver = self._risk_approver
            if approver is None or not approver(spec, params or {}):
                return self._finish(
                    ToolResult(
                        False, name, error_code=CODE_NEEDS_CONFIRMATION,
                        message="该操作需要用户确认后才能执行",
                    ),
                    started, actor, session_id, params,
                )

        decision: Decision = breaker.admit(
            name, params, category=spec.category, writes=spec.writes
        )
        if not decision.allowed:
            return self._finish(
                ToolResult(
                    False, name, error_code=decision.code, message=decision.detail,
                    gate={"phase": breaker.phase, "code": decision.code},
                ),
                started, actor, session_id, params,
            )

        self._emit("tool_start", tool=name, params=params or {}, writes=spec.writes,
                   category=spec.category)

        # 瞬时错误(超时/连接类)**自动重试一次**(D105,照云朵 tool_exec._TRANSIENT_RETRY=1)。
        # 理由:一次网络抖动不该吃掉模型一格预算和一轮往返 —— 以前只能靠模型自己再发一次,
        # 而在 D101 之前那次重发还会被 DUPLICATE_CALL 硬拒。
        # ⚠️ **只对只读工具**:写类重试有幂等风险(超时的时候它可能已经写进去了)。
        # ⚠️ 每次尝试各给一份 timeout_ms,所以最坏耗时是 2×timeout_ms —— 这是有意的取舍。
        retry_allowed = not spec.writes and spec.name not in NO_AUTO_RETRY_TOOLS
        attempts = 1 + (config.TOOL_TRANSIENT_RETRY if retry_allowed else 0)
        failed = ToolResult(False, name, error_code=CODE_HANDLER_ERROR, message="未执行")
        for attempt in range(1, attempts + 1):
            try:
                raw = await asyncio.wait_for(
                    spec.handler(params or {}), timeout=spec.timeout_ms / 1000.0
                )
            except asyncio.TimeoutError:
                failed = ToolResult(
                    False, name, error_code=CODE_TIMEOUT,
                    message="执行超时(>" + str(spec.timeout_ms) + "ms)",
                )
                retryable = True
            except ToolInputError as exc:
                # 参数错误不是故障,也不该自动重试(同一份坏参数重试只会再错一次)。
                failed = ToolResult(
                    False, name, error_code=CODE_BAD_PARAMS, message=str(exc)[:200],
                )
                retryable = False
            except Exception as exc:  # 工具失败不该打断主循环
                failed = ToolResult(
                    False, name, error_code=CODE_HANDLER_ERROR,
                    message=type(exc).__name__ + ": " + str(exc)[:200],
                )
                # 只有明确的超时/连接类才重试 —— 宁可漏判,不可误判(照云朵的分类口径)
                retryable = _is_transient(exc)
            else:
                data, truncated = self._post_process(raw, spec)
                note = decision.note
                if attempt > 1:
                    # 让模型知道"这是重试拿到的",免得它以为第一次就成功了(排查时要看真相)
                    note = (note + ";" if note else "") + (
                        "第一次" + failed.message + ",这次是**自动重试**后拿到的结果"
                    )
                if note:
                    data = _with_note(data, note)
                return self._finish(
                    ToolResult(True, name, data=data, truncated=truncated),
                    started, actor, session_id, params,
                )
            if attempt < attempts and retryable:
                logger.warning("工具 %s 瞬时失败(%s),自动重试一次", name, failed.message)
                self._emit("tool_retry", tool=name, attempt=attempt,
                           error_code=failed.error_code)
                continue
            break
        return self._finish(failed, started, actor, session_id, params)

    def _post_process(self, raw: Any, spec: ToolSpec) -> tuple[Any, bool]:
        """脱敏 + 按预算剪裁。**递归处理 dict/list**。

        ⚠️ 旧实现只处理 dict 顶层的字符串(2026-10-18 审计):`events` 这种
        list[dict] 会被原样放行,既不受 output_budget 约束,也不脱敏 ——
        身份证号/手机号能直接进模型上下文。
        """
        if isinstance(raw, str):
            return clip_tokens(mask_secrets(raw), spec.output_budget_tokens)
        if isinstance(raw, (dict, list)):
            return _clean_structured(raw, spec.output_budget_tokens)
        return raw, False

    def _finish(
        self,
        result: ToolResult,
        started: float,
        actor: str,
        session_id: str | None,
        params: dict[str, Any] | None,
    ) -> ToolResult:
        result.duration_ms = (time.perf_counter() - started) * 1000.0
        spec = self.get(result.tool)
        if spec is not None and spec.blocking and result.duration_ms > spec.timeout_ms:
            # 不可抢占的工具:wait_for 对它无效,标称超时只是"预算"。
            # 超出就记一笔 —— 生产上从来没有任何一条超时记录(D105),得先有尺子。
            logger.warning(
                "工具 %s 不可抢占且超出标称超时:%.0fms > %dms(事件循环被独占的时长)",
                result.tool, result.duration_ms, spec.timeout_ms,
            )
            self._emit(
                "tool_over_budget", tool=result.tool, budget_ms=spec.timeout_ms,
                duration_ms=round(result.duration_ms, 2),
            )
        self._emit(
            "tool_end",
            tool=result.tool,
            ok=result.ok,
            error_code=result.error_code,
            duration_ms=round(result.duration_ms, 2),
            data=result.data,
        )
        if self._audit_sink is not None:
            self._audit_sink(
                {
                    "actor": actor,
                    "action": "tool." + result.tool,
                    "target": result.error_code or "ok",
                    "session_id": session_id,
                    "detail": {
                        "params_digest": _digest(params),
                        "ok": result.ok,
                        "error_code": result.error_code,
                        "duration_ms": round(result.duration_ms, 2),
                        "truncated": result.truncated,
                    },
                }
            )
        return result


def _clean_structured(value: Any, budget: int) -> tuple[Any, bool]:
    """递归脱敏 + 剪裁 dict/list 里的字符串;返回 (新结构, 是否有截断)。"""
    if isinstance(value, str):
        return clip_tokens(mask_secrets(value), budget)
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        truncated = False
        for key, item in value.items():
            result, cut = _clean_structured(item, budget)
            cleaned[str(key)] = result
            if cut:
                truncated = True
                # 顶层字符串保留旧字段名,前端/模型都能看出“这里被剪过”。
                if isinstance(item, str):
                    cleaned["truncated_" + str(key)] = True
        return cleaned, truncated
    if isinstance(value, list):
        cleaned_list: list[Any] = []
        truncated = False
        for item in value:
            result, cut = _clean_structured(item, budget)
            cleaned_list.append(result)
            truncated = truncated or cut
        return cleaned_list, truncated
    return value, False


def _is_transient(exc: BaseException) -> bool:
    """这个异常值不值得重试一次?—— 只认明确的超时/连接类。

    判定口径照云朵 `app/skills/registry.py::_classify_skill_exception`:
    **宁可漏判,不可误判** —— 误判重试会绕过用户可见的门控错误(例如参数校验失败)。
    超时(TimeoutError)不在这里判,它在 execute 里单独成一支。
    """
    if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError)):
        return True
    try:
        import httpx

        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return True
    except Exception:  # pragma: no cover - httpx 是硬依赖,取不到就退化成文本判定
        pass
    text = str(exc)
    # 文本兜底(照云朵那条,再加几个 socket 层的常见词)。
    # ⚠️ 只加"一眼就是网络"的词:误判重试会绕过用户可见的门控错误。
    return bool(
        re.search(
            r"timeout|timed out|超时|connection|connect|unreachable|refused"
            r"|连接失败|网络|断网|无法连接|连不上",
            text, re.I,
        )
    )


def _with_note(data: Any, note: str) -> Any:
    """把"放行时的提醒"贴进工具结果(JSON 结构保持合法,别把结构化结果拍成字符串)。

    只在 dict 里加键、其余一律包一层 {"result": ..., "notice": ...} ——
    工具结果是 json.dumps 之后给模型看的,形状变了模型会以为工具换了协议。
    """
    if isinstance(data, dict):
        merged = dict(data)
        merged.setdefault("notice", note)
        return merged
    if data is None:
        return {"notice": note}
    return {"result": data, "notice": note}


def _digest(params: dict[str, Any] | None) -> str:
    """参数摘要(不落原文,避免把敏感内容写进审计)。"""
    import hashlib
    import json

    payload = json.dumps(params or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def spec_token_cost(spec: ToolSpec) -> int:
    return estimate_tokens(spec.description)
