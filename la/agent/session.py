"""带记忆与考古的会话:事件日志 -> 检索 -> 考古 -> 裁决 -> 投影 -> 压缩。

一轮的完整流程:

    turn/start
    -> user/message 落盘(JSONL + 镜像)
    -> 代码层识别"显式记忆请求" -> memory/note + facts
    --- Phase QA(快路径,预算 2)---
        query_summary_pool(摘要池检索)
    --- Phase ARCH(慢路径,预算 3)---
        query_timestamp_range(按时间回查原文)
        spawn_sub_agents(4 并行,风险=confirm)
    --- Phase ADJUDICATE(0 工具)---
        纯函数裁决:时间升序 -> 冲突检测 -> 采信最新
    -> 投影装配(含考古证据块)
    -> 调模型
    -> 回写热度 / 巩固回填 / 必要时压缩
    -> turn/end

重启后的续跑不需要任何内存状态:对话从事件重放,记忆从摘要池检索,证据从档案回查。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from .. import config
from ..config import Settings
from ..family import identity
from ..domain.receipts import (
    Receipt,
    ReceiptLog,
    ReceiptReverter,
    build_fallback_reverter as _fallback_reverter,
)
from ..domain.reverters import build_reverter
from ..llm.client import ChatResult, LLMClient, Usage
from ..memory.adjudicator import Verdict, adjudicate, render_evidence_block
from ..memory.consolidate import consolidate
from ..memory.fact_gate import SOURCE_MODEL, SOURCE_RULE, FactGate, FactProposal
from ..memory.decay import (
    BASE_STRENGTH_ON_ARCHAEOLOGY,
    VISITS_PASSIVE,
    VISITS_USER_INITIATED,
    classify_bucket,
)
from ..tools.subagent import STATUS_AMBIGUOUS
from ..memory.notes import detect_notes
from ..memory.projector import (
    Projection, Projector, build_checkpoint_block, build_memory_block,
)
from ..memory.retrieval import STATUS_HIT, STATUS_MISS, RetrievalResult, retrieve
from ..memory.scratchpad import (
    SOURCE_SUB_AGENT,
    SOURCE_TIMESTAMP_RANGE,
    Evidence,
    Scratchpad,
)
from ..memory.store import MemoryStore
from ..memory.topics import normalize_tag, tags_from_events
from ..memory.summarize import build_summarizer
from ..memory.turn_lock import turn_lock
from ..memory.timerange import TimeRange, parse_range
from ..llm.tools import build_tool_catalog
from ..base.tools import build_base_tools, build_web_tools
from ..base.web import build_web_backend
from ..ops import settings_store
from ..domain.receipts import DOMAIN_ARTIFACT
from ..skills import SkillLibrary
from ..storage import db
from ..tasks.dispatcher import KIND_BRIEFING, KIND_STUDY, Dispatcher, Dispatch
from ..tasks.goals import GoalStore
from ..tasks.schedules import ScheduleStore
from ..tasks.todos import TodoStore
from ..tools.archive_tools import build_archive_tools
from ..tools.registry import (
    RISK_CONFIRM,
    RISK_LOW,
    USER_WAIT_TOOLS,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from ..tools.subagent import SubAgentReport, SubAgentRunner, SubAgentTask, build_runner
from ..tools.task_tools import build_task_tools
from .context_trace import read_records, record_context, trace_path_for
from .feedback import ASK_RESULT_HINT, AskHandler, Answer, Option, Question
from .persona import (
    PersonaAppend,
    PersonaError,
    PersonaStore,
    as_persona_line,
)
from ..base.phases import (
    CODE_PHASE_BUDGET_EXCEEDED,
    CODE_TOTAL_STEPS_EXCEEDED,
    PHASE_ADJUDICATE,
    PHASE_ANSWER,
    PHASE_ARCH,
    PHASE_QA,
    CircuitBreaker,
)
from .prompts import IDENTITY, build_runtime_context

CONVERSATION_EVENT_TYPES = ("user/message", "assistant/message")

# "等用户"的工具(ask_user / category_create)的耗时**不计入**一轮的墙钟上限(D105):
# 那不是模型在做功,是用户在思考。名单的唯一口径在 tools/registry.py —— 那边还要用它
# 豁免"自动重试"(超时的含义是"用户没回答",重试=把同一个问题再弹一次)。
COMPACTION_EVENT_TYPES = ("user/message", "assistant/message", "memory/note")
#: 单次唤醒最多写几条。超过就由参数守卫拒掉,把剩下的写进报告。
WAKE_MAX_WRITES = 20
#: 唤醒指令的**代码侧**前缀:无人值守的规则不能由用户/模型改写。
WAKE_INSTRUCTION_HEADER = (
    "【定时唤醒】现在没有人和你在对话。你在按用户预先设定的任务自主执行一次。\n"
    "规则:\n"
    "1. 不要问用户,也不要说「请告诉我 / 要不要」—— 没人会回答。\n"
    "2. 只做追加型写入;删除、移动、取消会被系统拒绝,把这类事写进报告让用户回来做。\n"
    "3. 一切正常、也没写数据:不要调用 wake_report,直接结束。\n"
    "4. 发现要用户处理的事,或本次写了数据:调用 wake_report 说清楚(结论先行,短句)。\n"
    "5. 数字必须来自工具结果,不要编;报告要能在微信一屏看完。\n\n"
    "本次任务:\n"
)
RECENT_EVENTS_KEPT = 6
#: 记忆检索(query_summary_pool)的单次输出上限。_converse 对工具结果 >4,000 字符
#: 会转成截断预览;这里留出余量,保证返回的是**合法 JSON**而不是被剪断的字符串。
MAX_QUERY_TEXT_CHARS = 300
MAX_QUERY_ANCHORS = 12
MAX_QUERY_OUTPUT_CHARS = 3_600


def _summary_excerpt(text: str, terms: Any, limit: int = MAX_QUERY_TEXT_CHARS) -> str:
    """返回与查询相关的一段,而不是永远取摘要开头 300 字。

    先在摘要里找查询词第一次出现的位置,把窗口放在它周围;一个都没命中才退回开头。
    """
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    lowered = flat.lower()
    positions: list[int] = []
    for term in terms or ():
        needle = str(term or "").strip().lower()
        if not needle:
            continue
        found = lowered.find(needle)
        if found >= 0:
            positions.append(found)
    if not positions:
        return (flat[:limit - 1] + "…") if limit > 1 else flat[:limit]
    start = max(0, min(positions) - limit // 3)
    end = min(len(flat), start + limit)
    result = ("…" if start > 0 else "") + flat[start:end] + (
        "…" if end < len(flat) else ""
    )
    # 省略号也算长度:调用方按 MAX_QUERY_TEXT_CHARS 卡,必须真的不超。
    return (result[:limit - 1] + "…") if len(result) > limit else result
# ⚠️ 2026-10-17:原来这里还有一个 COMPACTION_TARGET_RATIO = 0.5。D134 之后压缩触发线
#    是写死的 BUDGET_COMPACT_TRIGGER(120000)、保留比例是 BUDGET_COMPACT_RETAIN_RATIO(20%),
#    那个 0.5 既不参与判定、又被分析脚本照着算 —— 属于"文档在说假话",已删。
WIDEN_DAYS = 7
SUBTASK_LABEL = "时段"

#: 模型"报账"的话术(说了"已经办好")。⭐ 这不是自然语言理解,而是**输出诚实性检查**:
#: 只有当这一轮**一条回执都没有、一个写入工具都没调**时才会用到它,命中就当场更正。
#:
#: 为什么必须由代码兜住:2026-10-13 真机 —— 用户在微信说"明天中午的时候更新云朵智能体",
#: 模型**一个工具都没调**(tool_calls 为空),却回了一张像模像样的回执卡
#: ("**新建提醒(收据 45)**:明天 2026-10-14 12:00…"),编号还是十分钟前那批复习回执的。
#: 用户去日历页找,什么也没有 —— "它说做了"被当成了事实,比"没做"更糟。
#:
#: ⚠️ 刻意**不收**光秃秃的"删了/改好了":那两句话在假设句里也会出现
#: ("你要是把那条删了…"),误报会往一轮正常的回答里塞一句莫名其妙的更正。
#:
#: ⚠️ 2026-10-22 真机又踩了两次**误报**(用户点名"更正反复出现"):
#:   * "另一条…是 10-12 的,已经过去了(已取消的那条会议不计入在内)" ——
#:     "已取消"是在**描述历史状态**,不是这一轮的动作;
#:   * "我说「记好了」却没生成文件" —— 是在**转述/自省**,不是报账。
#:   所以:① 去掉"已取消/已删除/已完成"这类状态词,只留明确的完成语;
#:        ② 匹配前先剥掉成对的中文/英文双引号 —— 引号里的"记好了"是引用,不是承诺;
#:        ③ 名字后面跟"的"的(已建立的那条)是定语,不是这一轮的完成动作。
QUOTED_SPAN_RE = re.compile(
    r"“[^”]{0,300}”|\"[^\"]{0,300}\"|(?<=[说称写句])「[^」]{0,300}」"
)
WRITE_CLAIM_RE = re.compile(
    r"已(经)?(记好|记下|建好|定好|加好|存好|写好|办好|新建|创建|添加|建立)(?!的)"
    r"|已(经)?(取消|删除)(?!的)"
    r"|记好了|记下了|建好了|定好了|加好了|存好了|写好了|办好了|删掉了|新建了|添加了"
    r"|收据\s*[#＃]?\s*\d+|回执\s*[#＃]?\s*\d+"
)


def claims_write(text: str) -> bool:
    """这句话像不像在说"我已经写进去了"。**只做筛查**,事实由回执决定。

    ⚠️ 先剥引号:2026-10-22 那次"我说「记好了」却没生成文件"就是被引号里的字骗到的。
    """
    cleaned = QUOTED_SPAN_RE.sub("", str(text or ""))
    return bool(cleaned) and WRITE_CLAIM_RE.search(cleaned) is not None

#: 发现"只报账、没调用"时补一轮的唯一指令。⭐ 措辞要点:
#:   * 明确说"没有任何写入工具调用" —— 模型上一轮就是漏了这一步;
#:   * 要求**立刻调用**,不是再解释一遍;
#:   * 给"本来就不需要写"留出口,免得它为了迎合而乱写。
REPAIR_WRITE_PROMPT = (
    "（系统核对）你刚才的回复说事情已经办好，但这一轮没有任何写入工具调用。"
    "现在立刻调用完成用户请求所需的那个写入工具；如果已经写过就不要重复写。"
    "工具成功后再用一句话回复结果。如果用户这句话本来就不需要写入，"
    "就直接说明没有写，不要改口。"
)


# 记忆线索:出现这些词说明用户在问"过去发生过的事",值得翻档案
MEMORY_CUES = (
    "上次", "上回", "之前", "以前", "那次", "那天", "当时", "原先", "原来",
    "说过", "说的", "聊过", "聊的", "提过", "提到", "记不记得", "还记得",
    "什么时候", "多少", "查一下", "翻一下",
)


@dataclass
class PhaseOutcome:
    breaker: CircuitBreaker
    pad: Scratchpad
    qa: RetrievalResult
    verdict: Verdict
    time_range: TimeRange | None = None
    tool_results: list[ToolResult] = field(default_factory=list)
    archaeology_ran: bool = False
    reports: list[SubAgentReport] = field(default_factory=list)


class MemorySession:
    """一个持久化的会话。与 AgentLoop 同接口,便于 CLI 互换。"""

    def __init__(
        self,
        client: LLMClient,
        settings: Settings,
        *,
        store: MemoryStore | None = None,
        session_id: str | None = None,
        projector: Projector | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        assistant_conn: Any = None,
        approver: Callable[[ToolSpec, dict[str, Any]], bool] | None = None,
        auto_approve: bool = False,
        auto_briefing: bool = True,
        briefing_hour: int = 9,
        direct_write: bool | None = None,
        on_step: Callable[[str, dict[str, Any]], None] | None = None,
        asker: "AskHandler | None" = None,
        surface: str = "",
        wake: bool = False,
        on_wake_write: Callable[[], None] | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        # 对话入口(weixin / web / cli,见 prompts.SURFACES):用户是在手机上还是在电脑前。
        # ⚠️ **可变**:桥接层一个进程里可能同时服务多个渠道,所以每轮按来源重设,
        #    而不是在构造时定死。
        # ⚠️ 与 memory 层那个 Event.surface **不是一回事**:那个是布尔量,指"这条事件
        #    进不进对话投影";这里是字符串,指"用户从哪个入口来"。别混。
        self.surface = surface
        self.session_id = session_id or settings.session_id
        #: 领域 owner 过滤用的成员。CLI/后台的会话 id 不是 member:<id>,这里为 None = 管理员视角。
        self.member_id = identity.member_id_from_session(self.session_id)
        self.store = store if store is not None else MemoryStore(db.connect_memory())
        self.projector = projector or Projector(settings)
        self.summarizer = build_summarizer(client, settings)
        # 事实准入闸(2026-10-13):规则/模型只**提议**,由它裁决后才写 facts。
        # 用同一个 client —— 离线(fake)模式下走 fake 的确定性判决,链路照样跑通。
        # ⚠️ 2026-10-22:确认不在闸里做,而在 memory_note 的工具回调里做
        #    (_confirm_and_add_fact):先问用户,确认后才 propose + adjudicate。
        #    这样"未确认"的候选根本不会产生;规则抓到的显式陈述保持原行为。
        self.fact_gate = FactGate(self.store, client=client, session_id=self.session_id)
        self.assistant_conn = assistant_conn
        # 人格(D117):**上半段是数据**(sys_settings 里的 persona_text),每轮现读 ——
        # 用户刚在人格页改完,下一轮就是新的她,不用重启进程。没有领域库连接时
        # 退化到出厂默认(读得到、存不下;存的时候会如实报错,不假装成功)。
        self.persona = PersonaStore(assistant_conn)
        self.approver = approver
        self.auto_approve = auto_approve
        self.auto_briefing = auto_briefing
        # M7 直写:领域的写操作直接执行(安全网 = 写入回执 + 一键撤销)。
        # 默认跟随配置;显式传参可以按会话覆盖(测试与 CLI 用得上)。
        self.direct_write = settings.direct_write if direct_write is None else bool(direct_write)
        # 执行反馈挂钩:阶段/工具级步骤事件 → 前端步骤账本(每步含用时)
        self._on_step = on_step
        # 统一反馈机制(M9):模型与领域层都能"问用户一句"。**没接线就如实说问不了**,
        # 而不是假装用户同意了 —— 见 agent/feedback.py。
        self.asker: AskHandler | None = asker
        # ---- 定时唤醒模式 -------------------------------------------------
        # ⚠️ 唤醒与聊天的区别只在**这一轮怎么写事件、谁批准写**:
        #    * 指令不落 user/message(否则第二天聊天里会多一句"你说:核对账本");
        #    * 只有真的要报告/真写了数据,才成对落 user+assistant(带 via=wake);
        #    * asker=None -> 问用户一律 unavailable,记忆写入也拿不到确认;
        #    * 工具层挂参数守卫,只放追加型写入。
        self.wake_mode = bool(wake)
        self.wake_allow_writes = True
        self.wake_dry_run = False
        self.wake_report: dict[str, Any] | None = None
        self.wake_receipts: list[int] = []
        self.wake_write_calls = 0
        #: 每次"即将执行一次写入"时回调(跑批用它把 writes 落库,进程崩了也数得清)。
        self._on_wake_write = on_wake_write
        #: 给历史看的"原始任务"(不带代码加的那段无人值守规则)
        self.wake_instruction = ""
        #: 本轮开始前两本回执账的水位线(结束时捞"这次到底写了什么")。
        self.wake_receipt_marks: tuple[int, int] = (0, 0)
        self.current_turn = 0
        #: 本会话累计"模型自己提议了几条事实"(给执行反馈/排障用)
        self.facts_proposed = 0

        # 统一写入回执(M7):领域库那本给领域服务用,记忆库那本给事项层用。
        self.receipts: ReceiptLog | None = None
        self.memory_receipts: ReceiptLog | None = None
        if assistant_conn is not None:
            self.receipts = ReceiptLog(assistant_conn)
            self.receipts.session_id = self.session_id
        self.memory_receipts = ReceiptLog(self.store.conn)
        self.memory_receipts.session_id = self.session_id

        # 事项层:目标 / 待办 / 提醒,全部事件溯源
        self.goals = GoalStore(self.store.conn, emit=self._emit_task_event,
                               receipts=self.memory_receipts)
        self.todos = TodoStore(self.store.conn, emit=self._emit_task_event,
                               receipts=self.memory_receipts, member_id=self.member_id)
        self.schedules = ScheduleStore(
            self.store.conn, emit=self._emit_task_event,
            receipts=self.memory_receipts, member_id=self.member_id,
        )
        self.library = SkillLibrary()
        self.dispatcher = Dispatcher(
            self.store.conn,
            settings=settings,
            todos=self.todos,
            goals=self.goals,
            schedules=self.schedules,
            # ⚠️ 必须绑定本会话(2026-10-18 审计):不传 session 时,
            #    简报会把全家摘要池里“昨天最近的一条”注入这个成员的上下文。
            summaries_between=lambda start, end: self.store.summaries_between(
                start, end, session_id=self.session_id
            ),
            briefing_hour=briefing_hour,
            study_digest=self._study_digest,
        )
        self.pending_dispatches: list[Dispatch] = []

        # 领域层(记账):有领域库连接才装配,否则退化成一个"只有记忆"的会话
        self.ledger = None
        self.calendar = None
        self.reminders = None
        self.study = None
        self.health = None
        #: 笔记域(M33)。它不是记忆:住在领域库、不可重建、不衰减、可编辑。
        self.notes = None
        self.materials = None
        self.intake = None
        # 产物层:桥接层要按**回执里的句柄**把文件捞出来发给用户,所以这里留一个引用。
        # ⚠️ 必须与 StudyService 用**同一个实例**(同一个根目录),否则会变成
        #    "登记在这边、文件在那边"的两份产物目录。
        self.artifacts = None
        if assistant_conn is not None:
            from ..domain.assembly import build_domain_services

            db.set_member(assistant_conn, self.member_id)
            # ⚠️ 装配只有一处(2026-10-18 审计修):素材层/录入流水线/四个域共用
            #    同一个时钟、同一个时区、同一个审计与回执 —— 与 Web 完全一致。
            #    以前这里不注入 now_provider/tzid,于是微信与网页对"本月 / 今天到期"
            #    各按各的算(deps.py 的注释宣称"只有一处时钟",那只对 Web 成立)。
            domain = build_domain_services(
                assistant_conn,
                settings=settings,
                receipts=self.receipts,
                member_id=self.member_id,
                # 微信/网页的录入会话都按“谁在录”归属。
                owner_session=self.session_id,
                # ⚠️ fake 提供方没有视觉能力,这里如实给 None(流水线会走"离线"分支),
                #    而不是让它返回一段假识别结果。
                client_factory=(None if settings.provider == "fake" else (lambda: client)),
            )
            self.ledger = domain.ledger
            self.calendar = domain.calendar
            self.reminders = domain.reminders
            self.study = domain.study
            self.health = domain.health
            self.notes = domain.notes
            self.artifacts = domain.artifacts
            # 素材层 + 拍照录入流水线。**手机那条主路径要用它**:
            # 微信来的照片先落素材层,然后由 intake 一步识别 + 录入。
            self.materials = domain.materials
            self.intake = domain.intake

        # 撤销入口:**两本回执账拼成一个按钮**(与 Web 层共用同一个装配函数)。
        # ⚠️ 事项层与记忆的回执在 memory.db,领域写入的在 assistant.db ——
        #   只能撤一边的撤销按钮是假的(用户点了没反应,还不知道为什么)。
        task_reverter = build_reverter(
            self.memory_receipts,
            todos=self.todos, goals=self.goals, schedules=self.schedules,
            memory=self.store,
        )
        self.reverter: ReceiptReverter | None = (
            _fallback_reverter(
                build_reverter(
                    self.receipts,
                    ledger=self.ledger, calendar=self.calendar, health=self.health,
                    study=self.study, todos=self.todos, goals=self.goals,
                    schedules=self.schedules,
                    # 记忆的"改口/遗忘"也走同一个撤销入口。
                    memory=self.store,
                    # 人格改写(D117):回执里带着上一版全文,撤销 = 退回上一版。
                    persona=self.persona,
                    # 产物层(M12):助手写出来的文件同样可撤销(artifact.file)。
                    artifacts=self.study.artifacts if self.study is not None else None,
                    # 笔记(M33):"记错了/删错了"要能在微信里一句 /撤销 就退回。
                    notes=self.notes,
                ),
                task_reverter,
            )
            if self.receipts is not None
            else task_reverter
        )

        self.runner: SubAgentRunner = build_runner(self.store, settings, client)
        self.registry = self._build_registry()
        self._active_breaker: CircuitBreaker | None = None

        self.turn_index = 0
        #: 这一轮是否停在"user/message 已落、assistant/message 还没落"的中间态。
        #: 失败/被中止时靠它补齐收尾事件,否则历史里会留下一条永远没有回复的提问。
        self._turn_open = False
        self.total_usage = Usage()
        self.last_active: str | None = None
        self.last_retrieval: RetrievalResult | None = None
        self.last_projection: Projection | None = None
        #: 最近一次 compact_once 的账(手动入口要如实告诉用户"压了没有、前后多少");
        #: 自动压缩也写,排障时能看出"这次是谁触发的"(via=auto/manual/cli)。
        self.last_compaction: dict[str, Any] | None = None
        #: 这一轮装配用的**料**(系统提示由哪几段拼成、裁剪前各段多大)——
        #: 只给轨迹页(D118)用,不参与任何执行判断。
        self.last_context_detail: dict[str, Any] | None = None
        self.last_outcome: PhaseOutcome | None = None
        self._last_qa: RetrievalResult | None = None
        self._on_event = on_event

    # -- 事项层事件 -------------------------------------------------------
    def _emit_task_event(self, event_type: str, data: dict[str, Any]) -> None:
        """目标/待办/提醒的每次变更都写一条事件 —— 所以三者都是可重放的。"""
        self.store.append(self.session_id, event_type, data)

    # -- 联网的账 ---------------------------------------------------------
    def _record_web_usage(self, payload: dict[str, Any]) -> None:
        """记一条 `web/usage`(D148)。

        ⚠️ 为什么必须记:官方 Search / Fetch **按成功请求次数**计费(5 元/1000 次),
        这笔钱**不在 token 账里** —— 不落这一行,报表就永远说"联网没花钱"。
        与 llm/usage 同一条纪律:写失败只吞掉自己,观测不该把回复链带崩。
        """
        try:
            self.store.append(self.session_id, "web/usage", dict(payload))
        except Exception:  # noqa: BLE001 - 账落不进去不影响这一轮
            pass

    # -- 工具注册 ---------------------------------------------------------
    def _build_registry(self) -> ToolRegistry:
        from ..tools.insight_tools import build_insight_tools

        # ⭐ D148:联网后端在**注册时**装配一次(只读配置,不碰网络)。
        #    没有 key 时 search 为 None、fetch 回落本机 —— 工具照样注册,
        #    调用时如实说明(工具清单随运行环境变会击穿前缀缓存,见 llm/tools.py)。
        _web = build_web_backend(self.settings)

        def _tool_names() -> tuple[str, ...]:
            """**模型可选**的工具名(延迟求值:建 registry 时它还没赋值)。

            ⚠️ 与使用报表同一口径:去掉框架自己发起的那几个(INTERNAL_TOOLS)。
               否则"从未调用"会把 spawn_sub_agents 这类**一直由系统调用**的工具
               也列进去,让模型以为自己有工具闲置着。
            """
            current = getattr(self, "registry", None)
            if current is None:
                return ()
            return tuple(name for name in current.names() if name not in INTERNAL_TOOLS)

        def _trace_records() -> list[dict[str, Any]]:
            """最近几轮轨迹(由"这一份记忆住在哪"决定路径,不读全局 data/)。"""
            archive = getattr(self.store, "archive_dir", None)
            root = Path(archive).parent if archive else None
            try:
                return read_records(limit=40, path=trace_path_for(root))
            except Exception:  # noqa: BLE001 - 轨迹读不到就不算"丢消息",不编数字
                return []

        # 护栏 G18:只读开关在这里生效 —— 工具层拒绝,而不是提示词里请求它别写。
        registry = ToolRegistry(
            audit_sink=self._audit, risk_approver=self._approve,
            read_only=self.settings.read_only,
            direct_write=self.direct_write,
            on_event=self._on_tool_event,
            guard=self._wake_guard if self.wake_mode else None,
        )
        registry.register_all(
            build_archive_tools(self.store, session_id=self.session_id)
        )
        # ⭐ M12 基础能力层:读 / 看 / 找 / 写。
        #    ⚠️ 视觉模型与素材层都可能是 None(离线模式、没有领域库),
        #       这时工具照样注册、调用时**如实报错**,而不是悄悄少几个工具 ——
        #       工具清单随会话变会击穿 KV 前缀缓存(见 llm/tools.py 的告诫)。
        registry.register_all(
            build_base_tools(
                materials=self.materials,
                artifacts=self.study.artifacts if self.study is not None else None,
                media=self.study.media if self.study is not None else None,
                receipts=self.receipts,
                receipt_domain=DOMAIN_ARTIFACT,
                vision_factory=(None if self.settings.provider == "fake" else (lambda: self.client)),
                vision_model=self.settings.model,
                session_id=self.session_id,
            )
        )
        # ⭐ M12 联网:默认关(LA_WEB_ENABLED=0)。工具**照样注册**,调用时如实说明 ——
        #    工具清单随配置变会击穿 KV 前缀缓存,也会让模型"以为不能联网"。
        registry.register_all(
            build_web_tools(
                # ⭐ 传**函数**而不是布尔:聊天页的「联网」按钮点了就生效,
                #    常驻的微信桥接会话不用重启(库里没记录时才回落到 .env 的默认值)。
                enabled=lambda: settings_store.web_enabled(
                    self.assistant_conn, default=self.settings.web_enabled,
                ),
                # ⭐ D148:后端换成 DeepSeek 官方 Search / Fetch(同一个 key)。
                provider=_web.search,
                fetcher=_web.fetch,
                provider_name=_web.search_name,
                fetch_name=_web.fetch_name,
                on_usage=self._record_web_usage,
                session_id=self.session_id,
            )
        )
        registry.register(
            ToolSpec(
                # ⚠️ required= 与 params= 各写一份就是"声明了两遍,只生效一遍"的经典坑:
                #    这个工具曾经只写了 required,模型看到的是没有任何属性的对象(见 D90)。
                #    新增工具时两者必须一起写 —— tests/test_m13_tool_schema.py 会拦。
                name="query_summary_pool",
                description=(
                    "检索长期记忆摘要池;回答关于过去的问题时先用它(快路径)。"
                    "⭐ query **原样用用户那句话里的关键词**,别改写成书面语 —— "
                    "中文检索按停用词切词,改写了反而匹配不上。"
                    "未命中就如实说「记忆里没有」,不要用听起来合理的猜测填空;"
                    "它没命中时再走 query_archive_search / query_timestamp_range(慢路径)。"
                ),
                phase=PHASE_QA,
                risk=RISK_LOW,
                timeout_ms=3000,
                handler=self._handle_summary_pool,
                output_budget_tokens=1500,
                required=("query",),
                params={
                    # 设计深化 §3.1 的参数表:query / tags / since / limit(1..20,默认 10)
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "要检索的关键词(用户原话里的词就行,例如 装修预算)",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "可选:只看带这些标签的摘要。标签由桶里出现过的工具/领域事件"
                                "自动打,取值:ledger(账目)/calendar(日程)/health(健康)/"
                                "study(错题)/task(待办·目标·提醒)/memory(明确记住的事)。"
                                "也接受中文(如 健康/账目/错题)。"
                            ),
                        },
                        "since": {
                            "type": "string",
                            "description": "可选:只看这个时刻之后的摘要(ISO8601,例如 2026-10-01T00:00:00+08:00)",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                            "description": "最多带回几条,默认 10",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            )
        )
        # ⭐ 统一反馈机制(M9):模型自己也能问用户一句,答案是**工具结果**,
        #    它接着往下做 —— 不需要用户重新说一遍。
        #    超时上限有两条:这里的 timeout_ms(硬上限)与 broker 的等待上限。
        registry.register(
            ToolSpec(
                name="ask_user",
                description=(
                    "问用户一个只有他能拍板的问题,拿到回答再继续。"
                    "⭐ 只在**真的需要他拍板**时用:新分类该叫什么、两笔账哪笔对、"
                    "这一笔算收入还是支出、两个日程冲突了保哪个。"
                    "能在账本/日历/健康页里查到的事实**不要问** —— 先查。"
                    "options 给 2~5 个「点一下就完事」的说法,用户也可以自己打字。"
                    "一次只问一个问题,问完等回答;"
                    "拿到 status=answered 就按 selected/text 执行;"
                    "status=timeout/cancelled/unavailable 表示**他没回答,不是同意**。"
                ),
                phase=PHASE_ANSWER,
                risk=RISK_LOW,
                timeout_ms=600000,
                handler=self._handle_ask,
                output_budget_tokens=400,
                writes=False,
                required=("prompt",),
                params={
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "要问用户的那句话,直接问,别绕。例:这笔修车费想放哪个分类?",
                        },
                        "options": {
                            "type": "array",
                            "maxItems": 6,
                            "description": "给用户点的选项。每个选项是一句能直接执行的话。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string", "description": "按钮上的字,≤16 字"},
                                    "description": {
                                        "type": "string",
                                        "description": "这个选项意味着什么,一句话",
                                    },
                                    "value": {
                                        "type": "string",
                                        "description": "选中后回给你的值;不填就等于 label",
                                    },
                                },
                                "required": ["label"],
                                "additionalProperties": False,
                            },
                        },
                        "allow_text": {
                            "type": "boolean",
                            "description": "是否允许用户自己写一段回答,默认 true",
                        },
                        "multi": {"type": "boolean", "description": "是否允许多选,默认 false"},
                        "domain": {
                            "type": "string",
                            "description": "这次提问属于哪个域:ledger/calendar/health/study/task/memory/system",
                        },
                        "context": {
                            "type": "object",
                            "description": "给用户看的上下文(金额、时间、候选分类等),前端原样显示",
                        },
                    },
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
            )
        )
        # ⭐ 2026-11-21:用户主动要求压缩上下文。
        #    确认必须由**工具自己**发起(与 memory_note 同一条口径):
        #    模型只负责听懂"压缩上下文",不能替用户确认;没回答/答得含糊一律不压。
        registry.register(
            ToolSpec(
                name="compact_context",
                description=(
                    "用户明确要求压缩上下文 / 压缩记忆 / 整理聊天记录时调用。"
                    "调用后会先问用户一句确认;他确认后才把最旧的一段历史压成摘要"
                    "(保留最近约 20%),原始事件仍在归档里,摘要以后靠检索使用。"
                    "⭐ 不要自己先问一遍,也不要只解释「不能压缩」;"
                    "工具结果说没压成,就不要声称已压缩。"
                ),
                phase=PHASE_ANSWER,
                risk=RISK_LOW,
                timeout_ms=600000,
                handler=self._handle_compact_context,
                output_budget_tokens=600,
                # 压缩会新增摘要并改写投影 —— 与写操作同一条只读闸。
                writes=True,
                params={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            )
        )
        registry.register(
            ToolSpec(
                # ⚠️ 同 query_summary_pool:required 与 params 必须成对出现(D90)。
                name="spawn_sub_agents",
                description=(
                    "派并行子代理分头挖掘档案的多个时间窗;上下文隔离,返回结构化报告。"
                    "⭐ 只在**一个时间窗翻不完**时用它(总步数上限只有 6 步);"
                    "每个任务给一个明确的问题 + 一个不重叠的时间窗,最多 4 个。"
                    "时间窗给具体时刻(ISO8601),不要写「上个月」这种相对说法。"
                ),
                phase=PHASE_ARCH,
                risk=RISK_CONFIRM,
                timeout_ms=150000,
                handler=self._handle_spawn,
                output_budget_tokens=2500,
                required=("tasks",),
                params={
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "maxItems": 4,
                            "description": "分派给子代理的时间窗任务;每个代理只看自己那一段,最多 4 个",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "要在这个时间窗里找什么(一句话,含关键词)",
                                    },
                                    "start": {
                                        "type": "string",
                                        "description": "时间窗开始,ISO8601 带时区,例如 2026-09-01T00:00:00+08:00",
                                    },
                                    "end": {
                                        "type": "string",
                                        "description": "时间窗结束,ISO8601 带时区(不含)",
                                    },
                                    "label": {
                                        "type": "string",
                                        "description": "这一段叫什么(给用户看的短标签,如 九月的装修讨论)",
                                    },
                                },
                                "required": ["query", "start", "end"],
                                "additionalProperties": False,
                            },
                        },
                        "max_agents": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 4,
                            "description": "最多派几个(默认 2,上限 4);任务数与它取小",
                        },
                        "max_tokens_per_agent": {
                            "type": "integer",
                            "minimum": 100,
                            "maximum": 2000,
                            "description": "每个代理返回片段的 token 上限(默认 500);代码会强制截断",
                        },
                    },
                    "required": ["tasks"],
                    "additionalProperties": False,
                },
            )
        )
        if self.ledger is not None:
            from ..domain.tools import (
                build_calendar_tools,
                build_health_tools,
                build_ledger_tools,
                build_study_tools,
            )

            registry.register_all(
                build_ledger_tools(
                    self.ledger, session_id=self.session_id, direct_write=self.direct_write,
                    # 领域层也能提问(新建分类要用户点头)—— 用同一套机制。
                    ask=self.ask,
                )
            )
            if self.calendar is not None:
                registry.register_all(
                    build_calendar_tools(self.calendar, session_id=self.session_id)
                )
            if self.study is not None:
                registry.register_all(
                    build_study_tools(self.study, session_id=self.session_id)
                )
            if self.health is not None:
                registry.register_all(
                    build_health_tools(self.health, session_id=self.session_id)
                )
        if self.notes is not None:
            # ⚠️ 笔记工具**不在**上面那个 if self.ledger 里:笔记不依赖账本库,
            #    只依赖 assistant.db。装配条件写错会让"只有记忆的会话"里没有笔记。
            from ..domain.tools import build_note_tools

            registry.register_all(
                build_note_tools(self.notes, session_id=self.session_id)
            )
        registry.register_all(
            build_task_tools(
                todos=self.todos,
                goals=self.goals,
                schedules=self.schedules,
                library=self.library,
                # ⭐ 2026-10-22:模型主动记事实前先问用户(微信/网页同一套提问通道)。
                #    没确认就不写;用户说"不记"也不写。
                add_fact=self._confirm_and_add_fact,
                memory_note_marker=self._mark_note,
                # 记忆的"改口/遗忘"要能直接翻库:memory_correct / memory_recall。
                memory=self.store,
                # ⭐ 2026-10-25:待放行的候选要能在**对话里**放行(微信端唯一入口)。
                fact_gate=self.fact_gate,
                # ⭐ 2026-10-25:"关于她自己"的话归人格页,不进事实层。
                persona=self.persona,
                append_persona=self._confirm_and_append_persona,
                session_id=self.session_id,
            )
        )
        # ⭐ D138:自我观察(行为视角)。只在**主人明确要求审视自己**时调,不自动触发。
        #    ⚠️ 三个依赖都**延迟求值** —— 建 registry 的时候 self.registry 还不存在,
        #       而轨迹目录要跟着"这一份记忆住在哪"(与 _record_trace 同一条口径)。
        registry.register_all(
            build_insight_tools(
                self.store,
                assistant_conn=self.assistant_conn,
                catalog=_tool_names,
                trace_loader=_trace_records,
            )
        )
        # 定时唤醒专用:报告工具。**只在唤醒会话注册** —— 普通聊天里它不存在,
        # 模型也就不会误调。工具清单随会话变会击穿一次前缀缓存,但只发生在唤醒
        # 那一轮(罕见),比"普通聊天里多一个永远不用的工具"更划算。
        if self.wake_mode:
            registry.register(
                ToolSpec(
                    name="wake_report",
                    description=(
                        "只在**定时唤醒**里用:把这次要告诉用户的结果写下来。"
                        "⭐ 没有要用户处理的事、也没写数据时,**不要调用** —— 直接结束,"
                        "系统会记成一次静默成功。只有发现异常、或本次写了数据时才调用。"
                        "报告会由系统发到用户微信;你**不要**说自己已经发送/提醒过了。"
                    ),
                    phase=PHASE_ANSWER,
                    risk=RISK_LOW,
                    timeout_ms=1000,
                    handler=self._handle_wake_report,
                    output_budget_tokens=300,
                    required=("text",),
                    category="task",
                    writes=False,
                    params={
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "给用户看的报告正文:结论先行,短句,不要 Markdown 表格",
                            },
                            "level": {
                                "type": "string",
                                "enum": ["info", "warn"],
                                "description": "info=只是告知;warn=需要用户处理",
                            },
                            "findings": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "一条一条的发现(可选);没有就留空",
                            },
                        },
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                )
            )
        return registry

    # -- 定时唤醒 ---------------------------------------------------------
    def _wake_guard(self, spec: ToolSpec, params: dict[str, Any]) -> str | None:
        """参数级守卫:只读模式 / 单次写入上限 / 追加型白名单。"""
        if spec.writes and not self.wake_allow_writes:
            return "这次唤醒是只读的(试跑或任务关了写入),不能改数据。"
        if spec.writes and self.wake_write_calls >= WAKE_MAX_WRITES:
            return (
                "单次唤醒最多写 " + str(WAKE_MAX_WRITES) + " 条,已经到上限;"
                "把剩下的写进报告让用户回来做。"
            )
        from ..tools import wake_policy

        return wake_policy.check(spec, params)

    async def _handle_wake_report(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.wake_mode:
            return {"ok": False, "note": "现在不是定时唤醒,这条报告不会发送。"}
        text = str((params or {}).get("text") or "").strip()
        if not text:
            raise ValueError("text 不能为空")
        level = str((params or {}).get("level") or "info").strip().lower()
        if level not in ("info", "warn"):
            level = "info"
        findings = [
            str(item).strip()
            for item in ((params or {}).get("findings") or [])
            if str(item).strip()
        ][:20]
        self.wake_report = {
            "text": text[:2000],
            "level": level,
            "findings": findings,
        }
        return {
            "ok": True,
            "note": (
                "已交给系统;它会在本次唤醒结束时发到用户微信。"
                "**不要**说你已经发送/提醒过了 —— 送达结果由系统记在 /wake 页。"
            ),
        }

    def tool_catalog(self) -> list[dict[str, Any]]:
        """给模型看的工具清单。顺序稳定 → KV 前缀缓存才可能命中。"""
        return build_tool_catalog(self.registry.specs())

    # -- 统一反馈机制 -----------------------------------------------------
    async def ask(self, question: Question) -> Answer:
        """问用户一句,等他的回答。**工具层与领域层共用的唯一入口。**

        没接通道(测试、批处理)时返回 unavailable —— 调用方据此降级,
        绝不能把"没人回答"当成"用户默认同意"。
        """
        if self.asker is None:
            return Answer(
                question_id=question.id, status="unavailable", by="system",
                note="这个会话没有接提问通道(命令行/测试环境),请改用已有选项或先问用户",
            )
        answer = await self.asker(question)
        # ⭐ 2026-11-21(D184):用户看到的问答必须同时进入**对话历史**,不能只留在
        #    ops_questions / 工具结果里。否则模型下一轮根本不知道自己问过什么 ——
        #    真机"我查了档案,没找到我说过 8000"就是这么发生的。
        self._record_ask_transcript(question, answer)
        self._log_asked(question, answer)
        return answer

    async def _handle_ask(self, params: dict[str, Any]) -> dict[str, Any]:
        """ask_user 工具:模型自己发起的提问。"""
        options = [
            Option(
                label=str(item.get("label") or "").strip(),
                description=str(item.get("description") or "").strip(),
                value=str(item.get("value") or "").strip(),
            )
            for item in (params.get("options") or [])
            if isinstance(item, dict) and str(item.get("label") or "").strip()
        ]
        context = params.get("context")
        question = Question(
            prompt=str(params.get("prompt") or ""),
            options=options,
            allow_text=bool(params.get("allow_text", True)),
            multi=bool(params.get("multi", False)),
            domain=str(params.get("domain") or "system"),
            context=context if isinstance(context, dict) else {},
            source="agent",
            session_id=self.session_id,
        )
        # ⚠️ 留痕与"问答进对话历史"都在 self.ask() 里统一做(D184),
        #    这里不再手写 feedback/asked —— 否则同一次提问会有两条记录。
        answer = await self.ask(question)
        self.emit("ask", question=answer.question_id, status=answer.status)
        return {
            "question_id": answer.question_id,
            "question": question.prompt,
            "status": answer.status,
            "selected": answer.selected,
            "text": answer.text,
            "note": answer.note,
            "hint": ASK_RESULT_HINT.get(answer.status, ""),
            "reminder": "你刚刚问过用户一次;他的回答就是上面这个结果,不要再重复问同一个问题。",
        }

    async def _handle_compact_context(self, params: dict[str, Any]) -> dict[str, Any]:
        """compact_context 工具:先问确认,再压。**没确认一个字都不压。**

        ⭐ 确认必须发生在工具内部,不能只靠提示词让模型"记得先问":
           提示词里写"请先确认"不算数(与 memory_note 同一条纪律)。
        """
        status = self.compaction_status()
        if not status["compactable"]:
            return {
                "ok": False,
                "status": "nothing_to_compact",
                "events": status["events"],
                "messages": status["messages"],
                "history_tokens": status["history_tokens"],
                "target_tokens": int(status["retain_tokens"]),
                "note": (
                    "现在没有可压的区间(历史还短,或已经压到保留线)。"
                    "不用压,也不要告诉用户已经压缩。"
                ),
            }
        question = Question(
            prompt=(
                "要现在压缩上下文吗?\n"
                "当前历史约 " + format(status["history_tokens"], ",") + " token,"
                "目标保留约 " + format(status["retain_tokens"], ",") + " token。"
                "会多一次模型调用;最近几条原文要留着,实际可能略高于目标。"
                "原始记录不会丢,摘要以后仍可检索。"
            ),
            options=[
                Option(label="压缩", description="现在压一次", value="压缩"),
                Option(label="先不压", description="这次不动", value="先不压"),
            ],
            allow_text=True,
            domain="memory",
            source="agent",
            session_id=self.session_id,
            context={
                "当前历史": format(status["history_tokens"], ",") + " token",
                "目标保留": format(status["retain_tokens"], ",") + " token",
                "历史消息": str(status.get("messages", 0)) + " 条",
            },
        )
        answer = await self.ask(question)
        # ⭐ 把**问出去的那句话**带回工具结果:确认题是工具发到微信/网页的,
        #    它不在 conversation_history 里(surface=False),模型下一轮看不到自己问过什么。
        #    2026-11-21 真机因此出现"我查了档案,没找到我说过 8000" —— 它确实看不到。
        asked = {
            "question": question.prompt,
            "target_tokens": int(status["retain_tokens"]),
        }
        if not answer.answered:
            return {
                **asked,
                "ok": False,
                "status": "unconfirmed",
                "reason": answer.status,
                "note": "用户没有回答 —— 这不是同意,没有压缩。",
            }
        choice = (answer.chosen() or answer.text or "").strip()
        if any(word in choice for word in (
            "不压", "先不", "不用", "不要", "不好", "不是", "不行", "取消", "算了", "否", "别",
        )):
            return {
                **asked, "ok": False, "status": "declined",
                "note": "用户选了先不压,这次没有压缩。",
            }
        if not any(word in choice for word in (
            "压缩", "压", "可以", "好的", "嗯", "好", "行", "是", "对", "确认",
        )):
            # 问了但答得含糊(或答了别的事):按"没确认"处理,绝不默认同意。
            return {
                **asked, "ok": False, "status": "unconfirmed",
                "note": "回答含糊,按没确认处理;没有压缩。",
            }
        result = await self.compact_manual()
        # 问出去的那句话必须带回;target 以**真正执行时**重算的为准(问的这段时间
        # 历史可能又变了),只有拿不到时才回落到问之前的估算。
        result["question"] = question.prompt
        result.setdefault("target_tokens", int(status["retain_tokens"]))
        if result.get("ok"):
            note = (
                "已压缩:历史 " + format(int(result["before_tokens"]), ",")
                + " → " + format(int(result["after_tokens"]), ",")
                + " token(目标保留 " + format(int(result["target_tokens"]), ",")
                + "),摘要 #" + str(result["summary_id"])
                + "。用一句话告诉用户结果;工具已经真的压了。"
            )
            if not result.get("target_reached"):
                note += (
                    "⭐ 实际比目标高:压缩只动最旧区间,最近几条事件里的原文必须保留;"
                    "跟用户如实说明,不要说「只剩目标那个数」。"
                )
            result["note"] = note
        else:
            result["note"] = (
                "这次没有压成(" + str(result.get("reason") or result.get("status"))
                + ")。不要告诉用户已经压缩。"
            )
        return result

    async def _converse(
        self,
        messages: list[dict[str, str]],
        breaker: CircuitBreaker,
        *,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> ChatResult:
        """回答阶段:模型可以主动调工具,直到它给出最终答复。

        工具清单**每轮完全一样**,不做任何按场景增删 —— 那会击穿 KV 前缀缓存。
        ⚠️ 唯一的例外是**到顶收尾那一轮**:把 tools 整个撤掉(`tools=None`),
        模型只能按现有结果成文(云朵语义,见 LIMIT_ANSWER_TOOL_CALLS/D101)。
        代价只发生在最后一轮,换来的是一句"不会再有人被拒"。

        ⚠️ on_delta / on_reasoning **每一轮都要传**:发起工具调用的那一轮本来就没有正文,
        真正要显示的答复在最后一轮 —— 只在 step==0 传会导致"答了但没显示"。
        """
        from ..base.phases import LIMIT_ANSWER_ROUNDS, LIMIT_ANSWER_TOOL_CALLS, PHASE_ANSWER

        catalog = self.tool_catalog()
        working: list[dict[str, Any]] = list(messages)
        breaker.enter(PHASE_ANSWER)

        # 到顶标志(D101,语义照云朵 app/agent/loop.py::_budget_notice → force_text):
        # 一旦置位,**后面每一轮都不再带工具**,模型只能按现有结果成文。
        # 它同时挡住"额度用尽后还被拒的调用"—— 那种拒绝白烧一轮模型往返,
        # 而且模型会把错误结果读成"这事做不成了"(真机上它就是这么对用户说的)。
        exhausted = ""
        started = time.monotonic()
        paused = 0.0   # 等用户回答累计时长:从墙钟里扣掉(见 USER_WAIT_TOOLS)

        for step in range(LIMIT_ANSWER_ROUNDS):
            # 每轮开头重判一次:额度/时长任一到底就收工具
            if not exhausted:
                reason = ""
                worked = time.monotonic() - started - paused
                if config.AGENT_MAX_TURN_SEC > 0 and worked > config.AGENT_MAX_TURN_SEC:
                    reason = ("已达本轮时长上限 " + str(int(config.AGENT_MAX_TURN_SEC))
                              + " 秒,已收回工具,按现有结果收尾")
                elif breaker.answer_tool_calls >= LIMIT_ANSWER_TOOL_CALLS:
                    reason = ("已达本轮工具调用上限 " + str(LIMIT_ANSWER_TOOL_CALLS)
                              + "(读/写共用),已收回工具,按现有结果收尾")
                if reason:
                    exhausted = reason
                    # 账本里留一行:**用户要分得清"做完了"和"被叫停了"**(云朵 2026-09-27 的定调)。
                    # 早先只有模型自己在回复里提一句,它不提用户就永远不知道。
                    self._step_end("phase:budget", "收起工具,按现有结果收尾", ok=False, detail=reason)
            result = await self.client.chat(
                working, tools=None if exhausted else (catalog or None),
                on_delta=on_delta, on_reasoning=on_reasoning,
            )
            self._step_end(
                "phase:thinking",
                "模型已应答",
                detail=(str(len(result.reasoning)) + " 字思考 · " if result.reasoning else "")
                + "上下文 " + str(len(working)) + " 条消息"
                + (";它要求调工具" if result.wants_tools else ""),
            )
            # ⭐ D136:每一次模型调用的 token 账都落一条事件 ——「使用报表」的事实源。
            #   为什么落在**这里**(而不是只在 turn/end 落一次):前缀缓存率是按**单次调用**
            #   算的,一轮里模型可能被调 1~N 次(工具往返),把 N 次合成一次会让"多少命中"
            #   变成一笔糊涂账;而 turn/end 只有**最后一次**的 usage(见它自己的 payload)。
            #   ⚠️ 写失败只吞掉自己:报表是观测,不该把回复链带崩(与 _record_trace 同一条纪律)。
            try:
                self.store.append(
                    self.session_id,
                    "llm/usage",
                    {
                        "step": step,
                        "tools_sent": not exhausted,
                        "prompt_tokens": int(result.usage.prompt_tokens or 0),
                        "completion_tokens": int(result.usage.completion_tokens or 0),
                        "total_tokens": int(result.usage.total_tokens or 0),
                        "cache_hit_tokens": int(result.usage.cache_hit_tokens or 0),
                        "cache_miss_tokens": int(result.usage.cache_miss_tokens or 0),
                        # 思考也计入 completion;单列出来,才看得出"额度被思考吃光"
                        "reasoning_tokens": int(result.usage.reasoning_tokens or 0),
                        "finish_reason": str(result.finish_reason or ""),
                    },
                )
            except Exception:  # noqa: BLE001 - 账落不进去不该影响回答
                pass
            if on_delta is not None and result.text:
                # 只有真的往外送正文时才报"生成回答":工具轮里的 text 通常为空
                self._step("phase:answer", "生成回答", result.wants_tools is False)
            if not result.wants_tools:
                if on_delta is not None:
                    self._step_end(
                        "phase:answer", "回答已生成",
                        detail=str(len(result.text)) + " 字",
                    )
                return result
            # tools 已随请求发出 -> 历史 assistant 消息必须带 reasoning_content
            working.append(result.assistant_message(include_reasoning=True))
            for call in result.tool_calls:
                call_started = time.monotonic()
                tool_result = await self.registry.execute(
                    call.name,
                    call.parsed_arguments(),
                    breaker=breaker,
                    session_id=self.session_id,
                )
                if call.name in USER_WAIT_TOOLS:
                    # 等用户的时长不计入墙钟(D105):那是他在想,不是我们在跑
                    paused += time.monotonic() - call_started
                # 普通工具的行由工具层自己开/收(on_event → _on_tool_event);
                # ⚠️ 框架内部工具(query_summary_pool / spawn_sub_agents / todo_write …)
                # 的步骤行由发起方 run_phases 管,这里**一个字段都不能再发** ——
                # 否则它们会各多出一行重复的"检索长期记忆"(实测踩过)。
                if (not exhausted and tool_result.error_code
                        in (CODE_PHASE_BUDGET_EXCEEDED, CODE_TOTAL_STEPS_EXCEEDED)):
                    # 被预算挡下的调用:立刻置位,下一轮起不带工具(云朵语义)
                    exhausted = str(tool_result.message or tool_result.error_code)
                    self._step_end("phase:budget", "收起工具,按现有结果收尾", ok=False, detail=exhausted)
                if call.name not in INTERNAL_TOOLS:
                    detail = (
                        "返回 "
                        + str(len(json.dumps(tool_result.data, ensure_ascii=False, default=str)))
                        + " 字"
                        if tool_result.ok and tool_result.data is not None
                        else str(tool_result.message or tool_result.error_code)
                    )
                    self._step_end(
                        "tool:" + call.name,
                        self._tool_title(call.name) or call.name,
                        ok=tool_result.ok,
                        detail=detail,
                    )
                tool_payload: dict[str, Any] = {
                    "tool": call.name,
                    "ok": tool_result.ok,
                    "error_code": tool_result.error_code,
                    "via": "model",
                }
                # ⭐ D136:技能是**藏在 skill 工具里**的(list / load),它没有自己的工具名 ——
                #    记账时把技能名带上,「使用报表」才能逐个技能说清"调了多少次、成不成功"。
                #    参数解析失败就只留 action,不编一个技能名(与"认不出来不猜"同一条纪律)。
                if call.name == "skill":
                    try:
                        skill_args = call.parsed_arguments()
                    except Exception:  # noqa: BLE001 - 参数坏了不该影响这次工具调用
                        skill_args = {}
                    action = str(skill_args.get("action") or "")
                    tool_payload["skill_action"] = action or (
                        "load" if skill_args.get("name") else "list"
                    )
                    skill_name = str(skill_args.get("name") or "").strip()
                    if skill_name:
                        tool_payload["skill"] = skill_name
                self.store.append(self.session_id, "tool/result", tool_payload)
                self.emit("tool", tool=call.name, ok=tool_result.ok)
                payload: Any = (
                    tool_result.data
                    if tool_result.ok
                    else {"error": tool_result.error_code, "message": tool_result.message}
                )
                # ⚠️ 截断必须保持**合法 JSON**(2026-10-18 审计):旧实现直接
                #    `json.dumps(...)[:4000]`,模型收到的是一段断掉的 JSON,
                #    既解析不了也看不出哪里被剪。现在超长就换成一个带 preview 的对象。
                encoded = json.dumps(payload, ensure_ascii=False, default=str)
                if len(encoded) > 4000:
                    encoded = json.dumps(
                        {"truncated": True,
                         "note": "工具结果超过 4000 字符,下面是截断预览",
                         "preview": encoded[:3600]},
                        ensure_ascii=False,
                    )
                working.append(
                    {"role": "tool", "tool_call_id": call.id, "content": encoded}
                )

        # 轮数用尽(连成文那一轮都没轮到):收回工具,强制它给出现在能给的答复。
        # ⚠️ 别删这条兜底:上面每轮都可能带工具,轮数用光时最后一轮可能正好是个工具轮。
        return await self.client.chat(working, tools=None)

    def relay_outbox(self, limit: int = 50):
        """把领域写入产生的事件搬进归档(领域 -> 记忆 的唯一合法方向)。"""
        if self.assistant_conn is None:
            return None
        from ..domain.outbox import OutboxRelay

        relay = OutboxRelay(
            self.assistant_conn,
            append_event=self._append_domain_event,
            seen=self._domain_event_seen,
        )
        return relay.drain(limit)

    def _domain_event_seen(self, outbox_key: str) -> bool:
        """反查这条发件箱记录是否已经搬进归档。

        ⚠️ 不能用 '"outbox_key":"<key>"' 这种精确模式:json.dumps 默认在冒号后加空格。
        两个片段同时出现即可 —— key 本身带时间戳与金额,唯一性足够。
        """
        row = self.store.conn.execute(
            "SELECT 1 FROM events WHERE data LIKE ? AND data LIKE ? LIMIT 1",
            ("%outbox_key%", "%" + outbox_key + "%"),
        ).fetchone()
        return row is not None

    def _append_domain_event(self, event_type: str, data: dict[str, Any]) -> None:
        self.store.append(self.session_id, event_type, data)

    def _log_asked(self, question: Question, answer: Answer) -> None:
        """把"问了什么、他怎么答的"落一条事件 —— 与 ask_user 同一口径。

        ⚠️ 留痕失败不该把这次写入一起拖死(与 _confirm_and_add_fact 里原来是同一段)。
        """
        try:
            self.store.append(
                self.session_id,
                "feedback/asked",
                {
                    "question_id": answer.question_id or question.id,
                    "prompt": question.prompt,
                    "options": [item.label for item in question.options],
                    "status": answer.status,
                    "selected": answer.selected,
                    "text": answer.text,
                    "by": answer.by,
                },
            )
        except Exception:  # noqa: BLE001 - 留痕失败不该挡住写入本身
            pass

    def _record_ask_transcript(self, question: Question, answer: Answer) -> None:
        """把一次**已答**的问答写进普通对话事件(assistant 问 → user 答)。

        为什么不是新事件类型:对话投影/预算/压缩/摘要/网页历史全都已经认
        user/message + assistant/message。把问答落成这两类事件,它就自动被
        下一轮投影、被压缩、被网页历史看见 —— 不需要在每个消费方再补一遍。

        ⚠️ 只记 answered:没答/超时的提问不落用户消息,避免历史里出现一条
           没有回答的 assistant 提问、把下一句新请求误读成回答。
        """
        if not answer.answered:
            return
        prompt = str(question.prompt or "").strip()
        if not prompt:
            return
        # 回传的是**用户看到的 label**,不是内部 value(create / use:餐饮)。
        labels = {item.key(): item.label for item in question.options}
        chosen = [
            labels.get(str(item), str(item))
            for item in answer.selected
            if str(item).strip()
        ]
        reply = "、".join(chosen) or str(answer.text or "").strip()
        if not reply:
            return
        question_id = answer.question_id or question.id
        try:
            self.store.append(
                self.session_id,
                "assistant/message",
                {
                    "text": prompt,
                    "tool_calls": [],
                    "reasoning": "",
                    "via": "ask",
                    "kind": "question",
                    "question_id": question_id,
                },
            )
            self.store.append(
                self.session_id,
                "user/message",
                {
                    "text": reply,
                    "via": "ask",
                    "kind": "answer",
                    "question_id": question_id,
                },
            )
        except Exception:  # noqa: BLE001 - 记录失败不能把问答本身拖死
            pass

    async def _confirm_and_add_fact(
        self,
        content: str,
        *,
        fact_type: str = "other",
        confidential: bool = False,
        supersedes: int | None = None,
    ) -> "FactProposal":
        """先问用户,确认后才写永久事实(2026-10-22)。

        ⭐ 用户点名:"事实在我没有确认的情况下已经写入;如果我在微信端,
          智能体应该发确认信息到我的微信,让我可以回复确认。"
          所以 memory_note 不再"先写后报",而是:

            ask_user(要记住这条吗?记住 / 不记)
              ├─ 回答"记住" -> 候选入库 -> 立刻过准入闸 -> 真的写进 facts
              ├─ 回答"不记" -> 什么都不写(返回 declined)
              └─ 没回答/通道不可用 -> 也不写(返回 unconfirmed)

        ⚠️ 为什么在工具里等、而不是回复之后再补一条确认:
          微信/网页的提问通道本来就能把问题送到用户手上并接回下一句
          (见 bridge.Thread._ask);放在工具里,模型拿到回答后可以直接说结果,
          不会出现"先说已记住、十分钟后才问要不要记"的错位。
        """
        shown = config.SECRET_PLACEHOLDER + "（敏感内容,不在聊天里展开）" if confidential else content
        verb = "改口" if supersedes is not None else "记住"
        question = Question(
            prompt=(
                "要" + verb + "这条吗？\n「" + shown + "」\n"
                "确认后才会写进长期记忆;不确认不会生效。"
            ),
            options=[
                Option(label="记住", description="写进长期记忆", value="记住"),
                Option(label="不记", description="这次不保存", value="不记"),
            ],
            allow_text=True,
            domain="memory",
            source="agent",
            session_id=self.session_id,
            context={"content": shown, "fact_type": fact_type},
        )
        answer = await self.ask(question)
        if not answer.answered:
            return FactProposal(candidate_id=0, status="unconfirmed")
        choice = (answer.chosen() or answer.text or "").strip()
        negative = any(
            word in choice
            for word in ("不记", "不用", "不要", "不好", "不是", "不行", "不确认", "取消", "算了", "否", "别")
        )
        if negative:
            return FactProposal(candidate_id=0, status="declined")
        positive = any(word in choice for word in ("记住", "确认", "保存", "可以", "好的", "嗯", "好", "行", "是", "对"))
        if not positive:
            # 问了但答得含糊(或答了别的事):按"没确认"处理,绝不默认同意。
            return FactProposal(candidate_id=0, status="unconfirmed")
        proposal = self.fact_gate.propose_candidate(
            content, source=SOURCE_MODEL, fact_type=fact_type,
            confidential=confidential, supersedes=supersedes,
        )
        if not proposal.candidate_id:
            # 同一句已经在队列里 —— 不再重复问、也不重复写
            return proposal
        row = self.store.get_candidate(proposal.candidate_id)
        if row is None:
            return proposal
        # ⭐ 先把候选挪出 pending 队列,再裁决:
        #    如果进程在"确认之后、裁决之前"挂了,这条候选会停在 review 等人工复核,
        #    而不是在下一轮被 process_pending 当成"没人确认"自动写进 facts。
        self.store.set_candidate_status(
            proposal.candidate_id, "review",
            reason="用户已确认,准入裁决中", verdict={"gate": "confirmed"},
        )
        row = self.store.get_candidate(proposal.candidate_id) or row
        admission = await self.fact_gate.adjudicate_row(row)
        if admission.status == "review":
            # ⭐ 2026-10-25:裁判拿不准时,**当场**问用户要不要放行。
            #
            #   以前这里只把 review 交回去,模型回一句"等审核通过才作数" ——
            #   而微信端没有任何审核入口,用户在微信里说"通过吧"没有任何动作,
            #   事实就永远写不进来(用户实际踩到的那段对话)。
            #   放行是人的决定,而人此刻就在对话里:问他一句,他就地就能拍板。
            admission = await self._ask_release(admission)
        return FactProposal(
            candidate_id=proposal.candidate_id,
            status=admission.status,
            fact_id=int(admission.fact_id or 0),
            supersedes=supersedes,
        )

    async def _ask_release(self, admission: Any) -> Any:
        """准入闸判不准时,就地请用户放行;他放行才写进 facts。

        ⚠️ 超时 / 答得含糊 / 说"先不放" 一律**保持 review**,绝不默认同意 ——
           与 memory_note 的第一道确认同一条口径。
        """
        shown = str(getattr(admission, "content", "") or "")
        reason = str(getattr(admission, "reason", "") or "")
        question = Question(
            prompt=(
                "准入闸判不准这一条,所以它还没生效:\n「" + shown + "」\n"
                + ("原因:" + reason[:80] + "\n" if reason else "")
                + "要放行吗?放行就直接写进长期记忆。"
            ),
            options=[
                Option(label="放行", description="直接写进长期记忆", value="放行"),
                Option(label="先不放", description="留在记忆页等我处理", value="先不放"),
            ],
            allow_text=True,
            domain="memory",
            source="agent",
            session_id=self.session_id,
            context={"candidate_id": int(getattr(admission, "candidate_id", 0) or 0)},
        )
        answer = await self.ask(question)
        if not answer.answered:
            return admission
        choice = (answer.chosen() or answer.text or "").strip()
        if any(word in choice for word in (
            "不放", "先不", "不用", "不要", "不好", "不是", "不行", "取消", "算了", "否",
        )):
            return admission
        if not any(word in choice for word in ("放行", "通过", "可以", "好的", "嗯", "好", "行", "是", "对")):
            return admission
        released = self.fact_gate.human_release(
            int(getattr(admission, "candidate_id", 0) or 0),
            approve=True, reason="用户在对话里放行", via="chat",
        )
        if released.status == "stale":
            return admission
        self.emit("fact_release", candidate_id=released.candidate_id,
                  status=released.status, fact_id=released.fact_id or 0)
        # 放行被拒收/判重时 returned FactAdmission 自带理由,原样交回去(不假装成功)
        return replace(
            admission, status=released.status, reason=released.reason,
            fact_id=released.fact_id, target_fact_id=released.target_fact_id,
        )

    async def _confirm_and_append_persona(
        self,
        text: str,
        *,
        name: str | None = None,
        note: str = "",
    ) -> PersonaAppend:
        """把一句"关于她自己"的话写进**人格页**(先问用户确认)。2026-10-25。

        ⭐ 用户原话:*"智能体的英文名应该是属于人格部分的内容,要更新到自己的人格
           界面"*。人格是**数据**(sys_settings),每轮现读 —— 写进去下一轮就生效,
           页面上也看得到;而事实层每轮无条件进上下文,且"我自己是谁"本来就在人格里,
           两处各说各的迟早打架。

        ⚠️ 三条与 memory_note 完全一致的口径:
           ① 先问、后写;没确认(unconfirmed / declined)**一个字都不写**;
           ② 校验不过(PersonaError)如实说理由,不假装写入;
           ③ 每次写入落一条可撤销回执(undo_plan 里带上一版全文 + 上一版名字)。
        """
        current = self.persona.state()
        line = as_persona_line(text, current.name) if str(text or "").strip() else ""
        wanted = " ".join(str(name or "").split())
        if not line and not wanted:
            return PersonaAppend("failed", reason="没有要写进人格的内容")
        if line:
            asked = "要把这句写进我的人格页吗?\n「" + line + "」"
        else:
            asked = "要把我的称呼改成「" + wanted + "」吗?"
        prompt = (
            (str(note).strip() + "\n" if str(note).strip() else "")
            + asked
            + "\n下一轮对话就生效;不确认不会写。"
        )
        question = Question(
            prompt=prompt,
            options=[
                Option(label="写进人格", description="下一轮对话生效", value="写进人格"),
                Option(label="先不写", description="这次不动人格", value="先不写"),
            ],
            allow_text=True,
            domain="system",
            source="agent",
            session_id=self.session_id,
            context={"persona_line": line, "persona_name": wanted},
        )
        answer = await self.ask(question)
        if not answer.answered:
            return PersonaAppend(
                "unconfirmed", added=line, name=current.name, reason="用户没有回答",
            )
        choice = (answer.chosen() or answer.text or "").strip()
        if any(word in choice for word in (
            "不写", "先不", "不用", "不要", "不好", "不是", "不行", "取消", "算了", "否", "别",
        )):
            return PersonaAppend(
                "declined", added=line, name=current.name, reason="用户选了先不写",
            )
        if not any(word in choice for word in (
            "写进", "写", "人格", "可以", "好的", "嗯", "好", "行", "是", "对",
        )):
            # 问了但答得含糊(或答了别的事):按"没确认"处理,绝不默认同意
            return PersonaAppend(
                "unconfirmed", added=line, name=current.name, reason="回答含糊,按没确认处理",
            )

        previous_body = current.body if current.custom else ""
        try:
            previous, notes = self.persona.append(line, name=wanted or None)
        except PersonaError as exc:
            return PersonaAppend(
                "failed", added=line, name=current.name, reason=str(exc),
                before=previous_body,
            )
        except RuntimeError as exc:  # 没有领域库 -> 没地方存(不假装成功)
            return PersonaAppend(
                "failed", added=line, name=current.name,
                reason="没存下来:" + str(exc)[:120], before=previous_body,
            )
        after = self.persona.state()
        renamed = after.name != current.name
        changed = after.body != current.body or renamed
        receipt_id = 0
        if changed:
            receipt_id = self._record_persona_append(
                current, previous, line, after=after, renamed=renamed,
            )
        self.emit("persona_append", status="appended" if changed else "unchanged",
                  renamed=renamed, receipt_id=receipt_id)
        return PersonaAppend(
            "appended" if changed else "unchanged",
            added=line,
            name=after.name,
            renamed=renamed,
            before=previous,
            notes=tuple(notes),
            receipt_id=receipt_id,
        )

    def _record_persona_append(
        self, before: Any, previous: str, line: str, *, after: Any, renamed: bool
    ) -> int:
        """人格追加的回执 —— 与人格页保存同一套(kind=persona.text,带上一版全文)。"""
        log = self.receipts
        if log is None:
            return 0
        from ..domain.receipts import DOMAIN_SYSTEM

        label = (
            "人格:改称呼为「" + str(after.name) + "」"
            if renamed else "人格:追加「" + (line[:16] or str(after.name)) + "」"
        )
        try:
            receipt_id = log.record(
                kind="persona.text",
                domain=DOMAIN_SYSTEM,
                label=label,
                detail={
                    "chars": after.chars, "before_chars": before.chars, "op": "append",
                    "name": after.name,
                    "before_name": before.name if before.name_custom else "",
                    "added": line[:120],
                },
                undo_plan={
                    "type": "persona", "before": previous,
                    "before_name": before.name if before.name_custom else "",
                },
                actor="agent",
                session_id=self.session_id,
            )
        except Exception:  # noqa: BLE001 - 记不上回执不该让这次人格写入失败
            return 0
        if receipt_id:
            log.conn.commit()
        return receipt_id

    def _add_fact(
        self,
        content: str,
        *,
        fact_type: str = "other",
        confidential: bool = False,
        supersedes: int | None = None,
    ) -> "FactProposal":
        """**提议**一条永久事实,返回 FactProposal(此刻还没有 fact_id)。

        ⚠️ 两个编号不能混(2026-10-18 审计修):
          * 候选编号 = fact_candidates.id,只是排队号;
          * 事实编号 = facts.id,只有准入闸裁决通过后才存在。
        以前这里返回候选编号、工具层当事实编号报给模型,于是 memory_note 说的编号
        要么查不到,要么指向另一条无关事实。

        ⚠️ supersedes(改口)**不在这里**停用旧事实:必须等新说法真的入库之后再做,
        否则候选被闸拒 = 旧记忆凭空消失。停用动作在 FactGate.adjudicate_row 里。
        """
        proposal = self.fact_gate.propose_candidate(
            content,
            source=SOURCE_MODEL,
            fact_type=fact_type,
            confidential=confidential,
            supersedes=supersedes,
        )
        if supersedes is not None:
            self.store.append(
                self.session_id,
                "memory/note",
                {
                    "content": content,
                    "fact_type": fact_type,
                    "explicit": True,
                    "via": "memory_correct",
                    "supersedes": int(supersedes),
                    "candidate_id": proposal.candidate_id,
                },
            )
        return proposal

    def _mark_note(self, content: str) -> None:
        self.store.append(
            self.session_id,
            "memory/note",
            {"content": content, "fact_type": "other", "explicit": True, "via": "memory_note"},
        )
        self.facts_proposed += 1

    def _approve(self, spec: ToolSpec, params: dict[str, Any]) -> bool:
        if self.auto_approve:
            return True
        if self.approver is not None:
            return bool(self.approver(spec, params))
        return False

    async def _handle_summary_pool(self, params: dict[str, Any]) -> dict[str, Any]:
        # 设计深化 §3.1 的参数表:query / tags / since / limit(1..20,默认 10)
        limit = min(max(int(params.get("limit") or 10), 1), 20)
        raw_tags = params.get("tags") or ()
        if not isinstance(raw_tags, (list, tuple)):
            raw_tags = [raw_tags]
        # 中文别名("健康")与小写领域名都能用;空值/纯空白不当作过滤条件。
        tags = [normalize_tag(tag) for tag in raw_tags if str(tag).strip()]
        result = retrieve(
            self.store,
            str(params["query"]),
            limit=limit,
            now=self.settings.now(),
            since=str(params["since"]) if params.get("since") else None,
            tags=tags,
            session_id=self.session_id,
        )
        self._last_qa = result
        # ⚠️ 2026-11-21(D185):以前每条摘要都带 300 字正文 + **全部 anchors**,
        #    "错题"一次命中 8 条就是 9,839 字符,超过 _converse 的 4,000 字符上限,
        #    模型拿到的是截断预览。现在:正文取与查询相关的一段、锚点只给命中/前 12 个,
        #    并按总字符预算逐条放;返回体保证是合法 JSON。
        hits_all: list[dict[str, Any]] = []
        for row in result.summaries:
            detail = result.details.get(row.id, {})
            matched = [
                str(item) for item in (detail.get("matched_anchors") or [])
                if str(item).strip()
            ]
            anchors = matched[:MAX_QUERY_ANCHORS]
            if not anchors:
                anchors = [str(item) for item in row.anchors[:MAX_QUERY_ANCHORS]]
            flat = " ".join(str(row.text).split())
            excerpt = _summary_excerpt(flat, result.terms)
            hits_all.append(
                {
                    "summary_id": row.id,
                    "bucket": row.start_time + " — " + row.end_time,
                    "text": excerpt,
                    "anchors": anchors,
                    "anchors_total": len(row.anchors),
                    "text_truncated": len(excerpt) < len(flat),
                    "anchors_truncated": len(anchors) < len(row.anchors),
                    "tags": list(row.tags),
                    "score": round(result.scores.get(row.id, 0.0), 6),
                    # 设计深化 §3.1 的输出字段:有没有命中"原样锚点"
                    "is_exact_anchor_hit": bool(
                        detail.get("is_exact_anchor_hit")
                    ),
                }
            )
        # 敏感事实不进对话(设计深化 §3.8 / §4.2)
        facts_all = [
            config.SECRET_PLACEHOLDER + "(#" + str(fact.id) + " 已标记为敏感)"
            if getattr(fact, "confidential", False)
            else fact.content
            for fact in result.facts
        ]
        payload: dict[str, Any] = {
            "status": result.status,
            "hits": [],
            "facts": facts_all,
            "total_hits": len(hits_all),
            "returned_hits": 0,
            "truncated": False,
        }

        def _size(item: dict[str, Any]) -> int:
            return len(json.dumps(item, ensure_ascii=False))

        # 先保 facts;事实通常很短,但别让它们把命中挤到预算外。
        if _size(payload) > MAX_QUERY_OUTPUT_CHARS:
            while payload["facts"] and _size(payload) > MAX_QUERY_OUTPUT_CHARS:
                payload["facts"] = payload["facts"][:-1]
                payload["facts_truncated"] = True

        for hit in hits_all:
            trial = dict(payload)
            trial["hits"] = payload["hits"] + [hit]
            if _size(trial) <= MAX_QUERY_OUTPUT_CHARS:
                payload["hits"].append(hit)
                continue
            # 还放不下:退到更短的片段/更少锚点;再不行就停(保留 total_hits 让模型知道还有)。
            compact = dict(hit)
            compact["text"] = hit["text"][:160]
            compact["anchors"] = hit["anchors"][:5]
            compact["text_truncated"] = True
            compact["anchors_truncated"] = True
            trial["hits"] = payload["hits"] + [compact]
            if _size(trial) <= MAX_QUERY_OUTPUT_CHARS:
                payload["hits"].append(compact)
                payload["truncated"] = True
                continue
            payload["truncated"] = True
            break

        payload["returned_hits"] = len(payload["hits"])
        if payload["returned_hits"] < len(hits_all):
            payload["truncated"] = True
        if any(
            hit.get("text_truncated") or hit.get("anchors_truncated")
            for hit in payload["hits"]
        ):
            payload["truncated"] = True
        return payload

    async def _handle_spawn(self, params: dict[str, Any]) -> dict[str, Any]:
        tasks = [
            SubAgentTask(
                query=str(item.get("query") or ""),
                start=str(item["start"]),
                end=str(item["end"]),
                # 模型参数里的 session_id 不可信/通常没有;默认只能查本会话。
                session_id=item.get("session_id") or self.session_id,
                label=str(item.get("label") or ""),
            )
            for item in params.get("tasks") or []
        ]
        per_agent = params.get("max_tokens_per_agent")
        reports = await self.runner.spawn(
            tasks,
            # 设计深化 §3.4:默认 2 个(派太多是白花钱);自动考古那条路会显式给 4
            max_agents=int(params.get("max_agents") or 2),
            max_tokens_per_agent=int(per_agent) if per_agent else None,
        )
        for report in reports:
            self.store.append(
                self.session_id,
                "subagent/result",
                {"report": report.as_dict()},
            )
        return {"reports": [report.as_dict() for report in reports]}

    def _audit(self, entry: dict[str, Any]) -> None:
        if self.assistant_conn is None:
            return
        from datetime import datetime, timezone

        try:
            self.assistant_conn.execute(
                "INSERT INTO ops_audit(ts, actor, action, target, detail, session_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    entry.get("actor", "agent"),
                    entry.get("action", "tool"),
                    entry.get("target"),
                    json.dumps(entry.get("detail") or {}, ensure_ascii=False),
                    entry.get("session_id"),
                ),
            )
            self.assistant_conn.commit()
        except Exception:
            self.assistant_conn.rollback()

    # -- 事件 -------------------------------------------------------------
    def emit(self, kind: str, **data: Any) -> None:
        if self._on_event is not None:
            self._on_event(kind, data)

    def start(self) -> None:
        first = self.store.last_seq(self.session_id) == 0
        self.store.ensure_session(self.session_id)
        if first:
            self.store.append(
                self.session_id,
                "session/start",
                {
                    "session_id": self.session_id,
                    "timezone": self.settings.timezone,
                    "provider": self.settings.provider,
                },
            )
        if self.auto_briefing:
            self.collect_dispatches()

    def collect_dispatches(self, now: Any = None) -> list[Dispatch]:
        """把到点的**播报**捡起来(每天最多一次简报 + 错题清单)。

        目标轮次的推进由 Dispatcher 在"简报真的派发成功"时完成 ——
        所以每次开进程不会平白吃掉一轮预算。

        ⚠️ **提醒(schedules 与日历提醒)一律不在这里认领**。这里没有送达渠道:
        `_briefing_text()` 只放 briefing/study,reminder 被显式过滤掉,也没有任何
        界面读 pending_dispatches。认领 = 已通知,在这里认领过的提醒就凭空消失了,
        而且再也不会响第二次。认领入口只有 la/bridge/reminder_push.py 一个。

        这个坑**踩实过一次**,而且比"看不见"更糟 —— 下面这段原先是这么写的:

            for instance in self.reminders.pending(now=moment):
                if not self.reminders.claim(instance.id, now=moment):   # ← 先认领
                    continue
                ... kind=KIND_REMINDER ...                              # ← 这里 NameError
            except Exception:  # 提醒失败不该挡住对话
                pass                                                    # ← 连报错都吞了

        `KIND_REMINDER` 在这个模块里**从来没有被导入过**,所以 try 块从第一轮起
        必然抛 NameError;而它在 claim() 之后、append() 之前 —— 日历提醒被标成
        "已通知"之后立刻被静默丢弃。取证:app_reminder_instances 里 id=1 的
        fired_at 有值,而 dispatches / events 里 **一条 calendar-reminder 都没有**。
        教训:认领动作后面不允许跟任何可能抛异常的代码,除非先确认送达。
        """
        moment = now or self.settings.now()
        self.pending_dispatches = self.dispatcher.deliver_pending(moment)

        for item in self.pending_dispatches:
            self.store.append(
                self.session_id,
                "dispatch",
                {"id": item.id, "kind": item.kind, "text": item.text},
            )
        return self.pending_dispatches

    def _study_digest(self, now: Any) -> str | None:
        """给调度器的错题推送回调。

        ⚠️ 写成方法而不是 lambda:调度器只需要一个"给我时间、给我文本"的纯函数,
        而错题服务可能在**没有领域库**的会话里缺席(那时返回 None,推送自然不发生)。
        """
        if self.study is None:
            return None
        return self.study.push_digest(now=now)

    def _briefing_text(self) -> str:
        # ⭐ 错题复习清单(study)也进这一段:它是**到点的推送**,就该出现在当轮上下文里。
        #    日程提醒(reminder)不进 —— 那是"提醒你去做",不该被当成事实来源复述。
        lines = [
            item.text
            for item in self.pending_dispatches
            if item.kind in (KIND_BRIEFING, KIND_STUDY)
        ]
        # 后台心跳可能已经推过了(到点时用户没在聊天)。那一条也要进上下文 ——
        # 否则"推送了但助手不知道",用户问"今天复习什么"它会答不上来。
        # ⚠️ 只补进上下文,**不再写一条 dispatch 事件**:事件日志不该因为多开几次
        #    聊天页面就多出几条一模一样的推送。
        try:
            today = self.dispatcher.study_push_today(self.settings.now())
        except Exception:
            today = None
        if today is not None and all(item.text != today.text for item in self.pending_dispatches):
            lines.append(today.text)
        return "\n".join(lines)

    def _memory_text(self, qa: RetrievalResult) -> str:
        return build_memory_block(
            facts=qa.facts,
            summaries=qa.summaries,
            status=qa.status,
            now_text="",
            briefing=self._briefing_text(),
            goals=self.goals.list(open_only=True),
            # ⭐ 有人在等放行的候选:模型必须知道,否则微信里用户说"通过吧"它接不住
            #    (2026-10-25)。只数本会话的 —— 别的成员的候选不该出现在这个上下文里。
            pending_review=self.store.review_count(session_id=self.session_id),
        )

    def _notes_hint(self, user_text: str) -> str:
        """笔记的"自动召回":命中时给一行标题线索,没命中一个字都不加。

        ⚠️ 这是**代码侧**的确定性召回,不是让模型自己想起 —— 真机上模型很少主动
           "想到"去查一个它不知道存在的本子。代价:每轮一次 FTS(约 1ms),
           命中时约 50 token,不命中 0。

        ⭐ 只给**标题**,不给正文:模型要不要读详情由它自己用 note_find(show) 决定。
           正文一进上下文,10 条笔记就能把预算吃掉一半。
        """
        if self.notes is None:
            return ""
        raw = str(user_text or "").strip()
        if not raw:
            return ""
        try:
            hits = self.notes.hint(raw)
        except Exception:  # noqa: BLE001 - 召回失败绝不影响这一轮
            return ""
        # 留痕:这条绕过工具管线(无门禁/无预算),不记事件就查不出"为什么没看见"。
        try:
            self.store.append(
                self.session_id, "journal/hint",
                {"query": raw[:200], "hit_ids": [int(item["id"]) for item in hits]},
            )
        except Exception:  # noqa: BLE001 - 记不上事件不该影响回答
            pass
        if not hits:
            return ""
        lines = ["", "<notes-hint>", "笔记里可能相关(**只是线索**,正文要用 note_find 取):"]
        for item in hits:
            lines.append(
                "- #" + str(item["id"]) + " " + str(item["title"])
                + "(" + str(item["folder"]) + " · " + str(item["date"]) + ")"
            )
        lines.append("</notes-hint>")
        return "\n".join(lines)

    # -- 投影 -------------------------------------------------------------
    def conversation_history(self, *, include_wake: bool = True) -> list[dict[str, str]]:
        """对话历史。

        `include_wake=False` 给**网页聊天渲染**用:唤醒那一轮(带 via=wake)不进
        普通聊天的时间线,但模型自己读历史时仍然看得见 —— 否则用户在微信回一句
        "那笔 32 是什么",它接不住。
        """
        ranges = self.store.compacted_ranges(self.session_id)

        def compacted(seq: int) -> bool:
            return any(start <= seq <= end for start, end in ranges)

        history: list[dict[str, str]] = []
        for event in self.store.events(self.session_id):
            if compacted(event.seq):
                continue
            if not include_wake and (event.data or {}).get("via") == "wake":
                continue
            if event.type == "user/message":
                history.append({"role": "user", "content": str(event.data.get("text") or "")})
            elif event.type == "assistant/message":
                message: dict[str, str] = {
                    "role": "assistant",
                    "content": str(event.data.get("text") or ""),
                }
                # ⚠️ 默认**不回传**跨轮思维链(config.REPLAY_HISTORY_REASONING):
                #    * API 只要求"轮内结果还没被消费的那条"带 reasoning_content **字段**,
                #      内容可以是空;跨轮历史(请求以 user 结尾)连字段都不校验
                #      —— 见 scripts/verify_reasoning_replay.py(真实模型 5 项);
                #    * 而真实库实测思维链 69500 字符 vs 正文 30000 字符 = 232%,
                #      带回去等于把历史窗口的三分之二花在"它当时怎么想的"上;
                #    * 同一条口径见云朵 app/agent/loop.py:452("它只活在本轮 transcript 里,
                #      不落库、不进跨轮历史")。DSH 相反:全文回传(靠 1M 窗口扛)。
                #    轮内那几轮的思维链照旧带(result.assistant_message(include_reasoning=True))。
                if config.REPLAY_HISTORY_REASONING:
                    reasoning = event.data.get("reasoning")
                    if reasoning:
                        message["reasoning_content"] = str(reasoning)
                history.append(message)
        return history

# -- 执行反馈(M7):阶段/工具步骤 ---------------------------------------
    def _step(self, key: str, title: str, running: bool = True) -> None:
        """开一步(或把同 key 的那一步重新打开)。

        key 由发起方给:**同一个 key = 同一行**。所以"检索记忆"反复发生也只占一行,
        而工具用 tool:名字 做 key,与"它到底跑了几次"解耦。
        """
        if self._on_step is None:
            return
        self._on_step(
            "step",
            {"key": key, "title": title, "status": "running" if running else "done"},
        )

    def _step_end(
        self, key: str, title: str, *, ok: bool = True, detail: str = ""
    ) -> None:
        if self._on_step is None:
            return
        self._on_step(
            "step",
            {
                "key": key,
                "title": title,
                "status": "done" if ok else "failed",
                "detail": detail[:200],
            },
        )

    def _on_tool_event(self, kind: str, data: dict[str, Any]) -> None:
        """工具层上报的开始/结束 —— 每步用时就是在这里被记下来的。

        ⚠️ 框架内部工具**不在这里开行**:它们的发起方(run_phases)
        已经用更准确的中文标题开了一步(`phase:qa` 等)。两边都开就会出现
        两行一模一样的"检索长期记忆" —— 实测踩过。
        """
        # 唤醒的账:写了几次、拿到哪些回执。恢复"跑到一半崩了"时靠它说清
        # "可能写了多少",而不是让用户自己猜。
        if self.wake_mode:
            if kind == "tool_start" and data.get("writes"):
                self.wake_write_calls += 1
                if self._on_wake_write is not None:
                    try:
                        self._on_wake_write()
                    except Exception:  # noqa: BLE001 - 记数失败不该挡住写入
                        pass
            elif kind == "tool_end":
                payload = data.get("data")
                if isinstance(payload, dict):
                    receipt_id = payload.get("receipt_id")
                    try:
                        if receipt_id:
                            self.wake_receipts.append(int(receipt_id))
                    except (TypeError, ValueError):
                        pass
        if self._on_step is None:
            return
        name = str(data.get("tool") or "")
        if name in INTERNAL_TOOLS:
            return
        if kind == "tool_start":
            spec = self.registry.get(name)
            self._on_step(
                "step",
                {
                    "key": "tool:" + name,
                    "title": self._tool_title(name, data.get("params") or {}),
                    "status": "running",
                    "domain": spec.domain if spec is not None else "",
                    "kind": spec.kind if spec is not None else "",
                },
            )
        elif kind == "tool_end":
            payload = data.get("data")
            if data.get("ok"):
                detail = (
                    str(len(json.dumps(payload, ensure_ascii=False, default=str))) + " 字结果"
                    if payload is not None
                    else ""
                )
            else:
                detail = str(data.get("error_code") or "")
            spec = self.registry.get(name)
            event: dict[str, Any] = {
                "key": "tool:" + name,
                "title": self._tool_title(name, {}) if data.get("ok") else
                         (self._tool_title(name, {}) + " 未完成"),
                "status": "done" if data.get("ok") else "failed",
                "detail": detail,
                # 领域/kind 由注册表给:Web 前端不必再维护一份 tool 名字 -> 领域 的映射。
                "domain": spec.domain if spec is not None else "",
                "kind": spec.kind if spec is not None else "",
            }
            # 写操作会带回执号(M7):前端据此在这一步上挂"撤销"按钮
            receipt_id = payload.get("receipt_id") if isinstance(payload, dict) else None
            if receipt_id:
                event["receipt_id"] = int(receipt_id)
            self._on_step("step", event)

    def _tool_title(self, name: str, params: dict[str, Any] | None = None) -> str:
        """工具名的中文说法。**只影响前端那一步显示什么**,不影响任何执行语义。

        ⚠️ 优先用注册表里的 title(由 tools/metadata.py 的单一真源补齐),
           其次退回兼容字典,最后才是 name.replace —— 新增工具忘了登记,只是名字难看。
        """
        spec = self.registry.get(name)
        base = (spec.title if spec is not None else "") or TOOL_TITLES.get(name)
        if not base:
            base = name.replace("_", " ")
        if not params:
            return base
        extra = TOOL_TITLE_PARAMS.get(name)
        if extra is None:
            return base
        try:
            text = str(extra(params)).strip()
        except Exception:
            text = ""
        return (base + " " + text) if text else base

    def _last_user_text(self) -> str:
        for event in reversed(self.store.events(self.session_id)):
            if event.type == "user/message":
                return str(event.data.get("text") or "")
        return ""

    def projection_preview(self) -> tuple[list[dict[str, str]], int]:
        """给 /context 与 --dump 用。全新进程里没有 last_retrieval,就现场检索一次。"""
        result = self.last_retrieval
        if result is None:
            result = retrieve(
                self.store, self._last_user_text(), now=self.settings.now(),
                session_id=self.session_id,
            )
        memory_text = self._memory_text(result)
        projection = self._build_projection(self.conversation_history(), memory_text, "")
        return projection.messages, projection.total_tokens

    def tools_tokens(self) -> int:
        """工具 schema 的 token 估算。每轮完全一样,所以缓存一次。

        ⚠️ 它由 API 的 tools 参数单独发出、不进 messages —— 早先因此**完全不在预算里**,
        52 个工具的 schema 估算值比整个投影还大。现在单列上报(见 docs/决策记录.md D95)。
        """
        cached = getattr(self, "_tools_tokens", None)
        if cached is None:
            cached = config.estimate_tokens(
                json.dumps(self.tool_catalog(), ensure_ascii=False, default=str)
            )
            self._tools_tokens = cached
        return int(cached)

    def _checkpoint_text(self) -> str:
        """⭐ D142:压缩检查点 —— "前面还有几段被压成了摘要"。

        ⚠️ 只读摘要池的**元信息**(时间、编号、一行预览),**不取正文**:
           正文要按需检索(D10),这里放一份等于把整个摘要池搬进每轮请求。
        ⚠️ 读不到就当没有这一段:压缩本身仍然是对的,模型只是少知道一件事 ——
           没有理由为了一个提示把这一轮回答带崩。
        """
        try:
            summaries = self.store.all_summaries(self.session_id)
        except Exception:  # noqa: BLE001 - 检查点只是提示,读不到不该影响回答
            return ""
        return build_checkpoint_block(summaries)

    def _build_projection(
        self, history: list[dict[str, str]], memory_text: str, evidence_text: str
    ) -> Projection:
        runtime_text = build_runtime_context(
            self.settings, turn=self.turn_index, last_active=self.last_active,
            surface=self.surface, session_id=self.session_id,
        )
        # ⭐ 人设**每轮现读**(D117):人格页保存完,下一轮就是新的她 —— 与聊天页那个
        #    「联网」按钮同一条口径(库里没记录时才回落到代码里的出厂默认)。
        #    ⚠️ 代价只有一次:改人格会让这一轮的 KV 前缀缓存前缀失效。
        identity = IDENTITY
        persona = self.persona.state()
        persona_text = persona.full_text
        catalog = self.library.catalog_text()
        system_text = identity + "\n\n" + persona_text + "\n\n" + catalog
        projection = self.projector.build(
            system_text=system_text,
            runtime_text=runtime_text,
            memory_text=memory_text,
            history=history,
            evidence_text=evidence_text,
            tools_tokens=self.tools_tokens(),
            checkpoint_text=self._checkpoint_text(),
        )
        # ⭐ 轨迹(D118):把这一轮装配用的料记在旁边,供 /trace 那一页说清
        #    "系统提示里这三段各占多少"与"哪一段被截尾了"。裁剪发生在 projector 里,
        #    所以这里记的是**裁剪前**的估算值,由页面与投影里的实际值对比得出。
        #    ⚠️ 它只是登记,不改装配结果一个字节。
        self.last_context_detail = {
            "parts": [
                {"key": "identity", "name": "身份与边界(代码写死,优先级最高)",
                 "chars": len(identity), "tokens": config.estimate_tokens(identity)},
                {"key": "persona", "name": "人格(人格页保存的那半 + 代码固定的尾巴)",
                 "chars": len(persona_text), "tokens": config.estimate_tokens(persona_text)},
                {"key": "catalog", "name": "技能目录(常驻;技能正文按需加载)",
                 "chars": len(catalog), "tokens": config.estimate_tokens(catalog)},
                {"key": "runtime", "name": "运行时上下文(时间/时区/会话/轮次/入口)",
                 "chars": len(runtime_text), "tokens": config.estimate_tokens(runtime_text)},
            ],
            # 裁剪前的段大小:与 projection.segments 一比就知道谁被砍过
            "pre_clip": {
                "system": config.estimate_tokens(system_text),
                "runtime": config.estimate_tokens(runtime_text),
                "memory": config.estimate_tokens(memory_text),
                "evidence": config.estimate_tokens(evidence_text) if evidence_text else 0,
            },
            "persona_custom": bool(persona.custom),
            "persona_chars": int(persona.chars),
        }
        return projection

    def _record_trace(self, projection: Projection) -> None:
        """把这一轮**真正发出去的那份上下文**落一份轨迹(D118)。

        ⚠️ 四件事刻意如此:
          * 失败**只吞掉自己**(轨迹是观测,不是主流程)—— 写不进去照样答;
          * 落的是 projection.messages **逐字**,不是重算出来的近似;
          * 落盘地点在**这一份记忆的数据目录**下的 trace/context.jsonl(data/trace/…),
            **不进** memory.db / 事件日志(理由见 la/agent/context_trace.py 的模块注释);
          * 根目录取 **store 的归档目录的上一级**,而不是 config.DATA_DIR ——
            这样"库在临时目录、轨迹却写进真实 data/"从结构上就不可能(实测踩过一次:
            tests/test_m1 用临时库跑二十轮,真实 data/trace 里长出了 40 条测试对话)。
        """
        try:
            archive = getattr(self.store, "archive_dir", None)
            root = Path(archive).parent if archive else None
            record_context(
                projection,
                session_id=self.session_id,
                turn=self.turn_index,
                surface=self.surface,
                provider=self.settings.provider,
                model=self.settings.model,
                detail=self.last_context_detail,
                at=self.settings.now(),
                path=trace_path_for(root),
            )
        except Exception:  # noqa: BLE001 - 轨迹绝不能把这一轮对话带崩
            pass

    # -- 阶段编排 ---------------------------------------------------------
    def _should_escalate(self, text: str, qa: RetrievalResult, trange: TimeRange | None) -> bool:
        """什么情况下值得从摘要池(快)升到档案(慢)。

        ⚠️ 门开得太大是有代价的:任何带时间词的普通提问("今天天气怎么样""明天要早起")
        都会触发一次档案回查 + 4 个子代理,既烧钱又会往摘要池里灌垃圾条目。
        所以判据是**回忆性话术**(上次/之前/那次/说的/聊的/提过…),不是"句子里有时间词"。
        """
        recalled = any(cue in text for cue in MEMORY_CUES)
        if qa.status == STATUS_HIT:
            # 摘要已命中:只有用户点明了时间、且是回忆性提问时,才去原文核对细节
            return recalled and trange is not None
        return recalled

    def _should_spawn(self, pad: Scratchpad, trange: TimeRange | None) -> bool:
        """按时间回查仍然颗粒无收,才值得花钱派子代理。"""
        if pad.evidence:
            return False
        return trange is not None

    def _build_tasks(self, text: str, trange: TimeRange) -> list[SubAgentTask]:
        """把"上周三上午"这样一个点,扩成 4 个相邻时段并行翻 —— 用户记错时间是常态。"""
        end = trange.end
        start = end - timedelta(days=WIDEN_DAYS)
        span = (end - start) / 4
        tasks: list[SubAgentTask] = []
        for index in range(4):
            window_start = start + span * index
            window_end = start + span * (index + 1)
            tasks.append(
                SubAgentTask(
                    query=text,
                    start=window_start.isoformat(timespec="seconds"),
                    end=window_end.isoformat(timespec="seconds"),
                    # ⚠️ 必须绑定本会话(2026-10-18 审计):不传时 query_range
                    #    会翻**全库**档案,家庭版里别的成员的对话会进本会话证据。
                    session_id=self.session_id,
                    label=SUBTASK_LABEL + str(index + 1),
                )
            )
        return tasks

    def _evidence_from_tool(self, result: ToolResult, source: str) -> list[Evidence]:
        if not result.ok or not isinstance(result.data, dict):
            return []
        items: list[Evidence] = []
        for event in result.data.get("events") or []:
            items.append(
                Evidence(
                    source=source,
                    timestamp=str(event.get("time") or ""),
                    content=str(event.get("text") or ""),
                    seq=event.get("seq"),
                    locator=str(event.get("locator") or ""),
                    salience=0.7,
                )
            )
        return items

    def _evidence_from_reports(self, reports: list[SubAgentReport]) -> list[Evidence]:
        items: list[Evidence] = []
        for report in reports:
            for finding in report.findings:
                items.append(
                    Evidence(
                        source=SOURCE_SUB_AGENT,
                        timestamp=finding.timestamp,
                        content=finding.content,
                        seq=finding.seq,
                        session_id=finding.session_id,
                        salience=finding.salience,
                        locator=finding.locator,
                    )
                )
        return items

    async def run_phases(self, user_text: str) -> PhaseOutcome:
        """QA -> ARCH -> ADJUDICATE。每一步都被熔断矩阵看着。"""
        now = self.settings.now()
        breaker = CircuitBreaker(phase=PHASE_QA)
        self._active_breaker = breaker
        pad = Scratchpad()
        results: list[ToolResult] = []

        # ---- Phase QA:摘要池(快路径)—— 同样走工具管线,所以有门禁也有审计 ----
        self._step("phase:qa", "检索长期记忆")
        self._last_qa = None
        qa_result = await self.registry.execute(
            "query_summary_pool", {"query": user_text}, breaker=breaker, session_id=self.session_id
        )
        results.append(qa_result)
        qa = self._last_qa
        self._step_end(
            "phase:qa",
            "检索长期记忆",
            ok=qa_result.ok,
            detail=(
                "命中 " + str(len(qa.summaries)) + " 条摘要 / " + str(len(qa.facts)) + " 条永久事实"
                if qa_result.ok and qa is not None
                else str(qa_result.message or qa_result.error_code)
            ),
        )
        if qa is None:
            # 被门禁拒绝时不绕过门禁直接检索;但永久事实仍然注入(它们与"问什么"无关)
            qa = RetrievalResult(
                status=STATUS_MISS,
                # 与正常路径同一口径:相关事实优先 + 600 token 子预算。
                facts=self.store.facts_for_context(
                    user_text,
                    limit=config.HOT_SUMMARY_MAX_ITEMS,
                    session_id=self.session_id,
                ),
            )
            pad.rejected.append(
                {"tool": "query_summary_pool", "code": qa_result.error_code,
                 "detail": qa_result.message}
            )
        pad.add_summary_hits(qa.summaries)
        self.store.append(
            self.session_id,
            "tool/result",
            {
                "tool": "query_summary_pool",
                "ok": qa_result.ok,
                "phase": PHASE_QA,
                "hits": len(qa.summaries),
                "status": qa.status,
            },
        )
        # 检索轨迹:审计与调优的唯一依据(设计深化 §1.2),永不 surface
        self.store.append(
            self.session_id,
            "memory/retrieval",
            {
                "query": user_text,
                "anchors": qa.query_anchors,
                "terms": qa.terms,
                "hit_ids": qa.ids,
                "status": qa.status,
                "elapsed_ms": round(qa.elapsed_ms, 2),
            },
        )

        # ⚠️ 时间范围只是"附加信息":解析失败不该打断这一轮(2026-10-17 审计)。
        #    parse_range 里已经兜了非法日期,这里再兜一层 —— 它坏了也不许把
        #    整轮对话变成 error 帧,更不许留下"没有回复的 user 事件"。
        try:
            trange = parse_range(user_text, now)
        except Exception:  # noqa: BLE001 - 解析失败 = 没有时间范围,照常回答
            trange = None
        archaeology_ran = False
        reports: list[SubAgentReport] = []

        # ---- Phase ARCH:档案(慢路径)----
        if self._should_escalate(user_text, qa, trange):
            breaker.enter(PHASE_ARCH)
            self.store.append(
                self.session_id, "phase/transition", {"from": PHASE_QA, "to": PHASE_ARCH}
            )
            archaeology_ran = True

            if trange is not None:
                self._step("phase:arch", "按时间回查档案原文")
                tool_result = await self.registry.execute(
                    "query_timestamp_range",
                    {
                        "start": trange.start.isoformat(timespec="seconds"),
                        "end": trange.end.isoformat(timespec="seconds"),
                        "keywords": user_text,
                    },
                    breaker=breaker,
                    session_id=self.session_id,
                )
                results.append(tool_result)
                pad.extend(self._evidence_from_tool(tool_result, SOURCE_TIMESTAMP_RANGE))
                self.store.append(
                    self.session_id,
                    "tool/result",
                    {"tool": "query_timestamp_range", "ok": tool_result.ok,
                     "error_code": tool_result.error_code, "count": len(pad)},
                )
                self._step_end(
                    "phase:arch",
                    "按时间回查档案原文",
                    ok=tool_result.ok and bool(pad),
                    detail=(
                        "取到 " + str(len(pad)) + " 条原文"
                        if pad else "这个时间窗里没有找到原文"
                    ),
                )

            if self._should_spawn(pad, trange) and trange is not None:
                tasks = self._build_tasks(user_text, trange)
                quota = breaker.allow_subagents(len(tasks))
                if quota.allowed:
                    self._step("phase:subagents", "并行挖掘 " + str(len(tasks)) + " 个时段")
                    tool_result = await self.registry.execute(
                        "spawn_sub_agents",
                        {"tasks": [task.as_dict() for task in tasks], "max_agents": len(tasks)},
                        breaker=breaker,
                        session_id=self.session_id,
                    )
                    results.append(tool_result)
                    if tool_result.ok and isinstance(tool_result.data, dict):
                        for raw in tool_result.data.get("reports") or []:
                            reports.append(_report_from_dict(raw))
                        pad.extend(self._evidence_from_reports(reports))
                    self.store.append(
                        self.session_id,
                        "tool/result",
                        {"tool": "spawn_sub_agents", "ok": tool_result.ok,
                         "error_code": tool_result.error_code, "agents": len(reports)},
                    )
                    self._step_end(
                        "phase:subagents",
                        "并行挖掘 " + str(len(reports)) + " 份报告",
                        ok=tool_result.ok,
                        detail=(
                            "共 " + str(sum(len(r.findings) for r in reports)) + " 条发现"
                            if tool_result.ok
                            else str(tool_result.message or tool_result.error_code)
                        ),
                    )
                else:
                    pad.rejected.append({"tool": "spawn_sub_agents", "code": quota.code,
                                         "detail": quota.detail})

        # ---- Phase ADJUDICATE:纯函数裁决,0 工具 ----
        self._step("phase:adjudicate", "裁决证据(纯代码,0 次模型调用)")
        breaker.enter(PHASE_ADJUDICATE)
        verdict = adjudicate(
            pad,
            terms=qa.terms,
            budget_tokens=config.BUDGET_EVIDENCE,
            budget_exhausted=archaeology_ran and not pad.evidence,
            # 设计深化 §2.2/§4.3:子代理报 ambiguous = 有多组候选,该问用户。
            # 判据由代码给(status 来自子代理协议),模型负责把问题问出来。
            ambiguous=any(report.status == STATUS_AMBIGUOUS for report in reports),
            now=now,
        )
        self.store.append(
            self.session_id,
            "phase/transition",
            {"to": PHASE_ADJUDICATE, "breaker": breaker.as_dict(), "verdict": verdict.as_dict()},
        )
        self._step_end(
            "phase:adjudicate",
            "证据已裁决",
            detail=verdict.basis + " · 证据 " + str(len(pad)) + " 条",
        )
        self.emit(
            "phases",
            phase=breaker.phase,
            steps=breaker.total_steps,
            evidence=len(pad),
            basis=verdict.basis,
        )
        return PhaseOutcome(
            breaker=breaker,
            pad=pad,
            qa=qa,
            verdict=verdict,
            time_range=trange,
            tool_results=results,
            archaeology_ran=archaeology_ran,
            reports=reports,
        )

    # -- 诚实性护栏:说了"已经写好",就得真有回执(D113) ---------------------
    #: 两本回执账的稳定短名。两张表的 id 各自从 1 自增 —— 界面上必须带前缀,
    #: 否则 "撤销 #5" 会撤到另一个域的写入(2026-10-18 审计修)。
    RECEIPT_SOURCES = (("a", "receipts"), ("m", "memory_receipts"))

    def _receipt_logs(self) -> list[tuple[str, Any]]:
        return [(key, getattr(self, attr, None)) for key, attr in self.RECEIPT_SOURCES]

    def receipt_marks(self) -> dict[str, int]:
        """两份回执账的**水位线**(各自一条序列,不能混用同一个数)。

        ⚠️ 这个坑踩过两次:桥接层与 CLI 都拿 assistant.db 的 last_id 去
           memory.db 里 since(),于是事项层(待办/提醒)的回执永远"这一轮没写"。
           现在水位线与取货都只走这一对方法。
        """
        marks: dict[str, int] = {}
        for key, log in self._receipt_logs():
            if log is None:
                continue
            try:
                # ⚠️ 必须按本会话取水位线(2026-10-18 审计):不传 session 时,
                #    桥接层会把别的成员/别的入口刚写的回执算成“这一轮写的”。
                marks[key] = int(log.last_id(session_id=self.session_id) or 0)
            except Exception:  # noqa: BLE001 - 取不到水位线不该让这一轮失败
                marks[key] = 0
        return marks

    def receipts_since(self, marks: dict[str, int] | None) -> list[Any]:
        """取两本回执账里**各自**在水位线之后的新回执。"""
        found: list[Any] = []
        for key, log in self._receipt_logs():
            if log is None:
                continue
            try:
                found.extend(log.since(
                    int((marks or {}).get(key, 0)), session_id=self.session_id,
                ))
            except Exception:  # noqa: BLE001 - 取不到就少一条,不该拖垮这一轮
                continue
        return sorted(
            found,
            key=lambda item: (str(getattr(item, "ts", "")), int(getattr(item, "id", 0))),
        )

    def _receipt_marks(self) -> list[tuple[Any, int]]:
        marks = self.receipt_marks()
        return [
            (log, marks[key])
            for key, log in self._receipt_logs()
            if log is not None and key in marks
        ]

    def _wrote_since(self, marks: list[tuple[Any, int]]) -> bool:
        for log, before in marks:
            try:
                if int(log.last_id(session_id=self.session_id) or 0) > before:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _needs_write_repair(
        self, result: ChatResult, breaker: CircuitBreaker, marks: list[tuple[Any, int]]
    ) -> bool:
        """是不是"说了已办好、其实一次写入都没发生"—— 需要补调工具。

        ⚠️ 与 _guard_write_claims 共用同一套事实判据,但这里**先补救**:
          真机(2026-10-22)模型在思考里已经想好"建一条待办",正文也写了"已建",
          却把 tool_calls 留空。只贴一句更正,用户还得再说一遍;补一轮工具调用
          能把这件事真正做完 —— 护栏的职责是"不让它冒充事实",不是"替它写检讨"。
        """
        text = str(result.text or "")
        if not claims_write(text):
            return False
        if bool(getattr(breaker, "answer_write_calls", 0)) or bool(
            getattr(breaker, "task_calls", 0)
        ):
            return False
        return not self._wrote_since(marks)

    async def _repair_write_claim(
        self,
        result: ChatResult,
        breaker: CircuitBreaker,
        projection: Projection,
    ) -> ChatResult:
        """补一轮:把"只说不做"的那件事真正调用工具做完。

        ⚠️ 上限两轮模型往返、不把补调正文重新流给前端(on_delta=None):
           网页已经显示过第一段,再流一遍会像"答了两次";最终 result.text
           仍然会走最后一帧整体替换。微信本来只取最终正文,不受影响。
        """
        before = int(getattr(breaker, "answer_write_calls", 0)) + int(
            getattr(breaker, "task_calls", 0)
        )
        working: list[dict[str, Any]] = list(projection.messages)
        working.append(result.assistant_message(include_reasoning=True))
        working.append({"role": "user", "content": REPAIR_WRITE_PROMPT})
        catalog = self.tool_catalog()
        final = result
        self._step("guard:repair", "上一句只报了结果、没有调用工具;补一轮真正写入")
        for step in range(2):
            repaired = await self.client.chat(
                working, tools=catalog or None, on_delta=None, on_reasoning=None,
            )
            result.usage = result.usage.merge(repaired.usage)
            try:
                self.store.append(
                    self.session_id,
                    "llm/usage",
                    {
                        "step": "repair-" + str(step),
                        "tools_sent": True,
                        "prompt_tokens": int(repaired.usage.prompt_tokens or 0),
                        "completion_tokens": int(repaired.usage.completion_tokens or 0),
                        "total_tokens": int(repaired.usage.total_tokens or 0),
                        "cache_hit_tokens": int(repaired.usage.cache_hit_tokens or 0),
                        "cache_miss_tokens": int(repaired.usage.cache_miss_tokens or 0),
                        "reasoning_tokens": int(repaired.usage.reasoning_tokens or 0),
                        "finish_reason": str(repaired.finish_reason or ""),
                    },
                )
            except Exception:  # noqa: BLE001 - 观测落不进去不影响补救本身
                pass
            final = repaired
            if not repaired.wants_tools:
                break
            working.append(repaired.assistant_message(include_reasoning=True))
            for call in repaired.tool_calls:
                tool_result = await self.registry.execute(
                    call.name,
                    call.parsed_arguments(),
                    breaker=breaker,
                    session_id=self.session_id,
                )
                payload: Any = (
                    tool_result.data
                    if tool_result.ok
                    else {"error": tool_result.error_code, "message": tool_result.message}
                )
                encoded = json.dumps(payload, ensure_ascii=False, default=str)
                if len(encoded) > 4000:
                    encoded = json.dumps(
                        {"truncated": True,
                         "note": "工具结果超过 4000 字符,下面是截断预览",
                         "preview": encoded[:3600]},
                        ensure_ascii=False,
                    )
                working.append(
                    {"role": "tool", "tool_call_id": call.id, "content": encoded}
                )
                try:
                    self.store.append(
                        self.session_id, "tool/result",
                        {"tool": call.name, "ok": tool_result.ok,
                         "error_code": tool_result.error_code, "via": "repair"},
                    )
                except Exception:  # noqa: BLE001 - 留痕失败不该打断补救
                    pass
                self.emit("tool", tool=call.name, ok=tool_result.ok)
                if call.name not in INTERNAL_TOOLS:
                    self._step_end(
                        "tool:" + call.name,
                        self._tool_title(call.name) or call.name,
                        ok=tool_result.ok,
                        detail=(
                            "返回 " + str(len(encoded)) + " 字"
                            if tool_result.ok
                            else str(tool_result.message or tool_result.error_code)
                        ),
                    )
        after = int(getattr(breaker, "answer_write_calls", 0)) + int(
            getattr(breaker, "task_calls", 0)
        )
        if after <= before:
            # 补调也没写:把原来的正文还回去,让 _guard_write_claims 去更正
            self._step_end("guard:repair", "补调后仍未写入", ok=False)
            return result
        self._step_end("guard:repair", "已补调写入工具", ok=True)
        if str(getattr(final, "text", "") or "").strip():
            final.usage = result.usage
            return final
        return replace(result, usage=result.usage)

    def _guard_write_claims(
        self, result: ChatResult, breaker: CircuitBreaker, marks: list[tuple[Any, int]]
    ) -> ChatResult:
        """它说"已经办好了",但这一轮其实什么都没写 —— **当场更正**。

        判据是**事实**,不是文风:这一轮一条回执都没有(两份回执账都没涨),
        而且写入类工具一次都没调(counter 由 phases 的断路器记着)。
        两条都成立时,那句话只能是编的 —— 于是把更正**接在同一句话后面**:
        不拦住它说话(那是 D101 的纪律),只是不许它冒充事实。
        ⭐ 2026-10-22(D155):在更正之前会先走一次 _repair_write_claim 补调工具;
        这里处理的是"补调也没写"的剩余情况。

        ⚠️ 更正必须落在 **result.text** 上而不是只发一条 notice:网页端最后那一帧
           (_sse "html")、微信那条正文、以及写进事件日志的历史,读的都是它。
        """
        text = str(result.text or "")
        if not claims_write(text):
            return result
        wrote = bool(getattr(breaker, "answer_write_calls", 0)) or bool(
            getattr(breaker, "task_calls", 0)
        )
        if wrote or self._wrote_since(marks):
            return result
        correction = (
            "\n\n⚠️ 更正:上面那句「已经办好」不作数 —— 这一轮我**一次写入工具都没调用**,"
            "库里没有新增、也没有改动。要真的记下来,请再跟我说一遍"
            "(也可以直接在网页「日历 → 提醒 / 待办」里手动加)。"
        )
        try:
            self.store.append(
                self.session_id,
                "turn/write-claim-without-write",
                {"text": text[:200]},
            )
        except Exception:  # noqa: BLE001 - 记不上事件也不能让更正丢掉
            pass
        self._step("guard:write-claim", "⚠️ 更正:这一轮没有真正写入", running=False)
        return replace(result, text=text + correction)

    def _abort_turn(self, exc: BaseException) -> None:
        """失败/被中止的一轮也要**收尾**。

        ⚠️ assistant 正文留空:不替模型编一句话,只把"这一轮没有回答"记下来,
           让下一轮的历史仍然 user/assistant 成对(否则模型会去回答那条旧问题)。
        """
        if not self._turn_open:
            return
        self._turn_open = False
        reason = type(exc).__name__
        if self.wake_mode:
            # 唤醒失败不写 assistant/message:那会在聊天里留下一条空回复,
            # 而这次失败只属于 /wake 的运行记录。
            try:
                self.store.append(
                    self.session_id, "wake/error",
                    {"index": self.turn_index, "error": reason},
                )
                # 与普通失败同一口径:turn/start 必须有 turn/end 收尾。
                self.store.append(
                    self.session_id, "turn/end",
                    {"index": self.turn_index, "error": reason},
                )
            except Exception:  # noqa: BLE001 - 收尾失败不能盖住原始异常
                pass
            return
        try:
            self.store.append(
                self.session_id, "assistant/message",
                {"text": "", "tool_calls": [], "reasoning": "", "aborted": reason},
            )
            self.store.append(
                self.session_id, "turn/end",
                {"index": self.turn_index, "error": reason},
            )
        except Exception:  # noqa: BLE001 - 收尾失败不能盖住原始异常
            pass

    # -- 一轮对话 ---------------------------------------------------------
    async def respond(
        self,
        user_text: str,
        *,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        wake: bool = False,
    ) -> ChatResult:
        # ⭐ P2:同一会话**跨进程**串行。Web 与微信是两个进程,各自有自己的
        #    MemorySession;只靠 __init__ 里那个 asyncio.Lock 管不住对方。
        #    锁覆盖整轮(投影/工具/回执/压缩),因为这些东西都读同一份事件。
        #    锁目录跟着记忆库的 archive_dir 走:测试用临时目录就不会污染真实 data/。
        archive_dir = Path(getattr(self.store, "archive_dir", None) or config.ARCHIVE_DIR)
        # 唤醒**不跟用户抢**同一把锁:等 5 秒拿不到就放弃(记 busy、十分钟后再试),
        # 而不是让正在聊天的人卡上几分钟。
        async with turn_lock(
            self.session_id,
            timeout=5.0 if wake else 600.0,
            directory=archive_dir / ".locks",
        ):
            try:
                return await self._respond_locked(
                    user_text, on_delta=on_delta, on_reasoning=on_reasoning, wake=wake
                )
            except BaseException as exc:  # noqa: BLE001 - 收尾之后原样抛出
                # ⚠️ 失败的一轮必须收尾(2026-10-18 审计修):以前模型抛错后事件日志里
                #    只有 user/message,没有 assistant/message、也没有 turn/end —— 下一轮
                #    的历史变成 user,user,assistant,模型会去回答那条"没人回"的旧问题。
                self._abort_turn(exc)
                raise

    async def run_wake(
        self,
        instruction: str,
        *,
        allow_writes: bool = True,
        dry_run: bool = False,
    ) -> ChatResult:
        """跑一次定时唤醒。返回的一轮结果;报告在 `self.wake_report`。"""
        self.wake_allow_writes = bool(allow_writes) and not bool(dry_run)
        self.wake_dry_run = bool(dry_run)
        self.wake_report = None
        self.wake_receipts = []
        self.wake_write_calls = 0
        self.wake_instruction = str(instruction or "").strip()
        self.wake_receipt_marks = (
            self.receipts.last_id(session_id=self.session_id) if self.receipts else 0,
            self.memory_receipts.last_id(session_id=self.session_id)
            if self.memory_receipts else 0,
        )
        header = WAKE_INSTRUCTION_HEADER
        if dry_run:
            header += (
                "【试跑】这次不会发给用户,而且**所有写入都会被拒绝**;"
                "只检查,把你会做的事写进 wake_report。\n\n"
            )
        return await self.respond(header + self.wake_instruction, wake=True)

    async def _respond_locked(
        self,
        user_text: str,
        *,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        wake: bool = False,
    ) -> ChatResult:
        self.turn_index += 1
        self.current_turn = self.turn_index
        # 回执带上"第几轮":前端只关心这一轮写了什么
        for log in (self.receipts, self.memory_receipts):
            if log is not None:
                log.turn = self.turn_index
        # 这一轮开始前的回执水位线:护栏靠它回答"这一轮到底写进去了什么"(D113)
        receipt_marks = self._receipt_marks()
        self.store.append(self.session_id, "turn/start", {"index": self.turn_index})
        # ⚠️ 唤醒的指令**不落 user/message**:落了之后,用户第二天打开聊天就会看到
        #    "19:00 你说:核对本周账本"。它只在本次请求里作为当前 user 输入存在;
        #    如果这次真的要报告/真写了数据,结束时再把 user+assistant **成对**落库。
        if not wake:
            self.store.append(self.session_id, "user/message", {"text": user_text})
            self._capture_notes(user_text)
        self._turn_open = True

        outcome = await self.run_phases(user_text)
        self.last_outcome = outcome
        self.last_retrieval = outcome.qa

        self._step(
            "phase:project",
            "装配上下文(记忆 " + str(len(outcome.qa.summaries)) + " 条摘要)",
            running=False,
        )
        memory_text = self._memory_text(outcome.qa)
        # ⭐ 笔记的"自动召回"(P2):命中的**标题**拼进记忆块,正文绝不进上下文。
        #    ⚠️ 它绕过工具管线(没有门禁/预算),所以必须在事件里留痕 ——
        #       否则以后查不出"它当时为什么没看见"。
        memory_text += self._notes_hint(user_text)
        evidence_text = (
            render_evidence_block(outcome.verdict, budget_tokens=config.BUDGET_EVIDENCE)
            if outcome.archaeology_ran
            else ""
        )
        # 唤醒的指令没落库,所以这里手工把它补成"本轮 user 输入";
        # 普通聊天照旧从事件日志读(它已经落过 user/message)。
        history = self.conversation_history()
        if wake:
            history = history + [{"role": "user", "content": user_text}]
        projection = self._build_projection(history, memory_text, evidence_text)
        self.last_projection = projection
        self.emit(
            "projection",
            segments=projection.segments,
            total=projection.total_tokens,
            tools=projection.tools_tokens,
            capped=projection.capped,
        )
        # ⭐ 轨迹(D118):**发出去之前**落一份 —— 模型调失败、超时、被中止时也要有,
        #    因为"它当时看到了什么"恰恰是排查失败时最需要的东西。
        self._record_trace(projection)

        result = await self._converse(
            projection.messages, outcome.breaker,
            on_delta=on_delta, on_reasoning=on_reasoning,
        )
        # ⭐ 2026-10-22:只报账、不调用工具时**先补一轮真正写入**,再对账。
        #    真机(微信)模型在思考里已经想好"建待办",正文也写了"已建",
        #    却把 tool_calls 留空 —— 只贴更正等于让用户再说一遍。
        # 唤醒是无人值守的:再补一轮"写入修复"既多花钱,又可能在没人看着时多写一笔。
        if not wake and self._needs_write_repair(result, outcome.breaker, receipt_marks):
            try:
                result = await self._repair_write_claim(result, outcome.breaker, projection)
            except Exception as exc:  # noqa: BLE001 - 补救失败不能盖住原始回答
                self.emit("write_repair_failed", error=type(exc).__name__)
        # ⭐ 说话之前先对一遍账:它有没有说"已经写好"却什么都没写(见 _guard_write_claims)
        result = self._guard_write_claims(result, outcome.breaker, receipt_marks)
        self._step("phase:persist", "写入事件日志与记忆", running=False)

        if wake:
            # 唤醒的落库规则:**写了数据,或模型主动报告了**,才成对落 user+assistant。
            # 静默的一轮一个字都不落 —— 否则历史里会留下"有 user 没 assistant"的半轮,
            # 下一轮模型会去回答那条旧指令。
            report_text = str((self.wake_report or {}).get("text") or "")
            if report_text or self.wake_write_calls:
                self.store.append(
                    self.session_id,
                    "user/message",
                    {"text": self.wake_instruction or user_text, "via": "wake"},
                )
                self.store.append(
                    self.session_id,
                    "assistant/message",
                    {
                        "text": report_text or result.text,
                        "tool_calls": [call.name for call in result.tool_calls],
                        "reasoning": result.reasoning,
                        "via": "wake",
                    },
                )
        else:
            self.store.append(
                self.session_id,
                "assistant/message",
                {
                    "text": result.text,
                    "tool_calls": [call.name for call in result.tool_calls],
                    # 思维链必须持久化:下一轮的请求同样带 tools,需要完整回传
                    "reasoning": result.reasoning,
                },
            )
        self._turn_open = False
        self.total_usage = self.total_usage.merge(result.usage)
        self.last_active = config.format_local(self.settings.now())

        self._write_back_heat(outcome.qa)
        # ⚠️ 回复已经落库了:**收尾失败不该让这一轮变成 error**(2026-10-18 审计修)。
        #    压缩/事实裁决/巩固都可能调模型,它们失败只该记一条事件 ——
        #    否则用户看到"出错了",而回答其实已经写进历史,且 turn/end 会缺。
        try:
            if wake:
                # 无人值守:不跑事实准入(它会再调一次模型,且可能在没人确认时写记忆);
                # 领域 -> 记忆的发件箱照常搬(账本事件要能被检索),压缩照常做。
                self.relay_outbox()
                await self._maybe_compact()
            else:
                await self._after_reply(outcome)
        except Exception as exc:  # noqa: BLE001 - 回复之后的维护失败不该回滚回答
            self.emit("after_reply_failed", error=type(exc).__name__)

        self.store.append(
            self.session_id,
            "turn/end",
            {"index": self.turn_index, "usage": result.usage.as_dict()},
        )
        self.emit("usage", **result.usage.as_dict())
        return result

    async def run_fact_gate(self) -> Any:
        """把这一轮攒下的**事实候选**过一遍准入闸(回复已发送之后)。

        ⚠️ 放在这里而不是回答之前,有两个理由:
          ① 它要调一次模型;放在回答链路上就是给每一轮加一次串行延迟;
          ② 事实"晚一轮生效"是可接受的,而"没核过就进上下文"不可接受 ——
             与巩固回填(§1.5)同一条取舍。
        """
        report = await self.fact_gate.process_pending()
        if report.considered:
            self.emit("fact_gate", summary=report.summary(), **{
                "admitted": report.admitted, "duplicate": report.duplicate,
                "rejected": report.rejected, "review": report.review,
            })
        return report

    async def _after_reply(self, outcome: PhaseOutcome) -> None:
        """回复已发送之后才做的事:事实准入 + 巩固回填 + 压缩。都不阻塞对话。"""
        # 领域 -> 记忆:把这一轮产生的账目事件搬进归档(失败不影响回复)
        self.relay_outbox()

        # 事实准入:候选 -> 裁决 -> 入库/合并/拒绝/待审(见 memory/fact_gate.py)
        try:
            await self.run_fact_gate()
        except Exception as exc:  # noqa: BLE001 - 准入失败绝不能把这一轮回答带崩
            self.emit("fact_gate_failed", error=type(exc).__name__)

        if outcome.archaeology_ran and outcome.pad.evidence:
            evidence = outcome.pad.sorted_evidence()
            target_session = self.session_id
            start_seq: int | None = None
            end_seq: int | None = None

            # 优先并进证据真正所属的那个桶(设计深化 §1.5:写回同一行,不新增行)
            for item in evidence:
                if item.seq is None:
                    continue
                candidate = self.store.find_summary_for_seq(
                    item.session_id or self.session_id, item.seq, item.seq
                )
                if candidate is not None:
                    target_session = candidate.session_id
                    start_seq, end_seq = candidate.start_seq, candidate.end_seq
                    break

            if start_seq is None:
                seqs = [item.seq for item in evidence if item.seq is not None]
                if not seqs:
                    await self._maybe_compact()
                    return
                target_session = evidence[0].session_id or self.session_id
                start_seq, end_seq = min(seqs), max(seqs)

            consolidation = await consolidate(
                self.store,
                session_id=target_session,
                start_seq=start_seq,
                end_seq=end_seq,
                rounds_after_reply=self.turn_index,
                evidence=evidence,
                start_time=evidence[0].timestamp,
                end_time=evidence[-1].timestamp,
            )
            if consolidation.applied and consolidation.summary_id:
                # 设计深化 §1.4:考古成功 -> base_strength += 1(永久,防归零)。
                # 幂等:只有"首次回填"(applied)才加,重复回填不加。
                self.store.bump_base_strength(
                    [consolidation.summary_id], BASE_STRENGTH_ON_ARCHAEOLOGY
                )
        await self._maybe_compact()

    # -- 记忆写入 ---------------------------------------------------------
    def _capture_notes(self, user_text: str) -> int:
        """规则识别出的"显式记忆" -> **候选队列**(不再直接写 facts)。

        ⚠️ 2026-10-13 改口径:以前这里是 `store.add_fact(...)` —— 规则匹配到什么就
           永久记住什么。实测后果(11 条生效事实里 5 条有问题):
             "我不吃香菜"  -> "用户比较喜欢 吃香菜"        (极性写反)
             "我喜欢吃什么东西" -> "用户喜欢 吃什么东西"   (疑问句入库)
             一句话被截在半个字上 / 一个未来动作被记成永久事实。
           现在规则只**提议**,入库由 FactGate 裁决(先结构检查、再模型判关系)。
        """
        notes = detect_notes(user_text)
        proposed = 0
        for note in notes:
            self.store.append(
                self.session_id,
                "memory/note",
                {"content": note.content, "fact_type": note.fact_type, "explicit": note.explicit},
            )
            # ⭐ 2026-10-22(D155):有提问通道(微信/网页/终端)时,规则只留痕、**不写候选**。
            #    否则用户说"记住:我不吃香菜"会同时走两条路:规则候选在回复后自动入库,
            #    而 memory_note 的确认还在等回答 —— 用户点"不记"也拦不住规则那条。
            #    现在只留一条路:模型调 memory_note(它会先问);没调就不写。
            #    无提问通道的批处理/测试仍走规则候选,行为不变。
            if self.asker is None and self.fact_gate.propose(
                note.content,
                source=SOURCE_RULE,
                fact_type=note.fact_type,
                keywords=note.keywords,
            ):
                proposed += 1
        if notes:
            self.emit("notes", count=len(notes), proposed=proposed)
        return len(notes)

    def _write_back_heat(self, retrieval: RetrievalResult) -> None:
        if not retrieval.ids:
            return
        amount = VISITS_USER_INITIATED if retrieval.query_anchors else VISITS_PASSIVE
        self.store.bump_visits(retrieval.ids, amount)
        # ⚠️ 只刷本会话:家庭版共库,别人的桶没必要每轮重算;分母也按会话取。
        self.store.refresh_heat_cache(self.settings.now(), session_id=self.session_id)

    # -- 压缩 -------------------------------------------------------------
    def _uncompacted_events(self) -> list[Any]:
        """还没被压过的 surface 事件(投影里模型真正看得见的那批)。"""
        ranges = self.store.compacted_ranges(self.session_id)
        last_end = max((end for _, end in ranges), default=0)
        return [e for e in self.store.events(self.session_id, last_end + 1) if e.surface]

    def _conversation_tokens(self, event: Any) -> int:
        """这条事件在 conversation_history 里占多少 token。

        ⚠️ **不能拿 json.dumps(event.data) 代替**:tool/result 的 JSON 很大,但它
          根本不进对话历史;按它累计会让压缩提前很多就停住 —— 2026-11-21 真机
          实测 41,759 只压到 31,225,原因就是这个(区间 3–1002 的 JSON 有 56,727
          token,而其中真正的对话文字只有 9,662)。这里的口径必须与
          config.estimate_messages_tokens(conversation_history()) 逐项一致。
        """
        if event.type not in ("user/message", "assistant/message"):
            return 0
        data = event.data or {}
        total = config.estimate_tokens(str(data.get("text") or "")) + 8
        # ⚠️ estimate_messages_tokens 对**每条消息**都加一次
        #    estimate_tokens(reasoning_content or "") —— 空串也算 4 token。
        #    少算这 4,811 条消息就差 3,244,选区会多压一截(2026-11-21 实测)。
        if config.REPLAY_HISTORY_REASONING:
            total += config.estimate_tokens(str(data.get("reasoning") or ""))
        else:
            total += config.estimate_tokens("")
        return total

    def _pick_range(
        self, events: list[Any], total: int, target: int
    ) -> tuple[int, int] | None:
        """从已经扫好的事件里切一段:保留最近 6 条,其余按"压掉多少**对话文字**"取最旧的。

        抽出来只为一件事:**手动压缩的状态展示与真正的选区用同一份算法**
        (否则页面说"可压 20 条",真压时按另一套算 —— 那就是两份真相)。
        """
        if len(events) <= RECENT_EVENTS_KEPT:
            return None
        candidates = events[:-RECENT_EVENTS_KEPT]
        to_compact = max(total - target, 0)
        if to_compact <= 0:
            return None
        used = 0
        end_index: int | None = None
        for index, event in enumerate(candidates):
            # ⚠️ 只累计"会进投影的对话文字" —— tool/result 再大也不算。
            used += self._conversation_tokens(event)
            # ⚠️ D184:问答转录的 assistant 提问不是**回合边界** —— 区间若正好切在
            #    提问上,会把下面的 user 回答留在外面,摘要就变成"问了没答"。
            #    只在真正的 assistant/message(最终答复)上收口。
            if event.type == "assistant/message" and event.data.get("via") != "ask":
                end_index = index
            if used >= to_compact:
                break
        if end_index is None or end_index < 1:
            return None
        return candidates[0].seq, candidates[end_index].seq

    def pick_compaction_range(self) -> tuple[int, int] | None:
        """决定这次压掉哪一段(返回 seq 区间),让**留下来的历史 = 目标保留量**。

        ⭐ D134(用户 2026-10-16 拍板):目标是"历史压缩到当前的 20%",所以这里算的是
        **该压掉多少**:`该压的 = 当前整段历史 − 目标保留量`。

        ⚠️ 旧口径是反的:把"保留多少"当成"这次压掉多少",于是压完还剩 80%(实测踩到:
        20 万的历史压完只剩 15.2 万,而目标是 3.9 万)。这一条特别容易写反,
        由 `tests/test_m1.py::test_compaction_triggers_at_the_configured_number` 钉住。
        """
        events = self._uncompacted_events()
        total = config.estimate_messages_tokens(self.conversation_history())
        return self._pick_range(events, total, self.compaction_retain_budget())

    def compaction_status(self) -> dict[str, Any]:
        """给「手动压缩」用的只读状态:多少事、多少 token、压完留多少、能不能压。

        ⚠️ "能不能压"必须调 `_pick_range` 而不是只看事件条数 —— 和历史太短、
           没有完整助手边界、已经压到保留线一样,都算"没有可压区间"。
        """
        events = self._uncompacted_events()
        total = config.estimate_messages_tokens(self.conversation_history())
        target = self.compaction_retain_budget()
        return {
            "events": len(events),
            "messages": sum(
                1 for e in events if e.type in ("user/message", "assistant/message")
            ),
            "history_tokens": total,
            "retain_tokens": target,
            "recent_kept": RECENT_EVENTS_KEPT,
            "compactable": self._pick_range(events, total, target) is not None,
        }

    def compaction_retain_budget(self) -> int:
        """⭐ D134:压缩后要保留多少 —— **当前历史上下文的 20%**(用户拍板)。

        ⚠️ 口径是"**当前事件日志里那整段历史**",不是对话段预算、也不是投影里剩下的那些:
           * 旧口径是 `BUDGET_CONVERSATION × 0.5` —— 说的是"预算的一半",与当时真有
             12 万还是 3 万无关;
           * 投影里的 `retained_tokens` 也不行:它已经被对话段预算**裁过**(实测踩到:
             历史 12.8 万时投影里只剩 12 万,拿它乘 20% 会算出 2.4 万,而正确的是 2.56 万)。
        复现:`tests/test_m1.py::test_compaction_triggers_at_the_configured_number`。
        """
        actual = config.estimate_messages_tokens(self.conversation_history())
        if actual <= 0:
            actual = int(getattr(self.last_projection, "retained_tokens", 0) or 0)
        return max(1, int(actual * config.BUDGET_COMPACT_RETAIN_RATIO))

    async def compact_once(self, *, via: str = "auto") -> int | None:
        """压一次最旧的未压缩区间。`via` 只用于留痕:auto / manual / cli。

        ⭐ 手动入口与自动压缩**共用这一条**;区别只在调用方是否先确认。
        """
        before_tokens = config.estimate_messages_tokens(self.conversation_history())
        self.last_compaction = {
            "status": "nothing_to_compact",
            "via": via,
            "before_tokens": before_tokens,
        }
        picked = self.pick_compaction_range()
        if picked is None:
            return None
        start_seq, end_seq = picked
        range_events = self.store.events(self.session_id, start_seq, end_seq)
        events = [e for e in range_events if e.type in COMPACTION_EVENT_TYPES]
        if not events:
            self.last_compaction.update(
                status="nothing_to_compact", reason="empty_range",
                start_seq=start_seq, end_seq=end_seq,
            )
            return None
        # ⭐ 2026-11-07:标签从**整个区间**推(含 tool/result 与 ledger./health./study. 事件)——
        #    只看进摘要的那三类推不出领域,这正是 tags 以前永远是 [] 的另一半原因。
        tags = tags_from_events(range_events)
        self.store.append(
            self.session_id,
            "compaction/start",
            {"bucket": {"start_seq": start_seq, "end_seq": end_seq}, "via": via},
        )
        # 稳定性校验(设计深化 §1.3-③,照抄 DSH 的 assertStable):
        # 摘要生成期间这个会话若又被追加了事件,说明区间还在长大 —— 放弃这一轮,
        # 宁可不压,也不要压出一个"少了后半段"的摘要。
        observed_last_seq = self.store.last_seq(self.session_id)
        draft = await self.summarizer.summarize(
            events, start_time=events[0].time, end_time=events[-1].time
        )
        if self.store.last_seq(self.session_id) != observed_last_seq:
            self.store.append(
                self.session_id,
                "compaction/skipped",
                {
                    "bucket": {"start_seq": start_seq, "end_seq": end_seq},
                    "reason": "unstable",
                    "observed_last_seq": observed_last_seq,
                    "via": via,
                },
            )
            self.last_compaction.update(
                status="skipped", reason="unstable",
                start_seq=start_seq, end_seq=end_seq,
            )
            self.emit("compaction_skipped", reason="unstable")
            return None
        summary_id = self.store.upsert_summary(
            session_id=self.session_id,
            start_seq=start_seq,
            end_seq=end_seq,
            start_time=events[0].time,
            end_time=events[-1].time,
            text=draft.text,
            anchors=draft.anchors,
            tags=tags,
            # 三档衰减(设计深化 §1.4):identity/decision 的桶 -> permanent,
            # 无笔记无锚点的闲聊 -> temporary,其余 periodic。
            category=classify_bucket(events),
        )
        self.store.append(
            self.session_id,
            "compaction/summary",
            {
                "summary_id": summary_id,
                "bucket": {"start_seq": start_seq, "end_seq": end_seq},
                "source": draft.source,
                "valid": draft.valid,
                "problems": list(draft.problems),
                "tags": tags,
                "via": via,
            },
        )
        self.last_compaction.update(
            status="compacted", summary_id=summary_id,
            after_tokens=config.estimate_messages_tokens(self.conversation_history()),
            start_seq=start_seq, end_seq=end_seq,
        )
        # 手动压缩发生在回合中间时,回合末尾的 _maybe_compact 还会拿**压缩前**的投影
        # 再判一次(可能再压一次)。清掉它,让"刚压过"这件事对后续判断生效。
        self.last_projection = None
        self.emit("compaction", summary_id=summary_id, start_seq=start_seq, end_seq=end_seq)
        return summary_id

    async def compact_manual(self) -> dict[str, Any]:
        """用户主动要求的一次压缩(网页按钮 / `compact_context` 工具)。

        ⚠️ **不加锁**:工具跑在 respond() 已持有的回合锁里;网页路由会自己拿锁。
           在这里再拿一次同一把文件锁会死锁(同一进程也不保证可重入)。
        """
        before_tokens = config.estimate_messages_tokens(self.conversation_history())
        target_tokens = max(
            1, int(before_tokens * config.BUDGET_COMPACT_RETAIN_RATIO)
        )
        summary_id = await self.compact_once(via="manual")
        after_tokens = config.estimate_messages_tokens(self.conversation_history())
        report = dict(self.last_compaction or {})
        if summary_id is not None:
            return {
                "ok": True,
                "status": "compacted",
                "summary_id": int(summary_id),
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "target_tokens": target_tokens,
                # 目标达不到时(最近几条原文必须留着)要如实告诉模型,别让它复述"只剩目标"。
                "target_reached": after_tokens <= int(target_tokens * 1.1),
                "start_seq": int(report.get("start_seq") or 0),
                "end_seq": int(report.get("end_seq") or 0),
                "note": "已压缩",
            }
        return {
            "ok": False,
            "status": str(report.get("status") or "nothing_to_compact"),
            "reason": str(report.get("reason") or ""),
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "target_tokens": target_tokens,
            "note": "没有压缩",
        }

    async def _maybe_compact(self) -> None:
        projection = self.last_projection
        if projection is None:
            return
        if projection.dropped_turns == 0 and not projection.needs_compaction:
            return
        await self.compact_once()

    # -- 兼容 AgentLoop 的接口 -------------------------------------------
    def reset(self) -> None:
        """清空的是"当前投影",不是落盘数据 —— 记忆永远在库里。"""
        self.last_retrieval = None
        self.last_projection = None
        self.last_context_detail = None
        self.last_outcome = None
        self.last_compaction = None


# 框架自己发起的工具 & 工具的中文说法:**都从 tools/metadata.py 派生**(唯一真源)。
# 以前它们是两份手写字典,同一个工具名散在四个地方,新增工具漏改一处不会报错,
# 只会"显示难看 / 步骤行重复 / 只读闸多拦一次"。
from ..tools.metadata import internal_names as _internal_names
from ..tools.metadata import titles as _tool_titles

INTERNAL_TOOLS = _internal_names()
TOOL_TITLES: dict[str, str] = _tool_titles()

# 参数 -> 标题后缀。只挑人类看得懂的那一个字段,不做拼接实验。
TOOL_TITLE_PARAMS: dict[str, Callable[[dict[str, Any]], str]] = {
    "expense_record": lambda p: _money(p.get("amount_cents")),
    "expense_query": lambda p: str(p.get("period") or ""),
    "expense_correct_last": lambda p: str(p.get("category") or p.get("payee") or ""),
    "calendar_schedule": lambda p: str(p.get("title") or ""),
    "calendar_change": lambda p: str(p.get("uid") or "")[:12],
    "health_log": lambda p: _health_log_title(p),
    "quiz_write": lambda p: str(p.get("subject") or p.get("name") or "")[:24],
    "todo_write": lambda p: str(p.get("content") or "")[:24],
    "goal": lambda p: str(p.get("objective") or "")[:24],
    "schedule": lambda p: str(p.get("at") or p.get("prompt") or "")[:24],
    "query_summary_pool": lambda p: str(p.get("query") or "")[:20],
    "query_archive_search": lambda p: str(p.get("query") or "")[:20],
    # 步骤行显示「做网页:周报」而不是光秃秃一个工具名(action/old_string 都不是给人看的)
    "html_page": lambda p: str(p.get("title") or p.get("filename") or "")[:24],
}


def _health_log_title(params: dict[str, Any]) -> str:
    """步骤行上的那半句话:「仰卧起坐 10 个」/「跑步 30 分钟 · 5.2 公里」。

    ⭐ 2026-11-21(D188):以前只拼 duration_min,只记个数的运动会在步骤行上
    变成「仰卧起坐 」—— 用户看到的是一条没有量的记录。文案复用服务层同一个
    格式化(局部 import:标题表在 import 期就建好,不想为一句文案拉重依赖)。
    """
    from ..domain.services.health_service import workout_amount_text

    if not params.get("activity"):
        return str(params.get("metric_code") or params.get("food_name") or "").strip()
    amount = workout_amount_text(
        params.get("duration_min"), params.get("distance_km"), params.get("reps")
    )
    return (str(params.get("activity")) + " " + amount).strip()


def _money(cents: Any) -> str:
    try:
        value = int(cents)
    except (TypeError, ValueError):
        return ""
    return str(round(value / 100.0, 2)) + " 元"


def _report_from_dict(raw: dict[str, Any]) -> SubAgentReport:
    from ..tools.subagent import Finding

    report = SubAgentReport(
        agent_id=str(raw.get("agent_id") or ""),
        status=str(raw.get("status") or "empty"),
        stop_reason=str(raw.get("stop_reason") or "completed"),
        note=str(raw.get("note") or ""),
        elapsed_ms=float(raw.get("elapsed_ms") or 0.0),
        raw_tokens=int(raw.get("raw_tokens") or 0),
    )
    for item in raw.get("findings") or []:
        report.findings.append(
            Finding(
                timestamp=str(item.get("timestamp") or ""),
                content=str(item.get("content") or ""),
                seq=item.get("seq"),
                session_id=item.get("session_id"),
                salience=float(item.get("salience") or 0.5),
                locator=str(item.get("locator") or ""),
            )
        )
    return report
