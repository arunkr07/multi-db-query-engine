import pytest

from multi_db_query_engine.databases import (
    _validate_mongo_document,
    _validate_mongo_filter,
    _validate_mongo_update,
)
from multi_db_query_engine.exceptions import UnsupportedOperationError


def test_mongo_filter_allows_basic_comparison_and_logical_operators():
    _validate_mongo_filter({
        "$and": [
            {"status": {"$eq": "paid"}},
            {"amount": {"$gte": 100}},
        ]
    })


def test_mongo_filter_rejects_server_side_execution_operator():
    with pytest.raises(UnsupportedOperationError, match="not allowed"):
        _validate_mongo_filter({"$where": "return true"})


def test_mongo_filter_rejects_unknown_operator_nested_in_field():
    with pytest.raises(UnsupportedOperationError, match="not allowed"):
        _validate_mongo_filter({"status": {"$function": {"body": "return true"}}})


def test_mongo_insert_document_rejects_operator_keys():
    with pytest.raises(UnsupportedOperationError, match="cannot start"):
        _validate_mongo_document({"profile": {"$where": "bad"}})


def test_mongo_update_requires_supported_operator_payload():
    _validate_mongo_update({"$set": {"status": "shipped"}, "$inc": {"attempts": 1}})
    with pytest.raises(UnsupportedOperationError, match="supported update operators"):
        _validate_mongo_update({"$where": "return true"})


def test_mongo_adapter_rejects_collection_outside_configured_scope():
    from types import SimpleNamespace

    from multi_db_query_engine.databases import MongoAdapter
    from multi_db_query_engine.models import (
        DatabaseConfig,
        DatabaseType,
        OperationType,
        PlannedOperation,
    )

    adapter = MongoAdapter.__new__(MongoAdapter)
    adapter.name = "restricted"
    adapter.config = DatabaseConfig(
        type=DatabaseType.MONGODB,
        uri="mongodb://localhost:27017",
        database="test",
        collection="orders",
    )
    adapter.db = SimpleNamespace(__getitem__=lambda self, name: pytest.fail("DB must not be accessed"))
    # Python special-method lookup bypasses instance attributes, so use an explicit fake.
    class FakeDB:
        def __getitem__(self, name):
            pytest.fail("DB must not be accessed for an unauthorized collection")

    adapter.db = FakeDB()
    op = PlannedOperation(
        database="restricted",
        database_type=DatabaseType.MONGODB,
        operation_type=OperationType.READ,
        action="find",
        collection="users",
        filter={},
    )
    with pytest.raises(UnsupportedOperationError, match="outside the configured scope"):
        adapter.execute(op, max_rows=10, timeout_seconds=1)


def test_mongo_adapter_rejects_collection_not_in_discovered_allowlist():
    from multi_db_query_engine.databases import MongoAdapter
    from multi_db_query_engine.models import (
        DatabaseConfig,
        DatabaseType,
        OperationType,
        PlannedOperation,
    )

    adapter = MongoAdapter.__new__(MongoAdapter)
    adapter.name = "unrestricted"
    adapter.config = DatabaseConfig(
        type=DatabaseType.MONGODB, uri="mongodb://localhost:27017", database="test"
    )
    adapter._allowed_collections = frozenset({"orders"})

    class FakeDB:
        def __getitem__(self, name):
            pytest.fail("DB must not be accessed for an undiscovered collection")

    adapter.db = FakeDB()
    op = PlannedOperation(
        database="unrestricted",
        database_type=DatabaseType.MONGODB,
        operation_type=OperationType.READ,
        action="find",
        collection="users",
        filter={},
    )
    with pytest.raises(UnsupportedOperationError, match="unknown or unauthorized"):
        adapter.execute(op, max_rows=10, timeout_seconds=1)


def test_mongo_schema_discovery_merges_fields_from_bounded_sample():
    from multi_db_query_engine.databases import MongoAdapter
    from multi_db_query_engine.models import DatabaseConfig, DatabaseType

    class FakeCursor:
        def __init__(self, docs):
            self.docs = docs
            self.limit_value = None

        def limit(self, count):
            self.limit_value = count
            return self

        def __iter__(self):
            return iter(self.docs[: self.limit_value])

    class FakeCollection:
        def __init__(self, docs):
            self.docs = docs
            self.cursor = FakeCursor(docs)

        def find(self, query):
            assert query == {}
            return self.cursor

    class FakeDB:
        def __init__(self):
            self.collection = FakeCollection([
                {"_id": 1, "common": "a"},
                {"_id": 2, "extra": "b"},
            ])

        def list_collection_names(self):
            return ["orders"]

        def __getitem__(self, name):
            assert name == "orders"
            return self.collection

    adapter = MongoAdapter.__new__(MongoAdapter)
    adapter.name = "sampled"
    adapter.config = DatabaseConfig(
        type=DatabaseType.MONGODB, uri="mongodb://localhost:27017", database="test"
    )
    adapter.db = FakeDB()

    assert adapter.discover_schema() == {"orders": ["_id", "common", "extra"]}
    assert adapter.db.collection.cursor.limit_value == 100
    assert adapter._allowed_collections == frozenset({"orders"})
