import zipfile
from io import BytesIO

import pytest

from mcp_email_server.attachment_security import (
    AuthEvaluation,
    SenderPolicy,
    check_declarations,
    decode_transfer,
    detect_content_type,
    evaluate_sender_trust,
    inspect_ooxml,
    inspect_pdf,
    inspect_xml,
    parse_authentication_results,
    sanitize_filename,
    validate_bytes,
    validate_filename,
)
from mcp_email_server.models import AttachmentMetadata

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

PDF_CLEAN = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << >>\n%%EOF"
PDF_WITH_JS = b"%PDF-1.4\n1 0 obj << /S /JavaScript /JS (app.alert('x')) >> endobj\n%%EOF"
PDF_WITH_LAUNCH = b"%PDF-1.4\n2 0 obj << /S /Launch /F (cmd.exe) >> endobj\n%%EOF"
PDF_WITH_OPENACTION = b"%PDF-1.4\n3 0 obj << /OpenAction [5 0 R] >> endobj\n%%EOF"

XML_CLEAN = b'<?xml version="1.0" encoding="UTF-8"?><DTE><Documento/></DTE>'
XML_XXE = (
    b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
    b"<r>&x;</r>"
)
XML_BILLION_LAUGHS = (
    b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "xxxxxxxxxx">'
    b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
    b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
    b'<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">'
    b'<!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">]>'
    b"<r>&e;</r>"
)


def make_zip(members: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


CONTENT_TYPES_XLSX = (
    b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Override PartName="/xl/workbook.xml" '
    b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    b"</Types>"
)
CONTENT_TYPES_XLSM = CONTENT_TYPES_XLSX.replace(
    b"vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    b"vnd.ms-excel.sheet.macroEnabled.main+xml",
)

OOXML_BASE = {"[Content_Types].xml": CONTENT_TYPES_XLSX, "xl/workbook.xml": b"<workbook/>"}
OOXML_WITH_MACRO = {**OOXML_BASE, "xl/vbaProject.bin": b"macros"}
OOXML_MACRO_CONTENT_TYPE = {"[Content_Types].xml": CONTENT_TYPES_XLSM, "xl/workbook.xml": b"<workbook/>"}
OOXML_EXTERNAL_LINK = {**OOXML_BASE, "xl/externalLinks/externalLink1.xml": b"<externalLink/>"}
OOXML_OLE = {**OOXML_BASE, "xl/embeddings/oleObject1.bin": b"ole"}


def metadata(filename: str = "doc.pdf", size: int = 100, content_type: str = "application/pdf") -> AttachmentMetadata:
    return AttachmentMetadata(part_id="2", filename=filename, content_type=content_type, size_bytes=size)


class TestFilenameValidation:
    def test_rejects_bidi_override_extension_spoof(self) -> None:
        assert validate_filename("factura‮fdp.exe") is not None

    def test_rejects_disallowed_extension(self) -> None:
        assert validate_filename("script.exe") is not None
        assert validate_filename("photo.png") is not None
        assert validate_filename("data.csv") is not None

    def test_rejects_missing_filename(self) -> None:
        assert validate_filename(None) is not None

    def test_double_extension_resolves_to_final_extension(self) -> None:
        assert validate_filename("factura.pdf.exe") is not None
        assert validate_filename("factura.exe.pdf") is None

    def test_accepts_allowed_extensions(self) -> None:
        for name in ("a.pdf", "b.xml", "c.xlsx", "d.docx"):
            assert validate_filename(name) is None

    def test_sanitize_filename_removes_path_and_control_chars(self) -> None:
        assert sanitize_filename("../../etc/passwd") == "_.._etc_passwd"
        assert sanitize_filename("a\x00b.pdf") == "a_b.pdf"
        assert sanitize_filename("factura‮fdp.pdf") == "factura_fdp.pdf"


class TestAuthenticationResults:
    def test_topmost_header_pass_with_matching_authserv(self) -> None:
        auth = parse_authentication_results(
            "mail.test.local; dkim=pass header.i=@example.com; dmarc=pass; spf=pass",
            expected_authserv_id="mail.test.local",
        )
        assert auth.passed is True
        assert auth.trusted_server is True

    def test_forged_authserv_id_is_not_trusted(self) -> None:
        auth = parse_authentication_results(
            "evil.example; dkim=pass header.i=@example.com; dmarc=pass",
            expected_authserv_id="mail.test.local",
        )
        assert auth.trusted_server is False
        assert auth.passed is False

    def test_dkim_fail_is_not_passed(self) -> None:
        auth = parse_authentication_results(
            "mail.test.local; dkim=fail; dmarc=fail; spf=pass",
            expected_authserv_id="mail.test.local",
        )
        assert auth.passed is False

    def test_missing_header_is_not_passed(self) -> None:
        auth = parse_authentication_results(None, expected_authserv_id="mail.test.local")
        assert auth.passed is False


class TestSenderTrust:
    POLICIES = [
        SenderPolicy(pattern="@proveedor.cl", action="allow"),
        SenderPolicy(pattern="contador@empresa.cl", action="allow"),
        SenderPolicy(pattern="@blocked.example", action="deny"),
    ]
    AUTH_OK = AuthEvaluation(authserv_id="mail.test.local", trusted_server=True, passed=True)
    AUTH_FAIL = AuthEvaluation(authserv_id="mail.test.local", trusted_server=True, passed=False)

    def test_domain_allow_with_auth_pass(self) -> None:
        assert evaluate_sender_trust("facturas@proveedor.cl", self.AUTH_OK, self.POLICIES) == "trusted"

    def test_exact_email_allow(self) -> None:
        assert evaluate_sender_trust("contador@empresa.cl", self.AUTH_OK, self.POLICIES) == "trusted"

    def test_allow_without_auth_pass_is_untrusted(self) -> None:
        assert evaluate_sender_trust("facturas@proveedor.cl", self.AUTH_FAIL, self.POLICIES) == "untrusted"

    def test_deny_beats_allow(self) -> None:
        policies = [*self.POLICIES, SenderPolicy(pattern="info@blocked.example", action="allow")]
        assert evaluate_sender_trust("info@blocked.example", self.AUTH_OK, policies) == "denied"

    def test_unknown_sender_is_untrusted(self) -> None:
        assert evaluate_sender_trust("random@other.example", self.AUTH_OK, self.POLICIES) == "untrusted"


class TestDeclarations:
    def test_rejects_too_many_attachments(self) -> None:
        items = [metadata(filename=f"f{i}.pdf") for i in range(11)]
        verdicts = check_declarations(items, max_count=10, max_file_bytes=10_000, max_message_bytes=25_000)
        assert all(reason is not None for _, reason in verdicts)

    def test_rejects_oversized_file(self) -> None:
        verdicts = check_declarations(
            [metadata(size=11_000)], max_count=10, max_file_bytes=10_000, max_message_bytes=25_000
        )
        assert verdicts[0][1] is not None

    def test_rejects_oversized_message_total(self) -> None:
        items = [metadata(size=9_000) for _ in range(3)]
        verdicts = check_declarations(items, max_count=10, max_file_bytes=10_000, max_message_bytes=25_000)
        assert all(reason is not None for _, reason in verdicts)

    def test_rejects_type_extension_mismatch(self) -> None:
        verdicts = check_declarations(
            [metadata(content_type="application/x-msdownload")],
            max_count=10,
            max_file_bytes=10_000,
            max_message_bytes=25_000,
        )
        assert "does not match" in (verdicts[0][1] or "")

    def test_inline_attachments_are_skipped(self) -> None:
        inline = AttachmentMetadata(part_id="2", filename="logo.png", content_type="image/png", size_bytes=800, inline=True)
        verdicts = check_declarations([inline], max_count=10, max_file_bytes=10_000, max_message_bytes=25_000)
        assert verdicts == []

    def test_octet_stream_declaration_is_accepted(self) -> None:
        verdicts = check_declarations(
            [metadata(content_type="application/octet-stream")],
            max_count=10,
            max_file_bytes=10_000,
            max_message_bytes=25_000,
        )
        assert verdicts[0][1] is None


class TestMagicBytes:
    def test_eicar_disguised_as_pdf_is_rejected(self) -> None:
        reason = validate_bytes(EICAR, "scan.pdf", "application/pdf", max_file_bytes=10_000)
        assert reason is not None

    def test_executable_is_rejected(self) -> None:
        reason = validate_bytes(b"MZ\x90\x00" + b"0" * 100, "tool.pdf", "application/pdf", max_file_bytes=10_000)
        assert reason == "executable content detected"

    def test_unknown_content_is_rejected_fail_closed(self) -> None:
        reason = validate_bytes(b"\x01\x02\x03\x04random", "doc.pdf", "application/pdf", max_file_bytes=10_000)
        assert reason is not None

    def test_xml_extension_rejects_zip_bytes(self) -> None:
        reason = validate_bytes(make_zip(OOXML_BASE), "dte.xml", "text/xml", max_file_bytes=10_000)
        assert "do not match" in (reason or "")

    def test_detect_content_type(self) -> None:
        assert detect_content_type(PDF_CLEAN) == "application/pdf"
        assert detect_content_type(make_zip(OOXML_BASE)) == "application/zip"
        assert detect_content_type(XML_CLEAN) == "text/xml"
        assert detect_content_type(b"\xef\xbb\xbf<dte/>") == "text/xml"


class TestStructuralChecks:
    def test_pdf_clean_passes(self) -> None:
        assert validate_bytes(PDF_CLEAN, "doc.pdf", "application/pdf", max_file_bytes=10_000) is None

    @pytest.mark.parametrize("payload", [PDF_WITH_JS, PDF_WITH_LAUNCH, PDF_WITH_OPENACTION])
    def test_pdf_active_content_rejected(self, payload: bytes) -> None:
        assert inspect_pdf(payload) is not None
        assert validate_bytes(payload, "doc.pdf", "application/pdf", max_file_bytes=10_000) is not None

    def test_xlsx_clean_passes(self) -> None:
        payload = make_zip(OOXML_BASE)
        assert validate_bytes(payload, "planilla.xlsx", "application/octet-stream", max_file_bytes=10_000) is None

    def test_xlsx_with_vba_project_rejected(self) -> None:
        assert inspect_ooxml(make_zip(OOXML_WITH_MACRO), ".xlsx") is not None

    def test_xlsx_with_macro_content_type_rejected(self) -> None:
        assert inspect_ooxml(make_zip(OOXML_MACRO_CONTENT_TYPE), ".xlsx") is not None

    def test_xlsx_with_external_link_rejected(self) -> None:
        assert inspect_ooxml(make_zip(OOXML_EXTERNAL_LINK), ".xlsx") is not None

    def test_xlsx_with_ole_object_rejected(self) -> None:
        assert inspect_ooxml(make_zip(OOXML_OLE), ".xlsx") is not None

    def test_zip_bomb_ratio_rejected(self) -> None:
        payload = make_zip({**OOXML_BASE, "big.bin": b"0" * 2_000_000})
        assert "ratio" in (inspect_ooxml(payload, ".xlsx") or "")

    def test_too_many_zip_entries_rejected(self) -> None:
        members = {**OOXML_BASE, **{f"f{i}.bin": b"x" for i in range(600)}}
        assert "entries" in (inspect_ooxml(make_zip(members), ".xlsx") or "")

    def test_xml_clean_passes(self) -> None:
        assert validate_bytes(XML_CLEAN, "dte.xml", "text/xml", max_file_bytes=10_000) is None

    def test_xml_xxe_rejected(self) -> None:
        assert inspect_xml(XML_XXE) is not None

    def test_xml_billion_laughs_rejected(self) -> None:
        assert inspect_xml(XML_BILLION_LAUGHS) is not None

    def test_xml_malformed_rejected(self) -> None:
        assert inspect_xml(b"<dte><unclosed>") is not None


class TestTransferDecoding:
    def test_base64_roundtrip(self) -> None:
        assert decode_transfer(b"aGVsbG8=\r\n", "base64") == b"hello"

    def test_invalid_base64_fails_closed(self) -> None:
        with pytest.raises(ValueError):
            decode_transfer(b"%%%not-base64%%%", "base64")

    def test_unknown_encoding_fails_closed(self) -> None:
        with pytest.raises(ValueError):
            decode_transfer(b"data", "x-uuencode")

    def test_identity_encodings(self) -> None:
        assert decode_transfer(b"data", "7bit") == b"data"
        assert decode_transfer(b"data", "") == b"data"

    def test_quoted_printable(self) -> None:
        assert decode_transfer(b"hola=20mundo", "quoted-printable") == b"hola mundo"
