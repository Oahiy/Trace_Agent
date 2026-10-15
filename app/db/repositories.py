"""Persistence operations share a caller-owned Session and never commit independently."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Chunk, Document, Job, ParseTask, User


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def is_active(self, owner_id: UUID, *, lock: bool = False) -> bool:
        query = select(User.id).where(User.id == owner_id, User.active.is_(True))
        if lock:
            query = query.with_for_update()
        return self.session.scalar(query) is not None

    def lock_active_flag(self, owner_id: UUID) -> bool:
        return (
            self.session.scalar(select(User.active).where(User.id == owner_id).with_for_update())
            is True
        )


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, document: Document) -> None:
        self.session.add(document)
        self.session.flush()

    def get(self, owner_id: UUID, document_id: UUID, *, lock: bool = False) -> Document | None:
        query = select(Document).where(Document.id == document_id, Document.owner_id == owner_id)
        return self.session.scalar(query.with_for_update() if lock else query)

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


class ChunkRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, chunk: Chunk) -> None:
        self.session.add(chunk)
        self.session.flush()

    def for_document(self, owner_id: UUID, document_id: UUID, version: str) -> list[Chunk]:
        return list(
            self.session.scalars(
                select(Chunk)
                .join(Document, Chunk.document_id == Document.id)
                .where(
                    Document.owner_id == owner_id,
                    Document.id == document_id,
                    Chunk.ingest_version == version,
                )
                .order_by(Chunk.ordinal)
            )
        )


class ParseTaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, job_id: UUID, *, lock: bool = False) -> ParseTask | None:
        query = select(ParseTask).where(ParseTask.job_id == job_id)
        return self.session.scalar(query.with_for_update() if lock else query)

    def identity(self, execution_id: UUID):
        return self.session.execute(
            select(Job.owner_id, Job.document_id, Job.id)
            .join(ParseTask, ParseTask.job_id == Job.id)
            .where(ParseTask.execution_id == execution_id, Job.operation == "ingest")
        ).first()

    def pending(self, owner_id: UUID, *, limit: int = 50):
        return self.session.scalars(
            select(ParseTask.job_id)
            .join(Job, Job.id == ParseTask.job_id)
            .where(Job.owner_id == owner_id, ParseTask.status.in_(("PENDING", "QUEUED", "RUNNING")))
            .order_by(ParseTask.created_at, ParseTask.job_id)
            .limit(limit)
        ).all()
