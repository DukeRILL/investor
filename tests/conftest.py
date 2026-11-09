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

    def send(self, chat_id, html, button=None, disable_preview=True, reply_to=None):
        from investor_agent.telegram import TelegramError
        if self.fail:
            raise TelegramError("sendMessage failed: test")
        self.sent.append({"chat": chat_id, "html": html, "button": button, "reply_to": reply_to})
        return len(self.sent)

    def edit_text(self, chat_id, mid, html, button=None):
        self.edited = getattr(self, "edited", []) + [{"mid": mid, "html": html, "button": button}]

    def pin(self, chat_id, mid):
        self.pinned = getattr(self, "pinned", []) + [mid]
        return True

    def send_photo(self, chat_id, photo_url, html, button=None):
        from investor_agent.telegram import TelegramError
        if self.fail:
            raise TelegramError("sendPhoto failed: test")
        self.sent.append({"chat": chat_id, "html": html, "button": button, "photo": photo_url})
        return len(self.sent)

    def send_series(self, chat_id, parts, button=None, reply_to=None):
        return [self.send(chat_id, p, reply_to=reply_to if i == 0 else None) for i, p in enumerate(parts)]

    def delete(self, chat_id, mid):
        self.deleted.append(mid)
        return True

    def send_poll(self, chat_id, question, options):
        self.sent.append({"chat": chat_id, "question": question, "options": list(options)})
        return len(self.sent)

    def stop_poll(self, chat_id, mid):
        self.stopped = mid
        votes = getattr(self, "votes", [])
        return {"options": [{"text": o, "voter_count": v} for o, v in votes]}


class FakeTelegraph:
    def __init__(self):
        self.pages = []

    def create_page(self, title, nodes):
        self.pages.append((title, nodes))
        return f"https://telegra.ph/test-page-{len(self.pages)}" if len(self.pages) > 1 else "https://telegra.ph/test-page"

    def edit_page(self, url, title, nodes):
        self.edited = getattr(self, "edited", []) + [(url, title, nodes)]
        return url

    def upload_image(self, data, content_type):
        return "https://telegra.ph/file/test.jpg"

    def get_page(self, url):
        urls = ["https://telegra.ph/test-page"] + [f"https://telegra.ph/test-page-{i}" for i in range(2, len(self.pages) + 1)]
        return self.pages[urls.index(url)][1] if url in urls else []


class FakeDeepSeek:
    """Scripted research(): each call pops a handler(registry, payload) -> dict."""

    def __init__(self, handlers):
        self.handlers = list(handlers)
        self.calls = []
        self.usage = Usage()

    def complete_json(self, model, system, payload, **kw):
        self.calls.append({"model": model, "payload": payload, "json": True})
        if not getattr(self, "json_handler", None):
            raise RuntimeError("no scripted complete_json")
        return self.json_handler(payload)

    def research(self, model, system, payload, registry, **kw):
        self.calls.append({"model": model, "payload": payload})
        return self.handlers.pop(0)(registry, payload)

    def fetch(self, url):
        return {"title": "", "content": ""}


@pytest.fixture
def make_app(tmp_path):
    def _make(deepseek=None, telegram=None, mode="auto", admin="111", http=None, images=None):
        settings = Settings(database_path=tmp_path / "db.sqlite3", publish_mode=mode, telegram_admin_chat_id=admin,
                            deepseek_api_key="k", telegram_bot_token="t")
        return App(settings=settings, http=http, store=Store(settings.database_path),
                   deepseek=deepseek or FakeDeepSeek([]), telegram=telegram or FakeTelegram(),
                   telegraph=FakeTelegraph(), images=images)
    return _make


class FakeHttp:
    """Serves files from a dict {url: (bytes, content_type)} for get_bytes; everything else is 404."""

    def __init__(self, files=None):
        self.files = files or {}

    def post_file(self, url, filename, data, content_type, timeout=60, fields=None):
        self.posted = {"url": url, "filename": filename, "fields": fields}
        return {"secure_url": "https://res.cloudinary.com/demo/image/upload/unicorn/abc.jpg"}

    def head(self, url, timeout=15):
        return 200 if url not in getattr(self, "dead", set()) else 404

    def get_bytes(self, url, max_bytes=6_000_000, timeout=30):
        from investor_agent.http import HttpError
        if url not in self.files:
            raise HttpError(404, "not found")
        return self.files[url]


class FakeImageHost:
    def __init__(self):
        self.uploads = []

    def upload_image(self, data, content_type):
        self.uploads.append((len(data), content_type))
        return "https://res.cloudinary.com/test/image/upload/unicorn/x.jpg"
