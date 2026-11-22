"""Web 层的服务容器。

设计取舍:**每个请求开一套连接**(SQLite 连接很便宜),而不是全局共享一个连接。
理由是 FastAPI 会把同步端点丢进线程池,而 sqlite3 连接默认不允许跨线程使用 ——
共享连接要么加锁、要么关掉线程检查,两条都比"每请求一个连接"更容易出错。

M7 起这里还负责装配**统一写入回执**(ops_receipts)与它的撤销入口:
写入反馈是项目级机制,所以它跟着服务容器走,五个域共用一套。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from fastapi import Request

from .. import config
from ..agent.feedback import QuestionLog
from ..agent.persona import PersonaStore
from ..domain.assembly import build_domain_services
from ..domain.calendars.reminders import ReminderService
from ..domain.receipts import ReceiptLog, ReceiptReverter
from ..domain.receipts import build_fallback_reverter
from ..domain.reverters import build_reverter
from ..domain.services.calendar_service import CalendarService
from ..domain.services.health_service import HealthService
from ..domain.services.ledger_service import LedgerService
from ..domain.services.notes_service import NotesService
from ..domain.services.study_service import StudyService
from ..family import identity
from ..memory.store import MemoryStore
from ..storage import db
from ..tasks.goals import GoalStore
from ..tasks.schedules import ScheduleStore
from ..tasks.todos import TodoStore


@dataclass
class Services:
    settings: config.Settings
    assistant: object
    memory: object
    store: MemoryStore
    ledger: LedgerService
    calendar: CalendarService
    reminders: ReminderService
    study: StudyService
    health: HealthService
    #: 笔记域(M33)。⚠️ 与 reverter 里包的是**同一个实例** —— 否则撤销按钮
    #: 会拿着另一个 service 去找刚写的那条回执。
    notes: NotesService
    # 事项层(待办 / 目标 / 提醒)。⭐ 挂在这里而不是每次现造两个理由:
    #   ① 日历页的「提醒」「待办」两个页签要读它(只读路径不碰模型);
    #   ② 写入口必须与 reverter 里包的是**同一批实例** —— 否则刚写的那条回执
    #      在 memory.db 里,撤销入口却拿着另一个连接去找它。
    todos: TodoStore
    goals: GoalStore
    schedules: ScheduleStore
    receipts: ReceiptLog | None = None
    #: 事项层与记忆的回执住在 memory.db(回执与写入必须同库同事务)。
    memory_receipts: ReceiptLog | None = None
    reverter: ReceiptReverter | None = None
    # 统一反馈机制(M9):它在等你拍板什么。**只读展示 + 回答**都要它。
    questions: QuestionLog | None = None
    #: 当前登录成员(未启用 PIN / 未登录时为 None)。由 auth 中间件写进 request.state。
    member: Any = None
    # 注:记忆页的人工动作(停用/改口)需要"准入闸 + 模型客户端",那由 app 层现建
    # 一个 MemorySession 来拿(与 /chat、/chat/history 同一套装配方式),不在这里存工厂。
    # 注:人格(D117)同理 —— 它只要一个连接,页面与路由各自 PersonaStore(services.assistant)
    # 现造即可(它是个无状态包装,存进容器反而多一份需要同步的东西)。

    def close(self) -> None:
        for conn in (self.assistant, self.memory):
            try:
                conn.close()
            except Exception:
                pass


def build_services(member: Any = None) -> Services:
    member_id = getattr(member, "id", None)
    # ⚠️ 两个连接都关掉同线程检查 —— 这不是图省事,是 FastAPI 的调用模型决定的:
    # **同步的生成器依赖**在 run_in_threadpool 里推进,端点也在 run_in_threadpool 里跑,
    # 两者**不保证落在同一个工作线程上**。于是「在线程 A 建连接、在线程 B 用连接」是常态,
    # 而默认的 check_same_thread=True 会随机抛 ProgrammingError(表现为页面偶尔 500)。
    #
    # 安全性:这个连接的生命周期三阶段(建 → 用 → 关)是**严格串行**的,
    # 不存在两个线程同时使用它的情况;SQLite 本身是 serialized 线程模式。见 D66。
    assistant = db.connect_assistant(check_same_thread=False)
    db.set_member(assistant, member_id)
    # 页面上的模型/语音配置改过之后,靠版本号把新值拉进本进程(最多每秒一次)。
    # 放在这里而不是中间件:每个请求都要装配服务,天然覆盖所有页面与接口。
    from ..ops import secrets as secret_store

    secret_store.sync(assistant)
    # ⚠️ 必须在 sync 之后取 settings:反过来的话,保存后的**第一个**请求还会用旧 key。
    settings = config.get_settings()
    memory = db.connect_memory(check_same_thread=False)
    store = MemoryStore(memory)
    receipts = ReceiptLog(assistant)
    # ⚠️ 装配只有一处(2026-10-18 审计修):时钟/时区/审计/回执在
    #    domain/assembly.py 里注入一次。以前 Web 注入、CLI/微信不注入,
    #    同一台机器上"本月 / 今天到期"会按不同时区算(且全是静默的)。
    domain = build_domain_services(
        assistant, settings=settings, receipts=receipts, member_id=member_id,
    )
    calendar = domain.calendar
    ledger = domain.ledger
    study = domain.study
    health = domain.health
    notes = domain.notes
    # 事项层(待办/目标/提醒)住在记忆库里,它们的回执也写在那个连接上。
    memory_receipts = ReceiptLog(memory)
    todos = TodoStore(memory, receipts=memory_receipts, member_id=member_id)
    goals = GoalStore(memory, receipts=memory_receipts)
    schedules = ScheduleStore(memory, receipts=memory_receipts, member_id=member_id)
    # 人格(D117):上半段存在 sys_settings 里,人格页写、聊天每轮读;
    # 它的回执落在领域库那本(receipts)上 —— 所以撤销实现也挂在那一边。
    persona = PersonaStore(assistant)
    # 两本回执账拼成一个撤销入口:事项层/记忆的回执在 memory.db,
    # 领域写入的在 assistant.db —— 只认一边的话,另一个域的「撤销」按钮点了没反应。
    reverter = build_fallback_reverter(
        build_reverter(
            receipts, ledger=ledger, calendar=calendar, health=health, study=study,
            todos=todos, goals=goals, schedules=schedules,
            # 记忆的"改口/遗忘"撤销(M13):旧说法重新生效。
            memory=store,
            # 人格改写(D117):回执里带着上一版全文。
            persona=persona,
            # 笔记(M33):新增/改/删/挪/建夹 都要能一键撤回。
            notes=notes,
        ),
        build_reverter(
            memory_receipts, todos=todos, goals=goals, schedules=schedules, memory=store,
        ),
    )
    return Services(
        settings=settings,
        assistant=assistant,
        memory=memory,
        store=store,
        ledger=ledger,
        calendar=calendar,
        reminders=domain.reminders,
        study=study,
        health=health,
        notes=notes,
        todos=todos,
        goals=goals,
        schedules=schedules,
        receipts=receipts,
        memory_receipts=memory_receipts,
        reverter=reverter,
        questions=QuestionLog(assistant),
        member=member,
    )


def build_model_client(settings: config.Settings):
    """按 provider 造模型客户端。**全项目同一个口径**(实现只有一份:llm/factory.py)。

    ⚠️ fake 提供方返回假客户端:它在聊天里够用,但**没有视觉能力** ——
    所以 build_intake 会据此把识别关掉,而不是让它返回一段假识别结果。
    """
    from ..llm.factory import build_client

    return build_client(settings)


def build_intake(services: Services):
    """装配拍照录入流水线。视觉客户端**按需现造**:识别是一次性调用,不是长期连接。"""
    from ..domain.study.intake import IntakeService

    factory = None
    if services.settings.provider != "fake" and services.settings.api_key:
        factory = lambda: build_model_client(services.settings)  # noqa: E731
    member = getattr(services, "member", None)
    owner_session = (
        identity.session_id_for_member(member.id) if member is not None else None
    )
    return IntakeService(
        services.assistant,
        study=services.study,
        # 与错题服务**共用同一个**媒体仓 —— 存图与取图必须是同一套根路径
        media=services.study.media,
        client_factory=factory,
        owner_session=owner_session,
    )


def build_dispatcher(services: Services, *, briefing_hour: int = 9):
    """装配确定性调度器(与 MemorySession 里那份是**同一套**接线)。

    ⚠️ 为什么单独抽出来:定时派发有两个入口 —— 会话开始时(用户来聊天)与
    后台心跳(到点了但没人来)。两处必须用同一套 store、同一个 study_digest,
    否则"推没推过"会被两套口径各答一次。
    """
    from ..tasks.dispatcher import Dispatcher, KIND_REMINDER  # noqa: F401

    # ⚠️ 用容器里那三个 store,而不是在这里再造一批:日历页的写入口用的是同一批,
    #    两批实例各记各的回执时,「撤销」会对着另一个对象找刚写的那条。
    return Dispatcher(
        services.memory,
        settings=services.settings,
        todos=services.todos,
        goals=services.goals,
        schedules=services.schedules,
        summaries_between=services.store.summaries_between,
        briefing_hour=briefing_hour,
        study_digest=lambda moment: services.study.push_digest(now=moment),
    )


def get_services(request: Request) -> Iterator[Services]:
    """FastAPI 依赖:一次请求一套连接,请求结束就关。"""
    services = build_services(member=getattr(request.state, "member", None))
    try:
        yield services
    finally:
        services.close()
