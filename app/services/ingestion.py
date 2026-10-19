"""First parsing stage: bounded computation outside short atomic DB transactions."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.config import Settings
from app.db.models import Chunk, IndexTask
from app.db.repositories import (
    ChunkRepository,
    DocumentRepository,
    JobRepository,
    ParseTaskRepository,
    UserRepository,
)
from app.db.session import DatabaseUnavailable
from app.ingestion.parser import IsolatedParser
from app.ingestion.text import ERROR_MESSAGES, INGEST_VERSION, ParseRejected, split_text
from app.retrieval.contracts import INDEX_MESSAGES
from app.services.documents import DataIntegrityError, ResourceNotFound, metadata_of, reference_of
from app.storage.files import FileStore, StorageUnavailable


class IngestConflict(Exception):
    def __init__(self) -> None:
        super().__init__("ingest_state_conflict")


@dataclass(frozen=True)
class ParseOutcome:
    document_id: UUID
    status: str
    chunks: int
    reused: bool
    error: str | None = None


class IngestionService:
    def __init__(self, sessions, files: FileStore, settings: Settings, *, parser=None) -> None:
        self.sessions = sessions
        self.files = files
        self.settings = settings
        self.parser = parser if parser is not None else IsolatedParser(settings)

    def _document(self, session, owner_id, document_id, *, lock=False):
        if not UserRepository(session).is_active(owner_id, lock=lock):
            raise ResourceNotFound()
        document = DocumentRepository(session).get(owner_id, document_id, lock=lock)
        if document is None:
            raise ResourceNotFound()
        if document.ingest_version != INGEST_VERSION:
            raise IngestConflict()
        return document

    def _existing(self, session, document) -> ParseOutcome | None:
        chunks = ChunkRepository(session).for_document(
            document.owner_id, document.id, document.ingest_version
        )
        if document.status in {"PARSED", "READY"}:
            if not chunks:
                raise DataIntegrityError()
            return ParseOutcome(document.id, document.status, len(chunks), True)
        if document.status == "FAILED":
            if chunks:
                job = JobRepository(session).for_document(document.owner_id, document.id)
                index = session.get(IndexTask, job.id) if job else None
                parse = ParseTaskRepository(session).get(job.id) if job else None
                if (
                    index is None
                    or index.status != "FAILED"
                    or parse is None
                    or parse.status != "SUCCEEDED"
                    or job.status != "FAILED"
                    or document.error_code != index.error_code
                    or job.error_code != index.error_code
                    or document.error_code not in INDEX_MESSAGES
                ):
                    raise DataIntegrityError()
                return ParseOutcome(document.id, "FAILED", len(chunks), True, document.error_code)
            code = document.error_code
            return ParseOutcome(
                document.id, "FAILED", 0, True, code if code in ERROR_MESSAGES else "internal_error"
            )
        if document.status != "UPLOADED" or chunks:
            raise DataIntegrityError()
        return None

    @staticmethod
    def _task(session, job, execution_id):
        task = ParseTaskRepository(session).get(job.id, lock=True)
        if execution_id is not None:
            if (
                task is None
                or task.execution_id != execution_id
                or task.status != "RUNNING"
                or task.deadline_at <= datetime.now(UTC)
            ):
                raise IngestConflict()
        elif task is not None and task.status in {"QUEUED", "RUNNING"}:
            raise IngestConflict()
        return task

    def parse(self, owner_id: UUID, document_id: UUID, *, execution_id=None) -> ParseOutcome:
        try:
            with self.sessions() as session:
                document = self._document(session, owner_id, document_id)
                reference = reference_of(document)
                metadata = metadata_of(document)
                existing = self._existing(session, document)
                job = JobRepository(session).for_document(owner_id, document_id)
                if job is None:
                    raise DataIntegrityError()
                if execution_id is not None or existing is None:
                    self._task(session, job, execution_id)
            if reference is None:
                raise StorageUnavailable()
            # The actual parsed bytes are exactly the bytes whose size/hash were verified.
            data = self.files.read_verified(reference, metadata.file_sha256)
            if existing is not None:
                return existing
            try:
                parsed = self.parser.parse(data, reference.media_type)
                chunks = split_text(parsed)
                if not chunks:
                    raise ParseRejected("no_extractable_text")
            except ParseRejected as error:
                return self._save(
                    owner_id,
                    document_id,
                    reference,
                    metadata,
                    error=error.code,
                    execution_id=execution_id,
                )
            return self._save(
                owner_id, document_id, reference, metadata, chunks=chunks, execution_id=execution_id
            )
        except IntegrityError:
            raise DataIntegrityError() from None
        except SQLAlchemyError:
            # Includes lost commit acknowledgement: retain data for idempotent reconciliation.
            raise DatabaseUnavailable() from None

    def _save(
        self,
        owner_id,
        document_id,
        reference,
        metadata,
        *,
        chunks=(),
        error=None,
        execution_id=None,
    ):
        with self.sessions.begin() as session:
            document = self._document(session, owner_id, document_id, lock=True)
            if reference_of(document) != reference or metadata_of(document) != metadata:
                raise IngestConflict()
            job = JobRepository(session).for_document(owner_id, document_id)
            if job is None:
                raise DataIntegrityError()
            # The parent ingest remains pending until indexing can be scheduled.
            if job.status != "PENDING":
                raise IngestConflict()
            task = self._task(session, job, execution_id)
            existing = self._existing(session, document)
            if existing is not None:
                return existing
            if error is not None:
                document.status, document.error_code = "FAILED", error
                job.status, job.error_code = "FAILED", error
                outcome = ParseOutcome(document_id, "FAILED", 0, False, error)
            else:
                repository = ChunkRepository(session)
                for chunk in chunks:
                    repository.add(
                        Chunk(
                            document_id=document_id,
                            ingest_version=document.ingest_version,
                            ordinal=chunk.ordinal,
                            content=chunk.content,
                            pdf_page=chunk.pdf_page,
                            char_start=chunk.char_start,
                            char_end=chunk.char_end,
                        )
                    )
                document.status, document.error_code = "PARSED", None
                # Indexing has not run. The overall ingest job remains PENDING.
                document.index_version = None
                outcome = ParseOutcome(document_id, "PARSED", len(chunks), False)
            if task is not None:
                task.status = "FAILED" if error else "SUCCEEDED"
                task.error_code, task.finished_at = error, datetime.now(UTC)
        return outcome

    def chunks(self, owner_id: UUID, document_id: UUID) -> list[dict]:
        try:
            with self.sessions() as session:
                document = self._document(session, owner_id, document_id)
                return [
                    {
                        "id": str(chunk.id),
                        "ordinal": chunk.ordinal,
                        "content": chunk.content,
                        "pdf_page": chunk.pdf_page,
                        "char_start": chunk.char_start,
                        "char_end": chunk.char_end,
                        "ingest_version": chunk.ingest_version,
                    }
                    for chunk in ChunkRepository(session).for_document(
                        owner_id, document_id, document.ingest_version
                    )
                ]
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None
