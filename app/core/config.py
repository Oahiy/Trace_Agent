"""Validated startup settings; no network calls or credentials in this module."""

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(Exception):
    """A fixed public error, without environment values or validation details."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRACEAGENT_",
        env_file=None,
        env_ignore_empty=True,
        extra="forbid",
        hide_input_in_errors=True,
        allow_inf_nan=False,
        frozen=True,
    )

    local_model_url: str = "http://127.0.0.1:11434"
    model_timeout_seconds: float = Field(default=30, gt=0, le=120)
    max_file_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=5 * 1024 * 1024)
    max_pdf_pages: int = Field(default=50, ge=1, le=50)
    max_text_characters: int = Field(default=200_000, ge=1, le=200_000)
    max_question_characters: int = Field(default=2000, ge=1, le=2000)
    upload_root: Path = Path(__file__).resolve().parents[2] / ".cache/day4/uploads"
    upload_timeout_seconds: float = Field(default=30, gt=0, le=30)
    parse_timeout_seconds: float = Field(default=60, gt=0, le=120)
    queue_enabled: bool = False

    @field_validator("upload_root")
    @classmethod
    def safe_upload_root(cls, value: Path) -> Path:
        if not value.is_absolute() or "sources" in value.parts:
            raise ValueError("an absolute non-source upload directory is required")
        return value

    @field_validator("local_model_url")
    @classmethod
    def local_origin(cls, value: str) -> str:
        address = urlsplit(value)
        if (
            any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in value
            )
            or address.scheme != "http"
            or address.hostname != "127.0.0.1"
            or address.username is not None
            or address.password is not None
            or address.path not in {"", "/"}
            or address.query
            or address.fragment
            or address.port == 0
        ):
            raise ValueError("a loopback HTTP origin is required")
        return value.rstrip("/")


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError:
        raise ConfigurationError("invalid_configuration") from None
