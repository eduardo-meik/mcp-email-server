import base64

import pytest
from pydantic import ValidationError

from mcp_email_server.adapters.errors import AdapterConfigurationError
from mcp_email_server.adapters.smtp import SmtpAdapter
from mcp_email_server.config import Settings
from mcp_email_server.models import OutboundAttachment, OutboundEmail


class FakeSmtpClient:
    def __init__(self, *, hostname: str, port: int, use_tls: bool) -> None:
        self.hostname = hostname
        self.port = port
        self.use_tls = use_tls
        self.connected = False
        self.started_tls = False
        self.logged_in = False
        self.quit_called = False
        self.sent_message = None
        self.sent_sender = None
        self.sent_recipients = None

    async def connect(self) -> None:
        self.connected = True

    async def starttls(self) -> None:
        self.started_tls = True

    async def login(self, username: str, password: str) -> None:
        assert username == "mailer@test.local"
        assert password == "secret"
        self.logged_in = True

    async def send_message(self, message, *, sender: str, recipients: list[str]) -> dict[str, str]:
        self.sent_message = message
        self.sent_sender = sender
        self.sent_recipients = recipients
        return {}

    async def quit(self) -> None:
        self.quit_called = True


def build_settings(**overrides) -> Settings:
    values = {
        "smtp_host": "mail.test.local",
        "smtp_port": 587,
        "smtp_username": "mailer@test.local",
        "smtp_password": "secret",
        "smtp_from_address": "mailer@test.local",
        "smtp_starttls": True,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_smtp_adapter_sends_email_with_expected_headers() -> None:
    client = FakeSmtpClient(hostname="mail.test.local", port=587, use_tls=False)
    adapter = SmtpAdapter(build_settings(), client_factory=lambda **kwargs: client)

    message_id = await adapter.send_email(
        OutboundEmail(
            to=["alice@example.com"],
            cc=["bob@example.com"],
            bcc=["carol@example.com"],
            subject="Project update",
            text_body="Hello team",
            html_body="<p>Hello team</p>",
            reply_to="manager@example.com",
            in_reply_to="<thread-parent@example.com>",
            references="<thread-root@example.com> <thread-parent@example.com>",
        )
    )

    assert message_id.startswith("<")
    assert client.connected is True
    assert client.started_tls is True
    assert client.logged_in is True
    assert client.quit_called is True
    assert client.sent_sender == "mailer@test.local"
    assert client.sent_recipients == [
        "alice@example.com",
        "bob@example.com",
        "carol@example.com",
        "4452112@bcc.hubspot.com",
    ]
    assert client.sent_message["From"] == "mailer@test.local"
    assert client.sent_message["To"] == "alice@example.com"
    assert client.sent_message["Cc"] == "bob@example.com"
    assert client.sent_message["Reply-To"] == "manager@example.com"
    assert client.sent_message["In-Reply-To"] == "<thread-parent@example.com>"
    assert client.sent_message["References"] == "<thread-root@example.com> <thread-parent@example.com>"
    assert client.sent_message["Subject"] == "Project update"
    assert "Hello team" in client.sent_message.as_string()
    assert "<p>Hello team</p>" in client.sent_message.as_string()


def test_smtp_adapter_adds_required_hubspot_bcc_without_duplicates() -> None:
    adapter = SmtpAdapter(build_settings())

    enriched = adapter.with_required_bcc(
        OutboundEmail(
            to=["alice@example.com"],
            bcc=["4452112@bcc.hubspot.com", "carol@example.com"],
            subject="Project update",
            text_body="Hello team",
        )
    )

    assert enriched.bcc == ["4452112@bcc.hubspot.com", "carol@example.com"]


@pytest.mark.asyncio
async def test_smtp_adapter_adds_attachments_to_mime_message() -> None:
    client = FakeSmtpClient(hostname="mail.test.local", port=587, use_tls=False)
    adapter = SmtpAdapter(build_settings(), client_factory=lambda **kwargs: client)

    await adapter.send_email(
        OutboundEmail(
            to=["alice@example.com"],
            subject="Invoice attached",
            text_body="Please see the invoice.",
            attachments=[
                OutboundAttachment(
                    filename="invoice.pdf",
                    content_type="application/pdf",
                    content_base64=base64.b64encode(b"fake-pdf-bytes").decode("ascii"),
                )
            ],
        )
    )

    attachments = list(client.sent_message.iter_attachments())

    assert len(attachments) == 1
    assert attachments[0].get_filename() == "invoice.pdf"
    assert attachments[0].get_content_type() == "application/pdf"
    assert attachments[0].get_payload(decode=True) == b"fake-pdf-bytes"


@pytest.mark.asyncio
async def test_smtp_adapter_renders_pdf_attachment_from_authored_text() -> None:
    client = FakeSmtpClient(hostname="mail.test.local", port=587, use_tls=False)
    adapter = SmtpAdapter(build_settings(), client_factory=lambda **kwargs: client)

    await adapter.send_email(
        OutboundEmail(
            to=["alice@example.com"],
            subject="Cotizacion adjunta",
            text_body="Adjunto la cotizacion solicitada.",
            attachments=[
                OutboundAttachment(
                    filename="cotizacion.pdf",
                    content_type="application/pdf",
                    content_text="# Cotizacion\n\nTotal: 1.250 USD\nVigencia: 15 dias.",
                )
            ],
        )
    )

    attachments = list(client.sent_message.iter_attachments())

    assert len(attachments) == 1
    assert attachments[0].get_filename() == "cotizacion.pdf"
    assert attachments[0].get_content_type() == "application/pdf"
    assert attachments[0].get_payload(decode=True).startswith(b"%PDF-")


def test_outbound_attachment_infers_content_type_and_accepts_data_urls() -> None:
    text_attachment = OutboundAttachment(filename="notas.txt", content_text="hola")
    data_url_attachment = OutboundAttachment(
        filename="logo.png",
        content_base64="data:image/png;base64,aGVsbG8=",
    )

    assert text_attachment.resolved_content_type() == "text/plain"
    assert text_attachment.as_bytes() == b"hola"
    assert data_url_attachment.content_base64 == "aGVsbG8="
    assert data_url_attachment.resolved_content_type() == "image/png"
    assert data_url_attachment.as_bytes() == b"hello"


def test_outbound_attachment_requires_exactly_one_content_source() -> None:
    with pytest.raises(ValidationError):
        OutboundAttachment(filename="empty.pdf")

    with pytest.raises(ValidationError):
        OutboundAttachment(
            filename="both.pdf",
            content_text="texto",
            content_base64=base64.b64encode(b"bytes").decode("ascii"),
        )


@pytest.mark.asyncio
async def test_smtp_adapter_blocks_when_configuration_is_missing() -> None:
    adapter = SmtpAdapter(build_settings(smtp_host=None))

    with pytest.raises(AdapterConfigurationError) as exc_info:
        await adapter.send_email(
            OutboundEmail(
                to=["alice@example.com"],
                subject="Project update",
                text_body="Hello team",
            )
        )

    assert exc_info.value.missing_configuration == ["MCP_EMAIL_SMTP_HOST"]