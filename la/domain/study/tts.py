"""听写发声:两条路,一条为主一条为辅,共用一个缓存。

⭐ 2026-10-12 实测定下的路线(用户拍板):

  * **主力 = 阿里云百炼 qwen3-tts-flash**(0.8 元/万字符,官方 HTTP 接口)
    理由:音质明显好过本机,价格按**字符**算 —— 一次听写 20 个词约 120 字符,
    一个学期不重复的词表也就两三千字符,折合**几毛钱**;而且音频按内容哈希缓存,
    同一个词表只生成一次。
  * **辅 = 本机 Windows SAPI**(zh-CN Huihui,离线、零成本、断网可用)
    两个用途:① 百炼没配 key / 调不通时**自动兜底**,听写不能因为欠费就哑掉;
    ② 想立刻听到声音、不想等网络时的快路径。

⚠️ 三条纪律:

  1. **按内容缓存**(`provider|model|voice|rate|text` 的 sha256)。缓存命中就不调服务 ——
     这既是省钱,也是把"服务挂了"挡在门外(与"出卷不依赖模型"同一条纪律)。
  2. **失败必须降级,不许静默**。百炼失败 → 落回 SAPI,并在结果里写清为什么
     (`notes`),由界面/命令行如实告诉用户"这条是离线音,因为××"。
  3. **不自己拼 SSML 之外的东西**。停顿靠**拼接静音**,不靠"求模型停 4 秒" ——
     实测 edge-tts 的 SSML 方案连服务本身都调不通(403),把节奏交给本地代码更稳。

依赖:httpx(已有);MP3 编码 lameenc(可选,**缺了就退回 WAV 并如实说明体积**);
离线路线用 Windows PowerShell 的 System.Speech(不需要装任何 Python 包)。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ... import config

PROVIDER_BAILIAN = "bailian"
PROVIDER_SAPI = "sapi"
PROVIDERS = (PROVIDER_BAILIAN, PROVIDER_SAPI)
#: 自动兜底顺序:先主力,后离线
FALLBACK_ORDER = (PROVIDER_BAILIAN, PROVIDER_SAPI)

BAILIAN_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
DEFAULT_MODEL = "qwen3-tts-flash"
#: 官方文档示例音色。中文女声,吐字清楚 —— 听写要的是"听得清",不是"好听"。
DEFAULT_VOICE = "Cherry"
DEFAULT_SAPI_VOICE = "Microsoft Huihui Desktop"
#: qwen3-tts-flash 官方价:0.8 元 / 万字符(华北2 北京)。只用于**估算**,不参与计费。
PRICE_YUAN_PER_10K_CHARS = 0.8

MP3_HINT = "MP3 编码需要 lameenc:.venv/Scripts/python.exe -m pip install lameenc(没装也能用,只是音频是 WAV,体积大 5 倍)"


class TtsError(RuntimeError):
    """发声失败。调用方应当降级而不是把整条链路打断。"""


@dataclass
class Clip:
    """一段音频。data 是**完整 WAV 字节**,便于拼接与检查。"""

    data: bytes = b""
    provider: str = ""
    voice: str = ""
    seconds: float = 0.0
    cached: bool = False
    error: str = ""
    #: 为什么用了这个引擎(例如"百炼:没有 DASHSCOPE_API_KEY")—— 要如实讲给用户听
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.data) and not self.error


@dataclass
class DictationAudio:
    """一份听写音频 + 它是怎么来的。"""

    data: bytes = b""                 # 最终交付的音频(WAV 或 MP3)
    media_type: str = "audio/wav"
    seconds: float = 0.0
    provider: str = ""
    voice: str = ""
    characters: int = 0
    segments: int = 0
    cached_hits: int = 0
    #: **这一次真的会产生费用的字符数**(缓存命中的段不算 —— 那一段不会再调服务)
    fresh_characters: int = 0
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.data) and not self.error

    @property
    def estimated_yuan(self) -> float:
        """这一份音频**从零生成**要多少钱(数量感,不是账单)。"""
        return round(self.characters / 10000.0 * PRICE_YUAN_PER_10K_CHARS, 6)

    @property
    def spent_yuan(self) -> float:
        """这一次**真的**花了多少:缓存命中的段不会再调服务,也就不花钱。

        ⚠️ 分开报的理由:全命中时说"花了 0.0034 元"是在误导用户 ——
        他会以为每次听写都在扣钱,而实际上同一份词表**只花第一次**。
        """
        return round(self.fresh_characters / 10000.0 * PRICE_YUAN_PER_10K_CHARS, 6)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "media_type": self.media_type,
            "byte_size": len(self.data),
            "seconds": round(self.seconds, 2),
            "provider": self.provider,
            "voice": self.voice,
            "characters": self.characters,
            "segments": self.segments,
            "cached_hits": self.cached_hits,
            "estimated_yuan": self.estimated_yuan,
            "spent_yuan": self.spent_yuan,
            "notes": list(self.notes),
            "error": self.error,
        }


# -- WAV 小工具(只碰 16bit PCM,够用了) --------------------------------------
#: ⚠️ 实测(配上百炼 key 的第一次真调用就撞上):
#:    qwen3-tts-flash 返回的是一个**流式 WAV** —— data 块的长度字段写的是占位值
#:    (实测 0x7FFFFFDB ≈ 2GB),而真实音频只有 30KB。标准库按头部一算,得到
#:    "44739 秒"(12 小时)这种荒唐值;更要命的是按它去读帧会**读空**,
#:    于是拼接出来的听写音频会静默变成一段静音。
#:    所以:**帧数一律按 data 块实际有多少字节算**,不信头部那个数。
def wav_layout(data: bytes) -> tuple[int, int, int, int]:
    """(声道数, 采样率, 采样位宽, data 块起始偏移)。不是 WAV 就抛 TtsError。"""
    import struct

    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise TtsError("这段音频不是 WAV")
    channels = rate = width = 0
    offset = 0
    position = 12
    while position + 8 <= len(data):
        chunk_id = data[position:position + 4]
        size = struct.unpack("<I", data[position + 4:position + 8])[0]
        body = position + 8
        if chunk_id == b"fmt " and body + 16 <= len(data):
            _fmt, channels, rate, _byte_rate, _block, bits = struct.unpack(
                "<HHIIHH", data[body:body + 16]
            )
            width = max(1, bits // 8)
        elif chunk_id == b"data":
            offset = body
            break
        # ⚠️ 流式 WAV 的 size 可能是占位值,别拿它跳转 —— 一格一格找过去
        position = body + (size if size < len(data) else 0)
        if size >= len(data):
            break
    if not channels or not rate or not offset:
        raise TtsError("这段 WAV 少了 fmt 或 data 块")
    return (int(channels), int(rate), int(width), int(offset))


def wav_params(data: bytes) -> tuple[int, int, int]:
    """(声道数, 采样率, 采样位宽)。不是 WAV 就抛 TtsError。"""
    channels, rate, width, _offset = wav_layout(data)
    return (channels, rate, width)


def wav_frames(data: bytes) -> int:
    """**真实**帧数:按 data 块实际有多少字节算(不信头部那个长度字段)。"""
    channels, _rate, width, offset = wav_layout(data)
    block = max(1, channels * width)
    return max(0, (len(data) - offset) // block)


def wav_seconds(data: bytes) -> float:
    try:
        channels, rate, width, offset = wav_layout(data)
    except TtsError:
        return 0.0
    except Exception:  # noqa: BLE001
        return 0.0
    block = max(1, channels * width)
    return float(max(0, (len(data) - offset) // block)) / float(rate or 1)


def silence(seconds: float, params: tuple[int, int, int]) -> bytes:
    """一段静音。⚠️ 必须与相邻音频**同格式**,否则拼出来的是噪声。"""
    channels, rate, width = params
    frames = max(0, int(round(float(seconds) * rate)))
    return b"\x00" * (frames * channels * width)


def _pcm_frames(piece: bytes) -> bytes:
    """取出一段 WAV 的**裸 PCM**(按 data 块实际长度)。"""
    channels, _rate, width, offset = wav_layout(piece)
    block = max(1, channels * width)
    usable = ((len(piece) - offset) // block) * block
    return piece[offset:offset + usable]


def concat_wav(segments: Sequence[bytes], gaps: Sequence[float]) -> bytes:
    """把若干 WAV 段按 (段, 停顿) 拼成一条。

    ⚠️ 格式必须一致(同一 provider、同一音色、同样的采样率)。不一致时**直接抛** ——
    拼出一段噪声比报错更糟:用户听到的是"刺啦"一声,还会以为是自己手机坏了。
    """
    pieces = [item for item in segments if item]
    if not pieces:
        raise TtsError("没有可拼接的音频")
    params = wav_params(pieces[0])
    for index, piece in enumerate(pieces[1:], start=1):
        if wav_params(piece) != params:
            raise TtsError(
                "第 " + str(index + 1) + " 段音频格式与第一段不一致("
                + str(wav_params(piece)) + " vs " + str(params) + "),不能拼接"
            )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(params[0])
        out.setsampwidth(params[2])
        out.setframerate(params[1])
        for index, piece in enumerate(pieces):
            # ⚠️ 按**真实**帧数读:流式 WAV 的 nframes 是占位值(实测 10 亿帧),
            #    照它读会读空 —— 拼出来是一段静音,而且不报错。
            out.writeframes(_pcm_frames(piece))
            gap = float(gaps[index]) if index < len(gaps) else 0.0
            if gap > 0:
                out.writeframes(silence(gap, params))
    return buffer.getvalue()


def wav_to_mp3(data: bytes, *, bitrate: int = 48) -> tuple[bytes, str]:
    """WAV → MP3。**没装 lameenc 就原样返回 WAV**,并把原因写进返回值。"""
    try:
        import lameenc
    except Exception:  # noqa: BLE001
        return data, MP3_HINT
    try:
        channels, rate, _width = wav_params(data)
        frames = _pcm_frames(data)
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(int(bitrate))
        encoder.set_in_sample_rate(int(rate))
        encoder.set_channels(int(channels))
        encoder.set_quality(2)
        payload = encoder.encode(frames) + encoder.flush()
        if not payload:
            return data, "MP3 编码返回空数据,已退回 WAV"
        return payload, ""
    except Exception as exc:  # noqa: BLE001 - 编码失败退回 WAV,不能因此没有音频
        return data, "MP3 编码失败(" + type(exc).__name__ + "),已退回 WAV"


# -- 两个 provider ------------------------------------------------------------
def bailian_wav(
    text: str,
    *,
    voice: str,
    model: str,
    api_key: str,
    timeout_s: float = 60.0,
    http: Any = None,
) -> bytes:
    """调阿里云百炼 qwen3-tts-flash,拿回 WAV 字节。

    ⚠️ 非流式接口**返回的是一个带签名的临时 URL**(expires_at,约 24 小时),
    不是音频本身 —— 所以必须**当场下载**。这里顺序写死:先 POST 拿 url,再 GET 取字节。
    """
    if not api_key:
        raise TtsError("没有 DASHSCOPE_API_KEY(百炼的 key 与 DeepSeek 的不是同一个)")
    payload = {
        "model": model or DEFAULT_MODEL,
        "input": {"text": str(text), "voice": voice or DEFAULT_VOICE, "language_type": "Chinese"},
    }
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    try:
        import httpx
    except Exception as exc:  # noqa: BLE001
        raise TtsError("缺少 httpx:" + type(exc).__name__) from exc
    client = http
    try:
        if client is not None:
            response = client.post(BAILIAN_URL, headers=headers, json=payload)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            body = response.json()
        else:
            with httpx.Client(timeout=timeout_s) as own:
                response = own.post(BAILIAN_URL, headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()
    except Exception as exc:  # noqa: BLE001
        raise TtsError("百炼调用失败:" + type(exc).__name__ + " " + str(exc)[:160]) from exc
    audio = ((body or {}).get("output") or {}).get("audio") or {}
    encoded = str(audio.get("data") or "")
    if encoded:
        try:
            return base64.b64decode(encoded)
        except Exception as exc:  # noqa: BLE001
            raise TtsError("百炼返回的 base64 解不开:" + type(exc).__name__) from exc
    url = str(audio.get("url") or "")
    if not url:
        message = str((body or {}).get("message") or "")[:160]
        raise TtsError("百炼没有返回音频" + ("(" + message + ")" if message else ""))
    try:
        if client is not None:
            got = client.get(url)
            if hasattr(got, "raise_for_status"):
                got.raise_for_status()
            return bytes(got.content)
        with httpx.Client(timeout=timeout_s) as own:
            got = own.get(url)
            got.raise_for_status()
            return bytes(got.content)
    except Exception as exc:  # noqa: BLE001
        raise TtsError("下载百炼音频失败:" + type(exc).__name__ + " " + str(exc)[:160]) from exc


SAPI_SCRIPT = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "try {{ $s.SelectVoice('{voice}') }} catch {{ }};"
    "$s.Rate = {rate};"
    "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
    "16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
    " [System.Speech.AudioFormat.AudioChannel]::Mono);"
    "$s.SetOutputToWaveFile('{target}', $fmt);"
    "$s.Speak('{text}');"
    "$s.Dispose()"
)


def sapi_wav(
    text: str,
    *,
    voice: str = DEFAULT_SAPI_VOICE,
    rate: int = -1,
    timeout_s: float = 60.0,
    runner: Any = None,
    tmp_dir: Path | str | None = None,
) -> bytes:
    """用 Windows 自带语音合成生成 WAV(离线、零依赖)。

    ⚠️ 文本要经 PowerShell **单引号字符串**传递:里面出现单引号会把脚本截断,
    所以先把单引号换成中文引号(听写词表里出现英文撇号是正常的,不能因此失败)。
    """
    safe_text = str(text).replace("'", "’").replace('"', "”").replace("\n", " ")
    work = Path(tmp_dir) if tmp_dir is not None else Path(config.TTS_CACHE_DIR)
    # ⚠️ 目标路径必须**每个进程唯一**:两个听写同时生成时,共用一个 _sapi_tmp.wav
    #    会互相覆盖(实测之外的坑,但代价极小,直接避开)。
    target = work / ("_sapi_" + hashlib.sha256(safe_text.encode("utf-8")).hexdigest()[:12] + ".wav")
    script = SAPI_SCRIPT.format(
        voice=str(voice or DEFAULT_SAPI_VOICE).replace("'", ""),
        rate=int(max(-10, min(10, rate))),
        text=safe_text,
        target=str(target).replace("'", ""),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass
    run = runner or (
        lambda command: subprocess.run(
            command, capture_output=True, timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    )
    try:
        completed = run(["powershell", "-NoProfile", "-Command", script])
    except Exception as exc:  # noqa: BLE001
        raise TtsError("调用本机语音合成失败:" + type(exc).__name__) from exc
    if getattr(completed, "returncode", 0) != 0:
        detail = (getattr(completed, "stderr", b"") or b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        raise TtsError("本机语音合成返回非零:" + str(detail)[:160])
    if not target.exists() or target.stat().st_size == 0:
        raise TtsError("本机语音合成没有产出音频(可能没装中文语音包)")
    payload = target.read_bytes()
    try:
        target.unlink()
    except OSError:
        pass
    return payload


# -- 引擎:缓存 + 主备切换 -----------------------------------------------------
def cache_key(text: str, *, provider: str, voice: str, model: str, rate: int) -> str:
    raw = "|".join([provider, model, voice, str(rate), str(text)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TtsEngine:
    """听写发声的入口。**主备自动切换 + 内容缓存**都在这里。"""

    def __init__(
        self,
        *,
        provider: str = "",
        voice: str = "",
        #: ⚠️ None = "去读设置",`""` = "**明确没有 key**"。
        #: 两者必须分开:测试里传空串是"假装没配 key",而 `or` 会把空串当成"没传",
        #: 于是它悄悄拿起了 .env 里那个真 key —— **测试真的发出了网络请求**
        #: (实测:配上 key 之后 `test_bailian_missing_key_falls_back_to_offline` 当场变红)。
        api_key: str | None = None,
        model: str = "",
        sapi_voice: str = "",
        rate: int | None = None,
        cache_dir: Path | str | None = None,
        timeout_s: float = 60.0,
        http: Any = None,
        runner: Any = None,
    ) -> None:
        settings = config.get_settings()
        self.provider = (provider or settings.tts_provider or PROVIDER_BAILIAN).strip().lower()
        if self.provider not in PROVIDERS:
            self.provider = PROVIDER_BAILIAN
        self.voice = voice or settings.tts_voice or DEFAULT_VOICE
        self.api_key = settings.dashscope_api_key if api_key is None else api_key
        self.model = model or settings.tts_model or DEFAULT_MODEL
        self.sapi_voice = sapi_voice or settings.tts_sapi_voice or DEFAULT_SAPI_VOICE
        self.rate = int(settings.tts_rate if rate is None else rate)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path(config.CACHE_DIR) / "tts"
        self.timeout_s = timeout_s
        self.http = http
        self.runner = runner

    # -- 单段 -------------------------------------------------------------
    def cache_path(self, text: str, *, provider: str) -> Path:
        key = cache_key(
            text, provider=provider, voice=self.voice if provider == PROVIDER_BAILIAN else self.sapi_voice,
            model=self.model, rate=self.rate,
        )
        return self.cache_dir / (key + ".wav")

    def speak(self, text: str, *, use_cache: bool = True) -> Clip:
        """生成一段音频。**先主力后离线**,成功为止;失败把原因写进 error。"""
        body = str(text or "").strip()
        if not body:
            return Clip(error="空文本")
        order = [self.provider] + [item for item in FALLBACK_ORDER if item != self.provider]
        reasons: list[str] = []
        for provider in order:
            if provider == PROVIDER_BAILIAN and not self.api_key:
                reasons.append("百炼:没有 DASHSCOPE_API_KEY")
                continue
            path = self.cache_path(body, provider=provider)
            if use_cache and path.exists() and path.stat().st_size > 0:
                payload = path.read_bytes()
                return Clip(
                    data=payload, provider=provider,
                    voice=self.voice if provider == PROVIDER_BAILIAN else self.sapi_voice,
                    seconds=wav_seconds(payload), cached=True, notes=list(reasons),
                )
            try:
                if provider == PROVIDER_BAILIAN:
                    payload = bailian_wav(
                        body, voice=self.voice, model=self.model, api_key=self.api_key,
                        timeout_s=self.timeout_s, http=self.http,
                    )
                    voice = self.voice
                else:
                    payload = sapi_wav(
                        body, voice=self.sapi_voice, rate=self.rate,
                        timeout_s=self.timeout_s, runner=self.runner,
                        tmp_dir=self.cache_dir,
                    )
                    voice = self.sapi_voice
                wav_params(payload)          # 校验:不是 WAV 就当失败,免得缓存一堆垃圾
            except TtsError as exc:
                reasons.append(provider + ":" + str(exc)[:120])
                continue
            except Exception as exc:  # noqa: BLE001
                reasons.append(provider + ":" + type(exc).__name__ + " " + str(exc)[:120])
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            except OSError:
                pass                          # 缓存写不进去不影响这次发声
            return Clip(
                data=payload, provider=provider, voice=voice, seconds=wav_seconds(payload),
                notes=list(reasons),
            )
        return Clip(error=";".join(reasons)[:300] or "没有可用的语音服务")

    # -- 听写 -------------------------------------------------------------
    def build_dictation(
        self,
        words: Sequence[str],
        *,
        title: str = "词语听写",
        repeat: int = 2,
        gap_s: float = 4.0,
        lead_s: float = 2.0,
        intro: str = "",
        outro: str = "",
        to_mp3: bool = True,
    ) -> DictationAudio:
        """词表 → 一段"读一遍 → 停 N 秒 → 再读一遍"的音频。

        ⚠️ 停顿是**本地拼静音**,不是求模型停 —— 节奏必须每次一样,而且可调。
        """
        items = [str(item).strip() for item in (words or []) if str(item).strip()]
        if not items:
            return DictationAudio(error="词表是空的")
        repeat = max(1, min(3, int(repeat)))
        gap_s = max(0.5, min(30.0, float(gap_s)))
        plan: list[tuple[str, float]] = []
        plan.append((intro or (title + "。请准备好纸笔。"), max(1.0, float(lead_s))))
        for index, word in enumerate(items, start=1):
            for round_index in range(repeat):
                # 第一遍带序号,后面几遍只读词 —— 与学校里的听写节奏一致
                spoken = ("第" + str(index) + "个," + word + "。") if round_index == 0 else (word + "。")
                plan.append((spoken, gap_s if not (index == len(items) and round_index == repeat - 1) else max(1.0, float(lead_s))))
        if outro:
            plan.append((outro, 0.0))
        return self._render_plan(plan, to_mp3=to_mp3)

    # -- 整段朗读(听力材料)------------------------------------------------
    def build_reading(
        self,
        text: str,
        *,
        repeat: int = 1,
        gap_s: float = 3.0,
        to_mp3: bool = True,
    ) -> DictationAudio:
        """一段话 → 音频(读 repeat 遍,遍与遍之间停 gap_s 秒)。

        ⭐ 与 build_dictation 的分工:那个是**词表**(逐个报序号、逐词留停顿),
        这个是**整段朗读**(听力材料/句子/课文)。⚠️ 两者都**不许**求模型"自己停顿" ——
        节奏一律本地拼静音,同一个输入每次得到一样的节奏。
        """
        body = str(text or "").strip()
        if not body:
            return DictationAudio(error="没有要念的内容")
        repeat = max(1, min(3, int(repeat)))
        gap = max(0.5, min(30.0, float(gap_s)))
        plan: list[tuple[str, float]] = [(body, gap)] * repeat
        return self._render_plan(plan, to_mp3=to_mp3)

    # -- 跟读(整句/整条,留白等孩子跟)---------------------------------------
    def build_follow(
        self,
        items: Sequence[str],
        *,
        gap_s: float = 2.5,
        lead_s: float = 1.0,
        repeat: int = 1,
        to_mp3: bool = True,
    ) -> DictationAudio:
        """一条一条念,念完留白等孩子跟读(不报序号,也不说"请准备好纸笔")。

        ⭐ 与 build_dictation 的分工:那个是**听写**(报一遍 → 停 → 再报一遍);
        这个是**跟读**(念一遍,留白让孩子跟着念)。词表跟读与整句跟读都走这一条。
        `repeat=2` 用在**合并清单**上:每条念两遍(先听、再跟),与单题的节奏一致。
        """
        bodies = [str(item).strip() for item in (items or []) if str(item).strip()]
        if not bodies:
            return DictationAudio(error="跟读内容是空的")
        gap = max(0.5, min(30.0, float(gap_s)))
        rounds = max(1, min(3, int(repeat)))
        plan: list[tuple[str, float]] = []
        for index, body in enumerate(bodies):
            last_item = index == len(bodies) - 1
            for round_index in range(rounds):
                if last_item and round_index == rounds - 1:
                    plan.append((body, max(1.0, float(lead_s))))
                else:
                    plan.append((body, gap))
        return self._render_plan(plan, to_mp3=to_mp3)

    def _render_plan(self, plan: Sequence[tuple[str, float]], *, to_mp3: bool) -> DictationAudio:
        notes: list[str] = []
        pieces: list[bytes] = []
        gaps: list[float] = []
        hits = 0
        characters = 0
        fresh = 0
        provider = ""
        voice = ""
        for text, gap in plan:
            clip = self.speak(text)
            if not clip.ok:
                return DictationAudio(error="发声失败:" + clip.error, notes=notes)
            if clip.provider not in ("", provider) and provider:
                notes.append("中途换了发声引擎(" + provider + " → " + clip.provider + ")")
            provider, voice = clip.provider, clip.voice
            for note in clip.notes:
                if note not in notes:
                    notes.append(note)
            hits += 1 if clip.cached else 0
            characters += len(text)
            if not clip.cached:
                fresh += len(text)
            pieces.append(clip.data)
            gaps.append(gap)
        try:
            merged = concat_wav(pieces, gaps)
        except TtsError as exc:
            return DictationAudio(error=str(exc), provider=provider, voice=voice)
        media_type = "audio/wav"
        payload = merged
        if to_mp3:
            payload, hint = wav_to_mp3(merged)
            if hint:
                notes.append(hint)
            else:
                media_type = "audio/mpeg"
        if provider == PROVIDER_SAPI and self.api_key:
            notes.append("这条是**本机离线音**:百炼那次没成功,已自动兜底")
        return DictationAudio(
            data=payload, media_type=media_type, seconds=wav_seconds(merged),
            provider=provider, voice=voice, characters=characters,
            fresh_characters=fresh, segments=len(pieces), cached_hits=hits, notes=notes,
        )


def parse_words(raw: str | Sequence[str]) -> list[str]:
    """把用户给的词表拆开:逗号/顿号/空格/换行都算分隔符,去重且保序。"""
    if isinstance(raw, str):
        text = raw.replace("，", ",").replace("、", ",").replace(";", ",").replace("；", ",")
        for separator in ("\n", "\r", "\t", " "):
            text = text.replace(separator, ",")
        items = text.split(",")
    else:
        items = [str(item) for item in raw]
    seen: list[str] = []
    for item in items:
        name = str(item).strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def render_dictation_text(spec: DictationAudio, words: Sequence[str]) -> str:
    """把音频元信息渲染成人能读的一段(命令行/回执用)。"""
    lines = [
        "听写音频:" + str(spec.segments) + " 段," + ("%.1f" % spec.seconds) + " 秒,"
        + str(round(len(spec.data) / 1024)) + " KB",
        "发声:" + spec.provider + " / " + spec.voice
        + "(这一次花 " + ("%.4f" % spec.spent_yuan) + " 元,缓存命中 "
        + str(spec.cached_hits) + "/" + str(spec.segments) + " 段)",
        "词表(" + str(len(words)) + "):" + "、".join(words),
    ]
    lines.extend("备注:" + item for item in spec.notes)
    return "\n".join(lines)


def engine_from_settings(**kwargs: Any) -> TtsEngine:
    return TtsEngine(**kwargs)


__all__ = [
    "Clip",
    "DictationAudio",
    "TtsEngine",
    "TtsError",
    "PROVIDER_BAILIAN",
    "PROVIDER_SAPI",
    "parse_words",
    "concat_wav",
    "silence",
    "wav_seconds",
    "wav_to_mp3",
]
