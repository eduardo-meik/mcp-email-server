# Architecture Map: Agentic Email Mesh



## Data Flow
1. **Trigger**: External Cron hits `/tasks/poll`.
2. **Fetch**: `ImapAdapter` retrieves UNSEEN emails from CPanel.
3. **Embed**: `OpenRouterAdapter` generates 2048-d vectors.
4. **Persist**: `SupabaseAdapter` saves data and updates `last_uid`.
5. **Serve**: `FastMCP` exposes tools for Claude to read/search this data.

## Inbound attachments
Secure, fail-closed pipeline: sender allowlist + aligned DKIM/DMARC,
BODYSTRUCTURE declaration checks, magic bytes, structural checks per type,
SHA-256 dedupe, ClamAV scan (separate Cloud Run service), private Supabase
Storage bucket and on-demand sanitized text extraction. Metadata is exposed in
`list_emails_metadata`/`get_emails_content` (`attachments[]`); bytes and text
only via the `download_attachment` tool. See [docs/attachments.md](docs/attachments.md).

## Deployment
- **Platform**: Render.com (Free Tier).
- **Container**: Docker (Stateless).
- **Outbound Rule**: Use HTTP APIs for sending (SMTP is blocked).
- **Antivirus**: standalone `clamav-scanner` Cloud Run service (`scanner/`);
  nightly rebuild via Cloud Scheduler + Cloud Build keeps signatures fresh.

## Official quotation delivery
`mcp_quoter` sends the original PDF directly to `POST /v1/quotation-emails`
with a dedicated bearer credential and a server-configured sender account.
The PDF stays in memory; only fingerprints, references and attempt outcomes go
through `SupabaseAdapter`. PostgreSQL atomically reserves idempotency keys and
enforces cross-worker send limits. SMTP acceptance is not recipient delivery.
See [the contract and staging checklist](docs/quotation-delivery.md).

All MCP requests now require the bearer header, including the handshake.
URL tokens are not accepted. Production refuses to boot without a non-placeholder
access token; polling is disabled without its separate webhook secret.