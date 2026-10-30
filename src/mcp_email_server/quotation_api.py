import asyncio
import logging
import re
from secrets import compare_digest
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_email_server.adapters.smtp import SmtpAdapter
from mcp_email_server.adapters.supabase import SupabaseAdapter
from mcp_email_server.config import Settings
from mcp_email_server.quotations import IDENTIFIER_PATTERN, QuotationAttempt, QuotationSendRequest, quoter_settings
from mcp_email_server.services.quotations import QuotationService

logger = logging.getLogger(__name__)
PREFIX = "/v1/quotation-emails"


class QuotationAccessGuard:
    def __init__(
        self, app: ASGIApp, token: str | None, max_bytes: int, max_concurrency: int,
        upload_timeout: float = 30,
    ) -> None:
        self.app = app
        self.token = token
        self.max_bytes = max_bytes
        self.max_concurrency = max_concurrency
        self.upload_timeout = upload_timeout
        self.active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(PREFIX):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        authorization = headers.get("authorization", "")
        if not self.token or not authorization.startswith("Bearer ") or not compare_digest(
            authorization[7:].encode(), self.token.encode()
        ):
            await JSONResponse({"detail": "Unauthorized"}, status_code=401)(scope, receive, send)
            return
        if scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        if self.active >= self.max_concurrency:
            await JSONResponse(
                {"detail": "quotation_upload_limit"}, status_code=429, headers={"Retry-After": "1"}
            )(scope, receive, send)
            return
        self.active += 1
        try:
            length = headers.get("content-length")
            if length is not None and (
                not re.fullmatch(r"[0-9]{1,20}", length) or int(length) > self.max_bytes
            ):
                await JSONResponse({"detail": "request_too_large"}, status_code=413)(scope, receive, send)
                return
            chunks = []
            size = 0
            try:
                async with asyncio.timeout(self.upload_timeout):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        size += len(chunk)
                        if size > self.max_bytes:
                            await JSONResponse({"detail": "request_too_large"}, status_code=413)(scope, receive, send)
                            return
                        chunks.append(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await JSONResponse({"detail": "upload_timeout"}, status_code=408)(scope, receive, send)
                return
            body = b"".join(chunks)
            chunks.clear()
            replayed = False

            async def replay() -> Message:
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay, send)
        finally:
            self.active -= 1


def install_quotation_api(app: FastAPI, settings: Settings) -> None:
    selected_settings = quoter_settings(settings)
    token = settings.quoter_token.get_secret_value().strip() if selected_settings is not None else None
    app.add_middleware(
        QuotationAccessGuard, token=token, max_bytes=settings.quotation_max_request_bytes,
        max_concurrency=settings.quotation_concurrency, upload_timeout=settings.quotation_upload_timeout_seconds,
    )
    service = None if selected_settings is None else QuotationService(
        selected_settings, SupabaseAdapter(selected_settings), SmtpAdapter(selected_settings)
    )
    app.state.quotation_service = service

    def resolve_service(request: Request) -> QuotationService:
        configured_service = request.app.state.quotation_service
        if configured_service is None:
            raise HTTPException(503, "quotation_api_not_configured")
        return configured_service

    @app.post(PREFIX, response_model=QuotationAttempt)
    async def send_quotation(request: Request, response: Response) -> QuotationAttempt:
        current = resolve_service(request)
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "application_json_required")
        try:
            payload = QuotationSendRequest.model_validate_json(await request.body())
            message, fingerprint = payload.prepare(current.settings)
        except (ValidationError, ValueError):
            # Never include Pydantic's input values (which may contain the PDF).
            raise HTTPException(422, "invalid_quotation_request") from None
        try:
            attempt = await current.send(payload, message, fingerprint)
        except httpx.HTTPError:
            logger.error("quotation_database_unavailable")
            raise HTTPException(503, "quotation_database_unavailable; query using the same key") from None
        if attempt.state in {"pendiente", "procesando"}:
            response.status_code = 202
        return attempt

    @app.get(PREFIX + "/by-key", response_model=QuotationAttempt)
    async def get_by_key(
        request: Request, idempotency_key: str = Header(alias="Idempotency-Key")
    ) -> QuotationAttempt:
        if not re.fullmatch(IDENTIFIER_PATTERN, idempotency_key):
            raise HTTPException(422, "invalid_idempotency_key")
        try:
            return await resolve_service(request).get(key=idempotency_key)
        except httpx.HTTPError:
            logger.error("quotation_database_unavailable")
            raise HTTPException(503, "quotation_database_unavailable") from None

    @app.get(PREFIX + "/{attempt_id}", response_model=QuotationAttempt)
    async def get_by_id(request: Request, attempt_id: UUID) -> QuotationAttempt:
        try:
            return await resolve_service(request).get(attempt_id=str(attempt_id))
        except httpx.HTTPError:
            logger.error("quotation_database_unavailable")
            raise HTTPException(503, "quotation_database_unavailable") from None

    @app.get("/internal/quotation-metrics")
    async def metrics(request: Request) -> dict:
        expected = settings.resolved_access_token()
        authorization = request.headers.get("authorization", "")
        if not expected or not authorization.startswith("Bearer ") or not compare_digest(
            authorization[7:].encode(), expected.encode()
        ):
            raise HTTPException(401, "Unauthorized")
        current = resolve_service(request)
        try:
            return await current.database.quotation_rpc("metrics", {"p_account": current.account})
        except httpx.HTTPError:
            logger.error("quotation_database_unavailable")
            raise HTTPException(503, "quotation_database_unavailable") from None
