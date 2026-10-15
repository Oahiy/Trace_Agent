"""Durable parse delivery, atomic claims, deadlines and explicit reconciliation."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.db.models import Job
from app.db.repositories import DocumentRepository, ParseTaskRepository, UserRepository
from app.db.session import DatabaseUnavailable
from app.services.documents import ResourceNotFound
from app.tasks.queue import QueueUnavailable


@dataclass(frozen=True)
class TaskSnapshot:
    job_id: UUID
    execution_id: UUID
    status: str
    error: str | None


def utcnow():
    return datetime.now(UTC)


class TaskService:
    def __init__(self, sessions, queue, *, clock=utcnow):
        self.sessions, self.queue, self.clock = sessions, queue, clock

    def _locked(self, session, owner_id, job_id, *, allow_inactive=False):
        active = UserRepository(session).lock_active_flag(owner_id)
        if not active and not allow_inactive:
            raise ResourceNotFound()
        job = session.get(Job, job_id)
        if job is None or job.owner_id != owner_id or job.operation != "ingest":
            raise ResourceNotFound()
        document = DocumentRepository(session).get(owner_id, job.document_id, lock=True)
        if document is None:
            raise ResourceNotFound()
        task = ParseTaskRepository(session).get(job_id, lock=True)
        return job, document, task

    def inspect(self, owner_id, job_id):
        try:
            with self.sessions.begin() as session:
                _, _, task = self._locked(session, owner_id, job_id)
                return self._snapshot(task) if task else None
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    @staticmethod
    def _snapshot(task):
        return TaskSnapshot(task.job_id, task.execution_id, task.status, task.error_code)

    def dispatch(self, owner_id, job_id):
        try:
            with self.sessions.begin() as session:
                job, document, task = self._locked(session, owner_id, job_id)
                if task is None or task.status in {"SUCCEEDED", "FAILED", "RUNNING"}:
                    return self._snapshot(task) if task else None
                now = self.clock()
                if (
                    now - task.created_at
                ).total_seconds() >= self.queue.settings.wait_timeout_seconds:
                    self._fail(job, document, task, "queue_wait_timeout", now)
                    return self._snapshot(task)
                execution_id = task.execution_id
            # No SQL connection/transaction is held during broker I/O.
            try:
                transport = self.queue.ensure(execution_id)
            except QueueUnavailable:
                raise QueueUnavailable(job_id) from None
            with self.sessions.begin() as session:
                job, document, task = self._locked(session, owner_id, job_id)
                if task.execution_id == execution_id and task.status in {"PENDING", "QUEUED"}:
                    if transport in {"failed", "stopped", "canceled", "finished"}:
                        self._fail(job, document, task, "task_execution_failed", self.clock())
                    else:
                        task.status = "QUEUED"
                        task.queued_at = task.queued_at or self.clock()
                return self._snapshot(task)
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    def claim(self, execution_id):
        try:
            with self.sessions.begin() as session:
                identity = ParseTaskRepository(session).identity(execution_id)
                if identity is None:
                    return None
                owner_id, document_id, job_id = identity
                job, document, task = self._locked(session, owner_id, job_id, allow_inactive=True)
                if task.execution_id != execution_id or task.status not in {"PENDING", "QUEUED"}:
                    return None
                now = self.clock()
                if not UserRepository(session).is_active(owner_id):
                    self._fail(job, document, task, "task_execution_failed", now)
                    return None
                if job.status != "PENDING" or document.ingest_version != "ingest-v1":
                    self._fail(job, document, task, "ingest_state_conflict", now)
                    return None
                if (
                    now - task.created_at
                ).total_seconds() >= self.queue.settings.wait_timeout_seconds:
                    self._fail(job, document, task, "queue_wait_timeout", now)
                    return None
                task.status = "RUNNING"
                task.started_at = now
                task.deadline_at = now + timedelta(
                    seconds=self.queue.settings.execution_timeout_seconds
                )
                return owner_id, document_id, job_id
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    @staticmethod
    def _fail(job, document, task, code, now):
        task.status, task.error_code, task.finished_at = "FAILED", code, now
        # Never delete chunks or downgrade previously stored evidence.
        if document.status == "UPLOADED":
            document.status, document.error_code = "FAILED", code
        if job.status == "PENDING":
            job.status, job.error_code = "FAILED", code

    def fail(self, owner_id, job_id, execution_id, code):
        try:
            with self.sessions.begin() as session:
                job, document, task = self._locked(session, owner_id, job_id, allow_inactive=True)
                if task and task.execution_id == execution_id and task.status == "RUNNING":
                    self._fail(job, document, task, code, self.clock())
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    def reconcile(self, owner_id):
        try:
            with self.sessions() as session:
                identifiers = ParseTaskRepository(session).pending(owner_id)
            results = []
            for job_id in identifiers:
                with self.sessions.begin() as session:
                    job, document, task = self._locked(session, owner_id, job_id)
                    if task.status == "RUNNING":
                        if self.clock() >= task.deadline_at:
                            self._fail(job, document, task, "task_timeout", self.clock())
                        results.append(self._snapshot(task))
                        continue
                results.append(self.dispatch(owner_id, job_id))
            return results
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None
