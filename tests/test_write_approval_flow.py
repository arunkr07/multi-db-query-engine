import json
import sqlite3
from types import SimpleNamespace

import pytest

from multi_db_query_engine.client import MultiDBQueryEngine
from multi_db_query_engine.config import Settings
from multi_db_query_engine.models import QueryStatus


def make_write_plan():
    return json.dumps(
        {
            "explanation": "Update the order status",
            "requires_write_approval": True,
            "operations": [
                {
                    "database": "local",
                    "database_type": "sqlite",
                    "operation_type": "write",
                    "statement": (
                        "UPDATE orders SET status=:status WHERE id=:id"
                    ),
                    "parameters": {"status": "shipped", "id": 1},
                }
            ],
        }
    )


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "approval_test.sqlite"

    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)"
        )
        conn.execute(
            "INSERT INTO orders (id, status) VALUES (1, 'pending')"
        )

    return path


def make_engine(database, monkeypatch):
    from multi_db_query_engine.models import (
        DatabaseType,
        OperationType,
        PlannedOperation,
    )

    settings = Settings(
        _env_file=None,
        openai_api_key="test-key",
        databases=json.dumps(
            {"local": {"type": "sqlite", "path": str(database)}}
        ),
    )

    engine = MultiDBQueryEngine(settings=settings)

    operation = PlannedOperation(
        database="local",
        database_type=DatabaseType.SQLITE,
        operation_type=OperationType.WRITE,
        statement="UPDATE orders SET status=:status WHERE id=:id",
        parameters={"status": "shipped", "id": 1},
    )

    fake_plan = SimpleNamespace(
        requires_write_approval=True,
        operations=[operation],
    )

    monkeypatch.setattr(
        engine._core,
        "planner",
        SimpleNamespace(
            plan=lambda *args, **kwargs: fake_plan,
            close=lambda: None,
        ),
    )

    return engine


def read_status(database):
    with sqlite3.connect(database) as conn:
        return conn.execute(
            "SELECT status FROM orders WHERE id=1"
        ).fetchone()[0]


def test_write_remains_pending_until_explicit_approval(database, monkeypatch):
    engine = make_engine(database, monkeypatch)

    try:
        response = engine.ask("Mark order 1 as shipped")

        assert response.status == QueryStatus.APPROVAL_REQUIRED
        assert read_status(database) == "pending"

        operation_id = response.data["operation_id"]
        pending = engine.get_pending_operation(operation_id)

        assert pending["operation_id"] == operation_id
        assert read_status(database) == "pending"
    finally:
        engine.close()


def test_explicit_approval_executes_write_once(database, monkeypatch):
    engine = make_engine(database, monkeypatch)

    try:
        response = engine.ask("Mark order 1 as shipped")
        assert response.status == QueryStatus.APPROVAL_REQUIRED
        assert read_status(database) == "pending"

        operation_id = response.data["operation_id"]
        approved = engine.approve_and_execute(operation_id)

        assert approved.status == QueryStatus.COMPLETED
        assert read_status(database) == "shipped"

        repeated = engine.approve_and_execute(operation_id)

        assert repeated.status == QueryStatus.FAILED
        assert read_status(database) == "shipped"
    finally:
        engine.close()