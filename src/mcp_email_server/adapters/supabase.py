import re

import httpx

from mcp_email_server.adapters.errors import AdapterConfigurationError
from mcp_email_server.config import Settings
from mcp_email_server.models import EmbeddedEmail, StoredEmailContext


THREAD_PREFIX_PATTERN = re.compile(r"^(?:(?:re|fw|fwd)\s*:\s*)+", re.IGNORECASE)


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

    async def get_stored_email_context(self, mailbox_id: str, subject: str, limit: int = 10) -> tuple[str, list[StoredEmailContext]]:
        missing = self._settings.missing_supabase_config()
        if missing:
            raise AdapterConfigurationError(missing)

        normalized_subject = self.normalize_thread_subject(subject)
        if not normalized_subject:
            return "", []

        headers = self._build_headers(prefer_merge=False)
        fetch_limit = max(limit * 5, 25)
        async with self._create_client() as client:
            response = await client.get(
                f"/rest/v1/{self._settings.supabase_messages_table}",
                headers=headers,
                params={
                    "select": "mailbox_id,folder,message_id,subject,from_addr,to_addr,date,body_text",
                    "mailbox_id": f"eq.{mailbox_id}",
                    "order": "date.desc.nullslast",
                    "limit": fetch_limit,
                },
            )
            response.raise_for_status()

        emails: list[StoredEmailContext] = []
        for row in response.json():
            row_subject = self.normalize_thread_subject(row.get("subject"))
            if row_subject.casefold() != normalized_subject.casefold():
                continue
            emails.append(
                StoredEmailContext(
                    mailbox_id=row.get("mailbox_id") or mailbox_id,
                    folder=row.get("folder") or "INBOX",
                    message_id=row.get("message_id"),
                    subject=row.get("subject") or "",
                    from_address=row.get("from_addr") or "",
                    to_addresses=self._split_addresses(row.get("to_addr")),
                    received_at=row.get("date"),
                    body_text=row.get("body_text") or "",
                )
            )
            if len(emails) >= limit:
                break

        return normalized_subject, emails

    @staticmethod
    def normalize_thread_subject(subject: str | None) -> str:
        cleaned = (subject or "").strip()
        if not cleaned:
            return ""
        return THREAD_PREFIX_PATTERN.sub("", cleaned).strip()

    @staticmethod
    def _split_addresses(value: str | None) -> list[str]:
        if not value:
            return []
        addresses: list[str] = []
        seen: set[str] = set()
        for item in value.split(","):
            normalized = item.strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            addresses.append(normalized)
        return addresses

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
