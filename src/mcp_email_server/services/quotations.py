import asyncio
import logging
import time

import httpx
from fastapi import HTTPException

from mcp_email_server.adapters.smtp import SmtpAdapter
from mcp_email_server.adapters.supabase import SupabaseAdapter
from mcp_email_server.config import Settings
from mcp_email_server.models import OutboundEmail
from mcp_email_server.quotations import QuotationAttempt, QuotationSendRequest, SmtpOutcome

logger = logging.getLogger(__name__)


class QuotationService:
    def __init__(self, settings: Settings, database: SupabaseAdapter, smtp: SmtpAdapter) -> None:
        self.settings = settings
        self.database = database
        self.smtp = smtp

    @property
    def account(self) -> str:
        return self.settings.resolve_account_config(self.settings.quoter_account).account_name

    async def send(
        self, request: QuotationSendRequest, message: OutboundEmail, fingerprint: str
    ) -> QuotationAttempt:
        reservation = await self.database.quotation_rpc("reserve", {
            "p_account": self.account,
            "p_key": request.idempotency_key,
            "p_fingerprint": fingerprint,
            "p_reference": request.quotation_reference,
            "p_version": request.quotation_version,
            "p_sender": self.settings.resolved_smtp_from_address(),
            "p_concurrency": self.settings.quotation_concurrency,
            "p_per_minute": self.settings.quotation_per_minute,
            "p_lease_seconds": self.settings.quotation_timeout_seconds + 90,
            "p_retention_days": self.settings.quotation_retention_days,
        })
        decision = reservation["decision"]
        if decision == "conflict":
            raise HTTPException(409, "idempotency_key_content_mismatch")
        if decision == "limited":
            raise HTTPException(429, "quotation_send_limit", headers={"Retry-After": "60"})
        attempt = QuotationAttempt.model_validate(reservation["attempt"])
        if decision == "existing":
            logger.info("quotation_duplicate_avoided attempt_id=%s", attempt.attempt_id)
            return attempt
        if decision != "reserved":
            raise RuntimeError("Unexpected quotation reservation decision")

        attempt_id = str(attempt.attempt_id)
        claimed = await self.database.quotation_rpc("claim", {
            "p_account": self.account, "p_attempt_id": attempt_id,
        })
        if not claimed["claimed"]:
            return QuotationAttempt.model_validate(claimed["attempt"])
        started = time.monotonic()
        try:
            outcome = await self.smtp.send_tracked_email(
                message, message_id=claimed["attempt"]["message_id"]
            )
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self._finish(
                    attempt_id,
                    SmtpOutcome(state="resultado_desconocido", error_code="request_cancelled"),
                    started,
                ))
            except HTTPException:
                logger.error("quotation_cancel_persistence_failed attempt_id=%s", attempt_id)
            raise
        result = await self._finish(attempt_id, outcome, started)
        logger.info(
            "quotation_finished attempt_id=%s state=%s duration_ms=%s error_code=%s",
            attempt_id, result.state, result.duration_ms, result.error_code,
        )
        return result

    async def _finish(self, attempt_id: str, outcome: SmtpOutcome, started: float) -> QuotationAttempt:
        try:
            row = await self.database.quotation_rpc("finish", {
                "p_account": self.account, "p_attempt_id": attempt_id,
                "p_state": outcome.state, "p_message_id": outcome.message_id,
                "p_smtp_queue_id": outcome.smtp_queue_id,
                "p_error_code": outcome.error_code, "p_accepted_count": outcome.accepted_count,
                "p_refused_count": outcome.refused_count,
                "p_duration_ms": int((time.monotonic() - started) * 1000),
            })
        except httpx.HTTPError as exc:
            logger.error("quotation_result_not_recorded attempt_id=%s", attempt_id)
            raise HTTPException(503, {
                "error_code": "result_not_recorded",
                "attempt_id": attempt_id,
                "instruction": "Query this attempt; do not resend with a new key.",
            }) from exc
        return QuotationAttempt.model_validate(row)

    async def get(self, attempt_id: str | None = None, key: str | None = None) -> QuotationAttempt:
        result = await self.database.quotation_rpc("get", {
            "p_account": self.account, "p_attempt_id": attempt_id, "p_key": key,
        })
        if result["attempt"] is None:
            raise HTTPException(404, "quotation_attempt_not_found")
        return QuotationAttempt.model_validate(result["attempt"])
