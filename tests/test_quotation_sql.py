"""Opt-in integration tests against an isolated, disposable PostgreSQL container."""

import asyncio
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from mcp_email_server.adapters.supabase import SupabaseAdapter
from mcp_email_server.config import Settings


@pytest.fixture(scope="module")
def postgres():
    container = os.environ.get("MCP_EMAIL_TEST_POSTGRES_CONTAINER")
    if not container:
        pytest.skip("Set MCP_EMAIL_TEST_POSTGRES_CONTAINER to a disposable PostgreSQL test container")
    database = "quotation_test_" + uuid4().hex

    def execute(sql, db=database, check=True):
        return subprocess.run(
            ["docker", "exec", "-i", container, "psql", "-X", "-v", "ON_ERROR_STOP=1",
             "-At", "-U", "postgres", "-d", db],
            input=sql, text=True, capture_output=True, check=check, timeout=30,
        )

    execute(f"create database {database};", db="postgres")
    try:
        execute("""
            do $$
            begin
                if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon; end if;
                if not exists (select 1 from pg_roles where rolname = 'authenticated') then
                    create role authenticated;
                end if;
                if not exists (select 1 from pg_roles where rolname = 'service_role') then
                    create role service_role bypassrls;
                end if;
            end $$;
        """)
        migration = (Path(__file__).parents[1] / "scripts" / "create_quotation_send_attempts.sql").read_text()
        execute(migration)
        execute(migration)
        yield execute
    finally:
        execute(f"drop database {database} with (force);", db="postgres")


def sql_value(value):
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def rpc(postgres, operation, **parameters):
    args = ",".join(f"{key} => {sql_value(value)}" for key, value in parameters.items())
    output = postgres(f"set role service_role; select public.quotation_{operation}({args});").stdout
    return json.loads(output.splitlines()[-1])


def reserve(postgres, account, key="key-1", fingerprint="a" * 64, concurrency=20, per_minute=100):
    return rpc(
        postgres, "reserve", p_account=account, p_key=key, p_fingerprint=fingerprint,
        p_reference="quote-123", p_version="1", p_sender="sales@example.com",
        p_concurrency=concurrency, p_per_minute=per_minute, p_lease_seconds=210,
        p_retention_days=90,
    )


def get(postgres, account, key):
    return rpc(postgres, "get", p_account=account, p_attempt_id=None, p_key=key)["attempt"]


def test_real_sql_atomic_duplicate_reservation_and_content_conflict(postgres):
    account = uuid4().hex
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: reserve(postgres, account), range(12)))
    assert sum(result["decision"] == "reserved" for result in results) == 1
    assert sum(result["decision"] == "existing" for result in results) == 11
    assert len({result["attempt"]["attempt_id"] for result in results}) == 1
    assert get(postgres, account, "key-1")["duplicate_requests"] == 11
    assert reserve(postgres, account, fingerprint="b" * 64)["decision"] == "conflict"
    assert reserve(postgres, account, key="deliberate-resend")["decision"] == "reserved"
    metrics = rpc(postgres, "metrics", p_account=account)
    assert metrics["duplicates_avoided"] == 11


def test_real_sql_global_concurrency_and_frequency_limits(postgres):
    account = uuid4().hex
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(
            lambda i: reserve(postgres, account, key=f"send-{i}", concurrency=1), range(6)
        ))
    assert [result["decision"] for result in results].count("reserved") == 1
    assert [result["decision"] for result in results].count("limited") == 5
    account = uuid4().hex
    assert reserve(postgres, account, per_minute=1)["decision"] == "reserved"
    assert reserve(postgres, account, key="second", per_minute=1)["decision"] == "limited"
    assert reserve(postgres, account, per_minute=1)["decision"] == "existing"


def test_real_sql_claim_is_atomic_and_recovery_never_resends(postgres):
    account = uuid4().hex
    attempt = reserve(postgres, account)["attempt"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(
            lambda _: rpc(postgres, "claim", p_account=account, p_attempt_id=attempt["attempt_id"]), range(4)
        ))
    assert sum(claim["claimed"] for claim in claims) == 1
    postgres(
        "update public.quotation_send_attempts set lease_until = clock_timestamp() - interval '1 second' "
        f"where attempt_id = {sql_value(attempt['attempt_id'])};"
    )
    recovered = get(postgres, account, "key-1")
    assert recovered["state"] == "resultado_desconocido"
    assert recovered["message_id"] == f"<{attempt['attempt_id']}@example.com>"
    assert recovered["error_code"] == "worker_interrupted"
    assert reserve(postgres, account)["decision"] == "existing"
    assert rpc(postgres, "metrics", p_account=account)["unknown"] == 1
    assert get(postgres, "another-account", "key-1") is None


def test_real_sql_pending_expiry_terminal_results_and_retention(postgres):
    account = uuid4().hex
    attempt = reserve(postgres, account)["attempt"]
    postgres(
        "update public.quotation_send_attempts set lease_until = clock_timestamp() - interval '1 second' "
        f"where attempt_id = {sql_value(attempt['attempt_id'])};"
    )
    assert get(postgres, account, "key-1")["state"] == "fallido"
    assert not rpc(postgres, "claim", p_account=account, p_attempt_id=attempt["attempt_id"])["claimed"]
    other = reserve(postgres, account, key="accepted")["attempt"]
    rpc(postgres, "claim", p_account=account, p_attempt_id=other["attempt_id"])
    result = rpc(
        postgres, "finish", p_account=account, p_attempt_id=other["attempt_id"],
        p_state="aceptado_por_smtp", p_message_id="<test@example.com>", p_error_code=None,
        p_smtp_queue_id="QUEUE123",
        p_accepted_count=1, p_refused_count=0, p_duration_ms=50,
    )
    assert result["state"] == "aceptado_por_smtp"
    assert reserve(postgres, account, key="accepted")["attempt"]["message_id"] == "<test@example.com>"
    postgres(
        "update public.quotation_send_attempts set expires_at = clock_timestamp() - interval '1 second' "
        f"where attempt_id = {sql_value(attempt['attempt_id'])};"
    )
    assert rpc(postgres, "cleanup")["deleted"] == 1
    assert get(postgres, account, "key-1") is None
    assert get(postgres, account, "accepted")["state"] == "aceptado_por_smtp"


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_real_sql_does_not_grant_untrusted_roles_table_or_rpc_access(postgres, role):
    denied = postgres(
        f"set role {role}; select * from public.quotation_send_attempts;", check=False
    )
    assert denied.returncode != 0
    assert "permission denied" in denied.stderr
    denied_rpc = postgres(
        f"set role {role}; select public.quotation_metrics('sales');", check=False
    )
    assert denied_rpc.returncode != 0
    assert "permission denied" in denied_rpc.stderr


@pytest.mark.asyncio
async def test_supabase_adapter_rpc_wire_contract():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"decision": "limited"})

    adapter = SupabaseAdapter(Settings(
        _env_file=None, supabase_url="https://test.supabase.co",
        supabase_service_role_key="test-role",
    ), transport=httpx.MockTransport(respond))
    assert await adapter.quotation_rpc("reserve", {"p_account": "sales"}) == {"decision": "limited"}
    assert requests[0].url.path == "/rest/v1/rpc/quotation_reserve"
    assert requests[0].headers["Authorization"] == "Bearer test-role"
    assert json.loads(requests[0].content) == {"p_account": "sales"}
    with pytest.raises(ValueError):
        await adapter.quotation_rpc("arbitrary_function", {})


@pytest.mark.asyncio
async def test_real_supabase_adapter_to_sql_reservation(postgres):
    """Run the actual HTTP adapter through an in-process PostgREST test bridge."""
    account = uuid4().hex

    async def respond(request):
        operation = request.url.path.removeprefix("/rest/v1/rpc/quotation_")
        result = await asyncio.to_thread(rpc, postgres, operation, **json.loads(request.content))
        return httpx.Response(200, json=result)

    adapter = SupabaseAdapter(Settings(
        _env_file=None, supabase_url="https://test.supabase.co", supabase_service_role_key="test-role",
    ), transport=httpx.MockTransport(respond))
    parameters = {
        "p_account": account, "p_key": "wire-key", "p_fingerprint": "c" * 64,
        "p_reference": "quote-123", "p_version": "1", "p_sender": "sales@example.com",
        "p_concurrency": 2, "p_per_minute": 10, "p_lease_seconds": 210, "p_retention_days": 90,
    }
    results = await asyncio.gather(*[adapter.quotation_rpc("reserve", parameters) for _ in range(4)])
    assert sum(result["decision"] == "reserved" for result in results) == 1
    assert get(postgres, account, "wire-key")["duplicate_requests"] == 3


@pytest.mark.asyncio
async def test_http_to_real_sql_to_smtp_end_to_end(postgres):
    from test_quotations import HEADERS, PDF, PREFIX, app_with_fakes, payload

    async def respond(request):
        operation = request.url.path.removeprefix("/rest/v1/rpc/quotation_")
        result = await asyncio.to_thread(rpc, postgres, operation, **json.loads(request.content))
        return httpx.Response(200, json=result)

    app, smtp, _ = app_with_fakes()
    service = app.state.quotation_service
    service.database = SupabaseAdapter(service.settings, transport=httpx.MockTransport(respond))
    key = "e2e-" + uuid4().hex
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        sent = await client.post(PREFIX, headers=HEADERS, json=payload(idempotency_key=key))
        replay = await client.post(PREFIX, headers=HEADERS, json=payload(idempotency_key=key))
        queried = await client.get(PREFIX + "/" + sent.json()["attempt_id"], headers=HEADERS)
    assert sent.status_code == replay.status_code == queried.status_code == 200
    assert queried.json()["state"] == "aceptado_por_smtp"
    assert sent.json()["attempt_id"] == replay.json()["attempt_id"]
    assert len(smtp.messages) == 1
    assert list(smtp.messages[0][0].iter_attachments())[0].get_payload(decode=True) == PDF
    stored = get(postgres, service.account, key)
    assert stored["duplicate_requests"] == 1
    assert "content_base64" not in stored
    assert "body_text" not in stored
    assert "attachments" not in stored
