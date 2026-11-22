"""笔记域的请求模型(**唯一真源**)。

REST 请求体与 LLM 工具参数是同一份 —— 工具层只做合并与分发,不再写第二份 schema
(能力域 §6.3)。字段的 max_length/ge 就是模型的约束,别在 service 里重写一遍。

⚠️ 这里刻意**不写业务判断**(夹存不存在、要不要查重)——那些属于 service。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: 笔记的品类。它不是"夹"(夹是树),而是"这条是什么"。
NOTE_KINDS = ("note", "musing", "idea", "excerpt")
#: 一条正文的硬上限。比微信单条上限(4000)宽得多,网页上可以写长文;
#: 再长就该拆成几条,或者写成产物文件。
MAX_BODY = 20_000
MAX_TITLE = 120
MAX_FOLDER = 200


class NoteAdd(BaseModel):
    """新建一条笔记。folder 是**路径字符串**('写作/长篇/《归途》'),不是 id。"""

    body: str = Field(..., min_length=1, max_length=MAX_BODY, description="正文。用户原话尽量照抄,不要替他润色。")
    title: str | None = Field(None, max_length=MAX_TITLE, description="标题;不给就用正文首句截断")
    folder: str | None = Field(
        None, max_length=MAX_FOLDER,
        description="放到哪个夹(路径,例如 写作/《归途》);不给就进「收件箱」",
    )
    kind: Literal["note", "musing", "idea", "excerpt"] = Field(
        "note", description="note=随手记/musing=感想/idea=灵感/excerpt=摘录"
    )
    ts: str | None = Field(None, max_length=40, description="事情发生的时刻 ISO8601;不给就是现在")
    force: bool = Field(
        False,
        description="确实要记一条和已有笔记很像的(比如同一天第二次练球)时传 true,跳过查重",
    )


class NoteAppend(BaseModel):
    """往已有笔记尾部追加一段 —— 长文分段、补一句话都走它。"""

    note_id: int = Field(..., ge=1, description="笔记编号(列表里的 #N)")
    body: str = Field(..., min_length=1, max_length=MAX_BODY, description="要追加的文字")
    separator: str = Field("\n\n", max_length=10, description="与原文之间的分隔符,默认空行")


class NoteEdit(BaseModel):
    """改一条笔记的标题或正文(只改传进来的字段)。"""

    note_id: int = Field(..., ge=1, description="笔记编号")
    title: str | None = Field(None, max_length=MAX_TITLE, description="新标题")
    body: str | None = Field(None, min_length=1, max_length=MAX_BODY, description="新正文(整段替换)")


class NoteMkdir(BaseModel):
    """新建一个文件夹(夹只能建在已有路径下,深度最多 6 层)。"""

    name: str = Field(..., min_length=1, max_length=40, description="夹名,不含 /")
    parent: str | None = Field(
        None, max_length=MAX_FOLDER, description="父夹路径;不给就建在根上"
    )


class NoteSearch(BaseModel):
    query: str = Field(..., min_length=1, max_length=200, description="搜什么(关键词即可,不必是整句)")
    folder: str | None = Field(None, max_length=MAX_FOLDER, description="只在这个夹(含子夹)里找")
    limit: int = Field(10, ge=1, le=25, description="最多几条")


class NoteTree(BaseModel):
    """看夹的目录结构。"""


class NoteRecent(BaseModel):
    limit: int = Field(10, ge=1, le=25, description="最多几条")
    folder: str | None = Field(None, max_length=MAX_FOLDER, description="只看这个夹(含子夹)")


class NoteShow(BaseModel):
    note_id: int = Field(..., ge=1, description="笔记编号")
    full: bool = Field(False, description="true = 返回全文;默认只给前 800 字")


class NoteMove(BaseModel):
    note_id: int = Field(..., ge=1, description="笔记编号")
    new_folder: str = Field(..., max_length=MAX_FOLDER, description="目标夹路径(必须已存在)")


class NoteRename(BaseModel):
    folder: str = Field(..., max_length=MAX_FOLDER, description="要改名的夹路径")
    name: str = Field(..., min_length=1, max_length=40, description="新名字(不含 /)")


class NoteDelete(BaseModel):
    note_id: int | None = Field(None, ge=1, description="删这一条(进回收站)")
    folder: str | None = Field(None, max_length=MAX_FOLDER, description="删这个夹(必须已经空了)")


class NoteRestore(BaseModel):
    note_id: int = Field(..., ge=1, description="从回收站恢复这一条")
