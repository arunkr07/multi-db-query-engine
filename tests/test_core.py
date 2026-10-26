import sqlite3

from multi_db_query_engine.config import Settings
from multi_db_query_engine.core import QueryOrchestrator
from multi_db_query_engine.models import (
    DatabaseType,
    OperationPlan,
    OperationType,
    PlannedOperation,
    QueryStatus,
)


def make_core(tmp_path, approval_callback=None):
    path = tmp_path / "core.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO orders (id,status) VALUES (1,'paid')")
    settings = Settings(_env_file=None, databases=f'{{"local":{{"type":"sqlite","path":"{path.as_posix()}"}}}}')
    core = QueryOrchestrator(settings, approval_callback=approval_callback)
    return core


def test_core_read_path(tmp_path):
    core = make_core(tmp_path)
    core.planner.plan = lambda *args: OperationPlan(explanation="read", operations=[
        PlannedOperation(database="local", database_type=DatabaseType.SQLITE, operation_type=OperationType.READ,
                         statement="SELECT id,status FROM orders WHERE status=:status", parameters={"status":"paid"})
    ])
    try:
        response = core.ask("show paid orders")
        assert response.status == QueryStatus.COMPLETED
        assert response.data["local"] == [{"id": 1, "status": "paid"}]
    finally:
        core.close()


def test_write_requires_approval_then_executes(tmp_path):
    core = make_core(tmp_path)
    core.planner.plan = lambda *args: OperationPlan(explanation="insert", operations=[
        PlannedOperation(database="local", database_type=DatabaseType.SQLITE, operation_type=OperationType.WRITE,
                         statement="INSERT INTO orders(id,status) VALUES(:id,:status)", parameters={"id": 2, "status": "pending"})
    ])
    try:
        response = core.ask("insert an order")
        assert response.status == QueryStatus.APPROVAL_REQUIRED
        operation_id = response.data["operation_id"]
        approved = core.approve_and_execute(operation_id)
        assert approved.status == QueryStatus.COMPLETED
        with sqlite3.connect(core.registry.get("local").config.path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM orders WHERE id=2").fetchone()[0] == 1
    finally:
        core.close()


def test_rejected_callback_executes_nothing_and_cleans_pending(tmp_path):
    core = make_core(tmp_path, approval_callback=lambda summary: False)
    core.planner.plan = lambda *args: OperationPlan(explanation="insert", operations=[
        PlannedOperation(database="local", database_type=DatabaseType.SQLITE, operation_type=OperationType.WRITE,
                         statement="INSERT INTO orders(id,status) VALUES(:id,:status)", parameters={"id": 2, "status": "pending"})
    ])
    try:
        response = core.ask("insert an order")
        assert response.status == QueryStatus.APPROVAL_REQUIRED
        assert core.list_pending_operations() == []
        with sqlite3.connect(core.registry.get("local").config.path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM orders WHERE id=2").fetchone()[0] == 0
    finally:
        core.close()


def test_callback_exception_executes_nothing(tmp_path):
    def callback(_):
        raise RuntimeError("callback secret should not be returned")
    core = make_core(tmp_path, approval_callback=callback)
    core.planner.plan = lambda *args: OperationPlan(explanation="insert", operations=[
        PlannedOperation(database="local", database_type=DatabaseType.SQLITE, operation_type=OperationType.WRITE,
                         statement="INSERT INTO orders(id,status) VALUES(:id,:status)", parameters={"id": 2, "status": "pending"})
    ])
    try:
        response = core.ask("insert an order")
        assert response.status == QueryStatus.FAILED
        assert "secret" not in response.answer
        assert core.list_pending_operations() == []
    finally:
        core.close()


def test_closed_core_rejects_requests(tmp_path):
    core = make_core(tmp_path)
    core.close()
    try:
        try:
            core.ask("show orders")
        except RuntimeError as exc:
            assert "closed" in str(exc).lower()
        else:
            raise AssertionError("closed core should reject requests")
    finally:
        core.close()

def test_sql_injection_payload_is_treated_as_parameter(tmp_path):
    core = make_core(tmp_path)

    malicious_input = "' OR 1=1 --"
    core.planner.plan = lambda *args: OperationPlan(
        explanation="read",
        operations=[
            PlannedOperation(
                database="local",
                database_type=DatabaseType.SQLITE,
                operation_type=OperationType.READ,
                statement="SELECT id, status FROM orders WHERE status=:status",
                parameters={"status": malicious_input},
            )
        ],
    )

    try:
        response = core.ask("show orders with a specific status")

        assert response.status == QueryStatus.NO_DATA
        assert response.data["local"] == []
    finally:
        core.close()


def test_database_failure_returns_safe_response(tmp_path):
    core = make_core(tmp_path)

    core.planner.plan = lambda *args: OperationPlan(
        explanation="read",
        operations=[
            PlannedOperation(
                database="local",
                database_type=DatabaseType.SQLITE,
                operation_type=OperationType.READ,
                statement="SELECT id, status FROM orders",
            )
        ],
    )

    def fail_execution(*args, **kwargs):
        from multi_db_query_engine.exceptions import DatabaseExecutionError
        raise DatabaseExecutionError("Sensitive database details")

    core.registry.get("local").execute = fail_execution

    try:
        response = core.ask("show orders")

        assert response.status == QueryStatus.FAILED
        assert "Sensitive database details" not in response.answer
        assert response.answer == (
            "Request failed. Check sanitized application logs."
        )
    finally:
        core.close()


def test_approved_write_failure_is_not_automatically_retried(tmp_path):
    core = make_core(tmp_path)

    core.planner.plan = lambda *args: OperationPlan(
        explanation="update",
        operations=[
            PlannedOperation(
                database="local",
                database_type=DatabaseType.SQLITE,
                operation_type=OperationType.WRITE,
                statement="UPDATE orders SET status=:status WHERE id=:id",
                parameters={"status": "shipped", "id": 1},
            )
        ],
    )

    calls = {"count": 0}

    def fail_execution(*args, **kwargs):
        from multi_db_query_engine.exceptions import DatabaseExecutionError
        calls["count"] += 1
        raise DatabaseExecutionError("Sensitive database details")

    core.registry.get("local").execute = fail_execution

    try:
        response = core.ask("change order 1 status to shipped")
        assert response.status == QueryStatus.APPROVAL_REQUIRED

        operation_id = response.data["operation_id"]
        result = core.approve_and_execute(operation_id)

        assert result.status == QueryStatus.FAILED
        assert calls["count"] == 1
        assert "Sensitive database details" not in result.answer
    finally:
        core.close()

def test_close_waits_for_inflight_request_before_closing_resources(tmp_path):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    core = make_core(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def slow_plan(*args):
        entered.set()
        assert release.wait(2)
        return OperationPlan(
            explanation="read",
            operations=[PlannedOperation(
                database="local", database_type=DatabaseType.SQLITE,
                operation_type=OperationType.READ, statement="SELECT id FROM orders",
            )],
        )

    core.planner.plan = slow_plan
    with ThreadPoolExecutor(max_workers=2) as pool:
        request = pool.submit(core.ask, "show orders")
        assert entered.wait(1)
        close = pool.submit(core.close)
        time.sleep(0.05)
        assert not close.done()
        release.set()
        assert request.result(timeout=2).status == QueryStatus.COMPLETED
        close.result(timeout=2)
    assert core.closed


def test_concurrent_close_calls_are_idempotent(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    core = make_core(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: core.close(), range(2)))
    assert results == [None, None]
    assert core.closed
