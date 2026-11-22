"""错题域的请求/响应模型。

⚠️ 两道必须守住的边界(能力域 §6.5):
  * 复习日期不由助手算 —— 那是 FSRS 的事(M4.3c);
  * 不给孩子看家长视角下的评价 —— 孩子端与家长端是两个投影。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

SUBJECTS = ("数学", "语文", "英语", "物理", "化学", "生物", "历史", "地理", "政治", "其他")


class ProblemCreate(BaseModel):
    """录入一道错题。图片先由调用方存成资产,这里只传资产 id 列表。"""

    subject: str = Field(..., max_length=20, description="学科,例如 数学")
    student_id: int | None = Field(
        None, description="这道错题是谁的(学生档案 id)。留空 = 用默认学生。"
    )
    student_name: str | None = Field(
        None, max_length=20, description="按名字指定学生;名字不存在时会**新建**该学生档案。"
    )
    stem_text: str | None = Field(
        None, max_length=2000, description="题干原文;拍照录入时可以先留空,等 OCR 或人工补"
    )
    answer_text: str | None = Field(None, max_length=2000, description="正确答案")
    explanation: str | None = Field(None, max_length=2000, description="解析或错因说明")
    grade: str | None = Field(None, max_length=20, description="年级,例如 五年级")
    source: str | None = Field(None, max_length=40, description="来源,例如 单元测/作业")
    knowledge_points: list[str] = Field(
        default_factory=list,
        description="涉及的知识点名称。⭐ 每个知识点会各生成一个**独立调度**的复习单元 —— 唯此才能归因。",
    )
    asset_ids: list[int] = Field(default_factory=list, description="已保存的题目图片资产 id")
    image_paths: list[str] = Field(
        default_factory=list, description="要一并保存的图片文件路径(本地路径,照片不出本机)"
    )


class ProblemAddInput(ProblemCreate):
    """工具层「录一道题」的输入:照片只认**素材编号**,不认资产编号。

    ⭐ 2026-11-01 真机 #16–#21:用户发了一张英语短语页,顺手说"把这些录成错题"。
    模型在 material_list 里看到的是「素材 #12」,录题时把它填进了 `asset_ids` ——
    **12 恰好是一张十月份的旧图形**,于是六道英语题全挂上了那张小正方体图。
    这种错能过所有校验:两个编号都存在,只是指向不同的东西。

    所以这里把两个名字空间分开:
      * `material_ids` = 聊天里收到的素材编号(模型看得见、也只该用这个);
      * `asset_ids` 从**模型可见的 schema** 里隐去(内部调用仍然可以传,比如识别流水线)。
    真传了 asset_ids 就报错,而不是"悄悄挂错一张图"。
    """

    material_ids: list[int] = Field(
        default_factory=list,
        description=(
            "照片来自聊天里收到的哪几条素材(素材清单里的 #N,填数字)。"
            "系统会换成题目照片;不传 = 这道题没有照片。"
        ),
    )
    asset_ids: SkipJsonSchema[list[int]] = Field(
        default_factory=list,
        description="内部字段(模型看不见):请用 material_ids 或 image_paths,不要手填资产 id。",
    )

    @model_validator(mode="after")
    def _reject_raw_asset_ids(self) -> "ProblemAddInput":
        if self.asset_ids:
            raise ValueError(
                "不要传 asset_ids —— 照片请用 material_ids(素材编号 #N)。"
                "素材编号和资产编号不是一回事,传错会挂上一张不相干的图。"
            )
        return self


class ProblemQuery(BaseModel):
    subject: str | None = Field(None, max_length=20, description="按学科过滤")
    knowledge_point: str | None = Field(None, max_length=40, description="按知识点过滤")
    student_id: int | None = Field(None, description="按学生过滤;留空 = 所有学生")
    status: Literal["pending", "confirmed", "archived"] | None = Field(
        None, description="按状态过滤;录入后未确认的是 pending"
    )
    include_archived: bool = Field(
        False, description="默认**不返回**已归档的错题(归档 = 撤销录入,不该继续出现在错题本里)"
    )
    limit: int = Field(20, ge=1, le=200, description="最多返回多少条")
    with_coverage: bool = Field(
        False,
        description="额外给一份覆盖域:错题一共多少道、各状态各多少(本清单只是筛选结果,不等于全部)。",
    )


class ReviewUnitRead(BaseModel):
    id: int
    problem_id: int
    ord: int = Field(..., description="维度序号:0=原题,1..n=按知识点的专项单元")
    kp_id: int | None = Field(None, description="这个单元考的是哪个知识点(0 号原题单元为空)")
    knowledge_point: str | None = None
    fsrs_state: str
    reps: int
    lapses: int
    due_date_local: str = Field(..., description="到期自然日(Asia/Shanghai),供「今日复习」查询")
    suspended: int = Field(0, description="1 = 已暂停调度(不算到期,也不推送)")


class ProblemRead(BaseModel):
    id: int
    subject: str
    grade: str | None = None
    source: str | None = None
    stem_text: str | None = None
    answer_text: str | None = None
    explanation: str | None = None
    status: str
    student_id: int | None = None
    student_name: str | None = None
    knowledge_points: list[str] = Field(default_factory=list)
    asset_ids: list[int] = Field(default_factory=list)
    review_units: list[ReviewUnitRead] = Field(default_factory=list)
    next_due_local: str | None = Field(
        None, description="下一次该复习的日子 = 所有未暂停单元里最早的那个到期日"
    )
    due_count: int = Field(0, description="今天(含更早)到期的单元数")
    reps: int = Field(0, description="所有单元的累计复习次数")
    lapses: int = Field(0, description="所有单元的累计答错次数 —— 「薄弱」的依据")
    audio_handle: str | None = Field(
        None,
        description=(
            "这道题**绑定好的音频**句柄(听力/听写题;录入时就已经生成)。"
            "⭐ 有它就直接写进页面/发出去,**不要再用 tts_audio 重做一遍** —— "
            "重做会得到另一段声音(不稳定),而且白花一次语音费与 token。"
        ),
    )
    audio_label: str = Field("", description="这段音频是什么(例如「听力音频」「听写音频」「认读音频」)")
    audio_purpose: str = Field(
        "",
        description="read_aloud = 认读:先让孩子自己读,再播放对照(页面据此给提示)",
    )
    photo_handles: list[str] = Field(
        default_factory=list,
        description=(
            "题目照片的可粘贴句柄(/assets/<id>)。⭐ 写网页时**直接用它们**:"
            "服务端落盘前会把图片内联成 data:,页面因此自包含、发到微信也看得见。"
        ),
    )
    figure_handle: str | None = Field(
        None,
        description=(
            "从照片上裁下来的图形的句柄(/assets/<id>);没有图形就是 null。"
            "与 audio_handle 一样:**直接用,别自己找图**。"
        ),
    )
    created_at: str = ""


class ProblemUpdate(BaseModel):
    """改一道错题。⭐ 只改内容,不动 app_review_units —— 改错别字不该重来一遍。"""

    subject: str | None = Field(None, max_length=20, description="改学科")
    grade: str | None = Field(None, max_length=20)
    source: str | None = Field(None, max_length=40)
    stem_text: str | None = Field(None, max_length=2000)
    answer_text: str | None = Field(None, max_length=2000)
    explanation: str | None = Field(None, max_length=2000)
    knowledge_points: list[str] | None = Field(
        None,
        description="整份替换知识点(不是追加)。新增的知识点会各补一个复习单元;"
        "移除的知识点只是不再挂在这道题上,已攒下的复习进度**不会被删**。",
    )
    student_id: int | None = Field(None, description="转给另一个学生")


class StudyPushSettings(BaseModel):
    """每日错题复习推送的设置。可由界面设定,也可由模型设定(工具 quiz_write 的 action=push)。"""

    enabled: bool | None = Field(None, description="要不要每天推送错题复习清单")
    push_time: str | None = Field(
        None, max_length=5, description='推送时间,24 小时制 "HH:MM",例如 "19:30"'
    )
    daily_limit: int | None = Field(
        None, ge=1, le=50, description="一张推送里最多列几道题(防一次压垮孩子)"
    )
    student_id: int | None = Field(None, description="把某个已存在的学生设为默认")
    student_name: str | None = Field(
        None, max_length=20, description="按名字设为默认学生;名字不存在就新建"
    )
    solve_on_recognize: bool | None = Field(
        None, description="拍照识别时是否让模型顺带补全答案与解析(默认关闭)"
    )
    targets: list[int] | None = Field(
        None,
        description=(
            "推给哪些成员(成员 id 列表)。空列表 = 所有已绑微信的成员。"
            "⚠️ 只有绑了微信的成员收得到。"
        ),
    )


class QuizMe(BaseModel):
    """取今天的复习清单。孩子端只会有题干与两个按钮。"""

    subject: str | None = Field(None, max_length=20, description="只复习某个学科;留空则全部")
    count: int = Field(10, ge=1, le=50, description="最多取几道")


class QuizResult(BaseModel):
    """记录一次复习结果。"""

    unit_id: int | None = Field(None, description="复习单元 id(单单元记账;与 problem_id 二选一)")
    problem_id: int | None = Field(
        None,
        description=(
            "错题 id:把这次作答记到这道题**今天到期的所有单元**上。"
            "⭐ 孩子端一道题只有一张卡(D108),卡片里的 problem_id 就用它 —— "
            "按单元分开记会把一次表现当成几次证据。"
        ),
    )
    rating: int = Field(
        ...,
        description="评分:1=忘了,3=会了。⭐ 孩子端只有这两个(2/4 是家长端的细分)",
    )
    elapsed_ms: int | None = Field(None, ge=0, description="这次花了多少毫秒,用于发现「磨了很久」")


class ProblemDeleteInput(BaseModel):
    """删掉一道**录错了**的错题(2026-11-09 用户:"录错的,智能体自己也删除不掉")。

    ⚠️ 只有**没复习过**的题才真的删;有复习记录的题一律改成归档 —— 那些日志是统计口径
       与 FSRS 调参的证据(要攒 1000 条),删掉等于篡改历史。而"录错"的题本来就没复习过。
    """

    problem_id: int = Field(
        ...,
        description=(
            "错题 id(卡片里那个 problem_id)。delete 时必填;"
            "result 时与 unit_id 二选一。"
        ),
    )
    reason: str = Field("", max_length=100, description="为什么删(留个痕迹,审计里看得到)")


class KnowledgePointCreate(BaseModel):
    name: str = Field(..., max_length=40, description="知识点名称,例如 异分母分数加减")
    subject: str | None = Field(None, max_length=20, description="学科")
    aliases: list[str] = Field(
        default_factory=list,
        description="别名。同一件事的不同叫法必须归到一个知识点,否则统计会分裂。",
    )


class WeakPointRow(BaseModel):
    knowledge_point: str
    problem_count: int = Field(..., description="涉及这个知识点的错题数")
    unit_count: int = Field(..., description="复习单元数")
    lapses: int = Field(..., description="累计答错次数 —— 这才是「薄弱」的依据")


class StudyStats(BaseModel):
    total_problems: int
    by_subject: dict[str, int] = Field(default_factory=dict)
    weak_points: list[WeakPointRow] = Field(default_factory=list)
    due_today: int = Field(0, description="今天到期的复习单元数")
    next_due_local: str | None = Field(
        None, description="今天没到期时,下一次有题可复习是哪天"
    )
    students: int = Field(0, description="在册学生数")
    engine: str = Field("fixed", description="当前排程引擎(fixed | fsrs)")
    schedule_note: str = Field("", description="复习周期是怎么排的(人话)")
    source: str = "study"
