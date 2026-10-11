import asyncio
import hashlib
import json
import sys

import httpx
import pytest

from app.experiments.day1 import ProbeError
from scripts import verify_sources as verifier


def source_fixture():
    return {
        "source_id": "synthetic",
        "cache_file": ".cache/day2/synthetic.pdf",
        "download_url": "https://journals.plos.org/synthetic.pdf",
        "sha256": hashlib.sha256(b"synthetic pinned input").hexdigest(),
        "page_count": 1,
        "verification_anchors": [{"pdf_page": 1, "anchor": "reporting requirement"}],
    }


def test_source_success_with_whitespace_normalization(tmp_path, monkeypatch):
    source = source_fixture()
    path = verifier.cache_path(source, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic pinned input")
    monkeypatch.setattr(
        verifier,
        "extract_pdf",
        lambda p: {
            "sha256": source["sha256"],
            "bytes": 22,
            "page_count": 1,
            "characters": 22,
            "pages": [{"pdf_page": 1, "text": "Reporting\nrequirement"}],
        },
    )
    assert verifier.verify_source(source, tmp_path)["anchors_checked"] == 1


def test_source_missing_cache_is_controlled(tmp_path):
    with pytest.raises(ProbeError, match="^source_cache_missing$"):
        verifier.verify_source(source_fixture(), tmp_path)


@pytest.mark.parametrize("path", ["../private.pdf", "sources/read-only.pdf"])
def test_source_paths_are_confined_to_cache(tmp_path, path):
    with pytest.raises(ProbeError, match="^source_path_outside_cache$"):
        verifier.cache_path({**source_fixture(), "cache_file": path}, tmp_path)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"sha256": "changed"}, "source_hash_mismatch"),
        ({"page_count": 2}, "source_page_count_mismatch"),
        ({"verification_anchors": [{"pdf_page": 2, "anchor": "x"}]}, "source_anchor_page_invalid"),
        ({"verification_anchors": [{"pdf_page": 1, "anchor": "absent"}]}, "source_anchor_missing"),
    ],
)
def test_changed_source_or_bad_location_is_rejected(tmp_path, monkeypatch, change, code):
    source = source_fixture()
    path = verifier.cache_path(source, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic pinned input")
    monkeypatch.setattr(
        verifier,
        "extract_pdf",
        lambda p: {
            "sha256": source["sha256"],
            "page_count": 1,
            "pages": [{"text": "reporting requirement"}],
        },
    )
    with pytest.raises(ProbeError, match="^" + code + "$"):
        verifier.verify_source({**source, **change}, tmp_path)


@pytest.mark.parametrize(
    "body,code", [(b"synthetic pinned input", None), (b"changed", "source_hash_mismatch")]
)
def test_restore_download_preserves_previous_file_on_hash_mismatch(
    tmp_path, monkeypatch, body, code
):
    source = source_fixture()
    path = verifier.cache_path(source, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"previous")
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        verifier.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            **kwargs,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
        ),
    )
    if code:
        with pytest.raises(ProbeError, match="^" + code + "$"):
            asyncio.run(verifier.restore_source(source, tmp_path))
        assert path.read_bytes() == b"previous"
    else:
        asyncio.run(verifier.restore_source(source, tmp_path))
        assert path.read_bytes() == body
    assert not path.with_suffix(".download.tmp").exists()


def test_restore_rejects_unapproved_origin_before_io(tmp_path):
    with pytest.raises(ProbeError, match="^source_download_origin_rejected$"):
        asyncio.run(
            verifier.restore_source(
                {**source_fixture(), "download_url": "https://other.example/leak-marker"}, tmp_path
            )
        )
    assert not (tmp_path / ".cache").exists()


def test_source_cli_failure_is_sanitized(tmp_path, monkeypatch, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "day2-sources.json").write_text("invalid json with leak-marker")
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify_sources"])
    assert verifier.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error_code": "source_verification_failed",
    }
