---
name: mcp-email-server-2
description: "Use this skill when Claude needs to operate the MCP Email Server for inbox sync, mailbox browsing, email reading, thread inspection, search, flag changes, moving or deleting emails, outbound replies or sends, file attachments, account-specific reply personas, or stored historical email context in Supabase. Also use when the user mentions email MCP, inbox sync, unread emails, mailboxes, search emails, read email content, reply to an email, send email, attach a file, attachment, attachments, archivo adjunto, adjunta este archivo al email, mark seen, flag email, move email, delete email, get thread, get account profile, account_name, mailbox, persona, tone, signature, or Supabase-backed email memory."
---

# MCP Email Server Skill

## Purpose

Use this skill when Claude needs to work through the deployed MCP email server instead of reasoning abstractly about email state.

This MCP server is a stateless email operations backend that:

1. fetches unread emails from IMAP
2. generates embeddings through OpenRouter
3. persists email content and sync state in Supabase
4. exposes mailbox browsing, search, threading, message actions, and outbound send tools through MCP

Claude should prefer MCP tools from this server for real inbox operations, not simulated advice, whenever the user wants actual mailbox work.

## Service Facts

- Runtime model: stateless service
- Persistence: Supabase only
- IMAP adapter: `aioimaplib`
- SMTP adapter: `aiosmtplib`
- MCP framework: `FastMCP`
- Web framework: `FastAPI`
- Production MCP URL: `https://mcp-email-server-y2eagv4zlq-uc.a.run.app/mcp` plus the request header `Authorization: Bearer <MCP_EMAIL_ACCESS_TOKEN>` (preferred: the token stays out of the URL)
- Tokens in paths or query strings are not supported. Clients must send a bearer header.
- The token is a secret kept in Secret Manager and the local `.env`; never write it into this file
- Every MCP request, including `initialize`, `ping` and `tools/list`, requires the token.
- Local MCP URL: `http://localhost:8000/mcp` (requires `MCP_EMAIL_ACCESS_TOKEN` too)
- Local health endpoint: `http://localhost:8000/healthz`
- Local poll endpoint: `http://localhost:8000/tasks/poll`

Only bearer authentication is supported, with or without a trailing slash.
Official quotation PDFs must travel directly from `mcp_quoter` to the restricted
HTTP contract in [docs/quotation-delivery.md](docs/quotation-delivery.md).
Claude must not transport their Base64 or regenerate the official PDF.

## When Claude Should Use This Skill

Use this skill when the user asks Claude to:

- review unread emails
- sync the inbox
- inspect configured mailboxes
- search emails by content
- read specific email bodies
- inspect an email thread
- load stored historical email context before replying
- adapt the tone or persona to the mailbox account in use
- send a new email
- send an email with a file attachment
- reply to an email
- mark emails seen, flagged, or answered
- move emails between folders
- delete emails
- work with multiple configured accounts through `account_name`
- diagnose whether the email MCP server is ready

Typical user phrases include:

- "revisa mi inbox"
- "sincroniza correos no leidos"
- "busca un correo sobre facturas"
- "lee este email"
- "muévelo a Archive"
- "márcalo como leído"
- "respóndele"
- "envía un correo"
- "adjunta este archivo al email"
- "envía este PDF adjunto"
- "manda este correo con archivo adjunto"
- "qué cuentas están disponibles"
- "mira el thread completo"

## High-Level Operating Rules For Claude

Claude should follow these rules when using this MCP:

1. Prefer server tools over speculation. If the user wants actual mailbox state, query the MCP.
2. If the account is unknown and the task could span multiple accounts, call `list_available_accounts` first.
3. If the task depends on fresh unread ingestion, call `get_system_status` and then `sync_unread_emails` before browsing results.
4. If the user uses relative dates like "hoy", "ayer", "esta semana", call `get_current_datetime` before building date filters.
5. For reading, prefer `list_emails_metadata` first, then `get_emails_content` only for the selected email ids.
6. Before drafting or sending a reply, call `get_account_profile` for the selected account and follow its persona, signature, and reply guidance.
7. Before drafting a reply, call `get_stored_email_context` to review persisted historical context from Supabase whenever a `reply_email_id` or subject is available.
8. For replies, prefer `reply_email_id` in `send_email` so threading headers are auto-filled correctly.
9. Use IMAP thread context and Supabase stored context together. IMAP provides the live thread surface; Supabase provides persisted historical context.
10. Treat `status = blocked` as a configuration issue, not a transient retry case.
11. Treat `status = error` as a runtime or integration failure and surface the `details` field clearly.
12. Do not assume local memory, local files, or SQLite persistence. This server is stateless.
13. Do not invent mailbox names, account names, email ids, persona rules, or signatures. Resolve them from MCP output first.
14. If the user asks to attach a file, use `send_email.attachments`. For any document you author yourself (reports, invoices, quotes, letters), pass `content_text` with `filename` and `content_type`; when `content_type` is `application/pdf` the server renders the PDF. Use `content_base64` only for an existing file whose bytes you already hold.

## Recommended Workflow Patterns

### 1. Runtime readiness

Use:

- `get_system_status`

Use when:

- the user says the server is failing
- Claude needs to confirm configuration state before syncing
- the deployment may be incomplete

Interpretation:

- `ok`: runtime configuration is ready for the selected account or overall service
- `degraded`: missing configuration exists

### 2. Inbox synchronization

Use:

- `get_system_status`
- `sync_unread_emails`

Recommended sequence:

1. call `get_system_status`
2. if status is acceptable, call `sync_unread_emails`
3. report `fetched`, `embedded`, `persisted`, and `last_uid`

### 3. Account discovery

Use:

- `list_available_accounts`
- `get_account_profile`

Use when:

- the deployment supports multiple accounts
- the user names an account ambiguously
- Claude needs the exact `account_name`
- Claude needs account-specific persona, tone, or signature guidance

Recommended sequence:

1. call `list_available_accounts` if the account is unknown
2. call `get_account_profile` before drafting a new outbound message or reply

### 4. Mailbox browsing

Use:

- `list_mailboxes`
- `list_emails_metadata`
- `get_emails_content`

Recommended sequence:

1. call `list_mailboxes` if the mailbox name is unknown
2. call `list_emails_metadata` with the chosen mailbox and filters
3. call `get_emails_content` only for the selected ids the user actually wants to open

### 5. Search

Use:

- `search_emails`

Use when:

- the user wants full-text search in the selected mailbox
- the query is a phrase or keyword search, not just metadata filtering

If the user wants structured filtering by sender, subject, date, or seen state, prefer `list_emails_metadata`.

### 6. Thread inspection

Use:

- `get_thread`
- `get_stored_email_context`

Use when:

- the user asks for the entire conversation
- Claude needs thread context before drafting a reply
- Claude needs persisted historical context from Supabase before replying

Recommended sequence:

1. call `get_thread` for live IMAP thread context
2. call `get_stored_email_context` with `reply_email_id` when available
3. use both surfaces when drafting the reply

### 7. Outbound send and replies

Use:

- `get_account_profile`
- `get_stored_email_context`
- `send_email`

For a new email:

- call `get_account_profile` first
- pass `to`, `subject`, and `body_text`
- optionally pass `cc`, `bcc`, `body_html`, `reply_to`, or `attachments`
- if the user says to attach a file, populate `attachments[]` with `filename` plus `content_text` for authored documents, or `content_base64` when you already hold the encoded bytes
- never paste a local path or a `file://` URL into an attachment field; the server cannot read the user's filesystem

For a reply:

- call `get_account_profile` first
- call `get_stored_email_context` first
- prefer `reply_email_id`
- provide `mailbox` if the source email is not in `INBOX`
- optionally include `attachments` when the reply needs files
- let the server fill `In-Reply-To`, `References`, and `Re:` handling

### 8. Email state changes

Use:

- `mark_email`
- `move_email`
- `delete_emails`

Safe usage:

- confirm the `email_ids` first from a listing step
- use the correct mailbox context
- summarize what changed after the tool succeeds

## MCP Tools

### `get_system_status`

Purpose:

- returns runtime readiness and missing configuration

Arguments:

- `account_name` optional

Returns:

- `app_name`
- `env`
- `status`
- `missing_configuration`

### `sync_unread_emails`

Purpose:

- fetch unread emails, generate embeddings, persist them to Supabase, and update sync state

Arguments:

- `limit` integer, optional, default `25`
- `account_name` optional

Returns:

- `status`
- `fetched`
- `embedded`
- `persisted`
- `last_uid`
- `dry_run`
- `details`
- `missing_configuration`

Status semantics:

- `ok`: sync completed
- `blocked`: required configuration is missing
- `error`: execution failed

### `list_available_accounts`

Purpose:

- list configured accounts exposed by the deployment

Returns:

- `status`
- `accounts`

Each account includes:

- `account_name`
- `email_address`
- `mailbox_id`
- `default_mailbox`
- `persona_name`

### `get_account_profile`

Purpose:

- return the selected account persona, signature, and response guidance

Arguments:

- `account_name` optional

Returns:

- `profile.account_name`
- `profile.email_address`
- `profile.mailbox_id`
- `profile.default_mailbox`
- `profile.persona_name`
- `profile.persona_instructions`
- `profile.signature`

### `get_current_datetime`

Purpose:

- return current UTC datetime

Use this before translating relative dates into explicit filters.

### `list_mailboxes`

Purpose:

- list IMAP mailboxes for the selected account

Arguments:

- `account_name` optional

### `list_emails_metadata`

Purpose:

- list email metadata using mailbox and header filters

Arguments:

- `account_name` optional
- `page` default `1`
- `page_size` default `10`
- `since` optional datetime
- `before` optional datetime
- `subject` optional partial subject
- `from_address` optional sender filter
- `to_address` optional recipient filter
- `order` `asc` or `desc`, default `desc`
- `mailbox` default `INBOX`
- `seen` optional boolean
- `flagged` optional boolean
- `answered` optional boolean

Returns:

- `emails`
- `total`
- `page`
- `page_size`

### `get_emails_content`

Purpose:

- fetch text and HTML bodies for selected email ids

Arguments:

- `email_ids` required list
- `account_name` optional
- `mailbox` default `INBOX`

### `search_emails`

Purpose:

- perform IMAP full-text `TEXT` search in a mailbox

Arguments:

- `query` required string
- `account_name` optional
- `mailbox` default `INBOX`
- `page_size` default `20`

### `get_thread`

Purpose:

- fetch a full conversation thread anchored on `message_id`

Arguments:

- `message_id` required
- `account_name` optional
- `mailbox` default `INBOX`

Returns:

- `message_id`
- `emails`

### `get_stored_email_context`

Purpose:

- return stored historical context from Supabase for a reply target or subject

Arguments:

- `reply_email_id` optional
- `subject` optional
- `account_name` optional
- `mailbox` default `INBOX`
- `limit` default `10`

Returns:

- `anchor_email_id`
- `normalized_subject`
- `emails`

Notes:

- use `reply_email_id` when replying to a known email
- this tool is designed to add persisted context from Supabase before drafting a response

### `mark_email`

Purpose:

- enable or disable IMAP flags on selected emails

Arguments:

- `email_ids` required list
- `flag` one of `seen`, `flagged`, `answered`
- `enable` default `true`
- `account_name` optional
- `mailbox` default `INBOX`

### `move_email`

Purpose:

- move selected emails to another mailbox

Arguments:

- `email_ids` required list
- `destination_mailbox` required
- `account_name` optional
- `source_mailbox` default `INBOX`

### `delete_emails`

Purpose:

- delete selected emails from the mailbox

Arguments:

- `email_ids` required list
- `account_name` optional
- `mailbox` default `INBOX`

### `send_email`

Purpose:

- send outbound email through SMTP

Arguments:

- `to` required list
- `subject` required string
- `body_text` required string
- `cc` optional list
- `bcc` optional list
- `body_html` optional string
- `attachments` optional list
- `reply_to` optional string
- `in_reply_to` optional string
- `references` optional string
- `reply_email_id` optional string
- `mailbox` default `INBOX`
- `account_name` optional

Each `attachments[]` item includes:

- `filename` required string
- `content_text` optional string with the document text authored by the model
- `content_base64` optional string with raw file bytes encoded as base64
- `content_type` optional string; inferred from the filename extension when omitted
- `disposition` optional `attachment` or `inline`
- `content_id` optional string for inline HTML references

Exactly one of `content_text` or `content_base64` must be provided.

Choosing the content source:

- generated document (invoice, quote, report, letter) → `content_text` plus `content_type`
- PDF output → `content_text` with `content_type: "application/pdf"`; the server renders the PDF with pagination
- plain text, CSV or Markdown → `content_text`; the server writes UTF-8 bytes
- existing binary file already encoded → `content_base64`; data URLs of the form `data:application/pdf;base64,...` are accepted
- local paths such as `C:\Users\...` or `file://` URLs are not supported and will fail validation

Returns:

- `status`
- `sent`
- `details`
- any SMTP result fields returned by the server model

## Required Configuration Signals

The runtime is degraded or blocked when any of these are missing for the selected account or service:

- `MCP_EMAIL_IMAP_HOST`
- `MCP_EMAIL_IMAP_USERNAME`
- `MCP_EMAIL_IMAP_PASSWORD`
- `MCP_EMAIL_OPENROUTER_API_KEY`
- `MCP_EMAIL_SUPABASE_URL`
- `MCP_EMAIL_SUPABASE_SERVICE_ROLE_KEY`

Useful optional settings include:

- `MCP_EMAIL_ACCOUNTS_JSON`
- `MCP_EMAIL_IMAP_MAILBOX`
- `MCP_EMAIL_MAILBOX_ID`
- `MCP_EMAIL_POLL_BATCH_SIZE`
- `MCP_EMAIL_SERVER_HOST`
- `MCP_EMAIL_SERVER_PORT`
- `MCP_EMAIL_POLL_WEBHOOK_SECRET`

## Claude Response Guidance

When Claude uses this MCP, it should:

- clearly say which account and mailbox it used
- say which persona profile it used when the task is outbound or reply-oriented
- summarize the result in user language, not just raw tool output
- surface counts and identifiers only when useful
- mention configuration blockers explicitly when status is `blocked` or `degraded`
- ask before destructive actions if the user intent is ambiguous
- avoid claiming an email was sent, moved, or deleted unless the MCP call succeeded

## Example User Tasks And Suggested Tool Flow

### "Revisa correos no leídos"

Suggested flow:

1. `get_system_status`
2. `sync_unread_emails`
3. `list_emails_metadata` with `mailbox = INBOX`, `seen = false`
4. optionally `get_emails_content` for the selected items

### "Busca un correo sobre contrato"

Suggested flow:

1. `list_available_accounts` if needed
2. `search_emails` with query terms
3. `get_emails_content` for the chosen result if the user wants the body

### "Respóndele a este correo"

Suggested flow:

1. identify the target email id from listing or search output
2. `get_account_profile`
3. `get_thread` for live context
4. `get_stored_email_context` for persisted context from Supabase
5. `send_email` using `reply_email_id`

### "Muévelo a Archive y márcalo como leído"

Suggested flow:

1. confirm the target `email_id`
2. `mark_email` with `flag = seen`, `enable = true`
3. `move_email` to the target mailbox

## Non-MCP HTTP Surface

These are part of the backend surface, but Claude should normally prefer MCP tools over direct HTTP endpoints:

- `POST /tasks/poll`: trigger unread sync over HTTP
- `GET /healthz`: health-style endpoint for the FastAPI app

For Claude task execution, the MCP surface is the primary interface.

## Source Of Truth

- Tool registration: `src/mcp_email_server/server.py`
- Runtime settings: `src/mcp_email_server/config.py`
- Sync orchestration: `src/mcp_email_server/services/sync.py`
- Deployment notes: `docs/google-cloud-run.md`

If behavior in this skill conflicts with code, treat the Python source as the source of truth.
