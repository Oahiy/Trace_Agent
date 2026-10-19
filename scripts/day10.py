"""Versioned indexing and exact retrieval; real demos use only the guarded test database."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from time import monotonic
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.config import DatabaseSettings
from app.db.models import IndexTask, Job, ParseTask
from app.db.session import DatabaseUnavailable, open_database
from app.llm.contracts import ModelFailure
from app.llm.ollama import OllamaAdapter
from app.retrieval.contracts import SPEC, IndexFailure
from app.services.auth import AuthService
from app.services.documents import DataIntegrityError, ResourceNotFound
from app.services.indexing import IndexingService
from app.services.ingestion import IngestConflict, IngestionService
from app.services.retrieval import RetrievalService
from app.storage.files import FileStore, StorageUnavailable
from scripts import day3, day4, day7
from scripts.day8 import confined_path, load_contracts, normalized

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/day10"


def require(condition, code):
    if not condition:
        raise day3.ProbeFailure(code)


def write_report(name, report):
    if (ROOT / ".cache").resolve() != ROOT / ".cache" or CACHE.resolve() != ROOT / ".cache/day10":
        raise day3.ProbeFailure("unsafe_report_path")
    CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".report-", dir=CACHE)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, CACHE / (name + ".json"))
    finally:
        temporary.unlink(missing_ok=True)


def isolated_environment(env):
    require(
        make_url(env["TRACEAGENT_TEST_DB_URL"]).database == "traceagent_test",
        "isolated_database_required",
    )
    return {
        **env,
        "TRACEAGENT_DB_URL": env["TRACEAGENT_TEST_DB_URL"],
        "TRACEAGENT_UPLOAD_ROOT": str(CACHE / "uploads"),
        "TRACEAGENT_QUEUE_ENABLED": "false",
    }


async def demo(env):
    started = monotonic()
    before = day7.application_snapshot(env)
    isolated = isolated_environment(env)
    day3.checked([sys.executable, "-m", "alembic", "upgrade", "head"], isolated)
    day4.seed(isolated)
    settings = Settings(upload_root=Path(isolated["TRACEAGENT_UPLOAD_ROOT"]), queue_enabled=False)
    files = FileStore(settings.upload_root)
    corpus, suite, lock = load_contracts()
    source = next(s for s in corpus.sources if s.source_id == "prisma-2020")
    raw = confined_path(source.cache_file).read_bytes()
    require(
        len(raw) == source.bytes and hashlib.sha256(raw).hexdigest() == source.sha256,
        "source_hash_mismatch",
    )
    fields = {
        key: str(getattr(source, key)) for key in day7.METADATA if getattr(source, key) is not None
    }
    headers = {"X-API-Key": isolated["TRACEAGENT_SEED_API_KEY_A"]}
    with (
        day4.api_server(isolated),
        httpx.Client(base_url=day4.ORIGIN, timeout=30, trust_env=False) as client,
    ):
        response = client.post(
            "/documents",
            headers=headers,
            data=fields,
            files={"file": ("prisma-2020.pdf", raw, "application/pdf")},
        )
        require(response.status_code == 202, "upload_failed")
        accepted = response.json()
        identifier = UUID(accepted["document_id"])
        with open_database(DatabaseSettings(url=isolated["TRACEAGENT_DB_URL"])) as database:
            owner = (
                AuthService(database.sessions)
                .authenticate(isolated["TRACEAGENT_SEED_API_KEY_A"])
                .owner_id
            )
            other = (
                AuthService(database.sessions)
                .authenticate(isolated["TRACEAGENT_SEED_API_KEY_B"])
                .owner_id
            )
            ingestion = IngestionService(database.sessions, files, settings)
            parsed = ingestion.parse(owner, identifier)
            chunks = ingestion.chunks(owner, identifier)
            chunk_ids = [c["id"] for c in chunks]
            require(parsed.status in {"PARSED", "READY"}, "parse_failed")
            async with OllamaAdapter(settings) as model:
                index = IndexingService(database.sessions, files, model)
                index_started = monotonic()
                indexed = await index.index(owner, identifier)
                index_seconds = monotonic() - index_started
                require(indexed.status == "READY", "index_failed")
                repeated = await index.index(owner, identifier)
                require(
                    repeated.reused and ingestion.chunks(owner, identifier) == chunks,
                    "index_replay_changed_evidence",
                )
                retrieval = RetrievalService(database.sessions, model)
                case = next(c for c in suite.cases if c.case_id == "dev-02")
                query_started = monotonic()
                result = await retrieval.search(
                    owner, {"question": case.question, "document_ids": [str(identifier)]}
                )
                query_seconds = monotonic() - query_started
                hit = any(
                    h["pdf_page"] == 5
                    and normalized("should not be used to assess") in normalized(h["content"])
                    for h in result["hits"]
                )
                require(hit, "expected_evidence_not_in_top5")
                wrong_year = await retrieval.search(
                    owner,
                    {
                        "question": case.question,
                        "document_ids": [str(identifier)],
                        "publication_year_from": 2020,
                        "publication_year_to": 2020,
                    },
                )
                require(
                    not wrong_year["hits"] and wrong_year["usage"]["embedding_calls"] == 0,
                    "publication_year_filter_failed",
                )
                for requested in (identifier, UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")):
                    try:
                        await retrieval.search(
                            other,
                            {"question": "reporting guidance", "document_ids": [str(requested)]},
                        )
                    except ResourceNotFound:
                        pass
                    else:
                        raise day3.ProbeFailure("owner_filter_failed")
            with database.sessions() as session:
                task = session.scalar(
                    select(IndexTask).where(IndexTask.job_id == UUID(accepted["job_id"]))
                )
                job = session.get(Job, task.job_id)
                parse = session.get(ParseTask, task.job_id)
                require(
                    task.status == job.status == parse.status == "SUCCEEDED",
                    "phase_status_mismatch",
                )
                budget = task.budget
            status = client.get(f"/documents/{identifier}/status", headers=headers)
            require(
                status.status_code == 200
                and status.json()["status"] == "READY"
                and status.json()["index_version"] == SPEC.version,
                "ready_http_failed",
            )
            with httpx.Client(timeout=3, trust_env=False) as local:
                runtime = local.get(settings.local_model_url + "/api/version").json()["version"]
    after = day7.application_snapshot(env)
    require(before == after, "application_records_changed")
    report = {
        "status": "passed",
        "recorded_at": datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).isoformat(),
        "scope": "One locked public PDF; actual HTTP upload/status, parsing, embeddings, PostgreSQL and internal exact retrieval",
        "database": "traceagent_test",
        "ollama": runtime,
        "index_spec": asdict(SPEC),
        "index_version": SPEC.version,
        "source_id": source.source_id,
        "source_sha256": source.sha256,
        "document_id": str(identifier),
        "chunks": len(chunk_ids),
        "chunk_ids_preserved": True,
        "index": asdict(indexed),
        "repeat": asdict(repeated),
        "index_budget": budget,
        "retrieval": result,
        "expected_evidence_in_top5": hit,
        "publication_year_2020": wrong_year,
        "cross_owner_and_absent_rejected": True,
        "document_status_http": status.json(),
        "application_snapshot_before": before,
        "application_snapshot_after": after,
        "dataset_lock": lock,
        "cloud_api_cost_usd": 0,
        "timings_seconds": {
            "index": round(index_seconds, 6),
            "query": round(query_seconds, 6),
            "demo": round(monotonic() - started, 6),
        },
        "limitations": [
            "Single-document dev smoke test; no corpus-wide Recall@5/MRR or blind evaluation",
            "No generated answer, medical safety workflow or claim-support evaluation",
            "Developer source/label review and third genuine conflict remain pending",
            "Indexing is explicitly invoked locally; existing queue still runs parsing only",
            "Failed index tasks are terminal; no manual retry or automatic recovery scheduler",
        ],
    }
    # UUIDs in outcomes are rendered without leaking secrets or connection URLs.
    return json.loads(json.dumps(report, default=str))


def tests(env):
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_day10.py",
        "tests/test_day10_database.py",
        "-q",
        "--basetemp=.cache/pytest/day10-tmp",
        "--junitxml=.cache/day10/pytest.xml",
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env={**env, "TRACEAGENT_DATABASE_TESTS": "1", "TRACEAGENT_QUEUE_ENABLED": "false"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = completed.stdout + completed.stderr
    (CACHE / "tests.log").write_text(output)
    require(completed.returncode == 0, "day10_tests_failed")
    return {
        "status": "passed",
        "summary": next(
            (line for line in reversed(output.splitlines()) if "passed" in line), "see log"
        ),
    }


async def operation(env, args):
    settings = Settings(upload_root=Path(env["TRACEAGENT_UPLOAD_ROOT"]))
    with open_database(DatabaseSettings(url=env["TRACEAGENT_DB_URL"])) as database:
        owner = (
            AuthService(database.sessions)
            .authenticate(env["TRACEAGENT_SEED_API_KEY_" + args.owner])
            .owner_id
        )
        async with OllamaAdapter(settings) as model:
            if args.command == "index":
                require(args.identifier is not None, "document_id_required")
                result = await IndexingService(
                    database.sessions, FileStore(settings.upload_root), model
                ).index(owner, UUID(args.identifier))
                return {
                    "status": "passed" if result.status == "READY" else "failed",
                    "index": json.loads(json.dumps(asdict(result), default=str)),
                }
            require(args.question is not None, "question_required")
            return {
                "status": "passed",
                "retrieval": await RetrievalService(database.sessions, model).search(
                    owner,
                    {
                        "question": args.question,
                        "document_ids": args.documents,
                        "publication_year_from": args.year_from,
                        "publication_year_to": args.year_to,
                    },
                ),
            }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["demo", "test", "index", "search"])
    parser.add_argument("identifier", nargs="?")
    parser.add_argument("--owner", choices=["A", "B"], default="A")
    parser.add_argument("--question")
    parser.add_argument("--documents", nargs="+")
    parser.add_argument("--year-from", type=int)
    parser.add_argument("--year-to", type=int)
    args = parser.parse_args(argv)
    try:
        env = day4.environment()
        if args.command == "test":
            result = tests(env)
        elif args.command == "demo":
            result = asyncio.run(demo(env))
        else:
            result = asyncio.run(operation(env, args))
    except (IndexFailure, ModelFailure) as error:
        result = {"status": "failed", "error_code": error.code}
    except (
        DatabaseUnavailable,
        DataIntegrityError,
        ResourceNotFound,
        IngestConflict,
        StorageUnavailable,
    ) as error:
        result = {"status": "failed", "error_code": str(error)}
    except (ValidationError, ValueError):
        result = {"status": "failed", "error_code": "invalid_request"}
    except day3.ProbeFailure as error:
        result = {"status": "failed", "error_code": str(error)}
    except Exception:
        result = {"status": "failed", "error_code": "day10_failed"}
    try:
        write_report(args.command, result)
    except Exception:
        result = {"status": "failed", "error_code": "report_write_failed"}
    # Full evidence is kept in the ignored report; terminal output is a safe summary.
    summary = {
        k: v
        for k, v in result.items()
        if k
        in {
            "status",
            "summary",
            "error_code",
            "chunks",
            "expected_evidence_in_top5",
            "index_version",
        }
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
