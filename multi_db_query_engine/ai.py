from __future__ import annotations

import json

from multi_db_query_engine.config import OPENAI_MODEL, Settings
from multi_db_query_engine.exceptions import ConfigurationError, QueryPlanningError
from multi_db_query_engine.models import OperationPlan, PlannedOperation

SYSTEM_PROMPT = """
You are a database operation planner. Return one JSON object with keys:
explanation, clarification, operations.

Each operation may contain:
database, database_type, operation_type, statement, action, parameters,
collection, filter, document, update, key, value, limit, explanation.

Use only the supplied database catalog.

CRITICAL OPERATION TYPE RULE:
- operation_type MUST be exactly "read" or "write".
- For SQL SELECT and WITH queries, use operation_type="read".
- For SQL INSERT, UPDATE, and DELETE queries, use operation_type="write".
- Never use SQL keywords such as "SELECT", "INSERT", "UPDATE", or
  "DELETE" as the value of operation_type.
- For MongoDB find and Redis get, use operation_type="read".
- For MongoDB insert_one, update_one, delete_one and Redis set/delete,
  use operation_type="write".

SQL:
- Only single SELECT/WITH reads and INSERT/UPDATE/DELETE writes.
- Use named :parameters for values; never interpolate values.
- Never issue DDL, administrative operations, stored procedures,
  multiple SQL statements, or unsupported commands.

MongoDB:
- Use structured collection/filter/document/update operations.
- Use only supported actions: find, insert_one, update_one, delete_one.
- Filters may use common comparison/logical operators only; never use $where,
  $expr, server-side JavaScript, or aggregation/pipeline operators.
- Updates must use supported update operators such as $set, $unset, or $inc.
- Update/delete operations require a specific non-empty filter.

Redis:
- Use only supported simple key/value actions: get, set, delete.
- Never issue arbitrary Redis commands.

Use only databases and schema objects in the supplied catalog.
Do not invent tables, columns, collections, or database names.
If the intent is ambiguous, return a non-empty clarification and an
empty operations list.
Every write requires explicit approval.
Do not include hidden reasoning.

Return valid JSON only.
""".strip()


class OpenAIQueryPlanner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = None

    def _get_client(self):
        if self.client is None:
            if self.settings.openai_api_key is None:
                raise ConfigurationError("OPENAI_API_KEY is required for AI queries")
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ConfigurationError('Install the "ai" extra') from exc
            self.client = OpenAI(api_key=self.settings.openai_api_key.get_secret_value())
        return self.client

    def plan(self, question, schemas, types, selected=None):
        catalog = {name: {"type": types[name].value, "schema": schema}
                   for name, schema in schemas.items() if selected is None or selected == name}
        if selected and selected not in catalog:
            raise QueryPlanningError(f"Unknown database {selected!r}")
        try:
            result = self._get_client().chat.completions.create(
                model=OPENAI_MODEL, temperature=0, response_format={"type": "json_object"}, timeout=20,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": json.dumps({"question": question, "catalog": catalog,
                                                                     "selected_database": selected}, default=str)}],
            )
            content = result.choices[0].message.content if result.choices else None
            raw = json.loads(content or "{}")
            clarification = raw.get("clarification")
            raw_operations = raw.get("operations", [])
            if clarification:
                if raw_operations:
                    raise QueryPlanningError("Planner returned both clarification and operations")
                return {"clarification": str(clarification)}
            if not isinstance(raw_operations, list):
                raise QueryPlanningError("Planner operations must be a JSON array")
            if len(raw_operations) > 10:
                raise QueryPlanningError("Planner exceeded the maximum of 10 operations")
            operations = []
            for item in raw_operations:
                op = PlannedOperation.model_validate(item)
                if op.database not in catalog or op.database_type != types[op.database]:
                    raise QueryPlanningError("Planner selected an unconfigured database or incorrect type")
                operations.append(op)
            if not operations:
                raise QueryPlanningError("Planner returned no operations and no clarification")
            return OperationPlan(operations=operations, explanation=str(raw.get("explanation", "Plan generated.")))
        except QueryPlanningError:
            raise
        except Exception as exc:
            raise QueryPlanningError("Could not produce a valid query plan") from exc

    def close(self):
        if self.client:
            self.client.close()
            self.client = None
