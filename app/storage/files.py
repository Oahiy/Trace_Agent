"""Bounded local file storage. Names and hashes are generated from trusted server data."""

import codecs
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class UploadRejected(Exception):
    def __init__(self, code: str, status: int = 422) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class StorageUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__("storage_unavailable")


class FileReference(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )
    key: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    size_bytes: int = Field(ge=1, le=5 * 1024 * 1024)
    media_type: Literal["text/plain", "application/pdf"]


@dataclass(frozen=True)
class FileArtifact:
    reference: FileReference
    sha256: str


class FileStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _directory(self) -> None:
        if self.root.is_symlink():
            raise StorageUnavailable()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise StorageUnavailable()

    def path(self, key: str) -> Path:
        if self.root.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}", key):
            raise StorageUnavailable()
        return self.root / (key + ".bin")

    def save(
        self,
        source: BinaryIO,
        media_type: str,
        *,
        max_bytes: int,
        max_characters: int,
    ) -> FileArtifact:
        if media_type not in {"text/plain", "application/pdf"}:
            raise UploadRejected("unsupported_file_type", 415)
        key = uuid4().hex
        target = self.path(key)
        temporary = self.root / (key + ".part")
        promoted = False
        completed = False
        created = False
        try:
            self._directory()
            digest = hashlib.sha256()
            size = 0
            characters = 0
            nonblank = False
            prefix = b""
            decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")

            def check_text(text: str) -> None:
                nonlocal characters, nonblank
                characters += len(text)
                if characters > max_characters or any(
                    ord(c) < 32 and c not in "\t\r\n" for c in text
                ):
                    raise UploadRejected("invalid_request")
                nonblank = nonblank or bool(text.strip())

            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            created = True
            with os.fdopen(descriptor, "wb") as output:
                while chunk := source.read(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadRejected("file_too_large", 413)
                    prefix = (prefix + chunk)[:8]
                    if media_type == "text/plain":
                        check_text(decoder.decode(chunk))
                    digest.update(chunk)
                    output.write(chunk)
                if not size:
                    raise UploadRejected("invalid_request")
                if media_type == "text/plain":
                    check_text(decoder.decode(b"", final=True))
                    if not nonblank or prefix.startswith(b"%PDF-"):
                        raise UploadRejected("invalid_request")
                elif not re.match(rb"%PDF-[12]\.[0-9]", prefix):
                    raise UploadRejected("unsupported_file_type", 415)
                output.flush()
                os.fsync(output.fileno())
            # Exclusive atomic publication: never overwrite another request's file,
            # even if a generated name collides. Both paths are on the same filesystem.
            os.link(temporary, target, follow_symlinks=False)
            promoted = True
            temporary.unlink()
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            artifact = FileArtifact(
                FileReference(key=key, size_bytes=size, media_type=media_type), digest.hexdigest()
            )
            completed = True
            return artifact
        except UnicodeError:
            raise UploadRejected("invalid_request") from None
        except OSError:
            raise StorageUnavailable() from None
        finally:
            # Registration has not started yet. These paths belong only to this save call.
            try:
                if created:
                    temporary.unlink(missing_ok=True)
                if promoted and not completed:
                    target.unlink(missing_ok=True)
            except OSError:
                raise StorageUnavailable() from None

    def discard(self, key: str) -> None:
        try:
            self.path(key).unlink(missing_ok=True)
        except OSError:
            raise StorageUnavailable() from None

    def verify(self, reference: FileReference, sha256: str) -> None:
        try:
            descriptor = os.open(self.path(reference.key), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != reference.size_bytes:
                    raise StorageUnavailable()
                digest = hashlib.file_digest(source, "sha256").hexdigest()
                if digest != sha256:
                    raise StorageUnavailable()
        except OSError:
            raise StorageUnavailable() from None
