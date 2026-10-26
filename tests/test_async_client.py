import asyncio
import sqlite3

import pytest

from multi_db_query_engine import (
    AsyncMultiDBQueryEngine,
    DatabaseType,
    OperationPlan,
    OperationType,
    PlannedOperation,
    QueryStatus,
    Settings,
)


def make_async_client(tmp_path):
    path = tmp_path / "async.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT)")
        conn.executemany("INSERT INTO items(label) VALUES (?)", [("one",), ("two",)])
    settings = Settings(_env_file=None, databases=f'{{"local":{{"type":"sqlite","path":"{path.as_posix()}"}}}}')
    client = AsyncMultiDBQueryEngine(settings=settings)
    client._client._core.planner.plan = lambda *args: OperationPlan(
        explanation="read",
        operations=[PlannedOperation(
            database="local", database_type=DatabaseType.SQLITE,
            operation_type=OperationType.READ, statement="SELECT id, label FROM items ORDER BY id",
        )],
    )
    return client


@pytest.mark.asyncio
async def test_async_client_handles_concurrent_requests_and_closes(tmp_path):
    client = make_async_client(tmp_path)
    try:
        responses = await asyncio.gather(*(client.ask("show items") for _ in range(8)))
        assert all(response.status == QueryStatus.COMPLETED for response in responses)
        assert all(len(response.data["local"]) == 2 for response in responses)
    finally:
        await client.aclose()
    with pytest.raises(RuntimeError, match="closed|closing"):
        await client.ask("show items")
