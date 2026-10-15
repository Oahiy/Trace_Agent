"""Bounded subprocess wrapper. Timed-out children are killed and reaped."""

import json
import os
import subprocess
import sys
from pathlib import Path

from app.core.config import Settings
from app.ingestion.text import ERROR_MESSAGES, PageText, ParsedText, ParseRejected, _valid_text

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = (
    "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
    "runpy.run_module('app.ingestion.worker',run_name='__main__')"
)


class IsolatedParser:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.memory_limited: bool | None = None

    def parse(self, data: bytes, media_type: str) -> ParsedText:
        if not data or len(data) > self.settings.max_file_bytes:
            raise ParseRejected("parse_resource_limit")
        self.memory_limited = None
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    BOOTSTRAP,
                    str(ROOT),
                    media_type,
                    str(self.settings.max_pdf_pages),
                    str(self.settings.max_text_characters),
                    str(self.settings.parse_timeout_seconds),
                ],
                cwd=ROOT,
                # No API keys, DB passwords, model endpoints or inherited Python config.
                env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise ParseRejected("parse_failed") from None
        try:
            output, _ = process.communicate(data, timeout=self.settings.parse_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise ParseRejected("parse_timeout") from None
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        if process.returncode != 0 or len(output) > 2 * 1024 * 1024:
            raise ParseRejected("parse_failed")
        return self._decode(output, media_type)

    def _decode(self, output: bytes, media_type: str) -> ParsedText:
        try:
            result = json.loads(output)
            if type(result["memory_limited"]) is not bool:
                raise ValueError()
            self.memory_limited = result["memory_limited"]
            if result.get("error") is not None:
                code = result["error"]
                raise ParseRejected(code if code in ERROR_MESSAGES else "parse_failed")
            pages = result["pages"]
            if not isinstance(pages, list) or not 1 <= len(pages) <= self.settings.max_pdf_pages:
                raise ValueError()
            output_pages = []
            for index, page in enumerate(pages, start=1):
                text, number = page["text"], page["pdf_page"]
                if not isinstance(text, str) or not _valid_text(text):
                    raise ValueError()
                if media_type == "text/plain":
                    if len(pages) != 1 or number is not None:
                        raise ValueError()
                elif media_type != "application/pdf" or type(number) is not int or number != index:
                    raise ValueError()
                output_pages.append(PageText(text, number))
            parsed = ParsedText(tuple(output_pages))
            if parsed.characters > self.settings.max_text_characters or not any(
                page.text.strip() for page in parsed.pages
            ):
                raise ValueError()
            return parsed
        except ParseRejected:
            raise
        except (ValueError, KeyError, TypeError):
            raise ParseRejected("parse_failed") from None
