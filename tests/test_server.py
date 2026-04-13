import asyncio

from fastapi.testclient import TestClient

from mcp_email_server.config import Settings
from mcp_email_server.models import SyncRunResult
from mcp_email_server.server import build_mcp_server, build_service, configured_accounts, create_app


def test_mcp_initialize_succeeds_through_fastapi_mount() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0"},
                },
            },
        )

    assert response.status_code == 200
    assert '"jsonrpc":"2.0"' in response.text
    assert '"name":"mcp-email-server"' in response.text


def test_mcp_server_registers_mvp_mailbox_tools() -> None:
    tool_names = {tool.name for tool in asyncio.run(build_mcp_server().list_tools())}

    assert {
        "get_system_status",
        "sync_unread_emails",
        "send_email",
        "list_available_accounts",
        "get_current_datetime",
        "list_mailboxes",
        "list_emails_metadata",
        "get_emails_content",
        "search_emails",
        "get_thread",
        "mark_email",
        "move_email",
        "delete_emails",
    }.issubset(tool_names)


def test_build_service_and_account_listing_support_multiple_accounts() -> None:
    settings = Settings(
        _env_file=None,
        imap_host="mail.shared.local",
        smtp_host="mail.shared.local",
        openrouter_api_key="router-secret",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-secret",
        accounts_json='['
        '{"account_name":"sales","mailbox_id":"sales-box","imap_username":"sales@example.com","imap_password":"sales-pass","smtp_username":"sales@example.com","smtp_password":"sales-pass","smtp_from_address":"sales@example.com"},'
        '{"account_name":"support","mailbox_id":"support-box","imap_username":"support@example.com","imap_password":"support-pass","smtp_username":"support@example.com","smtp_password":"support-pass","smtp_from_address":"support@example.com"}'
        ']',
    )

    accounts = configured_accounts(settings)
    support_service = build_service(settings, account_name="support")

    assert [account.account_name for account in accounts] == ["sales", "support"]
    assert [account.mailbox_id for account in accounts] == ["sales-box", "support-box"]
    assert support_service._imap_adapter.email_address == "support@example.com"
    assert support_service._imap_adapter.mailbox_id == "support-box"


def test_tasks_poll_allows_requests_when_secret_is_not_configured(monkeypatch) -> None:
    class DummyService:
        async def sync_unread_emails(self, limit: int) -> SyncRunResult:
            return SyncRunResult(status="ok", fetched=1, embedded=1, persisted=1, last_uid=limit)

    def fake_build_service(settings: Settings | None = None, account_name: str | None = None) -> DummyService:
        return DummyService()

    monkeypatch.setattr("mcp_email_server.server.build_service", fake_build_service)

    app = create_app(Settings(_env_file=None, poll_batch_size=7))

    with TestClient(app) as client:
        response = client.post("/tasks/poll")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["last_uid"] == 7


def test_tasks_poll_rejects_requests_with_missing_or_invalid_secret(monkeypatch) -> None:
    calls = {"count": 0}

    class DummyService:
        async def sync_unread_emails(self, limit: int) -> SyncRunResult:
            return SyncRunResult(status="ok", fetched=1, embedded=1, persisted=1, last_uid=limit)

    def fake_build_service(settings: Settings | None = None, account_name: str | None = None) -> DummyService:
        calls["count"] += 1
        return DummyService()

    monkeypatch.setattr("mcp_email_server.server.build_service", fake_build_service)

    app = create_app(Settings(_env_file=None, poll_batch_size=7, poll_webhook_secret="top-secret"))

    with TestClient(app) as client:
        missing_response = client.post("/tasks/poll")
        invalid_response = client.post("/tasks/poll", headers={"X-Webhook-Secret": "wrong-secret"})

    assert missing_response.status_code == 401
    assert invalid_response.status_code == 401
    assert calls["count"] == 0


def test_tasks_poll_accepts_requests_with_valid_secret(monkeypatch) -> None:
    class DummyService:
        async def sync_unread_emails(self, limit: int) -> SyncRunResult:
            return SyncRunResult(status="ok", fetched=2, embedded=2, persisted=2, last_uid=limit)

    def fake_build_service(settings: Settings | None = None, account_name: str | None = None) -> DummyService:
        return DummyService()

    monkeypatch.setattr("mcp_email_server.server.build_service", fake_build_service)

    app = create_app(Settings(_env_file=None, poll_batch_size=9, poll_webhook_secret="top-secret"))

    with TestClient(app) as client:
        response = client.post("/tasks/poll", headers={"X-Webhook-Secret": "top-secret"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["last_uid"] == 9