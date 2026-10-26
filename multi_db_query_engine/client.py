from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Any, Self

from multi_db_query_engine.config import Settings, get_settings
from multi_db_query_engine.core import QueryOrchestrator
from multi_db_query_engine.models import QueryResponse


class MultiDBQueryEngine:
    """Synchronous public client for natural-language multi-database queries."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        approval_callback: Callable[[dict[str, Any]], bool] | None = None,
        approval_ttl_seconds: int = 300,
    ) -> None:
        self._core = QueryOrchestrator(
            settings or get_settings(),
            approval_callback,
            approval_ttl_seconds,
        )

    @property
    def databases(self) -> list[str]:
        return self._core.registry.names

    @property
    def schemas(self) -> dict[str, dict[str, Any]]:
        return {
            name: dict(schema)
            for name, schema in self._core.registry.schemas.items()
        }

    def ask(
        self,
        question: str,
        *,
        database: str | None = None,
    ) -> QueryResponse:
        return self._core.ask(question, database)

    def get_pending_operation(self, operation_id: str) -> dict[str, Any]:
        return self._core.get_pending_operation(operation_id)

    def list_pending_operations(self) -> list[dict[str, Any]]:
        return self._core.list_pending_operations()

    def approve_and_execute(self, operation_id: str) -> QueryResponse:
        return self._core.approve_and_execute(operation_id)

    def close(self) -> None:
        self._core.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()