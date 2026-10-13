"""File persistence and database registration have separate, explicit failure boundaries."""

from typing import BinaryIO
from uuid import UUID

from pydantic import ValidationError

from app.core.config import Settings
from app.services.documents import (
    DataIntegrityError,
    DocumentMetadata,
    DocumentService,
    Registration,
    ResourceNotFound,
    SourceMetadataConflict,
    TooManyPendingTasks,
)
from app.storage.files import FileStore, StorageUnavailable

INGEST_VERSION = "ingest-v1"


class UploadService:
    def __init__(self, documents: DocumentService, files: FileStore, settings: Settings) -> None:
        self.documents = documents
        self.files = files
        self.settings = settings

    def upload(
        self, owner_id: UUID, metadata: DocumentMetadata, source: BinaryIO, media_type: str
    ) -> Registration:
        metadata = DocumentMetadata.model_validate(metadata)
        artifact = self.files.save(
            source,
            media_type,
            max_bytes=self.settings.max_file_bytes,
            max_characters=self.settings.max_text_characters,
        )
        candidate = artifact.reference
        try:
            payload = {
                **metadata.model_dump(),
                "file_sha256": artifact.sha256,
                "ingest_version": INGEST_VERSION,
            }
            result = self.documents.register(owner_id, payload, candidate)
        except (
            ValidationError,
            ResourceNotFound,
            SourceMetadataConflict,
            DataIntegrityError,
            TooManyPendingTasks,
        ):
            # These paths have definitely rolled back, or never opened a transaction.
            self.files.discard(candidate.key)
            raise
        # DatabaseUnavailable (including ambiguous COMMIT failure) deliberately retains
        # the candidate. A read-only audit reconciles it after the database recovers.
        if result.reused:
            self.files.discard(candidate.key)
        if result.stored_file is None:
            raise StorageUnavailable()
        self.files.verify(result.stored_file, artifact.sha256)
        return result
