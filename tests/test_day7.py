"""Acceptance assertions must catch incorrect evidence, even when HTTP succeeds."""

import json
from pathlib import Path

import pytest

from scripts import day3, day7


def chunk(**changes):
    return {
        "id": "chunk-1",
        "ordinal": 0,
        "content": "中文\r\n",
        "pdf_page": None,
        "char_start": 0,
        "char_end": 4,
        "ingest_version": "ingest-v1",
        **changes,
    }


def test_txt_locations_use_decoded_characters_and_preserve_crlf():
    decoded = b"\xef\xbb\xbf".decode("utf-8-sig") + "中文\r\n尾"
    day7.validate_locations([chunk()], decoded_text=decoded)


def test_pdf_locations_use_physical_page_with_blank_page_preserved():
    day7.validate_locations(
        [chunk(content="Evidence", pdf_page=2, char_start=None, char_end=None)],
        pages=[{"text": ""}, {"text": "Evidence on second physical page"}],
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"char_end": 8},
        {"char_start": True},
        {"content": "wrong"},
        {"ordinal": 1},
        {"ingest_version": "different"},
        {"pdf_page": 1},
    ],
)
def test_incorrect_txt_evidence_is_rejected(changes):
    with pytest.raises(day3.ProbeFailure):
        day7.validate_locations([chunk(**changes)], decoded_text="中文\r\n尾")


@pytest.mark.parametrize("page,content", [(1, "Evidence"), (3, "Evidence"), (2, "invented")])
def test_wrong_pdf_page_or_content_is_rejected(page, content):
    with pytest.raises(day3.ProbeFailure):
        day7.validate_locations(
            [chunk(content=content, pdf_page=page, char_start=None, char_end=None)],
            pages=[{"text": ""}, {"text": "Evidence"}],
        )


def test_empty_chunks_cannot_pass_acceptance():
    with pytest.raises(day3.ProbeFailure, match="chunks_missing"):
        day7.validate_locations([], decoded_text="text")


def test_application_snapshot_refuses_test_database_before_connection():
    with pytest.raises(day3.ProbeFailure, match="app_scope_required"):
        day7.application_snapshot({"TRACEAGENT_DB_URL": "postgresql://localhost/traceagent_test"})


def test_failed_acceptance_overwrites_stale_success_without_leaking_exception(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(day7, "CACHE", tmp_path)
    (tmp_path / "evidence.json").write_text('{"status":"passed"}')

    def fail():
        raise RuntimeError("password=secret-marker")

    monkeypatch.setattr(day7.day6, "environment", fail)
    assert day7.main() == 1
    result = json.loads((tmp_path / "evidence.json").read_text())
    assert result == {"status": "failed", "error_code": "day7_acceptance_failed"}


def test_day7_cache_uses_existing_single_inventory_and_explicit_ignore():
    root = Path(__file__).resolve().parents[1]
    manager = (root / "scripts/dev.sh").read_text()
    assert '"day7_acceptance_logs_reports_and_snapshots": "day7/"' in manager
    assert '"path": "day7/uploads/", "database": "traceagent_test"' in manager
    ignored = (root / ".gitignore").read_text().splitlines()
    assert "/.cache/manifest.json" in ignored and "/.cache/day7/" in ignored
