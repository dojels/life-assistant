"""M14d 验收测试:听力/听写音频**在录入时生成一次、绑定在题上**。

用户 2026-10-13 的原话:"在录入的时候,如果有声音和图形,就应该直接生成绑定。
因为错题是根据记忆多次给孩子学的,每次都重新生成的话,一是不稳定,二是浪费 token。"

这里守住两条不变量:

  ① **绑定一次、反复用**:同一道题第二次解析**不再合成**;出卷时一次语音调用都没有
     (只从产物层读字节)。判据用"引擎入口被调了几次",**不是**"跑了几次脚本" ——
     内容缓存会把重复调用掩盖掉,拿脚本调用数当证据会得到一条永远绿灯的假测试。
  ② **不许静默**:没配上语音、没有产物层、照片上没拿到要念的原文 —— 每一种都要在
     卷面上说出来(🔇 + 原因),而不是悄悄变成一道不会响的听力题。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from zoneinfo import ZoneInfo

from la.domain.artifacts import ArtifactStore
from la.domain.media import MediaStore
from la.domain.services.study_service import StudyService
from la.domain.study import audio as audio_mod
from la.domain.study.audio import (
    KIND_DICTATION,
    KIND_FOLLOW,
    KIND_LISTENING,
    describe_spec,
    plan_audio,
    plan_from_problem,
    spec_of,
)
from la.domain.study.intake import IntakeService, render_ingest_result
from la.domain.study.tts import TtsEngine
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT

TZ = ZoneInfo("Asia/Shanghai")
NOW = __import__("datetime").datetime(2026, 10, 13, 8, 0, tzinfo=TZ)


def make_wav(seconds: float = 0.2, rate: int = 16000) -> bytes:
    """造一段真 WAV(静音)。测试要的是格式正确,不是好听。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def make_png(width: int = 240, height: int = 160) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class FakeRunner:
    """假的本机 TTS:把一段真 WAV 写到脚本里的目标路径(与 tts.py 的真实调用路径一致)。"""

    def __init__(self, payload: bytes = b"", *, returncode: int = 0) -> None:
        self.payload = payload
        self.calls: list[str] = []
        self.returncode = returncode
        self.stderr = b"boom" if returncode else b""

    def __call__(self, command):  # noqa: ANN001
        script = " ".join(command)
        self.calls.append(script)
        if self.returncode:
            return self
        marker = "SetOutputToWaveFile('"
        start = script.index(marker) + len(marker)
        target = Path(script[start:script.index("'", start)])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payload)
        return self


class RecordingEngine:
    """记账用的引擎包装。

    ⚠️ 数的是**引擎入口被调了几次**,不是"合成脚本跑了几次":TtsEngine 自带按内容哈希
    的缓存,第二次同样的文字根本不会落到脚本上 —— 拿脚本调用数当"没有重复合成"的证据,
    这条测试会永远是绿灯。
    """

    def __init__(self, inner: TtsEngine) -> None:
        self.inner = inner
        self.readings = 0
        self.dictations = 0
        self.follows = 0

    @property
    def renders(self) -> int:
        return self.readings + self.dictations + self.follows

    def speak(self, text: str, **kwargs):  # noqa: ANN003
        return self.inner.speak(text, **kwargs)

    def build_dictation(self, words, **kwargs):  # noqa: ANN001, ANN003
        self.dictations += 1
        return self.inner.build_dictation(words, **kwargs)

    def build_reading(self, text: str, **kwargs):  # noqa: ANN003
        self.readings += 1
        return self.inner.build_reading(text, **kwargs)

    def build_follow(self, items, **kwargs):  # noqa: ANN001, ANN003
        self.follows += 1
        return self.inner.build_follow(items, **kwargs)


class StubEngine:
    """只记"调了哪个合成入口、参数是什么",不生音频(判节奏用)。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, dict]] = []

    def build_reading(self, text, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(("reading", text, dict(kwargs)))
        return None

    def build_follow(self, items, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(("follow", list(items), dict(kwargs)))
        return None

    def build_dictation(self, words, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(("dictation", list(words), dict(kwargs)))
        return None


class ScriptedClient:
    """按顺序把准备好的回复发出去,并记下每一次请求。"""

    model = "stub"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        from la.llm.client import ChatResult

        self.calls.append(list(messages))
        reply = self.replies.pop(0) if self.replies else "{}"
        return ChatResult(text=reply, model=self.model)


def probe_reply(*, audio: dict | None = None, figure: dict | None = None) -> str:
    return json.dumps({
        "is_problem": True,
        "student_answer": "life",
        "teacher_answer": "",
        "figure": figure or {"present": False, "kind": "", "description": "",
                             "bbox": [0, 0, 0, 0], "render": None},
        "audio": audio if audio is not None else {"needs": False, "kind": "none",
                                                  "text": "", "note": ""},
        "rotation_degrees": 0,
    }, ensure_ascii=False)


SOLVE_REPLY = json.dumps({
    "answer": "life", "steps": ["life 是「生活」"], "answer_kind": "reference",
    "missing_info": [], "confidence": 0.6,
}, ensure_ascii=False)

DICTATION_AUDIO = {"needs": True, "kind": "dictation",
                   "text": "life、slow、control", "note": "单词表印在题目里"}


class PlanTest(unittest.TestCase):
    """纯函数:什么算"要出声"、念什么、白名单怎么收敛。"""

    def test_whitelist_refuses_to_guess(self) -> None:
        self.assertEqual(audio_mod.whitelist_name("听力"), KIND_LISTENING)
        self.assertEqual(audio_mod.whitelist_name("LISTENING"), KIND_LISTENING)
        self.assertEqual(audio_mod.whitelist_name("听写"), KIND_DICTATION)
        self.assertEqual(audio_mod.whitelist_name("单词"), KIND_DICTATION)
        # ⭐ 认不出来就当"没有声音" —— 不许随便挑一种念法(念错了比没有更糟)
        self.assertEqual(audio_mod.whitelist_name("随便念点什么"), "")
        self.assertIsNone(plan_audio("随便念点什么", "abc"))

    def test_dictation_plan_uses_the_same_rhythm_every_time(self) -> None:
        plan = plan_audio("dictation", "life、 slow,control", subject="英语")
        self.assertTrue(plan is not None and plan.ok)
        self.assertEqual(plan.words, ["life", "slow", "control"])
        self.assertEqual(plan.repeat, audio_mod.DICTATION_REPEAT)
        self.assertEqual(plan.gap_s, audio_mod.DICTATION_GAP_S)
        self.assertEqual(plan.text, "life、slow、control")   # 存库的口径:顿号分隔
        self.assertEqual(
            plan_audio("listening", "Hello.\n\nNice  to meet you.").text,
            "Hello.\nNice to meet you.",
        )

    def test_refuses_instead_of_half_doing_it(self) -> None:
        self.assertIn("没拿到要念的原文", plan_audio("listening", "").error)
        # ⚠️ 词表是**去重**的:40 个一样的词只会剩一个,所以这里要 40 个不同的词
        self.assertIn("太多", plan_audio("dictation", "、".join("词" + str(n) for n in range(40))).error)
        # ⚠️ 超限**不截断**:截一半的听力材料是错的 —— 宁可整段不做,也不做半段
        too_long = plan_audio("listening", "字" * 500)
        self.assertIn("太长", too_long.error)
        self.assertFalse(too_long.ok)
        self.assertEqual(len(too_long.text), 500)

    def test_spec_round_trip_carries_the_recipe(self) -> None:
        plan = plan_audio("dictation", "life,slow")
        payload = audio_mod.spec_payload(plan, artifact_id="f123")
        back = spec_of(audio_mod.spec_json(payload))
        self.assertEqual(back["kind"], KIND_DICTATION)
        self.assertEqual(back["text"], "life、slow")
        self.assertEqual(back["artifact_id"], "f123")
        self.assertEqual(back["repeat"], 2)
        self.assertEqual(spec_of("这不是 JSON"), {})
        self.assertIn("听写音频", describe_spec(back))


class PlanFromProblemTest(unittest.TestCase):
    """⭐ 没有照片也要能绑:听写题的材料**本来就写在题干里**。

    实测(2026-10-13):库里的 #6「英语听写,没听出来的单词:life、slow、control」是家长
    **直接把词打进来**的 —— 0 张照片。这时候视觉那趟无从问起,而"要念什么"题干自己会说:
    为它调一次模型,是拿 token 换一条纯字符串解析(与 local_arithmetic 同一条取向)。
    """

    def test_dictation_words_are_read_from_the_problem(self) -> None:
        plan = plan_from_problem(stem="英语听写,没听出来的单词:life、slow、control",
                                 answer="life、slow、control", subject="英语")
        self.assertTrue(plan is not None and plan.ok)
        self.assertEqual(plan.kind, KIND_DICTATION)
        self.assertEqual(plan.words, ["life", "slow", "control"])
        # 题干里没写词表、只有答案时也要能读出来
        plan = plan_from_problem(stem="听写词语:一亩地(孩子听写时没听出来)", answer="一亩地")
        self.assertEqual(plan.words, ["一亩地"])

    def test_plain_homework_is_left_alone(self) -> None:
        """⚠️ 普通笔答题**一个字的音频都不做** —— 不许给每道题配朗读。"""
        self.assertIsNone(plan_from_problem(stem="26×7÷(15-13)=?", answer="91"))
        self.assertIsNone(plan_from_problem(
            stem="估 75.9+14.6×10+32.5×2<300(元);并写出你是怎么估的", answer="292 元<300 元"))

    def test_instruction_is_never_read_aloud_as_content(self) -> None:
        """⚠️ 实测的坑:**指令本身**被当成要念的内容("默写古诗《静夜思》"读一遍)。"""
        plan = plan_from_problem(stem="听录音,选出你听到的单词", answer="")
        self.assertFalse(plan.ok)
        self.assertIn("没有可念", plan.error)
        plan = plan_from_problem(stem="默写古诗《静夜思》", answer="")
        self.assertFalse(plan.ok)
        self.assertIn("没写要听写的词表", plan.error)

    def test_whole_material_goes_down_the_reading_path(self) -> None:
        plan = plan_from_problem(stem="听短文,回答问题:Tom has a cat. It is white.", answer="")
        self.assertTrue(plan.ok)
        self.assertEqual(plan.kind, KIND_LISTENING)
        self.assertIn("Tom has a cat.", plan.text)
        # 默写的古诗:答案是句子 → 整段朗读(而不是硬拆成"词表")
        plan = plan_from_problem(stem="默写古诗《静夜思》",
                                 answer="床前明月光,疑是地上霜。举头望明月,低头思故乡。")
        self.assertEqual(plan.kind, KIND_LISTENING)
        self.assertIn("低头思故乡", plan.text)

    def test_follow_along_words_are_read_from_the_stem(self) -> None:
        """⭐ 家长 2026-10-15 报的 bug:"这5个单词录成**跟读**的错题"→ 录完一声不出。

        题面是「跟读单词(星期一到星期五):Monday /ˈmʌndeɪ/、Tuesday …」——
        原来的出声线索里没有"跟读",题干预备队于是**看不出这题要出声**;
        而音标标注又会让每个词条长过"一个词",就算认出来了也拆不出词表。
        这里两条一起守住:认出跟读 → 剥掉音标 → 五个英文单词进词表。
        """
        stem = ("跟读单词(星期一到星期五):Monday /ˈmʌndeɪ/、Tuesday /ˈtjuːzdeɪ/、"
                "Wednesday /ˈwenzdeɪ/、Thursday /ˈθɜːzdeɪ/、Friday /ˈfraɪdeɪ/")
        plan = plan_from_problem(stem=stem, subject="英语")
        self.assertTrue(plan is not None and plan.ok, plan)
        # ⭐ 2026-11-09 起,跟读是**自己的**一种声音(不再是听写节奏):
        #    词表按条念一遍、留白让孩子跟,不报"第几个",也不说"请准备好纸笔"。
        self.assertEqual(plan.kind, KIND_FOLLOW)
        self.assertEqual(plan.items,
                         ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
        self.assertEqual(plan.repeat, 1)
        self.assertEqual(plan.label, "跟读音频")
        # ⚠️ 音标**不许**进音频:交给语音引擎念 /ˈmʌndeɪ/ 只会出乱码
        for mark in ("/", "ˈ", "mʌn"):
            self.assertNotIn(mark, plan.text)
        self.assertTrue(any("题干" in note for note in plan.notes), plan.notes)

    def test_the_meaning_gloss_is_not_read_aloud(self) -> None:
        """⚠️ 库里 #9 的答案抄成了 "Monday 星期一 /ˈmʌndeɪ/;Tuesday 星期二 …"。

        照单全念,孩子听到的是「星期一」「d 不发音」这些**说明文字** ——
        跟读的应该是那五个英文单词。题干里写着同语种的词表,优先用它。
        """
        stem = ("跟读单词(星期一到星期五):Monday /ˈmʌndeɪ/、Tuesday /ˈtjuːzdeɪ/、"
                "Wednesday /ˈwenzdeɪ/、Thursday /ˈθɜːzdeɪ/、Friday /ˈfraɪdeɪ/")
        answer = ("Monday 星期一 /ˈmʌndeɪ/;Tuesday 星期二 /ˈtjuːzdeɪ/;"
                  "Wednesday 星期三 /ˈwenzdeɪ/(d 不发音);Thursday 星期四 /ˈθɜːzdeɪ/(th 咬舌尖);"
                  "Friday 星期五 /ˈfraɪdeɪ/")
        plan = plan_from_problem(stem=stem, answer=answer, subject="英语")
        self.assertTrue(plan is not None and plan.ok, plan)
        self.assertEqual(plan.items, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
        for junk in ("星期一", "不发音", "咬舌尖"):
            self.assertNotIn(junk, plan.text)

    def test_follow_along_without_phonetics_also_works(self) -> None:
        plan = plan_from_problem(stem="跟读单词:Monday、Tuesday、Wednesday", subject="英语")
        self.assertTrue(plan is not None and plan.ok, plan)
        self.assertEqual(plan.items, ["Monday", "Tuesday", "Wednesday"])

    def test_a_phrase_is_read_as_a_phrase_not_as_words(self) -> None:
        """⭐ 2026-11-09 真机:「今天录的 take care of,我要录的是短语跟读,智能体只能录成听写,
        并且把三个词分开读,这不对。」

        下面三条就是今天录进库里的原样题干 —— 每一条都必须**整句连读**,
        不许出现「take、care、of」(那是听写,短语就毁了)。
        """
        cases = (
            ("英语跟读：take care of（照顾、照料）", "take care of"),
            ("英语跟读:take care of(三个词连读,take care of)", "take care of"),
            ("英语跟读：请连起来读整个短语「take care of」，不要拆成单个单词读。", "take care of"),
        )
        for stem, answer in cases:
            with self.subTest(stem=stem):
                plan = plan_from_problem(stem=stem, answer=answer, subject="英语")
                self.assertTrue(plan is not None and plan.ok, plan)
                self.assertEqual(plan.kind, KIND_FOLLOW)
                self.assertEqual(plan.text, "take care of")
                self.assertEqual(plan.items, [])
                self.assertNotIn("take、", plan.text)
                self.assertEqual(plan.label, "跟读音频")

    def test_a_slash_is_a_pause_not_the_word_slash(self) -> None:
        """"What's he like? / What does he like?" —— 斜杠是写法,念出来不该是"slash"。"""
        plan = plan_from_problem(
            stem="英语跟读：What's he like? / What does he like?", subject="英语",
        )
        self.assertTrue(plan is not None and plan.ok, plan)
        self.assertEqual(plan.kind, KIND_FOLLOW)
        self.assertNotIn("/", plan.text)
        self.assertIn("，", plan.text)

    def test_follow_phrase_renders_as_one_utterance_twice(self) -> None:
        """短语:整句念两遍(中间留白让他跟),**只调一次**合成。"""
        plan = plan_from_problem(stem="英语跟读：take care of", subject="英语")
        engine = StubEngine()
        audio_mod.render_plan(engine, plan)
        self.assertEqual(len(engine.calls), 1, engine.calls)
        kind, body, kwargs = engine.calls[0]
        self.assertEqual(kind, "reading")
        self.assertEqual(body, "take care of")
        self.assertEqual(kwargs["repeat"], 2)

    def test_follow_word_list_renders_item_by_item(self) -> None:
        """词表:一条念一遍(不是听写那种报两遍、报序号)。"""
        plan = plan_from_problem(stem="跟读单词:Monday、Tuesday", subject="英语")
        engine = StubEngine()
        audio_mod.render_plan(engine, plan)
        self.assertEqual(len(engine.calls), 1, engine.calls)
        kind, items, kwargs = engine.calls[0]
        self.assertEqual(kind, "follow")
        self.assertEqual(items, ["Monday", "Tuesday"])
        self.assertEqual(kwargs["gap_s"], 2.5)

    def test_reading_a_passage_goes_down_the_reading_path(self) -> None:
        plan = plan_from_problem(stem="朗读下面这段话:秋天到了,树上的叶子黄了。", subject="语文")
        self.assertTrue(plan is not None and plan.ok, plan)
        self.assertEqual(plan.kind, KIND_LISTENING)
        self.assertIn("叶子黄了", plan.text)

    def test_a_location_without_content_is_not_read_as_a_word_list(self) -> None:
        """⚠️ "跟读课文:P18 第3段"里没有内容 —— 念出来是一串编号,不如不念。"""
        plan = plan_from_problem(stem="跟读课文:P18 第3段", subject="语文")
        self.assertFalse(plan.ok)
        self.assertIn("没有可念", plan.error)
        plan = plan_from_problem(stem="跟读课文:第三单元", subject="语文")
        self.assertFalse(plan.ok)
        self.assertIn("没有可念", plan.error)

    def test_english_sentence_is_not_split_into_words(self) -> None:
        """⚠️ 另一个实测的坑:英文句子的句号没被算作标点,于是整句被拆成了"词表"。"""
        plan = plan_from_problem(stem="听录音,选出你听到的单词",
                                 answer="He is playing football. She likes apples.")
        self.assertEqual(plan.kind, KIND_LISTENING)


class BindTest(unittest.TestCase):
    """录入那条路:音频**做一次就绑上**,之后沿用。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.media = MediaStore(self.conn, root=self.tmp / "media")
        self.artifacts = ArtifactStore(self.conn, root=self.tmp / "artifacts")
        self.study = StudyService(
            self.conn, media=self.media, artifacts=self.artifacts, now_provider=lambda: NOW,
        )
        self.runner = FakeRunner(make_wav(0.3))
        self.engine = RecordingEngine(TtsEngine(
            provider="sapi", api_key="", cache_dir=self.tmp / "tts", runner=self.runner, rate=0,
        ))
        self.client = ScriptedClient([])
        self.intake = IntakeService(
            self.conn, study=self.study, media=self.media,
            client_factory=lambda: self.client, now_provider=lambda: NOW,
            tts_engine=self.engine,
        )

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._tmp.cleanup()

    def _seed(self) -> int:
        from la.domain.schemas.study import ProblemCreate

        asset = self.media.store_bytes(make_png(), media_type="image/png", subdir="problems")
        record = self.study.add_problem(
            ProblemCreate(subject="英语", stem_text="听写单词。", knowledge_points=["单词拼写"],
                          asset_ids=[asset.id]),
            actor="user",
        )
        return record.id

    def _row(self, problem_id: int):
        return self.conn.execute(
            "SELECT * FROM app_problems WHERE id = ?", (problem_id,)
        ).fetchone()

    def test_audio_is_made_once_then_reused(self) -> None:
        problem_id = self._seed()
        self.client.replies = [probe_reply(audio=DICTATION_AUDIO), SOLVE_REPLY]
        first = self.intake.resolve_problem(problem_id)
        self.assertFalse(first.get("error"), first)
        self.assertTrue(first["audio_artifact_id"])
        self.assertEqual(self.engine.renders, 1)
        row = self._row(problem_id)
        spec = spec_of(row["audio_spec"])
        self.assertEqual(spec["kind"], KIND_DICTATION)
        self.assertEqual(spec["text"], "life、slow、control")
        artifact = self.artifacts.get(str(row["audio_artifact_id"]))
        self.assertTrue(str(artifact.media_type).startswith("audio/"), artifact.media_type)
        self.assertGreater(artifact.byte_size, 1000)
        self.assertIn("已生成并绑定", first["audio_note"])

        # ⭐ 第二次解析(重跑)同样的题:**一次都不重新合成**,沿用录入时那一段
        spoken = len(self.runner.calls)      # 一次听写要念 7 段(1 句开场 + 3 个词 × 2 遍)
        self.assertGreater(spoken, 1)
        self.client.replies = [probe_reply(audio=DICTATION_AUDIO), SOLVE_REPLY]
        again = self.intake.resolve_problem(problem_id, force=True)
        self.assertEqual(self.engine.renders, 1, "重跑不该再合成一遍")
        self.assertEqual(len(self.runner.calls), spoken, "重跑连一次发声都不该有")
        self.assertIn("沿用", again["audio_note"])
        self.assertEqual(self._row(problem_id)["audio_artifact_id"],
                         row["audio_artifact_id"])

    def test_changed_text_is_rebuilt(self) -> None:
        """绑定的是**内容**:原文改了就得重做 —— 否则孩子听到的还是旧的那段。"""
        problem_id = self._seed()
        self.client.replies = [probe_reply(audio=DICTATION_AUDIO), SOLVE_REPLY]
        self.intake.resolve_problem(problem_id)
        self.client.replies = [
            probe_reply(audio={"needs": True, "kind": "dictation", "text": "life,slow"}),
            SOLVE_REPLY,
        ]
        self.intake.resolve_problem(problem_id, force=True)
        self.assertEqual(self.engine.renders, 2)
        self.assertEqual(spec_of(self._row(problem_id)["audio_spec"])["text"], "life、slow")

    def test_missing_text_is_asked_once_then_told_out_loud(self) -> None:
        """照片上没有要念的原文 → 追问一次;还是拿不到就**如实说这题会是哑的**。"""
        problem_id = self._seed()
        self.client.replies = [
            probe_reply(audio={"needs": True, "kind": "listening", "text": "",
                               "note": "题干只说「听录音」"}),
            '{"text": null, "note": "照片上没有录音原文"}',
            SOLVE_REPLY,
        ]
        result = self.intake.resolve_problem(problem_id)
        self.assertFalse(result["audio_artifact_id"])
        self.assertIn("没拿到要念的原文", result["audio_error"])
        self.assertEqual(self.engine.renders, 0)      # 一个字都没念
        self.assertIn("没拿到要念的原文", self._row(problem_id)["audio_spec"])

    def test_follow_up_text_is_used_when_the_photo_has_it(self) -> None:
        problem_id = self._seed()
        self.client.replies = [
            probe_reply(audio={"needs": True, "kind": "listening", "text": ""}),
            '{"text": "Hello. Nice to meet you.", "note": "印在题上"}',
            SOLVE_REPLY,
        ]
        result = self.intake.resolve_problem(problem_id)
        self.assertTrue(result["audio_artifact_id"])
        self.assertEqual(spec_of(self._row(problem_id)["audio_spec"])["kind"], KIND_LISTENING)
        self.assertEqual(self.engine.readings, 1)

    def test_dictation_without_photo_binds_from_the_stem(self) -> None:
        """⭐ 家长直接把词打进来的听写题(0 张照片):不调模型也把音频绑上。

        实测场景:库里的 #6 就是这么录进来的 —— 先前的实现"没有照片 → 什么都不做",
        声音素材于是永远缺一块。
        """
        from la.domain.schemas.study import ProblemCreate

        record = self.study.add_problem(
            ProblemCreate(subject="英语", stem_text="英语听写,没听出来的单词:life、slow、control",
                          knowledge_points=["单词拼写"]),
            actor="user",
        )
        offline = IntakeService(          # ⚠️ 故意**没有**模型客户端
            self.conn, study=self.study, media=self.media, client_factory=None,
            now_provider=lambda: NOW, tts_engine=self.engine,
        )
        result = offline.resolve_problem(record.id, force=True)
        self.assertIn("离线模式", str(result.get("error")))
        self.assertTrue(result["audio_artifact_id"], result)
        spec = spec_of(self._row(record.id)["audio_spec"])
        self.assertEqual(spec["kind"], KIND_DICTATION)
        self.assertEqual(spec["text"], "life、slow、control")
        self.assertEqual(self.engine.dictations, 1)

    def test_follow_along_problem_gets_its_audio_bound(self) -> None:
        """⭐ 真机那条的**端到端**复现:跟读题(0 张照片)必须绑上音频、写进 audio_spec。

        (2026-10-15:错题 #9「跟读单词(星期一到星期五)…」录进来后 audio_artifact_id 是空的,
        孩子复习时那道题一声不出。)
        """
        from la.domain.schemas.study import ProblemCreate

        stem = ("跟读单词(星期一到星期五):Monday /ˈmʌndeɪ/、Tuesday /ˈtjuːzdeɪ/、"
                "Wednesday /ˈwenzdeɪ/、Thursday /ˈθɜːzdeɪ/、Friday /ˈfraɪdeɪ/")
        record = self.study.add_problem(
            ProblemCreate(subject="英语", stem_text=stem, knowledge_points=["星期单词跟读"]),
            actor="user",
        )
        offline = IntakeService(          # 同样不靠模型:纯字符串解析
            self.conn, study=self.study, media=self.media, client_factory=None,
            now_provider=lambda: NOW, tts_engine=self.engine,
        )
        result = offline.resolve_problem(record.id, force=True)
        self.assertTrue(result["audio_artifact_id"], result)
        spec = spec_of(self._row(record.id)["audio_spec"])
        self.assertEqual(spec["kind"], KIND_FOLLOW)
        self.assertEqual(spec["label"], "跟读音频")
        self.assertEqual(spec["items"], ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
        self.assertIn("已生成并绑定", result["audio_note"])

    def test_photo_probe_saying_nothing_still_uses_the_stem(self) -> None:
        """照片上没读到原文(或模型说不用出声)时,**题干预备队**要接上。"""
        from la.domain.schemas.study import ProblemCreate

        asset = self.media.store_bytes(make_png(), media_type="image/png", subdir="problems")
        record = self.study.add_problem(
            ProblemCreate(subject="语文", stem_text="听写词语:一亩地(孩子听写时没听出来)",
                          knowledge_points=["字词"], asset_ids=[asset.id]),
            actor="user",
        )
        self.client.replies = [probe_reply(), SOLVE_REPLY]      # 探针说"不需要出声"
        result = self.intake.resolve_problem(record.id)
        self.assertTrue(result["audio_artifact_id"], result)
        self.assertEqual(spec_of(self._row(record.id)["audio_spec"])["text"], "一亩地")
        self.assertTrue(any("题干" in note for note in result["notes"]), result["notes"])

    def test_voice_failure_is_stated_not_swallowed(self) -> None:
        problem_id = self._seed()
        self.engine = RecordingEngine(TtsEngine(
            provider="sapi", api_key="", cache_dir=self.tmp / "tts2",
            runner=FakeRunner(returncode=1), rate=0,
        ))
        self.intake = IntakeService(
            self.conn, study=self.study, media=self.media,
            client_factory=lambda: self.client, now_provider=lambda: NOW,
            tts_engine=self.engine,
        )
        self.client.replies = [probe_reply(audio=DICTATION_AUDIO), SOLVE_REPLY]
        result = self.intake.resolve_problem(problem_id)
        self.assertFalse(result["audio_artifact_id"])
        self.assertIn("哑的", result["audio_note"])
        self.assertTrue(result["audio_error"])

    def test_no_artifact_store_is_said_out_loud(self) -> None:
        bare = StudyService(self.conn, media=self.media, now_provider=lambda: NOW)
        problem_id = self._seed()
        intake = IntakeService(
            self.conn, study=bare, media=self.media,
            client_factory=lambda: self.client, now_provider=lambda: NOW,
            tts_engine=self.engine,
        )
        self.client.replies = [probe_reply(audio=DICTATION_AUDIO), SOLVE_REPLY]
        result = intake.resolve_problem(problem_id)
        self.assertIn("没接产物层", result["audio_note"])
        self.assertFalse(result["audio_artifact_id"])

    def test_silent_problem_binds_nothing(self) -> None:
        """普通笔答题:一个字的音频都不做(不许给每道题配朗读)。"""
        from la.domain.schemas.study import ProblemCreate

        # ⚠️ 刻意用一道**普通数学题**做种:听写类的题就算题干没写词表,也该留下一条
        #    "为什么没有声音"的说明(见 test_silent_problem_binds_nothing 的下一条)。
        asset = self.media.store_bytes(make_png(), media_type="image/png", subdir="problems")
        record = self.study.add_problem(
            ProblemCreate(subject="数学", stem_text="26×7÷(15-13)=?", knowledge_points=["四则混合运算"],
                          asset_ids=[asset.id]),
            actor="user",
        )
        problem_id = record.id
        self.client.replies = [probe_reply(), SOLVE_REPLY]
        result = self.intake.resolve_problem(problem_id)
        self.assertFalse(result["audio_artifact_id"])
        self.assertEqual(result["audio_note"], "")
        self.assertEqual(self.engine.renders, 0)
        self.assertIsNone(self._row(problem_id)["audio_spec"])

    def test_unknown_kind_falls_back_to_listening_and_says_so(self) -> None:
        problem_id = self._seed()
        self.client.replies = [
            probe_reply(audio={"needs": True, "kind": "嗯", "text": "Hello there."}),
            SOLVE_REPLY,
        ]
        result = self.intake.resolve_problem(problem_id)
        self.assertTrue(result["audio_artifact_id"])
        self.assertEqual(self.engine.readings, 1)
        self.assertTrue(any("按听力" in note for note in result["notes"]), result["notes"])

    def test_receipt_tells_the_parent_the_sound_is_ready(self) -> None:
        """回执必须说人话:录进来时声音就已经做好绑上了(而不是"以后再说")。"""
        text = render_ingest_result({"material_id": 1, "recorded": [{
            "subject": "英语", "stem": "听写单词。", "knowledge_points": ["单词拼写"],
            "answer": "life", "answer_status": "unverified",
            "audio_artifact_id": "f123",
            "audio_note": "听写音频已生成并绑定:1.2 秒 · Microsoft Huihui Desktop",
        }]})
        self.assertIn("🔊", text)
        self.assertIn("已生成并绑定", text)
        # 做不成的那次:🔇 + 原因,不能悄悄咽下去
        text = render_ingest_result({"material_id": 1, "recorded": [{
            "subject": "英语", "stem": "听写单词。", "audio_note": "听力音频没做成:没有可用语音",
        }]})
        self.assertIn("🔇", text)
        self.assertIn("没有可用语音", text)

    # -- 手输的题(2026-10-26 #14 真机)-----------------------------------
    def test_hand_entered_dictation_gets_audio_without_any_recognition(self) -> None:
        """⭐ 真机 #14:聊天里说「录错题。语文听写涌现、结婚」,题进库了、一声不出。

        手输的题**不经过拍照识别**,而声音原先只在识别那条流水线里生成 ——
        bind_audio 就是补这一刀的:纯字符串判断要不要出声,再做一次绑上。
        """
        from la.domain.schemas.study import ProblemCreate

        record = self.study.add_problem(
            ProblemCreate(subject="语文", stem_text="语文听写,没听出来的词:涌现、结婚",
                          answer_text="涌现、结婚", knowledge_points=["生字听写"]),
            actor="user",
        )
        self.assertIsNone(self._row(record.id)["audio_artifact_id"], "录进来时还没有声音")
        hint = self.study.problem_detail(record.id)["materials"]["audio_hint"]
        self.assertIn("听写音频", hint)      # 「这题要出声,但还没做」—— 不能是"不需要声音"

        got = self.study.bind_audio(record.id, tts_engine=self.engine)
        self.assertTrue(got["needed"])
        self.assertTrue(got["audio_artifact_id"], got)
        self.assertEqual(self.engine.renders, 1)
        row = self._row(record.id)
        spec = spec_of(row["audio_spec"])
        self.assertEqual(spec["kind"], KIND_DICTATION)
        self.assertEqual(spec["text"], "涌现、结婚")
        materials = self.study.problem_detail(record.id)["materials"]
        self.assertEqual(materials["audio_handle"], "/artifacts/" + str(row["audio_artifact_id"]))
        self.assertEqual(materials["audio_hint"], "")

    def test_bind_audio_is_a_no_op_for_a_plain_problem(self) -> None:
        """普通笔答题:一个引擎都不建、一份音频都不做。"""
        from la.domain.schemas.study import ProblemCreate

        record = self.study.add_problem(
            ProblemCreate(subject="数学", stem_text="26×7÷(15-13)=?"), actor="user",
        )
        got = self.study.bind_audio(record.id, tts_engine=self.engine)
        self.assertFalse(got["needed"])
        self.assertEqual(self.engine.renders, 0)
        self.assertIsNone(self._row(record.id)["audio_spec"])

    def test_the_tool_path_also_binds_sound_on_add(self) -> None:
        """聊天工具 quiz_write(action=add)那条路:录完就把声音做上。

        实测 #14 就是这条路 —— 工具只调了 add_problem,音频一步都没走。
        """
        import asyncio

        from la.domain.tools import build_study_tools

        tools = build_study_tools(self.study, tts_engine=self.engine)
        handler = next(item for item in tools if item.name == "quiz_write").handler
        result = asyncio.run(handler({
            "action": "add", "subject": "语文",
            "stem_text": "语文听写,没听出来的词:涌现、结婚", "answer_text": "涌现、结婚",
        }))
        self.assertTrue(result["audio_handle"], result)
        self.assertIn("听写", result["audio_label"])
        self.assertIn("不要", result["audio_tip"], "要告诉模型别再重做一遍")
        problem_id = int(result["id"])
        self.assertTrue(self._row(problem_id)["audio_artifact_id"])

    # -- 照片:素材编号 ≠ 资产编号(2026-11-01 #16–#21 真机)----------------
    def _material(self, asset_id: int, *, kind: str = "image") -> int:
        media_type = "image/png" if kind == "image" else "audio/silk"
        cursor = self.conn.execute(
            "INSERT INTO app_materials(channel, asset_id, media_type, kind, handled, created_at)"
            " VALUES ('weixin', ?, ?, ?, 'recorded', ?)",
            (asset_id, media_type, kind, "2026-11-01T22:50:59+08:00"),
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    def _quiz_write(self):
        from la.domain.tools import build_study_tools

        tools = build_study_tools(self.study, tts_engine=self.engine)
        spec = next(item for item in tools if item.name == "quiz_write")
        return spec, spec.handler

    def test_material_ids_become_the_problem_photo(self) -> None:
        """⭐ 真机 #16–#21:模型把素材 #12 当资产 id 传,六道英语题全挂了同一张旧图形。

        照片只能用**素材编号**挂:换算成资产是服务端的事。
        """
        import asyncio

        asset = self.media.store_bytes(make_png(), media_type="image/png", subdir="problems")
        material_id = self._material(asset.id)
        _spec, handler = self._quiz_write()
        result = asyncio.run(handler({
            "action": "add", "subject": "英语", "stem_text": "英语跟读:think of",
            "material_ids": [material_id],
        }))
        self.assertEqual(result["asset_ids"], [asset.id])
        row = self._row(int(result["id"]))
        self.assertEqual(json.loads(row["asset_ids"]), [asset.id])

    def test_raw_asset_ids_are_rejected_and_point_to_material_ids(self) -> None:
        """模型再手填 asset_ids 时要**当场报错**,不能悄悄挂错一张图。"""
        import asyncio

        _spec, handler = self._quiz_write()
        with self.assertRaises(Exception) as caught:
            asyncio.run(handler({
                "action": "add", "subject": "英语", "stem_text": "英语跟读:also",
                "asset_ids": [12],
            }))
        self.assertIn("material_ids", str(caught.exception))

    def test_voice_material_is_not_used_as_the_photo(self) -> None:
        import asyncio

        asset = self.media.store_bytes(make_png(), media_type="image/png", subdir="problems")
        material_id = self._material(asset.id, kind="voice")
        _spec, handler = self._quiz_write()
        result = asyncio.run(handler({
            "action": "add", "subject": "英语", "stem_text": "英语跟读:love doing sth",
            "material_ids": [material_id],
        }))
        self.assertEqual(result["asset_ids"], [])
        self.assertIn("没有可用的图片素材", result.get("photo_note", ""))

    def test_tool_schema_shows_material_ids_and_hides_asset_ids(self) -> None:
        spec, _handler = self._quiz_write()
        props = spec.parameters_json_schema()["properties"]
        self.assertIn("material_ids", props)
        self.assertNotIn("asset_ids", props, "资产编号不该出现在模型能看到的 schema 里")

    def test_punctuation_only_entries_are_not_spoken(self) -> None:
        """洗完音标只剩一个 ")" 的时候,不能把它当词念出来(实测 #13)。"""
        self.assertEqual(audio_mod.words_from_list("answer 、 )"), [])
        self.assertFalse(audio_mod.looks_like_word(")"))
        self.assertTrue(audio_mod.looks_like_word("结婚"))


class SheetAudioTest(unittest.TestCase):
    """出卷那条路:**只读绑定好的音频**,一次语音调用都不该发生。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.media = MediaStore(self.conn, root=self.tmp / "media")
        self.artifacts = ArtifactStore(self.conn, root=self.tmp / "artifacts")
        self.study = StudyService(
            self.conn, media=self.media, artifacts=self.artifacts, now_provider=lambda: NOW,
        )
        self.engine = RecordingEngine(TtsEngine(
            provider="sapi", api_key="", cache_dir=self.tmp / "tts",
            runner=FakeRunner(make_wav(0.3)), rate=0,
        ))
        self.client = ScriptedClient([])
        self.intake = IntakeService(
            self.conn, study=self.study, media=self.media,
            client_factory=lambda: self.client, now_provider=lambda: NOW,
            tts_engine=self.engine,
        )

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._tmp.cleanup()

    def _bind(self) -> int:
        from la.domain.schemas.study import ProblemCreate

        asset = self.media.store_bytes(make_png(), media_type="image/png", subdir="problems")
        record = self.study.add_problem(
            ProblemCreate(subject="英语", stem_text="听写单词。", knowledge_points=["单词拼写"],
                          asset_ids=[asset.id]),
            actor="user",
        )
        self.client.replies = [probe_reply(audio=DICTATION_AUDIO), SOLVE_REPLY]
        self.intake.resolve_problem(record.id)
        return record.id

    def test_sheet_inlines_the_bound_audio_and_calls_no_voice_service(self) -> None:
        self._bind()
        renders = self.engine.renders
        result = self.study.make_sheet_html(today="2026-10-14")
        html = result["html"]
        self.assertEqual(result["problem_audios"], 1)
        self.assertEqual(self.engine.renders, renders, "出卷不该再合成任何音频")
        self.assertIn("data:audio/", html)
        self.assertNotIn("/artifacts/", html)          # 句柄必须已经变成字节,不能是裸的
        self.assertIn("🔊", html)
        self.assertIn("听写音频", html)
        self.assertIn("打印版没有声音", html)          # 打印出来也得看得出"这题要听"
        self.assertEqual(result["model"]["problem_audios"], 1)

    def test_sheet_tool_returns_the_ready_html_and_a_file_receipt(self) -> None:
        """⭐ 主智能体那条路(D110):出卷工具**默认给单文件 HTML** ——
        图形/照片/音频都在里面,而且会写一条 **artifact 域**的回执,
        微信就是靠那条回执把文件真的发出去(没有它,用户手里什么都没有)。
        """
        import asyncio

        from la.domain.receipts import DOMAIN_ARTIFACT, ReceiptLog
        from la.domain.tools import build_study_tools

        problem_id = self._bind()
        self.conn.execute(
            "UPDATE app_review_units SET due_date_local = ? WHERE problem_id = ?",
            (NOW.date().isoformat(), problem_id),
        )
        self.conn.commit()
        self.study.receipts = ReceiptLog(self.conn)
        tools = build_study_tools(self.study)
        # ⭐ 批次 B(2026-10-15):quiz_make_sheet 已并入 quiz_write(action=sheet)。
        handler = next(item for item in tools if item.name == "quiz_write").handler
        result = asyncio.run(handler({"action": "sheet"}))
        self.assertEqual(result["format"], "html")
        page = self.artifacts.read_bytes(
            self.artifacts.get(result["artifact_id"])
        ).decode("utf-8")
        self.assertIn("data:audio/", page, "绑定好的音频必须已经在页面里")
        self.assertIn("🔊", page)
        receipts = [item for item in self.study.receipts.recent(limit=20)
                    if item.domain == DOMAIN_ARTIFACT]
        self.assertTrue(
            any(str(item.detail.get("artifact_id")) == str(result["artifact_id"])
                for item in receipts),
            "少了这条回执,微信那头就不会把卷子当文件发出去",
        )
        # 只要纯文字清单时仍然给 Markdown(没有图与声音,如实说)
        text_result = asyncio.run(handler({"format": "md"}))
        self.assertEqual(text_result["format"], "md")
        self.assertIn("没有图形与音频", text_result["note"])

    def test_sheet_says_out_loud_when_the_audio_is_gone(self) -> None:
        problem_id = self._bind()
        self.conn.execute(
            "UPDATE app_problems SET audio_artifact_id = 'f0gone' WHERE id = ?", (problem_id,)
        )
        self.conn.commit()
        html = self.study.make_sheet_html(today="2026-10-14")["html"]
        self.assertNotIn("data:audio/", html)
        self.assertIn("🔇", html)
        self.assertIn("丢了", html)

    def test_sheet_says_why_a_listening_problem_has_no_sound(self) -> None:
        """识别说"要出声"但没做成 → 卷面上要写清原因,而不是默默少一个播放器。"""
        from la.domain.schemas.study import ProblemCreate

        asset = self.media.store_bytes(make_png(), media_type="image/png", subdir="problems")
        record = self.study.add_problem(
            ProblemCreate(subject="英语", stem_text="听录音,选出你听到的单词。",
                          knowledge_points=["听力"], asset_ids=[asset.id]),
            actor="user",
        )
        self.conn.execute(
            "UPDATE app_problems SET audio_spec = ? WHERE id = ?",
            (audio_mod.spec_json({"kind": "listening", "label": "听力音频",
                                  "error": "没拿到要念的原文"}), record.id),
        )
        self.conn.commit()
        html = self.study.make_sheet_html(today="2026-10-14")["html"]
        self.assertIn("🔇", html)
        self.assertIn("请家长念一遍", html)


class DeleteProblemTest(unittest.TestCase):
    """🗑 录错的题要能删掉(2026-11-09 用户:"录错的,智能体自己也删除不掉")。

    边界同样要守住:**复习过的题一律不删**,改成归档 —— 复习日志是统计与调参的依据。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.media = MediaStore(self.conn, root=self.tmp / "media")
        self.study = StudyService(self.conn, media=self.media, now_provider=lambda: NOW)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._tmp.cleanup()

    def _add(self, stem: str = "英语跟读：take care of") -> int:
        from la.domain.schemas.study import ProblemCreate

        record = self.study.add_problem(
            ProblemCreate(subject="英语", stem_text=stem, knowledge_points=["短语"]),
            actor="user",
        )
        return record.id

    def _count(self, sql: str, problem_id: int) -> int:
        return int(self.conn.execute(sql, (problem_id,)).fetchone()[0])

    def test_a_problem_that_was_never_reviewed_is_really_gone(self) -> None:
        problem_id = self._add()
        self.assertGreater(self._count("SELECT COUNT(*) FROM app_review_units WHERE problem_id = ?",
                                       problem_id), 0)
        got = self.study.delete_problem(problem_id, reason="录错了", actor="user")
        self.assertTrue(got["deleted"], got)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM app_problems WHERE id = ?", (problem_id,)).fetchone())
        for table in ("app_review_units", "app_problem_kps", "app_review_logs"):
            self.assertEqual(
                self._count("SELECT COUNT(*) FROM " + table + " WHERE problem_id = ?", problem_id),
                0, table,
            )

    def test_a_reviewed_problem_is_archived_not_deleted(self) -> None:
        problem_id = self._add()
        unit_id = int(self.study.list_units(problem_id)[0].id)
        self.study.record_review(unit_id, 1, actor="user")
        got = self.study.delete_problem(problem_id, actor="user")
        self.assertFalse(got["deleted"], got)
        self.assertTrue(got["archived"], got)
        self.assertIn("归档", got["note"])
        row = self.conn.execute(
            "SELECT status FROM app_problems WHERE id = ?", (problem_id,)).fetchone()
        self.assertIsNotNone(row, "复习过的题不许删")
        self.assertEqual(row["status"], "archived")
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM app_review_logs WHERE problem_id = ?", problem_id), 1,
            "复习日志是统计的证据,一条都不许少",
        )

    def test_the_tool_can_delete_and_says_so_in_the_schema(self) -> None:
        import asyncio

        from la.domain.tools import build_study_tools

        problem_id = self._add()
        spec = next(item for item in build_study_tools(self.study) if item.name == "quiz_write")
        self.assertIn("delete", spec.parameters_json_schema()["properties"]["action"]["description"])
        result = asyncio.run(spec.handler({
            "action": "delete", "problem_id": problem_id, "reason": "录错了",
        }))
        self.assertTrue(result["deleted"], result)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM app_problems WHERE id = ?", (problem_id,)).fetchone())

    def test_undoing_a_deleted_problem_says_it_is_gone(self) -> None:
        """回执托盘里那条"录错题"的撤销:题已经彻底删了,要如实说,不能假装归档了。"""
        import types

        from la.domain.receipts import ReceiptError

        receipt = types.SimpleNamespace(undo_plan={"type": "problem", "problem_id": 9999})
        with self.assertRaises(ReceiptError):
            self.study.revert_problem(receipt)

    def test_undoing_a_live_problem_still_archives_it(self) -> None:
        import types

        problem_id = self._add()
        receipt = types.SimpleNamespace(undo_plan={"type": "problem", "problem_id": problem_id})
        message = self.study.revert_problem(receipt)
        self.assertIn("已归档", message)
        row = self.conn.execute(
            "SELECT status FROM app_problems WHERE id = ?", (problem_id,)).fetchone()
        self.assertEqual(row["status"], "archived")

    def test_a_bare_problem_id_cannot_delete(self) -> None:
        """⚠️ 没写 action 的调用**绝不能**落到删除上 —— 删的是库里的行,猜错一次就没了。"""
        import asyncio

        from la.domain.tools import build_study_tools

        problem_id = self._add()
        spec = next(item for item in build_study_tools(self.study) if item.name == "quiz_write")
        with self.assertRaises(Exception):
            asyncio.run(spec.handler({"problem_id": problem_id}))
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM app_problems WHERE id = ?", (problem_id,)).fetchone())


if __name__ == "__main__":
    unittest.main()
