"""Auto-archive (behind flag) and retention purge inside sync_unread_emails."""

import pytest
from aioimaplib.aioimaplib import Response

from mcp_email_server.adapters.errors import AdapterExecutionError
from mcp_email_server.adapters.imap import ImapAdapter
from mcp_email_server.config import Settings
from mcp_email_server.services.sync import EmailSyncService
from test_attachment_service import (
    PDF_CLEAN,
    StubClamAv,
    StubStorage,
    StubSupabase,
    build_message,
)


class FakeSyncImapClient:
    def __init__(self, message: bytes) -> None:
        self._message = message
        self.fetch_calls: list[tuple[str, str]] = []
        self.logged_out = False
        encoded_size = len(message.rsplit(b"\r\n\r\n", 1)[1].split(b"\r\n--att--")[0])
        self._bodystructure = (
            '(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 9 1 NIL NIL NIL NIL)'
            f'("application" "pdf" ("name" "factura.pdf") NIL NIL "base64" {encoded_size} NIL '
            '("attachment" ("filename" "factura.pdf")) NIL NIL)'
            ' "mixed" ("boundary" "att") NIL NIL NIL)'
        )

    async def wait_hello_from_server(self) -> None:
        return None

    async def login(self, username: str, password: str) -> Response:
        return Response("OK", [b"LOGIN completed"])

    async def select(self, mailbox: str) -> Response:
        return Response("OK", [b"1"])

    async def logout(self) -> Response:
        self.logged_out = True
        return Response("OK", [b"LOGOUT completed"])

    async def search(self, *criteria, charset=None) -> Response:
        return Response("OK", [b"1"])

    async def fetch(self, message_set: str, message_parts: str) -> Response:
        self.fetch_calls.append((message_set, message_parts))
        return Response(
            "OK",
            [
                f"{message_set} FETCH (UID 201 BODY[] {{{len(self._message)}}}".encode(),
                self._message,
                b")",
            ],
        )

    async def uid(self, command: str, *args: str) -> Response:
        assert command == "FETCH"
        message_set, message_parts = args
        self.fetch_calls.append((message_set, message_parts))
        if "BODYSTRUCTURE" in message_parts:
            header_bytes = self._message.split(b"\r\n\r\n", 1)[0] + b"\r\n"
            return Response(
                "OK",
                [
                    (
                        f"201 FETCH (UID 201 FLAGS () BODYSTRUCTURE {self._bodystructure} "
                        f"BODY[HEADER] {{{len(header_bytes)}}}"
                    ).encode(),
                    header_bytes,
                    b")",
                ],
            )
        if "BODY.PEEK[2]" in message_parts:
            part = self._message.rsplit(b"\r\n\r\n", 1)[1].split(b"\r\n--att--")[0] + b"\r\n"
            return Response(
                "OK",
                [f"201 FETCH (UID 201 BODY[2] {{{len(part)}}}".encode(), part, b")"],
            )
        raise AssertionError(f"unexpected fetch: {message_parts}")


class SyncStubSupabase(StubSupabase):
    def __init__(self, expired_rows: list[dict] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.expired_rows = expired_rows or []
        self.deleted_row_ids: list[int] = []

    async def get_last_uid(self, mailbox_id: str, folder: str):
        return None

    async def persist_emails(self, embedded_emails):
        return len(embedded_emails)

    async def upsert_ingest_state(self, mailbox_id: str, folder: str, last_uid: int) -> None:
        return None

    async def list_expired_attachments(self, now=None):
        return list(self.expired_rows)

    async def delete_attachment_rows(self, row_ids) -> None:
        self.deleted_row_ids.extend(row_ids)


class FakeOpenRouter:
    async def embed_messages(self, unread_messages):
        return unread_messages


class FakeSmtp:
    pass


def build_sync_service(
    message: bytes,
    auto_archive: bool,
    expired_rows: list[dict] | None = None,
    clamav_status: str = "clean",
):
    settings = Settings(
        imap_host="mail.test.local",
        imap_username="agent@test.local",
        imap_password="secret",
        mailbox_id="agent",
        supabase_url="https://supabase.test",
        supabase_service_role_key="service-key",
        clamav_url="https://clamav.test",
        imap_authserv_id="mail.test.local",
        attachment_auto_archive=auto_archive,
    )
    client = FakeSyncImapClient(message)
    supabase = SyncStubSupabase(expired_rows=expired_rows)
    storage = StubStorage()
    clamav = StubClamAv(status=clamav_status)
    service = EmailSyncService(
        imap_adapter=ImapAdapter(settings, client_factory=lambda **_: client),
        openrouter_adapter=FakeOpenRouter(),
        smtp_adapter=FakeSmtp(),
        supabase_adapter=supabase,
        storage_adapter=storage,
        clamav_adapter=clamav,
    )
    return service, client, supabase, storage, clamav


@pytest.mark.asyncio
async def test_sync_auto_archive_disabled_by_default() -> None:
    service, client, supabase, storage, clamav = build_sync_service(build_message(PDF_CLEAN), auto_archive=False)

    result = await service.sync_unread_emails(limit=10)

    assert result.status == "ok"
    assert result.fetched == 1
    assert result.attachments_archived == 0
    assert clamav.calls == []
    assert storage.uploads == {}


@pytest.mark.asyncio
async def test_sync_auto_archives_allowlisted_sender_attachments() -> None:
    service, client, supabase, storage, clamav = build_sync_service(build_message(PDF_CLEAN), auto_archive=True)

    result = await service.sync_unread_emails(limit=10)

    assert result.status == "ok"
    assert result.attachments_archived == 1
    assert len(clamav.calls) == 1
    assert len(storage.uploads) == 1
    row = next(iter(supabase.rows.values()))
    assert row["scan_status"] == "clean"


@pytest.mark.asyncio
async def test_sync_auto_archive_skips_untrusted_senders() -> None:
    service, client, supabase, storage, clamav = build_sync_service(
        build_message(PDF_CLEAN, sender="random@other.example"),
        auto_archive=True,
    )

    result = await service.sync_unread_emails(limit=10)

    assert result.status == "ok"
    assert result.attachments_archived == 0
    assert clamav.calls == []
    assert storage.uploads == {}


@pytest.mark.asyncio
async def test_sync_purges_expired_attachments() -> None:
    expired = [
        {"id": 7, "storage_path": "agent/aaa.pdf"},
        {"id": 8, "storage_path": None},
    ]
    service, client, supabase, storage, clamav = build_sync_service(
        build_message(PDF_CLEAN),
        auto_archive=False,
        expired_rows=expired,
    )

    result = await service.sync_unread_emails(limit=10)

    assert result.attachments_purged == 2
    assert storage.deleted == ["agent/aaa.pdf"]
    assert sorted(supabase.deleted_row_ids) == [7, 8]


class FailingStorage(StubStorage):
    async def delete(self, key: str) -> None:
        raise AdapterExecutionError("storage down")


@pytest.mark.asyncio
async def test_sync_purge_keeps_row_when_storage_delete_fails() -> None:
    service, client, supabase, storage, clamav = build_sync_service(
        build_message(PDF_CLEAN),
        auto_archive=False,
        expired_rows=[{"id": 9, "storage_path": "agent/bbb.pdf"}, {"id": 10, "storage_path": None}],
    )
    service._storage_adapter = FailingStorage()

    result = await service.sync_unread_emails(limit=10)

    # only the row without a storage object is deleted; the other is retried later
    assert result.attachments_purged == 1
    assert supabase.deleted_row_ids == [10]
