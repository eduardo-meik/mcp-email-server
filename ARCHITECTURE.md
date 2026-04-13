# Architecture Map: Agentic Email Mesh



## Data Flow
1. **Trigger**: External Cron hits `/tasks/poll`.
2. **Fetch**: `ImapAdapter` retrieves UNSEEN emails from CPanel.
3. **Embed**: `OpenRouterAdapter` generates 2048-d vectors.
4. **Persist**: `SupabaseAdapter` saves data and updates `last_uid`.
5. **Serve**: `FastMCP` exposes tools for Claude to read/search this data.

## Deployment
- **Platform**: Render.com (Free Tier).
- **Container**: Docker (Stateless).
- **Outbound Rule**: Use HTTP APIs for sending (SMTP is blocked).