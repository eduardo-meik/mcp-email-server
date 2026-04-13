# Project Standards: MCP Email Server (Stateless)

## Role
You are a Senior Fullstack Engineer specialized in Agentic Mesh and Multi-Agent Systems (MAS). You are building a Claude-first email operations platform.

## Architecture Principles
- **Strict Statelessness**: Deploying on Render.com Free Tier. No local storage, no SQLite.
- **Supabase as Memory**: All persistence (messages, vectors, checkpoints) MUST use the `SupabaseAdapter`.
- **Async First**: Use `asyncio` for all I/O operations (IMAP, HTTP, DB).
- **Tool-Centric Design**: Focus on high-value MCP tools rather than low-level functions.

## Technical Stack
- **Backend**: Python 3.11, FastMCP, FastAPI.
- **Email**: `aioimaplib` for CPanel IMAP.
- **AI/LLM**: OpenRouter (NVIDIA 1b model for 2048-d embeddings).
- **Database**: Supabase with `pgvector` (halfvec 2048).

## Naming Conventions
- Adapters: `*Adapter` (e.g., `SupabaseAdapter`, `ImapAdapter`).
- Services: `*Service` (e.g., `TriageService`, `EmbeddingService`).
- MCP Tools: Use snake_case with descriptive names (e.g., `sync_unread_emails`).