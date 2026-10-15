"""Real Redis + PostgreSQL + spawned RQ worker acceptance, isolated DB/namespace only."""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from rq.job import Job as RQJob
from rq.serializers import JSONSerializer
from test_database import database as database
from test_database import owners as owners
from test_database import postgres as postgres
from test_day4_database import api as api
from test_day4_database import post
from test_day5 import pdf_pages

from app.db.models import Document, Job, ParseTask
from app.services.ingestion import IngestionService
from app.services.readiness import Readiness
from app.services.tasks import TaskService
from app.tasks.config import QueueSettings
from app.tasks.queue import ParseQueue, QueueUnavailable, rq_id
from scripts import day6

pytestmark = [
    pytest.mark.database,
    pytest.mark.skipif(
        os.environ.get("TRACEAGENT_QUEUE_TESTS") != "1",
        reason="requires isolated real Redis and spawned worker",
    ),
]


@pytest.fixture
def live(api, monkeypatch):
    _, app, _ = api
    raw = os.environ["TRACEAGENT_QUEUE_URL"]
    settings = QueueSettings(url=raw.removesuffix("/0") + "/1", name="traceagent-parse-test")
    queue = ParseQueue(settings)
    # Never erase the application broker DB, even if caller supplies a wrong URL.
    assert queue.connection.connection_pool.connection_kwargs["db"] == 1
    queue.connection.flushdb()
    service = TaskService(app.state.database.sessions, queue)
    app.state.tasks = service
    db_url = app.state.database.engine.url.render_as_string(hide_password=False)
    app.state.readiness = Readiness(db_url, app.state.settings.upload_root, settings)
    env = day6.RuntimeEnvironment(
        {
            **os.environ,
            "TRACEAGENT_DB_URL": db_url,
            "TRACEAGENT_QUEUE_URL": settings.url.get_secret_value(),
            "TRACEAGENT_QUEUE_NAME": settings.name,
            "TRACEAGENT_UPLOAD_ROOT": str(app.state.settings.upload_root),
        }
    )
    yield api, queue, service, env
    app.state.tasks = None
    queue.close()


def snapshot(app, job_id):
    with app.state.database.sessions() as session:
        task = session.get(ParseTask, job_id)
        return task.status, task.execution_id


def test_real_worker_json_parse_replay_and_heartbeat(live):
    (client, app, owners), queue, service, env = live
    assert client.get("/ready").status_code == 503
    response = post(client, data="中文\r\n证据\n".encode())
    job_id = UUID(response.json()["job_id"])
    assert response.status_code == 202 and snapshot(app, job_id)[0] == "QUEUED"
    with day6.worker_server(env) as worker:
        day6.wait_until(lambda: client.get("/ready").status_code == 200)
        day6.wait_until(lambda: snapshot(app, job_id)[0] == "SUCCEEDED")
        assert worker.poll() is None
        stage = service.inspect(owners[0], job_id)
        rq_job = RQJob.fetch(
            rq_id(stage.execution_id), connection=queue.connection, serializer=JSONSerializer
        )
        day6.wait_until(lambda: rq_job.get_status(refresh=True).value == "finished")
        assert rq_job.args == [str(stage.execution_id)]  # JSON arrays deserialize as lists.
        assert rq_job.retries_left is None
        with app.state.database.sessions() as session:
            doc = session.get(Document, UUID(response.json()["document_id"]))
            assert doc.status == "PARSED" and doc.index_version is None
            assert session.get(Job, job_id).status == "PENDING"
        before = service.inspect(owners[0], job_id)
        assert service.dispatch(owners[0], job_id) == before
    day6.wait_until(lambda: client.get("/ready").status_code == 503)
    assert client.get("/health").status_code == 200


def test_real_enqueue_ack_lost_http503_then_replay_recovers(live, monkeypatch):
    (client, app, owners), queue, service, env = live
    original = queue.ensure

    def lost(execution):
        original(execution)
        raise QueueUnavailable()

    with monkeypatch.context() as patch:
        patch.setattr(queue, "ensure", lost)
        failed = post(client)
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "queue_unavailable"
    job_id = UUID(failed.json()["job_id"])
    assert snapshot(app, job_id)[0] == "PENDING"
    retried = post(client)
    assert retried.status_code == 202 and retried.json()["job_id"] == str(job_id)
    assert queue.queue.count == 1
    with day6.worker_server(env):
        day6.wait_until(lambda: snapshot(app, job_id)[0] == "SUCCEEDED")
    assert service.inspect(owners[0], job_id).status == "SUCCEEDED"


def test_lost_test_broker_data_reconciles_same_execution(live):
    (client, app, owners), queue, service, env = live
    response = post(client)
    job_id = UUID(response.json()["job_id"])
    before = snapshot(app, job_id)
    queue.connection.flushdb()  # Guarded test DB 1, with no worker running.
    service.reconcile(owners[0])
    assert snapshot(app, job_id) == before and queue.queue.count == 1
    with day6.worker_server(env):
        day6.wait_until(lambda: snapshot(app, job_id)[0] == "SUCCEEDED")


def test_real_queue_connection_refusal_preserves_records_and_bounded_ready(live):
    (client, app, _), _, _, env = live
    settings = QueueSettings(
        url=env["TRACEAGENT_QUEUE_URL"].replace(":56379/", ":56380/"), name="traceagent-parse-test"
    )
    queue = ParseQueue(settings)
    app.state.tasks = TaskService(app.state.database.sessions, queue)
    try:
        failed = post(client)
        assert failed.status_code == 503
        assert snapshot(app, UUID(failed.json()["job_id"]))[0] == "PENDING"
        readiness = Readiness(env["TRACEAGENT_DB_URL"], app.state.settings.upload_root, settings)
        started = time.monotonic()
        assert asyncio.run(readiness.ready()) is False
        assert time.monotonic() - started < 1
    finally:
        queue.close()


def test_real_database_handshake_blackhole_is_cancelled_within_one_second(live):
    (_, app, _), _, _, env = live
    stopped = threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)

    def blackhole():
        with listener.accept()[0]:
            stopped.wait(2)

    thread = threading.Thread(target=blackhole, daemon=True)
    thread.start()
    # Deliberately never answer the PostgreSQL handshake; no real secret is sent.
    url = f"postgresql+psycopg://fixture:fixture@127.0.0.1:{listener.getsockname()[1]}/fixture"
    readiness = Readiness(url, app.state.settings.upload_root, day6.queue_settings(env))
    started = time.monotonic()
    try:
        assert asyncio.run(Readiness.bounded(readiness.database)) is False
        assert time.monotonic() - started < 1
    finally:
        stopped.set()
        thread.join(timeout=3)
        listener.close()


def test_worker_crash_after_claim_reconciles_to_failed_and_late_save_rejected(live):
    (client, app, owners), _, service, env = live
    # Claim in a real separate process, then die before doing any file work.
    response = post(client)
    job_id = UUID(response.json()["job_id"])
    task = service.inspect(owners[0], job_id)
    script = (
        "import os; from scripts.day6 import queue_settings; "
        "from app.db.config import load_database_settings; from app.db.session import open_database; "
        "from app.services.tasks import TaskService; from app.tasks.queue import ParseQueue; "
        "from uuid import UUID; "
        f"db=open_database(load_database_settings()); d=db.__enter__(); "
        f"assert TaskService(d.sessions,ParseQueue(queue_settings(os.environ))).claim(UUID('{task.execution_id}')); "
        "os._exit(17)"
    )
    process = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, timeout=10
    )
    assert process.returncode == 17 and snapshot(app, job_id)[0] == "RUNNING"
    with app.state.database.sessions.begin() as session:
        session.get(ParseTask, job_id).deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    assert service.reconcile(owners[0])[0].error == "task_timeout"
    ingest = IngestionService(app.state.database.sessions, app.state.files, app.state.settings)
    from app.services.ingestion import IngestConflict

    with pytest.raises(IngestConflict):
        ingest.parse(
            owners[0], UUID(response.json()["document_id"]), execution_id=task.execution_id
        )


def test_ready_rejects_worker_for_wrong_database_scope(live):
    (client, app, _), _, _, env = live
    with day6.worker_server(env):
        day6.wait_until(lambda: client.get("/ready").status_code == 200)
        wrong = Readiness(
            env["TRACEAGENT_DB_URL"],
            app.state.settings.upload_root / "wrong",
            day6.queue_settings(env),
        )
        assert asyncio.run(wrong.ready()) is False


def child_pid(parent):
    output = subprocess.run(
        ["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True
    ).stdout
    return next(
        (int(row.split()[0]) for row in output.splitlines() if int(row.split()[1]) == parent), None
    )


def no_running_process(pid):
    state = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
    ).stdout.strip()
    return not state or state.startswith("Z")


def stop_live_parser(live, *, timeout=300):
    (client, app, _), queue, _, env = live
    queue.settings = QueueSettings(
        url=env["TRACEAGENT_QUEUE_URL"],
        name=env["TRACEAGENT_QUEUE_NAME"],
        execution_timeout_seconds=timeout,
    )
    env["TRACEAGENT_QUEUE_EXECUTION_TIMEOUT_SECONDS"] = str(timeout)
    response = post(
        client, data=pdf_pages(*(["safe evidence " * 200] * 50)), media="application/pdf"
    )
    assert response.status_code == 202
    return UUID(response.json()["job_id"]), env


def test_actual_worker_sigkill_stops_horse_and_parser_and_heartbeat_expires(live):
    (_, app, owners), _, service, _ = live
    job_id, env = stop_live_parser(live)
    with day6.worker_server(env) as worker:
        day6.wait_until(lambda: snapshot(app, job_id)[0] == "RUNNING")
        horse = day6.wait_until(lambda: child_pid(worker.pid))
        parser = day6.wait_until(lambda: child_pid(horse))
        os.kill(parser, signal.SIGSTOP)
        try:
            worker.kill()
            worker.wait(timeout=5)
            day6.wait_until(
                lambda: no_running_process(horse) and no_running_process(parser), seconds=3
            )
        finally:
            if not no_running_process(parser):
                os.kill(parser, signal.SIGKILL)
    # Native RQ registration can outlive SIGKILL; freshness, not Redis PING, rejects it.
    day6.wait_until(lambda: asyncio.run(app.state.readiness.ready()) is False, seconds=12)
    with app.state.database.sessions.begin() as session:
        session.get(ParseTask, job_id).deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    assert service.reconcile(owners[0])[0].error == "task_timeout"


def test_real_rq_execution_timeout_kills_parser_and_persists_safe_failure(live):
    (_, app, _), _, _, _ = live
    job_id, env = stop_live_parser(live, timeout=2)
    with day6.worker_server(env) as worker:
        day6.wait_until(lambda: snapshot(app, job_id)[0] == "RUNNING")
        horse = day6.wait_until(lambda: child_pid(worker.pid))
        parser = day6.wait_until(lambda: child_pid(horse))
        os.kill(parser, signal.SIGSTOP)
        try:
            day6.wait_until(lambda: snapshot(app, job_id)[0] == "FAILED", seconds=6)
            day6.wait_until(lambda: no_running_process(parser), seconds=3)
            with app.state.database.sessions() as session:
                assert session.get(ParseTask, job_id).error_code == "task_timeout"
        finally:
            if not no_running_process(parser):
                os.kill(parser, signal.SIGKILL)


def test_real_worker_rejects_non_allowlisted_function_without_executing(live, tmp_path):
    _, queue, _, env = live
    marker = tmp_path / "must-not-exist"
    job = queue.queue.enqueue("os.system", f"touch {marker}", job_timeout=2, result_ttl=60)
    with day6.worker_server(env):
        day6.wait_until(lambda: job.get_status(refresh=True).value == "failed")
    job.refresh()
    assert not marker.exists() and job.latest_result().exc_string == "task_execution_failed"


@pytest.mark.parametrize("group", [False, True])
def test_orphan_watchdog_stops_child_when_parent_is_sigkilled(group):
    child_code = (
        "import os,time;from app.core.processes import watch_parent;"
        f"watch_parent(process_group={group});print(os.getpid(),flush=True);time.sleep(30)"
    )
    parent_code = (
        "import subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],start_new_session=True);"
        "time.sleep(30)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    child_pid = int(parent.stdout.readline().strip())
    try:
        parent.kill()
        parent.wait(timeout=5)

        def no_running_child():
            state = subprocess.run(
                ["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True
            ).stdout.strip()
            return not state or state.startswith("Z")

        day6.wait_until(no_running_child, seconds=3)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
