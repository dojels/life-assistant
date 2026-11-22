"""真机验收:tts_audio 的那条录音能不能作为文件走到聊天窗口(D128)。

⚠️ 服务重启**不算**验证。这里用真配置(真 TTS 引擎、真回执账、真工具处理器)跑一遍,
   判据只看一件事:**这一轮的出站文件里有没有那个 mp3**。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from la.bridge.bridge import Thread  # noqa: E402
from la.bridge.types import Inbound  # noqa: E402
from la.domain.receipts import DOMAIN_ARTIFACT  # noqa: E402
from la.domain.tools import build_study_tools  # noqa: E402
from la.llm.client import ChatResult  # noqa: E402


class Silent:
    """不出声的占位 transport(这里不需要真的发微信)。"""

    name = "silent"

    async def send(self, action):  # noqa: ANN001
        return None

    async def typing(self, thread_id: str) -> None:
        return None


def main() -> int:
    import time

    from la import config
    from la.bridge import Bridge

    # ⚠️ 每次用**新的临时会话**:不然会往真用户那本账里塞一条"验证用"的回执/产物
    probe_id = "verify-audio-" + str(int(time.time()))
    bridge = Bridge(settings=config.get_settings(), transport=Silent())
    thread = bridge.thread(probe_id)
    session = thread.session
    handler = next(
        item for item in build_study_tools(session.study, session_id=session.session_id)
        if item.name == "tts_audio"
    ).handler

    async def fake_respond(user_text, **kwargs):  # noqa: ANN001, ANN003
        result = await handler({"text": "Monday、Tuesday", "mode": "dictation"})
        print("delivered =", result["delivered"], "| receipt_id =", result["receipt_id"])
        print("file_name =", result["file_name"], "| seconds =", result["seconds"])
        print("spent_yuan =", result["spent_yuan"], "| provider =", result["provider"])
        return ChatResult(text="录音做好了。", model="stub")

    session.respond = fake_respond
    before = session.receipts.last_id()

    async def drain():
        events = []
        async for event in thread.handle(Inbound(
            channel="weixin", user_id=probe_id, thread_id=probe_id,
            text="把这两个单词读一遍发我", external_id="verify:tts:1",
        )):
            events.append(event)
        return events

    events = asyncio.run(drain())
    files = [event["data"] for event in events if event["event"] == "file"]
    fresh = [
        item for item in session.receipts.since(before)
        if item.domain == DOMAIN_ARTIFACT
    ]
    print()
    print("本轮 artifact 回执:", [(item.kind, item.detail.get("name")) for item in fresh])
    print("本轮出站文件:", [(item["name"], Path(item["path"]).exists()) for item in files])
    ok = len(files) == 1 and Path(files[0]["path"]).exists() and files[0]["name"].endswith(".mp3")
    print()
    print("结果:", "PASS" if ok else "FAIL")
    bridge.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
