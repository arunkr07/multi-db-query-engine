# Multi-DB Query Engine

An object-oriented Python SDK that translates natural-language questions into constrained database operations. It supports synchronous and asynchronous facades, Pydantic response models, schema discovery at client initialization, and explicit approval for all writes.

> **Status:** audited release candidate with automated unit tests. SQLite is covered by executable tests and a SQLite authorizer. PostgreSQL, MySQL, MongoDB, and Redis adapters require integration testing against actual services before production use. Do not treat passing unit tests as proof of production readiness.

## Features

- Database engines: SQLite, PostgreSQL, MySQL, MongoDB, Redis.
- OpenAI planner uses the fixed model `gpt-4.1-mini`.
- Multiple named database connections and caller-selected database support.
- SQL values use named parameters; SQL comments and multiple statements are rejected.
- Writes are never retried automatically and require explicit approval.
- Read retries are limited to adapter-classified transient database errors.
- Fixed internal limits: 30-second request deadline, 10-second database operation target, 100 returned rows, and up to 2 transient read retries.
- Schema metadata is discovered once at initialization and held in memory for the client lifetime.
- Structured JSON logging with basic sensitive-field redaction.
- Async facade delegates blocking SDK calls to worker threads using `asyncio.to_thread`; it is not a native async database implementation.

## Install

Python 3.11 or newer is required. Python 3.14 may work, but database driver compatibility depends on each driver release.

```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate
python -m pip install -e ".[all-databases,dev]"
```

Install only needed extras if desired: `.[ai]`, `.[postgres]`, `.[mysql]`, `.[mongodb]`, `.[redis]`.

## Configuration

Copy `.env.example` to `.env`. Never commit `.env` or share its contents.

```dotenv
OPENAI_API_KEY=your-key-here
MQE_DATABASES={"local":{"type":"sqlite","path":"./demo.db"}}
MQE_LOG_LEVEL=INFO
MQE_ENABLE_TRACING=false
```

`MQE_DATABASES` is a JSON object whose keys are caller-facing database names. Connection secrets belong in environment variables/configuration, not in source code. The database configuration string may contain DSNs/URIs; keep `.env` private and exclude it from version control.

## Quick start

```python
from multi_db_query_engine import MultiDBQueryEngine

with MultiDBQueryEngine() as engine:
    print(engine.databases)
    print(engine.schemas)
    response = engine.ask("Show the 5 most recent paid orders", database="local")
    print(response.model_dump())
```

Response contract: `status`, `answer`, `data`. Status values include `completed`, `no_data`, `needs_clarification`, `approval_required`, `partial`, and `failed`.

## Write approval workflow

When the planner produces a write operation, `ask()` returns `approval_required` with one or more operation IDs and review summaries. Approve each operation explicitly:

```python
pending = engine.list_pending_operations()
operation_id = pending[0]["operation_id"]
response = engine.approve_and_execute(operation_id)
```

Pending approvals are held in memory and expire after the configured TTL (default 300 seconds). They are lost on process restart. Approval summaries contain operation details for human review; do not expose them to untrusted users or logs. A write batch is not a distributed transaction. If a later write fails after an earlier write succeeds, the SDK returns `partial` with per-operation outcomes; it cannot roll back writes already committed by another database.

An `approval_callback` can be supplied to the client for a controlled approval flow. If any callback decision rejects the batch or raises, no write in that batch is executed and its pending approvals are cancelled.

## Async facade

```python
from multi_db_query_engine import AsyncMultiDBQueryEngine

async def main():
    async with AsyncMultiDBQueryEngine() as engine:
        response = await engine.ask("Count orders", database="local")
        print(response.model_dump())
```

The facade runs synchronous methods in worker threads. Cancellation of the awaiting coroutine does not guarantee that an already-running database call has been cancelled.

## Timeouts and limits

- Request deadline: 30 seconds, checked between planned operations and retries. This is not a hard wall-clock guarantee for the entire AI request.
- Database operation target: 10 seconds. SQLite uses a progress handler and lock wait timeout; PostgreSQL uses `statement_timeout`; MySQL applies `max_execution_time` to reads and has driver socket read/write timeouts; MongoDB reads use `maxTimeMS`; Redis uses socket timeouts.
- Driver/server behavior varies. In particular, some write operations cannot be interrupted using the same mechanism as reads. Always verify timeouts against your actual database/server/driver versions.
- Maximum returned rows: 100, with operation-level limit capped at 100.
- Retries: at most 2 retries for errors classified as transient by the adapter. Invalid SQL, validation errors, timeouts, and writes are not automatically retried.

## Security model and limitations

- SQL validation uses a conservative lexical guard plus a table-reference check against schema metadata discovered at startup. It is not a complete SQL parser and does not validate every column reference for PostgreSQL/MySQL. SQLite additionally uses its authorizer to restrict runtime table/column access and deny DDL, administrative actions, and selected dangerous file functions.
- Use a database account with least privilege. Prefer read-only credentials for read-oriented deployments; database permissions remain the primary security boundary for PostgreSQL/MySQL.
- Values are parameterized. SQL identifiers cannot be parameterized; runtime table checks are defense-in-depth, not a replacement for least-privilege database roles.
- MongoDB schema discovery samples one document per collection; it is approximate and may miss fields that appear in other documents.
- Redis key-prefix checks apply when `key_prefix` is configured.
- Cross-database writes are not atomic. DDL, administrative operations, stored procedures, arbitrary Redis commands, durable approval storage, and distributed transactions are out of scope.
- Logs aim to omit raw exception messages and redact common secret fields, but redaction is defense-in-depth. Do not log credentials, full result sets, or unreviewed write payloads.

## Tests

```bash
python -m pytest -v
python -m pytest --cov=multi_db_query_engine --cov-report=term-missing
```

Unit tests mock OpenAI and test SQLite locally, including schema guards, approval concurrency, shared-connection serialization, and concurrent async calls. Live integration tests for PostgreSQL, MySQL, MongoDB, and Redis should be added to CI using isolated test services before a production deployment. The async facade uses worker threads; cancellation of an awaiting coroutine does not terminate a running worker operation.

## Demo SQLite database

`setup_demo_db.py` creates a small `orders` table for local testing. `check_connection.py` checks configured connections and schema discovery. Both scripts are convenience utilities, not required SDK modules.
