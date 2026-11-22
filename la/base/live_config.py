"""进程内的"运行期配置覆盖"——零依赖、零 import。

为什么单独一个模块:config 负责"从文件读一次",这一份负责"运行中被人改了"。
把覆盖值放在这里,依赖方向就只有一个方向:**config 读这里,这里谁也不读**。
否则 config 与 ops 会绕成环(config 要读库里的密钥,而库连接要读 config 的路径)。

谁往里写:la.ops.secrets.sync() 从 assistant.db 读出来之后调用 apply()。
谁读:config.get_settings() 组装 Settings 时。
"""

from __future__ import annotations

#: 配置版本号。DB 里 sys_settings['config_rev'] 每次保存 +1,拿来判"要不要重建"。
_rev: int = 0
#: 覆盖值表。键名就是 DB 里的名字(如 llm.api_key / llm.base_url)。
_values: dict[str, str] = {}


def rev() -> int:
    return _rev


def values() -> dict[str, str]:
    return _values


def get(name: str, default: str | None = None) -> str | None:
    return _values.get(name, default)


def apply(values: dict[str, str], rev: int) -> None:
    """整份替换。**不做增量** —— 增量合并迟早会出现"删掉的键还在"的幽灵配置。"""
    global _rev, _values
    _rev = int(rev)
    _values = {str(k): str(v) for k, v in dict(values).items()}


def reset() -> None:
    """测试用:回到"没有任何覆盖"的初始状态。"""
    global _rev, _values
    _rev = 0
    _values = {}
