"""Subprocess worker for attachment text extraction.

Reads ``{"data": <base64>, "extension": ".pdf", "max_chars": 6000}`` from stdin
and prints a single JSON line with ``{"text": ...}`` or ``{"error": ...}``.
Runs with an address-space limit on POSIX so a hostile file cannot exhaust the
host's memory.
"""

from __future__ import annotations

import base64
import binascii
import json
import sys


def _apply_memory_limit() -> None:
    if sys.platform.startswith("win"):
        return
    try:
        import resource

        from mcp_email_server.attachment_text import WORKER_MEMORY_LIMIT_BYTES

        resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY_LIMIT_BYTES, WORKER_MEMORY_LIMIT_BYTES))
    except (ImportError, ValueError, OSError):
        pass


def main() -> int:
    _apply_memory_limit()
    try:
        request = json.loads(sys.stdin.read())
        data = base64.b64decode(request["data"], validate=True)
        extension = str(request["extension"]).lower()
        max_chars = int(request.get("max_chars", 6000))

        from mcp_email_server.attachment_text import extract_text_in_process

        text = extract_text_in_process(data, extension, max_chars)
        print(json.dumps({"text": text}))
        return 0
    except (ValueError, KeyError, binascii.Error) as exc:
        print(json.dumps({"error": f"{exc.__class__.__name__}: {exc}"}))
        return 0
    except Exception as exc:  # never leak a traceback to the caller
        print(json.dumps({"error": f"{exc.__class__.__name__}"}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
