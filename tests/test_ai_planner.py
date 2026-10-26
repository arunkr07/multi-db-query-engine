import json
from types import SimpleNamespace

import pytest

from multi_db_query_engine.ai import OpenAIQueryPlanner
from multi_db_query_engine.config import Settings
from multi_db_query_engine.exceptions import QueryPlanningError
from multi_db_query_engine.models import DatabaseType


class FakeCompletions:
    def __init__(self, content): self.content = content; self.calls = []
    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


def planner_with(content):
    planner = OpenAIQueryPlanner(Settings(_env_file=None, openai_api_key="test-key"))
    fake = FakeCompletions(content)
    planner.client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    return planner


def test_planner_returns_valid_read_plan():
    content = json.dumps({"explanation":"read orders", "operations":[{"database":"local", "database_type":"sqlite", "operation_type":"read", "statement":"SELECT id FROM orders", "limit":10}]})
    planner = planner_with(content)
    result = planner.plan("list orders", {"local":{"orders":[{"name":"id","type":"INTEGER"}]}}, {"local":DatabaseType.SQLITE})
    assert result.operations[0].database == "local"


def test_planner_clarification_is_returned():
    planner = planner_with(json.dumps({"clarification":"Which date range?", "operations":[]}))
    assert planner.plan("show sales", {"local":{}}, {"local":DatabaseType.SQLITE}) == {"clarification":"Which date range?"}


def test_planner_rejects_more_than_ten_operations():
    ops = [{"database":"local", "database_type":"sqlite", "operation_type":"read", "statement":"SELECT 1"} for _ in range(11)]
    planner = planner_with(json.dumps({"operations":ops}))
    with pytest.raises(QueryPlanningError, match="10 operations"):
        planner.plan("question", {"local":{}}, {"local":DatabaseType.SQLITE})


def test_planner_rejects_unknown_database():
    content = json.dumps({"operations":[{"database":"other", "database_type":"sqlite", "operation_type":"read", "statement":"SELECT 1"}]})
    planner = planner_with(content)
    with pytest.raises(QueryPlanningError, match="unconfigured"):
        planner.plan("question", {"local":{}}, {"local":DatabaseType.SQLITE})


def test_planner_rejects_invalid_json():
    planner = planner_with("not json")
    with pytest.raises(QueryPlanningError, match="valid query plan"):
        planner.plan("question", {"local":{}}, {"local":DatabaseType.SQLITE})
