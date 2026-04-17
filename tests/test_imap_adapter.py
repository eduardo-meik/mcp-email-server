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

SAMPLE_MESSAGE_2 = (
    b"Subject: Invoice reminder\r\n"
    b"From: Billing <billing@example.com>\r\n"
    b"To: Agent <agent@test.local>\r\n"
    b"Message-ID: <message-2@example.com>\r\n"
    b"Date: Sat, 12 Apr 2026 11:00:00 +0000\r\n"
    b"Content-Type: multipart/mixed; boundary=def\r\n"
    b"\r\n"
    b"--def\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Please review invoice 42\r\n"
    b"--def\r\n"
    b"Content-Type: application/pdf\r\n"
    b"Content-Disposition: attachment; filename=invoice.pdf\r\n"
    b"\r\n"
    b"%PDF-1.4\r\n"
    b"--def--\r\n"
)

SAMPLE_MESSAGE_3 = (
    b"Subject: Re: [AUDTY-OP] Test subject\r\n"
    b"From: Teammate <teammate@example.com>\r\n"
    b"To: Sender <sender@example.com>\r\n"
    b"Message-ID: <message-3@example.com>\r\n"
    b"In-Reply-To: <message-1@example.com>\r\n"
    b"References: <message-1@example.com>\r\n"
    b"Date: Sat, 12 Apr 2026 12:00:00 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Following up on the original note\r\n"
)


class FakeImapClient:
    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, str, str]] = []
        self.search_calls: list[tuple[tuple[str, ...], str | None]] = []
        self.store_calls: list[tuple[str, tuple[str, ...]]] = []
        self.move_calls: list[tuple[str, tuple[str, ...]]] = []
        self.copy_calls: list[tuple[str, tuple[str, ...]]] = []
        self.expunge_called = False
        self.logged_out = False
        self.messages = {
            "101": (SAMPLE_MESSAGE, [r"\Seen"]),
            "102": (SAMPLE_MESSAGE_2, [r"\Seen", r"\Flagged", r"\Answered"]),
            "103": (SAMPLE_MESSAGE_3, [r"\Seen"]),
        }

    async def wait_hello_from_server(self):
        return None

    async def login(self, username: str, password: str) -> Response:
        assert username == "agent@test.local"
        assert password == "secret"
        return Response("OK", [b"LOGIN completed"])

    async def select(self, mailbox: str) -> Response:
        assert mailbox == "INBOX"
        return Response("OK", [b"1"])

    async def list(self) -> Response:
        return Response(
            "OK",
            [
                b'(\\HasNoChildren) "/" "INBOX"',
                b'(\\HasNoChildren) "/" "Archive"',
            ],
        )

    async def search(self, *criteria, charset=None) -> Response:
        self.search_calls.append((criteria, charset))
        assert charset is None
        if criteria == ("UNSEEN",):
            return Response("OK", [b"101 102"])
        if "TEXT" in criteria:
            return Response("OK", [b"102"])
        if criteria == ("HEADER", "Message-ID", "<message-1@example.com>"):
            return Response("OK", [b"101"])
        if criteria == ("HEADER", "In-Reply-To", "<message-1@example.com>"):
            return Response("OK", [b"103"])
        if criteria == ("HEADER", "References", "<message-1@example.com>"):
            return Response("OK", [b"103"])
        if criteria == ("HEADER", "Message-ID", "<message-3@example.com>"):
            return Response("OK", [b"103"])
        if criteria == ("HEADER", "In-Reply-To", "<message-3@example.com>"):
            return Response("OK", [b""])
        if criteria == ("HEADER", "References", "<message-3@example.com>"):
            return Response("OK", [b""])
        if criteria == ("HEADER", "Subject", "Invoice"):
            return Response("OK", [b"102"])
        return Response("OK", [b"101 102"])

    async def fetch(self, message_set: str, message_parts: str) -> Response:
        self.fetch_calls.append(("FETCH", message_set, message_parts))
        message_bytes, flags = self.messages[message_set]
        flags_text = " ".join(flags)
        return Response(
            "OK",
            [
                f"{message_set} FETCH (FLAGS ({flags_text}) UID {message_set} BODY[] {{{len(message_bytes)}}}".encode(),
                message_bytes,
                b")",
            ],
        )

    async def uid(self, command: str, *args: str) -> Response:
        if command == "FETCH":
            message_set, message_parts = args
            self.fetch_calls.append(("UID FETCH", message_set, message_parts))
            message_bytes, flags = self.messages[message_set]
            flags_text = " ".join(flags)
            return Response(
                "OK",
                [
                    f"{message_set} FETCH (FLAGS ({flags_text}) UID {message_set} BODY[] {{{len(message_bytes)}}}".encode(),
                    message_bytes,
                    b")",
                ],
            )
        if command == "STORE":
            self.store_calls.append((command, args))
            return Response("OK", [b"STORE completed"])
        if command == "MOVE":
            self.move_calls.append((command, args))
            return Response("OK", [b"MOVE completed"])
        if command == "COPY":
            self.copy_calls.append((command, args))
            return Response("OK", [b"COPY completed"])
        raise AssertionError(f"Unexpected UID command: {command}")

    async def expunge(self) -> Response:
        self.expunge_called = True
        return Response("OK", [b"EXPUNGE completed"])

    async def logout(self) -> Response:
        self.logged_out = True
        return Response("OK", [b"LOGOUT completed"])


class FakeImapClientCombinedFetch(FakeImapClient):
    async def fetch(self, message_set: str, message_parts: str) -> Response:
        self.fetch_calls.append(("FETCH", message_set, message_parts))
        return Response(
            "OK",
            [
                f"{message_set} FETCH (UID {message_set} BODY[] {{{len(SAMPLE_MESSAGE)}}}\r\n".encode() + SAMPLE_MESSAGE,
                b")",
                b"Fetch completed (0.014 + 0.000 + 0.013 secs).",
            ],
        )


class FakeImapClientBytearrayBody(FakeImapClient):
    async def fetch(self, message_set: str, message_parts: str) -> Response:
        self.fetch_calls.append(("FETCH", message_set, message_parts))
        return Response(
            "OK",
            [
                f"{message_set} FETCH (UID {message_set} BODY[] {{{len(SAMPLE_MESSAGE)}}}".encode(),
                bytearray(SAMPLE_MESSAGE),
                b")",
                b"Fetch completed (0.004 + 0.000 + 0.003 secs).",
            ],
        )


class FakeImapClientSequenceSearch(FakeImapClient):
    sequence_map = {
        "1": "101",
        "2": "102",
        "3": "103",
    }

    async def search(self, *criteria, charset=None) -> Response:
        self.search_calls.append((criteria, charset))
        assert charset is None
        if criteria == ("UNSEEN",):
            return Response("OK", [b"1 2"])
        if "TEXT" in criteria:
            return Response("OK", [b"2"])
        if criteria == ("HEADER", "Message-ID", "<message-1@example.com>"):
            return Response("OK", [b"1"])
        if criteria == ("HEADER", "In-Reply-To", "<message-1@example.com>"):
            return Response("OK", [b"3"])
        if criteria == ("HEADER", "References", "<message-1@example.com>"):
            return Response("OK", [b"3"])
        if criteria == ("HEADER", "Subject", "Invoice"):
            return Response("OK", [b"2"])
        return Response("OK", [b"1 2"])

    async def fetch(self, message_set: str, message_parts: str) -> Response:
        actual_uid = self.sequence_map[message_set]
        self.fetch_calls.append(("FETCH", message_set, message_parts))
        message_bytes, flags = self.messages[actual_uid]
        flags_text = " ".join(flags)
        return Response(
            "OK",
            [
                f"{message_set} FETCH (FLAGS ({flags_text}) UID {actual_uid} BODY[] {{{len(message_bytes)}}}".encode(),
                bytearray(message_bytes),
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
    assert client.fetch_calls == [("FETCH", "101", "(UID BODY.PEEK[])")]
    assert client.logged_out is True


@pytest.mark.asyncio
async def test_fetch_unseen_filters_uids_using_last_uid() -> None:
    client = FakeImapClient()
    adapter = ImapAdapter(build_settings(), client_factory=lambda **_: client)

    messages = await adapter.fetch_unseen(limit=10, since_uid=101)

    assert [message.uid for message in messages] == [102]
    assert client.fetch_calls == [
        ("FETCH", "101", "(UID BODY.PEEK[])"),
        ("FETCH", "102", "(UID BODY.PEEK[])"),
    ]


@pytest.mark.asyncio
async def test_list_mailboxes_returns_available_folders() -> None:
    client = FakeImapClient()
    adapter = ImapAdapter(build_settings(), client_factory=lambda **_: client)

    mailboxes = await adapter.list_mailboxes()

    assert [mailbox.name for mailbox in mailboxes] == ["INBOX", "Archive"]


@pytest.mark.asyncio
async def test_list_emails_metadata_returns_paginated_results_with_flags() -> None:
    client = FakeImapClient()
    adapter = ImapAdapter(build_settings(), client_factory=lambda **_: client)

    emails, total = await adapter.list_emails_metadata(page=1, page_size=1, order="desc", subject="Invoice")

    assert total == 1
    assert len(emails) == 1
    assert emails[0].uid == 102
    assert emails[0].email_id == "INBOX:102"
    assert emails[0].mailbox == "INBOX"
    assert emails[0].from_address == "billing@example.com"
    assert emails[0].seen is True
    assert emails[0].flagged is True
    assert emails[0].answered is True
    assert emails[0].has_attachments is True
    assert client.search_calls[-1][0] == ("HEADER", "Subject", "Invoice")


@pytest.mark.asyncio
async def test_get_emails_content_and_search_emails_return_expected_messages() -> None:
    client = FakeImapClient()
    adapter = ImapAdapter(build_settings(), client_factory=lambda **_: client)

    emails = await adapter.get_emails_content(["INBOX:102"])
    search_results, total = await adapter.search_emails(query="invoice")

    assert len(emails) == 1
    assert emails[0].subject == "Invoice reminder"
    assert "invoice 42" in emails[0].text_body
    assert total == 1
    assert [email.uid for email in search_results] == [102]
    assert client.search_calls[-1][0] == ("TEXT", "invoice")


@pytest.mark.asyncio
async def test_get_thread_returns_related_messages_in_order() -> None:
    client = FakeImapClient()
    adapter = ImapAdapter(build_settings(), client_factory=lambda **_: client)

    thread = await adapter.get_thread(message_id="<message-1@example.com>")

    assert [email.uid for email in thread] == [101, 103]
    assert thread[0].message_id == "<message-1@example.com>"
    assert thread[1].in_reply_to == "<message-1@example.com>"
    assert thread[1].references == "<message-1@example.com>"


@pytest.mark.asyncio
async def test_mark_move_and_delete_issue_expected_imap_commands() -> None:
    client = FakeImapClient()
    adapter = ImapAdapter(build_settings(), client_factory=lambda **_: client)

    await adapter.mark_email(["INBOX:101"], flag="seen", enable=False)
    await adapter.move_email(["INBOX:101"], destination_mailbox="Archive")
    await adapter.delete_emails(["INBOX:102"])

    assert client.store_calls[0] == ("STORE", ("101", "-FLAGS.SILENT", r"(\Seen)"))
    assert client.move_calls[0] == ("MOVE", ("101", "Archive"))
    assert client.store_calls[1] == ("STORE", ("102", "+FLAGS.SILENT", r"(\Deleted)"))
    assert client.expunge_called is True


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


@pytest.mark.asyncio
async def test_fetch_unseen_uses_uid_from_fetch_when_search_returns_sequence_numbers() -> None:
    client = FakeImapClientSequenceSearch()
    adapter = ImapAdapter(build_settings(), client_factory=lambda **_: client)

    messages = await adapter.fetch_unseen(limit=10, since_uid=101)

    assert [message.uid for message in messages] == [102]
    assert client.fetch_calls == [
        ("FETCH", "1", "(UID BODY.PEEK[])"),
        ("FETCH", "2", "(UID BODY.PEEK[])"),
    ]


@pytest.mark.asyncio
async def test_list_emails_metadata_handles_sequence_number_search_results() -> None:
    client = FakeImapClientSequenceSearch()
    adapter = ImapAdapter(build_settings(), client_factory=lambda **_: client)

    emails, total = await adapter.list_emails_metadata(page=1, page_size=1, order="desc", subject="Invoice")

    assert total == 1
    assert len(emails) == 1
    assert emails[0].uid == 102
    assert emails[0].email_id == "INBOX:102"
    assert client.fetch_calls == [("FETCH", "2", "(UID FLAGS BODY.PEEK[])")]


def test_extract_project_tag_only_from_leading_brackets() -> None:
    assert ImapAdapter._extract_project_tag("[AUDTY-OP] Reunion") == "AUDTY-OP"
    assert ImapAdapter._extract_project_tag("Re: [AUDTY-OP] Reunion") == "AUDTY-OP"
    assert ImapAdapter._extract_project_tag("Fwd: RV: [AUDTY-OP] Reunion") == "AUDTY-OP"
    assert ImapAdapter._extract_project_tag("Sin etiqueta") is None
