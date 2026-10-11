import asyncio
import json
import os
import shutil
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from pypdf import PdfWriter

from app.experiments import day1
from app.experiments.day1 import LocalModel, ProbeError, cosine, dispatch_tool, extract_pdf
from app.main import app
from scripts import day1 as runner
from scripts.day1 import container_probe


def test_cache_wrapper_creates_test_parent_and_preserves_docker_context(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(Path(__file__).resolve().parents[1] / "scripts/dev.sh", scripts / "dev.sh")
    config = tmp_path / ".cache/docker/config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"currentContext": "preserve-existing-context"}')
    subprocess.run(["bash", str(scripts / "dev.sh"), sys.executable, "-c", "pass"], check=True)
    assert (tmp_path / ".cache/pytest").is_dir()
    assert (tmp_path / ".cache/colima").is_dir()
    assert (tmp_path / ".cache/lima").is_dir()
    assert json.loads(config.read_text())["currentContext"] == "preserve-existing-context"
    assert json.loads((tmp_path / ".cache/manifest.json").read_text())["root"] == ".cache/"


@pytest.mark.skipif(
    os.environ.get("TRACEAGENT_CONTAINER_TESTS") != "1", reason="requires running local Docker"
)
def test_real_container_success_isolation_and_failure():
    result = container_probe()
    assert result["success"]["exit_code"] == 0
    assert result["edge"]["host_project_not_mounted"] is True
    assert result["failure"]["missing_command_exit_code"] == 127


@pytest.mark.parametrize(
    "error,code",
    [
        (ProbeError("model_timeout"), "model_timeout"),
        (RuntimeError("private SQL token and stack"), "unexpected_probe_error"),
    ],
)
def test_cli_failure_does_not_expose_internal_details(tmp_path, monkeypatch, capsys, error, code):
    def fail():
        raise error

    monkeypatch.setattr(runner, "OUTPUT", tmp_path)
    monkeypatch.setattr(runner, "health_probe", fail)
    monkeypatch.setattr(sys, "argv", ["day1", "health"])
    assert runner.main() == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"probe": "health", "status": "failed", "error_code": code}
    assert json.loads((tmp_path / "health.json").read_text()) == {
        "status": "failed",
        "error_code": code,
    }


def test_cli_report_write_failure_is_controlled(tmp_path, monkeypatch, capsys):
    blocked = tmp_path / "file-instead-of-directory"
    blocked.write_text("synthetic fixture")
    monkeypatch.setattr(runner, "OUTPUT", blocked)
    monkeypatch.setattr(runner, "health_probe", lambda: {"synthetic": True})
    monkeypatch.setattr(sys, "argv", ["day1", "health"])
    assert runner.main() == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "report_write_failed"


def api_request(method, path):
    async def call():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
        ) as client:
            return await client.request(method, path)

    return asyncio.run(call())


def test_health_contract():
    result = api_request("GET", "/health")
    assert result.status_code == 200
    assert result.json() == {"status": "ok"}


def test_missing_route():
    assert api_request("GET", "/not-found").status_code == 404


def test_health_wrong_method():
    assert api_request("POST", "/health").status_code == 405


def test_valid_tool_dispatch_marks_synthetic_data():
    result = dispatch_tool({"name": "search_evidence", "arguments": {"query": " methods "}})
    assert result == {"synthetic": True, "query": "methods", "source_ids": ["demo-1"]}
    assert (
        dispatch_tool({"name": "read_source", "arguments": {"source_id": "demo-1"}})["synthetic"]
        is True
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": " "},
        {"query": 123},
        {"query": "x", "owner_id": "other"},
        {"query": "x" * 2001},
    ],
)
def test_invalid_tool_arguments_never_execute(arguments):
    with pytest.raises(ProbeError, match="invalid_tool_arguments"):
        dispatch_tool({"name": "search_evidence", "arguments": arguments})


def test_source_id_cannot_be_arbitrary_file_path():
    with pytest.raises(ProbeError, match="invalid_tool_arguments"):
        dispatch_tool({"name": "read_source", "arguments": {"source_id": "../../secret"}})


def test_unknown_tool_rejected():
    with pytest.raises(ProbeError, match="unknown_tool"):
        dispatch_tool({"name": "run_shell", "arguments": {}})


def make_model(handler, **kwargs):
    return LocalModel(transport=httpx.MockTransport(handler), **kwargs)


def test_chat_reads_real_api_shape_and_counts_usage():
    def handler(request):
        assert request.url.path == "/api/chat"
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "Hello."},
                "prompt_eval_count": 4,
                "eval_count": 2,
            },
        )

    model = make_model(handler)
    try:
        assert model.chat([{"role": "user", "content": "hello"}])["content"] == "Hello."
        assert model.chat_calls == 1
        assert model.usage[0]["input_tokens"] == 4
        assert model.usage[0]["cloud_cost"] == 0
    finally:
        model.close()


def test_blank_chat_prevents_network_request():
    def handler(request):
        pytest.fail("blank input reached the server")

    model = make_model(handler)
    try:
        with pytest.raises(ProbeError, match="empty_input"):
            model.chat([{"role": "user", "content": " "}])
        assert model.chat_calls == 0
    finally:
        model.close()


def test_timeout_consumes_budget_and_never_retries_or_leaks_details():
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("secret token and private provider body", request=request)

    model = make_model(handler, chat_limit=1)
    try:
        with pytest.raises(ProbeError) as failure:
            model.chat([{"role": "user", "content": "hello"}])
        assert str(failure.value) == "model_timeout"
        with pytest.raises(ProbeError, match="call_budget_exhausted"):
            model.chat([{"role": "user", "content": "hello"}])
        assert len(requests) == 1
    finally:
        model.close()


@pytest.mark.parametrize(
    "body,code",
    [
        ([], "invalid_model_response"),
        ({"message": {"role": "assistant", "content": ""}}, "empty_or_invalid_model_output"),
        ({"message": {"role": "user", "content": "hello"}}, "invalid_model_message"),
        (
            {"message": {"role": "assistant", "content": "", "tool_calls": [None]}},
            "invalid_tool_call",
        ),
    ],
)
def test_malformed_model_outputs_rejected(body, code):
    model = make_model(lambda request: httpx.Response(200, json=body))
    try:
        with pytest.raises(ProbeError, match=code):
            model.chat([{"role": "user", "content": "hello"}])
    finally:
        model.close()


def test_http_error_body_never_exposed():
    model = make_model(lambda request: httpx.Response(401, text="secret SQL and API key"))
    try:
        with pytest.raises(ProbeError) as failure:
            model.chat([{"role": "user", "content": "hello"}])
        assert str(failure.value) == "model_http_error"
    finally:
        model.close()


def test_total_deadline_cancels_slow_request(monkeypatch):
    original_timeout = asyncio.timeout
    monkeypatch.setattr(day1.asyncio, "timeout", lambda seconds: original_timeout(0.01))

    async def slow_handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "late"}})

    model = make_model(slow_handler)
    try:
        with pytest.raises(ProbeError, match="model_timeout"):
            model.chat([{"role": "user", "content": "hello"}])
        assert model.chat_calls == 1
    finally:
        model.close()


def test_invalid_json_rejected_without_body():
    model = make_model(lambda request: httpx.Response(200, text="private invalid JSON"))
    try:
        with pytest.raises(ProbeError, match="invalid_model_json"):
            model.chat([{"role": "user", "content": "hello"}])
    finally:
        model.close()


def test_cloud_endpoint_rejected():
    with pytest.raises(ProbeError, match="local_endpoint_required"):
        LocalModel("https://api.example.com")


def test_embedding_success_and_blank_input_boundary():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"embeddings": [[1, 0], [0, 1]], "prompt_eval_count": 5})

    model = make_model(handler)
    try:
        assert model.embed(["one", "two"]) == [[1, 0], [0, 1]]
        with pytest.raises(ProbeError, match="invalid_embedding_input"):
            model.embed([" "])
        assert len(requests) == 1
    finally:
        model.close()


def test_cosine_success():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0)


@pytest.mark.parametrize(
    "left,right,code",
    [
        ([1], [1, 0], "embedding_dimension_mismatch"),
        ([0, 0], [1, 0], "zero_embedding"),
        ([float("nan")], [1], "invalid_embedding_value"),
        ([True], [1], "invalid_embedding_value"),
        ([], [], "empty_embedding"),
    ],
)
def test_invalid_vectors_rejected(left, right, code):
    with pytest.raises(ProbeError, match=code):
        cosine(left, right)


def pdf_fixture(tmp_path: Path, *, text=False, blank_first=False, encrypted=False, image=False):
    writer = PdfWriter()
    if blank_first:
        writer.add_blank_page(300, 300)
    page = writer.add_blank_page(300, 300)
    if text:
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 20 100 Td (Synthetic engineering test fixture) Tj ET")
        page[NameObject("/Contents")] = stream
    if image:
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

        picture = DecodedStreamObject()
        picture.set_data(b"\x00\x00\x00")
        picture.update(
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
            {NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): picture})}
        )
        stream = DecodedStreamObject()
        stream.set_data(b"q 100 0 0 100 20 20 cm /Im1 Do Q")
        page[NameObject("/Contents")] = stream
    if encrypted:
        writer.encrypt("synthetic-test-password")
    buffer = BytesIO()
    writer.write(buffer)
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(buffer.getvalue())
    return path


def test_pdf_success_hash_and_physical_page_number(tmp_path):
    result = extract_pdf(pdf_fixture(tmp_path, text=True, blank_first=True))
    assert result["page_count"] == 2
    assert len(result["sha256"]) == 64
    assert result["pages"][0]["text"] == ""
    assert result["pages"][1]["pdf_page"] == 2
    assert "Synthetic engineering" in result["pages"][1]["text"]


@pytest.mark.parametrize("image", [False, True])
def test_image_or_blank_only_pdf_cannot_succeed(tmp_path, image):
    with pytest.raises(ProbeError, match="no_extractable_text"):
        extract_pdf(pdf_fixture(tmp_path, image=image))


def test_encrypted_pdf_rejected(tmp_path):
    with pytest.raises(ProbeError, match="encrypted_pdf"):
        extract_pdf(pdf_fixture(tmp_path, encrypted=True))


def test_corrupt_pdf_rejected(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7\nThis is a synthetic broken fixture.")
    with pytest.raises(ProbeError, match="pdf_parse_failed"):
        extract_pdf(path)


def test_oversized_pdf_rejected_before_parsing(tmp_path):
    path = tmp_path / "large.pdf"
    with path.open("wb") as file:
        file.truncate(5 * 1024 * 1024 + 1)
    with pytest.raises(ProbeError, match="pdf_too_large"):
        extract_pdf(path)


def test_pdf_page_limit(tmp_path):
    writer = PdfWriter()
    for _ in range(51):
        writer.add_blank_page(300, 300)
    path = tmp_path / "many-pages.pdf"
    writer.write(path)
    with pytest.raises(ProbeError, match="too_many_pages"):
        extract_pdf(path)


def test_pdf_text_limit(tmp_path, monkeypatch):
    from pypdf import PageObject

    path = pdf_fixture(tmp_path, text=True)
    monkeypatch.setattr(PageObject, "extract_text", lambda page: "x" * 200_001)
    with pytest.raises(ProbeError, match="too_much_text"):
        extract_pdf(path)
