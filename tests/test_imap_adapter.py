import pytest

from aioimaplib.aioimaplib import Response

from mcp_email_server.adapters.imap import ImapAdapter
from mcp_email_server.config import Settings


SAMPLE_MESSAGE = (
    b"Subject: [AUDTY-OP] Test subject\r\n"
    b"From: Sender <sender@example.com>\r\n"
    b"To: Alice <alice@example.com>, Bob <bob@example.com>\r\n"
    b"Cc: Carol <carol@example.com>\r\n"
    b"Message-ID: <message-1@example.com>\r\n"
    b"Date: Sat, 12 Apr 2026 10:00:00 +0000\r\n"
    b"Content-Type: multipart/alternative; boundary=abc\r\n"
    b"\r\n"
    b"--abc\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Hello world\r\n"
    b"--abc\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<p>Hello world</p>\r\n"
    b"--abc--\r\n"
)


class FakeImapClient:
    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, str, str]] = []
        self.logged_out = False

    async def wait_hello_from_server(self):
        return None

    async def login(self, username: str, password: str) -> Response:
        assert username == "agent@test.local"
        assert password == "secret"
        return Response("OK", [b"LOGIN completed"])

    async def select(self, mailbox: str) -> Response:
        assert mailbox == "INBOX"
        return Response("OK", [b"1"])

    async def search(self, *criteria, charset=None) -> Response:
        assert criteria == ("UNSEEN",)
        assert charset is None
        return Response("OK", [b"101 102"])

    async def uid(self, command: str, message_set: str, message_parts: str) -> Response:
        self.fetch_calls.append((command, message_set, message_parts))
        return Response(
            "OK",
            [
                f"{message_set} FETCH (UID {message_set} BODY[] {{{len(SAMPLE_MESSAGE)}}}".encode(),
                SAMPLE_MESSAGE,
                b")",
            ],
        )

    async def logout(self) -> Response:
        self.logged_out = True
        return Response("OK", [b"LOGOUT completed"])


class FakeImapClientCombinedFetch(FakeImapClient):
    async def uid(self, command: str, message_set: str, message_parts: str) -> Response:
        self.fetch_calls.append((command, message_set, message_parts))
        return Response(
            "OK",
            [
                f"{message_set} FETCH (UID {message_set} BODY[] {{{len(SAMPLE_MESSAGE)}}}\r\n".encode() + SAMPLE_MESSAGE,
                b")",
                b"Fetch completed (0.014 + 0.000 + 0.013 secs).",
            ],
        )


class FakeImapClientBytearrayBody(FakeImapClient):
    async def uid(self, command: str, message_set: str, message_parts: str) -> Response:
        self.fetch_calls.append((command, message_set, message_parts))
        return Response(
            "OK",
            [
                f"{message_set} FETCH (UID {message_set} BODY[] {{{len(SAMPLE_MESSAGE)}}}".encode(),
                bytearray(SAMPLE_MESSAGE),
                b")",
                b"Fetch completed (0.004 + 0.000 + 0.003 secs).",
            ],
        )


def build_settings() -> Settings:
    return Settings(
        imap_host="mail.test.local",
        imap_port=993,
        imap_username="agent@test.local",
        imap_password="secret",
        mailbox_id="agent",
    )


@pytest.mark.asyncio
async def test_fetch_unseen_returns_parsed_messages() -> None:
    client = FakeImapClient()
    adapter = ImapAdapter(build_settings(), client_factory=lambda **_: client)

    messages = await adapter.fetch_unseen(limit=1)

    assert len(messages) == 1
    assert messages[0].uid == 101
    assert messages[0].mailbox_id == "agent"
    assert messages[0].folder == "INBOX"
    assert messages[0].subject == "[AUDTY-OP] Test subject"
    assert messages[0].project_tag == "AUDTY-OP"
    assert messages[0].sender == "sender@example.com"
    assert messages[0].recipients == ["alice@example.com", "bob@example.com", "carol@example.com"]
    assert messages[0].text_body.strip() == "Hello world"
    assert messages[0].html_body.strip() == "<p>Hello world</p>"
    assert client.fetch_calls == [("FETCH", "101", "BODY.PEEK[]")]
    assert client.logged_out is True


@pytest.mark.asyncio
async def test_fetch_unseen_filters_uids_using_last_uid() -> None:
    client = FakeImapClient()
    adapter = ImapAdapter(build_settings(), client_factory=lambda **_: client)

    messages = await adapter.fetch_unseen(limit=10, since_uid=101)

    assert [message.uid for message in messages] == [102]
    assert client.fetch_calls == [("FETCH", "102", "BODY.PEEK[]")]


@pytest.mark.asyncio
async def test_fetch_unseen_handles_fetch_prefix_and_message_in_same_chunk() -> None:
    client = FakeImapClientCombinedFetch()
    adapter = ImapAdapter(build_settings(), client_factory=lambda **_: client)

    messages = await adapter.fetch_unseen(limit=1)

    assert len(messages) == 1
    assert messages[0].subject == "[AUDTY-OP] Test subject"
    assert messages[0].sender == "sender@example.com"
    assert messages[0].received_at is not None
    assert messages[0].text_body.strip() == "Hello world"


@pytest.mark.asyncio
async def test_fetch_unseen_handles_message_body_as_bytearray() -> None:
    client = FakeImapClientBytearrayBody()
    adapter = ImapAdapter(build_settings(), client_factory=lambda **_: client)

    messages = await adapter.fetch_unseen(limit=1)

    assert len(messages) == 1
    assert messages[0].subject == "[AUDTY-OP] Test subject"
    assert messages[0].sender == "sender@example.com"
    assert messages[0].text_body.strip() == "Hello world"


def test_extract_project_tag_only_from_leading_brackets() -> None:
    assert ImapAdapter._extract_project_tag("[AUDTY-OP] Reunion") == "AUDTY-OP"
    assert ImapAdapter._extract_project_tag("Re: [AUDTY-OP] Reunion") == "AUDTY-OP"
    assert ImapAdapter._extract_project_tag("Fwd: RV: [AUDTY-OP] Reunion") == "AUDTY-OP"
    assert ImapAdapter._extract_project_tag("Sin etiqueta") is None
