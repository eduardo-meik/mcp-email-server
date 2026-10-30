"""ClamAV scanner service for Cloud Run.

POST /scan with raw file bytes and a bearer token; Cloud Run IAM validates the
OIDC token before forwarding the request. The bytes are sent to clamd using
INSTREAM and the verdict is returned as JSON. Signatures are baked into the
image; rebuilds keep them fresh without running freshclam on the request path.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

CLAMD_HOST = os.environ.get("CLAMD_HOST", "127.0.0.1")
CLAMD_PORT = int(os.environ.get("CLAMD_PORT", "3310"))
SCANNER_TOKEN = os.environ.get("SCANNER_TOKEN")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
CHUNK_SIZE = 64 * 1024

app = FastAPI(title="clamav-scanner")


def _verify_token(authorization: str | None) -> None:
    if SCANNER_TOKEN is None:
        return
    provided = ""
    if authorization and authorization.startswith("Bearer "):
        provided = authorization[7:].strip()
    if not provided or provided != SCANNER_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def _instream_scan(chunks) -> tuple[str, str | None]:
    """Send chunks to clamd via INSTREAM; return (status, signature)."""

    with socket.create_connection((CLAMD_HOST, CLAMD_PORT), timeout=30) as sock:
        sock.sendall(b"zINSTREAM\x00")
        for chunk in chunks:
            if not chunk:
                continue
            sock.sendall(struct.pack(">I", len(chunk)) + chunk)
        sock.sendall(struct.pack(">I", 0))

        response = b""
        while not response.endswith(b"\x00"):
            data = sock.recv(4096)
            if not data:
                break
            response += data

    verdict = response.rstrip(b"\x00\r\n").decode("utf-8", errors="replace")
    if verdict.endswith("OK"):
        return "clean", None
    if verdict.endswith("FOUND"):
        signature = verdict.rsplit(" ", 2)[-2] if " " in verdict else None
        return "infected", signature
    raise RuntimeError(f"unexpected clamd response: {verdict}")


@app.get("/healthz")
async def healthz() -> dict:
    try:
        await asyncio.to_thread(_clamd_ping)
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Scanner unavailable") from exc
    return {"status": "ok"}


@app.post("/scan")
async def scan(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    _verify_token(authorization)

    body = await request.body()
    if not body:
        return JSONResponse({"status": "error", "detail": "empty body"}, status_code=400)
    if len(body) > MAX_UPLOAD_BYTES:
        return JSONResponse({"status": "error", "detail": "file too large"}, status_code=413)

    try:
        chunks = [body[i : i + CHUNK_SIZE] for i in range(0, len(body), CHUNK_SIZE)]
        result, signature = await asyncio.to_thread(_instream_scan, chunks)
    except (OSError, RuntimeError) as exc:
        return JSONResponse({"status": "error", "detail": exc.__class__.__name__}, status_code=503)

    payload: dict[str, str] = {"status": result}
    if signature:
        payload["signature"] = signature
    return JSONResponse(payload)


def _clamd_ping() -> None:
    with socket.create_connection((CLAMD_HOST, CLAMD_PORT), timeout=2) as sock:
        sock.sendall(b"zPING\x00")
        response = sock.recv(32)
    if response.rstrip(b"\x00\r\n") != b"PONG":
        raise OSError("clamd did not respond to PING")
