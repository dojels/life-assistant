"""微信 iLink 常驻服务入口。

用法:

    # 首次:扫码登录,然后开始常驻
    .venv/Scripts/python.exe -m la.bridge.adapters.weixin_ilink_main

    # 强制重新扫码(换号 / 会话过期后手动重来)
    .venv/Scripts/python.exe -m la.bridge.adapters.weixin_ilink_main --relogin

    # 只读观察:正常轮询,但**不回复**(用于"先只读跑一周"那一步)
    .venv/Scripts/python.exe -m la.bridge.adapters.weixin_ilink_main --dry-run

    # 冒烟:只拉一轮就退出(确认链路通,不常驻)
    .venv/Scripts/python.exe -m la.bridge.adapters.weixin_ilink_main --once

    # 多微信号(爸爸/妈妈各扫各的):一个进程一个账号,彼此凭据/游标隔离
    .venv/Scripts/python.exe -m la.bridge.adapters.weixin_ilink_main --account dad
    .venv/Scripts/python.exe -m la.bridge.adapters.weixin_ilink_main --account mom

⚠️ 运行前请确认微信里已有 ClawBot 插件入口(我 → 设置 → 插件)。
⚠️ 电脑关机 / 这个进程不在 = 收不到消息(但**不需要**开微信客户端)。

⭐ 这个进程还负责**到点提醒的主动推送**(la/bridge/reminder_push.py)。
   为什么非它不可:主动推送需要一个**还活着的 context_token**,而它只存在于这里
   —— 长轮询换来的、内存里的那一份。Web 进程的后台心跳拿不到它,所以那边
   只能推错题清单,推不了提醒(见 la/web/ticker.py 开头的三条边界)。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from ... import config
from ..bridge import Bridge
from .weixin_ilink import ILinkAdapter
from .weixin_protocol import SessionExpired


def _log(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _start_reminder_push(adapter, settings):
    """把"到点提醒"挂到常驻进程上。返回 (pusher, task)。

    ⚠️ 主动推送要一个**还活着的 context_token**,而它只存在于这里(长轮询换来的、
    内存里的那一份)。Web 进程的心跳拿不到它,所以那边只推错题清单。
    """
    from ...storage import db

    from ..reminder_push import ReminderPusher

    pusher = ReminderPusher(
        db.connect_memory(),
        db.connect_assistant(),
        # 家庭版:默认推到所有还有窗口的联系人;提醒指定了收件人时走 send_to。
        send=adapter.push_text_all,
        send_to=adapter.push_text_to_member,
        settings=settings,
        log=_log,
    )
    task = asyncio.create_task(pusher.run_forever())
    _log(
        "到点提醒:已挂上主动推送(每 " + str(int(pusher.interval_s)) + " 秒看一次;"
        "窗口关着就先攒着,超过 " + str(int(pusher.max_late_s // 60)) + " 分钟不再补发)"
    )
    return pusher, task


def _start_study_push(adapter, settings):
    """每日错题推送(D162):到点发清单 + 合并听写。"""
    from ..study_push import StudyPusher

    pusher = StudyPusher(
        send=adapter.push_text_to_member,
        send_file=adapter.push_file_to_member,
        log=_log,
        now=settings.now,
    )
    task = asyncio.create_task(pusher.run_forever())
    _log("每日错题推送:已挂上(到点发清单 + 合并听写;收件人按设置页的勾选)")
    return pusher, task


async def _run(args: argparse.Namespace) -> int:
    settings = config.get_settings()
    adapter: ILinkAdapter | None = None
    bridge: Bridge | None = None
    pusher = None
    push_task: asyncio.Task | None = None
    study_task: asyncio.Task | None = None
    try:
        # 先造 adapter 再造 bridge:adapter 的 transport 要交给 bridge
        adapter = ILinkAdapter(bridge=None, log=_log, account=args.account)
        if args.dry_run:
            # ⚠️ 只读 = **不回复、不写库**(2026-10-18 审计修)。以前这里只包了一层
            #    打印,原函数照常回复、照常写库 —— 日志是假的。现在连 Bridge 都不建:
            #    不建会话就不会写事件/回执,没有 transport 就不会发消息。
            async def _observe(raw: dict) -> None:
                _log("[dry-run] 收到一条消息,不回复、不写库:" + repr(raw)[:200])

            adapter._handle_raw = _observe  # type: ignore[method-assign]  # noqa: SLF001
            bridge = None
        else:
            bridge = Bridge(settings=settings, transport=adapter.transport)
            adapter.bridge = bridge

        _log("家庭助手 · 微信渠道(iLink / ClawBot)"
             + (" · 账号 " + adapter.account if adapter.account else ""))
        _log("联系人绑定成员后写 member:<id>(与 Web 同一时间线);未绑定暂用 bridge:<wxid>")
        if args.dry_run:
            _log("⚠️ 只读模式:只拉取与打印,不回复、不写数据库、不派发提醒")

        try:
            await adapter.login(force=args.relogin)
        except Exception as exc:  # noqa: BLE001
            _log("登录失败:" + type(exc).__name__ + " " + str(exc)[:300])
            return 1

        _log("开始监听" + ("(只拉一轮)" if args.once else "…Ctrl+C 退出"))
        max_polls = 1 if args.once else None
        if not args.dry_run and not args.once:
            # 只读模式(--dry-run)不挂推送:主动发消息不是"只读"。
            # --once 是冒烟(只拉一轮就退出),挂上也没意义。
            pusher, push_task = _start_reminder_push(adapter, settings)
            _, study_task = _start_study_push(adapter, settings)
        await adapter.run_forever(max_polls=max_polls)
        return 0
    except KeyboardInterrupt:
        _log("")
        _log("收到 Ctrl+C,退出")
        return 0
    finally:
        for task in (push_task, study_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 退出时不该因为推送任务报错改变退出码
                pass
        if pusher is not None:
            pusher.close()
        if adapter is not None:
            _log("统计:" + str(adapter.stats))
            await adapter.aclose()
        if bridge is not None:
            bridge.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="la-weixin", description="微信官方 iLink(ClawBot)渠道常驻服务"
    )
    parser.add_argument("--relogin", action="store_true", help="忽略已存凭据,重新扫码")
    parser.add_argument("--once", action="store_true", help="只拉一轮就退出(冒烟)")
    parser.add_argument("--dry-run", action="store_true", help="只读观察:轮询但绝不回复")
    parser.add_argument("--account", default="",
                        help="多微信号:账号名(凭据放 data/wechat/accounts/<name>/);默认旧单号路径")
    args = parser.parse_args(argv)

    # ⚠️ 以前这里设过 WindowsSelectorEventLoopPolicy。**已移除**,原因有两条:
    #   1. Python 3.14 起该 API 已废弃(3.16 移除),每次启动都打 DeprecationWarning,
    #      把日志噪音当真问题会浪费排查时间;
    #   2. 实测不需要 —— 微信渠道跑在默认的 ProactorEventLoop 上长轮询正常,
    #      启动、复用凭据、监听都通。
    # 真要换事件循环,应该用 asyncio.Runner(loop_factory=...) —— 那是新 API,
    # 但**没有实测理由之前不要为了"更稳"去换**。
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
