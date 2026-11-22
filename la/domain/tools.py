"""由同一份 Pydantic 模型生成 LLM 工具 —— "schema 单真源"的落点。

    expense_record 的工具参数        =  ExpenseCreate.model_json_schema()
    POST /expenses 的请求体          =  ExpenseCreate
    ledger_service.record_expense()  =  同一个 service 方法

任何一处手写第二份 schema 都是 bug(能力域 §6.3)。
工具描述按**任务粒度**写(不是每个 CRUD 端点包一个工具),并带命名空间前缀。
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import ValidationError

from ..agent.feedback import AskHandler, Option, Question
from ..base.phases import CATEGORY_RESEARCH, CATEGORY_TASK, PHASE_ANSWER
from ..ops.paths import DEFAULT_POLICY, PathNotAllowed
from .artifacts import KIND_FILE
from .receipts import DOMAIN_ARTIFACT
from ..tools.registry import RISK_CONFIRM, RISK_LOW, ToolInputError, ToolSpec
from .categories import CategoryAdvisor
from .schemas.calendar import (
    CalendarCancel,
    CalendarEventCreate,
    CalendarList,
    CalendarReschedule,
)
from .schemas.ledger import (
    CategoryCreate,
    CategoryList,
    ExpenseConfirm,
    ExpenseCorrect,
    ExpenseImport,
    ExpenseQuery,
    TxnCreate,
)
from .services.ledger_service import DIRECTION_LABELS, LedgerError, format_cents
from .schemas.health import (
    FoodLogCreate,
    HealthGoalCreate,
    MeasurementCreate,
    ProfileUpdate,
    TrendQuery,
    WorkoutCreate,
)
from .schemas.study import (
    KnowledgePointCreate,
    ProblemAddInput,
    ProblemDeleteInput,
    ProblemQuery,
    QuizMe,
    QuizResult,
    StudyPushSettings,
)
from .schemas.notes import (
    NoteAdd,
    NoteAppend,
    NoteDelete,
    NoteEdit,
    NoteMkdir,
    NoteMove,
    NoteRecent,
    NoteRename,
    NoteRestore,
    NoteSearch,
    NoteShow,
    NoteTree,
)
from .services.health_service import HealthService
from .services.calendar_service import CalendarService
from .services.notes_service import NotesService
from .services.ledger_service import LedgerService
from .services.study_service import StudyService
from .study.scheduler import RATING_LABELS


#: 合并工具收到未知 action 时的统一码(与 registry 的结构化错误码同风格)。
#: 各域的分发器都用它 —— "说了要做某件事,但那个动作不存在"要比"参数校验失败"更清楚。
CODE_UNKNOWN_ACTION = "UNKNOWN_ACTION"


def merge_input_models(
    name: str,
    action_values: tuple[str, ...],
    models: tuple[type, ...],
) -> type:
    """把 N 个 Pydantic 输入模型合成**一个**合并工具的输入模型(批次 B 用)。

    ⭐ 为什么走模型而不是手写 JSON Schema:合并不能成为"约束失守"的后门。
      每个字段仍由它原来那个模型定义(max_length / ge / le / Literal 都还在),
      合并模型只是把这些字段**收集**到一起 —— 单真源没有第二份。

    规则(刻意保守):
      * 加一个 action 字段(枚举 = 这个工具认的动作);
      * 字段名冲突时**最后一个模型的字段定义胜出**(调用方要保证同名同义:
        例如 metric_code 在 MeasurementCreate 与 TrendQuery 里含义相同,
        但后者 min 7 —— 取后者,宁可严一点);
      * 各模型原来的"必填"变成可选(必填是**按 action** 的,写进工具说明;
        真正的校验发生在服务层各自 model_validate 那一刻)。

    ⚠️ 不接受**:合并工具自己的 handler 必须显式校验 —— 用 pick_by_action()。
    """
    from typing import Annotated

    from pydantic import Field, create_model

    fields: dict[str, tuple[Any, Any]] = {
        "action": (str, Field(..., description="要做什么:" + " / ".join(action_values))),
    }
    for model in models:
        for field_name, info in model.model_fields.items():
            if field_name == "action":
                continue
            # ⚠️ **约束住在 metadata 里,不在 annotation 上**(pydantic v2):
            #    只搬 info.annotation 会得到一个没有 ge/le/max_length 的裸类型 ——
            #    实测踩过:amount 的 maximum=100 会静默消失。
            #    所以用 Annotated[原类型, *原 metadata] 整套搬过来。
            annotated = info.annotation or Any
            if info.metadata:
                annotated = Annotated[tuple([annotated, *info.metadata])]
            # 原来的必填在这里一律放宽成可选;按 action 的必填写在工具说明里。
            fields[field_name] = (annotated, None)
    return create_model(name, **fields)  # type: ignore[call-overload]


def dispatch_action(
    payload: dict[str, Any] | None,
    table: tuple[tuple[str, type, Callable[[Any], Any]], ...],
) -> Any:
    """合并工具的唯一分发点:选动作 → 按那个动作的模型校验 → 返回处理器。

    顺序(照 la/tools/task_tools.py 的 goal/skill 两个分发器同一条口径):
      1. 说了 action:在表里找它 —— 找到就用它的模型校验(**校验失败原样抛出**);
         action 认不出 → ValueError(带上"该给什么"),**绝不猜**;
      2. 没说 action:按表顺序挨个试,第一个校验通过的算 ——
         比默认挑第一个分支安全(模型只给一堆字段时,由"谁校验得过"决定它想干什么)。

    ⚠️ 两个坑(都实测踩过):
       ① 表里的模型是**动作自己的输入模型**(HealthLogCreate / ProblemQuery …),
          不是"合并出来的那个大模型" —— 后者的 action 字段是必填,而 action
          在进这里之前已经被摘掉了,拿它校验必然失败;
       ② 每个动作的字段是模型实例,动作**附加字段**(health_plan 的 days、
          quiz_write 的 limit/format)只能从原始 payload 取 —— 所以处理器拿两个参数。
    """
    from pydantic import ValidationError

    data = {k: v for k, v in (payload or {}).items() if k != "action"}
    raw = str((payload or {}).get("action") or "").strip()
    if raw:
        for name, model, handler in table:
            if name == raw:
                return handler(model.model_validate(data))
        allowed = " / ".join(name for name, _m, _h in table)
        raise ValueError("action 只能是 " + allowed + ";收到的是 " + raw)
    for _name, model, handler in table:
        try:
            return handler(model.model_validate(data))
        except ValidationError:
            continue
    allowed = " / ".join(name for name, _m, _h in table)
    raise ValueError(
        "action 只能是 " + allowed + "。也可以不写 action,"
        "但那样参数必须能唯一对应到其中一个动作(这次没能对应上)。"
    )


#: 域 -> (表, 时间列)。⚠️ 一律用 **_live 视图 + owner 过滤**(2026-10-18 审计修):
#: 以前 ledger 数的是 app_txns(含软删除/作废),也不过滤成员 —— 模型回答
#: "我一共记了多少"时会把别人的账、删掉的账都算进去,还与 query_expenses 的数字打架。
_COVERAGE_SPECS: dict[str, tuple[str, str]] = {
    "ledger": ("app_txns_live", "ts"),
    "ledger_categories": ("app_categories", "id"),
    "calendar": ("app_events", "dtstart"),
    "health": ("app_measurements", "measured_at"),
    "study": ("app_problems", "created_at"),
}
#: 哪些覆盖域的表有 owner_id(没有的那张不能加 owner 条件)。
_COVERAGE_OWNED = frozenset({"ledger", "calendar", "health", "study"})


def with_coverage(result: Any, params: Any, domain: str, service: Any) -> Any:
    """按 with_coverage 开关,把覆盖域挂到结果上。

    ⭐ 开关是**显式**的:每轮都附一份覆盖域会白白吃掉输出预算,
    而"我记了多少"只在用户真问的时候才需要。字段在 schema 里看得见,
    模型自己会打开它 —— 这比再加一个"数据总览"工具便宜得多(工具清单不动、KV 缓存不炸)。
    """
    flag = bool(getattr(params, "with_coverage", False)) if params is not None else False
    if not flag or not isinstance(result, dict):
        return result
    payload = coverage_of(
        getattr(service, "conn", None), domain=domain,
        member_id=getattr(service, "member_id", None),
    )
    if payload is not None:
        result["coverage"] = payload
    return result


def coverage_of(
    conn: Any, *, domain: str, member_id: int | None = None
) -> dict[str, Any] | None:
    """「我现在看得到哪些数据」—— 只读工具靠它如实报覆盖域。

    为什么不让模型自己回答这个问题:它没有别的办法知道表里有多少行、
    数据从哪天开始。凭印象说"你记了挺多账"正是本项目的头号禁忌(数字必须来自查询)。
    只改"清单"一个字也没用 —— 得给出**行数与时间跨度**。

    ⚠️ 只读、零参数、不猜:表不存在就返回 None(调用方原样透出,不编数字)。
    """
    if conn is None:
        return None
    spec = _COVERAGE_SPECS.get(domain)
    if spec is None:
        return None
    table, time_column = spec
    where, params = "", []
    if member_id is not None and domain in _COVERAGE_OWNED:
        where = " WHERE (owner_id IS NULL OR owner_id = ?)"
        params = [int(member_id)]
    try:
        total = int(
            conn.execute("SELECT COUNT(*) FROM " + table + where, params).fetchone()[0]
        )
        bounds = conn.execute(
            "SELECT MIN(" + time_column + "), MAX(" + time_column + ") FROM "
            + table + where, params
        ).fetchone()
    except Exception:  # noqa: BLE001 - 覆盖域只是附注,查不到就说查不到
        return None
    # 配置时区的现在(与 settings.now 同一口径),不是 OS 本地时区。
    from .. import config as _config

    now = _config.get_settings().now()
    payload: dict[str, Any] = {
        "table": table,
        "rows": total,
        "earliest": bounds[0],
        "latest": bounds[1],
        "as_of": now.isoformat(timespec="seconds"),
    }
    if total:
        extra = _COVERAGE_EXTRA.get(domain)
        if extra is not None:
            try:
                payload["breakdown"] = extra(conn, member_id)
            except Exception:  # noqa: BLE001 - 附注查不到就不给,绝不编
                pass
    return payload


def _ledger_breakdown(conn: Any, member_id: int | None = None) -> dict[str, int]:
    """账本:按交易状态 + 按分录方向各来一份(金额是分,永远正数,方向在正负号上)。"""
    where, params = "", []
    join_where = ""
    if member_id is not None:
        where = " WHERE (owner_id IS NULL OR owner_id = ?)"
        join_where = " WHERE (t.owner_id IS NULL OR t.owner_id = ?)"
        params = [int(member_id)]
    by_status = {
        str(row["status"]): int(row["n"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM app_txns" + where
            + " GROUP BY status",
            params,
        ).fetchall()
    }
    direction = conn.execute(
        "SELECT SUM(CASE WHEN p.amount_cents > 0 THEN 1 ELSE 0 END) AS income,"
        " SUM(CASE WHEN p.amount_cents < 0 THEN 1 ELSE 0 END) AS expense"
        " FROM app_postings p JOIN app_txns t ON t.id = p.txn_id" + join_where,
        params,
    ).fetchone()
    return {
        "income_legs": int(direction["income"] or 0),
        "expense_legs": int(direction["expense"] or 0),
        **{"status_" + key: value for key, value in by_status.items()},
    }


def _study_breakdown(conn: Any, member_id: int | None = None) -> dict[str, int]:
    where, params = "", []
    if member_id is not None:
        where = " WHERE (owner_id IS NULL OR owner_id = ?)"
        params = [int(member_id)]
    return {
        str(row["status"]): int(row["n"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM app_problems" + where
            + " GROUP BY status",
            params,
        ).fetchall()
    }


_COVERAGE_EXTRA = {"ledger": _ledger_breakdown, "study": _study_breakdown}


def check_tool_path(path: str | None, *, must_exist: bool = True) -> str | None:
    """模型给的路径一律先过白名单(M6.5)。

    ⚠️ 这是**真的攻击面**:一次提示注入就能让 expense_import 去读 .env 或 ~/.ssh/id_rsa。
    用户自己敲的命令行不走这里 —— 那时候路径是人给的,不是模型给的。

    空串 / None 原样放过(表示"这次没传"),交给各服务自己判断必填。
    """
    if path is None or str(path).strip() == "":
        return path
    DEFAULT_POLICY.check(path, must_exist=must_exist)
    return path


def tool_from_model(
    *,
    name: str,
    description: str,
    handler: Callable[[dict[str, Any]], Any],
    model: type | None = None,
    params: dict[str, Any] | None = None,
    phase: str = PHASE_ANSWER,
    risk: str = RISK_LOW,
    category: str = CATEGORY_TASK,
    output_budget_tokens: int = 800,
    timeout_ms: int = 5000,
    required: tuple[str, ...] = (),
    blocking: bool = False,
) -> ToolSpec:
    """model 与 params 二选一:领域工具给 model(单真源),系统工具给手写 params。"""
    return ToolSpec(
        name=name,
        description=description,
        phase=phase,
        risk=risk,
        timeout_ms=timeout_ms,
        handler=handler,
        output_budget_tokens=output_budget_tokens,
        required=required,
        category=category,
        blocking=blocking,
        model=model,
        params=params or {},
    )


def build_health_tools(
    service: HealthService, *, actor: str = "agent", session_id: str | None = None
) -> list[ToolSpec]:
    """健康工具(⭐ 批次 B 合并:6 个 → 3 个)。

    ⚠️ 两条硬边界:① **不给医疗建议**;② 回答一律用"约 / 区间",不报精确值。

    合并的口径(见 docs/工具集瘦身对照表.md):只在**门禁字段一致**的工具之间合 ——
    health_log = 三个"记一笔"(都 risk=low / phase=answer / writes=True),
    health_plan = goal + trend + energy(同上)。字段约束仍来自各自的模型,没有第二份。
    """
    #: 三个"记一笔"的输入模型:并集 + action。同名同义的字段(ts / note)自动去重。
    HealthLog = merge_input_models(
        "HealthLog", ("meal", "workout", "checkin", "profile"),
        (FoodLogCreate, WorkoutCreate, MeasurementCreate, ProfileUpdate),
    )
    #: goal / trend / energy 三者参数几乎不重叠,合并主要省的是"少两个工具名"。
    HealthPlan = merge_input_models(
        "HealthPlan", ("goal", "trend", "energy"),
        (HealthGoalCreate, TrendQuery),
    )

    #: 动作表:(动作名, 该动作的输入模型, 处理器)。⚠️ 用**各自的原模型**而不是
    #: 合并出来的 HealthLog —— 后者要求 action 字段,而 action 在分发前已摘掉(实测踩过)。
    _log_table = (
        ("meal", FoodLogCreate,
         lambda p: service.log_food(p, actor=actor, session_id=session_id)),
        ("workout", WorkoutCreate,
         lambda p: service.log_workout(p, actor=actor, session_id=session_id)),
        ("checkin", MeasurementCreate,
         lambda p: service.log_measurement(p, actor=actor, session_id=session_id)),
        # ⭐ 2026-11-11 真机:用户说"出生年份 1985、身高一米七二",模型只能把身高记成
        #    一条测量,还回"系统里没有存年份的字段" —— 因为**没有改档案的工具**。
        #    档案(性别/出生年份/身高/活动水平)是能量收支的输入,必须能写。
        ("profile", ProfileUpdate,
         lambda p: service.update_profile(**p.model_dump(exclude_none=True)).as_dict()),
    )

    async def health_log(params: dict[str, Any]) -> dict[str, Any]:
        # ⭐ 2026-11-21(D188):一次运动至少要有一个维度。这一条必须判在**分发之前**:
        #    ① 交给 pydantic 的 model_validator 会变成 HANDLER_ERROR,自省报表会把它
        #       读成"工具坏了"(照 expense_query 的教训,见 la/domain/tools.py 那条注释);
        #    ② 更糟的是"没给 action 就挨个试"会落到 profile(全字段可选)上 ——
        #       成功返回,库里却什么都没写。
        #    抛 ToolInputError = BAD_PARAMS,文案是给模型看的,它会照着去问用户。
        if str((params or {}).get("action") or "").strip() == "workout":
            if not str((params or {}).get("activity") or "").strip():
                # "我今天做了运动" —— 连做了什么都不知道,更不该替他想一个。
                raise ToolInputError(
                    "还不知道是什么运动。先问用户:做了什么(跑步 / 仰卧起坐 / …),"
                    "再加上时长 / 距离 / 个数里的至少一个。"
                )
            if all(
                (params or {}).get(key) in (None, "")
                for key in ("duration_min", "distance_km", "reps")
            ):
                raise ToolInputError(
                    "这次运动一个维度都没给。先问用户:时长(多久)/ 距离(多远)/ 个数(多少个)"
                    "至少给一个,然后只记他给的那一个 —— 不要估算、不要编。"
                )
        return dispatch_action(params, _log_table)

    def _energy_days(payload: Any) -> int:
        """能量收支的回看天数(默认 7;与趋势的 days 语义不同,所以单独取)。

        ⚠️ 校验过的 TrendQuery 实例也会走到这里 —— 所以两种形状都要认。
        """
        if payload is None:
            return 7
        if isinstance(payload, dict):
            return int(payload.get("days") or 7)
        return int(getattr(payload, "days", None) or 7)

    #: ⚠️ energy 用 TrendQuery 校验(它只认 metric_code/days/with_coverage),
    #:    但 days 的**语义不同**(趋势最小 7 天,能量 1~90):能量那条分支从
    #:    原始 params 取 days,不走模型实例 —— 这是本次合并里唯一一处"共用模型、
    #:    各自解释",所以特意写在动作表里让读者看见。
    def _today_brief(_payload: Any) -> dict[str, Any]:
        """今天的快照,**砍到模型读得动的量**(睡眠点列/两周柱状图那些是给页面看的)。

        ⭐ 关键是 device.has_data:它是"还没同步过"与"今天真的没动"的分界。
        少了它,模型会把没数据说成"你今天没怎么走路"。
        """
        snap = service.today_snapshot()
        sleep = snap.get("sleep") or {}
        return {
            "date": snap["date"],
            "device": snap.get("device") or {},
            "rings": [
                {"metric": ring["metric"], "name": ring["name"], "value": ring["value"],
                 "target": ring["target"], "unit": ring["unit"], "done": ring["done"]}
                for ring in snap.get("rings") or []
            ],
            "sleep": {
                key: sleep.get(key) for key in
                ("has_data", "last_hours", "average_hours", "bedtime", "wake",
                 "regularity_minutes")
            },
            # ⭐ 2026-11-21(D188):两个维度都要给模型看。以前只回 minutes,
            #    距离/个数存了也等于没存 —— 模型只能回答"你今天跑了 30 分钟",
            #    答不了"跑了多远、做了多少个"。
            "workouts": [
                {"kind": item["kind"], "minutes": item["duration_min"],
                 "distance_km": item["distance_km"], "reps": item["reps"],
                 "amount_text": item.get("amount_text") or "", "ts": item["ts"]}
                for item in snap.get("workouts") or []
            ],
            "latest": snap.get("latest") or [],
            "source": "health",
            "note": "value=null 表示没有这一项数据,不是 0;device.has_data=false 表示还没从手机同步过",
        }

    def _synced_brief(_payload: Any) -> dict[str, Any]:
        coverage = service.coverage()
        return {
            "last_sync": coverage.get("last_sync"),
            "metrics": coverage.get("metrics") or [],
            "total": coverage.get("total", 0),
            "workouts": coverage.get("workouts", 0),
            "source": "health",
            "note": "曲线为空时先看这里:是没数据,还是同步没跑通",
        }

    _plan_table = (
        ("goal", HealthGoalCreate, service.create_goal),
        ("trend", TrendQuery, lambda p: with_coverage(
            service.trend(p).model_dump(), p, "health", service)),
        ("energy", TrendQuery, lambda p: {
            "period": service.energy_summary(days=_energy_days(p)),
            "from_history": service.estimate_tdee_from_history(
                days=max(_energy_days(p), 21)),
            "source": "health",
        }),
        # ⭐ 2026-11-11:健康数据现在有一部分是从 iPhone 健康同步来的。
        #   这两个动作让模型能回答"我今天怎么样 / 同步通了没",而不是只会看体重。
        ("today", TrendQuery, _today_brief),
        ("synced", TrendQuery, _synced_brief),
    )

    async def health_plan(params: dict[str, Any]) -> dict[str, Any]:
        return dispatch_action(params, _plan_table)

    return [
        tool_from_model(
            name="health_log",
            description=(
                "记一笔健康数据(action 四选一):meal=进食 / workout=运动 / checkin=身体测量 / "
                "profile=身体档案(sex 性别、birth_year 出生年份、height_cm 身高、activity_level 活动水平)。"
                "必填按动作:meal 要 food_name;workout 要 activity + **至少一个维度**"
                "(时长 duration_min / 距离 distance_km(公里)/ 个数 reps);"
                "checkin 要 metric_code + value;profile 至少给一个字段。"
                "⭐ 用户说出生年份/身高/活动量时用 profile —— 这些是能量收支的输入;"
                "身高记成 checkin 测量**不会**进档案(只有页面兜底会读最新身高)。"
                "⚠️ 数值一律用「约」—— 份量与读数本身都有误差;不给医疗建议。"
                "meal 第一次填了 kcal_per_unit 就进你自己的食物库,以后不用再填;"
                "⭐ workout 的两个维度:跑步/走路/骑车/游泳记 distance_km,"
                "仰卧起坐/俯卧撑/深蹲/跳绳记 reps;他说了一个就只记一个,两个都说了就都记。"
                "一个都没说**先问**,不要估算 —— 没记时长就不算消耗。"
                "workout 的量用 MET-分钟归一(需要时长),可跨类型累加。"
            ),
            model=HealthLog,
            handler=health_log,
            required=("action",),
        ),
        tool_from_model(
            name="health_plan",
            description=(
                "健康:today 今天(步数/活动/锻炼/睡眠,来自 iPhone 健康)/ trend 趋势(metric_code)/ "
                "energy 能量收支 / synced 同步覆盖 / goal 目标(target_value)。"
                "⭐ value=null 是没数据不是 0;device.has_data=false 就是还没同步,别说「今天没动」。"
                "趋势是 EWMA、约 10 天滞后,要主动说明;能量日误差 ±300~500 千卡,用「约/区间」,"
                "21 天后附历史反推。算不出时先看档案(性别/出生年份/身高/活动水平)缺哪项,"
                "用 health_log action=profile 补齐。**不给医疗建议。**"
            ),
            model=HealthPlan,
            handler=health_plan,
            required=("action",),
        ),
    ]


def build_study_tools(
    service: StudyService,
    *,
    actor: str = "agent",
    session_id: str | None = None,
    tts_engine: Any = None,
) -> list[ToolSpec]:
    """错题工具。

    ⚠️ 没有"算出下次复习日期"这种工具 —— 排程是 FSRS 的事,不是模型的事(能力域 §4.1)。
    """

    async def quiz_add_wrong_answer(params: dict[str, Any]) -> dict[str, Any]:
        payload = dict(params or {})
        for item in payload.get("image_paths") or []:
            check_tool_path(str(item))
        # 📷 素材编号 → 题目照片。⭐ 2026-11-01 #16–#21:模型把 material_list 里的
        #    「#12」当成了资产 id 传进来,而 12 是一张旧图形 —— 六道题全挂错图。
        #    现在照片只认 material_ids,换算是服务端的事(见 ProblemAddInput)。
        material_ids = payload.pop("material_ids", None) or []
        photo_note = ""
        if material_ids:
            assets = service.asset_ids_from_materials(material_ids)
            if assets:
                payload["asset_ids"] = assets
            else:
                photo_note = ("material_ids 里没有可用的图片素材(编号不存在,或不是图片)—— "
                              "这道题没有挂照片。")
        record = service.add_problem(payload, actor=actor, session_id=session_id)
        result = record.model_dump()
        if photo_note:
            result["photo_note"] = photo_note
        # 🔊 聊天里"录错题"、网页"手工录一道"都**不经过拍照识别**,而声音以前只在识别
        #    那条流水线里生成 —— 实测 #14(「语文听写涌现、结婚」)因此哑了三天。
        #    这里补上:纯字符串判断要念什么,做一次、绑在题上(与识别同一条纪律)。
        #    ⚠️ 失败只记在结果里,**不许**把录题判成失败:题已经进库了。
        try:
            bound = service.bind_audio(record.id, tts_engine=tts_engine, actor=actor)
        except Exception as exc:  # noqa: BLE001
            bound = {"needed": True, "audio_artifact_id": None,
                     "audio_error": type(exc).__name__ + ": " + str(exc)[:80]}
        if bound.get("needed"):
            result.update({
                "audio_artifact_id": bound.get("audio_artifact_id"),
                "audio_handle": bound.get("audio_handle"),
                "audio_label": bound.get("audio_label") or "",
                "audio_text": bound.get("audio_text") or "",
                "audio_note": bound.get("audio_note") or "",
                "audio_error": bound.get("audio_error") or "",
                # 这句话是给模型看的:它得在回复里说清"有声音"还是"这道题现在是哑的"。
                "audio_tip": (
                    "这道题要出声,音频已生成并绑定 —— 回复里说一句「音频做好了,"
                    "复习时点开就能听」;**不要**再调 tts_audio 重做一遍。"
                    if bound.get("audio_artifact_id") else
                    "这道题要出声,但音频**没做成**(" + str(bound.get("audio_error")
                    or bound.get("audio_note") or "未知原因") + ") —— 如实告诉用户这题现在是哑的,"
                    "不要说「做好了」,也不要编一段音频。"
                ),
            })
        return result

    async def quiz_list(params: dict[str, Any]) -> dict[str, Any]:
        query = ProblemQuery.model_validate(params or {})
        items = service.list_problems(query)
        return with_coverage(
            {
                "count": len(items),
                "problems": [item.model_dump() for item in items],
                "source": "study",
            },
            query, "study", service,
        )

    async def quiz_weak_points(params: dict[str, Any]) -> dict[str, Any]:
        stats = service.stats()
        return {
            "total_problems": stats.total_problems,
            "by_subject": stats.by_subject,
            "weak_points": [row.model_dump() for row in stats.weak_points],
            "due_today": stats.due_today,
            "source": "study",
        }

    async def quiz_me(params: dict[str, Any]) -> dict[str, Any]:
        payload = QuizMe.model_validate(params or {})
        queue = service.review_queue(subject=payload.subject, limit=payload.count)
        state = service.push_state()
        return {
            "count": len(queue),
            "cards": queue,
            "source": "study",
            "push": {
                "enabled": state["push_enabled"],
                "time": state["push_time_local"],
                "daily_limit": state["push_daily_limit"],
                "due_today": state["due_count"],
                "already_past_push_time": state["ready"],
            },
            "note": (
                "这是孩子端视图:只有题干、配图与「忘了/会了」两个按钮。"
                "⭐ 复习日期由排程算法算出,**不要自己推算**;"
                "每日推送时间用 quiz_write(action=push) 设定。"
                "🔊 卡片里带 audio_handle 的,音频**在录入时就已经生成并绑定在题上**——"
                "直接用那个句柄,**不要**再用 tts_audio 重做一遍(重做会得到另一段声音,而且白花钱)。"
            ),
        }

    async def quiz_make_sheet(params: dict[str, Any]) -> dict[str, Any]:
        payload = params or {}
        limit = payload.get("limit")
        size = int(limit) if limit else None
        fmt = str(payload.get("format") or "html").strip().lower()
        if fmt in ("md", "markdown", "text", "文字"):
            got = service.make_sheet(limit=size, session_id=session_id)
            return {
                "artifact_id": got["artifact"]["id"],
                "label": got["artifact"]["label"],
                "items": got["items"],
                "date": got["date"],
                "format": "md",
                "download_url": "/artifacts/" + str(got["artifact"]["id"]),
                "audios": got.get("audios", 0),
                "note": "错题卷(Markdown,**没有图形与音频**)已生成。"
                        "🔊 清单里带 /artifacts/... 的听力题可以直接引用那些句柄。"
                        "⭐ 要给孩子做 / 发到微信的卷子请用 format=html —— 图形、照片、音频全在里面。",
            }
        # ⭐ 默认走**单文件 HTML**:图形(矢量重绘 / 裁剪)、照片、听力音频全部**内联**,
        #    与网页端、命令行同一个渲染器(study/sheet.py),出卷这一步一行模型代码都没有。
        #    ⚠️ 别再让模型自己手写页面:实测它会漏掉图与声音(见 D110)。
        got = service.make_sheet_html(limit=size, session_id=session_id)
        delivered = bool(got.get("delivered"))
        return {
            "artifact_id": got["artifact"]["id"],
            "label": got["artifact"]["label"],
            "items": got["items"],
            "date": got["date"],
            "format": "html",
            "problem_audios": got.get("problem_audios", 0),
            "download_url": "/artifacts/" + str(got["artifact"]["id"]),
            "preview_url": "/generated/" + str(got["artifact"]["id"]),
            "delivered": delivered,
            "note": (
                "错题卷(单文件 HTML)已生成:图形、照片、听力音频**都已经在页面里**,"
                "双击能开、发到微信点开能用、也能打印。"
                + ("这份卷子已经登记为**本次回复的附件**,系统会把它发到当前聊天;"
                   "如果上传失败,系统会再发一条说明,到时候以那条说明为准。"
                   if delivered else
                   "⚠️ 这次**没有**登记发文件回执(当前会话没接回执层或回执写失败),"
                   "用户**收不到附件** —— **不许**说「发你了」;"
                   "告诉用户到网页「素材」页打开。")
                + "**不要**再自己写 HTML 页面重排题目(那样会丢掉图形与声音),"
                "也不必再调 tts_audio。"
            ),
        }

    async def tts_audio(params: dict[str, Any]) -> dict[str, Any]:
        """把文字变成语音,登记成产物,并给出**可直接粘进网页**的 <audio> 片段。

        ⭐ 为什么要有这个工具(用户 2026-10-12 的实际反馈):
        他在微信里让助手出一份带听力题的 HTML 试卷,结果**听力题不会发音** ——
        因为页面里根本没有音频,模型只能写"让家长念"。根因有两层:
          ① 助手没有"生成语音"的能力;
          ② 就算有音频文件,html_page 的内联器也只认图片。
        两层都补上了:这里生成音频并给出句柄,内联器认得 <audio src="/artifacts/<id>">。
        """
        from .study.tts import TtsEngine, parse_words, wav_to_mp3

        payload = params or {}
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("text 不能为空 —— 要说的话(听写时用逗号/顿号分隔多个词)")
        if service.artifacts is None:
            raise ValueError("现在没有产物层,音频存不下来")
        mode = str(payload.get("mode") or "say").strip().lower()
        engine = tts_engine or TtsEngine()
        voice = str(payload.get("voice") or "")
        if voice:
            engine.voice = voice
        if mode == "dictation":
            words = parse_words(text)
            audio = engine.build_dictation(
                words,
                repeat=int(payload.get("repeat") or 2),
                gap_s=float(payload.get("gap") or 4.0),
            )
            if not audio.ok:
                raise ValueError("发声失败:" + (audio.error or "未知原因"))
            body, media_type = audio.data, audio.media_type
            seconds, provider, voice_used = audio.seconds, audio.provider, audio.voice
            spent = audio.spent_yuan
            label = "听写音频 " + "、".join(words[:6])
        else:
            clip = engine.speak(text)
            if not clip.ok:
                raise ValueError("发声失败:" + (clip.error or "未知原因"))
            body, media_type = clip.data, "audio/wav"
            mp3, hint = wav_to_mp3(clip.data)
            if not hint:
                body, media_type = mp3, "audio/mpeg"
            seconds, provider, voice_used = clip.seconds, clip.provider, clip.voice
            spent = round(len(text) / 10000.0 * 0.8, 6)
            label = "朗读 " + text[:12]
        suffix = ".mp3" if media_type == "audio/mpeg" else ".wav"
        # ⚠️ 用服务自己的时钟(2026-10-18 审计修):以前既调私有方法、又拿 OS 的
        #    datetime.now(...tzinfo) 取"现在",两处时钟可能不是同一个。
        stamp = service.now().strftime("%Y%m%d-%H%M%S")
        artifact = service.artifacts.register_bytes(
            bytes(body),
            filename=("语音-" + stamp + suffix),
            label=label,
            kind=KIND_FILE,
            session_id=session_id,
            meta={"provider": provider, "voice": voice_used, "seconds": round(seconds, 2),
                  "text": text[:200], "mode": mode},
        )
        # ⭐ 音频也要能**直接发到聊天里**(2026-10-15 真机踩过):桥接层只认
        #    **artifact 域的回执**里的 artifact_id 去发文件 —— 光把字节登记进产物层、
        #    不写回执,用户在微信里什么都收不到,而模型还会以为"已经发过去了"
        #    (实测:它说了"跟读音频单独发你了",用户手里一个文件都没有)。
        receipt_id = 0
        if service.receipts is not None:
            try:
                receipt_id = service.receipts.record(
                    kind="artifact.file",
                    domain=DOMAIN_ARTIFACT,
                    label="语音:" + label,
                    detail={"artifact_id": artifact.id,
                            "name": label + suffix,
                            "bytes": artifact.byte_size},
                    undo_plan={
                        "type": "artifact", "artifact_id": artifact.id,
                        "name": label + suffix, "existed": False,
                        "path": str(service.artifacts.path_of(artifact)),
                    },
                    actor=actor,
                    session_id=session_id,
                )
                if receipt_id:
                    service.receipts.conn.commit()
            except Exception:  # noqa: BLE001 - 留痕失败不该让"音频已经做好"变成失败
                receipt_id = 0
        handle = "/artifacts/" + str(artifact.id)
        return {
            "artifact_id": artifact.id,
            "handle": handle,
            "audio_html": '<audio controls preload="metadata" src="' + handle + '"></audio>',
            "seconds": round(seconds, 2),
            "voice": voice_used,
            "provider": provider,
            "spent_yuan": spent,
            "delivered": bool(receipt_id),
            "file_name": label + suffix,
            "receipt_id": receipt_id,
            "note": (
                "⭐ 把 audio_html 那段**原样**放进 html_page 的页面里(写在需要播放的位置),"
                "保存时服务端会把音频内联成 data: —— 预览沙箱与微信里都能播。"
                "不要自己写 <audio src='xxx.mp3'> 这种相对路径(内联不到,页面会是哑的)。"
                + ("🔊 这段音频**已经作为文件发给用户了**(微信里会收到 "
                   + label + suffix + "),不用再说「发你了」这类话 —— 系统回执会写明。"
                   if receipt_id else
                   "⚠️ 这次**没有**登记发文件回执(会话没接回执层)—— **不许**说"
                   "「音频发你了」;用户只能在「素材」页里拿到它。")
            ),
        }

    async def quiz_set_push(params: dict[str, Any]) -> dict[str, Any]:
        payload = StudyPushSettings.model_validate(params or {})
        if not any(value is not None for value in payload.model_dump().values()):
            raise ValueError("至少要给一个要改的字段(enabled / push_time / daily_limit / student_name)")
        updated = service.update_settings(payload, actor=actor)
        return {
            "ok": True,
            "settings": updated.model_dump() if hasattr(updated, "model_dump") else updated.as_dict(),
            "note": (
                "已生效。推送由**确定性调度器**按这个时间发,不经过模型、也不需要模型在线;"
                "到点会生成一条当日复习清单。改时间不需要重启。"
            ),
        }

    async def quiz_record_result(params: dict[str, Any]) -> dict[str, Any]:
        payload = QuizResult.model_validate(params)
        if payload.problem_id and not payload.unit_id:
            # ⭐ 孩子端是"一道题一张卡"(D108):一次作答记到这道题今天到期的所有单元上
            outcome = service.record_problem_review(
                payload.problem_id, payload.rating, elapsed_ms=payload.elapsed_ms,
                actor=actor, session_id=session_id,
            )
            return {
                "problem_id": outcome["problem_id"],
                "rating": outcome["rating"],
                "rating_label": RATING_LABELS.get(payload.rating, str(payload.rating)),
                "units": outcome["units"],
                "next_due_local": outcome["due_date_local"],
            }
        if not payload.unit_id:
            raise ValueError("要给 unit_id 或 problem_id(孩子端卡片里那个 problem_id)")
        result = service.record_review(
            payload.unit_id, payload.rating, elapsed_ms=payload.elapsed_ms,
            actor=actor, session_id=session_id,
        )
        return {
            **result.as_dict(),
            "rating_label": RATING_LABELS.get(payload.rating, str(payload.rating)),
            "next_due_local": result.due_date_local,
        }

    async def quiz_delete_problem(params: dict[str, Any]) -> dict[str, Any]:
        """删掉一道**录错了**的错题(有复习记录的会自动改成归档)。"""
        payload = ProblemDeleteInput.model_validate(params or {})
        outcome = service.delete_problem(
            payload.problem_id, reason=payload.reason, actor=actor, session_id=session_id,
        )
        if not outcome.get("deleted"):
            # 没删成,但也不是失败:它已经归档了 —— 模型要如实说出来,而不是报"删好了"。
            outcome["ok"] = False
        else:
            outcome["ok"] = True
        return outcome

    async def quiz_add_knowledge_point(params: dict[str, Any]) -> dict[str, Any]:
        point = service.upsert_knowledge_point(KnowledgePointCreate.model_validate(params))
        return point.as_dict()

    # -- 批次 B 合并后的两个入口(9 个 → 2 个)--------------------------------
    #: 只读三种:list(列表)/ me(今日清单)/ weak(薄弱点)。weak 零参数。
    QuizFind = merge_input_models(
        "QuizFind", ("list", "me", "weak"), (ProblemQuery, QuizMe)
    )
    #: 会写六种:add / result / push / know / sheet / delete。
    QuizWrite = merge_input_models(
        "QuizWrite", ("add", "result", "push", "know", "sheet", "delete"),
        # ⚠️ add 用 ProblemAddInput(照片认 material_ids),不是 ProblemCreate ——
        #    2026-11-01 #16–#21:模型把素材编号当资产编号,六道题挂上了同一张旧图。
        # ⚠️ delete 放最后:它的 problem_id 说明要同时讲清"result 时与 unit_id 二选一"。
        (ProblemAddInput, QuizResult, StudyPushSettings, KnowledgePointCreate,
         ProblemDeleteInput),
    )

    async def quiz_find(params: dict[str, Any]) -> dict[str, Any]:
        """错题查询:list(默认)/ me(今日清单)/ weak(薄弱点)。"""
        raw = str((params or {}).get("action") or "")
        if raw == "weak":
            return await quiz_weak_points(params)
        # ⚠️ list 有自己的筛选模型(ProblemQuery),me 是 QuizMe;都不带 action 字段,
        #    所以这里用"动作自己的模型"校验(见 dispatch_action 的坑①)。
        table = (
            ("list", ProblemQuery, lambda p: quiz_list(p.model_dump(exclude_unset=True))),
            ("me", QuizMe, lambda p: quiz_me(p.model_dump(exclude_unset=True))),
        )
        return await dispatch_action(params, table)

    async def quiz_write(params: dict[str, Any]) -> dict[str, Any]:
        """错题写入:add / result / push / know / sheet / delete。

        ⚠️ sheet 与 push 的参数不等于任何输入模型:
           * sheet 只有 limit / format(并进 QuizWrite 的并集,schema 里看得见);
           * push 用 StudyPushSettings —— "一个字段都没给"要如实报错,
             不能悄悄当成一次成功的空更新。
        """
        raw = str((params or {}).get("action") or "")
        # ⭐ 给了 format / limit 就是在要卷子 —— 这两个字段**只属于 sheet**
        #    (别把它误判成 add 或 result)。实测踩过:老调用只有 {"format": "md"},
        #    按模型挨个试都不过,最后报"没能对应上任何一个动作"。
        if not raw and (params or {}).get("format") is not None:
            raw = "sheet"
        if not raw and (params or {}).get("limit") is not None:
            raw = "sheet"
        if raw == "sheet":
            # sheet 的参数(limit/format)不属于任何输入模型 —— 直接进原处理器。
            return await quiz_make_sheet(params)
        if raw == "push":
            # ⚠️ push 单独一步:"一个字段都没给"要如实报错,
            #    不能悄悄当成一次成功的空更新。
            payload = StudyPushSettings.model_validate(params or {})
            if not any(value is not None for value in payload.model_dump().values()):
                raise ValueError(
                    "至少要给一个要改的字段(enabled / push_time / daily_limit / student_name)"
                )
            return await quiz_set_push(payload.model_dump())
        if raw == "delete":
            # ⚠️ 删除**必须显式给 action=delete**:不能落到"没给 action 就挨个试"那条路上 ——
            #    它删的是库里的行,猜错一次就没了。所以不进下面的 dispatch 表。
            return await quiz_delete_problem(params)
        table = (
            ("add", ProblemAddInput,
             lambda p: quiz_add_wrong_answer(p.model_dump(exclude_unset=True))),
            ("result", QuizResult,
             lambda p: quiz_record_result(p.model_dump(exclude_unset=True))),
            ("know", KnowledgePointCreate,
             lambda p: quiz_add_knowledge_point(p.model_dump(exclude_unset=True))),
        )
        return await dispatch_action(params, table)

    return [
        # ⭐ 批次 B 合并(2026-10-15):原来 9 个错题工具 → 2 个(见 docs/工具集瘦身对照表.md)。
        #    拆的是**只读/会写**这一刀,不是按域拆 —— 只读白名单按工具名判定,
        #    读写混并会击穿只读模式(G18)。两个工具的门禁声明都一致(risk=low / phase=answer)。
        tool_from_model(
            name="quiz_find",
            description=(
                "错题查询(action 三选一):list=按学科/知识点/状态列出错题(默认;"
                "按 subject/knowledge_point/status/limit 筛,带 with_coverage)/ "
                "me=取今天的复习清单(孩子端视图;用 subject/count);"
                "weak=看薄弱知识点分布(依据是累计答错次数,不是感觉,零参数)。"
                "⭐ 复习日期由排程算法算出,**不要自己推算**;"
                "每日推送时间用 quiz_write(action=push) 设定。"
                "🔊 卡片里带 audio_handle 的,音频**在录入时就已经生成并绑定在题上** —— "
                "直接用那个句柄,**不要**再用 tts_audio 重做一遍(重做会得到另一段声音,而且白花钱)。"
            ),
            model=QuizFind,
            handler=quiz_find,
            required=("action",),
        ),
        tool_from_model(
            name="quiz_write",
            description=(
                "错题写入:action=add 录入一道错题(要 subject)/ result 记录一次复习结果(要 rating)/ "
                "push 设定每日复习推送 / know 维护知识点与别名 / sheet 生成今天的错题卷 / "
                "delete 删掉一道**录错了**的题(只传 problem_id;没复习过的真删,复习过的自动改成归档)。"
                "⭐ add:尽量把涉及的知识点分开写 —— 每个知识点会各生成一个**独立调度**的复习单元,"
                "这样才知道孩子到底卡在哪一步;"
                "📷 照片:聊天里收到的图传 material_ids(素材清单里的 #N)—— 系统会换成题目照片;"
                "本地文件才用 image_paths。⚠️ **不要**传 asset_ids:素材编号和资产编号不是一回事,"
                "传错会挂上一张不相干的图(2026-11-01 实测,#16–#21 六道题挂了同一张旧图形)。"
                "⚠️ 听写/听力/认读题:题干或答案里**要写清要念什么**(例如「语文听写:涌现、结婚」,"
                "或把词表放在 answer 里)—— 录入时系统会按它生成音频并绑在题上;"
                "返回里的 audio_handle / audio_note / audio_error 就是结果,回复时照实说。"
                "🗣 短语/句子要跟读时,题干写成「英语跟读：take care of」—— 系统会**整句连读**,"
                "不会拆成单个词(拆开就不是短语了);真是一串词才写「跟读单词:Monday、Tuesday」。"
                "⚠️ 用户说「录错了」时用 delete 清掉,不要用 push/result 绕。"
                "⭐ result:孩子端只传 1(忘了)或 3(会了),2/4 是家长端的细分;"
                "**传 problem_id**(卡片里那个),**不要**逐个单元去记 —— 一道题可能挂着好几个复习单元,"
                "按题记一次系统会一起记上、回执也只有一条(逐个记会写出一长串重复回执,实测踩过);"
                "unit_id 只在家长端明确要「就记这一个单元」时才用。"
                "⭐ sheet:用户说「出一份今天的错题卷 / HTML 试卷 / 打印一份错题」时用它,"
                "默认 format=html(单文件网页,图形/照片/听力音频都已内联)—— "
                "**不要自己写页面重排题目**,也不要为了出卷再调 tts_audio 或 html_page。"
                "⭐ push:用户说「以后每天晚上七点半提醒孩子复习错题」时用它,**不要**为此去设日程提醒;"
                "推送由确定性调度器发,与模型在不在线无关。"
            ),
            model=QuizWrite,
            handler=quiz_write,
            required=("action",),
        ),
        tool_from_model(
            name="tts_audio",
            description=(
                "把一段文字变成**语音**,登记成产物,并返回一段可直接粘进网页的 <audio> 片段。"
                "⭐ 用户要「听力题 / 听写 / 朗读 / 让孩子听写单词」时**必须**用它 —— "
                "否则生成的页面里没有音频,听力题是哑的(只能写'让家长念')。"
                "用法:先调它,把返回的 audio_html 原样写进 html_page 页面里再保存。"
                "mode=dictation:按「读一遍 → 停 N 秒 → 再读一遍」的听写节奏读词表"
                "(text 里用逗号或顿号分隔);mode=say:正常朗读一遍。"
                "⚠️ 错题**自带的**听力/听写音频在录入时就已生成并绑定(题目/卡片里带 "
                "audio_handle)—— 那种情况**直接用句柄**,不要用这个工具重做一遍。"
            ),
            params={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要说的话;dictation 模式下是词表(逗号/顿号分隔)"},
                    "mode": {"type": "string", "enum": ["say", "dictation"],
                             "description": "say=朗读一遍(默认);dictation=听写节奏"},
                    "voice": {"type": "string", "description": "音色,默认取设置(如 Cherry / Ethan)"},
                    "repeat": {"type": "integer", "description": "dictation:每个词读几遍(默认 2)"},
                    "gap": {"type": "number", "description": "dictation:两遍之间停几秒(默认 4)"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=tts_audio,
        ),
        # (原 quiz_add_knowledge_point 已并入 quiz_write 的 action=know)
    ]


def build_calendar_tools(
    service: CalendarService, *, actor: str = "agent", session_id: str | None = None
) -> list[ToolSpec]:
    """日程工具。⚠️ 参数只收**具体时刻**:把"下周三下午三点"解析成 ISO8601 是调用方的事
    (模型擅长这个),而把重复规则展开成实例是代码的事 —— 分工不能反。"""

    async def calendar_schedule(params: dict[str, Any]) -> dict[str, Any]:
        event = service.create_event(params, actor=actor, session_id=session_id)
        return event.as_dict()

    async def calendar_list(params: dict[str, Any]) -> dict[str, Any]:
        payload = CalendarList.model_validate(params)
        occurrences, truncated = service.list_occurrences(
            start=payload.start, end=payload.end, limit=payload.limit
        )
        return with_coverage(
            {
                "start": payload.start,
                "end": payload.end,
                "count": len(occurrences),
                "truncated": truncated,
                "occurrences": [item.model_dump() for item in occurrences],
                "source": "calendar",
            },
            payload, "calendar", service,
        )

    async def calendar_reschedule(params: dict[str, Any]) -> dict[str, Any]:
        payload = CalendarReschedule.model_validate(params)
        return service.reschedule(payload.model_dump(), actor=actor, session_id=session_id)

    async def calendar_cancel(params: dict[str, Any]) -> dict[str, Any]:
        payload = CalendarCancel.model_validate(params)
        return service.cancel(payload.model_dump(), actor=actor, session_id=session_id)

    #: 批次 B:取消与改期合成一个(两者都是 risk=confirm / phase=answer,
    #: 所以合并**不放宽确认框** —— 这一点比省字节重要)。
    CalendarChange = merge_input_models(
        "CalendarChange", ("cancel", "reschedule"), (CalendarCancel, CalendarReschedule)
    )

    async def calendar_change(params: dict[str, Any]) -> dict[str, Any]:
        """取消或改期:action=cancel / reschedule。"""
        table = (
            ("cancel", CalendarCancel,
             lambda p: service.cancel(p.model_dump(exclude_unset=True),
                                      actor=actor, session_id=session_id)),
            ("reschedule", CalendarReschedule,
             lambda p: service.reschedule(p.model_dump(exclude_unset=True),
                                          actor=actor, session_id=session_id)),
        )
        return dispatch_action(params, table)

    return [
        tool_from_model(
            name="calendar_schedule",
            description=(
                "建一个日程。start 必须是 ISO8601 具体时刻(先把「下周三下午三点」算成时刻再传)。"
                "重复事件给 rrule(RFC5545 原文,例如 FREQ=WEEKLY;BYDAY=MO)。"
            ),
            model=CalendarEventCreate,
            handler=calendar_schedule,
            required=("title", "start"),
        ),
        tool_from_model(
            name="calendar_list",
            description="列出某个时间窗内的日程;重复事件会展开成一个个实例。",
            model=CalendarList,
            handler=calendar_list,
            required=("start", "end"),
        ),
        tool_from_model(
            name="calendar_change",
            description=(
                "改日程:action=cancel 取消 / action=reschedule 改期(要 uid,改期还要 new_start)。"
                "⭐ 两个动作都**要过确认**:给 recurrence_id 表示只对那一次生效"
                "(取消=EXDATE;改期=只改那一次),reschedule 还可以用 this_and_future=true "
                "表示此次及以后都改;都不给则作用于整个系列。"
            ),
            model=CalendarChange,
            handler=calendar_change,
            risk=RISK_CONFIRM,
            required=("action", "uid"),
        ),
    ]


def build_ledger_tools(
    service: LedgerService,
    *,
    actor: str = "agent",
    session_id: str | None = None,
    direct_write: bool = False,
    ask: AskHandler | None = None,
) -> list[ToolSpec]:
    """账本工具。⚠️ direct_write 决定助手记的账是草稿还是已入账(M7)。

    直写模式下不再有"请到账本页确认"这一步,安全网换成**写入回执 + 一键撤销**
    (ops_receipts + LedgerService.revert_txn)。默认 False 保持老行为。

    ⭐ M9 的**分类决策链**(本域最重要的一条业务规则,所以写在工具层):

        用户没说分类
          -> 规则引擎命中?           命中就用它(用户自己的纠正学出来的,证据最强)
          -> 没命中:返回 needs_category + 排好序的候选,**先不写账**
                ├─ 模型挑一个已有分类 -> 再调一次 expense_record
                └─ 都不合适           -> category_create(**会弹确认框给用户**)
                        ├─ 用户点了"新建" -> 建好,回执可撤销
                        └─ 用户没同意     -> 按他的选择改用已有分类,或者这笔先不记

    ⚠️ 为什么不"先记下来再改":一笔没有分类的账会立刻污染月度统计(未分类那一格),
       而事后纠正还得再问一次用户。**先定分类再落账**才是对的口径 —— 代价是模型多跑一轮。
    """

    def _needs_category(payload: TxnCreate, advice: Any) -> dict[str, Any]:
        """规则没命中时回给模型的形状。**不是错误** —— 是"还差一个决定"。"""
        return {
            "recorded": False,
            "needs_category": True,
            "amount_text": format_cents(payload.amount_cents),
            "direction": payload.direction,
            "payee": payload.payee,
            "narration": payload.narration,
            "candidates": [item.as_dict() for item in advice.candidates],
            "known_categories": advice.known,
            "what_now": (
                "这一笔**还没有写进账本**。请二选一:"
                "① 从 candidates 里挑一个,照原参数再调一次 expense_record,只补上 category;"
                "② 候选都不合适,就用 category_create 提一个新分类"
                "(它会弹确认框给用户,用户点了同意才真的建),建好之后再调 expense_record 记这笔。"
                "拿不准就先用 ask_user 问用户。**不要**自己编一个不在 known_categories 里的分类名。"
            ),
        }

    def _kind_of_category(name: str) -> str | None:
        try:
            return CategoryAdvisor(service.conn).kind_of(name)
        except Exception:      # 查不到就当不知道,别让记账失败
            return None

    async def expense_record(params: dict[str, Any]) -> dict[str, Any]:
        payload = TxnCreate.model_validate(params or {})
        if not payload.category:
            advice = service.advise_category(
                payee=payload.payee,
                narration=payload.narration,
                direction=payload.direction,
                account=payload.account,
                amount_cents=payload.amount_cents,
                ts=payload.ts,
            )
            if advice.rule_category:
                # 规则是用户自己纠正出来的 -> 直接用
                payload = payload.model_copy(update={"category": advice.rule_category})
            else:
                return _needs_category(payload, advice)
        record = service.record_expense(
            payload,
            actor=actor,
            session_id=session_id,
            status="confirmed" if direct_write else "draft",
        )
        result = record.model_dump()
        result["recorded"] = True
        result["amount_text"] = format_cents(record.amount_cents)
        result["direction_label"] = DIRECTION_LABELS.get(record.direction, record.direction)
        return result

    async def expense_query(params: dict[str, Any]) -> dict[str, Any]:
        # ⚠️ 2026-11-17 修:period 写错(以前单日 2026-11-16 不被接受)属于**参数错误**,
        #    不该被注册表记成 HANDLER_ERROR —— 自省报表会把它读成"工具坏了"。
        #    这里只做翻译,不吞错:错误码变成 BAD_PARAMS,文案仍是给模型看的那句。
        try:
            payload = ExpenseQuery.model_validate(params or {})
        except ValidationError as exc:
            detail = exc.errors()[0].get("msg", "") if exc.errors() else ""
            raise ToolInputError("参数不合法: " + str(detail or exc)) from exc
        try:
            result = service.query_expenses(payload)
        except LedgerError as exc:
            raise ToolInputError(str(exc)) from exc
        return with_coverage(result.model_dump(), payload, "ledger", service)

    async def expense_confirm(params: dict[str, Any]) -> dict[str, Any]:
        # ⚠️ 过 schema 校验(2026-10-18 审计修):以前是手写 JSON Schema + model=None,
        #    txn_id 只检查"有没有给",给了 "abc" 会在 int() 处炸成 HANDLER_ERROR。
        payload = ExpenseConfirm.model_validate(params or {})
        record = service.confirm(payload.txn_id, actor=actor, session_id=session_id)
        return record.model_dump()

    async def expense_correct_last(params: dict[str, Any]) -> dict[str, Any]:
        payload = ExpenseCorrect.model_validate(params)
        record = service.correct_last(
            category=payload.category,
            payee=payload.payee,
            amount_cents=payload.amount_cents,
            txn_id=payload.txn_id,
            actor=actor,
            session_id=session_id,
            learn=payload.learn,
        )
        return record.model_dump()

    async def expense_import(params: dict[str, Any]) -> dict[str, Any]:
        payload = ExpenseImport.model_validate(params)
        check_tool_path(payload.path)
        report = service.import_statement(
            payload.path, source=payload.source, actor=actor, session_id=session_id,
            dry_run=payload.dry_run,
        )
        result = report.as_dict()
        result["summary"] = report.summary()
        return result

    async def ledger_categories(params: dict[str, Any]) -> dict[str, Any]:
        payload = CategoryList.model_validate(params or {})
        return {
            "expense_categories": service.list_categories("expense")
            if payload.kind in (None, "expense") else [],
            "income_categories": service.list_categories("income")
            if payload.kind in (None, "income") else [],
            "note": "记账时 category 只能从这两份清单里取;要新增就走 category_create(需要用户点头)。",
        }

    async def category_create(params: dict[str, Any]) -> dict[str, Any]:
        payload = CategoryCreate.model_validate(params or {})
        name = payload.name.strip()
        known = service.list_categories(payload.kind)
        if name in known:
            return {
                "created": False, "status": "exists", "name": name, "kind": payload.kind,
                "note": "「" + name + "」已经在账本里了,直接用这个分类记账即可。",
            }
        if ask is None:
            return {
                "created": False, "status": "unavailable", "name": name, "kind": payload.kind,
                "note": "当前没有可用的提问通道,无法征得用户同意 —— 请改用已有分类:"
                        + " / ".join(known),
            }

        advice = service.advise_category(
            payee=payload.sample_payee, narration=payload.reason, direction=payload.kind
        )
        alternatives = [item for item in advice.candidates if item.name != name][:2]
        options = [
            Option(
                label="新建「" + name + "」",
                value="create",
                description=(payload.reason or "以后这类账都归它")[:40],
            )
        ]
        for item in alternatives:
            options.append(
                Option(label="用「" + item.name + "」", value="use:" + item.name,
                       description=item.why)
            )
        options.append(
            Option(label="先不建,这笔也先不记", value="cancel", description="等你想好了再说")
        )
        prompt = ("账本里还没有「" + name + "」这个" + DIRECTION_LABELS[payload.kind]
                  + "分类,新建吗?")
        if payload.sample_payee:
            prompt += "(" + payload.sample_payee + " 这笔)"
        answer = await ask(
            Question(
                prompt=prompt,
                options=options,
                allow_text=True,
                domain="ledger",
                # 上下文用**中文键**:它是直接摊给用户看的,不该出现 name/kind 这种字段名。
                context={
                    "分类": name,
                    "类型": DIRECTION_LABELS[payload.kind],
                    "原因": payload.reason or "",
                    "对方": payload.sample_payee or "",
                    "可改用": [item.name for item in alternatives],
                },
                source="domain",
                session_id=session_id,
            )
        )
        if answer.status != "answered":
            return {
                "created": False, "status": answer.status, "name": name, "kind": payload.kind,
                "note": "用户没有确认新建分类(" + (answer.note or answer.status) + ")。"
                        "**不要**假定他同意了:这笔可以先不记,或者改用已有分类 —— 已有分类:"
                        + " / ".join(known),
            }
        choice = answer.chosen()
        if choice == "create":
            result = service.create_category(
                name=name, kind=payload.kind, reason=payload.reason or "",
                actor=actor, session_id=session_id,
            )
            result["what_now"] = "分类已建好,现在可以再调一次 expense_record 把这一笔记到它下面。"
            return result
        if choice.startswith("use:"):
            picked = choice[4:]
            return {
                "created": False, "status": "use_existing", "name": picked,
                "kind": _kind_of_category(picked) or payload.kind,
                "note": "用户选择改用已有分类「" + picked + "」,请把这一笔记到它下面。",
            }
        if choice == "cancel" or not choice:
            return {
                "created": False, "status": "declined", "name": name, "kind": payload.kind,
                "note": "用户决定先不建这个分类,也先不记这笔。**就此停下**,别再追问。",
            }
        return {
            "created": False, "status": "answered_text", "name": name, "kind": payload.kind,
            "user_said": answer.text or choice,
            "note": "用户自己写了回答。按他的意思办:说的若是已有分类就直接用它记账;"
                    "说的若是别的新名字,再用 category_create 确认一次。",
        }

    return [
        tool_from_model(
            name="expense_record",
            description=(
                "记一笔账(支出或收入)。金额单位是**分**(38 元写 3800),永远填正数,"
                "方向用 direction 区分:expense=支出(默认),income=收入(工资/报销/退款/红包)。"
                "⭐ 参数里**不要传 ts**:用户说「昨天」时,先把日期算成 ISO8601 再传。"
                "⭐ 分类:只有在确定这个分类**已经在账本里**时才填 category"
                "(不确定就先调 ledger_categories 看一眼)。留空也没关系 —— "
                "留空会先走规则引擎;规则没命中时工具会返回 needs_category 与候选清单,"
                "由你挑一个再记,或者用 category_create 提新的(那会弹确认框给用户)。"
                "**不要**瞎编一个不在清单里的分类名。"
            ),
            model=TxnCreate,
            handler=expense_record,
            required=("amount_cents",),
        ),
        tool_from_model(
            name="expense_query",
            description=(
                "查询某个时间段的收支统计:回答「花了多少」「收了多少」「花在哪」「结余多少」必须用它。"
                "direction 选 expense(默认)/income/all;报「这个月的账」时用 all —— "
                "它会同时给出支出、收入与结余。所有数字由 SQL 聚合得出,不要自己算。"
                "⭐ **要看每一笔就打开 detail=true** —— 那时候返回里多一份 txns"
                "(逐笔的时刻/金额/对方/备注/分类/账户/状态,按时刻倒序),"
                "回答「每一天的每一笔」「都买了什么」「列出来给我看」「对一下账」必须用它;"
                "不打开的话只有**按天/按分类的合计**,同一天里那几笔各是多少是看不到的。"
                "⚠️ 别把 sample_txn_ids 当明细:它只是 5 个样例 id。"
            ),
            model=ExpenseQuery,
            handler=expense_query,
            required=("period",),
            # ⚠️ 明细会真吃预算:200 笔大约 1.5~2k token。默认 800 是给聚合留的,
            #    开了 detail 就可能被截断 —— 这里按"最坏情况"报账(同 deadline 那条纪律)。
            output_budget_tokens=2400,
        ),
        tool_from_model(
            name="ledger_categories",
            description=(
                "看账本里已有的分类清单(支出/收入分开)。"
                "记账或纠正之前不确定分类名时**先看它**,不要凭印象编。"
            ),
            model=CategoryList,
            handler=ledger_categories,
        ),
        tool_from_model(
            name="category_create",
            description=(
                "提一个**新分类**。⚠️ 调它不会直接建好:系统会弹一个确认框给用户,"
                "他点了同意才真的建,然后你再调 expense_record 把账记进去。"
                "只在候选分类都不合适时用;用户不同意时按返回的 status 处理,不要反复问同一个。"
            ),
            model=CategoryCreate,
            handler=category_create,
            timeout_ms=600000,   # 里面要等用户点确认框
            required=("name",),
        ),
        tool_from_model(
            name="expense_correct_last",
            description=(
                "纠正一笔账(分类/对方/金额)。不传 txn_id 就纠正最近一笔;"
                "纠正后会自动写成规则,下次同样的商户自动分对。"
            ),
            model=ExpenseCorrect,
            handler=expense_correct_last,
        ),
        tool_from_model(
            name="expense_import",  # 批量写入:直写模式下也直接执行(M7)
            description=(
                "导入支付宝/微信账单文件(csv 或 xlsx)。支出与收入都会入账。"
                "同一个文件重复导入会被 sha256 直接短路;逐行还有三级去重。"
            ),
            model=ExpenseImport,
            handler=expense_import,
            risk=RISK_CONFIRM,  # 批量写入必须经用户确认
            timeout_ms=60000,
            required=("path",),
            # 解析整份账单是同步的(csv/xlsx → 逐行三级去重),抢不动;超预算要记账(D105)
            blocking=True,
        ),
        tool_from_model(
            name="expense_confirm",
            description="把一笔草稿交易确认入账。只有用户明确同意后才能调用。",
            model=ExpenseConfirm,
            handler=expense_confirm,
            risk=RISK_CONFIRM,
            required=("txn_id",),
        ),
    ]

def build_note_tools(
    service: NotesService, *, actor: str = "agent", session_id: str | None = None
) -> list[ToolSpec]:
    """笔记工具:读 / 写 / 整理 三个(**按 risk 切,不按域切**)。

    ⚠️ 为什么"删/移"必须单独一个工具:risk 是 per-tool 的单值 ——
       把删除并进 low 的写入工具,等于在非直写部署里**取消确认**
       (docs/工具集瘦身对照表 §3.3 把这条列为"不可合并")。

    ⚠️ 描述里写死**反向指引**(笔记 ≠ 永久事实)。这不是洁癖:一条散文写进事实表,
       会跟着每一轮对话注入上下文;而"我不吃香菜"写进笔记,下次它就记不住了。
    """

    async def note_add(params: dict[str, Any]) -> dict[str, Any]:
        table = (
            ("add", NoteAdd,
             lambda p: service.add(p.model_dump(), actor=actor, session_id=session_id)),
            ("append", NoteAppend,
             lambda p: service.append(p.model_dump(), actor=actor, session_id=session_id)),
            ("edit", NoteEdit,
             lambda p: service.edit(p.model_dump(), actor=actor, session_id=session_id)),
            ("mkdir", NoteMkdir,
             lambda p: service.mkdir(p.model_dump(), actor=actor, session_id=session_id)),
        )
        return dispatch_action(params, table)

    async def note_find(params: dict[str, Any]) -> dict[str, Any]:
        table = (
            ("search", NoteSearch, lambda p: service.search(p.model_dump())),
            ("tree", NoteTree,
             lambda p: {"folders": service.tree(), "inbox": "收件箱"}),
            ("recent", NoteRecent, lambda p: service.recent(p.model_dump())),
            ("show", NoteShow, lambda p: service.show(p.model_dump())),
        )
        return dispatch_action(params, table)

    async def note_change(params: dict[str, Any]) -> dict[str, Any]:
        table = (
            ("move", NoteMove,
             lambda p: service.move(p.model_dump(), actor=actor, session_id=session_id)),
            ("rename", NoteRename,
             lambda p: service.rename_folder(p.model_dump(), actor=actor,
                                             session_id=session_id)),
            ("delete", NoteDelete,
             lambda p: service.delete(p.model_dump(), actor=actor, session_id=session_id)),
            ("restore", NoteRestore,
             lambda p: service.restore(p.model_dump(), actor=actor, session_id=session_id)),
        )
        return dispatch_action(params, table)

    NoteWrite = merge_input_models(
        "NoteWrite", ("add", "append", "edit", "mkdir"),
        (NoteAdd, NoteAppend, NoteEdit, NoteMkdir),
    )
    NoteFind = merge_input_models(
        "NoteFind", ("search", "tree", "recent", "show"),
        (NoteSearch, NoteTree, NoteRecent, NoteShow),
    )
    NoteChange = merge_input_models(
        "NoteChange", ("move", "rename", "delete", "restore"),
        (NoteMove, NoteRename, NoteDelete, NoteRestore),
    )

    return [
        tool_from_model(
            name="note_find",
            description=(
                "翻笔记。用户说「我记过 / 我之前写的 / 翻一下笔记 / 整理一下我最近的」,"
                "或问的是**某件事的经过与感想**(不是金额、不是日程、不是数值)时,"
                "**先查这里再回答**;引用时说清编号与日期(例如「你 11-05 记的那条(#37)」)。"
                "action=search 关键词检索 / tree 看夹结构 / recent 最近几条 / show 取一条正文。"
            ),
            model=NoteFind,
            handler=note_find,
            phase=PHASE_ANSWER,
            category=CATEGORY_RESEARCH,
            output_budget_tokens=1200,
            required=("action",),
        ),
        tool_from_model(
            name="note_add",
            description=(
                "记笔记:随笔、球后感、写作灵感、摘录。**这不是永久事实** ——"
                "身份/偏好/账号这类跨情境的短事实走 memory_note;有正文、有日期、"
                "以后要回看的走这里。"
                "action=add 新建(folder 是路径如「写作/《归途》」,不给就进收件箱);"
                "append 往已有那条后面补一段(note_id);edit 改标题或正文;mkdir 新建夹。"
                "用户的原话尽量照抄,不要替他润色。"
            ),
            model=NoteWrite,
            handler=note_add,
            phase=PHASE_ANSWER,
            category=CATEGORY_TASK,
            output_budget_tokens=500,
            required=("action",),
        ),
        tool_from_model(
            name="note_change",
            description=(
                "整理笔记(会改动已有内容)。action=move 挪到别的夹(note_id + new_folder,"
                "new_folder 必须已经存在)/ rename 夹改名 / delete 删(进回收站,可撤销)"
                " / restore 从回收站恢复。删除、改名要用户**明确说过**才做;"
                "拿不准哪一条就先 note_find 查,不要猜编号。"
            ),
            model=NoteChange,
            handler=note_change,
            phase=PHASE_ANSWER,
            category=CATEGORY_TASK,
            risk=RISK_CONFIRM,
            output_budget_tokens=500,
            required=("action",),
        ),
    ]
