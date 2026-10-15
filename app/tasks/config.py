"""Local, authenticated broker configuration. URLs never enter argv or public errors."""

from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import ConfigurationError


class QueueSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRACEAGENT_QUEUE_",
        env_file=None,
        env_ignore_empty=True,
        hide_input_in_errors=True,
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
    )
    url: SecretStr
    name: str = Field(default="traceagent-parse", pattern=r"^traceagent-parse(?:-test)?$")
    execution_timeout_seconds: int = Field(default=300, ge=2, le=300)
    wait_timeout_seconds: int = Field(default=1800, ge=1, le=1800)

    @field_validator("url")
    @classmethod
    def local_broker(cls, secret):
        raw = secret.get_secret_value()
        try:
            url = urlsplit(raw)
            valid = (
                url.scheme == "redis"
                and url.hostname == "127.0.0.1"
                and url.port is not None
                and 1 <= url.port <= 65535
                and url.username in {None, "", "default"}
                and url.password is not None
                and len(url.password) == 64
                and all(c in "0123456789abcdef" for c in url.password)
                and url.path in {"/0", "/1"}
                and not url.query
                and not url.fragment
                and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("an authenticated explicit local broker URL is required") from None
        return secret


def load_queue_settings():
    try:
        return QueueSettings()
    except (ValidationError, ValueError):
        raise ConfigurationError("invalid_queue_configuration") from None
