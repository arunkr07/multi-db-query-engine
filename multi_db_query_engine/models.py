from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DatabaseType(StrEnum):
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    SQLITE = "sqlite"
    MONGODB = "mongodb"
    REDIS = "redis"

class OperationType(StrEnum):
    READ = "read"
    WRITE = "write"

class QueryStatus(StrEnum):
    COMPLETED = "completed"
    NO_DATA = "no_data"
    NEEDS_CLARIFICATION = "needs_clarification"
    APPROVAL_REQUIRED = "approval_required"
    PARTIAL = "partial"
    FAILED = "failed"

class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=10000)
    database: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def question_not_blank(self):
        if not self.question:
            raise ValueError("question must not be blank")
        return self

class QueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: QueryStatus
    answer: str = Field(min_length=1)
    data: Any = None

class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: DatabaseType
    dsn: str | None = None
    path: str | None = None
    uri: str | None = None
    database: str | None = None
    collection: str | None = None
    url: str | None = None
    key_prefix: str = ""

    @model_validator(mode="after")
    def validate_connection(self):
        needed = {
            DatabaseType.POSTGRESQL: ("dsn",),
            DatabaseType.MYSQL: ("dsn",),
            DatabaseType.SQLITE: ("path",),
            DatabaseType.MONGODB: ("uri", "database"),
            DatabaseType.REDIS: ("url",),
        }[self.type]
        for field in needed:
            if not getattr(self, field):
                raise ValueError(f"{self.type.value} requires {field}")
        return self

class PlannedOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str = Field(min_length=1, max_length=128)
    database_type: DatabaseType
    operation_type: OperationType
    statement: str | None = None
    action: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    collection: str | None = None
    filter: dict[str, Any] = Field(default_factory=dict)
    document: dict[str, Any] = Field(default_factory=dict)
    update: dict[str, Any] = Field(default_factory=dict)
    key: str | None = None
    value: Any = None
    limit: int = Field(default=100, ge=1, le=100)
    explanation: str = "Planned database operation."

    @model_validator(mode="after")
    def validate_target(self):
        if self.database_type in {DatabaseType.POSTGRESQL, DatabaseType.MYSQL, DatabaseType.SQLITE}:
            if not self.statement:
                raise ValueError("SQL operation requires statement")
            if any((self.collection, self.document, self.update, self.key, self.value is not None)):
                raise ValueError("SQL operation contains fields for a non-SQL database")
        if self.database_type == DatabaseType.MONGODB:
            if self.statement or self.parameters or self.key or self.value is not None:
                raise ValueError("MongoDB operation contains fields for another database type")
            if not self.collection:
                raise ValueError("MongoDB operation requires collection")
            allowed = {"find"} if self.operation_type == OperationType.READ else {"insert_one", "update_one", "delete_one"}
            if self.action not in allowed:
                raise ValueError(f"MongoDB action must be one of {sorted(allowed)}")
            if self.action == "insert_one" and not self.document:
                raise ValueError("MongoDB insert_one requires document")
            if self.action == "update_one" and not self.update:
                raise ValueError("MongoDB update_one requires update")
        if self.database_type == DatabaseType.REDIS:
            if self.statement or self.parameters or self.collection or self.filter or self.document or self.update:
                raise ValueError("Redis operation contains fields for another database type")
            if not self.key:
                raise ValueError("Redis operation requires key")
            allowed = {"get"} if self.operation_type == OperationType.READ else {"set", "delete"}
            if self.action not in allowed:
                raise ValueError(f"Redis action must be one of {sorted(allowed)}")
            if self.action == "set" and self.value is None:
                raise ValueError("Redis set requires a non-null value")
        return self

class OperationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operations: list[PlannedOperation] = Field(min_length=1, max_length=10)
    explanation: str
    clarification: str | None = None

    @property
    def requires_write_approval(self) -> bool:
        return any(op.operation_type == OperationType.WRITE for op in self.operations)
