"""Deterministic text extraction and slicing, without storage or database access."""

from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100
# Day 4's ingest-v1 had no parser/chunks. This is its first pinned parsing profile.
# Any later change to extraction/normalization/slicing must use a new ingest version.
INGEST_VERSION = "ingest-v1"
MAX_PAGE_STREAM_BYTES = 4 * 1024 * 1024
MAX_TOTAL_STREAM_BYTES = 16 * 1024 * 1024
ERROR_MESSAGES = {
    "invalid_text": "Document does not contain supported UTF-8 text.",
    "invalid_pdf": "Document is not a supported valid PDF.",
    "encrypted_pdf": "Encrypted PDF documents are not supported.",
    "too_many_pages": "Document exceeds the PDF page limit.",
    "too_much_text": "Document exceeds the extracted text limit.",
    "no_extractable_text": "Document has no supported extractable text.",
    "unsupported_image_page": "Document contains an unsupported image-only page.",
    "parse_resource_limit": "Document exceeds parsing resource limits.",
    "parse_timeout": "Document parsing exceeded the allowed time.",
    "parse_failed": "Document parsing could not be completed.",
}


class ParseRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code if code in ERROR_MESSAGES else "parse_failed"
        super().__init__(self.code)


@dataclass(frozen=True)
class PageText:
    text: str
    pdf_page: int | None = None


@dataclass(frozen=True)
class ParsedText:
    pages: tuple[PageText, ...]

    @property
    def characters(self) -> int:
        return sum(len(page.text) for page in self.pages)


@dataclass(frozen=True)
class TextChunk:
    ordinal: int
    content: str
    pdf_page: int | None = None
    char_start: int | None = None
    char_end: int | None = None


def _valid_text(text: str) -> bool:
    return not any((ord(c) < 32 and c not in "\t\r\n") or 0xD800 <= ord(c) <= 0xDFFF for c in text)


def parse_bytes(
    data: bytes, media_type: str, *, max_pages: int = 50, max_characters: int = 200_000
) -> ParsedText:
    if media_type == "text/plain":
        try:
            text = data.decode("utf-8-sig", errors="strict")
        except UnicodeError:
            raise ParseRejected("invalid_text") from None
        if not _valid_text(text):
            raise ParseRejected("invalid_text")
        if len(text) > max_characters:
            raise ParseRejected("too_much_text")
        if not text.strip():
            raise ParseRejected("no_extractable_text")
        # Preserve CR/LF, Unicode and whitespace. Offsets address this decoded text.
        return ParsedText((PageText(text),))
    if media_type != "application/pdf" or not data.startswith(b"%PDF-"):
        raise ParseRejected("invalid_pdf")
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ParseRejected("encrypted_pdf")
        if len(reader.pages) > max_pages:
            raise ParseRejected("too_many_pages")
        pages, characters, stream_bytes = [], 0, 0
        for number, page in enumerate(reader.pages, start=1):
            contents = page.get_contents()
            size = len(contents.get_data()) if contents is not None else 0
            stream_bytes += size
            if size > MAX_PAGE_STREAM_BYTES or stream_bytes > MAX_TOTAL_STREAM_BYTES:
                raise ParseRejected("parse_resource_limit")
            text = page.extract_text() or ""
            if not _valid_text(text):
                raise ParseRejected("invalid_pdf")
            characters += len(text)
            if characters > max_characters:
                raise ParseRejected("too_much_text")
            if not text.strip():
                resources = page.get("/Resources", {})
                objects = resources.get("/XObject", {})
                if any(obj.get_object().get("/Subtype") == "/Image" for obj in objects.values()):
                    raise ParseRejected("unsupported_image_page")
            pages.append(PageText(text, number))
        if not any(page.text.strip() for page in pages):
            raise ParseRejected("no_extractable_text")
        return ParsedText(tuple(pages))
    except ParseRejected:
        raise
    except MemoryError:
        raise ParseRejected("parse_resource_limit") from None
    except Exception:
        # Malformed PDFs can fail in many parser/font paths. No parser details escape.
        raise ParseRejected("invalid_pdf") from None


def split_text(
    parsed: ParsedText, *, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> tuple[TextChunk, ...]:
    if type(size) is not int or type(overlap) is not int or not 0 <= overlap < size:
        raise ValueError("invalid_chunk_parameters")
    chunks = []
    for page in parsed.pages:
        start = 0
        while start < len(page.text):
            end = min(start + size, len(page.text))
            content = page.text[start:end]
            if content.strip():
                chunks.append(
                    TextChunk(
                        len(chunks),
                        content,
                        page.pdf_page,
                        start if page.pdf_page is None else None,
                        end if page.pdf_page is None else None,
                    )
                )
            if end == len(page.text):
                break
            start = end - overlap
    return tuple(chunks)
