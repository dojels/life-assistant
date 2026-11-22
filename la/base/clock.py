"""时间小工具:全项目**一处**实现的 UTC 时间戳与 ISO 解析。

以前 _utc_now() 在 7 个模块里各写一遍、parse_iso() 有两份逐字相同的拷贝。
它们看着无害,但漂移一次就是"一半表存秒级、一半存微秒级",排序与去重都会错。
放 base/ 是因为它是叶子层,谁都能 import,不会造成新的分层倒挂。
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """UTC 时间戳(秒级,带 +00:00)。数据库里所有 *_at 的默认写法。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(text: str | None) -> datetime | None:
    """宽松解析 ISO8601;空串/坏格式一律返回 None(调用方自己决定怎么降级)。"""
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return None
