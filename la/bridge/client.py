"""兼容层:按配置造 LLM 客户端(实现已收敛到 la/llm/factory.py)。

⚠️ 保留这个模块只是为了让桥接层的 import 不用改;新代码请直接用
la.llm.factory.build_client。
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..llm.factory import build_client as _build_client


def build_client(settings: Settings) -> Any:
    return _build_client(settings)
