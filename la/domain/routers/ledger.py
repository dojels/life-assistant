"""记账的 REST 薄壳。

⚠️ 这里**刻意不依赖任何 Web 框架**:每个处理器接收"请求体 dict"、返回 (status, body),
形状与任何框架的 handler 一一对应。M5 挂到 FastAPI/Starlette 上不需要改任何业务代码。

可验收标准(能力域 §6.3):**任何业务规则改动只允许改一个文件**。
如果某条规则需要同时改这里和 services/,说明抽象泄漏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..schemas.ledger import ExpenseCreate, ExpenseQuery
from ..services.ledger_service import (
    LedgerError,
    LedgerService,
    UnknownAccountError,
    UnknownCategoryError,
)


@dataclass
class Response:
    status: int
    body: dict[str, Any] = field(default_factory=dict)

    def as_tuple(self) -> tuple[int, dict[str, Any]]:
        return self.status, self.body


def _error(exc: Exception) -> Response:
    if isinstance(exc, (UnknownAccountError, UnknownCategoryError)):
        return Response(422, {"error": exc.__class__.__name__, "message": str(exc)})
    if isinstance(exc, LedgerError):
        return Response(400, {"error": exc.__class__.__name__, "message": str(exc)})
    if isinstance(exc, ValidationError):
        return Response(400, {"error": "ValidationError", "message": exc.errors()[0]["msg"]})
    raise exc


class LedgerRouter:
    """REST 端点集合。与 LLM 工具**调用同一批 service 方法**。"""

    def __init__(self, service: LedgerService) -> None:
        self.service = service

    # POST /expenses
    def create_expense(self, body: dict[str, Any]) -> Response:
        try:
            payload = ExpenseCreate.model_validate(body or {})
            record = self.service.record_expense(
                payload, actor=str((body or {}).get("actor") or "user"), status="draft"
            )
        except Exception as exc:  # noqa: BLE001 - 统一转成响应
            return _error(exc)
        return Response(201, record.model_dump())

    # POST /expenses/query
    def query_expenses(self, body: dict[str, Any]) -> Response:
        try:
            result = self.service.query_expenses(ExpenseQuery.model_validate(body or {}))
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return Response(200, result.model_dump())

    # POST /expenses/{id}/confirm
    def confirm_expense(self, txn_id: int) -> Response:
        try:
            record = self.service.confirm(txn_id, actor="user")
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return Response(200, record.model_dump())

    # GET /expenses/{id}
    def get_expense(self, txn_id: int) -> Response:
        try:
            record = self.service.read(txn_id)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return Response(200, record.model_dump())
