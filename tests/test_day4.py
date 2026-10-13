"""Fast checks: real temporary files, explicit fakes for database-dependent behavior."""

import asyncio
import hashlib
import json
import os
import subprocess
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import documents as routes
from app.core.config import Settings
from app.db.session import DatabaseUnavailable
from app.main import create_app
from app.services.auth import AuthenticationRequired, AuthService, Principal
from app.services.documents import (
    DataIntegrityError,
    DocumentMetadata,
    Registration,
    ResourceNotFound,
    SourceMetadataConflict,
    StoredDocument,
    TooManyPendingTasks,
)
from app.services.uploads import INGEST_VERSION, UploadService
from app.storage.files import FileStore, StorageUnavailable, UploadRejected
from scripts import day4

KEY = "a" * 43  # Fixed test-only key, never used for a seeded runtime user.
OWNER = uuid4()


def payload(**changes):
    return DocumentMetadata.model_validate(
        {
            **day4.metadata(),
            "publication_year": None,
            "file_sha256": "0" * 64,
            "ingest_version": INGEST_VERSION,
            **changes,
        }
    )


@pytest.fixture
def files(tmp_path):
    return FileStore(tmp_path / "uploads")


def save(files, data=b"synthetic text", media="text/plain", limit=5 * 1024 * 1024, chars=200_000):
    return files.save(BytesIO(data), media, max_bytes=limit, max_characters=chars)


def test_real_file_bytes_hash_private_permissions_and_verify(files):
    data = "可追溯的合成资料\n".encode()
    result = save(files, data)
    path = files.path(result.reference.key)
    assert path.read_bytes() == data
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert path.stat().st_mode & 0o777 == 0o600
    assert files.root.stat().st_mode & 0o777 == 0o700
    files.verify(result.reference, result.sha256)
    assert not list(files.root.glob("*.part"))


@pytest.mark.parametrize(
    "data,media,code,status",
    [
        (b"", "text/plain", "invalid_request", 422),
        (b"  \n", "text/plain", "invalid_request", 422),
        (b"\xff", "text/plain", "invalid_request", 422),
        (b"a\x00b", "text/plain", "invalid_request", 422),
        (b"%PDF-1.7", "text/plain", "invalid_request", 422),
        (b"not PDF", "application/pdf", "unsupported_file_type", 415),
        (b"test", "image/png", "unsupported_file_type", 415),
    ],
)
def test_invalid_content_has_no_saved_file(files, data, media, code, status):
    with pytest.raises(UploadRejected) as caught:
        save(files, data, media)
    assert caught.value.code == code and caught.value.status == status
    assert not list(files.root.glob("*"))


def test_exact_size_and_one_byte_over(files):
    accepted = save(files, b"x" * 128, limit=128)
    with pytest.raises(UploadRejected, match="file_too_large"):
        save(files, b"x" * 129, limit=128)
    assert len(list(files.root.glob("*.bin"))) == 1
    files.verify(accepted.reference, accepted.sha256)


def test_utf8_character_split_and_character_limit(files):
    data = b"a" * 65535 + "中文".encode()
    save(files, data)
    with pytest.raises(UploadRejected, match="invalid_request"):
        save(files, "中文".encode(), chars=1)
    with pytest.raises(UploadRejected, match="invalid_request"):
        save(files, b"ok\xe4\xb8")
    assert len(list(files.root.glob("*.bin"))) == 1


def test_storage_failure_cleans_candidate_and_never_leaks_error(files, monkeypatch):
    def fail(*args):
        raise OSError("private disk path and leak-marker")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(StorageUnavailable, match="^storage_unavailable$"):
        save(files)
    assert not list(files.root.glob("*"))


def test_directory_fsync_failure_cleans_promoted_file(files, monkeypatch):
    real = os.fsync
    calls = 0

    def fail_second(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failed")
        return real(fd)

    monkeypatch.setattr(os, "fsync", fail_second)
    with pytest.raises(StorageUnavailable):
        save(files)
    assert not list(files.root.glob("*"))


def test_storage_symlink_and_path_traversal_rejected(files, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    files.root.symlink_to(real, target_is_directory=True)
    with pytest.raises(StorageUnavailable):
        save(files)
    with pytest.raises(StorageUnavailable):
        files.path("../../outside")
    assert not list(real.iterdir())


def test_exclusive_creation_collision_preserves_preexisting_temporary_file(files, monkeypatch):
    import app.storage.files as module

    files.root.mkdir(mode=0o700)
    identifier = uuid4()
    existing = files.root / (identifier.hex + ".part")
    existing.write_bytes(b"unrelated work")
    monkeypatch.setattr(module, "uuid4", lambda: identifier)
    with pytest.raises(StorageUnavailable):
        save(files)
    assert existing.read_bytes() == b"unrelated work"


def test_publication_collision_preserves_existing_final_file(files, monkeypatch):
    import app.storage.files as module

    files.root.mkdir(mode=0o700)
    identifier = uuid4()
    existing = files.path(identifier.hex)
    existing.write_bytes(b"unrelated committed file")
    monkeypatch.setattr(module, "uuid4", lambda: identifier)
    with pytest.raises(StorageUnavailable):
        save(files)
    assert existing.read_bytes() == b"unrelated committed file"
    assert not list(files.root.glob("*.part"))


@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink", "length"])
def test_saved_file_damage_is_detected(files, tmp_path, damage):
    artifact = save(files)
    path = files.path(artifact.reference.key)
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"X" * artifact.reference.size_bytes)
    elif damage == "length":
        path.write_bytes(b"x")
    else:
        other = tmp_path / "other"
        path.rename(other)
        path.symlink_to(other)
    with pytest.raises(StorageUnavailable):
        files.verify(artifact.reference, artifact.sha256)


class FakeDocuments:
    """Only unit/API tests use this fake; integration tests use PostgreSQL."""

    records = {}
    calls = 0

    def __init__(self, sessions=None):
        pass

    def register(self, owner, metadata, reference):
        type(self).calls += 1
        identifier = uuid4()
        meta = DocumentMetadata.model_validate(metadata)
        self.records[identifier] = StoredDocument(
            identifier, owner, meta, "UPLOADED", stored_file=reference
        )
        return Registration(identifier, uuid4(), "UPLOADED", "PENDING", False, reference)

    def get(self, owner, identifier):
        record = self.records.get(identifier)
        return record if record is not None and record.owner_id == owner else None


@pytest.fixture
def api(tmp_path, monkeypatch):
    FakeDocuments.records = {}
    FakeDocuments.calls = 0

    def authenticate(self, key):
        if key != KEY:
            raise AuthenticationRequired()
        return Principal(OWNER)

    monkeypatch.setattr(AuthService, "authenticate", authenticate)
    monkeypatch.setattr(routes, "DocumentService", FakeDocuments)
    app = create_app(
        Settings(upload_root=tmp_path / "uploads"), database=SimpleNamespace(sessions=None)
    )
    with TestClient(app) as client:
        yield client, app


def post(client, *, data=b"synthetic text", media="text/plain", fields=None, headers=None):
    return client.post(
        "/documents",
        headers=headers or {"X-API-Key": KEY},
        data=fields if fields is not None else day4.metadata(),
        files={"file": ("../../do-not-use.txt", data, media)},
    )


def test_http_success_contract_ignores_filename_and_keeps_unknown_year(api):
    client, app = api
    response = post(client)
    assert response.status_code == 202
    assert set(response.json()) == {"document_id", "job_id", "document_status", "job_status"}
    identifier = response.json()["document_id"]
    status = client.get(f"/documents/{identifier}/status", headers={"X-API-Key": KEY})
    assert status.status_code == 200
    assert status.json() == {
        "document_id": identifier,
        "status": "UPLOADED",
        "index_version": None,
        "error": None,
    }
    record = next(iter(FakeDocuments.records.values()))
    assert record.metadata.publication_year is None
    assert app.state.files.path(record.stored_file.key).read_bytes() == b"synthetic text"
    assert "do-not-use" not in str(list(app.state.files.root.iterdir()))
    assert app.openapi()["paths"]["/documents"]["post"]["security"] == [{"APIKeyHeader": []}]


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": "client"},
        {"file_sha256": "0" * 64},
        {"ingest_version": "client"},
        {"storage_key": "../path"},
        {"title": " "},
        {"title": "x" * 201},
        {"publication_year": "True"},
        {"publication_year": "2021.0"},
        {"source_url": "file:///secret"},
        {"source_url": "http://key@localhost/path"},
    ],
)
def test_invalid_metadata_rejected_before_file_persistence(api, changes):
    client, app = api
    response = post(client, fields={**day4.metadata(), **changes})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert FakeDocuments.calls == 0
    assert not list(app.state.files.root.glob("*"))


@pytest.mark.parametrize(
    "headers", [{}, {"X-API-Key": "wrong"}, [("X-API-Key", KEY), ("X-API-Key", KEY)]]
)
def test_authentication_precedes_malformed_body_and_saves_nothing(api, headers):
    client, app = api
    response = client.post("/documents", headers=headers, content=b"malformed multipart")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "APIKey"
    assert FakeDocuments.calls == 0
    assert not app.state.files.root.exists()


@pytest.mark.parametrize(
    "content,content_type",
    [
        (b"x", "application/json"),
        (b"x", "multipart/form-data"),
        (b"wrong", "multipart/form-data; boundary=abc"),
        (
            b'--abc\r\nContent-Disposition: form-data; name="file"; filename="x.txt"\r\n\r\nunfinished',
            "multipart/form-data; boundary=abc",
        ),
    ],
)
def test_malformed_and_truncated_multipart_is_safe_400(api, content, content_type):
    client, app = api
    response = client.post(
        "/documents", headers={"X-API-Key": KEY, "Content-Type": content_type}, content=content
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert FakeDocuments.calls == 0
    assert not app.state.files.root.exists()


def multipart(fields):
    return (
        b"".join(
            b'--abc\r\nContent-Disposition: form-data; name="'
            + key.encode()
            + b'"\r\n\r\n'
            + value.encode()
            + b"\r\n"
            for key, value in fields
        )
        + b"--abc--\r\n"
    )


def test_duplicate_fields_and_missing_file_are_rejected(api):
    client, _ = api
    for fields in [[("title", "a"), ("title", "b")], list(day4.metadata().items())]:
        response = client.post(
            "/documents",
            content=multipart(fields),
            headers={"X-API-Key": KEY, "Content-Type": "multipart/form-data; boundary=abc"},
        )
        assert response.status_code == 422
    assert FakeDocuments.calls == 0


def test_extra_files_and_oversized_fields_rejected(api):
    client, _ = api
    response = client.post(
        "/documents",
        headers={"X-API-Key": KEY},
        data=day4.metadata(),
        files=[("file", ("a.txt", b"a", "text/plain")), ("file", ("b.txt", b"b", "text/plain"))],
    )
    assert response.status_code == 400
    response = post(client, fields={**day4.metadata(), "research_dates": "x" * 8193})
    assert response.status_code == 400
    assert FakeDocuments.calls == 0


def test_http_file_limit_respects_tighter_configuration(api):
    client, app = api
    app.state.settings = Settings(upload_root=app.state.files.root, max_file_bytes=12)
    assert post(client, data=b"x" * 12).status_code == 202
    response = post(client, data=b"x" * 13)
    assert response.status_code == 413
    assert FakeDocuments.calls == 1


@pytest.mark.parametrize("content_length", [None, "1"])
def test_body_limit_counts_raw_stream_without_trusting_header(api, content_length):
    _, app = api

    async def run():
        async def chunks():
            yield b"x" * (5 * 1024 * 1024 + 64 * 1024 + 1)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"X-API-Key": KEY, "Content-Type": "multipart/form-data; boundary=abc"}
            if content_length is not None:
                headers["Content-Length"] = content_length
            return await client.post("/documents", headers=headers, content=chunks())

    response = asyncio.run(run())
    assert response.status_code == 413 and response.json()["error"]["code"] == "file_too_large"
    assert FakeDocuments.calls == 0


def test_timeout_during_upload_closes_parser_temporary_files(api, monkeypatch):
    _, app = api
    app.state.settings = Settings(upload_root=app.state.files.root, upload_timeout_seconds=0.01)
    import starlette.formparsers as parser_module

    opened = []
    real = parser_module.SpooledTemporaryFile

    def track(*args, **kwargs):
        file = real(*args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(parser_module, "SpooledTemporaryFile", track)

    async def run():
        async def chunks():
            yield b'--abc\r\nContent-Disposition: form-data; name="file"; filename="x.txt"\r\n\r\na'
            await asyncio.sleep(0.05)
            yield b"\r\n--abc--\r\n"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/documents",
                headers={"X-API-Key": KEY, "Content-Type": "multipart/form-data; boundary=abc"},
                content=chunks(),
            )

    response = asyncio.run(run())
    assert response.status_code == 408
    assert opened and all(file.closed for file in opened)
    assert FakeDocuments.calls == 0


def test_unexpected_business_error_sanitized(api, monkeypatch, caplog):
    client, _ = api

    def fail(*args):
        raise RuntimeError("leak-marker private SQL token stack")

    monkeypatch.setattr(UploadService, "upload", fail)
    response = post(client)
    assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
    assert "leak-marker" not in response.text + caplog.text


@pytest.mark.parametrize(
    "error",
    [SourceMetadataConflict(), ResourceNotFound(), DataIntegrityError(), TooManyPendingTasks()],
)
def test_definitive_rejection_cleans_only_own_candidate(files, error):
    existing = save(files, b"old document")

    class Reject:
        def register(self, *args):
            raise error

    with pytest.raises(type(error)):
        UploadService(Reject(), files, Settings()).upload(
            OWNER, payload(), BytesIO(b"new file"), "text/plain"
        )
    assert list(files.root.glob("*.bin")) == [files.path(existing.reference.key)]
    files.verify(existing.reference, existing.sha256)


def test_ambiguous_commit_failure_retains_candidate(files):
    class Ambiguous:
        def register(self, *args):
            raise DatabaseUnavailable()

    with pytest.raises(DatabaseUnavailable):
        UploadService(Ambiguous(), files, Settings()).upload(
            OWNER, payload(), BytesIO(b"new file"), "text/plain"
        )
    assert len(list(files.root.glob("*.bin"))) == 1


def test_upload_service_revalidates_metadata_before_file_creation(files):
    from pydantic import ValidationError

    bad = payload().model_copy(update={"title": " "})
    with pytest.raises(ValidationError):
        UploadService(None, files, Settings()).upload(OWNER, bad, BytesIO(b"text"), "text/plain")
    assert not files.root.exists()


def test_missing_database_is_503_but_malformed_key_still_401(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACEAGENT_DB_URL", raising=False)
    with TestClient(create_app(Settings(upload_root=tmp_path / "files"))) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/documents", headers={"X-API-Key": "wrong"}).status_code == 401
        response = client.post("/documents", headers={"X-API-Key": KEY})
        assert (
            response.status_code == 503
            and response.json()["error"]["code"] == "database_unavailable"
        )


def test_mime_parameters_and_utf8_bom_preserve_original_bytes(api):
    client, app = api
    original = b"\xef\xbb\xbfUTF-8 text"
    response = post(client, data=original, media="text/plain; charset=utf-8")
    assert response.status_code == 202
    record = next(iter(FakeDocuments.records.values()))
    assert record.metadata.file_sha256 == hashlib.sha256(original).hexdigest()
    assert app.state.files.path(record.stored_file.key).read_bytes() == original


def test_duplicate_cleanup_keeps_original_file(files):
    old = save(files)

    class Duplicate:
        def register(self, *args):
            return Registration(uuid4(), uuid4(), "UPLOADED", "PENDING", True, old.reference)

    result = UploadService(Duplicate(), files, Settings()).upload(
        OWNER, payload(), BytesIO(b"synthetic text"), "text/plain"
    )
    assert result.reused
    assert list(files.root.glob("*.bin")) == [files.path(old.reference.key)]


def test_private_seed_credentials_generated_once(monkeypatch, tmp_path):
    monkeypatch.setattr(day4, "CACHE", tmp_path)
    monkeypatch.setattr(day4, "UPLOADS", tmp_path / "uploads")
    monkeypatch.setattr(day4, "ENV_FILE", tmp_path / "auth.env")
    day4.prepare()
    original = day4.ENV_FILE.read_bytes()
    day4.prepare()
    assert day4.ENV_FILE.read_bytes() == original
    assert day4.ENV_FILE.stat().st_mode & 0o777 == 0o600
    values = dict(line.split("=", 1) for line in original.decode().splitlines())
    assert len(set(values.values())) == 2
    assert all(len(value) == 43 for value in values.values())
    day4.ENV_FILE.chmod(0o644)
    with pytest.raises(day4.day3.ProbeFailure, match="permissions_invalid"):
        day4.prepare()


def test_seed_symlink_and_cli_private_error(monkeypatch, tmp_path, capsys):
    target = tmp_path / "auth.env"
    real = tmp_path / "other"
    real.write_text("preserve")
    target.symlink_to(real)
    monkeypatch.setattr(day4, "CACHE", tmp_path)
    monkeypatch.setattr(day4, "UPLOADS", tmp_path / "uploads")
    monkeypatch.setattr(day4, "ENV_FILE", target)
    assert day4.main(["prepare"]) == 1
    assert "preserve" not in capsys.readouterr().out
    assert real.read_text() == "preserve"


def test_cache_inventory_contains_day4_and_is_ignored():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".cache/manifest.json").read_text())
    assert manifest["upload_files"]["disposable_with_existing_database"] is False
    assert manifest["api_key_credentials"]["contains_secrets"] is True
    names = [
        ".cache/manifest.json",
        ".cache/day4/auth.env",
        ".cache/day4/uploads/test.bin",
        ".cache/day4/tests.xml",
    ]
    result = subprocess.run(
        ["git", "check-ignore", *names], cwd=root, capture_output=True, text=True
    )
    assert result.returncode == 0 and set(result.stdout.splitlines()) == set(names)


def test_upload_root_rejects_relative_and_source_directory():
    from pydantic import ValidationError

    for root in (Path("relative"), Path("/tmp/sources/uploads")):
        with pytest.raises(ValidationError):
            Settings(upload_root=root)


def test_failed_audit_has_nonzero_exit_status(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(day4, "CACHE", tmp_path)
    monkeypatch.setattr(day4, "environment", lambda: {})
    monkeypatch.setattr(
        day4, "audit", lambda env: {"status": "failed", "missing_or_corrupt": ["fixture"]}
    )
    assert day4.main(["audit"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


@pytest.mark.parametrize("key", [None, "", "short", "\u4e2d" * 43, "a" * 44])
def test_malformed_key_does_not_open_database(key):
    with pytest.raises(AuthenticationRequired):
        AuthService(None).authenticate(key)
