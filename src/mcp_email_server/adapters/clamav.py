"""Client for the standalone ClamAV scanner service (Cloud Run).

The scanner accepts ``POST /scan`` with the raw file bytes and answers
``{"status": "clean"}`` or ``{"status": "infected", "signature": ...}``.
Requests are authenticated with a bearer token (OIDC token in Cloud Run, or a
shared secret behind Render). Fail-closed: any transport or protocol problem
yields ``unavailable`` and the caller must not store the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from mcp_email_server.adapters.errors import AdapterConfigurationError
from mcp_email_server.config import Settings


METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)


@dataclass(frozen=True)
class ScanResult:
    status: str  # "clean" | "infected" | "unavailable"
    signature: str | None = None


class ClamAvAdapter:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    async def scan(self, data: bytes, filename: str = "attachment") -> ScanResult:
        base_url = self._settings.resolved_clamav_url()
        if base_url is None:
            raise AdapterConfigurationError(["MCP_EMAIL_CLAMAV_URL"])

        headers = {"Content-Type": "application/octet-stream", "X-Filename": filename[:128]}
        token = self._settings.resolved_clamav_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(
                timeout=self._settings.clamav_timeout_seconds,
                transport=self._transport,
            ) as client:
                if not token:
                    token = await self._cloud_run_identity_token(client, base_url)
                    if token is None:
                        return ScanResult(status="unavailable")
                    headers["Authorization"] = f"Bearer {token}"
                response = await client.post(f"{base_url}/scan", headers=headers, content=data)
        except (httpx.HTTPError, ValueError):
            return ScanResult(status="unavailable")

        if response.status_code != 200:
            return ScanResult(status="unavailable")

        try:
            payload = response.json()
        except ValueError:
            return ScanResult(status="unavailable")

        status = str(payload.get("status", "")).lower()
        if status == "clean":
            return ScanResult(status="clean")
        if status == "infected":
            signature = payload.get("signature")
            return ScanResult(status="infected", signature=str(signature) if signature else None)
        return ScanResult(status="unavailable")

    async def _cloud_run_identity_token(self, client: httpx.AsyncClient, audience: str) -> str | None:
        if not os.environ.get("K_SERVICE"):
            return None

        response = await client.get(
            METADATA_IDENTITY_URL,
            params={"audience": audience, "format": "full"},
            headers={"Metadata-Flavor": "Google"},
            timeout=min(self._settings.clamav_timeout_seconds, 5.0),
        )
        response.raise_for_status()
        token = response.text.strip()
        return token or None
