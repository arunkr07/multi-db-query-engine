from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from multi_db_query_engine.approval import ApprovalManager, PendingOperation
from multi_db_query_engine.exceptions import ApprovalError
from multi_db_query_engine.models import DatabaseType, OperationType, PlannedOperation


def write_op():
    return PlannedOperation(database="local", database_type=DatabaseType.SQLITE, operation_type=OperationType.WRITE,
                            statement="UPDATE x SET y=:y", parameters={"y": 2})


def test_approval_is_single_use():
    manager = ApprovalManager(); pending = manager.create(write_op())
    assert manager.consume(pending.operation_id) == pending.operation
    with pytest.raises(ApprovalError): manager.consume(pending.operation_id)


def test_expired_approval_rejected_and_removed():
    manager = ApprovalManager(); pending = manager.create(write_op())
    manager.pending[pending.operation_id] = PendingOperation(pending.operation_id, pending.created_at,
        datetime.now(UTC)-timedelta(seconds=1), pending.operation, pending.digest)
    with pytest.raises(ApprovalError, match="expired"): manager.get(pending.operation_id)
    assert pending.operation_id not in manager.pending


def test_unknown_operation_rejected():
    with pytest.raises(ApprovalError, match="Unknown"): ApprovalManager().get("missing")


def test_read_operation_cannot_be_approved():
    manager = ApprovalManager()
    read = PlannedOperation(database="local", database_type=DatabaseType.SQLITE, operation_type=OperationType.READ,
                            statement="SELECT 1")
    with pytest.raises(ApprovalError): manager.create(read)


def test_cancel_pending_operation():
    manager = ApprovalManager(); pending = manager.create(write_op())
    manager.cancel(pending.operation_id)
    assert manager.summaries() == []
    with pytest.raises(ApprovalError): manager.get(pending.operation_id)

def test_modified_pending_operation_is_rejected():
    manager = ApprovalManager()
    pending = manager.create(write_op())

    # Simulate tampering with the stored operation.
    original = pending.operation
    modified = original.model_copy(
        update={"statement": "UPDATE x SET y=999"}
    )

    manager.pending[pending.operation_id] = PendingOperation(
        operation_id=pending.operation_id,
        created_at=pending.created_at,
        expires_at=pending.expires_at,
        operation=modified,
        digest=pending.digest,
    )

    with pytest.raises(ApprovalError, match="changed after approval"):
        manager.get(pending.operation_id)


def test_concurrent_consumption_is_single_use():
    manager = ApprovalManager()
    pending = manager.create(write_op())

    def consume_once(_):
        try:
            manager.consume(pending.operation_id)
            return "consumed"
        except ApprovalError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(consume_once, range(100)))

    assert outcomes.count("consumed") == 1
    assert outcomes.count("rejected") == 99
