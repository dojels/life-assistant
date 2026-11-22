"""Markdown 的一个**小**子集 —— 只为"生成正式文档"服务(M12 / P1-4)。

⭐ 为什么刻意只支持这么点语法:这是**模型写、代码读**的合同。合同越小,
   模型越不容易写出"看着像语法、其实解析不了"的东西;而真要写复杂排版,
   应该加新语法并同时加测试,而不是让解析器去猜。

支持(其余一律当普通段落,不当错误 —— 宁可少渲染,不可丢掉内容):
    ---                    frontmatter(title / subtitle / author / date / header)
    # ## ### ####          标题
    - / * / •              无序列表
    1. 2.                  有序列表
    | a | b |              表格(第二行是 |---|---| 分隔行)
    **粗体**               行内加粗
    \\newpage              分页
    普通一行               正文段落
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FENCE = "---"
PAGEBREAK = "\\newpage"
HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
BULLET_RE = re.compile(r"^\s*[-*•]\s+(.*)$")
NUMBER_RE = re.compile(r"^\s*(\d+)[.、)]\s+(.*)$")
TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
TABLE_DIVIDER_RE = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


@dataclass
class Block:
    """一个块级元素。kind ∈ heading / paragraph / bullet / number / table / pagebreak。"""

    kind: str
    text: str = ""
    level: int = 1
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class Document:
    meta: dict[str, str] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)

    @property
    def title(self) -> str:
        if self.meta.get("title"):
            return self.meta["title"]
        for block in self.blocks:
            if block.kind == "heading" and block.level == 1 and block.text:
                return block.text
        return ""

    @property
    def headings(self) -> list[str]:
        return [block.text for block in self.blocks if block.kind == "heading" and block.text]

    @property
    def tables(self) -> list[list[list[str]]]:
        return [block.rows for block in self.blocks if block.kind == "table"]


def parse(text: str) -> Document:
    """把 Markdown 子集解析成块序列。**不认识的行当段落**,不抛异常。"""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    meta, start = _parse_frontmatter(lines)
    blocks: list[Block] = []
    index = start
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if stripped == PAGEBREAK:
            blocks.append(Block(kind="pagebreak"))
            index += 1
            continue
        heading = HEADING_RE.match(stripped)
        if heading:
            blocks.append(Block(kind="heading", text=heading.group(2).strip(),
                                level=len(heading.group(1))))
            index += 1
            continue
        if TABLE_ROW_RE.match(stripped):
            rows: list[list[str]] = []
            while index < len(lines) and TABLE_ROW_RE.match(lines[index].strip()):
                raw = lines[index].strip()
                if TABLE_DIVIDER_RE.match(raw):
                    index += 1
                    continue
                rows.append(_split_row(raw))
                index += 1
            if rows:
                blocks.append(Block(kind="table", rows=rows))
            continue
        bullet = BULLET_RE.match(line)
        if bullet:
            blocks.append(Block(kind="bullet", text=bullet.group(1).strip()))
            index += 1
            continue
        number = NUMBER_RE.match(line)
        if number:
            blocks.append(Block(kind="number", text=number.group(2).strip()))
            index += 1
            continue
        blocks.append(Block(kind="paragraph", text=stripped))
        index += 1
    return Document(meta=meta, blocks=blocks)


def _parse_frontmatter(lines: list[str]) -> tuple[dict[str, str], int]:
    """只认文件最开头的 --- ... ---(与 skim 技能的 frontmatter 同一种写法)。

    ⚠️ 值里不解析引号/转义 —— 这是"给人写标题用的",不是配置格式。
    """
    if not lines or lines[0].strip() != FENCE:
        return {}, 0
    meta: dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index].strip()
        if line == FENCE:
            return meta, index + 1
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    return {}, 0


def _split_row(raw: str) -> list[str]:
    inner = raw.strip().strip("|")
    return [cell.strip() for cell in inner.split("|")]


def inline_bold(text: str) -> list[tuple[str, bool]]:
    """把一行拆成 (文本, 是否加粗) 的片段。不处理嵌套 —— 合同里没有嵌套。"""
    parts: list[tuple[str, bool]] = []
    cursor = 0
    for match in BOLD_RE.finditer(text or ""):
        if match.start() > cursor:
            parts.append((text[cursor:match.start()], False))
        parts.append((match.group(1), True))
        cursor = match.end()
    if cursor < len(text or ""):
        parts.append((text[cursor:], False))
    return parts or [(text or "", False)]


__all__ = ["Block", "Document", "parse", "inline_bold", "PAGEBREAK"]
