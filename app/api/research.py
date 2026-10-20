"""Authenticated bounded JSON transport for durable research tasks."""

import json
from typing import Annotated
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.api.documents import authenticate
from app.api.schemas import ResearchRequest
from app.domains.med.research import WORKFLOW_VERSION
from app.services.auth import Principal
from app.services.research import ResearchService
from app.storage.files import UploadRejected

router = APIRouter()


class ResearchAccepted(BaseModel):
    job_id: str
    run_id: str
    status: str


class ResearchStatus(ResearchAccepted):
    question: str
    conditions: dict
    result: dict | None
    error: dict | None


def service(request):
    return ResearchService(
        request.app.state.database.sessions,
        request.app.state.research_queue,
        workflow_version=WORKFLOW_VERSION,
        timeout=request.app.state.settings.research_timeout_seconds,
    )


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = item
    return value


def invalid_constant(value):
    raise ValueError("invalid_json_constant")


@router.post(
    "/research",
    status_code=202,
    response_model=ResearchAccepted,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": ResearchRequest.model_json_schema()}},
        }
    },
)
async def create_research(request: Request, principal: Annotated[Principal, Depends(authenticate)]):
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise UploadRejected("invalid_request")
    raw = bytearray()
    try:
        with anyio.fail_after(30):
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 16384:
                    raise UploadRejected("invalid_request", 413)
                raw.extend(chunk)
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant
        )
        validated = ResearchRequest.model_validate(payload)
        if len(validated.question) > request.app.state.settings.max_question_characters:
            raise ValueError("question_limit")
    except (ValueError, ValidationError, RecursionError, ClientDisconnect):
        raise UploadRejected("invalid_request") from None
    except TimeoutError:
        raise UploadRejected("invalid_request", 408) from None
    research = service(request)
    accepted = await run_in_threadpool(research.create, principal.owner_id, validated)
    return await run_in_threadpool(research.dispatch, principal.owner_id, UUID(accepted["job_id"]))


@router.get("/research/{job_id}", response_model=ResearchStatus)
def research_status(
    job_id: str, request: Request, principal: Annotated[Principal, Depends(authenticate)]
):
    try:
        identifier = UUID(job_id)
        if str(identifier) != job_id:
            raise ValueError("canonical_id_required")
    except ValueError:
        raise UploadRejected("invalid_request") from None
    return service(request).get(principal.owner_id, identifier)
