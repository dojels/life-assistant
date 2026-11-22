"""Web 层的纯工具:不依赖 FastAPI app,也不捕获任何请求状态。

2026-10-18 架构收口:这些函数以前住在 create_app() 的闭包里,导致
任何按域拆出去的路由模块都得通过参数层层转发。它们本身没有状态,
搬到这里之后路由模块可以直接 import。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi.responses import RedirectResponse

#: 网页上传的硬上限。桥接层早就有 MAX_MEDIA_BYTES;网页三个入口也必须同一口径。
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


def int_or(text: Any, default: int, *, low: int, high: int) -> int:
    """查询串里的整数收敛到 [low, high];认不出来用 default(**不报错**)。"""
    try:
        return max(low, min(int(str(text).strip()), high))
    except (TypeError, ValueError):
        return default


def safe_next(raw: str) -> str:
    """只允许站内绝对路径,挡住 //evil.com 与反斜杠变体。"""
    text = str(raw or "").strip()
    if text.startswith("/") and not text.startswith("//") and "\\" not in text:
        return text
    return "/chat"


def flash(path: str, message: str, *, error: bool = False) -> RedirectResponse:
    """303 回跳 + 一条 flash(class 由 error 决定)。"""
    path = safe_next(path)
    separator = "&" if "?" in path else "?"
    key = "err" if error else "ok"
    return RedirectResponse(
        path + separator + key + "=" + quote(str(message)[:200]), status_code=303
    )
