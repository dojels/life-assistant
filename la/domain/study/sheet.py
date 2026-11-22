"""错题卷(单文件 HTML):**数据 + 模板 = 一个能写、能点、能出声、能打印的页面**。

⭐ 为什么出卷这一步一行模型代码都没有(用户 2026-10-12 拍板的方案):

  1. **快且稳**:题目、答案、图形在**录入时**就已经算好存库了;出卷只是把库里的数字
     填进模板。毫秒级,而且同一个输入渲染两次**逐字一致**(金样本测试的前提)。
  2. **不依赖 AI 可用**:模型欠费、断网、挂了,照样能出卷、能打印、能发微信 ——
     与"领域数据的读取路径不含 LLM"是同一条纪律(能力域 §7 第①条)。
  3. **可回归**:模板有版本号,写进产物 meta;三个月后还能重现今天那张卷子。

⚠️ 与 build_review_sheet(那是 Markdown 版)的分工:
    Markdown 版走微信聊天正文(不点链接也能看);本模块走**单文件 HTML**
    (图片/音频全部内联成 data:,双击就能打开,发到微信里点开就是一个真页面)。

两条"不许静默"的规矩:

  * 答案必须**带来源**。老数据(answer_source='legacy')一律标成
    "照片上的原答案,未核对",绝不冒充标准答案(实测:错题 #5 抄进来的就是
    "孩子算的 65 分钟 + 老师订正 54 分钟"两段混在一起)。
  * 图形缺失要**说出来**。"这道题有图但没裁出来"必须显示成一行提示,
    不能悄悄少一张图让孩子以为题目就长这样。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

#: ⚠️ v2 = 每题可以带一段**录入时就绑好的**音频(听力/听写题)。模板一变就要改版本号,
#:    否则三个月后重现旧卷子会得到一张"多出播放器"的页面却查不出原因。
TEMPLATE_VERSION = "sheet-v2"
TEMPLATE_NAME = "sheet.html.j2"

#: 答案来源 → 给人看的说法。⚠️ 每一句都必须是**保守**的:宁可说"未核对",不能暗示已确认。
SOURCE_LABELS = {
    "model": "模型独立解答(未经家长确认)",
    "teacher": "老师批改",
    "manual": "家长填写",
    "legacy": "照片上的原答案(可能是孩子的错答,未核对)",
    "none": "暂无答案",
}

STATUS_LABELS = {
    "agreed": "与老师批改一致",
    "conflict": "三方不一致,需要核对",
    "incomplete": "题干或图形不全,答案待补",
    "unverified": "未核对",
}

#: 冲突/残缺时不进"参考答案"区的两种状态
BLOCKING_STATUS = ("conflict", "incomplete")


def label_for(source: str, status: str) -> str:
    """一句话说清"这个答案是谁给的、可不可信"。"""
    base = SOURCE_LABELS.get(str(source or "").strip().lower(), SOURCE_LABELS["none"])
    extra = STATUS_LABELS.get(str(status or "").strip().lower(), "")
    if extra and extra not in base:
        return base + " · " + extra
    return base


def data_uri(payload: bytes, media_type: str) -> str:
    if not payload:
        return ""
    return "data:" + (media_type or "application/octet-stream") + ";base64," + base64.b64encode(payload).decode("ascii")


def steps_from_raw(raw: Any) -> list[str]:
    """solution_steps 存的是 JSON 数组,也容忍换行分隔的纯文本(手填的情况)。"""
    if not raw:
        return []
    text = str(raw).strip()
    if text.startswith("["):
        try:
            import json

            loaded = json.loads(text)
            if isinstance(loaded, list):
                return [str(item).strip() for item in loaded if str(item).strip()]
        except Exception:  # noqa: BLE001 - 解析不了就当纯文本
            pass
    return [line.strip(" -·*") for line in text.splitlines() if line.strip()]


@dataclass
class SheetProblem:
    """卷面上的一道题。**字段与模板一一对应**,模板里不出现任何业务判断。"""

    index: int = 0
    subject: str = ""
    stem: str = ""
    kps: list[str] = field(default_factory=list)
    answer: str = ""
    steps: list[str] = field(default_factory=list)
    answer_source: str = ""
    answer_status: str = ""
    answer_label: str = ""
    student_answer: str = ""
    figure_uri: str = ""
    #: 矢量重绘出来的图形(**白名单渲染器出的 SVG 源码**,内联进页面)。
    #: 有它就用它当主图 —— 清晰、能打印、不怕缩放;裁剪图仍然留着当证据。
    figure_svg: str = ""
    figure_note: str = ""
    #: 🔊 这道题的**声音素材**(听力/听写)。⭐ 它在**录入时**就生成并绑在题上了
    #: (见 study/audio.py):出卷只是把它内联进来。所以同一道题每次复习听到的是同一段,
    #: 也不会为"再念一遍"花 token 与语音费。
    audio_uri: str = ""
    audio_label: str = ""
    #: 有 uri = 播放器下面那行说明;没有 uri 但有它 = "这题本来该有声音,为什么没有"
    audio_note: str = ""
    conflict_note: str = ""
    unit_ids: list[int] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return str(self.answer_status or "").strip().lower() in BLOCKING_STATUS

    @property
    def needs_attention(self) -> bool:
        return self.blocked or bool(self.conflict_note)


@dataclass
class SheetModel:
    """一份卷子的**全部**数据。渲染器只认它,不碰数据库。"""

    who: str = "孩子"
    day: str = ""
    title: str = ""
    minutes: int = 10
    items: list[SheetProblem] = field(default_factory=list)
    template_version: str = TEMPLATE_VERSION
    generated_at: str = ""
    #: 听写段(可选):词表 + 内联音频 + 备注
    dictation_words: list[str] = field(default_factory=list)
    audio_uri: str = ""
    audio_note: str = ""
    footer: str = ""
    extra_notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "who": self.who,
            "day": self.day,
            "title": self.title or (self.who + "的错题卷 " + self.day),
            "minutes": self.minutes,
            "template_version": self.template_version,
            "items": [item.index for item in self.items],
            "dictation_words": list(self.dictation_words),
            "has_audio": bool(self.audio_uri),
            #: 绑定在**题目**上的音频段数(听力/听写题)。与上面的整页听写段是两回事。
            "problem_audios": sum(1 for item in self.items if item.audio_uri),
            "notes": list(self.extra_notes),
        }


def templates_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def render_html(model: SheetModel) -> str:
    """渲染单文件 HTML。**纯函数**(除了读模板文件),可以放心做金样本比对。"""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(templates_dir())),
        autoescape=select_autoescape(("html", "j2")),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(TEMPLATE_NAME)
    return template.render(
        model=model,
        title=model.title or (model.who + "的错题卷 " + model.day),
        minutes=model.minutes,
        version=model.template_version,
    )


def estimate_minutes(count: int) -> int:
    """卷面时长估计。与推送正文同一口径(每题约 3 分钟,最少 5 分钟)。"""
    return max(5, int(count) * 3)


__all__ = [
    "BLOCKING_STATUS",
    "SOURCE_LABELS",
    "STATUS_LABELS",
    "SheetModel",
    "SheetProblem",
    "TEMPLATE_VERSION",
    "data_uri",
    "estimate_minutes",
    "label_for",
    "render_html",
    "steps_from_raw",
    "templates_dir",
]
