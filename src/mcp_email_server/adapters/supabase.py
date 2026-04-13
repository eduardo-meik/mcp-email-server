import httpx

from mcp_email_server.adapters.errors import AdapterConfigurationError
from mcp_email_server.config import Settings
from mcp_email_server.models import EmbeddedEmail


class SupabaseAdapter:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    async def get_last_uid(self, mailbox_id: str, folder: str) -> int | None:
        missing = self._settings.missing_supabase_config()
        if missing:
            raise AdapterConfigurationError(missing)

        headers = self._build_headers(prefer_merge=False)
        async with self._create_client() as client:
            response = await client.get(
                f"/rest/v1/{self._settings.supabase_sync_state_table}",
                headers=headers,
                params={
                    "select": "last_uid",
                    "mailbox_id": f"eq.{mailbox_id}",
                    "folder": f"eq.{folder}",
                    "limit": 1,
                },
            )
            response.raise_for_status()

        rows = response.json()
        if not rows:
            return None
        return rows[0].get("last_uid")

    async def persist_emails(self, embedded_emails: list[EmbeddedEmail]) -> int:
        missing = self._settings.missing_supabase_config()
        if missing:
            raise AdapterConfigurationError(missing)

        if not embedded_emails:
            return 0

        headers = self._build_headers(prefer_merge=True)
        rows = [item.to_supabase_row() for item in embedded_emails]

        async with self._create_client() as client:
            response = await client.post(
                f"/rest/v1/{self._settings.supabase_messages_table}",
                headers=headers,
                params={"on_conflict": "mailbox_id,folder,uid"},
                json=rows,
            )
            response.raise_for_status()

        return len(rows)

    async def upsert_ingest_state(self, mailbox_id: str, folder: str, last_uid: int) -> None:
        missing = self._settings.missing_supabase_config()
        if missing:
            raise AdapterConfigurationError(missing)

        headers = self._build_headers(prefer_merge=True)
        payload = [{"mailbox_id": mailbox_id, "folder": folder, "last_uid": last_uid}]
        async with self._create_client() as client:
            response = await client.post(
                f"/rest/v1/{self._settings.supabase_sync_state_table}",
                headers=headers,
                params={"on_conflict": "mailbox_id,folder"},
                json=payload,
            )
            response.raise_for_status()

    def _build_headers(self, prefer_merge: bool) -> dict[str, str]:
        token = self._settings.supabase_service_role_key.get_secret_value()
        headers = {
            "apikey": token,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if prefer_merge:
            headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
        return headers

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._settings.resolved_supabase_rest_url(),
            timeout=30.0,
            transport=self._transport,
        )
