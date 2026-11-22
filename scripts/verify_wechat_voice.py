"""微信语音(D139)的验收:一句话由转写文字进来,不再多出一个"我不认识的附件"。

⚠️ 这个脚本测的不是"能不能跑",而是**回话里那句话是不是真的**:
   ① 语音消息的正文 = 微信转写的那段话,而且**原声不再下载**(不必、也不该);
   ② 没有转写的语音仍然要留原件,但承诺必须收窄 —— **不许说"你可以让我读它的内容"**;
   ③ "图 + 语音"这种混排消息,两样都要保住(以前只读 item_list[0],语音文字会整段丢);
   ④ 分流时**原始下标不变**(幂等键里有它,变了会让重发消息重复入素材);
   ⑤ AMR / SILK 被认成音频,而不是"二进制文件"(模型才不会说"文件读不出内容");
   ⑥ 适配器真的走这条分流(防止以后有人把它改回去)。

跑法:.venv/Scripts/python.exe scripts/verify_wechat_voice.py
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from la.base import extract, sniff  # noqa: E402
from la.base.tools import build_base_tools  # noqa: E402
from la.bridge.adapters.weixin_ilink import ILinkAdapter, split_media_refs  # noqa: E402
from la.bridge.adapters.weixin_media import media_refs_from_items  # noqa: E402
from la.bridge.adapters.weixin_protocol import parse_incoming  # noqa: E402
from la.bridge.bridge import _stored_note  # noqa: E402
from la.domain.media import _EXTENSIONS  # noqa: E402

AMR = b"#!AMR\n" + b"\x3c" * 64
SILK = b"\x02#!SILK_V3" + b"\x00" * 64

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASSED.append(label)
        print("  PASS  " + label)
    else:
        FAILED.append(label + (("  <- " + detail) if detail else ""))
        print("  FAIL  " + label + (("  <- " + detail) if detail else ""))


def _raw(items: list[dict]) -> dict:
    return {
        "from_user_id": "wxid_me",
        "message_id": "verify-voice-1",
        "context_token": "ctx",
        "item_list": items,
    }


def main() -> int:
    print("[1/7] 解析:语音的转写文字就是正文")
    message = parse_incoming(_raw([
        {"type": 3, "voice_item": {
            "text": "今天我一共花了多少钱",
            "media": {"encrypt_query_param": "p", "aes_key": "0" * 32},
        }},
    ]))
    check("解析得出来", message is not None)
    check("正文 = 转写文字", message is not None and message.text == "今天我一共花了多少钱",
          "" if message is None else message.text)
    check("记下了 voice_transcribed=1", message is not None and message.voice_transcribed == 1)

    print("[2/7] 分流:带转写的语音不下原声")
    pending, transcribed = split_media_refs(message.media if message else [])
    check("这条语音不进下载队列", pending == [], str(pending))
    check("计为 1 条已转写", transcribed == 1, str(transcribed))

    no_text = parse_incoming(_raw([{"type": 3, "voice_item": {
        "media": {"encrypt_query_param": "p"}}}])
    )
    pending2, transcribed2 = split_media_refs(no_text.media)
    check("没转写的语音仍要取原件", len(pending2) == 1 and transcribed2 == 0,
          "pending=" + str(len(pending2)))
    check("正文为空(交给桥接层如实说明)", no_text.text == "", no_text.text)

    print("[3/7] 混排:图 + 语音,两样都要保住")
    mixed = parse_incoming(_raw([
        {"type": 2, "image_item": {"media": {"encrypt_query_param": "img"}}},
        {"type": 3, "voice_item": {"text": "这道题是英语的"}},
    ]))
    check("语音文字没被丢掉", mixed.text == "这道题是英语的", mixed.text)
    pending3, transcribed3 = split_media_refs(mixed.media)
    check("只下载图片", [ref.kind for _, ref in pending3] == ["image"],
          str([ref.kind for _, ref in pending3]))
    check("下标保真(图片仍是 0)", [index for index, _ in pending3] == [0],
          str([index for index, _ in pending3]))
    check("语音那一条计为已转写", transcribed3 == 1)

    print("[4/7] 承诺边界:语音不许说「你可以让我读它的内容」")
    voice_note = _stored_note(SimpleNamespace(id=9, original_name=None, kind="voice",
                                             media_type="audio/amr"))
    check("语音:说了读不出", "读不出" in voice_note, voice_note)
    check("语音:没有那句空承诺", "你可以直接让我读它的内容" not in voice_note, voice_note)
    file_note = _stored_note(SimpleNamespace(id=10, original_name="周报.docx", kind="file",
                                            media_type="application/pdf"))
    check("文件:仍可以说能读", "你可以直接让我读它的内容" in file_note, file_note)

    print("[5/7] 类型:AMR / SILK 是音频,不是二进制")
    check("AMR → audio/amr", sniff.sniff_media_type(AMR) == "audio/amr",
          sniff.sniff_media_type(AMR))
    check("SILK → audio/silk", sniff.sniff_media_type(SILK) == "audio/silk",
          sniff.sniff_media_type(SILK))
    check("detect_kind = audio", sniff.detect_kind(AMR) == "audio")
    audio = extract.extract_text(AMR, name="语音.amr")
    check("抽取层说「音频」而不是「二进制」", audio.kind == "audio" and "音频" in audio.note,
          audio.note)
    check("抽取层提到「转写」", "转写" in audio.note, audio.note)
    check("落盘扩展名对得上", _EXTENSIONS.get("audio/amr") == ".amr"
          and _EXTENSIONS.get("audio/silk") == ".silk")

    print("[6/7] 面板与适配器:文案与代码都没走回头路")
    item = SimpleNamespace(
        id=9, kind="voice", media_type="audio/amr", original_name=None, channel="weixin",
        handled_label="已留档", created_at="2026-10-17T12:13:10+08:00",
    )
    specs = build_base_tools(materials=SimpleNamespace(list=lambda kind=None, limit=10: [item]))
    listing_spec = next(spec for spec in specs if spec.name == "material_list")
    listing = asyncio.run(listing_spec.handler({"limit": 5}))
    check("素材列表提醒语音读不出", "语音是音频" in listing, listing[-120:])
    check("适配器走 split_media_refs",
          "split_media_refs" in inspect.getsource(ILinkAdapter._handle_raw))
    refs = media_refs_from_items([{"type": 3, "voice_item": {"text": "好啊"}}])
    check("media_refs 标记已转写", refs and refs[0].transcribed and refs[0].transcript == "好啊")

    print("[7/7] 认不出类型的内容:当没发,不当附件")
    odd = parse_incoming(_raw([
        {"type": 1, "text_item": {"text": "在吗"}},
        {"type": 7, "location_item": {"poi": "某处"}},
    ]))
    check("正文照旧", odd.text == "在吗", odd.text)
    check("不当附件", odd.media == [], str([ref.kind for ref in odd.media]))
    check("只留类型号给日志", odd.unknown_item_types == [7], str(odd.unknown_item_types))

    total = len(PASSED) + len(FAILED)
    print("")
    print("通过 " + str(len(PASSED)) + "/" + str(total))
    if FAILED:
        print("失败:")
        for line in FAILED:
            print("  - " + line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
