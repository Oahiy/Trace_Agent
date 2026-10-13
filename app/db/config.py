"""Explicit local database configuration, separate from the public API's settings."""

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.core.config import ConfigurationError


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRACEAGENT_DB_",
        env_file=None,
        env_ignore_empty=True,
        extra="forbid",
        hide_input_in_errors=True,
        frozen=True,
        allow_inf_nan=False,
    )

    url: SecretStr
    connect_timeout_seconds: int = Field(default=3, ge=2, le=10)
    pool_timeout_seconds: float = Field(default=2, gt=0, le=10)
    statement_timeout_ms: int = Field(default=3000, ge=100, le=10_000)
    lock_timeout_ms: int = Field(default=1000, ge=100, le=10_000)
    idle_transaction_timeout_ms: int = Field(default=5000, ge=1000, le=30_000)

    @field_validator("url")
    @classmethod
    def local_postgres(cls, secret: SecretStr) -> SecretStr:
        raw = secret.get_secret_value()
        try:
            url = make_url(raw)
            valid = (
                url.drivername == "postgresql+psycopg"
                and url.host in {"127.0.0.1", "localhost"}
                and url.port is not None
                and 1 <= url.port <= 65535
                and bool(url.username)
                and bool(url.password)
                and bool(url.database)
                and not url.query
                and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw)
            )
        except (ArgumentError, ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("an explicit local PostgreSQL URL is required") from None
        return secret


def load_database_settings() -> DatabaseSettings:
    try:
        return DatabaseSettings()
    except (ValidationError, ValueError):
        raise ConfigurationError("invalid_database_configuration") from None
