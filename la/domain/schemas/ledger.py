"""记账域的请求/响应模型。

⚠️ 这些类同时是两个东西:
    1. REST 端点的请求体(routers/ledger.py)
    2. LLM 工具的 parameters(tools.py 直接取 model_json_schema())
所以**字段描述会直接变成给模型看的说明** —— 写描述时要按「给模型看」的标准写。

M9 起这一域同时管**支出与收入**(标准复式记账,两条腿和为零):
  * 支出:支出腿 +amount,资产腿 -amount;
  * 收入:资产腿 +amount,收入腿 -amount。
方向由 direction 决定,而不是靠金额正负 —— 提示词里让模型判断正负号是自找麻烦。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SOURCE_CHOICES = ("manual", "chat", "import", "ui")
DIMENSION_CHOICES = ("category", "payee", "account", "day", "month")
DIRECTION_CHOICES = ("expense", "income")


class TxnCreate(BaseModel):
    """录入一笔账(支出或收入)的草稿。助手写入的一律是草稿,是否直写由开关决定。

    旧名 ExpenseCreate 仍然可用(见文件末尾的别名)。
    """

    direction: Literal["expense", "income"] = Field(
        "expense",
        description="记的是支出还是收入:expense=支出(默认),income=收入(工资/报销/退款/红包)。",
    )
    amount_cents: int = Field(
        ...,
        gt=0,
        le=100_000_000,
        description="金额,单位是**分**(人民币分,不是元)。例如 38 元填 3800。金额永远填正数,方向由 direction 决定。",
    )
    payee: str | None = Field(
        None, max_length=60, description="交易对方或商户,例如 老张牛肉面。不确定就留空。"
    )
    narration: str | None = Field(None, max_length=200, description="备注,例如 和同事聚餐")
    category: str | None = Field(
        None,
        max_length=30,
        description=(
            "分类名,例如 餐饮、交通、购物、工资。"
            "⭐ 只有当这个分类**确实已经在账本里**时才填(可以先用 ledger_categories 看一遍)。"
            "拿不准就留空:留空会先走规则引擎,规则没命中时工具会返回候选分类,由你来定 —— "
            "**不要**为了填满字段而瞎编一个分类名。"
        ),
    )
    account: str = Field(
        "支付宝", max_length=30, description="资金账户名,例如 支付宝、微信钱包、银行卡、现金。"
    )
    ts: str | None = Field(
        None, description="发生时间,ISO8601 格式(例如 2026-10-08T12:30:00+08:00)。缺省表示现在。"
    )
    source: Literal["manual", "chat", "import", "ui"] = Field(
        "chat", description="来源:聊天录入用 chat,界面录入用 ui。"
    )


# 旧名保留:它在 40+ 处调用点与测试里出现过,而语义只是"现在也管收入了"。
# ⚠️ 是**别名**,不是第二个模型 —— schema 单真源仍然只有一份。
ExpenseCreate = TxnCreate


class ExpenseQuery(BaseModel):
    """查询某个时间段的收支统计。回答「花了多少」「收了多少」「花在哪」必须用它,不能凭记忆。"""

    period: str = Field(
        ...,
        max_length=40,
        description=(
            "统计周期:2026-10(某月)、2026-Q4(某季度)、2026(某年)、"
            "2026-10-01(某一天,含当天)或 2026-10-01..2026-10-31(区间);"
            "也可以直接写「今天」「本月」「上月」。"
        ),
    )
    dimension: Literal["category", "payee", "account", "day", "month"] = Field(
        "category", description="按什么维度分组。问「花在哪」用 category。"
    )
    direction: Literal["expense", "income", "all"] = Field(
        "expense",
        description=(
            "口径:expense=只算支出(默认)、income=只算收入、all=收支一起(这时 total_cents 是总和,"
            "同时给出 expense_cents / income_cents / net_cents)。"
        ),
    )
    include_drafts: bool = Field(
        False, description="是否把未确认的草稿也算进去。默认不算。"
    )
    with_coverage: bool = Field(
        False,
        description=(
            "额外给一份**覆盖域**:账本一共多少笔、最早最晚一笔是什么时候、"
            "草稿/已入账各多少。回答「我一共记了多少」「有没有这几个月的记录」时打开它 ——"
            "不要拿本区间的合计当成全部。"
        ),
    )
    detail: bool = Field(
        False,
        description=(
            "额外给一份**逐笔明细**(txns:每一笔的时刻/金额/对方/备注/分类/账户/状态)。"
            "⭐ 用户要看「每一天的每一笔」「都买了什么」「列出来给我看」「对一下账」时打开它 ——"
            "只打开 dimension 拿到的是**按天/按分类的合计**,看不出同一天里那几笔各是多少。"
            "默认关着是因为明细会真吃输出预算;合计类问题(花了多少)不需要它。"
        ),
    )
    detail_limit: int = Field(
        200, ge=1, le=1000,
        description=(
            "detail=true 时最多带回几笔(默认 200,按时刻倒序)。"
            "超过会截断并在 detail_note 里写清楚漏了多少笔 —— 不要因此自己推算总数。"
        ),
    )


class TxnDetailRow(BaseModel):
    """一笔账的**明细**(detail=true 时 txns 里的元素)。

    ⚠️ 金额一律是**分**、永远正数,方向在 direction 上 —— 与账本页、与 ExpenseRead 同一口径。
    """

    id: int = Field(..., description="交易 id(用来纠正或追溯)")
    ts: str = Field(..., description="时刻(ISO8601,带时区)")
    day: str = Field(..., description="日期 YYYY-MM-DD(与 dimension=day 的分组键对得上)")
    direction: Literal["expense", "income"] = Field(..., description="这笔是支出还是收入")
    amount_cents: int = Field(..., description="金额(分),永远正数;38 元 = 3800")
    payee: str | None = Field(None, description="交易对方(可能没有)")
    narration: str | None = Field(None, description="备注/摘要(可能没有)")
    category: str | None = Field(None, description="分类名(可能未分类)")
    account: str | None = Field(None, description="资金账户名(支付宝 / 微信钱包 / 现金 …)")
    status: Literal["draft", "confirmed"] = Field(
        ..., description="draft=草稿(还没确认入账),confirmed=已入账"
    )
    source: str = Field("", description="这笔从哪进来的(chat / ui / import)")


class GroupRow(BaseModel):
    key: str = Field(..., description="分组值,例如 餐饮")
    total_cents: int = Field(..., description="该组的金额合计(分),按 direction 选中的口径")
    txn_count: int = Field(..., description="该组的笔数")
    expense_cents: int = Field(0, description="该组里的支出(分)")
    income_cents: int = Field(0, description="该组里的收入(分)")


class ExpenseQueryResult(BaseModel):
    """查询结果。数字全部来自 SQL 聚合,模型只负责叙述。"""

    period: str
    start: str = Field(..., description="区间的开始时间(ISO8601)")
    end: str = Field(..., description="区间的结束时间(ISO8601,不含)")
    dimension: str
    direction: str = Field("expense", description="本次口径:expense / income / all")
    total_cents: int = Field(..., description="按 direction 选中的合计(分)")
    expense_cents: int = Field(0, description="区间内的支出合计(分),不随 direction 变化")
    income_cents: int = Field(0, description="区间内的收入合计(分),不随 direction 变化")
    net_cents: int = Field(0, description="结余 = 收入 - 支出(分),可能为负")
    txn_count: int = Field(..., description="区间内的交易笔数")
    groups: list[GroupRow] = Field(default_factory=list)
    #: 逐笔明细(detail=true 才有)。⭐ 顺序是**给人看的顺序**:时刻倒序。
    txns: list[TxnDetailRow] = Field(
        default_factory=list,
        description="逐笔明细(只有 detail=true 才有):时刻/金额/对方/备注/分类/账户/状态",
    )
    txn_total: int = Field(
        0, description="区间内符合条件的总笔数(detail=true 时给出);txns 可能少于它(被截断)"
    )
    detail_note: str = Field("", description="明细口径说明;被截断时写明漏了几笔")
    source: str = Field("ledger", description="数据来源标识;回答时必须注明,便于追溯")
    sample_txn_ids: list[int] = Field(default_factory=list, description="样例交易 id,可点击追溯")
    note: str = Field("", description="口径说明,例如是否包含草稿")


class ExpenseCorrect(BaseModel):
    """纠正一笔账。纠正之后会自动把这条经验写成规则,下次同样的商户自动分对。"""

    category: str | None = Field(None, max_length=30, description="改成这个分类")
    payee: str | None = Field(None, max_length=60, description="改成这个交易对方")
    amount_cents: int | None = Field(
        None, gt=0, le=100_000_000, description="改成这个金额(单位:分)"
    )
    txn_id: int | None = Field(
        None, description="要纠正的交易 id;不传则纠正**最近一笔**"
    )
    learn: bool = Field(True, description="是否把这次纠正写成规则(推荐 true)")


class ExpenseConfirm(BaseModel):
    """确认一笔草稿入账。txn_id 由工具/界面给,**必须**过模型校验。"""

    txn_id: int = Field(..., ge=1, description="要确认入账的交易 id(先 expense_query 拿到)")


class ExpenseImport(BaseModel):
    """导入一份账单文件。同文件重复导入会被 sha256 直接短路。"""

    path: str = Field(..., max_length=500, description="账单文件路径(csv 或 xlsx)")
    source: str | None = Field(
        None, max_length=30, description="来源标识,如 import:alipay;留空则自动识别"
    )
    dry_run: bool = Field(False, description="只解析不入库,用于先看会导入多少笔")


class ExpenseRead(BaseModel):
    """写入后的交易视图。"""

    id: int
    ts: str
    payee: str | None = None
    narration: str | None = None
    status: str
    source: str
    direction: str = Field("expense", description="这笔是支出(expense)还是收入(income)")
    amount_cents: int = Field(..., description="这笔的金额(分),永远为正;方向看 direction")
    category: str | None = None
    account: str | None = None
    postings: list[tuple[str, int]] = Field(
        default_factory=list, description="分录明细 [(账户名, 金额分)],合计为零"
    )


# --------------------------------------------------------------------------
# 分类(M9:模型可以建议新分类,但**必须用户点头**才建)
# --------------------------------------------------------------------------
class CategoryList(BaseModel):
    """列出账本里已有的分类。记账前先看它,别凭印象编分类名。"""

    kind: Literal["expense", "income"] | None = Field(
        None, description="只看支出分类或收入分类;不传就两类都列"
    )


class CategoryCreate(BaseModel):
    """新建一个分类。

    ⚠️ 调它不等于建好了:系统会**先弹一个确认框给用户**,他点了同意才真的建。
    所以不要假设它一定成功,也不要因为用户没点就去记一笔"已经分好类"的账。
    """

    name: str = Field(..., max_length=30, description="分类名,2~6 个字,例如 宠物、健身、房租")
    kind: Literal["expense", "income"] = Field(
        "expense", description="这是支出分类还是收入分类"
    )
    reason: str | None = Field(
        None, max_length=100, description="为什么要新建它,会显示给用户看,例如 这笔是给猫买猫粮"
    )
    sample_payee: str | None = Field(
        None, max_length=60, description="触发这个分类的商户/对方,便于用户判断该不该建"
    )

