"""Real parser and deterministic locator checks, with explicit fault-injection boundaries."""

import json
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest
from pydantic import ValidationError
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from app.core.config import Settings
from app.ingestion import parser as parser_module
from app.ingestion import text as text_module
from app.ingestion.parser import IsolatedParser
from app.ingestion.text import PageText, ParsedText, ParseRejected, parse_bytes, split_text
from app.storage.files import FileStore, StorageUnavailable
from scripts import day5
from scripts.day4 import pdf_fixture


def pdf_pages(*texts: str, encrypted=False, image_page=False) -> bytes:
    writer = PdfWriter()
    for text in texts:
        if text:
            writer.add_page(PdfReader(BytesIO(pdf_fixture())).pages[0])
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 25 100 Td (" + text.encode("ascii") + b") Tj ET")
            writer.pages[-1][NameObject("/Contents")] = writer._add_object(stream)
        else:
            page = writer.add_blank_page(width=300, height=200)
            if image_page:
                image = DecodedStreamObject()
                image.set_data(b"\xff\xff\xff")
                image.update(
                    {
                        NameObject("/Type"): NameObject("/XObject"),
                        NameObject("/Subtype"): NameObject("/Image"),
                        NameObject("/Width"): NumberObject(1),
                        NameObject("/Height"): NumberObject(1),
                        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                        NameObject("/BitsPerComponent"): NumberObject(8),
                    }
                )
                page[NameObject("/Resources")] = DictionaryObject(
                    {
                        NameObject("/XObject"): DictionaryObject(
                            {NameObject("/I1"): writer._add_object(image)}
                        )
                    }
                )
    if encrypted:
        writer.encrypt("test-fixture-password")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def cyclic_pdf() -> bytes:
    # A self-referential page tree, with a valid xref table. Feed only to the child.
    data = b"%PDF-1.7\n"
    offsets = [0]
    for number, body in enumerate(
        [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Count 1 /Kids [2 0 R] >>"],
        start=1,
    ):
        offsets.append(len(data))
        data += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 3\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    data += f"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return data


def test_txt_bom_crlf_unicode_locator_round_trip():
    text = "中文证据\r\n" + "evidence " * 240 + "\n末尾"
    parsed = parse_bytes(b"\xef\xbb\xbf" + text.encode(), "text/plain")
    assert parsed.pages == (PageText(text),)
    chunks = split_text(parsed)
    assert len(chunks) > 1 and chunks == split_text(parsed)
    covered = set()
    for ordinal, chunk in enumerate(chunks):
        assert chunk.ordinal == ordinal and chunk.pdf_page is None
        assert chunk.content == text[chunk.char_start : chunk.char_end]
        assert 0 < len(chunk.content) <= 1000
        covered.update(range(chunk.char_start, chunk.char_end))
    assert covered == set(range(len(text)))
    assert chunks[1].char_start == chunks[0].char_end - 100


@pytest.mark.parametrize("length,expected", [(1, 1), (1000, 1), (1001, 2), (1900, 2), (1901, 3)])
def test_chunk_boundaries_and_short_tail(length, expected):
    chunks = split_text(ParsedText((PageText("x" * length),)))
    assert len(chunks) == expected and chunks[-1].char_end == length


@pytest.mark.parametrize("size,overlap", [(0, 0), (10, -1), (10, 10), (1, 2), (True, 0)])
def test_invalid_chunk_configuration_cannot_loop(size, overlap):
    with pytest.raises(ValueError, match="^invalid_chunk_parameters$"):
        split_text(ParsedText((PageText("text"),)), size=size, overlap=overlap)


def test_pdf_blank_page_preserves_physical_number_and_never_crosses_pages():
    parsed = parse_bytes(pdf_pages("first " * 200, "", "third page"), "application/pdf")
    assert [p.pdf_page for p in parsed.pages] == [1, 2, 3]
    chunks = split_text(parsed)
    assert [c.pdf_page for c in chunks] == [1, 1, 3]
    assert all(c.char_start is None and c.char_end is None for c in chunks)
    assert all(c.content in parsed.pages[c.pdf_page - 1].text for c in chunks)


@pytest.mark.parametrize(
    "data,code",
    [(b"\xff", "invalid_text"), (b"x\0y", "invalid_text"), (b" \r\n", "no_extractable_text")],
)
def test_txt_rejections(data, code):
    with pytest.raises(ParseRejected, match="^" + code + "$"):
        parse_bytes(data, "text/plain")


def test_exact_character_and_page_limits():
    assert parse_bytes(b"x" * 200_000, "text/plain").characters == 200_000
    with pytest.raises(ParseRejected, match="^too_much_text$"):
        parse_bytes(b"x" * 200_001, "text/plain")
    assert len(parse_bytes(pdf_pages("text", *([""] * 49)), "application/pdf").pages) == 50
    with pytest.raises(ParseRejected, match="^too_many_pages$"):
        parse_bytes(pdf_pages("text", *([""] * 50)), "application/pdf")
    data = pdf_pages("1234567890")
    count = parse_bytes(data, "application/pdf").characters
    assert parse_bytes(data, "application/pdf", max_characters=count).characters == count
    with pytest.raises(ParseRejected, match="^too_much_text$"):
        parse_bytes(data, "application/pdf", max_characters=count - 1)


@pytest.mark.parametrize(
    "factory,code",
    [
        (lambda: b"%PDF-1.7\nbroken", "invalid_pdf"),
        (lambda: pdf_pages("text", encrypted=True), "encrypted_pdf"),
        (lambda: pdf_pages(""), "no_extractable_text"),
        (lambda: pdf_pages("", image_page=True), "unsupported_image_page"),
        (lambda: pdf_pages("text", "", image_page=True), "unsupported_image_page"),
    ],
)
def test_real_pdf_failure_paths(factory, code):
    with pytest.raises(ParseRejected, match="^" + code + "$"):
        IsolatedParser(Settings()).parse(factory(), "application/pdf")


def test_cyclic_pdf_is_rejected_in_real_child():
    with pytest.raises(ParseRejected):
        IsolatedParser(Settings(parse_timeout_seconds=3)).parse(cyclic_pdf(), "application/pdf")


def test_decoded_stream_limit(monkeypatch):
    monkeypatch.setattr(text_module, "MAX_PAGE_STREAM_BYTES", 5)
    with pytest.raises(ParseRejected, match="^parse_resource_limit$"):
        parse_bytes(pdf_pages("text"), "application/pdf")


def test_real_child_success_and_environment_does_not_inherit_secrets(monkeypatch):
    original = parser_module.subprocess.Popen
    observed = {}

    def start(*args, **kwargs):
        observed.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setenv("TRACEAGENT_DB_URL", "private-marker")
    monkeypatch.setenv("TRACEAGENT_SEED_API_KEY_A", "private-marker")
    monkeypatch.setattr(parser_module.subprocess, "Popen", start)
    parser = IsolatedParser(Settings())
    assert parser.parse(pdf_pages("evidence"), "application/pdf").pages[0].pdf_page == 1
    assert parser.memory_limited in {True, False}
    assert "private-marker" not in repr(observed)
    assert observed["stderr"] == subprocess.DEVNULL


def test_real_hung_child_is_killed_and_reaped(monkeypatch):
    original = parser_module.subprocess.Popen
    children = []

    def hung(*args, **kwargs):
        process = original([sys.executable, "-c", "import time;time.sleep(30)"], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(parser_module.subprocess, "Popen", hung)
    with pytest.raises(ParseRejected, match="^parse_timeout$"):
        IsolatedParser(Settings(parse_timeout_seconds=0.1)).parse(b"text", "text/plain")
    assert children[0].poll() is not None and children[0].returncode != 0


@pytest.mark.parametrize("code", ["import sys;sys.exit(2)", "print('not json')", "print('{}')"])
def test_child_crash_or_invalid_protocol_is_safe(monkeypatch, code):
    original = parser_module.subprocess.Popen

    def child(*args, **kwargs):
        return original([sys.executable, "-c", code], **kwargs)

    monkeypatch.setattr(parser_module.subprocess, "Popen", child)
    with pytest.raises(ParseRejected, match="^parse_failed$"):
        IsolatedParser(Settings()).parse(b"text", "text/plain")


@pytest.mark.parametrize(
    "result",
    [
        {"pages": [{"text": "x", "pdf_page": True}]},
        {"pages": [{"text": "x", "pdf_page": 2}]},
        {"pages": [{"text": " ", "pdf_page": 1}]},
        {"pages": [{"text": 123, "pdf_page": 1}]},
        {"pages": [{"text": "unsafe\u0000text", "pdf_page": 1}]},
        {"pages": [{"text": "\ud800", "pdf_page": 1}]},
        {"error": "private SQL exception"},
    ],
)
def test_invalid_child_output_is_never_a_success(result):
    with pytest.raises(ParseRejected, match="^parse_failed$"):
        IsolatedParser(Settings())._decode(
            json.dumps({"memory_limited": False, **result}).encode(), "application/pdf"
        )


@pytest.mark.parametrize("value", [0, -1, 121, float("inf")])
def test_parser_timeout_configuration_is_bounded(value):
    with pytest.raises(ValidationError):
        Settings(parse_timeout_seconds=value)


def test_verified_read_checks_same_bytes_and_rejects_fifo(tmp_path):
    store = FileStore(tmp_path / "uploads")
    artifact = store.save(
        BytesIO(b"trusted bytes"), "text/plain", max_bytes=100, max_characters=100
    )
    assert store.read_verified(artifact.reference, artifact.sha256) == b"trusted bytes"
    store.path(artifact.reference.key).write_bytes(b"altered bytes")
    with pytest.raises(StorageUnavailable):
        store.read_verified(artifact.reference, artifact.sha256)
    store.path(artifact.reference.key).unlink()
    os.mkfifo(store.path(artifact.reference.key), 0o600)
    with pytest.raises(StorageUnavailable):
        store.read_verified(artifact.reference, artifact.sha256)


def test_day5_cache_inventory_is_ignored():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".cache/manifest.json").read_text())
    assert manifest["day5_reports_fixtures_and_pdf_previews"] == "day5/"
    paths = [".cache/manifest.json", ".cache/day5/parser-before.json", ".cache/day5/preview.png"]
    result = subprocess.run(
        ["git", "check-ignore", *paths], cwd=root, capture_output=True, text=True
    )
    assert result.returncode == 0 and set(result.stdout.splitlines()) == set(paths)


def test_parser_launcher_failure_is_safe(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private process detail")

    monkeypatch.setattr(parser_module.subprocess, "Popen", fail)
    with pytest.raises(ParseRejected, match="^parse_failed$"):
        IsolatedParser(Settings()).parse(b"text", "text/plain")


@pytest.mark.parametrize("data", [b"", b"x" * (5 * 1024 * 1024 + 1)])
def test_file_limit_rejected_before_starting_child(data, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("oversized_or_empty_input_started_child")

    monkeypatch.setattr(parser_module.subprocess, "Popen", forbidden)
    with pytest.raises(ParseRejected, match="^parse_resource_limit$"):
        IsolatedParser(Settings()).parse(data, "text/plain")


def test_demo_failure_fixture_does_not_consume_application_quota(tmp_path, monkeypatch):
    app_url = "postgresql+psycopg://app:synthetic@127.0.0.1:55432/traceagent"
    test_url = "postgresql+psycopg://test:synthetic@127.0.0.1:55432/traceagent_test"
    original = {"TRACEAGENT_DB_URL": app_url, "TRACEAGENT_TEST_DB_URL": test_url}
    monkeypatch.setattr(day5, "CACHE", tmp_path)
    result = day5.failure_environment(original)
    assert result["TRACEAGENT_DB_URL"] == test_url
    assert result["TRACEAGENT_UPLOAD_ROOT"] == str(tmp_path / "failure-uploads")
    assert original["TRACEAGENT_DB_URL"] == app_url
    with pytest.raises(day5.day3.ProbeFailure, match="^isolated_database_name_required$"):
        day5.failure_environment({**original, "TRACEAGENT_TEST_DB_URL": app_url})


def test_day5_cli_unexpected_failure_never_prints_exception(monkeypatch, capsys):
    def fail():
        raise RuntimeError("private password SQL source text")

    monkeypatch.setattr(day5, "corpus", fail)
    assert day5.main(["corpus"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error_code": "day5_probe_failed",
    }
