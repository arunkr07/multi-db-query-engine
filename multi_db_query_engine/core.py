from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from multi_db_query_engine.ai import OpenAIQueryPlanner
from multi_db_query_engine.approval import ApprovalManager
from multi_db_query_engine.config import Settings
from multi_db_query_engine.databases import DatabaseRegistry
from multi_db_query_engine.exceptions import (
    ApprovalError,
    DatabaseExecutionError,
    QueryPlanningError,
    UnsupportedOperationError,
)
from multi_db_query_engine.models import OperationType, QueryRequest, QueryResponse, QueryStatus
from multi_db_query_engine.observability import get_logger, log_event
from multi_db_query_engine.security import DEFAULT_EXECUTION_LIMITS


def _lifecycle_guard(method):
    """Keep client resources alive until in-flight public operations finish."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        thread_id = threading.get_ident()
        with self._lifecycle_condition:
            if (self._closing or self.closed) and thread_id not in self._active_threads:
                raise RuntimeError("Client is closing or closed")
            self._active_operations += 1
            self._active_threads[thread_id] = self._active_threads.get(thread_id, 0) + 1
        try:
            return method(self, *args, **kwargs)
        finally:
            with self._lifecycle_condition:
                self._active_operations -= 1
                count = self._active_threads.get(thread_id, 0) - 1
                if count <= 0:
                    self._active_threads.pop(thread_id, None)
                else:
                    self._active_threads[thread_id] = count
                self._lifecycle_condition.notify_all()
    return wrapper


class QueryOrchestrator:
    def __init__(self, settings: Settings, approval_callback: Callable[[dict[str, Any]], bool] | None = None, approval_ttl_seconds: int = 300):
        self.settings = settings
        self.logger = get_logger(settings.log_level)
        self.limits = DEFAULT_EXECUTION_LIMITS
        self.registry = DatabaseRegistry(settings.database_configs())
        try:
            self.planner = OpenAIQueryPlanner(settings)
        except Exception:
            self.registry.close()
            raise
        self.approvals = ApprovalManager(approval_ttl_seconds)
        self.approval_callback = approval_callback
        self._lifecycle_condition = threading.Condition(threading.RLock())
        self._active_operations = 0
        self._active_threads: dict[int, int] = {}
        self._closing = False
        self.closed = False
        self.types = {name: self.registry.get(name).config.type for name in self.registry.names}

    @_lifecycle_guard
    def ask(self, question: str, database: str | None = None) -> QueryResponse:
        if self.closed:
            raise RuntimeError("Client is closed")
        req = QueryRequest(question=question, database=database)
        if req.database and req.database not in self.registry.names:
            return QueryResponse(status=QueryStatus.NEEDS_CLARIFICATION, answer=f"Database {req.database!r} is not configured.")
        if not self.registry.names:
            return QueryResponse(status=QueryStatus.FAILED, answer="No databases configured. Set MQE_DATABASES.")
        try:
            plan = self.planner.plan(req.question, self.registry.schemas, self.types, req.database)
            if isinstance(plan, dict) and plan.get("clarification"):
                return QueryResponse(status=QueryStatus.NEEDS_CLARIFICATION, answer=str(plan["clarification"]))
            if plan.requires_write_approval:
                if any(op.operation_type == OperationType.READ for op in plan.operations):
                    raise QueryPlanningError("Mixed read/write plans are unsupported; split the request")
                return self._handle_write_plan(plan.operations)

            results: dict[str, list[dict[str, Any]]] = {}
            deadline = time.monotonic() + self.limits.request_timeout_seconds
            for op in plan.operations:
                if op.operation_type != OperationType.READ:
                    raise QueryPlanningError("Unexpected write in read plan")
                attempts = 0
                while True:
                    try:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Request deadline exceeded")
                        rows = self._execute(op)
                        results.setdefault(op.database, []).extend(rows)
                        break
                    except DatabaseExecutionError as exc:
                        if not exc.transient or attempts >= self.limits.read_retries:
                            raise
                        attempts += 1
                        sleep_for = min(0.1 * (2 ** (attempts - 1)) + random.random() * 0.03, 0.5)
                        if time.monotonic() + sleep_for >= deadline:
                            raise TimeoutError("Request deadline exceeded") from exc
                        time.sleep(sleep_for)
            count = sum(len(rows) for rows in results.values())
            return QueryResponse(status=QueryStatus.COMPLETED if count else QueryStatus.NO_DATA,
                                 answer=f"Found {count} row(s) across {len(results)} database result group(s).", data=results)
        except (QueryPlanningError, UnsupportedOperationError) as exc:
            log_event(self.logger, logging.WARNING, "query rejected", error_type=type(exc).__name__)
            return QueryResponse(status=QueryStatus.NEEDS_CLARIFICATION, answer=str(exc))
        except Exception as exc:  # noqa: BLE001
            log_event(self.logger, logging.ERROR, "query failed", error_type=type(exc).__name__)
            return QueryResponse(status=QueryStatus.FAILED, answer="Request failed. Check sanitized application logs.")

    def _handle_write_plan(self, operations: list[Any]) -> QueryResponse:
        pending = [self.approvals.create(op) for op in operations]
        summaries = [item.summary() for item in pending]
        ids = [item.operation_id for item in pending]
        if not self.approval_callback:
            return QueryResponse(status=QueryStatus.APPROVAL_REQUIRED,
                                 answer="Review pending operations and approve each operation ID.",
                                 data={"operation_id": ids[0], "operations": summaries})
        try:
            decisions = [bool(self.approval_callback(summary)) for summary in summaries]
        except Exception as exc:  # noqa: BLE001
            self.approvals.cancel_many(ids)
            log_event(self.logger, logging.WARNING, "approval callback failed", error_type=type(exc).__name__)
            return QueryResponse(status=QueryStatus.FAILED, answer="Approval callback failed; no operations were executed.")
        if not all(decisions):
            self.approvals.cancel_many(ids)
            return QueryResponse(status=QueryStatus.APPROVAL_REQUIRED,
                                 answer="At least one write operation was not approved; no operations were executed.",
                                 data={"operations": summaries, "executed": []})

        results: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for item in pending:
            try:
                operation = self.approvals.consume(item.operation_id)
                operation_results = self._execute(operation)
                results.append({"operation_id": item.operation_id, "database": operation.database, "results": operation_results})
            except Exception as exc:  # noqa: BLE001
                failures.append({"operation_id": item.operation_id, "database": item.operation.database, "error_type": type(exc).__name__})
                # Cancel operations in the remaining unexecuted batch; never auto-retry writes.
                remaining = [p.operation_id for p in pending if p.operation_id != item.operation_id and p.operation_id in self.approvals.pending]
                self.approvals.cancel_many(remaining)
                break
        if failures:
            status = QueryStatus.PARTIAL if results else QueryStatus.FAILED
            return QueryResponse(status=status, answer="Write batch did not fully complete; inspect per-operation results.",
                                 data={"completed": results, "failed": failures, "remaining_cancelled": True})
        return QueryResponse(status=QueryStatus.COMPLETED, answer="Approved write operations completed.", data={"results": results})

    def _execute(self, op) -> list[dict[str, Any]]:
        adapter = self.registry.get(op.database)
        if adapter.config.type != op.database_type:
            raise QueryPlanningError("Database type mismatch")
        return adapter.execute(op, self.limits.max_result_rows, self.limits.database_timeout_seconds)

    @_lifecycle_guard
    def get_pending_operation(self, operation_id: str) -> dict[str, Any]:
        return self.approvals.get(operation_id).summary()

    @_lifecycle_guard
    def list_pending_operations(self) -> list[dict[str, Any]]:
        return self.approvals.summaries()

    @_lifecycle_guard
    def approve_and_execute(self, operation_id: str) -> QueryResponse:
        if self.closed:
            raise RuntimeError("Client is closed")
        try:
            pending = self.approvals.get(operation_id)
            adapter = self.registry.get(pending.operation.database)
            if adapter.config.type != pending.operation.database_type:
                raise ApprovalError("Database configuration mismatch")
            operation = self.approvals.consume(operation_id)
            result = self._execute(operation)
            return QueryResponse(status=QueryStatus.COMPLETED, answer="Approved write completed.",
                                 data={"database": operation.database, "results": result})
        except ApprovalError as exc:
            return QueryResponse(status=QueryStatus.FAILED, answer=str(exc))
        except Exception as exc:  # noqa: BLE001
            log_event(self.logger, logging.ERROR, "approved operation failed", error_type=type(exc).__name__)
            return QueryResponse(status=QueryStatus.FAILED, answer="Approved operation failed; it was not automatically retried.")

    def close(self) -> None:
        thread_id = threading.get_ident()
        with self._lifecycle_condition:
            if self.closed:
                return
            if self._active_threads.get(thread_id, 0):
                raise RuntimeError("Cannot close the client from inside an active SDK operation")
            if self._closing:
                while not self.closed:
                    self._lifecycle_condition.wait()
                return
            self._closing = True
            while self._active_operations:
                self._lifecycle_condition.wait()
        try:
            self.planner.close()
        finally:
            try:
                self.registry.close()
            finally:
                with self._lifecycle_condition:
                    self.closed = True
                    self._closing = False
                    self._lifecycle_condition.notify_all()
