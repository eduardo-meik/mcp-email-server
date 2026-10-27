import re
from collections.abc import Sequence
from datetime import datetime, timezone
from math import isfinite
from typing import ClassVar
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mcp_email_server.attachments import (
    DEFAULT_CONTENT_TYPE,
    MAX_ATTACHMENT_BYTES,
    decode_base64,
    guess_content_type,
    normalize_base64,
    text_payload_to_bytes,
)


PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bignore\s+(?:all|any|the|these|previous|prior|above)\s+(?:instructions?|prompts?|rules?|messages?)\b", re.IGNORECASE),
    re.compile(r"\b(?:system prompt|developer message|hidden instructions?|tool call|function call)\b", re.IGNORECASE),
    re.compile(r"\b(?:reveal|show|print|dump|return|expose)\b.{0,40}\b(?:api[- ]?key|token|password|secret|credential|system prompt)\b", re.IGNORECASE),
    re.compile(r"\b(?:browse|open|visit|fetch|call|execute|run)\b.{0,30}\b(?:url|link|browser|tool|shell|command|sql)\b", re.IGNORECASE),
    re.compile(r"\b(?:exfiltrate|steal|leak)\b", re.IGNORECASE),
)
REDACTION_MARKER = "[redacted suspicious instruction]"
MAX_LLM_TEXT_CHARS = 12000


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.replace("\x00", "").strip()
    return cleaned or None


def _clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return value.replace("\x00", "").strip()


def _sanitize_untrusted_content(value: str | None) -> str:
    cleaned = _clean_text(value)
    if not cleaned:
        return ""

    sanitized_lines: list[str] = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(pattern.search(line) for pattern in PROMPT_INJECTION_PATTERNS):
            sanitized_lines.append(REDACTION_MARKER)
            continue
        sanitized_lines.append(line)

    sanitized = "\n".join(sanitized_lines)
    if len(sanitized) > MAX_LLM_TEXT_CHARS:
        sanitized = sanitized[:MAX_LLM_TEXT_CHARS].rstrip()
    return sanitized


def _normalize_address_list(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: Sequence[str] = [value]
    else:
        values = value

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = _clean_text(item).lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(normalized)

    return cleaned


class EmailMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mailbox_id: str
    folder: str = "INBOX"
    uid: int
    message_id: str | None = None
    subject: str = ""
    project_tag: str | None = None
    sender: str = ""
    recipients: list[str] = Field(default_factory=list)
    received_at: datetime | None = None
    text_body: str = ""
    html_body: str | None = None

    @field_validator("mailbox_id", "folder", mode="before")
    @classmethod
    def _normalize_required_text(cls, value: str | None) -> str:
        cleaned = _clean_text(value)
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned

    @field_validator("uid")
    @classmethod
    def _validate_uid(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("uid must be greater than 0")
        return value

    @field_validator("message_id", "project_tag", mode="before")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        return _strip_or_none(value)

    @field_validator("subject", "text_body", mode="before")
    @classmethod
    def _normalize_text(cls, value: str | None) -> str:
        return _sanitize_untrusted_content(value)

    @field_validator("html_body", mode="before")
    @classmethod
    def _normalize_html_body(cls, value: str | None) -> str | None:
        cleaned = _strip_or_none(value)
        if cleaned is None:
            return None
        sanitized = _sanitize_untrusted_content(cleaned)
        return sanitized or None

    @field_validator("sender", mode="before")
    @classmethod
    def _normalize_sender(cls, value: str | None) -> str:
        return _clean_text(value).lower()

    @field_validator("recipients", mode="before")
    @classmethod
    def _normalize_recipients(cls, value: list[str] | None) -> list[str]:
        return _normalize_address_list(value)

    @field_validator("received_at", mode="before")
    @classmethod
    def _normalize_received_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @property
    def to_addr(self) -> str:
        return ", ".join(self.recipients)

    @property
    def date_text(self) -> str | None:
        return self.received_at.isoformat() if self.received_at else None


class EmbeddedEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: EmailMessage
    embedding: list[float] = Field(default_factory=list)
    model: str

    PRIMARY_EMBEDDING_DIMENSIONS: ClassVar[int] = 2048
    BACKUP_EMBEDDING_DIMENSIONS: ClassVar[int] = 384

    @field_validator("model", mode="before")
    @classmethod
    def _normalize_model(cls, value: str | None) -> str:
        cleaned = _clean_text(value)
        if not cleaned:
            raise ValueError("model must not be blank")
        return cleaned

    @field_validator("embedding", mode="before")
    @classmethod
    def _normalize_embedding(cls, value: Sequence[float] | None) -> list[float]:
        if value is None:
            return []

        cleaned: list[float] = []
        for item in value:
            number = float(item)
            if not isfinite(number):
                raise ValueError("embedding contains non-finite values")
            cleaned.append(number)
        return cleaned

    def to_supabase_row(self) -> dict[str, object]:
        return {
            "mailbox_id": self.message.mailbox_id,
            "folder": self.message.folder,
            "uid": self.message.uid,
            "message_id": self.message.message_id,
            "subject": self.message.subject,
            "project_tag": self.message.project_tag,
            "from_addr": self.message.sender,
            "to_addr": self.message.to_addr,
            "date": self.message.date_text,
            "body_text": self.message.text_body,
            "embedding": self._vector_literal(self._fit_embedding(self.embedding, self.PRIMARY_EMBEDDING_DIMENSIONS)),
            "embedding_384_backup": self._vector_literal(
                self._fit_embedding(self.embedding, self.BACKUP_EMBEDDING_DIMENSIONS)
            ),
        }

    @staticmethod
    def _fit_embedding(values: Sequence[float], dimensions: int) -> list[float]:
        fitted = list(values[:dimensions])
        if len(fitted) < dimensions:
            fitted.extend([0.0] * (dimensions - len(fitted)))
        return fitted

    @staticmethod
    def _vector_literal(values: Sequence[float]) -> str:
        return "[" + ",".join(str(value) for value in values) + "]"


class OutboundAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(description="File name shown to the recipient, including the extension when available.")
    content_text: str | None = Field(
        default=None,
        description=(
            "Document text authored directly by the model. Use this for reports, invoices, letters or any "
            "generated document. When content_type is application/pdf the server renders the PDF."
        ),
    )
    content_base64: str | None = Field(
        default=None,
        description=(
            "Base64-encoded raw bytes, only for files whose bytes already exist. Prefer content_text for "
            "generated documents. Data URLs are accepted."
        ),
    )
    content_type: str | None = Field(
        default=None,
        description=(
            "MIME type for the attachment, for example application/pdf or image/png. "
            "When omitted it is inferred from the file extension."
        ),
    )
    disposition: Literal["attachment", "inline"] = Field(
        default="attachment",
        description="Use 'attachment' for normal files or 'inline' for content referenced from the HTML body.",
    )
    content_id: str | None = Field(
        default=None,
        description="Optional content ID for inline attachments referenced from HTML, for example logo123.",
    )

    @field_validator("filename", mode="before")
    @classmethod
    def _normalize_required_attachment_text(cls, value: str | None) -> str:
        cleaned = _clean_text(value)
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned

    @field_validator("content_type", mode="before")
    @classmethod
    def _normalize_optional_content_type(cls, value: str | None) -> str | None:
        return _strip_or_none(value)

    @field_validator("content_base64", mode="before")
    @classmethod
    def _normalize_base64_payload(cls, value: str | None) -> str | None:
        cleaned = _strip_or_none(value)
        if cleaned is None:
            return None
        return normalize_base64(cleaned)

    @field_validator("content_text", mode="before")
    @classmethod
    def _normalize_content_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.replace("\x00", "")
        return cleaned if cleaned.strip() else None

    @field_validator("content_id", mode="before")
    @classmethod
    def _normalize_optional_attachment_text(cls, value: str | None) -> str | None:
        return _strip_or_none(value)

    @model_validator(mode="after")
    def _require_single_content_source(self) -> "OutboundAttachment":
        sources = [source for source in (self.content_base64, self.content_text) if source]
        if len(sources) != 1:
            raise ValueError("provide exactly one of content_text or content_base64")
        return self

    def resolved_content_type(self) -> str:
        return self.content_type or guess_content_type(self.filename) or DEFAULT_CONTENT_TYPE

    def as_bytes(self) -> bytes:
        if self.content_base64:
            payload = decode_base64(self.content_base64)
        else:
            payload = text_payload_to_bytes(self.content_text or "", self.resolved_content_type())

        if len(payload) > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"attachment '{self.filename}' exceeds the {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit"
            )
        return payload


class OutboundEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    subject: str
    text_body: str
    html_body: str | None = None
    attachments: list[OutboundAttachment] = Field(
        default_factory=list,
        description="Optional files to attach to the outbound email.",
    )
    reply_to: str | None = None
    in_reply_to: str | None = None
    references: str | None = None

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def _normalize_address_fields(cls, value: str | Sequence[str] | None) -> list[str]:
        return _normalize_address_list(value)

    @field_validator("subject", "text_body", mode="before")
    @classmethod
    def _normalize_outbound_text(cls, value: str | None) -> str:
        cleaned = _clean_text(value)
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned

    @field_validator("html_body", "reply_to", "in_reply_to", "references", mode="before")
    @classmethod
    def _normalize_optional_outbound_text(cls, value: str | None) -> str | None:
        cleaned = _strip_or_none(value)
        return cleaned.lower() if cleaned and "@" in cleaned and "<" not in cleaned else cleaned

    @property
    def all_recipients(self) -> list[str]:
        return [*self.to, *self.cc, *self.bcc]


class SendEmailResult(BaseModel):
    status: Literal["ok", "blocked", "error"]
    sent: bool = False
    message_id: str | None = None
    accepted_recipients: list[str] = Field(default_factory=list)
    details: str | None = None
    missing_configuration: list[str] = Field(default_factory=list)


class SyncRunResult(BaseModel):
    status: Literal["ok", "blocked", "error"]
    fetched: int = 0
    embedded: int = 0
    persisted: int = 0
    last_uid: int | None = None
    dry_run: bool = False
    details: str | None = None
    missing_configuration: list[str] = Field(default_factory=list)


class ServiceHealth(BaseModel):
    app_name: str
    env: str
    status: Literal["ok", "degraded"]
    missing_configuration: list[str] = Field(default_factory=list)


class ToolResult(BaseModel):
    status: Literal["ok", "blocked", "error"]
    details: str | None = None
    missing_configuration: list[str] = Field(default_factory=list)


class EmailAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_name: str
    email_address: str
    mailbox_id: str
    default_mailbox: str = "INBOX"
    persona_name: str | None = None


class AccountProfile(EmailAccount):
    persona_instructions: str | None = None
    signature: str | None = None


class AvailableAccountsResult(ToolResult):
    accounts: list[EmailAccount] = Field(default_factory=list)


class GetAccountProfileResult(ToolResult):
    profile: AccountProfile | None = None


class CurrentDatetimeResult(ToolResult):
    current_datetime: datetime
    timezone: str = "UTC"


class MailboxInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class ListMailboxesResult(ToolResult):
    mailboxes: list[MailboxInfo] = Field(default_factory=list)


class EmailMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_id: str
    account_name: str = ""
    uid: int
    mailbox: str = "INBOX"
    message_id: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    subject: str = ""
    project_tag: str | None = None
    from_address: str = ""
    to_addresses: list[str] = Field(default_factory=list)
    received_at: datetime | None = None
    seen: bool = False
    flagged: bool = False
    answered: bool = False
    has_attachments: bool = False


class EmailContent(EmailMetadata):
    text_body: str = ""
    html_body: str | None = None

    def as_metadata(self) -> EmailMetadata:
        return EmailMetadata(
            email_id=self.email_id,
            account_name=self.account_name,
            uid=self.uid,
            mailbox=self.mailbox,
            message_id=self.message_id,
            in_reply_to=self.in_reply_to,
            references=self.references,
            subject=self.subject,
            project_tag=self.project_tag,
            from_address=self.from_address,
            to_addresses=self.to_addresses,
            received_at=self.received_at,
            seen=self.seen,
            flagged=self.flagged,
            answered=self.answered,
            has_attachments=self.has_attachments,
        )


class ListEmailsResult(ToolResult):
    emails: list[EmailMetadata] = Field(default_factory=list)
    page: int = 1
    page_size: int = 10
    total: int = 0


class GetEmailsContentResult(ToolResult):
    emails: list[EmailContent] = Field(default_factory=list)


class GetThreadResult(ToolResult):
    message_id: str
    emails: list[EmailContent] = Field(default_factory=list)


class StoredEmailContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mailbox_id: str
    folder: str = "INBOX"
    message_id: str | None = None
    subject: str = ""
    from_address: str = ""
    to_addresses: list[str] = Field(default_factory=list)
    received_at: datetime | None = None
    body_text: str = ""


class GetStoredEmailContextResult(ToolResult):
    anchor_email_id: str | None = None
    normalized_subject: str = ""
    emails: list[StoredEmailContext] = Field(default_factory=list)


class EmailActionResult(ToolResult):
    email_ids: list[str] = Field(default_factory=list)
    count: int = 0