"""Web 的幂等闸门。

从 create_app() 里搬出来(2026-10-18 架构收口):它只依赖 Services 与
ops.idempotency,没有任何 app/模板状态,住在 app.py 的闭包里纯属历史原因。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from ..ops.idempotency import IdempotencyConflict, IdempotencyStore
from .deps import Services


def run_once(
    services: Services, token: str, payload: Any,
    action: Callable[[], Any], *, replay: Any = None,
) -> Any:
    """幂等闸门(M6.2)。

    ⚠️ 为什么用令牌而不是"检测双击":浏览器重试、用户狂点、断网重连、代理重放
    都会产生重复 POST,逐个识别不可靠。令牌是一次性的,判断是确定的。

    没有令牌(脚本/curl)= 不做幂等保护,原样执行 —— 免得静默吞掉真实操作。
    """
    if not token:
        return action()
    store = IdempotencyStore(services.assistant)
    decision = store.begin(token, payload)
    if not decision.should_process:
        # 只有“同键同载荷、且已完成”才能静默重放;冲突/进行中必须报出去,
        # 否则用户看到“成功”但什么都没写(2026-10-18 审计)。
        if decision.kind == "replay":
            return replay
        raise IdempotencyConflict(decision)
    try:
        result = action()
    except Exception as exc:
        store.fail(token, type(exc).__name__ + ": " + str(exc))
        raise
    store.complete(token, {"ok": True})
    return result if result is not None else replay


async def run_once_async(
    services: Services, token: str, payload: Any,
    action: Callable[[], Awaitable[Any]], *, replay: Any = None,
) -> Any:
    """run_once 的 await 版(记忆改口要过一次模型裁决,只能异步)。"""
    if not token:
        return await action()
    store = IdempotencyStore(services.assistant)
    decision = store.begin(token, payload)
    if not decision.should_process:
        if decision.kind == "replay":
            return replay
        raise IdempotencyConflict(decision)
    try:
        result = await action()
    except Exception as exc:
        store.fail(token, type(exc).__name__ + ": " + str(exc))
        raise
    store.complete(token, {"ok": True})
    return result if result is not None else replay
