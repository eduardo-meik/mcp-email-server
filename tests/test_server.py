from fastapi.testclient import TestClient

from mcp_email_server.server import create_app


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