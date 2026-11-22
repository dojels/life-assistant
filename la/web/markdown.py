"""安全的 Markdown 渲染:四层防护(能力域 §6.7.2 第 12 条)。

    L1 先转义  —— 输入里的所有 HTML 都变成实体,**原文永远不可能变成标签**
    L2 白名单解析 —— 只处理认识的 Markdown 子集,不认识的一律当纯文本
    L3 协议白名单 —— 链接只允许 http/https/mailto,其余降级成纯文本
    L4 只由本模块拼装 —— 没有任何一处把原始输入直接插进 HTML

**为什么不引第三方**:bleach 已归档;为了做白名单而引入一个完整的 HTML 解析器,
反而扩大了攻击面。本项目的渲染需求很窄(标题/粗体/代码/列表/链接/表格),自己写更可控。

⭐ 流式渲染的两个要点(云朵项目 §6.7.2 第 11 条):
  * **围栏感知边界**:不完整的代码围栏不渲染,否则每来一个字符都要重排;
  * **未闭合退化**:围栏还没闭合时,已收到的内容按代码块显示,而不是当普通文本。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

FENCE_MARK = "```"
FENCE = re.compile(r"^\s*" + FENCE_MARK + r"(\w*)\s*$")
HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
ORDERED = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
QUOTE = re.compile(r"^\s*>\s?(.*)$")
TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")
BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
AUTOLINK = re.compile(r"(?<![\"'=])(https?://[^\s<>()]+)")

SAFE_SCHEMES = ("http://", "https://", "mailto:", "/", "#")
STASH_OPEN = "@@STASH"
STASH_CLOSE = "@@"


def is_safe_url(url: str) -> bool:
    """协议白名单。javascript:/data:/vbscript: 一律降级成纯文本。"""
    candidate = (url or "").strip().lower()
    if not candidate:
        return False
    return candidate.startswith(SAFE_SCHEMES)


def escape(text: str) -> str:
    """L1:先转义。这一步之后,输入里的尖括号已经不可能变成标签。"""
    return html.escape(text or "", quote=True)


def render_inline(text: str) -> str:
    """输入必须是**已经转义过**的文本。输出是安全的 HTML 片段。"""
    safe = escape(text)
    placeholders: list[str] = []

    def stash(fragment: str) -> str:
        placeholders.append(fragment)
        return STASH_OPEN + str(len(placeholders) - 1) + STASH_CLOSE

    # 行内代码最先:它里面的东西不再参与任何其他解析
    safe = INLINE_CODE.sub(lambda m: stash("<code>" + m.group(1) + "</code>"), safe)

    def link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        if not is_safe_url(url):
            return match.group(0)  # L3:不在白名单 -> 降级成纯文本
        return stash(
            '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + "</a>"
        )

    safe = LINK.sub(link, safe)
    safe = BOLD.sub(lambda m: stash("<strong>" + m.group(1) + "</strong>"), safe)
    safe = ITALIC.sub(lambda m: stash("<em>" + m.group(1) + "</em>"), safe)

    def autolink(match: re.Match) -> str:
        url = match.group(1)
        if not is_safe_url(url):
            return match.group(0)
        return stash(
            '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + "</a>"
        )

    safe = AUTOLINK.sub(autolink, safe)
    for index, fragment in enumerate(placeholders):
        safe = safe.replace(STASH_OPEN + str(index) + STASH_CLOSE, fragment)
    return safe


@dataclass
class Rendered:
    html: str
    open_fence: bool = False
    language: str = ""


def render_markdown(text: str) -> Rendered:
    """把 Markdown 渲染成安全 HTML。未闭合的围栏会**退化成代码块**。"""
    lines = (text or "").split("\n")
    out: list[str] = []
    in_fence = False
    language = ""
    buffer: list[str] = []
    list_kind = ""
    table_buffer: list[str] = []

    def flush_paragraph() -> None:
        nonlocal buffer
        if buffer:
            out.append("<p>" + "<br>".join(render_inline(line) for line in buffer) + "</p>")
            buffer = []

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            out.append("</" + list_kind + ">")
            list_kind = ""

    def flush_table() -> None:
        nonlocal table_buffer
        if not table_buffer:
            return
        rows = [
            [cell.strip() for cell in row.strip().strip("|").split("|")]
            for row in table_buffer
            if not TABLE_SEP.match(row)
        ]
        table_buffer = []
        if not rows:
            return
        head, body = rows[0], rows[1:]
        parts = ["<table><thead><tr>"]
        parts.extend("<th>" + render_inline(cell) + "</th>" for cell in head)
        parts.append("</tr></thead><tbody>")
        for row in body:
            parts.append("<tr>")
            parts.extend("<td>" + render_inline(cell) + "</td>" for cell in row)
            parts.append("</tr>")
        parts.append("</tbody></table>")
        out.append("".join(parts))

    for line in lines:
        if FENCE.match(line):
            flush_paragraph()
            close_list()
            flush_table()
            if in_fence:
                out.append("</code></pre>")
                in_fence = False
            else:
                language = FENCE.match(line).group(1) or ""
                out.append('<pre><code class="lang-' + escape(language) + '">')
                in_fence = True
            continue
        if in_fence:
            out.append(escape(line))
            continue

        if TABLE_ROW.match(line):
            flush_paragraph()
            close_list()
            table_buffer.append(line)
            continue
        flush_table()

        if not line.strip():
            flush_paragraph()
            close_list()
            continue

        heading = HEADING.match(line)
        if heading:
            flush_paragraph()
            close_list()
            level = min(len(heading.group(1)) + 1, 5)
            out.append(
                "<h" + str(level) + ">" + render_inline(heading.group(2))
                + "</h" + str(level) + ">"
            )
            continue

        bullet = BULLET.match(line)
        ordered = ORDERED.match(line)
        if bullet or ordered:
            flush_paragraph()
            wanted = "ul" if bullet else "ol"
            if list_kind != wanted:
                close_list()
                out.append("<" + wanted + ">")
                list_kind = wanted
            content = bullet.group(1) if bullet else ordered.group(2)
            out.append("<li>" + render_inline(content) + "</li>")
            continue

        quote = QUOTE.match(line)
        if quote:
            flush_paragraph()
            close_list()
            out.append("<blockquote>" + render_inline(quote.group(1)) + "</blockquote>")
            continue

        close_list()
        buffer.append(line)

    flush_paragraph()
    close_list()
    flush_table()
    if in_fence:
        # 未闭合:补上闭合标签,内容已经按代码块转义过 —— 这就是"未闭合退化"
        out.append("</code></pre>")
    return Rendered(html="\n".join(out), open_fence=in_fence, language=language)


def safe_prefix_length(text: str) -> int:
    """可以安全渲染的前缀长度:到最后一个完整行,且**不切开代码围栏**。"""
    if not text:
        return 0
    index = text.rfind("\n")
    if index < 0:
        return 0
    prefix = text[: index + 1]
    if prefix.count(FENCE_MARK) % 2 == 1:
        start = prefix.rfind(FENCE_MARK)
        return prefix.rfind("\n", 0, start) + 1
    return index + 1


@dataclass
class StreamingMarkdown:
    """边收边渲染。只重渲染**已完成的行**,未完成的尾巴按纯文本附在后面。

    尾巴不渲染是因为它可能是半个 **粗体** 或半行表格 —— 渲染了会一闪一闪。
    """

    text: str = ""
    rendered: str = ""
    open_fence: bool = False

    def feed(self, delta: str) -> str:
        self.text += delta or ""
        cut = safe_prefix_length(self.text)
        result = render_markdown(self.text[:cut])
        self.open_fence = result.open_fence
        tail = self.text[cut:]
        parts = [result.html]
        if tail.strip():
            parts.append('<span class="pending">' + escape(tail) + "</span>")
        self.rendered = "\n".join(part for part in parts if part)
        return self.rendered
