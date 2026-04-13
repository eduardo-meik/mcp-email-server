from datetime import datetime
from secrets import compare_digest

from fastapi import FastAPI, Header, HTTPException, status
from fastmcp import FastMCP

from mcp_email_server.adapters import ImapAdapter, OpenRouterAdapter, SmtpAdapter, SupabaseAdapter
from mcp_email_server.config import Settings, get_settings
from mcp_email_server.models import (
    AvailableAccountsResult,
    CurrentDatetimeResult,
    EmailAccount,
    EmailActionResult,
    GetEmailsContentResult,
    GetThreadResult,
    ListEmailsResult,
    ListMailboxesResult,
    OutboundEmail,
    SendEmailResult,
    ServiceHealth,
    SyncRunResult,
)
from mcp_email_server.services import EmailSyncService


def build_service(settings: Settings | None = None, account_name: str | None = None) -> EmailSyncService:
    runtime_settings = (settings or get_settings()).for_account(account_name)
    return EmailSyncService(
        imap_adapter=ImapAdapter(runtime_settings),
        openrouter_adapter=OpenRouterAdapter(runtime_settings),
        smtp_adapter=SmtpAdapter(runtime_settings),
        supabase_adapter=SupabaseAdapter(runtime_settings),
    )


def configured_accounts(settings: Settings) -> list[EmailAccount]:
    accounts: list[EmailAccount] = []
    for account in settings.account_configs():
        accounts.append(
            EmailAccount(
                account_name=account.account_name,
                email_address=account.resolved_email_address(),
                mailbox_id=account.resolved_mailbox_id(),
                default_mailbox=account.imap_mailbox or settings.imap_mailbox,
            )
        )
    return accounts


def build_service_health(settings: Settings, account_name: str | None = None) -> ServiceHealth:
    try:
        if account_name is not None:
            selected_settings = settings.for_account(account_name)
            missing = selected_settings.missing_runtime_config()
        else:
            missing = sorted(
                {
                    f"{account.account_name}:{item}"
                    for account in settings.account_configs()
                    for item in settings.for_account(account.account_name).missing_imap_config()
                }
                | set(settings.missing_openrouter_config())
                | set(settings.missing_supabase_config())
            )
    except ValueError as exc:
        missing = [str(exc)]

    return ServiceHealth(
        app_name=settings.app_name,
        env=settings.env,
        status="ok" if not missing else "degraded",
        missing_configuration=missing,
    )


def verify_poll_webhook_secret(settings: Settings, provided_secret: str | None) -> None:
    if settings._is_missing_secret(settings.poll_webhook_secret):
        return

    expected_secret = settings.poll_webhook_secret.get_secret_value()
    if provided_secret is None or not compare_digest(provided_secret, expected_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook secret.",
        )


def build_mcp_server(settings: Settings | None = None) -> FastMCP:
    runtime_settings = settings or get_settings()
    server = FastMCP(
        name="mcp-email-server",
        instructions="Stateless email operations server backed by Supabase.",
    )

    def resolve_service(account_name: str | None = None) -> EmailSyncService:
        return build_service(runtime_settings, account_name=account_name)

    @server.tool(name="get_system_status", description="Return runtime readiness and missing configuration.")
    async def get_system_status(account_name: str | None = None) -> ServiceHealth:
        return build_service_health(runtime_settings, account_name=account_name)

    @server.tool(name="sync_unread_emails", description="Fetch unread emails, embed them, and persist them to Supabase.")
    async def sync_unread_emails(limit: int = 25, account_name: str | None = None) -> SyncRunResult:
        try:
            service = resolve_service(account_name=account_name)
        except ValueError as exc:
            return SyncRunResult(status="error", details=str(exc), dry_run=True)
        return await service.sync_unread_emails(limit=limit)

    @server.tool(name="send_email", description="Send an outbound email through the configured SMTP server.")
    async def send_email(
        to: list[str],
        subject: str,
        body_text: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        body_html: str | None = None,
        reply_to: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        reply_email_id: str | None = None,
        mailbox: str = "INBOX",
        account_name: str | None = None,
    ) -> SendEmailResult:
        try:
            service = resolve_service(account_name=account_name)
        except ValueError as exc:
            return SendEmailResult(status="error", sent=False, details=str(exc))
        return await service.send_email(
            OutboundEmail(
                to=to,
                cc=cc or [],
                bcc=bcc or [],
                subject=subject,
                text_body=body_text,
                html_body=body_html,
                reply_to=reply_to,
                in_reply_to=in_reply_to,
                references=references,
            ),
            reply_email_id=reply_email_id,
            mailbox=mailbox,
        )

    @server.tool(name="list_available_accounts", description="List the configured email accounts available to the server.")
    async def list_available_accounts() -> AvailableAccountsResult:
        return AvailableAccountsResult(status="ok", accounts=configured_accounts(runtime_settings))

    @server.tool(name="get_current_datetime", description="Return the current UTC date and time.")
    async def get_current_datetime() -> CurrentDatetimeResult:
        return resolve_service().get_current_datetime()

    @server.tool(name="list_mailboxes", description="List mailboxes for the configured email account.")
    async def list_mailboxes(account_name: str | None = None) -> ListMailboxesResult:
        try:
            service = resolve_service(account_name=account_name)
        except ValueError as exc:
            return ListMailboxesResult(status="error", details=str(exc))
        return await service.list_mailboxes(account_name=None)

    @server.tool(name="list_emails_metadata", description="List email metadata with optional mailbox and header filters.")
    async def list_emails_metadata(
        account_name: str | None = None,
        page: int = 1,
        page_size: int = 10,
        since: datetime | None = None,
        before: datetime | None = None,
        subject: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        order: str = "desc",
        mailbox: str = "INBOX",
        seen: bool | None = None,
        flagged: bool | None = None,
        answered: bool | None = None,
    ) -> ListEmailsResult:
        try:
            service = resolve_service(account_name=account_name)
        except ValueError as exc:
            return ListEmailsResult(status="error", details=str(exc), page=page, page_size=page_size)
        return await service.list_emails_metadata(
            account_name=None,
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

    @server.tool(name="get_emails_content", description="Return the text and HTML bodies for specific email ids.")
    async def get_emails_content(
        email_ids: list[str],
        account_name: str | None = None,
        mailbox: str = "INBOX",
    ) -> GetEmailsContentResult:
        try:
            service = resolve_service(account_name=account_name)
        except ValueError as exc:
            return GetEmailsContentResult(status="error", details=str(exc))
        return await service.get_emails_content(email_ids=email_ids, account_name=None, mailbox=mailbox)

    @server.tool(name="search_emails", description="Search emails by full-text content within a mailbox.")
    async def search_emails(
        query: str,
        account_name: str | None = None,
        mailbox: str = "INBOX",
        page_size: int = 20,
    ) -> ListEmailsResult:
        try:
            service = resolve_service(account_name=account_name)
        except ValueError as exc:
            return ListEmailsResult(status="error", details=str(exc), page_size=page_size)
        return await service.search_emails(
            query=query,
            account_name=None,
            mailbox=mailbox,
            page_size=page_size,
        )

    @server.tool(name="get_thread", description="Fetch a full email thread anchored on a Message-ID.")
    async def get_thread(
        message_id: str,
        account_name: str | None = None,
        mailbox: str = "INBOX",
    ) -> GetThreadResult:
        try:
            service = resolve_service(account_name=account_name)
        except ValueError as exc:
            return GetThreadResult(status="error", message_id=message_id, details=str(exc))
        return await service.get_thread(message_id=message_id, account_name=None, mailbox=mailbox)

    @server.tool(name="mark_email", description="Mark emails as seen, flagged, or answered.")
    async def mark_email(
        email_ids: list[str],
        flag: str,
        enable: bool = True,
        account_name: str | None = None,
        mailbox: str = "INBOX",
    ) -> EmailActionResult:
        try:
            service = resolve_service(account_name=account_name)
        except ValueError as exc:
            return EmailActionResult(status="error", details=str(exc))
        return await service.mark_email(
            email_ids=email_ids,
            flag=flag,
            enable=enable,
            account_name=None,
            mailbox=mailbox,
        )

    @server.tool(name="move_email", description="Move emails between IMAP mailboxes.")
    async def move_email(
        email_ids: list[str],
        destination_mailbox: str,
        account_name: str | None = None,
        source_mailbox: str = "INBOX",
    ) -> EmailActionResult:
        try:
            service = resolve_service(account_name=account_name)
        except ValueError as exc:
            return EmailActionResult(status="error", details=str(exc))
        return await service.move_email(
            email_ids=email_ids,
            destination_mailbox=destination_mailbox,
            account_name=None,
            source_mailbox=source_mailbox,
        )

    @server.tool(name="delete_emails", description="Delete emails from the selected mailbox.")
    async def delete_emails(
        email_ids: list[str],
        account_name: str | None = None,
        mailbox: str = "INBOX",
    ) -> EmailActionResult:
        try:
            service = resolve_service(account_name=account_name)
        except ValueError as exc:
            return EmailActionResult(status="error", details=str(exc))
        return await service.delete_emails(email_ids=email_ids, account_name=None, mailbox=mailbox)

    return server


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    mcp_server = build_mcp_server(runtime_settings)
    mcp_http_app = mcp_server.http_app(path="/", stateless_http=True)

    fastapi_app = FastAPI(title=runtime_settings.app_name, lifespan=mcp_http_app.lifespan)

    @fastapi_app.get("/healthz", response_model=ServiceHealth)
    async def healthz(account_name: str | None = None) -> ServiceHealth:
        return build_service_health(runtime_settings, account_name=account_name)

    @fastapi_app.post("/tasks/poll", response_model=SyncRunResult)
    async def poll_unread_emails(
        limit: int | None = None,
        account_name: str | None = None,
        x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
    ) -> SyncRunResult:
        verify_poll_webhook_secret(runtime_settings, x_webhook_secret)
        try:
            service = build_service(runtime_settings, account_name=account_name)
        except ValueError as exc:
            return SyncRunResult(status="error", details=str(exc), dry_run=True)
        return await service.sync_unread_emails(limit=limit or runtime_settings.poll_batch_size)

    fastapi_app.mount("/mcp", mcp_http_app)
    return fastapi_app


application = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(application, host=settings.server_host, port=settings.server_port)

if __name__ == "__main__":
    main()