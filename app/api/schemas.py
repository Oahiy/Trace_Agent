"""Version-one contract models. Research endpoints are not mounted on Day 2."""

from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
IdempotencyKey = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
]


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    question: Question
    document_ids: list[str] | None = Field(default=None, min_length=1, max_length=15)
    publication_year_from: int | None = Field(default=None, ge=1000, le=9999)
    publication_year_to: int | None = Field(default=None, ge=1000, le=9999)
    idempotency_key: IdempotencyKey

    @field_validator("question")
    @classmethod
    def safe_question(cls, value: str) -> str:
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("question must be valid Unicode") from None
        if any(ord(c) < 32 and c not in "\t\n\r" for c in value):
            raise ValueError("question contains invalid control characters")
        return value

    @field_validator("document_ids")
    @classmethod
    def canonical_ids(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        canonical = []
        for value in values:
            try:
                parsed = UUID(value)
            except ValueError:
                raise ValueError("document IDs must be canonical UUID strings") from None
            if str(parsed) != value:
                raise ValueError("document IDs must be canonical UUID strings")
            canonical.append(value)
        if len(set(canonical)) != len(canonical):
            raise ValueError("duplicate document IDs are not allowed")
        return canonical

    @model_validator(mode="after")
    def ordered_years(self) -> Self:
        if (
            self.publication_year_from is not None
            and self.publication_year_to is not None
            and self.publication_year_from > self.publication_year_to
        ):
            raise ValueError("publication year range is reversed")
        return self


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str
    message: str


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    error: ErrorDetail
    job_id: str | None = None
