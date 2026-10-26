from __future__ import annotations

import sqlite3
import threading
import time
from functools import wraps
from typing import Any

from multi_db_query_engine.exceptions import DatabaseExecutionError, UnsupportedOperationError
from multi_db_query_engine.models import (
    DatabaseConfig,
    DatabaseType,
    OperationType,
    PlannedOperation,
)
from multi_db_query_engine.security import (
    convert_named_parameters,
    validate_named_parameters,
    validate_sql_schema,
    validate_sql_statement,
)


def _serialized_connection_method(method):
    """Serialize operations on a DB-API connection that is not concurrency-safe."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._connection_lock:
            return method(self, *args, **kwargs)
    return wrapper


_SAFE_MONGO_FILTER_OPERATORS = {
    "$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin",
    "$exists", "$type", "$and", "$or", "$nor", "$not", "$elemMatch",
    "$all", "$size", "$mod",
}
_SAFE_MONGO_UPDATE_OPERATORS = {
    "$set", "$unset", "$inc", "$mul", "$min", "$max", "$rename",
    "$currentDate", "$addToSet", "$pop", "$pull", "$push", "$pullAll", "$bit",
}


def _validate_mongo_filter(value: Any) -> None:
    """Allow common declarative filter operators, but reject server-side execution operators."""
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.startswith("$") and key not in _SAFE_MONGO_FILTER_OPERATORS:
                raise UnsupportedOperationError(f"MongoDB filter operator {key!r} is not allowed")
            _validate_mongo_filter(nested)
    elif isinstance(value, list):
        for nested in value:
            _validate_mongo_filter(nested)


def _validate_mongo_document(value: Any) -> None:
    """Disallow operator-shaped keys in inserted documents and update payload values."""
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.startswith("$"):
                raise UnsupportedOperationError("MongoDB document fields cannot start with '$'")
            _validate_mongo_document(nested)
    elif isinstance(value, list):
        for nested in value:
            _validate_mongo_document(nested)


def _validate_mongo_update(value: dict[str, Any]) -> None:
    if not value or any(key not in _SAFE_MONGO_UPDATE_OPERATORS for key in value):
        raise UnsupportedOperationError("MongoDB update must use supported update operators")
    for update_value in value.values():
        if not isinstance(update_value, dict):
            raise UnsupportedOperationError("MongoDB update operator values must be objects")
        _validate_mongo_document(update_value)


class DatabaseAdapter:
    def __init__(self, name: str, config: DatabaseConfig):
        self.name, self.config = name, config

    def discover_schema(self) -> dict[str, Any]:
        raise NotImplementedError

    def execute(self, op: PlannedOperation, max_rows: int, timeout_seconds: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class SQLAdapter(DatabaseAdapter):
    def __init__(self, name: str, config: DatabaseConfig):
        super().__init__(name, config)
        self._connection_lock = threading.RLock()
        self.schema: dict[str, Any] | None = None
        self.connection = None
        try:
            if config.type == DatabaseType.SQLITE:
                self.connection = sqlite3.connect(config.path, check_same_thread=False, timeout=10)
                self.connection.row_factory = sqlite3.Row
                self.connection.execute("PRAGMA busy_timeout = 10000")
            elif config.type == DatabaseType.POSTGRESQL:
                import psycopg
                self.connection = psycopg.connect(config.dsn, autocommit=False, connect_timeout=10)
            elif config.type == DatabaseType.MYSQL:
                from urllib.parse import urlparse

                import pymysql
                u = urlparse(config.dsn or "")
                if u.scheme not in ("mysql", "mysql+pymysql"):
                    raise ValueError("MySQL DSN must use mysql:// or mysql+pymysql://")
                self.connection = pymysql.connect(
                    host=u.hostname or "localhost", port=u.port or 3306, user=u.username,
                    password=u.password, database=u.path.lstrip("/") or None,
                    connect_timeout=10, read_timeout=10, write_timeout=10,
                    cursorclass=pymysql.cursors.DictCursor, autocommit=False,
                )
        except ImportError as exc:
            raise DatabaseExecutionError(f"Install the driver extra for {config.type.value}") from exc
        except Exception as exc:
            transient = self._is_transient_driver_error(exc)
            raise DatabaseExecutionError(f"Could not connect to {name!r}", transient=transient) from exc

    def _is_transient_driver_error(self, exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        if self.config.type == DatabaseType.SQLITE:
            return isinstance(exc, sqlite3.OperationalError) and any(x in text for x in ("locked", "busy", "temporarily"))
        if self.config.type == DatabaseType.POSTGRESQL:
            return name in {"operationalerror", "interfaceerror", "connectionfailure", "serverclosedconnection"}
        if self.config.type == DatabaseType.MYSQL:
            code = exc.args[0] if getattr(exc, "args", None) else None
            return code in {1205, 1213, 2006, 2013, 2055}
        return name in {"autoreconnect", "networktimeout", "serverselectiontimeouterror", "connectionerror", "timeouterror"}

    def _cursor(self):
        return self.connection.cursor()

    def _sqlite_authorizer(self, operation_type: OperationType):
        """Constrain SQLite's actual execution actions to discovered tables and safe SQL actions."""
        if not self.schema:
            raise UnsupportedOperationError("Cannot execute SQL before schema discovery succeeds")
        known_tables = {str(name).casefold(): value for name, value in self.schema.items()}
        allowed = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_TRANSACTION,
        }
        if hasattr(sqlite3, "SQLITE_SAVEPOINT"):
            allowed.add(sqlite3.SQLITE_SAVEPOINT)
        if hasattr(sqlite3, "SQLITE_RECURSIVE"):
            allowed.add(sqlite3.SQLITE_RECURSIVE)
        if operation_type == OperationType.WRITE:
            allowed.update({sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE})

        denied_functions = {"load_extension", "readfile", "writefile"}

        def authorize(action, arg1, arg2, database_name, source):
            if action == sqlite3.SQLITE_READ:
                table = (arg1 or "").casefold()
                if table not in known_tables:
                    return sqlite3.SQLITE_DENY
                columns = known_tables[table]
                column = (arg2 or "").casefold()
                known_columns = {str(item.get("name", "")).casefold() for item in columns if isinstance(item, dict)}
                if column and column not in known_columns and column not in {"rowid", "_rowid_", "oid"}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                table = (arg1 or "").casefold()
                if operation_type != OperationType.WRITE or table not in known_tables:
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_UPDATE and arg2:
                    columns = known_tables[table]
                    known_columns = {str(item.get("name", "")).casefold() for item in columns if isinstance(item, dict)}
                    if arg2.casefold() not in known_columns:
                        return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION:
                function_name = (arg2 or arg1 or "").casefold()
                return sqlite3.SQLITE_DENY if function_name in denied_functions else sqlite3.SQLITE_OK
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        return authorize

    @_serialized_connection_method
    def discover_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {}
        cur = None
        try:
            cur = self._cursor()
            if self.config.type == DatabaseType.SQLITE:
                tables = cur.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'").fetchall()
                for row in tables:
                    table = row["name"]
                    cols = cur.execute('PRAGMA table_info("' + table.replace('"', '""') + '")').fetchall()
                    schema[table] = [{"name": c["name"], "type": c["type"]} for c in cols]
            elif self.config.type == DatabaseType.POSTGRESQL:
                cur.execute("SELECT table_schema,table_name,column_name,data_type FROM information_schema.columns WHERE table_schema NOT IN ('information_schema','pg_catalog') ORDER BY table_schema,table_name,ordinal_position")
                for s, t, c, d in cur.fetchall():
                    schema.setdefault(f"{s}.{t}", []).append({"name": c, "type": d})
                self.connection.rollback()
            else:
                cur.execute(
                    """
                    SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                    ORDER BY TABLE_NAME, ORDINAL_POSITION
                    """
                )
                for row in cur.fetchall():
                    table = row["TABLE_NAME"]
                    column = row["COLUMN_NAME"]
                    data_type = row["DATA_TYPE"]
                    schema.setdefault(table, []).append(
                        {"name": column, "type": data_type}
                    )
                self.connection.rollback()
            self.schema = schema
            return schema
        except Exception as exc:
            try: self.connection.rollback()
            except Exception: pass  # noqa: BLE001, S110
            raise DatabaseExecutionError(f"Schema discovery failed for {self.name!r}", transient=self._is_transient_driver_error(exc)) from exc
        finally:
            if cur:
                try: cur.close()
                except Exception: pass  # noqa: BLE001, S110

    @_serialized_connection_method
    def execute(self, op: PlannedOperation, max_rows: int, timeout_seconds: int) -> list[dict[str, Any]]:
        sql = validate_sql_statement(op.statement or "", op.operation_type)
        params = op.parameters or {}
        validate_named_parameters(sql, params)
        if self.schema is None:
            self.discover_schema()
        validate_sql_schema(sql, self.schema or {}, self.config.type)
        cur = None
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        try:
            cur = self._cursor()
            if self.config.type == DatabaseType.SQLITE:
                self.connection.set_authorizer(self._sqlite_authorizer(op.operation_type))
                self.connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
            elif self.config.type == DatabaseType.POSTGRESQL:
                cur.execute("SELECT set_config('statement_timeout', %s, true)", (f"{int(timeout_seconds * 1000)}ms",))
            elif self.config.type == DatabaseType.MYSQL and op.operation_type == OperationType.READ:
                # MySQL's max_execution_time applies to SELECT statements.
                cur.execute(f"SET SESSION max_execution_time = {int(timeout_seconds * 1000)}")
            driver = "sqlite" if self.config.type == DatabaseType.SQLITE else "named"
            sql = convert_named_parameters(sql, driver)
            cur.execute(sql, params)
            if time.monotonic() > deadline:
                raise TimeoutError("Database operation deadline exceeded")
            if op.operation_type == OperationType.READ:
                rows = cur.fetchmany(max(1, min(int(max_rows), int(op.limit), 100)))
                if self.config.type == DatabaseType.SQLITE:
                    result = [dict(row) for row in rows]
                elif self.config.type == DatabaseType.POSTGRESQL:
                    cols = [d.name for d in cur.description or []]
                    result = [dict(zip(cols, row)) for row in rows]
                else:
                    result = [dict(row) for row in rows]
                # Release server-side read transactions/snapshots promptly.
                try: self.connection.rollback()
                except Exception: pass  # noqa: BLE001, S110
                return result
            self.connection.commit()
            return [{"affected_rows": cur.rowcount}]
        except Exception as exc:
            try: self.connection.rollback()
            except Exception: pass  # noqa: BLE001, S110
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, TimeoutError) or "interrupted" in str(exc).lower() or "statement timeout" in str(exc).lower():
                raise DatabaseExecutionError("Database operation timed out") from exc
            if isinstance(exc, (DatabaseExecutionError, UnsupportedOperationError)):
                raise
            raise DatabaseExecutionError(
                f"Operation failed on {self.name!r}", transient=self._is_transient_driver_error(exc)
            ) from exc
        finally:
            if self.config.type == DatabaseType.SQLITE:
                try: self.connection.set_progress_handler(None, 0)
                except Exception: pass  # noqa: BLE001, S110
                try: self.connection.set_authorizer(None)
                except Exception: pass  # noqa: BLE001, S110
            if cur:
                try: cur.close()
                except Exception: pass  # noqa: BLE001, S110

    @_serialized_connection_method
    def close(self) -> None:
        if self.connection:
            self.connection.close()
            self.connection = None


class MongoAdapter(DatabaseAdapter):
    def __init__(self, name: str, config: DatabaseConfig):
        super().__init__(name, config)
        try:
            from pymongo import MongoClient
            self.client = MongoClient(config.uri, serverSelectionTimeoutMS=10000, socketTimeoutMS=10000, connectTimeoutMS=10000)
            self.db = self.client[config.database]
            self.client.admin.command("ping")
        except ImportError as exc:
            raise DatabaseExecutionError("Install the mongodb extra") from exc
        except Exception as exc:
            raise DatabaseExecutionError(f"Could not connect to MongoDB {name!r}", transient=self._is_transient(exc)) from exc

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        return type(exc).__name__ in {"AutoReconnect", "NetworkTimeout", "ServerSelectionTimeoutError", "ConnectionFailure"}

    def discover_schema(self) -> dict[str, Any]:
        result = {}
        try:
            names = [self.config.collection] if self.config.collection else self.db.list_collection_names()
            for name in names:
                if name:
                    # MongoDB collections do not have a fixed schema. Merge field names from a
                    # bounded sample instead of trusting one potentially unrepresentative document.
                    fields: set[str] = set()
                    for document in self.db[name].find({}).limit(100):
                        fields.update(str(key) for key in document)
                    result[name] = sorted(fields)
            self._allowed_collections = frozenset(result)
            return result
        except Exception as exc:
            raise DatabaseExecutionError(f"MongoDB schema discovery failed on {self.name!r}", transient=self._is_transient(exc)) from exc

    def execute(self, op: PlannedOperation, max_rows: int, timeout_seconds: int) -> list[dict[str, Any]]:
        try:
            collection_name = op.collection or self.config.collection
            if not collection_name:
                raise UnsupportedOperationError("MongoDB collection is required")
            # Enforce collection scope at the adapter boundary; never rely on the AI planner
            # to honor a configured collection restriction.
            if self.config.collection and collection_name != self.config.collection:
                raise UnsupportedOperationError("MongoDB operation targets a collection outside the configured scope")
            allowed_collections = getattr(self, "_allowed_collections", None)
            if allowed_collections is not None and collection_name not in allowed_collections:
                raise UnsupportedOperationError("MongoDB operation targets an unknown or unauthorized collection")
            c = self.db[collection_name]
            if op.operation_type == OperationType.READ:
                _validate_mongo_filter(op.filter or {})
                cursor = c.find(op.filter or {}).limit(min(max_rows, op.limit, 100)).max_time_ms(int(timeout_seconds * 1000))
                return [dict(x) for x in cursor]
            if op.action == "insert_one":
                _validate_mongo_document(op.document)
                r = c.insert_one(op.document)
                return [{"inserted_id": str(r.inserted_id)}]
            if op.action == "update_one":
                if not op.filter:
                    raise UnsupportedOperationError("MongoDB update requires a non-empty filter")
                _validate_mongo_filter(op.filter)
                _validate_mongo_update(op.update)
                r = c.update_one(op.filter, op.update)
                return [{"matched_count": r.matched_count, "modified_count": r.modified_count}]
            if op.action == "delete_one":
                if not op.filter:
                    raise UnsupportedOperationError("MongoDB delete requires a non-empty filter")
                _validate_mongo_filter(op.filter)
                r = c.delete_one(op.filter)
                return [{"deleted_count": r.deleted_count}]
            raise UnsupportedOperationError("Unsupported MongoDB action")
        except UnsupportedOperationError:
            raise
        except Exception as exc:
            if type(exc).__name__ in {"ExecutionTimeout", "NetworkTimeout"}:
                raise DatabaseExecutionError("MongoDB operation timed out") from exc
            raise DatabaseExecutionError(f"MongoDB operation failed on {self.name!r}", transient=self._is_transient(exc)) from exc

    def close(self) -> None:
        self.client.close()


class RedisAdapter(DatabaseAdapter):
    def __init__(self, name: str, config: DatabaseConfig):
        super().__init__(name, config)
        try:
            import redis
            # Redis-py applies socket timeouts at the connection level. This SDK currently
            # uses a documented fixed 10-second transport timeout for Redis operations.
            self.client = redis.Redis.from_url(config.url, decode_responses=True, socket_timeout=10, socket_connect_timeout=10)
            self.client.ping()
        except ImportError as exc:
            raise DatabaseExecutionError("Install the redis extra") from exc
        except Exception as exc:
            transient = type(exc).__name__ in {"ConnectionError", "TimeoutError", "BusyLoadingError"}
            raise DatabaseExecutionError(f"Could not connect to Redis {name!r}", transient=transient) from exc

    def discover_schema(self) -> dict[str, Any]:
        return {"key_value": {"key_prefix": self.config.key_prefix, "value_type": "string"}}

    def execute(self, op: PlannedOperation, max_rows: int, timeout_seconds: int) -> list[dict[str, Any]]:
        key = op.key or ""
        if self.config.key_prefix and not key.startswith(self.config.key_prefix):
            raise UnsupportedOperationError("Redis key must use configured key_prefix")
        try:
            if op.action == "get":
                value = self.client.get(key)
                return [] if value is None else [{"key": key, "value": value}]
            if op.action == "delete":
                return [{"deleted_count": self.client.delete(key)}]
            if op.action == "set":
                self.client.set(key, str(op.value))
                return [{"key": key, "stored": True}]
            raise UnsupportedOperationError("Unsupported Redis action")
        except UnsupportedOperationError:
            raise
        except Exception as exc:
            transient = type(exc).__name__ in {"ConnectionError", "TimeoutError", "BusyLoadingError"}
            if type(exc).__name__ == "TimeoutError":
                raise DatabaseExecutionError("Redis operation timed out") from exc
            raise DatabaseExecutionError(f"Redis operation failed on {self.name!r}", transient=transient) from exc

    def close(self) -> None:
        self.client.close()


class DatabaseRegistry:
    def __init__(self, configs: dict[str, DatabaseConfig]):
        self.adapters: dict[str, DatabaseAdapter] = {}
        self.schemas: dict[str, dict[str, Any]] = {}
        try:
            for name, cfg in configs.items():
                if cfg.type in {DatabaseType.SQLITE, DatabaseType.POSTGRESQL, DatabaseType.MYSQL}:
                    cls = SQLAdapter
                elif cfg.type == DatabaseType.MONGODB:
                    cls = MongoAdapter
                else:
                    cls = RedisAdapter
                adapter = cls(name, cfg)
                self.adapters[name] = adapter
                self.schemas[name] = adapter.discover_schema()
        except Exception:
            self.close()
            raise

    @property
    def names(self) -> list[str]:
        return list(self.adapters)

    def get(self, name: str) -> DatabaseAdapter:
        if name not in self.adapters:
            raise DatabaseExecutionError(f"Unknown database {name!r}")
        return self.adapters[name]

    def close(self) -> None:
        for adapter in list(self.adapters.values()):
            try: adapter.close()
            except Exception: pass  # noqa: BLE001, S110
        self.adapters.clear()
        self.schemas.clear()
