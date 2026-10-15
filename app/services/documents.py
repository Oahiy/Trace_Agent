"""Scenario-independent registration. A document and its task commit together."""

from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Document, Job, ParseTask
from app.db.repositories import DocumentRepository, JobRepository, UserRepository
from app.db.session import DatabaseUnavailable
from app.storage.files import FileReference

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
URL = Annotated[str, StringConstraints(min_length=1, max_length=2048)]


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
        frozen=True,
        revalidate_instances="always",
    )

    source_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    title: Title
    source_url: URL
    license: Title
    license_evidence_url: URL
    publication_year: int | None = Field(default=None, ge=1000, le=9999)
    version_label: Title | None = None
    research_dates: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    file_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", max_length=64)]
    ingest_version: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,32}$", max_length=32)
    ]

    @field_validator("source_url", "license_evidence_url")
    @classmethod
    def traceability_url(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.port == 0
            or url.username is not None
            or url.password is not None
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError("a source HTTP URL is required")
        # Metadata only: no request to this URL is made.
        return value


class ResourceNotFound(Exception):
    def __init__(self) -> None:
        super().__init__("resource_not_found")


class SourceMetadataConflict(Exception):
    def __init__(self) -> None:
        super().__init__("source_metadata_conflict")


class DataIntegrityError(Exception):
    def __init__(self) -> None:
        super().__init__("data_integrity_error")


class TooManyPendingTasks(Exception):
    def __init__(self) -> None:
        super().__init__("too_many_pending_tasks")


@dataclass(frozen=True)
class Registration:
    document_id: UUID
    job_id: UUID
    document_status: str
    job_status: str
    reused: bool
    stored_file: FileReference | None = None


@dataclass(frozen=True)
class StoredDocument:
    id: UUID
    owner_id: UUID
    metadata: DocumentMetadata
    status: str
    index_version: str | None = None
    error_code: str | None = None
    stored_file: FileReference | None = None


def reference_of(document: Document) -> FileReference | None:
    if document.storage_key is None:
        if document.file_size_bytes is not None or document.file_media_type is not None:
            raise DataIntegrityError()
        return None
    try:
        return FileReference(
            key=document.storage_key,
            size_bytes=document.file_size_bytes,
            media_type=document.file_media_type,
        )
    except ValidationError:
        raise DataIntegrityError() from None


def metadata_of(document: Document) -> DocumentMetadata:
    try:
        return DocumentMetadata(
            **{key: getattr(document, key) for key in DocumentMetadata.model_fields}
        )
    except ValidationError:
        raise DataIntegrityError() from None


class DocumentService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    @staticmethod
    def _reuse(session: Session, document: Document, request: DocumentMetadata) -> Registration:
        if metadata_of(document) != request:
            raise SourceMetadataConflict() from None
        job = JobRepository(session).for_document(document.owner_id, document.id)
        if job is None:
            raise DataIntegrityError() from None
        return Registration(
            document.id, job.id, document.status, job.status, True, reference_of(document)
        )

    def register(
        self, owner_id: UUID, payload: object, stored_file: FileReference | None = None
    ) -> Registration:
        request = DocumentMetadata.model_validate(payload)
        if stored_file is not None:
            stored_file = FileReference.model_validate(stored_file)
        try:
            with self.sessions.begin() as session:
                if not UserRepository(session).is_active(owner_id, lock=stored_file is not None):
                    raise ResourceNotFound()
                repository = DocumentRepository(session)
                existing = repository.by_fingerprint(
                    owner_id, request.file_sha256, request.ingest_version
                )
                if existing is not None:
                    result = self._reuse(session, existing, request)
                else:
                    if (
                        stored_file is not None
                        and JobRepository(session).unfinished_count(owner_id) >= 5
                    ):
                        raise TooManyPendingTasks()
                    document = Document(id=uuid4(), owner_id=owner_id, **request.model_dump())
                    if stored_file is not None:
                        document.storage_key = stored_file.key
                        document.file_size_bytes = stored_file.size_bytes
                        document.file_media_type = stored_file.media_type
                    repository.add(document)
                    job = Job(
                        id=uuid4(), owner_id=owner_id, document_id=document.id, operation="ingest"
                    )
                    JobRepository(session).add(job)
                    if stored_file is not None:
                        session.add(ParseTask(job_id=job.id))
                    result = Registration(
                        document.id, job.id, document.status, job.status, False, stored_file
                    )
            # The context has committed before success is returned.
            return result
        except IntegrityError as error:
            constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            if constraint == "uq_documents_owner_file_ingest":
                # A concurrent creator won. Reconcile after rollback, with a fresh Session.
                try:
                    with self.sessions.begin() as session:
                        existing = DocumentRepository(session).by_fingerprint(
                            owner_id, request.file_sha256, request.ingest_version
                        )
                        if existing is None:
                            raise DataIntegrityError()
                        return self._reuse(session, existing, request)
                except SQLAlchemyError:
                    raise DatabaseUnavailable() from None
            raise DataIntegrityError() from None
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    def get(self, owner_id: UUID, document_id: UUID) -> StoredDocument | None:
        try:
            with self.sessions() as session:
                document = DocumentRepository(session).get(owner_id, document_id)
                if document is None:
                    return None
                return StoredDocument(
                    document.id,
                    document.owner_id,
                    metadata_of(document),
                    document.status,
                    document.index_version,
                    document.error_code,
                    reference_of(document),
                )
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None
