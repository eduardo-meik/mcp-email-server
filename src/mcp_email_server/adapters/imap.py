import asyncio
import re
from datetime import datetime, timezone

from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from urllib.parse import quote, unquote

import aioimaplib

from mcp_email_server.adapters.errors import AdapterConfigurationError, AdapterExecutionError
from mcp_email_server.config import Settings
from mcp_email_server.models import EmailContent, EmailMessage, EmailMetadata, MailboxInfo


class ImapAdapter:
    PROJECT_TAG_PATTERN = re.compile(r"^(?:(?:re|fw|fwd|rv)\s*:\s*)*\[(?P<tag>[^\[\]]+)\]\s*", re.IGNORECASE)
    FETCH_PREFIX_PATTERN = re.compile(rb"^\d+\s+FETCH\s+\(", re.IGNORECASE)
    FLAGS_PATTERN = re.compile(rb"FLAGS\s+\((?P<flags>[^)]*)\)", re.IGNORECASE)
    UID_PATTERN = re.compile(rb"UID\s+(?P<uid>\d+)", re.IGNORECASE)
    MAILBOX_PATTERN = re.compile(r'"(?P<name>[^"]+)"\s*$')
    MESSAGE_ID_PATTERN = re.compile(r"<[^>]+>")
    FLAG_MAP = {
        "seen": r"\Seen",
        "flagged": r"\Flagged",
        "answered": r"\Answered",
    }

    def __init__(self, settings: Settings, client_factory=None) -> None:
        self._settings = settings
        self._client_factory = client_factory or aioimaplib.IMAP4_SSL

    @property
    def mailbox_id(self) -> str:
        return self._settings.resolved_mailbox_id()

    @property
    def account_name(self) -> str:
        return self.mailbox_id

    @property
    def email_address(self) -> str | None:
        return self._settings.imap_username

    @property
    def folder(self) -> str:
        return self._settings.imap_mailbox

    async def fetch_unseen(self, limit: int, since_uid: int | None = None) -> list[EmailMessage]:
        if limit <= 0:
            return []

        missing = self._settings.missing_imap_config()
        if missing:
            raise AdapterConfigurationError(missing)

        client = self._create_client()

        try:
            await self._login_and_select(client, self._settings.imap_mailbox)

            search_response = await client.search("UNSEEN", charset=None)
            self._ensure_ok(search_response.result, "search unseen", search_response.lines)

            message_numbers = self._parse_search_uids(search_response.lines)

            messages: list[EmailMessage] = []
            for message_number in message_numbers:
                fetch_response = await client.fetch(str(message_number), "(UID BODY.PEEK[])")
                self._ensure_ok(fetch_response.result, f"fetch message {message_number}", fetch_response.lines)
                message_bytes = self._extract_message_bytes(fetch_response.lines)
                resolved_uid = self._extract_uid(fetch_response.lines)
                if resolved_uid is None:
                    raise ValueError("FETCH response did not contain a UID")
                if since_uid is not None and resolved_uid <= since_uid:
                    continue
                messages.append(self._parse_message(uid=resolved_uid, message_bytes=message_bytes))
                if len(messages) >= limit:
                    break

            return messages
        except (aioimaplib.AioImapException, asyncio.TimeoutError, ValueError) as exc:
            raise AdapterExecutionError(f"IMAP fetch failed: {exc}") from exc
        finally:
            await self._safe_logout(client)

    async def list_mailboxes(self) -> list[MailboxInfo]:
        self._ensure_configured()
        client = self._create_client()

        try:
            await self._login(client)
            list_response = await client.list()
            self._ensure_ok(list_response.result, "list mailboxes", list_response.lines)
            mailbox_names = self._parse_mailboxes(list_response.lines)
            return [MailboxInfo(name=name) for name in mailbox_names]
        except (aioimaplib.AioImapException, asyncio.TimeoutError, ValueError) as exc:
            raise AdapterExecutionError(f"IMAP list mailboxes failed: {exc}") from exc
        finally:
            await self._safe_logout(client)

    async def list_emails_metadata(
        self,
        page: int = 1,
        page_size: int = 10,
        since: datetime | None = None,
        before: datetime | None = None,
        subject: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        order: str = "desc",
        mailbox: str | None = None,
        seen: bool | None = None,
        flagged: bool | None = None,
        answered: bool | None = None,
    ) -> tuple[list[EmailMetadata], int]:
        selected_mailbox = mailbox or self.folder
        client = self._create_client()

        try:
            await self._login_and_select(client, selected_mailbox)
            message_numbers = await self._search_uids(
                client,
                since=since,
                before=before,
                subject=subject,
                from_address=from_address,
                to_address=to_address,
                seen=seen,
                flagged=flagged,
                answered=answered,
            )
            ordered_message_numbers = sorted(message_numbers, reverse=order.lower() != "asc")
            total = len(ordered_message_numbers)
            page_slice = self._paginate_uids(ordered_message_numbers, page=page, page_size=page_size)
            emails = [
                (await self._fetch_email_content(client, message_number=message_number, mailbox=selected_mailbox)).as_metadata()
                for message_number in page_slice
            ]
            return emails, total
        except (aioimaplib.AioImapException, asyncio.TimeoutError, ValueError) as exc:
            raise AdapterExecutionError(f"IMAP list emails failed: {exc}") from exc
        finally:
            await self._safe_logout(client)

    async def get_emails_content(self, email_ids: list[str], mailbox: str | None = None) -> list[EmailContent]:
        if not email_ids:
            return []

        grouped = self._group_email_ids(email_ids, default_mailbox=mailbox or self.folder)
        client = self._create_client()
        emails: list[EmailContent] = []

        try:
            await self._login(client)
            for selected_mailbox, uids in grouped.items():
                await self._select_mailbox(client, selected_mailbox)
                for uid in uids:
                    emails.append(await self._fetch_email_content_by_uid(client, uid=uid, mailbox=selected_mailbox))
            return emails
        except (aioimaplib.AioImapException, asyncio.TimeoutError, ValueError) as exc:
            raise AdapterExecutionError(f"IMAP get email content failed: {exc}") from exc
        finally:
            await self._safe_logout(client)

    async def search_emails(self, query: str, mailbox: str | None = None, page_size: int = 20) -> tuple[list[EmailMetadata], int]:
        selected_mailbox = mailbox or self.folder
        client = self._create_client()

        try:
            await self._login_and_select(client, selected_mailbox)
            message_numbers = await self._search_uids(client, text_query=query)
            ordered_message_numbers = sorted(message_numbers, reverse=True)
            page_slice = ordered_message_numbers[:page_size]
            emails = [
                (await self._fetch_email_content(client, message_number=message_number, mailbox=selected_mailbox)).as_metadata()
                for message_number in page_slice
            ]
            return emails, len(ordered_message_numbers)
        except (aioimaplib.AioImapException, asyncio.TimeoutError, ValueError) as exc:
            raise AdapterExecutionError(f"IMAP search failed: {exc}") from exc
        finally:
            await self._safe_logout(client)

    async def get_thread(self, message_id: str, mailbox: str | None = None) -> list[EmailContent]:
        selected_mailbox = mailbox or self.folder
        client = self._create_client()

        try:
            await self._login_and_select(client, selected_mailbox)
            pending_message_ids = [message_id]
            visited_message_ids: set[str] = set()
            thread_messages: dict[int, EmailContent] = {}

            while pending_message_ids:
                current_message_id = pending_message_ids.pop(0)
                if current_message_id in visited_message_ids:
                    continue
                visited_message_ids.add(current_message_id)

                related_message_numbers: set[int] = set()
                for header_name in ("Message-ID", "In-Reply-To", "References"):
                    related_message_numbers.update(await self._search_header_uids(client, header_name, current_message_id))

                for message_number in sorted(related_message_numbers):
                    if message_number in thread_messages:
                        continue
                    email = await self._fetch_email_content(client, message_number=message_number, mailbox=selected_mailbox)
                    thread_messages[message_number] = email
                    for related_message_id in self._extract_related_message_ids(email):
                        if related_message_id not in visited_message_ids:
                            pending_message_ids.append(related_message_id)

            return sorted(
                thread_messages.values(),
                key=lambda email: (email.received_at or datetime.min.replace(tzinfo=timezone.utc), email.uid),
            )
        except (aioimaplib.AioImapException, asyncio.TimeoutError, ValueError) as exc:
            raise AdapterExecutionError(f"IMAP get thread failed: {exc}") from exc
        finally:
            await self._safe_logout(client)

    async def mark_email(self, email_ids: list[str], flag: str, enable: bool = True, mailbox: str | None = None) -> list[str]:
        if flag not in self.FLAG_MAP:
            raise AdapterExecutionError(f"Unsupported flag: {flag}")

        grouped = self._group_email_ids(email_ids, default_mailbox=mailbox or self.folder)
        client = self._create_client()

        try:
            await self._login(client)
            for selected_mailbox, uids in grouped.items():
                await self._select_mailbox(client, selected_mailbox)
                message_set = ",".join(str(uid) for uid in uids)
                store_command = "+FLAGS.SILENT" if enable else "-FLAGS.SILENT"
                store_response = await client.uid("STORE", message_set, store_command, f"({self.FLAG_MAP[flag]})")
                self._ensure_ok(store_response.result, f"store {flag}", store_response.lines)
        except (aioimaplib.AioImapException, asyncio.TimeoutError, ValueError) as exc:
            raise AdapterExecutionError(f"IMAP mark failed: {exc}") from exc
        finally:
            await self._safe_logout(client)

        return email_ids

    async def move_email(
        self,
        email_ids: list[str],
        destination_mailbox: str,
        source_mailbox: str | None = None,
    ) -> list[str]:
        grouped = self._group_email_ids(email_ids, default_mailbox=source_mailbox or self.folder)
        client = self._create_client()

        try:
            await self._login(client)
            for selected_mailbox, uids in grouped.items():
                await self._select_mailbox(client, selected_mailbox)
                message_set = ",".join(str(uid) for uid in uids)
                move_response = await client.uid("MOVE", message_set, destination_mailbox)
                if move_response.result != "OK":
                    copy_response = await client.uid("COPY", message_set, destination_mailbox)
                    self._ensure_ok(copy_response.result, f"copy to {destination_mailbox}", copy_response.lines)
                    delete_response = await client.uid("STORE", message_set, "+FLAGS.SILENT", r"(\Deleted)")
                    self._ensure_ok(delete_response.result, "mark deleted after copy", delete_response.lines)
                    expunge_response = await client.expunge()
                    self._ensure_ok(expunge_response.result, "expunge after copy", expunge_response.lines)
                    continue
                self._ensure_ok(move_response.result, f"move to {destination_mailbox}", move_response.lines)
        except (aioimaplib.AioImapException, asyncio.TimeoutError, ValueError) as exc:
            raise AdapterExecutionError(f"IMAP move failed: {exc}") from exc
        finally:
            await self._safe_logout(client)

        return email_ids

    async def delete_emails(self, email_ids: list[str], mailbox: str | None = None) -> list[str]:
        grouped = self._group_email_ids(email_ids, default_mailbox=mailbox or self.folder)
        client = self._create_client()

        try:
            await self._login(client)
            for selected_mailbox, uids in grouped.items():
                await self._select_mailbox(client, selected_mailbox)
                message_set = ",".join(str(uid) for uid in uids)
                store_response = await client.uid("STORE", message_set, "+FLAGS.SILENT", r"(\Deleted)")
                self._ensure_ok(store_response.result, "mark deleted", store_response.lines)
                expunge_response = await client.expunge()
                self._ensure_ok(expunge_response.result, "expunge", expunge_response.lines)
        except (aioimaplib.AioImapException, asyncio.TimeoutError, ValueError) as exc:
            raise AdapterExecutionError(f"IMAP delete failed: {exc}") from exc
        finally:
            await self._safe_logout(client)

        return email_ids

    def _create_client(self):
        self._ensure_configured()
        return self._client_factory(
            host=self._settings.imap_host,
            port=self._settings.imap_port,
        )

    def _ensure_configured(self) -> None:
        missing = self._settings.missing_imap_config()
        if missing:
            raise AdapterConfigurationError(missing)

    async def _login(self, client) -> None:
        await client.wait_hello_from_server()
        login_response = await client.login(
            self._settings.imap_username,
            self._settings.imap_password.get_secret_value(),
        )
        self._ensure_ok(login_response.result, "login", login_response.lines)

    async def _select_mailbox(self, client, mailbox: str) -> None:
        select_response = await client.select(mailbox)
        self._ensure_ok(select_response.result, f"select {mailbox}", select_response.lines)

    async def _login_and_select(self, client, mailbox: str) -> None:
        await self._login(client)
        await self._select_mailbox(client, mailbox)

    async def _safe_logout(self, client) -> None:
        try:
            await client.logout()
        except (aioimaplib.AioImapException, asyncio.TimeoutError, AttributeError):
            pass

    async def _search_uids(
        self,
        client,
        since: datetime | None = None,
        before: datetime | None = None,
        subject: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        seen: bool | None = None,
        flagged: bool | None = None,
        answered: bool | None = None,
        text_query: str | None = None,
    ) -> list[int]:
        criteria: list[str] = []

        if seen is True:
            criteria.append("SEEN")
        elif seen is False:
            criteria.append("UNSEEN")

        if flagged is True:
            criteria.append("FLAGGED")
        elif flagged is False:
            criteria.append("UNFLAGGED")

        if answered is True:
            criteria.append("ANSWERED")
        elif answered is False:
            criteria.append("UNANSWERED")

        if since is not None:
            criteria.extend(["SINCE", since.strftime("%d-%b-%Y")])
        if before is not None:
            criteria.extend(["BEFORE", before.strftime("%d-%b-%Y")])
        if subject:
            criteria.extend(["HEADER", "Subject", subject])
        if from_address:
            criteria.extend(["HEADER", "From", from_address])
        if to_address:
            criteria.extend(["HEADER", "To", to_address])
        if text_query:
            criteria.extend(["TEXT", text_query])
        if not criteria:
            criteria.append("ALL")

        search_response = await client.search(*criteria, charset=None)
        self._ensure_ok(search_response.result, "search emails", search_response.lines)
        return self._parse_search_uids(search_response.lines)

    async def _search_header_uids(self, client, header_name: str, header_value: str) -> list[int]:
        if not header_value.strip():
            return []
        search_response = await client.search("HEADER", header_name, header_value, charset=None)
        self._ensure_ok(search_response.result, f"search header {header_name}", search_response.lines)
        return self._parse_search_uids(search_response.lines)

    async def _fetch_email_content(self, client, message_number: int, mailbox: str) -> EmailContent:
        fetch_response = await client.fetch(str(message_number), "(UID FLAGS BODY.PEEK[])")
        self._ensure_ok(fetch_response.result, f"fetch message {message_number}", fetch_response.lines)
        message_bytes = self._extract_message_bytes(fetch_response.lines)
        resolved_uid = self._extract_uid(fetch_response.lines)
        if resolved_uid is None:
            raise ValueError("FETCH response did not contain a UID")
        flags = self._extract_flags(fetch_response.lines)
        return self._parse_email_content(uid=resolved_uid, mailbox=mailbox, message_bytes=message_bytes, flags=flags)

    async def _fetch_email_content_by_uid(self, client, uid: int, mailbox: str) -> EmailContent:
        fetch_response = await client.uid("FETCH", str(uid), "(FLAGS BODY.PEEK[])")
        self._ensure_ok(fetch_response.result, f"fetch uid {uid}", fetch_response.lines)
        message_bytes = self._extract_message_bytes(fetch_response.lines)
        resolved_uid = self._extract_uid(fetch_response.lines) or uid
        flags = self._extract_flags(fetch_response.lines)
        return self._parse_email_content(uid=resolved_uid, mailbox=mailbox, message_bytes=message_bytes, flags=flags)

    @staticmethod
    def build_email_id(mailbox: str, uid: int) -> str:
        return f"{quote(mailbox, safe='')}:{uid}"

    @staticmethod
    def parse_email_id(email_id: str, default_mailbox: str) -> tuple[str, int]:
        mailbox_token, separator, uid_token = email_id.rpartition(":")
        if not separator or not uid_token.isdigit():
            raise ValueError(f"Invalid email_id: {email_id}")
        mailbox = unquote(mailbox_token) if mailbox_token else default_mailbox
        return mailbox or default_mailbox, int(uid_token)

    @classmethod
    def _group_email_ids(cls, email_ids: list[str], default_mailbox: str) -> dict[str, list[int]]:
        grouped: dict[str, list[int]] = {}
        for email_id in email_ids:
            mailbox, uid = cls.parse_email_id(email_id, default_mailbox=default_mailbox)
            grouped.setdefault(mailbox, []).append(uid)
        return grouped

    @staticmethod
    def _paginate_uids(uids: list[int], page: int, page_size: int) -> list[int]:
        normalized_page = max(page, 1)
        normalized_page_size = max(page_size, 1)
        start = (normalized_page - 1) * normalized_page_size
        end = start + normalized_page_size
        return uids[start:end]

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

    @classmethod
    def _parse_mailboxes(cls, lines: list[bytes | bytearray]) -> list[str]:
        mailboxes: list[str] = []
        for line in lines:
            if not isinstance(line, (bytes, bytearray)):
                continue
            decoded = bytes(line).decode("utf-8", errors="ignore").strip()
            if not decoded or decoded.upper().endswith("LIST COMPLETED"):
                continue
            match = cls.MAILBOX_PATTERN.search(decoded)
            if match:
                mailboxes.append(match.group("name"))
                continue
            mailbox = decoded.rsplit(" ", maxsplit=1)[-1].strip('"')
            if mailbox:
                mailboxes.append(mailbox)
        return mailboxes

    @classmethod
    def _extract_flags(cls, lines: list[bytes | bytearray]) -> set[str]:
        joined = b" ".join(bytes(line) for line in lines if isinstance(line, (bytes, bytearray)))
        match = cls.FLAGS_PATTERN.search(joined)
        if not match:
            return set()
        flags_text = match.group("flags").decode("utf-8", errors="ignore").strip()
        return {flag for flag in flags_text.split() if flag}

    @classmethod
    def _extract_uid(cls, lines: list[bytes | bytearray]) -> int | None:
        joined = b" ".join(bytes(line) for line in lines if isinstance(line, (bytes, bytearray)))
        match = cls.UID_PATTERN.search(joined)
        if not match:
            return None
        return int(match.group("uid"))

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

    def _parse_email_content(self, uid: int, mailbox: str, message_bytes: bytes, flags: set[str]) -> EmailContent:
        parsed = BytesParser(policy=policy.default).parsebytes(message_bytes)
        plain_body, html_body = ImapAdapter._extract_bodies(parsed)
        recipients = [address for _, address in getaddresses(parsed.get_all("to", []) + parsed.get_all("cc", [])) if address]
        subject = self._decode_header_value(parsed.get("subject"))
        received_at = None
        if parsed.get("date"):
            received_at = parsedate_to_datetime(parsed["date"])

        return EmailContent(
            email_id=self.build_email_id(mailbox, uid),
            account_name=self.account_name,
            uid=uid,
            mailbox=mailbox,
            message_id=parsed.get("message-id"),
            in_reply_to=parsed.get("in-reply-to"),
            references=_strip_or_none(parsed.get("references")),
            subject=subject,
            project_tag=self._extract_project_tag(subject),
            from_address=getaddresses(parsed.get_all("from", []))[0][1] if parsed.get_all("from", []) else "",
            to_addresses=recipients,
            received_at=received_at,
            seen=r"\Seen" in flags,
            flagged=r"\Flagged" in flags,
            answered=r"\Answered" in flags,
            has_attachments=any(True for _ in parsed.iter_attachments()),
            text_body=plain_body,
            html_body=html_body,
        )

    @classmethod
    def _extract_related_message_ids(cls, email: EmailContent) -> list[str]:
        related: list[str] = []
        for candidate in (email.message_id, email.in_reply_to, *cls._parse_reference_ids(email.references)):
            if candidate and candidate not in related:
                related.append(candidate)
        return related

    @classmethod
    def _parse_reference_ids(cls, references: str | None) -> list[str]:
        if not references:
            return []
        return cls.MESSAGE_ID_PATTERN.findall(references)

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


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
