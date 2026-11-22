"""识图:把一张错题照片变成结构化字段(视觉模型,*写入*路径)。

⭐ 为什么读取路径"不含 LLM"这条铁律在这里**不适用**:
   拍照录入本来就是"让模型帮忙把纸上的东西变成字段" —— 它是一次**写入前的整理**,
   不是读取。而且它有明确的降级:识别失败 / 没配密钥 / 断网时,候选区仍然立得住,
   界面照样能手工把题干填完。存量数据的读取路径依旧一行模型代码都没有。

⚠️ 两条与 DeepSeek 视觉能力有关的实测约束(api-docs「图像理解」):
   1. 图片**只能出现在 user 消息里**,system / assistant 带图直接 400;
   2. 单图 base64 上限 32 MiB、请求体 48 MiB,格式按**内容**判断而不是后缀 ——
      所以这里用 media.sniff_media_type() 认过的类型拼 data URL,不信文件名。

与既有决策的关系:原计划 M4.3b 用 PaddleOCR 本地识别(D42/D61 决定延后)。
现在改成**视觉模型**,理由见决策记录 D76:同一个 deepseek-flash 已经在用了,
不必再引一个约 100MB 的重依赖;而"照片不出本机"的口径由用户在设置里显式选择。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .. import config

# 与 schemas/study.py 的 SUBJECTS 同一份口径(不在这里另抄一套学科名)
from ..domain.schemas.study import SUBJECTS

# 一张照片里最多认几道题。再多说明这不是"错题照"而是整张卷子,该拆开拍。
MAX_PROBLEMS_PER_IMAGE = 4
MAX_STEM_CHARS = 1500
MAX_TEXT_CHARS = 600
#: 要念的原文(听力材料/听写词表)最长抄多少字 —— 再长就不是一道题的音频了
MAX_AUDIO_CHARS = 800
MAX_KP_COUNT = 4
MAX_KP_CHARS = 40
MAX_RAW_CHARS = 8000       # 原始回复留档的上限(排错用,不是事实源)

PROMPT_TEMPLATE = """你是错题本的录入助手。用户给的是学生做错的题目照片(试卷、作业本或练习册)。

⚠️ 最重要的一条:**照片上写着的答案不是标准答案** —— 那多半是孩子做错的作答,
   或者是老师的批改痕迹。你这一趟**只负责如实抄录**,不判断对错,更不许把
   孩子写的东西当成"正确答案"(解题是下一趟的事,那一趟看不到孩子写的东西)。

要求:
1. **先判断这是不是题目**。是题目就照下面填;不是(风景照、聊天截图、无关书页、
   二维码……)就只回 is_problem=false,并在 reason 里用一句话说明你看到了什么。
2. 一张照片里可能有 1 到 4 道**互不相同**的题目。每道题输出一个对象;只有一道就只输出一个。
3. subject 只能从这些里选(必须原样返回其中一个):{subjects}
4. knowledge_points:每道题给 1 到 3 个**最小粒度**的知识点,用教材/考纲里的说法
   (例如"异分母分数加减",而不是"分数")。下面这些已有知识点能对上就**原样复用**:
   {known_points}
5. stem_text 抄题干原文(含必要的选项与小问)。**看不清或照片里被截掉的部分**用 □ 代替,
   绝对不要猜、不要补 —— 缺哪儿比写错哪儿重要得多。
6. student_answer:**学生写在纸上的作答**,原样抄(包括写错的、被红笔划掉的)。
   照片上没有学生笔迹就留空字符串。
7. teacher_answer:老师用红笔写的批改(对号/错号/**订正答案**,例如"订:54")。
   没有就留空字符串。⚠️ 拿不准是老师写的还是孩子写的,一律算 student_answer。
8. figure:这道题里的图形。
   * present:有没有图形。**只算**与解题有关的图:几何图形、统计图(柱/折线/扇形)、
     表格、数轴、坐标系、立体图与组合体示意、正方体展开图、示意图/流程图。
     ⚠️ **卡通插图、装饰画、水印、logo、页眉页脚的花边、出版社图标一律不算** ——
     实测踩过:一道纯文字的锯木头题,模型把旁边的卡通仓鼠当成"图形"裁了出来。
   * kind:一句话说是什么图(例如"小正方体组合体的正面视图");
   * description:图形里有什么,30 字以内(例如"下面一排 3 个小正方形,中间上方叠加 1 个");
   * bbox:**归一化到 0-1000 的整数** [左, 上, 右, 下],只框**图形本身**,
     不要把题干文字框进来;没有图形时 present=false、bbox=[0,0,0,0]。
9. rotation_degrees:照片里的文字**顺时针**转多少度才是正的(只能填 0/90/180/270)。
10. confidence 是 0 到 1 的小数,表示你对这道题识别结果的把握。
11. 只输出 JSON,不要任何解释、不要代码块标记。格式:
{{"is_problem":true,"reason":"","problems":[{{"subject":"数学","grade":"","stem_text":"","student_answer":"","teacher_answer":"","figure":{{"present":false,"kind":"","description":"","bbox":[0,0,0,0]}},"rotation_degrees":0,"explanation":"","knowledge_points":[""],"confidence":0.9,"source":""}}]}}"""

SOLVE_HINT = """
12. (可选)照片上有**解析文字**时,可以抄进 explanation;没有就留空。
    ⚠️ 仍然**不要**自己解题 —— 解题是下一趟的事(见 la/domain/study/answers.py)。"""


@dataclass
class RecognizedProblem:
    """模型认出来的一道题。字段与 ProblemCreate 一一对应,多一个 confidence。"""

    subject: str = "其他"
    stem_text: str = ""
    #: ⚠️ M14 起:这里是**孩子写在纸上的作答**。它永远不是标准答案,单独一列存。
    #: (旧提示词把照片上的答案抄进 answer_text —— 那多半是错答,已经被当成标准答案印过卷子。)
    student_answer: str = ""
    #: 老师红笔写的批改/订正答案(可空)。它是**比模型更可信**的一票。
    teacher_answer: str = ""
    explanation: str = ""
    grade: str = ""
    source: str = ""
    knowledge_points: list[str] = field(default_factory=list)
    confidence: float = 0.0
    #: 图形:{"present":bool,"kind":str,"description":str,"bbox":[l,t,r,b]}
    figure: dict[str, Any] = field(default_factory=dict)
    #: 照片里的文字要顺时针转多少度才是正的(0/90/180/270)
    rotation_degrees: int = 0

    @property
    def has_figure(self) -> bool:
        return bool(self.figure.get("present"))

    @property
    def figure_bbox(self) -> Any:
        return self.figure.get("bbox")

    def as_payload(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "grade": self.grade or None,
            "source": self.source or None,
            "stem_text": self.stem_text or None,
            "student_answer": self.student_answer or None,
            "teacher_answer": self.teacher_answer or None,
            "explanation": self.explanation or None,
            "figure": dict(self.figure) or None,
            "rotation_degrees": self.rotation_degrees,
            "knowledge_points": list(self.knowledge_points),
            "confidence": self.confidence,
        }


@dataclass
class RecognitionResult:
    """一次识别尝试的全部结果 —— **失败也是一种结果**,要能原样交给界面。"""

    problems: list[RecognizedProblem] = field(default_factory=list)
    raw: str = ""
    model: str = ""
    error: str = ""
    #: 模型对"这是不是题目照片"的判断。⚠️ 它与"认出几道题"是**两件事**:
    #: 一只猫 = is_problem 假且 problems 空;一张拍糊了的题目 = 都是空 + error 有话说。
    is_problem: bool = False
    #: 不是题目时,模型用一句话说明它看到了什么(直接给用户看,不加工)
    reason: str = ""
    #: 模型**真的回了一段能解析的 JSON**。用来区分两件完全不同的事:
    #: "它看过并判断这不是题目"(parsed=True) vs "压根没调通/没解析出来"(parsed=False)。
    #: 少了这个位,一只猫的照片会被报成"识别失败",用户就会以为是自己拍坏了。
    parsed: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.problems)


def build_prompt(
    *,
    subjects: Sequence[str] = SUBJECTS,
    known_points: Iterable[str] | None = None,
    solve: bool = False,
) -> str:
    """拼提示词。已有知识点**必须**喂给模型 —— 否则同一件事会被叫成两个名字。"""
    points = [str(item).strip() for item in (known_points or []) if str(item).strip()]
    known = "、".join(points[:60]) if points else "(暂时没有,请给出规范的教材说法)"
    text = PROMPT_TEMPLATE.format(subjects="、".join(subjects), known_points=known)
    return text + (SOLVE_HINT if solve else "")


def build_messages(
    image: bytes,
    media_type: str,
    *,
    prompt: str,
) -> list[dict[str, Any]]:
    """一条 user 消息:文本块 + 图片块(⚠️ 图片不能进 system/assistant)。"""
    encoded = base64.b64encode(image).decode("ascii")
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:" + media_type + ";base64," + encoded},
                },
            ],
        }
    ]


# -- 解析 -------------------------------------------------------------------
_FENCE = chr(96) * 3


def strip_code_fence(text: str) -> str:
    """去掉 Markdown 代码围栏,只保留里面的内容。

    围栏有三种写法(``` / ```json / 收尾的 ```),按**行**删最省事也最不容易错。
    """
    if _FENCE not in text:
        return text
    kept = [line for line in text.splitlines() if not line.strip().startswith(_FENCE)]
    return "\n".join(kept).strip()


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型回复里挖出那个 JSON 对象。

    ⚠️ 三层都得挡:① 常见的代码围栏;② 前后带废话(思考模式的尾巴);
    ③ 根本没给 JSON。用 raw_decode 从第一个 `{` 起试着解码 ——
    比"截取第一个 { 到最后一个 }"更稳:后者遇到字符串里的花括号会直接崩。
    """
    if not text:
        return None
    cleaned = strip_code_fence(text.strip())
    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            loaded, _end = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            return loaded
    return None


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    return " ".join(str(value).split())[:limit]


def normalize_subject(value: Any, *, subjects: Sequence[str] = SUBJECTS) -> str:
    """学科必须是闭集里的一员。

    ⚠️ 模型很爱回"初中数学""数学(人教版)"这类带修饰的说法 —— 直接判为"其他"
    会把统计口径搞乱,所以先做一次**包含**匹配;真的对不上才落到"其他"。
    """
    text = _text(value, 20)
    if text in subjects:
        return text
    for name in subjects:
        if name in text:
            return name
    return "其他"


def normalize_knowledge_points(value: Any, *, subject: str) -> list[str]:
    """知识点:去空、去重、限长、限量,且**不带学科前缀**(别名表按纯名字匹配)。"""
    if isinstance(value, str):
        raw_items: list[Any] = [item for item in value.replace("、", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raw_items = []
    points: list[str] = []
    for item in raw_items:
        name = _text(item, MAX_KP_CHARS).strip(" 　·,、;；")
        if not name:
            continue
        if name.startswith(subject) and len(name) > len(subject):
            name = name[len(subject):].lstrip("的 ")
        if not name or name in points:
            continue
        points.append(name)
    return points[:MAX_KP_COUNT]


def normalize_figure(value: Any) -> dict[str, Any]:
    """图形字段收敛。⚠️ 没有图形的题**不留空壳**(present=false 时只回一个空字典)。"""
    if not isinstance(value, dict):
        return {}
    present = value.get("present")
    if isinstance(present, str):
        present = present.strip().lower() in ("true", "1", "yes", "是", "有")
    if not present:
        return {}
    bbox = value.get("bbox")
    if isinstance(bbox, dict):
        bbox = [bbox.get("left", 0), bbox.get("top", 0), bbox.get("right", 0), bbox.get("bottom", 0)]
    if not isinstance(bbox, (list, tuple)):
        bbox = []
    return {
        "present": True,
        "kind": _text(value.get("kind") or value.get("type"), 40),
        "description": _text(value.get("description") or value.get("desc"), 120),
        "bbox": list(bbox)[:4],
    }


def normalize_audio(value: Any) -> dict[str, Any]:
    """「这道题要不要出声」字段收敛。⚠️ 不需要就回空字典(**不留空壳**)。

    这里**只做形状校验**;kind 的白名单在错题域(la/domain/study/audio.py)——
    与图形白名单同一条规矩:通用视觉层不 import 领域常量,免得两处各抄一份、越走越远。
    """
    if not isinstance(value, dict):
        return {}
    needs = value.get("needs")
    if isinstance(needs, str):
        needs = needs.strip().lower() in ("true", "1", "yes", "是", "要", "需要")
    kind = _text(value.get("kind") or value.get("type"), 20).strip().lower()
    body = _text(
        value.get("text") or value.get("content") or value.get("material"),
        MAX_AUDIO_CHARS,
    )
    note = _text(value.get("note") or value.get("why"), 120)
    if not (needs or body):
        return {}
    return {"needs": True, "kind": kind or "none", "text": body, "note": note}


def normalize_rotation(value: Any) -> int:
    """旋转角只能取 0/90/180/270。⚠️ 模型爱回 -90 / 360 / 45,一律收敛到最近的合法值。"""
    try:
        angle = float(value)
    except (TypeError, ValueError):
        return 0
    angle = angle % 360
    for allowed in (0, 90, 180, 270):
        if abs(angle - allowed) <= 20 or abs(angle - allowed - 360) <= 20:
            return allowed
    return 0


def normalize_problem(
    raw: dict[str, Any], *, subjects: Sequence[str] = SUBJECTS
) -> RecognizedProblem | None:
    """把模型给的一个对象收敛成**可以落库**的形状。整条认不出来就返回 None。"""
    if not isinstance(raw, dict):
        return None
    subject = normalize_subject(raw.get("subject"), subjects=subjects)
    stem = _text(raw.get("stem_text") or raw.get("stem"), MAX_STEM_CHARS)
    # ⚠️ 兼容老写法:老提示词把"照片上的答案"写进 answer_text —— 它的**语义**就是
    #    孩子写在纸上的东西,所以这里落到 student_answer,而不是 answer_text。
    student = _text(
        raw.get("student_answer") or raw.get("student") or raw.get("answer_text")
        or raw.get("answer"),
        MAX_TEXT_CHARS,
    )
    teacher = _text(
        raw.get("teacher_answer") or raw.get("teacher") or raw.get("correction"),
        MAX_TEXT_CHARS,
    )
    explanation = _text(raw.get("explanation"), MAX_TEXT_CHARS)
    if not (stem or student or teacher or explanation):
        return None
    confidence = raw.get("confidence")
    try:
        score = float(confidence)
    except (TypeError, ValueError):
        score = 0.0
    score = min(1.0, max(0.0, score))
    return RecognizedProblem(
        subject=subject,
        stem_text=stem,
        student_answer=student,
        teacher_answer=teacher,
        explanation=explanation,
        grade=_text(raw.get("grade"), 20),
        source=_text(raw.get("source"), 40),
        knowledge_points=normalize_knowledge_points(raw.get("knowledge_points"), subject=subject),
        confidence=score,
        figure=normalize_figure(raw.get("figure")),
        rotation_degrees=normalize_rotation(raw.get("rotation_degrees") or raw.get("rotation")),
    )


def parse_full_response(
    text: str, *, subjects: Sequence[str] = SUBJECTS
) -> RecognitionResult:
    """模型回复 -> (题目列表 + 是不是题目 + 它说的一句话)。

    ⚠️ 兼容老写法:提示词早期没有 is_problem 字段 —— 那时"认出了题目"就等价于
    "是题目";认不出也不能断言它不是题目,所以 is_problem 保持假。
    """
    loaded = extract_json(text)
    if loaded is None:
        return RecognitionResult(raw=text[:MAX_RAW_CHARS])
    items = loaded.get("problems")
    if items is None and any(key in loaded for key in ("stem_text", "stem", "subject")):
        items = [loaded]          # 模型偷懒只给了一个对象,而不是数组
    problems: list[RecognizedProblem] = []
    if isinstance(items, list):
        for item in items[:MAX_PROBLEMS_PER_IMAGE]:
            problem = normalize_problem(item, subjects=subjects)
            if problem is not None:
                problems.append(problem)
    flag = loaded.get("is_problem")
    if isinstance(flag, bool):
        is_problem = flag
    elif isinstance(flag, str):
        is_problem = flag.strip().lower() in ("true", "1", "yes", "是")
    else:
        is_problem = bool(problems)
    return RecognitionResult(
        problems=problems,
        raw=text[:MAX_RAW_CHARS],
        is_problem=is_problem,
        reason=_text(loaded.get("reason"), 200),
        parsed=True,
    )


def parse_response(text: str, *, subjects: Sequence[str] = SUBJECTS) -> list[RecognizedProblem]:
    """模型回复 -> 题目列表。认不出就返回空列表(调用方决定怎么降级)。"""
    return parse_full_response(text, subjects=subjects).problems


# -- 调用 -------------------------------------------------------------------
async def recognize_image(
    client: Any,
    image: bytes,
    media_type: str,
    *,
    subjects: Sequence[str] = SUBJECTS,
    known_points: Iterable[str] | None = None,
    solve: bool = False,
    model: str | None = None,
) -> RecognitionResult:
    """调一次视觉模型。**任何失败都变成 result.error,不往外抛** ——
    录入流程不能因为模型挂了就整条断掉。"""
    if not image:
        return RecognitionResult(error="空图片")
    prompt = build_prompt(subjects=subjects, known_points=known_points, solve=solve)
    messages = build_messages(image, media_type, prompt=prompt)
    try:
        result = await client.chat(messages, model=model)
    except Exception as exc:  # 网络 / 鉴权 / 超时 / 额度,一律降级
        return RecognitionResult(error=type(exc).__name__ + ": " + str(exc)[:200])
    text = str(getattr(result, "text", "") or "")
    outcome = parse_full_response(text, subjects=subjects)
    outcome.model = str(getattr(result, "model", "") or "")
    if not outcome.problems:
        if outcome.is_problem:
            # "说是题目却没给出字段"与"看不出是什么"要分开措辞 ——
            # 前者该重拍,后者该改口,混成一句话用户就不知道该干什么
            outcome.error = "看着像题目,但没抄出可用的字段(可能拍糊了或只拍了一半)"
        else:
            outcome.error = outcome.reason or "这张看着不是题目"
    return outcome


def recognize_image_sync(
    client: Any,
    image: bytes,
    media_type: str,
    *,
    subjects: Sequence[str] = SUBJECTS,
    known_points: Iterable[str] | None = None,
    solve: bool = False,
    model: str | None = None,
) -> RecognitionResult:
    """同步入口:Web 端的表单 POST 是同步端点(跑在线程池里),那里没有事件循环。

    ⚠️ 不能用 asyncio.get_event_loop():在 3.12+ 的线程里它要么报错、要么给出
    一个没在跑的循环。asyncio.run 每次新建并关闭一个循环,正好对应"一次调用"。
    """
    return asyncio.run(
        recognize_image(
            client, image, media_type,
            subjects=subjects, known_points=known_points, solve=solve, model=model,
        )
    )


# -- 通用看图(M12:基础层的 image_view 走这里)--------------------------------
# ⭐ 与上面的错题录入**共用 build_messages** —— "图片只进 user 消息"这条铁律
#    只写一遍;两边的区别只在提示词、以及输出是不是 JSON。
DESCRIBE_PROMPT = """你是家庭助手的"眼睛"。请**只讲图上真实存在的信息**,看不清就说看不清,不要脑补。

按三段回答,不要用 markdown 标题:
1. 一句话说明这是什么(场景 / 物体 / 文档类型);
2. 把图上**所有可见文字照抄**下来(看不清的字用 □ 代替,不要猜);没有文字就写"无文字";
3. 如果有具体问题,回答它。"""


def build_describe_prompt(question: str = "") -> str:
    text = DESCRIBE_PROMPT
    if str(question or "").strip():
        text += "\n\n具体问题:" + str(question).strip()
    return text


async def describe_image(
    client: Any,
    image: bytes,
    media_type: str,
    *,
    question: str = "",
    model: str | None = None,
    max_chars: int = config.IMAGE_DESCRIBE_MAX_CHARS,
) -> str:
    """通用看图:返回一段中文描述。**失败不抛** —— 返回一句能读的兜底说明。"""
    if not image:
        return "这张图是空的,看不了。"
    messages = build_messages(image, media_type, prompt=build_describe_prompt(question))
    try:
        result = await client.chat(messages, model=model)
    except Exception as exc:  # noqa: BLE001 - 看不了图不该让整轮对话崩掉
        return "看图失败(" + type(exc).__name__ + "):" + str(exc)[:160]
    text = str(getattr(result, "text", "") or "").strip()
    if not text:
        return "模型没有给出任何描述(可能图太大,或这个格式它不支持)。"
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n…(描述已截断:原文共 " + str(len(text)) + " 字)"
    return text


def describe_image_sync(
    client: Any,
    image: bytes,
    media_type: str,
    *,
    question: str = "",
    model: str | None = None,
    max_chars: int = config.IMAGE_DESCRIBE_MAX_CHARS,
) -> str:
    """同步入口(Web 端同步端点在线程池里跑,那里没有事件循环)。"""
    return asyncio.run(
        describe_image(client, image, media_type, question=question, model=model, max_chars=max_chars)
    )


# -- M14:答案与图形的"补拍"(给已经录进库的老错题用)-----------------------
# ⭐ 为什么单独一个提示词:老错题录进来时,照片上的答案被当成"正确答案"抄走了
#    (实测:错题 #5 抄进来的是"孩子算的 65 分钟 + 老师订正 54 分钟"两段混在一起)。
#    要修它就得**回到照片前**重新问一次 —— 但只问三件事(孩子写了什么、老师订正了
#    什么、图形在哪),不重抄题干、不重新归类:省 token,也少一层出错的机会。
ANSWER_PROBE_PROMPT = """这是一张**做错的题目**照片。只做抄录,**不要解题**。

⚠️ 最重要的一条:照片上写着的答案**不是标准答案** —— 那是孩子的作答或老师的批改痕迹。
   你只负责如实抄下来,不要判断对错。

抄三样东西:
1. student_answer:学生写在纸上的作答,原样抄(包括写错的、被红笔划掉的)。
2. teacher_answer:老师用红笔写的批改/订正答案(例如"订:54")。没有就留空。
   ⚠️ 拿不准是谁写的,一律算 student_answer。
3. figure:题目里的图形。**只算**与解题有关的图(几何图形、统计图、表格、数轴、
   坐标系、立体图/组合体示意、展开图、示意图);**卡通插图、装饰画、水印、logo 不算** ——
   实测踩过:一道纯文字的锯木头题,旁边的卡通仓鼠被当成了"图形"。
   present 有没有 / kind 是什么图 / description 30 字内 /
   bbox 归一化到 0-1000 的整数 [左,上,右,下],只框图形本身。
   rotation_degrees:把照片**顺时针**转多少度,里面的文字才能正着读?
   只能填 0 / 90 / 180 / 270。⚠️ 拿不准就填 0。
4. audio:这道题**要不要出声**(听力题、听写题、跟读/朗读题)。
   ⚠️ 只有**题目本身要求听或说**才算要(true);普通数学题、默读的阅读题、笔答题一律
   false —— **不许给每道题都配一段朗读**,那只会让孩子听一堆废话。
   * kind:"listening"(要放一段话/句子给他听)或"dictation"(听写词语/单词);
   * text:**要念的原文,一字不改地照抄**(听力材料/句子原文;听写就用、分隔词表)。
     ⚠️ 照片上没有的、你自己想出来的内容**一律不写** —— 宁可不发声,也不要念错的内容
     (听写念错一个词,孩子会照着错的写)。
   * note:一句话说明(例如"录音原文印在题目里")。没有就留空。

照片里若有好几道题,**只看与下面这段题干对应的那一道**。

只输出 JSON,不要解释、不要代码块标记:
{"is_problem":true,"student_answer":"","teacher_answer":"","figure":{"present":false,"kind":"","description":"","bbox":[0,0,0,0]},"audio":{"needs":false,"kind":"none","text":"","note":""},"rotation_degrees":0}"""



def parse_answer_probe(text: str) -> dict[str, Any]:
    """解析"补拍"回复。拿不到 JSON 就返回 error;字段缺失一律给**安全默认值**。"""
    loaded = extract_json(text or "")
    if not isinstance(loaded, dict):
        return {"error": "没解析出 JSON"}
    answer: dict[str, Any] | None = None
    if isinstance(loaded.get("problems"), list) and loaded["problems"]:
        first = loaded["problems"][0]
        answer = first if isinstance(first, dict) else None
    if answer is None and isinstance(loaded.get("problem"), dict):
        answer = loaded["problem"]
    if answer is None and any(
        key in loaded for key in ("student_answer", "teacher_answer", "figure")
    ):
        answer = loaded
    if answer is None:
        return {"error": "回复里没有题目字段"}
    return {
        "student_answer": _text(
            answer.get("student_answer") or answer.get("student"), MAX_TEXT_CHARS
        ),
        "teacher_answer": _text(
            answer.get("teacher_answer") or answer.get("teacher") or answer.get("correction"),
            MAX_TEXT_CHARS,
        ),
        "figure": normalize_figure(answer.get("figure")),
        # 这道题要不要出声(听力/听写)+ 要念的原文。⭐ 录入时就靠它把音频**一次做好绑定**
        "audio": normalize_audio(answer.get("audio")),
        "rotation_degrees": normalize_rotation(
            answer.get("rotation_degrees") or answer.get("rotation")
        ),
        "is_problem": bool(answer.get("is_problem", True)),
    }


async def probe_answers(
    client: Any,
    image: bytes,
    media_type: str,
    *,
    stem: str = "",
    catalog: str = "",
    model: str | None = None,
) -> dict[str, Any]:
    """回到照片前,只问"孩子写了什么 / 老师订正了什么 / 图形在哪"。失败返回 error,不抛。

    ⚠️ `catalog`(图形白名单说明)**2026-11-21 起不再使用**:矢量重绘关掉了,提示词里那段
    也删了。参数留着只是为了不打断既有调用方;新代码别再传它。
    """
    if not image:
        return {"error": "空图片"}
    prompt = ANSWER_PROBE_PROMPT
    if str(stem or "").strip():
        prompt += "\n\n题干:" + str(stem).strip()[:MAX_STEM_CHARS]
    messages = build_messages(image, media_type, prompt=prompt)
    try:
        result = await client.chat(messages, model=model)
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__ + ": " + str(exc)[:160]}
    return parse_answer_probe(str(getattr(result, "text", "") or ""))


def probe_answers_sync(
    client: Any, image: bytes, media_type: str, **kwargs: Any
) -> dict[str, Any]:
    """同步入口(Web 端表单在线程池里跑,那里没有事件循环)。"""
    return asyncio.run(probe_answers(client, image, media_type, **kwargs))


# ⭐ 裁剪必须能验货,而且验货必须能**变成一个动作**(2026-11-21 重做)。
#    原来这里只问「框完整吗」:答「不完整」也只写一句提示,框一点都不动 —— 白花一次调用。
#    更要命的是**方向从来没人问过**:实测两张真照片、两趟调用,模型自由报「顺时针转多少度」
#    四次只对一次(25%,等于四选一瞎蒙);而「同一张图四个方向摆出来、让它挑哪张是正的」,
#    同一批照片两次全对。所以这里改成:
#
#      ① 把裁出来的那一块按 0/90/180/270 各发一张(四张图,一次请求);
#      ② 一次问三件事:正不正(upright)/ 完整吗(complete)/ 混进题干文字了吗(has_text);
#      ③ 三个答案都能直接落成一个动作:换角度重裁 / 加大边距重裁 / 不动。
#
#    ⚠️ 为什么不问角度而让它挑一张:辨认是模型擅长的,心算旋转+换算是它不擅长的。
VERIFY_PROMPT = """这四张是**同一张图**按 0°、90°、180°、270°(顺时针)分别转过的样子:
第 1 张 = 原样(0°)、第 2 张 = 顺时针 90°、第 3 张 = 顺时针 180°、第 4 张 = 顺时针 270°。

这是从一道错题照片上**裁下来的一块**。请只回答三件事:

1. upright:哪一张里的**文字或图形是正着读的**?填「要顺时针转多少度才是正的」(0/90/180/270)。
   四张看起来都一样(纯几何图形、没有文字)就填 0。
2. complete:那一张里的图形**完整吗**?被切掉任何一边(边贴着图边、明显少一块)就填 false。
3. has_text:那一块里有没有**混进题干文字**(题目正文/答案/批改,不是图形自己的标注)?有就填 true。

只输出 JSON,不要解释、不要代码块标记:
{{"upright":0,"complete":true,"has_text":false,"why":"一句话"}}"""


def _angle(value: Any) -> int | None:
    """模型给的角度 -> 0/90/180/270;认不出就返回 None(**不要**当成 0)。

    ⚠️ 实测模型爱写 "270°"、"顺时针 270 度"、"第 4 张 = 270°" 这些壳。
    直接 float() 会失败,于是"它答了"被当成"它没答";而只抠第一个数字更糟 ——
    "第 4 张 = 270°" 会变成 4°。所以**从后往前**找,只认 0/90/180/270 这几个值。
    """
    if value is None or isinstance(value, bool):
        return None
    number: float | None
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = None
    if number is not None:
        # ⚠️ 不是「随便给个角度就收敛」:给 4(第 4 张)必须判"没答",
        #    否则它会变成 0° 并被当成"模型说不用转"。**只认整 90**:
        #    模型真会说 270,不会说 88。
        angle = number % 360
        for allowed in (0, 90, 180, 270):
            if angle == allowed:
                return allowed
        return None
    for item in reversed(re.findall(r"\d+", str(value or ""))):
        angle = int(item) % 360
        for allowed in (0, 90, 180, 270):
            if angle == allowed:
                return allowed
    return None


def _flag(value: Any) -> bool | None:
    """true/是/完整 一律认;没给就返回 None(与「明确说 false」分开)。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "是", "完整", "有"):
        return True
    if text in ("false", "0", "no", "否", "不完整", "没有"):
        return False
    return None


def parse_verify(text: str) -> dict[str, Any]:
    """解析「验货」回复。⚠️ 没给的字段返回 None —— 「没答」和「答了 0/false」是两件事。"""
    loaded = extract_json(text or "")
    if not isinstance(loaded, dict):
        return {"error": "没解析出 JSON"}
    return {
        "upright": _angle(loaded.get("upright")),
        "complete": _flag(loaded.get("complete")),
        "has_text": _flag(loaded.get("has_text")),
        "why": _text(loaded.get("why"), 60),
        "parsed": True,
    }


async def verify_figure(
    client: Any,
    tiles: Sequence[bytes],
    *,
    media_type: str = "image/jpeg",
    model: str | None = None,
) -> dict[str, Any]:
    """把「同一块的四个方向」一起发过去,问:正不正 / 完整吗 / 混进文字了吗。

    `tiles` 由调用方用 figure.rotation_tiles() 生成(0/90/180/270 四张)。
    **失败返回 error,不抛** —— 复核只决定「更准」,不决定「有没有图」。
    """
    images = [item for item in tiles if item]
    if not images:
        return {"error": "没有可复核的图"}
    blocks: list[dict[str, Any]] = [{"type": "text", "text": VERIFY_PROMPT}]
    for item in images:
        blocks.append({
            "type": "image_url",
            "image_url": {
                "url": "data:" + media_type + ";base64,"
                       + base64.b64encode(item).decode("ascii"),
            },
        })
    try:
        result = await client.chat([{"role": "user", "content": blocks}], model=model)
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__ + ": " + str(exc)[:160]}
    return parse_verify(str(getattr(result, "text", "") or ""))


def verify_figure_sync(client: Any, tiles: Sequence[bytes], **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(verify_figure(client, tiles, **kwargs))

# ⭐ 单独追问图形参数。实测:模型在"顺手带上"的字段里经常只写 kind(照着模板写了个 null),
#    但**单独问一句**时给得准得多 —— 一次只问一件事,是提示词里最便宜的一次胜利。
FIGURE_PARAMS_PROMPT = """这张照片里的这道题有一个图形,它是:{kind}。

请只给出它的**参数**(用 JSON)。需要:{guide}

只输出 JSON,不要解释、不要代码块标记,不要给图形源码:
{{"params":{{}}}}

⚠️ 参数必须**照实填**(按照片里真实看到的数字,不要猜、不要改题)。看不清就回 {{"params":null}}。"""


async def ask_figure_params(
    client: Any,
    image: bytes,
    media_type: str,
    *,
    kind: str,
    guide: str,
    stem: str = "",
    model: str | None = None,
) -> dict[str, Any]:
    """只问一件事:这个图形的参数是多少。失败/看不清返回空 dict。"""
    if not image or not kind:
        return {}
    prompt = FIGURE_PARAMS_PROMPT.format(kind=kind, guide=guide or "按图形本身的形状给")
    if str(stem or "").strip():
        prompt += "\n\n题干:" + str(stem).strip()[:MAX_STEM_CHARS]
    messages = build_messages(image, media_type, prompt=prompt)
    try:
        result = await client.chat(messages, model=model)
    except Exception:  # noqa: BLE001 - 问不到参数就退回裁剪,不是错误
        return {}
    loaded = extract_json(str(getattr(result, "text", "") or ""))
    if not isinstance(loaded, dict):
        return {}
    params = loaded.get("params")
    if params is None:
        params = loaded.get("parameters")
    return params if isinstance(params, dict) else {}


def ask_figure_params_sync(
    client: Any, image: bytes, media_type: str, **kwargs: Any
) -> dict[str, Any]:
    return asyncio.run(ask_figure_params(client, image, media_type, **kwargs))


# ⭐ 同一招再用一次:模型在"顺手带上"的字段里常只写 kind="listening" 却把 text 留空
#    (或者干脆没提 audio)。单独问一句"要念的是哪段文字",它给得准得多。
AUDIO_TEXT_PROMPT = """这道题**需要出声**(听力/听写/跟读),但我还没拿到"要念给孩子听的那段文字"。

请只回答一件事:要念的是哪段文字?

* 只抄**照片上印着的**字(听力材料的原文、句子、听写词表;词表用、分隔)。
* 照片上找不到(例如"听老师读"、录音只在磁带里)→ 回 {"text":null},
  ⚠️ **绝对不要自己编一段**:听写念错一个词,孩子会照着错的写。

只输出 JSON,不要解释、不要代码块标记:
{"text":"","note":"一句话"}

⚠️ 原文必须照抄,不要改写、不要翻译、不要补全。"""


async def ask_audio_text(
    client: Any,
    image: bytes,
    media_type: str,
    *,
    kind: str = "",
    stem: str = "",
    model: str | None = None,
) -> dict[str, Any]:
    """只问一件事:要念的原文是哪段。失败/照片上没有 → 空 text(**不许编**)。"""
    if not image:
        return {}
    prompt = AUDIO_TEXT_PROMPT
    if str(kind or "").strip():
        prompt += "\n\n(这题的类型:" + str(kind)[:20] + ")"
    if str(stem or "").strip():
        prompt += "\n\n题干:" + str(stem).strip()[:MAX_STEM_CHARS]
    messages = build_messages(image, media_type, prompt=prompt)
    try:
        result = await client.chat(messages, model=model)
    except Exception:  # noqa: BLE001 - 问不到原文就如实说"这道题会是哑的",不是崩溃
        return {}
    loaded = extract_json(str(getattr(result, "text", "") or ""))
    if not isinstance(loaded, dict):
        return {}
    return {
        "text": _text(loaded.get("text") or loaded.get("content"), MAX_AUDIO_CHARS),
        "note": _text(loaded.get("note"), 120),
    }


def ask_audio_text_sync(
    client: Any, image: bytes, media_type: str, **kwargs: Any
) -> dict[str, Any]:
    return asyncio.run(ask_audio_text(client, image, media_type, **kwargs))

