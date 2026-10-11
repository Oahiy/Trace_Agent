import asyncio
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.api.schemas import ErrorResponse, ResearchRequest
from app.core.config import ConfigurationError, Settings, load_settings
from app.experiments.day2 import (
    Document,
    DocumentAlreadyExists,
    DocumentCreate,
    DocumentService,
    MemoryDocumentRepository,
    RepositoryError,
    create_document,
)
from app.main import create_app
from scripts import day2 as runner


@pytest.fixture
def clean_settings_environment(monkeypatch):
    for key in os.environ:
        if key.startswith("TRACEAGENT_"):
            monkeypatch.delenv(key)


def test_settings_defaults_and_application_factory(clean_settings_environment):
    settings = load_settings()
    assert settings.local_model_url == "http://127.0.0.1:11434"
    assert settings.model_timeout_seconds == 30
    assert settings.max_file_bytes == 5 * 1024 * 1024
    assert settings.max_pdf_pages == 50
    assert settings.max_text_characters == 200_000
    assert settings.max_question_characters == 2000
    assert create_app(settings).state.settings is settings


def test_settings_environment_overrides_and_empty_defaults(clean_settings_environment, monkeypatch):
    monkeypatch.setenv("TRACEAGENT_MODEL_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("TRACEAGENT_MAX_PDF_PAGES", "10")
    monkeypatch.setenv("TRACEAGENT_LOCAL_MODEL_URL", "")
    settings = load_settings()
    assert settings.model_timeout_seconds == 2.5
    assert settings.max_pdf_pages == 10
    assert settings.local_model_url == "http://127.0.0.1:11434"


@pytest.mark.parametrize(
    "key,value",
    [
        ("MODEL_TIMEOUT_SECONDS", "-1"),
        ("MODEL_TIMEOUT_SECONDS", "0"),
        ("MODEL_TIMEOUT_SECONDS", "nan"),
        ("MODEL_TIMEOUT_SECONDS", "inf"),
        ("MODEL_TIMEOUT_SECONDS", "121"),
        ("MAX_FILE_BYTES", "0"),
        ("MAX_FILE_BYTES", str(5 * 1024 * 1024 + 1)),
        ("MAX_PDF_PAGES", "51"),
        ("MAX_TEXT_CHARACTERS", "200001"),
        ("MAX_QUESTION_CHARACTERS", "2001"),
        ("LOCAL_MODEL_URL", "https://remote.example/leak-marker"),
        ("LOCAL_MODEL_URL", "http://leak-marker@127.0.0.1:11434"),
        ("LOCAL_MODEL_URL", "http://127.0.0.1:11434/api"),
        ("LOCAL_MODEL_URL", "http://127.0.0.1:11434?token=leak-marker"),
        ("LOCAL_MODEL_URL", "http://127.0.0.1:11434#leak-marker"),
        ("LOCAL_MODEL_URL", "http://127.0.0.1:0"),
        ("LOCAL_MODEL_URL", "http://127.0.0.1:invalid"),
        ("LOCAL_MODEL_URL", "http://127.0.0.1:11434\n"),
        ("LOCAL_MODEL_URL", "http://127.0.0.1:\t11434"),
        ("LOCAL_MODEL_URL", " http://127.0.0.1:11434"),
        ("LOCAL_MODEL_URL", "\x01http://127.0.0.1:11434"),
        ("LOCAL_MODEL_URL", "\x7fhttp://127.0.0.1:11434"),
    ],
)
def test_invalid_configuration_stops_app_without_values(
    clean_settings_environment, monkeypatch, key, value
):
    monkeypatch.setenv("TRACEAGENT_" + key, value)
    with pytest.raises(ConfigurationError) as error:
        create_app()
    assert str(error.value) == "invalid_configuration"


def test_settings_valid_boundaries_and_frozen(clean_settings_environment):
    settings = Settings(
        model_timeout_seconds=120, max_file_bytes=1, local_model_url="http://127.0.0.1:1234/"
    )
    assert settings.local_model_url == "http://127.0.0.1:1234"
    with pytest.raises(ValidationError):
        settings.model_timeout_seconds = 10


def test_nul_url_rejected_on_direct_configuration(clean_settings_environment):
    # OS environment values cannot contain NUL, but a Python constructor can.
    with pytest.raises(ValidationError):
        Settings(local_model_url="\x00http://127.0.0.1:11434")


def test_env_file_is_not_loaded_implicitly(clean_settings_environment, monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("TRACEAGENT_MODEL_TIMEOUT_SECONDS=-1\n")
    monkeypatch.chdir(tmp_path)
    assert load_settings().model_timeout_seconds == 30


def test_health_and_legacy_errors_keep_their_public_contract(clean_settings_environment):
    async def run():
        application = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            health = await client.get("/health")
            missing = await client.get("/missing")
            method = await client.post("/health")
        assert health.status_code == 200 and health.json() == {"status": "ok"}
        assert missing.status_code == 404 and missing.json() == {"detail": "Not Found"}
        assert method.status_code == 405 and method.json() == {"detail": "Method Not Allowed"}
        assert "GET" in method.headers["allow"]

    asyncio.run(run())


def test_document_success_and_missing_id():
    service = DocumentService(MemoryDocumentRepository())
    document = create_document(
        {"title": " title ", "content": " text "}, service, document_id="doc-001"
    )
    assert document == Document("doc-001", "title", "text")
    assert service.get("doc-001") == document
    assert service.get("missing") is None


def test_document_exact_normalized_limits():
    request = DocumentCreate.model_validate(
        {"title": " " + "t" * 200 + " ", "content": "c" * 200_000}
    )
    assert len(request.title) == 200
    assert len(request.content) == 200_000


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": "t"},
        {"title": "", "content": "c"},
        {"title": " \n\t", "content": "c"},
        {"title": "t", "content": " \n"},
        {"title": 1, "content": "c"},
        {"title": ["t"], "content": "c"},
        {"title": "t", "content": True},
        {"title": "t" * 201, "content": "c"},
        {"title": "t", "content": "c" * 200_001},
        {"title": "t", "content": "c", "owner_id": "other"},
        {"title": "t", "content": "c", "id": "client-selected"},
    ],
    ids=[
        "missing",
        "missing-content",
        "empty",
        "blank-title",
        "blank-content",
        "number",
        "list",
        "bool",
        "long-title",
        "long-content",
        "owner",
        "id",
    ],
)
def test_invalid_document_never_reaches_save(payload):
    class SpyRepository:
        saves = 0

        def save(self, document):
            self.saves += 1

        def get(self, document_id):
            return None

    repository = SpyRepository()
    with pytest.raises(ValidationError):
        create_document(payload, DocumentService(repository), document_id="rejected")
    assert repository.saves == 0


def test_duplicate_id_preserves_original():
    repository = MemoryDocumentRepository()
    original = Document("doc-001", "first", "original")
    repository.save(original)
    with pytest.raises(DocumentAlreadyExists):
        repository.save(Document("doc-001", "second", "replacement"))
    assert repository.get("doc-001") == original


def test_stored_document_cannot_be_modified_through_alias():
    repository = MemoryDocumentRepository()
    document = Document("doc-001", "original", "text")
    repository.save(document)
    alias = repository.get("doc-001")
    with pytest.raises(FrozenInstanceError):
        alias.title = "changed"
    assert repository.get("doc-001").title == "original"


@pytest.mark.parametrize("operation", ["create", "get"])
def test_repository_failure_is_not_success(operation):
    class FailingRepository:
        def save(self, document):
            raise RepositoryError()

        def get(self, document_id):
            raise RepositoryError()

    service = DocumentService(FailingRepository())
    with pytest.raises(RepositoryError, match="^document_store_unavailable$"):
        if operation == "create":
            create_document({"title": "t", "content": "c"}, service, document_id="doc-001")
        else:
            service.get("doc-001")


def test_document_cli_runs_all_demonstrations(capsys):
    assert runner.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["synthetic"] is True
    assert result["failure"]["nothing_saved"] is True
    assert result["references"]["mutable_alias_title"] == "after"
    assert result["references"]["stored_document_frozen"] is True


def test_document_cli_sanitizes_unexpected_failure(monkeypatch, capsys):
    def fail():
        raise RuntimeError("leak-marker private SQL and token")

    monkeypatch.setattr(runner, "document_probe", fail)
    assert runner.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error_code": "day2_probe_failed",
    }


def research_payload(**overrides):
    return {
        "question": " Locate reporting requirements. ",
        "idempotency_key": "demo_1",
        **overrides,
    }


def test_research_contract_valid_inclusive_years_and_default_scope():
    request = ResearchRequest.model_validate(
        research_payload(publication_year_from=2021, publication_year_to=2021)
    )
    assert request.question == "Locate reporting requirements."
    assert request.document_ids is None
    assert request.publication_year_from == request.publication_year_to
    assert (
        ResearchRequest.model_validate(research_payload(question="q" * 2000)).question == "q" * 2000
    )


def test_research_contract_canonical_document_ids():
    ids = ["12345678-1234-4234-8234-123456789abc"]
    assert ResearchRequest.model_validate(research_payload(document_ids=ids)).document_ids == ids


def test_error_contract_can_identify_a_committed_pending_job():
    error = ErrorResponse.model_validate(
        {
            "error": {"code": "queue_unavailable", "message": "Queue is unavailable."},
            "job_id": "22345678-1234-4234-8234-123456789abc",
        }
    )
    assert error.job_id is not None
    with pytest.raises(ValidationError):
        ErrorResponse.model_validate(
            {"error": {"code": "failed", "message": "Failed."}, "stack": "private"}
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"question": " \n"},
        {"question": 123},
        {"question": "q" * 2001},
        {"document_ids": []},
        {"document_ids": ["../private"]},
        {"document_ids": ["12345678-1234-4234-8234-123456789ABC"]},
        {"document_ids": ["12345678-1234-4234-8234-123456789abc"] * 2},
        {"publication_year_from": "2021"},
        {"publication_year_from": True},
        {"publication_year_from": 2022, "publication_year_to": 2021},
        {"owner_id": "other-user"},
        {"idempotency_key": ""},
        {"idempotency_key": "invalid key"},
        {"idempotency_key": "k" * 65},
    ],
    ids=[
        "blank",
        "number",
        "long",
        "empty-scope",
        "path",
        "noncanonical",
        "duplicate",
        "coercion",
        "bool-year",
        "reversed-years",
        "owner",
        "empty-key",
        "bad-key",
        "long-key",
    ],
)
def test_research_contract_rejects_bad_input(overrides):
    with pytest.raises(ValidationError):
        ResearchRequest.model_validate(research_payload(**overrides))


def test_cache_inventory_and_wrapper_failure(tmp_path):
    root = Path(__file__).resolve().parents[1]
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "dev.sh").write_text((root / "scripts/dev.sh").read_text())
    completed = subprocess.run(
        ["bash", str(scripts / "dev.sh"), sys.executable, "-c", "raise SystemExit(9)"], check=False
    )
    assert completed.returncode == 9
    inventory = json.loads((tmp_path / ".cache/manifest.json").read_text())
    assert inventory["ruff"] == "ruff/"
    assert inventory["xdg_cache"] == "xdg/"
    assert inventory["day2_downloads_logs_reports_and_isolated_environment"] == "day2/"
    assert (tmp_path / ".cache/day2").is_dir()
    assert (tmp_path / ".cache/ruff").is_dir()
