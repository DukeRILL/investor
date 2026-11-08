"""Wiring: builds clients from settings; shared by the scheduler, CLI and web app."""

import logging
from dataclasses import dataclass
from datetime import timezone
from html import escape

from .config import Settings
from .deepseek import DeepSeekClient
from .http import Http, HttpxClient
from .images import CloudinaryClient
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
    images: object | None = None  # image host with upload_image(); Telegraph's own upload is dead

    def prompt(self, name: str) -> str:
        return (self.settings.prompts_dir / f"{name}.md").read_text(encoding="utf-8")

    def target_chat(self, radar: bool = False) -> str:
        """Channel in auto mode; admin chat in draft mode. Radar has its own mode."""
        mode = self.settings.radar_publish_mode if radar else self.settings.publish_mode
        if mode == "auto":
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
        self._budget_alert()
        return cost

    def _budget_alert(self) -> None:
        """Once per Beijing day: tell the admin when the day's DeepSeek spend passes DAILY_BUDGET_USD."""
        limit = self.settings.daily_budget_usd
        if limit <= 0:
            return
        from .timeutil import now_bj  # noqa: PLC0415

        now = now_bj()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        spent = self.store.usage_since(midnight.isoformat(timespec="seconds"))
        key = f"budget_alert:{now.date().isoformat()}"
        if spent >= limit and not self.store.get(key):
            self.store.set(key, "1")
            self.notify_admin(f"DeepSeek: расходы за сегодня ≈ ${spent:.2f}, дневной лимит ${limit:.2f}. "
                              f"При ${2 * limit:.2f} вызовы остановятся до завтра.")


def build_app(settings: Settings, http: Http | None = None) -> App:
    http = http or HttpxClient()
    store = Store(settings.database_path)
    images = None
    if settings.cloudinary_cloud_name and settings.cloudinary_api_key and settings.cloudinary_api_secret:
        images = CloudinaryClient(http, settings.cloudinary_cloud_name, settings.cloudinary_api_key,
                                  settings.cloudinary_api_secret)
    return App(
        settings=settings, http=http, store=store,
        deepseek=DeepSeekClient(http, settings.deepseek_api_key, settings.deepseek_base_url, store=store,
                                daily_budget_usd=settings.daily_budget_usd),
        telegram=TelegramClient(http, settings.telegram_bot_token),
        telegraph=TelegraphClient(http, lambda: store.get("telegraph_token"), lambda t: store.set("telegraph_token", t)),
        images=images,
    )
