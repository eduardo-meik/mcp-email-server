import pytest

from mcp_email_server.adapters.errors import AdapterConfigurationError
from mcp_email_server.adapters.smtp import SmtpAdapter
from mcp_email_server.config import Settings
from mcp_email_server.models import OutboundEmail


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
        )
    )

    assert message_id.startswith("<")
    assert client.connected is True
    assert client.started_tls is True
    assert client.logged_in is True
    assert client.quit_called is True
    assert client.sent_sender == "mailer@test.local"
    assert client.sent_recipients == ["alice@example.com", "bob@example.com", "carol@example.com"]
    assert client.sent_message["From"] == "mailer@test.local"
    assert client.sent_message["To"] == "alice@example.com"
    assert client.sent_message["Cc"] == "bob@example.com"
    assert client.sent_message["Reply-To"] == "manager@example.com"
    assert client.sent_message["Subject"] == "Project update"
    assert "Hello team" in client.sent_message.as_string()
    assert "<p>Hello team</p>" in client.sent_message.as_string()


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