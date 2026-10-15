"""Reproducible local parsing demonstrations. Never prints credentials or raw exceptions."""

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from time import monotonic
from uuid import UUID

import httpx
from sqlalchemy.engine import make_url

from app.core.config import ConfigurationError, Settings
from app.db.config import DatabaseSettings
from app.db.models import Job
from app.db.session import DatabaseUnavailable, open_database
from app.experiments.day1 import extract_pdf
from app.ingestion.parser import IsolatedParser
from app.ingestion.text import CHUNK_OVERLAP, CHUNK_SIZE, INGEST_VERSION, ParseRejected, split_text
from app.services.auth import AuthenticationRequired, AuthService
from app.services.documents import DataIntegrityError, ResourceNotFound
from app.services.ingestion import IngestConflict, IngestionService
from app.storage.files import StorageUnavailable
from scripts import day3, day4
from scripts.verify_sources import cache_path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/day5"


def write_report(name, result):
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / (name + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


def sources():
    return json.loads((ROOT / "docs/day2-sources.json").read_text())["sources"]


def corpus() -> dict:
    before = json.loads((ROOT / "docs/day5-parser-baseline.json").read_text())
    after, extraction, memory = [], [], []
    for source in sources():
        path = cache_path(source, ROOT)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise day3.ProbeFailure("source_hash_mismatch")
        # Public, pinned fixtures only. This preserves the Day 1 baseline comparison.
        started = monotonic()
        result = extract_pdf(path)
        after.append(
            {
                "source_id": source["source_id"],
                "pages": result["page_count"],
                "characters": result["characters"],
                "text_sha256": hashlib.sha256(
                    json.dumps(result["pages"], ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest(),
            }
        )
        parser = IsolatedParser(Settings())
        parsed = parser.parse(raw, "application/pdf")
        chunks = split_text(parsed)
        anchors = []
        for anchor in source["verification_anchors"]:
            expected = "".join(anchor["anchor"].split()).casefold()
            matching = [
                chunk.ordinal
                for chunk in chunks
                if chunk.pdf_page == anchor["pdf_page"]
                and expected in "".join(chunk.content.split()).casefold()
            ]
            if not matching:
                raise day3.ProbeFailure("chunk_anchor_missing")
            anchors.append({"pdf_page": anchor["pdf_page"], "chunk_ordinals": matching})
        if len(parsed.pages) != source["page_count"]:
            raise day3.ProbeFailure("page_count_changed")
        extraction.append(
            {
                "source_id": source["source_id"],
                "pages": len(parsed.pages),
                "characters": parsed.characters,
                "chunks": len(chunks),
                "anchors": anchors,
                "seconds_including_comparison": round(monotonic() - started, 4),
            }
        )
        memory.append(parser.memory_limited)
    if before["sources"] != after:
        raise day3.ProbeFailure("parser_patch_changed_corpus")
    result = {
        "status": "passed",
        "before": before["pypdf"],
        "after": version("pypdf"),
        "all_page_text_hashes_unchanged": True,
        "ingest_version": INGEST_VERSION,
        "chunk_characters": CHUNK_SIZE,
        "overlap_characters": CHUNK_OVERLAP,
        "memory_limit_active": all(memory),
        "sources": extraction,
        "baseline_timing_available": False,
    }
    write_report("corpus", result)
    return result


def settings(env):
    return Settings(upload_root=Path(env["TRACEAGENT_UPLOAD_ROOT"]))


def open_service(env, database):
    config = settings(env)
    from app.storage.files import FileStore

    return IngestionService(database.sessions, FileStore(config.upload_root), config)


def principal(env, database, owner):
    return AuthService(database.sessions).authenticate(env["TRACEAGENT_SEED_API_KEY_" + owner])


def inspect_document(env, identifier, owner, *, show=False):
    with open_database(DatabaseSettings(url=env["TRACEAGENT_DB_URL"])) as database:
        identity = principal(env, database, owner)
        ingest = open_service(env, database)
        chunks = ingest.chunks(identity.owner_id, UUID(identifier))
        result = {
            "status": "verified",
            "document_id": identifier,
            "chunks": len(chunks),
            "chunk_ids": [chunk["id"] for chunk in chunks],
            "ingest_version": INGEST_VERSION,
            "independent_process": True,
        }
        if show:
            result["preview"] = chunks[:2]
        return result


def failure_environment(env):
    """Failure fixtures use the isolated DB so existing application quotas stay intact."""
    target = env["TRACEAGENT_TEST_DB_URL"]
    if make_url(target).database != "traceagent_test":
        raise day3.ProbeFailure("isolated_database_name_required")
    return {
        **env,
        "TRACEAGENT_DB_URL": target,
        "TRACEAGENT_UPLOAD_ROOT": str(CACHE / "failure-uploads"),
    }


def demo(env):
    checks, documents = {}, []
    headers = {
        owner: {"X-API-Key": env["TRACEAGENT_SEED_API_KEY_" + owner]} for owner in ("A", "B")
    }
    txt = "中文证据\r\n" + "TraceAgent evidence retrieval only. " * 80 + "\n末尾"
    fixtures = [("A", b"\xef\xbb\xbf" + txt.encode(), "text/plain", day4.metadata("day5-txt"))]
    for source in sources():
        fields = {
            key: str(source[key])
            for key in (
                "source_id",
                "title",
                "source_url",
                "license",
                "license_evidence_url",
                "publication_year",
                "version_label",
                "research_dates",
            )
        }
        fixtures.append(("B", cache_path(source, ROOT).read_bytes(), "application/pdf", fields))
    with (
        day4.api_server(env),
        httpx.Client(base_url=day4.ORIGIN, timeout=15, trust_env=False) as client,
        open_database(DatabaseSettings(url=env["TRACEAGENT_DB_URL"])) as database,
    ):
        ingest = open_service(env, database)
        for owner, data, media, fields in fixtures:
            identity = principal(env, database, owner)

            def upload():
                return client.post(
                    "/documents",
                    headers=headers[owner],
                    data=fields,
                    files={"file": ("../../untrusted-name", data, media)},
                )

            response = upload()
            if response.status_code != 202:
                raise day3.ProbeFailure("day5_upload_failed")
            accepted = response.json()
            identifier = UUID(accepted["document_id"])
            outcome = ingest.parse(identity.owner_id, identifier)
            chunks = ingest.chunks(identity.owner_id, identifier)
            original_ids = [chunk["id"] for chunk in chunks]
            if media == "text/plain":
                checks["txt_bom_crlf_unicode_exact_locators"] = all(
                    chunk["content"] == txt[chunk["char_start"] : chunk["char_end"]]
                    for chunk in chunks
                )
            else:
                parsed = IsolatedParser(settings(env)).parse(data, media)
                checks[fields["source_id"] + "_physical_locators"] = all(
                    chunk["content"] in parsed.pages[chunk["pdf_page"] - 1].text for chunk in chunks
                )
            status = client.get(f"/documents/{identifier}/status", headers=headers[owner])
            checks[fields["source_id"] + "_status"] = (
                outcome.status == "PARSED"
                and status.status_code == 200
                and status.json()["status"] == "PARSED"
                and status.json()["index_version"] is None
            )
            duplicate = upload()
            checks[fields["source_id"] + "_replay"] = (
                duplicate.status_code == 202
                and duplicate.json()["document_id"] == str(identifier)
                and duplicate.json()["job_id"] == accepted["job_id"]
                and ingest.parse(identity.owner_id, identifier).reused
                and [chunk["id"] for chunk in ingest.chunks(identity.owner_id, identifier)]
                == original_ids
            )
            with database.sessions() as session:
                job = session.get(Job, UUID(accepted["job_id"]))
                checks[fields["source_id"] + "_job_pending"] = job.status == "PENDING"
            output = day3.checked(
                [
                    sys.executable,
                    "-m",
                    "scripts.day5",
                    "inspect",
                    str(identifier),
                    "--owner",
                    owner,
                ],
                env,
            )
            checks[fields["source_id"] + "_new_process"] = (
                json.loads(output)["chunk_ids"] == original_ids
            )
            other = "B" if owner == "A" else "A"
            checks[fields["source_id"] + "_owner_isolation"] = (
                client.get(f"/documents/{identifier}/status", headers=headers[other]).status_code
                == 404
            )
            documents.append(
                {
                    "document_id": str(identifier),
                    "owner_label": owner,
                    "source_id": fields["source_id"],
                    "status": outcome.status,
                    "chunks": len(chunks),
                }
            )
    failure_env = failure_environment(env)
    # A fresh test DB may have no schema; this target is guarded above.
    day3.checked([sys.executable, "-m", "alembic", "upgrade", "head"], failure_env)
    day4.seed(failure_env)
    with (
        day4.api_server(failure_env),
        httpx.Client(base_url=day4.ORIGIN, timeout=15, trust_env=False) as client,
        open_database(DatabaseSettings(url=failure_env["TRACEAGENT_DB_URL"])) as database,
    ):
        failure = client.post(
            "/documents",
            headers=headers["B"],
            data=day4.metadata("day5-invalid"),
            files={"file": ("broken.pdf", b"%PDF-1.7\ninvalid day5 fixture", "application/pdf")},
        )
        if failure.status_code != 202:
            raise day3.ProbeFailure("invalid_fixture_upload_failed")
        identifier = UUID(failure.json()["document_id"])
        outcome = open_service(failure_env, database).parse(
            principal(failure_env, database, "B").owner_id, identifier
        )
        failed_status = client.get(f"/documents/{identifier}/status", headers=headers["B"])
        checks["real_http_parse_failure_200_with_safe_code"] = (
            outcome.status == "FAILED"
            and failed_status.status_code == 200
            and failed_status.json()["error"] == "invalid_pdf"
        )
    # A fresh API process must observe the committed PARSED status.
    with (
        day4.api_server(env),
        httpx.Client(base_url=day4.ORIGIN, timeout=5, trust_env=False) as client,
    ):
        checks["api_restart_preserves_parsed_documents"] = all(
            client.get(
                f"/documents/{document['document_id']}/status",
                headers=headers[document["owner_label"]],
            ).json()["status"]
            == "PARSED"
            for document in documents
        )
    if not all(checks.values()):
        raise day3.ProbeFailure("day5_demo_failed")
    result = {
        "status": "passed",
        "transport": "real_loopback_http",
        "failure_fixture_database": "traceagent_test",
        "checks": checks,
        "documents": documents,
    }
    write_report("demo", result)
    return result


def offline_check(env):
    started = monotonic()
    try:
        day3.compose(env, "stop", "database")
        with (
            day4.api_server(env),
            httpx.Client(base_url=day4.ORIGIN, timeout=10, trust_env=False) as client,
        ):
            health = client.get("/health").status_code
            status = client.get(
                "/documents/00000000-0000-4000-8000-000000000001/status",
                headers={"X-API-Key": env["TRACEAGENT_SEED_API_KEY_A"]},
            )
            try:
                with open_database(DatabaseSettings(url=env["TRACEAGENT_DB_URL"])) as database:
                    open_service(env, database).parse(day3.USER_A, UUID(int=1))
            except DatabaseUnavailable:
                parse_failed_safely = True
            else:
                parse_failed_safely = False
        unavailable_seconds = round(monotonic() - started, 4)
    finally:
        day3.compose(env, "up", "-d", "--wait", "--wait-timeout", "60")
    with open_database(DatabaseSettings(url=env["TRACEAGENT_DB_URL"])) as database:
        database.probe()
    if health != 200 or status.status_code != 503 or not parse_failed_safely:
        raise day3.ProbeFailure("offline_check_failed")
    result = {
        "status": "passed",
        "real_database_stopped": True,
        "health_http": health,
        "business_http": status.status_code,
        "parse_safe_database_failure": parse_failed_safely,
        "outage_exercise_seconds_including_stop_and_api_start": unavailable_seconds,
        "recovered_with_fresh_connection": True,
    }
    write_report("offline", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["corpus", "demo", "parse", "inspect", "test", "audit", "offline-check"]
    )
    parser.add_argument("identifier", nargs="?")
    parser.add_argument("--owner", choices=["A", "B"], default="A")
    parser.add_argument(
        "--show", action="store_true", help="Show at most two local evidence chunks"
    )
    parser.add_argument("--report-label", choices=["local", "isolated"], default="local")
    args = parser.parse_args(argv)
    try:
        if args.command == "corpus":
            result = corpus()
        else:
            env = day4.environment()
            if args.command == "test":
                output = day3.checked(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-q",
                        "--basetemp=.cache/pytest/day5",
                        f"--junitxml=.cache/day5/tests-{args.report_label}.xml",
                    ],
                    {**env, "TRACEAGENT_DATABASE_TESTS": "1", "TRACEAGENT_CONTAINER_TESTS": "1"},
                    timeout=240,
                )
                (CACHE / f"test-summary-{args.report_label}.txt").write_text(output)
                result = {"status": "passed", "summary": output.splitlines()[-1]}
            elif args.command == "audit":
                result = day4.audit(env)
                write_report("audit", result)
            elif args.command in {"parse", "inspect"}:
                if args.identifier is None or str(UUID(args.identifier)) != args.identifier:
                    raise day3.ProbeFailure("canonical_document_id_required")
                if args.command == "inspect":
                    result = inspect_document(env, args.identifier, args.owner, show=args.show)
                else:
                    with open_database(DatabaseSettings(url=env["TRACEAGENT_DB_URL"])) as database:
                        identity = principal(env, database, args.owner)
                        result = asdict(
                            open_service(env, database).parse(
                                identity.owner_id, UUID(args.identifier)
                            )
                        )
            else:
                result = {"demo": demo, "offline-check": offline_check}[args.command](env)
        print(json.dumps(result, default=str, ensure_ascii=False, indent=2))
        return 1 if result.get("status") in {"failed", "FAILED"} else 0
    except (
        ConfigurationError,
        DatabaseUnavailable,
        StorageUnavailable,
        AuthenticationRequired,
        ResourceNotFound,
        IngestConflict,
        DataIntegrityError,
        ParseRejected,
        day3.ProbeFailure,
    ) as error:
        print(json.dumps({"status": "failed", "error_code": str(error)}))
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "day5_probe_failed"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
