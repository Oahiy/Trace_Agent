"""Read-only exact retrieval. Returns candidates with provenance, never generated answers."""

import json
from dataclasses import asdict
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.db.embeddings import EmbeddingRepository
from app.db.models import Document
from app.db.repositories import UserRepository
from app.db.session import DatabaseUnavailable
from app.llm.budget import CallBudget
from app.llm.contracts import EMBED
from app.retrieval.contracts import SPEC, IndexFailure, RetrievalRequest, content_digest
from app.services.documents import ResourceNotFound, metadata_of
from app.services.indexing import checked_vectors, input_chunks, validate_complete
from app.services.ingestion import IngestConflict


class RetrievalService:
    def __init__(self, sessions, embedder):
        self.sessions, self.embedder = sessions, embedder

    def _scope(self, session, owner_id, request):
        if not UserRepository(session).is_active(owner_id):
            raise ResourceNotFound()
        query = select(Document).where(Document.owner_id == owner_id).order_by(Document.id)
        if request.document_ids is not None:
            query = query.where(Document.id.in_([UUID(v) for v in request.document_ids]))
        documents = list(session.scalars(query.limit(101)))
        if request.document_ids is not None and len(documents) != len(request.document_ids):
            raise ResourceNotFound()
        if len(documents) > 100:
            raise IngestConflict()
        eligible, excluded, signatures = [], [], []
        for document in documents:
            chunk_signature = ()
            reason = None
            if document.status != "READY":
                reason = "not_ready"
            elif document.index_version != SPEC.version:
                reason = "incompatible_index"
            elif (
                request.publication_year_from is not None or request.publication_year_to is not None
            ) and document.publication_year is None:
                reason = "unknown_publication_year"
            elif (
                request.publication_year_from is not None
                and document.publication_year < request.publication_year_from
            ):
                reason = "outside_publication_year"
            elif (
                request.publication_year_to is not None
                and document.publication_year > request.publication_year_to
            ):
                reason = "outside_publication_year"
            if reason is not None:
                excluded.append({"document_id": str(document.id), "reason": reason})
            else:
                chunks = input_chunks(session, owner_id, document.id)
                validate_complete(session, document, chunks)
                chunk_signature = tuple((str(c.id), content_digest(c.content)) for c in chunks)
                eligible.append(document.id)
            signatures.append(
                (
                    document.id,
                    metadata_of(document),
                    document.status,
                    document.index_version,
                    chunk_signature,
                )
            )
        return eligible, excluded, signatures

    async def search(self, owner_id, payload):
        request = RetrievalRequest.model_validate(payload)
        # Copy the list-bearing model so callers cannot mutate scope while we await I/O.
        request = RetrievalRequest.model_validate(request.model_dump())
        budget = CallBudget(embedding_limit=2)
        try:
            with self.sessions() as session:
                before = self._scope(session, owner_id, request)
            eligible, excluded, signatures = before
            hits = []
            if eligible:
                result = await self.embedder.embed_query(request.question, budget=budget)
                if result.profile != EMBED:
                    raise IndexFailure("index_invalid_embedding")
                vector = checked_vectors(result.vectors, 1)[0]
                with self.sessions() as session:
                    if self._scope(session, owner_id, request) != before:
                        raise IngestConflict()
                    for chunk, document, distance in EmbeddingRepository(session).search(
                        owner_id, vector, eligible, request
                    ):
                        hits.append(
                            {
                                "document_id": str(document.id),
                                "chunk_id": str(chunk.id),
                                "source_sha256": document.file_sha256,
                                **metadata_of(document).model_dump(
                                    exclude={"file_sha256", "ingest_version"}
                                ),
                                "content": chunk.content,
                                "pdf_page": chunk.pdf_page,
                                "char_start": chunk.char_start,
                                "char_end": chunk.char_end,
                                "section": chunk.section,
                                "distance": float(distance),
                                "index_version": SPEC.version,
                            }
                        )
            snapshot = [
                {
                    "document_id": str(s[0]),
                    "metadata": s[1].model_dump(),
                    "status": s[2],
                    "index_version": s[3],
                    "chunks": s[4],
                }
                for s in signatures
            ]
            digest = sha256(
                json.dumps(
                    snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest()
            return {
                "question": request.question,
                "conditions": {
                    "document_ids": [str(s[0]) for s in signatures],
                    "publication_year_from": request.publication_year_from,
                    "publication_year_to": request.publication_year_to,
                    "limit": request.limit,
                },
                "data_snapshot": "ds1-" + digest,
                "index_version": SPEC.version,
                "embedding_profile": asdict(SPEC),
                "excluded_documents": excluded,
                "hits": hits,
                "usage": budget.snapshot(),
                "limitations": [
                    "Similarity ranks evidence candidates; claim support is not evaluated."
                ],
            }
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None
