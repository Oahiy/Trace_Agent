"""Durable, bounded research execution; scenario rules are injected at composition."""

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.api.schemas import ResearchRequest
from app.db.models import Document, Job, ResearchRun
from app.db.repositories import JobRepository, UserRepository
from app.db.research import ResearchRepository
from app.db.session import DatabaseUnavailable
from app.llm.budget import CallBudget
from app.llm.contracts import ModelFailure
from app.research.contracts import MESSAGES, LeaseLost, ResearchFailure
from app.retrieval.contracts import IndexFailure
from app.services.documents import DataIntegrityError, ResourceNotFound, TooManyPendingTasks
from app.services.ingestion import IngestConflict
from app.services.retrieval import RetrievalService
from app.tasks.queue import QueueUnavailable


def utcnow():
    return datetime.now(UTC)


def normalized_request(payload):
    request = ResearchRequest.model_validate(payload)
    value = request.model_dump(exclude={"idempotency_key"})
    if value["document_ids"] is not None:
        value["document_ids"] = sorted(value["document_ids"])
    digest = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return request.idempotency_key, value, digest


class DurableResearchBudget(CallBudget):
    def __init__(self, checkpoint):
        super().__init__(generation_limit=2, embedding_limit=2)
        self.checkpoint = checkpoint
        self.persistence_failure = None

    def reserve(self, kind, estimated_input, output_limit):
        reserved = super().reserve(kind, estimated_input, output_limit)
        try:
            self.checkpoint(self)
        except Exception as error:
            self.persistence_failure = error
            raise
        return reserved


class ResearchService:
    def __init__(self, sessions, queue=None, *, workflow_version, timeout=180, clock=utcnow):
        if not 0 < timeout <= 180:
            raise ValueError("invalid_research_timeout")
        self.sessions, self.queue = sessions, queue
        self.version, self.clock = workflow_version, clock
        self.timeout = (
            min(timeout, getattr(queue.settings, "execution_timeout_seconds", 180))
            if queue
            else timeout
        )
        self.wait_timeout = queue.settings.wait_timeout_seconds if queue else 1800

    @staticmethod
    def _ownership(session, owner_id, request):
        ids = request["document_ids"]
        if ids is not None:
            found = list(
                session.scalars(
                    select(Document.id).where(
                        Document.owner_id == owner_id, Document.id.in_([UUID(i) for i in ids])
                    )
                )
            )
            if len(found) != len(ids):
                raise ResourceNotFound()

    def _locked(self, session, owner_id, job_id, *, allow_inactive=False):
        active = UserRepository(session).lock_active_flag(owner_id)
        if not active and not allow_inactive:
            raise ResourceNotFound()
        row = ResearchRepository(session).get(owner_id, job_id, lock=True)
        if row is None:
            raise ResourceNotFound()
        return row

    @staticmethod
    def _accepted(job, run):
        return {"job_id": str(job.id), "run_id": str(run.id), "status": job.status}

    @staticmethod
    def _public(job, run):
        if (job.status == "SUCCEEDED") != (run.result is not None):
            raise DataIntegrityError()
        code = job.error_code if job.error_code in MESSAGES else "research_failed"
        return {
            **ResearchService._accepted(job, run),
            "question": run.request["question"],
            "conditions": {k: v for k, v in run.request.items() if k != "question"},
            "result": deepcopy(run.result),
            "error": {"code": code, "message": MESSAGES[code]} if job.status == "FAILED" else None,
        }

    def create(self, owner_id, payload):
        key, request, digest = normalized_request(payload)
        try:
            with self.sessions.begin() as session:
                if not UserRepository(session).is_active(owner_id, lock=True):
                    raise ResourceNotFound()
                self._ownership(session, owner_id, request)
                existing = ResearchRepository(session).by_key(owner_id, key)
                if existing:
                    job, run = existing
                    if job.input_sha256 != digest or run.request != request:
                        raise ResearchFailure("idempotency_conflict")
                else:
                    if JobRepository(session).unfinished_count(owner_id) >= 5:
                        raise TooManyPendingTasks()
                    job = Job(
                        id=uuid4(),
                        owner_id=owner_id,
                        operation="research",
                        idempotency_key=key,
                        input_sha256=digest,
                        status="PENDING",
                    )
                    JobRepository(session).add(job)
                    run = ResearchRun(
                        job_id=job.id,
                        id=uuid4(),
                        execution_id=uuid4(),
                        request=request,
                        workflow_version=self.version,
                        budget=CallBudget(generation_limit=2, embedding_limit=2).snapshot(),
                    )
                    session.add(run)
                return self._accepted(job, run)
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    def get(self, owner_id, job_id):
        try:
            with self.sessions() as session:
                if not UserRepository(session).is_active(owner_id):
                    raise ResourceNotFound()
                row = ResearchRepository(session).get(owner_id, job_id)
                if row is None:
                    raise ResourceNotFound()
                self._ownership(session, owner_id, row[1].request)
                return self._public(*row)
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    def dispatch(self, owner_id, job_id):
        try:
            with self.sessions.begin() as session:
                job, run = self._locked(session, owner_id, job_id)
                if job.status not in {"PENDING", "QUEUED"}:
                    return self._accepted(job, run)
                if (self.clock() - job.created_at).total_seconds() >= self.wait_timeout:
                    self._fail(job, run, "queue_wait_timeout")
                    return self._accepted(job, run)
                execution = run.execution_id
            if self.queue is None:
                raise QueueUnavailable(job_id)
            try:
                transport = self.queue.ensure(execution)
            except QueueUnavailable:
                raise QueueUnavailable(job_id) from None
            with self.sessions.begin() as session:
                job, run = self._locked(session, owner_id, job_id)
                if run.execution_id == execution and job.status in {"PENDING", "QUEUED"}:
                    if transport in {"failed", "finished", "stopped", "canceled"}:
                        self._fail(job, run, "research_failed")
                    else:
                        job.status, run.queued_at = "QUEUED", run.queued_at or self.clock()
                return self._accepted(job, run)
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    def claim(self, execution_id):
        try:
            with self.sessions.begin() as session:
                identity = ResearchRepository(session).identity(execution_id)
                if identity is None:
                    return None
                owner_id, job_id = identity
                job, run = self._locked(session, owner_id, job_id, allow_inactive=True)
                if run.execution_id != execution_id or job.status not in {"PENDING", "QUEUED"}:
                    return None
                if not UserRepository(session).is_active(owner_id):
                    self._fail(job, run, "research_failed")
                    return None
                if (self.clock() - job.created_at).total_seconds() >= self.wait_timeout:
                    self._fail(job, run, "queue_wait_timeout")
                    return None
                if run.workflow_version != self.version or run.started_at or job.attempt_count:
                    self._fail(job, run, "research_state_conflict")
                    return None
                try:
                    self._ownership(session, owner_id, run.request)
                except ResourceNotFound:
                    self._fail(job, run, "research_evidence_changed")
                    return None
                job.status, job.attempt_count = "RUNNING", 1
                run.started_at = self.clock()
                run.deadline_at = run.started_at + timedelta(seconds=self.timeout)
                return owner_id, job_id, deepcopy(run.request), run.deadline_at
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    def _lease(self, session, owner_id, job_id, execution_id):
        job, run = self._locked(session, owner_id, job_id, allow_inactive=True)
        if run.execution_id != execution_id or job.status != "RUNNING":
            raise LeaseLost()
        if self.clock() >= run.deadline_at:
            raise ResearchFailure("research_timeout")
        if not UserRepository(session).is_active(owner_id):
            raise ResearchFailure("research_failed")
        _, _, digest = normalized_request({**run.request, "idempotency_key": job.idempotency_key})
        if digest != job.input_sha256 or run.workflow_version != self.version:
            raise ResearchFailure("research_state_conflict")
        return job, run

    def checkpoint(self, owner_id, job_id, execution_id, budget, evidence=None):
        try:
            with self.sessions.begin() as session:
                _, run = self._lease(session, owner_id, job_id, execution_id)
                run.budget = budget.snapshot()
                if evidence is not None:
                    run.evidence = deepcopy(evidence)
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    def _fail(self, job, run, code):
        job.status, job.error_code = "FAILED", code
        run.result, run.finished_at = None, self.clock()

    def fail(self, owner_id, job_id, execution_id, code, budget=None):
        try:
            with self.sessions.begin() as session:
                job, run = self._locked(session, owner_id, job_id, allow_inactive=True)
                if run.execution_id != execution_id or job.status != "RUNNING":
                    return
                self._fail(job, run, code if code in MESSAGES else "research_failed")
                if budget is not None:
                    run.budget = budget.snapshot()
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    def finish(self, owner_id, job_id, execution_id, budget, result, retriever, payload):
        try:
            with self.sessions.begin() as session:
                job, run = self._lease(session, owner_id, job_id, execution_id)
                retriever.validate_snapshot(session, owner_id, payload, result["data_snapshot"])
                if self.clock() >= run.deadline_at:
                    raise ResearchFailure("research_timeout")
                run.result, run.budget, run.finished_at = result, budget.snapshot(), self.clock()
                job.status, job.error_code = "SUCCEEDED", None
        except SQLAlchemyError:
            # COMMIT may already be durable. Never overwrite it with a guessed failure.
            raise DatabaseUnavailable() from None

    def reconcile(self, owner_id):
        try:
            with self.sessions() as session:
                ids = ResearchRepository(session).unfinished(owner_id)
            results = []
            for job_id in ids:
                with self.sessions.begin() as session:
                    job, run = self._locked(session, owner_id, job_id)
                    if job.status == "RUNNING":
                        if run.deadline_at is None:
                            raise DataIntegrityError()
                        if self.clock() >= run.deadline_at:
                            self._fail(job, run, "research_timeout")
                        results.append(self._accepted(job, run))
                        continue
                results.append(self.dispatch(owner_id, job_id))
            return results
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    async def execute(self, execution_id, model, workflow):
        identity = self.claim(execution_id)
        if identity is None:
            return {"status": "ignored"}
        owner, job_id, request, deadline = identity
        budget = DurableResearchBudget(lambda b: self.checkpoint(owner, job_id, execution_id, b))
        retriever = RetrievalService(self.sessions, model)
        payload = {**request, "limit": 5}
        try:
            remaining = (deadline - self.clock()).total_seconds()
            async with asyncio.timeout(max(0, remaining)):
                reason = workflow.refusal_reason(request["question"])
                evidence = (
                    retriever.inspect_scope(owner, payload)
                    if reason
                    else await retriever.search(owner, payload, budget=budget)
                )
                self.checkpoint(owner, job_id, execution_id, budget, evidence)
                if reason or not evidence["hits"]:
                    result = workflow.refused(evidence, reason or "insufficient_evidence")
                else:
                    messages, selected, omitted = workflow.pack_context(
                        request["question"], evidence["hits"]
                    )
                    if not selected:
                        raise ResearchFailure("research_context_too_large")
                    generated = await model.generate(
                        messages, workflow.schema, budget=budget, max_output_tokens=512
                    )
                    result = workflow.bind_selection(generated.data, selected, evidence)
                    result["context_chunk_ids"] = [h["chunk_id"] for h in selected.values()]
                    result["omitted_chunk_ids"] = omitted
                self.checkpoint(owner, job_id, execution_id, budget)
                self.finish(owner, job_id, execution_id, budget, result, retriever, payload)
        except BaseException as caught:
            error = budget.persistence_failure or caught
            if isinstance(error, DatabaseUnavailable):
                raise error
            if isinstance(error, LeaseLost):
                return {"status": "ignored"}
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, asyncio.CancelledError):
                self.fail(owner, job_id, execution_id, "research_cancelled", budget)
                raise
            if isinstance(error, TimeoutError):
                code = "research_timeout"
            elif isinstance(error, ResearchFailure):
                code = error.code
            elif isinstance(error, ModelFailure):
                code = (
                    "research_budget_exhausted"
                    if error.code in {"call_budget_exhausted", "token_budget_exhausted"}
                    else "research_output_invalid"
                    if error.code
                    in {
                        "model_output_invalid",
                        "model_output_truncated",
                        "invalid_model_response",
                        "invalid_model_json",
                    }
                    else "research_model_failed"
                )
            elif isinstance(error, (IngestConflict, IndexFailure, ResourceNotFound)):
                code = "research_evidence_changed"
            else:
                code = "research_failed"
            self.fail(owner, job_id, execution_id, code, budget)
        # Active-user checks still apply when returning this internal execution summary.
        return {"status": self.get(owner, job_id)["status"]}
