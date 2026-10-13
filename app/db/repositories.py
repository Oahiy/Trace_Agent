"""Persistence operations share a caller-owned Session and never commit independently."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Document, Job, User


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def is_active(self, owner_id: UUID, *, lock: bool = False) -> bool:
        query = select(User.id).where(User.id == owner_id, User.active.is_(True))
        if lock:
            query = query.with_for_update()
        return self.session.scalar(query) is not None


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, document: Document) -> None:
        self.session.add(document)
        self.session.flush()

    def get(self, owner_id: UUID, document_id: UUID) -> Document | None:
        return self.session.scalar(
            select(Document).where(Document.id == document_id, Document.owner_id == owner_id)
        )

    def by_fingerprint(
        self, owner_id: UUID, file_sha256: str, ingest_version: str
    ) -> Document | None:
        return self.session.scalar(
            select(Document).where(
                Document.owner_id == owner_id,
                Document.file_sha256 == file_sha256,
                Document.ingest_version == ingest_version,
            )
        )


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, job: Job) -> None:
        self.session.add(job)
        self.session.flush()

    def unfinished_count(self, owner_id: UUID) -> int:
        return self.session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.owner_id == owner_id, Job.status.in_(("PENDING", "QUEUED", "RUNNING")))
        )

    def for_document(self, owner_id: UUID, document_id: UUID) -> Job | None:
        return self.session.scalar(
            select(Job).where(
                Job.owner_id == owner_id,
                Job.document_id == document_id,
                Job.operation == "ingest",
            )
        )
