"""RQ adapter: fixed function, sanitized errors, own DB pool in each execution."""

import logging
import os
import subprocess
import sys
from uuid import UUID

from rq import Queue, SpawnWorker
from rq.serializers import JSONSerializer
from rq.timeouts import JobTimeoutException

from app.core.config import load_settings
from app.db.config import load_database_settings
from app.db.session import DatabaseUnavailable, open_database
from app.services.documents import ResourceNotFound
from app.services.ingestion import IngestionService
from app.services.tasks import TaskService
from app.storage.files import FileStore
from app.tasks.config import load_queue_settings
from app.tasks.queue import ParseQueue, redis_connection


def canonical_uuid(value):
    identifier = UUID(value)
    if str(identifier) != value:
        raise ValueError("canonical_id_required")
    return identifier


def execute_parse(value):
    """Only a canonical execution ID is trusted; owner/document come from the DB."""
    try:
        execution_id = canonical_uuid(value)
    except (ValueError, TypeError, AttributeError):
        return {"status": "ignored"}
    settings = load_settings()
    queue = ParseQueue(load_queue_settings())
    try:
        with open_database(load_database_settings()) as database:
            service = TaskService(database.sessions, queue)
            try:
                identity = service.claim(execution_id)
            except ResourceNotFound:
                return {"status": "ignored"}
            if identity is None:
                return {"status": "ignored"}
            owner_id, document_id, job_id = identity
            try:
                outcome = IngestionService(
                    database.sessions, FileStore(settings.upload_root), settings
                ).parse(owner_id, document_id, execution_id=execution_id)
                return {"status": outcome.status, "chunks": outcome.chunks, "error": outcome.error}
            except DatabaseUnavailable:
                # Do not claim a failure was saved. Reconciliation observes commit/deadline.
                raise RuntimeError("database_unavailable") from None
            except JobTimeoutException:
                service.fail(owner_id, job_id, execution_id, "task_timeout")
                return {"status": "FAILED", "error": "task_timeout"}
            except Exception:
                service.fail(owner_id, job_id, execution_id, "task_execution_failed")
                return {"status": "FAILED", "error": "task_execution_failed"}
    finally:
        queue.close()


class SafeSpawnWorker(SpawnWorker):
    def serialize(self):
        from app.services.readiness import service_scope

        return {
            **super().serialize(),
            "traceagent_protocol": "parse-v1-json",
            "traceagent_scope": service_scope(
                load_database_settings().url.get_secret_value(), load_settings().upload_root
            ),
        }

    def fork_work_horse(self, job, queue):
        # Upstream SpawnWorker embeds Redis kwargs (including password) in python -c.
        # A fixed module receives only public execution IDs in argv instead.
        allowed = {
            "PATH",
            "LANG",
            "TMPDIR",
            "PYTHONPYCACHEPREFIX",
            "PYTHONPATH",
            "TRACEAGENT_DB_URL",
            "TRACEAGENT_QUEUE_URL",
            "TRACEAGENT_QUEUE_NAME",
            "TRACEAGENT_QUEUE_EXECUTION_TIMEOUT_SECONDS",
            "TRACEAGENT_QUEUE_WAIT_TIMEOUT_SECONDS",
            "TRACEAGENT_UPLOAD_ROOT",
            "TRACEAGENT_PARSE_TIMEOUT_SECONDS",
            "TRACEAGENT_MAX_FILE_BYTES",
            "TRACEAGENT_MAX_PDF_PAGES",
            "TRACEAGENT_MAX_TEXT_CHARACTERS",
        }
        child_env = {key: value for key, value in os.environ.items() if key in allowed}
        self._horse_process = subprocess.Popen(
            [sys.executable, "-m", "app.tasks.horse", self.name, job.id, self.execution.id],
            env=child_env,
            start_new_session=True,
        )
        self._horse_pid = self._horse_process.pid

    def handle_exception(self, job, *exc_info):
        # RQ's default handler logs full traceback and arguments.
        self.log.error("task_execution_failed")

    def handle_job_failure(self, job, queue, started_job_registry=None, exc_string=""):
        return super().handle_job_failure(
            job, queue, started_job_registry, exc_string="task_execution_failed"
        )


class SafeFormatter(logging.Formatter):
    def format(self, record):
        # Also covers library connection errors before a job is deserialized.
        return f"worker_event level={record.levelname}"


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter())
    logging.basicConfig(handlers=[handler], level=logging.WARNING, force=True)


def run_worker(*, burst=False):
    configure_logging()
    settings = load_queue_settings()
    connection = redis_connection(settings, worker=True)
    try:
        queue = Queue(settings.name, connection=connection, serializer=JSONSerializer)
        worker = SafeSpawnWorker(
            [queue],
            connection=connection,
            serializer=JSONSerializer,
            worker_ttl=20,
            job_monitoring_interval=2,
            maintenance_interval=2,
        )
        worker.work(burst=burst, with_scheduler=False, logging_level="WARNING")
    finally:
        connection.close()
