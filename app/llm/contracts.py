"""Provider-independent values and interfaces. No scenario rules or network I/O."""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

ERROR_CODES = frozenset(
    {
        "invalid_model_input",
        "model_client_closed",
        "model_timeout",
        "model_unavailable",
        "model_http_error",
        "model_rate_limited",
        "model_response_too_large",
        "invalid_model_json",
        "invalid_model_response",
        "invalid_model_usage",
        "model_output_truncated",
        "model_output_invalid",
        "model_usage_limit_exceeded",
        "model_identity_unavailable",
        "model_identity_mismatch",
        "model_cancelled",
        "invalid_embedding_count",
        "embedding_dimension_mismatch",
        "invalid_embedding_value",
        "zero_embedding",
        "call_budget_exhausted",
        "token_budget_exhausted",
        "budget_in_use",
        "attempt_budget_exhausted",
        "invalid_model_configuration",
        "model_failed",
    }
)


class ModelFailure(Exception):
    """Only allowlisted codes can cross the provider boundary."""

    def __init__(self, code: str):
        self.code = code if code in ERROR_CODES else "model_failed"
        super().__init__(self.code)


class Message(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)

    @field_validator("content")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError("unsupported message")
        return value


@dataclass(frozen=True)
class ModelProfile:
    name: str
    digest: str
    dimensions: int | None = None
    preprocessing_version: str | None = None


CHAT = ModelProfile(
    "qwen3:1.7b", "8f68893c685c3ddff2aa3fffce2aa60a30bb2da65ca488b61fff134a4d1730e7"
)
EMBED = ModelProfile(
    "nomic-embed-text:v1.5",
    "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
    768,
    "nomic-task-prefix-v1",
)


@dataclass(frozen=True)
class AttemptRecord:
    task_attempt: int
    request_attempt: int
    kind: Literal["generation", "embedding"]
    model: str
    model_digest: str
    estimated_input_tokens: int
    estimation_method: str
    input_tokens: int | None
    output_tokens: int | None
    charged_tokens: int
    seconds: float
    status: Literal["succeeded", "failed", "cancelled"]
    error_code: str | None
    http_status: int | None
    cloud_api_cost_usd: int = 0


@dataclass(frozen=True)
class GenerationResult[T: BaseModel]:
    data: T
    usage: AttemptRecord


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    profile: ModelProfile
    usage: AttemptRecord


class Generator(Protocol):
    async def generate[T: BaseModel](
        self, messages: list[Message], schema: type[T], *, budget, max_output_tokens: int = 1024
    ) -> GenerationResult[T]: ...


class Embedder(Protocol):
    async def embed_documents(self, texts: list[str], *, budget) -> EmbeddingResult: ...

    async def embed_query(self, text: str, *, budget) -> EmbeddingResult: ...
