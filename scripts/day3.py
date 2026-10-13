"""Local database lifecycle and reproducible demonstrations; never prints credentials."""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from sqlalchemy.exc import IntegrityError

from app.core.config import ConfigurationError
from app.db.config import load_database_settings
from app.db.models import Document, Job, User
from app.db.session import DatabaseUnavailable, open_database
from app.services.documents import (
    DocumentMetadata,
    DocumentService,
    SourceMetadataConflict,
)

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/day3"
ENV_FILE = CACHE / "database.env"
USER_A = UUID("10000000-0000-4000-8000-000000000001")
USER_B = UUID("10000000-0000-4000-8000-000000000002")
ENV_KEYS = {
    "POSTGRES_PASSWORD",
    "TRACEAGENT_APP_PASSWORD",
    "TRACEAGENT_DB_URL",
    "TRACEAGENT_MIGRATION_URL",
    "TRACEAGENT_TEST_DB_URL",
}


class ProbeFailure(Exception):
    pass


def prepare() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    if ENV_FILE.is_symlink():
        raise ProbeFailure("database_env_symlink_rejected")
    if ENV_FILE.exists():
        if ENV_FILE.stat().st_mode & 0o077:
            raise ProbeFailure("database_env_permissions_invalid")
        return
    admin = secrets.token_hex(32)
    application = secrets.token_hex(32)
    values = {
        "POSTGRES_PASSWORD": admin,
        "TRACEAGENT_APP_PASSWORD": application,
        "TRACEAGENT_DB_URL": f"postgresql+psycopg://traceagent_app:{application}@127.0.0.1:55432/traceagent",
        "TRACEAGENT_MIGRATION_URL": f"postgresql+psycopg://postgres:{admin}@127.0.0.1:55432/traceagent",
        "TRACEAGENT_TEST_DB_URL": f"postgresql+psycopg://postgres:{admin}@127.0.0.1:55432/traceagent_test",
    }
    # Exclusive creation also prevents accidentally rotating an existing volume's password.
    descriptor = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")


def environment() -> dict[str, str]:
    if ENV_FILE.is_symlink() or not ENV_FILE.is_file() or ENV_FILE.stat().st_mode & 0o077:
        raise ProbeFailure("database_env_missing_or_unsafe")
    values = dict(line.split("=", 1) for line in ENV_FILE.read_text().splitlines() if line)
    if set(values) != ENV_KEYS or any(not value for value in values.values()):
        raise ProbeFailure("database_env_invalid")
    return {**os.environ, **values}


def checked(command: list[str], env: dict[str, str], *, timeout: int = 90) -> str:
    result = subprocess.run(
        command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        # Neither raw child stderr nor command arguments enter a public error.
        raise ProbeFailure("database_command_failed")
    return result.stdout


def compose(env: dict[str, str], *arguments: str) -> str:
    return checked(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-f",
            str(ROOT / "compose.yaml"),
            *arguments,
        ],
        env,
    )


def bootstrap(env: dict[str, str]) -> None:
    url = env["TRACEAGENT_MIGRATION_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(
        url,
        connect_timeout=3,
        autocommit=True,
        options="-c statement_timeout=10000 -c lock_timeout=1000",
    ) as connection:
        if not connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'traceagent_app'"
        ).fetchone():
            connection.execute(
                sql.SQL(
                    "CREATE ROLE traceagent_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                ).format(sql.Literal(env["TRACEAGENT_APP_PASSWORD"]))
            )
        if not connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = 'traceagent_test'"
        ).fetchone():
            connection.execute("CREATE DATABASE traceagent_test")
        connection.execute("GRANT CONNECT ON DATABASE traceagent TO traceagent_app")
        connection.execute("GRANT USAGE ON SCHEMA public TO traceagent_app")
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        connection.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON TABLES FROM traceagent_app"
        )
        connection.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM traceagent_app")
        for table in ("users", "documents", "chunks", "jobs"):
            if connection.execute("SELECT to_regclass(%s)", ("public." + table,)).fetchone()[0]:
                connection.execute(
                    sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO traceagent_app").format(
                        sql.Identifier("public", table)
                    )
                )


def migrate(env: dict[str, str]) -> None:
    admin_env = {**env, "TRACEAGENT_DB_URL": env["TRACEAGENT_MIGRATION_URL"]}
    checked([sys.executable, "-m", "alembic", "upgrade", "head"], admin_env)
    checked([sys.executable, "-m", "alembic", "check"], admin_env)
    bootstrap(env)


def synthetic_metadata(**overrides: object) -> DocumentMetadata:
    return DocumentMetadata.model_validate(
        {
            "source_id": "synthetic-day3",
            "title": "Synthetic TraceAgent storage demonstration",
            "source_url": "https://example.org/traceagent/synthetic",
            "license": "Synthetic project fixture",
            "license_evidence_url": "https://example.org/traceagent/synthetic-license",
            "publication_year": None,
            "version_label": "storage-v1",
            "file_sha256": hashlib.sha256(b"synthetic traceagent day3 storage demo").hexdigest(),
            "ingest_version": "day3-v1",
            **overrides,
        }
    )


def demo(env: dict[str, str]) -> dict[str, object]:
    os.environ["TRACEAGENT_DB_URL"] = env["TRACEAGENT_DB_URL"]
    with open_database(load_database_settings()) as database:
        with database.sessions.begin() as session:
            for user_id in (USER_A, USER_B):
                if session.get(User, user_id) is None:
                    session.add(User(id=user_id))
        service = DocumentService(database.sessions)
        request = synthetic_metadata()
        registration = service.register(USER_A, request)
        repeated = service.register(USER_A, request)
        other_owner = service.register(USER_B, request)
        conflict = False
        try:
            service.register(USER_A, request.model_copy(update={"title": "conflicting title"}))
        except SourceMetadataConflict:
            conflict = True
        rollback_id = uuid4()
        try:
            with database.sessions.begin() as session:
                session.add(
                    Document(
                        id=rollback_id,
                        owner_id=USER_A,
                        **synthetic_metadata(
                            file_sha256=hashlib.sha256(rollback_id.bytes).hexdigest()
                        ).model_dump(),
                    )
                )
                session.flush()
                session.add(
                    Job(
                        id=uuid4(),
                        owner_id=USER_A,
                        document_id=rollback_id,
                        operation="ingest",
                        status="INVALID",
                    )
                )
                session.flush()
        except IntegrityError:
            pass
        rollback_clean = service.get(USER_A, rollback_id) is None
        child = json.loads(
            checked(
                [
                    sys.executable,
                    "-m",
                    "scripts.day3",
                    "read",
                    str(USER_A),
                    str(registration.document_id),
                ],
                env,
                timeout=15,
            )
        )
        result = {
            "status": "passed",
            "synthetic": True,
            "versions": database.probe(),
            "registration": asdict(registration),
            "checks": {
                "same_request_reuses_document_and_job": repeated.document_id
                == registration.document_id
                and repeated.job_id == registration.job_id,
                "different_owner_has_independent_record": other_owner.document_id
                != registration.document_id,
                "other_owner_cannot_read": service.get(USER_B, registration.document_id) is None,
                "source_metadata_conflict": conflict,
                "failed_job_rolls_back_document": rollback_clean,
                "new_process_reads_committed_document": child["found"]
                and child["document_id"] == str(registration.document_id),
            },
        }
        if not all(result["checks"].values()):
            raise ProbeFailure("database_demo_failed")
    (CACHE / "demo.json").write_text(json.dumps(result, default=str, indent=2) + "\n")
    return result


def test(env: dict[str, str]) -> dict[str, object]:
    test_env = {**env, "TRACEAGENT_DATABASE_TESTS": "1", "TRACEAGENT_CONTAINER_TESTS": "1"}
    output = checked(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--basetemp=.cache/pytest/day3",
            "--junitxml=.cache/day3/tests.xml",
        ],
        test_env,
        timeout=240,
    )
    (CACHE / "test-summary.txt").write_text(output)
    return {
        "status": "passed",
        "report": ".cache/day3/tests.xml",
        "summary": output.splitlines()[-1],
    }


def offline_check(env: dict[str, str]) -> dict[str, object]:
    compose(env, "stop", "database")
    started = time.monotonic()
    try:
        os.environ["TRACEAGENT_DB_URL"] = env["TRACEAGENT_DB_URL"]
        with open_database(load_database_settings()) as database:
            database.probe()
    except DatabaseUnavailable as error:
        if time.monotonic() - started > 5:
            raise ProbeFailure("database_failure_deadline_exceeded") from None
        result = {
            "status": "passed",
            "error_code": str(error),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "real_database_stopped": True,
        }
    else:
        raise ProbeFailure("stopped_database_was_reachable")
    finally:
        compose(env, "up", "-d", "--wait", "--wait-timeout", "60")
    (CACHE / "offline-check.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "prepare",
            "up",
            "migrate",
            "demo",
            "test",
            "offline-check",
            "probe",
            "down",
            "read",
        ],
    )
    parser.add_argument("ids", nargs="*")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare()
            result = {"status": "prepared", "credential_file": ".cache/day3/database.env"}
        else:
            env = environment()
            os.environ["TRACEAGENT_DB_URL"] = env["TRACEAGENT_DB_URL"]
            if args.command == "up":
                compose(env, "up", "-d", "--wait", "--wait-timeout", "60")
                bootstrap(env)
                result = {"status": "running"}
            elif args.command == "migrate":
                migrate(env)
                result = {"status": "migrated", "revision": "0002"}
            elif args.command == "demo":
                result = demo(env)
            elif args.command == "test":
                result = test(env)
            elif args.command == "offline-check":
                result = offline_check(env)
            elif args.command == "down":
                compose(env, "down")
                result = {"status": "stopped", "volume_preserved": True}
            else:
                with open_database(load_database_settings()) as database:
                    if args.command == "read":
                        if len(args.ids) != 2:
                            raise ProbeFailure("read_requires_owner_and_document")
                        record = DocumentService(database.sessions).get(
                            UUID(args.ids[0]), UUID(args.ids[1])
                        )
                        result = {
                            "found": record is not None,
                            "document_id": str(record.id) if record else None,
                        }
                    else:
                        result = {"status": "available", "versions": database.probe()}
        print(json.dumps(result, default=str, ensure_ascii=False, indent=2))
        return 0
    except (ConfigurationError, DatabaseUnavailable, ProbeFailure) as error:
        print(json.dumps({"status": "failed", "error_code": str(error)}))
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "day3_probe_failed"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
