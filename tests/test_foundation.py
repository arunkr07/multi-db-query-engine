import pytest
from pydantic import ValidationError

from multi_db_query_engine import (
    DatabaseConfig,
    DatabaseType,
    QueryRequest,
    QueryResponse,
    QueryStatus,
    Settings,
)
from multi_db_query_engine.exceptions import ConfigurationError, SecurityViolationError
from multi_db_query_engine.models import OperationType
from multi_db_query_engine.observability import sanitize
from multi_db_query_engine.security import validate_sql_statement


def test_response_contract():
    r=QueryResponse(status=QueryStatus.COMPLETED,answer="ok",data=[])
    assert set(r.model_dump())=={"status","answer","data"}

def test_blank_request_rejected():
    with pytest.raises(ValidationError): QueryRequest(question="  ")

def test_sqlite_requires_path():
    with pytest.raises(ValidationError): DatabaseConfig(type=DatabaseType.SQLITE)

def test_settings_parses_sqlite_config():
    s=Settings(_env_file=None,databases='{"local":{"type":"sqlite","path":":memory:"}}')
    assert s.database_configs()["local"].type == DatabaseType.SQLITE

def test_bad_json_rejected():
    with pytest.raises(ConfigurationError): Settings(_env_file=None,databases="nope").database_configs()

def test_sql_guard_rejects_multiple_statements():
    with pytest.raises(SecurityViolationError):
        validate_sql_statement(
            "SELECT 1; DELETE FROM x",
            OperationType.READ,
        )

def test_sql_guard_accepts_read():
    assert validate_sql_statement("SELECT 1",OperationType.READ)=="SELECT 1"

def test_log_sanitization():
    assert sanitize({"api_key":"secret"})["api_key"]=="[REDACTED]"
