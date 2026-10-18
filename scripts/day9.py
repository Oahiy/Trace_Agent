"""Small real-model acceptance experiment; no API, database, corpus seed or RAG evaluation."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import ConfigurationError, load_settings
from app.ingestion.parser import IsolatedParser
from app.llm.budget import CallBudget
from app.llm.contracts import CHAT, EMBED, Message, ModelFailure
from app.llm.ollama import OllamaAdapter
from scripts.day8 import confined_path, load_contracts

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".cache/day9"


class ExcerptSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    source_id: Literal["prisma-2020"]
    pdf_page: Literal[5]
    summary: str = Field(min_length=1, max_length=500)


class BoundaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: Literal["ok"]


def fixture(settings):
    corpus, _, lock = load_contracts()
    source = next(s for s in corpus.sources if s.source_id == "prisma-2020")
    path = confined_path(source.cache_file)
    if not path.is_file() or path.stat().st_size != source.bytes:
        raise ModelFailure("invalid_model_input")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != source.sha256:
        raise ModelFailure("invalid_model_input")
    parsed = IsolatedParser(settings).parse(raw, "application/pdf")
    page = parsed.pages[4].text
    position = page.lower().find("should not be used")
    if position < 0:
        raise ModelFailure("invalid_model_input")
    excerpt = page[max(0, position - 160) : position + 280]
    return source, excerpt, parsed.pages[0].text[:400], lock


def cosine(left, right):
    import math

    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    )


async def demo():
    settings = load_settings()
    source, excerpt, second, lock = fixture(settings)
    budget = CallBudget()
    async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
        response = await client.get(settings.local_model_url + "/api/version")
        response.raise_for_status()
        runtime = response.json()["version"]
        if not isinstance(runtime, str) or len(runtime) > 30:
            raise ModelFailure("invalid_model_response")
    async with OllamaAdapter(settings) as model:
        summary = await model.generate(
            [
                Message(
                    role="user",
                    content=(
                        "Summarize only this public methods excerpt in one short English sentence. "
                        "Preserve its stated limitation. No clinical inference. "
                        "source_id=prisma-2020; physical PDF page=5. Excerpt:\n" + excerpt
                    ),
                )
            ],
            ExcerptSummary,
            budget=budget,
            max_output_tokens=256,
        )
        documents = await model.embed_documents([excerpt, second], budget=budget)
        query = await model.embed_query(
            "Can PRISMA 2020 be used to assess methodological quality of systematic reviews?",
            budget=budget,
        )
        # Exercise a long allowed request, then reject an oversized one before inference.
        boundary = await model.generate(
            [Message(role="user", content="Return value ok. Ignore padding:\n" + "word " * 420)],
            BoundaryOutput,
            budget=budget,
            max_output_tokens=64,
        )
        before = budget.generation_calls
        try:
            await model.generate(
                [Message(role="user", content="x" * 2800)],
                BoundaryOutput,
                budget=budget,
                max_output_tokens=64,
            )
        except ModelFailure as error:
            if error.code != "invalid_model_input":
                raise
        else:
            raise ModelFailure("model_failed")
        if budget.generation_calls != before:
            raise ModelFailure("model_failed")
        # No actual request: assert the closed client fails safely after aclose.
        await model.aclose()
        try:
            await model.embed_query("question", budget=budget)
        except ModelFailure as error:
            if error.code != "model_client_closed":
                raise
        else:
            raise ModelFailure("model_failed")
    return {
        "status": "passed",
        "recorded_at": datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).isoformat(),
        "scope": "Day 9 adapter compatibility and budget experiments; not RAG or medical evaluation",
        "environment": {
            "ollama": runtime,
            "packages": {
                name: version(name) for name in ("httpx", "pydantic", "fastapi", "pytest")
            },
        },
        "profiles": [asdict(CHAT), asdict(EMBED)],
        "source": {
            "source_id": source.source_id,
            "sha256": source.sha256,
            "pdf_page": 5,
            "excerpt": excerpt,
            "source_url": source.source_url,
        },
        "dataset_lock": lock,
        "generation": summary.data.model_dump(),
        "embedding": {
            "document_count": len(documents.vectors),
            "query_count": len(query.vectors),
            "dimensions": len(query.vectors[0]),
            "query_document_cosines": [cosine(query.vectors[0], v) for v in documents.vectors],
        },
        "boundary": {
            "result": boundary.data.model_dump(),
            "usage": asdict(boundary.usage),
            "oversized_input_rejected_before_inference": True,
            "closed_client_rejected": True,
        },
        "budget": budget.snapshot(),
        "limitations": [
            "Byte-based input estimate is a conservative heuristic, not an exact tokenizer guarantee",
            "Client cancellation does not prove server-side inference has stopped",
            "Inventory digest checks before/after do not atomically bind a server response to weights",
            "Budget is in-process and caller-owned; no durable task retry accounting yet",
            "No database writes, indexing, retrieval metrics, medical safety or answer-quality acceptance",
            "Day 8 developer review and third genuine conflict remain pending",
        ],
    }


def write_report(result):
    cache = (ROOT / ".cache").resolve()
    if OUTPUT.resolve() != cache / "day9" or cache != ROOT / ".cache":
        raise OSError("unsafe output directory")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=OUTPUT)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, OUTPUT / "demo.json")
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["demo"])
    parser.parse_args()
    try:
        result = asyncio.run(demo())
    except ModelFailure as error:
        result = {"status": "failed", "error_code": error.code}
    except ConfigurationError:
        result = {"status": "failed", "error_code": "invalid_configuration"}
    except Exception:
        result = {"status": "failed", "error_code": "day9_failed"}
    try:
        write_report(result)
    except Exception:
        result = {"status": "failed", "error_code": "report_write_failed"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
