import pytest

from mcp_email_server.models import EmailContent, OutboundEmail
from mcp_email_server.services.sync import EmailSyncService


class FakeImapAdapter:
    account_name = "agent"
    email_address = "agent@test.local"
    mailbox_id = "agent"
    folder = "INBOX"

    def __init__(self) -> None:
        self.requested_email_ids: list[str] = []

    async def get_emails_content(self, email_ids: list[str], mailbox: str | None = None):
        self.requested_email_ids.extend(email_ids)
        return [
            EmailContent(
                email_id=email_ids[0],
                account_name="agent",
                uid=10,
                mailbox=mailbox or "INBOX",
                message_id="<parent@example.com>",
                references="<root@example.com> <parent@example.com>",
                subject="Status update",
                from_address="sender@example.com",
                to_addresses=["agent@test.local"],
                text_body="Parent body",
            )
        ]

    async def get_thread(self, message_id: str, mailbox: str | None = None):
        return [
            EmailContent(
                email_id="INBOX:10",
                account_name="agent",
                uid=10,
                mailbox=mailbox or "INBOX",
                message_id=message_id,
                subject="Thread root",
                from_address="sender@example.com",
                to_addresses=["agent@test.local"],
                text_body="Thread root body",
            )
        ]


class FakeSmtpAdapter:
    def __init__(self) -> None:
        self.sent_message = None

    async def send_email(self, message: OutboundEmail) -> str:
        self.sent_message = message
        return "<sent@example.com>"


class FakeOpenRouterAdapter:
    async def embed_messages(self, unread_messages):
        return unread_messages


class FakeSupabaseAdapter:
    async def get_last_uid(self, mailbox_id: str, folder: str):
        return None

    async def persist_emails(self, embedded_emails):
        return len(embedded_emails)

    async def upsert_ingest_state(self, mailbox_id: str, folder: str, last_uid: int) -> None:
        return None


@pytest.mark.asyncio
async def test_send_email_enriches_thread_headers_when_reply_email_id_is_provided() -> None:
    imap_adapter = FakeImapAdapter()
    smtp_adapter = FakeSmtpAdapter()
    service = EmailSyncService(
        imap_adapter=imap_adapter,
        openrouter_adapter=FakeOpenRouterAdapter(),
        smtp_adapter=smtp_adapter,
        supabase_adapter=FakeSupabaseAdapter(),
    )

    result = await service.send_email(
        OutboundEmail(
            to=["sender@example.com"],
            subject="Quick reply",
            text_body="Acknowledged",
        ),
        reply_email_id="INBOX:10",
        mailbox="INBOX",
    )

    assert result.status == "ok"
    assert imap_adapter.requested_email_ids == ["INBOX:10"]
    assert smtp_adapter.sent_message.subject == "Re: Status update"
    assert smtp_adapter.sent_message.in_reply_to == "<parent@example.com>"
    assert smtp_adapter.sent_message.references == "<root@example.com> <parent@example.com>"


@pytest.mark.asyncio
async def test_get_thread_returns_thread_emails() -> None:
    service = EmailSyncService(
        imap_adapter=FakeImapAdapter(),
        openrouter_adapter=FakeOpenRouterAdapter(),
        smtp_adapter=FakeSmtpAdapter(),
        supabase_adapter=FakeSupabaseAdapter(),
    )

    result = await service.get_thread(message_id="<thread@example.com>")

    assert result.status == "ok"
    assert result.message_id == "<thread@example.com>"
    assert [email.message_id for email in result.emails] == ["<thread@example.com>"]