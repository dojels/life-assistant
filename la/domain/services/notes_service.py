"""笔记服务:文件夹树 + 笔记 + 版本 + 回执。

⭐ 这个域的**全部业务规则都在这里**(能力域 §6.3):网页、聊天工具、CLI 都只调它。
   工具层负责"模型看得懂的措辞",路由层负责"表单怎么转成请求",两边都不许长规则。

三条与记忆层刻意相反的设计(见 docs/笔记域设计.md):
  1. **不衰减** —— 笔记是用户手写的东西,五年前那条也要能原样翻出来;
  2. **有"夹"** —— 树是人能维护的分类,不是模型猜出来的标签;
  3. **可编辑、可找回** —— 每次改动前留一版(app_note_versions),删除是软删。

⚠️ "没指定夹 -> 收件箱"是**故意**的:随手记的摩擦必须接近 0,归类留给之后。
   这与账本"先定分类再落账"正好相反 —— 账的分类影响统计,笔记的分类只影响找。
"""

from __future__ import annotations

import difflib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ... import config
from ...memory.archive import now_iso
from .. import ownership
from ..notes import search as note_search
from ..receipts import DOMAIN_NOTES, Receipt, ReceiptError, ReceiptLog
from ..schemas.notes import (
    NoteAdd,
    NoteAppend,
    NoteDelete,
    NoteEdit,
    NoteMkdir,
    NoteMove,
    NoteRecent,
    NoteRename,
    NoteRestore,
    NoteSearch,
    NoteShow,
)

DEFAULT_TZID = config.DEFAULT_TIMEZONE
INBOX_PATH = "收件箱"
MAX_DEPTH = 6
#: 列表里的预览长度。**列表只给预览,不给全文** —— 10 条笔记的正文能撑爆上下文。
PREVIEW_CHARS = 60
#: 相似度阈值:超过就认为"这条你记过"(0.86 ≈ 改了两个字的同一句话)。
DUP_RATIO = 0.86
KIND_LABELS = {"note": "随手记", "musing": "感想", "idea": "灵感", "excerpt": "摘录"}
SOURCE_LABELS = {"weixin": "微信", "web": "网页", "cli": "终端", "agent": "助手"}


class NotesError(RuntimeError):
    """笔记域的领域错误(夹不存在、夹不空、笔记不存在……)。"""


def _norm_name(name: str) -> str:
    text = " ".join(str(name or "").split())
    if not text:
        raise NotesError("夹名不能是空的")
    if "/" in text:
        raise NotesError("夹名里不能有 /(它是路径分隔符):" + text)
    if len(text) > 40:
        raise NotesError("夹名最多 40 个字")
    return text


def _norm_path(path: str | None) -> str:
    """把模型/用户给的路径收敛成 'a/b/c':去空白、去首尾斜杠、折叠重复斜杠。"""
    if path is None:
        return ""
    text = str(path).strip().replace("\\", "/")
    parts = [part.strip() for part in text.split("/") if part.strip()]
    return "/".join(parts)


def _auto_title(body: str) -> str:
    """没给标题时用正文首句 —— 只做截断,不改写(改了就不是他的原话)。"""
    first = ""
    for line in str(body or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            first = line
            break
    if not first:
        first = str(body or "").strip()
    for mark in ("。", "!", "?", ";", "!", "?", ";"):
        head, sep, _tail = first.partition(mark)
        if sep and head.strip():
            first = head.strip() + mark
            break
    return first[:40]


def _similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right).ratio()


@dataclass
class _Item:
    row: sqlite3.Row

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.row["id"]),
            "date": str(self.row["local_date"]),
            "ts": str(self.row["ts"]),
            "folder": str(self.row["folder_path"] or INBOX_PATH),
            "title": str(self.row["title"]),
            "kind": str(self.row["kind"]),
            "kind_label": KIND_LABELS.get(str(self.row["kind"]), str(self.row["kind"])),
            "source": str(self.row["source"]),
            "peek": _preview(self.row["body"]),
        }


def _preview(body: Any) -> str:
    text = " ".join(str(body or "").split())
    return text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")


class NotesService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        audit: Callable[..., None] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        tzid: str = DEFAULT_TZID,
        receipts: ReceiptLog | None = None,
        member_id: int | None = None,
    ) -> None:
        self.conn = conn
        self.member_id = member_id
        self._audit = audit
        self._now = now_provider
        self.tzid = tzid
        self.receipts = receipts
        # 能力探测状态:None = 还没探测过;第一次查询时才建索引。
        self._search_mode: str | None = None

    # -- 基础 -------------------------------------------------------------
    def _current_time(self) -> datetime:
        if self._now is not None:
            return self._now()
        return datetime.now(ZoneInfo(self.tzid))

    def _local_date(self, moment: datetime) -> str:
        return moment.astimezone(ZoneInfo(self.tzid)).date().isoformat()

    def _parse_ts(self, text: str | None) -> datetime:
        if not text:
            return self._current_time()
        try:
            parsed = datetime.fromisoformat(str(text))
        except ValueError as exc:
            raise NotesError("时间格式必须是 ISO8601: " + str(text)) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(self.tzid))
        return parsed

    def _owner_clause(self, column: str = "owner_id") -> tuple[str, list[object]]:
        return ownership.owner_filter(getattr(self, "member_id", None), column)

    def _owner_for_new(self) -> int | None:
        return ownership.owner_for_new(
            self.conn, ownership.DOMAIN_NOTES, getattr(self, "member_id", None)
        )

    def _stamp_owner(self, row_id: int, owner: int | None) -> None:
        if owner is not None:
            self.conn.execute(
                "UPDATE app_notes SET owner_id = ? WHERE id = ?", (owner, int(row_id))
            )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """领域 -> 记忆的单向桥:写进发件箱,由 OutboxRelay 搬进归档。"""
        self.conn.execute(
            "INSERT OR IGNORE INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                event_type,
                json.dumps(payload, ensure_ascii=False, default=str),
                "outbox:journal." + uuid.uuid4().hex[:16],
                now_iso(),
            ),
        )

    def _audit_call(self, *, actor: str, action: str, target: str,
                    detail: dict[str, Any] | None, session_id: str | None) -> None:
        if self._audit is not None:
            self._audit(
                actor=actor, action=action, target=target,
                detail=detail or {}, session_id=session_id,
            )

    # -- 夹 ---------------------------------------------------------------
    def _folders(self, *, include_archived: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM app_note_folders"
        if not include_archived:
            sql += " WHERE archived = 0"
        # 顺序 = 创建顺序(不是 path 的字符序):书的本子按"我建它们的先后"排,
        # 按字符排出来是 南方/回声/夏至/归途… 人一眼看不出规律。
        sql += " ORDER BY (kind = 'inbox') DESC, sort_order, id"
        return list(self.conn.execute(sql).fetchall())

    def ensure_inbox(self) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM app_note_folders WHERE kind = 'inbox' AND archived = 0"
            " ORDER BY id LIMIT 1"
        ).fetchone()
        if row is not None:
            return row
        now = now_iso()
        self.conn.execute(
            "INSERT INTO app_note_folders(parent_id, name, path, kind, sort_order, archived,"
            " created_at, updated_at) VALUES (NULL, ?, ?, 'inbox', 0, 0, ?, ?)",
            (INBOX_PATH, INBOX_PATH, now, now),
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM app_note_folders WHERE kind = 'inbox' ORDER BY id LIMIT 1"
        ).fetchone()

    def folder_by_path(self, path: str | None) -> sqlite3.Row | None:
        text = _norm_path(path)
        if not text:
            return None
        return self.conn.execute(
            "SELECT * FROM app_note_folders WHERE path = ? AND archived = 0", (text,)
        ).fetchone()

    def folder_by_id(self, folder_id: int | None) -> sqlite3.Row | None:
        if not folder_id:
            return None
        return self.conn.execute(
            "SELECT * FROM app_note_folders WHERE id = ?", (int(folder_id),)
        ).fetchone()

    def _counts(self) -> dict[int, int]:
        clause, params = self._owner_clause("owner_id")
        rows = self.conn.execute(
            "SELECT folder_id, COUNT(*) AS n FROM app_notes"
            " WHERE deleted_at IS NULL AND folder_id IS NOT NULL AND " + clause
            + " GROUP BY folder_id",
            params,
        ).fetchall()
        return {int(row["folder_id"]): int(row["n"]) for row in rows}

    def tree(self) -> list[dict[str, Any]]:
        """返回嵌套的夹列表(每个节点带条数)。收件箱永远排第一。"""
        counts = self._counts()
        nodes: dict[int, dict[str, Any]] = {}
        order: list[sqlite3.Row] = self._folders()
        for row in order:
            nodes[int(row["id"])] = {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "path": str(row["path"]),
                "kind": str(row["kind"]),
                "count": counts.get(int(row["id"]), 0),
                "children": [],
            }
        roots: list[dict[str, Any]] = []
        for row in order:
            node = nodes[int(row["id"])]
            parent_id = row["parent_id"]
            if parent_id and int(parent_id) in nodes:
                nodes[int(parent_id)]["children"].append(node)
            else:
                roots.append(node)
        return roots

    def folder_candidates(self, wanted: str | None, *, limit: int = 5) -> list[dict[str, Any]]:
        """给"夹不存在"时用的候选:同名的(任何层级)> 路径相近的 > 最近用过的。

        这与账本的分类候选同一套思路:**不让模型自己编一个夹名**,否则同一个意思
        会分裂成"网球 / 打网球 / 网球课"三份。候选按"像不像"排序,最多 5 个。
        """
        text = _norm_path(wanted)
        rows = self._folders()
        if not rows:
            return []
        last = text.split("/")[-1] if text else ""
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            path = str(row["path"])
            score = 0.0
            if text and path == text:
                score = 2.0
            elif last and str(row["name"]) == last:
                score = 1.6
            elif text and (text in path or path in text):
                score = 1.2
            elif last and _similarity(last, str(row["name"])) >= 0.5:
                score = 0.8
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], -int(item[1]["id"])))
        picked = [row for _score, row in scored[:limit]]
        if len(picked) < limit:
            # 补几个"最近用过的"(id 大的),让模型至少有一个合法落点
            seen = {int(row["id"]) for row in picked}
            for row in sorted(rows, key=lambda r: -int(r["id"]))[:limit]:
                if int(row["id"]) in seen:
                    continue
                picked.append(row)
                if len(picked) >= limit:
                    break
        return [
            {"id": int(row["id"]), "path": str(row["path"]),
             "count": self._counts().get(int(row["id"]), 0)}
            for row in picked
        ]

    def mkdir(
        self, payload: NoteMkdir | dict[str, Any], *, actor: str = "user",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, NoteMkdir):
            payload = NoteMkdir.model_validate(payload)
        name = _norm_name(payload.name)
        parent_path = _norm_path(payload.parent)
        parent = None
        if parent_path:
            parent = self.folder_by_path(parent_path)
            if parent is None:
                raise NotesError(
                    "没有这个父夹: " + parent_path
                    + ";已有:" + "、".join(str(row["path"]) for row in self._folders()[:8])
                )
        path = (str(parent["path"]) + "/" + name) if parent is not None else name
        depth = path.count("/") + 1
        if depth > MAX_DEPTH:
            raise NotesError("夹最多 " + str(MAX_DEPTH) + " 层")
        if self.folder_by_path(path) is not None:
            raise NotesError("已经有这个夹了: " + path)
        now = now_iso()
        cursor = self.conn.execute(
            "INSERT INTO app_note_folders(parent_id, name, path, kind, sort_order, archived,"
            " created_at, updated_at) VALUES (?, ?, ?, 'normal', 0, 0, ?, ?)",
            (int(parent["id"]) if parent is not None else None, name, path, now, now),
        )
        folder_id = int(cursor.lastrowid or 0)
        if self.receipts is not None:
            self.receipts.record(
                kind="journal.folder", domain=DOMAIN_NOTES, label="新建夹 " + path,
                detail={"folder_id": folder_id, "path": path, "op": "mkdir"},
                undo_plan={"op": "mkdir", "folder_id": folder_id},
                actor=actor, session_id=session_id,
            )
        self.conn.commit()
        self._emit("journal/folder", {"op": "mkdir", "path": path})
        self._audit_call(actor=actor, action="journal.mkdir", target=path,
                         detail={"folder_id": folder_id}, session_id=session_id)
        return {"ok": True, "folder_id": folder_id, "path": path}

    def rename_folder(
        self, payload: NoteRename | dict[str, Any], *, actor: str = "user",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, NoteRename):
            payload = NoteRename.model_validate(payload)
        old = _norm_path(payload.folder)
        row = self.folder_by_path(old)
        if row is None:
            raise NotesError("没有这个夹: " + old)
        if str(row["kind"]) == "inbox":
            raise NotesError("收件箱是系统夹,不能改名")
        name = _norm_name(payload.name)
        parent_path = old.rsplit("/", 1)[0] if "/" in old else ""
        new = (parent_path + "/" + name) if parent_path else name
        if new == old:
            return {"ok": True, "path": old, "unchanged": True}
        if self.folder_by_path(new) is not None:
            raise NotesError("已经有这个夹了: " + new)
        self._prefix_replace(old, new)
        self.conn.execute(
            "UPDATE app_note_folders SET name = ?, updated_at = ? WHERE id = ?",
            (name, now_iso(), int(row["id"])),
        )
        if self.receipts is not None:
            self.receipts.record(
                kind="journal.folder", domain=DOMAIN_NOTES, label="夹改名 " + old + " -> " + new,
                detail={"path": new, "op": "ren"},
                undo_plan={"op": "ren", "old": old, "new": new},
                actor=actor, session_id=session_id,
            )
        self.conn.commit()
        self._emit("journal/folder", {"op": "rename", "path": new, "old": old})
        self._audit_call(actor=actor, action="journal.rename", target=new,
                         detail={"old": old}, session_id=session_id)
        return {"ok": True, "path": new, "old": old}

    def delete_folder(
        self, folder: str, *, actor: str = "user", session_id: str | None = None,
    ) -> dict[str, Any]:
        """归档一个夹。**夹里还有笔记或子夹就拒绝** —— 不静默连坐删东西。"""
        path = _norm_path(folder)
        row = self.folder_by_path(path)
        if row is None:
            raise NotesError("没有这个夹: " + path)
        if str(row["kind"]) == "inbox":
            raise NotesError("收件箱是系统夹,不能删")
        notes = self.conn.execute(
            "SELECT COUNT(*) FROM app_notes WHERE deleted_at IS NULL AND folder_id IN"
            " (SELECT id FROM app_note_folders WHERE path = ? OR path LIKE ?)",
            (path, path + "/%"),
        ).fetchone()[0]
        if int(notes):
            raise NotesError(
                "这个夹里还有 " + str(int(notes)) + " 条笔记;先把它们移走或删掉,再删夹"
            )
        children = self.conn.execute(
            "SELECT COUNT(*) FROM app_note_folders WHERE archived = 0 AND path LIKE ?",
            (path + "/%",),
        ).fetchone()[0]
        if int(children):
            raise NotesError("这个夹下面还有 " + str(int(children)) + " 个子夹,先删子夹")
        self.conn.execute(
            "UPDATE app_note_folders SET archived = 1, updated_at = ? WHERE id = ?",
            (now_iso(), int(row["id"])),
        )
        if self.receipts is not None:
            self.receipts.record(
                kind="journal.folder", domain=DOMAIN_NOTES, label="删夹 " + path,
                detail={"path": path, "op": "delete"},
                undo_plan={"op": "archive", "folder_id": int(row["id"])},
                actor=actor, session_id=session_id,
            )
        self.conn.commit()
        self._emit("journal/folder", {"op": "archive", "path": path})
        self._audit_call(actor=actor, action="journal.rmdir", target=path,
                         detail={}, session_id=session_id)
        return {"ok": True, "path": path, "archived": True}

    def _prefix_replace(self, old: str, new: str) -> None:
        """把 old 及其子树的 path 前缀换成 new(一条 UPDATE,不递归)。"""
        self.conn.execute(
            "UPDATE app_note_folders SET path = ? || substr(path, ?), updated_at = ?"
            " WHERE path = ? OR path LIKE ?",
            (new, len(old) + 1, now_iso(), old, old + "/%"),
        )

    # -- 笔记:读 ---------------------------------------------------------
    def _select_sql(self) -> str:
        return (
            "SELECT n.*, COALESCE(f.path, ?) AS folder_path FROM app_notes n"
            " LEFT JOIN app_note_folders f ON f.id = n.folder_id"
        )

    def _load(self, note_id: int, *, include_deleted: bool = False) -> sqlite3.Row:
        sql = self._select_sql() + " WHERE n.id = ?"
        params: list[Any] = [INBOX_PATH, int(note_id)]
        if not include_deleted:
            sql += " AND n.deleted_at IS NULL"
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            raise NotesError("没有这条笔记(可能已经删了): #" + str(note_id))
        return row

    def show(
        self, payload: NoteShow | dict[str, Any] | int, *, full: bool | None = None
    ) -> dict[str, Any]:
        if isinstance(payload, int):
            note_id, want_full = payload, bool(full)
        else:
            if not isinstance(payload, NoteShow):
                payload = NoteShow.model_validate(payload)
            note_id, want_full = int(payload.note_id), bool(payload.full)
        row = self._load(note_id)
        item = _Item(row).as_dict()
        body = str(row["body"])
        limit = len(body) if want_full else 800
        item["body"] = body[:limit]
        item["truncated"] = len(body) > limit
        item["chars"] = len(body)
        item["versions"] = self.versions(note_id)
        return item

    def recent(
        self, payload: NoteRecent | dict[str, Any] | None = None, *, limit: int = 10,
        folder: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(payload, NoteRecent):
            limit, folder = int(payload.limit), payload.folder
        elif isinstance(payload, dict):
            limit = int(payload.get("limit") or limit)
            folder = payload.get("folder") or folder
        return self._list(folder=folder, limit=limit)

    def search(
        self, payload: NoteSearch | dict[str, Any], *, limit: int | None = None
    ) -> dict[str, Any]:
        if not isinstance(payload, NoteSearch):
            payload = NoteSearch.model_validate(payload)
        return self._list(
            folder=payload.folder, limit=int(limit or payload.limit), query=payload.query
        )

    def _list(
        self, *, folder: str | None = None, limit: int = 10, query: str | None = None
    ) -> dict[str, Any]:
        path = _norm_path(folder) or None
        limit = max(1, min(int(limit), 25))
        if query:
            mode = self._ensure_search()
            hits = note_search.search_notes(
                self.conn, query, limit=limit, folder_path=path, mode=mode
            )
            if not hits:
                return {"count": 0, "items": [], "query": query}
            ids = list(hits)
            clause, params = self._owner_clause("n.owner_id")
            placeholders = ",".join("?" for _ in ids)
            rows = self.conn.execute(
                self._select_sql()
                + " WHERE n.deleted_at IS NULL AND n.id IN (" + placeholders + ")"
                + " AND " + clause,
                [INBOX_PATH, *ids, *params],
            ).fetchall()
            by_id = {int(row["id"]): row for row in rows}
            ordered = sorted(
                ids,
                key=lambda i: (-hits[i].score, -len(hits[i].matched),
                               -int(by_id[i]["id"]) if i in by_id else 0),
            )
            items = [
                _Item(by_id[note_id]).as_dict()
                for note_id in ordered[:limit]
                if note_id in by_id
            ]
            return {"count": len(items), "items": items, "query": query}

        clause, params = self._owner_clause("n.owner_id")
        sql = self._select_sql() + " WHERE n.deleted_at IS NULL AND " + clause
        values: list[Any] = [INBOX_PATH, *params]
        if path:
            sql += (" AND n.folder_id IN (SELECT id FROM app_note_folders"
                    " WHERE path = ? OR path LIKE ?)")
            values.extend([path, path + "/%"])
        sql += " ORDER BY n.local_date DESC, n.id DESC LIMIT ?"
        values.append(limit)
        rows = self.conn.execute(sql, values).fetchall()
        return {"count": len(rows), "items": [_Item(row).as_dict() for row in rows]}

    def _ensure_search(self) -> str:
        if self._search_mode is None:
            self._search_mode = note_search.ensure_index(self.conn)
        return self._search_mode

    def hint(self, text: str, *, limit: int = 3) -> list[dict[str, Any]]:
        """给"每轮自动召回"用:只返回标题级线索,绝不返回正文。

        ⚠️ 它绕过了工具管线(没有门禁/预算),所以调用方**必须**记一条事件 ——
           否则以后查不出"它当时为什么没看见"(见 session._notes_hint)。
        """
        raw = str(text or "").strip()
        if len(raw) < 4 or len(raw) > 120:
            # 太短(「好」「嗯」)没有检索价值;太长(整段粘贴)召回必然发散。
            return []
        try:
            mode = self._ensure_search()
            hits = note_search.search_notes(self.conn, raw, limit=limit * 2, mode=mode)
        except Exception:  # noqa: BLE001 - 召回失败绝不能影响这一轮对话
            return []
        # 门槛与分差都来自真实语料校准(见 search.HINT_MIN_SCORE 的注释):
        # 分数 <0.30 的多半只是撞上了一个 2 字词;冠军领先 0.25 以上时,
        # 第二名基本都是噪声,只提示第一条(云朵的"分差门控")。
        ranked = sorted(hits.values(), key=lambda hit: -hit.score)
        ranked = [hit for hit in ranked if hit.score >= note_search.HINT_MIN_SCORE]
        if not ranked:
            return []
        if (
            len(ranked) > 1
            and ranked[0].score >= 0.6
            and ranked[0].score - ranked[1].score >= note_search.HINT_GAP
        ):
            ranked = ranked[:1]
        ids = [hit.note_id for hit in ranked[:limit]]
        clause, params = self._owner_clause("n.owner_id")
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            self._select_sql() + " WHERE n.deleted_at IS NULL AND n.id IN ("
            + placeholders + ") AND " + clause,
            [INBOX_PATH, *ids, *params],
        ).fetchall()
        by_id = {int(row["id"]): row for row in rows}
        out: list[dict[str, Any]] = []
        for note_id in ids:
            row = by_id.get(note_id)
            if row is None:
                continue
            title = str(row["title"])
            if len(title) > 30:
                # 线索行要短:它每轮都可能进上下文,一条占两行就等于白花预算。
                title = title[:30] + "…"
            out.append({
                "id": note_id,
                "title": title,
                "folder": str(row["folder_path"] or INBOX_PATH),
                "date": str(row["local_date"]),
                "score": round(float(hits[note_id].score), 2),
            })
        return out

    def versions(self, note_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, reason, created_at FROM app_note_versions WHERE note_id = ?"
            " ORDER BY id DESC LIMIT 5",
            (int(note_id),),
        ).fetchall()
        return [
            {"id": int(row["id"]), "reason": str(row["reason"]),
             "at": str(row["created_at"])}
            for row in rows
        ]

    def trash(self, *, limit: int = 50) -> list[dict[str, Any]]:
        clause, params = self._owner_clause("n.owner_id")
        rows = self.conn.execute(
            self._select_sql() + " WHERE n.deleted_at IS NOT NULL AND " + clause
            + " ORDER BY n.deleted_at DESC LIMIT ?",
            [INBOX_PATH, *params, max(1, min(int(limit), 100))],
        ).fetchall()
        out = []
        for row in rows:
            item = _Item(row).as_dict()
            item["deleted_at"] = str(row["deleted_at"])
            out.append(item)
        return out

    # -- 笔记:写 ---------------------------------------------------------
    def _snapshot(self, row: sqlite3.Row, reason: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO app_note_versions(note_id, title, body, folder_id, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (int(row["id"]), str(row["title"]), str(row["body"]),
             row["folder_id"], reason, now_iso()),
        )
        return int(cursor.lastrowid or 0)

    def _duplicates(self, body: str, folder_id: int, *, limit: int = 3) -> list[dict[str, Any]]:
        """同一夹里"像是同一条"的笔记。**只在写入时查,不额外花一次模型调用。**"""
        rows = self.conn.execute(
            "SELECT * FROM app_notes WHERE deleted_at IS NULL AND folder_id IS ?"
            " ORDER BY id DESC LIMIT 30",
            (int(folder_id),),
        ).fetchall()
        target = " ".join(body.split())
        found: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            other = " ".join(str(row["body"]).split())
            score = _similarity(target[:400], other[:400])
            if score >= DUP_RATIO:
                found.append((score, row))
        found.sort(key=lambda item: -item[0])
        return [
            {"id": int(row["id"]), "date": str(row["local_date"]),
             "title": str(row["title"]), "similarity": round(score, 2)}
            for score, row in found[:limit]
        ]

    def add(
        self, payload: NoteAdd | dict[str, Any], *, actor: str = "agent",
        session_id: str | None = None, source: str = "agent",
    ) -> dict[str, Any]:
        if not isinstance(payload, NoteAdd):
            payload = NoteAdd.model_validate(payload)
        body = str(payload.body or "").strip()
        if not body:
            raise NotesError("笔记正文不能是空的")
        wanted = _norm_path(payload.folder)
        if wanted:
            folder = self.folder_by_path(wanted)
            if folder is None:
                # 不是错误,是"还差一个决定":与账本的 needs_category 同一形状。
                return {
                    "recorded": False,
                    "needs_folder": True,
                    "wanted": wanted,
                    "candidates": self.folder_candidates(wanted),
                    "what_now": (
                        "这一条**还没写进去**。请二选一:① 从 candidates 里挑一个夹,"
                        "照原参数再调一次 note_add(只把 folder 换成那个 path);"
                        "② 用户确实要新建,就调 note_add(action=mkdir, name=…),"
                        "建好之后再记。"
                    ),
                }
        else:
            folder = self.ensure_inbox()
        folder_id = int(folder["id"])
        if not payload.force:
            duplicates = self._duplicates(body, folder_id)
            if duplicates:
                return {
                    "recorded": False,
                    "duplicate_of": duplicates,
                    "what_now": (
                        "这条和已有的很像,**还没写进去**。先问用户:是不是同一件事?"
                        "是同一件 -> 用 action=append 补在原来那条后面(或 action=edit 改它);"
                        "确实是新的 -> 用 force=true 再调一次 note_add。"
                    ),
                }
        moment = self._parse_ts(payload.ts)
        title = (payload.title or _auto_title(body)).strip()[:120]
        owner = self._owner_for_new()
        now = now_iso()
        cursor = self.conn.execute(
            "INSERT INTO app_notes(folder_id, title, body, kind, ts, local_date, source,"
            " tags, created_at, updated_at, owner_id) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (folder_id, title, body, payload.kind,
             moment.isoformat(timespec="seconds"), self._local_date(moment),
             source, now, now, owner),
        )
        note_id = int(cursor.lastrowid or 0)
        self._stamp_owner(note_id, owner)
        if self.receipts is not None:
            self.receipts.record(
                kind="journal.note", domain=DOMAIN_NOTES,
                label="记笔记 " + title,
                detail={"note_id": note_id, "folder": str(folder["path"]),
                        "title": title, "date": self._local_date(moment), "op": "add"},
                undo_plan={"op": "add", "note_id": note_id},
                actor=actor, session_id=session_id,
            )
        self.conn.commit()
        self._emit("journal/note", {
            "op": "add", "note_id": note_id, "folder": str(folder["path"]),
            "title": title, "date": self._local_date(moment),
        })
        self._audit_call(actor=actor, action="journal.note.add", target="note:" + str(note_id),
                         detail={"folder": str(folder["path"])}, session_id=session_id)
        return {
            "ok": True, "recorded": True, "note_id": note_id,
            "folder": str(folder["path"]), "title": title,
            "date": self._local_date(moment),
            "preview": _preview(body),
        }

    def append(
        self, payload: NoteAppend | dict[str, Any], *, actor: str = "agent",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, NoteAppend):
            payload = NoteAppend.model_validate(payload)
        row = self._load(int(payload.note_id))
        version_id = self._snapshot(row, "append")
        added = str(payload.body or "").strip()
        if not added:
            raise NotesError("要追加的内容不能是空的")
        new_body = str(row["body"]).rstrip() + str(payload.separator) + added
        self.conn.execute(
            "UPDATE app_notes SET body = ?, updated_at = ? WHERE id = ?",
            (new_body, now_iso(), int(row["id"])),
        )
        if self.receipts is not None:
            self.receipts.record(
                kind="journal.note", domain=DOMAIN_NOTES, label="补记 #" + str(row["id"]),
                detail={"note_id": int(row["id"]), "op": "edit", "added": len(added)},
                undo_plan={"op": "edit", "note_id": int(row["id"]), "version_id": version_id},
                actor=actor, session_id=session_id,
            )
        self.conn.commit()
        self._emit("journal/note", {"op": "edit", "note_id": int(row["id"]),
                                    "folder": str(row["folder_path"] or INBOX_PATH)})
        self._audit_call(actor=actor, action="journal.note.append",
                         target="note:" + str(row["id"]), detail={}, session_id=session_id)
        return {"ok": True, "note_id": int(row["id"]), "chars": len(new_body),
                "version_id": version_id}

    def edit(
        self, payload: NoteEdit | dict[str, Any], *, actor: str = "agent",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, NoteEdit):
            payload = NoteEdit.model_validate(payload)
        row = self._load(int(payload.note_id))
        if payload.title is None and payload.body is None:
            raise NotesError("没有要改的内容(标题和正文都没给)")
        version_id = self._snapshot(row, "edit")
        title = str(payload.title).strip()[:120] if payload.title is not None else str(row["title"])
        body = str(payload.body).strip() if payload.body is not None else str(row["body"])
        if not body:
            raise NotesError("正文不能改成空;真要丢掉这条,请用 action=delete")
        self.conn.execute(
            "UPDATE app_notes SET title = ?, body = ?, updated_at = ? WHERE id = ?",
            (title, body, now_iso(), int(row["id"])),
        )
        if self.receipts is not None:
            self.receipts.record(
                kind="journal.note", domain=DOMAIN_NOTES, label="改笔记 #" + str(row["id"]),
                detail={"note_id": int(row["id"]), "op": "edit"},
                undo_plan={"op": "edit", "note_id": int(row["id"]), "version_id": version_id},
                actor=actor, session_id=session_id,
            )
        self.conn.commit()
        self._emit("journal/note", {"op": "edit", "note_id": int(row["id"]),
                                    "folder": str(row["folder_path"] or INBOX_PATH)})
        self._audit_call(actor=actor, action="journal.note.edit",
                         target="note:" + str(row["id"]), detail={}, session_id=session_id)
        return {"ok": True, "note_id": int(row["id"]), "title": title,
                "version_id": version_id}

    def move(
        self, payload: NoteMove | dict[str, Any], *, actor: str = "agent",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, NoteMove):
            payload = NoteMove.model_validate(payload)
        row = self._load(int(payload.note_id))
        wanted = _norm_path(payload.new_folder)
        target = self.folder_by_path(wanted)
        if target is None:
            return {
                "moved": False, "needs_folder": True, "wanted": wanted,
                "candidates": self.folder_candidates(wanted),
                "what_now": "这个夹不存在,**还没挪**。挑一个候选的 path 再调一次,或先 action=mkdir。",
            }
        old_folder_id = row["folder_id"]
        if old_folder_id is not None and int(old_folder_id) == int(target["id"]):
            return {"ok": True, "moved": False, "note_id": int(row["id"]),
                    "folder": str(target["path"]), "unchanged": True}
        self.conn.execute(
            "UPDATE app_notes SET folder_id = ?, updated_at = ? WHERE id = ?",
            (int(target["id"]), now_iso(), int(row["id"])),
        )
        if self.receipts is not None:
            self.receipts.record(
                kind="journal.note", domain=DOMAIN_NOTES,
                label="挪笔记 #" + str(row["id"]) + " -> " + str(target["path"]),
                detail={"note_id": int(row["id"]), "op": "move",
                        "to": str(target["path"])},
                undo_plan={"op": "move", "note_id": int(row["id"]),
                           "from_folder_id": old_folder_id},
                actor=actor, session_id=session_id,
            )
        self.conn.commit()
        self._emit("journal/note", {"op": "move", "note_id": int(row["id"]),
                                    "folder": str(target["path"])})
        self._audit_call(actor=actor, action="journal.note.move",
                         target="note:" + str(row["id"]),
                         detail={"to": str(target["path"])}, session_id=session_id)
        return {"ok": True, "moved": True, "note_id": int(row["id"]),
                "folder": str(target["path"]), "from": str(row["folder_path"] or INBOX_PATH)}

    def delete(
        self, payload: NoteDelete | dict[str, Any], *, actor: str = "agent",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, NoteDelete):
            payload = NoteDelete.model_validate(payload)
        if payload.note_id is None and not payload.folder:
            raise NotesError("要删哪一条?给 note_id(删一条)或 folder(删一个空夹)")
        if payload.note_id is None:
            return self.delete_folder(str(payload.folder), actor=actor, session_id=session_id)
        row = self._load(int(payload.note_id))
        version_id = self._snapshot(row, "delete")
        self.conn.execute(
            "UPDATE app_notes SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), int(row["id"])),
        )
        if self.receipts is not None:
            self.receipts.record(
                kind="journal.note", domain=DOMAIN_NOTES,
                label="删笔记 #" + str(row["id"]) + " " + str(row["title"]),
                detail={"note_id": int(row["id"]), "op": "delete"},
                undo_plan={"op": "delete", "note_id": int(row["id"]), "version_id": version_id},
                actor=actor, session_id=session_id,
            )
        self.conn.commit()
        self._emit("journal/note", {"op": "delete", "note_id": int(row["id"]),
                                    "folder": str(row["folder_path"] or INBOX_PATH)})
        self._audit_call(actor=actor, action="journal.note.delete",
                         target="note:" + str(row["id"]), detail={}, session_id=session_id)
        return {"ok": True, "deleted": True, "note_id": int(row["id"]),
                "trash": True, "title": str(row["title"])}

    def restore(
        self, payload: NoteRestore | dict[str, Any], *, actor: str = "agent",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, NoteRestore):
            payload = NoteRestore.model_validate(payload)
        row = self._load(int(payload.note_id), include_deleted=True)
        if row["deleted_at"] is None:
            return {"ok": True, "note_id": int(row["id"]), "restored": False}
        # ⚠️ 本子可能已经在他处被删掉了(删本子只看"活着的笔记",回收站里那些
        #    仍指着旧夹)。恢复时若指的夹已归档,落到收件箱 —— 否则这条笔记会
        #    挂在一个界面上已经看不见的夹里,等于"恢复了但找不到"。
        folder = self.folder_by_id(row["folder_id"]) if row["folder_id"] else None
        inbox = None
        if folder is None or int(folder["archived"] or 0):
            inbox = self.ensure_inbox()
        self.conn.execute(
            "UPDATE app_notes SET deleted_at = NULL, folder_id = ?, updated_at = ? WHERE id = ?",
            (int(inbox["id"]) if inbox is not None else row["folder_id"],
             now_iso(), int(row["id"])),
        )
        if self.receipts is not None:
            self.receipts.record(
                kind="journal.note", domain=DOMAIN_NOTES,
                label="恢复笔记 #" + str(row["id"]),
                detail={"note_id": int(row["id"]), "op": "restore"},
                undo_plan={"op": "restore", "note_id": int(row["id"])},
                actor=actor, session_id=session_id,
            )
        self.conn.commit()
        self._emit("journal/note", {"op": "restore", "note_id": int(row["id"]),
                                    "folder": str(row["folder_path"] or INBOX_PATH)})
        self._audit_call(actor=actor, action="journal.note.restore",
                         target="note:" + str(row["id"]), detail={}, session_id=session_id)
        return {"ok": True, "note_id": int(row["id"]), "restored": True,
                "title": str(row["title"]),
                "moved_to_inbox": inbox is not None}

    # -- 撤销(由统一回执调用)--------------------------------------------
    def revert_note(self, receipt: Receipt) -> str:
        """撤销一条笔记写入。**不产生新回执**(否则撤销本身也能被撤销,无限套娃)。"""
        plan = receipt.undo_plan or {}
        op = str(plan.get("op") or "")
        note_id = plan.get("note_id")
        if not note_id:
            raise ReceiptError("这条回执没有指明是哪条笔记,撤不了")
        if op == "add":
            self.conn.execute(
                "UPDATE app_notes SET deleted_at = ?, updated_at = ? WHERE id = ?",
                (now_iso(), now_iso(), int(note_id)),
            )
            self.conn.commit()
            return "已把刚记的那条挪进回收站(#" + str(note_id) + ")"
        if op == "edit":
            version_id = plan.get("version_id")
            version = self.conn.execute(
                "SELECT * FROM app_note_versions WHERE id = ?", (int(version_id or 0),)
            ).fetchone()
            if version is None:
                raise ReceiptError("找不到改动前的版本,撤不回去")
            self.conn.execute(
                "UPDATE app_notes SET title = ?, body = ?, updated_at = ? WHERE id = ?",
                (str(version["title"]), str(version["body"]), now_iso(), int(note_id)),
            )
            self.conn.commit()
            return "已把 #" + str(note_id) + " 改回上一版"
        if op == "delete":
            self.conn.execute(
                "UPDATE app_notes SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                (now_iso(), int(note_id)),
            )
            self.conn.commit()
            return "已从回收站恢复 #" + str(note_id)
        if op == "move":
            from_id = plan.get("from_folder_id")
            self.conn.execute(
                "UPDATE app_notes SET folder_id = ?, updated_at = ? WHERE id = ?",
                (int(from_id) if from_id is not None else None, now_iso(), int(note_id)),
            )
            self.conn.commit()
            return "已把 #" + str(note_id) + " 挪回原来的夹"
        if op == "restore":
            self.conn.execute(
                "UPDATE app_notes SET deleted_at = ?, updated_at = ? WHERE id = ?",
                (now_iso(), now_iso(), int(note_id)),
            )
            self.conn.commit()
            return "已把 #" + str(note_id) + " 重新丢回回收站"
        raise ReceiptError("这种笔记操作不支持撤销: " + (op or "未知"))

    def revert_folder(self, receipt: Receipt) -> str:
        plan = receipt.undo_plan or {}
        op = str(plan.get("op") or "")
        if op == "mkdir":
            folder_id = plan.get("folder_id")
            row = self.folder_by_id(int(folder_id or 0))
            if row is None:
                raise ReceiptError("这个夹已经不在了")
            self.conn.execute(
                "UPDATE app_note_folders SET archived = 1, updated_at = ? WHERE id = ?",
                (now_iso(), int(row["id"])),
            )
            self.conn.commit()
            return "已把刚建的夹归档(" + str(row["path"]) + ")"
        if op in ("ren", "mvdir"):
            old, new = str(plan.get("old") or ""), str(plan.get("new") or "")
            if not old or not new:
                raise ReceiptError("这条回执没记下旧路径,撤不回去")
            self._prefix_replace(new, old)
            self.conn.execute(
                "UPDATE app_note_folders SET name = ?, updated_at = ? WHERE path = ?",
                (old.rsplit("/", 1)[-1], now_iso(), old),
            )
            self.conn.commit()
            return "已把夹名改回 " + old
        if op == "archive":
            row = self.folder_by_id(int(plan.get("folder_id") or 0))
            if row is None:
                raise ReceiptError("这个夹已经不在了")
            parent_path = str(row["path"]).rsplit("/", 1)[0] if "/" in str(row["path"]) else ""
            blocked = None
            if parent_path:
                blocked = self.folder_by_path(parent_path)
            self.conn.execute(
                "UPDATE app_note_folders SET archived = 0, parent_id = ?, updated_at = ?"
                " WHERE id = ?",
                (int(blocked["id"]) if blocked is not None else None,
                 now_iso(), int(row["id"])),
            )
            self.conn.commit()
            return "已把夹恢复回来(" + str(row["path"]) + ")"
        raise ReceiptError("这种夹操作不支持撤销: " + (op or "未知"))
