"""Pydantic schema:唯一真源。

同一份模型同时生成:
  * REST 请求体(la/domain/routers/)
  * LLM 工具的 JSON Schema(la/domain/tools.py)
手写第二份 schema 即为 bug(能力域 §6.3)。
"""

from .study import QuizMe, QuizResult  # noqa: F401
from .calendar import (  # noqa: F401
    CalendarCancel,
    CalendarEventCreate,
    CalendarEventRead,
    CalendarList,
    CalendarQueryResult,
    CalendarReschedule,
    OccurrenceRead,
)
from .ledger import (
    ExpenseCorrect,
    ExpenseCreate,
    ExpenseImport,
    ExpenseQuery,
    ExpenseQueryResult,
    ExpenseRead,
    GroupRow,
)

__all__ = [
    "ExpenseCorrect",
    "ExpenseCreate",
    "ExpenseImport",
    "ExpenseQuery",
    "ExpenseQueryResult",
    "ExpenseRead",
    "GroupRow",
]
