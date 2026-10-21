"""Deterministic evaluator checks, including deliberate false-positive counterexamples."""

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domains.med.dataset import EvalCase
from app.domains.med.evaluation import (
    EvaluationRejected,
    SemanticReview,
    apply_review,
    evaluate_case,
    retrieval_metrics,
    summarize,
)
from app.domains.med.research import MedResearchWorkflow
from app.research.contracts import MESSAGES


def fixture():
    case = EvalCase(
        case_id="dev-99",
        split="dev",
        category="single_document",
        question="What does PRISMA reporting preserve?",
        conditions={"source_ids": ["synthetic"]},
        expected_behavior="answer",
        expected_source_ids=["synthetic"],
        evidence=[{"source_id": "synthetic", "pdf_page": 1, "anchor": "source traceability"}],
        critical_claims=["Preserve source traceability."],
        limitations=["Synthetic methods-only fixture."],
        relation="single_source",
        review_basis="Synthetic test, not real-model evaluation.",
    )
    chunk = {
        "chunk_id": "c1",
        "document_id": "d1",
        "source_id": "synthetic",
        "source_sha256": "a" * 64,
        "publication_year": 2021,
        "source_url": "https://example.org/paper",
        "pdf_page": 1,
        "content": "PRISMA reporting preserves source traceability.",
    }
    evidence = {
        "data_snapshot": "synthetic-snapshot",
        "index_version": "synthetic-index",
        "conditions": {
            "document_ids": ["d1"],
            "publication_year_from": None,
            "publication_year_to": None,
            "limit": 5,
        },
        "excluded_documents": [],
        "hits": [{**chunk, "distance": 0.1}],
    }
    _, selected, omitted = MedResearchWorkflow.pack_context(case.question, evidence["hits"])
    result = MedResearchWorkflow.bind_selection(
        {"evidence_id": "E1", "sentence": 1}, selected, evidence
    )
    result["context_chunk_ids"] = [h["chunk_id"] for h in selected.values()]
    result["omitted_chunk_ids"] = omitted
    request = {
        "question": case.question,
        "document_ids": ["d1"],
        "publication_year_from": None,
        "publication_year_to": None,
        "idempotency_key": "fixture",
    }
    observation = {
        "request": request,
        "outcome": {
            "status": "SUCCEEDED",
            "question": case.question,
            "conditions": {
                k: v for k, v in request.items() if k not in {"question", "idempotency_key"}
            },
            "result": result,
            "error": None,
        },
        "evidence": evidence,
        "budget": {"generation_calls": 1, "embedding_calls": 1},
        "trusted_chunks": {"c1": chunk},
        "documents": {
            "d1": {"status": "READY", "index_version": "synthetic-index", "publication_year": 2021}
        },
        "document_map": {"synthetic": "d1"},
        "seconds": 0.25,
    }
    return case, observation


def review_for(row, **overrides):
    return SemanticReview(
        **{
            **dict(
                case_id=row["case_id"],
                observation_sha256=row["observation_sha256"],
                reviewer="agent",
                relevance="passed",
                completeness="passed",
                condition_fidelity="passed",
                conflict_handling="not_applicable",
                notes="Synthetic reviewer fixture; no developer signature.",
            ),
            **overrides,
        }
    )


def test_valid_original_citation_is_pending_until_semantically_reviewed():
    case, observation = fixture()
    row = evaluate_case(case, observation)
    assert set(row["checks"].values()) == {"passed"}
    assert row["quality_status"] == "pending_review"
    assert row["semantic_status"] == "pending" and row["anchor_hits"] == 1
    assert "content" not in repr(row) and observation["trusted_chunks"]["c1"][
        "content"
    ] not in repr(row)
    graded = apply_review(row, review_for(row))
    assert graded["quality_status"] == "passed" and graded["developer_review"] == "pending"
    assert row["quality_status"] == "pending_review"  # no mutation


@pytest.mark.parametrize(
    "field,value",
    [
        ("pdf_page", 2),
        ("source_url", "https://evil.invalid/"),
        ("document_id", "other-owner"),
        ("source_sha256", "b" * 64),
        ("publication_year", 2009),
    ],
)
def test_forged_citation_metadata_fails_against_db_snapshot(field, value):
    case, observation = fixture()
    observation["outcome"]["result"]["claims"][0]["citations"][0][field] = value
    assert evaluate_case(case, observation)["checks"]["citation_binding"] == "failed"


def test_fabricated_quote_or_unrelated_original_cannot_earn_semantic_pass():
    case, observation = fixture()
    observation["outcome"]["result"]["claims"][0]["text"] = "An unsupported conclusion."
    assert evaluate_case(case, observation)["checks"]["citation_binding"] == "failed"
    case, observation = fixture()
    row = evaluate_case(case, observation)
    reviewed = apply_review(row, review_for(row, relevance="failed", completeness="failed"))
    assert reviewed["automatic_status"] == "passed" and reviewed["quality_status"] == "failed"


def test_conflicting_evidence_cannot_be_hidden_by_valid_quote_or_high_recall():
    case, observation = fixture()
    first = observation["trusted_chunks"]["c1"]
    first["content"] = "The protocol search ended on 2020-01-01."
    second = {**first, "chunk_id": "c2", "content": "The protocol search ended on 2021-01-01."}
    observation["trusted_chunks"]["c2"] = second
    observation["evidence"]["hits"] = [{**first, "distance": 0.1}, {**second, "distance": 0.2}]
    _, selected, omitted = MedResearchWorkflow.pack_context(
        case.question, observation["evidence"]["hits"]
    )
    result = MedResearchWorkflow.bind_selection(
        {"evidence_id": "E1", "sentence": 1}, selected, observation["evidence"]
    )
    result.update(
        context_chunk_ids=[hit["chunk_id"] for hit in selected.values()], omitted_chunk_ids=omitted
    )
    observation["outcome"]["result"] = result
    row = evaluate_case(case, observation)
    reviewed = apply_review(
        row,
        review_for(
            row,
            conflict_handling="failed",
            notes="Synthetic same-condition dates disagree; choosing one side omits the conflict.",
        ),
    )
    assert reviewed["quality_status"] == "failed"
    assert reviewed["metrics"]["source_recall_at_5"] == 1


@pytest.mark.parametrize(
    "target",
    [
        "result_conditions",
        "snapshot",
        "excluded",
        "context",
        "omitted",
        "used_documents",
        "request_year",
    ],
)
def test_condition_snapshot_context_and_document_tampering_is_detected(target):
    case, observation = fixture()
    result = observation["outcome"]["result"]
    if target == "result_conditions":
        result["conditions"] = {**result["conditions"], "publication_year_from": 2009}
    elif target == "snapshot":
        result["data_snapshot"] = "changed"
    elif target == "excluded":
        result["excluded_documents"] = [{"document_id": "d2", "reason": "not_ready"}]
    elif target == "context":
        result["context_chunk_ids"] = []
    elif target == "omitted":
        result["omitted_chunk_ids"] = ["unrecorded"]
    elif target == "used_documents":
        result["used_document_ids"] = ["d2"]
    else:
        observation["request"]["publication_year_from"] = 2009
    assert evaluate_case(case, observation)["quality_status"] == "failed"


def test_empty_year_scope_refuses_with_zero_inference_and_has_no_retrieval_denominator():
    case, observation = fixture()
    case = case.model_copy(
        update={
            "category": "no_answer",
            "expected_behavior": "refuse_insufficient_evidence",
            "expected_source_ids": [],
            "evidence": [],
            "relation": "not_applicable",
        }
    )
    case = case.model_copy(
        update={
            "conditions": case.conditions.model_copy(
                update={"publication_year_from": 2009, "publication_year_to": 2009}
            )
        }
    )
    for conditions in (
        observation["request"],
        observation["outcome"]["conditions"],
        observation["evidence"]["conditions"],
    ):
        conditions.update(publication_year_from=2009, publication_year_to=2009)
    observation["evidence"]["hits"] = []
    observation["evidence"]["excluded_documents"] = [
        {"document_id": "d1", "reason": "outside_publication_year"}
    ]
    observation["outcome"]["result"] = MedResearchWorkflow.refused(observation["evidence"])
    observation["budget"] = {"generation_calls": 0, "embedding_calls": 0}
    row = evaluate_case(case, observation)
    assert row["quality_status"] == "passed" and row["semantic_status"] == "not_applicable"
    summary = summarize([row])
    assert summary["retrieval_denominator"] == 0 and summary["mean_mrr_at_5"] is None
    assert summary["correct_refusals"] == 1 and summary["verified_effective_answers"] == 0
    observation["budget"]["embedding_calls"] = 1
    assert evaluate_case(case, observation)["checks"]["empty_scope_zero_inference"] == "failed"


def test_all_refusals_and_injection_refusal_do_not_get_answer_credit():
    case, observation = fixture()
    observation["outcome"]["result"] = {
        **MedResearchWorkflow.refused(observation["evidence"]),
        "context_chunk_ids": ["c1"],
        "omitted_chunk_ids": [],
    }
    case = case.model_copy(update={"expected_behavior": "ignore_injection"})
    row = evaluate_case(case, observation)
    assert row["checks"]["citation_binding"] == "passed"
    assert row["checks"]["expected_behavior"] == "failed"
    assert summarize([row])["verified_effective_answers"] == 0


def test_top_five_source_recall_deduplicates_and_mrr_keeps_chunk_rank():
    hits = [{"source_id": x} for x in ("noise", "a", "a", "b", "noise", "c")]
    assert retrieval_metrics(["a", "b", "c"], hits) == {
        "source_recall_at_5": 2 / 3,
        "mrr_at_5": 0.5,
    }
    assert retrieval_metrics(["c"], hits) == {"source_recall_at_5": 0, "mrr_at_5": 0}
    assert retrieval_metrics([], hits) == {"source_recall_at_5": None, "mrr_at_5": None}


def test_safe_technical_failure_is_recorded_as_quality_failure_not_refusal():
    case, observation = fixture()
    observation["outcome"].update(
        status="FAILED",
        result=None,
        error={"code": "research_timeout", "message": MESSAGES["research_timeout"]},
    )
    row = evaluate_case(case, observation)
    assert row["quality_status"] == "failed" and row["actual_kind"] is None
    assert row["checks"]["safe_failure_contract"] == "passed"
    observation["outcome"]["error"]["message"] = "private-provider-marker"
    assert evaluate_case(case, observation)["checks"]["safe_failure_contract"] == "failed"


@pytest.mark.parametrize("mutation", ["not_ready", "wrong_exclusion", "medical_reason"])
def test_empty_refusal_cannot_hide_unready_corpus_wrong_filter_or_wrong_reason(mutation):
    case, observation = fixture()
    case = case.model_copy(
        update={
            "expected_behavior": "refuse_insufficient_evidence",
            "expected_source_ids": [],
            "evidence": [],
        }
    )
    observation["evidence"]["hits"] = []
    observation["budget"] = {"generation_calls": 0, "embedding_calls": 0}
    observation["outcome"]["result"] = MedResearchWorkflow.refused(observation["evidence"])
    if mutation == "not_ready":
        observation["documents"]["d1"]["status"] = "PARSED"
    elif mutation == "wrong_exclusion":
        excluded = [{"document_id": "d1", "reason": "not_ready"}]
        observation["evidence"]["excluded_documents"] = excluded
        observation["outcome"]["result"]["excluded_documents"] = excluded
    else:
        observation["outcome"]["result"]["reason"] = "medical_request"
    assert evaluate_case(case, observation)["quality_status"] == "failed"


@pytest.mark.parametrize("mutation", ["question", "budget", "evidence"])
def test_reviews_are_bound_to_case_request_evidence_result_and_budget(mutation):
    case, observation = fixture()
    row = evaluate_case(case, observation)
    review = review_for(row)
    changed = deepcopy(observation)
    if mutation == "question":
        changed["request"]["question"] = "different PRISMA question"
    elif mutation == "budget":
        changed["budget"]["generation_calls"] = 2
    else:
        changed["evidence"]["data_snapshot"] = "different"
    with pytest.raises(EvaluationRejected, match="^semantic_review_stale$"):
        apply_review(evaluate_case(case, changed), review)


def test_answer_review_requires_relevance_completeness_and_conditions():
    case, observation = fixture()
    row = evaluate_case(case, observation)
    with pytest.raises(EvaluationRejected, match="^answer_review_incomplete$"):
        apply_review(row, review_for(row, completeness="not_applicable"))
    with pytest.raises(ValidationError):
        review_for(row, reviewer="automatic-human-signature")


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), -1, True])
def test_invalid_measurements_fail_with_safe_code(seconds):
    case, observation = fixture()
    observation["seconds"] = seconds
    with pytest.raises(EvaluationRejected, match="^observation_invalid$"):
        evaluate_case(case, observation)


def test_acceptance_cases_duplicate_results_and_missing_observations_are_rejected():
    case, observation = fixture()
    with pytest.raises(EvaluationRejected, match="^development_split_required$"):
        evaluate_case(case.model_copy(update={"split": "acceptance"}), observation)
    with pytest.raises(EvaluationRejected, match="^observation_invalid$"):
        evaluate_case(case, {})
    row = evaluate_case(case, observation)
    for rows in ([], [row, row]):
        with pytest.raises(EvaluationRejected, match="^unique_nonempty_cases_required$"):
            summarize(rows)


def test_single_inventory_and_day12_artifacts_are_explicitly_ignored():
    root = Path(__file__).resolve().parents[1]
    assert (
        '"day12_evaluation_reports_previews_and_test_artifacts"'
        in (root / "scripts/dev.sh").read_text()
    )
    ignored = (root / ".gitignore").read_text()
    assert "/.cache/manifest.json" in ignored and "/.cache/day12/" in ignored
