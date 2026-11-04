import json
import re
from html import escape

import httpx

from .config import Settings
from .models import ResearchInput, ResearchResult
from .prompt import SYSTEM_PROMPT


def _json_from_model(content: str) -> dict:
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
    return json.loads(content)


class DeepSeekClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=45)
        self._owned_client = client is None

    async def research(self, research_input: ResearchInput) -> ResearchResult:
        if not self.settings.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        payload = {
            "model": self.settings.deepseek_model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": research_input.model_dump_json()},
            ],
        }
        response = await self.client.post(
            f"{self.settings.deepseek_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.deepseek_api_key}"},
            json=payload,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return ResearchResult.model_validate(_json_from_model(content))

    async def close(self) -> None:
        if self._owned_client:
            await self.client.aclose()


def telegram_chunks(text: str, max_length: int = 4096) -> list[str]:
    safe_text = escape(text)
    return [safe_text[index : index + max_length] for index in range(0, len(safe_text), max_length)] or [""]


class TelegramClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=20)
        self._owned_client = client is None

    async def publish(self, text: str) -> None:
        if not self.settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        endpoint = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        for chunk in telegram_chunks(text):
            response = await self.client.post(
                endpoint,
                json={"chat_id": self.settings.telegram_chat_id, "text": chunk, "parse_mode": self.settings.telegram_parse_mode},
            )
            response.raise_for_status()

    async def close(self) -> None:
        if self._owned_client:
            await self.client.aclose()
