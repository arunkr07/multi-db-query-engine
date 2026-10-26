import json
import logging

from multi_db_query_engine.observability import JsonFormatter, sanitize


def test_nested_sensitive_fields_are_redacted():
    assert sanitize({"config":{"password":"p","api_key":"k"},"ok":True}) == {"config":{"password":"[REDACTED]","api_key":"[REDACTED]"},"ok":True}


def test_log_message_common_secret_patterns_are_redacted():
    record = logging.LogRecord("test", logging.INFO, "", 0, "request api_key=abc123 succeeded", (), None)
    payload = json.loads(JsonFormatter().format(record))
    assert "abc123" not in payload["message"]
