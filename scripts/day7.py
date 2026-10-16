"""Day 7 real-file acceptance, isolated from application records and uploads."""

import hashlib
import json
import sys
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.config import DatabaseSettings
from app.db.models import Base, Document, Job, ParseTask
from app.db.session import open_database
from app.experiments.day1 import extract_pdf
from app.services.ingestion import IngestionService
from app.storage.files import FileStore
from scripts import day3, day4, day6
from scripts.verify_sources import cache_path, verify_source

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/day7"
METADATA = (
    "source_id",
    "title",
    "source_url",
    "license",
    "license_evidence_url",
    "publication_year",
    "version_label",
    "research_dates",
)
TXT = b"\xef\xbb\xbf" + ("Day 7 合成证据，保留字符范围。\r\n" * 100).encode()


def require(condition, code):
    if not condition:
        raise day3.ProbeFailure(code)


def application_snapshot(env):
    """Read-only, consistent digest; never serialize row contents or credentials."""
    require(make_url(env["TRACEAGENT_DB_URL"]).database == "traceagent", "app_scope_required")
    result = {}
    with open_database(DatabaseSettings(url=env["TRACEAGENT_DB_URL"])) as database:
        with database.sessions.begin() as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            for table in Base.metadata.sorted_tables:
                rows = session.execute(
                    select(table).order_by(*table.primary_key.columns)
                ).mappings()
                digest, count = hashlib.sha256(), 0
                for row in rows:
                    digest.update(json.dumps(dict(row), sort_keys=True, default=str).encode())
                    digest.update(b"\n")
                    count += 1
                result[table.name] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def validate_locations(chunks, *, decoded_text=None, pages=None):
    """Check persisted evidence against source text, without using the chunker."""
    require(bool(chunks), "chunks_missing")
    require([c["ordinal"] for c in chunks] == list(range(len(chunks))), "ordinal_mismatch")
    require(len({c["id"] for c in chunks}) == len(chunks), "chunk_id_duplicate")
    for chunk in chunks:
        require(chunk["ingest_version"] == "ingest-v1", "chunk_version_mismatch")
        require(bool(chunk["content"].strip()), "empty_chunk")
        if decoded_text is not None:
            start, end = chunk["char_start"], chunk["char_end"]
            require(
                chunk["pdf_page"] is None
                and type(start) is int
                and type(end) is int
                and 0 <= start < end <= len(decoded_text),
                "txt_locator_invalid",
            )
            require(decoded_text[start:end] == chunk["content"], "txt_locator_mismatch")
        else:
            page = chunk["pdf_page"]
            require(
                pages is not None
                and type(page) is int
                and 1 <= page <= len(pages)
                and chunk["char_start"] is None
                and chunk["char_end"] is None,
                "pdf_locator_invalid",
            )
            require(chunk["content"] in pages[page - 1]["text"], "pdf_locator_mismatch")


def evidence(env):
    before = application_snapshot(env)
    isolated = day6.isolated_environment(env)
    isolated["TRACEAGENT_UPLOAD_ROOT"] = str(CACHE / "uploads")
    day3.checked([sys.executable, "-m", "alembic", "upgrade", "head"], isolated)
    day4.seed(isolated)
    source = next(
        s
        for s in json.loads((ROOT / "docs/day2-sources.json").read_text())["sources"]
        if s["source_id"] == "prisma-2020"
    )
    verified = verify_source(source, ROOT)
    path = cache_path(source, ROOT)
    pages = extract_pdf(path)["pages"]
    fields = {key: str(source[key]) for key in METADATA if source.get(key) is not None}
    cases = [
        ("txt", TXT, "text/plain", day4.metadata("day7-txt")),
        ("public_pdf", path.read_bytes(), "application/pdf", fields),
    ]
    checks, records = {}, []
    headers = {"X-API-Key": isolated["TRACEAGENT_SEED_API_KEY_A"]}
    other = {"X-API-Key": isolated["TRACEAGENT_SEED_API_KEY_B"]}
    with (
        day4.api_server(isolated),
        httpx.Client(base_url=day4.ORIGIN, timeout=10, trust_env=False) as client,
        open_database(DatabaseSettings(url=isolated["TRACEAGENT_DB_URL"])) as database,
        day6.worker_server(isolated),
    ):
        day6.wait_until(lambda: client.get("/ready").status_code == 200)
        service = IngestionService(
            database.sessions,
            FileStore(CACHE / "uploads"),
            Settings(upload_root=CACHE / "uploads"),
        )
        for label, data, media, metadata in cases:

            def upload(values=metadata):
                return client.post(
                    "/documents",
                    headers=headers,
                    data=values,
                    files={"file": (label, data, media)},
                )

            accepted = upload()
            require(accepted.status_code == 202, "day7_upload_failed")
            identifiers = accepted.json()
            job_id, document_id = UUID(identifiers["job_id"]), UUID(identifiers["document_id"])

            def finished():
                with database.sessions() as session:
                    task = session.get(ParseTask, job_id)
                    return task.status if task.status in {"SUCCEEDED", "FAILED"} else None

            require(day6.wait_until(finished, seconds=90) == "SUCCEEDED", "day7_parse_failed")
            chunks = service.chunks(day3.USER_A, document_id)
            validate_locations(
                chunks,
                decoded_text=data.decode("utf-8-sig") if label == "txt" else None,
                pages=pages if label == "public_pdf" else None,
            )
            with database.sessions() as session:
                document, job = session.get(Document, document_id), session.get(Job, job_id)
                checks[label + "_metadata"] = all(
                    getattr(document, key)
                    == (
                        int(metadata[key])
                        if key == "publication_year" and metadata.get(key)
                        else metadata.get(key) or None
                    )
                    for key in METADATA
                )
                checks[label + "_parent_pending"] = (
                    job.status == "PENDING" and job.attempt_count == 0
                )
                checks[label + "_hash"] = document.file_sha256 == hashlib.sha256(data).hexdigest()
                execution = session.get(ParseTask, job_id).execution_id
            public = client.get(f"/documents/{document_id}/status", headers=headers)
            checks[label + "_parsed_not_ready"] = public.status_code == 200 and public.json() == {
                "document_id": str(document_id),
                "status": "PARSED",
                "index_version": None,
                "error": None,
            }
            repeated = upload()
            checks[label + "_replay_ids"] = repeated.status_code == 202 and all(
                repeated.json()[key] == identifiers[key] for key in ("document_id", "job_id")
            )
            checks[label + "_replay_chunks"] = service.chunks(day3.USER_A, document_id) == chunks
            with database.sessions() as session:
                checks[label + "_replay_execution"] = (
                    session.get(ParseTask, job_id).execution_id == execution
                )
            checks[label + "_owner_isolation"] = (
                client.get(
                    f"/documents/{document_id}/status",
                    headers=other,
                ).status_code
                == 404
            )
            checks[label + "_authentication"] = (
                client.get(
                    f"/documents/{document_id}/status",
                ).status_code
                == 401
            )
            conflict = upload({**metadata, "title": "Conflicting metadata"})
            checks[label + "_metadata_conflict"] = (
                conflict.status_code == 409
                and conflict.json()["error"]["code"] == "source_metadata_conflict"
                and service.chunks(day3.USER_A, document_id) == chunks
            )
            anchors = []
            if label == "public_pdf":
                for anchor in source["verification_anchors"]:
                    matches = [
                        c["ordinal"]
                        for c in chunks
                        if c["pdf_page"] == anchor["pdf_page"]
                        and "".join(anchor["anchor"].split()).casefold()
                        in "".join(c["content"].split()).casefold()
                    ]
                    require(bool(matches), "persisted_anchor_missing")
                    anchors.append({"pdf_page": anchor["pdf_page"], "chunk_ordinals": matches})
            records.append(
                {
                    "kind": label,
                    "document_id": str(document_id),
                    "job_id": str(job_id),
                    "chunks": len(chunks),
                    "all_chunk_locations_checked": True,
                    "source_id": metadata["source_id"],
                    "anchors": anchors,
                }
            )
        # Successful parsing does not release the full-ingest quota yet.
        first = None
        for number in range(5):
            quota_data = f"Day 7 quota fixture {number}".encode()
            quota_fields = day4.metadata(f"day7-quota-{number}")
            response = client.post(
                "/documents",
                headers=other,
                data=quota_fields,
                files={"file": ("quota.txt", quota_data, "text/plain")},
            )
            require(response.status_code == 202, "quota_setup_failed")
            if first is None:
                first = (quota_data, quota_fields, response.json())
        blocked = client.post(
            "/documents",
            headers=other,
            data=day4.metadata("day7-quota-overflow"),
            files={"file": ("quota.txt", b"Sixth distinct pending ingest", "text/plain")},
        )
        checks["sixth_pending_ingest_rejected"] = (
            blocked.status_code == 429
            and blocked.json()["error"]["code"] == "too_many_pending_tasks"
        )
        data, metadata, identifiers = first
        replay = client.post(
            "/documents",
            headers=other,
            data=metadata,
            files={"file": ("quota.txt", data, "text/plain")},
        )
        checks["replay_allowed_at_quota"] = replay.status_code == 202 and all(
            replay.json()[key] == identifiers[key] for key in ("document_id", "job_id")
        )

        def quota_finished():
            with database.sessions() as session:
                tasks = session.scalars(
                    select(ParseTask)
                    .join(Job, Job.id == ParseTask.job_id)
                    .where(Job.owner_id == day3.USER_B)
                ).all()
                return len(tasks) == 5 and all(task.status == "SUCCEEDED" for task in tasks)

        day6.wait_until(quota_finished, seconds=90)
    after = application_snapshot(env)
    checks["application_rows_unchanged"] = before == after
    result = {
        "status": "passed" if all(checks.values()) else "failed",
        "database": "traceagent_test",
        "upload_root": ".cache/day7/uploads/",
        "real_http_redis_worker_postgresql": True,
        "checks": checks,
        "documents": records,
        "public_source": verified,
        "application_snapshot_before": before,
        "application_snapshot_after": after,
        "model_calls": 0,
    }
    require(all(checks.values()), "day7_acceptance_failed")
    return result


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    try:
        result = evidence(day6.environment())
    except Exception:
        result = {"status": "failed", "error_code": "day7_acceptance_failed"}
    (CACHE / "evidence.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
