import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domains.med.dataset import (
    Corpus,
    DatasetRejected,
    EvalSuite,
    acceptance_gaps,
    canonical_digest,
    check_references,
)
from app.ingestion.text import PageText, ParsedText, ParseRejected
from scripts import day8

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def contracts():
    return day8.load_contracts(ROOT)[:2]


def test_committed_contracts_have_exact_splits_and_honest_gates(contracts):
    corpus, suite = contracts
    assert len(corpus.sources) == 10 and len(suite.cases) == 30
    assert sum(c.split == "dev" for c in suite.cases) == 10
    assert sum(c.split == "acceptance" for c in suite.cases) == 20
    assert sum(c.relation == "unresolved_conflict" for c in suite.cases) == 2
    assert acceptance_gaps(suite) == [
        "developer_source_and_label_review_pending",
        "three_genuine_conflict_cases_not_established",
    ]
    assert {e.source_id for c in suite.cases for e in c.evidence} == {
        s.source_id for s in corpus.sources
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"publication_year": True},
        {"publication_date": "20090721"},
        {"publication_year": 2021},
        {"bytes": 5 * 1024 * 1024 + 1},
        {"download_url": "https://other.example/file.pdf"},
        {"source_url": "https://name:secret@journals.plos.org/file"},
        {"source_url": "https://journals.plos.org:444/file"},
        {"source_url": "https://journals.plos.org/file#fragment"},
        {"cache_file": "sources/read-only.pdf"},
        {"cache_file": ".cache/day8/../secret.pdf"},
        {"sha256": "bad"},
        {"verification_anchors": []},
    ],
)
def test_source_invalid_type_date_origin_path_and_limits(contracts, changes):
    data = contracts[0].model_dump()
    data["sources"][0].update(changes)
    with pytest.raises(ValidationError):
        Corpus.model_validate(data)


@pytest.mark.parametrize("field", ["doi", "source_id", "sha256", "cache_file"])
def test_duplicate_sources_are_rejected(contracts, field):
    data = contracts[0].model_dump()
    data["sources"][1][field] = data["sources"][0][field]
    with pytest.raises(ValidationError):
        Corpus.model_validate(data)


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_id",
        "duplicate_question",
        "wrong_split",
        "empty_scope",
        "unknown_field",
        "reversed_years",
        "missing_evidence",
        "wrong_expected_source",
        "bad_no_answer",
        "one_conflict_anchor",
        "duplicate_evidence",
        "wrong_principal",
    ],
)
def test_suite_inconsistent_labels_and_split_are_rejected(contracts, change):
    data = contracts[1].model_dump()
    case = data["cases"][0]
    if change == "duplicate_id":
        case["case_id"] = data["cases"][1]["case_id"]
    elif change == "duplicate_question":
        case["question"] = "  " + data["cases"][1]["question"].upper() + "  "
    elif change == "wrong_split":
        case["split"] = "acceptance"
    elif change == "empty_scope":
        case["conditions"]["source_ids"] = []
    elif change == "unknown_field":
        case["owner_id"] = "client-controlled"
    elif change == "reversed_years":
        case["conditions"].update(publication_year_from=2021, publication_year_to=2009)
    elif change == "missing_evidence":
        case["evidence"] = []
    elif change == "wrong_expected_source":
        case["expected_source_ids"] = ["unknown"]
    elif change == "bad_no_answer":
        data["cases"][6]["expected_behavior"] = "answer"
    elif change == "one_conflict_anchor":
        data["cases"][8]["evidence"] = data["cases"][8]["evidence"][:1]
    elif change == "duplicate_evidence":
        case["evidence"].append(deepcopy(case["evidence"][0]))
    elif change == "wrong_principal":
        data["cases"][-2]["principal"] = "owner_a"
    with pytest.raises(ValidationError):
        EvalSuite.model_validate(data)


@pytest.mark.parametrize(
    "change,code",
    [
        ("snapshot", "corpus_snapshot_mismatch"),
        ("scope", "unknown_source"),
        ("page", "evidence_page_invalid"),
        ("year", "evidence_outside_years"),
    ],
)
def test_cross_manifest_checks(contracts, change, code):
    corpus, suite = contracts
    data = suite.model_dump()
    if change == "snapshot":
        data["corpus_sha256"] = "0" * 64
    elif change == "scope":
        data["cases"][0]["conditions"]["source_ids"].append("unknown")
    elif change == "page":
        data["cases"][0]["evidence"][0]["pdf_page"] = 50
    elif change == "year":
        data["cases"][0]["conditions"]["publication_year_from"] = 2021
    with pytest.raises(DatasetRejected, match=f"^{code}$"):
        check_references(corpus, EvalSuite.model_validate(data))


def test_digest_ignores_object_key_order_but_detects_content_change(contracts):
    data = contracts[0].model_dump()
    assert canonical_digest(data) == canonical_digest(dict(reversed(list(data.items()))))
    data["sources"][0]["research_dates"] += " revised"
    assert canonical_digest(data) != canonical_digest(contracts[0])


def test_duplicate_json_keys_are_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"value":1,"value":2}')
    with pytest.raises(DatasetRejected, match="^duplicate_json_key$"):
        day8.read_json(path)


def test_lock_tampering_is_rejected(tmp_path):
    for relative in [
        "docs/day8-sources.json",
        "evals/med/cases-v1.json",
        "evals/med/snapshot-v1.json",
    ]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / relative).read_bytes())
    lock = tmp_path / "evals/med/snapshot-v1.json"
    data = json.loads(lock.read_text())
    data["acceptance_gaps"] = []
    lock.write_text(json.dumps(data))
    with pytest.raises(DatasetRejected, match="^dataset_lock_mismatch$"):
        day8.load_contracts(tmp_path)


@pytest.fixture
def synthetic_files(contracts, tmp_path, monkeypatch):
    corpus, suite = contracts
    data = corpus.model_dump()
    parsed_by_raw = {}
    for source in data["sources"]:
        sid = source["source_id"]
        anchors = deepcopy(source["verification_anchors"])
        anchors += [e.model_dump() for c in suite.cases for e in c.evidence if e.source_id == sid]
        pages = tuple(
            PageText(" ".join(a["anchor"] for a in anchors if a["pdf_page"] == n), n)
            for n in range(1, source["page_count"] + 1)
        )
        parsed = ParsedText(pages)
        raw = ("synthetic-" + sid).encode()
        source.update(
            bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), characters=parsed.characters
        )
        target = tmp_path / source["cache_file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        parsed_by_raw[raw] = parsed
    corpus = Corpus.model_validate(data)
    data = suite.model_dump()
    data["corpus_sha256"] = canonical_digest(corpus)
    suite = EvalSuite.model_validate(data)

    class Parser:
        memory_limited = False

        def __init__(self, settings):
            pass

        def parse(self, raw, media_type):
            return parsed_by_raw[raw]

    monkeypatch.setattr(day8, "IsolatedParser", Parser)
    return corpus, suite, tmp_path


def test_verify_success_and_offline_materialization_are_reproducible(synthetic_files, monkeypatch):
    corpus, suite, root = synthetic_files
    lock = {
        "corpus_sha256": canonical_digest(corpus),
        "evals_sha256": canonical_digest(suite),
        "acceptance_gaps": acceptance_gaps(suite),
    }
    monkeypatch.setattr(day8, "load_contracts", lambda root: (corpus, suite, lock))
    first = day8.run("prepare", root=root)
    second = day8.run("prepare", root=root)
    assert first["status"] == "passed" and first["anchors_checked"] >= 50
    assert first["prepared_snapshot_reused"] is False and second["prepared_snapshot_reused"] is True
    assert first["prepared_snapshot_sha256"] == second["prepared_snapshot_sha256"]
    assert first["database_accessed"] is False and first["models_called"] is False
    assert first["day8_acceptance_ready"] is False
    with pytest.raises(DatasetRejected, match="^dataset_acceptance_incomplete$"):
        day8.run("prepare", root=root, require_acceptance=True)


@pytest.mark.parametrize(
    "change,code",
    [
        ("missing", "source_cache_missing"),
        ("size", "source_size_mismatch"),
        ("hash", "source_hash_mismatch"),
        ("profile", "parser_profile_mismatch"),
        ("text", "evidence_anchor_missing"),
        ("chunks", "chunk_anchor_missing"),
        ("extraction", "source_extraction_changed"),
    ],
)
def test_verify_failed_evidence_or_bytes_never_pass(synthetic_files, monkeypatch, change, code):
    corpus, suite, root = synthetic_files
    source = corpus.sources[0]
    path = root / source.cache_file
    if change == "missing":
        path.unlink()
    elif change == "size":
        path.write_bytes(path.read_bytes() + b"x")
    elif change == "hash":
        path.write_bytes(b"x" * source.bytes)
    elif change == "profile":
        corpus = corpus.model_copy(
            update={"profile": corpus.profile.model_copy(update={"parser_version": "different"})}
        )
    elif change == "chunks":
        monkeypatch.setattr(day8, "split_text", lambda parsed: ())
    else:

        class BadParser:
            memory_limited = False

            def __init__(self, settings):
                pass

            def parse(self, raw, media_type):
                count = source.characters if change == "text" else 1
                return ParsedText(
                    tuple(
                        PageText("x" * count if n == 1 else "", n)
                        for n in range(1, source.page_count + 1)
                    )
                )

        monkeypatch.setattr(day8, "IsolatedParser", BadParser)
    with pytest.raises(DatasetRejected, match=f"^{code}$"):
        day8.verify(corpus, suite, root)


def test_download_restores_only_missing_files_with_mock(synthetic_files, monkeypatch):
    corpus, suite, root = synthetic_files
    source = corpus.sources[0]
    (root / source.cache_file).unlink()
    restored = []

    async def restore(source, root):
        restored.append(source["source_id"])
        (root / source["cache_file"]).write_bytes(("synthetic-" + source["source_id"]).encode())

    monkeypatch.setattr(day8, "restore_source", restore)
    day8.verify(corpus, suite, root, download=True)
    assert restored == [source.source_id]


def test_existing_corruption_does_not_trigger_download(synthetic_files, monkeypatch):
    corpus, suite, root = synthetic_files
    (root / corpus.sources[0].cache_file).write_bytes(b"broken")

    async def forbidden(*args):
        raise AssertionError("must not replace existing bytes")

    monkeypatch.setattr(day8, "restore_source", forbidden)
    with pytest.raises(DatasetRejected, match="^source_size_mismatch$"):
        day8.verify(corpus, suite, root, download=True)


def test_cache_symlink_and_parent_escape_are_rejected(tmp_path):
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache/escape").symlink_to(tmp_path.parent, target_is_directory=True)
    for path in ["../private.pdf", ".cache/escape/private.pdf"]:
        with pytest.raises(DatasetRejected, match="^cache_path_rejected$"):
            day8.confined_path(path, tmp_path)


def test_publish_reuses_identical_and_preserves_conflicting_snapshot(tmp_path):
    path = tmp_path / "snapshot.json"
    assert day8.publish_snapshot(path, {"content": "original"}) is False
    before = path.read_bytes()
    assert day8.publish_snapshot(path, {"content": "original"}) is True
    with pytest.raises(DatasetRejected, match="^prepared_snapshot_conflict$"):
        day8.publish_snapshot(path, {"content": "changed"})
    assert path.read_bytes() == before and not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "error,code",
    [
        (RuntimeError("secret-marker"), "dataset_verification_failed"),
        (ValueError("secret-marker"), "dataset_invalid"),
        (ParseRejected("invalid_pdf"), "source_parse_failed"),
        (DatasetRejected("source_cache_missing"), "source_cache_missing"),
    ],
)
def test_cli_errors_are_sanitized(monkeypatch, capsys, error, code):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(day8, "run", fail)
    monkeypatch.setattr(sys, "argv", ["day8", "check"])
    assert day8.main() == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "error_code": code}


def test_cache_uses_one_existing_inventory_and_explicit_ignore():
    manager = (ROOT / "scripts/dev.sh").read_text()
    ignored = (ROOT / ".gitignore").read_text()
    assert '"day8_corpus_reports_previews_and_downloads"' in manager
    assert "/.cache/manifest.json" in ignored and "/.cache/day8/" in ignored
