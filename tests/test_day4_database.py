"""Day 4 acceptance against isolated PostgreSQL, with real API and temporary files."""

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from test_database import database as database
from test_database import migration
from test_database import owners as owners
from test_database import postgres as postgres

from app.core.config import Settings
from app.db.models import Document, Job, User
from app.db.repositories import JobRepository
from app.main import create_app
from app.services.auth import AuthenticationRequired, AuthService, key_digest
from app.services.documents import DocumentService
from app.services.uploads import UploadService
from app.storage.files import FileReference
from scripts import day4

pytestmark = [
    pytest.mark.database,
    pytest.mark.skipif(
        os.environ.get("TRACEAGENT_DATABASE_TESTS") != "1",
        reason="requires isolated real PostgreSQL",
    ),
]
KEY_A, KEY_B = "a" * 43, "b" * 43


@pytest.fixture
def api(database, owners, tmp_path):
    with database.sessions.begin() as session:
        for owner, key in zip(owners, (KEY_A, KEY_B), strict=True):
            session.get(User, owner).api_key_digest = key_digest(key)
    app = create_app(Settings(upload_root=tmp_path / "uploads"), database=database)
    with TestClient(app) as client:
        yield client, app, owners


def post(client, key=KEY_A, data=b"real PostgreSQL upload", fields=None, media="text/plain"):
    return client.post(
        "/documents",
        headers={"X-API-Key": key},
        data=fields or day4.metadata(),
        files={"file": ("../../untrusted.txt", data, media)},
    )


def test_real_http_storage_atomic_registration_dedup_and_owner_isolation(api):
    client, app, owners = api
    first = post(client)
    assert first.status_code == 202
    identifier = first.json()["document_id"]
    assert post(client).json() == first.json()
    second = post(client, KEY_B)
    assert second.status_code == 202 and second.json()["document_id"] != identifier
    with app.state.database.sessions() as session:
        document = session.get(Document, UUID(identifier))
        job = session.get(Job, UUID(first.json()["job_id"]))
        assert job.owner_id == document.owner_id == owners[0]
        assert job.document_id == document.id
        assert document.file_sha256 == hashlib.sha256(b"real PostgreSQL upload").hexdigest()
        assert document.ingest_version == "ingest-v1" and document.publication_year is None
        assert app.state.files.path(document.storage_key).read_bytes() == b"real PostgreSQL upload"
    own = client.get(f"/documents/{identifier}/status", headers={"X-API-Key": KEY_A})
    assert own.status_code == 200 and own.json()["index_version"] is None
    forbidden = client.get(f"/documents/{identifier}/status", headers={"X-API-Key": KEY_B})
    absent = client.get(
        "/documents/ffffffff-ffff-4fff-8fff-ffffffffffff/status", headers={"X-API-Key": KEY_B}
    )
    assert forbidden.status_code == absent.status_code == 404 and forbidden.json() == absent.json()
    assert len(list(app.state.files.root.glob("*.bin"))) == 2


def test_conflict_preserves_original_metadata_and_file(api):
    client, app, _ = api
    first = post(client)
    assert post(client, fields={**day4.metadata(), "title": "conflicting"}).status_code == 409
    assert len(list(app.state.files.root.glob("*.bin"))) == 1
    assert (
        client.get(
            f"/documents/{first.json()['document_id']}/status", headers={"X-API-Key": KEY_A}
        ).status_code
        == 200
    )


def test_authentication_digest_active_flag_unknown_key_and_seed_idempotence(api):
    _, app, owners = api
    auth = AuthService(app.state.database.sessions)
    assert auth.authenticate(KEY_A).owner_id == owners[0]
    with pytest.raises(AuthenticationRequired):
        auth.authenticate("c" * 43)
    with app.state.database.sessions.begin() as session:
        user = session.get(User, owners[0])
        assert user.api_key_digest == key_digest(KEY_A) and KEY_A not in user.api_key_digest
        user.active = False
    with pytest.raises(AuthenticationRequired):
        auth.authenticate(KEY_A)


def test_failed_job_insert_rolls_back_document_and_removes_candidate(api, monkeypatch):
    client, app, _ = api

    def bad(self, job):
        job.status = "INVALID"
        self.session.add(job)
        self.session.flush()

    monkeypatch.setattr(JobRepository, "add", bad)
    response = post(client)
    assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
    with app.state.database.sessions() as session:
        assert session.scalar(select(Document.id)) is None
        assert session.scalar(select(Job.id)) is None
    assert not list(app.state.files.root.glob("*"))


def test_real_concurrent_upload_keeps_one_record_job_and_file(api, monkeypatch):
    _, app, owners = api
    barrier = threading.Barrier(2, timeout=5)
    original = app.state.files.save

    def simultaneous(*args, **kwargs):
        result = original(*args, **kwargs)
        barrier.wait()
        return result

    monkeypatch.setattr(app.state.files, "save", simultaneous)
    from test_day4 import payload

    service = UploadService(
        DocumentService(app.state.database.sessions), app.state.files, app.state.settings
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: service.upload(
                    owners[0], payload(), BytesIO(b"same concurrent file"), "text/plain"
                ),
                range(2),
            )
        )
    assert results[0].document_id == results[1].document_id
    assert results[0].job_id == results[1].job_id
    assert sorted(result.reused for result in results) == [False, True]
    assert len(list(app.state.files.root.glob("*.bin"))) == 1
    for result in results:
        app.state.files.verify(
            result.stored_file, hashlib.sha256(b"same concurrent file").hexdigest()
        )


def test_unfinished_task_cap_allows_dedup_and_blocks_new_upload(api):
    client, app, _ = api
    accepted = [post(client, data=f"file {i}".encode()) for i in range(5)]
    assert all(response.status_code == 202 for response in accepted)
    assert post(client, data=b"file 0").json() == accepted[0].json()
    response = post(client, data=b"file 5")
    assert (
        response.status_code == 429 and response.json()["error"]["code"] == "too_many_pending_tasks"
    )
    assert len(list(app.state.files.root.glob("*.bin"))) == 5


def test_missing_file_returns_service_failure_not_404_or_success(api):
    client, app, _ = api
    identifier = UUID(post(client).json()["document_id"])
    with app.state.database.sessions() as session:
        app.state.files.path(session.get(Document, identifier).storage_key).unlink()
    response = client.get(f"/documents/{identifier}/status", headers={"X-API-Key": KEY_A})
    assert response.status_code == 503 and response.json()["error"]["code"] == "storage_unavailable"
    assert post(client).status_code == 503
    assert not list(app.state.files.root.glob("*.bin"))


@pytest.mark.parametrize(
    "changes",
    [
        {"storage_key": "../unsafe", "file_size_bytes": 1, "file_media_type": "text/plain"},
        {"file_size_bytes": None},
        {"storage_key": "a" * 32, "file_size_bytes": 5242881, "file_media_type": "text/plain"},
        {"storage_key": "a" * 32, "file_size_bytes": 1, "file_media_type": "image/png"},
    ],
)
def test_database_rejects_invalid_file_reference(api, changes):
    client, app, _ = api
    identifier = UUID(post(client).json()["document_id"])
    with pytest.raises(IntegrityError):
        with app.state.database.sessions.begin() as session:
            document = session.get(Document, identifier)
            for key, value in changes.items():
                setattr(document, key, value)
            session.flush()


def test_upgrade_from_day3_preserves_rows_and_downgrade_is_reversible(database):
    migration(database, "downgrade", "0001")
    with database.engine.begin() as connection:
        connection.execute(
            text("INSERT INTO users (id) VALUES ('10000000-0000-4000-8000-000000000099')")
        )
    migration(database, "upgrade", "head")
    with database.sessions() as session:
        user = session.get(User, UUID("10000000-0000-4000-8000-000000000099"))
        assert user is not None and user.api_key_digest is None
    assert {c["name"] for c in inspect(database.engine).get_columns("documents")} >= {
        "storage_key",
        "file_size_bytes",
        "file_media_type",
    }


def test_digest_unique_and_format_constraints(database, owners):
    with pytest.raises(IntegrityError):
        with database.sessions.begin() as session:
            for owner in owners:
                session.get(User, owner).api_key_digest = key_digest(KEY_A)
    with pytest.raises(IntegrityError):
        with database.sessions.begin() as session:
            session.get(User, owners[0]).api_key_digest = "invalid"


def test_seed_preserves_ids_and_digests_and_conflict_rolls_back(database, owners, monkeypatch):
    monkeypatch.setenv(
        "TRACEAGENT_DB_URL", database.engine.url.render_as_string(hide_password=False)
    )
    monkeypatch.setattr(day4.day3, "USER_A", owners[0])
    monkeypatch.setattr(day4.day3, "USER_B", owners[1])
    env = {
        "TRACEAGENT_DB_URL": os.environ["TRACEAGENT_DB_URL"],
        "TRACEAGENT_SEED_API_KEY_A": KEY_A,
        "TRACEAGENT_SEED_API_KEY_B": KEY_B,
    }
    assert day4.seed(env)["users"] == 2
    assert day4.seed(env)["users"] == 2
    with database.sessions() as session:
        assert session.get(User, owners[0]).api_key_digest == key_digest(KEY_A)
        assert session.get(User, owners[1]).api_key_digest == key_digest(KEY_B)
    with pytest.raises(day4.day3.ProbeFailure, match="seed_key_conflict"):
        day4.seed({**env, "TRACEAGENT_SEED_API_KEY_A": "c" * 43})
    with database.sessions() as session:
        assert session.get(User, owners[0]).api_key_digest == key_digest(KEY_A)


def test_concurrent_different_uploads_enforce_five_task_cap(api):
    _, app, owners = api
    from test_day4 import payload

    service = UploadService(
        DocumentService(app.state.database.sessions), app.state.files, app.state.settings
    )
    from app.services.documents import TooManyPendingTasks

    def upload(i):
        try:
            return service.upload(
                owners[0], payload(), BytesIO(f"concurrent unique {i}".encode()), "text/plain"
            )
        except TooManyPendingTasks:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(upload, range(6)))
    assert sum(result is not None for result in results) == 5
    assert len(list(app.state.files.root.glob("*.bin"))) == 5


def test_commit_acknowledgement_loss_retains_committed_file_and_retry_recovers(api, monkeypatch):
    client, app, _ = api
    original = app.state.database.sessions

    class AcknowledgementLost:
        def __call__(self):
            return original()

        @contextmanager
        def begin(self):
            with original.begin() as session:
                yield session
            # Actual COMMIT above succeeded. Inject a lost acknowledgement afterward.
            raise OperationalError("COMMIT", None, RuntimeError("private connection details"))

    with monkeypatch.context() as patch:
        patch.setattr(app.state.database, "sessions", AcknowledgementLost())
        response = post(client)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    with original() as session:
        document = session.scalar(select(Document))
        job = session.scalar(select(Job))
        assert document is not None and job.document_id == document.id
        identifier = str(document.id)
        app.state.files.verify(
            FileReference(
                key=document.storage_key,
                size_bytes=document.file_size_bytes,
                media_type=document.file_media_type,
            ),
            document.file_sha256,
        )
    retry = post(client)
    assert retry.status_code == 202 and retry.json()["document_id"] == identifier
    assert len(list(app.state.files.root.glob("*.bin"))) == 1


def test_failed_document_is_successful_status_query_with_safe_error(api):
    client, app, _ = api
    identifier = UUID(post(client).json()["document_id"])
    with app.state.database.sessions.begin() as session:
        document = session.get(Document, identifier)
        document.status = "FAILED"
        document.error_code = "unsafe vendor SQL leak-marker"
    response = client.get(f"/documents/{identifier}/status", headers={"X-API-Key": KEY_A})
    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    assert response.json()["error"] == "internal_error" and "leak-marker" not in response.text


def test_legacy_synthetic_metadata_is_not_a_real_upload(api):
    client, app, owners = api
    from scripts.day3 import synthetic_metadata

    registered = DocumentService(app.state.database.sessions).register(
        owners[0], synthetic_metadata()
    )
    response = client.get(
        f"/documents/{registered.document_id}/status", headers={"X-API-Key": KEY_A}
    )
    assert response.status_code == 503 and response.json()["error"]["code"] == "storage_unavailable"
