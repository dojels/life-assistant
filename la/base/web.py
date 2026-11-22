"""联网:web(action=search 检索 / action=fetch 抓正文)—— M12,后端在 D148 换成官方接口。

三条定死的规矩:

  1. **默认关**(`LA_WEB_ENABLED=0`)。联网 = 数据出境:检索词会发给搜索服务。
     只有用户明确打开才联网;开了之后也**只发用户原话里的检索词** ——
     账本/健康/错题/素材里的本地数据绝不自动拼进 query。
  2. **抓取要过闸**(两条路各有各的闸,详见下面 D148 那段):
     本机 httpx 走 SSRF 闸 —— scheme 只允许 http/https;解析出的**每个 IP** 都不许是
     内网/本机/链路本地/保留/组播;重定向最多 5 跳且**只允许同源**。
     没有这道闸,一句"帮我看看 http://127.0.0.1:8765/..." 
     就能把本机服务的内容读给模型。
  3. **外部内容按资料处理**:所有返回前面加一行提示(照 DSH
     EXTERNAL_WEB_CONTENT_NOTICE 的口径)。提示词里也写了同一条 ——
     两层都要有,只靠提示词挡不住注入。

⭐ D148 的两个后端(都走 DeepSeek 官方,同一个 key):
  * `POST /v1/tools/search` —— 自研搜索引擎,**不烧 token**,按成功请求计费
    (5 元 / 1000 次),每条结果给 title/url/snippet/summary/date/authority;
  * `POST /v1/tools/fetch`  —— 返回 **Markdown 正文**,反爬/编码/正文提取由对方处理。

  ⚠️ 抓取的两套后端两套闸,别混:本机 httpx(`LA_WEB_FETCH_PROVIDER=local`)走 **SSRF 闸**
  (解析出的每个 IP 都不许是内网);官方 Fetch 走**形态闸** —— 内网地址与带凭据的链接
  **不发给第三方**(对方本来也到不了我们的内网,挡的是"别把内网地址白送出去")。

  ⚠️ 旧路子(2026-10-11 起)是把搜索**塞进一次对话**(见下面这段实测记录)。换掉它的理由是
  "更便宜 + 信息更多",不是"旧的是错的":一次 query 烧 4888 输入 + 616 输出 token(约 0.014 元),
  而官方接口只按次收 0.005 元、还多给摘要。本机 fetch 那条路**留着**,因为它是唯一
  **不把链接发出去**的选项。

以下是旧路子的实测记录(留作对照,别删 —— 它解释了为什么以前"只有标题链接"):
`POST /anthropic/v1/messages` 带 `tools=[{"type":"web_search_20250305",...}]` 会返回
`web_search_tool_result` 块,里面的条目是 {title, url, page_age}(**没有 snippet**)。
所以返回给模型的是"标题 + 链接",要看内容再 `web(action=fetch)` 打开 —— 而不是假装有摘要。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .. import config
from .extract import html_to_text

USER_AGENT = "mo-family-agent/0.1 (+local personal assistant)"

#: 能抓的内容类型(其余按二进制处理:只报类型,不硬解)
TEXTUAL_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")


class WebError(RuntimeError):
    """联网失败的统一出口。消息是给模型/用户看的。"""


@dataclass(frozen=True)
class SearchHit:
    """一条搜索结果(DeepSeek 官方 Search 的 results[i])。

    ⭐ D148:旧路子(服务端工具塞进对话)只拿得到 title/url/page_age;
    现在官方接口还带 snippet / summary / date / authority —— 摘要才是让模型少翻几次网页的关键。
    """

    title: str
    url: str
    page_age: str = ""
    snippet: str = ""
    summary: str = ""
    site_name: str = ""
    date: str = ""
    authority: int = -1

    def as_line(self) -> str:
        """给模型看的一行(标题 + 链接 + 元信息 + 摘要)。

        ⚠️ 摘要按 config.WEB_SUMMARY_RENDER_CHARS 截 —— 8 条 × 800 字会把工具预算撑爆。
        """
        bits: list[str] = []
        when = (self.date or "")[:10] or self.page_age
        if when:
            bits.append(when)
        if self.site_name:
            bits.append(self.site_name)
        if self.authority >= 0:
            bits.append("权威 " + str(self.authority) + "/4")
        line = "- [" + (self.title or self.url) + "](" + self.url + ")"
        if bits:
            line += "(" + " · ".join(bits) + ")"
        body = (self.summary or self.snippet).strip()
        if body:
            limit = int(config.WEB_SUMMARY_RENDER_CHARS or 0)
            if limit and len(body) > limit:
                body = body[:limit] + "…"
            line += "\n  " + body
        return line


@dataclass(frozen=True)
class FetchedPage:
    url: str
    status: int
    media_type: str
    text: str
    truncated: bool = False
    #: 页面标题(官方 Fetch 会带;本机抓取没有 → 空串)
    title: str = ""
    #: 这一份是谁抓的:deepseek / local。报表与说明都要如实标出来。
    provider: str = "local"


# -- SSRF 闸 ---------------------------------------------------------------
def check_public_url(url: str) -> str:
    """校验一个 URL 能不能抓。不通过就抛 WebError(**默认拒绝**)。"""
    text = str(url or "").strip()
    if not text:
        raise WebError("链接是空的")
    if len(text) > config.URL_MAX_CHARS:
        raise WebError("链接太长(上限 " + str(config.URL_MAX_CHARS) + " 字符)")
    parts = urlparse(text)
    if parts.scheme not in ("http", "https"):
        raise WebError("只支持 http/https 链接,不支持 " + (parts.scheme or "(空)"))
    host = parts.hostname or ""
    if not host:
        raise WebError("这个链接里没有主机名")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise WebError("解析不了这个域名:" + host) from exc
    for info in infos:
        raw = info[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if (address.is_loopback or address.is_link_local or address.is_private
                or address.is_reserved or address.is_multicast or address.is_unspecified):
            raise WebError("这个地址指向本机或内网(" + str(address) + "),不允许抓取")
    return text


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlparse(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, (parts.hostname or ""), port


# -- 抓网页 ---------------------------------------------------------------
async def fetch_page(
    url: str,
    *,
    timeout_ms: int = config.WEB_TIMEOUT_MS,
    max_bytes: int = config.WEB_FETCH_MAX_BYTES,
    max_chars: int = config.WEB_FETCH_MAX_CHARS,
    max_redirects: int = config.WEB_MAX_REDIRECTS,
) -> FetchedPage:
    """抓一个网页并转成文本。**流式读、按字节截断**,不把 100MB 拉进内存。"""
    current = check_public_url(url)
    origin = _origin(current)
    timeout = httpx.Timeout(timeout_ms / 1000.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _hop in range(max_redirects + 1):
            async with client.stream("GET", current, headers={"User-Agent": USER_AGENT}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location") or ""
                    if not location:
                        raise WebError("对方给了跳转但没给地址")
                    target = urljoin(current, location)
                    check_public_url(target)
                    if _origin(target) != origin:
                        raise WebError("跳转到别的站点,已拒绝:" + target)
                    current = target
                    continue
                media_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                chunks: list[bytes] = []
                size = 0
                truncated = False
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= max_bytes:
                        truncated = True
                        break
                body = b"".join(chunks)
                status = response.status_code
                break
        else:
            raise WebError("跳转次数太多(超过 " + str(max_redirects) + " 次)")

    if not any(media_type.startswith(prefix) for prefix in TEXTUAL_TYPES):
        return FetchedPage(url=current, status=status, media_type=media_type or "未知",
                           text="(这不是文本内容:" + (media_type or "未知类型") + ",没有解析)",
                           provider="local")
    raw = _decode(body)
    text = html_to_text(raw) if "html" in media_type or raw.lstrip()[:1] == "<" else raw
    cut = False
    if len(text) > max_chars:
        text = text[:max_chars]
        cut = True
    return FetchedPage(url=current, status=status, media_type=media_type or "text/plain",
                       text=text, truncated=truncated or cut, provider="local")


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


# ==========================================================================
# DeepSeek 官方 Search / Fetch(D148)
# ==========================================================================
# ⭐ 为什么换成这两个**独立接口**(2026-10-18):
#   旧路子是"把搜索塞进一次对话"—— POST /anthropic/v1/messages 带
#   tools=[{type:"web_search_20250305"}]。真机实测一次 query 就烧掉
#   **4888 输入 token + 512 缓存读 + 616 输出 token**(约 0.014 元),
#   而且只拿得到 {title,url,page_age},**没有摘要** → 模型还得逐个 fetch。
#   官方接口正好补这两个洞:
#     * 不烧 token,按**成功请求次数**计费(5 元 / 1000 次 = 0.005 元/次);
#     * Search 直接给 snippet / summary / date / authority,一次就能读个大概。
#   Fetch 则把"反爬、编码、正文提取"这些麻烦交给了对方(本机那条路留在
#   LA_WEB_FETCH_PROVIDER=local 里,作为**不把链接发出去**的选项)。
#
# ⚠️ 计费口径(官方文档原话):只有 **HTTP 200 且产生结果** 的请求才计费 ——
#    Search 的 results 必须非空,Fetch 必须返回正文;失败/无结果/超时都不计费。
#    所以"成功"这件事必须由**我们**判定,不能拿"调了一次"当"收了一次钱"。

#: 官方端点(与对话同一个 key,Bearer 鉴权)
SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"
#: 每次**成功**请求 5 元 / 1000 次 = 0.005 元。这里是钱的唯一来源(报表按它估)。
SEARCH_FEE_PER_REQUEST = 0.005
FETCH_FEE_PER_REQUEST = 0.005
#: count 的合法区间(官方 1~50)
SEARCH_MIN_COUNT = 1
SEARCH_MAX_COUNT = 50
#: Fetch 的 url 上限(官方 8192)
FETCH_URL_MAX_CHARS = 8192


async def post_json(
    url: str, *, headers: dict[str, str], payload: dict[str, Any], timeout_ms: int,
) -> tuple[int, dict[str, Any], str]:
    """POST 一个 JSON,返回 (状态码, 解析后的 JSON, 原始文本)。

    ⚠️ 单独抽出来是为了**能被测试注入**:官方接口的解析与错误映射必须是可测的,
       而且测试不该打真网(那是真花钱)。
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_ms / 1000.0)) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise WebError("联网请求失败:" + type(exc).__name__ + " " + str(exc)[:120]) from exc
    text = str(response.text or "")
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return int(response.status_code), data, text


def _error_detail(status: int, data: dict[str, Any], text: str) -> str:
    """把接口错误翻成**能照做**的一句话。"""
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    message = str(error.get("message") or "").strip()
    if status == 401:
        return "API key 无效或过期(401)"
    if status == 402:
        return "余额不足,先充值(402)"
    if status == 403:
        return "对方站点拦住了抓取(403,安全策略)"
    if status == 404:
        return "抓不到可用的网页内容(404:链接失效 / 被站点拦 / 没有可提取的正文)"
    if status == 408:
        return "读取超时(408),稍等再试"
    if status == 413:
        return "页面太大(413)"
    if status == 429:
        return "请求太频繁(429,QPS 上限 5),稍等再试"
    if status >= 500:
        return "服务端出错(" + str(status) + "),稍等再试"
    return (message or ("HTTP " + str(status) + " " + text[:120])).strip()


class DeepSeekWeb:
    """官方 Search + Fetch。一个 key、两个接口,形状见模块顶部。"""

    name = "deepseek"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_ms: int | None = None,
        poster: Any = None,
    ) -> None:
        self.api_key = str(api_key or "")
        self.base_url = str(base_url or config.DEFAULT_BASE_URL).rstrip("/")
        self.timeout_ms = int(timeout_ms or config.WEB_TIMEOUT_MS)
        self._poster = poster or post_json

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise WebError("没有 DeepSeek API key,联网用不了")
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }
        status, data, text = await self._poster(
            self.base_url + path, headers=headers, payload=payload, timeout_ms=self.timeout_ms,
        )
        if status != 200:
            raise WebError("联网接口返回 " + str(status) + ":" + _error_detail(status, data, text))
        return data

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        count = max(SEARCH_MIN_COUNT, min(SEARCH_MAX_COUNT, int(limit or config.WEB_MAX_RESULTS)))
        data = await self._post(SEARCH_PATH, {
            "query": str(query or "").strip(),
            "count": count,
            # ⭐ 不要正文:summary 已经够模型判断要不要点进去;正文让 fetch 按需去取。
            "need_content": False,
        })
        return hits_from_results(data.get("results") or [], limit=count)

    async def fetch(self, url: str) -> FetchedPage:
        target = check_fetch_url(url)
        data = await self._post(FETCH_PATH, {"url": target})
        text = str(data.get("content") or "")
        if not text.strip():
            raise WebError("抓到了网页但没有正文(对方站点可能挡了)")
        text, truncated = clip_text(text)
        return FetchedPage(
            url=target, status=200, media_type="text/markdown", text=text,
            truncated=truncated, title=str(data.get("title") or ""), provider="deepseek",
        )


def hits_from_results(results: list[Any], *, limit: int) -> list[SearchHit]:
    """官方 Search 的 results[] → SearchHit(按 url 去重)。认不出的条目**跳过**,不猜。"""
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in results or []:
        if not isinstance(item, dict):
            continue
        link = str(item.get("url") or "").strip()
        if not link or link in seen:
            continue
        seen.add(link)
        try:
            authority = int(item.get("authority"))
        except (TypeError, ValueError):
            authority = -1
        hits.append(SearchHit(
            title=str(item.get("title") or "").strip(),
            url=link,
            snippet=str(item.get("snippet") or "").strip(),
            summary=str(item.get("summary") or "").strip(),
            site_name=str(item.get("site_name") or "").strip(),
            date=str(item.get("date") or "").strip(),
            page_age=str(item.get("date") or "").strip(),
            authority=authority,
        ))
        if len(hits) >= max(1, int(limit)):
            break
    return hits


def clip_text(text: str, *, max_chars: int | None = None, max_bytes: int | None = None) -> tuple[str, bool]:
    """按字符 / 字节双重上限截断正文,返回 (文本, 是否截断)。"""
    limit_chars = int(max_chars if max_chars is not None else config.WEB_FETCH_MAX_CHARS)
    limit_bytes = int(max_bytes if max_bytes is not None else config.WEB_FETCH_MAX_BYTES)
    out = str(text or "")
    truncated = False
    if limit_chars and len(out) > limit_chars:
        out = out[:limit_chars]
        truncated = True
    while limit_bytes and len(out.encode("utf-8")) > limit_bytes:
        out = out[: max(0, len(out) - max(1024, len(out) // 10))]
        truncated = True
    return out, truncated


def check_fetch_url(url: str) -> str:
    """送给官方 Fetch 之前的**形态检查**(不做 DNS)。

    ⚠️ 与 check_public_url 的分工:
      * 本机抓取要解析 DNS、挡住一切内网地址 —— 那是保护**我们的**网络;
      * 官方抓取由对方的服务器去访问,内网地址它根本到不了;这里挡的是另一件事:
        **别把内网地址/带凭据的链接发给第三方**(既泄露信息,又白花一次请求)。
    """
    text = str(url or "").strip()
    if not text:
        raise WebError("链接是空的")
    if len(text) > min(FETCH_URL_MAX_CHARS, config.URL_MAX_CHARS):
        raise WebError("链接太长(上限 " + str(min(FETCH_URL_MAX_CHARS, config.URL_MAX_CHARS)) + " 字符)")
    parts = urlparse(text)
    if parts.scheme not in ("http", "https"):
        raise WebError("只支持 http/https 链接,不支持 " + (parts.scheme or "(空)"))
    if parts.username or parts.password:
        raise WebError("链接里带了用户名或密码,不允许发出去")
    host = (parts.hostname or "").strip()
    if not host:
        raise WebError("这个链接里没有主机名")
    lowered = host.lower().rstrip(".")
    if lowered in ("localhost",) or lowered.endswith(".local") or lowered.endswith(".internal"):
        raise WebError("这个地址指向本机或内网,不发给联网服务")
    try:
        address = ipaddress.ip_address(lowered.strip("[]"))
    except ValueError:
        address = None
    if address is not None and (
        address.is_loopback or address.is_private or address.is_link_local
        or address.is_reserved or address.is_multicast or address.is_unspecified
    ):
        raise WebError("这个地址指向本机或内网,不发给联网服务")
    return text


@dataclass
class WebBackend:
    """网页能力的装配结果。**搜/抓各自可换**,但都走同一个 key。"""

    search: Any = None
    fetch: Any = None      # None = 用本机 httpx(fetch_page)
    search_name: str = "none"
    fetch_name: str = "local"

    @property
    def label(self) -> str:
        return "搜 " + self.search_name + " / 抓 " + self.fetch_name


def build_web_backend(settings: Any) -> WebBackend:
    """按设置装配。**缺 key 就如实降级**,不假装能用。

    * search: 只有 deepseek(官方 Search);none = 不提供;
    * fetch : deepseek(官方 Fetch)/ local(本机 httpx)/ 没 key 时回落 local。
    """
    api_key = str(getattr(settings, "api_key", "") or "")
    base_url = str(getattr(settings, "base_url", config.DEFAULT_BASE_URL))
    search_choice = str(getattr(settings, "web_search_provider", "") or "").strip().lower()
    fetch_choice = str(getattr(settings, "web_fetch_provider", "") or "").strip().lower()

    backend = WebBackend()
    if search_choice in ("deepseek",) and api_key:
        backend.search = DeepSeekWeb(api_key=api_key, base_url=base_url)
        backend.search_name = "deepseek"
    if fetch_choice in ("deepseek",) and api_key:
        backend.fetch = DeepSeekWeb(api_key=api_key, base_url=base_url)
        backend.fetch_name = "deepseek"
    elif fetch_choice in ("deepseek", "local", ""):
        if fetch_choice == "deepseek":
            # 配了官方但没有 key:回落本机,**并且名字照实写成 local(no key)**
            backend.fetch_name = "local(no key)"
        else:
            backend.fetch_name = "local"
    else:
        backend.fetch_name = "local"
    return backend


__all__ = [
    "WebError", "SearchHit", "FetchedPage", "WebBackend",
    "check_public_url", "check_fetch_url", "clip_text", "fetch_page", "hits_from_results",
    "DeepSeekWeb", "build_web_backend", "post_json",
    "SEARCH_FEE_PER_REQUEST", "FETCH_FEE_PER_REQUEST",
]
