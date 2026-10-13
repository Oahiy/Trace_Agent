"""Online migrations only, using the same validated, bounded connection settings."""

from alembic import context
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import ConfigurationError
from app.db.config import load_database_settings
from app.db.models import Base
from app.db.session import open_database

target_metadata = Base.metadata

if context.is_offline_mode():
    raise SystemExit("offline_migrations_not_supported")

try:
    with open_database(load_database_settings()) as database:
        with database.engine.connect() as connection:
            context.configure(
                connection=connection, target_metadata=target_metadata, compare_type=True
            )
            with context.begin_transaction():
                context.run_migrations()
except (SQLAlchemyError, ConfigurationError):
    raise SystemExit("database_migration_failed") from None
