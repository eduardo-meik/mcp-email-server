import asyncio

import pytest
from fastapi.testclient import TestClient

from mcp_email_server.config import Settings
from mcp_email_server.models import SyncRunResult
from mcp_email_server.server import build_account_profile, build_mcp_server, configured_accounts, create_app


def test_mcp_initialize_succeeds_through_fastapi_mount() -> None:
    app = create_app(Settings(_env_file=None, access_token="test-token"))

    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer test-token"},
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


def test_mcp_initialize_succeeds_without_trailing_slash() -> None:
    app = create_app(Settings(_env_file=None, access_token="test-token"))

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer test-token"},
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


def test_healthz_succeeds_with_trailing_slash() -> None:
    app = create_app(Settings(_env_file=None))

    with TestClient(app) as client:
        response = client.get("/healthz/")

    assert response.status_code == 200
    assert response.json()["app_name"] == "mcp-email-server"


INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1.0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
TOOL_CALL_REQUEST = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "list_available_accounts", "arguments": {}},
}
TOOLS_LIST_REQUEST = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}


def _guarded_client() -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, access_token="s3cret-token")))


def test_mcp_handshake_requires_credentials() -> None:
    with _guarded_client() as client:
        initialize = client.post("/mcp", headers=MCP_HEADERS, json=INITIALIZE_REQUEST)
        initialize_slash = client.post("/mcp/", headers=MCP_HEADERS, json=INITIALIZE_REQUEST)
        tools_list = client.post("/mcp", headers=MCP_HEADERS, json=TOOLS_LIST_REQUEST)

    assert [r.status_code for r in (initialize, initialize_slash, tools_list)] == [401] * 3


def test_mcp_tool_calls_require_the_access_token() -> None:
    with _guarded_client() as client:
        bare = client.post("/mcp", headers=MCP_HEADERS, json=TOOL_CALL_REQUEST)
        bare_slash = client.post("/mcp/", headers=MCP_HEADERS, json=TOOL_CALL_REQUEST)
        wrong_bearer = client.post(
            "/mcp",
            headers={**MCP_HEADERS, "Authorization": "Bearer wrong-token"},
            json=TOOL_CALL_REQUEST,
        )
        mixed_batch = client.post("/mcp", headers=MCP_HEADERS, json=[INITIALIZE_REQUEST, TOOL_CALL_REQUEST])
        not_json = client.post("/mcp", headers=MCP_HEADERS, content=b"not json")
        oversized = client.post("/mcp", headers=MCP_HEADERS, content=b" " * 20000 + b'{"method":"initialize"}')
        wrong_path = client.post("/mcp/wrong-token", headers=MCP_HEADERS, json=INITIALIZE_REQUEST)

    assert [r.status_code for r in (bare, bare_slash, wrong_bearer, mixed_batch, not_json, oversized)] == [401] * 6
    assert wrong_path.status_code == 404


def test_mcp_tool_calls_require_bearer_header_and_reject_url_tokens() -> None:
    with _guarded_client() as client:
        by_path = client.post("/mcp/s3cret-token", headers=MCP_HEADERS, json=TOOL_CALL_REQUEST)
        by_path_slash = client.post("/mcp/s3cret-token/", headers=MCP_HEADERS, json=TOOL_CALL_REQUEST)
        by_bearer = client.post(
            "/mcp",
            headers={**MCP_HEADERS, "Authorization": "Bearer s3cret-token"},
            json=TOOL_CALL_REQUEST,
        )

    assert [r.status_code for r in (by_path, by_path_slash, by_bearer)] == [404, 404, 200]
    assert "accounts" in by_bearer.text


def test_guard_leaves_healthz_open_and_never_leaks_the_token() -> None:
    with _guarded_client() as client:
        health = client.get("/healthz")
        rejected = client.post("/mcp", headers=MCP_HEADERS, json=TOOL_CALL_REQUEST)

    assert health.status_code == 200
    assert "s3cret-token" not in health.text
    assert "s3cret-token" not in rejected.text


def test_mcp_server_registers_mvp_mailbox_tools() -> None:
    tool_names = {tool.name for tool in asyncio.run(build_mcp_server().list_tools())}

    assert {
        "get_system_status",
        "sync_unread_emails",
        "send_email",
        "list_available_accounts",
        "get_account_profile",
        "get_current_datetime",
        "list_mailboxes",
        "list_emails_metadata",
        "get_emails_content",
        "search_emails",
        "get_thread",
        "get_stored_email_context",
        "mark_email",
        "move_email",
        "delete_emails",
    }.issubset(tool_names)


def test_send_email_tool_exposes_attachment_schema() -> None:
    send_tool = next(tool for tool in asyncio.run(build_mcp_server().list_tools()) if tool.name == "send_email")

    assert "attachments" in send_tool.description
    assert "attachments" in send_tool.parameters["properties"]
    assert "content_text" in send_tool.parameters["properties"]["attachments"]["description"]
    attachment_items = send_tool.parameters["properties"]["attachments"]["anyOf"][0]["items"]
    assert attachment_items["properties"]["filename"]["type"] == "string"
    assert attachment_items["properties"]["content_base64"]["anyOf"][0]["type"] == "string"
    assert "content_text" in attachment_items["properties"]
    assert "application/pdf" in attachment_items["properties"]["content_text"]["description"]
    assert attachment_items["required"] == ["filename"]


def test_account_listing_and_profile_support_multiple_accounts() -> None:
    settings = Settings(
        _env_file=None,
        imap_host="mail.shared.local",
        smtp_host="mail.shared.local",
        openrouter_api_key="router-secret",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="supabase-secret",
        accounts_json='['
        '{"account_name":"sales","mailbox_id":"sales-box","imap_username":"sales@example.com","imap_password":"sales-pass","smtp_username":"sales@example.com","smtp_password":"sales-pass","smtp_from_address":"sales@example.com","persona_name":"Sales closer"},'
        '{"account_name":"support","mailbox_id":"support-box","imap_username":"support@example.com","imap_password":"support-pass","smtp_username":"support@example.com","smtp_password":"support-pass","smtp_from_address":"support@example.com","persona_name":"Support advisor","persona_instructions":"Be calm and solution-oriented.","signature":"Support Desk"}'
        ']',
    )

    accounts = configured_accounts(settings)
    support_profile = build_account_profile(settings, account_name="support")
    support = settings.for_account("support")

    assert [account.account_name for account in accounts] == ["sales", "support"]
    assert [account.mailbox_id for account in accounts] == ["sales-box", "support-box"]
    assert [account.persona_name for account in accounts] == ["Sales closer", "Support advisor"]
    assert support.imap_username == "support@example.com"
    assert support.resolved_mailbox_id() == "support-box"
    assert support_profile.persona_instructions == "Be calm and solution-oriented."
    assert support_profile.signature == "Support Desk"


def test_tasks_poll_rejects_requests_when_secret_is_not_configured(monkeypatch) -> None:
    class DummyService:
        async def sync_unread_emails(self, limit: int) -> SyncRunResult:
            return SyncRunResult(status="ok", fetched=1, embedded=1, persisted=1, last_uid=limit)

    def fake_build_service(settings: Settings | None = None, account_name: str | None = None) -> DummyService:
        _ = (settings, account_name)
        return DummyService()

    monkeypatch.setattr("mcp_email_server.server.build_service", fake_build_service)

    app = create_app(Settings(_env_file=None, poll_batch_size=7))

    with TestClient(app) as client:
        response = client.post("/tasks/poll")

    assert response.status_code == 401


def test_tasks_poll_rejects_requests_with_missing_or_invalid_secret(monkeypatch) -> None:
    calls = {"count": 0}

    class DummyService:
        async def sync_unread_emails(self, limit: int) -> SyncRunResult:
            return SyncRunResult(status="ok", fetched=1, embedded=1, persisted=1, last_uid=limit)

    def fake_build_service(settings: Settings | None = None, account_name: str | None = None) -> DummyService:
        _ = (settings, account_name)
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
        _ = (settings, account_name)
        return DummyService()

    monkeypatch.setattr("mcp_email_server.server.build_service", fake_build_service)

    app = create_app(Settings(_env_file=None, poll_batch_size=9, poll_webhook_secret="top-secret"))

    with TestClient(app) as client:
        response = client.post("/tasks/poll", headers={"X-Webhook-Secret": "top-secret"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["last_uid"] == 9


def test_tasks_poll_syncs_all_configured_accounts_when_account_name_is_omitted(monkeypatch) -> None:
    calls: list[str | None] = []

    class DummyService:
        def __init__(self, current_account_name: str | None) -> None:
            self.current_account_name = current_account_name

        async def sync_unread_emails(self, limit: int) -> SyncRunResult:
            _ = limit
            calls.append(self.current_account_name)
            if self.current_account_name == "sales":
                return SyncRunResult(status="ok", fetched=1, embedded=1, persisted=1, last_uid=10)
            return SyncRunResult(status="ok", fetched=2, embedded=2, persisted=2, last_uid=20)

    def fake_build_service(_settings: Settings | None = None, account_name: str | None = None) -> DummyService:
        return DummyService(account_name)

    monkeypatch.setattr("mcp_email_server.server.build_service", fake_build_service)

    settings = Settings(
        _env_file=None,
        poll_batch_size=5,
        poll_webhook_secret="poll-secret",
        accounts_json='['
        '{"account_name":"sales","mailbox_id":"sales-box","imap_username":"sales@example.com","imap_password":"sales-pass"},'
        '{"account_name":"support","mailbox_id":"support-box","imap_username":"support@example.com","imap_password":"support-pass"}'
        ']',
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post("/tasks/poll", headers={"X-Webhook-Secret": "poll-secret"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["fetched"] == 3
    assert response.json()["embedded"] == 3
    assert response.json()["persisted"] == 3
    assert response.json()["last_uid"] == 20
    assert calls == ["sales", "support"]


@pytest.mark.parametrize("token", [None, "", "replace-me", "PLACEHOLDER", "changeme"])
def test_production_requires_non_placeholder_token(token: str | None) -> None:
    with pytest.raises(ValueError, match="MCP_EMAIL_ACCESS_TOKEN"):
        create_app(Settings(_env_file=None, env="production", access_token=token))


def test_development_without_token_does_not_authorize_operations() -> None:
    with TestClient(create_app(Settings(_env_file=None))) as client:
        assert client.post("/mcp", headers=MCP_HEADERS, json=TOOL_CALL_REQUEST).status_code == 401