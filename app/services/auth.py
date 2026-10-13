"""Trusted identity from high-entropy keys; only irreversible digests reach the database."""

import hashlib
import re
import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import User
from app.db.session import DatabaseUnavailable

KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class AuthenticationRequired(Exception):
    def __init__(self) -> None:
        super().__init__("authentication_required")


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Principal:
    owner_id: UUID


class AuthService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def authenticate(self, key: str | None) -> Principal:
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise AuthenticationRequired()
        digest = key_digest(key)
        try:
            with self.sessions() as session:
                row = session.execute(
                    select(User.id, User.api_key_digest, User.active).where(
                        User.api_key_digest == digest
                    )
                ).one_or_none()
                stored = row.api_key_digest if row is not None else "0" * 64
                matched = secrets.compare_digest(digest, stored)
                if row is None or not matched or not row.active:
                    raise AuthenticationRequired()
                return Principal(row.id)
        except SQLAlchemyError:
            raise DatabaseUnavailable() from None
