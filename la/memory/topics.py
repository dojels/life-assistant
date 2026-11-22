"""摘要桶的领域标签(2026-11-07 修)。

⭐ 之前 tags **永远**是空数组:压缩时硬编码 `tags=[]`(session.py),回填时只写
   内部标记 `["consolidated"]`(consolidate.py)。于是 `query_summary_pool` 的
   tags 参数、`retrieve(tags=...)` 的过滤、schema 里的 tags 列,全是死代码 ——
   模型按这个参数查,永远查不到。

现在标签由**代码确定**(不调模型、可复现),唯一真源是工具元数据里的 domain
(ledger/calendar/health/study/task/memory)与领域事件前缀:

    * 桶里出现过 tool/result -> 按工具名的 domain 打标;
    * 出现过 ledger.* / calendar.* / health.* / study.* 事件 -> 打对应标;
    * goal/change、schedule/change、todo/change -> task;
    * memory/note -> memory(这一桶里有明确的"记住"动作)。

⚠️ 框架自己的检索工具(query_summary_pool / query_timestamp_range / spawn_sub_agents)
   每轮都在日志里,不能拿它们当"这一桶聊的是记忆"的证据 —— internal 工具一律跳过。
⚠️ 标签是**能力域**,不是自由话题:纯聊"装修"而没有账目/日程/健康证据时,tags 可以是
   空的。空标签表示"没有可归的领域",不是"标签又坏了"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from ..tools.metadata import meta_for

#: 标签的固定顺序(渲染与测试都按它,避免集合顺序漂移)。
#: ⚠️ 加标签时三处一起改:这里、_EVENT_PREFIX_TAGS、tools/metadata.py 的 domain。
#:    "notes" 是 M33 加的:写笔记的那一桶摘要必须能被 tags 查到,否则模型按
#:    tags=笔记 过滤时永远空手而归(这就是 2026-11-07 修"tags 永远是空数组"的同一类 bug)。
DOMAIN_TAGS: tuple[str, ...] = (
    "ledger", "calendar", "health", "study", "task", "notes", "memory",
)

#: 领域事件前缀。前缀就是标签名 —— 新增领域时这里与 tools/metadata.py 一起改。
_EVENT_PREFIX_TAGS: frozenset[str] = frozenset({"ledger", "calendar", "health", "study"})

#: 事项层的事件类型(它们没有 domain 工具,只有 change 事件)。
_TASK_EVENTS: frozenset[str] = frozenset({"goal/change", "schedule/change", "todo/change"})

#: 中文别名 —— 模型/用户说"健康""账目"时也能过滤。键统一小写。
TAG_ALIASES: dict[str, str] = {
    "健康": "health",
    "账目": "ledger",
    "记账": "ledger",
    "账单": "ledger",
    "日程": "calendar",
    "日历": "calendar",
    "错题": "study",
    "学习": "study",
    "待办": "task",
    "任务": "task",
    "目标": "task",
    "记忆": "memory",
    # 笔记:用户说"笔记""日记""随笔"都该命中,工具/事件的领域名是 notes / journal。
    "笔记": "notes",
    "日记": "notes",
    "随笔": "notes",
    "本子": "notes",
    "journal": "notes",
}


def normalize_tag(value: object) -> str:
    """把用户/模型给的标签归一化:去空白、转小写、中文别名映射到领域名。"""
    text = str(value or "").strip().lower()
    return TAG_ALIASES.get(text, text)


def primary_domain(tags: Iterable[str]) -> str:
    """一组标签里的"主领域"(第一个领域标签);没有就返回空串。

    `consolidated` 这类内部标记不是领域,不能当分组依据。
    """
    for tag in tags:
        text = normalize_tag(tag)
        if text in DOMAIN_TAGS:
            return text
    return ""


def merge_tags(*groups: Iterable[str]) -> list[str]:
    """合并多组标签:领域标签按固定顺序在前,其余(如 consolidated)按字典序在后。"""
    found: set[str] = set()
    for group in groups:
        for tag in group:
            text = normalize_tag(tag)
            if text:
                found.add(text)
    known = [tag for tag in DOMAIN_TAGS if tag in found]
    extra = sorted(found - set(DOMAIN_TAGS))
    return known + extra


@dataclass(frozen=True)
class _EventView:
    """只用到 type/data 两个字段 —— 事件行与 SQL 行都能喂进来。"""

    type: str
    data: Any


def _data_of(event: Any) -> dict[str, Any]:
    data = getattr(event, "data", None)
    if isinstance(data, dict):
        return data
    if isinstance(data, str) and data.strip():
        try:
            loaded = json.loads(data)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def tags_from_events(events: Iterable[Any]) -> list[str]:
    """从一个桶的事件里推出领域标签。顺序固定(DOMAIN_TAGS),便于断言与缓存。"""
    found: set[str] = set()
    for event in events:
        etype = str(getattr(event, "type", "") or "")
        if not etype:
            continue
        prefix = etype.split(".", 1)[0]
        if prefix in _EVENT_PREFIX_TAGS:
            found.add(prefix)
            continue
        if etype in _TASK_EVENTS:
            found.add("task")
            continue
        if etype == "memory/note":
            found.add("memory")
            continue
        # 笔记域(M33):事件类型是 journal/...,标签叫 notes(与工具 domain 同名)。
        # ⚠️ 别指望 _EVENT_PREFIX_TAGS 接住它 —— 那里的前缀按 "." 切,而事件名用 "/"。
        if etype.startswith("journal/"):
            found.add("notes")
            continue
        if etype != "tool/result":
            continue
        data = _data_of(event)
        name = str(data.get("tool") or "")
        if not name:
            continue
        meta = meta_for(name)
        # ⚠️ 框架内部工具每轮都出现,不能当话题证据。
        if meta.get("internal"):
            continue
        # 写工具失败说明没写成,不算"这一桶聊过这个领域";只读工具问了就算。
        if data.get("ok") is False and not meta.get("read_only"):
            continue
        domain = str(meta.get("domain") or "")
        if domain in DOMAIN_TAGS:
            found.add(domain)
    return [tag for tag in DOMAIN_TAGS if tag in found]


def tags_from_rows(rows: Iterable[Any]) -> list[str]:
    """SQL 行版本:SELECT type, data FROM events 的结果直接喂进来。"""
    views: list[_EventView] = []
    for row in rows:
        try:
            etype = str(row["type"])
            data = row["data"]
        except (KeyError, IndexError, TypeError):
            try:
                etype = str(row[0])
                data = row[1]
            except (KeyError, IndexError, TypeError):
                continue
        views.append(_EventView(type=etype, data=data))
    return tags_from_events(views)
