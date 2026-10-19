"""Retrieval values are scenario independent; similarity never establishes claim support."""

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.ingestion.text import INGEST_VERSION
from app.llm.contracts import EMBED

INDEX_MESSAGES = {
    "index_timeout": "Document indexing exceeded its deadline.",
    "index_cancelled": "Document indexing was cancelled.",
    "index_model_failed": "Embedding service could not complete indexing.",
    "index_invalid_embedding": "Embedding output does not match the index specification.",
    "index_budget_exhausted": "Document exceeds the indexing request budget.",
    "index_failed": "Document indexing could not be completed.",
    "index_storage_unavailable": "Source file could not be verified for indexing.",
}


class IndexFailure(Exception):
    def __init__(self, code):
        self.code = code if code in INDEX_MESSAGES else "index_failed"
        super().__init__(self.code)


@dataclass(frozen=True)
class IndexSpec:
    model_name: str = EMBED.name
    model_digest: str = EMBED.digest
    dimensions: int = EMBED.dimensions
    preprocessing_version: str = EMBED.preprocessing_version
    normalization: str = "provider-l2-unit-v1"
    ingest_version: str = INGEST_VERSION

    @property
    def version(self):
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return "e1-" + sha256(encoded).hexdigest()[:24]


SPEC = IndexSpec()


def content_digest(content):
    return sha256(content.encode("utf-8")).hexdigest()


class RetrievalRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )

    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    document_ids: list[str] | None = Field(default=None, min_length=1, max_length=15)
    publication_year_from: int | None = Field(default=None, ge=1000, le=9999)
    publication_year_to: int | None = Field(default=None, ge=1000, le=9999)
    limit: int = Field(default=5, ge=1, le=5)

    @field_validator("document_ids")
    @classmethod
    def canonical_ids(cls, value):
        if value is not None:
            try:
                if any(str(UUID(v)) != v for v in value) or len(set(value)) != len(value):
                    raise ValueError("invalid IDs")
            except ValueError:
                raise ValueError("canonical unique document IDs required") from None
        return value

    @model_validator(mode="after")
    def ordered_years(self):
        if (
            self.publication_year_from is not None
            and self.publication_year_to is not None
            and self.publication_year_from > self.publication_year_to
        ):
            raise ValueError("publication year range is reversed")
        return self
