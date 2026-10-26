import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from multi_db_query_engine.databases import SQLAdapter
from multi_db_query_engine.exceptions import (
    DatabaseExecutionError,
    SecurityViolationError,
)
from multi_db_query_engine.models import (
    DatabaseConfig,
    DatabaseType,
    OperationType,
    PlannedOperation,
)


def test_sqlite_read_and_schema(tmp_path):
    path = tmp_path / "db.sqlite"

    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE orders(id INTEGER, status TEXT)")
        conn.execute("INSERT INTO orders VALUES(1, 'paid')")

    adapter = SQLAdapter(
        "local",
        DatabaseConfig(type=DatabaseType.SQLITE, path=str(path)),
    )

    try:
        assert "orders" in adapter.discover_schema()

        op = PlannedOperation(
            database="local",
            database_type=DatabaseType.SQLITE,
            operation_type=OperationType.READ,
            statement="SELECT id, status FROM orders WHERE status=:status",
            parameters={"status": "paid"},
        )

        assert adapter.execute(op, 100, 10) == [
            {"id": 1, "status": "paid"}
        ]
    finally:
        adapter.close()


def test_sqlite_write_execution_is_supported_at_adapter_layer(tmp_path):
    path = tmp_path / "db.sqlite"

    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER)")

    adapter = SQLAdapter(
        "local",
        DatabaseConfig(type=DatabaseType.SQLITE, path=str(path)),
    )

    try:
        op = PlannedOperation(
            database="local",
            database_type=DatabaseType.SQLITE,
            operation_type=OperationType.WRITE,
            statement="INSERT INTO t(id) VALUES(:id)",
            parameters={"id": 7},
        )

        assert adapter.execute(op, 100, 10) == [{"affected_rows": 1}]
    finally:
        adapter.close()

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT id FROM t").fetchall() == [(7,)]


def test_sqlite_rejects_missing_parameter(tmp_path):
    adapter = SQLAdapter(
        "local",
        DatabaseConfig(type=DatabaseType.SQLITE, path=str(tmp_path / "db.sqlite")),
    )

    try:
        op = PlannedOperation(
            database="local",
            database_type=DatabaseType.SQLITE,
            operation_type=OperationType.READ,
            statement="SELECT :required",
        )

        with pytest.raises(SecurityViolationError):
            adapter.execute(op, 10, 2)
    finally:
        adapter.close()


def test_row_limit_is_enforced(tmp_path):
    path = tmp_path / "db.sqlite"

    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.executemany(
            "INSERT INTO t VALUES(?)",
            [(i,) for i in range(10)],
        )

    adapter = SQLAdapter(
        "local",
        DatabaseConfig(type=DatabaseType.SQLITE, path=str(path)),
    )

    try:
        op = PlannedOperation(
            database="local",
            database_type=DatabaseType.SQLITE,
            operation_type=OperationType.READ,
            statement="SELECT id FROM t",
            limit=3,
        )

        assert len(adapter.execute(op, 100, 10)) == 3
    finally:
        adapter.close()


def test_concurrent_operations_on_shared_sqlite_connection_are_serialized(
    tmp_path,
):
    path = tmp_path / "concurrent.sqlite"

    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE counters (n INTEGER NOT NULL)")
        conn.execute("INSERT INTO counters VALUES (0)")

    adapter = SQLAdapter(
        "local",
        DatabaseConfig(type=DatabaseType.SQLITE, path=str(path)),
    )

    op = PlannedOperation(
        database="local",
        database_type=DatabaseType.SQLITE,
        operation_type=OperationType.WRITE,
        statement="UPDATE counters SET n = n + 1",
    )

    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(
                pool.map(
                    lambda _: adapter.execute(op, 100, 10),
                    range(50),
                )
            )

        assert all(
            result == [{"affected_rows": 1}]
            for result in results
        )
    finally:
        adapter.close()

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT n FROM counters"
        ).fetchone()[0] == 50


def test_sqlite_authorizer_rejects_unknown_table_even_if_static_guard_is_bypassed(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "guard.sqlite"

    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE known(id INTEGER)")
        conn.execute("CREATE TABLE secret(id INTEGER)")
        conn.execute("INSERT INTO secret VALUES(42)")

    adapter = SQLAdapter(
        "local",
        DatabaseConfig(type=DatabaseType.SQLITE, path=str(path)),
    )

    try:
        adapter.discover_schema()

        # Bypass the static schema validator to test SQLite's own authorizer.
        monkeypatch.setattr(
            "multi_db_query_engine.databases.validate_sql_schema",
            lambda *args: None,
        )

        adapter.schema = {"known": [{"name": "id", "type": "INTEGER"}]}

        op = PlannedOperation(
            database="local",
            database_type=DatabaseType.SQLITE,
            operation_type=OperationType.READ,
            statement="SELECT id FROM secret",
        )

        with pytest.raises(DatabaseExecutionError):
            adapter.execute(op, 100, 10)
    finally:
        adapter.close()


def test_sqlite_read_cannot_call_dangerous_file_functions(tmp_path):
    path = tmp_path / "guard.sqlite"

    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE known(id INTEGER)")

    adapter = SQLAdapter(
        "local",
        DatabaseConfig(type=DatabaseType.SQLITE, path=str(path)),
    )

    try:
        adapter.discover_schema()

        op = PlannedOperation(
            database="local",
            database_type=DatabaseType.SQLITE,
            operation_type=OperationType.READ,
            statement="SELECT readfile('demo.db') AS contents",
        )

        with pytest.raises(DatabaseExecutionError):
            adapter.execute(op, 100, 10)
    finally:
        adapter.close()