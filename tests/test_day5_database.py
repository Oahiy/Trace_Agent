"""Real PostgreSQL atomicity, tenant isolation, replay and concurrent parse checks."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from test_database import database as database
from test_database import owners as owners
from test_database import postgres as postgres
from test_day4_database import KEY_A, KEY_B, post
from test_day4_database import api as api
from test_day5 import pdf_pages

from app.db.models import Chunk, Document, Job, User
from app.db.repositories import ChunkRepository
from app.db.session import DatabaseUnavailable
from app.ingestion.text import INGEST_VERSION, PageText, ParsedText, ParseRejected
from app.services.documents import DataIntegrityError, ResourceNotFound
from app.services.ingestion import IngestConflict, IngestionService
from app.storage.files import StorageUnavailable

pytestmark = [pytest.mark.database, pytest.mark.usefixtures("postgres")]


def service(app, **kwargs):
    return IngestionService(
        app.state.database.sessions, app.state.files, app.state.settings, **kwargs
    )


def rows(app, model):
    with app.state.database.sessions() as session:
        return list(session.scalars(select(model)))


def test_real_txt_parse_persists_locators_and_preserves_pending_job(api):
    client, app, owners = api
    text = "中文证据\r\n" + "traceable text " * 200
    accepted = post(client, data=b"\xef\xbb\xbf" + text.encode()).json()
    identifier = UUID(accepted["document_id"])
    outcome = service(app).parse(owners[0], identifier)
    assert outcome.status == "PARSED" and not outcome.reused and outcome.chunks > 1
    for chunk in service(app).chunks(owners[0], identifier):
        assert chunk["content"] == text[chunk["char_start"] : chunk["char_end"]]
        assert chunk["ingest_version"] == INGEST_VERSION
    response = client.get(f"/documents/{identifier}/status", headers={"X-API-Key": KEY_A})
    assert response.status_code == 200 and response.json() == {
        "document_id": str(identifier),
        "status": "PARSED",
        "index_version": None,
        "error": None,
    }
    assert rows(app, Job)[0].status == "PENDING"


def test_pdf_physical_pages_and_metadata_survive(api):
    client, app, owners = api
    accepted = post(
        client, data=pdf_pages("page one", "", "page three"), media="application/pdf"
    ).json()
    identifier = UUID(accepted["document_id"])
    outcome = service(app).parse(owners[0], identifier)
    assert outcome.chunks == 2
    assert [chunk["pdf_page"] for chunk in service(app).chunks(owners[0], identifier)] == [1, 3]
    document = rows(app, Document)[0]
    assert document.source_id == "synthetic-day4-txt" and document.publication_year is None
    assert document.version_label == "fixture-v1" and document.index_version is None


@pytest.mark.parametrize(
    "data,code",
    [
        (b"%PDF-1.7\nbroken", "invalid_pdf"),
        (pdf_pages("x", encrypted=True), "encrypted_pdf"),
        (pdf_pages(""), "no_extractable_text"),
        (pdf_pages("", image_page=True), "unsupported_image_page"),
    ],
)
def test_parse_failure_is_atomic_safe_status_and_terminal_job(api, data, code):
    client, app, owners = api
    identifier = UUID(post(client, data=data, media="application/pdf").json()["document_id"])
    outcome = service(app).parse(owners[0], identifier)
    assert outcome.status == "FAILED" and outcome.error == code and outcome.chunks == 0
    assert rows(app, Chunk) == []
    job = rows(app, Job)[0]
    assert job.status == "FAILED" and job.error_code == code
    response = client.get(f"/documents/{identifier}/status", headers={"X-API-Key": KEY_A})
    assert response.status_code == 200 and response.json()["error"] == code
    assert service(app).parse(owners[0], identifier).reused


def test_replay_reuses_same_chunk_ids_and_does_not_downgrade_ready(api):
    client, app, owners = api
    identifier = UUID(post(client, data=b"evidence " * 300).json()["document_id"])
    ingest = service(app)
    ingest.parse(owners[0], identifier)
    original = ingest.chunks(owners[0], identifier)
    assert ingest.parse(owners[0], identifier).reused
    assert ingest.chunks(owners[0], identifier) == original
    with app.state.database.sessions.begin() as session:
        document = session.get(Document, identifier)
        # A state fixture only: this is not an executed embedding evaluation.
        document.status, document.index_version = "READY", "synthetic-test-index"
    assert ingest.parse(owners[0], identifier).status == "READY"
    assert ingest.chunks(owners[0], identifier) == original


def test_cross_owner_and_unknown_are_identical_before_read_or_parse(api, monkeypatch):
    client, app, owners = api
    identifier = UUID(post(client).json()["document_id"])

    def forbidden(*args):
        pytest.fail("unauthorized_file_read")

    monkeypatch.setattr(app.state.files, "read_verified", forbidden)
    for requested in (identifier, uuid4()):
        with pytest.raises(ResourceNotFound, match="^resource_not_found$"):
            service(app).parse(owners[1], requested)
        with pytest.raises(ResourceNotFound):
            service(app).chunks(owners[1], requested)
    assert rows(app, Chunk) == []


def test_disabled_owner_is_rejected(api):
    client, app, owners = api
    identifier = UUID(post(client).json()["document_id"])
    with app.state.database.sessions.begin() as session:
        session.get(User, owners[0]).active = False
    with pytest.raises(ResourceNotFound):
        service(app).parse(owners[0], identifier)


def test_hash_mismatch_never_creates_chunks_or_false_failed_state(api):
    client, app, owners = api
    identifier = UUID(post(client).json()["document_id"])
    document = rows(app, Document)[0]
    app.state.files.path(document.storage_key).write_bytes(b"modified source")
    with pytest.raises(StorageUnavailable):
        service(app).parse(owners[0], identifier)
    assert rows(app, Chunk) == [] and rows(app, Document)[0].status == "UPLOADED"


def test_second_chunk_constraint_failure_rolls_back_all_chunks_and_status(api, monkeypatch):
    client, app, owners = api
    identifier = UUID(post(client, data=b"evidence " * 300).json()["document_id"])
    original, calls = ChunkRepository.add, []

    def fail(self, chunk):
        calls.append(chunk)
        if len(calls) == 2:
            chunk.ordinal = -1
        original(self, chunk)

    with monkeypatch.context() as patch:
        patch.setattr(ChunkRepository, "add", fail)
        with pytest.raises(DataIntegrityError):
            service(app).parse(owners[0], identifier)
    assert len(calls) == 2 and rows(app, Chunk) == []
    assert rows(app, Document)[0].status == "UPLOADED" and rows(app, Job)[0].status == "PENDING"
    assert service(app).parse(owners[0], identifier).status == "PARSED"


def test_database_failure_during_save_cannot_claim_failed_was_saved(api, monkeypatch):
    client, app, owners = api
    identifier = UUID(post(client).json()["document_id"])

    def fail(*args):
        raise OperationalError("SQL", None, RuntimeError("private database detail"))

    with monkeypatch.context() as patch:
        patch.setattr(ChunkRepository, "add", fail)
        with pytest.raises(DatabaseUnavailable, match="^database_unavailable$"):
            service(app).parse(owners[0], identifier)
    assert rows(app, Chunk) == [] and rows(app, Document)[0].status == "UPLOADED"


def test_concurrent_parse_serializes_save_and_reuses_result(api):
    client, app, owners = api
    identifier = UUID(post(client).json()["document_id"])
    barrier = threading.Barrier(2, timeout=5)

    class Simultaneous:
        def parse(self, data, media):
            barrier.wait()
            return ParsedText((PageText(data.decode()),))

    def run():
        return service(app, parser=Simultaneous()).parse(owners[0], identifier)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: run(), range(2)))
    assert sorted(result.reused for result in outcomes) == [False, True]
    assert len(rows(app, Chunk)) == 1


def test_parsing_holds_no_db_connection_and_late_version_change_cannot_commit(api):
    client, app, owners = api
    identifier = UUID(post(client).json()["document_id"])

    class VersionChanges:
        def parse(self, data, media):
            assert app.state.database.engine.pool.checkedout() == 0
            with app.state.database.sessions.begin() as session:
                session.get(Document, identifier).ingest_version = "future-v2"
            return ParsedText((PageText(data.decode()),))

    with pytest.raises(IngestConflict):
        service(app, parser=VersionChanges()).parse(owners[0], identifier)
    assert rows(app, Chunk) == [] and rows(app, Document)[0].status == "UPLOADED"
    assert rows(app, Document)[0].ingest_version == "future-v2"


def test_local_command_cannot_bypass_running_task(api):
    client, app, owners = api
    identifier = UUID(post(client).json()["document_id"])
    with app.state.database.sessions.begin() as session:
        session.scalar(select(Job)).status = "RUNNING"
    with pytest.raises(IngestConflict):
        service(app).parse(owners[0], identifier)
    assert rows(app, Chunk) == [] and rows(app, Job)[0].status == "RUNNING"


def test_lost_commit_acknowledgement_reconciles_without_duplicate_chunks(api):
    client, app, owners = api
    identifier = UUID(post(client).json()["document_id"])
    original = app.state.database.sessions

    class LostAcknowledgement:
        def __call__(self):
            return original()

        @contextmanager
        def begin(self):
            with original.begin() as session:
                yield session
            raise OperationalError("COMMIT", None, RuntimeError("private commit detail"))

    ingest = IngestionService(LostAcknowledgement(), app.state.files, app.state.settings)
    with pytest.raises(DatabaseUnavailable):
        ingest.parse(owners[0], identifier)
    original_ids = [row.id for row in rows(app, Chunk)]
    assert original_ids and rows(app, Document)[0].status == "PARSED"
    assert service(app).parse(owners[0], identifier).reused
    assert [row.id for row in rows(app, Chunk)] == original_ids


def test_chunk_repository_owner_filter_works_without_service_precheck(api):
    client, app, owners = api
    identifier = UUID(post(client).json()["document_id"])
    service(app).parse(owners[0], identifier)
    with app.state.database.sessions() as session:
        assert ChunkRepository(session).for_document(owners[1], identifier, INGEST_VERSION) == []
    forbidden = client.get(f"/documents/{identifier}/status", headers={"X-API-Key": KEY_B})
    assert forbidden.status_code == 404


def test_parse_timeout_is_persisted_and_exposed_as_safe_string(api):
    client, app, owners = api
    identifier = UUID(post(client).json()["document_id"])

    class Timeout:
        def parse(self, data, media):
            raise ParseRejected("parse_timeout")

    assert service(app, parser=Timeout()).parse(owners[0], identifier).error == "parse_timeout"
    response = client.get(f"/documents/{identifier}/status", headers={"X-API-Key": KEY_A})
    assert response.status_code == 200 and response.json()["error"] == "parse_timeout"
    assert rows(app, Job)[0].status == "FAILED" and rows(app, Chunk) == []


def test_corrupt_file_after_parsed_never_silently_replaces_evidence(api):
    client, app, owners = api
    identifier = UUID(post(client).json()["document_id"])
    ingest = service(app)
    ingest.parse(owners[0], identifier)
    original = ingest.chunks(owners[0], identifier)
    app.state.files.path(rows(app, Document)[0].storage_key).unlink()
    with pytest.raises(StorageUnavailable):
        ingest.parse(owners[0], identifier)
    assert ingest.chunks(owners[0], identifier) == original
    assert rows(app, Document)[0].status == "PARSED"
    assert (
        client.get(f"/documents/{identifier}/status", headers={"X-API-Key": KEY_A}).status_code
        == 503
    )


def test_other_processing_version_is_preserved_and_not_parsed_with_current_profile(api):
    client, app, owners = api
    identifier = UUID(post(client).json()["document_id"])
    with app.state.database.sessions.begin() as session:
        session.get(Document, identifier).ingest_version = "legacy-v0"
    with pytest.raises(IngestConflict):
        service(app).parse(owners[0], identifier)
    assert rows(app, Chunk) == [] and rows(app, Document)[0].ingest_version == "legacy-v0"
