"""Fast checks use explicit substitutes; these do not prove a real worker executes."""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from redis.exceptions import TimeoutError as RedisTimeout
from rq.exceptions import DuplicateJobError, NoSuchJobError

from app.core.config import Settings
from app.main import create_app
from app.services.readiness import Readiness, fresh_worker
from app.tasks.config import QueueSettings
from app.tasks.queue import ParseQueue, QueueUnavailable
from app.tasks.worker import SafeFormatter, SafeSpawnWorker
from scripts import day6

URL = "redis://:" + "a" * 64 + "@127.0.0.1:56379/1"


@pytest.mark.parametrize(
    "change",
    [
        {"url": URL.replace("127.0.0.1", "example.org")},
        {"url": URL.replace("redis:", "http:")},
        {"url": URL + "?socket_timeout=900"},
        {"url": URL + "#fragment"},
        {"url": URL.replace("/1", "/9")},
        {"url": "redis://127.0.0.1:56379/1"},
        {"execution_timeout_seconds": 301},
        {"wait_timeout_seconds": 1801},
        {"name": "arbitrary-client-queue"},
    ],
)
def test_queue_config_rejects_unsafe_or_unbounded_settings(change):
    with pytest.raises(ValidationError):
        QueueSettings(**({"url": URL} | change))


def test_queue_config_accepts_local_authenticated_and_hides_secret():
    settings = QueueSettings(url=URL, name="traceagent-parse-test")
    assert "a" * 64 not in repr(settings)
    assert settings.execution_timeout_seconds == 300


def test_runtime_environment_repr_hides_values():
    assert "secret-marker" not in repr(day6.RuntimeEnvironment(TOKEN="secret-marker"))


def test_json_delivery_exact_payload_and_dedupe(monkeypatch):
    queue = ParseQueue(QueueSettings(url=URL))
    monkeypatch.setattr(
        "app.tasks.queue.Job.fetch", lambda *a, **kw: (_ for _ in ()).throw(NoSuchJobError())
    )
    calls = []
    monkeypatch.setattr(queue.queue, "enqueue", lambda *a, **kw: calls.append((a, kw)))
    execution = uuid4()
    assert queue.ensure(execution) == "queued"
    args, options = calls[0]
    assert args == ("app.tasks.worker.execute_parse", str(execution))
    assert options["unique"] is True and options["retry"] is None
    assert options["job_timeout"] == 300 and options["ttl"] == 1800
    assert "owner" not in json.dumps(calls) and "file" not in json.dumps(calls)
    monkeypatch.setattr(
        queue.queue, "enqueue", lambda *a, **kw: (_ for _ in ()).throw(DuplicateJobError())
    )
    assert queue.ensure(execution) == "existing"
    queue.close()


def test_queue_delivery_error_is_fixed_without_secret(monkeypatch):
    queue = ParseQueue(QueueSettings(url=URL))
    monkeypatch.setattr(
        "app.tasks.queue.Job.fetch", lambda *a, **kw: (_ for _ in ()).throw(RedisTimeout(URL))
    )
    with pytest.raises(QueueUnavailable, match="^queue_unavailable$"):
        queue.ensure(uuid4())
    queue.close()


def test_corrupt_transport_record_does_not_claim_queued(monkeypatch):
    queue = ParseQueue(QueueSettings(url=URL))
    monkeypatch.setattr(
        "app.tasks.queue.Job.fetch", lambda *a, **kw: SimpleNamespace(get_status=lambda: None)
    )
    with pytest.raises(QueueUnavailable):
        queue.ensure(uuid4())
    queue.close()


def test_startup_configuration_failure_closes_owned_database(tmp_path, monkeypatch):
    from app.core.config import ConfigurationError

    closed = []
    monkeypatch.setenv("TRACEAGENT_DB_URL", "present")
    monkeypatch.setattr("app.main.load_database_settings", lambda: None)
    monkeypatch.setattr(
        "app.main.Database", lambda _: SimpleNamespace(close=lambda: closed.append(True))
    )

    def invalid():
        raise ConfigurationError("invalid_queue_configuration")

    monkeypatch.setattr("app.main.load_queue_settings", invalid)
    with pytest.raises(ConfigurationError):
        with TestClient(create_app(Settings(upload_root=tmp_path / "uploads", queue_enabled=True))):
            pass
    assert closed == [True]


def test_worker_spawn_argv_contains_only_ids_and_no_inherited_api_keys(monkeypatch):
    monkeypatch.setenv("TRACEAGENT_QUEUE_URL", URL)
    monkeypatch.setenv("TRACEAGENT_SEED_API_KEY_A", "secret-key-marker")
    monkeypatch.setenv("TRACEAGENT_MAX_PDF_PAGES", "1")
    captures = []

    def spawn(*a, **kw):
        captures.append((a, kw))
        return SimpleNamespace(pid=1234)

    monkeypatch.setattr("app.tasks.worker.subprocess.Popen", spawn)
    worker = SimpleNamespace(name="worker-id", execution=SimpleNamespace(id="execution-id"))
    SafeSpawnWorker.fork_work_horse(worker, SimpleNamespace(id="job-id"), None)
    args, kwargs = captures[0]
    assert URL not in repr(args) and "secret-key-marker" not in repr(captures)
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["TRACEAGENT_QUEUE_URL"] == URL
    assert kwargs["env"]["TRACEAGENT_MAX_PDF_PAGES"] == "1"
    assert worker._horse_pid == 1234


def test_library_logging_never_formats_exception_or_arguments():
    record = logging.LogRecord(
        "rq",
        logging.ERROR,
        "private.py",
        1,
        "SQL password=%s",
        (URL,),
        (RuntimeError, RuntimeError(URL), None),
    )
    assert SafeFormatter().format(record) == "worker_event level=ERROR"


def worker_record(now):
    return {
        "last_heartbeat": now.isoformat(),
        "state": "idle",
        "queues": "traceagent-parse-test",
        "traceagent_protocol": "tasks-v2-json",
        "traceagent_scope": "scope",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "suspended"},
        {"queues": "different"},
        {"traceagent_scope": "wrong-db"},
        {"traceagent_protocol": "pickle"},
        {"death": "yes"},
        {"last_heartbeat": "invalid"},
        {"last_heartbeat": "2020-01-01T00:00:00+00:00"},
    ],
)
def test_worker_readiness_rejects_wrong_stale_or_dead(changes):
    now = datetime.now(UTC)
    assert not fresh_worker(worker_record(now) | changes, "traceagent-parse-test", "scope", now=now)


def test_worker_readiness_age_boundary_and_busy():
    now = datetime.now(UTC)
    record = worker_record(now - timedelta(seconds=10))
    assert fresh_worker(record, "traceagent-parse-test", "scope", now=now)
    assert fresh_worker(record | {"state": "busy"}, "traceagent-parse-test", "scope", now=now)
    assert not fresh_worker(
        worker_record(now + timedelta(seconds=1)), "traceagent-parse-test", "scope", now=now
    )


def test_readiness_cancels_stalled_check_within_one_second():
    cancelled = []

    async def blocked():
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.append(True)

    started = time.monotonic()
    assert asyncio.run(Readiness.bounded(blocked)) is False
    assert time.monotonic() - started < 1 and cancelled == [True]


def test_ready_disabled_is_safe_503_and_health_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACEAGENT_DB_URL", raising=False)
    with TestClient(create_app(Settings(upload_root=tmp_path / "uploads"))) as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.get("/ready")
        assert (
            response.status_code == 503
            and response.json()["error"]["code"] == "dependencies_unavailable"
        )


def test_isolated_demo_refuses_application_database():
    with pytest.raises(day6.day3.ProbeFailure, match="isolated_database_name_required"):
        day6.isolated_environment(
            {"TRACEAGENT_TEST_DB_URL": "postgresql+psycopg://u:p@127.0.0.1:1/traceagent"}
        )


def test_queue_credential_permissions_preserved_and_symlinks_rejected(tmp_path, monkeypatch):
    target = tmp_path / "queue.env"
    monkeypatch.setattr(day6, "CACHE", tmp_path)
    monkeypatch.setattr(day6, "ENV_FILE", target)
    day6.prepare()
    original = target.read_bytes()
    day6.prepare()
    assert target.read_bytes() == original and target.stat().st_mode & 0o777 == 0o600
    target.chmod(0o644)
    with pytest.raises(day6.day3.ProbeFailure):
        day6.prepare()
    target.unlink()
    target.symlink_to(tmp_path / "missing")
    with pytest.raises(day6.day3.ProbeFailure):
        day6.prepare()


def test_cache_inventory_registers_day6_and_is_ignored():
    root = Path(__file__).resolve().parents[1]
    inventory = json.loads((root / ".cache/manifest.json").read_text())
    assert inventory["day6_demo_uploads"]["database"] == "traceagent_test"
    assert "day6/" in inventory["day6_queue_credentials_logs_reports_and_isolated_environment"]
    assert "/.cache/manifest.json" in (root / ".gitignore").read_text()
    assert "/.cache/day6/" in (root / ".gitignore").read_text()
