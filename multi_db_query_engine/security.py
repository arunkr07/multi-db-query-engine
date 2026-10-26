from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from multi_db_query_engine.exceptions import SecurityViolationError
from multi_db_query_engine.models import DatabaseType, OperationType


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """Internal limits. These are intentionally not request-configurable."""
    request_timeout_seconds: int = 30
    database_timeout_seconds: int = 10
    max_result_rows: int = 100
    read_retries: int = 2


DEFAULT_EXECUTION_LIMITS = ExecutionLimits()
_START_READ = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_START_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
_FORBIDDEN_WORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "GRANT", "REVOKE", "CALL", "EXEC", "COPY", "ATTACH", "DETACH", "PRAGMA",
    "VACUUM", "INTO", "OUTFILE", "LOAD", "REPLACE", "REINDEX", "ANALYZE",
}


def _code_only(sql: str) -> str:
    """Replace quoted literal/identifier contents with spaces for lexical checks."""
    out: list[str] = []
    i = 0
    quote: str | None = None
    while i < len(sql):
        ch = sql[i]
        if quote:
            out.append(" ")
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote and quote in ("'", '"', '`'):
                    out.append(" ")
                    i += 2
                    continue
                if quote == "'" and i > 0 and sql[i - 1] == "\\":
                    i += 1
                    continue
                quote = None
            elif ch == "\\" and quote == "'" and i + 1 < len(sql):
                out.append(" ")
                i += 2
                continue
        elif ch in ("'", '"', '`'):
            quote = ch
            out.append(" ")
        else:
            out.append(ch)
        i += 1
    if quote:
        raise SecurityViolationError("Unterminated SQL quoted value or identifier")
    return "".join(out)


def validate_sql_statement(sql: str, operation_type: OperationType) -> str:
    sql = sql.strip()
    if not sql:
        raise SecurityViolationError("SQL statement must not be empty")
    code = _code_only(sql)
    # Comments are rejected outside literals; hash is treated as a comment marker for portability.
    if re.search(r"--|/\*|\*/|#", code):
        raise SecurityViolationError("SQL comments are not allowed")
    statement_code = code.rstrip()
    statement_code = statement_code.removesuffix(";")
    if ";" in statement_code:
        raise SecurityViolationError("Multiple SQL statements are not allowed")
    if operation_type == OperationType.READ:
        if not _START_READ.match(code) or re.search(r"\b(?:" + "|".join(sorted(_FORBIDDEN_WORDS)) + r")\b", code, re.IGNORECASE):
            raise SecurityViolationError("Reads must be a single SELECT/WITH statement without write or administrative clauses")
    elif operation_type == OperationType.WRITE:
        if not _START_WRITE.match(code) or re.search(r"\b(?:DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|CALL|EXEC|COPY|ATTACH|DETACH|PRAGMA|VACUUM|LOAD|OUTFILE)\b", code, re.IGNORECASE):
            raise SecurityViolationError("Only INSERT/UPDATE/DELETE writes are supported")
    else:
        raise SecurityViolationError("Unsupported SQL operation type")
    return sql



def _sql_tokens(sql: str) -> list[tuple[str, str, int]]:
    """Tokenize enough SQL structure to check table references without parsing expressions."""
    tokens: list[tuple[str, str, int]] = []
    i = 0
    depth = 0
    while i < len(sql):
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "'":
            i += 1
            while i < len(sql):
                if sql[i] == "'":
                    if i + 1 < len(sql) and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                if sql[i] == "\\" and i + 1 < len(sql):
                    i += 2
                else:
                    i += 1
            tokens.append(("literal", "", depth))
            continue
        if ch in ('"', '`', '['):
            end_char = ']' if ch == '[' else ch
            i += 1
            chars: list[str] = []
            while i < len(sql):
                if sql[i] == end_char:
                    if end_char != ']' and i + 1 < len(sql) and sql[i + 1] == end_char:
                        chars.append(end_char)
                        i += 2
                        continue
                    i += 1
                    break
                chars.append(sql[i])
                i += 1
            tokens.append(("identifier", "".join(chars), depth))
            continue
        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < len(sql) and (sql[i].isalnum() or sql[i] in "_$"):
                i += 1
            tokens.append(("word", sql[start:i], depth))
            continue
        if ch.isdigit():
            start = i
            i += 1
            while i < len(sql) and (sql[i].isalnum() or sql[i] in "._"):
                i += 1
            tokens.append(("number", sql[start:i], depth))
            continue
        if ch == "(":
            tokens.append(("symbol", ch, depth))
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            tokens.append(("symbol", ch, depth))
            i += 1
            continue
        tokens.append(("symbol", ch, depth))
        i += 1
    return tokens


def _cte_names(tokens: list[tuple[str, str, int]]) -> set[str]:
    """Collect ordinary CTE names so references to them are not treated as base tables."""
    names: set[str] = set()
    for i, (kind, value, depth) in enumerate(tokens):
        if kind != "word" or value.upper() != "WITH":
            continue
        j = i + 1
        if j < len(tokens) and tokens[j][0] == "word" and tokens[j][1].upper() == "RECURSIVE":
            j += 1
        while j < len(tokens):
            if tokens[j][0] not in {"word", "identifier"} or tokens[j][2] != depth:
                break
            name = tokens[j][1].casefold()
            j += 1
            # Optional CTE column-name list.
            if j < len(tokens) and tokens[j][1] == "(":
                open_depth = tokens[j][2]
                j += 1
                while j < len(tokens) and not (tokens[j][1] == ")" and tokens[j][2] == open_depth):
                    j += 1
                j += 1
            if j >= len(tokens) or tokens[j][0] != "word" or tokens[j][1].upper() != "AS":
                break
            names.add(name)
            j += 1
            if j >= len(tokens) or tokens[j][1] != "(":
                break
            body_depth = tokens[j][2]
            j += 1
            while j < len(tokens) and not (tokens[j][1] == ")" and tokens[j][2] == body_depth):
                j += 1
            j += 1
            if j < len(tokens) and tokens[j][1] == "," and tokens[j][2] == depth:
                j += 1
                continue
            break
    return names


def validate_sql_schema(
    sql: str,
    schema: dict[str, Any],
    database_type: DatabaseType,
) -> None:
    """Reject SQL that references tables absent from the schema discovered at startup.

    This is a conservative table-level guard, not a full SQL parser or column validator.
    Database permissions remain the primary security boundary.
    """
    tokens = _sql_tokens(sql)
    ctes = _cte_names(tokens)
    known = {str(table).casefold(): str(table) for table in schema}
    basename_map: dict[str, list[str]] = {}
    for key in schema:
        basename_map.setdefault(str(key).split(".")[-1].casefold(), []).append(str(key))

    refs: list[tuple[str, str | None]] = []
    for i, (kind, value, depth) in enumerate(tokens):
        if kind != "word" or value.upper() not in {"FROM", "JOIN", "UPDATE", "INTO"}:
            continue
        # FROM is also part of scalar expressions such as EXTRACT(YEAR FROM date_col)
        # and TRIM(... FROM value); it is not a table clause in those functions.
        if value.upper() == "FROM" and depth > 0:
            expression_from = False
            for open_index in range(i - 1, -1, -1):
                _, token_value, token_depth = tokens[open_index]
                if token_value == "(" and token_depth == depth - 1:
                    previous = tokens[open_index - 1] if open_index else ("", "", -1)
                    expression_from = (
                        previous[0] == "word"
                        and previous[1].upper() in {"EXTRACT", "TRIM", "SUBSTRING", "POSITION"}
                    )
                    break
            if expression_from:
                continue
        j = i + 1
        while j < len(tokens) and tokens[j][0] == "word" and tokens[j][1].upper() in {"ONLY", "LATERAL"}:
            j += 1
        if j >= len(tokens) or tokens[j][1] == "(" or tokens[j][0] not in {"word", "identifier"}:
            continue
        first = tokens[j][1]
        j += 1
        qualifier = None
        table = first
        if j + 1 < len(tokens) and tokens[j][1] == "." and tokens[j + 1][0] in {"word", "identifier"}:
            qualifier, table = first, tokens[j + 1][1]
        if qualifier is None and table.casefold() in ctes:
            continue
        refs.append((table, qualifier))

    for table, qualifier in refs:
        if qualifier is not None:
            full = f"{qualifier}.{table}".casefold()
            if full in known:
                continue
            if database_type == DatabaseType.SQLITE and qualifier.casefold() in {"main", "temp"} and table.casefold() in basename_map:
                continue
            raise SecurityViolationError(f"SQL references table {qualifier}.{table!s} outside the discovered schema")
        matches = basename_map.get(table.casefold(), [])
        if not matches:
            raise SecurityViolationError(f"SQL references table {table!r} outside the discovered schema")
        if len(matches) > 1:
            raise SecurityViolationError(f"SQL table {table!r} is ambiguous; qualify it with its schema")

def validate_named_parameters(sql: str, parameters: dict[str, Any] | None) -> None:
    """Require exact named-parameter matching outside SQL quoted strings/identifiers."""
    params = parameters or {}
    code = _code_only(sql)
    names: set[str] = set()
    i = 0
    while i < len(code):
        if code[i] == ":":
            # Avoid PostgreSQL casts (::type); only accept :identifier placeholders.
            if i + 1 < len(code) and code[i + 1] == ":":
                i += 2
                continue
            match = re.match(r":([A-Za-z_][A-Za-z0-9_]*)", code[i:])
            if match:
                names.add(match.group(1))
                i += len(match.group(0))
                continue
        i += 1
    missing = names - set(params)
    extra = set(params) - names
    if missing:
        raise SecurityViolationError(f"Missing SQL parameter(s): {', '.join(sorted(missing))}")
    if extra:
        raise SecurityViolationError(f"Unexpected SQL parameter(s): {', '.join(sorted(extra))}")


def convert_named_parameters(sql: str, driver: str) -> str:
    """Convert :name placeholders outside literals to the target driver's named style."""
    if driver == "sqlite":
        return sql
    out: list[str] = []
    i = 0
    quote: str | None = None
    while i < len(sql):
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote and quote in ("'", '"', '`'):
                    out.append(sql[i + 1]); i += 2; continue
                if quote == "'" and i > 0 and sql[i - 1] == "\\":
                    i += 1; continue
                quote = None
            elif ch == "\\" and quote == "'" and i + 1 < len(sql):
                out.append(sql[i + 1]); i += 2; continue
            i += 1
            continue
        if ch in ("'", '"', '`'):
            quote = ch; out.append(ch); i += 1; continue
        if ch == ":" and i + 1 < len(sql) and sql[i + 1] == ":":
            out.append("::"); i += 2; continue
        if ch == ":":
            match = re.match(r":([A-Za-z_][A-Za-z0-9_]*)", sql[i:])
            if match:
                out.append("%(" + match.group(1) + ")s")
                i += len(match.group(0)); continue
        out.append(ch); i += 1
    return "".join(out)
