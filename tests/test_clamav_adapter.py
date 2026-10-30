import httpx
import pytest

from mcp_email_server.adapters.clamav import ClamAvAdapter, METADATA_IDENTITY_URL
from mcp_email_server.config import Settings


@pytest.mark.asyncio
async def test_clamav_adapter_uses_configured_bearer_token() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer local-token"
        assert request.content == b"file bytes"
        return httpx.Response(200, json={"status": "clean"})

    adapter = ClamAvAdapter(
        Settings(
            _env_file=None,
            clamav_url="https://clamav.example.run.app/",
            clamav_auth_token="local-token",
        ),
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.scan(b"file bytes")

    assert result.status == "clean"


@pytest.mark.asyncio
async def test_clamav_adapter_fetches_cloud_run_oidc_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K_SERVICE", "mcp-email-server")
    audience = "https://clamav.example.run.app"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "metadata.google.internal" and request.url.path in METADATA_IDENTITY_URL:
            assert request.headers["Metadata-Flavor"] == "Google"
            assert request.url.params["audience"] == audience
            return httpx.Response(200, text="cloud-run-token")
        assert request.headers["Authorization"] == "Bearer cloud-run-token"
        return httpx.Response(200, json={"status": "clean"})

    adapter = ClamAvAdapter(
        Settings(_env_file=None, clamav_url=audience),
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.scan(b"file bytes")

    assert result.status == "clean"


@pytest.mark.asyncio
async def test_clamav_adapter_fails_closed_outside_cloud_run_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("K_SERVICE", raising=False)
    adapter = ClamAvAdapter(Settings(_env_file=None, clamav_url="https://clamav.example.run.app"))

    result = await adapter.scan(b"file bytes")

    assert result.status == "unavailable"
