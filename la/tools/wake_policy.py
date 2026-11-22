"""唤醒会话的**参数级**写入守卫。

⚠️ 为什么不能只靠 `direct_write` / `risk`:
  * `risk` 是 **per-tool 单值**,而 `todo_write` / `schedule` / `quiz_write`
    这些合并工具的 delete 动作与 add 动作住在同一个工具名下 —— 工具级白名单挡不住
    `todo_write(action=delete)`;
  * `direct_write=True` 会把所有 `risk=confirm` 的工具一起放行(删笔记/取消日程),
    那不是"能写数据",那是"无人值守也能删数据"。

所以唤醒只放行**追加型**动作,破坏型一律拒。拒的理由要能让模型看懂,并转成报告里的
"这一项需要你回来做",而不是静默失败。
"""

from __future__ import annotations

from typing import Any

from .registry import ToolSpec

#: 读工具里的例外:它要等用户回答,而唤醒时没有人在。
DENY_READ_ONLY = frozenset({"ask_user"})

#: 写工具里**整条**拒绝的(不是"某个动作"):它们要么改身份/记忆,要么不可逆。
DENY_WRITE_TOOLS = frozenset({
    "persona_note",       # 改人格 = 改"她是谁"
    "memory_note",        # 无人确认,写了也不生效;不如明确拒
    "memory_correct",     # forget 分支会直接改记忆,不需要确认
    "memory_review",      # 人工放行,必须有人
    "spawn_sub_agents",   # 子代理会带出另一套写入
    "expense_import",     # 整批导入,爆炸半径太大
    "note_change",        # 移/删/改名
    "calendar_change",    # 改期/取消
    "goal",               # 目标是用户的决定
})

#: 整条放行的写工具(追加型)。
ALLOW_WRITE_TOOLS = frozenset({
    # ⚠️ 只放"新增"。确认草稿 / 纠正旧账都是**改已有数据**,留给用户回来做 ——
    #    否则"账本先落 draft 等确认"这道保险就被模型自己拆了。
    "expense_record",
    "note_add", "health_log", "calendar_schedule", "category_create",
    "artifact_write", "html_page", "tts_audio",
})

#: 合并工具:按 action 放行;没给 action 的默认动作也写在这里。
ALLOW_ACTIONS: dict[str, frozenset[str]] = {
    "todo_write": frozenset({"add", "update", "set_due", "set_status", "complete", "reopen"}),
    "schedule": frozenset({"create", "update", "list"}),
    "quiz_write": frozenset({"add", "result", "know", "push", "sheet"}),
}
DEFAULT_ACTION: dict[str, str] = {
    "todo_write": "add",
    "schedule": "create",
    "quiz_write": "add",
}

#: 这些动作虽然住在"追加型"工具里,但实际是破坏型。
DENY_ACTIONS: dict[str, frozenset[str]] = {
    "todo_write": frozenset({"delete"}),
    "schedule": frozenset({"delete"}),
    "quiz_write": frozenset({"delete"}),
}


def check(spec: ToolSpec, params: dict[str, Any] | None) -> str | None:
    """返回拒绝理由;None = 放行。`spec.writes=False` 的读工具默认放行。"""
    name = spec.name
    data = params or {}
    if not spec.writes:
        if name in DENY_READ_ONLY:
            return "唤醒时没人在旁边,不能问用户;把要问的写进报告。"
        return None
    if name in DENY_WRITE_TOOLS:
        return "这条唤醒是无人值守的,不能" + _why(name) + ";把它写进报告让用户回来做。"
    if name in ALLOW_ACTIONS:
        action = str(data.get("action") or DEFAULT_ACTION.get(name, "")).strip().lower()
        if action in DENY_ACTIONS.get(name, frozenset()):
            return "这条唤醒不允许执行 " + name + " 的 " + action + " 动作(会删掉数据)。"
        if action and action not in ALLOW_ACTIONS[name]:
            return "这条唤醒只允许 " + name + " 的 " + "/".join(sorted(ALLOW_ACTIONS[name])) + "。"
        return None
    if name in ALLOW_WRITE_TOOLS:
        if name == "artifact_write" and str(data.get("path") or "").strip():
            return "唤醒只能把产物写进产物区;覆盖工作区文件要用户回来做(传 path 被拒)。"
        return None
    return "这条唤醒不允许调用会写数据的工具:" + name + "。"


def _why(name: str) -> str:
    return {
        "persona_note": "改人格",
        "memory_note": "在无人确认时写记忆",
        "memory_correct": "在无人确认时改记忆",
        "memory_review": "替用户放行记忆候选",
        "spawn_sub_agents": "派生会写数据的子代理",
        "expense_import": "整批导入账单",
        "note_change": "移动/删除笔记",
        "calendar_change": "改期/取消日程",
        "goal": "替用户改目标",
    }.get(name, "调用 " + name)
