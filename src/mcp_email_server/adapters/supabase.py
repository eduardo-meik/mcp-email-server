import re
import time
from datetime import datetime, timezone

import httpx

from mcp_email_server.adapters.errors import AdapterConfigurationError
from mcp_email_server.attachment_security import SenderPolicy
from mcp_email_server.config import Settings
from mcp_email_server.models import EmbeddedEmail, StoredEmailContext


THREAD_PREFIX_PATTERN = re.compile(r"^(?:(?:re|fw|fwd)\s*:\s*)+", re.IGNORECASE)

# PostgREST parameters are sent as JSON body / eq. filters only; user input is
# never interpolated into or=/and= filter strings.
ATTACHMENT_SELECT_FIELDS = (
    "id,mailbox_id,folder,uid,part_id,sha256,filename,content_type_declared,"
    "content_type_detected,size_bytes,sender,sender_trust,auth_result,"
    "scan_status,scan_signature,reject_reason,storage_path,extracted_text,"
    "injection_flags,created_at,expires_at"
)


class SupabaseAdapter:
    # Process-level cache: adapters are rebuilt per request, so the allowlist
    # cache lives on the class with a short TTL (default 5 minutes).
    _policy_cache: dict[str, tuple[float, list[SenderPolicy]]] = {}

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    async def quotation_rpc(self, operation: str, parameters: dict) -> dict:
        if operation not in {"reserve", "claim", "finish", "get", "metrics", "cleanup"}:
            raise ValueError("Unsupported quotation operation")
        missing = self._settings.missing_supabase_config()
        if missing:
            raise AdapterConfigurationError(missing)
        async with self._create_client() as client:
            response = await client.post(
                f"/rest/v1/rpc/quotation_{operation}",
                headers=self._build_headers(prefer_merge=False),
                json=parameters,
            )
            response.raise_for_status()
            return response.json()

    async def get_sender_policies(self, mailbox_id: str) -> list[SenderPolicy]:
        """Return allow/deny patterns for a mailbox, cached for a few minutes."""

        cached = self._policy_cache.get(mailbox_id)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]

        missing = self._settings.missing_supabase_config()
        if missing:
            raise AdapterConfigurationError(missing)

        headers = self._build_headers(prefer_merge=False)
        async with self._create_client() as client:
            response = await client.get(
                f"/rest/v1/{self._settings.supabase_attachment_policy_table}",
                headers=headers,
                params={
                    "select": "pattern,action",
                    "mailbox_id": f"eq.{mailbox_id}",
                    "order": "created_at.asc",
                },
            )
            response.raise_for_status()

        policies = [
            SenderPolicy(pattern=row.get("pattern") or "", action=row.get("action") or "deny")
            for row in response.json()
            if row.get("pattern")
        ]
        ttl = max(self._settings.attachment_policy_cache_seconds, 0)
        self._policy_cache[mailbox_id] = (now + ttl, policies)
        return policies

    async def get_attachment_row(
        self, mailbox_id: str, folder: str, uid: int, part_id: str
    ) -> dict | None:
        missing = self._settings.missing_supabase_config()
        if missing:
            raise AdapterConfigurationError(missing)

        headers = self._build_headers(prefer_merge=False)
        async with self._create_client() as client:
            response = await client.get(
                f"/rest/v1/{self._settings.supabase_attachments_table}",
                headers=headers,
                params={
                    "select": ATTACHMENT_SELECT_FIELDS,
                    "mailbox_id": f"eq.{mailbox_id}",
                    "folder": f"eq.{folder}",
                    "uid": f"eq.{uid}",
                    "part_id": f"eq.{part_id}",
                    "limit": 1,
                },
            )
            response.raise_for_status()

        rows = response.json()
        return rows[0] if rows else None

    async def find_clean_attachment_by_hash(self, sha256: str) -> dict | None:
        """Dedupe: reuse a previously scanned-clean copy of the same bytes."""

        missing = self._settings.missing_supabase_config()
        if missing:
            raise AdapterConfigurationError(missing)

        headers = self._build_headers(prefer_merge=False)
        async with self._create_client() as client:
            response = await client.get(
                f"/rest/v1/{self._settings.supabase_attachments_table}",
                headers=headers,
                params={
                    "select": ATTACHMENT_SELECT_FIELDS,
                    "sha256": f"eq.{sha256}",
                    "scan_status": "eq.clean",
                    "storage_path": "not.is.null",
                    "limit": 1,
                },
            )
            response.raise_for_status()

        rows = response.json()
        return rows[0] if rows else None

    async def upsert_attachment_row(self, row: dict) -> None:
        missing = self._settings.missing_supabase_config()
        if missing:
            raise AdapterConfigurationError(missing)

        headers = self._build_headers(prefer_merge=True)
        async with self._create_client() as client:
            response = await client.post(
                f"/rest/v1/{self._settings.supabase_attachments_table}",
                headers=headers,
                params={"on_conflict": "mailbox_id,folder,uid,part_id"},
                json=[row],
            )
            response.raise_for_status()

    async def update_attachment_extraction(
        self, row_id: int, extracted_text: str, injection_flags: list[str]
    ) -> None:
        missing = self._settings.missing_supabase_config()
        if missing:
            raise AdapterConfigurationError(missing)

        headers = self._build_headers(prefer_merge=True)
        async with self._create_client() as client:
            response = await client.patch(
                f"/rest/v1/{self._settings.supabase_attachments_table}",
                headers=headers,
                params={"id": f"eq.{row_id}"},
                json={"extracted_text": extracted_text, "injection_flags": injection_flags},
            )
            response.raise_for_status()

    async def list_expired_attachments(self, now: datetime | None = None) -> list[dict]:
        missing = self._settings.missing_supabase_config()
        if missing:
            raise AdapterConfigurationError(missing)

        moment = (now or datetime.now(timezone.utc)).isoformat()
        headers = self._build_headers(prefer_merge=False)
        async with self._create_client() as client:
            response = await client.get(
                f"/rest/v1/{self._settings.supabase_attachments_table}",
                headers=headers,
                params={
                    "select": "id,storage_path",
                    "expires_at": f"lt.{moment}",
                    "limit": 500,
                },
            )
            response.raise_for_status()
        return response.json()

    async def delete_attachment_rows(self, row_ids: list[int]) -> None:
        if not row_ids:
            return
        missing = self._settings.missing_supabase_config()
        if missing:
            raise AdapterConfigurationError(missing)

        headers = self._build_headers(prefer_merge=False)
        async with self._create_client() as client:
            response = await client.delete(
                f"/rest/v1/{self._settings.supabase_attachments_table}",
                headers=headers,
                params={"id": f"in.({','.join(str(row_id) for row_id in row_ids)})"},
            )
            response.raise_for_status()

    @classmethod
    def _reset_policy_cache(cls) -> None:
        cls._policy_cache.clear()

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
