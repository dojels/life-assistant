"""素材 / 产物 / 网页预览 / 聊天附件。

从 create_app() 搬出来的第九块业务路由(2026-10-18 架构收口)。
上传走同一套 MAX_UPLOAD_BYTES;预览 CSP 只有一份定义(base/html_page.py)。
"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..base.html_page import HTML_PREVIEW_CSP
from ..domain.materials import HANDLED_LABELS
from .deps import Services, build_intake, get_services
from .idempotency import run_once
from .util import MAX_UPLOAD_BYTES, flash, int_or
from .visibility import member_can_see, web_session


#: 素材一页几个(缩略图墙:24 个正好 5 行左右,手机上是 8 行)
MATERIAL_PAGE_SIZE = 24
#: 产物一页几个(列表行,一行 56px 上下)
ARTIFACT_PAGE_SIZE = 8


def _page_window(current: int, total: int) -> list[int | None]:
    """页码窗口:1 … 4 5 6 … 42。⚠️ 返回 None 表示省略号。

    ⭐ 不把 42 个页码全铺出来:素材到 1000 条时那会是 42 个链接,
       在手机上直接把翻页条挤成两行。
    """
    if total <= 7:
        return list(range(1, total + 1))
    keep = {1, total, current - 1, current, current + 1}
    if current <= 3:
        keep |= {2, 3, 4}
    if current >= total - 2:
        keep |= {total - 3, total - 2, total - 1}
    out: list[int | None] = []
    previous = 0
    for number in sorted(value for value in keep if 1 <= value <= total):
        if previous and number - previous > 1:
            out.append(None)
        out.append(number)
        previous = number
    return out


def register_material_routes(app, *, templates) -> None:
    # ---------------------------------------------------------------- 素材层 / 产物层
    #
    # ⭐ 这两层是"手机优先"的地基:用户从任何渠道送进来的原件都先落素材层,
    #    助手做出来的东西都落产物层。错题页与聊天页都只是它们的入口。
    @app.get("/materials", response_class=HTMLResponse)
    def materials_page(
        request: Request, channel: str = "", handled: str = "", upload: int = 0,
        page: int = 1, apage: int = 1,
        ok: str = "", err: str = "", services: Services = Depends(get_services),
    ):
        """素材与产物。**纯读** —— 关掉模型、断网,这一页照样打得开。

        ⚠️ 两段都**翻页**(2026-10-24 用户:"到 1000 个也一直下拉吗?")。
           以前是硬上限(素材 60 / 产物 30):超出的那些不是"在下面",
           而是**根本看不见** —— 比长页面更糟。
        """
        from math import ceil

        from ..domain.artifacts import ArtifactStore
        from ..domain.materials import MaterialService

        materials = MaterialService(services.assistant, media=services.study.media)
        artifacts = ArtifactStore(services.assistant)
        stats = materials.stats()
        artifact_stats = artifacts.stats()

        material_total = (
            stats["by_channel"].get(channel, 0) if channel
            else stats["by_handled"].get(handled, 0) if handled
            else stats["total"]
        )
        mat_pages = max(1, ceil(material_total / MATERIAL_PAGE_SIZE))
        mat_page = min(int_or(page, 1, low=1, high=10_000), mat_pages)
        rows = [
            item for item in materials.list(
                channel=channel or None, handled=handled or None,
                limit=MATERIAL_PAGE_SIZE,
                offset=(mat_page - 1) * MATERIAL_PAGE_SIZE,
            )
            if member_can_see(services, item.thread_id)
        ]

        # ⚠️ 产物按 session 可见性过滤:翻页发生在 SQL 里、过滤在后面,所以某页
        #    可能少几行(家庭版多成员时才会遇到)。总数用库里的,不假装"这页就是全部"。
        artifact_total = int(artifact_stats.get("total") or 0)
        art_pages = max(1, ceil(artifact_total / ARTIFACT_PAGE_SIZE))
        art_page = min(int_or(apage, 1, low=1, high=10_000), art_pages)
        artifact_rows = [
            item for item in artifacts.list(
                limit=ARTIFACT_PAGE_SIZE, offset=(art_page - 1) * ARTIFACT_PAGE_SIZE
            )
            if member_can_see(services, item.session_id)
        ]

        def link(*, page: int | None = None, apage: int | None = None) -> str:
            """翻页链接:保住对方的页码,也保住当前筛选。"""
            params: list[tuple[str, Any]] = []
            if channel:
                params.append(("channel", channel))
            if handled:
                params.append(("handled", handled))
            if page and page > 1:
                params.append(("page", page))
            if apage and apage > 1:
                params.append(("apage", apage))
            return "/materials" + ("?" + urlencode(params) if params else "")

        return templates.TemplateResponse(
            request,
            "materials.html",
            {
                "page": "materials",
                "materials": rows,
                "stats": stats,
                "artifacts": artifact_rows,
                "mat_page": mat_page, "mat_pages": mat_pages, "mat_total": material_total,
                "mat_window": _page_window(mat_page, mat_pages),
                "art_page": art_page, "art_pages": art_pages, "art_total": artifact_total,
                "art_window": _page_window(art_page, art_pages),
                "mt_link": link,
                "artifact_stats": artifacts.stats(),
                "channel": channel,
                "handled": handled,
                "compose": bool(upload),   # 没有 JS 时 ?upload=1 直接把弹窗摊开
                "handled_labels": HANDLED_LABELS,
                "ok": ok,
                "err": err,
            },
        )

    @app.post("/materials/upload")
    def materials_upload(
        files: list[UploadFile] = File(default=[]),
        ingest: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """手工上传一份原件。默认**只存不解析** —— 解析是错题页那件事。"""
        from ..domain.materials import MaterialService

        # ⚠️ 这个 _idem 以前声明了但没用(2026-10-18 审计):双击/刷新重发会重复
        #    插素材。现在把“读文件 + 落库 + 可选解析”整个包进幂等闸门;指纹用
        #    内容 sha256,而不是只看文件名。
        blobs: list[tuple[str | None, bytes]] = []
        for upload in files:
            data = upload.file.read(MAX_UPLOAD_BYTES + 1)
            if not data:
                continue
            if len(data) > MAX_UPLOAD_BYTES:
                return flash("/materials", "单个文件太大(上限 " + str(MAX_UPLOAD_BYTES // 1024 // 1024) + "MB)", error=True)
            blobs.append((upload.filename, data))
        if not blobs:
            return flash("/materials", "没有收到可用的文件", error=True)

        def _store() -> RedirectResponse:
            materials = MaterialService(services.assistant, media=services.study.media)
            stored = 0
            notes: list[str] = []
            for name, data in blobs:
                try:
                    material, fresh = materials.ingest_bytes(
                        data, channel="web", original_name=name,
                        thread_id=web_session(services),
                    )
                except Exception as exc:  # noqa: BLE001
                    notes.append(str(exc)[:60])
                    continue
                if not fresh:
                    continue
                stored += 1
                if ingest and material.kind == "image":
                    outcome = build_intake(services).ingest_material(material.id, actor="user")
                    notes.append(str(outcome.get("message") or "").strip())
            if not stored:
                return flash("/materials", "没有收到可用的文件" + ("(" + ";".join(notes) + ")" if notes else ""),
                              error=True)
            message = "已存 " + str(stored) + " 份素材"
            if notes:
                message += "。" + " ".join(notes)
            return flash("/materials", message)

        payload = {
            "files": [[name or "", hashlib.sha256(data).hexdigest()] for name, data in blobs],
            "ingest": bool(ingest),
        }
        return run_once(
            services, idem_token, payload, _store,
            replay=flash("/materials", "这一次已经处理过了"),
        )

    @app.get("/materials/{material_id}/file")
    def material_file(material_id: int, services: Services = Depends(get_services)) -> Response:
        """取原件。与 /assets 同一个道理:只认登记过的素材 id,不接受路径。"""
        from ..domain.materials import MaterialService

        materials = MaterialService(services.assistant, media=services.study.media)
        try:
            material = materials.get(int(material_id))
            if not member_can_see(services, material.thread_id):
                return Response(status_code=404, content=b"")
            payload = materials.read_bytes(material)
        except Exception:  # noqa: BLE001
            return Response(status_code=404, content=b"")
        return Response(
            content=payload,
            media_type=material.media_type or "application/octet-stream",
            headers={
                "Cache-Control": "private, max-age=604800",
                # 中文文件名要按 RFC 5987 编码,否则浏览器拿到的是乱码
                "Content-Disposition": "inline; filename*=UTF-8''"
                + quote(str(material.original_name or ("material-" + str(material.id)))),
            },
        )

    @app.get("/artifacts/{artifact_id}")
    def artifact_file(artifact_id: str, services: Services = Depends(get_services)) -> Response:
        """下载一个产物(助手做出来的东西)。"""
        from ..domain.artifacts import ArtifactStore

        store = ArtifactStore(services.assistant)
        try:
            artifact = store.get(str(artifact_id))
            if not member_can_see(services, artifact.session_id):
                return Response(status_code=404, content=b"")
            payload = store.read_bytes(artifact)
        except Exception:  # noqa: BLE001 - 取不到一律 404,不泄露存在性
            return Response(status_code=404, content=b"")
        return Response(
            content=payload,
            media_type=artifact.media_type or "application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(artifact.name),
            },
        )

    # ---------------------------------------------------------------- 网页预览(沙箱)
    #
    # ⭐ 用户要的是"能交互的网页"(点选项判分 / 折叠 / 计算器),所以安全边界必须做实。
    #
    # 威胁模型:网页是**模型写的**(内容可能来自用户输入 / 素材 / 联网),里面的脚本如果能
    # 拿到我们的源,就能读 cookie / localStorage、以用户身份调接口、还能伸手进父页面 DOM。
    #
    # 三层边界(照 D:\云朵辅助培训智能体 app/main.py::_HTML_PREVIEW_CSP,缺一不可):
    #   ① 响应头 CSP **sandbox allow-scripts** —— 文档被强制放进**不透明源**:拿不到
    #      cookie/localStorage、够不到父页面、不能跳转/弹窗/提交表单。关键是**不给
    #      allow-same-origin**;而且写成**响应头**(不是只靠 iframe 的 sandbox 属性)——
    #      用户在新标签页里直接打开这个地址,同样是被沙箱化的。
    #   ② **connect-src 'none'** —— 页面里的 fetch/XHR/WebSocket/beacon 一律发不出去(防外传);
    #      default-src 'none' + 只放行 data:/blob: 的图与内联样式脚本 → 自包含页面够用,
    #      外链一律不加载。
    #   ③ 只服务**产物区里登记过的** .html/.htm —— 不让它变成第二个、加固不同的下载入口。
    # ⭐ D140:这一串现在只有**一份定义**(base/html_page.py::HTML_PREVIEW_CSP)——
    #    page_view 的截图服务用的是同一串,所以"模型看到的沙箱"与"用户点预览看到的沙箱"
    #    是同一个;两处各写一份的话,迟早会漂成两个不同的世界。
    _HTML_PREVIEW_CSP = HTML_PREVIEW_CSP

    @app.get("/generated/{artifact_id}")
    def generated_page(artifact_id: str, services: Services = Depends(get_services)) -> Response:
        """生成网页的**沙箱预览**(html_page 的产物)。

        与 /artifacts/<id>(下载)刻意分开:那条是 attachment,这条是 inline + 沙箱 CSP。
        只认登记过的产物 id,不接受路径 —— 与素材那条同一个道理。
        """
        from ..domain.artifacts import ArtifactStore

        store = ArtifactStore(services.assistant)
        try:
            artifact = store.get(str(artifact_id))
            if not member_can_see(services, artifact.session_id):
                return Response(status_code=404, content=b"")
            payload = store.read_bytes(artifact)
        except Exception:  # noqa: BLE001 - 取不到一律 404,不泄露存在性
            return Response(status_code=404, content=b"")
        if not artifact.name.lower().endswith((".html", ".htm")):
            return Response(status_code=400, content="只允许预览网页(.html)".encode("utf-8"))
        return Response(
            content=payload,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Security-Policy": _HTML_PREVIEW_CSP,
                # 改版会生成新文件,但同一个 id 也可能被重新登记 → 禁缓存,免得看到上一版
                "Cache-Control": "no-store, max-age=0, must-revalidate",
                "X-Content-Type-Options": "nosniff",
            },
        )

    # ---------------------------------------------------------------- 聊天里的附件
    @app.post("/chat/attach")
    def chat_attach(
        files: list[UploadFile] = File(default=[]),
        services: Services = Depends(get_services),
    ) -> JSONResponse:
        """聊天页传图:走**和微信完全同一条**流水线(素材层 -> 识别 -> 录入)。

        ⭐ 刻意不经过聊天模型:照片本身不需要模型在场就能变成错题,
        省一次调用、也更快。结果作为一条**确定性回复**贴回聊天里,
        并把这件事记进对话事件 —— 下一轮模型就知道"刚才那张照片最后变成了什么"。
        """
        from ..domain.materials import MaterialService

        materials = MaterialService(services.assistant, media=services.study.media)
        intake = build_intake(services)
        messages: list[str] = []
        recorded: list[dict[str, Any]] = []
        names: list[str] = []
        stored = 0
        for upload in files:
            data = upload.file.read(MAX_UPLOAD_BYTES + 1)
            name = upload.filename or "照片"
            if not data:
                continue
            if len(data) > MAX_UPLOAD_BYTES:
                return JSONResponse({
                    "ok": False,
                    "message": "单个文件太大(上限 " + str(MAX_UPLOAD_BYTES // 1024 // 1024) + "MB)",
                })
            material, fresh = materials.ingest_bytes(
                data, channel="web", original_name=name, thread_id=web_session(services),
            )
            if not fresh:
                messages.append("这张刚才已经收过了(素材 #" + str(material.id) + ")。")
                continue
            stored += 1
            names.append(name)
            outcome = intake.ingest_material(material.id, auto_accept=True, actor="user")
            messages.append(str(outcome.get("message") or "").strip())
            recorded.extend(outcome.get("recorded") or [])
        if not stored:
            return JSONResponse({"ok": False, "message": "没有收到可用的图片。"})
        message = "\n".join(item for item in messages if item)
        # 记进对话事件:模型不看图,但它必须知道"这张照片最后变成了什么"
        try:
            services.store.ensure_session(web_session(services))
            services.store.append(
                web_session(services), "user/message",
                {"text": "[发来 " + str(stored) + " 张照片:" + "、".join(names) + "]"},
            )
            services.store.append(web_session(services), "assistant/message", {"text": message})
        except Exception:  # noqa: BLE001 - 记不上事件不该让这次上传失败
            pass
        receipt = None
        if services.receipts is not None and recorded:
            try:
                recent = services.receipts.recent(limit=5)
                found = next(
                    (item for item in recent if str(item.kind).startswith("study.")), None
                )
                receipt = found.as_dict() if found is not None else None
            except Exception:  # noqa: BLE001
                receipt = None
        return JSONResponse({
            "ok": True,
            "stored": stored,
            "message": message,
            "recorded": recorded,
            "receipt": receipt,
        })

