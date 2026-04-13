---
name: mcp-email-server-2
description: "Use this skill when you need to interact with the MCP Email Server for email ingestion, runtime checks, unread email sync, mailbox browsing, email search, email state changes, outbound sends, or general MCP email workflows. Keywords: email MCP, sync unread emails, list mailboxes, list emails metadata, get email content, search emails, mark email, move email, delete emails, send_email."
---

# MCP Email Server Skill

## Purpose

This server is a stateless MCP and FastAPI service for email operations.

Primary workflows:
1. Fetch unread emails from IMAP.
2. Generate embeddings through OpenRouter.
3. Persist messages and sync state in Supabase.
4. Expose mailbox browsing and email actions through MCP tools.

## Service Facts

- Runtime type: stateless service.
- Persistence: Supabase only.
- IMAP adapter: `aioimaplib`.
- MCP framework: `FastMCP`.
- Web framework: `FastAPI`.
- Default MCP base URL: `http://localhost:8000/mcp`.
- Default health endpoint: `http://localhost:8000/healthz`.
- Default poll endpoint: `http://localhost:8000/tasks/poll`.

## MCP Tools

### `get_system_status`

Returns runtime readiness and missing configuration.

Response shape:
- `app_name`: application name.
- `env`: current environment.
- `status`: `ok` or `degraded`.
- `missing_configuration`: list of missing environment variables.

Use this first when you need to confirm whether the server is ready to run email sync operations.

### `sync_unread_emails`

Fetches unread emails, generates embeddings, persists them to Supabase, and updates the ingest state.

Arguments:
- `limit` integer, optional, default `25`.
- `account_name` optional when multiple accounts are configured.

Response shape:
- `status`: `ok`, `blocked`, or `error`.
- `fetched`: number of unread messages fetched.
- `embedded`: number of messages embedded.
- `persisted`: number of rows written to Supabase.
- `last_uid`: highest processed IMAP UID.
- `dry_run`: `true` when blocked by configuration.
- `details`: optional error or status detail.
- `missing_configuration`: required variables that are not configured.

Use this tool when you need to trigger inbox ingestion from Claude.

### `list_available_accounts`

Returns the configured account surface for this deployment.

Current behavior:
- supports single-account and multi-account configurations.
- `account_name` resolves the configured account entry and defaults to the first configured account.
- `mailbox_id` is the persisted account partition for sync state and stored messages.

### `get_current_datetime`

Returns the current UTC timestamp.

Use this before translating relative user requests like "hoy" or "esta semana" into concrete filters.

### `list_mailboxes`

Lists IMAP folders for the configured account.

Arguments:
- `account_name` optional, validated against the configured single account.

### `list_emails_metadata`

Lists email metadata from a mailbox with optional filtering.

Arguments:
- `account_name` optional.
- `page` integer, default `1`.
- `page_size` integer, default `10`.
- `since` optional datetime.
- `before` optional datetime.
- `subject` optional partial subject filter.
- `from_address` optional sender filter.
- `to_address` optional recipient filter.
- `order` `asc` or `desc`, default `desc`.
- `mailbox` string, default `INBOX`.
- `seen` optional boolean.
- `flagged` optional boolean.
- `answered` optional boolean.

Response shape:
- `emails`: list of metadata rows.
- `total`: full match count before pagination.
- `page`, `page_size`.

### `get_emails_content`

Fetches text and HTML bodies for specific `email_id` values.

Arguments:
- `email_ids` list of ids from `list_emails_metadata`.
- `account_name` optional.
- `mailbox` optional fallback mailbox, default `INBOX`.

### `search_emails`

Runs IMAP full-text `TEXT` search in the selected mailbox.

Arguments:
- `query` required string.
- `account_name` optional.
- `mailbox` string, default `INBOX`.
- `page_size` integer, default `20`.

### `mark_email`

Marks messages using IMAP flags.

Arguments:
- `email_ids` list of ids.
- `flag` one of `seen`, `flagged`, `answered`.
- `enable` boolean, default `true`.
- `account_name` optional.
- `mailbox` string, default `INBOX`.

### `move_email`

Moves emails between folders.

Arguments:
- `email_ids` list of ids.
- `destination_mailbox` required string.
- `account_name` optional.
- `source_mailbox` string, default `INBOX`.

### `delete_emails`

Deletes emails from the selected mailbox.

Arguments:
- `email_ids` list of ids.
- `account_name` optional.
- `mailbox` string, default `INBOX`.

### `send_email`

Sends outbound email through SMTP.

Arguments:
- `to` required list of recipients.
- `subject` required string.
- `body_text` required string.
- `cc`, `bcc` optional recipient lists.
- `body_html` optional HTML body.
- `reply_to` optional reply-to header.
- `in_reply_to` optional thread parent Message-ID.
- `references` optional space-separated Message-ID chain.
- `reply_email_id` optional email id; when provided, the server auto-fills `In-Reply-To`, `References`, and a `Re:` subject if needed.
- `mailbox` optional mailbox used to resolve `reply_email_id`, default `INBOX`.
- `account_name` optional when multiple accounts are configured.

### `get_thread`

Fetches a conversation thread anchored on a `Message-ID`.

Arguments:
- `message_id` required Message-ID.
- `account_name` optional when multiple accounts are configured.
- `mailbox` string, default `INBOX`.

Response shape:
- `message_id`: anchor Message-ID.
- `emails`: list of full emails in thread order.

## Required Environment Variables

The server reports degraded or blocked status if these are missing:

- `MCP_EMAIL_IMAP_HOST`
- `MCP_EMAIL_IMAP_USERNAME`
- `MCP_EMAIL_IMAP_PASSWORD`
- `MCP_EMAIL_OPENROUTER_API_KEY`
- `MCP_EMAIL_SUPABASE_URL`
- `MCP_EMAIL_SUPABASE_SERVICE_ROLE_KEY`

Useful optional variables:

- `MCP_EMAIL_IMAP_MAILBOX` default `INBOX`
- `MCP_EMAIL_MAILBOX_ID` optional override for mailbox identity
- `MCP_EMAIL_POLL_BATCH_SIZE` default `25`
- `MCP_EMAIL_SERVER_HOST` default `0.0.0.0`
- `MCP_EMAIL_SERVER_PORT` default `8000`

## JSON-RPC Examples

### List available tools

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "tools/list",
  "params": {}
}
```

### Call `get_system_status`

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "tools/call",
  "params": {
    "name": "get_system_status",
    "arguments": {}
  }
}
```

### Call `sync_unread_emails` with default limit

```json
{
  "jsonrpc": "2.0",
  "id": "3",
  "method": "tools/call",
  "params": {
    "name": "sync_unread_emails",
    "arguments": {}
  }
}
```

### Call `sync_unread_emails` with an explicit limit

```json
{
  "jsonrpc": "2.0",
  "id": "4",
  "method": "tools/call",
  "params": {
    "name": "sync_unread_emails",
    "arguments": {
      "limit": 10
    }
  }
}
```

## HTTP Invocation Example

Send JSON-RPC requests as HTTP POST to `http://localhost:8000/mcp`.

Example payload:

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "tools/call",
  "params": {
    "name": "get_system_status",
    "arguments": {}
  }
}
```

## Operational Guidance

- Run `get_system_status` before `sync_unread_emails` if configuration may be incomplete.
- Run `list_available_accounts` first if a user asks for an account name and you do not already know the configured one.
- Run `get_current_datetime` before converting relative date requests into `since` or `before`.
- Use `get_thread` or `reply_email_id` when replying, so the server carries forward `In-Reply-To` and `References` correctly.
- Treat `status = blocked` as a configuration problem, not a transient runtime failure.
- Treat `status = error` as an execution failure in IMAP, HTTP, OpenRouter, or Supabase.
- The service stores the latest processed UID in Supabase and only fetches unseen messages after that point.
- The server is designed to be stateless. Do not assume local file or SQLite persistence.
- The current mailbox tooling is MVP scope: no attachment download and no dedicated credential store outside environment configuration.

## FastAPI Endpoints

These are not MCP tools, but they are part of the service surface:

- `GET /healthz`: health response similar to `get_system_status`.
- `POST /tasks/poll`: triggers the same unread sync flow, using the provided `limit` or the configured batch size.

## Source of Truth

Tool registration lives in `src/mcp_email_server/server.py`.
Runtime settings live in `src/mcp_email_server/config.py`.
Sync orchestration lives in `src/mcp_email_server/services/sync.py`.