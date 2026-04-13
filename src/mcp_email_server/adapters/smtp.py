from collections.abc import Callable
from email.message import EmailMessage as SmtpMessage
from email.utils import formatdate, make_msgid
from typing import Any

import aiosmtplib

from mcp_email_server.adapters.errors import AdapterConfigurationError, AdapterExecutionError
from mcp_email_server.config import Settings
from mcp_email_server.models import OutboundEmail


class SmtpAdapter:
    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory or aiosmtplib.SMTP

    async def send_email(self, message: OutboundEmail) -> str:
        missing = self._settings.missing_smtp_config()
        if missing:
            raise AdapterConfigurationError(missing)

        sender = self._settings.resolved_smtp_from_address()
        if sender is None:
            raise AdapterConfigurationError(["MCP_EMAIL_SMTP_FROM_ADDRESS"])

        smtp_message = self._build_message(sender=sender, message=message)
        client = self._client_factory(
            hostname=self._settings.smtp_host,
            port=self._settings.smtp_port,
            use_tls=self._settings.smtp_use_tls,
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
            except Exception:
                pass

        message_id = smtp_message.get("Message-ID")
        if not message_id:
            raise AdapterExecutionError("SMTP server accepted the email without returning a Message-ID")
        return message_id

    @staticmethod
    def _build_message(sender: str, message: OutboundEmail) -> SmtpMessage:
        smtp_message = SmtpMessage()
        smtp_message["From"] = sender
        smtp_message["To"] = ", ".join(message.to)
        if message.cc:
            smtp_message["Cc"] = ", ".join(message.cc)
        if message.reply_to:
            smtp_message["Reply-To"] = message.reply_to
        smtp_message["Subject"] = message.subject
        smtp_message["Date"] = formatdate(localtime=False)
        smtp_message["Message-ID"] = make_msgid()
        smtp_message.set_content(message.text_body)
        if message.html_body:
            smtp_message.add_alternative(message.html_body, subtype="html")
        return smtp_message