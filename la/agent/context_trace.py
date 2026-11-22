"""轨迹(D118):把每一轮**真正发出去的那份上下文**原样落一份。

为什么要有这一层:

  * 记忆页 L4/L5 回答的是"规则上会注入什么",人格页回答的是"系统提示里写的是什么";
    但用户真正想核对的是同一件事 —— **这一轮模型到底看到了什么**;
  * 而它是**过程数据**:投影装配完就交给 API 了,事件日志里只有 user/assistant 两条消息,
    投影自己不落盘。于是"它看到了什么"这个问题今天只能靠**重算**回答 ——
    重算一定会漂移(检索结果、人格、技能目录、时间都可能是之后的样子),
    而重算出来的东西与当时真正发出去的东西在页面上**长得一模一样**。
    那正是这一页最该避免的欺骗:看不到不可怕,看到一份错的才可怕(同 D59/D118)。

所以:每轮装配完(MemorySession.respond)把 messages **原样**写进 data/trace/context.jsonl,
页面(/trace)读的永远是**发出去的那一份**,不是现在重算出来的那一份。

四条纪律:

  1. **写盘失败绝不影响对话** —— 轨迹是观测,不是主流程(与 step_ledger._push 同源):
     磁盘满了、目录没了、JSON 里混进了不可序列化的东西,这一轮照样要答出来;
  2. **不进 memory.db、不进事件日志** —— 轨迹是观测数据,不是"发生过的事"。
     写进 events 会被摘要池收进去(之后检索"上次聊到什么"会翻出一堆提示词快照),
     而且它每轮一份、体积比对话本身大得多(与 D117 第 6 条同一条取舍);
  3. **目录跟着库走,而且是现算的**:根目录由调用方给(会话把"这一份记忆住在哪"告诉它),
     默认才是 config.DATA_DIR。⭐ 为什么不能只看 config.DATA_DIR:实测踩过 ——
     tests/test_m1 用**临时库 + 临时归档**跑二十轮对话,而 config.DATA_DIR 还指着真实目录,
     于是真实的 data/trace 里长出了 40 条测试对话。轨迹**跟着库走**,这种污染从结构上就不可能;
  4. **保留最近 MAX_RECORDS 份**,超出丢最旧的。轨迹是"最近发生了什么",不是档案 ——
     要看更久以前的,那是归档(archive/)与摘要池的事。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config

#: 记录格式版本。字段只增不改 —— 页面按 key 取,缺 key 就当没有,不猜。
RECORD_VERSION = 1

#: 轨迹目录名。⚠️ 只拼路径,**不做成 config 常量**:常量在 import 期就定死了,
#: 而"这一份记忆住在哪"是**运行时**才知道的事(见纪律 3)。
TRACE_SUBDIR = "trace"
TRACE_FILE_NAME = "context.jsonl"

#: 保留多少轮。40 轮 ≈ 1~2 MB(每轮约 30~50KB),够看清"刚才那几句它到底看到了什么"。
MAX_RECORDS = 40


def trace_path_for(root: Path | str | None = None) -> Path:
    """轨迹文件在哪。`root` 是**这一份记忆住的那个数据目录**(通常是归档的上一级)。

    ⭐ 每次**现算**,而且优先用调用方给的根:
       * 会话把 store 的归档目录告诉它 —— 库在临时目录,轨迹就在临时目录(见纪律 3);
       * 页面(读的那一侧)不传,回落到 config.DATA_DIR。
    """
    base = Path(root) if root is not None else Path(config.DATA_DIR)
    return base / TRACE_SUBDIR / TRACE_FILE_NAME


def trace_path() -> Path:
    """生产口径的轨迹文件(数据目录下的 trace/context.jsonl)。"""
    return trace_path_for(None)


def build_record(
    projection: Any,
    *,
    session_id: str,
    turn: int,
    surface: str = "",
    provider: str = "",
    model: str = "",
    detail: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """把一次投影翻成一条**自描述**的记录。

    ⚠️ messages 是**逐字**抄下来的(不是摘要、不是预览):这个页面存在的全部意义
       就是"看到的就是发出去的"。要压缩的是**投影本身**(由 projector 的预算管),
       不是这份记录 —— 在这里截断等于把一份假证据摆给人看。
    """
    labels = list(getattr(projection, "message_segments", None) or [])
    messages: list[dict[str, Any]] = []
    for index, message in enumerate(projection.messages):
        content = str(message.get("content") or "")
        reasoning = message.get("reasoning_content")
        item: dict[str, Any] = {
            "role": str(message.get("role") or ""),
            # 段名由**装配器**给出(Projection.message_segments),不在这里猜顺序
            "segment": labels[index] if index < len(labels) else "",
            "tokens": config.estimate_tokens(content) + 8,
            "content": content,
        }
        if reasoning:
            item["reasoning"] = str(reasoning)
            item["tokens"] += config.estimate_tokens(str(reasoning))
        messages.append(item)

    segments = {str(key): int(value) for key, value in dict(projection.segments).items()}
    record: dict[str, Any] = {
        "v": RECORD_VERSION,
        "at": config.format_local(at) if at is not None else "",
        #: 落盘时刻(epoch 毫秒):页面不该靠解析那行本地时间字符串来排序
        "at_ms": int(at.timestamp() * 1000) if at is not None else 0,
        "session": str(session_id or ""),
        "turn": int(turn or 0),
        "surface": str(surface or ""),
        "provider": str(provider or ""),
        "model": str(model or ""),
        "segments": segments,
        "total": int(getattr(projection, "total_tokens", 0) or 0),
        "tools": int(getattr(projection, "tools_tokens", 0) or 0),
        #: 对话段**实际**拿到的预算(被硬上限收过就不是默认值)。页面要说"被压到多少"。
        "conversation_budget": int(getattr(projection, "conversation_budget", 0) or 0),
        "grand_total": int(getattr(projection, "grand_total", 0) or 0),
        "dropped_turns": int(getattr(projection, "dropped_turns", 0) or 0),
        "capped": bool(getattr(projection, "capped", False)),
        # ⭐ D122:这一轮里"下一轮还能原样重放"的那一段 —— 它就是下一轮能命中的前缀
        "cache_prefix": int(getattr(projection, "cache_prefix_tokens", 0) or 0),
        "needs_compaction": bool(getattr(projection, "needs_compaction", False)),
        "messages": messages,
    }
    if detail:
        # 系统提示由哪几段拼成、以及**裁剪前**每段多大(页面据此标出"这一段被截尾了")
        if detail.get("parts"):
            record["parts"] = [dict(item) for item in detail["parts"]]
        if detail.get("pre_clip"):
            record["pre_clip"] = {str(k): int(v) for k, v in detail["pre_clip"].items()}
        if "persona_custom" in detail:
            record["persona_custom"] = bool(detail.get("persona_custom"))
        if "persona_chars" in detail:
            record["persona_chars"] = int(detail.get("persona_chars") or 0)
    return record


def record_context(
    projection: Any,
    *,
    session_id: str,
    turn: int,
    surface: str = "",
    provider: str = "",
    model: str = "",
    detail: dict[str, Any] | None = None,
    at: datetime | None = None,
    path: Path | str | None = None,
    max_records: int = MAX_RECORDS,
) -> dict[str, Any] | None:
    """落一条轨迹。**绝不抛异常**(纪律 1):写不进去就返回 None,这一轮照常答。

    ⭐ 为什么返回记录而不是 True/False:调用方(会话)不需要它,但验收脚本与测试
       需要一个"到底落成了什么"的东西来对照。返回 None = 没落成。
    """
    try:
        record = build_record(
            projection, session_id=session_id, turn=turn, surface=surface,
            provider=provider, model=model, detail=detail, at=at,
        )
        target = Path(path) if path is not None else trace_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        _prune(target, max_records=max_records)
        return record
    except Exception:  # noqa: BLE001 - 轨迹写不进去,不该让这一轮答不出来
        return None


def read_records(
    *, limit: int = MAX_RECORDS, path: Path | str | None = None
) -> list[dict[str, Any]]:
    """读最近若干条轨迹,**新的在前**。

    ⚠️ 坏行**跳过**而不是整页失败:一条记录写坏了(断电、磁盘满)不该让这一页打不开 ——
       与记忆页 L0 那份归档的读法同一条口径。
    """
    target = Path(path) if path is not None else trace_path()
    if not target.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("messages") is not None:
                    records.append(payload)
    except OSError:
        return []
    records.reverse()
    return records[: max(int(limit), 0)]


def count_records(*, path: Path | str | None = None) -> int:
    """文件里现在有几条(**含坏行之外的所有记录**)。页面用它说"保留最近 N 条"。"""
    target = Path(path) if path is not None else trace_path()
    if not target.exists():
        return 0
    try:
        with target.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def _prune(target: Path, *, max_records: int) -> None:
    """超出上限时**重写**一份(保留最新那些)。

    ⚠️ 不另加锁:轨迹只有一个写者(每轮对话结束那一次),而重写是
       "读全部 -> 留尾部 -> 原子替换(os.replace)"。万一并发,最坏的结果是丢掉
       一条轨迹 —— 它不是事实源,丢了不影响任何数据(见纪律 1/4)。
    """
    try:
        with target.open("r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
        if len(lines) <= max_records:
            return
        kept = lines[-max_records:]
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        return


__all__ = [
    "MAX_RECORDS",
    "RECORD_VERSION",
    "TRACE_FILE_NAME",
    "TRACE_SUBDIR",
    "build_record",
    "count_records",
    "read_records",
    "record_context",
    "trace_path",
    "trace_path_for",
]
