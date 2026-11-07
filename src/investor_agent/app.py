"""Wiring: builds clients from settings; shared by the scheduler, CLI and web app."""

import logging
from dataclasses import dataclass
from html import escape

from .config import Settings
from .deepseek import DeepSeekClient
from .http import Http, HttpxClient
from .storage import Store
from .telegram import TelegramClient, TelegramError, TelegraphClient

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    http: Http
    store: Store
    deepseek: DeepSeekClient
    telegram: TelegramClient
    telegraph: TelegraphClient

    def prompt(self, name: str) -> str:
        return (self.settings.prompts_dir / f"{name}.md").read_text(encoding="utf-8")

    def target_chat(self) -> str:
        """Channel in auto mode; admin chat in draft mode."""
        if self.settings.publish_mode == "auto":
            return self.settings.telegram_chat_id
        return self.settings.telegram_admin_chat_id or self.settings.telegram_chat_id

    def notify_admin(self, text: str) -> None:
        """Failures and reports go to the admin chat only; never to the channel."""
        log.warning("admin: %s", text)
        if not self.settings.telegram_admin_chat_id:
            return
        try:
            self.telegram.send(self.settings.telegram_admin_chat_id, escape(text[:3500]))
        except TelegramError as exc:
            log.error("admin notification failed: %s", exc)

    def record_usage(self, job: str) -> float:
        usage = self.deepseek.usage
        cost = usage.cost_usd()
        self.store.usage_add(job, cost, {"models": usage.by_model, "searches": usage.searches, "fetches": usage.fetches})
        self.deepseek.usage = type(usage)()
        return cost


def build_app(settings: Settings, http: Http | None = None) -> App:
    http = http or HttpxClient()
    store = Store(settings.database_path)
    return App(
        settings=settings, http=http, store=store,
        deepseek=DeepSeekClient(http, settings.deepseek_api_key, settings.deepseek_base_url),
        telegram=TelegramClient(http, settings.telegram_bot_token),
        telegraph=TelegraphClient(http, lambda: store.get("telegraph_token"), lambda t: store.set("telegraph_token", t)),
    )
