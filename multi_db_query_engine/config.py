import json
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from multi_db_query_engine.exceptions import ConfigurationError
from multi_db_query_engine.models import DatabaseConfig

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_DATABASE_TIMEOUT_SECONDS = 10
DEFAULT_MAX_RESULT_ROWS = 100
DEFAULT_READ_RETRIES = 2
OPENAI_MODEL = "gpt-4.1-mini"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MQE_", env_file=".env", extra="ignore", case_sensitive=False)
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    databases: str = "{}"
    log_level: str = "INFO"
    enable_tracing: bool = False

    def database_configs(self) -> dict[str, DatabaseConfig]:
        try:
            raw: Any = json.loads(self.databases)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("MQE_DATABASES must be valid JSON") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("MQE_DATABASES must be a JSON object")
        result = {}
        for name, cfg in raw.items():
            if not isinstance(name, str) or not name.strip():
                raise ConfigurationError("Database names must be non-empty")
            try:
                result[name] = DatabaseConfig.model_validate(cfg)
            except (ValidationError, TypeError) as exc:
                raise ConfigurationError(f"Invalid configuration for database {name!r}") from exc
        return result

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
