"""按配置造 LLM 客户端 —— **全项目唯一实现**。

以前同一段代码有三份(__main__.build_client、bridge/client.build_client、
web/deps.build_model_client),三处各自 import DeepSeekClient/FakeClient。
任何一处改参数(超时/重试/模型名)都要记得改三次,漏一处就是"网页能用、微信不能用"。
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from .client import DeepSeekClient
from .fake import FakeClient


def build_client(settings: Settings) -> Any:
    """按 provider 造客户端。fake 用于离线开发与验收(见 .env.example)。"""
    if settings.provider == "fake":
        return FakeClient()
    return DeepSeekClient(
        api_key=settings.api_key,
        base_url=settings.base_url,
        model=settings.model,
        timeout_s=settings.request_timeout_s,
        max_retries=settings.max_retries,
        reasoning_effort=settings.reasoning_effort,
    )
