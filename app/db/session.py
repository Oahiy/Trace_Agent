"""A database owns a pool; each business operation owns its Session and transaction."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.db.config import DatabaseSettings


class DatabaseUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__("database_unavailable")


class Database:
    def __init__(self, settings: DatabaseSettings) -> None:
        self.engine = create_engine(
            settings.url.get_secret_value(),
            echo=False,
            hide_parameters=True,
            pool_size=2,
            max_overflow=0,
            pool_timeout=settings.pool_timeout_seconds,
            connect_args={
                "connect_timeout": settings.connect_timeout_seconds,
                "options": (
                    f"-c statement_timeout={settings.statement_timeout_ms} "
                    f"-c lock_timeout={settings.lock_timeout_ms} "
                    f"-c idle_in_transaction_session_timeout={settings.idle_transaction_timeout_ms}"
                ),
            },
        )
        self.sessions = sessionmaker(self.engine)

    def probe(self) -> dict[str, str]:
        """An internal database probe, not the multi-dependency /ready endpoint."""
        try:
            with self.sessions() as session:
                version = session.scalar(text("SHOW server_version"))
                vector = session.scalar(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
                distance = session.scalar(text("SELECT '[1,0,0]'::vector <=> '[1,0,0]'::vector"))
                if not vector or distance != 0:
                    raise DatabaseUnavailable()
                return {"postgresql": version, "pgvector": vector}
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None

    def close(self) -> None:
        self.engine.dispose()


@contextmanager
def open_database(settings: DatabaseSettings) -> Iterator[Database]:
    database = Database(settings)
    try:
        yield database
    finally:
        database.close()
