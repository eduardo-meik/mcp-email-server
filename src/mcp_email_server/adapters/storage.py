"""Supabase Storage adapter for the private ``email-attachments`` bucket.

Objects are stored under ``{mailbox_id}/{sha256}.{ext}`` — never under the
original filename — and are only reachable through short-lived signed URLs.
"""

from __future__ import annotations

import httpx

from mcp_email_server.adapters.errors import AdapterConfigurationError, AdapterExecutionError
from mcp_email_server.config import Settings


class StorageAdapter:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    @property
    def bucket(self) -> str:
        return self._settings.attachment_storage_bucket

    def _ensure_configured(self) -> None:
        missing = self._settings.missing_supabase_config()
        if missing:
            raise AdapterConfigurationError(missing)

    async def upload(self, key: str, data: bytes, content_type: str) -> str:
        self._ensure_configured()
        async with self._create_client() as client:
            response = await client.post(
                f"/storage/v1/object/{self.bucket}/{key}",
                headers={
                    **self._auth_headers(),
                    "Content-Type": content_type,
                    "x-upsert": "true",
                },
                content=data,
            )
        self._ensure_success(response, "upload attachment")
        return key

    async def create_signed_url(self, key: str, expires_in_seconds: int) -> str:
        self._ensure_configured()
        async with self._create_client() as client:
            response = await client.post(
                f"/storage/v1/object/sign/{self.bucket}/{key}",
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json={"expiresIn": expires_in_seconds},
            )
        self._ensure_success(response, "sign attachment url")

        payload = response.json()
        signed_path = payload.get("signedURL") or payload.get("signedUrl")
        if not signed_path:
            raise AdapterExecutionError("Supabase did not return a signed URL")
        if signed_path.startswith("http"):
            return signed_path
        return f"{self._base_url()}{signed_path}"

    async def delete(self, key: str) -> None:
        self._ensure_configured()
        async with self._create_client() as client:
            response = await client.delete(
                f"/storage/v1/object/{self.bucket}/{key}",
                headers=self._auth_headers(),
            )
        self._ensure_success(response, "delete attachment")

    def _auth_headers(self) -> dict[str, str]:
        token = self._settings.supabase_service_role_key.get_secret_value()
        return {
            "apikey": token,
            "Authorization": f"Bearer {token}",
        }

    def _base_url(self) -> str:
        base = self._settings.resolved_supabase_rest_url()
        if not base:
            raise AdapterConfigurationError(self._settings.missing_supabase_config())
        return base

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=30.0, transport=self._transport)

    @staticmethod
    def _ensure_success(response: httpx.Response, operation: str) -> None:
        if response.status_code >= 400:
            raise AdapterExecutionError(
                f"Supabase storage {operation} failed with status {response.status_code}: {response.text[:200]}"
            )
