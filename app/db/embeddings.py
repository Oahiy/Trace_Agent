"""Vector persistence and exact search. Every evidence query enforces its owner in SQL."""

from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Chunk, ChunkEmbedding, Document, EmbeddingProfile, User
from app.retrieval.contracts import SPEC, content_digest
from app.services.documents import DataIntegrityError


class EmbeddingRepository:
    def __init__(self, session):
        self.session = session

    def ensure_profile(self):
        self.session.execute(
            insert(EmbeddingProfile)
            .values(version=SPEC.version, **asdict(SPEC))
            .on_conflict_do_nothing(index_elements=[EmbeddingProfile.version])
        )
        self.check_profile()

    def check_profile(self):
        row = self.session.get(EmbeddingProfile, SPEC.version)
        if row is None or any(getattr(row, k) != v for k, v in asdict(SPEC).items()):
            raise DataIntegrityError()

    def stored(self, owner_id, document_id):
        return list(
            self.session.scalars(
                select(ChunkEmbedding)
                .join(Chunk, Chunk.id == ChunkEmbedding.chunk_id)
                .join(Document, Document.id == Chunk.document_id)
                .where(
                    Document.owner_id == owner_id,
                    Document.id == document_id,
                    ChunkEmbedding.index_version == SPEC.version,
                )
                .order_by(Chunk.ordinal)
            )
        )

    def add(self, chunk, vector):
        self.session.add(
            ChunkEmbedding(
                chunk_id=chunk.id,
                index_version=SPEC.version,
                content_sha256=content_digest(chunk.content),
                embedding=list(vector),
            )
        )
        self.session.flush()

    def search(self, owner_id, vector, document_ids, request):
        distance = ChunkEmbedding.embedding.cosine_distance(list(vector)).label("distance")
        query = (
            select(Chunk, Document, distance)
            .join(ChunkEmbedding, ChunkEmbedding.chunk_id == Chunk.id)
            .join(Document, Document.id == Chunk.document_id)
            .join(User, User.id == Document.owner_id)
            .where(
                User.id == owner_id,
                User.active.is_(True),
                Document.owner_id == owner_id,
                Document.id.in_(document_ids),
                Document.status == "READY",
                Document.index_version == SPEC.version,
                Chunk.ingest_version == SPEC.ingest_version,
                ChunkEmbedding.index_version == SPEC.version,
            )
        )
        if request.publication_year_from is not None:
            query = query.where(Document.publication_year >= request.publication_year_from)
        if request.publication_year_to is not None:
            query = query.where(Document.publication_year <= request.publication_year_to)
        return list(
            self.session.execute(
                query.order_by(distance, Document.id, Chunk.ordinal, Chunk.id).limit(request.limit)
            )
        )
