"""图形:把模型给的**图形规格**渲染成 SVG(白名单,自家渲染器出图)。

⭐ 为什么不让模型直接写 SVG 源码(这条是实测定的):

  同一道图形题,模型自发给过一段**完全正确**的 SVG(4 个方块,与真图一致)——
  但那次它把整段 SVG 塞进了约定好的 `redraw` 字段里,把 schema 冲掉了;而另两次
  同一个模型给的**坐标**分别落在旁边的文字上和空白纸面上。结论:

    * "重画一个简单的规则图形"这件事,模型**做得到**;
    * 但让它同时**输出代码**和**遵守格式**,就是两件事一起赌,而且错了没人能校验。

  所以这里把两件事拆开:**模型只输出"这是什么图 + 参数"**(纯数据,可校验),
  **画由我们自己的渲染器画**(确定性、风格统一、能自检、能回归)。

⚠️ 三条纪律:

  1. **白名单之外一律不画**,退回照片裁剪。画错一个图形,比没有图形更坏 ——
     孩子会照着错的图去理解题意。
  2. **参数要自检**:和不对、角不合法、标注缺失 → 直接判不通过,交给裁剪。
  3. **零外链**:渲染出来的 SVG 不带任何外部引用(字体用系统通用族、不引 CSS)。
"""

from __future__ import annotations

from typing import Any, Callable
from xml.sax.saxutils import escape

#: 画布默认尺寸(px)。试卷里按容器宽度等比缩放。
CANVAS_W = 520
CANVAS_H = 320
PAD = 36

FILL = "#cfe3f2"
LINE = "#2b3440"
DIM = "#5b6675"
#: ⚠️ 这里必须**自带双引号**(它是要拼进标签的属性,不是属性值):
#:    写成 `font-family:'Segoe UI'` 会拼出一个没有引号的属性,XML 直接不合法 ——
#:    实测就是这么踩的(单元测试里用 ElementTree 解析才抓出来,肉眼看不出来)。
FONT = 'font-family="Segoe UI, Microsoft YaHei, sans-serif"'


class SpecError(ValueError):
    """图形规格不合法(调用方应当退回照片裁剪)。"""


def _number(value: Any, *, name: str, minimum: float | None = None,
            maximum: float | None = None, default: float | None = None) -> float:
    if value is None or value == "":
        if default is not None:
            return float(default)
        raise SpecError("缺少参数:" + name)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SpecError("参数 " + name + " 不是数字:" + str(value)[:20]) from exc
    if minimum is not None and number < minimum:
        raise SpecError("参数 " + name + " 太小")
    if maximum is not None and number > maximum:
        raise SpecError("参数 " + name + " 太大")
    return number


def _int(value: Any, *, name: str, minimum: int, maximum: int, default: int | None = None) -> int:
    number = _number(value, name=name, default=default)
    result = int(round(number))
    if result < minimum or result > maximum:
        raise SpecError("参数 " + name + " 超出范围")
    return result


def _text(value: Any, limit: int = 12) -> str:
    return escape(" ".join(str(value or "").split())[:limit])


def _svg(body: str, *, width: int = CANVAS_W, height: int = CANVAS_H,
         label: str = "题目图形") -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d"'
        ' role="img" aria-label="%s">'
        '<rect x="0" y="0" width="%d" height="%d" fill="#ffffff"/>%s</svg>'
        % (width, height, width, height, escape(label), width, height, body)
    )


def _line(x1: float, y1: float, x2: float, y2: float, *, width: float = 2.0,
          dash: str = "", color: str = LINE) -> str:
    extra = (' stroke-dasharray="' + dash + '"') if dash else ""
    return ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="%.1f"%s/>'
            % (x1, y1, x2, y2, color, width, extra))


def _text_at(x: float, y: float, value: str, *, size: int = 16, color: str = LINE,
             anchor: str = "middle") -> str:
    return ('<text x="%.1f" y="%.1f" font-size="%d" fill="%s" text-anchor="%s" %s>%s</text>'
            % (x, y, size, color, anchor, FONT, value))


# -- 各种图形 ----------------------------------------------------------------
def unit_cubes_front(params: dict[str, Any]) -> str:
    """小正方体组合体的**正面视图**:下面一排 N 个,指定列的上方再叠 1 个。"""
    cols = _int(params.get("cols"), name="cols", minimum=1, maximum=8, default=3)
    top_raw = params.get("top")
    if top_raw is None:
        top_raw = params.get("top_columns") or []
    if isinstance(top_raw, (int, float, str)):
        top_raw = [top_raw]
    if not isinstance(top_raw, (list, tuple)):
        raise SpecError("top 必须是列的序号数组")
    tops = sorted({_int(item, name="top", minimum=0, maximum=cols - 1) for item in top_raw})
    side = min(84.0, (CANVAS_W - 2 * PAD) / max(cols, 1))
    rows = 2 if tops else 1
    width, height = side * cols, side * rows
    left = (CANVAS_W - width) / 2
    top = (CANVAS_H - height) / 2
    parts: list[str] = []
    for column in range(cols):
        y = top + (side if tops else 0)
        parts.append(
            '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" stroke="%s"'
            ' stroke-width="2"/>' % (left + column * side, y, side, side, FILL, LINE)
        )
    for column in tops:
        parts.append(
            '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" stroke="%s"'
            ' stroke-width="2"/>'
            % (left + column * side, top, side, side, FILL, LINE)
        )
    return _svg("".join(parts), label="小正方体组合体的正面视图")


def number_line(params: dict[str, Any]) -> str:
    """数轴:刻度 + 标注 + 可选的点。"""
    low = _number(params.get("min"), name="min", default=0)
    high = _number(params.get("max"), name="max", default=10)
    if high <= low:
        raise SpecError("数轴的 max 必须大于 min")
    step = _number(params.get("step"), name="step", minimum=0.0001, default=(high - low) / 10)
    if (high - low) / step > 40:
        raise SpecError("刻度太密")
    left, right = PAD + 16, CANVAS_W - PAD
    axis_y = CANVAS_H / 2 + 40
    parts = [
        _line(left, axis_y, right, axis_y, width=2),
        '<polygon points="%.1f,%.1f %.1f,%.1f %.1f,%.1f" fill="%s"/>'
        % (right + 14, axis_y, right, axis_y - 6, right, axis_y + 6, LINE),
    ]
    value = low
    index = 0
    while value <= high + 1e-9 and index <= 40:
        x = left + (right - left) * ((value - low) / (high - low))
        parts.append(_line(x, axis_y - 6, x, axis_y + 6, width=1.5))
        parts.append(_text_at(x, axis_y + 26, _num(value), size=14, color=DIM))
        value += step
        index += 1
    for point in params.get("points") or []:
        if isinstance(point, dict):
            value = _number(point.get("value"), name="points.value")
            label = _text(point.get("label"), 10)
        else:
            value = _number(point, name="points")
            label = ""
        if value < low or value > high:
            continue
        x = left + (right - left) * ((value - low) / (high - low))
        parts.append('<circle cx="%.1f" cy="%.1f" r="5" fill="%s"/>' % (x, axis_y, LINE))
        if label:
            parts.append(_text_at(x, axis_y - 16, label, size=16))
    return _svg("".join(parts), label="数轴")


def axes_linear(params: dict[str, Any]) -> str:
    """坐标系里的一条直线 y = a·x + b。"""
    a = _number(params.get("a"), name="a", default=1)
    b = _number(params.get("b"), name="b", default=0)
    x_min = _number(params.get("x_min"), name="x_min", default=-5)
    x_max = _number(params.get("x_max"), name="x_max", default=5)
    if x_max <= x_min:
        raise SpecError("x_max 必须大于 x_min")
    y_at = [a * x_min + b, a * x_max + b]
    y_low, y_high = min(0.0, min(y_at)), max(0.0, max(y_at))
    if y_high - y_low < 1e-9:
        y_high = y_low + 1
    left, right = PAD + 20, CANVAS_W - PAD - 20
    top, bottom = PAD, CANVAS_H - PAD - 24

    def px(x: float) -> float:
        return left + (right - left) * ((x - x_min) / (x_max - x_min))

    def py(y: float) -> float:
        return bottom - (bottom - top) * ((y - y_low) / (y_high - y_low))

    parts = [
        _line(px(x_min), py(0), px(x_max), py(0), width=2),
        _line(px(0), top, px(0), bottom, width=2),
        _text_at(px(x_max) - 6, py(0) + 22, "x", size=15, color=DIM),
        _text_at(px(0) - 16, top + 14, "y", size=15, color=DIM),
        _line(px(x_min), py(a * x_min + b), px(x_max), py(a * x_max + b), width=2.5),
    ]
    return _svg("".join(parts), label="坐标系与直线")


def triangle(params: dict[str, Any]) -> str:
    """三角形(带顶点标签、可标直角)。"""
    labels = params.get("labels") or ["A", "B", "C"]
    if not isinstance(labels, (list, tuple)) or len(labels) < 3:
        raise SpecError("三角形需要 3 个顶点标签")
    points = [(CANVAS_W / 2, PAD + 20), (PAD + 30, CANVAS_H - PAD - 20),
              (CANVAS_W - PAD - 30, CANVAS_H - PAD - 20)]
    right_at = str(params.get("right_angle_at") or "").strip().upper()
    parts = [
        '<polygon points="%s" fill="%s" stroke="%s" stroke-width="2.5"/>'
        % (" ".join("%.1f,%.1f" % point for point in points), FILL, LINE)
    ]
    offsets = [(0, -12), (-18, 20), (18, 20)]
    for index, point in enumerate(points):
        parts.append(_text_at(point[0] + offsets[index][0], point[1] + offsets[index][1],
                              _text(labels[index], 4), size=17))
    if right_at:
        for index, name in enumerate(labels):
            if str(name).strip().upper() != right_at:
                continue
            x, y = points[index]
            size = 18.0
            towards = [
                (points[(index + 1) % 3][0] - x, points[(index + 1) % 3][1] - y),
                (points[(index + 2) % 3][0] - x, points[(index + 2) % 3][1] - y),
            ]
            marks = []
            for dx, dy in towards:
                length = max((dx * dx + dy * dy) ** 0.5, 1e-6)
                marks.append((x + dx / length * size, y + dy / length * size))
            parts.append('<polyline points="%.1f,%.1f %.1f,%.1f %.1f,%.1f" fill="none"'
                         ' stroke="%s" stroke-width="2"/>'
                         % (marks[0][0], marks[0][1],
                            x + (marks[0][0] - x) + (marks[1][0] - x),
                            y + (marks[0][1] - y) + (marks[1][1] - y),
                            marks[1][0], marks[1][1], LINE))
    return _svg("".join(parts), label="三角形")


def rectangle(params: dict[str, Any]) -> str:
    """矩形 / 正方形,可标长与宽。"""
    width_label = _text(params.get("width_label") or params.get("w"), 10)
    height_label = _text(params.get("height_label") or params.get("h"), 10)
    ratio = _number(params.get("ratio"), name="ratio", minimum=0.2, maximum=5.0, default=1.6)
    box_w = min(CANVAS_W - 2 * PAD, 300.0)
    box_h = box_w / ratio
    if box_h > CANVAS_H - 2 * PAD:
        box_h = CANVAS_H - 2 * PAD
        box_w = box_h * ratio
    x = (CANVAS_W - box_w) / 2
    y = (CANVAS_H - box_h) / 2
    parts = ['<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" stroke="%s"'
             ' stroke-width="2.5"/>' % (x, y, box_w, box_h, FILL, LINE)]
    if width_label:
        parts.append(_text_at(x + box_w / 2, y + box_h + 26, width_label, size=16))
    if height_label:
        parts.append(_text_at(x - 14, y + box_h / 2 + 5, height_label, size=16, anchor="end"))
    return _svg("".join(parts), label="矩形")


def circle(params: dict[str, Any]) -> str:
    """圆 + 半径(或直径)。"""
    show_diameter = bool(params.get("show_diameter"))
    centre = (CANVAS_W / 2, CANVAS_H / 2)
    radius = min(CANVAS_W, CANVAS_H) / 2 - PAD
    parts = ['<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s" stroke="%s" stroke-width="2.5"/>'
             % (centre[0], centre[1], radius, FILL, LINE),
             '<circle cx="%.1f" cy="%.1f" r="3.5" fill="%s"/>' % (centre[0], centre[1], LINE)]
    if show_diameter:
        parts.append(_line(centre[0] - radius, centre[1], centre[0] + radius, centre[1],
                           width=2.5, dash="6 5"))
    else:
        parts.append(_line(centre[0], centre[1], centre[0] + radius, centre[1], width=2.5))
    label = _text(params.get("radius_label") or params.get("label"), 10)
    if label:
        y = centre[1] - 10 if not show_diameter else centre[1] - 12
        parts.append(_text_at(centre[0] + radius / 2, y, label, size=16))
    if bool(params.get("show_centre")):
        parts.append(_text_at(centre[0] - 14, centre[1] + 20, "O", size=16))
    return _svg("".join(parts), label="圆")


def bar_chart(params: dict[str, Any]) -> str:
    """柱状图:类目 + 数值。"""
    categories = params.get("categories") or params.get("labels") or []
    values = params.get("values") or params.get("data") or []
    if not isinstance(categories, (list, tuple)) or not isinstance(values, (list, tuple)):
        raise SpecError("柱状图需要 categories 与 values 两个数组")
    if len(categories) != len(values) or not categories:
        raise SpecError("柱状图的类目与数值个数必须一致且非空")
    if len(categories) > 8:
        raise SpecError("类目太多")
    numbers = [_number(item, name="values") for item in values]
    if min(numbers) < 0:
        raise SpecError("负数的柱状图暂不支持")
    top_value = max(numbers) or 1.0
    left, right = PAD + 20, CANVAS_W - PAD
    base = CANVAS_H - PAD - 24
    top = PAD + 10
    slot = (right - left) / len(numbers)
    bar_w = slot * 0.55
    parts = [_line(left - 10, base, right, base, width=2),
             _line(left - 10, base, left - 10, top, width=2)]
    for index, (name, value) in enumerate(zip(categories, numbers)):
        height = (base - top) * (value / top_value)
        x = left + slot * index + (slot - bar_w) / 2
        y = base - height
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" stroke="%s"'
                     ' stroke-width="1.5"/>' % (x, y, bar_w, height, FILL, LINE))
        parts.append(_text_at(x + bar_w / 2, y - 8, _num(value), size=14))
        parts.append(_text_at(x + bar_w / 2, base + 22, _text(name, 8), size=14, color=DIM))
    unit = _text(params.get("unit"), 6)
    if unit:
        parts.append(_text_at(left - 10, top - 4, unit, size=13, color=DIM, anchor="end"))
    return _svg("".join(parts), label="柱状图")


def _num(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return ("%.4f" % value).rstrip("0").rstrip(".")


BUILDERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "unit_cubes_front": unit_cubes_front,
    "number_line": number_line,
    "axes_linear": axes_linear,
    "triangle": triangle,
    "rectangle": rectangle,
    "circle": circle,
    "bar_chart": bar_chart,
}

#: 有些图形**必须有参数才画**:没有参数时"默认值"不是在补全,而是在**编**。
#: 例如小正方体组合体,不给 cols/top 就画"三个一排"—— 那是替题目下结论,不是画图。
#: (三角形、矩形、圆这类"通用形状"不在此列:不给参数画的也确实是它。)
REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "unit_cubes_front": ("cols", "top"),
    "number_line": ("min", "max"),
    "axes_linear": ("a",),
    "bar_chart": ("categories", "values"),
}

#: 每种图形"要哪些参数、分别是什么"的一句话说明(单独追问时用)。
#: ⚠️ 实测:模型经常只报 kind 不报参数(它照着提示词里的 JSON 模板写了个 null 出来)。
#:    与其反复改主提示词,不如**单独追问一句** —— 一次一问,它给得比"顺手带上"准得多。
PARAM_GUIDE: dict[str, str] = {
    "unit_cubes_front": "cols(下面一排几个,1-8 的整数)、top(上面再叠一层的**列号**数组,从左往右从 0 开始;没有就空数组)",
    "number_line": "min(最左刻度)、max(最右刻度)、step(刻度间隔),可选 points([{\"value\":数字,\"label\":\"字母\"}])",
    "axes_linear": "a 与 b(直线 y = a·x + b),可选 x_min、x_max",
    "triangle": "labels(三个顶点标签的数组,如 [\"A\",\"B\",\"C\"]),可选 right_angle_at(直角顶点的标签)",
    "rectangle": "width_label(横边上的文字)、height_label(竖边上的文字),可选 ratio(宽高比)",
    "circle": "radius_label(半径上的文字),可选 show_diameter(true/false)、show_centre(true/false)",
    "bar_chart": "categories(类目数组)、values(数值数组,个数与类目一致),可选 unit",
}


#: 给提示词用的白名单说明(**改这里就等于改提示词**,不许两处各写一份)
CATALOG = """\
- unit_cubes_front:小正方体组合体的正面视图。参数 cols(下面一排几个,1-8)、top(上面再叠一层的列号数组,从 0 开始)
- number_line:数轴。参数 min、max、step,可选 points([{"value":3,"label":"A"}])
- axes_linear:坐标系里的一条直线。参数 a、b(即 y=a·x+b),可选 x_min、x_max
- triangle:三角形。参数 labels(["A","B","C"]),可选 right_angle_at(直角顶点,如 "A")
- rectangle:矩形/正方形。参数 width_label、height_label(写在边上的文字),可选 ratio(宽高比)
- circle:圆。参数 radius_label(写在半径上的文字),可选 show_diameter、show_centre
- bar_chart:柱状图。参数 categories(类目数组)、values(数值数组),可选 unit

⚠️ 下面这几种**必须把参数给全**,缺一个我就**不画**(改用照片裁剪,不会替你猜):
""" + "、".join(
    kind + "(" + "、".join(keys) + ")" for kind, keys in REQUIRED_PARAMS.items()
)


def supported_kinds() -> list[str]:
    return sorted(BUILDERS)


#: figure 里"不是参数"的那些键(挑参数时要排除)
_NOT_PARAMS = ("present", "kind", "type", "description", "desc", "bbox", "render",
               "spec", "figure_spec", "params", "parameters", "rotation_degrees")


def pick_spec(figure: Any) -> dict[str, Any] | None:
    """从模型给的 figure 里挑出**可渲染的规格**。挑不到就返回 None(调用方退回裁剪)。

    ⚠️ 为什么要这么宽容:实测模型读懂了白名单,却把白名单里的名字填进了它本来就有的
    `kind` 字段(`"kind":"unit_cubes_front"`),而没有另外给一个 `render` 对象 ——
    于是"照着格式要"变成了"什么都没拿到"。既然两种写法表达的是同一件事,
    解析这边就该两种都认:**格式的宽容留在解析层,渲染层只认干净的规格。**
    """
    if not isinstance(figure, dict):
        return None
    for key in ("render", "spec", "figure_spec", "规格"):
        value = figure.get(key)
        if isinstance(value, dict) and str(value.get("kind") or "").strip():
            return _clean(value)
    name = whitelist_name(figure.get("kind"))
    if name:
        params = figure.get("params") or figure.get("parameters")
        if not isinstance(params, dict) or not params:
            # kind 对了但没给 params:把 figure 里其余的键当成参数(常见的偷懒写法)
            params = {
                key: value for key, value in figure.items() if key not in _NOT_PARAMS
            }
        return {"kind": name, "params": params or {}}
    return None


#: 模型爱用的别名 → 白名单里的规范名。
#: ⚠️ 实测:同一道图形题,模型两次分别报了 "unit_cubes_front"(照抄了白名单)和
#:    "front_view"(自己的说法)。**同一件事两种叫法**是模型的常态,
#:    与 normalize_subject(学科)、normalize_knowledge_points(知识点)是同一条纪律:
#:    收敛放在解析层,渲染层只认规范名。
ALIASES: dict[str, str] = {
    "front_view": "unit_cubes_front",
    "cubes_front": "unit_cubes_front",
    "unit_cubes": "unit_cubes_front",
    "小正方体组合体": "unit_cubes_front",
    "正面视图": "unit_cubes_front",
    "组合体正面视图": "unit_cubes_front",
    "numberline": "number_line",
    "数轴": "number_line",
    "axes": "axes_linear",
    "coordinate_axes": "axes_linear",
    "坐标系": "axes_linear",
    "坐标系与直线": "axes_linear",
    "函数图像": "axes_linear",
    "三角形": "triangle",
    "矩形": "rectangle",
    "长方形": "rectangle",
    "正方形": "rectangle",
    "圆": "circle",
    "柱状图": "bar_chart",
    "条形图": "bar_chart",
    "统计图": "bar_chart",
}


def whitelist_name(value: Any) -> str:
    """把模型给的 kind 归一到白名单里的名字;认不出来就返回空串。"""
    name = _kind_name(value)
    if name in BUILDERS:
        return name
    return ALIASES.get(name) or ALIASES.get(str(value or "").strip(), "")


def _kind_name(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _clean(spec: dict[str, Any]) -> dict[str, Any]:
    params = spec.get("params")
    if not isinstance(params, dict):
        params = spec.get("parameters")
    if not isinstance(params, dict):
        params = {key: value for key, value in spec.items()
                  if key not in ("kind", "type", "params", "parameters")}
    return {"kind": _kind_name(spec.get("kind")), "params": params}


def missing_params(kind: Any, params: Any) -> list[str]:
    """这个图形的必填参数还缺哪些(缺了就不画)。"""
    name = whitelist_name(kind) or _kind_name(kind)
    if name not in BUILDERS:
        return []
    data = params if isinstance(params, dict) else {}
    return [key for key in REQUIRED_PARAMS.get(name, ()) if data.get(key) in (None, "")]


def render_spec(spec: Any) -> tuple[str, str]:
    """图形规格 → (svg, 错误)。**任何不认识/不合法的规格都返回错误,不回退乱画。**"""
    if not isinstance(spec, dict):
        return "", "没有图形规格"
    kind = _kind_name(spec.get("kind"))
    if not kind:
        return "", "图形规格里没有 kind"
    builder = BUILDERS.get(kind)
    if builder is None:
        return "", "不支持的图形种类:" + kind[:24]
    params = spec.get("params")
    if params is None:
        params = spec.get("参数") or {}
    if not isinstance(params, dict):
        return "", "params 必须是对象"
    missing = missing_params(kind, params)
    if missing:
        return "", "参数不全(缺 " + "、".join(missing) + "),已退回照片裁剪"
    try:
        svg = builder(params)
    except SpecError as exc:
        return "", str(exc)
    except Exception as exc:  # noqa: BLE001 - 画不出来就退回裁剪,不能让出卷挂掉
        return "", "画图失败:" + type(exc).__name__ + " " + str(exc)[:80]
    if "<svg" not in svg or "http://www.w3.org/2000/svg" not in svg:
        return "", "渲染结果不是 SVG"
    for bad in ("http://", "https://", "<script", "xlink:href"):
        if bad in svg.replace("http://www.w3.org/2000/svg", ""):
            return "", "SVG 里有不该出现的东西:" + bad
    return svg, ""


__all__ = ["CATALOG", "SpecError", "pick_spec", "render_spec", "supported_kinds"]
