"""Real PostgreSQL with deterministic inference and injected failures."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from test_database import database as database
from test_database import migration
from test_database import owners as owners
from test_database import postgres as postgres
from test_day4_database import KEY_A, KEY_B
from test_day4_database import api as api
from test_day10_database import FakeEmbedder, rows, run, service, upload

from app.db.models import Document, Job, ResearchRun, User
from app.db.research import ResearchRepository
from app.db.session import DatabaseUnavailable
from app.domains.med.research import MedResearchWorkflow
from app.llm.contracts import ModelFailure
from app.research.contracts import ResearchFailure
from app.services.documents import ResourceNotFound, TooManyPendingTasks
from app.services.research import ResearchService, utcnow
from app.tasks.queue import QueueUnavailable

pytestmark = [pytest.mark.database, pytest.mark.usefixtures("postgres")]
TEXT = "PRISMA is a reporting guideline for systematic reviews. It preserves source traceability."
QUESTION = "What is the PRISMA reporting guideline?"


class Model(FakeEmbedder):
    def __init__(self, *, selection=None, generate_hook=None, error=None):
        super().__init__()
        self.generations = 0
        self.selection, self.generate_hook, self.error = selection, generate_hook, error

    async def generate(self, messages, schema, *, budget, max_output_tokens):
        self.generations += 1
        with budget.operation():
            reservation = budget.reserve("generation", 100, max_output_tokens)
            if self.generate_hook:
                value = self.generate_hook()
                if hasattr(value, "__await__"):
                    await value
            if self.error:
                raise self.error
            wire = json.loads(messages[-1].content)["evidence"]
            selection = self.selection or {"evidence_id": next(iter(wire)), "sentence": 1}
            budget.settle("generation", reservation, 100, max_output_tokens, 80, 20)
            return SimpleNamespace(data=schema.model_validate(selection))


class Queue:
    settings = SimpleNamespace(wait_timeout_seconds=1800)

    def __init__(self):
        self.calls = []

    def ensure(self, execution):
        self.calls.append(execution)
        return "queued"


def research(app, queue=None, **kwargs):
    return ResearchService(
        app.state.database.sessions, queue, workflow_version=MedResearchWorkflow.version, **kwargs
    )


def ready(api):
    _, app, owners = api
    identifier = upload(api, data=TEXT.encode(), year=2021)
    assert run(service(app).index(owners[0], identifier)).status == "READY"
    return identifier


def payload(identifier=None, key="same", **kwargs):
    return {
        "question": QUESTION,
        "document_ids": [str(identifier)] if identifier else None,
        "idempotency_key": key,
        **kwargs,
    }


def create(app, owner, request, **kwargs):
    svc = research(app, **kwargs)
    accepted = svc.create(owner, request)
    with app.state.database.sessions() as session:
        execution = session.get(ResearchRun, UUID(accepted["job_id"])).execution_id
    return svc, accepted, execution


def execute(svc, execution, model=None):
    return run(svc.execute(execution, model or Model(), MedResearchWorkflow()))


def test_real_create_execute_query_and_successful_replay_without_inference(api):
    _, app, owners = api
    identifier = ready(api)
    svc, accepted, execution = create(app, owners[0], payload(identifier))
    model = Model()
    assert execute(svc, execution, model)["status"] == "SUCCEEDED"
    result = svc.get(owners[0], UUID(accepted["job_id"]))
    assert result["result"]["kind"] == "answer" and result["error"] is None
    assert result["result"]["claims"][0]["citations"][0]["char_start"] == 0
    assert result["result"]["claims"][0]["text"] in TEXT
    assert execute(svc, execution, model) == {"status": "ignored"}
    assert model.calls == model.generations == 1
    assert svc.create(owners[0], payload(identifier))["job_id"] == accepted["job_id"]
    saved = rows(app, ResearchRun)[0]
    assert saved.budget["embedding_calls"] == saved.budget["generation_calls"] == 1


def test_idempotency_conflict_owner_isolation_and_repository_filter(api):
    _, app, owners = api
    identifier = ready(api)
    svc, accepted, _ = create(app, owners[0], payload(identifier))
    with pytest.raises(ResearchFailure, match="idempotency_conflict"):
        svc.create(owners[0], payload(identifier, question="Different reporting question?"))
    for job in (UUID(accepted["job_id"]), uuid4()):
        with pytest.raises(ResourceNotFound):
            svc.get(owners[1], job)
        with app.state.database.sessions() as session:
            assert ResearchRepository(session).get(owners[1], job) is None
    with pytest.raises(ResourceNotFound):
        svc.create(owners[1], payload(identifier))


def test_concurrent_create_and_claim_have_one_winner(api):
    _, app, owners = api
    identifier = ready(api)
    svc = research(app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: svc.create(owners[0], payload(identifier)), range(2)))
    assert results[0] == results[1]
    execution = rows(app, ResearchRun)[0].execution_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: svc.claim(execution), range(2)))
    assert sum(c is not None for c in claims) == 1


def test_five_unfinished_jobs_include_ingest_and_research_and_reuse_is_exempt(api):
    _, app, owners = api
    # A parsed ingest remains unfinished and counts against the same quota.
    identifier = upload(api)
    svc = research(app)
    for index in range(4):
        svc.create(owners[0], payload(identifier, key=f"r{index}"))
    assert svc.create(owners[0], payload(identifier, key="r0"))
    with pytest.raises(TooManyPendingTasks):
        svc.create(owners[0], payload(identifier, key="overflow"))


@pytest.mark.parametrize(
    "mode", ["empty_year", "medical", "unsupported", "model_refusal", "forged_quote"]
)
def test_refusals_are_completed_results_with_no_unearned_claims(api, mode):
    _, app, owners = api
    identifier = ready(api)
    request = payload(identifier)
    model = Model()
    if mode == "empty_year":
        request.update(publication_year_from=2009, publication_year_to=2009)
    elif mode == "medical":
        request["question"] = "PRISMA reporting: give me a treatment dosage"
    elif mode == "unsupported":
        request["question"] = "What is tomorrow's weather?"
    elif mode == "model_refusal":
        model.selection = {"evidence_id": "", "sentence": 0}
    else:
        model.selection = {
            "evidence_id": "E1",
            "sentence": 99,
        }
    svc, accepted, execution = create(app, owners[0], request)
    assert execute(svc, execution, model)["status"] == "SUCCEEDED"
    result = svc.get(owners[0], UUID(accepted["job_id"]))["result"]
    assert result["kind"] == "refused" and result["claims"] == []
    if mode in {"empty_year", "medical", "unsupported"}:
        assert model.calls == model.generations == 0


def test_context_too_large_fails_before_generation_without_truncating_evidence(api):
    _, app, owners = api
    identifier = upload(api, data=("中" * 1000).encode(), year=2021)
    run(service(app).index(owners[0], identifier))
    svc, accepted, execution = create(app, owners[0], payload(identifier))
    model = Model()
    execute(svc, execution, model)
    assert (
        svc.get(owners[0], UUID(accepted["job_id"]))["error"]["code"]
        == "research_context_too_large"
    )
    assert model.generations == 0


@pytest.mark.parametrize(
    "code,expected",
    [
        ("model_timeout", "research_model_failed"),
        ("model_output_invalid", "research_output_invalid"),
        ("token_budget_exhausted", "research_budget_exhausted"),
    ],
)
def test_failed_generation_is_safe_and_preserves_reserved_budget(api, code, expected):
    _, app, owners = api
    identifier = ready(api)
    svc, accepted, execution = create(app, owners[0], payload(identifier))
    execute(svc, execution, Model(error=ModelFailure(code)))
    result = svc.get(owners[0], UUID(accepted["job_id"]))
    assert result["status"] == "FAILED" and result["result"] is None
    assert result["error"]["code"] == expected
    assert rows(app, ResearchRun)[0].budget["generation_tokens_charged"] == 3312


def test_model_wait_has_no_connection_and_both_reservations_are_already_durable(api):
    _, app, owners = api
    identifier = ready(api)
    svc, _, execution = create(app, owners[0], payload(identifier))

    def check():
        assert app.state.database.engine.pool.checkedout() == 0
        assert rows(app, ResearchRun)[0].budget["generation_calls"] == 1
        assert rows(app, ResearchRun)[0].budget["embedding_calls"] == 1

    assert execute(svc, execution, Model(generate_hook=check))["status"] == "SUCCEEDED"


def test_deadline_and_cancellation_are_failed_without_publishing(api):
    _, app, owners = api
    identifier = ready(api)

    async def delayed():
        await asyncio.sleep(0.1)

    svc, accepted, execution = create(app, owners[0], payload(identifier), timeout=0.04)
    execute(svc, execution, Model(generate_hook=delayed))
    assert svc.get(owners[0], UUID(accepted["job_id"]))["error"]["code"] == "research_timeout"
    svc, accepted, execution = create(app, owners[0], payload(identifier, key="cancel"))

    def cancelled():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        execute(svc, execution, Model(generate_hook=cancelled))
    assert rows(app, ResearchRun)[1].result is None
    assert svc.get(owners[0], UUID(accepted["job_id"]))["error"]["code"] == "research_cancelled"


@pytest.mark.parametrize("mode", ["metadata", "execution", "disabled", "input"])
def test_late_change_or_stale_execution_never_publishes(api, mode):
    _, app, owners = api
    identifier = ready(api)
    svc, accepted, execution = create(app, owners[0], payload(identifier))

    def change():
        with app.state.database.sessions.begin() as session:
            if mode == "metadata":
                session.get(Document, identifier).title = "changed"
            elif mode == "execution":
                session.get(ResearchRun, UUID(accepted["job_id"])).execution_id = uuid4()
            elif mode == "disabled":
                session.get(User, owners[0]).active = False
            else:
                row = session.get(ResearchRun, UUID(accepted["job_id"]))
                row.request = {**row.request, "question": "Changed PRISMA reporting question"}

    try:
        execute(svc, execution, Model(generate_hook=change))
    except ResourceNotFound:
        assert mode == "disabled"
    assert rows(app, ResearchRun)[0].result is None
    if mode == "metadata":
        assert (
            svc.get(owners[0], UUID(accepted["job_id"]))["error"]["code"]
            == "research_evidence_changed"
        )
    if mode == "input":
        assert (
            svc.get(owners[0], UUID(accepted["job_id"]))["error"]["code"]
            == "research_state_conflict"
        )


@pytest.mark.parametrize("when", ["before_commit", "after_commit"])
def test_real_final_transaction_rollback_or_ack_loss_is_never_guessed_failed(api, when):
    _, app, owners = api
    identifier = ready(api)
    svc, accepted, execution = create(app, owners[0], payload(identifier))
    original = app.state.database.sessions

    class FaultySessions:
        def __call__(self):
            return original()

        @contextmanager
        def begin(self):
            publishing = False
            with original.begin() as session:
                yield session
                publishing = any(
                    isinstance(item, Job)
                    and item.operation == "research"
                    and item.status == "SUCCEEDED"
                    for item in session.dirty
                )
                if publishing and when == "before_commit":
                    session.flush()
                    raise OperationalError("COMMIT", None, RuntimeError("private-marker"))
            if publishing and when == "after_commit":
                raise OperationalError("COMMIT", None, RuntimeError("private-marker"))

    svc.sessions = FaultySessions()
    with pytest.raises(DatabaseUnavailable):
        execute(svc, execution)
    value = svc.get(owners[0], UUID(accepted["job_id"]))
    assert value["status"] == ("RUNNING" if when == "before_commit" else "SUCCEEDED")
    if when == "before_commit":
        assert value["result"] is None
        svc.clock = lambda: utcnow() + timedelta(seconds=181)
        assert svc.reconcile(owners[0])[0]["status"] == "FAILED"
    else:
        assert execute(svc, execution) == {"status": "ignored"}


def test_scope_validation_cannot_publish_after_deadline(api, monkeypatch):
    from app.services.retrieval import RetrievalService

    _, app, owners = api
    identifier = ready(api)
    svc, accepted, execution = create(app, owners[0], payload(identifier))
    original = RetrievalService.validate_snapshot

    def late(*args):
        original(*args)
        svc.clock = lambda: utcnow() + timedelta(seconds=181)

    monkeypatch.setattr(RetrievalService, "validate_snapshot", late)
    execute(svc, execution)
    assert svc.get(owners[0], UUID(accepted["job_id"]))["error"]["code"] == "research_timeout"


def test_document_scope_corruption_before_claim_is_terminal_not_stuck_pending(api):
    _, app, owners = api
    identifier = ready(api)
    svc, accepted, execution = create(app, owners[0], payload(identifier))
    with app.state.database.sessions.begin() as session:
        row = session.get(ResearchRun, UUID(accepted["job_id"]))
        row.request = {**row.request, "document_ids": [str(uuid4())]}
    assert svc.claim(execution) is None
    assert next(j for j in rows(app, Job) if j.operation == "research").status == "FAILED"


def test_research_deadline_respects_a_shorter_transport_timeout(api):
    _, app, owners = api
    queue = Queue()
    queue.settings = SimpleNamespace(wait_timeout_seconds=1800, execution_timeout_seconds=2)
    svc, accepted, execution = create(app, owners[0], payload(), queue=queue)
    svc.claim(execution)
    row = rows(app, ResearchRun)[0]
    assert (row.deadline_at - row.started_at).total_seconds() == 2


def test_lost_dispatch_ack_preserves_pending_and_replay_uses_same_execution(api):
    _, app, owners = api
    identifier = ready(api)
    queue = Queue()
    svc, accepted, execution = create(app, owners[0], payload(identifier), queue=queue)

    def failed(value):
        raise QueueUnavailable()

    queue.ensure = failed
    with pytest.raises(QueueUnavailable) as error:
        svc.dispatch(owners[0], UUID(accepted["job_id"]))
    assert error.value.job_id == UUID(accepted["job_id"])
    assert svc.get(owners[0], UUID(accepted["job_id"]))["status"] == "PENDING"
    queue.ensure = lambda value: "existing"
    assert svc.dispatch(owners[0], UUID(accepted["job_id"]))["status"] == "QUEUED"
    assert rows(app, ResearchRun)[0].execution_id == execution


def test_crashed_run_and_queue_wait_are_explicitly_reconciled_without_restart(api):
    _, app, owners = api
    identifier = ready(api)
    svc, accepted, execution = create(app, owners[0], payload(identifier))
    svc.claim(execution)
    later = research(app, clock=lambda: utcnow() + timedelta(seconds=181))
    assert later.reconcile(owners[0])[0]["status"] == "FAILED"
    assert execute(svc, execution) == {"status": "ignored"}
    svc, other, _ = create(app, owners[0], payload(identifier, key="waiting"))
    later = research(app, clock=lambda: utcnow() + timedelta(seconds=1801))
    later.reconcile(owners[0])
    assert svc.get(owners[0], UUID(other["job_id"]))["error"]["code"] == "queue_wait_timeout"


def test_commit_ack_loss_does_not_overwrite_durable_success(api, monkeypatch):
    _, app, owners = api
    identifier = ready(api)
    svc, accepted, execution = create(app, owners[0], payload(identifier))
    original = svc.finish

    def lost(*args):
        original(*args)
        raise DatabaseUnavailable()

    monkeypatch.setattr(svc, "finish", lost)
    with pytest.raises(DatabaseUnavailable):
        execute(svc, execution)
    assert svc.get(owners[0], UUID(accepted["job_id"]))["status"] == "SUCCEEDED"
    assert execute(svc, execution) == {"status": "ignored"}


def test_checkpoint_database_failure_is_not_disguised_as_model_failure(api, monkeypatch):
    _, app, owners = api
    identifier = ready(api)
    svc, _, execution = create(app, owners[0], payload(identifier))

    def failed(*args, **kwargs):
        raise DatabaseUnavailable()

    monkeypatch.setattr(svc, "checkpoint", failed)
    with pytest.raises(DatabaseUnavailable):
        execute(svc, execution)
    assert rows(app, ResearchRun)[0].result is None
    assert [j.status for j in rows(app, Job) if j.operation == "research"] == ["RUNNING"]


def test_http_contract_auth_validation_ownership_and_safe_unexpected_error(api, monkeypatch):
    client, app, owners = api
    identifier = ready(api)
    app.state.research_queue = Queue()

    def post(value, key=KEY_A):
        return client.post("/research", headers={"X-API-Key": key}, json=value)

    response = post(payload(identifier))
    assert response.status_code == 202 and response.json()["status"] == "QUEUED"
    path = "/research/" + response.json()["job_id"]
    assert client.get(path, headers={"X-API-Key": KEY_A}).status_code == 200
    assert client.get(path, headers={"X-API-Key": KEY_B}).status_code == 404
    assert post(payload(identifier), KEY_B).status_code == 404
    assert post(payload(identifier, question="changed reporting question")).status_code == 409
    for value in (
        {**payload(identifier), "owner": str(owners[0])},
        {**payload(identifier), "publication_year_from": True},
        {**payload(identifier), "document_ids": []},
    ):
        assert post(value).status_code == 422
    headers = {"X-API-Key": KEY_A, "content-type": "application/json"}
    for raw in ('{"question":"secret-marker","question":"duplicate"}', '{"question":NaN}', "{"):
        invalid = client.post("/research", headers=headers, content=raw)
        assert invalid.status_code == 422 and "secret-marker" not in invalid.text
    assert client.post("/research", headers=headers, content=b"x" * 16385).status_code == 413
    monkeypatch.setattr(
        "app.api.research.service", lambda r: (_ for _ in ()).throw(RuntimeError("private-marker"))
    )
    failed = client.get(path, headers={"X-API-Key": KEY_A})
    assert failed.status_code == 500 and "private-marker" not in failed.text


def test_real_migration_roundtrip_preserves_existing_data_and_matches_metadata(api):
    _, app, owners = api
    identifier = ready(api)
    migration(app.state.database, "downgrade", "0004")
    assert "research_runs" not in inspect(app.state.database.engine).get_table_names()
    migration(app.state.database, "upgrade", "head")
    with app.state.database.sessions() as session:
        assert session.get(Document, identifier).status == "READY"
    assert "research_runs" in inspect(app.state.database.engine).get_table_names()
