from datetime import datetime, timezone

import httpx

from mcp_email_server.adapters.errors import AdapterConfigurationError, AdapterExecutionError
from mcp_email_server.adapters.imap import ImapAdapter
from mcp_email_server.adapters.openrouter import OpenRouterAdapter
from mcp_email_server.adapters.smtp import SmtpAdapter
from mcp_email_server.adapters.supabase import SupabaseAdapter
from mcp_email_server.models import (
    AvailableAccountsResult,
    CurrentDatetimeResult,
    EmailActionResult,
    EmailAccount,
    GetEmailsContentResult,
    GetThreadResult,
    ListEmailsResult,
    ListMailboxesResult,
    OutboundEmail,
    SendEmailResult,
    SyncRunResult,
)


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

    async def send_email(
        self,
        message: OutboundEmail,
        reply_email_id: str | None = None,
        mailbox: str | None = None,
    ) -> SendEmailResult:
        try:
            if reply_email_id:
                parent_messages = await self._imap_adapter.get_emails_content(email_ids=[reply_email_id], mailbox=mailbox)
                if not parent_messages:
                    raise AdapterExecutionError(f"Reply target not found: {reply_email_id}")
                parent_message = parent_messages[0]
                message = self._prepare_reply_message(message=message, parent_message=parent_message)
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

    def list_available_accounts(self) -> AvailableAccountsResult:
        missing = self._imap_adapter._settings.missing_imap_config()
        if missing:
            return AvailableAccountsResult(status="blocked", missing_configuration=missing, details="IMAP is not configured")

        account = EmailAccount(
            account_name=self._imap_adapter.account_name,
            email_address=self._imap_adapter.email_address or "",
            mailbox_id=self._imap_adapter.mailbox_id,
            default_mailbox=self._imap_adapter.folder,
        )
        return AvailableAccountsResult(status="ok", accounts=[account])

    def get_current_datetime(self) -> CurrentDatetimeResult:
        return CurrentDatetimeResult(status="ok", current_datetime=datetime.now(timezone.utc), timezone="UTC")

    async def list_mailboxes(self, account_name: str | None = None) -> ListMailboxesResult:
        try:
            self._validate_account_name(account_name)
            mailboxes = await self._imap_adapter.list_mailboxes()
        except AdapterConfigurationError as exc:
            return ListMailboxesResult(
                status="blocked",
                details=str(exc),
                missing_configuration=exc.missing_configuration,
            )
        except AdapterExecutionError as exc:
            return ListMailboxesResult(status="error", details=str(exc))

        return ListMailboxesResult(status="ok", mailboxes=mailboxes)

    async def list_emails_metadata(
        self,
        account_name: str | None = None,
        page: int = 1,
        page_size: int = 10,
        since: datetime | None = None,
        before: datetime | None = None,
        subject: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        order: str = "desc",
        mailbox: str | None = None,
        seen: bool | None = None,
        flagged: bool | None = None,
        answered: bool | None = None,
    ) -> ListEmailsResult:
        try:
            self._validate_account_name(account_name)
            emails, total = await self._imap_adapter.list_emails_metadata(
                page=page,
                page_size=page_size,
                since=since,
                before=before,
                subject=subject,
                from_address=from_address,
                to_address=to_address,
                order=order,
                mailbox=mailbox,
                seen=seen,
                flagged=flagged,
                answered=answered,
            )
        except AdapterConfigurationError as exc:
            return ListEmailsResult(
                status="blocked",
                details=str(exc),
                missing_configuration=exc.missing_configuration,
                page=page,
                page_size=page_size,
            )
        except AdapterExecutionError as exc:
            return ListEmailsResult(status="error", details=str(exc), page=page, page_size=page_size)

        return ListEmailsResult(status="ok", emails=emails, page=page, page_size=page_size, total=total)

    async def get_emails_content(
        self,
        email_ids: list[str],
        account_name: str | None = None,
        mailbox: str | None = None,
    ) -> GetEmailsContentResult:
        try:
            self._validate_account_name(account_name)
            emails = await self._imap_adapter.get_emails_content(email_ids=email_ids, mailbox=mailbox)
        except AdapterConfigurationError as exc:
            return GetEmailsContentResult(
                status="blocked",
                details=str(exc),
                missing_configuration=exc.missing_configuration,
            )
        except AdapterExecutionError as exc:
            return GetEmailsContentResult(status="error", details=str(exc))

        return GetEmailsContentResult(status="ok", emails=emails)

    async def get_thread(
        self,
        message_id: str,
        account_name: str | None = None,
        mailbox: str | None = None,
    ) -> GetThreadResult:
        try:
            self._validate_account_name(account_name)
            emails = await self._imap_adapter.get_thread(message_id=message_id, mailbox=mailbox)
        except AdapterConfigurationError as exc:
            return GetThreadResult(
                status="blocked",
                message_id=message_id,
                details=str(exc),
                missing_configuration=exc.missing_configuration,
            )
        except AdapterExecutionError as exc:
            return GetThreadResult(status="error", message_id=message_id, details=str(exc))

        return GetThreadResult(status="ok", message_id=message_id, emails=emails)

    async def search_emails(
        self,
        query: str,
        account_name: str | None = None,
        mailbox: str | None = None,
        page_size: int = 20,
    ) -> ListEmailsResult:
        try:
            self._validate_account_name(account_name)
            emails, total = await self._imap_adapter.search_emails(query=query, mailbox=mailbox, page_size=page_size)
        except AdapterConfigurationError as exc:
            return ListEmailsResult(
                status="blocked",
                details=str(exc),
                missing_configuration=exc.missing_configuration,
                page_size=page_size,
            )
        except AdapterExecutionError as exc:
            return ListEmailsResult(status="error", details=str(exc), page_size=page_size)

        return ListEmailsResult(status="ok", emails=emails, page=1, page_size=page_size, total=total)

    async def mark_email(
        self,
        email_ids: list[str],
        flag: str,
        enable: bool = True,
        account_name: str | None = None,
        mailbox: str | None = None,
    ) -> EmailActionResult:
        try:
            self._validate_account_name(account_name)
            updated_ids = await self._imap_adapter.mark_email(email_ids=email_ids, flag=flag, enable=enable, mailbox=mailbox)
        except AdapterConfigurationError as exc:
            return EmailActionResult(
                status="blocked",
                details=str(exc),
                missing_configuration=exc.missing_configuration,
            )
        except AdapterExecutionError as exc:
            return EmailActionResult(status="error", details=str(exc))

        return EmailActionResult(status="ok", email_ids=updated_ids, count=len(updated_ids))

    async def move_email(
        self,
        email_ids: list[str],
        destination_mailbox: str,
        account_name: str | None = None,
        source_mailbox: str | None = None,
    ) -> EmailActionResult:
        try:
            self._validate_account_name(account_name)
            moved_ids = await self._imap_adapter.move_email(
                email_ids=email_ids,
                destination_mailbox=destination_mailbox,
                source_mailbox=source_mailbox,
            )
        except AdapterConfigurationError as exc:
            return EmailActionResult(
                status="blocked",
                details=str(exc),
                missing_configuration=exc.missing_configuration,
            )
        except AdapterExecutionError as exc:
            return EmailActionResult(status="error", details=str(exc))

        return EmailActionResult(status="ok", email_ids=moved_ids, count=len(moved_ids))

    async def delete_emails(
        self,
        email_ids: list[str],
        account_name: str | None = None,
        mailbox: str | None = None,
    ) -> EmailActionResult:
        try:
            self._validate_account_name(account_name)
            deleted_ids = await self._imap_adapter.delete_emails(email_ids=email_ids, mailbox=mailbox)
        except AdapterConfigurationError as exc:
            return EmailActionResult(
                status="blocked",
                details=str(exc),
                missing_configuration=exc.missing_configuration,
            )
        except AdapterExecutionError as exc:
            return EmailActionResult(status="error", details=str(exc))

        return EmailActionResult(status="ok", email_ids=deleted_ids, count=len(deleted_ids))

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

    def _validate_account_name(self, account_name: str | None) -> None:
        if account_name is None:
            return

        normalized = account_name.strip().lower()
        allowed = {
            self._imap_adapter.account_name.lower(),
            (self._imap_adapter.email_address or "").lower(),
        }
        if normalized not in allowed:
            raise AdapterExecutionError(f"Unknown account_name: {account_name}")

    @staticmethod
    def _prepare_reply_message(message: OutboundEmail, parent_message) -> OutboundEmail:
        updates: dict[str, str | None] = {}

        if parent_message.message_id and not message.in_reply_to:
            updates["in_reply_to"] = parent_message.message_id

        if not message.references:
            reference_tokens: list[str] = []
            for raw_value in (parent_message.references, parent_message.in_reply_to, parent_message.message_id):
                if not raw_value:
                    continue
                for token in raw_value.split():
                    if token not in reference_tokens:
                        reference_tokens.append(token)
            updates["references"] = " ".join(reference_tokens) or None

        if parent_message.subject and not message.subject.lower().startswith("re:"):
            updates["subject"] = f"Re: {parent_message.subject}"

        return message.model_copy(update=updates)
