"""Local queue lifecycle, safe inspection, isolated demo and reproducible checks."""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy.engine import make_url

from app.db.config import DatabaseSettings
from app.db.models import Job, ParseTask
from app.db.session import open_database
from app.services.auth import AuthService
from app.services.tasks import TaskService
from app.tasks.config import QueueSettings
from app.tasks.queue import ParseQueue
from app.tasks.worker import run_worker
from scripts import day3, day4

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/day6"
ENV_FILE = CACHE / "queue.env"


class RuntimeEnvironment(dict):
    def __repr__(self):
        return "<protected-runtime-environment>"


def prepare():
    CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if ENV_FILE.is_symlink():
        raise day3.ProbeFailure("queue_env_symlink_rejected")
    if ENV_FILE.exists():
        if ENV_FILE.stat().st_mode & 0o077:
            raise day3.ProbeFailure("queue_env_permissions_invalid")
        return
    descriptor = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(f"TRACEAGENT_REDIS_PASSWORD={secrets.token_hex(32)}\n")


def environment():
    if ENV_FILE.is_symlink() or not ENV_FILE.is_file() or ENV_FILE.stat().st_mode & 0o077:
        raise day3.ProbeFailure("queue_env_missing_or_unsafe")
    value = ENV_FILE.read_text().strip()
    if not re.fullmatch(r"TRACEAGENT_REDIS_PASSWORD=[0-9a-f]{64}", value):
        raise day3.ProbeFailure("queue_env_invalid")
    password = value.split("=", 1)[1]
    return RuntimeEnvironment(
        {
            **day4.environment(),
            "TRACEAGENT_REDIS_PASSWORD": password,
            "TRACEAGENT_QUEUE_URL": f"redis://:{password}@127.0.0.1:56379/0",
            "TRACEAGENT_QUEUE_NAME": "traceagent-parse",
            "TRACEAGENT_QUEUE_ENABLED": "true",
        }
    )


def isolated_environment(env):
    target = env["TRACEAGENT_TEST_DB_URL"]
    if make_url(target).database != "traceagent_test":
        raise day3.ProbeFailure("isolated_database_name_required")
    return RuntimeEnvironment(
        {
            **env,
            "TRACEAGENT_DB_URL": target,
            "TRACEAGENT_QUEUE_URL": env["TRACEAGENT_QUEUE_URL"].removesuffix("/0") + "/1",
            "TRACEAGENT_QUEUE_NAME": "traceagent-parse-test",
            "TRACEAGENT_UPLOAD_ROOT": str(CACHE / "uploads"),
        }
    )


def compose(env, *args):
    return day3.checked(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-f",
            str(ROOT / "compose.day6.yaml"),
            *args,
        ],
        env,
    )


def queue_settings(env):
    return QueueSettings(
        url=env["TRACEAGENT_QUEUE_URL"],
        name=env["TRACEAGENT_QUEUE_NAME"],
        execution_timeout_seconds=int(env.get("TRACEAGENT_QUEUE_EXECUTION_TIMEOUT_SECONDS", "300")),
        wait_timeout_seconds=int(env.get("TRACEAGENT_QUEUE_WAIT_TIMEOUT_SECONDS", "1800")),
    )


@contextmanager
def worker_server(env, *, burst=False):
    with (CACHE / "worker.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "scripts.day6", "worker", *(["--burst"] if burst else [])],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def wait_until(check, *, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise day3.ProbeFailure("day6_wait_timeout")


def demo(env):
    env = isolated_environment(env)
    # Only the isolated test DB is migrated/seeded. Existing app users/quotas stay intact.
    day3.checked([sys.executable, "-m", "alembic", "upgrade", "head"], env)
    day4.seed(env)
    checks, records = {}, []
    headers = {"X-API-Key": env["TRACEAGENT_SEED_API_KEY_A"]}
    with (
        day4.api_server(env),
        httpx.Client(base_url=day4.ORIGIN, timeout=5, trust_env=False) as client,
        open_database(DatabaseSettings(url=env["TRACEAGENT_DB_URL"])) as database,
    ):
        checks["health_without_worker"] = client.get("/health").status_code == 200
        checks["redis_without_worker_not_ready"] = client.get("/ready").status_code == 503
        with worker_server(env) as worker:
            wait_until(lambda: client.get("/ready").status_code == 200)
            checks["separate_real_worker_ready"] = worker.poll() is None
            fixtures = [
                (
                    "day6-txt",
                    "可追溯证据\r\n".encode() + b"TraceAgent research only. " * 60,
                    "text/plain",
                ),
                ("day6-pdf", day4.pdf_fixture(), "application/pdf"),
                ("day6-invalid-pdf", b"%PDF-1.7\nbroken", "application/pdf"),
            ]
            for label, data, media in fixtures:
                response = client.post(
                    "/documents",
                    headers=headers,
                    data=day4.metadata(label),
                    files={"file": (label, data, media)},
                )
                if response.status_code != 202:
                    raise day3.ProbeFailure("day6_upload_failed")
                accepted = response.json()

                def finished():
                    with database.sessions() as session:
                        task = session.get(ParseTask, UUID(accepted["job_id"]))
                        return task.status if task.status in {"SUCCEEDED", "FAILED"} else None

                status = wait_until(finished)
                public = client.get(
                    f"/documents/{accepted['document_id']}/status", headers=headers
                ).json()
                expected = "FAILED" if label.endswith("invalid-pdf") else "SUCCEEDED"
                checks[label] = status == expected and public["index_version"] is None
                if status == "SUCCEEDED":
                    checks[label + "_parsed_not_ready"] = public["status"] == "PARSED"
                with database.sessions() as session:
                    job = session.get(Job, UUID(accepted["job_id"]))
                    checks[label + "_parent_state"] = job.status == (
                        "FAILED" if status == "FAILED" else "PENDING"
                    )
                again = client.post(
                    "/documents",
                    headers=headers,
                    data=day4.metadata(label),
                    files={"file": (label, data, media)},
                )
                checks[label + "_replay"] = (
                    again.status_code == 202 and again.json()["job_id"] == accepted["job_id"]
                )
                forbidden = client.get(
                    f"/documents/{accepted['document_id']}/status",
                    headers={"X-API-Key": env["TRACEAGENT_SEED_API_KEY_B"]},
                )
                checks[label + "_owner_isolation"] = forbidden.status_code == 404
                records.append({**accepted, "parse_status": status, "public_status": public})
        wait_until(lambda: client.get("/ready").status_code == 503)
        checks["worker_stopped_not_ready"] = True
        checks["health_after_worker_stop"] = client.get("/health").status_code == 200
    result = {
        "status": "passed" if all(checks.values()) else "failed",
        "database": "traceagent_test",
        "real_http_redis_worker_postgresql": True,
        "checks": checks,
        "documents": records,
    }
    (CACHE / "demo.json").write_text(json.dumps(result, indent=2) + "\n")
    if not all(checks.values()):
        raise day3.ProbeFailure("day6_demo_failed")
    return result


def offline_check(env):
    env = isolated_environment(env)
    day3.checked([sys.executable, "-m", "alembic", "upgrade", "head"], env)
    day4.seed(env)
    checks, timings = {}, {}
    headers = {"X-API-Key": env["TRACEAGENT_SEED_API_KEY_A"]}
    fields = day4.metadata("day6-offline")
    data = b"Day 6 real broker outage, recovery and database outage."
    with (
        day4.api_server(env),
        httpx.Client(base_url=day4.ORIGIN, timeout=5, trust_env=False) as client,
    ):
        with worker_server(env):
            wait_until(lambda: client.get("/ready").status_code == 200)
            try:
                compose(env, "stop", "redis")
                started = time.monotonic()
                unavailable = client.get("/ready")
                timings["redis_down_ready_seconds"] = round(time.monotonic() - started, 4)
                failed = client.post(
                    "/documents",
                    headers=headers,
                    data=fields,
                    files={"file": ("fixture.txt", data, "text/plain")},
                )
                checks["real_redis_stopped_ready503_health200"] = (
                    unavailable.status_code == 503 and client.get("/health").status_code == 200
                )
                checks["upload503_keeps_job_id"] = (
                    failed.status_code == 503
                    and failed.json()["error"]["code"] == "queue_unavailable"
                    and bool(failed.json()["job_id"])
                )
                job_id = failed.json()["job_id"]
            finally:
                compose(env, "up", "-d", "--wait", "--wait-timeout", "60")
        with worker_server(env):
            wait_until(lambda: client.get("/ready").status_code == 200)
            retry = client.post(
                "/documents",
                headers=headers,
                data=fields,
                files={"file": ("fixture.txt", data, "text/plain")},
            )
            checks["same_upload_recovers_same_job"] = (
                retry.status_code == 202 and retry.json()["job_id"] == job_id
            )
            document_id = retry.json()["document_id"]
            wait_until(
                lambda: (
                    client.get(f"/documents/{document_id}/status", headers=headers)
                    .json()
                    .get("status")
                    == "PARSED"
                )
            )
            try:
                day3.compose(env, "stop", "database")
                started = time.monotonic()
                unavailable = client.get("/ready")
                timings["database_down_ready_seconds"] = round(time.monotonic() - started, 4)
                checks["real_database_stopped_ready503_health200"] = (
                    unavailable.status_code == 503 and client.get("/health").status_code == 200
                )
                checks["business503_during_database_outage"] = (
                    client.get(f"/documents/{document_id}/status", headers=headers).status_code
                    == 503
                )
            finally:
                day3.compose(env, "up", "-d", "--wait", "--wait-timeout", "60")
            wait_until(lambda: client.get("/ready").status_code == 200)
            checks["recovered_ready200"] = True
        checks["ready_deadlines"] = all(value < 3 for value in timings.values())
    result = {
        "status": "passed" if all(checks.values()) else "failed",
        "real_services_stopped_and_restored": True,
        "checks": checks,
        "timings": timings,
    }
    (CACHE / "offline.json").write_text(json.dumps(result, indent=2) + "\n")
    if not all(checks.values()):
        raise day3.ProbeFailure("day6_offline_failed")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "prepare",
            "up",
            "down",
            "worker",
            "serve",
            "test",
            "demo",
            "offline-check",
            "inspect",
            "reconcile",
            "audit",
        ],
    )
    parser.add_argument("identifier", nargs="?")
    parser.add_argument("--owner", choices=["A", "B"], default="A")
    parser.add_argument("--burst", action="store_true")
    parser.add_argument(
        "--isolated-test", action="store_true", help="Use guarded test DB/broker and Day 6 uploads"
    )
    parser.add_argument("--report-label", choices=["local", "isolated"], default="local")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare()
            result = {"status": "prepared", "credential_file": ".cache/day6/queue.env"}
        elif args.command == "worker":
            if args.isolated_test or not os.environ.get("TRACEAGENT_QUEUE_URL"):
                env = environment()
                os.environ.update(isolated_environment(env) if args.isolated_test else env)
            run_worker(burst=args.burst)
            result = {"status": "stopped"}
        elif args.command == "serve":
            import uvicorn

            env = environment()
            os.environ.update(isolated_environment(env) if args.isolated_test else env)
            uvicorn.run("app.main:app", host="127.0.0.1", port=8000, access_log=False)
            result = {"status": "stopped"}
        else:
            env = environment()
            if args.command in {"up", "down"}:
                compose(
                    env,
                    *(
                        ["up", "-d", "--wait", "--wait-timeout", "60"]
                        if args.command == "up"
                        else ["down"]
                    ),
                )
                result = {
                    "status": "running" if args.command == "up" else "stopped",
                    "volume_preserved": True,
                }
            elif args.command == "test":
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-q",
                        "--basetemp=.cache/pytest/day6",
                        f"--junitxml=.cache/day6/tests-{args.report_label}.xml",
                    ],
                    env={
                        **env,
                        "TRACEAGENT_QUEUE_ENABLED": "false",
                        "TRACEAGENT_DATABASE_TESTS": "1",
                        "TRACEAGENT_CONTAINER_TESTS": "1",
                        "TRACEAGENT_QUEUE_TESTS": "1",
                    },
                    cwd=ROOT,
                    timeout=300,
                    capture_output=True,
                    text=True,
                )
                output = completed.stdout + completed.stderr
                (CACHE / f"test-output-{args.report_label}.log").write_text(output)
                (CACHE / f"test-summary-{args.report_label}.txt").write_text(output)
                if completed.returncode:
                    raise day3.ProbeFailure("day6_tests_failed")
                result = {"status": "passed", "summary": output.splitlines()[-1]}
            elif args.command == "demo":
                result = demo(env)
            elif args.command == "offline-check":
                result = offline_check(env)
            elif args.command == "audit":
                result = day4.audit(env)
            else:
                if args.isolated_test:
                    env = isolated_environment(env)
                with open_database(DatabaseSettings(url=env["TRACEAGENT_DB_URL"])) as database:
                    principal = AuthService(database.sessions).authenticate(
                        env["TRACEAGENT_SEED_API_KEY_" + args.owner]
                    )
                    queue = ParseQueue(queue_settings(env))
                    try:
                        service = TaskService(database.sessions, queue)
                        if args.command == "inspect":
                            if args.identifier is None:
                                raise day3.ProbeFailure("job_id_required")
                            value = service.inspect(principal.owner_id, UUID(args.identifier))
                            result = asdict(value) if value else {"status": "no_parse_stage"}
                        else:
                            result = {
                                "stages": [asdict(v) for v in service.reconcile(principal.owner_id)]
                            }
                    finally:
                        queue.close()
        print(json.dumps(result, default=str, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        # Local commands also must not leak credentials, SQL or child stderr.
        print(json.dumps({"status": "failed", "error_code": "day6_command_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
