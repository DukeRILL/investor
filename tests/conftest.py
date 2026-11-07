import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from investor_agent.app import App  # noqa: E402
from investor_agent.config import Settings  # noqa: E402
from investor_agent.deepseek import Usage  # noqa: E402
from investor_agent.storage import Store  # noqa: E402


class FakeTelegram:
    def __init__(self, fail: bool = False):
        self.sent, self.deleted, self.fail = [], [], fail

    def send(self, chat_id, html, button=None, disable_preview=True):
        from investor_agent.telegram import TelegramError
        if self.fail:
            raise TelegramError("sendMessage failed: test")
        self.sent.append({"chat": chat_id, "html": html, "button": button})
        return len(self.sent)

    def send_photo(self, chat_id, photo_url, html, button=None):
        from investor_agent.telegram import TelegramError
        if self.fail:
            raise TelegramError("sendPhoto failed: test")
        self.sent.append({"chat": chat_id, "html": html, "button": button, "photo": photo_url})
        return len(self.sent)

    def send_series(self, chat_id, parts, button=None):
        return [self.send(chat_id, p) for p in parts]

    def delete(self, chat_id, mid):
        self.deleted.append(mid)
        return True


class FakeTelegraph:
    def __init__(self):
        self.pages = []

    def create_page(self, title, nodes):
        self.pages.append((title, nodes))
        return f"https://telegra.ph/test-page-{len(self.pages)}" if len(self.pages) > 1 else "https://telegra.ph/test-page"

    def upload_image(self, data, content_type):
        return "https://telegra.ph/file/test.jpg"


class FakeDeepSeek:
    """Scripted research(): each call pops a handler(registry, payload) -> dict."""

    def __init__(self, handlers):
        self.handlers = list(handlers)
        self.calls = []
        self.usage = Usage()

    def research(self, model, system, payload, registry, **kw):
        self.calls.append({"model": model, "payload": payload})
        return self.handlers.pop(0)(registry, payload)

    def fetch(self, url):
        return {"title": "", "content": ""}


@pytest.fixture
def make_app(tmp_path):
    def _make(deepseek=None, telegram=None, mode="auto", admin="111", http=None):
        settings = Settings(database_path=tmp_path / "db.sqlite3", publish_mode=mode, telegram_admin_chat_id=admin,
                            deepseek_api_key="k", telegram_bot_token="t")
        return App(settings=settings, http=http, store=Store(settings.database_path),
                   deepseek=deepseek or FakeDeepSeek([]), telegram=telegram or FakeTelegram(),
                   telegraph=FakeTelegraph())
    return _make


class FakeHttp:
    """Serves files from a dict {url: (bytes, content_type)} for get_bytes; everything else is 404."""

    def __init__(self, files=None):
        self.files = files or {}

    def get_bytes(self, url, max_bytes=6_000_000, timeout=30):
        from investor_agent.http import HttpError
        if url not in self.files:
            raise HttpError(404, "not found")
        return self.files[url]
