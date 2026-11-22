"""M14 验收测试:单文件 HTML 互动错题卷 + 听写发声。

覆盖四件事:
  ① figure:bbox 归一化/加边距/夹紧/退化框(实测踩过的"图形顶部被切掉"就是这里);
  ② tts:主备切换(百炼不通 → 本机离线)、内容缓存、WAV 拼接、词表解析;
  ③ sheet:模板渲染(**自包含**:不许有任何外链)、来源标注、冲突题不进"参考答案"区;
  ④ service:出卷落成产物,且**出卷这一步不需要模型**(整条链路里没有 client)。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_m14_sheet -v
"""

from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path

from la.domain.artifacts import KIND_HTML, ArtifactStore
from la.domain.media import MediaStore
from la.domain.receipts import ReceiptLog
from la.domain.services.study_service import StudyService
from la.domain.study import figure as fig
from la.domain.study import tts as tts_mod
from la.domain.study.sheet import (
    SheetModel,
    SheetProblem,
    data_uri,
    estimate_minutes,
    label_for,
    render_html,
    steps_from_raw,
)
from la.domain.study.tts import TtsEngine, concat_wav, parse_words, wav_seconds
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT


def make_wav(seconds: float = 0.2, rate: int = 16000) -> bytes:
    """造一段真 WAV(静音)。测试要的是格式正确,不是好听。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def make_png(width: int = 240, height: int = 160, box: tuple | None = None) -> bytes:
    """造一张真 PNG(白底 + 中间一个红方块),用来验证裁剪。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle(box or (80, 40, 160, 120), fill="red")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class FakeRunner:
    """假的本机 TTS:把一段真 WAV 写到脚本里的那个目标路径。

    ⚠️ 不解析 PowerShell 脚本就没法知道目标路径 —— 这里从命令串里正则抠出来,
    好处是**被测代码真的走了"执行命令 → 读文件"这条真实路径**。
    """

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[str] = []
        self.returncode = 0
        self.stderr = b""

    def __call__(self, command):  # noqa: ANN001
        script = " ".join(command)
        self.calls.append(script)
        marker = "SetOutputToWaveFile('"
        start = script.index(marker) + len(marker)
        target = Path(script[start:script.index("'", start)])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payload)
        return self


class FakeResponse:
    def __init__(self, payload: dict | None = None, content: bytes = b"") -> None:
        self._payload = payload or {}
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeHttp:
    """假的百炼 HTTP:POST 返回一个带 data(base64) 的响应,GET 返回音频字节。"""

    def __init__(self, audio: bytes, *, mode: str = "data") -> None:
        self.audio = audio
        self.mode = mode
        self.posts: list[dict] = []

    def post(self, url, headers=None, json=None):  # noqa: A002, ANN001
        self.posts.append({"url": url, "json": json})
        if self.mode == "data":
            encoded = base64.b64encode(self.audio).decode("ascii")
            return FakeResponse({"output": {"audio": {"data": encoded}}})
        return FakeResponse({"output": {"audio": {"url": "https://example.invalid/a.wav"}}})

    def get(self, url):  # noqa: ANN001
        return FakeResponse({}, content=self.audio)


class FigureTest(unittest.TestCase):
    def test_parse_bbox_accepts_both_scales(self) -> None:
        self.assertEqual(fig.parse_bbox([750, 360, 960, 760]), (0.75, 0.36, 0.96, 0.76))
        self.assertEqual(fig.parse_bbox([0.75, 0.36, 0.96, 0.76]), (0.75, 0.36, 0.96, 0.76))
        self.assertEqual(fig.parse_bbox({"x1": 1, "y1": 2, "x2": 3, "y2": 4}),
                         (0.001, 0.002, 0.003, 0.004))
        self.assertEqual(fig.parse_bbox("750,360,960,760"), (0.75, 0.36, 0.96, 0.76))

    def test_parse_bbox_swaps_reversed_and_rejects_degenerate(self) -> None:
        self.assertEqual(fig.parse_bbox([960, 760, 750, 360]), (0.75, 0.36, 0.96, 0.76))
        for bad in ([5, 5, 5, 5], [1], "abc", None, [1, 2, 3, "x"]):
            self.assertIsNone(fig.parse_bbox(bad), bad)

    def test_expand_and_clamp_never_leaves_the_image(self) -> None:
        grew = fig.expand_bbox([0.75, 0.36, 0.96, 0.76])
        self.assertLess(grew[0], 0.75)
        self.assertGreater(grew[2], 0.96)
        clamped = fig.clamp_bbox(fig.expand_bbox([0.0, 0.0, 0.1, 0.1]))
        self.assertGreaterEqual(clamped[0], 0.0)
        self.assertGreaterEqual(clamped[1], 0.0)
        # 贴在角上、又太小的框:夹完必须判 None(交给调用方退回原图)
        self.assertIsNone(fig.clamp_bbox([0.0, 0.0, 0.005, 0.005]))

    def test_crop_figure_returns_real_image(self) -> None:
        payload = make_png()
        result = fig.crop_figure(payload, [200, 100, 600, 700])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.media_type, "image/png")
        self.assertGreater(result.width, 100)
        self.assertEqual((result.source_width, result.source_height), (240, 160))
        # 裁剪结果必须真的是一张能打开的 PNG
        from PIL import Image

        with Image.open(io.BytesIO(result.data)) as image:
            self.assertEqual(image.size, (result.width, result.height))

    def test_blank_needs_both_low_ink_and_low_structure(self) -> None:
        """⭐ 真机踩过(2026-11-21 错题 #30):线描的几何图裁出来"暗像素只有 0.3%",
        旧判据只看暗像素,把它当白纸丢掉 —— 卷面上就成了
        "识别说这题有图形,但这次没取到"。现在**两个条件都满足**才算空白。

        实测那张真裁剪图:ink=0.0033(<0.012)、structure=0.097(≫0.004)。
        """
        from unittest import mock

        payload = make_png()
        with mock.patch.object(fig, "ink_ratio", return_value=0.0033), \
                mock.patch.object(fig, "structure_ratio", return_value=0.097):
            self.assertFalse(fig.looks_blank(payload))      # 细线图形:不许当空白
        with mock.patch.object(fig, "ink_ratio", return_value=0.0), \
                mock.patch.object(fig, "structure_ratio", return_value=0.0):
            self.assertTrue(fig.looks_blank(payload))       # 白纸:仍然是空白

    def test_crop_figure_degrades_without_raising(self) -> None:
        broken = fig.crop_figure(b"not an image", [100, 100, 900, 900])
        self.assertFalse(broken.ok)
        self.assertTrue(broken.error)
        empty = fig.crop_figure(b"", [100, 100, 900, 900])
        self.assertFalse(empty.ok)
        tiny = fig.crop_figure(make_png(), [500, 500, 501, 501])
        self.assertFalse(tiny.ok)


class FigureSvgTest(unittest.TestCase):
    """白名单渲染器:只画认得的那几种,画不出来就**明确说不画**(退回裁剪)。"""

    SAMPLES = {
        "unit_cubes_front": {"cols": 3, "top": [1]},
        "number_line": {"min": 0, "max": 10, "step": 2, "points": [{"value": 4, "label": "A"}]},
        "axes_linear": {"a": 2, "b": 1},
        "triangle": {"labels": ["A", "B", "C"], "right_angle_at": "B"},
        "rectangle": {"width_label": "5cm", "height_label": "3cm"},
        "circle": {"radius_label": "r=3cm", "show_centre": True},
        "bar_chart": {"categories": ["一班", "二班", "三班"], "values": [3, 5, 2], "unit": "人"},
    }

    def test_every_kind_renders_valid_selfcontained_svg(self) -> None:
        import xml.etree.ElementTree as ET

        from la.domain.study.figures_svg import render_spec, supported_kinds

        self.assertEqual(sorted(self.SAMPLES), supported_kinds())
        for kind, params in self.SAMPLES.items():
            svg, error = render_spec({"kind": kind, "params": params})
            self.assertTrue(svg, kind + " 没画出来:" + error)
            ET.fromstring(svg)                     # 必须是合法 XML(踩过:属性少引号)
            body = svg.replace("http://www.w3.org/2000/svg", "")
            for bad in ("http://", "https://", "<script", "xlink:href", "<image"):
                self.assertNotIn(bad, body, kind)

    def test_bad_specs_are_refused_not_faked(self) -> None:
        from la.domain.study.figures_svg import render_spec

        # ⚠️ 三角形不给参数**不算错**(默认标 A/B/C 就够了);参数自相矛盾才算错。
        #    但"小正方体组合体不给 cols/top"必须拒 —— 那不是补全,是替题目下结论。
        for spec in ({"kind": "unit_cubes_front", "params": {}},
                     {"kind": "unit_cubes_front", "params": {"cols": 3}},
                     {"kind": "bar_chart", "params": {"categories": ["一班"]}},
                     None, {}, {"kind": "不认识的图"},
                     {"kind": "number_line", "params": {"min": 5, "max": 1}},
                     {"kind": "unit_cubes_front", "params": {"cols": 0}},
                     {"kind": "bar_chart", "params": {"categories": ["a"], "values": [1, 2]}},
                     {"kind": "bar_chart", "params": {"categories": [], "values": []}}):
            svg, error = render_spec(spec)
            self.assertFalse(svg, spec)
            self.assertTrue(error, spec)

    def test_pick_spec_accepts_both_shapes(self) -> None:
        """模型可能把白名单名字填进它本来就有的 kind 字段 —— 两种写法都要认。"""
        from la.domain.study.figures_svg import pick_spec

        nested = pick_spec({"present": True, "kind": "组合体",
                            "render": {"kind": "unit_cubes_front", "params": {"cols": 3}}})
        self.assertEqual(nested, {"kind": "unit_cubes_front", "params": {"cols": 3}})
        flat = pick_spec({"present": True, "kind": "unit_cubes_front", "cols": 3, "top": [1]})
        self.assertEqual(flat["kind"], "unit_cubes_front")
        self.assertEqual(flat["params"], {"cols": 3, "top": [1]})
        self.assertIsNone(pick_spec(None))
        # 模型爱用自己的叫法:别名要能认出来(实测它报过 "front_view")
        from la.domain.study.figures_svg import whitelist_name

        self.assertEqual(whitelist_name("front_view"), "unit_cubes_front")
        self.assertEqual(whitelist_name("小正方体组合体"), "unit_cubes_front")
        self.assertEqual(whitelist_name("  Triangle "), "triangle")
        self.assertEqual(whitelist_name("不认识的图"), "")
        self.assertEqual(pick_spec({"present": True, "kind": "数轴",
                                    "min": 0, "max": 10})["kind"], "number_line")

    def test_labels_are_escaped(self) -> None:
        import xml.etree.ElementTree as ET

        from la.domain.study.figures_svg import render_spec

        svg, error = render_spec({"kind": "triangle", "params": {"labels": ["<A>", "B", "C"]}})
        self.assertTrue(svg, error)
        self.assertNotIn("<A>", svg)
        ET.fromstring(svg)


class TtsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.wav = make_wav(0.2)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_parse_words_splits_and_dedupes(self) -> None:
        self.assertEqual(parse_words("乌鸦、跳舞,清晨 乌鸦"),
                         ["乌鸦", "跳舞", "清晨"])
        self.assertEqual(parse_words(["a", " a ", "b"]), ["a", "b"])
        self.assertEqual(parse_words(""), [])

    def test_concat_wav_adds_silence(self) -> None:
        merged = concat_wav([self.wav, self.wav], [4.0])
        self.assertAlmostEqual(wav_seconds(merged), 0.2 + 4.0 + 0.2, places=2)
        with self.assertRaises(tts_mod.TtsError):
            concat_wav([self.wav, make_wav(0.2, rate=24000)], [0.0])

    def test_bailian_missing_key_falls_back_to_offline(self) -> None:
        # ⚠️ api_key="" 是"**明确没有 key**"(不是"去读设置")—— 否则本机 .env 里
        #    一旦配了真 key,这条测试就会真的发一次网络请求(实测踩过)。
        runner = FakeRunner(self.wav)
        engine = TtsEngine(provider="bailian", api_key="", cache_dir=self.tmp,
                           runner=runner, rate=0)
        clip = engine.speak("乌鸦")
        self.assertTrue(clip.ok, clip.error)
        self.assertEqual(clip.provider, "sapi")
        self.assertTrue(any("DASHSCOPE_API_KEY" in note for note in clip.notes))
        self.assertEqual(len(runner.calls), 1)          # 真的执行了一次本机合成

    def test_cache_hit_skips_synthesis(self) -> None:
        runner = FakeRunner(self.wav)
        engine = TtsEngine(provider="sapi", cache_dir=self.tmp, runner=runner, rate=0)
        first = engine.speak("跳舞")
        second = engine.speak("跳舞")
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(len(runner.calls), 1)          # 第二次没再调服务

    def test_bailian_is_used_when_key_present(self) -> None:
        http = FakeHttp(self.wav)
        engine = TtsEngine(provider="bailian", api_key="sk-test", cache_dir=self.tmp, http=http)
        clip = engine.speak("清晨")
        self.assertTrue(clip.ok, clip.error)
        self.assertEqual(clip.provider, "bailian")
        self.assertEqual(clip.voice, "Cherry")
        self.assertEqual(http.posts[0]["json"]["model"], "qwen3-tts-flash")
        self.assertEqual(http.posts[0]["json"]["input"]["voice"], "Cherry")

    def test_bailian_url_mode_downloads_audio(self) -> None:
        http = FakeHttp(self.wav, mode="url")
        engine = TtsEngine(provider="bailian", api_key="sk-test", cache_dir=self.tmp, http=http)
        clip = engine.speak("清晨")
        self.assertTrue(clip.ok, clip.error)
        self.assertEqual(clip.data, self.wav)

    def test_build_dictation_plan_and_mp3(self) -> None:
        runner = FakeRunner(make_wav(0.1))
        engine = TtsEngine(provider="sapi", cache_dir=self.tmp, runner=runner, rate=-1)
        audio = engine.build_dictation(["乌鸦", "跳舞"], repeat=2, gap_s=1.0, lead_s=1.0)
        self.assertTrue(audio.ok, audio.error)
        # 开场语 + 每词两遍 = 5 段
        self.assertEqual(audio.segments, 5)
        self.assertEqual(audio.characters, len("词语听写。请准备好纸笔。") + len("第1个,乌鸦。")
                         + len("乌鸦。") + len("第2个,跳舞。") + len("跳舞。"))
        self.assertGreater(audio.seconds, 2.0)
        self.assertIn(audio.media_type, ("audio/mpeg", "audio/wav"))
        self.assertEqual(audio.estimated_yuan, round(audio.characters / 10000 * 0.8, 6))

    def test_build_dictation_rejects_empty(self) -> None:
        engine = TtsEngine(provider="sapi", cache_dir=self.tmp, runner=FakeRunner(self.wav))
        self.assertFalse(engine.build_dictation([]).ok)


class SheetRenderTest(unittest.TestCase):
    def test_label_for_is_always_conservative(self) -> None:
        self.assertIn("未经家长确认", label_for("model", "unverified"))
        self.assertIn("老师批改", label_for("teacher", "agreed"))
        self.assertIn("可能是孩子的错答", label_for("legacy", ""))
        self.assertIn("暂无答案", label_for("", ""))

    def test_steps_from_json_or_text(self) -> None:
        self.assertEqual(steps_from_raw('["第一步", "第二步"]'), ["第一步", "第二步"])
        self.assertEqual(steps_from_raw("第一步\n- 第二步"), ["第一步", "第二步"])
        self.assertEqual(steps_from_raw(None), [])

    def _model(self) -> SheetModel:
        return SheetModel(
            who="小明", day="2026-10-13", minutes=estimate_minutes(2),
            generated_at="2026-10-13 08:00",
            items=[
                SheetProblem(
                    index=1, subject="数学", stem="26×7-(15-13)=?", kps=["四则混合运算"],
                    answer="180", steps=["26×7=182", "182-2=180"],
                    answer_source="model", answer_status="unverified",
                    answer_label=label_for("model", "unverified"),
                    student_answer="176",
                    figure_uri=data_uri(make_png(), "image/png"),
                    figure_note="图形:原题照片裁剪", unit_ids=[1, 2],
                ),
                SheetProblem(
                    index=2, subject="数学", stem="(3)有( )种不同的摆法。", kps=["观察物体"],
                    answer="8", answer_source="legacy", answer_status="conflict",
                    answer_label=label_for("legacy", "conflict"),
                    student_answer="7", conflict_note="三方不一致:孩子 7、老师订正 8、模型 4。",
                    unit_ids=[3],
                ),
            ],
            dictation_words=["乌鸦", "跳舞"], audio_uri=data_uri(b"ID3fake", "audio/mpeg"),
            audio_note="发声:sapi / Microsoft Huihui Desktop",
        )

    def test_render_is_self_contained(self) -> None:
        html = render_html(self._model())
        self.assertIn("data:image/png;base64,", html)
        self.assertIn("data:audio/mpeg;base64,", html)
        # ⭐ 自包含的判据:页面里不许出现任何外链
        for bad in ("http://", "https://", "//cdn", "<link ", "src=\"/"):
            self.assertNotIn(bad, html, bad)
        self.assertIn("模板 sheet-v2", html)

    def test_redrawn_figure_wins_and_crop_stays_as_evidence(self) -> None:
        """矢量图当主图、裁剪图留作对照 —— 而且两者都在页面里(自包含)。"""
        model = self._model()
        model.items[0].figure_svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            '<rect x="1" y="1" width="8" height="8" fill="#cfe3f2"/></svg>'
        )
        model.items[0].figure_note = "图形:按题目参数重绘"
        html = render_html(model)
        self.assertIn("图形:按题目参数重绘", html)
        self.assertIn("对照原图", html)
        self.assertIn("data:image/png;base64,", html)

    def test_conflict_problem_does_not_pose_as_standard_answer(self) -> None:
        html = render_html(self._model())
        self.assertIn("⚠ 答案待确认", html)
        self.assertIn("模型/照片上的答案(未确认)", html)
        self.assertIn("三方不一致", html)
        # 冲突题的答案不许出现在"参考答案"这个标签后面
        self.assertNotIn("参考答案:</b>8", html)
        self.assertIn("参考答案:</b>180", html)


class ListeningAudioTest(unittest.TestCase):
    """⭐ 用户报的 bug:「微信里生成的 HTML 试卷,听力题不会发音」。

    根因有两层,这里两层都盯着:
      ① 助手**没有生成语音的能力** → 现在有 tts_audio 工具,返回可直接粘进页面的 <audio>;
      ② html_page 的内联器**只认图片** → 现在 <audio src="/artifacts/<id>"> 也会被内联成 data:。
    """

    def setUp(self) -> None:
        from la.domain.artifacts import ArtifactStore
        from la.domain.tools import build_study_tools

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.media = MediaStore(self.conn, root=self.tmp / "media")
        self.artifacts = ArtifactStore(self.conn, root=self.tmp / "artifacts")
        self.service = StudyService(
            self.conn, media=self.media, artifacts=self.artifacts,
            now_provider=lambda: __import__("datetime").datetime(
                2026, 10, 13, 8, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Shanghai")
            ),
        )
        self.engine = TtsEngine(
            provider="sapi", cache_dir=self.tmp / "tts", runner=FakeRunner(make_wav(0.15)), rate=0,
        )
        self.specs = build_study_tools(self.service, tts_engine=self.engine)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _call(self, name: str, params: dict):
        import asyncio

        spec = next(item for item in self.specs if item.name == name)
        return asyncio.run(spec.handler(params))

    def test_tts_audio_registers_a_real_audio_artifact(self) -> None:
        result = self._call("tts_audio", {"text": "乌鸦、跳舞", "mode": "dictation",
                                          "repeat": 2, "gap": 2})
        self.assertTrue(result["handle"].startswith("/artifacts/"))
        self.assertIn("<audio", result["audio_html"])
        self.assertIn(result["handle"], result["audio_html"])
        artifact = self.artifacts.get(result["artifact_id"])
        # ⚠️ 这一条是踩出来的:以前 .mp3 不在 MEDIA_TYPES 里,产物会落成 octet-stream,
        #    内联器就认不出它是音频 —— 听力题于是**哑**的。
        self.assertTrue(str(artifact.media_type).startswith("audio/"), artifact.media_type)
        self.assertGreater(artifact.byte_size, 1000)

    def test_tts_audio_delivers_the_file_by_receipt(self) -> None:
        """⭐ 真机踩过(2026-10-15):录音做出来了,用户手机上一个文件都没收到。

        根因:桥接层只认 **artifact 域回执**里的 artifact_id 去发文件 —— 光登记产物、
        不写回执,文件永远到不了聊天窗口,而模型还会照着自己的想象说"发你了"。
        这条盯着三件事:回执写了、认领得上、工具如实回答"这次到底发不发得出去"。
        """
        from la.domain.receipts import DOMAIN_ARTIFACT, ReceiptLog

        self.service.receipts = ReceiptLog(self.conn)
        result = self._call("tts_audio", {"text": "Monday、Tuesday", "mode": "dictation"})
        self.assertTrue(result["delivered"], result)
        self.assertTrue(result["receipt_id"])
        artifact_id = str(result["artifact_id"])
        claimed = [
            item for item in self.service.receipts.recent(limit=20)
            if item.domain == DOMAIN_ARTIFACT
            and str((item.detail or {}).get("artifact_id") or "") == artifact_id
        ]
        self.assertTrue(claimed, "少了这条回执,微信那头就不会把录音当文件发出去")
        self.assertTrue(str((claimed[0].detail or {}).get("name") or ""))
        self.assertIn("已经作为文件发给用户", result["note"])

    def test_tts_audio_says_out_loud_when_it_cannot_deliver(self) -> None:
        """没有回执层时**不许**让模型以为"已经发过去了" —— 如实说只能在素材页拿。"""
        result = self._call("tts_audio", {"text": "Monday", "mode": "say"})
        self.assertFalse(result["delivered"])
        self.assertIn("不许", result["note"])
        self.assertIn("素材", result["note"])

    def test_generated_page_inlines_the_audio(self) -> None:
        from la.base.html_page import inline_local_images

        result = self._call("tts_audio", {"text": "听录音,选出你听到的单词。", "mode": "say"})
        page = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
                "<h2>听力</h2>" + result["audio_html"] + "</body></html>")

        def resolve(ref: str) -> tuple[bytes, str]:
            artifact = self.artifacts.get(ref.rsplit("/", 1)[-1])
            return self.artifacts.read_bytes(artifact), str(artifact.media_type)

        new_page, notes, warns = inline_local_images(page, resolve)
        self.assertTrue(any("音频" in note for note in notes), notes)
        self.assertIn("data:audio/", new_page)
        self.assertNotIn('src="/artifacts/', new_page)     # 已经换掉了,不是哑的
        self.assertEqual([warn for warn in warns if "音频" in warn], [])

    def test_oversized_audio_is_refused_loudly(self) -> None:
        """超预算的音频**不许静默**:页面里留原样,并把"这段会是哑的"说清楚。"""
        from la.base.html_page import inline_local_images

        result = self._call("tts_audio", {"text": "第一题", "mode": "say"})
        page = "<audio controls src='" + result["handle"] + "'></audio>"

        def resolve(ref: str) -> tuple[bytes, str]:
            artifact = self.artifacts.get(ref.rsplit("/", 1)[-1])
            return self.artifacts.read_bytes(artifact), str(artifact.media_type)

        new_page, _notes, warns = inline_local_images(page, resolve, max_audio_bytes=8)
        self.assertNotIn("data:audio/", new_page)
        self.assertTrue(any("哑" in warn for warn in warns), warns)


class ServiceSheetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.media = MediaStore(self.conn, root=self.tmp / "media")
        self.artifacts = ArtifactStore(self.conn, root=self.tmp / "artifacts")
        self.receipts = ReceiptLog(self.conn)
        self.service = StudyService(
            self.conn, media=self.media, artifacts=self.artifacts,
            receipts=self.receipts,
            now_provider=lambda: __import__("datetime").datetime(
                2026, 10, 13, 8, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Shanghai")
            ),
        )

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _seed_problem(self) -> int:
        asset = self.media.store_bytes(make_png(), media_type="image/png", subdir="problems")
        record = self.service.add_problem(
            __import__("la.domain.schemas.study", fromlist=["ProblemCreate"]).ProblemCreate(
                subject="数学", stem_text="26×7-(15-13)=?", knowledge_points=["四则混合运算"],
                asset_ids=[asset.id],
            ),
            actor="user",
        )
        self.conn.execute(
            "UPDATE app_problems SET answer_text = ?, student_answer = ?,"
            " answer_source = 'model', answer_status = 'unverified',"
            " solution_steps = ?, figure_asset_id = ? WHERE id = ?",
            ("180", "176", '["26×7=182", "182-2=180"]', asset.id, record.id),
        )
        self.conn.commit()
        return record.id

    def test_make_sheet_html_works_without_any_model(self) -> None:
        self._seed_problem()
        # ⚠️ 新建的复习单元首次到期是"明天"(FIRST_DUE_DAYS=1),所以第二天才出得了卷
        result = self.service.make_sheet_html(today="2026-10-14")
        self.assertEqual(result["items"], 1)
        html = result["html"]
        self.assertIn("参考答案:</b>180", html)
        self.assertIn("照片上孩子原来的作答:176", html)
        self.assertIn("data:image/png;base64,", html)
        artifact = self.artifacts.get(result["artifact"]["id"])
        self.assertEqual(artifact.kind, KIND_HTML)
        self.assertEqual(artifact.media_type, "text/html; charset=utf-8")
        self.assertEqual(artifact.meta.get("template_version"), "sheet-v2")
        # 金样本之外的硬约束:这份 HTML 必须是从盘上读回来也一致的单文件
        on_disk = self.artifacts.read_bytes(artifact).decode("utf-8")
        self.assertEqual(on_disk, html)

    def test_sheet_receipt_is_scoped_to_the_calling_session(self) -> None:
        """微信里出的卷子,回执必须挂在**这个成员**的会话下。

        2026-10-18 审计把桥接层改成按会话取回执后,这里写死 "study" 的回执就再也
        进不了 _outbound_files:卷子在盘上,用户手里没有,模型还说"发你了"。
        """
        self._seed_problem()
        result = self.service.make_sheet_html(today="2026-10-14", session_id="member:2")
        self.assertTrue(result["delivered"])
        artifact = self.artifacts.get(result["artifact"]["id"])
        self.assertEqual(artifact.session_id, "member:2")
        fresh = [item for item in self.receipts.since(0, session_id="member:2")
                 if item.domain == "artifact"]
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0].detail["artifact_id"], artifact.id)
        # 别的成员不能把这份回执认成"自己这一轮写的"
        other = [item for item in self.receipts.since(0, session_id="member:1")
                 if item.domain == "artifact"]
        self.assertEqual(other, [])

    def test_make_dictation_sheet_registers_audio_and_page(self) -> None:
        runner = FakeRunner(make_wav(0.1))
        result = self.service.make_dictation_sheet(
            ["乌鸦", "跳舞"], provider="sapi", runner=runner,
            cache_dir=self.tmp / "tts",
        )
        self.assertEqual(result["words"], ["乌鸦", "跳舞"])
        self.assertIn("data:audio/", result["html"])
        self.assertNotIn("参考答案", result["html"])     # 听写页不该印错题
        audio_artifact = self.artifacts.get(result["audio_artifact"]["id"])
        self.assertTrue(self.artifacts.exists(audio_artifact))
        self.assertGreater(audio_artifact.byte_size, 0)
        head = self.artifacts.read_bytes(audio_artifact)[:4]
        # 有 lameenc 时是 MP3(帧同步 0xFFFx),没有时退回 WAV(RIFF)—— 两种都算对
        self.assertTrue(head == b"RIFF" or head[:3] == b"ID3" or head[0] == 0xFF, head)

    def test_sheet_columns_survive_migration(self) -> None:
        """老库升上来:answer_source 被回填成 legacy,不假装可信。"""
        self._seed_problem()
        self.conn.execute(
            "UPDATE app_problems SET answer_source = NULL, answer_status = NULL"
            " WHERE id = 1"
        )
        self.conn.commit()
        row = self.conn.execute("SELECT answer_source FROM app_problems WHERE id = 1").fetchone()
        self.assertIsNone(row["answer_source"])          # 迁移只在升级时回填,不会反复改
        result = self.service.make_sheet_html(today="2026-10-14")
        self.assertIn("暂无答案", result["html"])         # 没有来源 = 不许冒充标准答案


if __name__ == "__main__":
    unittest.main()
