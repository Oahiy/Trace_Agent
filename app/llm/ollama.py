"""Bounded local Ollama adapter. Only JSON is returned; provider bodies are never logged."""

import asyncio
import json
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import monotonic

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.llm.budget import CONTEXT_SIZE, ESTIMATION_METHOD, INPUT_LIMIT, OUTPUT_LIMIT, CallBudget
from app.llm.contracts import (
    CHAT,
    EMBED,
    AttemptRecord,
    EmbeddingResult,
    GenerationResult,
    Message,
    ModelFailure,
)

MAX_RESPONSE_BYTES = 1024 * 1024
RETRY_STATUSES = {429, 502, 503, 504}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("nonfinite JSON")


def decode_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ModelFailure("invalid_model_json") from None


def _count(body, key):
    value = body.get(key)
    if value is not None and (type(value) is not int or value < 0):
        raise ModelFailure("invalid_model_usage")
    return value


def _retry_delay(header):
    if header is None:
        return 0.05
    try:
        if len(header) > 100:
            return math.inf
        if header.isascii() and header.isdigit():
            return float(header)
        target = parsedate_to_datetime(header)
        if target.tzinfo is None:
            return math.inf
        return max(0, (target - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return math.inf


def validate_vectors(raw, count):
    if not isinstance(raw, list) or len(raw) != count:
        raise ModelFailure("invalid_embedding_count")
    vectors = []
    for vector in raw:
        if not isinstance(vector, list) or len(vector) != EMBED.dimensions:
            raise ModelFailure("embedding_dimension_mismatch")
        try:
            invalid = any(type(v) not in {int, float} or not math.isfinite(v) for v in vector)
        except OverflowError:
            invalid = True
        if invalid:
            raise ModelFailure("invalid_embedding_value")
        if not any(v != 0 for v in vector):
            raise ModelFailure("zero_embedding")
        vectors.append(tuple(float(v) for v in vector))
    return tuple(vectors)


def generation_input(messages, schema):
    """One serialization/estimation rule shared by context packing and inference."""
    specification = schema.model_json_schema()
    encoded = json.dumps(specification, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 8192:
        raise ValueError("schema limit")
    wire_messages = [
        {"role": "system", "content": "Return only JSON matching this schema: " + encoded},
        *[m.model_dump() for m in messages],
    ]
    estimated = len(json.dumps(wire_messages, ensure_ascii=False).encode()) + 256
    return specification, wire_messages, estimated


class OllamaAdapter:
    def __init__(self, settings: Settings, *, transport=None):
        self.timeout = min(settings.model_timeout_seconds, 30.0)
        self._client = httpx.AsyncClient(
            base_url=settings.local_model_url,
            timeout=httpx.Timeout(self.timeout, connect=min(3, self.timeout)),
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def __aenter__(self):
        if self._client.is_closed:
            raise ModelFailure("model_client_closed")
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def aclose(self):
        await self._client.aclose()

    async def _read(self, method, endpoint, payload=None):
        async with self._client.stream(method, endpoint, json=payload) as response:
            if response.status_code != 200:
                return response.status_code, response.headers.get("retry-after"), None
            size, chunks = 0, []
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ModelFailure("model_response_too_large")
                chunks.append(chunk)
            return response.status_code, None, b"".join(chunks)

    async def _verify_profile(self, profile):
        try:
            status, _, raw = await self._read("GET", "/api/tags")
        except httpx.RequestError:
            raise ModelFailure("model_identity_unavailable") from None
        if status != 200:
            raise ModelFailure("model_identity_unavailable")
        body = decode_json(raw)
        if not isinstance(body, dict) or not isinstance(body.get("models"), list):
            raise ModelFailure("model_identity_unavailable")
        matches = [
            m for m in body["models"] if isinstance(m, dict) and m.get("name") == profile.name
        ]
        if len(matches) != 1 or matches[0].get("digest") != profile.digest:
            raise ModelFailure("model_identity_mismatch")

    async def _run(self, kind, payload, estimated, output_limit, budget, validate):
        if self._client.is_closed:
            raise ModelFailure("model_client_closed")
        if not isinstance(budget, CallBudget):
            raise ModelFailure("invalid_model_input")
        profile = CHAT if kind == "generation" else EMBED
        endpoint = "/api/chat" if kind == "generation" else "/api/embed"
        deadline = asyncio.get_running_loop().time() + self.timeout
        with budget.operation():
            try:
                async with asyncio.timeout_at(deadline):
                    for attempt in (1, 2):
                        budget.check(kind, output_limit)
                        await self._verify_profile(profile)
                        reserved = budget.reserve(kind, estimated, output_limit)
                        started = monotonic()
                        input_tokens = output_tokens = http_status = None
                        code, status, retryable, delay = None, "failed", False, 0.05
                        try:
                            http_status, header, raw = await self._read("POST", endpoint, payload)
                            if http_status != 200:
                                retryable = http_status in RETRY_STATUSES
                                delay = _retry_delay(header)
                                raise ModelFailure(
                                    "model_rate_limited"
                                    if http_status == 429
                                    else "model_http_error"
                                )
                            body = decode_json(raw)
                            if not isinstance(body, dict):
                                raise ModelFailure("invalid_model_response")
                            input_tokens = _count(body, "prompt_eval_count")
                            if kind == "generation":
                                output_tokens = _count(body, "eval_count")
                            if "error" in body or body.get("model") != profile.name:
                                raise ModelFailure("invalid_model_response")
                            if kind == "generation" and (
                                (input_tokens is not None and input_tokens > INPUT_LIMIT)
                                or (output_tokens is not None and output_tokens > output_limit)
                            ):
                                raise ModelFailure("model_usage_limit_exceeded")
                            value = validate(body)
                            await self._verify_profile(profile)
                            status = "succeeded"
                        except httpx.TimeoutException:
                            code, retryable = "model_timeout", True
                        except httpx.RequestError as error:
                            code = "model_unavailable"
                            retryable = isinstance(
                                error,
                                (
                                    httpx.ConnectError,
                                    httpx.ReadError,
                                    httpx.WriteError,
                                    httpx.RemoteProtocolError,
                                ),
                            )
                        except ModelFailure as error:
                            code = error.code
                        except asyncio.CancelledError:
                            code = (
                                "model_timeout"
                                if asyncio.get_running_loop().time() >= deadline
                                else "model_cancelled"
                            )
                            status = "cancelled"
                            raise
                        except Exception:
                            code = "model_failed"
                            raise ModelFailure(code) from None
                        finally:
                            charged = budget.settle(
                                kind, reserved, estimated, output_limit, input_tokens, output_tokens
                            )
                            record = AttemptRecord(
                                budget.task_attempt,
                                attempt,
                                kind,
                                profile.name,
                                profile.digest,
                                estimated,
                                ESTIMATION_METHOD,
                                input_tokens,
                                output_tokens,
                                charged,
                                round(monotonic() - started, 6),
                                status,
                                code,
                                http_status,
                            )
                            budget.records.append(record)
                        if status == "succeeded":
                            return value, record
                        if (
                            not retryable
                            or attempt == 2
                            or delay >= deadline - asyncio.get_running_loop().time()
                        ):
                            raise ModelFailure(code)
                        budget.check(kind, output_limit)
                        await asyncio.sleep(delay)
            except TimeoutError:
                raise ModelFailure("model_timeout") from None
            except ModelFailure:
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                # Metadata transports can fail before an inference record exists too.
                raise ModelFailure("model_failed") from None

    async def generate[T: BaseModel](
        self,
        messages: list[Message],
        schema: type[T],
        *,
        budget: CallBudget,
        max_output_tokens: int = OUTPUT_LIMIT,
    ) -> GenerationResult[T]:
        if (
            not isinstance(messages, list)
            or not 1 <= len(messages) <= 16
            or any(not isinstance(m, Message) for m in messages)
            or messages[-1].role != "user"
            or not isinstance(schema, type)
            or not issubclass(schema, BaseModel)
            or schema.model_config.get("extra") != "forbid"
            or type(max_output_tokens) is not int
            or not 1 <= max_output_tokens <= OUTPUT_LIMIT
        ):
            raise ModelFailure("invalid_model_input")
        try:
            specification, wire_messages, estimated = generation_input(messages, schema)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ModelFailure("invalid_model_input") from None
        if estimated > INPUT_LIMIT:
            raise ModelFailure("invalid_model_input")
        payload = {
            "model": CHAT.name,
            "messages": wire_messages,
            "format": specification,
            "stream": False,
            "think": False,
            "keep_alive": "5m",
            "options": {
                "temperature": 0,
                "num_ctx": CONTEXT_SIZE,
                "num_predict": max_output_tokens,
            },
        }

        def validate(body):
            if body.get("done") is not True:
                raise ModelFailure("invalid_model_response")
            if body.get("done_reason") == "length":
                raise ModelFailure("model_output_truncated")
            message = body.get("message")
            if (
                body.get("done_reason") != "stop"
                or not isinstance(message, dict)
                or message.get("role") != "assistant"
                or message.get("tool_calls")
                or not isinstance(message.get("content"), str)
                or not message["content"].strip()
            ):
                raise ModelFailure("invalid_model_response")
            data = decode_json(message["content"])
            try:
                return schema.model_validate(data, strict=True)
            except ValidationError:
                raise ModelFailure("model_output_invalid") from None

        value, record = await self._run(
            "generation", payload, estimated, max_output_tokens, budget, validate
        )
        return GenerationResult(value, record)

    async def _embed(self, texts, prefix, budget):
        if not isinstance(texts, list) or not 1 <= len(texts) <= 8:
            raise ModelFailure("invalid_model_input")
        if any(
            not isinstance(t, str)
            or not t.strip()
            or len(t) > 2000
            or any(0xD800 <= ord(c) <= 0xDFFF for c in t)
            for t in texts
        ):
            raise ModelFailure("invalid_model_input")
        inputs = [prefix + t for t in texts]
        estimated = sum(len(t.encode()) for t in inputs) + 32 * len(inputs)
        payload = {
            "model": EMBED.name,
            "input": inputs,
            "truncate": False,
            "keep_alive": "5m",
        }
        vectors, record = await self._run(
            "embedding",
            payload,
            estimated,
            0,
            budget,
            lambda body: validate_vectors(body.get("embeddings"), len(texts)),
        )
        return EmbeddingResult(vectors, EMBED, record)

    async def embed_documents(self, texts: list[str], *, budget: CallBudget) -> EmbeddingResult:
        return await self._embed(texts, "search_document: ", budget)

    async def embed_query(self, text: str, *, budget: CallBudget) -> EmbeddingResult:
        return await self._embed([text], "search_query: ", budget)
