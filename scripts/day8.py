"""Check and materialize the pinned corpus offline; never registers database jobs."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

from pydantic import ValidationError

from app.core.config import Settings
from app.domains.med.dataset import (
    Corpus,
    DatasetRejected,
    EvalSuite,
    acceptance_gaps,
    canonical_digest,
    check_references,
)
from app.ingestion.parser import IsolatedParser
from app.ingestion.text import CHUNK_OVERLAP, CHUNK_SIZE, INGEST_VERSION, ParseRejected, split_text
from scripts.verify_sources import restore_source

ROOT = Path(__file__).resolve().parents[1]


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DatasetRejected("duplicate_json_key")
        result[key] = value
    return result


def read_json(path):
    if path.stat().st_size > 1024 * 1024:
        raise DatasetRejected("manifest_too_large")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=strict_object)


def load_contracts(root=ROOT):
    corpus = Corpus.model_validate(read_json(root / "docs/day8-sources.json"))
    suite = EvalSuite.model_validate(read_json(root / "evals/med/cases-v1.json"))
    check_references(corpus, suite)
    lock = read_json(root / "evals/med/snapshot-v1.json")
    expected = {
        "corpus_sha256": canonical_digest(corpus),
        "evals_sha256": canonical_digest(suite),
        "acceptance_gaps": acceptance_gaps(suite),
    }
    if lock != expected:
        raise DatasetRejected("dataset_lock_mismatch")
    return corpus, suite, lock


def confined_path(relative, root=ROOT):
    base = root.resolve() / ".cache"
    target = root / relative
    resolved = target.resolve()
    if base.resolve() != base or not resolved.is_relative_to(base):
        raise DatasetRejected("cache_path_rejected")
    return target


def normalized(text):
    # Comparison only. Never modify stored page text, chunk content or TXT offsets.
    return "".join(text.split()).casefold()


def verify(corpus, suite, root=ROOT, *, download=False):
    expected_profile = {
        "parser": "pypdf",
        "parser_version": version("pypdf"),
        "ingest_version": INGEST_VERSION,
        "chunk_characters": CHUNK_SIZE,
        "overlap_characters": CHUNK_OVERLAP,
    }
    if corpus.profile.model_dump() != expected_profile:
        raise DatasetRejected("parser_profile_mismatch")
    documents, reports, evidence_count = [], [], 0
    for source in corpus.sources:
        path = confined_path(source.cache_file, root)
        if not path.exists() and download:
            asyncio.run(restore_source(source.model_dump(), root))
        if not path.is_file():
            raise DatasetRejected("source_cache_missing")
        if path.stat().st_size != source.bytes:
            raise DatasetRejected("source_size_mismatch")
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != source.sha256:
            raise DatasetRejected("source_hash_mismatch")
        parser = IsolatedParser(Settings())
        parsed = parser.parse(raw, "application/pdf")
        if len(parsed.pages) != source.page_count or parsed.characters != source.characters:
            raise DatasetRejected("source_extraction_changed")
        chunks = split_text(parsed)
        anchors = list(source.verification_anchors)
        anchors.extend(
            e for c in suite.cases for e in c.evidence if e.source_id == source.source_id
        )
        checked = []
        for anchor in anchors:
            if not 1 <= anchor.pdf_page <= len(parsed.pages):
                raise DatasetRejected("evidence_page_invalid")
            if normalized(anchor.anchor) not in normalized(parsed.pages[anchor.pdf_page - 1].text):
                raise DatasetRejected("evidence_anchor_missing")
            matching = [
                c.ordinal
                for c in chunks
                if c.pdf_page == anchor.pdf_page
                and normalized(anchor.anchor) in normalized(c.content)
            ]
            if not matching:
                raise DatasetRejected("chunk_anchor_missing")
            checked.append({"pdf_page": anchor.pdf_page, "chunk_ordinals": matching})
            evidence_count += 1
        documents.append(
            {
                "source_id": source.source_id,
                "file_sha256": source.sha256,
                "chunks": [asdict(chunk) for chunk in chunks],
            }
        )
        reports.append(
            {
                "source_id": source.source_id,
                "pages": len(parsed.pages),
                "characters": parsed.characters,
                "chunks": len(chunks),
                "anchors": checked,
                "memory_limit_active": parser.memory_limited,
            }
        )
    return documents, {"sources": reports, "anchors_checked": evidence_count}


def publish_snapshot(path, payload):
    """Publish once; identical replay reuses bytes, changed output cannot overwrite."""
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            return False
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
                raise DatasetRejected("prepared_snapshot_conflict") from None
            return True
    finally:
        temporary.unlink(missing_ok=True)


def run(command, *, root=ROOT, download=False, require_acceptance=False):
    corpus, suite, lock = load_contracts(root)
    documents, verification = verify(corpus, suite, root, download=download)
    result = {
        "status": "passed",
        "scope": "offline corpus and annotation integrity; no model evaluation",
        **lock,
        "day8_acceptance_ready": not lock["acceptance_gaps"],
        "review_status": suite.review_status,
        "case_counts": {
            split: dict(Counter(c.category for c in suite.cases if c.split == split))
            for split in ("dev", "acceptance")
        },
        "source_count": len(corpus.sources),
        "case_count": len(suite.cases),
        "genuine_conflict_cases": sum(c.relation == "unresolved_conflict" for c in suite.cases),
        "models_called": False,
        "database_accessed": False,
        **verification,
    }
    if require_acceptance and lock["acceptance_gaps"]:
        raise DatasetRejected("dataset_acceptance_incomplete")
    if command == "prepare":
        snapshot_id = canonical_digest(lock)
        relative = f".cache/day8/snapshot-{snapshot_id}.json"
        target = confined_path(relative, root)
        result["prepared_snapshot_reused"] = publish_snapshot(
            target, {"lock": lock, "profile": corpus.profile.model_dump(), "documents": documents}
        )
        result["prepared_snapshot"] = relative
        result["prepared_snapshot_sha256"] = sha256(target.read_bytes()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "prepare"))
    parser.add_argument(
        "--download", action="store_true", help="restore missing pinned PDFs (prepare only)"
    )
    parser.add_argument(
        "--require-acceptance", action="store_true", help="also require all original Day 8 gates"
    )
    args = parser.parse_args()
    if args.download and args.command != "prepare":
        parser.error("--download requires prepare")
    try:
        result = run(
            args.command, download=args.download, require_acceptance=args.require_acceptance
        )
    except DatasetRejected as error:
        result = {"status": "failed", "error_code": str(error)}
    except (ValidationError, ValueError, TypeError, KeyError):
        result = {"status": "failed", "error_code": "dataset_invalid"}
    except ParseRejected:
        result = {"status": "failed", "error_code": "source_parse_failed"}
    except Exception:
        result = {"status": "failed", "error_code": "dataset_verification_failed"}
    print(json.dumps(result, ensure_ascii=False))
    return int(result["status"] != "passed")


if __name__ == "__main__":
    sys.exit(main())
