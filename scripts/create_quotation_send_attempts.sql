-- Apply in staging first. No PDF, Base64, body or recipient data is stored.
begin;

create table if not exists public.quotation_send_attempts (
    attempt_id uuid primary key default gen_random_uuid(),
    account_id text not null,
    idempotency_key text not null,
    fingerprint text not null check (fingerprint ~ '^[0-9a-f]{64}$'),
    quotation_reference text not null,
    quotation_version text not null,
    sender text not null,
    state text not null check (state in (
        'pendiente', 'procesando', 'aceptado_por_smtp', 'fallido', 'resultado_desconocido'
    )),
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    expires_at timestamptz not null,
    lease_until timestamptz not null,
    duration_ms bigint,
    message_id text,
    smtp_queue_id text,
    error_code text,
    accepted_count integer not null default 0,
    refused_count integer not null default 0,
    duplicate_requests bigint not null default 0,
    unique (account_id, idempotency_key)
);
create index if not exists quotation_send_attempts_account_created
    on public.quotation_send_attempts (account_id, created_at);
create index if not exists quotation_send_attempts_expiry
    on public.quotation_send_attempts (expires_at);
alter table public.quotation_send_attempts enable row level security;
revoke all on public.quotation_send_attempts from public, anon, authenticated;
grant select, insert, update, delete on public.quotation_send_attempts to service_role;

create or replace function public.quotation_recover(p_account text)
returns void language sql set search_path = public, pg_temp as $$
    update public.quotation_send_attempts
    set state = case when state = 'pendiente' then 'fallido' else 'resultado_desconocido' end,
        error_code = case when state = 'pendiente' then 'expired_before_smtp' else 'worker_interrupted' end,
        duration_ms = greatest(0, extract(epoch from (clock_timestamp() - created_at)) * 1000)::bigint,
        updated_at = clock_timestamp()
    where account_id = p_account and state in ('pendiente', 'procesando')
        and lease_until < clock_timestamp();
$$;

create or replace function public.quotation_reserve(
    p_account text, p_key text, p_fingerprint text, p_reference text, p_version text,
    p_sender text, p_concurrency integer, p_per_minute integer,
    p_lease_seconds double precision, p_retention_days integer
) returns jsonb language plpgsql set search_path = public, pg_temp as $$
declare
    attempt public.quotation_send_attempts;
begin
    if p_concurrency < 1 or p_per_minute < 1 or p_lease_seconds <= 0 or p_retention_days < 1 then
        raise exception 'Invalid quotation limits';
    end if;
    -- Serializes reservation + quota checks across all workers and instances.
    perform pg_advisory_xact_lock(hashtextextended('quotation:' || p_account, 0));
    perform public.quotation_recover(p_account);
    select * into attempt from public.quotation_send_attempts
        where account_id = p_account and idempotency_key = p_key for update;
    if found then
        if attempt.fingerprint <> p_fingerprint then
            return jsonb_build_object('decision', 'conflict');
        end if;
        update public.quotation_send_attempts
            set duplicate_requests = duplicate_requests + 1
            where attempt_id = attempt.attempt_id returning * into attempt;
        return jsonb_build_object('decision', 'existing', 'attempt', to_jsonb(attempt));
    end if;
    if (select count(*) from public.quotation_send_attempts
        where account_id = p_account and state in ('pendiente', 'procesando')) >= p_concurrency
        or (select count(*) from public.quotation_send_attempts
            where account_id = p_account and created_at > clock_timestamp() - interval '1 minute') >= p_per_minute
    then
        return jsonb_build_object('decision', 'limited');
    end if;
    insert into public.quotation_send_attempts (
        account_id, idempotency_key, fingerprint, quotation_reference, quotation_version,
        sender, state, lease_until, expires_at
    ) values (
        p_account, p_key, p_fingerprint, p_reference, p_version, p_sender, 'pendiente',
        clock_timestamp() + make_interval(secs => p_lease_seconds),
        clock_timestamp() + make_interval(days => p_retention_days)
    ) returning * into attempt;
    return jsonb_build_object('decision', 'reserved', 'attempt', to_jsonb(attempt));
end;
$$;

create or replace function public.quotation_claim(p_account text, p_attempt_id uuid)
returns jsonb language plpgsql set search_path = public, pg_temp as $$
declare
    attempt public.quotation_send_attempts;
begin
    perform public.quotation_recover(p_account);
    update public.quotation_send_attempts
        set state = 'procesando', updated_at = clock_timestamp(),
            message_id = '<' || attempt_id::text || '@' || split_part(sender, '@', 2) || '>'
        where account_id = p_account and attempt_id = p_attempt_id
            and state = 'pendiente' and lease_until > clock_timestamp()
        returning * into attempt;
    if found then
        return jsonb_build_object('claimed', true, 'attempt', to_jsonb(attempt));
    end if;
    select * into strict attempt from public.quotation_send_attempts
        where account_id = p_account and attempt_id = p_attempt_id;
    return jsonb_build_object('claimed', false, 'attempt', to_jsonb(attempt));
end;
$$;

create or replace function public.quotation_finish(
    p_account text, p_attempt_id uuid, p_state text, p_message_id text, p_smtp_queue_id text,
    p_error_code text, p_accepted_count integer, p_refused_count integer, p_duration_ms bigint
) returns jsonb language plpgsql set search_path = public, pg_temp as $$
declare
    attempt public.quotation_send_attempts;
begin
    if p_state not in ('aceptado_por_smtp', 'fallido', 'resultado_desconocido') then
        raise exception 'Invalid terminal state';
    end if;
    update public.quotation_send_attempts
        set state = p_state, message_id = coalesce(p_message_id, message_id),
            smtp_queue_id = p_smtp_queue_id, error_code = p_error_code,
            accepted_count = p_accepted_count, refused_count = p_refused_count,
            duration_ms = p_duration_ms, updated_at = clock_timestamp()
        where account_id = p_account and attempt_id = p_attempt_id
            and (state = 'procesando' or (state = 'resultado_desconocido' and error_code = 'worker_interrupted'))
        returning * into attempt;
    if not found then
        select * into strict attempt from public.quotation_send_attempts
            where account_id = p_account and attempt_id = p_attempt_id;
    end if;
    return to_jsonb(attempt);
end;
$$;

create or replace function public.quotation_get(
    p_account text, p_attempt_id uuid default null, p_key text default null
) returns jsonb language plpgsql set search_path = public, pg_temp as $$
declare
    attempt public.quotation_send_attempts;
begin
    if (p_attempt_id is null) = (p_key is null) then
        raise exception 'Exactly one attempt identifier is required';
    end if;
    perform public.quotation_recover(p_account);
    select * into attempt from public.quotation_send_attempts
        where account_id = p_account and
            ((p_attempt_id is not null and attempt_id = p_attempt_id)
             or (p_key is not null and idempotency_key = p_key));
    return jsonb_build_object('attempt', case when found then to_jsonb(attempt) else null end);
end;
$$;

create or replace function public.quotation_metrics(p_account text)
returns jsonb language plpgsql set search_path = public, pg_temp as $$
begin
    perform public.quotation_recover(p_account);
    return (
        select jsonb_build_object(
            'scope', 'retained_attempts',
            'total', count(*),
            'failed', count(*) filter (where state = 'fallido'),
            'unknown', count(*) filter (where state = 'resultado_desconocido'),
            'smtp_accepted', count(*) filter (where state = 'aceptado_por_smtp'),
            'duplicates_avoided', coalesce(sum(duplicate_requests), 0)
        ) from public.quotation_send_attempts where account_id = p_account
    );
end;
$$;

-- Schedule nightly with pg_cron / an external trusted scheduler. Never resend.
create or replace function public.quotation_cleanup()
returns jsonb language plpgsql set search_path = public, pg_temp as $$
declare
    account text;
    deleted_count bigint;
begin
    for account in select distinct account_id from public.quotation_send_attempts
        where state in ('pendiente', 'procesando') and lease_until < clock_timestamp()
    loop
        perform public.quotation_recover(account);
    end loop;
    delete from public.quotation_send_attempts
        where expires_at < clock_timestamp() and state not in ('pendiente', 'procesando');
    get diagnostics deleted_count = row_count;
    return jsonb_build_object('deleted', deleted_count);
end;
$$;

revoke all on function public.quotation_recover(text) from public, anon, authenticated;
revoke all on function public.quotation_reserve(text,text,text,text,text,text,integer,integer,double precision,integer)
    from public, anon, authenticated;
revoke all on function public.quotation_claim(text,uuid) from public, anon, authenticated;
revoke all on function public.quotation_finish(text,uuid,text,text,text,text,integer,integer,bigint)
    from public, anon, authenticated;
revoke all on function public.quotation_get(text,uuid,text) from public, anon, authenticated;
revoke all on function public.quotation_metrics(text) from public, anon, authenticated;
revoke all on function public.quotation_cleanup() from public, anon, authenticated;
grant execute on function public.quotation_recover(text) to service_role;
grant execute on function public.quotation_reserve(text,text,text,text,text,text,integer,integer,double precision,integer)
    to service_role;
grant execute on function public.quotation_claim(text,uuid) to service_role;
grant execute on function public.quotation_finish(text,uuid,text,text,text,text,integer,integer,bigint) to service_role;
grant execute on function public.quotation_get(text,uuid,text) to service_role;
grant execute on function public.quotation_metrics(text) to service_role;
grant execute on function public.quotation_cleanup() to service_role;

commit;
