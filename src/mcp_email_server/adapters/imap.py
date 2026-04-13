import asyncio
import re

from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

import aioimaplib

from mcp_email_server.adapters.errors import AdapterConfigurationError, AdapterExecutionError
from mcp_email_server.config import Settings
from mcp_email_server.models import EmailMessage


class ImapAdapter:
    PROJECT_TAG_PATTERN = re.compile(r"^(?:(?:re|fw|fwd|rv)\s*:\s*)*\[(?P<tag>[^\[\]]+)\]\s*", re.IGNORECASE)
    FETCH_PREFIX_PATTERN = re.compile(rb"^\d+\s+FETCH\s+\(", re.IGNORECASE)

    def __init__(self, settings: Settings, client_factory=None) -> None:
        self._settings = settings
        self._client_factory = client_factory or aioimaplib.IMAP4_SSL

    @property
    def mailbox_id(self) -> str:
        return self._settings.resolved_mailbox_id()

    @property
    def folder(self) -> str:
        return self._settings.imap_mailbox

    async def fetch_unseen(self, limit: int, since_uid: int | None = None) -> list[EmailMessage]:
        if limit <= 0:
            return []

        missing = self._settings.missing_imap_config()
        if missing:
            raise AdapterConfigurationError(missing)

        client = self._client_factory(
            host=self._settings.imap_host,
            port=self._settings.imap_port,
        )

        try:
            await client.wait_hello_from_server()
            login_response = await client.login(
                self._settings.imap_username,
                self._settings.imap_password.get_secret_value(),
            )
            self._ensure_ok(login_response.result, "login", login_response.lines)

            select_response = await client.select(self._settings.imap_mailbox)
            self._ensure_ok(select_response.result, f"select {self._settings.imap_mailbox}", select_response.lines)

            search_response = await client.search("UNSEEN", charset=None)
            self._ensure_ok(search_response.result, "search unseen", search_response.lines)

            uids = self._parse_search_uids(search_response.lines)
            if since_uid is not None:
                uids = [uid for uid in uids if uid > since_uid]

            messages: list[EmailMessage] = []
            for uid in uids[:limit]:
                fetch_response = await client.uid("FETCH", str(uid), "BODY.PEEK[]")
                self._ensure_ok(fetch_response.result, f"fetch uid {uid}", fetch_response.lines)
                message_bytes = self._extract_message_bytes(fetch_response.lines)
                messages.append(self._parse_message(uid=uid, message_bytes=message_bytes))

            return messages
        except (aioimaplib.AioImapException, asyncio.TimeoutError, ValueError) as exc:
            raise AdapterExecutionError(f"IMAP fetch failed: {exc}") from exc
        finally:
            try:
                await client.logout()
            except (aioimaplib.AioImapException, asyncio.TimeoutError):
                pass

    @staticmethod
    def _ensure_ok(result: str, operation: str, lines: list[bytes | bytearray] | None = None) -> None:
        if result != "OK":
            details = ""
            if lines:
                decoded = [bytes(line).decode("utf-8", errors="ignore") for line in lines if isinstance(line, (bytes, bytearray))]
                if decoded:
                    details = f": {' | '.join(decoded)}"
            raise AdapterExecutionError(f"IMAP {operation} failed with status {result}{details}")

    @staticmethod
    def _parse_search_uids(lines: list[bytes | bytearray]) -> list[int]:
        joined = b" ".join(bytes(line) for line in lines if isinstance(line, (bytes, bytearray))).decode("utf-8", errors="ignore")
        tokens = [token for token in joined.split() if token.isdigit()]
        return [int(token) for token in tokens]

    @staticmethod
    def _extract_message_bytes(lines: list[bytes | bytearray]) -> bytes:
        message_chunks: list[bytes] = []
        for line in lines:
            if not isinstance(line, (bytes, bytearray)):
                continue

            line_bytes = bytes(line)

            if line_bytes.startswith(b"Fetch completed"):
                continue

            if ImapAdapter.FETCH_PREFIX_PATTERN.match(line_bytes):
                _prefix, separator, remainder = line_bytes.partition(b"\r\n")
                if separator and remainder:
                    message_chunks.append(remainder)
                continue

            if line_bytes in {b")", b")\r\n"}:
                continue

            message_chunks.append(line_bytes)

        if not message_chunks:
            raise ValueError("FETCH response did not contain a message body")

        return b"".join(message_chunks)

    def _parse_message(self, uid: int, message_bytes: bytes) -> EmailMessage:
        parsed = BytesParser(policy=policy.default).parsebytes(message_bytes)
        plain_body, html_body = ImapAdapter._extract_bodies(parsed)
        recipients = [address for _, address in getaddresses(parsed.get_all("to", []) + parsed.get_all("cc", [])) if address]
        subject = self._decode_header_value(parsed.get("subject"))
        received_at = None
        if parsed.get("date"):
            received_at = parsedate_to_datetime(parsed["date"])

        return EmailMessage(
            mailbox_id=self._settings.resolved_mailbox_id(),
            folder=self._settings.imap_mailbox,
            uid=uid,
            message_id=parsed.get("message-id"),
            subject=subject,
            project_tag=self._extract_project_tag(subject),
            sender=getaddresses(parsed.get_all("from", []))[0][1] if parsed.get_all("from", []) else "",
            recipients=recipients,
            received_at=received_at,
            text_body=plain_body,
            html_body=html_body,
        )

    @staticmethod
    def _decode_header_value(value: str | None) -> str:
        if not value:
            return ""
        return str(make_header(decode_header(value))).strip()

    @classmethod
    def _extract_project_tag(cls, subject: str) -> str | None:
        match = cls.PROJECT_TAG_PATTERN.match(subject)
        if not match:
            return None
        tag = match.group("tag").strip()
        return tag or None

    @staticmethod
    def _extract_bodies(parsed_message) -> tuple[str, str | None]:
        if parsed_message.is_multipart():
            plain_part = parsed_message.get_body(preferencelist=("plain",))
            html_part = parsed_message.get_body(preferencelist=("html",))
            plain_body = plain_part.get_content() if plain_part else ""
            html_body = html_part.get_content() if html_part else None
            if not plain_body and html_part:
                plain_body = html_part.get_content()
            return plain_body, html_body

        content = parsed_message.get_content()
        if parsed_message.get_content_type() == "text/html":
            return content, content
        return content, None
