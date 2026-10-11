"""Small Day 1 probes, separate from the public API and future RAG platform."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path
from time import monotonic
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pypdf import PdfReader
from pypdf.errors import PdfReadError


class ProbeError(Exception):
    """Controlled error code; never forward provider bodies or exception strings."""


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=2000)

    @field_validator("query")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty query")
        return value.strip()


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: Literal["demo-1"]


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_evidence",
            "description": "Search synthetic research-method fixtures by query.",
            "parameters": SearchArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_source",
            "description": "Read synthetic source demo-1 when its ID is known.",
            "parameters": ReadArgs.model_json_schema(),
        },
    },
]


def dispatch_tool(function: dict) -> dict:
    """Two read-only fixtures. This does not claim real evidence retrieval."""
    if not isinstance(function, dict):
        raise ProbeError("invalid_tool_call")
    name = function.get("name")
    try:
        if name == "search_evidence":
            args = SearchArgs.model_validate(function.get("arguments"))
            return {"synthetic": True, "query": args.query, "source_ids": ["demo-1"]}
        if name == "read_source":
            args = ReadArgs.model_validate(function.get("arguments"))
            return {
                "synthetic": True,
                "source_id": args.source_id,
                "text": "Synthetic engineering fixture: the methods section describes eligibility criteria.",
            }
    except ValidationError:
        raise ProbeError("invalid_tool_arguments") from None
    raise ProbeError("unknown_tool")


class LocalModel:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        *,
        transport=None,
        chat_limit: int = 6,
        embed_limit: int = 2,
    ):
        address = urlparse(base_url)
        if address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ProbeError("local_endpoint_required")
        self.base_url, self.transport, self.closed = base_url, transport, False
        self.chat_limit, self.embed_limit = chat_limit, embed_limit
        self.chat_calls, self.embed_calls = 0, 0
        self.usage: list[dict] = []

    def close(self):
        self.closed = True

    def request(self, endpoint: str, payload: dict, kind: str) -> dict:
        if self.closed:
            raise ProbeError("model_client_closed")
        if kind not in {"chat", "embed"}:
            raise ProbeError("invalid_request_kind")
        counter = self.chat_calls if kind == "chat" else self.embed_calls
        limit = self.chat_limit if kind == "chat" else self.embed_limit
        if counter >= limit:
            raise ProbeError("call_budget_exhausted")
        # A failed request consumes budget too. No automatic retry in this probe.
        if kind == "chat":
            self.chat_calls += 1
        else:
            self.embed_calls += 1
        started = monotonic()

        async def send():
            # HTTPX phase timeouts alone are not a total wall-clock deadline.
            async with asyncio.timeout(30):
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(30),
                    transport=self.transport,
                    trust_env=False,
                ) as client:
                    return await client.post(endpoint, json=payload)

        try:
            response = asyncio.run(send())
            response.raise_for_status()
            result = response.json()
        except (TimeoutError, httpx.TimeoutException):
            raise ProbeError("model_timeout") from None
        except httpx.HTTPStatusError:
            raise ProbeError("model_http_error") from None
        except httpx.RequestError:
            raise ProbeError("model_unavailable") from None
        except ValueError:
            raise ProbeError("invalid_model_json") from None
        if not isinstance(result, dict):
            raise ProbeError("invalid_model_response")
        self.usage.append(
            {
                "kind": kind,
                "seconds": round(monotonic() - started, 3),
                "input_tokens": result.get("prompt_eval_count"),
                "output_tokens": result.get("eval_count"),
                "cloud_cost": 0,
            }
        )
        return result

    def chat(self, messages: list[dict], *, tools: bool = False) -> dict:
        if not messages or not str(messages[-1].get("content", "")).strip():
            raise ProbeError("empty_input")
        payload = {
            "model": "qwen3:1.7b",
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": "5m",
            "options": {"temperature": 0, "num_predict": 256, "num_ctx": 4096},
        }
        if tools:
            payload["tools"] = TOOLS
        result = self.request("/api/chat", payload, "chat")
        message = result.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ProbeError("invalid_model_message")
        content, calls = message.get("content"), message.get("tool_calls", [])
        if (
            not isinstance(content, str)
            or not isinstance(calls, list)
            or (not content.strip() and not calls)
        ):
            raise ProbeError("empty_or_invalid_model_output")
        if any(
            not isinstance(call, dict) or not isinstance(call.get("function"), dict)
            for call in calls
        ):
            raise ProbeError("invalid_tool_call")
        return message

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts or any(
            not isinstance(text, str) or not text.strip() or len(text) > 2000 for text in texts
        ):
            raise ProbeError("invalid_embedding_input")
        result = self.request(
            "/api/embed",
            {
                "model": "nomic-embed-text:v1.5",
                "input": texts,
                "truncate": False,
                "keep_alive": "5m",
            },
            "embed",
        )
        return validate_vectors(result.get("embeddings"), len(texts))


def validate_vectors(vectors, expected_count: int) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != expected_count or not vectors:
        raise ProbeError("invalid_embedding_count")
    dimension = len(vectors[0]) if isinstance(vectors[0], list) else 0
    if not dimension:
        raise ProbeError("empty_embedding")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimension:
            raise ProbeError("embedding_dimension_mismatch")
        if any(type(value) not in {int, float} or not math.isfinite(value) for value in vector):
            raise ProbeError("invalid_embedding_value")
        if not any(value != 0 for value in vector):
            raise ProbeError("zero_embedding")
    return vectors


def cosine(left: list[float], right: list[float]) -> float:
    validate_vectors([left, right], 2)
    numerator = sum(a * b for a, b in zip(left, right))
    denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return numerator / denominator


def extract_pdf(path: Path) -> dict:
    try:
        if path.stat().st_size > 5 * 1024 * 1024:
            raise ProbeError("pdf_too_large")
        raw = path.read_bytes()
        if not raw.startswith(b"%PDF-"):
            raise ProbeError("invalid_pdf")
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted:
            raise ProbeError("encrypted_pdf")
        if len(reader.pages) > 50:
            raise ProbeError("too_many_pages")
        pages, total = [], 0
        for number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            total += len(text)
            if total > 200_000:
                raise ProbeError("too_much_text")
            pages.append({"pdf_page": number, "text": text})
        if not any(page["text"].strip() for page in pages):
            raise ProbeError("no_extractable_text")
        return {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "page_count": len(pages),
            "characters": total,
            "pages": pages,
        }
    except (OSError, PdfReadError, ValueError, KeyError):
        raise ProbeError("pdf_parse_failed") from None


def save_report(path: Path, result: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
