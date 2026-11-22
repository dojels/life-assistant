"""错题服务:内容/调度分离、一题多单元、双视角。

⭐ 三条设计要点(能力域 §5.4),M4.3a 先把结构立住,算法(M4.3c)后补:

  1. **内容与调度必须分离**(抄 Anki 的思想,不抄它的表结构):
     改题干不会重置复习进度。
  2. **一道错题要拆成多个"复习单元"** —— 这不是优化,而是**能不能归因**的问题:
     只挂标签的话,孩子答错时你分不清他卡在"通分"还是"约分"。
  3. **孩子端与家长端是两个投影**:孩子端只有题干 + 图片 + "忘了/会了",
     不给任何家长视角下的评价(能力域 §6.5 划的边界)。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

from ... import config
from ...memory.archive import now_iso
from .. import ownership
from ..artifacts import KIND_FILE, KIND_HTML
from ..media import MediaStore
from ..receipts import DOMAIN_ARTIFACT, DOMAIN_STUDY, Receipt, ReceiptError, ReceiptLog
from ..study.audio import KIND_NONE as AUDIO_KIND_NONE
from ..study.audio import describe_spec as describe_audio
from ..study.audio import KIND_DICTATION, KIND_FOLLOW
from ..study.audio import merge_audio_specs, plan_from_problem, spec_of
from ..study.figure import crop_figure, expand_bbox, shrink_bbox
from ...ops import settings_store
from ..study.sheet import (
    SheetModel,
    SheetProblem,
    data_uri,
    estimate_minutes,
    label_for,
    render_html as render_sheet_html,
    steps_from_raw,
)
from ..schemas.study import (
    SUBJECTS,
    KnowledgePointCreate,
    ProblemCreate,
    ProblemQuery,
    ProblemRead,
    ProblemUpdate,
    ReviewUnitRead,
    StudyPushSettings,
    StudyStats,
    WeakPointRow,
)

# ⚠️ 单一来源(2026-10-17 审计):这个串原先在 8 个模块各写一份。
#    改 .env 的 LA_TIMEZONE 而这里不动,就会出现"提示词说伦敦、提醒按上海响"。
#    真正决定每个服务用哪个时区的是**构造参数 tzid**(由 build_services 注入 settings.timezone),
#    这里只是"没注入时的兜底"。
DEFAULT_TZID = config.DEFAULT_TIMEZONE
UNIT_ORD_BASE = 0        # 0 = 原题单元
FIRST_DUE_DAYS = 1       # ⚠️ 新建单元**不要立刻到期**,否则上传 20 道题就被 20 道题轰炸
DEFAULT_PUSH_TIME = "19:30"   # 默认推送时间:晚饭后,作业前的空档
DEFAULT_PUSH_LIMIT = 10       # 一次最多列几道:Anki 手册反复警告"新手一天学几百张,随后被压垮"


class StudyError(RuntimeError):
    """错题域的领域错误。"""


@dataclass
class KnowledgePoint:
    id: int
    name: str
    subject: str | None
    aliases: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "subject": self.subject,
                "aliases": list(self.aliases)}


@dataclass
class Student:
    """错题的**对象**。不是"本人"(那是健康档案的事),也不是账号。"""

    id: int
    name: str
    grade: str | None = None
    is_default: bool = False
    archived: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "grade": self.grade,
                "is_default": self.is_default, "archived": self.archived}


@dataclass
class StudySettings:
    """错题域的运行设置。**落库**,因为确定性调度器要读它(不能靠模型记得)。"""

    student_id: int | None = None
    student_name: str | None = None
    push_enabled: bool = True
    push_time_local: str = DEFAULT_PUSH_TIME
    push_daily_limit: int = DEFAULT_PUSH_LIMIT
    #: 推给哪些成员(D161)。空 = 所有已绑微信的成员。
    push_targets: tuple[int, ...] = ()
    #: 录完一道错题后,要不要**自动**让模型独立解一遍并裁出图形(M14,2026-10-13 起默认开)。
    #: 关掉它 = 只抄题干、不产出标准答案(照片上写的仍然只进 student_answer)。
    solve_on_recognize: bool = True
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "student_id": self.student_id,
            "student_name": self.student_name,
            "push_enabled": self.push_enabled,
            "push_time_local": self.push_time_local,
            "push_daily_limit": self.push_daily_limit,
            "push_targets": list(self.push_targets),
            "solve_on_recognize": self.solve_on_recognize,
            "updated_at": self.updated_at,
        }


def _parse_targets(raw: Any) -> tuple[int, ...]:
    """把库里的 push_targets 读成成员 id 元组。坏了就当"没选"(= 全部已绑定)。"""
    if not raw:
        return ()
    try:
        loaded = json.loads(str(raw))
    except Exception:  # noqa: BLE001
        return ()
    if not isinstance(loaded, list):
        return ()
    values: list[int] = []
    for item in loaded:
        try:
            member_id = int(item)
        except (TypeError, ValueError):
            continue
        if member_id > 0 and member_id not in values:
            values.append(member_id)
    return tuple(values)


def _first_error(exc: Exception) -> str:
    """把校验错误说成一句人话(哪个字段、哪里不对)。"""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            items = errors()
        except Exception:
            items = []
        if items:
            first = items[0]
            location = ".".join(str(part) for part in first.get("loc") or [])
            return (location + " " if location else "") + str(first.get("msg") or "")[:60]
    return str(exc)[:120]


def parse_push_time(value: str) -> str:
    """把用户/模型给的时间收敛成 `HH:MM`。

    ⚠️ 模型很爱回"晚上7点半""19:30:00""7:30 PM" —— 与其拒绝,不如认出来;
    真的认不出才报错。返回的是**归一化后**的字符串,存进库的就是它。
    """
    text = str(value or "").strip()
    if not text:
        raise StudyError("推送时间不能为空")
    lowered = text.lower().replace(" ", "")
    meridiem = ""
    for mark in ("am", "pm", "上午", "下午", "晚上", "早上", "中午"):
        if lowered.endswith(mark):
            meridiem = mark
            lowered = lowered[: -len(mark)]
            break
    if lowered.startswith("晚上") or lowered.startswith("下午"):
        meridiem = lowered[:2]
        lowered = lowered[2:]
    hours_text, _, minutes_text = lowered.partition(":")
    if not minutes_text:
        # "19点30" / "19时30分" 这类写法
        for mark in ("点", "时"):
            if mark in hours_text:
                hours_text, _, minutes_text = hours_text.partition(mark)
                break
    minutes_text = minutes_text.rstrip("分钟")
    # "七点半" / "19点整" —— 中文口语里最常出现的两个说法,认出来比报错有用
    if minutes_text == "半":
        minutes_text = "30"
    elif minutes_text in ("", "整"):
        minutes_text = "0"
    try:
        hours = int(hours_text)
        minutes = int(minutes_text)
    except ValueError as exc:
        raise StudyError("推送时间要写成 24 小时制的 HH:MM,例如 19:30(收到的是 " + text + ")") from exc
    if meridiem in ("pm", "下午", "晚上") and hours < 12:
        hours += 12
    if meridiem in ("am", "上午", "早上") and hours == 12:
        hours = 0
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise StudyError("推送时间超出范围: " + text)
    return str(hours).zfill(2) + ":" + str(minutes).zfill(2)


class StudyService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        media: MediaStore | None = None,
        audit: Callable[..., None] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        tzid: str = DEFAULT_TZID,
        srs_engine: str = "fixed",
        receipts: ReceiptLog | None = None,
        artifacts: Any = None,
        member_id: int | None = None,
    ) -> None:
        from ..study.scheduler import build_policy

        self.conn = conn
        self.member_id = member_id
        self.media = media or MediaStore(conn)
        # 产物层(错题卷/清单)。**可选**:没有它照样能复习,只是出不了文件。
        self.artifacts = artifacts
        self._audit = audit
        self._now = now_provider
        self.tzid = tzid
        self.policy = build_policy(srs_engine, tzid=tzid)
        # 统一写入回执(M7)
        self.receipts = receipts

    def now(self) -> datetime:
        """这个服务认为的"现在"(配置时区)。工具层要时间戳时用它,别用私有方法。"""
        return self._current_time()

    def _current_time(self) -> datetime:
        if self._now is not None:
            return self._now()
        return datetime.now(ZoneInfo(self.tzid))

    def _owner_clause(self, column: str = "owner_id") -> tuple[str, list[object]]:
        return ownership.owner_filter(getattr(self, "member_id", None), column)

    def _owner_for_new(self) -> int | None:
        return ownership.owner_for_new(
            self.conn, ownership.DOMAIN_STUDY, getattr(self, "member_id", None)
        )

    # -- 学生 -------------------------------------------------------------
    def list_students(self, *, include_archived: bool = False) -> list[Student]:
        sql = "SELECT * FROM app_students"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY is_default DESC, name"
        return [
            Student(
                id=int(row["id"]), name=row["name"], grade=row["grade"],
                is_default=bool(row["is_default"]), archived=bool(row["archived"]),
            )
            for row in self.conn.execute(sql).fetchall()
        ]

    def ensure_student(
        self, name: str, *, grade: str | None = None, make_default: bool = False
    ) -> Student:
        """按名字取学生;没有就建。**同名复用** —— 名字就是这里的自然键。"""
        text = (name or "").strip()
        if not text:
            raise StudyError("学生名字不能为空")
        row = self.conn.execute(
            "SELECT * FROM app_students WHERE name = ?", (text,)
        ).fetchone()
        if row is None:
            cursor = self.conn.execute(
                "INSERT INTO app_students(name, grade, is_default, archived, created_at)"
                " VALUES (?, ?, 0, 0, ?)",
                (text, grade, now_iso()),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM app_students WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        elif grade and grade != row["grade"]:
            self.conn.execute("UPDATE app_students SET grade = ? WHERE id = ?", (grade, row["id"]))
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM app_students WHERE id = ?", (row["id"],)
            ).fetchone()
        student = Student(
            id=int(row["id"]), name=row["name"], grade=row["grade"],
            is_default=bool(row["is_default"]), archived=bool(row["archived"]),
        )
        if row["archived"]:
            self.conn.execute("UPDATE app_students SET archived = 0 WHERE id = ?", (student.id,))
            self.conn.commit()
            student.archived = False
        if make_default:
            student = self.set_default_student(student.id)
        return student

    def default_student(self) -> Student:
        """默认学生。一个都没有时**现场建一个** —— 兜底路径不能是"抛异常"。"""
        row = self.conn.execute(
            "SELECT * FROM app_students WHERE archived = 0 ORDER BY is_default DESC, id LIMIT 1"
        ).fetchone()
        if row is None:
            return self.ensure_student("孩子", make_default=True)
        return Student(
            id=int(row["id"]), name=row["name"], grade=row["grade"],
            is_default=bool(row["is_default"]), archived=bool(row["archived"]),
        )

    def set_default_student(self, student_id: int) -> Student:
        self.conn.execute("UPDATE app_students SET is_default = 0")
        self.conn.execute(
            "UPDATE app_students SET is_default = 1, archived = 0 WHERE id = ?", (int(student_id),)
        )
        # ⚠️ 必须 UPSERT:设置表可能还一行都没有(第一次就把默认学生定下来是最常见的用法),
        #    裸 UPDATE 会**静默地什么都不做** —— 这正是"设了但没生效"这类 bug 的样子。
        self.conn.execute(
            "INSERT INTO app_study_settings(id, student_id, updated_at) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET student_id = excluded.student_id,"
            " updated_at = excluded.updated_at",
            (int(student_id), now_iso()),
        )
        self.conn.commit()
        for student in self.list_students():
            if student.id == int(student_id):
                return student
        raise StudyError("学生不存在: " + str(student_id))

    def rename_student(self, student_id: int, name: str) -> Student:
        text = (name or "").strip()
        if not text:
            raise StudyError("学生名字不能为空")
        clash = self.conn.execute(
            "SELECT id FROM app_students WHERE name = ? AND id <> ?", (text, int(student_id))
        ).fetchone()
        if clash is not None:
            raise StudyError("已经有一个叫「" + text + "」的学生了")
        self.conn.execute(
            "UPDATE app_students SET name = ? WHERE id = ?", (text, int(student_id))
        )
        self.conn.commit()
        return self.ensure_student(text)

    def student_for(
        self, *, student_id: int | None = None, student_name: str | None = None
    ) -> Student:
        """把"录给谁"这件事收敛成一个学生的 id。"""
        if student_id is not None:
            row = self.conn.execute(
                "SELECT * FROM app_students WHERE id = ?", (int(student_id),)
            ).fetchone()
            if row is None:
                raise StudyError("学生不存在: " + str(student_id))
            return Student(
                id=int(row["id"]), name=row["name"], grade=row["grade"],
                is_default=bool(row["is_default"]), archived=bool(row["archived"]),
            )
        if student_name and str(student_name).strip():
            return self.ensure_student(str(student_name))
        settings = self.settings()
        if settings.student_id is not None:
            row = self.conn.execute(
                "SELECT * FROM app_students WHERE id = ?", (int(settings.student_id),)
            ).fetchone()
            if row is not None:
                return Student(
                    id=int(row["id"]), name=row["name"], grade=row["grade"],
                    is_default=bool(row["is_default"]), archived=bool(row["archived"]),
                )
        return self.default_student()

    # -- 推送设置 ---------------------------------------------------------
    def settings(self) -> StudySettings:
        """读设置。**没有行也能读** —— 读路径不允许因为"还没设置过"而失败。"""
        row = self.conn.execute("SELECT * FROM app_study_settings WHERE id = 1").fetchone()
        student_name = None
        student_id = None
        if row is not None and row["student_id"] is not None:
            student_id = int(row["student_id"])
            found = self.conn.execute(
                "SELECT name FROM app_students WHERE id = ?", (student_id,)
            ).fetchone()
            student_name = found["name"] if found is not None else None
        if row is None or student_id is None:
            # ⚠️ 设置表可能还没写过(第一次用),但学生是**有的**(迁移就种了一个)。
            #    这里只读不建 —— 读路径不该有副作用。默认学生的权威来源仍是 app_students。
            fallback = self.conn.execute(
                "SELECT id, name FROM app_students WHERE archived = 0"
                " ORDER BY is_default DESC, id LIMIT 1"
            ).fetchone()
            if fallback is not None:
                student_id = int(fallback["id"])
                student_name = fallback["name"]
        if row is None:
            return StudySettings(student_id=student_id, student_name=student_name)
        return StudySettings(
            student_id=student_id,
            student_name=student_name,
            push_enabled=bool(row["push_enabled"]),
            push_time_local=row["push_time_local"] or DEFAULT_PUSH_TIME,
            push_daily_limit=int(row["push_daily_limit"] or DEFAULT_PUSH_LIMIT),
            push_targets=_parse_targets(row["push_targets"]),
            solve_on_recognize=bool(row["solve_on_recognize"]),
            updated_at=row["updated_at"],
        )

    def update_settings(
        self,
        payload: StudyPushSettings | dict[str, Any] | None = None,
        *,
        actor: str = "user",
        **kwargs: Any,
    ) -> StudySettings:
        """改设置。界面与模型走**同一条**路径(单真源),所以校验只写一份。"""
        merged: dict[str, Any] = {}
        if payload is not None:
            if isinstance(payload, StudyPushSettings):
                model = payload
            else:
                try:
                    model = StudyPushSettings.model_validate(payload)
                except Exception as exc:
                    # ⚠️ 把 pydantic 的报错翻成领域错误:调用方(界面 / 模型工具)只需要知道
                    #    "这个值不行",不需要知道是哪一层校验器拒的。
                    raise StudyError("设置有误:" + _first_error(exc)) from exc
            merged.update(model.model_dump(exclude_none=True))
        merged.update({key: value for key, value in kwargs.items() if value is not None})

        current = self.settings()
        fields: dict[str, Any] = {
            "student_id": current.student_id,
            "push_enabled": 1 if current.push_enabled else 0,
            "push_time_local": current.push_time_local,
            "push_daily_limit": current.push_daily_limit,
            "push_targets": json.dumps(list(current.push_targets)),
            "solve_on_recognize": 1 if current.solve_on_recognize else 0,
        }
        if "enabled" in merged:
            fields["push_enabled"] = 1 if merged["enabled"] else 0
        if "push_time" in merged:
            fields["push_time_local"] = parse_push_time(str(merged["push_time"]))
        if "daily_limit" in merged:
            limit = int(merged["daily_limit"])
            if not (1 <= limit <= 50):
                raise StudyError("每日上限要在 1 到 50 之间")
            fields["push_daily_limit"] = limit
        if "solve_on_recognize" in merged:
            fields["solve_on_recognize"] = 1 if merged["solve_on_recognize"] else 0
        if "targets" in merged:
            raw_targets = merged["targets"] or []
            targets: list[int] = []
            for item in raw_targets:
                try:
                    member_id = int(item)
                except (TypeError, ValueError) as exc:
                    raise StudyError("收件人必须是成员 id") from exc
                if member_id > 0 and member_id not in targets:
                    targets.append(member_id)
            fields["push_targets"] = json.dumps(targets)
        if merged.get("student_id") is not None:
            fields["student_id"] = self.set_default_student(int(merged["student_id"])).id
        # ⚠️ 顺序:先按 id,再按名字。两个都给了就以**名字**为准 ——
        #    用户刚刚敲进去的那个名字,才是他真正想要的那个学生。
        if "student_name" in merged:
            student = self.ensure_student(
                str(merged["student_name"]),
                grade=merged.get("grade"),
                make_default=bool(merged.get("make_default", True)),
            )
            fields["student_id"] = student.id

        self.conn.execute(
            "INSERT INTO app_study_settings(id, student_id, push_enabled, push_time_local,"
            " push_daily_limit, push_targets, solve_on_recognize, updated_at)"
            " VALUES (1, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET student_id = excluded.student_id,"
            " push_enabled = excluded.push_enabled,"
            " push_time_local = excluded.push_time_local,"
            " push_daily_limit = excluded.push_daily_limit,"
            " push_targets = excluded.push_targets,"
            " solve_on_recognize = excluded.solve_on_recognize,"
            " updated_at = excluded.updated_at",
            (
                fields["student_id"], fields["push_enabled"], fields["push_time_local"],
                fields["push_daily_limit"], fields["push_targets"],
                fields["solve_on_recognize"], now_iso(),
            ),
        )
        self.conn.commit()
        if self._audit is not None:
            self._audit(
                actor=actor, action="study.settings",
                target="study_settings:1", detail=dict(merged),
            )
            self.conn.commit()
        return self.settings()

    # -- 知识点 -----------------------------------------------------------
    def resolve_knowledge_point(self, name: str, *, subject: str | None = None,
                                commit: bool = True) -> KnowledgePoint:
        """精确名 -> 别名 -> 新建。同一个知识点的不同叫法必须归到一行。"""
        text = (name or "").strip()
        if not text:
            raise StudyError("知识点名称不能为空")

        row = self.conn.execute(
            "SELECT * FROM app_knowledge_points WHERE name = ? AND (subject IS ? OR subject = ?)"
            " ORDER BY id LIMIT 1",
            (text, subject, subject),
        ).fetchone()
        if row is None:
            alias = self.conn.execute(
                "SELECT kp_id FROM app_kp_aliases WHERE alias = ?", (text,)
            ).fetchone()
            if alias is not None:
                row = self.conn.execute(
                    "SELECT * FROM app_knowledge_points WHERE id = ?", (alias["kp_id"],)
                ).fetchone()
        if row is None:
            cursor = self.conn.execute(
                "INSERT INTO app_knowledge_points(name, subject, created_at) VALUES (?, ?, ?)",
                (text, subject, now_iso()),
            )
            if commit:
                self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM app_knowledge_points WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._kp_from_row(row)

    def _kp_from_row(self, row: sqlite3.Row) -> KnowledgePoint:
        aliases = [
            item["alias"]
            for item in self.conn.execute(
                "SELECT alias FROM app_kp_aliases WHERE kp_id = ? ORDER BY alias", (row["id"],)
            ).fetchall()
        ]
        return KnowledgePoint(
            id=int(row["id"]), name=row["name"], subject=row["subject"], aliases=aliases
        )

    def upsert_knowledge_point(self, payload: KnowledgePointCreate | dict[str, Any]) -> KnowledgePoint:
        if not isinstance(payload, KnowledgePointCreate):
            payload = KnowledgePointCreate.model_validate(payload)
        point = self.resolve_knowledge_point(payload.name, subject=payload.subject)
        for alias in payload.aliases:
            text = (alias or "").strip()
            if not text or text == point.name:
                continue
            self.conn.execute(
                "INSERT INTO app_kp_aliases(alias, kp_id) VALUES (?, ?)"
                " ON CONFLICT(alias) DO UPDATE SET kp_id = excluded.kp_id",
                (text, point.id),
            )
        if self.receipts is not None and payload.aliases:
            self.receipts.record(
                kind="study.knowledge_point",
                domain=DOMAIN_STUDY,
                label="补 " + point.name + " 的别名:" + "/".join(
                    str(item) for item in payload.aliases[:4]
                ),
                detail={"kp_id": point.id, "name": point.name,
                        "aliases": [str(item) for item in payload.aliases]},
                # 别名只是"多认几个叫法",撤销 = 把这些别名解绑,不动知识点本身。
                undo_plan={"type": "aliases", "kp_id": point.id,
                           "aliases": [str(item) for item in payload.aliases]},
                actor="agent",
            )
        self.conn.commit()
        return self.resolve_knowledge_point(point.name, subject=payload.subject)

    def list_knowledge_points(self, *, subject: str | None = None) -> list[KnowledgePoint]:
        sql = "SELECT * FROM app_knowledge_points"
        params: list[Any] = []
        if subject:
            sql += " WHERE subject = ?"
            params.append(subject)
        sql += " ORDER BY name"
        return [self._kp_from_row(row) for row in self.conn.execute(sql, params).fetchall()]

    # -- 素材 → 题目照片 ---------------------------------------------------
    def asset_ids_from_materials(self, material_ids: Iterable[int] | None) -> list[int]:
        """把**素材编号**(聊天里收到的 #N)换成**资产编号**(题目照片)。

        ⚠️ 为什么必须换、而不是直接收资产 id:2026-11-01 真机 #16–#21 —— 模型在
           素材清单里看到「#12」,录题时把 12 填进了 asset_ids;而 12 恰好是一张
           十月的旧图形。六道英语题因此全挂上了那张小正方体图。
           编号体系只能有一个入口,换算是**服务端**的事。

        只认 `kind='image'` 的素材(语音/文件不当照片);认不出的 id 直接跳过,不抛。
        """
        wanted: list[int] = []
        for item in material_ids or []:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value not in wanted:
                wanted.append(value)
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        try:
            rows = self.conn.execute(
                "SELECT id, asset_id, kind FROM app_materials WHERE id IN (" + marks + ")",
                tuple(wanted),
            ).fetchall()
        except sqlite3.Error:
            return []
        found = {int(row["id"]): row for row in rows}
        result: list[int] = []
        for material_id in wanted:          # 保持调用方给的顺序
            row = found.get(material_id)
            if row is None or row["asset_id"] is None:
                continue
            if str(row["kind"] or "") != "image":
                continue
            asset_id = int(row["asset_id"])
            if asset_id not in result:
                result.append(asset_id)
        return result

    # -- 错题 -------------------------------------------------------------
    def add_problem(
        self, payload: ProblemCreate | dict[str, Any], *, actor: str = "agent",
        session_id: str | None = None,
    ) -> ProblemRead:
        if not isinstance(payload, ProblemCreate):
            payload = ProblemCreate.model_validate(payload)
        subject = (payload.subject or "").strip()
        if not subject:
            raise StudyError("必须给出学科")

        asset_ids = list(payload.asset_ids)
        for path in payload.image_paths:
            asset = self.media.store_file(path)
            asset_ids.append(asset.id)

        # 归属:错题是**某个学生**的。前端没显式选就落到默认学生(现场建也要建出来)——
        # 这条路径不能失败,否则"录一道题"这件事会因为"还没建学生"而卡住。
        student = self.student_for(
            student_id=payload.student_id, student_name=payload.student_name
        )
        # ⚠️ 从这里到最后的 commit 是**一个事务**(2026-10-17 审计)。
        #    旧写法分 5 次提交(题 → 知识点 → 复习单元 → 回执 → outbox),中间任何一步失败
        #    都会留下"题在库里、没回执(没有撤销按钮)、没 outbox(记忆永远不知道这道题)"。
        #
        #    知识点是例外:新建一个知识点会自己提交(它是独立的建点操作,不该跟着一道题回滚),
        #    所以那一步不参与这个事务 —— 换来的是"最坏情况多一个没人用的知识点"。
        #
        #    ⚠️ 失败必须**显式 rollback**:异常往上抛之后,调用方还会用同一个连接写别的,
        #    不 rollback 的话那半截题会被下一次 commit 顺手提交上去。
        problem_id = 0
        owner = self._owner_for_new()
        try:
            cursor = self.conn.execute(
                "INSERT INTO app_problems(subject, grade, source, stem_text, answer_text,"
                " explanation, asset_ids, status, student_id, created_at, updated_at, owner_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    subject, payload.grade, payload.source, payload.stem_text,
                    payload.answer_text, payload.explanation,
                    json.dumps(asset_ids), student.id, now_iso(), now_iso(), owner,
                ),
            )
            problem_id = int(cursor.lastrowid or 0)

            for name in payload.knowledge_points:
                point = self.resolve_knowledge_point(name, subject=subject, commit=False)
                self.conn.execute(
                    "INSERT OR IGNORE INTO app_problem_kps(problem_id, kp_id) VALUES (?, ?)",
                    (problem_id, point.id),
                )

            self.ensure_review_units(problem_id)
            if self.receipts is not None:
                kp_text = (" · " + "/".join(str(item) for item in payload.knowledge_points[:3])
                           if payload.knowledge_points else "")
                self.receipts.record(
                    kind="study.problem",
                    domain=DOMAIN_STUDY,
                    label="录错题 " + subject + kp_text,
                    detail={
                        "problem_id": problem_id,
                        "subject": subject,
                        "student": student.name,
                        "knowledge_points": [str(item) for item in payload.knowledge_points],
                        "stem": (payload.stem_text or "")[:80],
                    },
                    # 撤销 = 归档(不是删行):复习日志与单元都留着,统计口径不破。
                    undo_plan={"type": "problem", "problem_id": problem_id},
                    actor=actor,
                    session_id=session_id,
                )
            # outbox 与回执一起进同一个事务:记忆不该"这道题永远不知道"。
            self._emit(
                "study.problem.added",
                {"problem_id": problem_id, "subject": subject, "student": student.name,
                 "knowledge_points": list(payload.knowledge_points)},
                key="study.problem.added:" + str(problem_id),
                commit=False,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        if self._audit is not None:
            self._audit(
                actor=actor, action="study.problem.add", target="problem:" + str(problem_id),
                detail={"subject": subject, "assets": asset_ids, "student": student.name,
                        "knowledge_points": list(payload.knowledge_points)},
                session_id=session_id,
            )
            self.conn.commit()
        return self.get_problem(problem_id)

    def get_problem(self, problem_id: int) -> ProblemRead:
        clause, params = self._owner_clause()
        row = self.conn.execute(
            "SELECT * FROM app_problems WHERE id = ? AND " + clause,
            (problem_id, *params),
        ).fetchone()
        if row is None:
            raise StudyError("错题不存在: " + str(problem_id))
        return self._problem_from_row(row)

    def list_problems(self, query: ProblemQuery | dict[str, Any] | None = None) -> list[ProblemRead]:
        if query is None:
            query = ProblemQuery()
        if not isinstance(query, ProblemQuery):
            query = ProblemQuery.model_validate(query)
        sql = "SELECT DISTINCT p.* FROM app_problems p"
        params: list[Any] = []
        joins = ""
        where: list[str] = []
        owner_sql, owner_params = self._owner_clause("p.owner_id")
        where.append(owner_sql)
        params.extend(owner_params)
        if query.knowledge_point:
            joins += (
                " JOIN app_problem_kps pk ON pk.problem_id = p.id"
                " JOIN app_knowledge_points k ON k.id = pk.kp_id"
            )
            where.append("(k.name = ? OR k.id IN (SELECT kp_id FROM app_kp_aliases WHERE alias = ?))")
            params.extend([query.knowledge_point, query.knowledge_point])
        if query.subject:
            where.append("p.subject = ?")
            params.append(query.subject)
        if query.student_id is not None:
            where.append("p.student_id = ?")
            params.append(int(query.student_id))
        if query.status:
            where.append("p.status = ?")
            params.append(query.status)
        elif not query.include_archived:
            where.append("p.status <> 'archived'")
        sql += joins
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY p.id DESC LIMIT ?"
        params.append(query.limit)
        return [self._problem_from_row(row) for row in self.conn.execute(sql, params).fetchall()]

    def problem_counts(self) -> dict[str, Any]:
        """错题本那一页的分面计数:**不含已归档**的各学科数量 + 已归档总数。

        ⭐ 与 stats().total_problems 的区别:那个把已归档也算进去(它是"库里一共多少道")。
        拿它当页签徽标,会出现"页签写 23、列表明明只有 17 道"的错觉(2026-11-09 实测),
        所以列表这一页另有一份**与筛选项对得上**的计数。
        """
        clause, params = self._owner_clause("owner_id")
        rows = self.conn.execute(
            "SELECT subject, status, count(*) AS n FROM app_problems WHERE " + clause
            + " GROUP BY subject, status",
            params,
        ).fetchall()
        by_subject: dict[str, int] = {}
        archived = 0
        total = 0
        for row in rows:
            count = int(row["n"] or 0)
            if str(row["status"]) == "archived":
                archived += count
                continue
            total += count
            name = str(row["subject"] or "")
            by_subject[name] = by_subject.get(name, 0) + count
        return {
            "total": total,
            "archived": archived,
            "by_subject": dict(sorted(by_subject.items(), key=lambda item: (-item[1], item[0]))),
        }

    def update_problem(
        self,
        problem_id: int,
        payload: ProblemUpdate | dict[str, Any] | None = None,
        *,
        subject: str | None = None,
        stem_text: str | None = None,
        answer_text: str | None = None,
        explanation: str | None = None,
        grade: str | None = None,
        source: str | None = None,
        knowledge_points: list[str] | None = None,
        student_id: int | None = None,
    ) -> ProblemRead:
        """改内容。

        ⭐ 这里**不动 app_review_units** —— 内容与调度分离的意义就在这:
        改个错别字不该让孩子把这道题重新学一遍。

        ⚠️ 唯一的例外是知识点:增删知识点**会**影响复习单元(每个知识点一个单元)。
        但处理方式仍然是"增的补单元、减的**暂停**单元",绝不删除带进度的单元。
        """
        # ⚠️ owner 越权检查(2026-10-18 审计修):这一行以前**写在 docstring 里**,
        #    于是它只是个字符串 —— member 2 用 id 能改 member 1 的题(改名成功落库),
        #    随后 get_problem 才抛"错题不存在",用户以为没改成。
        self.get_problem(problem_id)
        merged: dict[str, Any] = {
            "subject": subject, "stem_text": stem_text, "answer_text": answer_text,
            "explanation": explanation, "grade": grade, "source": source,
            "knowledge_points": knowledge_points, "student_id": student_id,
        }
        if payload is not None:
            model = (
                payload if isinstance(payload, ProblemUpdate)
                else ProblemUpdate.model_validate(payload)
            )
            for key, value in model.model_dump(exclude_none=True).items():
                merged[key] = value
        points = merged.pop("knowledge_points", None)

        fields: list[str] = []
        params: list[Any] = []
        for column in ("subject", "stem_text", "answer_text", "explanation", "grade",
                       "source", "student_id"):
            value = merged.get(column)
            if value is None:
                continue
            if column == "subject":
                text = str(value).strip()
                if not text:
                    raise StudyError("学科不能改成空")
                if text not in SUBJECTS:
                    raise StudyError("学科必须是这些之一:" + "/".join(SUBJECTS))
                value = text
            fields.append(column + " = ?")
            params.append(value)
        if fields:
            clause, owner_params = self._owner_clause()
            params.extend([now_iso(), int(problem_id), *owner_params])
            self.conn.execute(
                "UPDATE app_problems SET " + ", ".join(fields) + ", updated_at = ?"
                " WHERE id = ? AND " + clause,
                params,
            )
            self.conn.commit()
        if points is not None:
            self.set_knowledge_points(problem_id, list(points))
        return self.get_problem(problem_id)

    def set_knowledge_points(self, problem_id: int, names: list[str]) -> ProblemRead:
        """整份替换知识点。新增的补复习单元;移除的**暂停**其单元(进度留着)。"""
        problem = self.get_problem(problem_id)
        subject = problem.subject
        wanted: list[int] = []
        for name in names:
            text = str(name or "").strip()
            if not text:
                continue
            point = self.resolve_knowledge_point(text, subject=subject)
            if point.id not in wanted:
                wanted.append(point.id)
        current = [
            int(row["kp_id"])
            for row in self.conn.execute(
                "SELECT kp_id FROM app_problem_kps WHERE problem_id = ?", (int(problem_id),)
            ).fetchall()
        ]
        for kp_id in current:
            if kp_id not in wanted:
                self.conn.execute(
                    "DELETE FROM app_problem_kps WHERE problem_id = ? AND kp_id = ?",
                    (int(problem_id), kp_id),
                )
        for kp_id in wanted:
            self.conn.execute(
                "INSERT OR IGNORE INTO app_problem_kps(problem_id, kp_id) VALUES (?, ?)",
                (int(problem_id), kp_id),
            )
        self.conn.commit()
        self.ensure_review_units(int(problem_id))
        # 暂停 / 恢复:单元跟着知识点走,但**永远不删**
        self.conn.execute(
            "UPDATE app_review_units SET suspended = 1, updated_at = ?"
            " WHERE problem_id = ? AND kp_id IS NOT NULL AND kp_id NOT IN"
            " (SELECT kp_id FROM app_problem_kps WHERE problem_id = ?)",
            (now_iso(), int(problem_id), int(problem_id)),
        )
        self.conn.execute(
            "UPDATE app_review_units SET suspended = 0, updated_at = ?"
            " WHERE problem_id = ? AND kp_id IS NOT NULL AND kp_id IN"
            " (SELECT kp_id FROM app_problem_kps WHERE problem_id = ?)",
            (now_iso(), int(problem_id), int(problem_id)),
        )
        self.conn.commit()
        return self.get_problem(int(problem_id))

    def archive_problem(self, problem_id: int) -> ProblemRead:
        self.get_problem(problem_id)
        """归档(不是删除):复习日志与单元都留着,统计口径不破。"""
        self.conn.execute(
            "UPDATE app_problems SET status = 'archived', updated_at = ? WHERE id = ?",
            (now_iso(), int(problem_id)),
        )
        self.conn.execute(
            "UPDATE app_review_units SET suspended = 1, updated_at = ? WHERE problem_id = ?",
            (now_iso(), int(problem_id)),
        )
        self.conn.commit()
        return self.get_problem(int(problem_id))

    def delete_problem(
        self, problem_id: int, *, reason: str = "", actor: str = "user",
        session_id: str = "",
    ) -> dict[str, Any]:
        """彻底删掉一道**录错了**的错题(与"归档"分工)。

        ⭐ 2026-11-09 用户原话:"录错的,智能体自己也删除不掉" —— 聊天里连着录了三条
        take care of,前两条只能归档:复习清单里是没有了,可错题本、统计、库里还都在,
        模型也没有任何入口能把它们清掉。

        ⚠️ 边界:**有复习记录的题一律不删**,改成归档并**如实说明**。复习日志是统计口径
        与 FSRS 调参的证据(攒够 1000 条才能训参数),删掉等于篡改历史;而"录错"的题
        本来就没复习过 —— 这个边界不会挡住真正的清理。
        """
        problem = self.get_problem(problem_id)          # 不存在 → StudyError
        unit_ids = [int(unit.id) for unit in problem.review_units]
        logs = 0
        if unit_ids:
            marks = ",".join("?" for _ in unit_ids)
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM app_review_logs WHERE unit_id IN (" + marks + ")",
                tuple(unit_ids),
            ).fetchone()
            logs = int(row["n"] or 0) if row is not None else 0
        reps = sum(int(unit.reps or 0) for unit in problem.review_units)
        if logs or reps:
            self.archive_problem(problem_id)
            return {
                "problem_id": int(problem_id),
                "deleted": False,
                "archived": True,
                "review_logs": logs,
                "note": ("这题有 " + str(max(logs, reps)) + " 条复习记录 —— 没有删,改成归档了:"
                         "复习记录与统计口径都留着,它也不会再出现在复习清单里。"),
            }
        # 没有复习记录 = 这是"录错了"的那一类:真删(单元、知识点挂接、日志一起清)
        self.conn.execute("DELETE FROM app_review_logs WHERE problem_id = ?", (int(problem_id),))
        self.conn.execute("DELETE FROM app_review_units WHERE problem_id = ?", (int(problem_id),))
        self.conn.execute("DELETE FROM app_problem_kps WHERE problem_id = ?", (int(problem_id),))
        self.conn.execute("DELETE FROM app_problems WHERE id = ?", (int(problem_id),))
        self.conn.commit()
        if self._audit is not None:
            self._audit(
                actor=actor, action="study.problem.delete",
                target="problem:" + str(problem_id),
                detail={"subject": problem.subject,
                        "stem": (problem.stem_text or "")[:80],
                        "reason": str(reason or "")[:100]},
                session_id=session_id,
            )
        return {
            "problem_id": int(problem_id),
            "deleted": True,
            "archived": False,
            "review_logs": 0,
            "subject": problem.subject,
            "stem": (problem.stem_text or "")[:60],
            "note": ("已彻底删除 #" + str(problem_id) + "(" + problem.subject + "):"
                     "这题没有复习记录,删掉不影响任何统计。"),
        }

    def restore_problem(self, problem_id: int) -> ProblemRead:
        self.get_problem(problem_id)
        """取消归档。原来是 pending 还是 confirmed 无法还原,统一回到 confirmed。"""
        self.conn.execute(
            "UPDATE app_problems SET status = 'confirmed', updated_at = ? WHERE id = ?",
            (now_iso(), int(problem_id)),
        )
        self.conn.execute(
            "UPDATE app_review_units SET suspended = 0, updated_at = ?"
            " WHERE problem_id = ? AND (kp_id IS NULL OR kp_id IN"
            " (SELECT kp_id FROM app_problem_kps WHERE problem_id = ?))",
            (now_iso(), int(problem_id), int(problem_id)),
        )
        self.conn.commit()
        return self.get_problem(int(problem_id))

    def problem_detail(self, problem_id: int) -> dict[str, Any]:
        self.get_problem(problem_id)
        """一道题的全部:内容 + 单元(含知识点)+ 复习历史 + 配图。

        ⭐ 这是**家长视角**:孩子端永远只拿 render_card(child)。
        """
        problem = self.get_problem(problem_id)
        # ⭐ 一次作答会记到这道题**当天到期的所有单元**上(D108):整题一条、每个知识点
        #    各一条 —— 所以同一时刻的几条日志其实是**一次**复习。以前按单元平铺,
        #    家长页看着像"每天复习了两三遍"(2026-10-27 用户就问了这个问题)。
        #    这里按 (时刻, 评分) 合并成一条,并列清"这一次记到了哪几个单元"。
        grouped: dict[tuple[str, int], dict[str, Any]] = {}
        for unit in problem.review_units:
            name = unit.knowledge_point or "整题"
            for item in self.review_history(unit.id, limit=20):
                key = (str(item.get("ts_utc") or ""), int(item.get("rating") or 0))
                row = grouped.get(key)
                if row is None:
                    row = dict(item, units=[])
                    grouped[key] = row
                row["units"].append({
                    "unit_id": unit.id,
                    "name": name,
                    "interval_days": item.get("interval_days"),
                    "state_before": item.get("state_before"),
                    "state_after": item.get("state_after"),
                })
        history = sorted(grouped.values(),
                         key=lambda item: str(item.get("ts_utc") or ""), reverse=True)
        for item in history:
            # 库里存的是 UTC;家长看到 "11:26" 会以为系统坏了(实际是 19:26)。
            item["ts_local"] = self._local_stamp(item.get("ts_utc"))
            # 各单元的间隔/状态可能不同(单独补记过某一天):不一致就逐个列,不硬凑一行。
            item["uniform"] = len({
                (unit["interval_days"], unit["state_before"], unit["state_after"])
                for unit in item["units"]
            }) == 1
        assets = []
        for asset_id in problem.asset_ids:
            try:
                asset = self.media.get(asset_id)
            except FileNotFoundError:
                continue
            assets.append(
                {"id": asset.id, "media_type": asset.media_type,
                 "byte_size": asset.byte_size, "original_name": asset.original_name}
            )
        raw = self.conn.execute(
            "SELECT * FROM app_problems WHERE id = ?", (int(problem_id),)
        ).fetchone()
        return {
            "problem": problem.model_dump(),
            "units": [unit.model_dump() for unit in problem.review_units],
            "history": history[:30],
            "assets": assets,
            # 录入时生成并绑定的图形与声音(家长要能在这儿核对,见 D107)
            "materials": self._question_assets(raw) if raw is not None else {},
            "schedule_note": self.schedule_note(),
        }

    # -- 声音:手输的题也要有(2026-10-26 真机 #14 报的 bug)-----------------
    def knowledge_point_text(self, problem_id: int) -> str:
        """这道题挂的知识点名,拼成一串给启发式当线索。读不到给空串(不抛)。"""
        try:
            rows = self.conn.execute(
                "SELECT k.name FROM app_problem_kps pk"
                " JOIN app_knowledge_points k ON k.id = pk.kp_id"
                " WHERE pk.problem_id = ?",
                (int(problem_id),),
            ).fetchall()
        except Exception:  # noqa: BLE001 - 读不到线索不该让录题失败
            return ""
        return " ".join(str(row["name"]) for row in rows)

    def bind_audio(
        self, problem_id: int, *, force: bool = False, tts_engine: Any = None,
        actor: str = "agent",
    ) -> dict[str, Any]:
        """给**没走过拍照识别**的听力/听写题补做并绑定声音。

        ⭐ 为什么需要它(2026-10-26 真机 #14):声音只在"识别 → 解析"那条流水线里生成
        (IntakeService.resolve_problem → _bind_audio),而聊天里说一句「录错题。语文听写
        涌现、结婚」、或网页上「手工录一道」,走的都是 add_problem —— 题进库了,
        audio_spec 一个字节都没有。于是家长端写"这题不需要声音",复习卡上没有播放器,
        当天合并听写里也没有这两个词(它按 audio_spec 取词)。

        这里的做法与修复脚本、识别那条路**同一段代码**:先纯字符串判断要不要出声、
        念什么(不调模型),再走 resolve_problem(probe=False, solve=False) 把声音做一次绑上。
        不需要出声的题(普通算术题)一步都不多做 —— plan 为 None 时立刻返回。
        """
        row = self.conn.execute(
            "SELECT * FROM app_problems WHERE id = ?", (int(problem_id),)
        ).fetchone()
        if row is None:
            raise StudyError("错题不存在: " + str(problem_id))
        plan = plan_from_problem(
            stem=row["stem_text"], answer=row["answer_text"],
            subject=str(row["subject"] or ""),
            extra_hints=self.knowledge_point_text(problem_id),
        )
        if plan is None:
            return {"problem_id": int(problem_id), "needed": False, "audio_artifact_id": None}
        from ..study.intake import IntakeService

        intake = IntakeService(
            self.conn, study=self, media=self.media, client_factory=None,
            tts_engine=tts_engine, now_provider=self._current_time,
        )
        # solve=False:只补声音,绝不碰答案(人手填的答案是最不可覆盖的东西)。
        intake.resolve_problem(int(problem_id), probe=False, solve=False, force=force, actor=actor)
        fresh = self.conn.execute(
            "SELECT audio_artifact_id, audio_spec FROM app_problems WHERE id = ?",
            (int(problem_id),),
        ).fetchone()
        spec = spec_of(fresh["audio_spec"] if fresh is not None else None)
        artifact_id = fresh["audio_artifact_id"] if fresh is not None else None
        note = describe_audio(spec) or plan.describe()
        return {
            "problem_id": int(problem_id),
            "needed": True,
            "audio_artifact_id": artifact_id,
            "audio_handle": ("/artifacts/" + str(artifact_id)) if artifact_id else None,
            "audio_label": str(spec.get("label") or plan.label),
            "audio_kind": str(spec.get("kind") or plan.kind),
            "audio_text": str(spec.get("text") or plan.text),
            "audio_seconds": float(spec.get("seconds") or 0),
            "audio_note": note,
            "audio_error": str(spec.get("error") or ""),
        }

    def _first_photo_bytes(self, row: Any) -> tuple[bytes, str]:
        """这道题挂的第一张照片(重裁图形要回到原图,不能拿裁好的小图再裁)。"""
        try:
            asset_ids = json.loads(row["asset_ids"] or "[]")
        except Exception:  # noqa: BLE001
            return b"", ""
        for asset_id in asset_ids:
            try:
                return self.media.read_bytes(int(asset_id)), ""
            except Exception:  # noqa: BLE001 - 换下一张
                continue
        return b"", ""

    def recrop_figure(
        self, problem_id: int, *, rotate_delta: int = 0, zoom: float = 0.0,
        actor: str = "user",
    ) -> dict[str, Any]:
        """按**已经存下来的框**重裁一次图形。**不调模型、不花钱、立刻生效。**

        ⭐ 为什么要有这个入口(2026-11-21):图形"歪了 / 切了一边 / 带进一句题干"是
        模型估框的常态,而以前唯一的补救是 `--resolve-one --force` —— 5–6 次模型调用、
        几十秒,还不一定对。人眼看一眼就知道该往哪转、该收一点还是放一点;
        把这一步交给人,比再问一次模型便宜得多,也更准。

        `rotate_delta`:**顺时针**再转多少度(↺ 就是 -90)。
        `zoom`:+ 放大(往外扩边距)、- 收窄(往中心收,用来挤掉混进来的题干文字)。
        """
        row = self.conn.execute(
            "SELECT * FROM app_problems WHERE id = ?", (int(problem_id),)
        ).fetchone()
        if row is None:
            raise StudyError("错题不存在: " + str(problem_id))
        try:
            spec = json.loads(row["figure_spec"] or "{}")
        except Exception:  # noqa: BLE001
            spec = {}
        probe = spec.get("probe") if isinstance(spec.get("probe"), dict) else {}
        box: Any = None
        try:
            box = json.loads(row["figure_bbox"] or "null")
        except Exception:  # noqa: BLE001
            box = None
        if not isinstance(box, list) or len(box) < 4:
            box = probe.get("bbox")
        if not isinstance(box, list) or len(box) < 4:
            raise StudyError("这道题没有存下来的图形框,没法重裁 —— 只能重新识别一次")
        photo, _media_type = self._first_photo_bytes(row)
        if not photo:
            raise StudyError("找不到这道题的原照片,没法重裁")
        if zoom > 0:
            box = list(expand_bbox(box, float(zoom)))
        elif zoom < 0:
            shrunk = shrink_bbox(box, -float(zoom))
            if shrunk is None:
                raise StudyError("已经收到很小了,再收就没了")
            box = list(shrunk)
        rotation = (int(spec.get("rotation_applied") or 0) + int(rotate_delta)) % 360
        # ⚠️ pad=0:存下来的框**已经含过边距**了,再加一次会越点越大(滚雪球)。
        #    要放宽就走 zoom(+12%) —— 一次只加一次,人看得见。
        crop = crop_figure(photo, box, rotate_degrees=rotation, pad=0.0)
        if not crop.ok:
            raise StudyError("重裁失败:" + (crop.error or "未知原因"))
        stored = self.media.store_bytes(
            crop.data, subdir="figures", media_type="image/png",
            original_name="figure-" + str(problem_id) + ".png",
        )
        spec["rotation_applied"] = rotation
        spec["needs_check"] = False
        spec["manual"] = {"rotate_delta": int(rotate_delta), "zoom": float(zoom)}
        spec["probe"] = dict(probe, bbox=[round(float(item), 4) for item in crop.bbox],
                             manual_at=now_iso())
        self.conn.execute(
            "UPDATE app_problems SET figure_asset_id = ?, figure_bbox = ?, figure_spec = ?,"
            " updated_at = ? WHERE id = ?",
            (stored.id, json.dumps(crop.bbox), json.dumps(spec, ensure_ascii=False),
             now_iso(), int(problem_id)),
        )
        self.conn.commit()
        if self._audit is not None:
            self._audit(actor=actor, action="study.figure.recrop",
                        target="problem:" + str(problem_id),
                        detail={"rotation": rotation, "zoom": float(zoom)})
            self.conn.commit()
        return {
            "problem_id": int(problem_id), "figure_asset_id": stored.id,
            "rotation": rotation, "width": crop.width, "height": crop.height,
        }

    def confirm_problem(self, problem_id: int) -> ProblemRead:
        self.get_problem(problem_id)
        self.conn.execute(
            "UPDATE app_problems SET status = 'confirmed', updated_at = ? WHERE id = ?",
            (now_iso(), problem_id),
        )
        self.conn.commit()
        return self.get_problem(problem_id)

    def _problem_from_row(self, row: sqlite3.Row) -> ProblemRead:
        keys = set(row.keys())
        points = [
            item["name"]
            for item in self.conn.execute(
                "SELECT k.name FROM app_problem_kps pk JOIN app_knowledge_points k ON k.id = pk.kp_id"
                " WHERE pk.problem_id = ? ORDER BY k.name",
                (row["id"],),
            ).fetchall()
        ]
        units = self.list_units(int(row["id"]))
        active = [unit for unit in units if not unit.suspended]
        today = self._current_time().date().isoformat()
        student_id = int(row["student_id"]) if "student_id" in keys and row["student_id"] else None
        student_name = None
        if student_id is not None:
            found = self.conn.execute(
                "SELECT name FROM app_students WHERE id = ?", (student_id,)
            ).fetchone()
            student_name = found["name"] if found is not None else None
        return ProblemRead(
            id=int(row["id"]),
            subject=row["subject"],
            grade=row["grade"],
            source=row["source"] if "source" in keys else None,
            stem_text=row["stem_text"],
            answer_text=row["answer_text"],
            explanation=row["explanation"],
            status=row["status"],
            student_id=student_id,
            student_name=student_name,
            knowledge_points=points,
            asset_ids=[int(item) for item in json.loads(row["asset_ids"] or "[]")],
            # 📷 可直接粘进页面的照片句柄(列表页缩略图用它)。
            #    ⚠️ 以前只有 problem_detail / render_problem_card 才给,列表行拿不到 ——
            #       错题本因此"有照片的题也不显示缩略图"(2026-11-09 重做时发现)。
            photo_handles=["/assets/" + str(item) for item in json.loads(row["asset_ids"] or "[]")],
            review_units=units,
            # 下一次该复习的日子:所有**未暂停**单元里最早的那个
            next_due_local=min((unit.due_date_local for unit in active), default=None),
            due_count=len([unit for unit in active if unit.due_date_local <= today]),
            reps=sum(unit.reps for unit in units),
            lapses=sum(unit.lapses for unit in units),
            # 🔊 录进来时就绑好的音频(听力/听写题)。给句柄而不是给字节 ——
            #    调用方(模型/网页)要的是"拿去用",不是"再合成一遍"。
            audio_handle=(
                "/artifacts/" + str(row["audio_artifact_id"])
                if "audio_artifact_id" in keys and row["audio_artifact_id"] else None
            ),
            audio_label=str(
                (spec_of(row["audio_spec"] if "audio_spec" in keys else None) or {})
                .get("label") or ""
            ),
            audio_purpose=str(
                (spec_of(row["audio_spec"] if "audio_spec" in keys else None) or {})
                .get("purpose") or ""
            ),
            created_at=row["created_at"],
        )

    # -- 复习单元 ---------------------------------------------------------
    def ensure_review_units(self, problem_id: int) -> list[ReviewUnitRead]:
        """为一道题建齐复习单元:ord=0 是原题,之后每个知识点各一个。

        ⚠️ 新建单元**不要立刻到期**(FSRS 落地的五个坑之三):首次到期设在明天,
        否则一次上传 20 道题会立刻被 20 道题轰炸。

        ⚠️ 单元的身份是 `kp_id`,**不是 ord 的序号**。早期版本用"第几个知识点 ->
        ord 就是几"来建,于是给一道题补一个 kp_id 更小的知识点时,ord 会整体错位 ——
        孩子复习的卡片会悄悄换成另一个知识点。这里改成按 kp_id 对齐,ord 只当排序用。
        """
        units = self.list_units(problem_id)
        if UNIT_ORD_BASE not in {unit.ord for unit in units}:
            self._create_unit(problem_id, UNIT_ORD_BASE, kp_id=None, commit=False)
            units = self.list_units(problem_id)
        known = {unit.kp_id for unit in units if unit.kp_id is not None}
        next_ord = max([unit.ord for unit in units], default=UNIT_ORD_BASE) + 1
        rows = self.conn.execute(
            "SELECT kp_id FROM app_problem_kps WHERE problem_id = ? ORDER BY kp_id",
            (problem_id,),
        ).fetchall()
        for item in rows:
            kp_id = int(item["kp_id"])
            if kp_id in known:
                continue
            self._create_unit(problem_id, next_ord, kp_id=kp_id, commit=False)
            next_ord += 1
        return self.list_units(problem_id)

    def _create_unit(self, problem_id: int, ord_value: int, *, kp_id: int | None,
                     commit: bool = True) -> int:
        now = self._current_time()
        due_local = (now + timedelta(days=FIRST_DUE_DAYS)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        owner_row = self.conn.execute(
            "SELECT owner_id FROM app_problems WHERE id = ?", (problem_id,)
        ).fetchone()
        owner = owner_row["owner_id"] if owner_row is not None else None
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO app_review_units(problem_id, ord, kp_id, fsrs_state,"
            " due_utc, due_date_local, created_at, updated_at, owner_id)"
            " VALUES (?, ?, ?, 'new', ?, ?, ?, ?, ?)",
            (
                problem_id, ord_value, kp_id,
                due_local.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds"),
                due_local.date().isoformat(),
                now_iso(), now_iso(), owner,
            ),
        )
        if commit:
            self.conn.commit()
        return int(cursor.lastrowid or 0)

    def list_units(self, problem_id: int) -> list[ReviewUnitRead]:
        clause, params = self._owner_clause("u.owner_id")
        rows = self.conn.execute(
            "SELECT u.*, k.name AS kp_name FROM app_review_units u"
            " LEFT JOIN app_knowledge_points k ON k.id = u.kp_id"
            " WHERE u.problem_id = ? AND " + clause + " ORDER BY u.ord",
            (problem_id, *params),
        ).fetchall()
        return [
            ReviewUnitRead(
                id=int(row["id"]),
                problem_id=int(row["problem_id"]),
                ord=int(row["ord"]),
                kp_id=int(row["kp_id"]) if row["kp_id"] is not None else None,
                knowledge_point=row["kp_name"],
                fsrs_state=row["fsrs_state"],
                reps=int(row["reps"]),
                lapses=int(row["lapses"]),
                due_date_local=row["due_date_local"],
                suspended=int(row["suspended"] or 0),
            )
            for row in rows
        ]

    # -- 双视角 -----------------------------------------------------------
    def _local_stamp(self, raw: Any) -> str:
        """UTC ISO 串 → 本地时间 "2026-10-27 19:26"(读不出来就原样返回)。"""
        text = str(raw or "")
        if not text:
            return ""
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return text
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo("UTC"))
        return moment.astimezone(ZoneInfo(self.tzid)).strftime("%Y-%m-%d %H:%M")

    def _question_assets(self, row: Any) -> dict[str, Any]:
        """题目**自带的两样素材**:图形(裁剪/矢量重绘)与声音(听力/听写的音频)。

        ⭐ 两样都是**录入那一刻**就做好并绑在题上的(见 D104 / D107),所以这里只从库里读,
        不重新生成。孩子端也要看到它们 —— 听力题没有声音就不是那道题,图形题没有图也不是。
        """
        try:
            figure = json.loads(row["figure_spec"] or "{}")
        except Exception:  # noqa: BLE001
            figure = {}
        audio = spec_of(row["audio_spec"])
        # ⚠️ 手输的题(聊天里"录错题"、网页"手工录一道")**从来没走过识别**,于是
        #    audio_spec 是空的;模板以前一律写"这题不需要声音"—— 实测 #14 一道语文
        #    听写题就被这句话盖掉了。这里如实算一句:这题**看着要不要出声**。
        pending_plan = None
        if not audio and not row["audio_artifact_id"]:
            pending_plan = plan_from_problem(
                stem=row["stem_text"], answer=row["answer_text"],
                subject=str(row["subject"] or ""),
                extra_hints=self.knowledge_point_text(int(row["id"])),
            )
        asset_id = row["figure_asset_id"]
        try:
            photos = [int(item) for item in json.loads(row["asset_ids"] or "[]")]
        except Exception:  # noqa: BLE001
            photos = []
        return {
            # 📷 可**直接粘进页面**的句柄:内联器认 /assets/<素材 id>(见 D110)。
            #    ⚠️ 只给整数 id 是不够的 —— 模型没法把它变成引用(实测就是这么漏掉图与声音的)。
            "photo_handles": ["/assets/" + str(item) for item in photos],
            "figure_handle": ("/assets/" + str(asset_id)) if asset_id else None,
            # 图形:矢量重绘优先(SVG 源码,模板里直接内联),裁剪图永远留着当证据
            "figure_svg": str(figure.get("svg") or ""),
            "figure_asset_id": int(asset_id) if asset_id else None,
            "figure_source": str(row["figure_source"] or ""),
            "figure_kind": str(figure.get("kind") or ""),
            "figure_needs_check": bool(figure.get("needs_check")),
            "figure_rotation": int(figure.get("rotation_applied") or 0),
            "figure_description": str(figure.get("description") or ""),
            # 声音:句柄给下游直接用,别让它再合成一遍(重做会得到另一段声音)
            "audio_artifact_id": row["audio_artifact_id"],
            "audio_handle": ("/artifacts/" + str(row["audio_artifact_id"])
                             if row["audio_artifact_id"] else None),
            "audio_label": str(audio.get("label") or ""),
            "audio_purpose": str(audio.get("purpose") or ""),
            "audio_note": describe_audio(audio),
            "audio_error": str(audio.get("error") or ""),
            "audio_seconds": float(audio.get("seconds") or 0),
            # 给家长核对用的几项:要念的原文 / 音色 / 谁合成的 / 花了多少
            "audio_text": str(audio.get("text") or ""),
            "audio_voice": str(audio.get("voice") or ""),
            "audio_provider": str(audio.get("provider") or ""),
            "audio_spent_yuan": float(audio.get("spent_yuan") or 0),
            # 没生成音频时,"这题要不要出声"的诚实回答(空串 = 真的不需要)。
            # fixable = 题干/答案里读得出要念什么,补做一次就有声了。
            "audio_hint": (
                "" if (audio or row["audio_artifact_id"] or pending_plan is None)
                else pending_plan.describe()
            ),
            "audio_fixable": bool(pending_plan is not None and pending_plan.ok),
        }

    def render_card(self, unit_id: int, *, perspective: str = "child") -> dict[str, Any]:
        """一张复习卡(**按单元**)。

        ⚠️ 孩子端**只有**题干 + 图片 + 两个按钮;家长端才有知识点、错因、复习状态。
        这条边界是能力域 §6.5 明确划的:不把家长视角下的评价暴露给孩子。

        ⭐ 孩子端的清单请用 `render_problem_card`(一道题一张卡):单元是**记账单位**,
        不是"孩子要做几遍"的单位 —— 见 D108。
        """
        if perspective not in ("child", "parent"):
            raise StudyError("视角只能是 child 或 parent")
        row = self.conn.execute(
            "SELECT * FROM app_review_units WHERE id = ?", (unit_id,)
        ).fetchone()
        if row is None:
            raise StudyError("复习单元不存在: " + str(unit_id))
        card = self.render_problem_card(
            int(row["problem_id"]), unit_ids=[int(unit_id)], perspective=perspective
        )
        card["unit_id"] = int(unit_id)
        card["due_date_local"] = row["due_date_local"]
        if perspective == "parent":
            # 单元自己的那几项(按单元的弱点统计,不是整题的)
            card["fsrs_state"] = row["fsrs_state"]
            card["reps"] = int(row["reps"])
            card["lapses"] = int(row["lapses"])
            kp = self.conn.execute(
                "SELECT name FROM app_knowledge_points WHERE id = ?", (row["kp_id"],)
            ).fetchone() if row["kp_id"] else None
            card["knowledge_point"] = kp["name"] if kp else None
        return card

    def render_problem_card(
        self, problem_id: int, *, unit_ids: Sequence[int] | None = None,
        perspective: str = "child",
    ) -> dict[str, Any]:
        """一道题的复习卡。⭐ **孩子端一道题只有一张卡**(D108)。

        用户 2026-10-13 报的:"今日复习里,怎么有重复的" —— 一道题有几个知识点就有几个
        复习单元,而它们的首次到期是**同一天**,于是同一道题连着出现三四遍(实测 #1 三张)。
        单元是**记账单位**(为了分清他卡在「通分」还是「约分」),不是"要做几遍"的单位。
        """
        if perspective not in ("child", "parent"):
            raise StudyError("视角只能是 child 或 parent")
        self.get_problem(problem_id)
        row = self.conn.execute("SELECT * FROM app_problems WHERE id = ?",
                                (int(problem_id),)).fetchone()
        if row is None:
            raise StudyError("错题不存在: " + str(problem_id))
        ids = [int(value) for value in (unit_ids or [])]
        if not ids:
            ids = [int(item["id"]) for item in self.conn.execute(
                "SELECT id FROM app_review_units WHERE problem_id = ? AND suspended = 0"
                " ORDER BY ord", (int(problem_id),)).fetchall()]
        points = [str(item["name"]) for item in self.conn.execute(
            "SELECT k.name FROM app_problem_kps pk JOIN app_knowledge_points k ON k.id = pk.kp_id"
            " WHERE pk.problem_id = ? ORDER BY k.name", (int(problem_id),)).fetchall()]
        card: dict[str, Any] = {
            # ⚠️ unit_id 保留(单数):老调用方(工具/测试)还在按它记账
            "unit_id": ids[0] if ids else None,
            "unit_ids": ids,
            "problem_id": int(problem_id),
            "perspective": perspective,
            "subject": row["subject"],
            "stem_text": row["stem_text"],
            "asset_ids": [int(item) for item in json.loads(row["asset_ids"] or "[]")],
            "knowledge_points": points,
        }
        card.update(self._question_assets(row))
        if perspective == "child":
            card["buttons"] = [{"label": "忘了", "rating": 1}, {"label": "会了", "rating": 3}]
            card["guidance"] = "先自己想一想,想不出来就点「忘了」——没有关系。"
            if len(ids) > 1:
                # 说清楚"这一下会记到几个单元" —— 不然家长会以为漏记了
                card["guidance"] += "(这题今天有 " + str(len(ids)) + " 个单元到期,一次作答一起记。)"
            return card
        card.update({
            "answer_text": row["answer_text"],
            "explanation": row["explanation"],
        })
        return card

    # -- 统计 -------------------------------------------------------------
    def stats(self, *, today: str | None = None) -> StudyStats:
        p_clause, p_params = self._owner_clause("owner_id")
        total = int(
            self.conn.execute(
                "SELECT count(*) FROM app_problems WHERE " + p_clause, p_params
            ).fetchone()[0]
        )
        by_subject = {
            row["subject"]: int(row["n"])
            for row in self.conn.execute(
                "SELECT subject, count(*) AS n FROM app_problems WHERE " + p_clause
                + " GROUP BY subject ORDER BY n DESC",
                p_params,
            ).fetchall()
        }
        u_clause, u_params = self._owner_clause("u.owner_id")
        weak = [
            WeakPointRow(
                knowledge_point=row["name"],
                problem_count=int(row["problems"]),
                unit_count=int(row["units"]),
                lapses=int(row["lapses"] or 0),
            )
            for row in self.conn.execute(
                "SELECT k.name,"
                " (SELECT count(*) FROM app_problem_kps pk"
                "   JOIN app_problems p ON p.id = pk.problem_id"
                "   WHERE pk.kp_id = k.id AND " + self._owner_clause("p.owner_id")[0] + ") AS problems,"
                " (SELECT count(*) FROM app_review_units u WHERE u.kp_id = k.id AND " + u_clause + ") AS units,"
                " (SELECT COALESCE(SUM(u.lapses), 0) FROM app_review_units u"
                "   WHERE u.kp_id = k.id AND " + u_clause + ") AS lapses"
                " FROM app_knowledge_points k ORDER BY lapses DESC, problems DESC LIMIT 20",
                [*self._owner_clause("p.owner_id")[1], *u_params, *u_params],
            ).fetchall()
        ]
        day = today or self._current_time().date().isoformat()
        due_clause, due_params = self._owner_clause("p.owner_id")
        due = int(
            self.conn.execute(
                "SELECT count(*) FROM app_review_units u"
                " JOIN app_problems p ON p.id = u.problem_id"
                " WHERE u.suspended = 0 AND u.due_date_local <= ? AND " + due_clause,
                (day, *due_params),
            ).fetchone()[0]
        )
        next_clause, next_params = self._owner_clause("p.owner_id")
        next_due = self.conn.execute(
            "SELECT MIN(u.due_date_local) FROM app_review_units u"
            " JOIN app_problems p ON p.id = u.problem_id"
            " WHERE u.suspended = 0 AND p.status <> 'archived'"
            " AND u.due_date_local > ? AND " + next_clause,
            (day, *next_params),
        ).fetchone()
        students = int(
            self.conn.execute(
                "SELECT count(*) FROM app_students WHERE archived = 0"
            ).fetchone()[0]
        )
        return StudyStats(
            total_problems=total, by_subject=by_subject, weak_points=weak, due_today=due,
            next_due_local=next_due[0] if next_due and next_due[0] else None,
            students=students,
            engine=str(getattr(self.policy, "name", "fixed")),
            schedule_note=self.schedule_note(),
        )

    def due_units(self, *, today: str | None = None, limit: int = 50) -> list[ReviewUnitRead]:
        day = today or self._current_time().date().isoformat()
        # ⚠️ 归档的错题不进复习队列:回执上的"撤销 = 归档"要真的撤销掉,
        #    否则孩子明天还会看到那道"我刚录错的题"。
        clause, params = self._owner_clause("p.owner_id")
        rows = self.conn.execute(
            "SELECT u.id FROM app_review_units u JOIN app_problems p ON p.id = u.problem_id"
            " WHERE u.suspended = 0 AND p.status <> 'archived'"
            " AND u.due_date_local <= ? AND " + clause
            + " ORDER BY u.due_date_local, u.id LIMIT ?",
            (day, *params, limit),
        ).fetchall()
        units: list[ReviewUnitRead] = []
        for row in rows:
            unit = self.conn.execute(
                "SELECT problem_id FROM app_review_units WHERE id = ?", (row["id"],)
            ).fetchone()
            units.extend(
                item for item in self.list_units(int(unit["problem_id"])) if item.id == row["id"]
            )
        return units


    # -- 复习排程 ---------------------------------------------------------
    def set_policy(self, policy: Any) -> None:
        self.policy = policy

    def _unit_state(self, row: sqlite3.Row) -> Any:
        from ..study.scheduler import UnitState

        last = self.conn.execute(
            "SELECT interval_days FROM app_review_logs WHERE unit_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        return UnitState(
            stability=float(row["stability"] or 0.0),
            difficulty=float(row["difficulty"] or 0.0),
            state=row["fsrs_state"],
            reps=int(row["reps"]),
            lapses=int(row["lapses"]),
            last_interval_days=float(last["interval_days"]) if last and last["interval_days"] else None,
            last_review_utc=row["last_review_utc"],
            due_utc=row["due_utc"],
        )

    # -- 撤销(由统一回执调用)--------------------------------------------
    def revert_problem(self, receipt: Receipt) -> str:
        plan = receipt.undo_plan or {}
        problem_id = plan.get("problem_id")
        if not problem_id:
            raise ReceiptError("这条回执没有错题号,撤不了")
        # ⚠️ 那道题可能已经被**彻底删除**了(D177):这时不能再回一句"已归档" ——
        #    撤销的是一个不存在的行,用户会以为它还在。
        exists = self.conn.execute(
            "SELECT 1 FROM app_problems WHERE id = ?", (int(problem_id),)
        ).fetchone()
        if exists is None:
            raise ReceiptError("那道题已经彻底删除了,这条回执没有可撤销的东西")
        self.conn.execute(
            "UPDATE app_problems SET status = 'archived', updated_at = ? WHERE id = ?",
            (now_iso(), int(problem_id)),
        )
        self.conn.commit()
        return "已归档刚录的错题 #" + str(problem_id) + "(复习记录保留,统计不被破坏)"

    def revert_aliases(self, receipt: Receipt) -> str:
        plan = receipt.undo_plan or {}
        aliases = [str(item) for item in plan.get("aliases") or []]
        kp_id = plan.get("kp_id")
        if not aliases or not kp_id:
            raise ReceiptError("这条回执没有别名信息,撤不了")
        removed = 0
        for alias in aliases:
            cursor = self.conn.execute(
                "DELETE FROM app_kp_aliases WHERE alias = ? AND kp_id = ?", (alias, int(kp_id))
            )
            removed += int(cursor.rowcount or 0)
        self.conn.commit()
        return "已解绑 " + str(removed) + " 个别名" + ("(知识点本身保留)" if removed else "")

    def _apply_unit_review(
        self, unit_id: int, rating: int, *, elapsed_ms: int | None = None,
        source: str = "review",
    ) -> dict[str, Any]:
        """把一次评分落到**一个单元**上:改 FSRS 状态 + 追加一条复习日志。

        ⭐ 抽出来的理由(用户 2026-10-13 报的"今日复习里怎么有重复的"):一道题有多个单元,
        一次作答要同时记到几个单元上,而**回执只能有一条** —— 点一下"会了"就在回执托盘里
        冒出三条,是另一种形式的重复。发件箱/审计/回执由调用方写,这里只管那两行数据。
        """
        from ..study.scheduler import UnitState  # noqa: F401

        clause, params = self._owner_clause("p.owner_id")
        row = self.conn.execute(
            "SELECT u.* FROM app_review_units u"
            " JOIN app_problems p ON p.id = u.problem_id"
            " WHERE u.id = ? AND " + clause,
            (int(unit_id), *params),
        ).fetchone()
        if row is None:
            raise StudyError("复习单元不存在: " + str(unit_id))
        now = self._current_time()
        state = self._unit_state(row)
        result = self.policy.review(state, int(rating), now)
        self.conn.execute(
            "UPDATE app_review_units SET stability = ?, difficulty = ?, fsrs_state = ?,"
            " reps = ?, lapses = ?, last_review_utc = ?, due_utc = ?, due_date_local = ?,"
            " updated_at = ? WHERE id = ?",
            (
                result.stability, result.difficulty, result.state, result.reps, result.lapses,
                now.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds"),
                result.due_utc, result.due_date_local, now_iso(), int(unit_id),
            ),
        )
        cursor = self.conn.execute(
            "INSERT INTO app_review_logs(unit_id, problem_id, ts_utc, rating, interval_days,"
            " last_interval_days, elapsed_ms, state_before, state_after, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(unit_id), int(row["problem_id"]),
                now.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds"),
                int(rating), result.interval_days, state.last_interval_days,
                elapsed_ms, state.state, result.state, source,
            ),
        )
        return {"result": result, "row": row, "log_id": int(cursor.lastrowid or 0),
                "state_before": state}

    def _review_before(self, row: Any) -> dict[str, Any]:
        """复习前的单元状态(撤销要用,必须每次落库都存全)。"""
        return {
            "stability": float(row["stability"]),
            "difficulty": float(row["difficulty"]),
            "fsrs_state": row["fsrs_state"],
            "reps": int(row["reps"]),
            "lapses": int(row["lapses"]),
            "last_review_utc": row["last_review_utc"],
            "due_utc": row["due_utc"],
            "due_date_local": row["due_date_local"],
        }

    def record_problem_review(
        self,
        problem_id: int,
        rating: int,
        *,
        today: str | None = None,
        elapsed_ms: int | None = None,
        source: str = "review",
        actor: str = "user",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """一次作答记到这道题**今天到期的那些单元**上(D108)。

        ⭐ 为什么一起记:孩子看到的是一道题、作答的也是那一次 —— 把同一道题的三个单元
        分开打分,等于把**一次表现当成三次证据**。单元仍然是独立记账的(FSRS 状态各走各的),
        只是这一次证据同时喂给它们。
        """
        # ⚠️ 直接查这道题今天到期的单元(2026-10-18 审计修):以前是先取
        #    due_problem_groups(limit=200) 再在里面找 —— 到期题超过 200 道之后,
        #    第 201 道之后的作答会静默记 0 个单元(界面还显示成功,等于这次学习没发生)。
        day = today or self._current_time().date().isoformat()
        clause, owner_params = self._owner_clause("p.owner_id")
        rows = self.conn.execute(
            "SELECT u.id FROM app_review_units u"
            " JOIN app_problems p ON p.id = u.problem_id"
            " WHERE u.problem_id = ? AND u.suspended = 0 AND p.status <> 'archived'"
            " AND u.due_date_local <= ? AND " + clause
            + " ORDER BY u.ord, u.id",
            (int(problem_id), day, *owner_params),
        ).fetchall()
        unit_ids = [int(row["id"]) for row in rows]
        if not unit_ids:
            # 没有到期的单元:可能是重复提交,也可能是它已经复习过了 —— 不报错,如实说 0 个
            return {"problem_id": int(problem_id), "units": 0, "log_ids": [],
                    "due_date_local": None, "rating": int(rating)}
        applied: list[dict[str, Any]] = []
        for unit_id in unit_ids:
            info = self._apply_unit_review(unit_id, rating, elapsed_ms=elapsed_ms, source=source)
            applied.append({
                "unit_id": int(unit_id),
                "log_id": info["log_id"],
                "before": self._review_before(info["row"]),
                "result": info["result"],
            })
        last = applied[-1]["result"]
        # ⚠️ 一道题多个复习单元的到期日可能分叉:回执/界面上的“下次复习”
        #    必须取**最早**的那个(2026-10-18 审计),否则会报一个偏晚的日期,漏掉
        #    明天就该复习的单元。
        due_dates = [str(item["result"].due_date_local)
                     for item in applied
                     if getattr(item["result"], "due_date_local", None)]
        next_due = min(due_dates) if due_dates else last.due_date_local
        if self.receipts is not None:
            self.receipts.record(
                kind="study.review",
                domain=DOMAIN_STUDY,
                label=("记复习:" + str(int(rating)) + " 分(错题 #" + str(int(problem_id))
                       + (" 的 " + str(len(applied)) + " 个单元)" if len(applied) > 1 else ")")),
                detail={"problem_id": int(problem_id), "rating": int(rating),
                        "unit_ids": [item["unit_id"] for item in applied],
                        "log_ids": [item["log_id"] for item in applied],
                        "due_date_local": next_due},
                undo_plan={
                    "type": "review_batch",
                    "items": [{"unit_id": item["unit_id"], "log_id": item["log_id"],
                               "before": item["before"]} for item in applied],
                },
                actor=actor,
                session_id=session_id,
            )
        self.conn.commit()
        for item in applied:
            self._emit(
                "study.review.recorded",
                {"unit_id": item["unit_id"], "problem_id": int(problem_id),
                 "rating": int(rating), "interval_days": round(item["result"].interval_days, 3),
                 "state": item["result"].state},
                key="study.review.recorded:" + str(item["log_id"]),
            )
        if self._audit is not None:
            self._audit(
                actor=actor, action="study.review.record",
                target="problem:" + str(int(problem_id)),
                detail={"rating": int(rating), "units": len(applied),
                        "due_date_local": next_due},
                session_id=session_id,
            )
            self.conn.commit()
        return {
            "problem_id": int(problem_id), "units": len(applied),
            "unit_ids": [item["unit_id"] for item in applied],
            "log_ids": [item["log_id"] for item in applied],
            "due_date_local": next_due, "rating": int(rating),
        }

    def record_review(
        self,
        unit_id: int,
        rating: int,
        *,
        elapsed_ms: int | None = None,
        source: str = "review",
        actor: str = "user",
        session_id: str | None = None,
    ) -> Any:
        """记录一次复习。

        ⚠️ 复习**日志只增不改** —— 它是将来训练个人 FSRS 参数的唯一依据(需约 1000 条),
        而且 `last_interval_days` 与 `interval_days` 的对比就是"这次进步了没"。
        这些字段第一天就要存全,以后补不回来。
        """
        info = self._apply_unit_review(unit_id, rating, elapsed_ms=elapsed_ms, source=source)
        result, row, log_cursor, state = info["result"], info["row"], info["log_id"], info["state_before"]
        if self.receipts is not None:
            self.receipts.record(
                kind="study.review",
                domain=DOMAIN_STUDY,
                label="记复习:" + str(rating) + " 分(单元 #" + str(unit_id) + ")",
                detail={"unit_id": unit_id, "rating": int(rating),
                        "log_id": int(log_cursor),
                        "from_state": state.state, "to_state": result.state,
                        "due_date_local": result.due_date_local},
                # 撤销 = 单元状态回到复习前 + 删掉这条复习日志。
                # ⚠️ 日志是训练 FSRS 的依据,所以只删"刚写的那一条",不多删一笔。
                undo_plan={
                    "type": "review",
                    "unit_id": unit_id,
                    "log_id": int(log_cursor),
                    "before": {
                        "stability": float(row["stability"]),
                        "difficulty": float(row["difficulty"]),
                        "fsrs_state": row["fsrs_state"],
                        "reps": int(row["reps"]),
                        "lapses": int(row["lapses"]),
                        "last_review_utc": row["last_review_utc"],
                        "due_utc": row["due_utc"],
                        "due_date_local": row["due_date_local"],
                    },
                },
                actor=actor,
                session_id=session_id,
            )
        self.conn.commit()
        self._emit(
            "study.review.recorded",
            {"unit_id": unit_id, "problem_id": int(row["problem_id"]), "rating": int(rating),
             "interval_days": round(result.interval_days, 3), "state": result.state},
            # 用**复习日志行的 id** 做幂等键:它是这次复习的自然身份,
            # 而时间戳在固定时钟下会撞(测试里就是固定时钟)。
            key="study.review.recorded:" + str(log_cursor),
        )
        if self._audit is not None:
            self._audit(
                actor=actor, action="study.review.record", target="unit:" + str(unit_id),
                detail={"rating": int(rating), "interval_days": round(result.interval_days, 3),
                        "state": result.state},
                session_id=session_id,
            )
            self.conn.commit()
        return result

    def _restore_unit(self, unit_id: Any, before: dict[str, Any]) -> None:
        """把一个单元退回复习前的状态(不动日志)。"""
        self.conn.execute(
            "UPDATE app_review_units SET stability = ?, difficulty = ?, fsrs_state = ?,"
            " reps = ?, lapses = ?, last_review_utc = ?, due_utc = ?, due_date_local = ?,"
            " updated_at = ? WHERE id = ?",
            (
                float(before.get("stability") or 0.0),
                float(before.get("difficulty") or 0.0),
                str(before.get("fsrs_state") or "new"),
                int(before.get("reps") or 0),
                int(before.get("lapses") or 0),
                before.get("last_review_utc"),
                str(before.get("due_utc") or ""),
                str(before.get("due_date_local") or ""),
                now_iso(),
                int(unit_id),
            ),
        )

    def revert_review(self, receipt: Receipt) -> str:
        """撤销一次复习。⭐⭐ 两种回执都要认:
        `review`(单单元,老回执)与 `review_batch`(一道题的几个单元一起记,见 D108)——
        只认前者的后果是"点一次会了写了 3 个单元,撤销只退掉 1 个"。"""
        plan = receipt.undo_plan or {}
        items = list(plan.get("items") or [])
        if plan.get("unit_id") and plan.get("before"):
            items.append({"unit_id": plan["unit_id"], "log_id": plan.get("log_id"),
                          "before": plan["before"]})
        if not items:
            raise ReceiptError("这条复习回执缺少原状态,撤不了")
        for item in items:
            before = item.get("before") or {}
            if not item.get("unit_id") or not before:
                raise ReceiptError("这条复习回执缺少原状态,撤不了")
            self._restore_unit(item["unit_id"], before)
            log_id = item.get("log_id")
            if log_id:
                # ⚠️ 日志是训练 FSRS 的依据:只删"刚写的那几条",不多删一笔
                self.conn.execute("DELETE FROM app_review_logs WHERE id = ?", (int(log_id),))
        self.conn.commit()
        if len(items) == 1:
            return "已把单元 #" + str(items[0]["unit_id"]) + " 的复习进度退回复习前"
        return "已把 " + str(len(items)) + " 个单元的复习进度退回复习前"

    def due_problem_groups(
        self, *, today: str | None = None, limit: int = 20, subject: str | None = None
    ) -> list[dict[str, Any]]:
        """今天到期的题,**按题归并**(每道题带上它今天到期的单元 id)。

        ⭐ 为什么必须归并(用户 2026-10-13 报的"今日复习里怎么有重复的"):
           一道题有几个知识点就有几个单元,而它们的首次到期是**同一天** ——
           不归并的话孩子端会看到同一道题连着出现三四遍(实测 #1 一次 3 张)。
        """
        day = today or self._current_time().date().isoformat()
        sql = (
            "SELECT u.id AS unit_id, u.problem_id, u.due_date_local, u.ord"
            " FROM app_review_units u JOIN app_problems p ON p.id = u.problem_id"
            " WHERE u.suspended = 0 AND p.status <> 'archived' AND u.due_date_local <= ?"
        )
        clause, owner_params = self._owner_clause("p.owner_id")
        sql += " AND " + clause
        params: list[Any] = [day, *owner_params]
        if subject:
            sql += " AND p.subject = ?"
            params.append(subject)
        sql += " ORDER BY u.due_date_local, u.problem_id, u.ord LIMIT ?"
        # ⚠️ 多取一些:一个单元一行,归并之后条数会变少 —— 按 limit 取会把题数截短
        params.append(max(1, int(limit)) * 6)
        groups: dict[int, dict[str, Any]] = {}
        for row in self.conn.execute(sql, tuple(params)).fetchall():
            problem_id = int(row["problem_id"])
            group = groups.setdefault(problem_id, {
                "problem_id": problem_id, "unit_ids": [],
                "due_date_local": row["due_date_local"],
            })
            group["unit_ids"].append(int(row["unit_id"]))
        return list(groups.values())[:max(1, int(limit))]

    def review_queue(
        self, *, subject: str | None = None, limit: int = 20, today: str | None = None
    ) -> list[dict[str, Any]]:
        """今天该复习的清单(孩子端)。

        ⭐ **一道题一张卡**(D108):单元是记账单位,不是"孩子要做几遍"的单位。
        默认一天只看一次,复习过的不在同一天的清单里再出现。
        """
        return [
            self.render_problem_card(
                item["problem_id"], unit_ids=item["unit_ids"], perspective="child"
            )
            for item in self.due_problem_groups(today=today, limit=limit, subject=subject)
        ]

    # -- 每日推送 ---------------------------------------------------------
    def schedule_note(self) -> str:
        """把"复习周期是怎么排的"说成人话 —— 界面上要看得见依据,而不是一个黑箱日期。"""
        engine = str(getattr(self.policy, "name", "fixed"))
        if engine == "fsrs":
            return (
                "当前引擎:FSRS-6(按「还能记住的概率 ≈ 90%」反推下次日期)。"
                "单次间隔上限 180 天,保证一学年内至少再见一次;答错回到重学。"
            )
        return (
            "当前引擎:固定间隔 1 → 3 → 7 → 15 → 30 天。答错回到第 1 档。"
            "先用它验证孩子愿不愿意用,再换成 FSRS(改 LA_SRS_ENGINE=fsrs 即可)。"
        )

    def due_items(self, *, today: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """今天该复习的条目(带学科/题干/知识点)—— 推送文本与界面共用同一份口径。"""
        day = today or self._current_time().date().isoformat()
        clause, params = self._owner_clause("p.owner_id")
        rows = self.conn.execute(
            "SELECT u.id AS unit_id, u.problem_id, u.ord, u.due_date_local, u.lapses, u.reps,"
            " p.subject, p.stem_text, p.asset_ids, k.name AS kp_name"
            " FROM app_review_units u JOIN app_problems p ON p.id = u.problem_id"
            " LEFT JOIN app_knowledge_points k ON k.id = u.kp_id"
            " WHERE u.suspended = 0 AND p.status <> 'archived'"
            " AND u.due_date_local <= ? AND " + clause
            + " ORDER BY u.due_date_local, u.problem_id, u.ord LIMIT ?",
            (day, *params, int(limit)),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            items.append(
                {
                    "unit_id": int(row["unit_id"]),
                    "problem_id": int(row["problem_id"]),
                    "ord": int(row["ord"]),
                    "subject": row["subject"],
                    "stem_text": row["stem_text"],
                    "knowledge_point": row["kp_name"],
                    "due_date_local": row["due_date_local"],
                    "lapses": int(row["lapses"] or 0),
                    "reps": int(row["reps"] or 0),
                    "asset_count": len(json.loads(row["asset_ids"] or "[]")),
                }
            )
        return items

    def merged_audio_today(
        self, *, today: str | None = None, build: bool = True
    ) -> list[dict[str, Any]]:
        """今天的听写/跟读题**合并成按(怎么念, 语言)分的几段**(用户 2026-10-24 / 11-09)。

        语文、英语各一段,听写与跟读各一段;同一段里按出题顺序拼、去重。
        `build=False` 只分组不出声(测试与预览用)。
        """
        day = today or self._current_time().date().isoformat()
        try:
            items = self.due_problem_items(today=day, limit=200)
        except Exception:  # noqa: BLE001 - 页面不该因为读不到复习队列就 500
            return []
        ids = sorted({int(item["problem_id"]) for item in items})
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        try:
            rows = self.conn.execute(
                "SELECT id, subject, audio_spec FROM app_problems WHERE id IN (" + marks + ")",
                tuple(ids),
            ).fetchall()
        except Exception:  # noqa: BLE001
            return []
        entries = [
            {
                "problem_id": int(row["id"]),
                "subject": row["subject"],
                "spec": spec_of(row["audio_spec"]),
            }
            for row in rows
        ]
        groups = merge_audio_specs(entries)
        if not build:
            return groups
        for group in groups:
            group.update(self._merged_audio(day, group))
        return groups

    def _merged_audio(
        self, day: str, group: dict[str, Any]
    ) -> dict[str, Any]:
        """合并听写/跟读只做一次:按 (日期, 怎么念, 语言, 内容) 缓存,内容变了才重做。

        ⚠️ 缓存落在 sys_settings,不另建表 —— 它是一份**可重建**的派生结果,
           丢了顶多再合成一次(语音引擎自己还有内容缓存,不会重复花钱)。
        """
        from ..study.tts import TtsEngine

        kind = str(group.get("kind") or KIND_DICTATION)
        #: 跟读念"每一条",听写念"每个词" —— 两个键名不一样,合成入口也不一样。
        pieces = list(group.get("phrases") or []) if kind == KIND_FOLLOW else list(
            group.get("words") or []
        )
        text = "、".join(str(piece) for piece in pieces)
        key = "study.merge." + day + "." + kind + "." + str(group["language"])
        cached: dict[str, Any] = {}
        try:
            raw = settings_store.get_raw(self.conn, key)
            cached = json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001
            cached = {}
        if (
            cached.get("text") == text
            and cached.get("artifact_id")
            and self.artifacts is not None
        ):
            try:
                if self.artifacts.exists(str(cached["artifact_id"])):
                    return {
                        "audio_url": "/artifacts/" + str(cached["artifact_id"]),
                        "artifact_id": str(cached["artifact_id"]),
                        "seconds": cached.get("seconds") or 0,
                        "error": "",
                    }
            except Exception:  # noqa: BLE001
                pass
        if self.artifacts is None:
            return {"audio_url": "", "error": "没接产物层,合并声音存不下来"}
        try:
            engine = TtsEngine()
            if kind == KIND_FOLLOW:
                # 跟读清单:一条念两遍(第一遍听、第二遍跟),中间留 4 秒 —— 与单题的节奏同一口径,
                # 只是把所有跟读排在**一段**里,家长不用来回点。
                audio = engine.build_follow(
                    [str(piece) for piece in pieces], repeat=2, gap_s=4.0
                )
            else:
                audio = engine.build_dictation(
                    [str(piece) for piece in pieces], title=group["label"],
                    repeat=2, gap_s=4.0,
                )
        except Exception as exc:  # noqa: BLE001
            return {"audio_url": "", "error": type(exc).__name__ + ": " + str(exc)[:80]}
        if not audio.ok:
            return {"audio_url": "", "error": audio.error or "发声失败"}
        suffix = ".mp3" if audio.media_type == "audio/mpeg" else ".wav"
        artifact = self.artifacts.register_bytes(
            bytes(audio.data),
            filename=group["label"] + "-" + day + suffix,
            label=group["label"] + " " + day,
            kind=KIND_FILE,
            session_id="study",
            meta={
                "day": day,
                "kind": kind,
                "language": group["language"],
                "count": len(pieces),
            },
        )
        seconds = round(float(audio.seconds), 2)
        try:
            settings_store.set_text(
                self.conn,
                key,
                json.dumps(
                    {"text": text, "artifact_id": artifact.id, "seconds": seconds},
                    ensure_ascii=False,
                ),
            )
        except Exception:  # noqa: BLE001 - 缓存写不进去只影响下次重做,不影响这次
            pass
        return {
            "audio_url": "/artifacts/" + str(artifact.id),
            "artifact_id": str(artifact.id),
            "seconds": seconds,
            "error": "",
        }

    def push_state(self, *, now: datetime | None = None) -> dict[str, Any]:
        """今天推送的状态。⚠️ 只回答"该不该推、几点推、有多少道",**不负责发**。"""
        moment = now or self._current_time()
        settings = self.settings()
        # ⚠️ due_count 是**几道题**,不是几个单元(单元数单独给 due_units):
        #    界面上写的是"N 道",按单元算会把同一道题数三遍(D108)。
        due = self.due_problem_items(today=moment.date().isoformat(), limit=200)
        units = self.due_items(today=moment.date().isoformat(), limit=1000)
        return {
            **settings.as_dict(),
            "today": moment.date().isoformat(),
            "now": moment.strftime("%H:%M"),
            "due_count": len(due),
            "due_units": len(units),
            "ready": bool(
                settings.push_enabled and moment.strftime("%H:%M") >= settings.push_time_local
            ),
            "schedule_note": self.schedule_note(),
        }

    def push_text(self, *, now: datetime | None = None) -> str | None:
        """**只渲染**今天的推送正文,不认领、不写 dispatch(D162)。

        为什么与 push_digest 分开:微信常驻进程要"发出去",而网页心跳要"占住
        这一天的 dispatches 让页面与上下文看得见"。两件事都需要同一段文字,
        但认领只能有一个入口 —— 所以渲染共用,认领留给 push_digest。
        """
        moment = now or self._current_time()
        settings = self.settings()
        if not settings.push_enabled:
            return None
        if moment.strftime("%H:%M") < settings.push_time_local:
            return None
        day = moment.date().isoformat()
        who = settings.student_name or "孩子"
        # ⚠️ 必须**按题归并**:按单元铺的话同一道题会在推送里连着出现三四遍(D108)
        items = self.due_problem_items(today=day, limit=settings.push_daily_limit)
        head = "📚 " + who + "今天的错题复习"
        if not items:
            return head + ":今天没有到期的题,明天见。"
        lines = [head + "(" + str(len(items)) + " 道,预计 " + str(max(5, len(items) * 3)) + " 分钟):"]
        for index, item in enumerate(items, start=1):
            stem = (item["stem_text"] or "").strip().replace("\n", " ")
            if len(stem) > 28:
                stem = stem[:28] + "…"
            names = list(item.get("kp_names") or [])
            tail = (" · " + "、".join(names)) if names else ""
            mark = " ⚠️反复错" if item["lapses"] >= 3 else ""
            lines.append(
                str(index) + ". 【" + str(item["subject"]) + "】" + (stem or "(图题)")
                + tail + mark
            )
        lines.append("打开错题页逐张过一遍:会了 / 忘了,各点一下就好。")
        return "\n".join(lines)

    def push_digest(self, *, now: datetime | None = None) -> str | None:
        """给确定性调度器的入口:到点给出正文,**认领由 Dispatcher 做**。

        ⭐ 它不碰模型,只读库、算日期、拼字符串。"今天推没推过"由 dispatches
        表的主键抢占回答(重启、并发都不会重发)。
        """
        return self.push_text(now=now)

    def due_problem_items(self, *, today: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """今天到期的题,**按题归并**(一道题一条)。

        ⭐ 唯一一份"按题归并"的实现(D108):推送正文、错题卷、**都在这里取数** ——
        按单元铺的话同一道题会印三四遍(用户 2026-10-13 报的就是这个现象)。
        归并时把同一道题的几个知识点收集成 `kp_names`,答错次数取其中最大的一条。
        """
        cap = max(1, int(limit))
        raw = self.due_items(today=today, limit=max(cap * 6, cap))
        unique: dict[int, dict[str, Any]] = {}
        for item in raw:
            problem_id = int(item["problem_id"])
            group = unique.get(problem_id)
            if group is None:
                group = {**item, "unit_ids": [], "kp_names": []}
                unique[problem_id] = group
            group["unit_ids"].append(int(item["unit_id"]))
            name = str(item.get("knowledge_point") or "").strip()
            if name and name not in group["kp_names"]:
                group["kp_names"].append(name)
            group["lapses"] = max(int(group.get("lapses") or 0), int(item.get("lapses") or 0))
        return list(unique.values())[:cap]

    # -- 产物:错题卷 -------------------------------------------------------
    def _sheet_items(self, *, today: str, cap: int) -> list[dict[str, Any]]:
        """错题卷的选题:**按题去重**。

        ⚠️ 一道题有几个知识点就有几个单元,直接铺到纸上同一道题会印两遍 ——
        纸面清单与屏幕上的复习卡不是一回事,这里必须去重(与 D108 同一件事)。
        """
        return self.due_problem_items(today=today, limit=cap)
    def build_review_sheet(
        self,
        *,
        today: str | None = None,
        limit: int | None = None,
        include_answers: bool = True,
    ) -> str:
        """把今天的复习清单渲染成一份**可打印 / 可发回手机**的错题卷。

        ⚠️ 纯字符串拼装:数字全部来自 SQL,格式固定,**不经过模型**。
        家长要的是"能打印出来给孩子做",不是"一段漂亮的散文"。
        """
        moment = self._current_time()
        day = today or moment.date().isoformat()
        settings = self.settings()
        who = settings.student_name or "孩子"
        cap = int(limit or settings.push_daily_limit)
        items = self._sheet_items(today=day, cap=cap)
        lines = [
            "# " + who + "的错题卷 · " + day,
            "",
            "共 " + str(len(items)) + " 道 · 生成时间 " + moment.strftime("%Y-%m-%d %H:%M")
            + " · " + self.schedule_note().split("。")[0],
            "",
        ]
        if not items:
            lines.append("今天没有到期的错题。")
            return "\n".join(lines)
        answers: list[str] = []
        for index, item in enumerate(items, start=1):
            problem = self.get_problem(int(item["problem_id"]))
            names = [str(name) for name in problem.knowledge_points]
            points = ("　[" + "、".join(names) + "]") if names else ""
            lines.append("## " + str(index) + ". 【" + str(item["subject"]) + "】" + points)
            lines.append("")
            stem = (item["stem_text"] or "").strip() or "(图题 —— 见错题照片)"
            lines.extend("　　" + row for row in stem.splitlines())
            if item["asset_count"]:
                lines.append("　　(配图 " + str(item["asset_count"]) + " 张,打开错题页可看)")
            if problem.audio_handle:
                # 🔊 听力/听写题的音频是**录入时绑好的**(D107):这里只印句柄,
                #    绝不重新合成 —— 重做会得到另一段声音,还白花钱。
                lines.append("　　🔊 " + (problem.audio_label or "音频") + " "
                             + problem.audio_handle + "(点开就能听)")
            lines.append("")
            lines.append("　　答:______________________________________")
            lines.append("")
            if include_answers:
                answer = (problem.answer_text or "").strip() or "(没记答案)"
                answers.append(str(index) + ". " + answer)
        if include_answers and answers:
            lines.extend(["---", "", "## 答案(给孩子做完再看)", ""])
            lines.extend("　　" + row for row in answers)
        return "\n".join(lines)

    def make_sheet(
        self,
        *,
        today: str | None = None,
        limit: int | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """生成错题卷并**登记成产物**(可下载、可发回微信)。"""
        if self.artifacts is None:
            raise StudyError("没有接产物层,出不了文件")
        moment = self._current_time()
        day = today or moment.date().isoformat()
        text = self.build_review_sheet(today=day, limit=limit)
        items = self._sheet_items(
            today=day, cap=int(limit or self.settings().push_daily_limit)
        )
        label = (self.settings().student_name or "孩子") + "的错题卷 " + day
        artifact = self.artifacts.register_text(
            text,
            filename="错题卷-" + day + ".md",
            label=label,
            session_id=session_id or "study",
        )
        self._emit(
            "study.sheet.made",
            {"artifact_id": artifact.id, "date": day, "items": len(items)},
            key="study.sheet:" + artifact.id,
        )
        return {"artifact": artifact.as_dict(), "items": len(items), "date": day, "text": text,
                "audios": sum(1 for item in items
                              if self.get_problem(int(item["problem_id"])).audio_handle)}

    # -- 产物:单文件 HTML 互动试卷(M14)----------------------------------
    def _figure_uri(self, asset_id: Any) -> tuple[str, str]:
        """把裁剪图从素材层读出来内联成 data:。返回 (uri, 说明)。"""
        if not asset_id or self.media is None:
            return "", ""
        try:
            asset = self.media.get(int(asset_id))
        except Exception:  # noqa: BLE001 - 图没了不该让整份卷子出不来
            return "", "图形丢了(素材库里找不到),请打开错题页看原图"
        try:
            # ⚠️ 必须走 MediaStore.read_bytes:Asset.path() 用的是全局 MEDIA_DIR,
            #    在"自定义 root"(测试、多实例)下会读到错的目录 —— 实测踩过。
            payload = self.media.read_bytes(int(asset_id))
        except OSError:
            return "", "图形文件读不出来,请打开错题页看原图"
        return data_uri(payload, asset.media_type), "图形:原题照片裁剪"

    def _audio_uri(
        self, artifact_id: Any, spec: dict[str, Any] | None = None
    ) -> tuple[str, str, str]:
        """把**绑定在题上**的音频读出来内联成 data:。返回 (uri, 标签, 说明)。

        ⭐ 这里**绝不重新合成**:合成在录入那一刻已经做过一次(见 study/audio.py 模块头)。
        读不出来必须把原因写出来 —— 一道静默变哑的听力题,家长只会以为孩子不肯听。
        """
        info = dict(spec or {})
        label = str(info.get("label") or "音频")
        failure = str(info.get("error") or "").strip()
        if failure:
            return "", label, label + "没做成:" + failure + " —— 这一题请家长念一遍"
        wanted = str(artifact_id or "").strip()
        if not wanted:
            if info:
                return "", label, label + "没绑定(录入时没生成)—— 请家长念一遍,或重新识别这道题"
            return "", "", ""
        if self.artifacts is None:
            return "", label, label + "读不出来:现在没有接产物层"
        try:
            artifact = self.artifacts.get(wanted)
            payload = self.artifacts.read_bytes(artifact)
        except Exception:  # noqa: BLE001 - 音频丢了不该让整份卷子出不来
            return "", label, label + "丢了(产物库里找不到)—— 重新识别一次这道题就能补回来"
        if len(payload) > int(config.HTML_INLINE_MAX_AUDIO_BYTES):
            return "", label, (label + "太大(" + str(round(len(payload) / 1024))
                                  + "KB),没内联进页面 —— 请到「素材」页打开它")
        caption = describe_audio(info) or label
        return data_uri(payload, artifact.media_type), label, caption + " · 点下面的播放器听(打印版没有声音)"

    def _receipt_for_artifact(
        self,
        artifact: Any,
        *,
        name: str,
        label: str,
        session_id: str | None = None,
    ) -> int:
        """给产物写一条 **artifact 域**的回执 —— 微信就是靠它把文件真的发给用户。

        ⚠️ 没有回执 = 用户收不到文件(桥接层只认回执里的 artifact_id)。
        回执写失败**不许**把出卷判成失败:文件已经在盘上了。

        ⚠️ `session_id` 必须是**发起这次出卷的会话**(member:<id> / web / CLI)。
        以前这里写死 "study",而桥接层在 2026-10-18 改成按会话取回执之后,
        微信里出的卷子就永远进不了 `_outbound_files` —— 文件在服务器上,人手里没有。
        默认仍退回 "study",保住网页/CLI 的旧行为。
        """
        if self.receipts is None:
            return 0
        try:
            receipt_id = self.receipts.record(
                kind="artifact.file",
                domain=DOMAIN_ARTIFACT,
                label=label,
                detail={"artifact_id": artifact.id, "name": name, "bytes": artifact.byte_size},
                undo_plan={
                    "type": "artifact",
                    "artifact_id": artifact.id,
                    "name": name,
                    "existed": False,
                    "path": str(self.artifacts.path_of(artifact)) if self.artifacts else "",
                },
                actor="agent",
                session_id=session_id or "study",
            )
            self.receipts.conn.commit()
            return int(receipt_id or 0)
        except Exception:  # noqa: BLE001 - 留痕失败不该让"卷子已经出好了"变成失败
            return 0

    def _answer_note(self, status: str, student_answer: str, answer: str) -> str:
        """把"为什么这题不能当标准答案"说清楚。⚠️ 不给结论,只摆事实。"""
        flag = str(status or "").strip().lower()
        if flag == "conflict":
            mine = (answer or "").strip() or "(空)"
            theirs = (student_answer or "").strip() or "(空)"
            return ("三方不一致:照片上孩子的作答是「" + theirs[:60] + "」,"
                    "这里给的是「" + mine[:60] + "」—— 先核对,再当标准答案用。")
        if flag == "incomplete":
            return ("题干或图形不全(照片可能没拍全),模型给不出可靠答案 —— "
                    "补拍或补齐题干后再识别一次。")
        return ""

    def build_sheet_model(
        self,
        *,
        today: str | None = None,
        limit: int | None = None,
        dictation_words: Sequence[str] | None = None,
        audio: Any = None,
        audio_note: str = "",
        extra_notes: Sequence[str] | None = None,
        with_items: bool = True,
    ) -> SheetModel:
        """把今天到期的错题装配成一份**卷面数据**。⭐ 全程不调模型。

        `with_items=False` 用于纯听写页:那份页面只要音频,不该顺带把错题也印上去。
        """
        moment = self._current_time()
        day = today or moment.date().isoformat()
        settings = self.settings()
        who = settings.student_name or "孩子"
        cap = int(limit or settings.push_daily_limit)
        rows = self._sheet_rows(today=day, cap=cap) if with_items else []
        items: list[SheetProblem] = []
        for index, row in enumerate(rows, start=1):
            uri, figure_note = self._figure_uri(row.get("figure_asset_id"))
            try:
                spec = json.loads(row.get("figure_spec") or "{}")
            except Exception:  # noqa: BLE001
                spec = {}
            figure_svg = str(spec.get("svg") or "")
            if figure_svg:
                figure_note = "图形:按题目参数重绘"
                if spec.get("needs_check"):
                    figure_note += "(模型说原照片上可能没框全,下面可对照原图)"
            # ⚠️ 只在"**本来该有图**"时才提示:裁过的图丢了、或识别说这题有图却没裁出来。
            #    有照片不等于有图形(实测:锯木头那道题的照片里根本没有图形),
            #    对那种题每次都喊"没裁出图形"是把噪声当提醒 —— 用户会很快学会无视它。
            source = str(row.get("figure_source") or "").strip().lower()
            if not uri and not figure_note and source in ("crop", "svg"):
                figure_note = "这道题有图形,但这次没取到 —— 打开错题页看原图"
            if uri and not figure_svg:
                turned = int(spec.get("rotation_applied") or 0)
                if turned:
                    # ⚠️ 旋转是这套流程里最不可靠的一环(实测模型报 270、正确答案是 90),
                    #    所以**把它写在图上**:摆反了一眼就能看出来,而不是悄悄印一张倒着的图
                    figure_note += "(照片是横躺的,已顺时针摆正 " + str(turned) + "° —— 摆反了点开原图核对)"
            # 🔊 声音:听力/听写题的音频**在录入时就做好并绑在题上**了(见 study/audio.py),
            #    出卷这里只是把产物读出来内联 —— 不调模型、不重新合成,所以每次都是同一段。
            audio_uri, audio_label, audio_note = self._audio_uri(
                row.get("audio_artifact_id"), row.get("audio")
            )
            status = str(row.get("answer_status") or "").strip().lower()
            answer = str(row.get("answer_text") or "").strip()
            student = str(row.get("student_answer") or "").strip()
            items.append(
                SheetProblem(
                    index=index,
                    subject=str(row.get("subject") or "其他"),
                    stem=str(row.get("stem_text") or "").strip() or "(图题 —— 见下方图形)",
                    kps=[str(name) for name in (row.get("kp_names") or [])],
                    answer=answer,
                    steps=steps_from_raw(row.get("solution_steps")),
                    answer_source=str(row.get("answer_source") or ""),
                    answer_status=status,
                    answer_label=label_for(str(row.get("answer_source") or ""), status),
                    student_answer=student,
                    figure_uri=uri,
                    figure_svg=figure_svg,
                    figure_note=figure_note,
                    audio_uri=audio_uri,
                    audio_label=audio_label,
                    audio_note=audio_note,
                    conflict_note=self._answer_note(status, student, answer),
                    unit_ids=[int(value) for value in (row.get("unit_ids") or [])],
                )
            )
        audio_uri = ""
        note = audio_note
        if audio is not None and getattr(audio, "ok", False):
            audio_uri = data_uri(bytes(audio.data), audio.media_type)
            bits = [str(getattr(audio, "provider", "")), str(getattr(audio, "voice", ""))]
            note = audio_note or ("发声:" + " / ".join(item for item in bits if item)
                                  + " · " + ("%.0f" % float(getattr(audio, "seconds", 0))) + " 秒")
            for item in getattr(audio, "notes", []) or []:
                if item:
                    note += " · " + str(item)
        return SheetModel(
            who=who, day=day, minutes=estimate_minutes(len(items)), items=items,
            generated_at=moment.strftime("%Y-%m-%d %H:%M"),
            dictation_words=[str(item) for item in (dictation_words or [])],
            audio_uri=audio_uri, audio_note=note,
            extra_notes=[str(item) for item in (extra_notes or [])],
        )

    def _sheet_rows(self, *, today: str, cap: int) -> list[dict[str, Any]]:
        """卷面用的一行一题(带 M14 的新列 + 知识点 + 复习单元 id)。

        ⚠️ 与 _sheet_items 同一口径:**按题去重**,不是按复习单元 ——
        一道题有几个知识点就有几个单元,直接铺到纸上同一道题会印两遍。
        """
        picked = self._sheet_items(today=today, cap=cap)
        rows: list[dict[str, Any]] = []
        for item in picked:
            problem_id = int(item["problem_id"])
            row = self.conn.execute(
                "SELECT id, subject, stem_text, answer_text, student_answer, answer_source,"
                " answer_status, solution_steps, asset_ids, figure_asset_id, figure_source,"
                " figure_spec, audio_artifact_id, audio_spec"
                " FROM app_problems WHERE id = ?",
                (problem_id,),
            ).fetchone()
            if row is None:
                continue
            names = [
                str(value["name"])
                for value in self.conn.execute(
                    "SELECT k.name FROM app_problem_kps pk"
                    " JOIN app_knowledge_points k ON k.id = pk.kp_id"
                    " WHERE pk.problem_id = ? ORDER BY pk.kp_id",
                    (problem_id,),
                ).fetchall()
            ]
            units = [
                int(value["id"])
                for value in self.conn.execute(
                    "SELECT id FROM app_review_units WHERE problem_id = ? AND suspended = 0"
                    " ORDER BY ord",
                    (problem_id,),
                ).fetchall()
            ]
            payload = dict(row)
            payload["kp_names"] = names
            payload["unit_ids"] = units
            try:
                payload["asset_ids"] = json.loads(row["asset_ids"] or "[]")
            except Exception:  # noqa: BLE001
                payload["asset_ids"] = []
            payload["audio"] = spec_of(row["audio_spec"])
            payload["audio_artifact_id"] = row["audio_artifact_id"]
            rows.append(payload)
        return rows

    def make_sheet_html(
        self,
        *,
        today: str | None = None,
        limit: int | None = None,
        dictation_words: Sequence[str] | None = None,
        title: str = "词语听写",
        repeat: int = 2,
        gap_s: float = 4.0,
        filename: str | None = None,
        session_id: str | None = None,
        **audio_kwargs: Any,
    ) -> dict[str, Any]:
        """出一份**单文件**互动错题卷(HTML)。图片与音频全部内联,双击就能打开。

        ⭐ 出卷这一步不调模型;只有"要带听写"时才会去要音频(而且按内容哈希缓存)。
        """
        if self.artifacts is None:
            raise StudyError("没有接产物层,出不了文件")
        moment = self._current_time()
        day = today or moment.date().isoformat()
        audio = None
        words: list[str] = []
        if dictation_words:
            from ..study.tts import TtsEngine, parse_words

            words = parse_words(dictation_words)
            if words:
                audio = TtsEngine(**audio_kwargs).build_dictation(
                    words, title=title, repeat=repeat, gap_s=gap_s,
                )
        model = self.build_sheet_model(
            today=day, limit=limit, dictation_words=words, audio=audio,
        )
        html = render_sheet_html(model)
        label = model.who + "的错题卷 " + day
        session = session_id or "study"
        artifact = self.artifacts.register_bytes(
            html.encode("utf-8"),
            filename=filename or ("错题卷-" + day + ".html"),
            label=label,
            kind=KIND_HTML,
            session_id=session,
            meta={
                "template_version": model.template_version,
                "items": len(model.items),
                "date": day,
                "dictation": len(words),
            },
        )
        # ⭐ 产物回执必须落在 **artifact 域**:桥接层就是靠它认领"这一轮写出来的文件"
        #    (见 bridge._outbound_files),微信那头才会把整份卷子当**文件**发出去。
        #    ⚠️ 少了这一步的后果实测过:卷子在服务器上好好的,用户手里什么都没有。
        deliver = filename or ("错题卷-" + day + ".html")
        receipt_id = self._receipt_for_artifact(
            artifact, name=deliver, label="错题卷:" + day, session_id=session,
        )
        self._emit(
            "study.sheet.html",
            {"artifact_id": artifact.id, "date": day, "items": len(model.items),
             "dictation": len(words)},
            key="study.sheet.html:" + artifact.id,
        )
        return {
            "artifact": artifact.as_dict(),
            "items": len(model.items),
            "date": day,
            "html": html,
            "model": model.as_dict(),
            #: 绑定在题目上的音频段数(听力/听写题,录入时已生成)—— 与整页听写段是两回事
            "problem_audios": sum(1 for item in model.items if item.audio_uri),
            "audio": None if audio is None else audio.as_dict(),
            #: 有没有写出发文件回执。只有渠道接回执层时才为 True;工具层据此决定
            #: 能不能说“文件发你了”,不许再无条件地骗用户。
            "delivered": bool(receipt_id),
        }

    def make_dictation_sheet(
        self,
        words: Sequence[str],
        *,
        title: str = "词语听写",
        repeat: int = 2,
        gap_s: float = 4.0,
        filename: str | None = None,
        session_id: str | None = None,
        **audio_kwargs: Any,
    ) -> dict[str, Any]:
        """只出一份**听写页**(带音频的 HTML)+ 音频本体(可单独发微信)。"""
        if self.artifacts is None:
            raise StudyError("没有接产物层,出不了文件")
        from ..study.tts import TtsEngine, parse_words

        clean = parse_words(words)
        if not clean:
            raise StudyError("词表是空的 —— 例如 --dictation 乌鸦,跳舞,清晨")
        audio = TtsEngine(**audio_kwargs).build_dictation(
            clean, title=title, repeat=repeat, gap_s=gap_s,
        )
        if not audio.ok:
            raise StudyError("发声失败:" + (audio.error or "未知原因"))
        moment = self._current_time()
        day = moment.date().isoformat()
        model = self.build_sheet_model(
            today=day, with_items=False, dictation_words=clean, audio=audio,
            extra_notes=[
                "这是一份听写页:点上面的播放器,它会读一遍、停一下、再读一遍。",
                "听写完把纸拍照发回微信,错的词会自动进错题本。",
            ],
        )
        model.title = model.who + "的" + title + " " + day
        html = render_sheet_html(model)
        suffix = ".mp3" if audio.media_type == "audio/mpeg" else ".wav"
        session = session_id or "study"
        audio_artifact = self.artifacts.register_bytes(
            bytes(audio.data),
            filename=(title + "-" + day + suffix),
            label=model.who + "的" + title + "音频 " + day,
            kind=KIND_FILE,
            session_id=session,
            meta={"provider": audio.provider, "voice": audio.voice,
                  "seconds": round(audio.seconds, 2), "words": clean},
        )
        page_artifact = self.artifacts.register_bytes(
            html.encode("utf-8"),
            filename=filename or (title + "-" + day + ".html"),
            label=model.title,
            kind=KIND_HTML,
            session_id=session,
            meta={"template_version": model.template_version, "words": clean,
                  "audio_artifact": audio_artifact.id},
        )
        # 听写页与音频都写回执:微信那头会把这两样都当**文件**发出去(见 D110)
        page_receipt = self._receipt_for_artifact(
            page_artifact,
            name=(filename or (title + "-" + day + ".html")),
            label="听写页:" + title + " " + day,
            session_id=session,
        )
        self._receipt_for_artifact(
            audio_artifact,
            name=(title + "-" + day + suffix),
            label="听写音频:" + title + " " + day,
            session_id=session,
        )
        self._emit(
            "study.dictation.made",
            {"artifact_id": page_artifact.id, "audio_artifact": audio_artifact.id,
             "words": len(clean), "provider": audio.provider},
            key="study.dictation:" + page_artifact.id,
        )
        return {
            "artifact": page_artifact.as_dict(),
            "audio_artifact": audio_artifact.as_dict(),
            "words": clean,
            "audio": audio.as_dict(),
            "html": html,
            "delivered": bool(page_receipt),
        }

    def review_history(self, unit_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM app_review_logs WHERE unit_id = ? ORDER BY id DESC LIMIT ?",
            (unit_id, limit),
        ).fetchall()
        return [
            {
                "ts_utc": row["ts_utc"],
                "rating": int(row["rating"]),
                "interval_days": row["interval_days"],
                "last_interval_days": row["last_interval_days"],
                "elapsed_ms": row["elapsed_ms"],
                "state_before": row["state_before"],
                "state_after": row["state_after"],
                "source": row["source"],
            }
            for row in rows
        ]

    # -- 内部 -------------------------------------------------------------
    def _emit(self, event_type: str, payload: dict[str, Any], key: str | None = None,
              *, commit: bool = True) -> None:
        """写发件箱。

        ⚠️ 幂等键必须来自**事件的自然身份**,不能拿 payload 的哈希凑数 ——
        两次完全相同的复习(同一单元、同一评分)会算出同一个哈希,第二条直接
        UNIQUE 冲突。实测踩过。
        """
        resolved = key or (event_type + ":" + uuid.uuid4().hex[:16])
        self.conn.execute(
            "INSERT INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                event_type,
                json.dumps(payload, ensure_ascii=False, default=str),
                "outbox:" + resolved,
                now_iso(),
            ),
        )
        if commit:
            self.conn.commit()
