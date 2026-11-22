"""拍照录入:图片 -> 候选区 -> (模型识别) -> 人工确认 -> 正式错题。

⭐ 为什么中间要有一个**候选区**,而不是"识别完直接落库":
   识别是会错的。错了要能改、能丢;断网、没密钥、模型抽风时,流程也不能断。
   所以这条流水线的形状是固定的三段:

       上传(存资产,立刻有结果)  ->  识别(可以是空的)  ->  确认(人来盖章)

   任何一段失败都只是"这一段没做成",不会把前一段的成果丢掉 —— 照片还在候选区躺着。

⚠️ 与 M4.3a 既有表的关系:app_intake_sessions / app_intake_candidates 在那一版就建好了,
   只是当时没有流水线去用它。这里补上识别用的列(model / raw_text / problem_id),
   并复用 sessions.profile_id 存**学生 id**(这张表就是"给谁录的"这个问题)。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from ... import config
from ...llm.vision import (
    MAX_PROBLEMS_PER_IMAGE,
    RecognitionResult,
    RecognizedProblem,
    ask_audio_text_sync,
    probe_answers_sync,
    recognize_image_sync,
    verify_figure_sync,
)
from ...memory.archive import now_iso
from ..artifacts import KIND_FILE
from ..media import MediaStore, sniff_media_type
from ..schemas.study import ProblemCreate
from ..services.study_service import StudyError, StudyService
from .answers import (
    SOURCE_LEGACY,
    SOURCE_MANUAL,
    SOURCE_MODEL,
    contains_number,
    format_number,
    local_arithmetic,
    STATUS_AGREED,
    STATUS_CONFLICT,
    STATUS_UNVERIFIED,
    Solution,
    compare_three_way,
    solve_problem_sync,
)
from .audio import (
    KIND_LISTENING,
    KIND_NONE,
    AudioPlan,
    describe_spec,
    normalize_text,
    plan_audio,
    plan_from_problem,
    render_plan,
    spec_json,
    spec_of,
    spec_payload,
)
# ⚠️ 必须改名导入:figures_svg 里也有一个 whitelist_name(那是**图形**白名单),
#    它在本文件下面几行被 import 进来,会把同名的这个**覆盖掉** ——
#    实测症状:听力/听写题一律被兜底成"按听力处理",而代码看着毫无问题(2026-10-13 踩过)。
from .audio import whitelist_name as audio_kind_name
from .figure import (
    DEFAULT_PAD,
    bbox_to_thousand,
    crop_figure,
    downscale_for_model,
    ink_ratio,
    looks_blank,
    parse_bbox,
    rotation_tiles,
    structure_ratio,
)



# ⚠️ 单一来源(2026-10-17 审计):这个串原先在 8 个模块各写一份。
#    改 .env 的 LA_TIMEZONE 而这里不动,就会出现"提示词说伦敦、提醒按上海响"。
#    真正决定每个服务用哪个时区的是**构造参数 tzid**(由 build_services 注入 settings.timezone),
#    这里只是"没注入时的兜底"。
DEFAULT_TZID = config.DEFAULT_TIMEZONE
SESSION_OPEN = "open"
SESSION_REVIEWING = "reviewing"
SESSION_DONE = "done"
DECISION_PENDING = "pending"
DECISION_ACCEPTED = "accepted"
DECISION_EDITED = "edited"
DECISION_REJECTED = "rejected"
# 一次识别最多处理几张图:再多就不像"录错题",而像"批量导入一整套卷子"
MAX_IMAGES_PER_RUN = 6

MIME_FALLBACK = "image/jpeg"


class IntakeError(RuntimeError):
    """录入流程的领域错误。"""


@dataclass
class Candidate:
    id: int
    session_id: int
    asset_id: int | None
    payload: dict[str, Any]
    confidence: float
    decision: str
    created_at: str
    model: str = ""
    raw_text: str = ""
    problem_id: int | None = None
    original_name: str | None = None
    media_type: str = ""

    @property
    def meta(self) -> dict[str, Any]:
        meta = self.payload.get("_meta")
        return meta if isinstance(meta, dict) else {}

    @property
    def recognized(self) -> bool:
        return bool(self.meta.get("recognized_at"))

    @property
    def error(self) -> str:
        return str(self.meta.get("error") or "")

    @property
    def settled(self) -> bool:
        return self.decision in (DECISION_ACCEPTED, DECISION_EDITED, DECISION_REJECTED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "asset_id": self.asset_id,
            "payload": self.payload,
            "confidence": self.confidence,
            "decision": self.decision,
            "created_at": self.created_at,
            "model": self.model,
            "problem_id": self.problem_id,
            "original_name": self.original_name,
            "media_type": self.media_type,
            "recognized": self.recognized,
            "error": self.error,
        }


@dataclass
class IntakeSession:
    id: int
    created_at: str
    status: str
    student_id: int | None = None
    #: 归属会话(member:<id> / bridge:<wxid> / NULL=旧库单用户遗留)。
    owner_session: str | None = None
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def pending(self) -> list[Candidate]:
        return [item for item in self.candidates if not item.settled]

    @property
    def accepted(self) -> list[Candidate]:
        return [
            item for item in self.candidates
            if item.decision in (DECISION_ACCEPTED, DECISION_EDITED)
        ]

    @property
    def recognized_count(self) -> int:
        return len([item for item in self.candidates if item.recognized])

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "status": self.status,
            "student_id": self.student_id,
            "owner_session": self.owner_session,
            "candidates": [item.as_dict() for item in self.candidates],
        }


class IntakeService:
    """录入流水线。**不持有模型客户端**,只持有一个"怎么拿到客户端"的工厂。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        study: StudyService,
        media: MediaStore | None = None,
        client_factory: Callable[[], Any] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        tzid: str = DEFAULT_TZID,
        auto_resolve: bool | None = None,
        tts_engine: Any = None,
        owner_session: str | None = None,
    ) -> None:
        self.conn = conn
        #: 归属会话。None = 未启用成员/CLI/后台,不做读写过滤(与领域 owner 口径一致)。
        self.owner_session = owner_session
        self.study = study
        self.media = media or MediaStore(conn)
        self._client_factory = client_factory
        self._now = now_provider
        self.tzid = tzid
        #: 语音引擎(对象或"返回引擎的可调用")。None = 用时现造一个(百炼 → 本机兜底)。
        #: ⚠️ 留这个口子是给测试用的:真造引擎会去读设置、可能联网(见 .env 里那个真 key)。
        self._tts_engine = tts_engine
        #: 录入后要不要**自动**补标准答案与图形(M14)。
        #: None = 听设置(`app_study_settings.solve_on_recognize`,默认开)。
        #: ⚠️ 放在这里而不是各个入口上:录错题有四个入口(微信素材 / CLI / 网页确认 /
        #:    聊天工具),每个入口各写一遍必然走成"同一个动作两种行为"。
        self._auto_resolve = auto_resolve

    # -- 会话 -------------------------------------------------------------
    def _moment(self) -> datetime:
        if self._now is not None:
            return self._now()
        return datetime.now(ZoneInfo(self.tzid))

    def start_session(self, *, student_id: int | None = None) -> int:
        cursor = self.conn.execute(
            "INSERT INTO app_intake_sessions(profile_id, created_at, status, owner_session)"
            " VALUES (?, ?, ?, ?)",
            (student_id, now_iso(), SESSION_OPEN, self.owner_session),
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    def session(self, session_id: int) -> IntakeSession:
        row = self.conn.execute(
            "SELECT * FROM app_intake_sessions WHERE id = ?", (int(session_id),)
        ).fetchone()
        if row is None:
            raise IntakeError("录入会话不存在: " + str(session_id))
        owner = row["owner_session"] if "owner_session" in row.keys() else None
        if self.owner_session and owner not in (None, self.owner_session):
            raise IntakeError("这条录入不属于当前成员")
        return IntakeSession(
            id=int(row["id"]),
            created_at=row["created_at"],
            status=row["status"],
            student_id=int(row["profile_id"]) if row["profile_id"] is not None else None,
            owner_session=str(owner) if owner is not None else None,
            candidates=self.candidates(int(row["id"])),
        )

    def latest_session(self, *, statuses: Sequence[str] = (SESSION_OPEN, SESSION_REVIEWING)):
        """最近一次还没处理完的会话 —— 界面靠它回答"我上次拍的那张呢"。"""
        marks = ",".join("?" for _ in statuses)
        sql = (
            "SELECT id FROM app_intake_sessions WHERE status IN (" + marks + ")"
        )
        params: list[Any] = list(statuses)
        if self.owner_session:
            sql += " AND (owner_session = ? OR owner_session IS NULL)"
            params.append(self.owner_session)
        sql += " ORDER BY id DESC LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        return self.session(int(row["id"])) if row is not None else None

    def pending_count(self) -> int:
        sql = (
            "SELECT count(*) FROM app_intake_candidates c"
            " JOIN app_intake_sessions s ON s.id = c.session_id"
            " WHERE c.decision = ?"
        )
        params: list[Any] = [DECISION_PENDING]
        if self.owner_session:
            sql += " AND (s.owner_session = ? OR s.owner_session IS NULL)"
            params.append(self.owner_session)
        row = self.conn.execute(sql, params).fetchone()
        return int(row[0])

    def _set_status(self, session_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE app_intake_sessions SET status = ? WHERE id = ?", (status, int(session_id))
        )
        self.conn.commit()

    def _refresh_status(self, session_id: int) -> str:
        """会话状态由**候选区的实际状态**决定,不由调用顺序决定。"""
        rows = self.conn.execute(
            "SELECT decision, count(*) AS n FROM app_intake_candidates WHERE session_id = ?"
            " GROUP BY decision",
            (int(session_id),),
        ).fetchall()
        counts = {row["decision"]: int(row["n"]) for row in rows}
        pending = counts.get(DECISION_PENDING, 0)
        done = counts.get(DECISION_ACCEPTED, 0) + counts.get(DECISION_EDITED, 0)
        status = SESSION_REVIEWING if pending else (SESSION_DONE if done else SESSION_DONE)
        self._set_status(session_id, status)
        return status

    # -- 上传 -------------------------------------------------------------
    def add_image(
        self, session_id: int, data: bytes, *, original_name: str | None = None
    ) -> Candidate:
        """存图 + 建候选行。**这一步不调用模型** —— 先把照片安全落地。"""
        self.session(session_id)          # 会话不存在就当场报错,别写孤儿行
        media_type = sniff_media_type(data)
        if not media_type.startswith("image/"):
            raise IntakeError("只支持图片(按内容判断,不看后缀):" + str(original_name or ""))
        asset = self.media.store_bytes(data, original_name=original_name, media_type=media_type)
        cursor = self.conn.execute(
            "INSERT INTO app_intake_candidates(session_id, asset_id, payload_json, confidence,"
            " decision, created_at) VALUES (?, ?, ?, 0, ?, ?)",
            (int(session_id), asset.id, json.dumps({}, ensure_ascii=False),
             DECISION_PENDING, now_iso()),
        )
        self.conn.commit()
        self._set_status(session_id, SESSION_REVIEWING)
        return self.get_candidate(int(cursor.lastrowid or 0))

    def add_asset(
        self,
        session_id: int,
        asset_id: int,
        *,
        original_name: str | None = None,
        split_from: int | None = None,
    ) -> Candidate:
        """把**已经在字节仓里的**资产挂成一条候选。

        ⭐ 与 add_image 的区别:这里**不复制字节**。微信来的照片已经由素材层存好了,
        再读一遍、再走一次内容寻址纯属浪费 —— 资产本来就是内容寻址的,
        同一个 asset_id 天然就是同一张图。
        """
        self.session(session_id)
        payload: dict[str, Any] = {}
        if split_from is not None:
            payload["_meta"] = {"split_from": int(split_from)}
        cursor = self.conn.execute(
            "INSERT INTO app_intake_candidates(session_id, asset_id, payload_json, confidence,"
            " decision, created_at) VALUES (?, ?, ?, 0, ?, ?)",
            (int(session_id), int(asset_id),
             json.dumps(payload, ensure_ascii=False), DECISION_PENDING, now_iso()),
        )
        self.conn.commit()
        self._set_status(session_id, SESSION_REVIEWING)
        return self.get_candidate(int(cursor.lastrowid or 0))

    def recognize_candidate(self, candidate_id: int, *, solve: bool = False) -> RecognitionResult:
        """只认这一条候选(它已经指向某张图)。给"素材直接进流水线"用。"""
        candidate = self.get_candidate(candidate_id)
        if candidate.asset_id is None:
            return RecognitionResult(error="这条候选没有图片")
        client = self._client_factory() if self._client_factory is not None else None
        if client is None:
            return RecognitionResult(error="")
        asset = self.media.get(int(candidate.asset_id))
        try:
            data = asset.path(self.media.root).read_bytes()
        except OSError as exc:
            return RecognitionResult(error="读不到图片: " + str(exc)[:80])
        result = recognize_image_sync(
            client, data, asset.media_type or MIME_FALLBACK,
            known_points=self._known_points(), solve=solve,
        )
        self._store_result(candidate, result, solve=solve)
        return result

    # -- 素材 → 错题(手机那条主路径)--------------------------------------
    def ingest_material(
        self,
        material_id: int,
        *,
        auto_accept: bool = True,
        solve: bool = False,
        student_id: int | None = None,
        actor: str = "agent",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """一份素材走完整条流水线:**识别 → 判定是不是题目 → 直接录入 → 留痕**。

        ⭐ 这就是"手机上拍一张错题发过来,它自己录进去"的那一步。三条取舍:

        1. **认出来就直接录**(`auto_accept`),不再让人点确认 —— 手机上的确认成本极高,
           而错了有回执可撤销(直写 + 回执 + 一键撤销是本项目既有的写法)。
        2. **识别失败不改状态**:素材留在那里(handled=failed),照片一份都不丢,
           回话里说清楚"收到了但没认出来"。
        3. **回话由这里生成**,不由模型生成 —— 微信那条链路上模型可能压根没参与,
           而用户最需要看到的恰恰是"到底录了什么"。
        """
        from ..materials import (
            HANDLED_FAILED,
            HANDLED_RECORDED,
            HANDLED_STORED,
            MaterialService,
        )

        service = MaterialService(self.conn, media=self.media)
        material = service.get(int(material_id))
        outcome: dict[str, Any] = {
            "material_id": material.id,
            "session_id": None,
            "is_problem": False,
            "recognized": 0,
            "recorded": [],
            "pending": [],
            "offline": False,
            "error": "",
            "reason": "",
            "handled": material.handled,
            "message": "",
        }
        if not material.is_image:
            service.mark(material.id, handled=HANDLED_STORED, note="不是图片,先留档")
            outcome["handled"] = HANDLED_STORED
            outcome["message"] = (
                "已收下《" + (material.original_name or ("素材 #" + str(material.id)))
                + "》存成素材 #" + str(material.id) + "(不是图片,先留档,没有解析)。"
            )
            return outcome

        target_student = student_id
        if target_student is None:
            target_student = material.student_id or self.study.settings().student_id
        intake_session = self.start_session(student_id=target_student)
        outcome["session_id"] = intake_session
        candidate = self.add_asset(
            intake_session, int(material.asset_id or 0), original_name=material.original_name
        )

        if not self.vision_available:
            outcome["offline"] = True
            outcome["error"] = "当前没有可用的模型(离线模式或没配密钥)"
            outcome["handled"] = HANDLED_FAILED
            service.mark(material.id, handled=HANDLED_FAILED, result=outcome,
                         note="离线模式,没有识别")
            outcome["message"] = (
                "照片收到了(素材 #" + str(material.id) + "),但现在没有可用的模型,没法识别。"
                "原图已经存好,等模型可用时还能再识别。"
            )
            return outcome

        result = self.recognize_candidate(candidate.id, solve=solve)
        outcome["recognized"] = 1 if result.ok else 0
        outcome["is_problem"] = bool(result.is_problem)
        outcome["error"] = result.error
        outcome["reason"] = result.reason

        accepted: list[dict[str, Any]] = []
        if result.ok and auto_accept:
            for item in self.session(intake_session).candidates:
                if item.settled or not item.recognized:
                    continue
                try:
                    accepted.append(self.accept(item.id, actor=actor, session_id=session_id))
                except (IntakeError, StudyError) as exc:
                    outcome["error"] = (str(outcome["error"]) + " " + str(exc)).strip()
        outcome["recorded"] = accepted
        outcome["pending"] = [item.id for item in self.session(intake_session).pending]
        if accepted:
            handled = HANDLED_RECORDED
        elif result.parsed and not result.is_problem:
            # ⭐ "模型看过,并判断这不是题目"与"识别失败"是两件事:
            #    前者是**正常结果**(一只猫的照片),后者才需要用户重拍。
            handled = HANDLED_STORED
        elif result.ok:
            handled = HANDLED_STORED      # 认出来了,但按要求没有自动录
        else:
            handled = HANDLED_FAILED
        service.mark(material.id, handled=handled, result=outcome, student_id=target_student)
        outcome["handled"] = handled
        outcome["message"] = render_ingest_result(outcome, material)
        return outcome

    def candidates(self, session_id: int) -> list[Candidate]:
        rows = self.conn.execute(
            "SELECT c.*, a.original_name AS original_name, a.media_type AS media_type"
            " FROM app_intake_candidates c LEFT JOIN app_assets a ON a.id = c.asset_id"
            " WHERE c.session_id = ? ORDER BY c.id",
            (int(session_id),),
        ).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def get_candidate(self, candidate_id: int) -> Candidate:
        row = self.conn.execute(
            "SELECT c.*, a.original_name AS original_name, a.media_type AS media_type"
            " FROM app_intake_candidates c LEFT JOIN app_assets a ON a.id = c.asset_id"
            " WHERE c.id = ?",
            (int(candidate_id),),
        ).fetchone()
        if row is None:
            raise IntakeError("候选不存在: " + str(candidate_id))
        return self._candidate_from_row(row)

    def _candidate_from_row(self, row: sqlite3.Row) -> Candidate:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        keys = set(row.keys())
        return Candidate(
            id=int(row["id"]),
            session_id=int(row["session_id"]),
            asset_id=int(row["asset_id"]) if row["asset_id"] is not None else None,
            payload=payload if isinstance(payload, dict) else {},
            confidence=float(row["confidence"] or 0.0),
            decision=row["decision"],
            created_at=row["created_at"],
            model=str(row["model"] or "") if "model" in keys else "",
            raw_text=str(row["raw_text"] or "") if "raw_text" in keys else "",
            problem_id=(
                int(row["problem_id"])
                if "problem_id" in keys and row["problem_id"] is not None
                else None
            ),
            original_name=row["original_name"] if "original_name" in keys else None,
            media_type=str(row["media_type"] or "") if "media_type" in keys else "",
        )

    # -- 识别 -------------------------------------------------------------
    @property
    def vision_available(self) -> bool:
        return self._client_factory is not None

    def recognize_session(
        self, session_id: int, *, solve: bool = False, force: bool = False, limit: int | None = None
    ) -> dict[str, Any]:
        """对会话里**每张图**各调一次视觉模型。

        ⚠️ 一次一张图,而不是把 6 张图塞进一次请求:① 出错了能定位到具体哪张;
        ② 一张照片里有两道题时,额外那道可以自然地变成**同一个 asset 上的第二个候选**。
        """
        report: dict[str, Any] = {
            "session_id": int(session_id), "images": 0, "recognized": 0, "problems": 0,
            "failed": 0, "skipped": 0, "errors": [], "offline": False,
        }
        target = self.session(session_id)
        # ⚠️ 一张图**只跑一次**识别,而且只认"主候选":
        #   一张照片上两道题时,第二道题是从同一次识别里拆出来的候选(split_from),
        #   它没有自己的图 —— 再拿同一张图去认一遍,只会把第一道的字段盖到它头上,
        #   再拆出第三条,越跑越多。
        seen_assets: set[int] = set()
        targets = []
        for item in target.candidates:
            if item.asset_id is None or item.settled:
                continue
            if item.meta.get("split_from"):
                continue
            if item.asset_id in seen_assets:
                continue
            if not force and item.recognized:
                continue
            seen_assets.add(int(item.asset_id))
            targets.append(item)
        cap = MAX_IMAGES_PER_RUN if limit is None else max(1, int(limit))
        targets = targets[:cap]
        report["skipped"] = max(0, len(target.pending) - len(targets))
        if not targets:
            return report

        client = self._client_factory() if self._client_factory is not None else None
        if client is None:
            report["offline"] = True
            report["errors"].append("没有可用的模型客户端:识别已跳过,可以手工填写。")
            return report

        known_points = self._known_points()
        for candidate in targets:
            report["images"] += 1
            asset = self.media.get(int(candidate.asset_id or 0))
            try:
                data = asset.path(self.media.root).read_bytes()
            except OSError as exc:
                report["failed"] += 1
                report["errors"].append("读不到图片 #" + str(asset.id) + ": " + str(exc)[:80])
                self._store_result(candidate, RecognitionResult(error="图片文件丢了"))
                continue
            result = recognize_image_sync(
                client, data, asset.media_type or MIME_FALLBACK,
                known_points=known_points, solve=solve,
            )
            self._store_result(candidate, result, solve=solve)
            if result.ok:
                report["recognized"] += 1
                report["problems"] += len(result.problems)
            else:
                report["failed"] += 1
                report["errors"].append(result.error or "识别失败")
        self._refresh_status(session_id)
        return report

    def _known_points(self, limit: int = 60) -> list[str]:
        """喂给模型的"已有知识点"清单 —— 复用是别名表能起作用的前提。"""
        rows = self.conn.execute(
            "SELECT name FROM app_knowledge_points ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [str(row["name"]) for row in rows]

    def _store_result(
        self, candidate: Candidate, result: RecognitionResult, *, solve: bool = False
    ) -> None:
        """识别结果写回候选区。第一道题落在**本候选**上,多出来的另开候选行。"""
        meta = {
            "recognized_at": now_iso(),
            "solve": bool(solve),
            "model": result.model,
            "error": result.error,
        }
        problems: list[RecognizedProblem] = list(result.problems)
        payload: dict[str, Any] = {"_meta": meta}
        if problems:
            payload.update(problems[0].as_payload())
            confidence = problems[0].confidence
        else:
            confidence = 0.0
        self.conn.execute(
            "UPDATE app_intake_candidates SET payload_json = ?, confidence = ?, model = ?,"
            " raw_text = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), confidence, result.model,
             result.raw, candidate.id),
        )
        for extra in problems[1:]:
            if self._already_split(candidate, extra):
                # ⚠️ 重跑识别(force)时,同一条拆分候选会被重复创建 ——
                #    一张照片上两道题,跑三次就变六条,人会以为模型疯了。
                #    身份用"同一张资产 + 同一段题干",因为这就是"同一道题"的自然身份。
                continue
            extra_payload = {"_meta": dict(meta, split_from=candidate.id)}
            extra_payload.update(extra.as_payload())
            self.conn.execute(
                "INSERT INTO app_intake_candidates(session_id, asset_id, payload_json,"
                " confidence, decision, created_at, model, raw_text)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (candidate.session_id, candidate.asset_id,
                 json.dumps(extra_payload, ensure_ascii=False), extra.confidence,
                 DECISION_PENDING, now_iso(), result.model, ""),
            )
        self.conn.commit()

    def _already_split(self, candidate: Candidate, extra: RecognizedProblem) -> bool:
        stem = (extra.stem_text or "").strip()
        if not stem:
            return False
        for item in self.candidates(candidate.session_id):
            if item.asset_id != candidate.asset_id:
                continue
            if str(item.payload.get("stem_text") or "").strip() == stem:
                return True
        return False

    def _auto_resolve_on(self) -> bool:
        """要不要在录入之后自动解题 + 裁图。读设置,读不到就当关(宁可不动数据)。"""
        if self._auto_resolve is not None:
            return bool(self._auto_resolve)
        try:
            return bool(self.study.settings().solve_on_recognize)
        except Exception:  # noqa: BLE001 - 设置读不出来不该让录入失败
            return False

    # -- 确认 -------------------------------------------------------------
    def accept(
        self,
        candidate_id: int,
        *,
        overrides: dict[str, Any] | None = None,
        actor: str = "user",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """人工确认:候选 -> 正式错题(+ 复习单元)。**字段以人填的为准。**"""
        candidate = self.get_candidate(candidate_id)
        if candidate.decision in (DECISION_ACCEPTED, DECISION_EDITED):
            raise IntakeError("这条候选已经入库了(错题 #" + str(candidate.problem_id) + ")")
        payload = merge_overrides(candidate.payload, overrides or {})
        # ⭐ 照片必须跟着走进错题 —— 复习时孩子看到的就是这张图,丢了它等于把题丢了。
        #    允许多张:候选本来就挂在同一个资产上,配合 overrides 也能补图。
        asset_ids = [int(item) for item in (payload.get("asset_ids") or [])]
        if candidate.asset_id is not None and int(candidate.asset_id) not in asset_ids:
            asset_ids.insert(0, int(candidate.asset_id))
        payload["asset_ids"] = asset_ids
        # 归属:候选/表单都没说"这是谁的"时,用**这个录入会话**选的学生的。
        # ⚠️ 少了这一步,CLI 的 --student 小明 会被静默忽略,错题全记到默认学生头上(实测踩过)。
        if payload.get("student_id") is None and not payload.get("student_name"):
            session = self.session(candidate.session_id)
            if session.student_id is not None:
                payload["student_id"] = session.student_id
        try:
            model = ProblemCreate.model_validate(payload)
        except Exception as exc:                       # pydantic 的报错对用户没意义,换成一句人话
            raise IntakeError("字段还不完整:" + _first_error(exc)) from exc
        if not model.subject:
            raise IntakeError("必须先选一个学科")
        problem = self.study.add_problem(model, actor=actor, session_id=session_id)
        decision = DECISION_EDITED if overrides else DECISION_ACCEPTED
        self.conn.execute(
            "UPDATE app_intake_candidates SET decision = ?, problem_id = ? WHERE id = ?",
            (decision, problem.id, candidate.id),
        )
        self.conn.commit()
        status = self._refresh_status(candidate.session_id)
        # ⭐ M14:录入之后**自动**补标准答案与图形(用户 2026-10-13 拍板)。
        #    放在 accept 里,是因为四个入口最后都走这里 —— 一个人写一遍,行为就不会分叉。
        #    ⚠️ 它失败**不许把录入判成失败**:题已经进库了,补答案是锦上添花。
        resolved: dict[str, Any] = {}
        human_answer = str(model.answer_text or "").strip()
        if human_answer:
            # 人手填过答案:标成 manual 并**只补图形**,绝不拿模型的答案覆盖人填的那个
            self.conn.execute(
                "UPDATE app_problems SET answer_source = ?, answer_status = ? WHERE id = ?",
                (SOURCE_MANUAL, STATUS_UNVERIFIED, problem.id),
            )
            self.conn.commit()
        if self._auto_resolve_on():
            try:
                resolved = self.resolve_problem(problem.id, solve=not human_answer)
            except Exception as exc:  # noqa: BLE001
                resolved = {"error": type(exc).__name__ + ": " + str(exc)[:120]}
        return {
            "candidate_id": candidate.id,
            "problem_id": problem.id,
            "decision": decision,
            "session_status": status,
            "answer": resolved.get("answer") or "",
            "answer_status": resolved.get("answer_status") or "",
            "answer_note": resolved.get("answer_note") or "",
            "figure_source": resolved.get("figure_source") or "",
            "figure_asset_id": resolved.get("figure_asset_id"),
            # 声音:录进来那一刻就已经做好并绑定(听力/听写题);没有就如实说原因
            "audio_artifact_id": resolved.get("audio_artifact_id"),
            "audio_label": resolved.get("audio_label") or "",
            "audio_note": resolved.get("audio_note") or "",
            "audio_error": resolved.get("audio_error") or "",
            "resolve_error": resolved.get("error") or "",
            "resolve_notes": list(resolved.get("notes") or []),
            "review_units": len(problem.review_units),
            "subject": problem.subject,
            # ⚠️ stem 必须带上:回话里要逐条列出"录了什么",没有题干就只剩"(图题)",
            #    用户根本认不出录进去的是哪道题(实测第一版就是这样)。
            "stem": problem.stem_text or "",
            "knowledge_points": list(problem.knowledge_points),
            "first_due_local": (
                problem.review_units[0].due_date_local if problem.review_units else None
            ),
        }

    def reject(self, candidate_id: int, *, note: str = "") -> dict[str, Any]:
        candidate = self.get_candidate(candidate_id)
        payload = dict(candidate.payload)
        meta = dict(candidate.meta)
        meta["reject_note"] = str(note)[:200]
        payload["_meta"] = meta
        self.conn.execute(
            "UPDATE app_intake_candidates SET decision = ?, payload_json = ? WHERE id = ?",
            (DECISION_REJECTED, json.dumps(payload, ensure_ascii=False), candidate.id),
        )
        self.conn.commit()
        status = self._refresh_status(candidate.session_id)
        return {"candidate_id": candidate.id, "decision": DECISION_REJECTED,
                "session_status": status}

    # -- M14:标准答案与图形(回到照片前重问一次)----------------------------
    def resolve_problem(
        self,
        problem_id: int,
        *,
        probe: bool = True,
        solve: bool = True,
        refine: bool = True,
        force: bool = False,
        actor: str = "agent",
    ) -> dict[str, Any]:
        """给一道**已经录进库**的错题补上:标准答案(模型独立解)+ 图形(裁剪)。

        ⭐ 为什么必须"回到照片前":老错题录进来时,照片上的答案被当成正确答案抄走了
        (实测 #5 = "孩子算的 65 + 老师订正 54" 混在一起)。要修它只有一条路 ——
        重新看那张照片,把孩子写的、老师订的、图形在哪分开抄一遍。

        步骤(每一步失败都**不阻断**下一步):
          ① 看照片:student_answer / teacher_answer / figure{bbox} (1 次视觉调用)
          ② 按 bbox 裁剪图形 → 存素材层(0 次调用)
          ②b **验货**:裁出来的那块按四个方向各发一张,问"正不正/完整吗/混进文字了吗"
             (1 次视觉调用)。以前这一步只问"完整吗"且什么都不改;现在角度不对就换角度
             重裁、被切掉就加大边距重裁 —— 这是 2026-11-21 修掉"图是歪的/没取到"的地方。
          ③ 独立解题:题干 + 图形截图 → answer/steps/缺什么 (1 次调用)
          ④ 三方对照(纯代码)→ answer_status + 人话说明
        """
        row = self.conn.execute(
            "SELECT * FROM app_problems WHERE id = ?", (int(problem_id),)
        ).fetchone()
        if row is None:
            return {"problem_id": int(problem_id), "error": "没有这道错题"}
        stem = str(row["stem_text"] or "").strip()
        current_status = str(row["answer_status"] or "").strip().lower()
        current_source = str(row["answer_source"] or "").strip().lower()
        # ⚠️ legacy = 老数据里"从照片上抄来的答案",它**恰恰是孩子写的那个** ——
        #    绝不能被当成"已经解过了"而跳过(那样这一趟补答案就白跑了)。
        if (not force and current_source != SOURCE_LEGACY
                and current_status in (STATUS_AGREED, STATUS_CONFLICT, STATUS_UNVERIFIED)):
            return {
                "problem_id": int(problem_id), "skipped": True,
                "reason": "已经解过了(" + current_status + ")",
                "answer_status": current_status,
            }
        client = self._client_factory() if self._client_factory is not None else None
        if client is None:
            # ⚠️ 没有模型也能做的那部分**照做**:题干里写着听写词表的题(家长常直接把词打进来,
            #    纸上没东西可拍),声音素材是**纯字符串解析**(见 audio.plan_from_problem)——
            #    它不该因为没配 key / 欠费就没了。与"读取路径不含 LLM"是同一条纪律。
            audio_changes, audio_note, audio_notes = self._bind_audio(
                int(problem_id), row, {}, image=b"", media_type="",
                stem=stem, subject=str(row["subject"] or ""), client=None, force=force,
            )
            if audio_changes:
                self._apply_problem_changes(int(problem_id), audio_changes, actor=actor)
            spec_now = spec_of(audio_changes.get("audio_spec"))
            return {
                "problem_id": int(problem_id),
                "error": "没有模型客户端(离线模式)",
                "audio_artifact_id": audio_changes.get("audio_artifact_id"),
                "audio_kind": str(spec_now.get("kind") or ""),
                "audio_label": str(spec_now.get("label") or ""),
                "audio_seconds": float(spec_now.get("seconds") or 0),
                "audio_note": audio_note or describe_spec(spec_now),
                "audio_error": str(spec_now.get("error") or ""),
                "notes": audio_notes,
            }

        changes: dict[str, Any] = {}
        notes: list[str] = []
        student = str(row["student_answer"] or "").strip()
        teacher = str(row["teacher_answer"] or "").strip()

        # ① 照片:拆开"孩子写的 / 老师订的 / 图形在哪 / 这题要不要出声"
        photo, media_type = self._first_photo(row)
        figure: dict[str, Any] = {}
        #: 这道题要不要出声(听力/听写)+ 要念的原文。⭐ 录入时靠它把音频**做一次、绑上**。
        audio_info: dict[str, Any] = {}
        rotation = 0
        #: 这个框是谁给的:probe(补拍那一趟)/ recognize(识别那一趟,兜底用)。
        #: ⚠️ 落进 figure_spec,下次"这图为什么没取到/为什么歪了"才有得查。
        bbox_source = "probe"
        probe_bytes: bytes = b""
        probe_type: str = ""
        if photo and probe:
            # ⚠️ **问的时候用小图**:实测把 4283×718 的原图直接发过去,模型的框三次给出
            #    三个完全不同的答案;缩到长边 1200 再问就准了(见 figure.MODEL_MAX_SIDE)。
            #    坐标是归一化的,所以裁的时候仍然用**原图**、不用换算。
            probe_bytes, probe_type = downscale_for_model(photo)
            if not probe_bytes:
                probe_bytes, probe_type = photo, media_type
            # ⚠️ 2026-11-21 起**不再传 catalog**:矢量重绘已关掉,让模型继续输出 render
            #    规格只会白花 token(那份规格现在没人消费)。
            probed = probe_answers_sync(client, probe_bytes, probe_type or media_type,
                                        stem=stem)
            if probed.get("error"):
                notes.append("看照片失败:" + str(probed["error"])[:80])
            else:
                if (force or not student) and probed.get("student_answer"):
                    student = str(probed["student_answer"])
                    changes["student_answer"] = student
                if (force or not teacher) and probed.get("teacher_answer"):
                    teacher = str(probed["teacher_answer"])
                    changes["teacher_answer"] = teacher
                figure = probed.get("figure") or {}
                audio_info = dict(probed.get("audio") or {})
                rotation = int(probed.get("rotation_degrees") or 0)
                # ⚠️ 兜底(2026-11-21 实测):这一趟"说是图形、却没给能用的框"时,
                #    识别那一趟其实常常给过一个能用的框 —— 丢掉它,整道题就没图了
                #    (真错题 #30 就是这样:卷面写"识别说有图形,但这次没取到")。
                #    两趟是**同一个模型看同一张照片**,这一趟没给不代表上一趟错。
                if figure.get("present") and parse_bbox(figure.get("bbox")) is None:
                    fallback = self._recognize_figure(int(problem_id))
                    if fallback.get("bbox") is not None:
                        figure = dict(figure, bbox=list(fallback["bbox"]))
                        bbox_source = "recognize"
                        notes.append(
                            "这一趟没给出可用的图形框 —— 改用识别那一趟的框"
                            "(边距已加大,请打开原图核对)"
                        )
                if rotation:
                    notes.append("照片是横躺/倒着的(按顺时针 " + str(rotation) + "° 摆正;不对就点开原图核对)")

        # ② 图形:加边距裁剪 → 存素材层
        figure_id = row["figure_asset_id"]
        figure_bytes: bytes | None = None
        figure_note = ""
        needs_check = False
        if photo and (figure.get("present") or force):
            if not figure.get("present"):
                # 重跑时说"没有图形",就得把上一次那条**清掉** —— 否则图上还挂着
                # 上一轮误判的裁剪(实测:一道纯文字题的卡通仓鼠被当成图形裁了出来)
                changes["figure_source"] = "none"
                changes["figure_asset_id"] = None
                changes["figure_bbox"] = None
                changes["figure_spec"] = None
                figure_id = None
            else:
                base_pad = 0.24 if bbox_source == "recognize" else DEFAULT_PAD
                rotation_guess = rotation
                crop = crop_figure(
                    photo, figure.get("bbox"), rotate_degrees=rotation, pad=base_pad,
                )
                if not crop.ok and bbox_source != "recognize":
                    # 这里的失败可能是「框太贴边/太小/退化」—— 先别急着宣布「没图」,
                    # 回头用识别那一趟的框再试一次(实测两趟给的框常常一大一小,
                    # 小的贴边裁不出来时,大的反而能裁)。
                    retry = self._recognize_figure(int(problem_id))
                    if retry.get("bbox") is not None and parse_bbox(retry["bbox"]) is not None:
                        second = crop_figure(
                            photo, retry["bbox"], rotate_degrees=rotation, pad=0.24,
                        )
                        if second.ok:
                            crop = second
                            figure = dict(figure, bbox=list(retry["bbox"]))
                            bbox_source = "recognize"
                            base_pad = 0.24
                            notes.append(
                                "这一趟的框裁不出东西 —— 已改用识别那一趟的框"
                                "(边距已加大,请打开原图核对)"
                            )
                # ⭐ 验货(2026-11-21):把裁出来的那一块按 0/90/180/270 各发一张,一次问三件事
                #    (正不正 / 完整吗 / 混进题干文字了吗)。三个答案都能落成一个动作 ——
                #    这是「复核」第一次真的改东西(以前只写一句提示,框一动不动)。
                #    ⚠️ 这四个方向图用的是**没转过**的那一块:模型只需要"挑一张",
                #       不需要心算角度 —— 实测自由回答角度只有 1/4 对,四选一两次全对。
                verified: dict[str, Any] = {}
                pad_final = base_pad
                if crop.ok and refine:
                    piece = crop_figure(
                        photo, figure.get("bbox"), rotate_degrees=0, pad=base_pad,
                    )
                    tiles = rotation_tiles(piece.data) if piece.ok else []
                    if tiles:
                        verified = verify_figure_sync(client, tiles)
                        if verified.get("error"):
                            notes.append("图形复核失败(按原样保留):"
                                         + str(verified["error"])[:60])
                        else:
                            upright = verified.get("upright")
                            if upright is not None and int(upright) % 360 != int(rotation) % 360:
                                notes.append(
                                    "图形方向按复核改了:识别说顺时针 " + str(rotation)
                                    + "°,复核说 " + str(int(upright) % 360) + "°"
                                )
                                rotation = int(upright) % 360
                            if verified.get("complete") is False:
                                pad_final = min(0.34, base_pad + 0.15)
                                notes.append(
                                    "⚠️ 复核说图形可能被切掉了一边 —— 已加大边距重裁,请核对"
                                )
                            if verified.get("has_text") is True:
                                notes.append(
                                    "⚠️ 复核说这块图里带进了题干文字"
                                    "(可以在下面「收窄」一点,或打开原图核对)"
                                )
                            if (verified.get("complete") is False
                                    or verified.get("has_text") is True):
                                needs_check = True
                if crop.ok and (int(rotation) != int(rotation_guess) or pad_final != base_pad):
                    # 复核说「转错了 / 切掉了」—— 现在才真的重裁。用**原图**重裁,
                    # 不是把裁出来的小图再转一次(那样会掉一次质量、还会带白边)。
                    crop = crop_figure(
                        photo, figure.get("bbox"), rotate_degrees=rotation, pad=pad_final,
                    )
                #: 这块到底有没有东西 —— **只算一次**(两次算白花 50ms,口径还容易漂)
                blank = bool(crop.ok and looks_blank(crop.data))
                figure_note = str(figure.get("description") or figure.get("kind") or "")
                # ⚠️ 2026-11-21:矢量重绘(SVG 白名单)**先关掉**,图形统一走照片裁剪。
                #    理由:① 那条路其实一直是死的(`normalize_figure` 把 render 丢了,
                #    `pick_spec` 永远看不到规格),纸面上「有」、实际一次都没画出来;
                #    ② 画错一个图形比没有图形更坏(孩子会照着错的图理解题意);
                #    ③ 少一个变量,排查「图为什么不对」的成本立刻减半。
                #    渲染器本身(`figures_svg.py`)与**老数据里已经存下的 SVG** 都保留 ——
                #    只是新录入不再生成。要重新打开:把这段后面接回 `pick_spec` /
                #    `ask_figure_params` / `render_spec`,并让探针重新传 catalog。
                spec_payload: dict[str, Any] = {
                    "kind": str(figure.get("kind") or ""),
                    "description": figure_note,
                    "rotation_applied": rotation,
                    "needs_check": needs_check,
                    # ⚠️ 诊断留档(2026-11-21):这套流程"有时没图、有时图是歪的",
                    #    事后只能对着卷面猜是哪一步不对。这几行把**当时的输入与判断**
                    #    一起存下来:框是谁给的、模型说要转多少度、裁出来有多"空"。
                    "probe": {
                        "bbox": list(figure.get("bbox") or [])[:4],
                        "bbox_source": bbox_source,
                        # 模型第一趟(识别/补拍)报的角度,以及复核改成什么
                        "rotation_guess": rotation_guess,
                        "rotation": rotation,
                        "crop_error": crop.error,
                        "ink": round(ink_ratio(crop.data), 4) if crop.ok else None,
                        "structure": round(structure_ratio(crop.data), 4) if crop.ok else None,
                        "blank": blank,
                        "size": [crop.width, crop.height] if crop.ok else None,
                    },
                    # 复核的原话(没复核/失败也会有痕迹:error 在里面)
                    "verify": verified,
                }
                if crop.ok and blank:
                    # 框落在空白纸面上(实测有过):不当作图形。宁可说"没取到"。
                    changes["figure_source"] = "crop"
                    changes["figure_asset_id"] = None
                    figure_id = None
                    notes.append("这一块裁出来是空白的(模型给的框可能落在空白处)—— 打开原图看")
                elif crop.ok:
                    stored = self.media.store_bytes(
                        crop.data, subdir="figures", media_type="image/png",
                        original_name="figure-" + str(problem_id) + ".png",
                    )
                    figure_id = stored.id
                    figure_bytes = crop.data
                    changes["figure_asset_id"] = stored.id
                    changes["figure_bbox"] = json.dumps(crop.bbox)
                    changes["figure_source"] = "crop"
                else:
                    # 有图但没裁出来:如实记下来,让卷面上显示"图形没取到"
                    changes["figure_source"] = "crop"
                    notes.append("图形没裁出来:" + crop.error)
                changes["figure_spec"] = json.dumps(spec_payload, ensure_ascii=False)

        # ②b 声音:听力/听写题的音频**在录入时就生成一次并绑定在题上**(见 study/audio.py)。
        #     ⭐ 用户 2026-10-13 拍板:错题是按记忆曲线**反复**学的,每复习一次都重新合成的话,
        #     ① 每次念的都不是同一段声音(不稳定)② 白花 token 与语音费。
        audio_note = ""
        # ⚠️ 这里**不按"探针有没有说要出声"来决定要不要调** —— 题干预备队(听写词表就写在
        #    题干里)恰恰要在"模型什么都没说"时接上;没事可做时它自己会立刻返回(纯字符串判断)。
        if self.study is not None:
            audio_changes, audio_note, audio_notes = self._bind_audio(
                int(problem_id), row, audio_info,
                image=probe_bytes, media_type=probe_type or media_type,
                stem=stem, subject=str(row["subject"] or ""),
                client=client, force=force,
            )
            changes.update(audio_changes)
            notes.extend(audio_notes)

        # ③ 独立解题。⚠️ 入参里**不放** student_answer(锚定效应,见 answers.py 模块头)
        solution = Solution(error="")
        if solve:
            solution = solve_problem_sync(
                client, stem=stem, image=figure_bytes, media_type="image/png",
                figure_note=figure_note,
            )
            if solution.error:
                notes.append("解题失败:" + solution.error[:80])
            # ⭐ 能被确定性验算的题一律验算:纯算式不该靠模型的运气。
            #    对得上就注明"与模型一致",对不上就**以本地为准**(并留下模型给的那个值)。
            checked = local_arithmetic(stem)
            if checked is not None:
                expression, value = checked
                exact = format_number(value)
                if solution.ok and contains_number(solution.answer, value):
                    notes.append("本地验算:" + expression + " = " + exact + "(与模型一致)")
                else:
                    notes.append("本地验算:" + expression + " = " + exact + " —— 模型给的是「"
                                 + (solution.answer or "空")[:30] + "」,已按本地结果")
                    solution.steps = [expression + " = " + exact] + list(solution.steps)
                    solution.answer = exact
                    solution.answer_kind = "unique"
                    solution.missing_info = []
                    solution.error = ""

        # ④ 三方对照 + 落库
        # ⚠️ `solve=False` 是"只补图形,别碰答案" —— 人手填过答案时必须这么调,
        #    否则模型的答案会把人填的那个盖掉(那是最不可接受的一种覆盖)。
        status, note = "", ""
        if solve:
            status, note = compare_three_way(
                model_answer=solution.answer, teacher_answer=teacher, student_answer=student,
                answer_kind=solution.answer_kind, missing_info=solution.missing_info,
            )
            if solution.ok:
                changes["answer_text"] = solution.answer
                changes["answer_source"] = SOURCE_MODEL
                changes["solution_steps"] = json.dumps(solution.steps, ensure_ascii=False)
            else:
                # 解不出来就把老答案清掉:它来自照片(孩子的错答),留着只会被当成标准答案印出去
                old = str(row["answer_text"] or "").strip()
                if old:
                    note += "(原来的答案「" + old[:40] + "」已清空 —— 它来自照片,未经核对)"
                changes["answer_text"] = None
                changes["answer_source"] = (
                    SOURCE_MODEL if not solution.error else (row["answer_source"] or SOURCE_LEGACY)
                )
                changes["solution_steps"] = None
            changes["answer_status"] = status
            changes["answer_note"] = note
        audio_spec_final = spec_of(
            changes["audio_spec"] if "audio_spec" in changes else row["audio_spec"]
        )
        self._apply_problem_changes(int(problem_id), changes, actor=actor)
        return {
            "problem_id": int(problem_id),
            "stem": stem[:60],
            "answer": solution.answer,
            "steps": list(solution.steps),
            "answer_status": status,
            "answer_note": note,
            "teacher_answer": teacher,
            "student_answer": student,
            "missing_info": list(solution.missing_info),
            "figure_asset_id": figure_id,
            "figure_source": str(changes.get("figure_source") or row["figure_source"] or ""),
            "figure_kind": str(figure.get("kind") or ""),
            # 声音:绑上了什么、还是"这次会是哑的"(三种结局都要看得见)
            "audio_artifact_id": (
                changes["audio_artifact_id"] if "audio_artifact_id" in changes
                else row["audio_artifact_id"]
            ),
            "audio_kind": str(audio_spec_final.get("kind") or ""),
            "audio_label": str(audio_spec_final.get("label") or ""),
            "audio_seconds": float(audio_spec_final.get("seconds") or 0),
            "audio_note": audio_note or describe_spec(audio_spec_final),
            "audio_error": str(audio_spec_final.get("error") or ""),
            "notes": notes,
            "solution_error": solution.error,
        }

    # -- M14d:声音素材(录入时做一次,之后每次复习都用同一段)------------------
    def _audio_engine(self) -> Any:
        """语音引擎。注入的优先(测试/离线用),否则按设置现造一个(百炼 → 本机兜底)。"""
        source = self._tts_engine
        if source is None:
            from .tts import TtsEngine

            return TtsEngine()
        return source() if callable(source) else source

    def _bind_audio(
        self,
        problem_id: int,
        row: Any,
        info: dict[str, Any],
        *,
        image: bytes,
        media_type: str,
        stem: str,
        subject: str,
        client: Any,
        force: bool,
    ) -> tuple[dict[str, Any], str, list[str]]:
        """把"这题要出声"变成一段**绑定在题上**的音频(理由见 study/audio.py 模块头)。

        返回 (要写进 app_problems 的列, 一句给人看的话, 备注列表)。
        ⚠️ 三种结局都要**说出来**:这次绑上了 / 沿用了旧的 / 没做成(卷面上会是哑的)。
        后者尤其不能吞:静默失败的听力题,家长会以为是孩子不肯听。
        """
        changes: dict[str, Any] = {}
        notes: list[str] = []
        existing = spec_of(row["audio_spec"])
        info = dict(info or {})
        # ① 照片/模型那条路
        plan: AudioPlan | None = None
        image_error = ""
        kind = audio_kind_name(info.get("kind"))
        text = normalize_text(info.get("text"))
        if kind not in ("", KIND_NONE):
            if not text and client is not None and image:
                # 单独追问一句"要念的是哪段文字"(与图形参数同一招:一次只问一件事)
                asked = ask_audio_text_sync(client, image, media_type, kind=kind, stem=stem)
                text = normalize_text(asked.get("text"))
                if text and asked.get("note"):
                    notes.append("音频原文的说明:" + str(asked["note"])[:60])
            plan = plan_audio(kind, text, subject=subject, title=(subject or "") + "听力")
        elif info.get("needs") and text:
            # 模型说要出声却没给可靠的 kind:按**听力**(整段读一遍)兜底并说明。
            # ⚠️ 这里兜的只是"怎么念"(节奏),**念什么**仍然只认照片上抄下来的原文。
            notes.append("模型没说是听力还是听写 —— 按听力(整段读一遍)处理")
            plan = plan_audio(KIND_LISTENING, text, subject=subject,
                              title=(subject or "") + "听力")
        if plan is not None and not plan.ok:
            image_error = plan.error
        # ② 题干预备队:**照片上问不出来、或者压根没有照片**时,题干自己会说话 ——
        #    "听写词语:一亩地"这种题,要念什么本来就写在题目里(纯字符串解析,不调模型)。
        if plan is None or not plan.ok:
            # 知识点名一起给启发式:题干可能只有词表,线索在"单词认读"这种知识点名里。
            kp_text = ""
            try:
                kp_text = " ".join(
                    str(item["name"])
                    for item in self.conn.execute(
                        "SELECT k.name FROM app_problem_kps pk"
                        " JOIN app_knowledge_points k ON k.id = pk.kp_id"
                        " WHERE pk.problem_id = ?",
                        (int(problem_id),),
                    ).fetchall()
                )
            except Exception:  # noqa: BLE001 - 读不到知识点不该影响这一步
                kp_text = ""
            fallback = plan_from_problem(
                stem=stem, answer=row["answer_text"] or "", subject=subject,
                extra_hints=kp_text,
            )
            if fallback is not None and fallback.ok:
                plan = fallback
                notes.extend(fallback.notes)
                if image_error:
                    notes.append("照片上没读到要念的原文 —— 改用题干里写的了")
            elif plan is None:
                plan = fallback          # 可能是 None,也可能是"看出要出声但没有材料"
        if plan is None:
            if force and existing:
                notes.append("这次识别说这题不用出声 —— 已把上次绑的音频摘掉")
                return {"audio_artifact_id": None, "audio_spec": None}, "", notes
            return {}, "", notes
        if not plan.ok:
            notes.append(plan.describe())
            return {"audio_spec": spec_json(spec_payload(plan, error=plan.error))}, \
                plan.describe(), notes
        # ⭐ 已经绑过**同一段内容**就一次都不重新合成 —— 这正是"绑定"的意义。
        #    判据是 kind+原文+节奏,不是"有没有音频":改了原文还是要重做的。
        #    ⚠️ 这里**故意不看 force**:force 是"重跑这一趟识别与解题",不是"重新念一遍"。
        #       同一段文字重念一次,得到的往往是**听感上不一样**的另一段声音(用户要的恰恰是
        #       "每次复习听到的是同一段");何况内容缓存本来就不会再花钱,重做只剩坏处。
        if (
            existing.get("kind") == plan.kind
            and existing.get("text") == plan.text
            and int(existing.get("repeat") or 0) == int(plan.repeat)
            and str(row["audio_artifact_id"] or "")
            and self._artifact_exists(str(row["audio_artifact_id"]))
        ):
            return {}, describe_spec(existing) + " —— 沿用了录入时那一段(没有重新合成)", notes
        if self.study is None or self.study.artifacts is None:
            message = plan.label + "没做成:没接产物层,音频存不下来(这道题在卷面上会是哑的)"
            notes.append(message)
            return {"audio_spec": spec_json(spec_payload(plan, error="没有接产物层"))}, message, notes
        audio = render_plan(self._audio_engine(), plan)
        if not audio.ok:
            message = (plan.label + "没做成:" + (audio.error or "未知原因")
                       + " —— 这道题在卷面上会是哑的,请把听力材料补拍进来")
            notes.append(message)
            return (
                {"audio_artifact_id": None,
                 "audio_spec": spec_json(spec_payload(plan, audio, error=audio.error or "发声失败"))},
                message, notes,
            )
        suffix = ".mp3" if audio.media_type == "audio/mpeg" else ".wav"
        artifact = self.study.artifacts.register_bytes(
            bytes(audio.data),
            filename=plan.label + "-" + str(problem_id) + suffix,
            label=plan.label + " 错题 #" + str(problem_id),
            kind=KIND_FILE,
            session_id="study",
            meta={
                "problem_id": int(problem_id), "kind": plan.kind,
                "provider": audio.provider, "voice": audio.voice,
                "seconds": round(float(audio.seconds), 2),
            },
        )
        payload = spec_payload(plan, audio, artifact_id=artifact.id)
        spent = float(audio.spent_yuan)
        line = (
            plan.label + "已生成并绑定:" + ("%.1f" % float(audio.seconds)) + " 秒"
            + (" · " + str(audio.voice) if audio.voice else "")
            + ("(用了缓存,这次没花钱)" if spent <= 0 else "(这次花 %.4f 元)" % spent)
        )
        for note in list(audio.notes or [])[:2]:
            if note:
                notes.append("发声:" + str(note)[:80])
        return {"audio_artifact_id": artifact.id, "audio_spec": spec_json(payload)}, line, notes

    def _artifact_exists(self, artifact_id: str) -> bool:
        """产物还在不在。⚠️ 查不到一律当"不在"(宁可重做一次,也不要绑一个空句柄)。"""
        if self.study is None or self.study.artifacts is None:
            return False
        try:
            return bool(self.study.artifacts.exists(artifact_id))
        except Exception:  # noqa: BLE001
            return False

    def resolve_answers(
        self,
        *,
        limit: int = 20,
        only_unresolved: bool = True,
        force: bool = False,
        actor: str = "agent",
    ) -> dict[str, Any]:
        """批量补答案。**默认只补还没有答案的那些**,不重跑已经解过的。"""
        sql = "SELECT id FROM app_problems WHERE status <> 'archived'"
        params: list[Any] = []
        if only_unresolved and not force:
            # "还没解过" = 没有模型答案(legacy 是老数据抄来的照片答案,不算解过)
            sql += (" AND (answer_source IS NULL OR answer_source = 'legacy'"
                    " OR answer_status IS NULL OR answer_status = ''"
                    " OR answer_status = 'incomplete')")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        results = [self.resolve_problem(int(row["id"]), force=force, actor=actor) for row in rows]
        return {
            "total": len(results),
            "resolved": sum(1 for item in results if item.get("answer")),
            "skipped": sum(1 for item in results if item.get("skipped")),
            "failed": sum(1 for item in results if item.get("error")),
            "items": results,
        }

    def _recognize_figure(self, problem_id: int) -> dict[str, Any]:
        """识别那一趟(候选区)记下的图形框 —— 给"补拍这一趟"兜底。

        ⚠️ 为什么留这条路(2026-11-21 实测):两道真错题里,一道的第一趟**给过能用的框**,
        第二趟却"说是图形、框退化",于是整道题的图就没了,卷面上只剩一句
        "识别说这题有图形,但这次没取到"。两趟是**同一个模型看同一张照片**,
        第二趟没给不代表上一趟错 —— 有框总比没图强(框还能加边距、还能人工核)。
        """
        try:
            row = self.conn.execute(
                "SELECT payload_json FROM app_intake_candidates WHERE problem_id = ?"
                " ORDER BY id DESC LIMIT 1",
                (int(problem_id),),
            ).fetchone()
        except sqlite3.Error:
            return {}
        if row is None:
            return {}
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            return {}
        figure = payload.get("figure") if isinstance(payload, dict) else None
        if not isinstance(figure, dict) or not figure.get("present"):
            return {}
        return {"bbox": figure.get("bbox"), "rotation": payload.get("rotation_degrees") or 0}

    def _first_photo(self, row: Any) -> tuple[bytes, str]:
        """错题挂的第一张照片(有图形的题都靠它裁)。读不到就返回空。"""
        try:
            asset_ids = json.loads(row["asset_ids"] or "[]")
        except Exception:  # noqa: BLE001
            return b"", ""
        for asset_id in asset_ids:
            try:
                asset = self.media.get(int(asset_id))
                return self.media.read_bytes(int(asset_id)), asset.media_type or MIME_FALLBACK
            except Exception:  # noqa: BLE001 - 换下一张
                continue
        return b"", ""

    def _apply_problem_changes(
        self, problem_id: int, changes: dict[str, Any], *, actor: str
    ) -> None:
        """只更新**这次真的算出来的**那些列(空 changes 直接返回)。

        ⚠️ `None` 是**有意义的值**(清空某列),所以这里只过滤"没被提到"的键,
        不能顺手把 None 也滤掉 —— 那会让"清空老答案"这件事静默失效。
        """
        fields = dict(changes)
        if not fields:
            return
        assignments = ", ".join(key + " = ?" for key in fields)
        self.conn.execute(
            "UPDATE app_problems SET " + assignments + ", updated_at = ? WHERE id = ?",
            (*fields.values(), now_iso(), int(problem_id)),
        )
        self.conn.commit()
        if self.study is None:
            return
        # ⚠️ 两件事都是实测踩出来的:
        #   ① 幂等键**不能只用 (题号, 状态)** —— 重解同一道题会算出同一个键,第二条直接
        #      UNIQUE 冲突(补答案整批中断,而且已经写好的行不会回滚)。
        #      每一次解析都是一次独立事件,所以键里带一个随机段。
        #   ② 发件箱是**补充**,不是这次写入的前提 —— 它失败不许把补答案这件事判成失败。
        try:
            # 发件箱归领域服务所有,这里借用它的写入(单机单进程,同一个连接)
            self.study._emit(
                "study.answer.resolved",
                {"problem_id": int(problem_id), "status": fields.get("answer_status", ""),
                 "source": fields.get("answer_source", "")},
                key=("study.answer:" + str(problem_id) + ":"
                     + str(fields.get("answer_status", "")) + ":" + uuid.uuid4().hex[:10]),
            )
        except Exception:  # noqa: BLE001 - 留痕失败不该让"答案已经写好了"变成失败
            pass


def merge_overrides(
    payload: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    """把表单改过的字段并到候选载荷上。

    ⚠️ 三条规矩,每一条都对应一种"人已经改过了但机器又把它覆盖回去"的事故:
      1. `_meta` 永远不参与合并(它是识别的元数据,不是题目字段);
      2. 表单里的空字符串 = **明确要清空**这个字段,而不是"没填所以保留旧值";
      3. knowledge_points 允许用逗号/顿号分隔的字符串传进来(表单就是这么传的)。
    """
    merged = {key: value for key, value in payload.items() if key != "_meta"}
    for key, value in overrides.items():
        if key.startswith("_") or value is None:
            continue
        if key in ("knowledge_points", "kp"):
            merged["knowledge_points"] = _split_points(value)
            continue
        merged[key] = value
    for key in ("subject", "grade", "source", "stem_text", "answer_text", "explanation"):
        if isinstance(merged.get(key), str):
            value = merged[key].strip()
            # 可选文本字段:清空 -> NULL。落成空串的话,界面上会看到"有值但是空的",
            # 而且 "IS NULL" 这类查询再也问不出"这道题到底填没填答案"。
            merged[key] = value or ("" if key == "subject" else None)
    merged.setdefault("knowledge_points", [])
    return merged


def _split_points(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        items: list[Any] = list(value)
    else:
        items = str(value or "").replace("、", ",").replace(";", ",").split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def render_ingest_result(outcome: dict[str, Any], material: Any = None) -> str:
    """把流水线结果说成一句给**人**看的话(微信与网页聊天共用同一份措辞)。

    ⚠️ 这段话不经过模型,是确定性生成的 —— 它要回答的是"你到底录了什么",
    这件事不允许有一丝自由发挥。
    """
    mid = outcome.get("material_id")
    recorded = list(outcome.get("recorded") or [])
    if recorded:
        lines = ["📚 照片收到,认出 " + str(len(recorded)) + " 道题,已录进错题本:"]
        for index, item in enumerate(recorded, start=1):
            points = "、".join(str(x) for x in (item.get("knowledge_points") or []))
            head = "【" + str(item.get("subject") or "其他") + "】"
            stem = str(item.get("stem") or "").strip().replace("\n", " ")
            if len(stem) > 30:
                stem = stem[:30] + "…"
            tail = (" · " + points) if points else ""
            lines.append(str(index) + ". " + head + (stem or "(图题)") + tail)
            #⭐ 录完就已经有标准答案了(自动解题),要说清楚它是谁给的、可不可信 ——
            #   "录进去了"和"答案对不对"是两件事,用户需要一眼看出后者。
            answer = str(item.get("answer") or "").strip()
            status = str(item.get("answer_status") or "").strip()
            if answer:
                flag = {"agreed": "(与老师批改一致)", "conflict": "(⚠️ 与老师批改不一致,待核对)",
                        "incomplete": "(题干不全,待补)"}.get(status, "(模型独立解出,未确认)")
                lines.append("   答案:" + answer[:30] + flag)
            elif status == "incomplete":
                lines.append("   答案:暂时给不出来 —— 题干或图形不全,补拍后再识别一次")
            if item.get("figure_source") == "crop":
                lines.append("   图形:已从照片上裁出来,试卷里会带上")
            # 🔊 声音是**录进来那一刻**就做好并绑在题上的(见 study/audio.py):
            #    之后每次复习都是同一段,不重新合成。做没做成都要报出来。
            audio = str(item.get("audio_note") or "").strip()
            if audio:
                lines.append("   " + ("🔊 " if item.get("audio_artifact_id") else "🔇 ") + audio)
        first_due = recorded[0].get("first_due_local")
        if first_due:
            lines.append("首次复习排在 " + str(first_due) + "。")
        lines.append("录错了就回「/撤销」把它退回去。")
        return "\n".join(lines)

    if outcome.get("offline"):
        return str(outcome.get("message") or "照片收到了,但现在没有可用的模型。")

    error = str(outcome.get("error") or "").strip()
    if outcome.get("is_problem"):
        return (
            "照片收到了(素材 #" + str(mid) + "),看着像题目,但没抄出可用的字段"
            + ("(" + error + ")" if error else "")
            + "。可以重拍一张更清楚的,或到电脑上手工补录 —— 原图已经存好了。"
        )
    reason = str(outcome.get("reason") or error or "").strip()
    return (
        "照片收到了(素材 #" + str(mid) + "),这张看着不是题目"
        + (":" + reason if reason else "")
        + "。已留档,没有录进错题本。"
    )


def _first_error(exc: Exception) -> str:
    """把 pydantic 的校验错误翻译成"哪个字段不对"。"""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            items = errors()
        except Exception:
            items = []
        if items:
            first = items[0]
            location = ".".join(str(part) for part in first.get("loc") or [])
            return (location + ": " if location else "") + str(first.get("msg") or "")[:80]
    return str(exc)[:120]
