"""One frozen, read-only retrieval comparison; no automatic production adoption."""

import argparse
import asyncio
import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime
from time import monotonic
from zoneinfo import ZoneInfo

from pypdf import PdfReader
from sqlalchemy import func, literal_column, select

from app.core.config import Settings
from app.db.config import DatabaseSettings
from app.db.embeddings import EmbeddingRepository
from app.db.models import Chunk, ChunkEmbedding, Document, User
from app.db.session import open_database
from app.domains.med.dataset import DatasetRejected, canonical_digest
from app.domains.med.evaluation import EvaluationRejected
from app.domains.med.research import MedResearchWorkflow, pack_context
from app.llm.budget import CallBudget
from app.llm.contracts import EMBED, ModelFailure
from app.llm.ollama import OllamaAdapter
from app.retrieval.contracts import SPEC, RetrievalRequest
from app.services.auth import AuthService
from app.services.indexing import checked_vectors
from app.services.retrieval import RetrievalService
from scripts import day3, day6, day11, day12
from scripts.day8 import confined_path, load_contracts

ROOT = day12.ROOT
CACHE = ROOT / ".cache/day14"
CONFIG = {
    "version": "retrieval-diagnostic-v1",
    "case_ids": ["dev-01", "dev-02", "dev-03", "dev-08"],
    "candidate_limit_per_route": 20,
    "final_limit": 5,
    "rrf_constant": 60,
    "route_weights": [1, 1],
    "lexical_query": "ascii-alphanumeric-terms-OR-postgres-english-v1",
    "max_embedding_attempts": 6,
    "generation_attempts": 0,
    "max_scope_chunks": 2048,
}


def write_report(name, value):
    day12.write_report(name, value, cache=CACHE)


@contextmanager
def experiment_lock():
    cache = day12.report_directory(CACHE)
    fd = os.open(cache / "experiment.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EvaluationRejected("experiment_in_use") from None
        yield
    finally:
        os.close(fd)


def lexical_query(question):
    """No labels, medical vocabulary, model rewriting or user-controlled SQL syntax."""
    return " OR ".join(dict.fromkeys(re.findall(r"[a-z0-9]+", question.casefold())))


def evidence_query(owner, document_ids, request):
    """Every route filters before ranking, using the production eligibility rules."""
    query = (
        select(Chunk, Document)
        .join(ChunkEmbedding, ChunkEmbedding.chunk_id == Chunk.id)
        .join(Document, Document.id == Chunk.document_id)
        .join(User, User.id == Document.owner_id)
        .where(
            User.id == owner,
            User.active.is_(True),
            Document.owner_id == owner,
            Document.id.in_(document_ids),
            Document.status == "READY",
            Document.index_version == SPEC.version,
            Chunk.ingest_version == SPEC.ingest_version,
            ChunkEmbedding.index_version == SPEC.version,
        )
    )
    if request.publication_year_from is not None:
        query = query.where(Document.publication_year >= request.publication_year_from)
    if request.publication_year_to is not None:
        query = query.where(Document.publication_year <= request.publication_year_to)
    return query


def ranked_rows(session, owner, document_ids, request, vector):
    query = evidence_query(owner, document_ids, request)
    distance = ChunkEmbedding.embedding.cosine_distance(list(vector)).label("distance")
    stable = (Document.id, Chunk.ordinal, Chunk.id)
    started = monotonic()
    dense = list(
        session.execute(
            query.add_columns(distance)
            .order_by(distance, *stable)
            .limit(CONFIG["max_scope_chunks"] + 1)
        )
    )
    dense_seconds = monotonic() - started
    day12.require(len(dense) <= CONFIG["max_scope_chunks"], "diagnostic_scope_too_large")
    # This independent SQL path must reproduce the public baseline before comparison.
    production = EmbeddingRepository(session).search(owner, vector, document_ids, request)
    day12.require(
        [row[0].id for row in dense[: request.limit]] == [row[0].id for row in production],
        "diagnostic_baseline_mismatch",
    )
    english = literal_column("'english'::regconfig")
    terms = lexical_query(request.question)
    started = monotonic()
    if terms:
        document = func.to_tsvector(english, Chunk.content)
        search = func.websearch_to_tsquery(english, terms)
        rank = func.ts_rank_cd(document, search).label("lexical_rank")
        lexical = list(
            session.execute(
                query.add_columns(rank)
                .where(document.op("@@")(search))
                .order_by(rank.desc(), *stable)
                .limit(CONFIG["max_scope_chunks"] + 1)
            )
        )
    else:
        lexical = []
    day12.require(len(lexical) <= CONFIG["max_scope_chunks"], "diagnostic_scope_too_large")
    return (
        dense,
        lexical,
        {
            "dense_seconds": dense_seconds,
            "lexical_seconds": monotonic() - started,
        },
    )


def fused_ids(dense, lexical):
    """Fuse bounded rank lists; missing routes contribute zero, ties are stable."""
    scores = {}
    for route in (dense, lexical):
        seen = set()
        for rank, identifier in enumerate(route[: CONFIG["candidate_limit_per_route"]], 1):
            day12.require(identifier not in seen, "duplicate_route_candidate")
            seen.add(identifier)
            scores[identifier] = scores.get(identifier, 0) + 1 / (CONFIG["rrf_constant"] + rank)
    # The dense list already carries the documented document/ordinal/chunk tie order.
    stable = {identifier: rank for rank, identifier in enumerate(dict.fromkeys([*dense, *lexical]))}
    return sorted(scores, key=lambda key: (-scores[key], stable[key]))[: CONFIG["final_limit"]]


def normalized(value):
    return "".join(value.casefold().split())


def target_matches(target, hit):
    return (
        target.source_id == hit["source_id"]
        and target.pdf_page == hit["pdf_page"]
        and normalized(target.anchor) in normalized(hit["content"])
    )


def measurements(case, ids, trusted):
    hits = [trusted[key] for key in ids]
    selected = pack_context(case.question, hits[:5])[1]
    return {
        "top5_chunk_ids": ids[:5],
        "target_ranks": [
            next((rank for rank, hit in enumerate(hits, 1) if target_matches(t, hit)), None)
            for t in case.evidence
        ],
        "target_hit_at_5": [any(target_matches(t, hit) for hit in hits[:5]) for t in case.evidence],
        "target_loaded": [
            any(target_matches(t, hit) for hit in selected.values()) for t in case.evidence
        ],
        "context_chunk_ids": [hit["chunk_id"] for hit in selected.values()],
    }


class QueryBudget(CallBudget):
    """Reserve the shared six-attempt cap durably before the provider sees a request."""

    def __init__(self, report):
        super().__init__(embedding_limit=2)
        count = report.get("reserved_embedding_calls")
        day12.require(
            type(count) is int and 0 <= count <= CONFIG["max_embedding_attempts"],
            "diagnostic_ledger_invalid",
        )
        self.report = report

    def check(self, kind, output_limit):
        if kind != "embedding":
            raise ModelFailure("invalid_model_input")
        if self.report["reserved_embedding_calls"] >= CONFIG["max_embedding_attempts"]:
            raise ModelFailure("call_budget_exhausted")
        super().check(kind, output_limit)

    def reserve(self, kind, estimated_input, output_limit):
        charge = super().reserve(kind, estimated_input, output_limit)
        self.report["reserved_embedding_calls"] += 1
        write_report("experiment", self.report)
        return charge


async def experiment(env):
    with experiment_lock():
        day12.require(not (CACHE / "experiment.json").exists(), "experiment_already_recorded")
        baseline = day12.read_report(CACHE / "evaluate.json")
        corpus, suite, lock = load_contracts()
        day12.require(
            baseline["status"] == "completed"
            and baseline["dataset_lock"] == lock
            and baseline["workflow_version"] == MedResearchWorkflow.version,
            "diagnostic_baseline_changed",
        )
        cases = {case.case_id: case for case in suite.cases if case.split == "dev"}
        report = {
            "status": "collecting",
            "recorded_at": datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).isoformat(),
            "config": CONFIG,
            "config_sha256": canonical_digest(CONFIG),
            "baseline_sha256": canonical_digest(baseline),
            "dataset_lock": lock,
            "reserved_embedding_calls": 0,
            "generation_calls": 0,
            "rows": [],
            "production_adopted": False,
            "developer_review": "pending",
        }
        write_report("experiment", report)
        isolated = day12.isolated_environment(env)
        try:
            with (
                day11.model_server(isolated) as runtime,
                open_database(DatabaseSettings(url=isolated["TRACEAGENT_DB_URL"])) as database,
            ):
                owner = (
                    AuthService(database.sessions)
                    .authenticate(isolated["TRACEAGENT_SEED_API_KEY_A"])
                    .owner_id
                )
                report["database_versions"] = database.probe()
                report["runtime"] = runtime
                async with OllamaAdapter(Settings()) as model:
                    retriever = RetrievalService(database.sessions, model)
                    for identifier in CONFIG["case_ids"]:
                        case, observation = cases[identifier], baseline["observations"][identifier]
                        request = RetrievalRequest.model_validate(
                            {
                                k: v
                                for k, v in observation["request"].items()
                                if k != "idempotency_key"
                            }
                        )
                        day12.require(
                            request.question == case.question, "diagnostic_question_changed"
                        )
                        budget = QueryBudget(report)
                        row = {"case_id": identifier, "status": "running"}
                        report["rows"].append(row)
                        write_report("experiment", report)
                        try:
                            with database.sessions() as session:
                                before = retriever._scope(session, owner, request)
                            eligible, excluded, signatures = before
                            day12.require(
                                retriever._snapshot(signatures)
                                == observation["evidence"]["data_snapshot"],
                                "diagnostic_evidence_changed",
                            )
                            if eligible:
                                generated = await model.embed_query(request.question, budget=budget)
                                day12.require(
                                    generated.profile == EMBED, "diagnostic_embedding_changed"
                                )
                                vector = checked_vectors(generated.vectors, 1)[0]
                                with database.sessions() as session:
                                    day12.require(
                                        retriever._scope(session, owner, request) == before,
                                        "diagnostic_evidence_changed",
                                    )
                                    dense, lexical, times = ranked_rows(
                                        session, owner, eligible, request, vector
                                    )
                                dense_ids = [str(v[0].id) for v in dense]
                                lexical_ids = [str(v[0].id) for v in lexical]
                                trusted = observation["trusted_chunks"]
                                day12.require(
                                    set(dense_ids) == set(trusted), "diagnostic_index_incomplete"
                                )
                                hybrid = fused_ids(dense_ids, lexical_ids)
                                row.update(
                                    scope_chunks=len(dense),
                                    lexical_query=lexical_query(case.question),
                                    dense=measurements(case, dense_ids, trusted),
                                    lexical=measurements(case, lexical_ids, trusted),
                                    hybrid=measurements(case, hybrid, trusted),
                                    timings=times,
                                    same_query_vector=True,
                                    raw_pdf_targets=[],
                                    stored_target_chunks=[
                                        sum(target_matches(t, h) for h in trusted.values())
                                        for t in case.evidence
                                    ],
                                )
                                for target in case.evidence:
                                    source = next(
                                        s for s in corpus.sources if s.source_id == target.source_id
                                    )
                                    text = (
                                        PdfReader(confined_path(source.cache_file))
                                        .pages[target.pdf_page - 1]
                                        .extract_text()
                                    )
                                    row["raw_pdf_targets"].append(
                                        normalized(target.anchor) in normalized(text)
                                    )
                            else:
                                row.update(
                                    scope_chunks=0,
                                    excluded_documents=excluded,
                                    dense=measurements(case, [], {}),
                                    hybrid=measurements(case, [], {}),
                                    empty_scope_zero_inference=True,
                                )
                            row["status"] = "completed"
                        finally:
                            row["budget"] = budget.snapshot()
                            write_report("experiment", report)
                report["status"] = "completed"
                report["summary"] = {
                    "cases": len(report["rows"]),
                    "embedding_attempts": report["reserved_embedding_calls"],
                    "generation_calls": 0,
                    "production_adopted": False,
                }
        except Exception:
            report["status"] = "failed"
            report["error_code"] = "retrieval_diagnostic_failed"
            write_report("experiment", report)
            raise
        write_report("experiment", report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["evaluate", "experiment", "review"])
    parser.add_argument("--require-quality", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "review":
            result = day12.review(cache=CACHE)
        else:
            env = day6.environment()
            result = asyncio.run(
                experiment(env)
                if args.command == "experiment"
                else day12.evaluate(env, cache=CACHE)
            )
    except (EvaluationRejected, DatasetRejected, day3.ProbeFailure, ModelFailure) as error:
        result = {"status": "failed", "error_code": str(error)}
    except Exception:
        result = {"status": "failed", "error_code": "day14_failed"}
    try:
        if args.command != "experiment":
            write_report(args.command, result)
    except Exception:
        result = {"status": "failed", "error_code": "report_write_failed"}
    print(json.dumps({k: v for k, v in result.items() if k in {"status", "summary", "error_code"}}))
    if result["status"] != "completed":
        return 1
    counts = result.get("summary", {}).get("quality_counts", {})
    if args.require_quality and (
        not counts or any(counts.get(k, 0) for k in ("failed", "pending_review"))
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
