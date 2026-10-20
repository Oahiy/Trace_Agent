"""Scenario-independent storage. UUIDs and state values are internal, not client input."""

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={"pk": "pk_%(table_name)s"})


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("api_key_digest", name="uq_users_api_key_digest"),
        CheckConstraint("api_key_digest ~ '^[0-9a-f]{64}$'", name="ck_users_api_key_digest"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    api_key_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_documents_id_owner"),
        UniqueConstraint(
            "owner_id", "file_sha256", "ingest_version", name="uq_documents_owner_file_ingest"
        ),
        CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name="ck_documents_sha256"),
        CheckConstraint("char_length(btrim(title)) BETWEEN 1 AND 200", name="ck_documents_title"),
        CheckConstraint("char_length(btrim(source_id)) > 0", name="ck_documents_source_id"),
        CheckConstraint(
            "char_length(btrim(ingest_version)) > 0", name="ck_documents_ingest_version"
        ),
        CheckConstraint("publication_year BETWEEN 1000 AND 9999", name="ck_documents_year"),
        CheckConstraint(
            "status IN ('UPLOADED','PARSED','READY','FAILED')", name="ck_documents_status"
        ),
        CheckConstraint(
            "status <> 'READY' OR index_version IS NOT NULL", name="ck_documents_ready_index"
        ),
        Index("ix_documents_owner_status", "owner_id", "status"),
        UniqueConstraint("storage_key", name="uq_documents_storage_key"),
        CheckConstraint(
            "(storage_key IS NULL AND file_size_bytes IS NULL AND file_media_type IS NULL) OR "
            "(storage_key IS NOT NULL AND file_size_bytes IS NOT NULL "
            "AND file_media_type IS NOT NULL AND storage_key ~ '^[0-9a-f]{32}$' "
            "AND file_size_bytes BETWEEN 1 AND 5242880 "
            "AND file_media_type IN ('text/plain','application/pdf'))",
            name="ck_documents_stored_file",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", name="fk_documents_owner", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200))
    source_url: Mapped[str] = mapped_column(String(2048))
    license: Mapped[str] = mapped_column(String(200))
    license_evidence_url: Mapped[str] = mapped_column(String(2048))
    publication_year: Mapped[int | None] = mapped_column(Integer)
    version_label: Mapped[str | None] = mapped_column(String(200))
    research_dates: Mapped[str | None] = mapped_column(String(2000))
    file_sha256: Mapped[str] = mapped_column(String(64))
    ingest_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), server_default="UPLOADED")
    index_version: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    storage_key: Mapped[str | None] = mapped_column(String(32))
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    file_media_type: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "ingest_version", "ordinal", name="uq_chunks_document_version_ordinal"
        ),
        CheckConstraint("ordinal >= 0", name="ck_chunks_ordinal"),
        CheckConstraint(
            "char_length(btrim(content)) BETWEEN 1 AND 200000", name="ck_chunks_content"
        ),
        CheckConstraint("char_length(btrim(ingest_version)) > 0", name="ck_chunks_ingest_version"),
        CheckConstraint(
            "(pdf_page IS NOT NULL AND pdf_page > 0 AND char_start IS NULL AND char_end IS NULL) "
            "OR (pdf_page IS NULL AND char_start IS NOT NULL AND char_end IS NOT NULL "
            "AND char_start >= 0 AND char_end > char_start)",
            name="ck_chunks_locator",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", name="fk_chunks_document", ondelete="RESTRICT"), nullable=False
    )
    ingest_version: Mapped[str] = mapped_column(String(32))
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    pdf_page: Mapped[int | None] = mapped_column(Integer)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "owner_id"],
            ["documents.id", "documents.owner_id"],
            name="fk_jobs_document_owner",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("document_id", "operation", name="uq_jobs_document_operation"),
        UniqueConstraint(
            "owner_id", "operation", "idempotency_key", name="uq_jobs_owner_operation_key"
        ),
        CheckConstraint("operation IN ('ingest','research')", name="ck_jobs_operation"),
        CheckConstraint(
            "(operation = 'ingest' AND document_id IS NOT NULL) "
            "OR (operation = 'research' AND document_id IS NULL)",
            name="ck_jobs_document_kind",
        ),
        CheckConstraint(
            "status IN ('PENDING','QUEUED','RUNNING','SUCCEEDED','FAILED')", name="ck_jobs_status"
        ),
        CheckConstraint("attempt_count BETWEEN 0 AND 2", name="ck_jobs_attempt_count"),
        CheckConstraint(
            "(idempotency_key IS NULL AND input_sha256 IS NULL) OR "
            "(idempotency_key IS NOT NULL AND input_sha256 IS NOT NULL "
            "AND idempotency_key ~ '^[A-Za-z0-9_-]{1,64}$' "
            "AND input_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_jobs_idempotency_pair",
        ),
        Index("ix_jobs_owner_status", "owner_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", name="fk_jobs_owner", ondelete="RESTRICT"), nullable=False
    )
    document_id: Mapped[UUID | None] = mapped_column(Uuid)
    operation: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), server_default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    input_sha256: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ParseTask(Base):
    """One durable preprocessing stage per ingest job, not a completed ingest attempt."""

    __tablename__ = "parse_tasks"
    __table_args__ = (
        UniqueConstraint("execution_id", name="uq_parse_tasks_execution"),
        CheckConstraint(
            "status IN ('PENDING','QUEUED','RUNNING','SUCCEEDED','FAILED')",
            name="ck_parse_tasks_status",
        ),
        CheckConstraint(
            "(status = 'RUNNING' AND started_at IS NOT NULL AND deadline_at IS NOT NULL) "
            "OR status <> 'RUNNING'",
            name="ck_parse_tasks_running",
        ),
        CheckConstraint(
            "(status IN ('SUCCEEDED','FAILED') AND finished_at IS NOT NULL) "
            "OR (status NOT IN ('SUCCEEDED','FAILED') AND finished_at IS NULL)",
            name="ck_parse_tasks_finished",
        ),
        Index("ix_parse_tasks_status_created", "status", "created_at"),
    )

    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("jobs.id", name="fk_parse_tasks_job", ondelete="CASCADE"), primary_key=True
    )
    execution_id: Mapped[UUID] = mapped_column(Uuid, default=uuid4)
    status: Mapped[str] = mapped_column(String(16), server_default="PENDING")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmbeddingProfile(Base):
    __tablename__ = "embedding_profiles"
    __table_args__ = (
        CheckConstraint("dimensions = 768", name="ck_embedding_profiles_dimensions"),
        CheckConstraint("model_digest ~ '^[0-9a-f]{64}$'", name="ck_embedding_profiles_digest"),
    )

    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    model_name: Mapped[str] = mapped_column(String(100))
    model_digest: Mapped[str] = mapped_column(String(64))
    dimensions: Mapped[int] = mapped_column(Integer)
    preprocessing_version: Mapped[str] = mapped_column(String(100))
    normalization: Mapped[str] = mapped_column(String(100))
    ingest_version: Mapped[str] = mapped_column(String(32))


class ChunkEmbedding(Base):
    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_chunk_embeddings_hash"),
        CheckConstraint("vector_norm(embedding) > 0", name="ck_chunk_embeddings_nonzero"),
    )

    chunk_id: Mapped[UUID] = mapped_column(
        ForeignKey("chunks.id", name="fk_chunk_embeddings_chunk", ondelete="CASCADE"),
        primary_key=True,
    )
    index_version: Mapped[str] = mapped_column(
        ForeignKey("embedding_profiles.version", name="fk_chunk_embeddings_profile"),
        primary_key=True,
    )
    content_sha256: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float]] = mapped_column(VECTOR(768))


class IndexTask(Base):
    """One explicit, durable indexing execution; failed executions are not auto-retried."""

    __tablename__ = "index_tasks"
    __table_args__ = (
        UniqueConstraint("execution_id", name="uq_index_tasks_execution"),
        CheckConstraint("status IN ('RUNNING','SUCCEEDED','FAILED')", name="ck_index_tasks_status"),
        CheckConstraint("deadline_at > started_at", name="ck_index_tasks_deadline"),
        CheckConstraint(
            "(status = 'RUNNING' AND finished_at IS NULL) OR "
            "(status IN ('SUCCEEDED','FAILED') AND finished_at IS NOT NULL)",
            name="ck_index_tasks_finished",
        ),
    )

    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("jobs.id", name="fk_index_tasks_job", ondelete="CASCADE"), primary_key=True
    )
    execution_id: Mapped[UUID] = mapped_column(Uuid, default=uuid4)
    index_version: Mapped[str] = mapped_column(
        ForeignKey("embedding_profiles.version", name="fk_index_tasks_profile")
    )
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    budget: Mapped[dict] = mapped_column(JSON)


class ResearchRun(Base):
    """Research payload and execution lease; jobs owns the single business status."""

    __tablename__ = "research_runs"
    __table_args__ = (
        UniqueConstraint("id", name="uq_research_runs_id"),
        UniqueConstraint("execution_id", name="uq_research_runs_execution"),
        CheckConstraint(
            "(started_at IS NULL AND deadline_at IS NULL) OR "
            "(started_at IS NOT NULL AND deadline_at > started_at)",
            name="ck_research_runs_deadline",
        ),
        CheckConstraint(
            "result IS NULL OR finished_at IS NOT NULL", name="ck_research_runs_result"
        ),
    )

    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("jobs.id", name="fk_research_runs_job", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(Uuid, default=uuid4)
    execution_id: Mapped[UUID] = mapped_column(Uuid, default=uuid4)
    workflow_version: Mapped[str] = mapped_column(String(40))
    request: Mapped[dict] = mapped_column(JSON)
    budget: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
