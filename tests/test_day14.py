"""Deterministic diagnostic guardrails; these do not prove retrieval quality."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from test_day11 import hit

from app.core.config import Settings
from app.domains.med.evaluation import EvaluationRejected
from app.llm.contracts import EMBED, ModelFailure
from app.llm.ollama import OllamaAdapter
from scripts import day12, day14


@pytest.fixture
def reports(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(day12, "ROOT", root)
    monkeypatch.setattr(day14, "CACHE", root / ".cache/day14")
    return root


def test_or_query_is_generic_deduplicated_and_has_no_user_query_operators():
    assert day14.lexical_query('Registration registration: "registry" -name; DROP TABLE?') == (
        "registration OR registry OR name OR drop OR table"
    )
    assert day14.lexical_query("注册信息？") == ""


def test_fusion_uses_both_routes_deduplicates_and_preserves_dense_fallback():
    assert day14.fused_ids(["a", "b", "c"], []) == ["a", "b", "c"]
    assert day14.fused_ids(["a", "b", "c"], ["c", "b", "x"]) == ["c", "b", "a", "x"]
    assert day14.fused_ids([], []) == []


def test_fusion_equal_scores_keep_document_ordinal_tie_order():
    assert day14.fused_ids(["b", "a"], ["a", "b"]) == ["b", "a"]
    assert day14.fused_ids([], ["b", "a"]) == ["b", "a"]


def test_fusion_enforces_candidate_and_output_limits():
    dense = [str(i) for i in range(30)]
    assert len(day14.fused_ids(dense, [])) == 5
    assert day14.fused_ids(dense, ["29"]) == ["0", "29", "1", "2", "3"]


def test_fusion_rejects_duplicate_candidates_within_a_route():
    with pytest.raises(EvaluationRejected, match="^duplicate_route_candidate$"):
        day14.fused_ids(["a", "a"], [])


def test_target_measurement_requires_correct_source_page_and_text():
    target = SimpleNamespace(source_id="expected", pdf_page=3, anchor="Registration number")
    h = {**hit("Registration\n number and registry name."), "source_id": "expected", "pdf_page": 3}
    assert day14.target_matches(target, h)
    assert not day14.target_matches(target, {**h, "pdf_page": 1})
    assert not day14.target_matches(target, {**h, "source_id": "other"})
    assert not day14.target_matches(target, {**h, "content": "An unrelated original quotation."})
    case = SimpleNamespace(
        question="What registration information is requested?", evidence=[target]
    )
    measured = day14.measurements(case, [h["chunk_id"]], {h["chunk_id"]: h})
    assert measured["target_ranks"] == [1]
    assert measured["target_hit_at_5"] == measured["target_loaded"] == [True]


def test_empty_evidence_measurements_do_not_invent_hits():
    case = SimpleNamespace(question="Find eligible PRISMA sources.", evidence=[])
    assert day14.measurements(case, [], {}) == {
        "top5_chunk_ids": [],
        "target_ranks": [],
        "target_hit_at_5": [],
        "target_loaded": [],
        "context_chunk_ids": [],
    }


def test_report_namespace_is_private_and_preserves_previous_days(reports):
    day12.write_report("evaluate", {"day": 13}, cache=reports / ".cache/day13")
    day14.write_report("evaluate", {"day": 14})
    assert day12.read_report(reports / ".cache/day13/evaluate.json") == {"day": 13}
    assert day12.read_report(day14.CACHE / "evaluate.json") == {"day": 14}
    assert (day14.CACHE / "evaluate.json").stat().st_mode & 0o077 == 0
    assert not list(day14.CACHE.glob(".report-*"))


def test_report_namespace_rejects_symlink(reports, tmp_path):
    (reports / ".cache").mkdir()
    (reports / ".cache/day14").symlink_to(tmp_path)
    with pytest.raises(EvaluationRejected, match="^unsafe_report_path$"):
        day14.write_report("evaluate", {})


def test_shared_embedding_cap_survives_new_per_case_budgets(reports):
    ledger = {"reserved_embedding_calls": 0}
    for _ in range(6):
        day14.QueryBudget(ledger).reserve("embedding", 50, 0)
    assert day12.read_report(day14.CACHE / "experiment.json")["reserved_embedding_calls"] == 6
    with pytest.raises(ModelFailure, match="^call_budget_exhausted$"):
        day14.QueryBudget(ledger).reserve("embedding", 50, 0)


@pytest.mark.parametrize("count", [-1, 7, True, 0.5, None])
def test_corrupt_shared_counter_cannot_reset_budget(count):
    with pytest.raises(EvaluationRejected, match="^diagnostic_ledger_invalid$"):
        day14.QueryBudget({"reserved_embedding_calls": count})


def test_query_experiment_cannot_spend_generation_or_more_than_two_attempts_per_case(reports):
    budget = day14.QueryBudget({"reserved_embedding_calls": 0})
    with pytest.raises(ModelFailure, match="^invalid_model_input$"):
        budget.reserve("generation", 50, 512)
    budget.reserve("embedding", 50, 0)
    budget.reserve("embedding", 50, 0)
    with pytest.raises(ModelFailure, match="^call_budget_exhausted$"):
        budget.reserve("embedding", 50, 0)


def test_checkpoint_write_failure_prevents_embedding_http_request(reports, monkeypatch):
    calls = []

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": EMBED.name, "digest": EMBED.digest}]}
            )
        calls.append(request.url.path)
        return httpx.Response(503)

    monkeypatch.setattr(day14, "write_report", lambda *a: (_ for _ in ()).throw(OSError("private")))

    async def run():
        async with OllamaAdapter(Settings(), transport=httpx.MockTransport(handler)) as model:
            with pytest.raises(ModelFailure):
                await model.embed_query(
                    "registration", budget=day14.QueryBudget({"reserved_embedding_calls": 0})
                )

    asyncio.run(run())
    assert calls == []


def test_failed_http_retry_is_charged_and_cannot_cross_global_cap(reports):
    calls, ledger = [], {"reserved_embedding_calls": 5}

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": EMBED.name, "digest": EMBED.digest}]}
            )
        calls.append(request.url.path)
        return httpx.Response(503)

    async def run():
        async with OllamaAdapter(Settings(), transport=httpx.MockTransport(handler)) as model:
            with pytest.raises(ModelFailure, match="^call_budget_exhausted$"):
                await model.embed_query("registration", budget=day14.QueryBudget(ledger))

    asyncio.run(run())
    assert calls == ["/api/embed"]
    assert day12.read_report(day14.CACHE / "experiment.json")["reserved_embedding_calls"] == 6


def test_concurrent_experiment_and_replay_are_rejected_before_services(reports, monkeypatch):
    with day14.experiment_lock():
        with pytest.raises(EvaluationRejected, match="^experiment_in_use$"):
            with day14.experiment_lock():
                pytest.fail("entered twice")
    day14.write_report("experiment", {"reserved_embedding_calls": 3})
    monkeypatch.setattr(day14.day11, "model_server", lambda env: pytest.fail("model started"))
    with pytest.raises(EvaluationRejected, match="^experiment_already_recorded$"):
        asyncio.run(day14.experiment({}))
    assert day12.read_report(day14.CACHE / "experiment.json")["reserved_embedding_calls"] == 3


def test_failed_experiment_does_not_replace_charged_report(reports, monkeypatch, capsys):
    day14.write_report("experiment", {"status": "failed", "reserved_embedding_calls": 4})
    monkeypatch.setattr(day14.day6, "environment", lambda: {})
    assert day14.main(["experiment"]) == 1
    assert day12.read_report(day14.CACHE / "experiment.json")["reserved_embedding_calls"] == 4
    assert json.loads(capsys.readouterr().out)["error_code"] == "experiment_already_recorded"


@pytest.mark.parametrize("counts", [{"failed": 1}, {"pending_review": 1}, {}])
def test_quality_gate_is_nonzero_for_failed_pending_or_missing_scores(reports, monkeypatch, counts):
    monkeypatch.setattr(
        day12, "review", lambda **kw: {"status": "completed", "summary": {"quality_counts": counts}}
    )
    assert day14.main(["review", "--require-quality"]) == 2


def test_runner_redacts_unexpected_error_and_replaces_stale_review(reports, monkeypatch, capsys):
    day14.write_report("review", {"status": "completed"})
    monkeypatch.setattr(day12, "review", lambda **kw: (_ for _ in ()).throw(RuntimeError("secret")))
    assert day14.main(["review"]) == 1
    assert "secret" not in capsys.readouterr().out
    assert day12.read_report(day14.CACHE / "review.json") == {
        "status": "failed",
        "error_code": "day14_failed",
    }


def test_single_inventory_and_explicit_ignore_cover_day14():
    assert (
        '"day14_retrieval_diagnostics_evaluations_and_test_artifacts"'
        in (day14.ROOT / "scripts/dev.sh").read_text()
    )
    ignored = (day14.ROOT / ".gitignore").read_text()
    assert "/.cache/manifest.json" in ignored and "/.cache/day14/" in ignored
