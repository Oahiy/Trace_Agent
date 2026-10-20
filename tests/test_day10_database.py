"""Real PostgreSQL, simulated inference: atomic publication, ownership and failure recovery."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from test_database import database as database
from test_database import migration
from test_database import owners as owners
from test_database import postgres as postgres
from test_day4_database import KEY_A, post
from test_day4_database import api as api
from test_day5_database import service as parse_service
from test_day10 import vector

from app.core.config import Settings
from app.db.embeddings import EmbeddingRepository
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Document,
    EmbeddingProfile,
    IndexTask,
    Job,
    ParseTask,
    User,
)
from app.db.session import DatabaseUnavailable
from app.llm.contracts import EMBED, AttemptRecord, EmbeddingResult, ModelFailure
from app.llm.ollama import OllamaAdapter
from app.retrieval.contracts import SPEC, IndexFailure
from app.services.documents import DataIntegrityError, ResourceNotFound
from app.services.indexing import IndexingService, utcnow
from app.services.ingestion import IngestConflict
from app.services.retrieval import RetrievalService

pytestmark = [pytest.mark.database, pytest.mark.usefixtures("postgres")]


class FakeEmbedder:
    def __init__(self, *, fail_on=None, output=None, hook=None):
        self.calls, self.fail_on, self.output, self.hook = 0, fail_on, output, hook

    async def embed_documents(self, texts, *, budget):
        self.calls += 1
        with budget.operation():
            reserved = budget.reserve("embedding", 100, 0)
            if self.hook:
                self.hook()
            if self.calls == self.fail_on:
                raise ModelFailure("model_timeout")
            charged = budget.settle("embedding", reserved, 100, 0, 10, None)
            record = AttemptRecord(
                1,
                1,
                "embedding",
                EMBED.name,
                EMBED.digest,
                100,
                "synthetic-test",
                10,
                None,
                charged,
                0.01,
                "succeeded",
                None,
                200,
            )
            budget.records.append(record)
        return self.output or EmbeddingResult(tuple(vector() for _ in texts), EMBED, record)

    async def embed_query(self, text, *, budget):
        return await self.embed_documents([text], budget=budget)


def service(app, model=None, **kwargs):
    return IndexingService(
        app.state.database.sessions, app.state.files, model or FakeEmbedder(), **kwargs
    )


def upload(api, *, data=b"traceable evidence " * 150, year=None):
    client, app, owners = api
    fields = None
    if year is not None:
        from scripts.day4 import metadata

        fields = {**metadata(), "publication_year": str(year)}
    identifier = UUID(post(client, data=data, fields=fields).json()["document_id"])
    assert parse_service(app).parse(owners[0], identifier).status == "PARSED"
    return identifier


def rows(app, model):
    with app.state.database.sessions() as session:
        return list(session.scalars(select(model)))


def run(awaitable):
    return asyncio.run(awaitable)


def test_real_vector_codec_three_dimensional_distances_and_768_roundtrip(api):
    _, app, owners = api
    with app.state.database.sessions.begin() as session:
        result = (
            session.execute(
                text(
                    "SELECT v::vector <=> CAST(:q AS vector) FROM "
                    "(VALUES ('[1,0,0]'),('[0,1,0]'),('[-1,0,0]')) AS x(v) ORDER BY 1"
                ),
                {"q": "[1,0,0]"},
            )
            .scalars()
            .all()
        )
        assert result == [0, 1, 2]
    identifier = upload(api)
    assert run(service(app).index(owners[0], identifier)).status == "READY"
    assert list(rows(app, ChunkEmbedding)[0].embedding) == list(vector())


def test_complete_index_atomically_sets_ready_and_parent_success_and_replays(api):
    client, app, owners = api
    identifier = upload(api)
    model = FakeEmbedder()
    index = service(app, model)
    before = [c.id for c in rows(app, Chunk)]
    first = run(index.index(owners[0], identifier))
    assert first.status == "READY" and not first.reused and first.chunks == len(before)
    task, job, parse = rows(app, IndexTask)[0], rows(app, Job)[0], rows(app, ParseTask)[0]
    assert task.status == job.status == parse.status == "SUCCEEDED" and job.attempt_count == 1
    assert task.budget["embedding_calls"] == model.calls == 1
    assert len(task.budget["records"]) == 1
    response = client.get(f"/documents/{identifier}/status", headers={"X-API-Key": KEY_A})
    assert response.json() == {
        "document_id": str(identifier),
        "status": "READY",
        "index_version": SPEC.version,
        "error": None,
    }
    assert run(index.index(owners[0], identifier)).reused and model.calls == 1
    assert [c.id for c in rows(app, Chunk)] == before
    assert len(rows(app, ChunkEmbedding)) == len(before)
    assert parse_service(app).parse(owners[0], identifier).status == "READY"


def test_second_batch_failure_keeps_chunks_without_partial_vectors_and_releases_quota(api):
    client, app, owners = api
    identifier = upload(api, data=b"evidence " * 1300)
    assert len(rows(app, Chunk)) > 8
    before = [c.id for c in rows(app, Chunk)]
    outcome = run(service(app, FakeEmbedder(fail_on=2)).index(owners[0], identifier))
    assert outcome.status == "FAILED" and outcome.error == "index_timeout"
    assert rows(app, ChunkEmbedding) == [] and [c.id for c in rows(app, Chunk)] == before
    assert rows(app, Job)[0].status == "FAILED" and rows(app, ParseTask)[0].status == "SUCCEEDED"
    assert rows(app, IndexTask)[0].budget["embedding_calls"] == 2
    # Regression: FAILED + retained chunks is legitimate only for a recorded index failure.
    replay = parse_service(app).parse(owners[0], identifier)
    assert replay.reused and replay.error == "index_timeout" and replay.chunks == len(before)
    assert run(service(app).index(owners[0], identifier)).reused
    assert (
        client.get(f"/documents/{identifier}/status", headers={"X-API-Key": KEY_A}).json()["error"]
        == "index_timeout"
    )


def test_failed_with_chunks_without_failed_index_record_still_rejected(api):
    _, app, owners = api
    identifier = upload(api)
    with app.state.database.sessions.begin() as session:
        session.get(Document, identifier).status = "FAILED"
    with pytest.raises(DataIntegrityError):
        parse_service(app).parse(owners[0], identifier)


@pytest.mark.parametrize("mode", ["dimension", "zero", "profile", "count", "norm"])
def test_bad_embedding_never_publishes_index(api, mode):
    _, app, owners = api
    identifier = upload(api, data=b"small source")
    output = EmbeddingResult((vector(),), EMBED, None)
    if mode == "dimension":
        output = replace(output, vectors=((1.0,),))
    elif mode == "zero":
        output = replace(output, vectors=((0.0,) * 768,))
    elif mode == "profile":
        output = replace(output, profile=replace(EMBED, digest="f" * 64))
    elif mode == "count":
        output = replace(output, vectors=())
    else:
        output = replace(output, vectors=(vector(2),))
    outcome = run(service(app, FakeEmbedder(output=output)).index(owners[0], identifier))
    assert outcome.error == "index_invalid_embedding" and not rows(app, ChunkEmbedding)


def test_index_and_search_reject_other_owner_or_missing_before_inference(api):
    _, app, owners = api
    identifier = upload(api)
    model = FakeEmbedder()
    for requested in (identifier, uuid4()):
        with pytest.raises(ResourceNotFound):
            run(service(app, model).index(owners[1], requested))
        with pytest.raises(ResourceNotFound):
            run(
                RetrievalService(app.state.database.sessions, model).search(
                    owners[1], {"question": "q", "document_ids": [str(requested)]}
                )
            )
    assert model.calls == 0


def test_exact_retrieval_preserves_conditions_locators_and_tie_order(api):
    _, app, owners = api
    identifier = upload(api, year=2021)
    run(service(app).index(owners[0], identifier))
    model = FakeEmbedder()
    retrieval = RetrievalService(app.state.database.sessions, model)
    result = run(
        retrieval.search(owners[0], {"question": " evidence? ", "document_ids": [str(identifier)]})
    )
    assert result["question"] == "evidence?" and result["conditions"]["document_ids"] == [
        str(identifier)
    ]
    assert len(result["hits"]) == len(rows(app, Chunk)) < 5
    assert all(h["distance"] == 0 for h in result["hits"])
    assert [h["chunk_id"] for h in result["hits"]] == [
        str(c.id) for c in sorted(rows(app, Chunk), key=lambda c: c.ordinal)
    ]
    for hit in result["hits"]:
        assert hit["publication_year"] == 2021 and hit["pdf_page"] is None
        assert hit["char_end"] > hit["char_start"] and hit["license"] and hit["source_url"]
    assert model.calls == 1
    empty = run(
        retrieval.search(
            owners[0], {"question": "q", "publication_year_from": 2020, "publication_year_to": 2020}
        )
    )
    assert empty["hits"] == [] and model.calls == 1
    assert empty["excluded_documents"][0]["reason"] == "outside_publication_year"


def test_unknown_year_and_parsed_documents_are_excluded_without_model_call(api):
    _, app, owners = api
    identifier = upload(api)
    model = FakeEmbedder()
    retrieval = RetrievalService(app.state.database.sessions, model)
    assert (
        run(retrieval.search(owners[0], {"question": "q"}))["excluded_documents"][0]["reason"]
        == "not_ready"
    )
    run(service(app).index(owners[0], identifier))
    result = run(retrieval.search(owners[0], {"question": "q", "publication_year_from": 2000}))
    assert (
        result["excluded_documents"][0]["reason"] == "unknown_publication_year" and model.calls == 0
    )


def test_repository_enforces_owner_even_without_service_precheck(api):
    _, app, owners = api
    identifier = upload(api)
    run(service(app).index(owners[0], identifier))
    from app.retrieval.contracts import RetrievalRequest

    with app.state.database.sessions() as session:
        repository = EmbeddingRepository(session)
        assert repository.stored(owners[1], identifier) == []
        assert (
            repository.search(owners[1], vector(), [identifier], RetrievalRequest(question="q"))
            == []
        )


def test_model_wait_holds_no_connection_and_reservation_is_already_durable(api):
    _, app, owners = api
    identifier = upload(api)

    def hook():
        assert app.state.database.engine.pool.checkedout() == 0
        task = rows(app, IndexTask)[0]
        assert task.budget["embedding_calls"] == 1
        assert task.budget["embedding_tokens_charged"] == 100
        assert task.budget["records"] == []

    assert run(service(app, FakeEmbedder(hook=hook)).index(owners[0], identifier)).status == "READY"


def test_concurrent_index_claim_is_rejected_before_duplicate_inference(api):
    _, app, owners = api
    identifier = upload(api)
    first, second = service(app), service(app)
    first._claim(owners[0], identifier)
    with pytest.raises(IngestConflict):
        run(second.index(owners[0], identifier))
    assert rows(app, Job)[0].attempt_count == 1


def test_expired_execution_is_reconciled_to_failure_without_restarting_budget(api):
    _, app, owners = api
    identifier = upload(api)
    index = service(app)
    index._claim(owners[0], identifier)
    with app.state.database.sessions.begin() as session:
        task = session.scalar(select(IndexTask))
        task.started_at = utcnow() - timedelta(seconds=400)
        task.deadline_at = utcnow() - timedelta(seconds=1)
        task.budget = {**task.budget, "embedding_calls": 3}
    outcome = run(index.index(owners[0], identifier))
    assert outcome.error == "index_timeout" and outcome.reused
    assert rows(app, IndexTask)[0].budget["embedding_calls"] == 3
    assert rows(app, Job)[0].attempt_count == 1 and not rows(app, ChunkEmbedding)


def test_late_chunk_change_or_execution_token_cannot_publish(api):
    _, app, owners = api
    identifier = upload(api)

    def change():
        with app.state.database.sessions.begin() as session:
            session.scalar(select(Chunk)).content = "changed evidence"

    with pytest.raises(IngestConflict):
        run(service(app, FakeEmbedder(hook=change)).index(owners[0], identifier))
    assert not rows(app, ChunkEmbedding) and rows(app, Document)[0].status == "PARSED"


def test_second_vector_insert_failure_rolls_back_all_vectors_and_ready(api, monkeypatch):
    _, app, owners = api
    identifier = upload(api)
    original, calls = EmbeddingRepository.add, []

    def fail(self, chunk, vector):
        calls.append(chunk)
        if len(calls) == 2:
            raise OperationalError("SQL", None, RuntimeError("private detail"))
        original(self, chunk, vector)

    monkeypatch.setattr(EmbeddingRepository, "add", fail)
    with pytest.raises(DatabaseUnavailable, match="^database_unavailable$"):
        run(service(app).index(owners[0], identifier))
    assert len(calls) == 2 and not rows(app, ChunkEmbedding)
    assert rows(app, Document)[0].status == "PARSED" and rows(app, IndexTask)[0].status == "RUNNING"


def test_lost_final_commit_ack_replays_committed_index_without_inference(api):
    _, app, owners = api
    identifier = upload(api)
    original = app.state.database.sessions

    class LostAck:
        def __call__(self):
            return original()

        @contextmanager
        def begin(self):
            published = False
            with original.begin() as session:
                yield session
                published = any(
                    isinstance(x, Document) and x.status == "READY" for x in session.dirty
                )
            if published:
                raise OperationalError("COMMIT", None, RuntimeError("private detail"))

    with pytest.raises(DatabaseUnavailable):
        run(
            IndexingService(LostAck(), app.state.files, FakeEmbedder()).index(owners[0], identifier)
        )
    assert rows(app, Document)[0].status == "READY"
    model = FakeEmbedder()
    assert run(service(app, model).index(owners[0], identifier)).reused and model.calls == 0


def test_missing_vector_or_modified_profile_is_detected_before_query_inference(api):
    _, app, owners = api
    identifier = upload(api)
    run(service(app).index(owners[0], identifier))
    with app.state.database.sessions.begin() as session:
        session.delete(session.scalar(select(ChunkEmbedding)))
    model = FakeEmbedder()
    with pytest.raises(DataIntegrityError):
        run(
            RetrievalService(app.state.database.sessions, model).search(
                owners[0], {"question": "q"}
            )
        )
    assert model.calls == 0


def test_scope_changed_during_query_embedding_is_not_returned(api):
    _, app, owners = api
    identifier = upload(api)
    run(service(app).index(owners[0], identifier))

    def change():
        with app.state.database.sessions.begin() as session:
            session.get(Document, identifier).publication_year = 2025

    with pytest.raises(IngestConflict):
        run(
            RetrievalService(app.state.database.sessions, FakeEmbedder(hook=change)).search(
                owners[0], {"question": "q"}
            )
        )


def test_cancelled_index_preserves_reserved_usage_and_never_becomes_ready(api):
    _, app, owners = api
    identifier = upload(api)

    class Cancelled(FakeEmbedder):
        async def embed_documents(self, texts, *, budget):
            budget.reserve("embedding", 99, 0)
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        run(service(app, Cancelled()).index(owners[0], identifier))
    task = rows(app, IndexTask)[0]
    assert task.status == "FAILED" and task.error_code == "index_cancelled"
    assert task.budget["embedding_calls"] == 1 and not rows(app, ChunkEmbedding)


def test_real_adapter_retry_is_counted_in_durable_budget(api):
    _, app, owners = api
    identifier = upload(api, data=b"small source")
    attempts = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"models": [{"name": EMBED.name, "digest": EMBED.digest}]}
            )
        attempts.append(1)
        assert rows(app, IndexTask)[0].budget["embedding_calls"] == len(attempts)
        if len(attempts) == 1:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"model": EMBED.name, "embeddings": [list(vector())], "prompt_eval_count": 4}
        )

    async def execute():
        async with OllamaAdapter(Settings(), transport=httpx.MockTransport(respond)) as adapter:
            return await service(app, adapter).index(owners[0], identifier)

    assert run(execute()).status == "READY"
    budget = rows(app, IndexTask)[0].budget
    assert budget["embedding_calls"] == 2 and len(budget["records"]) == 2
    assert [r["status"] for r in budget["records"]] == ["failed", "succeeded"]


def test_day10_migration_preserves_existing_rows_through_roundtrip(database, owners):
    from app.services.documents import DocumentService
    from scripts.day3 import synthetic_metadata

    registration = DocumentService(database.sessions).register(owners[0], synthetic_metadata())
    migration(database, "downgrade", "0003")
    with database.sessions() as session:
        assert session.get(Document, registration.document_id).status == "UPLOADED"
    migration(database, "upgrade", "head")
    with database.sessions() as session:
        assert session.get(Document, registration.document_id).status == "UPLOADED"
        assert session.scalar(select(func.count()).select_from(ChunkEmbedding)) == 0


def test_concurrent_async_index_runs_inference_only_once(api):
    _, app, owners = api
    identifier = upload(api)

    async def execute():
        entered, release = asyncio.Event(), asyncio.Event()

        class Blocked(FakeEmbedder):
            async def embed_documents(self, texts, *, budget):
                entered.set()
                await release.wait()
                return await super().embed_documents(texts, budget=budget)

        model = Blocked()
        first = asyncio.create_task(service(app, model).index(owners[0], identifier))
        await entered.wait()
        with pytest.raises(IngestConflict):
            await service(app, model).index(owners[0], identifier)
        release.set()
        assert (await first).status == "READY" and model.calls == 1

    run(execute())


def test_actual_attempt_timeout_records_failure_and_retains_chunks(api):
    _, app, owners = api
    identifier = upload(api)

    class Slow(FakeEmbedder):
        async def embed_documents(self, texts, *, budget):
            budget.reserve("embedding", 100, 0)
            await asyncio.sleep(1)

    result = run(service(app, Slow(), timeout=0.03).index(owners[0], identifier))
    assert result.status == "FAILED" and result.error == "index_timeout"
    assert rows(app, IndexTask)[0].budget["embedding_calls"] == 1
    assert rows(app, Chunk) and not rows(app, ChunkEmbedding)


def test_expiry_at_request_reservation_prevents_post_and_persists_failure(api, monkeypatch):
    _, app, owners = api
    identifier = upload(api)
    posts = []

    def respond(request):
        if request.method == "POST":
            posts.append(1)
            pytest.fail("inference_after_deadline")
        return httpx.Response(200, json={"models": [{"name": EMBED.name, "digest": EMBED.digest}]})

    async def execute():
        async with OllamaAdapter(Settings(), transport=httpx.MockTransport(respond)) as model:
            index = service(app, model)
            original = index._claim

            def claim(*args):
                value = original(*args)
                index.clock = lambda: utcnow() + timedelta(seconds=400)
                return value

            monkeypatch.setattr(index, "_claim", claim)
            return await index.index(owners[0], identifier)

    result = run(execute())
    assert result.error == "index_timeout" and not posts
    assert rows(app, IndexTask)[0].status == "FAILED"


def test_checkpoint_database_failure_is_not_swallowed_or_misreported(api, monkeypatch):
    _, app, owners = api
    identifier = upload(api)
    posts = []

    def respond(request):
        if request.method == "POST":
            posts.append(1)
        return httpx.Response(200, json={"models": [{"name": EMBED.name, "digest": EMBED.digest}]})

    async def execute():
        async with OllamaAdapter(Settings(), transport=httpx.MockTransport(respond)) as model:
            index = service(app, model)

            def failed(*args):
                raise DatabaseUnavailable()

            monkeypatch.setattr(index, "_checkpoint", failed)
            return await index.index(owners[0], identifier)

    with pytest.raises(DatabaseUnavailable):
        run(execute())
    assert posts == [] and rows(app, IndexTask)[0].status == "RUNNING"
    assert rows(app, Document)[0].status == "PARSED"


def test_large_document_rejected_before_claim_instead_of_resetting_budget(api):
    _, app, owners = api
    identifier = upload(api, data=b"x" * 117000)
    assert len(rows(app, Chunk)) > 128
    model = FakeEmbedder()
    with pytest.raises(IndexFailure, match="^index_budget_exhausted$"):
        run(service(app, model).index(owners[0], identifier))
    assert model.calls == 0 and not rows(app, IndexTask)
    assert rows(app, Job)[0].status == "PENDING" and rows(app, Document)[0].status == "PARSED"


def test_profile_tampering_and_content_drift_are_detected(api):
    _, app, owners = api
    identifier = upload(api)
    run(service(app).index(owners[0], identifier))
    with app.state.database.sessions.begin() as session:
        session.get(EmbeddingProfile, SPEC.version).model_digest = "f" * 64
    try:
        with pytest.raises(DataIntegrityError):
            run(service(app).index(owners[0], identifier))
    finally:
        with app.state.database.sessions.begin() as session:
            session.get(EmbeddingProfile, SPEC.version).model_digest = SPEC.model_digest
    with app.state.database.sessions.begin() as session:
        session.scalar(select(Chunk)).content = "drifted text"
    with pytest.raises(DataIntegrityError):
        run(
            RetrievalService(app.state.database.sessions, FakeEmbedder()).search(
                owners[0], {"question": "q"}
            )
        )


def test_exact_ranking_uses_distance_ascending_not_similarity_ascending(api):
    _, app, owners = api
    identifier = upload(api)
    run(service(app).index(owners[0], identifier))
    ordered = sorted(rows(app, Chunk), key=lambda c: c.ordinal)
    with app.state.database.sessions.begin() as session:
        for position, chunk in enumerate(ordered):
            row = session.get(ChunkEmbedding, (chunk.id, SPEC.version))
            row.embedding = list(
                vector(-1)
                if position == 0
                else vector()
                if position == 1
                else (0.0, 1.0) + (0.0,) * 766
            )
    result = run(
        RetrievalService(app.state.database.sessions, FakeEmbedder()).search(
            owners[0], {"question": "q"}
        )
    )
    assert result["hits"][0]["chunk_id"] == str(ordered[1].id)
    assert [h["distance"] for h in result["hits"]] == [0, 1, 1, 2]


def test_disabled_owner_is_rejected_before_index_and_after_query_wait(api):
    _, app, owners = api
    identifier = upload(api)
    run(service(app).index(owners[0], identifier))

    def disable():
        with app.state.database.sessions.begin() as session:
            session.get(User, owners[0]).active = False

    with pytest.raises(ResourceNotFound):
        run(
            RetrievalService(app.state.database.sessions, FakeEmbedder(hook=disable)).search(
                owners[0], {"question": "q"}
            )
        )
    with pytest.raises(ResourceNotFound):
        run(service(app).index(owners[0], identifier))


def test_file_loss_after_ready_is_not_reported_as_successful_replay(api):
    _, app, owners = api
    from app.storage.files import StorageUnavailable

    identifier = upload(api)
    run(service(app).index(owners[0], identifier))
    app.state.files.path(rows(app, Document)[0].storage_key).unlink()
    with pytest.raises(StorageUnavailable):
        run(service(app).index(owners[0], identifier))
    assert rows(app, Document)[0].status == "READY"


def test_no_file_at_index_time_records_specific_safe_failure(api):
    _, app, owners = api
    identifier = upload(api)
    app.state.files.path(rows(app, Document)[0].storage_key).unlink()
    outcome = run(service(app).index(owners[0], identifier))
    assert outcome.error == "index_storage_unavailable" and not rows(app, ChunkEmbedding)


def test_database_rejects_zero_and_duplicate_vectors_without_service(api):
    _, app, owners = api
    from sqlalchemy.exc import IntegrityError

    identifier = upload(api)
    run(service(app).index(owners[0], identifier))
    with pytest.raises(IntegrityError):
        with app.state.database.sessions.begin() as session:
            session.scalar(select(ChunkEmbedding)).embedding = [0.0] * 768
    existing = rows(app, ChunkEmbedding)[0]
    with pytest.raises(IntegrityError):
        with app.state.database.sessions.begin() as session:
            session.add(
                ChunkEmbedding(
                    chunk_id=existing.chunk_id,
                    index_version=existing.index_version,
                    content_sha256=existing.content_sha256,
                    embedding=list(vector()),
                )
            )


def test_expired_task_does_not_overwrite_inconsistent_ready_state(api):
    _, app, owners = api
    identifier = upload(api)
    index = service(app)
    index._claim(owners[0], identifier)
    with app.state.database.sessions.begin() as session:
        task = session.scalar(select(IndexTask))
        task.started_at = utcnow() - timedelta(seconds=400)
        task.deadline_at = utcnow() - timedelta(seconds=1)
        document = session.get(Document, identifier)
        document.status, document.index_version = "READY", SPEC.version
        session.scalar(select(Job)).status = "SUCCEEDED"
    with pytest.raises(DataIntegrityError):
        run(index.index(owners[0], identifier))
    assert rows(app, Document)[0].status == "READY"


def test_retrieval_snapshot_is_repeatable_and_tracks_evidence_identity(api):
    _, app, owners = api
    identifier = upload(api)
    run(service(app).index(owners[0], identifier))
    retrieval = RetrievalService(app.state.database.sessions, FakeEmbedder())
    first = run(retrieval.search(owners[0], {"question": "q", "limit": 1}))
    repeated = run(retrieval.search(owners[0], {"question": "q", "limit": 1}))
    assert first["data_snapshot"] == repeated["data_snapshot"]
    assert first["conditions"]["limit"] == 1 and len(first["hits"]) == 1
    assert first["hits"][0]["source_sha256"] == rows(app, Document)[0].file_sha256
    with app.state.database.sessions.begin() as session:
        session.get(Document, identifier).title = "changed source title"
    changed = run(retrieval.search(owners[0], {"question": "q"}))
    assert first["data_snapshot"] != changed["data_snapshot"]


def test_migrate_revision_uses_migration_role_without_granting_runtime_metadata_access(
    postgres, monkeypatch, capsys
):
    import json
    import os

    from sqlalchemy.exc import ProgrammingError

    from app.db.config import DatabaseSettings
    from app.db.session import open_database
    from scripts import day3

    runtime_url = os.environ["TRACEAGENT_DB_URL"]
    monkeypatch.setenv("TRACEAGENT_DB_URL", runtime_url)
    monkeypatch.setattr(
        day3,
        "environment",
        lambda: {
            "TRACEAGENT_DB_URL": runtime_url,
            "TRACEAGENT_MIGRATION_URL": os.environ["TRACEAGENT_TEST_DB_URL"],
        },
    )
    monkeypatch.setattr(day3, "migrate", lambda env: None)
    assert day3.main(["migrate"]) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == "0005"
    with open_database(DatabaseSettings(url=runtime_url)) as database:
        with pytest.raises(ProgrammingError):
            with database.sessions() as session:
                session.scalar(text("SELECT version_num FROM alembic_version"))
