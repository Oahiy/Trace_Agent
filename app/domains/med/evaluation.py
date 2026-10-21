"""Offline MedEvidence evaluation. Structural checks never imply semantic support."""

import math
from collections import Counter
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domains.med.dataset import EvalCase, canonical_digest
from app.domains.med.research import MedResearchWorkflow, pack_context
from app.research.contracts import MESSAGES

CORE_CASE_IDS = ("dev-01", "dev-02", "dev-03", "dev-05", "dev-08", "dev-10")
EVALUATOR_VERSION = "med-eval-v1"


class EvaluationRejected(Exception):
    """Fixed codes only; no input, provider response or exception text."""


class SemanticReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    case_id: str = Field(pattern=r"^dev-\d{2}$")
    observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: Literal["agent", "developer"]
    relevance: Literal["passed", "failed", "not_applicable"]
    completeness: Literal["passed", "failed", "not_applicable"]
    condition_fidelity: Literal["passed", "failed", "not_applicable"]
    conflict_handling: Literal["passed", "failed", "not_applicable"]
    notes: str = Field(min_length=1, max_length=1500)


def normalized(text):
    return "".join(text.split()).casefold()


def retrieval_metrics(expected_sources, hits):
    """Top five chunks, distinct source recall; MRR uses the first relevant chunk rank."""
    expected = set(expected_sources)
    if not expected:
        return {"source_recall_at_5": None, "mrr_at_5": None}
    ranked = [hit["source_id"] for hit in hits[:5]]
    first = next((i for i, source in enumerate(ranked, 1) if source in expected), None)
    return {
        "source_recall_at_5": len(expected.intersection(ranked)) / len(expected),
        "mrr_at_5": 1 / first if first else 0,
    }


def evaluate_case(case: EvalCase, observation: dict) -> dict:
    """Compare captured execution with the prewritten case and DB-derived trusted chunks.

    Raw observations stay in ignored cache. Returned rows contain no full excerpts/prompts.
    A successful structural result remains pending until a matching semantic review exists.
    """
    if case.split != "dev":
        raise EvaluationRejected("development_split_required")
    try:
        return _evaluate(case, observation)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        raise EvaluationRejected("observation_invalid") from None


def _evaluate(case, observation):
    request, outcome = observation["request"], observation["outcome"]
    evidence, budget = observation["evidence"], observation["budget"]
    trusted, mapping = observation["trusted_chunks"], observation["document_map"]
    documents = observation["documents"]
    seconds = observation["seconds"]
    if type(seconds) not in {float, int} or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("invalid_seconds")
    for key in ("generation_calls", "embedding_calls"):
        if type(budget[key]) is not int or budget[key] < 0:
            raise ValueError("invalid_usage")
    checks = {}

    def check(name, value):
        checks[name] = "passed" if value else "failed"

    expected_ids = sorted(mapping[source] for source in case.conditions.source_ids)
    years = case.conditions.model_dump(exclude={"source_ids"})
    expected_conditions = {"document_ids": expected_ids, **years}
    check(
        "request_contract",
        request["question"] == case.question
        and sorted(request["document_ids"]) == expected_ids
        and all(request[k] == v for k, v in years.items())
        and outcome["question"] == case.question
        and {**outcome["conditions"], "document_ids": sorted(outcome["conditions"]["document_ids"])}
        == expected_conditions,
    )
    check("execution_succeeded", outcome["status"] == "SUCCEEDED")
    check("budget_limits", budget["generation_calls"] <= 2 and budget["embedding_calls"] <= 2)
    result = outcome["result"]
    if outcome["status"] == "FAILED":
        error = outcome["error"]
        check(
            "safe_failure_contract",
            result is None
            and set(error) == {"code", "message"}
            and error["code"] in MESSAGES
            and error["message"] == MESSAGES[error["code"]],
        )
    else:
        check("terminal_contract", outcome["status"] == "SUCCEEDED" and outcome["error"] is None)
    if result is None:
        hits = evidence["hits"] if evidence else []
        semantic = "not_applicable"
        check("expected_behavior", False)
    else:
        hits = evidence["hits"]
        actual_scope = {
            **evidence["conditions"],
            "document_ids": sorted(evidence["conditions"]["document_ids"]),
        }
        check("evidence_conditions", actual_scope == {**expected_conditions, "limit": 5})
        check(
            "scope_ready",
            all(
                documents[identifier]["status"] == "READY"
                and documents[identifier]["index_version"] == evidence["index_version"]
                for identifier in expected_ids
            ),
        )
        expected_exclusions = []
        for identifier in expected_ids:
            year = documents[identifier]["publication_year"]
            filtered = (
                years["publication_year_from"] is not None
                or years["publication_year_to"] is not None
            )
            reason = (
                "unknown_publication_year"
                if filtered and year is None
                else "outside_publication_year"
                if filtered
                and (
                    (
                        years["publication_year_from"] is not None
                        and year < years["publication_year_from"]
                    )
                    or (
                        years["publication_year_to"] is not None
                        and year > years["publication_year_to"]
                    )
                )
                else None
            )
            if reason:
                expected_exclusions.append({"document_id": identifier, "reason": reason})
        check(
            "filter_exclusions",
            sorted(evidence["excluded_documents"], key=lambda item: item["document_id"])
            == expected_exclusions,
        )
        check(
            "result_provenance",
            result["conditions"] == evidence["conditions"]
            and result["excluded_documents"] == evidence["excluded_documents"]
            and result["data_snapshot"] == evidence["data_snapshot"]
            and result["index_version"] == evidence["index_version"]
            and result["workflow_version"] == MedResearchWorkflow.version
            and bool(result["limitations"]),
        )
        check(
            "retrieved_provenance",
            len(hits) <= 5
            and all(
                hit["document_id"] in expected_ids
                and {k: v for k, v in hit.items() if k != "distance"} == trusted[hit["chunk_id"]]
                for hit in hits
            ),
        )
        claims = result["claims"]
        answering = result["kind"] == "answer"
        check(
            "result_contract",
            (answering and len(claims) == 1)
            or (
                result["kind"] == "refused"
                and claims == []
                and result["used_document_ids"] == []
                and result["reason"] in {"insufficient_evidence", "medical_request"}
            ),
        )
        expected_answer = case.expected_behavior in {"answer", "ignore_injection"}
        check(
            "expected_behavior",
            answering == expected_answer
            and (expected_answer or result.get("reason") == "insufficient_evidence"),
        )
        semantic = "pending" if answering else "not_applicable"
        if hits:
            _, selected, omitted = pack_context(case.question, hits)
            context_ids = [h["chunk_id"] for h in selected.values()]
            check(
                "context_accounting",
                result.get("context_chunk_ids") == context_ids
                and result.get("omitted_chunk_ids") == omitted,
            )
        else:
            context_ids = []
            check(
                "empty_scope_zero_inference",
                budget["generation_calls"] == budget["embedding_calls"] == 0,
            )
        cited_ids, citation_valid = [], True
        for claim in claims:
            citation_valid = citation_valid and bool(claim["citations"])
            for citation in claim["citations"]:
                chunk = trusted.get(citation["chunk_id"], {})
                fragment = citation["support_fragment"]
                citation_valid = citation_valid and (
                    citation["chunk_id"] in context_ids
                    and bool(fragment)
                    and claim["text"] == fragment
                    and fragment in chunk.get("content", "")
                    and {k: v for k, v in citation.items() if k != "support_fragment"}
                    == {k: v for k, v in chunk.items() if k != "content"}
                )
                cited_ids.append(citation["document_id"])
        check(
            "citation_binding",
            citation_valid and sorted(set(cited_ids)) == sorted(result["used_document_ids"]),
        )
    metrics = retrieval_metrics(case.expected_source_ids, hits)
    # Anchor presence is a diagnostic, never a semantic-support verdict.
    anchor_hits = sum(
        any(
            h["source_id"] == anchor.source_id
            and h["pdf_page"] == anchor.pdf_page
            and normalized(anchor.anchor) in normalized(h["content"])
            for h in hits
        )
        for anchor in case.evidence
    )
    return {
        "case_id": case.case_id,
        "category": case.category,
        "expected_behavior": case.expected_behavior,
        "actual_status": outcome["status"],
        "actual_kind": result["kind"] if result else None,
        "observation_sha256": canonical_digest(
            {"case": case.model_dump(), "observation": observation}
        ),
        "checks": checks,
        "automatic_status": "failed" if "failed" in checks.values() else "passed",
        "semantic_status": semantic,
        "developer_review": "pending",
        "metrics": metrics,
        "anchor_hits": anchor_hits,
        "expected_anchor_count": len(case.evidence),
        "seconds": round(seconds, 4),
        "usage": {k: budget[k] for k in ("generation_calls", "embedding_calls")},
        "quality_status": "failed"
        if "failed" in checks.values()
        else "pending_review"
        if semantic == "pending"
        else "passed",
    }


def apply_review(row: dict, review: SemanticReview) -> dict:
    if row["case_id"] != review.case_id or row["observation_sha256"] != review.observation_sha256:
        raise EvaluationRejected("semantic_review_stale")
    if row["semantic_status"] != "pending":
        raise EvaluationRejected("semantic_review_not_applicable")
    values = [
        review.relevance,
        review.completeness,
        review.condition_fidelity,
        review.conflict_handling,
    ]
    if any(value == "not_applicable" for value in values[:3]):
        raise EvaluationRejected("answer_review_incomplete")
    result = deepcopy(row)
    result["semantic_status"] = "failed" if "failed" in values else "passed"
    result["semantic_review"] = review.model_dump()
    if review.reviewer == "developer":
        result["developer_review"] = result["semantic_status"]
    result["quality_status"] = (
        "passed"
        if result["automatic_status"] == result["semantic_status"] == "passed"
        else "failed"
    )
    return result


def summarize(rows):
    if not rows or len({r["case_id"] for r in rows}) != len(rows):
        raise EvaluationRejected("unique_nonempty_cases_required")
    recall = [
        r["metrics"]["source_recall_at_5"]
        for r in rows
        if r["metrics"]["source_recall_at_5"] is not None
    ]
    mrr = [r["metrics"]["mrr_at_5"] for r in rows if r["metrics"]["mrr_at_5"] is not None]
    answerable = [r for r in rows if r["expected_behavior"] in {"answer", "ignore_injection"}]
    unanswerable = [r for r in rows if r["expected_behavior"] == "refuse_insufficient_evidence"]
    return {
        "cases": len(rows),
        "quality_counts": dict(Counter(r["quality_status"] for r in rows)),
        "retrieval_denominator": len(recall),
        "mean_source_recall_at_5": sum(recall) / len(recall) if recall else None,
        "mean_mrr_at_5": sum(mrr) / len(mrr) if mrr else None,
        "answerable_denominator": len(answerable),
        "verified_effective_answers": sum(r["quality_status"] == "passed" for r in answerable),
        "unanswerable_denominator": len(unanswerable),
        "correct_refusals": sum(r["quality_status"] == "passed" for r in unanswerable),
        "developer_reviews_pending": sum(r["developer_review"] == "pending" for r in rows),
        "all_developer_reviews_complete": all(r["developer_review"] != "pending" for r in rows),
    }
