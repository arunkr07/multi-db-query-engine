from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from multi_db_query_engine.exceptions import ApprovalError
from multi_db_query_engine.models import OperationType, PlannedOperation


@dataclass(frozen=True)
class PendingOperation:
    operation_id: str
    created_at: datetime
    expires_at: datetime
    operation: PlannedOperation
    digest: str

    def summary(self) -> dict[str, Any]:
        op = self.operation
        return {
            "operation_id": self.operation_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "database": op.database,
            "database_type": op.database_type.value,
            "operation_type": op.operation_type.value,
            "statement": op.statement,
            "parameters": op.parameters,
            "collection": op.collection,
            "filter": op.filter,
            "document": op.document,
            "update": op.update,
            "key": op.key,
            "value": op.value,
            "explanation": op.explanation,
            "plan_digest": self.digest,
        }


class ApprovalManager:
    def __init__(self, ttl_seconds: int = 300):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.ttl = ttl_seconds
        self.pending: dict[str, PendingOperation] = {}
        self.consumed: set[str] = set()
        # Approval state is shared by sync callers and async worker threads.
        self._lock = threading.RLock()

    @staticmethod
    def _digest(op: PlannedOperation) -> str:
        payload = json.dumps(op.model_dump(mode="json"), sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def create(self, op: PlannedOperation) -> PendingOperation:
        if op.operation_type != OperationType.WRITE:
            raise ApprovalError("Only write operations require approval")
        now = datetime.now(UTC)
        item = PendingOperation(
            secrets.token_urlsafe(24), now, now + timedelta(seconds=self.ttl), op, self._digest(op)
        )
        with self._lock:
            self.pending[item.operation_id] = item
        return item

    def get(self, op_id: str) -> PendingOperation:
        with self._lock:
            if op_id in self.consumed:
                raise ApprovalError("Operation already processed")
            item = self.pending.get(op_id)
            if item is None:
                raise ApprovalError("Unknown operation ID")
            if datetime.now(UTC) >= item.expires_at:
                self.pending.pop(op_id, None)
                raise ApprovalError("Operation approval expired")
            if self._digest(item.operation) != item.digest:
                raise ApprovalError("Operation changed after approval request")
            return item

    def consume(self, op_id: str) -> PlannedOperation:
        # Keep validation and state transition in one critical section so
        # concurrent callers cannot both consume the same approval.
        with self._lock:
            item = self.get(op_id)
            self.pending.pop(op_id, None)
            self.consumed.add(op_id)
            return item.operation

    def cancel(self, op_id: str) -> None:
        """Remove an unconsumed pending operation (e.g. a rejected batch)."""
        with self._lock:
            if op_id in self.consumed:
                raise ApprovalError("Cannot cancel an operation already processed")
            self.pending.pop(op_id, None)

    def cancel_many(self, operation_ids: list[str]) -> None:
        for operation_id in operation_ids:
            self.cancel(operation_id)

    def summaries(self) -> list[dict[str, Any]]:
        with self._lock:
            now = datetime.now(UTC)
            expired = [key for key, item in self.pending.items() if item.expires_at <= now]
            for key in expired:
                self.pending.pop(key, None)
            return [item.summary() for item in self.pending.values()]
