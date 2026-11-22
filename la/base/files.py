"""读文本、看目录 —— 基础层(M12)。

⭐ 三条工程约束(全部照抄 DSH @deepseek-ai/dsh-tool-fs,见 _research/DSH基础能力层调研报告.md §1):
  1. **带行号与续读指令**:模型要靠 offset 翻页,所以每行给行号,结尾给
     "(Showing lines X-Y of T. Use offset=N to continue.)" 这类 footer;
  2. **超长必须如实说**:单行超 2000 字符截断并标记,字节超上限提前停下并说明,
     **绝不静默丢内容** —— 否则模型会以为自己看到了全文,据此下结论;
  3. **offset 越界要报错**,不返回空字符串:空字符串会被理解成"文件是空的"。

为什么"读"只有一个工具:工具清单常驻每一次请求(现在已 40 个),
格式细节应该沉到 extract.py 里,而不是让模型去挑工具。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from .. import config

#: 解码顺序。⚠️ gbk 放最后:它能"成功"解出很多乱七八糟的字节,先用严格 utf-8 挡一道。
ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "gbk")


class BaseToolError(RuntimeError):
    """基础工具的失败。消息是**给用户/模型看的**,不夹栈信息。"""


@dataclass(frozen=True)
class ReadOutcome:
    """一次带窗口的读取结果。"""

    display_path: str
    lines: list[tuple[int, str]] = field(default_factory=list)
    total_lines: int = 0
    encoding: str = "utf-8"
    truncated_line: bool = False
    truncated_bytes: bool = False

    @property
    def first_line(self) -> int:
        return self.lines[0][0] if self.lines else 0

    @property
    def last_line(self) -> int:
        return self.lines[-1][0] if self.lines else 0


def decode_text(data: bytes) -> tuple[str, str]:
    """按 ENCODINGS 逐个试,返回 (文本, 实际编码)。全失败抛 BaseToolError。"""
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise BaseToolError("这份文件按 utf-8 / gbk 都解不出文本,可能是二进制(不是文本文件)")


def read_text_window(
    path: Path,
    *,
    offset: int = 1,
    limit: int = config.READ_MAX_LINES,
    max_line_chars: int = config.READ_MAX_LINE_CHARS,
    max_bytes: int = config.READ_MAX_BYTES,
) -> ReadOutcome:
    """读 [offset, offset+limit) 这些行。失败一律抛 BaseToolError(不抛 OSError)。"""
    if offset < 1:
        raise BaseToolError("offset 从 1 开始")
    if limit < 1:
        raise BaseToolError("limit 至少是 1")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BaseToolError("读不到这个文件:" + str(exc)) from exc
    if size > config.READ_MAX_FILE_BYTES:
        raise BaseToolError(
            "文件太大(" + str(size // (1 << 20)) + "MB,上限 "
            + str(config.READ_MAX_FILE_BYTES // (1 << 20)) + "MB);先用 file(action=list) 看清目录再决定读哪个"
        )
    data = path.read_bytes()
    if not data:
        return ReadOutcome(display_path=str(path), total_lines=0)
    text, encoding = decode_text(data)
    raw_lines = text.splitlines()
    total = len(raw_lines)
    if total and offset > total:
        raise BaseToolError(
            "起始行 " + str(offset) + " 超过文件总行数 " + str(total) + "(要读最后一段就用 offset="
            + str(max(1, total - limit + 1)) + ")"
        )

    picked: list[tuple[int, str]] = []
    used = 0
    truncated_line = False
    truncated_bytes = False
    for index in range(offset - 1, min(offset - 1 + limit, total)):
        line = raw_lines[index]
        if len(line) > max_line_chars:
            line = line[:max_line_chars] + " ... (line truncated to " + str(max_line_chars) + " chars)"
            truncated_line = True
        cost = len(line.encode("utf-8")) + 1
        if picked and used + cost > max_bytes:
            truncated_bytes = True
            break
        picked.append((index + 1, line))
        used += cost
    return ReadOutcome(
        display_path=str(path), lines=picked, total_lines=total, encoding=encoding,
        truncated_line=truncated_line, truncated_bytes=truncated_bytes,
    )


def render_read(outcome: ReadOutcome) -> str:
    """渲染成给模型看的信封:头一行说清"这是哪个文件、第几段、共多少行"。"""
    head = "文件:" + outcome.display_path
    if outcome.total_lines == 0:
        return head + "(空文件,0 行)"
    if outcome.encoding != "utf-8":
        head += "(编码 " + outcome.encoding + ")"
    if not outcome.lines:
        return head + "(没有可返回的行)"
    width = max(len(str(number)) for number, _ in outcome.lines)
    body = "\n".join(str(number).rjust(width) + "| " + text for number, text in outcome.lines)
    if outcome.last_line < outcome.total_lines:
        footer = ("(Showing lines " + str(outcome.first_line) + "-" + str(outcome.last_line)
                  + " of " + str(outcome.total_lines) + ". Use offset="
                  + str(outcome.last_line + 1) + " to continue.)")
    else:
        footer = "(End of file - total " + str(outcome.total_lines) + " lines)"
    notes: list[str] = []
    if outcome.truncated_line:
        notes.append("有超长行被截断(单行上限 " + str(config.READ_MAX_LINE_CHARS) + " 字符)")
    if outcome.truncated_bytes:
        notes.append("本次输出已达字节上限(" + str(config.READ_MAX_BYTES) + " 字节),提前停在这里")
    tail = ("\n⚠️ " + ";".join(notes)) if notes else ""
    return head + " " + footer + "\n" + body + tail


def list_directory(
    root: Path,
    *,
    pattern: str = "",
    recursive: bool = False,
    limit: int = config.LIST_MAX_ITEMS,
    max_depth: int = config.LIST_MAX_DEPTH,
) -> tuple[list[str], int, int]:
    """列目录,返回 (每行文本, 条目总数, 没显示的条数)。

    ⚠️ 三条纪律:
      * **不跟随符号链接** —— 一个软链就能把白名单绕过去;
      * 点开头的条目与 __pycache__ **一律不列**(.env / .git / .venv 没一个是模型该看的);
      * 目录在前、文件在后,各自按名字排序 —— 输出稳定,前缀缓存才复用得上。
    pattern 只筛文件,不筛目录(否则递归时子目录会被整棵剪掉)。
    """
    if not root.exists():
        raise BaseToolError("这个目录不存在:" + str(root))
    if not root.is_dir():
        raise BaseToolError("这是个文件,不是目录:" + str(root))
    entries: list[tuple[bool, str, str]] = []
    _collect(root, prefix="", depth=0, pattern=pattern, recursive=recursive,
             max_depth=max_depth, out=entries)
    entries.sort(key=lambda item: (0 if item[0] else 1, item[2]))
    rows = [item[1] for item in entries]
    total = len(rows)
    if total > limit:
        return rows[:limit], total, total - limit
    return rows, total, 0


def _collect(
    root: Path, *, prefix: str, depth: int, pattern: str, recursive: bool,
    max_depth: int, out: list[tuple[bool, str, str]],
) -> None:
    dirs: list[Path] = []
    ready: list[tuple[bool, str, str]] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name.startswith(".") or entry.name == "__pycache__" or entry.is_symlink():
            continue
        if entry.is_dir():
            dirs.append(entry)
            continue
        if pattern and not fnmatch.fnmatch(entry.name, pattern):
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        ready.append((False, prefix + entry.name + "  " + human_size(size), entry.name))
    for entry in dirs:
        out.append((True, prefix + entry.name + "/", entry.name))
    out.extend(ready)
    if not recursive or depth >= max_depth:
        return
    for entry in dirs:
        _collect(entry, prefix=prefix + entry.name + "/", depth=depth + 1, pattern=pattern,
                 recursive=recursive, max_depth=max_depth, out=out)


def human_size(size: int) -> str:
    if size < 1024:
        return str(size) + "B"
    if size < 1024 * 1024:
        return str(round(size / 1024, 1)) + "KB"
    return str(round(size / (1024 * 1024), 1)) + "MB"
