import pytest
from pydantic import ValidationError

from multi_db_query_engine.models import DatabaseType, OperationType, PlannedOperation, QueryRequest


def test_query_request_strips_question():
    assert QueryRequest(question="  show orders  ").question == "show orders"


def test_query_request_blank_rejected():
    with pytest.raises(ValidationError): QueryRequest(question="  ")


def test_redis_payload_rejects_sql_fields():
    with pytest.raises(ValidationError):
        PlannedOperation(database="cache", database_type=DatabaseType.REDIS, operation_type=OperationType.READ,
                         action="get", key="orders:1", statement="SELECT 1")


def test_mongodb_update_requires_update_payload():
    with pytest.raises(ValidationError):
        PlannedOperation(database="mongo", database_type=DatabaseType.MONGODB, operation_type=OperationType.WRITE,
                         collection="orders", action="update_one")
