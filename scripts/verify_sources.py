"""Verify the fixed Day 2 corpus and evidence anchors; optionally restore its cache."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.experiments.day1 import ProbeError, extract_pdf

ROOT = Path(__file__).resolve().parents[1]


def cache_path(source: dict, root: Path) -> Path:
    path = (root / source["cache_file"]).resolve()
    if not path.is_relative_to((root / ".cache").resolve()):
        raise ProbeError("source_path_outside_cache")
    return path


async def restore_source(source: dict, root: Path) -> None:
    path = cache_path(source, root)
    address = urlsplit(source["download_url"])
    if (
        address.scheme != "https"
        or address.hostname != "journals.plos.org"
        or address.username is not None
        or address.password is not None
        or address.port not in {None, 443}
    ):
        raise ProbeError("source_download_origin_rejected")
    data = bytearray()
    # A total deadline and per-phase timeouts; no automatic retries or redirects.
    async with asyncio.timeout(30):
        async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
            async with client.stream("GET", source["download_url"]) as response:
                if response.status_code != 200:
                    raise ProbeError("source_download_failed")
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 5 * 1024 * 1024:
                        raise ProbeError("source_download_too_large")
    if hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise ProbeError("source_hash_mismatch")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".download.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def verify_source(source: dict, root: Path) -> dict:
    path = cache_path(source, root)
    if not path.is_file():
        raise ProbeError("source_cache_missing")
    result = extract_pdf(path)
    if result["sha256"] != source["sha256"]:
        raise ProbeError("source_hash_mismatch")
    if result["page_count"] != source["page_count"]:
        raise ProbeError("source_page_count_mismatch")
    for evidence in source["verification_anchors"]:
        page = evidence["pdf_page"]
        if not 1 <= page <= result["page_count"]:
            raise ProbeError("source_anchor_page_invalid")
        text = "".join(result["pages"][page - 1]["text"].split()).casefold()
        anchor = "".join(evidence["anchor"].split()).casefold()
        if not anchor or anchor not in text:
            raise ProbeError("source_anchor_missing")
    return {
        "source_id": source["source_id"],
        "sha256": result["sha256"],
        "bytes": result["bytes"],
        "page_count": result["page_count"],
        "characters": result["characters"],
        "anchors_checked": len(source["verification_anchors"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true", help="restore all pinned source PDFs")
    args = parser.parse_args()
    try:
        manifest = json.loads((ROOT / "docs/day2-sources.json").read_text())
        results = []
        for source in manifest["sources"]:
            if args.download:
                asyncio.run(restore_source(source, ROOT))
            results.append(verify_source(source, ROOT))
    except ProbeError as error:
        print(json.dumps({"status": "failed", "error_code": str(error)}))
        return 1
    except (TimeoutError, httpx.TimeoutException):
        print(json.dumps({"status": "failed", "error_code": "source_download_timeout"}))
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "source_verification_failed"}))
        return 1
    print(json.dumps({"status": "passed", "sources": results}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
