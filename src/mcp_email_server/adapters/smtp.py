import asyncio
import logging
import re
from collections.abc import Callable
from email.message import EmailMessage as SmtpMessage
from email.utils import formatdate, make_msgid
from typing import Any

import aiosmtplib

from mcp_email_server.adapters.errors import AdapterConfigurationError, AdapterExecutionError
from mcp_email_server.config import Settings
from mcp_email_server.models import OutboundEmail
from mcp_email_server.quotations import SmtpOutcome


class SmtpAdapter:
    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory or aiosmtplib.SMTP

    async def send_tracked_email(self, message: OutboundEmail, message_id: str | None = None) -> SmtpOutcome:
        missing = self._settings.missing_smtp_config()
        if missing:
            raise AdapterConfigurationError(missing)
        sender = self._settings.resolved_smtp_from_address()
        if sender is None:
            raise AdapterConfigurationError(["MCP_EMAIL_SMTP_FROM_ADDRESS"])
        message = self.with_required_bcc(message)
        smtp_message = self._build_message(sender, message)
        if message_id is not None:
            smtp_message.replace_header("Message-ID", message_id)
        message_id = str(smtp_message["Message-ID"])
        client = self._client_factory(
            hostname=self._settings.smtp_host,
            port=self._settings.smtp_port,
            use_tls=self._settings.smtp_use_tls,
            start_tls=False,
            timeout=self._settings.smtp_timeout_seconds,
        )
        sending = False
        try:
            async with asyncio.timeout(self._settings.quotation_timeout_seconds):
                await client.connect()
                if not self._settings.smtp_use_tls and self._settings.smtp_starttls:
                    await client.starttls()
                await client.login(
                    self._settings.smtp_username,
                    self._settings.smtp_password.get_secret_value(),
                )
                sending = True
                refused, response = await client.send_message(
                    smtp_message, sender=sender, recipients=message.all_recipients,
                    timeout=self._settings.smtp_timeout_seconds,
                )
                refused_count = len(refused)
                queue_match = re.search(
                    r"\bqueued as ([A-Za-z0-9][A-Za-z0-9._-]{0,127})(?:\s|$)", response, re.IGNORECASE
                )
                return SmtpOutcome(
                    state="aceptado_por_smtp", message_id=message_id,
                    smtp_queue_id=queue_match.group(1) if queue_match else None,
                    accepted_count=len(set(message.all_recipients)) - refused_count,
                    refused_count=refused_count,
                    error_code="smtp_partial_rejection" if refused_count else None,
                )
        except (
            aiosmtplib.SMTPDataError,
            aiosmtplib.SMTPSenderRefused,
            aiosmtplib.SMTPRecipientsRefused,
        ):
            return SmtpOutcome(state="fallido", message_id=message_id, error_code="smtp_rejected")
        except (aiosmtplib.SMTPException, OSError, TimeoutError):
            return SmtpOutcome(
                state="resultado_desconocido" if sending else "fallido",
                message_id=message_id,
                error_code="smtp_outcome_unknown" if sending else "smtp_connection_failed",
            )
        finally:
            # A QUIT failure must never overwrite a successful DATA response.
            client.close()

    async def send_email(self, message: OutboundEmail) -> str:
        missing = self._settings.missing_smtp_config()
        if missing:
            raise AdapterConfigurationError(missing)

        sender = self._settings.resolved_smtp_from_address()
        if sender is None:
            raise AdapterConfigurationError(["MCP_EMAIL_SMTP_FROM_ADDRESS"])

        message = self.with_required_bcc(message)
        smtp_message = self._build_message(sender=sender, message=message)
        client = self._client_factory(
            hostname=self._settings.smtp_host,
            port=self._settings.smtp_port,
            use_tls=self._settings.smtp_use_tls,
            timeout=self._settings.smtp_timeout_seconds,
        )
        try:
            await client.connect()
            if not self._settings.smtp_use_tls and self._settings.smtp_starttls:
                await client.starttls()
            await client.login(
                self._settings.smtp_username,
                self._settings.smtp_password.get_secret_value(),
            )
            await client.send_message(
                smtp_message,
                sender=sender,
                recipients=message.all_recipients,
            )
        except aiosmtplib.SMTPException as exc:
            raise AdapterExecutionError(str(exc)) from exc
        finally:
            try:
                await client.quit()
            except (aiosmtplib.SMTPException, OSError, TimeoutError):
                logging.getLogger(__name__).warning("smtp_quit_failed")

        message_id = smtp_message.get("Message-ID")
        if not message_id:
            raise AdapterExecutionError("SMTP server accepted the email without returning a Message-ID")
        return message_id

    def with_required_bcc(self, message: OutboundEmail) -> OutboundEmail:
        required_bcc = self._settings.required_bcc_recipients()
        if not required_bcc:
            return message

        merged_bcc = list(dict.fromkeys([*message.bcc, *required_bcc]))
        if merged_bcc == message.bcc:
            return message

        return message.model_copy(update={"bcc": merged_bcc})

    @staticmethod
    def _build_message(sender: str, message: OutboundEmail) -> SmtpMessage:
        smtp_message = SmtpMessage()
        smtp_message["From"] = sender
        smtp_message["To"] = ", ".join(message.to)
        if message.cc:
            smtp_message["Cc"] = ", ".join(message.cc)
        if message.reply_to:
            smtp_message["Reply-To"] = message.reply_to
        if message.in_reply_to:
            smtp_message["In-Reply-To"] = message.in_reply_to
        if message.references:
            smtp_message["References"] = message.references
        smtp_message["Subject"] = message.subject
        smtp_message["Date"] = formatdate(localtime=False)
        smtp_message["Message-ID"] = make_msgid()
        smtp_message.set_content(message.text_body)
        if message.html_body:
            smtp_message.add_alternative(message.html_body, subtype="html")
        for attachment in message.attachments:
            resolved_type = attachment.resolved_content_type()
            maintype, separator, subtype = resolved_type.partition("/")
            if not separator or not maintype or not subtype:
                maintype, subtype = "application", "octet-stream"
            try:
                payload = attachment.as_bytes()
            except ValueError as exc:
                raise AdapterExecutionError(f"Invalid attachment '{attachment.filename}': {exc}") from exc
            smtp_message.add_attachment(
                payload,
                maintype=maintype,
                subtype=subtype,
                filename=attachment.filename,
                disposition=attachment.disposition,
                cid=attachment.content_id,
            )
        return smtp_message