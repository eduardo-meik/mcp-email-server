import httpx

from mcp_email_server.adapters.errors import AdapterConfigurationError
from mcp_email_server.config import Settings
from mcp_email_server.models import EmailMessage, EmbeddedEmail


class OpenRouterAdapter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def embed_messages(self, messages: list[EmailMessage]) -> list[EmbeddedEmail]:
        missing = self._settings.missing_openrouter_config()
        if missing:
            raise AdapterConfigurationError(missing)

        if not messages:
            return []

        payload = {
            "model": self._settings.openrouter_embedding_model,
            "input": [self._message_to_embedding_text(message) for message in messages],
        }
        headers = {
            "Authorization": f"Bearer {self._settings.openrouter_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(base_url=self._settings.openrouter_base_url, timeout=30.0) as client:
            response = await client.post("/embeddings", headers=headers, json=payload)
            response.raise_for_status()

        body = response.json()
        data = body.get("data", [])
        return [
            EmbeddedEmail(
                message=message,
                embedding=item.get("embedding", []),
                model=body.get("model", self._settings.openrouter_embedding_model),
            )
            for message, item in zip(messages, data, strict=False)
        ]

    @staticmethod
    def _message_to_embedding_text(message: EmailMessage) -> str:
        return "\n".join(
            part
            for part in [
                f"subject: {message.subject}".strip(),
                f"from: {message.sender}".strip(),
                message.text_body.strip(),
            ]
            if part
        )
