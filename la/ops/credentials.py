"""凭据分层读取:真实环境变量 > 配置文件 > .env。

为什么要有层:**临时实验不该改文件,长期配置不该塞进 shell 历史**。
真实环境变量优先级最高,所以 `$env:DEEPSEEK_API_KEY=xxx; python -m la` 这种一次性覆盖永远生效。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .. import config

SETTINGS_FILE = config.SETTINGS_FILE
SENSITIVE_NAMES = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LA_WEB_TOKEN")


# 解析器住在 config 里(config 不能反向依赖 ops),这里只是换个好记的名字。
_read_simple_yaml = config.read_settings_yaml


@dataclass
class CredentialReport:
    name: str
    source: str          # env | settings.yaml | .env | missing
    present: bool
    preview: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "source": self.source, "present": self.present,
                "preview": self.preview}


def mask(secret: str) -> str:
    """只留头尾,永远不要打印完整密钥。"""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return secret[:4] + "…" + secret[-4:]


def resolve(name: str, *, default: str | None = None) -> str | None:
    """按 真实环境变量 > settings.yaml > .env 的顺序取值。"""
    value = os.environ.get(name)
    if value:
        return value
    from_file = _read_simple_yaml(SETTINGS_FILE).get(name)
    if from_file:
        return from_file
    env_values = config.load_dotenv()
    if env_values.get(name):
        return env_values[name]
    return default


#: 页面设置的密钥对应的凭据名(D157 起密钥可以在设置页改)
PAGE_NAMES = {"DEEPSEEK_API_KEY": "llm.api_key", "DASHSCOPE_API_KEY": "tts.api_key"}


def _page_secret(conn, name: str) -> str:
    """从页面设置里取一份明文(只用于算掩码,不外传)。没有库/没有 key 文件就返回空。"""
    page_name = PAGE_NAMES.get(name)
    if conn is None or not page_name:
        return ""
    try:
        from .secrets import get_secret

        return get_secret(conn, page_name) or ""
    except Exception:  # noqa: BLE001 - 审计不该因为解不开一个密钥就整体失败
        return ""


def audit(conn=None) -> list[CredentialReport]:
    """列出已知凭据的来源与是否就位 —— **只给掩码,不给明文**。

    优先级与 config.get_settings 一致:真实环境变量 > 页面 > settings.yaml > .env。
    """
    from_file = _read_simple_yaml(SETTINGS_FILE)
    env_values = config.load_dotenv()
    reports: list[CredentialReport] = []
    for name in SENSITIVE_NAMES:
        page_value = _page_secret(conn, name)
        if os.environ.get(name):
            source = "env"
        elif page_value:
            reports.append(
                CredentialReport(
                    name=name, source="page", present=True, preview=mask(page_value)
                )
            )
            continue
        elif from_file.get(name):
            source = "settings.yaml"
        elif env_values.get(name):
            source = ".env"
        else:
            reports.append(CredentialReport(name=name, source="missing", present=False))
            continue
        reports.append(
            CredentialReport(
                name=name, source=source, present=True,
                preview=mask(os.environ.get(name) or from_file.get(name) or env_values[name]),
            )
        )
    return reports
