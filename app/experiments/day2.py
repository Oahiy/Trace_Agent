"""Synthetic document experiment. It is not an upload API or persistent store."""

from dataclasses import dataclass
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, StringConstraints

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Content = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200_000)]


class DocumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    title: Title
    content: Content


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    content: str


class RepositoryError(Exception):
    """Persistence adapters must translate underlying errors to this fixed code."""

    def __init__(self) -> None:
        super().__init__("document_store_unavailable")


class DocumentAlreadyExists(Exception):
    def __init__(self) -> None:
        super().__init__("document_already_exists")


class DocumentRepository(Protocol):
    def save(self, document: Document) -> None: ...

    def get(self, document_id: str) -> Document | None: ...


class MemoryDocumentRepository:
    def __init__(self) -> None:
        self._documents: dict[str, Document] = {}

    def save(self, document: Document) -> None:
        if document.id in self._documents:
            raise DocumentAlreadyExists()
        self._documents[document.id] = document

    def get(self, document_id: str) -> Document | None:
        return self._documents.get(document_id)


class DocumentService:
    def __init__(self, repository: DocumentRepository) -> None:
        self._repository = repository

    def create(self, document: Document) -> Document:
        self._repository.save(document)
        return document

    def get(self, document_id: str) -> Document | None:
        return self._repository.get(document_id)


def create_document(payload: object, service: DocumentService, *, document_id: str) -> Document:
    """The caller supplies an internal ID; payload cannot choose it or an owner."""
    request = DocumentCreate.model_validate(payload)
    document = Document(id=document_id, title=request.title, content=request.content)
    return service.create(document)
