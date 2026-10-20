"""Real DB/Redis/spawned worker; a loopback HTTP substitute makes inference deterministic."""

import json
import os
import signal
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

import pytest
from rq.job import Job as RQJob
from rq.serializers import JSONSerializer
from test_database import database as database
from test_database import owners as owners
from test_database import postgres as postgres
from test_day4_database import KEY_A
from test_day4_database import api as api
from test_day6_queue import child_pid, no_running_process
from test_day6_queue import live as live
from test_day11_database import payload, ready, research

from app.db.models import ResearchRun
from app.llm.contracts import CHAT, EMBED
from app.services.research import utcnow
from app.tasks.queue import QueueUnavailable, ResearchQueue
from scripts import day6

pytestmark = [
    pytest.mark.database,
    pytest.mark.skipif(
        os.environ.get("TRACEAGENT_QUEUE_TESTS") != "1",
        reason="requires real isolated DB/Redis and spawned worker",
    ),
]


@pytest.fixture
def inference_server():
    state = {"generation": 0, "embedding": 0, "mode": "answer"}
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, data):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.respond(
                200, {"models": [{"name": p.name, "digest": p.digest} for p in (CHAT, EMBED)]}
            )

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["content-length"])))
            if self.path == "/api/embed":
                state["embedding"] += 1
                self.respond(
                    200,
                    {
                        "model": EMBED.name,
                        "embeddings": [[1.0] + [0.0] * 767],
                        "prompt_eval_count": 10,
                    },
                )
                return
            state["generation"] += 1
            entered.set()
            if state["mode"] == "retry" and state["generation"] == 1:
                self.respond(503, {"error": "private-provider-marker"})
                return
            if state["mode"] == "hold":
                release.wait(4)
            assert "E1" in json.loads(data["messages"][-1]["content"])["evidence"]
            content = json.dumps({"evidence_id": "E1", "sentence": 1})
            if state["mode"] == "invalid":
                content = '{"private":"private-provider-marker"}'
            self.respond(
                200,
                {
                    "model": CHAT.name,
                    "done": True,
                    "done_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                    "prompt_eval_count": 100,
                    "eval_count": 20,
                },
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, entered, release, f"http://127.0.0.1:{server.server_port}"
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def research_live(live, inference_server):
    api, parse_queue, parse_service, env = live
    client, app, owners = api
    app.state.tasks = None
    identifier = ready(api)
    app.state.tasks = parse_service
    queue = ResearchQueue(parse_queue.settings)
    app.state.research_queue = queue
    env["TRACEAGENT_LOCAL_MODEL_URL"] = inference_server[3]
    svc = research(app, queue)
    try:
        yield api, identifier, queue, svc, env, inference_server
    finally:
        app.state.research_queue = None
        queue.close()


def submit(client, identifier):
    return client.post("/research", headers={"X-API-Key": KEY_A}, json=payload(identifier))


def completed(svc, owner, job):
    value = svc.get(owner, UUID(job))
    return value if value["status"] in {"SUCCEEDED", "FAILED"} else None


def test_real_worker_http_retry_is_counted_in_one_durable_task_budget(research_live):
    (client, app, owners), identifier, queue, svc, env, model = research_live
    model[0]["mode"] = "retry"
    response = submit(client, identifier)
    assert response.status_code == 202 and response.json()["status"] == "QUEUED"
    job = response.json()["job_id"]
    with day6.worker_server(env):
        result = day6.wait_until(lambda: completed(svc, owners[0], job))
        assert result["status"] == "SUCCEEDED" and result["result"]["kind"] == "answer"
        assert client.get("/ready").status_code == 200
        assert submit(client, identifier).json()["job_id"] == job
    with app.state.database.sessions() as session:
        record = session.get(ResearchRun, UUID(job))
        assert record.budget["embedding_calls"] == 1 and record.budget["generation_calls"] == 2
        assert [r["status"] for r in record.budget["records"]] == [
            "succeeded",
            "failed",
            "succeeded",
        ]
        transport = RQJob.fetch(
            "research-" + str(record.execution_id),
            connection=queue.connection,
            serializer=JSONSerializer,
        )
        assert transport.func_name == "app.tasks.worker.execute_research"
        assert transport.args == [str(record.execution_id)] and transport.retries_left is None
    assert model[0]["generation"] == 2


def test_real_enqueue_ack_loss_recovers_same_task_without_duplicate_inference(
    research_live, monkeypatch
):
    (client, app, owners), identifier, queue, svc, env, model = research_live
    original = queue.ensure

    def lost(execution):
        original(execution)
        raise QueueUnavailable()

    with monkeypatch.context() as patch:
        patch.setattr(queue, "ensure", lost)
        failed = submit(client, identifier)
    assert failed.status_code == 503
    job = failed.json()["job_id"]
    assert svc.get(owners[0], UUID(job))["status"] == "PENDING"
    assert submit(client, identifier).json()["job_id"] == job
    assert queue.queue.count == 1
    with day6.worker_server(env):
        assert day6.wait_until(lambda: completed(svc, owners[0], job))["status"] == "SUCCEEDED"
    assert model[0]["generation"] == 1


def test_broker_loss_reconciles_same_execution_from_database(research_live):
    (client, app, owners), identifier, queue, svc, env, model = research_live
    response = submit(client, identifier)
    with app.state.database.sessions() as session:
        before = session.get(ResearchRun, UUID(response.json()["job_id"])).execution_id
    assert queue.connection.connection_pool.connection_kwargs["db"] == 1
    queue.connection.flushdb()
    svc.reconcile(owners[0])
    with day6.worker_server(env):
        assert (
            day6.wait_until(lambda: completed(svc, owners[0], response.json()["job_id"]))["status"]
            == "SUCCEEDED"
        )
    with app.state.database.sessions() as session:
        assert session.get(ResearchRun, UUID(response.json()["job_id"])).execution_id == before


def test_actual_worker_sigkill_preserves_reservations_until_explicit_timeout_reconcile(
    research_live,
):
    (client, app, owners), identifier, queue, svc, env, model = research_live
    model[0]["mode"] = "hold"
    job = submit(client, identifier).json()["job_id"]
    with day6.worker_server(env) as worker:
        assert model[1].wait(10)
        horse = day6.wait_until(lambda: child_pid(worker.pid))
        with app.state.database.sessions() as session:
            assert session.get(ResearchRun, UUID(job)).budget["generation_calls"] == 1
        worker.send_signal(signal.SIGKILL)
        worker.wait(timeout=5)
        day6.wait_until(lambda: no_running_process(horse), seconds=3)
    svc.clock = lambda: utcnow() + timedelta(seconds=181)
    result = svc.reconcile(owners[0])
    assert result[0]["status"] == "FAILED"
    assert svc.get(owners[0], UUID(job))["error"]["code"] == "research_timeout"
    with app.state.database.sessions() as session:
        record = session.get(ResearchRun, UUID(job))
        assert record.result is None and record.budget["generation_calls"] == 1
    model[2].set()


@pytest.mark.parametrize(
    "mode,expected", [("invalid", "research_output_invalid"), ("hold", "research_timeout")]
)
def test_real_worker_invalid_output_or_attempt_timeout_saves_safe_failure(
    research_live, mode, expected
):
    (client, app, owners), identifier, queue, svc, env, model = research_live
    model[0]["mode"] = mode
    env["TRACEAGENT_RESEARCH_TIMEOUT_SECONDS"] = "0.5" if mode == "hold" else "180"
    job = submit(client, identifier).json()["job_id"]
    with day6.worker_server(env):
        result = day6.wait_until(lambda: completed(svc, owners[0], job))
        assert result["status"] == "FAILED" and result["result"] is None
        assert result["error"]["code"] == expected
        assert "private-provider-marker" not in json.dumps(result)
    model[2].set()
