import json
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PLACEHOLDER_VALUES = {
    "",
    "replace-me",
    "mail.example.com",
    "agent@example.com",
    "https://your-project.supabase.co",
}


class EmailAccountConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account_name: str
    mailbox_id: str | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    imap_username: str | None = None
    imap_password: SecretStr | None = None
    imap_mailbox: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from_address: str | None = None
    smtp_use_tls: bool | None = None
    smtp_starttls: bool | None = None

    def resolved_mailbox_id(self) -> str:
        if self.mailbox_id and self.mailbox_id.strip():
            return self.mailbox_id.strip()
        return self.account_name.strip()

    def resolved_email_address(self) -> str:
        for candidate in (self.imap_username, self.smtp_from_address, self.smtp_username):
            if candidate and candidate.strip():
                return candidate.strip().lower()
        return ""


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
    accounts_json: str | None = None

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

    def account_configs(self) -> list[EmailAccountConfig]:
        if self._is_missing_value(self.accounts_json):
            return [self._default_account_config()]

        try:
            raw_accounts = json.loads(self.accounts_json)
        except json.JSONDecodeError as exc:
            raise ValueError("MCP_EMAIL_ACCOUNTS_JSON must be valid JSON") from exc

        if not isinstance(raw_accounts, list) or not raw_accounts:
            raise ValueError("MCP_EMAIL_ACCOUNTS_JSON must contain a non-empty JSON array")

        return [EmailAccountConfig.model_validate(item) for item in raw_accounts]

    def default_account_name(self) -> str:
        return self.account_configs()[0].account_name

    def resolve_account_config(self, account_name: str | None = None) -> EmailAccountConfig:
        if account_name is None or not account_name.strip():
            return self.account_configs()[0]

        normalized = account_name.strip().lower()
        for config in self.account_configs():
            aliases = {
                config.account_name.lower(),
                config.resolved_mailbox_id().lower(),
            }
            email_address = config.resolved_email_address()
            if email_address:
                aliases.add(email_address)
            if normalized in aliases:
                return config

        raise ValueError(f"Unknown account_name: {account_name}")

    def for_account(self, account_name: str | None = None) -> "Settings":
        account = self.resolve_account_config(account_name)
        data = self.model_dump(mode="python")
        data.update(
            {
                "imap_host": account.imap_host if account.imap_host is not None else data.get("imap_host"),
                "imap_port": account.imap_port if account.imap_port is not None else data.get("imap_port"),
                "imap_username": account.imap_username if account.imap_username is not None else data.get("imap_username"),
                "imap_password": account.imap_password if account.imap_password is not None else data.get("imap_password"),
                "imap_mailbox": account.imap_mailbox if account.imap_mailbox is not None else data.get("imap_mailbox"),
                "mailbox_id": account.resolved_mailbox_id(),
                "smtp_host": account.smtp_host if account.smtp_host is not None else data.get("smtp_host"),
                "smtp_port": account.smtp_port if account.smtp_port is not None else data.get("smtp_port"),
                "smtp_username": account.smtp_username if account.smtp_username is not None else data.get("smtp_username"),
                "smtp_password": account.smtp_password if account.smtp_password is not None else data.get("smtp_password"),
                "smtp_from_address": account.smtp_from_address if account.smtp_from_address is not None else data.get("smtp_from_address"),
                "smtp_use_tls": account.smtp_use_tls if account.smtp_use_tls is not None else data.get("smtp_use_tls"),
                "smtp_starttls": account.smtp_starttls if account.smtp_starttls is not None else data.get("smtp_starttls"),
            }
        )
        return type(self).model_validate(data)

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

    def _default_account_config(self) -> EmailAccountConfig:
        return EmailAccountConfig(
            account_name=self.resolved_mailbox_id(),
            mailbox_id=self.resolved_mailbox_id(),
            imap_host=self.imap_host,
            imap_port=self.imap_port,
            imap_username=self.imap_username,
            imap_password=self.imap_password,
            imap_mailbox=self.imap_mailbox,
            smtp_host=self.smtp_host,
            smtp_port=self.smtp_port,
            smtp_username=self.smtp_username,
            smtp_password=self.smtp_password,
            smtp_from_address=self.smtp_from_address,
            smtp_use_tls=self.smtp_use_tls,
            smtp_starttls=self.smtp_starttls,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
