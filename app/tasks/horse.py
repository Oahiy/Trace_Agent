"""Fixed spawn entry point: no connection strings or executable code in argv."""

import os
import sys

from rq import Queue
from rq.executions import Execution
from rq.job import Job
from rq.serializers import JSONSerializer

from app.core.processes import watch_parent
from app.tasks.config import load_queue_settings
from app.tasks.queue import redis_connection
from app.tasks.worker import SafeSpawnWorker, configure_logging


def main():
    watch_parent(process_group=True)
    configure_logging()
    settings = load_queue_settings()
    connection = redis_connection(settings)
    try:
        name, job_id, execution_id = sys.argv[1:]
        worker = SafeSpawnWorker.find_by_key(
            f"rq:worker:{name}", connection=connection, serializer=JSONSerializer
        )
        if worker is None:
            return 1
        job = Job.fetch(job_id, connection=connection, serializer=JSONSerializer)
        # JSON avoids pickle; this allowlist also prevents alternate function imports.
        if (
            job.func_name
            not in {"app.tasks.worker.execute_parse", "app.tasks.worker.execute_research"}
            or len(job.args) != 1
            or job.kwargs
            or job.origin != settings.name
        ):
            return 1
        worker.execution = Execution.fetch(execution_id, job.id, connection=connection)
        worker._is_horse = True
        worker.main_work_horse(
            job, Queue(settings.name, connection=connection, serializer=JSONSerializer)
        )
        return 0
    except Exception:
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    os._exit(main())
