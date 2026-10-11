"""Run with: bash scripts/dev.sh .venv/bin/python -m scripts.day1 health|models|pdf|container."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

import httpx

from app.experiments.day1 import (
    LocalModel,
    ProbeError,
    cosine,
    dispatch_tool,
    extract_pdf,
    save_report,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".cache/day1"


def health_probe():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with (OUTPUT / "api.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(timeout=2, trust_env=False) as client:
                deadline = monotonic() + 15
                while True:
                    try:
                        response = client.get(url + "/health")
                        break
                    except httpx.RequestError:
                        if monotonic() >= deadline or process.poll() is not None:
                            raise ProbeError("api_startup_failed") from None
                        sleep(0.1)
                missing = client.get(url + "/not-found")
                if (
                    response.status_code != 200
                    or response.json() != {"status": "ok"}
                    or missing.status_code != 404
                ):
                    raise ProbeError("health_contract_failed")
                result = {
                    "success": {"status": response.status_code, "body": response.json()},
                    "edge": {"missing_route_status": missing.status_code},
                }
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            client.get(url + "/health")
    except httpx.RequestError:
        result["failure"] = {"server_stopped": "connection_failed_as_expected"}
    else:
        raise ProbeError("stopped_server_still_responds")
    return result


def model_probe():
    model = LocalModel()
    try:
        generation = model.chat(
            [
                {
                    "role": "user",
                    "content": "In exactly two short English sentences, summarize: TraceAgent is a Python backend for traceable public-document research. It preserves source locations and refuses unsupported answers.",
                }
            ]
        )
        traces = []
        cases = [
            ("Find evidence about research methods.", "search_evidence"),
            ("Read source demo-1.", "read_source"),
        ]
        for prompt, expected in cases:
            messages = [
                {
                    "role": "system",
                    "content": "Use the appropriate read-only tool for source requests. Fixtures are synthetic engineering data. Make exactly one tool call. Do not invent evidence.",
                },
                {"role": "user", "content": prompt},
            ]
            selected = model.chat(messages, tools=True)
            calls = selected.get("tool_calls", [])
            if len(calls) != 1 or calls[0].get("function", {}).get("name") != expected:
                save_report(
                    OUTPUT / "unexpected-tool-output.json",
                    {"expected": expected, "message": selected},
                )
                raise ProbeError("tool_selection_failed")
            observation = dispatch_tool(calls[0]["function"])
            messages.extend(
                [
                    selected,
                    {"role": "tool", "tool_name": expected, "content": json.dumps(observation)},
                ]
            )
            final = model.chat(messages)
            if final.get("tool_calls"):
                raise ProbeError("unexpected_additional_tool_call")
            traces.append(
                {
                    "prompt": prompt,
                    "assistant_call": selected,
                    "observation": observation,
                    "final": final,
                }
            )
        greeting = model.chat(
            [{"role": "user", "content": "Say hello in one short sentence."}], tools=True
        )
        if greeting.get("tool_calls"):
            raise ProbeError("unnecessary_tool_call")
        texts = [
            "search_document: FastAPI builds HTTP APIs in Python.",
            "search_document: Python web APIs can be built with FastAPI.",
            "search_document: A banana is a yellow fruit.",
        ]
        vectors = model.embed(texts)
        similar, unrelated = cosine(vectors[0], vectors[1]), cosine(vectors[0], vectors[2])
        if similar <= unrelated:
            raise ProbeError("embedding_example_ranking_failed")
        return {
            "provider": "local_ollama",
            "chat_model": "qwen3:1.7b",
            "embedding_model": "nomic-embed-text:v1.5",
            "generation": generation,
            "tool_traces": traces,
            "greeting": greeting,
            "embedding": {
                "texts": texts,
                "dimension": len(vectors[0]),
                "same_topic_cosine": similar,
                "unrelated_cosine": unrelated,
                "count": len(vectors),
            },
            "usage": model.usage,
            "chat_calls": model.chat_calls,
            "embedding_calls": model.embed_calls,
            "cloud_cost": 0,
            "limitations": ["synthetic tool observations", "three-text embedding smoke check"],
        }
    finally:
        model.close()


def pdf_probe():
    manifest = json.loads((ROOT / "docs/day1-sources.json").read_text())
    results = []
    for source in manifest["sources"]:
        result = extract_pdf(ROOT / source["cache_file"])
        save_report(OUTPUT / (source["source_id"] + "-pages.json"), result)
        matches = []
        for anchor in source["verification_anchors"]:
            hits = [
                page["pdf_page"]
                for page in result["pages"]
                if anchor.lower() in " ".join(page["text"].split()).lower()
            ]
            if not hits:
                raise ProbeError("pdf_anchor_not_found")
            matches.append({"anchor": anchor, "pdf_pages": hits})
        results.append(
            {
                **source,
                **{key: value for key, value in result.items() if key != "pages"},
                "anchor_locations": matches,
            }
        )
    return {"topic": manifest["topic"], "sources": results, "parser": "pypdf 6.20.0"}


def container_probe():
    def run(arguments):
        try:
            return subprocess.run(
                ["docker", *arguments], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ProbeError("container_runtime_unavailable") from None

    version = run(["version", "--format", "{{json .}}"])
    if version.returncode:
        raise ProbeError("container_runtime_unavailable")
    image = "alpine@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1"
    success = run(["run", "--rm", "--network", "none", image, "printf", "traceagent-container-ok"])
    edge = run(
        [
            "run",
            "--rm",
            "--network",
            "none",
            image,
            "/bin/sh",
            "-c",
            'test ! -e "$1"',
            "sh",
            str(ROOT / "README.md"),
        ]
    )
    failure = run(["run", "--rm", "--network", "none", image, "/missing-traceagent-command"])
    if (
        success.returncode != 0
        or success.stdout != "traceagent-container-ok"
        or edge.returncode != 0
        or failure.returncode != 127
    ):
        raise ProbeError("container_probe_failed")
    return {
        "versions": json.loads(version.stdout),
        "image": image,
        "success": {"exit_code": success.returncode, "output": success.stdout},
        "edge": {"host_project_not_mounted": True, "network": "none", "exit_code": edge.returncode},
        "failure": {"missing_command_exit_code": failure.returncode},
        "test_containers": "automatically_removed",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("probe", choices=["health", "models", "pdf", "container"])
    args = parser.parse_args()
    try:
        result = {
            "health": health_probe,
            "models": model_probe,
            "pdf": pdf_probe,
            "container": container_probe,
        }[args.probe]()
    except ProbeError as error:
        result = {"status": "failed", "error_code": str(error)}
    except Exception:
        result = {"status": "failed", "error_code": "unexpected_probe_error"}
    else:
        result = {"status": "passed", **result}
    try:
        save_report(OUTPUT / (args.probe + ".json"), result)
    except OSError:
        print(
            json.dumps(
                {"probe": args.probe, "status": "failed", "error_code": "report_write_failed"}
            )
        )
        return 1
    summary = {"probe": args.probe, "status": result["status"]}
    if result["status"] == "failed":
        summary["error_code"] = result["error_code"]
    else:
        summary["report"] = str(OUTPUT / (args.probe + ".json"))
    print(json.dumps(summary))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
