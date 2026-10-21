"""Runner guardrails and exit status; no live dependencies or model calls."""

import json

import pytest
from test_day12 import fixture

from app.domains.med.evaluation import EvaluationRejected, evaluate_case
from scripts import day12


def test_runner_rejects_application_database_before_any_write():
    with pytest.raises(Exception, match="^isolated_database_required$"):
        day12.isolated_environment(
            {"TRACEAGENT_TEST_DB_URL": "postgresql+psycopg://u:p@127.0.0.1/traceagent"}
        )


@pytest.mark.parametrize("state", ["failed", "pending_review"])
def test_quality_gate_exit_two_does_not_mislabel_completed_collection_as_success(
    monkeypatch, state, capsys
):
    report = {"status": "completed", "summary": {"quality_counts": {state: 1}}}
    saved = []
    monkeypatch.setattr(day12, "review", lambda: report)
    monkeypatch.setattr(day12, "write_report", lambda name, value: saved.append((name, value)))
    assert day12.main(["review", "--require-quality"]) == 2
    assert saved[0][1]["summary"]["quality_counts"][state] == 1
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert day12.main(["review"]) == 0


def test_failed_run_overwrites_previous_success_with_safe_failure(monkeypatch, capsys):
    saved = []

    def failed():
        raise RuntimeError("private-password-marker")

    monkeypatch.setattr(day12, "review", failed)
    monkeypatch.setattr(day12, "write_report", lambda name, value: saved.append(value))
    assert day12.main(["review"]) == 1
    assert saved == [{"status": "failed", "error_code": "day12_failed"}]
    assert "private-password-marker" not in capsys.readouterr().out


def test_report_write_failure_returns_nonzero_safe_summary(monkeypatch, capsys):
    monkeypatch.setattr(day12, "review", lambda: {"status": "completed"})
    monkeypatch.setattr(
        day12, "write_report", lambda *args: (_ for _ in ()).throw(OSError("private-path"))
    )
    assert day12.main(["review"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error_code": "report_write_failed",
    }


def test_report_replacement_is_atomic_and_private_and_leaves_no_temp_files(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    cache = root / ".cache/day12"
    monkeypatch.setattr(day12, "ROOT", root)
    monkeypatch.setattr(day12, "CACHE", cache)
    day12.write_report("evaluate", {"status": "completed"})
    day12.write_report("evaluate", {"status": "failed", "error_code": "safe"})
    assert json.loads((cache / "evaluate.json").read_text())["status"] == "failed"
    assert (cache / "evaluate.json").stat().st_mode & 0o077 == 0
    assert not list(cache.glob(".report-*"))
    with pytest.raises(EvaluationRejected, match="^report_name_invalid$"):
        day12.write_report("../outside", {})


def test_report_symlink_and_oversize_inputs_are_rejected(tmp_path, monkeypatch):
    root = tmp_path / "project"
    cache = root / ".cache/day12"
    cache.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (cache / "evaluate.json").symlink_to(outside)
    monkeypatch.setattr(day12, "ROOT", root)
    monkeypatch.setattr(day12, "CACHE", cache)
    with pytest.raises(EvaluationRejected, match="^unsafe_report_path$"):
        day12.write_report("evaluate", {})
    with pytest.raises(EvaluationRejected, match="^report_input_invalid$"):
        day12.read_report(cache / "evaluate.json")
    assert outside.read_text() == "{}"
    large = tmp_path / "large.json"
    with large.open("wb") as handle:
        handle.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(EvaluationRejected, match="^report_input_invalid$"):
        day12.read_report(large)


def test_review_recomputes_rows_from_observations_instead_of_trusting_saved_scores(monkeypatch):
    case, observation = fixture()
    row = evaluate_case(case, observation)
    report = {
        "status": "completed",
        "dataset_lock": {},
        "workflow_version": day12.MedResearchWorkflow.version,
        "evaluator_version": day12.EVALUATOR_VERSION,
        "observations": {case.case_id: observation},
        "rows": [{**row, "quality_status": "passed"}],
    }
    from types import SimpleNamespace

    monkeypatch.setattr(day12, "CORE_CASE_IDS", (case.case_id,))
    monkeypatch.setattr(day12, "load_contracts", lambda: (None, SimpleNamespace(cases=[case]), {}))
    monkeypatch.setattr(
        day12,
        "read_report",
        lambda path: report if path.name == "evaluate.json" else {"reviews": []},
    )
    with pytest.raises(EvaluationRejected, match="^semantic_review_coverage_invalid$"):
        day12.review()
