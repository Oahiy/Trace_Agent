"""HTTP-only authentication, bounded multipart parsing, and safe public errors."""

import re
from typing import Annotated
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ValidationError
from python_multipart.exceptions import MultipartParseError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser, parse_options_header
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from app.db.session import DatabaseUnavailable
from app.ingestion.text import ERROR_MESSAGES
from app.services.auth import KEY_PATTERN, AuthenticationRequired, AuthService, Principal
from app.services.documents import (
    DataIntegrityError,
    DocumentMetadata,
    DocumentService,
    ResourceNotFound,
    SourceMetadataConflict,
    TooManyPendingTasks,
)
from app.services.uploads import INGEST_VERSION, UploadService
from app.storage.files import StorageUnavailable, UploadRejected
from app.tasks.queue import QueueUnavailable

router = APIRouter()
scheme = APIKeyHeader(name="X-API-Key", auto_error=False)
FIELDS = {
    "source_id",
    "title",
    "source_url",
    "license",
    "license_evidence_url",
    "publication_year",
    "version_label",
    "research_dates",
}
MESSAGES = {
    **ERROR_MESSAGES,
    "authentication_required": "Valid authentication is required.",
    "invalid_request": "Request does not satisfy the contract.",
    "file_too_large": "File or upload request exceeds the allowed size.",
    "unsupported_file_type": "Only UTF-8 TXT and PDF uploads are supported.",
    "resource_not_found": "Resource was not found.",
    "source_metadata_conflict": "Source metadata conflicts with the stored document.",
    "database_unavailable": "Database is unavailable.",
    "storage_unavailable": "File storage is unavailable.",
    "upload_timeout": "Upload exceeded the allowed time.",
    "too_many_pending_tasks": "Too many unfinished tasks.",
    "internal_error": "The request could not be completed.",
    "queue_unavailable": "Task queue is unavailable.",
    "task_execution_failed": "Background task could not be completed.",
    "queue_wait_timeout": "Task exceeded its queue wait limit.",
    "task_timeout": "Task exceeded its execution deadline.",
    "ingest_state_conflict": "Task state does not allow this operation.",
}


def error_response(code: str, status: int) -> JSONResponse:
    headers = {"WWW-Authenticate": "APIKey"} if status == 401 else None
    return JSONResponse(
        {"error": {"code": code, "message": MESSAGES[code]}},
        status_code=status,
        headers=headers,
    )


class DocumentAccepted(BaseModel):
    document_id: str
    job_id: str
    document_status: str
    job_status: str


class DocumentStatus(BaseModel):
    document_id: str
    status: str
    index_version: str | None
    error: str | None


def authenticate(request: Request, key: Annotated[str | None, Depends(scheme)]) -> Principal:
    if len(request.headers.getlist("x-api-key")) != 1 or not key or not KEY_PATTERN.fullmatch(key):
        raise AuthenticationRequired()
    database = request.app.state.database
    if database is None:
        raise DatabaseUnavailable()
    return AuthService(database.sessions).authenticate(key)


class CompleteMultipartParser(MultiPartParser):
    """The pinned parser's finalize() allows truncation; require its end callback."""

    complete = False

    def on_end(self) -> None:
        self.complete = True

    async def parse(self):
        form = await super().parse()
        if not self.complete:
            await form.close()
            raise MultiPartException("incomplete_multipart")
        return form


@router.post(
    "/documents",
    status_code=202,
    response_model=DocumentAccepted,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "file",
                            "source_id",
                            "title",
                            "source_url",
                            "license",
                            "license_evidence_url",
                        ],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            **{key: {"type": "string"} for key in sorted(FIELDS)},
                        },
                    }
                }
            },
        }
    },
)
async def upload_document(request: Request, principal: Annotated[Principal, Depends(authenticate)]):
    settings = request.app.state.settings
    content_type, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary", b"")
    if (
        content_type != b"multipart/form-data"
        or not 1 <= len(boundary) <= 200
        or options.get(b"charset", b"utf-8").lower() != b"utf-8"
    ):
        raise UploadRejected("invalid_request", 400)
    form = None
    try:
        with anyio.fail_after(settings.upload_timeout_seconds):
            form = await CompleteMultipartParser(
                request.headers, request.stream(), max_files=1, max_fields=8, max_part_size=8192
            ).parse()
        pairs = form.multi_items()
        names = [key for key, _ in pairs]
        if len(names) != len(set(names)) or set(names) - (FIELDS | {"file"}):
            raise UploadRejected("invalid_request")
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise UploadRejected("invalid_request")
        if file.size is None or file.size > settings.max_file_bytes:
            raise UploadRejected("file_too_large", 413)
        metadata = {key: value for key, value in pairs if key != "file"}
        if any(not isinstance(value, str) for value in metadata.values()):
            raise UploadRejected("invalid_request")
        year = metadata.get("publication_year")
        if year == "":
            metadata["publication_year"] = None
        elif year is not None:
            if not re.fullmatch(r"[0-9]{4}", year):
                raise UploadRejected("invalid_request")
            metadata["publication_year"] = int(year)
        validated = DocumentMetadata.model_validate(
            {
                **metadata,
                "file_sha256": "0" * 64,
                "ingest_version": INGEST_VERSION,
            }
        )
        service = UploadService(
            DocumentService(request.app.state.database.sessions),
            request.app.state.files,
            settings,
        )
        # One blocking operation owns the file and its own Session. It is not abandoned
        # on cancellation while committing; network parsing has a separate deadline.
        result = await run_in_threadpool(
            service.upload,
            principal.owner_id,
            validated,
            file.file,
            (file.content_type or "").split(";", 1)[0].strip().lower(),
        )
        if request.app.state.tasks is not None:
            await run_in_threadpool(
                request.app.state.tasks.dispatch, principal.owner_id, result.job_id
            )
        return DocumentAccepted(
            document_id=str(result.document_id),
            job_id=str(result.job_id),
            document_status=result.document_status,
            job_status=result.job_status,
        )
    except TimeoutError:
        raise UploadRejected("upload_timeout", 408) from None
    except ValidationError:
        raise UploadRejected("invalid_request") from None
    except (MultiPartException, MultipartParseError, ClientDisconnect, UnicodeError, ValueError):
        raise UploadRejected("invalid_request", 400) from None
    finally:
        if form is not None:
            await form.close()


@router.get("/documents/{document_id}/status", response_model=DocumentStatus)
def document_status(
    document_id: str, request: Request, principal: Annotated[Principal, Depends(authenticate)]
):
    try:
        identifier = UUID(document_id)
    except ValueError:
        raise UploadRejected("invalid_request") from None
    if str(identifier) != document_id:
        raise UploadRejected("invalid_request")
    record = DocumentService(request.app.state.database.sessions).get(
        principal.owner_id, identifier
    )
    if record is None:
        raise ResourceNotFound()
    if record.stored_file is None:
        # Historical Day 3 synthetic registrations never claimed to save real files.
        raise StorageUnavailable()
    request.app.state.files.verify(record.stored_file, record.metadata.file_sha256)
    safe_code = record.error_code if record.error_code in MESSAGES else None
    if record.error_code and safe_code is None:
        safe_code = "internal_error"
    return DocumentStatus(
        document_id=str(record.id),
        status=record.status,
        index_version=record.index_version,
        error=safe_code,
    )


def install_error_handlers(application) -> None:
    @application.exception_handler(QueueUnavailable)
    async def queue_error(request, error):
        return JSONResponse(
            {
                "error": {"code": "queue_unavailable", "message": MESSAGES["queue_unavailable"]},
                "job_id": str(error.job_id) if error.job_id else None,
            },
            status_code=503,
        )

    mappings = {
        AuthenticationRequired: (401, "authentication_required"),
        ResourceNotFound: (404, "resource_not_found"),
        SourceMetadataConflict: (409, "source_metadata_conflict"),
        DatabaseUnavailable: (503, "database_unavailable"),
        StorageUnavailable: (503, "storage_unavailable"),
        DataIntegrityError: (500, "internal_error"),
        TooManyPendingTasks: (429, "too_many_pending_tasks"),
    }
    for error_type, (status, code) in mappings.items():

        async def mapped(request, error, status=status, code=code):
            return error_response(code, status)

        application.add_exception_handler(error_type, mapped)

    @application.exception_handler(UploadRejected)
    async def rejected(request, error):
        return error_response(error.code, error.status)

    @application.exception_handler(HTTPException)
    async def http_error(request, error):
        if request.url.path == "/documents" and request.method == "POST":
            return error_response(
                "file_too_large" if error.status_code == 413 else "invalid_request",
                error.status_code,
            )
        # Preserve the existing framework 404/405 contract and Allow header.
        return JSONResponse(
            {"detail": error.detail}, status_code=error.status_code, headers=error.headers
        )
