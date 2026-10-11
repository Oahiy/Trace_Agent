"""Run a synthetic success, missing-document case, and controlled failure."""

import json
from dataclasses import FrozenInstanceError, asdict, dataclass

from pydantic import BaseModel, ValidationError

from app.experiments.day2 import (
    DocumentCreate,
    DocumentService,
    MemoryDocumentRepository,
    create_document,
)


def document_probe() -> dict:
    repository = MemoryDocumentRepository()
    service = DocumentService(repository)
    created = create_document(
        {"title": " Python references ", "content": " Synthetic engineering fixture. "},
        service,
        document_id="doc-001",
    )
    assert service.get("doc-001") == created
    assert service.get("missing") is None
    try:
        create_document({"title": " \t", "content": "text"}, service, document_id="rejected")
    except ValidationError:
        failure_code = "invalid_document_input"
    else:
        raise AssertionError("blank title was accepted")
    assert service.get("rejected") is None

    @dataclass
    class MutableDocument:
        title: str

    mutable = MutableDocument("before")
    alias = mutable
    mutable.title = "after"
    try:
        created.title = "changed"
    except FrozenInstanceError:
        immutable = True
    else:
        raise AssertionError("document unexpectedly changed")

    class CoercingCount(BaseModel):
        count: int

    assert CoercingCount.model_validate({"count": "5"}).count == 5
    try:
        DocumentCreate.model_validate({"title": 5, "content": "text"})
    except ValidationError:
        strict_rejected_number = True
    else:
        raise AssertionError("numeric title was accepted")
    return {
        "synthetic": True,
        "success": asdict(created),
        "edge": {"missing_document": None},
        "failure": {"error_code": failure_code, "nothing_saved": True},
        "references": {"mutable_alias_title": alias.title, "stored_document_frozen": immutable},
        "validation": {
            "default_numeric_coercion": 5,
            "strict_numeric_title_rejected": strict_rejected_number,
        },
        "limits": {"title_characters": 200, "content_characters": 200_000},
    }


def main() -> int:
    try:
        result = document_probe()
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "day2_probe_failed"}))
        return 1
    print(json.dumps({"status": "passed", **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
