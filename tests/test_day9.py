import asyncio
import json
import math
import sys
from dataclasses import FrozenInstanceError

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import Settings
from app.llm.budget import INPUT_LIMIT, CallBudget
from app.llm.contracts import CHAT, EMBED, Message, ModelFailure
from app.llm.ollama import MAX_RESPONSE_BYTES, OllamaAdapter, _retry_delay, validate_vectors
from scripts import day9 as runner


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: int


MESSAGES = [Message(role="user", content="Extract the integer seven.")]


def inventory():
    return {"models": [{"name": p.name, "digest": p.digest} for p in (CHAT, EMBED)]}


def generated(**overrides):
    return {
        "model": CHAT.name,
        "message": {"role": "assistant", "content": '{"value":7}'},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 100,
        "eval_count": 5,
        **overrides,
    }


def embedded(count=1):
    return {
        "model": EMBED.name,
        "embeddings": [[1.0] + [0.0] * 767 for _ in range(count)],
        "prompt_eval_count": 10,
    }


def transport(handler):
    async def dispatch(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=inventory())
        response = handler(request)
        return await response if hasattr(response, "__await__") else response

    return httpx.MockTransport(dispatch)


def execute(handler, operation=None, *, budget=None, timeout=30):
    budget = budget or CallBudget()

    async def run():
        async with OllamaAdapter(
            Settings(model_timeout_seconds=timeout), transport=transport(handler)
        ) as model:
            if operation:
                return await operation(model, budget)
            return await model.generate(MESSAGES, Output, budget=budget)

    return asyncio.run(run())


def test_generation_sends_schema_explicit_options_and_returns_typed_result():
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert request.url.host == "127.0.0.1"
        assert payload["format"] == Output.model_json_schema()
        assert payload["stream"] is False and payload["think"] is False
        assert payload["options"] == {"temperature": 0, "num_ctx": 4096, "num_predict": 1024}
        assert "schema" in payload["messages"][0]["content"]
        return httpx.Response(200, json=generated())

    budget = CallBudget()
    result = execute(handler, budget=budget)
    assert result.data == Output(value=7)
    assert result.usage.model_digest == CHAT.digest
    assert result.usage.input_tokens == 100 and result.usage.output_tokens == 5
    assert result.usage.charged_tokens == budget.generation_tokens_charged == 105
    assert result.usage.estimated_input_tokens > 100
    assert "heuristic-not-tokenizer" in result.usage.estimation_method
    assert result.usage.cloud_api_cost_usd == 0
    assert budget.generation_calls == 1 and len(seen) == 1
    assert "Extract the integer" not in json.dumps(budget.snapshot())
    with pytest.raises(FrozenInstanceError):
        result.usage.status = "changed"


@pytest.mark.parametrize("content", ["", "  ", "\ud800"])
def test_message_rejects_blank_or_surrogate(content):
    with pytest.raises(ValidationError):
        Message(role="user", content=content)


@pytest.mark.parametrize(
    "messages,schema,limit",
    [
        ([], Output, 1024),
        ([{"role": "user", "content": "raw dict"}], Output, 1024),
        ([Message(role="assistant", content="x")], Output, 1024),
        (MESSAGES * 17, Output, 1024),
        (MESSAGES, dict, 1024),
        (MESSAGES, Output, 0),
        (MESSAGES, Output, 1025),
        (MESSAGES, Output, True),
        ([Message(role="user", content="x" * 2800)], Output, 1024),
        ([Message(role="user", content="中文" * 500)], Output, 1024),
    ],
)
def test_invalid_generation_input_never_reaches_network(messages, schema, limit):
    async def run(model, budget):
        return await model.generate(messages, schema, budget=budget, max_output_tokens=limit)

    with pytest.raises(ModelFailure, match="invalid_model_input"):
        execute(lambda _: pytest.fail("invalid input reached inference"), run)


def test_schema_must_forbid_unknown_fields():
    class Loose(BaseModel):
        value: int

    async def run(model, budget):
        await model.generate(MESSAGES, Loose, budget=budget)

    with pytest.raises(ModelFailure, match="invalid_model_input"):
        execute(lambda _: pytest.fail("loose schema reached inference"), run)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"done": False}, "invalid_model_response"),
        ({"done": 1}, "invalid_model_response"),
        ({"done_reason": "length"}, "model_output_truncated"),
        ({"done_reason": "unknown"}, "invalid_model_response"),
        ({"model": "different-model"}, "invalid_model_response"),
        ({"error": "private provider body"}, "invalid_model_response"),
        ({"message": {"role": "user", "content": '{"value":7}'}}, "invalid_model_response"),
        ({"message": {"role": "assistant", "content": " "}}, "invalid_model_response"),
        ({"message": {"role": "assistant", "content": 7}}, "invalid_model_response"),
        (
            {"message": {"role": "assistant", "content": "{}", "tool_calls": [{}]}},
            "invalid_model_response",
        ),
        ({"message": {"role": "assistant", "content": '{"value":"7"}'}}, "model_output_invalid"),
        (
            {"message": {"role": "assistant", "content": '{"value":7,"extra":0}'}},
            "model_output_invalid",
        ),
        ({"message": {"role": "assistant", "content": "{}"}}, "model_output_invalid"),
        (
            {"message": {"role": "assistant", "content": '{"value":1,"value":7}'}},
            "invalid_model_json",
        ),
        ({"message": {"role": "assistant", "content": '{"value":NaN}'}}, "invalid_model_json"),
        ({"message": {"role": "assistant", "content": "```json\n{}\n```"}}, "invalid_model_json"),
        ({"prompt_eval_count": True}, "invalid_model_usage"),
        ({"eval_count": -1}, "invalid_model_usage"),
        ({"eval_count": 1.5}, "invalid_model_usage"),
        ({"prompt_eval_count": 2801}, "model_usage_limit_exceeded"),
        ({"eval_count": 1025}, "model_usage_limit_exceeded"),
    ],
)
def test_bad_generation_responses_fail_once_and_preserve_usage(changes, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=generated(**changes))

    budget = CallBudget()
    with pytest.raises(ModelFailure) as failure:
        execute(handler, budget=budget)
    assert str(failure.value) == code
    assert len(requests) == budget.generation_calls == 1
    assert budget.records[0].error_code == code
    assert budget.records[0].status == "failed"
    assert budget.generation_tokens_charged > 0
    assert "private" not in json.dumps(budget.snapshot())


@pytest.mark.parametrize("raw", [b"not-json secret", b"[]", b'{"x":1,"x":2}', b"\xff"])
def test_malformed_envelope_never_leaks_body_or_retries(raw):
    budget = CallBudget()
    with pytest.raises(ModelFailure):
        execute(lambda _: httpx.Response(200, content=raw), budget=budget)
    assert budget.generation_calls == 1
    assert budget.generation_tokens_charged == 3824
    assert budget.records[0].http_status == 200
    assert "secret" not in json.dumps(budget.snapshot())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 301, 302, 307])
def test_permanent_http_failure_or_redirect_never_retries_or_exposes_body(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status, text="SQL secret patient record", headers={"location": "https://example.com"}
        )

    budget = CallBudget()
    with pytest.raises(ModelFailure, match="model_http_error"):
        execute(handler, budget=budget)
    assert len(requests) == 1
    assert budget.records[0].http_status == status
    assert budget.records[0].input_tokens is None
    assert budget.generation_tokens_charged == 3824
    assert "patient" not in json.dumps(budget.snapshot())


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_failure_retries_once_then_success(status):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, headers={"retry-after": "0"})
        return httpx.Response(200, json=generated())

    budget = CallBudget()
    assert execute(handler, budget=budget).data.value == 7
    assert len(requests) == budget.generation_calls == 2
    assert [r.status for r in budget.records] == ["failed", "succeeded"]
    assert budget.generation_tokens_charged == 3824 + 105


def test_repeated_transient_error_stops_at_two_requests():
    budget = CallBudget()
    with pytest.raises(ModelFailure, match="model_http_error"):
        execute(lambda _: httpx.Response(503), budget=budget)
    assert budget.generation_calls == 2
    assert budget.generation_tokens_charged == 2 * 3824


@pytest.mark.parametrize("header", ["60", "not-a-date", "-1", "9" * 101])
def test_retry_after_cannot_escape_shared_deadline(header):
    budget = CallBudget()
    with pytest.raises(ModelFailure, match="model_rate_limited"):
        execute(
            lambda _: httpx.Response(429, headers={"retry-after": header}),
            budget=budget,
            timeout=0.1,
        )
    assert budget.generation_calls == 1


def test_retry_after_supports_http_date_and_invalid_dates():
    assert _retry_delay("Wed, 21 Oct 2015 07:28:00 GMT") == 0
    assert math.isinf(_retry_delay("Wed, 21 Oct 2015 07:28:00"))


@pytest.mark.parametrize(
    "error", [httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout]
)
def test_transient_transport_errors_retry_and_are_sanitized(error):
    budget = CallBudget()

    def fail(request):
        raise error("private provider address and password", request=request)

    with pytest.raises(ModelFailure):
        execute(fail, budget=budget)
    assert budget.generation_calls == 2
    assert "password" not in json.dumps(budget.snapshot())


def test_nontransient_transport_error_is_not_retried():
    budget = CallBudget()

    def fail(request):
        raise httpx.UnsupportedProtocol("secret", request=request)

    with pytest.raises(ModelFailure, match="model_unavailable"):
        execute(fail, budget=budget)
    assert budget.generation_calls == 1


def test_total_deadline_cancels_request_and_leaves_no_orphan():
    cancelled = []

    async def slow(_):
        try:
            await asyncio.sleep(1)
        finally:
            cancelled.append(True)

    budget = CallBudget()
    with pytest.raises(ModelFailure, match="model_timeout"):
        execute(slow, budget=budget, timeout=0.02)
    assert cancelled == [True]
    assert budget.records[0].error_code == "model_timeout"
    assert budget.generation_calls == 1 and budget.generation_tokens_charged == 3824
    assert not budget._active


def test_retry_wait_and_second_request_share_total_deadline():
    requests = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        await asyncio.sleep(1)

    budget = CallBudget()
    with pytest.raises(ModelFailure, match="model_timeout"):
        execute(handler, budget=budget, timeout=0.08)
    assert len(requests) == 2
    assert budget.records[-1].error_code == "model_timeout"


def test_caller_cancellation_propagates_and_reserves_unknown_usage():
    async def run():
        entered = asyncio.Event()

        async def slow(_):
            entered.set()
            await asyncio.sleep(10)

        budget = CallBudget()
        async with OllamaAdapter(Settings(), transport=transport(slow)) as model:
            task = asyncio.create_task(model.generate(MESSAGES, Output, budget=budget))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert budget.records[0].error_code == "model_cancelled"
        assert budget.generation_tokens_charged == 3824 and not budget._active

    asyncio.run(run())


def test_missing_usage_stays_null_with_conservative_reservation():
    budget = CallBudget()
    result = execute(
        lambda _: httpx.Response(200, json=generated(prompt_eval_count=None, eval_count=None)),
        budget=budget,
    )
    assert result.usage.input_tokens is None and result.usage.output_tokens is None
    assert budget.generation_tokens_charged == 3824


def test_valid_usage_is_retained_when_output_validation_fails():
    budget = CallBudget()
    with pytest.raises(ModelFailure, match="model_output_invalid"):
        execute(
            lambda _: httpx.Response(
                200, json=generated(message={"role": "assistant", "content": "{}"})
            ),
            budget=budget,
        )
    assert budget.generation_tokens_charged == 105
    assert budget.records[0].input_tokens == 100


def test_budget_exhaustion_prevents_first_request_or_retry():
    for budget, code in [
        (CallBudget(token_limit=3823), "token_budget_exhausted"),
        (CallBudget(generation_limit=1), "call_budget_exhausted"),
    ]:
        with pytest.raises(ModelFailure, match=code):
            execute(lambda _: httpx.Response(503), budget=budget)
        assert budget.generation_calls == (0 if code == "token_budget_exhausted" else 1)


def test_manual_attempt_keeps_cumulative_token_and_call_history():
    budget = CallBudget()
    for _ in range(2):
        with pytest.raises(ModelFailure):
            execute(lambda _: httpx.Response(503), budget=budget)
    assert budget.generation_calls == 4
    budget.next_task_attempt()
    assert budget.attempt_generation_calls == 0
    with pytest.raises(ModelFailure):
        execute(lambda _: httpx.Response(503), budget=budget)
    assert budget.generation_calls == 6
    assert budget.generation_tokens_charged == 22944
    with pytest.raises(ModelFailure, match="token_budget_exhausted"):
        execute(lambda _: pytest.fail("budget bypass"), budget=budget)
    with pytest.raises(ModelFailure, match="attempt_budget_exhausted"):
        budget.next_task_attempt()
    assert [r.task_attempt for r in budget.records] == [1, 1, 1, 1, 2, 2]


def test_same_budget_cannot_run_concurrently_or_reset_during_call():
    budget = CallBudget()
    with budget.operation():
        with pytest.raises(ModelFailure, match="budget_in_use"):
            execute(lambda _: pytest.fail("concurrent request"), budget=budget)
        with pytest.raises(ModelFailure, match="budget_in_use"):
            budget.next_task_attempt()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"generation_limit": 5},
        {"generation_limit": True},
        {"embedding_limit": 0},
        {"token_limit": 24001},
    ],
)
def test_invalid_budget_configuration(kwargs):
    with pytest.raises(ModelFailure, match="invalid_model_configuration"):
        CallBudget(**kwargs)


def test_document_and_query_prefixes_order_and_metadata():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert body["truncate"] is False
        response = embedded(len(body["input"]))
        for i, vector in enumerate(response["embeddings"]):
            vector[0] = i + 1
        return httpx.Response(200, json=response)

    async def run(model, budget):
        docs = await model.embed_documents(["first", "second"], budget=budget)
        query = await model.embed_query("question", budget=budget)
        return docs, query

    budget = CallBudget()
    docs, query = execute(handler, run, budget=budget)
    assert seen[0]["input"] == ["search_document: first", "search_document: second"]
    assert seen[1]["input"] == ["search_query: question"]
    assert docs.vectors[0][0] == 1 and docs.vectors[1][0] == 2
    assert len(query.vectors[0]) == 768
    assert docs.profile.preprocessing_version == "nomic-task-prefix-v1"
    assert budget.embedding_calls == 2 and budget.embedding_tokens_charged == 20
    assert docs.usage.output_tokens is None and budget.generation_tokens_charged == 0


@pytest.mark.parametrize("texts", [[], [" "], [1], ["x" * 2001], ["\ud800"], ["x"] * 9, "text"])
def test_invalid_embedding_input_prevents_inference(texts):
    async def run(model, budget):
        await model.embed_documents(texts, budget=budget)

    with pytest.raises(ModelFailure, match="invalid_model_input"):
        execute(lambda _: pytest.fail("invalid embedding reached network"), run)


@pytest.mark.parametrize(
    "vectors,count,code",
    [
        ([], 1, "invalid_embedding_count"),
        ([[1] * 768], 2, "invalid_embedding_count"),
        ([[1] * 767], 1, "embedding_dimension_mismatch"),
        ([[1] * 769, [1] * 769], 2, "embedding_dimension_mismatch"),
        ([[True] + [0] * 767], 1, "invalid_embedding_value"),
        ([[float("nan")] + [0] * 767], 1, "invalid_embedding_value"),
        ([[float("inf")] + [0] * 767], 1, "invalid_embedding_value"),
        ([[10**400] + [0] * 767], 1, "invalid_embedding_value"),
        ([[0] * 768], 1, "zero_embedding"),
    ],
)
def test_vector_failures(vectors, count, code):
    with pytest.raises(ModelFailure, match=code):
        validate_vectors(vectors, count)


def test_embedding_failure_usage_and_call_cap():
    async def run(model, budget):
        await model.embed_query("question", budget=budget)

    budget = CallBudget(embedding_limit=1)
    with pytest.raises(ModelFailure, match="call_budget_exhausted"):
        execute(lambda _: httpx.Response(503), run, budget=budget)
    assert budget.embedding_calls == 1
    assert budget.embedding_tokens_charged > 0
    assert budget.records[0].output_tokens is None


@pytest.mark.parametrize(
    "body",
    [{"models": []}, {"models": [{"name": CHAT.name, "digest": "changed"}]}, {"models": None}],
)
def test_identity_failure_prevents_inference(body):
    async def run():
        def handler(request):
            assert request.url.path == "/api/tags"
            return httpx.Response(200, json=body)

        budget = CallBudget()
        async with OllamaAdapter(Settings(), transport=httpx.MockTransport(handler)) as model:
            with pytest.raises(ModelFailure, match="model_identity"):
                await model.generate(MESSAGES, Output, budget=budget)
        assert budget.generation_calls == 0

    asyncio.run(run())


def test_model_identity_drift_after_inference_rejects_result():
    async def run():
        tags = 0

        def handler(request):
            nonlocal tags
            if request.url.path == "/api/tags":
                tags += 1
                return httpx.Response(200, json=inventory() if tags == 1 else {"models": []})
            return httpx.Response(200, json=generated())

        budget = CallBudget()
        async with OllamaAdapter(Settings(), transport=httpx.MockTransport(handler)) as model:
            with pytest.raises(ModelFailure, match="model_identity_mismatch"):
                await model.generate(MESSAGES, Output, budget=budget)
        assert budget.records[0].input_tokens == 100
        assert budget.records[0].status == "failed"

    asyncio.run(run())


def test_response_size_limit_and_unexpected_error_are_safe():
    budget = CallBudget()
    with pytest.raises(ModelFailure, match="model_response_too_large"):
        execute(
            lambda _: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)), budget=budget
        )
    assert budget.generation_calls == 1

    def fail(_):
        raise RuntimeError("private SQL and key")

    with pytest.raises(ModelFailure) as error:
        execute(fail)
    assert str(error.value) == "model_failed"


def test_adapter_closes_resources_and_caps_legacy_timeout():
    async def run():
        model = OllamaAdapter(
            Settings(model_timeout_seconds=120), transport=transport(lambda _: None)
        )
        assert model.timeout == 30
        await model.aclose()
        with pytest.raises(ModelFailure, match="model_client_closed"):
            await model.generate(MESSAGES, Output, budget=CallBudget())
        with pytest.raises(ModelFailure, match="model_client_closed"):
            async with model:
                pass

    asyncio.run(run())


def test_error_allowlist_hides_unknown_details():
    assert str(ModelFailure("private unexpected exception")) == "model_failed"
    assert INPUT_LIMIT == 2800


def test_exact_input_estimate_boundary_and_one_byte_over():
    async def run(model, budget):
        initial = await model.generate([Message(role="user", content="x")], Output, budget=budget)
        length = 1 + INPUT_LIMIT - initial.usage.estimated_input_tokens
        boundary = await model.generate(
            [Message(role="user", content="x" * length)], Output, budget=budget
        )
        assert boundary.usage.estimated_input_tokens == 2800
        before = budget.generation_calls
        with pytest.raises(ModelFailure, match="invalid_model_input"):
            await model.generate(
                [Message(role="user", content="x" * (length + 1))], Output, budget=budget
            )
        assert budget.generation_calls == before

    execute(lambda _: httpx.Response(200, json=generated()), run)


def test_maximum_embedding_batch_and_text_length_are_accepted_without_truncation():
    async def run(model, budget):
        return await model.embed_documents(["x" * 2000] * 8, budget=budget)

    result = execute(lambda _: httpx.Response(200, json=embedded(8)), run)
    assert len(result.vectors) == 8


def test_streaming_response_cannot_keep_total_deadline_alive_with_small_chunks():
    closed = []

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield b" "

        async def aclose(self):
            closed.append(True)

    budget = CallBudget()
    with pytest.raises(ModelFailure, match="model_timeout"):
        execute(lambda _: httpx.Response(200, stream=SlowBody()), budget=budget, timeout=0.02)
    assert closed == [True]
    assert budget.generation_calls == 1


def test_unexpected_metadata_error_is_sanitized_before_inference():
    async def run():
        def fail(_):
            raise RuntimeError("private metadata secret")

        budget = CallBudget()
        async with OllamaAdapter(Settings(), transport=httpx.MockTransport(fail)) as model:
            with pytest.raises(ModelFailure) as error:
                await model.generate(MESSAGES, Output, budget=budget)
        assert str(error.value) == "model_failed"
        assert budget.generation_calls == 0 and not budget.records
        assert not budget._active

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure,code",
    [
        (ModelFailure("model_timeout"), "model_timeout"),
        (RuntimeError("private stack and token"), "day9_failed"),
    ],
)
def test_cli_failure_overwrites_stale_success_safely(tmp_path, monkeypatch, capsys, failure, code):
    root = tmp_path
    output = root / ".cache/day9"
    output.mkdir(parents=True)
    (output / "demo.json").write_text('{"status":"passed","stale":true}')

    async def fail():
        raise failure

    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "OUTPUT", output)
    monkeypatch.setattr(runner, "demo", fail)
    monkeypatch.setattr(sys, "argv", ["day9", "demo"])
    assert runner.main() == 1
    expected = {"status": "failed", "error_code": code}
    assert json.loads(capsys.readouterr().out) == expected
    assert json.loads((output / "demo.json").read_text()) == expected
    assert not list(output.glob(".report-*.tmp"))


def test_cli_success_report_and_write_failure_are_distinct(tmp_path, monkeypatch, capsys):
    async def success():
        return {"status": "passed", "synthetic": True}

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "OUTPUT", tmp_path / ".cache/day9")
    monkeypatch.setattr(runner, "demo", success)
    monkeypatch.setattr(sys, "argv", ["day9", "demo"])
    assert runner.main() == 0
    assert json.loads(capsys.readouterr().out)["synthetic"] is True
    monkeypatch.setattr(runner, "OUTPUT", tmp_path / "outside-cache")
    assert runner.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error_code": "report_write_failed",
    }
    assert not (tmp_path / "outside-cache").exists()


def test_cache_inventory_and_explicit_ignore_include_day9():
    root = runner.ROOT
    wrapper = (root / "scripts/dev.sh").read_text()
    ignore = (root / ".gitignore").read_text()
    assert '"day9_model_logs_reports_and_test_artifacts"' in wrapper
    assert "/.cache/manifest.json" in ignore and "/.cache/day9/" in ignore
