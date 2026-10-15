"""Private subprocess protocol. No database, authentication or network clients."""

import json
import math
import sys
from dataclasses import asdict


def apply_limits(timeout: float) -> bool:
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    cpu_seconds = max(2, math.ceil(timeout) + 1)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    except (OSError, ValueError):
        # macOS may not implement RLIMIT_AS. Never claim it is active there.
        return False
    return True


def main() -> int:
    from app.core.processes import watch_parent

    watch_parent()
    # Limit address space before importing the PDF parser.
    memory_limited = apply_limits(float(sys.argv[4]))
    from app.ingestion.text import ParseRejected, parse_bytes

    try:
        data = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
        if not data or len(data) > 5 * 1024 * 1024:
            raise ParseRejected("parse_resource_limit")
        parsed = parse_bytes(
            data, sys.argv[1], max_pages=int(sys.argv[2]), max_characters=int(sys.argv[3])
        )
        result = {"pages": [asdict(page) for page in parsed.pages], "error": None}
    except ParseRejected as error:
        result = {"error": error.code}
    except MemoryError:
        result = {"error": "parse_resource_limit"}
    except Exception:
        result = {"error": "parse_failed"}
    result["memory_limited"] = memory_limited
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
