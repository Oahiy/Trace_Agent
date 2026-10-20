"""Real PostgreSQL checks; never replace these with SQLite or an in-memory repository."""

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, event, func, inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError, SQLAlchemyError

from app.db.config import DatabaseSettings
from app.db.models import Chunk, Document, Job, User
from app.db.repositories import DocumentRepository, JobRepository
from app.db.session import DatabaseUnavailable, open_database
from app.services.documents import (
    DataIntegrityError,
    DocumentService,
    ResourceNotFound,
    SourceMetadataConflict,
)
from scripts.day3 import synthetic_metadata

pytestmark = [
    pytest.mark.database,
    pytest.mark.skipif(
        os.environ.get("TRACEAGENT_DATABASE_TESTS") != "1",
        reason="Real PostgreSQL checks require TRACEAGENT_DATABASE_TESTS=1",
    ),
]
ROOT = Path(__file__).resolve().parents[1]


def migration(database, direction, revision):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TRACEAGENT_DB_URL", database.engine.url.render_as_string(hide_password=False))
        getattr(command, direction)(Config(str(ROOT / "alembic.ini")), revision)


@pytest.fixture(scope="session")
def postgres():
    if os.environ.get("TRACEAGENT_DATABASE_TESTS") != "1":
        pytest.skip("Real PostgreSQL checks require TRACEAGENT_DATABASE_TESTS=1")
    value = os.environ.get("TRACEAGENT_TEST_DB_URL")
    if not value:
        pytest.fail("isolated_database_url_required", pytrace=False)
    settings = DatabaseSettings(url=value)
    with open_database(settings) as database:
        # These tests delete their fixture rows and downgrade tables. Guard the target.
        if database.engine.url.database != "traceagent_test":
            pytest.fail("isolated_database_name_required", pytrace=False)
        migration(database, "upgrade", "head")
        yield database


@pytest.fixture
def database(postgres):
    with postgres.sessions.begin() as session:
        for model in (Job, Chunk, Document, User):
            session.execute(delete(model))
    yield postgres


@pytest.fixture
def owners(database):
    ids = (uuid4(), uuid4())
    with database.sessions.begin() as session:
        session.add_all([User(id=identifier) for identifier in ids])
    return ids


def count(database, model):
    with database.sessions() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_committed_document_and_job_visible_in_fresh_session(database, owners):
    service = DocumentService(database.sessions)
    result = service.register(owners[0], synthetic_metadata())
    assert result.document_status == "UPLOADED" and result.job_status == "PENDING"
    assert not result.reused
    with database.sessions() as session:
        document = session.get(Document, result.document_id)
        job = session.get(Job, result.job_id)
        assert document.owner_id == job.owner_id == owners[0]
        assert job.document_id == document.id
        assert document.publication_year is None
        assert document.created_at.tzinfo is not None


def test_owner_isolation_and_unknown_document(database, owners):
    service = DocumentService(database.sessions)
    result = service.register(owners[0], synthetic_metadata())
    assert service.get(owners[0], result.document_id).metadata.source_id == "synthetic-day3"
    assert service.get(owners[1], result.document_id) is None
    assert service.get(owners[0], uuid4()) is None


def test_repeated_input_reuses_and_conflict_preserves_original(database, owners):
    service = DocumentService(database.sessions)
    request = synthetic_metadata()
    first = service.register(owners[0], request)
    repeated = service.register(owners[0], request)
    assert repeated.reused and repeated.document_id == first.document_id
    assert repeated.job_id == first.job_id
    with pytest.raises(SourceMetadataConflict, match="^source_metadata_conflict$"):
        service.register(owners[0], request.model_copy(update={"publication_year": 2021}))
    assert service.get(owners[0], first.document_id).metadata.publication_year is None
    assert count(database, Document) == count(database, Job) == 1


def test_same_hash_other_owner_or_ingest_version_is_independent(database, owners):
    service = DocumentService(database.sessions)
    first = service.register(owners[0], synthetic_metadata())
    second = service.register(owners[1], synthetic_metadata())
    third = service.register(owners[0], synthetic_metadata(ingest_version="day3-v2"))
    assert len({first.document_id, second.document_id, third.document_id}) == 3
    assert count(database, Document) == count(database, Job) == 3


@pytest.mark.parametrize("disabled", [False, True])
def test_unknown_or_disabled_owner_has_no_writes(database, disabled):
    owner = uuid4()
    if disabled:
        with database.sessions.begin() as session:
            session.add(User(id=owner, active=False))
    with pytest.raises(ResourceNotFound):
        DocumentService(database.sessions).register(owner, synthetic_metadata())
    assert count(database, Document) == count(database, Job) == 0


def test_failed_job_write_rolls_back_flushed_document(database, owners, monkeypatch):
    def invalid_add(self, job):
        # The document was already flushed; a real database CHECK fails next.
        job.status = "INVALID"
        self.session.add(job)
        self.session.flush()

    with monkeypatch.context() as patch:
        patch.setattr(JobRepository, "add", invalid_add)
        with pytest.raises(DataIntegrityError, match="^data_integrity_error$"):
            DocumentService(database.sessions).register(owners[0], synthetic_metadata())
    assert count(database, Document) == count(database, Job) == 0
    result = DocumentService(database.sessions).register(owners[0], synthetic_metadata())
    assert result.document_status == "UPLOADED"


def test_database_unique_constraint_without_service_precheck(database, owners):
    first = DocumentService(database.sessions).register(owners[0], synthetic_metadata())
    with pytest.raises(IntegrityError):
        with database.sessions.begin() as session:
            session.add(
                Document(id=uuid4(), owner_id=owners[0], **synthetic_metadata().model_dump())
            )
    assert count(database, Document) == 1
    assert DocumentService(database.sessions).get(owners[0], first.document_id) is not None


def test_concurrent_identical_registration_reconciles_unique_race(database, owners, monkeypatch):
    barrier = threading.Barrier(2, timeout=5)
    original = DocumentRepository.add

    def simultaneous_add(self, document):
        barrier.wait()
        return original(self, document)

    monkeypatch.setattr(DocumentRepository, "add", simultaneous_add)
    service = DocumentService(database.sessions)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: service.register(owners[0], synthetic_metadata()), range(2))
        )
    assert results[0].document_id == results[1].document_id
    assert results[0].job_id == results[1].job_id
    assert sorted(result.reused for result in results) == [False, True]
    assert count(database, Document) == count(database, Job) == 1


def test_job_cannot_reference_another_owners_document(database, owners):
    result = DocumentService(database.sessions).register(owners[0], synthetic_metadata())
    # Remove the existing job so its uniqueness constraint cannot mask the FK.
    with database.sessions.begin() as session:
        session.execute(delete(Job).where(Job.id == result.job_id))
    with pytest.raises(IntegrityError) as error:
        with database.sessions.begin() as session:
            session.add(
                Job(
                    id=uuid4(),
                    owner_id=owners[1],
                    document_id=result.document_id,
                    operation="ingest",
                )
            )
    assert error.value.orig.diag.constraint_name == "fk_jobs_document_owner"
    assert count(database, Job) == 0


@pytest.mark.parametrize(
    "model,changes,constraint",
    [
        (Document, {"owner_id": uuid4()}, "fk_documents_owner"),
        (Document, {"file_sha256": "bad"}, "ck_documents_sha256"),
        (Document, {"publication_year": 999}, "ck_documents_year"),
        (Document, {"title": " "}, "ck_documents_title"),
        (Document, {"status": "INVALID"}, "ck_documents_status"),
        (Document, {"status": "READY", "index_version": None}, "ck_documents_ready_index"),
        (Job, {"status": "INVALID"}, "ck_jobs_status"),
        (Job, {"attempt_count": -1}, "ck_jobs_attempt_count"),
        (Job, {"attempt_count": 3}, "ck_jobs_attempt_count"),
        (Job, {"document_id": uuid4()}, "fk_jobs_document_owner"),
        (Job, {"idempotency_key": "same", "input_sha256": None}, "ck_jobs_idempotency_pair"),
    ],
)
def test_database_constraints_reject_invalid_rows(database, owners, model, changes, constraint):
    result = DocumentService(database.sessions).register(owners[0], synthetic_metadata())
    if model is Document:
        values = {
            **synthetic_metadata(
                file_sha256=hashlib.sha256(uuid4().bytes).hexdigest()
            ).model_dump(),
            "owner_id": owners[0],
        }
    else:
        with database.sessions.begin() as session:
            session.execute(delete(Job).where(Job.id == result.job_id))
        values = {"owner_id": owners[0], "document_id": result.document_id, "operation": "ingest"}
    with pytest.raises(IntegrityError) as error:
        with database.sessions.begin() as session:
            session.add(model(id=uuid4(), **{**values, **changes}))
            session.flush()
    assert error.value.orig.diag.constraint_name == constraint
    assert count(database, Document) == 1
    assert count(database, Job) == (1 if model is Document else 0)


def test_commit_failure_never_returns_success(database, owners):
    def fail_commit(connection):
        raise OperationalError(None, None, RuntimeError("leak-marker"))

    event.listen(database.engine, "commit", fail_commit)
    try:
        with pytest.raises(DatabaseUnavailable, match="^database_unavailable$"):
            DocumentService(database.sessions).register(owners[0], synthetic_metadata())
    finally:
        event.remove(database.engine, "commit", fail_commit)
    assert count(database, Document) == count(database, Job) == 0


def test_corrupt_stored_metadata_has_safe_error(database, owners):
    result = DocumentService(database.sessions).register(owners[0], synthetic_metadata())
    with database.sessions.begin() as session:
        session.get(Document, result.document_id).source_url = "file:///leak-marker"
    with pytest.raises(DataIntegrityError, match="^data_integrity_error$"):
        DocumentService(database.sessions).get(owners[0], result.document_id)


def test_research_idempotency_constraint_is_owner_scoped(database, owners):
    digest = "a" * 64
    with database.sessions.begin() as session:
        session.add_all(
            [
                Job(
                    owner_id=owner,
                    operation="research",
                    idempotency_key="same",
                    input_sha256=digest,
                )
                for owner in owners
            ]
        )
    with pytest.raises(IntegrityError):
        with database.sessions.begin() as session:
            session.add(
                Job(
                    owner_id=owners[0],
                    operation="research",
                    idempotency_key="same",
                    input_sha256="b" * 64,
                )
            )
    assert count(database, Job) == 2


@pytest.mark.parametrize(
    "locator",
    [
        {"pdf_page": 1},
        {"char_start": 0, "char_end": 10, "section": "intro"},
    ],
)
def test_chunk_valid_source_locator(database, owners, locator):
    result = DocumentService(database.sessions).register(owners[0], synthetic_metadata())
    with database.sessions.begin() as session:
        session.add(
            Chunk(
                document_id=result.document_id,
                ingest_version="day3-v1",
                ordinal=0,
                content="synthetic text",
                **locator,
            )
        )
    assert count(database, Chunk) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"pdf_page": 0},
        {"pdf_page": None},
        {"ordinal": -1},
        {"content": " "},
        {"pdf_page": 1, "char_start": 0, "char_end": 1},
        {"pdf_page": None, "char_start": 5, "char_end": 5},
        {"pdf_page": None, "char_start": 0, "char_end": None},
        {"document_id": uuid4()},
    ],
)
def test_chunk_invalid_locator_content_or_parent_rejected(database, owners, changes):
    result = DocumentService(database.sessions).register(owners[0], synthetic_metadata())
    values = {
        "document_id": result.document_id,
        "ingest_version": "day3-v1",
        "ordinal": 0,
        "content": "text",
        "pdf_page": 1,
    }
    with pytest.raises(IntegrityError):
        with database.sessions.begin() as session:
            session.add(Chunk(**{**values, **changes}))
    assert count(database, Chunk) == 0


def test_chunk_duplicate_ordinal_rejected(database, owners):
    result = DocumentService(database.sessions).register(owners[0], synthetic_metadata())
    with pytest.raises(IntegrityError):
        with database.sessions.begin() as session:
            for _ in range(2):
                session.add(
                    Chunk(
                        document_id=result.document_id,
                        ingest_version="day3-v1",
                        ordinal=0,
                        content="text",
                        pdf_page=1,
                    )
                )
    assert count(database, Chunk) == 0


def test_empty_database_migration_downgrade_and_upgrade(database):
    migration(database, "downgrade", "base")
    assert inspect(database.engine).get_table_names() == ["alembic_version"]
    migration(database, "upgrade", "head")
    inspector = inspect(database.engine)
    assert set(inspector.get_table_names()) == {
        "users",
        "documents",
        "chunks",
        "jobs",
        "parse_tasks",
        "embedding_profiles",
        "chunk_embeddings",
        "index_tasks",
        "research_runs",
        "alembic_version",
    }
    assert {constraint["name"] for constraint in inspector.get_unique_constraints("documents")} >= {
        "uq_documents_owner_file_ingest",
        "uq_documents_id_owner",
    }
    with database.sessions() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TRACEAGENT_DB_URL", database.engine.url.render_as_string(hide_password=False))
        command.check(Config(str(ROOT / "alembic.ini")))
    assert database.probe()["pgvector"] == "0.8.7"


def test_real_statement_timeout_and_session_recovery(database):
    with pytest.MonkeyPatch.context():
        settings = DatabaseSettings(
            url=database.engine.url.render_as_string(hide_password=False), statement_timeout_ms=100
        )
        with open_database(settings) as limited:
            started = time.monotonic()
            with pytest.raises(SQLAlchemyError):
                with limited.sessions.begin() as session:
                    session.execute(text("SELECT pg_sleep(1)"))
            assert time.monotonic() - started < 2
            with limited.sessions() as session:
                assert session.scalar(text("SELECT 1")) == 1


def test_engine_lifecycle_disposes_pool(database):
    settings = DatabaseSettings(url=database.engine.url.render_as_string(hide_password=False))
    with open_database(settings) as managed:
        pool = managed.engine.pool
        with managed.sessions() as session:
            assert session.scalar(text("SELECT 1")) == 1
        assert pool.checkedin() == 1
    assert pool.checkedin() == 0


def test_runtime_role_cannot_create_tables(postgres):
    url = os.environ.get("TRACEAGENT_DB_URL")
    if not url:
        pytest.fail("runtime_database_url_required", pytrace=False)
    with open_database(DatabaseSettings(url=url)) as runtime:
        with runtime.sessions() as session:
            flags = session.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user"
                )
            ).one()
            assert tuple(flags) == (False, False, False)
        with pytest.raises(ProgrammingError):
            with runtime.sessions.begin() as session:
                session.execute(text("CREATE TABLE public.runtime_must_not_create (id int)"))
        with pytest.raises(ProgrammingError):
            with runtime.sessions() as session:
                session.execute(text("SELECT * FROM public.alembic_version"))


def test_real_pool_wait_timeout_maps_to_safe_error(database):
    settings = DatabaseSettings(
        url=database.engine.url.render_as_string(hide_password=False), pool_timeout_seconds=0.1
    )
    with open_database(settings) as limited:
        with limited.engine.connect(), limited.engine.connect():
            started = time.monotonic()
            with pytest.raises(DatabaseUnavailable, match="^database_unavailable$"):
                DocumentService(limited.sessions).get(uuid4(), uuid4())
            assert 0.08 <= time.monotonic() - started < 1


def test_real_row_lock_timeout(database, owners):
    result = DocumentService(database.sessions).register(owners[0], synthetic_metadata())
    settings = DatabaseSettings(
        url=database.engine.url.render_as_string(hide_password=False),
        lock_timeout_ms=100,
        statement_timeout_ms=1000,
    )
    statement = text("UPDATE documents SET title = title WHERE id = :id")
    with database.sessions.begin() as holder:
        holder.execute(statement, {"id": result.document_id})
        with open_database(settings) as limited:
            started = time.monotonic()
            with pytest.raises(SQLAlchemyError) as error:
                with limited.sessions.begin() as blocked:
                    blocked.execute(statement, {"id": result.document_id})
            assert error.value.orig.sqlstate == "55P03"
            assert time.monotonic() - started < 2


def test_real_idle_transaction_timeout_and_recovery(database):
    settings = DatabaseSettings(
        url=database.engine.url.render_as_string(hide_password=False),
        idle_transaction_timeout_ms=1000,
    )
    with open_database(settings) as limited:
        with pytest.raises(SQLAlchemyError) as error:
            with limited.sessions.begin() as session:
                assert session.scalar(text("SELECT 1")) == 1
                time.sleep(1.2)
                session.execute(text("SELECT 1"))
        assert error.value.orig.sqlstate == "25P03"
        with limited.sessions() as session:
            assert session.scalar(text("SELECT 1")) == 1


def test_connection_refused_maps_to_safe_error():
    # Reserve a loopback port without listening so the failure target is known.
    import socket

    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
        with open_database(
            DatabaseSettings(
                url=f"postgresql+psycopg://dummy:leak-marker@127.0.0.1:{port}/traceagent_test"
            )
        ) as unavailable:
            started = time.monotonic()
            with pytest.raises(DatabaseUnavailable, match="^database_unavailable$"):
                DocumentService(unavailable.sessions).get(uuid4(), uuid4())
            assert time.monotonic() - started < 5
