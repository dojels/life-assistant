"""图形:把模型估的 bbox 变成一张**可信的裁剪图**(错题卷要有图才作数)。

⭐ 为什么默认走"裁剪"而不是"让模型重画":

  2026-10-12 实测(照片 3,4283×718 的横条)。同一个提示词下,模型给出的
  bbox 是 [750,360,960,760](归一化到 0-1000),按它裁出来:
    * ✅ 位置对,框住的确实是那个小正方体组合体;
    * ❌ 左边多带了"看到的是"四个字,**上边把图形顶部切掉了**;
    * ❌ 再加 5% 边距重裁,顶部**仍然**被切 —— 说明上边界本身就估低了。
  同一次调用里它自发给出的 SVG 却是**对的**(4 个方块,与真图完全一致)。

  所以结论不是"谁更好",而是"两个都要,但顺序不能反":
    裁剪图 = 真相(可以核对的证据,永远留底) → 矢量图 = 修饰(清晰、可打印,但没人能替它兜底)。

三条纪律:

  1. **必须加边距再夹紧**。bbox 是模型凭视觉估的,系统性偏紧;加默认 6% 边距、
     夹到图片内。裁不出东西(退化框 / 越界 / 图太小)时**返回 None**,
     由调用方退回"整张原图" —— 宁可多带空白,也不能把图形切掉一半印给孩子。
  2. **方向先纠正**。手机横着拍的照片(EXIF 方向)必须先 transpose,否则所有坐标
     差 90 度(实测另一张照片就是躺着的,模型自己报了 rotation_degrees=270)。
  3. **失败不抛**。没装 Pillow / 图片坏了 / 框退化 —— 一律返回 (None, 原因),录入
     流程照常往下走,只是这道题没有裁剪图。界面要如实说,不许静默。

依赖 Pillow(**延迟导入**,与 python-docx / pycryptodome 同一条纪律)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

#: 裁剪框最小边长(像素)。比这还小的一定是退化框,不如不裁。
MIN_SIDE_PX = 24
#: 归一化坐标默认按 0-1000 解释(与提示词里的约定一致)
NORM_SCALE = 1000.0
#: 默认边距比例(相对图形自身宽高)。
#: ⚠️ 从 6% 提到 18% 是**实测逼出来的**:两次真照片上,模型给的框一次把图形**顶部**切掉、
#:    一次把**右侧**切掉(加 6% 都没救回来)。这里的取舍是不对称的 ——
#:    多带一点页面空白只是难看,切掉一块图形就是**题错了**。所以宁可宽,不可窄。
DEFAULT_PAD = 0.18
#: 裁剪图长边上限。手机屏幕上看图,1400px 足够,也省内联进 HTML 的体积。
DEFAULT_MAX_SIDE = 1400

PILLOW_HINT = (
    "图形裁剪需要 Pillow:请执行 "
    ".venv/Scripts/python.exe -m pip install pillow"
)


class FigureError(RuntimeError):
    """图形的领域错误(调用方可以捕获它并降级)。"""


@dataclass
class CropResult:
    """一次裁剪的结果。**失败也是一种结果**,用 ok 判断,不要靠异常。"""

    data: bytes = b""
    media_type: str = "image/png"
    width: int = 0
    height: int = 0
    source_width: int = 0
    source_height: int = 0
    #: 实际用的像素框 (left, top, right, bottom)
    box: tuple[int, int, int, int] = (0, 0, 0, 0)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.data) and not self.error

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "media_type": self.media_type,
            "width": self.width,
            "height": self.height,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "box": list(self.box),
            "bbox": [round(value, 4) for value in self.bbox],
            "error": self.error,
            "byte_size": len(self.data),
        }


def pillow_ready() -> tuple[bool, str]:
    """Pillow 是否可用。返回 (可用, 原因/版本)。"""
    try:
        from PIL import Image  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - 没装就是没装,不是错误
        return False, PILLOW_HINT + "(" + type(exc).__name__ + ")"
    try:
        import PIL

        return True, "Pillow " + str(getattr(PIL, "__version__", "?"))
    except Exception:  # noqa: BLE001
        return True, "Pillow"


# -- 归一化坐标 ---------------------------------------------------------------
def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace("%", "")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def parse_bbox(value: Any) -> tuple[float, float, float, float] | None:
    """把模型给的框收敛成**归一化 0..1**的 (left, top, right, bottom)。

    ⚠️ 三件事必须容错,因为模型三种写法都会用:
      * 0-1000 整数(提示词里的约定)与 0..1 小数 —— 用"最大值 > 1.5"判别;
      * 顺序写反(右<左)—— 交换回来,而不是判它无效;
      * 退化框(宽或高为 0)—— 返回 None,交给调用方退回原图。
    字典写法 {"x1":..,"y1":..,"x2":..,"y2":..} 一并认(不同模型爱用不同壳)。
    """
    if isinstance(value, dict):
        for keys in (("x1", "y1", "x2", "y2"), ("left", "top", "right", "bottom"),
                     ("l", "t", "r", "b")):
            if all(key in value for key in keys):
                value = [value[key] for key in keys]
                break
        else:
            return None
    if isinstance(value, str):
        parts = [item for item in value.replace("，", ",").replace(" ", ",").split(",") if item]
        value = parts
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    numbers = [_as_float(item) for item in list(value)[:4]]
    if any(item is None for item in numbers):
        return None
    left, top, right, bottom = (float(item) for item in numbers)  # type: ignore[arg-type]
    if max(left, top, right, bottom) > 1.5:
        left, top, right, bottom = (item / NORM_SCALE for item in (left, top, right, bottom))
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top
    left, top = max(0.0, left), max(0.0, top)
    right, bottom = min(1.0, right), min(1.0, bottom)
    if right - left <= 0 or bottom - top <= 0:
        return None
    return (left, top, right, bottom)


def expand_bbox(
    bbox: Sequence[float], pad: float = DEFAULT_PAD
) -> tuple[float, float, float, float]:
    """按自身宽高的比例向外扩边距(默认 6%)。"""
    left, top, right, bottom = (float(item) for item in bbox[:4])
    pad_x = max(0.0, (right - left) * float(pad))
    pad_y = max(0.0, (bottom - top) * float(pad))
    return (left - pad_x, top - pad_y, right + pad_x, bottom + pad_y)



def shrink_bbox(
    bbox: Sequence[float], ratio: float = 0.12
) -> tuple[float, float, float, float] | None:
    """向**中心**收一点(裁进来的题干文字太多时用)。收得太小就返回 None。

    ⚠️ 收窄是**有风险**的动作(可能把图形切掉),所以只给人点、不自动做;
    而且收完任一维小于 3% 就当作无效,让调用方保留原框。
    """
    left, top, right, bottom = (float(item) for item in bbox[:4])
    keep = max(0.1, 1.0 - float(ratio))
    cx = (left + right) / 2.0
    cy = (top + bottom) / 2.0
    half_w = (right - left) * keep / 2.0
    half_h = (bottom - top) * keep / 2.0
    if half_w <= 0.015 or half_h <= 0.015:
        return None
    return (max(0.0, cx - half_w), max(0.0, cy - half_h),
            min(1.0, cx + half_w), min(1.0, cy + half_h))


def rotation_tiles(payload: bytes, *, max_side: int = 600, quality: int = 85) -> list[bytes]:
    """把一块图按 0/90/180/270(**顺时针**)各出一张 JPEG。

    ⭐ 给「哪一张是正的」那次复核用。为什么不拼成一张拼图:拼图要靠画在格子上的标记
       指认,多一层「把格子对回角度」的心算;四张独立图配一句「第几张 = 多少度」,
       是一次纯辨认 —— 实测自由回答角度只有 1/4 对,四选一两次全对。
    """
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(payload)) as image:
            piece = image.convert("RGB")
            longest = max(piece.size)
            if max_side and longest > max_side:
                scale = float(max_side) / float(longest)
                piece = piece.resize(
                    (max(1, int(piece.width * scale)), max(1, int(piece.height * scale)))
                )
        out: list[bytes] = []
        for angle in (0, 90, 180, 270):
            turned = piece.rotate(-angle, expand=True, fillcolor="white") if angle else piece
            buffer = io.BytesIO()
            turned.save(buffer, format="JPEG", quality=quality)
            out.append(buffer.getvalue())
        return out
    except Exception:  # noqa: BLE001 - 出不了图就退回「不复核」,不是错误
        return []


def clamp_bbox(
    bbox: Sequence[float], *, minimum: float = 0.02
) -> tuple[float, float, float, float] | None:
    """夹到 [0,1];夹完太小(任一维 < minimum)就返回 None。"""
    left, top, right, bottom = (float(item) for item in bbox[:4])
    left, top = max(0.0, min(1.0, left)), max(0.0, min(1.0, top))
    right, bottom = max(0.0, min(1.0, right)), max(0.0, min(1.0, bottom))
    if right - left < minimum or bottom - top < minimum:
        return None
    return (left, top, right, bottom)


def bbox_to_pixels(
    bbox: Sequence[float], width: int, height: int, *, min_side: int = MIN_SIDE_PX
) -> tuple[int, int, int, int] | None:
    """归一化框 → 像素框。太小/越界一律 None(调用方退回原图)。"""
    if width <= 0 or height <= 0:
        return None
    left, top, right, bottom = (float(item) for item in bbox[:4])
    box = (
        max(0, int(round(left * width))),
        max(0, int(round(top * height))),
        min(int(width), int(round(right * width))),
        min(int(height), int(round(bottom * height))),
    )
    if box[2] - box[0] < min_side or box[3] - box[1] < min_side:
        return None
    return box


def crop_figure(
    data: bytes,
    bbox: Any,
    *,
    pad: float = DEFAULT_PAD,
    max_side: int = DEFAULT_MAX_SIDE,
    rotate_degrees: int = 0,
) -> CropResult:
    """从原图字节里裁出图形。**任何失败都变成 result.error,不往外抛。**

    ⚠️ 四个顺序不能换:
      1. exif_transpose(先把照片摆正,否则坐标全差 90 度);
      2. 转 RGB(CMYK / P 模式存 PNG 会出事,手机照片偶尔带这种);
      3. **裁剪**(bbox 是模型在**原始方向**上估的,所以必须先裁);
      4. 再按 rotate_degrees 把**裁出来的那块**摆正(逆时针为正,PIL 口径)。

    ⚠️ 旋转是**最不可靠的一环**:实测同一张横躺的照片,模型报 270,而正确答案是顺时针 90 ——
    差一个方向。所以调用方要把"转了多少度"记进 figure_spec 并**显示给用户看**,
    让人一眼能发现"摆反了",而不是悄悄印一张倒过来的图。
    """
    try:
        from PIL import Image, ImageOps
    except Exception as exc:  # noqa: BLE001
        return CropResult(error=PILLOW_HINT + "(" + type(exc).__name__ + ")")
    if not data:
        return CropResult(error="空图片")
    parsed = parse_bbox(bbox)
    if parsed is None:
        return CropResult(error="没有可用的图形框(模型没给出 bbox,或框退化了)")
    try:
        import io

        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            padded = clamp_bbox(expand_bbox(parsed, pad))
            if padded is None:
                return CropResult(error="图形框贴在边上/太小,裁不出来 —— 退回原图")
            box = bbox_to_pixels(padded, width, height)
            if box is None:
                return CropResult(error="图形框太小(不足 " + str(MIN_SIDE_PX) + " 像素)—— 退回原图")
            piece = image.convert("RGB").crop(box)
            angle = int(rotate_degrees or 0) % 360
            if angle:
                # PIL 的 rotate 是**逆时针**;rotate_degrees 是"顺时针多少度才正"
                piece = piece.rotate(-angle, expand=True, fillcolor="white")
            longest = max(piece.size)
            if max_side and longest > max_side:
                scale = float(max_side) / float(longest)
                piece = piece.resize(
                    (max(1, int(piece.width * scale)), max(1, int(piece.height * scale)))
                )
            buffer = io.BytesIO()
            piece.save(buffer, format="PNG", optimize=True)
            payload = buffer.getvalue()
            return CropResult(
                data=payload, width=piece.width, height=piece.height,
                source_width=width, source_height=height, box=box, bbox=padded,
            )
    except Exception as exc:  # noqa: BLE001 - 裁不出来不该让录入整条断掉
        return CropResult(error=type(exc).__name__ + ": " + str(exc)[:160])


#: 一块裁剪图里"有内容的像素"占比低于这个值,就**有可能**是空白(没框到东西)。
#: 实测:模型有一次给的框落在**空白纸面**上,裁出来是一张纯纸 —— 与其把它当"图形"
#: 印在卷子上,不如说"这次没取到,打开原图看"。
#:
#: ⚠️ 2026-11-21 实测修正:光看"暗像素占比"会把**真图形判成空白**,而且不是偶发 ——
#:    两道真错题里,一道(草坪+两条小路)裁出来是**完整、方向也对的图形**,却被这里判成
#:    空白丢掉,卷面上就成了"识别说这题有图形,但这次没取到";换成"线条结构"再判,
#:    同一张图的结构占比 0.05+ 而白纸只有 0.000。原因是 80×80 的平均缩小把细线
#:    糊成了浅灰(实测同一张图:80px 下 0.0033、320px 下 0.0255)。
#:    所以现在**两个条件都满足**才算空白:又不够暗、又没有线条。
MIN_INK_RATIO = 0.012
#: "有线条结构"的像素占比下限(见 structure_ratio)。低于它 + 暗像素也少 = 真空白。
MIN_STRUCTURE_RATIO = 0.004


def ink_ratio(payload: bytes) -> float:
    """裁剪图里"明显比纸暗"的像素占比。用来判断这块到底有没有东西。"""
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(payload)) as image:
            small = image.convert("L").resize((80, 80))
            # ⚠️ 用 tobytes() 而不是 getdata():后者在 Pillow 14 会被移除(实测有弃用警告)
            pixels = list(small.tobytes())
    except Exception:  # noqa: BLE001 - 量不出来就当"有内容",不因此丢掉图
        return 1.0
    if not pixels:
        return 1.0
    if max(pixels) - min(pixels) < 12:
        return 0.0                      # 整块一个色 = 空白(白纸、纯色底)
    darkest = min(pixels)
    # 纸本身有底色与阴影,所以阈值贴着**这张图自己的最暗值**取,再看有多少像素接近它
    threshold = darkest + max(20, int((sum(pixels) / len(pixels) - darkest) * 0.35))
    dark = sum(1 for value in pixels if value <= threshold)
    return dark / len(pixels)


def structure_ratio(payload: bytes, *, size: int = 160, delta: int = 24) -> float:
    """裁剪图里"有线条结构"的像素占比 —— 白纸≈0,线描图形明显大于 0。

    ⚠️ 为什么需要它:ink_ratio 只看"暗",而教科书的几何图形是**细线**画在白纸上,
    面积一大、线一细,平均缩小后线就淡到不算"暗"了(实测 0.0033,阈值 0.012)。
    线条判据看的是**相邻像素的落差**,缩图不会把它抹平(除非本来就什么都没有)。
    """
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(payload)) as image:
            # ⚠️ 用 BOX(区域平均)而不是默认的 BICUBIC:后者会振铃出假边缘。
            small = image.convert("L").resize((size, size), Image.BOX)
            pixels = small.tobytes()
    except Exception:  # noqa: BLE001 - 量不出来就当"有内容",不因此丢掉图
        return 1.0
    if len(pixels) < size * size:
        return 1.0
    strong = 0
    for y in range(size):
        row = y * size
        for x in range(1, size):
            index = row + x
            if abs(pixels[index] - pixels[index - 1]) >= delta:
                strong += 1
            if y and abs(pixels[index] - pixels[index - size]) >= delta:
                strong += 1
    return strong / float(size * size * 2)


def looks_blank(
    payload: bytes,
    *,
    minimum: float = MIN_INK_RATIO,
    structure: float = MIN_STRUCTURE_RATIO,
) -> bool:
    """这块裁剪图是不是**空白纸面**。

    ⚠️ 两个条件都要满足才算空白(理由见 MIN_INK_RATIO 上面那段实测):只看暗像素会
    把细线图形丢掉 —— 而"丢掉图形"正是用户看到的"这题明明有图,却没取到"。
    """
    if ink_ratio(payload) >= minimum:
        return False
    return structure_ratio(payload) < structure


def bbox_to_thousand(bbox: Any) -> list[int]:
    """0-1 归一化框 → 0-1000 整数框(给提示词用)。

    ⚠️ 实测踩过:0-1 的框被当成 0-1000 喂给模型,`[0.7356, 0.3368, …]` 取整后变成
    `[1, 0, 1, 0]` —— 模型看到的是一句"你的框只有左上角一点",整条复核就废了。
    """
    values: list[int] = []
    for item in list(bbox or [])[:4]:
        try:
            values.append(int(round(float(item) * NORM_SCALE)))
        except (TypeError, ValueError):
            values.append(0)
    return values


#: 发给模型看的副本的长边上限。
#: ⭐ 这是整套图形流程里**最关键的一个数**(实测找出来的根因):
#:    把 4283×718 的原图**直接**发过去,模型的 bbox 三次给出三个完全不同的答案
#:    (切掉图形右边 / 落在旁边的文字上 / 落在空白纸面上);把同一张图缩到长边 1200
#:    再问,坐标立刻准了 —— 裁出来是完整的图形。
#:    原因大概率是服务端对大图做了缩放或切块,归一化坐标于是不是"整张原图"的坐标。
#:    坐标是归一化的,所以**问的时候用小图、裁的时候用原图**,两边不用换算。
MODEL_MAX_SIDE = 1400


def downscale_for_model(
    data: bytes, *, max_side: int = MODEL_MAX_SIDE, quality: int = 88
) -> tuple[bytes, str]:
    """给模型看的副本(长边 ≤ max_side 的 JPEG)。Pillow 不可用/图本来就小 → 原样返回。"""
    try:
        from PIL import Image, ImageOps
        import io

        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            width, height = image.size
            longest = max(width, height)
            if longest <= max_side:
                return data, ""
            scale = float(max_side) / float(longest)
            small = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
            buffer = io.BytesIO()
            small.save(buffer, format="JPEG", quality=quality)
            return buffer.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - 缩不了就用原图,大不了回退到"框可能不准"
        return data, ""


def crop_figure_sync(data: bytes, bbox: Any, **kwargs: Any) -> CropResult:
    """同步入口(与 vision.recognize_image_sync 同一个理由:Web 端在线程池里跑)。"""
    return crop_figure(data, bbox, **kwargs)


def figure_hint(problem: Any) -> str:
    """给界面/试卷用的一句话:这道题有没有图、图从哪来。"""
    source = str(getattr(problem, "figure_source", "") or "").strip().lower()
    if source == "crop":
        return "图形:原图裁剪"
    if source == "svg":
        return "图形:矢量重绘"
    return ""
