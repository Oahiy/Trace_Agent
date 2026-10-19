"""Explicit local indexing: durable claim/accounting, bounded I/O, one atomic publication."""

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.db.embeddings import EmbeddingRepository
from app.db.models import IndexTask, Job
from app.db.repositories import (
    ChunkRepository,
    DocumentRepository,
    JobRepository,
    ParseTaskRepository,
    UserRepository,
)
from app.db.session import DatabaseUnavailable
from app.llm.budget import CallBudget
from app.llm.contracts import EMBED, ModelFailure
from app.llm.ollama import validate_vectors
from app.retrieval.contracts import INDEX_MESSAGES, SPEC, IndexFailure, content_digest
from app.services.documents import DataIntegrityError, ResourceNotFound, metadata_of, reference_of
from app.services.ingestion import IngestConflict
from app.storage.files import StorageUnavailable


def utcnow():
    return datetime.now(UTC)


@dataclass(frozen=True)
class InputChunk:
    id: UUID
    ordinal: int
    content: str
    pdf_page: int | None
    char_start: int | None
    char_end: int | None
    section: str | None


@dataclass(frozen=True)
class IndexOutcome:
    document_id: UUID
    status: str
    chunks: int
    index_version: str | None
    reused: bool
    error: str | None = None


def checked_vectors(vectors, count):
    try:
        result = validate_vectors([list(v) for v in vectors], count)
        if any(abs(math.sqrt(math.fsum(x * x for x in v)) - 1) > 0.001 for v in result):
            raise ValueError("normalization")
        return result
    except (ModelFailure, ValueError, TypeError, OverflowError):
        raise IndexFailure("index_invalid_embedding") from None


def input_chunks(session, owner_id, document_id):
    rows = ChunkRepository(session).for_document(owner_id, document_id, SPEC.ingest_version)
    if not rows or [c.ordinal for c in rows] != list(range(len(rows))):
        raise DataIntegrityError()
    return tuple(
        InputChunk(**{k: getattr(c, k) for k in InputChunk.__dataclass_fields__}) for c in rows
    )


def validate_complete(session, document, chunks):
    repository = EmbeddingRepository(session)
    repository.check_profile()
    rows = repository.stored(document.owner_id, document.id)
    if (
        document.index_version != SPEC.version
        or document.ingest_version != SPEC.ingest_version
        or [r.chunk_id for r in rows] != [c.id for c in chunks]
        or any(r.content_sha256 != content_digest(c.content) for r, c in zip(rows, chunks))
    ):
        raise DataIntegrityError()
    finished = session.scalar(
        select(IndexTask.job_id)
        .join(Job, Job.id == IndexTask.job_id)
        .where(
            Job.owner_id == document.owner_id,
            Job.document_id == document.id,
            Job.operation == "ingest",
            Job.status == "SUCCEEDED",
            IndexTask.status == "SUCCEEDED",
            IndexTask.index_version == SPEC.version,
        )
    )
    if finished is None:
        raise DataIntegrityError()
    try:
        checked_vectors([r.embedding for r in rows], len(chunks))
    except IndexFailure:
        raise DataIntegrityError() from None


class DurableIndexBudget(CallBudget):
    """Persist reservation BEFORE inference. A crash retains unknown charged usage."""

    def __init__(self, checkpoint):
        super().__init__()
        self.checkpoint = checkpoint
        self.persistence_failure = None

    def reserve(self, kind, estimated_input, output_limit):
        if kind != "embedding":
            raise ModelFailure("invalid_model_input")
        reserved = super().reserve(kind, estimated_input, output_limit)
        try:
            self.checkpoint(self)
        except (DatabaseUnavailable, IngestConflict, ResourceNotFound, IndexFailure) as error:
            self.persistence_failure = error
            raise ModelFailure("model_failed") from None
        return reserved


class IndexingService:
    def __init__(self, sessions, files, embedder, *, timeout=300, clock=utcnow):
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 300
        ):
            raise IndexFailure("index_failed")
        self.sessions, self.files, self.embedder = sessions, files, embedder
        self.timeout, self.clock = timeout, clock

    def _locked(self, session, owner_id, document_id):
        if not UserRepository(session).is_active(owner_id, lock=True):
            raise ResourceNotFound()
        document = DocumentRepository(session).get(owner_id, document_id, lock=True)
        if document is None:
            raise ResourceNotFound()
        job = JobRepository(session).for_document(owner_id, document_id)
        if job is None:
            raise DataIntegrityError()
        return document, job, session.get(IndexTask, job.id)

    @staticmethod
    def _outcome(document, chunks, reused, error=None):
        return IndexOutcome(
            document.id, document.status, len(chunks), document.index_version, reused, error
        )

    def _claim(self, owner_id, document_id):
        with self.sessions.begin() as session:
            document, job, task = self._locked(session, owner_id, document_id)
            if document.ingest_version != SPEC.ingest_version:
                raise IngestConflict()
            chunks = input_chunks(session, owner_id, document_id)
            if task is not None:
                if task.index_version != SPEC.version:
                    raise IngestConflict()
                if task.status == "RUNNING":
                    if document.status != "PARSED" or job.status != "RUNNING":
                        raise DataIntegrityError()
                    if task.deadline_at > self.clock():
                        raise IngestConflict()
                    self._fail_locked(document, job, task, "index_timeout")
                if task.status == "FAILED":
                    if (
                        document.status != "FAILED"
                        or job.status != "FAILED"
                        or document.error_code != task.error_code
                        or job.error_code != task.error_code
                    ):
                        raise DataIntegrityError()
                    code = task.error_code if task.error_code in INDEX_MESSAGES else "index_failed"
                    return self._outcome(document, chunks, True, code)
                if document.status != "READY" or job.status != "SUCCEEDED":
                    raise DataIntegrityError()
                validate_complete(session, document, chunks)
                return self._outcome(document, chunks, True)
            parse = ParseTaskRepository(session).get(job.id)
            if (
                document.status != "PARSED"
                or job.status != "PENDING"
                or job.attempt_count != 0
                or parse is None
                or parse.status != "SUCCEEDED"
            ):
                raise IngestConflict()
            # Reserve room for the adapter's one automatic retry per batch.
            if 2 * math.ceil(len(chunks) / 8) > 32:
                raise IndexFailure("index_budget_exhausted")
            if any(len(c.content) > 2000 for c in chunks):
                raise IndexFailure("index_invalid_embedding")
            if EmbeddingRepository(session).stored(owner_id, document_id):
                raise DataIntegrityError()
            EmbeddingRepository(session).ensure_profile()
            now, execution_id = self.clock(), uuid4()
            task = IndexTask(
                job_id=job.id,
                execution_id=execution_id,
                index_version=SPEC.version,
                status="RUNNING",
                started_at=now,
                deadline_at=now + timedelta(seconds=self.timeout),
                budget=CallBudget().snapshot(),
            )
            session.add(task)
            job.status, job.attempt_count, job.error_code = "RUNNING", 1, None
            return execution_id, chunks, metadata_of(document), reference_of(document)

    def _owned(self, session, owner_id, document_id, execution_id, *, deadline=True):
        document, job, task = self._locked(session, owner_id, document_id)
        if (
            task is None
            or task.execution_id != execution_id
            or task.status != "RUNNING"
            or job.status != "RUNNING"
            or document.status != "PARSED"
            or document.ingest_version != SPEC.ingest_version
            or task.index_version != SPEC.version
        ):
            raise IngestConflict()
        if deadline and task.deadline_at <= self.clock():
            raise IndexFailure("index_timeout")
        return document, job, task

    def _checkpoint(self, owner_id, document_id, execution_id, budget):
        try:
            with self.sessions.begin() as session:
                _, _, task = self._owned(session, owner_id, document_id, execution_id)
                task.budget = budget.snapshot()
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    def _fail_locked(self, document, job, task, code, budget=None):
        document.status, document.index_version, document.error_code = "FAILED", None, code
        job.status, job.error_code = "FAILED", code
        task.status, task.error_code, task.finished_at = "FAILED", code, self.clock()
        if budget is not None:
            task.budget = budget.snapshot()

    def _fail(self, owner_id, document_id, execution_id, code, budget):
        with self.sessions.begin() as session:
            document, job, task = self._owned(
                session, owner_id, document_id, execution_id, deadline=False
            )
            self._fail_locked(document, job, task, code, budget)
            return self._outcome(
                document, input_chunks(session, owner_id, document_id), False, code
            )

    def _finish(
        self, owner_id, document_id, execution_id, chunks, metadata, reference, vectors, budget
    ):
        with self.sessions.begin() as session:
            document, job, task = self._owned(session, owner_id, document_id, execution_id)
            if (
                metadata_of(document) != metadata
                or reference_of(document) != reference
                or input_chunks(session, owner_id, document_id) != chunks
            ):
                raise IngestConflict()
            repository = EmbeddingRepository(session)
            repository.check_profile()
            if repository.stored(owner_id, document_id):
                raise DataIntegrityError()
            for chunk, vector in zip(chunks, vectors, strict=True):
                repository.add(chunk, vector)
            if task.deadline_at <= self.clock():
                raise IndexFailure("index_timeout")
            # No await/remote operation occurs inside publication's transaction.
            document.status, document.index_version, document.error_code = (
                "READY",
                SPEC.version,
                None,
            )
            job.status, job.error_code = "SUCCEEDED", None
            task.status, task.finished_at, task.error_code = "SUCCEEDED", self.clock(), None
            task.budget = budget.snapshot()
            return self._outcome(document, chunks, False)

    async def index(self, owner_id, document_id):
        try:
            claim = self._claim(owner_id, document_id)
            if isinstance(claim, IndexOutcome):
                # Replays verify the original stored file too.
                with self.sessions() as session:
                    document = DocumentRepository(session).get(owner_id, document_id)
                    reference, metadata = reference_of(document), metadata_of(document)
                if reference is None:
                    raise StorageUnavailable()
                self.files.verify(reference, metadata.file_sha256)
                return claim
            execution_id, chunks, metadata, reference = claim
            budget = DurableIndexBudget(
                lambda b: self._checkpoint(owner_id, document_id, execution_id, b)
            )
            try:
                async with asyncio.timeout(self.timeout):
                    if reference is None:
                        raise StorageUnavailable()
                    self.files.verify(reference, metadata.file_sha256)
                    vectors = []
                    for start in range(0, len(chunks), 8):
                        batch = chunks[start : start + 8]
                        result = await self.embedder.embed_documents(
                            [c.content for c in batch], budget=budget
                        )
                        if budget.persistence_failure is not None:
                            raise budget.persistence_failure
                        if result.profile != EMBED:
                            raise IndexFailure("index_invalid_embedding")
                        vectors.extend(checked_vectors(result.vectors, len(batch)))
                        self._checkpoint(owner_id, document_id, execution_id, budget)
                    self.files.verify(reference, metadata.file_sha256)
                    return self._finish(
                        owner_id,
                        document_id,
                        execution_id,
                        chunks,
                        metadata,
                        reference,
                        vectors,
                        budget,
                    )
            except ModelFailure as error:
                if budget.persistence_failure is not None:
                    if isinstance(budget.persistence_failure, IndexFailure):
                        return self._fail(
                            owner_id,
                            document_id,
                            execution_id,
                            budget.persistence_failure.code,
                            budget,
                        )
                    raise budget.persistence_failure
                code = "index_model_failed"
                if error.code == "model_timeout":
                    code = "index_timeout"
                elif error.code in {"call_budget_exhausted", "token_budget_exhausted"}:
                    code = "index_budget_exhausted"
                return self._fail(owner_id, document_id, execution_id, code, budget)
            except TimeoutError:
                return self._fail(owner_id, document_id, execution_id, "index_timeout", budget)
            except StorageUnavailable:
                return self._fail(
                    owner_id, document_id, execution_id, "index_storage_unavailable", budget
                )
            except IndexFailure as error:
                return self._fail(owner_id, document_id, execution_id, error.code, budget)
            except asyncio.CancelledError:
                self._fail(owner_id, document_id, execution_id, "index_cancelled", budget)
                raise
            except (
                IngestConflict,
                DataIntegrityError,
                ResourceNotFound,
                DatabaseUnavailable,
                SQLAlchemyError,
            ):
                raise
            except Exception:
                return self._fail(owner_id, document_id, execution_id, "index_failed", budget)
        except IntegrityError:
            raise DataIntegrityError() from None
        except SQLAlchemyError:
            # Includes ambiguous COMMIT: caller must reconcile; never overwrite success.
            raise DatabaseUnavailable() from None
