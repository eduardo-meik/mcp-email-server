---
name: mcp-email-server-2
description: "Use this skill when you need to interact with the MCP Email Server for email ingestion, runtime checks, unread email sync, IMAP-to-Supabase workflows, or MCP tool invocation. Keywords: email MCP, sync unread emails, Supabase email embeddings, IMAP inbox sync, get_system_status, sync_unread_emails."
---

# MCP Email Server Skill

## Purpose

This server is a stateless MCP and FastAPI service for email operations.

Primary workflow:
1. Fetch unread emails from IMAP.
2. Generate embeddings through OpenRouter.
3. Persist messages and sync state in Supabase.
4. Expose the workflow through MCP tools and HTTP endpoints.

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
- Treat `status = blocked` as a configuration problem, not a transient runtime failure.
- Treat `status = error` as an execution failure in IMAP, HTTP, OpenRouter, or Supabase.
- The service stores the latest processed UID in Supabase and only fetches unseen messages after that point.
- The server is designed to be stateless. Do not assume local file or SQLite persistence.

## FastAPI Endpoints

These are not MCP tools, but they are part of the service surface:

- `GET /healthz`: health response similar to `get_system_status`.
- `POST /tasks/poll`: triggers the same unread sync flow, using the provided `limit` or the configured batch size.

## Source of Truth

Tool registration lives in `src/mcp_email_server/server.py`.
Runtime settings live in `src/mcp_email_server/config.py`.
Sync orchestration lives in `src/mcp_email_server/services/sync.py`.