from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self

from multi_db_query_engine.client import MultiDBQueryEngine
from multi_db_query_engine.config import Settings
from multi_db_query_engine.models import QueryResponse


class AsyncMultiDBQueryEngine:
    """Async facade over the synchronous SDK.

    Calls run in worker threads. Cancelling an awaiting task does not stop
    a database operation already running in a worker thread.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        approval_callback: Callable[[dict[str, Any]], bool] | None = None,
        approval_ttl_seconds: int = 300,
    ) -> None:
        self._client = MultiDBQueryEngine(
            settings=settings,
            approval_callback=approval_callback,
            approval_ttl_seconds=approval_ttl_seconds,
        )

    @property
    def databases(self) -> list[str]:
        return self._client.databases

    @property
    def schemas(self) -> dict[str, dict[str, Any]]:
        return self._client.schemas

    async def ask(
        self,
        question: str,
        *,
        database: str | None = None,
    ) -> QueryResponse:
        return await asyncio.to_thread(
            self._client.ask,
            question,
            database=database,
        )

    async def get_pending_operation(self, operation_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._client.get_pending_operation,
            operation_id,
        )

    async def list_pending_operations(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._client.list_pending_operations,
        )

    async def approve_and_execute(self, operation_id: str) -> QueryResponse:
        return await asyncio.to_thread(
            self._client.approve_and_execute,
            operation_id,
        )

    async def aclose(self) -> None:
        await asyncio.to_thread(self._client.close)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
    self,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
) -> None:
        await self.aclose()