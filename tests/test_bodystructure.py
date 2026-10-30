import pytest

from mcp_email_server.bodystructure import (
    BodyStructureError,
    attachments_from_bodystructure,
    extract_bodystructure_value,
    parse_bodystructure,
)


def test_parse_nested_multipart_numbering_matches_rfc3501() -> None:
    value = (
        '(("text" "plain" NIL NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
        '(("text" "plain" NIL NIL NIL "7bit" 20 1 NIL NIL NIL NIL)'
        '("text" "html" NIL NIL NIL "7bit" 30 1 NIL NIL NIL NIL) "alternative" NIL NIL NIL NIL)'
        '("application" "pdf" ("name" "doc.pdf") NIL NIL "base64" 999 NIL ("attachment" ("filename" "doc.pdf")) NIL NIL)'
        ' "mixed" ("boundary" "b") NIL NIL NIL)'
    )

    root = parse_bodystructure(value)
    attachments = attachments_from_bodystructure(root)

    assert [item.part_id for item in attachments] == ["3"]
    assert attachments[0].filename == "doc.pdf"
    assert attachments[0].content_type == "application/pdf"
    assert attachments[0].size_bytes == 999
    assert attachments[0].inline is False


def test_text_part_with_filename_counts_as_attachment() -> None:
    value = (
        '("text" "csv" ("name" "data.csv") NIL NIL "base64" 55 2 NIL ("attachment" ("filename" "data.csv")) NIL NIL)'
    )
    root = parse_bodystructure(value)
    # A non-multipart root has no separately addressable attachments.
    assert attachments_from_bodystructure(root) == []

    wrapped = f'({value} "mixed" NIL NIL NIL NIL)'
    attachments = attachments_from_bodystructure(parse_bodystructure(wrapped))
    assert [item.part_id for item in attachments] == ["1"]
    assert attachments[0].filename == "data.csv"


def test_inline_image_with_content_id_is_flagged_inline() -> None:
    value = (
        '(("text" "html" NIL NIL NIL "7bit" 100 3 NIL NIL NIL NIL)'
        '("image" "jpeg" NIL "<img1>" NIL "base64" 5000 NIL ("inline" ("filename" "photo.jpg")) NIL NIL)'
        ' "related" NIL NIL NIL NIL)'
    )
    attachments = attachments_from_bodystructure(parse_bodystructure(value))

    assert len(attachments) == 1
    assert attachments[0].part_id == "2"
    assert attachments[0].inline is True
    assert attachments[0].content_id == "<img1>"


def test_small_image_without_disposition_is_treated_as_inline() -> None:
    value = (
        '(("text" "plain" NIL NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
        '("image" "gif" NIL NIL NIL "base64" 900 NIL NIL NIL NIL)'
        ' "mixed" NIL NIL NIL NIL)'
    )
    attachments = attachments_from_bodystructure(parse_bodystructure(value))

    assert attachments[0].inline is True


def test_quoted_string_escapes_and_nil_params() -> None:
    value = (
        '(("application" "octet-stream" ("name" "weird \\"name\\".bin") NIL NIL "base64" 42 NIL NIL NIL NIL)'
        ' "mixed" NIL NIL NIL NIL)'
    )
    attachments = attachments_from_bodystructure(parse_bodystructure(value))

    assert attachments[0].filename == 'weird "name".bin'


def test_message_rfc822_part_is_not_an_attachment() -> None:
    value = (
        '(("text" "plain" NIL NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
        '("message" "rfc822" NIL NIL NIL "7bit" 3000 NIL NIL NIL NIL)'
        ' "mixed" NIL NIL NIL NIL)'
    )
    attachments = attachments_from_bodystructure(parse_bodystructure(value))

    assert [item.part_id for item in attachments] == ["2"]
    assert attachments[0].content_type == "message/rfc822"


def test_extract_bodystructure_value_from_fetch_line() -> None:
    line = (
        '4 FETCH (UID 104 FLAGS (\\Seen) BODYSTRUCTURE '
        '(("text" "plain" NIL NIL NIL "7bit" 10 1 NIL NIL NIL NIL) "mixed" NIL NIL NIL NIL) '
        'BODY[HEADER] {230}'
    )
    value = extract_bodystructure_value(line)

    assert value == '(("text" "plain" NIL NIL NIL "7bit" 10 1 NIL NIL NIL NIL) "mixed" NIL NIL NIL NIL)'


def test_extract_bodystructure_value_returns_none_without_marker() -> None:
    assert extract_bodystructure_value('4 FETCH (UID 104 BODY[] {230}') is None


def test_parse_rejects_unbalanced_input() -> None:
    with pytest.raises(BodyStructureError):
        parse_bodystructure('(("text" "plain" NIL NIL NIL "7bit" 10 1')
