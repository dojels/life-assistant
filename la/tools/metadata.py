"""工具元数据的**唯一真源**(2026-10-18 审计修)。

以前同一个工具的名字散在四处:session.TOOL_TITLES(中文标题)、
step_ledger.TOOL_KINDS(前端图标/领域)、registry.READ_ONLY_TOOLS(只读白名单)、
session.INTERNAL_TOOLS(框架自己发起、不开步骤行)。新增一个工具要记得改四遍,
漏一处不会报错,只会"显示难看 / 只读闸多拦一次 / 步骤行重复一行"。

现在只在这里写一次,四处都从它派生:

    TOOL_META[name] = {
        "title":    中文说法(步骤行/前端显示)
        "domain":   ledger | calendar | health | study | task | memory | system
        "kind":     tool | task        (前端图标;内部工具用 task)
        "internal": True = 框架自己发起,工具层不开步骤行
        "read_only":True = 只读工具(只读模式放行、瞬时错误可自动重试)
    }

⚠️ 没登记的工具不会报错:标题退回 name.replace("_", " ")、领域为空、
   只读=False(安全方向是"多拦")。新增工具时**请在这里补一行**。
"""

from __future__ import annotations

from typing import Any

TOOL_META: dict[str, dict[str, Any]] = {
    # ---- 记账 ----
    "expense_record": {"title": "记一笔支出", "domain": "ledger", "kind": "tool"},
    "expense_query": {"title": "查支出统计", "domain": "ledger", "kind": "tool", "read_only": True},
    "expense_correct_last": {"title": "纠正一笔账", "domain": "ledger", "kind": "tool"},
    "expense_confirm": {"title": "确认入账", "domain": "ledger", "kind": "tool"},
    "expense_import": {"title": "导入账单", "domain": "ledger", "kind": "tool"},
    "ledger_categories": {"title": "看分类清单", "domain": "ledger", "kind": "tool", "read_only": True},
    "category_create": {"title": "新建分类", "domain": "ledger", "kind": "tool"},
    # ---- 日程 ----
    "calendar_schedule": {"title": "建日程", "domain": "calendar", "kind": "tool"},
    "calendar_list": {"title": "查日程", "domain": "calendar", "kind": "tool", "read_only": True},
    "calendar_change": {"title": "改日程/取消日程", "domain": "calendar", "kind": "tool"},
    # ---- 健康 ----
    "health_log": {"title": "记健康数据", "domain": "health", "kind": "tool"},
    "health_plan": {"title": "健康(目标/趋势/能量)", "domain": "health", "kind": "tool", "read_only": True},
    # ---- 错题 ----
    "quiz_find": {"title": "查错题/取复习清单", "domain": "study", "kind": "tool", "read_only": True},
    "quiz_write": {"title": "错题写入(录题/记结果/推送/出卷)", "domain": "study", "kind": "tool"},
    "tts_audio": {"title": "生成语音", "domain": "study", "kind": "tool"},
    # ---- 事项 ----
    "todo_list": {"title": "看待办", "domain": "task", "kind": "tool", "read_only": True},
    "todo_write": {"title": "记待办", "domain": "task", "kind": "task", "internal": True},
    "goal_get": {"title": "看目标", "domain": "task", "kind": "tool", "read_only": True},
    "goal": {"title": "定目标/改目标", "domain": "task", "kind": "task", "internal": True},
    "schedule": {"title": "提醒(定/改/删/看)", "domain": "task", "kind": "task", "internal": True},
    # ---- 记忆 ----
    "memory_recall": {"title": "看记忆里的事实", "domain": "memory", "kind": "tool", "read_only": True},
    "memory_correct": {"title": "改一条记忆", "domain": "memory", "kind": "task"},
    "memory_note": {"title": "写进记忆", "domain": "memory", "kind": "task"},
    "memory_review": {"title": "放行待审的记忆", "domain": "memory", "kind": "task"},
    "persona_note": {"title": "写进人格", "domain": "system", "kind": "task"},
    "compact_context": {"title": "压缩上下文", "domain": "memory", "kind": "task"},
    "add_fact": {"title": "记一条永久事实", "domain": "memory", "kind": "task"},
    "query_summary_pool": {
        "title": "检索长期记忆", "domain": "memory", "kind": "task",
        "internal": True, "read_only": True,
    },
    "query_archive_search": {
        "title": "翻聊天档案", "domain": "memory", "kind": "tool", "read_only": True,
    },
    "query_timestamp_range": {
        "title": "按时间回查原文", "domain": "memory", "kind": "task",
        "internal": True, "read_only": True,
    },
    "spawn_sub_agents": {
        "title": "并行挖掘多个时段", "domain": "memory", "kind": "task", "internal": True,
    },
    # ---- 笔记 ----
    # 笔记 ≠ 记忆:这三条住在 assistant.db,不可重建、不衰减、可编辑。
    "note_find": {"title": "翻笔记", "domain": "notes", "kind": "tool", "read_only": True},
    "note_add": {"title": "记笔记", "domain": "notes", "kind": "tool"},
    "note_change": {"title": "整理笔记(移/删/改名)", "domain": "notes", "kind": "tool"},
    # ---- 基础能力层 ----
    "file": {"title": "读文件/看目录", "domain": "system", "kind": "tool", "read_only": True},
    "image_view": {"title": "看图", "domain": "system", "kind": "tool", "read_only": True},
    "material_list": {
        "title": "看收到过的素材", "domain": "system", "kind": "tool", "read_only": True,
    },
    "artifact_write": {"title": "写产物文件", "domain": "system", "kind": "tool"},
    "html_page": {"title": "做网页", "domain": "system", "kind": "tool"},
    "web": {"title": "联网搜索/打开网页", "domain": "system", "kind": "tool", "read_only": True},
    "skill": {"title": "看/加载技能", "domain": "system", "kind": "tool", "read_only": True},
    "self_inspect": {"title": "看自己的行为账", "domain": "system", "kind": "tool", "read_only": True},
    "page_view": {"title": "看一眼自己写的网页", "domain": "system", "kind": "tool", "read_only": True},
    "ask_user": {"title": "问用户一句", "domain": "system", "kind": "tool", "read_only": True},
    "wake_report": {
        "title": "交唤醒报告", "domain": "system", "kind": "tool", "read_only": True,
    },
}


def meta_for(name: str) -> dict[str, Any]:
    return TOOL_META.get(str(name), {})


def read_only_names() -> tuple[str, ...]:
    return tuple(sorted(name for name, meta in TOOL_META.items() if meta.get("read_only")))


def titles() -> dict[str, str]:
    return {name: str(meta.get("title") or "") for name, meta in TOOL_META.items() if meta.get("title")}


def kinds() -> dict[str, tuple[str, str]]:
    """tool:<名字> -> (前端 kind, 领域)。与 step_ledger 的 KIND_TOOL/KIND_TASK 对齐。"""
    out: dict[str, tuple[str, str]] = {}
    for name, meta in TOOL_META.items():
        kind = "task" if str(meta.get("kind") or "tool") == "task" else "tool"
        out[name] = (kind, str(meta.get("domain") or ""))
    return out


def internal_names() -> frozenset[str]:
    return frozenset(name for name, meta in TOOL_META.items() if meta.get("internal"))
