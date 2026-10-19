"""Deterministic contract, numeric boundary and cache/report regressions."""

from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.retrieval.contracts import SPEC, IndexFailure, IndexSpec, RetrievalRequest
from app.services.indexing import checked_vectors


def vector(first=1):
    return (float(first),) + (0.0,) * 767


def test_index_spec_is_stable_short_and_changes_with_semantics():
    assert SPEC == IndexSpec() and len(SPEC.version) <= 32
    for key, value in {
        "model_digest": "f" * 64,
        "dimensions": 512,
        "preprocessing_version": "new",
        "normalization": "new",
        "ingest_version": "new",
    }.items():
        assert replace(SPEC, **{key: value}).version != SPEC.version


def test_valid_request_trims_question_and_preserves_unknown_year():
    result = RetrievalRequest(question="  evidence?  ", document_ids=[str(uuid4())])
    assert result.question == "evidence?" and result.publication_year_from is None
    assert result.limit == 5


@pytest.mark.parametrize(
    "update",
    [
        {"question": " "},
        {"question": "x" * 2001},
        {"limit": True},
        {"limit": 0},
        {"limit": 6},
        {"publication_year_from": True},
        {"publication_year_from": "2020"},
        {"publication_year_from": 2021, "publication_year_to": 2020},
        {"owner_id": str(uuid4())},
        {"document_ids": []},
        {"document_ids": ["doc-1"]},
        {"document_ids": ["AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"]},
        {"document_ids": [str(uuid4())] * 2},
    ],
)
def test_retrieval_request_rejects_bad_contract(update):
    with pytest.raises(ValidationError):
        RetrievalRequest.model_validate({"question": "evidence?", **update})


@pytest.mark.parametrize(
    "vectors,count",
    [
        ([], 1),
        ([vector()], 2),
        ([[1.0] * 769], 1),
        ([[0.0] * 768], 1),
        ([[float("nan")] + [0.0] * 767], 1),
        ([[float("inf")] + [0.0] * 767], 1),
        ([[True] + [0.0] * 767], 1),
        ([vector(2)], 1),
        ([[1e308] * 768], 1),
    ],
)
def test_invalid_or_nonunit_vectors_fail_safely(vectors, count):
    with pytest.raises(IndexFailure, match="^index_invalid_embedding$"):
        checked_vectors(vectors, count)


def test_unit_vector_validation_preserves_order_and_values():
    assert checked_vectors([vector(), vector(-1)], 2) == (vector(), vector(-1))


def test_day10_report_failure_replaces_stale_success_and_cleans_temporary_files(
    tmp_path, monkeypatch
):
    import json

    from scripts import day10

    monkeypatch.setattr(day10, "ROOT", tmp_path)
    monkeypatch.setattr(day10, "CACHE", tmp_path / ".cache/day10")
    day10.write_report("demo", {"status": "passed"})
    day10.write_report("demo", {"status": "failed", "error_code": "index_failed"})
    assert json.loads((day10.CACHE / "demo.json").read_text())["status"] == "failed"
    assert list(day10.CACHE.glob(".report-*")) == []


def test_day10_isolated_environment_rejects_application_database():
    from scripts.day3 import ProbeFailure
    from scripts.day10 import isolated_environment

    with pytest.raises(ProbeFailure, match="^isolated_database_required$"):
        isolated_environment(
            {"TRACEAGENT_TEST_DB_URL": "postgresql+psycopg://localhost/traceagent"}
        )


def test_day3_migrate_reports_actual_revision_instead_of_historical_constant(monkeypatch, capsys):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from scripts import day3

    @contextmanager
    def fake_database(settings):
        class Session:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def scalar(self, query):
                return "future-revision"

        yield SimpleNamespace(sessions=Session)

    monkeypatch.setattr(
        day3,
        "environment",
        lambda: {
            "TRACEAGENT_DB_URL": "postgresql+psycopg://test:test@127.0.0.1:55432/traceagent",
            "TRACEAGENT_MIGRATION_URL": "postgresql+psycopg://test:test@127.0.0.1:55432/traceagent",
        },
    )
    monkeypatch.setattr(day3, "migrate", lambda env: None)
    monkeypatch.setattr(day3, "open_database", fake_database)
    monkeypatch.setenv(
        "TRACEAGENT_DB_URL", "postgresql+psycopg://test:test@127.0.0.1:55432/traceagent"
    )
    assert day3.main(["migrate"]) == 0
    assert "future-revision" in capsys.readouterr().out
