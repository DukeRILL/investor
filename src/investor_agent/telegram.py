"""Telegram Bot API and Telegraph clients. Tokens never appear in logs or errors."""

import json
import logging

from .http import Http, HttpError

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, http: Http, token: str):
        self.http, self._token = http, token

    def _call(self, method: str, payload: dict) -> dict:
        if not self._token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")
        try:
            data = self.http.request("POST", f"https://api.telegram.org/bot{self._token}/{method}",
                                     json_body=payload, timeout=60)
        except HttpError as exc:  # strip URL with token from the message
            raise TelegramError(f"{method} failed: HTTP {exc.status}") from None
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description") if isinstance(data, dict) else "bad response"
            raise TelegramError(f"{method} failed: {desc}")
        return data["result"]

    def send(self, chat_id: str, html: str, button: tuple[str, str] | None = None,
             disable_preview: bool = True) -> int:
        payload = {"chat_id": chat_id, "text": html, "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": disable_preview}}
        if button:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
        return int(self._call("sendMessage", payload)["message_id"])

    def send_photo(self, chat_id: str, photo_url: str, html: str, button: tuple[str, str] | None = None) -> int:
        """Photo by public URL with an HTML caption (max 1024 visible characters)."""
        payload = {"chat_id": chat_id, "photo": photo_url, "caption": html, "parse_mode": "HTML"}
        if button:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
        return int(self._call("sendPhoto", payload)["message_id"])

    def delete(self, chat_id: str, message_id: int) -> bool:
        return bool(self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}))

    def send_series(self, chat_id: str, parts: list[str], button: tuple[str, str] | None = None) -> list[int]:
        """Send all parts; if one fails, delete the ones already sent so no fragment stays."""
        sent: list[int] = []
        try:
            for i, part in enumerate(parts):
                sent.append(self.send(chat_id, part, button if i == len(parts) - 1 else None))
        except TelegramError:
            for mid in sent:
                try:
                    self.delete(chat_id, mid)
                except TelegramError:
                    log.warning("could not delete partial message")
            raise
        return sent


class TelegraphClient:
    API = "https://api.telegra.ph"

    def __init__(self, http: Http, token_getter, token_setter, short_name: str = "investor_duker",
                 author_name: str = "Школа единорогов", author_url: str = "https://t.me/investor_duker"):
        self.http = http
        self._get, self._set = token_getter, token_setter
        self.short_name, self.author_name, self.author_url = short_name, author_name, author_url

    def _call(self, method: str, payload: dict) -> dict:
        data = self.http.request("POST", f"{self.API}/{method}", json_body=payload, timeout=60)
        if not isinstance(data, dict) or not data.get("ok"):
            raise TelegramError(f"telegraph {method} failed: {data.get('error') if isinstance(data, dict) else data}")
        return data["result"]

    def _token(self) -> str:
        token = self._get()
        if not token:
            result = self._call("createAccount", {"short_name": self.short_name, "author_name": self.author_name,
                                                  "author_url": self.author_url})
            token = result["access_token"]
            self._set(token)
        return token

    def upload_image(self, data: bytes, content_type: str) -> str:
        """Upload a jpeg/png/gif to telegra.ph and return its absolute URL."""
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"}[content_type]
        result = self.http.post_file("https://telegra.ph/upload", f"image.{ext}", data, content_type)
        if not isinstance(result, list) or not result or not result[0].get("src"):
            raise TelegramError(f"telegraph upload failed: {str(result)[:120]}")
        return "https://telegra.ph" + result[0]["src"]

    def create_page(self, title: str, nodes: list) -> str:
        payload = {"access_token": self._token(), "title": title[:256], "author_name": self.author_name,
                   "author_url": self.author_url, "content": json.dumps(nodes, ensure_ascii=False, separators=(",", ":")),  # Telegraph caps content at 64 KB
                   "return_content": False}
        return self._call("createPage", payload)["url"]
