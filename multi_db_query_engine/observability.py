import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

SENSITIVE = ("password", "passwd", "secret", "token", "api_key", "apikey", "authorization", "credential", "dsn", "uri")


def sanitize(value: Any, key: str = "") -> Any:
    if any(part in key.lower() for part in SENSITIVE):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        # Avoid leaking common credential forms if a caller accidentally logs a raw string.
        value = re.sub(r"(?i)(api[_-]?key|password|passwd|token|secret)\s*[=:]\s*[^\s,;]+", r"\1=[REDACTED]", value)
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {"timestamp": datetime.now(UTC).isoformat(), "level": record.levelname,
                "logger": record.name, "message": sanitize(record.getMessage())}
        fields = getattr(record, "event_fields", None)
        if isinstance(fields, dict):
            data["fields"] = sanitize(fields)
        if record.exc_info and record.exc_info[0]:
            data["exception_type"] = record.exc_info[0].__name__
        return json.dumps(data, separators=(",", ":"))


def get_logger(level="INFO"):
    logger = logging.getLogger("multi_db_query_engine")
    logger.setLevel(level.upper())
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


def log_event(logger, level, message, **fields):
    logger.log(level, sanitize(message), extra={"event_fields": sanitize(fields)})
