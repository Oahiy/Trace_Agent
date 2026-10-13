import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.config import ConfigurationError
from app.db.config import load_database_settings
from app.main import create_app
from app.services.documents import DocumentService
from scripts import day3

DUMMY_URL = "postgresql+psycopg://test:leak-marker@127.0.0.1:55432/traceagent_test"


@pytest.fixture
def clean_database_environment(monkeypatch):
    for key in os.environ:
        if key.startswith("TRACEAGENT_DB_"):
            monkeypatch.delenv(key)


def test_database_configuration_explicit_and_secret(clean_database_environment, monkeypatch):
    monkeypatch.setenv("TRACEAGENT_DB_URL", DUMMY_URL)
    settings = load_database_settings()
    assert settings.connect_timeout_seconds == 3
    assert settings.url.get_secret_value() == DUMMY_URL
    assert "leak-marker" not in repr(settings)
    with pytest.raises(ValidationError):
        settings.statement_timeout_ms = 100


@pytest.mark.parametrize(
    "value",
    [
        "not-a-url-leak-marker",
        "postgresql://test:leak-marker@127.0.0.1:55432/db",
        "postgresql+psycopg://test:leak-marker@remote.example:55432/db",
        "postgresql+psycopg://test:leak-marker@127.0.0.1/db",
        "postgresql+psycopg://test:leak-marker@127.0.0.1:0/db",
        "postgresql+psycopg://test:leak-marker@127.0.0.1:99999/db",
        "postgresql+psycopg://test:leak-marker@127.0.0.1:55432/db?options=unsafe",
        "postgresql+psycopg://test@127.0.0.1:55432/db",
        DUMMY_URL + "\n",
    ],
)
def test_bad_database_url_is_sanitized(clean_database_environment, monkeypatch, value):
    monkeypatch.setenv("TRACEAGENT_DB_URL", value)
    with pytest.raises(ConfigurationError) as error:
        load_database_settings()
    assert str(error.value) == "invalid_database_configuration"


@pytest.mark.parametrize(
    "key,value",
    [
        ("CONNECT_TIMEOUT_SECONDS", "1"),
        ("POOL_TIMEOUT_SECONDS", "nan"),
        ("POOL_TIMEOUT_SECONDS", "0"),
        ("STATEMENT_TIMEOUT_MS", "0"),
        ("LOCK_TIMEOUT_MS", "10001"),
        ("IDLE_TRANSACTION_TIMEOUT_MS", "999"),
    ],
)
def test_database_timeout_bounds(clean_database_environment, monkeypatch, key, value):
    monkeypatch.setenv("TRACEAGENT_DB_URL", DUMMY_URL)
    monkeypatch.setenv("TRACEAGENT_DB_" + key, value)
    with pytest.raises(ConfigurationError):
        load_database_settings()


def test_health_creation_does_not_require_database(clean_database_environment, monkeypatch):
    monkeypatch.setenv("TRACEAGENT_DB_URL", "invalid")
    assert create_app().state.settings is not None


def test_database_env_file_is_not_implicitly_loaded(
    clean_database_environment, monkeypatch, tmp_path
):
    (tmp_path / ".env").write_text("TRACEAGENT_DB_URL=" + DUMMY_URL)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigurationError):
        load_database_settings()


@pytest.mark.parametrize(
    "change",
    [
        {"title": " "},
        {"title": "x" * 201},
        {"publication_year": True},
        {"publication_year": "2021"},
        {"owner_id": "client"},
        {"file_sha256": "A" * 64},
        {"ingest_version": "../unsafe"},
        {"source_url": "http://secret@localhost/file"},
        {"source_url": "file:///private"},
        {"source_url": "https://example.org:invalid/x"},
    ],
)
def test_invalid_metadata_never_opens_transaction(change):
    class SpyFactory:
        calls = 0

        def begin(self):
            self.calls += 1
            raise AssertionError("invalid input reached the database")

    factory = SpyFactory()
    payload = {**day3.synthetic_metadata().model_dump(), **change}
    with pytest.raises(ValidationError):
        DocumentService(factory).register(uuid4(), payload)
    assert factory.calls == 0


def test_metadata_instances_are_revalidated():
    invalid = day3.synthetic_metadata().model_copy(update={"title": " "})
    with pytest.raises(ValidationError):
        DocumentService(None).register(uuid4(), invalid)


def test_prepare_creates_private_credentials_once(monkeypatch, tmp_path):
    cache = tmp_path / "day3"
    target = cache / "database.env"
    monkeypatch.setattr(day3, "CACHE", cache)
    monkeypatch.setattr(day3, "ENV_FILE", target)
    day3.prepare()
    original = target.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600
    assert set(line.split("=", 1)[0] for line in original.decode().splitlines()) == day3.ENV_KEYS
    day3.prepare()
    assert target.read_bytes() == original
    target.chmod(0o644)
    with pytest.raises(day3.ProbeFailure, match="permissions_invalid"):
        day3.prepare()


def test_cli_unexpected_error_has_no_private_details(monkeypatch, capsys):
    def fail():
        raise RuntimeError("leak-marker SQL password stack")

    monkeypatch.setattr(day3, "prepare", fail)
    assert day3.main(["prepare"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error_code": "day3_probe_failed",
    }


def test_day3_cache_inventory_and_gitignore():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "git",
            "check-ignore",
            ".cache/manifest.json",
            ".cache/day3/database.env",
            ".cache/day3/tests.xml",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert set(result.stdout.splitlines()) == {
        ".cache/manifest.json",
        ".cache/day3/database.env",
        ".cache/day3/tests.xml",
    }


def test_prepare_rejects_symlink(monkeypatch, tmp_path):
    target = tmp_path / "database.env"
    real = tmp_path / "other.env"
    real.write_text("preserve")
    target.symlink_to(real)
    monkeypatch.setattr(day3, "CACHE", tmp_path)
    monkeypatch.setattr(day3, "ENV_FILE", target)
    with pytest.raises(day3.ProbeFailure, match="symlink_rejected"):
        day3.prepare()
    assert real.read_text() == "preserve"
