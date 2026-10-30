"""AttachmentService: the secure inbound-attachment pipeline.

Layer order (cheap -> expensive, fail-closed at every step):
1. Sender allowlist (Supabase ``attachment_sender_policy``) + aligned
   DKIM/DMARC from the topmost ``Authentication-Results`` header.
2. Declaration checks from BODYSTRUCTURE (count, sizes, type/extension).
3. Download only that part via ``BODY.PEEK[part_id]``; decode the transfer
   encoding; re-check the real size.
4. Magic bytes must match extension and declared type.
5. Structural checks per type (PDF/OOXML/XML).
6. SHA-256 dedupe against previously scanned-clean files.
7. ClamAV scan; only ``clean`` continues. Scanner down -> ``pending_scan``.
8. Upload to the private bucket under ``{mailbox_id}/{sha256}.{ext}``.
9. Optional text extraction in a sandboxed subprocess, cached in Supabase.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from mcp_email_server.adapters.clamav import ClamAvAdapter
from mcp_email_server.adapters.errors import AdapterConfigurationError, AdapterExecutionError
from mcp_email_server.adapters.imap import AttachmentContext, ImapAdapter
from mcp_email_server.adapters.storage import StorageAdapter
from mcp_email_server.adapters.supabase import SupabaseAdapter
from mcp_email_server.attachment_security import (
    check_declarations,
    decode_transfer,
    evaluate_sender_trust,
    parse_authentication_results,
    sanitize_filename,
    validate_bytes,
)
from mcp_email_server.attachment_text import extract_text_sandboxed, wrap_untrusted
from mcp_email_server.config import Settings
from mcp_email_server.models import DownloadAttachmentResult
from mcp_email_server.schemas import PART_ID_PATTERN

EMAIL_ID_PATTERN = re.compile(r"^[^:]+:\d+$")


class AttachmentService:
    def __init__(
        self,
        settings: Settings,
        imap_adapter: ImapAdapter,
        supabase_adapter: SupabaseAdapter,
        storage_adapter: StorageAdapter,
        clamav_adapter: ClamAvAdapter,
    ) -> None:
        self._settings = settings
        self._imap = imap_adapter
        self._supabase = supabase_adapter
        self._storage = storage_adapter
        self._clamav = clamav_adapter

    async def download_attachment(
        self,
        email_id: str,
        part_id: str,
        include_text: bool = False,
        text_offset: int = 0,
        max_chars: int = 6000,
    ) -> DownloadAttachmentResult:
        if not EMAIL_ID_PATTERN.match(email_id or ""):
            return DownloadAttachmentResult(status="error", details=f"invalid email_id: {email_id!r}")
        if not PART_ID_PATTERN.match(part_id or ""):
            return DownloadAttachmentResult(status="error", details=f"invalid part_id: {part_id!r}")

        missing = self._settings.missing_supabase_config()
        if missing:
            return DownloadAttachmentResult(status="blocked", missing_configuration=missing)

        mailbox, uid = ImapAdapter.parse_email_id(email_id, default_mailbox=self._settings.imap_mailbox)
        mailbox_id = self._settings.resolved_mailbox_id()
        max_chars = min(max(max_chars, 1), self._settings.attachment_extract_max_chars)
        text_offset = max(text_offset, 0)

        try:
            return await self._download(
                mailbox_id=mailbox_id,
                mailbox=mailbox,
                uid=uid,
                part_id=part_id,
                include_text=include_text,
                text_offset=text_offset,
                max_chars=max_chars,
            )
        except AdapterConfigurationError as exc:
            return DownloadAttachmentResult(
                status="blocked", details=str(exc), missing_configuration=exc.missing_configuration
            )
        except AdapterExecutionError as exc:
            return DownloadAttachmentResult(status="error", details=str(exc))

    async def _download(
        self,
        mailbox_id: str,
        mailbox: str,
        uid: int,
        part_id: str,
        include_text: bool,
        text_offset: int,
        max_chars: int,
    ) -> DownloadAttachmentResult:
        existing = await self._supabase.get_attachment_row(mailbox_id, mailbox, uid, part_id)
        if existing and existing.get("scan_status") == "clean" and existing.get("storage_path"):
            data = None
            if include_text and existing.get("extracted_text") is None:
                data = await self._load_part_bytes(mailbox, uid, part_id)
            return await self._respond_from_row(existing, include_text, text_offset, max_chars, data=data)
        if existing and existing.get("scan_status") == "rejected":
            return DownloadAttachmentResult(
                status="blocked",
                details="attachment was previously rejected",
                email_id=ImapAdapter.build_email_id(mailbox, uid),
                part_id=part_id,
                scan_status="rejected",
                reject_reason=existing.get("reject_reason"),
            )

        context = await self._imap.fetch_attachment_context(
            ImapAdapter.build_email_id(mailbox, uid), mailbox=mailbox
        )
        declaration = next(
            (item for item in context.attachments if item.metadata.part_id == part_id),
            None,
        )
        if declaration is None:
            return DownloadAttachmentResult(
                status="error",
                details=f"part {part_id} not found in message",
                email_id=context.email_id,
                part_id=part_id,
            )
        metadata = declaration.metadata

        # Layer 1: sender trust. Untrusted senders get metadata only, never bytes.
        trust = await self._evaluate_trust(mailbox_id, context)
        base_kwargs = {
            "email_id": context.email_id,
            "part_id": part_id,
            "filename": metadata.filename,
            "content_type": metadata.content_type,
            "size_bytes": metadata.size_bytes,
            "sender_trust": trust,
        }
        if trust != "trusted":
            return DownloadAttachmentResult(
                status="blocked",
                details="sender is not trusted; only metadata is exposed",
                reject_reason="sender not in allowlist or DKIM/DMARC not aligned",
                **base_kwargs,
            )

        # Layer 2: declaration checks before downloading any bytes.
        declaration_verdicts = dict(
            (item.part_id, reason)
            for item, reason in check_declarations(
                [entry.metadata for entry in context.attachments],
                max_count=self._settings.attachment_max_count,
                max_file_bytes=self._settings.attachment_max_file_bytes,
                max_message_bytes=self._settings.attachment_max_message_bytes,
            )
        )
        if declaration_verdicts.get(part_id):
            return await self._reject(
                mailbox_id, mailbox, uid, context, metadata, trust, declaration_verdicts[part_id] or "rejected"
            )

        # Layer 3: download only this part and re-check the real size.
        raw = await self._imap.fetch_attachment_part_bytes(mailbox=mailbox, uid=uid, part_id=part_id)
        try:
            data = decode_transfer(raw, declaration.encoding)
        except ValueError as exc:
            return await self._reject(mailbox_id, mailbox, uid, context, metadata, trust, str(exc))

        # Layers 4-5: magic bytes + structural checks.
        reject_reason = validate_bytes(
            data,
            filename=metadata.filename or "",
            declared_content_type=metadata.content_type,
            max_file_bytes=self._settings.attachment_max_file_bytes,
        )
        if reject_reason:
            return await self._reject(mailbox_id, mailbox, uid, context, metadata, trust, reject_reason)

        sha256 = hashlib.sha256(data).hexdigest()
        base_kwargs["sha256"] = sha256
        base_kwargs["size_bytes"] = len(data)

        # Layer 6: dedupe — a clean scan of identical bytes is reused.
        duplicate = await self._supabase.find_clean_attachment_by_hash(sha256)
        if duplicate and duplicate.get("storage_path"):
            await self._store_row(
                mailbox_id, mailbox, uid, context, metadata, trust, sha256,
                scan_status="clean",
                storage_path=duplicate["storage_path"],
                extracted_text=duplicate.get("extracted_text"),
                injection_flags=duplicate.get("injection_flags"),
            )
            row = await self._supabase.get_attachment_row(mailbox_id, mailbox, uid, part_id)
            return await self._respond_from_row(row or {}, include_text, text_offset, max_chars, data=data)

        # Layer 7: antivirus. Fail-closed when unavailable.
        try:
            scan = await self._clamav.scan(data, filename=sanitize_filename(metadata.filename) or "attachment")
        except AdapterConfigurationError:
            scan = None
        if scan is None or scan.status == "unavailable":
            return DownloadAttachmentResult(
                status="pending_scan",
                details="antivirus scanner is unavailable; nothing was stored, retry later",
                scan_status="pending_scan",
                **base_kwargs,
            )
        if scan.status == "infected":
            return await self._reject(
                mailbox_id, mailbox, uid, context, metadata, trust,
                f"antivirus detection: {scan.signature or 'unknown'}",
                scan_status="rejected",
                scan_signature=scan.signature,
            )

        # Layer 8: store under a content-derived key, never the original name.
        extension = self._extension_of(metadata.filename)
        storage_path = f"{mailbox_id}/{sha256}{extension}"
        await self._storage.upload(storage_path, data, content_type=metadata.content_type)
        await self._store_row(
            mailbox_id, mailbox, uid, context, metadata, trust, sha256,
            scan_status="clean",
            storage_path=storage_path,
        )

        row = await self._supabase.get_attachment_row(mailbox_id, mailbox, uid, part_id)
        return await self._respond_from_row(
            row or {}, include_text, text_offset, max_chars, data=data
        )

    async def _load_part_bytes(self, mailbox: str, uid: int, part_id: str) -> bytes:
        """Fetch and decode a single part again (used for on-demand extraction)."""

        context = await self._imap.fetch_attachment_context(
            ImapAdapter.build_email_id(mailbox, uid), mailbox=mailbox
        )
        declaration = next(
            (item for item in context.attachments if item.metadata.part_id == part_id),
            None,
        )
        if declaration is None:
            raise AdapterExecutionError(f"part {part_id} not found in message")
        raw = await self._imap.fetch_attachment_part_bytes(mailbox=mailbox, uid=uid, part_id=part_id)
        try:
            return decode_transfer(raw, declaration.encoding)
        except ValueError as exc:
            raise AdapterExecutionError(str(exc)) from exc

    async def _evaluate_trust(self, mailbox_id: str, context: AttachmentContext) -> str:
        policies = await self._supabase.get_sender_policies(mailbox_id)
        auth = parse_authentication_results(
            context.authentication_results,
            expected_authserv_id=self._settings.resolved_authserv_id(),
        )
        return evaluate_sender_trust(context.sender, auth, policies)

    async def _reject(
        self,
        mailbox_id: str,
        mailbox: str,
        uid: int,
        context: AttachmentContext,
        metadata,
        trust: str,
        reason: str,
        scan_status: str = "rejected",
        scan_signature: str | None = None,
    ) -> DownloadAttachmentResult:
        await self._store_row(
            mailbox_id, mailbox, uid, context, metadata, trust, sha256=None,
            scan_status=scan_status,
            scan_signature=scan_signature,
            reject_reason=reason,
            storage_path=None,
        )
        return DownloadAttachmentResult(
            status="blocked",
            details=reason,
            email_id=context.email_id,
            part_id=metadata.part_id,
            filename=metadata.filename,
            content_type=metadata.content_type,
            scan_status=scan_status,
            sender_trust=trust,
            reject_reason=reason,
        )

    async def _store_row(
        self,
        mailbox_id: str,
        mailbox: str,
        uid: int,
        context: AttachmentContext,
        metadata,
        trust: str,
        sha256: str | None,
        scan_status: str,
        storage_path: str | None,
        scan_signature: str | None = None,
        reject_reason: str | None = None,
        extracted_text: str | None = None,
        injection_flags: list[str] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        row = {
            "mailbox_id": mailbox_id,
            "folder": mailbox,
            "uid": uid,
            "part_id": metadata.part_id,
            "sha256": sha256,
            "filename": sanitize_filename(metadata.filename)[:255],
            "content_type_declared": metadata.content_type,
            "content_type_detected": metadata.content_type if scan_status == "clean" else None,
            "size_bytes": metadata.size_bytes,
            "sender": context.sender.replace("\x00", "")[:255],
            "sender_trust": trust,
            "auth_result": (context.authentication_results or "").replace("\x00", "")[:1024],
            "scan_status": scan_status,
            "scan_signature": scan_signature,
            "reject_reason": reject_reason,
            "storage_path": storage_path,
            "extracted_text": (extracted_text or "").replace("\x00", "") or None,
            "injection_flags": injection_flags or [],
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=self._settings.attachment_retention_days)).isoformat(),
        }
        await self._supabase.upsert_attachment_row(row)

    async def _respond_from_row(
        self,
        row: dict,
        include_text: bool,
        text_offset: int,
        max_chars: int,
        data: bytes | None = None,
    ) -> DownloadAttachmentResult:
        storage_path = row.get("storage_path")
        signed_url = None
        expires_at = None
        if storage_path:
            ttl = self._settings.attachment_signed_url_ttl_seconds
            signed_url = await self._storage.create_signed_url(storage_path, ttl)
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

        result = DownloadAttachmentResult(
            status="ok",
            email_id=ImapAdapter.build_email_id(row.get("folder") or "INBOX", int(row.get("uid") or 0)),
            part_id=row.get("part_id"),
            filename=row.get("filename"),
            content_type=row.get("content_type_detected") or row.get("content_type_declared"),
            size_bytes=row.get("size_bytes"),
            sha256=row.get("sha256"),
            scan_status=row.get("scan_status"),
            sender_trust=row.get("sender_trust"),
            signed_url=signed_url,
            signed_url_expires_at=expires_at,
        )

        if include_text:
            await self._attach_text(result, row, data, max_chars, text_offset)
        return result

    async def _attach_text(
        self,
        result: DownloadAttachmentResult,
        row: dict,
        data: bytes | None,
        max_chars: int,
        text_offset: int,
    ) -> None:
        cached = row.get("extracted_text")
        flags = list(row.get("injection_flags") or [])

        if cached is None:
            if data is None:
                result.details = "text extraction requires a fresh download; retry with include_text"
                return
            extracted = extract_text_sandboxed(
                data,
                extension=self._extension_of(row.get("filename")),
                timeout_seconds=self._settings.attachment_extract_timeout_seconds,
                max_chars=self._settings.attachment_extract_max_chars,
            )
            if extracted.error:
                result.details = f"text extraction unavailable: {extracted.error}"
                return
            cached = extracted.text
            flags = extracted.injection_flags
            row_id = row.get("id")
            if row_id is not None:
                try:
                    await self._supabase.update_attachment_extraction(int(row_id), cached, flags)
                except (AdapterConfigurationError, AdapterExecutionError):
                    pass  # caching is best-effort; never fail the download

        page = cached[text_offset : text_offset + max_chars]
        result.text = wrap_untrusted(page, sha256=result.sha256 or "", sender_trust=result.sender_trust or "unknown")
        result.text_offset = text_offset
        end = text_offset + len(page)
        result.next_offset = end if end < len(cached) else None
        result.injection_flags = flags

    @staticmethod
    def _extension_of(filename: str | None) -> str:
        name = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if "." not in name:
            return ""
        return "." + name.rsplit(".", 1)[-1].lower()
