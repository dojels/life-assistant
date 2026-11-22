"""入口:python -m la

用法:
    python -m la                    进入 REPL
    python -m la --once "现在几点"   跑一轮就退出(脚本与验收用)
    python -m la --check            环境与数据库自检
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from typing import Any

from . import config
from .agent.prompts import SURFACE_CLI
from .agent.session import MemorySession
from .agent.step_ledger import format_duration
from .storage import db


def build_client(settings: config.Settings):
    """按 provider 造客户端(唯一实现在 la/llm/factory.py)。"""
    from .llm.factory import build_client as _build

    return _build(settings)


def _vision_client_factory(settings: config.Settings):
    """视觉识别的客户端工厂。

    ⚠️ fake 提供方 = **没有识别能力**,返回 None,录入流程自动退化成"手工填"。
    这不是偷懒:离线模式要能跑通整条链路,而不是假装识别成功了。
    """
    if settings.provider == "fake":
        return None
    return lambda: build_client(settings)


def cmd_intake(session, settings: config.Settings, args) -> int:
    """拍照录入:存图 -> 视觉识别 -> 候选区(可选 --auto 直接入库)。"""
    from pathlib import Path

    from .domain.media import MediaStore
    from .domain.study.intake import IntakeError, IntakeService

    if session.study is None:
        print("没有领域库连接,无法录入。")
        return 1
    conn = session.assistant_conn
    service = IntakeService(
        conn,
        study=session.study,
        media=MediaStore(conn),
        client_factory=_vision_client_factory(settings),
    )
    student = (
        session.study.ensure_student(args.student)
        if args.student
        else session.study.default_student()
    )
    session_id = service.start_session(student_id=student.id)
    stored = 0
    for raw_path in args.intake_image:
        path = Path(raw_path)
        if not path.exists():
            print("找不到图片:" + str(path))
            continue
        try:
            candidate = service.add_image(
                session_id, path.read_bytes(), original_name=path.name
            )
            stored += 1
            print("已存图 " + path.name + " → 候选 #" + str(candidate.id)
                  + "(资产 #" + str(candidate.asset_id) + ")")
        except IntakeError as exc:
            print("跳过 " + path.name + ":" + str(exc))
    if not stored:
        print("没有可用的图片,已放弃这次录入。")
        return 1

    if service.vision_available:
        print("正在识别 " + str(stored) + " 张图(视觉模型,通常十几秒)…")
    else:
        print("离线模式(LA_LLM_PROVIDER=fake):跳过识别,请手工填写。")
    report = service.recognize_session(session_id, solve=bool(args.solve))
    print("识别结果:" + str(report["recognized"]) + " 张成功 / "
          + str(report["failed"]) + " 张失败,共 " + str(report["problems"]) + " 道题")
    for message in report["errors"]:
        print("  ! " + str(message))

    fresh = service.session(session_id)
    for candidate in fresh.candidates:
        if candidate.settled:
            continue
        payload = candidate.payload
        print("  [候选 #" + str(candidate.id) + "] " + str(payload.get("subject") or "未定")
              + " · 把握 " + str(round(float(payload.get("confidence") or 0) * 100)) + "%")
        if payload.get("stem_text"):
            print("      题干:" + str(payload["stem_text"])[:120])
        if payload.get("answer_text"):
            print("      答案:" + str(payload["answer_text"])[:120])
        if payload.get("knowledge_points"):
            print("      知识点:" + "、".join(str(x) for x in payload["knowledge_points"]))
        if candidate.error:
            print("      识别失败:" + candidate.error)

    if args.auto:
        accepted = 0
        for candidate in fresh.candidates:
            if candidate.settled or not candidate.recognized:
                continue
            try:
                outcome = service.accept(candidate.id, actor="user")
            except IntakeError as exc:
                print("  候选 #" + str(candidate.id) + " 入不了库:" + str(exc))
                continue
            accepted += 1
            print("  ✅ 已录入错题 #" + str(outcome["problem_id"])
                  + "(" + str(outcome["subject"]) + ","
                  + str(outcome["review_units"]) + " 个复习单元,下次 "
                  + str(outcome["first_due_local"]) + ")")
        if not accepted:
            print("没有可自动入库的候选 —— 打开界面逐条确认:"
                  " http://127.0.0.1:8765/study/intake/" + str(session_id))
        return 0

    print("确认入库:重跑加 --auto,或打开界面 http://127.0.0.1:8765/study/intake/"
          + str(session_id))
    print("(候选区一直留着 —— 照片不会因为关掉终端而丢)")
    return 0


def cmd_study_settings(session) -> int:
    """打印错题设置与今天的推送状态。"""
    if session.study is None:
        print("没有领域库连接,无法读取设置。")
        return 1
    settings_now = session.study.settings()
    state = session.study.push_state()
    print("== 错题设置 ==")
    print("默认学生   : " + str(settings_now.student_name or "(未设置)")
          + "   在册 " + str(len(session.study.list_students())) + " 人")
    print("每日推送   : " + ("开" if settings_now.push_enabled else "关")
          + "   时间 " + str(settings_now.push_time_local))
    print("每日上限   : " + str(settings_now.push_daily_limit) + " 道")
    print("识别补全   : " + ("开(照片没答案时让模型解)" if settings_now.solve_on_recognize else "关"))
    print("今天到期   : " + str(state["due_count"]) + " 道"
          + ("   (已过推送时间)" if state["ready"] else "   (还没到推送时间)"))
    print("复习周期   : " + session.study.schedule_note())
    # "今天推过没"由调度器回答 —— 它才是唯一知道 dispatches 表的人(会话里那份就是它)
    try:
        push = session.dispatcher.study_push_today(session.settings.now())
    except Exception as exc:
        print("今天推过没 : (读不到:" + type(exc).__name__ + ")")
    else:
        head = push.text.splitlines()[0] if push else ""
        print("今天推过没 : " + ("推过了 → " + head if push else "还没推"))
    return 0


def cmd_check(settings: config.Settings) -> int:
    created = config.ensure_runtime_dirs()
    print("== 家庭助手自检 ==")
    print("项目根目录 : " + str(config.BASE_DIR))
    print("数据目录   : " + str(config.DATA_DIR))
    if created:
        names = []
        for path in created:
            try:
                names.append(str(path.relative_to(config.BASE_DIR)))
            except ValueError:
                names.append(str(path))  # LA_DATA_DIR 指向项目外时直接给绝对路径
        print("本次新建   : " + ", ".join(names))
    else:
        print("本次新建   : (无,目录已齐)")
    print("时区 / 现在: " + settings.timezone + " / " + config.format_local(settings.now()))
    print("模型提供方 : " + settings.provider + " (" + settings.model + ")")
    if settings.provider == "fake":
        print("密钥       : 离线模式,不需要密钥")
    else:
        print("密钥       : " + ("已配置" if settings.api_key else "未配置(.env 里填 DEEPSEEK_API_KEY)"))

    conn = db.connect_assistant()
    try:
        snapshot = db.pragma_snapshot(conn)
        print("领域库     : " + str(config.ASSISTANT_DB))
        print(
            "PRAGMA     : journal_mode=" + str(snapshot["journal_mode"])
            + " busy_timeout=" + str(snapshot["busy_timeout"])
            + " synchronous=" + str(snapshot["synchronous"])
            + " foreign_keys=" + str(snapshot["foreign_keys"])
        )
        tables = db.table_names(conn)
        print("表(" + str(len(tables)) + ")     : " + ", ".join(tables))
        versions = [
            str(row[0]) + ":" + str(row[1])
            for row in conn.execute("SELECT version, name FROM sys_schema_version ORDER BY version")
        ]
        print("迁移版本   : " + (", ".join(versions) if versions else "(空)"))
        txns = conn.execute("SELECT count(*) FROM app_txns_live").fetchone()[0]
        pending = conn.execute(
            "SELECT count(*) FROM ops_outbox WHERE processed_at IS NULL"
        ).fetchone()[0]
        drafts = conn.execute(
            "SELECT count(*) FROM app_txns_live WHERE status = 'draft'"
        ).fetchone()[0]
        print("账本       : 交易 " + str(txns) + " 笔(草稿 " + str(drafts)
              + ") / 待中继事件 " + str(pending))
    finally:
        conn.close()

    memory = db.connect_memory()
    try:
        from .memory.search import ensure_search_index

        print("记忆库     : " + str(config.MEMORY_DB))
        print("检索索引   : " + ensure_search_index(memory) + "(实测:两字词走 LIKE 降级)")
        summaries = memory.execute("SELECT count(*) FROM summary_pool").fetchone()[0]
        facts = memory.execute("SELECT count(*) FROM facts WHERE active = 1").fetchone()[0]
        events = memory.execute("SELECT count(*) FROM events").fetchone()[0]
        print("记忆内容   : 摘要 " + str(summaries) + " 条 / 生效事实 " + str(facts)
              + " 条 / 事件 " + str(events) + " 条")
    finally:
        memory.close()

    print("投影预算   : 对话段 " + str(config.BUDGET_CONVERSATION)
          + " / 压缩触发 " + str(config.BUDGET_COMPACT_TRIGGER)
          + " / 硬上限 " + str(config.BUDGET_HARD_MAX) + " token")
    print("自检完成。")
    return 0


def _interactive_approver(spec, params) -> bool:
    """confirm 级工具必须当面批准 —— 脚本里用 --yes 预先授权。"""
    print("[需要确认] " + spec.name)
    print("  参数: " + str(params)[:400])
    try:
        answer = input("  允许执行吗? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def build_cli_asker(conn: Any):
    """命令行下的统一反馈机制:把问题打在终端里,行内回答。

    ⚠️ 它和 Web 走的是**同一套** Question/Broker/落库路径(见 agent/feedback.py),
    只是 transport 换成了 input() —— 所以"超时不算同意""答案只生效一次"这两条
    在命令行下同样成立。
    """
    from .agent.feedback import (
        STATUS_CANCELLED,
        Answer,
        QuestionLog,
        build_asker,
        format_question,
    )

    async def terminal_transport(question) -> "Answer":
        print("")
        print(format_question(question))
        try:
            raw = (await asyncio.to_thread(input, "  你的回答(序号 / 文字,回车=先不回答):")).strip()
        except (EOFError, KeyboardInterrupt):
            return Answer(question_id=question.id, status=STATUS_CANCELLED,
                          by="user", note="终端里跳过了这个问题")
        if not raw:
            return Answer(question_id=question.id, status=STATUS_CANCELLED,
                          by="user", note="用户直接回车跳过了")
        if raw.isdigit() and 1 <= int(raw) <= len(question.options):
            chosen = question.options[int(raw) - 1]
            return Answer(question_id=question.id, selected=[chosen.key()], by="user")
        return Answer(question_id=question.id, text=raw, by="user")

    log = QuestionLog(conn) if conn is not None else None
    return build_asker(log=log, transport=terminal_transport)


def _print_phase_event(kind: str, data: dict) -> None:
    if kind == "phases":
        print(
            "  [阶段] " + str(data.get("phase")) + " 步数=" + str(data.get("steps"))
            + " 证据=" + str(data.get("evidence")) + " 依据=" + str(data.get("basis"))
        )
    elif kind in ("retrieval", "compaction", "notes"):
        print("  [" + kind + "] " + str(data))


def _make_console_survivable() -> None:
    """让中文输出在"编码装不下"的控制台上也不至于把进程打死。

    ⚠️ 真实踩过两处:GitHub Actions 的 Windows runner 控制台是 cp1252,
    打印一句「== 家庭助手自检 ==」就 UnicodeEncodeError 退出,整个 CI 红;
    用户把输出重定向到文件、或换成非中文代码页的终端时同样会遇到。

    ⭐ 只改 errors(**不改 encoding**):能显示的照常显示,显示不了的变成 '?',
    这样"打印一行帮助文字"永远不会比"真正要跑的任务"更致命。
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _make_console_survivable()
    parser = argparse.ArgumentParser(prog="la", description="家庭助手(本地优先的个人助理)")
    parser.add_argument("--once", metavar="TEXT", help="只跑一轮就退出")
    parser.add_argument("--check", action="store_true", help="打印环境与数据库自检后退出")
    # --- 验收闸门(la/ops/verify.py)---------------------------------------
    # ⭐ 一条命令跑完全部验收。层级是**累积**的:T1 = T0 + T1;只 T2 才花钱。
    parser.add_argument("--verify", metavar="LEVEL", nargs="?", const="0",
                        help="跑验收后退出。LEVEL:0(默认,单元测试+模板)/ 1(+端到端)/ "
                             "2 或 all(+真机,花钱)。全过返回 0,有红返回 1")
    parser.add_argument("--list-checks", action="store_true", help="列出全部验收项后退出")
    parser.add_argument("--only", metavar="TEXT", help="配合 --verify:只跑名字里含这个的项")
    parser.add_argument("--dry-run", action="store_true", help="配合 --verify:只说会跑什么,不真跑")
    parser.add_argument("--quiet", action="store_true", help="配合 --verify:少打印")
    parser.add_argument("--json", action="store_true",
                        help="配合 --verify:只输出一行 JSON(给脚本读)")
    parser.add_argument("--verify-timeout", metavar="SECONDS", type=int, default=0,
                        help="配合 --verify:覆盖每一项的超时(秒)")
    parser.add_argument("--session", metavar="ID", help="覆盖会话 ID(默认 default)")
    parser.add_argument("--compact", action="store_true", help="强制压缩一次最旧区间后退出")
    parser.add_argument("--dump", action="store_true", help="打印本轮投影(段预算 + 内容)后退出")
    parser.add_argument("--yes", action="store_true", help="自动批准 confirm 级工具(脚本与验收用)")
    parser.add_argument("--phases", action="store_true", help="每轮打印阶段流转与工具调用")
    parser.add_argument("--briefing", action="store_true", help="打印今日简报(待办 + 目标 + 昨日概要)后退出")
    parser.add_argument("--todos", metavar="FILTER", nargs="?", const="pending", help="列出待办后退出")
    parser.add_argument("--goals", action="store_true", help="列出目标后退出")
    parser.add_argument("--import-bill", metavar="PATH", help="导入账单文件(csv/xlsx)后退出")
    parser.add_argument("--ledger", metavar="N", nargs="?", const="10", help="列出最近 N 笔交易")
    parser.add_argument("--query-expense", metavar="PERIOD", help="按分类聚合某个周期(如 2026-10)")
    parser.add_argument("--export-beancount", action="store_true", help="导出 Beancount 文本后退出")
    parser.add_argument("--agenda", metavar="DAYS", nargs="?", const="14", help="列出未来 N 天的日程")
    parser.add_argument("--ics", metavar="PATH", help="导出只读 .ics 订阅文件")
    parser.add_argument("--reminders", action="store_true", help="物化并派发到点的提醒后退出")
    parser.add_argument("--add-problem", metavar="SUBJECT", help="录入一道错题后退出")
    parser.add_argument("--stem", metavar="TEXT", help="配合 --add-problem:题干")
    parser.add_argument("--answer", metavar="TEXT", help="配合 --add-problem:正确答案")
    parser.add_argument("--kp", metavar="NAME", action="append", default=None, help="知识点,可重复")
    parser.add_argument("--image", metavar="PATH", action="append", default=None, help="题目图片路径,可重复")
    parser.add_argument("--problems", metavar="N", nargs="?", const="10", help="列出最近 N 道错题")
    parser.add_argument("--weak-points", action="store_true", help="薄弱知识点统计")
    parser.add_argument("--child-card", metavar="UNIT_ID", type=int, help="按孩子视角渲染一张复习卡")
    parser.add_argument("--review", metavar="N", nargs="?", const="10", help="取今天的复习清单(孩子端)")
    parser.add_argument("--record-review", metavar="UNIT_ID", type=int, help="记录一次复习结果")
    parser.add_argument("--rating", metavar="1|3", type=int, default=3, help="配合 --record-review:1=忘了,3=会了")
    parser.add_argument("--intake-image", metavar="PATH", action="append", default=None,
                        help="拍照录入:把错题照片交给视觉模型识别后进候选区(可重复)")
    parser.add_argument("--auto", action="store_true",
                        help="配合 --intake-image:识别结果直接入库,不再逐条确认")
    parser.add_argument("--student", metavar="NAME", help="这批错题是谁的(默认学生)")
    parser.add_argument("--solve", action="store_true",
                        help="配合 --intake-image:照片上没有答案时让模型补全答案与解析")
    parser.add_argument("--study-settings", action="store_true", help="打印错题设置与今日推送状态")
    parser.add_argument("--set-push-time", metavar="HH:MM", help="设定每日复习推送时间,例如 19:30")
    parser.add_argument("--push", dest="push_on", action="store_true", default=None,
                        help="打开每日复习推送")
    parser.add_argument("--no-push", dest="push_on", action="store_false", default=None,
                        help="关掉每日复习推送")
    parser.add_argument("--push-now", action="store_true",
                        help="立刻生成一次复习推送正文(不等推送时间,用于验证)")
    parser.add_argument("--materials", metavar="N", nargs="?", const="20",
                        help="列出最近 N 份素材(手机发来的原件都在这儿)")
    parser.add_argument("--ingest-material", metavar="ID", type=int,
                        help="把某份素材走一遍识别 -> 直接录成错题")
    parser.add_argument("--sheet", action="store_true",
                        help="生成今天的错题卷(Markdown,产物层)并打印路径")
    parser.add_argument("--sheet-html", action="store_true",
                        help="出今天的**单文件互动错题卷**(HTML:图形与听写音频内联,"
                             "双击就能打开、能发微信)")
    parser.add_argument("--dictation", metavar="WORDS", default=None,
                        help="听写词表(逗号/顿号分隔,如 乌鸦、跳舞、清晨)。单独用=出一份听写页;"
                             "与 --sheet-html 同用=把听写嵌进错题卷")
    parser.add_argument("--voice", metavar="NAME", default="",
                        help="听写音色(默认 LA_TTS_VOICE,如 Cherry)")
    parser.add_argument("--tts-provider", metavar="NAME", default="",
                        help="发声引擎:bailian(阿里云百炼,默认)| sapi(本机离线)")
    parser.add_argument("--repeat", metavar="N", type=int, default=2,
                        help="每个词读几遍(默认 2)")
    parser.add_argument("--gap", metavar="SECONDS", type=float, default=4.0,
                        help="两遍之间的停顿秒数(默认 4)")
    parser.add_argument("--date", metavar="YYYY-MM-DD", default="",
                        help="出错题卷的日期(默认今天;补出昨天/预览明天的卷子用)")
    parser.add_argument("--resolve-answers", metavar="N", nargs="?", const=20, type=int,
                        default=None,
                        help="给最近 N 道错题补**标准答案与图形**(模型独立解题 + 裁剪图;默认 20 道)")
    parser.add_argument("--force", action="store_true",
                        help="配合 --resolve-answers:已经解过的也重解一遍")
    parser.add_argument("--resolve-one", metavar="ID", type=int, default=None,
                        help="只给某一道错题补答案与图形(调试或精修单题用)")
    parser.add_argument("--log-weight", metavar="KG", type=float, help="记一次体重")
    parser.add_argument("--log-workout", metavar="ACTIVITY",
                        help="记一次运动,配合 --minutes / --distance-km / --reps(至少一个)")
    parser.add_argument("--minutes", metavar="N", type=int, default=None,
                        help="配合 --log-workout:时长(分钟)")
    parser.add_argument("--distance-km", metavar="KM", type=float, default=None,
                        help="配合 --log-workout:距离(公里)")
    parser.add_argument("--reps", metavar="N", type=int, default=None,
                        help="配合 --log-workout:个数(仰卧起坐这类)")
    parser.add_argument("--trend", metavar="METRIC", nargs="?", const="weight", help="看某指标趋势(默认体重)")
    parser.add_argument("--energy", metavar="DAYS", nargs="?", const="7", help="能量收支估算")
    parser.add_argument("--health-today", action="store_true", help="今天的健康小结")
    # --- M6 安全与运维 ---------------------------------------------------
    parser.add_argument("--backup", action="store_true", help="立刻备份(账本 + 记忆 + 归档)后退出")
    parser.add_argument("--backups", action="store_true", help="列出已有备份后退出")
    parser.add_argument("--restore-drill", action="store_true",
                        help="把最近一份备份**真的恢复一遍**并核对,不是看文件在不在")
    parser.add_argument("--guardrails", action="store_true", help="跑一遍护栏检查后退出")
    parser.add_argument("--privacy", action="store_true", help="数据与隐私自查后退出")
    parser.add_argument("--report", metavar="DAYS", nargs="?", const="7",
                        help="周报/月报(默认 7 天;--report 30 出月报)后退出")
    parser.add_argument("--rm-expense", metavar="TXN_ID", type=int, help="软删除一笔交易(可撤销)")
    parser.add_argument("--reason", metavar="TEXT", default="", help="配合 --rm-expense:原因")
    parser.add_argument("--undo", action="store_true", help="撤销最近一次删除")
    parser.add_argument("--undo-log", action="store_true", help="列出可撤销的操作")
    parser.add_argument("--receipts", metavar="N", nargs="?", const="10",
                        help="列出最近的写入回执(五个域一张表)")
    parser.add_argument("--undo-receipt", metavar="ID", type=int,
                        help="按回执号撤销一次写入(任意域)")
    parser.add_argument("--import-history", metavar="PATH",
                        help="把既有聊天记录导入为初始记忆(同一文件只导一次)")
    parser.add_argument("--history-session", metavar="ID", default="imported",
                        help="配合 --import-history:导入到哪个会话")
    parser.add_argument("--web", metavar="HOST:PORT", nargs="?", const="127.0.0.1:8765",
                        help="启动本地 Web 界面(默认 127.0.0.1:8765)")
    parser.add_argument("--say", action="append", metavar="TEXT", default=None,
                        help="按顺序跑多轮对话(--once 的多轮版,验收用)")
    args = parser.parse_args(argv)

    if args.session:
        os.environ["LA_SESSION_ID"] = args.session
    settings = config.get_settings(reload=True)

    if args.check:
        return cmd_check(settings)

    # ⭐ 验收闸门**不建会话**:它要能在没有密钥、模型欠费、断网时照跑。
    if args.verify is not None or args.list_checks:
        from .ops import verify as verify_mod

        return verify_mod.cmd_verify(
            level=(args.verify if args.verify is not None else "0"),
            only=str(args.only or ""),
            as_json=bool(args.json),
            dry_run=bool(args.dry_run),
            quiet=bool(args.quiet),
            list_checks=bool(args.list_checks),
            timeout=int(args.verify_timeout or 0),
        )

    # --- M6 运维命令:不启动会话(不需要 API key 也能跑)------------------
    if args.import_history:
        from .ops.history_import import HistoryImportError, import_history

        from .memory.store import MemoryStore

        store = MemoryStore(db.connect_memory())
        try:
            report = import_history(
                store, args.import_history, session_id=args.history_session
            )
        except HistoryImportError as exc:
            print("导入失败:" + str(exc))
            return 1
        print(report.summary())
        if not report.already_done:
            print("导入的是「原始记录」而不是「事实」—— 事实会在真实对话里被提炼。")
        return 0

    if args.backup or args.backups or args.restore_drill or args.guardrails \
            or args.privacy or args.report is not None:
        from .ops import backup as backup_mod

        config.ensure_runtime_dirs()
        if args.backup:
            result = backup_mod.backup_now()
            for item in result.as_dict().get("files", []) or []:
                print("  " + str(item))
            print(result.summary() if hasattr(result, "summary") else result.as_dict())
            removed = backup_mod.prune()
            if removed:
                print("(按保留策略清掉了 " + str(removed) + " 份旧备份)")
            return 0
        if args.backups:
            items = backup_mod.list_backups()
            if not items:
                print("还没有备份。跑 python -m la --backup")
                return 1
            for item in items:
                print("- " + str(item.get("name")) + "  " + str(item.get("size_mb")) + " MB  "
                      + str(item.get("created_at") or ""))
            return 0
        if args.restore_drill:
            report = backup_mod.restore_drill()
            print(report.render() if hasattr(report, "render") else report.as_dict())
            return 0 if report.ok else 1
        if args.report is not None:
            from .ops.report import build_report

            memory_conn = db.connect_memory()
            report = build_report(
                db.connect_assistant(), memory_conn, days=int(args.report)
            )
            print(report.render())
            return 0
        if args.guardrails:
            from .ops.guardrails import run_all

            results = run_all(
                assistant=db.connect_assistant(), memory=db.connect_memory(),
                settings=settings,
            )
            mark = {"pass": "✅", "fail": "❌", "na": "⏭️ "}
            for item in results:
                print(mark.get(item.status, "?") + " " + item.code + " " + item.title)
                if item.evidence:
                    print("      " + item.evidence)
            failed = [item for item in results if item.status == "fail"]
            print("---")
            print(str(len(results)) + " 条检查,失败 " + str(len(failed)) + " 条")
            return 1 if failed else 0
        if args.privacy:
            from .ops.selfcheck import build_privacy_report

            report = build_privacy_report(
                assistant=db.connect_assistant(), memory=db.connect_memory()
            )
            print(report.render())
            return 0

    from .ui.cli import CLI, configure_stdio

    configure_stdio()
    assistant_conn = db.connect_assistant()
    ledger_conn = assistant_conn
    on_event = _print_phase_event if args.phases else None
    session = MemorySession(
        build_client(settings),
        settings,
        assistant_conn=assistant_conn,
        auto_approve=args.yes,
        approver=None if args.yes else _interactive_approver,
        # M9:模型/领域要用户拍板时,在终端里问一句(与 Web 同一套机制)
        asker=build_cli_asker(assistant_conn),
        on_event=on_event,
        # ⭐ 让模型知道这是在**电脑终端**里说话(纯文本、可以直接给文件路径)
        surface=SURFACE_CLI,
        # --reminders 要自己认领提醒;若会话启动就领走了,它就永远看不到
        auto_briefing=not args.reminders,
    )
    session.start()
    cli = CLI(session)

    if args.add_problem:
        if session.study is None:
            print("没有领域库连接,无法录入。")
            return 1
        record = session.study.add_problem(
            {
                "subject": args.add_problem,
                "stem_text": args.stem,
                "answer_text": args.answer,
                "knowledge_points": args.kp or [],
                "image_paths": args.image or [],
            },
            actor="user",
        )
        print("已录入错题 #" + str(record.id) + "(" + record.subject + ",状态 " + record.status + ")")
        if record.knowledge_points:
            print("知识点:" + "、".join(record.knowledge_points))
        print("已建复习单元 " + str(len(record.review_units)) + " 个"
              + "(原题 1 个 + 每个知识点各 1 个),首次到期 "
              + (record.review_units[0].due_date_local if record.review_units else "-"))
        # 🔊 听写/听力题:声音在录入这一刻做一次绑上(与网页/聊天同一条纪律)
        try:
            bound = session.study.bind_audio(record.id, actor="user")
        except Exception as exc:  # noqa: BLE001 - 声音没做成不影响录题
            bound = {"needed": True, "audio_error": type(exc).__name__ + ": " + str(exc)[:80]}
        if bound.get("needed"):
            print(("🔊 " + str(bound.get("audio_note") or "声音已生成"))
                  if bound.get("audio_artifact_id")
                  else ("🔇 " + str(bound.get("audio_error") or "声音没做成")))
        if args.image:
            print("图片已存 " + str(len(record.asset_ids)) + " 张(内容寻址,重复图片自动去重)")
        return 0

    if args.intake_image:
        return cmd_intake(session, settings, args)

    if args.study_settings:
        if session.study is None:
            print("没有领域库连接,无法读取设置。")
            return 1
        return cmd_study_settings(session)

    if args.set_push_time or args.push_on is not None:
        if session.study is None:
            print("没有领域库连接,无法改设置。")
            return 1
        payload: dict[str, Any] = {}
        if args.set_push_time:
            payload["push_time"] = args.set_push_time
        if args.push_on is not None:
            payload["enabled"] = bool(args.push_on)
        try:
            session.study.update_settings(payload)
        except Exception as exc:
            print("改不了:" + str(exc))
            return 1
        return cmd_study_settings(session)

    if args.push_now:
        if session.study is None:
            print("没有领域库连接,无法生成推送。")
            return 1
        moment = settings.now()
        state = session.study.push_state(now=moment)
        print("推送设置:"
              + ("开" if state["push_enabled"] else "关")
              + " / " + str(state["push_time_local"])
              + " / 今日到期 " + str(state["due_count"]) + " 道"
              + " / 现在 " + str(state["now"]))
        text = session.study.push_digest(now=moment)
        if text is None:
            # 没到点也说清楚"为什么没有" —— 这比打印一片空白有用
            forced = session.study.push_digest(
                now=moment.replace(hour=23, minute=59)
            )
            print("现在还没到推送时间,所以正式推送不会发。到点时的正文会是:")
            print(forced or "(推送已关闭)")
            return 0
        print(text)
        return 0

    if args.materials is not None:
        if session.materials is None:
            print("没有领域库连接,无法读取素材。")
            return 1
        items = session.materials.list(limit=int(args.materials))
        stats = session.materials.stats()
        print("素材共 " + str(stats["total"]) + " 份:" + str(stats["by_handled"]))
        for item in items:
            name = item.original_name or ("素材 #" + str(item.id))
            print("  #" + str(item.id) + " [" + item.channel + "] " + name
                  + "  " + str(round(item.byte_size / 1024, 1)) + " KB"
                  + "  " + item.handled_label)
        return 0

    if args.ingest_material is not None:
        if session.intake is None:
            print("没有领域库连接,无法处理素材。")
            return 1
        outcome = session.intake.ingest_material(args.ingest_material, actor="user")
        print(str(outcome.get("message") or ""))
        for item in outcome.get("recorded") or []:
            print("  → 错题 #" + str(item["problem_id"]) + " " + str(item.get("subject"))
                  + " 单元 " + str(item.get("review_units")) + " 个")
        return 0

    if args.sheet:
        if session.study is None:
            print("没有领域库连接,出不了错题卷。")
            return 1
        try:
            result = session.study.make_sheet()
        except Exception as exc:  # noqa: BLE001
            print("出错题卷失败:" + str(exc))
            return 1
        print("错题卷已生成:" + str(result["items"]) + " 道")
        print("  " + str(config.ARTIFACTS_DIR / result["artifact"]["rel_path"]))
        print("  下载: /artifacts/" + str(result["artifact"]["id"]))
        return 0

    # --- M14:给老错题补标准答案与图形 --------------------------------------
    if args.resolve_one is not None:
        if session.intake is None:
            print("没有录入服务,补不了答案。")
            return 1
        try:
            item = session.intake.resolve_problem(int(args.resolve_one), force=True)
        except Exception as exc:  # noqa: BLE001
            print("补答案失败:" + str(exc))
            return 1
        print("#" + str(item.get("problem_id")) + " [" + str(item.get("answer_status")) + "] "
              + str(item.get("answer") or "(没解出来)"))
        if item.get("answer_note"):
            print("  " + str(item["answer_note"]))
        if item.get("steps"):
            for step in item["steps"]:
                print("  · " + str(step))
        if item.get("figure_asset_id"):
            print("  图形 → 素材 #" + str(item["figure_asset_id"])
                  + ("(" + str(item["figure_kind"]) + ")" if item.get("figure_kind") else ""))
        # 🔊 声音:录进来时该绑的都绑了;没绑的要把原因说出来(D107)
        if item.get("audio_note"):
            print("  " + ("🔊 " if item.get("audio_artifact_id") else "🔇 ")
                  + str(item["audio_note"]))
        for note in item.get("notes") or []:
            print("  备注:" + str(note))
        return 0

    if args.resolve_answers is not None:
        if session.intake is None:
            print("没有录入服务,补不了答案。")
            return 1
        limit = max(1, int(args.resolve_answers))
        print("补标准答案与图形:最多 " + str(limit) + " 道"
              + ("(强制重解)" if args.force else "(只补还没解的)")
              + "。每题 2 次模型调用(看照片 + 独立解题)…")
        try:
            report = session.intake.resolve_answers(limit=limit, force=bool(args.force))
        except Exception as exc:  # noqa: BLE001
            print("补答案失败:" + str(exc))
            return 1
        for item in report["items"]:
            tag = "#" + str(item.get("problem_id"))
            if item.get("error"):
                print("  " + tag + " ❌ " + str(item["error"]))
                continue
            if item.get("skipped"):
                print("  " + tag + " ⏭  " + str(item.get("reason")))
                continue
            print("  " + tag + " [" + str(item.get("answer_status")) + "] "
                  + (str(item.get("answer") or "(没解出来)")[:60]))
            if item.get("answer_note"):
                print("        " + str(item["answer_note"])[:150])
            if item.get("figure_asset_id"):
                print("        图形 → 素材 #" + str(item["figure_asset_id"])
                      + ("(" + str(item["figure_kind"]) + ")" if item.get("figure_kind") else ""))
            if item.get("audio_note"):
                print("        " + ("🔊 " if item.get("audio_artifact_id") else "🔇 ")
                      + str(item["audio_note"]))
            for note in item.get("notes") or []:
                print("        备注:" + str(note))
        print("共 " + str(report["total"]) + " 道:解出 " + str(report["resolved"])
              + " · 跳过 " + str(report["skipped"]) + " · 失败 " + str(report["failed"]))
        return 0

    # --- M14:单文件 HTML 互动错题卷 + 听写 ---------------------------------
    if args.sheet_html or args.dictation is not None:
        if session.study is None:
            print("没有领域库连接,出不了错题卷。")
            return 1
        audio_kwargs: dict[str, Any] = {}
        if args.voice:
            audio_kwargs["voice"] = args.voice
        if args.tts_provider:
            audio_kwargs["provider"] = args.tts_provider
        try:
            if args.sheet_html:
                result = session.study.make_sheet_html(
                    today=args.date or None,
                    dictation_words=args.dictation, repeat=args.repeat, gap_s=args.gap,
                    **audio_kwargs,
                )
            else:
                result = session.study.make_dictation_sheet(
                    args.dictation, repeat=args.repeat, gap_s=args.gap, **audio_kwargs,
                )
        except Exception as exc:  # noqa: BLE001
            print("出卷失败:" + str(exc))
            return 1
        if args.sheet_html:
            print("互动错题卷已生成:" + str(result["items"]) + " 道"
                  + ("(含听写 " + str(result["model"]["dictation_words"] and len(result["model"]["dictation_words"])) + " 词)"
                     if result["model"]["dictation_words"] else ""))
        else:
            print("听写页已生成:" + str(len(result["words"])) + " 个词")
            print("  词表:" + "、".join(result["words"]))
        audio = result.get("audio")
        if audio:
            print("  发声:" + str(audio["provider"]) + " / " + str(audio["voice"])
                  + " · " + str(round(float(audio["seconds"]))) + " 秒"
                  + " · 这一次花 " + str(audio.get("spent_yuan", audio["estimated_yuan"])) + " 元"
                  + "(缓存命中 " + str(audio["cached_hits"]) + "/" + str(audio["segments"]) + " 段;"
                  "全量重生成要 " + str(audio["estimated_yuan"]) + " 元)")
            for note in audio.get("notes") or []:
                print("  备注:" + str(note))
        print("  " + str(config.ARTIFACTS_DIR / result["artifact"]["rel_path"]))
        print("  下载: /artifacts/" + str(result["artifact"]["id"]))
        print("  ⭐ 这是**单文件**页面:图片与音频都在里面,发到微信里点开就能用")
        return 0

    if args.problems is not None:
        if session.study is None:
            print("没有领域库连接,无法读取。")
            return 1
        items = session.study.list_problems({"limit": int(args.problems)})
        if not items:
            print("还没有错题记录。")
        for item in items:
            points = ("  [" + "、".join(item.knowledge_points) + "]") if item.knowledge_points else ""
            stem = (item.stem_text or "(未填题干)")[:40]
            who = ("(" + item.student_name + ")") if item.student_name else ""
            print("  #" + str(item.id) + " " + item.subject + who + " " + stem + points
                  + "  图 " + str(len(item.asset_ids)) + " 张 / 单元 " + str(len(item.review_units))
                  + " / 下次 " + str(item.next_due_local or "-"))
        return 0

    if args.weak_points:
        if session.study is None:
            print("没有领域库连接,无法统计。")
            return 1
        stats = session.study.stats()
        print("错题总数 " + str(stats.total_problems) + " / 今日到期 " + str(stats.due_today))
        for subject, count in stats.by_subject.items():
            print("  " + subject + ": " + str(count))
        if stats.weak_points:
            print("薄弱知识点(按累计答错排序):")
            for row in stats.weak_points:
                print("  " + row.knowledge_point + ": 错题 " + str(row.problem_count)
                      + " / 单元 " + str(row.unit_count) + " / 答错 " + str(row.lapses))
        return 0

    if args.web:
        host, _, port_text = args.web.partition(":")
        import uvicorn

        print("家庭助手 Web 界面: http://" + host + ":" + (port_text or "8765"))
        print("(读取路径不经过模型 —— 关掉 API 也能查看与编辑)")
        # 后台心跳:到点就推错题复习清单(不经过模型)。测试里不启动它 —— 见 create_app。
        from .ops import gate as gate_mod
        from .web.app import create_app

        # ⭐ 自我更新后自动跑 T0(D146)。开关放模块级:它不依赖哪一个 app 实例。
        gate_mod.enable_auto()
        # ⚠️ 必须把**这个**实例交给 uvicorn,不能再用字符串 "la.web.app:app":
        #    字符串会让 uvicorn 重新导入并服务**模块级** app(with_ticker=False),
        #    于是 create_app(with_ticker=True) 建的 startup 事件绑在一个被丢弃的
        #    实例上 —— 后台跑批永远不会启动(2026-11-10 云端实测:日志里没有 [wake])。
        app = create_app(with_ticker=True)
        uvicorn.run(app, host=host or "127.0.0.1",
                    port=int(port_text or 8765), log_level="warning")
        return 0

    if args.log_weight is not None:
        if session.health is None:
            print("没有领域库连接,无法记录。")
            return 1
        out = session.health.log_measurement(
            {"metric_code": "weight", "value": args.log_weight, "source": "user"}, actor="user"
        )
        print("记下了:" + str(out["value"]) + out["unit"] + "(" + out["local_date"] + ")")
        return 0

    if args.log_workout:
        if session.health is None:
            print("没有领域库连接,无法记录。")
            return 1
        if args.minutes is None and args.distance_km is None and args.reps is None:
            # 与工具层同一条规矩:一个维度都没有就**不写**,也不替用户编一个。
            print("至少给一个维度:--minutes(时长)/ --distance-km(距离)/ --reps(个数)")
            return 2
        out = session.health.log_workout(
            {"activity": args.log_workout, "duration_min": args.minutes,
             "distance_km": args.distance_km, "reps": args.reps},
            actor="user",
        )
        line = out["activity"] + " " + out["amount_text"] + " → "
        if out["met_minutes"] is not None:
            line += str(out["met_minutes"]) + " MET-分钟,强度" + out["intensity"]
        else:
            line += "没记时长,不算消耗(强度标签:" + out["intensity"] + ")"
        if "kcal_estimate" in out:
            line += ",约 " + str(out["kcal_estimate"]) + " 千卡(估算)"
        print(line)
        return 0

    if args.trend:
        if session.health is None:
            print("没有领域库连接,无法读取。")
            return 1
        result = session.health.trend({"metric_code": args.trend, "days": 90})
        print(result.summary)
        if result.weekly_rate is not None:
            print("(趋势线有约 " + str(result.trend_lag_days) + " 天滞后,属正常)")
        return 0

    if args.energy:
        if session.health is None:
            print("没有领域库连接,无法统计。")
            return 1
        summary = session.health.energy_summary(days=int(args.energy))
        if "missing" in summary:
            print(summary["missing"])
            return 0
        low, high = summary["deficit_range"]
        print("BMR 约 " + str(summary["bmr"]) + " 千卡 / TDEE 约 " + str(summary["tdee"])
              + " 千卡(" + str(summary["activity_label"]) + ")")
        print("摄入约 " + str(summary["intake_kcal"]) + " 千卡 / 运动消耗约 "
              + str(summary["burn_kcal"]) + " 千卡")
        print("热量缺口约 " + str(summary["deficit_kcal"]) + " 千卡,区间 "
              + str(low) + " ~ " + str(high) + "(公式估算,日误差常达 ±400)")
        history = session.health.estimate_tdee_from_history(days=21)
        if history.get("available"):
            print("用你自己的历史反推 TDEE ≈ " + str(history["estimated_tdee"])
                  + " 千卡(更准)")
        else:
            print("历史反推:" + history["reason"])
        print(summary["disclaimer"])
        return 0

    if args.health_today:
        if session.health is None:
            print("没有领域库连接,无法读取。")
            return 1
        summary = session.health.daily_summary()
        print(summary["date"] + ":" + str(summary["meal_count"]) + " 餐 / 摄入约 "
              + str(summary["intake_kcal"]) + " 千卡 / 运动 " + str(summary["met_minutes"])
              + " MET-分钟")
        for item in summary["measurements"]:
            print("  " + item["metric"] + " " + str(item["value"]) + item["unit"])
        return 0

    if args.review is not None:
        if session.study is None:
            print("没有领域库连接,无法取复习清单。")
            return 1
        queue = session.study.review_queue(limit=int(args.review))
        if not queue:
            print("今天没有要复习的题 —— 明天再来。")
            return 0
        print("今天有 " + str(len(queue)) + " 道要复习:")
        for card in queue:
            print("  单元 #" + str(card["unit_id"]) + "  [" + str(card["subject"]) + "] "
                  + str(card["stem_text"] or "(无题干)")[:50]
                  + ("  (配图 " + str(len(card["asset_ids"])) + " 张)" if card["asset_ids"] else ""))
        print("复习完用:--record-review 单元号 --rating 1|3")
        return 0

    if args.record_review is not None:
        if session.study is None:
            print("没有领域库连接,无法记录。")
            return 1
        result = session.study.record_review(args.record_review, args.rating, actor="user")
        from .domain.study.scheduler import RATING_LABELS

        print("记下了:" + RATING_LABELS.get(args.rating, str(args.rating))
              + " → 下次复习 " + result.due_date_local
              + "(间隔 " + str(round(result.interval_days, 2)) + " 天,状态 " + result.state + ")")
        return 0

    if args.child_card:
        if session.study is None:
            print("没有领域库连接,无法渲染。")
            return 1
        card = session.study.render_card(args.child_card, perspective="child")
        print("【" + str(card["subject"]) + "】" + str(card["stem_text"] or "(无题干)"))
        if card["asset_ids"]:
            print("(配图 " + str(len(card["asset_ids"])) + " 张)")
        print("按钮:" + " / ".join(b["label"] for b in card["buttons"]))
        print(str(card["guidance"]))
        return 0

    if args.agenda is not None:
        if session.calendar is None:
            print("没有领域库连接,无法读日程。")
            return 1
        days = int(args.agenda)
        now = settings.now()
        from datetime import timedelta as _td

        end = now + _td(days=days)
        occurrences, truncated = session.calendar.expand(start=now.isoformat(), end=end.isoformat())
        if not occurrences:
            print("未来 " + str(days) + " 天没有日程。")
        for item in occurrences:
            mark = "✎" if item.is_override else ("↻" if item.is_recurring else " ")
            finish = item.end.strftime("%H:%M") if item.end else ""
            print(
                "  " + mark + " " + item.start.strftime("%m-%d %H:%M") + "-" + finish
                + "  " + item.title + ("  @" + item.location if item.location else "")
            )
        if truncated:
            print("  (结果被截断,窗口过大)")
        return 0

    if args.ics:
        if session.calendar is None:
            print("没有领域库连接,无法导出。")
            return 1
        path = session.calendar.write_ics(args.ics)
        print("已导出 " + str(path))
        return 0

    if args.reminders:
        if session.reminders is None:
            print("没有领域库连接,无法处理提醒。")
            return 1
        now = settings.now()
        created = session.reminders.materialize(now=now)
        pending = session.reminders.pending(now=now)
        if not pending:
            print("没有到点的提醒(本次物化了 " + str(created) + " 条)。")
            return 0
        for instance in pending:
            if not session.reminders.claim(instance.id, now=now):
                continue
            event = session.calendar.get(instance.event_uid) if session.calendar else None
            title = event.title if event is not None else instance.event_uid
            print("  ⏰ " + instance.fire_at_local(settings.timezone).strftime("%m-%d %H:%M")
                  + "  " + title)
        return 0

    if args.rm_expense is not None:
        if session.ledger is None:
            print("没有领域库连接,无法删除。")
            return 1
        result = session.ledger.soft_delete(
            int(args.rm_expense), reason=args.reason, actor="user"
        )
        print("已删除(可撤销): #" + str(result.id) + " " + str(result.payee or "")
              + " " + str(result.amount_cents / 100) + " 元")
        print("撤销:python -m la --undo")
        return 0

    if args.undo:
        if session.ledger is None:
            print("没有领域库连接,无法撤销。")
            return 1
        from .domain.undo import UndoError

        try:
            info = session.ledger.undo_last(actor="user")
        except UndoError as exc:
            print("不能撤销:" + str(exc))
            return 1
        print("已撤销:" + info["label"])
        return 0

    if args.undo_log:
        if session.ledger is None:
            print("没有领域库连接,无法读取。")
            return 1
        entries = session.ledger.undo.recent(limit=10, unused_only=False)
        if not entries:
            print("还没有任何可撤销的操作。")
            return 0
        for entry in entries:
            print(("- " if not entry.used else "- (已用过) ") + "#" + str(entry.id)
                  + " " + entry.ts[:16] + " " + entry.label)
        return 0

    if args.receipts is not None:
        # ⭐ 五个域共用的写入回执(M7):比 --undo-log 宽 —— 那边只管账本的软删除。
        from .domain.receipts import format_receipts

        if session.receipts is None:
            print("没有领域库连接,读不到回执。")
            return 1
        items = session.receipts.recent(limit=int(args.receipts))
        tasks = (
            session.memory_receipts.recent(limit=int(args.receipts))
            if session.memory_receipts is not None else []
        )
        merged = sorted([*items, *tasks], key=lambda item: item.id, reverse=True)
        print(format_receipts(merged, limit=int(args.receipts)))
        print("(撤销:python -m la --undo-receipt <回执号>)")
        return 0

    if args.undo_receipt is not None:
        if session.reverter is None:
            print("没有可撤销的写入通道。")
            return 1
        from .domain.receipts import ReceiptError

        try:
            info = session.reverter.undo(int(args.undo_receipt))
        except ReceiptError as exc:
            print("不能撤销:" + str(exc))
            return 1
        print("已撤销:" + str(info["note"]))
        return 0

    if args.ledger is not None:
        if session.ledger is None:
            print("没有领域库连接,无法读账本。")
            return 1
        limit = int(args.ledger)
        rows = ledger_conn.execute(
            "SELECT id, ts, payee, status, source FROM app_txns_live ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        if not rows:
            print("账本还是空的。")
        for row in rows:
            print(
                "- #" + str(row["id"]) + " " + str(row["ts"])[:16] + " "
                + (row["payee"] or "(无对方)") + "  [" + str(row["status"]) + "] "
                + str(row["source"])
            )
        return 0

    if args.query_expense:
        if session.ledger is None:
            print("没有领域库连接,无法查询。")
            return 1
        from .domain.schemas.ledger import ExpenseQuery

        result = session.ledger.query_expenses(
            ExpenseQuery(period=args.query_expense, include_drafts=args.yes)
        )
        print("周期 " + result.period + "(" + result.start[:10] + " → " + result.end[:10] + ")")
        print("合计 " + str(result.total_cents) + " 分(" + str(result.total_cents / 100) + " 元)/ "
              + str(result.txn_count) + " 笔")
        for group in result.groups:
            print("  " + group.key + ": " + str(group.total_cents) + " 分 / "
                  + str(group.txn_count) + " 笔")
        print("来源: " + result.source + "(" + result.note + ")")
        return 0

    if args.export_beancount:
        if session.ledger is None:
            print("没有领域库连接,无法导出。")
            return 1
        print(session.ledger.export_beancount(), end="")
        return 0

    if args.import_bill:
        if session.ledger is None:
            print("没有领域库连接,无法导入。")
            return 1
        report = session.ledger.import_statement(args.import_bill, actor="import")
        print(report.summary())
        if report.fuzzy_candidates:
            print("以下是存疑行(金额与对方在邻近日期已出现过),需要你人工确认:")
            for item in report.fuzzy_candidates:
                print("  第 " + str(item["line"]) + " 行 " + str(item["ts"])[:16] + " "
                      + str(item["payee"]) + " " + str(item["amount_cents"]) + " 分"
                      + " → 疑似与 #" + ",".join(str(x) for x in item["candidates"]) + " 重复")
        session.relay_outbox()
        return 0

    if args.briefing:
        briefing = session.dispatcher.build_briefing(settings.now())
        print(briefing.render())
        return 0

    if args.todos is not None:
        items = session.todos.list(status=args.todos)
        if not items:
            print("没有待办。")
        for item in items:
            due = " (" + str(item.due_date_local) + " 到期)" if item.due_date_local else ""
            print("- [" + item.status + "] " + item.content + due + "  " + item.id)
        return 0

    if args.goals:
        items = session.goals.list(open_only=False)
        if not items:
            print("没有目标。")
        for item in items:
            print("- " + item.render() + "  " + item.id + "  revision=" + str(item.revision))
        return 0

    if args.dump:
        messages, total = session.projection_preview()
        print("== 本轮投影 " + str(len(messages)) + " 条 / 约 " + str(total)
              + " token(压缩触发 " + str(config.BUDGET_COMPACT_TRIGGER)
              + " / 硬上限 " + str(config.BUDGET_HARD_MAX) + ")==")
        for index, message in enumerate(messages):
            print("--- [" + str(index) + "] " + str(message["role"]) + " ---")
            print(str(message["content"]))
        return 0

    if args.compact:
        summary_id = asyncio.run(session.compact_once(via="cli"))
        print("压缩结果: " + ("已生成摘要 id=" + str(summary_id) if summary_id else "无可用区间"))
        return 0

    if args.say:
        for text in args.say:
            asyncio.run(cli.run_once(text, echo_user=False, prefix=""))
            print()
        return 0

    if args.once is not None:
        # --once 是"跑一轮就退出",没有常驻界面 —— 但**执行反馈不能因此消失**。
        # 直写模式下用户更需要看到"它到底写了什么",所以要打步骤与回执。
        # ⚠️ 两本回执账各记各的水位线(2026-10-18 审计修):以前只取 assistant.db 的,
        #    待办/提醒(住在 memory.db)这一轮写了什么永远看不见。
        receipt_marks = session.receipt_marks()
        started = time.perf_counter()

        def on_step(kind: str, data: dict[str, Any]) -> None:
            if kind != "step" or not data.get("title"):
                return
            mark = {"running": "…", "done": "✔", "failed": "✗"}.get(
                str(data.get("status")), "…"
            )
            print("  " + mark + " " + str(data["title"]))

        session._on_step = on_step  # noqa: SLF001 - CLI 是这套机制的第二个消费者
        asyncio.run(cli.run_once(args.once, echo_user=False, prefix=""))
        merged = session.receipts_since(receipt_marks)
        if merged:
            from .domain.receipts import format_receipts as _fmt_receipts

            print("")
            print("本轮写入 " + str(len(merged)) + " 次(可撤销):")
            print(_fmt_receipts(merged))
        print("用时 " + format_duration((time.perf_counter() - started) * 1000))
        return 0
    return asyncio.run(cli.repl())


if __name__ == "__main__":
    raise SystemExit(main())