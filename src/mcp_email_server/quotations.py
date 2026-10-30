"""Server-to-server quotation contract. PDF payloads never enter persistence."""

import base64
import binascii
import hashlib
import json
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from mcp_email_server.config import Settings
from mcp_email_server.models import OutboundAttachment, OutboundEmail

AttemptState = Literal["pendiente", "procesando", "aceptado_por_smtp", "fallido", "resultado_desconocido"]
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
ADDRESS_PATTERN = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class QuotationPdf(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=5, max_length=128)
    content_type: Literal["application/pdf"] = "application/pdf"
    content_base64: SecretStr

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]*\.pdf", value, re.IGNORECASE):
            raise ValueError("filename must be a plain PDF filename")
        return value

    def decode(self, max_bytes: int) -> bytes:
        value = self.content_base64.get_secret_value()
        if len(value) > 4 * ((max_bytes + 2) // 3):
            raise ValueError("PDF exceeds the file size limit")
        try:
            payload = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Invalid PDF Base64") from exc
        if not payload or len(payload) > max_bytes:
            raise ValueError("PDF exceeds the file size limit or is empty")
        if not payload.startswith(b"%PDF-") or b"%%EOF" not in payload[-1024:]:
            raise ValueError("Attachment must contain a complete PDF")
        return payload


class QuotationSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    idempotency_key: str = Field(pattern=IDENTIFIER_PATTERN)
    quotation_reference: str = Field(pattern=IDENTIFIER_PATTERN)
    quotation_version: str = Field(pattern=IDENTIFIER_PATTERN)
    to: list[str] = Field(min_length=1, max_length=100)
    cc: list[str] = Field(default_factory=list, max_length=100)
    bcc: list[str] = Field(default_factory=list, max_length=100)
    subject: str = Field(min_length=1, max_length=998)
    body_text: str = Field(min_length=1, max_length=100_000)
    body_html: str | None = Field(default=None, max_length=100_000)
    attachments: list[QuotationPdf] = Field(min_length=1, max_length=10)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def validate_addresses(cls, values: list[str]) -> list[str]:
        for value in values:
            if len(value) > 254 or not ADDRESS_PATTERN.fullmatch(value):
                raise ValueError("Recipients must be bare email addresses")
        return values

    @field_validator("subject")
    @classmethod
    def safe_header(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Invalid subject header")
        return value

    @model_validator(mode="after")
    def safe_body(self) -> "QuotationSendRequest":
        if not self.body_text.strip() or "\x00" in self.body_text or "\x00" in (self.body_html or ""):
            raise ValueError("Invalid body")
        return self

    def prepare(self, settings: Settings) -> tuple[OutboundEmail, str]:
        if len(self.attachments) > settings.quotation_max_attachments:
            raise ValueError("Too many attachments")
        required_bcc = settings.required_bcc_recipients()
        self.validate_addresses(required_bcc)
        effective_bcc = list(dict.fromkeys([*self.bcc, *required_bcc]))
        if len(self.to) + len(self.cc) + len(effective_bcc) > settings.quotation_max_recipients:
            raise ValueError("Too many recipients (including required BCC)")
        total = 0
        attachment_hashes = []
        outbound_attachments = []
        for attachment in self.attachments:
            payload = attachment.decode(settings.quotation_max_file_bytes)
            total += len(payload)
            if total > settings.quotation_max_total_bytes:
                raise ValueError("Attachments exceed the aggregate size limit")
            attachment_hashes.append({
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
            outbound_attachments.append(OutboundAttachment(
                filename=attachment.filename,
                content_type=attachment.content_type,
                content_base64=base64.b64encode(payload).decode("ascii"),
            ))
        fingerprint_data = self.model_dump(exclude={"attachments", "idempotency_key"}, mode="json")
        fingerprint_data.update({
            "attachments": attachment_hashes,
            "bcc": effective_bcc,
            "sender": settings.resolved_smtp_from_address(),
        })
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        return OutboundEmail(
            to=self.to, cc=self.cc, bcc=effective_bcc, subject=self.subject,
            text_body=self.body_text, html_body=self.body_html, attachments=outbound_attachments,
        ), fingerprint


class SmtpOutcome(BaseModel):
    state: AttemptState
    message_id: str | None = None
    smtp_queue_id: str | None = None
    error_code: str | None = None
    accepted_count: int = 0
    refused_count: int = 0


class QuotationAttempt(BaseModel):
    attempt_id: UUID
    idempotency_key: str
    quotation_reference: str
    quotation_version: str
    state: AttemptState
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    duration_ms: int | None = None
    message_id: str | None = None
    smtp_queue_id: str | None = None
    error_code: str | None = None
    accepted_count: int = 0
    refused_count: int = 0
    duplicate_requests: int = 0
    delivery_confirmed: Literal[False] = False


def quoter_settings(settings: Settings) -> Settings | None:
    if settings.quoter_token is None or settings._is_missing_secret(settings.quoter_token):
        if settings.quoter_token is not None or settings.quoter_account is not None:
            raise ValueError("A non-placeholder MCP_EMAIL_QUOTER_TOKEN and a fixed account are required")
        return None
    token = settings.quoter_token.get_secret_value().strip()
    if len(token) < 32 or token in {settings.resolved_access_token(), settings.resolved_poll_webhook_secret()}:
        raise ValueError("MCP_EMAIL_QUOTER_TOKEN must be distinct and contain at least 32 characters")
    if not settings.quoter_account or not settings.quoter_account.strip():
        raise ValueError("MCP_EMAIL_QUOTER_ACCOUNT is required")
    account = settings.for_account(settings.quoter_account)
    missing = account.missing_smtp_config() + account.missing_supabase_config()
    if missing:
        raise ValueError("Quotation configuration missing: " + ", ".join(missing))
    if not account.smtp_use_tls and not account.smtp_starttls:
        raise ValueError("Quotation SMTP requires TLS")
    sender = account.resolved_smtp_from_address()
    if sender is None or not ADDRESS_PATTERN.fullmatch(sender):
        raise ValueError("Quotation SMTP sender must be a bare email address")
    return account
