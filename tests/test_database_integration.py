from __future__ import annotations

import os
import uuid

import pytest

from multi_db_query_engine.databases import (
    MongoAdapter,
    RedisAdapter,
    SQLAdapter,
)
from multi_db_query_engine.exceptions import UnsupportedOperationError
from multi_db_query_engine.models import (
    DatabaseConfig,
    DatabaseType,
    OperationType,
    PlannedOperation,
)

pytestmark = pytest.mark.skipif(
    os.getenv("MQE_RUN_INTEGRATION") != "1",
    reason="Set MQE_RUN_INTEGRATION=1 to run Docker database integration tests",
)

PG_DSN = (
    "postgresql://mqe_test:mqe_local_test_password"
    "@127.0.0.1:55432/mqe_test"
)
MYSQL_DSN = (
    "mysql://mqe_test:mqe_local_test_password"
    "@127.0.0.1:53306/mqe_test"
)
MONGO_URI = "mongodb://127.0.0.1:57017"
MONGO_DB = "mqe_integration_test"
MONGO_COLLECTION = "mqe_integration_items"
REDIS_URL = "redis://127.0.0.1:56379/0"
REDIS_PREFIX = "mqe:integration:"


def sql_operation(
    database: str,
    operation_type: OperationType,
    statement: str,
    parameters: dict | None = None,
) -> PlannedOperation:
    return PlannedOperation(
        database=database,
        database_type=(
            DatabaseType.POSTGRESQL
            if database == "postgres"
            else DatabaseType.MYSQL
        ),
        operation_type=operation_type,
        statement=statement,
        parameters=parameters or {},
    )


@pytest.fixture
def postgres():
    adapter = SQLAdapter(
        "postgres",
        DatabaseConfig(type=DatabaseType.POSTGRESQL, dsn=PG_DSN),
    )
    try:
        # Create the dedicated test table directly through the driver.
        # The SDK correctly prohibits CREATE TABLE through its query interface.
        with adapter.connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS public.mqe_integration_items (
                    id TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
        adapter.connection.commit()

        adapter.discover_schema()
        yield adapter
    finally:
        adapter.close()


@pytest.fixture
def mysql():
    adapter = SQLAdapter(
        "mysql",
        DatabaseConfig(type=DatabaseType.MYSQL, dsn=MYSQL_DSN),
    )
    try:
        adapter.discover_schema()
        # Create this dedicated test table using the driver connection directly:
        # CREATE TABLE is intentionally outside the SDK's allowed write operations.
        with adapter.connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS mqe_integration_items (
                    id VARCHAR(100) PRIMARY KEY,
                    value VARCHAR(255) NOT NULL
                )
                """
            )
        adapter.connection.commit()
        adapter.discover_schema()
        yield adapter
    finally:
        adapter.close()


@pytest.fixture
def mongo():
    adapter = MongoAdapter(
        "mongo",
        DatabaseConfig(
            type=DatabaseType.MONGODB,
            uri=MONGO_URI,
            database=MONGO_DB,
            collection=MONGO_COLLECTION,
        ),
    )
    try:
        adapter.db[MONGO_COLLECTION].delete_many(
            {"_integration_test": True}
        )
        yield adapter
    finally:
        adapter.close()


@pytest.fixture
def redis_adapter():
    adapter = RedisAdapter(
        "redis",
        DatabaseConfig(
            type=DatabaseType.REDIS,
            url=REDIS_URL,
            key_prefix=REDIS_PREFIX,
        ),
    )
    yield adapter
    adapter.close()


def test_postgres_schema_and_parameterized_read(postgres):
    assert "public.mqe_integration_items" in postgres.schema

    item_id = f"pg-{uuid.uuid4().hex}"
    postgres.execute(
        sql_operation(
            "postgres",
            OperationType.WRITE,
            """
            INSERT INTO public.mqe_integration_items (id, value)
            VALUES (:id, :value)
            """,
            {"id": item_id, "value": "before"},
        ),
        max_rows=100,
        timeout_seconds=10,
    )

    rows = postgres.execute(
        sql_operation(
            "postgres",
            OperationType.READ,
            """
            SELECT id, value
            FROM public.mqe_integration_items
            WHERE id = :id
            """,
            {"id": item_id},
        ),
        max_rows=100,
        timeout_seconds=10,
    )

    assert rows == [{"id": item_id, "value": "before"}]

    postgres.execute(
        sql_operation(
            "postgres",
            OperationType.WRITE,
            """
            UPDATE public.mqe_integration_items
            SET value = :value
            WHERE id = :id
            """,
            {"id": item_id, "value": "after"},
        ),
        max_rows=100,
        timeout_seconds=10,
    )

    rows = postgres.execute(
        sql_operation(
            "postgres",
            OperationType.READ,
            """
            SELECT value
            FROM public.mqe_integration_items
            WHERE id = :id
            """,
            {"id": item_id},
        ),
        max_rows=100,
        timeout_seconds=10,
    )
    assert rows == [{"value": "after"}]


def test_mysql_schema_and_parameterized_read(mysql):
    assert "mqe_integration_items" in mysql.schema

    item_id = f"my-{uuid.uuid4().hex}"
    mysql.execute(
        sql_operation(
            "mysql",
            OperationType.WRITE,
            """
            INSERT INTO mqe_integration_items (id, value)
            VALUES (:id, :value)
            """,
            {"id": item_id, "value": "before"},
        ),
        max_rows=100,
        timeout_seconds=10,
    )

    rows = mysql.execute(
        sql_operation(
            "mysql",
            OperationType.READ,
            """
            SELECT id, value
            FROM mqe_integration_items
            WHERE id = :id
            """,
            {"id": item_id},
        ),
        max_rows=100,
        timeout_seconds=10,
    )
    assert rows == [{"id": item_id, "value": "before"}]

    mysql.execute(
        sql_operation(
            "mysql",
            OperationType.WRITE,
            """
            UPDATE mqe_integration_items
            SET value = :value
            WHERE id = :id
            """,
            {"id": item_id, "value": "after"},
        ),
        max_rows=100,
        timeout_seconds=10,
    )

    rows = mysql.execute(
        sql_operation(
            "mysql",
            OperationType.READ,
            """
            SELECT value
            FROM mqe_integration_items
            WHERE id = :id
            """,
            {"id": item_id},
        ),
        max_rows=100,
        timeout_seconds=10,
    )
    assert rows == [{"value": "after"}]


def test_mongodb_insert_read_update_delete(mongo):
    item_id = uuid.uuid4().hex

    insert_op = PlannedOperation(
        database="mongo",
        database_type=DatabaseType.MONGODB,
        operation_type=OperationType.WRITE,
        action="insert_one",
        collection=MONGO_COLLECTION,
        document={
            "_integration_test": True,
            "test_id": item_id,
            "value": "before",
        },
    )
    inserted = mongo.execute(insert_op, max_rows=100, timeout_seconds=10)
    assert inserted[0]["inserted_id"]

    read_op = PlannedOperation(
        database="mongo",
        database_type=DatabaseType.MONGODB,
        operation_type=OperationType.READ,
        action="find",
        collection=MONGO_COLLECTION,
        filter={"_integration_test": True, "test_id": item_id},
    )
    rows = mongo.execute(read_op, max_rows=100, timeout_seconds=10)
    assert len(rows) == 1
    assert rows[0]["value"] == "before"

    update_op = PlannedOperation(
        database="mongo",
        database_type=DatabaseType.MONGODB,
        operation_type=OperationType.WRITE,
        action="update_one",
        collection=MONGO_COLLECTION,
        filter={"_integration_test": True, "test_id": item_id},
        update={"$set": {"value": "after"}},
    )
    updated = mongo.execute(update_op, max_rows=100, timeout_seconds=10)
    assert updated[0]["matched_count"] == 1
    assert updated[0]["modified_count"] == 1

    rows = mongo.execute(read_op, max_rows=100, timeout_seconds=10)
    assert rows[0]["value"] == "after"

    delete_op = PlannedOperation(
        database="mongo",
        database_type=DatabaseType.MONGODB,
        operation_type=OperationType.WRITE,
        action="delete_one",
        collection=MONGO_COLLECTION,
        filter={"_integration_test": True, "test_id": item_id},
    )
    deleted = mongo.execute(delete_op, max_rows=100, timeout_seconds=10)
    assert deleted[0]["deleted_count"] == 1


def test_redis_set_get_delete_and_prefix_enforcement(redis_adapter):
    key = f"{REDIS_PREFIX}{uuid.uuid4().hex}"

    set_op = PlannedOperation(
        database="redis",
        database_type=DatabaseType.REDIS,
        operation_type=OperationType.WRITE,
        action="set",
        key=key,
        value="integration-value",
    )
    assert redis_adapter.execute(
        set_op, max_rows=100, timeout_seconds=10
    ) == [{"key": key, "stored": True}]

    get_op = PlannedOperation(
        database="redis",
        database_type=DatabaseType.REDIS,
        operation_type=OperationType.READ,
        action="get",
        key=key,
    )
    assert redis_adapter.execute(
        get_op, max_rows=100, timeout_seconds=10
    ) == [{"key": key, "value": "integration-value"}]

    bad_key_op = PlannedOperation(
        database="redis",
        database_type=DatabaseType.REDIS,
        operation_type=OperationType.WRITE,
        action="set",
        key=f"outside:{uuid.uuid4().hex}",
        value="must-not-write",
    )
    with pytest.raises(UnsupportedOperationError):
        redis_adapter.execute(
            bad_key_op, max_rows=100, timeout_seconds=10
        )

    delete_op = PlannedOperation(
        database="redis",
        database_type=DatabaseType.REDIS,
        operation_type=OperationType.WRITE,
        action="delete",
        key=key,
    )
    assert redis_adapter.execute(
        delete_op, max_rows=100, timeout_seconds=10
    ) == [{"deleted_count": 1}]

    assert redis_adapter.execute(
        get_op, max_rows=100, timeout_seconds=10
    ) == []