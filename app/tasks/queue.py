"""Bounded JSON-only RQ delivery. PostgreSQL owns the durable business state."""

from uuid import UUID

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
from rq import Queue
from rq.exceptions import DuplicateJobError, NoSuchJobError
from rq.job import Job
from rq.serializers import JSONSerializer

from app.tasks.config import QueueSettings


class QueueUnavailable(Exception):
    def __init__(self, job_id=None):
        super().__init__("queue_unavailable")
        self.job_id = job_id


def redis_connection(settings: QueueSettings, *, worker=False):
    return Redis.from_url(
        settings.url.get_secret_value(),
        socket_connect_timeout=0.25,
        # Worker blocks for up to five seconds on BLPOP. Producers/probes must not.
        socket_timeout=7 if worker else 0.25,
        retry=Retry(NoBackoff(), 0),
        max_connections=4,
        decode_responses=False,
    )


def rq_id(execution_id: UUID) -> str:
    return f"parse-{execution_id}"


class ParseQueue:
    def __init__(self, settings: QueueSettings):
        self.settings = settings
        self.connection = redis_connection(settings)
        self.queue = Queue(settings.name, connection=self.connection, serializer=JSONSerializer)

    def ensure(self, execution_id: UUID):
        try:
            try:
                job = Job.fetch(
                    rq_id(execution_id), connection=self.connection, serializer=JSONSerializer
                )
            except NoSuchJobError:
                job = None
            if job is not None:
                # Existing failed/finished transport records are not blindly executed again.
                status = job.get_status()
                if status is None:
                    raise QueueUnavailable()
                return status.value
            try:
                self.queue.enqueue(
                    "app.tasks.worker.execute_parse",
                    str(execution_id),
                    job_id=rq_id(execution_id),
                    unique=True,
                    job_timeout=self.settings.execution_timeout_seconds,
                    ttl=self.settings.wait_timeout_seconds,
                    result_ttl=3600,
                    failure_ttl=3600,
                    description="parse_stage",
                    retry=None,
                )
            except DuplicateJobError:
                # A concurrent producer won. The worker still verifies DB ownership/token.
                return "existing"
            return "queued"
        except RedisError:
            raise QueueUnavailable() from None

    def close(self):
        self.connection.close()
