"""错题 / 拍照录入 / 复习 / 设置路由(纯 SSR)。

从 create_app() 搬出来的第四块业务路由(2026-10-18 架构收口)。
读取路径不含模型;唯一会调模型的是“识别”那一个 POST。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..domain.schemas.study import SUBJECTS
from .deps import Services, build_intake, get_services
from .idempotency import run_once
from .util import MAX_UPLOAD_BYTES, flash
from .visibility import asset_visible

STUDY_TAB_REVIEW = "review"
STUDY_TAB_PROBLEMS = "problems"
STUDY_TAB_WEAK = "weak"
STUDY_TAB_INTAKE = "intake"
STUDY_TABS = (STUDY_TAB_REVIEW, STUDY_TAB_PROBLEMS, STUDY_TAB_WEAK, STUDY_TAB_INTAKE)


def normalize_study_tab(value: str, *, subject: str = "", archived: bool = False) -> str:
    wanted = str(value or "").strip().lower()
    if subject or archived:
        return STUDY_TAB_PROBLEMS
    return wanted if wanted in STUDY_TABS else STUDY_TAB_REVIEW


def due_hint(raw: Any, *, today: str) -> str:
    """"2026-11-10" → "明天" / "今天" / "3 天后" / "已过期 2 天";读不出来给空串。

    ⭐ 家长一眼要的是"还有几天",不是一串 ISO 日期(2026-11-09 重做错题页)。
      只认日期区间,不做"还剩几小时"—— 排程本来就是按自然日算的。
    """
    try:
        due = date.fromisoformat(str(raw or "")[:10])
        base = date.fromisoformat(str(today or "")[:10])
    except ValueError:
        return ""
    days = (due - base).days
    if days == 0:
        return "今天"
    if days == 1:
        return "明天"
    if days == 2:
        return "后天"
    if days > 2:
        return str(days) + " 天后"
    return "已过期 " + str(-days) + " 天"


#: 学科 → 列表左侧那个小圆点的颜色类名(不在表里的走 other)。
SUBJECT_KEYS = {
    "语文": "chinese",
    "数学": "math",
    "英语": "english",
    "物理": "physics",
    "化学": "chemistry",
    "生物": "biology",
    "历史": "history",
    "地理": "geography",
    "政治": "politics",
}

#: 排序权重:逾期 → 今天 → 最近到期 → 更远 → 没有下次(已归档)。
DUE_ORDER = {"late": 0, "now": 1, "soon": 2, "later": 3, "none": 4}


def problem_cards(problems: Any, *, today: str) -> list[dict[str, Any]]:
    """错题本列表的**行**:日期说人话,并按"要不要现在做"排序(2026-11-09 重做)。

    ⭐ 旧列表按 id 倒序 —— 最新录的排最前,而家长打开这一页要的是"哪些今天要复习"。
       现在:逾期最前(红色)、今天次之、然后按最近到期。归档的排在最后 ——
       它们只在「已归档」这个筛选里出现。
    """
    try:
        base = date.fromisoformat(str(today or "")[:10])
    except ValueError:
        base = None
    cards: list[dict[str, Any]] = []
    for item in problems:
        due = str(getattr(item, "next_due_local", None) or "")
        kind, label = "none", "—"
        distance = None
        if due and base is not None:
            try:
                distance = (date.fromisoformat(due[:10]) - base).days
            except ValueError:
                distance = None
        if distance is None:
            label = due[5:] if len(due) >= 10 else (due or "—")
        elif distance < 0:
            kind, label = "late", "逾期 " + str(-distance) + " 天"
        elif distance == 0:
            kind, label = "now", "今天"
        elif distance == 1:
            kind, label = "soon", "明天"
        elif distance <= 7:
            kind, label = "soon", str(distance) + " 天后"
        else:
            kind, label = "later", due[5:10]
        subject = str(getattr(item, "subject", "") or "")
        cards.append({
            "id": int(item.id),
            "subject": subject,
            "subject_key": SUBJECT_KEYS.get(subject, "other"),
            "stem": str(getattr(item, "stem_text", None) or "(图题)"),
            "student": getattr(item, "student_name", None) or "",
            "kps": list(getattr(item, "knowledge_points", None) or []),
            "status": str(getattr(item, "status", "") or ""),
            "archived": str(getattr(item, "status", "")) == "archived",
            "pending": str(getattr(item, "status", "")) == "pending",
            "reps": int(getattr(item, "reps", 0) or 0),
            "lapses": int(getattr(item, "lapses", 0) or 0),
            "due_kind": kind,
            "due_label": label,
            "due_date": due,
            "due_count": int(getattr(item, "due_count", 0) or 0),
            "has_audio": bool(getattr(item, "audio_handle", None)),
            "photo": (list(getattr(item, "photo_handles", None) or []) or [""])[0],
        })
    cards.sort(key=lambda card: (
        DUE_ORDER.get(card["due_kind"], 9),
        card["due_date"] or "9999-12-31",
        -card["id"],
    ))
    return cards


def _pushed_today(services: Services):
    try:
        from .deps import build_dispatcher

        return build_dispatcher(services).study_push_today(services.settings.now())
    except Exception:
        return None


def _wechat_windows() -> dict[str, dict[str, Any]]:
    """盘上每个微信联系人还剩多久的"可主动推送"窗口(读不到就当没有)。"""
    try:
        from ..bridge.adapters import weixin_store

        return weixin_store.windows()
    except Exception:  # noqa: BLE001 - 读不到窗口不该让错题页 500
        return {}


def _window_note(user_ids: list[str], windows: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """这个收件人现在能不能收到 —— (提示文字, 级别)。

    ⚠️ 为什么要在页面上先说(2026-10-24):妈妈那个号六天没说话,窗口早关了,
       可页面什么都没提示,晚上推的时候静默失败,用户只能问"为什么没收到"。
    """
    from ..bridge.adapters.weixin_ilink import human_gap

    entries = [windows[uid] for uid in user_ids if uid in windows]
    if not entries:
        return "还没跟机器人说过话(说一句就能收到)", "warn"
    if any(item.get("open") for item in entries):
        return "", ""
    ages = [float(item.get("age_s") or 0.0) for item in entries]
    return "窗口已关(上次说话 " + human_gap(min(ages)) + "前),让 TA 给机器人发一句话", "warn"


def _push_members(services: Services) -> list[dict[str, Any]]:
    """推送收件人候选:所有在用成员,标出谁绑了微信、窗口还开不开(D161/D163)。"""
    from ..family import members as member_store
    from ..ops import settings_store

    try:
        rows = member_store.list_members(services.assistant, active_only=True)
        bindings = services.assistant.execute(
            "SELECT member_id, external_id FROM app_member_bindings WHERE channel = 'weixin'"
        ).fetchall()
    except Exception:  # noqa: BLE001 - 设置页不该因为读不到成员就 500
        return []
    by_member: dict[int, list[str]] = {}
    for row in bindings:
        by_member.setdefault(int(row["member_id"]), []).append(str(row["external_id"]))
    windows = _wechat_windows()
    today = services.settings.now().date().isoformat()
    result: list[dict[str, Any]] = []
    for member in rows:
        user_ids = by_member.get(member.id, [])
        note, level = _window_note(user_ids, windows) if user_ids else ("", "")
        error = settings_store.get_raw(
            services.assistant, "study.push.err." + today + "." + str(member.id)
        )
        result.append({
            "id": member.id,
            "name": member.name,
            "bound": bool(user_ids),
            "note": note,
            "note_level": level,
            "error": error or "",
        })
    return result


def register_study_routes(app, *, templates) -> None:
    def _study_context(
        services: Services, *, tab: str = STUDY_TAB_REVIEW, subject: str = "",
        archived: bool = False, **extra: Any
    ) -> dict[str, Any]:
        """错题页的数据。⭐ 只查**当前这一页**要用的东西(用户 2026-10-13:"下拉太长了")。

        ⚠️ 以前是一次性把复习卡、错题本(最多 200 道)、薄弱点、录入全塞进一页 ——
        手机上一屏一屏往下拉,想看的东西永远在很远的地方。现在按页取数:
        错题本那份 200 道的查询只在那一页发生。
        """
        intake = build_intake(services)
        context: dict[str, Any] = {
            "page": "study",
            "tab": tab,
            "stats": services.study.stats(),          # 每页都要:页签上的计数与排程说明
            "settings": services.study.settings(),
            "students": services.study.list_students(),
            "subjects": list(SUBJECTS),
            "push": services.study.push_state(),      # 页签上的"今天几道"就用它,不再查一遍
            "push_members": _push_members(services),  # 推送收件人候选(D161)
            "subject": subject,
            "archived": archived,
            # 错题本那一页的分面计数(与筛选项对得上,不含已归档)
            "counts": services.study.problem_counts(),
        }
        # 页签上的"待确认"角标要每页都看得见,所以这个计数一直查(它只是一条 count)
        context["pending_candidates"] = intake.pending_count()
        if tab == STUDY_TAB_REVIEW:
            context["queue"] = services.study.review_queue(limit=20)
            context["pushed_today"] = _pushed_today(services)
            # 同一天多道听写/跟读各并成一段(语文/英语分开)。第一次会合成一次,之后读缓存。
            context["merged_groups"] = services.study.merged_audio_today()
        elif tab == STUDY_TAB_PROBLEMS:
            # ⚠️ 「已归档」那一栏要**只看归档的**:旧写法只传 include_archived=True,
            #    结果把没归档的也一起列了出来(2026-11-09 重做时发现,筛选形同虚设)。
            query: dict[str, Any] = {"limit": 200}
            if archived:
                query["status"] = "archived"
            if subject:
                query["subject"] = subject
            items = services.study.list_problems(query)
            context["problems"] = items
            context["problem_cards"] = problem_cards(
                items, today=services.study.now().date().isoformat(),
            )
        elif tab == STUDY_TAB_INTAKE:
            context.update({
                "intake": intake.latest_session(),
                "vision_ready": intake.vision_available,
            })
        context.update(extra)
        return context

    @app.get("/study", response_class=HTMLResponse)
    def study_page(
        request: Request,
        tab: str = "",
        subject: str = "",
        archived: int = 0,
        ok: str = "",
        err: str = "",
        services: Services = Depends(get_services),
    ):
        # ⚠️ 带了学科/归档筛选的老链接与书签,一律落到**错题本**那一页 ——
        #    筛选只在那一页有意义,不然会得到"点了数学却什么都没变"。
        chosen = normalize_study_tab(tab, subject=subject, archived=bool(archived))
        return templates.TemplateResponse(
            request,
            "study.html",
            _study_context(services, tab=chosen, subject=subject, archived=bool(archived),
                           ok=ok, err=err),
        )

    @app.get("/assets/{asset_id}")
    def asset_bytes(asset_id: int, services: Services = Depends(get_services)) -> Response:
        """把题目照片发回浏览器。

        ⚠️ 只认**库里登记过的资产 id**,不接受任何路径参数 —— 网页永远不能变成
        一个"给我路径我读给你看"的文件读取器(与 M6 的路径白名单是同一条纪律)。
        内容寻址意味着同一个 id 的内容永不改变,所以可以放心让浏览器长期缓存。
        """
        # ⚠️ 用**服务层那一个** MediaStore,而不是在这里另造一个:
        #    它的根路径由服务装配决定,自己 new 一个就等于把"图存哪儿"写了两遍。
        store = services.study.media
        if not asset_visible(services, int(asset_id)):
            return Response(status_code=404, content=b"")
        try:
            asset = store.get(int(asset_id))
            payload = asset.path(store.root).read_bytes()
        except (FileNotFoundError, OSError):
            return Response(status_code=404, content=b"")
        return Response(
            content=payload,
            media_type=asset.media_type or "application/octet-stream",
            headers={"Cache-Control": "private, max-age=604800"},
        )

    # -- 拍照录入 ---------------------------------------------------------
    @app.post("/study/upload")
    def study_upload(
        files: list[UploadFile] = File(default=[]),
        student_id: str = Form(""),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """收照片。**这一步不调用模型** —— 先把图安全落地,识别是下一步的事。

        ⚠️ 这里**不需要** _idem 令牌:同一张图按内容寻址去重(sha256),
           重复提交不会变成两份素材;以前声明了却不用,是个假承诺。
        """
        intake = build_intake(services)
        chosen = None
        if student_id.strip():
            try:
                chosen = services.study.student_for(student_id=int(student_id))
            except Exception:
                chosen = None
        if chosen is None:
            chosen = services.study.default_student()
        session_id = intake.start_session(student_id=chosen.id)
        stored = 0
        refused: list[str] = []
        for upload in files:
            name = upload.filename or "photo"
            data = upload.file.read(MAX_UPLOAD_BYTES + 1)
            if not data:
                continue
            if len(data) > MAX_UPLOAD_BYTES:
                refused.append(name + ":文件太大(上限 " + str(MAX_UPLOAD_BYTES // 1024 // 1024) + "MB)")
                continue
            try:
                intake.add_image(session_id, data, original_name=name)
                stored += 1
            except Exception as exc:
                refused.append(name + ":" + str(exc)[:60])
        if not stored:
            return flash(
                "/study?tab=" + STUDY_TAB_INTAKE,
                "没有收到可用的图片" + ("(" + ";".join(refused) + ")" if refused else ""),
                error=True,
            )
        return flash(
            "/study/intake/" + str(session_id),
            "已收到 " + str(stored) + " 张照片" + (";跳过 " + ";".join(refused) if refused else ""),
        )

    @app.get("/study/intake/{session_id}", response_class=HTMLResponse)
    def study_intake_page(
        session_id: int, request: Request, ok: str = "", err: str = "",
        services: Services = Depends(get_services),
    ):
        from ..domain.study.intake import IntakeError

        intake = build_intake(services)
        try:
            session = intake.session(session_id)
        except IntakeError as exc:
            return flash("/study?tab=" + STUDY_TAB_INTAKE, str(exc), error=True)
        return templates.TemplateResponse(
            request,
            "study_intake.html",
            {
                "page": "study",
                "session": session,
                "students": services.study.list_students(),
                "subjects": list(SUBJECTS),
                "settings": services.study.settings(),
                "vision_ready": intake.vision_available,
                "ok": ok,
                "err": err,
            },
        )

    @app.post("/study/intake/{session_id}/recognize")
    def study_intake_recognize(
        session_id: int,
        solve: str = Form(""),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """跑一次视觉识别。慢(十几秒),但**失败只是失败**:候选与照片都还在。"""
        intake = build_intake(services)
        try:
            report = intake.recognize_session(session_id, solve=bool(solve))
        except Exception as exc:
            return flash("/study/intake/" + str(session_id), "识别失败:" + str(exc)[:120], error=True)
        if report.get("offline"):
            return flash(
                "/study/intake/" + str(session_id),
                "当前没有可用的模型(离线模式或没配密钥)—— 请手工填写字段。",
                error=True,
            )
        message = (
            "识别完成:" + str(report["recognized"]) + " 张成功 / " + str(report["failed"])
            + " 张失败,共 " + str(report["problems"]) + " 道题"
        )
        if report.get("errors"):
            message += "(" + str(report["errors"][0])[:60] + ")"
        return flash("/study/intake/" + str(session_id), message,
                      error=bool(report["failed"]) and not report["recognized"])

    @app.post("/study/intake/candidate/{candidate_id}/accept")
    def study_intake_accept(
        candidate_id: int,
        subject: str = Form(...),
        stem: str = Form(""),
        answer: str = Form(""),
        explanation: str = Form(""),
        kp: str = Form(""),
        grade: str = Form(""),
        student_id: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """人工确认:这一步之后它才是一道**错题**,并自动建好复习单元。"""
        from ..domain.study.intake import IntakeError

        intake = build_intake(services)
        try:
            candidate = intake.get_candidate(candidate_id)
            intake.session(candidate.session_id)   # 归属校验(服务层会拒绝别人的会话)
        except IntakeError as exc:
            return flash("/study?tab=" + STUDY_TAB_INTAKE, str(exc), error=True)
        back = "/study/intake/" + str(candidate.session_id)
        overrides = {
            "subject": subject, "stem_text": stem, "answer_text": answer,
            "explanation": explanation, "grade": grade,
            "knowledge_points": kp,
            "student_id": int(student_id) if student_id.strip().isdigit() else None,
        }
        try:
            outcome = run_once(
                services, idem_token, {"candidate": candidate_id, "subject": subject},
                lambda: intake.accept(candidate_id, overrides=overrides, actor="user"),
            )
        except IntakeError as exc:
            return flash(back, str(exc), error=True)
        except Exception as exc:
            return flash(back, "录入失败:" + str(exc)[:120], error=True)
        if outcome is None:
            # 幂等闸门挡住了重复提交(双击、断网重试、刷新重发)—— 这是**正常**结果,不是错误
            return flash(back, "这一条刚刚已经确认过了,重复提交已忽略。")
        message = (
            "已录入错题 #" + str(outcome["problem_id"]) + "(" + str(outcome["subject"])
            + "," + str(outcome["review_units"]) + " 个复习单元,首次复习 "
            + str(outcome["first_due_local"]) + ")"
        )
        # 🔊 听力/听写音频是**录入这一刻**就做好并绑上的(D107)——做没做成都要说
        audio = str(outcome.get("audio_note") or "").strip()
        if audio:
            message += " · " + ("🔊 " if outcome.get("audio_artifact_id") else "🔇 ") + audio
        return flash(back, message)

    @app.post("/study/intake/candidate/{candidate_id}/reject")
    def study_intake_reject(
        candidate_id: int,
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        from ..domain.study.intake import IntakeError

        intake = build_intake(services)
        try:
            candidate = intake.get_candidate(candidate_id)
            intake.session(candidate.session_id)   # 归属校验
            intake.reject(candidate_id)
        except IntakeError as exc:
            return flash("/study?tab=" + STUDY_TAB_INTAKE, str(exc), error=True)
        return flash("/study/intake/" + str(candidate.session_id), "已丢弃候选 #" + str(candidate_id))

    # -- 错题详情与编辑 ---------------------------------------------------
    @app.get("/study/problem/{problem_id}", response_class=HTMLResponse)
    def study_problem_page(
        problem_id: int, request: Request, ok: str = "", err: str = "",
        services: Services = Depends(get_services),
    ):
        try:
            detail = services.study.problem_detail(problem_id)
        except Exception as exc:
            return flash("/study?tab=" + STUDY_TAB_PROBLEMS, str(exc), error=True)
        units = detail["units"]
        today = services.study.now().date().isoformat()
        # 3 个单元都排在明天时,**不逐行印三遍同一个日期** —— 那是最典型的噪音。
        due_dates = {str(unit.get("due_date_local") or "") for unit in units}
        return templates.TemplateResponse(
            request,
            "study_problem.html",
            {
                "page": "study",
                "detail": detail,
                "problem": detail["problem"],
                "units": units,
                "units_same_due": len(due_dates) <= 1,
                "due_hint": due_hint(detail["problem"].get("next_due_local"), today=today),
                "history": detail["history"],
                "assets": detail["assets"],
                "materials": detail.get("materials") or {},
                "students": services.study.list_students(),
                "subjects": list(SUBJECTS),
                "engine_note": detail["schedule_note"],
                "ok": ok,
                "err": err,
            },
        )

    @app.post("/study/problem/{problem_id}/update")
    def study_problem_update(
        problem_id: int,
        subject: str = Form(...),
        stem: str = Form(""),
        answer: str = Form(""),
        explanation: str = Form(""),
        kp: str = Form(""),
        grade: str = Form(""),
        source: str = Form(""),
        student_id: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        back = "/study/problem/" + str(problem_id)
        payload = {
            "subject": subject,
            "stem_text": stem,
            "answer_text": answer,
            "explanation": explanation,
            "grade": grade,
            "source": source,
            "knowledge_points": [item.strip() for item in kp.replace("、", ",").split(",")
                                 if item.strip()],
            "student_id": int(student_id) if student_id.strip().isdigit() else None,
        }
        try:
            run_once(services, idem_token, {"problem": problem_id, "subject": subject},
                     lambda: services.study.update_problem(problem_id, payload))
        except Exception as exc:
            return flash(back, "改不了:" + str(exc)[:120], error=True)
        return flash(back, "已保存。⭐ 复习进度没有被重置 —— 改内容不该让孩子重学一遍。")

    @app.post("/study/problem/{problem_id}/status")
    def study_problem_status(
        problem_id: int,
        action: str = Form(...),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        back = "/study/problem/" + str(problem_id)
        if action == "archive":
            services.study.archive_problem(problem_id)
            return flash(back, "已归档:不再出现在复习清单里,但复习记录与统计口径都保留。")
        if action == "restore":
            services.study.restore_problem(problem_id)
            return flash(back, "已恢复复习。")
        return flash(back, "不认识的动作:" + str(action), error=True)

    @app.post("/study/problem/{problem_id}/confirm")
    def study_problem_confirm(
        problem_id: int,
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """人工核对通过:把「AI 写进来的、还没经你看过」这个标记去掉。

        ⚠️ 这个入口以前**根本不存在**(服务层有 confirm_problem,页面只画了个标签):
        待确认的题在页面上没有任何办法变成「在学」,只能归档再恢复绕一圈。
        """
        back = "/study/problem/" + str(problem_id)
        try:
            services.study.confirm_problem(problem_id)
        except Exception as exc:  # noqa: BLE001
            return flash(back, "确认失败:" + str(exc)[:120], error=True)
        return flash(
            back,
            "已确认这道题(状态:在学)。⭐ 确认只是把「还没经你核对」这个标记去掉 —— "
            "排程、推送、复习卡本来就在用它,不确认也照常复习。",
        )

    @app.post("/study/problem/{problem_id}/delete")
    def study_problem_delete(
        problem_id: int,
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """彻底删掉一道**录错了**的题(2026-11-09:"录错的,智能体自己也删除不掉")。

        ⚠️ 有复习记录的题这里删不掉 —— 服务层会把它改成归档并如实回报原因:
           复习日志是统计与调参的依据,不是可以随手清掉的草稿。
        """
        back = "/study/problem/" + str(problem_id)
        try:
            got = services.study.delete_problem(
                problem_id, reason="家长在错题页点的删除", actor="user",
            )
        except Exception as exc:  # noqa: BLE001
            return flash(back, "删不掉:" + str(exc)[:120], error=True)
        if not got.get("deleted"):
            return flash(back, "没删:" + str(got.get("note") or ""), error=True)
        return flash(
            "/study?tab=" + STUDY_TAB_PROBLEMS,
            "🗑 已删除错题 #" + str(problem_id) + " —— 它没有复习记录,统计不受影响。",
        )

    @app.post("/study/problem/{problem_id}/figure")
    def study_problem_figure(
        problem_id: int,
        action: str = Form(...),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """图形的**人工**返工:转 90° / 收窄 / 放大。**不调模型、不花钱、立刻生效。**

        ⭐ 为什么给这三个按钮(2026-11-21):图形"歪了 / 切了一边 / 带进一句题干"是
        模型估框的常态。以前唯一的补救是 `--resolve-one --force`(5–6 次模型调用),
        而人眼看一眼就知道该往哪转。**人比模型便宜,也更准。**
        """
        back = "/study/problem/" + str(problem_id)
        plan = {
            "rotate_left": (-90, 0.0),
            "rotate_right": (90, 0.0),
            "tighter": (0, -0.12),
            "looser": (0, 0.12),
        }
        if action not in plan:
            return flash(back, "不认识的图形动作:" + str(action), error=True)
        delta, zoom = plan[action]
        try:
            got = services.study.recrop_figure(
                problem_id, rotate_delta=delta, zoom=zoom, actor="user",
            )
        except Exception as exc:  # noqa: BLE001
            return flash(back, "重裁失败:" + str(exc)[:120], error=True)
        return flash(
            back,
            "图形已重裁(顺时针 " + str(got["rotation"]) + "°,"
            + str(got["width"]) + "×" + str(got["height"]) + ")。"
            "还不对就再点一次 —— 这一步不调模型、不花钱。",
        )

    @app.post("/study/problem/{problem_id}/audio")
    def study_problem_audio(
        problem_id: int,
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """给**漏做了声音**的听写/听力题补做一次(与录入时同一段代码)。

        ⭐ 老数据用:手工录进来的听写题以前不会有 audio_spec(见 2026-10-26 的 #14),
        补做一次之后,家长页、复习卡、当天合并听写、错题卷都会用它。
        """
        back = "/study/problem/" + str(problem_id)
        try:
            got = services.study.bind_audio(problem_id, force=True, actor="user")
        except Exception as exc:  # noqa: BLE001
            return flash(back, "补做声音失败:" + str(exc)[:120], error=True)
        if not got.get("needed"):
            return flash(
                back,
                "这题看着不需要出声(题干/答案/知识点里没有听写、听力、认读的线索),没有补做。",
                error=True,
            )
        if got.get("audio_artifact_id"):
            return flash(back, "🔊 " + str(got.get("audio_note") or "声音补好了"))
        return flash(
            back,
            "🔇 还是没做成:" + str(got.get("audio_error") or got.get("audio_note") or "未知原因"),
            error=True,
        )

    # -- 手工录入 / 复习 / 设置 -------------------------------------------
    @app.post("/study/problem")
    def study_add(
        subject: str = Form(...),
        stem: str = Form(""),
        answer: str = Form(""),
        kp: str = Form(""),
        student_id: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        record = run_once(
            services, idem_token, {"subject": subject, "stem": stem},
            lambda: services.study.add_problem(
                {
                    "subject": subject,
                    "stem_text": stem or None,
                    "answer_text": answer or None,
                    "knowledge_points": [item.strip() for item in kp.replace("、", ",").split(",")
                                         if item.strip()],
                    "student_id": int(student_id) if student_id.strip().isdigit() else None,
                },
                actor="user",
            ),
        )
        if record is None:
            return RedirectResponse("/study?tab=" + STUDY_TAB_PROBLEMS, status_code=303)
        message = (
            "已录入错题 #" + str(record.id) + ":建了 " + str(len(record.review_units))
            + " 个复习单元,首次复习 "
            + (record.review_units[0].due_date_local if record.review_units else "-")
        )
        # 🔊 手输的听写/听力题也要有声音(2026-10-26 #14 踩出来的:以前只有拍照识别那条
        #    路做音频,手输的题一直哑着)。做没做成都要在回执里说清。
        try:
            bound = services.study.bind_audio(record.id, actor="user")
        except Exception as exc:  # noqa: BLE001 - 声音没做成不该把"录题成功"变成失败
            bound = {"needed": True, "audio_error": type(exc).__name__ + ": " + str(exc)[:80]}
        if bound.get("needed"):
            message += " · " + (
                "🔊 " + str(bound.get("audio_note") or "声音已生成")
                if bound.get("audio_artifact_id") else
                "🔇 " + str(bound.get("audio_error") or bound.get("audio_note") or "声音没做成")
            )
        return flash("/study?tab=" + STUDY_TAB_PROBLEMS, message)

    @app.post("/study/review/problem/{problem_id}")
    def study_review_problem(
        problem_id: int,
        rating: int = Form(...),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """一题一次作答:记到这道题**今天到期的所有单元**上(D108)。

        ⭐ 幂等键里带的是 (题号, 评分),所以双击/刷新重发只会记一次。
        """
        run_once(
            services, idem_token, {"problem_id": problem_id, "rating": rating},
            lambda: services.study.record_problem_review(problem_id, rating, actor="user"),
        )
        return RedirectResponse("/study?tab=" + STUDY_TAB_REVIEW, status_code=303)

    @app.post("/study/review/{unit_id}")
    def study_review(
        unit_id: int,
        rating: int = Form(...),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """按**单元**记一次复习(老入口:工具/脚本仍在用)。"""
        run_once(
            services, idem_token, {"unit_id": unit_id, "rating": rating},
            lambda: services.study.record_review(unit_id, rating, actor="user"),
        )
        return RedirectResponse("/study?tab=" + STUDY_TAB_REVIEW, status_code=303)

    @app.post("/study/sheet")
    def study_sheet(
        limit: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """出今天的错题卷(产物层)。⭐ 纯代码生成,不经过模型。"""
        try:
            result = run_once(
                services, idem_token, {"sheet": limit},
                lambda: services.study.make_sheet(
                    limit=int(limit) if limit.strip().isdigit() else None
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return flash("/study?tab=" + STUDY_TAB_REVIEW, "出错题卷失败:" + str(exc)[:120], error=True)
        if result is None:
            return flash("/study?tab=" + STUDY_TAB_REVIEW, "刚刚已经生成过一份了,去「素材」页下载。")
        return flash(
            "/study?tab=" + STUDY_TAB_REVIEW,
            "错题卷已生成:" + str(result["items"]) + " 道 → 在「素材」页下载,或直接点 "
            + "/artifacts/" + str(result["artifact"]["id"]),
        )

    @app.post("/study/settings")
    def study_settings(
        push_time: str = Form(""),
        enabled: str = Form(""),
        daily_limit: str = Form(""),
        student_id: str = Form(""),
        student_name: str = Form(""),
        solve_on_recognize: str = Form(""),
        targets: list[str] = Form([]),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """推送设置。界面与模型走的是**同一个** update_settings(校验只写一份)。"""
        payload: dict[str, Any] = {
            "enabled": bool(enabled),
            "solve_on_recognize": bool(solve_on_recognize),
            # 空 = 所有已绑微信的成员;勾了就只推给勾中的人。
            "targets": [int(item) for item in targets if str(item).isdigit()],
        }
        if push_time.strip():
            payload["push_time"] = push_time.strip()
        if daily_limit.strip():
            payload["daily_limit"] = int(daily_limit)
        if student_id.strip().isdigit():
            payload["student_id"] = int(student_id)
        if student_name.strip():
            payload["student_name"] = student_name.strip()
        try:
            run_once(services, idem_token, payload,
                     lambda: services.study.update_settings(payload, actor="user"))
        except Exception as exc:
            return flash("/study?tab=" + STUDY_TAB_REVIEW, "设置没改成:" + str(exc)[:120], error=True)
        updated = services.study.settings()
        return flash(
            "/study?tab=" + STUDY_TAB_REVIEW,
            "设置已保存:每天 " + ("推" if updated.push_enabled else "不推")
            + " " + updated.push_time_local + ",最多 " + str(updated.push_daily_limit) + " 道。"
            "到点由本地调度器直接生成清单,不需要模型在线。",
        )

