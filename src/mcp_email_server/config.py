from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PLACEHOLDER_VALUES = {
    "",
    "replace-me",
    "mail.example.com",
    "agent@example.com",
    "https://your-project.supabase.co",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="MCP_EMAIL_",
        extra="ignore",
    )

    app_name: str = "mcp-email-server"
    env: str = "development"
    log_level: str = "INFO"
    poll_batch_size: int = 25

    imap_host: str | None = None
    imap_port: int = 993
    imap_username: str | None = None
    imap_password: SecretStr | None = None
    imap_mailbox: str = "INBOX"
    mailbox_id: str | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from_address: str | None = None
    smtp_use_tls: bool = False
    smtp_starttls: bool = True

    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_embedding_model: str = "nvidia/llama-nemotron-embed-vl-1b-v2:free"

    supabase_url: str | None = None
    supabase_service_role_key: SecretStr | None = None
    supabase_schema: str = "public"
    supabase_messages_table: str = "email_embeddings"
    supabase_sync_state_table: str = "email_ingest_state"

    server_host: str = "0.0.0.0"
    server_port: int = Field(default=8000, ge=1, le=65535)

    @staticmethod
    def _is_missing_value(value: str | None) -> bool:
        if value is None:
            return True
        normalized = value.strip()
        return normalized in PLACEHOLDER_VALUES

    @classmethod
    def _is_missing_secret(cls, value: SecretStr | None) -> bool:
        if value is None:
            return True
        return cls._is_missing_value(value.get_secret_value())

    def missing_imap_config(self) -> list[str]:
        missing: list[str] = []
        if self._is_missing_value(self.imap_host):
            missing.append("MCP_EMAIL_IMAP_HOST")
        if self._is_missing_value(self.imap_username):
            missing.append("MCP_EMAIL_IMAP_USERNAME")
        if self._is_missing_secret(self.imap_password):
            missing.append("MCP_EMAIL_IMAP_PASSWORD")
        return missing

    def missing_openrouter_config(self) -> list[str]:
        return [] if not self._is_missing_secret(self.openrouter_api_key) else ["MCP_EMAIL_OPENROUTER_API_KEY"]

    def resolved_mailbox_id(self) -> str:
        if not self._is_missing_value(self.mailbox_id):
            return self.mailbox_id.strip()
        if self._is_missing_value(self.imap_username):
            return "default"
        return self.imap_username.split("@", maxsplit=1)[0].strip() or "default"

    def resolved_smtp_from_address(self) -> str | None:
        if not self._is_missing_value(self.smtp_from_address):
            return self.smtp_from_address.strip().lower()
        if not self._is_missing_value(self.smtp_username):
            return self.smtp_username.strip().lower()
        return None

    def resolved_supabase_rest_url(self) -> str | None:
        if self._is_missing_value(self.supabase_url):
            return None

        parsed = urlparse(self.supabase_url)
        if parsed.scheme in {"http", "https"}:
            return self.supabase_url.rstrip("/")

        if parsed.scheme.startswith("postgres") and parsed.hostname:
            host = parsed.hostname
            if host.startswith("db.") and host.endswith(".supabase.co"):
                project_ref = host.removeprefix("db.").removesuffix(".supabase.co")
                return f"https://{project_ref}.supabase.co"

        return self.supabase_url.rstrip("/")

    def missing_supabase_config(self) -> list[str]:
        missing: list[str] = []
        if self._is_missing_value(self.supabase_url):
            missing.append("MCP_EMAIL_SUPABASE_URL")
        if self._is_missing_secret(self.supabase_service_role_key):
            missing.append("MCP_EMAIL_SUPABASE_SERVICE_ROLE_KEY")
        return missing

    def missing_smtp_config(self) -> list[str]:
        missing: list[str] = []
        if self._is_missing_value(self.smtp_host):
            missing.append("MCP_EMAIL_SMTP_HOST")
        if self._is_missing_value(self.smtp_username):
            missing.append("MCP_EMAIL_SMTP_USERNAME")
        if self._is_missing_secret(self.smtp_password):
            missing.append("MCP_EMAIL_SMTP_PASSWORD")
        if self.resolved_smtp_from_address() is None:
            missing.append("MCP_EMAIL_SMTP_FROM_ADDRESS")
        return missing

    def missing_runtime_config(self) -> list[str]:
        return [
            *self.missing_imap_config(),
            *self.missing_openrouter_config(),
            *self.missing_supabase_config(),
        ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
