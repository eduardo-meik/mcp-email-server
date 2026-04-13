from fastapi import FastAPI
from fastmcp import FastMCP

from mcp_email_server.adapters import ImapAdapter, OpenRouterAdapter, SupabaseAdapter
from mcp_email_server.config import Settings, get_settings
from mcp_email_server.models import ServiceHealth, SyncRunResult
from mcp_email_server.services import EmailSyncService


def build_service(settings: Settings | None = None) -> EmailSyncService:
    runtime_settings = settings or get_settings()
    return EmailSyncService(
        imap_adapter=ImapAdapter(runtime_settings),
        openrouter_adapter=OpenRouterAdapter(runtime_settings),
        supabase_adapter=SupabaseAdapter(runtime_settings),
    )


def build_mcp_server(settings: Settings | None = None) -> FastMCP:
    runtime_settings = settings or get_settings()
    service = build_service(runtime_settings)
    server = FastMCP(
        name="mcp-email-server",
        instructions="Stateless email operations server backed by Supabase.",
    )

    @server.tool(name="get_system_status", description="Return runtime readiness and missing configuration.")
    async def get_system_status() -> ServiceHealth:
        missing = runtime_settings.missing_runtime_config()
        return ServiceHealth(
            app_name=runtime_settings.app_name,
            env=runtime_settings.env,
            status="ok" if not missing else "degraded",
            missing_configuration=missing,
        )

    @server.tool(name="sync_unread_emails", description="Fetch unread emails, embed them, and persist them to Supabase.")
    async def sync_unread_emails(limit: int = 25) -> SyncRunResult:
        return await service.sync_unread_emails(limit=limit)

    return server


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    service = build_service(runtime_settings)
    mcp_server = build_mcp_server(runtime_settings)

    fastapi_app = FastAPI(title=runtime_settings.app_name)

    @fastapi_app.get("/healthz", response_model=ServiceHealth)
    async def healthz() -> ServiceHealth:
        missing = runtime_settings.missing_runtime_config()
        return ServiceHealth(
            app_name=runtime_settings.app_name,
            env=runtime_settings.env,
            status="ok" if not missing else "degraded",
            missing_configuration=missing,
        )

    @fastapi_app.post("/tasks/poll", response_model=SyncRunResult)
    async def poll_unread_emails(limit: int | None = None) -> SyncRunResult:
        return await service.sync_unread_emails(limit=limit or runtime_settings.poll_batch_size)

    fastapi_app.mount("/mcp", mcp_server.http_app(path="/", stateless_http=True))
    return fastapi_app


application = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(application, host=settings.server_host, port=settings.server_port)

if __name__ == "__main__":
    main()