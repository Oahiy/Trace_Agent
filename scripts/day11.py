"""Real asynchronous research demo and checks; all artifacts use the single cache inventory."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from time import monotonic
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.config import DatabaseSettings
from app.db.models import ResearchRun
from app.db.session import open_database
from app.domains.med.research import MedResearchWorkflow
from app.llm.ollama import OllamaAdapter
from app.services.auth import AuthService
from app.services.indexing import IndexingService
from app.services.research import ResearchService
from app.storage.files import FileStore
from scripts import day3, day4, day6, day7
from scripts.day8 import confined_path, load_contracts

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/day11"


def require(condition, code):
    if not condition:
        raise day3.ProbeFailure(code)


def write_report(name, value):
    require(
        (ROOT / ".cache").resolve() == ROOT / ".cache" and CACHE.resolve() == ROOT / ".cache/day11",
        "unsafe_report_path",
    )
    CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".report-", dir=CACHE)
    path = Path(filename)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(path, CACHE / (name + ".json"))
    finally:
        path.unlink(missing_ok=True)


def isolated_environment(env):
    require(
        make_url(env["TRACEAGENT_TEST_DB_URL"]).database == "traceagent_test",
        "isolated_database_required",
    )
    isolated = day6.isolated_environment(env)
    isolated["TRACEAGENT_UPLOAD_ROOT"] = str(CACHE / "uploads")
    return isolated


@contextmanager
def model_server(env):
    process = None
    with (
        httpx.Client(timeout=2, trust_env=False) as client,
        (CACHE / "ollama.log").open("ab") as log,
    ):
        try:
            client.get("http://127.0.0.1:11434/api/version").raise_for_status()
        except httpx.HTTPError:
            process = subprocess.Popen(
                ["ollama", "serve"], cwd=ROOT, env=env, stdout=log, stderr=log
            )
        try:

            def running():
                if process is not None and process.poll() is not None:
                    raise day3.ProbeFailure("model_start_failed")
                try:
                    response = client.get("http://127.0.0.1:11434/api/version")
                    return response.json() if response.status_code == 200 else None
                except httpx.HTTPError:
                    return None

            version = day6.wait_until(running, seconds=20)
            yield {"version": version["version"], "owned_process": process is not None}
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


async def demo(env):
    started = monotonic()
    before = day7.application_snapshot(env)
    isolated = isolated_environment(env)
    day3.checked([sys.executable, "-m", "alembic", "upgrade", "head"], isolated)
    day4.seed(isolated)
    corpus, suite, lock = load_contracts()
    source = next(s for s in corpus.sources if s.source_id == "prisma-2020")
    case = next(c for c in suite.cases if c.case_id == "dev-02")
    raw = confined_path(source.cache_file).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == source.sha256, "source_hash_mismatch")
    fields = {
        key: str(getattr(source, key)) for key in day7.METADATA if getattr(source, key) is not None
    }
    headers = {"X-API-Key": isolated["TRACEAGENT_SEED_API_KEY_A"]}
    other = {"X-API-Key": isolated["TRACEAGENT_SEED_API_KEY_B"]}
    settings = Settings(upload_root=Path(isolated["TRACEAGENT_UPLOAD_ROOT"]))
    with (
        model_server(isolated) as runtime,
        day4.api_server(isolated),
        day6.worker_server(isolated),
        httpx.Client(base_url=day4.ORIGIN, timeout=10, trust_env=False) as client,
        open_database(DatabaseSettings(url=isolated["TRACEAGENT_DB_URL"])) as database,
    ):
        day6.wait_until(lambda: client.get("/ready").status_code == 200)
        uploaded = client.post(
            "/documents",
            headers=headers,
            data=fields,
            files={"file": ("prisma-2020.pdf", raw, "application/pdf")},
        )
        require(uploaded.status_code == 202, "upload_failed")
        identifier = uploaded.json()["document_id"]
        day6.wait_until(
            lambda: (
                client.get(f"/documents/{identifier}/status", headers=headers).json()["status"]
                in {"PARSED", "READY"}
            ),
            seconds=60,
        )
        owner = (
            AuthService(database.sessions)
            .authenticate(isolated["TRACEAGENT_SEED_API_KEY_A"])
            .owner_id
        )
        async with OllamaAdapter(settings) as model:
            indexed = await IndexingService(
                database.sessions, FileStore(settings.upload_root), model
            ).index(owner, UUID(identifier))
            require(indexed.status == "READY", "index_failed")

        def submit(question, key, **conditions):
            request = {
                "question": question,
                "document_ids": [identifier],
                "idempotency_key": key,
                **conditions,
            }
            response = client.post("/research", headers=headers, json=request)
            require(response.status_code == 202, "research_create_failed")
            job_id = response.json()["job_id"]

            def completed():
                result = client.get("/research/" + job_id, headers=headers)
                require(result.status_code == 200, "research_query_failed")
                value = result.json()
                return value if value["status"] in {"SUCCEEDED", "FAILED"} else None

            outcome = day6.wait_until(completed, seconds=60)
            require(outcome["status"] == "SUCCEEDED", "research_execution_failed")
            return request, outcome

        request, answer = submit(case.question, "day11-dev02-" + MedResearchWorkflow.version)
        require(answer["result"]["kind"] == "answer", "research_answer_missing")
        claim = answer["result"]["claims"][0]
        require(claim["citations"][0]["pdf_page"] == 5, "expected_page_missing")
        require(
            "should not be used to assess" in " ".join(claim["text"].split()).lower(),
            "expected_support_missing",
        )
        repeat = client.post("/research", headers=headers, json=request)
        require(repeat.json()["job_id"] == answer["job_id"], "idempotency_replay_failed")
        require(
            client.post(
                "/research",
                headers=headers,
                json={**request, "question": "Different PRISMA reporting question"},
            ).status_code
            == 409,
            "idempotency_conflict_failed",
        )
        require(
            client.get("/research/" + answer["job_id"], headers=other).status_code == 404,
            "cross_owner_query_failed",
        )
        require(
            client.post("/research", headers=other, json=request).status_code == 404,
            "cross_owner_input_failed",
        )
        _, empty = submit(
            case.question, "day11-empty-year", publication_year_from=2009, publication_year_to=2009
        )
        _, medical = submit("PRISMA reporting: prescribe a treatment dosage", "day11-medical")
        require(empty["result"]["kind"] == medical["result"]["kind"] == "refused", "refusal_failed")
        budgets = {}
        with database.sessions() as session:
            for value in (answer, empty, medical):
                record = session.get(ResearchRun, UUID(value["job_id"]))
                budgets[value["job_id"]] = record.budget
        require(
            all(
                budgets[v["job_id"]]["generation_calls"]
                == budgets[v["job_id"]]["embedding_calls"]
                == 0
                for v in (empty, medical)
            ),
            "refusal_spent_inference",
        )
        require(budgets[answer["job_id"]]["generation_calls"] == 1, "unexpected_generation_count")
    after = day7.application_snapshot(env)
    require(before == after, "application_state_changed")
    return {
        "status": "passed",
        "recorded_at": datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).isoformat(),
        "runtime": runtime,
        "database": "traceagent_test",
        "source_id": source.source_id,
        "source_sha256": source.sha256,
        "dataset_lock": lock,
        "answer": answer,
        "empty_year": empty,
        "medical": medical,
        "budgets": budgets,
        "checks": {
            "real_http": True,
            "separate_worker": True,
            "owner_isolation": True,
            "idempotency": True,
            "source_page": True,
            "application_data_preserved": True,
        },
        "application_snapshot_before": before,
        "application_snapshot_after": after,
        "cloud_api_cost_usd": 0,
        "seconds": round(monotonic() - started, 4),
        "limitations": [
            "One development case; not a corpus-wide or blind evaluation",
            "Human developer review and third genuine conflict case remain pending",
            "Extractive summary; no free-form clinical answer generation",
        ],
    }


def tests(env):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_day11.py",
            "tests/test_day11_database.py",
            "tests/test_day11_queue.py",
            "-q",
            "--basetemp=.cache/pytest/day11",
            "--junitxml=.cache/day11/pytest.xml",
        ],
        cwd=ROOT,
        env={
            **env,
            "TRACEAGENT_DATABASE_TESTS": "1",
            "TRACEAGENT_QUEUE_TESTS": "1",
            "TRACEAGENT_QUEUE_ENABLED": "false",
        },
        capture_output=True,
        text=True,
        timeout=240,
    )
    output = completed.stdout + completed.stderr
    (CACHE / "tests.log").write_text(output)
    require(completed.returncode == 0, "day11_tests_failed")
    return {
        "status": "passed",
        "summary": next(
            (line for line in reversed(output.splitlines()) if "passed" in line), "see log"
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["demo", "test", "inspect", "reconcile"])
    parser.add_argument("identifier", nargs="?")
    parser.add_argument("--owner", choices=["A", "B"], default="A")
    parser.add_argument("--isolated-test", action="store_true")
    args = parser.parse_args(argv)
    try:
        env = day6.environment()
        if args.command == "test":
            result = tests(env)
        elif args.command == "demo":
            result = asyncio.run(demo(env))
        else:
            env = isolated_environment(env) if args.isolated_test else env
            from app.tasks.queue import ResearchQueue

            queue = ResearchQueue(day6.queue_settings(env))
            try:
                with open_database(DatabaseSettings(url=env["TRACEAGENT_DB_URL"])) as database:
                    owner = (
                        AuthService(database.sessions)
                        .authenticate(env["TRACEAGENT_SEED_API_KEY_" + args.owner])
                        .owner_id
                    )
                    service = ResearchService(
                        database.sessions, queue, workflow_version=MedResearchWorkflow.version
                    )
                    if args.command == "inspect":
                        require(args.identifier is not None, "job_id_required")
                        value = service.get(owner, UUID(args.identifier))
                    else:
                        value = service.reconcile(owner)
                    result = {"status": "passed", "research": value}
            finally:
                queue.close()
    except day3.ProbeFailure as error:
        result = {"status": "failed", "error_code": str(error)}
    except Exception:
        result = {"status": "failed", "error_code": "day11_failed"}
    try:
        write_report(args.command, result)
    except Exception:
        result = {"status": "failed", "error_code": "report_write_failed"}
    print(json.dumps({k: v for k, v in result.items() if k in {"status", "summary", "error_code"}}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
