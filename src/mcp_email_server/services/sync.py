import httpx

from mcp_email_server.adapters.errors import AdapterConfigurationError, AdapterExecutionError
from mcp_email_server.adapters.imap import ImapAdapter
from mcp_email_server.adapters.openrouter import OpenRouterAdapter
from mcp_email_server.adapters.smtp import SmtpAdapter
from mcp_email_server.adapters.supabase import SupabaseAdapter
from mcp_email_server.models import OutboundEmail, SendEmailResult, SyncRunResult


class EmailSyncService:
    def __init__(
        self,
        imap_adapter: ImapAdapter,
        openrouter_adapter: OpenRouterAdapter,
        smtp_adapter: SmtpAdapter,
        supabase_adapter: SupabaseAdapter,
    ) -> None:
        self._imap_adapter = imap_adapter
        self._openrouter_adapter = openrouter_adapter
        self._smtp_adapter = smtp_adapter
        self._supabase_adapter = supabase_adapter

    async def send_email(self, message: OutboundEmail) -> SendEmailResult:
        try:
            message_id = await self._smtp_adapter.send_email(message)
        except AdapterConfigurationError as exc:
            return SendEmailResult(
                status="blocked",
                sent=False,
                details=str(exc),
                missing_configuration=exc.missing_configuration,
            )
        except AdapterExecutionError as exc:
            return SendEmailResult(status="error", sent=False, details=str(exc))

        return SendEmailResult(
            status="ok",
            sent=True,
            message_id=message_id,
            accepted_recipients=message.all_recipients,
        )

    async def sync_unread_emails(self, limit: int) -> SyncRunResult:
        try:
            last_uid = await self._supabase_adapter.get_last_uid(
                mailbox_id=self._imap_adapter.mailbox_id,
                folder=self._imap_adapter.folder,
            )
            unread_messages = await self._imap_adapter.fetch_unseen(limit=limit, since_uid=last_uid)
            embedded_messages = await self._openrouter_adapter.embed_messages(unread_messages)
            persisted = await self._supabase_adapter.persist_emails(embedded_messages)
            if unread_messages:
                last_uid = max(message.uid for message in unread_messages)
                await self._supabase_adapter.upsert_ingest_state(
                    mailbox_id=self._imap_adapter.mailbox_id,
                    folder=self._imap_adapter.folder,
                    last_uid=last_uid,
                )
        except AdapterConfigurationError as exc:
            return SyncRunResult(
                status="blocked",
                dry_run=True,
                details=str(exc),
                missing_configuration=exc.missing_configuration,
            )
        except AdapterExecutionError as exc:
            return SyncRunResult(status="error", details=str(exc))
        except httpx.HTTPError as exc:
            return SyncRunResult(status="error", details=str(exc))

        return SyncRunResult(
            status="ok",
            fetched=len(unread_messages),
            embedded=len(embedded_messages),
            persisted=persisted,
            last_uid=last_uid,
            dry_run=False,
        )
