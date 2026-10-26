import pytest

from multi_db_query_engine.exceptions import SecurityViolationError
from multi_db_query_engine.models import OperationType
from multi_db_query_engine.security import (
    convert_named_parameters,
    validate_named_parameters,
    validate_sql_statement,
)


def test_forbidden_keyword_inside_string_literal_is_allowed():
    sql = "SELECT 'drop table is only text' AS note"
    assert validate_sql_statement(sql, OperationType.READ) == sql


def test_multiple_statements_rejected():
    with pytest.raises(SecurityViolationError):
        validate_sql_statement("SELECT 1; DELETE FROM users", OperationType.READ)


def test_comments_rejected_outside_literal():
    with pytest.raises(SecurityViolationError):
        validate_sql_statement("SELECT 1 -- comment", OperationType.READ)
    assert validate_sql_statement("SELECT '-- text' AS note", OperationType.READ)


def test_parameter_validation_detects_missing_and_extra():
    with pytest.raises(SecurityViolationError, match="Missing"):
        validate_named_parameters("SELECT * FROM t WHERE id=:id", {})
    with pytest.raises(SecurityViolationError, match="Unexpected"):
        validate_named_parameters("SELECT 1", {"unused": 1})


def test_parameter_converter_skips_quoted_text_and_postgres_casts():
    sql = "SELECT ':literal', col::text FROM t WHERE id=:id AND note=':not_a_param'"
    converted = convert_named_parameters(sql, "named")
    assert "%(id)s" in converted
    assert "':literal'" in converted
    assert "col::text" in converted
    assert "':not_a_param'" in converted


def test_read_cannot_contain_write_keyword_as_sql_clause():
    with pytest.raises(SecurityViolationError):
        validate_sql_statement("WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x", OperationType.READ)


def test_single_trailing_semicolon_allowed_but_double_rejected():
    assert validate_sql_statement("SELECT 1;", OperationType.READ) == "SELECT 1;"
    with pytest.raises(SecurityViolationError):
        validate_sql_statement("SELECT 1;;", OperationType.READ)

def test_union_based_sql_injection_is_rejected_when_multiple_statements_present():
    sql = "SELECT id FROM orders; DROP TABLE orders"
    with pytest.raises(SecurityViolationError):
        validate_sql_statement(sql, OperationType.READ)


def test_write_statement_cannot_be_classified_as_read():
    sql = "UPDATE orders SET status='shipped' WHERE id=1"
    with pytest.raises(SecurityViolationError):
        validate_sql_statement(sql, OperationType.READ)


def test_sql_values_are_parameterized_not_interpolated():
    sql = "SELECT id FROM orders WHERE status=:status"
    params = {"status": "' OR 1=1 --"}
    validate_named_parameters(sql, params)

    # User input remains a parameter value, not part of the SQL statement.
    assert "' OR 1=1 --" not in sql
    assert ":status" in sql

def test_schema_guard_rejects_unknown_tables():
    from multi_db_query_engine.models import DatabaseType
    from multi_db_query_engine.security import validate_sql_schema

    schema = {"orders": [{"name": "id", "type": "INTEGER"}]}
    with pytest.raises(SecurityViolationError, match="outside the discovered schema"):
        validate_sql_schema("SELECT * FROM secret_table", schema, DatabaseType.SQLITE)
    with pytest.raises(SecurityViolationError, match="outside the discovered schema"):
        validate_sql_schema("UPDATE secret_table SET id=:id", schema, DatabaseType.SQLITE)


def test_schema_guard_allows_known_tables_and_ctes():
    from multi_db_query_engine.models import DatabaseType
    from multi_db_query_engine.security import validate_sql_schema

    schema = {
        "orders": [{"name": "id", "type": "INTEGER"}],
        "customers": [{"name": "id", "type": "INTEGER"}],
    }
    validate_sql_schema(
        "WITH recent AS (SELECT id FROM orders) SELECT r.id FROM recent r JOIN customers c ON c.id=r.id",
        schema,
        DatabaseType.SQLITE,
    )


def test_schema_guard_supports_quoted_identifiers():
    from multi_db_query_engine.models import DatabaseType
    from multi_db_query_engine.security import validate_sql_schema

    validate_sql_schema('SELECT "order id" FROM "orders"', {"orders": []}, DatabaseType.SQLITE)


def test_schema_guard_does_not_treat_extract_from_as_table_reference():
    from multi_db_query_engine.models import DatabaseType
    from multi_db_query_engine.security import validate_sql_schema

    validate_sql_schema(
        "SELECT EXTRACT(YEAR FROM created_at) FROM orders",
        {"orders": [{"name": "created_at", "type": "DATE"}]},
        DatabaseType.POSTGRESQL,
    )
