import asyncio
import base64
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import aiosmtplib
import httpx
import pytest
from fastapi.testclient import TestClient

from mcp_email_server.adapters.smtp import SmtpAdapter
from mcp_email_server.config import Settings
from mcp_email_server.quotation_api import PREFIX, QuotationAccessGuard
from mcp_email_server.quotations import QuotationSendRequest
from mcp_email_server.server import create_app
from mcp_email_server.services.quotations import QuotationService

QUOTER_TOKEN = "test-quoter-exclusive-credential-32-characters"
HEADERS = {"Authorization": f"Bearer {QUOTER_TOKEN}"}
PDF = b"%PDF-1.7\nOfficial quotation bytes \x00\xff\n%%EOF\n"


def settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "access_token": "admin-test-token",
        "poll_webhook_secret": "poll-test-token",
        "quoter_token": QUOTER_TOKEN,
        "quoter_account": "sales",
        "accounts_json": json.dumps([
            {"account_name": "sales", "smtp_from_address": "sales@example.com"},
            {"account_name": "other", "smtp_from_address": "other@example.com"},
        ]),
        "smtp_host": "smtp.example.com",
        "smtp_username": "sales@example.com",
        "smtp_password": "smtp-test-password",
        "supabase_url": "https://test.supabase.co",
        "supabase_service_role_key": "test-service-role",
        "hubspot_bcc_address": None,
    }
    values.update(overrides)
    return Settings(**values)


def payload(**overrides) -> dict:
    values = {
        "idempotency_key": "quote-123-v1-send-1",
        "quotation_reference": "quote-123",
        "quotation_version": "1",
        "to": ["buyer@example.com"],
        "subject": "Official quotation",
        "body_text": "The original official PDF is attached.",
        "attachments": [{
            "filename": "quotation.pdf",
            "content_type": "application/pdf",
            "content_base64": base64.b64encode(PDF).decode(),
        }],
    }
    values.update(overrides)
    return values


class MemoryDatabase:
    """Test double only; production reservations use PostgreSQL transactions."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.rows = {}
        self.calls = []
        self.fail_finish = False

    async def quotation_rpc(self, operation: str, parameters: dict) -> dict:
        self.calls.append((operation, deepcopy(parameters)))
        async with self.lock:
            if operation == "reserve":
                key = parameters["p_key"]
                if key in self.rows:
                    row = self.rows[key]
                    if row["fingerprint"] != parameters["p_fingerprint"]:
                        return {"decision": "conflict"}
                    row["duplicate_requests"] += 1
                    return {"decision": "existing", "attempt": deepcopy(row)}
                now = datetime.now(timezone.utc)
                row = {
                    "attempt_id": str(uuid4()), "idempotency_key": key,
                    "quotation_reference": parameters["p_reference"],
                    "quotation_version": parameters["p_version"],
                    "fingerprint": parameters["p_fingerprint"], "state": "pendiente",
                    "created_at": now.isoformat(), "updated_at": now.isoformat(),
                    "expires_at": (now + timedelta(days=90)).isoformat(), "duplicate_requests": 0,
                }
                self.rows[key] = row
                return {"decision": "reserved", "attempt": deepcopy(row)}
            row = next((
                row for row in self.rows.values()
                if row["attempt_id"] == parameters.get("p_attempt_id")
                or row["idempotency_key"] == parameters.get("p_key")
            ), None)
            if operation == "get":
                return {"attempt": deepcopy(row)}
            if operation == "claim":
                row["state"] = "procesando"
                row["message_id"] = f"<{row['attempt_id']}@example.com>"
                return {"claimed": True, "attempt": deepcopy(row)}
            if operation == "finish":
                if self.fail_finish:
                    raise httpx.ConnectError("sensitive provider diagnostics")
                for name in (
                    "state", "message_id", "smtp_queue_id", "error_code",
                    "accepted_count", "refused_count", "duration_ms"
                ):
                    row[name] = parameters[f"p_{name}"]
                return deepcopy(row)
            if operation == "metrics":
                return {"total": len(self.rows)}
            raise AssertionError(operation)


class SmtpClient:
    def __init__(self, fail_at=None, failure=None, refused=None, delay=0, response=None) -> None:
        self.fail_at = fail_at
        self.failure = failure or TimeoutError("sensitive SMTP diagnostics")
        self.refused = refused or {}
        self.delay = delay
        self.response = response or "250 queued; sensitive text must not be persisted"
        self.messages = []
        self.closed = False
        self.started = asyncio.Event()
        self.release = None

    async def step(self, name):
        if self.fail_at == name:
            raise self.failure

    async def connect(self):
        await self.step("connect")

    async def starttls(self):
        await self.step("starttls")

    async def login(self, username, password):
        await self.step("login")

    async def send_message(self, message, **kwargs):
        self.messages.append((message, kwargs))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        await asyncio.sleep(self.delay)
        await self.step("send")
        return self.refused, self.response

    def close(self):
        self.closed = True


def app_with_fakes(config=None, smtp=None, database=None):
    config = config or settings()
    app = create_app(config)
    smtp = smtp or SmtpClient()
    database = database or MemoryDatabase()
    current = app.state.quotation_service
    app.state.quotation_service = QuotationService(
        current.settings, database, SmtpAdapter(current.settings, client_factory=lambda **_kwargs: smtp)
    )
    return app, smtp, database


def test_http_send_preserves_pdf_and_only_persists_metadata(caplog):
    app, smtp, database = app_with_fakes()
    with TestClient(app) as client:
        sent = client.post(PREFIX, headers=HEADERS, json=payload())
        assert sent.status_code == 200
        result = sent.json()
        assert result["state"] == "aceptado_por_smtp"
        assert result["delivery_confirmed"] is False
        assert result["accepted_count"] == 1
        assert result["duration_ms"] >= 0
        queried = client.get(PREFIX + "/" + result["attempt_id"], headers=HEADERS)
        assert queried.json() == result
        by_key = client.get(PREFIX + "/by-key", headers={**HEADERS, "Idempotency-Key": payload()["idempotency_key"]})
        assert by_key.json() == result
        assert client.get(PREFIX + "/" + str(uuid4()), headers=HEADERS).status_code == 404
    mime, envelope = smtp.messages[0]
    attachments = list(mime.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_payload(decode=True) == PDF
    assert attachments[0].get_filename() == "quotation.pdf"
    assert attachments[0].get_content_type() == "application/pdf"
    assert mime["From"] == "sales@example.com"
    assert str(mime["Message-ID"]) == result["message_id"]
    assert envelope["sender"] == "sales@example.com"
    assert smtp.closed
    persisted = json.dumps(database.calls)
    for private in (payload()["attachments"][0]["content_base64"], payload()["body_text"], "buyer@example.com"):
        assert private not in persisted
        assert private not in caplog.text
        assert private not in sent.text
    assert "sensitive" not in persisted


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Bearer admin-test-token", "Basic abc"])
def test_quote_api_rejects_missing_invalid_or_other_credentials(authorization):
    app, smtp, database = app_with_fakes()
    with TestClient(app) as client:
        headers = {} if authorization is None else {"Authorization": authorization}
        assert client.post(PREFIX, headers=headers, json=payload()).status_code == 401
        assert client.get(PREFIX + "/by-key", headers=headers).status_code == 401
    assert smtp.messages == []
    assert database.calls == []


def test_quoter_cannot_access_mailbox_operations_or_admin_metrics():
    app, smtp, database = app_with_fakes()
    with TestClient(app) as client:
        for tool in ("send_email", "get_emails_content", "search_emails", "delete_emails", "list_available_accounts"):
            result = client.post("/mcp", headers=HEADERS, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool, "arguments": {"account_name": "other"}},
            })
            assert result.status_code == 401
        assert client.post("/tasks/poll", headers=HEADERS).status_code == 401
        assert client.get("/internal/quotation-metrics", headers=HEADERS).status_code == 401
        assert client.get(
            "/internal/quotation-metrics", headers={"Authorization": "Bearer admin-test-token"}
        ).json() == {"total": 0}
    assert not smtp.messages


@pytest.mark.parametrize("change", [
    {"account_name": "other"}, {"from": "other@example.com"}, {"pdf_url": "https://example.com/a.pdf"},
    {"to": ["bad@example.com\r\nBcc: other@example.com"]}, {"to": ["Name <buyer@example.com>"]},
    {"to": []}, {"subject": "test\r\nBcc: other@example.com"}, {"subject": "\x00"},
    {"body_text": " "}, {"quotation_reference": "../secret"},
])
def test_contract_rejects_extra_sources_account_selection_and_invalid_headers(change):
    app, smtp, database = app_with_fakes()
    with TestClient(app) as client:
        result = client.post(PREFIX, headers=HEADERS, json=payload(**change))
    assert result.status_code == 422
    assert result.json() == {"detail": "invalid_quotation_request"}
    assert not database.calls
    assert not smtp.messages


@pytest.mark.parametrize("attachment_change", [
    {"filename": "../quotation.pdf"}, {"filename": "bad\r\n.pdf"}, {"filename": "quote.exe"},
    {"content_type": "image/png"}, {"content_base64": "not-base64!"},
    {"content_base64": base64.b64encode(b"not a PDF").decode()},
    {"content_base64": base64.b64encode(b"%PDF-1.7\ntruncated").decode()},
    {"content_text": "generate a replacement PDF"}, {"path": "C:\\private\\quote.pdf"},
    {"url": "https://example.com/quote.pdf"},
])
def test_contract_rejects_invalid_attachment_without_echoing_content(attachment_change):
    declaration = payload()["attachments"][0] | attachment_change
    app, smtp, database = app_with_fakes()
    with TestClient(app) as client:
        result = client.post(PREFIX, headers=HEADERS, json=payload(attachments=[declaration]))
    assert result.status_code == 422
    assert result.json() == {"detail": "invalid_quotation_request"}
    assert not smtp.messages
    assert not database.calls


@pytest.mark.parametrize("overrides,change", [
    ({"quotation_max_file_bytes": len(PDF) - 1}, {}),
    ({"quotation_max_total_bytes": len(PDF) - 1}, {}),
    ({}, {"attachments": payload()["attachments"] * 2}),
    ({"quotation_max_recipients": 1}, {"cc": ["second@example.com"]}),
    ({"quotation_max_recipients": 1, "hubspot_bcc_address": "crm@example.com"}, {}),
    ({"quotation_max_attachments": 2, "quotation_max_total_bytes": len(PDF) * 2 - 1},
     {"attachments": payload()["attachments"] * 2}),
])
def test_exact_limits(overrides, change):
    app, smtp, database = app_with_fakes(settings(**overrides))
    with TestClient(app) as client:
        result = client.post(PREFIX, headers=HEADERS, json=payload(**change))
    assert result.status_code == 422
    assert not database.calls
    assert not smtp.messages


def test_exact_file_and_aggregate_boundary_is_accepted():
    app, _, _ = app_with_fakes(settings(
        quotation_max_file_bytes=len(PDF), quotation_max_total_bytes=len(PDF), quotation_max_recipients=1
    ))
    with TestClient(app) as client:
        assert client.post(PREFIX, headers=HEADERS, json=payload()).status_code == 200


@pytest.mark.parametrize("change", [
    {"quotation_version": "2"},
    {"quotation_reference": "other-quotation"},
    {"body_text": "Changed body"},
    {"to": ["another@example.com"]},
    {"attachments": [{
        "filename": "quotation.pdf",
        "content_base64": base64.b64encode(b"%PDF-1.7\nChanged official PDF\n%%EOF\n").decode(),
    }]},
])
def test_idempotency_fingerprint_covers_pdf_version_reference_body_and_recipients(change):
    app, smtp, _ = app_with_fakes()
    with TestClient(app) as client:
        assert client.post(PREFIX, headers=HEADERS, json=payload()).status_code == 200
        assert client.post(PREFIX, headers=HEADERS, json=payload(**change)).status_code == 409
    assert len(smtp.messages) == 1


@pytest.mark.asyncio
async def test_simultaneous_duplicates_conflicts_and_deliberate_resend():
    smtp = SmtpClient()
    smtp.release = asyncio.Event()
    app, smtp, database = app_with_fakes(smtp=smtp)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.post(PREFIX, headers=HEADERS, json=payload()))
        await asyncio.wait_for(smtp.started.wait(), timeout=5)
        duplicate = await client.post(PREFIX, headers=HEADERS, json=payload())
        assert duplicate.status_code == 202
        assert duplicate.json()["state"] == "procesando"
        mismatch = await client.post(PREFIX, headers=HEADERS, json=payload(subject="Changed"))
        assert mismatch.status_code == 409
        smtp.release.set()
        accepted = await first
        replay = await client.post(PREFIX, headers=HEADERS, json=payload())
        assert replay.json()["attempt_id"] == accepted.json()["attempt_id"] == duplicate.json()["attempt_id"]
        assert replay.json()["state"] == "aceptado_por_smtp"
        assert len(smtp.messages) == 1
        deliberate = await client.post(PREFIX, headers=HEADERS, json=payload(idempotency_key="new-key"))
        assert deliberate.json()["attempt_id"] != accepted.json()["attempt_id"]
        assert len(smtp.messages) == 2
        # A rebuilt stateless service still consults the same persistent repository.
        original = app.state.quotation_service
        app.state.quotation_service = QuotationService(original.settings, database, original.smtp)
        restarted = await client.post(PREFIX, headers=HEADERS, json=payload())
        assert restarted.json()["attempt_id"] == accepted.json()["attempt_id"]
        assert len(smtp.messages) == 2


@pytest.mark.parametrize("phase,failure,expected", [
    ("connect", TimeoutError("secret"), "fallido"),
    ("login", aiosmtplib.SMTPAuthenticationError(535, "secret"), "fallido"),
    ("send", TimeoutError("secret"), "resultado_desconocido"),
    ("send", aiosmtplib.SMTPServerDisconnected("secret"), "resultado_desconocido"),
    ("send", aiosmtplib.SMTPDataError(554, "secret"), "fallido"),
    ("send", aiosmtplib.SMTPRecipientsRefused([]), "fallido"),
])
def test_smtp_failures_are_sanitized_and_never_automatically_retried(phase, failure, expected, caplog):
    smtp = SmtpClient(fail_at=phase, failure=failure)
    app, smtp, database = app_with_fakes(smtp=smtp)
    with TestClient(app) as client:
        result = client.post(PREFIX, headers=HEADERS, json=payload())
        assert result.json()["state"] == expected
        replay = client.post(PREFIX, headers=HEADERS, json=payload())
        assert replay.json()["attempt_id"] == result.json()["attempt_id"]
        assert replay.json()["state"] == expected
    assert len(smtp.messages) == (1 if phase == "send" else 0)
    assert "secret" not in result.text
    assert "secret" not in caplog.text
    assert "secret" not in json.dumps(database.calls)


def test_smtp_total_timeout_is_unknown_after_submission():
    app, smtp, _ = app_with_fakes(
        settings(quotation_timeout_seconds=0.01), smtp=SmtpClient(delay=0.1)
    )
    with TestClient(app) as client:
        result = client.post(PREFIX, headers=HEADERS, json=payload())
    assert result.json()["state"] == "resultado_desconocido"
    assert smtp.closed


def test_smtp_partial_acceptance_is_explicit():
    app, _, _ = app_with_fakes(smtp=SmtpClient(refused={"other@example.com": (550, "rejected")}))
    with TestClient(app) as client:
        result = client.post(PREFIX, headers=HEADERS, json=payload(cc=["other@example.com"]))
    assert result.json()["state"] == "aceptado_por_smtp"
    assert result.json()["accepted_count"] == 1
    assert result.json()["refused_count"] == 1
    assert result.json()["error_code"] == "smtp_partial_rejection"
    assert result.json()["delivery_confirmed"] is False


def test_smtp_queue_reference_is_recorded_without_raw_provider_response():
    app, _, database = app_with_fakes(smtp=SmtpClient(response="250 2.0.0 Ok: queued as ABC123"))
    with TestClient(app) as client:
        result = client.post(PREFIX, headers=HEADERS, json=payload())
    assert result.json()["smtp_queue_id"] == "ABC123"
    assert "250 2.0.0" not in json.dumps(database.calls)


def test_persistence_failure_after_acceptance_never_returns_success():
    database = MemoryDatabase()
    database.fail_finish = True
    app, smtp, _ = app_with_fakes(database=database)
    with TestClient(app) as client:
        result = client.post(PREFIX, headers=HEADERS, json=payload())
        assert result.status_code == 503
        assert result.json()["detail"]["error_code"] == "result_not_recorded"
        assert result.json()["detail"]["attempt_id"]
        replay = client.post(PREFIX, headers=HEADERS, json=payload())
        assert replay.status_code == 202
        assert replay.json()["state"] == "procesando"
    assert len(smtp.messages) == 1


@pytest.mark.asyncio
async def test_cancellation_records_unknown_without_resending():
    smtp = SmtpClient()
    smtp.release = asyncio.Event()
    app, smtp, database = app_with_fakes(smtp=smtp)
    service = app.state.quotation_service
    request = QuotationSendRequest.model_validate(payload())
    message, fingerprint = request.prepare(service.settings)
    task = asyncio.create_task(service.send(request, message, fingerprint))
    await asyncio.wait_for(smtp.started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert next(iter(database.rows.values()))["state"] == "resultado_desconocido"
    assert smtp.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("declared,authorized,expected,reads", [
    (100, True, 413, 0), (None, True, 413, 2), (100, False, 401, 0),
    pytest.param("9" * 5000, True, 413, 0, id="oversized-length-header"),
    ("invalid", True, 413, 0),
])
async def test_body_limit_is_checked_before_parsing_and_before_reading_declared_oversize(
    declared, authorized, expected, reads
):
    calls = []
    received = 0

    async def downstream(*_args):
        raise AssertionError("Oversized requests must not reach FastAPI")

    async def receive():
        nonlocal received
        received += 1
        return {"type": "http.request", "body": b"x" * 6, "more_body": True}

    async def send(message):
        calls.append(message)

    headers = [(b"authorization", f"Bearer {QUOTER_TOKEN}".encode())] if authorized else []
    if declared is not None:
        headers.append((b"content-length", str(declared).encode()))
    guard = QuotationAccessGuard(downstream, QUOTER_TOKEN, max_bytes=10, max_concurrency=1)
    await guard({"type": "http", "path": PREFIX, "method": "POST", "headers": headers}, receive, send)
    assert calls[0]["status"] == expected
    assert received == reads


@pytest.mark.asyncio
async def test_upload_total_timeout_releases_capacity():
    responses = []

    async def downstream(*_args):
        raise AssertionError("Incomplete uploads must not reach FastAPI")

    async def receive():
        await asyncio.sleep(0.1)
        return {"type": "http.request", "body": b"x", "more_body": True}

    async def send(message):
        responses.append(message)

    guard = QuotationAccessGuard(downstream, QUOTER_TOKEN, 100, 1, upload_timeout=0.01)
    await guard({
        "type": "http", "path": PREFIX, "method": "POST",
        "headers": [(b"authorization", f"Bearer {QUOTER_TOKEN}".encode())],
    }, receive, send)
    assert responses[0]["status"] == 408
    assert guard.active == 0


@pytest.mark.asyncio
async def test_local_upload_concurrency_rejects_excess_without_processing():
    smtp = SmtpClient()
    smtp.release = asyncio.Event()
    app, smtp, database = app_with_fakes(settings(quotation_concurrency=1), smtp=smtp)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.post(PREFIX, headers=HEADERS, json=payload()))
        await asyncio.wait_for(smtp.started.wait(), timeout=5)
        limited = await client.post(PREFIX, headers=HEADERS, json=payload(idempotency_key="second"))
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "1"
        smtp.release.set()
        assert (await first).status_code == 200
    assert len([operation for operation, _ in database.calls if operation == "reserve"]) == 1
    assert len(smtp.messages) == 1


def test_database_outage_before_reservation_does_not_send_or_leak_provider_errors(caplog):
    class UnavailableDatabase:
        async def quotation_rpc(self, operation, parameters):
            raise httpx.ConnectError("sensitive-database-diagnostics")

    app, smtp, _ = app_with_fakes(database=UnavailableDatabase())
    with TestClient(app) as client:
        result = client.post(PREFIX, headers=HEADERS, json=payload())
    assert result.status_code == 503
    assert not smtp.messages
    assert "sensitive-database-diagnostics" not in result.text
    assert "sensitive-database-diagnostics" not in caplog.text


@pytest.mark.parametrize("overrides", [
    {"quoter_account": None}, {"quoter_account": "missing"},
    {"quoter_token": "replace-me"}, {"quoter_token": "short"},
    {"access_token": QUOTER_TOKEN}, {"poll_webhook_secret": QUOTER_TOKEN},
    {"smtp_starttls": False}, {"supabase_url": None},
])
def test_invalid_quoter_configuration_fails_closed(overrides):
    with pytest.raises(ValueError):
        create_app(settings(**overrides))
