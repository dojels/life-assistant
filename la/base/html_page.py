r"""网页能力(M13):html_page —— 生成/修改**可预览、可交互的自包含网页**。

实现方式照另一个本地项目里的 skills/html-page(它已经在真机上被反复折磨过;路径按要求不写进本仓库 —— Windows 的转义序列还会让 Python 发 SyntaxWarning)。
三条照搬的结论,每条都有实测代价,别凭直觉改回去:

  1. **改页面走 patch,不许整页重画。** 云朵实测:同一处小改动,整页重做比最小片段替换
     **贵 2.9 倍、慢 3.2 倍**(输出 token 6.9 倍);四版之间 25 个 class 换了 19 个,
     用户看到的是"变了个风格"而不是"只改了那一处"。所以本会话里对**同一标题**再 create
     会被拒绝 —— 拒绝文案里直接给出 patch 的正确顺序(先 source 取回源码,再最小替换)。
  2. **预览是沙箱化的**(见 web/app.py 的 _HTML_PREVIEW_CSP:响应头 sandbox allow-scripts
     + connect-src 'none')。所以页面必须**自包含**:外链 CDN / localStorage / fetch
     在预览里一律不工作 —— 落盘前 lint() 自检这几类并如实回报,而不是等用户打开看到白屏。
  3. **图片由服务端内联。** 预览 CSP 只放行 data: / blob:,而模型**拿不到图片字节**
     (让它自己贴 base64 等于把字节搬进 token 流 —— 云朵为此实测烧掉 17 次工具调用)。
     所以页面里只写路径/句柄,写盘前由 inline_local_images() 换成 data: URI,
     页面因此真正自包含(下载到手机、断网也能看)。

本模块只做**确定性的文本处理**:不解析 DOM、不执行页面里的任何代码、不联网。
基础层不 import 域 —— 产物/素材都按**依赖注入**传进来(与 base/tools.py 同一条纪律)。

⭐ D140 起这里还有第二个工具 **page_view**:把生成过的网页用无头 Chrome 渲染成截图,
再让视觉模型看一眼(引擎在 base/shot.py)。它补的是"写出来的东西自己没法验收" ——
版面对不对、有没有白屏,以前只能问用户。网页预览与**截图**共用同一串 CSP
(HTML_PREVIEW_CSP,全项目只有这一份),两边看到的是同一个沙箱。
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

from .. import config
from ..base.phases import CATEGORY_TASK
from ..llm.vision import describe_image
from ..ops.paths import DEFAULT_POLICY, PathNotAllowed
from ..tools.registry import RISK_LOW, ToolSpec
from .files import BaseToolError, human_size
from .images import IMAGE_MEDIA_TYPES
from .sniff import sniff_media_type

#: 与 tools/task_tools.py 的 ANY_PHASE、base/tools.py 的 PHASE_ANY 同一口径。
PHASE_ANY = "any"

#: 产物表里网页的 kind。与 domain/artifacts.py::kind_for(".html") 同一个值 ——
#: 基础层不 import 域,所以这里是一个**字符串常量**,两处都改才算改对(有测试盯着)。
PAGE_KIND = "html"

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
#: ⭐ M14d:音频也内联。听力题**没有声音就等于没有这道题** —— 而页面里的 <audio>
#:    一旦指向本地文件,发到微信(单文件)或放进沙箱预览都是**哑的**。
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".ogg", ".aac", ".opus")
AUDIO_MEDIA_TYPES = (
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/wave",
    "audio/mp4", "audio/m4a", "audio/x-m4a", "audio/ogg", "audio/aac", "audio/webm",
)
#: 没有扩展名的**句柄**(/artifacts/<id>、/materials/<n>/file):先解析,再按真实类型判
#: ⚠️ M14f:/assets/<素材 id> 也算句柄 —— 错题的照片与裁出来的图形**存在素材层**,
#:    而它同样没有扩展名。少了这一条,模型写对句柄也内联不了(见 D110)。
_HANDLE_RE = re.compile(
    r"/?(?:artifacts/[A-Za-z0-9_\-]{4,40}|materials/\d{1,12}/file|assets/\d{1,12})$"
)

_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/]")
#: 带 scheme 的一律不当本地路径(data:/blob:/http(s):/file: 全在内 ——
#: file: 尤其不碰:预览页若能引用 file://,就能把本机文件画进画面)。
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_EXT_RE = re.compile(r"\.([A-Za-z0-9]+)$")

# <img ... src="X"> —— 只在标签内匹配,且不跨 >(避免把整段 HTML 吞掉)
_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc\s*=\s*)(["\'])([^"\']+)(\2)', re.I)
# CSS url(X):style 属性与 <style> 里的背景图
_CSS_URL_RE = re.compile(r'(url\(\s*)(["\']?)([^"\'\s)]+)(\2)(\s*\))', re.I)
# <audio src="X"> 与音频里的 <source src="X"> —— 听力题就靠这两个
_AUDIO_SRC_RE = re.compile(r'(<audio\b[^>]*?\bsrc\s*=\s*)(["\'])([^"\']+)(\2)', re.I)
_SOURCE_SRC_RE = re.compile(r'(<source\b[^>]*?\bsrc\s*=\s*)(["\'])([^"\']+)(\2)', re.I)
# 内联图片的 data: URI(回读源码时折叠它,patch 时再还原 —— 照云朵 source_view 的口径)。
# ⚠️ 只折**base64 载荷**,`data:image/png;base64,` 这个前缀原样留着:模型因此看得见
#    "这里原本是一张 png",而不是一个没头没尾的记号。
_DATA_URI_RE = re.compile(r"(data:[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+;base64,)([A-Za-z0-9+/=]+)")
_PLACEHOLDER_RE = re.compile(r"ELIDED_BASE64_\d+_of_\d+_chars")

#: 自检:沙箱里做不到的事,早说比白屏好(四条规则照云朵 html-page._LINT_RULES)。
_LINT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r'(?:src|href)\s*=\s*["\'](?:https?:)?//', re.I),
        "引用了**外部资源**(CDN/外链):预览沙箱里 default-src 'none',这些不会加载"
        " → 改成内联,或本地图片写路径(服务端会自动内联)",
    ),
    (
        re.compile(r"\b(?:localStorage|sessionStorage|indexedDB|document\.cookie)\b"),
        "用了 localStorage/cookie 这类存储:预览是**不透明源**,访问会抛异常(页面白屏)"
        " → 交互状态请放内存变量",
    ),
    # ⚠️ 别在 fetch\s*\( 后面加 \b:括号与引号之间没有词边界,那样写会**漏报**(云朵实测踩过)
    (
        re.compile(r"(?:fetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon\s*\()"),
        "页面要访问网络:沙箱是 connect-src 'none',请求发不出去 → 交互请全部在本地完成",
    ),
    (
        re.compile(r"(?:window\.top|window\.parent|top\.location|parent\.document)"),
        "要直接读写父页面/顶层窗口:沙箱下拿不到父页面 DOM → 不需要,直接操作自己这一页",
    ),
)

#: 生成网页的**沙箱预览**响应头 —— ⭐ 全项目**唯一一份**定义:
#:   * web/app.py 的 /generated/<id> 路由用它(用户点『预览』看到的就是它);
#:   * base/shot.py 的截图服务也用它(page_view 让模型看到的就是它)。
#: 两者必须是同一串,否则"我看过了没问题"会变成一句假话 —— 那是两个不同的沙箱。
#:
#: 缺一不可(照 D:\云朵辅助培训智能体 app/main.py::_HTML_PREVIEW_CSP):
#:   ① sandbox allow-scripts —— 文档被强制放进**不透明源**:拿不到 cookie/localStorage、
#:      够不到父页面;关键是**不给 allow-same-origin**。写成响应头(不是只靠 iframe 属性),
#:      用户直接在新标签打开这个地址也照样被沙箱化。
#:   ② connect-src 'none' —— 页面里的 fetch/XHR/WebSocket/beacon 一律发不出去(防外传);
#:      default-src 'none' + 只放行 data:/blob: 的图与内联样式脚本 → 自包含页面够用,外链不加载。
HTML_PREVIEW_CSP = (
    "sandbox allow-scripts; "
    "default-src 'none'; "
    "img-src data: blob:; "
    "media-src data: blob:; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline' 'unsafe-eval'; "
    "font-src data:; "
    "connect-src 'none'; "
    "form-action 'none'; base-uri 'none'"
)

_STAMP_RE = re.compile(r"^\d{8}-\d{6}-")
_VERSION_RE = re.compile(r"-v\d+$", re.I)
_LEADING_PAGE_RE = re.compile(r"^(?:page_)+", re.I)


# ==========================================================================
# 纯函数:拼装 / 自检 / 折叠 / 最小替换
# ==========================================================================
def ensure_document(html: str, title: str) -> str:
    """片段 → 完整文档;并保证有 charset 与 viewport。

    charset 漏了中文会乱码(云朵真机踩过的经典);viewport 是这个项目的**手机优先**:
    没有它,页面在手机上按 980px 宽排版,字小到看不清。
    """
    text = str(html or "").strip()
    low = text.lower()
    if "<html" not in low and not low.startswith("<!doctype"):
        return (
            '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            "<title>" + _escape(str(title or "页面")) + "</title>\n</head>\n<body>\n"
            + text + "\n</body>\n</html>\n"
        )
    if "charset" not in low:
        if re.search(r"<head[^>]*>", text, re.I):
            text = re.sub(r"(<head[^>]*>)", r'\1\n<meta charset="utf-8">', text, count=1, flags=re.I)
        else:
            text = '<meta charset="utf-8">\n' + text
    if "viewport" not in text.lower():
        meta = '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        if re.search(r"<head[^>]*>", text, re.I):
            text = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + "\n" + meta, text, count=1, flags=re.I)
        else:
            text = meta + text
    return text


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def page_title(html: str) -> str:
    """页面自报的标题(<title>)—— 没给 title 时用它当产物名。"""
    match = re.search(r"<title[^>]*>(.*?)</title>", str(html or ""), re.S | re.I)
    if not match:
        return ""
    from html import unescape

    return unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()[:60]


def lint(html: str) -> list[str]:
    """自检:返回人话提示(沙箱里做不到的事)。命中一处报一条,带命中片段。"""
    out: list[str] = []
    for pattern, message in _LINT_RULES:
        found = pattern.search(str(html or ""))
        if found:
            out.append(message + "(命中:" + found.group(0)[:40] + ")")
    return out


def clean_stem(raw: str) -> str:
    """模型给的 filename/title → 干净主干。

    产物真名长这样:20261012-190500-page_周报-v2.html(时间戳与版本序号由产物层/本模块生成)。
    模型下一轮往往把这个真名原样传回来当 filename,所以这里要把**时间戳 / page_ 前缀 /
    -vN 序号 / 扩展名**都剥掉,否则主干会一轮一轮越叠越长(云朵为此专门修过一次)。
    """
    text = str(raw or "").strip()
    text = re.sub(r"\.html?$", "", text, flags=re.I)
    text = _STAMP_RE.sub("", text)
    text = _LEADING_PAGE_RE.sub("", text)
    text = _VERSION_RE.sub("", text)
    return text.strip()


def looks_local(ref: str) -> bool:
    """这个引用是不是"本地图片"的形态(不是就不该动它)。"""
    raw = unquote(str(ref or "").strip())
    if not raw:
        return False
    if raw.startswith("//"):
        return False
    if _DRIVE_RE.match(raw) or raw.startswith("\\\\"):
        return True  # Windows 绝对路径(别被 : 误判成 scheme)
    if _SCHEME_RE.match(raw):
        return False  # data:/blob:/http(s):/file: 等
    return True  # 相对 / 根相对


def _ext_of(ref: str) -> str:
    match = _EXT_RE.search(str(ref or "").split("?")[0].split("#")[0])
    return ("." + match.group(1).lower()) if match else ""


def fold_data_uris(text: str) -> tuple[str, dict[str, str]]:
    """把 data:image/...;base64,** 折成占位符。返回 (折叠后的文本, 占位符 → 原文)。

    ⭐ 为什么要折叠:页面里的内联图片一张就是几十 KB 的 base64。回读源码给模型看时,
    那些字节既占满上下文又毫无信息量。折叠之后模型能逐字复制要改的片段,
    patch 时再按同一张表还原(**占位符永远不会被写进产物**)。
    """
    table: dict[str, str] = {}

    def _sub(match: re.Match[str]) -> str:
        payload = match.group(2)
        placeholder = "ELIDED_BASE64_" + str(len(table) + 1) + "_of_" + str(len(payload)) + "_chars"
        table[placeholder] = payload
        return match.group(1) + placeholder

    return _DATA_URI_RE.sub(_sub, str(text or "")), table


def restore_placeholders(text: str, table: dict[str, str]) -> tuple[str, str]:
    """把折叠占位符还原成原始字节。返回 (还原后的文本, 错误说明或空串)。

    ⚠️ 还原不了就**拒绝写入**(照云朵 source_view 的纪律):宁可报错,
    也不许把 ELIDED_BASE64_… 这种记号当正文写进产物 —— 那等于把页面里的图全删了。
    """
    out = str(text or "")
    for placeholder, original in (table or {}).items():
        out = out.replace(placeholder, original)
    left = _PLACEHOLDER_RE.search(out)
    if left:
        return out, (
            "这段内容里的 " + left.group(0) + " 对不上当前页面里的内联图片 —— "
            "占位符是回读源码时折叠图片留下的记号,**只能从 action=source 的结果里连它一起复制**,不能自己写"
        )
    return out, ""


def patch_text(text: str, old: str, new: str, *, replace_all: bool = False) -> tuple[str, str]:
    """在源码里做**最小片段替换**。返回 (新源码, 错误说明或空串)。

    两种拒绝各说清出路:没命中 → 先取回源码;命中多处 → 把范围放大到唯一。
    """
    if not old:
        return "", "old_string 是空的:要说清要替换掉的那段原文"
    hits = str(text).count(old)
    if hits == 0:
        return "", (
            "没找到 old_string 的原文 —— 先用 action=source 取回**当前**源码,"
            "再从里面逐字复制要改的那一段;不要凭印象写(上一版的结构可能已经变了)"
        )
    if hits > 1 and not replace_all:
        return "", (
            "old_string 在页面里出现了 " + str(hits) + " 次,无法确定改哪一处 —— "
            "多带上下几行让它**唯一命中**,或者传 replace_all=true 全改"
        )
    return (str(text).replace(old, new) if replace_all else str(text).replace(old, new, 1)), ""


# ==========================================================================
# 图片内联(服务端做:模型拿不到图片字节)
# ==========================================================================
#: 老产物里 media_type 可能落成了 application/octet-stream(见 sniff.py 的 M14d 注释)。
#: 这种时候按**扩展名**兜一下 —— 不然一句"它不是音频"就把听力题的音频挡在门外。
_MIME_BY_EXT = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".aac": "audio/aac", ".opus": "audio/ogg",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


def _kind_word(kind: str) -> str:
    if kind == "audio":
        return "音频"
    if kind == "probe":
        return "文件"
    return "图"


def _ref_kind(ref: str) -> str:
    """这个引用是什么:image / audio / probe(句柄,要解析后才知道)/ ""(不碰)。

    ⚠️ 句柄(`/artifacts/<id>`)**没有扩展名**,所以不能只看后缀就跳过 ——
    实测就是这个原因让"音频能不能内联"永远走不到解析那一步。
    """
    raw = unquote(str(ref or ""))
    ext = _ext_of(raw)
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if _HANDLE_RE.search(raw):
        return "probe"
    return ""


def inline_local_images(
    html: str,
    resolve: Callable[[str], tuple[bytes, str]],
    *,
    max_image_bytes: int | None = None,
    max_images: int | None = None,
    max_total_bytes: int | None = None,
    max_audio_bytes: int | None = None,
    max_audios: int | None = None,
    max_audio_total_bytes: int | None = None,
) -> tuple[str, list[str], list[str]]:
    """页面里指向本地**图片与音频**的引用 → data: URI。返回 (新 HTML, 说明, 提示)。

    ⚠️ 只认"看起来是本地引用"的写法;带 scheme 的一律不碰(http/https/data/blob/file)。
    超预算 / 找不到 / 类型不对的引用**保持原样**并写进提示 —— 绝不静默丢
    (云朵 inline_assets 的口径:宁可页面上少一张图并被明确告知,也不要悄悄换掉用户的页面)。

    ⭐ M14d:音频同样内联(听力题没有声音 = 没有这道题)。图片与音频**分开记账**:
    一张卷子既可能有几何图又可能有听力音频,共用一个额度会造出"图放得下、音频放不下"
    这种莫名其妙的失败。预览的 CSP 已经放行 `media-src data:`,所以内联即可播放。
    """
    text = str(html or "")
    per_image = int(max_image_bytes if max_image_bytes is not None else config.HTML_INLINE_MAX_IMAGE_BYTES)
    image_cap = int(max_images if max_images is not None else config.HTML_INLINE_MAX_IMAGES)
    total_cap = int(max_total_bytes if max_total_bytes is not None else config.HTML_INLINE_MAX_TOTAL_BYTES)
    per_audio = int(max_audio_bytes if max_audio_bytes is not None else config.HTML_INLINE_MAX_AUDIO_BYTES)
    audio_cap = int(max_audios if max_audios is not None else config.HTML_INLINE_MAX_AUDIOS)
    audio_total_cap = int(
        max_audio_total_bytes if max_audio_total_bytes is not None
        else config.HTML_INLINE_MAX_AUDIO_TOTAL_BYTES
    )

    refs: list[str] = []
    for pattern in (_IMG_SRC_RE, _CSS_URL_RE, _AUDIO_SRC_RE, _SOURCE_SRC_RE):
        for match in pattern.finditer(text):
            ref = match.group(3).strip()
            if ref not in refs:
                refs.append(ref)

    resolved: dict[str, str] = {}
    notes: list[str] = []
    warns: list[str] = []
    total = 0
    audio_total = 0
    image_count = 0
    audio_count = 0
    for ref in refs:
        if not looks_local(ref):
            continue  # 外链/data: 由 lint 去提示
        raw = unquote(ref)
        kind = _ref_kind(raw)
        if not kind:
            continue  # 不是图片/音频的引用不碰(它可能就是一个普通链接)
        try:
            payload, media_type = resolve(raw)
        except (BaseToolError, PathNotAllowed) as exc:
            warns.append("读不到这个" + _kind_word(kind) + ":" + ref[:60]
                         + "(" + str(exc)[:80] + ") —— 预览里不会显示,换一个或去掉它")
            continue
        except Exception as exc:  # noqa: BLE001 - 内联失败绝不能让整个页面生成失败
            warns.append("内联这个" + _kind_word(kind) + "时出错:" + ref[:60]
                         + "(" + type(exc).__name__ + ":" + str(exc)[:60] + ")")
            continue
        if not payload:
            warns.append("这个" + _kind_word(kind) + "是空的:" + ref[:60])
            continue
        if media_type in ("", "application/octet-stream"):
            # ⭐ 先**按内容**认(项目一贯口径:不信后缀),认不出再退回后缀。
            #    实测:听写生成的 mp3 在库里落成了 octet-stream(那时 sniff 还不认音频),
            #    于是"它不是音频"这句话把听力题的音频挡在了门外。
            media_type = sniff_media_type(payload) or media_type
        if media_type in ("", "application/octet-stream"):
            media_type = _MIME_BY_EXT.get(_ext_of(raw), media_type)
        if kind == "probe":
            # 句柄:解析出来了才按**真实类型**判是图还是音频
            if media_type in IMAGE_MEDIA_TYPES:
                kind = "image"
            elif media_type in AUDIO_MEDIA_TYPES:
                kind = "audio"
            else:
                warns.append("「" + ref[:60] + "」按内容看既不是图片也不是音频("
                             + str(media_type) + "),没有内联")
                continue
        elif kind == "image" and media_type not in IMAGE_MEDIA_TYPES:
            warns.append("「" + ref[:60] + "」按内容看不是能内联的图片(" + str(media_type) + "),没有内联")
            continue
        elif kind == "audio" and media_type not in AUDIO_MEDIA_TYPES:
            warns.append("「" + ref[:60] + "」按内容看不是能内联的音频(" + str(media_type)
                         + ") —— 听力题会没声音,检查一下这个文件")
            continue
        if kind == "image":
            if image_count >= image_cap:
                warns.append("这一页要内联的图超过 " + str(image_cap) + " 张:后面的图没有内联 —— 少放几张再生成")
                break
            if len(payload) > per_image:
                warns.append(
                    "图太大(" + human_size(len(payload)) + ",单张上限 " + human_size(per_image) + "):"
                    + ref[:50] + " 没有内联,预览里不会显示 —— 换张小图(截图/缩略图)更合适"
                )
                continue
            if total + len(payload) > total_cap:
                warns.append(
                    "这一页的内联图片总量超过 " + human_size(total_cap) + ":" + ref[:50]
                    + " 之后的图没有内联 —— 少放几张,或者把它们拆成另一页"
                )
                continue
            total += len(payload)
            image_count += 1
        else:
            if audio_count >= audio_cap:
                warns.append("这一页要内联的音频超过 " + str(audio_cap) + " 段:后面的没有内联 —— 少放几段再生成")
                break
            if len(payload) > per_audio:
                warns.append(
                    "音频太大(" + human_size(len(payload)) + ",单段上限 " + human_size(per_audio) + "):"
                    + ref[:50] + " 没有内联,页面里会是**哑的** —— 用 tts_audio 重新生成一段更短的"
                )
                continue
            if audio_total + len(payload) > audio_total_cap:
                warns.append(
                    "这一页的内联音频总量超过 " + human_size(audio_total_cap) + ":" + ref[:50]
                    + " 之后的音频没有内联 —— 少放几段,或者把它们拆成另一页"
                )
                continue
            audio_total += len(payload)
            audio_count += 1
        resolved[ref] = "data:" + media_type + ";base64," + base64.b64encode(payload).decode("ascii")
        notes.append(("已内联" + _kind_word(kind) + " ") + ref[:50] + "(" + human_size(len(payload)) + ")")

    if not resolved:
        return text, notes, warns

    def _sub(match: re.Match[str]) -> str:
        ref = match.group(3)
        replacement = resolved.get(ref)
        if replacement is None:
            return match.group(0)
        parts = [match.group(1), match.group(2), replacement]
        if match.re.groups >= 4:
            parts.append(match.group(4))
        if match.re.groups >= 5:
            parts.append(match.group(5))
        return "".join(parts)

    for pattern in (_IMG_SRC_RE, _CSS_URL_RE, _AUDIO_SRC_RE, _SOURCE_SRC_RE):
        text = pattern.sub(_sub, text)
    return text, notes, warns


# ==========================================================================
# 工具
# ==========================================================================
def _spec(
    name: str,
    description: str,
    handler: Callable[[dict[str, Any]], Any],
    *,
    params: dict[str, Any],
    required: tuple[str, ...] = (),
    budget: int = 800,
    timeout_ms: int = 8000,
    writes: bool = False,
    blocking: bool = False,
) -> ToolSpec:
    """与 base/tools.py::_spec 同一形状。

    ⚠️ 刻意**不 import 它**:tools.py 要 import 本模块(注册工具),反过来 import 就成环。
    这十几行是"两个模块共用同一份工具合同"的最小代价。
    """
    return ToolSpec(
        name=name,
        description=description,
        phase=PHASE_ANY,
        risk=RISK_LOW,
        timeout_ms=timeout_ms,
        handler=handler,
        output_budget_tokens=budget,
        required=required,
        category=CATEGORY_TASK,
        writes=writes,
        blocking=blocking,
        params=params,
    )


def _object(**properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


def _string(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _int(desc: str, *, minimum: int = 1, maximum: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "description": desc, "minimum": minimum}
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


HTML_PAGE_DESC = (
    "做一个**网页**给用户:自包含 HTML(CSS/JS 全内联、可带交互 —— 点选项判分、折叠、滑块、"
    "计算器、纯前端图表)。生成后有沙箱预览地址:用户在气泡「本轮产物」里点『预览』直接打开、"
    "点『下载』存到本机,发到微信也是一个文件。"
    "action=create 写新页面(默认);⭐ **改页面一律 action=patch**"
    "(先 action=source 取回源码,再给 old_string/new_string 做最小替换)。"
    "对同一标题重复 create 会被拒绝 —— 整页重画实测贵 2.9 倍、慢 3.2 倍,而且 class/CSS 会整体漂移,"
    "用户看到的是「换了个风格」而不是「只改了那一处」。"
    "⚠️ 页面跑在沙箱里:不能用 localStorage/cookie、不能联网(fetch/XHR/WebSocket)、"
    "不能引外链 CDN、碰不到父页面 —— 状态放内存变量,数据写死在页面里;"
    '中文页面带 <meta charset="utf-8">。图片:写本地路径或 /materials/<编号>/file、'
    "/artifacts/<产物id>,服务端会自动内联成 data:(**不要自己贴 base64,也不要写 http 外链**)。"
    "📷 错题的照片与图形在**素材层**,句柄是 /assets/<素材id> —— 清单/卡片里的 "
    "photo_handles、figure_handle 就是它们,直接粘,别自己找图。"
    "🔊 **音频同样会被内联**:<audio controls src=\"/artifacts/<产物id>\"></audio> —— "
    "听力题/听写题必须这么写。错题自带的音频在题库里就有(audio_handle,录入时已生成),"
    "**直接用它**;只有临时要一段新声音才调 tts_audio。"
    "写成 <audio src=\"单词.mp3\"> 这种相对路径内联不到,页面会是**哑的**。"
    "⚠️ 整份**错题卷**不要手写:用 quiz_write(action=sheet),图形/照片/音频都在里面。"
    "⭐ 只有用户明确要「网页/页面/在线小工具」时才用;要 Word/Excel/清单用 artifact_write,"
    "普通回答直接在对话里说。"
    "👀 写完之后想知道**版面对不对**(挤不挤、有没有白屏、字有没有被截断),"
    "用 **page_view** 把这一页渲染出来自己看一眼 —— 不要靠猜,也别动不动就问用户「你看看行不行」。"
)

PAGE_VIEW_DESC = (
    "**看一眼自己生成的网页**(把 html 渲染成截图,再由视觉模型读出画面)。"
    "只截本会话里生成过的网页产物,不改任何东西。"
    "⭐ 什么时候用:刚 create/patch 完一个页面、想知道版面有没有问题;"
    "用户说「你自己看看」;或者页面里有用 JS 算出来的东西、光读源码看不出效果。"
    "⚠️ 它只拍**一屏**(视口 = width×height,默认 1280×900),不是交互之后的样子"
    "(不会替你点按钮/翻页)。页面很长就**分屏看**:offset_y=0 是第一屏,再给 900 / 1800 … "
    "一屏一屏往下,**看不到的部分不算排版问题**,别把「视口装不下」当成缺陷报上来;"
    "说出来的是**视觉模型的描述**,细节可能被过滤 —— 问得越具体越准"
    "(例如 question=「标题和表格有没有重叠」),不要只问「好不好看」。"
    "⚠️ 内容与事实仍然以源码和内部数据为准:截图只能证明「画面上看起来是这样」。"
)


def build_html_tools(
    *,
    artifacts: Any = None,
    receipts: Any = None,
    receipt_domain: str = "artifact",
    materials: Any = None,
    media: Any = None,
    actor: str = "agent",
    session_id: str | None = None,
    vision_factory: Callable[[], Any] | None = None,
    vision_model: str = "",
) -> list[ToolSpec]:
    """网页工具(html_page 写、page_view 看)。依赖按需注入:没有产物层就没有这个能力(调用时如实报错)。"""

    # -- 目标页面 ---------------------------------------------------------
    def _pages(limit: int = 100, *, all_sessions: bool = False) -> list[Any]:
        """本会话的网页产物;all_sessions=True 时放宽到**所有会话**。

        ⚠️ 两种口径各有各的道理,别合并:
          * 改页面(patch/source)**必须**只看本会话 —— 改错文件是真损失;
          * 看页面(page_view)可以放宽 —— 它是只读的,而且"你上周给我做的错题卷
            长什么样"这种问题,**那一页根本不是在这个会话里生成的**(错题卷挂在
            study 会话下)。放宽之后要在结果里如实标出"它来自别的会话"。
        """
        if artifacts is None:
            return []
        try:
            rows = artifacts.list(
                session_id=None if all_sessions else session_id, limit=limit
            )
        except Exception:  # noqa: BLE001 - 查不到不该把工具打挂
            return []
        return [row for row in rows if str(getattr(row, "name", "")).lower().endswith((".html", ".htm"))]

    def _same_title(page: Any, title: str) -> bool:
        wanted = str(title or "").strip()
        if not wanted:
            return False
        label = str(getattr(page, "label", "") or "").strip()
        return label == wanted or clean_stem(label) == clean_stem(wanted)

    def _version_of(page: Any) -> int:
        meta = getattr(page, "meta", None) or {}
        try:
            version = int(meta.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        if version:
            return version
        match = re.search(r"-v(\d+)\.html?$", str(getattr(page, "name", "")), re.I)
        return int(match.group(1)) if match else 1

    def _next_version(title: str) -> int:
        """同一标题的第几版。⚠️ 必须在**写盘/登记之前**算(云朵的坑:登记后再算永远是 2)。"""
        best = 0
        for page in _pages():
            if _same_title(page, title):
                best = max(best, _version_of(page))
        return best + 1

    def _existing_page(title: str) -> Any:
        for page in _pages():  # list 是新的在前
            if _same_title(page, title):
                return page
        return None

    def _resolve_page(
        params: dict[str, Any], *, all_sessions: bool = False
    ) -> tuple[Any, str]:
        """目标页 → (产物, 兜底说明)。找不到就如实说清有哪些。

        all_sessions=True 只给**看**用(page_view);改网页永远只看本会话。
        """
        if artifacts is None:
            raise BaseToolError("这个会话没有产物层,改不了网页")
        wanted = str(
            params.get("filename") or params.get("page") or params.get("artifact_id") or ""
        ).strip()
        pages = _pages(all_sessions=all_sessions)
        if wanted:
            for page in pages:
                if wanted in (str(page.id), str(page.name)):
                    return page, ""
            stem = clean_stem(wanted)
            for page in pages:
                if clean_stem(page.name) == stem or clean_stem(getattr(page, "label", "")) == stem:
                    return page, ""
            raise BaseToolError(
                "没有找到那一页:「" + wanted + "」。本会话里有 " + str(len(pages))
                + " 个网页产物 —— 不传 filename 再调一次会用**最近一份**;要指定别的,先 action=source 看名字。"
            )
        if not pages:
            raise BaseToolError(
                "还没有生成过网页 —— 先用 html_page(action=create) 生成一个再来。"
            )
        # ⚠️ 兜底必须**点明**:否则模型只看到"在 xxx 里没找到原文",却不知道 xxx 根本不是它想改的那一份
        # ⭐ 看的时候(跨会话)还要再偏一层:优先**本会话**最近一份,本会话没有才退回全局最近的
        if all_sessions:
            mine = _pages()
            if mine:
                return mine[0], "(本次是按**兜底**选的最近一份:" + str(mine[0].name) + ")"
        return pages[0], "(本次是按**兜底**选的最近一份:" + str(pages[0].name) + ")"

    # -- 图片 -------------------------------------------------------------
    def _find_by_name(name: str) -> Path | None:
        """按文件名在素材区/产物区里找一个。有界:深度 ≤3、最多看 400 个文件。"""
        seen = 0
        for root in (Path(config.MEDIA_DIR), Path(config.ARTIFACTS_DIR)):
            if not root.is_dir():
                continue
            direct = root / name
            if direct.is_file():
                return direct
            for dirpath, dirnames, filenames in os.walk(root):
                if str(dirpath)[len(str(root)):].count(os.sep) >= 3:
                    dirnames[:] = []
                    continue
                for filename in filenames:
                    seen += 1
                    if seen > 400:
                        return None
                    if filename.lower() == name.lower():
                        return Path(dirpath) / filename
        return None

    def _resolve_image(ref: str) -> tuple[bytes, str]:
        """页面里的一个本地图片引用 → (字节, media_type)。

        ⭐ 认四种写法,其余一律拒绝 —— **绝不返回白名单之外的路径**:
          ① 句柄:/artifacts/<产物id>、/materials/<素材编号>/file、/assets/<素材id>;
          ② 白名单内的路径(BASE_DIR / DATA_DIR 之下,DEFAULT_POLICY 是唯一判据);
          ③ 只有一个文件名 → 在素材区/产物区里按名字找(有界)。
        ⚠️ /assets/<id> 是**素材层**的图片(错题照片、裁出来的图形)。少了这一条,
        模型把句柄写对了也内联不了 —— 页面发出去就是一张破图(D110 实测)。
        """
        text = str(ref or "").strip()
        handle = re.fullmatch(r"/?artifacts/([A-Za-z0-9_\-]{4,40})", text)
        if handle and artifacts is not None:
            artifact = artifacts.get(handle.group(1))
            payload = artifacts.read_bytes(artifact)
            return payload, str(artifact.media_type or sniff_media_type(payload))
        handle = re.fullmatch(r"/?assets/(\d{1,12})", text)
        if handle and media is not None:
            asset_id = int(handle.group(1))
            payload = media.read_bytes(asset_id)
            asset = media.get(asset_id)
            return payload, str(getattr(asset, "media_type", "") or sniff_media_type(payload))
        handle = re.fullmatch(r"/?materials/(\d{1,12})/file", text)
        if handle and materials is not None:
            material = materials.get(int(handle.group(1)))
            if material is None:
                raise BaseToolError("没有这个素材:#" + handle.group(1))
            payload = materials.read_bytes(material)
            return payload, str(getattr(material, "media_type", "") or sniff_media_type(payload))
        if Path(text).is_absolute() or "/" in text or "\\" in text:
            resolved = DEFAULT_POLICY.check(text, must_exist=True)  # 白名单先过(这是真攻击面)
            if not resolved.is_file():
                raise BaseToolError("这不是一个文件:" + text)
            payload = resolved.read_bytes()
            return payload, sniff_media_type(payload)
        found = _find_by_name(text)
        if found is None:
            raise BaseToolError(
                "在素材区/产物区里找不到这张图:" + text
                + "(写清路径,或者用 /materials/<素材编号>/file、/artifacts/<产物id>)"
            )
        payload = found.read_bytes()
        return payload, sniff_media_type(payload)

    # -- 落盘 + 登记(create / patch 共用;只此一处写盘)---------------------
    def _store(
        text: str, *, title: str, params: dict[str, Any], existed: bool, note_head: str,
    ) -> dict[str, Any]:
        version = _next_version(title)
        text, inlined, image_warns = inline_local_images(text, _resolve_image)
        warns = lint(text)
        stem = clean_stem(str(params.get("filename") or "")) or clean_stem(title) or "page"
        name = "page_" + stem + "-v" + str(version) + ".html"
        artifact = artifacts.register_text(
            text,
            filename=name,
            label=title,
            session_id=session_id,
            # ⚠️ kind 必须显式给:register_text 的默认是 KIND_TEXT,不给的话网页会被登记成
            #    普通文本产物(「素材」页就不会给『预览』入口了)。
            kind=PAGE_KIND,
            meta={"source": actor, "title": title, "version": version, "generator": "html_page"},
        )
        preview_url = "/generated/" + str(artifact.id)
        # 交给用户的**文件显示名**:不带产物层那串时间戳前缀,但版本号要留着 ——
        # 微信/下载拿到的是这个名字(见 bridge._outbound_files:回执里的 name 才是唯一可信的出处),
        # 用户因此能一眼看出"这是第 2 版",旧版也仍然能打开对照。
        deliver_name = stem + ("" if version <= 1 else "-v" + str(version)) + ".html"
        receipt_id = 0
        if receipts is not None:
            # 与领域写入同一条纪律:写要留痕 + 可一键撤销(M7)。撤销 = 删掉这一版文件
            # (上一版原样还在,所以"退回上一版"永远是打开旧的那份产物 —— 见 domain/reverters.py)。
            receipt_id = receipts.record(
                kind="artifact.file",
                domain=receipt_domain,
                label=("网页:" if not existed else "网页改版:") + title,
                detail={
                    "artifact_id": artifact.id,
                    "name": deliver_name,
                    "bytes": artifact.byte_size,
                    "preview_url": preview_url,
                },
                undo_plan={
                    "type": "artifact",
                    "artifact_id": artifact.id,
                    "name": deliver_name,
                    "existed": False,
                    "path": str(artifacts.path_of(artifact)),
                },
                actor=actor,
                session_id=session_id,
            )
            if receipt_id:
                receipts.conn.commit()

        lines = [
            note_head + "网页「" + title + "」:" + artifact.name
            + "(" + human_size(artifact.byte_size) + "," + (("第 " + str(version) + " 版") if existed else "新版") + ")",
            "预览(沙箱内运行,可以直接交互):" + preview_url,
            "下载:" + "/artifacts/" + str(artifact.id),
            "改这一页(**不要整页重写**):html_page(action=source, filename=" + artifact.name
            + ") 取回源码 → html_page(action=patch, filename=" + artifact.name
            + ", old_string=…, new_string=…);本会话里对同一标题再 create 会被拒绝。",
        ]
        if inlined:
            lines.append("图片:" + ";".join(inlined))
        if warns:
            lines.extend(["", "自检提示(" + str(len(warns)) + " 条,沙箱里做不到的事):"])
            lines.extend("- " + item for item in warns)
            lines.append("请按提示改成自包含页面;不改的话用户预览时那部分不生效。")
        else:
            lines.append("自检通过:页面自包含,没发现沙箱不支持的能力。")
        if image_warns:
            lines.extend(["", "图片提示(" + str(len(image_warns)) + " 条):"])
            lines.extend("- " + item for item in image_warns)
        lines.append(
            "告诉用户:在气泡底部的「本轮产物」条里点『预览』就能打开、点『下载』存到本机"
            "(刷新之后这两条在页面顶部「最近的写入回执」里同样有)。"
        )
        return {
            "artifact_id": artifact.id,
            "name": artifact.name,
            "label": title,
            "version": version,
            "bytes": artifact.byte_size,
            "file_path": str(artifacts.path_of(artifact)),
            "preview_url": preview_url,
            "download_url": "/artifacts/" + str(artifact.id),
            "receipt_id": receipt_id,
            "lint": warns,
            "note": "\n".join(lines),
        }

    def _rewrite_refusal(page: Any) -> str:
        """拒绝"整页重画"时给模型的**出路**(只说"不行"等于把用户卡住)。

        ⚠️ 两条硬约束,改文案时别踩:
          ① **工具失败的文案会被 registry 截到 200 字符**(见 tools/registry.py 的 CODE_HANDLER_ERROR)
             —— 所以出路写在最前面,而且用**产物 id**(13 字符)而不是那种 30+ 字符的文件名;
          ② 只拒绝、不给下一步 = 把用户卡住(云朵的 _rewrite_refusal 就是为这件事写的)。
        """
        label = str(getattr(page, "label", "") or getattr(page, "name", ""))
        return (
            "「" + label + "」已经生成过 —— **不要用 create 重画**(整页重写贵 2.9 倍、慢 3.2 倍,"
            "class/CSS 还会整体漂移)。改法:① action=source filename=" + str(getattr(page, "id", ""))
            + " 取回源码;② action=patch 只改那一处。真要整页重做,换个新 title。"
        )

    # -- 三个动作 ---------------------------------------------------------
    def _create(params: dict[str, Any]) -> dict[str, Any]:
        if artifacts is None:
            raise BaseToolError("这个会话没有产物层,生成不了网页")
        html = str(params.get("html") or "").strip()
        if not html:
            raise BaseToolError(
                "没有收到 HTML 源码(html 参数是空的):把**完整页面源码**放进 html(含 <!doctype html>)"
            )
        size = len(html.encode("utf-8"))
        if size > config.HTML_PAGE_MAX_BYTES:
            raise BaseToolError(
                "页面源码太大(" + human_size(size) + ",上限 " + human_size(config.HTML_PAGE_MAX_BYTES)
                + "):自包含页面不需要这么大,精简一下,或者拆成两页"
            )
        title = str(params.get("title") or "").strip() or page_title(html) or "网页"
        if session_id:
            duplicate = _existing_page(title)
            if duplicate is not None:
                raise BaseToolError(_rewrite_refusal(duplicate))
        text = ensure_document(html, title)
        return _store(text, title=title, params=params, existed=False, note_head="已生成")

    def _patch(params: dict[str, Any]) -> dict[str, Any]:
        old = params.get("old_string")
        if old is None:
            old = params.get("old")
        new = params.get("new_string")
        if new is None:
            new = params.get("new")
        if old is None or str(old) == "":
            raise BaseToolError(
                "patch 需要 old_string(要替换掉的原文,逐字来自 action=source 取回的源码);"
                "只给 new_string 无法定位要改哪里"
            )
        old_text, new_text = str(old), ("" if new is None else str(new))
        page, hint = _resolve_page(params)
        path = artifacts.path_of(page)
        try:
            if path.stat().st_size > config.HTML_PAGE_MAX_PATCH_BYTES:
                raise BaseToolError(
                    "这一页太大了(>" + human_size(config.HTML_PAGE_MAX_PATCH_BYTES)
                    + "),不适合整篇读改写:把要改的那一段用 file(action=read) 读出来,再定向替换"
                )
        except OSError:
            pass
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise BaseToolError("读不出这一页的源码(" + type(exc).__name__ + "),改不了:" + str(exc)[:100]) from exc
        # 折叠一次只为拿到那张"占位符 → 真实字节"的表:模型从 action=source 抄回来的 old_string
        # 里可能带着 ELIDED_BASE64_…,替换前要把它还原成真实字节 —— 然后**在原始源码里**定位
        # (照云朵 source_view.restore_for_write 的口径:匹配发生在原字节上,不是折叠文本上)。
        _folded, table = fold_data_uris(current)
        old_text, error = restore_placeholders(old_text, table)
        if error:
            raise BaseToolError(error + " " + hint)
        new_text, error = restore_placeholders(new_text, table)
        if error:
            raise BaseToolError(error + " " + hint)
        patched, error = patch_text(current, old_text, new_text, replace_all=bool(params.get("replace_all")))
        if error:
            raise BaseToolError(error + " " + hint)
        title = str(getattr(page, "label", "") or "").strip() or page_title(patched) or clean_stem(page.name) or "网页"
        return _store(
            ensure_document(patched, title), title=title, params=params, existed=True,
            note_head="已改好(只动了 old_string 命中的那一处,其余与上一版逐字节相同)",
        )

    def _source(params: dict[str, Any]) -> dict[str, Any]:
        page, hint = _resolve_page(params)
        path = artifacts.path_of(page)
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise BaseToolError("读不出这一页的源码(" + type(exc).__name__ + "):" + str(exc)[:100]) from exc
        folded, table = fold_data_uris(current)
        clipped = ""
        if len(folded) > config.HTML_SOURCE_MAX_CHARS:
            clipped = (
                " ⚠️ 源码太长,只给了前 " + str(config.HTML_SOURCE_MAX_CHARS) + " 个字符:"
                "要改后面的部分,用 file 直接读这个文件(" + str(path) + ")"
            )
            folded = folded[: config.HTML_SOURCE_MAX_CHARS]
        return {
            "name": str(page.name),
            "artifact_id": str(page.id),
            "file_path": str(path),
            "source": folded,
            "folded_images": len(table),
            "note": (
                "这是这一页**当前**的源码,可以直接从里面逐字复制 old_string。"
                "源码里的 ELIDED_BASE64_… 是内联图片折叠后的记号:可以连它一起复制进 "
                "old_string/new_string(落盘时会自动还原),**不要自己改它**。" + clipped + " " + hint
            ).strip(),
        }

    # -- 看:把自己生成的网页渲染成截图,再让视觉模型看一眼 ----------------
    async def _page_view(params: dict[str, Any]) -> dict[str, Any]:
        """网页 → 无头 Chrome 截图 → 视觉模型描述。

        ⭐ 为什么要它(用户 2026-10-17:*"比如自己生成的 html,用户可以让它自己看了找问题"*):
        页面以前只有**源码**可看 —— 版面对不对、有没有挤在一起、白屏没有,它一概不知道,
        只能问用户"你看看行不行"。现在它自己能看一眼再说话。

        ⚠️ 两条诚实边界(文案里必须带上,别让模型把描述当成源码级结论):
          ① 看到的是**渲染后的第一屏**,不是交互之后的状态(不会替用户点按钮/翻页);
          ② 说出来的是**视觉模型的描述**,细节可能被过滤 —— 所以 question 越具体越准。
        """
        from . import shot as shot_mod  # 延迟 import:别让 import 期就把浏览器那套拖进来

        page, hint = _resolve_page(params, all_sessions=True)
        path = artifacts.path_of(page)
        try:
            html = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise BaseToolError(
                "读不出这一页的内容(" + type(exc).__name__ + "),截不了图:" + str(exc)[:80]
            ) from exc
        # ⚠️ 放宽到所有会话之后,必须**说清它从哪来** —— 用户(和模型自己)才知道
        #    看的是"这一轮刚做的"还是"上周在别处做的"。
        owner = str(getattr(page, "session_id", "") or "")
        if owner and session_id and owner != session_id:
            hint = (hint + " " if hint else "") + "(这一页是在会话「" + owner + "」里生成的)"
        try:
            offset = int(params.get("offset_y") or 0)
        except (TypeError, ValueError):
            offset = 0
        offset = max(0, min(20_000, offset))
        try:
            shot = await asyncio.to_thread(
                shot_mod.render_html, html,
                width=params.get("width") or 0, height=params.get("height") or 0,
                offset_y=offset,
            )
        except shot_mod.ShotError as exc:
            # ⚠️ 措辞要防一件事:截不出来**不代表页面有问题**。模型很容易把"我没看到"
            #    说成"页面是空的/坏了",所以这句必须说清。
            raise BaseToolError(
                "没截成图:" + exc.message + "(这只说明我没看到画面,**不代表页面有问题**)"
            ) from exc

        question = str(params.get("question") or "").strip()
        if not question:
            question = (
                "这是网页渲染出来的画面。请照实说:版面有没有问题"
                "(挤在一起、错位、横向溢出、留白过大、乱码、白屏)?最该改哪里?"
                "不要评价内容写得好不好,只说你看得见的。"
                "⚠️ 页面比视口长、下面还有内容看不到 —— 这**不算问题**(那是截图范围的限制,"
                "不是排版错误),不要把它当成缺陷报上来;确实想往下看,就加大 height 再截一次。"
            )
        if vision_factory is None:
            raise BaseToolError(
                "截图已经生成(在 " + str(shot.path) + "),**但现在没有可用的视觉模型,我看不了它**;"
                "要看源码用 html_page(action=source)"
            )
        if vision_model and vision_model not in config.IMAGE_INPUT_MODELS:
            raise BaseToolError(
                "当前模型 " + vision_model + " 没有声明支持图片输入,我看不了截图;"
                "截图已生成在 " + str(shot.path)
            )
        description = await describe_image(
            vision_factory(), shot.data, "image/png",
            question=question, model=vision_model or None,
        )
        label = str(getattr(page, "label", "") or getattr(page, "name", ""))
        head = (
            "【网页截图 " + label + " · " + str(shot.width) + "×" + str(shot.height)
            + (" · 从 y=" + str(offset) + " 开始的一屏" if offset else "")
            + " · " + str(shot.kb) + "KB · " + str(shot.elapsed) + "s】"
        )
        tail = (
            "\n\n(这是**渲染出来的画面**,是视觉模型描述的 —— 不是源码级结论;"
            + ("这一屏是把页面上移 " + str(offset) + "px 拍的,固定定位的元素位置会与真实滚动不同;"
               if offset else "")
            + "要看别的部分就把 offset_y 换成别处(一屏 height 像素),要更细就带上 question 再截一次。"
            "截图文件:" + str(shot.path) + ")"
        )
        if hint:
            tail += "\n" + hint
        return {
            "note": head + "\n" + description + tail,
            "page": str(getattr(page, "name", "")),
            "artifact_id": str(getattr(page, "id", "")),
            "width": shot.width,
            "height": shot.height,
            "bytes": len(shot.data),
            "elapsed": shot.elapsed,
            "path": str(shot.path),
        }

    async def html_page(params: dict[str, Any]) -> dict[str, Any]:
        action = str(params.get("action") or "create").strip().lower()
        if action in ("patch", "edit", "apply"):
            return _patch(params)
        if action in ("source", "read", "cat"):
            return _source(params)
        return _create(params)

    return [
        _spec(
            "html_page", HTML_PAGE_DESC, html_page,
            params=_object(
                action=_string("create=生成新网页(默认);patch=在已生成的网页上做最小片段替换;source=取回当前源码(改之前先取)"),
                title=_string("网页标题(也进文件名)。用户没给就用内容主题,别留空"),
                html=_string("完整 HTML 源码(含 <!doctype html>),只在 action=create 用"),
                filename=_string(
                    "要改/要取源码的那一页:文件名或产物 id(preview 地址里 /generated/<id> 的那串)。"
                    "action=patch/source 时强烈建议显式传 —— 不传就按「本会话最近一份网页产物」兜底,可能改错文件"
                ),
                old_string=_string(
                    "只在 action=patch 用:要被替换掉的原文,逐字来自 action=source 的源码,必须能唯一命中"
                    "(多带上下几行)。凭印象写必然报没找到"
                ),
                new_string=_string("只在 action=patch 用:替换成的新内容(要删掉这段就给空字符串)"),
                replace_all={"type": "boolean", "description": "只在 action=patch 用:old_string 命中多处时是否全改(默认 false)"},
            ),
            required=(),
            budget=6000, timeout_ms=30000, writes=True, blocking=True,
        ),
        # ⭐ D140:看自己写的页面。它是**只读**的(不动产物、不动素材),所以 writes=False。
        #    timeout 给到 60 秒:Chrome 冷启动 + 页面渲染实测 2~5 秒,留足余量;
        #    blocking=True 是因为浏览器那一段跑在线程里的同步子进程 —— 到点抢不动它
        #    (内部自己按 PID 杀整棵进程树,见 base/shot.py)。
        _spec(
            "page_view", PAGE_VIEW_DESC, _page_view,
            params=_object(
                filename=_string(
                    "看哪一页:产物 id 或文件名(page_* 那个)。留空 = 本会话**最近一份**网页"
                ),
                question=_string(
                    "你想从这张截图里知道什么(可选,例如「标题和表格有没有重叠」)。"
                    "不给就照实描述版面问题"
                ),
                width=_int("视口宽(CSS 像素,默认 1280)", minimum=320, maximum=2400),
                height=_int("视口高(CSS 像素,默认 900)", minimum=320, maximum=2400),
                offset_y=_int(
                    "从页面顶部往下第几像素开始看(默认 0 = 第一屏)。"
                    "页面比一屏长就分屏看:900、1800 …… 每次加一个 height",
                    minimum=0, maximum=20000,
                ),
            ),
            required=(),
            budget=1200, timeout_ms=60000, writes=False, blocking=True,
        ),
    ]


__all__ = [
    "HTML_PREVIEW_CSP",
    "build_html_tools",
    "clean_stem",
    "ensure_document",
    "fold_data_uris",
    "inline_local_images",
    "lint",
    "looks_local",
    "page_title",
    "patch_text",
    "restore_placeholders",
    "HTML_PAGE_DESC",
    "PAGE_KIND",
]
