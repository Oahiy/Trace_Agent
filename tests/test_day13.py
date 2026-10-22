"""Deterministic binding/runner regressions; selection quality needs real inference."""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from test_day11 import evidence, hit

from app.core.config import Settings
from app.domains.med.evaluation import EvaluationRejected
from app.domains.med.research import EvidenceSelection, bind_selection, pack_context
from app.llm.budget import INPUT_LIMIT
from app.llm.contracts import CHAT, Message, ModelFailure
from app.llm.ollama import OllamaAdapter, generation_input
from scripts import day12, day13


@pytest.fixture
def reports(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(day12, "ROOT", root)
    monkeypatch.setattr(day12, "CACHE", root / ".cache/day12")
    monkeypatch.setattr(day13, "CACHE", root / ".cache/day13")
    return root


def test_selected_complete_answer_keeps_exact_words_page_and_conditions():
    raw = "ency and accountability [15]. The statement contains a checklist and a flow diagram."
    h, scope = hit(raw), evidence()
    scope["conditions"] = {"publication_year_from": 2009, "document_ids": [h["document_id"]]}
    result = bind_selection({"evidence_id": "E1", "sentence": 2}, {"E1": h}, scope)
    assert result["kind"] == "answer"
    claim = result["claims"][0]
    assert claim["text"] == "The statement contains a checklist and a flow diagram."
    assert claim["text"] == claim["citations"][0]["support_fragment"]
    assert claim["text"] in raw
    assert claim["citations"][0]["pdf_page"] == h["pdf_page"]
    assert result["conditions"] == scope["conditions"]


def test_packing_and_inference_share_budget_after_instruction_change():
    hits = [hit("A complete reporting sentence. " * 30), hit("Another sentence. " * 100)]
    messages, selected, omitted = pack_context("What does the PRISMA statement contain?", hits)
    assert generation_input(messages, EvidenceSelection)[2] <= INPUT_LIMIT
    assert selected and omitted
    assert {h["chunk_id"] for h in selected.values()} | set(omitted) == {
        h["chunk_id"] for h in hits
    }
    payload = json.loads(messages[-1].content)
    for key, h in selected.items():
        assert "".join(payload["evidence"][key].values()) == h["content"].rstrip()


def test_new_namespace_does_not_replace_historical_report(reports):
    day12.write_report("evaluate", {"day": 12})
    day13.write_report("evaluate", {"day": 13})
    assert day12.read_report(reports / ".cache/day12/evaluate.json") == {"day": 12}
    assert day12.read_report(reports / ".cache/day13/evaluate.json") == {"day": 13}
    assert (reports / ".cache/day13/evaluate.json").stat().st_mode & 0o077 == 0
    assert list((reports / ".cache/day13").glob(".report-*")) == []


def test_report_directory_rejects_other_names_and_symlink_ancestors(reports, tmp_path):
    with pytest.raises(EvaluationRejected, match="^unsafe_report_path$"):
        day12.write_report("evaluate", {}, cache=reports / ".cache/day99")
    target = tmp_path / "outside"
    target.mkdir()
    (reports / ".cache").symlink_to(target)
    with pytest.raises(EvaluationRejected, match="^unsafe_report_path$"):
        day13.write_report("evaluate", {})
    assert not list(target.iterdir())


def test_shared_cap_survives_new_per_case_budgets_and_retains_reservations(reports):
    ledger = {"reserved_generation_calls": 0}
    for _ in range(6):
        budget = day13.DiagnosticBudget(ledger)
        budget.reserve("generation", 20, 512)
        assert day12.read_report(day13.CACHE / "experiment.json") == ledger
    with pytest.raises(ModelFailure, match="^call_budget_exhausted$"):
        day13.DiagnosticBudget(ledger).reserve("generation", 20, 512)
    assert ledger["reserved_generation_calls"] == 6
    with pytest.raises(ModelFailure, match="^invalid_model_input$"):
        day13.DiagnosticBudget(ledger).reserve("embedding", 20, 0)


def test_failed_http_retry_is_reserved_in_same_global_ledger(reports):
    calls, ledger = [], {"reserved_generation_calls": 5}

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": CHAT.name, "digest": CHAT.digest}]}
            )
        calls.append(request.url.path)
        return httpx.Response(503)

    async def run():
        async with OllamaAdapter(Settings(), transport=httpx.MockTransport(handler)) as model:
            budget = day13.DiagnosticBudget(ledger)
            with pytest.raises(ModelFailure, match="^call_budget_exhausted$"):
                await model.generate(
                    [Message(role="user", content="Select evidence.")],
                    EvidenceSelection,
                    budget=budget,
                    max_output_tokens=512,
                )
            assert len(budget.records) == 1 and budget.records[0].status == "failed"

    asyncio.run(run())
    assert calls == ["/api/chat"]
    assert day12.read_report(day13.CACHE / "experiment.json")["reserved_generation_calls"] == 6


def test_checkpoint_failure_prevents_http_generation(reports, monkeypatch):
    calls = []

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": CHAT.name, "digest": CHAT.digest}]}
            )
        calls.append(request.url.path)
        return httpx.Response(503)

    monkeypatch.setattr(day13, "write_report", lambda *a: (_ for _ in ()).throw(OSError("private")))

    async def run():
        async with OllamaAdapter(Settings(), transport=httpx.MockTransport(handler)) as model:
            with pytest.raises(ModelFailure, match="^model_failed$"):
                await model.generate(
                    [Message(role="user", content="Select evidence.")],
                    EvidenceSelection,
                    budget=day13.DiagnosticBudget({"reserved_generation_calls": 0}),
                    max_output_tokens=512,
                )

    asyncio.run(run())
    assert calls == []


def test_concurrent_experiment_rejected_before_any_call(reports):
    with day13.experiment_lock():
        with pytest.raises(EvaluationRejected, match="^experiment_in_use$"):
            with day13.experiment_lock():
                pytest.fail("lock entered twice")


@pytest.mark.parametrize("state", ["failed", "pending_review"])
def test_quality_failures_and_pending_are_exit_two(reports, monkeypatch, state):
    monkeypatch.setattr(
        day13, "review", lambda: {"status": "completed", "summary": {"quality_counts": {state: 1}}}
    )
    assert day13.main(["review", "--require-quality"]) == 2
    assert day13.main(["review"]) == 0


def test_failed_review_redacts_exception_and_replaces_stale_success(reports, monkeypatch, capsys):
    day13.write_report("review", {"status": "completed"})
    monkeypatch.setattr(day13, "review", lambda: (_ for _ in ()).throw(RuntimeError("secret")))
    assert day13.main(["review"]) == 1
    assert "secret" not in capsys.readouterr().out
    assert day12.read_report(day13.CACHE / "review.json") == {
        "status": "failed",
        "error_code": "day13_failed",
    }


def test_failed_experiment_cannot_erase_consumed_budget(reports, monkeypatch):
    ledger = {"status": "collecting", "reserved_generation_calls": 6}
    day13.write_report("experiment", ledger)
    monkeypatch.setattr(day13.day6, "environment", lambda: {})

    async def failed(env):
        raise EvaluationRejected("candidate_limit_exhausted")

    monkeypatch.setattr(day13, "experiment", failed)
    assert day13.main(["experiment"]) == 1
    assert day12.read_report(day13.CACHE / "experiment.json") == ledger


def test_diagnostic_budget_does_not_hide_model_failures(reports):
    ledger = {"reserved_generation_calls": 0}
    budget = day13.DiagnosticBudget(ledger)
    budget.reserve("generation", 20, 512)
    budget.reserve("generation", 20, 512)
    with pytest.raises(ModelFailure, match="^call_budget_exhausted$"):
        budget.reserve("generation", 20, 512)
    assert ledger["reserved_generation_calls"] == 2


@pytest.mark.parametrize("count", [-1, 7, True, 1.5, None])
def test_invalid_ledger_cannot_reset_cap(count):
    with pytest.raises(EvaluationRejected, match="^diagnostic_ledger_invalid$"):
        day13.DiagnosticBudget({"reserved_generation_calls": count})


@pytest.mark.parametrize(
    "count,error", [(1, "candidate_already_recorded"), (2, "candidate_limit_exhausted")]
)
def test_experiment_rejects_replay_and_third_candidate_before_model(
    reports, monkeypatch, count, error
):
    from types import SimpleNamespace

    h = hit()
    observation = {
        "evidence": {"hits": [h]},
        "outcome": {"result": {"context_chunk_ids": [h["chunk_id"]]}},
    }
    baseline = {"observations": {"dev-01": observation}}
    case = SimpleNamespace(case_id="dev-01", question="What is PRISMA?")
    wire = {"E1": day13.numbered_sentences(h["content"])}
    prompt_sha = day13.canonical_digest(day13.messages_for(case.question, wire)[0].content)
    ledger = {
        "baseline_sha256": day13.canonical_digest(baseline),
        "context_sha256": day13.canonical_digest(wire),
        "reserved_generation_calls": 3,
        "experiments": [{"prompt_sha256": prompt_sha}] * count,
    }
    day13.write_report("experiment", ledger)
    monkeypatch.setattr(day13, "load_baseline", lambda: (baseline, case))
    monkeypatch.setattr(day13.day11, "model_server", lambda env: pytest.fail("model started"))
    with pytest.raises(EvaluationRejected, match="^" + error + "$"):
        asyncio.run(day13.experiment({}))
    assert day12.read_report(day13.CACHE / "experiment.json") == ledger


def test_report_failure_returns_safe_nonzero_status(reports, monkeypatch, capsys):
    monkeypatch.setattr(day13, "review", lambda: {"status": "completed"})
    monkeypatch.setattr(day13, "write_report", lambda *a: (_ for _ in ()).throw(OSError("secret")))
    assert day13.main(["review"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error_code": "report_write_failed",
    }


def test_single_inventory_contains_all_day13_artifacts():
    root = day13.ROOT
    assert (
        '"day13_selection_experiments_evaluations_and_test_artifacts"'
        in (root / "scripts/dev.sh").read_text()
    )
    ignored = (root / ".gitignore").read_text()
    assert "/.cache/manifest.json" in ignored and "/.cache/day13/" in ignored


def test_baseline_drift_rejected_before_model_start(monkeypatch):
    from types import SimpleNamespace

    class Case:
        case_id = "dev-01"

        def model_dump(self):
            return {"case_id": self.case_id}

    case = Case()
    baseline = {"status": "completed", "dataset_lock": {}, "observations": {"dev-01": {}}}
    published = {"real_evaluation": {"rows": [{"case_id": "dev-01", "observation_sha256": "bad"}]}}
    original = deepcopy(baseline)
    monkeypatch.setattr(day13, "load_contracts", lambda: (None, SimpleNamespace(cases=[case]), {}))
    monkeypatch.setattr(
        day12, "read_report", lambda path: baseline if path.name == "baseline.json" else published
    )
    with pytest.raises(EvaluationRejected, match="^baseline_observation_changed$"):
        day13.load_baseline()
    assert baseline == original
