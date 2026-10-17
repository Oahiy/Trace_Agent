"""MedEvidence dataset contracts; no API, database, network or model calls."""

import hashlib
import json
from collections import Counter
from datetime import date
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Category = Literal[
    "single_document", "cross_document", "no_answer", "conflict_evidence", "security"
]
EXPECTED_COUNTS = {
    "dev": {
        "single_document": 4,
        "cross_document": 2,
        "no_answer": 2,
        "conflict_evidence": 1,
        "security": 1,
    },
    "acceptance": {
        "single_document": 8,
        "cross_document": 4,
        "no_answer": 4,
        "conflict_evidence": 2,
        "security": 2,
    },
}


class DatasetRejected(Exception):
    """Only fixed codes reach CLI output, never raw validation input."""


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class Evidence(Contract):
    source_id: Identifier
    pdf_page: int = Field(ge=1, le=50)
    anchor: Text = Field(max_length=300)


class Source(Contract):
    source_id: Identifier
    title: Text = Field(max_length=300)
    authors: Text
    doi: Text
    publication_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    publication_year: int = Field(ge=1000, le=9999)
    version_label: Text
    research_dates: Text
    source_url: Text
    download_url: Text
    license: Text
    license_evidence_url: Text
    license_basis: Text
    cache_file: str = Field(pattern=r"^\.cache/day(?:1|2|8)/[a-z0-9-]+\.pdf$")
    retrieved_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    sha256: Digest
    bytes: int = Field(ge=1, le=5 * 1024 * 1024)
    page_count: int = Field(ge=1, le=50)
    characters: int = Field(ge=1, le=200_000)
    verification_anchors: list[Evidence] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_source(self) -> Self:
        if date.fromisoformat(self.publication_date).year != self.publication_year:
            raise ValueError("publication_date_mismatch")
        date.fromisoformat(self.retrieved_date)
        for value in (self.source_url, self.download_url, self.license_evidence_url):
            address = urlsplit(value)
            if (
                address.scheme != "https"
                or address.hostname != "journals.plos.org"
                or address.username is not None
                or address.password is not None
                or address.port not in (None, 443)
                or address.fragment
                or any(c.isspace() or ord(c) < 32 for c in value)
            ):
                raise ValueError("source_origin_rejected")
        if any(
            a.source_id != self.source_id or a.pdf_page > self.page_count
            for a in self.verification_anchors
        ):
            raise ValueError("source_anchor_invalid")
        return self


class ParsingProfile(Contract):
    parser: Literal["pypdf"]
    parser_version: Text
    ingest_version: Text
    chunk_characters: int = Field(gt=0)
    overlap_characters: int = Field(ge=0)


class Corpus(Contract):
    version: Literal["medevidence-corpus-v1"]
    topic: Text
    language: Literal["en"]
    review_status: Literal["agent_source_checked_developer_pending"]
    profile: ParsingProfile
    sources: list[Source] = Field(min_length=10, max_length=15)

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        for field in ("source_id", "doi", "sha256", "cache_file"):
            values = [getattr(source, field).casefold() for source in self.sources]
            if len(values) != len(set(values)):
                raise ValueError("duplicate_source")
        if self.profile.overlap_characters >= self.profile.chunk_characters:
            raise ValueError("invalid_chunk_profile")
        return self


class Conditions(Contract):
    # Portable source IDs are mapped to owner-scoped document UUIDs only after ingestion.
    source_ids: list[Identifier] = Field(min_length=1, max_length=15)
    publication_year_from: int | None = Field(default=None, ge=1000, le=9999)
    publication_year_to: int | None = Field(default=None, ge=1000, le=9999)

    @model_validator(mode="after")
    def valid_conditions(self) -> Self:
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("duplicate_scope")
        if self.publication_year_from is not None and self.publication_year_to is not None:
            if self.publication_year_from > self.publication_year_to:
                raise ValueError("reversed_years")
        return self


class EvalCase(Contract):
    case_id: Identifier
    split: Literal["dev", "acceptance"]
    category: Category
    principal: Literal["owner_a", "owner_b"] = "owner_a"
    question: Text = Field(max_length=2000)
    conditions: Conditions
    expected_behavior: Literal[
        "answer", "refuse_insufficient_evidence", "reject_unauthorized", "ignore_injection"
    ]
    expected_source_ids: list[Identifier]
    evidence: list[Evidence]
    critical_claims: list[Text] = Field(min_length=1)
    limitations: list[Text] = Field(min_length=1)
    relation: Literal[
        "single_source",
        "complementary",
        "version_difference",
        "condition_difference",
        "unresolved_conflict",
        "not_applicable",
    ]
    review_basis: Text

    @model_validator(mode="after")
    def coherent_expectations(self) -> Self:
        expected = self.expected_source_ids
        locations = [
            (e.source_id, e.pdf_page, "".join(e.anchor.split()).casefold()) for e in self.evidence
        ]
        if len(locations) != len(set(locations)):
            raise ValueError("duplicate_evidence")
        if len(expected) != len(set(expected)) or not set(expected) <= set(
            self.conditions.source_ids
        ):
            raise ValueError("expected_scope_invalid")
        if set(expected) != {e.source_id for e in self.evidence}:
            raise ValueError("expected_evidence_mismatch")
        answering = self.expected_behavior in {"answer", "ignore_injection"}
        if answering != bool(expected):
            raise ValueError("answer_evidence_required")
        if self.category == "cross_document" and len(expected) < 2:
            raise ValueError("multiple_sources_required")
        if self.category == "single_document" and (
            len(expected) != 1 or self.expected_behavior != "answer"
        ):
            raise ValueError("single_source_answer_required")
        if self.category == "conflict_evidence" and len(self.evidence) < 2:
            raise ValueError("multiple_evidence_locations_required")
        if (
            self.category == "no_answer"
            and self.expected_behavior != "refuse_insufficient_evidence"
        ):
            raise ValueError("no_answer_behavior_invalid")
        if self.category == "security" and self.expected_behavior not in {
            "reject_unauthorized",
            "ignore_injection",
        }:
            raise ValueError("security_behavior_required")
        if (self.expected_behavior == "reject_unauthorized") != (self.principal == "owner_b"):
            raise ValueError("ownership_fixture_invalid")
        if self.category == "conflict_evidence" and self.relation not in {
            "version_difference",
            "condition_difference",
            "unresolved_conflict",
        }:
            raise ValueError("conflict_relation_required")
        return self


class EvalSuite(Contract):
    version: Literal["medevidence-evals-v1"]
    corpus_sha256: Digest
    review_status: Literal["agent_source_checked_developer_pending"]
    cases: list[EvalCase] = Field(min_length=30, max_length=30)

    @model_validator(mode="after")
    def unique_and_balanced(self) -> Self:
        identifiers = [case.case_id for case in self.cases]
        questions = [" ".join(case.question.casefold().split()) for case in self.cases]
        if len(set(identifiers)) != len(identifiers) or len(set(questions)) != len(questions):
            raise ValueError("duplicate_case")
        for split, expected in EXPECTED_COUNTS.items():
            if Counter(case.category for case in self.cases if case.split == split) != expected:
                raise ValueError("split_counts_invalid")
        return self


def canonical_digest(value: Contract | dict) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, Contract) else value
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def check_references(corpus: Corpus, suite: EvalSuite) -> None:
    if suite.corpus_sha256 != canonical_digest(corpus):
        raise DatasetRejected("corpus_snapshot_mismatch")
    sources = {source.source_id: source for source in corpus.sources}
    for case in suite.cases:
        if not set(case.conditions.source_ids) <= sources.keys():
            raise DatasetRejected("unknown_source")
        for evidence in case.evidence:
            source = sources[evidence.source_id]
            if evidence.pdf_page > source.page_count:
                raise DatasetRejected("evidence_page_invalid")
            year = source.publication_year
            if (
                case.conditions.publication_year_from is not None
                and year < case.conditions.publication_year_from
            ) or (
                case.conditions.publication_year_to is not None
                and year > case.conditions.publication_year_to
            ):
                raise DatasetRejected("evidence_outside_years")


def acceptance_gaps(suite: EvalSuite) -> list[str]:
    gaps = ["developer_source_and_label_review_pending"]
    if (
        sum(
            c.category == "conflict_evidence" and c.relation == "unresolved_conflict"
            for c in suite.cases
        )
        < 3
    ):
        gaps.append("three_genuine_conflict_cases_not_established")
    return gaps
