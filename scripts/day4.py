"""Reproducible local Day 4 lifecycle; secret values never enter output or arguments."""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from uuid import UUID

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select

from app.core.config import ConfigurationError
from app.db.config import load_database_settings
from app.db.models import Document, User
from app.db.session import DatabaseUnavailable, open_database
from app.services.auth import KEY_PATTERN, key_digest
from app.services.documents import DocumentService
from app.storage.files import FileStore, StorageUnavailable
from scripts import day3

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/day4"
UPLOADS = CACHE / "uploads"
ENV_FILE = CACHE / "auth.env"
KEYS = {"TRACEAGENT_SEED_API_KEY_A", "TRACEAGENT_SEED_API_KEY_B"}
ORIGIN = "http://127.0.0.1:8000"
ALPINE = "alpine@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1"


def prepare() -> None:
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    UPLOADS.mkdir(parents=True, exist_ok=True, mode=0o700)
    if ENV_FILE.is_symlink():
        raise day3.ProbeFailure("auth_env_symlink_rejected")
    if ENV_FILE.exists():
        if ENV_FILE.stat().st_mode & 0o077:
            raise day3.ProbeFailure("auth_env_permissions_invalid")
        return
    values = {key: secrets.token_urlsafe(32) for key in sorted(KEYS)}
    descriptor = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")


def environment() -> dict[str, str]:
    if ENV_FILE.is_symlink() or not ENV_FILE.is_file() or ENV_FILE.stat().st_mode & 0o077:
        raise day3.ProbeFailure("auth_env_missing_or_unsafe")
    values = dict(line.split("=", 1) for line in ENV_FILE.read_text().splitlines() if line)
    if set(values) != KEYS or any(not KEY_PATTERN.fullmatch(value) for value in values.values()):
        raise day3.ProbeFailure("auth_env_invalid")
    if len(set(values.values())) != 2:
        raise day3.ProbeFailure("auth_keys_must_differ")
    return {**day3.environment(), **values, "TRACEAGENT_UPLOAD_ROOT": str(UPLOADS)}


def seed(env: dict[str, str]) -> dict:
    os.environ["TRACEAGENT_DB_URL"] = env["TRACEAGENT_DB_URL"]
    with open_database(load_database_settings()) as database:
        with database.sessions.begin() as session:
            for label, identifier in (("A", day3.USER_A), ("B", day3.USER_B)):
                digest = key_digest(env["TRACEAGENT_SEED_API_KEY_" + label])
                user = session.get(User, identifier, with_for_update=True)
                if user is None:
                    user = User(id=identifier, api_key_digest=digest)
                    session.add(user)
                elif user.api_key_digest is None:
                    user.api_key_digest = digest
                elif not secrets.compare_digest(user.api_key_digest, digest):
                    raise day3.ProbeFailure("seed_key_conflict")
                if user.active is False:
                    raise day3.ProbeFailure("seed_user_disabled")
    return {"status": "seeded", "users": 2, "database_stores_digests_only": True}


def pdf_fixture() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 25 100 Td (Synthetic TraceAgent upload fixture.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def metadata(label: str = "txt") -> dict[str, str]:
    return {
        "source_id": "synthetic-day4-" + label,
        "title": "Synthetic upload " + label,
        "source_url": "https://example.org/traceagent/" + label,
        "license": "Synthetic project fixture",
        "license_evidence_url": "https://example.org/traceagent/license",
        "publication_year": "",
        "version_label": "fixture-v1",
    }


def record(env: dict[str, str], identifier: str, owner: str = "A"):
    os.environ["TRACEAGENT_DB_URL"] = env["TRACEAGENT_DB_URL"]
    with open_database(load_database_settings()) as database:
        result = DocumentService(database.sessions).get(
            day3.USER_A if owner == "A" else day3.USER_B, UUID(identifier)
        )
        if result is None or result.stored_file is None:
            raise day3.ProbeFailure("uploaded_document_not_found")
        FileStore(UPLOADS).verify(result.stored_file, result.metadata.file_sha256)
        return result


@contextmanager
def api_server(env: dict[str, str]):
    with httpx.Client(timeout=2, trust_env=False) as client:
        try:
            client.get(ORIGIN + "/health")
        except httpx.HTTPError:
            pass
        else:
            raise day3.ProbeFailure("demo_port_already_in_use")
    with (CACHE / "api.log").open("ab") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--no-access-log",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(timeout=2, trust_env=False) as client:
                for _ in range(80):
                    if process.poll() is not None:
                        raise day3.ProbeFailure("api_start_failed")
                    try:
                        if client.get(ORIGIN + "/health").json() == {"status": "ok"}:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise day3.ProbeFailure("api_start_timeout")
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def demo(env: dict[str, str]) -> dict:
    checks = {}
    headers_a = {"X-API-Key": env["TRACEAGENT_SEED_API_KEY_A"]}
    headers_b = {"X-API-Key": env["TRACEAGENT_SEED_API_KEY_B"]}
    text = b"Synthetic TraceAgent evidence-retrieval upload. No patient data.\n"
    with api_server(env), httpx.Client(base_url=ORIGIN, timeout=15, trust_env=False) as client:

        def upload(headers, label, data, kind, fields=None):
            return client.post(
                "/documents",
                headers=headers,
                data=fields or metadata(label),
                files={"file": ("../../untrusted-name", data, kind)},
            )

        first = upload(headers_a, "txt", text, "text/plain")
        if first.status_code != 202:
            raise day3.ProbeFailure("initial_upload_failed")
        accepted = first.json()
        identifier = accepted["document_id"]
        status = client.get(f"/documents/{identifier}/status", headers=headers_a)
        checks["real_http_upload_and_status"] = status.status_code == 200 and status.json() == {
            "document_id": identifier,
            "status": "UPLOADED",
            "index_version": None,
            "error": None,
        }
        checks["repeat_reuses_document_and_job"] = (
            upload(headers_a, "txt", text, "text/plain").json() == accepted
        )
        forbidden = client.get(f"/documents/{identifier}/status", headers=headers_b)
        absent = client.get(
            "/documents/ffffffff-ffff-4fff-8fff-ffffffffffff/status", headers=headers_b
        )
        checks["owner_isolation_same_404"] = (
            forbidden.status_code == absent.status_code == 404 and forbidden.json() == absent.json()
        )
        other = upload(headers_b, "txt", text, "text/plain")
        checks["other_owner_independent_upload"] = (
            other.status_code == 202 and other.json()["document_id"] != identifier
        )
        conflict = upload(
            headers_a, "txt", text, "text/plain", {**metadata(), "title": "Conflicting source"}
        )
        checks["metadata_conflict_409"] = conflict.status_code == 409
        checks["missing_key_401"] = upload({}, "txt", text, "text/plain").status_code == 401
        checks["invalid_key_401"] = (
            upload({"X-API-Key": "invalid"}, "txt", text, "text/plain").status_code == 401
        )
        checks["unsupported_type_415"] = (
            upload(headers_a, "txt", text, "image/png").status_code == 415
        )
        checks["invalid_utf8_422"] = (
            upload(headers_a, "txt", b"\xff", "text/plain").status_code == 422
        )
        checks["empty_file_422"] = upload(headers_a, "txt", b"", "text/plain").status_code == 422
        checks["client_owner_rejected_422"] = (
            upload(
                headers_a, "txt", text, "text/plain", {**metadata(), "owner_id": str(day3.USER_B)}
            ).status_code
            == 422
        )
        pdf = upload(headers_a, "pdf", pdf_fixture(), "application/pdf")
        checks["real_text_pdf_upload"] = pdf.status_code == 202
        limit = b"%PDF-1.7\n" + b" " * (5 * 1024 * 1024 - 9)
        # This checks upload size acceptance only, not complete PDF validity.
        checks["exact_5_mib_size_boundary"] = (
            upload(headers_a, "size", limit, "application/pdf").status_code == 202
        )
        checks["one_byte_over_limit_413"] = (
            upload(headers_a, "size", limit + b"x", "application/pdf").status_code == 413
        )
        stored = record(env, identifier)
        checks["bytes_hash_and_unknown_year"] = (
            stored.metadata.file_sha256 == hashlib.sha256(text).hexdigest()
            and stored.metadata.publication_year is None
        )
        child = json.loads(
            day3.checked(
                [sys.executable, "-m", "scripts.day4", "read", identifier], env, timeout=15
            )
        )
        checks["independent_process_reads_file_and_database"] = child["status"] == "verified"
    with api_server(env), httpx.Client(base_url=ORIGIN, timeout=10, trust_env=False) as client:
        after = client.get(f"/documents/{identifier}/status", headers=headers_a)
        checks["api_restart_preserves_status"] = (
            after.status_code == 200 and after.json() == status.json()
        )
    result = {
        "status": "passed" if all(checks.values()) else "failed",
        "synthetic": True,
        "transport": "real_loopback_http",
        "registration": accepted,
        "checks": checks,
    }
    (CACHE / "demo.json").write_text(json.dumps(result, indent=2) + "\n")
    if not all(checks.values()):
        raise day3.ProbeFailure("day4_demo_failed")
    return result


def shared_check(env: dict[str, str]) -> dict:
    demo_result = json.loads((CACHE / "demo.json").read_text())
    stored = record(env, demo_result["registration"]["document_id"])
    command = [
        "docker",
        "run",
        "--rm",
        "--read-only",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--mount",
        f"type=bind,source={UPLOADS},target=/data/uploads,readonly",
        ALPINE,
    ]
    output = day3.checked(
        [*command, "sha256sum", "/data/uploads/" + stored.stored_file.key + ".bin"], env
    )
    refused = subprocess.run(
        [*command, "sh", "-c", "touch /data/uploads/write-must-fail"],
        env=env,
        cwd=ROOT,
        capture_output=True,
        timeout=30,
    )
    if (
        output.split()[0] != stored.metadata.file_sha256
        or refused.returncode == 0
        or (UPLOADS / "write-must-fail").exists()
    ):
        raise day3.ProbeFailure("shared_file_check_failed")
    return {
        "status": "passed",
        "container_reads_same_sha256": True,
        "read_only_write_refused": True,
        "network": "none",
        "host_mount": "only_project_upload_directory",
        "real_worker": "not_implemented_until_day6",
    }


def audit(env: dict[str, str]) -> dict:
    os.environ["TRACEAGENT_DB_URL"] = env["TRACEAGENT_DB_URL"]
    with open_database(load_database_settings()) as database:
        with database.sessions() as session:
            rows = session.execute(
                select(
                    Document.storage_key,
                    Document.file_size_bytes,
                    Document.file_media_type,
                    Document.file_sha256,
                ).where(Document.storage_key.is_not(None))
            ).all()
    from app.storage.files import FileReference

    referenced = {row.storage_key for row in rows}
    missing = []
    for row in rows:
        try:
            FileStore(UPLOADS).verify(
                FileReference(
                    key=row.storage_key,
                    size_bytes=row.file_size_bytes,
                    media_type=row.file_media_type,
                ),
                row.file_sha256,
            )
        except StorageUnavailable:
            missing.append(row.storage_key)
    result = {
        "status": "passed" if not missing else "failed",
        "read_only": True,
        "referenced_files": len(rows),
        "missing_or_corrupt": missing,
        "unreferenced_candidates": [
            path.stem for path in UPLOADS.glob("*.bin") if path.stem not in referenced
        ],
        "temporary_files": len(list(UPLOADS.glob("*.part"))),
    }
    (CACHE / "audit.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def offline_check(env: dict[str, str]) -> dict:
    identifier = json.loads((CACHE / "demo.json").read_text())["registration"]["document_id"]
    headers = {"X-API-Key": env["TRACEAGENT_SEED_API_KEY_A"]}
    with api_server(env), httpx.Client(base_url=ORIGIN, timeout=10, trust_env=False) as client:
        day3.compose(env, "stop", "database")
        try:
            started = time.monotonic()
            failed = client.get(f"/documents/{identifier}/status", headers=headers)
            elapsed = time.monotonic() - started
            health = client.get("/health")
            if (
                failed.status_code != 503
                or failed.json()["error"]["code"] != "database_unavailable"
                or health.status_code != 200
            ):
                raise day3.ProbeFailure("offline_http_behavior_failed")
        finally:
            day3.compose(env, "up", "-d", "--wait", "--wait-timeout", "60")
        # Recreate the server pool after a real stop, without claiming automatic pool recovery.
    with api_server(env), httpx.Client(base_url=ORIGIN, timeout=10, trust_env=False) as client:
        restored = client.get(f"/documents/{identifier}/status", headers=headers)
        if restored.status_code != 200:
            raise day3.ProbeFailure("restored_http_behavior_failed")
    return {
        "status": "passed",
        "real_database_stopped": True,
        "business_http": 503,
        "health_http": 200,
        "elapsed_seconds": round(elapsed, 3),
        "restored_after_api_restart": True,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "prepare",
            "seed",
            "serve",
            "demo",
            "test",
            "shared-check",
            "audit",
            "offline-check",
            "read",
        ],
    )
    parser.add_argument("identifier", nargs="?")
    parser.add_argument("--report-label", choices=["local", "isolated"], default="local")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare()
            result = {"status": "prepared", "credential_file": ".cache/day4/auth.env"}
        else:
            env = environment()
            if args.command == "seed":
                result = seed(env)
            elif args.command == "serve":
                import uvicorn

                os.environ.update(env)
                uvicorn.run("app.main:app", host="127.0.0.1", port=8000, access_log=False)
                return 0
            elif args.command == "test":
                output = day3.checked(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-q",
                        "--basetemp=.cache/pytest/day4",
                        f"--junitxml=.cache/day4/tests-{args.report_label}.xml",
                    ],
                    {**env, "TRACEAGENT_DATABASE_TESTS": "1", "TRACEAGENT_CONTAINER_TESTS": "1"},
                    timeout=240,
                )
                (CACHE / f"test-summary-{args.report_label}.txt").write_text(output)
                result = {
                    "status": "passed",
                    "summary": output.splitlines()[-1],
                    "report": f".cache/day4/tests-{args.report_label}.xml",
                }
            elif args.command == "read":
                if args.identifier is None:
                    raise day3.ProbeFailure("document_id_required")
                record(env, args.identifier)
                result = {"status": "verified", "independent_process": True}
            else:
                result = {
                    "demo": demo,
                    "shared-check": shared_check,
                    "audit": audit,
                    "offline-check": offline_check,
                }[args.command](env)
                (CACHE / (args.command + ".json")).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") != "failed" else 1
    except (
        ConfigurationError,
        DatabaseUnavailable,
        StorageUnavailable,
        day3.ProbeFailure,
    ) as error:
        print(json.dumps({"status": "failed", "error_code": str(error)}))
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "day4_probe_failed"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
