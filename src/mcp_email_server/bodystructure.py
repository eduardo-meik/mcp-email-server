"""Parse IMAP BODYSTRUCTURE responses and derive attachment metadata.

Attachment metadata is extracted without downloading attachment bytes, either
from a raw BODYSTRUCTURE response or from an already-parsed MIME tree. Both
paths produce identical RFC 3501 part numbers so a ``part_id`` shown in
metadata can later be fetched with ``BODY.PEEK[<part_id>]``.
"""

from __future__ import annotations

import re

from mcp_email_server.models import AttachmentMetadata

INLINE_IMAGE_MAX_BYTES = 20 * 1024

_BODYSTRUCTURE_TOKEN = "BODYSTRUCTURE"


class BodyStructureError(ValueError):
    pass


class BodyPart:
    """One parsed BODYSTRUCTURE node (leaf part or multipart container)."""

    def __init__(self, node: list) -> None:
        self.is_multipart = bool(node) and isinstance(node[0], list)
        self.children: list[BodyPart] = []
        self.media_type = ""
        self.media_subtype = ""
        self.params: dict[str, str] = {}
        self.content_id: str | None = None
        self.encoding = ""
        self.size = 0
        self.disposition: str | None = None
        self.disposition_params: dict[str, str] = {}

        if self.is_multipart:
            index = 0
            while index < len(node) and isinstance(node[index], list):
                self.children.append(BodyPart(node[index]))
                index += 1
            self.media_type = "multipart"
            self.media_subtype = _as_str(node[index]).lower() if index < len(node) and _as_str(node[index]) else ""
            return

        self.media_type = _as_str(node[0]).lower() if len(node) > 0 else ""
        self.media_subtype = _as_str(node[1]).lower() if len(node) > 1 else ""
        self.params = _parse_params(node[2]) if len(node) > 2 else {}
        self.content_id = _as_str(node[3]) if len(node) > 3 else None
        self.encoding = _as_str(node[5]).lower() if len(node) > 5 else ""
        self.size = _as_int(node[6]) if len(node) > 6 else 0

        # Extension data starts after the core fields; text parts carry an extra
        # line-count field and message/rfc822 carries envelope/body/lines.
        ext_index = 7
        if self.media_type == "text":
            ext_index = 8
        elif self.media_type == "message" and self.media_subtype == "rfc822":
            ext_index = 10
        disposition = node[ext_index + 1] if len(node) > ext_index + 1 else None
        if isinstance(disposition, list) and disposition:
            self.disposition = (_as_str(disposition[0]) or "").lower() or None
            if len(disposition) > 1:
                self.disposition_params = _parse_params(disposition[1])

    @property
    def content_type(self) -> str:
        if not self.media_type:
            return "application/octet-stream"
        return f"{self.media_type}/{self.media_subtype or 'octet-stream'}"

    @property
    def filename(self) -> str | None:
        return self.disposition_params.get("filename") or self.params.get("name")


def parse_bodystructure(text: str) -> BodyPart:
    """Parse the parenthesized BODYSTRUCTURE value into a :class:`BodyPart` tree."""

    node, index = _parse_value(text, 0)
    if not isinstance(node, list):
        raise BodyStructureError("BODYSTRUCTURE must be a parenthesized list")
    _skip_ws(text, index)  # tolerate trailing data
    return BodyPart(node)


def extract_bodystructure_value(fetch_line: str) -> str | None:
    """Return the balanced parenthesized BODYSTRUCTURE section of a FETCH line."""

    marker = fetch_line.upper().find(_BODYSTRUCTURE_TOKEN)
    if marker < 0:
        return None
    index = _skip_ws(fetch_line, marker + len(_BODYSTRUCTURE_TOKEN))
    if index >= len(fetch_line) or fetch_line[index] != "(":
        return None

    depth = 0
    in_string = False
    escaped = False
    for cursor in range(index, len(fetch_line)):
        char = fetch_line[cursor]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return fetch_line[index : cursor + 1]
    raise BodyStructureError("unbalanced BODYSTRUCTURE response")


def attachments_from_bodystructure(root: BodyPart) -> list[AttachmentMetadata]:
    """Return attachment metadata for every leaf file part, numbered per RFC 3501."""

    return [metadata for metadata, _encoding in declarations_from_bodystructure(root)]


def declarations_from_bodystructure(root: BodyPart) -> list[tuple[AttachmentMetadata, str]]:
    """Attachment metadata plus the declared transfer encoding per part."""

    declarations: list[tuple[AttachmentMetadata, str]] = []
    if not root.is_multipart:
        return declarations

    for part_id, part in _iter_leaf_parts(root):
        metadata = _leaf_to_metadata(part_id, part)
        if metadata is not None:
            declarations.append((metadata, part.encoding))
    return declarations


def declarations_from_mime(parsed_message) -> list[tuple[AttachmentMetadata, str]]:
    """MIME-tree equivalent of :func:`declarations_from_bodystructure`."""

    declarations: list[tuple[AttachmentMetadata, str]] = []
    if not parsed_message.is_multipart():
        return declarations

    for part_id, part in _iter_mime_leaf_parts(parsed_message):
        content_type = part.get_content_type() or "application/octet-stream"
        media_type = content_type.split("/", 1)[0]
        filename = part.get_filename()
        if not _is_attachment(media_type=media_type, filename=filename):
            continue
        content_id = part.get("Content-ID")
        if content_id:
            content_id = content_id.strip()
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        size = len(payload) if payload is not None else 0
        disposition = part.get_content_disposition()
        metadata = AttachmentMetadata(
            part_id=part_id,
            filename=filename,
            content_type=content_type,
            size_bytes=size,
            inline=_is_inline(disposition=disposition, content_id=content_id, media_type=media_type, size=size),
            content_id=content_id,
        )
        declarations.append((metadata, (part.get("Content-Transfer-Encoding") or "").strip().lower()))
    return declarations


def attachments_from_mime(parsed_message) -> list[AttachmentMetadata]:
    """Return attachment metadata from a parsed ``email.message.EmailMessage``.

    Part numbers match the BODYSTRUCTURE numbering so both paths agree.
    """

    return [metadata for metadata, _encoding in declarations_from_mime(parsed_message)]


def find_body_part(root: BodyPart, part_id: str) -> BodyPart | None:
    for candidate_id, part in _iter_leaf_parts(root):
        if candidate_id == part_id:
            return part
    return None


def _iter_leaf_parts(part: BodyPart, prefix: str = ""):
    if not part.is_multipart:
        return
    for index, child in enumerate(part.children, start=1):
        part_id = f"{prefix}.{index}" if prefix else str(index)
        if child.is_multipart:
            yield from _iter_leaf_parts(child, part_id)
        else:
            yield part_id, child


def _iter_mime_leaf_parts(message, prefix: str = ""):
    index = 0
    for child in message.iter_parts():
        index += 1
        part_id = f"{prefix}.{index}" if prefix else str(index)
        if child.get_content_type() == "message/rfc822":
            yield part_id, child
        elif child.is_multipart():
            yield from _iter_mime_leaf_parts(child, part_id)
        else:
            yield part_id, child


def _leaf_to_metadata(part_id: str, part: BodyPart) -> AttachmentMetadata | None:
    if not _is_attachment(media_type=part.media_type, filename=part.filename):
        return None
    return AttachmentMetadata(
        part_id=part_id,
        filename=part.filename,
        content_type=part.content_type,
        size_bytes=part.size,
        inline=_is_inline(
            disposition=part.disposition,
            content_id=part.content_id,
            media_type=part.media_type,
            size=part.size,
        ),
        content_id=part.content_id,
    )


def _is_attachment(media_type: str, filename: str | None) -> bool:
    if filename:
        return True
    if media_type in {"text", "multipart", ""}:
        return False
    return True


def _is_inline(disposition: str | None, content_id: str | None, media_type: str, size: int) -> bool:
    if disposition == "inline" or content_id:
        return True
    return media_type == "image" and 0 < size < INLINE_IMAGE_MAX_BYTES


def _parse_params(node) -> dict[str, str]:
    if not isinstance(node, list):
        return {}
    params: dict[str, str] = {}
    index = 0
    while index + 1 < len(node):
        name = _as_str(node[index])
        value = _as_str(node[index + 1])
        if name and value is not None:
            params[name.lower()] = value
        index += 2
    return params


def _as_str(value) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value) -> int:
    return value if isinstance(value, int) else 0


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _parse_value(text: str, index: int):
    index = _skip_ws(text, index)
    if index >= len(text):
        raise BodyStructureError("unexpected end of BODYSTRUCTURE")

    char = text[index]
    if char == "(":
        return _parse_list(text, index)
    if char == '"':
        return _parse_quoted(text, index)
    if char == "{":
        # Inline literals inside BODYSTRUCTURE are rare (8-bit parameter values);
        # the bytes live in a different response line, so treat them as absent.
        match = re.match(r"\{\d+\+?\}", text[index:])
        if not match:
            raise BodyStructureError("malformed literal marker")
        return None, index + match.end()

    end = index
    while end < len(text) and text[end] not in " ()":
        end += 1
    token = text[index:end]
    if token.upper() == "NIL":
        return None, end
    if token.isdigit():
        return int(token), end
    raise BodyStructureError(f"unexpected token: {token!r}")


def _parse_list(text: str, index: int):
    items: list = []
    index += 1
    while True:
        index = _skip_ws(text, index)
        if index >= len(text):
            raise BodyStructureError("unbalanced BODYSTRUCTURE list")
        if text[index] == ")":
            return items, index + 1
        item, index = _parse_value(text, index)
        items.append(item)


def _parse_quoted(text: str, index: int):
    chars: list[str] = []
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            chars.append(text[index + 1])
            index += 2
            continue
        if char == '"':
            return "".join(chars), index + 1
        chars.append(char)
        index += 1
    raise BodyStructureError("unterminated quoted string")
