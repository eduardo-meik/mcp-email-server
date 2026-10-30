-- Inbound attachments: sender allowlist, metadata table and private bucket.
-- RLS is enabled without policies on purpose: only the service role (used by
-- the MCP server) may read or write these tables.

create table if not exists public.attachment_sender_policy (
    id          bigint generated always as identity primary key,
    mailbox_id  text not null,
    pattern     text not null,  -- exact email or @domain
    action      text not null check (action in ('allow', 'deny')),
    note        text,
    created_at  timestamptz not null default now()
);

create unique index if not exists attachment_sender_policy_unique
    on public.attachment_sender_policy (mailbox_id, pattern);

alter table public.attachment_sender_policy enable row level security;


create table if not exists public.email_attachments (
    id                     bigint generated always as identity primary key,
    mailbox_id             text not null,
    folder                 text not null,
    uid                    bigint not null,
    part_id                text not null,
    sha256                 text,
    filename               text,
    content_type_declared  text,
    content_type_detected  text,
    size_bytes             bigint,
    sender                 text,
    sender_trust           text,  -- trusted | untrusted | denied
    auth_result            text,  -- topmost Authentication-Results header
    scan_status            text not null check (scan_status in ('clean', 'rejected', 'pending_scan')),
    scan_signature         text,
    reject_reason          text,
    storage_path           text,
    extracted_text         text,
    injection_flags        jsonb not null default '[]'::jsonb,
    created_at             timestamptz not null default now(),
    expires_at             timestamptz
);

create unique index if not exists email_attachments_unique_part
    on public.email_attachments (mailbox_id, folder, uid, part_id);

create index if not exists email_attachments_sha256_idx
    on public.email_attachments (sha256);

create index if not exists email_attachments_expires_at_idx
    on public.email_attachments (expires_at);

alter table public.email_attachments enable row level security;


-- Private bucket with type and size limits enforced by Supabase Storage.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'email-attachments',
    'email-attachments',
    false,
    10485760,  -- 10 MiB
    array[
        'application/pdf',
        'application/xml',
        'text/xml',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/octet-stream'
    ]
)
on conflict (id) do nothing;

-- No storage.objects policies: only the service role can read/write objects.
