"""后台心跳:让"到点推送"在只开着 Web 服务时也真的发生。

⭐ 为什么需要它:错题复习清单是**确定性调度器**算出来的(不看模型脸色),但调度器
   总得有人喊它一声。原来的两个入口都是"用户来了才动"(开会话、跑 CLI)——
   于是"晚上 7 点半推送"这件事实际上变成了"晚上 7 点半之后你第一次打开时才推送"。

   这个线程就是那个"喊一声"的人:每分钟看一眼,该推就推。

⚠️ 三条边界,一条都不能越:
   1. **只做错题推送**。日程提醒不在这里认领 —— 认领 = 已通知,而这里没有真正的
      送达渠道,抢先认领会把提醒悄悄吃掉(用户永远收不到)。
   2. **不碰模型**。心跳里出现任何 LLM 调用,都会把"关掉 API 也能用"这条底线弄丢。
   3. **出错只记日志**。一个后台线程抛异常静默死掉是最难查的故障,所以每轮都兜住,
      并留下一行 stderr —— 与项目其它地方的"不吞异常"口径一致。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from typing import Any

from .. import config
from .deps import build_dispatcher, build_services

DEFAULT_INTERVAL_S = 60.0
TICKER_ENV = "LA_TICKER"        # 0/false/no/off = 关掉后台心跳


def ticker_enabled() -> bool:
    return (os.getenv(TICKER_ENV) or "1").strip().lower() not in ("0", "false", "no", "off")


def tick_once(now: datetime | None = None) -> dict[str, Any]:
    """跑一轮:装配服务 -> 问调度器该不该推 -> 关掉连接。

    ⚠️ **每轮一套连接**,和 Web 请求一样:后台线程持有长期连接是最容易写出
    "database is locked"和跨线程错误的写法(见 D66)。
    """
    report: dict[str, Any] = {"at": "", "study": None, "error": ""}
    services = build_services()
    try:
        moment = now or services.settings.now()
        report["at"] = config.format_local(moment)
        dispatcher = build_dispatcher(services)
        dispatch = dispatcher.deliver_study_push(moment)
        report["study"] = dispatch.id if dispatch is not None else None
    except Exception as exc:                     # 心跳失败不许把线程带走
        report["error"] = type(exc).__name__ + ": " + str(exc)[:200]
        print("[ticker] " + report["error"], file=sys.stderr, flush=True)
    finally:
        services.close()
    return report


class DispatchTicker(threading.Thread):
    """守护线程:每 interval_s 秒 tick 一次。进程退出时自然结束。"""

    def __init__(self, *, interval_s: float = DEFAULT_INTERVAL_S) -> None:
        super().__init__(name="la-dispatch-ticker", daemon=True)
        self.interval_s = max(5.0, float(interval_s))
        self._stop = threading.Event()
        self.last_report: dict[str, Any] = {}
        self.ticks = 0

    def run(self) -> None:  # pragma: no cover - 线程体,靠单测直接调 tick_once 覆盖
        # 先歇一拍再跑第一轮:启动瞬间(迁移、建库)是最忙的时候,别去抢锁
        while not self._stop.wait(self.interval_s):
            try:
                self.last_report = tick_once()
                self.ticks += 1
            except Exception as exc:
                print("[ticker] 轮次异常: " + repr(exc)[:200], file=sys.stderr, flush=True)

    def stop(self) -> None:
        self._stop.set()


def start_ticker(*, interval_s: float = DEFAULT_INTERVAL_S) -> DispatchTicker | None:
    """在 Web 服务里启动心跳。已启动过 / 被环境变量关掉时返回 None。"""
    if not ticker_enabled():
        return None
    thread = DispatchTicker(interval_s=interval_s)
    thread.start()
    return thread
