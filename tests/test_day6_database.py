"""Real PostgreSQL/file tests; broker substitutes here are explicitly not real RQ."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from test_database import database as database
from test_database import owners as owners
from test_database import postgres as postgres
from test_day4_database import api as api
from test_day4_database import post

from app.db.models import Chunk, Document, Job, ParseTask, User
from app.db.session import DatabaseUnavailable
from app.services.documents import ResourceNotFound
from app.services.ingestion import IngestConflict, IngestionService
from app.services.tasks import TaskService
from app.tasks.config import QueueSettings
from app.tasks.queue import QueueUnavailable

pytestmark = [pytest.mark.database, pytest.mark.usefixtures("postgres")]


class BrokerSubstitute:
    def __init__(self):
        self.settings = QueueSettings(url="redis://:" + "a" * 64 + "@127.0.0.1:56379/1")
        self.calls = []

    def ensure(self, execution_id):
        self.calls.append(execution_id)
        return "queued"


def setup(api, data=b"day6 durable stage", media="text/plain"):
    client, app, owners = api
    response = post(client, data=data, media=media)
    assert response.status_code == 202
    job_id = UUID(response.json()["job_id"])
    document_id = UUID(response.json()["document_id"])
    queue = BrokerSubstitute()
    service = TaskService(app.state.database.sessions, queue)
    ingest = IngestionService(app.state.database.sessions, app.state.files, app.state.settings)
    return app.state.database, owners[0], job_id, document_id, queue, service, ingest


def test_stage_created_atomically_and_dedup_reuses_execution(api):
    db, owner, job, doc, queue, service, _ = setup(api)
    before = service.inspect(owner, job)
    again = post(api[0], data=b"day6 durable stage")
    assert again.status_code == 202 and again.json()["job_id"] == str(job)
    assert before.status == "PENDING"
    service.dispatch(owner, job)
    after = service.inspect(owner, job)
    assert after.execution_id == before.execution_id and after.status == "QUEUED"
    with db.sessions() as session:
        assert session.get(Document, doc).status == "UPLOADED"
        assert session.get(Job, job).status == "PENDING"


def test_claim_parse_and_stage_success_atomic_parent_not_complete(api):
    db, owner, job, doc, _, service, ingest = setup(api)
    task = service.dispatch(owner, job)
    assert service.claim(task.execution_id) == (owner, doc, job)
    outcome = ingest.parse(owner, doc, execution_id=task.execution_id)
    assert outcome.status == "PARSED"
    with db.sessions() as session:
        assert session.get(ParseTask, job).status == "SUCCEEDED"
        assert session.get(ParseTask, job).finished_at is not None
        assert session.get(Job, job).status == "PENDING"
        assert session.get(Job, job).attempt_count == 0
        assert session.get(Document, doc).index_version is None
    assert service.claim(task.execution_id) is None
    assert service.dispatch(owner, job).status == "SUCCEEDED"


def test_parse_failure_is_atomic_for_document_parent_and_stage(api):
    db, owner, job, doc, _, service, ingest = setup(api, b"%PDF-1.7\nbroken", "application/pdf")
    task = service.dispatch(owner, job)
    service.claim(task.execution_id)
    assert ingest.parse(owner, doc, execution_id=task.execution_id).status == "FAILED"
    with db.sessions() as session:
        assert session.get(ParseTask, job).status == session.get(Job, job).status == "FAILED"
        assert session.get(Document, doc).status == "FAILED"
        assert session.scalar(select(Chunk.id)) is None


def test_only_one_concurrent_claim_and_running_duplicate_does_not_extend_deadline(api):
    db, owner, job, doc, _, service, _ = setup(api)
    task = service.dispatch(owner, job)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(service.claim, [task.execution_id] * 2))
    assert results.count((owner, doc, job)) == 1 and results.count(None) == 1
    with db.sessions() as session:
        deadline = session.get(ParseTask, job).deadline_at
    assert service.claim(task.execution_id) is None
    with db.sessions() as session:
        assert session.get(ParseTask, job).deadline_at == deadline


def test_fast_worker_result_cannot_be_downgraded_by_delivery_ack(api):
    _, owner, job, doc, queue, service, ingest = setup(api)

    def fast(execution_id):
        service.claim(execution_id)
        ingest.parse(owner, doc, execution_id=execution_id)
        return "queued"

    queue.ensure = fast
    assert service.dispatch(owner, job).status == "SUCCEEDED"


def test_delivery_failure_preserves_job_file_and_pending_stage(api):
    db, owner, job, doc, queue, service, _ = setup(api)

    def unavailable(_):
        raise QueueUnavailable()

    queue.ensure = unavailable
    with pytest.raises(QueueUnavailable) as caught:
        service.dispatch(owner, job)
    assert caught.value.job_id == job
    assert service.inspect(owner, job).status == "PENDING"
    with db.sessions() as session:
        document = session.get(Document, doc)
        assert document.status == "UPLOADED"
        assert api[1].state.files.path(document.storage_key).is_file()


def test_owner_and_unknown_job_do_not_touch_broker(api):
    _, owner, job, _, queue, service, _ = setup(api)
    for wrong_owner, wrong_job in [(api[2][1], job), (owner, uuid4())]:
        with pytest.raises(ResourceNotFound):
            service.dispatch(wrong_owner, wrong_job)
    assert queue.calls == []


def test_disabled_owner_before_claim_is_terminal_without_file_work(api):
    db, owner, job, doc, _, service, _ = setup(api)
    task = service.dispatch(owner, job)
    with db.sessions.begin() as session:
        session.get(User, owner).active = False
    assert service.claim(task.execution_id) is None
    with db.sessions() as session:
        assert session.get(ParseTask, job).status == "FAILED"
        assert session.get(Document, doc).status == "FAILED"
        assert session.scalar(select(Chunk.id)) is None
    with pytest.raises(ResourceNotFound):
        service.inspect(owner, job)


def test_disabled_owner_during_execution_can_record_safe_failure(api):
    db, owner, job, doc, _, service, ingest = setup(api)
    task = service.dispatch(owner, job)
    service.claim(task.execution_id)
    with db.sessions.begin() as session:
        session.get(User, owner).active = False
    with pytest.raises(ResourceNotFound):
        ingest.parse(owner, doc, execution_id=task.execution_id)
    service.fail(owner, job, task.execution_id, "task_execution_failed")
    with db.sessions() as session:
        assert session.get(ParseTask, job).status == "FAILED"
        assert session.scalar(select(Chunk.id)) is None


def test_pending_and_queued_can_be_reconciled_without_changing_execution(api):
    _, owner, job, _, queue, service, _ = setup(api)
    original = service.inspect(owner, job)
    assert service.reconcile(owner)[0].status == "QUEUED"
    assert service.reconcile(owner)[0].execution_id == original.execution_id
    assert queue.calls == [original.execution_id] * 2


def test_expired_queue_wait_fails_without_delivery(api):
    db, owner, job, doc, queue, service, _ = setup(api)
    with db.sessions.begin() as session:
        session.get(ParseTask, job).created_at = datetime.now(UTC) - timedelta(seconds=1801)
    assert service.reconcile(owner)[0].error == "queue_wait_timeout"
    assert not queue.calls
    with db.sessions() as session:
        assert session.get(Document, doc).status == session.get(Job, job).status == "FAILED"


def test_running_deadline_fails_and_late_result_cannot_write_chunks(api):
    db, owner, job, doc, _, service, ingest = setup(api)
    task = service.dispatch(owner, job)
    service.claim(task.execution_id)
    with db.sessions.begin() as session:
        session.get(ParseTask, job).deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    assert service.reconcile(owner)[0].error == "task_timeout"
    with pytest.raises(IngestConflict):
        ingest.parse(owner, doc, execution_id=task.execution_id)
    with db.sessions() as session:
        assert session.scalar(select(Chunk.id)) is None


def test_old_execution_cannot_claim_or_save_after_token_change(api):
    db, owner, job, doc, _, service, ingest = setup(api)
    task = service.dispatch(owner, job)
    service.claim(task.execution_id)
    with db.sessions.begin() as session:
        session.get(ParseTask, job).execution_id = uuid4()
    assert service.claim(task.execution_id) is None
    with pytest.raises(IngestConflict):
        ingest.parse(owner, doc, execution_id=task.execution_id)
    with db.sessions() as session:
        assert session.scalar(select(Chunk.id)) is None


def test_local_command_cannot_bypass_queued_or_running_stage(api):
    _, owner, job, doc, _, service, ingest = setup(api)
    task = service.dispatch(owner, job)
    with pytest.raises(IngestConflict):
        ingest.parse(owner, doc)
    service.claim(task.execution_id)
    with pytest.raises(IngestConflict):
        ingest.parse(owner, doc)


def test_local_parse_finishes_stage_without_claiming_ingest_complete(api):
    _, owner, job, doc, queue, service, ingest = setup(api)
    ingest.parse(owner, doc)
    assert service.dispatch(owner, job).status == "SUCCEEDED" and not queue.calls


def test_failed_stage_insert_rolls_back_document_job_and_file(api):
    client, app, _ = api

    def reject(session, *_):
        for obj in session.new:
            if isinstance(obj, ParseTask):
                obj.status = "invalid"

    event.listen(app.state.database.sessions, "before_flush", reject)
    try:
        assert post(client).status_code == 500
    finally:
        event.remove(app.state.database.sessions, "before_flush", reject)
    with app.state.database.sessions() as session:
        assert session.scalar(select(Document.id)) is None
        assert session.scalar(select(Job.id)) is None
    assert not list(app.state.files.root.glob("*"))


def test_database_failure_during_stage_delivery_does_not_claim_success(api, monkeypatch):
    db, owner, job, _, _, service, _ = setup(api)

    def broken():
        raise OperationalError("secret-SQL", None, RuntimeError("password"))

    monkeypatch.setattr(service.sessions, "begin", broken)
    with pytest.raises(DatabaseUnavailable, match="^database_unavailable$"):
        service.dispatch(owner, job)
