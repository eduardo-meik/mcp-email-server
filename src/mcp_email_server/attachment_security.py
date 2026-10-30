"""Pure security validators for inbound attachments (pipeline layers 1-6).

Every function is side-effect free and fail-closed: when a control cannot be
evaluated the attachment is rejected. Layers are ordered cheap -> expensive so
most inputs are discarded before spending CPU, network or tokens.
"""

from __future__ import annotations

import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

from mcp_email_server.models import AttachmentMetadata

# ---------------------------------------------------------------------------
# Policy decisions (confirmed): PDF, XML (DTE), XLSX, DOCX only.
# ---------------------------------------------------------------------------

ALLOWED_EXTENSIONS: dict[str, set[str]] = {
    ".pdf": {"application/pdf"},
    ".xml": {"application/xml", "text/xml"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
}

EXECUTABLE_SIGNATURES: tuple[bytes, ...] = (
    b"MZ",               # PE / DOS executable
    b"\x7fELF",          # ELF
    b"\xfe\xed\xfa",     # Mach-O
    b"\xcf\xfa\xed\xfe", # Mach-O
    b"\xca\xfe\xba\xbe", # Mach-O fat / Java class
    b"#!",               # scripts
)

_BIDI_OVERRIDE_RANGE = range(0x202A, 0x202F)
_BIDI_ISOLATE_RANGE = range(0x2066, 0x206A)
_TAG_CHAR_RANGE = range(0xE0000, 0xE0080)
_ZERO_WIDTH_CHARS = {"​", "‌", "‍", "﻿", "­"}

_FILENAME_SANITIZE_PATTERN = re.compile(r"[\x00-\x1f\x7f<>:\"|?*]")
_AUTH_RESULT_ENTRY_PATTERN = re.compile(r"(?P<method>dkim|dmarc|spf)\s*=\s*(?P<result>[a-z]+)", re.IGNORECASE)

PDF_FORBIDDEN_TOKENS: tuple[bytes, ...] = (
    b"/JavaScript",
    b"/JS",
    b"/Launch",
    b"/EmbeddedFile",
    b"/AA",
    b"/XFA",
    b"/Encrypt",
)
PDF_OPENACTION_PATTERN = re.compile(rb"/OpenAction\s*(?:\[\s*)?\d+\s+\d+\s+R")

OOXML_MAX_ENTRIES = 512
OOXML_MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
OOXML_MAX_COMPRESSION_RATIO = 100.0
OOXML_FORBIDDEN_MEMBERS = ("vbaproject.bin",)
OOXML_FORBIDDEN_PARTS = ("externallink", "oleobject", "activex")
OOXML_MACRO_TYPES = {
    "application/vnd.ms-excel.sheet.macroenabled.main+xml",
    "application/vnd.ms-word.document.macroenabled.main+xml",
}


@dataclass(frozen=True)
class SenderPolicy:
    pattern: str
    action: str  # "allow" | "deny"


@dataclass(frozen=True)
class AuthEvaluation:
    authserv_id: str
    trusted_server: bool
    passed: bool
    results: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DeclarationVerdict:
    allowed: bool
    reject_reason: str | None = None


def normalize_sender(sender: str) -> str:
    return sender.replace("\x00", "").strip().lower()


def parse_authentication_results(header_value: str | None, expected_authserv_id: str | None) -> AuthEvaluation:
    """Evaluate only the topmost Authentication-Results header.

    Lower headers may be forged by the sender, so they are never considered.
    Without a pass from the trusted server the result is not passed.
    """

    if not header_value:
        return AuthEvaluation(authserv_id="", trusted_server=False, passed=False)

    authserv_id, _, methods_text = header_value.partition(";")
    authserv_id = authserv_id.strip().lower()

    results = {
        match.group("method").lower(): match.group("result").lower()
        for match in _AUTH_RESULT_ENTRY_PATTERN.finditer(methods_text)
    }

    trusted_server = True
    if expected_authserv_id:
        expected = expected_authserv_id.strip().lower()
        trusted_server = authserv_id == expected or authserv_id.endswith(f".{expected}") or expected.endswith(f".{authserv_id}")

    passed = trusted_server and (results.get("dkim") == "pass" or results.get("dmarc") == "pass")
    return AuthEvaluation(
        authserv_id=authserv_id,
        trusted_server=trusted_server,
        passed=passed,
        results=results,
    )


def policy_matches(pattern: str, sender: str) -> bool:
    normalized_pattern = pattern.strip().lower()
    if not normalized_pattern or not sender:
        return False
    if normalized_pattern.startswith("@"):
        return sender.endswith(normalized_pattern)
    return sender == normalized_pattern


def evaluate_sender_trust(sender: str, auth: AuthEvaluation, policies: list[SenderPolicy]) -> str:
    """Return ``trusted`` only when an allow rule matches AND auth passed."""

    normalized_sender = normalize_sender(sender)
    matched_actions = [policy.action for policy in policies if policy_matches(policy.pattern, normalized_sender)]
    if "deny" in matched_actions:
        return "denied"
    if "allow" in matched_actions and auth.passed:
        return "trusted"
    return "untrusted"


def sanitize_filename(filename: str | None) -> str:
    """Strip control/path-hostile characters; never trust the result as a path."""

    if not filename:
        return ""
    cleaned = unicodedata.normalize("NFKC", filename)
    cleaned = _FILENAME_SANITIZE_PATTERN.sub("_", cleaned)
    cleaned = "".join("_" if _is_forbidden_unicode(char) else char for char in cleaned)
    return cleaned.replace("/", "_").replace("\\", "_").strip().strip(".")


def _is_forbidden_unicode(char: str) -> bool:
    code = ord(char)
    return (
        code in _BIDI_OVERRIDE_RANGE
        or code in _BIDI_ISOLATE_RANGE
        or code in _TAG_CHAR_RANGE
        or char in _ZERO_WIDTH_CHARS
    )


def validate_filename(filename: str | None) -> str | None:
    """Return a reject reason when the filename is missing, hostile or not allowed."""

    if not filename:
        return "missing filename"
    if any(_is_forbidden_unicode(char) for char in filename):
        return "filename contains hidden unicode controls"
    suffix = _final_extension(filename)
    if suffix not in ALLOWED_EXTENSIONS:
        return f"extension not allowed: {suffix or '(none)'}"
    return None


def _final_extension(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def check_declarations(
    attachments: list[AttachmentMetadata],
    max_count: int,
    max_file_bytes: int,
    max_message_bytes: int,
) -> list[tuple[AttachmentMetadata, str | None]]:
    """Layer 2: validate declarations from BODYSTRUCTURE before any download.

    Returns ``(attachment, reject_reason)`` pairs; a ``None`` reason means the
    declaration passed the cheap checks.
    """

    verdicts: list[tuple[AttachmentMetadata, str | None]] = []
    candidates = [item for item in attachments if not item.inline]

    if len(candidates) > max_count:
        return [(item, f"too many attachments (> {max_count})") for item in candidates]

    total_declared = sum(item.size_bytes for item in candidates)
    for item in candidates:
        reason = validate_filename(item.filename)
        if reason is None and item.size_bytes > max_file_bytes:
            reason = f"declared size exceeds {max_file_bytes} bytes"
        if reason is None and total_declared > max_message_bytes:
            reason = f"message attachments exceed {max_message_bytes} bytes in total"
        if reason is None:
            allowed_types = ALLOWED_EXTENSIONS[_final_extension(item.filename or "")]
            declared = item.content_type.lower()
            if declared not in allowed_types and declared != "application/octet-stream":
                reason = f"declared type {declared} does not match extension"
        verdicts.append((item, reason))
    return verdicts


def detect_content_type(data: bytes) -> str | None:
    """Layer 4: magic-bytes detection. ``None`` means unrecognized."""

    if not data:
        return None
    if any(data.startswith(signature) for signature in EXECUTABLE_SIGNATURES):
        return "application/x-executable"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"):
        return "application/zip"
    probe = data[:512].lstrip(b"\xef\xbb\xbf\xff\xfe\xfe\xff\x00\t\r\n ")
    if probe.startswith(b"<"):
        return "text/xml"
    return None


def validate_bytes(data: bytes, filename: str, declared_content_type: str, max_file_bytes: int) -> str | None:
    """Layers 3-5 on downloaded bytes. Returns a reject reason or ``None``."""

    if len(data) > max_file_bytes:
        return f"actual size exceeds {max_file_bytes} bytes"

    suffix = _final_extension(filename)
    if suffix not in ALLOWED_EXTENSIONS:
        return f"extension not allowed: {suffix or '(none)'}"

    detected = detect_content_type(data)
    if detected == "application/x-executable":
        return "executable content detected"
    if detected is None:
        return "unrecognized content (fail-closed)"

    if suffix == ".pdf" and detected != "application/pdf":
        return f"magic bytes {detected} do not match .pdf"
    if suffix in {".xlsx", ".docx"} and detected != "application/zip":
        return f"magic bytes {detected} do not match {suffix}"
    if suffix == ".xml" and detected != "text/xml":
        return f"magic bytes {detected} do not match .xml"

    if suffix == ".pdf":
        return inspect_pdf(data)
    if suffix in {".xlsx", ".docx"}:
        return inspect_ooxml(data, suffix)
    return inspect_xml(data)


def inspect_pdf(data: bytes) -> str | None:
    """Layer 5 for PDF: reject active content and encryption (cheap byte scan)."""

    for token in PDF_FORBIDDEN_TOKENS:
        if token in data:
            return f"PDF contains forbidden token {token.decode()}"
    if PDF_OPENACTION_PATTERN.search(data):
        return "PDF contains an automatic OpenAction"
    return None


def inspect_ooxml(data: bytes, suffix: str) -> str | None:
    """Layer 5 for OOXML: macro/OLE/external-link checks and zip-bomb limits."""

    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile:
        return "corrupt OOXML container"

    infos = archive.infolist()
    if len(infos) > OOXML_MAX_ENTRIES:
        return f"OOXML has too many entries (> {OOXML_MAX_ENTRIES})"

    total_uncompressed = 0
    for info in infos:
        name = info.filename.replace("\\", "/").lower()
        leaf = name.rsplit("/", 1)[-1]
        if leaf in OOXML_FORBIDDEN_MEMBERS:
            return "OOXML contains a VBA macro project"
        if any(fragment in name for fragment in OOXML_FORBIDDEN_PARTS):
            return f"OOXML contains forbidden part {info.filename}"
        if leaf.endswith((".docm", ".xlsm")):
            return "macro-enabled document embedded"
        total_uncompressed += info.file_size

    if total_uncompressed > OOXML_MAX_UNCOMPRESSED_BYTES:
        return "OOXML expands beyond the uncompressed size limit"
    compressed = max(len(data), 1)
    if total_uncompressed / compressed > OOXML_MAX_COMPRESSION_RATIO:
        return "OOXML compression ratio looks like a zip bomb"

    content_types = _read_member(archive, "[Content_Types].xml")
    if content_types is None:
        return "OOXML is missing [Content_Types].xml"
    lowered = content_types.lower()
    if any(macro_type.encode() in lowered for macro_type in OOXML_MACRO_TYPES):
        return "OOXML declares a macro-enabled content type"
    return None


def _read_member(archive: zipfile.ZipFile, name: str, max_bytes: int = 1024 * 1024) -> bytes | None:
    try:
        with archive.open(name) as member:
            return member.read(max_bytes + 1)[:max_bytes]
    except KeyError:
        return None
    except (NotImplementedError, RuntimeError):
        # Encrypted or unsupported-compression member: fail closed.
        return None


def inspect_xml(data: bytes) -> str | None:
    """Layer 5 for XML/DTE: defusedxml blocks XXE and entity-expansion attacks."""

    if len(data) > 8 * 1024 * 1024:
        return "XML exceeds the parse size limit"
    try:
        from defusedxml import DefusedXmlException
        from defusedxml.ElementTree import fromstring as safe_fromstring
    except ImportError:  # fail closed if the control cannot run
        return "defusedxml is not installed"

    try:
        safe_fromstring(data)
    except DefusedXmlException as exc:
        return f"XML rejected: {exc.__class__.__name__}"
    except Exception:
        return "XML is not well-formed"
    return None


def decode_transfer(data: bytes, encoding: str) -> bytes:
    """Decode a Content-Transfer-Encoding; unknown encodings fail closed."""

    normalized = (encoding or "").strip().lower()
    if normalized in {"", "7bit", "8bit", "binary"}:
        return data
    if normalized == "base64":
        import base64
        import binascii

        try:
            return base64.b64decode(b"".join(data.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid base64 transfer encoding") from exc
    if normalized in {"quoted-printable", "quopri"}:
        import quopri

        return quopri.decodestring(data)
    raise ValueError(f"unsupported transfer encoding: {normalized}")
