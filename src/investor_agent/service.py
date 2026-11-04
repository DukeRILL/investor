import json
from datetime import datetime, timezone

from .clients import DeepSeekClient, TelegramClient
from .config import Settings
from .models import ResearchInput, ResearchResult
from .storage import PublicationStore


class ResearchService:
    def __init__(self, settings: Settings, deepseek: DeepSeekClient, telegram: TelegramClient, store: PublicationStore) -> None:
        self.settings, self.deepseek, self.telegram, self.store = settings, deepseek, telegram, store

    def publish_reason(self, result: ResearchResult) -> str | None:
        if self.settings.telegram_publish_mode != "auto":
            return "draft_mode"
        if result.status != "PUBLISH":
            return f"status_{result.status.lower()}"
        if not result.sources:
            return "no_sources"
        if self.store.already_published(result.event_id, result.subject):
            return "duplicate"
        return None

    async def run(self, research_input: ResearchInput) -> tuple[ResearchResult, bool, str]:
        result = await self.deepseek.research(research_input)
        reason = self.publish_reason(result)
        if reason:
            return result, False, reason
        await self.telegram.publish(result.telegram_post)
        self.store.save(result.event_id, result.subject, datetime.now(timezone.utc).isoformat(), json.dumps([str(source.url) for source in result.sources]))
        return result, True, "published"
