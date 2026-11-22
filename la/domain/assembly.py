"""领域服务的**唯一装配点**(2026-10-18 审计修)。

为什么要它:同一批服务以前在三个地方各拼一遍 —— web/deps.py、agent/session.py、
bridge(经 session)——参数还各不相同:只有 Web 那套注入了 now_provider/tzid,
CLI/微信那套全用写死的默认时区。于是同一台机器上,网页与微信对"本月 / 今天到期"
会算出不同区间,而且是静默的。

现在:时钟、时区、审计、回执只在这里注入一次。入口只负责"用哪个连接、哪个成员"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .artifacts import ArtifactStore
from .audit import AuditSink
from .calendars.reminders import ReminderService
from .materials import MaterialService
from .services.calendar_service import CalendarService
from .services.health_service import HealthService
from .services.ledger_service import LedgerService
from .services.notes_service import NotesService
from .services.study_service import StudyService
from .study.intake import IntakeService


@dataclass(frozen=True)
class DomainServices:
    """一次性装配好的领域服务(全部共用同一个连接与同一个时钟)。"""

    artifacts: ArtifactStore
    ledger: LedgerService
    calendar: CalendarService
    reminders: ReminderService
    study: StudyService
    health: HealthService
    notes: NotesService
    materials: MaterialService
    intake: IntakeService


def build_domain_services(
    conn: Any,
    *,
    settings: Any,
    receipts: Any = None,
    member_id: int | None = None,
    client_factory: Callable[[], Any] | None = None,
    owner_session: str | None = None,
) -> DomainServices:
    """按**同一个** settings.now / settings.timezone 装配全部领域服务。"""
    sink = AuditSink(conn)
    now = settings.now
    tzid = settings.timezone
    artifacts = ArtifactStore(conn, now_provider=now, tzid=tzid)
    calendar = CalendarService(
        conn, audit=sink, now_provider=now, tzid=tzid, receipts=receipts,
        member_id=member_id,
    )
    ledger = LedgerService(
        conn, audit=sink, now_provider=now, tzid=tzid, receipts=receipts,
        member_id=member_id,
    )
    study = StudyService(
        conn, audit=sink, now_provider=now, tzid=tzid,
        srs_engine=settings.srs_engine, receipts=receipts, artifacts=artifacts,
        member_id=member_id,
    )
    health = HealthService(
        conn, audit=sink, now_provider=now, tzid=tzid, receipts=receipts,
        member_id=member_id,
    )
    notes = NotesService(
        conn, audit=sink, now_provider=now, tzid=tzid, receipts=receipts,
        member_id=member_id,
    )
    materials = MaterialService(
        conn, media=study.media, now_provider=now, tzid=tzid,
    )
    intake = IntakeService(
        conn, study=study, media=study.media, client_factory=client_factory,
        now_provider=now, tzid=tzid, owner_session=owner_session,
    )
    return DomainServices(
        artifacts=artifacts,
        ledger=ledger,
        calendar=calendar,
        # ⚠️ 时钟也必须注入(2026-10-18 审计):旧实现自己兜底 DEFAULT_TZID,
        #    改 LA_TIMEZONE 后 materialize 的窗口会与其它服务差一个时区。
        reminders=ReminderService(conn, calendar, now_provider=now),
        study=study,
        health=health,
        notes=notes,
        materials=materials,
        intake=intake,
    )
