"""笔记页路由(纯 SSR)。

⭐ 这一页是"快路径":不经过模型,直接读服务层(services.notes)。
   与聊天里那条慢路径用的是**同一个** NotesService —— 两套逻辑早就会漂。

⚠️ 写操作全部走统一回执(M7),所以这里的按钮不弹确认框:写错了去回执里撤。
   与账本/健康同一套规矩,不另发明一套。
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..domain.services.notes_service import INBOX_PATH, NotesError
from .deps import Services, get_services
from .idempotency import run_once
from .util import flash


def _flatten(tree: list[dict], depth: int = 0, parent_name: str = "") -> list[dict]:
    """把嵌套夹拍平。

    每个节点带两个名字:
      * name  —— 叶名(下拉框、标题用);
      * label —— 筛选片上的短路径(子夹写成「长篇 / 《归途》」,只带一层父名:
                 写满 写作/长篇/《归途》 会在手机上把一排片挤成一团)。
    """
    out: list[dict] = []
    for node in tree:
        label = node["name"] if depth == 0 else (parent_name + " / " + node["name"])
        out.append({"depth": depth, "id": node["id"], "path": node["path"],
                    "name": node["name"], "label": label, "count": node["count"],
                    "kind": node["kind"]})
        out.extend(_flatten(node.get("children") or [], depth + 1, node["name"]))
    return out


_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _day_label(day: str, today: date) -> str:
    """列表里的日期分组标题:今天 / 昨天 / 周三 / 11月5日。

    ⭐ 为什么不显示完整日期:笔记是"最近在写什么",不是账本。相对日期比
       "2026-11-08" 更快读懂,而完整日期放在正文顶上那一行就够。
    """
    try:
        moment = date.fromisoformat(str(day)[:10])
    except ValueError:
        return str(day)
    delta = (today - moment).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "昨天"
    if 1 < delta < 7:
        return _WEEKDAYS[moment.weekday()]
    if moment.year == today.year:
        return str(moment.month) + "月" + str(moment.day) + "日"
    return str(moment.year) + "年" + str(moment.month) + "月" + str(moment.day) + "日"


def _group_by_day(items: list[dict], today: date) -> list[dict]:
    """按日期分组(保序)。同一个日期只出现一个标题。"""
    groups: list[dict] = []
    for item in items:
        day = str(item.get("date") or "")
        if not groups or groups[-1]["date"] != day:
            groups.append({"date": day, "label": _day_label(day, today), "items": []})
        groups[-1]["items"].append(item)
    return groups


def _back(folder: str = "", note: int = 0) -> str:
    parts: list[str] = []
    if folder:
        # 夹名是中文(还有《》):Location 头必须是合法 URL,否则某些客户端
        # 会把整段原样塞进地址栏、跳转后变成 404。
        parts.append("folder=" + quote(str(folder)))
    if note:
        parts.append("note=" + str(int(note)))
    return "/journal" + ("?" + "&".join(parts) if parts else "")


def register_journal_routes(app, *, templates) -> None:
    @app.get("/journal", response_class=HTMLResponse)
    def journal_page(
        request: Request,
        folder: str = "",
        q: str = "",
        note: int = 0,
        new: int = 0,
        focus: int = 0,
        ok: str = "",
        err: str = "",
        services: Services = Depends(get_services),
    ):
        tree = services.notes.tree()
        flat = _flatten(tree)
        folder_path = str(folder or "").strip()
        query = str(q or "").strip()
        #: 新建态:右栏换成一张空白纸,而不是把表单塞在列表中间(那是"管理后台"的做法)。
        composing = bool(new)
        if query:
            listing = services.notes.search(
                {"query": query, "folder": folder_path or None, "limit": 25}
            )
        else:
            listing = services.notes.recent(
                {"limit": 25, "folder": folder_path or None}
            )
        items = listing.get("items") or []
        selected = None
        if composing:
            selected = None
        elif note:
            try:
                selected = services.notes.show({"note_id": int(note)})
            except NotesError as exc:
                err = err or str(exc)
        elif items:
            selected = services.notes.show({"note_id": int(items[0]["id"])})
        # 收件箱的条数单独拎出来:它是"还没归类"的水位线,页面顶上要能看见。
        inbox_count = next(
            (node["count"] for node in flat if node["kind"] == "inbox"), 0
        )
        # ---- 当前本子能不能删(与 service 的规则**同一口径**)----------------
        # 规则:里面一条活着的笔记都不能有、不能有子本子、收件箱不能删。
        # 这里先算好给页面用,**不靠界面启用/禁用**:服务端还会再拦一次。
        current = next((node for node in flat if node["path"] == folder_path), None)
        descendants = [
            node for node in flat
            if folder_path and node["path"].startswith(folder_path + "/")
        ]
        folder_note_count = (int(current["count"]) if current else 0) + sum(
            int(node["count"]) for node in descendants
        )
        folder_child_count = len(descendants)
        folder_deletable = bool(
            current
            and current["kind"] != "inbox"
            and folder_note_count == 0
            and folder_child_count == 0
        )
        return templates.TemplateResponse(
            request,
            "journal.html",
            {
                "page": "journal",
                "tree": tree,
                "folders": flat,
                "folder": folder_path,
                #: 只给最后一段:面包屑里写满 "写作/长篇/《归途》" 会把标题挤掉。
                "folder_label": folder_path.split("/")[-1] if folder_path else "",
                #: 删本子的入口与理由(空才允许;有内容时页面要**说清楚为什么**)
                "folder_kind": current["kind"] if current else "",
                "folder_note_count": folder_note_count,
                "folder_child_count": folder_child_count,
                "folder_deletable": folder_deletable,
                "q": query,
                "items": items,
                "groups": _group_by_day(items, services.settings.now().date()),
                "selected": selected,
                "composing": composing,
                #: 整页阅读:隐藏列表,正文铺满(用户:"日记里都是文本,太小了不方便看")
                "focus": bool(focus),
                "inbox_count": inbox_count,
                "total": sum(node["count"] for node in flat),
                "trash_count": len(services.notes.trash(limit=100)),
                "inbox_path": INBOX_PATH,
                "ok": ok,
                "err": err,
            },
        )

    @app.post("/journal/notes")
    def journal_create(
        body: str = Form(...),
        title: str = Form(""),
        folder: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        target = _back(folder=folder)

        def _create() -> None:
            services.notes.add(
                {"body": body, "title": title or None, "folder": folder or None},
                actor="user", source="web",
            )

        try:
            created = run_once(
                services, idem_token, {"body": body, "folder": folder}, _create
            )
        except NotesError as exc:
            return flash(target, str(exc), error=True)
        # 记完直接打开这一条:否则用户刚写完,右栏还是上一条(会以为没写进去)。
        created_id = int((created or {}).get("note_id") or 0) if isinstance(created, dict) else 0
        return RedirectResponse(
            _back(folder=folder, note=created_id) if created_id else target,
            status_code=303,
        )

    @app.post("/journal/notes/{note_id}")
    def journal_save(
        note_id: int,
        title: str = Form(""),
        body: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        target = _back(note=note_id)

        def _save() -> None:
            services.notes.edit(
                {"note_id": note_id, "title": title or None, "body": body or None},
                actor="user",
            )

        try:
            run_once(services, idem_token, {"note_id": note_id, "body": body}, _save)
        except NotesError as exc:
            return flash(target, str(exc), error=True)
        return RedirectResponse(target, status_code=303)

    @app.post("/journal/notes/{note_id}/move")
    def journal_move(
        note_id: int,
        new_folder: str = Form(...),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        target = _back(folder=new_folder, note=note_id)
        try:
            run_once(
                services, idem_token, {"note_id": note_id, "to": new_folder},
                lambda: services.notes.move(
                    {"note_id": note_id, "new_folder": new_folder}, actor="user"
                ),
            )
        except NotesError as exc:
            return flash(_back(note=note_id), str(exc), error=True)
        return RedirectResponse(target, status_code=303)

    @app.post("/journal/notes/{note_id}/delete")
    def journal_delete(
        note_id: int,
        folder: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        try:
            run_once(
                services, idem_token, {"note_id": note_id},
                lambda: services.notes.delete({"note_id": note_id}, actor="user"),
            )
        except NotesError as exc:
            return flash(_back(folder=folder, note=note_id), str(exc), error=True)
        return RedirectResponse(_back(folder=folder), status_code=303)

    @app.post("/journal/notes/{note_id}/restore")
    def journal_restore(
        note_id: int,
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        try:
            run_once(
                services, idem_token, {"note_id": note_id},
                lambda: services.notes.restore({"note_id": note_id}, actor="user"),
            )
        except NotesError as exc:
            return flash("/journal/trash", str(exc), error=True)
        return RedirectResponse("/journal/trash", status_code=303)

    @app.post("/journal/folders")
    def journal_mkdir(
        name: str = Form(...),
        parent: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        target = _back(folder=parent)
        try:
            created = run_once(
                services, idem_token, {"name": name, "parent": parent},
                lambda: services.notes.mkdir(
                    {"name": name, "parent": parent or None}, actor="user"
                ),
            )
        except NotesError as exc:
            return flash(target, str(exc), error=True)
        # 建完直接进这个新本子:否则停在父本子上,新夹混在一排片里,像"没建成"。
        new_path = str((created or {}).get("path") or "") if isinstance(created, dict) else ""
        return RedirectResponse(
            _back(folder=new_path) if new_path else target, status_code=303
        )

    @app.post("/journal/folders/rename")
    def journal_rename(
        folder: str = Form(...),
        name: str = Form(...),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        try:
            run_once(
                services, idem_token, {"folder": folder, "name": name},
                lambda: services.notes.rename_folder(
                    {"folder": folder, "name": name}, actor="user"
                ),
            )
        except NotesError as exc:
            return flash(_back(folder=folder), str(exc), error=True)
        return RedirectResponse(_back(folder=folder), status_code=303)

    @app.post("/journal/folders/delete")
    def journal_rmdir(
        folder: str = Form(...),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        try:
            run_once(
                services, idem_token, {"folder": folder},
                lambda: services.notes.delete_folder(folder, actor="user"),
            )
        except NotesError as exc:
            return flash(_back(folder=folder), str(exc), error=True)
        return RedirectResponse(_back(), status_code=303)

    @app.get("/journal/trash", response_class=HTMLResponse)
    def journal_trash(
        request: Request,
        ok: str = "",
        err: str = "",
        services: Services = Depends(get_services),
    ):
        return templates.TemplateResponse(
            request,
            "journal_trash.html",
            {"page": "journal", "items": services.notes.trash(limit=100),
             "ok": ok, "err": err},
        )
