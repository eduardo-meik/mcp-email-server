from datetime import datetime
from secrets import compare_digest
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from pydantic import Field
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_email_server.adapters import ImapAdapter, OpenRouterAdapter, SmtpAdapter, SupabaseAdapter
from mcp_email_server.config import Settings, get_settings
from mcp_email_server.models import (
    AccountProfile,
    AvailableAccountsResult,
    CurrentDatetimeResult,
    EmailAccount,
    EmailActionResult,
    GetAccountProfileResult,
    GetEmailsContentResult,
    GetStoredEmailContextResult,
    GetThreadResult,
    ListEmailsResult,
    ListMailboxesResult,
    OutboundAttachment,
    OutboundEmail,
    SendEmailResult,
    ServiceHealth,
    SyncRunResult,
)
from mcp_email_server.services import EmailSyncService
from mcp_email_server.quotation_api import install_quotation_api


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
                persona_name=account.persona_name,
            )
        )
    return accounts


def build_account_profile(settings: Settings, account_name: str | None = None) -> AccountProfile:
    account = settings.resolve_account_config(account_name)
    return AccountProfile(
        account_name=account.account_name,
        email_address=account.resolved_email_address(),
        mailbox_id=account.resolved_mailbox_id(),
        default_mailbox=account.imap_mailbox or settings.imap_mailbox,
        persona_name=account.persona_name,
        persona_instructions=account.persona_instructions,
        signature=account.signature,
    )


def build_account_profile_result(settings: Settings, account_name: str | None = None) -> GetAccountProfileResult:
    try:
        profile = build_account_profile(settings, account_name=account_name)
    except ValueError as exc:
        return GetAccountProfileResult(status="error", details=str(exc))

    return GetAccountProfileResult(status="ok", profile=profile)


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


def _tokens_match(provided: str, expected: str) -> bool:
    return compare_digest(provided.encode(), expected.encode())


def authorize_mcp_path(path: str, authorization: str | None, expected_token: str | None) -> str | None:
    """Return the path to route to the MCP app, or None when the request is not authorized."""
    if path not in ("/mcp", "/mcp/") or expected_token is None:
        return None
    if authorization and authorization.startswith("Bearer ") and _tokens_match(authorization[7:].strip(), expected_token):
        return "/mcp/" if path == "/mcp" else path

    return None


class McpAccessGuard:
    def __init__(self, app: ASGIApp, expected_token: str | None) -> None:
        self.app = app
        self.expected_token = expected_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/mcp" or path.startswith("/mcp/")):
            await self.app(scope, receive, send)
            return

        routed_path = authorize_mcp_path(path, Headers(scope=scope).get("authorization"), self.expected_token)
        if routed_path is not None:
            await self._forward(scope, receive, send, routed_path)
            return

        if path in ("/mcp", "/mcp/"):
            response = JSONResponse({"detail": "Unauthorized"}, status_code=status.HTTP_401_UNAUTHORIZED)
        else:
            response = JSONResponse({"detail": "Not Found"}, status_code=status.HTTP_404_NOT_FOUND)
        await response(scope, receive, send)

    async def _forward(self, scope: Scope, receive: Receive, send: Send, routed_path: str) -> None:
        await self.app({**scope, "path": routed_path, "raw_path": routed_path.encode()}, receive, send)

def verify_poll_webhook_secret(settings: Settings, provided_secret: str | None) -> None:
    expected_secret = settings.resolved_poll_webhook_secret()
    if expected_secret is None or provided_secret is None or not _tokens_match(provided_secret, expected_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook secret.",
        )


async def sync_configured_accounts(
    settings: Settings,
    limit: int,
    account_name: str | None = None,
) -> SyncRunResult:
    try:
        account_names = [account_name] if account_name else [account.account_name for account in settings.account_configs()]
    except ValueError as exc:
        return SyncRunResult(status="error", details=str(exc), dry_run=True)

    results: list[tuple[str, SyncRunResult]] = []
    for current_account_name in account_names:
        try:
            service = build_service(settings, account_name=current_account_name)
        except ValueError as exc:
            results.append(
                (
                    current_account_name or "default",
                    SyncRunResult(status="error", details=str(exc), dry_run=True),
                )
            )
            continue

        results.append((current_account_name or "default", await service.sync_unread_emails(limit=limit)))

    statuses = [result.status for _, result in results]
    if any(current_status == "error" for current_status in statuses):
        overall_status = "error"
    elif any(current_status == "blocked" for current_status in statuses):
        overall_status = "blocked"
    else:
        overall_status = "ok"

    missing_configuration = [
        f"{current_account_name}:{item}"
        for current_account_name, result in results
        for item in result.missing_configuration
    ]
    details = "; ".join(
        f"{current_account_name}: {result.details}"
        for current_account_name, result in results
        if result.details
    ) or None
    last_uids = [result.last_uid for _, result in results if result.last_uid is not None]

    return SyncRunResult(
        status=overall_status,
        fetched=sum(result.fetched for _, result in results),
        embedded=sum(result.embedded for _, result in results),
        persisted=sum(result.persisted for _, result in results),
        last_uid=max(last_uids) if last_uids else None,
        dry_run=any(result.dry_run for _, result in results),
        details=details,
        missing_configuration=missing_configuration,
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
        return await sync_configured_accounts(runtime_settings, limit=limit, account_name=account_name)

    @server.tool(
        name="send_email",
        description=(
            "Send an outbound email through the configured SMTP server. "
            "Supports file attachments through attachments[]: pass content_text to have the server "
            "build the file, including PDF rendering."
        ),
    )
    async def send_email(
        to: Annotated[list[str], Field(description="Primary recipient email addresses.")],
        subject: Annotated[str, Field(description="Email subject line.")],
        body_text: Annotated[str, Field(description="Plain-text body of the email.")],
        cc: Annotated[list[str] | None, Field(description="Optional CC recipient email addresses.")] = None,
        bcc: Annotated[list[str] | None, Field(description="Optional BCC recipient email addresses.")] = None,
        body_html: Annotated[
            str | None,
            Field(description="Optional HTML version of the email body."),
        ] = None,
        attachments: Annotated[
            list[OutboundAttachment] | None,
            Field(
                description=(
                    "Optional file attachments. For documents you author (reports, invoices, letters, quotes) "
                    "pass content_text together with filename and content_type; when content_type is "
                    "application/pdf the server renders the PDF for you. Use content_base64 only when you "
                    "already hold the encoded bytes of an existing file."
                )
            ),
        ] = None,
        reply_to: Annotated[str | None, Field(description="Optional Reply-To email address.")] = None,
        in_reply_to: Annotated[
            str | None,
            Field(description="Optional Message-ID this email replies to."),
        ] = None,
        references: Annotated[
            str | None,
            Field(description="Optional References header used to preserve thread context."),
        ] = None,
        reply_email_id: Annotated[
            str | None,
            Field(description="Optional internal email_id to anchor the reply against stored context."),
        ] = None,
        mailbox: Annotated[str, Field(description="Mailbox used to resolve reply context. Defaults to INBOX.")] = "INBOX",
        account_name: Annotated[
            str | None,
            Field(description="Optional configured account name when multiple outbound accounts exist."),
        ] = None,
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
                attachments=attachments or [],
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

    @server.tool(name="get_account_profile", description="Return the selected account persona, signature, and response guidance.")
    async def get_account_profile(account_name: str | None = None) -> GetAccountProfileResult:
        return build_account_profile_result(runtime_settings, account_name=account_name)

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

    @server.tool(name="get_stored_email_context", description="Return stored historical email context from Supabase for a reply target or subject.")
    async def get_stored_email_context(
        reply_email_id: str | None = None,
        subject: str | None = None,
        account_name: str | None = None,
        mailbox: str = "INBOX",
        limit: int = 10,
    ) -> GetStoredEmailContextResult:
        try:
            service = resolve_service(account_name=account_name)
        except ValueError as exc:
            return GetStoredEmailContextResult(status="error", anchor_email_id=reply_email_id, details=str(exc))
        return await service.get_stored_email_context(
            reply_email_id=reply_email_id,
            subject=subject,
            account_name=None,
            mailbox=mailbox,
            limit=limit,
        )

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
    if runtime_settings.env.strip().lower() in {"production", "prod"} and runtime_settings.resolved_access_token() is None:
        raise ValueError("MCP_EMAIL_ACCESS_TOKEN is required in production and must not be a placeholder")
    mcp_server = build_mcp_server(runtime_settings)
    mcp_http_app = mcp_server.http_app(path="/", stateless_http=True)

    fastapi_app = FastAPI(title=runtime_settings.app_name, lifespan=mcp_http_app.lifespan)

    fastapi_app.add_middleware(McpAccessGuard, expected_token=runtime_settings.resolved_access_token())
    install_quotation_api(fastapi_app, runtime_settings)

    @fastapi_app.get("/healthz", response_model=ServiceHealth)
    @fastapi_app.get("/healthz/", response_model=ServiceHealth, include_in_schema=False)
    async def healthz(account_name: str | None = None) -> ServiceHealth:
        return build_service_health(runtime_settings, account_name=account_name)

    @fastapi_app.post("/tasks/poll", response_model=SyncRunResult)
    async def poll_unread_emails(
        limit: int | None = None,
        account_name: str | None = None,
        x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
    ) -> SyncRunResult:
        verify_poll_webhook_secret(runtime_settings, x_webhook_secret)
        return await sync_configured_accounts(
            runtime_settings,
            limit=limit or runtime_settings.poll_batch_size,
            account_name=account_name,
        )

    fastapi_app.mount("/mcp", mcp_http_app)
    return fastapi_app


application = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(application, host=settings.server_host, port=settings.server_port, access_log=False)

if __name__ == "__main__":
    main()